"""Music Video Generation Studio - Local Web UI Server.

Zero external dependencies - powered entirely by Python standard library:
- ThreadingHTTPServer for concurrent requests & SSE streaming
- HTTP 206 Partial Content (Range requests) for smooth audio & video scrubbing
- Subprocess manager with real-time log broadcast
- Automated timestamped backup on every save
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Module aliases
if "server" not in sys.modules:
    sys.modules["server"] = sys.modules[__name__]
if "work.ui.server" not in sys.modules:
    sys.modules["work.ui.server"] = sys.modules[__name__]

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
    from run_overnight_v3 import (
        ProjectContext,
        compute_project_task_hashes,
        resolve_rendered_clip_path,
        find_local_config,
        auto_detect_wgp_root,
    )
except ImportError:
    ProjectContext = None
    compute_project_task_hashes = None
    resolve_rendered_clip_path = None
    find_local_config = None
    auto_detect_wgp_root = None


def resolve_engine_python() -> str:
    """Finds the best Python interpreter for GPU rendering jobs.
    Checks environment variables, local machine config, Pinokio auto-detection,
    and falls back to current running Python.
    """
    env_py = os.environ.get("PINOKIO_PY") or os.environ.get("WGP_PYTHON") or os.environ.get("ENGINE_PYTHON")
    if env_py and Path(env_py).is_file():
        return str(Path(env_py).resolve())

    if find_local_config:
        try:
            cfg = find_local_config(WORKSPACE_ROOT)
            cfg_py = cfg.get("python_path") or cfg.get("python") or cfg.get("wgp_python")
            if cfg_py and Path(cfg_py).is_file():
                return str(Path(cfg_py).resolve())
            cfg_wgp = cfg.get("wgp_root") or cfg.get("environment", {}).get("wgp_root")
            if cfg_wgp:
                for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
                    cand = Path(cfg_wgp) / rel
                    if cand.is_file():
                        return str(cand.resolve())
        except Exception:
            pass

    if auto_detect_wgp_root:
        try:
            detected = auto_detect_wgp_root()
            if detected:
                for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
                    cand = detected / rel
                    if cand.is_file():
                        return str(cand.resolve())
        except Exception:
            pass

    return sys.executable


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
queue_worker_thread = None
current_worker_id = 0
last_backup_times: dict[str, float] = {}
dummy_render_mode: bool = bool(os.environ.get("DUMMY_RENDER", "0") in ("1", "true", "yes"))


def broadcast_log(line: str):
    global log_seq_counter
    with render_lock:
        log_seq_counter += 1
        log_history.append((log_seq_counter, line))
        if len(log_history) > 3000:
            log_history.pop(0)


def read_project_engine_status(slug: str | None) -> dict | None:
    """Reads the live structured Wan2GP engine_status.json for a project if present."""
    if not slug:
        return None
    proj_dir = PROJECTS_DIR / slug
    for jobs_sub in ("work/jobs_v3", "work/jobs"):
        st_file = proj_dir / jobs_sub / "engine_status.json"
        if st_file.is_file():
            try:
                data = json.loads(st_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


# Submodule re-exports
try:
    from work.ui.server_workers import (
        derive_scene_id,
        derive_target_arg,
        execute_single_render,
        kill_active_render,
        run_anchor_worker,
        run_pipeline_worker,
        run_queue_worker,
        run_stitch_worker,
        stitch_in_progress,
        stitch_lock,
    )
    from work.ui.server_queue import (
        backup_project_json,
        clear_project_queue,
        collect_project_clips,
        compute_fast_project_queue,
        delete_scene_in_project_data,
        derive_split_scene_ids,
        enqueue_in_project_queue,
        extract_anchor_id,
        is_scene_match,
        move_in_project_queue,
        prepare_targeted_queue_items,
        remove_from_project_queue,
        resolve_project_resolution,
        split_scene_in_project_data,
    )
    from work.ui.server_routes import (
        StudioRequestHandler,
        run_server,
    )
except ImportError:
    from server_workers import (
        derive_scene_id,
        derive_target_arg,
        execute_single_render,
        kill_active_render,
        run_anchor_worker,
        run_pipeline_worker,
        run_queue_worker,
        run_stitch_worker,
        stitch_in_progress,
        stitch_lock,
    )
    from server_queue import (
        backup_project_json,
        clear_project_queue,
        collect_project_clips,
        compute_fast_project_queue,
        delete_scene_in_project_data,
        derive_split_scene_ids,
        enqueue_in_project_queue,
        extract_anchor_id,
        is_scene_match,
        move_in_project_queue,
        prepare_targeted_queue_items,
        remove_from_project_queue,
        resolve_project_resolution,
        split_scene_in_project_data,
    )
    from server_routes import (
        StudioRequestHandler,
        run_server,
    )

__all__ = [
    "WORKSPACE_ROOT",
    "STATIC_DIR",
    "PROJECTS_DIR",
    "BACKUP_DIR",
    "RUNNER_SCRIPT",
    "PROFILES_FILE",
    "TEMPLATE_FILE",
    "TEMPLATES_FILE",
    "ProjectContext",
    "compute_project_task_hashes",
    "resolve_rendered_clip_path",
    "find_local_config",
    "auto_detect_wgp_root",
    "resolve_engine_python",
    "render_lock",
    "active_process",
    "log_subscribers",
    "log_seq_counter",
    "log_history",
    "render_status",
    "render_job_queue",
    "queue_worker_active",
    "queue_worker_thread",
    "current_worker_id",
    "last_backup_times",
    "dummy_render_mode",
    "broadcast_log",
    "read_project_engine_status",
    "derive_scene_id",
    "derive_target_arg",
    "kill_active_render",
    "execute_single_render",
    "run_anchor_worker",
    "run_stitch_worker",
    "stitch_lock",
    "stitch_in_progress",
    "run_pipeline_worker",
    "run_queue_worker",
    "extract_anchor_id",
    "backup_project_json",
    "resolve_project_resolution",
    "collect_project_clips",
    "compute_fast_project_queue",
    "derive_split_scene_ids",
    "split_scene_in_project_data",
    "delete_scene_in_project_data",
    "is_scene_match",
    "move_in_project_queue",
    "remove_from_project_queue",
    "enqueue_in_project_queue",
    "clear_project_queue",
    "prepare_targeted_queue_items",
    "StudioRequestHandler",
    "run_server",
]


if __name__ == "__main__":
    port = 7860
    for i, a in enumerate(sys.argv[1:]):
        if a in ("--port", "-p") and i + 1 < len(sys.argv[1:]):
            port = int(sys.argv[1:][i + 1])
    if "--dummy" in sys.argv[1:]:
        dummy_render_mode = True
    run_server(port)
