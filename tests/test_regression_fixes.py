"""Targeted regression tests for Batch 1-3 pipeline, server, security, and UI fixes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_ROOT / "work"
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

from pipeline.context import ProjectContext
from pipeline.stage2_hashes import compute_project_task_hashes
from ui import server, server_engines, server_engine_io, server_fal, server_security, server_workers


class _ReqDummy:
    """Minimal HTTP handler double for StudioRequestHandler / server_fal tests."""

    def __init__(self, path: str, body_bytes: bytes | None = None, headers: dict | None = None):
        self.path = path
        self.headers = dict(headers or {})
        if body_bytes is not None and "Content-Length" not in self.headers:
            self.headers["Content-Length"] = str(len(body_bytes))
        self.rfile = io.BytesIO(body_bytes or b"")
        self.status_code = 200
        self.payload = None

    def send_json(self, data, status=200):
        self.status_code = int(status)
        self.payload = data

    def send_error_json(self, message, status=400):
        self.status_code = int(status)
        self.payload = {"error": message}


def _make_mini_project(tmp_root: Path, slug: str = "reg_proj") -> tuple[Path, Path]:
    pdir = tmp_root / slug
    (pdir / "work" / "jobs").mkdir(parents=True, exist_ok=True)
    (pdir / "work" / "renders").mkdir(parents=True, exist_ok=True)
    (pdir / "work" / "anchors").mkdir(parents=True, exist_ok=True)
    (pdir / "audio").mkdir(parents=True, exist_ok=True)
    wav = pdir / "audio" / "song.wav"
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    a01 = pdir / "work" / "anchors" / "A01_Hero.jpg"
    a01.write_bytes(b"JPG")
    cfg = {
        "project": {
            "name": "Regression Test",
            "fps": 24,
            "master_audio": "audio/song.wav",
            "directories": {"anchors": "work/anchors", "renders": "work/renders", "jobs": "work/jobs"},
        },
        "models": {
            "default_video_renderer": 1,
            "default_anchor_renderer": 3,
            "default_video_resolution": 1,
            "video_resolutions": {"1": {"resolution": "832x480", "width": 832, "height": 480}},
            "video_renderers": {"1": {"name": "Wan2.1", "model_type": "t2v", "frames_formula": "4n+1", "audio_prompt_type": "A"}},
            "fal_video_renderers": {
                "fal_minimax_h3_i2v": {
                    "name": "[fal.ai] MiniMax H3",
                    "model_type": "minimax_h3_fl2va_pruned_pdd",
                    "backend": "fal",
                    "fal_endpoint": "minimax/h3/image-to-video",
                }
            },
        },
        "anchors": [{"id": "A01", "filename": "A01_Hero.jpg", "seed": 1001, "prompt": "Hero"}],
        "scenes": [
            {"id": "S01", "start": 0.0, "end": 4.0, "mode": "solo", "stem": "song", "anchor": "A01_Hero.jpg", "prompt": "Singing 1"},
            {"id": "S02", "start": 4.0, "end": 8.0, "mode": "solo", "stem": "song", "anchor": "A01_Hero.jpg", "prompt": "Singing 2"},
        ],
    }
    cfg_path = pdir / f"{slug}.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return pdir, cfg_path


class SecurityAndRouteValidationTests(unittest.TestCase):
    def test_01_path_traversal_and_json_dict_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "valid_proj")
            outside = root / "secret.json"
            outside.write_text("{}", encoding="utf-8")

            self.assertEqual(server_security.resolve_project_dir_safe(root, "valid_proj"), pdir.resolve())
            for bad_slug in ("", ".", "..", "../secret", "a/b", "a\\b", "valid_proj\x00x", None):
                self.assertIsNone(server_security.resolve_project_dir_safe(root, bad_slug))

            self.assertEqual(server_security.resolve_config_in_project_safe(pdir, cfg_path.name), cfg_path.resolve())
            for bad_cfg in ("../secret.json", "sub/file.json", "does_not_exist.json", "state_hashes.json"):
                self.assertIsNone(server_security.resolve_config_in_project_safe(pdir, bad_cfg))

            self.assertIsNone(server_security.resolve_path_under_dir(pdir, "../secret.json"))
            self.assertIsNone(server_security.resolve_path_under_dir(pdir, str(outside)))
            self.assertEqual(server_security.resolve_path_under_dir(pdir, "audio/song.wav"), (pdir / "audio" / "song.wav").resolve())

            d_empty, err_empty = server_security.read_json_dict(_ReqDummy("/x", b"", {}))
            self.assertEqual((d_empty, err_empty), ({}, None))
            d_arr, err_arr = server_security.read_json_dict(_ReqDummy("/x", b"[1,2]"))
            self.assertIsNone(d_arr)
            self.assertIsNotNone(err_arr)
            d_bad, err_bad = server_security.read_json_dict(_ReqDummy("/x", b"{not-json"))
            self.assertIsNone(d_bad)
            self.assertIsNotNone(err_bad)

    def test_02_fal_routes_400_404_409_status_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_mini_project(root, "api_proj")
            with mock.patch.object(server, "PROJECTS_DIR", root):
                h404 = _ReqDummy("/api/fal/config")
                server_fal.handle_fal_post(h404, "/api/fal/config", {"slug": "../escape", "custom_fal_app": "a/b"})
                self.assertEqual(h404.status_code, 404)

                h400 = _ReqDummy("/api/render/engines")
                server_fal.handle_fal_post(h400, "/api/render/engines", {"slug": "api_proj", "enabled_engines": {"bogus": True}})
                self.assertEqual(h400.status_code, 400)

                q_file = root / "api_proj" / "work" / "jobs" / "v3_queue.json"
                q_file.write_text(json.dumps([{"id": "S01_solo_832x480", "output_filename": "S01_solo_832x480", "filename": "S01_solo_832x480.mp4", "status": "rendering"}]), encoding="utf-8")
                h409 = _ReqDummy("/api/queue/set_engine")
                server_fal.handle_fal_post(h409, "/api/queue/set_engine", {"slug": "api_proj", "id": "S01_solo_832x480", "backend_override": "fal"})
                self.assertEqual(h409.status_code, 409)


class PipelineAndSchedulerRegressionTests(unittest.TestCase):
    def test_03_stage_hash_isolation_and_local_override_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir, cfg_path = _make_mini_project(Path(tmp), "hash_proj")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["scenes"][0]["renderer"] = "fal_minimax_h3_i2v"
            cfg["scenes"][0]["mode"] = "duet_method_b"
            cfg["scenes"][0]["prompt_m"] = "Male"
            cfg["scenes"][0]["prompt_f"] = "Female"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            ctx.cli_backend = "local"
            rid, rspec = ctx.resolve_video_renderer(ctx.scenes[0], pass_num=1)
            self.assertNotIn("fal", str(rid))
            self.assertNotEqual(rspec.get("backend"), "fal")

            # Hash section isolation: updating trim_cache must not clobber Stage 2 tasks
            ctx.save_state_hashes({"tasks": {"S01": "abc"}, "trim_cache": {"S01": "t1"}})
            ctx.save_state_hashes({"tasks": {"S01": "xyz_stale"}, "trim_cache": {"S01": "t2"}}, updated_sections={"trim_cache"})
            saved = ctx.load_state_hashes()
            self.assertEqual(saved["tasks"]["S01"], "abc")
            self.assertEqual(saved["trim_cache"]["S01"], "t2")

    def test_04_sticky_stop_reset_and_parallel_downscale_drainer_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "drain_proj")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["scenes"].append({"id": "S03", "start": 8.0, "end": 12.0, "mode": "solo", "stem": "song", "anchor": "A01_Hero.jpg", "prompt": "S3"})
            cfg["scenes"].append({"id": "S04", "start": 12.0, "end": 16.0, "mode": "solo", "stem": "song", "anchor": "A01_Hero.jpg", "prompt": "S4"})
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            orig_pdir = server.PROJECTS_DIR
            orig_en = dict(server.enabled_engines)
            orig_mp = dict(server.max_parallel)
            server.PROJECTS_DIR = root
            try:
                server.max_parallel.update({"local": 1, "fal": 2, "minimax": 1})
                q_file = pdir / "work" / "jobs" / "v3_queue.json"
                q_items = [
                    {"idx": i + 1, "id": f"S0{i+1}_solo_832x480", "scene_id": f"S0{i+1}", "target_arg": f"S0{i+1}", "filename": f"S0{i+1}_solo_832x480.mp4", "output_filename": f"S0{i+1}_solo_832x480", "status": "queued", "needs_render": True, "backend": "fal", "backend_override": "fal"}
                    for i in range(4)
                ]
                q_file.write_text(json.dumps(q_items), encoding="utf-8")

                # Simulate prior stop leaving sticky stop_requested=True before run_multi_engine_queue_worker
                server.engine_workers["fal"]["stop_requested"] = True

                both_started = threading.Event()
                release_first_two = threading.Event()
                seen: list[str] = []
                lock = threading.Lock()

                def _fake_exec(cmd_args, _slug, **_kw):
                    tgt = str(cmd_args[1])
                    with lock:
                        seen.append(tgt)
                        idx_now = len(seen)
                        if idx_now == 2:
                            both_started.set()
                    if idx_now <= 2:
                        release_first_two.wait(timeout=3.0)
                    return 0

                with mock.patch.object(server, "execute_single_render", side_effect=_fake_exec), \
                     mock.patch.object(server, "run_stitch_worker"):
                    server.run_queue_worker("drain_proj", force_restart=False, enabled_engines={"local": False, "fal": True, "minimax": False}, max_parallel={"fal": 2})
                    self.assertFalse(server.engine_workers["fal"]["stop_requested"])
                    self.assertTrue(both_started.wait(timeout=3.0))
                    # Downscale 2 -> 1 while BOTH slots finish at once; one must continue and finish S03/S04
                    server.set_enabled_engines(server, "drain_proj", max_parallel={"fal": 1})
                    release_first_two.set()
                    dl = time.time() + 5.0
                    while server.queue_worker_active and time.time() < dl:
                        time.sleep(0.05)

                self.assertEqual(sorted(seen), ["S01", "S02", "S03", "S04"])
                self.assertEqual(server_engine_io.load_persisted_max_parallel(pdir)["fal"], 1)
            finally:
                server.PROJECTS_DIR = orig_pdir
                server.queue_worker_active = False
                server.render_status["running"] = False
                server.render_status["current_project"] = None
                server.enabled_engines.update(orig_en)
                server.max_parallel.update(orig_mp)
                for b in ("local", "fal", "minimax"):
                    server.engine_slots[b].clear()
                    server.engine_workers[b].update({"running": False, "draining": False, "active_count": 0, "stop_requested": False})

    def test_05_queue_override_pass_hash_and_eta_and_gva_silent_roi(self):
        from pipeline.media_utils import attach_queue_time_estimates
        with tempfile.TemporaryDirectory() as tmp:
            pdir, cfg_path = _make_mini_project(Path(tmp), "gva_proj")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["scenes"][0] = {
                "id": "S01", "start": 0.0, "end": 4.0, "mode": "gva_male_left_silent_right",
                "stem_m": "song", "stem_f": "silent", "anchor": "A01_Hero.jpg",
                "prompt_m": "Male sings", "prompt_f": "Female listens silently",
            }
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            ctx = ProjectContext(cfg_path)
            from pipeline.stage2_hashes import load_queue_backend_overrides
            (ctx.jobs_dir / "v3_queue.json").write_text(json.dumps([
                {"output_filename": "S01_Pass1_Left_832x480", "backend_override": "fal"},
            ]), encoding="utf-8")
            h_def = compute_project_task_hashes(ctx)
            h_ovr = compute_project_task_hashes(ctx, backend_overrides={"S01_Pass1_Left_832x480": "fal"})
            h_from_q = compute_project_task_hashes(ctx, backend_overrides=load_queue_backend_overrides(ctx))
            p1_key = "S01_Pass1_Left_832x480"
            p2_key = "S01_Pass2_Final_832x480"
            self.assertNotEqual(h_def[p1_key][0], h_ovr[p1_key][0])
            self.assertNotEqual(h_def[p2_key][0], h_ovr[p2_key][0])
            self.assertEqual(h_ovr[p1_key][0], h_from_q[p1_key][0])
            self.assertEqual(h_ovr[p2_key][0], h_from_q[p2_key][0])

            # enrich_queue_with_fal_metadata clears stale cloud ETAs on switch back to local + attach_queue_time_estimates uses active_eng_map
            q = [
                {"id": "S01", "scene_id": "S01", "output_filename": "S01_solo_832x480", "filename": "S01_solo_832x480.mp4", "status": "rendering", "duration": 5.0, "resolution": "832x480", "fal_endpoint": "old", "estimated_cost_usd": 0.5, "estimated_seconds": 999.0, "estimated_str": "16m 39s"},
                {"id": "S02", "scene_id": "S02", "output_filename": "S02_solo_832x480", "filename": "S02_solo_832x480.mp4", "status": "queued", "duration": 6.0, "resolution": "832x480", "backend_override": "fal"},
            ]
            sc_map = {s["id"]: s for s in cfg["scenes"]}
            server_fal.enrich_queue_with_fal_metadata(q, sc_map, ctx=ctx, proj_dir=pdir, default_res="832x480")
            self.assertNotIn("fal_endpoint", q[0])
            self.assertEqual(q[1]["backend"], "fal")
            attach_queue_time_estimates(q, sc_map, ctx=ctx, proj_dir=pdir, default_res="832x480", live_eng=None, active_eng_map={"S01_solo_832x480.mp4": {"started_at": time.time() - 10}})
            self.assertIn("estimated_remaining_seconds", q[0])
            self.assertGreater(q[1]["estimated_seconds"], 0)

            # gva_male_left_silent_right right side preserves silent audio cleanly & local cleans cloud keys
            from types import SimpleNamespace
            from pipeline.duet_roi import repair_scene_lipsync_clip
            (ctx.renders_dir / f"{p2_key}.mp4").write_bytes(b"\x00" * 5000)
            captured_tasks = []
            def _fake_crop(*_a, **kw):
                for p_out in (kw["out_first_jpg"], _a[7]):
                    Path(p_out).parent.mkdir(parents=True, exist_ok=True)
                    Path(p_out).write_bytes(b"X")
            def _fake_wgp(**kw):
                t = kw["task_cfg"]
                captured_tasks.append(dict(t))
                out_p = ctx.renders_dir / f"{t['output_filename']}.mp4"
                out_p.write_bytes(b"REPAIRED_CROP")
                return SimpleNamespace(success=True, generated_files=[str(out_p)], errors=[]), False, {}
            def _fake_comp(_ff, _src, _crop, _box, tmp_out, **_kw):
                Path(tmp_out).write_bytes(b"\x00" * 5000)

            with mock.patch("pipeline.duet_roi.probe_video_dimensions", return_value=(832, 480)), \
                 mock.patch("pipeline.duet_roi.probe_video_fps", return_value=24.0), \
                 mock.patch("pipeline.duet_roi.probe_video_frames", return_value=97), \
                 mock.patch("pipeline.duet_roi.prepare_roi_crop_and_mask", side_effect=_fake_crop), \
                 mock.patch("pipeline.wgp_bridge.execute_wgp_job", side_effect=_fake_wgp), \
                 mock.patch("pipeline.duet_roi.composite_roi_onto_guide", side_effect=_fake_comp):
                res = repair_scene_lipsync_clip(ctx, mock.MagicMock(), "S01", side="right", roi_mode="half_split", backend="local", dummy_render=False)
                with self.assertRaises(ValueError):
                    repair_scene_lipsync_clip(ctx, mock.MagicMock(), "S01", side="right", roi_mode="half_split", backend="fal", dummy_render=False)
            self.assertTrue(res["take_id"].startswith("take_rep_"))
            self.assertIn("silent", Path(str(captured_tasks[0].get("audio_guide") or "")).name.lower())
            self.assertFalse(any(k.startswith("_") or k in ("backend", "fal_endpoint", "minimax_model") for k in captured_tasks[0]))

    def test_06_late_enqueue_wakes_idle_and_recovered_engine_mid_run(self):
        """Items queued after hitting Run must execute even when another engine is running, after a prior engine failure, or if its stem already ran once in-session."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "late_enq_proj")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["scenes"].append({"id": "S03", "start": 8.0, "end": 12.0, "mode": "solo", "stem": "song", "anchor": "A01_Hero.jpg", "prompt": "S3"})
            cfg["scenes"].append({"id": "S04", "start": 12.0, "end": 16.0, "mode": "solo", "stem": "song", "anchor": "A01_Hero.jpg", "prompt": "S4"})
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            orig_pdir = server.PROJECTS_DIR
            orig_en = dict(server.enabled_engines)
            orig_mp = dict(server.max_parallel)
            server.PROJECTS_DIR = root
            try:
                server.max_parallel.update({"local": 1, "fal": 1, "minimax": 1})
                q_file = pdir / "work" / "jobs" / "v3_queue.json"
                q_items = [
                    {"idx": 1, "id": "S01_solo_832x480", "scene_id": "S01", "target_arg": "S01", "filename": "S01_solo_832x480.mp4", "output_filename": "S01_solo_832x480", "status": "queued", "needs_render": True, "backend": "minimax", "backend_override": "minimax", "task_hash": "h1"},
                    {"idx": 2, "id": "S02_solo_832x480", "scene_id": "S02", "target_arg": "S02", "filename": "S02_solo_832x480.mp4", "output_filename": "S02_solo_832x480", "status": "queued", "needs_render": True, "backend": "local", "backend_override": "local", "task_hash": "h2"},
                    {"idx": 3, "id": "S03_solo_832x480", "scene_id": "S03", "target_arg": "S03", "filename": "S03_solo_832x480.mp4", "output_filename": "S03_solo_832x480", "status": "completed", "needs_render": False, "backend": "local", "backend_override": "local", "task_hash": "h3"},
                    {"idx": 4, "id": "S04_solo_832x480", "scene_id": "S04", "target_arg": "S04", "filename": "S04_solo_832x480.mp4", "output_filename": "S04_solo_832x480", "status": "completed", "needs_render": False, "backend": "local", "backend_override": "local", "task_hash": "h4"},
                ]
                q_file.write_text(json.dumps(q_items), encoding="utf-8")

                mm_started = threading.Event()
                mm_release = threading.Event()
                s02_failed_once = [False]
                ran: list[str] = []
                lock = threading.Lock()

                def _fake_exec(cmd_args, _slug, **_kw):
                    tgt = str(cmd_args[1])
                    with lock:
                        ran.append(tgt)
                    if tgt == "S01":
                        mm_started.set()
                        mm_release.wait(timeout=4.0)
                        return 0
                    if tgt == "S02" and not s02_failed_once[0]:
                        s02_failed_once[0] = True
                        return 1  # Pause local mid-session due to failure
                    time.sleep(0.05)
                    return 0

                with mock.patch.object(server, "execute_single_render", side_effect=_fake_exec), \
                     mock.patch.object(server, "run_stitch_worker"):
                    server.run_queue_worker("late_enq_proj", force_restart=False, enabled_engines={"local": True, "fal": False, "minimax": True})
                    self.assertTrue(mm_started.wait(timeout=3.0))

                    # Wait for local failure to pause local while minimax S01 is still active
                    dl = time.time() + 3.0
                    while ("local" not in (getattr(server, "_session_failed_engines", set()) or set()) or server.engine_workers["local"]["running"]) and time.time() < dl:
                        time.sleep(0.02)
                    self.assertIn("local", server._session_failed_engines)

                    # Simulate S03 having previously completed earlier in the same session
                    server._session_completed_hashes.add(("S03_solo_832x480", "h3"))

                    # Preserve failed state across compute_fast_project_queue(force_recheck=False)
                    q_mid = json.loads(q_file.read_text(encoding="utf-8"))
                    s02_row = next(x for x in q_mid if x["scene_id"] == "S02")
                    self.assertEqual(s02_row["status"], "failed")

                    # Late-enqueue S03 and S04 while minimax S01 is still running; local must recover & run both!
                    server.enqueue_in_project_queue("late_enq_proj", "S03_solo_832x480")
                    server.enqueue_in_project_queue("late_enq_proj", "S04_solo_832x480")
                    server.run_queue_worker("late_enq_proj", force_restart=False)

                    dl2 = time.time() + 4.0
                    while ("S03" not in ran or "S04" not in ran) and time.time() < dl2:
                        time.sleep(0.02)
                    self.assertIn("S03", ran)
                    self.assertIn("S04", ran)

                    # Also verify requeue_failed_items resets S02 so retrying runs S02 cleanly
                    server_engine_io.requeue_failed_items(server, "late_enq_proj")
                    server.run_queue_worker("late_enq_proj", force_restart=False)
                    mm_release.set()
                    dl3 = time.time() + 4.0
                    while server.queue_worker_active and time.time() < dl3:
                        time.sleep(0.03)
                self.assertEqual(ran.count("S02"), 2)
                self.assertIn("S03", ran)
                self.assertIn("S04", ran)
            finally:
                server.PROJECTS_DIR = orig_pdir
                server.queue_worker_active = False
                server.render_status["running"] = False
                server.render_status["current_project"] = None
                server._session_completed_hashes = set()
                server._session_failed_engines = set()
                server.enabled_engines.update(orig_en)
                server.max_parallel.update(orig_mp)
                for b in ("local", "fal", "minimax"):
                    server.engine_slots[b].clear()
                    server.engine_workers[b].update({"running": False, "draining": False, "active_count": 0, "stop_requested": False})

    def test_07_local_oom_automatic_retry_and_non_oom_no_retry(self):
        """Local CUDA OOM crashes trigger 1 automatic restart+retry; non-OOM failures do not retry."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "oom_retry_proj")
            orig_pdir = server.PROJECTS_DIR
            orig_en = dict(server.enabled_engines)
            orig_mp = dict(server.max_parallel)
            orig_delay = getattr(server, "oom_retry_delay_s", 3.0)
            server.PROJECTS_DIR = root
            server.oom_retry_delay_s = 0.02
            try:
                server.max_parallel.update({"local": 1, "fal": 1, "minimax": 1})
                jdir = pdir / "work" / "jobs"
                q_file = jdir / "v3_queue.json"
                q_items = [
                    {"idx": 1, "id": "S01_solo_832x480", "scene_id": "S01", "target_arg": "S01", "filename": "S01_solo_832x480.mp4", "output_filename": "S01_solo_832x480", "status": "queued", "needs_render": True, "backend": "local", "backend_override": "local"},
                    {"idx": 2, "id": "S02_solo_832x480", "scene_id": "S02", "target_arg": "S02", "filename": "S02_solo_832x480.mp4", "output_filename": "S02_solo_832x480", "status": "queued", "needs_render": True, "backend": "local", "backend_override": "local"},
                ]
                q_file.write_text(json.dumps(q_items), encoding="utf-8")

                attempts: list[str] = []

                def _exec_oom_then_ok(cmd_args, _slug, **_kw):
                    tgt = str(cmd_args[1])
                    attempts.append(tgt)
                    if tgt == "S01" and attempts.count("S01") == 1:
                        (jdir / "engine_status_local__S01_solo_832x480.json").write_text(json.dumps({
                            "state": "failed",
                            "backend": "local",
                            "output_filename": "S01_solo_832x480",
                            "error": ["CUDA error: out of memory (cudaErrorMemoryAllocation)"],
                            "updated_at": time.time(),
                        }), encoding="utf-8")
                        return 1
                    return 0

                with mock.patch.object(server, "execute_single_render", side_effect=_exec_oom_then_ok), \
                     mock.patch.object(server, "run_stitch_worker"):
                    server.run_queue_worker("oom_retry_proj", force_restart=False, enabled_engines={"local": True, "fal": False, "minimax": False})
                    dl = time.time() + 4.0
                    while server.queue_worker_active and time.time() < dl:
                        time.sleep(0.02)

                self.assertEqual(attempts, ["S01", "S01", "S02"])
                q_done = json.loads(q_file.read_text(encoding="utf-8"))
                self.assertEqual([x["status"] for x in q_done], ["completed", "completed"])

                # Non-OOM errors must NOT retry
                non_oom_attempts: list[str] = []
                q_file.write_text(json.dumps([q_items[0]]), encoding="utf-8")
                server._session_completed_hashes = set()

                def _exec_non_oom(cmd_args, _slug, **_kw):
                    non_oom_attempts.append(str(cmd_args[1]))
                    (jdir / "engine_status_local__S01_solo_832x480.json").write_text(json.dumps({
                        "state": "failed",
                        "backend": "local",
                        "output_filename": "S01_solo_832x480",
                        "error": ["Missing audio stem"],
                        "updated_at": time.time(),
                    }), encoding="utf-8")
                    return 1

                with mock.patch.object(server, "execute_single_render", side_effect=_exec_non_oom), \
                     mock.patch.object(server, "run_stitch_worker"):
                    server.run_queue_worker("oom_retry_proj", force_restart=False, enabled_engines={"local": True, "fal": False, "minimax": False})
                    dl = time.time() + 4.0
                    while server.queue_worker_active and time.time() < dl:
                        time.sleep(0.02)

                self.assertEqual(non_oom_attempts, ["S01"])
                self.assertEqual(json.loads(q_file.read_text(encoding="utf-8"))[0]["status"], "failed")
            finally:
                server.PROJECTS_DIR = orig_pdir
                server.oom_retry_delay_s = orig_delay
                server.queue_worker_active = False
                server.render_status["running"] = False
                server.render_status["current_project"] = None
                server._session_completed_hashes = set()
                server._session_failed_engines = set()
                server.enabled_engines.update(orig_en)
                server.max_parallel.update(orig_mp)
                for b in ("local", "fal", "minimax"):
                    server.engine_slots[b].clear()
                    server.engine_workers[b].update({"running": False, "draining": False, "active_count": 0, "stop_requested": False})

    def test_08_stale_completed_status_does_not_leak_into_new_run(self):
        """Pre-slot 'completed' status JSONs (old started_at / pct=100) must not leak into a newly started run."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, _ = _make_mini_project(root, "stale_proj")
            orig_pdir = server.PROJECTS_DIR
            orig_proj = server.render_status.get("current_project")
            orig_running = server.render_status.get("running")
            server.PROJECTS_DIR = root
            try:
                jdir = pdir / "work" / "jobs"
                old_ts = time.time() - 83000.0
                old_payload = {
                    "state": "completed", "backend": "local", "filename": "S01_solo_832x480.mp4",
                    "output_filename": "S01_solo_832x480", "phase": "completed", "raw_phase": "Completed",
                    "pct": 100.0, "started_at": old_ts, "updated_at": old_ts + 20.0,
                }
                (jdir / "engine_status_local__S01_solo_832x480.json").write_text(json.dumps(old_payload), encoding="utf-8")
                (jdir / "engine_status_local.json").write_text(json.dumps(old_payload), encoding="utf-8")
                (jdir / "engine_status.json").write_text(json.dumps(old_payload), encoding="utf-8")

                now_ts = round(time.time(), 3)
                with server.render_lock:
                    server.render_status["running"] = True
                    server.render_status["current_project"] = "stale_proj"
                    server.queue_worker_active = True
                    server.engine_slots["local"]["local#1"] = {
                        "slot_id": "local#1", "slot_seq": 1, "backend": "local", "running": True,
                        "draining": False, "project": "stale_proj", "filename": "S01_solo_832x480.mp4",
                        "output_filename": "S01_solo_832x480", "started_at": now_ts,
                    }

                st = server.get_multi_engine_render_status()
                tk = st["engines"]["local"]["active_tasks"][0]["engine_status"]
                self.assertEqual(tk["state"], "rendering")
                self.assertEqual(tk["phase"], "starting")
                self.assertEqual(tk["pct"], 0.0)
                self.assertAlmostEqual(float(tk["started_at"]), now_ts, delta=1.0)
                self.assertNotEqual(st["engine_status"].get("raw_phase"), "Completed")

                # write_starting_slot_status resets disk files before subprocess writes its first update
                server_engine_io.write_starting_slot_status(pdir, "local", "S01_solo_832x480", now_ts, reason="EDITED")
                disk_st = json.loads((jdir / "engine_status_local__S01_solo_832x480.json").read_text(encoding="utf-8"))
                self.assertEqual(disk_st["state"], "rendering")
                self.assertEqual(disk_st["pct"], 0.0)
                self.assertAlmostEqual(float(disk_st["started_at"]), now_ts, delta=0.1)

                # Long 1080p denoising step (>15m without update) must NOT expire active slot progress to Initializing 0%
                long_step_st = {"state": "rendering", "filename": "S01_solo_832x480.mp4", "output_filename": "S01_solo_832x480", "phase": "inference", "raw_phase": "Denoising", "pct": 20.0, "started_at": now_ts - 1400.0, "updated_at": now_ts - 1100.0}
                self.assertTrue(server_engine_io.is_status_valid_for_slot(long_step_st, now_ts - 1450.0, "S01_solo_832x480"))
                self.assertFalse(server_engine_io.is_status_valid_for_slot(long_step_st, None, "S01_solo_832x480"))
            finally:
                server.PROJECTS_DIR = orig_pdir
                server.queue_worker_active = False
                server.render_status["running"] = orig_running
                server.render_status["current_project"] = orig_proj
                server.engine_slots["local"].clear()
                server_engines._sync_engine_worker_summary(server, "local", force_sync=True)

    def test_09_cloud_polling_ticks_do_not_flood_stdout_logs(self):
        """Periodic cloud polling ticks like 'MiniMax: running (4s)', '(8s)' must not flood console stdout."""
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from pipeline.context import ProjectContext
        from pipeline.wgp_bridge import execute_wgp_job

        with tempfile.TemporaryDirectory() as tmp:
            pdir, cfg_path = _make_mini_project(Path(tmp), "log_proj")
            ctx = ProjectContext(cfg_path)
            events = [
                SimpleNamespace(kind="progress", data=SimpleNamespace(phase="uploading", raw_phase="MiniMax: submitting (MiniMax-H3)...", status="MiniMax: submitting (MiniMax-H3)...", current_step=None, total_steps=None, window_index=1, window_count=1, progress=None)),
                SimpleNamespace(kind="progress", data=SimpleNamespace(phase="Denoising", raw_phase="MiniMax: running", status="MiniMax: running", current_step=None, total_steps=None, window_index=1, window_count=1, progress=None)),
                SimpleNamespace(kind="progress", data=SimpleNamespace(phase="Denoising", raw_phase="MiniMax: running (4s)", status="MiniMax: running (4s)", current_step=None, total_steps=None, window_index=1, window_count=1, progress=None)),
                SimpleNamespace(kind="progress", data=SimpleNamespace(phase="Denoising", raw_phase="MiniMax: running (9s)", status="MiniMax: running (9s)", current_step=None, total_steps=None, window_index=1, window_count=1, progress=None)),
                SimpleNamespace(kind="progress", data=SimpleNamespace(phase="Denoising", raw_phase="MiniMax: running (13s)", status="MiniMax: running (13s)", current_step=None, total_steps=None, window_index=1, window_count=1, progress=None)),
                SimpleNamespace(kind="progress", data=SimpleNamespace(phase="downloading", raw_phase="MiniMax: downloading output...", status="MiniMax: downloading output...", current_step=None, total_steps=None, window_index=1, window_count=1, progress=None)),
            ]
            class _FakeJob:
                events = type("E", (), {"iter_with_heartbeats": lambda _s, timeout=0.25: iter(events)})()
                def result(self): return SimpleNamespace(success=True, generated_files=["out.mp4"], errors=[])
                def cancel(self): pass
            class _FakeMiniMaxSession:
                def submit_task(self, _cfg): return _FakeJob()
            _FakeMiniMaxSession.__name__ = "MiniMaxSession"
            sess = _FakeMiniMaxSession()

            buf = io.StringIO()
            with redirect_stdout(buf):
                res, cancelled, _ = execute_wgp_job(ctx, sess, {"_backend": "minimax"}, 1, 1, "S01.mp4", "scene", "TEST")
            self.assertTrue(res.success)
            self.assertFalse(cancelled)
            human_lines = [l.strip() for l in buf.getvalue().splitlines() if l.strip() and not l.startswith("[QUEUE_")]
            running_lines = [l for l in human_lines if "MiniMax: running" in l]
            self.assertEqual(running_lines, ["[Denoising] MiniMax: running"])
            q_lines = [l for l in buf.getvalue().splitlines() if l.startswith("[QUEUE_PROGRESS]")]
            self.assertEqual(len(q_lines), 6)


