"""Project directory & work artifact path resolution helpers.

Standardizes all Pipeline stages, Take Vault, and Studio UI modules on the
canonical project directories configured in `<project>.json` under
`project.directories`, defaulting to non-`_v3` folders:
`work/anchors`, `work/audio`, `work/masks`, `work/jobs`, `work/renders`,
`work/trimmed`, and `work/frames`.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any


_DIR_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("anchors", "anchors", "work/anchors"),
    ("audio_slices", "audio_slices", "work/audio"),
    ("masks", "masks", "work/masks"),
    ("jobs", "jobs", "work/jobs"),
    ("renders", "renders", "work/renders"),
    ("trimmed", "trimmed", "work/trimmed"),
    ("strips", "review_strips", "work/frames"),
)


def find_project_config_json(proj_dir: Path, config_name: str | None = None) -> Path | None:
    """Finds the active project config JSON inside `proj_dir` (ignoring state/local files)."""
    pdir = Path(proj_dir)
    if not pdir.is_dir():
        return None
    if config_name:
        cand = pdir / str(config_name).strip()
        if cand.is_file():
            return cand
    jsons = [
        j for j in sorted(pdir.glob("*.json"))
        if not j.name.startswith(("state_hashes", "local_config"))
    ]
    return jsons[-1] if jsons else None


def read_project_json_safe(cfg_path: Path | None) -> dict[str, Any]:
    """Safely parses a project JSON file, returning {} on missing/corrupt input."""
    if not cfg_path or not Path(cfg_path).is_file():
        return {}
    try:
        data = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def path_rel_str(proj_dir: Path, target_dir: Path, default_rel: str) -> str:
    """Returns forward-slash path relative to `proj_dir`, or `default_rel` if outside."""
    try:
        rel = Path(target_dir).resolve().relative_to(Path(proj_dir).resolve())
        return rel.as_posix() or default_rel
    except Exception:
        return default_rel


def resolve_project_dirs(
    proj_dir: Path,
    config_data: dict[str, Any] | None = None,
    config_file: Path | None = None,
) -> dict[str, Any]:
    """Resolves configured working directories and relative prefixes for a project."""
    pdir = Path(proj_dir).resolve()
    resolved_cfg_file = Path(config_file) if config_file else find_project_config_json(pdir)
    data = config_data if isinstance(config_data, dict) else read_project_json_safe(resolved_cfg_file)
    dirs_cfg = (data.get("project") or {}).get("directories") or {}

    out: dict[str, Any] = {"config_file": resolved_cfg_file}
    for out_key, cfg_key, std_rel in _DIR_DEFAULTS:
        raw_val = str(dirs_cfg.get(cfg_key) or "").strip() if isinstance(dirs_cfg, dict) else ""
        resolved = (pdir / (raw_val or std_rel)).resolve()
        out[out_key] = resolved
        out[f"{out_key}_rel"] = path_rel_str(pdir, resolved, std_rel)
    return out


def resolve_queue_file(
    proj_dir: Path, *, for_write: bool = False, config_data: dict | None = None
) -> Path:
    q_file = resolve_project_dirs(proj_dir, config_data=config_data)["jobs"] / "v3_queue.json"
    if for_write:
        q_file.parent.mkdir(parents=True, exist_ok=True)
    return q_file


def resolve_done_history_file(
    proj_dir: Path, *, for_write: bool = False, config_data: dict | None = None
) -> Path:
    h_file = resolve_project_dirs(proj_dir, config_data=config_data)["jobs"] / "done_history.json"
    if for_write:
        h_file.parent.mkdir(parents=True, exist_ok=True)
    return h_file


def resolve_state_hashes_paths(proj_dir: Path, config_data: dict | None = None) -> list[Path]:
    pdir = Path(proj_dir).resolve()
    j_hash = resolve_project_dirs(pdir, config_data=config_data)["jobs"] / "state_hashes.json"
    legacy_hash = (pdir / "work" / "state_hashes.json").resolve()
    return [j_hash] if j_hash == legacy_hash else [j_hash, legacy_hash]


def resolve_vault_media_path(
    proj_dir: Path,
    renders_dir: Path | None,
    take_obj: dict | None,
    *,
    path_key: str = "file_path",
    rel_key: str = "vault_rel_mp4",
) -> Path | None:
    """Resolves a take's MP4 or strip path inside the project's renders vault."""
    if not isinstance(take_obj, dict):
        return None
    pdir = Path(proj_dir)
    path_val = str(take_obj.get(path_key) or "").strip()
    if path_val:
        cand = pdir / path_val
        if cand.is_file():
            return cand
    rel_val = str(take_obj.get(rel_key) or "").strip()
    if rel_val:
        rdir = Path(renders_dir) if renders_dir else resolve_project_dirs(pdir)["renders"]
        cand = rdir / rel_val
        if cand.is_file():
            return cand
    return None


AUDIO_EXTS: tuple[str, ...] = (".flac", ".wav", ".mp3", ".ogg", ".m4a", ".aac")


def resolve_master_audio_path(project_dir: Path, raw_master_audio: str | None) -> Path:
    """Resolves `project.master_audio`, falling back cleanly across audio extensions (e.g. .mp3 -> .flac)."""
    pdir = Path(project_dir)
    raw = str(raw_master_audio or "").strip() or "audio/master_audio.mp3"
    p = Path(raw)
    cand = (p if p.is_absolute() else (pdir / p)).resolve()
    if cand.is_file():
        return cand
    for ext in AUDIO_EXTS:
        alt = cand.with_suffix(ext)
        if alt.is_file():
            return alt
    audio_dir = pdir / "audio"
    for stem in ("master_audio", "master"):
        for ext in AUDIO_EXTS:
            alt = audio_dir / f"{stem}{ext}"
            if alt.is_file():
                return alt
    return cand


def scan_project_audio_assets(proj_dir: Path, config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scans on-disk audio files and verifies which configured stems actually exist."""
    pdir = Path(proj_dir).resolve()
    proj_cfg = (config_data or {}).get("project") or {}
    audio_dir = (pdir / "audio").resolve()
    audio_files = (
        sorted(
            (a.name for a in audio_dir.iterdir() if a.is_file() and a.suffix.lower() in AUDIO_EXTS),
            key=str.lower,
        )
        if audio_dir.is_dir()
        else []
    )
    disk_by_lower = {fn.lower(): fn for fn in audio_files}

    master_path = resolve_master_audio_path(pdir, proj_cfg.get("master_audio"))
    master_exists = master_path.is_file()
    master_audio_file = (
        disk_by_lower.get(master_path.name.lower(), master_path.name)
        if master_exists and master_path.resolve().parent == audio_dir
        else (master_path.name if master_exists else "")
    )

    audio_stems: list[str] = []
    stem_files: dict[str, str] = {}
    missing_stems: list[str] = []
    raw_stems = proj_cfg.get("stems") if isinstance(proj_cfg.get("stems"), dict) else {}
    for key, raw_path in raw_stems.items():
        raw_str = str(raw_path or "").strip()
        cand = (Path(raw_str) if Path(raw_str).is_absolute() else (pdir / raw_str)).resolve() if raw_str else None
        resolved_name = ""
        if cand and cand.is_file():
            resolved_name = disk_by_lower.get(cand.name.lower(), cand.name) if cand.parent == audio_dir else cand.name
        elif raw_str and Path(raw_str).name.lower() in disk_by_lower:
            resolved_name = disk_by_lower[Path(raw_str).name.lower()]
        elif str(key).lower() in disk_by_lower:
            resolved_name = disk_by_lower[str(key).lower()]
        if resolved_name:
            audio_stems.append(key)
            stem_files[key] = resolved_name
        else:
            missing_stems.append(key)

    return {
        "audio_files": audio_files,
        "master_audio_file": master_audio_file,
        "master_audio_exists": master_exists,
        "audio_stems": audio_stems,
        "stem_files": stem_files,
        "missing_stems": missing_stems,
    }


