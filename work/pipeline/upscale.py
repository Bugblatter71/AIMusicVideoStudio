"""Spatial upsampling policy, shared resolution/upscaling helpers, and on-demand clip upscaling."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .fal_catalog import resolve_upscale_backend
from .media_utils import probe_video_dimensions, probe_video_frames, resolve_rendered_clip_path
from .takes import (
    _find_strip,
    _pass_key,
    load_takes_manifest,
    safe_link_or_copy,
    save_takes_manifest,
)

SPATIAL_UPSAMPLER_OPTIONS = [
    {"value": "", "label": "None (Stage 3 FFmpeg Scale Only)", "stage2": True, "late": False, "scale": 1.0},
    {"value": "h3_vae*2", "label": "MiniMax H3 Detail VAE x2 (Stage 2 Recommended for 540p)", "stage2": True, "late": False, "scale": 2.0},
    {"value": "h3_vae*1.5", "label": "MiniMax H3 Detail VAE x1.5 (Stage 2 Recommended for 720p -> 1080p)", "stage2": True, "late": False, "scale": 1.5},
    {"value": "flashvsrplus*2", "label": "FlashVSR+ Turbo x2 (AI Video Super-Res)", "stage2": True, "late": True, "scale": 2.0},
    {"value": "flashvsrplus2pass*2", "label": "FlashVSR+ 2-Pass x2 (AI Video Super-Res High Detail)", "stage2": True, "late": True, "scale": 2.0},
    {"value": "seedvr2*2", "label": "SeedVR2 x2 (Diffusion Detail Restoration)", "stage2": True, "late": True, "scale": 2.0},
]

AI_REFINER_OPTIONS = [
    {"value": "", "label": "None", "h3_only": False, "scale": 1.0},
    {"value": "h3facerefine", "label": "H3 Face Refiner 1x (Faces/Lips Restoration)", "h3_only": True, "scale": 1.0},
    {"value": "seedvr2*1", "label": "SeedVR2 1x (Diffusion Detail & Texture Refiner)", "h3_only": False, "scale": 1.0},
    {"value": "flashvsrplus*1", "label": "FlashVSR+ 1x (Temporal Detail Polish)", "h3_only": False, "scale": 1.0},
    {"value": "flashvsrplus2pass*1", "label": "FlashVSR+ 2-Pass 1x (Deep Temporal Polish)", "h3_only": False, "scale": 1.0},
    {"value": "ltx25_refine*1", "label": "LTX 2.5 Latent Refiner 1x", "h3_only": False, "scale": 1.0},
    {"value": "dlss5*1", "label": "DLSS5 Super-Res Refiner 1x (CUDA 12.6+)", "h3_only": False, "scale": 1.0},
]

SPATIAL_UPSAMPLER_KEYS = tuple(o["value"] for o in SPATIAL_UPSAMPLER_OPTIONS)
AI_REFINER_KEYS = tuple(o["value"] for o in AI_REFINER_OPTIONS if o["value"])
_AI_REFINER_SET = {k.lower() for k in AI_REFINER_KEYS}
_LATE_PREFIXES = ("flashvsr", "seedvr2", "h3facerefine", "lanczos", "ltx25_refine", "dlss5")


def parse_res_wh(res_str: Any) -> tuple[int, int] | None:
    """Parses a 'WxH' resolution string into (w, h), returning None for 'match'/invalid strings."""
    text = str(res_str or "").strip().lower()
    if "x" not in text:
        return None
    parts = text.split("x", 1)
    try:
        w, h = int(parts[0].strip()), int(parts[1].strip())
        return (w, h) if (w > 0 and h > 0) else None
    except ValueError:
        return None


def parse_upsampling_scale(value: Any) -> float:
    """Extracts the spatial scale multiplier from a Wan2GP spatial_upsampling value (e.g. 'h3_vae*2' -> 2.0)."""
    text = str(value or "").strip().lower()
    if not text or text in ("none", "off", "0", "default", "inherit", "null"):
        return 1.0
    if "*" in text:
        try:
            return float(text.rsplit("*", 1)[-1])
        except ValueError:
            return 1.0
    return 1.0


def explain_spatial_upsampling_skip(
    raw_upsampling: Any,
    res_str: str,
    target_res_str: str = "",
    model_type: str = "",
    pass_num: int | None = None,
    max_scale_source_width: int = 1536,
) -> str:
    """Returns empty string if upsampling is active, or concise skip reason if skipped by policy."""
    val = str(raw_upsampling or "").strip()
    low = val.lower()
    if not low or low in ("none", "off", "0", "default", "inherit", "null"):
        return ""
    if pass_num == 1:
        return "Skipped on Pass 1"
    mtype = str(model_type or "").lower()
    if "h3_vae" in low and mtype and not mtype.startswith("minimax_h3"):
        return "Requires MiniMax H3 model"
    scale = parse_upsampling_scale(val)
    if scale > 1.0:
        src_wh = parse_res_wh(res_str)
        if src_wh:
            sw, sh = src_wh
            if sw >= max_scale_source_width:
                return f"Skipped >= {max_scale_source_width}px (8GB VRAM guard)"
            tgt_wh = parse_res_wh(target_res_str)
            if tgt_wh and sw >= tgt_wh[0] and sh >= tgt_wh[1]:
                return f"Skipped: already >= target ({tgt_wh[0]}x{tgt_wh[1]})"
    return ""


def resolve_effective_spatial_upsampling(
    raw_upsampling: Any,
    res_str: str,
    model_type: str = "",
    pass_num: int | None = None,
    max_scale_source_width: int = 1536,
    target_res_str: str = "",
) -> str:
    """Normalizes spatial_upsampling and enforces compatibility, target-resolution, and 8GB VRAM guards.

    - 'none'/'off'/'default'/empty resolve to ''.
    - Pass 1 of two-pass duet scenes suppresses upsampling so Pass 1 stays 1:1 with video_mask.
    - 'h3_vae*' is skipped for non-minimax_h3 models.
    - Multipliers > 1.0 are suppressed when source width >= max_scale_source_width OR source >= target_res_str.
    """
    val = str(raw_upsampling or "").strip()
    low = val.lower()
    if not low or low in ("none", "off", "0", "default", "inherit", "null"):
        return ""
    if explain_spatial_upsampling_skip(
        val, res_str, target_res_str=target_res_str, model_type=model_type,
        pass_num=pass_num, max_scale_source_width=max_scale_source_width,
    ):
        return ""
    return val


def _clean_refiner_list(raw_val: Any) -> list[str] | None:
    """Normalizes ai_refiners field. Returns None when inheriting, [] when explicitly cleared."""
    if raw_val is None:
        return None
    if isinstance(raw_val, str):
        s = raw_val.strip()
        if not s or s.lower() in ("default", "inherit"):
            return None
        if s.lower() in ("none", "off", "0", "null"):
            return []
        tokens = [t.strip() for t in s.split(",") if t.strip()]
    elif isinstance(raw_val, (list, tuple)):
        tokens = [str(t or "").strip() for t in raw_val if str(t or "").strip()]
        if len(tokens) == 1 and tokens[0].lower() in ("none", "off"):
            return []
    else:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        tl = t.lower()
        if tl in ("none", "off", "0", "default", "inherit", "null") or tl in seen:
            continue
        seen.add(tl)
        out.append(t)
    return out


def _opt_float(src: dict, key: str, current: float, lo: float, hi: float) -> float:
    if key not in src or src[key] is None or str(src[key]).strip().lower() in ("", "default", "inherit", "null"):
        return current
    try:
        return max(lo, min(hi, float(src[key])))
    except (ValueError, TypeError):
        return current


def _opt_bool(src: dict, key: str, current: bool) -> bool:
    if key not in src or src[key] is None or str(src[key]).strip().lower() in ("", "default", "inherit", "null"):
        return current
    v = src[key]
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def resolve_scene_filters(
    sc: dict | None,
    vrspec: dict | None = None,
    vshared: dict | None = None,
    pass_num: int | None = None,
    raw_spatial_upsampling: str = "",
) -> dict:
    """Resolves composable 1x refiners, film grain, window CC, and Stage 3 FFmpeg filters (global -> renderer -> scene)."""
    sc = sc or {}
    vrspec = vrspec or {}
    vshared = vshared or {}

    ai_refiners: list[str] = []
    ai_overridden_empty = False
    face_strength, face_count, face_use_ref = 0.75, 1, True
    grain_int, grain_sat, wcc = 0.0, 0.5, 0.0
    ff_deband, ff_denoise, ff_cas = False, 0.0, 0.0
    ff_eq = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0}

    for tier in (vshared, vrspec, sc):
        f = tier.get("filters") if isinstance(tier.get("filters"), dict) else {}
        if "ai_refiners" in f:
            parsed = _clean_refiner_list(f.get("ai_refiners"))
            if parsed is not None:
                ai_refiners = parsed
                ai_overridden_empty = (tier is sc and len(parsed) == 0)
        face_strength = _opt_float(f, "face_strength", face_strength, 0.0, 1.0)
        if "face_count" in f and f["face_count"] is not None:
            try:
                face_count = max(1, min(10, int(f["face_count"])))
            except (ValueError, TypeError):
                pass
        face_use_ref = _opt_bool(f, "face_use_anchor_ref", face_use_ref)
        grain_int = _opt_float(f, "film_grain_intensity", grain_int, 0.0, 1.0)
        grain_int = _opt_float(tier, "film_grain_intensity", grain_int, 0.0, 1.0)
        grain_sat = _opt_float(f, "film_grain_saturation", grain_sat, 0.0, 1.0)
        grain_sat = _opt_float(tier, "film_grain_saturation", grain_sat, 0.0, 1.0)
        wcc = _opt_float(f, "window_color_correction", wcc, 0.0, 1.0)
        wcc = _opt_float(tier, "sliding_window_color_correction_strength", wcc, 0.0, 1.0)
        ff = f.get("ffmpeg") if isinstance(f.get("ffmpeg"), dict) else {}
        ff_deband = _opt_bool(ff, "deband", ff_deband)
        ff_denoise = _opt_float(ff, "denoise", ff_denoise, 0.0, 10.0)
        ff_cas = _opt_float(ff, "cas", ff_cas, 0.0, 1.0)
        eq = ff.get("eq") if isinstance(ff.get("eq"), dict) else {}
        ff_eq["brightness"] = _opt_float(eq, "brightness", ff_eq["brightness"], -1.0, 1.0)
        ff_eq["contrast"] = _opt_float(eq, "contrast", ff_eq["contrast"], 0.1, 3.0)
        ff_eq["saturation"] = _opt_float(eq, "saturation", ff_eq["saturation"], 0.0, 3.0)
        ff_eq["gamma"] = _opt_float(eq, "gamma", ff_eq["gamma"], 0.1, 5.0)

    # Auto-migrate legacy 1x refiner placed in spatial_upsampling unless explicitly disabled
    raw_up = str(raw_spatial_upsampling or "").strip()
    if raw_up.lower() in _AI_REFINER_SET and not ai_overridden_empty and raw_up.lower() not in {x.lower() for x in ai_refiners}:
        ai_refiners = [raw_up, *ai_refiners]

    if pass_num == 1:
        ai_refiners = []
        grain_int = 0.0

    return {
        "ai_refiners": ai_refiners,
        "face_strength": round(face_strength, 3),
        "face_count": face_count,
        "face_use_anchor_ref": face_use_ref,
        "film_grain_intensity": round(grain_int, 3),
        "film_grain_saturation": round(grain_sat, 3),
        "window_color_correction": round(wcc, 3),
        "ffmpeg": {
            "deband": ff_deband,
            "denoise": round(ff_denoise, 3),
            "cas": round(ff_cas, 3),
            "eq": {k: round(v, 3) for k, v in ff_eq.items()},
        },
    }


def stage2_filters_hash_payload(filters_cfg: dict | None, eff_spatial_up: str = "") -> dict:
    """Returns non-default Stage 2 filter keys for task hashing, preserving legacy hashes when filters are off."""
    if not filters_cfg:
        return {}
    out: dict[str, Any] = {}
    ai_refs = list(filters_cfg.get("ai_refiners") or [])
    legacy_single = [eff_spatial_up] if eff_spatial_up and eff_spatial_up.lower() in _AI_REFINER_SET else []
    if ai_refs and ai_refs != legacy_single:
        out["ai_refiners"] = ai_refs
    if any(r.lower() == "h3facerefine" for r in ai_refs) or eff_spatial_up.lower() == "h3facerefine":
        if round(float(filters_cfg.get("face_strength", 0.75)), 3) != 0.75:
            out["face_strength"] = round(float(filters_cfg["face_strength"]), 3)
        if int(filters_cfg.get("face_count", 1)) != 1:
            out["face_count"] = int(filters_cfg["face_count"])
        if not filters_cfg.get("face_use_anchor_ref", True):
            out["face_anchor_ref"] = False
    if float(filters_cfg.get("film_grain_intensity", 0.0)) > 0.001:
        out["film_grain"] = [round(float(filters_cfg["film_grain_intensity"]), 3), round(float(filters_cfg.get("film_grain_saturation", 0.5)), 3)]
    if float(filters_cfg.get("window_color_correction", 0.0)) > 0.001:
        out["window_cc"] = round(float(filters_cfg["window_color_correction"]), 3)
    return out


def apply_filters_to_video_task(task: dict, filters_cfg: dict | None) -> None:
    """Applies Stage 2 film grain, window CC, inline 1x refiner, and schedules remaining chained refiners."""
    if not filters_cfg:
        return
    if float(filters_cfg.get("film_grain_intensity", 0.0)) > 0.001:
        task["film_grain_intensity"] = float(filters_cfg["film_grain_intensity"])
        task["film_grain_saturation"] = float(filters_cfg.get("film_grain_saturation", 0.5))
    if float(filters_cfg.get("window_color_correction", 0.0)) > 0.001:
        task["sliding_window_color_correction_strength"] = float(filters_cfg["window_color_correction"])

    ai_refs = list(filters_cfg.get("ai_refiners") or [])
    cur_up = str(task.get("spatial_upsampling") or "").strip()
    if not cur_up and ai_refs:
        cur_up = ai_refs.pop(0)
        task["spatial_upsampling"] = cur_up
    elif cur_up.lower() in _AI_REFINER_SET and ai_refs and ai_refs[0].lower() == cur_up.lower():
        ai_refs.pop(0)

    if cur_up.lower() == "h3facerefine":
        fs, fc = float(filters_cfg.get("face_strength", 0.75)), int(filters_cfg.get("face_count", 1))
        if round(fs, 3) != 0.75 or fc != 1:
            task["spatial_postprocessing_parameters"] = {"face_strength": fs, "face_count": fc}

    if ai_refs:
        task["_chained_ai_refiners"] = ai_refs
        task["_face_params"] = {
            "face_strength": float(filters_cfg.get("face_strength", 0.75)),
            "face_count": int(filters_cfg.get("face_count", 1)),
            "face_use_anchor_ref": bool(filters_cfg.get("face_use_anchor_ref", True)),
        }


def run_chained_ai_refiners(ctx, lazy_session, out_mp4: Path, task: dict, dummy_render: bool = False) -> None:
    """Runs any remaining 1x AI refiners sequentially on a newly generated Stage 2 MP4."""
    chained = list(task.get("_chained_ai_refiners") or [])
    if not chained or not out_mp4.is_file():
        return
    from .wgp_bridge import resolve_session_for_backend
    fp = task.get("_face_params") or {}
    anchor_p = str(task.get("image_start") or "")
    seed_val = int(task.get("seed") or 1000)

    for ref_method in chained:
        if dummy_render:
            print(f"      [Filter Chain] Dummy-applying 1x refiner '{ref_method}' on {out_mp4.name}", flush=True)
            continue
        ref_backend = resolve_upscale_backend(ctx, method=ref_method, explicit_backend=str(task.get("_backend") or ""))
        session = resolve_session_for_backend(lazy_session, ref_backend)
        if not hasattr(session, "run_media_postprocessing"):
            session = resolve_session_for_backend(lazy_session, "local")
        kwargs: dict[str, Any] = {
            "spatial_upsampling": ref_method,
            "seed": seed_val,
            "return_media": False,
        }
        if ref_method.lower() == "h3facerefine":
            kwargs["spatial_postprocessing_parameters"] = {
                "face_strength": float(fp.get("face_strength", 0.75)),
                "face_count": int(fp.get("face_count", 1)),
            }
            if fp.get("face_use_anchor_ref", True) and anchor_p and Path(anchor_p).is_file():
                kwargs["postprocessing_reference_images"] = [anchor_p]
        print(f"      [Filter Chain] Applying 1x refiner '{ref_method}' on {out_mp4.name}...", flush=True)
        res = session.run_media_postprocessing(str(out_mp4), **kwargs)
        gen_files = getattr(res, "generated_files", None) or getattr(res, "output_files", None) or getattr(res, "files", None) or []
        if not getattr(res, "success", True) or not gen_files:
            errs = [getattr(e, "message", str(e)) for e in (getattr(res, "errors", None) or [])]
            raise RuntimeError(f"Chained refiner '{ref_method}' failed on {out_mp4.name}: {errs}")
        prod = Path(gen_files[0])
        if prod.is_file() and prod.resolve() != out_mp4.resolve():
            tmp_swap = out_mp4.with_name(f".refine_swap_{out_mp4.name}")
            shutil.move(str(prod), str(tmp_swap))
            os.replace(str(tmp_swap), str(out_mp4))


def validate_late_upsampling_method(method: str, ai_refiner: str = "", film_grain_intensity: float = 0.0) -> str | None:
    """Returns None if valid for late MP4 post-processing, else an error message."""
    clean = str(method or "").strip().lower()
    ref_clean = str(ai_refiner or "").strip().lower()
    if not clean and not ref_clean and float(film_grain_intensity or 0.0) <= 0.0:
        return "Choose a spatial upscaling method, a 1x AI refiner, or film grain."
    for tok in (t for t in (clean, ref_clean) if t):
        if tok.startswith("h3_vae"):
            return "h3_vae is a Stage 2 latent VAE decoder and cannot post-process an existing MP4. Choose flashvsrplus*2, seedvr2*2, or a 1x refiner."
        base = tok.split("*", 1)[0]
        if not any(base.startswith(p) for p in _LATE_PREFIXES):
            return f"Unsupported post-processing method '{tok}'."
    return None


def resolve_scene_canonical_stem(sc: dict, renders_dir: Path | None, res_str: str) -> tuple[str, bool, int | None]:
    """Computes (canonical_stem, is_two_pass, target_pass_num) for a scene dict matching stage2_scenes naming."""
    sid = sc.get("id", "")
    mode = sc.get("mode", "solo")
    is_two_pass = mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
    if is_two_pass:
        return f"{sid}_Pass2_Final_{res_str}", True, 2
    if sc.get("output_name"):
        raw = str(sc["output_name"])
    elif mode in ("duet_method_a", "duet_a"):
        cand = next(
            (c for c in (f"{sid}_duet_a", f"{sid}_duet_method_a", f"{sid}_duet_stereo") if renders_dir and (renders_dir / f"{c}_{res_str}.mp4").exists()),
            None,
        )
        raw = cand or f"{sid}_duet_a"
    elif mode in ("solo", "male_solo", "female_solo"):
        stem_key = sc.get("stem") or ("female" if mode == "female_solo" else "male")
        cand = next(
            (c for c in (f"{sid}_{mode}", f"{sid}_solo", f"{sid}_male_solo", f"{sid}_female_solo", f"{sid}_{stem_key}") if renders_dir and (renders_dir / f"{c}_{res_str}.mp4").exists()),
            None,
        )
        raw = cand or (f"{sid}_{mode}" if mode != "solo" else f"{sid}_solo")
    elif mode in ("silent_locked", "silent", "ambient"):
        raw = f"{sid}_silent"
    else:
        raw = f"{sid}_{sc.get('stem', 'silent')}"
    stem = f"{raw}_{res_str}" if res_str not in raw else raw
    return stem, False, None


def upscale_scene_clip(
    ctx,
    lazy_session,
    scene_id: str,
    method: str = "",
    take_id: str | None = None,
    dummy_render: bool = False,
    ai_refiner: str = "",
    face_strength: float = 0.75,
    face_count: int = 1,
    film_grain_intensity: float = 0.0,
    film_grain_saturation: float = 0.5,
    backend: str = "",
) -> dict:
    """Runs late AI spatial upscaling and/or 1x refiner/grain on an existing rendered clip, saving a new Take."""
    from .wgp_bridge import resolve_session_for_backend, write_engine_status

    method = str(method or "").strip()
    ai_refiner = str(ai_refiner or "").strip()
    film_grain_intensity = max(0.0, min(1.0, float(film_grain_intensity or 0.0)))
    film_grain_saturation = max(0.0, min(1.0, float(film_grain_saturation if film_grain_saturation is not None else 0.5)))

    err = validate_late_upsampling_method(method, ai_refiner=ai_refiner, film_grain_intensity=film_grain_intensity)
    if err:
        raise ValueError(err)

    sc = next((s for s in ctx.scenes if str(s.get("id", "")).upper() == scene_id.strip().upper()), None)
    if not sc:
        raise ValueError(f"Scene '{scene_id}' not found in project config.")

    sid = sc["id"]
    _, vrspec = ctx.resolve_video_renderer(sc)
    res_str = ctx.resolve_scene_resolution(sc, vrspec)
    out_name, _, pass_num = resolve_scene_canonical_stem(sc, ctx.renders_dir, res_str)
    active_mp4 = resolve_rendered_clip_path(ctx.renders_dir, out_name, promote=True)

    manifest = load_takes_manifest(ctx.project_dir)
    sc_entry = manifest.setdefault("scenes", {}).setdefault(sid, {"active_takes": {}, "takes": []})
    src_take = None
    src_mp4 = active_mp4
    if take_id:
        src_take = next((t for t in sc_entry.get("takes", []) if t.get("take_id") == take_id), None)
        if not src_take:
            raise ValueError(f"Take '{take_id}' not found for scene '{sid}'.")
        rel = src_take.get("vault_rel_mp4")
        if rel and (ctx.renders_dir / rel).is_file():
            src_mp4 = ctx.renders_dir / rel
    if not src_mp4.is_file() or src_mp4.stat().st_size < 1000:
        raise FileNotFoundError(f"No rendered clip found for scene '{sid}' at {src_mp4}")

    t_start = time.time()
    scale = parse_upsampling_scale(method) if method else 1.0
    src_dims = probe_video_dimensions(ctx.ffmpeg, src_mp4)
    if scale > 1.0 and src_dims and src_dims[0] >= 1920:
        print(f"[Upscale] Note: clip is already {src_dims[0]}x{src_dims[1]}; proceeding with explicit '{method}' request.", flush=True)

    op_label = "+".join(x for x in (method, ai_refiner, f"grain={film_grain_intensity:.2f}" if film_grain_intensity > 0 else "") if x)
    init_backend = "local" if dummy_render else resolve_upscale_backend(ctx, method=method, ai_refiner=ai_refiner, explicit_backend=backend)
    up_backend = init_backend
    write_engine_status(ctx, {"state": "rendering", "backend": up_backend, "phase": "upscaling", "raw_phase": f"Post-processing {sid} ({op_label})", "filename": active_mp4.name, "pct": 10.0}, backend=up_backend, task_stem=out_name)
    tmp_out = ctx.renders_dir / f".upscale_tmp_{sid}_{int(time.time())}.mp4"
    post_steps = [s for s in (method, ai_refiner) if s] or [""]
    _, anchor_path = ctx.resolve_anchor_path(sc.get("anchor", "")) if sc.get("anchor") else ("", Path())

    try:
        if dummy_render:
            sw, sh = src_dims if src_dims else (960, 544)
            tw = max(2, int(round(sw * scale / 2.0)) * 2)
            th = max(2, int(round(sh * scale / 2.0)) * 2)
            vf_parts = [f"scale={tw}:{th}:flags=lanczos"] if (tw != sw or th != sh) else ["null"]
            subprocess.run(
                [
                    str(ctx.ffmpeg), "-y", "-i", str(src_mp4),
                    "-vf", ",".join(vf_parts),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "copy",
                    str(tmp_out),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            seed_val = int(sc.get("seed") if sc.get("seed") is not None else (sc.get("seed_m") or 1000))
            cur_src = src_mp4
            for step_idx, step_method in enumerate(post_steps):
                is_last = step_idx == (len(post_steps) - 1)
                step_backend = (
                    "local"
                    if (not step_method and film_grain_intensity > 0)
                    else resolve_upscale_backend(ctx, method=step_method, ai_refiner=ai_refiner, explicit_backend=backend)
                )
                up_backend = step_backend
                session = resolve_session_for_backend(lazy_session, step_backend)
                kwargs: dict[str, Any] = {
                    "spatial_upsampling": step_method,
                    "film_grain_intensity": film_grain_intensity if is_last else 0.0,
                    "film_grain_saturation": film_grain_saturation,
                    "seed": seed_val,
                    "return_media": False,
                }
                if step_method.lower() == "h3facerefine":
                    kwargs["spatial_postprocessing_parameters"] = {"face_strength": float(face_strength), "face_count": int(face_count)}
                    if anchor_path.is_file():
                        kwargs["postprocessing_reference_images"] = [str(anchor_path)]
                eng_label = "fal.ai" if step_backend == "fal" else "Wan2GP"
                print(f"[Upscale] Running {eng_label} post-processing '{step_method or 'grain'}' on {cur_src.name}...", flush=True)
                result = session.run_media_postprocessing(str(cur_src), **kwargs)
                if not getattr(result, "success", True):
                    errs = [getattr(e, "message", str(e)) for e in (getattr(result, "errors", None) or [])]
                    raise RuntimeError(f"Post-processing failed for {cur_src.name}: {errs}")
                gen_files = getattr(result, "generated_files", None) or getattr(result, "output_files", None) or getattr(result, "files", None) or []
                produced = Path(gen_files[0]) if gen_files else None
                if not produced or not produced.is_file():
                    cands = sorted(ctx.renders_dir.glob(f"*{cur_src.stem}*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                    produced = next((c for c in cands if c.resolve() != cur_src.resolve()), None)
                if not produced or not produced.is_file():
                    raise RuntimeError(f"Wan2GP post-processing finished but no output file was located for {cur_src.name}.")
                if produced.resolve() != tmp_out.resolve():
                    shutil.move(str(produced), str(tmp_out))
                cur_src = tmp_out

        elapsed = round(time.time() - t_start, 2)
        out_dims = probe_video_dimensions(ctx.ffmpeg, tmp_out)
        out_res_str = f"{out_dims[0]}x{out_dims[1]}" if out_dims else res_str
        digest = hashlib.sha256(f"{src_mp4.name}:{op_label}:{time.time()}".encode()).hexdigest()[:12]
        new_take_id = f"take_up_{digest}"
        vault_rel = f"takes/{sid}/{out_name}__up_{digest}.mp4"
        vault_mp4 = ctx.renders_dir / vault_rel
        vault_mp4.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_out), str(vault_mp4))
        safe_link_or_copy(vault_mp4, active_mp4)

        strip_src = _find_strip(ctx.project_dir, sid, out_name)
        vault_rel_strip = None
        if strip_src and strip_src.is_file():
            vault_rel_strip = f"takes/{sid}/{out_name}__up_{digest}_strip.jpg"
            safe_link_or_copy(strip_src, ctx.renders_dir / vault_rel_strip)
        if ctx.trimmed_dir.is_dir():
            for old_trim in ctx.trimmed_dir.glob(f"*_{sid}_*.mp4"):
                try:
                    old_trim.unlink()
                except OSError:
                    pass

        created_ts = time.time()
        state = ctx.load_state_hashes()
        sr_old = (state.get("scene_renders") or {}).get(out_name) or {}
        eff_th = (src_take.get("task_hash") if src_take else "") or (state.get("task_hashes") or state.get("tasks") or {}).get(out_name, "")
        eff_rs = (src_take.get("render_start") if (src_take and src_take.get("render_start") is not None) else sr_old.get("render_start", float(sc.get("start", 0))))
        eff_re = (src_take.get("render_end") if (src_take and src_take.get("render_end") is not None) else sr_old.get("render_end", float(sc.get("end", 0))))
        eff_fr = (src_take.get("frames") if (src_take and src_take.get("frames") is not None) else sr_old.get("frames", probe_video_frames(ctx.ffmpeg, tmp_out) or 0))
        existing_takes = sc_entry.setdefault("takes", [])
        take_entry = {
            "take_id": new_take_id,
            "take_number": len(existing_takes) + 1,
            "scene_id": sid,
            "pass_num": pass_num,
            "out_name": out_name,
            "task_hash": eff_th,
            "source_take_id": src_take.get("take_id") if src_take else sc_entry.get("active_take_id"),
            "seed": src_take.get("seed") if src_take else sc.get("seed"),
            "prompt": src_take.get("prompt") if src_take else sc.get("prompt"),
            "anchor_id": sc.get("anchor"),
            "render_start": eff_rs,
            "render_end": eff_re,
            "frames": eff_fr,
            "created_at": round(created_ts, 3),
            "created_iso": datetime.datetime.fromtimestamp(created_ts, datetime.timezone.utc).isoformat(),
            "vault_rel_mp4": vault_rel,
            "vault_rel_strip": vault_rel_strip,
            "file_path": f"work/renders_v3/{vault_rel}",
            "strip_path": f"work/renders_v3/{vault_rel_strip}" if vault_rel_strip else None,
            "elapsed_s": elapsed,
            "size_bytes": vault_mp4.stat().st_size,
            "resolution": res_str,
            "output_resolution": out_res_str,
            "spatial_upsampling": method,
            "ai_refiner": ai_refiner,
            "model_type": f"postprocess:{op_label}",
            "backend": "local" if dummy_render else up_backend,
            "scene_snapshot": copy.deepcopy(sc),
            "label": f"Take {len(existing_takes) + 1} ({op_label})",
        }
        existing_takes.append(take_entry)
        sc_entry.setdefault("active_takes", {})[_pass_key(pass_num)] = new_take_id
        sc_entry["active_take_id"] = new_take_id
        save_takes_manifest(ctx.project_dir, manifest)

        sr_entry = state.setdefault("scene_renders", {}).setdefault(out_name, {})
        sr_entry["output_resolution"] = out_res_str
        sr_entry["spatial_upsampling"] = op_label
        ctx.save_state_hashes(state, updated_keys={out_name})

        write_engine_status(ctx, {"state": "idle", "backend": up_backend, "pct": 100.0, "status_text": f"Post-processed {sid} ({op_label}) -> {out_res_str}"}, backend=up_backend, task_stem=out_name)
        print(f"[Upscale] Saved take {new_take_id} ({op_label}, {out_res_str}) in {elapsed}s -> {active_mp4.name}", flush=True)
        return take_entry
    finally:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass


def handle_upscale_api(handler, srv, body: dict | None) -> None:
    """POST /api/takes/upscale handler: dispatches non-blocking clip upscaling / 1x refiner post-processing."""
    from http import HTTPStatus

    body = body or {}
    slug = str(body.get("project") or body.get("slug") or "").strip()
    sid = str(body.get("scene_id") or "").strip()
    method = str(body.get("method") or "").strip()
    ai_refiner = str(body.get("ai_refiner") or "").strip()
    film_grain = float(body.get("film_grain_intensity") or 0.0)
    if not method and not ai_refiner and film_grain <= 0.0:
        method = "flashvsrplus*2"
    take_id = body.get("take_id")

    if not slug or not sid:
        handler.send_error_json("project and scene_id are required")
        return
    err = validate_late_upsampling_method(method, ai_refiner=ai_refiner, film_grain_intensity=film_grain)
    if err:
        handler.send_error_json(err)
        return
    explicit_backend = str(body.get("backend") or "").strip().lower()
    if explicit_backend in ("local", "fal"):
        eff_backend = explicit_backend
    else:
        from .media_utils import find_local_config
        cfg_d = find_local_config(srv.PROJECTS_DIR / slug)
        b_raw = str(cfg_d.get("default_upscale_backend") or cfg_d.get("default_execution_backend") or "local").strip().lower()
        eff_backend = "local" if (method.lower().startswith("dlss5") or (not method and film_grain > 0)) else ("fal" if b_raw == "fal" else "local")
    if eff_backend == "local":
        with srv.render_lock:
            if getattr(srv, "local_adhoc_busy", False) or (not getattr(srv, "queue_worker_active", False) and srv.render_status.get("running")) or getattr(srv, "engine_workers", {}).get("local", {}).get("running"):
                handler.send_error_json("Local GPU is currently busy rendering. Stop it or wait for it to finish.", status=HTTPStatus.CONFLICT)
                return
            srv.local_adhoc_busy = True

    proj_dir = srv.PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        if eff_backend == "local":
            with srv.render_lock: srv.local_adhoc_busy = False
        handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
        return
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        if eff_backend == "local":
            with srv.render_lock: srv.local_adhoc_busy = False
        handler.send_error_json(f"No project config found in {proj_dir}", status=HTTPStatus.NOT_FOUND)
        return
    config_file = str(jsons[-1])

    cmd_args = [config_file, "upscale", sid, "--method", method]
    if ai_refiner:
        cmd_args.extend(["--ai-refiner", ai_refiner])
    if film_grain > 0.0:
        cmd_args.extend(["--film-grain", str(film_grain)])
    if take_id:
        cmd_args.extend(["--take-id", str(take_id)])
    if explicit_backend in ("local", "fal"):
        cmd_args.extend(["--upscale-backend", explicit_backend])

    def _run():
        try:
            code = srv.execute_single_render(cmd_args, slug)
            if code == 0:
                srv.broadcast_log(f"[UPSCALE_DONE] {json.dumps({'scene_id': sid, 'method': method, 'ai_refiner': ai_refiner})}")
            else:
                srv.broadcast_log(f"[Studio UI Error] Clip post-processing failed for {sid} (exit code {code}).")
        finally:
            if eff_backend == "local":
                with srv.render_lock:
                    srv.local_adhoc_busy = False
                if getattr(srv, "queue_worker_active", False) and hasattr(srv, "wake_enabled_engine_workers"):
                    srv.wake_enabled_engine_workers(slug)

    threading.Thread(target=_run, daemon=True).start()
    handler.send_json({"success": True, "started": True, "scene_id": sid, "method": method, "ai_refiner": ai_refiner})
