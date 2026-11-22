"""fal.ai model catalog, Windows DPAPI secret storage, backend routing precedence, and cost estimation."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import sys
from pathlib import Path
from typing import Any

from .media_utils import WORKSPACE_ROOT, find_local_config

_VALID_BACKENDS = ("local", "fal", "minimax")

FAL_ANCHOR_PROFILES: dict[str, dict[str, Any]] = {
    "fal_flux2_klein_9b": {
        "name": "[fal.ai] FLUX.2 Klein 9B (Multi-Ref)",
        "model_type": "flux2_klein_9b",
        "backend": "fal",
        "fal_endpoint": "fal-ai/flux-2/klein/9b",
        "fal_adapter": "flux_image",
        "ref_tag_style": "image N",
        "num_inference_steps": 4,
        "guidance_scale": 1.0,
        "price_per_unit_usd": 0.015,
    },
    "fal_flux2_klein_4b": {
        "name": "[fal.ai] FLUX.2 Klein 4B (Fast Multi-Ref)",
        "model_type": "flux2_klein_4b",
        "backend": "fal",
        "fal_endpoint": "fal-ai/flux-2/klein/4b",
        "fal_adapter": "flux_image",
        "ref_tag_style": "image N",
        "num_inference_steps": 4,
        "guidance_scale": 1.0,
        "price_per_unit_usd": 0.008,
    },
    "fal_qwen_edit_2511": {
        "name": "[fal.ai] Qwen Image Edit 2511 (Multi-Subject)",
        "model_type": "qwen_image_edit_plus2_20B",
        "backend": "fal",
        "fal_endpoint": "fal-ai/qwen-image-edit-2511",
        "fal_adapter": "qwen_image_edit",
        "ref_tag_style": "image N",
        "num_inference_steps": 28,
        "guidance_scale": 4.0,
        "price_per_unit_usd": 0.03,
    },
    "fal_qwen_image_21": {
        "name": "[fal.ai] Qwen Image 2.1 (2K Spatial)",
        "model_type": "qwen_image_21_7B",
        "backend": "fal",
        "fal_endpoint": "fal-ai/qwen-image-2.0",
        "fal_adapter": "qwen_image",
        "ref_tag_style": "<imageN>",
        "num_inference_steps": 40,
        "guidance_scale": 4.0,
        "price_per_unit_usd": 0.025,
    },
    "fal_flux3_klein": {
        "name": "[fal.ai] FLUX 3 Klein (Ordered Multi-Ref)",
        "model_type": "flux3_klein_base",
        "backend": "fal",
        "fal_endpoint": "fal-ai/flux-3/klein",
        "fal_adapter": "flux_image",
        "ref_tag_style": "<ref_image_N0>",
        "num_inference_steps": 30,
        "guidance_scale": 3.5,
        "price_per_unit_usd": 0.025,
    },
    "fal_fibo2": {
        "name": "[fal.ai] Bria Fibo 2.0",
        "model_type": "fibo2_dev",
        "backend": "fal",
        "fal_endpoint": "bria/fibo/generate",
        "fal_adapter": "flux_image",
        "ref_tag_style": "image N",
        "num_inference_steps": 40,
        "guidance_scale": 5.0,
        "price_per_unit_usd": 0.04,
    },
    "fal_sensenova_u1": {
        "name": "[fal.ai] SenseNova U1.5",
        "model_type": "sensenova_u1_5_8b_mot",
        "backend": "fal",
        "fal_endpoint": "fal-ai/sensenova-u1",
        "fal_adapter": "flux_image",
        "ref_tag_style": "Image N",
        "num_inference_steps": 40,
        "guidance_scale": 4.0,
        "price_per_unit_usd": 0.03,
    },
}

FAL_VIDEO_PROFILES: dict[str, dict[str, Any]] = {
    "fal_minimax_h3_i2v": {
        "name": "[fal.ai] MiniMax H3 First/Last Frame (FL2VA 2K)",
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "backend": "fal",
        "fal_endpoint": "minimax/h3/image-to-video",
        "fal_lipsync_endpoint": "minimax/h3-max/lip-sync/image-to-video",
        "fal_adapter": "minimax_h3_fl2va",
        "frames_formula": "h3",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": False,
        "audio_prompt_type": "A",
        "price_per_sec_usd": {"480p": 0.04, "768p": 0.07, "1080p": 0.10, "2k": 0.14},
    },
    "fal_minimax_h3_lipsync": {
        "name": "[fal.ai] MiniMax H3 Max Lip-Sync (Audio-Driven)",
        "model_type": "minimax_h3_fl2va_pdd",
        "backend": "fal",
        "fal_endpoint": "minimax/h3-max/lip-sync/image-to-video",
        "fal_adapter": "minimax_h3_lipsync",
        "frames_formula": "h3",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "A",
        "price_per_sec_usd": {"480p": 0.05, "768p": 0.08, "1080p": 0.12, "2k": 0.16},
    },
    "fal_minimax_h3_ref2v": {
        "name": "[fal.ai] MiniMax H3 Multimodal Reference (Ref2VA 2K)",
        "model_type": "minimax_h3_ref2va_pruned_ptd_refasymopd",
        "backend": "fal",
        "fal_endpoint": "minimax/h3/reference-to-video",
        "fal_adapter": "minimax_h3_ref2v",
        "frames_formula": "h3_ref2va",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": False,
        "supports_image_refs": True,
        "audio_prompt_type": "AS",
        "price_per_sec_usd": {"480p": 0.05, "768p": 0.08, "1080p": 0.12, "2k": 0.16},
    },
    "fal_minimax_h3_max_ref2v": {
        "name": "[fal.ai] MiniMax H3 Max Reference-to-Video",
        "model_type": "minimax_h3_ref2va_ptd_refasymopd",
        "backend": "fal",
        "fal_endpoint": "minimax/h3-max/reference-to-video",
        "fal_adapter": "minimax_h3_ref2v",
        "frames_formula": "h3_ref2va",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": False,
        "supports_image_refs": True,
        "audio_prompt_type": "AS",
        "price_per_sec_usd": {"480p": 0.05, "768p": 0.08, "1080p": 0.12, "2k": 0.16},
    },
    "fal_ltx23_a2v": {
        "name": "[fal.ai] LTX-2.3 Audio-to-Video",
        "model_type": "ltx2_22B_distilled_1_1",
        "backend": "fal",
        "fal_endpoint": "fal-ai/ltx-2.3/audio-to-video",
        "fal_adapter": "ltx2_a2v",
        "frames_formula": "ltx2",
        "prompt_style": "ltx2",
        "max_duration_s": 20.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "A",
        "price_per_sec_usd": {"480p": 0.04, "768p": 0.06, "1080p": 0.09, "2k": 0.12},
    },
    "fal_ltx25_a2v": {
        "name": "[fal.ai] LTX-2.5 Audio-to-Video",
        "model_type": "ltx2_25_22B",
        "backend": "fal",
        "fal_endpoint": "fal-ai/ltx-2.5/audio-to-video",
        "fal_adapter": "ltx2_a2v",
        "frames_formula": "ltx2",
        "prompt_style": "ltx2",
        "max_duration_s": 20.0,
        "supports_end_anchor": False,
        "supports_image_refs": False,
        "audio_prompt_type": "A",
        "price_per_sec_usd": {"480p": 0.05, "768p": 0.07, "1080p": 0.10, "2k": 0.14},
    },
}

from .fal_adapters import EXTRA_FAL_VIDEO_PROFILES  # noqa: E402
FAL_VIDEO_PROFILES.update(EXTRA_FAL_VIDEO_PROFILES)

FAL_UPSCALE_ROUTES: dict[str, dict[str, Any]] = {
    "seedvr2": {"endpoint": "fal-ai/seedvr/upscale/video", "adapter": "seedvr2_video", "price_per_sec_usd": 0.03},
    "flashvsr": {"endpoint": "fal-ai/flashvsr/upscale/video", "adapter": "flashvsr_video", "price_per_sec_usd": 0.025},
    "h3facerefine": {"endpoint": "fal-ai/sync-lipsync/v2", "adapter": "sync_lipsync_v2", "price_per_sec_usd": 0.05},
    "ltx25_refine": {"endpoint": "fal-ai/seedvr/upscale/video", "adapter": "seedvr2_video", "price_per_sec_usd": 0.03},
}

MINIMAX_VIDEO_PROFILES: dict[str, dict[str, Any]] = {
    "minimax_api_h3": {
        "name": "[MiniMax API] MiniMax-H3 Flagship (768P / 2K + Audio & Refs)",
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "backend": "minimax",
        "minimax_model": "MiniMax-H3",
        "frames_formula": "h3",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": True,
        "audio_prompt_type": "AS",
        "price_per_sec_usd": {"480p": 0.06, "768p": 0.08, "1080p": 0.13, "2k": 0.13},
    },
    "minimax_api_h3_highspeed": {
        "name": "[MiniMax API] MiniMax-H3-Highspeed (480P / 768P)",
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "backend": "minimax",
        "minimax_model": "MiniMax-H3-Highspeed",
        "frames_formula": "h3",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": True,
        "audio_prompt_type": "AS",
        "price_per_sec_usd": {"480p": 0.04, "768p": 0.06, "1080p": 0.06, "2k": 0.06},
    },
    "minimax_api_h3_lite": {
        "name": "[MiniMax API] MiniMax-H3-Lite (Fast Draft)",
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "backend": "minimax",
        "minimax_model": "MiniMax-H3-Lite",
        "frames_formula": "h3",
        "prompt_style": "h3",
        "max_duration_s": 15.0,
        "supports_end_anchor": True,
        "supports_image_refs": True,
        "audio_prompt_type": "AS",
        "price_per_sec_usd": {"480p": 0.025, "768p": 0.04, "1080p": 0.04, "2k": 0.04},
    },
}


# ---------------------------------------------------------------------------
# Windows DPAPI secret encryption & local_config.json helpers
# ---------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_is_supported() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def dpapi_encrypt_secret(plaintext: str) -> str:
    """Encrypts a UTF-8 secret bound to the current Windows user via DPAPI (CryptProtectData)."""
    if not dpapi_is_supported():
        raise RuntimeError("Windows DPAPI is only available on Windows hosts.")
    raw = str(plaintext).encode("utf-8")
    in_buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), "MusicVideoStudioFAL", None, None, None, 0x1, ctypes.byref(out_blob)
    ):
        raise OSError("CryptProtectData failed to encrypt secret.")
    try:
        cipher = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(cipher).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def dpapi_decrypt_secret(ciphertext_b64: str) -> str:
    """Decrypts a base64 DPAPI blob created by dpapi_encrypt_secret on this Windows user account."""
    if not dpapi_is_supported():
        raise RuntimeError("Windows DPAPI is only available on Windows hosts.")
    raw = base64.b64decode(str(ciphertext_b64).strip().encode("ascii"))
    in_buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob)
    ):
        raise OSError("CryptUnprotectData failed (wrong Windows user or corrupted DPAPI blob).")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _find_local_config_path(root_dir: Path | None = None) -> Path:
    base = Path(root_dir) if root_dir else WORKSPACE_ROOT
    for d in (base, base.parent, WORKSPACE_ROOT, Path.cwd()):
        for fn in ("config.local.json", "local_config.json"):
            cand = d / fn
            if cand.is_file():
                return cand
    return WORKSPACE_ROOT / "local_config.json"


def read_local_config(root_dir: Path | None = None) -> dict:
    p = _find_local_config_path(root_dir)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return find_local_config(Path(root_dir) if root_dir else WORKSPACE_ROOT)


def write_local_config(updates: dict[str, Any], root_dir: Path | None = None, remove_keys: tuple[str, ...] = ()) -> Path:
    p = _find_local_config_path(root_dir)
    cur = read_local_config(root_dir)
    cur.update(updates)
    for rk in remove_keys:
        cur.pop(rk, None)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return p


def _resolve_secret(env_vars: tuple[str, ...], dpapi_field: str, plain_field: str, root_dir: Path | None = None) -> tuple[str, str]:
    """Shared secret resolver: env var -> Windows DPAPI blob in local_config.json -> plaintext local fallback."""
    for ev in env_vars:
        val = os.environ.get(ev, "").strip()
        if val:
            return val, "env"
    cfg = read_local_config(root_dir)
    dpapi_blob = str(cfg.get(dpapi_field) or "").strip()
    if dpapi_blob:
        try:
            plain = dpapi_decrypt_secret(dpapi_blob).strip()
            if plain:
                return plain, "dpapi"
        except Exception:
            return "", "dpapi_error"
    plain_cfg = str(cfg.get(plain_field) or "").strip()
    if plain_cfg:
        return plain_cfg, "plaintext_local"
    return "", "none"


def resolve_fal_key(root_dir: Path | None = None) -> tuple[str, str]:
    """Returns (api_key, source) checking FAL_KEY env -> DPAPI local_config -> plaintext fallback."""
    return _resolve_secret(("FAL_KEY",), "fal_key_dpapi", "fal_key", root_dir=root_dir)


def resolve_minimax_key(root_dir: Path | None = None) -> tuple[str, str]:
    """Returns (api_key, source) checking MINIMAX_API_KEY env -> DPAPI local_config -> plaintext fallback."""
    return _resolve_secret(("MINIMAX_API_KEY", "MINIMAX_KEY"), "minimax_key_dpapi", "minimax_key", root_dir=root_dir)


def _apply_secret_update(updates: dict, removes: list[str], secret_val: str | None, clear_flag: bool, dpapi_field: str, plain_field: str):
    if clear_flag:
        removes.extend([dpapi_field, plain_field])
    elif secret_val is not None and secret_val.strip():
        clean_k = secret_val.strip()
        if dpapi_is_supported():
            updates[dpapi_field] = dpapi_encrypt_secret(clean_k)
            removes.append(plain_field)
        else:
            updates[plain_field] = clean_k


def save_fal_settings(
    root_dir: Path | None = None,
    *,
    fal_key: str | None = None,
    clear_key: bool = False,
    minimax_key: str | None = None,
    clear_minimax_key: bool = False,
    default_execution_backend: str | None = None,
    default_upscale_backend: str | None = None,
    custom_fal_app: str | None = None,
    fal_timeout_s: float | None = None,
    max_cloud_concurrency: int | None = None,
) -> Path:
    updates: dict[str, Any] = {}
    removes: list[str] = []
    _apply_secret_update(updates, removes, fal_key, clear_key, "fal_key_dpapi", "fal_key")
    _apply_secret_update(updates, removes, minimax_key, clear_minimax_key, "minimax_key_dpapi", "minimax_key")
    if default_execution_backend is not None:
        deb = str(default_execution_backend).strip().lower()
        if deb not in _VALID_BACKENDS:
            raise ValueError(f"Invalid default_execution_backend '{default_execution_backend}'")
        updates["default_execution_backend"] = deb
    if default_upscale_backend is not None:
        dub = str(default_upscale_backend).strip().lower()
        if dub not in ("local", "fal"):
            raise ValueError(f"Invalid default_upscale_backend '{default_upscale_backend}'")
        updates["default_upscale_backend"] = dub
    if custom_fal_app is not None:
        capp = custom_fal_app.strip()
        if capp:
            import re
            if ".." in capp or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+", capp):
                raise ValueError(f"Invalid custom_fal_app endpoint '{capp}'")
            updates["custom_fal_app"] = capp
        else:
            removes.append("custom_fal_app")
    if fal_timeout_s is not None:
        updates["fal_timeout_s"] = max(30.0, float(fal_timeout_s))
    if max_cloud_concurrency is not None:
        updates["max_cloud_concurrency"] = max(1, min(8, int(max_cloud_concurrency)))
    return write_local_config(updates, root_dir=root_dir, remove_keys=tuple(removes))


def _key_hint(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 4 else ("set" if key else "")


def get_fal_status(root_dir: Path | None = None) -> dict[str, Any]:
    key, source = resolve_fal_key(root_dir)
    mkey, msource = resolve_minimax_key(root_dir)
    cfg = read_local_config(root_dir)
    return {
        "configured": bool(key),
        "source": source,
        "hint": _key_hint(key),
        "dpapi_supported": dpapi_is_supported(),
        "default_execution_backend": cfg.get("default_execution_backend", "local"),
        "default_upscale_backend": cfg.get("default_upscale_backend", "local"),
        "custom_fal_app": cfg.get("custom_fal_app", ""),
        "fal_timeout_s": float(cfg.get("fal_timeout_s", 600.0)),
        "max_cloud_concurrency": int(cfg.get("max_cloud_concurrency", 1)),
        "minimax": {
            "configured": bool(mkey),
            "source": msource,
            "hint": _key_hint(mkey),
            "dpapi_supported": dpapi_is_supported(),
        },
    }


# ---------------------------------------------------------------------------
# Backend Precedence & Endpoint Routing
# ---------------------------------------------------------------------------
def _norm_backend(val: Any) -> str:
    b = str(val or "").strip().lower()
    if b in ("fal", "fal.ai", "cloud"):
        return "fal"
    if b in ("minimax", "minimax_api", "hailuo", "platform.minimax.io"):
        return "minimax"
    if b in ("local", "wgp", "wan2gp"):
        return "local"
    return ""


def get_fal_profiles_for_kind(kind: str = "video", profiles_raw: dict | None = None) -> dict[str, dict[str, Any]]:
    base = FAL_ANCHOR_PROFILES if kind == "anchor" else FAL_VIDEO_PROFILES
    merged = {k: dict(v) for k, v in base.items()}
    key = "fal_anchor_renderers" if kind == "anchor" else "fal_video_renderers"
    if isinstance(profiles_raw, dict) and isinstance(profiles_raw.get(key), dict):
        for k, v in profiles_raw[key].items():
            if isinstance(v, dict):
                merged[str(k)] = dict(v)
    return merged


def get_minimax_profiles(profiles_raw: dict | None = None) -> dict[str, dict[str, Any]]:
    merged = {k: dict(v) for k, v in MINIMAX_VIDEO_PROFILES.items()}
    if isinstance(profiles_raw, dict) and isinstance(profiles_raw.get("minimax_video_renderers"), dict):
        for k, v in profiles_raw["minimax_video_renderers"].items():
            if isinstance(v, dict):
                merged[str(k)] = dict(v)
    return merged


def resolve_task_backend(
    ctx,
    sc: dict | None = None,
    rspec: dict | None = None,
    pass_num: int | None = None,
    stage: str = "video",
) -> str:
    """Resolves 'local' vs 'fal' vs 'minimax' across CLI override -> dedicated renderer -> scene -> project -> machine default."""
    cli_b = _norm_backend(getattr(ctx, "cli_backend", ""))
    if cli_b:
        return "local" if (stage == "anchor" and cli_b == "minimax") else cli_b

    if rspec and rspec.get("local_only"):
        return "local"

    if stage == "video" and isinstance(sc, dict):
        pass_b = _norm_backend(sc.get("backend_m")) if pass_num == 1 else (_norm_backend(sc.get("backend_f")) if pass_num == 2 else "")
        if pass_b:
            return pass_b

    spec_b = _norm_backend(rspec.get("backend")) if isinstance(rspec, dict) else ""
    if spec_b in ("fal", "minimax"):
        return "local" if (stage == "anchor" and spec_b == "minimax") else spec_b

    chosen = _norm_backend(sc.get("backend")) if isinstance(sc, dict) else ""

    raw_models = getattr(ctx, "raw", {}).get("models", {}) if hasattr(ctx, "raw") else {}
    if not chosen and stage == "anchor":
        chosen = _norm_backend(raw_models.get("default_anchor_backend"))
    if not chosen:
        chosen = _norm_backend(raw_models.get("default_backend"))
    if not chosen and spec_b:
        chosen = spec_b
    if not chosen:
        cfg = read_local_config(getattr(ctx, "project_dir", None))
        chosen = _norm_backend(cfg.get("default_execution_backend")) or "local"

    # MiniMax online API covers Stage 2 video only; anchor stage falls back safely unless anchor backend was overridden
    if stage == "anchor" and chosen == "minimax":
        ab = _norm_backend(raw_models.get("default_anchor_backend"))
        return ab if ab in ("local", "fal") else "local"
    return chosen


def is_ref2va_spec(rspec: dict | None, spec_or_sc: dict | None = None) -> bool:
    """Unified check for Ref2VA/Singularity video profiles across context, hashes, queue, and routing."""
    r = rspec or {}
    mtype = str(r.get("model_type") or "").lower()
    if str(r.get("frames_formula") or "").lower() == "h3_ref2va" or r.get("supports_image_refs"):
        return True
    if any(k in mtype for k in ("ref2va", "fasth3", "singularity")):
        return True
    return bool(isinstance(spec_or_sc, dict) and spec_or_sc.get("image_refs"))


def resolve_upscale_backend(ctx, method: str = "", ai_refiner: str = "", explicit_backend: str = "") -> str:
    tok = (method or ai_refiner or "").strip().lower().split("*", 1)[0]
    if tok == "dlss5":
        return "local"
    exp = _norm_backend(explicit_backend) or _norm_backend(getattr(ctx, "cli_upscale_backend", "")) or _norm_backend(getattr(ctx, "cli_backend", ""))
    if exp:
        return "fal" if exp == "minimax" else exp
    raw_models = getattr(ctx, "raw", {}).get("models", {}) if hasattr(ctx, "raw") else {}
    pb = _norm_backend(raw_models.get("default_upscale_backend")) or _norm_backend(raw_models.get("default_backend"))
    if pb:
        return "fal" if pb == "minimax" else pb
    cfg = read_local_config(getattr(ctx, "project_dir", None))
    cb = _norm_backend(cfg.get("default_upscale_backend")) or _norm_backend(cfg.get("default_execution_backend")) or "local"
    return "fal" if cb == "minimax" else cb


def resolve_fal_route(spec_or_task: dict | None, sc: dict | None = None, pass_num: int | None = None, stage: str = "video", custom_fal_app: str = "") -> dict[str, Any]:
    """Maps a local or fal renderer spec/task to its concrete fal.ai endpoint, adapter, limits, and compatibility notes."""
    spec = spec_or_task or {}
    mtype = str(spec.get("model_type") or "").lower()
    explicit_ep = str(spec.get("fal_endpoint") or "").strip()
    explicit_ad = str(spec.get("fal_adapter") or "").strip()
    notes: list[str] = []

    if stage == "anchor":
        if explicit_ep:
            return {"endpoint": explicit_ep, "adapter": explicit_ad or "flux_image", "max_duration_s": 0.0, "price_per_unit_usd": float(spec.get("price_per_unit_usd", 0.02)), "notes": notes}
        if "qwen_image_edit" in mtype:
            return {**FAL_ANCHOR_PROFILES["fal_qwen_edit_2511"], "endpoint": FAL_ANCHOR_PROFILES["fal_qwen_edit_2511"]["fal_endpoint"], "adapter": "qwen_image_edit", "notes": notes}
        if "qwen_image" in mtype:
            return {**FAL_ANCHOR_PROFILES["fal_qwen_image_21"], "endpoint": FAL_ANCHOR_PROFILES["fal_qwen_image_21"]["fal_endpoint"], "adapter": "qwen_image", "notes": notes}
        if "flux3" in mtype:
            return {**FAL_ANCHOR_PROFILES["fal_flux3_klein"], "endpoint": FAL_ANCHOR_PROFILES["fal_flux3_klein"]["fal_endpoint"], "adapter": "flux_image", "notes": notes}
        if "fibo" in mtype:
            return {**FAL_ANCHOR_PROFILES["fal_fibo2"], "endpoint": FAL_ANCHOR_PROFILES["fal_fibo2"]["fal_endpoint"], "adapter": "flux_image", "notes": notes}
        if "sensenova" in mtype:
            return {**FAL_ANCHOR_PROFILES["fal_sensenova_u1"], "endpoint": FAL_ANCHOR_PROFILES["fal_sensenova_u1"]["fal_endpoint"], "adapter": "flux_image", "notes": notes}
        if "klein_4b" in mtype or "klein_base_4b" in mtype:
            return {**FAL_ANCHOR_PROFILES["fal_flux2_klein_4b"], "endpoint": FAL_ANCHOR_PROFILES["fal_flux2_klein_4b"]["fal_endpoint"], "adapter": "flux_image", "notes": notes}
        return {**FAL_ANCHOR_PROFILES["fal_flux2_klein_9b"], "endpoint": FAL_ANCHOR_PROFILES["fal_flux2_klein_9b"]["fal_endpoint"], "adapter": "flux_image", "notes": notes}

    # Stage 2 Video
    mode = str((sc or {}).get("mode") or spec.get("mode") or "solo").lower()
    is_two_pass = mode in ("duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right")
    if (pass_num == 2 or spec.get("video_guide")) and is_two_pass:
        if custom_fal_app:
            return {"endpoint": custom_fal_app, "adapter": "custom_wgp_app", "max_duration_s": 30.0, "price_per_sec_usd": {"480p": 0.04, "768p": 0.06, "1080p": 0.08, "2k": 0.10}, "notes": notes}
        notes.append("Two-Pass Duet Pass 2 uses cloud ROI Lip-Sync composite on right split.")
        return {"endpoint": "fal-ai/sync-lipsync/v2", "adapter": "duet_pass2_roi_lipsync", "max_duration_s": 15.0, "price_per_sec_usd": {"480p": 0.05, "768p": 0.05, "1080p": 0.05, "2k": 0.05}, "notes": notes}

    if mode in ("duet_method_a", "duet_a"):
        notes.append("Stereo duet rendered via base H3 + multi-speaker sync.")

    has_end = bool((sc or {}).get("anchor_end") or spec.get("image_end"))
    has_audio = bool(spec.get("audio_guide") or ((sc or {}).get("stem", "male") not in ("silent", "none") and mode not in ("silent", "silent_locked", "ambient")))
    is_ref2va = bool(spec.get("supports_image_refs") or "ref2va" in mtype or "fasth3" in mtype or "singularity" in mtype or spec.get("image_refs"))

    if explicit_ep:
        if explicit_ad == "minimax_h3_fl2va" and has_audio and not has_end and spec.get("fal_lipsync_endpoint"):
            return {"endpoint": spec["fal_lipsync_endpoint"], "adapter": "minimax_h3_lipsync", "max_duration_s": float(spec.get("max_duration_s", 15.0)), "price_per_sec_usd": spec.get("price_per_sec_usd", {}), "notes": notes}
        return {"endpoint": explicit_ep, "adapter": explicit_ad or "minimax_h3_lipsync", "max_duration_s": float(spec.get("max_duration_s", 15.0)), "price_per_sec_usd": spec.get("price_per_sec_usd", {}), "notes": notes}

    if "ltx2" in mtype:
        key = "fal_ltx25_a2v" if "25" in mtype else "fal_ltx23_a2v"
        p = FAL_VIDEO_PROFILES[key]
        return {**p, "endpoint": p["fal_endpoint"], "adapter": p["fal_adapter"], "notes": notes}

    for prof in FAL_VIDEO_PROFILES.values():
        if prof.get("model_type", "").lower() == mtype and prof.get("fal_endpoint"):
            return {**prof, "endpoint": prof["fal_endpoint"], "adapter": prof.get("fal_adapter", "kling_i2v"), "notes": notes}

    if is_ref2va:
        key = "fal_minimax_h3_max_ref2v" if "ptd" in mtype and "pruned" not in mtype else "fal_minimax_h3_ref2v"
        p = FAL_VIDEO_PROFILES[key]
        return {**p, "endpoint": p["fal_endpoint"], "adapter": p["fal_adapter"], "notes": notes}

    if has_end:
        p = FAL_VIDEO_PROFILES["fal_minimax_h3_i2v"]
        return {**p, "endpoint": p["fal_endpoint"], "adapter": "minimax_h3_fl2va", "notes": notes}

    if has_audio:
        p = FAL_VIDEO_PROFILES["fal_minimax_h3_lipsync"]
        return {**p, "endpoint": p["fal_endpoint"], "adapter": "minimax_h3_lipsync", "notes": notes}

    p = FAL_VIDEO_PROFILES["fal_minimax_h3_i2v"]
    return {**p, "endpoint": p["fal_endpoint"], "adapter": "minimax_h3_fl2va", "notes": notes}


def _res_tier(res_str: str) -> str:
    txt = str(res_str or "768p").lower()
    if "1088" in txt or "1080" in txt or "1920" in txt:
        return "1080p"
    if "480" in txt or "832" in txt:
        return "480p"
    if "2k" in txt or "2560" in txt or "1440" in txt:
        return "2k"
    return "768p"


def resolve_minimax_route(
    spec_or_task: dict | None,
    sc: dict | None = None,
    pass_num: int | None = None,
    resolution: str = "1344x768",
) -> dict[str, Any]:
    """Maps a scene/renderer spec to a direct platform.minimax.io /v2/video_generation configuration."""
    spec = spec_or_task or {}
    mm_model = str(spec.get("minimax_model") or "").strip() or "MiniMax-H3"
    tier = _res_tier(resolution)
    mode = str((sc or {}).get("mode") or spec.get("mode") or "solo").lower()
    is_two_pass = mode in ("duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right")
    notes: list[str] = []

    if mm_model == "MiniMax-H3" and tier == "480p":
        mm_model = "MiniMax-H3-Highspeed"
        target_res = "480P"
        notes.append("Using MiniMax-H3-Highspeed for 480P.")
    elif mm_model in ("MiniMax-H3-Highspeed", "MiniMax-H3-Lite"):
        target_res = "480P" if tier == "480p" else "768P"
        if tier in ("1080p", "2k"):
            notes.append(f"{mm_model} caps natively at 768P (scaled in Stage 3).")
    else:
        target_res = "2K" if tier in ("1080p", "2k") else "768P"

    prof_key = {
        "MiniMax-H3": "minimax_api_h3",
        "MiniMax-H3-Highspeed": "minimax_api_h3_highspeed",
        "MiniMax-H3-Lite": "minimax_api_h3_lite",
    }.get(mm_model, "minimax_api_h3")
    prof = MINIMAX_VIDEO_PROFILES[prof_key]

    adapter = "minimax_v2_video"
    if (pass_num == 2 or spec.get("video_guide")) and is_two_pass:
        adapter = "duet_pass2_roi_minimax"
        notes.append("Two-Pass Duet Pass 2 uses MiniMax-H3 ROI crop + audio sync.")

    return {
        **prof,
        "minimax_model": mm_model,
        "endpoint": f"api.minimax.io/v2/{mm_model}",
        "target_resolution": target_res,
        "adapter": adapter,
        "notes": notes,
    }


def _build_cost_estimate(route: dict, dur_sec: float, resolution: str, fallback_rate: float = 0.08) -> dict[str, Any]:
    tier = _res_tier(resolution)
    rates = route.get("price_per_sec_usd") or {}
    rate = float(rates.get(tier, rates.get("768p", fallback_rate))) if isinstance(rates, dict) else float(rates or fallback_rate)
    d = max(1.0, float(dur_sec or 5.0))
    max_d = float(route.get("max_duration_s", 15.0) or 15.0)
    chunks = max(1, int((d + max_d - 0.01) // max_d)) if max_d > 0 and d > max_d else 1
    cost = round(d * rate, 2)
    return {
        "endpoint": route["endpoint"],
        "adapter": route["adapter"],
        "rate_per_sec_usd": rate,
        "estimated_cost_usd": cost,
        "cost_str": f"~${cost:.2f}",
        "auto_chunks": chunks,
        "notes": list(route.get("notes") or []),
    }


def estimate_fal_cost_usd(
    dur_sec: float,
    resolution: str = "1344x768",
    rspec: dict | None = None,
    sc: dict | None = None,
    pass_num: int | None = None,
) -> dict[str, Any]:
    route = resolve_fal_route(rspec, sc=sc, pass_num=pass_num, stage="video")
    return _build_cost_estimate(route, dur_sec, resolution, fallback_rate=0.08)


def estimate_minimax_cost_usd(
    dur_sec: float,
    resolution: str = "1344x768",
    rspec: dict | None = None,
    sc: dict | None = None,
    pass_num: int | None = None,
) -> dict[str, Any]:
    route = resolve_minimax_route(rspec, sc=sc, pass_num=pass_num, resolution=resolution)
    return _build_cost_estimate(route, dur_sec, resolution, fallback_rate=0.08)


def init_context_fal(ctx, profiles_raw: dict, raw_models: dict):
    """Attaches cloud renderer catalogs and runtime override slots to ProjectContext."""
    ctx.fal_anchor_renderers = get_fal_profiles_for_kind("anchor", profiles_raw)
    if isinstance(raw_models.get("fal_anchor_renderers"), dict):
        ctx.fal_anchor_renderers.update({str(k): dict(v) for k, v in raw_models["fal_anchor_renderers"].items() if isinstance(v, dict)})
    ctx.fal_video_renderers = get_fal_profiles_for_kind("video", profiles_raw)
    if isinstance(raw_models.get("fal_video_renderers"), dict):
        ctx.fal_video_renderers.update({str(k): dict(v) for k, v in raw_models["fal_video_renderers"].items() if isinstance(v, dict)})
    ctx.minimax_video_renderers = get_minimax_profiles(profiles_raw)
    if isinstance(raw_models.get("minimax_video_renderers"), dict):
        ctx.minimax_video_renderers.update({str(k): dict(v) for k, v in raw_models["minimax_video_renderers"].items() if isinstance(v, dict)})
    ctx.cli_backend = ""
    ctx.cli_upscale_backend = ""


def attach_fal_task_meta(
    ctx,
    task: dict,
    sc: dict | None = None,
    rspec: dict | None = None,
    pass_num: int | None = None,
    stage: str = "video",
) -> str:
    """Attaches backend routing metadata to a task without altering local-only task hashes or settings."""
    backend = resolve_task_backend(ctx, sc=sc, rspec=rspec, pass_num=pass_num, stage=stage)
    task["_backend"] = backend
    ctx_meta = (
        {
            "id": sc.get("id"),
            "mode": sc.get("mode"),
            "split_x": sc.get("split_x", 0.5),
            "stem": sc.get("stem"),
            "pass2_roi_mode": sc.get("pass2_roi_mode", ""),
            "pass2_roi_side": sc.get("pass2_roi_side", "right"),
            "pass2_roi_box": sc.get("pass2_roi_box"),
        }
        if isinstance(sc, dict) else {}
    )
    if backend == "fal":
        cfg_local = read_local_config(getattr(ctx, "project_dir", None))
        route = resolve_fal_route(rspec or task, sc=sc, pass_num=pass_num, stage=stage, custom_fal_app=cfg_local.get("custom_fal_app", ""))
        task["_fal_spec"] = route
        task["_fal_ctx"] = ctx_meta
        task["backend"] = "fal"
        task["fal_endpoint"] = route["endpoint"]
    elif backend == "minimax":
        route = resolve_minimax_route(rspec or task, sc=sc, pass_num=pass_num, resolution=str(task.get("resolution") or "1344x768"))
        task["_fal_spec"] = route
        task["_fal_ctx"] = ctx_meta
        task["backend"] = "minimax"
        task["fal_endpoint"] = route["endpoint"]
        task["minimax_model"] = route["minimax_model"]
    return backend
