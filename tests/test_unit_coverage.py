"""Comprehensive unit and HTTP API tests closing backend coverage gaps.

Covers stage1_anchors, stage3_master, cli, characters, upscale, server_media,
server_routes, and server_queue edge cases hermetically in temp directories.
Uses in-process StudioRequestHandler invocation so all unit tests run cleanly
without requiring TCP port binding or live GPU renders.
"""

from __future__ import annotations

import base64
import email.message
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_ROOT / "work"
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

from pipeline import cli as pipeline_cli
from pipeline.characters import (
    build_tag_lookup,
    compile_character_prompt,
    crop_reference_from_clip,
    save_uploaded_reference_image,
)
from pipeline.cli import (
    _run_upscale_cli,
    init_new_project,
    is_project_config,
    parse_cli_and_config,
    resolve_project_config,
)
from pipeline.context import ProjectContext
from pipeline.media_utils import resolve_ffmpeg_path
from pipeline.stage1_anchors import (
    compile_anchor_prompt_and_refs,
    import_anchors_from_dir,
    match_anchor_id,
    run_stage1_anchors,
)
from pipeline.stage2_hashes import compute_project_task_hashes
from pipeline.stage3_master import (
    assemble_progressive_master,
    build_stage3_ffmpeg_extra_filters,
    build_stage3_scale_filters,
    resolve_scene_fade_filters,
)
from pipeline.upscale import resolve_scene_canonical_stem, validate_late_upsampling_method
from pipeline.wgp_bridge import LazyWgpSession
from ui import server
from ui.server_routes import StudioRequestHandler

_MIN_1X1_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\x00\x00\x00"
    b"\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_silent_wav(path: Path, duration_s: float = 2.0, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nframes = int(duration_s * sr)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * nframes)


def _make_dummy_mp4(ffmpeg_bin: Path, path: Path, duration_s: float = 2.0, w: int = 320, h: int = 180) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_bin),
        "-y",
        "-f", "lavfi",
        "-i", f"color=c=navy:s={w}x{h}:d={duration_s}:r=25",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=mono",
        "-t", str(duration_s),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _create_fixture_project(
    root_dir: Path,
    slug: str = "unit_proj",
    *,
    with_anchors: bool = True,
    fps: int = 25,
) -> tuple[Path, Path]:
    proj_dir = root_dir / slug
    for sub in (
        "audio",
        "references",
        "work/anchors",
        "work/audio",
        "work/jobs",
        "work/renders",
        "work/trimmed",
        "work/frames",
    ):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)

    _write_silent_wav(proj_dir / "audio" / "master.wav", duration_s=4.0)
    _write_silent_wav(proj_dir / "audio" / "male.wav", duration_s=4.0)

    if with_anchors:
        (proj_dir / "work" / "anchors" / "A01_Lead.jpg").write_bytes(b"A01_JPG_BYTES_" + b"\x01" * 64)
        (proj_dir / "work" / "anchors" / "A02_Wide.jpg").write_bytes(b"A02_JPG_BYTES_" + b"\x02" * 64)

    cfg = {
        "project": {
            "name": f"Project {slug}",
            "project_dir": ".",
            "master_audio": "audio/master.wav",
            "stems": {"male": "audio/male.wav"},
            "master_output": "{project_name}_Master_{resolution}_{fps}fps.mp4",
            "master_resolution": "640x360",
            "fps": fps,
            "total_duration": 4.0,
            "fade_out_last_scene_seconds": 0.5,
            "directories": {
                "anchors": "work/anchors",
                "audio_slices": "work/audio",
                "masks": "work/masks",
                "jobs": "work/jobs",
                "renders": "work/renders",
                "trimmed": "work/trimmed",
                "review_strips": "work/frames",
            },
        },
        "models": {
            "default_anchor_renderer": 3,
            "default_video_renderer": 1,
            "default_video_resolution": 1,
            "anchor_renderers": {
                "3": {"model_type": "z_image", "name": "Z-Image", "num_inference_steps": 6, "guidance_scale": 1.0}
            },
            "video_renderers": {
                "1": {"model_type": "ltx23_i2v_22B", "name": "LTX 2.3"}
            },
            "video_resolutions": {
                "1": {"resolution": "832x480", "name": "480p Fast"}
            },
            "video_shared_settings": {"negative_prompt": "split-screen"},
            "anchor_shared_settings": {"image_refs": []},
        },
        "variables": {"LEAD_SINGER": "young male singer in dark jacket"},
        "markers": [],
        "anchors": [
            {"id": "A01", "filename": "A01_Lead.jpg", "seed": 1001, "prompt": "Close portrait of {LEAD_SINGER}"},
            {"id": "A02", "filename": "A02_Wide.jpg", "seed": 1002, "prompt": "Wide stage shot of {LEAD_SINGER}"},
        ],
        "scenes": [
            {
                "id": "S01",
                "start": 0.0,
                "end": 2.0,
                "mode": "solo",
                "stem": "male",
                "anchor": "A01_Lead.jpg",
                "seed": 2001,
                "prompt": "{LEAD_SINGER} singing earnestly under spotlight",
            },
            {
                "id": "S02",
                "start": 2.0,
                "end": 4.0,
                "mode": "silent_locked",
                "anchor": "A02_Wide.jpg",
                "seed": 2002,
                "prompt": "Slow pan across atmospheric stage",
            },
        ],
    }
    cfg_path = proj_dir / f"{slug}.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return proj_dir, cfg_path


