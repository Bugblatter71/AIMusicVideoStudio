"""Low-level multi-engine I/O helpers, queue locking, process termination, and status snapshots."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

try:
    from pipeline.media_utils import atomic_write_json, locked_file_op
except ImportError:
    try:
        from work.pipeline.media_utils import atomic_write_json, locked_file_op
    except ImportError:
        atomic_write_json = None
        locked_file_op = None

ENGINES: tuple[str, ...] = ("local", "fal", "minimax")
ENGINE_LABELS: dict[str, str] = {"local": "Local", "fal": "fal.ai", "minimax": "MiniMax"}
MAX_PARALLEL_BOUNDS: dict[str, tuple[int, int]] = {"local": (1, 1), "fal": (1, 40), "minimax": (1, 30)}


def norm_engine(raw: str | None) -> str:
    b = str(raw or "").strip().lower()
    if b in ("fal", "fal.ai", "cloud"):
        return "fal"
    if b in ("minimax", "minimax_api", "hailuo", "platform.minimax.io"):
        return "minimax"
    return "local"


def clamp_parallel(backend: str, raw_val) -> int:
    b = norm_engine(backend)
    lo, hi = MAX_PARALLEL_BOUNDS.get(b, (1, 1))
    try:
        v = int(raw_val)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def clean_task_stem(raw: str | None) -> str:
    s = Path(str(raw or "").strip()).stem if raw else ""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)[:96]


def effective_item_backend(item: dict | None) -> str:
    if not isinstance(item, dict):
        return "local"
    return norm_engine(item.get("backend_override") or item.get("backend") or "local")


def extract_backend_overrides(items: list[dict] | None) -> dict:
    overrides: dict = {}
    for itm in items or []:
        if not isinstance(itm, dict):
            continue
        raw = str(itm.get("backend_override") or "").strip().lower()
        if raw not in ENGINES:
            continue
        out_fn = itm.get("output_filename")
        if out_fn:
            overrides[out_fn] = raw
        sid = itm.get("scene_id")
        if sid:
            overrides[(str(sid).upper(), itm.get("pass"))] = raw
    return overrides


def write_queue_json_locked(srv, queue_file: Path, items: list[dict]) -> None:
    with srv.queue_file_lock:
        if atomic_write_json and locked_file_op:
            with locked_file_op(queue_file.with_suffix(".lock")):
                atomic_write_json(queue_file, items)
        else:
            queue_file.parent.mkdir(parents=True, exist_ok=True)
            queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")


def read_queue_json_locked(srv, queue_file: Path) -> list[dict]:
    with srv.queue_file_lock:
        if not queue_file.is_file():
            return []
        try:
            data = json.loads(queue_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _read_json_dict(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def read_project_engine_statuses(srv, slug: str | None) -> dict[str, dict | None]:
    res: dict[str, dict | None] = {b: None for b in ENGINES}
    if not slug:
        return res
    proj_dir = srv.PROJECTS_DIR / slug
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        jdir = proj_dir / jobs_sub
        if not jdir.is_dir():
            continue
        for b in ENGINES:
            if res[b] is None:
                res[b] = _read_json_dict(jdir / f"engine_status_{b}.json")
        leg = _read_json_dict(jdir / "engine_status.json")
        if leg is not None:
            b_tag = norm_engine(leg.get("backend"))
            if res[b_tag] is None:
                res[b_tag] = leg
    return res


def read_project_task_statuses(srv, slug: str | None) -> dict[tuple[str, str], dict]:
    """Reads per-task status snapshots engine_status_<backend>__<task_stem>.json."""
    res: dict[tuple[str, str], dict] = {}
    if not slug:
        return res
    proj_dir = srv.PROJECTS_DIR / slug
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        jdir = proj_dir / jobs_sub
        if not jdir.is_dir():
            continue
        for sf in jdir.glob("engine_status_*__*.json"):
            d = _read_json_dict(sf)
            if not d:
                continue
            stem_part = sf.stem[len("engine_status_"):]
            if "__" not in stem_part:
                continue
            raw_b, t_stem = stem_part.split("__", 1)
            b = norm_engine(d.get("backend") or raw_b)
            key = (b, t_stem)
            if key not in res:
                res[key] = d
    return res


def _iter_eligible_items(items: list[dict], backend: str, srv):
    target_b = norm_engine(backend)
    done_set = getattr(srv, "_session_completed_hashes", None) or set()
    by_sid_p1: dict[str, dict] = {}
    for x in items:
        if x.get("pass") == 1:
            sid_k = str(x.get("scene_id") or srv.derive_scene_id(x.get("output_filename", "")) or "").strip().upper()
            if sid_k:
                by_sid_p1[sid_k] = x

    for idx, itm in enumerate(items):
        if itm.get("status") != "queued" or not itm.get("needs_render"):
            continue
        if effective_item_backend(itm) != target_b:
            continue
        done_k = (itm.get("output_filename", ""), str(itm.get("task_hash") or ""))
        r_str = str(itm.get("reason") or "")
        if done_k[0] and done_k in done_set and "FORCED" not in r_str and not r_str.startswith("TARGETED"):
            continue
        if itm.get("pass") == 2:
            sid_k = str(itm.get("scene_id") or srv.derive_scene_id(itm.get("output_filename", "")) or "").strip().upper()
            p1 = by_sid_p1.get(sid_k)
            if p1 is not None and (p1.get("status") in ("queued", "rendering", "failed") or p1.get("needs_render")):
                continue
        yield idx, itm


def pick_next_eligible_item(items: list[dict], backend: str, srv) -> tuple[int | None, dict | None]:
    return next(_iter_eligible_items(items, backend, srv), (None, None))


def count_eligible_items(items: list[dict], backend: str, srv) -> int:
    return sum(1 for _ in _iter_eligible_items(items, backend, srv))


def _inject_backend_on_queue_tag(line: str, backend: str) -> str:
    if "[QUEUE_" not in line:
        return line
    q_idx = line.find("[QUEUE_")
    sp_idx = line.find(" ", q_idx)
    if sp_idx == -1:
        return line
    tag = line[q_idx:sp_idx]
    rest = line[sp_idx + 1:]
    try:
        obj = json.loads(rest)
        if isinstance(obj, dict) and not obj.get("backend"):
            obj["backend"] = backend
            return f"{tag} {json.dumps(obj)}"
    except Exception:
        pass
    return line[q_idx:]


def emit_process_line(srv, raw_clean: str, engine_tag: str | None = None) -> None:
    if "[QUEUE_" in raw_clean:
        q_idx = raw_clean.find("[QUEUE_")
        prefix = raw_clean[:q_idx].strip()
        if prefix:
            srv.broadcast_log(f"[{ENGINE_LABELS[norm_engine(engine_tag)]}] {prefix}" if engine_tag else prefix)
        srv.broadcast_log(_inject_backend_on_queue_tag(raw_clean[q_idx:], norm_engine(engine_tag)) if engine_tag else raw_clean[q_idx:])
    elif engine_tag and raw_clean and not raw_clean.startswith(("[Studio UI", "[Local]", "[fal.ai]", "[MiniMax]")):
        srv.broadcast_log(f"[{ENGINE_LABELS[norm_engine(engine_tag)]}] {raw_clean}")
    else:
        srv.broadcast_log(raw_clean)


def terminate_proc(srv, proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        pid = proc.pid
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        else:
            proc.terminate()
    except Exception as e:
        srv.broadcast_log(f"[Studio UI Warning] Process termination error: {e}")
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def parse_valid_engine(raw: str | None) -> str | None:
    """Strictly validates an engine identifier, returning 'local'|'fal'|'minimax' or None."""
    if not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val in ("local", "wgp", "wan2gp"):
        return "local"
    if val in ("fal", "fal.ai", "falai"):
        return "fal"
    if val in ("minimax", "minimax_api", "minimax-api"):
        return "minimax"
    return None


def is_fresh_render_status(st: dict | None, ttl: float = 900.0) -> bool:
    """Returns True if status payload looks non-stale (age <= ttl when timestamp present)."""
    if not isinstance(st, dict):
        return False
    ts = st.get("updated_at") or st.get("timestamp")
    if ts is None:
        return True
    try:
        return (time.time() - float(ts)) <= float(ttl)
    except Exception:
        return True


def _fal_cfg_helpers():
    try:
        from pipeline.fal_catalog import read_local_config, write_local_config
        return read_local_config, write_local_config
    except ImportError:
        from work.pipeline.fal_catalog import read_local_config, write_local_config
        return read_local_config, write_local_config


def load_persisted_max_parallel(proj_dir: Path | None = None) -> dict[str, int] | None:
    """Loads persisted per-engine max_parallel values from local_config.json if configured."""
    try:
        read_local_config, _ = _fal_cfg_helpers()
        cfg = read_local_config(proj_dir)
        qp = cfg.get("queue_max_parallel")
        if isinstance(qp, dict) and any(b in qp for b in ENGINES):
            return {b: clamp_parallel(b, qp.get(b, 1)) for b in ENGINES}
    except Exception:
        pass
    return None


def save_persisted_max_parallel(mp: dict[str, int], proj_dir: Path | None = None) -> None:
    """Persists per-engine max_parallel values into local_config.json."""
    try:
        _, write_local_config = _fal_cfg_helpers()
        clean = {b: clamp_parallel(b, mp.get(b, 1)) for b in ENGINES}
        write_local_config({"queue_max_parallel": clean}, root_dir=proj_dir)
    except Exception:
        pass


def cleanup_after_kill(srv, active_proj: str, targets: tuple[str, ...], target_b: str | None, target_stem: str, killed_stems: list[tuple[str, str]], still_running: bool, still_active_stems: set[str]) -> None:
    """Updates queue items and per-task/per-engine status JSONs after user stop/kill."""
    try:
        from work.ui import server_workers as _sw
    except ImportError:
        try: from ui import server_workers as _sw
        except ImportError: import server_workers as _sw
    try:
        p_dir = srv.PROJECTS_DIR / active_proj
        q_file = p_dir / "work" / "jobs_v3" / "v3_queue.json"
        if q_file.is_file():
            with srv.queue_file_lock:
                q_items = read_queue_json_locked(srv, q_file)
                for q_it in q_items:
                    if q_it.get("status") != "rendering":
                        continue
                    it_stem = clean_task_stem(q_it.get("output_filename") or q_it.get("filename"))
                    match = (it_stem == target_stem) if target_stem else ((target_b is None or effective_item_backend(q_it) == target_b) and (it_stem not in still_active_stems))
                    if match:
                        if target_stem:
                            q_it.update({"status": "skipped", "needs_render": False, "reason": "STOPPED BY USER"})
                        else:
                            q_it["status"] = "queued" if q_it.get("needs_render") else "skipped"
                            q_it["reason"] = "STOPPED BY USER"
                        _sw._restore_if_unlinked(p_dir, q_it.get("output_filename") or q_it.get("filename"), q_it.get("scene_id"))
                write_queue_json_locked(srv, q_file, q_items)
        now_ts = round(time.time(), 3)
        for jobs_sub in ("work/jobs_v3", "work/jobs"):
            jdir = p_dir / jobs_sub
            if not jdir.is_dir():
                continue
            cand_files = [jdir / f"engine_status_{b}__{s}.json" for b, s in killed_stems]
            if not target_stem:
                for b in targets:
                    cand_files.append(jdir / f"engine_status_{b}.json")
                    for pf in jdir.glob(f"engine_status_{b}__*.json"):
                        stem_p = pf.stem.split("__", 1)[-1]
                        if stem_p not in still_active_stems:
                            cand_files.append(pf)
            if (target_b is None and not target_stem) or not still_running:
                cand_files.append(jdir / "engine_status.json")
            seen_p: set[Path] = set()
            for sf in cand_files:
                if sf in seen_p or not sf.is_file():
                    continue
                seen_p.add(sf)
                try:
                    sd = json.loads(sf.read_text(encoding="utf-8"))
                    if target_stem or sd.get("state") == "rendering" or not sf.stem.startswith("engine_status_") or "__" not in sf.stem:
                        _sw._restore_if_unlinked(p_dir, sd.get("output_filename") or sd.get("filename"))
                        sd.update({"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user", "updated_at": now_ts})
                        sf.write_text(json.dumps(sd, indent=2), encoding="utf-8")
                except Exception:
                    pass
    except Exception:
        pass


def read_task_failure_summary(proj_dir: Path, b: str, out_fn: str, code: int) -> tuple[str, str]:
    """Reads failure reason & detail from engine status JSON before marking failed."""
    out_stem = clean_task_stem(out_fn)
    b = norm_engine(b)
    err_txt = ""
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        jdir = proj_dir / jobs_sub
        if not jdir.is_dir():
            continue
        cands = ((jdir / f"engine_status_{b}__{out_stem}.json",) if out_stem else ()) + (jdir / f"engine_status_{b}.json",)
        for sf in cands:
            if not sf.is_file():
                continue
            try:
                sd = json.loads(sf.read_text(encoding="utf-8"))
                if sf.name == f"engine_status_{b}.json" and out_stem and clean_task_stem(sd.get("output_filename") or sd.get("filename")) not in ("", out_stem):
                    continue
                ev = sd.get("error")
                raw = " ".join(str(x).strip() for x in ev if str(x).strip()) if isinstance(ev, list) else str(ev or "").strip()
                if raw:
                    err_txt = raw.splitlines()[0][:240]
                    break
            except Exception:
                pass
        if err_txt:
            break
    low = err_txt.lower()
    if any(m in low for m in _OOM_MARKERS):
        return "CUDA OOM", err_txt
    return f"FAILED (code {code})", (err_txt or f"Exited with code {code}")


def mark_task_failed_status(srv, proj_dir: Path, b: str, out_fn: str, code: int) -> None:
    """Clears leftover 'rendering' status on disk when a worker exits non-zero cleanly/abruptly."""
    now_ts = round(time.time(), 3)
    out_stem = clean_task_stem(out_fn)
    if not out_stem:
        return
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        jdir = proj_dir / jobs_sub
        if not jdir.is_dir():
            continue
        for sf in (jdir / f"engine_status_{b}__{out_stem}.json", jdir / f"engine_status_{b}.json", jdir / "engine_status.json"):
            if not sf.is_file():
                continue
            try:
                sd = json.loads(sf.read_text(encoding="utf-8"))
                if sd.get("state") == "rendering" and clean_task_stem(sd.get("output_filename") or sd.get("filename")) in ("", out_stem):
                    sd.update({"state": "failed", "phase": "failed", "raw_phase": f"Exited with code {code}", "updated_at": now_ts})
                    sf.write_text(json.dumps(sd, indent=2), encoding="utf-8")
            except Exception:
                pass


def resolve_srv_obj(srv):
    if srv is not None and hasattr(srv, "render_lock"):
        return srv
    mod = sys.modules.get("work.ui.server") or sys.modules.get("ui.server") or sys.modules.get("server")
    if mod is not None:
        return mod
    try:
        from work.ui import server
        return server
    except ImportError:
        import server
        return server


def set_queue_item_backend_override(srv, slug: str, item_id: str, backend_override: str | None) -> list[dict]:
    """Sets or clears a queue pass's per-item backend_override and wakes engines if active."""
    raw_b = str(backend_override or "").strip().lower()
    if raw_b and raw_b not in ENGINES:
        raise ValueError(f"Unsupported engine '{backend_override}'. Must be '', 'local', 'fal', or 'minimax'.")
    proj_dir = srv.PROJECTS_DIR / slug
    q_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    with srv.queue_file_lock:
        if not q_file.is_file():
            srv.compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, q_file)
        clean_id = str(item_id).replace(".mp4", "")
        found = next((x for x in items if x.get("output_filename") in (item_id, clean_id) or x.get("filename") in (item_id, clean_id) or x.get("id") in (item_id, clean_id)), None)
        if not found:
            raise KeyError(f"Queue item '{item_id}' not found")
        if found.get("status") == "rendering":
            raise RuntimeError("Cannot change engine while pass is actively rendering")
        if raw_b:
            found["backend_override"] = raw_b
        else:
            found.pop("backend_override", None)
        if hasattr(srv, "_session_completed_hashes") and isinstance(srv._session_completed_hashes, set):
            fn_rm = str(found.get("output_filename") or "")
            srv._session_completed_hashes = {k for k in srv._session_completed_hashes if k[0] != fn_rm}
        write_queue_json_locked(srv, q_file, items)
        items = srv.compute_fast_project_queue(slug, force_recheck=False)
    return items


