"""
Unit and Integration Tests for Non-Destructive Rollback & Render Vault System.

Covers:
1. Hardlink creation, unlinking, and copy fallback (safe_link_or_copy).
2. Retroactive baseline take seeding (ensure_baseline_takes_seeded).
3. Subsequent render archiving (before_scene_render & after_scene_render).
4. Auto-promotion vault hit on prompt/seed revert (try_auto_promote_vault_hit).
5. Manual take activation & parameter restoration (promote_take_to_active).
6. Project-level checkpoint creation, deduplication, and zero-GPU rollback (rollback_project_checkpoint).
7. Takes and Checkpoints REST APIs (handle_takes_api).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from http import HTTPStatus
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from pipeline.context import ProjectContext
from pipeline.stage2_scenes import compute_project_task_hashes
from pipeline.takes import (
    safe_link_or_copy,
    load_takes_manifest,
    save_takes_manifest,
    save_state_hashes_data,
    ensure_baseline_takes_seeded,
    before_scene_render,
    after_scene_render,
    restore_previous_active_take_if_missing,
    try_auto_promote_vault_hit,
    promote_take_to_active,
    delete_scene_take,
    create_project_checkpoint,
    list_project_checkpoints,
    rollback_project_checkpoint,
    get_vault_stats,
    handle_takes_api,
)


class MockContext:
    def __init__(self, project_dir, config_file):
        self.project_dir = Path(project_dir)
        self.config_file = Path(config_file)


class DummyRequestHandler:
    """Mock HTTP request handler for testing REST endpoints."""

    def __init__(self, path: str = ""):
        self.path = path
        self.sent_status = None
        self.sent_json_data = None
        self.sent_error = None

    def send_json(self, data: dict | list, status=HTTPStatus.OK):
        self.sent_status = status
        self.sent_json_data = data

    def send_error_json(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self.sent_status = status
        self.sent_error = message


class TestTakesAndRollback(unittest.TestCase):
    def setUp(self):
        try:
            import server
            self._orig_server_projects_dir = getattr(server, "PROJECTS_DIR", None)
        except Exception:
            self._orig_server_projects_dir = None

        # Create an isolated temporary test directory for each test
        self.test_dir = tempfile.mkdtemp(prefix="mvg_vault_test_")
        self.proj_dir = Path(self.test_dir) / "test_slug"
        self.proj_dir.mkdir(parents=True, exist_ok=True)

        self.renders_dir = self.proj_dir / "work" / "renders"
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir = self.proj_dir / "work" / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.anchors_dir = self.proj_dir / "work" / "anchors"
        self.anchors_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.proj_dir / "work" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.proj_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        (self.audio_dir / "master.wav").write_bytes(b"dummy_wav_header")

        # Create basic anchor
        self.anchor_file = self.anchors_dir / "A01_vocalist.jpg"
        self.anchor_file.write_bytes(b"dummy_anchor_bytes_12345")

        # Create basic project config
        self.config_file = self.proj_dir / "test_slug.json"
        self.initial_project_data = {
            "project": {"name": "Test Project", "default_resolution": "768x432", "fps": 24, "master_audio": "audio/master.wav"},
            "models": {"resolution": "768x432"},
            "scenes": [
                {"id": "S01", "mode": "solo", "output_name": "S01_solo", "start": 0.0, "end": 5.0, "anchor": "A01", "prompt": "Solo vocalist singing intro", "seed": 1001, "video_renderer": "minimax_h3"},
                {"id": "S02", "mode": "solo", "output_name": "S02_solo", "start": 5.0, "end": 10.0, "anchor": "A01", "prompt": "Solo vocalist singing verse", "seed": 1002, "video_renderer": "minimax_h3"},
            ],
        }
        self.config_file.write_text(json.dumps(self.initial_project_data, indent=2), encoding="utf-8")

        # Create initial state hashes
        self.state_hashes_file = self.proj_dir / "work" / "state_hashes.json"
        self.initial_state_hashes = {
            "tasks": {"S01_solo_768x432": "hash_s01_v1", "S02_solo_768x432": "hash_s02_v1"},
            "task_hashes": {"S01_solo_768x432": "hash_s01_v1", "S02_solo_768x432": "hash_s02_v1"},
            "scene_renders": {
                "S01_solo_768x432": {"completed_at": 1700000000.0, "elapsed": 45.0},
                "S02_solo_768x432": {"completed_at": 1700000050.0, "elapsed": 48.0},
            },
        }
        self.state_hashes_file.write_text(json.dumps(self.initial_state_hashes, indent=2), encoding="utf-8")

    def tearDown(self):
        if getattr(self, "_orig_server_projects_dir", None) is not None:
            try:
                import server
                server.PROJECTS_DIR = self._orig_server_projects_dir
            except Exception:
                pass
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 1. safe_link_or_copy
    # -------------------------------------------------------------------------
    def test_01_safe_link_or_copy(self):
        """Verify safe_link_or_copy unlinks destination and links or copies without truncating source."""
        src = self.renders_dir / "test_src.mp4"
        src.write_bytes(b"dummy_video_content_abc")
        dst = self.renders_dir / "test_dst.mp4"

        # Link to new destination
        ok = safe_link_or_copy(src, dst)
        self.assertTrue(ok)
        self.assertTrue(dst.is_file())
        self.assertEqual(dst.read_bytes(), b"dummy_video_content_abc")

        # Overwrite destination: must unlink dst first so src is NOT modified
        dst_overwrite = self.renders_dir / "test_dst.mp4"
        src2 = self.renders_dir / "test_src2.mp4"
        src2.write_bytes(b"dummy_video_content_xyz")

        ok2 = safe_link_or_copy(src2, dst_overwrite)
        self.assertTrue(ok2)
        self.assertEqual(dst_overwrite.read_bytes(), b"dummy_video_content_xyz")
        # Ensure original src content is intact!
        self.assertEqual(src.read_bytes(), b"dummy_video_content_abc")

    # -------------------------------------------------------------------------
    # 2. Baseline Seeding
    # -------------------------------------------------------------------------
    def test_02_baseline_seeding(self):
        """Verify ensure_baseline_takes_seeded discovers existing renders and creates Take 1 baseline."""
        # Create a pre-existing render in renders
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"x" * 60000)  # > 50KB valid size

        s01_strip = self.frames_dir / "S01_strip.jpg"
        s01_strip.write_bytes(b"strip_frame_bytes")

        # Seed baseline
        manifest = ensure_baseline_takes_seeded(self.proj_dir, self.config_file)
        self.assertIn("S01", manifest.get("scenes", {}))

        # Check manifest
        manifest = load_takes_manifest(self.proj_dir)
        self.assertIn("S01", manifest.get("scenes", {}))
        s01_data = manifest["scenes"]["S01"]
        self.assertTrue(s01_data.get("is_baseline_seeded"))
        self.assertEqual(len(s01_data.get("takes", [])), 1)

        take1 = s01_data["takes"][0]
        self.assertEqual(take1["take_number"], 1)
        self.assertTrue(take1.get("is_baseline"))
        self.assertEqual(take1["task_hash"], "hash_s01_v1")
        self.assertEqual(take1["seed"], 1001)

        # Verify vaulted take file exists on disk
        vault_file = self.proj_dir / take1["file_path"]
        self.assertTrue(vault_file.is_file())
        self.assertEqual(vault_file.stat().st_size, 60000)

        # Calling again should be a fast no-op (<50ms)
        t0 = time.time()
        seeded_again = ensure_baseline_takes_seeded(self.proj_dir, self.config_file)
        dur = time.time() - t0
        self.assertEqual(len(seeded_again["scenes"]["S01"]["takes"]), 1)
        self.assertLess(dur, 0.05)

    # -------------------------------------------------------------------------
    # 3. Subsequent Render Archiving
    # -------------------------------------------------------------------------
    def test_03_subsequent_render_archiving(self):
        """Verify before_scene_render unlinks active file and after_scene_render vaults new take."""
        # Seed baseline first
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"x" * 60000)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        ctx = MockContext(self.proj_dir, self.config_file)
        sc = self.initial_project_data["scenes"][0]
        out_name = "S01_solo_768x432"
        state = {"task_hashes": {out_name: "hash_s01_v1"}}

        # Pre-render hook for Take 2
        before_scene_render(ctx, sc, out_name, s01_render, state)
        self.assertFalse(s01_render.exists(), "Active render file should be unlinked before rendering fresh inode")

        # Engine produces new render
        s01_render.write_bytes(b"y" * 65000)

        # Post-render hook for Take 2
        t_dict = {
            "prompt": "Solo vocalist singing intro with high energy",
            "seed": 2002,
            "duration": 5.0,
            "video_renderer": "minimax_h3",
        }
        new_take = after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash="hash_s01_v2", t_dict=t_dict, elapsed=42.0, state=state
        )

        self.assertIsNotNone(new_take)
        self.assertEqual(new_take["take_number"], 2)
        self.assertEqual(new_take["task_hash"], "hash_s01_v2")

        # Verify manifest now has both takes and preserved previously selected Take 1 as active
        manifest = load_takes_manifest(self.proj_dir)
        takes = manifest["scenes"]["S01"]["takes"]
        self.assertEqual(len(takes), 2)
        self.assertEqual(manifest["scenes"]["S01"]["active_take_id"], takes[0]["take_id"])
        self.assertEqual(s01_render.stat().st_size, 60000)

        # Both vault files must exist independently
        vault1 = self.proj_dir / "work" / "renders" / takes[0]["vault_rel_mp4"]
        vault2 = self.proj_dir / "work" / "renders" / takes[1]["vault_rel_mp4"]
        self.assertTrue(vault1.is_file())
        self.assertTrue(vault2.is_file())
        self.assertEqual(vault1.stat().st_size, 60000)
        self.assertEqual(vault2.stat().st_size, 65000)

    # -------------------------------------------------------------------------
    # 4. Vault Cache-Hit Auto Promotion
    # -------------------------------------------------------------------------
    def test_04_vault_cache_hit_auto_promotion(self):
        """Verify try_auto_promote_vault_hit promotes matching take when user reverts prompt/seed."""
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"x" * 60000)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        ctx = MockContext(self.proj_dir, self.config_file)
        sc = self.initial_project_data["scenes"][0]
        out_name = "S01_solo_768x432"
        state = {"task_hashes": {out_name: "hash_s01_v1"}}

        # Create Take 2 and explicitly switch active to Take 2 before testing Undo vault hit back to Take 1
        before_scene_render(ctx, sc, out_name, s01_render, state)
        s01_render.write_bytes(b"y" * 65000)
        t_dict = {"prompt": "Alt prompt", "seed": 2002, "duration": 5.0}
        after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash="hash_s01_v2", t_dict=t_dict, elapsed=40.0, state=state,
            preserve_existing_active=False,
        )

        # Active is now Take 2. Now simulate user undoing back to Take 1's hash ("hash_s01_v1")
        promoted = try_auto_promote_vault_hit(
            self.proj_dir, self.config_file, "S01", pass_num=None, cur_task_hash="hash_s01_v1"
        )
        self.assertTrue(promoted)

        # Active file on disk should now match Take 1 content (60000 bytes)!
        self.assertEqual(s01_render.stat().st_size, 60000)

        # Manifest active take should now be Take 1
        manifest = load_takes_manifest(self.proj_dir)
        self.assertEqual(manifest["scenes"]["S01"]["active_take_id"], manifest["scenes"]["S01"]["takes"][0]["take_id"])

        # State hashes file should have completed_at preserved
        hashes_data = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
        self.assertEqual(hashes_data["tasks"]["S01_solo_768x432"], "hash_s01_v1")

    # -------------------------------------------------------------------------
    # 5. Manual Take Activation
    # -------------------------------------------------------------------------
    def test_05_manual_take_activation(self):
        """Verify promote_take_to_active restores creative params and sets active file."""
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"x" * 60000)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        ctx = MockContext(self.proj_dir, self.config_file)
        sc = self.initial_project_data["scenes"][0]
        out_name = "S01_solo_768x432"
        state = {"task_hashes": {out_name: "hash_s01_v1"}}

        # Take 2 render
        before_scene_render(ctx, sc, out_name, s01_render, state)
        s01_render.write_bytes(b"y" * 65000)
        t_dict = {"prompt": "V2 Prompt", "seed": 9999, "duration": 5.0}
        after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash="hash_s01_v2", t_dict=t_dict, elapsed=40.0, state=state
        )

        # Manually alter scene config in project.json
        cfg_data = json.loads(self.config_file.read_text(encoding="utf-8"))
        cfg_data["scenes"][0]["prompt"] = "V2 Prompt"
        cfg_data["scenes"][0]["seed"] = 9999
        self.config_file.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")

        # Now manually activate Take 1
        manifest = load_takes_manifest(self.proj_dir)
        take1_id = manifest["scenes"]["S01"]["takes"][0]["take_id"]

        res = promote_take_to_active(self.proj_dir, self.config_file, "S01", take1_id)
        self.assertTrue(res["success"])
        self.assertEqual(res["active_take_id"], take1_id)

        # Assert config file params restored
        updated_cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_cfg["scenes"][0]["prompt"], "Solo vocalist singing intro")
        self.assertEqual(updated_cfg["scenes"][0]["seed"], 1001)

        # Assert disk file matches Take 1
        self.assertEqual(s01_render.stat().st_size, 60000)

        # Assert mtime bumped above anchor mtime
        self.assertGreater(s01_render.stat().st_mtime, self.anchor_file.stat().st_mtime)

    # -------------------------------------------------------------------------
    # 6. Project-Level Checkpoints and Rollback
    # -------------------------------------------------------------------------
    def test_06_project_checkpoints_and_rollback(self):
        """Verify create_project_checkpoint snapshots project state and rollback_project_checkpoint restores it."""
        # 1. Create a manual checkpoint 'CP_BEFORE_EDIT'
        cp1 = create_project_checkpoint(
            self.proj_dir, self.config_file, name="CP_BEFORE_EDIT", trigger="manual", comment="Initial stable state"
        )
        self.assertIsNotNone(cp1)
        self.assertEqual(cp1["name"], "CP_BEFORE_EDIT")

        # 2. Mutate project config: delete S02, alter S01 prompt
        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        cfg["scenes"][0]["prompt"] = "Completely altered prompt"
        cfg["scenes"] = [cfg["scenes"][0]]  # Deleted S02
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        # Checkpoint list should have CP1
        cps = list_project_checkpoints(self.proj_dir)
        self.assertEqual(len(cps), 1)

        # 3. Rollback to CP1
        roll_res = rollback_project_checkpoint(self.proj_dir, cp1["id"])
        self.assertTrue(roll_res["success"])

        # 4. Verify project config restored perfectly
        restored_cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(len(restored_cfg["scenes"]), 2)
        self.assertEqual(restored_cfg["scenes"][0]["prompt"], "Solo vocalist singing intro")
        self.assertEqual(restored_cfg["scenes"][1]["id"], "S02")

        # 5. Verify pre-rollback safety snapshot was created automatically
        cps_after = list_project_checkpoints(self.proj_dir)
        self.assertGreaterEqual(len(cps_after), 2)
        triggers = [c["trigger"] for c in cps_after]
        self.assertIn("pre_rollback", triggers)

    # -------------------------------------------------------------------------
    # 7. Vault Stats Calculation
    # -------------------------------------------------------------------------
    def test_07_vault_stats(self):
        """Verify get_vault_stats returns correct take counts and disk usage."""
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"x" * 1048576)  # 1 MB
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        stats = get_vault_stats(self.proj_dir)
        self.assertGreaterEqual(stats["total_scenes"], 1)
        self.assertGreaterEqual(stats["total_takes"], 1)
        self.assertAlmostEqual(stats["total_mb"], 1.0, places=1)

    # -------------------------------------------------------------------------
    # 8. REST APIs via handle_takes_api
    # -------------------------------------------------------------------------
    def test_08_rest_apis(self):
        """Verify REST API dispatcher responds correctly to takes and checkpoint requests."""
        # Mock server module PROJECTS_DIR to point to our test directory
        import server
        server.PROJECTS_DIR = Path(self.test_dir)

        # Seed baseline so we have a take
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"x" * 60000)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        # Test GET /api/takes/test_slug/S01
        h1 = DummyRequestHandler()
        handle_takes_api(h1, "get_takes", path="/api/takes/test_slug/S01")
        self.assertEqual(h1.sent_status, HTTPStatus.OK)
        self.assertTrue(h1.sent_json_data["success"])
        self.assertEqual(len(h1.sent_json_data["takes"]), 1)

        # Test POST /api/checkpoints/create
        h2 = DummyRequestHandler()
        handle_takes_api(
            h2, "create", body={"project": "test_slug", "name": "API Checkpoint", "comment": "Created via API"}
        )
        self.assertEqual(h2.sent_status, HTTPStatus.OK)
        self.assertTrue(h2.sent_json_data["success"])
        created_cp_id = h2.sent_json_data["checkpoint"]["id"]

        # Test GET /api/checkpoints/test_slug
        h3 = DummyRequestHandler()
        handle_takes_api(h3, "get_checkpoints", path="/api/checkpoints/test_slug")
        self.assertEqual(h3.sent_status, HTTPStatus.OK)
        self.assertGreaterEqual(len(h3.sent_json_data["checkpoints"]), 1)

        # Test POST /api/checkpoints/rollback
        h4 = DummyRequestHandler()
        handle_takes_api(h4, "rollback", body={"project": "test_slug", "checkpoint_id": created_cp_id})
        self.assertEqual(h4.sent_status, HTTPStatus.OK)
        self.assertTrue(h4.sent_json_data["success"])

    # -------------------------------------------------------------------------
    # 9. Dual-location state_hashes Checkpoints and Rollback
    # -------------------------------------------------------------------------
    def test_09_checkpoint_with_jobs_state_hashes(self):
        """Verify checkpoint & rollback works when state_hashes is in jobs."""
        if self.state_hashes_file.is_file():
            self.state_hashes_file.unlink()
        jobs_hashes = self.jobs_dir / "state_hashes.json"
        jobs_hashes.write_text(json.dumps({"tasks": {"S01_solo": "hash_v1"}}, indent=2), encoding="utf-8")

        cp = create_project_checkpoint(self.proj_dir, self.config_file, name="JobsV3 Checkpoint", trigger="manual")
        self.assertTrue((self.proj_dir / "checkpoints" / cp["id"] / "state_hashes.json").is_file())

        # Mutate jobs hashes
        jobs_hashes.write_text(json.dumps({"tasks": {"S01_solo": "hash_mutated"}}, indent=2), encoding="utf-8")

        # Rollback
        roll = rollback_project_checkpoint(self.proj_dir, cp["id"])
        self.assertTrue(roll["success"])

        # Verify restoration in jobs
        restored = json.loads(jobs_hashes.read_text(encoding="utf-8"))
        self.assertEqual(restored.get("tasks", {}).get("S01_solo"), "hash_v1")

    # -------------------------------------------------------------------------
    # 10. Server Media Take Resolution with Null strip_path
    # -------------------------------------------------------------------------
    def test_10_server_media_take_resolution_with_null_strip(self):
        """Verify server_media handles takes with strip_path=None without throwing TypeError."""
        import server
        from server_media import handle_media_stream

        server.PROJECTS_DIR = Path(self.test_dir)

        # Create dummy vault take file
        take_id = "take_20261108_120000_S01_solo"
        vault_clip = self.renders_dir / "takes" / "S01" / f"{take_id}.mp4"
        vault_clip.parent.mkdir(parents=True, exist_ok=True)
        vault_clip.write_bytes(b"dummy_mp4_bytes")

        # Create manifest entry with null strip_path
        manifest = {
            "version": 1,
            "scenes": {
                "S01": {
                    "takes": [
                        {
                            "take_id": take_id,
                            "out_name": "S01_solo",
                            "file_path": None,
                            "strip_path": None,
                            "vault_rel_mp4": f"takes/S01/{take_id}.mp4",
                            "vault_rel_strip": None,
                        }
                    ]
                }
            },
        }
        (self.proj_dir / "work" / "takes_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Request clip with ?take=take_id
        class MediaMockHandler:
            def __init__(self, path):
                self.path = path
                self.headers = {}
                self.sent_status = None
                self.written = b""

            def send_response(self, code):
                self.sent_status = code

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

            def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST):
                self.sent_status = status

            @property
            def wfile(self):
                import io
                return io.BytesIO()

        h = MediaMockHandler(f"/media/test_slug/clip/S01_solo.mp4?take={take_id}")
        # Should not raise TypeError: unsupported operand type(s) for /: 'WindowsPath' and 'NoneType'
        handle_media_stream(h, "test_slug/clip/S01_solo.mp4")
        self.assertEqual(h.sent_status, HTTPStatus.OK)

        # Streaming bare scene ID without ?take= must resolve active_take_id (not first alphabetical resolution/Pass1)
        (self.renders_dir / "S01_Pass1_Left_1280x720.mp4").write_bytes(b"OLD_1280_P1" + b"x" * 60000)
        (self.renders_dir / "S01_Pass2_Final_1280x720.mp4").write_bytes(b"OLD_1280_P2" + b"x" * 60000)
        act_vault = self.renders_dir / "takes" / "S01" / "S01_Pass2_Final_1920x1088__act1920.mp4"
        act_vault.write_bytes(b"ACT_1920_P2" + b"y" * 60000)
        (self.renders_dir / "S01_Pass2_Final_1920x1088.mp4").write_bytes(b"ACT_1920_P2" + b"y" * 60000)
        manifest["scenes"]["S01"]["active_take_id"] = "take_act1920"
        manifest["scenes"]["S01"]["takes"].append({
            "take_id": "take_act1920",
            "take_number": 2,
            "scene_id": "S01",
            "pass_num": 2,
            "out_name": "S01_Pass2_Final_1920x1088",
            "vault_rel_mp4": "takes/S01/S01_Pass2_Final_1920x1088__act1920.mp4",
            "file_path": "work/renders/takes/S01/S01_Pass2_Final_1920x1088__act1920.mp4",
            "resolution": "1920x1088",
        })
        (self.proj_dir / "work" / "takes_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        import ui.server_queue as srv_q
        clips = srv_q.collect_project_clips(self.proj_dir, self.initial_project_data)
        self.assertEqual(clips["S01"]["filename"], "S01_Pass2_Final_1920x1088.mp4")

        import io
        from unittest import mock
        cap_buf = io.BytesIO()
        h_bare = MediaMockHandler("/media/test_slug/clip/S01?t=123")
        with mock.patch.object(MediaMockHandler, "wfile", new_callable=mock.PropertyMock, return_value=cap_buf):
            handle_media_stream(h_bare, "test_slug/clip/S01")
        self.assertEqual(h_bare.sent_status, HTTPStatus.OK)
        self.assertTrue(cap_buf.getvalue().startswith(b"ACT_1920_P2"))

        # Frontend JS must always include take=<active_take_id> when previewing active take (never bare scene id)
        takes_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "takes_and_history.js").read_text(encoding="utf-8")
        self.assertIn("?take=${encodeURIComponent(chosenTake.take_id)}", takes_js)
        scenes_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "scenes_tab.js").read_text(encoding="utf-8")
        self.assertIn("?take=${encodeURIComponent(t.take_id)}", scenes_js)
        insp_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "scene_inspector.js").read_text(encoding="utf-8")
        self.assertIn("canon?.filename", insp_js)

    # =========================================================================
    # USER JOURNEY (UJ) END-TO-END TESTS
    # =========================================================================

    def test_11_uj_render_cancellation_and_crash_recovery(self):
        """UJ 1: Mid-render cancellation or failure restores prior active take without data loss."""
        # 1. Establish initial active render Take 1
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"take1_stable_content_" + b"x" * 60000)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        ctx = MockContext(self.proj_dir, self.config_file)
        sc = self.initial_project_data["scenes"][0]
        out_name = "S01_solo_768x432"
        state = {"task_hashes": {out_name: "hash_s01_v1"}}

        # 2. Start rendering Take 2: active file is safely unlinked (or left as 0-byte stub)
        before_scene_render(ctx, sc, out_name, s01_render, state)
        self.assertFalse(s01_render.exists())
        s01_render.write_bytes(b"")

        # 3. Simulate server.kill_active_render mid-render (without scene_id in queue item)
        import server
        orig_pdir = server.PROJECTS_DIR
        try:
            server.PROJECTS_DIR = Path(self.test_dir)
            q_file = self.jobs_dir / "v3_queue.json"
            q_file.write_text(json.dumps([{"output_filename": out_name, "status": "rendering", "needs_render": True}]), encoding="utf-8")
            server.kill_active_render(target_slug=self.proj_dir.name)
        finally:
            server.PROJECTS_DIR = orig_pdir

        # 4. Verify previous active take was cleanly restored over the 0-byte stub
        self.assertTrue(s01_render.is_file())
        self.assertEqual(s01_render.read_bytes()[:21], b"take1_stable_content_")

    def test_12_uj_timeline_undo_auto_hit_vault_promotion(self):
        """UJ 2: Timeline Undo -> Reverted JSON -> Fast queue auto-promotes matching vault take."""
        import server
        server.PROJECTS_DIR = Path(self.test_dir)

        ctx = ProjectContext(self.config_file)
        real_h = compute_project_task_hashes(ctx, target_resolution="768x432")
        h_v1 = real_h["S01_solo_768x432"][0]

        # 1. S01 rendered as Take 1
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"take_1_video_bytes_" + b"1" * 60000)

        state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
        state.setdefault("tasks", {})["S01_solo_768x432"] = h_v1
        state.setdefault("task_hashes", {})["S01_solo_768x432"] = h_v1
        save_state_hashes_data(self.proj_dir, state)

        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        # Verify initial queue is up to date
        q1 = server.compute_fast_project_queue("test_slug")
        s01_q1 = next(item for item in q1 if item.get("scene_id") == "S01")
        self.assertEqual(s01_q1["status"], "completed")

        # 2. User edits scene seed and renders Take 2
        sc = self.initial_project_data["scenes"][0]
        out_name = "S01_solo_768x432"

        before_scene_render(ctx, sc, out_name, s01_render, state)
        s01_render.write_bytes(b"take_2_video_bytes_" + b"2" * 60000)

        p_data = json.loads(self.config_file.read_text(encoding="utf-8"))
        p_data["scenes"][0]["seed"] = 2002
        self.config_file.write_text(json.dumps(p_data, indent=2), encoding="utf-8")

        ctx2 = ProjectContext(self.config_file)
        real_h2 = compute_project_task_hashes(ctx2, target_resolution="768x432")
        h_v2 = real_h2["S01_solo_768x432"][0]

        t2_dict = dict(sc, seed=2002)
        tk2 = after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash=h_v2, t_dict=t2_dict, elapsed=35.0, state=state
        )
        # Take 1 remains active until user explicitly selects Take 2; activate Take 2 before testing Undo back to Take 1
        self.assertEqual(s01_render.read_bytes()[:19], b"take_1_video_bytes_")
        p_data_act = promote_take_to_active(self.proj_dir, self.config_file, "S01", tk2["take_id"])
        self.assertEqual(p_data_act["active_take_id"], tk2["take_id"])

        state["tasks"][out_name] = h_v2
        state["task_hashes"][out_name] = h_v2
        save_state_hashes_data(self.proj_dir, state)
        self.assertEqual(s01_render.read_bytes()[:19], b"take_2_video_bytes_")

        # 3. User hits Undo (Ctrl+Z): project JSON reverts back to seed 1001 (Take 1)
        p_data = json.loads(self.config_file.read_text(encoding="utf-8"))
        p_data["scenes"][0]["seed"] = 1001
        self.config_file.write_text(json.dumps(p_data, indent=2), encoding="utf-8")

        # 4. Fast queue evaluates: detects vault hit and auto-promotes Take 1
        q2 = server.compute_fast_project_queue("test_slug")
        s01_q2 = next(item for item in q2 if item.get("scene_id") == "S01")
        self.assertEqual(s01_q2["status"], "completed")
        self.assertIn("VAULT HIT", s01_q2["reason"])
        self.assertFalse(s01_q2["needs_render"])

        # 5. Active file is automatically restored to Take 1
        self.assertEqual(s01_render.read_bytes()[:19], b"take_1_video_bytes_")

    def test_13_uj_scene_inspector_take_preview_diff_and_activation(self):
        """UJ 3: Scene Inspector take browser, historical preview streaming, and activation."""
        import server
        from server_media import handle_media_stream
        server.PROJECTS_DIR = Path(self.test_dir)

        # 1. Seed baseline Take 1
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"take1_cinematic_v1_" + b"x" * 60000)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        # 2. Render Take 2
        ctx = MockContext(self.proj_dir, self.config_file)
        sc = self.initial_project_data["scenes"][0]
        out_name = "S01_solo_768x432"
        state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))

        before_scene_render(ctx, sc, out_name, s01_render, state)
        s01_render.write_bytes(b"take2_experimental_v2_" + b"y" * 60000)
        t2_dict = dict(sc, prompt="Experimental prompt", seed=7777)
        after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash="hash_s01_v2", t_dict=t2_dict, elapsed=38.0, state=state
        )

        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        cfg["scenes"][0]["prompt"] = "Experimental prompt"
        cfg["scenes"][0]["seed"] = 7777
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        # 3. User selects S01 in Inspector: Take 1 remained active; Take 2 was added to vault without replacing Take 1
        h_get = DummyRequestHandler()
        handle_takes_api(h_get, "get_takes", path="/api/takes/test_slug/S01")
        self.assertEqual(h_get.sent_status, HTTPStatus.OK)
        takes_list = h_get.sent_json_data["takes"]
        self.assertEqual(len(takes_list), 2)
        take1_id = takes_list[0]["take_id"]
        take2_id = takes_list[1]["take_id"]
        self.assertEqual(h_get.sent_json_data["active_take_id"], take1_id)

        # User clicks 'Use' on Take 2 first, verifying activation switches cleanly, then restores Take 1 below
        h_act2 = DummyRequestHandler()
        handle_takes_api(h_act2, "activate", body={"project": "test_slug", "scene_id": "S01", "take_id": take2_id})
        self.assertEqual(h_act2.sent_status, HTTPStatus.OK)
        self.assertEqual(s01_render.read_bytes()[:22], b"take2_experimental_v2_")

        # 4. User previews Take 1 video stream
        class PreviewStreamMock:
            def __init__(self, path):
                self.path = path
                self.sent_status = None
                self.headers = {}
            def send_response(self, code): self.sent_status = code
            def send_header(self, k, v): pass
            def end_headers(self): pass
            def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST): self.sent_status = status
            @property
            def wfile(self):
                import io
                return io.BytesIO()

        h_stream = PreviewStreamMock(f"/media/test_slug/clip/{out_name}.mp4?take={take1_id}")
        handle_media_stream(h_stream, f"test_slug/clip/{out_name}.mp4")
        self.assertEqual(h_stream.sent_status, HTTPStatus.OK)

        # 5. User clicks 'Restore This Take' button: POST /api/takes/activate
        h_act = DummyRequestHandler()
        handle_takes_api(
            h_act, "activate",
            body={"project": "test_slug", "scene_id": "S01", "take_id": take1_id}
        )
        self.assertEqual(h_act.sent_status, HTTPStatus.OK)
        self.assertTrue(h_act.sent_json_data["success"])

        # 6. Verify project.json and active file restored to Take 1
        restored_cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(restored_cfg["scenes"][0]["prompt"], "Solo vocalist singing intro")
        self.assertEqual(restored_cfg["scenes"][0]["seed"], 1001)
        self.assertEqual(s01_render.read_bytes()[:19], b"take1_cinematic_v1_")

    def test_14_uj_two_pass_duet_selective_pass2_take_promotion(self):
        """UJ 4: Two-Pass Duet GVA scene allows restoring Pass 2 take while preserving Pass 1 intact."""
        # 1. Setup Two-Pass Duet scene S02
        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        s02 = cfg["scenes"][1]
        s02["mode"] = "gva_male_left_silent_right"
        s02["prompt_m"] = "Male lead vocals left"
        s02["prompt_f"] = "Female listening right"
        s02["seed_m"] = 8101
        s02["seed_f"] = 8201
        s02["split_x"] = 0.50
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        p1_render = self.renders_dir / "S02_Pass1_Left_768x432.mp4"
        p2_render = self.renders_dir / "S02_Pass2_Final_768x432.mp4"
        p1_render.write_bytes(b"p1_male_pass1_bytes_" + b"m" * 60000)
        p2_render.write_bytes(b"p2_take1_female_bytes_" + b"f" * 60000)

        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)
        manifest = load_takes_manifest(self.proj_dir)
        p2_take1_id = manifest["scenes"]["S02"]["takes"][1]["take_id"]

        # 2. User re-renders Pass 2 with new female prompt & seed
        ctx = MockContext(self.proj_dir, self.config_file)
        out_p2 = "S02_Pass2_Final_768x432"
        state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))

        before_scene_render(ctx, s02, out_p2, p2_render, state)
        p2_render.write_bytes(b"p2_take2_female_new_bytes_" + b"g" * 60000)
        t2_dict = dict(s02, prompt_f="Female singing harmony", seed_f=9999)
        after_scene_render(
            ctx, s02, pass_num=2, out_name=out_p2, out_mp4=p2_render,
            t_hash="hash_p2_v2", t_dict=t2_dict, elapsed=45.0, state=state
        )

        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        cfg["scenes"][1]["prompt_f"] = "Female singing harmony"
        cfg["scenes"][1]["seed_f"] = 9999
        cfg["scenes"][1]["split_x"] = 0.48
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        # 3. User restores Pass 2 Take 1 via promote_take_to_active with pass_num=2
        res = promote_take_to_active(
            self.proj_dir, self.config_file, "S02",
            take_id_or_pass=2, take_id=p2_take1_id
        )
        self.assertTrue(res["success"])

        # 4. Verify only Pass 2 fields restored; Pass 1 fields intact
        updated_cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        sc_updated = updated_cfg["scenes"][1]
        self.assertEqual(sc_updated["prompt_f"], "Female listening right")
        self.assertEqual(sc_updated["seed_f"], 8201)
        self.assertEqual(sc_updated["prompt_m"], "Male lead vocals left")
        self.assertEqual(sc_updated["seed_m"], 8101)
        self.assertEqual(sc_updated["split_x"], 0.48)
        self.assertEqual(p2_render.read_bytes()[:22], b"p2_take1_female_bytes_")

    def test_15_uj_multi_scene_checkpoint_and_zero_gpu_rollback(self):
        """UJ 5: Multi-scene checkpoint creation and zero-GPU rollback."""
        import server
        server.PROJECTS_DIR = Path(self.test_dir)

        ctx = ProjectContext(self.config_file)
        real_h = compute_project_task_hashes(ctx, target_resolution="768x432")
        h_s01_init = real_h["S01_solo_768x432"][0]
        h_s02_init = real_h["S02_solo_768x432"][0]

        # 1. Render both S01 and S02
        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s02_render = self.renders_dir / "S02_solo_768x432.mp4"
        s01_render.write_bytes(b"s01_milestone_alpha_" + b"a" * 60000)
        s02_render.write_bytes(b"s02_milestone_alpha_" + b"b" * 60000)

        state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
        for k in ("tasks", "task_hashes"):
            state.setdefault(k, {}).update({"S01_solo_768x432": h_s01_init, "S02_solo_768x432": h_s02_init})
        save_state_hashes_data(self.proj_dir, state)

        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        # 2. User creates Checkpoint 'Milestone Alpha' via REST API
        h_cp = DummyRequestHandler()
        handle_takes_api(
            h_cp, "create",
            body={"project": "test_slug", "name": "Milestone Alpha", "comment": "Good rough cut"}
        )
        self.assertEqual(h_cp.sent_status, HTTPStatus.OK)
        cp_id = h_cp.sent_json_data["checkpoint"]["id"]

        # 3. User performs extensive multi-scene changes
        ctx = MockContext(self.proj_dir, self.config_file)
        state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))

        # Edit and re-render S01
        sc1 = self.initial_project_data["scenes"][0]
        before_scene_render(ctx, sc1, "S01_solo_768x432", s01_render, state)
        s01_render.write_bytes(b"s01_experimental_failed_take_" + b"f" * 60000)
        after_scene_render(
            ctx, sc1, pass_num=None, out_name="S01_solo_768x432", out_mp4=s01_render,
            t_hash="h_s01_exp", t_dict=dict(sc1, seed=8888), elapsed=30.0, state=state
        )

        # Edit and re-render S02
        sc2 = self.initial_project_data["scenes"][1]
        before_scene_render(ctx, sc2, "S02_solo_768x432", s02_render, state)
        s02_render.write_bytes(b"s02_experimental_failed_take_" + b"g" * 60000)
        after_scene_render(
            ctx, sc2, pass_num=None, out_name="S02_solo_768x432", out_mp4=s02_render,
            t_hash="h_s02_exp", t_dict=dict(sc2, seed=9999), elapsed=32.0, state=state
        )

        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        cfg["scenes"][0]["seed"] = 8888
        cfg["scenes"][1]["seed"] = 9999
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        # 4. User decides the experiment failed: triggers Rollback to Milestone Alpha
        h_roll = DummyRequestHandler()
        handle_takes_api(
            h_roll, "rollback",
            body={"project": "test_slug", "checkpoint_id": cp_id}
        )
        self.assertEqual(h_roll.sent_status, HTTPStatus.OK)
        self.assertTrue(h_roll.sent_json_data["success"])

        # 5. Verify pre_rollback checkpoint was automatically created
        cps = list_project_checkpoints(self.proj_dir)
        triggers = [c.get("trigger") for c in cps]
        self.assertIn("pre_rollback", triggers)

        # 6. Verify project config restored
        restored_cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(restored_cfg["scenes"][0]["seed"], 1001)
        self.assertEqual(restored_cfg["scenes"][1]["seed"], 1002)

        # 7. Verify both scene MP4s restored to Milestone Alpha
        self.assertEqual(s01_render.read_bytes()[:20], b"s01_milestone_alpha_")
        self.assertEqual(s02_render.read_bytes()[:20], b"s02_milestone_alpha_")

        # 8. Verify fast queue reports UP-TO-DATE for both scenes
        queue = server.compute_fast_project_queue("test_slug")
        for q_item in queue:
            self.assertEqual(q_item["status"], "completed")
            self.assertFalse(q_item["needs_render"])

    def test_16_uj_revert_edited_active_scene_to_rendered_take(self):
        """UJ 6: Edited active scene reverts cleanly via /api/takes/activate and keeps clip UP-TO-DATE."""
        import server
        server.PROJECTS_DIR = Path(self.test_dir)

        ctx = ProjectContext(self.config_file)
        real_h = compute_project_task_hashes(ctx, target_resolution="768x432")
        h_s01 = real_h["S01_solo_768x432"][0]

        s01_render = self.renders_dir / "S01_solo_768x432.mp4"
        s01_render.write_bytes(b"s01_rendered_on_disk_" + b"x" * 60000)
        state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
        state.setdefault("tasks", {})["S01_solo_768x432"] = h_s01
        state.setdefault("task_hashes", {})["S01_solo_768x432"] = h_s01
        state.setdefault("scene_renders", {})["S01_solo_768x432"] = {"render_start": 0.0, "render_end": 5.0}
        save_state_hashes_data(self.proj_dir, state)
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)

        manifest = load_takes_manifest(self.proj_dir)
        take1_id = manifest["scenes"]["S01"]["active_take_id"]

        # Edit S01 heavily: prompt, seed, resolution override, upsampling, filters, negative prompt, timing
        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        sc1 = cfg["scenes"][0]
        sc1["prompt"] = "Totally changed prompt"
        sc1["seed"] = 99999
        sc1["start"] = 0.5
        sc1["end"] = 6.5
        sc1["resolution"] = "1280x720"
        sc1["spatial_upsampling"] = "flashvsrplus*2"
        sc1["filters"] = {"film_grain": 0.25, "deband": False}
        sc1["negative_prompt"] = "extra blur"
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        q_edited = server.compute_fast_project_queue("test_slug", force_recheck=True)
        s01_edited = next(x for x in q_edited if x.get("scene_id") == "S01")
        self.assertTrue(s01_edited["needs_render"])

        # Revert scene via POST /api/takes/activate with restore_timing=True
        h_act = DummyRequestHandler()
        handle_takes_api(
            h_act, "activate",
            body={"project": "test_slug", "scene_id": "S01", "take_id": take1_id, "restore_timing": True},
        )
        self.assertEqual(h_act.sent_status, HTTPStatus.OK)
        self.assertTrue(h_act.sent_json_data["success"])

        restored_sc = json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][0]
        self.assertEqual(restored_sc["prompt"], "Solo vocalist singing intro")
        self.assertEqual(restored_sc["seed"], 1001)
        self.assertEqual(restored_sc["start"], 0.0)
        self.assertEqual(restored_sc["end"], 5.0)
        for added_key in ("resolution", "spatial_upsampling", "filters", "negative_prompt"):
            self.assertNotIn(added_key, restored_sc)
        self.assertEqual(s01_render.read_bytes()[:21], b"s01_rendered_on_disk_")

        q_reverted = server.compute_fast_project_queue("test_slug", force_recheck=True)
        s01_reverted = next(x for x in q_reverted if x.get("scene_id") == "S01")
        self.assertEqual(s01_reverted["status"], "completed")
        self.assertFalse(s01_reverted["needs_render"])

    def test_17_vault_discovery_dedup_and_delete_take(self):
        """Verify vault MP4 discovery, duplicate ID dedup, lock preservation, and take deletion."""
        import server
        orig_pdir = server.PROJECTS_DIR
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            # 1. Pre-seed duplicate take_baseline_default entries + unindexed vault MP4s
            s01_render = self.renders_dir / "S01_solo_768x432.mp4"
            s01_render.write_bytes(b"active_s01_" + b"a" * 60000)

            s01_vault = self.renders_dir / "takes" / "S01"
            s01_vault.mkdir(parents=True, exist_ok=True)
            dup1 = s01_vault / "dup1.mp4"
            dup2 = s01_vault / "dup2.mp4"
            vault_a = s01_vault / "S01_solo_768x432_vA.mp4"
            vault_b = s01_vault / "S01_solo_768x432_vB.mp4"
            dup1.write_bytes(b"dup1_bytes_" + b"b" * 60000)
            dup2.write_bytes(b"dup2_bytes_" + b"c" * 60000)
            vault_a.write_bytes(b"vault_alpha_" + b"d" * 60000)
            vault_b.write_bytes(b"vault_beta_" + b"e" * 60000)

            legacy_manifest = {
                "version": 1,
                "scenes": {
                    "S01": {
                        "active_take_id": "take_baseline_default",
                        "active_takes": {"S01_solo_768x432": "take_baseline_default"},
                        "takes": [
                            {"take_id": "take_baseline_default", "scene_id": "S01", "out_name": "S01_solo_768x432", "vault_rel_mp4": "takes/S01/dup1.mp4", "created_at": "2026-11-01T10:00:00Z"},
                            {"take_id": "take_baseline_default", "scene_id": "S01", "out_name": "S01_solo_768x432", "vault_rel_mp4": "takes/S01/dup2.mp4", "created_at": "2026-11-02T10:00:00Z"},
                        ],
                    }
                },
            }
            save_takes_manifest(self.proj_dir, legacy_manifest)

            # GET /api/takes/<slug>/<sid> must trigger vault sync even when takes list is non-empty
            h_get = DummyRequestHandler()
            handle_takes_api(h_get, "get_takes", path="/api/takes/test_slug/S01")
            self.assertEqual(h_get.sent_status, HTTPStatus.OK)
            takes_s01 = h_get.sent_json_data["takes"]
            self.assertEqual(len(takes_s01), 4)
            self.assertEqual(len({t["take_id"] for t in takes_s01}), 4)
            rels_s01 = {t.get("vault_rel_mp4") for t in takes_s01}
            self.assertIn("takes/S01/S01_solo_768x432_vA.mp4", rels_s01)
            self.assertIn("takes/S01/S01_solo_768x432_vB.mp4", rels_s01)

            # 2. Delete a historical (non-active) take via API -> unlinks vault file, keeps active MP4
            active_tid = h_get.sent_json_data["active_take_id"]
            hist_t = next(t for t in takes_s01 if t["take_id"] != active_tid and "vA" in (t.get("vault_rel_mp4") or ""))
            h_del = DummyRequestHandler()
            handle_takes_api(h_del, "delete_take", body={"project": "test_slug", "scene_id": "S01", "take_id": hist_t["take_id"]})
            self.assertEqual(h_del.sent_status, HTTPStatus.OK)
            self.assertTrue(h_del.sent_json_data["success"])
            self.assertEqual(len(h_del.sent_json_data["takes"]), 3)
            self.assertEqual([t["take_number"] for t in h_del.sent_json_data["takes"]], [1, 2, 3])
            self.assertFalse(vault_a.exists())
            self.assertEqual(s01_render.read_bytes()[:11], b"active_s01_")

            # 3. Lock S02, render 2 takes, verify promote_take_to_active preserves locked=True
            ctx = MockContext(self.proj_dir, self.config_file)
            sc2 = self.initial_project_data["scenes"][1]
            out_s02 = "S02_solo_768x432"
            s02_render = self.renders_dir / f"{out_s02}.mp4"
            state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))

            before_scene_render(ctx, sc2, out_s02, s02_render, state)
            s02_render.write_bytes(b"s02_take1_" + b"1" * 60000)
            after_scene_render(ctx, sc2, pass_num=None, out_name=out_s02, out_mp4=s02_render, t_hash="h_s02_t1", t_dict=dict(sc2, seed=1002), elapsed=10.0, state=state)
            before_scene_render(ctx, sc2, out_s02, s02_render, state)
            s02_render.write_bytes(b"s02_take2_" + b"2" * 60000)
            after_scene_render(ctx, sc2, pass_num=None, out_name=out_s02, out_mp4=s02_render, t_hash="h_s02_t2", t_dict=dict(sc2, seed=2002), elapsed=12.0, state=state)

            state = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
            state.setdefault("tasks", {})[out_s02] = "h_s02_t2"
            state.setdefault("task_hashes", {})[out_s02] = "h_s02_t2"
            save_state_hashes_data(self.proj_dir, state)

            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["scenes"][1]["locked"] = True
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            s02_takes = load_takes_manifest(self.proj_dir)["scenes"]["S02"]["takes"]
            t1_id = next(t["take_id"] for t in s02_takes if t.get("task_hash") == "h_s02_t1")
            t2_id = next(t["take_id"] for t in s02_takes if t.get("task_hash") == "h_s02_t2")

            prom = promote_take_to_active(self.proj_dir, self.config_file, "S02", t1_id)
            self.assertTrue(prom["success"])
            self.assertTrue(json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][1].get("locked"))
            self.assertEqual(s02_render.read_bytes()[:10], b"s02_take1_")

            # 4. Deleting active Take 1 auto-promotes remaining Take 2 and updates state_hashes
            del1 = delete_scene_take(self.proj_dir, self.config_file, "S02", t1_id)
            self.assertTrue(del1["success"])
            self.assertEqual(del1["active_take_id"], t2_id)
            self.assertEqual(len(del1["takes"]), 1)
            self.assertEqual(s02_render.read_bytes()[:10], b"s02_take2_")
            sh_after_del1 = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
            self.assertEqual(sh_after_del1["task_hashes"].get(out_s02), "h_s02_t2")

            # 5. Deleting last remaining take unlinks active MP4 and clears state_hashes
            del2 = delete_scene_take(self.proj_dir, self.config_file, "S02", t2_id)
            self.assertTrue(del2["success"])
            self.assertEqual(del2["takes"], [])
            self.assertIsNone(del2["active_take_id"])
            self.assertFalse(s02_render.exists())
            sh_after_del2 = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
            self.assertNotIn(out_s02, sh_after_del2.get("task_hashes", {}))
            self.assertNotIn(out_s02, sh_after_del2.get("tasks", {}))

            # 6. Deleting missing take returns NOT_FOUND
            h_missing = DummyRequestHandler()
            handle_takes_api(h_missing, "delete_take", body={"project": "test_slug", "scene_id": "S02", "take_id": "nonexistent_take"})
            self.assertEqual(h_missing.sent_status, HTTPStatus.NOT_FOUND)
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_18_revert_scene_after_default_resolution_change_and_pass2(self):
        """Regression: reverting a scene rendered under an older project default resolution restores resolution & clears queue."""
        import server
        orig_pdir = getattr(server, "PROJECTS_DIR", None)
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            # Configure project at preset 3 (1280x720) with a two-pass duet scene inheriting default resolution
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["models"] = {"default_video_resolution": 3}
            cfg["scenes"] = [{
                "id": "S20", "mode": "duet_method_b", "output_name": "S20_Duet",
                "start": 0.0, "end": 5.0, "anchor": "A01", "split_x": 0.5,
                "prompt_m": "Male vocalist left", "seed_m": 1111, "stem_m": "master.wav",
                "prompt_f": "Female vocalist right", "seed_f": 2222, "stem_f": "master.wav",
            }]
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            p1_name, p2_name = "S20_Pass1_Left_1280x720", "S20_Pass2_Final_1280x720"
            p1_mp4, p2_mp4 = self.renders_dir / f"{p1_name}.mp4", self.renders_dir / f"{p2_name}.mp4"
            p1_mp4.write_bytes(b"p1_1280x720_" + b"a" * 60000)
            p2_mp4.write_bytes(b"p2_1280x720_" + b"b" * 60000)

            ctx = ProjectContext(self.config_file)
            real_h = compute_project_task_hashes(ctx)
            h_p1, h_p2 = real_h[p1_name][0], real_h[p2_name][0]

            state = {"tasks": {p1_name: h_p1, p2_name: h_p2}, "task_hashes": {p1_name: h_p1, p2_name: h_p2}, "scene_renders": {}}
            save_state_hashes_data(self.proj_dir, state)
            sc20 = json.loads(json.dumps(cfg["scenes"][0]))
            after_scene_render(ctx, sc20, pass_num=1, out_name=p1_name, out_mp4=p1_mp4, t_hash=h_p1, t_dict=dict(sc20, resolution="1280x720"), elapsed=20.0, state=state)
            after_scene_render(ctx, sc20, pass_num=2, out_name=p2_name, out_mp4=p2_mp4, t_hash=h_p2, t_dict=dict(sc20, resolution="1280x720"), elapsed=25.0, state=state)

            manifest = load_takes_manifest(self.proj_dir)
            p2_tid = manifest["scenes"]["S20"]["active_take_id"]

            # Change project default resolution to 6 (1920x1088) and add stale scene edits (no resolution on S20)
            cfg["models"]["default_video_resolution"] = 6
            cfg["scenes"][0]["prompt_f"] = "Edited female prompt"
            cfg["scenes"][0]["spatial_upsampling"] = "flashvsrplus*2"
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            # Simulate Pass 1 newer on disk too (common after intermediate P1 run)
            os.utime(p1_mp4, (time.time() + 10.0, time.time() + 10.0))

            q_before = server.compute_fast_project_queue("test_slug", force_recheck=True)
            self.assertTrue(all(x["needs_render"] for x in q_before if x.get("scene_id") == "S20"))

            # 1) Revert via POST /api/takes/activate (pass_num=None path)
            h_act = DummyRequestHandler()
            handle_takes_api(h_act, "activate", body={"project": "test_slug", "scene_id": "S20", "take_id": p2_tid})
            self.assertEqual(h_act.sent_status, HTTPStatus.OK)
            restored = json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][0]
            self.assertEqual(restored.get("resolution"), 3)
            self.assertEqual(restored.get("prompt_f"), "Female vocalist right")
            self.assertNotIn("spatial_upsampling", restored)

            q_after = server.compute_fast_project_queue("test_slug", force_recheck=True)
            s20_items = [x for x in q_after if x.get("scene_id") == "S20"]
            self.assertEqual(len(s20_items), 2)
            self.assertTrue(all(x["status"] == "completed" and not x["needs_render"] for x in s20_items))

            # 2) Also verify pass_num=2 direct activation restores resolution & clears stale upsampling
            cfg2 = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg2["scenes"][0].pop("resolution", None)
            cfg2["scenes"][0]["spatial_upsampling"] = "flashvsrplus*2"
            self.config_file.write_text(json.dumps(cfg2, indent=2), encoding="utf-8")
            prom_p2 = promote_take_to_active(self.proj_dir, self.config_file, "S20", 2, take_id=p2_tid)
            self.assertTrue(prom_p2["success"])
            restored2 = json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][0]
            self.assertEqual(restored2.get("resolution"), 3)
            self.assertNotIn("spatial_upsampling", restored2)
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_19_restore_take_preserves_timeline_sync_on_overlapping_or_gapping_snapshot(self):
        """Regression: reverting a take with older timing never silently breaks timeline continuity or desyncs downstream scenes."""
        import subprocess
        node_script = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const els = new Map(), mkEl = () => ({ style: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false }, dataset: {}, addEventListener() {}, querySelectorAll: () => [], appendChild() {} });
["btnUndo","btnRedo","btnCheckpointsModal","btnCloseCheckpointsModal","btnCreateCheckpoint","btnDiscardChanges","btnSaveProject","inspectorForm","btnToggleSceneLock","btnDeleteScene","btnRevertSceneToRendered","btnSplitSceneHeader"].forEach((id) => els.set(id, mkEl()));
const fetchCalls = [], toasts = [];
let confirms = 0;
const state = {
  currentSlug: "sync_proj",
  selectedSceneIdx: 1,
  projectData: {
    project: { name: "Sync Test", fps: 24 },
    scenes: [
      { id: "S04b", start: 24.69, end: 29.62, prompt: "b" },
      { id: "S05", start: 29.62, end: 33.4, prompt: "New", seed: 8206 },
      { id: "S06", start: 33.4, end: 39.6, prompt: "next" },
    ],
  },
};
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN,
  document: { readyState: "complete", getElementById: (id) => els.get(id) || null, createElement: mkEl, addEventListener() {} },
  window: {}, addEventListener() {}, state, markProjectDirty() {}, redrawTimeline() {}, renderSceneList() {}, selectScene() {}, loadProjectQueue() {},
  showToast: (m) => toasts.push(m),
  confirm: () => { confirms++; return true; },
  alert: (m) => { throw new Error("Unexpected alert: " + m); },
  fetch: async (url, opts) => {
    const body = JSON.parse(opts.body);
    fetchCalls.push(body);
    const sc = state.projectData.scenes.find((s) => s.id === body.scene_id);
    const snap = body.take_id === "t_overlap" ? { id: "S05", start: 27.2, end: 33.4, prompt: "Old", seed: 8205 }
      : body.take_id === "t_gap" ? { id: "S05", start: 30.5, end: 33.4, prompt: "Gap", seed: 8204 }
      : { id: "S05", start: 29.62, end: 33.4, prompt: "Fixed", seed: 8207 };
    const out = Object.assign({}, snap);
    if (!body.restore_timing) { out.start = sc.start; out.end = sc.end; }
    return { ok: true, json: async () => ({ success: true, restored_params: out }) };
  },
  setTimeout, clearTimeout,
});
ctx.window = ctx; ctx.window.state = state;
vm.runInContext(fs.readFileSync("work/ui/static/js/takes_and_history.js", "utf8"), ctx);
(async () => {
  ctx.initProjectHistory();
  // 1) Older pre-split take overlapping S04b must keep S05 at 29.62..33.4 with zero confirm prompt
  await ctx.restoreSceneToTake(state.projectData.scenes[1], { take_id: "t_overlap", take_number: 5, scene_snapshot: { start: 27.2, end: 33.4, prompt: "Old", seed: 8205 } });
  assert.strictEqual(fetchCalls[0].restore_timing, false);
  assert.strictEqual(state.projectData.scenes[1].start, 29.62);
  assert.strictEqual(state.projectData.scenes[1].seed, 8205);
  assert.strictEqual(confirms, 0);
  // 2) Shrinking take leaving a gap after S04b must also keep 29.62..33.4
  await ctx.restoreSceneToTake(state.projectData.scenes[1], { take_id: "t_gap", take_number: 4, scene_snapshot: { start: 30.5, end: 33.4, prompt: "Gap", seed: 8204 } });
  assert.strictEqual(fetchCalls[1].restore_timing, false);
  assert.strictEqual(state.projectData.scenes[1].start, 29.62);
  // 3) Repairing a broken overlap back to contiguous snapshot timing restores 29.62..33.4 automatically
  state.projectData.scenes[1].start = 27.2;
  await ctx.restoreSceneToTake(state.projectData.scenes[1], { take_id: "t_fix", take_number: 6, scene_snapshot: { start: 29.62, end: 33.4, prompt: "Fixed", seed: 8207 } });
  assert.strictEqual(fetchCalls[2].restore_timing, true);
  assert.strictEqual(state.projectData.scenes[1].start, 29.62);
  console.log("RESTORE_TIMING_SYNC_OK");
})();
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"STDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("RESTORE_TIMING_SYNC_OK", res.stdout)

    def test_20_stage3_master_clamps_overlap_and_fills_gap_preserving_downstream_sync(self):
        """Regression: Stage 3 master stitch clamps scene overlaps and fills gaps so S06+ stay synced to audio."""
        import subprocess
        from pipeline.media_utils import probe_video_duration, resolve_ffmpeg_path
        from pipeline.stage3_master import assemble_progressive_master
        ffmpeg = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        s01_mp4 = self.renders_dir / "S01_solo_768x432.mp4"
        s02_mp4 = self.renders_dir / "S02_solo_768x432.mp4"
        s03_mp4 = self.renders_dir / "S03_solo_768x432.mp4"
        for mp4, dur in ((s01_mp4, 2.5), (s02_mp4, 3.5), (s03_mp4, 2.5)):
            subprocess.run(
                [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"testsrc2=s=768x432:r=24:d={dur}", "-c:v", "libx264", "-preset", "ultrafast", str(mp4)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        cfg["project"]["total_duration"] = 0
        cfg["scenes"] = [
            {"id": "S01", "start": 0.0, "end": 2.0, "mode": "solo", "prompt": "a"},
            {"id": "S02", "start": 1.0, "end": 4.0, "mode": "solo", "prompt": "b"},
            {"id": "S03", "start": 5.0, "end": 6.0, "mode": "solo", "prompt": "c"},
        ]
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        ctx = ProjectContext(self.config_file)
        ok = assemble_progressive_master(ctx, [
            {"id": "S01", "start": 0.0, "end": 2.0, "clip": s01_mp4.name, "mode": "solo"},
            {"id": "S02", "start": 1.0, "end": 4.0, "clip": s02_mp4.name, "mode": "solo"},
            {"id": "S03", "start": 5.0, "end": 6.0, "clip": s03_mp4.name, "mode": "solo"},
        ])
        self.assertTrue(ok)
        self.assertAlmostEqual(probe_video_duration(ffmpeg, ctx.master_output), 6.0, delta=0.12)

    def test_21_safe_link_or_copy_retries_win_lock_and_promote_preserves_json_on_lock(self):
        """Regression: transient WinError 32 retries cleanly, and persistent lock never leaves project.json half-reverted."""
        from unittest import mock
        src1 = self.renders_dir / "lock_src1.mp4"
        src2 = self.renders_dir / "lock_src2.mp4"
        dst = self.renders_dir / "S01_solo_768x432.mp4"
        src1.write_bytes(b"v1_bytes_" + b"a" * 60000)
        src2.write_bytes(b"v2_bytes_" + b"b" * 60000)
        safe_link_or_copy(src1, dst)

        calls = {"n": 0}
        orig_unlink = Path.unlink
        def flaky_unlink(self_p, *a, **kw):
            if self_p == dst:
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise PermissionError(32, "The process cannot access the file", str(self_p))
            return orig_unlink(self_p, *a, **kw)

        with mock.patch.object(Path, "unlink", flaky_unlink), mock.patch("pipeline.takes.time.sleep"):
            self.assertTrue(safe_link_or_copy(src2, dst))
        self.assertEqual(calls["n"], 3)
        self.assertTrue(dst.read_bytes().startswith(b"v2_bytes_"))

        # Seed takes and simulate persistent lock during promote_take_to_active
        ensure_baseline_takes_seeded(self.proj_dir, self.config_file)
        manifest = load_takes_manifest(self.proj_dir)
        t1_id = manifest["scenes"]["S01"]["active_take_id"]
        cfg_before = self.config_file.read_text(encoding="utf-8")
        edited = json.loads(cfg_before)
        edited["scenes"][0]["prompt"] = "Should remain untouched on lock"
        self.config_file.write_text(json.dumps(edited, indent=2), encoding="utf-8")
        expected_cfg = self.config_file.read_text(encoding="utf-8")

        def locked_unlink(self_p, *a, **kw):
            if self_p == dst:
                raise PermissionError(32, "Locked by video preview", str(self_p))
            return orig_unlink(self_p, *a, **kw)

        with mock.patch.object(Path, "unlink", locked_unlink), mock.patch("pipeline.takes.time.sleep"):
            with self.assertRaises(PermissionError):
                promote_take_to_active(self.proj_dir, self.config_file, "S01", t1_id)
        self.assertEqual(self.config_file.read_text(encoding="utf-8"), expected_cfg)

    def test_22_restore_take_unloads_clip_preview_and_media_releases_handle(self):
        """Regression: clip preview is unloaded before /api/takes/activate and Range streaming releases file handles between writes."""
        import subprocess
        from server_media import stream_file_with_range
        node_script = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const els = new Map();
const mkEl = (id) => ({
  id, style: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
  dataset: {}, attrs: {}, paused: false,
  getAttribute(k) { return this.attrs[k] ?? null; },
  setAttribute(k, v) { this.attrs[k] = String(v); },
  removeAttribute(k) { delete this.attrs[k]; },
  addEventListener() {}, querySelectorAll: () => [], appendChild() {},
  pause() { this.paused = true; },
  load() { events.push("unload:" + (this.attrs.src || "cleared")); },
});
const events = [];
["btnUndo","btnRedo","btnCheckpointsModal","btnCloseCheckpointsModal","btnCreateCheckpoint","btnDiscardChanges","btnSaveProject","inspectorForm","btnToggleSceneLock","btnDeleteScene","btnRevertSceneToRendered","btnSplitSceneHeader"].forEach((id) => els.set(id, mkEl(id)));
const vp = mkEl("clipVideoPlayer");
vp.setAttribute("src", "/media/p/clip/S03.mp4?t=1");
vp.setAttribute("data-loaded-src", "/media/p/clip/S03.mp4?t=1");
els.set("clipVideoPlayer", vp);
const state = { currentSlug: "p", selectedSceneIdx: 0, projectData: { project: { name: "P", fps: 24 }, scenes: [{ id: "S03", start: 12.6, end: 18.72, prompt: "New", seed: 1 }] } };
const takesSrc = fs.readFileSync("work/ui/static/js/takes_and_history.js", "utf8");
const unloadFnSrc = fs.readFileSync("work/ui/static/js/state_and_utils.js", "utf8").match(/function unloadClipVideoPlayer\(\)[\s\S]*?return true;\r?\n\}/)[0];
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN,
  document: { readyState: "complete", getElementById: (id) => els.get(id) || null, createElement: mkEl, addEventListener() {} },
  window: {}, addEventListener() {}, state, markProjectDirty() {}, redrawTimeline() {}, renderSceneList() {}, loadProjectQueue() {},
  showToast() {}, alert: (m) => { throw new Error("Unexpected alert: " + m); },
  confirm: () => true,
  refreshProjectAssets: async () => { events.push("refreshAssets"); },
  selectScene: () => { events.push("selectScene"); },
  fetch: async () => { events.push("fetchActivate"); return { ok: true, json: async () => ({ success: true, restored_params: { id: "S03", start: 12.6, end: 18.72, prompt: "Old", seed: 1 } }) }; },
  setTimeout, clearTimeout,
});
ctx.window = ctx; ctx.window.state = state;
vm.runInContext(unloadFnSrc + "\nwindow.unloadClipVideoPlayer = unloadClipVideoPlayer;\n" + takesSrc, ctx);
(async () => {
  ctx.initProjectHistory();
  await ctx.restoreSceneToTake(state.projectData.scenes[0], { take_id: "t6", take_number: 6, scene_snapshot: { start: 12.6, end: 20.4, prompt: "Old", seed: 1 } });
  assert.deepStrictEqual(events, ["unload:cleared", "fetchActivate", "refreshAssets", "selectScene"]);
  assert.strictEqual(vp.getAttribute("src"), null);
  console.log("UNLOAD_BEFORE_ACTIVATE_OK");
})();
"""
        res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"STDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("UNLOAD_BEFORE_ACTIVATE_OK", res.stdout)

        # Verify aborted range streaming releases OS file handle immediately so replacement works
        big_src1 = self.renders_dir / "big1.mp4"
        big_src2 = self.renders_dir / "big2.mp4"
        active_clip = self.renders_dir / "S03_active_1920x1088.mp4"
        big_src1.write_bytes(b"1" * (1200 * 1024))
        big_src2.write_bytes(b"2" * (1200 * 1024))
        safe_link_or_copy(big_src1, active_clip)

        class _AbortedWriter:
            def __init__(self): self.writes = 0
            def write(self, data):
                self.writes += 1
                if self.writes >= 2: raise ConnectionResetError("client closed")
        class _RangeReq:
            headers = {"Range": "bytes=0-"}
            wfile = _AbortedWriter()
            def send_response(self, *_): pass
            def send_header(self, *_): pass
            def end_headers(self): pass

        stream_file_with_range(_RangeReq(), active_clip)
        self.assertTrue(safe_link_or_copy(big_src2, active_clip))
        self.assertTrue(active_clip.read_bytes().startswith(b"2" * 64))

    def test_23_revert_take_restores_renderer_after_default_model_change(self):
        """Regression: reverting a take rendered under an older default_video_renderer restores renderer ID and isn't overwritten by vault auto-promote."""
        import server
        import subprocess
        orig_pdir = getattr(server, "PROJECTS_DIR", None)
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["models"] = {"default_video_renderer": 1, "default_video_resolution": 3}
            cfg["scenes"] = [{
                "id": "S03", "mode": "solo", "renderer": None, "start": 0.0, "end": 5.0,
                "anchor": "A01.jpg", "prompt": "Singer at mic", "seed": 66105,
            }]
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            out_name = "S03_solo_1280x720"
            s03_mp4 = self.renders_dir / f"{out_name}.mp4"
            s03_mp4.write_bytes(b"take1_minimax_" + b"a" * 60000)

            ctx1 = ProjectContext(self.config_file)
            h1 = compute_project_task_hashes(ctx1)[out_name][0]
            state = {"tasks": {out_name: h1}, "task_hashes": {out_name: h1}, "scene_renders": {}}
            save_state_hashes_data(self.proj_dir, state)
            sc0 = copy_sc = dict(cfg["scenes"][0])
            after_scene_render(
                ctx1, sc0, None, out_name, s03_mp4, h1,
                {"prompt": sc0["prompt"], "seed": sc0["seed"], "model_type": "minimax_h3_fl2va_pruned_pdd", "render_start": 0.0, "render_end": 5.0},
                10.0, state,
            )

            # Switch project default renderer to 14 (ltx2_25_22B_distilled) and render Take 2
            cfg["models"]["default_video_renderer"] = 14
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            ctx2 = ProjectContext(self.config_file)
            h2 = compute_project_task_hashes(ctx2)[out_name][0]
            self.assertNotEqual(h1, h2)
            before_scene_render(ctx2, cfg["scenes"][0], out_name, s03_mp4, state)
            s03_mp4.write_bytes(b"take2_ltx25_" + b"b" * 60000)
            after_scene_render(
                ctx2, dict(cfg["scenes"][0]), None, out_name, s03_mp4, h2,
                {"prompt": sc0["prompt"], "seed": sc0["seed"], "model_type": "ltx2_25_22B_distilled", "render_start": 0.0, "render_end": 5.0},
                12.0, state,
            )
            state["tasks"][out_name] = state["task_hashes"][out_name] = h2
            save_state_hashes_data(self.proj_dir, state)

            manifest = load_takes_manifest(self.proj_dir)
            t1_id, t2_id = manifest["scenes"]["S03"]["takes"][0]["take_id"], manifest["scenes"]["S03"]["takes"][1]["take_id"]
            self.assertEqual(manifest["scenes"]["S03"]["active_take_id"], t1_id)
            promote_take_to_active(self.proj_dir, self.config_file, "S03", t2_id)
            manifest_mid = load_takes_manifest(self.proj_dir)
            self.assertEqual(manifest_mid["scenes"]["S03"]["active_take_id"], t2_id)

            # Activate Take 1 via handle_takes_api (which immediately runs compute_fast_project_queue)
            h_act = DummyRequestHandler()
            handle_takes_api(h_act, "activate", body={"project": "test_slug", "scene_id": "S03", "take_id": t1_id})
            self.assertEqual(h_act.sent_status, HTTPStatus.OK)
            self.assertTrue(h_act.sent_json_data["success"])

            manifest_after = load_takes_manifest(self.proj_dir)
            self.assertEqual(manifest_after["scenes"]["S03"]["active_take_id"], t1_id)
            self.assertTrue(s03_mp4.read_bytes().startswith(b"take1_minimax_"))
            restored = json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][0]
            self.assertEqual(restored.get("renderer"), 1)

            q_after = server.compute_fast_project_queue("test_slug", force_recheck=True)
            s03_q = next(x for x in q_after if x.get("scene_id") == "S03")
            self.assertEqual(s03_q["status"], "completed")
            self.assertFalse(s03_q["needs_render"])

            # Verify JS computeSceneTakeDiffs treats active Take 1 as zero Model diff and Take 2 as Model diff
            node_script = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const els = new Map(), mkEl = () => ({ style: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false }, dataset: {}, addEventListener() {}, querySelectorAll: () => [], appendChild() {} });
