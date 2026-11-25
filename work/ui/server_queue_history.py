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


try:
    from pipeline.project_paths import resolve_done_history_file, resolve_queue_file, resolve_state_hashes_paths
except ImportError:
    try:
        from work.pipeline.project_paths import resolve_done_history_file, resolve_queue_file, resolve_state_hashes_paths
    except ImportError:
        resolve_done_history_file = None
        resolve_queue_file = None
        resolve_state_hashes_paths = None


def done_history_path(proj_dir: Path, for_write: bool = False) -> Path:
    if resolve_done_history_file:
        return resolve_done_history_file(Path(proj_dir), for_write=for_write)
    return Path(proj_dir) / "work" / "jobs" / "done_history.json"


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


_FILL_KEYS = ("elapsed", "duration", "fal_endpoint", "resolution", "take_id", "take_number", "scene_id", "pass", "model_type", "backend")


def _match_distance(ex: dict, rec: dict) -> float | None:
    rec_fn, ex_fn = rec.get("output_filename"), ex.get("output_filename")
    if not rec_fn or ex_fn != rec_fn:
        return None
    rec_tid, ex_tid = rec.get("take_id"), ex.get("take_id")
    if rec_tid and ex_tid:
        return 0.0 if rec_tid == ex_tid else None
    ex_t, rec_t = float(ex.get("completed_at") or 0.0), float(rec.get("completed_at") or 0.0)
    if bool(rec_tid) != bool(ex_tid):
        t_take, t_shadow = (ex_t, rec_t) if ex_tid else (rec_t, ex_t)
        return abs(t_shadow - t_take) if -5.0 <= (t_shadow - t_take) <= 60.0 else None
    return abs(ex_t - rec_t) if abs(ex_t - rec_t) <= 5.0 else None


def _merge_done_record(entries: list[dict], rec: dict) -> bool:
    if not rec:
        return False
    best_ex, best_dist = None, None
    for ex in entries:
        dist = _match_distance(ex, rec)
        if dist is not None and (best_dist is None or dist < best_dist):
            best_ex, best_dist = ex, dist
            if dist == 0.0 and ex.get("take_id") == rec.get("take_id"):
                break
    if best_ex is not None:
        changed = False
        if rec.get("take_id") and not best_ex.get("take_id"):
            saved_fill = {k: best_ex.get(k) for k in _FILL_KEYS if best_ex.get(k) not in (None, "")}
            for k in ("history_id", "take_id", "take_number", "completed_at", "reason", "elapsed", "duration", "scene_id", "pass", "model_type", "backend", "fal_endpoint", "resolution"):
                rv = rec.get(k)
                if rv not in (None, ""):
                    best_ex[k] = rv
            for k, v in saved_fill.items():
                if best_ex.get(k) in (None, ""):
                    best_ex[k] = v
            return True
        for k in _FILL_KEYS:
            rv, ev = rec.get(k), best_ex.get(k)
            if (ev is None or ev == "" or (k == "model_type" and ev == "minimax_h3_fl2va_pruned_pdd" and rv and rv != ev)) and rv not in (None, ""):
                best_ex[k] = rv
                changed = True
        return changed
    entries.append(copy.deepcopy(rec))
    return True


def _reconcile_entries(entries: list[dict]) -> tuple[list[dict], bool]:
    ordered = sorted(entries, key=lambda x: (0 if x.get("take_id") else 1, -float(x.get("completed_at") or 0.0)))
    reconciled: list[dict] = []
    for e in ordered:
        _merge_done_record(reconciled, e)
    return reconciled, len(reconciled) != len(entries)


