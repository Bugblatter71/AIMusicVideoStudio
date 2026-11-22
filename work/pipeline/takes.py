import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from .prompts import extract_anchor_id


def safe_link_or_copy(src: Path, dst: Path) -> bool:
    """Safely replace dst with a hardlink (or copy) to src.
    CRITICAL: Always unlinks dst first so we never truncate a shared NTFS inode in-place!
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"Source file missing: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        try:
            if dst.resolve() == src.resolve() and dst.stat().st_ino == src.stat().st_ino:
                return True
        except OSError:
            pass
        dst.unlink(missing_ok=True)
    try:
        os.link(str(src), str(dst))
        return True
    except OSError:
        shutil.copy2(str(src), str(dst))
        return True


_BUILTIN_RES_MAP = {"1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088"}
_P1_SYNC_KEYS = ("prompt_m", "seed_m", "stem_m", "anchor", "anchor_end", "split_x", "render_pad_start", "render_pad_end", "resolution", "filters", "negative_prompt", "negative_mode", "negative_prompt_m", "prompt_mode", "prompt_components", "video_renderer", "renderer", "renderer_m", "backend", "backend_m", "mode")
_P2_SYNC_KEYS = ("prompt_f", "seed_f", "stem_f", "resolution", "spatial_upsampling", "filters", "negative_prompt", "negative_mode", "negative_prompt_f", "prompt_mode", "prompt_components", "video_renderer", "renderer", "renderer_f", "backend", "backend_f", "pass2_roi_mode", "pass2_roi_side", "pass2_roi_box", "mode")

def _resolve_take_target_resolution(proj_data: dict, sc: dict, target_take: dict):
    """Return a resolution preset/WxH to write onto sc if effective resolution != take's rendered resolution."""
    rm = proj_data.get("models") or {}
    res_map = {**_BUILTIN_RES_MAP, **{str(k): str(v["resolution"] if isinstance(v, dict) else v) for k, v in (rm.get("video_resolutions") or {}).items() if (isinstance(v, dict) and v.get("resolution")) or (isinstance(v, str) and "x" in v)}}
    vs = rm.get("video_shared_settings") if isinstance(rm.get("video_shared_settings"), dict) else {}
    def_raw = rm.get("default_video_resolution") or rm.get("resolution") or vs.get("resolution") or proj_data.get("master_resolution") or 4
    sc_raw = sc.get("resolution")
    if sc_raw is not None and str(sc_raw).strip().lower() not in ("", "default", "inherit", "none", "null"):
        cur_res = res_map.get(str(sc_raw).strip(), str(sc_raw).strip())
    else:
        vr = (rm.get("video_renderers") or {}).get(str(sc.get("renderer") or sc.get("video_renderer") or ""))
        vr_res = vr.get("resolution") if isinstance(vr, dict) else None
        cur_res = res_map.get(str(vr_res or def_raw).strip(), str(vr_res or def_raw).strip())
    take_res = str(target_take.get("resolution") or "").strip() or (re.search(r"_(\d+x\d+)$", str(target_take.get("out_name") or "")) or [None, ""])[1]
    if not take_res or cur_res == take_res: return None
    for k, v in res_map.items():
        if v == take_res: return int(k) if k.isdigit() else k
    return take_res


def get_takes_manifest_path(proj_dir: Path) -> Path:
    return Path(proj_dir) / "work" / "takes_manifest.json"


def _find_strip(proj_dir: Path, sid: str, out_name: str) -> Path | None:
    p = Path(proj_dir)
    for d in (p / "work" / "frames_v3", p / "work" / "frames", p / "work" / "renders_v3"):
        for cand in (d / f"{sid}_strip.jpg", d / f"{out_name}_strip.jpg"):
            if cand.is_file() and cand.stat().st_size > 0: return cand
    return None


def _active_strip_path(proj_dir: Path, sid: str) -> Path:
    p = Path(proj_dir)
    fdir = p / "work" / "frames_v3"
    if not fdir.is_dir() and (p / "work" / "frames").is_dir(): fdir = p / "work" / "frames"
    return fdir / f"{sid}_strip.jpg"


def get_state_hashes_file(proj_dir: Path) -> Path:
    p1, p2 = Path(proj_dir) / "work" / "state_hashes.json", Path(proj_dir) / "work" / "jobs_v3" / "state_hashes.json"
    if p1.is_file() and p2.is_file(): return p1 if p1.stat().st_mtime >= p2.stat().st_mtime else p2
    return p1 if p1.is_file() else (p2 if p2.is_file() else p1)


def save_state_hashes_data(proj_dir: Path, data: dict) -> None:
    from .media_utils import atomic_write_json, locked_file_op
    pdir = Path(proj_dir)
    th = data.get("task_hashes") or data.get("tasks") or {}
    data["task_hashes"] = data["tasks"] = th
    with locked_file_op(pdir / "work" / "jobs_v3" / "state_hashes.lock"):
        for p in (pdir / "work" / "state_hashes.json", pdir / "work" / "jobs_v3" / "state_hashes.json"):
            if p.parent.is_dir():
                try: atomic_write_json(p, data)
                except Exception: pass


def load_takes_manifest(proj_dir: Path) -> dict:
    p = get_takes_manifest_path(proj_dir)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "scenes": {}}


def save_takes_manifest(proj_dir: Path, manifest: dict) -> None:
    from .media_utils import atomic_write_json
    atomic_write_json(get_takes_manifest_path(proj_dir), manifest)


def _pass_key(pass_num: int | None) -> str:
    return f"p{pass_num}" if pass_num is not None else "default"


