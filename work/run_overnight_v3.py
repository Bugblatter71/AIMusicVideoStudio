"""
Generic Self-Service Music Video Generation Pipeline (Wan2GP Local Image + Video + FFmpeg Assembly).

All project- and run-specific settings (environment paths, model parameters, character reference
images, prompt variables, anchor specifications, scene storyboard, and run controls) live in an
external JSON configuration file passed as a parameter.

USAGE:
------
  python work/run_overnight_v3.py <project_config.json> [options / Anchor IDs / Scene IDs]

EXAMPLES:
---------
  1. Automatic Change Detection (renders only edited anchors/scenes & stitches master video):
       python work/run_overnight_v3.py all_or_nothing_v3.json

  2. List all anchors & scenes defined in the JSON:
       python work/run_overnight_v3.py all_or_nothing_v3.json --list

  3. Regenerate specific anchor(s) ONLY (stop after anchors so you can preview them):
       python work/run_overnight_v3.py all_or_nothing_v3.json --anchors-only A03 A11

  4. Regenerate ALL anchors ONLY:
       python work/run_overnight_v3.py all_or_nothing_v3.json --anchors-only all-anchors

  5. Regenerate specific anchor(s) + re-render scenes using them + stitch master video:
       python work/run_overnight_v3.py all_or_nothing_v3.json A03 A11

  6. Re-render specific scene(s) (or a single pass like S14:p2) + stitch master video:
       python work/run_overnight_v3.py all_or_nothing_v3.json S02 S14:p2

  7. Re-render ALL scenes + stitch master video:
       python work/run_overnight_v3.py all_or_nothing_v3.json all

  8. Re-stitch master video immediately without GPU rendering:
       python work/run_overnight_v3.py all_or_nothing_v3.json stitch
"""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


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
    for d in (root_dir, root_dir.parent, Path.cwd(), Path(__file__).resolve().parent.parent):
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


def resolve_ffmpeg_path(project_dir: Path, configured_val: str, wgp_root: Path | None) -> Path:
    """Finds a working FFmpeg executable across project tools, Wan2GP, Pinokio, or system PATH."""
    if configured_val:
        cand = resolve_path(project_dir, configured_val)
        if cand.is_file():
            return cand
    local_tool = project_dir / "tools" / "ffmpeg.exe"
    if local_tool.is_file():
        return local_tool
    workspace_tool = Path(__file__).resolve().parent.parent / "tools" / "ffmpeg.exe"
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


def extract_anchor_id(name_or_str: str | Path) -> str:
    """Extracts normalized anchor ID like 'A01', 'A02' ... 'A14' from any filename or reference string.
    Supports formats like:
      'A01 - Man_singing...', 'A01_Male_Orchestra...', 'A01.jpg', 'A1', 'a01'
    """
    s = Path(str(name_or_str)).name.strip()
    m = re.match(r"^(A\d{1,2})([ _\-\.]|$)", s, re.IGNORECASE)
    if m:
        num = int(m.group(1)[1:])
        return f"A{num:02d}"
    parts = re.split(r"[-_ \.]", s)
    if parts:
        p0 = parts[0].strip().upper()
        if p0.startswith("A") and p0[1:].isdigit():
            num = int(p0[1:])
            return f"A{num:02d}"
    return s.upper()


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


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


def expand_variables(text: str, variables: dict[str, str]) -> str:
    out = text
    for k, v in variables.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def resolve_marker_prompt(prompt_text: str, scene_start: float, markers: list[dict], fps: float = 24.0) -> str:
    """Replaces <marker_name> tags with the marker's relative offset from clip start."""
    if not prompt_text or not markers:
        return prompt_text

    marker_map = {m["name"].strip().lower(): m for m in markers if "name" in m and "time" in m}
    if not marker_map:
        return prompt_text

    def repl(match):
        full_inner = match.group(1).strip()
        parts = [p.strip() for p in full_inner.split(":", 1)]
        tag_name = parts[0].lower()
        modifier = parts[1].lower() if len(parts) > 1 else None

        if tag_name not in marker_map:
            return match.group(0)

        marker = marker_map[tag_name]
        rel_time = float(marker["time"]) - float(scene_start)
        rel_frame = max(0, round(rel_time * float(fps)))

        # Context lookaround
        start_idx = match.start()
        end_idx = match.end()
        preceding = prompt_text[max(0, start_idx - 15) : start_idx]
        following = prompt_text[end_idx : end_idx + 15]

        preceded_by_frame = bool(re.search(r"\b(frame|frames|f)\s*$", preceding, re.IGNORECASE))
        followed_by_frame = bool(re.match(r"^\s*(frames?|f\b)", following, re.IGNORECASE))
        followed_by_sec = bool(re.match(r"^\s*(seconds?|s\b)", following, re.IGNORECASE))

        # Explicit modifiers
        if modifier in ("frame", "frames"):
            return str(rel_frame) if (preceded_by_frame or followed_by_frame) else f"frame {rel_frame}"
        elif modifier in ("f", "raw_frame"):
            return str(rel_frame)
        elif modifier in ("sec", "seconds"):
            return f"{rel_time:.2f} seconds"
        elif modifier == "s":
            return f"{rel_time:.2f}s"
        elif modifier in ("raw", "num"):
            return f"{rel_time:.2f}"

        # Context-dependent defaults
        if preceded_by_frame or followed_by_frame:
            return str(rel_frame)
        if followed_by_sec:
            return f"{rel_time:.2f}"
        return f"{rel_time:.2f}s"

    return re.sub(r"<([^>]+)>", repl, prompt_text)


def adapt_ref_tags(prompt: str, ref_tag_style: str) -> str:
    """Translates <image1>..<image4> tags to match the selected renderer's reference syntax."""
    style = (ref_tag_style or "<imageN>").strip()
    if style == "<imageN>":
        return prompt
    out = prompt
    for i in range(1, 10):
        src = f"<image{i}>"
        if src not in out:
            continue
        if style == "<ref_image_N0>":
            out = out.replace(src, f"<ref_image_{i - 1}>")
        elif style == "Image N":
            out = out.replace(src, f"Image {i}")
        elif style == "image N":
            out = out.replace(src, f"image {i}")
    return out


def adapt_video_prompt(prompt: str, prompt_style: str) -> str:
    """Adapts H3 structured prompts when switching to non-H3 video renderers."""
    style = (prompt_style or "h3").strip().lower()
    if style == "h3":
        return prompt
    lines = []
    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("For the target video,"):
            continue
        if line.startswith("non_diegetic_music:"):
            continue
        if line.startswith("integrated_multimodal_description:"):
            line = line[len("integrated_multimodal_description:") :].strip()
        if line.startswith("[Shot 1]"):
            line = line[len("[Shot 1]") :].strip()
        if line.startswith("Continue directly from <Picture 1>."):
            line = line[len("Continue directly from <Picture 1>.") :].strip()
        line = line.replace("<Picture 1>", "the starting frame")
        if line.startswith("overall_soundscape:"):
            snd = line[len("overall_soundscape:") :].strip()
            if snd and snd.lower() != "n/a":
                lines.append(f"Audio soundscape: {snd}")
            continue
        lines.append(line)
    return " ".join(lines)




