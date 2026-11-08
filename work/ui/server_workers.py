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


stitch_lock = threading.Lock()
stitch_in_progress = False


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


def kill_active_render():
    """Signals graceful Wan2GP cancellation via cancel_request.json and terminates the render process tree."""
    srv = _srv()
    with srv.render_lock:
        srv.queue_worker_active = False
        srv.current_worker_id += 1
        srv.render_job_queue.clear()
        proc = srv.active_process
        active_proj = srv.render_status.get("current_project")

    # 1. Write graceful cancel_request.json so Wan2GP's job.cancel() / _interrupt triggers cleanly
    cancel_files: list[Path] = []
    if active_proj:
        for jobs_sub in ("work/jobs_v3", "work/jobs"):
            jdir = srv.PROJECTS_DIR / active_proj / jobs_sub
            if jdir.is_dir():
                cfile = jdir / "cancel_request.json"
                try:
                    cfile.write_text(json.dumps({"requested_at": time.time(), "reason": "STOPPED BY USER"}), encoding="utf-8")
                    cancel_files.append(cfile)
                except Exception:
                    pass

    if proc is not None:
        srv.broadcast_log("[Studio UI] Requesting graceful Wan2GP engine stop & terminating render process tree...")
        if cancel_files and proc.poll() is None:
            try:
                proc.wait(timeout=0.35)
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            pid = proc.pid
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
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

    for cfile in cancel_files:
        try:
            if cfile.exists():
                cfile.unlink()
        except Exception:
            pass

    with srv.render_lock:
        active_proj = srv.render_status.get("current_project") or active_proj
        srv.active_process = None
        srv.render_status["running"] = False
        if srv.render_status["exit_code"] is None:
            srv.render_status["exit_code"] = -9

    # Clean up any lingering "rendering" status in the active project queue file and engine_status.json
    try:
        if active_proj:
            q_file = srv.PROJECTS_DIR / active_proj / "work" / "jobs_v3" / "v3_queue.json"
            if q_file.is_file():
                q_items = json.loads(q_file.read_text(encoding="utf-8"))
                for q_it in q_items:
                    if q_it.get("status") == "rendering":
                        q_it["status"] = "queued" if q_it.get("needs_render") else "skipped"
                        q_it["reason"] = "STOPPED BY USER"
                q_file.write_text(json.dumps(q_items, indent=2), encoding="utf-8")
            for jobs_sub in ("work/jobs_v3", "work/jobs"):
                st_file = srv.PROJECTS_DIR / active_proj / jobs_sub / "engine_status.json"
                if st_file.is_file():
                    try:
                        st_data = json.loads(st_file.read_text(encoding="utf-8"))
                        st_data["state"] = "cancelled"
                        st_data["phase"] = "cancelled"
                        st_data["raw_phase"] = "Stopped by user"
                        st_data["updated_at"] = round(time.time(), 3)
                        st_file.write_text(json.dumps(st_data, indent=2), encoding="utf-8")
                    except Exception:
                        pass
    except Exception:
        pass

    srv.broadcast_log("[Studio UI] Render process stopped.")


