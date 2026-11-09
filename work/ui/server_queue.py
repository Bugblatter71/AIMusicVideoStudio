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
except ImportError:
    try:
        from work.pipeline.context import derive_split_scene_ids, split_scene_in_project_data, delete_scene_in_project_data, resolve_scene_padding
    except ImportError:
        from run_overnight_v3 import derive_split_scene_ids, split_scene_in_project_data, delete_scene_in_project_data, resolve_scene_padding

try:
    from pipeline.takes import ensure_baseline_takes_seeded, try_auto_promote_vault_hit
    from pipeline.media_utils import attach_queue_time_estimates
except ImportError:
    try:
        from work.pipeline.takes import ensure_baseline_takes_seeded, try_auto_promote_vault_hit
        from work.pipeline.media_utils import attach_queue_time_estimates
    except ImportError:
        ensure_baseline_takes_seeded = None
        try_auto_promote_vault_hit = None
        attach_queue_time_estimates = None


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
    if m:
        return f"A{int(m.group(1)):02d}{(m.group(2) or '').lower()}"
    parts = re.split(r"[-_ \.]", s)
    m2 = re.match(r"^A(\d+)([a-z]?)$", parts[0].strip(), re.IGNORECASE) if parts else None
    if m2:
        return f"A{int(m2.group(1)):02d}{(m2.group(2) or '').lower()}"
    return s.upper()


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
        models.get("default_video_resolution")
        or models.get("resolution")
        or (models.get("video_shared_settings", {}).get("resolution") if isinstance(models.get("video_shared_settings"), dict) else None)
        or data.get("project", {}).get("master_resolution")
        or "4"
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

    clip_search_dirs = [
        trimmed_dir,
        renders_dir,
        proj_dir / "work" / "trimmed_v3",
        proj_dir / "work" / "renders_v3",
    ]

    def _clip_score(cname: str, cpath: str) -> int:
        low_name = cname.lower()
        low_path = cpath.lower()
        if "placeholder" in low_name:
            return -100
        if "pass2_final" in low_name:
            return 50
        if "pass2" in low_name:
            return 45
        if "renders" in low_path and "pass1" not in low_name:
            return 40
        if "pass1" in low_name:
            return 30
        if "trimmed" in low_path:
            return 20
        return 10

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
        is_two_pass = mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
        res_str = default_res
        if is_two_pass:
            p1_name = f"{sid}_Pass1_Left_{res_str}"
            p2_name = f"{sid}_Pass2_Final_{res_str}"
            valid_p1_for_sid[sid] = p1_name
            valid_passes[p1_name] = {"scene_id": sid, "target_arg": f"{sid}:p1", "pass": 1, "resolution": res_str}
            valid_passes[p2_name] = {"scene_id": sid, "target_arg": f"{sid}:p2", "pass": 2, "resolution": res_str}
        else:
            if mode == "male_solo":
                raw_name = sc.get("output_name", f"{sid}_male_solo")
            elif mode == "female_solo":
                raw_name = sc.get("output_name", f"{sid}_female_solo")
            elif mode == "silent_locked":
                raw_name = sc.get("output_name", f"{sid}_silent")
            else:
                raw_name = sc.get("output_name", f"{sid}_{mode}")
            out_fn = f"{raw_name}_{res_str}" if res_str not in raw_name else raw_name
            valid_passes[out_fn] = {"scene_id": sid, "target_arg": sid, "pass": None, "resolution": res_str}

    task_hashes_current = {}
    ctx = None
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
        except ImportError:
            pass

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
    for idx_a in range(len(existing_items)):
        for idx_b in range(idx_a + 1, len(existing_items)):
            ia, ib = existing_items[idx_a], existing_items[idx_b]
            sa = ia.get("scene_id") or srv.derive_scene_id(ia.get("output_filename", ""))
            sb = ib.get("scene_id") or srv.derive_scene_id(ib.get("output_filename", ""))
            pa = ia.get("pass") or (2 if "Pass2" in ia.get("output_filename", "") else (1 if "Pass1" in ia.get("output_filename", "") else None))
            pb = ib.get("pass") or (2 if "Pass2" in ib.get("output_filename", "") else (1 if "Pass1" in ib.get("output_filename", "") else None))
            if sa and sa == sb and pa == 2 and pb == 1:
                existing_items[idx_a], existing_items[idx_b] = existing_items[idx_b], existing_items[idx_a]

    queue = []
    seen = set()
    p1_needs_rerender: dict[str, str] = {}
    live_eng = srv.read_project_engine_status(slug)
    eng_rendering = bool(isinstance(live_eng, dict) and live_eng.get("state") == "rendering")
    proc_active = bool(srv.active_process is not None and srv.active_process.poll() is None)
    is_engine_active = proc_active or eng_rendering
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
                skipped_hash = current_item.get("user_skipped_hash")
                skipped_a_mtime = float(current_item.get("user_skipped_anchor_mtime") or 0.0)
                hash_unchanged = (skipped_hash is not None and skipped_hash == cur_task_hash)
                anchor_unchanged = (not anchor_info or not skipped_a_mtime or cur_anchor_mtime <= skipped_a_mtime + 0.001)
                if hash_unchanged and anchor_unchanged:
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
            render_info = scene_renders.get(out_name)
            if render_info and "render_start" in render_info and "render_end" in render_info:
                if not ((float(render_info["render_start"]) <= req_s + 0.05) and (float(render_info["render_end"]) >= req_e - 0.05)):
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
        item["scene_id"] = sid
        item["target_arg"] = spec["target_arg"]
        item["pass"] = pass_num

        status, reason, needs_render, t_hash, a_mtime = _evaluate_pass(out_name, sid, pass_num, current_item=item)
        item["status"] = status
        item["reason"] = reason
        item["needs_render"] = needs_render
        item["task_hash"] = t_hash
        item["anchor_mtime"] = a_mtime
        if status != "skipped" or reason != "SKIPPED BY USER":
            item.pop("user_skipped_hash", None)
            item.pop("user_skipped_anchor_mtime", None)

        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        if mp4_path.exists() and mp4_path.stat().st_size > 50000:
            if not item.get("completed_at"):
                item["completed_at"] = round(mp4_path.stat().st_mtime, 3)
        render_info = scene_renders.get(out_name)
        if render_info:
            if not item.get("completed_at") and render_info.get("completed_at"):
                item["completed_at"] = render_info.get("completed_at")
            if not item.get("elapsed") and render_info.get("elapsed"):
                item["elapsed"] = render_info.get("elapsed")

        if pass_num == 1 and needs_render:
            p1_needs_rerender[sid] = reason

        queue.append(item)

    # 2. Add any project passes not already in the queue
    for out_name, spec in valid_passes.items():
        if out_name in seen:
            continue
        seen.add(out_name)
        sid = spec["scene_id"]
        tgt_arg = spec["target_arg"]
        pass_num = spec["pass"]
        res_str = spec["resolution"]
        sc = scene_by_id.get(sid)
        dur_val = round(float(sc.get("end", 0)) - float(sc.get("start", 0)), 2) if sc else 0.0

        status, reason, needs_render, t_hash, a_mtime = _evaluate_pass(out_name, sid, pass_num)
        if pass_num == 1 and needs_render:
            p1_needs_rerender[sid] = reason

        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        completed_at = round(mp4_path.stat().st_mtime, 3) if mp4_path.exists() and mp4_path.stat().st_size > 50000 else None
        elapsed_val = None
        render_info = scene_renders.get(out_name)
        if render_info:
            if render_info.get("completed_at"):
                completed_at = render_info.get("completed_at")
            if render_info.get("elapsed"):
                elapsed_val = render_info.get("elapsed")

        queue.append({
            "idx": len(queue) + 1, "id": out_name, "scene_id": sid, "target_arg": tgt_arg,
            "pass": pass_num, "filename": f"{out_name}.mp4", "output_filename": out_name,
            "duration": dur_val, "completed_at": completed_at, "elapsed": elapsed_val,
            "status": status, "reason": reason, "needs_render": needs_render,
            "task_hash": t_hash, "anchor_mtime": a_mtime,
            "model_type": "minimax_h3_fl2va_pruned_pdd", "resolution": res_str,
        })

    live_eng = srv.read_project_engine_status(slug) if is_engine_active else None
    for i, itm in enumerate(queue, 1):
        itm["idx"] = i
        itm["total"] = len(queue)
        if itm.get("status") == "rendering" and isinstance(live_eng, dict):
            eng_fn = live_eng.get("filename") or ""
            if not eng_fn or eng_fn in (itm.get("filename"), itm.get("output_filename")):
                for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count"):
                    if k in live_eng and live_eng[k] is not None:
                        itm[k] = live_eng[k]

    if attach_queue_time_estimates:
        try:
            attach_queue_time_estimates(queue, scene_by_id, ctx=ctx, proj_dir=proj_dir, default_res=default_res, live_eng=live_eng)
        except Exception:
            pass

    if hashes_changed[0] and hashes_file.is_file():
        try:
            h_data = json.loads(hashes_file.read_text(encoding="utf-8"))
            h_tasks = h_data.setdefault("tasks", {})
            h_tasks.update(hashes)
            h_th = h_data.setdefault("task_hashes", {})
            h_th.update(hashes)
            hashes_file.write_text(json.dumps(h_data, indent=2), encoding="utf-8")
        except Exception:
            pass

    try:
        queue_file.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    except Exception:
        pass

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
        tgt_num = int(m_tgt.group(1))
        tgt_sub = m_tgt.group(2)
        m_sid = re.match(r"^S0*(\d+)([A-Z]?)$", sid_clean)
        if m_sid:
            sid_num = int(m_sid.group(1))
            sid_sub = m_sid.group(2)
            if tgt_num == sid_num and tgt_sub == sid_sub:
                return True
        out_prefix = out_clean.split("_")[0]
        m_out = re.match(r"^S0*(\d+)([A-Z]?)$", out_prefix)
        if m_out:
            out_num = int(m_out.group(1))
            out_sub = m_out.group(2)
            if tgt_num == out_num and tgt_sub == out_sub:
                return True

    return False


