import copy
import datetime
import hashlib
import json
import os
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


def get_takes_manifest_path(proj_dir: Path) -> Path:
    return Path(proj_dir) / "work" / "takes_manifest.json"


def get_state_hashes_file(proj_dir: Path) -> Path:
    p1 = Path(proj_dir) / "work" / "state_hashes.json"
    p2 = Path(proj_dir) / "work" / "jobs_v3" / "state_hashes.json"
    if p1.is_file(): return p1
    if p2.is_file(): return p2
    return p1


def save_state_hashes_data(proj_dir: Path, data: dict) -> None:
    pdir = Path(proj_dir)
    # Ensure tasks and task_hashes are synced
    th = data.get("task_hashes") or data.get("tasks") or {}
    data["task_hashes"] = th
    data["tasks"] = th
    for p in (pdir / "work" / "state_hashes.json", pdir / "work" / "jobs_v3" / "state_hashes.json"):
        if p.parent.is_dir():
            try:
                p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass


def load_takes_manifest(proj_dir: Path) -> dict:
    p = get_takes_manifest_path(proj_dir)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "scenes": {}}


def save_takes_manifest(proj_dir: Path, manifest: dict) -> None:
    p = get_takes_manifest_path(proj_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _pass_key(pass_num: int | None) -> str:
    return f"p{pass_num}" if pass_num is not None else "default"


def ensure_baseline_takes_seeded(proj_dir: Path, config_file: Path) -> dict:
    """Ensures existing renders in renders_v3 are registered as baseline takes in takes_manifest.json."""
    proj_dir = Path(proj_dir)
    renders_dir = proj_dir / "work" / "renders_v3"
    if not renders_dir.is_dir():
        return load_takes_manifest(proj_dir)

    manifest = load_takes_manifest(proj_dir)
    scenes_map = manifest.setdefault("scenes", {})
    state_file = get_state_hashes_file(proj_dir)
    state_data = {}
    if state_file.is_file():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    task_hashes = state_data.get("task_hashes") or state_data.get("tasks") or {}
    settings_store = state_data.get("settings_store", {})
    scene_renders = state_data.get("scene_renders", {})

    proj_data = {}
    if Path(config_file).is_file():
        try:
            proj_data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        except Exception:
            pass
    scenes = proj_data.get("scenes", [])
    scene_by_id = {s.get("id"): s for s in scenes if s.get("id")}

    changed = False
    for mp4_path in sorted(renders_dir.glob("*.mp4")):
        if mp4_path.name.endswith(".tmp.mp4") or "takes" in mp4_path.parts:
            continue
        if mp4_path.stat().st_size < 50000:
            continue
        out_name = mp4_path.stem
        # Derive scene_id and pass
        parts = out_name.split("_")
        sid = parts[0] if parts else out_name
        pass_num = 1 if "_Pass1" in out_name else (2 if "_Pass2" in out_name else None)
        pkey = _pass_key(pass_num)

        sc_entry = scenes_map.setdefault(sid, {"active_takes": {}, "takes": []})
        existing_takes = sc_entry.setdefault("takes", [])
        active_map = sc_entry.setdefault("active_takes", {})

        # Check if already seeded for this out_name
        if any(t.get("out_name") == out_name for t in existing_takes):
            continue

        sc_info = scene_by_id.get(sid, {})
        t_hash = task_hashes.get(out_name, "")
        t_hash_prefix = t_hash[:12] if t_hash else f"baseline_{pkey}"
        take_id = f"take_{t_hash_prefix}"

        vault_rel_mp4 = f"takes/{sid}/{out_name}__{t_hash_prefix}.mp4"
        vault_mp4 = renders_dir / vault_rel_mp4
        safe_link_or_copy(mp4_path, vault_mp4)

        strip_path = renders_dir / f"{out_name}_strip.jpg"
        vault_rel_strip = None
        if strip_path.is_file() and strip_path.stat().st_size > 1000:
            vault_rel_strip = f"takes/{sid}/{out_name}__{t_hash_prefix}_strip.jpg"
            safe_link_or_copy(strip_path, renders_dir / vault_rel_strip)

        r_info = scene_renders.get(out_name, {})
        created_ts = r_info.get("completed_at") or mp4_path.stat().st_mtime
        take_entry = {
            "take_id": take_id,
            "take_number": len(existing_takes) + 1,
            "scene_id": sid,
            "pass_num": pass_num,
            "out_name": out_name,
            "task_hash": t_hash,
            "seed": sc_info.get("seed"),
            "prompt": sc_info.get("prompt"),
            "anchor_id": sc_info.get("anchor"),
            "anchor_sha256": state_data.get("anchor_file_hashes", {}).get(sc_info.get("anchor", "")),
            "parent_p1_task_hash": None,
            "created_at": round(float(created_ts), 3),
            "created_iso": datetime.datetime.fromtimestamp(created_ts, datetime.timezone.utc).isoformat(),
            "vault_rel_mp4": vault_rel_mp4,
            "vault_rel_strip": vault_rel_strip,
            "file_path": f"work/renders_v3/{vault_rel_mp4}",
            "strip_path": f"work/renders_v3/{vault_rel_strip}" if vault_rel_strip else None,
            "render_start": r_info.get("render_start", float(sc_info.get("start", 0))),
            "render_end": r_info.get("render_end", float(sc_info.get("end", 0))),
            "frames": r_info.get("frames", 0),
            "elapsed_s": r_info.get("elapsed", 0.0),
            "size_bytes": mp4_path.stat().st_size,
            "resolution": parts[-1] if "x" in parts[-1] else "",
            "model_id": str(sc_info.get("renderer") or proj_data.get("models", {}).get("default_video_renderer", 1)),
            "model_type": settings_store.get(out_name, {}).get("model_type", ""),
            "scene_snapshot": copy.deepcopy(sc_info),
            "render_settings": settings_store.get(out_name, {}),
            "label": f"Take {len(existing_takes) + 1} (Baseline)",
            "is_baseline": True,
        }
        existing_takes.append(take_entry)
        active_map[pkey] = take_id
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
    if out_mp4.exists():
        return True
    manifest = load_takes_manifest(ctx.project_dir)
    sc_entry = manifest.get("scenes", {}).get(sid, {})
    pass_num = 1 if "_Pass1" in out_name else (2 if "_Pass2" in out_name else None)
    pkey = _pass_key(pass_num)
    active_id = sc_entry.get("active_takes", {}).get(pkey)
    if not active_id:
        return False
    for t in sc_entry.get("takes", []):
        if t.get("take_id") == active_id and t.get("out_name") == out_name:
            v_path = ctx.project_dir / "work" / "renders_v3" / t["vault_rel_mp4"]
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
    strip_src = renders_dir / f"{out_name}_strip.jpg"
    vault_rel_strip = None
    if strip_src.is_file():
        vault_rel_strip = f"takes/{sid}/{out_name}__{t_hash_prefix}_strip.jpg"
        safe_link_or_copy(strip_src, renders_dir / vault_rel_strip)

    manifest = load_takes_manifest(ctx.project_dir)
    sc_entry = manifest.setdefault("scenes", {}).setdefault(sid, {"active_takes": {}, "takes": []})
    existing = sc_entry.setdefault("takes", [])

    created_ts = time.time()
    take_entry = {
        "take_id": take_id,
        "take_number": len(existing) + 1,
        "scene_id": sid,
        "pass_num": pass_num,
        "out_name": out_name,
        "task_hash": t_hash,
        "seed": t_dict.get("seed", sc.get("seed")),
        "prompt": t_dict.get("prompt", sc.get("prompt")),
        "anchor_id": sc.get("anchor"),
        "anchor_sha256": state.get("anchor_file_hashes", {}).get(sc.get("anchor", "")),
        "parent_p1_task_hash": t_dict.get("parent_p1_task_hash") if pass_num == 2 else None,
        "created_at": round(created_ts, 3),
        "created_iso": datetime.datetime.fromtimestamp(created_ts, datetime.timezone.utc).isoformat(),
        "vault_rel_mp4": vault_rel_mp4,
        "vault_rel_strip": vault_rel_strip,
        "file_path": f"work/renders_v3/{vault_rel_mp4}",
        "strip_path": f"work/renders_v3/{vault_rel_strip}" if vault_rel_strip else None,
        "render_start": t_dict.get("render_start", float(sc.get("start", 0))),
        "render_end": t_dict.get("render_end", float(sc.get("end", 0))),
        "frames": t_dict.get("video_length", 0),
        "elapsed_s": round(float(elapsed), 1),
        "size_bytes": out_mp4.stat().st_size if out_mp4.exists() else 0,
        "resolution": str(t_dict.get("resolution", "")),
        "model_id": str(sc.get("renderer") or (getattr(ctx, "models", {}) or {}).get("default_video_renderer", 1)),
        "model_type": str(t_dict.get("model_type", "")),
        "scene_snapshot": copy.deepcopy(sc),
        "render_settings": {k: v for k, v in t_dict.items() if k not in ("audio_path", "image_start", "image_end")},
        "label": f"Take {len(existing) + 1}",
    }
    # Update or append
    idx = next((i for i, t in enumerate(existing) if t.get("take_id") == take_id and t.get("out_name") == out_name), None)
    if idx is not None:
        existing[idx] = take_entry
    else:
        existing.append(take_entry)
    sc_entry.setdefault("active_takes", {})[pkey] = take_id
    sc_entry["active_take_id"] = take_id
    save_takes_manifest(ctx.project_dir, manifest)
    return take_entry


def try_auto_promote_vault_hit(
    proj_dir: Path, *args, **kwargs
) -> bool:
    """Checks vault for an existing take matching hashes and promotes it automatically."""
    proj_dir = Path(proj_dir)
    manifest = load_takes_manifest(proj_dir)

    # Detect calling convention:
    # Convention A: (proj_dir, config_file, sid, pass_num, cur_task_hash)
    if args and (isinstance(args[0], (Path, str)) and (str(args[0]).endswith(".json") or Path(args[0]).is_file())):
        config_file = Path(args[0])
        sid = str(args[1]) if len(args) > 1 else kwargs.get("sid", "")
        pass_num = args[2] if len(args) > 2 else kwargs.get("pass_num")
        cur_task_hash = args[3] if len(args) > 3 else kwargs.get("cur_task_hash", "")
        valid_hashes = {cur_task_hash} if cur_task_hash else set()
        out_name = None
        sc_start = None
        sc_end = None
        cur_anchor_hash = None
    # Convention B: (proj_dir, sid, pass_num, out_name, curr_hashes, sc_start, sc_end, cur_anchor_hash, state, state_file)
    else:
        sid = str(args[0]) if len(args) > 0 else kwargs.get("sid", "")
        pass_num = args[1] if len(args) > 1 else kwargs.get("pass_num")
        out_name = str(args[2]) if len(args) > 2 else kwargs.get("out_name")
        curr_hashes = args[3] if len(args) > 3 else kwargs.get("curr_hashes", ())
        valid_hashes = set(curr_hashes) if isinstance(curr_hashes, (list, tuple, set)) else {str(curr_hashes)}
        sc_start = args[4] if len(args) > 4 else kwargs.get("sc_start")
        sc_end = args[5] if len(args) > 5 else kwargs.get("sc_end")
        cur_anchor_hash = args[6] if len(args) > 6 else kwargs.get("cur_anchor_hash")

    sc_entry = manifest.get("scenes", {}).get(sid, {})
    takes = sc_entry.get("takes", [])
    if not takes:
        return False

    renders_dir = proj_dir / "work" / "renders_v3"
    state_file = get_state_hashes_file(proj_dir)
    state = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    for t in reversed(takes):
        if out_name and t.get("out_name") != out_name:
            continue
        if valid_hashes and t.get("task_hash") not in valid_hashes:
            continue
        v_rel = t.get("vault_rel_mp4") or ""
        v_path = renders_dir / v_rel
        if not v_path.is_file() or v_path.stat().st_size < 50000:
            continue
        if sc_start is not None and sc_end is not None:
            r_start = float(t.get("render_start", 0))
            r_end = float(t.get("render_end", 0))
            if not ((r_start <= sc_start + 0.05) and (r_end >= sc_end - 0.05)):
                continue
        if cur_anchor_hash and t.get("anchor_sha256") and t.get("anchor_sha256") != cur_anchor_hash:
            continue

        # Found match! Promote to active
        target_out_name = t.get("out_name") or out_name or f"{sid}_solo"
        active_mp4 = renders_dir / f"{target_out_name}.mp4"
        safe_link_or_copy(v_path, active_mp4)

        # Restore strip
        v_strip = renders_dir / (t.get("vault_rel_strip") or "")
        active_strip = renders_dir / f"{target_out_name}_strip.jpg"
        if v_strip.is_file():
            safe_link_or_copy(v_strip, active_strip)

        safe_mtime = max(time.time(), v_path.stat().st_mtime + 2.0)
        aid = t.get("scene_snapshot", {}).get("anchor")
        if not aid and config_file and Path(config_file).is_file():
            try:
                p_cfg = json.loads(Path(config_file).read_text(encoding="utf-8"))
                sc_found = next((s for s in p_cfg.get("scenes", []) if s.get("id") == sid), None)
                if sc_found:
                    aid = sc_found.get("anchor")
            except Exception:
                pass
        if aid:
            anchors_dir = proj_dir / "work" / "anchors_v3"
            for f in (anchors_dir / f"{aid}.png", *anchors_dir.glob(f"{aid}*")):
                if f.is_file():
                    safe_mtime = max(safe_mtime, f.stat().st_mtime + 2.0)
                    break
        if pass_num == 2 and "_Pass2" in target_out_name:
            p1_name = target_out_name.replace("_Pass2_Final", "_Pass1_Left")
            p1_mp4 = renders_dir / f"{p1_name}.mp4"
            if p1_mp4.is_file():
                safe_mtime = max(safe_mtime, p1_mp4.stat().st_mtime + 2.0)
        try:
            os.utime(str(active_mp4), (safe_mtime, safe_mtime))
        except OSError:
            pass

        state.setdefault("task_hashes", {})[target_out_name] = t["task_hash"]
        state.setdefault("tasks", {})[target_out_name] = t["task_hash"]
        if t.get("render_settings"):
            state.setdefault("settings_store", {})[target_out_name] = t["render_settings"]
        state.setdefault("scene_renders", {})[target_out_name] = {
            "render_start": t.get("render_start"),
            "render_end": t.get("render_end"),
            "frames": t.get("frames"),
            "elapsed": t.get("elapsed_s"),
            "completed_at": t.get("created_at"),
        }
        save_state_hashes_data(proj_dir, state)

        pkey = _pass_key(pass_num)
        sc_entry.setdefault("active_takes", {})[pkey] = t["take_id"]
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
    target_take = None
    if pass_num is not None:
        target_take = next((t for t in takes if t.get("take_id") == take_id and t.get("pass_num") == pass_num), None)
    if not target_take:
        target_take = next((t for t in takes if t.get("take_id") == take_id), None)
    if not target_take:
        raise ValueError(f"Take {take_id} not found for scene {sid}")

    renders_dir = proj_dir / "work" / "renders_v3"
    out_name = target_take["out_name"]
    vault_mp4 = renders_dir / target_take["vault_rel_mp4"]
    active_mp4 = renders_dir / f"{out_name}.mp4"

    if not vault_mp4.is_file():
        raise FileNotFoundError(f"Vault video file missing: {vault_mp4}")

    # 1. Update project.json scene settings
    proj_data = json.loads(config_file.read_text(encoding="utf-8"))
    scenes = proj_data.get("scenes", [])
    target_sc = next((s for s in scenes if s.get("id") == sid), None)
    if not target_sc: raise ValueError(f"Scene {sid} not found in project")

    snap = target_take.get("scene_snapshot", {})
    if pass_num == 1:
        for k in ("prompt_m", "seed_m", "stem_m", "anchor", "anchor_end", "split_x", "render_pad_start", "render_pad_end"):
            if k in snap and snap[k] is not None: target_sc[k] = copy.deepcopy(snap[k])
            elif k in ("anchor_end", "render_pad_start", "render_pad_end"): target_sc.pop(k, None)
    elif pass_num == 2:
        for k in ("prompt_f", "seed_f", "stem_f", "renderer"):
            if k in snap and snap[k] is not None: target_sc[k] = copy.deepcopy(snap[k])
    else:
        for k, v in snap.items():
            if (k not in ("id", "start", "end") or restore_timing) and v is not None:
                target_sc[k] = copy.deepcopy(v)
        for pk in ("anchor_end", "render_pad_start", "render_pad_end", "pad_start", "pad_end", "render_overlap"):
            if pk not in snap or snap[pk] is None: target_sc.pop(pk, None)

    if restore_timing and "start" in snap and "end" in snap:
        target_sc["start"], target_sc["end"] = snap["start"], snap["end"]

    config_file.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")

    # 2. Hardlink / copy vault video to active MP4
    safe_link_or_copy(vault_mp4, active_mp4)

    # 3. Strip thumbnail
    active_strip = renders_dir / f"{out_name}_strip.jpg"
    vault_strip = renders_dir / (target_take.get("vault_rel_strip") or "")
    if vault_strip.is_file():
        safe_link_or_copy(vault_strip, active_strip)
    # Clean stale dynamic thumbnail caches
    for stale in renders_dir.glob(f"{out_name}_strip_*f.jpg"):
        stale.unlink(missing_ok=True)

    # 4. Harmonize mtime above anchor(s) & Pass 1
    safe_mtime = max(time.time(), vault_mp4.stat().st_mtime + 2.0)
    for a_ref in (target_sc.get("anchor"), target_sc.get("anchor_end")):
        if a_ref:
            for f in (proj_dir / "work" / "anchors_v3").glob(f"{extract_anchor_id(a_ref)}*"):
                if f.is_file():
                    safe_mtime = max(safe_mtime, f.stat().st_mtime + 2.0)
                    break
    if pass_num == 2 and "_Pass2" in out_name:
        p1_name = out_name.replace("_Pass2_Final", "_Pass1_Left")
        p1_mp4 = renders_dir / f"{p1_name}.mp4"
        if p1_mp4.exists():
            safe_mtime = max(safe_mtime, p1_mp4.stat().st_mtime + 2.0)

    try:
        os.utime(str(active_mp4), (safe_mtime, safe_mtime))
    except OSError:
        pass

    # 5. Update state_hashes.json
    state_file = get_state_hashes_file(proj_dir)
    state_data = {}
    if state_file.is_file():
        try: state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception: pass

    state_data.setdefault("task_hashes", {})[out_name] = target_take["task_hash"]
    state_data.setdefault("tasks", {})[out_name] = target_take["task_hash"]
    if target_take.get("render_settings"):
        state_data.setdefault("settings_store", {})[out_name] = target_take["render_settings"]
    state_data.setdefault("scene_renders", {})[out_name] = {
        "render_start": target_take.get("render_start"),
        "render_end": target_take.get("render_end"),
        "frames": target_take.get("frames"),
        "elapsed": target_take.get("elapsed_s"),
        "completed_at": target_take.get("created_at"),
    }
    save_state_hashes_data(proj_dir, state_data)

    # 6. Update active take in manifest
    pkey = _pass_key(pass_num)
    sc_entry.setdefault("active_takes", {})[pkey] = take_id
    sc_entry["active_take_id"] = take_id
    save_takes_manifest(proj_dir, manifest)

    return {
        "success": True,
        "active_take_id": take_id,
        "restored_params": copy.deepcopy(target_sc),
        "scene": target_sc,
        "take": target_take,
    }


def create_project_checkpoint(
    proj_dir: Path, config_file: Path, name: str = "",
    trigger: str = "manual", note: str = "", comment: str = ""
) -> dict:
    """Saves project.json, state_hashes.json, and active_takes.json in checkpoints/<ckpt_id>/."""
    proj_dir = Path(proj_dir)
    config_file = Path(config_file)
    ckpts_dir = proj_dir / "checkpoints"
    ckpts_dir.mkdir(parents=True, exist_ok=True)
    index_file = ckpts_dir / "index.json"

    index_data = {"version": 1, "checkpoints": []}
    if index_file.is_file():
        try: index_data = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception: pass

    # Deduplication check for auto pre_render checkpoints
    proj_bytes = config_file.read_bytes() if config_file.is_file() else b""
    proj_hash = hashlib.sha256(proj_bytes).hexdigest()[:16]

    ckpts = index_data.get("checkpoints", [])
    if trigger == "pre_render" and ckpts:
        last_ckpt = ckpts[0]
        if last_ckpt.get("trigger") == "pre_render" and last_ckpt.get("proj_hash") == proj_hash:
            return last_ckpt

    if comment and not note:
        note = comment

    ts = time.time()
    ts_str = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    ckpt_id = f"ckpt_{ts_str}_{trigger}"
    ckpt_dir = ckpts_dir / ckpt_id
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    # 1. Snapshot project.json
    (ckpt_dir / "project.json").write_bytes(proj_bytes)

    # 2. Snapshot state_hashes.json
    state_file = get_state_hashes_file(proj_dir)
    if state_file.is_file():
        shutil.copy2(str(state_file), str(ckpt_dir / "state_hashes.json"))

    # 3. Snapshot active takes map
    manifest = load_takes_manifest(proj_dir)
    active_map = {}
    for sid, sdata in manifest.get("scenes", {}).items():
        active_map[sid] = sdata.get("active_takes", {})
    (ckpt_dir / "active_takes.json").write_text(json.dumps(active_map, indent=2), encoding="utf-8")

    ckpt_meta = {
        "id": ckpt_id,
        "name": name or f"Checkpoint {ts_str}",
        "trigger": trigger,
        "note": note,
        "comment": note,
        "timestamp": round(ts, 3),
        "created_at": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(),
        "created_iso": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(),
        "proj_hash": proj_hash,
    }
    ckpts.insert(0, ckpt_meta)

    # Prune auto checkpoints beyond 30
    auto_ckpts = [c for c in ckpts if c.get("trigger") == "pre_render"]
    if len(auto_ckpts) > 30:
        for c_to_del in auto_ckpts[30:]:
            ckpts.remove(c_to_del)
            shutil.rmtree(ckpts_dir / c_to_del["id"], ignore_errors=True)

    index_data["checkpoints"] = ckpts
    index_file.write_text(json.dumps(index_data, indent=2), encoding="utf-8")
    return ckpt_meta


def list_project_checkpoints(proj_dir: Path) -> list:
    ckpts_dir = Path(proj_dir) / "checkpoints"
    index_file = ckpts_dir / "index.json"
    if index_file.is_file():
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            return data.get("checkpoints", [])
        except Exception:
            pass
    return []


def rollback_project_checkpoint(
    proj_dir: Path, config_file: Path | str | None = None, checkpoint_id: str = ""
) -> dict:
    """Restores project.json, state_hashes.json, and active take hardlinks to checkpoint state."""
    proj_dir = Path(proj_dir)
    if isinstance(config_file, str) and not checkpoint_id and ("ckpt_" in config_file or Path(config_file).name.startswith("ckpt_")):
        checkpoint_id = config_file
        config_file = None
    if config_file is None:
        jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
        if not jsons:
            raise FileNotFoundError(f"Project config JSON not found in {proj_dir}")
        config_file = jsons[-1]
    config_file = Path(config_file)
    ckpt_dir = proj_dir / "checkpoints" / checkpoint_id
    if not ckpt_dir.is_dir():
        raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found at {ckpt_dir}")

    # Safety checkpoint of current state before rollback
    create_project_checkpoint(
        proj_dir, config_file,
        name=f"Auto: Before rollback to {checkpoint_id}",
        trigger="pre_rollback"
    )

    # Restore project.json
    ckpt_proj = ckpt_dir / "project.json"
    if ckpt_proj.is_file():
        shutil.copy2(str(ckpt_proj), str(config_file))

    # Restore state_hashes.json
    ckpt_state = ckpt_dir / "state_hashes.json"
    if ckpt_state.is_file():
        try:
            state_data = json.loads(ckpt_state.read_text(encoding="utf-8"))
            save_state_hashes_data(proj_dir, state_data)
        except Exception:
            for s_target in (proj_dir / "work" / "state_hashes.json", proj_dir / "work" / "jobs_v3" / "state_hashes.json"):
                if s_target.parent.is_dir():
                    shutil.copy2(str(ckpt_state), str(s_target))

    # Restore active takes
    active_map_file = ckpt_dir / "active_takes.json"
    restored_count = 0
    if active_map_file.is_file():
        try:
            active_map = json.loads(active_map_file.read_text(encoding="utf-8"))
            manifest = load_takes_manifest(proj_dir)
            renders_dir = proj_dir / "work" / "renders_v3"
            for sid, pmap in active_map.items():
                sc_entry = manifest.get("scenes", {}).get(sid, {})
                takes = sc_entry.get("takes", [])
                for pkey, tid in pmap.items():
                    t_obj = next((t for t in takes if t.get("take_id") == tid), None)
                    if t_obj:
                        v_mp4 = renders_dir / t_obj["vault_rel_mp4"]
                        a_mp4 = renders_dir / f"{t_obj['out_name']}.mp4"
                        if v_mp4.is_file():
                            safe_link_or_copy(v_mp4, a_mp4)
                            restored_count += 1
                        v_strip = renders_dir / (t_obj.get("vault_rel_strip") or "")
                        a_strip = renders_dir / f"{t_obj['out_name']}_strip.jpg"
                        if v_strip.is_file():
                            safe_link_or_copy(v_strip, a_strip)
                        sc_entry.setdefault("active_takes", {})[pkey] = tid
                        sc_entry["active_take_id"] = tid
            save_takes_manifest(proj_dir, manifest)
        except Exception:
            pass

    return {
        "success": True,
        "restored_checkpoint_id": checkpoint_id,
        "restored_takes_count": restored_count,
    }


def get_vault_stats(proj_dir: Path) -> dict:
    proj_dir = Path(proj_dir)
    renders_dir = proj_dir / "work" / "renders_v3"
    vault_dir = renders_dir / "takes"
    total_bytes = 0
    take_files = 0
    if vault_dir.is_dir():
        for f in vault_dir.rglob("*.mp4"):
            if f.is_file():
                total_bytes += f.stat().st_size
                take_files += 1
    manifest = load_takes_manifest(proj_dir)
    total_scenes = len(manifest.get("scenes", {}))
    return {
        "total_scenes": total_scenes,
        "total_takes": take_files,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
    }


def handle_takes_api(handler, action: str, path: str = "", body: dict | None = None, slug: str = ""):
    """REST API dispatcher for scene takes and project checkpoints."""
    from http import HTTPStatus
    srv = sys.modules.get("work.ui.server") or sys.modules.get("server")
    projects_dir = getattr(srv, "PROJECTS_DIR", None) if srv else None
    if not projects_dir:
        projects_dir = Path("projects")

    def _resolve_proj(p_slug: str):
        pdir = projects_dir / p_slug
        if not pdir.is_dir():
            return None, None
        jsons = [j for j in sorted(pdir.glob("*.json")) if not j.name.startswith("state_hashes")]
        cfile = jsons[-1] if jsons else None
        return pdir, cfile

    if action == "get_takes":
        parts = path[len("/api/takes/"):].strip("/").split("/")
        if len(parts) < 2:
            handler.send_error_json("Invalid takes path", status=HTTPStatus.BAD_REQUEST)
            return
        p_slug, sid = parts[0], parts[1]
        pdir, _ = _resolve_proj(p_slug)
        if not pdir:
            handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        manifest = load_takes_manifest(pdir)
        sc_data = manifest.get("scenes", {}).get(sid, {})
        handler.send_json({
            "success": True,
            "scene_id": sid,
            "active_take_id": sc_data.get("active_take_id"),
            "active_takes": sc_data.get("active_takes", {}),
            "takes": sc_data.get("takes", []),
        })
        return

    if action == "get_checkpoints":
        p_slug = path[len("/api/checkpoints/"):].strip("/")
        pdir, _ = _resolve_proj(p_slug)
        if not pdir:
            handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        cps = list_project_checkpoints(pdir)
        stats = get_vault_stats(pdir)
        handler.send_json({
            "success": True,
            "checkpoints": cps,
            "vault_stats": stats,
        })
        return

    if action == "get_vault_stats":
        p_slug = path[len("/api/vault/stats/"):].strip("/")
        pdir, _ = _resolve_proj(p_slug)
        if not pdir:
            handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        stats = get_vault_stats(pdir)
        handler.send_json({"success": True, **stats})
        return

    if action == "activate":
        body = body or {}
        p_slug = body.get("project") or body.get("slug")
        sid = body.get("scene_id")
        tid = body.get("take_id")
        if not p_slug or not sid or not tid:
            handler.send_error_json("project, scene_id, and take_id are required")
            return
        pdir, cfile = _resolve_proj(p_slug)
        if not pdir or not cfile:
            handler.send_error_json("Project or config not found", status=HTTPStatus.NOT_FOUND)
            return
        try:
            res = promote_take_to_active(pdir, cfile, sid, tid)
            if srv and hasattr(srv, "compute_fast_project_queue"):
                srv.compute_fast_project_queue(p_slug, force_recheck=True)
            handler.send_json({"success": True, **res})
        except Exception as e:
            handler.send_error_json(str(e))
        return

    if action == "create":
        body = body or {}
        p_slug = body.get("project") or body.get("slug")
        name = body.get("name", "").strip() or "Manual Checkpoint"
        comment = body.get("comment", "").strip()
        if not p_slug:
            handler.send_error_json("project is required"); return
        pdir, cfile = _resolve_proj(p_slug)
        if not pdir or not cfile:
            handler.send_error_json("Project or config not found", status=HTTPStatus.NOT_FOUND); return
        try:
            cp = create_project_checkpoint(pdir, cfile, name=name, trigger="manual", comment=comment)
            handler.send_json({"success": True, "checkpoint": cp})
        except Exception as e:
            handler.send_error_json(str(e))
        return

    if action == "rollback":
        body = body or {}
        p_slug = body.get("project") or body.get("slug")
        cid = body.get("checkpoint_id")
        if not p_slug or not cid:
            handler.send_error_json("project and checkpoint_id are required"); return
        pdir, _ = _resolve_proj(p_slug)
        if not pdir:
            handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND); return
        try:
            res = rollback_project_checkpoint(pdir, cid)
            if srv and hasattr(srv, "compute_fast_project_queue"):
                srv.compute_fast_project_queue(p_slug, force_recheck=True)
            handler.send_json({"success": True, **res})
        except Exception as e:
            handler.send_error_json(str(e))
        return

    handler.send_error_json(f"Unknown takes API action: {action}")
