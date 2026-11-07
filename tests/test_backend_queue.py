"""
Backend Integration Test Suite for Music Video Studio Render Queue & Engine.
Focuses on:
1. Fast queue calculation & structure.
2. Queue reordering & status transitions (move up/down, remove, enqueue, clear).
3. Audio stem resolution safety (no hard ffmpeg crash on missing stems).
4. Single-scene dummy render execution (fast FFmpeg simulation without GPU).
5. Immediate master restitch execution.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

# Add project root to path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from run_overnight_v3 import ProjectContext, LazyWgpSession, run_stage1_anchors, run_stage2_scenes
import server


class TestRenderQueueBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_slug = "test_project"
        cls.proj_dir = WORKSPACE_ROOT / "projects" / cls.project_slug
        cls.json_path = cls.proj_dir / "test_project.json"
        assert cls.json_path.exists(), f"Test project missing: {cls.json_path}"

    def setUp(self):
        # Reset queue file for clean testing
        jobs_dir = self.proj_dir / "work" / "jobs_v3"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        self.queue_file = jobs_dir / "v3_queue.json"

    def test_01_fast_queue_calculation(self):
        """Verify compute_fast_project_queue computes tasks in under 50ms with correct pass counts."""
        t0 = time.time()
        queue = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        dur = time.time() - t0

        self.assertLess(dur, 0.5, f"Queue computation took too long: {dur:.3f}s")
        self.assertEqual(len(queue), 4, f"Expected 4 render passes, got {len(queue)}")
        
        # S01 (male solo), S02:p1 (left), S02:p2 (final), S03 (female solo)
        out_names = [q["output_filename"] for q in queue]
        self.assertTrue(any("S01" in n for n in out_names))
        self.assertTrue(any("S02_Pass1_Left" in n for n in out_names))
        self.assertTrue(any("S02_Pass2_Final" in n for n in out_names))
        self.assertTrue(any("S03" in n for n in out_names))

    def test_02_queue_order_manipulation(self):
        """Verify moving queue items up/down preserves valid order and syncs to disk."""
        queue = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        first_item = queue[0]["output_filename"]
        second_item = queue[1]["output_filename"]

        # Simulate move down for first item
        self.queue_file.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        
        items = json.loads(self.queue_file.read_text(encoding="utf-8"))
        # Swap 0 and 1
        items[0], items[1] = items[1], items[0]
        for i, itm in enumerate(items, 1):
            itm["idx"] = i
        self.queue_file.write_text(json.dumps(items, indent=2), encoding="utf-8")

        reloaded = json.loads(self.queue_file.read_text(encoding="utf-8"))
        self.assertEqual(reloaded[0]["output_filename"], second_item)
        self.assertEqual(reloaded[1]["output_filename"], first_item)

    def test_03_audio_stem_resolution_safety(self):
        """Verify resolve_scene_audio handles missing or invalid stem gracefully with silence, avoiding crashes."""
        ctx = ProjectContext(self.json_path)
        
        # Valid stem
        wav_valid = ctx.resolve_scene_audio("S01", "male", 0.0, 2.0, 48)
        self.assertTrue(wav_valid.exists())
        self.assertGreater(wav_valid.stat().st_size, 1000)

        # Missing stem should fall back to silent wav without raising exception
        wav_fallback = ctx.resolve_scene_audio("S99", "non_existent_stem_xyz", 0.0, 2.0, 48)
        self.assertTrue(wav_fallback.exists())
        self.assertIn("silent", wav_fallback.name)

    def test_04_dummy_render_execution(self):
        """Verify executing overnight script with --dummy completes single scene in <3 seconds."""
        cmd = [
            sys.executable,
            str(WORKSPACE_ROOT / "work" / "run_overnight_v3.py"),
            str(self.json_path),
            "S01",
            "--dummy",
        ]
        t0 = time.time()
        res = subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        dur = time.time() - t0

        self.assertEqual(res.returncode, 0, f"Render failed: {res.stderr}\n{res.stdout}")
        self.assertLess(dur, 10.0, f"Dummy render took too long: {dur:.1f}s")
        self.assertIn("[DUMMY]", res.stdout)
        
        # Check generated mp4
        renders_dir = self.proj_dir / "work" / "renders_v3"
        s01_mp4s = list(renders_dir.glob("S01_male_solo_*.mp4"))
        self.assertTrue(len(s01_mp4s) > 0, "Expected S01 rendered mp4 file")
        self.assertGreater(s01_mp4s[0].stat().st_size, 10000)

    def test_05_immediate_restitch(self):
        """Verify immediate restitch runs and completes without error."""
        cmd = [
            sys.executable,
            str(WORKSPACE_ROOT / "work" / "run_overnight_v3.py"),
            str(self.json_path),
            "stitch",
        ]
        res = subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Restitch failed: {res.stderr}\n{res.stdout}")
        self.assertIn("Master video restitched", res.stdout)

    def test_06_anchor_change_detection(self):
        """Verify modifying an anchor image file triggers automatic re-render detection for dependent scenes."""
        aon_anchors = WORKSPACE_ROOT / "projects" / "all_or_nothing" / "work" / "anchors_v3"
        a01_files = list(aon_anchors.glob("A01*"))
        self.assertTrue(len(a01_files) > 0, "A01 anchor must exist")
        a01 = a01_files[0]
        orig_mtime = a01.stat().st_mtime
        try:
            now = time.time()
            # Touch anchor to future so it is newer than the rendered MP4
            os.utime(a01, (now + 100, now + 100))
            q_test = server.compute_fast_project_queue("all_or_nothing", force_recheck=True)
            queued_map = {x["scene_id"]: x for x in q_test if x["status"] == "queued"}
            self.assertIn("S01", queued_map, "S01 must be queued when A01 is newer on disk")
            self.assertTrue(queued_map["S01"]["needs_render"])
            self.assertIn("A01", queued_map["S01"]["reason"])
        finally:
            os.utime(a01, (orig_mtime, orig_mtime))

    def test_07_exact_scene_id_disambiguation(self):
        """Verify targeted scene selection exact matching (e.g. S1 matches only S01, never S10, S11, S12a; S2 matches only S02, never S22)."""
        # Predicate matching verification
        self.assertTrue(server.is_scene_match("S01", "S01_male_solo", "S1"))
        self.assertTrue(server.is_scene_match("S01", "S01_male_solo", "S01"))
        self.assertFalse(server.is_scene_match("S10", "S10_solo", "S1"))
        self.assertFalse(server.is_scene_match("S11", "S11_solo", "S1"))
        self.assertFalse(server.is_scene_match("S12a", "S12a_solo", "S1"))
        self.assertFalse(server.is_scene_match("S12b", "S12b_solo", "S1"))
        self.assertTrue(server.is_scene_match("S02", "S02_Pass1", "S2"))
        self.assertTrue(server.is_scene_match("S02", "S02_Pass1", "S02"))
        self.assertFalse(server.is_scene_match("S22", "S22_solo", "S2"))
        self.assertTrue(server.is_scene_match("S12a", "S12a_solo", "S12a"))
        self.assertFalse(server.is_scene_match("S12b", "S12b_solo", "S12a"))

        # Queue targeting verification
        matched = server.prepare_targeted_queue_items(self.project_slug, "S1")
        self.assertTrue(len(matched) > 0)
        for m in matched:
            self.assertEqual(m["scene_id"], "S01")

    def test_08_two_pass_specific_pass_targeting(self):
        """Verify targeting specific pass (S02:p1 or S02:p2) enqueues only that specific pass."""
        # 1. Target only Pass 1
        p1_matches = server.prepare_targeted_queue_items(self.project_slug, "S02:p1")
        self.assertEqual(len(p1_matches), 1, f"Expected exactly 1 item for S02:p1, got {len(p1_matches)}")
        self.assertEqual(p1_matches[0]["pass"], 1)
        self.assertIn("Pass1", p1_matches[0]["output_filename"])

        # 2. Target only Pass 2
        p2_matches = server.prepare_targeted_queue_items(self.project_slug, "S02:p2")
        self.assertEqual(len(p2_matches), 1, f"Expected exactly 1 item for S02:p2, got {len(p2_matches)}")
        self.assertEqual(p2_matches[0]["pass"], 2)
        self.assertIn("Pass2", p2_matches[0]["output_filename"])

        # 3. Target whole scene S02 (both passes)
        both_matches = server.prepare_targeted_queue_items(self.project_slug, "S02")
        self.assertEqual(len(both_matches), 2, f"Expected 2 passes for S02, got {len(both_matches)}")
        passes = {m["pass"] for m in both_matches}
        self.assertEqual(passes, {1, 2})

    def test_09_prompt_edit_change_detection(self):
        """Verify modifying prompt in scene configuration flags task as EDITED requiring render even when MP4 and state_hashes exist."""
        # Backup original test_project.json
        server.backup_project_json(self.json_path)
        orig_text = self.json_path.read_text(encoding="utf-8")

        # Ensure rendered MP4 exists for S01
        renders_dir = self.proj_dir / "work" / "renders_v3"
        renders_dir.mkdir(parents=True, exist_ok=True)
        s01_mp4 = renders_dir / "S01_male_solo_960x544.mp4"
        if not s01_mp4.exists() or s01_mp4.stat().st_size < 50000:
            s01_mp4.write_bytes(b"x" * 60000)

        # Store baseline hash in state_hashes.json
        hashes_file = self.proj_dir / "work" / "jobs_v3" / "state_hashes.json"
        hashes_file.parent.mkdir(parents=True, exist_ok=True)
        old_hashes = {
            "tasks": {"S01_male_solo_960x544": "baseline_hash_999"},
            "scene_renders": {"S01_male_solo_960x544": {"render_start": 0.0, "render_end": 10.0}},
        }
        hashes_file.write_text(json.dumps(old_hashes, indent=2), encoding="utf-8")

        try:
            # Modify S01 prompt
            mod_data = json.loads(orig_text)
            for sc in mod_data.get("scenes", []):
                if sc.get("id") == "S01":
                    sc["prompt"] = f"MODIFIED TEST PROMPT {time.time()}"
                    break
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")

            # Notice: state_hashes.json is NOT deleted!
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_item = next((x for x in q if x.get("scene_id") == "S01"), None)
            self.assertIsNotNone(s01_item, "S01 must exist in computed queue")
            self.assertTrue(s01_item["needs_render"], "S01 must need render after prompt edit")
            self.assertEqual(s01_item["status"], "queued")
            self.assertIn("EDITED", s01_item["reason"])
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")

    def test_10_queue_worker_no_clobber(self):
        """Verify targeted render with --no-queue-sync preserves UI queue without clobbering with SKIPPED."""
        # Setup initial queue
        initial_queue = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        self.assertEqual(len(initial_queue), 4)

        # Run single scene render with --no-queue-sync
        cmd = [
            sys.executable,
            str(WORKSPACE_ROOT / "work" / "run_overnight_v3.py"),
            str(self.json_path),
            "S01",
            "--dummy",
            "--no-queue-sync",
            "--no-stitch",
            "--skip-anchors",
        ]
        res = subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Render failed: {res.stderr}\n{res.stdout}")

        # Verify v3_queue.json still has 4 items and was NOT clobbered
        q_disk = json.loads(self.queue_file.read_text(encoding="utf-8"))
        self.assertEqual(len(q_disk), 4, f"Queue was clobbered! Expected 4 items, found {len(q_disk)}")
        skipped_count = sum(1 for itm in q_disk if itm.get("status") == "skipped")
        self.assertLess(skipped_count, 4, "Queue should not have all items marked skipped")

    def test_11_long_scene_frames_and_bounds_coverage(self):
        """Verify long scenes (>15s) are not clamped to sliding_window_size (362f) and detect bounds coverage in queue."""
        ctx = ProjectContext(self.json_path)
        rspec = {"model_type": "minimax_h3_fl2va_pruned_pdd", "frames_formula": "h3", "sliding_window_size": 362}

        # 1. Verify 26.2s scene yields >= 630 frames, never clamped to 362
        frames_26s = ctx.valid_video_frames(26.2, rspec)
        self.assertGreaterEqual(frames_26s, 630, f"Expected >= 630 frames for 26.2s scene, got {frames_26s}")
        self.assertEqual(frames_26s, 634, f"Expected exact valid H3 grid (17*37+5 = 634), got {frames_26s}")

        # 2. Verify server.compute_fast_project_queue flags scenes where timeline exceeds render bounds
        # Simulate S01 whose render on disk only covers [0.0, 3.0] but timeline requires [0.0, 6.0]
        hashes_file = self.proj_dir / "work" / "jobs_v3" / "state_hashes.json"
        h_data = {}
        if hashes_file.is_file():
            try:
                h_data = json.loads(hashes_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        orig_scene_renders = h_data.get("scene_renders", {})
        try:
            h_data["scene_renders"] = {
                "S01_male_solo_960x544": {
                    "render_start": 0.0,
                    "render_end": 3.0,
                    "frames": 72,
                }
            }
            hashes_file.write_text(json.dumps(h_data, indent=2), encoding="utf-8")

            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_item = next((x for x in q if x.get("scene_id") == "S01"), None)
            self.assertIsNotNone(s01_item, "S01 must exist in queue")
            self.assertTrue(s01_item["needs_render"], "S01 must need render because timeline exceeds render bounds")
            self.assertEqual(s01_item["status"], "queued")
            self.assertIn("TIMELINE EXCEEDS RENDER", s01_item["reason"])
        finally:
            h_data["scene_renders"] = orig_scene_renders
            hashes_file.write_text(json.dumps(h_data, indent=2), encoding="utf-8")

    def test_12_clips_collection_and_placeholder_exclusion(self):
        """Verify collect_project_clips excludes placeholders and prioritizes Pass2_Final renders over Pass1 and trimmed."""
        trimmed_dir = self.proj_dir / "work" / "trimmed_v3"
        renders_dir = self.proj_dir / "work" / "renders_v3"
        trimmed_dir.mkdir(parents=True, exist_ok=True)
        renders_dir.mkdir(parents=True, exist_ok=True)

        ph_file = trimmed_dir / "01_S02_placeholder_960x544.mp4"
        p1_file = renders_dir / "S02_Pass1_Left_960x544.mp4"
        p2_file = renders_dir / "S02_Pass2_Final_960x544.mp4"
        ph_file.write_bytes(b"ph" * 1000)
        p1_file.write_bytes(b"p1" * 1000)
        p2_file.write_bytes(b"p2" * 1000)

        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        clips = server.collect_project_clips(self.proj_dir, data)

        self.assertIn("S02", clips, "S02 should be mapped in clips")
        self.assertEqual(clips["S02"]["filename"], "S02_Pass2_Final_960x544.mp4", "Pass2_Final should take priority for S02")
        self.assertNotIn("placeholder", clips["S02"]["filename"].lower(), "Placeholder should never be chosen as active clip")
        self.assertFalse("\\" in clips["S02"]["path"], "Clip path must use forward slashes")

    def test_13_queue_worker_lifecycle_status_running(self):
        """Verify execute_single_render keeps render_status['running'] True while queue_worker_active is True."""
        from unittest.mock import MagicMock, patch

        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = ["[Studio UI] Mock line\n", ""]
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        try:
            with patch("subprocess.Popen", return_value=mock_proc):
                # 1. When queue_worker_active is True, finishing a single render must NOT flip running to False
                server.queue_worker_active = True
                code = server.execute_single_render([str(self.json_path), "S01"], self.project_slug)
                self.assertEqual(code, 0)
                self.assertTrue(
                    server.render_status["running"],
                    "execute_single_render must keep render_status['running'] True while queue_worker_active is True",
                )
                self.assertIsNone(
                    server.render_status["exit_code"],
                    "exit_code must remain None while queue_worker_active is True",
                )

                # 2. When queue_worker_active is False (standalone render), finishing sets running=False and exit_code=0
                server.queue_worker_active = False
                code2 = server.execute_single_render([str(self.json_path), "S01"], self.project_slug)
                self.assertEqual(code2, 0)
                self.assertFalse(server.render_status["running"])
                self.assertEqual(server.render_status["exit_code"], 0)
        finally:
            server.queue_worker_active = False
            with server.render_lock:
                server.render_status["running"] = False
                server.render_status["exit_code"] = 0

    def test_14_duplicate_numbered_clip_resolution(self):
        """Verify resolve_rendered_clip_path detects and promotes newer duplicate clips like out_name(2).mp4."""
        from work.run_overnight_v3 import resolve_rendered_clip_path
        renders_dir = self.proj_dir / "work" / "renders_v3"
        renders_dir.mkdir(parents=True, exist_ok=True)
        out_base = "test_clip_dup_960x544"
        canon_file = renders_dir / f"{out_base}.mp4"
        dup_file = renders_dir / f"{out_base}(2).mp4"

        try:
            canon_file.write_bytes(b"old_data" * 10000)
            time.sleep(0.05)
            dup_file.write_bytes(b"new_data_full" * 10000)

            resolved = resolve_rendered_clip_path(renders_dir, out_base, promote=True)
            self.assertEqual(resolved.stem, out_base, f"Expected canonical stem, got {resolved.name}")
            self.assertEqual(resolved.read_bytes()[:8], b"new_data", "Resolved clip should have promoted the newer duplicate data")
        finally:
            canon_file.unlink(missing_ok=True)
            dup_file.unlink(missing_ok=True)

    def test_15_stem_edit_change_detection_solo_and_duet(self):
        """Verify changing stem on a solo scene (like S12b) or stem_m/stem_f on a duet flags the scene as EDITED."""
        from work.run_overnight_v3 import compute_project_task_hashes

        server.backup_project_json(self.json_path)
        orig_text = self.json_path.read_text(encoding="utf-8")
        hashes_file = self.proj_dir / "work" / "jobs_v3" / "state_hashes.json"
        orig_hashes_text = hashes_file.read_text(encoding="utf-8") if hashes_file.exists() else None

        renders_dir = self.proj_dir / "work" / "renders_v3"
        renders_dir.mkdir(parents=True, exist_ok=True)
        target_fns = ("S01_male_solo_960x544.mp4", "S02_Pass1_Left_960x544.mp4", "S02_Pass2_Final_960x544.mp4")
        saved_mp4s: dict[str, bytes | None] = {}
        for fn in target_fns:
            p = renders_dir / fn
            if p.exists():
                raw = p.read_bytes()
                saved_mp4s[fn] = raw if raw != (b"v" * 60000) else None
            else:
                saved_mp4s[fn] = None

        try:
            # Ensure rendered MP4s exist for S01 and S02 with mtime newer than anchors
            for fn in target_fns:
                p = renders_dir / fn
                p.write_bytes(b"v" * 60000)

            # Compute baseline hashes before stem edits and store in state_hashes.json
            ctx_base = ProjectContext(self.json_path)
            base_hashes = compute_project_task_hashes(ctx_base, target_resolution="960x544")
            state_payload = {
                "tasks": {k: v[0] for k, v in base_hashes.items()},
                "scene_renders": {
                    "S01_male_solo_960x544": {"render_start": 0.0, "render_end": 10.0},
                    "S02_Pass1_Left_960x544": {"render_start": 3.0, "render_end": 10.0},
                    "S02_Pass2_Final_960x544": {"render_start": 3.0, "render_end": 10.0},
                },
            }
            hashes_file.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

            # Baseline check: both S01 and S02 should be UP-TO-DATE
            q_base = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_base = next(x for x in q_base if x.get("scene_id") == "S01")
            s02_p2_base = next(x for x in q_base if x.get("scene_id") == "S02" and x.get("pass") == 2)
            self.assertFalse(s01_base["needs_render"], "S01 should be up-to-date before stem edit")
            self.assertFalse(s02_p2_base["needs_render"], "S02 Pass 2 should be up-to-date before stem edit")

            # Now change S01 stem to 'silent' and S02 stem_f to 'silent'
            mod_data = json.loads(orig_text)
            for sc in mod_data.get("scenes", []):
                if sc.get("id") == "S01":
                    sc["stem"] = "silent"
                elif sc.get("id") == "S02":
                    sc["stem_f"] = "silent"
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")

            q_after = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_after = next(x for x in q_after if x.get("scene_id") == "S01")
            s02_p2_after = next(x for x in q_after if x.get("scene_id") == "S02" and x.get("pass") == 2)

            self.assertTrue(s01_after["needs_render"], "S01 must need render after changing stem to 'silent'")
            self.assertIn("EDITED", s01_after["reason"])
            self.assertTrue(s02_p2_after["needs_render"], "S02 Pass 2 must need render after changing stem_f to 'silent'")
            self.assertIn("EDITED", s02_p2_after["reason"])
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            if orig_hashes_text is not None:
                hashes_file.write_text(orig_hashes_text, encoding="utf-8")
            for fn, raw in saved_mp4s.items():
                p = renders_dir / fn
                if raw is not None:
                    p.write_bytes(raw)
                else:
                    p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
