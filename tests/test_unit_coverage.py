"""Comprehensive unit and HTTP API tests closing backend coverage gaps.

Covers stage1_anchors, stage3_master, cli, characters, upscale, server_media,
server_routes, and server_queue edge cases hermetically in temp directories.
Uses in-process StudioRequestHandler invocation so all unit tests run cleanly
without requiring TCP port binding or live GPU renders.
"""

from __future__ import annotations

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
        "work/anchors_v3",
        "work/audio_v3",
        "work/jobs_v3",
        "work/renders_v3",
        "work/trimmed_v3",
        "work/frames_v3",
    ):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)

    _write_silent_wav(proj_dir / "audio" / "master.wav", duration_s=4.0)
    _write_silent_wav(proj_dir / "audio" / "male.wav", duration_s=4.0)

    if with_anchors:
        (proj_dir / "work" / "anchors_v3" / "A01_Lead.jpg").write_bytes(b"A01_JPG_BYTES_" + b"\x01" * 64)
        (proj_dir / "work" / "anchors_v3" / "A02_Wide.jpg").write_bytes(b"A02_JPG_BYTES_" + b"\x02" * 64)

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
                "anchors": "work/anchors_v3",
                "audio_slices": "work/audio_v3",
                "masks": "work/masks",
                "jobs": "work/jobs_v3",
                "renders": "work/renders_v3",
                "trimmed": "work/trimmed_v3",
                "review_strips": "work/frames_v3",
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

            clip_path = proj_dir / "work" / "renders_v3" / "S01_solo_832x480.mp4"
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
            (proj_dir / "work" / "anchors_v3" / "A05_Main.jpg").write_bytes(a05_bytes)
            (proj_dir / "work" / "anchors_v3" / "A05a_Sub.jpg").write_bytes(a05a_bytes)
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
            a05 = proj_dir / "work" / "anchors_v3" / "A05_Stage.jpg"
            a05a = proj_dir / "work" / "anchors_v3" / "A05a_Strings.jpg"
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


if __name__ == "__main__":
    unittest.main()