def reset_session_blocks_for_items(srv, items: list[dict] | None, *, clear_failed: bool = True) -> None:
    """Clears completed-hash deduplication and failed-engine pauses for newly queued items."""
    if srv is None or not items:
        return
    done_set = getattr(srv, "_session_completed_hashes", None)
    failed_set = getattr(srv, "_session_failed_engines", None)
    for it in items:
        fn = str(it.get("output_filename") or "")
        if fn and isinstance(done_set, set):
            for k in [x for x in done_set if x[0] == fn]:
                done_set.discard(k)
        if clear_failed and isinstance(failed_set, set):
            failed_set.discard(effective_item_backend(it))


def requeue_failed_items(srv, slug: str, enabled_map: dict | None = None) -> list[dict]:
    """Resets failed queue entries on enabled engines back to queued before retrying."""
    q_file = srv.PROJECTS_DIR / slug / "work" / "jobs_v3" / "v3_queue.json"
    if not q_file.is_file():
        return []
    with srv.queue_file_lock:
        items = read_queue_json_locked(srv, q_file)
        retried: list[dict] = []
        for it in items:
            if it.get("status") != "failed":
                continue
            b = effective_item_backend(it)
            if isinstance(enabled_map, dict) and not enabled_map.get(b, True):
                continue
            it.update({"status": "queued", "needs_render": True, "reason": "RETRY"})
            retried.append(it)
        if retried:
            reset_session_blocks_for_items(srv, retried, clear_failed=True)
            write_queue_json_locked(srv, q_file, items)
        return items


