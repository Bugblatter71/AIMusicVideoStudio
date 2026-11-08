import re
import shutil
import subprocess
from pathlib import Path

from .context import ProjectContext
from .media_utils import (
    probe_video_dimensions,
    probe_video_fps,
    probe_video_frames,
    resolve_rendered_clip_path,
)


def assemble_progressive_master(ctx: ProjectContext, scene_meta: list[dict], log_prefix: str = "") -> bool:
    """Assembles a full-song master video from start to finish.
    Uses rendered AI video clips where available, and loops high-res anchor portraits as storyboard
    placeholders for scenes that have not yet been rendered.
    Supports head-offset trimming for clips generated with render overlap padding.
    Safely handles Windows media player file locks.
    """
    state = ctx.load_state_hashes()
    scene_renders = state.setdefault("scene_renders", {})
    trim_cache = state.setdefault("trim_cache", {})

    target_w, target_h = None, None
    for sc in scene_meta:
        src = resolve_rendered_clip_path(ctx.renders_dir, Path(sc["clip"]).stem, promote=True)
        if src.exists():
            dims = probe_video_dimensions(ctx.ffmpeg, src)
            if dims:
                target_w, target_h = dims
                break

    master_res = str(ctx.project_cfg.get("master_resolution", "")).strip()
    if master_res and master_res.lower() not in ("", "match", "render", "auto", "default"):
        resolved_master = ctx.resolve_resolution(master_res)
        if resolved_master == "1920x1088":
            res_str = "1920:1080"
        else:
            res_str = resolved_master.replace("x", ":")
    else:
        cfg_res = ctx.current_resolution
        if cfg_res == "1920x1088":
            res_str = "1920:1080"
        else:
            res_str = cfg_res.replace("x", ":")

    res_tag = res_str.replace(":", "x")
    target_tw, target_th = [int(v) for v in res_str.split(":")]
    concat_lines = []
    last_idx = len(scene_meta) - 1
    rendered_count = 0
    placeholder_count = 0

    ctx.trimmed_dir.mkdir(parents=True, exist_ok=True)
    if ctx.generate_review_strips:
        ctx.strips_dir.mkdir(parents=True, exist_ok=True)

    for i, sc in enumerate(scene_meta):
        sid = sc["id"]
        dur = sc["dur"]
        canonical_stem = re.sub(r"\(\d+\)$", "", Path(sc["clip"]).stem)
        src_clip = resolve_rendered_clip_path(ctx.renders_dir, canonical_stem, promote=True)
        dst_clip = ctx.trimmed_dir / f"{i:02d}_{sid}_{res_tag}.mp4"
        dst_placeholder = ctx.trimmed_dir / f"{i:02d}_{sid}_placeholder_{res_tag}.mp4"

        # Lock exact frame bounds on the master fps grid (eliminates cumulative timing drift)
        start_frame = int(round(sc["start"] * ctx.fps))
        end_frame = int(round(sc["end"] * ctx.fps))
        target_frames = max(1, end_frame - start_frame)

        # Retrieve render_start for offset trimming
        render_start = sc.get("render_start")
        if render_start is None:
            r_info = scene_renders.get(canonical_stem) or scene_renders.get(src_clip.stem)
            if r_info and "render_start" in r_info:
                render_start = float(r_info["render_start"])
            else:
                render_start = float(sc["start"])
        else:
            render_start = float(render_start)

        # Head offset into the rendered clip to reach timeline edit start
        head_offset_s = max(0.0, float(sc["start"]) - render_start) + ctx.trim_first_frame_s
        trim_sig = f"{sc['start']:.3f}_{sc['end']:.3f}_{head_offset_s:.3f}_{target_frames}_{res_str}_{ctx.fps}"

        if src_clip.exists() and src_clip.stat().st_size > 50000:
            rendered_count += 1
            active_clip = dst_clip
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
                vf_filters = []
                if head_offset_s > 0.0005:
                    vf_filters.extend([
                        f"trim=start={head_offset_s:.3f}",
                        "setpts=PTS-STARTPTS",
                    ])
                vf_filters.extend([
                    "tpad=stop_mode=clone:stop=-1",
                    f"scale={res_str}:flags=lanczos",
                    f"fps={ctx.fps}",
                ])
                if i == last_idx and ctx.fade_out_last_s > 0:
                    fade_start = max(0.0, dur - ctx.fade_out_last_s)
                    vf_filters.append(f"fade=t=out:st={fade_start:.3f}:d={ctx.fade_out_last_s:.3f}")
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-i", str(src_clip),
                        "-vf", ",".join(vf_filters),
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p",
                        str(dst_clip),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                trim_cache[dst_clip.name] = trim_sig
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
            needs_encode = (
                (not dst_placeholder.exists())
                or (anchor_path.exists() and dst_placeholder.stat().st_mtime < anchor_path.stat().st_mtime)
                or (trim_cache.get(dst_placeholder.name) != trim_sig)
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
                vf_filters = [
                    f"scale={res_str}:flags=lanczos",
                    f"fps={ctx.fps}",
                ]
                if i == last_idx and ctx.fade_out_last_s > 0:
                    fade_start = max(0.0, dur - ctx.fade_out_last_s)
                    vf_filters.append(f"fade=t=out:st={fade_start:.3f}:d={ctx.fade_out_last_s:.3f}")
                if anchor_path.exists():
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-loop", "1",
                        "-i", str(anchor_path),
                        "-vf", ",".join(vf_filters),
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                else:
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-f", "lavfi",
                        "-i", f"color=c=black:s={res_str}:r={ctx.fps}",
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                trim_cache[dst_placeholder.name] = trim_sig

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

    ctx.save_state_hashes(state)

    concat_txt = ctx.trimmed_dir / "concat_list.txt"
    concat_txt.write_text("\n".join(concat_lines), encoding="utf-8")

    total_dur = float(ctx.total_duration) if ctx.total_duration else sum(s["dur"] for s in scene_meta)
    tmp_output = ctx.master_output.with_suffix(".tmp.mp4")

    subprocess.run(
        [
            str(ctx.ffmpeg), "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_txt),
            "-i", str(ctx.master_audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-r", str(ctx.fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k",
            "-t", f"{total_dur:.3f}",
            str(tmp_output),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        if ctx.master_output.exists():
            ctx.master_output.unlink()
        shutil.move(str(tmp_output), str(ctx.master_output))
        status_msg = f"{rendered_count}/{len(scene_meta)} scenes rendered"
        if placeholder_count > 0:
            status_msg += f", {placeholder_count} anchor placeholders"
        else:
            status_msg += " (100% complete)"
        print(f"{log_prefix}[Progressive Master Updated] -> {ctx.master_output.name} ({status_msg})", flush=True)
        return True
    except PermissionError:
        print(f"{log_prefix}[Notice] {ctx.master_output.name} is currently open in a player. Clips cached; will update master once player is closed.", flush=True)
        if tmp_output.exists():
            try:
                tmp_output.unlink()
            except Exception:
                pass
        return False
    except Exception as e:
        print(f"{log_prefix}[Warning] Could not update master output file: {e}", flush=True)
        return False


def run_stage3_assemble_master(ctx: ProjectContext, scene_meta: list[dict]):
    print("\n" + "=" * 72, flush=True)
    print("STAGE 3: Assembling Flash-Free Master Video & Extracting Review Strips", flush=True)
    print("=" * 72, flush=True)

    assemble_progressive_master(ctx, scene_meta, log_prefix="  ")
