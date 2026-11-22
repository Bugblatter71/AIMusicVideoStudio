"""Persistent Done-queue render history tracker across re-queues, edits, and restarts."""

from __future__ import annotations

import copy
import json
from pathlib import Path

try:
    from pipeline.media_utils import atomic_write_json
except ImportError:
    try:
        from work.pipeline.media_utils import atomic_write_json
    except ImportError:
        atomic_write_json = None


def done_history_path(proj_dir: Path) -> Path:
    return Path(proj_dir) / "work" / "jobs_v3" / "done_history.json"


def _resolve_proj_dir(srv, slug_or_dir) -> Path | None:
    if isinstance(slug_or_dir, Path):
        return slug_or_dir
    s = str(slug_or_dir or "").strip()
    if not s:
        return None
    p = Path(s)
    if p.is_dir():
        return p
    if srv and getattr(srv, "PROJECTS_DIR", None):
        return Path(srv.PROJECTS_DIR) / s
    return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if atomic_write_json:
        atomic_write_json(path, data)
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_raw_entries(proj_dir: Path) -> list[dict]:
    p = done_history_path(proj_dir)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [dict(x) for x in (items or []) if isinstance(x, dict)]
    except Exception:
        return []


def _save_raw_entries(proj_dir: Path, entries: list[dict]) -> list[dict]:
    entries.sort(key=lambda x: float(x.get("completed_at") or 0.0), reverse=True)
    trimmed = entries[:500]
    _write_json(done_history_path(proj_dir), {"version": 1, "items": trimmed})
    return trimmed


def build_done_record_from_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    out_fn = str(item.get("output_filename") or "").strip()
    if not out_fn and item.get("filename"):
        out_fn = Path(str(item["filename"])).stem
    if not out_fn:
        return None
    comp = item.get("completed_at")
    if comp is None:
        return None
    try:
        comp_f = float(comp)
    except (TypeError, ValueError):
        return None
    if comp_f <= 0:
        return None

    fn = str(item.get("filename") or f"{out_fn}.mp4")
    backend = str(item.get("backend_override") or item.get("backend") or "local").strip().lower() or "local"
    rec = {
        "history_id": str(item.get("history_id") or (f"take:{item['take_id']}" if item.get("take_id") else f"{out_fn}@{round(comp_f, 3)}")),
        "id": out_fn,
        "output_filename": out_fn,
        "filename": fn,
        "scene_id": item.get("scene_id") or "",
        "pass": item.get("pass") if item.get("pass") is not None else item.get("pass_num"),
        "duration": item.get("duration"),
        "status": "completed",
        "needs_render": False,
        "completed_at": comp_f,
        "elapsed": item.get("elapsed"),
        "model_type": item.get("model_type") or item.get("model") or "minimax_h3_fl2va_pruned_pdd",
        "backend": backend,
        "fal_endpoint": item.get("fal_endpoint") or "",
        "resolution": item.get("resolution") or "",
        "reason": str(item.get("reason") or "COMPLETED"),
    }
    if item.get("take_id"):
        rec["take_id"] = item["take_id"]
    if item.get("take_number") is not None:
        rec["take_number"] = item["take_number"]
    return rec


def _merge_done_record(entries: list[dict], rec: dict) -> bool:
    if not rec:
        return False
    rec_tid = rec.get("take_id")
    rec_fn = rec.get("output_filename")
    rec_t = float(rec.get("completed_at") or 0.0)
    for ex in entries:
        same_tid = bool(rec_tid and ex.get("take_id") and ex.get("take_id") == rec_tid and ex.get("output_filename") == rec_fn)
        same_run = bool(rec_fn and ex.get("output_filename") == rec_fn and abs(float(ex.get("completed_at") or 0.0) - rec_t) <= 5.0)
        if same_tid or same_run:
            changed = False
            for k in ("elapsed", "duration", "fal_endpoint", "resolution", "take_id", "take_number", "scene_id", "pass", "model_type", "backend"):
                rv = rec.get(k)
                ev = ex.get(k)
                if (ev is None or ev == "" or (k == "model_type" and ev == "minimax_h3_fl2va_pruned_pdd" and rv and rv != ev)) and rv not in (None, ""):
                    ex[k] = rv
                    changed = True
            return changed
    entries.append(copy.deepcopy(rec))
    return True


