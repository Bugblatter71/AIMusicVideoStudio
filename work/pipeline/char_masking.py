"""Studio Character Reference Masking, Cutout Storage & AI De-Occlusion Manager.

Runs purely on Python standard library inside the Studio server process, delegating
heavy segmentation / edge matting to `work/pipeline/char_mask_worker.py` executed
via `server.resolve_engine_python()` (Wan2GP's venv).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

from .char_mask_worker import find_wgp_ckpt_status
from .media_utils import WORKSPACE_ROOT, auto_detect_wgp_root, resolve_path, resolve_wgp_root_with_fallback

_GPU_ONLY_METHODS = frozenset({"sam3", "birefnet", "birefnet-general", "bria_rmbg", "bria-rmbg"})
_VALID_METHODS = frozenset({"yolo_person", "u2net", "u2net_msr_preview", "birefnet", "birefnet-general", "bria_rmbg", "bria-rmbg", "sam3"})


def _get_srv():
    for name in ("work.ui.server", "ui.server", "server"):
        if name in sys.modules:
            return sys.modules[name]
    try:
        from work.ui import server
        return server
    except ImportError:
        import server
        return server


def resolve_proj_rel_safe(proj_dir: Path, raw_rel: str | None) -> Path | None:
    """Resolves a project-relative path strictly inside `proj_dir`, rejecting traversal."""
    s = str(raw_rel or "").strip().replace("\\", "/").lstrip("/")
    if not s or "\x00" in s:
        return None
    parts = [p for p in s.split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None
    base = Path(proj_dir).resolve()
    cand = (base / "/".join(parts)).resolve()
    try:
        cand.relative_to(base)
    except ValueError:
        return None
    return cand


def _resolve_wgp_root(srv=None) -> Path | None:
    cfg_wgp = None
    try:
        if srv is not None and callable(getattr(srv, "find_local_config", None)):
            cfg = srv.find_local_config(WORKSPACE_ROOT) or {}
            cfg_wgp = cfg.get("wgp_root") or (cfg.get("environment") or {}).get("wgp_root")
    except Exception:
        pass
    return resolve_wgp_root_with_fallback(cfg_wgp, auto_detect_fn=auto_detect_wgp_root)


def _resolve_worker_python(srv=None) -> str:
    try:
        if srv is not None and callable(getattr(srv, "resolve_engine_python", None)):
            py = srv.resolve_engine_python()
            if py and Path(py).is_file():
                return str(py)
    except Exception:
        pass
    return sys.executable


def _is_local_gpu_busy(srv, slug: str) -> bool:
    if srv is None:
        return False
    try:
        if srv.render_status.get("running"):
            return True
    except Exception:
        pass
    try:
        if callable(getattr(srv, "active_rendering_map", None)):
            is_act, amap = srv.active_rendering_map(srv, slug)
            if is_act or (isinstance(amap, dict) and any("local" in str(k).lower() for k in amap)):
                return True
    except Exception:
        pass
    return False


def _run_mask_worker(srv, payload: dict[str, Any], *, force_cpu: bool = False) -> dict[str, Any]:
    """Executes `char_mask_worker.py` in Wan2GP's Python venv and parses JSON output."""
    py_exe = _resolve_worker_python(srv)
    worker_script = Path(__file__).resolve().with_name("char_mask_worker.py")
    env = os.environ.copy()
    if force_cpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    proc = subprocess.run(
        [py_exe, str(worker_script), "--request", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=240,
    )
    out_lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    parsed: dict[str, Any] = {}
    for ln in reversed(out_lines):
        if ln.startswith("{") and ln.endswith("}"):
            try:
                parsed = json.loads(ln)
                break
            except Exception:
                pass
    if proc.returncode != 0 or not parsed.get("success"):
        err = parsed.get("error") or (proc.stderr or "").strip() or f"Mask worker exited with code {proc.returncode}"
        raise RuntimeError(err)
    return parsed


def _mask_artifact_rels(entity_id: str, source_rel: str) -> dict[str, str]:
    clean_ent = re.sub(r"[^a-zA-Z0-9_-]", "_", entity_id).strip("_") or "character"
    src_norm = str(source_rel).replace("\\", "/").lstrip("/")
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(src_norm).stem).strip("_") or "ref"
    masks_rel = f"references/{clean_ent}/masks"
    return {
        "mask_rel": f"{masks_rel}/{stem}_mask.png",
        "rgba_rel": f"{masks_rel}/{stem}_rgba.png",
        "cutout_rel": f"{masks_rel}/{stem}_cutout.png",
        "msr_preview_mask_rel": f"{masks_rel}/{stem}_msr_preview_mask.png",
        "msr_preview_cutout_rel": f"{masks_rel}/{stem}_msr_preview_cutout.png",
    }