class FrontendRegressionNodeTests(unittest.TestCase):
    def test_06_frontend_duet_fal_and_queue_fixes_in_node_vm(self):
        script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const els = new Map();
function mkEl(tag = "div") {
  const el = {
    tagName: tag.toUpperCase(),
    id: "", value: "", checked: true, disabled: false, textContent: "",
    style: {}, dataset: {}, className: "", listeners: {}, children: [],
    options: [{ textContent: "" }],
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    appendChild(c) { c.parentElement = this; this.children.push(c); if (c.id) els.set(c.id, c); return c; },
    insertBefore(c) { return this.appendChild(c); },
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); },
    closest() { return null; },
    matches() { return false; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    contains() { return false; },
  };
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get() { return html; },
    set(v) {
      html = String(v);
      for (const m of html.matchAll(/id="([^"]+)"/g)) {
        if (!els.has(m[1])) {
          const ch = mkEl("input"); ch.id = m[1]; els.set(m[1], ch);
        }
      }
    },
  });
  return el;
}

const scDuet = { id: "S15", mode: "duet_method_b", start: 40, end: 50, resolution: "6" };
const scFallback = { id: "S16", prompt_m: "Male", prompt_f: "Female" };
const grp = mkEl("div"); grp.id = "videoRendererGroup"; els.set("videoRendererGroup", grp);
const upSel = mkEl("select"); upSel.id = "sceneLateUpscaleMethod"; grp.appendChild(upSel); els.set("sceneLateUpscaleMethod", upSel);
const tray = mkEl("div"); tray.id = "sceneTakesTray"; els.set("sceneTakesTray", tray);
const upBtn = mkEl("button"); upBtn.id = "btnUpscaleTake"; tray.appendChild(upBtn); els.set("btnUpscaleTake", upBtn);
const runBtn = mkEl("button"); runBtn.id = "btnRenderAll"; els.set("btnRenderAll", runBtn);
const lanes = mkEl("div"); lanes.id = "multiEngineActiveLanes"; els.set("multiEngineActiveLanes", lanes);
const tbody = mkEl("tbody"); tbody.id = "queueTableBody"; els.set("queueTableBody", tbody);

