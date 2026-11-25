#!/usr/bin/env python3
"""Validate model_profiles.json against Studio Wan2GP & cloud engine invariants."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


def validate_profiles(profiles_path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not profiles_path.is_file():
        return [f"Profiles file not found: {profiles_path}"], []

    data = json.loads(profiles_path.read_text(encoding="utf-8"))

    # 1. Resolutions must be divisible by 16 for DiT/VAE macroblocks
    res_map = data.get("video_resolutions", {})
    if not res_map:
        errors.append("Missing video_resolutions dictionary.")
    for rnum, rspec in res_map.items():
        rstr = rspec.get("resolution") if isinstance(rspec, dict) else str(rspec)
        try:
            w, h = map(int, str(rstr).lower().split("x"))
            if w % 16 != 0 or h % 16 != 0:
                errors.append(f"Resolution [{rnum}] '{rstr}' dimensions must be multiples of 16.")
        except Exception:
            errors.append(f"Resolution [{rnum}] has invalid format '{rstr}'.")

    # 2. Anchor & Video local renderers
    for rid, spec in data.get("anchor_renderers", {}).items():
        if not spec.get("model_type"):
            errors.append(f"anchor_renderers[{rid}] missing model_type.")
        if int(spec.get("num_inference_steps", 0) or 0) <= 0:
            errors.append(f"anchor_renderers[{rid}] num_inference_steps must be > 0.")

    for rid, spec in data.get("video_renderers", {}).items():
        mtype = str(spec.get("model_type") or "")
        if not mtype:
            errors.append(f"video_renderers[{rid}] missing model_type.")
            continue
        if int(spec.get("num_inference_steps", 0) or 0) <= 0:
            errors.append(f"video_renderers[{rid}] num_inference_steps must be > 0.")
        apt = spec.get("audio_prompt_type")
        if "ref2va" in mtype.lower() and spec.get("supports_image_refs") and apt not in (None, "AS", "AP"):
            warnings.append(f"video_renderers[{rid}] ({mtype}) unusual audio_prompt_type={apt!r}.")
        if "minimax_h3" in mtype.lower() and spec.get("frames_formula") not in ("h3", "h3_ref2va", None):
            warnings.append(f"video_renderers[{rid}] ({mtype}) unexpected frames_formula={spec.get('frames_formula')!r}.")
        if str(spec.get("ref_mode") or "").lower() == "ltx2_msr":
            ipt = str(spec.get("image_prompt_type") or "")
            vpt = str(spec.get("video_prompt_type") or "")
            apt_s = str(apt or "")
            if any(ch in ipt for ch in "SVL"):
                errors.append(f"video_renderers[{rid}] ({mtype}) ltx2_msr image_prompt_type={ipt!r} must not contain S/V/L.")
            if vpt not in ("KI", "I"):
                errors.append(f"video_renderers[{rid}] ({mtype}) ltx2_msr video_prompt_type={vpt!r} must be 'KI' or 'I'.")
            if any(ch in apt_s for ch in "K2"):
                errors.append(f"video_renderers[{rid}] ({mtype}) ltx2_msr audio_prompt_type={apt_s!r} must not contain K/2.")

    # 3. Cloud profile sections (ensure fal_catalog keys align if present)
    sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
    try:
        from pipeline.fal_catalog import FAL_ANCHOR_PROFILES, FAL_VIDEO_PROFILES, MINIMAX_VIDEO_PROFILES
        for key in data.get("fal_anchor_renderers", {}):
            if key not in FAL_ANCHOR_PROFILES:
                warnings.append(f"fal_anchor_renderers[{key}] not registered in fal_catalog.FAL_ANCHOR_PROFILES.")
        for key in data.get("fal_video_renderers", {}):
            if key not in FAL_VIDEO_PROFILES:
                warnings.append(f"fal_video_renderers[{key}] not registered in fal_catalog.FAL_VIDEO_PROFILES.")
        for key in data.get("minimax_video_renderers", {}):
            if key not in MINIMAX_VIDEO_PROFILES:
                warnings.append(f"minimax_video_renderers[{key}] not registered in fal_catalog.MINIMAX_VIDEO_PROFILES.")
    except Exception as exc:
        warnings.append(f"Skipped fal_catalog cross-check: {exc}")

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Validate Studio model_profiles.json")
    parser.add_argument("profiles", nargs="?", default=str(WORKSPACE_ROOT / "model_profiles.json"), help="Path to model_profiles.json")
    args = parser.parse_args()

    errs, warns = validate_profiles(Path(args.profiles))
    for w in warns:
        print(f"[WARN] {w}")
    for e in errs:
        print(f"[FAIL] {e}", file=sys.stderr)
    if errs:
        sys.exit(1)
    print(f"OK: {Path(args.profiles).name} passed profile & macroblock validation ({len(warns)} warnings).")


if __name__ == "__main__":
    main()
