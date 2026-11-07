"""Music Video Generation Studio - Local Web UI Server.

Zero external dependencies - powered entirely by Python standard library:
- ThreadingHTTPServer for concurrent requests & SSE streaming
- HTTP 206 Partial Content (Range requests) for smooth audio & video scrubbing
- Subprocess manager with real-time log broadcast
- Automated timestamped backup on every save
"""

from __future__ import annotations

import datetime
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECTS_DIR = WORKSPACE_ROOT / "projects"
BACKUP_DIR = WORKSPACE_ROOT / "backup"
RUNNER_SCRIPT = WORKSPACE_ROOT / "work" / "run_overnight_v3.py"
PROFILES_FILE = WORKSPACE_ROOT / "model_profiles.json"
TEMPLATE_FILE = WORKSPACE_ROOT / "project_template.json"
TEMPLATES_FILE = TEMPLATE_FILE

sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
try:
    from run_overnight_v3 import ProjectContext, compute_project_task_hashes, resolve_rendered_clip_path
except ImportError:
    ProjectContext = None
    compute_project_task_hashes = None
    resolve_rendered_clip_path = None

# Global state for background render subprocess
render_lock = threading.Lock()
active_process: subprocess.Popen | None = None
log_subscribers: list[threading.Event] = []
log_seq_counter = 0
log_history: list[tuple[int, str]] = []
render_status = {
    "running": False,
    "exit_code": None,
    "current_project": None,
    "args": [],
    "start_time": None,
}
render_job_queue: list[tuple[list[str], str]] = []
queue_worker_active = False
dummy_render_mode: bool = bool(os.environ.get("DUMMY_RENDER", "0") in ("1", "true", "yes"))


def broadcast_log(line: str):
    global log_seq_counter
    with render_lock:
        log_seq_counter += 1
        log_history.append((log_seq_counter, line))
        if len(log_history) > 3000:
            log_history.pop(0)


def kill_active_render():
    """Forcefully kills the render process and any child processes (ffmpeg, workers) on Windows or POSIX."""
    global active_process, render_status, queue_worker_active
    with render_lock:
        queue_worker_active = False
        render_job_queue.clear()
        proc = active_process

    if proc is not None:
        broadcast_log("[Studio UI] Terminating render process tree...")
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
            broadcast_log(f"[Studio UI Warning] Process termination error: {e}")

        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    with render_lock:
        active_proj = render_status.get("current_project")
        active_process = None
        render_status["running"] = False
        if render_status["exit_code"] is None:
            render_status["exit_code"] = -9

    # Clean up any lingering "rendering" status in the active project queue file
    try:
        if active_proj:
            q_file = PROJECTS_DIR / active_proj / "work" / "jobs_v3" / "v3_queue.json"
            if q_file.is_file():
                q_items = json.loads(q_file.read_text(encoding="utf-8"))
                for q_it in q_items:
                    if q_it.get("status") == "rendering":
                        q_it["status"] = "queued" if q_it.get("needs_render") else "skipped"
                        q_it["reason"] = "STOPPED BY USER"
                q_file.write_text(json.dumps(q_items, indent=2), encoding="utf-8")
    except Exception:
        pass

    broadcast_log("[Studio UI] Render process stopped.")


