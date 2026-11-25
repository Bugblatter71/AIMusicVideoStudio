"""Wan2GP-env CLI worker for character segmentation, edge matting, and MSR cutout previews.

Runs inside Wan2GP's Python venv (`resolve_engine_python()`) so Studio UI server can stay
dependency-free. Pure helper functions at module top have no third-party imports so unit
tests in standard Python can import and verify prompt/geometry contracts directly.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
from pathlib import Path
from typing import Any


def box_xyxy_to_xywh_norm(box_xyxy: list[float] | tuple[float, ...], width: int, height: int) -> list[float]:
    """Converts [x0, y0, x1, y1] (pixel or already-normalized 0..1) into normalized [x, y, w, h]."""
    if len(box_xyxy) != 4:
        raise ValueError(f"Expected 4 box coordinates, got {len(box_xyxy)}")
    x0, y0, x1, y1 = [float(v) for v in box_xyxy]
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) > 1.5 and width > 0 and height > 0:
        x0, x1 = x0 / width, x1 / width
        y0, y1 = y0 / height, y1 / height
    cx0 = max(0.0, min(1.0, min(x0, x1)))
    cy0 = max(0.0, min(1.0, min(y0, y1)))
    cx1 = max(0.0, min(1.0, max(x0, x1)))
    cy1 = max(0.0, min(1.0, max(y0, y1)))
    return [round(cx0, 6), round(cy0, 6), round(max(1e-4, cx1 - cx0), 6), round(max(1e-4, cy1 - cy0), 6)]


def build_sam3_prompt_requests(
    *,
    session_id: str = "",
    box_norm_xyxy: list[float] | None = None,
    text_prompt: str = "",
    points: list[list[float]] | None = None,
    point_labels: list[int] | None = None,
    width: int = 1,
    height: int = 1,
    obj_id: int = 1,
) -> dict[str, Any]:
    """Builds Wan2GP SAM3 predictor payloads respecting sam3_video_inference.py assertions.

    SAM3 forbids combining `points` with `text` or `bounding_boxes` in the same `add_prompt`
    call (`assert text_str is None and boxes_xywh is None`). Pass 1 seeds the object via text
    or normalized XYWH bounding box; Pass 2 refines the returned `obj_id` with click points.
    """
    seed_req: dict[str, Any] = {"type": "add_prompt", "session_id": session_id, "frame_index": 0, "rel_coordinates": True}
    clean_text = str(text_prompt or "").strip()
    if box_norm_xyxy and len(box_norm_xyxy) == 4:
        seed_req["bounding_boxes"] = [box_xyxy_to_xywh_norm(box_norm_xyxy, width, height)]
        seed_req["bounding_box_labels"] = [1]
    elif clean_text:
        seed_req["text"] = clean_text
    else:
        seed_req["text"] = "person"

    refine_req: dict[str, Any] | None = None
    pts = [[round(max(0.0, min(1.0, float(x))), 6), round(max(0.0, min(1.0, float(y))), 6)] for x, y in (points or [])]
    lbls = [1 if int(lbl) > 0 else 0 for lbl in (point_labels or [])]
    if pts and len(pts) == len(lbls):
        refine_req = {
            "type": "add_prompt",
            "session_id": session_id,
            "frame_index": 0,
            "obj_id": int(obj_id),
            "points": pts,
            "point_labels": lbls,
            "clear_old_points": True,
            "rel_coordinates": True,
        }
    return {"seed": seed_req, "refine": refine_req}


def find_wgp_ckpt_status(wgp_root: Path | str | None) -> dict[str, bool]:
    """Reports which character extraction models are already downloaded under Wan2GP ckpts/."""
    if not wgp_root:
        return {"yolo_person": False, "u2net": False, "birefnet": False, "bria_rmbg": False, "sam3": False, "qwen21_turbo": False}
    ck = Path(wgp_root) / "ckpts"
    return {
        "yolo_person": (ck / "buffalo_l" / "person_yolov8m-seg.safetensors").is_file(),
        "u2net": (ck / "rembg" / "u2net_bf16.safetensors").is_file(),
        "birefnet": (ck / "rembg" / "birefnet-general_bf16.safetensors").is_file(),
        "bria_rmbg": (ck / "rembg" / "bria-rmbg2_bf16.safetensors").is_file(),
        "sam3": (ck / "sam3" / "sam3.1_multiplex_bf16.safetensors").is_file(),
        "qwen21_turbo": (ck / "qwen_image_21_turbo_7B_int8_convrot.safetensors").is_file()
        or (ck / "qwen_image_21_7B_int8_convrot.safetensors").is_file(),
    }


def subtract_bystander_masks(target_prob, other_masks, suppression: float = 0.85, threshold: float = 0.45):
    """Suppresses overlapping non-target person masks from the selected character probability map."""
    import numpy as np

    tgt = np.asarray(target_prob, dtype=np.float32)
    if not other_masks:
        return np.clip(tgt >= threshold, 0, 1).astype(np.float32) if threshold is not None else tgt
    others = np.zeros_like(tgt, dtype=np.float32)
    for m in other_masks:
        others = np.maximum(others, np.asarray(m, dtype=np.float32))
    cleaned = np.clip(tgt - float(suppression) * others, 0.0, 1.0)
    return cleaned


def _ensure_wgp_sys_path(wgp_root: str | Path) -> Path:
    root = Path(wgp_root).resolve()
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    os.chdir(root_s)
    return root


def _refine_alpha_with_pymatting(img_rgb, mask_u8, erode_px: int = 7, dilate_px: int = 9):
    import cv2
    import numpy as np
    from PIL import Image
    from pymatting import estimate_alpha_cf, estimate_foreground_ml

    h, w = mask_u8.shape[:2]
    max_dim = 1100
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        sw, sh = max(32, int(round(w * scale))), max(32, int(round(h * scale)))
        work_rgb = np.asarray(Image.fromarray(img_rgb).resize((sw, sh), Image.Resampling.LANCZOS))
        work_m = cv2.resize(mask_u8, (sw, sh), interpolation=cv2.INTER_LINEAR)
    else:
        work_rgb, work_m = img_rgb, mask_u8

    m_bin = (work_m >= 128).astype(np.uint8) * 255
    ke = np.ones((max(3, erode_px | 1), max(3, erode_px | 1)), np.uint8)
    kd = np.ones((max(3, dilate_px | 1), max(3, dilate_px | 1)), np.uint8)
    sure_fg = cv2.erode(m_bin, ke)
    possible = cv2.dilate(m_bin, kd)
    if not np.any(sure_fg) or np.all(possible == 255):
        return mask_u8.astype(np.float32) / 255.0

    trimap = np.full(work_m.shape, 128, dtype=np.uint8)
    trimap[possible == 0] = 0
    trimap[sure_fg > 0] = 255
    px = np.asarray(work_rgb, dtype=np.float64) / 255.0
    alpha = np.clip(estimate_alpha_cf(px, trimap / 255.0), 0.0, 1.0)
    fg = np.clip(estimate_foreground_ml(px, alpha), 0.0, 1.0)
    if scale < 1.0:
        alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8), "L").resize((w, h), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
        return alpha, None
    return alpha.astype(np.float32), fg


def save_mask_outputs(
    source_path: str | Path,
    alpha_arr,
    *,
    mask_path: str | Path | None = None,
    rgba_path: str | Path | None = None,
    cutout_path: str | Path | None = None,
    feather_px: float = 0.0,
    refine_edges: bool = False,
) -> dict[str, Any]:
    """Saves L-mode mask, transparent RGBA, and pure-white RGB cutout safe for Wan2GP MSR."""
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(source_path) as im:
        src = im.convert("RGB")
    w, h = src.size
    rgb_np = np.asarray(src, dtype=np.uint8)
    a = np.asarray(alpha_arr, dtype=np.float32)
    if a.ndim == 3:
        a = a[..., 0]
    if a.max() > 1.5:
        a = a / 255.0
    a = np.clip(a, 0.0, 1.0)
    if a.shape != (h, w):
        a = cv2.resize(a, (w, h), interpolation=cv2.INTER_LINEAR)

    fg_override = None
    if refine_edges:
        try:
            res = _refine_alpha_with_pymatting(rgb_np, (a * 255).astype(np.uint8))
            if isinstance(res, tuple):
                a, fg_override = res
            else:
                a = res
        except Exception:
            pass

    if feather_px and feather_px > 0.2:
        k = int(max(3, (round(float(feather_px) * 2) * 2 + 1)))
        a = cv2.GaussianBlur(a, (k, k), float(feather_px))
        a = np.clip(a, 0.0, 1.0)

    a_u8 = np.clip(np.round(a * 255.0), 0, 255).astype(np.uint8)
    fg_rgb = np.clip(np.round(fg_override * 255.0), 0, 255).astype(np.uint8) if fg_override is not None else rgb_np

    if mask_path:
        Path(mask_path).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(a_u8, "L").save(mask_path, format="PNG")
    if rgba_path:
        Path(rgba_path).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(np.dstack((fg_rgb, a_u8)), "RGBA").save(rgba_path, format="PNG")
    if cutout_path:
        Path(cutout_path).parent.mkdir(parents=True, exist_ok=True)
        a3 = a[..., None]
        white_bg = np.full((h, w, 3), 255.0, dtype=np.float32)
        comp = np.clip(np.round(fg_rgb.astype(np.float32) * a3 + white_bg * (1.0 - a3)), 0, 255).astype(np.uint8)
        Image.fromarray(comp, "RGB").save(cutout_path, format="PNG")

    fg_ratio = float((a_u8 >= 128).mean())
    return {"width": w, "height": h, "fg_ratio": round(fg_ratio, 4)}


def _encode_mask_b64(mask_u8, max_side: int = 384) -> str:
    import numpy as np
    from PIL import Image

    im = Image.fromarray(np.asarray(mask_u8, dtype=np.uint8), "L")
    w, h = im.size
    if max(w, h) > max_side:
        sc = max_side / max(w, h)
        im = im.resize((max(16, int(round(w * sc))), max(16, int(round(h * sc)))), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _run_yolo_instances(src_path: Path, wgp_root: Path, use_gpu: bool = False):
    import cv2
    import numpy as np
    from PIL import Image

    _ensure_wgp_sys_path(wgp_root)
    from postprocessing.face_refiner.face import _load_yolo

    ckpt = wgp_root / "ckpts" / "buffalo_l" / "person_yolov8m-seg.safetensors"
    if not ckpt.is_file():
        raise FileNotFoundError(f"YOLOv8m-seg checkpoint not found: {ckpt}")
    with Image.open(src_path) as im:
        w, h = im.size
    model = _load_yolo(str(ckpt))
    dev = "0" if (use_gpu and _cuda_ok()) else "cpu"
    res = model.predict(str(src_path), device=dev, conf=0.25, retina_masks=True, verbose=False)[0]
    raw_masks = res.masks.data.cpu().numpy() if res.masks is not None else np.zeros((0, h, w), dtype=np.float32)

    dets = []
    for i, b in enumerate(res.boxes):
        x0, y0, x1, y1 = [float(v) for v in b.xyxy[0]]
        conf = float(b.conf[0])
        m = cv2.resize(raw_masks[i].astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR) if i < len(raw_masks) else np.zeros((h, w), dtype=np.float32)
        area_px = int((m >= 0.45).sum())
        cx_dist = abs(((x0 + x1) / (2 * max(1, w))) - 0.5)
        rank_score = area_px * (0.65 + 0.35 * conf) * (1.15 - 0.3 * cx_dist)
        dets.append({
            "raw_idx": i,
            "box_norm": [round(x0 / max(1, w), 5), round(y0 / max(1, h), 5), round(x1 / max(1, w), 5), round(y1 / max(1, h), 5)],
            "conf": round(conf, 4),
            "area_px": area_px,
            "score": rank_score,
            "mask": m,
        })
    dets.sort(key=lambda d: d["score"], reverse=True)
    for idx_sorted, d in enumerate(dets):
        d["idx"] = idx_sorted
    return dets, w, h


def _cuda_ok() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _run_u2net(src_path: Path, wgp_root: Path, msr_preview: bool = False, use_gpu: bool = False):
    import numpy as np
    import torch
    from PIL import Image, ImageOps
    from safetensors.torch import load_file

    _ensure_wgp_sys_path(wgp_root)
    from models.background_removal.u2net import U2NET
    from preprocessing.rembg.native import _input, _matting

    ckpt = wgp_root / "ckpts" / "rembg" / "u2net_bf16.safetensors"
    if not ckpt.is_file():
        raise FileNotFoundError(f"U2-Net checkpoint not found: {ckpt}")
    with Image.open(src_path) as im:
        orig = ImageOps.exif_transpose(im.convert("RGB"))
    ow, oh = orig.size

    if msr_preview:
        bw, bh = 1280, 704
        canvas = np.full((bh, bw, 3), 255, dtype=np.uint8)
        scale = min(bh / oh, bw / ow)
        nh, nw = int(oh * scale), int(ow * scale)
        top, left = (bh - nh) // 2, (bw - nw) // 2
        rim = orig.resize((nw, nh), resample=Image.Resampling.LANCZOS)
        canvas[top:top + nh, left:left + nw] = np.asarray(rim)
        pil_in = Image.fromarray(canvas)
    else:
        pil_in = orig

    model = U2NET()
    sd = load_file(str(ckpt), device="cpu")
    model.load_state_dict({k: v.float() for k, v in sd.items()}, strict=True)
    dev = "cuda" if (use_gpu and _cuda_ok()) else "cpu"
    model.to(dev).eval()
    with torch.inference_mode():
        logits = model(_input(pil_in, "u2net").to(dev)).float()
        probs = logits.sigmoid()[0, 0]
        mn, mx = probs.amin(), probs.amax()
        m_arr = ((probs - mn) / (mx - mn).clamp_min(1e-8) * 255).clamp(0, 255).byte().cpu().numpy()
        mask = Image.fromarray(m_arr, "L").resize(pil_in.size, Image.Resampling.LANCZOS)
    try:
        rgba = _matting(pil_in, mask, 240, 10, 1)
        alpha = np.asarray(rgba.split()[-1], dtype=np.float32) / 255.0
    except Exception:
        alpha = np.asarray(mask, dtype=np.float32) / 255.0

    if msr_preview:
        # Map canvas back onto source dimensions for transparent preview/overlay, AND save full 1280x704 preview if requested
        cropped_alpha = alpha[top:top + nh, left:left + nw]
        alpha = np.asarray(Image.fromarray(np.clip(cropped_alpha * 255, 0, 255).astype(np.uint8), "L").resize((ow, oh), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
    return alpha


def _run_native_rembg(src_path: Path, wgp_root: Path, model_name: str, crop_box: list[float] | None = None):
    import numpy as np
    from PIL import Image, ImageOps

    _ensure_wgp_sys_path(wgp_root)
    from preprocessing.rembg.native import NativeSession, remove, release

    with Image.open(src_path) as im:
        orig = ImageOps.exif_transpose(im.convert("RGB"))
    ow, oh = orig.size
    x0, y0, x1, y1 = 0, 0, ow, oh
    target_img = orig
    if crop_box and len(crop_box) == 4:
        nx0, ny0, nx1, ny1 = [max(0.0, min(1.0, float(v))) for v in crop_box]
        x0, y0 = int(round(min(nx0, nx1) * ow)), int(round(min(ny0, ny1) * oh))
        x1, y1 = int(round(max(nx0, nx1) * ow)), int(round(max(ny0, ny1) * oh))
        if (x1 - x0) >= 32 and (y1 - y0) >= 32:
            target_img = orig.crop((x0, y0, x1, y1))

    try:
        sess = NativeSession(model_name)
        mask_pil = remove(target_img, session=sess, only_mask=True)
        crop_a = np.asarray(mask_pil.convert("L"), dtype=np.float32) / 255.0
        full_a = np.zeros((oh, ow), dtype=np.float32)
        full_a[y0:y1, x0:x1] = crop_a[: (y1 - y0), : (x1 - x0)]
        return full_a
    finally:
        release()


def _run_sam3(src_path: Path, wgp_root: Path, box_norm: list[float] | None, text_prompt: str, points: list[list[float]] | None, point_labels: list[int] | None):
    import numpy as np
    from PIL import Image, ImageOps

    _ensure_wgp_sys_path(wgp_root)
    from preprocessing.sam3.preprocessor import _cleanup, _sam3_outputs_to_binary_mask, load_sam3_mask_predictor

    with Image.open(src_path) as im:
        orig = ImageOps.exif_transpose(im.convert("RGB"))
    ow, oh = orig.size
    payloads = build_sam3_prompt_requests(
        box_norm_xyxy=box_norm, text_prompt=text_prompt, points=points, point_labels=point_labels, width=ow, height=oh,
    )
    need_text = "text" in payloads["seed"]
    predictor = load_sam3_mask_predictor(include_text_encoder=need_text, postprocess_batch_size=1)
    session_id = None
    try:
        resp = predictor.handle_request({"type": "start_session", "resource_path": [orig], "offload_video_to_cpu": False, "cache_frame_outputs": False})
        session_id = resp["session_id"]
        s_req = {**payloads["seed"], "session_id": session_id}
        res = predictor.handle_request(s_req)
        outs = res.get("outputs") if isinstance(res, dict) else None
        if payloads["refine"] is not None:
            obj_ids = (outs or {}).get("out_obj_ids")
            oid = int(obj_ids[0]) if obj_ids is not None and len(obj_ids) > 0 else 1
            r_req = {**payloads["refine"], "session_id": session_id, "obj_id": oid}
            res2 = predictor.handle_request(r_req)
            outs = res2.get("outputs") if isinstance(res2, dict) else outs
        bmask = _sam3_outputs_to_binary_mask(outs, oh, ow, fill_hole_area=64)
        return np.asarray(bmask, dtype=np.float32)
    finally:
        if session_id is not None:
            try:
                predictor.handle_request({"type": "close_session", "session_id": session_id})
            except Exception:
                pass
        try:
            predictor.shutdown()
        except Exception:
            pass
        _cleanup()


def _dispatch(req: dict[str, Any]) -> dict[str, Any]:
    action = str(req.get("action") or "").strip().lower()
    src = Path(req["source_path"])
    wgp_root = Path(req["wgp_root"]) if req.get("wgp_root") else None
    use_gpu = bool(req.get("use_gpu", False))

    if action == "detect_instances":
        dets, w, h = _run_yolo_instances(src, wgp_root, use_gpu=use_gpu)
        return {
            "success": True,
            "width": w,
            "height": h,
            "detections": [
                {
                    "idx": d["idx"],
                    "box_norm": d["box_norm"],
                    "conf": d["conf"],
                    "area_px": d["area_px"],
                    "mask_b64": _encode_mask_b64((d["mask"] >= 0.45).astype("uint8") * 255),
                }
                for d in dets
            ],
        }

    if action == "composite_mask":
        import numpy as np
        from PIL import Image

        with Image.open(req["mask_path"]) as mp:
            alpha = np.asarray(mp.convert("L"), dtype=np.float32) / 255.0
        stats = save_mask_outputs(
            src, alpha,
            mask_path=req.get("out_mask_path"),
            rgba_path=req.get("out_rgba_path"),
            cutout_path=req.get("out_cutout_path"),
            feather_px=float(req.get("feather_px") or 0.0),
            refine_edges=bool(req.get("refine_edges", False)),
        )
        return {"success": True, **stats}

    if action == "extract_mask":
        method = str(req.get("method") or "yolo_person").strip().lower()
        refine_edges = bool(req.get("refine_edges", True))
        if method == "yolo_person":
            dets, _, _ = _run_yolo_instances(src, wgp_root, use_gpu=use_gpu)
            if not dets:
                raise RuntimeError("No person instance detected in image.")
            t_idx = max(0, min(len(dets) - 1, int(req.get("det_idx", 0))))
            tgt = dets[t_idx]["mask"]
            others = [d["mask"] for j, d in enumerate(dets) if j != t_idx]
            cleaned = subtract_bystander_masks(tgt, others, suppression=0.85, threshold=None)
            alpha = (cleaned >= 0.45).astype("float32")
        elif method in ("u2net", "u2net_msr_preview"):
            alpha = _run_u2net(src, wgp_root, msr_preview=(method == "u2net_msr_preview"), use_gpu=use_gpu)
            refine_edges = False
        elif method in ("birefnet", "birefnet-general", "bria_rmbg", "bria-rmbg"):
            m_name = "bria-rmbg" if "bria" in method else "birefnet-general"
            crop_box = req.get("crop_box")
            if not crop_box and bool(req.get("auto_box", False)) and (wgp_root / "ckpts" / "buffalo_l" / "person_yolov8m-seg.safetensors").is_file():
                dets, _, _ = _run_yolo_instances(src, wgp_root, use_gpu=use_gpu)
                if dets:
                    bx0, by0, bx1, by1 = dets[0]["box_norm"]
                    crop_box = [max(0.0, bx0 - 0.06), max(0.0, by0 - 0.06), min(1.0, bx1 + 0.06), min(1.0, by1 + 0.06)]
            alpha = _run_native_rembg(src, wgp_root, m_name, crop_box=crop_box)
            refine_edges = False
        elif method == "sam3":
            alpha = _run_sam3(
                src, wgp_root,
                box_norm=req.get("crop_box"),
                text_prompt=str(req.get("text_prompt") or ""),
                points=req.get("points"),
                point_labels=req.get("point_labels"),
            )
        else:
            raise ValueError(f"Unsupported mask extraction method: {method}")

        stats = save_mask_outputs(
            src, alpha,
            mask_path=req.get("out_mask_path"),
            rgba_path=req.get("out_rgba_path"),
            cutout_path=req.get("out_cutout_path"),
            feather_px=float(req.get("feather_px") or 0.0),
            refine_edges=refine_edges,
        )
        return {"success": True, "method": method, **stats}

    raise ValueError(f"Unknown char_mask_worker action: {action}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MVG Character Masking & Extraction Worker")
    parser.add_argument("--request", required=True, help="Path to JSON request file, or '-' for stdin.")
    args = parser.parse_args()
    raw = sys.stdin.read() if args.request == "-" else Path(args.request).read_text(encoding="utf-8")
    req = json.loads(raw)
    try:
        res = _dispatch(req)
        print(json.dumps(res), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