_OOM_MARKERS = ("out of memory", "outofmemoryerror", "cudaerrormemoryallocation", "allocation on device")


def is_oom_failure(srv, proj_dir: Path, backend: str, out_fn: str, attempt_t0: float = 0.0, min_log_idx: int = 0) -> bool:
    """Checks task/engine status files (and recent log tail) for a CUDA out-of-memory error."""
    b = norm_engine(backend)
    stem = clean_task_stem(out_fn)
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        jdir = proj_dir / jobs_sub
        if not jdir.is_dir():
            continue
        for sf in ((jdir / f"engine_status_{b}__{stem}.json",) if stem else ()) + (jdir / f"engine_status_{b}.json",):
            if not sf.is_file():
                continue
            try:
                sd = json.loads(sf.read_text(encoding="utf-8"))
                if sf.name == f"engine_status_{b}.json" and stem:
                    if clean_task_stem(sd.get("output_filename") or sd.get("filename")) not in ("", stem):
                        continue
                if attempt_t0 > 0 and float(sd.get("updated_at") or 0) < (attempt_t0 - 2.0):
                    continue
                if sd.get("state") == "cancelled":
                    return False
                err_val = sd.get("error")
                err_txt = " ".join(str(x) for x in err_val) if isinstance(err_val, list) else str(err_val or "")
                blob = f"{err_txt} {sd.get('raw_phase') or ''} {sd.get('status_text') or ''}".lower()
                if any(m in blob for m in _OOM_MARKERS):
                    return True
            except Exception:
                pass
    logs = list(getattr(srv, "log_history", None) or []) if srv is not None else []
    tail = logs[max(0, min_log_idx):] if min_log_idx > 0 else logs[-30:]
    for entry in tail:
        txt = str(entry.get("text") if isinstance(entry, dict) else entry or "").lower()
        if (not stem or stem.lower() in txt or "cuda" in txt or "torch" in txt) and any(m in txt for m in _OOM_MARKERS):
            return True
    return False


