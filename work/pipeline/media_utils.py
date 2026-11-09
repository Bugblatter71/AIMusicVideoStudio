import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent


def _sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# Task keys that are NOT render settings (tracked elsewhere: prompt/seed in the task hash,
# input files via anchor hash / pass-1 cascade, and timing/length via coverage checks).
# Everything else in a video task is a render setting (resolution, model, steps, solver, flow_shift, custom_settings, ...).
_NON_SETTING_TASK_KEYS = {
    "prompt", "seed", "output_filename", "image_start", "image_end", "audio_guide", "video_guide", "video_mask",
    "video_length", "render_start", "render_end",
}
# Keys compared against the settings JSON that Wan2GP embeds in each rendered MP4
# (used for clips rendered before render-settings tracking existed).
_EMBEDDED_CHECK_KEYS = ("model_type", "resolution", "force_fps", "sample_solver", "guidance_phases", "flow_shift", "spatial_upsampling", "config")


def render_settings_of(task: dict) -> dict:
    return {k: v for k, v in task.items() if k not in _NON_SETTING_TASK_KEYS}


def _norm_setting(v) -> str:
    s = str(v).strip().lower()
    try:
        return repr(float(s))
    except ValueError:
        return s


def read_embedded_render_settings(ffmpeg: Path, mp4: Path) -> dict | None:
    """Returns the generation settings JSON Wan2GP stores in the MP4 comment tag, or None."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [str(ffprobe), "-v", "quiet", "-print_format", "json", "-show_format", str(mp4)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        comment = json.loads(out.stdout).get("format", {}).get("tags", {}).get("comment", "")
        return json.loads(comment) if comment.strip().startswith("{") else None
    except Exception:
        return None


def probe_video_fps(ffmpeg: Path, mp4: Path) -> float | None:
    """Probes the average framerate of an MP4 file using ffprobe."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [
                str(ffprobe), "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", str(mp4),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        data = json.loads(out.stdout)
        streams = data.get("streams", [])
        if streams:
            rate_str = streams[0].get("avg_frame_rate") or streams[0].get("r_frame_rate", "")
            if "/" in rate_str:
                num, den = rate_str.split("/")
                if float(den) > 0:
                    return float(num) / float(den)
            elif rate_str:
                return float(rate_str)
    except Exception:
        pass
    return None


def probe_video_dimensions(ffmpeg: Path, mp4: Path) -> tuple[int, int] | None:
    """Probes the width and height of an MP4 file using ffprobe."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [
                str(ffprobe), "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                str(mp4),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        parts = out.stdout.strip().split("x")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None


def probe_video_duration(ffmpeg: Path, mp4: Path) -> float | None:
    """Probes the total duration in seconds of an MP4 file."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [
                str(ffprobe), "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(mp4),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        val = out.stdout.strip()
        return float(val) if val else None
    except Exception:
        return None