def resolve_ref_mask_state(proj_dir: Path, look: dict, rel_path: str) -> dict[str, Any]:
    """Resolves original source vs cutout/mask paths cleanly even when passed an active cutout."""
    norm_rel = str(rel_path or "").replace("\\", "/").lstrip("/")
    ref_masks = look.get("ref_masks") if isinstance(look.get("ref_masks"), dict) else {}
    meta = ref_masks.get(norm_rel)
    if not isinstance(meta, dict):
        for v in ref_masks.values():
            if isinstance(v, dict) and v.get("source_rel") == norm_rel:
                meta = v
                break
    source_rel = str((meta or {}).get("source_rel") or norm_rel).replace("\\", "/").lstrip("/")
    mask_rel = str((meta or {}).get("mask_rel") or "").replace("\\", "/").lstrip("/")
    rgba_rel = str((meta or {}).get("rgba_rel") or "").replace("\\", "/").lstrip("/")
    cutout_rel = str((meta or {}).get("cutout_rel") or "").replace("\\", "/").lstrip("/")

    def _exists(rp: str) -> bool:
        p = resolve_proj_rel_safe(proj_dir, rp)
        return bool(p and p.is_file())

    return {
        "rel_path": norm_rel,
        "source_rel": source_rel if _exists(source_rel) else norm_rel,
        "mask_rel": mask_rel if (mask_rel and _exists(mask_rel)) else "",
        "rgba_rel": rgba_rel if (rgba_rel and _exists(rgba_rel)) else "",
        "cutout_rel": cutout_rel if (cutout_rel and _exists(cutout_rel)) else "",
        "is_cutout": bool(meta and cutout_rel and norm_rel == cutout_rel),
        "has_mask": bool(mask_rel and _exists(mask_rel)),
        "meta": meta or {},
    }


def _decode_png_bytes(data_url: str) -> bytes:
    if not data_url:
        raise ValueError("mask_data_url is required")
    raw_b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
    raw = base64.b64decode(raw_b64, validate=False)
    if not raw or len(raw) > 25 * 1024 * 1024 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Expected valid PNG data URL under 25 MB")
    return raw


