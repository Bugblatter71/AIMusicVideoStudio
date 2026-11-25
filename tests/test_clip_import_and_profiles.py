"""Tests for external clip import (CLI + Python + HTTP), Wan 2.2 local profiles, and new fal.ai scenic profiles."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from pipeline.clip_import import import_video_clip_as_take, run_import_clip_cli
from pipeline.context import ProjectContext
from pipeline.fal_bridge import FalSession
from pipeline.fal_catalog import FAL_VIDEO_PROFILES, estimate_fal_cost_usd, resolve_fal_route
from pipeline.media_utils import probe_video_dimensions, probe_video_fps
from pipeline.prompts import adapt_ref_tags, align_anchor_resolution
from pipeline.stage1_anchors import compile_anchor_prompt_and_refs
from pipeline.takes import load_takes_manifest
import server
import server_routes


def _make_mp4(ffmpeg: Path, out_mp4: Path, dur: float = 4.2, w: int = 640, h: int = 480, fps: int = 30):
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg), "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out_mp4),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_wav(ffmpeg: Path, out_wav: Path, dur: float = 10.0):
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"sine=frequency=520:duration={dur}", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out_wav)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_jpg(ffmpeg: Path, out_jpg: Path):
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=navy:s=640x360:d=0.05", "-vframes", "1", "-q:v", "2", str(out_jpg)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class _DummyFalClient:
    def upload_file(self, path: str) -> str:
        return f"https://mock.fal.media/{Path(path).name}"


class TestClipImportAndProfiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base_ctx = ProjectContext(WORKSPACE_ROOT / "projects" / "test_project" / "test_project.json")
        cls.ffmpeg = base_ctx.ffmpeg

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _build_proj(self, slug: str = "demo_proj") -> tuple[Path, Path]:
        pdir = self.root / slug
        (pdir / "audio").mkdir(parents=True, exist_ok=True)
        (pdir / "work" / "anchors").mkdir(parents=True, exist_ok=True)
        (pdir / "work" / "renders").mkdir(parents=True, exist_ok=True)
        (pdir / "work" / "jobs").mkdir(parents=True, exist_ok=True)
        (pdir / "local_config.json").write_text("{}", encoding="utf-8")

        _make_wav(self.ffmpeg, pdir / "audio" / "master.wav", dur=10.0)
        _make_wav(self.ffmpeg, pdir / "audio" / "male.wav", dur=10.0)
        _make_jpg(self.ffmpeg, pdir / "work" / "anchors" / "A01_hero.jpg")

        profiles = json.loads((WORKSPACE_ROOT / "model_profiles.json").read_text(encoding="utf-8"))
        cfg = {
            "project": {
                "name": "Import & Profiles Test",
                "fps": 24,
                "master_audio": "audio/master.wav",
                "stems": {"male": "audio/male.wav"},
                "render_overlap_seconds": 0.0,
                "directories": {"anchors": "work/anchors", "renders": "work/renders", "jobs": "work/jobs"},
            },
            "models": {
                "default_anchor_renderer": 3,
                "default_video_renderer": 1,
                "default_video_resolution": 1,
                "video_resolutions": profiles["video_resolutions"],
                "video_renderers": profiles["video_renderers"],
                "fal_video_renderers": profiles["fal_video_renderers"],
                "video_shared_settings": {"resolution": "832x480", "negative_prompt": "blurry"},
            },
            "anchors": [{"id": "A01", "filename": "A01_hero.jpg", "prompt": "Hero", "seed": 11}],
            "scenes": [
                {"id": "S01", "start": 0.0, "end": 4.0, "mode": "solo", "anchor": "A01_hero.jpg", "stem": "male", "prompt": "Wide ocean camera push", "seed": 101},
                {"id": "S02", "start": 4.0, "end": 8.0, "mode": "silent_locked", "anchor": "A01_hero.jpg", "stem": "silent", "prompt": "Moody rain on glass", "seed": 102},
            ],
        }
        cfg_path = pdir / f"{slug}.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return pdir, cfg_path

    def test_01_wan22_profiles_and_quantization(self):
        _, cfg_path = self._build_proj()
        ctx = ProjectContext(cfg_path)
        self.assertIn("17", ctx.video_renderers)
        self.assertIn("18", ctx.video_renderers)

        spec17 = ctx.video_renderers["17"]
        self.assertEqual(spec17["model_type"], "i2v_2_2")
        self.assertEqual(ctx.model_fps(spec17), 16.0)
        # 4s * 16fps + 2 = 66 -> next 4k+1 >= 66 is 69 (17*4+1)
        f17 = ctx.valid_video_frames(4.0, spec17)
        self.assertEqual((f17 - 1) % 4, 0)
        self.assertEqual(f17, 69)

        spec18 = ctx.video_renderers["18"]
        self.assertEqual(spec18["model_type"], "ti2v_2_2")
        self.assertEqual(ctx.model_fps(spec18), 24.0)
        f18 = ctx.valid_video_frames(4.0, spec18)
        self.assertEqual((f18 - 1) % 4, 0)
        self.assertEqual(f18, 101)

        task = ctx.make_video_task(
            "S02_silent_832x480", "Close up on rain", f17, 102,
            ctx.anchors_dir / "A01_hero.jpg", ctx.project_dir / "audio" / "master.wav", spec17, sc=ctx.scenes[1],
        )
        self.assertEqual(task["model_type"], "i2v_2_2")
        self.assertEqual(task["audio_guide"], "")

    def test_02_fal_scenic_profiles_routing_and_payloads(self):
        _, cfg_path = self._build_proj()
        ctx = ProjectContext(cfg_path)
        expected = [
            "fal_kling_v3_std", "fal_kling_v3_pro", "fal_seedance_2_fast",
            "fal_seedance_v1_pro", "fal_wan_26_i2v", "fal_veo31_fast", "fal_veo31_lite",
        ]
        for k in expected:
            self.assertIn(k, FAL_VIDEO_PROFILES)
            self.assertIn(k, ctx.fal_video_renderers)

        sess = FalSession(ctx)
        sess._client = _DummyFalClient()
        a1 = ctx.anchors_dir / "A01_hero.jpg"

        # Kling v3 must use start_image_url + end_image_url + string duration + generate_audio=False
        _, k_spec = ctx.resolve_video_renderer({"id": "S01", "renderer": "fal_kling_v3_std"})
        k_route = resolve_fal_route(k_spec, sc={"id": "S01", "anchor_end": "A01_hero.jpg"})
        self.assertEqual(k_route["endpoint"], "fal-ai/kling-video/v3/standard/image-to-video")
        k_args = sess._build_arguments(k_route, {"prompt": "Scenic push", "image_start": str(a1), "image_end": str(a1), "resolution": "1344x768", "seed": 7}, 6.0)
        self.assertIn("start_image_url", k_args)
        self.assertNotIn("image_url", k_args)
        self.assertIn("end_image_url", k_args)
        self.assertEqual(k_args["duration"], "6")
        self.assertFalse(k_args["generate_audio"])

        # Veo 3.1 must use "4s"/"6s"/"8s", aspect_ratio="16:9", generate_audio=False
        _, v_spec = ctx.resolve_video_renderer({"id": "S01", "renderer": "fal_veo31_fast"})
        v_route = resolve_fal_route(v_spec)
        v_args = sess._build_arguments(v_route, {"prompt": "Slow pan", "image_start": str(a1), "resolution": "1920x1088", "seed": 7}, 6.0)
        self.assertIn("image_url", v_args)
        self.assertEqual(v_args["duration"], "6s")
        self.assertEqual(v_args["resolution"], "1080p")
        self.assertFalse(v_args["generate_audio"])

        # Wan v2.6 must map to "5"/"10"/"15" and "720p"/"1080p"
        _, w_spec = ctx.resolve_video_renderer({"id": "S01", "renderer": "fal_wan_26_i2v"})
        w_args = sess._build_arguments(resolve_fal_route(w_spec), {"prompt": "Foggy harbor", "image_start": str(a1), "resolution": "1344x768", "seed": 9}, 9.0)
        self.assertEqual(w_args["duration"], "10")
        self.assertEqual(w_args["resolution"], "720p")

        # Seedance 2.0 Fast & v1 Pro
        _, s2_spec = ctx.resolve_video_renderer({"id": "S01", "renderer": "fal_seedance_2_fast"})
        s2_args = sess._build_arguments(resolve_fal_route(s2_spec), {"prompt": "Run", "image_start": str(a1), "image_end": str(a1), "resolution": "1344x768", "seed": 9}, 5.0)
        self.assertEqual((s2_args["duration"], s2_args["generate_audio"]), (5, False))
        self.assertIn("end_image_url", s2_args)

        _, s1_spec = ctx.resolve_video_renderer({"id": "S01", "renderer": "fal_seedance_v1_pro"})
        s1_args = sess._build_arguments(resolve_fal_route(s1_spec), {"prompt": "Run", "image_start": str(a1), "resolution": "1920x1088", "seed": 9}, 8.0)
        self.assertEqual((s1_args["duration"], s1_args["resolution"]), ("10", "1080p"))

        est = estimate_fal_cost_usd(6.0, "1344x768", rspec=v_spec)
        self.assertGreater(est["estimated_cost_usd"], 0.0)

    def test_03_import_video_clip_as_take_normalizes_and_syncs_queue(self):
        pdir, cfg_path = self._build_proj("flow_proj")
        ext_mp4 = self.root / "flow_raw_640x480_30fps.mp4"
        ext2_mp4 = self.root / "flow_raw_v2.mp4"
        _make_mp4(self.ffmpeg, ext_mp4, dur=4.3, w=640, h=480, fps=30)
        _make_mp4(self.ffmpeg, ext2_mp4, dur=4.3, w=640, h=480, fps=30)

        # Seed an existing FORCED item in v3_queue.json to confirm import clears stale forced queue state
        (pdir / "work" / "jobs" / "v3_queue.json").write_text(
            json.dumps([{"scene_id": "S01", "output_filename": "S01_solo_832x480", "status": "queued", "reason": "FORCED", "needs_render": True}]),
            encoding="utf-8",
        )

        res1 = import_video_clip_as_take(pdir, "S01", ext_mp4, label="Flow Take 1")
        self.assertTrue(res1["success"])
        out_mp4 = Path(res1["active_mp4"])
        self.assertTrue(out_mp4.is_file())
        self.assertEqual(probe_video_dimensions(self.ffmpeg, out_mp4), (832, 480))
        self.assertAlmostEqual(float(probe_video_fps(self.ffmpeg, out_mp4)), 24.0, places=1)

        # Second import on same scene/prompt adds a distinct take without overwriting Flow Take 1
        res2 = import_video_clip_as_take(pdir, "S01", ext2_mp4, label="Flow Take 2")
        mf = load_takes_manifest(pdir)
        s01_takes = mf["scenes"]["S01"]["takes"]
        self.assertEqual(len(s01_takes), 2)
        self.assertEqual([t["label"] for t in s01_takes], ["Flow Take 1", "Flow Take 2"])
        self.assertEqual(mf["scenes"]["S01"]["active_take_id"], res2["take"]["take_id"])

        with mock.patch.object(server, "PROJECTS_DIR", self.root):
            q = server.compute_fast_project_queue("flow_proj")
        s01_q = next(it for it in q if it.get("scene_id") == "S01")
        self.assertEqual(s01_q["status"], "completed")
        self.assertFalse(s01_q["needs_render"])

    def test_04_cli_and_http_import_endpoints(self):
        pdir, cfg_path = self._build_proj("http_proj")
        ext_mp4 = self.root / "cli_clip.mp4"
        _make_mp4(self.ffmpeg, ext_mp4, dur=4.3, w=832, h=480, fps=24)

        self.assertTrue(run_import_clip_cli([str(cfg_path), "import-clip", "S02", str(ext_mp4), "--label", "CLI Flow", "--no-normalize"]))
        mf = load_takes_manifest(pdir)
        self.assertEqual(mf["scenes"]["S02"]["takes"][0]["label"], "CLI Flow")

        def _post_inproc(url_path: str, data_bytes: bytes, content_type: str):
            h = object.__new__(server_routes.StudioRequestHandler)
            h.path = url_path
            h.headers = Message()
            h.headers["Content-Type"] = content_type
            h.headers["Content-Length"] = str(len(data_bytes))
            h.rfile = io.BytesIO(data_bytes)
            h.wfile = io.BytesIO()
            captured = {}
            def _send_json(obj, status=200):
                captured["status"] = int(status)
                captured["body"] = obj
            h.send_json = _send_json
            h.send_error_json = lambda msg, status=400: _send_json({"error": msg}, status=status)
            h.do_POST()
            return captured

        with mock.patch.object(server, "PROJECTS_DIR", self.root):
            res_bin = _post_inproc(
                "/api/takes/import?project=http_proj&scene_id=S01&filename=flow_up.mp4&label=WebUpload",
                ext_mp4.read_bytes(),
                "video/mp4",
            )
            self.assertEqual(res_bin["status"], 200)
            self.assertTrue(res_bin["body"]["success"])
            self.assertEqual(res_bin["body"]["take"]["label"], "WebUpload")

            res_json = _post_inproc(
                "/api/takes/import",
                json.dumps({"project": "http_proj", "scene_id": "S01", "path": str(ext_mp4), "label": "JsonImport"}).encode("utf-8"),
                "application/json",
            )
            self.assertEqual(res_json["status"], 200)
            self.assertTrue(res_json["body"]["success"])
            self.assertEqual(res_json["body"]["take"]["label"], "JsonImport")

            res_bad = _post_inproc(
                "/api/takes/import?project=http_proj&scene_id=S01&filename=bad.txt",
                b"not a video",
                "text/plain",
            )
            self.assertEqual(res_bad["status"], 400)

    def test_05_minimax_h3_image_profiles_and_capabilities(self):
        profiles = json.loads((WORKSPACE_ROOT / "model_profiles.json").read_text(encoding="utf-8"))
        ar = profiles["anchor_renderers"]
        for rid, expected_mt, expected_refs, expected_steps in (
            ("17", "minimax_h3_image_pruned", 16, 6),
            ("18", "minimax_h3_image", 16, 6),
            ("19", "minimax_p_h3_image", 8, 8),
        ):
            self.assertIn(rid, ar)
            spec = ar[rid]
            self.assertEqual(spec["model_type"], expected_mt)
            self.assertEqual(spec["ref_tag_style"], "<Picture N>")
            self.assertEqual(spec["max_image_refs"], expected_refs)
            self.assertEqual(spec["num_inference_steps"], expected_steps)
            self.assertEqual(spec["guidance_phases"], 1)
            self.assertEqual(spec["guidance_scale"], 1.0)
            self.assertEqual(spec["sample_solver"], "euler")
            self.assertTrue(spec["no_negative_prompt"])
            self.assertGreaterEqual(len(spec["resolutions"]), 8)

        # Prompt tag translation works through <image16> (not limited to 1..9)
        self.assertEqual(
            adapt_ref_tags("Combine <image1>, <image9>, and <image16>", "<Picture N>"),
            "Combine <Picture 1>, <Picture 9>, and <Picture 16>",
        )
        self.assertEqual(align_anchor_resolution("1920x1080", "minimax_h3_image_pruned"), "1920x1088")
        self.assertEqual(align_anchor_resolution("5472x3072", "minimax_h3_image_pruned"), "5472x3072")

        pdir, cfg_path = self._build_proj("h3_img_proj")
        ref_rels = []
        for idx in range(1, 13):
            rel = f"references/ref_{idx:02d}.jpg"
            _make_jpg(self.ffmpeg, pdir / rel)
            ref_rels.append(rel)

        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["models"]["anchor_renderers"] = ar
        cfg["models"]["anchor_shared_settings"] = {"resolution": "1920x1080", "negative_prompt": "blurry, artifact"}
        cfg["anchors"] = [
            {"filename": "A01_H3Pruned.jpg", "renderer": 17, "prompt": "Compose <image1> with <image10> and <image12>", "image_refs": ref_rels, "seed": 11},
            {"filename": "A02_PH3.jpg", "renderer": 19, "resolution": "3872x2176", "prompt": "Edit <image1> using <image8> and <image10>", "image_refs": ref_rels, "seed": 22},
            {"filename": "A03_Klein.jpg", "renderer": 3, "prompt": "Port <image1> and <image5>", "image_refs": ref_rels, "seed": 33},
        ]
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        ctx = ProjectContext(cfg_path)
        _, r17 = ctx.resolve_anchor_renderer(ctx.anchors[0])
        _, adapted17, refs17 = compile_anchor_prompt_and_refs(ctx, ctx.anchors[0], r17)
        self.assertEqual(len(refs17), 12)
        self.assertEqual(adapted17, "Compose <Picture 1> with <Picture 10> and <Picture 12>")
        t17 = ctx.make_anchor_task(None, r17, "A01_H3Pruned", adapted17, seed=11, sp=ctx.anchors[0], image_refs=refs17)
        self.assertEqual(t17["model_type"], "minimax_h3_image_pruned")
        self.assertEqual(t17["resolution"], "1920x1088")
        self.assertEqual(t17["guidance_phases"], 1)
        self.assertEqual(t17["sample_solver"], "euler")
        self.assertEqual(t17["video_prompt_type"], "I")
        self.assertEqual(len(t17["image_refs"]), 12)
        self.assertEqual(t17["negative_prompt"], "")

        _, r19 = ctx.resolve_anchor_renderer(ctx.anchors[1])
        _, adapted19, refs19 = compile_anchor_prompt_and_refs(ctx, ctx.anchors[1], r19)
        self.assertEqual(len(refs19), 8)
        self.assertEqual(adapted19, "Edit <Picture 1> using <Picture 8> and <Picture 10>")
        t19 = ctx.make_anchor_task(None, r19, "A02_PH3", adapted19, seed=22, sp=ctx.anchors[1], image_refs=[])
        self.assertEqual(t19["resolution"], "3872x2176")
        self.assertEqual(t19["flow_shift"], 12.0)
        self.assertEqual(t19["switch_threshold"], 0.7)
        self.assertEqual(t19["num_inference_steps2"], 3)
        self.assertEqual(t19["video_prompt_type"], "")
        self.assertIsNone(t19["image_refs"])

        _, r3 = ctx.resolve_anchor_renderer(ctx.anchors[2])
        _, _, refs3 = compile_anchor_prompt_and_refs(ctx, ctx.anchors[2], r3)
        self.assertEqual(len(refs3), 4)


if __name__ == "__main__":
    unittest.main()