def _build_seeded_take_entry(
    proj_dir: Path, sid: str, out_name: str, pass_num: int | None, mp4_path: Path, vault_rel_mp4: str,
    vault_rel_strip: str | None, t_hash: str, take_id: str, take_number: int,
    sc_info: dict, state_data: dict, settings_store: dict, scene_renders: dict, proj_data: dict, parts: list[str]
) -> dict:
    r_info = scene_renders.get(out_name, {})
    created_ts = r_info.get("completed_at") or mp4_path.stat().st_mtime
    iso = datetime.datetime.fromtimestamp(created_ts, datetime.timezone.utc).isoformat()
    def_res = str(sc_info.get("renderer") or proj_data.get("models", {}).get("default_video_renderer", 1))
    return {
        "take_id": take_id, "take_number": take_number, "scene_id": sid, "pass_num": pass_num,
        "out_name": out_name, "task_hash": t_hash, "seed": sc_info.get("seed"), "prompt": sc_info.get("prompt"),
        "anchor_id": sc_info.get("anchor"),
        "anchor_sha256": state_data.get("anchor_file_hashes", {}).get(sc_info.get("anchor", "")),
        "parent_p1_task_hash": None, "created_at": round(float(created_ts), 3), "created_iso": iso,
        "vault_rel_mp4": vault_rel_mp4, "vault_rel_strip": vault_rel_strip,
        "file_path": f"work/renders_v3/{vault_rel_mp4}",
        "strip_path": f"work/renders_v3/{vault_rel_strip}" if vault_rel_strip else None,
        "render_start": r_info.get("render_start", float(sc_info.get("start", 0))),
        "render_end": r_info.get("render_end", float(sc_info.get("end", 0))),
        "frames": r_info.get("frames", 0), "elapsed_s": r_info.get("elapsed", 0.0),
        "size_bytes": mp4_path.stat().st_size, "resolution": parts[-1] if parts and "x" in parts[-1] else "",
        "model_id": def_res, "model_type": settings_store.get(out_name, {}).get("model_type", ""),
        "scene_snapshot": copy.deepcopy(sc_info), "render_settings": settings_store.get(out_name, {}),
        "label": f"Take {take_number} (Baseline)", "is_baseline": True,
    }


def _dedup_scene_takes(sc_entry: dict) -> bool:
    takes = sc_entry.get("takes")
    if not isinstance(takes, list) or not takes:
        return False
    changed = False
    seen: set[str] = set()
    active_id = sc_entry.get("active_take_id")
    active_map = sc_entry.setdefault("active_takes", {})
    for idx, t in enumerate(takes):
        tid = str(t.get("take_id") or "")
        if not tid or tid in seen or tid in ("take_baseline", "take_baseline_default"):
            raw = f"{t.get('vault_rel_mp4') or t.get('out_name') or sid_fallback(t)}:{idx}"
            new_tid = f"take_{hashlib.sha1(raw.encode()).hexdigest()[:12]}"
            while new_tid in seen:
                new_tid = f"{new_tid}_{idx}"
            if tid and tid not in seen:
                if active_id == tid:
                    sc_entry["active_take_id"] = new_tid
                for pk, av in list(active_map.items()):
                    if av == tid:
                        active_map[pk] = new_tid
            t["take_id"] = new_tid
            changed = True
        seen.add(t["take_id"])
        if t.get("take_number") != idx + 1:
            t["take_number"] = idx + 1
            changed = True
    return changed


def sid_fallback(t: dict) -> str:
    return str(t.get("scene_id") or "scene")


def ensure_baseline_takes_seeded(proj_dir: Path, config_file: Path | None = None) -> dict:
    """Ensures existing renders in renders_v3 and takes vault are registered in takes_manifest.json."""
    from .media_utils import locked_file_op
    proj_dir = Path(proj_dir)
    renders_dir = proj_dir / "work" / "renders_v3"
    if not renders_dir.is_dir():
        return load_takes_manifest(proj_dir)
    with locked_file_op(get_takes_manifest_path(proj_dir).with_suffix(".lock")):
        return _ensure_baseline_takes_seeded_locked(proj_dir, renders_dir, config_file)