def move_in_project_queue(slug: str, item_id: str, direction: str = "up") -> list[dict]:
    """Moves a queued item up or down among queued items, enforcing that Pass 1 of a two-pass scene always precedes Pass 2."""
    srv = _srv()
    proj_dir = srv.PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)

    items = json.loads(queue_file.read_text(encoding="utf-8"))
    clean_id = str(item_id).replace(".mp4", "")
    idx = next(
        (
            i for i, x in enumerate(items)
            if x.get("output_filename") in (item_id, clean_id)
            or x.get("filename") in (item_id, clean_id)
            or x.get("id") in (item_id, clean_id)
        ),
        None,
    )
    if idx is not None:
        moving_item = items[idx]
        moving_sid = moving_item.get("scene_id") or srv.derive_scene_id(moving_item.get("output_filename", ""))
        moving_pass = moving_item.get("pass")
        if moving_pass is None:
            if "Pass1" in moving_item.get("output_filename", ""):
                moving_pass = 1
            elif "Pass2" in moving_item.get("output_filename", ""):
                moving_pass = 2

        swap_idx = None
        if direction == "up":
            for i in range(idx - 1, -1, -1):
                if items[i].get("status") == "queued":
                    swap_idx = i
                    break
        else:
            for i in range(idx + 1, len(items)):
                if items[i].get("status") == "queued":
                    swap_idx = i
                    break

        if swap_idx is not None:
            # Guard: Pass 2 cannot move above Pass 1 of the same scene; Pass 1 cannot move below Pass 2 of the same scene
            blocked = False
            if direction == "up" and moving_pass == 2:
                for k in range(swap_idx, idx):
                    other_sid = items[k].get("scene_id") or srv.derive_scene_id(items[k].get("output_filename", ""))
                    other_pass = items[k].get("pass") or (1 if "Pass1" in items[k].get("output_filename", "") else None)
                    if other_sid == moving_sid and other_pass == 1:
                        blocked = True
                        break
            elif direction != "up" and moving_pass == 1:
                for k in range(idx + 1, swap_idx + 1):
                    other_sid = items[k].get("scene_id") or srv.derive_scene_id(items[k].get("output_filename", ""))
                    other_pass = items[k].get("pass") or (2 if "Pass2" in items[k].get("output_filename", "") else None)
                    if other_sid == moving_sid and other_pass == 2:
                        blocked = True
                        break

            if not blocked:
                items[idx], items[swap_idx] = items[swap_idx], items[idx]
                for i, itm in enumerate(items, 1):
                    itm["idx"] = i
                queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return items


