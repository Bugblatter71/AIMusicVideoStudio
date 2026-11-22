"""Multi-engine parallel queue scheduler with configurable per-engine concurrency slots."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

try:
    from .server_engine_io import (
        ENGINES,
        ENGINE_LABELS,
        MAX_PARALLEL_BOUNDS,
        clamp_parallel,
        clean_task_stem,
        cleanup_after_kill,
        count_eligible_items,
        effective_item_backend,
        emit_process_line,
        execute_slot_task_with_oom_retry,
        extract_backend_overrides,
        is_fresh_render_status,
        is_status_valid_for_slot,
        mark_task_completed_status,
        mark_task_failed_status,
        read_task_failure_summary,
        norm_engine,
        pick_next_eligible_item,
        read_project_engine_statuses,
        read_project_task_statuses,
        read_queue_json_locked,
        reset_session_blocks_for_items,
        resolve_srv_obj as _resolve_srv_obj,
        save_persisted_max_parallel,
        set_queue_item_backend_override as _set_queue_item_backend_override_io,
        terminate_proc,
        write_queue_json_locked,
        write_starting_slot_status,
    )
except ImportError:
    from server_engine_io import (  # type: ignore
        ENGINES,
        ENGINE_LABELS,
        MAX_PARALLEL_BOUNDS,
        clamp_parallel,
        clean_task_stem,
        cleanup_after_kill,
        count_eligible_items,
        effective_item_backend,
        emit_process_line,
        execute_slot_task_with_oom_retry,
        extract_backend_overrides,
        is_fresh_render_status,
        is_status_valid_for_slot,
        mark_task_completed_status,
        mark_task_failed_status,
        read_task_failure_summary,
        norm_engine,
        pick_next_eligible_item,
        read_project_engine_statuses,
        read_project_task_statuses,
        read_queue_json_locked,
        reset_session_blocks_for_items,
        resolve_srv_obj as _resolve_srv_obj,
        save_persisted_max_parallel,
        set_queue_item_backend_override as _set_queue_item_backend_override_io,
        terminate_proc,
        write_queue_json_locked,
        write_starting_slot_status,
    )
try: from .server_queue_history import record_done_items
except ImportError:
    try: from work.ui.server_queue_history import record_done_items
    except ImportError: from server_queue_history import record_done_items


def ensure_engine_state(srv) -> None:
    if not hasattr(srv, "enabled_engines") or not isinstance(srv.enabled_engines, dict):
        srv.enabled_engines = {b: (b == "local") for b in ENGINES}
    for b in ENGINES:
        srv.enabled_engines.setdefault(b, b == "local")
    if not hasattr(srv, "max_parallel") or not isinstance(srv.max_parallel, dict):
        srv.max_parallel = {b: 1 for b in ENGINES}
    for b in ENGINES:
        srv.max_parallel[b] = clamp_parallel(b, srv.max_parallel.get(b, 1))
    if not hasattr(srv, "engine_slots") or not isinstance(srv.engine_slots, dict):
        srv.engine_slots = {b: {} for b in ENGINES}
    for b in ENGINES:
        if not isinstance(srv.engine_slots.get(b), dict):
            srv.engine_slots[b] = {}
    if not hasattr(srv, "engine_workers") or not isinstance(srv.engine_workers, dict):
        srv.engine_workers = {}
    for b in ENGINES:
        srv.engine_workers.setdefault(b, {
            "running": False, "draining": False, "active_process": None,
            "active_count": 0, "max_parallel": srv.max_parallel[b],
            "filename": None, "output_filename": None, "scene_id": None,
            "target_arg": None, "started_at": None, "worker_id": 0,
            "stop_requested": False, "project": None,
        })
    if not hasattr(srv, "_slot_seq"):
        srv._slot_seq = 0
    if not hasattr(srv, "queue_file_lock"):
        srv.queue_file_lock = threading.RLock()


def _sorted_active_slots(srv, b: str) -> list[dict]:
    return sorted(
        [s for s in srv.engine_slots[b].values() if s.get("running")],
        key=lambda s: (float(s.get("started_at") or 0), int(s.get("slot_seq") or 0)),
    )


def _sync_engine_worker_summary(srv, b: str, force_sync: bool = False) -> None:
    """Keeps legacy srv.engine_workers[b] summary synchronized with srv.engine_slots[b]."""
    active = _sorted_active_slots(srv, b)
    limit = clamp_parallel(b, srv.max_parallel.get(b, 1))
    enabled = bool(srv.enabled_engines.get(b, b == "local"))
    for i, slot in enumerate(active):
        slot["draining"] = (not enabled) or (i >= limit)
    w = srv.engine_workers[b]
    w["max_parallel"] = limit
    if active:
        lead = active[0]
        w.update({
            "running": True,
            "active_count": len(active),
            "draining": any(s.get("draining") for s in active),
            "filename": lead.get("filename"),
            "output_filename": lead.get("output_filename"),
            "scene_id": lead.get("scene_id"),
            "target_arg": lead.get("target_arg"),
            "started_at": lead.get("started_at"),
            "project": lead.get("project"),
            "active_process": lead.get("active_process"),
        })
    elif force_sync or srv.engine_slots[b] or not w.get("running"):
        w.update({
            "running": False, "active_count": 0, "draining": False,
            "filename": None, "output_filename": None, "scene_id": None,
            "target_arg": None, "started_at": None, "active_process": None,
        })


def _any_slots_running(srv) -> bool:
    return any(
        any(s.get("running") for s in srv.engine_slots[b].values()) or srv.engine_workers[b].get("running")
        for b in ENGINES
    )


def get_multi_engine_render_status(srv=None) -> dict:
    srv = _resolve_srv_obj(srv)
    ensure_engine_state(srv)
    with srv.render_lock:
        for b in ENGINES:
            _sync_engine_worker_summary(srv, b)
        st = dict(srv.render_status)
        st["history_count"] = len(srv.log_history)
        st["queue_worker_active"] = bool(getattr(srv, "queue_worker_active", False))
        enabled_copy = {b: bool(srv.enabled_engines.get(b, b == "local")) for b in ENGINES}
        max_p_copy = {b: clamp_parallel(b, srv.max_parallel.get(b, 1)) for b in ENGINES}
        workers_copy = {b: {k: v for k, v in srv.engine_workers[b].items() if k != "active_process"} for b in ENGINES}
        slots_copy = {
            b: [{k: v for k, v in s.items() if k != "active_process"} for s in _sorted_active_slots(srv, b)]
            for b in ENGINES
        }
        any_slot_running = any(slots_copy[b] or workers_copy[b].get("running") for b in ENGINES)
        if st["queue_worker_active"] or any_slot_running:
            st["running"] = True

    curr_proj = st.get("current_project") or next(
        (s.get("project") for b in ENGINES for s in slots_copy[b] if s.get("project")),
        None,
    )
    per_files = read_project_engine_statuses(srv, curr_proj)
    per_tasks = read_project_task_statuses(srv, curr_proj)
    leg_eng = srv.read_project_engine_status(curr_proj)
    if leg_eng and (leg_eng.get("state") not in ("rendering", "cancelling") or not is_fresh_render_status(leg_eng)):
        leg_eng = None
    if not per_files.get("local") and not any(v and v.get("state") == "rendering" and is_fresh_render_status(v) for v in per_files.values()) and not leg_eng and srv.PROJECTS_DIR.is_dir():
        for d in srv.PROJECTS_DIR.iterdir():
            if not d.is_dir():
                continue
            cands = read_project_engine_statuses(srv, d.name)
            if any(v and v.get("state") == "rendering" and is_fresh_render_status(v) for v in cands.values()):
                curr_proj, per_files = d.name, cands
                per_tasks = read_project_task_statuses(srv, curr_proj)
                leg_eng = srv.read_project_engine_status(curr_proj)
                if leg_eng and (leg_eng.get("state") not in ("rendering", "cancelling") or not is_fresh_render_status(leg_eng)):
                    leg_eng = None
                break

    engines_view: dict[str, dict] = {}
    any_file_rendering = False
    chosen_eng = leg_eng if isinstance(leg_eng, dict) else None
    for b in ENGINES:
        active_list, w_sum, f_st = slots_copy[b], workers_copy[b], per_files.get(b)
        file_ren = bool(isinstance(f_st, dict) and f_st.get("state") == "rendering" and is_fresh_render_status(f_st))
        active_tasks: list[dict] = []
        for slot in active_list:
            fn = str(slot.get("filename") or "")
            out_stem = clean_task_stem(slot.get("output_filename") or fn)
            slot_ts = slot.get("started_at")
            t_st = per_tasks.get((b, out_stem))
            if not is_status_valid_for_slot(t_st, slot_ts, out_stem):
                t_st = f_st if is_status_valid_for_slot(f_st, slot_ts, out_stem) else None
            if t_st is None and fn:
                t_st = {"state": "rendering", "backend": b, "filename": fn, "output_filename": out_stem, "phase": "starting", "raw_phase": "Initializing engine...", "status_text": "Initializing engine...", "pct": 0.0 if b == "local" else None, "started_at": slot_ts}
            active_tasks.append({"slot_id": slot.get("slot_id"), "backend": b, "filename": fn, "output_filename": out_stem, "started_at": slot_ts or (t_st.get("started_at") if isinstance(t_st, dict) else None), "draining": bool(slot.get("draining")), "engine_status": t_st})
        if not active_tasks and (w_sum.get("running") or (file_ren and not st["queue_worker_active"])):
            fn = str(w_sum.get("filename") or (f_st.get("filename") if file_ren else "") or "")
            out_stem = clean_task_stem(w_sum.get("output_filename") or fn)
            w_ts = w_sum.get("started_at")
            t_st = per_tasks.get((b, out_stem))
            if not is_status_valid_for_slot(t_st, w_ts, out_stem):
                t_st = f_st if (file_ren and is_status_valid_for_slot(f_st, w_ts, out_stem)) else None
            active_tasks.append({"slot_id": f"{b}#0", "backend": b, "filename": fn, "output_filename": out_stem, "started_at": w_ts or (t_st.get("started_at") if isinstance(t_st, dict) else None), "draining": bool(w_sum.get("draining")), "engine_status": t_st})
        is_ren = bool(active_tasks)
        if is_ren:
            any_file_rendering = True
            lead_st = active_tasks[0].get("engine_status")
            if isinstance(lead_st, dict) and (chosen_eng is None or chosen_eng.get("state") != "rendering"):
                chosen_eng = lead_st
        lead_task = active_tasks[0] if active_tasks else {}
        engines_view[b] = {
            "enabled": enabled_copy[b], "max_parallel": max_p_copy[b], "active_count": len(active_tasks), "running": is_ren,
            "draining": bool(active_tasks and any(t.get("draining") for t in active_tasks)),
            "filename": lead_task.get("filename"), "started_at": lead_task.get("started_at"),
            "engine_status": lead_task.get("engine_status") if is_ren else (None if st.get("running") else f_st),
            "active_tasks": active_tasks,
        }

    st["current_project"] = curr_proj
    st["engine_status"] = chosen_eng
    if any_file_rendering or (isinstance(chosen_eng, dict) and chosen_eng.get("state") == "rendering" and is_fresh_render_status(chosen_eng)):
        st["running"] = True
    st["enabled_engines"] = enabled_copy
    st["max_parallel"] = max_p_copy
    st["engines"] = engines_view
    st["stitch_in_progress"] = bool(getattr(srv, "stitch_in_progress", False))
    st["stitch_progress"] = getattr(srv, "stitch_progress", None)
    return st


def active_rendering_map(srv, slug: str, legacy_eng: dict | None = None) -> tuple[bool, dict[str, dict]]:
    """Returns (is_any_engine_active, {filename_or_stem: engine_status_dict}) across all active tasks for `slug`."""
    ensure_engine_state(srv)
    per_eng = read_project_engine_statuses(srv, slug)
    per_tasks = read_project_task_statuses(srv, slug)
    with srv.render_lock:
        curr_proj = srv.render_status.get("current_project")
        q_active_raw = bool(getattr(srv, "queue_worker_active", False))
        slots_for_slug = [
            (b, dict(s))
            for b in ENGINES
            for s in _sorted_active_slots(srv, b)
            if s.get("project") in (None, slug)
        ]
        legacy_w_for_slug = [
            (b, dict(srv.engine_workers[b]))
            for b in ENGINES
            if not srv.engine_slots[b] and srv.engine_workers[b].get("running") and srv.engine_workers[b].get("project") in (None, slug)
        ]
        proc_active = bool(srv.active_process is not None and srv.active_process.poll() is None and curr_proj in (None, slug))
        q_active = bool(q_active_raw and curr_proj in (None, slug))

    active_map: dict[str, dict] = {}
    running_stems = {clean_task_stem(s.get("output_filename") or s.get("filename")) for _, s in (slots_for_slug + legacy_w_for_slug)}
    for (b, t_stem), st in per_tasks.items():
        if isinstance(st, dict) and st.get("state") == "rendering" and is_fresh_render_status(st):
            if (not q_active) or (t_stem in running_stems):
                fn = str(st.get("filename") or f"{t_stem}.mp4")
                active_map[fn] = st
                active_map[Path(fn).stem] = st
    for b, st in per_eng.items():
        if isinstance(st, dict) and st.get("state") == "rendering" and is_fresh_render_status(st):
            fn = str(st.get("filename") or "")
            if fn and ((not q_active) or (clean_task_stem(fn) in running_stems)) and fn not in active_map:
                active_map[fn] = st
                active_map[Path(fn).stem] = st
    if isinstance(legacy_eng, dict) and legacy_eng.get("state") == "rendering" and is_fresh_render_status(legacy_eng):
        fn = str(legacy_eng.get("filename") or "")
        if fn and ((not q_active) or (clean_task_stem(fn) in running_stems)) and fn not in active_map:
            active_map[fn] = legacy_eng
            active_map[Path(fn).stem] = legacy_eng
    for b, slot in slots_for_slug + legacy_w_for_slug:
        fn = str(slot.get("filename") or "")
        if fn and fn not in active_map:
            st_stub = {"state": "rendering", "backend": b, "filename": fn, "started_at": slot.get("started_at")}
            active_map[fn] = st_stub
            active_map[Path(fn).stem] = st_stub
    is_any = bool(proc_active or q_active or slots_for_slug or legacy_w_for_slug or active_map)
    return is_any, active_map


def kill_engine_render(srv, target_slug: str | None = None, backend: str | None = None, filename: str | None = None) -> None:
    ensure_engine_state(srv)
    target_b = norm_engine(backend) if backend else None
    target_stem = clean_task_stem(filename) if filename else ""
    procs_to_kill: list[subprocess.Popen] = []
    killed_stems: list[tuple[str, str]] = []

    with srv.render_lock:
        active_proj = srv.render_status.get("current_project") or target_slug
        if target_stem and target_b is None:
            for b in ENGINES:
                if any(clean_task_stem(s.get("output_filename") or s.get("filename")) == target_stem for s in srv.engine_slots[b].values()) or clean_task_stem(srv.engine_workers[b].get("output_filename") or srv.engine_workers[b].get("filename")) == target_stem:
                    target_b = b
                    break
        targets = (target_b,) if target_b else ENGINES
        if target_b is None and not target_stem:
            srv.queue_worker_active = False
            srv.current_worker_id += 1
            srv.render_job_queue.clear()
            if srv.active_process is not None:
                procs_to_kill.append(srv.active_process)
                srv.active_process = None
        for b in targets:
            for sid_k, slot in list(srv.engine_slots[b].items()):
                slot_stem = clean_task_stem(slot.get("output_filename") or slot.get("filename"))
                if target_stem and slot_stem != target_stem:
                    continue
                active_proj = active_proj or slot.get("project")
                if slot_stem:
                    killed_stems.append((b, slot_stem))
                slot["stop_requested"] = True
                slot["worker_id"] = int(slot.get("worker_id") or 0) + 1
                slot["running"] = False
                slot["draining"] = False
                slot["filename"] = None
                slot["started_at"] = None
                p = slot.get("active_process")
                slot["active_process"] = None
                if p is not None and p not in procs_to_kill:
                    procs_to_kill.append(p)
                if srv.active_process is p:
                    srv.active_process = None
            if not target_stem:
                w = srv.engine_workers[b]
                active_proj = active_proj or w.get("project")
                w["stop_requested"] = True
                w["worker_id"] += 1
                w["running"] = False
                p = w.get("active_process")
                w["active_process"] = None
                if p is not None and p not in procs_to_kill:
                    procs_to_kill.append(p)
                if srv.active_process is p:
                    srv.active_process = None
            _sync_engine_worker_summary(srv, b, force_sync=True)

    if target_stem and target_b and not any(st == target_stem for _, st in killed_stems):
        killed_stems.append((target_b, target_stem))

    live_procs = [p for p in procs_to_kill if p.poll() is None]
    cancel_files: list[Path] = []
    if active_proj and live_procs:
        for jobs_sub in ("work/jobs_v3", "work/jobs"):
            jdir = srv.PROJECTS_DIR / active_proj / jobs_sub
            if not jdir.is_dir():
                continue
            names: list[str] = []
            if target_stem and target_b:
                names.append(f"cancel_request_{target_b}__{target_stem}.json")
            else:
                names.extend([f"cancel_request_{b}.json" for b in targets] + [f"cancel_request_{b}__{s}.json" for b, s in killed_stems])
                if target_b is None:
                    names.append("cancel_request.json")
            for nm in dict.fromkeys(names):
                cf = jdir / nm
                try:
                    cf.write_text(json.dumps({"requested_at": time.time(), "reason": "STOPPED BY USER", "backend": target_b or "all"}), encoding="utf-8")
                    cancel_files.append(cf)
                except Exception:
                    pass

    if procs_to_kill:
        lbl = f"{ENGINE_LABELS[target_b]} ({target_stem})" if (target_b and target_stem) else (ENGINE_LABELS[target_b] if target_b else "all active")
        srv.broadcast_log(f"[Studio UI] Requesting graceful {lbl} stop & terminating process tree...")
        if cancel_files:
            dl = time.time() + 1.5
            while time.time() < dl and any(p.poll() is None for p in procs_to_kill):
                time.sleep(0.05)
        for p in procs_to_kill:
            terminate_proc(srv, p)
        for cf in cancel_files:
            try:
                if cf.exists():
                    cf.unlink()
            except Exception:
                pass

    with srv.render_lock:
        still_running = _any_slots_running(srv)
        if target_b and not target_stem and still_running:
            srv.enabled_engines[target_b] = False
        if not still_running or (target_b is None and not target_stem):
            srv.queue_worker_active = False
            srv.render_status["running"] = False
            srv.render_status["exit_code"] = -1
            srv._session_rendered_any = False
            srv._session_failed_code = 0
            srv._session_failed_engines = set()
        still_active_stems = {clean_task_stem(s.get("output_filename") or s.get("filename")) for b in ENGINES for s in _sorted_active_slots(srv, b)}

    if active_proj:
        cleanup_after_kill(srv, active_proj, targets, target_b, target_stem, killed_stems, still_running, still_active_stems)
    lbl_done = f"{ENGINE_LABELS[target_b]} ({target_stem or 'engine'})" if target_b else "Render process"
    srv.broadcast_log(f"[Studio UI] {lbl_done} stopped.")


def _call_execute(srv, cmd_args: list[str], slug: str, backend: str, slot_id: str | None = None, target_filename: str | None = None) -> int:
    try:
        from work.ui import server_workers as sw
    except ImportError:
        try: from ui import server_workers as sw
        except ImportError: import server_workers as sw
    fn = getattr(srv, "execute_single_render", sw.execute_single_render)
    if fn is sw.execute_single_render:
        return fn(cmd_args, slug, engine_tag=backend, slot_id=slot_id, target_filename=target_filename)
    return fn(cmd_args, slug)


def _sw_mod():
    try:
        from work.ui import server_workers as sw
        return sw
    except ImportError:
        try: from ui import server_workers as sw; return sw
        except ImportError: import server_workers as sw; return sw


def _schedule_stitch(srv, config_path: str, slug: str) -> None:
    sw = _sw_mod()
    with sw.stitch_lock:
        if sw.stitch_in_progress or getattr(sw, "_stitch_spawning", False):
            srv.stitch_pending = True
            return
        sw._stitch_spawning = True
    def _go():
        try:
            srv.run_stitch_worker(config_path, slug)
        finally:
            with sw.stitch_lock:
                sw._stitch_spawning = False
    threading.Thread(target=_go, daemon=True).start()


def _wake_ready_engines(srv, project_slug: str, session_id: int, config_path: str, items: list[dict], enabled_now: dict, failed_engs: set) -> bool:
    woke = False
    for b in ENGINES:
        if enabled_now.get(b, True) and b not in failed_engs:
            avail = count_eligible_items(items, b, srv)
            if avail > 0 and _fill_engine_slots(srv, project_slug, b, session_id, config_path, avail_hint=avail) > 0:
                woke = True
    return woke


def _coordinator_maybe_finish(srv, project_slug: str, session_id: int, config_path: str, queue_file: Path) -> None:
    sw = _sw_mod()
    while getattr(srv, "stitch_in_progress", False) or getattr(sw, "_stitch_spawning", False):
        time.sleep(0.1)
    with srv.render_lock:
        if getattr(srv, "_spawning_workers", False) or srv.current_worker_id != session_id or not getattr(srv, "queue_worker_active", False):
            return
        if _any_slots_running(srv):
            return
        enabled_now = dict(srv.enabled_engines)
        failed_engs = set(getattr(srv, "_session_failed_engines", set()))
        rendered_any = bool(getattr(srv, "_session_rendered_any", False))
        had_failure = bool(getattr(srv, "_session_failed_code", 0))

    if _wake_ready_engines(srv, project_slug, session_id, config_path, read_queue_json_locked(srv, queue_file), enabled_now, failed_engs):
        return

    if rendered_any:
        srv.broadcast_log("[Studio UI Queue] All enabled queued passes completed! Restitching master video...")
        srv.run_stitch_worker(config_path, project_slug)
        srv.broadcast_log("[Studio UI Queue] Queue finished. Master video updated.")

    with srv.render_lock:
        if srv.current_worker_id != session_id or not getattr(srv, "queue_worker_active", False):
            return
        if _any_slots_running(srv):
            return
        enabled_now = dict(srv.enabled_engines)
        failed_engs = set(getattr(srv, "_session_failed_engines", set()))

    if _wake_ready_engines(srv, project_slug, session_id, config_path, read_queue_json_locked(srv, queue_file), enabled_now, failed_engs):
        return

    with srv.render_lock:
        if srv.current_worker_id == session_id:
            if _any_slots_running(srv):
                return
            srv.queue_worker_active = False
            srv.render_status["running"] = False
            srv.render_status["exit_code"] = getattr(srv, "_session_failed_code", 0)
            srv._session_rendered_any = False
            srv._session_failed_code = 0
            srv._session_failed_engines = set()
    if not had_failure and not rendered_any:
        srv.broadcast_log("[Studio UI Queue] No passes currently queued for enabled engines.")
    srv.broadcast_log("[Studio UI Queue] Queue runner idle.")


def _spawn_engine_slot(srv, project_slug: str, backend: str, session_id: int, config_path: str) -> None:
    ensure_engine_state(srv)
    b = norm_engine(backend)
    with srv.render_lock:
        srv._slot_seq = int(getattr(srv, "_slot_seq", 0)) + 1
        seq = srv._slot_seq
        slot_id = f"{b}#{seq}"
        my_slot_id = int(srv.engine_workers[b].get("worker_id") or 0) + 1
        srv.engine_workers[b]["worker_id"] = my_slot_id
        srv.engine_workers[b]["stop_requested"] = False
        slot = {
            "slot_id": slot_id, "slot_seq": seq, "backend": b,
            "running": True, "draining": False, "stop_requested": False,
            "worker_id": my_slot_id, "project": project_slug,
            "filename": None, "output_filename": None, "scene_id": None,
            "target_arg": None, "started_at": time.time(), "active_process": None,
        }
        srv.engine_slots[b][slot_id] = slot
        _sync_engine_worker_summary(srv, b)

    def _loop():
        proj_dir = srv.PROJECTS_DIR / project_slug
        queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
        try:
            while getattr(srv, "queue_worker_active", False) and srv.current_worker_id == session_id:
                with srv.render_lock:
                    paused = not srv.enabled_engines.get(b, True)
                    eng_failed = b in (getattr(srv, "_session_failed_engines", None) or set())
                    limit_now = clamp_parallel(b, srv.max_parallel.get(b, 1))
                    other_running = sum(1 for k, s in srv.engine_slots[b].items() if k != slot_id and s.get("running"))
                    over_limit = other_running >= limit_now
                    slot_abort = (slot["worker_id"] != my_slot_id or slot["stop_requested"])
                    local_busy = (b == "local" and bool(getattr(srv, "local_adhoc_busy", False)))
                    if paused or over_limit or eng_failed or slot_abort or local_busy:
                        slot["running"] = False
                        slot["draining"] = False
                if paused:
                    srv.broadcast_log(f"[Studio UI Queue] [{ENGINE_LABELS[b]}] Paused after current item (unchecked).")
                    break
                if over_limit:
                    srv.broadcast_log(f"[Studio UI Queue] [{ENGINE_LABELS[b]}] Slot drained (max parallel={limit_now}).")
                    break
                if eng_failed or slot_abort or local_busy:
                    break

                next_idx = next_task = None
                with srv.queue_file_lock:
                    items = read_queue_json_locked(srv, queue_file)
                    if not items:
                        items = srv.compute_fast_project_queue(project_slug)
                    next_idx, next_task = pick_next_eligible_item(items, b, srv)
                    if next_task is None or next_idx is None:
                        break
                    out_fn = next_task.get("output_filename", "")
                    target_arg = next_task.get("target_arg") or srv.derive_target_arg(out_fn) or next_task.get("scene_id")
                    next_task["target_arg"] = target_arg
                    next_task["scene_id"] = next_task.get("scene_id") or srv.derive_scene_id(out_fn)
                    items[next_idx]["status"] = "rendering"
                    if hasattr(srv, "normalize_queue_units"):
                        try: srv.normalize_queue_units(items, srv)
                        except Exception: pass
                    write_queue_json_locked(srv, queue_file, items)

                with srv.render_lock:
                    slot["filename"] = f"{out_fn}.mp4" if out_fn else None
                    slot["output_filename"] = out_fn
                    slot["scene_id"] = next_task.get("scene_id")
                    slot["target_arg"] = target_arg
                    slot["started_at"] = round(time.time(), 3)
                    _sync_engine_worker_summary(srv, b)

                ovr = str(next_task.get("backend_override") or "").strip().lower()
                srv.broadcast_log(f"[Studio UI Queue] [{ENGINE_LABELS[b]}] >>> Starting render ({slot_id}): {out_fn} ({target_arg})" + (f" [override={ovr}]" if ovr else ""))
                write_starting_slot_status(proj_dir, b, out_fn, slot["started_at"], reason=str(next_task.get("reason") or ""), model=str(next_task.get("model_type") or ""), frames=int(next_task.get("video_length") or 0), resolution=str(next_task.get("resolution") or ""))
                t0 = time.time()
                cmd_args = [config_path, target_arg, "--skip-anchors", "--no-stitch", "--no-queue-sync"]
                if ovr in ENGINES:
                    cmd_args.extend(["--backend", ovr])
                code = execute_slot_task_with_oom_retry(srv, _call_execute, cmd_args, project_slug, b, slot_id, out_fn, proj_dir, session_id, slot, my_slot_id, next_task.get("scene_id"))

                t_elapsed = round(time.time() - t0, 1)
                t_done = round(time.time(), 3)
                if not getattr(srv, "queue_worker_active", False) or srv.current_worker_id != session_id or slot["worker_id"] != my_slot_id:
                    break

                with srv.queue_file_lock:
                    cur_items = read_queue_json_locked(srv, queue_file) or items
                    for it in cur_items:
                        if it.get("output_filename") == out_fn:
                            if code == 0:
                                it.update({"status": "completed", "needs_render": False, "reason": "COMPLETED", "completed_at": t_done, "elapsed": t_elapsed})
                                it.pop("user_skipped_hash", None)
                                it.pop("error", None)
                                try: record_done_items(srv, proj_dir, [it], only_completed=True)
                                except Exception: pass
                            else:
                                fail_reason, fail_err = read_task_failure_summary(proj_dir, b, out_fn, code)
                                it.update({"status": "failed", "reason": fail_reason, "error": fail_err})
                            break
                    if code == 0 and ("_Pass1_Left" in out_fn or next_task.get("pass") == 1):
                        sid = next_task.get("scene_id") or srv.derive_scene_id(out_fn)
                        cascaded: list[dict] = []
                        for it in cur_items:
                            if (it.get("scene_id") == sid or srv.derive_scene_id(it.get("output_filename", "")) == sid) and it.get("pass") == 2 and it.get("status") != "rendering":
                                it.update({"status": "queued", "needs_render": True, "reason": "PASS 1 NEWER ON DISK"})
                                it.pop("user_skipped_hash", None)
                                it.pop("user_skipped_anchor_mtime", None)
                                cascaded.append(it)
                        if cascaded:
                            reset_session_blocks_for_items(srv, cascaded, clear_failed=False)
                    if hasattr(srv, "normalize_queue_units"):
                        try: srv.normalize_queue_units(cur_items, srv)
                        except Exception: pass
                    write_queue_json_locked(srv, queue_file, cur_items)

                if code == 0:
                    mark_task_completed_status(proj_dir, b, out_fn, t_elapsed)
                    with srv.render_lock:
                        srv._session_rendered_any = True
                        if hasattr(srv, "_session_completed_hashes"):
                            srv._session_completed_hashes.add((out_fn, str(next_task.get("task_hash") or "")))
                    wake_enabled_engine_workers(srv, project_slug)
                    if "_Pass1_Left" not in out_fn:
                        _schedule_stitch(srv, config_path, project_slug)
                else:
                    with srv.render_lock:
                        srv._session_failed_code = code
                        fe = getattr(srv, "_session_failed_engines", None)
                        if not isinstance(fe, set):
                            srv._session_failed_engines = set()
                        srv._session_failed_engines.add(b)
                    mark_task_failed_status(srv, proj_dir, b, out_fn, code)
                    srv.broadcast_log(f"[Studio UI Error] [{ENGINE_LABELS[b]}] Render failed for {out_fn} (exit code {code}). Engine paused.")
                    break
                time.sleep(0.15)
        except Exception as exc:
            srv.broadcast_log(f"[Studio UI Queue Worker Exception] [{ENGINE_LABELS[b]}] {exc}")
            with srv.render_lock:
                srv._session_failed_code = -1
        finally:
            with srv.render_lock:
                if slot["worker_id"] == my_slot_id:
                    slot["running"] = False
                    slot["draining"] = False
                    slot["filename"] = None
                    slot["started_at"] = None
                    slot["active_process"] = None
                srv.engine_slots[b].pop(slot_id, None)
                _sync_engine_worker_summary(srv, b, force_sync=True)
                should_wake = bool(getattr(srv, "queue_worker_active", False) and srv.current_worker_id == session_id)
            if should_wake:
                wake_enabled_engine_workers(srv, project_slug)
            _coordinator_maybe_finish(srv, project_slug, session_id, config_path, queue_file)

    threading.Thread(target=_loop, daemon=True).start()


def _fill_engine_slots(srv, project_slug: str, backend: str, session_id: int, config_path: str, avail_hint: int | None = None) -> int:
    ensure_engine_state(srv)
    b = norm_engine(backend)
    with srv.render_lock:
        if not getattr(srv, "queue_worker_active", False) or srv.current_worker_id != session_id:
            return 0
        if not srv.enabled_engines.get(b, True):
            return 0
        if b == "local" and getattr(srv, "local_adhoc_busy", False):
            return 0
        if b in (getattr(srv, "_session_failed_engines", None) or set()):
            return 0
        cur_running = sum(1 for s in srv.engine_slots[b].values() if s.get("running") and s.get("project") == project_slug)
        limit = clamp_parallel(b, srv.max_parallel.get(b, 1))
        needed = max(0, limit - cur_running)
    if needed <= 0:
        return 0
    if avail_hint is None:
        q_file = srv.PROJECTS_DIR / project_slug / "work" / "jobs_v3" / "v3_queue.json"
        avail_hint = count_eligible_items(read_queue_json_locked(srv, q_file), b, srv)
    to_spawn = min(needed, max(0, int(avail_hint)))
    for _ in range(to_spawn):
        _spawn_engine_slot(srv, project_slug, b, session_id, config_path)
    return to_spawn


def wake_enabled_engine_workers(srv, project_slug: str | None = None) -> None:
    if project_slug is None and isinstance(srv, str):
        srv, project_slug = _resolve_srv_obj(None), srv
    else:
        srv = _resolve_srv_obj(srv)
    if not project_slug:
        return
    ensure_engine_state(srv)
    with srv.render_lock:
        if not getattr(srv, "queue_worker_active", False):
            return
        session_id = srv.current_worker_id
        enabled_copy = dict(srv.enabled_engines)
        failed_engs = set(getattr(srv, "_session_failed_engines", set()))
    proj_dir = srv.PROJECTS_DIR / project_slug
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        return
    config_path = str(jsons[-1])
    items = read_queue_json_locked(srv, proj_dir / "work" / "jobs_v3" / "v3_queue.json")
    for b in ENGINES:
        if enabled_copy.get(b, True) and b not in failed_engs:
            avail = count_eligible_items(items, b, srv)
            if avail > 0:
                _fill_engine_slots(srv, project_slug, b, session_id, config_path, avail_hint=avail)


def run_multi_engine_queue_worker(srv, project_slug: str, force_restart: bool = False, enabled_engines: dict | None = None, max_parallel: dict | None = None) -> None:
    ensure_engine_state(srv)
    with srv.render_lock:
        if srv.active_process is not None and srv.active_process.poll() is not None:
            srv.active_process = None
        failed_ref = getattr(srv, "_session_failed_engines", None)
        if isinstance(enabled_engines, dict):
            for b in ENGINES:
                if b in enabled_engines:
                    val = bool(enabled_engines[b])
                    srv.enabled_engines[b] = val
                    if val:
                        srv.engine_workers[b]["stop_requested"] = False
                        if isinstance(failed_ref, set):
                            failed_ref.discard(b)
        if isinstance(max_parallel, dict):
            for b in ENGINES:
                if b in max_parallel:
                    srv.max_parallel[b] = clamp_parallel(b, max_parallel[b])
        for b in ENGINES:
            _sync_engine_worker_summary(srv, b)
        if getattr(srv, "queue_worker_active", False):
            curr_proj = srv.render_status.get("current_project")
            if curr_proj and curr_proj != project_slug:
                raise RuntimeError(f"Another project ('{curr_proj}') is already rendering")
            if not force_restart:
                pass_wake = True
            else:
                pass_wake = False
                srv.queue_worker_active = False
        else:
            pass_wake = False
    if force_restart:
        kill_engine_render(srv, target_slug=project_slug, backend=None)
    if pass_wake:
        srv.broadcast_log("[Studio UI Queue] Queue already active; engaging enabled engines.")
        wake_enabled_engine_workers(srv, project_slug)
        return

    with srv.render_lock:
        srv.current_worker_id += 1
        session_id = srv.current_worker_id
        srv.queue_worker_active = True
        srv.render_status["running"] = True
        srv.render_status["exit_code"] = None
        srv.render_status["start_time"] = time.time()
        srv.render_status["current_project"] = project_slug
        srv._run_log_seq_floor = int(getattr(srv, "log_seq_counter", 0) or 0)
        srv._session_rendered_any = False
        srv._session_failed_code = 0
        srv._session_failed_engines = set()
        srv._session_completed_hashes = set()
        for b in ENGINES:
            srv.engine_workers[b]["stop_requested"] = False
        enabled_copy = dict(srv.enabled_engines)

    proj_dir = srv.PROJECTS_DIR / project_slug
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        srv.broadcast_log("[Studio UI Error] No project config JSON found.")
        with srv.render_lock:
            if srv.current_worker_id == session_id:
                srv.queue_worker_active = False
                srv.render_status["running"] = False
        return
    config_path = str(jsons[-1])
    q_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    items = read_queue_json_locked(srv, q_file)
    if not items:
        items = srv.compute_fast_project_queue(project_slug)
    srv.broadcast_log("[Studio UI Queue] Multi-engine parallel queue started.")
    any_started = False
    with srv.render_lock:
        srv._spawning_workers = True
    try:
        for b in ENGINES:
            if enabled_copy.get(b, True):
                avail = count_eligible_items(items, b, srv)
                if avail > 0 and _fill_engine_slots(srv, project_slug, b, session_id, config_path, avail_hint=avail) > 0:
                    any_started = True
    finally:
        with srv.render_lock:
            srv._spawning_workers = False
    with srv.render_lock:
        all_done = not _any_slots_running(srv)
    if (not any_started) or all_done:
        _coordinator_maybe_finish(srv, project_slug, session_id, config_path, q_file)


def set_enabled_engines(srv, slug: str | None, enabled_map: dict | None = None, max_parallel: dict | None = None) -> dict:
    ensure_engine_state(srv)
    failed_ref = getattr(srv, "_session_failed_engines", None)
    with srv.render_lock:
        if isinstance(enabled_map, dict):
            for b in ENGINES:
                if b in enabled_map:
                    val = bool(enabled_map[b])
                    srv.enabled_engines[b] = val
                    if val:
                        srv.engine_workers[b]["stop_requested"] = False
                        if isinstance(failed_ref, set):
                            failed_ref.discard(b)
        if isinstance(max_parallel, dict):
            for b in ENGINES:
                if b in max_parallel:
                    srv.max_parallel[b] = clamp_parallel(b, max_parallel[b])
        for b in ENGINES:
            _sync_engine_worker_summary(srv, b)
        is_active = bool(getattr(srv, "queue_worker_active", False))
        active_slug = slug or srv.render_status.get("current_project")
    if isinstance(max_parallel, dict) and active_slug:
        p_dir = srv.PROJECTS_DIR / str(active_slug).strip()
        if p_dir.is_dir():
            save_persisted_max_parallel(srv.max_parallel, proj_dir=p_dir)
    if is_active and active_slug:
        wake_enabled_engine_workers(srv, active_slug)
    return get_multi_engine_render_status(srv)


def set_queue_item_backend_override(srv, slug: str, item_id: str, backend_override: str | None) -> list[dict]:
    ensure_engine_state(srv)
    items = _set_queue_item_backend_override_io(srv, slug, item_id, backend_override)
    wake_enabled_engine_workers(srv, slug)
    return items