def _ensure_baseline_takes_seeded_locked(proj_dir: Path, renders_dir: Path, config_file: Path | None = None) -> dict:

    if config_file is None:
        jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
        config_file = jsons[-1] if jsons else (proj_dir / "missing.json")

    manifest = load_takes_manifest(proj_dir)
    scenes_map = manifest.setdefault("scenes", {})
    changed = False
    for sc_entry in scenes_map.values():
        if _dedup_scene_takes(sc_entry):
            changed = True

    state_file = get_state_hashes_file(proj_dir)
    state_data = {}
    if state_file.is_file():
        try: state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception: pass
    task_hashes = state_data.get("task_hashes") or state_data.get("tasks") or {}
    settings_store, scene_renders = state_data.get("settings_store", {}), state_data.get("scene_renders", {})

    proj_data = {}
    if Path(config_file).is_file():
        try: proj_data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        except Exception: pass
    scene_by_id = {s.get("id"): s for s in proj_data.get("scenes", []) if s.get("id")}

    # 1. Seed any active top-level renders_v3/*.mp4 not yet indexed by out_name
    for mp4_path in sorted(renders_dir.glob("*.mp4")):
        if mp4_path.name.endswith(".tmp.mp4") or "takes" in mp4_path.parts or mp4_path.stat().st_size < 50000:
            continue
        out_name = mp4_path.stem
        parts = out_name.split("_")
        sid = parts[0] if parts else out_name
        pass_num = 1 if "_Pass1" in out_name else (2 if "_Pass2" in out_name else None)
        pkey = _pass_key(pass_num)
        sc_entry = scenes_map.setdefault(sid, {"active_takes": {}, "takes": []})
        existing_takes = sc_entry.setdefault("takes", [])
        if any(t.get("out_name") == out_name for t in existing_takes):
            continue

        t_hash = task_hashes.get(out_name, "")
        t_hash_prefix = t_hash[:12] if t_hash else hashlib.sha1(f"{out_name}:{mp4_path.stat().st_size}".encode()).hexdigest()[:12]
        take_id = f"take_{t_hash_prefix}"
        if any(t.get("take_id") == take_id for t in existing_takes):
            t_hash_prefix = hashlib.sha1(f"{out_name}:{mp4_path.stat().st_size}:{len(existing_takes)}".encode()).hexdigest()[:12]
            take_id = f"take_{t_hash_prefix}"

        vault_rel_mp4 = f"takes/{sid}/{out_name}__{t_hash_prefix}.mp4"
        safe_link_or_copy(mp4_path, renders_dir / vault_rel_mp4)
        strip_path = _find_strip(proj_dir, sid, out_name)
        vault_rel_strip = None
        if strip_path and strip_path.is_file() and strip_path.stat().st_size > 0:
            vault_rel_strip = f"takes/{sid}/{out_name}__{t_hash_prefix}_strip.jpg"
            safe_link_or_copy(strip_path, renders_dir / vault_rel_strip)

        entry = _build_seeded_take_entry(
            proj_dir, sid, out_name, pass_num, mp4_path, vault_rel_mp4, vault_rel_strip,
            t_hash, take_id, len(existing_takes) + 1, scene_by_id.get(sid, {}),
            state_data, settings_store, scene_renders, proj_data, parts,
        )
        existing_takes.append(entry)
        sc_entry.setdefault("active_takes", {})[pkey] = take_id
        sc_entry["active_take_id"] = take_id
        sc_entry["is_baseline_seeded"] = True
        changed = True

    # 2. Index any vaulted MP4s in renders_v3/takes/ not yet referenced in manifest
    takes_root = renders_dir / "takes"
    if takes_root.is_dir():
        for sid_dir in sorted(takes_root.iterdir()):
            if not sid_dir.is_dir():
                continue
            sid = sid_dir.name
            sc_entry = scenes_map.setdefault(sid, {"active_takes": {}, "takes": []})
            existing_takes = sc_entry.setdefault("takes", [])
            known_rels = {t.get("vault_rel_mp4") for t in existing_takes if t.get("vault_rel_mp4")}
            known_ids = {t.get("take_id") for t in existing_takes if t.get("take_id")}
            for mp4_path in sorted(sid_dir.glob("*.mp4")):
                if mp4_path.name.endswith(".tmp.mp4") or mp4_path.stat().st_size < 50000:
                    continue
                rel_vault = f"takes/{sid}/{mp4_path.name}"
                if rel_vault in known_rels:
                    continue
                stem = mp4_path.stem
                out_name, h_part = stem.rsplit("__", 1) if "__" in stem else (stem, "")
                parts = out_name.split("_")
                pass_num = 1 if "_Pass1" in out_name else (2 if "_Pass2" in out_name else None)
                pkey = _pass_key(pass_num)
                use_h = h_part and h_part not in ("baseline", "baseline_default") and f"take_{h_part}" not in known_ids
                t_hash_prefix = h_part if use_h else hashlib.sha1(f"{rel_vault}:{mp4_path.stat().st_size}".encode()).hexdigest()[:12]
                take_id = f"take_{t_hash_prefix}"
                strip_cand = mp4_path.with_name(f"{stem}_strip.jpg")
                rel_strip = f"takes/{sid}/{strip_cand.name}" if strip_cand.is_file() and strip_cand.stat().st_size > 0 else None
                entry = _build_seeded_take_entry(
                    proj_dir, sid, out_name, pass_num, mp4_path, rel_vault, rel_strip,
                    task_hashes.get(out_name, ""), take_id, len(existing_takes) + 1,
                    scene_by_id.get(sid, {}), state_data, settings_store, scene_renders, proj_data, parts,
                )
                existing_takes.append(entry)
                known_rels.add(rel_vault)
                known_ids.add(take_id)
                sc_entry.setdefault("active_takes", {}).setdefault(pkey, take_id)
                if not sc_entry.get("active_take_id"):
                    sc_entry["active_take_id"] = take_id
                sc_entry["is_baseline_seeded"] = True
                changed = True

    if changed:
        save_takes_manifest(proj_dir, manifest)
    return manifest


def before_scene_render(ctx, sc: dict, out_name: str, out_mp4: Path, state: dict) -> None:
    """Pre-render hook: Archives active render if not already archived, then unlinks out_mp4.
    Unlinking is critical so new inference writes to a fresh inode and never truncates vaulted hardlinks.
    """
    out_mp4 = Path(out_mp4)
    c_file = getattr(ctx, "config_file", None) or getattr(ctx, "json_path", None)
    if out_mp4.is_file() and out_mp4.stat().st_size > 50000:
        ensure_baseline_takes_seeded(ctx.project_dir, c_file)
    # Remove active file link so subsequent write creates fresh inode
    out_mp4.unlink(missing_ok=True)


def restore_previous_active_take_if_missing(ctx, sid: str, out_name: str, out_mp4: Path) -> bool:
    """Safety fallback: If a render fails or is canceled, re-links the active take to out_mp4."""
    out_mp4 = Path(out_mp4)
    if out_mp4.is_file() and out_mp4.stat().st_size > 1000:
        return True
    pdir = getattr(ctx, "project_dir", None) or Path(ctx)
    sc_entry = load_takes_manifest(pdir).get("scenes", {}).get(sid, {})
    takes = sc_entry.get("takes", [])
    pkey = _pass_key(1 if "_Pass1" in out_name else (2 if "_Pass2" in out_name else None))
    active_id = sc_entry.get("active_takes", {}).get(pkey) or sc_entry.get("active_take_id")
    cands = [t for t in takes if t.get("take_id") == active_id and t.get("out_name") == out_name]
    cands += [t for t in reversed(takes) if t.get("out_name") == out_name and t not in cands]
    for t in cands:
        v_path = pdir / "work" / "renders_v3" / (t.get("vault_rel_mp4") or "")
        if v_path.is_file():
            safe_link_or_copy(v_path, out_mp4)
            return True
    return False


