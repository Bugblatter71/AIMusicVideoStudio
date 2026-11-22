import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .context import ProjectContext
from .media_utils import (
    probe_video_dimensions,
    probe_video_fps,
    probe_video_frames,
    resolve_rendered_clip_path,
)
from .prompts import resolve_master_resolution


def _emit_stitch_progress(pct: int, phase: str, detail: str = "", current: int = 0, total: int = 0) -> None:
    payload = {"pct": max(0, min(100, int(pct))), "phase": phase, "detail": detail, "current": current, "total": total}
    print(f"[MASTER_STITCH_PROGRESS] {json.dumps(payload)}", flush=True)


def build_stage3_ffmpeg_extra_filters(ffmpeg_filters: dict | None) -> tuple[list[str], list[str]]:
    """Builds optional Stage 3 FFmpeg filters: (pre_scale=[hqdn3d], post_scale=[deband, eq, cas])."""
    if not isinstance(ffmpeg_filters, dict) or not ffmpeg_filters:
        return [], []
    pre_f: list[str] = []
    post_f: list[str] = []

    denoise = float(ffmpeg_filters.get("denoise") or 0.0)
    if denoise > 0.001:
        luma = round(denoise, 2)
        chroma = round(luma * 0.75, 2)
        lt = round(luma * 1.5, 2)
        ct = round(chroma * 1.5, 2)
        pre_f.append(f"hqdn3d={luma:.2f}:{chroma:.2f}:{lt:.2f}:{ct:.2f}")

    if ffmpeg_filters.get("deband"):
        post_f.append("deband=1thr=0.02:2thr=0.02:3thr=0.02:blur=1")

    eq = ffmpeg_filters.get("eq") if isinstance(ffmpeg_filters.get("eq"), dict) else {}
    br = float(eq.get("brightness", 0.0) or 0.0)
    ct = float(eq.get("contrast", 1.0) if eq.get("contrast") is not None else 1.0)
    sa = float(eq.get("saturation", 1.0) if eq.get("saturation") is not None else 1.0)
    ga = float(eq.get("gamma", 1.0) if eq.get("gamma") is not None else 1.0)
    if abs(br) > 0.001 or abs(ct - 1.0) > 0.001 or abs(sa - 1.0) > 0.001 or abs(ga - 1.0) > 0.001:
        post_f.append(f"eq=brightness={br:.3f}:contrast={ct:.3f}:saturation={sa:.3f}:gamma={ga:.3f}")

    cas = float(ffmpeg_filters.get("cas") or 0.0)
    if cas > 0.001:
        post_f.append(f"cas=strength={cas:.2f}")

    return pre_f, post_f