def _load_scene_durations(proj_dir: Path) -> dict[str, float]:
    durs: dict[str, float] = {}
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
    if not jsons:
        return durs
    try:
        data = json.loads(jsons[-1].read_text(encoding="utf-8"))
        for sc in data.get("scenes", []):
            sid = sc.get("id")
            if sid:
                durs[sid] = round(float(sc.get("end", 0)) - float(sc.get("start", 0)), 2)
    except Exception:
        pass
    return durs


def _sync_from_takes_manifest(proj_dir: Path, entries: list[dict]) -> bool:
    mf_path = proj_dir / "work" / "takes_manifest.json"
    if not mf_path.is_file():
        return False
    try:
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    scenes = mf.get("scenes") if isinstance(mf, dict) else None
    if not isinstance(scenes, dict):
        return False
    durs = _load_scene_durations(proj_dir)
    changed = False
    for sid, sc_entry in scenes.items():
        if not isinstance(sc_entry, dict):
            continue
        for tk in sc_entry.get("takes") or []:
            if not isinstance(tk, dict):
                continue
            out_name = str(tk.get("out_name") or "").strip()
            created_at = tk.get("created_at")
            if not out_name or created_at is None:
                continue
            r_start, r_end = tk.get("render_start"), tk.get("render_end")
            dur = round(float(r_end) - float(r_start), 2) if (r_start is not None and r_end is not None) else durs.get(sid)
            tk_num = tk.get("take_number")
            rec = build_done_record_from_item({
                "output_filename": out_name,
                "scene_id": tk.get("scene_id") or sid,
                "pass": tk.get("pass_num"),
                "duration": dur,
                "completed_at": created_at,
                "elapsed": tk.get("elapsed_s"),
                "model_type": tk.get("model_type") or "",
                "backend": tk.get("backend") or "local",
                "fal_endpoint": tk.get("fal_endpoint") or "",
                "resolution": tk.get("resolution") or "",
                "take_id": tk.get("take_id"),
                "take_number": tk_num,
                "reason": f"Take {tk_num}" if tk_num else "COMPLETED",
            })
            if rec and _merge_done_record(entries, rec):
                changed = True
    return changed


def _with_lock(srv, fn):
    lk = getattr(srv, "queue_file_lock", None) if srv else None
    if lk is not None:
        with lk:
            return fn()
    return fn()


def record_done_items(srv, slug_or_dir, items, *, only_completed: bool = True) -> list[dict]:
    proj_dir = _resolve_proj_dir(srv, slug_or_dir)
    if proj_dir is None or not proj_dir.is_dir():
        return []
    seq = [items] if isinstance(items, dict) else list(items or [])

    def _run() -> list[dict]:
        entries = _load_raw_entries(proj_dir)
        changed = _sync_from_takes_manifest(proj_dir, entries)
        for it in seq:
            if not isinstance(it, dict):
                continue
            if only_completed and it.get("status") != "completed":
                continue
            rec = build_done_record_from_item(it)
            if rec and _merge_done_record(entries, rec):
                changed = True
        if changed:
            return _save_raw_entries(proj_dir, entries)
        entries.sort(key=lambda x: float(x.get("completed_at") or 0.0), reverse=True)
        return entries

    return _with_lock(srv, _run)


def get_done_queue_history(srv, slug_or_dir, current_queue: list[dict] | None = None) -> list[dict]:
    proj_dir = _resolve_proj_dir(srv, slug_or_dir)
    if proj_dir is None or not proj_dir.is_dir():
        return []

    def _run() -> list[dict]:
        entries = _load_raw_entries(proj_dir)
        changed = _sync_from_takes_manifest(proj_dir, entries)
        q_items = current_queue
        if q_items is None:
            qf = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if qf.is_file():
                try:
                    raw = json.loads(qf.read_text(encoding="utf-8"))
                    q_items = raw if isinstance(raw, list) else raw.get("queue", [])
                except Exception:
                    q_items = []
        for it in q_items or []:
            if isinstance(it, dict) and it.get("status") == "completed":
                rec = build_done_record_from_item(it)
                if rec and _merge_done_record(entries, rec):
                    changed = True
        if changed:
            return _save_raw_entries(proj_dir, entries)
        entries.sort(key=lambda x: float(x.get("completed_at") or 0.0), reverse=True)
        return entries

    return _with_lock(srv, _run)