const orderLog = [];
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN,
  document: {
    activeElement: null,
    getElementById: (id) => els.get(id) || null,
    querySelectorAll: (sel) => sel === "#inspectorForm input, #inspectorForm select" ? [] : [],
    createElement: mkEl,
    addEventListener: () => {},
  },
  window: {},
  state: {
    currentSlug: "demo",
    selectedSceneIdx: 0,
    isDirty: false,
    projectData: { scenes: [scDuet, scFallback], models: { default_video_resolution: 6 } },
    models: { video_renderers: {}, fal_video_renderers: {}, minimax_video_renderers: {} },
    sseTaskStatuses: new Map(),
    renderQueue: [
      { idx: 1, id: "S01", output_filename: "S01", filename: "S01.mp4", status: "completed", backend: "fal" },
      { idx: 2, id: "S02", output_filename: "S02", filename: "S02.mp4", status: "rendering", backend: "minimax" },
    ],
  },
  escapeHtml: (s) => String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  markProjectDirty: () => { orderLog.push("dirty"); ctx.state.isDirty = true; },
  recordHistorySnapshot: () => { orderLog.push("snap"); },
  renderSceneList: () => {},
  renderQueueUI: () => {},
  refreshProjectAssets: async () => {},
  showToast: () => {},
  alert: () => {},
  fetch: async () => ({ ok: false, status: 500, json: async () => ({ error: "fail" }) }),
  setInterval, clearInterval,
});
ctx.window = ctx;
ctx.window.state = ctx.state;
ctx.window.markProjectDirty = ctx.markProjectDirty;
ctx.window.recordHistorySnapshot = ctx.recordHistorySnapshot;