def build_stage3_scale_filters(
    res_str: str,
    scale_algo: str = "lanczos",
    sharpen: float = 0.0,
    preserve_aspect: bool = True,
    src_dims: tuple[int, int] | None = None,
    crop_align: str = "center",
    ffmpeg_filters: dict | None = None,
) -> list[str]:
    """Builds FFmpeg filter chain for scaling/cropping and Stage 3 polish without degrading native pixels.

    - Exact match (src_w == w and src_h == h): skips scale, crop, and unsharp; applies pre/post extra filters if set.
    - Same-width vertical trim (src_w == w and src_h > h, e.g. 1920x1088 -> 1920x1080):
      pure center crop (4px top + 4px bottom) with zero resampling and no unsharp halos.
    - Source >= target in both dimensions: downscales smoothly without sharpening halos.
    - Source < target in either dimension: applies aspect-safe upscale + crop + optional unsharp.
    """
    w, h = [int(v) for v in res_str.split(":")]
    algo = scale_algo if scale_algo in ("lanczos", "bicubic", "spline", "bilinear", "neighbor") else "lanczos"
    pre_f, post_f = build_stage3_ffmpeg_extra_filters(ffmpeg_filters)
    filters = [*pre_f]
    did_upscale = src_dims is None

    if src_dims is not None:
        sw, sh = int(src_dims[0]), int(src_dims[1])
        if sw == w and sh == h:
            return [*filters, *post_f, "setsar=1:1"]
        if sw == w and sh > h and preserve_aspect:
            extra_h = sh - h
            y_off = 0 if crop_align == "top" else (extra_h if crop_align == "bottom" else extra_h // 2)
            return [*filters, f"crop={w}:{h}:0:{y_off}", *post_f, "setsar=1:1"]
        did_upscale = (sw < w) or (sh < h)

    if preserve_aspect:
        filters.append(f"scale={w}:{h}:force_original_aspect_ratio=increase:flags={algo}")
        filters.append(f"crop={w}:{h}")
    else:
        filters.append(f"scale={w}:{h}:flags={algo}")
    filters.extend(post_f)
    if did_upscale and sharpen > 0.001:
        luma_amt = round(min(1.5, max(0.05, float(sharpen) * 1.2)), 2)
        filters.append(f"unsharp=5:5:{luma_amt}:5:5:0.0")
    filters.append("setsar=1:1")
    return filters


def resolve_scene_fade_filters(
    fade_dur: float,
    fade_end_s: float | None,
    sc_start: float,
    sc_end: float,
    timeline_end_s: float,
) -> tuple[str, list[str]]:
    """Computes (cache_sig, vf_filters) for a scene overlapping the master fade-out window."""
    dur_val = max(0.0, float(fade_dur or 0.0))
    end_limit = max(0.0, float(timeline_end_s or 0.0))
    explicit_end = fade_end_s is not None and str(fade_end_s).strip() != ""
    fade_end_t = max(0.0, min(end_limit, float(fade_end_s))) if explicit_end else end_limit
    fade_start_t = max(0.0, fade_end_t - dur_val)
    active = dur_val > 0.0005 or (explicit_end and fade_end_t < end_limit - 0.0005)
    if (not active) or sc_end <= fade_start_t + 0.0005:
        return "nofade", []
    if sc_start >= fade_end_t - 0.0005:
        return "black", ["drawbox=c=black:t=fill"]
    eff_d = max(0.001, fade_end_t - fade_start_t)
    if fade_start_t >= sc_start - 0.0005:
        rel_st = max(0.0, fade_start_t - sc_start)
        return f"fade{rel_st:.3f}_{eff_d:.3f}", [f"fade=t=out:st={rel_st:.3f}:d={eff_d:.3f}"]
    pre_off = sc_start - fade_start_t
    return (
        f"fadecont{pre_off:.3f}_{eff_d:.3f}",
        [f"setpts=PTS-STARTPTS+{pre_off:.6f}/TB", f"fade=t=out:st=0.000:d={eff_d:.3f}", "setpts=PTS-STARTPTS"],
    )


def assemble_progressive_master(
    ctx: ProjectContext,
    scene_meta: list[dict],
    log_prefix: str = "",
    progressive: bool = False,
) -> bool:
    """Assembles a full-song master video from start to finish.
    Uses rendered AI video clips where available, and loops high-res anchor portraits as storyboard
    placeholders for scenes that have not yet been rendered.
    Supports head-offset trimming for clips generated with render overlap padding.
    Safely handles Windows media player file locks.
    """
    state = ctx.load_state_hashes()
    scene_renders = state.setdefault("scene_renders", {})
    trim_cache = state.setdefault("trim_cache", {})

    ctx.update_master_output()
    resolved_master = resolve_master_resolution(ctx, progressive=progressive)
    res_str = resolved_master.replace("x", ":")
    scale_algo = str(ctx.project_cfg.get("master_scale_algorithm") or "lanczos").strip().lower()
    if scale_algo not in ("lanczos", "bicubic", "spline", "bilinear", "neighbor"):
        scale_algo = "lanczos"
    try:
        sharpen = max(0.0, min(1.0, float(ctx.project_cfg.get("master_sharpen") or 0.0)))
    except (ValueError, TypeError):
        sharpen = 0.0

    res_tag = res_str.replace(":", "x")
    target_tw, target_th = [int(v) for v in res_str.split(":")]
    scale_filters = build_stage3_scale_filters(res_str, scale_algo=scale_algo, sharpen=sharpen, preserve_aspect=True)
    concat_lines = []
    last_idx = len(scene_meta) - 1
    rendered_count = 0
    placeholder_count = 0

    ctx.trimmed_dir.mkdir(parents=True, exist_ok=True)
    if ctx.generate_review_strips:
        ctx.strips_dir.mkdir(parents=True, exist_ok=True)

    project_scenes_by_id = {
        s["id"]: s for s in getattr(ctx, "scenes", [])
        if isinstance(s, dict) and "id" in s
    }

    total_scenes = max(1, len(scene_meta))
    last_sc_end = max(
        (float(project_scenes_by_id.get(s["id"], {}).get("end", s.get("end", 0.0))) for s in scene_meta),
        default=0.0,
    )
    timeline_end_s = (
        min(last_sc_end, float(ctx.total_duration))
        if (last_sc_end > 0 and ctx.total_duration)
        else (last_sc_end or float(ctx.total_duration or 0.0))
    )
    raw_fade_end = getattr(
        ctx,
        "fade_out_end_s",
        ctx.project_cfg.get("fade_out_end_seconds") if isinstance(getattr(ctx, "project_cfg", None), dict) else None,
    )
    _emit_stitch_progress(2, "Preparing scenes", f"Checking {len(scene_meta)} scenes for {res_tag} master...", 0, total_scenes)

    for i, sc in enumerate(scene_meta):
        sid = sc["id"]
        curr_sc = project_scenes_by_id.get(sid)
        sc_start = float(curr_sc["start"]) if (curr_sc and "start" in curr_sc) else float(sc["start"])
        sc_end = float(curr_sc["end"]) if (curr_sc and "end" in curr_sc) else float(sc["end"])
        sc_dur = max(0.001, round(sc_end - sc_start, 3))
        dur = sc_dur
        canonical_stem = re.sub(r"\(\d+\)$", "", Path(sc["clip"]).stem)
        src_clip = resolve_rendered_clip_path(ctx.renders_dir, canonical_stem, promote=True)
        dst_clip = ctx.trimmed_dir / f"{i:02d}_{sid}_{res_tag}.mp4"
        dst_placeholder = ctx.trimmed_dir / f"{i:02d}_{sid}_placeholder_{res_tag}.mp4"

        # Lock exact frame bounds on the master fps grid (eliminates cumulative timing drift)
        start_frame = int(round(sc_start * ctx.fps))
        end_frame = int(round(sc_end * ctx.fps))
        target_frames = max(1, end_frame - start_frame)

        # Retrieve render_start for offset trimming (prioritizing physical render info for existing clips)
        r_info = scene_renders.get(canonical_stem) or scene_renders.get(src_clip.stem)
        if src_clip.exists() and r_info and "render_start" in r_info:
            render_start = float(r_info["render_start"])
        elif sc.get("render_start") is not None:
            render_start = float(sc["render_start"])
        elif r_info and "render_start" in r_info:
            render_start = float(r_info["render_start"])
        else:
            render_start = sc_start

        # Head offset into the rendered clip to reach timeline edit start
        head_offset_s = max(0.0, sc_start - render_start) + ctx.trim_first_frame_s
        src_dims = probe_video_dimensions(ctx.ffmpeg, src_clip) if (src_clip.exists() and src_clip.stat().st_size > 50000) else None
        dims_tag = f"{src_dims[0]}x{src_dims[1]}" if src_dims else "unk"
        is_two_pass = str((curr_sc or sc).get("mode") or "").lower() in ("duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right")
        sc_filters = ctx.resolve_scene_filters(curr_sc or sc, pass_num=2 if is_two_pass else None) if hasattr(ctx, "resolve_scene_filters") else {}
        ff_cfg = sc_filters.get("ffmpeg") or {}
        ff_pre, ff_post = build_stage3_ffmpeg_extra_filters(ff_cfg)
        ff_sig = "|".join([*ff_pre, *ff_post])
        fade_sig, fade_filters = resolve_scene_fade_filters(
            ctx.fade_out_last_s, raw_fade_end, sc_start, sc_end, timeline_end_s
        )
        trim_sig = f"{sc_start:.3f}_{sc_end:.3f}_{head_offset_s:.3f}_{target_frames}_{res_str}_{scale_algo}_{sharpen:.2f}_{ctx.fps}_{dims_tag}_tv2_center_{fade_sig}"
        if ff_sig:
            trim_sig = f"{trim_sig}_{ff_sig}"

        if src_clip.exists() and src_clip.stat().st_size > 50000:
            rendered_count += 1
            active_clip = dst_clip
            clip_scale_filters = build_stage3_scale_filters(
                res_str, scale_algo=scale_algo, sharpen=sharpen, preserve_aspect=True,
                src_dims=src_dims, crop_align="center", ffmpeg_filters=ff_cfg,
            )
            needs_encode = (
                (not dst_clip.exists())
                or (dst_clip.stat().st_mtime < src_clip.stat().st_mtime)
                or (trim_cache.get(dst_clip.name) != trim_sig)
            )
            if not needs_encode and dst_clip.exists():
                clip_fps = probe_video_fps(ctx.ffmpeg, dst_clip)
                if clip_fps is None or abs(clip_fps - ctx.fps) > 0.1:
                    needs_encode = True
                else:
                    dims = probe_video_dimensions(ctx.ffmpeg, dst_clip)
                    if dims != (target_tw, target_th):
                        needs_encode = True
                    else:
                        cframes = probe_video_frames(ctx.ffmpeg, dst_clip)
                        if cframes is not None and cframes != target_frames:
                            needs_encode = True
            if needs_encode:
                _emit_stitch_progress(5 + int((i / total_scenes) * 70), f"Trimming & scaling scene {i + 1}/{total_scenes} ({sid})", f"Encoding {dst_clip.name}", i + 1, total_scenes)
                vf_filters = []
                if head_offset_s > 0.0005:
                    vf_filters.extend([
                        f"trim=start={head_offset_s:.3f}",
                        "setpts=PTS-STARTPTS",
                    ])
                vf_filters.extend([
                    "tpad=stop_mode=clone:stop=-1",
                    *clip_scale_filters,
                    f"fps={ctx.fps}",
                    "scale=out_range=tv",
                    "format=yuv420p",
                    *fade_filters,
                ])
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-i", str(src_clip),
                        "-vf", ",".join(vf_filters),
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-color_range", "tv", "-pix_fmt", "yuv420p",
                        str(dst_clip),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                trim_cache[dst_clip.name] = trim_sig
            elif i == 0 or i == last_idx or (i + 1) % 4 == 0:
                _emit_stitch_progress(5 + int(((i + 1) / total_scenes) * 70), f"Verifying cached scenes ({i + 1}/{total_scenes})", f"Scene {sid} up-to-date", i + 1, total_scenes)
            if dst_placeholder.exists():
                try:
                    dst_placeholder.unlink()
                except Exception:
                    pass
        else:
            placeholder_count += 1
            active_clip = dst_placeholder
            anchor_fn = sc.get("anchor")
            aid, anchor_path = ctx.resolve_anchor_path(anchor_fn)
            ph_sig = f"{trim_sig}_{'anch1' if anchor_path.exists() else 'anch0'}"
            needs_encode = (
                (not dst_placeholder.exists())
                or (anchor_path.exists() and dst_placeholder.stat().st_mtime < anchor_path.stat().st_mtime)
                or (trim_cache.get(dst_placeholder.name) != ph_sig)
            )
            if not needs_encode and dst_placeholder.exists():
                ph_fps = probe_video_fps(ctx.ffmpeg, dst_placeholder)
                if ph_fps is None or abs(ph_fps - ctx.fps) > 0.1:
                    needs_encode = True
                else:
                    dims = probe_video_dimensions(ctx.ffmpeg, dst_placeholder)
                    if dims != (target_tw, target_th):
                        needs_encode = True
                    else:
                        cframes = probe_video_frames(ctx.ffmpeg, dst_placeholder)
                        if cframes is not None and cframes != target_frames:
                            needs_encode = True
            if needs_encode:
                _emit_stitch_progress(5 + int((i / total_scenes) * 70), f"Building placeholder {i + 1}/{total_scenes} ({sid})", f"Encoding {dst_placeholder.name}", i + 1, total_scenes)
                ph_scale_filters = build_stage3_scale_filters(res_str, scale_algo=scale_algo, sharpen=sharpen, preserve_aspect=True, ffmpeg_filters=ff_cfg) if ff_cfg else scale_filters
                vf_filters = [
                    *ph_scale_filters,
                    f"fps={ctx.fps}",
                    "scale=out_range=tv",
                    "format=yuv420p",
                    *fade_filters,
                ]
                if anchor_path.exists():
                    ph_vf = ["tpad=stop_mode=clone:stop=-1", *vf_filters]
                    cmd = [
                        str(ctx.ffmpeg), "-nostdin", "-y",
                        "-i", str(anchor_path),
                        "-vf", ",".join(ph_vf),
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-color_range", "tv", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                else:
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-f", "lavfi",
                        "-i", f"color=c=black:s={res_tag}:r={ctx.fps}",
                        "-vf", "format=yuv420p",
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-color_range", "tv", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                trim_cache[dst_placeholder.name] = ph_sig

        concat_lines.append(f"file '{active_clip.as_posix()}'")

        if ctx.generate_review_strips:
            strip_jpg = ctx.strips_dir / f"review_{sid}.jpg"
            n_thumbs = max(2, min(30, round(dur / 1.0)))
            r = n_thumbs / max(0.5, dur)
            if not strip_jpg.exists() or (src_clip.exists() and strip_jpg.stat().st_mtime < src_clip.stat().st_mtime) or needs_encode:
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-i", str(active_clip),
                        "-vf", f"fps={r:.4f},scale=-1:144,tile={n_thumbs}x1",
                        "-frames:v", "1", "-q:v", "2", str(strip_jpg),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

    ctx.save_state_hashes(state, updated_sections={"trim_cache"})

    concat_txt = ctx.trimmed_dir / "concat_list.txt"
    concat_txt.write_text("\n".join(concat_lines), encoding="utf-8")

    total_dur = (
        float(ctx.total_duration)
        if ctx.total_duration
        else sum(
            float(project_scenes_by_id.get(s["id"], {}).get("end", s.get("end", 0.0)))
            - float(project_scenes_by_id.get(s["id"], {}).get("start", s.get("start", 0.0)))
            for s in scene_meta
        )
    )
    tmp_output = ctx.master_output.with_suffix(".tmp.mp4")

    _emit_stitch_progress(82, "Muxing master video & audio", f"Concatenating {len(scene_meta)} scenes ({rendered_count} clips, {placeholder_count} placeholders) at {res_tag}...", total_scenes, total_scenes)

    subprocess.run(
        [
            str(ctx.ffmpeg), "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_txt),
            "-i", str(ctx.master_audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-r", str(ctx.fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-color_range", "tv", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k",
            "-t", f"{total_dur:.3f}",
            str(tmp_output),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    _emit_stitch_progress(96, "Finalizing master MP4", f"Writing {ctx.master_output.name}...", total_scenes, total_scenes)

    try:
        for attempt in range(6):
            try:
                if ctx.master_output.exists():
                    ctx.master_output.unlink()
                shutil.move(str(tmp_output), str(ctx.master_output))
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.15)
        status_msg = f"{rendered_count}/{len(scene_meta)} scenes rendered"
        if placeholder_count > 0:
            status_msg += f", {placeholder_count} anchor placeholders"
        else:
            status_msg += " (100% complete)"
        _emit_stitch_progress(100, "Complete", f"{ctx.master_output.name} ({status_msg})", total_scenes, total_scenes)
        print(f"{log_prefix}[Progressive Master Updated] -> {ctx.master_output.name} ({status_msg})", flush=True)
        return True
    except PermissionError:
        print(f"{log_prefix}[Error] [MASTER_FILE_LOCKED] {ctx.master_output.name} is currently open in a player or another program. Close the player to update the master video.", flush=True)
        if tmp_output.exists():
            try:
                tmp_output.unlink()
            except Exception:
                pass
        return False
    except Exception as e:
        print(f"{log_prefix}[Error] [MASTER_STITCH_FAILED] Could not update master output file: {e}", flush=True)
        return False


def run_stage3_assemble_master(ctx: ProjectContext, scene_meta: list[dict]) -> bool:
    print("\n" + "=" * 72, flush=True)
    print("STAGE 3: Assembling Flash-Free Master Video & Extracting Review Strips", flush=True)
    print("=" * 72, flush=True)

    return assemble_progressive_master(ctx, scene_meta, log_prefix="  ")
