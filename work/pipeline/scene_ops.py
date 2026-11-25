"""
Scene operations module for Music Video Generation Studio.
Handles scene lifecycle mutations:
- derive_split_scene_ids
- derive_duplicate_scene_id
- split_scene_in_project_data
- delete_scene_in_project_data
- duplicate_scene_in_project_data
- duplicate_scene_with_takes
- handle_duplicate_scene_api
"""

import copy
import hashlib
import json
from pathlib import Path
import re


def _find_scene_index(scenes: list[dict], scene_id: str) -> int:
    """Finds a scene index in `scenes` by exact ID or zero-padded fallback ('S2' -> 'S02')."""
    target_id = str(scene_id).strip().upper()
    for i, sc in enumerate(scenes):
        if str(sc.get("id", "")).strip().upper() == target_id:
            return i
    m_num = re.match(r"^S0*(\d+)([A-Z]?)$", target_id)
    if m_num:
        t_num, t_sub = int(m_num.group(1)), m_num.group(2)
        for i, sc in enumerate(scenes):
            m_sid = re.match(r"^S0*(\d+)([A-Z]?)$", str(sc.get("id", "")).strip().upper())
            if m_sid and int(m_sid.group(1)) == t_num and m_sid.group(2) == t_sub:
                return i
    raise ValueError(f"Scene '{scene_id}' not found in project.")


def derive_duplicate_scene_id(orig_id: str, existing_ids: set[str] | list[str]) -> str:
    """Derives the next unique scene ID when duplicating an existing scene.

    Examples:
      'S05'  -> 'S05a' (or 'S05b', ... if taken)
      'S05a' -> 'S05b'
      'Intro' -> 'Intro_a' (or 'Intro_b', ... 'Intro_2')
    """
    clean_id = str(orig_id).strip()
    upper_existing = {str(x).strip().upper() for x in existing_ids}

    m_num = re.match(r"^(S0*\d+)$", clean_id, re.IGNORECASE)
    if m_num:
        for offset in range(26):
            cand = f"{clean_id}{chr(ord('a') + offset)}"
            if cand.upper() not in upper_existing:
                return cand

    m_letter = re.match(r"^(S0*\d+)([a-z])$", clean_id, re.IGNORECASE)
    if m_letter:
        prefix, cur_char = m_letter.group(1), m_letter.group(2).lower()
        for offset in range(1, 26):
            nxt_char = chr((ord(cur_char) - ord("a") + offset) % 26 + ord("a"))
            cand = f"{prefix}{nxt_char}"
            if cand.upper() not in upper_existing:
                return cand

    for offset in range(26):
        cand = f"{clean_id}_{chr(ord('a') + offset)}"
        if cand.upper() not in upper_existing:
            return cand

    n = 2
    while f"{clean_id}_{n}".upper() in upper_existing:
        n += 1
    return f"{clean_id}_{n}"


def _remap_duplicated_out_name(out_name: str, orig_id: str, dup_id: str) -> str:
    """Maps a render/take out_name from orig_id to dup_id preserving pass/resolution suffixes."""
    out_s = str(out_name or "").strip()
    orig_s = str(orig_id or "").strip()
    dup_s = str(dup_id or "").strip()
    if not out_s:
        return dup_s
    if orig_s and out_s.upper() == orig_s.upper():
        return dup_s
    if orig_s and out_s.upper().startswith(f"{orig_s.upper()}_"):
        return f"{dup_s}{out_s[len(orig_s):]}"
    m_res = re.match(r"^(.+?)(_\d+x\d+)$", out_s)
    if m_res:
        return f"{m_res.group(1)}_{dup_s}{m_res.group(2)}"
    return f"{out_s}_{dup_s}"