def execute_slot_task_with_oom_retry(
    srv, exec_fn, cmd_args: list[str], slug: str, backend: str, slot_id: str,
    out_fn: str, proj_dir: Path, session_id: int, slot: dict, my_slot_id: int, scene_id: str | None = None,
) -> int:
    """Runs a slot task once, and for Local CUDA OOM crashes waits briefly and retries once in a fresh subprocess."""
    try:
        from work.ui import server_workers as sw
    except ImportError:
        try: from ui import server_workers as sw
        except ImportError: import server_workers as sw
    max_retries = max(0, int(getattr(srv, "local_oom_retries", 1))) if backend == "local" else 0
    delay_s = max(0.0, float(getattr(srv, "oom_retry_delay_s", 3.0)))
    code = -1
    for attempt in range(max_retries + 1):
        attempt_t0 = time.time()
        if attempt > 0:
            if hasattr(srv, "render_lock"):
                with srv.render_lock:
                    slot["started_at"] = round(attempt_t0, 3)
            else:
                slot["started_at"] = round(attempt_t0, 3)
            write_starting_slot_status(proj_dir, backend, out_fn, attempt_t0)
        min_log_idx = len(getattr(srv, "log_history", None) or [])
        code = exec_fn(srv, cmd_args, slug, backend, slot_id=slot_id, target_filename=out_fn)
        if code != 0:
            try: sw._restore_if_unlinked(proj_dir, out_fn, scene_id)
            except Exception: pass
        if code == 0 or attempt >= max_retries:
            break
        with srv.render_lock:
            still_ok = (
                getattr(srv, "queue_worker_active", False)
                and srv.current_worker_id == session_id
                and slot.get("worker_id") == my_slot_id
                and not slot.get("stop_requested")
                and bool(srv.enabled_engines.get(backend, True))
            )
        if not still_ok or not is_oom_failure(srv, proj_dir, backend, out_fn, attempt_t0, min_log_idx=min_log_idx):
            break
        if hasattr(srv, "broadcast_log"):
            srv.broadcast_log(f"[Studio UI Queue] [{ENGINE_LABELS.get(backend, backend)}] CUDA out of memory on {out_fn}; restarting local engine in {delay_s:.1f}s (retry {attempt + 1}/{max_retries})...")
        dl = time.time() + delay_s
        while time.time() < dl:
            with srv.render_lock:
                if not getattr(srv, "queue_worker_active", False) or srv.current_worker_id != session_id or slot.get("worker_id") != my_slot_id or slot.get("stop_requested") or not srv.enabled_engines.get(backend, True):
                    return code
            time.sleep(min(0.1, max(0.01, dl - time.time())))
    return code