def after_scene_render(
    ctx, sc: dict, pass_num: int | None, out_name: str, out_mp4: Path,
    t_hash: str, t_dict: dict, elapsed: float, state: dict
) -> dict:
    """Post-render hook: Stores generated clip in vault and promotes it to active."""
    out_mp4 = Path(out_mp4)
    renders_dir = ctx.project_dir / "work" / "renders_v3"
    sid = sc.get("id") or out_name.split("_")[0]
    t_hash_prefix = t_hash[:12] if t_hash else "take"
    take_id = f"take_{t_hash_prefix}"
    pkey = _pass_key(pass_num)

    vault_rel_mp4 = f"takes/{sid}/{out_name}__{t_hash_prefix}.mp4"
    vault_mp4 = renders_dir / vault_rel_mp4
    vault_mp4.parent.mkdir(parents=True, exist_ok=True)

    # Copy output to vault if not already there, then link active
    if out_mp4.resolve() != vault_mp4.resolve():
        shutil.copy2(str(out_mp4), str(vault_mp4))
        safe_link_or_copy(vault_mp4, out_mp4)

    # Handle filmstrip thumbnail
    strip_src = _find_strip(ctx.project_dir, sid, out_name)
    vault_rel_strip = None
    if strip_src and strip_src.is_file():
        vault_rel_strip = f"takes/{sid}/{out_name}__{t_hash_prefix}_strip.jpg"
        safe_link_or_copy(strip_src, renders_dir / vault_rel_strip)

    from .media_utils import locked_file_op
    with locked_file_op(get_takes_manifest_path(ctx.project_dir).with_suffix(".lock")):
        manifest = load_takes_manifest(ctx.project_dir)
        sc_entry = manifest.setdefault("scenes", {}).setdefault(sid, {"active_takes": {}, "takes": []})
        existing = sc_entry.setdefault("takes", [])
        created_ts = time.time()
        iso = datetime.datetime.fromtimestamp(created_ts, datetime.timezone.utc).isoformat()
        def_res = str(sc.get("renderer") or (getattr(ctx, "models", {}) or {}).get("default_video_renderer", 1))
        take_entry = {
            "take_id": take_id, "take_number": len(existing) + 1, "scene_id": sid, "pass_num": pass_num,
            "out_name": out_name, "task_hash": t_hash, "seed": t_dict.get("seed", sc.get("seed")),
            "prompt": t_dict.get("prompt", sc.get("prompt")), "anchor_id": sc.get("anchor"),
            "anchor_sha256": state.get("anchor_file_hashes", {}).get(extract_anchor_id(sc.get("anchor", "") or "")),
            "parent_p1_task_hash": t_dict.get("parent_p1_task_hash") if pass_num == 2 else None,
            "created_at": round(created_ts, 3), "created_iso": iso, "vault_rel_mp4": vault_rel_mp4, "vault_rel_strip": vault_rel_strip,
            "file_path": f"work/renders_v3/{vault_rel_mp4}", "strip_path": f"work/renders_v3/{vault_rel_strip}" if vault_rel_strip else None,
            "render_start": t_dict.get("render_start", float(sc.get("start", 0))), "render_end": t_dict.get("render_end", float(sc.get("end", 0))),
            "frames": t_dict.get("video_length", 0), "elapsed_s": round(float(elapsed), 1),
            "size_bytes": out_mp4.stat().st_size if out_mp4.exists() else 0, "resolution": str(t_dict.get("resolution", "")), "model_id": def_res,
            "model_type": str(t_dict.get("model_type", "")), "backend": str(t_dict.get("_backend") or t_dict.get("backend") or "local"),
            "fal_endpoint": str((t_dict.get("_fal_spec") or {}).get("endpoint") or t_dict.get("fal_endpoint") or ""),
            "scene_snapshot": copy.deepcopy(sc),
            "render_settings": {k: v for k, v in t_dict.items() if k not in ("audio_path", "image_start", "image_end") and not str(k).startswith("_")},
            "label": f"Take {len(existing) + 1}",
        }
        idx = next((i for i, t in enumerate(existing) if t.get("take_id") == take_id and t.get("out_name") == out_name), None)
        if idx is not None: existing[idx] = take_entry
        else: existing.append(take_entry)
        sc_entry.setdefault("active_takes", {})[pkey] = take_id
        sc_entry["active_take_id"] = take_id
        save_takes_manifest(ctx.project_dir, manifest)
        return take_entry


def _adjust_promote_mtime(renders_dir: Path, active_mp4: Path, out_name: str, pass_num, this_take: dict, sc_entry: dict, takes: list, safe_mtime: float) -> None:
    if pass_num == 2 and "_Pass2" in out_name:
        p1_mp4 = renders_dir / f"{out_name.replace('_Pass2_Final', '_Pass1_Left')}.mp4"
        if p1_mp4.is_file(): safe_mtime = max(safe_mtime, p1_mp4.stat().st_mtime + 2.0)
    elif pass_num == 1 and "_Pass1_Left" in out_name:
        p2_mp4 = renders_dir / f"{out_name.replace('_Pass1_Left', '_Pass2_Final')}.mp4"
        p2_tk = next((x for x in takes if x.get("take_id") == sc_entry.get("active_takes", {}).get("p2")), None)
        if p2_mp4.is_file() and p2_tk and float(p2_tk.get("created_at") or 0) >= float(this_take.get("created_at") or 0):
            if not p2_tk.get("parent_p1_task_hash") or p2_tk.get("parent_p1_task_hash") == this_take.get("task_hash"):
                safe_mtime = min(safe_mtime, p2_mp4.stat().st_mtime - 2.0)
    try: os.utime(str(active_mp4), (safe_mtime, safe_mtime))
    except OSError: pass