def probe_video_frames(ffmpeg: Path, mp4: Path) -> int | None:
    """Probes the total video frame count of an MP4 file."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [
                str(ffprobe), "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=nb_frames",
                "-of", "default=noprint_wrappers=1:nokey=1", str(mp4),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        val = out.stdout.strip()
        return int(val) if val.isdigit() else None
    except Exception:
        return None


def resolve_rendered_clip_path(renders_dir: Path, out_name: str, promote: bool = False) -> Path:
    """Finds the active MP4 file for a given out_name.
    If numbered duplicates exist (e.g. out_name(2).mp4 created by Wan2GP due to file locks),
    returns the newest valid duplicate. When promote=True, attempts to reconcile/promote
    the newest duplicate back to canonical out_name.mp4 if writable.
    """
    renders_dir = Path(renders_dir)
    base_name = re.sub(r"\(\d+\)$", "", Path(out_name).stem)
    canonical = renders_dir / f"{base_name}.mp4"
    if not renders_dir.exists():
        return canonical

    candidates = []
    pattern = f"{base_name}*.mp4"
    for p in renders_dir.glob(pattern):
        if p.name.endswith(".tmp.mp4") or p.name.endswith(".tmp_promote.mp4") or "placeholder" in p.name.lower():
            continue
        stem = p.stem
        if stem == base_name or re.match(rf"^{re.escape(base_name)}\(\d+\)$", stem):
            try:
                sz = p.stat().st_size
                if sz > 50000:
                    candidates.append((p.stat().st_mtime, sz, p))
            except Exception:
                pass

    if not candidates:
        return canonical

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_path = candidates[0][2]

    if promote and best_path != canonical and best_path.exists():
        tmp_canon = renders_dir / f"{base_name}.tmp_promote.mp4"
        try:
            shutil.copy2(str(best_path), str(tmp_canon))
            os.replace(str(tmp_canon), str(canonical))
            best_path.unlink(missing_ok=True)
            return canonical
        except Exception:
            try:
                tmp_canon.unlink(missing_ok=True)
            except Exception:
                pass

    return best_path if best_path.exists() else canonical


def check_scene_render_coverage(
    ffmpeg: Path,
    out_mp4: Path,
    scene_renders: dict,
    out_name: str,
    start: float,
    end: float,
) -> tuple[bool, float | None, float | None]:
    """
    Checks if an existing rendered mp4 clip fully covers the required [start, end] timeline window.
    Returns (is_covered, rendered_start, rendered_end).
    """
    if not (out_mp4.exists() and out_mp4.stat().st_size > 50000):
        return False, None, None

    render_info = scene_renders.get(out_name)
    if render_info and "render_start" in render_info and "render_end" in render_info:
        r_start = float(render_info["render_start"])
        r_end = float(render_info["render_end"])
    else:
        # Legacy fallback or newly discovered clip on disk: probe duration
        dur = probe_video_duration(ffmpeg, out_mp4)
        frames = probe_video_frames(ffmpeg, out_mp4)
        if dur is not None and dur > 0:
            r_start = float(start)
            r_end = float(start) + float(dur)
            scene_renders[out_name] = {
                "render_start": r_start,
                "render_end": r_end,
                "frames": frames,
                "duration": dur,
            }
        else:
            return False, None, None

    # Fully covered if rendered clip starts at or before timeline start and ends at or after timeline end
    # Uses a tiny 50ms tolerance for numerical/frame-rate quantization
    is_covered = (r_start <= (start + 0.05)) and (r_end >= (end - 0.05))
    return is_covered, r_start, r_end


def find_local_config(root_dir: Path) -> dict:
    """Searches for config.local.json or local_config.json in project or workspace root."""
    for d in (root_dir, root_dir.parent, Path.cwd(), WORKSPACE_ROOT):
        for fn in ("config.local.json", "local_config.json"):
            p = d / fn
            if p.is_file():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
    return {}


def auto_detect_wgp_root() -> Path | None:
    """Auto-detects Wan2GP app installation under Pinokio across standard locations."""
    wgp_env = os.environ.get("WGP_ROOT")
    if wgp_env and Path(wgp_env).is_dir():
        return Path(wgp_env).resolve()

    pinokio_home = os.environ.get("PINOKIO_HOME") or os.environ.get("PINOKIO_ROOT")
    candidates = []
    if pinokio_home:
        candidates.append(Path(pinokio_home) / "api" / "wan.git" / "app")

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        up = Path(user_profile)
        candidates.extend([
            up / "pinokio" / "api" / "wan.git" / "app",
            up / "Downloads" / "pinokio" / "api" / "wan.git" / "app",
            up / "Documents" / "pinokio" / "api" / "wan.git" / "app",
        ])
    for drive in ("C", "D", "E", "F"):
        candidates.append(Path(f"{drive}:/pinokio/api/wan.git/app"))
        candidates.append(Path(f"{drive}:/Pinokio/api/wan.git/app"))

    for c in candidates:
        try:
            if c.is_dir() and ((c / "wgp_config.json").exists() or (c / "models").exists()):
                return c.resolve()
        except Exception:
            pass
    return None


def resolve_path(base_dir: Path, p: str | Path) -> Path:
    path_obj = Path(p)
    if path_obj.is_absolute():
        return path_obj
    cand = (base_dir / path_obj).resolve()
    if cand.exists():
        return cand
    ws_cand = (WORKSPACE_ROOT / path_obj).resolve()
    if ws_cand.exists():
        return ws_cand
    audio_cand = (base_dir / "audio" / path_obj).resolve()
    if audio_cand.exists():
        return audio_cand
    return cand


def resolve_ffmpeg_path(project_dir: Path, configured_val: str, wgp_root: Path | None) -> Path:
    """Finds a working FFmpeg executable across project tools, Wan2GP, Pinokio, or system PATH."""
    if configured_val:
        cand = resolve_path(project_dir, configured_val)
        if cand.is_file():
            return cand
    local_tool = project_dir / "tools" / "ffmpeg.exe"
    if local_tool.is_file():
        return local_tool
    workspace_tool = WORKSPACE_ROOT / "tools" / "ffmpeg.exe"
    if workspace_tool.is_file():
        return workspace_tool
    if wgp_root and Path(wgp_root).is_dir():
        wgp_ff = Path(wgp_root) / "ffmpeg_bins" / "ffmpeg.exe"
        if wgp_ff.is_file():
            return wgp_ff
        try:
            pin_ff = Path(wgp_root).parent.parent.parent / "bin" / "ffmpeg.exe"
            if pin_ff.is_file():
                return pin_ff
        except Exception:
            pass
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return Path(sys_ff)
    return Path("ffmpeg")


def settings_change_reason(ffmpeg: Path, settings_store: dict, out_name: str, task: dict, out_mp4: Path, file_exists: bool) -> str | None:
    """Returns a human-readable reason if the clip's render settings differ from the last render, else None."""
    cur = render_settings_of(task)
    prev = settings_store.get(out_name)
    if prev is not None:
        prev_clean = {k: v for k, v in prev.items() if k not in _NON_SETTING_TASK_KEYS}
        diffs = [k for k in sorted(set(prev_clean) | set(cur)) if prev_clean.get(k) != cur.get(k)]
        prev = prev_clean
    elif file_exists:
        meta = read_embedded_render_settings(ffmpeg, out_mp4)
        if not meta:
            return None
        diffs = [
            k for k in _EMBEDDED_CHECK_KEYS
            if k in cur and k in meta and _norm_setting(meta[k]) != _norm_setting(cur[k])
        ]
        prev = meta
    else:
        return None
    if not diffs:
        return None
    shown = ", ".join(f"{k} {prev.get(k)!r}->{cur.get(k)!r}" for k in diffs[:3])
    more = f" (+{len(diffs) - 3} more)" if len(diffs) > 3 else ""
    return f"RENDER SETTINGS CHANGED: {shown}{more}"


