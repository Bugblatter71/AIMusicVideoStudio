import copy
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .media_utils import (
    WORKSPACE_ROOT,
    _file_sha256,
    auto_detect_wgp_root,
    find_local_config,
    resolve_ffmpeg_path,
    resolve_path,
)
from .prompts import (
    adapt_video_prompt,
    expand_variables,
    extract_anchor_id,
    resolve_marker_prompt,
)


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
        self.anchors = raw.get("anchors") or raw.get("anchor_prompts") or []
        self.scenes = raw.get("scenes", [])
        self.no_stitch = False
        self.no_queue_sync = False

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
            raw_name = ref_or_sp.get("filename") or ref_or_sp.get("id") or ""
            if raw_name and not Path(raw_name).suffix:
                raw_name = f"{raw_name}.jpg"
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
        mtype = rspec.get("model_type", "minimax_h3_fl2va_pruned_pdd")
        is_ref2va = (
            str(rspec.get("frames_formula", "")).lower() == "h3_ref2va"
            or "ref2va" in mtype.lower()
            or "fasth3" in mtype.lower()
            or "singularity" in mtype.lower()
        )
        raw_apt = rspec.get("audio_prompt_type", vshared.get("audio_prompt_type", ""))
        if is_ref2va and raw_apt == "A":
            raw_apt = "AS"
        task = {
            "model_type": mtype,
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
            "audio_prompt_type": raw_apt,
            "image_start": str(image_start),
            "audio_guide": str(audio_path) if "A" in raw_apt else "",
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

    def make_anchor_task(
        self,
        session,
        rspec: dict,
        output_filename: str,
        prompt: str,
        seed: int = 42,
        resolution: str | None = None,
        sp: dict | None = None,
        image_refs: list | None = None,
    ) -> dict:
        sp = sp or {}
        shared_cfg = self.anchor_shared_cfg
        model_type = rspec["model_type"]
        res_val = resolution or sp.get("resolution", rspec.get("resolution", shared_cfg.get("resolution", "1664x960")))
        if "x" in str(res_val):
            try:
                rw, rh = [int(x) for x in str(res_val).lower().split("x", 1)]
                if "flux3" in model_type.lower() and (rw % 16 != 0 or rh % 16 != 0):
                    rw = max(64, ((rw + 8) // 16) * 16)
                    rh = max(64, ((rh + 8) // 16) * 16)
                    res_val = f"{rw}x{rh}"
            except ValueError:
                pass
        refs = self.anchor_refs if image_refs is None else image_refs
        has_refs = bool(refs)

        cfg = session.get_default_settings(model_type)
        cfg["model_type"] = model_type
        cfg["image_mode"] = rspec.get("image_mode", shared_cfg.get("image_mode", 1))
        cfg["resolution"] = res_val
        cfg["seed"] = seed
        cfg["output_filename"] = output_filename
        cfg["remove_background_images_ref"] = rspec.get(
            "remove_background_images_ref", shared_cfg.get("remove_background_images_ref", 0)
        )
        if has_refs:
            cfg["video_prompt_type"] = rspec.get("video_prompt_type", shared_cfg.get("video_prompt_type", "I"))
            cfg["image_refs"] = list(refs)
        else:
            cfg["video_prompt_type"] = ""
            cfg["image_refs"] = None

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
        cfg["prompt"] = prompt
        return cfg


from pipeline.scene_ops import (
    derive_split_scene_ids,
    split_scene_in_project_data,
    delete_scene_in_project_data,
)

