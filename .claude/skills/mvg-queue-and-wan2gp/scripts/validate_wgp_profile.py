#!/usr/bin/env python3
"""Validate model profiles in model_profiles.json against Wan2GP engine expectations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PROFILES_FILE = WORKSPACE_ROOT / "model_profiles.json"


def validate_profiles() -> tuple[list[str], list[str]]:
    if not PROFILES_FILE.is_file():
        return [f"File not found: {PROFILES_FILE}"], []

    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"Invalid JSON in {PROFILES_FILE}: {e}"], []

    errors = []
    warnings = []

    video_models = data.get("video_models", {})
    anchor_models = data.get("anchor_models", {})
    resolutions = data.get("resolutions", {})

    # Check resolutions: must be multiples of 16 for video diffusion latents
    for key, spec in resolutions.items():
        w = spec.get("width", 0)
        h = spec.get("height", 0)
        if w % 16 != 0:
            errors.append(f"Resolution key '{key}' width {w} is not a multiple of 16 (required for Wan2GP macroblocks)")
        if h % 16 != 0:
            errors.append(f"Resolution key '{key}' height {h} is not a multiple of 16 (required for Wan2GP macroblocks)")

    # Check video models
    for mkey, mspec in video_models.items():
        mtype = mspec.get("type", "")
        # MiniMax Ref2VA / GVA requires audio_prompt_type="AS" (Audio Spectrogram)
        if "fl2va" in mkey or "gva" in mkey or "minimax" in mkey:
            apt = mspec.get("audio_prompt_type")
            if apt != "AS":
                errors.append(f"Model '{mkey}' has audio_prompt_type='{apt}', must be 'AS' for MiniMax GVA/Ref2VA")

        cfg = mspec.get("cfg_scale", 1.0)
        if not (1.0 <= cfg <= 20.0):
            warnings.append(f"Model '{mkey}' cfg_scale {cfg} outside typical range [1.0, 20.0]")

        steps = mspec.get("steps", 25)
        if not (10 <= steps <= 100):
            warnings.append(f"Model '{mkey}' sampling steps {steps} outside typical range [10, 100]")

    # Check anchor models
    for akey, aspec in anchor_models.items():
        steps = aspec.get("steps", 20)
        if steps < 4 or steps > 100:
            warnings.append(f"Anchor model '{akey}' steps {steps} abnormal")

    return errors, warnings


def main():
    errors, warnings = validate_profiles()
    print("=" * 80)
    print(f"  Wan2GP Model Profile Validation ({PROFILES_FILE.name})")
    print("=" * 80)

    if not errors and not warnings:
        print("  ✅ All model profiles and resolution presets are valid and compatible with Wan2GP!")
    else:
        if errors:
            print(f"  ❌ {len(errors)} Critical Profile Errors:")
            for e in errors:
                print(f"     - {e}")
        if warnings:
            print(f"  ⚠️ {len(warnings)} Warnings:")
            for w in warnings:
                print(f"     - {w}")

    print("-" * 80)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
