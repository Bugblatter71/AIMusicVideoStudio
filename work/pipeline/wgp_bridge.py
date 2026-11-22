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


class SessionRouter(LazyWgpSession):
    """Routes pipeline render requests lazily between local Wan2GP, fal.ai cloud, and direct MiniMax API sessions."""

    def __init__(self, ctx: ProjectContext):
        super().__init__(ctx)
        self._fal_session = None
        self._minimax_session = None

    def get_fal(self):
        if self._fal_session is None:
            from .fal_bridge import FalSession

            self._fal_session = FalSession(self.ctx)
        return self._fal_session

    def get_minimax(self):
        if self._minimax_session is None:
            from .minimax_bridge import MiniMaxSession

            self._minimax_session = MiniMaxSession(self.ctx)
        return self._minimax_session

    def get_for_backend(self, backend: str = "local"):
        b = str(backend or "").strip().lower()
        if b in ("fal", "fal.ai", "cloud"):
            return self.get_fal()
        if b in ("minimax", "minimax_api", "hailuo", "platform.minimax.io"):
            return self.get_minimax()
        return self.get()

    def get_for_task(self, task_cfg: dict | None = None):
        b = (task_cfg or {}).get("_backend") or (task_cfg or {}).get("backend") or "local"
        return self.get_for_backend(str(b))

    def close(self):
        for s in (self._fal_session, self._minimax_session):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        self._fal_session = None
        self._minimax_session = None
        super().close()


def resolve_session_for_backend(lazy_session, backend: str = "local"):
    """Selects a concrete local, fal.ai, or MiniMax session cleanly without breaking mock LazyWgpSession objects."""
    b = str(backend or "local").strip().lower()
    if b in ("fal", "fal.ai", "cloud"):
        if hasattr(lazy_session, "get_fal"):
            return lazy_session.get_fal()
        if hasattr(lazy_session, "get_for_backend"):
            return lazy_session.get_for_backend("fal")
    elif b in ("minimax", "minimax_api", "hailuo", "platform.minimax.io"):
        if hasattr(lazy_session, "get_minimax"):
            return lazy_session.get_minimax()
        if hasattr(lazy_session, "get_for_backend"):
            return lazy_session.get_for_backend("minimax")
    if hasattr(lazy_session, "get_for_backend"):
        return lazy_session.get_for_backend("local")
    return lazy_session.get()


def _norm_engine_tag(raw: str | None) -> str:
    b = str(raw or "").strip().lower()
    if b in ("fal", "fal.ai", "cloud"):
        return "fal"
    if b in ("minimax", "minimax_api", "hailuo", "platform.minimax.io"):
        return "minimax"
    return "local"


def _write_status_file(status_file: Path, lock_file: Path, payload: dict, merge: bool) -> dict:
    from .media_utils import atomic_write_json, locked_file_op
    with locked_file_op(lock_file):
        cur: dict = {}
        if merge and status_file.exists():
            try:
                loaded = json.loads(status_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cur = loaded
            except Exception:
                cur = {}
        cur.update(payload)
        cur["updated_at"] = round(time.time(), 3)
        try:
            atomic_write_json(status_file, cur)
        except Exception:
            pass
        return cur


def _clean_task_stem(raw: str | None) -> str:
    s = Path(str(raw or "").strip()).stem if raw else ""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)[:96]


def write_engine_status(
    ctx: ProjectContext,
    payload: dict,
    merge: bool = True,
    backend: str | None = None,
    task_stem: str | None = None,
) -> dict:
    """Persists active engine snapshot to per-task/per-backend/legacy status files."""
    b = _norm_engine_tag(backend or (payload or {}).get("backend") or getattr(ctx, "cli_backend", ""))
    data = dict(payload or {})
    data.setdefault("backend", b)
    t_stem = _clean_task_stem(task_stem or data.get("output_filename") or data.get("filename"))
    if t_stem:
        _write_status_file(ctx.jobs_dir / f"engine_status_{b}__{t_stem}.json", ctx.jobs_dir / f"engine_status_{b}__{t_stem}.lock", data, merge)
    eng_res = _write_status_file(ctx.jobs_dir / f"engine_status_{b}.json", ctx.jobs_dir / f"engine_status_{b}.lock", data, merge)
    _write_status_file(ctx.jobs_dir / "engine_status.json", ctx.jobs_dir / "engine_status.lock", data, merge)
    return eng_res


def check_cancel_requested(ctx: ProjectContext, backend: str | None = None, task_stem: str | None = None) -> bool:
    """Checks whether task-specific, backend-specific, or global cancel_request exists."""
    b = _norm_engine_tag(backend or getattr(ctx, "cli_backend", ""))
    t_stem = _clean_task_stem(task_stem)
    if t_stem and (ctx.jobs_dir / f"cancel_request_{b}__{t_stem}.json").exists():
        return True
    return (ctx.jobs_dir / f"cancel_request_{b}.json").exists() or (ctx.jobs_dir / "cancel_request.json").exists()


def clear_cancel_request(ctx: ProjectContext, backend: str | None = None, task_stem: str | None = None, include_global: bool = False):
    b = _norm_engine_tag(backend or getattr(ctx, "cli_backend", ""))
    t_stem = _clean_task_stem(task_stem)
    if t_stem:
        targets = [ctx.jobs_dir / f"cancel_request_{b}__{t_stem}.json"]
        if include_global and ctx.jobs_dir.is_dir():
            own = {f"cancel_request_{b}__{t_stem}.json", f"cancel_request_{b}.json"}
            if not any(p.name not in own for p in ctx.jobs_dir.glob("cancel_request_*.json")):
                targets.append(ctx.jobs_dir / "cancel_request.json")
    elif backend is not None:
        targets = [ctx.jobs_dir / f"cancel_request_{b}.json"]
    else:
        targets = [ctx.jobs_dir / f"cancel_request_{b}.json", ctx.jobs_dir / "cancel_request.json"]
    for cf in targets:
        if cf.exists():
            try: cf.unlink()
            except OSError: pass