def _load_takes_manifest_dict(proj_dir: Path) -> dict:
    mf_path = Path(proj_dir) / "work" / "takes_manifest.json"
    if not mf_path.is_file():
        return {}
    try:
        data = json.loads(mf_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_completion_meta(proj_dir: Path, out_fn: str, started_at: float, fallback_ts: float, fallback_elapsed: float) -> dict:
    """Finds the actual take/render completion timestamp and take metadata produced during [started_at, fallback_ts]."""
    proj_dir = Path(proj_dir)
    min_ts = float(started_at or 0.0) - 2.0
    mf = _load_takes_manifest_dict(proj_dir)
    cands = [
        tk for sc_entry in (mf.get("scenes") or {}).values() if isinstance(sc_entry, dict)
        for tk in (sc_entry.get("takes") or [])
        if isinstance(tk, dict) and tk.get("out_name") == out_fn and float(tk.get("created_at") or 0.0) >= min_ts
    ]
    if cands:
        tk = max(cands, key=lambda x: float(x.get("created_at") or 0.0))
        meta = {
            "completed_at": round(float(tk["created_at"]), 3),
            "elapsed": tk.get("elapsed_s") if tk.get("elapsed_s") is not None else fallback_elapsed,
            "take_id": tk.get("take_id"),
        }
        if tk.get("take_number") is not None:
            meta["take_number"] = tk.get("take_number")
        return meta

    sh_paths = resolve_state_hashes_paths(proj_dir) if resolve_state_hashes_paths else [proj_dir / "work" / "jobs" / "state_hashes.json", proj_dir / "work" / "state_hashes.json"]
    for shp in sh_paths:
        if not shp.is_file():
            continue
        try:
            sh = json.loads(shp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in (out_fn, f"{out_fn}.mp4", f"{out_fn}.jpg", f"{out_fn}.png"):
            info = (sh.get("scene_renders") or {}).get(key) or (sh.get("anchor_renders") or {}).get(key)
            if isinstance(info, dict) and float(info.get("completed_at") or 0.0) >= min_ts:
                return {
                    "completed_at": round(float(info["completed_at"]), 3),
                    "elapsed": info.get("elapsed") if info.get("elapsed") is not None else fallback_elapsed,
                }
    return {"completed_at": fallback_ts, "elapsed": fallback_elapsed}


def attach_queue_take_metadata(proj_dir: Path, queue: list[dict]) -> None:
    """Attaches active take_id/take_number and canonical timestamp onto completed queue items."""
    mf = _load_takes_manifest_dict(proj_dir)
    scenes = mf.get("scenes")
    if not isinstance(scenes, dict) or not queue:
        return
    by_out: dict[str, dict] = {}
    for sc_entry in scenes.values():
        if not isinstance(sc_entry, dict):
            continue
        takes = {t.get("take_id"): t for t in (sc_entry.get("takes") or []) if isinstance(t, dict) and t.get("take_id")}
        act_ids = set((sc_entry.get("active_takes") or {}).values())
        if sc_entry.get("active_take_id"):
            act_ids.add(sc_entry["active_take_id"])
        for tid in act_ids:
            tk = takes.get(tid)
            if tk and tk.get("out_name"):
                by_out[tk["out_name"]] = tk
    for it in queue:
        if not isinstance(it, dict) or it.get("status") != "completed":
            continue
        tk = by_out.get(it.get("output_filename") or "")
        if not tk:
            continue
        tk_ts = float(tk.get("created_at") or 0.0)
        cur_ts = float(it.get("completed_at") or 0.0)
        if not cur_ts or (tk_ts and -5.0 <= (cur_ts - tk_ts) <= 60.0):
            it["take_id"] = tk["take_id"]
            if tk.get("take_number") is not None:
                it["take_number"] = tk.get("take_number")
            if tk_ts:
                it["completed_at"] = round(tk_ts, 3)


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
        entries, changed = _reconcile_entries(_load_raw_entries(proj_dir))
        if _sync_from_takes_manifest(proj_dir, entries):
            changed = True
        for it in seq:
            if not isinstance(it, dict):
                continue
            if only_completed and it.get("status") != "completed":
                continue
            rec = build_done_record_from_item(it)
            if rec and _merge_done_record(entries, rec):
                changed = True
        entries, extra_clean = _reconcile_entries(entries)
        if changed or extra_clean:
            return _save_raw_entries(proj_dir, entries)
        entries.sort(key=lambda x: float(x.get("completed_at") or 0.0), reverse=True)
        return entries

    return _with_lock(srv, _run)


def get_done_queue_history(srv, slug_or_dir, current_queue: list[dict] | None = None) -> list[dict]:
    proj_dir = _resolve_proj_dir(srv, slug_or_dir)
    if proj_dir is None or not proj_dir.is_dir():
        return []

    def _run() -> list[dict]:
        entries, changed = _reconcile_entries(_load_raw_entries(proj_dir))
        if _sync_from_takes_manifest(proj_dir, entries):
            changed = True
        q_items = current_queue
        if q_items is None:
            qf = resolve_queue_file(proj_dir) if resolve_queue_file else (proj_dir / "work" / "jobs" / "v3_queue.json")
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
        entries, extra_clean = _reconcile_entries(entries)
        if changed or extra_clean:
            return _save_raw_entries(proj_dir, entries)
        entries.sort(key=lambda x: float(x.get("completed_at") or 0.0), reverse=True)
        return entries

    return _with_lock(srv, _run)


def clear_scene_queue_curation(srv, slug_or_dir, sid: str, passes: tuple = (1, 2, None)) -> bool:
    """Clears transient curation (FORCED/SKIPPED/failed) and stale backend_override on reverted scene passes."""
    proj_dir = _resolve_proj_dir(srv, slug_or_dir)
    if proj_dir is None or not proj_dir.is_dir():
        return False
    qf = resolve_queue_file(proj_dir) if resolve_queue_file else (proj_dir / "work" / "jobs" / "v3_queue.json")
    if not qf.is_file():
        return False
    lk = getattr(srv, "queue_file_lock", None)

    def _run() -> bool:
        try:
            raw = json.loads(qf.read_text(encoding="utf-8"))
        except Exception:
            return False
        items = raw if isinstance(raw, list) else (raw.get("queue", []) if isinstance(raw, dict) else [])
        sid_u = str(sid or "").strip().upper()
        changed = False
        for it in items:
            if not isinstance(it, dict) or str(it.get("scene_id") or "").strip().upper() != sid_u:
                continue
            if it.get("pass") not in passes or it.get("status") == "rendering":
                continue
            for k in ("backend_override", "override_backend", "user_skipped_hash", "user_skipped_anchor_mtime", "error", "completed_at", "elapsed", "take_id", "take_number"):
                if k in it:
                    it.pop(k, None)
                    changed = True
            if it.get("status") in ("queued", "skipped", "failed"):
                it["status"] = "completed"
                it["reason"] = "REVERTED"
                it["needs_render"] = False
                changed = True
        if changed:
            payload = items if isinstance(raw, list) else {**raw, "queue": items}
            _write_json(qf, payload)
        return changed

    if lk is not None:
        with lk:
            return _run()
    return _run()