def _apply_cutout_to_look(
    proj_dir: Path,
    cfg_file: Path,
    data: dict,
    eid: str,
    lid: str,
    active_rel: str,
    meta: dict[str, Any],
    *,
    keep_both: bool = False,
    apply_cutout: bool = True,
) -> dict[str, Any]:
    from .anchor_takes import _backup_and_save_project_json

    chars = data.setdefault("characters", {})
    ent = chars[eid]
    look = ent["looks"][lid]
    ref_masks = look.setdefault("ref_masks", {})
    cutout_rel = meta["cutout_rel"]
    source_rel = meta["source_rel"]
    ref_masks[cutout_rel] = meta

    if apply_cutout:
        img_refs = look.setdefault("image_refs", [])
        if keep_both:
            if source_rel not in img_refs:
                img_refs.append(source_rel)
            if cutout_rel not in img_refs:
                img_refs.append(cutout_rel)
        else:
            replaced = False
            for idx, cur in enumerate(img_refs):
                if cur in (active_rel, source_rel, cutout_rel):
                    if not replaced:
                        img_refs[idx] = cutout_rel
                        replaced = True
            # Remove duplicate cutout/source entries if swapping in-place
            seen: list[str] = []
            for cur in img_refs:
                if cur not in seen:
                    seen.append(cur)
            look["image_refs"] = seen
            if cutout_rel not in look["image_refs"]:
                look["image_refs"].append(cutout_rel)

        # Keep raw source reachable in generated_takes so it is never lost from Takes strip
        gen_takes = look.setdefault("generated_takes", [])
        if source_rel and not any(isinstance(t, dict) and t.get("rel_path") == source_rel for t in gen_takes):
            gen_takes.append({
                "take_id": f"src_{int(time.time())}",
                "rel_path": source_rel,
                "filename": Path(source_rel).name,
                "seed": int(look.get("gen_seed") or 1001),
                "prompt": "Original unmasked reference source",
                "is_raw_source": True,
                "created_at": round(time.time(), 3),
            })

        if ent.get("avatar") in (active_rel, source_rel, ""):
            ent["avatar"] = cutout_rel

    _backup_and_save_project_json(proj_dir, cfg_file, data)
    return chars


def build_char_isolate_prompt(ent: dict, look: dict, custom_prompt: str = "") -> str:
    """Builds clean Qwen Image Edit 2.1 / 2511 isolation prompt preserving identity & wardrobe."""
    cp = str(custom_prompt or "").strip()
    if cp:
        return cp if "<image1>" in cp or "image 1" in cp.lower() else f"From <image1>, {cp}"
    traits = str(ent.get("physical_traits") or ent.get("name") or "the main subject").strip()
    ldesc = str(look.get("description") or look.get("name") or "").strip()
    desc_part = f" ({traits}{f', {ldesc}' if ldesc else ''})" if traits else ""
    return (
        f"Extract and isolate only the main subject{desc_part} from <image1>, preserving exact face, hair, expression, "
        "and clothing details. Place the subject cleanly centered alone against a solid pure white studio background with soft even lighting. "
        "Remove all other people, background crowds, overlapping hands, and clutter; cleanly reconstruct any occluded sleeves or outfit edges."
    )


def resolve_char_isolate_override(ctx, ent: dict, look: dict, entity_id: str, look_id: str) -> dict[str, Any]:
    """Resolves `look['isolate_spec']` into sp_like overrides for `CHAR:<eid>:<lid>:isolate`."""
    iso = look.get("isolate_spec") if isinstance(look.get("isolate_spec"), dict) else {}
    raw_rel = str(iso.get("source_rel") or "").strip().replace("\\", "/").lstrip("/")
    if not raw_rel:
        refs = look.get("image_refs") or []
        raw_rel = str(refs[0] if refs else "").strip().replace("\\", "/").lstrip("/")
    src_path = resolve_proj_rel_safe(ctx.project_dir, raw_rel)
    if src_path is None or not src_path.is_file():
        src_path = resolve_path(ctx.project_dir, raw_rel) if raw_rel else None
    if src_path is None or not src_path.is_file():
        raise FileNotFoundError(f"Source reference image for AI de-occlusion not found: {raw_rel or '(none)'}")

    prompt = build_char_isolate_prompt(ent, look, str(iso.get("prompt") or ""))
    renderer = iso.get("renderer")
    if renderer in (None, ""):
        renderer = 1  # Qwen Image 2.1 Turbo 7B (on disk)
    backend = str(iso.get("backend") or "").strip() or None
    resolution = str(iso.get("resolution") or "1024x1024").strip()
    seed = int(iso.get("seed") or look.get("gen_seed") or 1001)
    return {
        "source_rel": raw_rel,
        "source_path": str(src_path),
        "prompt": prompt,
        "renderer": renderer,
        "backend": backend,
        "resolution": resolution,
        "seed": seed,
        "video_prompt_type": "KI",
        "image_refs": [str(src_path)],
        "negative_prompt": str(iso.get("negative_prompt") or "crowds, extra people, extra hands, busy background, text, watermark"),
    }


