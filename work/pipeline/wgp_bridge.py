import json
import os
import sys
import time
from pathlib import Path

from .context import ProjectContext


class LazyWgpSession:
    """Initializes the Wan2GP GPU session only when at least one GPU task needs to run."""

    def __init__(self, ctx: ProjectContext):
        self.ctx = ctx
        self._session = None

    def get(self):
        if self._session is None:
            sys.path.insert(0, str(self.ctx.wgp_root))
            os.chdir(self.ctx.wgp_root)
            from shared.api import init

            self._session = init(
                root=self.ctx.wgp_root,
                config_path=self.ctx.wgp_root / "wgp_config.json",
                output_dir=self.ctx.renders_dir,
                console_output=True,
                console_isatty=False,
            )
        return self._session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


def write_engine_status(ctx: ProjectContext, payload: dict, merge: bool = True) -> dict:
    """Persists a structured snapshot of the active Wan2GP engine state to jobs_dir/engine_status.json."""
    status_file = ctx.jobs_dir / "engine_status.json"
    current: dict = {}
    if merge and status_file.exists():
        try:
            loaded = json.loads(status_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except Exception:
            current = {}
    current.update(payload)
    current["updated_at"] = round(time.time(), 3)
    try:
        ctx.jobs_dir.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:
        pass
    return current


def check_cancel_requested(ctx: ProjectContext) -> bool:
    """Checks whether the Studio UI or caller has written a cancel_request.json flag."""
    cancel_file = ctx.jobs_dir / "cancel_request.json"
    return cancel_file.exists()


def clear_cancel_request(ctx: ProjectContext):
    cancel_file = ctx.jobs_dir / "cancel_request.json"
    if cancel_file.exists():
        try:
            cancel_file.unlink()
        except OSError:
            pass


def format_progress_update(p, idx: int, filename: str, session=None) -> dict:
    """Normalizes a Wan2GP ProgressUpdate across all engine phases and sliding windows."""
    phase = getattr(p, "phase", None) or "inference"
    raw_phase = getattr(p, "raw_phase", None) or phase
    status_text = getattr(p, "status", None) or raw_phase
    step = getattr(p, "current_step", None)
    total_steps = getattr(p, "total_steps", None)
    win_idx = getattr(p, "window_index", None)
    win_cnt = getattr(p, "window_count", None)
    raw_pct = float(getattr(p, "progress", 0.0) or 0.0)

    # Smooth monotonic progress across multi-window generations (where each window spans 20%..85%)
    if win_cnt and int(win_cnt) > 1 and win_idx is not None and 20.0 <= raw_pct <= 85.0:
        win_frac = max(0.0, min(1.0, (raw_pct - 20.0) / 65.0))
        pct = round(20.0 + 65.0 * (((max(1, int(win_idx)) - 1) + win_frac) / int(win_cnt)), 1)
    else:
        pct = round(raw_pct, 1)

    wgp_q = None
    if session is not None and hasattr(session, "list_queue"):
        try:
            q_info = session.list_queue()
            if isinstance(q_info, dict):
                wgp_q = {
                    "total_count": q_info.get("total_count", 0),
                    "queued_count": q_info.get("queued_count", 0),
                    "running_count": q_info.get("running_count", 0),
                }
        except Exception:
            wgp_q = None

    payload = {
        "idx": idx,
        "filename": filename,
        "phase": phase,
        "raw_phase": raw_phase,
        "status_text": status_text,
        "step": step,
        "total_steps": total_steps,
        "pct": pct,
        "window_pct": round(raw_pct, 1),
        "window_index": win_idx,
        "window_count": win_cnt,
    }
    if wgp_q is not None:
        payload["wgp_queue"] = wgp_q
    return payload


def execute_wgp_job(
    ctx: ProjectContext,
    session,
    task_cfg: dict,
    idx: int,
    total: int,
    filename: str,
    stage: str,
    reason: str,
    model: str = "",
    frames: int = 0,
    resolution: str = "",
    on_progress_item: dict | None = None,
    sync_queue_fn=None,
):
    """Submits a task to WanGPSession, streams all structured ProgressUpdate phases/windows to
    engine_status.json and stdout, and honours graceful mid-flight cancellation via cancel_request.json."""
    t0 = time.time()
    write_engine_status(
        ctx,
        {
            "state": "rendering",
            "stage": stage,
            "idx": idx,
            "total": total,
            "filename": filename,
            "reason": reason,
            "model": model,
            "frames": frames,
            "resolution": resolution,
            "phase": "starting",
            "raw_phase": "Submitting task to Wan2GP...",
            "status_text": "Submitting task to Wan2GP...",
            "step": None,
            "total_steps": None,
            "pct": 0.0,
            "window_index": None,
            "window_count": None,
            "started_at": round(t0, 3),
            "error": None,
        },
        merge=False,
    )

    job = session.submit_task(task_cfg)
    cancelled_by_user = False
    last_emitted_sig = None

    for event in job.events.iter(timeout=0.25):
        if not cancelled_by_user and check_cancel_requested(ctx):
            cancelled_by_user = True
            try:
                job.cancel()
            except Exception:
                pass
            if hasattr(session, "cancel"):
                try:
                    session.cancel()
                except Exception:
                    pass
            write_engine_status(
                ctx,
                {
                    "state": "cancelling",
                    "phase": "cancelled",
                    "raw_phase": "Stopping render...",
                    "status_text": "Cancellation requested by user",
                },
            )
            print(
                f"[QUEUE_CANCEL] {json.dumps({'idx': idx, 'filename': filename, 'reason': 'STOPPED BY USER'})}",
                flush=True,
            )

        if event.kind == "progress":
            p = event.data
            prog = format_progress_update(p, idx, filename, session=session)
            sig = (
                prog["phase"],
                prog["raw_phase"],
                prog["step"],
                prog["total_steps"],
                prog["pct"],
                prog["window_index"],
                prog["window_count"],
            )
            if sig == last_emitted_sig:
                continue
            last_emitted_sig = sig

            if prog["total_steps"] and prog["step"] in (1, prog["total_steps"] // 2, prog["total_steps"]):
                win_prefix = f"[Win {prog['window_index']}/{prog['window_count']}] " if (prog["window_count"] and prog["window_count"] > 1 and prog["window_index"]) else ""
                print(
                    f"      {win_prefix}[{prog['phase']}] {prog['step']}/{prog['total_steps']} ({prog['pct']:.0f}%)",
                    flush=True,
                )
            elif not prog["total_steps"]:
                print(
                    f"      [{prog['phase']}] {prog['raw_phase']} ({prog['pct']:.0f}%)",
                    flush=True,
                )

            print(f"\n[QUEUE_PROGRESS] {json.dumps(prog)}", flush=True)
            write_engine_status(ctx, prog)

            if on_progress_item is not None:
                on_progress_item["pct"] = prog["pct"]
                on_progress_item["phase"] = prog["phase"]
                on_progress_item["raw_phase"] = prog["raw_phase"]
                on_progress_item["step"] = prog["step"]
                on_progress_item["total_steps"] = prog["total_steps"]
                on_progress_item["window_index"] = prog["window_index"]
                on_progress_item["window_count"] = prog["window_count"]
                if sync_queue_fn is not None:
                    sync_queue_fn()
        elif event.kind == "error":
            err_msg = getattr(event.data, "message", str(event.data))
            write_engine_status(ctx, {"error": [err_msg]})

    res = job.result()
    was_cancelled = cancelled_by_user or (
        not res.success and any("cancel" in getattr(e, "message", "").lower() for e in (res.errors or []))
    )
    return res, was_cancelled, t0