class ProjectContext:
    """Holds all resolved paths, model settings, anchors, and scenes loaded from a JSON file."""

    def __init__(self, json_path: Path, profiles_path: Path | None = None):
        self.json_path = Path(json_path).resolve()
        if not self.json_path.exists():
            raise FileNotFoundError(f"Configuration JSON not found: {self.json_path}")

        raw = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.raw = raw

        proj = raw.get("project", {})
        self.project_cfg = proj
        self.project_name = proj.get("name", self.json_path.stem)
        self.project_dir = resolve_path(self.json_path.parent, proj.get("project_dir", "."))

        # Check for separate generic model profiles file
        profiles_raw = {}
        cand_profiles = profiles_path
        if cand_profiles is None:
            p_key = raw.get("profiles_file") or raw.get("model_profiles") or raw.get("profiles") or "model_profiles.json"
            cand_profiles = resolve_path(self.project_dir, p_key)

        if cand_profiles and Path(cand_profiles).exists():
            self.profiles_path = Path(cand_profiles).resolve()
            try:
                profiles_raw = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[Warning] Failed to load model profiles from {self.profiles_path}: {e}")
        else:
            self.profiles_path = None

        # Environment setup: local override -> project config -> model profile
        local_cfg = find_local_config(self.project_dir)
        env = dict(profiles_raw.get("environment", {}))
        env.update(raw.get("environment", {}))
        if isinstance(local_cfg.get("environment"), dict):
            env.update(local_cfg["environment"])
        elif local_cfg:
            env.update({k: v for k, v in local_cfg.items() if k in ("wgp_root", "ffmpeg", "extra_path_dirs")})

        raw_models = raw.get("models", {})
        self.run_controls = raw.get("run_controls", {})
        self.variables = raw.get("variables", {})
        self.markers = raw.get("markers", [])

        # Resolve WGP root with auto-detection fallback across standard Pinokio locations
        wgp_str = os.environ.get("WGP_ROOT") or env.get("wgp_root", "")
        if wgp_str and Path(wgp_str).is_dir():
            self.wgp_root = Path(wgp_str).resolve()
        else:
            detected = auto_detect_wgp_root()
            self.wgp_root = detected if detected else resolve_path(self.project_dir, wgp_str)

        extra_paths = [str(resolve_path(self.project_dir, p)) for p in env.get("extra_path_dirs", []) if p]
        if self.wgp_root and self.wgp_root.is_dir():
            try:
                pinokio_base = self.wgp_root.parent.parent.parent
                for dyn_dir in (
                    pinokio_base / "bin",
                    pinokio_base / "bin" / "miniconda" / "Library" / "bin",
                    self.wgp_root / "ffmpeg_bins",
                ):
                    if dyn_dir.is_dir() and str(dyn_dir) not in extra_paths:
                        extra_paths.append(str(dyn_dir))
            except Exception:
                pass

        if extra_paths:
            os.environ["PATH"] = ";".join(extra_paths) + ";" + os.environ.get("PATH", "")
        os.environ["GIT_PYTHON_REFRESH"] = "quiet"

        self.ffmpeg = resolve_ffmpeg_path(self.project_dir, env.get("ffmpeg", ""), self.wgp_root)

        # Project parameters & paths
        self.fps = int(proj.get("fps", 25))
        self.total_duration = proj.get("total_duration")
        self.trim_first_frame_s = float(proj.get("trim_first_frame_seconds", 0.04))
        self.fade_out_last_s = float(proj.get("fade_out_last_scene_seconds", 2.5))

        # Configurable render overlap padding (head and tail handles)
        raw_overlap = proj.get("render_overlap_seconds", proj.get("overlap_seconds", 0.0))
        if isinstance(raw_overlap, dict):
            self.head_overlap_s = float(raw_overlap.get("head", 0.0))
            self.tail_overlap_s = float(raw_overlap.get("tail", 0.0))
        else:
            self.head_overlap_s = float(raw_overlap or 0.0)
            self.tail_overlap_s = float(raw_overlap or 0.0)

        self.master_audio = resolve_path(self.project_dir, proj["master_audio"])
        self.master_output_template = proj.get("master_output", "All_Or_Nothing_Master_{resolution}_{fps}fps.mp4")
        self.master_output = resolve_path(self.project_dir, self.master_output_template)
        self.stems = {
            k: resolve_path(self.project_dir, v)
            for k, v in proj.get("stems", {}).items()
        }
        # Auto-discover any audio files dropped into the project audio directory
        audio_folder = self.project_dir / "audio"
        if audio_folder.is_dir():
            for af in audio_folder.glob("*.*"):
                if af.suffix.lower() in (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"):
                    if af.name != self.master_audio.name:
                        if af.stem not in self.stems:
                            self.stems[af.stem] = af
                        if af.name not in self.stems:
                            self.stems[af.name] = af

        dirs = proj.get("directories", {})
        self.anchors_dir = resolve_path(self.project_dir, dirs.get("anchors", "work/anchors"))
        self.audio_dir = resolve_path(self.project_dir, dirs.get("audio_slices", "work/audio"))
        self.masks_dir = resolve_path(self.project_dir, dirs.get("masks", "work/masks"))
        self.jobs_dir = resolve_path(self.project_dir, dirs.get("jobs", "work/jobs"))
        self.renders_dir = resolve_path(self.project_dir, dirs.get("renders", "work/renders"))
        self.trimmed_dir = resolve_path(self.project_dir, dirs.get("trimmed", "work/trimmed"))
        self.generate_review_strips = bool(proj.get("generate_review_strips", False))
        self.strips_dir = resolve_path(self.project_dir, dirs.get("review_strips", "work/frames"))

        for d in (
            self.anchors_dir,
            self.audio_dir,
            self.masks_dir,
            self.jobs_dir,
            self.renders_dir,
            self.trimmed_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        if self.generate_review_strips:
            self.strips_dir.mkdir(parents=True, exist_ok=True)

        self.state_hash_file = self.jobs_dir / "state_hashes.json"

        # Model configurations: project chooses which models/resolution to use; profile supplies the catalogs
        self.default_anchor_renderer = str(
            raw_models.get("default_anchor_renderer")
            or raw_models.get("anchor_renderer")
            or 3
        )
        self.anchor_shared_cfg = dict(profiles_raw.get("anchor_shared_settings", {}))
        self.anchor_shared_cfg.update(
            raw_models.get("anchor_shared_settings") or raw_models.get("anchor_generator") or {}
        )
        self.anchor_renderers = {
            str(k): dict(v) for k, v in profiles_raw.get("anchor_renderers", {}).items()
        }
        self.anchor_renderers.update({
            str(k): dict(v) for k, v in raw_models.get("anchor_renderers", {}).items()
        })
        if not self.anchor_renderers and "anchor_generator" in raw_models:
            ag = dict(raw_models["anchor_generator"])
            self.anchor_renderers["1"] = {
                "name": ag.get("model_type", "qwen_image_21_turbo_7B"),
                "model_type": ag.get("model_type", "qwen_image_21_turbo_7B"),
                "ref_tag_style": "<imageN>",
                "custom_settings": ag.get("custom_settings", {}),
            }

        self.anchor_refs = [
            str(resolve_path(self.project_dir, r))
            for r in self.anchor_shared_cfg.get("image_refs", [])
        ]

        # Standard block-aligned 16:9 widescreen resolutions (exact multiples of 16 for DiT / VAE models)
        self.builtin_resolutions = {
            "1": {
                "name": "480p Fast Preview",
                "resolution": "832x480",
                "width": 832,
                "height": 480,
                "aspect": "16:9",
                "description": "Fast generation (~1 min/s) for testing motion & framing",
            },
            "2": {
                "name": "540p qHD Balanced",
                "resolution": "960x544",
                "width": 960,
                "height": 544,
                "aspect": "16:9",
                "description": "Good speed/clarity balance for full song passes",
            },
            "3": {
                "name": "720p HD Standard",
                "resolution": "1280x720",
                "width": 1280,
                "height": 720,
                "aspect": "16:9",
                "description": "Standard 720p HD widescreen format",
            },
            "4": {
                "name": "768p WXGA High-Res (Default)",
                "resolution": "1344x768",
                "width": 1344,
                "height": 768,
                "aspect": "16:9",
                "description": "Recommended native DiT resolution for high detail",
            },
            "5": {
                "name": "864p Near-1080p High Fidelity",
                "resolution": "1536x864",
                "width": 1536,
                "height": 864,
                "aspect": "16:9",
                "description": "Near 1080p high fidelity generation",
            },
            "6": {
                "name": "1080p Full HD Block-Aligned",
                "resolution": "1920x1088",
                "width": 1920,
                "height": 1088,
                "aspect": "16:9",
                "description": "Native 1080p multiple-of-16, downscaled/muxed cleanly to 1920x1080",
            },
        }
        self.video_resolutions = dict(self.builtin_resolutions)
        if "video_resolutions" in profiles_raw:
            for k, v in profiles_raw["video_resolutions"].items():
                self.video_resolutions[str(k)] = dict(v)
        if "video_resolutions" in raw_models:
            for k, v in raw_models["video_resolutions"].items():
                self.video_resolutions[str(k)] = dict(v)

        self.default_video_renderer = str(
            raw_models.get("default_video_renderer")
            or raw_models.get("video_renderer")
            or 1
        )
        # Project chooses the resolution (supports default_video_resolution or resolution)
        raw_res = (
            raw_models.get("default_video_resolution")
            or raw_models.get("resolution")
            or (raw_models.get("video_shared_settings", {}).get("resolution") if isinstance(raw_models.get("video_shared_settings"), dict) else None)
            or self.project_cfg.get("master_resolution")
            or 4
        )
        self.default_video_resolution = str(raw_res)

        self.video_shared_cfg = dict(profiles_raw.get("video_shared_settings", {}))
        self.video_shared_cfg.update(
            raw_models.get("video_shared_settings") or raw_models.get("video_generator") or {}
        )
        self.current_resolution = self.resolve_resolution(self.default_video_resolution)
        self.video_shared_cfg["resolution"] = self.current_resolution
        self.update_master_output()

        self.video_renderers = {
            str(k): dict(v) for k, v in profiles_raw.get("video_renderers", {}).items()
        }
        self.video_renderers.update({
            str(k): dict(v) for k, v in raw_models.get("video_renderers", {}).items()
        })
        if not self.video_renderers and "video_generator" in raw_models:
            vg = dict(raw_models["video_generator"])
            self.video_renderers["1"] = {
                "name": vg.get("model_type", "minimax_h3_fl2va_pruned_pdd"),
                "model_type": vg.get("model_type", "minimax_h3_fl2va_pruned_pdd"),
                "frames_formula": "h3",
                "prompt_style": "h3",
                "num_inference_steps": vg.get("num_inference_steps", 8),
                "guidance_phases": vg.get("guidance_phases", 1),
                "flow_shift": vg.get("flow_shift", 12.0),
                "sample_solver": vg.get("sample_solver", "euler"),
                "audio_prompt_type": vg.get("audio_prompt_type", "A"),
                "pass2_video_prompt_type": "GVA",
                "denoising_strength": vg.get("denoising_strength", 1.0),
                "masking_strength": vg.get("masking_strength", 1.0),
                "sliding_window_size": vg.get("sliding_window_size", 362),
                "sliding_window_overlap": vg.get("sliding_window_overlap", 18),
                "custom_settings": vg.get("custom_settings", {"h3_mask_mode": "grouped_rows"}),
            }

        # Anchors & Scenes
        self.anchors = raw.get("anchors", [])
        self.scenes = raw.get("scenes", [])

    def scan_anchor_disk_files(self) -> dict[str, Path]:
        """Scans self.anchors_dir for any image files starting with Ann (e.g. A01, A02 ... A14).
        Returns a dictionary mapping 'A01' -> Path(...), etc.
        """
        if not self.anchors_dir.exists():
            return {}
        res: dict[str, Path] = {}
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        for p in sorted(self.anchors_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in exts:
                aid = extract_anchor_id(p.name)
                if aid.startswith("A") and aid[1:].isdigit():
                    res[aid] = p
        return res

    def resolve_anchor_path(self, ref_or_sp: str | dict | Path) -> tuple[str, Path]:
        """Given an anchor spec dict, a filename string, or an anchor ID (e.g. 'A01'):
        Resolves to (aid, actual_path_on_disk).
        If an image file starting with that aid exists in anchors_dir, uses that file.
        Otherwise falls back to anchors_dir / default_filename.
        """
        if isinstance(ref_or_sp, dict):
            raw_name = ref_or_sp.get("filename", "")
        else:
            raw_name = str(ref_or_sp)
        aid = extract_anchor_id(raw_name)
        disk_map = self.scan_anchor_disk_files()
        if aid in disk_map:
            return aid, disk_map[aid]
        candidate = self.anchors_dir / Path(raw_name).name
        return aid, candidate

    def resolve_anchor_renderer(self, sp: dict) -> tuple[str, dict]:
        override = sp.get("renderer")
        if override is None or str(override).strip().lower() in ("", "0", "none", "null", "default"):
            rid = str(self.default_anchor_renderer)
        else:
            rid = str(override).strip()
        if rid not in self.anchor_renderers:
            valid = ", ".join(sorted(self.anchor_renderers.keys(), key=lambda x: int(x) if x.isdigit() else 999))
            raise ValueError(
                f"Invalid anchor renderer '{rid}' on {sp.get('filename', 'anchor')}. Valid renderer numbers: {valid}"
            )
        return rid, self.anchor_renderers[rid]

    def resolve_video_renderer(self, sc: dict, pass_num: int | None = None) -> tuple[str, dict]:
        override = None
        if pass_num == 1 and sc.get("renderer_m") is not None:
            override = sc.get("renderer_m")
        elif pass_num == 2 and sc.get("renderer_f") is not None:
            override = sc.get("renderer_f")
        if override is None:
            override = sc.get("renderer")
        if override is None or str(override).strip().lower() in ("", "0", "none", "null", "default"):
            rid = str(self.default_video_renderer)
        else:
            rid = str(override).strip()
        if rid not in self.video_renderers:
            valid = ", ".join(sorted(self.video_renderers.keys(), key=lambda x: int(x) if x.isdigit() else 999))
            raise ValueError(
                f"Invalid video renderer '{rid}' on scene {sc.get('id', '?')}. Valid renderer numbers: {valid}"
            )
        return rid, self.video_renderers[rid]

    def resolve_resolution(self, val: Any) -> str:
        """Resolves an integer (1..6), string number ('1'..'6'), dict, or 'WxH' string to a valid 'WxH' string."""
        if val is None or val == "":
            val = getattr(self, "default_video_resolution", "4")
        if isinstance(val, dict):
            val = val.get("resolution", getattr(self, "default_video_resolution", "4"))
        sval = str(val).strip()
        if sval in self.video_resolutions:
            entry = self.video_resolutions[sval]
            return entry["resolution"] if isinstance(entry, dict) else str(entry)
        if "x" in sval.lower():
            return sval.lower()
        if sval in self.builtin_resolutions:
            return self.builtin_resolutions[sval]["resolution"]
        return "1344x768"

    def get_resolution_number(self, res_str: str) -> str:
        """Finds matching option number (e.g. '4') for a given 'WxH' resolution string."""
        target = self.resolve_resolution(res_str).lower()
        for k, v in self.video_resolutions.items():
            r = v["resolution"].lower() if isinstance(v, dict) else str(v).lower()
            if r == target:
                return k
        return "-"

    def update_master_output(self):
        """Updates master output path based on template and current resolution/fps."""
        template = str(self.project_cfg.get("master_output", "All_Or_Nothing_Master_{resolution}_{fps}fps.mp4"))
        safe_name = re.sub(r'[^\w\-]+', '_', self.project_name).strip('_')
        template = template.replace("{project_name}", safe_name)
        res_str = self.current_resolution
        if "{resolution}" in template:
            formatted = template.replace("{resolution}", res_str).replace("{fps}", str(self.fps))
        elif "{fps}" in template:
            p = Path(template)
            stem = re.sub(r'_\d+x\d+', f'_{res_str}', p.stem)
            if f"_{res_str}" not in stem:
                stem = f"{stem}_{res_str}"
            formatted = f"{stem}{p.suffix}".replace("{fps}", str(self.fps))
        else:
            p = Path(template)
            stem = re.sub(r'_\d+x\d+', f'_{res_str}', p.stem)
            if f"_{res_str}" not in stem:
                stem = f"{stem}_{res_str}"
            formatted = f"{stem}{p.suffix}"
        self.master_output = resolve_path(self.project_dir, formatted)

    def set_resolution(self, val: Any):
        """Switches project resolution and recalculates master output path."""
        self.default_video_resolution = str(val)
        self.current_resolution = self.resolve_resolution(val)
        self.video_shared_cfg["resolution"] = self.current_resolution
        self.update_master_output()

    def load_state_hashes(self) -> dict:
        if self.state_hash_file.exists():
            try:
                return json.loads(self.state_hash_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"anchors": {}, "tasks": {}}

    def save_state_hashes(self, state: dict):
        self.state_hash_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def model_fps(self, rspec: dict | None = None) -> float:
        spec = rspec or {}
        force = spec.get("force_fps", self.video_shared_cfg.get("force_fps", ""))
        if str(force).strip():
            try:
                return float(force)
            except ValueError:
                pass
        formula = str(spec.get("frames_formula", "")).lower()
        mtype = str(spec.get("model_type", "")).lower()
        if "h3" in formula or "minimax_h3" in mtype:
            return 24.0
        return float(self.fps)

    def valid_video_frames(self, min_seconds: float, rspec: dict | None = None) -> int:
        spec = rspec or {}
        formula = str(spec.get("frames_formula", "h3")).lower()
        fps = self.model_fps(rspec)
        needed = math.ceil(min_seconds * fps) + 2
        if formula == "ltx2":
            needed = max(17, needed)
            k = math.ceil((needed - 1) / 8)
            return 8 * k + 1
        min_f = 124 if formula == "h3_ref2va" else 107
        needed = max(min_f, needed)
        k = math.ceil((needed - 5) / 17)
        calculated = 17 * k + 5
        if "max_frames" in spec:
            calculated = min(int(spec["max_frames"]), calculated)
        return calculated

    def slice_audio(self, src_audio: Path, start_s: float, dur_s: float, out_wav: Path):
        expected_size = round(dur_s * 176400) + 44
        if out_wav.exists() and abs(out_wav.stat().st_size - expected_size) < 4000:
            return
        subprocess.run(
            [
                str(self.ffmpeg), "-y", "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}",
                "-i", str(src_audio), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out_wav),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def ensure_silent_wav(self, dur_s: float, frames: int) -> Path:
        out_wav = self.audio_dir / f"silent_{frames}f.wav"
        expected_size = round(dur_s * 176400) + 44
        if out_wav.exists() and abs(out_wav.stat().st_size - expected_size) < 4000:
            return out_wav
        subprocess.run(
            [
                str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{dur_s:.3f}", "-c:a", "pcm_s16le", str(out_wav),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return out_wav

    def resolve_scene_audio(self, sid: str, stem_key: str, start_s: float, gen_dur: float, frames: int) -> Path:
        skey = (stem_key or "").strip().lower()
        if not skey or skey in ("silent", "none"):
            return self.ensure_silent_wav(gen_dur, frames)

        # Match configured project stems or master audio
        src = None
        if stem_key in self.stems:
            src = self.stems[stem_key]
        elif skey in ("master", "full"):
            src = self.master_audio
        else:
            for k, v in self.stems.items():
                if k.lower() == skey:
                    src = v
                    break
            if src is None:
                cand = resolve_path(self.project_dir, stem_key)
                if cand.exists() and cand.is_file():
                    src = cand
                else:
                    for ext in ("", ".mp3", ".wav", ".flac", ".ogg", ".m4a"):
                        c = self.project_dir / "audio" / f"{stem_key}{ext}"
                        if c.exists() and c.is_file():
                            src = c
                            break

        # If stem does not exist, log a clear warning with valid stems and fall back safely to silence
        if src is None or not Path(src).exists():
            valid_keys = list(self.stems.keys()) + ["master", "silent"]
            print(
                f"  [Warning] Scene {sid}: Audio stem '{stem_key}' not found on disk (valid stems: {valid_keys}). "
                f"Using silence so the render can proceed.",
                flush=True,
            )
            return self.ensure_silent_wav(gen_dur, frames)

        safe_stem_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem_key)
        out_wav = self.audio_dir / f"{sid}_{safe_stem_tag}.wav"
        self.slice_audio(Path(src), start_s, gen_dur, out_wav)
        return out_wav

    def ensure_right_mask(self, frames: int, split_x_pct: float = 0.50, resolution: str | None = None) -> Path:
        res_str = self.resolve_resolution(resolution or self.current_resolution)
        w, h = [int(x) for x in res_str.lower().split("x")]
        tag = f"{int(split_x_pct * 100)}_{w}x{h}"
        mask_mp4 = self.masks_dir / f"mask_right_{tag}_{frames}f.mp4"
        if mask_mp4.exists() and mask_mp4.stat().st_size > 1000:
            return mask_mp4
        mask_pgm = self.masks_dir / f"mask_right_{tag}.pgm"
        if not mask_pgm.exists():
            header = f"P5\n{w} {h}\n255\n".encode("ascii")
            left_w = int(w * split_x_pct)
            right_w = w - left_w
            row = (b"\x00" * left_w) + (b"\xff" * right_w)
            mask_pgm.write_bytes(header + (row * h))
        subprocess.run(
            [
                str(self.ffmpeg), "-y", "-loop", "1", "-i", str(mask_pgm),
                "-frames:v", str(frames), "-r", str(self.fps),
                "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(mask_mp4),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return mask_mp4

    def make_video_task(
        self,
        output_filename: str,
        prompt: str,
        video_length: int,
        seed: int,
        image_start: Path,
        audio_path: Path,
        rspec: dict,
        video_guide: Path | None = None,
        video_mask: Path | None = None,
    ) -> dict:
        vshared = self.video_shared_cfg
        expanded = expand_variables(prompt, self.variables)
        adapted_prompt = adapt_video_prompt(expanded, rspec.get("prompt_style", "h3"))
        pass2_vpt = rspec.get("pass2_video_prompt_type", "GVA")
        neg_prompt = rspec.get("negative_prompt", vshared.get("negative_prompt", ""))
        task = {
            "model_type": rspec.get("model_type", "minimax_h3_fl2va_pruned_pdd"),
            "prompt": adapted_prompt,
            "negative_prompt": neg_prompt,
            "resolution": self.resolve_resolution(rspec.get("resolution", self.current_resolution)),
            "video_length": video_length,
            "seed": seed,
            "force_fps": str(rspec.get("force_fps", vshared.get("force_fps", ""))),
            "num_inference_steps": rspec.get("num_inference_steps", vshared.get("num_inference_steps", 8)),
            "guidance_phases": rspec.get("guidance_phases", vshared.get("guidance_phases", 1)),
            "sample_solver": rspec.get("sample_solver", vshared.get("sample_solver", "euler")),
            "repeat_generation": rspec.get("repeat_generation", vshared.get("repeat_generation", 1)),
            "multi_prompts_gen_type": rspec.get("multi_prompts_gen_type", vshared.get("multi_prompts_gen_type", "PG")),
            "image_mode": rspec.get("image_mode", vshared.get("image_mode", 0)),
            "image_prompt_type": rspec.get("image_prompt_type", vshared.get("image_prompt_type", "S")),
            "video_prompt_type": pass2_vpt if (video_guide and video_mask) else "",
            "audio_prompt_type": rspec.get("audio_prompt_type", vshared.get("audio_prompt_type", "")),
            "image_start": str(image_start),
            "audio_guide": str(audio_path) if "A" in rspec.get("audio_prompt_type", vshared.get("audio_prompt_type", "")) else "",
            "sliding_window_size": rspec.get("sliding_window_size", vshared.get("sliding_window_size", 362)),
            "sliding_window_overlap": rspec.get("sliding_window_overlap", vshared.get("sliding_window_overlap", 18)),
            "sliding_window_discard_last_frames": rspec.get(
                "sliding_window_discard_last_frames", vshared.get("sliding_window_discard_last_frames", 0)
            ),
            "sliding_window_trim_first_frames": rspec.get(
                "sliding_window_trim_first_frames", vshared.get("sliding_window_trim_first_frames", 0)
            ),
            "skip_steps_cache_type": rspec.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            "skip_steps_multiplier": rspec.get("skip_steps_multiplier", vshared.get("skip_steps_multiplier", 0.08)),
            "custom_settings": dict(rspec.get("custom_settings", vshared.get("custom_settings", {}))),
            "output_filename": output_filename,
        }
        if "flow_shift" in rspec or "flow_shift" in vshared:
            task["flow_shift"] = rspec.get("flow_shift", vshared.get("flow_shift", 12.0))
        mtype = rspec.get("model_type", "minimax_h3_fl2va_pruned_pdd")
        if "spatial_upsampling" in rspec or "spatial_upsampling" in vshared:
            upscale = rspec.get("spatial_upsampling", vshared.get("spatial_upsampling", ""))
            if upscale and ("h3_vae" in upscale) and not mtype.startswith("minimax_h3"):
                pass
            elif upscale:
                task["spatial_upsampling"] = upscale
        if "config" in rspec or "config" in vshared:
            task["config"] = rspec.get("config", vshared.get("config", ""))
        elif "video_vae" in rspec or "video_vae" in vshared:
            vvae = rspec.get("video_vae", vshared.get("video_vae", ""))
            if vvae:
                task["config"] = f",{vvae}" if not vvae.startswith(",") else vvae
        if video_guide and video_mask:
            task["video_guide"] = str(video_guide)
            task["video_mask"] = str(video_mask)
            task["denoising_strength"] = rspec.get("denoising_strength", vshared.get("denoising_strength", 1.0))
            task["masking_strength"] = rspec.get("masking_strength", vshared.get("masking_strength", 1.0))
        return task


class LazyWgpSession:
    """Initializes the Wan2GP GPU session only when at least one GPU task needs to run."""

    def __init__(self, ctx: ProjectContext):
        self.ctx = ctx
        self._session = None

    def get(self):
        if self._session is None:
            sys.path.insert(0, str(self.ctx.wgp_root))
            os.chdir(self.ctx.wgp_root)
            from shared.api import init

            self._session = init(
                root=self.ctx.wgp_root,
                config_path=self.ctx.wgp_root / "wgp_config.json",
                output_dir=self.ctx.renders_dir,
                console_output=True,
                console_isatty=False,
            )
        return self._session


def match_anchor_id(token: str, filename: str) -> bool:
    t = token.strip().upper()
    aid_t = extract_anchor_id(t)
    aid_fn = extract_anchor_id(filename)
    return (
        t in ("ALL", "ALL-ANCHORS")
        or aid_t == aid_fn
        or t in (filename.upper(), Path(filename).stem.upper())
    )


def _scene_id_matches(tok: str, sid: str) -> bool:
    t = tok.strip().upper()
    s = sid.strip().upper()
    if t == s:
        return True
    m_tok = re.match(r"^S0*(\d+)([A-Z_]*)$", t)
    m_sid = re.match(r"^S0*(\d+)([A-Z_]*)$", s)
    if m_tok and m_sid:
        tok_num, tok_suf = int(m_tok.group(1)), m_tok.group(2)
        sid_num, sid_suf = int(m_sid.group(1)), m_sid.group(2)
        if tok_num == sid_num:
            if not tok_suf or tok_suf == sid_suf:
                return True
    elif s.startswith(t):
        rem = s[len(t):]
        if rem and not rem[0].isdigit():
            return True
    return False


def match_scene_pass(force_scenes: list[str], sid: str, pass_num: int | None) -> bool:
    for raw in force_scenes:
        tok = raw.strip().upper()
        if tok in ("ALL", "ALL-SCENES"):
            return True
        if ":" in tok:
            base, sub = tok.split(":", 1)
            if _scene_id_matches(base, sid):
                if sub in ("P1", "PASS1", "1", "LEFT", "M") and pass_num in (None, 1):
                    return True
                if sub in ("P2", "PASS2", "2", "RIGHT", "F") and pass_num in (None, 2):
                    return True
        elif _scene_id_matches(tok, sid):
            return True
    return False


def print_catalog(ctx: ProjectContext):
    print("\n" + "=" * 88)
    prof_info = f" | profiles: {ctx.profiles_path.name}" if ctx.profiles_path else ""
    print(f"PROJECT: {ctx.project_name} ({ctx.json_path.name}{prof_info})")
    print("=" * 88)
    print(f"AVAILABLE ANCHOR RENDERERS (default_anchor_renderer = {ctx.default_anchor_renderer}):")
    print("-" * 88)
    for rid in sorted(ctx.anchor_renderers.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        spec = ctx.anchor_renderers[rid]
        marker = "*DEFAULT*" if rid == str(ctx.default_anchor_renderer) else "         "
        steps = spec.get("num_inference_steps", "-")
        cfg_val = spec.get("guidance_scale", spec.get("embedded_guidance_scale", "-"))
        print(f"  [{rid:>2s}] {marker} | {spec['model_type']:28s} | steps={str(steps):2s}, cfg={str(cfg_val):4s} | {spec.get('name', '')}")

    print("\n" + "-" * 88)
    print(f"AVAILABLE VIDEO RENDERERS (default_video_renderer = {ctx.default_video_renderer}):")
    print("-" * 88)
    for rid in sorted(ctx.video_renderers.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        spec = ctx.video_renderers[rid]
        marker = "*DEFAULT*" if rid == str(ctx.default_video_renderer) else "         "
        steps = spec.get("num_inference_steps", "-")
        phases = spec.get("guidance_phases", 1)
        print(f"  [{rid:>2s}] {marker} | {spec['model_type']:40s} | steps={str(steps):2s}, ph={phases} | {spec.get('name', '')}")

    print("\n" + "-" * 88)
    cur_res_num = ctx.get_resolution_number(ctx.current_resolution)
    print(f"AVAILABLE VIDEO RESOLUTIONS (current = #{cur_res_num} [{ctx.current_resolution}]):")
    print("-" * 88)
    for r_num in sorted(ctx.video_resolutions.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        entry = ctx.video_resolutions[r_num]
        r_str = entry["resolution"] if isinstance(entry, dict) else str(entry)
        r_name = entry.get("name", "") if isinstance(entry, dict) else ""
        r_desc = entry.get("description", "") if isinstance(entry, dict) else ""
        marker = "*ACTIVE* " if str(cur_res_num) == str(r_num) else "         "
        print(f"  [{r_num:>2s}] {marker} | {r_str:10s} | {r_name:32s} | {r_desc}")

    print("\n" + "-" * 88)
    print(f"ANCHOR IMAGES ({len(ctx.anchors)} total in {ctx.anchors_dir}):")
    print("-" * 88)
    for sp in ctx.anchors:
        aid, target_path = ctx.resolve_anchor_path(sp)
        fn = target_path.name
        rid, rspec = ctx.resolve_anchor_renderer(sp)
        ovr = f"#{rid}(ovr)" if sp.get("renderer") is not None else f"#{rid}(def)"
        used_by = [sc["id"] for sc in ctx.scenes if extract_anchor_id(sc["anchor"]) == aid]
        exists = "OK" if target_path.exists() else "MISSING"
        print(f"  {aid:4s} | {ovr:8s} {rspec['model_type']:24s} | seed={sp['seed']} | [{exists}] {fn:52s} -> {', '.join(used_by)}")

    print("\n" + "-" * 88)
    print(f"VIDEO SCENES ({len(ctx.scenes)} total in {ctx.renders_dir}):")
    print("-" * 88)
    for sc in ctx.scenes:
        sid = sc["id"]
        dur = sc["end"] - sc["start"]
        aid, apath = ctx.resolve_anchor_path(sc["anchor"])
        mode = sc["mode"]
        vrid, vrspec = ctx.resolve_video_renderer(sc)
        vovr = f"#{vrid}(ovr)" if sc.get("renderer") is not None else f"#{vrid}(def)"
        print(f"  {sid:4s} | {vovr:8s} {vrspec['model_type']:28s} | {sc['start']:6.2f}s - {sc['end']:6.2f}s ({dur:5.2f}s) | Anchor: {aid:4s} | {mode}")
    print("=" * 88 + "\n")


def run_stage1_anchors(
    ctx: ProjectContext,
    lazy_session: LazyWgpSession,
    state: dict,
    force_anchors: list[str],
    sync_only: bool = False,
    dummy_render: bool = False,
) -> set[str]:
    def_rid = str(ctx.default_anchor_renderer)
    def_model = ctx.anchor_renderers.get(def_rid, {}).get("model_type", "qwen_image_21_turbo_7B")
    print("\n" + "=" * 72, flush=True)
    print(
        f"STAGE 1: Checking / Detecting {len(ctx.anchors)} Anchors in {ctx.anchors_dir} "
        f"(Default Renderer #{def_rid}: {def_model})",
        flush=True,
    )
    print("=" * 72, flush=True)

    regenerated_anchors: set[str] = set()
    anchor_hashes = state.setdefault("anchors", {})
    anchor_file_hashes = state.setdefault("anchor_file_hashes", {})
    shared_cfg = ctx.anchor_shared_cfg

    for idx, sp in enumerate(ctx.anchors, 1):
        aid, target_file = ctx.resolve_anchor_path(sp)
        fn = target_file.name
        rid, rspec = ctx.resolve_anchor_renderer(sp)
        model_type = rspec["model_type"]

        raw_expanded = expand_variables(sp.get("prompt", ""), ctx.variables)
        adapted_prompt = adapt_ref_tags(raw_expanded, rspec.get("ref_tag_style", "<imageN>"))

        hash_payload = {
            "filename": fn,
            "seed": sp["seed"],
            "prompt": raw_expanded,
            "refs": ctx.anchor_refs,
        }
        if model_type != "qwen_image_21_turbo_7B":
            hash_payload["model_type"] = model_type
            hash_payload["steps"] = rspec.get("num_inference_steps")
            hash_payload["cfg"] = rspec.get("guidance_scale", rspec.get("embedded_guidance_scale"))
        cur_hash = _sha256(hash_payload)

        file_exists = target_file.exists() and target_file.stat().st_size > 10000
        cur_file_hash = _file_sha256(target_file) if file_exists else "missing"
        prev_file_hash = anchor_file_hashes.get(aid)

        if sync_only and file_exists:
            anchor_hashes[fn] = cur_hash
            anchor_file_hashes[aid] = cur_file_hash
            continue

        forced = any(match_anchor_id(tok, fn) for tok in force_anchors)

        file_changed = (prev_file_hash is not None and prev_file_hash != cur_file_hash)
        is_new_anchor = (prev_file_hash is None and file_exists)

        if file_exists and not forced:
            if is_new_anchor or file_changed:
                reason = "NEW ANCHOR FILE DETECTED" if is_new_anchor else "ANCHOR FILE UPDATED ON DISK"
                print(f"  [{idx:02d}/{len(ctx.anchors)}] {aid} ({fn}) [{reason}].", flush=True)
                anchor_hashes[fn] = cur_hash
                anchor_file_hashes[aid] = cur_file_hash
                ctx.save_state_hashes(state)
                regenerated_anchors.add(aid)
                regenerated_anchors.add(fn)
                continue
            else:
                print(f"  [{idx:02d}/{len(ctx.anchors)}] {aid} ({fn}) [OK / Up-to-date].", flush=True)
                continue

        reason = "FORCED" if forced else "MISSING FILE"
        print(
            f"  [{idx:02d}/{len(ctx.anchors)}] Generating {fn} [{reason}] "
            f"(Renderer #{rid}: {model_type}, seed={sp['seed']})...",
            flush=True,
        )

        if target_file.exists():
            target_file.unlink()

        if dummy_render:
            t0 = time.time()
            target_file.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    str(ctx.ffmpeg), "-y", "-f", "lavfi",
                    "-i", "color=c=0x1e293b:s=960x544:d=1",
                    "-vf", f"drawtext=text='[DUMMY ANCHOR {aid}]':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=(h-text_h)/2",
                    "-frames:v", "1", "-q:v", "2", str(target_file),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"      -> [DUMMY] Generated mock anchor {target_file.name} in {time.time() - t0:.2f}s", flush=True)
            anchor_hashes[fn] = cur_hash
            anchor_file_hashes[aid] = _file_sha256(target_file)
            ctx.save_state_hashes(state)
            regenerated_anchors.add(aid)
            regenerated_anchors.add(fn)
            continue

        session = lazy_session.get()
        cfg = session.get_default_settings(model_type)
        cfg["model_type"] = model_type
        cfg["image_mode"] = rspec.get("image_mode", shared_cfg.get("image_mode", 1))
        cfg["resolution"] = sp.get("resolution", rspec.get("resolution", shared_cfg.get("resolution", "1664x960")))
        cfg["seed"] = sp["seed"]
        cfg["output_filename"] = target_file.stem
        cfg["remove_background_images_ref"] = rspec.get(
            "remove_background_images_ref", shared_cfg.get("remove_background_images_ref", 0)
        )
        cfg["video_prompt_type"] = rspec.get("video_prompt_type", shared_cfg.get("video_prompt_type", "I"))
        cfg["image_refs"] = ctx.anchor_refs

        for opt_key in (
            "num_inference_steps",
            "guidance_scale",
            "embedded_guidance_scale",
            "flow_shift",
            "sample_solver",
            "multi_prompts_gen_type",
        ):
            if opt_key in rspec:
                cfg[opt_key] = rspec[opt_key]
            if opt_key in sp:
                cfg[opt_key] = sp[opt_key]

        merged_custom = dict(cfg.get("custom_settings") or {})
        if isinstance(shared_cfg.get("custom_settings"), dict):
            merged_custom.update(shared_cfg["custom_settings"])
        if isinstance(rspec.get("custom_settings"), dict):
            merged_custom.update(rspec["custom_settings"])
        if isinstance(sp.get("custom_settings"), dict):
            merged_custom.update(sp["custom_settings"])
        cfg["custom_settings"] = merged_custom
        cfg["prompt"] = adapted_prompt

        t0 = time.time()
        job = session.submit_task(cfg)
        for event in job.events.iter(timeout=0.5):
            if event.kind == "progress":
                p = event.data
                if p.total_steps and p.current_step in (1, p.total_steps // 2, p.total_steps):
                    print(f"      [{p.phase}] {p.current_step}/{p.total_steps} ({p.progress:.0f}%)", flush=True)
        res = job.result()
        if not res.success or not res.generated_files:
            raise RuntimeError(f"Failed generating {fn}: {[e.message for e in res.errors]}")

        gen_path = Path(res.generated_files[0])
        if gen_path.resolve() != target_file.resolve():
            shutil.move(str(gen_path), str(target_file))
        print(f"      -> Saved {target_file.name} in {time.time() - t0:.1f}s", flush=True)

        anchor_hashes[fn] = cur_hash
        anchor_file_hashes[aid] = _file_sha256(target_file)
        ctx.save_state_hashes(state)
        regenerated_anchors.add(aid)
        regenerated_anchors.add(fn)

    return regenerated_anchors


def import_anchors_from_dir(ctx: ProjectContext, src_dir_str: str, state: dict) -> set[str]:
    src_dir = resolve_path(ctx.project_dir, src_dir_str)
    if not src_dir.exists() or not src_dir.is_dir():
        print(f"[Import Error] Directory '{src_dir}' does not exist or is not a directory.", flush=True)
        return set()

    print("\n" + "=" * 72, flush=True)
    print(f"IMPORTING ANCHOR IMAGES from: {src_dir}", flush=True)
    print("=" * 72, flush=True)

    exts = {".jpg", ".jpeg", ".png", ".webp"}
    candidates = sorted([p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in exts])
    imported: set[str] = set()
    anchor_hashes = state.setdefault("anchors", {})

    # Map candidate by pattern matching
    matched_candidates: dict[str, Path] = {}
    used_candidates: set[Path] = set()

    for sp in ctx.anchors:
        fn = sp["filename"]
        aid = fn.split("_")[0].upper()  # e.g. "A01"
        num_str = aid.lstrip("A").lstrip("0") or "0"
        two_digit = f"{int(num_str):02d}"

        for c in candidates:
            if c in used_candidates:
                continue
            c_stem = c.stem.upper().replace(" ", "_").replace("-", "_")
            # Matches: A01, A1, SHOT_1, SHOT_01, SCENE_1, SCENE_01, 01, 1, A01_..., etc.
            if (
                c_stem == aid
                or c_stem == f"A{num_str}"
                or c_stem == f"SHOT_{num_str}"
                or c_stem == f"SHOT_{two_digit}"
                or c_stem == f"SHOT{num_str}"
                or c_stem == f"SHOT{two_digit}"
                or c_stem == f"SCENE_{num_str}"
                or c_stem == f"SCENE_{two_digit}"
                or c_stem == num_str
                or c_stem == two_digit
                or c_stem.startswith(f"{aid}_")
                or f"_{aid}_" in c_stem
                or f"_{aid}$" in c_stem
            ):
                matched_candidates[fn] = c
                used_candidates.add(c)
                break

    # Fallback: if no explicit pattern matched, but folder has 14 files, match sequentially in order
    if len(matched_candidates) == 0 and len(candidates) == len(ctx.anchors):
        print(f"[Import Notice] No A01..A14 names found, but found exactly {len(candidates)} images.")
        print("  Auto-mapping candidate images to A01..A14 in sequential file order.", flush=True)
        for sp, c in zip(ctx.anchors, candidates):
            matched_candidates[sp["filename"]] = c

    for sp in ctx.anchors:
        fn = sp["filename"]
        if fn not in matched_candidates:
            continue
        matched = matched_candidates[fn]
        target_file = ctx.anchors_dir / fn

        ctx.anchors_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(ctx.ffmpeg),
            "-y",
            "-i",
            str(matched),
            "-vf",
            "scale=832:480:force_original_aspect_ratio=decrease,pad=832:480:(ow-iw)/2:(oh-ih)/2:color=black",
            "-q:v",
            "2",
            str(target_file),
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            shutil.copy2(str(matched), str(target_file))

        raw_expanded = expand_variables(sp.get("prompt", ""), ctx.variables)
        rid, rspec = ctx.resolve_anchor_renderer(sp)
        hash_payload = {
            "filename": fn,
            "seed": sp["seed"],
            "prompt": raw_expanded,
            "refs": ctx.anchor_refs,
            "imported_file_hash": _file_sha256(target_file),
        }
        cur_hash = _sha256(hash_payload)
        anchor_hashes[fn] = cur_hash
        imported.add(fn)
        print(f"  [Imported] {matched.name} -> {target_file.name}", flush=True)

    ctx.save_state_hashes(state)
    print(f"Imported {len(imported)} anchor image(s) into {ctx.anchors_dir}.\n", flush=True)
    return imported


def _stem_matches_legacy_render(ctx: ProjectContext, sid: str, stem_key: str, default_stem: str) -> bool:
    """Returns True if stem_key is either the mode's default stem or already has a sliced audio file on disk."""
    if stem_key == default_stem:
        return True
    skey = (stem_key or "").strip().lower()
    if not skey or skey in ("silent", "none"):
        return default_stem in ("", "silent", "none")
    safe_stem_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem_key)
    return (ctx.audio_dir / f"{sid}_{safe_stem_tag}.wav").exists()


def compute_project_task_hashes(ctx: ProjectContext, target_resolution: str | None = None) -> dict[str, tuple[str, ...]]:
    """Computes (current_hash, *compat_hashes) for every video pass in the project.
    Single source of truth used by both server.py and run_stage2_scenes.
    Includes audio stem selection (`stem`, `stem_m`, `stem_f`) in `current_hash`, while
    preserving backward-compatible hashes for existing unedited scenes.
    """
    results: dict[str, tuple[str, ...]] = {}
    vshared = ctx.video_shared_cfg
    res_override = ctx.resolve_resolution(target_resolution) if target_resolution else None

    for sc in ctx.scenes:
        sid = sc["id"]
        start = float(sc.get("start", 0))
        end = float(sc.get("end", 0))
        mode = sc.get("mode", "solo")
        aid, anchor_path = ctx.resolve_anchor_path(sc.get("anchor", ""))
        anchor_fn = anchor_path.name
        a_hash = f"{_file_sha256(anchor_path)}"

        if mode in ("solo", "male_solo", "female_solo", "silent_locked", "single_pass"):
            vrid, vrspec = ctx.resolve_video_renderer(sc)
            vmodel = vrspec["model_type"]
            res_str = res_override or ctx.resolve_resolution(vrspec.get("resolution", ctx.current_resolution))
            if mode == "male_solo":
                default_stem = "male"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_male_solo")
            elif mode == "female_solo":
                default_stem = "female"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_female_solo")
            elif mode == "silent_locked":
                default_stem = "silent"
                stem_key = "silent"
                raw_out_name = sc.get("output_name", f"{sid}_silent")
            elif mode == "solo":
                default_stem = "male"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_solo")
            else:
                default_stem = "silent"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_{stem_key}")

            out_name = f"{raw_out_name}_{res_str}" if res_str not in raw_out_name else raw_out_name

            raw_prompt = sc.get("prompt") or sc.get("prompt_m") or sc.get("prompt_f") or ""
            expanded_prompt = expand_variables(raw_prompt, ctx.variables)
            req_start = max(0.0, start - ctx.head_overlap_s)
            expanded_prompt = resolve_marker_prompt(expanded_prompt, req_start, ctx.markers, fps=ctx.model_fps(vrspec))
            scene_seed = int(sc.get("seed") if sc.get("seed") is not None else (sc.get("seed_m") or 1000))

            flow_shift = vrspec.get("flow_shift", vshared.get("flow_shift", 12.0)) if ("flow_shift" in vrspec or "flow_shift" in vshared) else None
            base_payload = {
                "sid": sid, "mode": mode,
                "anchor": anchor_fn, "a_hash": a_hash,
                "prompt": expanded_prompt, "seed": scene_seed,
                "resolution": res_str,
                "model_type": vmodel,
                "steps": vrspec.get("num_inference_steps"),
                "phases": vrspec.get("guidance_phases"),
                "flow_shift": flow_shift,
                "sample_solver": vrspec.get("sample_solver", vshared.get("sample_solver", "euler")),
                "spatial_upsampling": vrspec.get("spatial_upsampling", vshared.get("spatial_upsampling", "")),
                "config": vrspec.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            t_payload = dict(base_payload, stem=stem_key)
            t_hash = _sha256(t_payload)
            t_payload_leg = dict(t_payload, start=start, end=end)
            valid_hashes = [t_hash, _sha256(t_payload_leg)]

            if _stem_matches_legacy_render(ctx, sid, stem_key, default_stem):
                h_nostem = _sha256(base_payload)
                h_nostem_leg = _sha256(dict(base_payload, start=start, end=end))
                valid_hashes.extend([h_nostem, h_nostem_leg])

            results[out_name] = tuple(valid_hashes)

        elif mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
            vrid_p1, vrspec_p1 = ctx.resolve_video_renderer(sc, pass_num=1)
            vrid_p2, vrspec_p2 = ctx.resolve_video_renderer(sc, pass_num=2)
            vmodel_p1 = vrspec_p1["model_type"]
            vmodel_p2 = vrspec_p2["model_type"]
            res_str = res_override or ctx.resolve_resolution(vrspec_p2.get("resolution", ctx.current_resolution))
            out_p1 = f"{sid}_Pass1_Left_{res_str}"
            out_p2 = f"{sid}_Pass2_Final_{res_str}"

            default_stem_m = "male"
            stem_m = sc.get("stem_m") or sc.get("stem_p1") or sc.get("stem") or default_stem_m
            default_stem_f = "female" if mode == "duet_method_b" else "silent"
            stem_f = sc.get("stem_f") or sc.get("stem_p2") or default_stem_f

            raw_m = sc.get("prompt_m") or sc.get("prompt") or ""
            raw_f = sc.get("prompt_f") or sc.get("prompt") or ""
            exp_prompt_m = expand_variables(raw_m, ctx.variables)
            req_start = max(0.0, start - ctx.head_overlap_s)
            exp_prompt_m = resolve_marker_prompt(exp_prompt_m, req_start, ctx.markers, fps=ctx.model_fps(vrspec_p1))
            exp_prompt_f = expand_variables(raw_f, ctx.variables)
            exp_prompt_f = resolve_marker_prompt(exp_prompt_f, req_start, ctx.markers, fps=ctx.model_fps(vrspec_p2))

            seed_m = int(sc.get("seed_m") if sc.get("seed_m") is not None else (sc.get("seed") or 1000))
            seed_f = int(sc.get("seed_f") if sc.get("seed_f") is not None else (sc.get("seed") or 1000))
            split_x = float(sc.get("split_x", 0.50))

            p1_flow = vrspec_p1.get("flow_shift", vshared.get("flow_shift", 12.0)) if ("flow_shift" in vrspec_p1 or "flow_shift" in vshared) else None
            p1_base = {
                "sid": sid, "pass": 1, "mode": mode,
                "anchor": anchor_fn, "a_hash": a_hash,
                "prompt_m": exp_prompt_m, "seed_m": seed_m,
                "resolution": res_str,
                "model_type": vmodel_p1,
                "steps": vrspec_p1.get("num_inference_steps"),
                "phases": vrspec_p1.get("guidance_phases"),
                "flow_shift": p1_flow,
                "sample_solver": vrspec_p1.get("sample_solver", vshared.get("sample_solver", "euler")),
                "spatial_upsampling": vrspec_p1.get("spatial_upsampling", vshared.get("spatial_upsampling", "")),
                "config": vrspec_p1.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec_p1.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            p1_payload = dict(p1_base, stem_m=stem_m)
            h_p1 = _sha256(p1_payload)
            h_p1_leg = _sha256(dict(p1_payload, start=start, end=end))
            h_p1_nostem = _sha256(p1_base)
            h_p1_nostem_leg = _sha256(dict(p1_base, start=start, end=end))

            p1_valid = [h_p1, h_p1_leg]
            p1_matches_legacy = _stem_matches_legacy_render(ctx, sid, stem_m, default_stem_m)
            if p1_matches_legacy:
                p1_valid.extend([h_p1_nostem, h_p1_nostem_leg])
            results[out_p1] = tuple(p1_valid)

            p2_flow = vrspec_p2.get("flow_shift", vshared.get("flow_shift", 12.0)) if ("flow_shift" in vrspec_p2 or "flow_shift" in vshared) else None
            p2_base = {
                "sid": sid, "pass": 2, "mode": mode,
                "h_p1": h_p1, "split_x": split_x,
                "prompt_f": exp_prompt_f, "seed_f": seed_f,
                "resolution": res_str,
                "model_type": vmodel_p2,
                "steps": vrspec_p2.get("num_inference_steps"),
                "phases": vrspec_p2.get("guidance_phases"),
                "flow_shift": p2_flow,
                "sample_solver": vrspec_p2.get("sample_solver", vshared.get("sample_solver", "euler")),
                "spatial_upsampling": vrspec_p2.get("spatial_upsampling", vshared.get("spatial_upsampling", "")),
                "config": vrspec_p2.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec_p2.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            p2_payload = dict(p2_base, stem_f=stem_f)
            h_p2 = _sha256(p2_payload)
            h_p2_leg = _sha256(dict(p2_payload, start=start, end=end, h_p1=h_p1_leg))

            p2_valid = [h_p2, h_p2_leg]
            if p1_matches_legacy and _stem_matches_legacy_render(ctx, sid, stem_f, default_stem_f):
                p2_nostem = dict(p2_base, h_p1=h_p1_nostem)
                p2_nostem_leg = dict(p2_base, h_p1=h_p1_nostem_leg, start=start, end=end)
                p2_valid.extend([_sha256(p2_nostem), _sha256(p2_nostem_leg)])
            results[out_p2] = tuple(p2_valid)

    return results


def run_stage2_scenes(
    ctx: ProjectContext,
    lazy_session: LazyWgpSession,
    state: dict,
    force_scenes: list[str],
    regenerated_anchors: set[str],
    cascade_anchors: bool = True,
    sync_only: bool = False,
    stitch_only: bool = False,
    dummy_render: bool = False,
    no_queue_sync: bool = False,
) -> list[dict]:
    def_vrid = str(ctx.default_video_renderer)
    def_vmodel = ctx.video_renderers.get(def_vrid, {}).get("model_type", "minimax_h3_fl2va_pruned_pdd")
    print("\n" + "=" * 72, flush=True)
    print(
        f"STAGE 2: Slicing Audio & Checking / Rendering {len(ctx.scenes)} Video Scenes "
        f"(Default Renderer #{def_vrid}: {def_vmodel})",
        flush=True,
    )
    print("=" * 72, flush=True)

    anchor_hashes = state.setdefault("anchors", {})
    task_hashes = state.setdefault("tasks", {})
    settings_store = state.setdefault("task_settings", {})
    project_task_hashes = compute_project_task_hashes(ctx)

    all_tasks: list[tuple[dict, str, str, bool]] = []
    scene_meta: list[dict] = []

    has_scene_filter = bool(force_scenes) and not any(
        x.strip().upper() in ("ALL", "ALL-SCENES") for x in force_scenes
    )
    if has_scene_filter:
        print(f"[Filter] Targeted scene rendering active: {', '.join(force_scenes)} (all other scenes will be skipped)", flush=True)

    for sc in ctx.scenes:
        sid = sc["id"]
        start = float(sc["start"])
        end = float(sc["end"])
        edit_dur = round(end - start, 3)
        mode = sc["mode"]
        aid, anchor_path = ctx.resolve_anchor_path(sc["anchor"])
        anchor_fn = anchor_path.name
        anchor_was_regen = cascade_anchors and (anchor_fn in regenerated_anchors or aid in regenerated_anchors)

        if mode in ("solo", "male_solo", "female_solo", "silent_locked", "single_pass"):
            vrid, vrspec = ctx.resolve_video_renderer(sc)
            vmodel = vrspec["model_type"]

            # Overlap calculation: render with configured padding beyond timeline edit boundaries
            req_start = max(0.0, start - ctx.head_overlap_s)
            req_end = end + ctx.tail_overlap_s
            req_dur = req_end - req_start
            frames = ctx.valid_video_frames(req_dur, vrspec)
            gen_dur = frames / ctx.model_fps(vrspec)
            target_render_start = req_start
            target_render_end = target_render_start + gen_dur

            res_str = ctx.resolve_resolution(vrspec.get("resolution", ctx.current_resolution))
            if mode == "male_solo":
                stem_key = sc.get("stem", "male")
                raw_out_name = sc.get("output_name", f"{sid}_male_solo")
            elif mode == "female_solo":
                stem_key = sc.get("stem", "female")
                raw_out_name = sc.get("output_name", f"{sid}_female_solo")
            elif mode == "silent_locked":
                stem_key = "silent"
                raw_out_name = sc.get("output_name", f"{sid}_silent")
            elif mode == "solo":
                stem_key = sc.get("stem", "male")
                raw_out_name = sc.get("output_name", f"{sid}_solo")
            else:
                stem_key = sc.get("stem", "silent")
                raw_out_name = sc.get("output_name", f"{sid}_{stem_key}")

            if res_str not in raw_out_name:
                out_name = f"{raw_out_name}_{res_str}"
            else:
                out_name = raw_out_name

            # Fast skip: if targeting specific scene(s) and this scene is not targeted, skip audio slicing and disk checks
            if has_scene_filter and not match_scene_pass(force_scenes, sid, None):
                all_tasks.append((
                    {"output_filename": out_name, "model_type": vmodel, "video_length": frames, "resolution": res_str},
                    "", "SKIPPED (NOT TARGETED)", False,
                ))
                scene_meta.append({
                    "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                    "clip": f"{out_name}.mp4", "render_start": target_render_start, "render_end": target_render_end,
                    "anchor": sc.get("anchor"),
                })
                continue

            out_mp4 = resolve_rendered_clip_path(ctx.renders_dir, out_name, promote=True)
            if not out_mp4.exists():
                legacy_mp4 = ctx.renders_dir / f"{raw_out_name}.mp4"
                if legacy_mp4.exists() and legacy_mp4.stat().st_size > 50000:
                    dims = probe_video_dimensions(ctx.ffmpeg, legacy_mp4)
                    if dims and f"{dims[0]}x{dims[1]}" == res_str:
                        shutil.copy2(str(legacy_mp4), str(out_mp4))

            scene_renders = state.setdefault("scene_renders", {})
            is_covered, rendered_start, rendered_end = check_scene_render_coverage(
                ctx.ffmpeg, out_mp4, scene_renders, out_name, start, end
            )
            actual_render_start = rendered_start if is_covered else target_render_start
            actual_render_end = rendered_end if is_covered else target_render_end

            wav_path = ctx.resolve_scene_audio(sid, stem_key, target_render_start, gen_dur, frames)
            raw_prompt = sc.get("prompt") or sc.get("prompt_m") or sc.get("prompt_f") or ""
            expanded_prompt = expand_variables(raw_prompt, ctx.variables)
            expanded_prompt = resolve_marker_prompt(expanded_prompt, target_render_start, ctx.markers, fps=ctx.model_fps(vrspec))
            scene_seed = int(sc.get("seed") if sc.get("seed") is not None else (sc.get("seed_m") or 1000))

            t_dict = ctx.make_video_task(
                output_filename=out_name,
                prompt=expanded_prompt,
                video_length=frames,
                seed=scene_seed,
                image_start=anchor_path,
                audio_path=wav_path,
                rspec=vrspec,
            )
            t_dict["render_start"] = target_render_start
            t_dict["render_end"] = target_render_end

            valid_hashes = project_task_hashes.get(out_name, ("",))
            t_hash = valid_hashes[0]

            file_exists = out_mp4.exists() and out_mp4.stat().st_size > 50000
            prev_hash = task_hashes.get(out_name)
            changed = (prev_hash is not None) and (prev_hash not in valid_hashes)

            if sync_only and file_exists:
                task_hashes[out_name] = t_hash
                settings_store[out_name] = render_settings_of(t_dict)
                needs_render, reason = False, "SYNCED"
            elif stitch_only:
                needs_render, reason = False, "STITCH-ONLY"
            elif has_scene_filter:
                forced = match_scene_pass(force_scenes, sid, None)
                if not forced:
                    needs_render, reason = False, "SKIPPED (NOT TARGETED)"
                else:
                    settings_reason = settings_change_reason(ctx.ffmpeg, settings_store, out_name, t_dict, out_mp4, file_exists)
                    if not file_exists:
                        needs_render, reason = True, "MISSING FILE (TARGETED)"
                    elif settings_reason:
                        needs_render, reason = True, f"FORCED ({settings_reason})"
                    elif changed:
                        needs_render, reason = True, "FORCED (EDITED)"
                    elif not is_covered:
                        needs_render, reason = True, f"FORCED (BOUNDS NOT COVERED: [{start:.2f}, {end:.2f}] vs [{rendered_start:.2f}, {rendered_end:.2f}])"
                    else:
                        needs_render, reason = True, "FORCED"
            else:
                forced = match_scene_pass(force_scenes, sid, None)
                anchor_newer = file_exists and anchor_path.exists() and (out_mp4.stat().st_mtime < anchor_path.stat().st_mtime)
                settings_reason = settings_change_reason(ctx.ffmpeg, settings_store, out_name, t_dict, out_mp4, file_exists)

                if forced:
                    needs_render, reason = True, "FORCED"
                elif not file_exists:
                    needs_render, reason = True, "MISSING FILE"
                elif anchor_was_regen:
                    needs_render, reason = True, f"ANCHOR {aid} UPDATED"
                elif anchor_newer:
                    needs_render, reason = True, f"ANCHOR {aid} NEWER ON DISK"
                elif settings_reason:
                    needs_render, reason = True, settings_reason
                elif changed:
                    needs_render, reason = True, "PROMPT/SEED/RENDERER/ANCHOR EDITED"
                elif not is_covered:
                    r_str = f"[{rendered_start:.2f}, {rendered_end:.2f}]" if rendered_start is not None else "none"
                    needs_render, reason = True, f"TIMELINE EXCEEDS RENDER ([{start:.2f}, {end:.2f}] not covered by {r_str})"
                else:
                    needs_render, reason = False, "UP-TO-DATE (COVERED BY RENDER)"

                if not needs_render:
                    task_hashes[out_name] = t_hash
                    settings_store[out_name] = render_settings_of(t_dict)

            all_tasks.append((t_dict, t_hash, reason, needs_render))
            scene_meta.append({
                "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                "clip": out_mp4.name, "render_start": actual_render_start, "render_end": actual_render_end,
                "anchor": sc.get("anchor")
            })

        elif mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
            vrid_p1, vrspec_p1 = ctx.resolve_video_renderer(sc, pass_num=1)
            vrid_p2, vrspec_p2 = ctx.resolve_video_renderer(sc, pass_num=2)
            vmodel_p1 = vrspec_p1["model_type"]
            vmodel_p2 = vrspec_p2["model_type"]

            # Overlap calculation
            req_start = max(0.0, start - ctx.head_overlap_s)
            req_end = end + ctx.tail_overlap_s
            req_dur = req_end - req_start
            frames = ctx.valid_video_frames(req_dur, vrspec_p2)
            gen_dur = frames / ctx.model_fps(vrspec_p2)
            target_render_start = req_start
            target_render_end = target_render_start + gen_dur

            res_str = ctx.resolve_resolution(vrspec_p2.get("resolution", ctx.current_resolution))
            out_p1 = f"{sid}_Pass1_Left_{res_str}"
            out_p2 = f"{sid}_Pass2_Final_{res_str}"

            # Fast skip: if targeting specific scene(s) and neither pass of this scene is targeted, skip audio and masks
            if has_scene_filter and not (match_scene_pass(force_scenes, sid, 1) or match_scene_pass(force_scenes, sid, 2)):
                all_tasks.append((
                    {"output_filename": out_p1, "model_type": vmodel_p1, "video_length": frames, "resolution": res_str},
                    "", "SKIPPED (NOT TARGETED)", False,
                ))
                all_tasks.append((
                    {"output_filename": out_p2, "model_type": vmodel_p2, "video_length": frames, "resolution": res_str},
                    "", "SKIPPED (NOT TARGETED)", False,
                ))
                scene_meta.append({
                    "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                    "clip": f"{out_p2}.mp4", "render_start": target_render_start, "render_end": target_render_end,
                    "anchor": sc.get("anchor"),
                })
                continue

            stem_m = sc.get("stem_m") or sc.get("stem_p1") or sc.get("stem") or "male"
            if mode == "duet_method_b":
                stem_f = sc.get("stem_f") or sc.get("stem_p2") or "female"
            elif mode == "gva_male_left_silent_right":
                stem_f = sc.get("stem_f") or sc.get("stem_p2") or "silent"
            else:
                stem_f = sc.get("stem_f") or sc.get("stem_p2") or "silent"

            wav_m = ctx.resolve_scene_audio(sid, stem_m, target_render_start, gen_dur, frames)
            wav_f = ctx.resolve_scene_audio(sid, stem_f, target_render_start, gen_dur, frames)

            split_x = float(sc.get("split_x", 0.50))
            mask_path = ctx.ensure_right_mask(frames, split_x, res_str)

            raw_m = sc.get("prompt_m") or sc.get("prompt") or ""
            raw_f = sc.get("prompt_f") or sc.get("prompt") or ""
            exp_prompt_m = expand_variables(raw_m, ctx.variables)
            exp_prompt_m = resolve_marker_prompt(exp_prompt_m, target_render_start, ctx.markers, fps=ctx.model_fps(vrspec_p1))
            exp_prompt_f = expand_variables(raw_f, ctx.variables)
            exp_prompt_f = resolve_marker_prompt(exp_prompt_f, target_render_start, ctx.markers, fps=ctx.model_fps(vrspec_p2))

            seed_m = int(sc.get("seed_m") if sc.get("seed_m") is not None else (sc.get("seed") or 1000))
            seed_f = int(sc.get("seed_f") if sc.get("seed_f") is not None else (sc.get("seed") or 1000))

            t_p1 = ctx.make_video_task(
                output_filename=out_p1,
                prompt=exp_prompt_m,
                video_length=frames,
                seed=seed_m,
                image_start=anchor_path,
                audio_path=wav_m,
                rspec=vrspec_p1,
            )
            t_p1["render_start"] = target_render_start
            t_p1["render_end"] = target_render_end

            t_p2 = ctx.make_video_task(
                output_filename=out_p2,
                prompt=exp_prompt_f,
                video_length=frames,
                seed=seed_f,
                image_start=anchor_path,
                audio_path=wav_f,
                rspec=vrspec_p2,
                video_guide=ctx.renders_dir / f"{out_p1}.mp4",
                video_mask=mask_path,
            )
            t_p2["render_start"] = target_render_start
            t_p2["render_end"] = target_render_end

            valid_p1 = project_task_hashes.get(out_p1, ("",))
            valid_p2 = project_task_hashes.get(out_p2, ("",))
            h_p1 = valid_p1[0]
            h_p2 = valid_p2[0]

            mp4_p1 = resolve_rendered_clip_path(ctx.renders_dir, out_p1, promote=True)
            mp4_p2 = resolve_rendered_clip_path(ctx.renders_dir, out_p2, promote=True)
            if not mp4_p1.exists():
                legacy_p1 = ctx.renders_dir / f"{sid}_Pass1_Left.mp4"
                if legacy_p1.exists() and legacy_p1.stat().st_size > 50000:
                    dims = probe_video_dimensions(ctx.ffmpeg, legacy_p1)
                    if dims and f"{dims[0]}x{dims[1]}" == res_str:
                        shutil.copy2(str(legacy_p1), str(mp4_p1))
            if not mp4_p2.exists():
                legacy_p2 = ctx.renders_dir / f"{sid}_Pass2_Final.mp4"
                if legacy_p2.exists() and legacy_p2.stat().st_size > 50000:
                    dims = probe_video_dimensions(ctx.ffmpeg, legacy_p2)
                    if dims and f"{dims[0]}x{dims[1]}" == res_str:
                        shutil.copy2(str(legacy_p2), str(mp4_p2))
            ex_p1 = mp4_p1.exists() and mp4_p1.stat().st_size > 50000
            ex_p2 = mp4_p2.exists() and mp4_p2.stat().st_size > 50000
            prev_p1 = task_hashes.get(out_p1)
            prev_p2 = task_hashes.get(out_p2)

            scene_renders = state.setdefault("scene_renders", {})
            cov_p1, r_start_p1, r_end_p1 = check_scene_render_coverage(ctx.ffmpeg, mp4_p1, scene_renders, out_p1, start, end)
            cov_p2, r_start_p2, r_end_p2 = check_scene_render_coverage(ctx.ffmpeg, mp4_p2, scene_renders, out_p2, start, end)

            actual_render_start = r_start_p2 if cov_p2 else target_render_start
            actual_render_end = r_end_p2 if cov_p2 else target_render_end

            if sync_only and ex_p1 and ex_p2:
                task_hashes[out_p1] = h_p1
                task_hashes[out_p2] = h_p2
                settings_store[out_p1] = render_settings_of(t_p1)
                settings_store[out_p2] = render_settings_of(t_p2)
                nr_p1, r_p1 = False, "SYNCED"
                nr_p2, r_p2 = False, "SYNCED"
            elif stitch_only:
                nr_p1, r_p1 = False, "STITCH-ONLY"
                nr_p2, r_p2 = False, "STITCH-ONLY"
            elif has_scene_filter:
                force_p1 = match_scene_pass(force_scenes, sid, 1)
                force_p2 = match_scene_pass(force_scenes, sid, 2)
                set_p1 = settings_change_reason(ctx.ffmpeg, settings_store, out_p1, t_p1, mp4_p1, ex_p1)
                set_p2 = settings_change_reason(ctx.ffmpeg, settings_store, out_p2, t_p2, mp4_p2, ex_p2)

                if force_p1:
                    nr_p1, r_p1 = True, (f"FORCED ({set_p1})" if set_p1 else "FORCED")
                elif force_p2 and not ex_p1:
                    nr_p1, r_p1 = True, "REQUIRED FOR PASS 2"
                elif force_p2 and not cov_p1:
                    nr_p1, r_p1 = True, f"FORCED PASS 1 (BOUNDS NOT COVERED)"
                else:
                    nr_p1, r_p1 = False, "SKIPPED (NOT TARGETED)"

                if force_p2:
                    nr_p2, r_p2 = True, (f"FORCED ({set_p2})" if set_p2 else "FORCED")
                elif nr_p1:
                    nr_p2, r_p2 = True, f"PASS 1 RE-RENDERED ({r_p1})"
                else:
                    nr_p2, r_p2 = False, "SKIPPED (NOT TARGETED)"
            else:
                force_p1 = match_scene_pass(force_scenes, sid, 1)
                force_p2 = match_scene_pass(force_scenes, sid, 2)
                chg_p1 = (prev_p1 is not None) and (prev_p1 not in valid_p1)
                anchor_newer_p1 = ex_p1 and anchor_path.exists() and (mp4_p1.stat().st_mtime < anchor_path.stat().st_mtime)
                set_p1 = settings_change_reason(ctx.ffmpeg, settings_store, out_p1, t_p1, mp4_p1, ex_p1)
                set_p2 = settings_change_reason(ctx.ffmpeg, settings_store, out_p2, t_p2, mp4_p2, ex_p2)

                if force_p1:
                    nr_p1, r_p1 = True, "FORCED"
                elif not ex_p1:
                    nr_p1, r_p1 = True, "MISSING FILE"
                elif anchor_was_regen:
                    nr_p1, r_p1 = True, f"ANCHOR {aid} UPDATED"
                elif anchor_newer_p1:
                    nr_p1, r_p1 = True, f"ANCHOR {aid} NEWER ON DISK"
                elif set_p1:
                    nr_p1, r_p1 = True, set_p1
                elif chg_p1:
                    nr_p1, r_p1 = True, "PASS 1 (prompt_m/seed_m) EDITED"
                elif not cov_p1:
                    r_str = f"[{r_start_p1:.2f}, {r_end_p1:.2f}]" if r_start_p1 is not None else "none"
                    nr_p1, r_p1 = True, f"TIMELINE EXCEEDS RENDER ([{start:.2f}, {end:.2f}] not covered by {r_str})"
                else:
                    nr_p1, r_p1 = False, "UP-TO-DATE (COVERED BY RENDER)"

                chg_p2 = (prev_p2 is not None) and (prev_p2 not in valid_p2)
                if force_p2:
                    nr_p2, r_p2 = True, "FORCED"
                elif nr_p1:
                    nr_p2, r_p2 = True, f"PASS 1 RE-RENDERED ({r_p1})"
                elif not ex_p2:
                    nr_p2, r_p2 = True, "MISSING FILE"
                elif set_p2:
                    nr_p2, r_p2 = True, set_p2
                elif chg_p2:
                    nr_p2, r_p2 = True, "PASS 2 (prompt_f/seed_f/split_x) EDITED"
                elif not cov_p2:
                    r_str = f"[{r_start_p2:.2f}, {r_end_p2:.2f}]" if r_start_p2 is not None else "none"
                    nr_p2, r_p2 = True, f"TIMELINE EXCEEDS RENDER ([{start:.2f}, {end:.2f}] not covered by {r_str})"
                else:
                    nr_p2, r_p2 = False, "UP-TO-DATE (COVERED BY RENDER)"

                if not nr_p1:
                    task_hashes[out_p1] = h_p1
                    settings_store[out_p1] = render_settings_of(t_p1)
                if not nr_p2:
                    task_hashes[out_p2] = h_p2
                    settings_store[out_p2] = render_settings_of(t_p2)

            all_tasks.append((t_p1, h_p1, r_p1, nr_p1))
            all_tasks.append((t_p2, h_p2, r_p2, nr_p2))
            scene_meta.append({
                "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                "clip": mp4_p2.name, "render_start": actual_render_start, "render_end": actual_render_end,
                "anchor": sc.get("anchor")
            })

    ctx.save_state_hashes(state)
    (ctx.jobs_dir / "v3_manifest.json").write_text(json.dumps([t[0] for t in all_tasks], indent=2), encoding="utf-8")
    (ctx.jobs_dir / "v3_timeline.json").write_text(json.dumps(scene_meta, indent=2), encoding="utf-8")

    queue_summary = []
    for idx, (t, t_hash, reason, needs_render) in enumerate(all_tasks, 1):
        queue_summary.append({
            "idx": idx,
            "total": len(all_tasks),
            "filename": f"{t['output_filename']}.mp4",
            "output_filename": t["output_filename"],
            "reason": reason,
            "needs_render": needs_render,
            "status": "queued" if needs_render else "skipped",
            "model_type": t.get("model_type"),
            "video_length": t.get("video_length"),
            "resolution": t.get("resolution"),
        })
    queue_file = ctx.jobs_dir / "v3_queue.json"
    if not (has_scene_filter or no_queue_sync):
        try:
            queue_file.write_text(json.dumps(queue_summary, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _sync_queue():
        if has_scene_filter or no_queue_sync:
            return
        try:
            queue_file.write_text(json.dumps(queue_summary, indent=2), encoding="utf-8")
        except Exception:
            pass

    if sync_only or stitch_only:
        return scene_meta

    pending_count = sum(1 for _, _, _, nr in all_tasks if nr)
    print(f"  -> {pending_count} of {len(all_tasks)} video passes queued for GPU rendering.", flush=True)

    # Initial progressive master assembly (displays all existing clips and anchor placeholders)
    if not has_scene_filter:
        assemble_progressive_master(ctx, scene_meta, log_prefix="  ")

    for idx, (t, t_hash, reason, needs_render) in enumerate(all_tasks, 1):
        out_name = t["output_filename"]
        out_mp4 = ctx.renders_dir / f"{out_name}.mp4"

        # Check dynamic user queue requests from UI
        req_file = ctx.jobs_dir / "queue_requests.json"
        if req_file.exists():
            try:
                dyn_reqs = json.loads(req_file.read_text(encoding="utf-8"))
                matched = False
                for r in list(dyn_reqs):
                    r_clean = str(r).strip()
                    if r_clean and (r_clean in out_name or out_name.startswith(f"{r_clean}_") or out_name == r_clean):
                        matched = True
                        needs_render = True
                        reason = f"QUEUED BY USER"
                        queue_summary[idx - 1]["needs_render"] = True
                        queue_summary[idx - 1]["reason"] = reason
                        dyn_reqs.remove(r)
                        break
                if matched:
                    req_file.write_text(json.dumps(dyn_reqs, indent=2), encoding="utf-8")
            except Exception:
                pass

        if not needs_render:
            queue_summary[idx - 1]["status"] = "skipped"
            _sync_queue()
            print(f"  [{idx:02d}/{len(all_tasks)}] {out_mp4.name} [{reason}] — skipping.", flush=True)
            print(f"[QUEUE_SKIP] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'reason': reason})}", flush=True)
            continue

        if out_mp4.exists():
            try:
                out_mp4.unlink()
            except OSError:
                pass

        if dummy_render:
            t0 = time.time()
            print(
                f"  [{idx:02d}/{len(all_tasks)}] [DUMMY] Simulating render for {out_mp4.name} [{reason}] "
                f"({t.get('video_length')}f, {t.get('resolution')})...",
                flush=True,
            )
            print(
                f"[QUEUE_ACTIVE] {json.dumps({'idx': idx, 'total': len(all_tasks), 'filename': out_mp4.name, 'reason': reason, 'model': t.get('model_type'), 'frames': t.get('video_length'), 'resolution': t.get('resolution')})}",
                flush=True,
            )
            queue_summary[idx - 1]["status"] = "rendering"
            _sync_queue()

            # Emit mock progress steps
            total_dummy_steps = 5
            for step in range(1, total_dummy_steps + 1):
                time.sleep(0.04)
                pct = round((step / total_dummy_steps) * 100, 1)
                print(
                    f"[QUEUE_PROGRESS] {json.dumps({'idx': idx, 'phase': 'Sampling', 'step': step, 'total_steps': total_dummy_steps, 'pct': pct})}",
                    flush=True,
                )

            # Generate mock video with FFmpeg
            frames = t.get("video_length", 107)
            dur = frames / ctx.fps
            res = t.get("resolution") or ctx.current_resolution
            w, h = [int(x) for x in res.split("x")]
            cmd = [
                str(ctx.ffmpeg), "-y",
                "-f", "lavfi", "-i", f"color=c=0x1e293b:s={w}x{h}:r={ctx.fps}:d={dur:.3f}",
            ]
            audio_file = t.get("audio_guide")
            if audio_file and Path(audio_file).exists() and Path(audio_file).stat().st_size > 100:
                cmd.extend(["-i", str(audio_file), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest"])
            else:
                cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest"])

            cmd.extend([
                "-vf", f"drawtext=text='[DUMMY RENDER - {out_name}]':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", f"{dur:.3f}", str(out_mp4),
            ])
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elapsed = round(time.time() - t0, 1)
            print(f"      -> [DUMMY] Generated mock {out_mp4.name} in {elapsed:.1f}s", flush=True)
            print(
                f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'elapsed': elapsed})}",
                flush=True,
            )
            queue_summary[idx - 1]["status"] = "completed"
            queue_summary[idx - 1]["elapsed"] = elapsed
            _sync_queue()

            task_hashes[out_name] = t_hash
            settings_store[out_name] = render_settings_of(t)
            scene_renders = state.setdefault("scene_renders", {})
            scene_renders[out_name] = {
                "render_start": t.get("render_start", 0.0),
                "render_end": t.get("render_end", 0.0),
                "frames": t.get("video_length", 0),
            }
            img_start = t.get("image_start")
            if img_start and Path(img_start).exists():
                aid = extract_anchor_id(Path(img_start).name)
                if aid.startswith("A") and aid[1:].isdigit():
                    state.setdefault("anchor_file_hashes", {})[aid] = _file_sha256(Path(img_start))
            ctx.save_state_hashes(state)

            if "_Pass1_Left" not in out_name and not has_scene_filter:
                assemble_progressive_master(ctx, scene_meta, log_prefix="      ")
            continue

        session = lazy_session.get()
        t0 = time.time()
        print(
            f"  [{idx:02d}/{len(all_tasks)}] Rendering {out_mp4.name} [{reason}] "
            f"(model={t['model_type']}, {t['video_length']}f, vpt={t['video_prompt_type'] or 'S'})...",
            flush=True,
        )
        print(
            f"[QUEUE_ACTIVE] {json.dumps({'idx': idx, 'total': len(all_tasks), 'filename': out_mp4.name, 'reason': reason, 'model': t.get('model_type'), 'frames': t.get('video_length'), 'resolution': t.get('resolution')})}",
            flush=True,
        )
        queue_summary[idx - 1]["status"] = "rendering"
        _sync_queue()

        wangp_task = {k: v for k, v in t.items() if k not in ("render_start", "render_end")}
        job = session.submit_task(wangp_task)
        for event in job.events.iter(timeout=0.5):
            if event.kind == "progress":
                p = event.data
                if p.total_steps and p.current_step in (1, p.total_steps // 2, p.total_steps):
                    print(f"      [{p.phase}] {p.current_step}/{p.total_steps} ({p.progress:.0f}%)", flush=True)
                if p.total_steps:
                    print(
                        f"[QUEUE_PROGRESS] {json.dumps({'idx': idx, 'phase': p.phase, 'step': p.current_step, 'total_steps': p.total_steps, 'pct': round(p.progress, 1)})}",
                        flush=True,
                    )
        res = job.result()
        if not res.success or not res.generated_files:
            queue_summary[idx - 1]["status"] = "failed"
            queue_summary[idx - 1]["error"] = [e.message for e in res.errors]
            _sync_queue()
            print(
                f"[QUEUE_FAIL] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'error': [e.message for e in res.errors]})}",
                flush=True,
            )
            raise RuntimeError(f"Failed rendering {out_mp4.name}: {[e.message for e in res.errors]}")

        gen_path = Path(res.generated_files[0])
        if gen_path.resolve() != out_mp4.resolve():
            shutil.copy2(gen_path, out_mp4)
        elapsed = round(time.time() - t0, 1)
        print(f"      -> Completed {out_mp4.name} in {elapsed:.1f}s", flush=True)
        print(
            f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'elapsed': elapsed})}",
            flush=True,
        )
        queue_summary[idx - 1]["status"] = "completed"
        queue_summary[idx - 1]["elapsed"] = elapsed
        _sync_queue()

        task_hashes[out_name] = t_hash
        settings_store[out_name] = render_settings_of(t)
        scene_renders = state.setdefault("scene_renders", {})
        scene_renders[out_name] = {
            "render_start": t.get("render_start", 0.0),
            "render_end": t.get("render_end", 0.0),
            "frames": t.get("video_length", 0),
        }
        img_start = t.get("image_start")
        if img_start and Path(img_start).exists():
            aid = extract_anchor_id(Path(img_start).name)
            if aid.startswith("A") and aid[1:].isdigit():
                state.setdefault("anchor_file_hashes", {})[aid] = _file_sha256(Path(img_start))
        ctx.save_state_hashes(state)

        # Update progressive master if this pass completed a full scene (not an intermediate pass 1 guide)
        if "_Pass1_Left" not in out_name:
            assemble_progressive_master(ctx, scene_meta, log_prefix="      ")

    return scene_meta


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
            if not strip_jpg.exists() or (src_clip.exists() and strip_jpg.stat().st_mtime < src_clip.stat().st_mtime) or needs_encode:
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-i", str(active_clip),
                        "-vf", f"fps=5/{max(1.0, dur):.3f},scale=-1:144,tile=5x1",
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


def is_project_config(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and "project" in data
    except Exception:
        return False


def resolve_project_config(arg: str | None) -> Path | None:
    if arg:
        raw_p = Path(arg)
        if raw_p.is_file() and is_project_config(raw_p):
            return raw_p.resolve()
        if raw_p.with_suffix(".json").is_file() and is_project_config(raw_p.with_suffix(".json")):
            return raw_p.with_suffix(".json").resolve()

        ws_p = WORKSPACE_ROOT / arg
        if ws_p.is_file() and is_project_config(ws_p):
            return ws_p.resolve()
        if ws_p.with_suffix(".json").is_file() and is_project_config(ws_p.with_suffix(".json")):
            return ws_p.with_suffix(".json").resolve()

        proj_p = WORKSPACE_ROOT / "projects" / arg
        if proj_p.is_file() and is_project_config(proj_p):
            return proj_p.resolve()
        if proj_p.with_suffix(".json").is_file() and is_project_config(proj_p.with_suffix(".json")):
            return proj_p.with_suffix(".json").resolve()

        for cand_dir in [raw_p, ws_p, proj_p]:
            if cand_dir.is_dir():
                jsons = [j for j in sorted(cand_dir.glob("*.json")) if is_project_config(j)]
                if jsons:
                    return jsons[-1].resolve()

        return None

    # Fallback when no arg is given
    cwd_jsons = [j for j in sorted(Path.cwd().glob("*.json")) if is_project_config(j) and j.name not in ("model_profiles.json", "project_template.json")]
    if len(cwd_jsons) == 1:
        return cwd_jsons[0].resolve()

    proj_dir = WORKSPACE_ROOT / "projects"
    if proj_dir.is_dir():
        proj_folders = [d for d in proj_dir.iterdir() if d.is_dir()]
        if len(proj_folders) == 1:
            jsons = [j for j in sorted(proj_folders[0].glob("*.json")) if is_project_config(j)]
            if jsons:
                return jsons[-1].resolve()

    return None


def parse_cli_and_config(argv: list[str]):
    json_arg: str | None = None
    cli_renderer: str | None = None
    cli_video_renderer: str | None = None
    cli_resolution: str | None = None
    cli_profiles: str | None = None
    cli_import_dir: str | None = None
    cli_skip_anchors: bool = False
    other_tokens: list[str] = []

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--config", "-c") and i + 1 < len(argv):
            json_arg = argv[i + 1]
            i += 2
            continue
        if tok in ("--profiles", "--profiles-file", "-p") and i + 1 < len(argv):
            cli_profiles = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--profiles=") or tok.lower().startswith("--profiles-file="):
            cli_profiles = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--renderer", "--anchor-renderer", "-r") and i + 1 < len(argv):
            cli_renderer = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--renderer=") or tok.lower().startswith("--anchor-renderer="):
            cli_renderer = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--video-renderer", "--vrenderer", "-vr") and i + 1 < len(argv):
            cli_video_renderer = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--video-renderer=") or tok.lower().startswith("--vrenderer="):
            cli_video_renderer = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--res", "--resolution") and i + 1 < len(argv):
            cli_resolution = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--res=") or tok.lower().startswith("--resolution="):
            cli_resolution = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--import-anchors", "--import", "-ia") and i + 1 < len(argv):
            cli_import_dir = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--import-anchors=") or tok.lower().startswith("--import="):
            cli_import_dir = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.lower() in ("--skip-anchors", "--no-anchors", "--video-only", "-vo"):
            cli_skip_anchors = True
            i += 1
            continue
        if tok.lower().endswith(".json") and json_arg is None:
            json_arg = tok
            i += 1
            continue
        other_tokens.append(tok)
        i += 1

    if json_arg is None:
        cand = None
        if other_tokens:
            cand = resolve_project_config(other_tokens[0])
            if cand:
                json_path = cand
                other_tokens.pop(0)
        if cand is None:
            cand = resolve_project_config(None)
            if cand:
                json_path = cand
            else:
                raise SystemExit(
                    "Usage: render_video.bat <project_name / config.json> [options / Anchor IDs / Scene IDs]\n"
                    "Example: render_video.bat all_or_nothing --list\n"
                    "         render_video.bat --new my_new_song"
                )
    else:
        cand = resolve_project_config(json_arg)
        if cand:
            json_path = cand
        else:
            raise FileNotFoundError(f"Configuration JSON or project not found: {json_arg}")

    ctx = ProjectContext(json_path, profiles_path=Path(cli_profiles) if cli_profiles else None)
    if cli_renderer is not None:
        ctx.default_anchor_renderer = str(cli_renderer).strip()
    if cli_video_renderer is not None:
        ctx.default_video_renderer = str(cli_video_renderer).strip()
    if cli_resolution is not None:
        ctx.set_resolution(cli_resolution)
    rc = ctx.run_controls

    force_anchors = [str(x) for x in rc.get("force_rerender_anchors", [])]
    force_scenes = [str(x) for x in rc.get("force_rerender_scenes", [])]
    anchors_only = bool(rc.get("anchors_only", False))
    cascade_anchors = bool(rc.get("cascade_anchors", True))
    stitch_only = bool(rc.get("stitch_only", False))
    skip_anchors = bool(rc.get("skip_anchors", False)) or cli_skip_anchors
    sync_only = False
    list_only = False
    dummy_render = (
        bool(os.environ.get("DUMMY_RENDER", "0") in ("1", "true", "yes"))
        or bool(rc.get("dummy_render", False))
    )

    valid_scene_ids = {sc["id"].upper() for sc in ctx.scenes}

    for raw in other_tokens:
        t = raw.strip()
        if not t:
            continue
        tu = t.upper()
        if tu in ("--LIST", "-L", "LIST"):
            list_only = True
        elif tu in ("--DUMMY-RENDER", "--DUMMY", "--MOCK", "-M"):
            dummy_render = True
        elif tu in ("--SYNC-HASHES", "SYNC-HASHES"):
            sync_only = True
        elif tu in ("--STITCH-ONLY", "--STITCH", "STITCH"):
            stitch_only = True
        elif tu in ("--ANCHORS-ONLY", "ANCHORS-ONLY", "ANCHORS"):
            anchors_only = True
        elif tu in ("--SKIP-ANCHORS", "SKIP-ANCHORS", "--VIDEO-ONLY", "VIDEO-ONLY"):
            skip_anchors = True
        elif tu in ("--NO-CASCADE", "NO-CASCADE"):
            cascade_anchors = False
        elif tu in ("--ALL-ANCHORS", "ALL-ANCHORS"):
            force_anchors.append("ALL")
        elif tu in ("--ALL-SCENES", "ALL-SCENES", "--ALL", "ALL"):
            force_scenes.append("ALL")
        elif any(match_anchor_id(tu, sp["filename"]) for sp in ctx.anchors):
            force_anchors.append(tu)
        elif any(_scene_id_matches(tu.split(":", 1)[0], sc_id) for sc_id in valid_scene_ids):
            force_scenes.append(tu)
        else:
            print(f"[Warning] Unrecognized argument '{raw}' (use --list to see valid Anchor/Scene IDs)", flush=True)

    return (
        ctx,
        force_anchors,
        force_scenes,
        anchors_only,
        cascade_anchors,
        stitch_only,
        sync_only,
        list_only,
        cli_import_dir,
        skip_anchors,
        dummy_render,
    )


def init_new_project(target_name: str, force: bool = False):
    target_raw = Path(target_name)
    if target_raw.suffix.lower() == ".json":
        slug = target_raw.stem
        parent_dir = target_raw.parent
    else:
        slug = target_raw.name
        parent_dir = target_raw.parent

    if parent_dir == Path(".") or str(parent_dir) in ("", "."):
        project_dir = (WORKSPACE_ROOT / "projects" / slug).resolve()
    else:
        project_dir = (parent_dir / slug).resolve() if not target_raw.name.endswith(".json") else parent_dir.resolve()

    json_path = project_dir / f"{slug}.json"

    if json_path.exists() and not force:
        print(f"\n[Error] Project '{slug}' already exists at {project_dir}!", flush=True)
        print(f"To overwrite, use: render_video.bat --new {slug} --force\n", flush=True)
        sys.exit(1)

    template_candidates = [
        WORKSPACE_ROOT / "project_template.json",
        Path.cwd() / "project_template.json",
    ]
    template_data = None
    for cand in template_candidates:
        if cand.exists():
            try:
                template_data = json.loads(cand.read_text(encoding="utf-8"))
                break
            except Exception:
                pass

    if template_data is None:
        print("[Error] Could not locate 'project_template.json' template.", flush=True)
        sys.exit(1)

    dirs_to_create = [
        project_dir,
        project_dir / "audio",
        project_dir / "references",
        project_dir / "work",
        project_dir / "work" / "anchors",
        project_dir / "work" / "audio",
        project_dir / "work" / "jobs",
        project_dir / "work" / "masks",
        project_dir / "work" / "renders",
        project_dir / "work" / "trimmed",
        project_dir / "work" / "frames",
    ]
    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)

    audio_readme = project_dir / "audio" / "README.txt"
    if not audio_readme.exists():
        audio_readme.write_text(
            f"Audio Files for Project: {slug}\n"
            "============================================================================\n"
            "Place your song audio files here:\n"
            "  1. master_audio.mp3  - Full song audio used for the final master video\n"
            "  2. lead_vocal.mp3    - Isolated vocal stem used for lipsync alignment\n"
            "  3. backing_track.mp3 - (Optional) Instrumental / backing track\n\n"
            f"Update paths in {slug}.json under 'project.master_audio' and 'project.stems'.\n",
            encoding="utf-8",
        )

    ref_readme = project_dir / "references" / "README.txt"
    if not ref_readme.exists():
        ref_readme.write_text(
            f"Reference Images for Project: {slug}\n"
            "============================================================================\n"
            "Place character reference images and visual style sheets here.\n"
            f"Reference them in {slug}.json under 'models.anchor_shared_settings.image_refs'.\n",
            encoding="utf-8",
        )

    local_bat = project_dir / "render.bat"
    local_bat.write_text(
        "@echo off\n"
        "setlocal\n"
        'cd /d "%~dp0"\n'
        'for %%I in ("%~dp0..\\..") do set "ROOT_DIR=%%~fI"\n'
        f'call "%ROOT_DIR%\\render_video.bat" "%~dp0{slug}.json" %*\n'
        "exit /b %ERRORLEVEL%\n",
        encoding="utf-8",
    )

    clean_name = slug.replace("_", " ").replace("-", " ").title()
    safe_name = re.sub(r"[^\w\-]+", "_", slug).strip("_")

    template_data.setdefault("project", {})["name"] = clean_name
    template_data["project"]["project_dir"] = "."
    template_data["project"]["master_output"] = f"{safe_name}_Master_{{resolution}}_{{fps}}fps.mp4"
    template_data["project"]["master_audio"] = "audio/master_audio.mp3"
    template_data["project"]["stems"] = {
        "lead_vocal": "audio/lead_vocal.mp3",
        "backing_track": "audio/backing_track.mp3",
    }
    template_data["project"]["directories"] = {
        "anchors": "work/anchors",
        "audio_slices": "work/audio",
        "masks": "work/masks",
        "jobs": "work/jobs",
        "renders": "work/renders",
        "trimmed": "work/trimmed",
        "review_strips": "work/frames",
    }
    template_data["profiles_file"] = "model_profiles.json"

    json_path.write_text(json.dumps(template_data, indent=2), encoding="utf-8")

    rel_proj = project_dir.relative_to(WORKSPACE_ROOT) if project_dir.is_relative_to(WORKSPACE_ROOT) else project_dir

    print("\n" + "=" * 76, flush=True)
    print(f"  FRESH PROJECT FOLDER STRUCTURE CREATED: {rel_proj}/", flush=True)
    print("=" * 76, flush=True)
    print(f"  Project Directory : {project_dir}", flush=True)
    print(f"  Configuration     : {json_path.name}", flush=True)
    print(f"  Audio Folder      : {project_dir / 'audio'}", flush=True)
    print(f"  References Folder : {project_dir / 'references'}", flush=True)
    print(f"  Work / Caches     : {project_dir / 'work'}", flush=True)
    print(f"  Local Launcher    : {project_dir / 'render.bat'}", flush=True)
    print("-" * 76, flush=True)
    print("  Next Steps:", flush=True)
    print(f"  1. Place master song audio at: {rel_proj}/audio/master_audio.mp3", flush=True)
    print(f"     (and isolated vocal stem at: {rel_proj}/audio/lead_vocal.mp3)", flush=True)
    print(f"  2. Edit {rel_proj}/{json_path.name} to customize characters and prompts", flush=True)
    print(f"  3. Preview timeline:   render_video.bat {slug} --list", flush=True)
    print(f"  4. Render video:       render_video.bat {slug}", flush=True)
    print(f"     (or inside {rel_proj}/, simply run: render.bat)", flush=True)
    print("=" * 76 + "\n", flush=True)


def main():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.lower() in ("--new", "new", "--init", "init"):
            force = any(x.lower() in ("--force", "-f") for x in args)
            remaining = [x for j, x in enumerate(args) if j != i and not x.startswith("-")]
            target = remaining[0] if remaining else "new_project.json"
            init_new_project(target, force=force)
            return

    t_start = time.time()
    (
        ctx,
        force_anchors,
        force_scenes,
        anchors_only,
        cascade_anchors,
        stitch_only,
        sync_only,
        list_only,
        cli_import_dir,
        skip_anchors,
        dummy_render,
    ) = parse_cli_and_config(args)

    if list_only:
        print_catalog(ctx)
        return

    print(f"[Config] Loaded project '{ctx.project_name}' from: {ctx.json_path}", flush=True)
    state = ctx.load_state_hashes()
    lazy_session = LazyWgpSession(ctx)

    if sync_only:
        run_stage1_anchors(ctx, lazy_session, state, [], sync_only=True)
        run_stage2_scenes(ctx, lazy_session, state, [], set(), sync_only=True)
        print(f"[Sync] Baseline {ctx.state_hash_file} synced with current JSON state.", flush=True)
        return

    if stitch_only:
        print("\n[Stitch] Immediate master restitch requested. Skipping anchor and audio prep...", flush=True)
        timeline_file = ctx.jobs_dir / "v3_timeline.json"
        scene_meta = None
        if timeline_file.is_file():
            try:
                scene_meta = json.loads(timeline_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        if not scene_meta:
            scene_meta = []
            res_str = ctx.resolve_resolution(ctx.current_resolution)
            for sc in ctx.scenes:
                sid = sc["id"]
                start = float(sc["start"])
                end = float(sc["end"])
                edit_dur = round(end - start, 3)
                mode = sc["mode"]
                if mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
                    out_clip = f"{sid}_Pass2_Final_{res_str}.mp4"
                elif mode == "female_solo":
                    out_clip = f"{sid}_female_solo_{res_str}.mp4"
                elif mode == "silent_locked":
                    out_clip = f"{sid}_silent_{res_str}.mp4"
                else:
                    out_clip = f"{sid}_male_solo_{res_str}.mp4"
                scene_meta.append({
                    "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                    "clip": out_clip, "render_start": start, "render_end": end,
                    "anchor": sc.get("anchor"),
                })
        run_stage3_assemble_master(ctx, scene_meta)
        print(f"\n[Stitch] Master video restitched in {time.time() - t_start:.1f}s.", flush=True)
        return

    imported_anchors: set[str] = set()
    if cli_import_dir:
        imported_anchors = import_anchors_from_dir(ctx, cli_import_dir, state)

    if skip_anchors or (force_scenes and not force_anchors):
        print("\n[Anchors] Skipping Stage 1 anchor checks (single-scene render mode).", flush=True)
        regen_anchors = set(imported_anchors)
    else:
        regen_anchors = run_stage1_anchors(ctx, lazy_session, state, force_anchors, dummy_render=dummy_render).union(imported_anchors)

    if anchors_only:
        print(f"\n[Anchors-Only Mode] Finished anchor processing in {time.time() - t_start:.1f}s.", flush=True)
        print(f"Preview your updated anchors in: {ctx.anchors_dir}", flush=True)
        return

    scene_meta = run_stage2_scenes(
        ctx,
        lazy_session,
        state,
        force_scenes,
        regen_anchors,
        cascade_anchors=cascade_anchors,
        stitch_only=stitch_only,
        dummy_render=dummy_render,
        no_queue_sync=bool(force_scenes),
    )
    run_stage3_assemble_master(ctx, scene_meta)
    print(f"\nPipeline finished in {(time.time() - t_start) / 60:.1f} minutes.", flush=True)


if __name__ == "__main__":
    main()