def derive_split_scene_ids(orig_id: str, existing_ids: set[str] | list[str]) -> tuple[str, str]:
    """Derives sensible unique IDs for splitting a scene into two parts.
    Examples:
      'S02'  -> ('S02a', 'S02b')
      'S12a' -> ('S12a', 'S12b')  (if S12b is unused)
      'S12b' -> ('S12b', 'S12c')
      'Intro' -> ('Intro_a', 'Intro_b')
    """
    clean_id = str(orig_id).strip()
    upper_existing = {str(x).strip().upper() for x in existing_ids}

    # Case 1: Standard S-prefixed number without letter suffix: e.g. S01, S2, S10
    m_num = re.match(r"^(S0*\d+)$", clean_id, re.IGNORECASE)
    if m_num:
        cand1 = f"{clean_id}a"
        cand2 = f"{clean_id}b"
        if cand1.upper() not in upper_existing and cand2.upper() not in upper_existing:
            return cand1, cand2

    # Case 2: Number with single letter suffix: e.g. S12a, S26a
    m_letter = re.match(r"^(S0*\d+)([a-z])$", clean_id, re.IGNORECASE)
    if m_letter:
        prefix = m_letter.group(1)
        cur_char = m_letter.group(2).lower()
        part1 = clean_id
        for offset in range(1, 26):
            nxt_char = chr((ord(cur_char) - ord("a") + offset) % 26 + ord("a"))
            cand = f"{prefix}{nxt_char}"
            if cand.upper() not in upper_existing:
                return part1, cand

    # Case 3: Letter suffix for alphanumeric names: e.g. Intro -> Intro_a, Intro_b
    cand1 = f"{clean_id}_a"
    cand2 = f"{clean_id}_b"
    if cand1.upper() not in upper_existing and cand2.upper() not in upper_existing:
        return cand1, cand2

    for offset in range(1, 26):
        cand = f"{clean_id}_{offset+1}"
        if cand.upper() not in upper_existing:
            return clean_id, cand

    return f"{clean_id}_part1", f"{clean_id}_part2"


def split_scene_in_project_data(
    data: dict,
    scene_id: str,
    split_time: float,
    id1: str | None = None,
    id2: str | None = None,
) -> tuple[dict, dict, dict]:
    """Splits a scene in project data into two sequential sub-scenes at split_time.

    Args:
        data: Project configuration dictionary containing a "scenes" list.
        scene_id: ID of the scene to split (e.g. "S02", "S12a").
        split_time: Point in seconds where the cut occurs (start < split_time < end).
        id1: Optional custom ID for the first part.
        id2: Optional custom ID for the second part.

    Returns:
        tuple of (modified_data, first_scene, second_scene)
    """
    scenes = data.get("scenes", [])
    target_idx = _find_scene_index(scenes, scene_id)
    target_scene = scenes[target_idx]

    start = float(target_scene.get("start", 0))
    end = float(target_scene.get("end", 0))
    split_time = round(float(split_time), 3)

    if not (start < split_time < end):
        raise ValueError(
            f"Split time {split_time:.2f}s must be strictly between scene start ({start:.2f}s) and end ({end:.2f}s)."
        )
    if (split_time - start < 0.05) or (end - split_time < 0.05):
        raise ValueError(
            f"Split time {split_time:.2f}s leaves a segment shorter than 0.05s (start: {start:.2f}s, end: {end:.2f}s)."
        )

    orig_id = str(target_scene.get("id", scene_id)).strip()
    existing_ids = {str(s.get("id", "")).strip() for s in scenes if s.get("id")}
    auto_id1, auto_id2 = derive_split_scene_ids(orig_id, existing_ids)
    final_id1 = str(id1).strip() if id1 else auto_id1
    final_id2 = str(id2).strip() if id2 else auto_id2

    if final_id1.upper() == final_id2.upper():
        raise ValueError(f"Split scene IDs cannot be identical ('{final_id1}').")

    other_ids = {s.get("id", "").strip().upper() for j, s in enumerate(scenes) if j != target_idx}
    if final_id1.upper() in other_ids:
        raise ValueError(f"Scene ID '{final_id1}' already exists in project.")
    if final_id2.upper() in other_ids:
        raise ValueError(f"Scene ID '{final_id2}' already exists in project.")

    scene1 = copy.deepcopy(target_scene)
    scene2 = copy.deepcopy(target_scene)

    scene1["id"] = final_id1
    scene1["start"] = start
    scene1["end"] = split_time

    scene2["id"] = final_id2
    scene2["start"] = split_time
    scene2["end"] = end

    if "title" in target_scene:
        orig_title = target_scene["title"]
        scene1["title"] = f"{orig_title} (Part 1)"
        scene2["title"] = f"{orig_title} (Part 2)"

    if "anchor_end" in scene1:
        scene1.pop("anchor_end", None)

    ov = target_scene.get("render_overlap") if target_scene.get("render_overlap") is not None else target_scene.get("render_overlap_seconds")
    if ov is not None:
        try:
            h_ov = float(ov.get("head", 0.0)) if isinstance(ov, dict) else float(ov)
            t_ov = float(ov.get("tail", 0.0)) if isinstance(ov, dict) else float(ov)
            if not any(scene1.get(k) is not None for k in ("render_pad_start", "pad_start", "head_overlap")):
                scene1["render_pad_start"] = h_ov
            if not any(scene2.get(k) is not None for k in ("render_pad_end", "pad_end", "tail_overlap")):
                scene2["render_pad_end"] = t_ov
        except (ValueError, TypeError):
            pass
        for ok in ("render_overlap", "render_overlap_seconds"):
            scene1.pop(ok, None)
            scene2.pop(ok, None)

    for k in ("render_pad_end", "pad_end", "tail_overlap"):
        scene1.pop(k, None)
    for k in ("render_pad_start", "pad_start", "head_overlap"):
        scene2.pop(k, None)

    if "output_name" in target_scene:
        out_orig = target_scene["output_name"]
        if out_orig.startswith(orig_id):
            scene1["output_name"] = final_id1 + out_orig[len(orig_id):]
            scene2["output_name"] = final_id2 + out_orig[len(orig_id):]
        else:
            scene1["output_name"] = f"{out_orig}_{final_id1}"
            scene2["output_name"] = f"{out_orig}_{final_id2}"

    # Increment seeds so the two separate video clips don't generate identical random noise
    for k in ("seed", "seed_m", "seed_f"):
        if k in scene2 and isinstance(scene2[k], int):
            scene2[k] += 1

    scenes[target_idx : target_idx + 1] = [scene1, scene2]
    return data, scene1, scene2


