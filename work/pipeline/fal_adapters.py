"""Extra fal.ai non-MiniMax scenic video profiles and request payload adapters."""

from __future__ import annotations

from typing import Any


SCENIC_FAL_ADAPTERS = frozenset({
    "kling_i2v",
    "wan26_i2v",
    "seedance_i2v",
    "seedance_v1_i2v",
    "veo31_i2v",
})


EXTRA_FAL_VIDEO_PROFILES: dict[str, dict[str, Any]] = {
    "fal_kling_v3_std": {
        "name": "[fal.ai] Kling v3 Standard I2V (Start/End Scenic)",
        "model_type": "kling_v3_std",
        "backend": "fal",
        "fal_endpoint": "fal-ai/kling-video/v3/standard/image-to-video",
        "fal_adapter": "kling_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.084, "768p": 0.084, "1080p": 0.084, "2k": 0.112},
    },
    "fal_kling_v3_pro": {
        "name": "[fal.ai] Kling v3 Pro I2V (Cinematic + Start/End)",
        "model_type": "kling_v3_pro",
        "backend": "fal",
        "fal_endpoint": "fal-ai/kling-video/v3/pro/image-to-video",
        "fal_adapter": "kling_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.112, "768p": 0.112, "1080p": 0.112, "2k": 0.168},
    },
    "fal_seedance_2_fast": {
        "name": "[fal.ai] Seedance 2.0 Fast I2V (Action/Choreography)",
        "model_type": "seedance_2_fast",
        "backend": "fal",
        "fal_endpoint": "bytedance/seedance-2.0/fast/image-to-video",
        "fal_adapter": "seedance_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.04, "768p": 0.06, "1080p": 0.09, "2k": 0.09},
    },
    "fal_seedance_v1_pro": {
        "name": "[fal.ai] Seedance 1.0 Pro I2V (1080p Smooth Motion)",
        "model_type": "seedance_v1_pro",
        "backend": "fal",
        "fal_endpoint": "fal-ai/bytedance/seedance/v1/pro/image-to-video",
        "fal_adapter": "seedance_v1_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 10.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.05, "768p": 0.09, "1080p": 0.15, "2k": 0.15},
    },
    "fal_wan_26_i2v": {
        "name": "[fal.ai] Wan v2.6 I2V (Moody / Artistic Scenic)",
        "model_type": "wan_26_i2v",
        "backend": "fal",
        "fal_endpoint": "wan/v2.6/image-to-video",
        "fal_adapter": "wan26_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 15.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.07, "768p": 0.10, "1080p": 0.15, "2k": 0.15},
    },
    "fal_veo31_fast": {
        "name": "[fal.ai] Google Veo 3.1 Fast I2V (Coherent Physics)",
        "model_type": "veo31_fast",
        "backend": "fal",
        "fal_endpoint": "fal-ai/veo3.1/fast/image-to-video",
        "fal_adapter": "veo31_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 8.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.10, "768p": 0.10, "1080p": 0.15, "2k": 0.25},
    },
    "fal_veo31_lite": {
        "name": "[fal.ai] Google Veo 3.1 Lite I2V (Budget Coherent)",
        "model_type": "veo31_lite",
        "backend": "fal",
        "fal_endpoint": "fal-ai/veo3.1/lite/image-to-video",
        "fal_adapter": "veo31_i2v",
        "frames_formula": "ltx2",
        "prompt_style": "prose",
        "max_duration_s": 8.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "",
        "price_per_sec_usd": {"480p": 0.05, "768p": 0.05, "1080p": 0.08, "2k": 0.08},
    },
}


def build_scenic_fal_arguments(
    adapter: str,
    prompt: str,
    start_url: str,
    end_url: str,
    w: int,
    dur_s: float,
    seed: int,
) -> dict[str, Any]:
    """Constructs endpoint-specific JSON arguments for non-MiniMax scenic fal.ai models."""
    d = max(1.0, float(dur_s or 5.0))
    if adapter == "kling_i2v":
        args: dict[str, Any] = {
            "prompt": prompt,
            "duration": str(max(3, min(15, int(round(d))))),
            "generate_audio": False,
        }
        if start_url:
            args["start_image_url"] = start_url
        if end_url:
            args["end_image_url"] = end_url
        return args

    if adapter == "wan26_i2v":
        dur_tok = "5" if d <= 6.0 else ("10" if d <= 11.0 else "15")
        args = {
            "prompt": prompt,
            "resolution": "1080p" if w >= 1800 else "720p",
            "duration": dur_tok,
            "seed": seed,
        }
        if start_url:
            args["image_url"] = start_url
        return args

    if adapter == "seedance_i2v":
        args = {
            "prompt": prompt,
            "resolution": "720p" if w >= 1000 else "480p",
            "duration": max(4, min(15, int(round(d)))),
            "aspect_ratio": "16:9",
            "generate_audio": False,
            "seed": seed,
        }
        if start_url:
            args["image_url"] = start_url
        if end_url:
            args["end_image_url"] = end_url
        return args

    if adapter == "seedance_v1_i2v":
        res_tok = "1080p" if w >= 1800 else ("720p" if w >= 1000 else "480p")
        args = {
            "prompt": prompt,
            "resolution": res_tok,
            "duration": "10" if d >= 7.5 else "5",
            "aspect_ratio": "16:9",
            "seed": seed,
        }
        if start_url:
            args["image_url"] = start_url
        return args

    if adapter == "veo31_i2v":
        dur_tok = "4s" if d <= 4.5 else ("6s" if d <= 6.5 else "8s")
        args = {
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "duration": dur_tok,
            "resolution": "1080p" if w >= 1800 else "720p",
            "generate_audio": False,
        }
        if start_url:
            args["image_url"] = start_url
        return args

    raise ValueError(f"Unsupported scenic fal adapter: {adapter}")