def format_duration_estimate(seconds: float) -> str:
    """Formats an estimated duration in seconds into a concise human-readable string (e.g. '12.4m', '1h 33m')."""
    s = max(0.0, float(seconds or 0.0))
    if s < 60.0:
        return f"{int(round(s))}s"
    if s < 3600.0:
        mins = round(s / 60.0, 1)
        return f"{int(round(mins))}m" if (mins >= 20.0 or mins == int(mins)) else f"{mins:.1f}m"
    h = int(s // 3600)
    m = int(round((s % 3600) / 60.0))
    if m == 60:
        h += 1
        m = 0
    return f"{h}h {m:02d}m" if m > 0 else f"{h}h"


def quantize_video_frames(
    min_seconds: float,
    frames_formula: str = "h3",
    fps: float = 24.0,
    max_frames: int | None = None,
) -> tuple[int, float]:
    """Quantizes a duration (in seconds) to valid model video frames and returns (frames, next_quantum_max_sec)."""
    formula = str(frames_formula or "h3").strip().lower()
    fps_val = float(fps or 24.0)
    needed = math.ceil(max(0.0, float(min_seconds or 0.0)) * fps_val) + 2
    if formula == "ltx2":
        needed = max(17, needed)
        k = math.ceil((needed - 1) / 8)
        frames = 8 * k + 1
    else:
        min_f = 124 if formula == "h3_ref2va" else 107
        needed = max(min_f, needed)
        k = math.ceil((needed - 5) / 17)
        frames = 17 * k + 5
    if max_frames:
        frames = min(int(max_frames), frames)
    max_sec = round((frames - 2) / fps_val, 2)
    return frames, max_sec


def estimate_pass_render_time(
    frames: int,
    resolution: str = "1344x768",
    model_id: str | int = "1",
    model_type: str = "minimax_h3_fl2va_pruned_pdd",
    pass_num: int | None = None,
    has_end_anchor: bool = False,
    num_steps: int = 8,
    sliding_window_size: int = 362,
    sliding_window_overlap: int = 18,
    calibration_alpha: float = 1.0,
    next_quantum_max_sec: float | None = None,
) -> dict:
    """Computes non-linear estimated render time for a single render pass (Solo, Pass 1, or Pass 2)."""
    f = max(1, int(frames or 141))
    m_str = str(model_id if model_id is not None else "1").strip()
    mtype_lower = str(model_type or "").lower()
    is_ref2va = (
        m_str in ("9", "10", "11", "12", "13")
        or "ref2va" in mtype_lower
        or "fasth3" in mtype_lower
        or "singularity" in mtype_lower
    )

    delta_f = 0
    if has_end_anchor and pass_num != 2:
        delta_f += 7
    if is_ref2va:
        delta_f += 14
        if pass_num == 2:
            delta_f += 12
    elif pass_num == 2:
        delta_f -= 10

    w_size = max(17, int(sliding_window_size or 362))
    w_ov = max(0, min(w_size - 1, int(sliding_window_overlap or 18)))
    hop = max(1, w_size - w_ov)

    if f <= w_size:
        windows = [f]
    else:
        n_win = 1 + math.ceil((f - w_size) / hop)
        rem = f
        windows = []
        for _ in range(n_win):
            cur = min(rem, w_size)
            windows.append(cur)
            rem -= hop
            if rem <= 0:
                break

    if f <= 226:
        vram_pager_mult = 1.0
        regime = "fast_vram"
    elif len(windows) == 1:
        ramp = min(1.0, (f - 226.0) / (345.0 - 226.0))
        vram_pager_mult = 1.0 + 1.40 * ramp
        regime = "vram_spill"
    else:
        vram_pager_mult = 2.76
        regime = "split_window"

    base_sum = 0.0
    for w_f in windows:
        f_eff = max(1.0, float(w_f + delta_f))
        base_sum += (2.29 * f_eff + 0.01224 * (f_eff ** 2)) * vram_pager_mult

    m_res = 1.0
    res_clean = str(resolution or "1344x768").strip().lower()
    if "x" in res_clean:
        try:
            parts = res_clean.split("x")
            w_px, h_px = int(parts[0]), int(parts[1].split()[0])
            if w_px > 0 and h_px > 0:
                m_res = ((w_px * h_px) / (1344.0 * 768.0)) ** 1.15
        except Exception:
            pass

    m_steps = max(1.0, float(num_steps or 8)) / 8.0
    alpha = max(0.5, min(2.5, float(calibration_alpha or 1.0)))
    est_sec = round(base_sum * m_res * m_steps * alpha, 1)

    return {
        "estimated_seconds": est_sec,
        "estimated_str": format_duration_estimate(est_sec),
        "frames": f,
        "window_count": len(windows),
        "regime": regime,
        "is_split_window": f > 226,
        "rate_per_frame": round(est_sec / f, 2),
        "next_quantum_max_sec": next_quantum_max_sec if next_quantum_max_sec is not None else round((f - 2) / 24.0, 2),
    }


def compute_calibration_factor(proj_dir: Path | str | None) -> float:
    """Computes an empirical median calibration multiplier alpha from completed takes in takes_manifest.json."""
    if not proj_dir:
        return 1.0
    manifest_path = Path(proj_dir) / "work" / "takes_manifest.json"
    if not manifest_path.is_file():
        return 1.0
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        takes = [
            t for sc in data.get("scenes", {}).values()
            for t in sc.get("takes", [])
            if float(t.get("elapsed_s") or 0.0) > 30.0 and int(t.get("frames") or 0) > 0
        ]
        if len(takes) < 3:
            return 1.0
        takes.sort(key=lambda x: float(x.get("created_at") or 0.0), reverse=True)
        ratios = []
        for t in takes[:10]:
            snap = t.get("scene_snapshot") or {}
            rset = t.get("render_settings") or {}
            has_end = bool(snap.get("anchor_end") or rset.get("image_end"))
            m_id = t.get("model_id") or snap.get("renderer") or "1"
            pred = estimate_pass_render_time(
                frames=int(t["frames"]),
                resolution=str(t.get("resolution") or rset.get("resolution") or "1344x768"),
                model_id=m_id,
                model_type=str(t.get("model_type") or rset.get("model_type") or ""),
                pass_num=t.get("pass_num"),
                has_end_anchor=has_end,
                num_steps=int(rset.get("num_inference_steps") or 8),
                sliding_window_size=int(rset.get("sliding_window_size") or 362),
                sliding_window_overlap=int(rset.get("sliding_window_overlap") or 18),
                calibration_alpha=1.0,
            )["estimated_seconds"]
            if pred > 0:
                ratios.append(float(t["elapsed_s"]) / pred)
        if not ratios:
            return 1.0
        ratios.sort()
        mid = len(ratios) // 2
        med = ratios[mid] if len(ratios) % 2 == 1 else 0.5 * (ratios[mid - 1] + ratios[mid])
        return round(max(0.5, min(2.5, med)), 3)
    except Exception:
        return 1.0


def estimate_scene_render_time(
    sc: dict,
    ctx=None,
    resolution: str = "1344x768",
    calibration_alpha: float = 1.0,
) -> dict:
    """Computes total estimated render time across all passes of a scene, accounting for padding handles and quantums."""
    from .context import resolve_scene_padding

    start_s = float(sc.get("start", 0.0) or 0.0)
    end_s = float(sc.get("end", 0.0) or 0.0)
    if ctx is not None and hasattr(ctx, "get_scene_render_padding"):
        head_pad, tail_pad = ctx.get_scene_render_padding(sc)
    else:
        head_pad, tail_pad = resolve_scene_padding(sc)
    eff_start = max(0.0, start_s - head_pad)
    eff_end = end_s + tail_pad
    eff_dur = max(0.1, eff_end - eff_start)

    mode = str(sc.get("mode") or "solo").strip()
    is_duet = mode in ("duet_method_b", "two_pass_gva", "gva_male_left_silent_right")
    has_end = bool(sc.get("anchor_end"))

    passes = [1, 2] if is_duet else [None]
    pass_estimates = []
    for p_num in passes:
        if ctx is not None and hasattr(ctx, "resolve_video_renderer"):
            vrid, vrspec = ctx.resolve_video_renderer(sc, pass_num=p_num)
            frames = ctx.valid_video_frames(eff_dur, vrspec)
            fps_val = ctx.model_fps(vrspec)
            max_sec = round((frames - 2) / fps_val, 2)
            res_str = ctx.resolve_resolution(vrspec.get("resolution", ctx.current_resolution))
            mtype = vrspec.get("model_type", "minimax_h3_fl2va_pruned_pdd")
            steps = int(vrspec.get("num_inference_steps", 8))
            w_size = int(vrspec.get("sliding_window_size", 362))
            w_ov = int(vrspec.get("sliding_window_overlap", 18))
        else:
            raw_r = sc.get("renderer_m" if p_num == 1 else ("renderer_f" if p_num == 2 else "renderer")) or sc.get("renderer") or "1"
            vrid = str(raw_r).strip() if str(raw_r).strip().lower() not in ("", "0", "none", "null", "default") else "1"
            is_ref = vrid in ("9", "10", "11", "12", "13")
            formula = "ltx2" if vrid in ("14", "15", "16") else ("h3_ref2va" if is_ref else "h3")
            frames, max_sec = quantize_video_frames(eff_dur, frames_formula=formula, fps=24.0)
            res_str = resolution
            mtype = "minimax_h3_ref2va_pruned_ptd_refasymopd" if is_ref else "minimax_h3_fl2va_pruned_pdd"
            steps = 20 if vrid in ("5", "6", "7", "8", "13") else 8
            w_size = 241 if formula == "ltx2" else 362
            w_ov = 9 if formula == "ltx2" else 18

        est = estimate_pass_render_time(
            frames=frames,
            resolution=res_str,
            model_id=vrid,
            model_type=mtype,
            pass_num=p_num,
            has_end_anchor=has_end and p_num != 2,
            num_steps=steps,
            sliding_window_size=w_size,
            sliding_window_overlap=w_ov,
            calibration_alpha=calibration_alpha,
            next_quantum_max_sec=max_sec,
        )
        pass_estimates.append(est)

    total_sec = round(sum(p["estimated_seconds"] for p in pass_estimates), 1)
    primary = pass_estimates[0]
    split_savings_sec = 0.0
    if primary["is_split_window"] and (end_s - start_s) >= 2.0:
        mid_s = round((start_s + end_s) / 2.0, 2)
        sc_a = dict(sc, id=f"{sc.get('id', 'S')}a", start=start_s, end=mid_s)
        sc_a.pop("anchor_end", None)
        sc_a.pop("render_pad_end", None)
        sc_b = dict(sc, id=f"{sc.get('id', 'S')}b", start=mid_s, end=end_s)
        sc_b.pop("render_pad_start", None)
        est_a = estimate_scene_render_time(sc_a, ctx=ctx, resolution=resolution, calibration_alpha=calibration_alpha) if sc_a["end"] - sc_a["start"] < (end_s - start_s) - 0.1 else {"estimated_seconds": total_sec / 2}
        est_b = estimate_scene_render_time(sc_b, ctx=ctx, resolution=resolution, calibration_alpha=calibration_alpha) if sc_b["end"] - sc_b["start"] < (end_s - start_s) - 0.1 else {"estimated_seconds": total_sec / 2}
        split_total = est_a["estimated_seconds"] + est_b["estimated_seconds"]
        split_savings_sec = round(max(0.0, total_sec - split_total), 1)

    return {
        "estimated_seconds": total_sec,
        "estimated_str": format_duration_estimate(total_sec),
        "frames": primary["frames"],
        "effective_duration": round(eff_dur, 2),
        "head_pad": round(head_pad, 2),
        "tail_pad": round(tail_pad, 2),
        "window_count": primary["window_count"],
        "regime": primary["regime"],
        "is_split_window": primary["is_split_window"],
        "next_quantum_max_sec": primary["next_quantum_max_sec"],
        "quantum_headroom_sec": round(max(0.0, primary["next_quantum_max_sec"] - eff_dur), 2),
        "split_savings_seconds": split_savings_sec,
        "split_savings_str": format_duration_estimate(split_savings_sec) if split_savings_sec > 0 else "",
        "is_duet": is_duet,
        "pass_count": len(pass_estimates),
        "passes": pass_estimates,
    }


def attach_queue_time_estimates(
    queue: list[dict],
    scene_by_id: dict,
    ctx=None,
    proj_dir: Path | None = None,
    default_res: str = "1344x768",
    live_eng: dict | None = None,
) -> dict:
    """Populates estimated_seconds, estimated_str, frames, regime, and is_split_window on each queue item."""
    alpha = compute_calibration_factor(proj_dir)
    now_ts = time.time()
    pending_total_sec = 0.0

    for itm in queue:
        sid = itm.get("scene_id")
        sc = scene_by_id.get(sid) if sid else None
        p_num = itm.get("pass")
        res_str = str(itm.get("resolution") or default_res)

        if sc:
            sc_est = estimate_scene_render_time(sc, ctx=ctx, resolution=res_str, calibration_alpha=alpha)
            if p_num == 2 and len(sc_est["passes"]) > 1:
                p_est = sc_est["passes"][1]
            else:
                p_est = sc_est["passes"][0]
        else:
            dur = float(itm.get("duration") or 6.0)
            frames, max_sec = quantize_video_frames(dur, "h3", 24.0)
            p_est = estimate_pass_render_time(frames=frames, resolution=res_str, pass_num=p_num, calibration_alpha=alpha, next_quantum_max_sec=max_sec)

        itm["frames"] = p_est["frames"]
        itm["estimated_seconds"] = p_est["estimated_seconds"]
        itm["estimated_str"] = p_est["estimated_str"]
        itm["is_split_window"] = p_est["is_split_window"]
        itm["regime"] = p_est["regime"]
        itm["next_quantum_max_sec"] = p_est["next_quantum_max_sec"]
        itm["calibration_alpha"] = alpha
        if not itm.get("window_count"):
            itm["window_count"] = p_est["window_count"]

        status = itm.get("status")
        if status == "rendering":
            rem_sec = p_est["estimated_seconds"]
            if isinstance(live_eng, dict):
                started = float(live_eng.get("started_at") or 0.0)
                pct = float(itm.get("pct") or live_eng.get("pct") or 0.0)
                step = int(itm.get("step") or live_eng.get("step") or 0)
                if started > 0 and step >= 1 and pct >= 5.0:
                    elapsed_live = max(1.0, now_ts - started)
                    p_frac = max(0.05, min(0.99, pct / 100.0))
                    w = p_frac ** 1.2
                    blended_total = (1.0 - w) * p_est["estimated_seconds"] + w * (elapsed_live / p_frac)
                    rem_sec = max(5.0, round(blended_total - elapsed_live, 1))
                elif started > 0:
                    elapsed_live = max(0.0, now_ts - started)
                    rem_sec = max(5.0, round(p_est["estimated_seconds"] - elapsed_live, 1))
            itm["estimated_remaining_seconds"] = rem_sec
            itm["estimated_remaining_str"] = format_duration_estimate(rem_sec)
            pending_total_sec += rem_sec
        elif status == "queued":
            pending_total_sec += p_est["estimated_seconds"]

    return {
        "calibration_alpha": alpha,
        "pending_estimated_seconds": round(pending_total_sec, 1),
        "pending_estimated_str": format_duration_estimate(pending_total_sec),
    }