def execute_single_render(cmd_args: list[str], project_slug: str) -> int:
    """Executes a single engine task synchronously while streaming stdout/stderr."""
    global active_process, render_status
    with render_lock:
        render_status["running"] = True
        render_status["exit_code"] = None
        render_status["current_project"] = project_slug
        render_status["args"] = cmd_args
        render_status["start_time"] = time.time()

    broadcast_log(f"[Studio UI] Executing: {' '.join(cmd_args)}")

    py_cand = Path(r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe")
    py_exec = str(py_cand) if py_cand.exists() else sys.executable

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    clean_cmd_args = [str(a) for a in cmd_args if a is not None and str(a).strip()]
    global dummy_render_mode
    if dummy_render_mode and "--dummy" not in clean_cmd_args:
        clean_cmd_args.append("--dummy")
    full_cmd = [py_exec, "-u", str(RUNNER_SCRIPT)] + clean_cmd_args

    proc = None
    exit_code = -1
    try:
        proc = subprocess.Popen(
            full_cmd,
            cwd=str(WORKSPACE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        with render_lock:
            active_process = proc

        while True:
            if not queue_worker_active and render_status.get("running") is False:
                break
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                broadcast_log(line.rstrip("\r\n"))
            except Exception:
                break

        try:
            proc.wait(timeout=3)
            exit_code = proc.returncode
        except Exception:
            exit_code = -9
    except Exception as e:
        broadcast_log(f"[Studio UI Error] {e}")
        exit_code = -1
    finally:
        with render_lock:
            active_process = None
            if not queue_worker_active:
                render_status["running"] = False
                render_status["exit_code"] = exit_code

    broadcast_log(f"[Studio UI] Finished single render (exit code {exit_code})")
    return exit_code


stitch_lock = threading.Lock()
stitch_in_progress = False


def run_stitch_worker(config_path: str, project_slug: str):
    """Executes master video restitching immediately in a separate thread.
    Does not touch GPU models, does not wait for render locks, and can run
    in parallel with active scene renders."""
    global stitch_in_progress
    with stitch_lock:
        if stitch_in_progress:
            broadcast_log("[Studio UI Stitch] Master restitch already in progress...")
            return
        stitch_in_progress = True

    broadcast_log("[Studio UI Stitch] >>> Immediate master video restitch initiated...")
    py_cand = Path(r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe")
    py_exec = str(py_cand) if py_cand.exists() else sys.executable

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [py_exec, "-u", str(RUNNER_SCRIPT), config_path, "stitch", "--skip-anchors"]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(WORKSPACE_ROOT),
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
                broadcast_log(f"[Stitch] {clean_line}")
        proc.wait(timeout=60)
        code = proc.returncode
        if code == 0:
            broadcast_log("[Studio UI Stitch] ✅ Master video restitched successfully!")
            broadcast_log("[MASTER_UPDATED]")
        else:
            broadcast_log(f"[Studio UI Stitch] ❌ Master restitch exited with code {code}")
    except Exception as e:
        broadcast_log(f"[Studio UI Stitch Error] {e}")
    finally:
        with stitch_lock:
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
        return f"{sid}:1"
    elif "Pass2" in out_name:
        return f"{sid}:2"
    return sid


def run_pipeline_worker(cmd_args: list[str], project_slug: str):
    """Legacy/direct worker runner for custom commands like 'stitch'."""
    if cmd_args and "stitch" in cmd_args:
        jsons = sorted((PROJECTS_DIR / project_slug).glob("*.json"))
        cfg = str(jsons[-1]) if jsons else cmd_args[0]
        run_stitch_worker(cfg, project_slug)
    else:
        execute_single_render(cmd_args, project_slug)


def run_queue_worker(project_slug: str, force_restart: bool = False):
    """Executes the render queue by submitting ONE SINGLE RENDER at a time to the engine."""
    global queue_worker_active, active_process
    with render_lock:
        if active_process is not None and active_process.poll() is not None:
            active_process = None

        if queue_worker_active:
            if active_process is not None and active_process.poll() is None:
                if not force_restart:
                    broadcast_log("[Studio UI Queue] Queue is already actively executing.")
                    return
                # Force restart requested (e.g. targeted single scene render)
                try:
                    active_process.kill()
                    active_process.wait(timeout=1)
                except Exception:
                    pass
                active_process = None
            queue_worker_active = False
        queue_worker_active = True
        render_status["running"] = True
        render_status["exit_code"] = None

    def _worker():
        global queue_worker_active
        last_exit_code = 0
        try:
            broadcast_log("[Studio UI Queue] Queue processing started (single-render mode).")
            proj_dir = PROJECTS_DIR / project_slug
            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"

            # Resolve project configuration JSON file
            jsons = sorted(proj_dir.glob("*.json"))
            if not jsons:
                broadcast_log("[Studio UI Error] No project config JSON found.")
                return
            config_path = str(jsons[-1])

            rendered_any = False
            while queue_worker_active:
                # 1. Read queue file fresh on every loop iteration
                items = []
                if queue_file.is_file():
                    try:
                        items = json.loads(queue_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass

                if not items:
                    items = compute_fast_project_queue(project_slug)

                # 2. Find next task marked queued
                next_task = None
                next_idx = None
                for idx, item in enumerate(items):
                    if item.get("status") == "queued" and item.get("needs_render"):
                        next_task = item
                        next_idx = idx
                        break

                if not next_task:
                    if rendered_any and queue_worker_active:
                        broadcast_log("[Studio UI Queue] All queued passes completed! Restitching master video...")
                        run_stitch_worker(config_path, project_slug)
                        broadcast_log("[Studio UI Queue] Queue finished. Master video updated.")
                    else:
                        broadcast_log("[Studio UI Queue] No passes currently queued for rendering.")
                    break

                out_fn = next_task.get("output_filename", "")
                target_arg = next_task.get("target_arg") or next_task.get("scene_id") or derive_target_arg(out_fn)
                next_task["target_arg"] = target_arg
                next_task["scene_id"] = next_task.get("scene_id") or derive_scene_id(out_fn)

                # 3. Mark as rendering and persist
                items[next_idx]["status"] = "rendering"
                try:
                    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                except Exception:
                    pass

                broadcast_log(f"[Studio UI Queue] >>> Starting single render [{next_idx + 1}/{len(items)}]: {out_fn} ({target_arg})")

                # 4. Submit single render task to engine
                cmd_args = [config_path, target_arg, "--skip-anchors", "--no-stitch", "--no-queue-sync"]
                code = execute_single_render(cmd_args, project_slug)
                if not queue_worker_active:
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
                    broadcast_log(f"[Studio UI Error] Single render failed for {out_fn} (exit code {code}). Queue paused.")
                    break

                time.sleep(0.5)
        except Exception as e:
            broadcast_log(f"[Studio UI Queue Worker Exception] {e}")
            last_exit_code = -1
        finally:
            with render_lock:
                queue_worker_active = False
                render_status["running"] = False
                render_status["exit_code"] = last_exit_code
            broadcast_log("[Studio UI Queue] Queue runner idle.")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def extract_anchor_id(name_or_str: str | Path) -> str:
    """Extracts normalized anchor ID like 'A01', 'A02' ... from any filename or string."""
    s = Path(str(name_or_str)).name.strip()
    m = re.match(r"^(A\d{1,2})([ _\-\.]|$)", s, re.IGNORECASE)
    if m:
        num = int(m.group(1)[1:])
        return f"A{num:02d}"
    parts = re.split(r"[-_ \.]", s)
    if parts:
        p0 = parts[0].strip().upper()
        if p0.startswith("A") and p0[1:].isdigit():
            num = int(p0[1:])
            return f"A{num:02d}"
    return s.upper()


def backup_project_json(json_path: Path):
    if not json_path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"{json_path.stem}_{ts}.json"
    shutil.copy2(json_path, dest)


def resolve_project_resolution(data: dict) -> str:
    models = data.get("models", {})
    raw = (
        models.get("default_video_resolution")
        or models.get("resolution")
        or (models.get("video_shared_settings", {}).get("resolution") if isinstance(models.get("video_shared_settings"), dict) else None)
        or data.get("project", {}).get("master_resolution")
        or "4"
    )
    res_map = {
        "1": "832x480", "2": "960x544", "3": "1280x720",
        "4": "1344x768", "5": "1536x864", "6": "1920x1088",
    }
    s = str(raw).strip()
    if s in res_map:
        return res_map[s]
    if "x" in s.lower():
        return s.lower()
    return "1344x768"


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
                    "filename": clip.name,
                    "path": rel_path,
                    "size_bytes": clip.stat().st_size,
                    "mtime": clip.stat().st_mtime,
                }
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
    proj_dir = PROJECTS_DIR / slug
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

    renders_dir = proj_dir / "work" / "renders_v3"
    anchors_dir = proj_dir / "work" / "anchors_v3"
    jobs_dir = proj_dir / "work" / "jobs_v3"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    queue_file = jobs_dir / "v3_queue.json"

    hashes_file = jobs_dir / "state_hashes.json"
    hashes = {}
    stored_anchor_hashes = {}
    scene_renders = {}
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

    task_hashes_current = {}
    if ProjectContext and compute_project_task_hashes and config_file.is_file():
        try:
            ctx = ProjectContext(config_file)
            task_hashes_current = compute_project_task_hashes(ctx, target_resolution=default_res)
        except Exception:
            pass

    existing_items = []
    if queue_file.is_file() and not force_recheck:
        try:
            existing_items = json.loads(queue_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    queue = []
    seen = set()
    p1_needs_rerender: dict[str, str] = {}
    is_engine_active = active_process is not None and active_process.poll() is None

    # Helper to evaluate pass state
    def _evaluate_pass(out_name: str, sid: str, pass_num: int | None, current_item: dict | None = None) -> tuple[str, str, bool]:
        if resolve_rendered_clip_path:
            mp4_path = resolve_rendered_clip_path(renders_dir, out_name)
        else:
            mp4_path = renders_dir / f"{out_name}.mp4"
        file_exists = mp4_path.exists() and mp4_path.stat().st_size > 50000

        # Preserve active rendering status if engine is running
        if current_item and current_item.get("status") == "rendering" and is_engine_active:
            return "rendering", current_item.get("reason", "RENDERING"), True

        # Preserve explicit FORCED queue status unless force_recheck
        if current_item and not force_recheck:
            if current_item.get("status") == "queued" and current_item.get("reason") == "FORCED":
                return "queued", "FORCED", True

        if not file_exists:
            return "queued", "MISSING FILE", True

        # Check Two-Pass Pass 2 dependency on Pass 1
        if pass_num == 2 and sid in p1_needs_rerender:
            return "queued", f"PASS 1 RE-RENDERED ({p1_needs_rerender[sid]})", True

        sc = scene_by_id.get(sid)

        # Check timeline bounds coverage
        if sc:
            sc_start = float(sc.get("start", 0))
            sc_end = float(sc.get("end", 0))
            render_info = scene_renders.get(out_name)
            if render_info and "render_start" in render_info and "render_end" in render_info:
                r_start = float(render_info["render_start"])
                r_end = float(render_info["render_end"])
                if not ((r_start <= sc_start + 0.05) and (r_end >= sc_end - 0.05)):
                    return "queued", f"TIMELINE EXCEEDS RENDER ([{sc_start:.2f}, {sc_end:.2f}] not covered by [{r_start:.2f}, {r_end:.2f}])", True

        aid = extract_anchor_id(sc.get("anchor", "")) if sc else ""
        anchor_info = disk_anchors.get(aid)
        stored_a_hash = stored_anchor_hashes.get(aid)

        if anchor_info and mp4_path.stat().st_mtime < anchor_info["mtime"]:
            return "queued", f"ANCHOR {aid} NEWER ON DISK", True
        if anchor_info and stored_a_hash and anchor_info["hash"] != stored_a_hash:
            return "queued", f"ANCHOR {aid} EDITED", True

        # Check if prompt, seed, stem, renderer, or settings have been edited
        curr_hashes = task_hashes_current.get(out_name)
        if curr_hashes:
            prev_h = hashes.get(out_name)
            if prev_h is not None:
                if prev_h not in curr_hashes:
                    return "queued", "PROMPT/SEED/SETTINGS EDITED", True
            else:
                return "queued", "EDITED", True
        elif out_name not in hashes:
            return "queued", "EDITED", True

        # File exists and is up-to-date with anchor and settings
        return "completed", "UP-TO-DATE", False

    # 1. Preserve existing user-ordered items if present and matching current resolution
    for item in existing_items:
        out_name = item.get("output_filename")
        if not out_name or out_name in seen:
            continue
        if any(r in out_name for r in ("832x480", "960x544", "1280x720", "1344x768", "1536x864", "1920x1088")) and default_res not in out_name:
            continue
        seen.add(out_name)
        sid = item.get("scene_id") or derive_scene_id(out_name)
        item["scene_id"] = sid
        if not item.get("target_arg"):
            item["target_arg"] = derive_target_arg(out_name)

        pass_num = item.get("pass")
        if pass_num is None:
            if "Pass1" in out_name:
                pass_num = 1
            elif "Pass2" in out_name:
                pass_num = 2
        item["pass"] = pass_num

        status, reason, needs_render = _evaluate_pass(out_name, sid, pass_num, current_item=item)
        item["status"] = status
        item["reason"] = reason
        item["needs_render"] = needs_render

        if pass_num == 1 and needs_render:
            p1_needs_rerender[sid] = reason

        queue.append(item)

    # 2. Add any project scenes not already in the queue
    for sc in scenes:
        sid = sc.get("id", "")
        mode = sc.get("mode", "solo")
        is_two_pass = mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva")

        res_str = default_res
        if is_two_pass:
            targets = [
                (f"{sid}_Pass1_Left_{res_str}", f"{sid}:p1", 1),
                (f"{sid}_Pass2_Final_{res_str}", f"{sid}:p2", 2),
            ]
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
            targets = [(out_fn, sid, None)]

        for out_name, tgt_arg, pass_num in targets:
            if out_name in seen:
                continue
            seen.add(out_name)

            status, reason, needs_render = _evaluate_pass(out_name, sid, pass_num)
            if pass_num == 1 and needs_render:
                p1_needs_rerender[sid] = reason

            queue.append({
                "idx": len(queue) + 1,
                "id": out_name,
                "scene_id": sid,
                "target_arg": tgt_arg,
                "pass": pass_num,
                "filename": f"{out_name}.mp4",
                "output_filename": out_name,
                "status": status,
                "reason": reason,
                "needs_render": needs_render,
                "model_type": "minimax_h3_fl2va_pruned_pdd",
                "resolution": res_str,
            })

    for i, itm in enumerate(queue, 1):
        itm["idx"] = i
        itm["total"] = len(queue)

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


def prepare_targeted_queue_items(slug: str, target_sid: str) -> list[dict]:
    """Ensures targeted scene and its pass items are enqueued and positioned at the top of the queue."""
    target_sid = str(target_sid).strip().upper()
    target_base = target_sid.split(":")[0].strip()
    target_pass = None
    if ":" in target_sid:
        p_tok = target_sid.split(":")[1].lower().replace("p", "").strip()
        if p_tok.isdigit():
            target_pass = int(p_tok)

    proj_dir = PROJECTS_DIR / slug
    queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
    jsons = sorted(proj_dir.glob("*.json"))
    config_file = jsons[-1] if jsons else None

    if not queue_file.is_file():
        compute_fast_project_queue(slug)

    items = []
    if queue_file.is_file():
        try:
            items = json.loads(queue_file.read_text(encoding="utf-8"))
        except Exception:
            items = []

    matched_items = []
    for item in items:
        out_fn = item.get("output_filename", "")
        sid = item.get("scene_id") or derive_scene_id(out_fn)
        item["scene_id"] = sid
        if not item.get("target_arg"):
            item["target_arg"] = derive_target_arg(out_fn)

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
            item["status"] = "queued"
            item["needs_render"] = True
            item["reason"] = f"TARGETED ({target_sid})"
            matched_items.append(item)
        elif item.get("status") == "rendering" and not (active_process and active_process.poll() is None):
            item["status"] = "queued" if item.get("needs_render") else "skipped"
            item["reason"] = "INTERRUPTED"

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
                        "idx": len(items) + 1,
                        "id": out_fn,
                        "scene_id": sid,
                        "target_arg": tgt_arg,
                        "pass": p_num,
                        "filename": f"{out_fn}.mp4",
                        "output_filename": out_fn,
                        "status": "queued",
                        "reason": f"TARGETED ({target_sid})",
                        "needs_render": True,
                        "model_type": "minimax_h3_fl2va_pruned_pdd",
                        "resolution": res_str,
                    }
                    items.insert(0, new_item)
                    matched_items.append(new_item)
        except Exception as ge:
            broadcast_log(f"[Studio UI Warning] Could not dynamically generate queue item for {target_sid}: {ge}")

    # Move matched items to the top of the queued list
    if matched_items:
        for m in reversed(matched_items):
            if m in items:
                items.remove(m)
            items.insert(0, m)
        for i, itm in enumerate(items, 1):
            itm["idx"] = i

    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return matched_items


class StudioRequestHandler(BaseHTTPRequestHandler):
    server_version = "MusicVideoStudio/1.0"

    def log_message(self, format, *args):
        # Silence routine static asset logs to keep console clean
        if "/media/" in self.path or "/static/" in self.path:
            return
        super().log_message(format, *args)

    def send_json(self, data: dict | list, status=HTTPStatus.OK):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self.send_json({"error": message}, status=status)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 1. API: List Projects
        if path == "/api/projects":
            self.handle_list_projects()
            return

        # 2. API: Project Details & Queue
        if path.startswith("/api/project/") and path.endswith("/queue"):
            slug = unquote(path[len("/api/project/"): -len("/queue")].strip("/"))
            self.handle_get_project_queue(slug)
            return

        if path.startswith("/api/project/"):
            slug = unquote(path[len("/api/project/"):].strip("/"))
            config_name = parse_qs(parsed.query).get("config", [None])[0]
            self.handle_get_project(slug, config_name=config_name)
            return

        # 3. API: Model Profiles
        if path == "/api/models":
            self.handle_get_models()
            return

        # 3b. API: Dummy Render Mode setting
        if path == "/api/settings/dummy_mode":
            global dummy_render_mode
            self.send_json({"success": True, "dummy_render": dummy_render_mode})
            return

        # 4. API: Render Status
        if path == "/api/render/status":
            with render_lock:
                st = dict(render_status)
                st["history_count"] = len(log_history)
            self.send_json(st)
            return

        # 5. API: Log Stream (Server-Sent Events)
        if path == "/api/render/events":
            self.handle_sse_stream()
            return

        # 6. Media Streaming (Audio, Video, Images with Range support)
        if path.startswith("/media/"):
            self.handle_media_stream(path[len("/media/"):])
            return

        # 7. Static UI Files
        self.handle_static_file(path)

    def do_POST(self):
        global active_process
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()

        # Create new project
        if path == "/api/projects/new":
            name = (body or {}).get("name", "").strip()
            if not name:
                self.send_error_json("Project name is required")
                return
            slug = re.sub(r"[^\w\-]+", "_", name).strip("_").lower()
            proj_dir = PROJECTS_DIR / slug
            if proj_dir.exists():
                self.send_error_json(f"Project '{slug}' already exists")
                return

            # Import runner's init logic
            sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
            try:
                import run_overnight_v3
                run_overnight_v3.init_new_project(slug, force=False)
            except Exception as e:
                self.send_error_json(f"Failed to create project: {e}")
                return

            self.send_json({"success": True, "slug": slug})
            return

        # Settings: Toggle Dummy Render Mode
        if path == "/api/settings/dummy_mode":
            global dummy_render_mode
            val = bool((body or {}).get("enabled", True))
            dummy_render_mode = val
            broadcast_log(f"[Studio UI Settings] Dummy render mode {'ENABLED' if dummy_render_mode else 'DISABLED'}")
            self.send_json({"success": True, "dummy_render": dummy_render_mode})
            return

        # Queue management: Move item up/down
        if path == "/api/queue/move":
            slug = (body or {}).get("slug")
            item_id = (body or {}).get("id")
            direction = (body or {}).get("direction", "up")
            if not slug or not item_id:
                self.send_error_json("slug and id are required")
                return

            proj_dir = PROJECTS_DIR / slug
            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if not queue_file.is_file():
                compute_fast_project_queue(slug)

            try:
                items = json.loads(queue_file.read_text(encoding="utf-8"))
                clean_id = str(item_id).replace(".mp4", "")
                idx = next((i for i, x in enumerate(items) if x.get("output_filename") in (item_id, clean_id) or x.get("filename") in (item_id, clean_id) or x.get("id") in (item_id, clean_id)), None)
                if idx is not None:
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
                        items[idx], items[swap_idx] = items[swap_idx], items[idx]
                        for i, itm in enumerate(items, 1):
                            itm["idx"] = i
                        queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                self.send_json({"success": True, "queue": items})
                return
            except Exception as e:
                self.send_error_json(str(e))
                return


        # Queue management: Remove item from queue
        if path == "/api/queue/remove":
            slug = (body or {}).get("slug")
            item_id = (body or {}).get("id")
            if not slug or not item_id:
                self.send_error_json("slug and id are required")
                return

            proj_dir = PROJECTS_DIR / slug
            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if not queue_file.is_file():
                compute_fast_project_queue(slug)

            try:
                items = json.loads(queue_file.read_text(encoding="utf-8"))
                clean_id = str(item_id).replace(".mp4", "")
                for item in items:
                    if item.get("output_filename") in (item_id, clean_id) or item.get("filename") in (item_id, clean_id) or item.get("id") in (item_id, clean_id):
                        item["status"] = "skipped"
                        item["needs_render"] = False
                        break
                queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                self.send_json({"success": True, "queue": items})
                return
            except Exception as e:
                self.send_error_json(str(e))
                return

        # Queue management: Enqueue single item
        if path == "/api/queue/enqueue":
            slug = (body or {}).get("slug")
            item_id = (body or {}).get("id")
            if not slug or not item_id:
                self.send_error_json("slug and id are required")
                return

            proj_dir = PROJECTS_DIR / slug
            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if not queue_file.is_file():
                compute_fast_project_queue(slug)

            try:
                items = json.loads(queue_file.read_text(encoding="utf-8"))
                clean_id = str(item_id).replace(".mp4", "")
                for item in items:
                    if item.get("output_filename") in (item_id, clean_id) or item.get("filename") in (item_id, clean_id) or item.get("id") in (item_id, clean_id):
                        item["status"] = "queued"
                        item["needs_render"] = True
                        item["reason"] = "FORCED"
                        break
                queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                self.send_json({"success": True, "queue": items})
                return
            except Exception as e:
                self.send_error_json(str(e))
                return


        # Queue management: Enqueue all unrendered
        if path == "/api/queue/enqueue_all":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("slug is required")
                return
            queue = compute_fast_project_queue(slug, force_recheck=True)
            self.send_json({"success": True, "queue": queue})
            return

        # Queue management: Clear queue
        if path == "/api/queue/clear":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("slug is required")
                return
            proj_dir = PROJECTS_DIR / slug
            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if queue_file.is_file():
                try:
                    items = json.loads(queue_file.read_text(encoding="utf-8"))
                    for item in items:
                        if item.get("status") == "queued":
                            item["status"] = "skipped"
                            item["needs_render"] = False
                    queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")
                    self.send_json({"success": True, "queue": items})
                    return
                except Exception as e:
                    self.send_error_json(str(e))
                    return
            self.send_json({"success": True, "queue": []})
            return

        # Direct Restitch Endpoint (runs immediately without blocking or waiting for render queue)
        if path == "/api/stitch":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("Project slug is required")
                return

            proj_dir = PROJECTS_DIR / slug
            jsons = sorted(proj_dir.glob("*.json"))
            if not jsons:
                self.send_error_json(f"No project config found in {proj_dir}")
                return
            config_file = str(jsons[-1])

            threading.Thread(target=run_stitch_worker, args=(config_file, slug), daemon=True).start()
            self.send_json({"success": True, "message": "Master video restitching started immediately."})
            return

        # Start render: Single-submission queue execution
        if path == "/api/render/start":
            slug = (body or {}).get("slug")
            args = (body or {}).get("args", [])
            if not slug:
                self.send_error_json("Project slug is required")
                return

            proj_dir = PROJECTS_DIR / slug
            jsons = sorted(proj_dir.glob("*.json"))
            if not jsons:
                self.send_error_json(f"No project config found in {proj_dir}")
                return
            config_file = jsons[-1]

            # Direct restitch request
            if args == ["stitch"]:
                threading.Thread(target=run_stitch_worker, args=(str(config_file), slug), daemon=True).start()
                self.send_json({"success": True, "queued": False, "args": args})
                return

            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if not queue_file.is_file():
                compute_fast_project_queue(slug)

            # Targeted scene render (e.g. clicking "Render Scene")
            if args:
                target_sid = str(args[0]).strip().upper()
                try:
                    prepare_targeted_queue_items(slug, target_sid)
                except Exception as e:
                    broadcast_log(f"[Studio UI Error] Failed preparing queue for scene {target_sid}: {e}")
            else:
                # "Render Missing / Changed": ensure unrendered or edited passes are evaluated and queued
                try:
                    items = compute_fast_project_queue(slug, force_recheck=True)
                    has_any_queued = any(x.get("status") == "queued" and x.get("needs_render") for x in items)
                    if not has_any_queued:
                        broadcast_log("[Studio UI Queue] All passes up-to-date. Nothing to render.")
                        self.send_json({"success": True, "queued": False, "message": "All passes up-to-date. Nothing to render."})
                        return
                except Exception as e:
                    broadcast_log(f"[Studio UI Error] Failed preparing queue for Render Missing / Changed: {e}")

            # Start single-render queue worker
            run_queue_worker(slug, force_restart=bool(args))
            self.send_json({"success": True, "queued": True, "message": f"Queue running (target: {args or 'all pending'})."})
            return

        # Cancel active render
        if path == "/api/render/stop":
            kill_active_render()
            self.send_json({"success": True, "stopped": True})
            return

        self.send_error_json("Endpoint not found", status=HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/project/"):
            slug = unquote(path[len("/api/project/"):].strip("/"))
            config_name = parse_qs(parsed.query).get("config", [None])[0]
            body = self.read_json_body()
            if not body or "project" not in body:
                self.send_error_json("Invalid project JSON payload")
                return

            proj_dir = PROJECTS_DIR / slug
            if not proj_dir.is_dir():
                self.send_error_json(f"Project folder '{slug}' not found", status=HTTPStatus.NOT_FOUND)
                return

            target_json = None
            if config_name:
                cand = proj_dir / config_name
                if cand.exists():
                    target_json = cand
            if target_json is None:
                jsons = sorted(proj_dir.glob("*.json"))
                target_json = jsons[-1] if jsons else proj_dir / f"{slug}.json"

            # Always back up before saving
            backup_project_json(target_json)

            try:
                target_json.write_text(json.dumps(body, indent=2), encoding="utf-8")
                self.send_json({"success": True, "saved": str(target_json.name)})
            except Exception as e:
                self.send_error_json(f"Failed to write file: {e}")
            return

        self.send_error_json("Endpoint not found", status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return None
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    def handle_list_projects(self):
        projects = []
        if PROJECTS_DIR.is_dir():
            for d in sorted(PROJECTS_DIR.iterdir()):
                if d.is_dir():
                    jsons = [j for j in sorted(d.glob("*.json")) if not j.name.startswith("state_hashes")]
                    for j in jsons:
                        try:
                            data = json.loads(j.read_text(encoding="utf-8"))
                            pcfg = data.get("project", {})
                            display_name = pcfg.get("name") or d.name.replace("_", " ").title()
                            projects.append({
                                "slug": d.name,
                                "name": display_name,
                                "label": f"{display_name} ({j.name})",
                                "config_file": j.name,
                                "fps": pcfg.get("fps", 24),
                                "total_duration": pcfg.get("total_duration", 0),
                                "anchors_count": len(data.get("anchors", [])),
                                "scenes_count": len(data.get("scenes", [])),
                                "markers_count": len(data.get("markers", [])),
                                "master_output": pcfg.get("master_output", ""),
                            })
                        except Exception:
                            projects.append({
                                "slug": d.name,
                                "name": d.name,
                                "label": f"{d.name} ({j.name})",
                                "config_file": j.name,
                                "error": "Failed to parse JSON",
                            })
        self.send_json({"projects": projects})

    def handle_get_project_queue(self, slug: str):
        proj_dir = PROJECTS_DIR / slug
        if not proj_dir.is_dir():
            self.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
            return

        queue = compute_fast_project_queue(slug)
        self.send_json({"success": True, "queue": queue})

    def handle_get_project(self, slug: str, config_name: str | None = None):
        proj_dir = PROJECTS_DIR / slug
        if not proj_dir.is_dir():
            self.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
            return

        jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
        if not jsons:
            self.send_error_json(f"No project config found in {slug}", status=HTTPStatus.NOT_FOUND)
            return

        cfg_file = None
        if config_name:
            for j in jsons:
                if j.name == config_name:
                    cfg_file = j
                    break
        if cfg_file is None:
            cfg_file = jsons[-1]
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            self.send_error_json(f"Error parsing {cfg_file.name}: {e}")
            return

        # Scan on-disk status for anchors, renders, master videos
        work_dir = proj_dir / "work"
        anchors_dir = proj_dir / data.get("project", {}).get("directories", {}).get("anchors", "work/anchors")
        renders_dir = proj_dir / data.get("project", {}).get("directories", {}).get("renders", "work/renders")

        # Collect existing anchor images (match by both filename and canonical ID like A01)
        existing_anchors = {}
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        anchor_search_dirs = [
            anchors_dir,
            proj_dir / "work" / "anchors_v3",
            proj_dir / "work" / "anchors",
            proj_dir / "references",
            proj_dir / "work" / "ref_sheets",
        ]
        scanned_dirs = set()
        for sdir in anchor_search_dirs:
            if sdir.is_dir():
                resolved_sdir = str(sdir.resolve())
                if resolved_sdir in scanned_dirs:
                    continue
                scanned_dirs.add(resolved_sdir)
                for img in sdir.iterdir():
                    if img.is_file() and any(img.name.lower().endswith(ext) for ext in image_exts):
                        aid = extract_anchor_id(img.name)
                        info = {
                            "filename": img.name,
                            "aid": aid,
                            "size_bytes": img.stat().st_size,
                            "mtime": img.stat().st_mtime,
                        }
                        if img.name not in existing_anchors:
                            existing_anchors[img.name] = info
                        # Map aid (e.g. A01) -> info, prioritizing primary anchors_dir
                        if aid and (aid not in existing_anchors or sdir == anchors_dir):
                            existing_anchors[aid] = info

        # Collect rendered clips across renders_dir and trimmed_dir
        existing_clips = collect_project_clips(proj_dir, data)
        strips_dir = proj_dir / data.get("project", {}).get("directories", {}).get("review_strips", "work/frames_v3")
        strips_dir.mkdir(parents=True, exist_ok=True)

        # Collect existing filmstrip images
        existing_strips = {}
        if strips_dir.is_dir():
            for s in strips_dir.glob("*_strip.jpg"):
                raw_sid = s.stem.replace("_strip", "")
                strip_info = {
                    "filename": s.name,
                    "mtime": s.stat().st_mtime,
                }
                for sid_variant in (raw_sid, raw_sid.upper(), raw_sid.lower()):
                    existing_strips[sid_variant] = strip_info

        # Collect master outputs
        master_videos = []
        for vid in sorted(proj_dir.glob("*Master*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            master_videos.append({
                "filename": vid.name,
                "size_mb": round(vid.stat().st_size / (1024 * 1024), 2),
                "mtime": vid.stat().st_mtime,
            })

        # Scan available audio files
        audio_dir = proj_dir / "audio"
        audio_files = []
        if audio_dir.is_dir():
            for a in audio_dir.glob("*.*"):
                if a.suffix.lower() in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
                    audio_files.append(a.name)

        # Scan reference images
        ref_dir = proj_dir / "references"
        ref_files = []
        if ref_dir.is_dir():
            for r in ref_dir.glob("*.*"):
                if r.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    ref_files.append(r.name)

        self.send_json({
            "slug": slug,
            "config_file": cfg_file.name,
            "data": data,
            "assets": {
                "anchors": existing_anchors,
                "clips": existing_clips,
                "strips": existing_strips,
                "master_videos": master_videos,
                "audio_files": audio_files,
                "reference_files": ref_files,
            },
        })

    def handle_get_models(self):
        data = {}
        if PROFILES_FILE.is_file():
            try:
                data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.send_json(data)

    def handle_sse_stream(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Send existing log history first
        with render_lock:
            existing = list(log_history)
            is_running = render_status["running"] or queue_worker_active
            exit_code = render_status["exit_code"]

        last_seen_seq = 0
        for seq, line in existing:
            payload = json.dumps({"text": line, "done": False})
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            last_seen_seq = seq
        self.wfile.flush()

        has_sent_done = not is_running and exit_code is not None
        ping_timer = 0.0

        # Stream new lines until client disconnects
        while True:
            try:
                time.sleep(0.15)
                ping_timer += 0.15
                with render_lock:
                    is_running = render_status["running"] or queue_worker_active
                    exit_code = render_status["exit_code"]
                    # If log_history was cleared or reset behind us
                    if log_history and log_history[-1][0] < last_seen_seq:
                        last_seen_seq = 0
                    new_items = [item for item in log_history if item[0] > last_seen_seq]

                if new_items:
                    for seq, line in new_items:
                        payload = json.dumps({"text": line, "done": False})
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        last_seen_seq = seq
                    self.wfile.flush()

                if is_running:
                    has_sent_done = False
                elif exit_code is not None and not has_sent_done:
                    has_sent_done = True
                    payload = json.dumps({
                        "text": f"[Status] Process stopped (Exit code {exit_code})",
                        "done": True,
                        "exit_code": exit_code,
                    })
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()

                # Send periodic keep-alive ping comment
                if ping_timer >= 15.0:
                    ping_timer = 0.0
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                break

    def handle_media_stream(self, subpath: str):
        subpath = unquote(subpath)
        parts = subpath.strip("/").split("/")
        if len(parts) < 2:
            self.send_error_json("Invalid media path", status=HTTPStatus.NOT_FOUND)
            return

        slug = parts[0]
        proj_dir = PROJECTS_DIR / slug
        if not proj_dir.is_dir():
            self.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND)
            return

        target_file: Path | None = None
        category = parts[1]

        if category == "audio" and len(parts) >= 3:
            rel = "/".join(parts[2:])
            target_file = proj_dir / "audio" / rel
            if not target_file.exists():
                target_file = proj_dir / rel
        elif category == "anchor" and len(parts) >= 3:
            raw_target = "/".join(parts[2:])
            filename = Path(raw_target).name
            aid = extract_anchor_id(filename)

            cfg_anchors_dir = None
            jsons = sorted(proj_dir.glob("*.json"))
            if jsons:
                try:
                    pcfg = json.loads(jsons[-1].read_text(encoding="utf-8")).get("project", {})
                    dpath = pcfg.get("directories", {}).get("anchors")
                    if dpath:
                        cfg_anchors_dir = proj_dir / dpath
                except Exception:
                    pass

            primary_anchor_dirs = [d for d in [cfg_anchors_dir, proj_dir / "work" / "anchors_v3", proj_dir / "work" / "anchors"] if d and d.is_dir()]
            ref_dirs = [d for d in [proj_dir / "references", proj_dir / "work" / "ref_sheets"] if d and d.is_dir()]
            image_exts = {".jpg", ".jpeg", ".png", ".webp"}

            # 1. Primary check: If canonical ID like 'A01' is known, prioritize active file in anchors_dir
            if aid:
                for cdir in primary_anchor_dirs:
                    for img in sorted(cdir.iterdir()):
                        if img.is_file() and any(img.name.lower().endswith(ext) for ext in image_exts):
                            if extract_anchor_id(img.name) == aid:
                                target_file = img
                                break
                    if target_file is not None:
                        break

            # 2. Exact filename check across primary anchor folders
            if target_file is None:
                for cdir in primary_anchor_dirs:
                    cand = cdir / filename
                    if cand.is_file():
                        target_file = cand
                        break

            # 3. Check reference directories as fallback
            if target_file is None:
                for cdir in ref_dirs:
                    cand = cdir / filename
                    if cand.is_file():
                        target_file = cand
                        break
                    if aid:
                        for img in sorted(cdir.iterdir()):
                            if img.is_file() and any(img.name.lower().endswith(ext) for ext in image_exts):
                                if extract_anchor_id(img.name) == aid:
                                    target_file = img
                                    break
                    if target_file is not None:
                        break
        elif category == "clip" and len(parts) >= 3:
            filename = parts[2]
            candidates = [
                proj_dir / "work" / "renders_v3" / filename,
                proj_dir / "work" / "renders" / filename,
                proj_dir / "work" / "trimmed_v3" / filename,
                proj_dir / "work" / "trimmed" / filename,
            ]
            for cand in candidates:
                if cand.exists():
                    target_file = cand
                    break

            # If not found directly, try matching by scene ID prefix/pattern
            if target_file is None:
                sid = Path(filename).stem.upper()
                for cdir in [proj_dir / "work" / "trimmed_v3", proj_dir / "work" / "renders_v3"]:
                    if cdir.is_dir():
                        for c in sorted(cdir.glob("*.mp4"), key=lambda x: len(x.name)):
                            if re.search(rf"(?:^|_)({re.escape(sid)})(?:[_\.]|$)", c.name, re.IGNORECASE):
                                target_file = c
                                break
                    if target_file is not None:
                        break

        elif category == "strip" and len(parts) >= 3:
            raw_sid = Path(parts[2]).stem.replace("_strip", "")
            strips_dir = proj_dir / "work" / "frames_v3"
            strips_dir.mkdir(parents=True, exist_ok=True)

            # Check if strip file already exists (case-insensitive)
            strip_file = None
            if strips_dir.is_dir():
                for s in strips_dir.glob("*_strip.jpg"):
                    if s.stem.replace("_strip", "").lower() == raw_sid.lower():
                        strip_file = s
                        break

            # Find matching clip across all clip folders, sorted by modification time descending
            matched_clip = None
            candidate_clips = []
            for cdir in [
                proj_dir / "work" / "trimmed_v3",
                proj_dir / "work" / "trimmed",
                proj_dir / "work" / "renders_v3",
                proj_dir / "work" / "renders",
            ]:
                if cdir.is_dir():
                    for c in cdir.glob("*.mp4"):
                        if c.name.endswith(".tmp.mp4"):
                            continue
                        if re.search(rf"(?:^|_)({re.escape(raw_sid)})(?:[_\.]|$)", c.name, re.IGNORECASE):
                            candidate_clips.append(c)

            if candidate_clips:
                candidate_clips.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                matched_clip = candidate_clips[0]

            # Re-generate strip if strip does not exist OR if video clip is newer than existing strip
            needs_regen = False
            if matched_clip:
                if not strip_file or not strip_file.exists():
                    needs_regen = True
                elif matched_clip.stat().st_mtime > strip_file.stat().st_mtime:
                    needs_regen = True

            if needs_regen and matched_clip:
                ffmpeg_bin = WORKSPACE_ROOT / "tools" / "ffmpeg.exe"
                if ffmpeg_bin.exists():
                    out_cand = strips_dir / f"{raw_sid}_strip.jpg"
                    try:
                        cmd = [
                            str(ffmpeg_bin), "-y", "-i", str(matched_clip),
                            "-vf", "fps=5/5.0,scale=-1:144,tile=5x1",
                            "-frames:v", "1", "-q:v", "2", str(out_cand)
                        ]
                        subprocess.run(cmd, capture_output=True, timeout=10)
                        if out_cand.exists():
                            strip_file = out_cand
                    except Exception as e:
                        print(f"[Strip Regen Error] {e}")

            if strip_file and strip_file.exists():
                target_file = strip_file
        elif category == "master":
            # Return latest master video
            masters = sorted(proj_dir.glob("*Master*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if masters:
                target_file = masters[0]
        else:
            # Direct path relative to project
            target_file = proj_dir / "/".join(parts[1:])

        if not target_file or not target_file.is_file():
            self.send_error_json(f"File not found: {subpath}", status=HTTPStatus.NOT_FOUND)
            return

        self.stream_file_with_range(target_file)

    def stream_file_with_range(self, file_path: Path):
        file_size = file_path.stat().st_size
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1

                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()

                try:
                    with open(file_path, "rb") as f:
                        f.seek(start)
                        bytes_remaining = length
                        chunk_size = 64 * 1024
                        while bytes_remaining > 0:
                            chunk = f.read(min(chunk_size, bytes_remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            bytes_remaining -= len(chunk)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass
                return

        # Normal full file transfer
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

        try:
            with open(file_path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=64 * 1024)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def handle_static_file(self, raw_path: str):
        rel_path = raw_path.strip("/")
        if not rel_path or rel_path == "index.html":
            file_path = STATIC_DIR / "index.html"
        else:
            file_path = (STATIC_DIR / rel_path).resolve()

        # Security check: prevent directory traversal outside static dir
        try:
            file_path.relative_to(STATIC_DIR)
        except ValueError:
            self.send_error_json("Access denied", status=HTTPStatus.FORBIDDEN)
            return

        if not file_path.is_file():
            self.send_error_json("Asset not found", status=HTTPStatus.NOT_FOUND)
            return

        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "text/plain"
        if file_path.suffix in (".html", ".css", ".js"):
            mime_type += "; charset=utf-8"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


def run_server(port: int = 7860):
    host = "127.0.0.1"
    server = ThreadingHTTPServer((host, port), StudioRequestHandler)
    print("=" * 76)
    print(f"  Music Video Generation Studio - UI Server")
    print("=" * 76)
    print(f"  URL        : http://{host}:{port}")
    print(f"  Projects   : {PROJECTS_DIR}")
    print(f"  Profiles   : {PROFILES_FILE}")
    print(f"  Root       : {WORKSPACE_ROOT}")
    print("=" * 76)
    print("Press Ctrl+C to stop the server.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    port = 7860
    for i, a in enumerate(sys.argv[1:]):
        if a in ("--port", "-p") and i + 1 < len(sys.argv[1:]):
            port = int(sys.argv[1:][i + 1])
    if "--dummy" in sys.argv[1:]:
        dummy_render_mode = True
    run_server(port)