vm.runInContext(fs.readFileSync("work/ui/static/js/fal_settings.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/duet_repair.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);

// 1. fal pricing & window export (including per-pass renderer override)
assert.strictEqual(typeof ctx.window.estimateSceneCloudCostFrontend, "function");
const est1080 = ctx.window.estimateSceneCloudCostFrontend(scDuet, "fal");
assert.strictEqual(est1080.costStr, "~$1.20");
ctx.state.models.fal_video_renderers.fal_ltx25 = { fal_endpoint: "fal-ai/ltx-2.5/audio-to-video", price_per_sec_usd: { "1080p": 0.10 } };
scDuet.renderer_m = "fal_ltx25";
const estPass1 = ctx.window.estimateSceneCloudCostFrontend(scDuet, "fal", 1);
assert.strictEqual(estPass1.endpoint, "fal-ai/ltx-2.5/audio-to-video");
assert.strictEqual(estPass1.costStr, "~$1.00");
delete scDuet.renderer_m;

// 2. duet_repair: isTwoPassScene prompt_m/f fallback preserved & dirty-before-snapshot & strict ROI validation
assert.strictEqual(ctx.window.isTwoPassScene(scFallback), true);
ctx.window.ensureDuetPassControls(grp);
const selF = els.get("sceneBackendF");
selF.value = "local";
selF.dispatchEvent({ type: "change", target: selF });
assert.deepStrictEqual(orderLog.slice(0, 2), ["dirty", "snap"]);

const boxEl = els.get("scenePass2RoiBox");
scDuet.pass2_roi_mode = "custom";
scDuet.pass2_roi_box = [0.4, 0.1, 0.9, 0.9];
boxEl.value = "0.5abc,0.1,0.9,0.9";
boxEl.dispatchEvent({ type: "blur", target: boxEl });
assert.deepStrictEqual(scDuet.pass2_roi_box, [0.4, 0.1, 0.9, 0.9], "Garbage parseFloat token must be rejected");

// 3. queue_engines: empty filename progress & Hero Card promotion on partial completion
ctx.window.handleMultiEngineSSEEvent("[QUEUE_ACTIVE]", { filename: "S01.mp4", backend: "fal" });
ctx.window.handleMultiEngineSSEEvent("[QUEUE_ACTIVE]", { filename: "S02.mp4", backend: "minimax" });
ctx.window.handleMultiEngineSSEEvent("[QUEUE_PROGRESS]", { filename: "", backend: "minimax", pct: 42, raw_phase: "Denoising" });
assert.strictEqual(ctx.state.engineLive.minimax.filename, "S02.mp4");
assert.strictEqual(ctx.state.engineLive.minimax.stem, "S02");
ctx.window.handleMultiEngineSSEEvent("[QUEUE_DONE]", { filename: "S01.mp4", backend: "fal" });
const promoted = ctx.window.promoteActiveRenderTaskFromState("S01.mp4");
assert.strictEqual(promoted, true);
assert.strictEqual(ctx.state.activeRenderTask.filename, "S02.mp4");

// 4. queue_engines: queued list sorting by video scene order, engine, and length (preserving Pass 1 before Pass 2 & pinned rendering)
(async () => {
  ctx.state.projectData.scenes = [{ id: "S01", start: 0, end: 12 }, { id: "S02", start: 12, end: 16 }, { id: "S03", start: 16, end: 24 }];
  ctx.state.renderQueue = [
    { idx: 1, id: "S03_solo", output_filename: "S03_solo", filename: "S03_solo.mp4", scene_id: "S03", backend: "fal", duration: 8, status: "queued" },
    { idx: 2, id: "S02_Pass1", output_filename: "S02_Pass1", filename: "S02_Pass1.mp4", scene_id: "S02", pass: 1, backend: "minimax", duration: 4, status: "queued" },
    { idx: 3, id: "S02_Pass2", output_filename: "S02_Pass2", filename: "S02_Pass2.mp4", scene_id: "S02", pass: 2, backend: "local", duration: 4, status: "queued" },
    { idx: 4, id: "S01_solo", output_filename: "S01_solo", filename: "S01_solo.mp4", scene_id: "S01", backend: "local", duration: 12, status: "rendering" },
  ];
  const orderNames = () => JSON.stringify(ctx.state.renderQueue.map((x) => x.output_filename));
  await ctx.window.sortRenderQueue("length");
  assert.strictEqual(orderNames(), JSON.stringify(["S01_solo", "S02_Pass1", "S02_Pass2", "S03_solo"]));
  await ctx.window.sortRenderQueue("length");
  assert.strictEqual(orderNames(), JSON.stringify(["S01_solo", "S03_solo", "S02_Pass1", "S02_Pass2"]));
  await ctx.window.sortRenderQueue("engine");
  assert.strictEqual(orderNames(), JSON.stringify(["S01_solo", "S03_solo", "S02_Pass1", "S02_Pass2"]));
  await ctx.window.sortRenderQueue("scene");
  assert.strictEqual(orderNames(), JSON.stringify(["S01_solo", "S02_Pass1", "S02_Pass2", "S03_solo"]));
  console.log("FRONTEND_REG_OK");
})().catch((err) => { console.error(err); process.exit(1); });
"""
        res = subprocess.run(["node", "-e", script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("FRONTEND_REG_OK", res.stdout)

    def test_07_scene_lock_immediate_persist_isolated_from_unsaved_edits(self):
        from ui import server_routes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, cfg_path = _make_mini_project(root, "lock_proj")
            with mock.patch.object(server, "PROJECTS_DIR", root):
                # 1. Backend /api/scene/lock persists ONLY locked=True and preserves disk fields
                h_lock = _ReqDummy("/api/scene/lock", json.dumps({"slug": "lock_proj", "scene_id": "S01", "locked": True}).encode("utf-8"))
                server_routes.StudioRequestHandler.do_POST(h_lock)
                self.assertEqual(h_lock.status_code, 200)
                self.assertTrue(h_lock.payload.get("success"))
                saved_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertTrue(saved_disk["scenes"][0].get("locked"))
                self.assertEqual(saved_disk["scenes"][0]["prompt"], "Singing 1")

                # Reject non-boolean locked and unknown scene_id
                h_bad = _ReqDummy("/api/scene/lock", json.dumps({"slug": "lock_proj", "scene_id": "S01", "locked": "yes"}).encode("utf-8"))
                server_routes.StudioRequestHandler.do_POST(h_bad)
                self.assertEqual(h_bad.status_code, 400)
                h_miss = _ReqDummy("/api/scene/lock", json.dumps({"slug": "lock_proj", "scene_id": "S99", "locked": True}).encode("utf-8"))
                server_routes.StudioRequestHandler.do_POST(h_miss)
                self.assertEqual(h_miss.status_code, 404)

                # Unlock persists removal cleanly
                h_unlock = _ReqDummy("/api/scene/lock", json.dumps({"slug": "lock_proj", "scene_id": "S01", "locked": False}).encode("utf-8"))
                server_routes.StudioRequestHandler.do_POST(h_unlock)
                self.assertEqual(h_unlock.status_code, 200)
                self.assertNotIn("locked", json.loads(cfg_path.read_text(encoding="utf-8"))["scenes"][0])

        # 2. Frontend toggleSceneLock immediately persists lock without marking project dirty or saving unsaved edits
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const els = new Map();
const mkEl = (id = "") => {
  const el = {
    id, tagName: "DIV", value: "", textContent: "", disabled: false,
    style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, querySelectorAll() { return []; }, firstElementChild: null, children: [],
  };
  if (id) els.set(id, el);
  return el;
};
["btnUndo","btnRedo","btnCheckpointsModal","btnCloseCheckpointsModal","btnCreateCheckpoint","btnDiscardChanges","btnSaveProject","inspectorForm","btnToggleSceneLock","btnDeleteScene","btnRevertSceneToRendered","btnSplitSceneHeader","btnRefreshMasterVideo","btnStitchMasterFromTab"].forEach(mkEl);

const fetchCalls = [];
const state = {
  currentSlug: "lock_proj",
  selectedSceneIdx: 0,
  isDirty: false,
  projectData: {
    scenes: [
      { id: "S01", start: 0, end: 4, mode: "solo", prompt: "Orig 1" },
      { id: "S02", start: 4, end: 8, mode: "solo", prompt: "Orig 2" },
    ],
  },
};
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN,
  document: {
    readyState: "complete",
    getElementById: (id) => els.get(id) || null,
    createElement: (t) => mkEl(),
    addEventListener() {},
  },
  window: {},
  addEventListener() {},
  state,
  markProjectDirty: (dirty = true) => { state.isDirty = dirty; },
  redrawTimeline: () => {},
  renderSceneList: () => {},
  updateProjectSettingsUI: () => {},
  selectScene: () => {},
  syncScenesTabCard: () => {},
  updateInspectorSceneHeader: () => {},
  showToast: () => {},
  confirm: () => true,
  fetch: async (url, opts) => {
    fetchCalls.push({ url, body: JSON.parse(opts.body) });
    return { ok: true, status: 200, json: async () => ({ success: true }) };
  },
  setTimeout, clearTimeout,
});
ctx.window = ctx;
ctx.window.state = state;
vm.runInContext(fs.readFileSync("work/ui/static/js/takes_and_history.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/master_and_export.js", "utf8"), ctx);

(async () => {
  ctx.initProjectHistory();
  assert.strictEqual(state.isDirty, false);

  // Case A: Clean project -> toggleSceneLock persists only locked and stays NOT dirty
  await ctx.toggleSceneLock(0);
  assert.strictEqual(state.projectData.scenes[0].locked, true);
  assert.strictEqual(fetchCalls.length, 1);
  assert.deepStrictEqual(fetchCalls[0], { url: "/api/scene/lock", body: { slug: "lock_proj", scene_id: "S01", locked: true } });
  assert.strictEqual(state.isDirty, false, "Locking a clean scene must not leave project dirty");

  // Case B: Make an unsaved edit to S01.prompt, then unlock S01 -> stays dirty AND discard keeps unlocked state!
  state.projectData.scenes[0].prompt = "Unsaved Prompt Edit";
  ctx.recordHistorySnapshot("Edit S01 Prompt");
  assert.strictEqual(state.isDirty, true);

  await ctx.toggleSceneLock(0);
  assert.strictEqual(state.projectData.scenes[0].locked, undefined);
  assert.strictEqual(fetchCalls.length, 2);
  assert.deepStrictEqual(fetchCalls[1], { url: "/api/scene/lock", body: { slug: "lock_proj", scene_id: "S01", locked: false } });
  assert.strictEqual(state.isDirty, true, "Unrelated unsaved prompt edit must remain dirty (unsaved)");
  assert.ok(!("prompt" in fetchCalls[1].body), "Lock endpoint payload must never send prompt edits");

  // Discarding unsaved changes reverts prompt back to "Orig 1" while preserving disk lock state (unlocked)
  ctx.discardUnsavedChanges();
  assert.strictEqual(state.projectData.scenes[0].prompt, "Orig 1");
  assert.strictEqual(state.projectData.scenes[0].locked, undefined);
  assert.strictEqual(state.isDirty, false);
  console.log("LOCK_ISOLATION_OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("LOCK_ISOLATION_OK", res.stdout)

    def test_scenes_tab_locked_and_unlocked_filters(self):
        """Scenes tab toolbar provides Locked and Unlocked filter pills with accurate filtering and counts."""
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function mkNode(tag = "div") {
  const node = {
    tagName: tag.toUpperCase(),
    _className: "",
    _text: "",
    _innerHTML: "",
    dataset: {},
    style: {},
    children: [],
    parentNode: null,
    scrollTop: 0,
    options: [],
    classList: {
      contains: (c) => node._className.split(/\s+/).includes(c),
      toggle: (c, force) => {
        const s = new Set(node._className.split(/\s+/).filter(Boolean));
        if (force === undefined ? !s.has(c) : force) s.add(c); else s.delete(c);
        node._className = [...s].join(" ");
      },
    },
    appendChild(ch) { if (ch) { ch.parentNode = this; this.children.push(ch); } return ch; },
    replaceWith(rep) {
      if (!this.parentNode) return;
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) { rep.parentNode = this.parentNode; this.parentNode.children[i] = rep; }
    },
    add(o) { this.options.push(o); },
    addEventListener() {},
    querySelectorAll(sel) {
      const out = [];
      const visit = (n) => {
        for (const ch of (n.children || [])) {
          if (sel === ".scene-card" && ch.classList?.contains("scene-card")) out.push(ch);
          else if (sel === ".scenes-filter-pill" && ch.classList?.contains("scenes-filter-pill")) out.push(ch);
          visit(ch);
        }
      };
      visit(this);
      return out;
    },
    querySelector(sel) {
      if (sel === ".scenes-list-container") return this._walk((n) => n.classList?.contains("scenes-list-container"));
      const mFilter = sel.match(/^\.scenes-filter-pill\[data-filter-key="([^"]+)"\]$/);
      if (mFilter) return this._walk((n) => n.classList?.contains("scenes-filter-pill") && n.dataset?.filterKey === mFilter[1]);
      const mCard = sel.match(/^\.scene-card\[data-scene-idx="(\d+)"\]$/);
      if (mCard) return this._walk((n) => n.classList?.contains("scene-card") && String(n.dataset?.sceneIdx) === mCard[1]);
      return null;
    },
    _walk(pred) {
      for (const ch of (this.children || [])) {
        if (pred(ch)) return ch;
        const hit = ch._walk?.(pred);
        if (hit) return hit;
      }
      return null;
    },
    contains(target) {
      if (this === target) return true;
      return (this.children || []).some((ch) => ch.contains?.(target));
    },
  };
  Object.defineProperty(node, "className", { get: () => node._className, set: (v) => { node._className = String(v || ""); } });
  Object.defineProperty(node, "textContent", { get: () => node._text, set: (v) => { node._text = String(v || ""); } });
  Object.defineProperty(node, "innerHTML", { get: () => node._innerHTML, set: (v) => { node._innerHTML = String(v || ""); if (!v) node.children = []; } });
  return node;
}

const tabRoot = mkNode("div");
tabRoot.id = "tab-scenes";
tabRoot.className = "active";

const state = {
  selectedSceneIdx: 0,
  renderQueue: [],
  assets: { clips: {} },
  projectData: {
    project: { fps: 24 },
    scenes: [
      { id: "S01", start: 0, end: 4, mode: "solo", anchor: "A01", locked: true },
      { id: "S02", start: 4, end: 8, mode: "solo", anchor: "A02" },
      { id: "S03", start: 8, end: 12, mode: "duet_method_b", anchor: "A03", locked: true },
    ],
  },
};

const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN, setTimeout, clearTimeout,
  document: {
    readyState: "complete",
    activeElement: null,
    getElementById: (id) => (id === "tab-scenes" ? tabRoot : null),
    createElement: (tag) => mkNode(tag),
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
    addEventListener() {},
  },
  Option: function (lbl, val) { return { text: lbl, value: val, dataset: {} }; },
  window: {},
  state,
  canonicalizeSceneMode: (m) => m || "solo",
  isTwoPassScene: (sc) => String(sc?.mode || "").includes("duet_method_b"),
  isStereoDuetMode: (m) => String(m || "").includes("duet_method_a"),
  escapeHtml: (s) => String(s || ""),
  extractAnchorId: (s) => String(s || ""),
  markProjectDirty: () => {},
  redrawTimeline: () => {},
  getSceneOptionCatalog: () => ({ vRenderers: {}, fModels: {}, mmModels: {}, resOpts: [], upOpts: [], refOpts: [] }),
  buildAdvSummaryBadgesHtml: () => "",
});
ctx.window = ctx;
ctx.window.state = state;
vm.runInContext(fs.readFileSync("work/ui/static/js/scenes_tab.js", "utf8"), ctx);

ctx.renderSceneList(true);
const pills = tabRoot.querySelectorAll(".scenes-filter-pill");
assert.deepStrictEqual(
  pills.map((p) => p.textContent),
  ["All", "Needs Render (0)", "Duets", "Silent", "Locked (2)", "Unlocked (1)"],
);
assert.strictEqual(tabRoot.querySelectorAll(".scene-card").length, 3);

// Click "Locked (2)"
pills.find((p) => p.dataset.filterKey === "locked").onclick();
assert.deepStrictEqual(
  tabRoot.querySelectorAll(".scene-card").map((c) => c.dataset.sceneIdx),
  ["0", "2"],
);

// Unlock S01 and sync -> S01 drops out of Locked view and counts update
delete state.projectData.scenes[0].locked;
ctx.syncScenesTabCard(state.projectData.scenes[0]);
assert.deepStrictEqual(
  tabRoot.querySelectorAll(".scene-card").map((c) => c.dataset.sceneIdx),
  ["2"],
);
assert.strictEqual(tabRoot.querySelector('.scenes-filter-pill[data-filter-key="locked"]').textContent, "Locked (1)");
assert.strictEqual(tabRoot.querySelector('.scenes-filter-pill[data-filter-key="unlocked"]').textContent, "Unlocked (2)");

// Click "Unlocked (2)" -> shows S01 and S02
tabRoot.querySelector('.scenes-filter-pill[data-filter-key="unlocked"]').onclick();
assert.deepStrictEqual(
  tabRoot.querySelectorAll(".scene-card").map((c) => c.dataset.sceneIdx),
  ["0", "1"],
);
console.log("SCENES_LOCK_FILTER_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("SCENES_LOCK_FILTER_OK", res.stdout)

    def test_scenes_tab_click_jumps_timeline_and_repeat_loops_master(self):
        """Clicking a scene jumps timeline to it; Repeat loops that scene in Master Video."""
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function mkNode(tag = "div", id = "") {
  const listeners = {};
  const node = {
    id,
    tagName: tag.toUpperCase(),
    _className: "",
    _text: "",
    _innerHTML: "",
    dataset: {},
    style: {},
    attrs: {},
    children: [],
    parentNode: null,
    scrollTop: 0,
    scrollLeft: 0,
    clientWidth: 800,
    paused: true,
    ended: false,
    currentTime: 0,
    duration: 20,
    readyState: 1,
    playCount: 0,
    options: [],
    classList: {
      contains: (c) => node._className.split(/\s+/).includes(c),
      toggle: (c, force) => {
        const s = new Set(node._className.split(/\s+/).filter(Boolean));
        if (force === undefined ? !s.has(c) : force) s.add(c); else s.delete(c);
        node._className = [...s].join(" ");
      },
    },
    getContext() { return {}; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    appendChild(ch) { if (ch) { ch.parentNode = this; this.children.push(ch); } return ch; },
    replaceWith(rep) {
      if (!this.parentNode) return;
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) { rep.parentNode = this.parentNode; this.parentNode.children[i] = rep; }
    },
    add(o) { this.options.push(o); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click() { (listeners.click || []).forEach((fn) => fn({ target: this, stopPropagation() {} })); this.onclick?.({ target: this, stopPropagation() {} }); },
    play() { this.paused = false; this.playCount++; return Promise.resolve(); },
    pause() { this.paused = true; },
    closest(sel) {
      if (sel === "button, select, input, textarea, a, label" && ["BUTTON", "SELECT", "INPUT", "TEXTAREA", "A", "LABEL"].includes(this.tagName)) return this;
      return null;
    },
    querySelectorAll(sel) {
      const out = [];
      const visit = (n) => {
        for (const ch of (n.children || [])) {
          if (sel === ".scene-card" && ch.classList?.contains("scene-card")) out.push(ch);
          else if (sel === ".scene-card[data-scene-id]" && ch.classList?.contains("scene-card") && ch.dataset?.sceneId) out.push(ch);
          visit(ch);
        }
      };
      visit(this);
      return out;
    },
    querySelector(sel) {
      if (sel === ".scenes-list-container") return this._walk((n) => n.classList?.contains("scenes-list-container"));
      if (sel === ".scene-card-title-row") return this._walk((n) => n.classList?.contains("scene-card-title-row"));
      if (sel === "[data-action='repeat']") return this._walk((n) => n.dataset?.action === "repeat");
      return null;
    },
    _walk(pred) {
      for (const ch of (this.children || [])) {
        if (pred(ch)) return ch;
        const hit = ch._walk?.(pred);
        if (hit) return hit;
      }
      return null;
    },
    contains(target) { return this === target || (this.children || []).some((ch) => ch.contains?.(target)); },
  };
  Object.defineProperty(node, "className", { get: () => node._className, set: (v) => { node._className = String(v || ""); } });
  Object.defineProperty(node, "textContent", { get: () => node._text, set: (v) => { node._text = String(v || ""); } });
  Object.defineProperty(node, "innerHTML", { get: () => node._innerHTML, set: (v) => { node._innerHTML = String(v || ""); if (!v) node.children = []; } });
  return node;
}

const byId = {
  audioPlayer: mkNode("audio", "audioPlayer"),
  timelineCanvas: mkNode("canvas", "timelineCanvas"),
  timelinePlayhead: mkNode("div", "timelinePlayhead"),
  timelineScrollContainer: mkNode("div", "timelineScrollContainer"),
  timecodeDisplay: mkNode("div", "timecodeDisplay"),
  toastMessage: mkNode("div", "toastMessage"),
  "tab-scenes": mkNode("div", "tab-scenes"),
  "tab-master": mkNode("div", "tab-master"),
  masterLoopBadge: mkNode("span", "masterLoopBadge"),
  masterLoopText: mkNode("span", "masterLoopText"),
  btnClearMasterLoop: mkNode("button", "btnClearMasterLoop"),
  masterVideoPlayer: mkNode("video", "masterVideoPlayer"),
  clipVideoPlayer: mkNode("video", "clipVideoPlayer"),
};
byId["tab-scenes"].className = "active";
byId.masterVideoPlayer.setAttribute("src", "/projects/all_or_nothing/final_music_video.mp4");

const seeks = [];
const switchedTabs = [];
const selectedIdxs = [];
const rootBody = mkNode("body", "body");
Object.values(byId).forEach((el) => rootBody.appendChild(el));

const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN, isFinite, Promise, setTimeout, clearTimeout,
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  document: {
    readyState: "complete",
    activeElement: null,
    body: rootBody,
    getElementById: (id) => byId[id] || mkNode("div", id),
    createElement: (tag) => mkNode(tag),
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
    querySelectorAll: (sel) => rootBody.querySelectorAll(sel),
    addEventListener() {},
  },
  Option: function (lbl, val) { return { text: lbl, value: val, dataset: {} }; },
  window: {},
  addEventListener: () => {},
  seekTimeline: (t, ...rest) => seeks.push(t),
  selectScene: (idx) => { selectedIdxs.push(idx); ctx.state.selectedSceneIdx = idx; },
  switchTab: (tab) => {
    switchedTabs.push(tab);
    byId["tab-scenes"].classList.toggle("active", tab === "scenes");
    byId["tab-master"].classList.toggle("active", tab === "master");
  },
  canonicalizeSceneMode: (m) => m || "solo",
  isTwoPassScene: (sc) => String(sc?.mode || "").includes("duet_method_b"),
  isStereoDuetMode: (m) => String(m || "").includes("duet_method_a"),
  escapeHtml: (s) => String(s || ""),
  markProjectDirty: () => {},
  redrawTimeline: () => {},
  getSceneOptionCatalog: () => ({ vRenderers: {}, fModels: {}, mmModels: {}, resOpts: [], upOpts: [], refOpts: [] }),
  buildAdvSummaryBadgesHtml: () => "",
});
ctx.window = ctx;
vm.runInContext(fs.readFileSync("work/ui/static/js/state_and_utils.js", "utf8"), ctx);
ctx.state.projectData = {
  project: { fps: 24 },
  scenes: [
    { id: "S01", start: 0, end: 5, mode: "solo", anchor: "A01" },
    { id: "S02", start: 10, end: 14, mode: "solo", anchor: "A02" },
  ],
};
ctx.window.state = ctx.state;
vm.runInContext(fs.readFileSync("work/ui/static/js/scenes_tab.js", "utf8"), ctx);

ctx.renderSceneList(true);
const cards = byId["tab-scenes"].querySelectorAll(".scene-card");
assert.strictEqual(cards.length, 2);

// 1. Clicking scene 1 card jumps timeline to S02 (start=10)
cards[1].onclick({ target: cards[1], stopPropagation() {} });
assert.strictEqual(selectedIdxs[selectedIdxs.length - 1], 1);
assert.ok(seeks.length >= 1 && seeks[seeks.length - 1] >= 10 && seeks[seeks.length - 1] < 14);
assert.ok(byId.timelineScrollContainer.scrollLeft > 0);

// 2. Clicking Repeat on S02 activates master tab, seeks masterVideoPlayer, and loops inside S02
const repBtn = cards[1].querySelector("[data-action='repeat']");
assert.ok(repBtn, "Expected Repeat button on scene card");
repBtn.onclick({ target: repBtn, stopPropagation() {} });
assert.strictEqual(ctx.state.masterLoopSceneId, "S02");
assert.strictEqual(switchedTabs[switchedTabs.length - 1], "master");
assert.strictEqual(byId.masterVideoPlayer.currentTime, 10);
assert.strictEqual(byId.masterVideoPlayer.playCount, 1);
assert.strictEqual(byId.masterLoopBadge.style.display, "inline-flex");
assert.ok(byId.masterLoopText.textContent.includes("S02"));
assert.strictEqual(repBtn.textContent, "🔁 Looping");

// 3. enforceMasterSceneLoop wraps currentTime >= 13.97 back to 10
byId.masterVideoPlayer.currentTime = 13.98;
assert.strictEqual(ctx.enforceMasterSceneLoop(byId.masterVideoPlayer), true);
assert.strictEqual(byId.masterVideoPlayer.currentTime, 10);

// 4. Clearing loop via clear button restores state
byId.btnClearMasterLoop.click();
assert.strictEqual(ctx.state.masterLoopSceneId, null);
assert.strictEqual(byId.masterLoopBadge.style.display, "none");
assert.strictEqual(repBtn.textContent, "🔁 Repeat");
console.log("SCENES_TIMELINE_JUMP_AND_REPEAT_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("SCENES_TIMELINE_JUMP_AND_REPEAT_OK", res.stdout)

    def test_timeline_scene_jump_bar_proportional_lights_and_repeats(self):
        """Proportional full-width scene jump bar above timeline jumps, highlights selected scene, and repeats."""
        index_html = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="sceneJumpBar"', index_html)
        self.assertLess(index_html.index('id="sceneJumpBar"'), index_html.index('id="timelineScrollContainer"'))
        self.assertIn('js/scene_jump_bar.js', index_html)

        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function mkNode(tag = "div", id = "") {
  const listeners = {};
  const node = {
    id,
    tagName: tag.toUpperCase(),
    _className: "",
    _text: "",
    _innerHTML: "",
    dataset: {},
    style: {},
    attrs: {},
    children: [],
    parentNode: null,
    scrollLeft: 0,
    clientWidth: 800,
    paused: true,
    ended: false,
    currentTime: 0,
    duration: 20,
    readyState: 1,
    playCount: 0,
    classList: {
      contains: (c) => node._className.split(/\s+/).includes(c),
      toggle: (c, force) => {
        const s = new Set(node._className.split(/\s+/).filter(Boolean));
        if (force === undefined ? !s.has(c) : force) s.add(c); else s.delete(c);
        node._className = [...s].join(" ");
      },
    },
    getContext() { return {}; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    appendChild(ch) { if (ch) { ch.parentNode = this; this.children.push(ch); } return ch; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click() { (listeners.click || []).forEach((fn) => fn({ target: this, stopPropagation() {} })); this.onclick?.({ target: this, stopPropagation() {} }); },
    play() { this.paused = false; this.playCount++; return Promise.resolve(); },
    pause() { this.paused = true; },
    closest(sel) {
      if (sel === ".scene-jump-repeat" && this.classList.contains("scene-jump-repeat")) return this;
      return null;
    },
    querySelectorAll(sel) {
      const out = [];
      const cls = sel.startsWith(".") ? sel.slice(1).split("[")[0] : "";
      const visit = (n) => {
        for (const ch of (n.children || [])) {
          if (cls && ch.classList?.contains(cls)) out.push(ch);
          visit(ch);
        }
      };
      visit(this);
      return out;
    },
    querySelector(sel) {
      if (sel === ".scene-jump-repeat") return this._walk((n) => n.classList?.contains("scene-jump-repeat"));
      return null;
    },
    _walk(pred) {
      for (const ch of (this.children || [])) {
        if (pred(ch)) return ch;
        const hit = ch._walk?.(pred);
        if (hit) return hit;
      }
      return null;
    },
  };
  Object.defineProperty(node, "className", { get: () => node._className, set: (v) => { node._className = String(v || ""); } });
  Object.defineProperty(node, "textContent", { get: () => node._text, set: (v) => { node._text = String(v || ""); } });
  Object.defineProperty(node, "innerHTML", { get: () => node._innerHTML, set: (v) => { node._innerHTML = String(v || ""); if (!v) node.children = []; } });
  return node;
}

const byId = {
  sceneJumpBar: mkNode("div", "sceneJumpBar"),
  audioPlayer: mkNode("audio", "audioPlayer"),
  timelineCanvas: mkNode("canvas", "timelineCanvas"),
  timelinePlayhead: mkNode("div", "timelinePlayhead"),
  timelineScrollContainer: mkNode("div", "timelineScrollContainer"),
  timecodeDisplay: mkNode("div", "timecodeDisplay"),
  toastMessage: mkNode("div", "toastMessage"),
  "tab-scenes": mkNode("div", "tab-scenes"),
  "tab-master": mkNode("div", "tab-master"),
  masterLoopBadge: mkNode("span", "masterLoopBadge"),
  masterLoopText: mkNode("span", "masterLoopText"),
  btnClearMasterLoop: mkNode("button", "btnClearMasterLoop"),
  masterVideoPlayer: mkNode("video", "masterVideoPlayer"),
  clipVideoPlayer: mkNode("video", "clipVideoPlayer"),
};
byId.masterVideoPlayer.setAttribute("src", "/projects/all_or_nothing/final_music_video.mp4");

const seeks = [];
const switchedTabs = [];
const rootBody = mkNode("body", "body");
Object.values(byId).forEach((el) => rootBody.appendChild(el));

const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN, isFinite, Promise, setTimeout, clearTimeout,
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  document: {
    readyState: "complete",
    activeElement: null,
    body: rootBody,
    getElementById: (id) => byId[id] || mkNode("div", id),
    createElement: (tag) => mkNode(tag),
    querySelectorAll: (sel) => rootBody.querySelectorAll(sel),
    addEventListener() {},
  },
  window: {},
  addEventListener: () => {},
  seekTimeline: (t) => seeks.push(t),
  selectScene: (idx) => { ctx.state.selectedSceneIdx = idx; ctx.redrawTimeline(); },
  redrawTimeline: () => { ctx.renderSceneJumpBar(); },
  switchTab: (tab) => {
    switchedTabs.push(tab);
    byId["tab-master"].classList.toggle("active", tab === "master");
  },
});
ctx.window = ctx;
vm.runInContext(fs.readFileSync("work/ui/static/js/state_and_utils.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/scene_jump_bar.js", "utf8"), ctx);

ctx.state.projectData = {
  project: { fps: 24 },
  scenes: [
    { id: "S01", start: 0, end: 2, mode: "solo" },
    { id: "S02", start: 2, end: 8, mode: "duet_method_b" },
    { id: "S03", start: 8, end: 16, mode: "silent_locked" },
  ],
};
ctx.renderSceneJumpBar();

const btns = byId.sceneJumpBar.querySelectorAll(".scene-jump-btn");
assert.strictEqual(btns.length, 3);
assert.strictEqual(btns[0].dataset.widthPct, "12.5000");
assert.strictEqual(btns[1].dataset.widthPct, "37.5000");
assert.strictEqual(btns[2].dataset.widthPct, "50.0000");
assert.strictEqual(btns[0].style.flex, "2 1 0%");
assert.strictEqual(btns[1].style.flex, "6 1 0%");
assert.strictEqual(btns[2].style.flex, "8 1 0%");

// Clicking scene button 1 jumps timeline to S02 and lights it up
btns[1].onclick({ target: btns[1], stopPropagation() {} });
assert.strictEqual(ctx.state.selectedSceneIdx, 1);
assert.ok(seeks.length >= 1 && seeks[seeks.length - 1] >= 2 && seeks[seeks.length - 1] < 8);
assert.strictEqual(btns[0].classList.contains("is-selected"), false);
assert.strictEqual(btns[1].classList.contains("is-selected"), true);
assert.strictEqual(btns[2].classList.contains("is-selected"), false);

// Selecting another scene via timeline selectScene lights up that button instead
ctx.selectScene(2);
assert.strictEqual(btns[1].classList.contains("is-selected"), false);
assert.strictEqual(btns[2].classList.contains("is-selected"), true);

// Repeat button uses a glyph (no word "Repeat") and triggers master loop
const rep0 = btns[0].querySelector(".scene-jump-repeat");
assert.ok(rep0 && rep0.innerHTML.includes("<svg") && !rep0.textContent.includes("Repeat"));
rep0.onclick({ target: rep0, stopPropagation() {} });
assert.strictEqual(ctx.state.masterLoopSceneId, "S01");
assert.strictEqual(switchedTabs[switchedTabs.length - 1], "master");
assert.strictEqual(btns[0].classList.contains("is-looping"), true);
assert.strictEqual(rep0.classList.contains("is-looping"), true);
console.log("SCENE_JUMP_BAR_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("SCENE_JUMP_BAR_OK", res.stdout)


class RenderTimeEstimationRegressionTests(unittest.TestCase):
    def test_backend_roi_model_isolation_and_cloud_eta_accuracy(self):
        """Cloud & ROI Pass 2 takes must never drag full-frame local 1080p calibration down."""
        from pipeline.media_utils import (
            attach_queue_time_estimates,
            compute_calibration_factor,
            estimate_cloud_pass_render_time,
            estimate_scene_render_time,
        )

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            (proj / "work").mkdir(parents=True, exist_ok=True)
            takes = [
                # Fast cloud MiniMax 1920x1088 runs (~280s)
                {"backend": "minimax", "resolution": "1920x1088", "frames": 175, "elapsed_s": 280.0, "created_at": 1000 + i}
                for i in range(6)
            ] + [
                # Fast local ROI Pass 2 tight_performer runs (~450s) tagged 1920x1088 in manifest
                {"backend": "local", "pass_num": 2, "resolution": "1920x1088", "frames": 175, "elapsed_s": 450.0, "model_id": "1",
                 "scene_snapshot": {"pass2_roi_mode": "tight_performer", "pass2_roi_side": "right", "split_x": 0.5}, "created_at": 2000 + i}
                for i in range(4)
            ] + [
                # True full-frame local 1920x1088 model 9 runs (~4000s)
                {"backend": "local", "pass_num": 2, "resolution": "1920x1088", "frames": 175, "elapsed_s": 4200.0, "model_id": "9",
                 "scene_snapshot": {"pass2_roi_mode": "full"}, "created_at": 3000 + i}
                for i in range(4)
            ]
            (proj / "work" / "takes_manifest.json").write_text(json.dumps({"scenes": {"S16": {"takes": takes}}}), encoding="utf-8")

            alpha_full_m9 = compute_calibration_factor(proj, resolution="1920x1088", backend="local", model_id="9")
            alpha_roi = compute_calibration_factor(proj, resolution="896x768", backend="local", model_id="1")
            alpha_mm = compute_calibration_factor(proj, resolution="1920x1088", backend="minimax")

            # Must remain near 1.0 instead of being crushed to 0.5 by MiniMax/ROI takes
            self.assertGreater(alpha_full_m9, 0.75)
            self.assertGreater(alpha_roi, 0.85)
            self.assertGreater(alpha_mm, 0.8)

            sc_full = {"id": "S16", "start": 0.0, "end": 7.0, "mode": "two_pass_gva", "renderer": "9", "resolution": "1920x1088"}
            sc_roi = {"id": "S20", "start": 0.0, "end": 7.0, "mode": "two_pass_gva", "renderer": "1", "resolution": "1920x1088", "pass2_roi_mode": "tight_performer"}
            est_full = estimate_scene_render_time(sc_full, resolution="1920x1088", calibration_alpha=alpha_full_m9, backend="local")
            est_roi = estimate_scene_render_time(sc_roi, resolution="1920x1088", calibration_alpha=alpha_roi, backend="local")
            self.assertGreater(est_full["passes"][1]["estimated_seconds"], 3000.0)
            self.assertLess(est_roi["passes"][1]["estimated_seconds"], 900.0)

            # Multi-chunk cloud estimate must not multiply duration by chunks twice
            est_1c = estimate_cloud_pass_render_time(7.5, resolution="1920x1088", auto_chunks=1)
            est_3c = estimate_cloud_pass_render_time(22.5, resolution="1920x1088", auto_chunks=3)
            self.assertAlmostEqual(est_3c, est_1c * 3.0, delta=5.0)

            q = [
                {"scene_id": "S16", "pass": 2, "backend": "local", "resolution": "1920x1088", "filename": "S16_Pass2.mp4", "output_filename": "S16_Pass2", "status": "rendering", "pct": 100.0},
                {"scene_id": "S20", "pass": 2, "backend": "minimax", "resolution": "1920x1088", "filename": "S20_Pass2.mp4", "output_filename": "S20_Pass2", "status": "queued", "duration": 7.0},
            ]
            attach_queue_time_estimates(q, {"S16": sc_full, "S20": sc_roi}, proj_dir=proj, default_res="1920x1088", live_eng={"filename": "S16_Pass2.mp4", "state": "completed", "pct": 100.0})
            self.assertEqual(q[0]["estimated_remaining_seconds"], 0.0)
            self.assertTrue(200.0 <= q[1]["estimated_seconds"] <= 400.0)

    def test_10_parallel_anchor_hash_preservation_and_no_false_requeue(self):
        """Parallel save_state_hashes must not overwrite newer anchor hashes; vault Pass1 promotes stay <= Pass2."""
        import os
        from pipeline.context import ProjectContext
        from pipeline.media_utils import _file_sha256
        from pipeline.stage2_hashes import compute_project_task_hashes
        from pipeline.takes import try_auto_promote_vault_hit

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg = _make_mini_project(root, "false_q_proj")
            data = json.loads(cfg.read_text(encoding="utf-8"))
            data["scenes"] = [
                {"id": "S01", "start": 0.0, "end": 4.0, "mode": "silent_locked", "anchor": "A01", "prompt": "Test", "seed": 1},
                {"id": "S02", "start": 4.0, "end": 8.0, "mode": "duet_method_b", "anchor": "A02", "prompt_m": "M", "prompt_f": "F", "seed_m": 1, "seed_f": 2, "stem_m": "male", "stem_f": "female"},
            ]
            cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
            for old_a in (pdir / "work" / "anchors").glob("*"): old_a.unlink(missing_ok=True)
            a1 = pdir / "work" / "anchors" / "A01.jpg"
            a2 = pdir / "work" / "anchors" / "A02.jpg"
            a1.write_bytes(b"new_a1_bytes")
            a2.write_bytes(b"a2_bytes")
            now = time.time()
            os.utime(str(a1), (now - 100.0, now - 100.0))
            os.utime(str(a2), (now - 100.0, now - 100.0))

            ctx = ProjectContext(cfg)
            st_stale = {"anchor_file_hashes": {"A01": "old_a01_hash", "A02": _file_sha256(a2)}, "tasks": {}, "task_hashes": {}}
            st_fresh = {"anchor_file_hashes": {"A01": _file_sha256(a1), "A02": _file_sha256(a2)}, "tasks": {}, "task_hashes": {}}
            ctx.save_state_hashes(st_fresh)
            # Parallel save scoped to S02 / A02 must NOT clobber A01's fresh hash
            ctx.save_state_hashes(st_stale, updated_keys={"S02_Pass1_Left_832x480"}, updated_anchor_ids={"A02"})
            after_sh = ctx.load_state_hashes()
            self.assertEqual(after_sh["anchor_file_hashes"]["A01"], _file_sha256(a1))

            # Even if A01 stored hash WERE stale while S01 active take finished after A01 mtime, no false ANCHOR A01 EDITED
            after_sh["anchor_file_hashes"]["A01"] = "old_a01_hash"
            h_map = compute_project_task_hashes(ctx)
            h_s1 = h_map["S01_silent_832x480"][0]
            h_p1 = h_map["S02_Pass1_Left_832x480"][0]
            h_p2 = h_map["S02_Pass2_Final_832x480"][0]
            after_sh["tasks"] = {"S01_silent_832x480": h_s1, "S02_Pass1_Left_832x480": h_p1, "S02_Pass2_Final_832x480": h_p2}
            after_sh["task_hashes"] = dict(after_sh["tasks"])
            ctx.save_state_hashes(after_sh)

            rdir = pdir / "work" / "renders"
            rdir.mkdir(parents=True, exist_ok=True)
            for fn in ("S01_silent_832x480.mp4", "S02_Pass1_Left_832x480.mp4", "S02_Pass2_Final_832x480.mp4"):
                p = rdir / fn
                p.write_bytes(b"MP4" * 20000)
                os.utime(str(p), (now - 10.0, now - 10.0))

            v_p1 = rdir / "takes" / "S02" / "S02_Pass1_Left_832x480__p1.mp4"
            v_p1.parent.mkdir(parents=True, exist_ok=True)
            v_p1.write_bytes(b"MP4" * 20000)
            manifest = {
                "scenes": {
                    "S01": {"active_takes": {"default": "t_s1"}, "takes": [{"take_id": "t_s1", "out_name": "S01_silent_832x480", "task_hash": h_s1, "created_at": now - 10.0}]},
                    "S02": {"active_takes": {"p1": "t_p1", "p2": "t_p2"}, "takes": [
                        {"take_id": "t_p1", "out_name": "S02_Pass1_Left_832x480", "pass_num": 1, "task_hash": h_p1, "vault_rel_mp4": "takes/S02/S02_Pass1_Left_832x480__p1.mp4", "created_at": now - 50.0, "render_start": 4.0, "render_end": 8.0},
                        {"take_id": "t_p2", "out_name": "S02_Pass2_Final_832x480", "pass_num": 2, "task_hash": h_p2, "created_at": now - 10.0, "render_start": 4.0, "render_end": 8.0},
                    ]},
                }
            }
            (pdir / "work" / "takes_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            # Re-promoting old Pass 1 must keep Pass 1 mtime <= Pass 2 mtime so Pass 2 isn't falsely queued
            self.assertTrue(try_auto_promote_vault_hit(pdir, "S02", 1, "S02_Pass1_Left_832x480", (h_p1,), 4.0, 8.0))
            self.assertLessEqual((rdir / "S02_Pass1_Left_832x480.mp4").stat().st_mtime, (rdir / "S02_Pass2_Final_832x480.mp4").stat().st_mtime)

            orig_pdir = server.PROJECTS_DIR
            server.PROJECTS_DIR = root
            try:
                q = server.compute_fast_project_queue("false_q_proj", force_recheck=True)
                self.assertEqual([x["filename"] for x in q if x["status"] != "completed"], [])
            finally:
                server.PROJECTS_DIR = orig_pdir

    def test_11_cancel_single_engine_clears_stale_active_sse_status(self):
        """Cancelling MiniMax task while Local GPU stays running must clear sseTaskStatuses so Delete appears."""
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const els = new Map();
const mkEl = (id) => {
  const el = { id, textContent: "", innerHTML: "", value: "", style: {}, dataset: {}, children: [], classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } }, addEventListener() {}, querySelectorAll() { return []; } };
  if (id) els.set(id, el);
  return el;
};
const tbody = mkEl("queueTableBody");
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN, Promise, setTimeout, clearTimeout,
  setInterval: () => 1, clearInterval: () => {},
  EventSource: class { close() {} }, URLSearchParams: class { get() { return null; } },
  document: { getElementById: (id) => els.get(id) || mkEl(id), querySelectorAll: () => [], querySelector: () => null, createElement: () => mkEl("") },
  window: {}, location: { search: "" },
  state: { currentSlug: "all_or_nothing", queueFilter: "queued", renderQueue: [], sseTaskStatuses: new Map(), engineLive: {}, projectData: { scenes: [] } },
  fetch: async () => ({ json: async () => ({ success: true }) }),
  showToast() {}, refreshProjectAssets() {}, formatDurationEstimate: (s) => `${Math.round(s)}s`, escapeHtml: (s) => String(s || ""),
});
ctx.window = ctx;
ctx.window.state = ctx.state;
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

// Both Local S15 and MiniMax S10a start rendering
ctx.handleQueueSSEEvent('[QUEUE_ACTIVE] ' + JSON.stringify({ idx: 1, filename: 'S15_Pass2_Final_1920x1088.mp4', backend: 'local' }));
ctx.handleQueueSSEEvent('[QUEUE_ACTIVE] ' + JSON.stringify({ idx: 2, filename: 'S10a_silent_1920x1088.mp4', backend: 'minimax' }));
assert.strictEqual(ctx.state.sseTaskStatuses.get('S10a_silent_1920x1088.mp4').status, 'rendering');

// Cancel S10a on MiniMax while S15 Local remains rendering
ctx.handleQueueSSEEvent('[QUEUE_CANCEL] ' + JSON.stringify({ idx: 2, filename: 'S10a_silent_1920x1088.mp4', backend: 'minimax', reason: 'STOPPED BY USER' }));
assert.strictEqual(ctx.state.sseTaskStatuses.has('S10a_silent_1920x1088.mp4'), false);

// Even if sseTaskStatuses somehow had a stale entry, authoritative st.engines purges it during reconcileQueueStatuses
ctx.state.sseTaskStatuses.set('S10a_silent_1920x1088.mp4', { status: 'rendering' });
const q = [
  { filename: 'S15_Pass2_Final_1920x1088.mp4', output_filename: 'S15_Pass2_Final_1920x1088', status: 'rendering', backend: 'local', needs_render: true },
  { filename: 'S10a_silent_1920x1088.mp4', output_filename: 'S10a_silent_1920x1088', status: 'queued', backend: 'minimax', needs_render: true },
];
const st = { running: true, engines: { local: { running: true, active_tasks: [{ filename: 'S15_Pass2_Final_1920x1088.mp4' }] }, minimax: { running: false, active_tasks: [] } } };
ctx.reconcileQueueStatuses(q, st, null, true);
assert.strictEqual(q[0].status, 'rendering');
assert.strictEqual(q[1].status, 'queued');
assert.strictEqual(ctx.state.sseTaskStatuses.has('S10a_silent_1920x1088.mp4'), false);

ctx.state.renderQueue = q;
ctx.renderQueueUI();
assert.ok(tbody.innerHTML.includes('btn-queue-del" data-id="S10a_silent_1920x1088.mp4"'));
console.log('CANCEL_MULTI_ENGINE_OK');
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("CANCEL_MULTI_ENGINE_OK", res.stdout)

    def test_28_render_queue_tab_defaults_filters_and_readonly_engine(self):
        """Render Queue rename, Pending->Done->All order/default, no Skipped filter,
        fal/minimax unchecked by default, and read-only engine cells in All/Done views."""
        from ui import server, server_engines

        html = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("🚀 Render Queue</button>", html)
        self.assertNotIn("🚀 Render Console</button>", html)
        self.assertNotIn("btnFilterSkipped", html)
        self.assertNotIn("countSkipped", html)

        idx_q = html.index('id="btnFilterQueued"')
        idx_d = html.index('id="btnFilterDone"')
        idx_a = html.index('id="btnFilterAll"')
        self.assertLess(idx_q, idx_d)
        self.assertLess(idx_d, idx_a)
        q_btn_line = [l for l in html.splitlines() if 'id="btnFilterQueued"' in l][0]
        a_btn_line = [l for l in html.splitlines() if 'id="btnFilterAll"' in l][0]
        self.assertIn("btn-filter active", q_btn_line)
        self.assertNotIn("btn-filter active", a_btn_line)

        local_chk = [l for l in html.splitlines() if 'data-engine="local"' in l and "chk-engine-run" in l][0]
        fal_chk = [l for l in html.splitlines() if 'data-engine="fal"' in l and "chk-engine-run" in l][0]
        mm_chk = [l for l in html.splitlines() if 'data-engine="minimax"' in l and "chk-engine-run" in l][0]
        self.assertIn(" checked>", local_chk)
        self.assertNotIn(" checked", fal_chk)
        self.assertNotIn(" checked", mm_chk)

        # Fresh server state should have local enabled and cloud engines disabled by default
        class DummySrv:
            render_lock = __import__("threading").RLock()
            render_status = {}
            log_history = []
        ds = DummySrv()
        server_engines.ensure_engine_state(ds)
        self.assertEqual(ds.enabled_engines, {"local": True, "fal": False, "minimax": False})

        # Verify state_and_utils.js default queueFilter + queue_engines.js read-only All/Done behavior
        state_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "state_and_utils.js").read_text(encoding="utf-8")
        self.assertIn('queueFilter: "queued"', state_js)

        node_script = """
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const noopEl = () => ({ style: {}, dataset: {}, children: [], classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {}, appendChild() {}, innerHTML: '', textContent: '' });
const ctx = vm.createContext({
  console, Date, Math, JSON, Map, Set,
  location: { search: "" },
  URLSearchParams: class { get() { return null; } },
  EventSource: class { close() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  document: {
    _els: {},
    getElementById(id) { return this._els[id] || (this._els[id] = noopEl()); },
    querySelectorAll: () => [],
    createElement: noopEl,
  },
  state: { currentSlug: "proj", queueFilter: "queued", renderQueue: [], sseTaskStatuses: new Map() },
  showToast() {}, escapeHtml: (s) => String(s || ""),
});
ctx.window = ctx;
ctx.window.state = ctx.state;
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

assert.strictEqual(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8").includes("RENDERING TASK ["), false);
assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.getEnabledEnginesMap())), { local: true, fal: false, minimax: false });
const qItem = { filename: "S01_solo.mp4", status: "queued", backend: "fal", resolved_default_backend: "local", backend_override: "fal" };
ctx.state.queueFilter = "queued";
assert.ok(ctx.renderQueueEngineCell(qItem).includes('class="queue-engine-select'));
ctx.state.queueFilter = "all";
const allCell = ctx.renderQueueEngineCell(qItem);
assert.ok(!allCell.includes('<select') && allCell.includes('fal.ai'));
ctx.state.queueFilter = "completed";
const doneCell = ctx.renderQueueEngineCell({ ...qItem, status: "completed" });
assert.ok(!doneCell.includes('<select') && doneCell.includes('fal.ai'));

// Active lane hides redundant hero header/title/timer/details; queue table renders Model column
ctx.state.queueFilter = "queued";
ctx.state.renderQueue = [{ idx: 9, filename: "S15_Pass2.mp4", output_filename: "S15_Pass2", status: "rendering", backend: "local", model_type: "minimax_h3_fl2va_pruned_pdd", video_length: 175, resolution: "1920x1088", reason: "FORCED" }];
ctx.state.activeRenderTask = { idx: 9, total: 44, filename: "S15_Pass2.mp4", backend: "local", model: "minimax_h3_fl2va_pruned_pdd", frames: 175, resolution: "1920x1088", reason: "FORCED" };
ctx.handleMultiEngineSSEEvent("[QUEUE_ACTIVE]", { filename: "S15_Pass2.mp4", backend: "local", started_at: Date.now() / 1000 });
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("activeTaskHeaderRow").style.display, "none");
assert.strictEqual(ctx.document.getElementById("activeTaskTitle").style.display, "none");
assert.strictEqual(ctx.document.getElementById("activeTaskTimer").style.display, "none");
assert.strictEqual(ctx.document.getElementById("activeTaskDetails").style.display, "none");
assert.ok(ctx.document.getElementById("queueTableHead").innerHTML.includes(">Model</th>"));
assert.ok(ctx.document.getElementById("queueTableBody").innerHTML.includes("minimax_h3_fl2va_pruned_pdd"));

// Failed tasks remain visible in default Pending (queued) tab with Retry + Delete controls
ctx.state.activeRenderTask = null;
ctx.handleMultiEngineSSEEvent("[QUEUE_CANCEL]", { filename: "S15_Pass2.mp4", backend: "local" });
ctx.state.renderQueue = [{ idx: 9, filename: "S15_Pass2.mp4", output_filename: "S15_Pass2", status: "failed", backend: "local", model_type: "minimax_h3_fl2va_pruned_pdd", reason: "CUDA OOM", error: "CUDA error: out of memory", needs_render: true }];
ctx.renderQueueUI();
const fBody = ctx.document.getElementById("queueTableBody").innerHTML;
assert.ok(fBody.includes("S15_Pass2.mp4") && fBody.includes("❌ Failed") && fBody.includes("CUDA OOM") && fBody.includes("btn-queue-add") && fBody.includes("btn-queue-del"));
assert.ok(!ctx.document.getElementById("activeTaskTitle").textContent.includes("All files up-to-date"));
assert.strictEqual(ctx.document.getElementById("countQueued").textContent, 1);

// Only one local task can stay 'rendering' across SSE transition and /api/render/status poll
ctx.state.queueFilter = "queued";
ctx.state.renderQueue = [
  { idx: 1, scene_id: "S15", pass: 1, filename: "S15_Pass1.mp4", output_filename: "S15_Pass1", status: "rendering", backend: "local", estimated_remaining_str: "1.8m" },
  { idx: 2, scene_id: "S15", pass: 2, filename: "S15_Pass2.mp4", output_filename: "S15_Pass2", status: "rendering", backend: "local", estimated_remaining_str: "24m" },
];
ctx.reconcileSseActiveTask(ctx.state.renderQueue[1], "local");
assert.strictEqual(ctx.state.renderQueue[0].status, "completed");
assert.strictEqual(ctx.state.renderQueue[0].estimated_remaining_str, undefined);
assert.strictEqual(ctx.state.renderQueue[1].status, "rendering");

ctx.state.renderQueue[0].status = "rendering";
ctx.state.renderQueue[0].estimated_remaining_str = "1.8m";
ctx.syncRenderStatusWithQueue({
  running: true,
  queue_worker_active: true,
  engines: {
    local: { running: true, active_tasks: [{ filename: "S15_Pass2.mp4", engine_status: { state: "rendering", filename: "S15_Pass2.mp4" } }] },
    fal: { running: false, active_tasks: [] },
    minimax: { running: false, active_tasks: [] },
  },
});
assert.notStrictEqual(ctx.state.renderQueue[0].status, "rendering");
assert.strictEqual(ctx.state.renderQueue[0].estimated_remaining_str, undefined);
assert.strictEqual(ctx.state.renderQueue[1].status, "rendering");
console.log("QUEUE_UI_RULES_OK");
"""
        res2 = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0, f"Node VM failed:\nSTDOUT:{res2.stdout}\nSTDERR:{res2.stderr}")
        self.assertIn("QUEUE_UI_RULES_OK", res2.stdout)

        # Backend dedupe keeps <= 1 local rendering task while preserving parallel cloud tasks
        from ui import server_engine_io
        b_items = [
            {"idx": 1, "scene_id": "S15", "pass": 1, "output_filename": "S15_Pass1_Left_1920x1088", "backend": "local", "status": "rendering", "needs_render": True, "started_at": 100.0, "estimated_remaining_str": "1.8m"},
            {"idx": 2, "scene_id": "S15", "pass": 2, "output_filename": "S15_Pass2_Final_1920x1088", "backend": "local", "status": "rendering", "needs_render": True, "started_at": 200.0, "estimated_remaining_str": "24m"},
            {"idx": 3, "scene_id": "S18b", "pass": 1, "output_filename": "S18b_Pass1_Left_1920x1088", "backend": "minimax", "status": "rendering", "needs_render": True, "started_at": 205.0, "estimated_remaining_str": "5.1m"},
        ]
        server_engine_io.dedupe_rendering_queue_items(b_items, keep_stem="S15_Pass2_Final_1920x1088")
        self.assertEqual(b_items[0]["status"], "completed")
        self.assertNotIn("estimated_remaining_str", b_items[0])
        self.assertEqual(b_items[1]["status"], "rendering")
        self.assertEqual(b_items[1]["estimated_remaining_str"], "24m")
        self.assertEqual(b_items[2]["status"], "rendering")

        act_map = server_engine_io.collect_active_rendering_map(
            running_stems=set(),
            local_slots=["S15_Pass2_Final_1920x1088"],
            q_active=False,
            per_tasks={
                ("local", "S15_Pass1_Left_1920x1088"): {"state": "rendering", "backend": "local", "filename": "S15_Pass1_Left_1920x1088.mp4", "updated_at": 9999999999.0},
                ("local", "S15_Pass2_Final_1920x1088"): {"state": "rendering", "backend": "local", "filename": "S15_Pass2_Final_1920x1088.mp4", "updated_at": 9999999999.0},
                ("minimax", "S18b_Pass1_Left_1920x1088"): {"state": "rendering", "backend": "minimax", "filename": "S18b_Pass1_Left_1920x1088.mp4", "updated_at": 9999999999.0},
            },
            per_eng={"local": {"state": "rendering", "backend": "local", "filename": "S15_Pass1_Left_1920x1088.mp4", "updated_at": 9999999999.0}},
            legacy_eng=None,
            slots_items=[],
        )
        self.assertNotIn("S15_Pass1_Left_1920x1088", act_map)
        self.assertIn("S15_Pass2_Final_1920x1088", act_map)
        self.assertIn("S18b_Pass1_Left_1920x1088", act_map)


class WorkerPipeEncodingRegressionTests(unittest.TestCase):
    """Ensure execute_single_render decodes UTF-8 tqdm / arbitrary bytes cleanly on Windows without false -9 exit."""

    def test_01_execute_single_render_utf8_tqdm_and_invalid_bytes_no_exit_minus9(self):
        env_map = server_workers._subprocess_env()
        self.assertEqual(env_map.get("PYTHONUTF8"), "1")
        self.assertEqual(env_map.get("PYTHONIOENCODING"), "utf-8:replace")

        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            dummy_runner = tmp_root / "emit_tqdm.py"
            dummy_runner.write_text(
                "import sys\n"
                "sys.stderr.buffer.write('Encoding Text Prompt:  24%|\\u2588\\u2588\\u258d       | 12/50\\n'.encode('utf-8'))\n"
                "sys.stderr.buffer.write(b'raw_bad_bytes:\\x8d\\x9d\\xff\\n')\n"
                "sys.stderr.flush()\n"
                "print('DONE_OK', flush=True)\n",
                encoding="utf-8",
            )
            proj_dir = tmp_root / "projects" / "demo" / "work" / "jobs"
            proj_dir.mkdir(parents=True, exist_ok=True)
            logs: list[str] = []
            with mock.patch.object(server, "RUNNER_SCRIPT", dummy_runner), \
                 mock.patch.object(server, "PROJECTS_DIR", tmp_root / "projects"), \
                 mock.patch.object(server, "WORKSPACE_ROOT", tmp_root), \
                 mock.patch.object(server, "resolve_engine_python", return_value=sys.executable), \
                 mock.patch.object(server, "broadcast_log", side_effect=logs.append):
                code = server_workers.execute_single_render(["ignored"], "demo", engine_tag="local")

        self.assertEqual(code, 0)
        self.assertTrue(any("Encoding Text Prompt:" in line for line in logs), f"Missing tqdm line in logs: {logs}")
        self.assertTrue(any("DONE_OK" in line for line in logs), f"Process output cut short: {logs}")


class DoneQueueHistoryPersistenceTests(unittest.TestCase):
    """Ensure re-queueing, editing, or changing resolution never removes past completions from Done history."""

    def test_01_done_queue_persists_across_requeue_edit_resolution_and_vm(self):
        from ui import server_queue_history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "done_hist_proj")
            ctx = ProjectContext(cfg_path)
            hashes = compute_project_task_hashes(ctx)
            s01_stem = next(k for k in hashes if k.startswith("S01_"))
            mp4 = pdir / "work" / "renders" / f"{s01_stem}.mp4"
            mp4.write_bytes(b"\x00" * 60000)
            sh_file = pdir / "work" / "jobs" / "state_hashes.json"
            sh_file.write_text(
                json.dumps({
                    "tasks": {s01_stem: hashes[s01_stem][0]},
                    "scene_renders": {s01_stem: {"completed_at": 1700000100.0, "elapsed": 42.5}},
                }),
                encoding="utf-8",
            )

            with mock.patch.object(server, "PROJECTS_DIR", root):
                q1 = server.compute_fast_project_queue("done_hist_proj", force_recheck=True)
                self.assertEqual(next(x for x in q1 if x["scene_id"] == "S01")["status"], "completed")
                d1 = server.get_done_queue_history(server, "done_hist_proj", q1)
                self.assertEqual(len(d1), 1)
                self.assertEqual(d1[0]["scene_id"], "S01")
                self.assertEqual(d1[0]["elapsed"], 42.5)

                # 1. Force re-queue via enqueue_in_project_queue: active item is queued, Done history still keeps S01
                q2 = server.enqueue_in_project_queue("done_hist_proj", s01_stem)
                self.assertEqual(next(x for x in q2 if x["scene_id"] == "S01")["status"], "queued")
                d2 = server.get_done_queue_history(server, "done_hist_proj", q2)
                self.assertEqual(len(d2), 1)
                self.assertEqual(d2[0]["output_filename"], s01_stem)

                # 2. Targeted render + prompt edit + resolution switch: Done history still keeps the completed 832x480 record
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["scenes"][0]["prompt"] = "Updated prompt"
                cfg["scenes"][0]["resolution"] = "4"
                cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                q3 = server.compute_fast_project_queue("done_hist_proj", force_recheck=True)
                self.assertEqual(next(x for x in q3 if x["scene_id"] == "S01")["status"], "queued")
                server.prepare_targeted_queue_items("done_hist_proj", "S01", is_worker_running=False)
                d3 = server.get_done_queue_history(server, "done_hist_proj")
                self.assertEqual(len(d3), 1)
                self.assertEqual(d3[0]["output_filename"], s01_stem)

                # 3. Completing a second render later records both completions newest-first, while same-run <=5s deduplicates
                server_queue_history.record_done_items(
                    server, "done_hist_proj",
                    {"output_filename": s01_stem, "scene_id": "S01", "completed_at": 1700000102.0, "elapsed": 42.5, "status": "completed"},
                )
                self.assertEqual(len(server.get_done_queue_history(server, "done_hist_proj")), 1)
                server_queue_history.record_done_items(
                    server, "done_hist_proj",
                    {"output_filename": "S01_solo_1344x768", "scene_id": "S01", "completed_at": 1700000500.0, "elapsed": 90.0, "status": "completed"},
                )
                d4 = server.get_done_queue_history(server, "done_hist_proj")
                self.assertEqual(len(d4), 2)
                self.assertGreaterEqual(d4[0]["completed_at"], d4[1]["completed_at"])

        # Frontend Node VM verification for queue_engines.js + queue_and_console.js
        node_script = """
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const noopEl = () => ({ style: {}, dataset: {}, children: [], classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {}, appendChild() {}, innerHTML: "", textContent: "" });
const ctx = vm.createContext({
  console, Date, Math, JSON, Map, Set, Array, Number, String, Boolean,
  location: { search: "" },
  URLSearchParams: class { get() { return null; } },
  EventSource: class { close() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  document: { _els: {}, getElementById(id) { return this._els[id] || (this._els[id] = noopEl()); }, querySelectorAll: () => [], createElement: noopEl },
  state: { currentSlug: "projA", queueFilter: "completed", renderQueue: [], sseTaskStatuses: new Map(), engineLive: {} },
  showToast() {}, refreshProjectAssets() {}, formatDurationEstimate: (s) => `${Math.round(s)}s`,
  formatCompletedAt: () => "2026-11-22 12:00", formatElapsed: (s) => `${s}s`,
  escapeHtml: (s) => String(s || ""),
});
ctx.window = ctx; ctx.window.state = ctx.state;
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

ctx.applyQueueApiResponse({
  queue: [{ idx: 1, filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "completed", completed_at: 1700000100, elapsed: 40 }],
  done_queue: [{ idx: 1, filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "completed", completed_at: 1700000100, elapsed: 40 }],
});
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 1);

// Re-queue S01 in queue payload (even if done_queue isn't passed back): Done count & table keep S01
ctx.applyQueueApiResponse({
  queue: [{ idx: 1, filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "queued", needs_render: true, reason: "FORCED" }],
});
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 1);
assert.ok(ctx.document.getElementById("queueTableBody").innerHTML.includes("S01_solo.mp4"));

