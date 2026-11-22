"""Shared Cropped-ROI geometry, masking, feathered compositing, cross-engine Pass 2, and lip-sync repair."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .media_utils import (
    probe_video_dimensions,
    probe_video_fps,
    probe_video_frames,
    read_embedded_render_settings,
    resolve_rendered_clip_path,
)
from .takes import _find_strip, _pass_key, load_takes_manifest, safe_link_or_copy, save_takes_manifest

VALID_ROI_MODES = ("full", "half_split", "tight_performer", "custom")


def normalize_roi_mode(raw: Any, default: str = "full") -> str:
    s = str(raw or "").strip().lower()
    if s in ("half", "half_split", "split", "roi"):
        return "half_split"
    if s in ("tight", "tight_performer", "face", "upper_body"):
        return "tight_performer"
    if s == "custom":
        return "custom"
    if s in ("full", "full_frame", "none", "off"):
        return "full"
    return default


def normalize_roi_side(raw: Any, default: str = "right") -> str:
    s = str(raw or "").strip().lower()
    if s in ("left", "l", "m", "male", "p1"):
        return "left"
    if s == "custom":
        return "custom"
    if s in ("right", "r", "f", "female", "p2"):
        return "right"
    return default


def normalize_roi_box(raw: Any) -> list[float] | None:
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        raw = [float(p) for p in parts] if len(parts) == 4 else None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = [max(0.0, min(1.0, float(v))) for v in raw]
    except (TypeError, ValueError):
        return None
    if x1 <= x0 + 0.05 or y1 <= y0 + 0.05:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _snap_dim(raw_val: int, max_val: int, align: int = 32) -> int:
    step = 32 if (align == 32 and max_val >= 288) else 16
    max_aligned = (max_val // step) * step if max_val >= step else (max_val & ~1)
    min_dim = min(max_aligned, 256)
    snapped = ((int(raw_val) + step // 2) // step) * step
    return max(min_dim, min(max_aligned, snapped))


def compute_roi_box(
    width: int,
    height: int,
    split_x: float = 0.5,
    side: str = "right",
    roi_mode: str = "half_split",
    custom_box: list[float] | tuple[float, ...] | None = None,
) -> tuple[int, int, int, int]:
    """Computes a 32-pixel patch-aligned (cx, cy, cw, ch) crop box inside (width, height)."""
    w, h = max(32, int(width)), max(32, int(height))
    rm = normalize_roi_mode(roi_mode, default="half_split")
    sd = normalize_roi_side(side, default="right")
    sx = max(0.15, min(0.85, float(split_x if split_x is not None else 0.5)))

    cbox = normalize_roi_box(custom_box)
    if (rm == "custom" or sd == "custom") and cbox is not None:
        rx0, ry0, rx1, ry1 = cbox
        cw = _snap_dim(round((rx1 - rx0) * w), w, align=32)
        ch = _snap_dim(round((ry1 - ry0) * h), h, align=32)
        cx = max(0, min(w - cw, round(((rx0 + rx1) * 0.5 * w - cw * 0.5) / 2.0) * 2))
        cy = max(0, min(h - ch, round(((ry0 + ry1) * 0.5 * h - ch * 0.5) / 2.0) * 2))
        return cx, cy, cw, ch

    if rm == "full":
        cw, ch = (w // 16) * 16, (h // 16) * 16
        return max(0, (w - cw) // 2) & ~1, max(0, (h - ch) // 2) & ~1, cw, ch

    split_px = int(round(w * sx)) & ~1
    if rm == "tight_performer":
        half_w = (w - split_px) if sd == "right" else split_px
        cw = _snap_dim(round(max(half_w * 0.90, w * 0.45)), w, align=32)
        ch = _snap_dim(round(h * 0.72), h, align=32)
        cx = max(0, (w - cw) & ~1) if sd == "right" else 0
        cy = max(0, min(h - ch, round((h * 0.04) / 2.0) * 2))
        return cx, cy, cw, ch

    # half_split
    ch = (h // 16) * 16
    cy = max(0, (h - ch) // 2) & ~1
    if sd == "left":
        cw = _snap_dim(split_px, w, align=32)
        return 0, cy, cw, ch
    cw = _snap_dim(w - split_px, w, align=32)
    cx = max(0, min(w - cw, (w - cw) & ~1))
    return cx, cy, cw, ch


def _resolve_border_px(cw: int, ch: int, border_px: int) -> int:
    if int(border_px) <= 0:
        return 0
    max_b = min(cw // 5, ch // 5)
    if int(border_px) >= 32 and max_b >= 32:
        return max(32, (min(int(border_px), max_b) // 32) * 32)
    return max(4, min(int(border_px), cw // 8, ch // 8))


def _edge_feather_weight(dist: int, span: int, start: int) -> float:
    if dist < start:
        return 0.0
    if dist < start + span:
        return (dist - start + 0.5) / span
    return 1.0


def write_border_ring_pgm(path: Path, cw: int, ch: int, full_w: int, full_h: int, cx: int, cy: int, border_px: int = 64) -> None:
    """Writes a GVA PGM mask with >=32px patch-aligned 0-locked borders on edges shared with Pass 1."""
    b = _resolve_border_px(cw, ch, border_px)
    b_l = b if cx > 0 else 0
    b_r = b if (cx + cw) < full_w else 0
    b_t = b if cy > 0 else 0
    b_b = b if (cy + ch) < full_h else 0
    inner_w = max(1, cw - b_l - b_r)
    interior_row = (b"\x00" * b_l) + (b"\xff" * inner_w) + (b"\x00" * b_r)
    solid_zero = b"\x00" * cw
    header = f"P5\n{cw} {ch}\n255\n".encode("ascii")
    rows = [solid_zero if (y < b_t or y >= ch - b_b) else interior_row for y in range(ch)]
    path.write_bytes(header + b"".join(rows))


def write_feather_alpha_pgm(
    path: Path, cw: int, ch: int, full_w: int, full_h: int, cx: int, cy: int, feather_px: int = 20, border_px: int = 0
) -> None:
    """Writes a feathered alpha PGM (0..255 ramps shifted across locked border when border_px > 0)."""
    f = max(2, min(int(feather_px), cw // 8, ch // 8))
    b = _resolve_border_px(cw, ch, border_px)
    start = max(0, b - 4) if b > 0 else 0
    f_l = f if cx > 0 else 0
    f_r = f if (cx + cw) < full_w else 0
    f_t = f if cy > 0 else 0
    f_b = f if (cy + ch) < full_h else 0
    x_w = [1.0] * cw
    for i in range(min(cw, start + f_l)) if f_l else ():
        x_w[i] = min(x_w[i], _edge_feather_weight(i, f_l, start))
    for i in range(min(cw, start + f_r)) if f_r else ():
        x_w[cw - 1 - i] = min(x_w[cw - 1 - i], _edge_feather_weight(i, f_r, start))
    data = bytearray(cw * ch)
    for y in range(ch):
        yw = 1.0
        if f_t and y < start + f_t:
            yw = min(yw, _edge_feather_weight(y, f_t, start))
        if f_b and (ch - 1 - y) < start + f_b:
            yw = min(yw, _edge_feather_weight(ch - 1 - y, f_b, start))
        if yw <= 0.001:
            continue
        off = y * cw
        if yw >= 0.999 and not f_l and not f_r:
            data[off : off + cw] = b"\xff" * cw
        else:
            for x in range(cw):
                data[off + x] = max(0, min(255, int(round(255.0 * x_w[x] * yw))))
    path.write_bytes(f"P5\n{cw} {ch}\n255\n".encode("ascii") + bytes(data))


def ensure_aligned_pass2_guide(ffmpeg: Path, guide_mp4: Path, target_res: str, target_fps: float, cache_dir: Path) -> Path:
    """Normalizes Pass 1 guide dimensions/FPS to target_res when cross-engine Pass 1 differs."""
    try:
        tw, th = [int(x) for x in str(target_res).lower().split("x", 1)]
    except ValueError:
        return guide_mp4
    dims = probe_video_dimensions(ffmpeg, guide_mp4)
    fps = probe_video_fps(ffmpeg, guide_mp4)
    if dims == (tw, th) and (fps is None or abs(fps - float(target_fps)) <= 0.5):
        return guide_mp4
    cache_dir.mkdir(parents=True, exist_ok=True)
    sig = f"v2_{guide_mp4.stem}_{int(guide_mp4.stat().st_mtime_ns)}_{tw}x{th}_{float(target_fps):.2f}"
    aligned = cache_dir / f".guide_aligned_{hashlib.sha256(sig.encode()).hexdigest()[:10]}.mp4"
    if aligned.is_file() and aligned.stat().st_size > 1000:
        return aligned
    vf = f"scale={tw}:{th}:flags=lanczos,fps={float(target_fps):.3f},setsar=1:1"
    subprocess.run(
        [str(ffmpeg), "-y", "-i", str(guide_mp4), "-vf", vf, "-an", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(aligned)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return aligned


def ensure_aligned_pass2_start_image(
    ffmpeg: Path, raw_guide_mp4: Path, aligned_guide_mp4: Path, cache_dir: Path
) -> Path | None:
    """Extracts Frame 0 of the aligned Pass 1 guide so full-frame Pass 2 image_start matches video_guide framing."""
    if not aligned_guide_mp4.is_file():
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    first_jpg = cache_dir / f".guide_start_{aligned_guide_mp4.stem}.jpg"
    src_mtime = aligned_guide_mp4.stat().st_mtime_ns
    if raw_guide_mp4.is_file():
        src_mtime = max(src_mtime, raw_guide_mp4.stat().st_mtime_ns)
    if first_jpg.is_file() and first_jpg.stat().st_size > 500 and first_jpg.stat().st_mtime_ns >= src_mtime:
        return first_jpg
    subprocess.run(
        [str(ffmpeg), "-y", "-i", str(aligned_guide_mp4), "-vframes", "1", "-q:v", "1", str(first_jpg)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return first_jpg if first_jpg.is_file() and first_jpg.stat().st_size > 0 else None


def prepare_roi_crop_and_mask(
    ffmpeg: Path,
    guide_mp4: Path,
    box: tuple[int, int, int, int],
    full_w: int,
    full_h: int,
    frames: int,
    fps: float,
    out_crop_mp4: Path,
    out_mask_mp4: Path | None = None,
    out_first_jpg: Path | None = None,
    border_px: int = 64,
) -> None:
    cx, cy, cw, ch = box
    out_crop_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-i", str(guide_mp4), "-vf", f"crop={cw}:{ch}:{cx}:{cy}", "-an", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(out_crop_mp4)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if out_first_jpg is not None:
        subprocess.run(
            [str(ffmpeg), "-y", "-i", str(out_crop_mp4), "-vframes", "1", "-q:v", "2", str(out_first_jpg)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if out_mask_mp4 is not None:
        mask_pgm = out_mask_mp4.with_suffix(".pgm")
        write_border_ring_pgm(mask_pgm, cw, ch, full_w, full_h, cx, cy, border_px=border_px)
        try:
            subprocess.run(
                [
                    str(ffmpeg), "-y", "-loop", "1", "-framerate", f"{float(fps):.3f}", "-i", str(mask_pgm),
                    "-frames:v", str(max(1, int(frames))), "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(out_mask_mp4),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            mask_pgm.unlink(missing_ok=True)


def composite_roi_onto_guide(
    ffmpeg: Path,
    base_mp4: Path,
    crop_out_mp4: Path,
    box: tuple[int, int, int, int],
    out_mp4: Path,
    audio_path: Path | None = None,
    feather_px: int = 20,
    preserve_base_audio: bool = False,
    border_px: int = 0,
) -> Path:
    """Blends crop_out_mp4 back onto base_mp4 at box=(cx,cy,cw,ch) with feathered seam edges."""
    cx, cy, cw, ch = box
    base_dims = probe_video_dimensions(ffmpeg, base_mp4) or (cx + cw, cy + ch)
    full_w, full_h = base_dims
    fps = probe_video_fps(ffmpeg, base_mp4) or 24.0
    n_frames = probe_video_frames(ffmpeg, base_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    alpha_pgm = out_mp4.with_name(f".roi_alpha_{out_mp4.stem}_{cw}x{ch}.pgm")
    write_feather_alpha_pgm(alpha_pgm, cw, ch, full_w, full_h, cx, cy, feather_px=feather_px, border_px=border_px)
    try:
        use_ext_audio = (not preserve_base_audio) and audio_path is not None and Path(audio_path).is_file() and "silent" not in Path(audio_path).name.lower()
        fc = (
            f"[1:v]scale={cw}:{ch}:flags=lanczos,format=yuva420p[fg0];"
            f"[2:v]format=gray[al];"
            f"[fg0][al]alphamerge[fg];"
            f"[0:v][fg]overlay={cx}:{cy}:shortest=1:eof_action=repeat[v]"
        )
        cmd = [
            str(ffmpeg), "-y", "-i", str(base_mp4), "-i", str(crop_out_mp4),
            "-loop", "1", "-framerate", f"{float(fps):.3f}", "-i", str(alpha_pgm),
        ]
        if use_ext_audio:
            cmd.extend(["-i", str(audio_path), "-filter_complex", fc, "-map", "[v]", "-map", "3:a:0"])
        elif preserve_base_audio:
            cmd.extend(["-filter_complex", fc, "-map", "[v]", "-map", "0:a?"])
        else:
            cmd.extend(["-filter_complex", fc, "-map", "[v]", "-map", "1:a?"])
        if n_frames and n_frames > 0:
            cmd.extend(["-frames:v", str(int(n_frames))])
        cmd.extend(["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out_mp4)])
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_mp4
    finally:
        alpha_pgm.unlink(missing_ok=True)


def composite_roi_cloud_pass2(ctx, task: dict, out_path: Path, render_crop_fn) -> Path:
    """Shared Two-Pass Duet Pass 2 ROI crop + cloud render + feathered overlay composite."""
    guide = Path(str(task.get("video_guide") or ""))
    if not guide.is_file():
        raise FileNotFoundError(f"Pass 1 video guide missing for cloud duet Pass 2: {guide}")
    ffmpeg = getattr(ctx, "ffmpeg", Path("ffmpeg"))
    fctx = task.get("_fal_ctx") or {}
    split_x = float(fctx.get("split_x", 0.50))
    side = normalize_roi_side(fctx.get("pass2_roi_side") or "right")
    roi_mode = normalize_roi_mode(fctx.get("pass2_roi_mode") or "half_split", default="half_split")
    if roi_mode == "full":
        roi_mode = "half_split"
    custom_box = normalize_roi_box(fctx.get("pass2_roi_box"))
    w, h = probe_video_dimensions(ffmpeg, guide) or (1344, 768)
    box = compute_roi_box(w, h, split_x=split_x, side=side, roi_mode=roi_mode, custom_box=custom_box)

    audio_p = Path(str(task.get("audio_guide") or ""))
    if not audio_p.is_file() or "silent" in audio_p.name.lower():
        shutil.copy2(str(guide), str(out_path))
        return out_path

    fps = probe_video_fps(ffmpeg, guide) or float(getattr(ctx, "fps", 24) or 24)
    n_frames = probe_video_frames(ffmpeg, guide) or int(task.get("video_length") or 120)
    crop_in = out_path.with_name(f".cloud_roi_in_{out_path.name}")
    crop_out = out_path.with_name(f".cloud_roi_out_{out_path.name}")
    tmp_comp = out_path.with_name(f".cloud_roi_tmp_{out_path.name}")
    try:
        prepare_roi_crop_and_mask(ffmpeg, guide, box, w, h, n_frames, fps, crop_in, out_mask_mp4=None, out_first_jpg=None)
        render_crop_fn(crop_in, audio_p, crop_out, box[2], box[3])
        composite_roi_onto_guide(ffmpeg, guide, crop_out, box, tmp_comp, audio_path=audio_p, feather_px=18, preserve_base_audio=False)
        shutil.move(str(tmp_comp), str(out_path))
        return out_path
    finally:
        for tmp in (crop_in, crop_out, tmp_comp):
            tmp.unlink(missing_ok=True)


def prepare_local_roi_pass2_task(ctx, mp4_p1: Path, exec_task: dict, sc: dict, res_str: str, frames: int, fps: float) -> tuple[dict, dict | None]:
    """Aligns Pass 1 guide dimensions/FPS and optionally crops to target ROI for fast Local GVA Pass 2."""
    if not mp4_p1.is_file():
        return exec_task, None
    ffmpeg = getattr(ctx, "ffmpeg", Path("ffmpeg"))
    cache_dir = ctx.jobs_dir / "guide_cache"
    aligned = ensure_aligned_pass2_guide(ffmpeg, mp4_p1, res_str, fps, cache_dir)
    updated = dict(exec_task, video_guide=str(aligned))
    roi_mode = normalize_roi_mode(sc.get("pass2_roi_mode"), default="full")
    if roi_mode == "full":
        if aligned != mp4_p1 or not read_embedded_render_settings(ffmpeg, mp4_p1):
            first_jpg = ensure_aligned_pass2_start_image(ffmpeg, mp4_p1, aligned, cache_dir)
            if first_jpg is not None:
                updated["image_start"] = str(first_jpg)
        return updated, None
    w, h = probe_video_dimensions(ffmpeg, aligned) or [int(x) for x in res_str.lower().split("x")]
    split_x = float(sc.get("split_x", 0.50))
    side = normalize_roi_side(sc.get("pass2_roi_side") or "right")
    custom_box = normalize_roi_box(sc.get("pass2_roi_box"))
    box = compute_roi_box(w, h, split_x=split_x, side=side, roi_mode=roi_mode, custom_box=custom_box)
    cx, cy, cw, ch = box
    out_stem = str(updated.get("output_filename") or sc.get("id") or "roi")
    tmp_dir = ctx.jobs_dir / "roi_work"
    crop_in = tmp_dir / f"{out_stem}_guide_{cw}x{ch}.mp4"
    crop_mask = tmp_dir / f"{out_stem}_mask_{cw}x{ch}.mp4"
    first_jpg = tmp_dir / f"{out_stem}_first_{cw}x{ch}.jpg"
    prepare_roi_crop_and_mask(ffmpeg, aligned, box, w, h, frames, fps, crop_in, out_mask_mp4=crop_mask, out_first_jpg=first_jpg)
    updated["video_guide"] = str(crop_in)
    updated["video_mask"] = str(crop_mask)
    updated["image_start"] = str(first_jpg)
    updated["resolution"] = f"{cw}x{ch}"
    updated["output_filename"] = f".roi_crop_{out_stem}"
    updated.pop("spatial_upsampling", None)
    raw_audio = str(updated.get("audio_guide") or "")
    has_live_audio = bool(raw_audio and "silent" not in Path(raw_audio).name.lower() and Path(raw_audio).is_file())
    meta = {
        "base_mp4": aligned,
        "box": box,
        "audio_path": Path(raw_audio) if has_live_audio else None,
        "tmp_files": [crop_in, crop_mask, first_jpg],
    }
    return updated, meta


def finalize_local_roi_pass2_output(ctx, gen_path: Path, out_mp4: Path, roi_meta: dict | None) -> Path:
    """Composites Local GPU cropped ROI render back onto Pass 1 video and cleans temp crop artifacts."""
    if not roi_meta:
        if Path(gen_path).resolve() != Path(out_mp4).resolve():
            shutil.copy2(str(gen_path), str(out_mp4))
        return out_mp4
    ffmpeg = getattr(ctx, "ffmpeg", Path("ffmpeg"))
    tmp_comp = out_mp4.with_name(f".roi_comp_{out_mp4.name}")
    try:
        composite_roi_onto_guide(
            ffmpeg,
            roi_meta["base_mp4"],
            gen_path,
            roi_meta["box"],
            tmp_comp,
            audio_path=roi_meta.get("audio_path"),
            feather_px=20,
            preserve_base_audio=not bool(roi_meta.get("audio_path")),
            border_px=64,
        )
        shutil.move(str(tmp_comp), str(out_mp4))
        return out_mp4
    finally:
        if tmp_comp.exists():
            tmp_comp.unlink(missing_ok=True)
        for tf in roi_meta.get("tmp_files") or []:
            try:
                Path(tf).unlink(missing_ok=True)
            except OSError:
                pass
        if Path(gen_path).exists() and Path(gen_path).resolve() != Path(out_mp4).resolve():
            try:
                Path(gen_path).unlink(missing_ok=True)
            except OSError:
                pass


def apply_pass2_guide_and_roi(
    ctx,
    sc: dict,
    pass_num: int | None,
    exec_task: dict,
    task_backend: str,
    dummy_render: bool = False,
) -> tuple[dict, dict | None]:
    """Resolves Pass 1 guide clip, aligns cross-engine dims/FPS, and prepares Local ROI crop if enabled."""
    if dummy_render or pass_num != 2:
        return exec_task, None
    vg_val = exec_task.get("video_guide")
    if not vg_val:
        return exec_task, None
    vg_path = Path(str(vg_val))
    mp4_p1 = vg_path if vg_path.is_file() else (resolve_rendered_clip_path(ctx.renders_dir, vg_path.stem) or vg_path)
    if not mp4_p1.is_file():
        return exec_task, None
    if str(task_backend or "local").strip().lower() != "local":
        return dict(exec_task, video_guide=str(mp4_p1)), None
    res_str = str(exec_task.get("resolution") or "1344x768")
    frames = int(exec_task.get("video_length") or 81)
    try:
        fps = float(exec_task.get("force_fps") or getattr(ctx, "target_fps", 24) or 24)
    except (TypeError, ValueError):
        fps = float(getattr(ctx, "target_fps", 24) or 24)
    return prepare_local_roi_pass2_task(ctx, mp4_p1, exec_task, sc or {}, res_str, frames, fps)


def repair_scene_lipsync_clip(
    ctx,
    lazy_session,
    scene_id: str,
    side: str = "right",
    roi_mode: str = "half_split",
    roi_box: list[float] | str | None = None,
    stem: str = "",
    backend: str = "local",
    take_id: str | None = None,
    prompt: str = "",
    dummy_render: bool = False,
    split_x: float | None = None,
) -> dict:
    """Repairs a single performer's lip-sync via cropped ROI GVA / cloud Sync on an existing scene take."""
    from .fal_catalog import resolve_fal_route, resolve_minimax_route
    from .prompts import compile_modular_scene_prompt, resolve_marker_prompt
    from .upscale import resolve_scene_canonical_stem
    from .wgp_bridge import execute_wgp_job, resolve_session_for_backend, write_engine_status

    sc = next((s for s in ctx.scenes if str(s.get("id", "")).upper() == scene_id.strip().upper()), None)
    if not sc:
        raise ValueError(f"Scene '{scene_id}' not found in project config.")
    sid = sc["id"]
    side_norm = normalize_roi_side(side, default="right")
    mode_norm = normalize_roi_mode(roi_mode, default="half_split")
    if mode_norm == "full":
        mode_norm = "half_split"
    box_norm = normalize_roi_box(roi_box)
    eff_backend = str(backend or "local").strip().lower()
    if eff_backend not in ("local", "fal", "minimax"):
        eff_backend = "local"

    _, vrspec = ctx.resolve_video_renderer(sc, pass_num=2)
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

    head_pad, tail_pad = ctx.get_scene_render_padding(sc)
    req_start = max(0.0, float(sc.get("start", 0)) - head_pad)
    fps = float(probe_video_fps(ctx.ffmpeg, src_mp4) or ctx.model_fps(vrspec) or ctx.fps or 24)
    n_frames = int(probe_video_frames(ctx.ffmpeg, src_mp4) or ctx.valid_video_frames(float(sc.get("end", 0)) - req_start + tail_pad, vrspec))
    dur_s = n_frames / max(1.0, fps)

    is_gva_silent = str(sc.get("mode", "")).lower() == "gva_male_left_silent_right"
    def_stem = ("silent" if (is_gva_silent and side_norm == "right") else None) or sc.get("stem_m" if side_norm == "left" else "stem_f") or sc.get("stem") or ("male" if side_norm == "left" else "female")
    target_stem = str(stem or def_stem).strip()
    wav_audio = ctx.resolve_scene_audio(sid, target_stem, req_start, dur_s, n_frames)
    if eff_backend in ("fal", "minimax") and "silent" in wav_audio.name.lower():
        raise ValueError(f"Scene '{sid}' target stem '{target_stem}' resolved to silence; select a vocal stem for cloud lip-sync repair.")

    sx = float(split_x if split_x is not None else sc.get("split_x", 0.50))
    sw, sh = probe_video_dimensions(ctx.ffmpeg, src_mp4) or [int(x) for x in res_str.lower().split("x")]
    box = compute_roi_box(sw, sh, split_x=sx, side=side_norm, roi_mode=mode_norm, custom_box=box_norm)
    cx, cy, cw, ch = box

    t0 = time.time()
    op_label = f"repair:{side_norm}/{mode_norm}@{eff_backend}"
    write_engine_status(ctx, {"state": "rendering", "backend": eff_backend, "phase": "repairing", "raw_phase": f"Lip-Sync Repair {sid} ({cw}x{ch})", "filename": active_mp4.name, "pct": 10.0}, backend=eff_backend, task_stem=out_name)

    tmp_dir = ctx.jobs_dir / "roi_repair"
    crop_in = tmp_dir / f".rep_in_{sid}_{cw}x{ch}.mp4"
    crop_mask = tmp_dir / f".rep_mask_{sid}_{cw}x{ch}.mp4"
    crop_first = tmp_dir / f".rep_first_{sid}_{cw}x{ch}.jpg"
    crop_out = tmp_dir / f".rep_out_{sid}_{cw}x{ch}.mp4"
    rep_task_stem = f".rep_crop_{sid}_{cw}x{ch}"
    rep_task_mp4 = ctx.renders_dir / f"{rep_task_stem}.mp4"
    tmp_comp = ctx.renders_dir / f".repair_comp_{sid}_{int(time.time())}.mp4"
    aud_mux = tmp_comp.with_name(f".rep_mux_{sid}.mp4")

    try:
        prepare_roi_crop_and_mask(ctx.ffmpeg, src_mp4, box, sw, sh, n_frames, fps, crop_in, out_mask_mp4=crop_mask, out_first_jpg=crop_first)
        if dummy_render:
            subprocess.run(
                [str(ctx.ffmpeg), "-y", "-i", str(crop_in), "-vf", "eq=brightness=0.01", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-an", str(crop_out)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            raw_p = prompt or compile_modular_scene_prompt(sc, pass_num=2 if side_norm == "right" else 1) or f"Close-up of the singer on the {side_norm} singing naturally with synchronized lip movements."
            resolved_p = resolve_marker_prompt(raw_p, req_start, ctx.markers, fps=fps)
            seed_val = int(sc.get("seed_f" if side_norm == "right" else "seed_m") or sc.get("seed") or 8301)
            session = resolve_session_for_backend(lazy_session, eff_backend)
            sc_clean = {k: v for k, v in sc.items() if k not in ("backend_m", "backend_f", "renderer_m", "renderer_f")}
            sc_clean["backend"] = eff_backend
            rep_task = ctx.make_video_task(
                output_filename=rep_task_stem,
                prompt=resolved_p,
                video_length=n_frames,
                seed=seed_val,
                image_start=crop_first,
                audio_path=wav_audio,
                rspec=vrspec,
                video_guide=crop_in,
                video_mask=crop_mask,
                sc=sc_clean,
                pass_num=2,
            )
            rep_task["resolution"] = f"{cw}x{ch}"
            rep_task["video_prompt_type"] = vrspec.get("pass2_video_prompt_type", "GVA")
            if eff_backend == "fal":
                rep_task["_backend"] = "fal"
                rep_task["backend"] = "fal"
                rep_task["_fal_spec"] = {"endpoint": "fal-ai/sync-lipsync/v2", "adapter": "duet_pass2_roi_lipsync", "max_duration_s": 15.0}
                rep_task["_fal_ctx"] = {"id": sid, "mode": "duet_method_b", "split_x": sx, "pass2_roi_side": side_norm, "pass2_roi_mode": mode_norm, "pass2_roi_box": box_norm}
                rep_task["video_guide"] = str(src_mp4)
                rep_task["output_filename"] = tmp_comp.stem
            elif eff_backend == "minimax":
                mm_rt = resolve_minimax_route(vrspec, sc=dict(sc_clean, mode="duet_method_b"), pass_num=2, resolution=f"{cw}x{ch}")
                rep_task["_backend"] = "minimax"
                rep_task["backend"] = "minimax"
                rep_task["_fal_spec"] = mm_rt
                rep_task["_fal_ctx"] = {"id": sid, "mode": "duet_method_b", "split_x": sx, "pass2_roi_side": side_norm, "pass2_roi_mode": mode_norm, "pass2_roi_box": box_norm}
                rep_task["video_guide"] = str(src_mp4)
                rep_task["output_filename"] = tmp_comp.stem
            else:
                rep_task.pop("spatial_upsampling", None)
                rep_task = {k: v for k, v in rep_task.items() if not k.startswith("_") and k not in ("backend", "fal_endpoint", "minimax_model")}

            res, cancelled, _ = execute_wgp_job(
                ctx=ctx, session=session, task_cfg=rep_task, idx=1, total=1,
                filename=active_mp4.name, stage="repair", reason=op_label,
                model=str(rep_task.get("model_type") or ""), frames=n_frames, resolution=f"{cw}x{ch}",
            )
            if cancelled or not getattr(res, "success", True) or not getattr(res, "generated_files", None):
                errs = [getattr(e, "message", str(e)) for e in (getattr(res, "errors", None) or [])]
                raise RuntimeError(f"Lip-sync repair failed for {sid}: {errs or 'cancelled'}")
            prod = Path(res.generated_files[0])
            if eff_backend in ("fal", "minimax"):
                if prod.resolve() != tmp_comp.resolve():
                    shutil.move(str(prod), str(tmp_comp))
                # Ensure original stereo/base audio from source take is preserved cleanly across cloud repair
                if str(sc.get("mode", "")).lower() in ("duet_method_a", "duet_a") and not stem:
                    subprocess.run([str(ctx.ffmpeg), "-y", "-i", str(tmp_comp), "-i", str(src_mp4), "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-shortest", str(aud_mux)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    shutil.move(str(aud_mux), str(tmp_comp))
            else:
                if prod.resolve() != crop_out.resolve():
                    shutil.move(str(prod), str(crop_out))

        if dummy_render or eff_backend == "local":
            keep_orig = (not stem) or str(sc.get("mode", "")).lower() in ("duet_method_a", "duet_a")
            composite_roi_onto_guide(
                ctx.ffmpeg, src_mp4, crop_out, box, tmp_comp,
                audio_path=wav_audio, feather_px=20, preserve_base_audio=keep_orig, border_px=64,
            )

        elapsed = round(time.time() - t0, 2)
        digest = hashlib.sha256(f"{src_mp4.name}:{op_label}:{target_stem}:{time.time()}".encode()).hexdigest()[:12]
        new_take_id = f"take_rep_{digest}"
        vault_rel = f"takes/{sid}/{out_name}__rep_{digest}.mp4"
        vault_mp4 = ctx.renders_dir / vault_rel
        vault_mp4.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_comp), str(vault_mp4))
        safe_link_or_copy(vault_mp4, active_mp4)

        strip_src = _find_strip(ctx.project_dir, sid, out_name)
        vault_rel_strip = None
        if strip_src and strip_src.is_file():
            vault_rel_strip = f"takes/{sid}/{out_name}__rep_{digest}_strip.jpg"
            safe_link_or_copy(strip_src, ctx.renders_dir / vault_rel_strip)
        if ctx.trimmed_dir.is_dir():
            for old_trim in ctx.trimmed_dir.glob(f"*_{sid}_*.mp4"):
                try:
                    old_trim.unlink()
                except OSError:
                    pass

        created_ts = time.time()
        sh_state = ctx.load_state_hashes()
        sr_info = (sh_state.get("scene_renders") or {}).get(out_name) or {}
        eff_th = (src_take.get("task_hash") if src_take else "") or (sh_state.get("task_hashes") or sh_state.get("tasks") or {}).get(out_name, "")
        eff_rs = (src_take.get("render_start") if (src_take and src_take.get("render_start") is not None) else sr_info.get("render_start", req_start))
        eff_re = (src_take.get("render_end") if (src_take and src_take.get("render_end") is not None) else sr_info.get("render_end", float(sc.get("end", 0)) + tail_pad))
        eff_fr = (src_take.get("frames") if (src_take and src_take.get("frames") is not None) else sr_info.get("frames", n_frames))
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
            "prompt": prompt or (src_take.get("prompt") if src_take else sc.get("prompt")),
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
            "resolution": f"{sw}x{sh}",
            "roi_box": list(box),
            "roi_mode": mode_norm,
            "roi_side": side_norm,
            "repair_stem": target_stem,
            "model_type": op_label,
            "backend": "local" if dummy_render else eff_backend,
            "scene_snapshot": copy.deepcopy(sc),
            "label": f"Take {len(existing_takes) + 1} (Lip-Sync {side_norm} {cw}x{ch})",
        }
        existing_takes.append(take_entry)
        sc_entry.setdefault("active_takes", {})[_pass_key(pass_num)] = new_take_id
        sc_entry["active_take_id"] = new_take_id
        save_takes_manifest(ctx.project_dir, manifest)

        write_engine_status(ctx, {"state": "idle", "backend": eff_backend, "pct": 100.0, "status_text": f"Repaired {sid} ({side_norm} {cw}x{ch})"}, backend=eff_backend, task_stem=out_name)
        print(f"[Repair] Saved take {new_take_id} ({op_label}, {cw}x{ch}) in {elapsed}s -> {active_mp4.name}", flush=True)
        return take_entry
    finally:
        for f in (crop_in, crop_mask, crop_first, crop_out, rep_task_mp4, tmp_comp, aud_mux):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def run_repair_lipsync_cli(args: list[str]) -> bool:
    """Handles `<config> repair-lipsync <scene_id> [--side right] [--roi-mode half_split] [--stem female] [--backend local] [--take-id id]`."""
    idx = next((i for i, a in enumerate(args) if a.lower() in ("repair-lipsync", "--repair-lipsync", "repair_lipsync")), -1)
    if idx < 0:
        return False
    from .cli import parse_cli_and_config
    from .wgp_bridge import SessionRouter

    rem = [a for j, a in enumerate(args) if j != idx]
    side, roi_mode, roi_box, stem, backend, take_id, prompt = "right", "half_split", None, "", "local", None, ""
    clean: list[str] = []
    i = 0
    while i < len(rem):
        tok = rem[i]
        if tok == "--side" and i + 1 < len(rem): side = rem[i + 1]; i += 2; continue
        if tok.lower().startswith("--side="): side = tok.split("=", 1)[1]; i += 1; continue
        if tok == "--roi-mode" and i + 1 < len(rem): roi_mode = rem[i + 1]; i += 2; continue
        if tok.lower().startswith("--roi-mode="): roi_mode = tok.split("=", 1)[1]; i += 1; continue
        if tok == "--roi-box" and i + 1 < len(rem): roi_box = rem[i + 1]; i += 2; continue
        if tok.lower().startswith("--roi-box="): roi_box = tok.split("=", 1)[1]; i += 1; continue
        if tok == "--stem" and i + 1 < len(rem): stem = rem[i + 1]; i += 2; continue
        if tok.lower().startswith("--stem="): stem = tok.split("=", 1)[1]; i += 1; continue
        if tok == "--take-id" and i + 1 < len(rem): take_id = rem[i + 1]; i += 2; continue
        if tok.lower().startswith("--take-id="): take_id = tok.split("=", 1)[1]; i += 1; continue
        if tok == "--prompt" and i + 1 < len(rem): prompt = rem[i + 1]; i += 2; continue
        if tok.lower().startswith("--prompt="): prompt = tok.split("=", 1)[1]; i += 1; continue
        if tok in ("--backend", "--repair-backend") and i + 1 < len(rem): backend = rem[i + 1]; i += 2; continue
        if tok.lower().startswith(("--backend=", "--repair-backend=")): backend = tok.split("=", 1)[1]; i += 1; continue
        clean.append(tok)
        i += 1
    ctx, _, force_scenes, _, _, _, _, _, _, _, dummy_render = parse_cli_and_config(clean)
    scene_tok = force_scenes[0] if force_scenes else next((t for t in clean if not t.startswith("-") and not t.lower().endswith(".json")), "")
    if not scene_tok:
        raise SystemExit("Usage: run_overnight_v3.py <config.json> repair-lipsync <scene_id> [--side right] [--roi-mode half_split] [--backend local]")
    lazy_session = SessionRouter(ctx)
    try:
        repair_scene_lipsync_clip(
            ctx, lazy_session, scene_tok,
            side=side, roi_mode=roi_mode, roi_box=roi_box,
            stem=stem, backend=backend, take_id=take_id, prompt=prompt, dummy_render=dummy_render,
        )
    finally:
        lazy_session.close()
    return True


def handle_repair_lipsync_api(handler, srv, body: dict | None) -> None:
    """POST /api/takes/repair_lipsync handler: dispatches non-blocking cropped ROI lip-sync repair."""
    from http import HTTPStatus

    body = body or {}
    slug = str(body.get("project") or body.get("slug") or "").strip()
    sid = str(body.get("scene_id") or "").strip()
    side = normalize_roi_side(body.get("side") or "right")
    roi_mode = normalize_roi_mode(body.get("roi_mode") or "half_split", default="half_split")
    roi_box = normalize_roi_box(body.get("roi_box"))
    stem = str(body.get("stem") or "").strip()
    backend = str(body.get("backend") or "local").strip().lower()
    if backend not in ("local", "fal", "minimax"):
        backend = "local"
    take_id = body.get("take_id")
    prompt = str(body.get("prompt") or "").strip()

    if not slug or not sid:
        handler.send_error_json("project and scene_id are required")
        return
    if backend == "local":
        with srv.render_lock:
            if getattr(srv, "local_adhoc_busy", False) or (not getattr(srv, "queue_worker_active", False) and srv.render_status.get("running")) or getattr(srv, "engine_workers", {}).get("local", {}).get("running"):
                handler.send_error_json("Local GPU is currently busy rendering. Stop it or pick fal.ai / MiniMax.", status=HTTPStatus.CONFLICT)
                return
            srv.local_adhoc_busy = True

    proj_dir = srv.PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        if backend == "local":
            with srv.render_lock: srv.local_adhoc_busy = False
        handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
        return
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        if backend == "local":
            with srv.render_lock: srv.local_adhoc_busy = False
        handler.send_error_json(f"No project config found in {proj_dir}", status=HTTPStatus.NOT_FOUND)
        return
    config_file = str(jsons[-1])

    cmd_args = [config_file, "repair-lipsync", sid, "--side", side, "--roi-mode", roi_mode, "--backend", backend]
    if roi_box:
        cmd_args.extend(["--roi-box", ",".join(str(v) for v in roi_box)])
    if stem:
        cmd_args.extend(["--stem", stem])
    if take_id:
        cmd_args.extend(["--take-id", str(take_id)])
    if prompt:
        cmd_args.extend(["--prompt", prompt])

    def _run():
        try:
            code = srv.execute_single_render(cmd_args, slug)
            if code == 0:
                srv.broadcast_log(f"[REPAIR_LIPSYNC_DONE] {json.dumps({'scene_id': sid, 'side': side, 'roi_mode': roi_mode, 'backend': backend})}")
            else:
                srv.broadcast_log(f"[Studio UI Error] Lip-sync repair failed for {sid} (exit code {code}).")
        finally:
            if backend == "local":
                with srv.render_lock:
                    srv.local_adhoc_busy = False
                if getattr(srv, "queue_worker_active", False) and hasattr(srv, "wake_enabled_engine_workers"):
                    srv.wake_enabled_engine_workers(slug)

    threading.Thread(target=_run, daemon=True).start()
    handler.send_json({"success": True, "started": True, "scene_id": sid, "side": side, "roi_mode": roi_mode, "backend": backend})
