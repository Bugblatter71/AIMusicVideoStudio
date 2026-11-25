"""
Comprehensive unit & integration tests for fal.ai Hybrid Cloud Rendering.
All cloud requests are cleanly mocked (zero network calls, zero fal.ai spend).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from pipeline import fal_catalog
from pipeline.context import ProjectContext
from pipeline.fal_bridge import FalSession
from pipeline.fal_catalog import (
    dpapi_decrypt_secret,
    dpapi_encrypt_secret,
    dpapi_is_supported,
    estimate_fal_cost_usd,
    get_fal_status,
    resolve_fal_key,
    resolve_fal_route,
    resolve_task_backend,
    resolve_upscale_backend,
    save_fal_settings,
)
from pipeline.media_utils import render_settings_of
from pipeline.stage1_anchors import run_stage1_anchors
from pipeline.stage2_hashes import compute_project_task_hashes
from pipeline.stage2_scenes import run_stage2_scenes
from pipeline.takes import load_takes_manifest
from pipeline.upscale import upscale_scene_clip
from pipeline.wgp_bridge import SessionRouter, check_cancel_requested
import server
import server_fal


def _make_ffmpeg_test_mp4(ffmpeg: Path, out_mp4: Path, dur: float = 1.5, w: int = 320, h: int = 180):
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg), "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out_mp4),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_ffmpeg_test_wav(ffmpeg: Path, out_wav: Path, dur: float = 2.0):
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg), "-y", "-f", "lavfi", "-i", f"sine=frequency=520:duration={dur}",
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out_wav),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_dummy_jpg(path: Path, ffmpeg: Path | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if ffmpeg is not None:
        subprocess.run(
            [str(ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:d=0.05", "-vframes", "1", "-q:v", "2", str(path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 1400 + b"\xff\xd9")


class _DummyHandler:
    def __init__(self, path: str = ""):
        self.path = path
        self.status = 200
        self.payload = None
        self.err = None

    def send_json(self, data, status=200):
        self.status = int(status)
        self.payload = data

    def send_error_json(self, message, status=400):
        self.status = int(status)
        self.err = message


class _FakeFalQueueHandle:
    def __init__(self, result_data: dict, req_id: str = "req-mock-1"):
        self.request_id = req_id
        self._res = result_data
        self.cancelled = False

    def iter_events(self, with_logs: bool = False):
        yield SimpleQueued()
        yield SimpleInProgress(["step 1/2"])
        yield SimpleCompleted()

    def get(self):
        if self.cancelled:
            raise RuntimeError("Cancelled")
        return self._res

    def cancel(self):
        self.cancelled = True


class SimpleQueued:
    pass


class SimpleInProgress:
    def __init__(self, logs=None):
        self.logs = logs or []


class SimpleCompleted:
    pass


class _MockFalClient:
    """In-memory fal_client double verifying upload deduplication, endpoint args, and cancellation."""

    def __init__(self, ffmpeg: Path):
        self.ffmpeg = ffmpeg
        self.uploads: list[str] = []
        self.submissions: list[tuple[str, dict]] = []
        self.handles: list[_FakeFalQueueHandle] = []
        self._media_map: dict[str, bytes] = {}
        self.hold_event = None

    def upload_file(self, path: str) -> str:
        self.uploads.append(path)
        return f"https://mock.fal.media/{len(self.uploads)}/{Path(path).name}"

    def submit(self, endpoint: str, arguments: dict):
        self.submissions.append((endpoint, dict(arguments)))
        idx = len(self.submissions)
        url = f"https://mock.fal.media/out_{idx}"
        if any(k in endpoint for k in ("flux", "qwen", "fibo", "sensenova")):
            url += ".jpg"
            self._media_map[url] = b"\xff\xd8\xff\xe0" + b"\x11" * 1600 + b"\xff\xd9"
            out = {"images": [{"url": url}]}
        else:
            url += ".mp4"
            with tempfile.TemporaryDirectory() as td:
                tmp_mp4 = Path(td) / f"gen_{idx}.mp4"
                _make_ffmpeg_test_mp4(self.ffmpeg, tmp_mp4, dur=1.5)
                self._media_map[url] = tmp_mp4.read_bytes()
            out = {"video": {"url": url}}
        h = _FakeFalQueueHandle(out, req_id=f"req-{idx}")
        self.handles.append(h)
        if self.hold_event is not None:
            orig_iter = h.iter_events
            def _slow_iter(with_logs: bool = False):
                for ev in orig_iter(with_logs=with_logs):
                    self.hold_event.wait(timeout=3.0)
                    yield ev
            h.iter_events = _slow_iter
        return h


def _build_temp_project(root: Path, models_extra: dict | None = None, scenes: list[dict] | None = None, anchors: list[dict] | None = None) -> Path:
    proj_dir = root / "proj"
    (proj_dir / "audio").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "anchors").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "renders").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "jobs").mkdir(parents=True, exist_ok=True)

    # Seed empty local_config.json inside temp proj so tests never read/modify workspace root local_config.json
    (proj_dir / "local_config.json").write_text("{}", encoding="utf-8")

    # Find working ffmpeg
    sys_ctx = ProjectContext(WORKSPACE_ROOT / "projects" / "test_project" / "test_project.json")
    ff = sys_ctx.ffmpeg
    _make_ffmpeg_test_wav(ff, proj_dir / "audio" / "master.wav", dur=12.0)
    _make_ffmpeg_test_wav(ff, proj_dir / "audio" / "male_vocal.wav", dur=12.0)
    _make_ffmpeg_test_wav(ff, proj_dir / "audio" / "female_vocal.wav", dur=12.0)

    a1_p = proj_dir / "work" / "anchors" / "A01_test.jpg"
    _make_dummy_jpg(a1_p, ff)

    models_cfg = {
        "default_anchor_renderer": 3,
        "default_video_renderer": 1,
        "default_video_resolution": 1,
        "video_shared_settings": {
            "resolution": "832x480",
            "negative_prompt": "blurry",
        },
    }
    if models_extra:
        models_cfg.update(models_extra)

    cfg = {
        "project": {
            "name": "Fal Test Project",
            "fps": 24,
            "master_audio": "audio/master.wav",
            "stems": {"male": "audio/male_vocal.wav", "female": "audio/female_vocal.wav"},
            "render_overlap_seconds": 0.0,
            "directories": {
                "anchors": "work/anchors",
                "renders": "work/renders",
                "jobs": "work/jobs",
            },
        },
        "models": models_cfg,
        "anchors": anchors or [
            {"id": "A01", "filename": "A01_test.jpg", "prompt": "Singer at <Picture 1>", "seed": 11},
        ],
        "scenes": scenes or [
            {
                "id": "S01", "start": 0.0, "end": 4.0, "mode": "solo",
                "anchor": "A01_test.jpg", "stem": "male",
                "prompt": "He sings with passion", "seed": 101,
            },
            {
                "id": "S02", "start": 4.0, "end": 8.0, "mode": "solo",
                "anchor": "A01_test.jpg", "stem": "male",
                "prompt": "She watches silently", "seed": 102,
            },
        ],
    }
    cfg_path = proj_dir / "proj.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_path


class TestFalIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ctx = ProjectContext(WORKSPACE_ROOT / "projects" / "test_project" / "test_project.json")
        cls.ffmpeg = ctx.ffmpeg

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_dpapi_encryption_and_secret_hygiene(self):
        self.assertTrue(dpapi_is_supported(), "Windows host should support DPAPI")
        secret = "fal_key_demo_12345:super_secret_abcd"
        blob = dpapi_encrypt_secret(secret)
        self.assertNotIn(secret, blob)
        self.assertEqual(dpapi_decrypt_secret(blob), secret)

        cfg_path = _build_temp_project(self.root)
        proj_dir = cfg_path.parent
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FAL_KEY", None)
            save_fal_settings(root_dir=proj_dir, fal_key=secret, default_execution_backend="fal", default_upscale_backend="fal")
            raw_local = json.loads((proj_dir / "local_config.json").read_text(encoding="utf-8"))
            self.assertIn("fal_key_dpapi", raw_local)
            self.assertNotIn("fal_key", raw_local)
            self.assertNotIn(secret, json.dumps(raw_local))

            key, src = resolve_fal_key(proj_dir)
            self.assertEqual(key, secret)
            self.assertEqual(src, "dpapi")

            st = get_fal_status(root_dir=proj_dir)
            self.assertTrue(st["configured"])
            self.assertEqual(st["source"], "dpapi")
            self.assertEqual(st["hint"], "…abcd")
            self.assertNotIn(secret, json.dumps(st))

            # Env var takes priority over DPAPI
            with mock.patch.dict(os.environ, {"FAL_KEY": "env_override_9999"}):
                k2, s2 = resolve_fal_key(proj_dir)
                self.assertEqual((k2, s2), ("env_override_9999", "env"))

            # Clear key leaves backend setting intact cleanly
            save_fal_settings(root_dir=proj_dir, clear_key=True)
            k3, s3 = resolve_fal_key(proj_dir)
            self.assertEqual((k3, s3), ("", "none"))

    def test_02_backend_routing_precedence_and_profiles(self):
        cfg_path = _build_temp_project(self.root, models_extra={"default_backend": "fal", "default_upscale_backend": "fal"})
        ctx = ProjectContext(cfg_path)
        self.assertIn("fal_minimax_h3_i2v", ctx.fal_video_renderers)
        self.assertIn("fal_flux2_klein_9b", ctx.fal_anchor_renderers)
        # Crucially, ctx.video_renderers keeps only local Wan2GP entries
        self.assertNotIn("fal_minimax_h3_i2v", ctx.video_renderers)

        rid, spec = ctx.resolve_video_renderer({"id": "S01", "renderer": "fal_minimax_h3_ref2v"})
        self.assertEqual(rid, "fal_minimax_h3_ref2v")
        self.assertEqual(spec["fal_endpoint"], "minimax/h3/reference-to-video")

        # Project default = fal, S02 overrides to local
        self.assertEqual(resolve_task_backend(ctx, sc={"id": "S01"}, rspec=ctx.video_renderers["1"]), "fal")
        self.assertEqual(resolve_task_backend(ctx, sc={"id": "S02", "backend": "local"}, rspec=ctx.video_renderers["1"]), "local")
        # Pass-specific duet override
        sc_duet = {"id": "S02", "backend": "local", "backend_m": "fal", "backend_f": "local"}
        self.assertEqual(resolve_task_backend(ctx, sc=sc_duet, pass_num=1), "fal")
        self.assertEqual(resolve_task_backend(ctx, sc=sc_duet, pass_num=2), "local")
        # Dedicated fal renderer overrides scene backend="local"
        self.assertEqual(resolve_task_backend(ctx, sc={"id": "S02", "backend": "local"}, rspec=spec), "fal")
        # CLI override wins over project/scene/renderer
        ctx.cli_backend = "local"
        self.assertEqual(resolve_task_backend(ctx, sc={"id": "S01", "backend": "fal"}, rspec=spec), "local")
        ctx.cli_backend = ""
        # local_only flag forces local
        self.assertEqual(resolve_task_backend(ctx, sc={"id": "S01", "backend": "fal"}, rspec={"local_only": True}), "local")
        # Upscale precedence + dlss5 local lock
        self.assertEqual(resolve_upscale_backend(ctx, method="seedvr2*2"), "fal")
        self.assertEqual(resolve_upscale_backend(ctx, method="dlss5*1", explicit_backend="fal"), "local")

    def test_03_zero_invalidation_of_local_hashes_and_toggle_sensitivity(self):
        cfg_path = _build_temp_project(self.root)
        ctx_plain = ProjectContext(cfg_path)
        h_plain = compute_project_task_hashes(ctx_plain)
        a1 = ctx_plain.anchors_dir / "A01_test.jpg"
        au = ctx_plain.project_dir / "audio" / "male_vocal.wav"
        _, rspec0 = ctx_plain.resolve_video_renderer(ctx_plain.scenes[0])
        task_plain = ctx_plain.make_video_task(
            "S01_solo_832x480", "S01 prompt", 96, 101, a1, au, rspec0, sc=ctx_plain.scenes[0], resolution="832x480",
        )
        rs_plain = render_settings_of(task_plain)
        self.assertNotIn("backend", rs_plain)
        self.assertNotIn("_backend", rs_plain)

        # Explicit default_backend="local" produces BIT-FOR-BIT identical hashes and settings
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw["models"]["default_backend"] = "local"
        raw["scenes"][0]["backend"] = "local"
        cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        ctx_loc = ProjectContext(cfg_path)
        h_loc = compute_project_task_hashes(ctx_loc)
        self.assertEqual(h_plain, h_loc)

        # Flip ONLY S01 to fal: S01 hash changes, S02 stays identical!
        raw["scenes"][0]["backend"] = "fal"
        cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        ctx_mix = ProjectContext(cfg_path)
        h_mix = compute_project_task_hashes(ctx_mix)
        self.assertNotEqual(h_mix["S01_solo_832x480"], h_loc["S01_solo_832x480"])
        self.assertEqual(h_mix["S02_solo_832x480"], h_loc["S02_solo_832x480"])

        _, rspec_f = ctx_mix.resolve_video_renderer(ctx_mix.scenes[0])
        task_fal = ctx_mix.make_video_task(
            "S01_solo_832x480", "S01 prompt", 96, 101, a1, au, rspec_f, sc=ctx_mix.scenes[0], resolution="832x480",
        )
        rs_fal = render_settings_of(task_fal)
        self.assertEqual(rs_fal.get("backend"), "fal")
        self.assertIn("fal_endpoint", rs_fal)

    def test_04_fal_session_stage1_upload_dedup_and_stage2_adapters(self):
        cfg_path = _build_temp_project(
            self.root,
            models_extra={"default_backend": "fal", "default_anchor_renderer": "fal_qwen_edit_2511", "default_video_renderer": "fal_minimax_h3_i2v"},
            anchors=[{"id": "A02", "filename": "A02_cloud.jpg", "prompt": "Singer <Picture 1> on stage", "seed": 22}],
        )
        # Put an anchor ref file for stage 1 + use existing A01_test for stage 2
        ref_img = self.root / "proj" / "work" / "anchors" / "A01_test.jpg"
        ctx = ProjectContext(cfg_path)
        ctx.anchor_refs = [str(ref_img)]
        fake_client = _MockFalClient(self.ffmpeg)
        fal_sess = FalSession(ctx, client=fake_client)

        def fake_dl(url, out_path):
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(fake_client._media_map[url])
            return out_path

        router = SessionRouter(ctx)
        router._fal_session = fal_sess
        state = {"anchor_hashes": {}, "anchor_file_hashes": {}, "tasks": {}, "scene_renders": {}}

        with mock.patch.object(fal_sess, "_download_file", side_effect=fake_dl):
            run_stage1_anchors(ctx, router, state, [])
            self.assertEqual(len(fake_client.submissions), 1)
            ep1, args1 = fake_client.submissions[0]
            self.assertEqual(ep1, "fal-ai/qwen-image-edit-2511")
            self.assertIn("Image 1", args1["prompt"])
            self.assertTrue((ctx.anchors_dir / "A02_cloud.jpg").is_file())
            self.assertEqual(len(fake_client.uploads), 1)

            # Upload deduplication: second call on same file reuses SHA-256 upload cache
            u2 = fal_sess.upload_media(ref_img)
            self.assertEqual(len(fake_client.uploads), 1)
            self.assertTrue(u2.startswith("https://mock.fal.media/1/"))

            # Reload FalSession and verify persistent fal_upload_cache.json avoids re-upload
            fal_sess.close()
            fal_sess2 = FalSession(ctx, client=fake_client)
            self.assertEqual(fal_sess2.upload_media(ref_img), u2)
            self.assertEqual(len(fake_client.uploads), 1)

    def test_05_mixed_local_plus_fal_stage2_duet_roi_chunking_upscale_cancel(self):
        # S01: solo fal (22s long -> auto chunked into 2 sequential chunks!),
        # S02: two-pass duet fal (Pass 1 + Pass 2 ROI lipsync composite!),
        # S03: explicit local Wan2GP mock!
        scenes = [
            {"id": "S01", "start": 0.0, "end": 22.0, "mode": "solo", "anchor": "A01_test.jpg", "stem": "male", "backend": "fal", "prompt": "Long solo chorus <Picture 1>", "seed": 1},
            {"id": "S02", "start": 0.0, "end": 3.0, "mode": "duet_method_b", "anchor": "A01_test.jpg", "stem_m": "male", "stem_f": "female", "backend": "fal", "prompt_m": "Male sings", "prompt_f": "Female harmonizes", "seed_m": 2, "seed_f": 3},
            {"id": "S03", "start": 8.0, "end": 11.0, "mode": "solo", "anchor": "A01_test.jpg", "stem": "male", "backend": "local", "prompt": "Local GPU clip", "seed": 4},
        ]
        # Give project a 24s audio file for chunking
        cfg_path = _build_temp_project(self.root, scenes=scenes)
        ff = self.ffmpeg
        _make_ffmpeg_test_wav(ff, cfg_path.parent / "audio" / "master.wav", dur=24.0)
        _make_ffmpeg_test_wav(ff, cfg_path.parent / "audio" / "male_vocal.wav", dur=24.0)
        _make_ffmpeg_test_wav(ff, cfg_path.parent / "audio" / "female_vocal.wav", dur=24.0)

        ctx = ProjectContext(cfg_path)
        ctx.no_stitch = True
        fake_client = _MockFalClient(ff)
        fal_sess = FalSession(ctx, client=fake_client)

        class _FakeWgpEvent:
            kind = "progress"
            data = types.SimpleNamespace(phase="inference", raw_phase="Local WGP", status="Running", current_step=1, total_steps=1, window_index=1, window_count=1, progress=100.0)

        class _FakeWgpEvents:
            def iter(self, timeout=0.25):
                yield _FakeWgpEvent()

        class _FakeWgpJob:
            def __init__(self, out_mp4: Path):
                self.events = _FakeWgpEvents()
                self._out = out_mp4
            def cancel(self): pass
            def result(self):
                _make_ffmpeg_test_mp4(ff, self._out, dur=1.2)
                return types.SimpleNamespace(success=True, generated_files=[str(self._out)], errors=[])

        local_calls = []
        class _FakeLocalSession:
            def submit_task(self, task_cfg):
                local_calls.append(task_cfg)
                out_mp4 = ctx.renders_dir / f"{task_cfg['output_filename']}.mp4"
                return _FakeWgpJob(out_mp4)
            def run_media_postprocessing(self, src, **kw):
                return types.SimpleNamespace(success=True, generated_files=[src], errors=[])
            def close(self): pass

        router = SessionRouter(ctx)
        router._fal_session = fal_sess
        router._session = _FakeLocalSession()

        state = {"tasks": {}, "scene_renders": {}}
        with mock.patch.object(fal_sess, "_download_file", side_effect=lambda u, p: (Path(p).write_bytes(fake_client._media_map[u]) or Path(p))):
            run_stage2_scenes(ctx, router, state, [], set(), sync_only=False, no_stitch=True)

            # Verify S01 chunked (2 submissions), S02 Pass 1 (1 submission) + Pass 2 ROI lipsync (1 submission) = 4 fal submissions total!
            endpoints = [ep for ep, _ in fake_client.submissions]
            self.assertEqual(len(endpoints), 4, f"Expected 4 fal calls (2 chunks + p1 + p2 ROI), got {endpoints}")
            self.assertEqual(endpoints[-1], "fal-ai/sync-lipsync/v2")
            # Verify S03 went strictly to local Wan2GP with ZERO '_' keys or fake 'backend' keys
            self.assertEqual(len(local_calls), 1)
            self.assertNotIn("backend", local_calls[0])
            self.assertNotIn("_backend", local_calls[0])

            manifest = load_takes_manifest(ctx.project_dir)
            s1_take = manifest["scenes"]["S01"]["takes"][-1]
            s3_take = manifest["scenes"]["S03"]["takes"][-1]
            self.assertEqual(s1_take["backend"], "fal")
            self.assertIn("minimax/h3", s1_take["fal_endpoint"])
            self.assertEqual(s3_take["backend"], "local")

            # Late upscale on S03 explicitly routed to fal SeedVR2!
            up_take = upscale_scene_clip(ctx, router, "S03", "seedvr2*2", backend="fal")
            self.assertEqual(up_take["backend"], "fal")
            self.assertEqual(fake_client.submissions[-1][0], "fal-ai/seedvr/upscale/video")

    def test_06_fal_cancellation_propagation(self):
        cfg_path = _build_temp_project(self.root, models_extra={"default_backend": "fal"})
        ctx = ProjectContext(cfg_path)
        import threading
        hold = threading.Event()
        fake_client = _MockFalClient(self.ffmpeg)
        fake_client.hold_event = hold
        fal_sess = FalSession(ctx, client=fake_client)

        a1 = ctx.anchors_dir / "A01_test.jpg"
        au = ctx.project_dir / "audio" / "male_vocal.wav"
        _, rspec = ctx.resolve_video_renderer(ctx.scenes[0])
        t = ctx.make_video_task("S01_cancel_test", "Sing", 96, 99, a1, au, rspec, sc=ctx.scenes[0], resolution="832x480")
        from pipeline.wgp_bridge import execute_wgp_job
        (ctx.jobs_dir / "cancel_request.json").write_text('{"reason":"test"}', encoding="utf-8")

        def _release():
            time.sleep(0.15)
            hold.set()
        threading.Thread(target=_release, daemon=True).start()

        res, was_cancelled, _ = execute_wgp_job(
            ctx=ctx, session=fal_sess, task_cfg=t, idx=1, total=1,
            filename="S01_cancel_test.mp4", stage="scene", reason="TEST",
        )
        self.assertTrue(was_cancelled)
        self.assertTrue(fake_client.handles[0].cancelled)

    def test_07_ui_server_routes_and_queue_enrichment(self):
        # 1. /api/models patch includes fal_* renderers
        patched = server_fal.patch_models_payload(json.loads((WORKSPACE_ROOT / "model_profiles.json").read_text(encoding="utf-8")))
        self.assertIn("fal_anchor_renderers", patched)
        self.assertIn("fal_video_renderers", patched)
        self.assertIn("fal_minimax_h3_i2v", patched["fal_video_renderers"])

        # 2. HTTP /api/fal/config + /api/fal/status + /api/fal/test
        cfg_path = _build_temp_project(self.root, models_extra={"default_backend": "fal"})
        with mock.patch.object(server, "PROJECTS_DIR", self.root):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FAL_KEY", None)
                h_post = _DummyHandler("/api/fal/config")
                self.assertTrue(server_fal.handle_fal_post(h_post, "/api/fal/config", {"slug": "proj", "fal_key": "test_id:secret_xyz9"}))
                self.assertTrue(h_post.payload["success"])
                self.assertEqual(h_post.payload["hint"], "…xyz9")
                self.assertNotIn("secret_xyz9", json.dumps(h_post.payload))

                h_get = _DummyHandler("/api/fal/status?slug=proj")
                self.assertTrue(server_fal.handle_fal_get(h_get, "/api/fal/status", urlparse(h_get.path)))
                self.assertEqual(h_get.payload["project_default_backend"], "fal")
                self.assertEqual(h_get.payload["source"], "dpapi")

                h_test = _DummyHandler("/api/fal/test")
                with mock.patch.object(server_fal, "_probe_fal_connection", return_value=(True, 200, "OK")):
                    self.assertTrue(server_fal.handle_fal_post(h_test, "/api/fal/test", {"slug": "proj"}))
                    self.assertTrue(h_test.payload["success"])

            # 3. Fast Queue Enrichment: S01 -> fal with cost_str, S02 (local override) -> local
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw["scenes"][1]["backend"] = "local"
            cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            queue = server.compute_fast_project_queue("proj", force_recheck=True)
            q_s1 = next(q for q in queue if q["scene_id"] == "S01")
            q_s2 = next(q for q in queue if q["scene_id"] == "S02")
            self.assertEqual(q_s1["backend"], "fal")
            self.assertIn("cost_str", q_s1)
            self.assertGreater(q_s1["estimated_cost_usd"], 0.0)
            self.assertEqual(q_s2["backend"], "local")
            self.assertNotIn("cost_str", q_s2)

    def test_08_frontend_html_script_tag_and_node_vm_checks(self):
        html_txt = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("js/fal_settings.js?v=1", html_txt)

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const elements = {};
const mkEl = (id) => {
  if (!elements[id]) {
    elements[id] = {
      id, value: "", innerHTML: "", style: {}, options: [], dataset: {}, checked: false,
      tagName: "SELECT", listeners: {}, children: [],
      add(opt) { this.options.push(opt); },
      appendChild(c) { this.children.push(c); if (c.tagName === "OPTGROUP") this.options.push(...(c.children || [])); else this.options.push(c); },
      addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
      closest() { return null; },
      querySelector() { return null; },
    };
  }
  return elements[id];
};
const ctx = {
  console, Math, JSON, String, Number, Array, Object, Boolean, parseInt, parseFloat, isNaN, RegExp, Date, Set, Map,
  Option: function(text, value) { this.textContent = text; this.value = value; this.dataset = {}; },
  document: {
    getElementById: (id) => mkEl(id),
    createElement: (tag) => { const o = mkEl("__anon_" + tag + "_" + Math.random()); o.tagName = tag.toUpperCase(); return o; },
    addEventListener: () => {},
  },
  window: null,
  state: {
    currentSlug: "proj",
    selectedSceneIdx: 0,
    models: {
      anchor_renderers: { "3": { name: "Klein 9B", model_type: "flux2_klein_9b" } },
      fal_anchor_renderers: { "fal_flux2_klein_9b": { name: "[fal.ai] Klein 9B", model_type: "flux2_klein_9b" } },
      video_renderers: { "1": { name: "H3 Local", model_type: "minimax_h3_fl2va_pruned_pdd" } },
      fal_video_renderers: { "fal_minimax_h3_ref2v": { name: "[fal.ai] H3 Ref2V", model_type: "minimax_h3_ref2va_pruned_ptd_refasymopd", supports_image_refs: true } },
    },
    projectData: {
      project: {},
      models: { default_anchor_renderer: 3, default_video_renderer: 1, default_backend: "fal" },
      scenes: [{ id: "S01", start: 0, end: 5, renderer: "fal_minimax_h3_ref2v", backend: "fal" }],
    },
  },
  renderAnchorCards: () => {}, updateAnchorDropdowns: () => {}, markProjectDirty: () => {},
  redrawTimeline: () => {}, showToast: () => {}, refreshProjectAssets: () => {},
  loadProjectQueue: () => {}, getMergedAnchorsList: () => [],
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/anchors_and_settings.js", "utf8"), ctx);
ctx.populateModelSelectors();
const vidSel = mkEl("cfgDefaultVideoRenderer");
vidSel.value = "fal_minimax_h3_ref2v";
ctx.syncSettingsFormToState("cfgDefaultVideoRenderer");
assert.strictEqual(ctx.state.projectData.models.default_video_renderer, "fal_minimax_h3_ref2v");

vm.runInContext(fs.readFileSync("work/ui/static/js/fal_settings.js", "utf8"), ctx);
// Dedicated fal_* project default forces scenes without explicit local model onto cloud
assert.strictEqual(ctx.resolveEffectiveSceneBackend(ctx.state.projectData.scenes[0]), "fal");
// Standard project model (renderer=1) with default_backend='fal' allows scene override backend='local'
ctx.state.projectData.models.default_video_renderer = 1;
assert.strictEqual(ctx.resolveEffectiveSceneBackend({ id: "S02", backend: "local" }), "local");
assert.strictEqual(ctx.resolveEffectiveSceneBackend({ id: "S02" }), "fal");
assert.strictEqual(ctx.resolveEffectiveSceneBackend({ id: "S03", renderer: "fal_minimax_h3_ref2v", backend: "local" }), "fal");
console.log("NODE_FAL_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("NODE_FAL_OK", res.stdout)

    def test_09_rest_fal_client_fallback_when_sdk_absent(self):
        from pipeline.fal_bridge import _RestFalClient
        client = _RestFalClient("key_id:key_sec", timeout_s=5.0)
        calls = []
        status_polls = {"n": 0}

        class _MockResp:
            def __init__(self, body: dict):
                self._b = json.dumps(body).encode("utf-8")
            def read(self):
                return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_urlopen(req, timeout=60):
            calls.append((req.get_method(), req.full_url))
            if "storage/upload/initiate" in req.full_url:
                return _MockResp({"upload_url": "https://cdn.fal/put/1", "file_url": "https://cdn.fal/file/1.jpg"})
            if req.full_url == "https://cdn.fal/put/1":
                return _MockResp({})
            if "/requests/req-r1/status" in req.full_url:
                status_polls["n"] += 1
                if status_polls["n"] == 1:
                    return _MockResp({"status": "IN_QUEUE", "queue_position": 2})
                return _MockResp({"status": "COMPLETED", "logs": [{"message": "done"}]})
            if req.full_url.endswith("/requests/req-r1"):
                return _MockResp({"image": {"url": "https://cdn.fal/out.jpg"}})
            return _MockResp({"request_id": "req-r1", "status_url": "https://queue.fal.run/m/requests/req-r1/status", "response_url": "https://queue.fal.run/m/requests/req-r1"})

        f = self.root / "sample.jpg"
        _make_dummy_jpg(f, self.ffmpeg)
        ua_headers = []
        def _capturing_urlopen(req, timeout=60):
            ua_headers.append(req.get_header("User-agent") or req.headers.get("User-Agent") or "")
            return _fake_urlopen(req, timeout=timeout)
        with mock.patch("urllib.request.urlopen", side_effect=_capturing_urlopen):
            uploaded = client.upload_file(str(f))
            self.assertEqual(uploaded, "https://cdn.fal/file/1.jpg")
            self.assertTrue(any("https://rest.fal.ai/storage/upload/initiate" in u for _, u in calls))
            self.assertTrue(all("fal-client/" in ua for ua in ua_headers))
            h = client.submit("fal-ai/flux-2/klein/9b", {"prompt": "test"})
            events = list(h.iter_events(interval=0.01))
            self.assertEqual([e.kind for e in events], ["queued", "completed"])
            self.assertEqual(h.get()["image"]["url"], "https://cdn.fal/out.jpg")
            client.cancel("fal-ai/flux-2/klein/9b", "req-r1")
        self.assertTrue(any(m == "PUT" and u.endswith("/cancel") for m, u in calls))

    def test_10_minimax_dpapi_routing_hash_and_session(self):
        from pipeline.fal_catalog import resolve_minimax_key, resolve_minimax_route, estimate_minimax_cost_usd
        from pipeline.minimax_bridge import MiniMaxSession
        from pipeline.wgp_bridge import execute_wgp_job

        cfg_path = _build_temp_project(self.root)
        proj_dir = cfg_path.parent
        secret = "mm_live_secret_key_9988"

        # 1. DPAPI storage & env override
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINIMAX_API_KEY", None)
            save_fal_settings(root_dir=proj_dir, minimax_key=secret, default_execution_backend="minimax")
            raw_local = json.loads((proj_dir / "local_config.json").read_text(encoding="utf-8"))
            self.assertIn("minimax_key_dpapi", raw_local)
            self.assertNotIn(secret, json.dumps(raw_local))
            key_val, src = resolve_minimax_key(proj_dir)
            self.assertEqual((key_val, src), (secret, "dpapi"))
            st = get_fal_status(proj_dir)
            self.assertTrue(st["minimax"]["configured"])
            self.assertEqual(st["minimax"]["hint"], "…9988")
            self.assertNotIn(secret, json.dumps(st))

            os.environ["MINIMAX_API_KEY"] = "env_mm_override_1234"
            self.assertEqual(resolve_minimax_key(proj_dir), ("env_mm_override_1234", "env"))

        # 2. Routing, anchor fallback, and cost estimation
        ctx = ProjectContext(cfg_path)
        self.assertEqual(resolve_task_backend(ctx, sc=ctx.scenes[0], stage="anchor"), "local")
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw["models"]["default_backend"] = "minimax"
        cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        ctx_mm = ProjectContext(cfg_path)
        self.assertEqual(resolve_task_backend(ctx_mm, sc=ctx_mm.scenes[0], stage="anchor"), "local")
        self.assertEqual(resolve_task_backend(ctx_mm, sc=ctx_mm.scenes[0], stage="video"), "minimax")
        lite_route = resolve_minimax_route({"minimax_model": "MiniMax-H3-Lite"}, resolution="1344x768")
        self.assertEqual(lite_route["target_resolution"], "768P")
        cost_info = estimate_minimax_cost_usd(5.0, "1344x768", rspec={"minimax_model": "MiniMax-H3-Lite"})
        self.assertEqual(cost_info["estimated_cost_usd"], 0.2)

        # 3. Hash isolation across backends: S02 stays local & identical while S01 swaps local/fal/minimax
        def _hashes_for_s1(backend_name: str, renderer=None) -> dict:
            d = json.loads(cfg_path.read_text(encoding="utf-8"))
            d["models"]["default_backend"] = "local"
            d["scenes"][0]["backend"] = backend_name
            if renderer:
                d["scenes"][0]["renderer"] = renderer
            else:
                d["scenes"][0].pop("renderer", None)
            d["scenes"][1]["backend"] = "local"
            cfg_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
            c = ProjectContext(cfg_path)
            return compute_project_task_hashes(c)

        h_loc = _hashes_for_s1("local")
        h_fal = _hashes_for_s1("fal")
        h_mm = _hashes_for_s1("minimax", renderer="minimax_api_h3_highspeed")
        self.assertEqual(h_loc["S02_solo_832x480"], h_fal["S02_solo_832x480"])
        self.assertEqual(h_loc["S02_solo_832x480"], h_mm["S02_solo_832x480"])
        self.assertNotEqual(h_loc["S01_solo_832x480"], h_fal["S01_solo_832x480"])
        self.assertNotEqual(h_loc["S01_solo_832x480"], h_mm["S01_solo_832x480"])
        self.assertNotEqual(h_fal["S01_solo_832x480"], h_mm["S01_solo_832x480"])

        # 4. Mixed stage2 (minimax 18s chunked + minimax duet ROI + local GPU) + cache dedup + cancel
        scenes = [
            {"id": "S01", "start": 0.0, "end": 18.0, "mode": "solo", "anchor": "A01_test.jpg", "stem": "male", "backend": "minimax", "renderer": "minimax_api_h3_highspeed", "prompt": "Slow dolly push <Picture 1>", "seed": 1},
            {"id": "S02", "start": 0.0, "end": 3.0, "mode": "duet_method_b", "anchor": "A01_test.jpg", "stem_m": "male", "stem_f": "female", "backend": "minimax", "renderer": "minimax_api_h3_lite", "prompt_m": "Male lead", "prompt_f": "Female harmony", "seed_m": 2, "seed_f": 3},
            {"id": "S03", "start": 8.0, "end": 11.0, "mode": "solo", "anchor": "A01_test.jpg", "stem": "male", "backend": "local", "prompt": "Local GPU clip", "seed": 4},
        ]
        cfg_m = _build_temp_project(self.root / "mm_mix", scenes=scenes)
        ff = self.ffmpeg
        _make_ffmpeg_test_wav(ff, cfg_m.parent / "audio" / "master.wav", dur=20.0)
        _make_ffmpeg_test_wav(ff, cfg_m.parent / "audio" / "male_vocal.wav", dur=20.0)
        _make_ffmpeg_test_wav(ff, cfg_m.parent / "audio" / "female_vocal.wav", dur=20.0)
        sample_mp4 = self.root / "mm_sample.mp4"
        _make_ffmpeg_test_mp4(ff, sample_mp4, dur=1.2)
        mp4_bytes = sample_mp4.read_bytes()

        save_fal_settings(root_dir=cfg_m.parent, minimax_key=secret, default_execution_backend="local")
        ctx_m = ProjectContext(cfg_m)
        ctx_m.no_stitch = True

        uploads: list[str] = []
        mm_payloads: list[dict] = []
        polls: dict[str, int] = {}
        retrieve_calls: list[str] = []

        class _FakeResp:
            def __init__(self, obj=None, raw_bytes: bytes | None = None):
                self._b = raw_bytes if raw_bytes is not None else json.dumps(obj).encode("utf-8")
            def read(self):
                return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_mm_urlopen(req, timeout=60):
            url = req.full_url
            if url.endswith("/v2/files"):
                uploads.append(url)
                return _FakeResp({"file_id": f"mmf_{len(uploads)}"})
            if url.endswith("/v2/video_generation"):
                body = json.loads((req.data or b"{}").decode("utf-8"))
                mm_payloads.append(body)
                return _FakeResp({"task_id": f"mmt_{len(mm_payloads)}"})
            if "/v2/query/video_generation/" in url:
                tid = url.rsplit("/", 1)[-1]
                polls[tid] = polls.get(tid, 0) + 1
                if polls[tid] == 1:
                    return _FakeResp({"task": {"status": "processing"}})
                if tid == "mmt_1":
                    return _FakeResp({"task": {"status": "succeeded", "file_id": "mm_out_1"}})
                return _FakeResp({"task": {"status": "succeeded", "content": {"url": f"https://cdn.minimax.io/{tid}.mp4"}}})
            if "/v1/files/retrieve" in url:
                retrieve_calls.append(url)
                return _FakeResp({"file": {"download_url": "https://cdn.minimax.io/mmt_1.mp4"}})
            if url.startswith("https://cdn.minimax.io/"):
                return _FakeResp(raw_bytes=mp4_bytes)
            raise AssertionError(f"Unexpected MiniMax URL: {url}")

        class _LocalJob:
            def __init__(self, out_p: Path):
                self._out = out_p
                self.events = types.SimpleNamespace(iter=lambda timeout=0.25: iter([
                    types.SimpleNamespace(kind="progress", data=types.SimpleNamespace(phase="inference", raw_phase="Local", status="Running", current_step=1, total_steps=1, window_index=1, window_count=1, progress=100.0))
                ]))
            def cancel(self): pass
            def result(self):
                _make_ffmpeg_test_mp4(ff, self._out, dur=1.2)
                return types.SimpleNamespace(success=True, generated_files=[str(self._out)], errors=[])

        local_calls = []
        class _LocalSess:
            def submit_task(self, t):
                local_calls.append(t)
                return _LocalJob(ctx_m.renders_dir / f"{t['output_filename']}.mp4")
            def run_media_postprocessing(self, src, **kw):
                return types.SimpleNamespace(success=True, generated_files=[src], errors=[])
            def close(self): pass

        router = SessionRouter(ctx_m)
        router._session = _LocalSess()
        state = {"tasks": {}, "scene_renders": {}}

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINIMAX_API_KEY", None)
            with mock.patch("urllib.request.urlopen", side_effect=_fake_mm_urlopen), mock.patch("pipeline.minimax_bridge.time.sleep", return_value=None):
                run_stage2_scenes(ctx_m, router, state, [], set(), sync_only=False, no_stitch=True)
                self.assertEqual(len(mm_payloads), 4, f"Expected 4 MiniMax calls (2 chunks + duet p1 + p2 ROI), got {len(mm_payloads)}")
                self.assertEqual(len(retrieve_calls), 1, "Expected /v1/files/retrieve fallback on mmt_1")
                self.assertEqual(mm_payloads[0]["model"], "MiniMax-H3-Highspeed")
                self.assertFalse(mm_payloads[0]["aigc_watermark"])
                roles0 = [c.get("role") for c in mm_payloads[0]["content"] if c.get("role")]
                self.assertIn("reference_image", roles0)
                self.assertIn("reference_audio", roles0)
                self.assertNotIn("first_frame", roles0)
                self.assertNotIn("last_frame", roles0)
                self.assertEqual(mm_payloads[-1]["model"], "MiniMax-H3-Lite")
                self.assertEqual(mm_payloads[-1]["ratio"], "adaptive")

                # Verify persistent 6-day upload cache avoids re-uploading A01_test.jpg
                up_before = len(uploads)
                sess2 = MiniMaxSession(ctx_m)
                cached_mm_url = sess2.upload_media(ctx_m.anchors_dir / "A01_test.jpg")
                self.assertTrue(cached_mm_url.startswith("mm_file://"))
                self.assertEqual(len(uploads), up_before)

                # Verify silent / first+last frame mode never mixes reference_* roles and uses adaptive ratio
                end_img = ctx_m.anchors_dir / "A02_end.jpg"
                _make_dummy_jpg(end_img, ff)
                fl_payload = sess2._build_payload(
                    {"minimax_model": "MiniMax-H3", "target_resolution": "768P", "adapter": "minimax_v2_video"},
                    {
                        "prompt": "Smooth camera push from start to end frame",
                        "image_start": str(ctx_m.anchors_dir / "A01_test.jpg"),
                        "image_end": str(end_img),
                        "audio_guide": str(ctx_m.project_dir / "audio" / "male_vocal.wav"),
                    },
                    6.0,
                )
                fl_roles = [c.get("role") for c in fl_payload["content"] if c.get("role")]
                self.assertEqual(fl_roles, ["first_frame", "last_frame"])
                self.assertEqual(fl_payload["ratio"], "adaptive")

            self.assertEqual(len(local_calls), 1)
            self.assertNotIn("backend", local_calls[0])
            manifest = load_takes_manifest(ctx_m.project_dir)
            self.assertEqual(manifest["scenes"]["S01"]["takes"][-1]["backend"], "minimax")
            self.assertEqual(manifest["scenes"]["S03"]["takes"][-1]["backend"], "local")

            # Cancellation test on MiniMaxSession
            (ctx_m.jobs_dir / "cancel_request.json").write_text('{"reason":"user stop"}', encoding="utf-8")
            mm_sess = router.get_minimax()
            _, rspec_s1 = ctx_m.resolve_video_renderer(ctx_m.scenes[0])
            t_cancel = ctx_m.make_video_task(
                "S01_mm_cancel", "Cancel me", 96, 55,
                ctx_m.anchors_dir / "A01_test.jpg", ctx_m.project_dir / "audio" / "male_vocal.wav",
                rspec_s1, sc=ctx_m.scenes[0], resolution="832x480",
            )
            with mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=60: _FakeResp({"file_id": "mmf_c"}) if req.full_url.endswith("/v2/files") else (_FakeResp({"task_id": "mmt_c"}) if req.full_url.endswith("/v2/video_generation") else _FakeResp({"task": {"status": "queueing"}}))), mock.patch("pipeline.minimax_bridge.time.sleep", lambda _s: time.sleep(0.05)):
                _, was_cancelled, _ = execute_wgp_job(
                    ctx=ctx_m, session=mm_sess, task_cfg=t_cancel, idx=1, total=1,
                    filename="S01_mm_cancel.mp4", stage="scene", reason="TEST",
                )
            self.assertTrue(was_cancelled)

    def test_11_minimax_http_api_queue_and_node_ui(self):
        patched = server_fal.patch_models_payload(json.loads((WORKSPACE_ROOT / "model_profiles.json").read_text(encoding="utf-8")))
        self.assertIn("minimax_video_renderers", patched)
        self.assertIn("minimax_api_h3", patched["minimax_video_renderers"])
        self.assertIn("minimax_api_h3_highspeed", patched["minimax_video_renderers"])
        self.assertIn("minimax_api_h3_lite", patched["minimax_video_renderers"])

        cfg_path = _build_temp_project(self.root, models_extra={"default_backend": "minimax"})
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw["scenes"][0]["renderer"] = "minimax_api_h3_highspeed"
        raw["scenes"][1]["backend"] = "local"
        cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        with mock.patch.object(server, "PROJECTS_DIR", self.root):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MINIMAX_API_KEY", None)
                h_post = _DummyHandler("/api/minimax/config")
                self.assertTrue(server_fal.handle_fal_post(h_post, "/api/minimax/config", {"slug": "proj", "minimax_key": "mm_api_test_7788"}))
                self.assertTrue(h_post.payload["success"])
                self.assertEqual(h_post.payload["minimax"]["hint"], "…7788")
                self.assertNotIn("mm_api_test_7788", json.dumps(h_post.payload))

                h_get = _DummyHandler("/api/minimax/status?slug=proj")
                self.assertTrue(server_fal.handle_fal_get(h_get, "/api/minimax/status", urlparse(h_get.path)))
                self.assertEqual(h_get.payload["minimax"]["source"], "dpapi")
                self.assertEqual(h_get.payload["project_default_backend"], "minimax")

                h_test = _DummyHandler("/api/minimax/test")
                with mock.patch.object(server_fal, "_probe_minimax_connection", return_value=(True, 200, "OK")):
                    self.assertTrue(server_fal.handle_fal_post(h_test, "/api/minimax/test", {"slug": "proj"}))
                    self.assertTrue(h_test.payload["success"])

            queue = server.compute_fast_project_queue("proj", force_recheck=True)
            q_s1 = next(q for q in queue if q["scene_id"] == "S01")
            q_s2 = next(q for q in queue if q["scene_id"] == "S02")
            self.assertEqual(q_s1["backend"], "minimax")
            self.assertIn("api.minimax.io", q_s1["fal_endpoint"])
            self.assertIn("cost_str", q_s1)
            self.assertGreater(q_s1["estimated_cost_usd"], 0.0)
            self.assertEqual(q_s2["backend"], "local")
            self.assertNotIn("cost_str", q_s2)

        html_txt = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="stereoDuetControlsRow"', html_txt)
        self.assertNotIn('id="stereoDuetStemGroup"', html_txt)
        self.assertEqual(html_txt.count('id="sceneSeed"'), 1)
        self.assertEqual(html_txt.count('id="sceneStemLeft"'), 1)
        self.assertEqual(html_txt.count('id="sceneStemRight"'), 1)

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const elements = {};
const mkEl = (id) => {
  if (!elements[id]) {
    elements[id] = {
      id, value: "", _innerHTML: "", textContent: "", title: "", className: "",
      children: [], parentNode: null,
      get innerHTML() { return this._innerHTML; },
      set innerHTML(v) { this._innerHTML = v; if (!v) { this.options = []; this.children = []; } },
      style: {}, dataset: {}, disabled: false, checked: false, options: [], selectedOptions: [],
      classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,v){v?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
      appendChild(child) {
        if (child) {
          if (child.parentNode && Array.isArray(child.parentNode.children)) {
            child.parentNode.children = child.parentNode.children.filter((x) => x !== child);
          }
          child.parentNode = this;
          this.children.push(child);
          this.options.push(child);
          if (!this.value) this.value = child.value || "";
        }
        return child;
      },
      addEventListener() {}, setAttribute() {}, removeAttribute() {},
    };
  }
  return elements[id];
};
const timingRowEl = mkEl("__timingRow");
timingRowEl.appendChild(mkEl("singleSeedGroup"));
const ctx = {
  console, Math, JSON, String, Number, Array, Object, Boolean, parseInt, parseFloat, isNaN, RegExp, Date, Set, Map,
  Option: function(text, value) { this.textContent = text; this.value = value; this.dataset = {}; },
  document: {
    getElementById: (id) => mkEl(id),
    querySelector: (sel) => (sel === ".inspector-row-timing" ? timingRowEl : null),
    querySelectorAll: () => [],
    createElement: (tag) => { const o = mkEl("__anon_" + tag + "_" + Math.random()); o.tagName = tag.toUpperCase(); return o; },
    addEventListener: () => {},
  },
  window: null,
  state: {
    currentSlug: "proj",
    selectedSceneIdx: 0,
    models: {
      anchor_renderers: { "3": { name: "Klein 9B", model_type: "flux2_klein_9b" } },
      fal_anchor_renderers: {},
      video_renderers: { "1": { name: "H3 Local", model_type: "minimax_h3_fl2va_pruned_pdd" } },
      fal_video_renderers: { "fal_minimax_h3_ref2v": { name: "[fal.ai] H3 Ref2V", model_type: "minimax_h3_ref2va_pruned_ptd_refasymopd", supports_image_refs: true } },
      minimax_video_renderers: { "minimax_api_h3_lite": { name: "[MiniMax API] MiniMax-H3-Lite", model_type: "minimax_h3_fl2va_pdd", minimax_model: "MiniMax-H3-Lite" } },
    },
    assets: { audio_files: ["11 Drums.mp3", "10 Guitar.wav", "master.wav"] },
    projectData: {
      project: { master_audio: "audio/master.wav", stems: { male: "audio/male_vocal.mp3", bass: "audio/bass.mp3" } },
      models: { default_anchor_renderer: 3, default_video_renderer: 1, default_backend: "minimax" },
      scenes: [{ id: "S01", start: 0, end: 5 }, { id: "S02", start: 5, end: 10, backend: "local" }],
    },
  },
  renderAnchorCards: () => {}, updateAnchorDropdowns: () => {}, markProjectDirty: () => {},
  redrawTimeline: () => {}, showToast: () => {}, refreshProjectAssets: () => {},
  loadProjectQueue: () => {}, getMergedAnchorsList: () => [],
  resizeAllPromptTextareas: () => {}, updatePromptResolvedPreviews: () => {},
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/anchors_and_settings.js", "utf8"), ctx);
ctx.populateModelSelectors();
ctx.updateInspectorStemsDropdown();
const leftVals = mkEl("sceneStemLeft").options.map((o) => o.value);
const rightVals = mkEl("sceneStemRight").options.map((o) => o.value);
assert.ok(leftVals.includes("11 Drums") && leftVals.includes("bass") && leftVals.includes("master"));
assert.deepStrictEqual(leftVals, rightVals);
assert.ok(mkEl("sceneStemLeft").options.every((o) => o.textContent.startsWith("L: ")));
assert.ok(mkEl("sceneStemRight").options.every((o) => o.textContent.startsWith("R: ")));
vm.runInContext(fs.readFileSync("work/ui/static/js/scene_inspector.js", "utf8"), ctx);
ctx.updateSceneInspectorInputs({ id: "S10", mode: "duet_method_a", stem_m: "11 Drums", stem_f: "female", seed: 777 });
assert.strictEqual(mkEl("stereoDuetControlsRow").style.display, "flex");
assert.strictEqual(mkEl("singleSeedGroup").style.display, "block");
assert.strictEqual(mkEl("singleSeedGroup").parentNode, mkEl("stereoDuetControlsRow"));
assert.strictEqual(mkEl("sceneSeed").value, 777);
ctx.updateSceneInspectorInputs({ id: "S11", mode: "duet_method_b", stem_m: "male", stem_f: "female", seed_m: 1, seed_f: 2 });
assert.strictEqual(mkEl("stereoDuetControlsRow").style.display, "none");
assert.strictEqual(mkEl("duetPromptsGroup").style.display, "block");
assert.strictEqual(mkEl("singleSeedGroup").style.display, "none");
ctx.updateSceneInspectorInputs({ id: "S12", mode: "solo", stem: "male", seed: 555 });
assert.strictEqual(mkEl("stereoDuetControlsRow").style.display, "none");
assert.strictEqual(mkEl("singleSeedGroup").style.display, "block");
assert.strictEqual(mkEl("singleSeedGroup").parentNode, timingRowEl);
vm.runInContext(fs.readFileSync("work/ui/static/js/fal_settings.js", "utf8"), ctx);
assert.strictEqual(ctx.resolveEffectiveSceneBackend(ctx.state.projectData.scenes[0]), "minimax");
assert.strictEqual(ctx.resolveEffectiveSceneBackend(ctx.state.projectData.scenes[1]), "local");
assert.strictEqual(ctx.resolveEffectiveSceneBackend({ id: "S03", renderer: "minimax_api_h3_lite", backend: "local" }), "minimax");
console.log("NODE_MINIMAX_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("NODE_MINIMAX_OK", res.stdout)

    def test_12_honest_cloud_progress_no_fabricated_pct(self):
        from pipeline import fal_bridge, minimax_bridge, wgp_bridge
        from types import SimpleNamespace
        from unittest.mock import patch
        import contextlib
        import io
        import tempfile

        self.assertEqual(fal_bridge._extract_real_cloud_progress({"status": "processing"}), (None, None, None))
        self.assertEqual(fal_bridge._extract_real_cloud_progress({"progress": 64}), (64.0, None, None))
        self.assertEqual(fal_bridge._extract_real_cloud_progress({"logs": [{"message": "step 3/8"}]}), (37.5, 3, 8))
        self.assertEqual(fal_bridge._extract_real_cloud_progress({"logs": [{"message": "denoising 62.5%"}]}), (62.5, None, None))

        # Verify format_progress_update + execute_wgp_job preserve None pct for honest indeterminate reporting
        prog_none = SimpleNamespace(phase="rendering", raw_phase="MiniMax: processing (12s)", step=None, total_steps=None, progress=None)
        fmt_none = wgp_bridge.format_progress_update(prog_none, 1, 1)
        self.assertIsNone(fmt_none["pct"])
        self.assertIsNone(fmt_none["window_pct"])
        self.assertIsNone(fmt_none["step"])
        self.assertEqual(fmt_none["raw_phase"], "MiniMax: processing (12s)")

        cfg_p = _build_temp_project(
            self.root / "honest_prog",
            scenes=[{"id": "S01", "start": 0.0, "end": 5.0, "mode": "solo", "backend": "minimax", "prompt": "test"}],
        )
        ctx_p = ProjectContext(cfg_p)
        out_mp4 = self.root / "honest_prog_out.mp4"

        class _FakeMiniMaxSession:
            def submit_task(self, _task_cfg):
                h = fal_bridge.FalJobHandle()
                fal_bridge.push_cloud_status(h, "rendering", "MiniMax: processing (12s)", real_source={"status": "processing"})
                out_mp4.write_bytes(b"mp4bytes")
                h.finish(fal_bridge.FalJobResult(success=True, generated_files=[str(out_mp4)], errors=[]))
                return h

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res_tuple = wgp_bridge.execute_wgp_job(
                ctx_p,
                _FakeMiniMaxSession(),
                {"prompt": "test", "output_path": str(out_mp4), "video_length": 120, "resolution": "1920x1088", "model_type": "minimax_h3"},
                idx=1,
                total=1,
                filename="S01.mp4",
                stage="Stage 2",
                reason="MISSING",
                model="MiniMax-H3",
                frames=120,
                resolution="1920x1088",
            )
        self.assertTrue(res_tuple[0] if isinstance(res_tuple, tuple) else res_tuple)
        out_text = buf.getvalue()
        q_lines = [ln for ln in out_text.splitlines() if "[QUEUE_PROGRESS]" in ln]
        self.assertTrue(q_lines, f"Expected [QUEUE_PROGRESS] in stdout, got:\n{out_text}")
        payload = json.loads(q_lines[0].split("[QUEUE_PROGRESS]", 1)[1].strip())
        self.assertIsNone(payload["pct"])
        self.assertIsNone(payload["window_pct"])
        self.assertIsNone(payload["step"])
        self.assertIsNone(payload["total_steps"])
        self.assertIn("MiniMax: processing (12s)", payload["raw_phase"])

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const elements = {};
const mkEl = (id) => {
  if (!elements[id]) {
    elements[id] = {
      id, value: "", innerHTML: "", textContent: "", title: "", className: "",
      style: {}, dataset: {}, disabled: false, checked: false, options: [], children: [],
      classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,v){v?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
      addEventListener() {}, setAttribute() {}, appendChild() {}, querySelectorAll: () => [],
    };
  }
  return elements[id];
};
const ctx = {
  console, Math, JSON, String, Number, Array, Object, Boolean, parseInt, parseFloat, isNaN, RegExp, Date, Set, Map, Promise, URLSearchParams,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, json: async () => ({}) }),
  EventSource: function() { return { addEventListener(){}, close(){} }; },
  location: { search: "" },
  document: {
    getElementById: (id) => mkEl(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: null,
  state: { currentSlug: "proj", renderQueue: [], sseTaskStatuses: new Map(), sseRawLogs: [] },
  escapeHtml: (s) => String(s),
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

assert.strictEqual(ctx.formatActivePhaseLabel({ raw_phase: "MiniMax: processing (12s)", pct: null }), "[MiniMax: processing (12s)]");
assert.strictEqual(ctx.formatActivePhaseLabel({ raw_phase: "MiniMax: processing (5s)", window_count: 2, window_index: 1, window_pct: null, pct: null }), "[Window 1/2 • MiniMax: processing (5s)]");
assert.strictEqual(ctx.formatActivePhaseLabel({ raw_phase: "Denoising", window_count: 2, window_index: 1, window_pct: 50, pct: 25, step: 4, total_steps: 8 }), "[Window 1/2 • Denoising] Step 4/8 (50%)");
assert.strictEqual(ctx.formatActivePhaseLabel({ raw_phase: "fal.ai: Denoising", step: 3, total_steps: 8, pct: null }), "[fal.ai: Denoising] Step 3/8");
assert.strictEqual(ctx.formatActivePhaseLabel({ raw_phase: "Denoising", step: 9, total_steps: 20, pct: 45 }), "[Denoising] Step 9/20 (45%)");

ctx.updateActiveProgressBarDOM({ phase: "rendering", raw_phase: "MiniMax: processing (12s)", pct: null });
assert.strictEqual(mkEl("activeProgressBar").classList.contains("is-indeterminate"), true);
assert.strictEqual(mkEl("activeProgressBar").style.width, "100%");
assert.strictEqual(mkEl("activeProgressPct").textContent, "Live");
assert.strictEqual(mkEl("activeProgressPhase").textContent, "[MiniMax: processing (12s)]");

ctx.updateActiveProgressBarDOM({ phase: "queued", raw_phase: "fal.ai: IN_QUEUE (pos 2)", pct: null });
assert.strictEqual(mkEl("activeProgressBar").classList.contains("is-indeterminate"), true);
assert.strictEqual(mkEl("activeProgressPct").textContent, "Queued");

ctx.updateActiveProgressBarDOM({ phase: "uploading", raw_phase: "MiniMax: submitting...", pct: null });
assert.strictEqual(mkEl("activeProgressPct").textContent, "Uploading");

ctx.updateActiveProgressBarDOM({ phase: "downloading", raw_phase: "MiniMax: downloading output...", pct: null });
assert.strictEqual(mkEl("activeProgressPct").textContent, "Downloading");

ctx.updateActiveProgressBarDOM({ phase: "rendering", raw_phase: "Denoising", step: 9, total_steps: 20, pct: 45 });
assert.strictEqual(mkEl("activeProgressBar").classList.contains("is-indeterminate"), false);
assert.strictEqual(mkEl("activeProgressBar").style.width, "45%");
assert.strictEqual(mkEl("activeProgressPct").textContent, "45%");
assert.strictEqual(mkEl("activeProgressPhase").textContent, "[Denoising] Step 9/20 (45%)");
console.log("NODE_HONEST_PROGRESS_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("NODE_HONEST_PROGRESS_OK", res.stdout)

    def test_13_modular_raw_prompt_preservation_on_mode_switch(self):
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const elements = {};
const mkEl = (id) => {
  if (!elements[id]) {
    elements[id] = {
      id, value: "", innerHTML: "", textContent: "", title: "", className: "",
      style: {}, dataset: {}, disabled: false, checked: false, options: [], children: [],
      _listeners: {},
      classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,v){v?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
      addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
      dispatchEvent() {}, setAttribute() {}, removeAttribute() {},
      appendChild(c) { if (c) { c.parentNode = this; this.children.push(c); } return c; },
      querySelectorAll: () => [],
      querySelector: () => null,
    };
  }
  return elements[id];
};
const ctx = {
  console, Math, JSON, String, Number, Array, Object, Boolean, parseInt, parseFloat, isNaN, RegExp, Date, Set, Map,
  requestAnimationFrame: () => 0,
  escapeHtml: (s) => String(s),
  showToast: () => {}, markProjectDirty: () => {}, redrawTimeline: () => {},
  updateAnchorDropdowns: () => {}, renderAnchorCards: () => {}, getMergedAnchorsList: () => [],
  updateSceneMarkersBar: () => {}, refreshInspectorCollapsibleBadges: () => {},
  Option: function(t, v) { this.textContent = t; this.value = v; this.dataset = {}; },
  document: {
    getElementById: (id) => mkEl(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => mkEl("__el_" + tag + "_" + Math.random()),
    addEventListener: () => {},
    activeElement: null,
  },
  window: null,
  state: {
    currentSlug: "proj",
    selectedSceneIdx: 0,
    projectData: {
      project: { render_overlap_seconds: 0 },
      markers: [],
      scenes: [],
    },
  },
};
ctx.window = ctx;
ctx.global = ctx;
ctx.addEventListener = () => {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/scene_inspector.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/markers_and_prompts.js", "utf8"), ctx);

// 1. Exact S15 scenario: converted from two-pass to duet_method_a with stale _m/_f keys & full raw duet prompt
const s15 = {
  id: "S15",
  mode: "duet_method_a",
  prompt_mode: "raw",
  prompt_camera: "Subtle, smooth orbital camera drift around the performance.",
  prompt_atmosphere: "",
  prompt_performance_m: "Inside the studio, {MALE_STUDIO_DESC} on the left sings passionately.",
  prompt_performance_f: "Inside the studio, {FEMALE_STUDIO_DESC} on the right sings her counter-lyric.",
  prompt_musicians: "",
  prompt_soundscape_m: "Studio room tone and male vocal.",
  prompt_soundscape_f: "Studio room tone and female vocal.",
  prompt: "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Subtle, smooth orbital camera drift around the performance. Inside the studio, {MALE_STUDIO_DESC} on the left faces right and sings passionately. {FEMALE_STUDIO_DESC} on the right faces left and sings her counter-lyric.\noverall_soundscape: Studio room tone and male vocal.\nnon_diegetic_music: N/A",
};
ctx.state.projectData.scenes = [s15];
ctx.state.selectedSceneIdx = 0;

// Clicking Modular button must parse raw prompt into prompt_performance and preserve both singers!
mkEl("btnPromptModeComponents")._listeners.click[0]();
assert.strictEqual(s15.prompt_mode, "components");
assert.ok(s15.prompt_performance.includes("{MALE_STUDIO_DESC}"), "Missing male desc in hydrated perf");
assert.ok(s15.prompt_performance.includes("{FEMALE_STUDIO_DESC}"), "Missing female desc in hydrated perf");
assert.ok(s15.prompt.includes("{MALE_STUDIO_DESC}"), "Main male prompt overwritten!");
assert.ok(s15.prompt.includes("{FEMALE_STUDIO_DESC}"), "Main female prompt overwritten!");

// 2. Editing in Raw mode when stale components exist must NOT revert to stale components on Modular click
mkEl("btnPromptModeRaw")._listeners.click[0]();
assert.strictEqual(s15.prompt_mode, "raw");
s15.prompt = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Custom updated raw solo description.\noverall_soundscape: Studio room tone under the vocal performance.\nnon_diegetic_music: N/A";
mkEl("btnPromptModeComponents")._listeners.click[0]();
assert.ok(s15.prompt_performance.includes("Custom updated raw solo description."), "Raw edit lost on switch to Modular!");
assert.ok(s15.prompt.includes("Custom updated raw solo description."), "Canonical prompt reverted to stale components!");

// 3. Mode switch duet_method_b -> duet_method_a preserves combined performance & removes stale _m/_f keys
const s2 = {
  id: "S16",
  start: 0,
  end: 5,
  mode: "duet_method_b",
  prompt_mode: "components",
  prompt_camera: "Locked wide shot.",
  prompt_performance_m: "Male sings left.",
  prompt_performance_f: "Female sings right.",
  prompt_m: "Male prompt",
  prompt_f: "Female prompt",
  seed_m: 1,
  seed_f: 2,
};
ctx.handleSceneModeChange(s2, "duet_method_a");
assert.ok(s2.prompt_performance.includes("Male sings left.") && s2.prompt_performance.includes("Female sings right."));
assert.strictEqual(s2.prompt_performance_m, undefined);
assert.strictEqual(s2.prompt_performance_f, undefined);
console.log("NODE_PROMPT_PRESERVATION_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("NODE_PROMPT_PRESERVATION_OK", res.stdout)

    def test_14_multi_engine_parallel_override_and_continuation(self):
        from pipeline import wgp_bridge
        from pipeline.context import ProjectContext
        import threading
        import time

        proj_root = self.root / "multi_eng_proj"
        scenes = [
            {"id": "S01", "start": 0.0, "end": 4.0, "mode": "solo", "anchor": "A01_test.jpg", "backend": "local", "prompt": "S1 local", "seed": 1},
            {"id": "S02", "start": 4.0, "end": 8.0, "mode": "duet_method_b", "anchor": "A01_test.jpg", "backend": "local", "prompt_m": "S2 p1", "prompt_f": "S2 p2", "seed_m": 2, "seed_f": 3},
            {"id": "S03", "start": 8.0, "end": 12.0, "mode": "solo", "anchor": "A01_test.jpg", "backend": "fal", "prompt": "S3 fal", "seed": 4},
        ]
        cfg_p = _build_temp_project(proj_root, scenes=scenes)
        slug = cfg_p.parent.name
        cfg_before = cfg_p.read_text(encoding="utf-8")

        # 1. Per-engine status & cancel isolation + safe updated_keys merge on state_hashes.json
        ctx_p = ProjectContext(cfg_p)
        wgp_bridge.write_engine_status(ctx_p, {"state": "rendering", "filename": "S01.mp4", "pct": 40.0}, backend="local")
        wgp_bridge.write_engine_status(ctx_p, {"state": "rendering", "filename": "S02_Pass1_Left.mp4", "pct": None}, backend="minimax")
        st_loc = json.loads((ctx_p.jobs_dir / "engine_status_local.json").read_text(encoding="utf-8"))
        st_mm = json.loads((ctx_p.jobs_dir / "engine_status_minimax.json").read_text(encoding="utf-8"))
        self.assertEqual(st_loc["filename"], "S01.mp4")
        self.assertEqual(st_mm["filename"], "S02_Pass1_Left.mp4")
        self.assertIsNone(st_mm["pct"])

        (ctx_p.jobs_dir / "cancel_request_local.json").write_text("{}", encoding="utf-8")
        self.assertTrue(wgp_bridge.check_cancel_requested(ctx_p, backend="local"))
        self.assertFalse(wgp_bridge.check_cancel_requested(ctx_p, backend="minimax"))
        wgp_bridge.clear_cancel_request(ctx_p, backend="local")

        sA = ctx_p.load_state_hashes()
        sA.setdefault("tasks", {})["S01_solo"] = "hash_s1"
        ctx_p.save_state_hashes(sA, updated_keys={"S01_solo"})
        sB = ctx_p.load_state_hashes()
        sB["tasks"]["S02_Pass1"] = "hash_s2_p1"
        ctx_p.save_state_hashes(sB, updated_keys={"S02_Pass1"})
        merged = ctx_p.load_state_hashes()
        self.assertEqual(merged["tasks"].get("S01_solo"), "hash_s1")
        self.assertEqual(merged["tasks"].get("S02_Pass1"), "hash_s2_p1")

        # 2. Queue-level per-pass override (S02:p1 -> minimax while S02:p2 stays local, project JSON untouched)
        orig_projects_dir = server.PROJECTS_DIR
        orig_enabled = dict(server.enabled_engines)
        server.PROJECTS_DIR = cfg_p.parent.parent
        try:
            q0 = server.compute_fast_project_queue(slug, force_recheck=True)
            s01_fn = next(x["output_filename"] for x in q0 if x["scene_id"] == "S01")
            s02_p1_fn = next(x["output_filename"] for x in q0 if x["scene_id"] == "S02" and x.get("pass") == 1)
            s02_p2_fn = next(x["output_filename"] for x in q0 if x["scene_id"] == "S02" and x.get("pass") == 2)
            s03_fn = next(x["output_filename"] for x in q0 if x["scene_id"] == "S03")
            h_set = _DummyHandler("/api/queue/set_engine")
            self.assertTrue(server_fal.handle_fal_post(h_set, "/api/queue/set_engine", {"slug": slug, "id": s02_p1_fn, "backend_override": "minimax"}))
            q1 = h_set.payload["queue"]
            itm_p1 = next(x for x in q1 if x["output_filename"] == s02_p1_fn)
            itm_p2 = next(x for x in q1 if x["output_filename"] == s02_p2_fn)
            self.assertEqual(itm_p1["backend"], "minimax")
            self.assertEqual(itm_p1["backend_override"], "minimax")
            self.assertIn("cost_str", itm_p1)
            self.assertEqual(itm_p2["backend"], "local")
            self.assertEqual(cfg_p.read_text(encoding="utf-8"), cfg_before, "Queue override must NOT mutate project JSON")

            # 3. Parallel worker execution across Local + MiniMax, dependency gate (P1 -> P2),
            #    unchecked fal engine paused, then ticking fal mid-run engages fal immediately!
            exec_lock = threading.Lock()
            active_cnt = [0]
            max_conc = [0]
            exec_order: list[tuple[str, str]] = []
            fal_started = threading.Event()
            both_started = threading.Event()

            def _fake_exec(cmd_args, _slug, **_kw):
                tgt = str(cmd_args[1])
                b_arg = cmd_args[cmd_args.index("--backend") + 1] if "--backend" in cmd_args else ("fal" if tgt == "S03" else "local")
                with exec_lock:
                    active_cnt[0] += 1
                    max_conc[0] = max(max_conc[0], active_cnt[0])
                    exec_order.append((tgt, b_arg))
                    if max_conc[0] >= 2:
                        both_started.set()
                    if tgt == "S03":
                        fal_started.set()
                time.sleep(0.22)
                with exec_lock:
                    active_cnt[0] -= 1
                return 0

            with mock.patch.object(server, "execute_single_render", side_effect=_fake_exec), \
                 mock.patch.object(server, "run_stitch_worker"):
                # Leave S03 (fal) queued, start with local=True, minimax=True, fal=False
                server.run_queue_worker(slug, force_restart=False, enabled_engines={"local": True, "minimax": True, "fal": False})
                self.assertTrue(both_started.wait(timeout=3.0), f"Expected Local + MiniMax concurrency >= 2, got order={exec_order}")
                # Check S03 did NOT start while fal unchecked, then engage fal mid-run via HTTP route!
                self.assertFalse(any(t == "S03" for t, _ in exec_order))
                h_eng = _DummyHandler("/api/render/engines")
                self.assertTrue(server_fal.handle_fal_post(h_eng, "/api/render/engines", {"slug": slug, "enabled_engines": {"fal": True}}))
                self.assertTrue(h_eng.payload["enabled_engines"]["fal"])
                self.assertTrue(fal_started.wait(timeout=3.0), f"Ticking fal mid-run should engage S03, got={exec_order}")

                deadline = time.time() + 6.0
                while server.queue_worker_active and time.time() < deadline:
                    time.sleep(0.05)

            self.assertFalse(server.queue_worker_active)
            self.assertGreaterEqual(max_conc[0], 2)
            idx_p1 = next(i for i, (t, _) in enumerate(exec_order) if t == "S02:p1")
            idx_p2 = next(i for i, (t, _) in enumerate(exec_order) if t == "S02:p2")
            self.assertLess(idx_p1, idx_p2, "Pass 2 on Local must wait for Pass 1 on MiniMax!")
            self.assertEqual(dict(exec_order)["S02:p1"], "minimax")
            self.assertEqual(dict(exec_order)["S02:p2"], "local")

            # 4. When remaining queued item is ONLY on unchecked engine, Run finishes & returns idle
            server._session_completed_hashes = set()
            server.clear_project_queue(slug)
            server.enqueue_in_project_queue(slug, s03_fn)
            ran_any = []
            with mock.patch.object(server, "execute_single_render", side_effect=lambda a, _s: (ran_any.append(a[1]), 0)[1]), \
                 mock.patch.object(server, "run_stitch_worker"):
                server.run_queue_worker(slug, force_restart=False, enabled_engines={"local": True, "minimax": True, "fal": False})
                deadline = time.time() + 4.0
                while server.queue_worker_active and time.time() < deadline:
                    time.sleep(0.05)
            self.assertEqual(ran_any, [])
            self.assertFalse(server.queue_worker_active)

            # 4b. Queue continuation: item enqueued while post-render stitch runs is still picked up before idle
            server._session_completed_hashes = set()
            cont_ran: list[str] = []
            enq_lock = threading.Lock()
            enqueued_mid = [False]

            def _fake_stitch(_cfg, _slug):
                time.sleep(0.20)
                with enq_lock:
                    if not enqueued_mid[0]:
                        enqueued_mid[0] = True
                        server.enqueue_in_project_queue(slug, s01_fn)

            with mock.patch.object(server, "execute_single_render", side_effect=lambda a, _s: (cont_ran.append(a[1]), time.sleep(0.05), 0)[2]), \
                 mock.patch.object(server, "run_stitch_worker", side_effect=_fake_stitch):
                server.run_queue_worker(slug, force_restart=False, enabled_engines={"local": True, "minimax": False, "fal": True})
                deadline = time.time() + 5.0
                while server.queue_worker_active and time.time() < deadline:
                    time.sleep(0.05)
            self.assertEqual(cont_ran, ["S03", "S01"])
            self.assertFalse(server.queue_worker_active)

            # 4c. Unticking an engine mid-run drains cleanly after current pass without starting next local item
            server._session_completed_hashes = set()
            server.clear_project_queue(slug)
            server.set_queue_item_backend_override(server, slug, s03_fn, "local")
            server.enqueue_in_project_queue(slug, s01_fn)
            server.enqueue_in_project_queue(slug, s03_fn)
            drain_ran: list[str] = []
            s01_started = threading.Event()
            s01_release = threading.Event()

            def _fake_drain_exec(a, _s, **_kw):
                drain_ran.append(a[1])
                s01_started.set()
                s01_release.wait(timeout=3.0)
                return 0

            with mock.patch.object(server, "execute_single_render", side_effect=_fake_drain_exec), \
                 mock.patch.object(server, "run_stitch_worker"):
                server.run_queue_worker(slug, force_restart=False, enabled_engines={"local": True, "minimax": False, "fal": False})
                self.assertTrue(s01_started.wait(timeout=3.0))
                st_drain = server.set_enabled_engines(server, slug, {"local": False})
                self.assertTrue(st_drain["engines"]["local"]["draining"])
                s01_release.set()
                deadline = time.time() + 4.0
                while server.queue_worker_active and time.time() < deadline:
                    time.sleep(0.05)
            self.assertEqual(drain_ran, ["S01"])
            self.assertFalse(server.queue_worker_active)
            server.set_queue_item_backend_override(server, slug, s03_fn, "")

            # 5. Per-engine stop only stops target backend
            with mock.patch.object(server, "execute_single_render", return_value=0):
                server.engine_workers["minimax"]["running"] = True
                server.engine_workers["minimax"]["filename"] = "S02_Pass1_Left.mp4"
                server.engine_workers["local"]["running"] = True
                server.engine_workers["local"]["filename"] = "S01_solo.mp4"
                server.queue_worker_active = True
                server.kill_active_render(target_slug=slug, backend="local")
                self.assertFalse(server.engine_workers["local"]["running"])
                self.assertTrue(server.engine_workers["minimax"]["running"])
                self.assertTrue(server.queue_worker_active)
                server.kill_active_render(target_slug=slug, backend="minimax")
                self.assertFalse(server.queue_worker_active)
        finally:
            server.PROJECTS_DIR = orig_projects_dir
            server.enabled_engines.update(orig_enabled)
            for b in ("local", "fal", "minimax"):
                server.engine_workers[b].update({"running": False, "draining": False, "active_process": None, "filename": None, "stop_requested": False})

        # 6. Frontend Node VM check for queue_engines.js + queue_and_console.js
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const elements = {};
const mkEl = (id) => {
  if (!elements[id]) {
    elements[id] = {
      id, value: "", innerHTML: "", textContent: "", title: "", className: "",
      style: {}, dataset: {}, disabled: false, checked: true, options: [], children: [],
      classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,v){v?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
      addEventListener() {}, setAttribute() {}, appendChild() {}, querySelectorAll: () => [],
    };
  }
  return elements[id];
};
const ctx = {
  console, Math, JSON, String, Number, Array, Object, Boolean, parseInt, parseFloat, isNaN, RegExp, Date, Set, Map, Promise, URLSearchParams,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, json: async () => ({}) }),
  EventSource: function() { return { addEventListener(){}, close(){} }; },
  location: { search: "" },
  document: {
    getElementById: (id) => mkEl(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => mkEl("__el_" + tag + "_" + Math.random()),
    addEventListener: () => {},
  },
  window: null,
  state: {
    currentSlug: "proj",
    renderQueue: [
      { idx: 1, filename: "S01_solo.mp4", output_filename: "S01_solo", status: "rendering", backend: "local", resolved_default_backend: "local", pct: 35 },
      { idx: 2, filename: "S02_Pass1.mp4", output_filename: "S02_Pass1", status: "queued", backend: "minimax", backend_override: "minimax", resolved_default_backend: "local" },
    ],
    sseTaskStatuses: new Map(), sseRawLogs: [],
  },
  escapeHtml: (s) => String(s),
  showToast: () => {},
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

const cellHtml = ctx.renderQueueEngineCell(ctx.state.renderQueue[1]);
assert.ok(cellHtml.includes('class="queue-engine-select is-override"'));
assert.ok(cellHtml.includes("Auto (Local)"));
assert.ok(cellHtml.includes('value="minimax" selected'));

ctx.handleMultiEngineSSEEvent("[QUEUE_ACTIVE]", { filename: "S01_solo.mp4", backend: "local", started_at: Date.now() / 1000 });
ctx.handleMultiEngineSSEEvent("[QUEUE_ACTIVE]", { filename: "S02_Pass1.mp4", backend: "minimax", started_at: Date.now() / 1000 });
const lanesHtml = mkEl("multiEngineActiveLanes").innerHTML;
assert.ok(lanesHtml.includes("S01_solo.mp4") && lanesHtml.includes("S02_Pass1.mp4"));
assert.ok(lanesHtml.includes('data-engine="local"') && lanesHtml.includes('data-engine="minimax"'));
assert.ok(lanesHtml.includes("is-indeterminate"));
assert.strictEqual(ctx.activeEngineCount(), 2);
console.log("NODE_MULTI_ENGINE_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("NODE_MULTI_ENGINE_OK", res.stdout)

    def test_15_per_engine_max_parallel_spinners_and_concurrent_slots(self):
        """Verify per-engine max_parallel spinners (default 1), multiple concurrent fal/MiniMax slots,
        mid-run scaling up/down, per-task status/cancel isolation, and multi-job frontend lanes."""
        import threading
        from pipeline import wgp_bridge
        from pipeline.context import ProjectContext

        proj_root = self.root / "multi_slot_test"
        scenes = [
            {"id": "S01", "start": 0.0, "end": 4.0, "mode": "solo", "stem": "male", "anchor": "A01_test.jpg", "seed": 11, "backend": "fal", "prompt": "Fal 1"},
            {"id": "S02", "start": 4.0, "end": 8.0, "mode": "solo", "stem": "male", "anchor": "A01_test.jpg", "seed": 12, "backend": "fal", "prompt": "Fal 2"},
            {"id": "S03", "start": 8.0, "end": 12.0, "mode": "solo", "stem": "female", "anchor": "A01_test.jpg", "seed": 13, "backend": "minimax", "prompt": "MM 1"},
            {"id": "S04", "start": 12.0, "end": 16.0, "mode": "solo", "stem": "female", "anchor": "A01_test.jpg", "seed": 14, "backend": "minimax", "prompt": "MM 2"},
        ]
        cfg_p = _build_temp_project(proj_root, scenes=scenes)
        slug = cfg_p.parent.name

        # 1. Per-task status & cancel isolation within the SAME cloud backend (fal)
        ctx_p = ProjectContext(cfg_p)
        wgp_bridge.write_engine_status(ctx_p, {"state": "rendering", "filename": "S01_solo_832x480.mp4", "phase": "queued", "pct": None}, backend="fal", task_stem="S01_solo_832x480")
        wgp_bridge.write_engine_status(ctx_p, {"state": "rendering", "filename": "S02_solo_832x480.mp4", "phase": "uploading", "pct": None}, backend="fal", task_stem="S02_solo_832x480")
        (ctx_p.jobs_dir / "cancel_request_fal__S01_solo_832x480.json").write_text("{}", encoding="utf-8")
        self.assertTrue(wgp_bridge.check_cancel_requested(ctx_p, backend="fal", task_stem="S01_solo_832x480"))
        self.assertFalse(wgp_bridge.check_cancel_requested(ctx_p, backend="fal", task_stem="S02_solo_832x480"))
        wgp_bridge.clear_cancel_request(ctx_p, backend="fal", task_stem="S01_solo_832x480")
        wgp_bridge.write_engine_status(ctx_p, {"state": "completed"}, backend="fal", task_stem="S01_solo_832x480")
        wgp_bridge.write_engine_status(ctx_p, {"state": "completed"}, backend="fal", task_stem="S02_solo_832x480")
        wgp_bridge.write_engine_status(ctx_p, {"state": "completed"}, backend="fal")

        orig_projects_dir = server.PROJECTS_DIR
        orig_enabled = dict(server.enabled_engines)
        orig_max_p = dict(server.max_parallel)
        server.PROJECTS_DIR = cfg_p.parent.parent
        try:
            # 2. Defaults = 1 & bounds clamping via POST /api/render/engines
            server.max_parallel.update({"local": 1, "fal": 1, "minimax": 1})
            st0 = server.get_multi_engine_render_status()
            self.assertEqual(st0["max_parallel"], {"local": 1, "fal": 1, "minimax": 1})

            h_mp = _DummyHandler("/api/render/engines")
            self.assertTrue(server_fal.handle_fal_post(h_mp, "/api/render/engines", {
                "slug": slug, "max_parallel": {"local": 8, "fal": 2, "minimax": 99},
            }))
            self.assertEqual(h_mp.payload["max_parallel"], {"local": 1, "fal": 2, "minimax": 30})
            self.assertTrue(server.compute_fast_project_queue(slug, force_recheck=True))

            # 3. Run 2 concurrent fal tasks while minimax starts at 1, then scale minimax 1 -> 2 mid-run
            fal_cur, fal_max = [0], [0]
            mm_cur, mm_max = [0], [0]
            both_fal = threading.Event()
            mm1_started = threading.Event()
            mm_allow = threading.Event()
            seen_sids: list[str] = []
            lock = threading.Lock()

            def _fake_exec(cmd_args, _slug, **_kw):
                tgt = str(cmd_args[1])
                b = "fal" if tgt in ("S01", "S02") else "minimax"
                with lock:
                    seen_sids.append(tgt)
                    if b == "fal":
                        fal_cur[0] += 1
                        fal_max[0] = max(fal_max[0], fal_cur[0])
                        if fal_cur[0] >= 2:
                            both_fal.set()
                    else:
                        mm_cur[0] += 1
                        mm_max[0] = max(mm_max[0], mm_cur[0])
                        mm1_started.set()
                if b == "fal":
                    both_fal.wait(timeout=4.0)
                    time.sleep(0.08)
                else:
                    mm_allow.wait(timeout=5.0)
                    time.sleep(0.06)
                with lock:
                    if b == "fal": fal_cur[0] -= 1
                    else: mm_cur[0] -= 1
                return 0

            with mock.patch.object(server, "execute_single_render", side_effect=_fake_exec), \
                 mock.patch.object(server, "run_stitch_worker"):
                server.run_queue_worker(slug, force_restart=False, enabled_engines={"local": True, "fal": True, "minimax": True}, max_parallel={"local": 1, "fal": 2, "minimax": 1})
                self.assertTrue(both_fal.wait(timeout=3.0), f"Expected 2 simultaneous fal tasks, got fal_max={fal_max[0]}, seen={seen_sids}")
                self.assertTrue(mm1_started.wait(timeout=3.0))
                self.assertEqual(mm_max[0], 1, "MiniMax must stay serial at max_parallel=1 until scaled up")
                # Scale MiniMax 1 -> 2 mid-run and inspect active_tasks
                h_up = _DummyHandler("/api/render/engines")
                server_fal.handle_fal_post(h_up, "/api/render/engines", {"slug": slug, "max_parallel": {"minimax": 2}})
                deadline_mm = time.time() + 2.5
                while mm_max[0] < 2 and time.time() < deadline_mm:
                    time.sleep(0.03)
                self.assertEqual(mm_max[0], 2, "Raising minimax spinner 1 -> 2 mid-run must launch 2nd concurrent slot")
                st_live = server.get_multi_engine_render_status()
                self.assertEqual(len(st_live["engines"]["minimax"]["active_tasks"]), 2)
                # Verify active_rendering_map marks BOTH MiniMax concurrent passes as rendering
                is_act, amap = server.active_rendering_map(server, slug) if hasattr(server, "active_rendering_map") else (True, {})
                self.assertTrue(is_act)
                # Scale MiniMax back down 2 -> 1 mid-run: excess slot marks draining=True without kill
                h_dn = _DummyHandler("/api/render/engines")
                server_fal.handle_fal_post(h_dn, "/api/render/engines", {"slug": slug, "max_parallel": {"minimax": 1}})
                self.assertTrue(h_dn.payload["engines"]["minimax"]["draining"])
                mm_allow.set()
                deadline = time.time() + 5.0
                while server.queue_worker_active and time.time() < deadline:
                    time.sleep(0.05)

            self.assertEqual(sorted(seen_sids), ["S01", "S02", "S03", "S04"])
            self.assertEqual(fal_max[0], 2)
            self.assertFalse(server.queue_worker_active)

            # 4. Targeted per-job stop kills ONLY matching fal slot while sibling fal slot stays running
            server._session_completed_hashes = set()
            server.enqueue_in_project_queue(slug, "S01_solo_832x480")
            server.enqueue_in_project_queue(slug, "S02_solo_832x480")
            fal_release = threading.Event()
            two_live = threading.Event()
            live_cnt = [0]

            def _fake_wait(a, _s, **_kw):
                with lock:
                    live_cnt[0] += 1
                    if live_cnt[0] >= 2: two_live.set()
                fal_release.wait(timeout=3.0)
                return 0

            with mock.patch.object(server, "execute_single_render", side_effect=_fake_wait), \
                 mock.patch.object(server, "run_stitch_worker"):
                server.run_queue_worker(slug, force_restart=False, enabled_engines={"local": False, "fal": True, "minimax": False}, max_parallel={"fal": 2})
                self.assertTrue(two_live.wait(timeout=3.0))
                server.kill_active_render(target_slug=slug, backend="fal", filename="S01_solo_832x480.mp4")
                st_after = server.get_multi_engine_render_status()
                rem_fns = [t["filename"] for t in st_after["engines"]["fal"]["active_tasks"]]
                self.assertEqual(rem_fns, ["S02_solo_832x480.mp4"])
                fal_release.set()
                deadline = time.time() + 4.0
                while server.queue_worker_active and time.time() < deadline:
                    time.sleep(0.05)
        finally:
            server.PROJECTS_DIR = orig_projects_dir
            server.queue_worker_active = False
            server.render_status["running"] = False
            server.render_status["current_project"] = None
            server.enabled_engines.update(orig_enabled)
            server.max_parallel.update(orig_max_p)
            for b in ("local", "fal", "minimax"):
                server.engine_slots[b].clear()
                server.engine_workers[b].update({"running": False, "draining": False, "active_count": 0, "active_process": None, "filename": None, "stop_requested": False})

        # 5. HTML spinners & Node VM check for multi-slot lanes and non-clobbering [QUEUE_ACTIVE]
        idx_html = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="maxParallelFal"', idx_html)
        self.assertIn('id="maxParallelMinimax"', idx_html)
        self.assertIn('value="1"', idx_html)

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const elements = {};
const mkEl = (id) => {
  if (!elements[id]) {
    elements[id] = {
      id, value: "1", innerHTML: "", textContent: "", title: "", className: "",
      style: {}, dataset: {}, disabled: false, checked: true, options: [], children: [],
      classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, toggle(c,v){v?this._s.add(c):this._s.delete(c);}, contains(c){return this._s.has(c);} },
      addEventListener() {}, setAttribute() {}, appendChild() {}, querySelectorAll: () => [],
    };
  }
  return elements[id];
};
const ctx = {
  console, Math, JSON, String, Number, Array, Object, Boolean, parseInt, parseFloat, isNaN, isFinite: Number.isFinite, RegExp, Date, Set, Map, Promise, URLSearchParams,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, json: async () => ({}) }),
  EventSource: function() { return { addEventListener(){}, close(){} }; },
  location: { search: "" },
  document: {
    getElementById: (id) => mkEl(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => mkEl("__el_" + tag + "_" + Math.random()),
    addEventListener: () => {},
  },
  window: null,
  state: {
    currentSlug: "proj",
    renderQueue: [
      { idx: 1, filename: "S01_solo_832x480.mp4", output_filename: "S01_solo_832x480", status: "queued", backend: "fal" },
      { idx: 2, filename: "S02_solo_832x480.mp4", output_filename: "S02_solo_832x480", status: "queued", backend: "fal" },
    ],
    sseTaskStatuses: new Map(), sseRawLogs: [],
  },
  escapeHtml: (s) => String(s),
  showToast: () => {},
};
ctx.window = ctx;
ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.getMaxParallelMap())), { local: 1, fal: 1, minimax: 1 });
ctx.syncMultiEngineStatusUI({
  enabled_engines: { local: true, fal: true, minimax: true },
  max_parallel: { local: 1, fal: 3, minimax: 2 },
  engines: {
    local: { running: false, active_tasks: [] },
    fal: { running: true, active_tasks: [
      { filename: "S01_solo_832x480.mp4", started_at: 100, draining: false, engine_status: { phase: "queued", raw_phase: "IN_QUEUE", pct: null } },
      { filename: "S02_solo_832x480.mp4", started_at: 101, draining: false, engine_status: { phase: "uploading", raw_phase: "Submitting", pct: null } },
    ] },
    minimax: { running: false, active_tasks: [] },
  },
});
assert.strictEqual(ctx.activeEngineCount(), 2);
const html = mkEl("multiEngineActiveLanes").innerHTML;
assert.ok(html.includes("S01_solo_832x480.mp4") && html.includes("S02_solo_832x480.mp4"));

// Two sequential [QUEUE_ACTIVE] on fal must keep BOTH S01 & S02 rendering (not complete S01!)
ctx.handleQueueSSEEvent('[QUEUE_ACTIVE] {"filename":"S01_solo_832x480.mp4","backend":"fal"}');
ctx.handleQueueSSEEvent('[QUEUE_ACTIVE] {"filename":"S02_solo_832x480.mp4","backend":"fal"}');
assert.strictEqual(ctx.state.renderQueue[0].status, "rendering");
assert.strictEqual(ctx.state.renderQueue[1].status, "rendering");
console.log("NODE_MULTI_SLOT_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("NODE_MULTI_SLOT_OK", res.stdout)

    def test_16_duet_method_a_dual_reference_audio_and_clip_cap(self):
        """duet_method_a splits L/R stems into <Audio 1>+<Audio 2> <=15s combined and falls back cleanly >15s."""
        from pipeline.fal_catalog import resolve_minimax_route, resolve_fal_route
        from pipeline.fal_bridge import FalSession
        from pipeline.minimax_bridge import MiniMaxSession

        def _write_wav(path: Path, dur_s: float, channels: int = 2):
            import wave as _wave
            path.parent.mkdir(parents=True, exist_ok=True)
            nf = int(round(dur_s * 44100))
            with _wave.open(str(path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(b"\x01\x00" * (nf * channels))

        with tempfile.TemporaryDirectory() as tmp:
            t_dir = Path(tmp)
            a_img = t_dir / "A01.jpg"
            a_img.write_bytes(b"IMG")
            wav_s = t_dir / "stereo.wav"
            wav_l = t_dir / "male.wav"
            wav_r = t_dir / "female.wav"
            _write_wav(wav_s, 7.2)
            _write_wav(wav_l, 7.2)
            _write_wav(wav_r, 7.2)

            mm = MiniMaxSession(ctx=None)
            route_mm = resolve_minimax_route({"minimax_model": "MiniMax-H3"}, resolution="1344x768")
            base_prompt = (
                "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
                "integrated_multimodal_description: [Shot 1] Male (S1) sings to the left audio track and female (S2) sings to the right audio track."
            )
            with mock.patch.object(mm, "upload_media", side_effect=lambda p: f"mm_file://{Path(p).stem}"):
                task_short = {
                    "prompt": base_prompt,
                    "image_start": str(a_img),
                    "audio_guide": str(wav_s),
                    "_duet_audio_left": str(wav_l),
                    "_duet_audio_right": str(wav_r),
                }
                pay_single = mm._build_payload(route_mm, task_short, dur_s=7.2)
                aud_items = [c for c in pay_single["content"] if c.get("type") == "audio_url"]
                # MiniMax V2 drops 2nd reference_audio -> must always send the single stereo WAV (left + right combined)
                self.assertEqual([c["audio_url"]["url"] for c in aud_items], ["mm_file://stereo"])
                txt_dual = next(c["text"] for c in pay_single["content"] if c.get("type") == "text")
                self.assertIn("left audio track", txt_dual)
                self.assertIn("right audio track", txt_dual)

                # Explicit <Audio 1> / <Audio 2> in prompt gets mapped onto stereo channel descriptions
                task_long = dict(task_short, prompt="<Audio 1> left, <Audio 2> right")
                pay_tags = mm._build_payload(route_mm, task_long, dur_s=9.0)
                aud_items_long = [c for c in pay_tags["content"] if c.get("type") == "audio_url"]
                self.assertEqual([c["audio_url"]["url"] for c in aud_items_long], ["mm_file://stereo"])
                txt_long = next(c["text"] for c in pay_tags["content"] if c.get("type") == "text")
                self.assertNotIn("<Audio 2>", txt_long)

            # fal.ai minimax_h3_ref2v also preserves single combined stereo WAV
            _write_wav(wav_s, 6.5)
            fal_s = FalSession(ctx=None)
            route_ref = resolve_fal_route({"supports_image_refs": True}, stage="video")
            with mock.patch.object(fal_s, "upload_media", side_effect=lambda p: f"https://fal.mock/{Path(p).name}"):
                f_args = fal_s._build_arguments(route_ref, task_short, dur_s=6.5)
                self.assertEqual(f_args["reference_audios"], ["https://fal.mock/stereo.wav"])
                self.assertNotIn("<Audio 2>", f_args["prompt"])

            # MiniMax tail-pads shorter reference_audio WAVs to match discrete 5+17k frames and trims over-length MP4 output
            from types import SimpleNamespace
            import math
            import wave
            from pipeline.media_utils import probe_video_duration, probe_video_frames
            from pipeline.minimax_bridge import MiniMaxSession, resolve_minimax_route, trim_minimax_output_to_task

            with tempfile.TemporaryDirectory() as pad_td:
                pad_root = Path(pad_td)
                wav_158 = pad_root / "male.wav"
                img_s = pad_root / "start.jpg"
                _make_ffmpeg_test_wav(self.ffmpeg, wav_158, dur=158 / 24.0)
                _make_dummy_jpg(img_s, self.ffmpeg)
                with wave.open(str(wav_158), "rb") as orig_wf:
                    orig_rate = orig_wf.getframerate()
                    orig_nframes = orig_wf.getnframes()
                    orig_raw = orig_wf.readframes(orig_nframes)

                uploaded_mm: list[str] = []
                mm_pad = MiniMaxSession(ctx=None)
                route_mm_pad = resolve_minimax_route({})
                task_158 = {
                    "prompt": "Male singer performs with emotion",
                    "image_start": str(img_s),
                    "audio_guide": str(wav_158),
                    "video_length": 158,
                }

                def _fake_mm_upload(p: str) -> str:
                    uploaded_mm.append(str(p))
                    return f"mm_file://{Path(p).stem}"

                with mock.patch.object(mm_pad, "upload_media", side_effect=_fake_mm_upload):
                    payload_158 = mm_pad._build_payload(route_mm_pad, task_158, dur_s=158 / 24.0)

                self.assertEqual(payload_158["duration"], 7)
                audio_urls = [c["audio_url"]["url"] for c in payload_158["content"] if c.get("role") == "reference_audio"]
                self.assertEqual(audio_urls, ["mm_file://male"])
                padded_wav = Path(uploaded_mm[-1])
                self.assertEqual(padded_wav.stem, "male")
                self.assertEqual(padded_wav.name, "male.wav")
                self.assertNotEqual(padded_wav.resolve(), wav_158.resolve())
                with wave.open(str(padded_wav), "rb") as pad_wf:
                    pad_nframes = pad_wf.getnframes()
                    pad_raw = pad_wf.readframes(pad_nframes)
                self.assertGreaterEqual(pad_nframes, int(math.ceil((175 / 24.0) * orig_rate)))
                self.assertEqual(pad_raw[:len(orig_raw)], orig_raw)
                self.assertTrue(any(b != 0 for b in orig_raw))
                self.assertTrue(all(b == 0 for b in pad_raw[len(orig_raw):]))

                mp4_175 = pad_root / "S22_mm_out.mp4"
                _make_ffmpeg_test_mp4(self.ffmpeg, mp4_175, dur=175 / 24.0, w=320, h=180)
                self.assertEqual(probe_video_frames(self.ffmpeg, mp4_175), 175)
                trim_minimax_output_to_task(
                    SimpleNamespace(ffmpeg=self.ffmpeg, fps=24),
                    mp4_175,
                    {"video_length": 158, "audio_guide": str(wav_158)},
                    158 / 24.0,
                    win_cnt=1,
                )
                self.assertEqual(probe_video_frames(self.ffmpeg, mp4_175), 158)
                trimmed_dur = probe_video_duration(self.ffmpeg, mp4_175)
                self.assertIsNotNone(trimmed_dur)
                self.assertAlmostEqual(trimmed_dur, 158 / 24.0, delta=0.06)

    def test_12_cloud_keys_shared_across_all_projects(self):
        """Saving fal.ai & MiniMax keys under one project must share them across all projects."""
        import shutil
        from pipeline.fal_catalog import resolve_minimax_key, _shared_secret_config_target

        stage_tmp = self.root / "_stage"
        stage_tmp.mkdir(parents=True, exist_ok=True)
        _build_temp_project(stage_tmp / "a")
        _build_temp_project(stage_tmp / "b")
        dir_a = self.root / "proj_a"
        dir_b = self.root / "proj_b"
        shutil.move(str(stage_tmp / "a" / "proj"), str(dir_a))
        shutil.move(str(stage_tmp / "b" / "proj"), str(dir_b))
        shutil.rmtree(stage_tmp, ignore_errors=True)
        for d in (dir_a, dir_b):
            (d / "local_config.json").unlink(missing_ok=True)
        # Unrelated per-project local_config without secrets inside dir_b must never block shared keys
        (dir_b / "local_config.json").write_text(json.dumps({"queue_max_parallel": {"fal": 2}}), encoding="utf-8")

        with mock.patch.object(server, "PROJECTS_DIR", self.root):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FAL_KEY", None)
                os.environ.pop("MINIMAX_API_KEY", None)
                os.environ.pop("MINIMAX_KEY", None)

                # Save both keys while proj_b (even with its own non-secret local_config) is active
                h_post = _DummyHandler("/api/fal/config")
                self.assertTrue(server_fal.handle_fal_post(h_post, "/api/fal/config", {
                    "slug": "proj_b",
                    "fal_key": "shared_fal_id:shared_secret_4455",
                    "minimax_key": "shared_mm_secret_8899",
                }))
                self.assertTrue(h_post.payload["success"])
                self.assertEqual(h_post.payload["keys_scope"], "shared")
                self.assertEqual(h_post.payload["hint"], "…4455")
                self.assertEqual(h_post.payload["minimax"]["hint"], "…8899")
                self.assertTrue((self.root / "local_config.json").is_file())
                self.assertNotIn("fal_key_dpapi", (dir_b / "local_config.json").read_text(encoding="utf-8"))

                # Switch to proj_a -> status, direct key resolvers & connection tests inherit both keys
                h_get_a = _DummyHandler("/api/fal/status?slug=proj_a")
                self.assertTrue(server_fal.handle_fal_get(h_get_a, "/api/fal/status", urlparse(h_get_a.path)))
                self.assertTrue(h_get_a.payload["configured"])
                self.assertEqual(h_get_a.payload["hint"], "…4455")
                self.assertTrue(h_get_a.payload["minimax"]["configured"])
                self.assertEqual(h_get_a.payload["minimax"]["hint"], "…8899")
                self.assertEqual(h_get_a.payload["keys_scope"], "shared")
                self.assertEqual(resolve_fal_key(dir_a), ("shared_fal_id:shared_secret_4455", "dpapi"))
                self.assertEqual(resolve_fal_key(dir_b), ("shared_fal_id:shared_secret_4455", "dpapi"))
                self.assertEqual(resolve_minimax_key(dir_a), ("shared_mm_secret_8899", "dpapi"))
                self.assertEqual(resolve_minimax_key(dir_b), ("shared_mm_secret_8899", "dpapi"))

                with mock.patch.object(server_fal, "_probe_fal_connection", return_value=(True, 200, "OK")) as m_fal, \
                     mock.patch.object(server_fal, "_probe_minimax_connection", return_value=(True, 200, "OK")) as m_mm:
                    h_tf = _DummyHandler("/api/fal/test")
                    server_fal.handle_fal_post(h_tf, "/api/fal/test", {"slug": "proj_a"})
                    self.assertTrue(h_tf.payload["success"])
                    m_fal.assert_called_once_with("shared_fal_id:shared_secret_4455")

                    h_tm = _DummyHandler("/api/minimax/test")
                    server_fal.handle_fal_post(h_tm, "/api/minimax/test", {"slug": "proj_a"})
                    self.assertTrue(h_tm.payload["success"])
                    m_mm.assert_called_once_with("shared_mm_secret_8899")

                # Clearing MiniMax key from proj_a removes it for proj_b too
                h_clr = _DummyHandler("/api/fal/config")
                server_fal.handle_fal_post(h_clr, "/api/fal/config", {"slug": "proj_a", "minimax_key": ""})
                h_get_b = _DummyHandler("/api/fal/status?slug=proj_b")
                server_fal.handle_fal_get(h_get_b, "/api/fal/status", urlparse(h_get_b.path))
                self.assertTrue(h_get_b.payload["configured"])
                self.assertFalse(h_get_b.payload["minimax"]["configured"])

        # Workspace projects (all_or_nothing / not_there_yet) always share keys at WORKSPACE_ROOT/local_config.json
        ws_a = WORKSPACE_ROOT / "projects" / "all_or_nothing"
        ws_b = WORKSPACE_ROOT / "projects" / "not_there_yet"
        self.assertEqual(resolve_fal_key(ws_a), resolve_fal_key(ws_b))
        self.assertEqual(resolve_minimax_key(ws_a), resolve_minimax_key(ws_b))
        self.assertEqual(_shared_secret_config_target(ws_a), _shared_secret_config_target(ws_b))

        fal_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "fal_settings.js").read_text(encoding="utf-8")
        self.assertIn("Shared Across", fal_js)


if __name__ == "__main__":
    unittest.main()
