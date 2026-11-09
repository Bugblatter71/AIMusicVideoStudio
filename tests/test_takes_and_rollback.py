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

        self.renders_dir = self.proj_dir / "work" / "renders_v3"
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir = self.proj_dir / "work" / "frames_v3"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.anchors_dir = self.proj_dir / "work" / "anchors_v3"
        self.anchors_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.proj_dir / "work" / "jobs_v3"
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
        # Create a pre-existing render in renders_v3
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

        # Verify manifest now has both takes
        manifest = load_takes_manifest(self.proj_dir)
        takes = manifest["scenes"]["S01"]["takes"]
        self.assertEqual(len(takes), 2)
        self.assertEqual(manifest["scenes"]["S01"]["active_take_id"], new_take["take_id"])

        # Both vault files must exist independently
        vault1 = self.proj_dir / "work" / "renders_v3" / takes[0]["vault_rel_mp4"]
        vault2 = self.proj_dir / "work" / "renders_v3" / takes[1]["vault_rel_mp4"]
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

        # Create Take 2
        before_scene_render(ctx, sc, out_name, s01_render, state)
        s01_render.write_bytes(b"y" * 65000)
        t_dict = {"prompt": "Alt prompt", "seed": 2002, "duration": 5.0}
        after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash="hash_s01_v2", t_dict=t_dict, elapsed=40.0, state=state
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
    def test_09_checkpoint_with_jobs_v3_state_hashes(self):
        """Verify checkpoint & rollback works when state_hashes is in jobs_v3."""
        if self.state_hashes_file.is_file():
            self.state_hashes_file.unlink()
        jobs_hashes = self.jobs_dir / "state_hashes.json"
        jobs_hashes.write_text(json.dumps({"tasks": {"S01_solo": "hash_v1"}}, indent=2), encoding="utf-8")

        cp = create_project_checkpoint(self.proj_dir, self.config_file, name="JobsV3 Checkpoint", trigger="manual")
        self.assertTrue((self.proj_dir / "checkpoints" / cp["id"] / "state_hashes.json").is_file())

        # Mutate jobs_v3 hashes
        jobs_hashes.write_text(json.dumps({"tasks": {"S01_solo": "hash_mutated"}}, indent=2), encoding="utf-8")

        # Rollback
        roll = rollback_project_checkpoint(self.proj_dir, cp["id"])
        self.assertTrue(roll["success"])

        # Verify restoration in jobs_v3
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

        # 2. Start rendering Take 2: active file is safely unlinked
        before_scene_render(ctx, sc, out_name, s01_render, state)
        self.assertFalse(s01_render.exists())

        # 3. Simulate cancellation or engine crash mid-render
        restore_previous_active_take_if_missing(ctx, "S01", out_name, s01_render)

        # 4. Verify previous active take was cleanly restored
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
        after_scene_render(
            ctx, sc, pass_num=None, out_name=out_name, out_mp4=s01_render,
            t_hash=h_v2, t_dict=t2_dict, elapsed=35.0, state=state
        )

        state["tasks"][out_name] = h_v2
        state["task_hashes"][out_name] = h_v2
        save_state_hashes_data(self.proj_dir, state)
        self.assertEqual(s01_render.read_bytes()[:19], b"take_2_video_bytes_")

        # 3. User hits Undo (Ctrl+Z): project JSON reverts back to seed 1001 (Take 1)
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

        # 3. User selects S01 in Inspector: UI requests /api/takes/test_slug/S01
        h_get = DummyRequestHandler()
        handle_takes_api(h_get, "get_takes", path="/api/takes/test_slug/S01")
        self.assertEqual(h_get.sent_status, HTTPStatus.OK)
        takes_list = h_get.sent_json_data["takes"]
        self.assertEqual(len(takes_list), 2)
        take1_id = takes_list[0]["take_id"]
        take2_id = takes_list[1]["take_id"]
        self.assertEqual(h_get.sent_json_data["active_take_id"], take2_id)

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


if __name__ == "__main__":
    unittest.main()