// Switching projects isolates doneQueue even before new project's queue response arrives, and discards late old responses & foreign SSE
ctx.state.currentSlug = "projB";
ctx.applyQueueApiResponse({
  slug: "projA",
  queue: [{ idx: 1, filename: "S99_old_projA.mp4", output_filename: "S99_old_projA", scene_id: "S99", status: "completed", completed_at: 1700000900 }],
  done_queue: [{ idx: 1, filename: "S99_old_projA.mp4", output_filename: "S99_old_projA", scene_id: "S99", status: "completed", completed_at: 1700000900 }],
});
ctx.applyQueueApiResponse({ slug: "projB", queue: [], done_queue: [] });
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 0);
assert.ok(!ctx.document.getElementById("queueTableBody").innerHTML.includes("S99_old_projA"));

// Foreign [QUEUE_DONE] SSE event from projA must not leak into projB's Done list
ctx.state.renderStatus = { running: true, queue_worker_active: true, current_project: "projA" };
ctx.handleQueueSSEEvent('[QUEUE_DONE] {"idx": 1, "filename": "S88_projA_live.mp4", "elapsed": 12}', "projA");
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 0);
assert.ok(!ctx.document.getElementById("queueTableBody").innerHTML.includes("S88_projA_live"));

// Global active engine conflict banner appears when projA holds the GPU while viewing projB
ctx.syncMultiEngineStatusUI({
  running: true, queue_worker_active: true, current_project: "projA",
  engines: { local: { running: true, filename: "S88_projA_live.mp4", active_tasks: [{ filename: "S88_projA_live.mp4", engine_status: { state: "rendering", pct: 42 } }] }, fal: { running: false }, minimax: { running: false } },
});
assert.ok(ctx.document.getElementById("multiEngineActiveLanes").innerHTML.includes("projA"));
assert.strictEqual(ctx.promoteActiveRenderTaskFromState(), false);
assert.strictEqual(ctx.state.isEngineRunning, false);
console.log("DONE_HISTORY_VM_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("DONE_HISTORY_VM_OK", res.stdout)

    def test_02_done_list_deduplicates_take_and_post_exit_shadow_across_backend_and_ui(self):
        from pipeline.takes import after_scene_render
        from ui import server_queue_history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "dup_done_proj")
            ctx = ProjectContext(cfg_path)
            s01_stem = "S01_solo_832x480"
            mp4 = pdir / "work" / "renders" / f"{s01_stem}.mp4"
            mp4.write_bytes(b"\x00" * 60000)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            sc_obj = cfg["scenes"][0]
            state = {"tasks": {}, "scene_renders": {}, "anchor_file_hashes": {}}

            # Simulate take written at t=1000.0 while subprocess teardown + stitch finishes 7.2s later (t=1007.2)
            t0 = 960.0
            t_take = 1000.0
            tk = after_scene_render(
                ctx, sc_obj, None, s01_stem, mp4, "hash_take1_aaaa1111",
                {"seed": 1001, "prompt": "Singing 1", "resolution": "832x480", "model_type": "minimax_h3_fl2va_pruned_pdd"},
                elapsed=38.5, state=state, created_at=t_take,
            )
            self.assertEqual(tk["created_at"], 1000.0)

            meta = server_queue_history.resolve_completion_meta(pdir, s01_stem, started_at=t0, fallback_ts=t_take + 7.2, fallback_elapsed=45.7)
            self.assertEqual(meta["completed_at"], 1000.0)
            self.assertEqual(meta["take_id"], tk["take_id"])
            self.assertEqual(meta["elapsed"], 38.5)

            # Even if an old worker or stitch delay wrote an untaken COMPLETED record 7.2s (or 25s) later, Done history keeps 1 entry
            with mock.patch.object(server, "PROJECTS_DIR", root):
                dh_path = server_queue_history.done_history_path(pdir, for_write=True)
                dh_path.parent.mkdir(parents=True, exist_ok=True)
                dh_path.write_text(json.dumps({
                    "version": 1,
                    "items": [
                        {"history_id": f"{s01_stem}@{t_take + 7.2}", "id": s01_stem, "output_filename": s01_stem, "filename": f"{s01_stem}.mp4", "scene_id": "S01", "status": "completed", "completed_at": t_take + 7.2, "elapsed": 45.7, "reason": "COMPLETED"},
                        {"history_id": f"take:{tk['take_id']}", "id": s01_stem, "output_filename": s01_stem, "filename": f"{s01_stem}.mp4", "scene_id": "S01", "status": "completed", "completed_at": t_take, "elapsed": 38.5, "reason": f"Take {tk['take_number']}", "take_id": tk["take_id"], "take_number": tk["take_number"]},
                    ],
                }), encoding="utf-8")

                reconciled = server.get_done_queue_history(server, "dup_done_proj", [
                    {"output_filename": s01_stem, "filename": f"{s01_stem}.mp4", "scene_id": "S01", "status": "completed", "completed_at": t_take + 25.0, "elapsed": 55.0, "reason": "UP-TO-DATE"}
                ])
                self.assertEqual(len(reconciled), 1)
                self.assertEqual(reconciled[0]["take_id"], tk["take_id"])
                self.assertEqual(reconciled[0]["completed_at"], t_take)

                # A genuinely distinct second take 40s later + its untaken shadow 7s later keeps 2 entries total (1 per render)
                tk2 = after_scene_render(
                    ctx, sc_obj, None, s01_stem, mp4, "hash_take2_bbbb2222",
                    {"seed": 1002, "prompt": "Singing 1 v2", "resolution": "832x480", "model_type": "minimax_h3_fl2va_pruned_pdd"},
                    elapsed=30.0, state=state, created_at=t_take + 40.0,
                )
                d2 = server.get_done_queue_history(server, "dup_done_proj", [
                    {"output_filename": s01_stem, "filename": f"{s01_stem}.mp4", "scene_id": "S01", "status": "completed", "completed_at": t_take + 47.0, "elapsed": 37.0, "reason": "COMPLETED"}
                ])
                self.assertEqual(len(d2), 2)
                self.assertEqual({x.get("take_id") for x in d2}, {tk["take_id"], tk2["take_id"]})

        # Frontend Node VM: optimistic SSE [QUEUE_DONE] + server COMPLETED/take response 7-15s later stays 1 Done row
        node_script = """
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const noopEl = () => ({ style: {}, dataset: {}, children: [], classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {}, appendChild() {}, innerHTML: "", textContent: "" });
const ctx = vm.createContext({
  console, Date, Math, JSON, Map, Set, Array, Number, String, Boolean,
  location: { search: "" },
  URLSearchParams: class { get() { return null; } },
  EventSource: class { close() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  document: { _els: {}, getElementById(id) { return this._els[id] || (this._els[id] = noopEl()); }, querySelectorAll: () => [], createElement: noopEl },
  state: { currentSlug: "projDup", queueFilter: "completed", renderQueue: [{ idx: 1, id: "S01_solo", filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "rendering" }], sseTaskStatuses: new Map(), engineLive: {} },
  showToast() {}, refreshProjectAssets() {}, formatDurationEstimate: (s) => `${Math.round(s)}s`,
  formatCompletedAt: () => "2026-11-22 12:00", formatElapsed: (s) => `${s}s`,
  escapeHtml: (s) => String(s || ""),
});
ctx.window = ctx; ctx.window.state = ctx.state;
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_engines.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/queue_and_console.js", "utf8"), ctx);

const sseNowSec = Math.floor(Date.now() / 1000);
ctx.handleQueueSSEEvent('[QUEUE_DONE] {"idx": 1, "filename": "S01_solo.mp4", "elapsed": 38.5}', "projDup");
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 1);

// 8 seconds later server finishes subprocess + stitch and returns authoritative take + COMPLETED queue item
ctx.applyQueueApiResponse({
  slug: "projDup",
  queue: [{ idx: 1, filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "completed", completed_at: sseNowSec + 8.0, elapsed: 45.7 }],
  done_queue: [{ history_id: "take:take_aaa1", filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "completed", completed_at: sseNowSec + 0.4, elapsed: 38.5, take_id: "take_aaa1", take_number: 1, reason: "Take 1" }],
});
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 1);

// Second distinct take 20s later produces 2 rows (both distinct renders shown once each)
ctx.applyQueueApiResponse({
  slug: "projDup",
  done_queue: [{ history_id: "take:take_bbb2", filename: "S01_solo.mp4", output_filename: "S01_solo", scene_id: "S01", status: "completed", completed_at: sseNowSec + 25.0, elapsed: 31.0, take_id: "take_bbb2", take_number: 2, reason: "Take 2" }],
});
ctx.renderQueueUI();
assert.strictEqual(ctx.document.getElementById("countDone").textContent, 2);
console.log("DONE_DEDUP_VM_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("DONE_DEDUP_VM_OK", res.stdout)


class StandardDirsAndStitchRegressionTests(unittest.TestCase):
    def test_07_standard_non_v3_dirs_queue_transitions_to_completed(self):
        from pipeline.media_utils import resolve_ffmpeg_path
        from pipeline.project_paths import resolve_done_history_file, resolve_project_dirs, resolve_queue_file
        from pipeline.takes import after_scene_render

        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "std_dirs_proj")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["project"]["directories"] = {
                "anchors": "work/anchors",
                "audio_slices": "work/audio",
                "masks": "work/masks",
                "jobs": "work/jobs",
                "renders": "work/renders",
                "trimmed": "work/trimmed",
                "review_strips": "work/frames",
            }
            cfg["scenes"] = [cfg["scenes"][0]]
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            pdirs = resolve_project_dirs(pdir)
            self.assertEqual(pdirs["renders"], (pdir / "work" / "renders").resolve())
            self.assertEqual(pdirs["jobs"], (pdir / "work" / "jobs").resolve())
            self.assertEqual(resolve_queue_file(pdir), (pdir / "work" / "jobs" / "v3_queue.json").resolve())
            self.assertEqual(resolve_done_history_file(pdir), (pdir / "work" / "jobs" / "done_history.json").resolve())

            with mock.patch.object(server, "PROJECTS_DIR", root):
                q1 = server.compute_fast_project_queue("std_dirs_proj", force_recheck=True)
                self.assertEqual(len(q1), 1)
                self.assertEqual(q1[0]["status"], "queued")
                self.assertEqual(q1[0]["reason"], "MISSING FILE")

                ctx = ProjectContext(cfg_path)
                out_name = q1[0]["output_filename"]
                out_mp4 = ctx.renders_dir / f"{out_name}.mp4"
                out_mp4.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:d=3.0",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_mp4),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                # Bump mtime cleanly after dummy anchor to simulate freshly completed render
                now_ts = time.time() + 2.0
                subprocess.run([sys.executable, "-c", f"import os; os.utime(r'{out_mp4}', ({now_ts}, {now_ts}))"], check=True)

                hashes = compute_project_task_hashes(ctx)
                cur_h = hashes[out_name][0]
                state = {"tasks": {out_name: cur_h}, "task_hashes": {out_name: cur_h}, "scene_renders": {out_name: {"render_start": 0.0, "render_end": 4.0, "frames": 96, "elapsed": 1.2, "completed_at": now_ts}}}
                ctx.save_state_hashes(state)
                take = after_scene_render(
                    ctx, ctx.scenes[0], None, out_name, out_mp4, cur_h,
                    {"resolution": "832x480", "render_start": 0.0, "render_end": 4.0, "video_length": 96}, 1.2, state,
                )
                self.assertTrue(str(take.get("file_path", "")).startswith("work/renders/takes/"))
                self.assertFalse((pdir / "work" / "renders_v3").exists())
                self.assertFalse((pdir / "work" / "jobs_v3").exists())

                q2 = server.compute_fast_project_queue("std_dirs_proj", force_recheck=False)
                self.assertEqual(q2[0]["status"], "completed")
                self.assertFalse(q2[0]["needs_render"])
                done_list = server.get_done_queue_history(server, "std_dirs_proj", q2)
                self.assertEqual(len(done_list), 1)
                self.assertEqual(done_list[0]["output_filename"], out_name)

    def test_08_stitch_missing_master_audio_fallback_and_worker_bool_status(self):
        from pipeline.media_utils import probe_has_audio_stream, resolve_ffmpeg_path
        from pipeline.stage3_master import assemble_progressive_master

        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "stitch_noaudio")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["project"]["master_audio"] = "audio/does_not_exist.mp3"
            cfg["project"]["total_duration"] = 2.0
            cfg["scenes"] = [cfg["scenes"][0]]
            cfg["scenes"][0]["end"] = 2.0
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            s01_mp4 = ctx.renders_dir / "S01_solo_832x480.mp4"
            subprocess.run(
                [
                    str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:d=2.0",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(s01_mp4),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                ok = assemble_progressive_master(ctx, [{"id": "S01", "start": 0.0, "end": 2.0, "clip": s01_mp4.name, "mode": "solo"}])
            self.assertTrue(ok, f"Expected silent audio fallback stitch to succeed; log:\n{buf.getvalue()}")
            self.assertTrue(ctx.master_output.is_file())
            self.assertGreater(ctx.master_output.stat().st_size, 10000)
            self.assertTrue(probe_has_audio_stream(ffmpeg_bin, ctx.master_output))

            # Failure diagnostics check: FFmpeg error details must surface via [MASTER_STITCH_FAILED] & run_stitch_worker -> False
            ctx.master_output.unlink(missing_ok=True)
            fake_proc = mock.MagicMock(returncode=1, stdout="Invalid filter graph syntax")
            err_buf = io.StringIO()
            with mock.patch("pipeline.stage3_master.subprocess.run", return_value=fake_proc):
                with mock.patch("sys.stdout", err_buf):
                    bad_ok = assemble_progressive_master(ctx, [{"id": "S01", "start": 0.0, "end": 2.0, "clip": s01_mp4.name, "mode": "solo"}])
            self.assertFalse(bad_ok)
            self.assertIn("[MASTER_STITCH_FAILED]", err_buf.getvalue())
            self.assertIn("Invalid filter graph syntax", err_buf.getvalue())

            # run_stitch_worker bool return + stitch_error propagation
            class _FakePopen:
                def __init__(self, *a, **kw):
                    self.stdout = io.StringIO("[Error] [MASTER_STITCH_FAILED] FFmpeg master concat/mux failed\n")
                    self.returncode = 1
                def wait(self, timeout=None): return 1

            with mock.patch.object(server, "PROJECTS_DIR", root):
                with mock.patch.object(server_workers.subprocess, "Popen", _FakePopen):
                    res_ok = server_workers.run_stitch_worker(str(cfg_path), "stitch_noaudio")
            self.assertFalse(res_ok)
            self.assertIn("MASTER_STITCH_FAILED", str(getattr(server, "stitch_error", "")))
            st = server.get_multi_engine_render_status(server)
            self.assertIn("MASTER_STITCH_FAILED", str(st.get("stitch_error", "")))

    def test_09_flac_master_audio_resolution_and_stitch_and_mime(self):
        from pipeline.media_utils import probe_has_audio_stream, resolve_ffmpeg_path
        from pipeline.stage3_master import assemble_progressive_master
        from ui import server_media

        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "flac_proj")
            flac_path = pdir / "audio" / "master_audio.flac"
            flac_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2.0", "-c:a", "flac", str(flac_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            # Even if project JSON defaults to audio/master_audio.mp3, master_audio.flac resolves automatically
            cfg["project"]["master_audio"] = "audio/master_audio.mp3"
            cfg["project"]["total_duration"] = 2.0
            cfg["scenes"] = [cfg["scenes"][0]]
            cfg["scenes"][0]["end"] = 2.0
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            self.assertEqual(ctx.master_audio, flac_path.resolve())
            s01_mp4 = ctx.renders_dir / "S01_solo_832x480.mp4"
            subprocess.run(
                [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:d=2.0", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(s01_mp4)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            ok = assemble_progressive_master(ctx, [{"id": "S01", "start": 0.0, "end": 2.0, "clip": s01_mp4.name, "mode": "solo"}])
            self.assertTrue(ok)
            self.assertTrue(probe_has_audio_stream(ffmpeg_bin, ctx.master_output))

            # HTTP streaming serves audio/flac cleanly
            sent = {"headers": {}}
            class _RangeDummy:
                headers = {}
                wfile = io.BytesIO()
                def send_response(self, code): sent["code"] = code
                def send_header(self, k, v): sent["headers"][k.lower()] = v
                def end_headers(self): pass
            server_media.stream_file_with_range(_RangeDummy(), flac_path)
            self.assertEqual(sent.get("code"), 200)
            self.assertEqual(sent["headers"].get("content-type"), "audio/flac")

    def test_10_timeline_header_band_does_not_overlap_thumbnails(self):
        tl_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "timeline_and_playback.js").read_text(encoding="utf-8")
        self.assertIn("const headerH = 18;", tl_js)
        self.assertIn("const thumbTop = trackTop + headerH;", tl_js)
        self.assertIn("ctx.drawImage(stripImg, f * srcFrameW, 0, srcFrameW, stripImg.naturalHeight, curX, thumbTop, thumbW, trackHeight);", tl_js)
        self.assertIn("ctx.drawImage(anchorImg, curX, thumbTop, thumbW, trackHeight);", tl_js)
        self.assertIn("ctx.fillRect(sx, trackTop, sw, headerH);", tl_js)


    def test_11_last_opened_project_persists_and_refresh_updates_takes_ui(self):
        from pipeline import takes as _takes

        # 1. Backend: _ensure_baseline_takes_seeded ignores Wan2GP internal tmp*_continue*.mp4 scratch renders
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "tmp_skip_proj")
            (pdir / "work" / "renders" / "tmp9ac3a4bc_continue1.mp4").write_bytes(b"\x00" * 60000)
            mf = _takes.ensure_baseline_takes_seeded(pdir, cfg_path)
            self.assertNotIn("tmp9ac3a4bc", mf.get("scenes", {}))

        # 2. Frontend Node VM: switching project persists last opened project across bare-URL reloads + refreshProjectAssets updates Takes UI
        node_script = """
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const noopEl = (id = "") => ({
  id, value: "", options: [], children: [], style: {}, dataset: {},
  getContext: () => ({}),
  classList: { _s: new Set(), toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); }, add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
  addEventListener() {}, appendChild(c) { this.children.push(c); this.options.push(c); },
  innerHTML: "", textContent: "",
});
const storage = new Map();
let curHref = "http://127.0.0.1:7860/";
let curSearch = "";
const els = {};
const calls = { takesInvalidated: 0, takesUpdatedFor: [] };
const ctx = vm.createContext({
  console, Date, Math, JSON, Map, Set, Array, Number, String, Boolean, URL, URLSearchParams,
  CustomEvent: class { constructor(t, d) { this.type = t; this.detail = d?.detail; } },
  Event: class { constructor(t) { this.type = t; } },
  localStorage: { getItem: (k) => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)) },
  location: { get href() { return curHref; }, get search() { return curSearch; } },
  history: { replaceState(_s, _t, u) { curHref = String(u); curSearch = new URL(curHref).search; } },
  document: {
    getElementById(id) { return els[id] || (els[id] = noopEl(id)); },
    querySelectorAll: () => [],
    createElement: (tag) => ({ ...noopEl(), tagName: tag }),
  },
  fetch: async (url) => {
    if (url === "/api/projects") {
      return { ok: true, json: async () => ({ projects: [
        { slug: "all_or_nothing", name: "All or Nothing", config_file: "all_or_nothing_v4.json" },
        { slug: "not_there_yet", name: "Not There Yet", config_file: "not_there_yet.json" },
      ] }) };
    }
    const slug = url.includes("not_there_yet") ? "not_there_yet" : "all_or_nothing";
    const cfg = slug === "not_there_yet" ? "not_there_yet.json" : "all_or_nothing_v4.json";
    return { ok: true, json: async () => ({
      config_file: cfg,
      data: { project: { name: slug, total_duration: 10 }, scenes: [{ id: "S01", start: 0, end: 5 }, { id: "S02", start: 5, end: 10 }], anchors: [] },
      assets: { anchors: [], clips: {} },
    }) };
  },
  AudioContext: null, webkitAudioContext: null,
  showToast() {}, redrawTimeline() {}, updatePlayheadVisual() {}, loadProjectAudio: async () => {},
  updateProjectSettingsUI() {}, updateAnchorStudioUI() {}, updateInspectorAnchorsDropdown() {}, updateInspectorStemsDropdown() {},
  updateMasterVideoUI() {}, updateSceneClipPreview() {}, loadProjectQueue() {}, dispatchEvent() {}, addEventListener() {},
  invalidateSceneTakesCache() { calls.takesInvalidated++; },
  updateSceneTakesUI(sc) { calls.takesUpdatedFor.push(sc.id); },
  canvasImageCache: {},
});
ctx.window = ctx;
vm.runInContext(fs.readFileSync("work/ui/static/js/state_and_utils.js", "utf8"), ctx);