def format_progress_update(p, idx: int, filename: str, session=None) -> dict:
    """Normalizes a Wan2GP ProgressUpdate across all engine phases and sliding windows."""
    phase = getattr(p, "phase", None) or "inference"
    raw_phase = getattr(p, "raw_phase", None) or phase
    status_text = getattr(p, "status", None) or raw_phase
    step = getattr(p, "current_step", None)
    total_steps = getattr(p, "total_steps", None)
    win_idx = getattr(p, "window_index", None)
    win_cnt = getattr(p, "window_count", None)
    raw_p = getattr(p, "progress", None)
    if raw_p is None:
        pct = None
        win_pct = None
    else:
        raw_pct = float(raw_p)
        if win_cnt and int(win_cnt) > 1 and win_idx is not None and 20.0 <= raw_pct <= 85.0:
            win_frac = max(0.0, min(1.0, (raw_pct - 20.0) / 65.0))
            pct = round(20.0 + 65.0 * (((max(1, int(win_idx)) - 1) + win_frac) / int(win_cnt)), 1)
        else:
            pct = round(raw_pct, 1)
        win_pct = round(raw_pct, 1)

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
        "window_pct": win_pct,
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
    cls_name = getattr(session, "__class__", None).__name__
    raw_b = str((task_cfg or {}).get("_backend") or (task_cfg or {}).get("backend") or "").strip().lower()
    if raw_b in ("minimax", "minimax_api", "hailuo", "platform.minimax.io") or cls_name == "MiniMaxSession":
        backend_tag, submit_msg = "minimax", "Submitting task to MiniMax API..."
    elif raw_b in ("fal", "fal.ai", "cloud") or cls_name == "FalSession":
        backend_tag, submit_msg = "fal", "Submitting task to fal.ai..."
    else:
        backend_tag, submit_msg = "local", "Submitting task to Wan2GP..."
    t_stem = _clean_task_stem(filename or (task_cfg or {}).get("output_filename"))
    write_engine_status(
        ctx,
        {
            "state": "rendering",
            "backend": backend_tag,
            "stage": stage,
            "idx": idx,
            "total": total,
            "filename": filename,
            "output_filename": t_stem,
            "reason": reason,
            "model": model,
            "frames": frames,
            "resolution": resolution,
            "phase": "starting",
            "raw_phase": submit_msg,
            "status_text": submit_msg,
            "step": None,
            "total_steps": None,
            "pct": 0.0 if backend_tag == "local" else None,
            "window_index": None,
            "window_count": None,
            "started_at": round(t0, 3),
            "error": None,
        },
        merge=False,
        backend=backend_tag,
        task_stem=t_stem,
    )

    submit_cfg = {k: v for k, v in task_cfg.items() if not str(k).startswith("_")} if (backend_tag == "local" and isinstance(task_cfg, dict)) else task_cfg
    job = session.submit_task(submit_cfg)
    import re as _re
    cancelled_by_user = False
    last_emitted_sig = None
    last_logged_nonstep_sig = None

    ev_iter = job.events.iter_with_heartbeats(timeout=0.25) if hasattr(job.events, "iter_with_heartbeats") else job.events.iter(timeout=0.25)
    for event in ev_iter:
        if not cancelled_by_user and check_cancel_requested(ctx, backend=backend_tag, task_stem=t_stem):
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
                backend=backend_tag,
                task_stem=t_stem,
            )
            print(
                f"[QUEUE_CANCEL] {json.dumps({'idx': idx, 'filename': filename, 'reason': 'STOPPED BY USER'})}",
                flush=True,
            )
        if event is None:
            continue

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

            pct_sfx = f" ({prog['pct']:.0f}%)" if prog["pct"] is not None else ""
            if prog["total_steps"] and prog["step"] in (1, prog["total_steps"] // 2, prog["total_steps"]):
                win_prefix = f"[Win {prog['window_index']}/{prog['window_count']}] " if (prog["window_count"] and prog["window_count"] > 1 and prog["window_index"]) else ""
                print(
                    f"      {win_prefix}[{prog['phase']}] {prog['step']}/{prog['total_steps']}{pct_sfx}",
                    flush=True,
                )
            elif not prog["total_steps"]:
                base_raw = _re.sub(r"\s*\(\d+s\)\s*$", "", str(prog["raw_phase"] or "")).strip()
                nonstep_sig = (prog["phase"], base_raw, prog["pct"], prog["window_index"], prog["window_count"])
                if nonstep_sig != last_logged_nonstep_sig:
                    last_logged_nonstep_sig = nonstep_sig
                    print(
                        f"      [{prog['phase']}] {base_raw or prog['raw_phase']}{pct_sfx}",
                        flush=True,
                    )

            print(f"\n[QUEUE_PROGRESS] {json.dumps(prog)}", flush=True)
            write_engine_status(ctx, prog, backend=backend_tag, task_stem=t_stem)

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
            write_engine_status(ctx, {"error": [err_msg]}, backend=backend_tag, task_stem=t_stem)

    res = job.result()
    was_cancelled = cancelled_by_user or (
        not res.success and any("cancel" in getattr(e, "message", "").lower() for e in (res.errors or []))
    )
    return res, was_cancelled, t0