def delete_scene_in_project_data(
    data: dict,
    scene_id: str,
    close_gap: bool = True,
) -> tuple[dict, dict]:
    """Deletes a scene from project configuration by its scene ID.

    Args:
        data: Project configuration dictionary containing a "scenes" list.
        scene_id: ID of the scene to delete (e.g. "S02", "S12a").
        close_gap: If True (default), automatically adjusts adjacent scenes
                   to prevent empty gaps in the timeline:
                   - If deleted scene was first (index 0): next scene (now index 0)
                     has its 'start' adjusted to 0.00.
                   - If deleted scene was in the middle: previous scene has its
                     'end' extended to match the next scene's 'start'.
                   - If deleted scene was the last scene: previous scene has its
                     'end' extended to fill up to project.total_duration (or the
                     deleted scene's end).

    Returns:
        tuple of (modified_data, deleted_scene_dict)

    Raises:
        ValueError: If scene_id is not found in the project.
    """
    scenes = data.get("scenes", [])
    target_idx = _find_scene_index(scenes, scene_id)

    deleted_scene = scenes.pop(target_idx)

    if close_gap and scenes:
        if target_idx == 0:
            # First scene was removed: pull next scene back to 0.00
            scenes[0]["start"] = 0.00
        elif target_idx < len(scenes):
            # Middle scene was removed: extend previous scene's end to touch the next scene's start
            prev_scene = scenes[target_idx - 1]
            next_scene = scenes[target_idx]
            prev_scene["end"] = next_scene.get("start", prev_scene.get("end", 0))
        else:
            # Trailing scene was removed: extend new last scene to project total_duration or deleted scene's end
            last_scene = scenes[-1]
            total_dur = data.get("project", {}).get("total_duration")
            if isinstance(total_dur, (int, float)) and float(total_dur) > float(last_scene.get("start", 0)):
                last_scene["end"] = round(float(total_dur), 2)
            elif float(deleted_scene.get("end", 0)) > float(last_scene.get("start", 0)):
                last_scene["end"] = round(float(deleted_scene["end"]), 2)

    return data, deleted_scene


def duplicate_scene_in_project_data(
    data: dict,
    scene_id: str,
    dup_id: str | None = None,
) -> tuple[dict, dict, dict]:
    """Duplicates a scene immediately after the original in `data["scenes"]`.

    Timeline placement:
      - Non-final scene [start, end]: original becomes [start, mid], duplicate
        takes [mid, end] so all downstream scenes stay synced to the song audio.
      - Final scene [start, end]: original keeps [start, end] and duplicate is
        appended at [end, end + duration].
    """
    scenes = data.get("scenes", [])
    target_idx = _find_scene_index(scenes, scene_id)
    target_scene = scenes[target_idx]

    orig_id = str(target_scene.get("id", scene_id)).strip()
    existing_ids = {str(s.get("id", "")).strip() for s in scenes if s.get("id")}
    final_dup_id = str(dup_id).strip() if dup_id else derive_duplicate_scene_id(orig_id, existing_ids)
    if not final_dup_id:
        raise ValueError("Duplicate scene ID cannot be empty.")
    if final_dup_id.upper() in {x.upper() for x in existing_ids}:
        raise ValueError(f"Scene ID '{final_dup_id}' already exists in project.")

    orig_start = round(float(target_scene.get("start", 0.0)), 2)
    orig_end = round(float(target_scene.get("end", orig_start + 5.0)), 2)
    dur = round(orig_end - orig_start, 2)
    is_last = target_idx == len(scenes) - 1

    dup_scene = copy.deepcopy(target_scene)
    dup_scene["id"] = final_dup_id
    dup_scene.pop("locked", None)

    if is_last:
        eff_dur = dur if dur >= 0.5 else 5.0
        target_scene["start"] = orig_start
        target_scene["end"] = orig_end
        dup_scene["start"] = orig_end
        dup_scene["end"] = round(orig_end + eff_dur, 2)
    else:
        if dur < 0.2:
            raise ValueError(
                f"Scene '{orig_id}' duration ({dur:.2f}s) is too short to share its timeline slot with a duplicate."
            )
        mid = round(orig_start + dur / 2.0, 2)
        target_scene["start"] = orig_start
        target_scene["end"] = mid
        dup_scene["start"] = mid
        dup_scene["end"] = orig_end

    if target_scene.get("title"):
        dup_scene["title"] = f"{target_scene['title']} (Copy)"
    if target_scene.get("output_name"):
        dup_scene["output_name"] = _remap_duplicated_out_name(target_scene["output_name"], orig_id, final_dup_id)

    scenes.insert(target_idx + 1, dup_scene)
    return data, target_scene, dup_scene