def resolve_take_target_renderer(
    proj_data: dict[str, Any],
    sc: dict[str, Any],
    target_take: dict[str, Any],
    pass_num: int | None = None,
) -> tuple[str | int | None, str | None]:
    """Returns (renderer_id, backend) to write onto sc if effective model_type or backend differs from target_take."""
    t_rs = target_take.get("render_settings") or {}
    take_model = str(target_take.get("model_type") or t_rs.get("model_type") or "").strip()
    take_backend = str(target_take.get("backend") or "").strip().lower() or None
    if not take_model and not take_backend:
        return None, None
    rm = proj_data.get("models") or {}
    vr_map: dict[str, dict[str, Any]] = {}
    vr_sec: dict[str, str] = {}
    mp = Path(__file__).resolve().parent.parent.parent / "model_profiles.json"
    srcs = []
    if mp.is_file():
        try: srcs.append(json.loads(mp.read_text(encoding="utf-8")))
        except Exception: pass
    srcs.append(rm)
    for src in srcs:
        for sec in ("video_renderers", "fal_video_renderers", "minimax_video_renderers"):
            for k, v in (src.get(sec) or {}).items():
                if isinstance(v, dict):
                    ks = str(k)
                    vr_map[ks] = v
                    vr_sec[ks] = sec

    p = target_take.get("pass_num") if pass_num is None else pass_num
    ovr = sc.get("renderer_m") if p == 1 else (sc.get("renderer_f") if p == 2 else None)
    if ovr is None:
        ovr = sc.get("renderer") if sc.get("renderer") is not None else sc.get("video_renderer")
    def_id = rm.get("default_video_renderer") or rm.get("video_renderer") or 1
    rid = str(def_id) if (ovr is None or str(ovr).strip().lower() in ("", "0", "none", "null", "default")) else str(ovr).strip()
    cur_model = str((vr_map.get(rid) or {}).get("model_type") or "").strip()

    t_snap = target_take.get("scene_snapshot") or {}
    snap_ovr = t_snap.get("renderer_m" if p == 1 else ("renderer_f" if p == 2 else "renderer")) or t_snap.get("renderer") or t_snap.get("video_renderer")
    snap_rid = str(snap_ovr).strip() if snap_ovr not in (None, "", "0", 0, "none", "null", "default") else ""
    mid = str(target_take.get("model_id") or "").strip()

    ren_fix: str | int | None = None
    wrong_cloud_profile = bool(
        take_model and cur_model == take_model and vr_sec.get(rid) not in (None, "video_renderers")
        and mid in vr_map and vr_sec.get(mid) == "video_renderers" and snap_rid != rid
    )
    if take_model and (cur_model != take_model or wrong_cloud_profile):
        def _compat(k: str, v: dict[str, Any]) -> bool:
            if str(v.get("model_type") or "").strip() != take_model:
                return False
            if not take_backend:
                return True
            if vr_sec.get(k) == "video_renderers" and not v.get("cloud_only"):
                return True
            return str(v.get("backend") or ("local" if not v.get("cloud_only") else "")).strip().lower() in ("", take_backend)

        cands = [k for k, v in vr_map.items() if _compat(k, v)]
        if not cands:
            cands = [k for k, v in vr_map.items() if str(v.get("model_type") or "").strip() == take_model]
        if cands:
            if snap_rid in cands:
                pick = snap_rid
            elif mid in cands:
                pick = mid
            elif take_backend == "local" or t_rs.get("flow_shift") is not None or t_rs.get("skip_steps_cache_type"):
                local_cands = [k for k in cands if vr_sec.get(k) == "video_renderers"]
                pick = local_cands[0] if local_cands else cands[0]
            else:
                pick = cands[0]
            ren_fix = int(pick) if pick.isdigit() else pick
    return ren_fix, take_backend


