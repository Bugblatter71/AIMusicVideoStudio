#!/usr/bin/env python3
"""Diagnose render queue states, state hashes, per-engine status JSONs, and vault state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PROJECTS_DIR = WORKSPACE_ROOT / "projects"


def _resolve_jobs_dir(proj_dir: Path) -> Path:
    for cand in (proj_dir / "work" / "jobs_v3", proj_dir / "work" / "jobs"):
        if cand.is_dir() and any(cand.glob("*.json")):
            return cand
    return proj_dir / "work" / "jobs_v3" if (proj_dir / "work" / "jobs_v3").is_dir() else (proj_dir / "work" / "jobs")


def diagnose_project(slug: str) -> dict:
    proj_dir = PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        return {"error": f"Project folder not found: {proj_dir}"}

    jobs_dir = _resolve_jobs_dir(proj_dir)
    queue_file = jobs_dir / "v3_queue.json"
    hashes_file = next(
        (h for h in (jobs_dir / "state_hashes.json", proj_dir / "work" / "state_hashes.json") if h.is_file()),
        jobs_dir / "state_hashes.json",
    )
    status_file = jobs_dir / "engine_status.json"
    cancel_file = jobs_dir / "cancel_request.json"
    manifest_file = next(
        (m for m in (proj_dir / "work" / "takes_manifest.json", proj_dir / "work" / "renders_v3" / "takes_manifest.json", proj_dir / "work" / "renders" / "takes_manifest.json") if m.is_file()),
        proj_dir / "work" / "takes_manifest.json",
    )
    ckpts_file = proj_dir / "checkpoints" / "index.json"

    result = {
        "slug": slug,
        "jobs_dir": str(jobs_dir.relative_to(proj_dir)) if jobs_dir.exists() else "work/jobs",
        "queue_file_exists": queue_file.is_file(),
        "hashes_file_exists": hashes_file.is_file(),
        "engine_status_exists": status_file.is_file(),
        "cancel_requested": cancel_file.is_file(),
        "vault_stats": {"total_takes": 0, "scenes": 0},
        "checkpoints_count": 0,
        "summary": {"total": 0, "queued": 0, "completed": 0, "skipped": 0, "rendering": 0, "failed": 0},
        "items": [],
        "engine_status": None,
        "engine_statuses": {},
    }

    if manifest_file.is_file():
        try:
            m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            scs = m_data.get("scenes", {})
            result["vault_stats"] = {"total_takes": sum(len(s.get("takes", [])) for s in scs.values()), "scenes": len(scs)}
        except Exception:
            pass

    if ckpts_file.is_file():
        try:
            c_data = json.loads(ckpts_file.read_text(encoding="utf-8"))
            result["checkpoints_count"] = len(c_data.get("checkpoints", []))
        except Exception:
            pass

    if status_file.is_file():
        try:
            result["engine_status"] = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as e:
            result["engine_status_error"] = str(e)

    if jobs_dir.is_dir():
        for ef in sorted(jobs_dir.glob("engine_status_*.json")):
            if "__" in ef.name:
                continue
            try:
                result["engine_statuses"][ef.stem] = json.loads(ef.read_text(encoding="utf-8"))
            except Exception:
                pass

    stored_hashes = {}
    if hashes_file.is_file():
        try:
            stored_hashes = json.loads(hashes_file.read_text(encoding="utf-8")).get("tasks", {})
        except Exception:
            pass

    if queue_file.is_file():
        try:
            items = json.loads(queue_file.read_text(encoding="utf-8"))
            result["summary"]["total"] = len(items)
            for itm in items:
                st = itm.get("status", "unknown")
                result["summary"][st] = result["summary"].get(st, 0) + 1
                out_fn = itm.get("output_filename", "")
                cur_hash = itm.get("task_hash", "")
                prev_hash = stored_hashes.get(out_fn, "")
                hash_match = (cur_hash == prev_hash) if (cur_hash and prev_hash) else None
                result["items"].append({
                    "idx": itm.get("idx"),
                    "scene_id": itm.get("scene_id"),
                    "output_filename": out_fn,
                    "status": st,
                    "backend": itm.get("backend_override") or itm.get("backend") or "local",
                    "model_type": itm.get("model_type"),
                    "reason": itm.get("reason"),
                    "error": itm.get("error"),
                    "needs_render": itm.get("needs_render"),
                    "estimated_str": itm.get("estimated_str"),
                    "hash_match": hash_match,
                    "task_hash": cur_hash[:10] if cur_hash else None,
                    "stored_hash": prev_hash[:10] if prev_hash else None,
                })
        except Exception as e:
            result["queue_error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="MVG Queue State Diagnostic Tool")
    parser.add_argument("slug", help="Project slug (e.g. all_or_nothing)")
    parser.add_argument("--json", action="store_true", help="Output diagnostic report as JSON")
    args = parser.parse_args()

    diag = diagnose_project(args.slug)
    if args.json:
        print(json.dumps(diag, indent=2))
        return
    if "error" in diag:
        print(f"Error: {diag['error']}", file=sys.stderr)
        sys.exit(1)

    print("=" * 88)
    print(f"  Queue Diagnostics: {args.slug} ({diag['jobs_dir']})")
    print("=" * 88)
    summ = diag["summary"]
    print(f"  Total: {summ['total']} | Queued: {summ.get('queued', 0)} | Failed: {summ.get('failed', 0)} | Rendering: {summ.get('rendering', 0)} | Completed: {summ.get('completed', 0)}")
    vstats = diag.get("vault_stats", {})
    if vstats.get("total_takes", 0) > 0:
        print(f"  Vault: {vstats['total_takes']} takes across {vstats['scenes']} scenes | Checkpoints: {diag.get('checkpoints_count', 0)}")
    if diag.get("cancel_requested"):
        print("  WARNING: Stale cancel_request.json present in jobs dir!")

    for k, eng in diag.get("engine_statuses", {}).items():
        print(f"  [{k}] state={eng.get('state')} phase={eng.get('phase')} pct={eng.get('pct')} ({eng.get('filename')})")

    print("\n  Passes Detail:")
    print("-" * 88)
    for itm in diag["items"]:
        st = itm["status"].upper()
        h_str = " [HASH MISMATCH]" if itm["hash_match"] is False else (" [HASH OK]" if itm["hash_match"] is True else "")
        err_str = f" | ERR: {itm['error']}" if itm.get("error") else ""
        eta = f" ({itm['estimated_str']})" if itm.get("estimated_str") else ""
        print(f"  #{itm['idx']:<2} {itm['scene_id']:<6} [{st:<9}] [{itm['backend']:<7}] {itm['output_filename']}{eta} | {itm['reason']}{h_str}{err_str}")
    print("-" * 88)


if __name__ == "__main__":
    main()