def _compute_single_scene_hashes_safe(ctx, sc: dict) -> dict[str, tuple[str, ...]]:
    """Computes task hashes for a single scene dict without mutating `ctx`."""
    if ctx is None or not isinstance(sc, dict):
        return {}
    try:
        from .stage2_hashes import compute_project_task_hashes
        tmp_ctx = copy.copy(ctx)
        tmp_ctx.scenes = [sc]
        if isinstance(getattr(ctx, "raw", None), dict):
            tmp_ctx.raw = {**ctx.raw, "scenes": [sc]}
        return compute_project_task_hashes(tmp_ctx) or {}
    except Exception:
        return {}


def _dup_req_bounds(ctx, sc: dict) -> tuple[float, float]:
    """Computes effective [req_start, req_end] including custom render padding."""
    has_custom = any(
        sc.get(k) is not None
        for k in ("render_pad_start", "render_pad_end", "pad_start", "pad_end", "render_overlap", "head_overlap", "tail_overlap")
    )
    h_pad, t_pad = ctx.get_scene_render_padding(sc) if (ctx is not None and has_custom) else (0.0, 0.0)
    return (
        round(max(0.0, float(sc.get("start", 0.0)) - float(h_pad)), 3),
        round(float(sc.get("end", 0.0)) + float(t_pad), 3),
    )


def _clone_vault_media_for_dup(
    proj_dir: Path,
    renders_dir: Path,
    dup_takes_dir: Path,
    orig_sid: str,
    dup_sid: str,
    new_out: str,
    new_tid: str,
    old_tk: dict,
    new_tk: dict,
    used_mp4_names: set[str],
    fallback_strip: Path | None = None,
) -> None:
    """Hardlinks (or copies) a take's vaulted MP4 and review strip into `takes/<dup_sid>/`."""
    from .project_paths import resolve_vault_media_path
    from .takes import _file_path_for_vault, safe_link_or_copy

    v_src = resolve_vault_media_path(proj_dir, renders_dir, old_tk)
    stem_base = f"{new_out}__{new_tid.removeprefix('take_')}"
    if v_src and v_src.is_file():
        old_stem = v_src.stem
        if "__" in old_stem:
            o_part, h_part = old_stem.rsplit("__", 1)
            stem_base = f"{_remap_duplicated_out_name(o_part, orig_sid, dup_sid)}__{h_part}"
        cand_name = f"{stem_base}.mp4"
        if cand_name in used_mp4_names or (dup_takes_dir / cand_name).exists():
            cand_name = f"{stem_base}_{len(used_mp4_names) + 1}.mp4"
        used_mp4_names.add(cand_name)
        v_dst = dup_takes_dir / cand_name
        safe_link_or_copy(v_src, v_dst)
        rel_mp4 = f"takes/{dup_sid}/{cand_name}"
        new_tk["vault_rel_mp4"] = rel_mp4
        new_tk["file_path"] = _file_path_for_vault(proj_dir, renders_dir, rel_mp4)
        new_tk["size_bytes"] = v_dst.stat().st_size
    else:
        rel_mp4 = old_tk.get("vault_rel_mp4") or ""
        new_tk["vault_rel_mp4"] = rel_mp4
        new_tk["file_path"] = old_tk.get("file_path")

    s_src = resolve_vault_media_path(proj_dir, renders_dir, old_tk, path_key="strip_path", rel_key="vault_rel_strip")
    if (not s_src or not s_src.is_file()) and fallback_strip and fallback_strip.is_file():
        s_src = fallback_strip
    if s_src and s_src.is_file():
        s_name = f"{Path(stem_base).name}_strip.jpg"
        if v_src and new_tk.get("vault_rel_mp4"):
            s_name = f"{Path(new_tk['vault_rel_mp4']).stem}_strip.jpg"
        s_dst = dup_takes_dir / s_name
        safe_link_or_copy(s_src, s_dst)
        rel_strip = f"takes/{dup_sid}/{s_name}"
        new_tk["vault_rel_strip"] = rel_strip
        new_tk["strip_path"] = _file_path_for_vault(proj_dir, renders_dir, rel_strip)
    else:
        new_tk["vault_rel_strip"] = None
        new_tk["strip_path"] = None


