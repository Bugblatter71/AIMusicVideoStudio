from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

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
    return {k: v for k, v in task.items() if k not in _NON_SETTING_TASK_KEYS and not str(k).startswith("_")}


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


def _ffprobe_query(ffmpeg: Path | None, mp4: Path, select_streams: str | None, show_entries: str, fmt: str, timeout: int = 15) -> str:
    """Runs ffprobe cleanly and returns stripped stdout, or '' on error."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix) if ffmpeg else Path("ffprobe")
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    cmd = [str(ffprobe), "-v", "quiet"]
    if select_streams:
        cmd.extend(["-select_streams", select_streams])
    cmd.extend(["-show_entries", show_entries, "-of", fmt, str(mp4)])
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout).stdout.strip()
    except Exception:
        return ""


def probe_video_dimensions(ffmpeg: Path, mp4: Path) -> tuple[int, int] | None:
    """Probes the width and height of an MP4 file using ffprobe."""
    parts = _ffprobe_query(ffmpeg, mp4, "v:0", "stream=width,height", "csv=p=0:s=x", timeout=30).split("x")
    try:
        return (int(parts[0]), int(parts[1])) if len(parts) == 2 else None
    except Exception:
        return None


def probe_video_duration(ffmpeg: Path, mp4: Path) -> float | None:
    """Probes the total duration in seconds of an MP4 file."""
    val = _ffprobe_query(ffmpeg, mp4, None, "format=duration", "default=noprint_wrappers=1:nokey=1")
    try:
        return float(val) if val else None
    except Exception:
        return None


def probe_video_frames(ffmpeg: Path, mp4: Path) -> int | None:
    """Probes the total video frame count of an MP4 file."""
    val = _ffprobe_query(ffmpeg, mp4, "v:0", "stream=nb_frames", "default=noprint_wrappers=1:nokey=1")
    return int(val) if val.isdigit() else None


def probe_has_audio_stream(ffmpeg: Path, mp4: Path) -> bool:
    """Returns True if an MP4/media file contains at least one audio stream (defaults True if unprobed)."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix) if ffmpeg else Path("ffprobe")
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [str(ffprobe), "-v", "quiet", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", str(mp4)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return True if out.returncode != 0 else bool(out.stdout.strip())
    except Exception:
        return True


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
    allow_tail_pad: bool = False,
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
            r_end = max(float(start) + float(dur), float(end)) if allow_tail_pad else (float(start) + float(dur))
            scene_renders[out_name] = {
                "render_start": r_start,
                "render_end": r_end,
                "frames": frames,
                "duration": dur,
            }
        else:
            return False, None, None

    # Fully covered if rendered clip starts at or before timeline start and ends at or after timeline end
    # Uses a tiny 50ms tolerance for numerical/frame-rate quantization; allow_tail_pad accepts held/padded outro cuts
    is_covered = (r_start <= (start + 0.05)) and (allow_tail_pad or (r_end >= (end - 0.05)))
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
        prev_clean = {k: v for k, v in prev.items() if k not in _NON_SETTING_TASK_KEYS and not str(k).startswith("_")}
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

    m_res, w_px, h_px = 1.0, 1344, 768
    res_clean = str(resolution or "1344x768").strip().lower()
    if "x" in res_clean:
        try:
            parts = res_clean.split("x")
            w_px, h_px = int(parts[0]), int(parts[1].split()[0])
            if w_px > 0 and h_px > 0:
                px = float(w_px * h_px)
                m_res = (px / (1344.0 * 768.0)) ** 1.15
                if px > 1_400_000.0:
                    m_res *= 1.0 + 1.40 * min(1.5, (px - 1_400_000.0) / 688_960.0)
        except Exception:
            pass

    m_steps = max(1.0, float(num_steps or 8)) / 8.0
    alpha = max(0.4, min(4.0, float(calibration_alpha or 1.0)))
    est_sec = round(base_sum * m_res * m_steps * alpha, 1)

    return {
        "estimated_seconds": est_sec,
        "estimated_str": format_duration_estimate(est_sec),
        "frames": f,
        "resolution": res_clean,
        "window_count": len(windows),
        "regime": regime,
        "is_split_window": f > 226,
        "rate_per_frame": round(est_sec / f, 2),
        "next_quantum_max_sec": next_quantum_max_sec if next_quantum_max_sec is not None else round((f - 2) / 24.0, 2),
    }


def _is_ref2va_model(model_id: Any, model_type: str = "") -> bool:
    m_str = str(model_id if model_id is not None else "1").strip()
    mt = str(model_type or "").lower()
    return m_str in ("9", "10", "11", "12", "13") or "ref2va" in mt or "fasth3" in mt or "singularity" in mt


def resolve_pass_effective_resolution(sc: dict | None, pass_num: int | None, base_res: str, backend: str = "local") -> str:
    """Returns effective render resolution (accounts for cropped local Pass 2 ROI when enabled)."""
    res_s = str(base_res or "1344x768").strip().lower()
    if pass_num != 2 or str(backend or "local").strip().lower() != "local" or not isinstance(sc, dict) or "x" not in res_s:
        return res_s
    roi = str(sc.get("pass2_roi_mode") or "full").strip().lower()
    if roi in ("full", "full_frame", "none", "off", ""):
        return res_s
    try:
        w, h = [int(p.split()[0]) for p in res_s.split("x", 1)]
        from .duet_roi import compute_roi_box
        _, _, cw, ch = compute_roi_box(w, h, float(sc.get("split_x") or 0.5), sc.get("pass2_roi_side") or "right", roi, sc.get("pass2_roi_box"))
        return f"{cw}x{ch}"
    except Exception:
        return res_s


def estimate_cloud_pass_render_time(dur_sec: float, resolution: str = "1344x768", auto_chunks: int = 1, calibration_alpha: float = 1.0) -> float:
    """Computes realistic duration-scaled cloud render time without chunk double-counting."""
    res_l = str(resolution or "1344x768").lower()
    chunks = max(1, int(auto_chunks or 1))
    per_chunk_dur = max(1.0, float(dur_sec or 6.0)) / chunks
    base_s = 285.0 if ("1088" in res_l or "1080" in res_l or "2k" in res_l) else (120.0 if "768" in res_l else 75.0)
    alpha = max(0.4, min(3.0, float(calibration_alpha or 1.0)))
    return round(chunks * max(45.0, base_s * max(0.8, per_chunk_dur / 7.5)) * alpha, 1)


def compute_calibration_factor(
    proj_dir: Path | str | None,
    resolution: str | None = None,
    backend: str = "local",
    model_id: str | int | None = None,
) -> float:
    """Computes empirical median multiplier isolated by execution backend, ROI crop resolution, and model tier."""
    if not proj_dir:
        return 1.0
    manifest_path = Path(proj_dir) / "work" / "takes_manifest.json"
    if not manifest_path.is_file():
        return 1.0
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tgt_bk = str(backend or "local").strip().lower()
        def _take_bk(t: dict) -> str:
            return str(t.get("backend") or (t.get("render_settings") or {}).get("backend") or "local").strip().lower()
        all_takes = [
            t for sc in data.get("scenes", {}).values() for t in sc.get("takes", [])
            if float(t.get("elapsed_s") or 0.0) > 30.0 and int(t.get("frames") or 0) > 0
            and (_take_bk(t) == tgt_bk if tgt_bk in ("fal", "minimax") else _take_bk(t) not in ("fal", "minimax"))
        ]
        if not all_takes: return 1.0
        def _px(res_s: str) -> int:
            try: w_s, h_s = str(res_s or "").lower().split("x", 1); return int(w_s) * int(h_s.split()[0])
            except Exception: return 0
        def _take_res(t: dict) -> str:
            rset, snap = t.get("render_settings") or {}, t.get("scene_snapshot") or {}
            return resolve_pass_effective_resolution(snap, t.get("pass_num"), str(t.get("resolution") or rset.get("resolution") or "1344x768").strip().lower(), backend=tgt_bk)
        takes = all_takes
        if resolution and tgt_bk == "local":
            tgt_res = str(resolution).strip().lower()
            exact = [t for t in all_takes if _take_res(t) == tgt_res]
            if len(exact) >= 3: takes = exact
            else:
                tgt_hi = _px(tgt_res) >= (1536 * 864)
                tier = [t for t in all_takes if (_px(_take_res(t)) >= (1536 * 864)) == tgt_hi]
                if len(tier) < 3: return 1.0
                takes = tier
            if model_id is not None and _px(tgt_res) >= (1536 * 864):
                want_ref = _is_ref2va_model(model_id)
                fam = [t for t in takes if _is_ref2va_model(t.get("model_id") or (t.get("scene_snapshot") or {}).get("renderer") or "1", str(t.get("model_type") or "")) == want_ref]
                if len(fam) >= 3: takes = fam
        elif len(takes) < 3: return 1.0
        takes.sort(key=lambda x: float(x.get("created_at") or 0.0), reverse=True)
        ratios = []
        for t in takes[:10]:
            snap, rset, t_res = t.get("scene_snapshot") or {}, t.get("render_settings") or {}, _take_res(t)
            if tgt_bk in ("fal", "minimax"):
                d_s = max(1.0, (int(t["frames"]) - 2) / 24.0)
                pred = estimate_cloud_pass_render_time(d_s, resolution=t_res, auto_chunks=max(1, int((d_s + 14.99) // 15.0)) if d_s > 15.0 else 1, calibration_alpha=1.0)
            else:
                pred = estimate_pass_render_time(
                    frames=int(t["frames"]), resolution=t_res, model_id=t.get("model_id") or snap.get("renderer") or "1",
                    model_type=str(t.get("model_type") or rset.get("model_type") or ""), pass_num=t.get("pass_num"),
                    has_end_anchor=bool(snap.get("anchor_end") or rset.get("image_end")), num_steps=int(rset.get("num_inference_steps") or 8),
                    sliding_window_size=int(rset.get("sliding_window_size") or 362), sliding_window_overlap=int(rset.get("sliding_window_overlap") or 18), calibration_alpha=1.0,
                )["estimated_seconds"]
            if pred > 0: ratios.append(float(t["elapsed_s"]) / pred)
        if not ratios: return 1.0
        ratios.sort()
        mid = len(ratios) // 2
        med = ratios[mid] if len(ratios) % 2 == 1 else 0.5 * (ratios[mid - 1] + ratios[mid])
        return round(max(0.4, min(4.0, med)), 3)
    except Exception: return 1.0

def estimate_scene_render_time(sc: dict, ctx=None, resolution: str = "1344x768", calibration_alpha: float = 1.0, backend: str = "local") -> dict:
    """Computes total estimated render time across all passes of a scene, accounting for padding handles, ROI, and quantums."""
    from .context import resolve_scene_padding
    start_s, end_s = float(sc.get("start", 0.0) or 0.0), float(sc.get("end", 0.0) or 0.0)
    head_pad, tail_pad = ctx.get_scene_render_padding(sc) if (ctx is not None and hasattr(ctx, "get_scene_render_padding")) else resolve_scene_padding(sc)
    eff_dur = max(0.1, (end_s + tail_pad) - max(0.0, start_s - head_pad))
    mode, has_end = str(sc.get("mode") or "solo").strip(), bool(sc.get("anchor_end"))
    is_duet = mode in ("duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right")
    passes, pass_estimates = ([1, 2] if is_duet else [None]), []
    for p_num in passes:
        if ctx is not None and hasattr(ctx, "resolve_video_renderer"):
            vrid, vrspec = ctx.resolve_video_renderer(sc, pass_num=p_num)
            frames, fps_val = ctx.valid_video_frames(eff_dur, vrspec), ctx.model_fps(vrspec)
            max_sec = round((frames - 2) / fps_val, 2)
            res_str = ctx.resolve_scene_resolution(sc, vrspec) if hasattr(ctx, "resolve_scene_resolution") else ctx.resolve_resolution(sc.get("resolution") or vrspec.get("resolution") or ctx.current_resolution)
            mtype, steps = vrspec.get("model_type", "minimax_h3_fl2va_pruned_pdd"), int(vrspec.get("num_inference_steps", 8))
            w_size, w_ov = int(vrspec.get("sliding_window_size", 362)), int(vrspec.get("sliding_window_overlap", 18))
        else:
            raw_r = sc.get("renderer_m" if p_num == 1 else ("renderer_f" if p_num == 2 else "renderer")) or sc.get("renderer") or "1"
            vrid = str(raw_r).strip() if str(raw_r).strip().lower() not in ("", "0", "none", "null", "default") else "1"
            is_ref = _is_ref2va_model(vrid)
            formula = "ltx2" if vrid in ("14", "15", "16") else ("h3_ref2va" if is_ref else "h3")
            frames, max_sec = quantize_video_frames(eff_dur, frames_formula=formula, fps=24.0)
            res_map = {"1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088"}
            raw_sc_res = str(sc.get("resolution") or "").strip()
            res_str = res_map.get(raw_sc_res, raw_sc_res.lower() if "x" in raw_sc_res.lower() else resolution) if (raw_sc_res and raw_sc_res.lower() not in ("default", "inherit", "none", "null")) else resolution
            mtype, steps = ("minimax_h3_ref2va_pruned_ptd_refasymopd" if is_ref else "minimax_h3_fl2va_pruned_pdd"), (20 if vrid in ("5", "6", "7", "8", "13") else 8)
            w_size, w_ov = (241 if formula == "ltx2" else 362), (9 if formula == "ltx2" else 18)
        eff_res = resolve_pass_effective_resolution(sc, p_num, res_str, backend=backend)
        est = estimate_pass_render_time(
            frames=frames, resolution=eff_res, model_id=vrid, model_type=mtype, pass_num=p_num, has_end_anchor=has_end and p_num != 2,
            num_steps=steps, sliding_window_size=w_size, sliding_window_overlap=w_ov, calibration_alpha=calibration_alpha, next_quantum_max_sec=max_sec,
        )
        est["model_id"] = vrid
        pass_estimates.append(est)
    total_sec, primary = round(sum(p["estimated_seconds"] for p in pass_estimates), 1), pass_estimates[0]
    split_savings_sec = 0.0
    if primary["is_split_window"] and (end_s - start_s) >= 2.0:
        mid_s = round((start_s + end_s) / 2.0, 2)
        sc_a, sc_b = dict(sc, id=f"{sc.get('id', 'S')}a", start=start_s, end=mid_s), dict(sc, id=f"{sc.get('id', 'S')}b", start=mid_s, end=end_s)
        sc_a.pop("anchor_end", None); sc_a.pop("render_pad_end", None); sc_b.pop("render_pad_start", None)
        est_a = estimate_scene_render_time(sc_a, ctx=ctx, resolution=resolution, calibration_alpha=calibration_alpha, backend=backend) if sc_a["end"] - sc_a["start"] < (end_s - start_s) - 0.1 else {"estimated_seconds": total_sec / 2}
        est_b = estimate_scene_render_time(sc_b, ctx=ctx, resolution=resolution, calibration_alpha=calibration_alpha, backend=backend) if sc_b["end"] - sc_b["start"] < (end_s - start_s) - 0.1 else {"estimated_seconds": total_sec / 2}
        split_savings_sec = round(max(0.0, total_sec - (est_a["estimated_seconds"] + est_b["estimated_seconds"])), 1)
    return {
        "estimated_seconds": total_sec, "estimated_str": format_duration_estimate(total_sec), "frames": primary["frames"],
        "resolution": primary.get("resolution", resolution), "effective_duration": round(eff_dur, 2), "head_pad": round(head_pad, 2),
        "tail_pad": round(tail_pad, 2), "window_count": primary["window_count"], "regime": primary["regime"],
        "is_split_window": primary["is_split_window"], "next_quantum_max_sec": primary["next_quantum_max_sec"],
        "quantum_headroom_sec": round(max(0.0, primary["next_quantum_max_sec"] - eff_dur), 2), "split_savings_seconds": split_savings_sec,
        "split_savings_str": format_duration_estimate(split_savings_sec) if split_savings_sec > 0 else "", "is_duet": is_duet,
        "pass_count": len(pass_estimates), "passes": pass_estimates,
    }

def attach_queue_time_estimates(
    queue: list[dict], scene_by_id: dict, ctx=None, proj_dir: Path | None = None,
    default_res: str = "1344x768", live_eng: dict | None = None, active_eng_map: dict[str, dict] | None = None,
) -> dict:
    """Populates backend-, ROI-, and model-calibrated time estimates and remaining countdowns on queue items."""
    alpha_by_res: dict[str, float] = {}
    alpha_cache: dict[tuple[str, str, str], float] = {}
    def _get_alpha(res_k: str, bk: str = "local", m_id: str | int | None = None) -> float:
        fam = ("ref2va" if _is_ref2va_model(m_id) else "std") if (m_id is not None and bk == "local") else "any"
        ck = (res_k, bk, fam)
        if ck not in alpha_cache: alpha_cache[ck] = compute_calibration_factor(proj_dir, resolution=res_k, backend=bk, model_id=m_id)
        if bk == "local" and res_k not in alpha_by_res: alpha_by_res[res_k] = alpha_cache[ck]
        return alpha_cache[ck]
    default_alpha = _get_alpha(default_res.strip().lower(), "local", None)
    now_ts, pending_total_sec = time.time(), 0.0
    for itm in queue:
        sid, p_num = itm.get("scene_id"), itm.get("pass")
        sc = scene_by_id.get(sid) if sid else None
        res_str = str(itm.get("resolution") or default_res)
        res_key = res_str.strip().lower()
        itm_bk = str(itm.get("backend") or "local").strip().lower()
        is_cloud = itm_bk in ("fal", "minimax")
        eff_res = resolve_pass_effective_resolution(sc, p_num, res_key, backend=itm_bk) if sc else res_key
        m_id = None
        if sc and not is_cloud:
            if ctx is not None and hasattr(ctx, "resolve_video_renderer"):
                try: m_id, _ = ctx.resolve_video_renderer(sc, pass_num=p_num)
                except Exception: m_id = None
            if m_id is None:
                m_id = sc.get("renderer_m" if p_num == 1 else ("renderer_f" if p_num == 2 else "renderer")) or sc.get("renderer") or "1"
        alpha = _get_alpha(eff_res, itm_bk, m_id)
        if res_key not in alpha_by_res and itm_bk == "local" and eff_res == res_key: alpha_by_res[res_key] = alpha
        if sc:
            sc_est = estimate_scene_render_time(sc, ctx=ctx, resolution=res_str, calibration_alpha=alpha, backend=itm_bk)
            p_est = sc_est["passes"][1] if (p_num == 2 and len(sc_est["passes"]) > 1) else sc_est["passes"][0]
        else:
            frames, max_sec = quantize_video_frames(float(itm.get("duration") or 6.0), "h3", 24.0)
            p_est = estimate_pass_render_time(frames=frames, resolution=res_str, pass_num=p_num, calibration_alpha=alpha, next_quantum_max_sec=max_sec)
        itm["frames"] = p_est["frames"]
        if is_cloud:
            dur_s = float(itm.get("duration") or (sc.get("end", 6.0) - sc.get("start", 0.0) if sc else 6.0))
            eff_est_sec = estimate_cloud_pass_render_time(dur_s, resolution=res_str, auto_chunks=int(itm.get("auto_chunks") or 1), calibration_alpha=alpha)
            itm["estimated_seconds"], itm["estimated_str"] = eff_est_sec, format_duration_estimate(eff_est_sec)
        else:
            eff_est_sec = p_est["estimated_seconds"]
            itm["estimated_seconds"], itm["estimated_str"] = eff_est_sec, p_est["estimated_str"]
        itm["is_split_window"], itm["regime"], itm["next_quantum_max_sec"], itm["calibration_alpha"] = p_est["is_split_window"], p_est["regime"], p_est["next_quantum_max_sec"], alpha
        if not is_cloud: itm["effective_resolution"] = eff_res
        if not itm.get("window_count"): itm["window_count"] = p_est["window_count"]
        if itm.get("status") == "rendering":
            rem_sec, eng_src = eff_est_sec, live_eng
            if isinstance(active_eng_map, dict):
                eng_src = next((active_eng_map[c] for c in (itm.get("output_filename"), itm.get("filename"), Path(str(itm.get("filename") or "")).stem) if c and c in active_eng_map), live_eng)
            started = float(itm.get("started_at") or 0.0)
            if isinstance(eng_src, dict):
                eng_fn = str(eng_src.get("filename") or eng_src.get("output_filename") or "")
                if (not eng_fn) or eng_fn in (itm.get("filename"), itm.get("output_filename"), f"{itm.get('output_filename')}.mp4"):
                    if not started: started = float(eng_src.get("started_at") or 0.0)
                    pct = float(eng_src["pct"] if eng_src.get("pct") is not None else (itm.get("pct") or 0.0))
                    step = int(eng_src["step"] if eng_src.get("step") is not None else (itm.get("step") or 0))
                    if str(eng_src.get("state") or "") == "completed" or pct >= 99.5: rem_sec = 0.0
                    elif started > 0 and 0.0 < (now_ts - started) < 43200.0 and step >= 1 and 5.0 <= pct < 99.5:
                        elapsed_live, p_frac = max(1.0, now_ts - started), max(0.05, min(0.99, pct / 100.0))
                        w = p_frac ** 1.2
                        rem_sec = max(5.0, round(((1.0 - w) * eff_est_sec + w * (elapsed_live / p_frac)) - elapsed_live, 1))
                    elif started > 0 and 0.0 <= (now_ts - started) < 43200.0:
                        rem_sec = max(5.0, round(eff_est_sec - max(0.0, now_ts - started), 1))
            elif started > 0 and 0.0 <= (now_ts - started) < 43200.0:
                rem_sec = max(5.0, round(eff_est_sec - max(0.0, now_ts - started), 1))
            itm["estimated_remaining_seconds"], itm["estimated_remaining_str"] = rem_sec, format_duration_estimate(rem_sec)
            pending_total_sec += rem_sec
        else:
            itm.pop("estimated_remaining_seconds", None); itm.pop("estimated_remaining_str", None)
            if itm.get("status") == "queued": pending_total_sec += eff_est_sec
    return {"calibration_alpha": default_alpha, "calibration_alpha_by_res": alpha_by_res, "pending_estimated_seconds": round(pending_total_sec, 1), "pending_estimated_str": format_duration_estimate(pending_total_sec)}


_FILE_OP_LOCKS: dict[str, threading.RLock] = {}
_FILE_OP_GUARD = threading.Lock()


@contextlib.contextmanager
def locked_file_op(lock_path: Path, timeout: float = 10.0):
    """Thread + cross-process advisory lock around read-modify-write state files."""
    lp = Path(lock_path).resolve()
    with _FILE_OP_GUARD:
        tlock = _FILE_OP_LOCKS.setdefault(str(lp), threading.RLock())
    with tlock:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lp, "a+b")
        locked = False
        try:
            deadline = time.time() + max(0.5, timeout)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.02)
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            fh.close()


def atomic_write_json(path: Path, data) -> None:
    """Atomically writes JSON via a process/thread-unique temp file and os.replace."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(4):
        try:
            os.replace(str(tmp), str(p))
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.015)


def prepend_unique_env_path(extra_paths: list[str] | None = None, env_map=None) -> str:
    """Prepends directories to PATH without duplicates so Windows env stays below 32,767 chars."""
    target = env_map if env_map is not None else os.environ
    raw_cur = [p for p in str(target.get("PATH", "")).split(os.pathsep) if p]
    merged: list[str] = []
    seen: set[str] = set()
    for raw_p in [*(extra_paths or []), *raw_cur]:
        s = str(raw_p).strip()
        if not s:
            continue
        key = os.path.normcase(os.path.normpath(s))
        if key not in seen:
            seen.add(key)
            merged.append(s)
    joined = os.pathsep.join(merged)
    target["PATH"] = joined
    return joined