def find_companion_pass1_take(
    proj_dir: Path,
    renders_dir: Path,
    takes: list[dict[str, Any]],
    target_take: dict[str, Any],
    active_p1_id: str | None,
) -> dict[str, Any] | None:
    """Finds a matching Pass 1 take when restoring a Pass 2 take whose active Pass 1 is stale or incompatible."""
    if not isinstance(target_take, dict) or target_take.get("pass_num") != 2:
        return None
    target_ts = float(target_take.get("created_at") or 0.0)
    target_res = str(target_take.get("resolution") or "").strip()
    parent_h = str(target_take.get("parent_p1_task_hash") or "").strip()
    t_snap = target_take.get("scene_snapshot") or {}

    def _valid_p1(tk: dict[str, Any] | None) -> bool:
        if not isinstance(tk, dict) or tk.get("pass_num") != 1:
            return False
        vp = resolve_vault_media_path(proj_dir, renders_dir, tk)
        if not vp or not vp.is_file() or vp.stat().st_size < 50000:
            return False
        tk_res = str(tk.get("resolution") or "").strip()
        return not (target_res and tk_res and tk_res != target_res)

    act_p1 = next((t for t in takes if t.get("take_id") == active_p1_id), None) if active_p1_id else None
    if act_p1 and _valid_p1(act_p1):
        p1_ts = float(act_p1.get("created_at") or 0.0)
        parent_ok = (not parent_h) or (act_p1.get("task_hash") == parent_h)
        time_ok = (not target_ts) or (not p1_ts) or (p1_ts <= target_ts + 1.0)
        indep_p2 = bool(t_snap.get("renderer_f"))
        model_ok = indep_p2 or not act_p1.get("model_type") or not target_take.get("model_type") or act_p1.get("model_type") == target_take.get("model_type")
        if parent_ok and time_ok and model_ok:
            return None

    cands = [t for t in reversed(takes) if t.get("take_id") != active_p1_id and _valid_p1(t)]
    if parent_h:
        exact = next((t for t in cands if t.get("task_hash") == parent_h), None)
        if exact:
            return exact

    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int, float] | None = None
    for t in cands:
        t_ts = float(t.get("created_at") or 0.0)
        if target_ts and t_ts and t_ts > target_ts + 1.0:
            continue
        t_snap_c = t.get("scene_snapshot") or {}
        score = (
            1 if (t.get("model_type") and t.get("model_type") == target_take.get("model_type")) else 0,
            1 if (t.get("anchor_id") and t.get("anchor_id") == target_take.get("anchor_id")) else 0,
            1 if (t_snap_c.get("seed_m") is not None and t_snap_c.get("seed_m") == t_snap.get("seed_m")) else 0,
            t_ts,
        )
        if best is None or score > best_score:
            best, best_score = t, score
    return best


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
    if not take_res or cur_res == take_res:
        return None
    for k, v in res_map.items():
        if v == take_res:
            return int(k) if k.isdigit() else k
    return take_res