def clone_scene_takes_in_project(
    proj_dir: Path,
    config_file: Path,
    orig_scene: dict,
    dup_scene: dict,
    orig_start_before: float,
    orig_end_before: float,
    orig_hashes_before: dict[str, tuple[str, ...]] | None = None,
    ctx_after=None,
) -> dict:
    """Clones all vault takes, filmstrips, active renders, and matching state hashes to `dup_scene`."""
    from .media_utils import locked_file_op
    from .project_paths import resolve_project_dirs, resolve_vault_media_path
    from .takes import (
        _active_strip_path,
        _pass_key,
        ensure_baseline_takes_seeded,
        get_state_hashes_file,
        load_takes_manifest,
        safe_link_or_copy,
        save_state_hashes_data,
        save_takes_manifest,
    )

    proj_dir, config_file = Path(proj_dir), Path(config_file)
    pdirs = resolve_project_dirs(proj_dir, config_file=config_file)
    renders_dir = pdirs["renders"]
    orig_sid = str(orig_scene.get("id") or "").strip()
    dup_sid = str(dup_scene.get("id") or "").strip()

    manifest = load_takes_manifest(proj_dir)
    scenes_map = manifest.setdefault("scenes", {})
    orig_entry = scenes_map.get(orig_sid)
    if orig_entry is None:
        orig_u = orig_sid.upper()
        orig_entry = next((v for k, v in scenes_map.items() if str(k).strip().upper() == orig_u), None)

    orig_takes = list((orig_entry or {}).get("takes") or [])
    state_file = get_state_hashes_file(proj_dir)
    state_data: dict = {}
    if state_file.is_file():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state_data = {}

    state_changed = False
    cur_hashes_after = _compute_single_scene_hashes_safe(ctx_after, dup_scene)
    orig_hashes_after = _compute_single_scene_hashes_safe(ctx_after, orig_scene)
    st_tasks = state_data.get("task_hashes") or state_data.get("tasks") or {}
    orig_scene_renders = state_data.get("scene_renders") or {}
    orig_settings_store = state_data.get("settings_store") or {}

    # Preserve up-to-date status on original scene if its legacy hash included `end` before halving
    for o_out, o_valid in orig_hashes_after.items():
        prev_h = st_tasks.get(o_out)
        if prev_h and orig_hashes_before and prev_h in (orig_hashes_before.get(o_out) or ()) and prev_h not in o_valid:
            state_data.setdefault("task_hashes", {})[o_out] = o_valid[0]
            state_data.setdefault("tasks", {})[o_out] = o_valid[0]
            state_changed = True

    if not orig_takes:
        if state_changed:
            save_state_hashes_data(proj_dir, state_data)
        return {"copied_takes_count": 0, "dup_takes": []}

    dup_req_s, dup_req_e = _dup_req_bounds(ctx_after, dup_scene)
    orig_active_map = (orig_entry or {}).get("active_takes") or {}
    orig_active_id = (orig_entry or {}).get("active_take_id")
    orig_active_ids = {v for v in orig_active_map.values() if v}
    if orig_active_id:
        orig_active_ids.add(orig_active_id)

    orig_pass_uptodate: dict[str, bool] = {}
    for t in orig_takes:
        o_out = str(t.get("out_name") or "").strip()
        if o_out and o_out not in orig_pass_uptodate:
            o_mp4 = renders_dir / f"{o_out}.mp4"
            prev_h = st_tasks.get(o_out)
            if o_out in (orig_hashes_before or {}):
                orig_pass_uptodate[o_out] = bool(
                    o_mp4.is_file() and (prev_h is None or prev_h in orig_hashes_before[o_out])
                )
            else:
                orig_pass_uptodate[o_out] = bool(o_mp4.is_file() and o_mp4.stat().st_size > 1000)

    dup_takes_dir = renders_dir / "takes" / dup_sid
    if dup_takes_dir.exists() or any(str(k).strip().upper() == dup_sid.upper() for k in scenes_map):
        from .scene_purge import purge_deleted_scene_artifacts
        purge_deleted_scene_artifacts(proj_dir, [dup_sid], config_data=getattr(ctx_after, "raw", None), config_file=config_file, clear_queue=False)
        manifest = load_takes_manifest(proj_dir)
        scenes_map = manifest.setdefault("scenes", {})
    dup_takes_dir.mkdir(parents=True, exist_ok=True)
    orig_active_strip = _active_strip_path(proj_dir, orig_sid)

    cloned_takes: list[dict] = []
    id_map: dict[str, str] = {}
    used_tids: set[str] = set()
    used_mp4_names: set[str] = set()
    snap_hash_cache: dict[tuple[str, str], str] = {}
    p1_hash_map: dict[str, str] = {}

    # Compute Pass 1 / solo takes before Pass 2 takes so parent_p1_task_hash remapping resolves cleanly
    order = sorted(range(len(orig_takes)), key=lambda i: (1 if orig_takes[i].get("pass_num") == 2 else 0, i))
    temp_slots: list[dict | None] = [None] * len(orig_takes)

    for idx in order:
        old_tk = orig_takes[idx]
        old_tid = str(old_tk.get("take_id") or "")
        old_out = str(old_tk.get("out_name") or "").strip()
        new_out = _remap_duplicated_out_name(old_out, orig_sid, dup_sid)
        is_act = bool(old_tid and old_tid in orig_active_ids)
        was_up = orig_pass_uptodate.get(old_out, False)

        raw_id_seed = f"{dup_sid}:{old_tid or idx}:{new_out}:{idx}"
        new_tid = f"take_{hashlib.sha1(raw_id_seed.encode()).hexdigest()[:12]}"
        while new_tid in used_tids:
            new_tid = f"take_{hashlib.sha1((raw_id_seed + ':x').encode()).hexdigest()[:12]}"
            raw_id_seed += ":x"
        used_tids.add(new_tid)
        if old_tid:
            id_map[old_tid] = new_tid

        new_tk = copy.deepcopy(old_tk)
        new_tk["take_id"] = new_tid
        new_tk["take_number"] = idx + 1
        new_tk["scene_id"] = dup_sid
        new_tk["out_name"] = new_out

        snap = new_tk.get("scene_snapshot")
        if isinstance(snap, dict):
            snap["id"] = dup_sid
            snap.pop("locked", None)
            if snap.get("output_name"):
                snap["output_name"] = _remap_duplicated_out_name(snap["output_name"], orig_sid, dup_sid)
            matched_orig_slot = (
                abs(float(snap.get("start", dup_scene["start"])) - orig_start_before) <= 0.05
                and abs(float(snap.get("end", dup_scene["end"])) - orig_end_before) <= 0.05
            )
            if (is_act and was_up) or matched_orig_slot:
                snap["start"] = dup_scene["start"]
                snap["end"] = dup_scene["end"]

        if is_act and was_up:
            new_tk["render_start"] = dup_req_s
            new_tk["render_end"] = dup_req_e
        elif (
            old_tk.get("render_start") is not None
            and old_tk.get("render_end") is not None
            and abs(float(old_tk["render_start"]) - orig_start_before) <= 0.05
            and abs(float(old_tk["render_end"]) - orig_end_before) <= 0.05
        ):
            new_tk["render_start"] = dup_req_s
            new_tk["render_end"] = dup_req_e

        # Resolve canonical task_hash for dup_sid
        new_hash = ""
        if is_act and was_up and new_out in cur_hashes_after and cur_hashes_after[new_out]:
            new_hash = cur_hashes_after[new_out][0]
        elif isinstance(snap, dict):
            cache_k = (json.dumps(snap, sort_keys=True, default=str), new_out)
            if cache_k not in snap_hash_cache:
                s_hashes = _compute_single_scene_hashes_safe(ctx_after, snap)
                snap_hash_cache[cache_k] = (s_hashes.get(new_out) or ("",))[0]
            new_hash = snap_hash_cache[cache_k]
        if not new_hash:
            new_hash = str(old_tk.get("task_hash") or "")
        new_tk["task_hash"] = new_hash

        if old_tk.get("pass_num") == 1 and old_tk.get("task_hash"):
            p1_hash_map[str(old_tk["task_hash"])] = new_hash
        elif old_tk.get("pass_num") == 2:
            old_p1_ref = str(old_tk.get("parent_p1_task_hash") or "")
            if old_p1_ref in p1_hash_map:
                new_tk["parent_p1_task_hash"] = p1_hash_map[old_p1_ref]

        fb_strip = orig_active_strip if (old_tid == orig_active_id and orig_active_strip.is_file()) else None
        _clone_vault_media_for_dup(
            proj_dir, renders_dir, dup_takes_dir, orig_sid, dup_sid, new_out, new_tid,
            old_tk, new_tk, used_mp4_names, fallback_strip=fb_strip,
        )
        temp_slots[idx] = new_tk

    cloned_takes = [t for t in temp_slots if t is not None]
    new_active_takes = {
        pkey: id_map[tid]
        for pkey, tid in orig_active_map.items()
        if tid in id_map
    }
    new_active_id = id_map.get(orig_active_id or "") or (cloned_takes[-1]["take_id"] if cloned_takes else None)
    if not new_active_takes and new_active_id:
        act_obj = next((t for t in cloned_takes if t["take_id"] == new_active_id), None)
        if act_obj:
            new_active_takes[_pass_key(act_obj.get("pass_num"))] = new_active_id

    scenes_map[dup_sid] = {
        "is_baseline_seeded": bool((orig_entry or {}).get("is_baseline_seeded", True)),
        "active_take_id": new_active_id,
        "active_takes": new_active_takes,
        "takes": cloned_takes,
    }

    # Link active MP4(s) & active strip for dup_sid, and sync state_hashes.json
    active_objs_by_pass: list[dict] = []
    for pkey, tid in new_active_takes.items():
        tk = next((t for t in cloned_takes if t["take_id"] == tid), None)
        if tk and tk not in active_objs_by_pass:
            active_objs_by_pass.append(tk)
    if new_active_id:
        tk_main = next((t for t in cloned_takes if t["take_id"] == new_active_id), None)
        if tk_main and tk_main not in active_objs_by_pass:
            active_objs_by_pass.append(tk_main)

    # Ensure Pass 1 is linked before Pass 2
    active_objs_by_pass.sort(key=lambda t: (1 if t.get("pass_num") == 2 else 0, int(t.get("take_number") or 0)))
    active_p1_obj = next((t for t in active_objs_by_pass if t.get("pass_num") == 1), None)

    for act_tk in active_objs_by_pass:
        new_out = str(act_tk.get("out_name") or "").strip()
        old_out = next((str(ot.get("out_name") or "").strip() for ot in orig_takes if id_map.get(str(ot.get("take_id") or "")) == act_tk["take_id"]), "")
        if act_tk.get("pass_num") == 2 and active_p1_obj and orig_pass_uptodate.get(old_out):
            if not act_tk.get("parent_p1_task_hash") or float(act_tk.get("created_at") or 0) < float(active_p1_obj.get("created_at") or 0):
                act_tk["parent_p1_task_hash"] = active_p1_obj.get("task_hash")
                if float(act_tk.get("created_at") or 0) < float(active_p1_obj.get("created_at") or 0):
                    act_tk["created_at"] = round(float(active_p1_obj.get("created_at") or 0) + 0.001, 3)

        orig_active_mp4_exists = bool(old_out and (renders_dir / f"{old_out}.mp4").is_file())
        if new_out and (orig_active_mp4_exists or act_tk["take_id"] == new_active_id):
            v_mp4 = resolve_vault_media_path(proj_dir, renders_dir, act_tk)
            if v_mp4 and v_mp4.is_file():
                safe_link_or_copy(v_mp4, renders_dir / f"{new_out}.mp4")

        if act_tk.get("task_hash") and new_out:
            state_data.setdefault("task_hashes", {})[new_out] = act_tk["task_hash"]
            state_data.setdefault("tasks", {})[new_out] = act_tk["task_hash"]
            state_changed = True
        if new_out:
            src_ri = orig_scene_renders.get(old_out, {}) if old_out else {}
            state_data.setdefault("scene_renders", {})[new_out] = {
                "render_start": act_tk.get("render_start"),
                "render_end": act_tk.get("render_end"),
                "frames": act_tk.get("frames", src_ri.get("frames")),
                "elapsed": act_tk.get("elapsed_s", src_ri.get("elapsed")),
                "completed_at": act_tk.get("created_at", src_ri.get("completed_at")),
            }
            if old_out and old_out in orig_settings_store:
                state_data.setdefault("settings_store", {})[new_out] = copy.deepcopy(orig_settings_store[old_out])
            state_changed = True

    # Link active timeline thumbnail strip for dup_sid
    dup_strip_dst = _active_strip_path(proj_dir, dup_sid)
    main_act = next((t for t in active_objs_by_pass if t["take_id"] == new_active_id), None) or (active_objs_by_pass[-1] if active_objs_by_pass else None)
    v_strip = resolve_vault_media_path(proj_dir, renders_dir, main_act, path_key="strip_path", rel_key="vault_rel_strip") if main_act else None
    if v_strip and v_strip.is_file():
        safe_link_or_copy(v_strip, dup_strip_dst)
    elif orig_active_strip.is_file():
        safe_link_or_copy(orig_active_strip, dup_strip_dst)

    if state_changed:
        save_state_hashes_data(proj_dir, state_data)
    with locked_file_op(pdirs["jobs"] / "takes_manifest.lock"):
        save_takes_manifest(proj_dir, manifest)

    return {
        "copied_takes_count": len(cloned_takes),
        "dup_takes": cloned_takes,
        "active_take_id": new_active_id,
        "active_takes": new_active_takes,
    }


