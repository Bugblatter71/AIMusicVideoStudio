"""Render worker threads, process management, and log streaming for Studio UI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


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


try:
    from pipeline.media_utils import prepend_unique_env_path
    from pipeline.project_paths import resolve_project_dirs
    from pipeline.takes import create_project_checkpoint
except ImportError:
    try:
        from work.pipeline.media_utils import prepend_unique_env_path
        from work.pipeline.project_paths import resolve_project_dirs
        from work.pipeline.takes import create_project_checkpoint
    except ImportError:
        prepend_unique_env_path = None
        resolve_project_dirs = None
        create_project_checkpoint = None


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8:replace"
    if prepend_unique_env_path is not None:
        prepend_unique_env_path(env_map=env)
    return env


stitch_lock = threading.Lock()
checkpoint_lock = threading.Lock()
stitch_in_progress = False
_stitch_spawning = False


def _se():
    try:
        from work.ui import server_engines as se
        return se
    except ImportError:
        import server_engines as se
        return se


def derive_scene_id(out_name: str) -> str:
    if not out_name:
        return ""
    return str(out_name).split("_")[0]


def derive_target_arg(out_name: str) -> str:
    if not out_name:
        return ""
    parts = str(out_name).split("_")
    sid = parts[0]
    if "Pass1" in out_name:
        return f"{sid}:p1"
    elif "Pass2" in out_name:
        return f"{sid}:p2"
    return sid


def _restore_if_unlinked(p_dir: Path, out_name: str | None, sid: str | None = None) -> None:
    if not out_name:
        return
    clean_name = Path(out_name).stem
    s_id = sid or derive_scene_id(clean_name)
    if clean_name and s_id:
        try:
            from pipeline.takes import restore_previous_active_take_if_missing
            rdir = resolve_project_dirs(p_dir)["renders"] if resolve_project_dirs else (p_dir / "work" / "renders")
            restore_previous_active_take_if_missing(p_dir, s_id, clean_name, rdir / f"{clean_name}.mp4")
        except Exception:
            pass


def kill_active_render(target_slug: str | None = None, backend: str | None = None, filename: str | None = None):
    """Signals graceful cancellation and terminates one job, one engine lane, or all active render processes."""
    _se().kill_engine_render(_srv(), target_slug=target_slug, backend=backend, filename=filename)


def execute_single_render(cmd_args: list[str], project_slug: str, engine_tag: str | None = None, slot_id: str | None = None, target_filename: str | None = None) -> int:
    """Executes a single engine task synchronously while streaming stdout/stderr."""
    srv = _srv()
    se = _se()
    se.ensure_engine_state(srv)
    b_tag = se.norm_engine(engine_tag) if engine_tag else None
    t_stem = se.clean_task_stem(target_filename)
    with srv.render_lock:
        srv.render_status["running"] = True
        srv.render_status["exit_code"] = None
        srv.render_status["current_project"] = project_slug
        srv.render_status["args"] = cmd_args
        if not srv.render_status.get("start_time"):
            srv.render_status["start_time"] = time.time()
        siblings_active = sum(1 for k, s in srv.engine_slots[b_tag].items() if k != slot_id and s.get("running")) if b_tag else 0

    if project_slug:
        with srv.render_lock:
            stopping_global = any(s.get("stop_requested") for em in srv.engine_slots.values() for s in em.values())
        p_dir = srv.PROJECTS_DIR / project_slug
        jdir = resolve_project_dirs(p_dir)["jobs"] if resolve_project_dirs else (p_dir / "work" / "jobs")
        if b_tag and t_stem:
            names = [f"cancel_request_{b_tag}__{t_stem}.json"] + ([f"cancel_request_{b_tag}.json"] if siblings_active == 0 else [])
        elif b_tag:
            names = [f"cancel_request_{b_tag}.json"]
        else:
            names = ["cancel_request.json", "cancel_request_local.json", "cancel_request_fal.json", "cancel_request_minimax.json"]
        if b_tag and not stopping_global and siblings_active == 0:
            names.append("cancel_request.json")
        for nm in names:
            cf = jdir / nm
            if cf.exists():
                try: cf.unlink()
                except Exception: pass

        if create_project_checkpoint:
            with checkpoint_lock:
                try:
                    p_dir = srv.PROJECTS_DIR / project_slug
                    c_file = next((Path(a) for a in cmd_args if str(a).endswith(".json") and Path(a).is_file()), None)
                    if not c_file and p_dir.is_dir():
                        js = [j for j in sorted(p_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
                        if js: c_file = js[-1]
                    if p_dir.is_dir() and c_file and c_file.is_file():
                        tgt = "batch"
                        if "--scene" in cmd_args:
                            idx = cmd_args.index("--scene")
                            if idx + 1 < len(cmd_args): tgt = cmd_args[idx + 1]
                        elif len(cmd_args) > 1 and not cmd_args[1].startswith("-"):
                            tgt = cmd_args[1]
                        create_project_checkpoint(p_dir, c_file, name=f"Auto: Pre-render ({tgt})", trigger="pre_render")
                except Exception:
                    pass

    srv.broadcast_log(f"[Studio UI] Executing: {' '.join(cmd_args)}")
    py_exec = srv.resolve_engine_python()
    env = _subprocess_env()
    clean_cmd_args = [str(a) for a in cmd_args if a is not None and str(a).strip()]
    if getattr(srv, "dummy_render_mode", False) and "--dummy" not in clean_cmd_args:
        clean_cmd_args.append("--dummy")
    full_cmd = [py_exec, "-u", str(srv.RUNNER_SCRIPT)] + clean_cmd_args

    proc = None
    exit_code = -1
    try:
        proc = subprocess.Popen(
            full_cmd,
            cwd=str(srv.WORKSPACE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        with srv.render_lock:
            if b_tag:
                if slot_id and slot_id in srv.engine_slots[b_tag]:
                    srv.engine_slots[b_tag][slot_id]["active_process"] = proc
                srv.engine_workers[b_tag]["active_process"] = proc
            if srv.active_process is None or b_tag in (None, "local"):
                srv.active_process = proc

        stop_triggered = False
        while True:
            if b_tag:
                slot_obj = srv.engine_slots[b_tag].get(slot_id) if slot_id else None
                if (slot_obj.get("stop_requested") if slot_obj is not None else srv.engine_workers[b_tag]["stop_requested"]):
                    stop_triggered = True
                    break
            elif not getattr(srv, "queue_worker_active", False) and srv.render_status.get("running") is False:
                stop_triggered = True
                break
            try:
                line = proc.stdout.readline()
            except Exception as read_exc:
                srv.broadcast_log(f"[Studio UI Error] Pipe read failed for {target_filename or project_slug}: {read_exc}")
                break
            if not line:
                break
            try:
                se.emit_process_line(srv, line.rstrip("\r\n"), engine_tag=b_tag)
            except Exception as emit_exc:
                srv.broadcast_log(f"[Studio UI Warning] Log parse error: {emit_exc}")

        wait_s = 3 if stop_triggered else 30
        try:
            proc.wait(timeout=wait_s)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            srv.broadcast_log(f"[Studio UI Error] Subprocess still running after stdout closed ({wait_s}s timeout); terminating.")
            se.terminate_proc(srv, proc)
            exit_code = -9
        except Exception:
            exit_code = -9
    except Exception as e:
        srv.broadcast_log(f"[Studio UI Error] {e}")
        exit_code = -1
    finally:
        if proc is not None and proc.stdout:
            try: proc.stdout.close()
            except Exception: pass
        with srv.render_lock:
            if b_tag:
                if slot_id and slot_id in srv.engine_slots[b_tag] and srv.engine_slots[b_tag][slot_id].get("active_process") is proc:
                    srv.engine_slots[b_tag][slot_id]["active_process"] = None
                if srv.engine_workers[b_tag].get("active_process") is proc:
                    srv.engine_workers[b_tag]["active_process"] = next((s.get("active_process") for s in srv.engine_slots[b_tag].values() if s.get("active_process") is not None), None)
            if srv.active_process is proc:
                srv.active_process = next((srv.engine_workers[x]["active_process"] for x in se.ENGINES if srv.engine_workers[x].get("active_process") is not None), None)
            if not getattr(srv, "queue_worker_active", False) and not any(srv.engine_workers[x]["running"] for x in se.ENGINES):
                srv.render_status["running"] = False
                srv.render_status["exit_code"] = exit_code

    srv.broadcast_log(f"[Studio UI] Finished single render (exit code {exit_code})")
    return exit_code


def run_anchor_worker(config_path: str, project_slug: str, anchor_args: list[str]):
    """Executes Stage 1 anchor generation from UI and refreshes dependent scene queue state."""
    srv = _srv()
    cmd_args = [config_path] + [str(a) for a in anchor_args]
    held = False
    with srv.render_lock:
        if not getattr(srv, "local_adhoc_busy", False):
            srv.local_adhoc_busy = True
            held = True
    try:
        code = execute_single_render(cmd_args, project_slug)
        if code == 0:
            try:
                srv.compute_fast_project_queue(project_slug, force_recheck=False)
            except Exception:
                pass
            srv.broadcast_log("[ANCHORS_UPDATED]")
    finally:
        if held:
            with srv.render_lock:
                srv.local_adhoc_busy = False
            try:
                if getattr(srv, "queue_worker_active", False):
                    _se().wake_enabled_engine_workers(srv, project_slug)
            except Exception:
                pass


def run_stitch_worker(config_path: str, project_slug: str) -> bool:
    """Executes master video restitching immediately in a separate thread.
    Does not touch GPU models, does not wait for render locks, and can run
    in parallel with active scene renders."""
    global stitch_in_progress, _stitch_spawning
    srv = _srv()
    with stitch_lock:
        if stitch_in_progress:
            srv.stitch_pending = True
            srv.broadcast_log("[Studio UI Stitch] Master restitch already in progress (queued follow-up)...")
            cur_prog = getattr(srv, "stitch_progress", None)
            if cur_prog:
                srv.broadcast_log(f"[MASTER_STITCH_PROGRESS] {json.dumps(cur_prog)}")
            return True
        stitch_in_progress = srv.stitch_in_progress = True
        _stitch_spawning = False
        srv.stitch_pending = False
        srv.stitch_error = None
        srv.stitch_progress = {"pct": 0, "phase": "Starting stitch worker...", "detail": "Initializing...", "slug": project_slug}

    py_exec = srv.resolve_engine_python()
    env = _subprocess_env()
    cmd = [py_exec, "-u", str(srv.RUNNER_SCRIPT), config_path, "stitch", "--skip-anchors"]
    final_ok = False
    try:
        while True:
            srv.broadcast_log(f"[MASTER_STITCH_START] {json.dumps({'slug': project_slug})}")
            srv.broadcast_log("[Studio UI Stitch] >>> Immediate master video restitch initiated...")
            proc = None
            stitch_succeeded = False
            lock_error_msg = None
            last_stitch_error = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(srv.WORKSPACE_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    clean_line = line.rstrip("\r\n")
                    if clean_line:
                        if clean_line.startswith("[MASTER_STITCH_PROGRESS]"):
                            try:
                                srv.stitch_progress = json.loads(clean_line[24:].strip())
                            except Exception:
                                pass
                            srv.broadcast_log(clean_line)
                        else:
                            srv.broadcast_log(f"[Stitch] {clean_line}")
                            if "is currently open in a player" in clean_line or "MASTER_FILE_LOCKED" in clean_line:
                                lock_error_msg = clean_line
                            if "[MASTER_STITCH_FAILED]" in clean_line or "CalledProcessError:" in clean_line or "[Stitch Error]" in clean_line:
                                last_stitch_error = clean_line
                            if "[Progressive Master Updated]" in clean_line:
                                stitch_succeeded = True
                proc.wait(timeout=120)
                code = proc.returncode
                if code == 0 and stitch_succeeded:
                    srv.stitch_error = None
                    final_ok = True
                    srv.broadcast_log("[Studio UI Stitch] ✅ Master video restitched successfully!")
                    srv.broadcast_log("[MASTER_UPDATED]")
                elif lock_error_msg:
                    srv.stitch_error = lock_error_msg
                    final_ok = False
                    srv.broadcast_log("[Studio UI Stitch Error] ⚠️ Master video file is locked by a video player or external program. Please close your media player and try again.")
                    srv.broadcast_log(f"[MASTER_LOCKED] {json.dumps({'error': lock_error_msg})}")
                else:
                    err_txt = last_stitch_error or f"Master restitch exited with code {code}"
                    srv.stitch_error = err_txt
                    final_ok = False
                    srv.broadcast_log(f"[Studio UI Stitch] ❌ Master restitch exited with code {code}")
                    srv.broadcast_log(f"[MASTER_ERROR] {json.dumps({'code': code, 'error': err_txt})}")
            except Exception as e:
                srv.stitch_error = str(e)
                final_ok = False
                srv.broadcast_log(f"[Studio UI Stitch Error] {e}")
                srv.broadcast_log(f"[MASTER_ERROR] {json.dumps({'error': str(e)})}")
            finally:
                if proc is not None and proc.stdout:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
            with stitch_lock:
                if getattr(srv, "stitch_pending", False):
                    srv.stitch_pending = False
                    srv.stitch_progress = {"pct": 0, "phase": "Running follow-up stitch...", "detail": "Latest renders detected...", "slug": project_slug}
                    continue
                break
    finally:
        with stitch_lock:
            stitch_in_progress = srv.stitch_in_progress = False
            srv.stitch_progress = None
    return final_ok


def run_pipeline_worker(cmd_args: list[str], project_slug: str):
    """Legacy/direct worker runner for custom commands like 'stitch'."""
    srv = _srv()
    if cmd_args and "stitch" in cmd_args:
        jsons = sorted((srv.PROJECTS_DIR / project_slug).glob("*.json"))
        cfg = str(jsons[-1]) if jsons else cmd_args[0]
        run_stitch_worker(cfg, project_slug)
    else:
        execute_single_render(cmd_args, project_slug)


def run_queue_worker(project_slug: str, force_restart: bool = False, enabled_engines: dict | None = None, max_parallel: dict | None = None):
    """Executes the render queue with independent parallel engine workers and configurable per-engine concurrency."""
    _se().run_multi_engine_queue_worker(_srv(), project_slug, force_restart=force_restart, enabled_engines=enabled_engines, max_parallel=max_parallel)