(async () => {
  await ctx.loadProjects();
  assert.strictEqual(ctx.state.currentSlug, "all_or_nothing");
  await ctx.selectProject("not_there_yet", "not_there_yet.json");
  assert.strictEqual(curSearch, "?project=not_there_yet");

  // Simulate user opening bare root URL after switching to not_there_yet
  curHref = "http://127.0.0.1:7860/"; curSearch = "";
  await ctx.loadProjects();
  assert.strictEqual(ctx.state.currentSlug, "not_there_yet");
  assert.strictEqual(ctx.document.getElementById("projectSelect").value, "not_there_yet:::not_there_yet.json");

  // Explicit URL ?project= takes precedence over localStorage
  curHref = "http://127.0.0.1:7860/?project=all_or_nothing"; curSearch = "?project=all_or_nothing";
  await ctx.loadProjects();
  assert.strictEqual(ctx.state.currentSlug, "all_or_nothing");

  // refreshProjectAssets invalidates takes cache and refreshes selected scene's Takes UI
  ctx.state.selectedSceneIdx = 1;
  await ctx.refreshProjectAssets(true);
  assert.ok(calls.takesInvalidated >= 1);
  assert.deepStrictEqual(calls.takesUpdatedFor, ["S02"]);
  console.log("PROJECT_PERSIST_AND_TAKES_REFRESH_OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("PROJECT_PERSIST_AND_TAKES_REFRESH_OK", res.stdout)

    def test_12_stems_dropdown_excludes_missing_stems_and_deduplicates_master(self):
        """Non-existent template/default stems must not appear in Audio Stem dropdowns, and master extension fallback must deduplicate."""
        from pipeline.cli import init_new_project
        from pipeline.project_paths import scan_project_audio_assets

        with tempfile.TemporaryDirectory() as tmp:
            proj_dir = Path(tmp) / "not_there_yet"
            init_new_project(str(proj_dir), force=True)
            init_cfg = json.loads((proj_dir / "not_there_yet.json").read_text(encoding="utf-8"))
            self.assertEqual(init_cfg["project"]["stems"], {})

            # Simulate user dropping master_audio.flac while template stems still point to missing files
            (proj_dir / "audio" / "master_audio.flac").write_bytes(b"fLaCmock")
            cfg_data = {
                "project": {
                    "master_audio": "audio/master_audio.mp3",
                    "stems": {"lead_vocal": "audio/lead_vocal.mp3", "backing_track": "audio/backing_track.mp3"},
                },
                "scenes": [
                    {"id": "S01", "mode": "ambient", "stem": "silent"},
                    {"id": "S02", "mode": "male_solo", "stem": "male"},
                ],
            }
            assets = scan_project_audio_assets(proj_dir, cfg_data)
            self.assertEqual(assets["audio_files"], ["master_audio.flac"])
            self.assertEqual(assets["master_audio_file"], "master_audio.flac")
            self.assertTrue(assets["master_audio_exists"])
            self.assertEqual(assets["audio_stems"], [])
            self.assertEqual(sorted(assets["missing_stems"]), ["backing_track", "lead_vocal"])

            # When a stem file actually exists (even with different casing), scan_project_audio_assets surfaces it
            (proj_dir / "audio" / "Male_Vocal.mp3").write_bytes(b"ID3mock")
            cfg_with_male = {
                "project": {
                    "master_audio": "audio/master_audio.mp3",
                    "stems": {"male": "audio/male_vocal.mp3", "female": "audio/female_vocal.mp3"},
                }
            }
            assets_male = scan_project_audio_assets(proj_dir, cfg_with_male)
            self.assertEqual(assets_male["audio_stems"], ["male"])
            self.assertEqual(assets_male["stem_files"], {"male": "Male_Vocal.mp3"})
            self.assertEqual(assets_male["missing_stems"], ["female"])

        node_script = """
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const els = new Map();
const mkEl = (id) => {
  if (els.has(id)) return els.get(id);
  const o = {
    id, value: "", style: {}, options: [], dataset: {}, childNodes: [],
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {}, querySelectorAll: () => [],
    add(opt) { this.appendChild(opt); },
    appendChild(c) { this.options.push(c); c.remove = () => { this.options = this.options.filter((x) => x !== c); }; return c; },
    set innerHTML(v) { if (v === "") this.options = []; },
    get innerHTML() { return ""; },
  };
  els.set(id, o);
  return o;
};
const ctx = {
  Option: function(text, value) { return { textContent: text, text, value, dataset: {} }; },
  document: {
    getElementById: mkEl,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => { const o = mkEl("__el_" + tag + "_" + Math.random()); o.tagName = tag.toUpperCase(); return o; },
    addEventListener: () => {},
  },
  window: null,
  state: {
    currentSlug: "not_there_yet",
    assets: """ + json.dumps(assets) + """,
    projectData: """ + json.dumps(cfg_data) + """,
    models: { anchor_renderers: {}, video_renderers: {} },
  },
  markProjectDirty() {}, showToast() {}, redrawTimeline() {}, refreshProjectAssets() {},
  resizeAllPromptTextareas() {}, updatePromptResolvedPreviews() {},
};
ctx.window = ctx; ctx.global = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/anchors_and_settings.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/scene_inspector.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/master_and_export.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("work/ui/static/js/duet_repair.js", "utf8"), ctx);

ctx.updateInspectorStemsDropdown();
const stemSel = mkEl("sceneStem");
const vals = Array.from(stemSel.options, (o) => o.value);
assert.deepStrictEqual(vals, ["master", "silent"]);
assert.ok(stemSel.options[0].textContent.includes("master_audio.flac"));
assert.deepStrictEqual(
  Array.from(ctx.getSceneOptionCatalog(ctx.state.projectData.scenes[1]).stems, (s) => s.value),
  ["master", "silent"]
);

// Scene S02 saved with missing stem "male" displays explicit "(missing audio file)" warning without polluting dropdown permanently
ctx.updateSceneInspectorInputs(ctx.state.projectData.scenes[1]);
assert.strictEqual(stemSel.value, "male");
assert.ok(stemSel.options.some((o) => o.value === "male" && o.textContent.includes("missing audio file")));
ctx.updateSceneInspectorInputs(ctx.state.projectData.scenes[0]);
assert.strictEqual(stemSel.value, "silent");
assert.deepStrictEqual(Array.from(stemSel.options, (o) => o.value), ["master", "silent"]);

// Repair UI stem dropdown excludes missing stems and hardcoded male/female/backing/lead defaults
const upBtn = mkEl("btnUpscaleTake");
const tray = mkEl("sceneTakesTray");
upBtn.parentElement = tray;
tray.insertBefore = () => {};
mkEl("btnToggleLipSyncRepair");
mkEl("lipSyncRepairBar");
ctx.syncTakeRepairUI(ctx.state.projectData.scenes[1]);
const repVals = Array.from(mkEl("repStem").options, (o) => o.value);
assert.deepStrictEqual(repVals, ["", "master"]);

// Mode switch when project lacks separate vocal stems falls back to master instead of inventing male/lead_vocal
const sNew = { id: "S03", mode: "ambient", stem: "silent" };
ctx.handleSceneModeChange(sNew, "solo");
assert.strictEqual(sNew.stem, "master");
console.log("STEM_DROPDOWN_DISK_VERIFIED_OK");
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("STEM_DROPDOWN_DISK_VERIFIED_OK", res.stdout)

    def test_15_master_stitch_beyond_stale_total_duration_and_added_scenes(self):
        """Master stitch uses actual scene span even when project.total_duration is smaller/stale and reconciles stale v3_timeline.json."""
        from pipeline.media_utils import probe_video_duration, resolve_ffmpeg_path
        from pipeline.stage3_master import assemble_progressive_master, resolve_scene_fade_filters

        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir, cfg_path = _make_mini_project(root, "beyond_td_proj")
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            audio_p = pdir / "audio" / "master_audio.mp3"
            audio_p.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "5.0", "-c:a", "libmp3lame", str(audio_p)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            cfg["project"]["master_audio"] = "audio/master_audio.mp3"
            cfg["project"]["total_duration"] = 1.0  # Smaller than scenes (e.g. stale template 60s on a 126s timeline)
            cfg["project"]["fade_out_last_scene_seconds"] = 0.5
            cfg["scenes"] = [
                {**cfg["scenes"][0], "id": "S01", "start": 0.0, "end": 1.5, "stem": "silent"},
                {**cfg["scenes"][0], "id": "S02", "start": 1.5, "end": 3.0, "stem": "silent"},
            ]
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx = ProjectContext(cfg_path)
            s01_mp4 = ctx.renders_dir / "S01_solo_832x480.mp4"
            s02_mp4 = ctx.renders_dir / "S02_solo_832x480.mp4"
            for mp4, color in ((s01_mp4, "navy"), (s02_mp4, "teal")):
                subprocess.run(
                    [
                        str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24:d=1.6",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(mp4),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if mp4.stat().st_size <= 55000:
                    with open(mp4, "ab") as f:
                        f.write(b"\x00" * (60000 - mp4.stat().st_size))

            # Fade filter for S02 must not become solid black just because stale total_duration=1.0 < 1.5
            fsig, ff = resolve_scene_fade_filters(0.5, None, 1.5, 3.0, 3.0)
            self.assertNotIn("drawbox=c=black:t=fill", ff)

            # Stale v3_timeline.json only containing S01 must be reconciled with ctx.scenes (S01 + S02) on stitch_only
            (ctx.jobs_dir / "v3_timeline.json").write_text(
                json.dumps([{"id": "S01", "start": 0.0, "end": 1.5, "clip": s01_mp4.name, "mode": "solo"}]),
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["run_overnight_v3.py", str(cfg_path), "stitch"]):
                from pipeline import cli as pipeline_cli
                pipeline_cli.main()

            self.assertTrue(ctx.master_output.is_file())
            master_dur = probe_video_duration(ffmpeg_bin, ctx.master_output)
            self.assertIsNotNone(master_dur)
            self.assertGreaterEqual(master_dur, 2.9, f"Expected ~3.0s master covering S01+S02, got {master_dur}s")
            reconciled = json.loads((ctx.jobs_dir / "v3_timeline.json").read_text(encoding="utf-8"))
            self.assertEqual([e["id"] for e in reconciled], ["S01", "S02"])


if __name__ == "__main__":
    unittest.main()