def duplicate_scene_with_takes(
    proj_dir: Path | str,
    config_file: Path | str,
    scene_id: str,
    dup_id: str | None = None,
    write_config: bool = True,
) -> dict:
    """Duplicates a scene immediately after the original and copies all takes, strips, and active renders."""
    from .context import ProjectContext
    from .stage2_hashes import compute_project_task_hashes
    from .takes import ensure_baseline_takes_seeded

    proj_dir, config_file = Path(proj_dir), Path(config_file)
    try:
        ensure_baseline_takes_seeded(proj_dir, config_file)
    except Exception:
        pass

    orig_hashes_before: dict[str, tuple[str, ...]] = {}
    try:
        orig_hashes_before = compute_project_task_hashes(ProjectContext(config_file)) or {}
    except Exception:
        orig_hashes_before = {}

    data = json.loads(config_file.read_text(encoding="utf-8"))
    orig_idx = _find_scene_index(data.get("scenes", []), scene_id)
    orig_before = data["scenes"][orig_idx]
    orig_start_before = round(float(orig_before.get("start", 0.0)), 2)
    orig_end_before = round(float(orig_before.get("end", orig_start_before + 5.0)), 2)

    updated_data, orig_scene, dup_scene = duplicate_scene_in_project_data(data, scene_id, dup_id=dup_id)
    if write_config:
        config_file.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")

    try:
        ctx_after = ProjectContext(config_file)
    except Exception:
        ctx_after = None

    take_res = clone_scene_takes_in_project(
        proj_dir=proj_dir,
        config_file=config_file,
        orig_scene=orig_scene,
        dup_scene=dup_scene,
        orig_start_before=orig_start_before,
        orig_end_before=orig_end_before,
        orig_hashes_before=orig_hashes_before,
        ctx_after=ctx_after,
    )
    return {
        "data": updated_data,
        "scenes": updated_data.get("scenes", []),
        "orig_scene": orig_scene,
        "duplicated_scene": dup_scene,
        **take_res,
    }