def execute_single_render(cmd_args: list[str], project_slug: str) -> int:
    """Executes a single engine task synchronously while streaming stdout/stderr."""
    srv = _srv()
    with srv.render_lock:
        srv.render_status["running"] = True
        srv.render_status["exit_code"] = None
        srv.render_status["current_project"] = project_slug
        srv.render_status["args"] = cmd_args
        srv.render_status["start_time"] = time.time()

    # Clear any stale cancel_request.json before launching
    if project_slug:
        for jobs_sub in ("work/jobs_v3", "work/jobs"):
            cfile = srv.PROJECTS_DIR / project_slug / jobs_sub / "cancel_request.json"
            if cfile.exists():
                try:
                    cfile.unlink()
                except Exception:
                    pass

    srv.broadcast_log(f"[Studio UI] Executing: {' '.join(cmd_args)}")

    py_exec = srv.resolve_engine_python()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
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
            bufsize=1,
            env=env,
        )
        with srv.render_lock:
            srv.active_process = proc

        while True:
            if not getattr(srv, "queue_worker_active", False) and srv.render_status.get("running") is False:
                break
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                raw_clean = line.rstrip("\r\n")
                if "[QUEUE_" in raw_clean and not raw_clean.startswith("[QUEUE_"):
                    q_idx = raw_clean.find("[QUEUE_")
                    prefix = raw_clean[:q_idx].strip()
                    if prefix:
                        srv.broadcast_log(prefix)
                    srv.broadcast_log(raw_clean[q_idx:])
                else:
                    srv.broadcast_log(raw_clean)
            except Exception:
                break

        try:
            proc.wait(timeout=3)
            exit_code = proc.returncode
        except Exception:
            exit_code = -9
    except Exception as e:
        srv.broadcast_log(f"[Studio UI Error] {e}")
        exit_code = -1
    finally:
        if proc is not None and proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        with srv.render_lock:
            if srv.active_process is proc:
                srv.active_process = None
            if not getattr(srv, "queue_worker_active", False):
                srv.render_status["running"] = False
                srv.render_status["exit_code"] = exit_code

    srv.broadcast_log(f"[Studio UI] Finished single render (exit code {exit_code})")
    return exit_code


def run_anchor_worker(config_path: str, project_slug: str, anchor_args: list[str]):
    """Executes Stage 1 anchor generation from UI and refreshes dependent scene queue state."""
    srv = _srv()
    cmd_args = [config_path] + [str(a) for a in anchor_args]
    code = execute_single_render(cmd_args, project_slug)
    if code == 0:
        try:
            srv.compute_fast_project_queue(project_slug, force_recheck=False)
        except Exception:
            pass
        srv.broadcast_log("[ANCHORS_UPDATED]")