def remove_from_project_queue(slug: str, item_id: str) -> list[dict]:
    """Marks a queued item as explicitly skipped by user, recording its current task_hash so it stays skipped until edited."""
    srv = _srv()
    proj_dir = srv.PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)

    items = json.loads(queue_file.read_text(encoding="utf-8"))
    clean_id = str(item_id).replace(".mp4", "")
    for item in items:
        if (
            item.get("output_filename") in (item_id, clean_id)
            or item.get("filename") in (item_id, clean_id)
            or item.get("id") in (item_id, clean_id)
        ):
            item["status"] = "skipped"
            item["needs_render"] = False
            item["reason"] = "SKIPPED BY USER"
            item["user_skipped_hash"] = item.get("task_hash", "")
            item["user_skipped_anchor_mtime"] = item.get("anchor_mtime", 0.0)
            break
    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return items


def enqueue_in_project_queue(slug: str, item_id: str) -> list[dict]:
    """Forces an item into the render queue."""
    srv = _srv()
    proj_dir = srv.PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)

    items = json.loads(queue_file.read_text(encoding="utf-8"))
    clean_id = str(item_id).replace(".mp4", "")
    for item in items:
        if (
            item.get("output_filename") in (item_id, clean_id)
            or item.get("filename") in (item_id, clean_id)
            or item.get("id") in (item_id, clean_id)
        ):
            item["status"] = "queued"
            item["needs_render"] = True
            item["reason"] = "FORCED"
            item.pop("user_skipped_hash", None)
            item.pop("user_skipped_anchor_mtime", None)
            break
    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return items