def _call_handler(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Invokes StudioRequestHandler cleanly in-process without opening TCP sockets."""
    raw_body = b""
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = str(v)
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        msg["Content-Type"] = "application/json"
        msg["Content-Length"] = str(len(raw_body))

    h = StudioRequestHandler.__new__(StudioRequestHandler)
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.headers = msg
    h.rfile = io.BytesIO(raw_body)
    h.wfile = io.BytesIO()
    status_holder = [200]
    resp_headers: dict[str, str] = {}

    h.send_response = lambda code, message=None: status_holder.__setitem__(0, int(code))
    h.send_error = lambda code, message=None, explain=None: status_holder.__setitem__(0, int(code))
    h.send_header = lambda key, val: resp_headers.__setitem__(key, str(val))
    h.end_headers = lambda: None
    h.log_message = lambda *args, **kwargs: None

    getattr(h, f"do_{method}")()
    return status_holder[0], resp_headers, h.wfile.getvalue()


class TestStage1AnchorsCoverage(unittest.TestCase):
    def test_01_match_anchor_id_exact_and_subletter(self):
        self.assertTrue(match_anchor_id("A01", "A01_Lead.jpg"))
        self.assertTrue(match_anchor_id("A1", "A01_Lead.jpg"))
        self.assertTrue(match_anchor_id("A05a", "A05a_Strings.jpg"))
        self.assertFalse(match_anchor_id("A05", "A05a_Strings.jpg"))
        self.assertFalse(match_anchor_id("A05a", "A05_Stage.jpg"))
        self.assertFalse(match_anchor_id("A02", "A01_Lead.jpg"))

    def test_02_run_stage1_anchors_dummy_sync_and_change_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg_path = _create_fixture_project(Path(tmp), with_anchors=False)
            ctx = ProjectContext(cfg_path)
            lazy = LazyWgpSession(ctx)
            state = {"anchor_file_hashes": {}, "anchor_tasks": {}}

            # Initial dummy render generates both anchors and records hashes
            mod1 = run_stage1_anchors(ctx, lazy, state, force_anchors=[], dummy_render=True)
            self.assertTrue({"A01", "A02"}.issubset(mod1))
            a01 = ctx.anchors_dir / "A01_Lead.jpg"
            a02 = ctx.anchors_dir / "A02_Wide.jpg"
            self.assertTrue(a01.is_file() and a02.is_file())
            self.assertIn("A01", state["anchor_file_hashes"])

            # Second pass without changes skips both
            mod2 = run_stage1_anchors(ctx, lazy, state, force_anchors=[], dummy_render=True)
            self.assertEqual(mod2, set())

            # External/manual edit on disk flags A01 as modified in cascade without overwriting bytes
            edited_bytes = a01.read_bytes() + b"EDITED"
            a01.write_bytes(edited_bytes)
            mod3 = run_stage1_anchors(ctx, lazy, state, force_anchors=[], dummy_render=True)
            self.assertIn("A01", mod3)
            self.assertNotIn("A02", mod3)
            self.assertEqual(a01.read_bytes(), edited_bytes)

            # Force rendering A02 updates A02 only
            mod4 = run_stage1_anchors(ctx, lazy, state, force_anchors=["A02"], dummy_render=True)
            self.assertIn("A02", mod4)
            self.assertNotIn("A01", mod4)

    def test_03_import_anchors_from_dir_and_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            _, cfg_path = _create_fixture_project(tmp_p, with_anchors=False)
            ctx = ProjectContext(cfg_path)
            state = {"anchor_file_hashes": {}}

            src_dir = tmp_p / "import_src"
            src_dir.mkdir()
            (src_dir / "A01_Imported.png").write_bytes(_MIN_1X1_PNG)
            (src_dir / "A02_Imported.jpg").write_bytes(b"CUSTOM_JPG_DATA")

            modified = import_anchors_from_dir(ctx, str(src_dir), state)
            self.assertEqual(modified, {"A01_Lead.jpg", "A02_Wide.jpg"})
            self.assertTrue((ctx.anchors_dir / "A01_Lead.jpg").is_file())
            self.assertEqual((ctx.anchors_dir / "A02_Wide.jpg").read_bytes(), b"CUSTOM_JPG_DATA")

            # Missing source directory safely returns empty set
            self.assertEqual(import_anchors_from_dir(ctx, str(tmp_p / "does_not_exist"), state), set())


class TestStage3MasterCoverage(unittest.TestCase):
    def test_04_build_stage3_filters_and_scale_alignment(self):
        pre, post = build_stage3_ffmpeg_extra_filters({
            "deband": True,
            "denoise": 2.0,
            "cas": 0.4,
            "eq": {"brightness": 0.1, "contrast": 1.15, "saturation": 1.1, "gamma": 1.05},
        })
        self.assertTrue(any("hqdn3d" in f for f in pre))
        self.assertTrue(any("deband" in f for f in post))
        self.assertTrue(any("cas=" in f for f in post))
        self.assertTrue(any("eq=" in f for f in post))

        top_filters = build_stage3_scale_filters("640:360", preserve_aspect=True, src_dims=(480, 480), crop_align="top")
        self.assertGreaterEqual(len(top_filters), 1)
        self.assertTrue(any("crop=640:360" in f for f in top_filters))

    def test_05_assemble_progressive_master_real_ffmpeg_fade_and_cache(self):
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg_path = _create_fixture_project(Path(tmp))
            ctx = ProjectContext(cfg_path)
            s01_mp4 = ctx.renders_dir / "S01_solo_832x480.mp4"
            _make_dummy_mp4(ffmpeg_bin, s01_mp4, duration_s=2.0)

            scene_meta = [
                {"id": "S01", "start": 0.0, "end": 2.0, "clip": s01_mp4, "mode": "solo"},
                {"id": "S02", "start": 2.0, "end": 4.0, "clip": ctx.renders_dir / "S02_silent_832x480.mp4", "mode": "silent_locked"},
            ]
            ok1 = assemble_progressive_master(ctx, scene_meta)
            self.assertTrue(ok1)
            self.assertTrue(ctx.master_output.is_file())
            self.assertGreater(ctx.master_output.stat().st_size, 5000)

            state_data = ctx.load_state_hashes()
            self.assertIn("trim_cache", state_data)
            self.assertTrue(any(k.startswith("00_S01_") for k in state_data["trim_cache"]))

            # Second identical run succeeds cleanly
            ok2 = assemble_progressive_master(ctx, scene_meta)
            self.assertTrue(ok2)

            # Changing fade_out invalidates cache and rebuilds both placeholder and rendered last-scene trims
            ph_key = next(k for k in state_data["trim_cache"] if k.startswith("01_S02_"))
            ph_sig_before = state_data["trim_cache"][ph_key]
            ctx.raw["project"]["fade_out_last_scene_seconds"] = 0.0
            ctx.fade_out_last_s = 0.0
            ok3 = assemble_progressive_master(ctx, scene_meta)
            self.assertTrue(ok3)
            state_after_ph = ctx.load_state_hashes()
            self.assertNotEqual(state_after_ph["trim_cache"][ph_key], ph_sig_before)

            # Rendered clip (>50KB) as last scene also re-encodes when fade_out_last_s changes
            rendered_mp4 = ctx.renders_dir / "S01_rendered_640x360.mp4"
            subprocess.run(
                [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=2.0:r=25",
                 "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2.0",
                 "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", "-c:a", "aac", str(rendered_mp4)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            single_meta = [{"id": "S01", "start": 0.0, "end": 2.0, "clip": rendered_mp4, "mode": "solo"}]
            ctx.fade_out_last_s = 0.5
            self.assertTrue(assemble_progressive_master(ctx, single_meta))
            st_r1 = ctx.load_state_hashes()
            sig_r1 = st_r1["trim_cache"]["00_S01_640x360.mp4"]
            dst_r = ctx.trimmed_dir / "00_S01_640x360.mp4"
            mt1 = dst_r.stat().st_mtime_ns
            time.sleep(0.05)
            ctx.fade_out_last_s = 1.5
            self.assertTrue(assemble_progressive_master(ctx, single_meta))
            st_r2 = ctx.load_state_hashes()
            self.assertNotEqual(st_r2["trim_cache"]["00_S01_640x360.mp4"], sig_r1)
            self.assertGreater(dst_r.stat().st_mtime_ns, mt1)

    def test_05b_fade_out_end_seconds_and_tv_color_range(self):
        # Unit checks for resolve_scene_fade_filters (default, inside-scene, cross-boundary, post-fade black)
        sig_mid, filt_mid = resolve_scene_fade_filters(1.5, 18.0, 10.0, 22.0, 22.0)
        self.assertEqual(sig_mid, "fade6.500_1.500")
        self.assertEqual(filt_mid, ["fade=t=out:st=6.500:d=1.500"])
        sig_c1, _ = resolve_scene_fade_filters(2.0, 3.0, 0.0, 2.0, 5.0)
        sig_c2, filt_c2 = resolve_scene_fade_filters(2.0, 3.0, 2.0, 4.0, 5.0)
        sig_c3, filt_c3 = resolve_scene_fade_filters(2.0, 3.0, 4.0, 5.0, 5.0)
        self.assertEqual(sig_c1, "fade1.000_2.000")
        self.assertTrue(sig_c2.startswith("fadecont1.000_2.000") or sig_c2 == "fadecont1.000_2.000")
        self.assertTrue(any("setpts=PTS-STARTPTS+1.000000/TB" in f for f in filt_c2))
        self.assertEqual(sig_c3, "black")
        self.assertEqual(filt_c3, ["drawbox=c=black:t=fill"])

        # End-to-end FFmpeg check: JPG placeholder S01 (0..2s) -> white rendered S02 (2..5s) with fade complete at 3.5s
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        ffprobe_bin = ffmpeg_bin.with_name("ffprobe.exe" if ffmpeg_bin.name.lower().endswith(".exe") else "ffprobe")
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg_path = _create_fixture_project(Path(tmp))
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw["project"]["fade_out_last_scene_seconds"] = 1.0
            raw["project"]["fade_out_end_seconds"] = 3.5
            raw["scenes"][1]["end"] = 4.0
            cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            ctx = ProjectContext(cfg_path)
            self.assertEqual(ctx.fade_out_end_s, 3.5)

            subprocess.run(
                [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x64", "-frames:v", "1", str(ctx.anchors_dir / "A01_Lead.jpg")],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            s02_mp4 = ctx.renders_dir / "S02_silent_832x480.mp4"
            subprocess.run(
                [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "color=c=white:s=640x360:d=3.0:r=24",
                 "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "3.0",
                 "-vf", "noise=alls=8:allf=t,format=yuv420p",
                 "-c:v", "libx264", "-crf", "12", "-color_range", "tv", "-pix_fmt", "yuv420p", "-c:a", "aac", str(s02_mp4)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            scene_meta = [
                {"id": "S01", "start": 0.0, "end": 2.0, "clip": ctx.renders_dir / "S01_solo_832x480.mp4", "mode": "solo", "anchor": "A01_Lead.jpg"},
                {"id": "S02", "start": 2.0, "end": 4.0, "clip": s02_mp4, "mode": "silent_locked"},
            ]
            self.assertTrue(assemble_progressive_master(ctx, scene_meta))
            # Placeholders and master must stay on uniform yuv420p tv range (never yuvj420p/pc grey lift)
            ph_clip = next(ctx.trimmed_dir.glob("00_S01_placeholder_*.mp4"))
            for probe_tgt in (ph_clip, ctx.master_output):
                info = subprocess.check_output(
                    [str(ffprobe_bin), "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=pix_fmt,color_range", "-of", "csv=p=0", str(probe_tgt)],
                    text=True,
                ).strip()
                self.assertEqual(info, "yuv420p,tv")

            def _mean_rgb_at(ts: str) -> float:
                buf = subprocess.check_output(
                    [str(ffmpeg_bin), "-v", "error", "-ss", ts, "-i", str(ctx.master_output),
                     "-vframes", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
                )
                return sum(buf) / len(buf)

            self.assertGreater(_mean_rgb_at("2.2"), 200.0)
            self.assertLess(_mean_rgb_at("3.6"), 1.5)
            self.assertLess(_mean_rgb_at("3.9"), 1.5)

            html_txt = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
            js_txt = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "anchors_and_settings.js").read_text(encoding="utf-8")
            self.assertIn("cfgFadeOutEnd", html_txt)
            self.assertIn("fade_out_end_seconds", js_txt)
            self.assertIn('"cfgFadeOutEnd"', js_txt)

    def test_06_assemble_progressive_master_locked_output_returns_false(self):
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg_path = _create_fixture_project(Path(tmp))
            ctx = ProjectContext(cfg_path)
            s01_mp4 = ctx.renders_dir / "S01_solo_832x480.mp4"
            _make_dummy_mp4(ffmpeg_bin, s01_mp4, duration_s=2.0)

            scene_meta = [
                {"id": "S01", "start": 0.0, "end": 2.0, "clip": s01_mp4, "mode": "solo"},
            ]
            buf = io.StringIO()
            with mock.patch("pipeline.stage3_master.shutil.move", side_effect=PermissionError("Locked in player")):
                with mock.patch("sys.stdout", buf):
                    ok = assemble_progressive_master(ctx, scene_meta)
            self.assertFalse(ok)
            self.assertIn("[MASTER_FILE_LOCKED]", buf.getvalue())


class TestCliCoverage(unittest.TestCase):
    def test_07_is_project_config_and_resolve_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="my_clip")
            non_proj = tmp_p / "profiles.json"
            non_proj.write_text(json.dumps({"video_renderers": {}}), encoding="utf-8")

            self.assertTrue(is_project_config(cfg_path))
            self.assertFalse(is_project_config(non_proj))
            self.assertFalse(is_project_config(tmp_p / "missing.json"))

            self.assertEqual(resolve_project_config(str(cfg_path)), cfg_path.resolve())
            self.assertEqual(resolve_project_config(str(cfg_path.with_suffix(""))), cfg_path.resolve())
            self.assertEqual(resolve_project_config(str(proj_dir)), cfg_path.resolve())

    def test_08_parse_cli_and_config_flags_and_targeted_scenes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg_path = _create_fixture_project(Path(tmp))
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
            ) = parse_cli_and_config([
                "--config", str(cfg_path),
                "--skip-anchors",
                "--no-stitch",
                "--no-queue-sync",
                "--dummy",
                "S01",
                "A01",
            ])
            self.assertTrue(skip_anchors)
            self.assertTrue(ctx.no_stitch)
            self.assertTrue(ctx.no_queue_sync)
            self.assertTrue(dummy_render)
            self.assertEqual(force_scenes, ["S01"])
            self.assertEqual(force_anchors, ["A01"])
            self.assertFalse(anchors_only)
            self.assertTrue(cascade_anchors)
            self.assertFalse(stitch_only)
            self.assertFalse(sync_only)
            self.assertFalse(list_only)
            self.assertIsNone(cli_import_dir)

    def test_09_init_new_project_creates_structure_and_blocks_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fresh_vid"
            self.assertIsNone(init_new_project(str(target)))
            expected_json = target.resolve() / "fresh_vid.json"
            self.assertTrue(expected_json.is_file())
            self.assertTrue((target / "audio").is_dir())
            self.assertTrue((target / "references").is_dir())
            self.assertEqual(json.loads(expected_json.read_text(encoding="utf-8"))["project"]["name"], "Fresh Vid")

            with self.assertRaises(SystemExit) as cm:
                init_new_project(str(target), force=False)
            self.assertEqual(cm.exception.code, 1)

            # Overwrite with force=True succeeds cleanly
            init_new_project(str(target), force=True)
            self.assertTrue(expected_json.is_file())

    def test_10_run_upscale_cli_validation_and_dummy(self):
        self.assertFalse(_run_upscale_cli(["only_config.json"]))
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg_path = _create_fixture_project(Path(tmp))
            ctx = ProjectContext(cfg_path)
            _make_dummy_mp4(ffmpeg_bin, ctx.renders_dir / "S01_solo_832x480.mp4", duration_s=1.0)
            ok = _run_upscale_cli([str(cfg_path), "upscale", "S01", "--method", "flashvsrplus*2", "--dummy"])
            self.assertTrue(ok)


class TestUpscaleAndCharactersCoverage(unittest.TestCase):
    def test_11_validate_late_upsampling_and_canonical_stem(self):
        self.assertIsNotNone(validate_late_upsampling_method("", ai_refiner="", film_grain_intensity=0.0))
        self.assertIn("Stage 2", validate_late_upsampling_method("h3_vae*2"))
        self.assertIn("Unsupported", validate_late_upsampling_method("unknown_upscaler*4"))
        self.assertIsNone(validate_late_upsampling_method("flashvsrplus*2"))
        self.assertIsNone(validate_late_upsampling_method("", ai_refiner="seedvr2*1"))
        self.assertIsNone(validate_late_upsampling_method("", film_grain_intensity=0.2))

        self.assertEqual(resolve_scene_canonical_stem({"id": "S02", "mode": "duet_method_b"}, None, "832x480"), ("S02_Pass2_Final_832x480", True, 2))
        self.assertEqual(resolve_scene_canonical_stem({"id": "S03", "mode": "duet_method_a"}, None, "832x480"), ("S03_duet_a_832x480", False, None))
        self.assertEqual(resolve_scene_canonical_stem({"id": "S04", "mode": "silent_locked"}, None, "832x480"), ("S04_silent_832x480", False, None))

    def test_12_characters_helpers_and_rest_api(self):
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            tmp_p = tmp_root / "projects"
            tmp_p.mkdir()
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="char_proj")

            # Upload reference image & test crop from clip
            rel_img = save_uploaded_reference_image(proj_dir, "char_male", "default", "pose.png", _MIN_1X1_PNG)
            self.assertTrue((proj_dir / rel_img).is_file())
            self.assertEqual(rel_img, "references/char_male/pose.png")

            clip_path = proj_dir / "work" / "renders" / "S01_solo_832x480.mp4"
            _make_dummy_mp4(ffmpeg_bin, clip_path, duration_s=1.0)
            rel_crop = crop_reference_from_clip(proj_dir, "S01_solo_832x480.mp4", 0.2, None, "references/char_male/crop.jpg")
            self.assertTrue((proj_dir / rel_crop).is_file())

            # Compile character @tag prompt via real build_tag_lookup
            chars_seed = {
                "char_male": {
                    "id": "char_male",
                    "name": "Male",
                    "kind": "character",
                    "default_look": "default",
                    "physical_traits": "a brooding male vocalist",
                    "looks": {
                        "default": {
                            "tag": "@male",
                            "name": "Default",
                            "description": "wearing dark concert coat",
                            "legacy_prompt_text": "a brooding male vocalist",
                            "image_refs": [rel_img],
                        }
                    },
                }
            }
            tag_lookup = build_tag_lookup(chars_seed, proj_dir)
            expanded_txt, refs_off = compile_character_prompt("@male singing on stage", tag_lookup, supports_refs=False)
            self.assertIn("brooding male vocalist", expanded_txt)
            self.assertEqual(refs_off, [])
            expanded_ref, refs_on = compile_character_prompt("@male singing on stage", tag_lookup, supports_refs=True)
            self.assertGreaterEqual(len(refs_on), 1)
            self.assertIn("<Picture 1>", expanded_ref)

            with mock.patch.object(server, "PROJECTS_DIR", tmp_p):
                st, _, raw = _call_handler("GET", "/api/project/char_proj/characters")
                self.assertEqual(st, 200)
                self.assertIn("characters", json.loads(raw.decode("utf-8")))

                chars_new = {"char_maya": {"id": "char_maya", "name": "Maya", "tag": "@maya", "description": "violinist", "looks": {"default": {"images": []}}}}
                st_save, _, raw_save = _call_handler(
                    "POST",
                    "/api/project/char_proj/characters/save",
                    body={"slug": "char_proj", "characters": chars_new},
                )
                self.assertEqual(st_save, 200)
                self.assertTrue(json.loads(raw_save.decode("utf-8")).get("success"))
                self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["characters"]["char_maya"]["name"], "Maya")
                self.assertTrue((tmp_root / "backup").is_dir() and any((tmp_root / "backup").glob("char_proj_*.json")))

                st_bad, _, _ = _call_handler("POST", "/api/project/char_proj/characters/nonexistent", body={"slug": "char_proj"})
                self.assertEqual(st_bad, 404)


class TestServerMediaAndRoutesCoverage(unittest.TestCase):
    def test_13_server_media_range_requests_security_and_subletter_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            proj_dir, _ = _create_fixture_project(tmp_p, slug="media_proj")
            a05_bytes = b"A05_MAIN_CONTENT"
            a05a_bytes = b"A05A_SUBLETTER_CONTENT"
            (proj_dir / "work" / "anchors" / "A05_Main.jpg").write_bytes(a05_bytes)
            (proj_dir / "work" / "anchors" / "A05a_Sub.jpg").write_bytes(a05a_bytes)
            (proj_dir / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")

            with mock.patch.object(server, "PROJECTS_DIR", tmp_p):
                st_r1, hdr_r1, body_r1 = _call_handler(
                    "GET",
                    "/media/media_proj/audio/audio/master.wav",
                    headers={"Range": "bytes=0-99"},
                )
                self.assertEqual(st_r1, 206)
                self.assertEqual(len(body_r1), 100)
                self.assertIn("bytes 0-99/", hdr_r1.get("Content-Range", ""))

                st_r2, _, body_r2 = _call_handler(
                    "GET",
                    "/media/media_proj/audio/audio/master.wav",
                    headers={"Range": "bytes=100-"},
                )
                self.assertEqual(st_r2, 206)
                self.assertGreater(len(body_r2), 1000)

                st_trav, _, _ = _call_handler("GET", "/media/media_proj/ref/../secret.txt")
                self.assertIn(st_trav, (400, 403, 404))

                st_a, _, body_a = _call_handler("GET", "/media/media_proj/anchor/A05a")
                self.assertEqual(st_a, 200)
                self.assertEqual(body_a, a05a_bytes)

                st_miss, _, _ = _call_handler("GET", "/media/media_proj/clip/NO_SUCH.mp4")
                self.assertEqual(st_miss, 404)

    def test_14_server_routes_projects_dummy_mode_put_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            tmp_p = tmp_root / "projects"
            tmp_p.mkdir()
            shutil.copy(WORKSPACE_ROOT / "project_template.json", tmp_root / "project_template.json")
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="routes_proj")
            orig_dummy = server.dummy_render_mode
            try:
                with mock.patch.object(server, "PROJECTS_DIR", tmp_p), \
                     mock.patch.object(server, "BACKUP_DIR", tmp_root / "backup"), \
                     mock.patch.object(pipeline_cli, "WORKSPACE_ROOT", tmp_root), \
                     mock.patch.object(server, "run_queue_worker") as m_run_q:
                    st_list, _, raw_list = _call_handler("GET", "/api/projects")
                    self.assertEqual(st_list, 200)
                    self.assertTrue(any(p["slug"] == "routes_proj" for p in json.loads(raw_list)["projects"]))

                    st_new, _, _ = _call_handler("POST", "/api/projects/new", body={"name": "brand_new_proj"})
                    self.assertEqual(st_new, 200)
                    self.assertTrue((tmp_p / "brand_new_proj" / "brand_new_proj.json").is_file())
                    st_dup, _, _ = _call_handler("POST", "/api/projects/new", body={"name": "brand_new_proj"})
                    self.assertEqual(st_dup, 400)

                    st_dm, _, raw_dm = _call_handler("POST", "/api/settings/dummy_mode", body={"enabled": True})
                    self.assertEqual(st_dm, 200)
                    self.assertTrue(json.loads(raw_dm)["dummy_render"])

                    st_put_bad, _, _ = _call_handler("PUT", "/api/project/routes_proj", body={"invalid": True})
                    self.assertEqual(st_put_bad, 400)

                    doc = json.loads(cfg_path.read_text(encoding="utf-8"))
                    doc["scenes"][0]["prompt"] = "Updated prompt via PUT"
                    st_put_ok, _, _ = _call_handler("PUT", "/api/project/routes_proj", body=doc)
                    self.assertEqual(st_put_ok, 200)
                    self.assertEqual(json.loads(cfg_path.read_text())["scenes"][0]["prompt"], "Updated prompt via PUT")
                    self.assertTrue(any((tmp_root / "backup").glob("routes_proj_*.json")))

                    st_enq, _, raw_enq = _call_handler("POST", "/api/queue/enqueue_all", body={"slug": "routes_proj"})
                    self.assertEqual(st_enq, 200)
                    self.assertTrue(all(it["status"] == "queued" for it in json.loads(raw_enq)["queue"]))
                    m_run_q.assert_called_once_with("routes_proj", force_restart=False)

                    st_clr, _, raw_clr = _call_handler("POST", "/api/queue/clear", body={"slug": "routes_proj"})
                    self.assertEqual(st_clr, 200)
                    self.assertTrue(all(it["status"] == "skipped" for it in json.loads(raw_clr)["queue"]))
            finally:
                server.dummy_render_mode = orig_dummy

    def test_15_server_queue_subletter_anchor_mtime_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="subletter_proj")
            a05 = proj_dir / "work" / "anchors" / "A05_Stage.jpg"
            a05a = proj_dir / "work" / "anchors" / "A05a_Strings.jpg"
            a05.write_bytes(b"A05_ORIG")
            a05a.write_bytes(b"A05A_ORIG")

            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["anchors"] = [
                {"id": "A05", "filename": "A05_Stage.jpg", "seed": 1005, "prompt": "Stage"},
                {"id": "A05a", "filename": "A05a_Strings.jpg", "seed": 1006, "prompt": "Strings"},
            ]
            cfg["scenes"] = [
                {"id": "S01", "start": 0.0, "end": 2.0, "mode": "solo", "stem": "male", "anchor": "A05_Stage.jpg", "prompt": "Stage"},
                {"id": "S02", "start": 2.0, "end": 4.0, "mode": "solo", "stem": "male", "anchor": "A05a_Strings.jpg", "prompt": "Strings"},
            ]
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            hashes = compute_project_task_hashes(ctx)
            for stem in hashes:
                (ctx.renders_dir / f"{stem}.mp4").write_bytes(b"\x00" * 60000)
            a05_sha = hashlib.sha256(a05.read_bytes()).hexdigest()[:16]
            a05a_sha = hashlib.sha256(a05a.read_bytes()).hexdigest()[:16]
            (ctx.jobs_dir / "state_hashes.json").write_text(json.dumps({
                "tasks": {stem: vals[0] for stem, vals in hashes.items()},
                "anchor_file_hashes": {"A05_Stage.jpg": a05_sha, "A05a_Strings.jpg": a05a_sha},
            }), encoding="utf-8")

            with mock.patch.object(server, "PROJECTS_DIR", tmp_p):
                q0 = server.compute_fast_project_queue("subletter_proj", force_recheck=True)
                self.assertEqual([x["status"] for x in q0], ["completed", "completed"])

                # Update ONLY A05a on disk -> S02 queued with ANCHOR A05a NEWER ON DISK, S01 stays completed
                a05a.write_bytes(b"A05A_UPDATED_V2")
                q1 = server.compute_fast_project_queue("subletter_proj", force_recheck=True)
                by_sid = {x["scene_id"]: x for x in q1}
                self.assertEqual(by_sid["S01"]["status"], "completed")
                self.assertEqual(by_sid["S02"]["status"], "queued")
                self.assertIn("ANCHOR A05a", by_sid["S02"]["reason"])

    def test_16_project_context_dedupes_path_across_repeated_inits(self):
        import os as _os
        from ui import server_workers
        orig_path = _os.environ.get("PATH", "")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="path_bloat_proj")
            extra_d = proj_dir / "custom_bin"
            extra_d.mkdir(parents=True, exist_ok=True)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["environment"] = {"extra_path_dirs": [str(extra_d)]}
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            try:
                _os.environ["PATH"] = orig_path
                ProjectContext(cfg_path)
                len_after_1 = len(_os.environ["PATH"])
                for _ in range(300):
                    ProjectContext(cfg_path)
                self.assertEqual(len(_os.environ["PATH"]), len_after_1)

                # _subprocess_env also cleans duplicate entries out of an already-bloated PATH
                _os.environ["PATH"] = _os.pathsep.join([str(extra_d)] * 400 + [orig_path])
                sub_env = server_workers._subprocess_env()
                self.assertLessEqual(len(sub_env["PATH"]), len_after_1 + 64)
            finally:
                _os.environ["PATH"] = orig_path


class TestAnchorStudioAndStage1PromptRefs(unittest.TestCase):
    def test_17_compile_anchor_prompt_and_refs_character_expansion_and_neg_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="anc_char_proj", with_anchors=False)
            (proj_dir / "references" / "elena_ref.png").write_bytes(_MIN_1X1_PNG)
            (proj_dir / "references" / "shared_bg.png").write_bytes(_MIN_1X1_PNG)

            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["characters"] = {
                "char_elena": {
                    "id": "char_elena",
                    "name": "Elena",
                    "physical_traits": "concert violinist",
                    "default_look": "emerald",
                    "looks": {
                        "emerald": {
                            "tag": "@elena",
                            "description": "wearing emerald velvet gown",
                            "image_refs": ["references/elena_ref.png"],
                        }
                    },
                }
            }
            cfg["models"]["anchor_shared_settings"] = {
                "image_refs": ["references/shared_bg.png"],
                "negative_prompt": "blurry, watermark",
            }
            cfg["anchors"] = [
                {
                    "filename": "A01_Elena.jpg",
                    "seed": 101,
                    "resolution": "1280x720",
                    "negative_prompt": "extra fingers, deformed hands",
                    "prompt": "Dramatic spotlight on @elena holding a violin",
                },
                {
                    "filename": "A02_Custom.jpg",
                    "seed": 102,
                    "image_refs": ["references/shared_bg.png"],
                    "prompt": "Wide stage framing of @elena",
                },
                {
                    "filename": "A03_Manual.jpg",
                    "external": True,
                    "prompt": "",
                },
                {
                    "filename": "A04_New.jpg",
                    "seed": 204,
                    "prompt": "Portrait of @elena at center stage",
                },
            ]
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            _, rspec0 = ctx.resolve_anchor_renderer(ctx.anchors[0])
            canon0, adapted0, refs0 = compile_anchor_prompt_and_refs(ctx, ctx.anchors[0], rspec0)
            self.assertIn("concert violinist", canon0)
            self.assertIn("wearing emerald velvet gown", canon0)
            self.assertIn("concert violinist", adapted0)
            self.assertEqual([Path(p).name for p in refs0], ["elena_ref.png"])

            task0 = ctx.make_anchor_task(
                None, rspec0, str(ctx.anchors_dir / "A01_Elena.jpg"), adapted0, seed=101, sp=ctx.anchors[0], image_refs=refs0
            )
            self.assertEqual(task0["negative_prompt"], "extra fingers, deformed hands")
            self.assertEqual(task0["resolution"], "1280x720")
            self.assertEqual(task0["image_refs"], refs0)

            # Default shared negative prompt inherited when anchor omits negative_prompt
            _, rspec3 = ctx.resolve_anchor_renderer(ctx.anchors[3])
            _, adapted3, refs3 = compile_anchor_prompt_and_refs(ctx, ctx.anchors[3], rspec3)
            task3 = ctx.make_anchor_task(
                None, rspec3, str(ctx.anchors_dir / "A04_New.jpg"), adapted3, seed=204, sp=ctx.anchors[3], image_refs=refs3
            )
            self.assertEqual(task3["negative_prompt"], "blurry, watermark")

            # Explicit sp["image_refs"] overrides matched @character refs
            _, rspec1 = ctx.resolve_anchor_renderer(ctx.anchors[1])
            _, _, refs1 = compile_anchor_prompt_and_refs(ctx, ctx.anchors[1], rspec1)
            self.assertEqual([Path(p).name for p in refs1], ["shared_bg.png"])

            # Dummy generation for targeted A04 creates A04_New.jpg without creating A03_Manual or A01_Elena
            lazy = LazyWgpSession(ctx)
            state = {}
            regen = run_stage1_anchors(ctx, lazy, state, force_anchors=["A04"], dummy_render=True)
            self.assertTrue("A04" in regen or "A04_New.jpg" in regen)
            self.assertTrue((ctx.anchors_dir / "A04_New.jpg").is_file())
            self.assertGreater((ctx.anchors_dir / "A04_New.jpg").stat().st_size, 1000)
            self.assertFalse((ctx.anchors_dir / "A03_Manual.jpg").exists())
            self.assertFalse((ctx.anchors_dir / "A01_Elena.jpg").exists())

            # Unforced Stage 1 skips external anchor with empty prompt cleanly
            regen2 = run_stage1_anchors(ctx, lazy, state, force_anchors=[], dummy_render=True)
            self.assertNotIn("A03_Manual.jpg", regen2)
            self.assertFalse((ctx.anchors_dir / "A03_Manual.jpg").exists())
            self.assertNotIn("A04_New.jpg", regen2)

            # Editing an already-generated anchor's prompt triggers automatic CONFIG CHANGED regeneration on unforced run
            ctx.anchors[3]["prompt"] = "Updated wide twilight portrait of @elena on stage"
            regen3 = run_stage1_anchors(ctx, lazy, state, force_anchors=[], dummy_render=True)
            self.assertIn("A04_New.jpg", regen3)

            # Pure text-to-image override (image_refs=[]) strips any orphan <imageN> tags
            no_ref_sp = {"filename": "A05_NoRef.jpg", "image_refs": [], "prompt": "Solo portrait of @elena (from <image1>)"}
            _, adapted_noref, refs_noref = compile_anchor_prompt_and_refs(ctx, no_ref_sp, rspec0)
            self.assertEqual(refs_noref, [])
            self.assertNotIn("<image1>", adapted_noref)
            self.assertNotIn("<Picture 1>", adapted_noref)

    def test_18_anchors_upload_and_delete_api_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            proj_dir, _ = _create_fixture_project(tmp_p, slug="anc_api_proj", with_anchors=False)
            b64_png = base64.b64encode(_MIN_1X1_PNG).decode("ascii")
            data_url = f"data:image/png;base64,{b64_png}"

            with mock.patch.object(server, "PROJECTS_DIR", tmp_p):
                # Reject path traversal and corrupt payload
                st_trav, _, _ = _call_handler(
                    "POST",
                    "/api/anchors/upload",
                    body={"slug": "anc_api_proj", "filename": "../evil.png", "data_url": data_url},
                )
                self.assertEqual(st_trav, 400)
                self.assertFalse((proj_dir / "work" / "evil.png").exists())

                st_bad_img, _, _ = _call_handler(
                    "POST",
                    "/api/anchors/upload",
                    body={"slug": "anc_api_proj", "filename": "A03_Stage.png", "data_url": "data:image/png;base64,bm90YW5pbWc="},
                )
                self.assertEqual(st_bad_img, 400)

                # Upload valid PNG anchor
                st_ok, _, raw_ok = _call_handler(
                    "POST",
                    "/api/anchors/upload",
                    body={"slug": "anc_api_proj", "filename": "A03_Stage.png", "data_url": data_url},
                )
                self.assertEqual(st_ok, 200)
                resp_ok = json.loads(raw_ok.decode("utf-8"))
                self.assertTrue(resp_ok["success"])
                self.assertEqual(resp_ok["aid"], "A03")
                dest = proj_dir / "work" / "anchors" / "A03_Stage.png"
                self.assertTrue(dest.is_file())
                self.assertEqual(dest.read_bytes(), _MIN_1X1_PNG)

                # Reject traversal on delete, then delete valid anchor
                st_del_bad, _, _ = _call_handler(
                    "POST",
                    "/api/anchors/delete",
                    body={"slug": "anc_api_proj", "filename": "../A03_Stage.png"},
                )
                self.assertEqual(st_del_bad, 400)
                self.assertTrue(dest.is_file())

                st_del_ok, _, raw_del = _call_handler(
                    "POST",
                    "/api/anchors/delete",
                    body={"slug": "anc_api_proj", "filename": "A03_Stage.png"},
                )
                self.assertEqual(st_del_ok, 200)
                self.assertTrue(json.loads(raw_del.decode("utf-8"))["deleted"])
                self.assertFalse(dest.exists())

                # Upload standalone source image via /api/anchors/upload-source with ZERO characters/refs
                st_src_bad, _, _ = _call_handler(
                    "POST",
                    "/api/anchors/upload-source",
                    body={"slug": "anc_api_proj", "filename": "bad.png", "data_url": "data:image/png;base64,bm90YW5pbWc="},
                )
                self.assertEqual(st_src_bad, 400)

                st_src_ok, _, raw_src_ok = _call_handler(
                    "POST",
                    "/api/anchors/upload-source",
                    body={"slug": "anc_api_proj", "filename": "Look Plate.png", "data_url": data_url},
                )
                self.assertEqual(st_src_ok, 200)
                src_resp = json.loads(raw_src_ok.decode("utf-8"))
                self.assertTrue(src_resp["success"])
                rel_src = src_resp["rel_path"]
                self.assertTrue(rel_src.startswith("work/anchor_sources/Look_Plate_"))
                self.assertTrue((proj_dir / rel_src).is_file())
                self.assertIn(rel_src, src_resp.get("anchor_source_files", []))

                # Compile & build task when anchor uses image_refs directly on zero-character project
                from pipeline.context import ProjectContext
                from pipeline.stage1_anchors import compile_anchor_prompt_and_refs
                cfg_p = proj_dir / "anc_api_proj.json"
                cfg_data = json.loads(cfg_p.read_text(encoding="utf-8"))
                cfg_data["characters"] = {}
                cfg_data["anchors"] = [{"filename": "A01_Hero.jpg", "seed": 111, "prompt": "Subject from <image1> on neon rooftop", "image_refs": [rel_src]}]
                cfg_p.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")
                ctx_src = ProjectContext(cfg_p)
                rspec_src = {"model_type": "flux2_klein_9b_kv", "ref_tag_style": "image N"}
                canon_p, adapted_p, res_refs = compile_anchor_prompt_and_refs(ctx_src, cfg_data["anchors"][0], rspec_src)
                self.assertEqual(len(res_refs), 1)
                self.assertEqual(Path(res_refs[0]).resolve(), (proj_dir / rel_src).resolve())
                self.assertIn("image 1", adapted_p)
                t_cfg = ctx_src.make_anchor_task(None, rspec_src, "A01_Hero", adapted_p, seed=111, sp=cfg_data["anchors"][0], image_refs=res_refs)
                self.assertEqual(t_cfg["image_refs"], res_refs)
                self.assertEqual(t_cfg["video_prompt_type"], "I")

                # Archive take records image_refs and take-to-anchor carries image_refs over
                from pipeline.anchor_takes import archive_anchor_take_file
                a01_file = proj_dir / "work" / "anchors" / "A01_Hero.jpg"
                a01_file.parent.mkdir(parents=True, exist_ok=True)
                a01_file.write_bytes(_MIN_1X1_PNG)
                t_entry = archive_anchor_take_file(
                    a01_file.parent, proj_dir, "A01", a01_file, cfg_data["anchors"][0],
                    seed=111, model_type="flux2_klein_9b_kv", image_refs=[rel_src],
                )
                self.assertEqual((t_entry or {}).get("image_refs"), [rel_src])
                st_prom, _, raw_prom = _call_handler(
                    "POST", "/api/anchors/take-to-anchor",
                    body={"slug": "anc_api_proj", "aid": "A01", "take_id": t_entry["take_id"], "label": "Promoted"},
                )
                self.assertEqual(st_prom, 200)
                self.assertEqual(json.loads(raw_prom.decode("utf-8"))["new_anchor"].get("image_refs"), [rel_src])

                # /api/anchors/delete-source rejects non-anchor_sources path, deletes valid uploaded source & detaches from JSON
                st_dsrc_bad, _, _ = _call_handler(
                    "POST", "/api/anchors/delete-source",
                    body={"slug": "anc_api_proj", "rel_path": "work/anchors/A01_Hero.jpg"},
                )
                self.assertEqual(st_dsrc_bad, 400)
                st_dsrc_ok, _, raw_dsrc = _call_handler(
                    "POST", "/api/anchors/delete-source",
                    body={"slug": "anc_api_proj", "rel_path": rel_src},
                )
                self.assertEqual(st_dsrc_ok, 200)
                dsrc_resp = json.loads(raw_dsrc.decode("utf-8"))
                self.assertTrue(dsrc_resp["deleted"])
                self.assertFalse((proj_dir / rel_src).exists())
                self.assertEqual(dsrc_resp["anchor_source_files"], [])
                updated_cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
                self.assertNotIn("image_refs", updated_cfg["anchors"][0])

    def test_19_anchors_studio_js_vm_create_edit_prefill_and_render(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not available")
        js_script = r"""
        const fs = require("fs");
        const vm = require("vm");
        const calls = { dirty: 0, history: [], dropdown: 0, switchTab: [], renderArgs: null, toast: [] };
        const fakeEl = () => ({
          innerHTML: "", style: {}, value: "", checked: false, disabled: false, dataset: {},
          className: "", textContent: "", options: [], classList: { add(){}, remove(){}, toggle(){} },
          add(o){ this.options.push(o); },
          append(){}, appendChild(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
          addEventListener(){}, dispatchEvent(){}, focus(){}, click(){}, scrollIntoView(){},
        });
        const win = {
          state: {
            currentSlug: "vm_proj",
            selectedSceneIdx: 0,
            isDirty: false,
            models: { anchor_renderers: {}, fal_anchor_renderers: {} },
            assets: { anchors: {}, ref_files: [] },
            charactersData: { char_e: { name: "Elena", default_look: "default", looks: { default: { tag: "@elena", image_refs: ["references/e.png"] } } } },
            projectData: {
              anchors: [{ filename: "A01_Lead.jpg", seed: 1001, prompt: "Initial anchor" }],
              scenes: [{ id: "S01", start: 0, end: 3, seed: 3003, anchor: "A01_Lead.jpg", prompt: "For the target video, continuous camera:\ndetailed_description: [Shot 01] Continue directly from <Picture 1>. @elena singing passionately <cue>." }],
              models: { anchor_shared_settings: { image_refs: [] } },
            },
          },
          extractAnchorId: (s) => (String(s || "").match(/^(A\d+[a-z]?)/i)?.[1] || String(s || "")).toUpperCase(),
          escapeHtml: (s) => String(s || ""),
          getMergedAnchorsList: () => [],
          markProjectDirty: () => { calls.dirty++; win.state.isDirty = true; },
          recordHistorySnapshot: (lbl) => calls.history.push(lbl),
          updateInspectorAnchorsDropdown: () => { calls.dropdown++; },
          updateSceneAnchorPreview: () => {},
          redrawTimeline: () => {},
          switchTab: (t) => calls.switchTab.push(t),
          showToast: (m) => calls.toast.push(m),
          triggerRender: async (args) => { calls.renderArgs = args; },
          handleQueueSSEEvent: () => {},
          addEventListener: () => {},
        };
        const ctx = vm.createContext({
          window: win,
          document: { readyState: "complete", getElementById: () => null, createElement: () => fakeEl(), addEventListener: () => {} },
          Event: function(t){ this.type = t; },
          Option: function(t, v){ return { text: t, value: v }; },
          setTimeout, clearTimeout, console, Promise,
        });
        Object.assign(ctx, { window: ctx.window });
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
        const derived = ctx.window.deriveAnchorPromptFromScene(ctx.window.state.projectData.scenes[0]);
        if (!derived.startsWith("Cinematic 16:9 still frame,") || derived.includes("For the target video") || derived.includes("<Picture 1>") || derived.includes("<cue>")) {
          throw new Error("Bad derived prompt: " + derived);
        }
        const duetSc = {
          id: "S02",
          mode: "duet_method_b",
          prompt_m: "For the target video:\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Wide studio floor. On the left, @male sings with synchronized lip movements, while the partner on the right listens quietly.",
          prompt_f: "For the target video:\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Wide studio floor. On the right, @elena sings harmony with synchronized lip movements.",
        };
        const duetDerived = ctx.window.deriveAnchorPromptFromScene(duetSc);
        if (!duetDerived.includes("@male") || !duetDerived.includes("@elena") || duetDerived.includes("synchronized lip movements")) {
          throw new Error("Bad duet derived prompt: " + duetDerived);
        }
        const a2 = ctx.window.createNewAnchor({ label: "Chorus", prompt: "Neon @elena closeup", seed: 4444, resolution: "1280x720", negative_prompt: "blurry" });
        if (a2.filename !== "A02_Chorus.jpg" || a2.seed !== 4444 || !ctx.window.state.isDirty) {
          throw new Error("Bad created anchor: " + JSON.stringify(a2));
        }
        const a3 = ctx.window.createAnchorFromScene(0);
        if (a3.filename !== "A03_S01.jpg" || ctx.window.state.projectData.scenes[0].anchor !== "A03_S01.jpg" || !calls.switchTab.includes("anchors")) {
          throw new Error("Bad createAnchorFromScene result: " + JSON.stringify(a3));
        }
        const a4 = ctx.window.duplicateAnchor("A02_Chorus.jpg");
        if (!a4 || a4.filename !== "A04_Chorus_Copy.jpg" || a4.prompt !== "Neon @elena closeup" || a4.resolution !== "1280x720" || a4.negative_prompt !== "blurry") {
          throw new Error("Bad duplicateAnchor result: " + JSON.stringify(a4));
        }
        ctx.window.renderSingleAnchor("A02_Chorus.jpg");
        if (JSON.stringify(calls.renderArgs) !== JSON.stringify(["--anchors-only", "A02"])) {
          throw new Error("Bad renderSingleAnchor args: " + JSON.stringify(calls.renderArgs));
        }
        ctx.window.renderSingleAnchor("A02_Chorus.jpg", 3);
        if (JSON.stringify(calls.renderArgs) !== JSON.stringify(["--anchors-only", "A02:x3"])) {
          throw new Error("Bad multi-take renderSingleAnchor args: " + JSON.stringify(calls.renderArgs));
        }
        process.stdout.write("VM_OK");
        """
        proc = subprocess.run(
            [node_bin, "-e", js_script, str(WORK_DIR / "ui" / "static" / "js" / "anchors_studio.js")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(proc.stdout.strip(), "VM_OK")

    def test_20_anchor_takes_batch_use_delete_promote_and_character_ref_generation(self):
        from pipeline.anchor_takes import run_character_ref_generation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            tmp_p = tmp_root / "projects"
            tmp_p.mkdir()
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="takes_proj", with_anchors=False)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["characters"] = {
                "char_elena": {
                    "id": "char_elena",
                    "name": "Elena",
                    "physical_traits": "concert violinist",
                    "default_look": "emerald",
                    "looks": {
                        "emerald": {
                            "tag": "@elena",
                            "description": "wearing emerald velvet gown",
                            "gen_prompt": "Cinematic portrait of @elena on stage",
                            "gen_seed": 500,
                            "image_refs": [],
                        }
                    },
                }
            }
            cfg["anchors"] = [
                {"filename": "A01_Elena.jpg", "seed": 101, "prompt": "Portrait of @elena"},
            ]
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            lazy = LazyWgpSession(ctx)
            state = {}

            # 1. Generate 3 takes for A01 in one batch via A01:x3
            run_stage1_anchors(ctx, lazy, state, force_anchors=["A01:x3"], dummy_render=True)
            with mock.patch.object(server, "PROJECTS_DIR", tmp_p):
                st_t, _, raw_t = _call_handler("POST", "/api/anchors/takes", body={"slug": "takes_proj"})
                self.assertEqual(st_t, 200)
                a01_slot = json.loads(raw_t.decode("utf-8"))["anchor_takes"]["A01"]
                takes_list = a01_slot["takes"]
                self.assertEqual(len(takes_list), 3)
                self.assertEqual([t["seed"] for t in takes_list], [101, 102, 103])
                self.assertEqual(a01_slot["active_take_id"], takes_list[2]["take_id"])

                # 2. Activate Take #1 (seed 101)
                st_use, _, raw_use = _call_handler(
                    "POST", "/api/anchors/take-use",
                    body={"slug": "takes_proj", "aid": "A01", "take_id": takes_list[0]["take_id"]},
                )
                self.assertEqual(st_use, 200)
                self.assertEqual(json.loads(raw_use.decode("utf-8"))["active_take_id"], takes_list[0]["take_id"])
                self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["anchors"][0]["seed"], 101)

                # 3. Promote Take #2 (seed 102) into a separate new Anchor (A02)
                st_prom, _, raw_prom = _call_handler(
                    "POST", "/api/anchors/take-to-anchor",
                    body={"slug": "takes_proj", "aid": "A01", "take_id": takes_list[1]["take_id"]},
                )
                self.assertEqual(st_prom, 200)
                prom_data = json.loads(raw_prom.decode("utf-8"))
                self.assertEqual(prom_data["new_aid"], "A02")
                self.assertTrue((ctx.anchors_dir / prom_data["new_anchor"]["filename"]).is_file())

                # 4. Delete Take #3 (seed 103)
                st_del, _, raw_del = _call_handler(
                    "POST", "/api/anchors/take-delete",
                    body={"slug": "takes_proj", "aid": "A01", "take_id": takes_list[2]["take_id"]},
                )
                self.assertEqual(st_del, 200)
                rem_takes = json.loads(raw_del.decode("utf-8"))["anchor_takes"]["A01"]["takes"]
                self.assertEqual(len(rem_takes), 2)

                # 4b. Subsequent generation on A01 skips already-used seeds (101, 102) instead of producing duplicates
                run_stage1_anchors(ctx, lazy, state, force_anchors=["A01:x2"], dummy_render=True)
                st_t2, _, raw_t2 = _call_handler("POST", "/api/anchors/takes", body={"slug": "takes_proj"})
                self.assertEqual(st_t2, 200)
                takes_after_second_run = json.loads(raw_t2.decode("utf-8"))["anchor_takes"]["A01"]["takes"]
                self.assertEqual([t["seed"] for t in takes_after_second_run], [101, 102, 103, 104])

                # 5. Generate 2 character look reference image takes via run_character_ref_generation
                ctx2 = ProjectContext(cfg_path)
                created_refs = run_character_ref_generation(ctx2, lazy, "char_elena:emerald:x2", dummy_render=True)
                self.assertEqual(len(created_refs), 2)
                for rp in created_refs:
                    self.assertTrue((proj_dir / rp).is_file())
                saved_chars = json.loads(cfg_path.read_text(encoding="utf-8"))["characters"]
                gen_takes = saved_chars["char_elena"]["looks"]["emerald"]["generated_takes"]
                self.assertEqual(len(gen_takes), 2)
                self.assertEqual([t["seed"] for t in gen_takes], [500, 501])

                # 6. Turn character look take #1 into a separate Stage 1 Anchor (A03) & delete take #2
                st_ca, _, raw_ca = _call_handler(
                    "POST", "/api/anchors/char-take-action",
                    body={
                        "slug": "takes_proj",
                        "sub_action": "to_anchor",
                        "entity_id": "char_elena",
                        "look_id": "emerald",
                        "rel_path": created_refs[0],
                        "take_id": gen_takes[0]["take_id"],
                    },
                )
                self.assertEqual(st_ca, 200)
                ca_data = json.loads(raw_ca.decode("utf-8"))
                self.assertEqual(ca_data["new_aid"], "A03")
                self.assertTrue((ctx.anchors_dir / ca_data["new_anchor"]["filename"]).is_file())

                st_cd, _, raw_cd = _call_handler(
                    "POST", "/api/anchors/char-take-action",
                    body={
                        "slug": "takes_proj",
                        "sub_action": "delete",
                        "entity_id": "char_elena",
                        "look_id": "emerald",
                        "rel_path": created_refs[1],
                        "take_id": gen_takes[1]["take_id"],
                    },
                )
                self.assertEqual(st_cd, 200)
                self.assertFalse((proj_dir / created_refs[1]).exists())

                # 7. Verify prepare_targeted_anchor_queue_items queues one entry per image (including while a scene is rendering)
                from pipeline.anchor_takes import prepare_targeted_anchor_queue_items
                q_entries = prepare_targeted_anchor_queue_items(server, "takes_proj", ["--anchors-only", "A01:x3"], is_worker_running=False)
                self.assertEqual(len(q_entries), 3)
                self.assertTrue(all(e.get("stage") == "anchor" and e.get("status") == "queued" for e in q_entries))
                # Seeds 101..104 were already used on A01 above, so new queued entries get [105, 106, 107]
                self.assertEqual([e["seed"] for e in q_entries], [105, 106, 107])
                self.assertEqual([e["target_arg"] for e in q_entries], ["A01:s105", "A01:s106", "A01:s107"])

                # Individual anchor queue entries can be reordered and removed via standard queue operations
                q_after_move = server.move_in_project_queue("takes_proj", q_entries[1]["filename"], "up")
                anc_in_q = [x for x in q_after_move if x.get("stage") == "anchor" and x.get("status") == "queued"]
                self.assertEqual([x["seed"] for x in anc_in_q], [106, 105, 107])

                q_after_rm = server.remove_from_project_queue("takes_proj", q_entries[2]["filename"])
                anc_queued_after_rm = [x for x in q_after_rm if x.get("stage") == "anchor" and x.get("status") == "queued"]
                self.assertEqual([x["seed"] for x in anc_queued_after_rm], [106, 105])

                # Queuing more anchor takes while a task is actively rendering appends without blocking or duplicating queued seeds
                more_entries = prepare_targeted_anchor_queue_items(server, "takes_proj", ["--anchors-only", "A01:x2"], is_worker_running=True)
                self.assertEqual([e["seed"] for e in more_entries], [107, 108])

                # 8. Batch delete active + historical takes via take_ids in one API call promotes newest survivor
                batch_del_ids = [takes_after_second_run[0]["take_id"], takes_after_second_run[2]["take_id"]]
                st_bdel, _, raw_bdel = _call_handler(
                    "POST", "/api/anchors/take-delete",
                    body={"slug": "takes_proj", "aid": "A01", "take_ids": batch_del_ids},
                )
                self.assertEqual(st_bdel, 200)
                bdel_data = json.loads(raw_bdel.decode("utf-8"))
                self.assertEqual(bdel_data["deleted_count"], 2)
                rem_after_batch = bdel_data["anchor_takes"]["A01"]["takes"]
                self.assertEqual([t["seed"] for t in rem_after_batch], [102, 104])
                self.assertEqual(bdel_data["anchor_takes"]["A01"]["active_take_id"], takes_after_second_run[3]["take_id"])

    def test_21_completed_anchor_seeds_never_requeue_or_duplicate(self):
        from pipeline.anchor_takes import archive_anchor_take_file, prepare_targeted_anchor_queue_items
        from ui import server_engine_io

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp) / "projects"
            tmp_p.mkdir()
            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="anc_noseed_dup", with_anchors=True)
            a01_img = proj_dir / "work" / "anchors" / "A01_Lead.jpg"
            a01_img.write_bytes(b"IMG_SEED_1001")
            archive_anchor_take_file(
                proj_dir / "work" / "anchors",
                proj_dir,
                "A01",
                a01_img,
                {"filename": "A01_Lead.jpg", "seed": 1001, "prompt": "P"},
                seed=1001,
                make_active=True,
            )

            # 1. collect_active_rendering_map must ignore stale slot['filename'] once per_tasks marks it completed
            act_map = server_engine_io.collect_active_rendering_map(
                running_stems=set(),
                local_slots=["A01_Lead_s1001"],
                q_active=True,
                per_tasks={
                    ("local", "A01_Lead_s1001"): {
                        "state": "completed",
                        "backend": "local",
                        "filename": "A01_Lead_s1001.jpg",
                        "output_filename": "A01_Lead_s1001",
                        "started_at": 1000.0,
                        "updated_at": 1029.5,
                    }
                },
                per_eng={},
                legacy_eng=None,
                slots_items=[("local", {"filename": "A01_Lead_s1001.jpg", "started_at": 1000.0})],
            )
            self.assertNotIn("A01_Lead_s1001", act_map)
            self.assertNotIn("A01_Lead_s1001.jpg", act_map)

            # 2. Corrupted completed rows & leftover duplicate queued rows for already-rendered seeds heal and never re-run
            q_file = proj_dir / "work" / "jobs" / "v3_queue.json"
            q_file.parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(server, "PROJECTS_DIR", tmp_p):
                q_file.write_text(
                    json.dumps([
                        {
                            "idx": 1, "id": "A01_Lead_s1001", "stage": "anchor", "anchor_id": "A01", "scene_id": "A01",
                            "target_arg": "A01:s1001", "seed": 1001, "filename": "A01_Lead_s1001.jpg",
                            "output_filename": "A01_Lead_s1001", "status": "queued", "reason": "COMPLETED",
                            "needs_render": True, "completed_at": 1030.0, "backend": "local",
                        },
                        {
                            "idx": 2, "id": "A01_Lead_s1002", "stage": "anchor", "anchor_id": "A01", "scene_id": "A01",
                            "target_arg": "A01:s1002", "seed": 1002, "filename": "A01_Lead_s1002.jpg",
                            "output_filename": "A01_Lead_s1002", "status": "queued", "reason": "ANCHOR A01 TAKE 2/2 (s1002)",
                            "needs_render": True, "backend": "local",
                        },
                    ]),
                    encoding="utf-8",
                )
                reconciled = server.compute_fast_project_queue("anc_noseed_dup", force_recheck=False)
                by_id = {x["id"]: x for x in reconciled}
                self.assertEqual(by_id["A01_Lead_s1001"]["status"], "completed")
                self.assertFalse(by_id["A01_Lead_s1001"]["needs_render"])
                self.assertEqual(by_id["A01_Lead_s1002"]["status"], "queued")
                _, picked = server_engine_io.pick_next_eligible_item(reconciled, "local", server)
                self.assertEqual((picked or {}).get("id"), "A01_Lead_s1002")

                # 3. Re-queuing A01 drops stale queued rows for seeds already saved in anchor_takes.json
                q_file.write_text(
                    json.dumps([
                        {
                            "idx": 1, "id": "A01_Lead_s1001", "stage": "anchor", "anchor_id": "A01", "scene_id": "A01",
                            "target_arg": "A01:s1001", "seed": 1001, "filename": "A01_Lead_s1001.jpg",
                            "output_filename": "A01_Lead_s1001", "status": "queued", "reason": "STOPPED BY USER",
                            "needs_render": True, "backend": "local",
                        },
                    ]),
                    encoding="utf-8",
                )
                new_entries = prepare_targeted_anchor_queue_items(server, "anc_noseed_dup", ["--anchors-only", "A01:x2"], is_worker_running=False)
                self.assertEqual([e["seed"] for e in new_entries], [1002, 1003])
                after_q = json.loads(q_file.read_text(encoding="utf-8"))
                self.assertFalse(any(x.get("id") == "A01_Lead_s1001" and x.get("status") == "queued" for x in after_q))

    def test_22_stale_pinokio_wgp_path_redetection_and_repair(self):
        import os as _os
        from pipeline.media_utils import auto_detect_wgp_root, is_valid_wgp_root, repair_stale_local_config

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            ws_dir = tmp_p / "ws"
            ws_dir.mkdir()
            old_pin = tmp_p / "old_pinokio"
            old_wgp = old_pin / "api" / "wan.git" / "app"
            old_py = old_wgp / "venv" / "Scripts" / "python.exe"
            new_pin = tmp_p / "new_pinokio"
            new_wgp = new_pin / "api" / "wan.git" / "app"
            new_py = new_wgp / "venv" / "Scripts" / "python.exe"
            (new_wgp / "models").mkdir(parents=True)
            (new_wgp / "wgp_config.json").write_text("{}", encoding="utf-8")
            new_py.parent.mkdir(parents=True)
            if _os.name == "nt":
                shutil.copy2(sys.executable, new_py)
                for dll in Path(sys.executable).parent.glob("*.dll"):
                    shutil.copy2(dll, new_py.parent / dll.name)
            else:
                new_py.write_text("#!/bin/sh\n", encoding="utf-8")

            # Empty leftover folder is rejected; valid install is accepted
            old_wgp.mkdir(parents=True)
            self.assertFalse(is_valid_wgp_root(old_wgp))
            self.assertTrue(is_valid_wgp_root(new_wgp))

            # Stale local_config.json + config.local.bat pointing to removed/empty old_pinokio
            (ws_dir / "local_config.json").write_text(
                json.dumps({
                    "wgp_root": str(old_wgp),
                    "pinokio_root": str(old_pin),
                    "python_path": str(old_py),
                    "fal_key_dpapi": "keep_me_secret",
                }),
                encoding="utf-8",
            )
            (ws_dir / "config.local.bat").write_text(
                f'@echo off\nset "PINOKIO_PY={old_py}"\nset "WGP_ROOT={old_wgp}"\n',
                encoding="utf-8",
            )

            proj_dir, cfg_path = _create_fixture_project(tmp_p, slug="reloc_proj", with_anchors=False)
            (proj_dir / "local_config.json").write_text(json.dumps({"wgp_root": str(old_wgp), "python_path": str(old_py)}), encoding="utf-8")

            env_patch = {
                "WGP_ROOT": str(old_wgp),
                "PINOKIO_PY": str(old_py),
                "PINOKIO_HOME": str(new_pin),
            }
            with mock.patch.dict(_os.environ, env_patch, clear=False):
                detected = auto_detect_wgp_root()
                self.assertEqual(detected, new_wgp.resolve())
                ctx = ProjectContext(cfg_path)
                self.assertEqual(ctx.wgp_root, new_wgp.resolve())
                with mock.patch.object(server, "WORKSPACE_ROOT", ws_dir):
                    self.assertEqual(Path(server.resolve_engine_python()).resolve(), new_py.resolve())
                    self.assertTrue(repair_stale_local_config(ws_dir, wgp_root=detected))

            repaired_json = json.loads((ws_dir / "local_config.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(repaired_json["wgp_root"]).resolve(), new_wgp.resolve())
            self.assertEqual(Path(repaired_json["python_path"]).resolve(), new_py.resolve())
            self.assertEqual(repaired_json["fal_key_dpapi"], "keep_me_secret")
            repaired_bat = (ws_dir / "config.local.bat").read_text(encoding="utf-8")
            self.assertIn(str(new_wgp.resolve()), repaired_bat)

            # End-to-end launch_ui.bat check on Windows: stale config.local.bat must recover and re-scan
            if _os.name == "nt":
                bat_ws = tmp_p / "bat_ws"
                (bat_ws / "work" / "ui").mkdir(parents=True)
                shutil.copy2(WORKSPACE_ROOT / "launch_ui.bat", bat_ws / "launch_ui.bat")
                (bat_ws / "config.local.bat").write_text(
                    f'@echo off\nset "PINOKIO_PY={old_py}"\nset "WGP_ROOT={old_wgp}"\n',
                    encoding="utf-8",
                )
                (bat_ws / "work" / "ui" / "server.py").write_text("import sys; print('LAUNCHED_OK', sys.executable)\n", encoding="utf-8")
                bat_env = _os.environ.copy()
                bat_env["PINOKIO_HOME"] = str(new_pin)
                bat_env["MVG_NO_BROWSER"] = "1"
                res_bat = subprocess.run(
                    ["cmd", "/c", str(bat_ws / "launch_ui.bat")],
                    cwd=str(bat_ws),
                    env=bat_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(res_bat.returncode, 0, res_bat.stdout + res_bat.stderr)
                self.assertIn("LAUNCHED_OK", res_bat.stdout)
                self.assertIn(str(new_wgp.resolve()), (bat_ws / "config.local.bat").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