def _apply_pass_sync_keys(target_sc: dict, snap: dict, p_num: int) -> None:
    sync_keys = _P1_SYNC_KEYS if p_num == 1 else _P2_SYNC_KEYS
    keep_req = ("prompt_m", "seed_m", "stem_m", "anchor", "split_x", "mode") if p_num == 1 else ("prompt_f", "seed_f", "stem_f", "split_x", "mode")
    for k in sync_keys:
        if k in snap and snap[k] is not None:
            target_sc[k] = copy.deepcopy(snap[k])
        elif k not in keep_req:
            target_sc.pop(k, None)


def _apply_take_renderer_and_backend(proj_data: dict, target_sc: dict, take_obj: dict, eff_p: int | None) -> None:
    ren_fix, bk_fix = resolve_take_target_renderer(proj_data, target_sc, take_obj, pass_num=eff_p)
    rk = "renderer_m" if eff_p == 1 else ("renderer_f" if eff_p == 2 else "renderer")
    if ren_fix is not None:
        target_sc[rk] = ren_fix
    bk = "backend_m" if eff_p == 1 else ("backend_f" if eff_p == 2 else "backend")
    inherited_bk = str(target_sc.get("backend") or (proj_data.get("models") or {}).get("default_backend") or "local").strip().lower()
    if bk_fix in ("fal", "minimax") or (bk_fix == "local" and (target_sc.get(bk) in ("fal", "minimax") or (eff_p in (1, 2) and inherited_bk in ("fal", "minimax")))):
        target_sc[bk] = bk_fix


def sync_restored_scene_fields(
    proj_data: dict,
    target_sc: dict,
    sid: str,
    target_take: dict,
    pass_num: int | None,
    restore_timing: bool,
    comp_p1: dict | None = None,
    p1_backend_source: dict | None = None,
) -> None:
    """Applies snapshot, resolution, renderer, and backend fields onto target_sc when activating a take."""
    snap = target_take.get("scene_snapshot", {})
    was_locked = target_sc.get("locked")
    eff_pass = target_take.get("pass_num") if pass_num is None else pass_num
    if pass_num in (1, 2):
        if pass_num == 2 and comp_p1 and isinstance(comp_p1.get("scene_snapshot"), dict):
            _apply_pass_sync_keys(target_sc, comp_p1["scene_snapshot"], 1)
        _apply_pass_sync_keys(target_sc, snap, pass_num)
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
    p1_src = comp_p1 or (p1_backend_source if (eff_pass == 2 and pass_num is None) else None)
    if p1_src:
        _apply_take_renderer_and_backend(proj_data, target_sc, p1_src, 1)
    _apply_take_renderer_and_backend(proj_data, target_sc, target_take, eff_pass)
    if was_locked is not None:
        target_sc["locked"] = was_locked
    if restore_timing and "start" in snap and "end" in snap:
        target_sc["start"], target_sc["end"] = snap["start"], snap["end"]