def clear_project_queue(slug: str) -> list[dict]:
    """Marks all currently queued items as skipped by user until edited or Queue All is clicked."""
    srv = _srv()
    proj_dir = srv.PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    if not queue_file.is_file():
        compute_fast_project_queue(slug)

    items = json.loads(queue_file.read_text(encoding="utf-8"))
    for item in items:
        if item.get("status") == "queued":
            item["status"] = "skipped"
            item["needs_render"] = False
            item["reason"] = "SKIPPED BY USER"
            item["user_skipped_hash"] = item.get("task_hash", "")
            item["user_skipped_anchor_mtime"] = item.get("anchor_mtime", 0.0)
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
                is_two_pass = mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
                if is_two_pass:
                    targets = [
                        (f"{sid}_Pass1_Left_{res_str}", f"{sid}:p1", 1),
                        (f"{sid}_Pass2_Final_{res_str}", f"{sid}:p2", 2),
                    ]
                else:
                    raw_name = sc.get("output_name", f"{sid}_{mode}")
                    out_fn = f"{raw_name}_{res_str}" if res_str not in raw_name else raw_name
                    targets = [(out_fn, sid, None)]

                for out_fn, tgt_arg, p_num in targets:
                    if target_pass is not None and p_num != target_pass:
                        continue
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
        for i, itm in enumerate(items, 1):
            itm["idx"] = i

    if attach_queue_time_estimates and config_file and config_file.is_file():
        try:
            cfg_d = json.loads(config_file.read_text(encoding="utf-8"))
            sc_map = {s.get("id"): s for s in cfg_d.get("scenes", []) if s.get("id")}
            attach_queue_time_estimates(items, sc_map, proj_dir=proj_dir, default_res=resolve_project_resolution(cfg_d))
        except Exception:
            pass

    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return matched_items