def try_auto_promote_vault_hit(proj_dir: Path, *args, **kwargs) -> bool:
    """Checks vault for an existing take matching hashes and promotes it automatically."""
    proj_dir = Path(proj_dir)
    manifest = load_takes_manifest(proj_dir)

    if args and isinstance(args[0], (Path, str)) and (str(args[0]).endswith(".json") or Path(args[0]).is_file()):
        config_file, sid = Path(args[0]), str(args[1]) if len(args) > 1 else kwargs.get("sid", "")
        pass_num = args[2] if len(args) > 2 else kwargs.get("pass_num")
        h0 = args[3] if len(args) > 3 else kwargs.get("cur_task_hash", "")
        valid_hashes = {h0} if h0 else set()
        out_name = sc_start = sc_end = cur_anchor_hash = None
    else:
        config_file = None
        sid = str(args[0]) if len(args) > 0 else kwargs.get("sid", "")
        pass_num = args[1] if len(args) > 1 else kwargs.get("pass_num")
        out_name = str(args[2]) if len(args) > 2 else kwargs.get("out_name")
        ch = args[3] if len(args) > 3 else kwargs.get("curr_hashes", ())
        valid_hashes = set(ch) if isinstance(ch, (list, tuple, set)) else {str(ch)}
        sc_start, sc_end = (args[4] if len(args) > 4 else kwargs.get("sc_start")), (args[5] if len(args) > 5 else kwargs.get("sc_end"))
        cur_anchor_hash = args[6] if len(args) > 6 else kwargs.get("cur_anchor_hash")

    sc_entry = manifest.get("scenes", {}).get(sid, {})
    takes = sc_entry.get("takes", [])
    if not takes:
        return False

    renders_dir, state_file, state = proj_dir / "work" / "renders_v3", get_state_hashes_file(proj_dir), {}
    if state_file.is_file():
        try: state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception: pass

    for t in reversed(takes):
        if out_name and t.get("out_name") != out_name: continue
        if valid_hashes and t.get("task_hash") not in valid_hashes: continue
        v_path = renders_dir / (t.get("vault_rel_mp4") or "")
        if not v_path.is_file() or v_path.stat().st_size < 50000: continue
        if sc_start is not None and sc_end is not None:
            r_start, r_end, snap = float(t.get("render_start", 0)), float(t.get("render_end", 0)), (t.get("scene_snapshot") or {})
            allow_pad = bool(snap.get("hold_last_frame") or snap.get("pad_tail") or snap.get("allow_tail_pad") or kwargs.get("allow_tail_pad"))
            if not ((r_start <= sc_start + 0.05) and (allow_pad or r_end >= sc_end - 0.05)): continue
        if cur_anchor_hash and t.get("anchor_sha256") and t.get("anchor_sha256") != cur_anchor_hash: continue

        target_out_name = t.get("out_name") or out_name or f"{sid}_solo"
        active_mp4 = renders_dir / f"{target_out_name}.mp4"
        safe_link_or_copy(v_path, active_mp4)
        v_strip = renders_dir / (t.get("vault_rel_strip") or "")
        if v_strip.is_file(): safe_link_or_copy(v_strip, _active_strip_path(proj_dir, sid))

        safe_mtime = max(time.time(), v_path.stat().st_mtime + 2.0)
        aid = t.get("scene_snapshot", {}).get("anchor")
        if not aid and config_file and Path(config_file).is_file():
            try:
                sc_found = next((s for s in json.loads(Path(config_file).read_text(encoding="utf-8")).get("scenes", []) if s.get("id") == sid), None)
                if sc_found: aid = sc_found.get("anchor")
            except Exception: pass
        if aid:
            anchors_dir = proj_dir / "work" / "anchors_v3"
            for f in (anchors_dir / f"{aid}.png", *anchors_dir.glob(f"{aid}*")):
                if f.is_file(): safe_mtime = max(safe_mtime, f.stat().st_mtime + 2.0); break
        _adjust_promote_mtime(renders_dir, active_mp4, target_out_name, pass_num, t, sc_entry, takes, safe_mtime)

        state.setdefault("task_hashes", {})[target_out_name] = t["task_hash"]
        state.setdefault("tasks", {})[target_out_name] = t["task_hash"]
        if cur_anchor_hash and aid:
            state.setdefault("anchor_file_hashes", {})[extract_anchor_id(aid)] = cur_anchor_hash
        if t.get("render_settings"): state.setdefault("settings_store", {})[target_out_name] = t["render_settings"]
        state.setdefault("scene_renders", {})[target_out_name] = {
            "render_start": t.get("render_start"), "render_end": t.get("render_end"),
            "frames": t.get("frames"), "elapsed": t.get("elapsed_s"), "completed_at": t.get("created_at"),
        }
        save_state_hashes_data(proj_dir, state)

        sc_entry.setdefault("active_takes", {})[_pass_key(pass_num)] = t["take_id"]
        sc_entry["active_take_id"] = t["take_id"]
        save_takes_manifest(proj_dir, manifest)
        return True
    return False


