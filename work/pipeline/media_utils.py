import hashlib
import json
import os
import re
import shutil
import subprocess
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
    "prompt", "seed", "output_filename", "image_start", "audio_guide", "video_guide", "video_mask",
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