def build_isolate_sp_like(ctx, ent: dict, look: dict, entity_id: str, look_id: str, explicit_seed: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Constructs the `sp_like` task spec and isolate metadata dict for `CHAR:<eid>:<lid>:isolate`."""
    iso = resolve_char_isolate_override(ctx, ent, look, entity_id, look_id)
    seed_val = iso["seed"] if explicit_seed is None else int(explicit_seed)
    sp_like = {
        "filename": f"CHAR_{entity_id}_{look_id}.jpg",
        "prompt": iso["prompt"],
        "negative_prompt": iso["negative_prompt"],
        "renderer": iso["renderer"],
        "backend": iso["backend"],
        "resolution": iso["resolution"],
        "video_prompt_type": iso["video_prompt_type"],
        "seed": seed_val,
        "image_refs": iso["image_refs"],
    }
    return sp_like, iso


def _load_char_context(proj_dir: Path, eid: str, lid: str, allow_unsaved: bool = False):
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        raise FileNotFoundError("Project JSON not found")
    cfg_file = jsons[-1]
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    chars = data.get("characters") or {}
    ent = chars.get(eid)
    look = (ent.get("looks") or {}).get(lid) if isinstance(ent, dict) else None
    if not isinstance(ent, dict) or not isinstance(look, dict):
        if allow_unsaved:
            return cfg_file, data, (ent if isinstance(ent, dict) else {}), (look if isinstance(look, dict) else {})
        raise KeyError("Character or look not found")
    return cfg_file, data, ent, look


def handle_char_masking_api(handler, action: str, proj_dir: Path, slug: str, body: dict) -> None:
    """Handles mask-info, mask-detect, mask-extract, mask-save, mask-toggle, and isolate-ai."""
    srv = _get_srv()
    eid = str(body.get("entity_id") or "").strip()
    lid = str(body.get("look_id") or "").strip()
    rel_path = str(body.get("rel_path") or body.get("source_rel") or "").strip()
    if not eid or not lid:
        handler.send_error_json("entity_id and look_id are required", status=HTTPStatus.BAD_REQUEST)
        return

    read_only_act = action in ("mask-info", "mask-detect") or (action == "mask-extract" and not bool(body.get("apply_immediately", False)))
    try:
        cfg_file, data, ent, look = _load_char_context(proj_dir, eid, lid, allow_unsaved=read_only_act)
    except FileNotFoundError as exc:
        handler.send_error_json(str(exc), status=HTTPStatus.NOT_FOUND)
        return
    except KeyError as exc:
        handler.send_error_json(str(exc), status=HTTPStatus.NOT_FOUND)
        return

    wgp_root = _resolve_wgp_root(srv)
    state_info = resolve_ref_mask_state(proj_dir, look, rel_path) if rel_path else {}

    if action == "mask-info":
        handler.send_json({
            "success": True,
            "slug": slug,
            "entity_id": eid,
            "look_id": lid,
            **state_info,
            "models": find_wgp_ckpt_status(wgp_root),
            "gpu_busy": _is_local_gpu_busy(srv, slug),
        })
        return

    src_rel = str(state_info.get("source_rel") or rel_path).strip()
    src_file = resolve_proj_rel_safe(proj_dir, src_rel)
    if src_file is None or not src_file.is_file():
        handler.send_error_json(f"Reference image not found inside project: {src_rel}", status=HTTPStatus.NOT_FOUND)
        return

    arts = _mask_artifact_rels(eid, src_rel)

    if action == "mask-detect":
        gpu_busy = _is_local_gpu_busy(srv, slug)
        try:
            res = _run_mask_worker(srv, {
                "action": "detect_instances",
                "source_path": str(src_file),
                "wgp_root": str(wgp_root) if wgp_root else "",
                "use_gpu": not gpu_busy,
            }, force_cpu=gpu_busy)
            handler.send_json({"success": True, **res, "source_rel": src_rel})
        except Exception as exc:
            handler.send_error_json(f"Instance detection failed: {exc}")
        return

    if action == "mask-extract":
        method = str(body.get("method") or "yolo_person").strip().lower()
        if method not in _VALID_METHODS:
            handler.send_error_json(f"Unsupported extraction method '{method}'", status=HTTPStatus.BAD_REQUEST)
            return
        gpu_busy = _is_local_gpu_busy(srv, slug)
        if gpu_busy and method in _GPU_ONLY_METHODS:
            handler.send_error_json(
                f"Local GPU is busy rendering. Use 'Auto-Split People (YOLOv8m-seg)' or manual brush now, or wait until the GPU render finishes.",
                status=HTTPStatus.CONFLICT,
            )
            return
        is_preview = method == "u2net_msr_preview" or bool(body.get("preview_only", False))
        m_rel = arts["msr_preview_mask_rel"] if is_preview else arts["mask_rel"]
        c_rel = arts["msr_preview_cutout_rel"] if is_preview else arts["cutout_rel"]
        r_rel = arts["rgba_rel"]
        try:
            res = _run_mask_worker(srv, {
                "action": "extract_mask",
                "method": method,
                "source_path": str(src_file),
                "wgp_root": str(wgp_root) if wgp_root else "",
                "use_gpu": not gpu_busy,
                "det_idx": int(body.get("det_idx", 0)),
                "crop_box": body.get("crop_box"),
                "auto_box": bool(body.get("auto_box", False)),
                "text_prompt": str(body.get("text_prompt") or ""),
                "points": body.get("points"),
                "point_labels": body.get("point_labels"),
                "feather_px": float(body.get("feather_px") or 0.0),
                "refine_edges": bool(body.get("refine_edges", method in ("yolo_person", "sam3"))),
                "out_mask_path": str((proj_dir / m_rel).resolve()),
                "out_rgba_path": "" if is_preview else str((proj_dir / r_rel).resolve()),
                "out_cutout_path": str((proj_dir / c_rel).resolve()),
            }, force_cpu=gpu_busy)
        except Exception as exc:
            handler.send_error_json(f"Extraction failed ({method}): {exc}")
            return

        chars = data.get("characters")
        if bool(body.get("apply_immediately", False)) and not is_preview:
            meta = {
                "source_rel": src_rel,
                "mask_rel": m_rel,
                "rgba_rel": r_rel,
                "cutout_rel": c_rel,
                "method": method,
                "premasked_white": True,
                "updated_at": round(time.time(), 3),
            }
            chars = _apply_cutout_to_look(
                proj_dir, cfg_file, data, eid, lid, state_info.get("rel_path") or src_rel, meta,
                keep_both=bool(body.get("keep_both", False)), apply_cutout=True,
            )
            try:
                srv.compute_fast_project_queue(slug, force_recheck=True)
            except Exception:
                pass
        handler.send_json({
            "success": True,
            "method": method,
            "preview_only": is_preview,
            "source_rel": src_rel,
            "mask_rel": m_rel,
            "rgba_rel": "" if is_preview else r_rel,
            "cutout_rel": c_rel,
            "characters": chars,
            **res,
        })
        return

    if action == "mask-save":
        data_url = str(body.get("mask_data_url") or "").strip()
        try:
            mask_bytes = _decode_png_bytes(data_url)
        except Exception as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        m_path = (proj_dir / arts["mask_rel"]).resolve()
        m_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_mask = m_path.with_name(f"_tmp_{m_path.name}")
        tmp_mask.write_bytes(mask_bytes)
        gpu_busy = _is_local_gpu_busy(srv, slug)
        try:
            res = _run_mask_worker(srv, {
                "action": "composite_mask",
                "source_path": str(src_file),
                "mask_path": str(tmp_mask),
                "feather_px": float(body.get("feather_px") or 0.0),
                "refine_edges": bool(body.get("refine_edges", False)),
                "out_mask_path": str(m_path),
                "out_rgba_path": str((proj_dir / arts["rgba_rel"]).resolve()),
                "out_cutout_path": str((proj_dir / arts["cutout_rel"]).resolve()),
            }, force_cpu=gpu_busy)
        except Exception as exc:
            handler.send_error_json(f"Failed saving mask: {exc}")
            return
        finally:
            tmp_mask.unlink(missing_ok=True)

        meta = {
            "source_rel": src_rel,
            "mask_rel": arts["mask_rel"],
            "rgba_rel": arts["rgba_rel"],
            "cutout_rel": arts["cutout_rel"],
            "method": str(body.get("method") or "manual_brush"),
            "premasked_white": True,
            "updated_at": round(time.time(), 3),
        }
        chars = _apply_cutout_to_look(
            proj_dir, cfg_file, data, eid, lid, state_info.get("rel_path") or src_rel, meta,
            keep_both=bool(body.get("keep_both", False)), apply_cutout=True,
        )
        try:
            srv.compute_fast_project_queue(slug, force_recheck=True)
        except Exception:
            pass
        handler.send_json({
            "success": True,
            "source_rel": src_rel,
            **{k: arts[k] for k in ("mask_rel", "rgba_rel", "cutout_rel")},
            "characters": chars,
            **res,
        })
        return

    if action == "mask-toggle":
        from .anchor_takes import _backup_and_save_project_json

        if not state_info.get("cutout_rel"):
            handler.send_error_json("No saved cutout exists for this reference image", status=HTTPStatus.NOT_FOUND)
            return
        cur_rel = state_info["rel_path"]
        target_rel = state_info["source_rel"] if state_info["is_cutout"] else state_info["cutout_rel"]
        img_refs = look.setdefault("image_refs", [])
        swapped = False
        for i, r in enumerate(img_refs):
            if r == cur_rel:
                img_refs[i] = target_rel
                swapped = True
                break
        if not swapped:
            img_refs.append(target_rel)
        if ent.get("avatar") == cur_rel:
            ent["avatar"] = target_rel
        _backup_and_save_project_json(proj_dir, cfg_file, data)
        try:
            srv.compute_fast_project_queue(slug, force_recheck=True)
        except Exception:
            pass
        handler.send_json({
            "success": True,
            "active_rel": target_rel,
            "using_cutout": target_rel == state_info["cutout_rel"],
            "characters": data.get("characters"),
        })
        return

    if action == "isolate-ai":
        from .anchor_takes import _backup_and_save_project_json

        iso_prompt = build_char_isolate_prompt(ent, look, str(body.get("prompt") or ""))
        renderer = body.get("renderer")
        if renderer in (None, ""):
            renderer = 1
        backend = str(body.get("backend") or "").strip()
        resolution = str(body.get("resolution") or "1024x1024").strip()
        seed = int(body.get("seed") or look.get("gen_seed") or 1001)
        use_src = str(body.get("source_rel") or src_rel).strip().replace("\\", "/").lstrip("/")
        if resolve_proj_rel_safe(proj_dir, use_src) is None:
            handler.send_error_json("Invalid source_rel", status=HTTPStatus.BAD_REQUEST)
            return
        look["isolate_spec"] = {
            "source_rel": use_src,
            "prompt": iso_prompt,
            "renderer": renderer,
            "backend": backend,
            "resolution": resolution,
            "seed": seed,
            "negative_prompt": str(body.get("negative_prompt") or "crowds, extra people, extra hands, busy background, text, watermark"),
        }
        _backup_and_save_project_json(proj_dir, cfg_file, data)
        handler.send_json({
            "success": True,
            "target_token": f"CHAR:{eid}:{lid}:isolate",
            "isolate_spec": look["isolate_spec"],
            "characters": data.get("characters"),
        })
        return

    handler.send_error_json(f"Unknown masking action: {action}", status=HTTPStatus.NOT_FOUND)
