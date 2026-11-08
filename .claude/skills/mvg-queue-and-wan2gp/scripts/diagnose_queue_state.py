#!/usr/bin/env python3
"""Diagnose render queue states, state hashes, and engine status for a project."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PROJECTS_DIR = WORKSPACE_ROOT / "projects"


def diagnose_project(slug: str) -> dict:
    proj_dir = PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        return {"error": f"Project folder not found: {proj_dir}"}

    jobs_dir = proj_dir / "work" / "jobs_v3"
    queue_file = jobs_dir / "v3_queue.json"
    hashes_file = jobs_dir / "state_hashes.json"
    status_file = jobs_dir / "engine_status.json"
    cancel_file = jobs_dir / "cancel_request.json"

    result = {
        "slug": slug,
        "queue_file_exists": queue_file.is_file(),
        "hashes_file_exists": hashes_file.is_file(),
        "engine_status_exists": status_file.is_file(),
        "cancel_requested": cancel_file.is_file(),
        "summary": {"total": 0, "queued": 0, "completed": 0, "skipped": 0, "rendering": 0, "failed": 0},
        "items": [],
        "engine_status": None,
    }

    if status_file.is_file():
        try:
            result["engine_status"] = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as e:
            result["engine_status_error"] = str(e)

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
                    "reason": itm.get("reason"),
                    "needs_render": itm.get("needs_render"),
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
        print(f"❌ {diag['error']}", file=sys.stderr)
        sys.exit(1)

    print("=" * 80)
    print(f"  Queue Diagnostics: {args.slug}")
    print("=" * 80)
    summ = diag["summary"]
    print(f"  Total Passes : {summ['total']}")
    print(f"  Queued       : {summ.get('queued', 0)}")
    print(f"  Completed    : {summ.get('completed', 0)}")
    print(f"  Skipped      : {summ.get('skipped', 0)}")
    print(f"  Rendering    : {summ.get('rendering', 0)}")
    if summ.get("failed", 0) > 0:
        print(f"  Failed       : {summ['failed']}")

    if diag.get("cancel_requested"):
        print("\n  ⚠️ ATTENTION: Stale 'cancel_request.json' file is present in jobs_v3/ directory!")

    eng = diag.get("engine_status")
    if eng:
        print(f"\n  Engine Status: state={eng.get('state')} | phase={eng.get('phase')} | {eng.get('pct', 0)}% ({eng.get('filename')})")

    print("\n  Passes Detail:")
    print("-" * 80)
    for itm in diag["items"]:
        st = itm["status"].upper()
        h_str = ""
        if itm["hash_match"] is False:
            h_str = " [HASH MISMATCH]"
        elif itm["hash_match"] is True:
            h_str = " [HASH OK]"
        print(f"  #{itm['idx']:<2} {itm['scene_id']:<6} [{st:<9}] {itm['output_filename']} | {itm['reason']}{h_str}")
    print("-" * 80)


if __name__ == "__main__":
    main()
