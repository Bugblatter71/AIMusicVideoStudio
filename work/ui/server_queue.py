"""Queue computation, state validation, and scene ordering for Studio UI."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

try:
    from pipeline.context import derive_split_scene_ids, split_scene_in_project_data, delete_scene_in_project_data, resolve_scene_padding
    from pipeline.takes import ensure_baseline_takes_seeded, try_auto_promote_vault_hit
    from pipeline.media_utils import attach_queue_time_estimates
except ImportError:
    try:
        from work.pipeline.context import derive_split_scene_ids, split_scene_in_project_data, delete_scene_in_project_data, resolve_scene_padding
        from work.pipeline.takes import ensure_baseline_takes_seeded, try_auto_promote_vault_hit
        from work.pipeline.media_utils import attach_queue_time_estimates
    except ImportError:
        from run_overnight_v3 import derive_split_scene_ids, split_scene_in_project_data, delete_scene_in_project_data, resolve_scene_padding
        ensure_baseline_takes_seeded, try_auto_promote_vault_hit, attach_queue_time_estimates = None, None, None


def _srv():
    mod = sys.modules.get("work.ui.server") or sys.modules.get("server")
    if mod is not None:
        return mod
    try:
        from work.ui import server
        return server
    except ImportError:
        import server
        return server


def extract_anchor_id(name_or_str: str | Path) -> str:
    """Extracts normalized anchor ID like 'A01', 'A05b' ... from any filename or string."""
    s = Path(str(name_or_str)).name.strip()
    m = re.match(r"^A(\d{1,2})([a-z]?)(?:[ _\-\.]|$)", s, re.IGNORECASE)
    if m: return f"A{int(m.group(1)):02d}{(m.group(2) or '').lower()}"
    parts = re.split(r"[-_ \.]", s)
    m2 = re.match(r"^A(\d+)([a-z]?)$", parts[0].strip(), re.IGNORECASE) if parts else None
    return f"A{int(m2.group(1)):02d}{(m2.group(2) or '').lower()}" if m2 else s.upper()


def backup_project_json(json_path: Path, throttle_seconds: float = 0.0):
    srv = _srv()
    json_path = Path(json_path)
    if not json_path.exists(): return
    now, key = time.time(), str(json_path.resolve())
    if throttle_seconds > 0 and key in srv.last_backup_times and (now - srv.last_backup_times[key] < throttle_seconds):
        return
    srv.last_backup_times[key] = now
    srv.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(json_path, srv.BACKUP_DIR / f"{json_path.stem}_{ts}.json")


def resolve_project_resolution(data: dict) -> str:
    models = data.get("models", {})
    raw = (
        models.get("default_video_resolution") or models.get("resolution")
        or (models.get("video_shared_settings", {}).get("resolution") if isinstance(models.get("video_shared_settings"), dict) else None)
        or data.get("project", {}).get("master_resolution") or "4"
    )
    res_map = {"1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088"}
    s = str(raw).strip()
    return res_map.get(s, s.lower() if "x" in s.lower() else "1344x768")


def collect_project_clips(proj_dir: Path, data: dict) -> dict[str, dict]:
    """Collect rendered clips across renders_dir and trimmed_dir, prioritizing
    Pass2_Final over Pass1, renders with audio over trimmed, and strictly
    excluding placeholders from scene mapping.
    """
    proj_dir = Path(proj_dir)
    trimmed_dir = proj_dir / data.get("project", {}).get("directories", {}).get("trimmed", "work/trimmed")
    renders_dir = proj_dir / data.get("project", {}).get("directories", {}).get("renders", "work/renders")

    clip_search_dirs = [trimmed_dir, renders_dir, proj_dir / "work" / "trimmed_v3", proj_dir / "work" / "renders_v3"]

    def _clip_score(cname: str, cpath: str) -> int:
        low_name, low_path = cname.lower(), cpath.lower()
        if "placeholder" in low_name: return -100
        if "pass2_final" in low_name: return 50
        if "pass2" in low_name: return 45
        if "renders" in low_path and "pass1" not in low_name: return 40
        if "pass1" in low_name: return 30
        return 20 if "trimmed" in low_path else 10

    existing_clips = {}
    canonical_scores = {}
    scanned_clip_dirs = set()
    scene_renders = {}
    for hf in (proj_dir / "work" / "jobs_v3" / "state_hashes.json", proj_dir / "work" / "state_hashes.json"):
        if hf.is_file():
            try: scene_renders = json.loads(hf.read_text(encoding="utf-8")).get("scene_renders", {}); break
            except Exception: pass
    for cdir in clip_search_dirs:
        if cdir.is_dir():
            rcdir = str(cdir.resolve())
            if rcdir in scanned_clip_dirs:
                continue
            scanned_clip_dirs.add(rcdir)
            for clip in cdir.glob("*.mp4"):
                if clip.name.endswith(".tmp.mp4"):
                    continue
                rel_path = clip.relative_to(proj_dir).as_posix()
                score = _clip_score(clip.name, rel_path)
                info = {
                    "filename": clip.name, "path": rel_path,
                    "size_bytes": clip.stat().st_size, "mtime": clip.stat().st_mtime,
                }
                r_inf = scene_renders.get(clip.stem)
                if r_inf and "render_start" in r_inf:
                    info["render_start"], info["render_end"] = r_inf["render_start"], r_inf.get("render_end")
                if clip.name not in existing_clips:
                    existing_clips[clip.name] = info

                # Exclude placeholders from canonical scene ID mapping
                if "placeholder" in clip.name.lower():
                    continue

                m = re.search(r"(?:^|_)(S\d{1,2}[a-z]?)(?:[_\.]|$)", clip.name, re.IGNORECASE)
                if m:
                    matched_sid = m.group(1)
                    for sid_variant in (matched_sid, matched_sid.upper(), matched_sid.lower()):
                        prev_score = canonical_scores.get(sid_variant, -999)
                        prev_info = existing_clips.get(sid_variant)
                        should_replace = False
                        if prev_info is None or score > prev_score:
                            should_replace = True
                        elif score == prev_score and info["mtime"] > prev_info["mtime"]:
                            should_replace = True

                        if should_replace:
                            canonical_scores[sid_variant] = score
                            existing_clips[sid_variant] = info

    return existing_clips


def compute_fast_project_queue(slug: str, force_recheck: bool = False) -> list[dict]:
    """Instantly evaluates all scenes in the project JSON against disk, anchor mtimes/hashes, and state hashes."""
    srv = _srv()
    proj_dir = srv.PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        return []

    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        return []
    config_file = jsons[-1]

    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    if ensure_baseline_takes_seeded:
        try:
            ensure_baseline_takes_seeded(proj_dir, config_file)
        except Exception:
            pass

    renders_dir = proj_dir / "work" / "renders_v3"
    anchors_dir = proj_dir / "work" / "anchors_v3"
    jobs_dir = proj_dir / "work" / "jobs_v3"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    queue_file = jobs_dir / "v3_queue.json"

    hashes_file = jobs_dir / "state_hashes.json"
    if not hashes_file.is_file():
        alt_hashes = proj_dir / "work" / "state_hashes.json"
        if alt_hashes.is_file():
            hashes_file = alt_hashes
    hashes = {}
    stored_anchor_hashes = {}
    scene_renders = {}
    hashes_changed = [False]
    if hashes_file.is_file():
        try:
            h_data = json.loads(hashes_file.read_text(encoding="utf-8"))
            hashes = h_data.get("tasks", {})
            stored_anchor_hashes = h_data.get("anchor_file_hashes", {})
            scene_renders = h_data.get("scene_renders", {})
        except Exception:
            pass

    # Scan anchor files on disk in anchors_dir
    disk_anchors: dict[str, dict] = {}
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    if anchors_dir.exists():
        for p in anchors_dir.iterdir():
            if p.is_file() and p.suffix.lower() in exts:
                aid = extract_anchor_id(p.name)
                if aid.startswith("A") and aid[1:].isdigit():
                    try:
                        disk_anchors[aid] = {
                            "path": p,
                            "mtime": p.stat().st_mtime,
                            "hash": hashlib.sha256(p.read_bytes()).hexdigest()[:16],
                        }
                    except Exception:
                        pass

    scenes = data.get("scenes", [])
    scene_by_id = {sc.get("id"): sc for sc in scenes if sc.get("id")}
    default_res = resolve_project_resolution(data)

    # Build canonical map of valid passes for the current project scenes and modes
    valid_passes: dict[str, dict] = {}
    valid_p1_for_sid: dict[str, str] = {}
    for sc in scenes:
        sid = sc.get("id", "")
        if not sid:
            continue
        mode = sc.get("mode", "solo")
        is_two_pass = mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
        res_str = default_res
        if is_two_pass:
            p1_name, p2_name = f"{sid}_Pass1_Left_{res_str}", f"{sid}_Pass2_Final_{res_str}"
            valid_p1_for_sid[sid] = p1_name
            valid_passes[p1_name] = {"scene_id": sid, "target_arg": f"{sid}:p1", "pass": 1, "resolution": res_str}
            valid_passes[p2_name] = {"scene_id": sid, "target_arg": f"{sid}:p2", "pass": 2, "resolution": res_str}
        else:
            if mode in ("duet_method_a", "duet_a"):
                cand = next((c for c in (f"{sid}_duet_a", f"{sid}_duet_method_a", f"{sid}_duet_stereo") if (renders_dir / f"{c}_{res_str}.mp4").exists()), None) if not sc.get("output_name") else None
                raw_name = sc.get("output_name", cand or f"{sid}_duet_a")
            else:
                def_mode = "male_solo" if mode == "male_solo" else ("female_solo" if mode == "female_solo" else ("silent" if mode in ("silent_locked", "silent", "ambient") else mode))
                cand = next((c for c in (f"{sid}_{def_mode}", f"{sid}_solo", f"{sid}_male_solo", f"{sid}_female_solo") if (renders_dir / f"{c}_{res_str}.mp4").exists()), None) if not sc.get("output_name") and mode in ("solo", "male_solo", "female_solo") else None
                raw_name = sc.get("output_name", cand or f"{sid}_{def_mode}")
            out_fn = f"{raw_name}_{res_str}" if res_str not in raw_name else raw_name
            valid_passes[out_fn] = {"scene_id": sid, "target_arg": sid, "pass": None, "resolution": res_str}

    task_hashes_current, ctx = {}, None
    ProjectContext = getattr(srv, "ProjectContext", None)
    compute_project_task_hashes = getattr(srv, "compute_project_task_hashes", None)
    resolve_rendered_clip_path = getattr(srv, "resolve_rendered_clip_path", None)
    if not ProjectContext or not compute_project_task_hashes:
        try:
            from work.pipeline.context import ProjectContext as _PC
            from work.pipeline.stage2_hashes import compute_project_task_hashes as _CPTH
            from work.pipeline.media_utils import resolve_rendered_clip_path as _RRCP
            ProjectContext = ProjectContext or _PC
            compute_project_task_hashes = compute_project_task_hashes or _CPTH
            resolve_rendered_clip_path = resolve_rendered_clip_path or _RRCP
        except ImportError: pass

    if ProjectContext and compute_project_task_hashes and config_file.is_file():
        try:
            ctx = ProjectContext(config_file)
            task_hashes_current = compute_project_task_hashes(ctx, target_resolution=default_res)
        except Exception:
            pass

    existing_items = []
    if queue_file.is_file():
        try:
            existing_items = json.loads(queue_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Ensure Pass 1 of any two-pass scene in existing_items precedes Pass 2 of the same scene
    normalize_queue_units(existing_items, srv)

    queue = []
    seen = set()
    p1_needs_rerender: dict[str, str] = {}
    live_eng = srv.read_project_engine_status(slug)
    eng_rendering = bool(isinstance(live_eng, dict) and live_eng.get("state") == "rendering")
    proc_active = bool(srv.active_process is not None and srv.active_process.poll() is None)
    is_engine_active = proc_active or eng_rendering or bool(getattr(srv, "queue_worker_active", False))
    active_eng_fn = str(live_eng.get("filename") or "") if (eng_rendering and isinstance(live_eng, dict)) else ""

    # Helper to evaluate pass state
    def _evaluate_pass(out_name: str, sid: str, pass_num: int | None, current_item: dict | None = None) -> tuple[str, str, bool, str, float]:
        if resolve_rendered_clip_path:
            mp4_path = resolve_rendered_clip_path(renders_dir, out_name)
        else:
            mp4_path = renders_dir / f"{out_name}.mp4"
        file_exists = mp4_path.exists() and mp4_path.stat().st_size > 50000

        curr_hashes = task_hashes_current.get(out_name)
        cur_task_hash = curr_hashes[0] if curr_hashes else ""

        sc = scene_by_id.get(sid)
        aid = extract_anchor_id(sc.get("anchor", "")) if sc else ""
        aid_end = extract_anchor_id(sc.get("anchor_end", "")) if (sc and pass_num != 2 and sc.get("anchor_end")) else ""
        anchor_info, end_info = disk_anchors.get(aid), (disk_anchors.get(aid_end) if aid_end else None)
        cur_anchor_mtime = max(float(anchor_info["mtime"]) if anchor_info else 0.0, float(end_info["mtime"]) if end_info else 0.0)

        # Preserve active rendering status only for the task matching the active engine job
        is_active_eng_task = bool(active_eng_fn and (out_name == active_eng_fn or f"{out_name}.mp4" == active_eng_fn))
        if is_engine_active and (is_active_eng_task or (not active_eng_fn and current_item and current_item.get("status") == "rendering")):
            ren_reason = (live_eng.get("reason") if is_active_eng_task else None) or (current_item.get("reason") if current_item else None) or "RENDERING"
            return "rendering", str(ren_reason), True, cur_task_hash, cur_anchor_mtime

        # Preserve explicit user curation (FORCED or SKIPPED BY USER) unless force_recheck is requested
        if current_item and not force_recheck:
            cur_status = current_item.get("status")
            cur_reason = str(current_item.get("reason") or "")
            if cur_status == "queued" and (cur_reason == "FORCED" or cur_reason.startswith("TARGETED")):
                return "queued", cur_reason, True, cur_task_hash, cur_anchor_mtime
            if cur_status == "skipped" and cur_reason == "SKIPPED BY USER":
                s_h, s_m = current_item.get("user_skipped_hash"), float(current_item.get("user_skipped_anchor_mtime") or 0.0)
                if (s_h == cur_task_hash) and (not anchor_info or not s_m or cur_anchor_mtime <= s_m + 0.001):
                    return "skipped", "SKIPPED BY USER", False, cur_task_hash, cur_anchor_mtime

        if not file_exists:
            return "queued", "MISSING FILE", True, cur_task_hash, cur_anchor_mtime

        # Check Two-Pass Pass 2 dependency on Pass 1
        if pass_num == 2:
            if sid in p1_needs_rerender:
                return "queued", f"PASS 1 RE-RENDERED ({p1_needs_rerender[sid]})", True, cur_task_hash, cur_anchor_mtime
            p1_name = valid_p1_for_sid.get(sid)
            if p1_name:
                p1_mp4 = resolve_rendered_clip_path(renders_dir, p1_name) if resolve_rendered_clip_path else (renders_dir / f"{p1_name}.mp4")
                if p1_mp4.exists() and p1_mp4.stat().st_size > 50000 and mp4_path.stat().st_mtime < p1_mp4.stat().st_mtime:
                    return "queued", "PASS 1 NEWER ON DISK", True, cur_task_hash, cur_anchor_mtime

        # Check timeline bounds coverage
        if sc:
            has_custom = any(sc.get(k) is not None for k in ("render_pad_start", "render_pad_end", "pad_start", "pad_end", "render_overlap", "head_overlap", "tail_overlap"))
            h_pad, t_pad = (ctx.get_scene_render_padding(sc) if ctx else resolve_scene_padding(sc)) if has_custom else (0.0, 0.0)
            req_s, req_e = max(0.0, float(sc.get("start", 0)) - h_pad), float(sc.get("end", 0)) + t_pad
            r_info = scene_renders.get(out_name)
            allow_pad = bool(sc.get("hold_last_frame") or sc.get("pad_tail") or sc.get("allow_tail_pad"))
            if r_info and "render_start" in r_info and "render_end" in r_info:
                if not ((float(r_info["render_start"]) <= req_s + 0.05) and (allow_pad or float(r_info["render_end"]) >= req_e - 0.05)):
                    return "queued", "TIMELINE CHANGED", True, cur_task_hash, cur_anchor_mtime

        for a_id, a_inf in ((aid, anchor_info), (aid_end, end_info)):
            if a_id and a_inf and mp4_path.stat().st_mtime < a_inf["mtime"]:
                return "queued", f"ANCHOR {a_id} NEWER ON DISK", True, cur_task_hash, cur_anchor_mtime
            if a_id and a_inf and stored_anchor_hashes.get(a_id) and a_inf["hash"] != stored_anchor_hashes.get(a_id):
                return "queued", f"ANCHOR {a_id} EDITED", True, cur_task_hash, cur_anchor_mtime

        # Check character reference images if model supports reference images
        if ctx and sc:
            try:
                vrid, vrspec = ctx.resolve_video_renderer(sc, pass_num=pass_num)
                if vrspec.get("supports_image_refs") or "ref2va" in vrspec.get("model_type", "").lower() or "fasth3" in vrspec.get("model_type", "").lower():
                    raw_p = sc.get("prompt") or sc.get("prompt_m" if pass_num == 1 else "prompt_f") or ""
                    cand_refs = ctx.resolve_scene_image_refs(sc, raw_p)
                    from pipeline.prompts import resolve_scene_refs
                    resolved_refs, _ = resolve_scene_refs(raw_p, cand_refs)
                    for r_path in resolved_refs:
                        if r_path.exists() and mp4_path.stat().st_mtime < r_path.stat().st_mtime:
                            return "queued", f"REF {r_path.name} NEWER ON DISK", True, cur_task_hash, cur_anchor_mtime
            except Exception:
                pass

        # Check if prompt, seed, stem, renderer, or settings have been edited
        if curr_hashes:
            prev_h = hashes.get(out_name)
            if prev_h is not None:
                if prev_h not in curr_hashes:
                    promoted = False
                    if try_auto_promote_vault_hit and cur_task_hash:
                        try:
                            if try_auto_promote_vault_hit(proj_dir, config_file, sid, pass_num, cur_task_hash):
                                file_exists = mp4_path.exists() and mp4_path.stat().st_size > 50000
                                if file_exists:
                                    hashes[out_name] = cur_task_hash
                                    hashes_changed[0] = True
                                    promoted = True
                        except Exception:
                            pass
                    if promoted:
                        return "completed", "UP-TO-DATE (VAULT HIT)", False, cur_task_hash, cur_anchor_mtime
                    else:
                        return "queued", "PROMPT/SEED/SETTINGS EDITED", True, cur_task_hash, cur_anchor_mtime
            else:
                hashes[out_name] = cur_task_hash
                hashes_changed[0] = True
        elif out_name not in hashes:
            hashes[out_name] = cur_task_hash
            hashes_changed[0] = True

        # File exists and is up-to-date with anchor and settings
        return "completed", "UP-TO-DATE", False, cur_task_hash, cur_anchor_mtime

    # 1. Preserve existing user-ordered items if still valid in current project scenes/modes/resolution
    for item in existing_items:
        out_name = item.get("output_filename")
        if not out_name or out_name in seen or out_name not in valid_passes:
            continue
        seen.add(out_name)
        spec = valid_passes[out_name]
        sid = spec["scene_id"]
        pass_num = spec["pass"]
        sc = scene_by_id.get(sid)
        if sc:
            item["duration"] = round(float(sc.get("end", 0)) - float(sc.get("start", 0)), 2)
        item.update({"scene_id": sid, "target_arg": spec["target_arg"], "pass": pass_num})

        status, reason, needs_render, t_hash, a_mtime = _evaluate_pass(out_name, sid, pass_num, current_item=item)
        item.update({"status": status, "reason": reason, "needs_render": needs_render, "task_hash": t_hash, "anchor_mtime": a_mtime})
        if status != "skipped" or reason != "SKIPPED BY USER":
            item.pop("user_skipped_hash", None); item.pop("user_skipped_anchor_mtime", None)
        if status != "rendering":
            for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "estimated_remaining_seconds", "estimated_remaining_str"):
                item.pop(k, None)

        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        if mp4_path.exists() and mp4_path.stat().st_size > 50000 and not item.get("completed_at"):
            item["completed_at"] = round(mp4_path.stat().st_mtime, 3)
        render_info = scene_renders.get(out_name)
        if render_info:
            if not item.get("completed_at") and render_info.get("completed_at"): item["completed_at"] = render_info.get("completed_at")
            if not item.get("elapsed") and render_info.get("elapsed"): item["elapsed"] = render_info.get("elapsed")

        if pass_num == 1 and needs_render: p1_needs_rerender[sid] = reason
        queue.append(item)

    # 2. Add any project passes not already in the queue
    for out_name, spec in valid_passes.items():
        if out_name in seen: continue
        seen.add(out_name); sid, tgt_arg, pass_num, res_str = spec["scene_id"], spec["target_arg"], spec["pass"], spec["resolution"]
        sc = scene_by_id.get(sid)
        dur_val = round(float(sc.get("end", 0)) - float(sc.get("start", 0)), 2) if sc else 0.0

        status, reason, needs_render, t_hash, a_mtime = _evaluate_pass(out_name, sid, pass_num)
        if pass_num == 1 and needs_render: p1_needs_rerender[sid] = reason

        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        completed_at = round(mp4_path.stat().st_mtime, 3) if mp4_path.exists() and mp4_path.stat().st_size > 50000 else None
        elapsed_val = None
        render_info = scene_renders.get(out_name)
        if render_info:
            if render_info.get("completed_at"): completed_at = render_info.get("completed_at")
            if render_info.get("elapsed"): elapsed_val = render_info.get("elapsed")

        queue.append({
            "idx": len(queue) + 1, "id": out_name, "scene_id": sid, "target_arg": tgt_arg,
            "pass": pass_num, "filename": f"{out_name}.mp4", "output_filename": out_name,
            "duration": dur_val, "completed_at": completed_at, "elapsed": elapsed_val,
            "status": status, "reason": reason, "needs_render": needs_render,
            "task_hash": t_hash, "anchor_mtime": a_mtime,
            "model_type": "minimax_h3_fl2va_pruned_pdd", "resolution": res_str,
        })

    normalize_queue_units(queue, srv)
    live_eng = srv.read_project_engine_status(slug) if is_engine_active else None
    for i, itm in enumerate(queue, 1):
        itm["idx"], itm["total"] = i, len(queue)
        if itm.get("status") == "rendering" and isinstance(live_eng, dict):
            eng_fn = live_eng.get("filename") or ""
            if not eng_fn or eng_fn in (itm.get("filename"), itm.get("output_filename")):
                for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count"):
                    if k in live_eng and live_eng[k] is not None: itm[k] = live_eng[k]
        elif itm.get("status") != "rendering":
            for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "estimated_remaining_seconds", "estimated_remaining_str"):
                itm.pop(k, None)

    if attach_queue_time_estimates:
        try:
            attach_queue_time_estimates(queue, scene_by_id, ctx=ctx, proj_dir=proj_dir, default_res=default_res, live_eng=live_eng)
        except Exception:
            pass

    if hashes_changed[0] and hashes_file.is_file():
        try:
            h_data = json.loads(hashes_file.read_text(encoding="utf-8"))
            h_data.setdefault("tasks", {}).update(hashes); h_data.setdefault("task_hashes", {}).update(hashes)
            h_txt, alt_h = json.dumps(h_data, indent=2), proj_dir / "work" / "state_hashes.json"
            hashes_file.write_text(h_txt, encoding="utf-8")
            if alt_h != hashes_file and alt_h.is_file(): alt_h.write_text(h_txt, encoding="utf-8")
        except Exception: pass

    try: queue_file.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    except Exception: pass

    return queue


def is_scene_match(sid: str, out_fn: str, target_base: str) -> bool:
    """Checks whether a scene ID or output filename matches the target scene string.
    Normalizes single-digit scene prefixes (e.g. 'S1' matches 'S01', 'S2' matches 'S02'),
    while strictly preventing false positives (e.g. 'S1' will NEVER match 'S10', 'S11', 'S12a')."""
    if not target_base:
        return False
    target_base = target_base.strip().upper()
    sid_clean = (sid or "").strip().upper()
    out_clean = (out_fn or "").strip().upper()

    # Exact matches
    if sid_clean == target_base or out_clean == target_base:
        return True
    if out_clean.startswith(f"{target_base}_"):
        return True

    # Normalized zero-padded match: e.g. S1 <-> S01
    m_tgt = re.match(r"^S0*(\d+)([A-Z]?)$", target_base)
    if m_tgt:
        tgt_num, tgt_sub = int(m_tgt.group(1)), m_tgt.group(2)
        m_sid = re.match(r"^S0*(\d+)([A-Z]?)$", sid_clean)
        if m_sid and tgt_num == int(m_sid.group(1)) and tgt_sub == m_sid.group(2): return True
        m_out = re.match(r"^S0*(\d+)([A-Z]?)$", out_clean.split("_")[0])
        if m_out and tgt_num == int(m_out.group(1)) and tgt_sub == m_out.group(2): return True
    return False


def _group_queue_units(items: list[dict], srv=None) -> list[list[dict]]:
    """Groups queue items into atomic SceneUnits, keeping Pass 1 and Pass 2 of any two-pass scene contiguous
    in [Pass 1, Pass 2] order, and pins any unit containing an actively rendering task ahead of all queued units."""
    srv = srv or _srv()
    by_sid: dict[str, list[dict]] = {}
    units: list[list[dict]] = []
    for itm in items:
        out_fn = itm.get("output_filename") or itm.get("filename") or ""
        sid = str(itm.get("scene_id") or srv.derive_scene_id(out_fn) or "").strip()
        if sid:
            itm["scene_id"] = sid
        p_num = itm.get("pass")
        if p_num is None:
            p_num = 1 if "pass1" in out_fn.lower() else (2 if "pass2" in out_fn.lower() else None)
            if p_num is not None:
                itm["pass"] = p_num
        sid_k = sid.upper()
        if sid_k and p_num in (1, 2):
            if sid_k not in by_sid:
                by_sid[sid_k] = []
                units.append(by_sid[sid_k])
            by_sid[sid_k].append(itm)
        else:
            units.append([itm])
    for u in by_sid.values():
        u.sort(key=lambda x: x.get("pass") or 0)
        if len(u) == 2:
            p1, p2 = u[0], u[1]
            if p2.get("status") == "rendering" and p1.get("status") == "queued":
                p1.update({"status": "completed", "needs_render": False})
            elif p1.get("status") in ("queued", "rendering") or p1.get("needs_render"):
                if p2.get("status") not in ("queued", "rendering") or not p2.get("needs_render"):
                    r = p1.get("reason") or "QUEUED"
                    p2.update({"status": "queued", "needs_render": True, "reason": f"PASS 1 RE-RENDERED ({r})"})
                    p2.pop("user_skipped_hash", None)
                    p2.pop("user_skipped_anchor_mtime", None)
    ren_idx = next((i for i, u in enumerate(units) if any(x.get("status") == "rendering" for x in u)), None)
    first_q = next((i for i, u in enumerate(units) if i != ren_idx and any(x.get("status") == "queued" for x in u)), None)
    if ren_idx is not None and first_q is not None and ren_idx > first_q:
        units.insert(first_q, units.pop(ren_idx))
    return units


def normalize_queue_units(items: list[dict], srv=None) -> list[dict]:
    """Flattens grouped SceneUnits back into `items` in-place and re-numbers 1-based `idx`."""
    flat = [itm for u in _group_queue_units(items, srv) for itm in u]
    for i, itm in enumerate(flat, 1):
        itm["idx"], itm["total"] = i, len(flat)
    items[:] = flat
    return items


def reorder_in_project_queue(
    slug: str, item_id: str = "", target_id: str = "", position: str = "before", order: list[str] | None = None
) -> list[dict]:
    """Reorders queued items by moving item_id's SceneUnit before/after target_id's SceneUnit,
    or according to sequence in order. Multiple passes of the same scene remain strictly coupled in [Pass 1, Pass 2] order.
    Actively rendering items remain pinned at the top of the queue.
    """
    srv = _srv()
    queue_file = srv.PROJECTS_DIR / slug / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)
    items = json.loads(queue_file.read_text(encoding="utf-8"))
    units = _group_queue_units(items, srv)
    is_ren = lambda u: any(x.get("status") == "rendering" for x in u)
    is_q = lambda u: (not is_ren(u)) and any(x.get("status") == "queued" for x in u)

    if order:
        order_keys = [str(x).replace(".mp4", "").upper() for x in order]
        def _unit_rank(u):
            for x in u:
                for k in (x.get("scene_id", ""), x.get("output_filename", ""), x.get("filename", ""), x.get("id", "")):
                    k_clean = str(k).replace(".mp4", "").upper()
                    if k_clean in order_keys:
                        return order_keys.index(k_clean)
            return 999999
        ren_units = [u for u in units if is_ren(u)]
        q_units = [u for u in units if is_q(u)]
        other_units = [u for u in units if not is_ren(u) and not is_q(u)]
        q_units.sort(key=_unit_rank)
        units = ren_units + q_units + other_units
    elif item_id and target_id:
        clean_src = str(item_id).replace(".mp4", "")
        clean_dst = str(target_id).replace(".mp4", "")
        def _match_unit(val, cval):
            return next((i for i, u in enumerate(units) if any(x.get("output_filename") in (val, cval) or x.get("filename") in (val, cval) or x.get("id") in (val, cval) or str(x.get("scene_id", "")).upper() == cval.upper() for x in u)), None)
        src_idx = _match_unit(item_id, clean_src)
        dst_idx = _match_unit(target_id, clean_dst)
        if src_idx is not None and dst_idx is not None and src_idx != dst_idx and is_q(units[src_idx]):
            src_u = units.pop(src_idx)
            dst_idx = _match_unit(target_id, clean_dst)
            if dst_idx is not None:
                if is_ren(units[dst_idx]):
                    insert_pos = dst_idx + 1
                elif position == "after":
                    insert_pos = dst_idx + 1
                else:
                    insert_pos = dst_idx
                units.insert(insert_pos, src_u)

    flat = [itm for u in _group_queue_units([x for u in units for x in u], srv) for itm in u]
    for i, itm in enumerate(flat, 1):
        itm["idx"], itm["total"] = i, len(flat)
    queue_file.write_text(json.dumps(flat, indent=2), encoding="utf-8")
    return flat


def move_in_project_queue(slug: str, item_id: str, direction: str = "up") -> list[dict]:
    """Moves a queued item (and its coupled pass, if two-pass) up or down as an atomic unit among queued units,
    keeping active renders pinned at the top of the queued list and Pass 1 strictly before Pass 2."""
    srv = _srv()
    queue_file = srv.PROJECTS_DIR / slug / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)
    items = json.loads(queue_file.read_text(encoding="utf-8"))
    units = _group_queue_units(items, srv)
    clean_id = str(item_id).replace(".mp4", "")
    u_idx = next((i for i, u in enumerate(units) if any(x.get("output_filename") in (item_id, clean_id) or x.get("filename") in (item_id, clean_id) or x.get("id") in (item_id, clean_id) for x in u)), None)
    if u_idx is not None:
        is_ren = lambda u: any(x.get("status") == "rendering" for x in u)
        is_q = lambda u: (not is_ren(u)) and any(x.get("status") == "queued" for x in u)
        if is_q(units[u_idx]):
            swap_u = None
            if direction == "up":
                for i in range(u_idx - 1, -1, -1):
                    if is_ren(units[i]): break
                    if is_q(units[i]):
                        swap_u = i
                        break
            else:
                for i in range(u_idx + 1, len(units)):
                    if is_q(units[i]):
                        swap_u = i
                        break
            if swap_u is not None:
                tgt_id = units[swap_u][0].get("output_filename") or units[swap_u][0].get("id")
                return reorder_in_project_queue(slug, item_id=item_id, target_id=tgt_id, position="before" if direction == "up" else "after")
    flat = [itm for u in units for itm in u]
    for i, itm in enumerate(flat, 1):
        itm["idx"], itm["total"] = i, len(flat)
    queue_file.write_text(json.dumps(flat, indent=2), encoding="utf-8")
    return flat


def remove_from_project_queue(slug: str, item_id: str) -> list[dict]:
    """Marks a queued item as explicitly skipped by user, cascading to Pass 2 if Pass 1 is missing on disk or Pass 2 depends on it."""
    srv, proj_dir = _srv(), _srv().PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file(): compute_fast_project_queue(slug)
    items = json.loads(queue_file.read_text(encoding="utf-8"))
    normalize_queue_units(items, srv)
    clean_id = str(item_id).replace(".mp4", "")
    for item in items:
        if item.get("output_filename") in (item_id, clean_id) or item.get("filename") in (item_id, clean_id) or item.get("id") in (item_id, clean_id):
            item.update({"status": "skipped", "needs_render": False, "reason": "SKIPPED BY USER", "user_skipped_hash": item.get("task_hash", ""), "user_skipped_anchor_mtime": item.get("anchor_mtime", 0.0)})
            if item.get("pass") == 1:
                p1_mp4 = proj_dir / "work" / "renders_v3" / f"{item.get('output_filename', '')}.mp4"
                p1_exists = p1_mp4.is_file() and p1_mp4.stat().st_size > 50000
                for sib in items:
                    if sib.get("scene_id") == item.get("scene_id") and sib.get("pass") == 2 and sib.get("status") == "queued":
                        if not p1_exists or str(sib.get("reason") or "").startswith("PASS 1"):
                            sib.update({"status": "skipped", "needs_render": False, "reason": "SKIPPED BY USER", "user_skipped_hash": sib.get("task_hash", ""), "user_skipped_anchor_mtime": sib.get("anchor_mtime", 0.0)})
            break
    normalize_queue_units(items, srv)
    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return items


def enqueue_in_project_queue(slug: str, item_id: str) -> list[dict]:
    """Forces an item into the render queue, cascading Pass 1 -> Pass 2 and keeping any active render pinned ahead of queued items."""
    srv = _srv()
    queue_file = srv.PROJECTS_DIR / slug / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file(): compute_fast_project_queue(slug)
    items = json.loads(queue_file.read_text(encoding="utf-8"))
    normalize_queue_units(items, srv)
    clean_id = str(item_id).replace(".mp4", "")
    target_item = next((it for it in items if it.get("output_filename") in (item_id, clean_id) or it.get("filename") in (item_id, clean_id) or it.get("id") in (item_id, clean_id)), None)
    if not target_item:
        items = compute_fast_project_queue(slug, force_recheck=True)
        target_item = next((it for it in items if it.get("output_filename") in (item_id, clean_id) or it.get("filename") in (item_id, clean_id) or it.get("id") in (item_id, clean_id)), None)
    if target_item:
        target_item.update({"status": "queued", "needs_render": True, "reason": "FORCED"})
        target_item.pop("user_skipped_hash", None); target_item.pop("user_skipped_anchor_mtime", None)
        sid = target_item.get("scene_id") or srv.derive_scene_id(target_item.get("output_filename", ""))
        for sib in items:
            sib_sid = sib.get("scene_id") or srv.derive_scene_id(sib.get("output_filename", ""))
            if sib_sid == sid and sib is not target_item and sib.get("status") != "rendering":
                sib.update({"status": "queued", "needs_render": True, "reason": "PASS 1 RE-RENDERED (FORCED)" if target_item.get("pass") == 1 else "COUPLED PASS (FORCED)"})
                sib.pop("user_skipped_hash", None); sib.pop("user_skipped_anchor_mtime", None)
    normalize_queue_units(items, srv)
    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return items


def clear_project_queue(slug: str) -> list[dict]:
    """Marks all currently queued items as skipped by user until edited or Queue All is clicked."""
    srv = _srv()
    queue_file = srv.PROJECTS_DIR / slug / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)
    items = json.loads(queue_file.read_text(encoding="utf-8"))
    for item in items:
        if item.get("status") == "queued":
            item.update({"status": "skipped", "needs_render": False, "reason": "SKIPPED BY USER", "user_skipped_hash": item.get("task_hash", ""), "user_skipped_anchor_mtime": item.get("anchor_mtime", 0.0)})
    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return items


def prepare_targeted_queue_items(slug: str, target_sid: str, is_worker_running: bool | None = None) -> list[dict]:
    """Prepares the queue for a targeted scene render:
    - When idle (is_worker_running=False): defers other queued items so ONLY the targeted scene renders, placing it at index 0.
    - When running (is_worker_running=True): keeps the active render running and places the targeted scene immediately after the active item.
    """
    srv = _srv()
    target_sid = str(target_sid).strip().upper()
    target_base = target_sid.split(":")[0].strip()
    target_pass = None
    if ":" in target_sid:
        p_tok = target_sid.split(":")[1].lower().replace("p", "").strip()
        if p_tok.isdigit():
            target_pass = int(p_tok)

    proj_dir = srv.PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    config_file = jsons[-1] if jsons else None

    if not queue_file.is_file():
        compute_fast_project_queue(slug)

    items = []
    if queue_file.is_file():
        try:
            items = json.loads(queue_file.read_text(encoding="utf-8"))
        except Exception:
            items = []

    if is_worker_running is None:
        is_worker_running = bool(
            (getattr(srv, "queue_worker_active", False) and srv.active_process is not None and srv.active_process.poll() is None)
            or any(x.get("status") == "rendering" for x in items)
        )

    matched_items = []
    for item in items:
        out_fn = item.get("output_filename", "")
        sid = item.get("scene_id") or srv.derive_scene_id(out_fn)
        item["scene_id"] = sid
        if not item.get("target_arg"):
            item["target_arg"] = srv.derive_target_arg(out_fn)

        pass_num = item.get("pass")
        if pass_num is None:
            if "Pass1" in out_fn:
                pass_num = 1
            elif "Pass2" in out_fn:
                pass_num = 2
            item["pass"] = pass_num

        is_match = is_scene_match(sid, out_fn, target_base)
        if is_match and target_pass is not None:
            is_match = (pass_num == target_pass)

        if is_match:
            if not (is_worker_running and item.get("status") == "rendering"):
                item["status"] = "queued"
                item["needs_render"] = True
                item["reason"] = f"TARGETED ({target_sid})"
                item.pop("user_skipped_hash", None)
                item.pop("user_skipped_anchor_mtime", None)
            matched_items.append(item)
        else:
            if not is_worker_running:
                if item.get("status") in ("queued", "rendering"):
                    item["status"] = "skipped"
                    item["needs_render"] = False
                    item["reason"] = "DEFERRED (SINGLE SCENE RENDER)"

    # If targeted scene was not found in existing queue, generate it from project config
    if not matched_items and config_file and config_file.is_file():
        try:
            cfg_data = json.loads(config_file.read_text(encoding="utf-8"))
            sc = next((s for s in cfg_data.get("scenes", []) if is_scene_match(s.get("id", ""), "", target_base)), None)
            if sc:
                res_str = resolve_project_resolution(cfg_data)
                sid = sc.get("id", target_base)
                mode = sc.get("mode", "solo")
                is_two = mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
                raw_name = sc.get("output_name", f"{sid}_duet_a" if mode in ("duet_method_a", "duet_a") else f"{sid}_{mode}")
                s_fn = f"{raw_name}_{res_str}" if res_str not in raw_name else raw_name
                targets = [(f"{sid}_Pass1_Left_{res_str}", f"{sid}:p1", 1), (f"{sid}_Pass2_Final_{res_str}", f"{sid}:p2", 2)] if is_two else [(s_fn, sid, None)]
                for out_fn, tgt_arg, p_num in targets:
                    if target_pass is not None and p_num != target_pass: continue
                    new_item = {
                        "idx": len(items) + 1, "id": out_fn, "scene_id": sid, "target_arg": tgt_arg,
                        "pass": p_num, "filename": f"{out_fn}.mp4", "output_filename": out_fn,
                        "status": "queued", "reason": f"TARGETED ({target_sid})", "needs_render": True,
                        "model_type": "minimax_h3_fl2va_pruned_pdd", "resolution": res_str,
                    }
                    items.insert(0, new_item)
                    matched_items.append(new_item)
        except Exception as ge:
            srv.broadcast_log(f"[Studio UI Warning] Could not dynamically generate queue item for {target_sid}: {ge}")

    # Ensure Pass 1 is always ordered before Pass 2 within matched_items
    matched_items.sort(key=lambda x: x.get("pass") or 0)

    if matched_items:
        movable = [m for m in matched_items if m.get("status") != "rendering"]
        for m in reversed(movable):
            if m in items:
                items.remove(m)
            if is_worker_running:
                render_idx = next((i for i, x in enumerate(items) if x.get("status") == "rendering"), -1)
                items.insert(render_idx + 1, m)
            else:
                items.insert(0, m)
        normalize_queue_units(items, srv)

    if attach_queue_time_estimates and config_file and config_file.is_file():
        try:
            cfg_d = json.loads(config_file.read_text(encoding="utf-8"))
            sc_map = {s.get("id"): s for s in cfg_d.get("scenes", []) if s.get("id")}
            attach_queue_time_estimates(items, sc_map, proj_dir=proj_dir, default_res=resolve_project_resolution(cfg_d))
        except Exception:
            pass

    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return matched_items