def run_stitch_worker(config_path: str, project_slug: str):
    """Executes master video restitching immediately in a separate thread.
    Does not touch GPU models, does not wait for render locks, and can run
    in parallel with active scene renders."""
    global stitch_in_progress
    srv = _srv()
    with stitch_lock:
        if stitch_in_progress:
            srv.broadcast_log("[Studio UI Stitch] Master restitch already in progress...")
            return
        stitch_in_progress = True

    srv.broadcast_log("[Studio UI Stitch] >>> Immediate master video restitch initiated...")
    py_exec = srv.resolve_engine_python()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [py_exec, "-u", str(srv.RUNNER_SCRIPT), config_path, "stitch", "--skip-anchors"]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(srv.WORKSPACE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            clean_line = line.rstrip("\r\n")
            if clean_line:
                srv.broadcast_log(f"[Stitch] {clean_line}")
        proc.wait(timeout=60)
        code = proc.returncode
        if code == 0:
            srv.broadcast_log("[Studio UI Stitch] ✅ Master video restitched successfully!")
            srv.broadcast_log("[MASTER_UPDATED]")
        else:
            srv.broadcast_log(f"[Studio UI Stitch] ❌ Master restitch exited with code {code}")
    except Exception as e:
        srv.broadcast_log(f"[Studio UI Stitch Error] {e}")
    finally:
        if 'proc' in locals() and proc is not None and proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        with stitch_lock:
            stitch_in_progress = False


def run_pipeline_worker(cmd_args: list[str], project_slug: str):
    """Legacy/direct worker runner for custom commands like 'stitch'."""
    srv = _srv()
    if cmd_args and "stitch" in cmd_args:
        jsons = sorted((srv.PROJECTS_DIR / project_slug).glob("*.json"))
        cfg = str(jsons[-1]) if jsons else cmd_args[0]
        run_stitch_worker(cfg, project_slug)
    else:
        execute_single_render(cmd_args, project_slug)


def run_queue_worker(project_slug: str, force_restart: bool = False):
    """Executes the render queue by submitting ONE SINGLE RENDER at a time to the engine."""
    srv = _srv()
    with srv.render_lock:
        if srv.active_process is not None and srv.active_process.poll() is not None:
            srv.active_process = None

        if getattr(srv, "queue_worker_active", False):
            if srv.active_process is not None and srv.active_process.poll() is None:
                if not force_restart:
                    srv.broadcast_log("[Studio UI Queue] Queue is already actively executing (updated queue will be picked up next).")
                    return
                try:
                    srv.active_process.kill()
                    srv.active_process.wait(timeout=1)
                except Exception:
                    pass
                srv.active_process = None
            srv.queue_worker_active = False
        srv.current_worker_id += 1
        my_worker_id = srv.current_worker_id
        srv.queue_worker_active = True
        srv.render_status["running"] = True
        srv.render_status["exit_code"] = None

    def _worker():
        srv_w = _srv()
        last_exit_code = 0
        try:
            srv_w.broadcast_log("[Studio UI Queue] Queue processing started (single-render mode).")
            proj_dir = srv_w.PROJECTS_DIR / project_slug
            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"

            # Resolve project configuration JSON file
            jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
            if not jsons:
                srv_w.broadcast_log("[Studio UI Error] No project config JSON found.")
                return
            config_path = str(jsons[-1])

            rendered_any = False
            while getattr(srv_w, "queue_worker_active", False) and srv_w.current_worker_id == my_worker_id:
                # 1. Read queue file fresh on every loop iteration
                items = []
                if queue_file.is_file():
                    try:
                        items = json.loads(queue_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass

                if not items:
                    items = srv_w.compute_fast_project_queue(project_slug)

                # 2. Find next task marked queued
                next_task = None
                next_idx = None
                for idx, item in enumerate(items):
                    if item.get("status") == "queued" and item.get("needs_render"):
                        next_task = item
                        next_idx = idx
                        break

                if not next_task:
                    if rendered_any and getattr(srv_w, "queue_worker_active", False) and srv_w.current_worker_id == my_worker_id:
                        srv_w.broadcast_log("[Studio UI Queue] All queued passes completed! Restitching master video...")
                        run_stitch_worker(config_path, project_slug)
                        srv_w.broadcast_log("[Studio UI Queue] Queue finished. Master video updated.")
                    else:
                        srv_w.broadcast_log("[Studio UI Queue] No passes currently queued for rendering.")
                    break

                out_fn = next_task.get("output_filename", "")
                target_arg = next_task.get("target_arg") or derive_target_arg(out_fn) or next_task.get("scene_id")
                next_task["target_arg"] = target_arg
                next_task["scene_id"] = next_task.get("scene_id") or derive_scene_id(out_fn)

                # 3. Mark as rendering and persist
                items[next_idx]["status"] = "rendering"
                try:
                    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                except Exception:
                    pass

                srv_w.broadcast_log(f"[Studio UI Queue] >>> Starting single render [{next_idx + 1}/{len(items)}]: {out_fn} ({target_arg})")

                # 4. Submit single render task to engine
                cmd_args = [config_path, target_arg, "--skip-anchors", "--no-stitch", "--no-queue-sync"]
                code = execute_single_render(cmd_args, project_slug)
                if not getattr(srv_w, "queue_worker_active", False) or srv_w.current_worker_id != my_worker_id:
                    break

                # 5. Reload queue file fresh to respect any user reordering/deletions done during render
                try:
                    items = json.loads(queue_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

                for item in items:
                    if item.get("output_filename") == out_fn:
                        if code == 0:
                            item["status"] = "completed"
                            item["needs_render"] = False
                            item["reason"] = "COMPLETED"
                            item.pop("user_skipped_hash", None)
                            rendered_any = True
                        else:
                            item["status"] = "failed"
                        break

                try:
                    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                except Exception:
                    pass

                if code != 0:
                    last_exit_code = code
                    srv_w.broadcast_log(f"[Studio UI Error] Single render failed for {out_fn} (exit code {code}). Queue paused.")
                    break

                time.sleep(0.2)
        except Exception as e:
            srv_w.broadcast_log(f"[Studio UI Queue Worker Exception] {e}")
            last_exit_code = -1
        finally:
            with srv_w.render_lock:
                if srv_w.current_worker_id == my_worker_id:
                    srv_w.queue_worker_active = False
                    srv_w.render_status["running"] = False
                    srv_w.render_status["exit_code"] = last_exit_code
            srv_w.broadcast_log("[Studio UI Queue] Queue runner idle.")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
