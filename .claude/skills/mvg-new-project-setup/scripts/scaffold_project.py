#!/usr/bin/env python3
"""Validate and scaffold a complete new music video project config from a storyboard spec JSON."""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PROJECTS_DIR = WORKSPACE_ROOT / "projects"
BACKUP_DIR = WORKSPACE_ROOT / "backup"

VALID_MODES = {
    "solo", "male_solo", "female_solo", "silent", "silent_locked", "ambient",
    "duet_method_a", "duet_a", "duet_stereo",
    "duet_method_b", "gva_male_left_silent_right", "duet", "two_pass_gva",
}
DUET_TWO_PASS_MODES = {"duet_method_b", "gva_male_left_silent_right", "duet", "two_pass_gva"}


def _find_latest_json(pdir: Path, slug: str) -> Path | None:
    jsons = [j for j in sorted(pdir.glob("*.json")) if not j.name.startswith("state_hashes") and j.name != "storyboard_spec.json"]
    return jsons[-1] if jsons else (pdir / f"{slug}.json")


def _extract_aid(val: str) -> str:
    m = re.match(r"^(A\d+[a-z]?)", str(val or "").strip(), re.IGNORECASE)
    return m.group(1).upper() if m else str(val or "").strip()


def validate_spec_data(data: dict, proj_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    scenes = data.get("scenes", [])
    anchors = data.get("anchors", [])
    if not isinstance(scenes, list) or not scenes:
        errors.append("Spec must contain at least one scene in 'scenes'.")
        return errors, warnings

    known_aids: set[str] = set()
    for a in anchors:
        fn = a.get("filename") or a.get("id") or ""
        if not fn:
            errors.append("An anchor entry is missing 'filename'.")
            continue
        known_aids.add(fn)
        known_aids.add(_extract_aid(fn))

    audio_dir = proj_dir / "audio"
    audio_files = {f.name.lower() for f in audio_dir.iterdir() if f.is_file()} if audio_dir.is_dir() else set()
    stems_cfg = {k.lower(): Path(v).name.lower() for k, v in (data.get("project", {}).get("stems") or {}).items()}

    seen_sids: set[str] = set()
    for i, sc in enumerate(scenes):
        sid = str(sc.get("id") or "").strip()
        if not sid:
            errors.append(f"Scene index {i} missing 'id'.")
            continue
        if sid.upper() in seen_sids:
            errors.append(f"Duplicate scene id '{sid}'.")
        seen_sids.add(sid.upper())

        start, end = float(sc.get("start", 0)), float(sc.get("end", 0))
        if end <= start:
            errors.append(f"Scene {sid}: end ({end}) must be > start ({start}).")
        if end - start > 15.0:
            warnings.append(f"Scene {sid}: duration {end - start:.2f}s > 15s (will trigger sliding window / cloud chunking).")
        if i > 0:
            prev_end = float(scenes[i - 1].get("end", 0))
            if abs(start - prev_end) > 0.05:
                errors.append(f"Timeline discontinuity between {scenes[i - 1].get('id')} (end {prev_end}) and {sid} (start {start}).")

        mode = str(sc.get("mode") or "solo").strip()
        if mode not in VALID_MODES:
            errors.append(f"Scene {sid}: unknown mode '{mode}'.")

        anc = sc.get("anchor")
        if not anc:
            errors.append(f"Scene {sid}: missing 'anchor'.")
        elif known_aids and anc not in known_aids and _extract_aid(anc) not in known_aids:
            errors.append(f"Scene {sid}: anchor '{anc}' is not defined in 'anchors'.")

        if mode in DUET_TWO_PASS_MODES:
            for k in ("stem_m", "stem_f"):
                st = sc.get(k)
                if not st:
                    warnings.append(f"Scene {sid}: two-pass mode '{mode}' missing '{k}'.")
            if not sc.get("prompt_m") and not sc.get("prompt"):
                warnings.append(f"Scene {sid}: missing 'prompt_m'.")
            if not sc.get("prompt_f") and not sc.get("prompt"):
                warnings.append(f"Scene {sid}: missing 'prompt_f'.")
        else:
            st = sc.get("stem")
            if mode not in ("silent", "silent_locked", "ambient") and audio_files and st and st != "silent":
                mapped = stems_cfg.get(str(st).lower(), Path(str(st)).name.lower())
                if mapped not in audio_files and f"{mapped}.mp3" not in audio_files and f"{mapped}.wav" not in audio_files:
                    warnings.append(f"Scene {sid}: stem '{st}' not yet found in audio/.")

    return errors, warnings


def export_starter_spec(slug: str, out_path: Path) -> None:
    pdir = PROJECTS_DIR / slug
    cfg_path = _find_latest_json(pdir, slug) if pdir.is_dir() else (WORKSPACE_ROOT / "project_template.json")
    base = json.loads((cfg_path if cfg_path and cfg_path.is_file() else (WORKSPACE_ROOT / "project_template.json")).read_text(encoding="utf-8"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    print(f"Exported starter storyboard spec -> {out_path}")


def apply_spec(slug: str, spec_path: Path) -> None:
    pdir = PROJECTS_DIR / slug
    if not pdir.is_dir():
        print(f"Project directory '{pdir}' does not exist. Run: python work/run_overnight_v3.py --new {slug}", file=sys.stderr)
        sys.exit(1)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    target_json = _find_latest_json(pdir, slug)
    base = json.loads(target_json.read_text(encoding="utf-8")) if target_json and target_json.is_file() else {}

    # Deep-merge top-level sections while preserving project defaults
    merged = dict(base)
    for k, v in spec.items():
        if k in ("project", "models", "run_controls") and isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v

    errs, warns = validate_spec_data(merged, pdir)
    for w in warns:
        print(f"[WARN] {w}")
    if errs:
        for e in errs:
            print(f"[FAIL] {e}", file=sys.stderr)
        sys.exit(1)

    if target_json and target_json.is_file():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(target_json, BACKUP_DIR / f"{target_json.stem}_{ts}.json")
    assert target_json is not None
    target_json.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"OK: Applied validated storyboard spec -> {target_json} ({len(merged.get('scenes', []))} scenes, {len(merged.get('anchors', []))} anchors).")


def main():
    parser = argparse.ArgumentParser(description="Scaffold & validate an MVG storyboard spec onto a project JSON")
    parser.add_argument("slug", help="Target project slug under projects/")
    parser.add_argument("--spec", help="Path to storyboard JSON spec to validate and apply")
    parser.add_argument("--export-starter", help="Write a starter storyboard spec JSON to this path")
    args = parser.parse_args()

    if args.export_starter:
        export_starter_spec(args.slug, Path(args.export_starter))
        return
    if args.spec:
        sp = Path(args.spec)
        if not sp.is_file():
            cand = WORKSPACE_ROOT / args.spec
            if cand.is_file():
                sp = cand
        if not sp.is_file():
            print(f"Spec file not found: {args.spec}", file=sys.stderr)
            sys.exit(1)
        apply_spec(args.slug, sp)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