def promote_take_to_active(
    proj_dir: Path, config_file: Path, sid: str,
    take_id_or_pass: str | int | None,
    take_id: str | None = None,
    restore_timing: bool = False
) -> dict:
    """Restores scene parameters from take snapshot, hardlinks take MP4 to active, and updates state hashes."""
    proj_dir = Path(proj_dir)
    config_file = Path(config_file)
    if take_id is None:
        take_id = str(take_id_or_pass)
        pass_num = None
    else:
        pass_num = take_id_or_pass

    manifest = load_takes_manifest(proj_dir)
    sc_entry = manifest.get("scenes", {}).get(sid, {})
    takes = sc_entry.get("takes", [])
    target_take = next((t for t in takes if t.get("take_id") == take_id and t.get("pass_num") == pass_num), None) if pass_num is not None else None
    if not target_take: target_take = next((t for t in takes if t.get("take_id") == take_id), None)
    if not target_take: raise ValueError(f"Take {take_id} not found for scene {sid}")
    renders_dir, out_name = proj_dir / "work" / "renders_v3", target_take["out_name"]
    vault_mp4, active_mp4 = renders_dir / target_take["vault_rel_mp4"], renders_dir / f"{out_name}.mp4"
    if not vault_mp4.is_file(): raise FileNotFoundError(f"Vault video file missing: {vault_mp4}")
    # 1. Update project.json scene settings
    proj_data = json.loads(config_file.read_text(encoding="utf-8"))
    target_sc = next((s for s in proj_data.get("scenes", []) if s.get("id") == sid), None)
    if not target_sc: raise ValueError(f"Scene {sid} not found in project")

    snap = target_take.get("scene_snapshot", {})
    was_locked = target_sc.get("locked")
    if pass_num in (1, 2):
        sync_keys = _P1_SYNC_KEYS if pass_num == 1 else _P2_SYNC_KEYS
        keep_req = ("prompt_m", "seed_m", "stem_m", "anchor", "split_x", "mode") if pass_num == 1 else ("prompt_f", "seed_f", "stem_f", "split_x", "mode")
        for k in sync_keys:
            if k in snap and snap[k] is not None: target_sc[k] = copy.deepcopy(snap[k])
            elif k not in keep_req: target_sc.pop(k, None)
    elif snap:
        cur_id = target_sc.get("id") or sid
        keep_timing = {} if restore_timing else {k: target_sc[k] for k in ("start", "end") if k in target_sc}
        target_sc.clear()
        target_sc.update(copy.deepcopy(snap))
        target_sc["id"] = cur_id
        target_sc.update(keep_timing)
    res_fix = _resolve_take_target_resolution(proj_data, target_sc, target_take)
    if res_fix is not None:
        target_sc["resolution"] = res_fix
    if was_locked is not None:
        target_sc["locked"] = was_locked

    if restore_timing and "start" in snap and "end" in snap:
        target_sc["start"], target_sc["end"] = snap["start"], snap["end"]

    config_file.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")
    safe_link_or_copy(vault_mp4, active_mp4)

    active_strip = _active_strip_path(proj_dir, sid)
    vault_strip = renders_dir / (target_take.get("vault_rel_strip") or "")
    if vault_strip.is_file(): safe_link_or_copy(vault_strip, active_strip)
    for stale in active_strip.parent.glob(f"{sid}_strip_*f.jpg"): stale.unlink(missing_ok=True)

    safe_mtime = max(time.time(), vault_mp4.stat().st_mtime + 2.0)
    for a_ref in (target_sc.get("anchor"), target_sc.get("anchor_end")):
        if a_ref:
            for f in (proj_dir / "work" / "anchors_v3").glob(f"{extract_anchor_id(a_ref)}*"):
                if f.is_file(): safe_mtime = max(safe_mtime, f.stat().st_mtime + 2.0); break
    eff_pass = target_take.get("pass_num") if pass_num is None else pass_num
    _adjust_promote_mtime(renders_dir, active_mp4, out_name, eff_pass, target_take, sc_entry, takes, safe_mtime)

    state_file, state_data = get_state_hashes_file(proj_dir), {}
    if state_file.is_file():
        try: state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception: pass
    t_hash = target_take.get("task_hash") or ""
    for hk in ("task_hashes", "tasks"):
        if t_hash: state_data.setdefault(hk, {})[out_name] = t_hash
        else: state_data.get(hk, {}).pop(out_name, None)
    if target_take.get("render_settings"): state_data.setdefault("settings_store", {})[out_name] = target_take["render_settings"]
    state_data.setdefault("scene_renders", {})[out_name] = {
        "render_start": target_take.get("render_start"), "render_end": target_take.get("render_end"),
        "frames": target_take.get("frames"), "elapsed": target_take.get("elapsed_s"), "completed_at": target_take.get("created_at"),
    }
    save_state_hashes_data(proj_dir, state_data)

    pkey = _pass_key(target_take.get("pass_num") if pass_num is None else pass_num)
    sc_entry.setdefault("active_takes", {})[pkey] = take_id
    sc_entry["active_take_id"] = take_id
    save_takes_manifest(proj_dir, manifest)
    return {"success": True, "active_take_id": take_id, "restored_params": copy.deepcopy(target_sc), "scene": target_sc, "take": target_take}


def delete_scene_take(proj_dir: Path, config_file: Path | None, sid: str, take_id: str) -> dict:
    """Deletes a take from manifest and vault, re-linking active render or cleaning state if needed."""
    proj_dir = Path(proj_dir)
    manifest = ensure_baseline_takes_seeded(proj_dir, config_file) if config_file else load_takes_manifest(proj_dir)
    sc_entry = manifest.get("scenes", {}).get(sid, {})
    takes = sc_entry.get("takes", [])
    idx = next((i for i, t in enumerate(takes) if t.get("take_id") == take_id), None)
    if idx is None:
        return {"success": False, "error": f"Take {take_id} not found for scene {sid}"}

    renders_dir = proj_dir / "work" / "renders_v3"
    target = takes.pop(idx)
    v_rel, s_rel = target.get("vault_rel_mp4"), target.get("vault_rel_strip")
    all_remaining = [t for s in manifest.get("scenes", {}).values() for t in s.get("takes", [])]
    if v_rel and not any(t.get("vault_rel_mp4") == v_rel for t in all_remaining):
        (renders_dir / v_rel).unlink(missing_ok=True)
    if s_rel and not any(t.get("vault_rel_strip") == s_rel for t in all_remaining):
        (renders_dir / s_rel).unlink(missing_ok=True)

    out_name = target.get("out_name") or ""
    pass_num = target.get("pass_num")
    pkey = _pass_key(pass_num)
    was_active = sc_entry.get("active_take_id") == take_id or sc_entry.get("active_takes", {}).get(pkey) == take_id

    for i, t in enumerate(takes):
        t["take_number"] = i + 1
    same_pass = [t for t in takes if _pass_key(t.get("pass_num")) == pkey]
    if sc_entry.get("active_takes", {}).get(pkey) == take_id:
        if same_pass: sc_entry["active_takes"][pkey] = same_pass[-1]["take_id"]
        else: sc_entry["active_takes"].pop(pkey, None)
    if sc_entry.get("active_take_id") == take_id:
        sc_entry["active_take_id"] = same_pass[-1]["take_id"] if same_pass else (takes[-1]["take_id"] if takes else None)

    state_file, state_data = get_state_hashes_file(proj_dir), {}
    if state_file.is_file():
        try: state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception: pass
    state_changed = False

    if out_name:
        active_mp4 = renders_dir / f"{out_name}.mp4"
        same_out = [t for t in takes if t.get("out_name") == out_name and (renders_dir / (t.get("vault_rel_mp4") or "")).is_file()]
        if not same_out:
            active_mp4.unlink(missing_ok=True)
            for k in ("task_hashes", "tasks", "scene_renders", "settings_store"):
                if out_name in state_data.get(k, {}):
                    state_data[k].pop(out_name, None); state_changed = True
        elif was_active or not active_mp4.exists():
            repl = same_out[-1]
            safe_link_or_copy(renders_dir / repl["vault_rel_mp4"], active_mp4)
            if repl.get("vault_rel_strip") and (renders_dir / repl["vault_rel_strip"]).is_file():
                safe_link_or_copy(renders_dir / repl["vault_rel_strip"], _active_strip_path(proj_dir, sid))
            for hk in ("task_hashes", "tasks"):
                if repl.get("task_hash"): state_data.setdefault(hk, {})[out_name] = repl["task_hash"]
                else: state_data.get(hk, {}).pop(out_name, None)
            state_data.setdefault("scene_renders", {})[out_name] = {
                "render_start": repl.get("render_start"), "render_end": repl.get("render_end"),
                "frames": repl.get("frames"), "elapsed": repl.get("elapsed_s"), "completed_at": repl.get("created_at"),
            }
            state_changed = True

    new_active = next((t for t in takes if t.get("take_id") == sc_entry.get("active_take_id")), None)
    if new_active and new_active.get("out_name") and (renders_dir / (new_active.get("vault_rel_mp4") or "")).is_file():
        a_path = renders_dir / f"{new_active['out_name']}.mp4"
        if not a_path.exists():
            safe_link_or_copy(renders_dir / new_active["vault_rel_mp4"], a_path)

    if state_changed:
        save_state_hashes_data(proj_dir, state_data)
    save_takes_manifest(proj_dir, manifest)
    return {
        "success": True, "scene_id": sid, "deleted_take_id": take_id,
        "active_take_id": sc_entry.get("active_take_id"),
        "active_takes": sc_entry.get("active_takes", {}),
        "takes": takes, "vault_stats": get_vault_stats(proj_dir),
    }