def resolve_manifest_active_take_clip(
    proj_dir: Path, sid: str, renders_dir: Path | None = None
) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolves a scene's active take MP4 and metadata directly from takes_manifest.json."""
    pdir = Path(proj_dir)
    mf_path = pdir / "work" / "takes_manifest.json"
    if not mf_path.is_file():
        return None, None
    try:
        m_data = json.loads(mf_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    scenes = m_data.get("scenes") if isinstance(m_data, dict) else None
    if not isinstance(scenes, dict):
        return None, None
    sd = next((v for k, v in scenes.items() if str(k).upper() == str(sid).upper()), None)
    if not isinstance(sd, dict):
        return None, None
    takes = sd.get("takes") or []
    aid = sd.get("active_take_id") or (sd.get("active_takes") or {}).get("p2") or (sd.get("active_takes") or {}).get("default")
    t_obj = next((t for t in takes if isinstance(t, dict) and t.get("take_id") == aid), None)
    if not t_obj:
        return None, None
    rdir = Path(renders_dir) if renders_dir else resolve_project_dirs(pdir)["renders"]
    vpath = resolve_vault_media_path(pdir, rdir, t_obj)
    if vpath and vpath.is_file():
        return vpath, t_obj
    out_n = str(t_obj.get("out_name") or "").strip()
    if out_n and (rdir / f"{out_n}.mp4").is_file():
        return rdir / f"{out_n}.mp4", t_obj
    return None, t_obj


def restore_active_files_after_vault(
    proj_dir: Path,
    renders_dir: Path,
    sid: str,
    prev_active: dict[str, Any],
    sc_entry: dict[str, Any],
    existing_takes: list[dict[str, Any]],
    created_ts: float,
    sc: dict[str, Any] | None,
    extract_aid_fn,
    safe_link_fn,
    adjust_mtime_fn,
    active_strip_fn,
) -> None:
    """Keeps active clip, strip, and mtime aligned to `prev_active` after vaulting a new non-active take."""
    prev_vpath = resolve_vault_media_path(proj_dir, renders_dir, prev_active)
    if not prev_vpath or not prev_vpath.is_file():
        return
    prev_out_name = str(prev_active.get("out_name") or "").strip()
    if prev_out_name:
        prev_out_mp4 = renders_dir / f"{prev_out_name}.mp4"
        safe_link_fn(prev_vpath, prev_out_mp4)
        anchor_floor = 0.0
        pdirs = resolve_project_dirs(proj_dir)
        anc_dir = pdirs.get("anchors")
        if isinstance(sc, dict) and isinstance(anc_dir, Path) and anc_dir.is_dir():
            for a_ref in (sc.get("anchor"), sc.get("anchor_end")):
                if a_ref:
                    aid = extract_aid_fn(a_ref)
                    for f in anc_dir.glob(f"{aid}*"):
                        if f.is_file():
                            anchor_floor = max(anchor_floor, f.stat().st_mtime + 2.0)
                            break
        safe_mt = max(float(created_ts or 0.0), prev_vpath.stat().st_mtime, anchor_floor)
        adjust_mtime_fn(
            renders_dir, prev_out_mp4, prev_out_name, prev_active.get("pass_num"),
            prev_active, sc_entry, existing_takes, safe_mt, min_mtime=anchor_floor,
        )
    prev_strip = resolve_vault_media_path(proj_dir, renders_dir, prev_active, path_key="strip_path", rel_key="vault_rel_strip")
    if prev_strip and prev_strip.is_file():
        safe_link_fn(prev_strip, active_strip_fn(proj_dir, sid))


def is_valid_wgp_root(candidate: Path | str | None) -> bool:
    """Returns True only if `candidate` is an existing Wan2GP app directory with marker files."""
    if not candidate:
        return False
    try:
        p = Path(candidate)
        if not p.is_dir():
            return False
        return (
            (p / "wgp_config.json").is_file()
            or (p / "wgp.py").is_file()
            or (p / "models").is_dir()
            or (p / "shared" / "api.py").is_file()
        )
    except Exception:
        return False


def find_wgp_venv_python(wgp_root: Path | str | None) -> Path | None:
    """Returns the Python executable inside `wgp_root/venv` if present on disk."""
    if not wgp_root:
        return None
    try:
        base = Path(wgp_root)
        for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
            cand = base / rel
            if cand.is_file():
                return cand.resolve()
    except Exception:
        pass
    return None


def resolve_wgp_root_with_fallback(
    *configured_candidates: Path | str | None,
    auto_detect_fn=None,
) -> Path | None:
    """Validates `WGP_ROOT` and configured paths, re-scanning if prior paths disappeared."""
    env_wgp = os.environ.get("WGP_ROOT")
    if env_wgp and is_valid_wgp_root(env_wgp):
        return Path(env_wgp).resolve()
    for raw in configured_candidates:
        if raw and is_valid_wgp_root(raw):
            return Path(raw).resolve()
    if auto_detect_fn is not None:
        return auto_detect_fn()
    return None


def repair_stale_local_config(workspace_root: Path, wgp_root: Path | None = None, auto_detect_fn=None) -> bool:
    """Updates stale Pinokio/Wan2GP paths in existing machine-local config files when relocated."""
    ws = Path(workspace_root)
    target_wgp = Path(wgp_root).resolve() if (wgp_root and is_valid_wgp_root(wgp_root)) else (auto_detect_fn() if auto_detect_fn else None)
    if not target_wgp or not is_valid_wgp_root(target_wgp):
        return False
    target_wgp = Path(target_wgp).resolve()
    target_py = find_wgp_venv_python(target_wgp)
    pin_base = target_wgp.parents[2] if len(target_wgp.parts) >= 4 and target_wgp.parent.name.lower() == "wan.git" else None
    changed = False

    for fn in ("config.local.json", "local_config.json"):
        cfg_p = ws / fn
        if not cfg_p.is_file():
            continue
        try:
            data = json.loads(cfg_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        dirty = False
        old_wgp = data.get("wgp_root")
        if old_wgp and not is_valid_wgp_root(old_wgp):
            data["wgp_root"] = str(target_wgp)
            dirty = True
        old_py = data.get("python_path")
        if old_py and not Path(str(old_py)).is_file() and target_py:
            data["python_path"] = str(target_py)
            dirty = True
        old_pin = data.get("pinokio_root")
        if old_pin and not Path(str(old_pin)).is_dir() and pin_base and pin_base.is_dir():
            data["pinokio_root"] = str(pin_base)
            dirty = True
        if dirty:
            cfg_p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            changed = True

    bat_p = ws / "config.local.bat"
    if bat_p.is_file() and target_py:
        raw_bat = bat_p.read_text(encoding="utf-8", errors="ignore")
        m_py = re.search(r'(?im)^\s*set\s+"?PINOKIO_PY=([^"\r\n]+)"?', raw_bat)
        m_wgp = re.search(r'(?im)^\s*set\s+"?WGP_ROOT=([^"\r\n]+)"?', raw_bat)
        py_stale = bool(m_py and not Path(m_py.group(1).strip()).is_file())
        wgp_stale = bool(m_wgp and not is_valid_wgp_root(m_wgp.group(1).strip()))
        if py_stale or wgp_stale:
            pin_s = str(pin_base) if pin_base else str(target_wgp.parent)
            bat_p.write_text(
                "\n".join([
                    "@echo off",
                    "REM Machine-local overrides for Music Video Studio (ignored by git)",
                    f'set "PINOKIO_PY={target_py}"',
                    f'set "WGP_ROOT={target_wgp}"',
                    f'set "PATH={pin_s}\\bin;{pin_s}\\bin\\miniconda\\Library\\bin;{target_wgp}\\ffmpeg_bins;%PATH%"',
                    "",
                ]),
                encoding="utf-8",
            )
            changed = True
    return changed