def handle_duplicate_scene_api(handler, srv, body: dict | None, path: str = "", require_project=None) -> None:
    """HTTP POST handler for `/api/scene/duplicate` and `/api/project/<slug>/scene/duplicate`."""
    from urllib.parse import unquote

    payload = body if isinstance(body, dict) else {}
    slug = payload.get("slug")
    if not slug and path.startswith("/api/project/") and path.endswith("/scene/duplicate"):
        slug = unquote(path[len("/api/project/"): -len("/scene/duplicate")].strip("/"))
    scene_id = payload.get("scene_id")
    dup_id = payload.get("dup_id")
    if not slug or not scene_id:
        handler.send_error_json("slug and scene_id are required")
        return

    proj_dir, config_file = require_project(handler, srv, str(slug))
    if not proj_dir:
        return
    try:
        srv.backup_project_json(config_file, throttle_seconds=0.0)
        res = duplicate_scene_with_takes(proj_dir, config_file, str(scene_id), dup_id=dup_id, write_config=True)
        updated_queue = srv.compute_fast_project_queue(str(slug), force_recheck=True)
        orig_sc, dup_sc = res["orig_scene"], res["duplicated_scene"]
        if hasattr(srv, "broadcast_log"):
            srv.broadcast_log(
                f"[Studio UI] Scene {orig_sc.get('id')} duplicated -> {dup_sc.get('id')} "
                f"({dup_sc.get('start')}s-{dup_sc.get('end')}s, copied {res['copied_takes_count']} take(s))"
            )
        handler.send_json({
            "success": True,
            "orig_scene": orig_sc,
            "duplicated_scene": dup_sc,
            "scenes": res["scenes"],
            "copied_takes_count": res["copied_takes_count"],
            "dup_takes": res["dup_takes"],
            "slug": str(slug),
            "queue": updated_queue,
            "done_queue": srv.get_done_queue_history(srv, str(slug), updated_queue),
        })
    except Exception as e:
        handler.send_error_json(str(e))