def create_project_checkpoint(
    proj_dir: Path, config_file: Path, name: str = "", trigger: str = "manual", note: str = "", comment: str = ""
) -> dict:
    """Saves project.json, state_hashes.json, and active_takes.json in checkpoints/<ckpt_id>/."""
    proj_dir, config_file = Path(proj_dir), Path(config_file)
    ckpts_dir = proj_dir / "checkpoints"
    ckpts_dir.mkdir(parents=True, exist_ok=True)
    index_file = ckpts_dir / "index.json"
    index_data = {"version": 1, "checkpoints": []}
    if index_file.is_file():
        try: index_data = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception: pass

    proj_bytes = config_file.read_bytes() if config_file.is_file() else b""
    proj_hash = hashlib.sha256(proj_bytes).hexdigest()[:16]
    ckpts = index_data.get("checkpoints", [])
    if trigger == "pre_render" and ckpts and ckpts[0].get("trigger") == "pre_render" and ckpts[0].get("proj_hash") == proj_hash:
        return ckpts[0]

    note = note or comment
    ts = time.time()
    iso = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    ts_str = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    ckpt_id = f"ckpt_{ts_str}_{trigger}"
    ckpt_dir = ckpts_dir / ckpt_id
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / "project.json").write_bytes(proj_bytes)
    state_file = get_state_hashes_file(proj_dir)
    if state_file.is_file(): shutil.copy2(str(state_file), str(ckpt_dir / "state_hashes.json"))
    manifest = load_takes_manifest(proj_dir)
    (ckpt_dir / "active_takes.json").write_text(json.dumps({sid: sd.get("active_takes", {}) for sid, sd in manifest.get("scenes", {}).items()}, indent=2), encoding="utf-8")

    ckpt_meta = {
        "id": ckpt_id, "name": name or f"Checkpoint {ts_str}", "trigger": trigger,
        "note": note, "comment": note, "timestamp": round(ts, 3),
        "created_at": iso, "created_iso": iso, "proj_hash": proj_hash,
    }
    ckpts.insert(0, ckpt_meta)
    auto_ckpts = [c for c in ckpts if c.get("trigger") == "pre_render"]
    if len(auto_ckpts) > 30:
        for c_del in auto_ckpts[30:]:
            ckpts.remove(c_del); shutil.rmtree(ckpts_dir / c_del["id"], ignore_errors=True)
    index_data["checkpoints"] = ckpts
    index_file.write_text(json.dumps(index_data, indent=2), encoding="utf-8")
    return ckpt_meta


def list_project_checkpoints(proj_dir: Path) -> list:
    index_file = Path(proj_dir) / "checkpoints" / "index.json"
    if index_file.is_file():
        try: return json.loads(index_file.read_text(encoding="utf-8")).get("checkpoints", [])
        except Exception: pass
    return []


def rollback_project_checkpoint(proj_dir: Path, config_file: Path | str | None = None, checkpoint_id: str = "") -> dict:
    """Restores project.json, state_hashes.json, and active take hardlinks to checkpoint state."""
    proj_dir = Path(proj_dir)
    if isinstance(config_file, str) and not checkpoint_id and ("ckpt_" in config_file or Path(config_file).name.startswith("ckpt_")):
        checkpoint_id, config_file = config_file, None
    if config_file is None:
        jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
        if not jsons: raise FileNotFoundError(f"Project config JSON not found in {proj_dir}")
        config_file = jsons[-1]
    config_file = Path(config_file)
    ckpt_dir = proj_dir / "checkpoints" / checkpoint_id
    if not ckpt_dir.is_dir(): raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found at {ckpt_dir}")
    create_project_checkpoint(proj_dir, config_file, name=f"Auto: Before rollback to {checkpoint_id}", trigger="pre_rollback")

    ckpt_proj, ckpt_state = ckpt_dir / "project.json", ckpt_dir / "state_hashes.json"
    if ckpt_proj.is_file(): shutil.copy2(str(ckpt_proj), str(config_file))
    if ckpt_state.is_file():
        try: save_state_hashes_data(proj_dir, json.loads(ckpt_state.read_text(encoding="utf-8")))
        except Exception:
            for st in (proj_dir / "work" / "state_hashes.json", proj_dir / "work" / "jobs_v3" / "state_hashes.json"):
                if st.parent.is_dir(): shutil.copy2(str(ckpt_state), str(st))

    active_map_file, restored_count = ckpt_dir / "active_takes.json", 0
    if active_map_file.is_file():
        try:
            active_map = json.loads(active_map_file.read_text(encoding="utf-8"))
            manifest, renders_dir = load_takes_manifest(proj_dir), proj_dir / "work" / "renders_v3"
            for sid, pmap in active_map.items():
                sc_entry = manifest.get("scenes", {}).get(sid, {})
                takes = sc_entry.get("takes", [])
                for pkey, tid in pmap.items():
                    t_obj = next((t for t in takes if t.get("take_id") == tid), None)
                    if not t_obj: continue
                    v_mp4, a_mp4 = renders_dir / t_obj["vault_rel_mp4"], renders_dir / f"{t_obj['out_name']}.mp4"
                    if v_mp4.is_file(): safe_link_or_copy(v_mp4, a_mp4); restored_count += 1
                    v_strip = renders_dir / (t_obj.get("vault_rel_strip") or "")
                    if v_strip.is_file(): safe_link_or_copy(v_strip, _active_strip_path(proj_dir, t_obj.get("scene_id", "")))
                    sc_entry.setdefault("active_takes", {})[pkey] = tid
                    sc_entry["active_take_id"] = tid
            save_takes_manifest(proj_dir, manifest)
        except Exception: pass
    return {"success": True, "restored_checkpoint_id": checkpoint_id, "restored_takes_count": restored_count}