def is_status_valid_for_slot(st_dict: dict | None, slot_started_at: float | None, out_stem: str = "") -> bool:
    """Rejects stale completed/failed/cancelled or pre-slot status JSONs for an active slot."""
    if not isinstance(st_dict, dict) or st_dict.get("state") not in ("rendering", "cancelling"):
        return False
    if not is_fresh_render_status(st_dict, ttl=14400.0 if slot_started_at else 900.0):
        return False
    if out_stem and clean_task_stem(st_dict.get("output_filename") or st_dict.get("filename")) not in ("", out_stem):
        return False
    if slot_started_at:
        ts = float(st_dict.get("updated_at") or st_dict.get("started_at") or 0.0)
        if ts > 0 and ts < float(slot_started_at) - 1.5:
            return False
    return True


def write_starting_slot_status(proj_dir: Path, backend: str, out_fn: str, started_at: float, reason: str = "", model: str = "", frames: int = 0, resolution: str = "") -> None:
    """Resets task/engine/legacy status files to a fresh 'starting' snapshot as soon as a slot claims a task."""
    b = norm_engine(backend)
    stem = clean_task_stem(out_fn)
    fn = f"{out_fn}.mp4" if out_fn and not str(out_fn).endswith(".mp4") else str(out_fn or "")
    payload = {
        "state": "rendering", "backend": b, "stage": "scene",
        "filename": fn, "output_filename": stem, "reason": reason,
        "model": model, "frames": frames, "resolution": resolution,
        "phase": "starting", "raw_phase": "Initializing engine...", "status_text": "Initializing engine...",
        "step": None, "total_steps": None, "pct": 0.0 if b == "local" else None,
        "window_index": None, "window_count": None, "started_at": round(float(started_at), 3),
        "updated_at": round(time.time(), 3), "error": None,
    }
    jdir = proj_dir / "work" / "jobs_v3"
    jdir.mkdir(parents=True, exist_ok=True)
    targets = [jdir / f"engine_status_{b}.json", jdir / "engine_status.json"]
    if stem:
        targets.insert(0, jdir / f"engine_status_{b}__{stem}.json")
    for sf in targets:
        try:
            if atomic_write_json: atomic_write_json(sf, payload)
            else: sf.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass


def mark_task_completed_status(proj_dir: Path, b: str, out_fn: str, elapsed: float = 0.0) -> None:
    """Clears leftover 'rendering' status on disk when a slot finishes cleanly without writing completion status."""
    now_ts, out_stem = round(time.time(), 3), clean_task_stem(out_fn)
    if not out_stem:
        return
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        jdir = proj_dir / jobs_sub
        if not jdir.is_dir():
            continue
        for sf in (jdir / f"engine_status_{b}__{out_stem}.json", jdir / f"engine_status_{b}.json", jdir / "engine_status.json"):
            sd = _read_json_dict(sf)
            if isinstance(sd, dict) and sd.get("state") == "rendering" and clean_task_stem(sd.get("output_filename") or sd.get("filename")) in ("", out_stem):
                sd.update({"state": "completed", "phase": "completed", "raw_phase": "Completed", "pct": 100.0, "elapsed": elapsed, "updated_at": now_ts})
                try:
                    if atomic_write_json: atomic_write_json(sf, sd)
                    else: sf.write_text(json.dumps(sd, indent=2), encoding="utf-8")
                except Exception:
                    pass