const state = {
  currentSlug: "test_slug", selectedSceneIdx: 0,
  models: { video_renderers: { "1": { model_type: "minimax_h3_fl2va_pruned_pdd" }, "14": { model_type: "ltx2_25_22B_distilled" } } },
  projectData: { models: { default_video_renderer: 14, default_video_resolution: 3 }, scenes: [{ id: "S03", renderer: 1, start: 0, end: 5, prompt: "Singer at mic", seed: 66105 }] },
};
const ctx = vm.createContext({
  console, Math, Date, Number, String, Array, Object, Set, Map, JSON, parseInt, parseFloat, isNaN,
  document: { readyState: "complete", getElementById: (id) => els.get(id) || null, createElement: mkEl, addEventListener() {} },
  window: {}, addEventListener() {}, state, setTimeout, clearTimeout,
});
ctx.window = ctx; ctx.window.state = state;
const modFn = fs.readFileSync("work/ui/static/js/state_and_utils.js", "utf8").match(/function resolveSceneModelType[\s\S]*?\r?\n\}/)[0];
vm.runInContext(modFn + "\nwindow.resolveSceneModelType = resolveSceneModelType;\n" + fs.readFileSync("work/ui/static/js/takes_and_history.js", "utf8"), ctx);
const t1 = { take_id: "t1", model_type: "minimax_h3_fl2va_pruned_pdd", scene_snapshot: { id: "S03", renderer: null, start: 0, end: 5, prompt: "Singer at mic", seed: 66105 } };
const t2 = { take_id: "t2", model_type: "ltx2_25_22B_distilled", scene_snapshot: { id: "S03", renderer: null, start: 0, end: 5, prompt: "Singer at mic", seed: 66105 } };
assert.ok(!modFn.includes("BUG") && !ctx.resolveSceneModelType(t1.scene_snapshot, null, t1).includes("ltx"));
const m1 = ctx.resolveSceneModelType(state.projectData.scenes[0], null, null);
assert.strictEqual(m1, "minimax_h3_fl2va_pruned_pdd");
assert.strictEqual(ctx.resolveSceneModelType(t1.scene_snapshot, null, t1), m1);
assert.strictEqual(ctx.resolveSceneModelType(t2.scene_snapshot, null, t2), "ltx2_25_22B_distilled");
console.log("RENDERER_MODEL_DIFF_OK");
"""
            res = subprocess.run(["node", "-e", node_script], cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"STDOUT:{res.stdout}\nSTDERR:{res.stderr}")
            self.assertIn("RENDERER_MODEL_DIFF_OK", res.stdout)
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_24_revert_two_pass_duet_pass2_take_restores_compatible_pass1_and_backend(self):
        """Regression: reverting a Two-Pass Duet Pass 2 take after switching model/backend restores companion Pass 1 + backend_m and clears stale queue curation."""
        import server
        from ui.server_queue import write_queue_json_locked
        orig_pdir = getattr(server, "PROJECTS_DIR", None)
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["models"] = {"default_video_renderer": 14, "default_video_resolution": 3}
            s14 = {
                "id": "S14", "mode": "duet_method_b", "renderer": 9, "start": 0.0, "end": 5.0,
                "split_x": 0.5, "anchor": "A01.jpg",
                "prompt_m": "Male sings left", "prompt_f": "Female sings right",
                "seed_m": 80775, "seed_f": 60554, "stem_m": "male", "stem_f": "female",
            }
            cfg["scenes"] = [s14]
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            out_p1, out_p2 = "S14_Pass1_Left_1280x720", "S14_Pass2_Final_1280x720"
            p1_mp4 = self.renders_dir / f"{out_p1}.mp4"
            p2_mp4 = self.renders_dir / f"{out_p2}.mp4"

            # 1) Render Take 1 (Pass 1 on #9 @ minimax) and Take 2 (Pass 2 on #9 @ local)
            ctx1 = ProjectContext(self.config_file)
            h_old = compute_project_task_hashes(ctx1, backend_overrides={out_p1: "minimax", out_p2: "local"})
            h1_p1, h1_p2 = h_old[out_p1][0], h_old[out_p2][0]
            state = {"tasks": {out_p1: h1_p1, out_p2: h1_p2}, "task_hashes": {out_p1: h1_p1, out_p2: h1_p2}, "scene_renders": {}}
            save_state_hashes_data(self.proj_dir, state)

            p1_mp4.write_bytes(b"take1_ref2va_minimax_" + b"m" * 60000)
            after_scene_render(
                ctx1, dict(s14), 1, out_p1, p1_mp4, h1_p1,
                {"prompt": s14["prompt_m"], "seed": s14["seed_m"], "model_type": "minimax_h3_ref2va_pruned", "backend": "minimax", "render_start": 0.0, "render_end": 5.0},
                10.0, state, created_at=1000.0,
            )
            p2_mp4.write_bytes(b"take2_ref2va_local___" + b"f" * 60000)
            after_scene_render(
                ctx1, dict(s14), 2, out_p2, p2_mp4, h1_p2,
                {"prompt": s14["prompt_f"], "seed": s14["seed_f"], "model_type": "minimax_h3_ref2va_pruned", "backend": "local", "render_start": 0.0, "render_end": 5.0},
                12.0, state, created_at=1100.0,
            )

            # 2) Switch scene to LTX-2.5 (#14 default) and render Take 3 (P1) + Take 4 (P2)
            cfg["scenes"][0]["renderer"] = None
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            ctx2 = ProjectContext(self.config_file)
            h_new = compute_project_task_hashes(ctx2)
            h2_p1, h2_p2 = h_new[out_p1][0], h_new[out_p2][0]

            before_scene_render(ctx2, cfg["scenes"][0], out_p1, p1_mp4, state)
            p1_mp4.write_bytes(b"take3_ltx25_p1_______" + b"x" * 60000)
            after_scene_render(
                ctx2, dict(cfg["scenes"][0]), 1, out_p1, p1_mp4, h2_p1,
                {"prompt": s14["prompt_m"], "seed": s14["seed_m"], "model_type": "ltx2_25_22B_distilled", "backend": "local", "render_start": 0.0, "render_end": 5.0},
                10.0, state, created_at=2000.0,
            )
            before_scene_render(ctx2, cfg["scenes"][0], out_p2, p2_mp4, state)
            p2_mp4.write_bytes(b"take4_ltx25_p2_______" + b"y" * 60000)
            after_scene_render(
                ctx2, dict(cfg["scenes"][0]), 2, out_p2, p2_mp4, h2_p2,
                {"prompt": s14["prompt_f"], "seed": s14["seed_f"], "model_type": "ltx2_25_22B_distilled", "backend": "local", "render_start": 0.0, "render_end": 5.0},
                12.0, state, created_at=2100.0,
            )
            state["tasks"][out_p1] = state["task_hashes"][out_p1] = h2_p1
            state["tasks"][out_p2] = state["task_hashes"][out_p2] = h2_p2
            save_state_hashes_data(self.proj_dir, state)

            # Seed stale queue curation + conflicting backend_override before reverting
            q_file = self.proj_dir / "work" / "jobs" / "v3_queue.json"
            write_queue_json_locked(server, q_file, [
                {"id": out_p1, "scene_id": "S14", "pass": 1, "output_filename": out_p1, "status": "queued", "reason": "FORCED", "backend_override": "local"},
                {"id": out_p2, "scene_id": "S14", "pass": 2, "output_filename": out_p2, "status": "skipped", "reason": "SKIPPED BY USER"},
            ])

            mf = load_takes_manifest(self.proj_dir)
            take1_id = mf["scenes"]["S14"]["takes"][0]["take_id"]
            take2_id = mf["scenes"]["S14"]["takes"][1]["take_id"]

            # 3) Activate historical Pass 2 Take 2 via REST API
            h_act = DummyRequestHandler()
            handle_takes_api(h_act, "activate", body={"project": "test_slug", "scene_id": "S14", "take_id": take2_id})
            self.assertEqual(h_act.sent_status, HTTPStatus.OK)
            self.assertTrue(h_act.sent_json_data["success"])

            mf_after = load_takes_manifest(self.proj_dir)
            self.assertEqual(mf_after["scenes"]["S14"]["active_takes"].get("p1"), take1_id)
            self.assertEqual(mf_after["scenes"]["S14"]["active_takes"].get("p2"), take2_id)
            self.assertTrue(p1_mp4.read_bytes().startswith(b"take1_ref2va_minimax_"))
            self.assertTrue(p2_mp4.read_bytes().startswith(b"take2_ref2va_local___"))

            sc_after = json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][0]
            self.assertEqual(sc_after.get("renderer"), 9)
            self.assertEqual(sc_after.get("backend_m"), "minimax")

            q_after = server.compute_fast_project_queue("test_slug", force_recheck=True)
            by_pass = {it["pass"]: it for it in q_after if it.get("scene_id") == "S14"}
            self.assertEqual(by_pass[1]["status"], "completed")
            self.assertFalse(by_pass[1]["needs_render"])
            self.assertEqual(by_pass[1].get("take_id"), take1_id)
            self.assertEqual(by_pass[2]["status"], "completed")
            self.assertFalse(by_pass[2]["needs_render"])
            self.assertEqual(by_pass[2].get("take_id"), take2_id)
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_25_revert_minimax_on_renderer1_avoids_cloud_only_profile_mismatch(self):
        """Regression: reverting a take rendered via video_renderers['1'] + backend='minimax' restores renderer=1, not 'minimax_api_h3'."""
        import server
        orig_pdir = getattr(server, "PROJECTS_DIR", None)
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["models"] = {"default_video_renderer": 14, "default_video_resolution": 3}
            s22 = {
                "id": "S22", "mode": "duet_method_b", "renderer": None, "backend": "minimax",
                "start": 0.0, "end": 5.0, "split_x": 0.48, "anchor": "A01.jpg",
                "prompt_m": "Lead left", "prompt_f": "Drummer right",
                "seed_m": 8222, "seed_f": 8322, "stem_m": "male", "stem_f": "female",
                "pass2_roi_mode": "tight_performer",
            }
            cfg["scenes"] = [dict(s22, renderer=1)]
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            out_p1, out_p2 = "S22_Pass1_Left_1280x720", "S22_Pass2_Final_1280x720"
            p1_mp4, p2_mp4 = self.renders_dir / f"{out_p1}.mp4", self.renders_dir / f"{out_p2}.mp4"
            ctx1 = ProjectContext(self.config_file)
            h1 = compute_project_task_hashes(ctx1, backend_overrides={out_p1: "minimax", out_p2: "local"})
            st = {"tasks": {out_p1: h1[out_p1][0], out_p2: h1[out_p2][0]}, "task_hashes": {out_p1: h1[out_p1][0], out_p2: h1[out_p2][0]}, "scene_renders": {}}
            save_state_hashes_data(self.proj_dir, st)

            # Snapshots intentionally omit explicit renderer_m/backend_m (default #1 + queue minimax)
            snap_old = dict(s22)
            p1_mp4.write_bytes(b"s22_p1_h3_minimax____" + b"a" * 60000)
            after_scene_render(
                ctx1, snap_old, 1, out_p1, p1_mp4, h1[out_p1][0],
                {"prompt": s22["prompt_m"], "seed": s22["seed_m"], "model_type": "minimax_h3_fl2va_pruned_pdd", "backend": "minimax", "render_start": 0.0, "render_end": 5.0},
                10.0, st, created_at=1000.0,
            )
            p2_mp4.write_bytes(b"s22_p2_h3_local______" + b"b" * 60000)
            after_scene_render(
                ctx1, snap_old, 2, out_p2, p2_mp4, h1[out_p2][0],
                {"prompt": s22["prompt_f"], "seed": s22["seed_f"], "model_type": "minimax_h3_fl2va_pruned_pdd", "backend": "local", "render_start": 0.0, "render_end": 5.0},
                12.0, st, created_at=1100.0,
            )

            # Render later LTX-2 takes under default renderer 14
            cfg["scenes"] = [dict(s22)]
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            ctx2 = ProjectContext(self.config_file)
            h2 = compute_project_task_hashes(ctx2, backend_overrides={out_p1: "local", out_p2: "local"})
            before_scene_render(ctx2, cfg["scenes"][0], out_p1, p1_mp4, st)
            p1_mp4.write_bytes(b"s22_p1_ltx25_________" + b"c" * 60000)
            after_scene_render(ctx2, dict(s22), 1, out_p1, p1_mp4, h2[out_p1][0], {"prompt": s22["prompt_m"], "seed": s22["seed_m"], "model_type": "ltx2_25_22B_distilled", "backend": "local", "render_start": 0.0, "render_end": 5.0}, 10.0, st, created_at=2000.0)
            before_scene_render(ctx2, cfg["scenes"][0], out_p2, p2_mp4, st)
            p2_mp4.write_bytes(b"s22_p2_ltx25_________" + b"d" * 60000)
            after_scene_render(ctx2, dict(s22), 2, out_p2, p2_mp4, h2[out_p2][0], {"prompt": s22["prompt_f"], "seed": s22["seed_f"], "model_type": "ltx2_25_22B_distilled", "backend": "local", "render_start": 0.0, "render_end": 5.0}, 12.0, st, created_at=2100.0)
            st["tasks"][out_p1] = st["task_hashes"][out_p1] = h2[out_p1][0]
            st["tasks"][out_p2] = st["task_hashes"][out_p2] = h2[out_p2][0]
            save_state_hashes_data(self.proj_dir, st)

            mf = load_takes_manifest(self.proj_dir)
            tk_h3_p2 = mf["scenes"]["S22"]["takes"][1]["take_id"]
            h_act = DummyRequestHandler()
            handle_takes_api(h_act, "activate", body={"project": "test_slug", "scene_id": "S22", "take_id": tk_h3_p2})
            self.assertEqual(h_act.sent_status, HTTPStatus.OK)

            sc_after = json.loads(self.config_file.read_text(encoding="utf-8"))["scenes"][0]
            self.assertEqual(sc_after.get("renderer_m"), 1)
            self.assertEqual(sc_after.get("renderer_f"), 1)
            self.assertEqual(sc_after.get("backend_m"), "minimax")
            self.assertEqual(sc_after.get("backend_f"), "local")

            q_after = server.compute_fast_project_queue("test_slug", force_recheck=True)
            by_pass = {it["pass"]: it for it in q_after if it.get("scene_id") == "S22"}
            self.assertEqual(by_pass[1]["status"], "completed")
            self.assertFalse(by_pass[1]["needs_render"])
            self.assertEqual(by_pass[2]["status"], "completed")
            self.assertFalse(by_pass[2]["needs_render"])
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_21_duplicate_scene_slots_takes_and_independent_vault(self):
        """Duplicate scene places copy right after original, clones all takes/strips/active clips, and keeps queue UP-TO-DATE."""
        import server
        from pipeline.scene_ops import derive_duplicate_scene_id, duplicate_scene_with_takes

        self.assertEqual(derive_duplicate_scene_id("S05", {"S05"}), "S05a")
        self.assertEqual(derive_duplicate_scene_id("S05", {"S05", "S05a"}), "S05b")
        self.assertEqual(derive_duplicate_scene_id("S05a", {"S05", "S05a"}), "S05b")
        self.assertEqual(derive_duplicate_scene_id("Intro", {"Intro"}), "Intro_a")

        orig_pdir = server.PROJECTS_DIR
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            ctx = ProjectContext(self.config_file)
            hashes = compute_project_task_hashes(ctx)
            s01_out, s02_out = "S01_solo_768x432", "S02_solo_768x432"
            s01_mp4, s02_mp4 = self.renders_dir / f"{s01_out}.mp4", self.renders_dir / f"{s02_out}.mp4"
            st = {"tasks": {}, "task_hashes": {}, "scene_renders": {}}

            # Record Take 1 & Take 2 on S01, plus strip image
            s01_mp4.write_bytes(b"s01_take1__" + b"a" * 60000)
            strip_p = self.frames_dir / "S01_strip.jpg"
            strip_p.write_bytes(b"s01_strip_bytes_v1")
            after_scene_render(
                ctx, {**ctx.scenes[0], "seed": 1001}, None, s01_out, s01_mp4,
                "hash_s01_t1", {"prompt": ctx.scenes[0]["prompt"], "seed": 1001, "render_start": 0.0, "render_end": 5.0},
                10.0, st, created_at=1000.0,
            )
            before_scene_render(ctx, ctx.scenes[0], s01_out, s01_mp4, st)
            s01_mp4.write_bytes(b"s01_take2__" + b"b" * 60000)
            strip_p.write_bytes(b"s01_strip_bytes_v2")
            after_scene_render(
                ctx, ctx.scenes[0], None, s01_out, s01_mp4,
                hashes[s01_out][0], {"prompt": ctx.scenes[0]["prompt"], "seed": 1001, "render_start": 0.0, "render_end": 5.0},
                12.0, st, created_at=2000.0, preserve_existing_active=False,
            )

            # Seed S02 render too
            s02_mp4.write_bytes(b"s02_take1__" + b"c" * 60000)
            after_scene_render(
                ctx, ctx.scenes[1], None, s02_out, s02_mp4,
                hashes[s02_out][0], {"prompt": ctx.scenes[1]["prompt"], "seed": 1002, "render_start": 5.0, "render_end": 10.0},
                11.0, st, created_at=2100.0,
            )
            st["tasks"][s01_out] = st["task_hashes"][s01_out] = hashes[s01_out][0]
            st["tasks"][s02_out] = st["task_hashes"][s02_out] = hashes[s02_out][0]
            save_state_hashes_data(self.proj_dir, st)

            # 1. Duplicate non-final scene S01 -> [S01 (0..2.5), S01a (2.5..5.0), S02 (5..10)]
            res1 = duplicate_scene_with_takes(self.proj_dir, self.config_file, "S01")
            scenes = res1["scenes"]
            self.assertEqual([s["id"] for s in scenes], ["S01", "S01a", "S02"])
            self.assertEqual((scenes[0]["start"], scenes[0]["end"]), (0.0, 2.5))
            self.assertEqual((scenes[1]["start"], scenes[1]["end"]), (2.5, 5.0))
            self.assertEqual((scenes[2]["start"], scenes[2]["end"]), (5.0, 10.0))
            self.assertEqual(scenes[1]["output_name"], "S01a_solo")
            self.assertEqual(res1["copied_takes_count"], 2)

            # Active render, strip & vault files exist independently for S01a
            s01a_mp4 = self.renders_dir / "S01a_solo_768x432.mp4"
            s01a_strip = self.frames_dir / "S01a_strip.jpg"
            self.assertTrue(s01a_mp4.is_file() and s01a_mp4.stat().st_size > 60000)
            self.assertTrue(s01a_strip.is_file() and s01a_strip.read_bytes() == b"s01_strip_bytes_v2")

            mf = load_takes_manifest(self.proj_dir)
            s01a_entry = mf["scenes"]["S01a"]
            self.assertEqual(len(s01a_entry["takes"]), 2)
            self.assertEqual(s01a_entry["takes"][0]["scene_id"], "S01a")
            self.assertEqual(s01a_entry["takes"][1]["out_name"], "S01a_solo_768x432")
            for tk in s01a_entry["takes"]:
                self.assertTrue((self.renders_dir / tk["vault_rel_mp4"]).is_file())
                self.assertIn("takes/S01a/", tk["vault_rel_mp4"])

            # Deleting Take 1 from S01 must not affect S01a's cloned Take 1
            s01_tk1_id = mf["scenes"]["S01"]["takes"][0]["take_id"]
            del_res = delete_scene_take(self.proj_dir, self.config_file, "S01", s01_tk1_id)
            self.assertTrue(del_res["success"])
            self.assertEqual(len(load_takes_manifest(self.proj_dir)["scenes"]["S01"]["takes"]), 1)
            self.assertEqual(len(load_takes_manifest(self.proj_dir)["scenes"]["S01a"]["takes"]), 2)
            self.assertTrue((self.renders_dir / s01a_entry["takes"][0]["vault_rel_mp4"]).is_file())

            # Queue reports S01, S01a, and S02 as UP-TO-DATE
            q1 = server.compute_fast_project_queue("test_slug", force_recheck=True)
            by_sid = {it["scene_id"]: it for it in q1}
            for sid in ("S01", "S01a", "S02"):
                self.assertEqual(by_sid[sid]["status"], "completed", f"Unexpected status for {sid}: {by_sid[sid]}")
                self.assertFalse(by_sid[sid]["needs_render"])

            # Editing S01a prompt queues ONLY S01a
            cfg_cur = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg_cur["scenes"][1]["prompt"] = "Brand new variation prompt for S01a"
            self.config_file.write_text(json.dumps(cfg_cur, indent=2), encoding="utf-8")
            q_edit = server.compute_fast_project_queue("test_slug", force_recheck=True)
            by_sid_edit = {it["scene_id"]: it for it in q_edit}
            self.assertEqual(by_sid_edit["S01"]["status"], "completed")
            self.assertEqual(by_sid_edit["S01a"]["status"], "queued")
            self.assertEqual(by_sid_edit["S02"]["status"], "completed")

            # 2. Duplicate last scene S02 -> extends [10.0, 15.0] keeping S02 [5.0, 10.0], also UP-TO-DATE
            res_last = duplicate_scene_with_takes(self.proj_dir, self.config_file, "S02")
            last_scenes = res_last["scenes"]
            self.assertEqual([s["id"] for s in last_scenes], ["S01", "S01a", "S02", "S02a"])
            self.assertEqual((last_scenes[2]["start"], last_scenes[2]["end"]), (5.0, 10.0))
            self.assertEqual((last_scenes[3]["start"], last_scenes[3]["end"]), (10.0, 15.0))
            q_last = server.compute_fast_project_queue("test_slug", force_recheck=True)
            by_sid_last = {it["scene_id"]: it for it in q_last}
            self.assertEqual(by_sid_last["S02a"]["status"], "completed")
            self.assertFalse(by_sid_last["S02a"]["needs_render"])
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_22_duplicate_two_pass_duet_and_js_ui_handler(self):
        """Two-pass duet duplication preserves Pass 1/Pass 2 companion hashes & UP-TO-DATE state; JS handler updates timeline/selection cleanly."""
        import subprocess
        import server
        from pipeline.scene_ops import duplicate_scene_with_takes

        orig_pdir = server.PROJECTS_DIR
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["models"] = {"default_video_renderer": 14, "default_video_resolution": 3}
            s01_duet = {
                "id": "S01", "mode": "duet_method_b", "start": 0.0, "end": 6.0, "split_x": 0.5,
                "anchor": "A01_vocalist.jpg", "prompt_m": "Male left", "prompt_f": "Female right",
                "seed_m": 5001, "seed_f": 5002, "stem_m": "male", "stem_f": "female", "locked": True,
            }
            cfg["scenes"] = [s01_duet]
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx = ProjectContext(self.config_file)
            hashes = compute_project_task_hashes(ctx)
            out_p1, out_p2 = "S01_Pass1_Left_1280x720", "S01_Pass2_Final_1280x720"
            p1_mp4, p2_mp4 = self.renders_dir / f"{out_p1}.mp4", self.renders_dir / f"{out_p2}.mp4"
            st = {"tasks": {out_p1: hashes[out_p1][0], out_p2: hashes[out_p2][0]}, "task_hashes": {out_p1: hashes[out_p1][0], out_p2: hashes[out_p2][0]}, "scene_renders": {}}
            p1_mp4.write_bytes(b"p1_" + b"x" * 60000)
            after_scene_render(ctx, s01_duet, 1, out_p1, p1_mp4, hashes[out_p1][0], {"prompt": "Male left", "seed": 5001, "render_start": 0.0, "render_end": 6.0}, 8.0, st, created_at=1000.0)
            p2_mp4.write_bytes(b"p2_" + b"y" * 60000)
            after_scene_render(ctx, s01_duet, 2, out_p2, p2_mp4, hashes[out_p2][0], {"prompt": "Female right", "seed": 5002, "render_start": 0.0, "render_end": 6.0}, 9.0, st, created_at=1100.0)
            save_state_hashes_data(self.proj_dir, st)

            res = duplicate_scene_with_takes(self.proj_dir, self.config_file, "S01")
            self.assertEqual([s["id"] for s in res["scenes"]], ["S01", "S01a"])
            self.assertFalse(res["duplicated_scene"].get("locked", False))
            self.assertEqual(res["copied_takes_count"], 2)
            self.assertTrue((self.renders_dir / "S01a_Pass1_Left_1280x720.mp4").is_file())
            self.assertTrue((self.renders_dir / "S01a_Pass2_Final_1280x720.mp4").is_file())
            q = server.compute_fast_project_queue("test_slug", force_recheck=True)
            s01a_items = [it for it in q if it.get("scene_id") == "S01a"]
            self.assertEqual(len(s01a_items), 2)
            self.assertTrue(all(it["status"] == "completed" and not it["needs_render"] for it in s01a_items))

            # Verify scene_duplicate.js lock guard & UI refresh logic in Node VM
            js_path = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "scene_duplicate.js"
            node_test = r"""
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');
const src = fs.readFileSync(process.argv[1], 'utf8');
const calls = { fetch: [], toasts: [], selected: null, snapshots: [] };
const ctx = {
  window: {},
  document: { getElementById: () => ({ addEventListener() {}, disabled: false }) },
  state: {
    currentSlug: 'test_slug',
    isDirty: false,
    selectedSceneIdx: 0,
    projectData: { scenes: [{ id: 'S01', start: 0, end: 5, locked: true }, { id: 'S02', start: 5, end: 10 }] },
  },
  showToast: (m) => calls.toasts.push(m),
  commitEditSession: () => {},
  recordHistorySnapshot: (d) => calls.snapshots.push(d),
  patchSavedScenesArrayBaseline: (fn) => { const a = []; fn(a); calls.baselinePatched = a.length; },
  applyQueueApiResponse: (d) => { ctx.state.renderQueue = d.queue; },
  renderQueueUI: () => { calls.queueRendered = true; },
  refreshProjectAssets: async () => { calls.assetsRefreshed = true; },
  redrawTimeline: () => { calls.redrawn = true; },
  selectScene: (i) => { calls.selected = i; ctx.state.selectedSceneIdx = i; },
  renderSceneList: () => { calls.listRendered = true; },
  loadProjectQueue: async () => { calls.queueLoaded = true; },
  fetch: async (url, opts) => {
    calls.fetch.push({ url, body: JSON.parse(opts.body) });
    return {
      ok: true,
      status: 200,
      json: async () => ({
        success: true,
        orig_scene: { id: 'S02', start: 5, end: 10, locked: true },
        duplicated_scene: { id: 'S02a', start: 10, end: 15 },
        scenes: [{ id: 'S01', start: 0, end: 5, locked: true }, { id: 'S02', start: 5, end: 10, locked: true }, { id: 'S02a', start: 10, end: 15 }],
        copied_takes_count: 2,
        queue: [],
      }),
    };
  },
  console,
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
(async () => {
  // Locked non-last scene cannot share slot silently
  assert.strictEqual(await ctx.duplicateScene(0), false);
  assert.strictEqual(calls.fetch.length, 0);
  assert(calls.toasts[0].includes('locked'));

  // Locked last scene succeeds and selects duplicate immediately after original
  ctx.state.projectData.scenes[1].locked = true;
  assert.strictEqual(await ctx.duplicateScene(1), true);
  assert.deepStrictEqual(calls.fetch[0].body, { slug: 'test_slug', scene_id: 'S02' });
  assert.deepStrictEqual(ctx.state.projectData.scenes.map((s) => s.id), ['S01', 'S02', 'S02a']);
  assert.strictEqual(calls.selected, 2);
  assert.strictEqual(calls.assetsRefreshed, true);
  assert.strictEqual(calls.queueLoaded, true);
  assert(calls.snapshots[0].includes('S02a'));
  console.log('NODE_DUP_JS_OK');
})();
"""
            proc = subprocess.run(["node", "-e", node_test, str(js_path)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, msg=f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
            self.assertIn("NODE_DUP_JS_OK", proc.stdout)
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_23_recreated_scene_id_does_not_inherit_deleted_scene_takes(self):
        """Deleting S02 then saving a new scene also named S02 purges old takes/clips/strips/hashes; S01 untouched."""
        import server
        from pipeline.scene_purge import collect_orphaned_manifest_scene_ids, save_project_json_with_purge

        class _Srv:
            def backup_project_json(self, *a, **k):
                return None

        orig_pdir = server.PROJECTS_DIR
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            ctx = ProjectContext(self.config_file)
            st = {"tasks": {}, "task_hashes": {}, "scene_renders": {}}
            for idx, (out, sid_seed) in enumerate((("S01_solo_768x432", 1001), ("S02_solo_768x432", 1002))):
                mp4 = self.renders_dir / f"{out}.mp4"
                for t in (1, 2):
                    mp4.write_bytes(f"{out}_take{t}__".encode() + b"x" * 60000)
                    after_scene_render(
                        ctx, ctx.scenes[idx], None, out, mp4, f"hash_{out}_{t}",
                        {"prompt": ctx.scenes[idx]["prompt"], "seed": sid_seed}, 5.0, st, created_at=1000.0 + t,
                    )
            (self.frames_dir / "S02_strip.jpg").write_bytes(b"s02_strip")
            st["tasks"]["S02_solo_768x432"] = st["task_hashes"]["S02_solo_768x432"] = "hash_S02_solo_768x432_2"
            st["tasks"]["S01_solo_768x432"] = st["task_hashes"]["S01_solo_768x432"] = "hash_S01_solo_768x432_2"
            save_state_hashes_data(self.proj_dir, st)
            mf0 = load_takes_manifest(self.proj_dir)
            self.assertGreaterEqual(len(mf0["scenes"]["S02"]["takes"]), 1)
            s01_take_count = len(mf0["scenes"]["S01"]["takes"])

            # User deletes S02 and adds a brand-new S02 before saving (UI flags it _freshSceneTakes)
            body = json.loads(self.config_file.read_text(encoding="utf-8"))
            body["scenes"][1] = {"id": "S02", "mode": "solo", "start": 5.0, "end": 10.0, "anchor": "A01",
                                 "prompt": "Totally new scene", "seed": 7777, "_freshSceneTakes": True}
            purged = save_project_json_with_purge(_Srv(), self.proj_dir, self.config_file, "test_slug", body, {})
            self.assertEqual(purged, ["S02"])

            saved = json.loads(self.config_file.read_text(encoding="utf-8"))
            self.assertNotIn("_freshSceneTakes", saved["scenes"][1])
            mf1 = load_takes_manifest(self.proj_dir)
            self.assertNotIn("S02", mf1["scenes"])
            self.assertEqual(len(mf1["scenes"]["S01"]["takes"]), s01_take_count)
            self.assertFalse((self.renders_dir / "S02_solo_768x432.mp4").exists())
            self.assertFalse((self.renders_dir / "takes" / "S02").exists())
            self.assertFalse((self.frames_dir / "S02_strip.jpg").exists())
            self.assertTrue((self.renders_dir / "S01_solo_768x432.mp4").is_file())
            st1 = json.loads(self.state_hashes_file.read_text(encoding="utf-8"))
            self.assertNotIn("S02_solo_768x432", st1.get("tasks", {}))
            self.assertIn("S01_solo_768x432", st1.get("tasks", {}))
            self.assertEqual(collect_orphaned_manifest_scene_ids(self.proj_dir, saved["scenes"]), [])

            # Takes API for the recreated S02 returns no takes, and the queue wants it rendered
            h = DummyRequestHandler("/api/takes/test_slug/S02")
            handle_takes_api(h, "get_takes", path="/api/takes/test_slug/S02")
            takes_resp = h.sent_json_data or {}
            self.assertEqual(takes_resp.get("takes", []), [])
            q = server.compute_fast_project_queue("test_slug", force_recheck=True)
            s02_items = [it for it in q if it["scene_id"] == "S02"]
            self.assertTrue(s02_items and all(it["needs_render"] for it in s02_items))
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_26_delete_scene_checkpoint_archive_anchor_spinner_and_js_isolation(self):
        """POST /api/scene/delete archives checkpoint takes while purging unreferenced ones; anchor batch >8 & JS VM isolation."""
        import io
        import subprocess
        import server
        from http import HTTPStatus
        from pipeline.anchor_takes import parse_anchor_batch_count

        class _HttpPostHandler:
            def __init__(self, path, body_dict):
                self.command = "POST"
                self.path = path
                raw = json.dumps(body_dict).encode("utf-8")
                self.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
                self.rfile = io.BytesIO(raw)
                self.wfile = io.BytesIO()
                self.response_status = None

            def send_json(self, data, status=HTTPStatus.OK):
                self.response_status = int(status)
                self.wfile.write(json.dumps(data).encode("utf-8"))

            def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST):
                self.response_status = int(status)
                self.wfile.write(json.dumps({"error": msg}).encode("utf-8"))

            def read_json_body(self):
                return server.StudioRequestHandler.read_json_body(self)

        self.assertEqual(parse_anchor_batch_count(["A01:x15"], "A01.jpg", {}), 15)
        self.assertEqual(parse_anchor_batch_count(["A01"], "A01.jpg", {"gen_count": 20}), 20)

        orig_pdir = server.PROJECTS_DIR
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            ctx = ProjectContext(self.config_file)
            hashes = compute_project_task_hashes(ctx)
            s02_out = next(k for k in hashes if k.startswith("S02_"))
            s02_mp4 = self.renders_dir / f"{s02_out}.mp4"
            st = {"tasks": {}, "task_hashes": {}, "scene_renders": {}}

            # Take 1 on S02 -> captured in checkpoint cp1
            s02_mp4.write_bytes(b"s02_ckpt_t1__" + b"a" * 60000)
            after_scene_render(
                ctx, ctx.scenes[1], None, s02_out, s02_mp4, hashes[s02_out][0],
                {"prompt": ctx.scenes[1]["prompt"], "seed": 1002}, 5.0, st, created_at=1000.0,
            )
            mf_before = load_takes_manifest(self.proj_dir)
            t1_id = mf_before["scenes"]["S02"]["takes"][0]["take_id"]
            ckpt = create_project_checkpoint(self.proj_dir, self.config_file, name="Before delete")
            self.assertTrue(ckpt.get("id"))

            # Take 2 on S02 -> NOT in checkpoint
            before_scene_render(ctx, ctx.scenes[1], s02_out, s02_mp4, st)
            s02_mp4.write_bytes(b"s02_newer_t2__" + b"b" * 60000)
            after_scene_render(
                ctx, ctx.scenes[1], None, s02_out, s02_mp4, "newer_hash_t2",
                {"prompt": "modified", "seed": 1003}, 5.0, st, created_at=1005.0,
            )

            # Delete S02 via POST /api/scene/delete
            h_del = _HttpPostHandler("/api/scene/delete", {"slug": "test_slug", "scene_id": "S02", "close_gap": True})
            server.StudioRequestHandler.do_POST(h_del)
            self.assertEqual(h_del.response_status, 200)
            mf_after = load_takes_manifest(self.proj_dir)
            self.assertNotIn("S02", mf_after["scenes"])
            self.assertIn(t1_id, mf_after.get("archived_takes", {}))
            self.assertEqual(len(mf_after.get("archived_takes", {})), 1)
            self.assertFalse((self.renders_dir / "takes" / "S02").exists())

            # Rollback checkpoint restores S02 AND its checkpoint-archived Take 1
            rb = rollback_project_checkpoint(self.proj_dir, self.config_file, ckpt["id"])
            self.assertTrue(rb.get("success"))
            h_get = DummyRequestHandler("/api/takes/test_slug/S02")
            handle_takes_api(h_get, "get_takes", path="/api/takes/test_slug/S02")
            self.assertEqual([t["take_id"] for t in (h_get.sent_json_data or {}).get("takes", [])], [t1_id])
            self.assertTrue(s02_mp4.is_file())

            # Frontend Node VM check across real UI JS modules
            js_dir = WORKSPACE_ROOT / "work" / "ui" / "static" / "js"
            node_script = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const jsDir = process.argv[1];
const els = {};
const noop = () => {};
const mk2d = () => ({ clearRect:noop, fillRect:noop, strokeRect:noop, beginPath:noop, moveTo:noop, lineTo:noop, stroke:noop, fill:noop, fillText:noop, drawImage:noop, save:noop, restore:noop, clip:noop, rect:noop, roundRect:noop, scale:noop, translate:noop, setTransform:noop, setLineDash:noop, createLinearGradient:()=>({addColorStop:noop}), measureText:()=>({width:10}), fillStyle:'', strokeStyle:'', lineWidth:1, font:'' });
const mkEl = (id) => ({
  id, value: '', textContent: '', innerHTML: '', src: '', checked: false, disabled: false, width: 800, height: 120,
  style: { display: '', height: '' }, dataset: {}, children: [], options: [], tagName: 'DIV', attrs: {}, _qs: {},
  classList: { _s: new Set(), add(...a){a.forEach(x=>this._s.add(x))}, remove(...a){a.forEach(x=>this._s.delete(x))}, toggle(x,f){ f===undefined?(this._s.has(x)?this._s.delete(x):this._s.add(x)):(f?this._s.add(x):this._s.delete(x)); }, contains(x){return this._s.has(x)} },
  listeners: {}, addEventListener(t, fn){ (this.listeners[t]=this.listeners[t]||[]).push(fn); },
  querySelector(s){ return this._qs[s] || (this._qs[s] = mkEl(s)); }, querySelectorAll(){ return []; }, closest(){ return null; },
  appendChild(c){ this.children.push(c); if (c && (c.tagName==='OPTION' || 'value' in c)) this.options.push(c); return c; },
  append(...items){ items.forEach(c => this.appendChild(c)); },
  add(opt){ this.options.push(opt); },
  setAttribute(k,v){ this.attrs[k]=v; if(k==='src') this.src=v; },
  getAttribute(k){ return k==='src' ? (this.src||null) : (this.attrs[k]||null); },
  removeAttribute(k){ delete this.attrs[k]; if(k==='src') this.src=''; },
  load: noop, pause: noop, play: async () => {}, insertBefore: noop, focus: noop, scrollIntoView: noop,
  click(){ (this.listeners.click||[]).forEach(f=>f()); if (typeof this.onclick === 'function') this.onclick(); },
  getContext(){ return mk2d(); }, getBoundingClientRect(){ return { left:0, top:0, width:800, height:120 }; },
  parentElement: { getBoundingClientRect(){ return { left:0, top:0, width:800, height:120 }; }, scrollLeft: 0, scrollWidth: 800 }
});
const doc = {
  readyState: 'complete',
  getElementById(id){ return els[id] || (els[id] = mkEl(id)); },
  querySelector(){ return mkEl('q'); }, querySelectorAll(){ return []; },
  createElement(t){ const e = mkEl(t); e.tagName = String(t||'div').toUpperCase(); return e; }, addEventListener(){}, activeElement: null,
};
const fetchCalls = [];
const ctx = {
  window: {}, document: doc, console, setTimeout, clearTimeout, setInterval, clearInterval, requestAnimationFrame: noop,
  addEventListener: noop, removeEventListener: noop, dispatchEvent: noop, CustomEvent: function(n,o){this.type=n;this.detail=o&&o.detail;},
  Option: function(t,v){ this.text=t||''; this.value=v===undefined?this.text:v; },
  resizeAllPromptTextareas: noop, renderCueReferenceChips: noop, updateCompiledPromptPreview: noop, updateSceneDurationBadge: noop,
  syncModularComponentsUI: noop, updatePromptResolvedPreviews: noop, updateLipSyncRepairVisibility: noop,
  renderInspectorNegativePrompts: noop, updateInspectorAnchorPreview: noop, syncInspectorModelSelectors: noop,
  refreshProjectAssets: async () => {}, renderQueueUI: noop, loadProjectQueue: async () => {}, renderSceneJumpBar: noop, updateMasterVideoUI: noop,
  fetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (String(url).includes('/api/takes/')) return { ok: true, json: async () => ({ success: true, takes: [{ take_id: 'old_tk_1', take_number: 1, scene_id: 'S02', created_iso: '2026-11-01T00:00:00Z' }], active_take_id: 'old_tk_1' }) };
    if (String(url).startsWith('/api/project/') && opts && opts.method === 'PUT') return { ok: true, json: async () => ({ success: true, status: 'saved', purged_scenes: ['S02'] }) };
    if (String(url).startsWith('/api/project/')) return { ok: true, json: async () => ({ data: {}, assets: { clips: [], strips: [] } }) };
    return { ok: true, json: async () => ({}), text: async () => '' };
  },
  URL, URLSearchParams, Date, Math, JSON, Object, Array, Set, Map, Number, String, Boolean, Promise, RegExp, Error,
  Image: function(){}, AudioContext: function(){}, confirm: () => true, alert: () => {},
  performance: { now: () => 1000 }, localStorage: { getItem: () => null, setItem: () => {} },
};
ctx.window = ctx; ctx.globalThis = ctx; ctx.__fetchCalls = fetchCalls;
vm.createContext(ctx);
for (const f of ['state_and_utils.js','markers_and_prompts.js','duet_repair.js','scene_duplicate.js','scene_inspector.js','takes_and_history.js','scenes_tab.js','timeline_and_playback.js','anchors_and_settings.js','anchors_studio.js']) {
  vm.runInContext(fs.readFileSync(path.join(jsDir, f), 'utf8'), ctx, { filename: f });
}
(async () => {
  await vm.runInContext('(' + async function() {
    state.currentSlug = 'my_slug';
    state.projectData = {
      project: { name: 'Demo', total_duration: 12 },
      anchors: [{ id: 'A01', filename: 'A01.jpg' }],
      scenes: [
        { id: 'S01', start: 0, end: 4, mode: 'solo', anchor: 'A01.jpg', prompt: 'p1', seed: 101 },
        { id: 'S02', start: 4, end: 8, mode: 'solo', anchor: 'A01.jpg', prompt: 'p2', seed: 102 },
      ]
    };
    state.assets = { clips: [{ scene_id: 'S02', filename: 'S02_solo_832x480.mp4', size_kb: 500 }], strips: [{ scene_id: 'S02', filename: 'S02_strip.jpg' }] };
    state.selectedSceneIdx = 1;
    initProjectHistory();

    await updateSceneTakesUI(state.projectData.scenes[1]);
    if (document.getElementById('sceneTakesTray').style.display !== 'block') throw new Error('expected takes tray visible before delete');

    deleteScene(1);
    document.getElementById('btnAddScene').click();
    const added = state.projectData.scenes[1];
    if (added.id !== 'S02' || !added._freshSceneTakes) throw new Error('expected fresh S02');

    const takesFetchesBefore = __fetchCalls.filter(c => String(c.url).includes('/api/takes/')).length;
    await updateSceneTakesUI(added);
    if (__fetchCalls.filter(c => String(c.url).includes('/api/takes/')).length !== takesFetchesBefore) throw new Error('must not fetch old takes for _freshSceneTakes');
    if (document.getElementById('sceneTakesTray').style.display !== 'none') throw new Error('takes tray must hide before Save');
    updateSceneClipPreview(added);
    if (document.getElementById('clipStatusBadge').textContent !== 'Missing') throw new Error('clip badge must be Missing before Save');

    performUndo(); performUndo();
    if (state.projectData.scenes[1]._freshSceneTakes) throw new Error('Undo should restore original S02 without _freshSceneTakes');
    performRedo(); performRedo();
    if (!state.projectData.scenes[1]._freshSceneTakes) throw new Error('Redo should reapply _freshSceneTakes');

    const snap = JSON.parse(JSON.stringify(state.projectData));
    state.projectData.scenes = [{ id: 'S01', start: 0, end: 4 }, { id: 'S03', start: 4, end: 8 }];
    document.getElementById('btnAddScene').click();
    if (state.projectData.scenes[2].id !== 'S04') throw new Error('expected collision-safe S04');
    state.projectData = snap;

    const ok = await saveProjectData();
    if (!ok) throw new Error('saveProjectData failed');
    const putCall = __fetchCalls.find(c => c.opts && c.opts.method === 'PUT');
    if (!putCall || !putCall.url.includes('purge_scenes=S02')) throw new Error('expected purge_scenes=S02');
    const putBody = JSON.parse(putCall.opts.body);
    if (putBody.scenes.some(s => '_freshSceneTakes' in s)) throw new Error('transient flag leaked into saved JSON');

    // Anchor batch count > 8 in renderSingleAnchor
    window.triggerRender = async (args) => { window.__lastAnchorArgs = args; };
    await window.renderSingleAnchor('A01', 15);
    if (!Array.isArray(window.__lastAnchorArgs) || window.__lastAnchorArgs[1] !== 'A01:x15') throw new Error('expected A01:x15, got ' + JSON.stringify(window.__lastAnchorArgs));
    console.log('NODE_RECREATE_AND_SPINNER_OK');
  } + ')()', ctx);
})().catch(e => { console.error(e); process.exit(1); });
"""
            proc = subprocess.run(["node", "-e", node_script, str(js_dir)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
            self.assertIn("NODE_RECREATE_AND_SPINNER_OK", proc.stdout)
        finally:
            server.PROJECTS_DIR = orig_pdir

    def test_27_preserve_selected_active_take_across_new_scene_renders(self):
        """New scene takes archive to vault without replacing the previously selected active ('Use') version."""
        import subprocess
        import server
        from pipeline.media_utils import resolve_ffmpeg_path
        from pipeline.stage2_scenes import run_stage2_scenes
        from pipeline.stage3_master import assemble_progressive_master
        from pipeline.wgp_bridge import LazyWgpSession

        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        subprocess.run(
            [str(ffmpeg_bin), "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "4.0", str(self.audio_dir / "master.wav")],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        orig_pdir = server.PROJECTS_DIR
        server.PROJECTS_DIR = Path(self.test_dir)
        try:
            cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
            cfg["models"]["video_shared_settings"] = {"skip_steps_cache_type": ""}
            cfg["scenes"] = [
                {
                    "id": "S01", "mode": "solo", "output_name": "S01_solo",
                    "start": 0.0, "end": 2.0, "anchor": "A01_vocalist.jpg",
                    "prompt": "Take 1 prompt", "seed": 1001, "resolution": "832x480",
                },
                {
                    "id": "S02", "mode": "duet_method_b", "start": 2.0, "end": 4.0,
                    "split_x": 0.5, "anchor": "A01_vocalist.jpg",
                    "prompt_m": "Male M1", "prompt_f": "Female F1",
                    "seed_m": 2001, "seed_f": 2002, "stem_m": "master", "stem_f": "master",
                    "resolution": "832x480",
                },
            ]
            cfg["project"]["total_duration"] = 4.0
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            # 1. Render initial takes via Stage 2 dummy_render -> first takes become active ("Use")
            ctx1 = ProjectContext(self.config_file)
            sh1 = ctx1.load_state_hashes()
            run_stage2_scenes(ctx1, LazyWgpSession(ctx1), sh1, force_scenes=[], regenerated_anchors=set(), dummy_render=True, no_stitch=True)
            mf1 = load_takes_manifest(self.proj_dir)
            s01_t1 = mf1["scenes"]["S01"]["takes"][0]
            self.assertEqual(mf1["scenes"]["S01"]["active_take_id"], s01_t1["take_id"])
            s02_p1_t1 = next(t for t in mf1["scenes"]["S02"]["takes"] if t["pass_num"] == 1)
            s02_p2_t1 = next(t for t in mf1["scenes"]["S02"]["takes"] if t["pass_num"] == 2)
            self.assertEqual(mf1["scenes"]["S02"]["active_takes"]["p1"], s02_p1_t1["take_id"])
            self.assertEqual(mf1["scenes"]["S02"]["active_takes"]["p2"], s02_p2_t1["take_id"])
            self.assertEqual(mf1["scenes"]["S02"]["active_take_id"], s02_p2_t1["take_id"])

            s01_old_mp4 = self.renders_dir / "S01_solo_832x480.mp4"
            t1_size = s01_old_mp4.stat().st_size

            # 2. Edit S01 resolution/seed and S02 Pass 1+Pass 2 seeds, then re-render new takes
            cfg["scenes"][0]["seed"] = 9001
            cfg["scenes"][0]["resolution"] = "960x544"
            cfg["scenes"][1]["seed_m"] = 9002
            cfg["scenes"][1]["seed_f"] = 9003
            self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            ctx2 = ProjectContext(self.config_file)
            sh2 = ctx2.load_state_hashes()
            meta2 = run_stage2_scenes(ctx2, LazyWgpSession(ctx2), sh2, force_scenes=[], regenerated_anchors=set(), dummy_render=True, no_stitch=True)

            mf2 = load_takes_manifest(self.proj_dir)
            self.assertEqual(len(mf2["scenes"]["S01"]["takes"]), 2)
            self.assertEqual(mf2["scenes"]["S01"]["active_take_id"], s01_t1["take_id"])
            self.assertEqual(mf2["scenes"]["S01"]["active_takes"]["default"], s01_t1["take_id"])
            self.assertEqual(s01_old_mp4.stat().st_size, t1_size)

            # Two-pass duet: Pass 2 Take 2 links parent_p1_task_hash to newly rendered Pass 1 Take 2 while keeping Take 1s active
            s02_p1_t2 = [t for t in mf2["scenes"]["S02"]["takes"] if t["pass_num"] == 1][1]
            s02_p2_t2 = [t for t in mf2["scenes"]["S02"]["takes"] if t["pass_num"] == 2][1]
            self.assertEqual(s02_p2_t2.get("parent_p1_task_hash"), s02_p1_t2.get("task_hash"))
            self.assertEqual(mf2["scenes"]["S02"]["active_takes"]["p1"], s02_p1_t1["take_id"])
            self.assertEqual(mf2["scenes"]["S02"]["active_takes"]["p2"], s02_p2_t1["take_id"])
            self.assertEqual(mf2["scenes"]["S02"]["active_take_id"], s02_p2_t1["take_id"])

            # Queue reports all passes UP-TO-DATE (doesn't loop re-rendering Take 2)
            q_after = server.compute_fast_project_queue("test_slug", force_recheck=True)
            for it in q_after:
                if it.get("scene_id") in ("S01", "S02"):
                    self.assertEqual(it["status"], "completed", msg=f"Unexpected status for {it}")
                    self.assertFalse(it["needs_render"])

            # Stage 3 master uses preserved active Take 1 for S01 even though project resolution changed to 960x544
            self.assertTrue(assemble_progressive_master(ctx2, meta2, log_prefix="  "))
            trimmed_s01 = next((p for p in ctx2.trimmed_dir.glob("00_S01_*.mp4")), None)
            self.assertIsNotNone(trimmed_s01)
        finally:
            server.PROJECTS_DIR = orig_pdir


if __name__ == "__main__":
    unittest.main()