def get_vault_stats(proj_dir: Path) -> dict:
    vault_dir = Path(proj_dir) / "work" / "renders_v3" / "takes"
    total_bytes, take_files = 0, 0
    if vault_dir.is_dir():
        for f in vault_dir.rglob("*.mp4"):
            if f.is_file(): total_bytes += f.stat().st_size; take_files += 1
    manifest = load_takes_manifest(proj_dir)
    return {"total_scenes": len(manifest.get("scenes", {})), "total_takes": take_files, "total_mb": round(total_bytes / (1024 * 1024), 2)}


def handle_takes_api(handler, action: str, path: str = "", body: dict | None = None, slug: str = ""):
    """REST API dispatcher for scene takes and project checkpoints."""
    from http import HTTPStatus
    srv = sys.modules.get("work.ui.server") or sys.modules.get("server")
    projects_dir = (getattr(srv, "PROJECTS_DIR", None) if srv else None) or Path("projects")

    def _resolve_proj(p_slug: str):
        pdir = projects_dir / p_slug
        if not pdir.is_dir(): return None, None
        jsons = [j for j in sorted(pdir.glob("*.json")) if not j.name.startswith("state_hashes")]
        return pdir, (jsons[-1] if jsons else None)

    if action == "get_takes":
        parts = path[len("/api/takes/"):].strip("/").split("/")
        if len(parts) < 2: handler.send_error_json("Invalid takes path", status=HTTPStatus.BAD_REQUEST); return
        p_slug, sid = parts[0], parts[1]
        pdir, cfile = _resolve_proj(p_slug)
        if not pdir: handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        manifest = ensure_baseline_takes_seeded(pdir, cfile) if (cfile and cfile.is_file()) else load_takes_manifest(pdir)
        sc_data = manifest.get("scenes", {}).get(sid, {})
        handler.send_json({
            "success": True, "scene_id": sid, "active_take_id": sc_data.get("active_take_id"),
            "active_takes": sc_data.get("active_takes", {}), "takes": sc_data.get("takes", []),
        }); return

    if action == "get_checkpoints":
        pdir, _ = _resolve_proj(path[len("/api/checkpoints/"):].strip("/"))
        if not pdir: handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        handler.send_json({"success": True, "checkpoints": list_project_checkpoints(pdir), "vault_stats": get_vault_stats(pdir)}); return

    if action == "get_vault_stats":
        pdir, _ = _resolve_proj(path[len("/api/vault/stats/"):].strip("/"))
        if not pdir: handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        handler.send_json({"success": True, **get_vault_stats(pdir)}); return

    body = body or {}
    if action in ("activate", "delete_take"):
        p_slug, sid, tid = body.get("project") or body.get("slug"), body.get("scene_id"), body.get("take_id")
        if not p_slug or not sid or not tid: handler.send_error_json("project, scene_id, and take_id are required"); return
        pdir, cfile = _resolve_proj(p_slug)
        if not pdir or (action == "activate" and not cfile): handler.send_error_json("Project or config not found", status=HTTPStatus.NOT_FOUND); return
        try:
            res = promote_take_to_active(pdir, cfile, sid, tid, restore_timing=bool(body.get("restore_timing"))) if action == "activate" else delete_scene_take(pdir, cfile, sid, tid)
            if not res.get("success", True): handler.send_error_json(res.get("error", "Failed"), status=HTTPStatus.NOT_FOUND); return
            if srv and hasattr(srv, "compute_fast_project_queue"): srv.compute_fast_project_queue(p_slug, force_recheck=True)
            handler.send_json({"success": True, **res})
        except Exception as e: handler.send_error_json(str(e))
        return

    if action == "create":
        p_slug = body.get("project") or body.get("slug")
        name, comment = body.get("name", "").strip() or "Manual Checkpoint", body.get("comment", "").strip()
        if not p_slug: handler.send_error_json("project is required"); return
        pdir, cfile = _resolve_proj(p_slug)
        if not pdir or not cfile: handler.send_error_json("Project or config not found", status=HTTPStatus.NOT_FOUND); return
        try: handler.send_json({"success": True, "checkpoint": create_project_checkpoint(pdir, cfile, name=name, trigger="manual", comment=comment)})
        except Exception as e: handler.send_error_json(str(e))
        return

    if action == "rollback":
        p_slug, cid = body.get("project") or body.get("slug"), body.get("checkpoint_id")
        if not p_slug or not cid: handler.send_error_json("project and checkpoint_id are required"); return
        pdir, _ = _resolve_proj(p_slug)
        if not pdir: handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        try:
            res = rollback_project_checkpoint(pdir, cid)
            if srv and hasattr(srv, "compute_fast_project_queue"): srv.compute_fast_project_queue(p_slug, force_recheck=True)
            handler.send_json({"success": True, **res})
        except Exception as e: handler.send_error_json(str(e))
        return

    handler.send_error_json(f"Unknown takes API action: {action}")
