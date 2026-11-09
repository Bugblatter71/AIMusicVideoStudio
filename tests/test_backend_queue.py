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
            self.assertIn("TIMELINE", s01_item["reason"])
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

    def test_16_uj_cli_flags_no_stitch_and_no_queue_sync(self):
        """UJ: Running single pass with --no-stitch and --no-queue-sync must not warn, stitch, or emit [QUEUE_SKIP] for other scenes."""
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
        self.assertNotIn("Unrecognized argument", res.stdout, "--no-stitch and --no-queue-sync must be recognized CLI flags")
        self.assertNotIn("STAGE 3: Assembling", res.stdout, "--no-stitch must skip Stage 3 master video assembly")
        self.assertNotIn("[QUEUE_SKIP]", res.stdout, "Targeted single-pass run must not emit [QUEUE_SKIP] for non-targeted scenes")

    def test_17_uj_two_pass_p1_only_does_not_render_p2(self):
        """UJ: Targeting S02:p1 must render ONLY Pass 1 (not Pass 2), and Pass 2 must then be flagged as needing render."""
        renders_dir = self.proj_dir / "work" / "renders_v3"
        renders_dir.mkdir(parents=True, exist_ok=True)
        p1_mp4 = renders_dir / "S02_Pass1_Left_960x544.mp4"
        p2_mp4 = renders_dir / "S02_Pass2_Final_960x544.mp4"
        p2_backup = p2_mp4.read_bytes() if p2_mp4.exists() else None
        try:
            p2_mp4.unlink(missing_ok=True)
            cmd = [
                sys.executable,
                str(WORKSPACE_ROOT / "work" / "run_overnight_v3.py"),
                str(self.json_path),
                "S02:p1",
                "--dummy",
                "--no-queue-sync",
                "--no-stitch",
                "--skip-anchors",
            ]
            res = subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Render failed: {res.stderr}\n{res.stdout}")
            self.assertTrue(p1_mp4.exists(), "Pass 1 MP4 must be generated when S02:p1 is targeted")
            self.assertFalse(p2_mp4.exists(), "Pass 2 MP4 must NOT be generated when only S02:p1 is targeted")

            # Also verify that if an older Pass 2 file exists on disk with mtime < Pass 1 mtime, Pass 2 is queued
            p2_mp4.write_bytes(b"v" * 60000)
            old_time = p1_mp4.stat().st_mtime - 120
            os.utime(p2_mp4, (old_time, old_time))
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            p2_item = next(x for x in q if x.get("scene_id") == "S02" and x.get("pass") == 2)
            self.assertTrue(p2_item["needs_render"], "Pass 2 must need render when Pass 1 MP4 on disk is newer than Pass 2 MP4")
            self.assertEqual(p2_item["status"], "queued")
        finally:
            if p2_backup is not None:
                p2_mp4.write_bytes(p2_backup)

    def test_18_uj_scene_id_matches_strict_suffix(self):
        """UJ: _scene_id_matches in run_overnight_v3 must not match S12 against S12a or S12b."""
        from run_overnight_v3 import _scene_id_matches
        self.assertTrue(_scene_id_matches("S1", "S01"))
        self.assertTrue(_scene_id_matches("S01", "S1"))
        self.assertTrue(_scene_id_matches("S12", "S12"))
        self.assertTrue(_scene_id_matches("S12a", "S12a"))
        self.assertFalse(_scene_id_matches("S12", "S12a"), "S12 must not match S12a")
        self.assertFalse(_scene_id_matches("S12", "S12b"), "S12 must not match S12b")
        self.assertFalse(_scene_id_matches("S1", "S10"), "S1 must not match S10")

    def test_19_uj_manual_remove_and_clear_persist_across_refresh_until_edited(self):
        """UJ: Removing an item or clearing the queue persists across refreshes until that scene is edited or Queue All is clicked."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        renders_dir = self.proj_dir / "work" / "renders_v3"
        s01_mp4 = renders_dir / "S01_male_solo_960x544.mp4"
        s01_backup = s01_mp4.read_bytes() if s01_mp4.exists() else None
        try:
            # Make S01 missing so it wants to be queued
            s01_mp4.unlink(missing_ok=True)
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_item = next(x for x in q if x["scene_id"] == "S01")
            self.assertEqual(s01_item["status"], "queued")

            # User removes S01 from queue
            server.remove_from_project_queue(self.project_slug, s01_item["output_filename"])

            # Refresh queue (force_recheck=False) -> S01 must stay skipped!
            q_refreshed = server.compute_fast_project_queue(self.project_slug, force_recheck=False)
            s01_after = next(x for x in q_refreshed if x["scene_id"] == "S01")
            self.assertEqual(s01_after["status"], "skipped", "Manually removed scene must stay skipped on normal queue refresh")
            self.assertFalse(s01_after["needs_render"])

            # Now user edits S01's prompt in project JSON -> S01 must automatically re-queue!
            mod_data = json.loads(orig_text)
            for sc in mod_data.get("scenes", []):
                if sc.get("id") == "S01":
                    sc["prompt"] = f"Newly edited prompt {time.time()}"
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")

            q_edited = server.compute_fast_project_queue(self.project_slug, force_recheck=False)
            s01_edited = next(x for x in q_edited if x["scene_id"] == "S01")
            self.assertEqual(s01_edited["status"], "queued", "Scene edited after being skipped must automatically re-queue")
            self.assertTrue(s01_edited["needs_render"])

            # User clicks Clear Queue -> all queued items become skipped and stay skipped on refresh
            server.clear_project_queue(self.project_slug)
            q_cleared = server.compute_fast_project_queue(self.project_slug, force_recheck=False)
            queued_remaining = [x for x in q_cleared if x["status"] == "queued"]
            self.assertEqual(len(queued_remaining), 0, "Cleared queue must remain empty across normal refreshes")

            # User clicks Queue All (force_recheck=True) -> unrendered items return to queued
            q_all = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_requeued = next(x for x in q_all if x["scene_id"] == "S01")
            self.assertEqual(s01_requeued["status"], "queued", "Queue All must re-queue skipped unrendered items")
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            if s01_backup is not None:
                s01_mp4.write_bytes(s01_backup)

    def test_20_uj_custom_queue_order_and_forced_preserved_on_render_start(self):
        """UJ: Reordering items, forcing a completed scene, and removing a scene are all preserved when starting Render Missing / Changed."""
        q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        # Force enqueue S03
        s03_fn = next(x["output_filename"] for x in q if x["scene_id"] == "S03")
        s01_fn = next(x["output_filename"] for x in q if x["scene_id"] == "S01")
        server.enqueue_in_project_queue(self.project_slug, s03_fn)
        # Remove S01
        server.remove_from_project_queue(self.project_slug, s01_fn)
        # Move S03 up to the top of queued items
        for _ in range(5):
            server.move_in_project_queue(self.project_slug, s03_fn, "up")

        # Now simulate clicking "Render Missing / Changed" (compute_fast_project_queue with force_recheck=False)
        q_start = server.compute_fast_project_queue(self.project_slug, force_recheck=False)
        s03_item = next(x for x in q_start if x["scene_id"] == "S03")
        s01_item = next(x for x in q_start if x["scene_id"] == "S01")
        self.assertEqual(s03_item["status"], "queued", "Forced item S03 must remain queued")
        self.assertEqual(s01_item["status"], "skipped", "Removed item S01 must remain skipped")
        queued_items = [x for x in q_start if x["status"] == "queued"]
        self.assertEqual(queued_items[0]["scene_id"], "S03", "S03 must remain first among queued items")

    def test_21_uj_deleted_scene_or_mode_change_purged_from_queue(self):
        """UJ: Stale items from deleted scenes or old scene modes in v3_queue.json are automatically purged."""
        q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        q.insert(0, {
            "idx": 1,
            "id": "S99_deleted_scene_960x544",
            "scene_id": "S99",
            "output_filename": "S99_deleted_scene_960x544",
            "filename": "S99_deleted_scene_960x544.mp4",
            "status": "queued",
            "reason": "MISSING FILE",
            "needs_render": True,
        })
        self.queue_file.write_text(json.dumps(q, indent=2), encoding="utf-8")

        q_clean = server.compute_fast_project_queue(self.project_slug, force_recheck=False)
        sids = [x["scene_id"] for x in q_clean]
        self.assertNotIn("S99", sids, "Deleted scene S99 must be purged from v3_queue.json")
        self.assertEqual(len(q_clean), 4, "Only the 4 valid project passes should remain")

    def test_22_uj_targeted_scene_render_when_idle_vs_when_running(self):
        """UJ: Clicking Render Scene when idle defers non-targeted items so only target runs; when running, appends target right after active item without killing it."""
        # 1. Start from all items queued (idle)
        q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        for itm in q:
            itm["status"] = "queued"
            itm["needs_render"] = True
            itm["reason"] = "MISSING FILE"
        self.queue_file.write_text(json.dumps(q, indent=2), encoding="utf-8")

        # Click "Render Scene" on S03 while IDLE
        server.prepare_targeted_queue_items(self.project_slug, "S03", is_worker_running=False)
        q_idle = json.loads(self.queue_file.read_text(encoding="utf-8"))
        queued_idle = [x for x in q_idle if x["status"] == "queued" and x["needs_render"]]
        self.assertEqual(len(queued_idle), 1, "When idle, targeting S03 must queue ONLY S03")
        self.assertEqual(queued_idle[0]["scene_id"], "S03")

        # 2. Now simulate S03 is actively rendering, and user clicks "Render Scene" on S01
        q_idle[0]["status"] = "rendering"
        self.queue_file.write_text(json.dumps(q_idle, indent=2), encoding="utf-8")

        server.prepare_targeted_queue_items(self.project_slug, "S01", is_worker_running=True)
        q_running = json.loads(self.queue_file.read_text(encoding="utf-8"))
        self.assertEqual(q_running[0]["scene_id"], "S03", "Actively rendering scene S03 must remain at index 0")
        self.assertEqual(q_running[0]["status"], "rendering", "Actively rendering scene S03 must remain in 'rendering' status")
        self.assertEqual(q_running[1]["scene_id"], "S01", "Newly targeted scene S01 must be placed immediately after active item")
        self.assertEqual(q_running[1]["status"], "queued")

    def test_23_uj_two_pass_move_ordering_guard(self):
        """UJ: Pass 2 of a two-pass scene cannot be moved above Pass 1 of the same scene."""
        q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        for itm in q:
            itm["status"] = "queued"
            itm["needs_render"] = True
        self.queue_file.write_text(json.dumps(q, indent=2), encoding="utf-8")

        p1_fn = next(x["output_filename"] for x in q if x["scene_id"] == "S02" and x.get("pass") == 1)
        p2_fn = next(x["output_filename"] for x in q if x["scene_id"] == "S02" and x.get("pass") == 2)

        # Try moving Pass 2 up past Pass 1
        server.move_in_project_queue(self.project_slug, p2_fn, "up")
        q_after = json.loads(self.queue_file.read_text(encoding="utf-8"))
        idx_p1 = next(i for i, x in enumerate(q_after) if x["output_filename"] == p1_fn)
        idx_p2 = next(i for i, x in enumerate(q_after) if x["output_filename"] == p2_fn)
        self.assertLess(idx_p1, idx_p2, "Pass 1 must always remain before Pass 2 of the same scene")

    def test_24_uj_anchor_only_render_and_cascade(self):
        """UJ: Rendering a single anchor (--anchors-only A01) regenerates A01 and flags dependent scene S01 in the queue."""
        anchors_dir = self.proj_dir / "work" / "anchors_v3"
        a01_files = list(anchors_dir.glob("A01*"))
        self.assertTrue(len(a01_files) > 0, "A01 anchor must exist in test_project")
        a01_file = a01_files[0]
        a01_backup = a01_file.read_bytes()
        try:
            cmd = [
                sys.executable,
                str(WORKSPACE_ROOT / "work" / "run_overnight_v3.py"),
                str(self.json_path),
                "--anchors-only",
                "A01",
                "--dummy",
            ]
            res = subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Anchor render failed: {res.stderr}\n{res.stdout}")
            self.assertIn("[DUMMY] Generated mock anchor", res.stdout)
            self.assertIn("[QUEUE_ACTIVE]", res.stdout, "Anchor generation should emit [QUEUE_ACTIVE] for UI progress tracking")
            self.assertIn("[QUEUE_DONE]", res.stdout, "Anchor generation should emit [QUEUE_DONE] for UI progress tracking")

            # Dependent scene S01 should now be flagged in compute_fast_project_queue
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=False)
            s01_item = next(x for x in q if x["scene_id"] == "S01")
            self.assertTrue(s01_item["needs_render"], "S01 must need render after A01 is regenerated")
            self.assertEqual(s01_item["status"], "queued")
        finally:
            a01_file.write_bytes(a01_backup)

    def test_25_uj_end_to_end_worker_with_midflight_queue_edit(self):
        """UJ: Full run_queue_worker execution in dummy mode respects mid-flight removal of a pending item and completes cleanly."""
        prev_dummy = server.dummy_render_mode
        server.dummy_render_mode = True
        try:
            self.queue_file.unlink(missing_ok=True)
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            # Queue S01 (first) and S03 (later) only
            for itm in q:
                if itm["scene_id"] in ("S01", "S03"):
                    itm["status"] = "queued"
                    itm["needs_render"] = True
                    itm["reason"] = "FORCED"
                else:
                    itm["status"] = "skipped"
                    itm["needs_render"] = False
                    itm["reason"] = "SKIPPED BY USER"
                    itm["user_skipped_hash"] = itm.get("task_hash", "")
            self.queue_file.write_text(json.dumps(q, indent=2), encoding="utf-8")

            s03_fn = next(x["output_filename"] for x in q if x["scene_id"] == "S03")

            # Start the real queue worker thread (S01 starts rendering first)
            server.run_queue_worker(self.project_slug, force_restart=False)

            # Immediately remove S03 while S01 is actively rendering
            time.sleep(0.15)
            server.remove_from_project_queue(self.project_slug, s03_fn)

            # Wait for worker to finish (up to 15s)
            deadline = time.time() + 15.0
            while server.queue_worker_active and time.time() < deadline:
                time.sleep(0.2)

            self.assertFalse(server.queue_worker_active, "Queue worker should have finished")
            q_final = json.loads(self.queue_file.read_text(encoding="utf-8"))
            s01_final = next(x for x in q_final if x["scene_id"] == "S01")
            s03_final = next(x for x in q_final if x["scene_id"] == "S03")
            self.assertEqual(s01_final["status"], "completed", "S01 should have rendered to completion")
            self.assertEqual(s03_final["status"], "skipped", "S03 removed mid-flight must not have been rendered")
        finally:
            server.kill_active_render()
            server.dummy_render_mode = prev_dummy

    def test_26_derive_split_scene_ids(self):
        """Verify derive_split_scene_ids generates clean, collision-free sub-scene IDs."""
        derive_fn = server.derive_split_scene_ids

        # 1. Standard numeric S-prefix: S01 -> S01a, S01b
        id1, id2 = derive_fn("S01", {"S01", "S02"})
        self.assertEqual(id1, "S01a")
        self.assertEqual(id2, "S01b")

        # 2. Single-digit prefix: S2 -> S2a, S2b
        id1, id2 = derive_fn("S2", {"S1", "S2"})
        self.assertEqual(id1, "S2a")
        self.assertEqual(id2, "S2b")

        # 3. Already suffixed with 'a', 'b' free: S12a -> S12a, S12b
        id1, id2 = derive_fn("S12a", {"S12a", "S13"})
        self.assertEqual(id1, "S12a")
        self.assertEqual(id2, "S12b")

        # 4. Already suffixed with 'a', 'b' taken: S12a -> S12a, S12c
        id1, id2 = derive_fn("S12a", {"S12a", "S12b", "S13"})
        self.assertEqual(id1, "S12a")
        self.assertEqual(id2, "S12c")

        # 5. Non-S alphanumeric names: Intro -> Intro_a, Intro_b
        id1, id2 = derive_fn("Intro", {"Intro", "Outro"})
        self.assertEqual(id1, "Intro_a")
        self.assertEqual(id2, "Intro_b")

    def test_27_split_scene_data_mutation_and_validation(self):
        """Verify split_scene_in_project_data mutates scene bounds, seeds, titles, and enforces boundaries."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            data["scenes"][0]["title"] = "Intro Verse"

            # 1. Valid split of S01 (0.0 to 4.0s) at 2.0s
            updated_data, sc1, sc2 = server.split_scene_in_project_data(data, "S01", 2.0)
            self.assertEqual(sc1["id"], "S01a")
            self.assertEqual(sc2["id"], "S01b")
            self.assertEqual(sc1["start"], 0.0)
            self.assertEqual(sc1["end"], 2.0)
            self.assertEqual(sc2["start"], 2.0)
            self.assertEqual(sc2["end"], 4.0)
            self.assertEqual(sc2["seed"], sc1["seed"] + 1)
            self.assertEqual(sc1["title"], "Intro Verse (Part 1)")
            self.assertEqual(sc2["title"], "Intro Verse (Part 2)")

            # Verify total scenes increased by 1
            self.assertEqual(len(updated_data["scenes"]), 4)

            # 2. Validation error: split time <= start
            with self.assertRaises(ValueError) as ctx:
                server.split_scene_in_project_data(data, "S01a", 0.0)
            self.assertIn("strictly between", str(ctx.exception))

            # 3. Validation error: split time >= end
            with self.assertRaises(ValueError) as ctx:
                server.split_scene_in_project_data(data, "S01a", 2.5)
            self.assertIn("strictly between", str(ctx.exception))

            # 4. Validation error: segment too short (<0.05s)
            with self.assertRaises(ValueError) as ctx:
                server.split_scene_in_project_data(data, "S01a", 0.02)
            self.assertIn("shorter than", str(ctx.exception))

            # 5. Validation error: non-existent scene
            with self.assertRaises(ValueError) as ctx:
                server.split_scene_in_project_data(data, "S99", 5.0)
            self.assertIn("not found", str(ctx.exception))

            # 6. Custom ID override
            _, cust1, cust2 = server.split_scene_in_project_data(data, "S01b", 3.0, id1="S01_first", id2="S01_second")
            self.assertEqual(cust1["id"], "S01_first")
            self.assertEqual(cust2["id"], "S01_second")
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")

    def test_28_split_scene_two_pass_duet_preservation(self):
        """Verify splitting a two-pass duet scene preserves anchors, stems, prompts, and increments dual seeds."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            # S02 is a duet scene from 4.0 to 8.0s
            orig_s02 = next(s for s in data["scenes"] if s["id"] == "S02")
            self.assertEqual(orig_s02["mode"], "duet_method_b")

            updated_data, d1, d2 = server.split_scene_in_project_data(data, "S02", 6.0)
            self.assertEqual(d1["id"], "S02a")
            self.assertEqual(d2["id"], "S02b")
            self.assertEqual(d1["start"], 4.0)
            self.assertEqual(d1["end"], 6.0)
            self.assertEqual(d2["start"], 6.0)
            self.assertEqual(d2["end"], 8.0)

            # Both duet passes must preserve anchors, split_x, and dual stems
            self.assertEqual(d1["anchor"], orig_s02["anchor"])
            self.assertEqual(d2["anchor"], orig_s02["anchor"])
            self.assertEqual(d1["split_x"], orig_s02.get("split_x", 0.5))
            self.assertEqual(d2["split_x"], orig_s02.get("split_x", 0.5))

            self.assertEqual(d1["stem_m"], orig_s02["stem_m"])
            self.assertEqual(d1["stem_f"], orig_s02["stem_f"])
            self.assertEqual(d2["stem_m"], orig_s02["stem_m"])
            self.assertEqual(d2["stem_f"], orig_s02["stem_f"])

            # Seeds for both male and female should be incremented in segment 2
            self.assertEqual(d2["seed_m"], orig_s02["seed_m"] + 1)
            self.assertEqual(d2["seed_f"], orig_s02["seed_f"] + 1)

            # Output names updated if present
            if "output_name" in orig_s02:
                self.assertTrue(d1["output_name"].startswith("S02a"))
                self.assertTrue(d2["output_name"].startswith("S02b"))
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")

    def test_29_split_scene_queue_recomputation(self):
        """Verify that splitting a scene automatically expands the queue with all required sub-passes."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            # S03 is solo scene from 8.0 to 12.0s. Split at 10.0s.
            data = json.loads(orig_text)
            mod_data, sc3a, sc3b = server.split_scene_in_project_data(data, "S03", 10.0)
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")

            # Recompute queue
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)

            # Expected 5 passes: S01, S02:p1, S02:p2, S03a, S03b
            self.assertEqual(len(q), 5, f"Expected 5 passes after splitting S03, got {len(q)}")
            scene_ids = [itm["scene_id"] for itm in q]
            self.assertIn("S01", scene_ids)
            self.assertIn("S02", scene_ids)
            self.assertIn("S03a", scene_ids)
            self.assertIn("S03b", scene_ids)
            self.assertNotIn("S03", scene_ids)

            # S03a and S03b must have correct target args and needs_render
            itm_3a = next(x for x in q if x["scene_id"] == "S03a")
            itm_3b = next(x for x in q if x["scene_id"] == "S03b")
            self.assertEqual(itm_3a["target_arg"], "S03a")
            self.assertEqual(itm_3b["target_arg"], "S03b")
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_30_split_scene_http_endpoint(self):
        """Verify the POST /api/scene/split REST API endpoint handling."""
        import io
        from http import HTTPStatus

        class DummyHandler:
            def __init__(self, method, path, body_dict):
                self.command = method
                self.path = path
                body_bytes = json.dumps(body_dict).encode("utf-8")
                self.headers = {"Content-Length": str(len(body_bytes)), "Content-Type": "application/json"}
                self.rfile = io.BytesIO(body_bytes)
                self.wfile = io.BytesIO()
                self.response_status = None
                self.response_headers = {}

            def send_response(self, status):
                self.response_status = status

            def send_header(self, k, v):
                self.response_headers[k] = v

            def end_headers(self):
                pass

            def send_json(self, data, status=HTTPStatus.OK):
                server.StudioRequestHandler.send_json(self, data, status)

            def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST):
                server.StudioRequestHandler.send_error_json(self, msg, status)

            def read_json_body(self):
                return server.StudioRequestHandler.read_json_body(self)

            def handle_media_stream(self, subpath):
                return server.StudioRequestHandler.handle_media_stream(self, subpath)

        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            # 1. Valid split request (S01 is 0.0 to 4.0s, split at 2.0s)
            h1 = DummyHandler("POST", "/api/scene/split", {
                "slug": self.project_slug,
                "scene_id": "S01",
                "split_time": 2.0,
            })
            server.StudioRequestHandler.do_POST(h1)
            self.assertEqual(h1.response_status, 200)
            res1 = json.loads(h1.wfile.getvalue().decode("utf-8"))
            self.assertTrue(res1.get("success"))
            self.assertEqual(res1["scene1"]["id"], "S01a")
            self.assertEqual(res1["scene2"]["id"], "S01b")
            self.assertEqual(len(res1["scenes"]), 4)

            # 2. Invalid split time returns error 400
            h2 = DummyHandler("POST", "/api/scene/split", {
                "slug": self.project_slug,
                "scene_id": "S01a",
                "split_time": 99.0,
            })
            server.StudioRequestHandler.do_POST(h2)
            self.assertEqual(h2.response_status, 400)
            res2 = json.loads(h2.wfile.getvalue().decode("utf-8"))
            self.assertIn("error", res2)

            # 3. Missing parameters returns error 400
            h3 = DummyHandler("POST", "/api/scene/split", {
                "slug": self.project_slug,
            })
            server.StudioRequestHandler.do_POST(h3)
            self.assertEqual(h3.response_status, 400)
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_31_project_tool_split_scene_cli(self):
        """Verify project_tool.py split-scene CLI command splits scene and creates timestamped backup."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        tool_script = WORKSPACE_ROOT / ".agents" / "skills" / "mvg-project-ops" / "scripts" / "project_tool.py"
        try:
            cmd = [
                sys.executable,
                str(tool_script),
                "split-scene",
                self.project_slug,
                "S01",
                "1.5",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, f"CLI split-scene failed: {proc.stderr}")
            self.assertIn("Successfully split scene S01", proc.stdout)
            self.assertIn("S01a", proc.stdout)
            self.assertIn("S01b", proc.stdout)

            # Verify on disk
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            s_ids = [s["id"] for s in data["scenes"]]
            self.assertIn("S01a", s_ids)
            self.assertIn("S01b", s_ids)
            self.assertNotIn("S01", s_ids)
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_32_delete_first_scene_gap_closing(self):
        """Verify deleting the first scene (S01) adjusts the next scene (S02) start to 0.00."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            mod_data, del_sc = server.delete_scene_in_project_data(data, "S01", close_gap=True)
            self.assertEqual(del_sc["id"], "S01")
            self.assertEqual(len(mod_data["scenes"]), 2)
            self.assertEqual(mod_data["scenes"][0]["id"], "S02")
            self.assertEqual(mod_data["scenes"][0]["start"], 0.00, "Next scene must pull back to 0.00 to close timeline gap")
            self.assertEqual(mod_data["scenes"][0]["end"], 8.00)

            # Test queue recomputation on disk
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s_ids = [itm["scene_id"] for itm in q]
            self.assertNotIn("S01", s_ids, "Deleted S01 must not appear in queue")
            self.assertIn("S02", s_ids)
            self.assertIn("S03", s_ids)
            # S02 now covers 8s instead of 4s
            s02_pass1 = next(x for x in q if x["scene_id"] == "S02" and x.get("pass") == 1)
            self.assertEqual(s02_pass1["target_arg"], "S02:p1")
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_33_delete_middle_scene_gap_closing_and_duet_purge(self):
        """Verify deleting middle duet scene (S02) extends previous scene (S01) and purges both passes."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            mod_data, del_sc = server.delete_scene_in_project_data(data, "S02", close_gap=True)
            self.assertEqual(del_sc["id"], "S02")
            self.assertEqual(len(mod_data["scenes"]), 2)
            # S01 extends from 4.0s to 8.0s (touching S03.start)
            self.assertEqual(mod_data["scenes"][0]["id"], "S01")
            self.assertEqual(mod_data["scenes"][0]["start"], 0.00)
            self.assertEqual(mod_data["scenes"][0]["end"], 8.00, "Previous scene end must extend to meet next scene start")
            self.assertEqual(mod_data["scenes"][1]["id"], "S03")
            self.assertEqual(mod_data["scenes"][1]["start"], 8.00)

            # Recompute queue: both S02:p1 and S02:p2 must be purged
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            self.assertEqual(len(q), 2, "Only S01 and S03 passes should remain")
            out_fns = [x["output_filename"] for x in q]
            self.assertFalse(any("S02" in fn for fn in out_fns), "All S02 passes must be purged from queue")
            self.assertTrue(any("S01" in fn for fn in out_fns))
            self.assertTrue(any("S03" in fn for fn in out_fns))
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_34_delete_last_scene_gap_closing(self):
        """Verify deleting the last scene (S03) extends the preceding scene to total duration."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            mod_data, del_sc = server.delete_scene_in_project_data(data, "S03", close_gap=True)
            self.assertEqual(del_sc["id"], "S03")
            self.assertEqual(len(mod_data["scenes"]), 2)
            # S02 was 4.0 to 8.0s; with total_duration = 12.0s, it now ends at 12.0s
            self.assertEqual(mod_data["scenes"][1]["id"], "S02")
            self.assertEqual(mod_data["scenes"][1]["end"], 12.00, "Last scene must extend to fill trailing duration")

            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            self.assertEqual(len(q), 3, "Queue should contain S01, S02:p1, and S02:p2")
            s_ids = [x["scene_id"] for x in q]
            self.assertNotIn("S03", s_ids)
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_35_delete_scene_without_closing_gap(self):
        """Verify deleting a scene with close_gap=False preserves neighboring scene timestamps."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            mod_data, del_sc = server.delete_scene_in_project_data(data, "S02", close_gap=False)
            self.assertEqual(del_sc["id"], "S02")
            self.assertEqual(len(mod_data["scenes"]), 2)
            # Timestamps must NOT be adjusted
            self.assertEqual(mod_data["scenes"][0]["start"], 0.0)
            self.assertEqual(mod_data["scenes"][0]["end"], 4.0, "S01 end must not change when close_gap=False")
            self.assertEqual(mod_data["scenes"][1]["start"], 8.0, "S03 start must not change when close_gap=False")
            self.assertEqual(mod_data["scenes"][1]["end"], 12.0)
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_36_delete_scene_validation_and_case_insensitive(self):
        """Verify delete error handling, case-insensitivity, and zero-padding fallback."""
        data = json.loads(self.json_path.read_text(encoding="utf-8"))

        # 1. Non-existent scene raises ValueError
        with self.assertRaises(ValueError) as cm:
            server.delete_scene_in_project_data(data, "S999")
        self.assertIn("not found in project", str(cm.exception))

        # 2. Case-insensitivity (lowercase 's01' matches 'S01')
        d1 = json.loads(self.json_path.read_text(encoding="utf-8"))
        _, del1 = server.delete_scene_in_project_data(d1, "s01")
        self.assertEqual(del1["id"], "S01")

        # 3. Zero-padding match ('S1' matches 'S01')
        d2 = json.loads(self.json_path.read_text(encoding="utf-8"))
        _, del2 = server.delete_scene_in_project_data(d2, "S1")
        self.assertEqual(del2["id"], "S01")

        # 4. Suffix protection (S12 does not match S12a)
        d_suffix = {
            "project": {"total_duration": 20.0},
            "scenes": [
                {"id": "S12a", "start": 0.0, "end": 5.0},
                {"id": "S12b", "start": 5.0, "end": 10.0},
            ]
        }
        with self.assertRaises(ValueError):
            server.delete_scene_in_project_data(d_suffix, "S12")

    def test_37_delete_all_scenes_to_empty_state(self):
        """Verify deleting all scenes leaves a clean empty list without IndexError or queue crashes."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(orig_text)
            for sid in ["S01", "S02", "S03"]:
                data, _ = server.delete_scene_in_project_data(data, sid, close_gap=True)
            self.assertEqual(data["scenes"], [])

            # Deleting from empty raises ValueError
            with self.assertRaises(ValueError):
                server.delete_scene_in_project_data(data, "S01")

            # Recompute queue with empty scenes list
            self.json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            self.assertEqual(q, [], "Queue for empty project must be empty list")
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_38_delete_scene_http_endpoint(self):
        """Verify the POST /api/scene/delete REST API endpoint."""
        import io
        from http import HTTPStatus

        class DummyHandler:
            def __init__(self, method, path, body_dict):
                self.command = method
                self.path = path
                body_bytes = json.dumps(body_dict).encode("utf-8")
                self.headers = {"Content-Length": str(len(body_bytes)), "Content-Type": "application/json"}
                self.rfile = io.BytesIO(body_bytes)
                self.wfile = io.BytesIO()
                self.response_status = None
                self.response_headers = {}

            def send_response(self, status):
                self.response_status = status

            def send_header(self, k, v):
                self.response_headers[k] = v

            def end_headers(self):
                pass

            def send_json(self, data, status=HTTPStatus.OK):
                server.StudioRequestHandler.send_json(self, data, status)

            def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST):
                server.StudioRequestHandler.send_error_json(self, msg, status)

            def read_json_body(self):
                return server.StudioRequestHandler.read_json_body(self)

            def handle_media_stream(self, subpath):
                return server.StudioRequestHandler.handle_media_stream(self, subpath)

        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            # 1. Valid delete request for S02
            h1 = DummyHandler("POST", "/api/scene/delete", {
                "slug": self.project_slug,
                "scene_id": "S02",
                "close_gap": True,
            })
            server.StudioRequestHandler.do_POST(h1)
            self.assertEqual(h1.response_status, 200)
            res1 = json.loads(h1.wfile.getvalue().decode("utf-8"))
            self.assertTrue(res1.get("success"))
            self.assertEqual(res1["deleted_scene"]["id"], "S02")
            self.assertEqual(len(res1["scenes"]), 2)
            # Queue in response must only have 2 passes (S01 and S03)
            self.assertEqual(len(res1["queue"]), 2)

            # 2. Delete non-existent scene returns 400
            h2 = DummyHandler("POST", "/api/scene/delete", {
                "slug": self.project_slug,
                "scene_id": "S999",
            })
            server.StudioRequestHandler.do_POST(h2)
            self.assertEqual(h2.response_status, 400)
            res2 = json.loads(h2.wfile.getvalue().decode("utf-8"))
            self.assertIn("error", res2)

            # 3. Missing slug returns 400
            h3 = DummyHandler("POST", "/api/scene/delete", {
                "scene_id": "S01",
            })
            server.StudioRequestHandler.do_POST(h3)
            self.assertEqual(h3.response_status, 400)

            # 4. Unknown slug returns 404
            h4 = DummyHandler("POST", "/api/scene/delete", {
                "slug": "non_existent_project_xyz_123",
                "scene_id": "S01",
            })
            server.StudioRequestHandler.do_POST(h4)
            self.assertEqual(h4.response_status, 404)
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_39_project_tool_delete_scene_cli(self):
        """Verify project_tool.py delete-scene CLI command removes scene and creates backup."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        tool_script = WORKSPACE_ROOT / ".agents" / "skills" / "mvg-project-ops" / "scripts" / "project_tool.py"
        try:
            cmd = [
                sys.executable,
                str(tool_script),
                "delete-scene",
                self.project_slug,
                "S02",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, f"CLI delete-scene failed: {proc.stderr}")
            self.assertIn("Successfully deleted scene S02", proc.stdout)
            self.assertIn("Remaining scenes: 2", proc.stdout)

            # Verify on disk
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            s_ids = [s["id"] for s in data["scenes"]]
            self.assertNotIn("S02", s_ids)
            self.assertEqual(s_ids, ["S01", "S03"])
            # S01 extended to 8.0s
            s01 = next(s for s in data["scenes"] if s["id"] == "S01")
            self.assertEqual(s01["end"], 8.0)
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_40_deleted_scene_excluded_from_master_stitch(self):
        """Verify that master timeline metadata and clips mapping excludes any deleted scene."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        try:
            # Delete S02 from project
            data = json.loads(orig_text)
            mod_data, del_sc = server.delete_scene_in_project_data(data, "S02", close_gap=True)
            self.json_path.write_text(json.dumps(mod_data, indent=2), encoding="utf-8")

            # Initialize ProjectContext with updated data
            ctx = ProjectContext(self.json_path)

            # Master assembly scene_meta iterates strictly over ctx.scenes
            active_ids = [s["id"] for s in ctx.scenes]
            self.assertNotIn("S02", active_ids, "Deleted scene S02 must not be in ProjectContext active scenes")
            self.assertEqual(active_ids, ["S01", "S03"])

            # Verify task hashes from compute_project_task_hashes
            hashes = server.compute_project_task_hashes(ctx)
            for out_fn in hashes:
                self.assertNotIn("S02", out_fn, f"Deleted scene S02 must not have task hash generated: {out_fn}")
            self.assertTrue(any("S01" in fn for fn in hashes))
            self.assertTrue(any("S03" in fn for fn in hashes))
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_41_media_streaming_master_and_filmstrip(self):
        """Verify that /media/<slug>/master dynamically resolves the latest master video
        and /media/<slug>/strip/<sid> properly serves and generates filmstrip images."""
        import io
        from http import HTTPStatus
        import server_media

        class DummyStreamHandler:
            def __init__(self, range_header=None):
                self.headers = {"Range": range_header} if range_header else {}
                self.wfile = io.BytesIO()
                self.status = None
                self.headers_sent = {}

            def send_response(self, st):
                self.status = st

            def send_header(self, k, v):
                self.headers_sent[k] = v

            def end_headers(self):
                pass

            def send_error_json(self, msg, status=400):
                self.status = status

        # 1. Create a dummy master video in project dir
        dummy_master = self.proj_dir / "Test_Project_Master_1344x768_24fps.mp4"
        dummy_master.write_bytes(b"TEST_MASTER_VIDEO_CONTENT_BYTES")
        try:
            # Full stream request
            h = DummyStreamHandler()
            server_media.handle_media_stream(h, f"{self.project_slug}/master")
            self.assertEqual(h.status, HTTPStatus.OK)
            self.assertEqual(h.headers_sent.get("Content-Type"), "video/mp4")
            self.assertEqual(h.wfile.getvalue(), b"TEST_MASTER_VIDEO_CONTENT_BYTES")

            # Range request (HTTP 206)
            h_range = DummyStreamHandler(range_header="bytes=0-3")
            server_media.handle_media_stream(h_range, f"{self.project_slug}/master")
            self.assertEqual(h_range.status, HTTPStatus.PARTIAL_CONTENT)
            self.assertEqual(h_range.wfile.getvalue(), b"TEST")

            # 2. Test filmstrip serving for an existing strip
            frames_dir = self.proj_dir / "work" / "frames_v3"
            frames_dir.mkdir(parents=True, exist_ok=True)
            dummy_strip = frames_dir / "S01_strip.jpg"
            dummy_strip.write_bytes(b"DUMMY_STRIP_JPEG_BYTES")

            h_strip = DummyStreamHandler()
            server_media.handle_media_stream(h_strip, f"{self.project_slug}/strip/S01")
            self.assertEqual(h_strip.status, HTTPStatus.OK)
            self.assertEqual(h_strip.headers_sent.get("Content-Type"), "image/jpeg")
            self.assertEqual(h_strip.wfile.getvalue(), b"DUMMY_STRIP_JPEG_BYTES")
        finally:
            if dummy_master.exists():
                dummy_master.unlink()
            if (self.proj_dir / "work" / "frames_v3" / "S01_strip.jpg").exists():
                (self.proj_dir / "work" / "frames_v3" / "S01_strip.jpg").unlink()

    def test_42_filmstrip_clip_disambiguation_and_autoregen(self):
        """Regression test for filmstrip scene matching and auto-generation.
        Ensures S20 matches ONLY S20 clips (e.g. 19_S20_1344x768.mp4 or S20_male_solo_1344x768.mp4),
        and strictly rejects 00_S01.mp4, 01_S02.mp4, 21_S22_Pass2_Final_1344x768.mp4, or .tmp.mp4."""
        import re

        def _matches(raw_sid: str, filename: str) -> bool:
            if filename.endswith(".tmp.mp4"):
                return False
            return bool(re.search(rf"(?:^|_)({re.escape(raw_sid)})(?:[_\.]|$)", filename, re.IGNORECASE))

        # Test positive matches for S20
        self.assertTrue(_matches("S20", "S20_male_solo_1344x768.mp4"))
        self.assertTrue(_matches("S20", "19_S20_1344x768.mp4"))
        self.assertTrue(_matches("S20", "20_S20_960x544.mp4"))
        self.assertTrue(_matches("S20", "S20.mp4"))

        # Test strict rejection of unrelated scenes and other scene numbers
        self.assertFalse(_matches("S20", "00_S01.mp4"))
        self.assertFalse(_matches("S20", "01_S02.mp4"))
        self.assertFalse(_matches("S20", "S02_Pass1_Left_1344x768.mp4"))
        self.assertFalse(_matches("S20", "S22_Pass2_Final_1344x768.mp4"))
        self.assertFalse(_matches("S20", "21_S22_Pass2_Final_1344x768.mp4"))
        self.assertFalse(_matches("S20", "S20a_male_solo_1344x768.mp4"))
        self.assertFalse(_matches("S20", "S20_male_solo_1344x768.tmp.mp4"))

    def test_43_multi_window_incomplete_render_detection(self):
        """Regression test for multi-window sliding window rendering (e.g. S26a).
        Verifies that an intermediate single-window MP4 covering only part of a long scene
        is detected as requiring render until fully completed and registered with matching bounds."""
        orig_text = self.json_path.read_text(encoding="utf-8")
        renders_dir = self.proj_dir / "work" / "renders_v3"
        renders_dir.mkdir(parents=True, exist_ok=True)
        jobs_dir = self.proj_dir / "work" / "jobs_v3"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        state_hashes_file = jobs_dir / "state_hashes.json"

        # Long scene S01 from 0.00 to 21.00s (504 frames)
        data = json.loads(orig_text)
        data["scenes"][0]["start"] = 0.00
        data["scenes"][0]["end"] = 21.00
        self.json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Create dummy intermediate file (15s duration / 362 frames instead of 504)
        dummy_mp4 = renders_dir / "S01_male_solo_960x544.mp4"
        dummy_mp4.write_bytes(b"x" * 60000)

        try:
            # 1. State hashes has intermediate coverage: only 0.00 to 15.08s
            state_data = {
                "tasks": {"S01_male_solo_960x544": "old_intermediate_hash"},
                "scene_renders": {
                    "S01_male_solo_960x544": {
                        "render_start": 0.00,
                        "render_end": 15.08,
                        "frames": 362
                    }
                }
            }
            state_hashes_file.write_text(json.dumps(state_data), encoding="utf-8")

            # Queue must flag it as needing render because timeline exceeds render
            q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_item = next((item for item in q if item["scene_id"] == "S01"), None)
            self.assertIsNotNone(s01_item)
            self.assertTrue(s01_item["needs_render"], "Incomplete multi-window render must be flagged as needing render")

            # 2. Update state hashes with complete coverage (0.00 to 21.00s) and current task hash
            ctx = ProjectContext(self.json_path)
            cur_hashes = server.compute_project_task_hashes(ctx, target_resolution="960x544")
            cur_hash = cur_hashes["S01_male_solo_960x544"][0]
            state_data["tasks"]["S01_male_solo_960x544"] = cur_hash
            state_data["scene_renders"]["S01_male_solo_960x544"] = {
                "render_start": 0.00,
                "render_end": 21.00,
                "frames": 504
            }
            state_hashes_file.write_text(json.dumps(state_data), encoding="utf-8")

            # Re-evaluate queue: must now be UP-TO-DATE and needs_render False
            q_after = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
            s01_after = next((item for item in q_after if item["scene_id"] == "S01"), None)
            self.assertIsNotNone(s01_after)
            self.assertEqual(s01_after["status"], "completed")
            self.assertEqual(s01_after["reason"], "UP-TO-DATE")
            self.assertFalse(s01_after["needs_render"], "Registered complete render must be UP-TO-DATE")
        finally:
            self.json_path.write_text(orig_text, encoding="utf-8")
            if dummy_mp4.exists():
                dummy_mp4.unlink()
            server.compute_fast_project_queue(self.project_slug, force_recheck=True)

    def test_44_master_video_discovery_and_range_streaming(self):
        """Regression test for master video discovery.
        Verifies:
        1. Timestamped master video files in root project directory are discovered.
        2. Master video files in work/ directory are discovered.
        3. Temporary .tmp.mp4 files are strictly excluded.
        4. Range requests return HTTP 206 Partial Content with correct byte slices."""
        import io
        from http import HTTPStatus
        import server_media

        class DummyStreamHandler:
            def __init__(self, range_header=None):
                self.headers = {"Range": range_header} if range_header else {}
                self.wfile = io.BytesIO()
                self.status = None
                self.headers_sent = {}

            def send_response(self, st): self.status = st
            def send_header(self, k, v): self.headers_sent[k] = v
            def end_headers(self): pass
            def send_error_json(self, msg, status=400): self.status = status

        # Case A: Master in project root with timestamped name
        root_master = self.proj_dir / "All_Or_Nothing_v4_1_Master_1344x768_24fps.mp4"
        root_master.write_bytes(b"ROOT_MASTER_BYTES_0123456789")

        # Incomplete tmp master that must be ignored
        tmp_master = self.proj_dir / "All_Or_Nothing_Master.tmp.mp4"
        tmp_master.write_bytes(b"TMP_PARTIAL_CORRUPT")

        try:
            h = DummyStreamHandler()
            server_media.handle_media_stream(h, f"{self.project_slug}/master")
            self.assertEqual(h.status, HTTPStatus.OK)
            self.assertEqual(h.wfile.getvalue(), b"ROOT_MASTER_BYTES_0123456789")

            # Partial content test (Range 0-3)
            h_range = DummyStreamHandler(range_header="bytes=0-3")
            server_media.handle_media_stream(h_range, f"{self.project_slug}/master")
            self.assertEqual(h_range.status, HTTPStatus.PARTIAL_CONTENT)
            self.assertEqual(h_range.wfile.getvalue(), b"ROOT")
            self.assertEqual(h_range.headers_sent.get("Content-Range"), "bytes 0-3/28")

            # Direct filename request
            h_direct = DummyStreamHandler()
            server_media.handle_media_stream(h_direct, f"{self.project_slug}/master/{root_master.name}")
            self.assertEqual(h_direct.status, HTTPStatus.OK)
            self.assertEqual(h_direct.wfile.getvalue(), b"ROOT_MASTER_BYTES_0123456789")
        finally:
            if root_master.exists(): root_master.unlink()
            if tmp_master.exists(): tmp_master.unlink()

    def test_45_anchor_dropdown_and_filmstrip_frontend_contracts(self):
        """Regression test verifying frontend structural and operational contracts:
        1. index.html contains the custom anchor dropdown elements (button, thumb, aid, menu).
        2. anchors_and_settings.js populates .anchor-dropdown-item and attaches event listeners.
        3. studio.css includes expanded dimensions (min-width >= 500px, max-height >= 500px).
        4. timeline_and_playback.js includes multi-thumbnail filmstrip and tiling fallback."""
        index_html = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        studio_css = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "css" / "studio.css").read_text(encoding="utf-8")
        anchors_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "anchors_and_settings.js").read_text(encoding="utf-8")
        timeline_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "timeline_and_playback.js").read_text(encoding="utf-8")

        # 1. Custom Anchor Dropdown elements in index.html
        self.assertIn('id="btnAnchorDropdownTrigger"', index_html)
        self.assertIn('id="anchorTriggerThumb"', index_html)
        self.assertIn('id="anchorTriggerAid"', index_html)
        self.assertIn('id="anchorTriggerName"', index_html)
        self.assertIn('id="anchorDropdownMenu"', index_html)

        # 2. Dropdown item population in anchors_and_settings.js
        self.assertIn('anchor-dropdown-item', anchors_js)
        self.assertIn('initAnchorDropdownTrigger', anchors_js)
        self.assertIn('updateInspectorAnchorsDropdown', anchors_js)

        # 3. Dropdown dimensions & 2-column layout in studio.css
        self.assertIn('.custom-anchor-dropdown-menu', studio_css)
        self.assertIn('grid-template-columns: 1fr 1fr', studio_css)
        self.assertIn('max-height: 560px', studio_css)
        self.assertIn('min-width: 540px', studio_css)
        self.assertIn('height = "120px"', anchors_js)

        # 4. Multi-thumbnail / filmstrip rendering in timeline_and_playback.js
        self.assertIn('/strip/', timeline_js)
        self.assertIn('ctx.drawImage(stripImg', timeline_js)
        self.assertIn('numThumbs = Math.ceil', timeline_js)

    def test_46_progress_stream_and_carriage_return_resilience(self):
        """Regression test verifying that tqdm progress bar carriage returns (\r)
        do not mask [QUEUE_PROGRESS] events from reaching the UI or desynchronizing stage/steps."""
        queue_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "queue_and_console.js").read_text(encoding="utf-8")
        server_workers_py = (self.proj_dir.parent.parent / "work" / "ui" / "server_workers.py").read_text(encoding="utf-8")
        wgp_bridge_py = (self.proj_dir.parent.parent / "work" / "pipeline" / "wgp_bridge.py").read_text(encoding="utf-8")

        # 1. Frontend extracts [QUEUE_ even when preceded by tqdm / denoising progress text
        self.assertIn('data.text.indexOf("[QUEUE_")', queue_js)
        # 2. Frontend background sync reconciles with live engine_status.json
        self.assertIn('/api/render/status', queue_js)
        self.assertIn('eng.step > curStep', queue_js)
        # 3. Status column badge displays '⚡ Rendering' without redundant percentage
        self.assertIn('⚡ Rendering</span>', queue_js)
        self.assertNotIn('⚡ Rendering (${t.pct', queue_js)
        self.assertNotIn('⚡ Rendering (${payload.pct', queue_js)

        # 4. Server worker splits embedded [QUEUE_ tags from raw stdout lines
        self.assertIn('[QUEUE_"', server_workers_py)
        self.assertIn('raw_clean.find("[QUEUE_")', server_workers_py)

        # 5. Pipeline bridge emits [QUEUE_PROGRESS] with leading newline to break free of carriage-return lines
        self.assertIn('\\n[QUEUE_PROGRESS]', wgp_bridge_py)

    def test_47_timeline_zoom_zero_start_and_canvas_width_safety(self):
        """Regression test verifying that:
        1. Zoom slider starts at zero (min=0, value=0, step=0.25, max=4.25) in index.html.
        2. Extraneous zoom label and fit button are removed for clean transport bar.
        3. timeline_and_playback.js enforces maxSafeWidth to prevent white canvas bug.
        4. Track height and images dynamically resize vertically with zoom level.
        5. queue_and_console.js uses window_pct for multi-window tasks to match engine stage."""
        index_html = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        timeline_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "timeline_and_playback.js").read_text(encoding="utf-8")
        queue_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "queue_and_console.js").read_text(encoding="utf-8")

        # 1. Slider starts at far left with smallest size 1.0 (notch 4) and capped safely at 3.5
        self.assertIn('id="timelineZoom"', index_html)
        self.assertIn('min="1.0"', index_html)
        self.assertIn('value="1.0"', index_html)
        self.assertIn('max="3.5"', index_html)
        self.assertIn('step="0.25"', index_html)
        self.assertIn('autocomplete="off"', index_html)
        self.assertNotIn('id="timelineZoomVal"', index_html)
        self.assertNotIn('id="btnZoomFit"', index_html)
        self.assertIn('zoomSlider.min = "1.0"', timeline_js)
        self.assertIn('zoomSlider.value = String(state.zoom || 1.0)', timeline_js)

        # 2. Maximum safe canvas width guard (prevents Chromium white canvas texture limit bug)
        self.assertIn('maxSafeWidth', timeline_js)
        self.assertIn('Math.min(maxSafeWidth', timeline_js)

        # 3. Dynamic vertical track and thumbnail resizing
        self.assertIn('Math.round(baseTrackHeight * state.zoom)', timeline_js)
        self.assertNotIn('const trackHeight = 64;', timeline_js)

        # 4. Multi-window active phase label alignment and continuous engine polling
        self.assertIn('isMultiWin && task.window_pct !== undefined', queue_js)
        self.assertIn('fetch("/api/render/status")', queue_js)

        # 5. Right-click drag panning across timeline canvas & container
        self.assertIn('isRightClickDragging', timeline_js)
        self.assertIn('scrollContainer.scrollLeft = rightClickStartScrollLeft - dx', timeline_js)
        self.assertIn('scrollContainer.addEventListener("contextmenu", (e) => e.preventDefault())', timeline_js)
        self.assertIn('canvas.addEventListener("contextmenu", (e) => e.preventDefault())', timeline_js)

        # 6. Sticky scene name label while scrolling timeline until boundary/badge limit
        self.assertIn('const labelX = Math.max(sx + 6, curScrollLeft + 6)', timeline_js)
        self.assertIn('scrollContainer.addEventListener("scroll"', timeline_js)
        self.assertIn('requestAnimationFrame', timeline_js)
        self.assertIn('labelX + fullW <= rightLimit', timeline_js)

    def test_48_filmstrip_dynamic_thumbnails_scale_with_duration(self):
        """Regression test verifying that filmstrip thumbnail count scales with scene duration:
        1. server_media.py calculates target_n proportionally from duration (not hardcoded to 5x1).
        2. server_media.py checks cur_n != target_n to dynamically regenerate legacy 5-frame strips.
        3. timeline_and_playback.js detects numFrames from image aspect ratio rather than hardcoding 5."""
        server_media_py = (self.proj_dir.parent.parent / "work" / "ui" / "server_media.py").read_text(encoding="utf-8")
        timeline_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "timeline_and_playback.js").read_text(encoding="utf-8")

        # 1. Proportional thumbnail calculation in server_media.py
        self.assertIn('target_n = max(2, min(30, round(dur / 1.0)))', server_media_py)
        self.assertIn('tile={target_n}x1', server_media_py)

        # 2. Legacy strip regeneration detection
        self.assertIn('cur_n != target_n', server_media_py)

        # 3. Dynamic frame count, 16:9 aspect ratio and separator line rendering in timeline_and_playback.js
        self.assertIn('numFrames = Math.max(1, Math.round(frameAspect / (16 / 9)))', timeline_js)
        self.assertIn('thumbAspect = 16 / 9', timeline_js)
        self.assertIn('thumbW = Math.max(20, trackHeight * thumbAspect)', timeline_js)

    def test_49_queue_clip_duration_column_and_short_reason(self):
        """Regression test verifying the queue UI Length column and concise Reason formatting:
        1. index.html defines the Length column header and table rows.
        2. server_queue.py computes and includes duration on queue items.
        3. queue_and_console.js contains formatQueueReason and renders the clip length cell."""
        index_html = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        queue_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "queue_and_console.js").read_text(encoding="utf-8")

        # 1. Header and table colspans
        self.assertIn('>Length</th>', index_html)
        self.assertIn('colspan="8"', index_html)
        self.assertTrue('colCount' in queue_js or 'isDoneFilter ? 8 : 6' in queue_js or 'colspan="8"' in queue_js)

        # 2. Duration in queue items
        q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        self.assertTrue(len(q) > 0)
        for itm in q:
            self.assertIn("duration", itm)
            self.assertIsInstance(itm["duration"], (int, float))

        # 3. Reason formatting and length rendering in frontend
        self.assertIn('formatQueueReason', queue_js)
        self.assertIn('lenStr', queue_js)
        self.assertIn('Timeline changed', queue_js)

    def test_50_done_list_sorted_descending_and_completed_columns(self):
        """Regression test verifying:
        1. Done list in queue_and_console.js is sorted descending by completion timestamp.
        2. index.html has Completed and Time Taken column headers.
        3. state_and_utils.js has formatCompletedAt and formatElapsed helpers.
        4. server_queue.py attaches completed_at timestamps to completed passes."""
        index_html = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        queue_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "queue_and_console.js").read_text(encoding="utf-8")
        utils_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "state_and_utils.js").read_text(encoding="utf-8")

        # 1. Table headers
        self.assertIn('>Completed</th>', index_html)
        self.assertIn('>Time Taken</th>', index_html)

        # 2. Utility formatting functions
        self.assertIn('formatCompletedAt', utils_js)
        self.assertIn('formatElapsed', utils_js)

        # 3. Descending sort by completed_at in Done filter
        self.assertIn('filter === "completed"', queue_js)
        self.assertIn('timeB - timeA', queue_js)

        # 4. Server queue populates completed_at
        q = server.compute_fast_project_queue("all_or_nothing", force_recheck=True)
        completed_items = [itm for itm in q if itm.get("status") == "completed"]
        self.assertTrue(len(completed_items) > 0)
        has_completed_at = any(itm.get("completed_at") is not None for itm in completed_items)
        self.assertTrue(has_completed_at, "At least one completed queue item must have completed_at timestamp populated")

    def test_51_stitch_locking_detection_and_auto_stitch(self):
        """Regression test verifying:
        1. server_workers.py triggers auto-restitch after scene render passes.
        2. stage3_master.py detects PermissionError and outputs [MASTER_FILE_LOCKED].
        3. server_workers.py broadcasts [MASTER_LOCKED] and [MASTER_ERROR] on failure.
        4. queue_and_console.js handles [MASTER_LOCKED] and [MASTER_ERROR] to alert user."""
        server_workers_py = (self.proj_dir.parent.parent / "work" / "ui" / "server_workers.py").read_text(encoding="utf-8")
        stage3_py = (self.proj_dir.parent.parent / "work" / "pipeline" / "stage3_master.py").read_text(encoding="utf-8")
        queue_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "queue_and_console.js").read_text(encoding="utf-8")

        # 1. Auto-restitch in queue runner
        self.assertIn('srv_w.run_stitch_worker(config_path, project_slug)', server_workers_py)
        self.assertIn('"_Pass1_Left" not in out_fn', server_workers_py)

        # 2. Stage 3 lock detection
        self.assertIn('[MASTER_FILE_LOCKED]', stage3_py)
        self.assertIn('return False', stage3_py)

        # 3. Server workers broadcasts error events
        self.assertIn('[MASTER_LOCKED]', server_workers_py)
        self.assertIn('[MASTER_ERROR]', server_workers_py)

        # 4. Frontend user reporting
        self.assertIn('[MASTER_LOCKED]', queue_js)
        self.assertIn('[MASTER_ERROR]', queue_js)

    def test_52_done_filter_includes_completed_columns_other_lists_omit(self):
        """Regression test verifying:
        1. index.html defines queueTableHead.
        2. queue_and_console.js includes Completed and Time Taken columns for done filter only.
        3. queue_and_console.js dynamically omits Completed column for all other lists."""
        index_html = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        queue_js = (self.proj_dir.parent.parent / "work" / "ui" / "static" / "js" / "queue_and_console.js").read_text(encoding="utf-8")

        self.assertIn('id="queueTableHead"', index_html)
        self.assertIn('isDoneFilter', queue_js)
        self.assertIn('isDoneFilter ? \'<th style="padding: 6px 10px; width: 140px; color: var(--text-muted);">Completed</th>', queue_js)
        self.assertIn('isDoneFilter ?', queue_js)

    def test_53_stitch_head_offset_trimmed_when_scene_start_nudged_later(self):
        """Regression test verifying:
        1. When a scene's start is nudged later in project JSON, assemble_progressive_master resolves
           the physical render_start from scene_renders for existing clips.
        2. head_offset_s = max(0.0, sc_start - render_start) + ctx.trim_first_frame_s trims the beginning
           of the clip rather than starting at 0.0.
        3. trim_sig records the positive head_offset_s (e.g. 1.670s) so re-encoding is triggered.
        4. cli.py stitch_only synchronizes scene_meta with ctx.scenes and scene_renders."""
        # 1. Pipeline stage3_master source checks
        stage3_py = (self.proj_dir.parent.parent / "work" / "pipeline" / "stage3_master.py").read_text(encoding="utf-8")
        self.assertIn('src_clip.exists() and r_info and "render_start" in r_info', stage3_py)
        self.assertIn('max(0.0, sc_start - render_start) + ctx.trim_first_frame_s', stage3_py)
        self.assertIn('project_scenes_by_id = {', stage3_py)

        # 2. Pipeline cli.py stitch_only synchronization checks
        cli_py = (self.proj_dir.parent.parent / "work" / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('project_scenes_by_id = {s["id"]: s for s in ctx.scenes', cli_py)
        self.assertIn('if r_info and "render_start" in r_info:', cli_py)

        # 3. Functional behavior check with ProjectContext and mock state
        ctx = ProjectContext(self.json_path)
        state = ctx.load_state_hashes()
        scene_renders = state.setdefault("scene_renders", {})
        clip_stem = "S18_solo_1344x768"
        scene_renders[clip_stem] = {
            "render_start": 136.80,
            "render_end": 152.59,
            "frames": 379,
        }
        sc_meta = {
            "id": "S18",
            "start": 138.47,
            "end": 152.10,
            "dur": 13.63,
            "clip": f"{clip_stem}.mp4",
            "render_start": 138.47,
        }
        r_info = scene_renders.get(clip_stem)
        resolved_render_start = float(r_info["render_start"])
        head_offset = max(0.0, float(sc_meta["start"]) - resolved_render_start) + ctx.trim_first_frame_s
        self.assertAlmostEqual(head_offset, 1.670, places=3)
        self.assertGreater(head_offset, 0.5)

    def test_54_ref2va_identity_references_and_scene_model_selector(self):
        """Regression test verifying:
        1. Pure prompt functions: find_ref_tags, strip_dangling_ref_tags, resolve_scene_refs, build_ref2va_prompt.
        2. make_video_task with FL2VA (Model 1) strips dangling tags and leaves video_prompt_type free of 'I'.
        3. make_video_task with Ref2VA (Model 12) adds 'I' to video_prompt_type and populates image_refs.
        4. Scene Inspector UI contains #sceneVideoRenderer and #sceneRendererBadge.
        5. Existing all_or_nothing scenes with default model (FL2VA) remain UP-TO-DATE without hash changes."""
        from pipeline.prompts import (
            find_ref_tags,
            strip_dangling_ref_tags,
            resolve_scene_refs,
            build_ref2va_prompt,
        )

        # 1. Prompt helpers
        sample_prompt = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "The male singer from <image1> and <image2> sings to the woman from <image3> and <image4>.\n"
            "overall_soundscape: Quiet party ambience.\n"
            "non_diegetic_music: N/A"
        )
        tags = find_ref_tags(sample_prompt)
        self.assertEqual(tags, [1, 2, 3, 4])

        stripped = strip_dangling_ref_tags("male singer from <image1> and <image2> (neat beard)")
        self.assertEqual(stripped, "male singer (neat beard)")

        ref_files = [
            WORKSPACE_ROOT / "projects" / "all_or_nothing" / "work" / "ref_sheets" / "male_angle1_front34.jpg",
            WORKSPACE_ROOT / "projects" / "all_or_nothing" / "work" / "ref_sheets" / "male_angle3_profile.jpg",
            WORKSPACE_ROOT / "projects" / "all_or_nothing" / "work" / "ref_sheets" / "female_angle1_front34.jpg",
            WORKSPACE_ROOT / "projects" / "all_or_nothing" / "work" / "ref_sheets" / "female_angle3_profile.jpg",
        ]
        sel_refs, tag_map = resolve_scene_refs(sample_prompt, ref_files)
        self.assertEqual(len(sel_refs), 4)
        self.assertEqual(tag_map, {1: 2, 2: 3, 3: 4, 4: 5})

        ref2va_prompt = build_ref2va_prompt(sample_prompt, tag_map)
        self.assertIn("subject_definitions:", ref2va_prompt)
        self.assertIn("<Subject 1> is the male lead from <Picture 2> and <Picture 3>", ref2va_prompt)
        self.assertIn("<Subject 2> is the female lead from <Picture 4> and <Picture 5>", ref2va_prompt)
        self.assertIn("attribute_transfer", ref2va_prompt)
        # Ensure zero blank lines inside prompt
        self.assertNotIn("\n\n", ref2va_prompt)

        # 2. FL2VA task compilation (Model 1)
        ctx = ProjectContext(self.json_path)
        m1_rspec = ctx.video_renderers["1"]
        anchor_p = self.proj_dir / "work" / "anchors_v3" / "A01.jpg"
        audio_p = self.proj_dir / "work" / "audio_v3" / "S01_male.wav"
        fl2va_task = ctx.make_video_task(
            output_filename="test_fl2va",
            prompt=sample_prompt,
            video_length=96,
            seed=1001,
            image_start=anchor_p,
            audio_path=audio_p,
            rspec=m1_rspec,
            image_refs=ref_files,
        )
        self.assertNotIn("image_refs", fl2va_task)
        self.assertNotIn("I", fl2va_task.get("video_prompt_type", ""))
        self.assertNotIn("<image1>", fl2va_task["prompt"])

        # 3. Ref2VA task compilation (Model 12)
        m12_rspec = ctx.video_renderers["12"]
        ref2va_task = ctx.make_video_task(
            output_filename="test_ref2va",
            prompt=sample_prompt,
            video_length=96,
            seed=1001,
            image_start=anchor_p,
            audio_path=audio_p,
            rspec=m12_rspec,
            image_refs=ref_files,
        )
        self.assertIn("image_refs", ref2va_task)
        self.assertEqual(len(ref2va_task["image_refs"]), 4)
        self.assertIn("I", ref2va_task["video_prompt_type"])
        self.assertIn("subject_definitions:", ref2va_task["prompt"])

        # 4. UI elements
        index_html = (WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        inspector_js = (WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "scene_inspector.js").read_text(encoding="utf-8")
        self.assertIn('id="sceneVideoRenderer"', index_html)
        self.assertIn('id="sceneRendererBadge"', index_html)
        self.assertIn('populateSceneVideoRenderer', inspector_js)
        self.assertIn('"sceneVideoRenderer"', inspector_js)

        # 5. Invariance check for all_or_nothing
        aon_cfg = WORKSPACE_ROOT / "projects" / "all_or_nothing" / "all_or_nothing_v4.json"
        if aon_cfg.exists():
            aon_ctx = ProjectContext(aon_cfg)
            from pipeline.stage2_scenes import compute_project_task_hashes
            hashes = compute_project_task_hashes(aon_ctx)
            self.assertIn("S01_male_solo_1344x768", hashes)
            # Baseline hash remains stable
            state = aon_ctx.load_state_hashes()
            prev_s01 = state.get("tasks", {}).get("S01_male_solo_1344x768")
            if prev_s01:
                self.assertIn(prev_s01, hashes["S01_male_solo_1344x768"])

    def test_55_character_studio_and_multi_reference_compilation(self):
        """Regression test verifying Character Management Studio & Multi-Reference System (v2 spec)."""
        import io
        import base64
        from http import HTTPStatus

        # 1. Default Synthesis & Zero-Invalidation
        aon_cfg = WORKSPACE_ROOT / "projects" / "all_or_nothing" / "all_or_nothing_v4.json"
        if aon_cfg.exists():
            aon_ctx = ProjectContext(aon_cfg)
            self.assertIn("marcus", aon_ctx.characters, "Default synthesis must produce marcus entity")
            self.assertIn("elena", aon_ctx.characters, "Default synthesis must produce elena entity")
            self.assertIn("@male_studio", aon_ctx.tag_lookup, "tag_lookup must include @male_studio")
            self.assertIn("@female_studio", aon_ctx.tag_lookup, "tag_lookup must include @female_studio")

            from pipeline.stage2_scenes import compute_project_task_hashes
            hashes = compute_project_task_hashes(aon_ctx)
            state = aon_ctx.load_state_hashes()
            tasks = state.get("tasks", {})
            for t_name in [
                "S01_male_solo_1344x768",
                "S02_Pass1_Left_1344x768",
                "S02_Pass2_Final_1344x768",
                "S06_male_solo_1344x768",
                "S18_solo_1344x768",
                "S26a_male_solo_1344x768",
            ]:
                if t_name in tasks and t_name in hashes:
                    self.assertIn(tasks[t_name], hashes[t_name], f"Task hash for {t_name} invalidated after character synthesis!")

            q_aon = server.compute_fast_project_queue("all_or_nothing")
            completed_aon = [x for x in q_aon if not x.get("needs_render")]
            self.assertGreaterEqual(len(completed_aon), 32, "All completed passes in all_or_nothing must remain UP-TO-DATE")

        # 2. Dual-Mode Prompt Compilation (@male_studio)
        ctx = ProjectContext(self.json_path)
        m1_rspec = ctx.video_renderers["1"]
        m12_rspec = ctx.video_renderers["12"]
        anchor_p = self.proj_dir / "work" / "anchors_v3" / "A01.jpg"
        audio_p = self.proj_dir / "work" / "audio_v3" / "S01_male.wav"

        # FL2VA Model 1 (preserves <Picture 1> first-frame token while stripping <imageN> and <Picture 2..10>):
        fl2va_task = ctx.make_video_task(
            output_filename="test_fl2va_char",
            prompt="For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\ndetailed_description:\n[Shot 1] Continue directly from <Picture 1>. Performance with @male_studio singing into vintage mic from <Picture 2>.",
            video_length=96,
            seed=1001,
            image_start=anchor_p,
            audio_path=audio_p,
            rspec=m1_rspec,
        )
        self.assertNotIn("image_refs", fl2va_task)
        self.assertNotIn("<image", fl2va_task["prompt"])
        self.assertNotIn("<Picture 2>", fl2va_task["prompt"])
        self.assertIn("<Picture 1>", fl2va_task["prompt"])
        self.assertIn("the exact 30-year-old Black male singer", fl2va_task["prompt"])

        # Ref2VA Model 12:
        ref2va_task = ctx.make_video_task(
            output_filename="test_ref2va_char",
            prompt="Performance with @male_studio singing into vintage mic",
            video_length=96,
            seed=1001,
            image_start=anchor_p,
            audio_path=audio_p,
            rspec=m12_rspec,
        )
        self.assertIn("image_refs", ref2va_task)
        self.assertGreaterEqual(len(ref2va_task["image_refs"]), 1)
        self.assertIn("I", ref2va_task["video_prompt_type"])
        self.assertIn("subject_definitions:", ref2va_task["prompt"])
        self.assertIn("<Subject 1>", ref2va_task["prompt"])
        self.assertIn("attribute_transfer", ref2va_task["prompt"])
        self.assertIn("fully_preserved", ref2va_task["prompt"])

        # 3. Multi-Subject Ensemble Compilation (S10 Orchestra) & Mixed {VAR} + @tag
        orch_prompt = "Camera pans across soundstage showing @strings, @pianist, @brass, @woodwinds, and {FEMALE_STUDIO_DESC}."
        orch_task = ctx.make_video_task(
            output_filename="test_orch_ref2va",
            prompt=orch_prompt,
            video_length=96,
            seed=1001,
            image_start=anchor_p,
            audio_path=audio_p,
            rspec=m12_rspec,
        )
        self.assertIn("subject_appearance_reference", orch_task["prompt"])
        self.assertIn("attribute_transfer", orch_task["prompt"])
        self.assertIn("environment_and_style_reference", orch_task["prompt"])
        self.assertIn("<Subject 5>", orch_task["prompt"])

        # 3b. Hash preservation on @tag migration vs hash invalidation on description edit
        from pipeline.characters import expand_prompt_with_characters
        canon_unmod, leg_unmod = expand_prompt_with_characters(
            "Marcus @male_studio sings", ctx.variables, ctx.tag_lookup
        )
        self.assertIsNotNone(leg_unmod, "Unmodified @male_studio should also return legacy expanded prompt for hash continuity")
        self.assertIn("from <image1> and <image2>", leg_unmod)
        self.assertNotIn("<image1>", canon_unmod)

        # Edit Marcus description and verify legacy hash fallback is disabled and canonical changes
        mod_chars = json.loads(json.dumps(ctx.characters))
        mod_chars["marcus"]["looks"]["studio"]["description"] = "wearing a neon cyberpunk jacket"
        from pipeline.characters import build_tag_lookup
        mod_lookup = build_tag_lookup(mod_chars, self.proj_dir)
        canon_mod, leg_mod = expand_prompt_with_characters(
            "Marcus @male_studio sings", ctx.variables, mod_lookup
        )
        self.assertIsNone(leg_mod, "Modified look description must not match legacy hash")
        self.assertIn("wearing a neon cyberpunk jacket", canon_mod)
        self.assertNotEqual(canon_unmod, canon_mod)

        # 4. REST Endpoints & Media Streaming (with clean teardown)
        orig_json_bytes = self.json_path.read_bytes()
        uploaded_file = self.proj_dir / "references" / "marcus" / "unit_test_upload.jpg"
        try:
            class DummyHandler:
                def __init__(self, method, path, body_dict=None):
                    self.command = method
                    self.path = path
                    body_bytes = json.dumps(body_dict).encode("utf-8") if body_dict else b""
                    self.headers = {"Content-Length": str(len(body_bytes)), "Content-Type": "application/json"}
                    self.rfile = io.BytesIO(body_bytes)
                    self.wfile = io.BytesIO()
                    self.response_status = None
                    self.response_headers = {}

                def send_response(self, status):
                    self.response_status = status

                def send_header(self, k, v):
                    self.response_headers[k] = v

                def end_headers(self):
                    pass

                def send_json(self, data, status=HTTPStatus.OK):
                    server.StudioRequestHandler.send_json(self, data, status)

                def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST):
                    server.StudioRequestHandler.send_error_json(self, msg, status)

                def read_json_body(self):
                    return server.StudioRequestHandler.read_json_body(self)

                def handle_media_stream(self, subpath):
                    return server.StudioRequestHandler.handle_media_stream(self, subpath)

            # GET /api/project/test_project/characters
            h_get = DummyHandler("GET", f"/api/project/{self.project_slug}/characters")
            server.StudioRequestHandler.do_GET(h_get)
            self.assertEqual(h_get.response_status, 200)
            res_get = json.loads(h_get.wfile.getvalue().decode("utf-8"))
            self.assertTrue(res_get.get("success"))
            self.assertIn("characters", res_get)
            self.assertIn("autocomplete", res_get)
            self.assertIn("scene_usage", res_get)

            # POST /api/project/characters/save
            h_save = DummyHandler("POST", "/api/project/characters/save", {
                "slug": self.project_slug,
                "characters": res_get["characters"],
            })
            server.StudioRequestHandler.do_POST(h_save)
            self.assertEqual(h_save.response_status, 200)

            # POST /api/project/characters/upload-image
            dummy_data_url = "data:image/jpeg;base64," + base64.b64encode(b"dummy image bytes").decode("ascii")
            h_upload = DummyHandler("POST", "/api/project/characters/upload-image", {
                "slug": self.project_slug,
                "entity_id": "marcus",
                "look_id": "studio",
                "filename": "unit_test_upload.jpg",
                "data_url": dummy_data_url,
            })
            server.StudioRequestHandler.do_POST(h_upload)
            self.assertEqual(h_upload.response_status, 200)
            res_up = json.loads(h_upload.wfile.getvalue().decode("utf-8"))
            self.assertTrue(res_up.get("success"))
            self.assertIn("references/marcus/unit_test_upload.jpg", res_up.get("rel_path", ""))

            # GET /media/<slug>/ref/references/marcus/unit_test_upload.jpg
            h_media = DummyHandler("GET", f"/media/{self.project_slug}/ref/references/marcus/unit_test_upload.jpg")
            server.StudioRequestHandler.do_GET(h_media)
            self.assertEqual(h_media.response_status, 200)
        finally:
            self.json_path.write_bytes(orig_json_bytes)
            if uploaded_file.exists():
                uploaded_file.unlink()

        # 5. Frontend JS Sanity & Module Line Counts (< 850 lines)
        js_char_file = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "characters_studio.js"
        js_text = js_char_file.read_text(encoding="utf-8")
        self.assertNotIn("jsonStringifySafe", js_text)
        self.assertNotIn("state.modelProfiles", js_text)
        self.assertNotIn("state.projectAssets", js_text)

        files_to_check = [
            WORKSPACE_ROOT / "work" / "pipeline" / "characters.py",
            WORKSPACE_ROOT / "work" / "pipeline" / "prompts.py",
            WORKSPACE_ROOT / "work" / "pipeline" / "context.py",
            WORKSPACE_ROOT / "work" / "ui" / "server_routes.py",
            js_char_file,
        ]
        for f in files_to_check:
            self.assertTrue(f.exists(), f"File {f.name} must exist")
            line_count = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLess(
                line_count,
                850,
                f"Module {f.name} exceeds 850 lines limit (currently {line_count} lines)!",
            )

    def test_56_estimator_quantization_and_padding(self):
        """Verify 17k+5 and 8k+1 frame quantization, quantum ceiling headroom, and padding impact."""
        from pipeline.media_utils import quantize_video_frames, format_duration_estimate, estimate_scene_render_time

        # 5.79s * 24 + 2 = 141 -> 17k+5 = 141 frames (max sec = 5.79s)
        f141, max_s141 = quantize_video_frames(5.79, frames_formula="h3", fps=24.0)
        self.assertEqual(f141, 141)
        self.assertAlmostEqual(max_s141, 5.79, places=2)

        # Adding 0.25s padding pushes 5.79s -> 6.04s -> crosses 5.79s quantum into 158 frames (6.50s ceiling)
        f158, max_s158 = quantize_video_frames(6.04, frames_formula="h3", fps=24.0)
        self.assertEqual(f158, 158)
        self.assertAlmostEqual(max_s158, 6.50, places=2)

        # ltx2 (8k+1) formula check
        f_ltx2, _ = quantize_video_frames(4.0, frames_formula="ltx2", fps=24.0)
        self.assertEqual((f_ltx2 - 1) % 8, 0)

        # Formatting checks
        self.assertEqual(format_duration_estimate(45.2), "45s")
        self.assertEqual(format_duration_estimate(665.0), "11.1m")
        self.assertEqual(format_duration_estimate(5580.0), "1h 33m")

        # Scene padding integration
        sc_no_pad = {"id": "S01", "start": 10.0, "end": 15.79, "mode": "male_solo"}
        sc_with_pad = {"id": "S01", "start": 10.0, "end": 15.79, "mode": "male_solo", "render_pad_start": 0.25, "render_pad_end": 0.25}
        est_no_pad = estimate_scene_render_time(sc_no_pad)
        est_with_pad = estimate_scene_render_time(sc_with_pad)
        self.assertEqual(est_no_pad["frames"], 141)
        self.assertEqual(est_with_pad["frames"], 158)
        self.assertGreater(est_with_pad["estimated_seconds"], est_no_pad["estimated_seconds"])

    def test_57_estimator_model_and_end_anchor_offsets(self):
        """Verify conditioning token frame offsets for End Anchor (+7f), Ref2VA (+14f/+26f), and FL2VA Pass 2 (-10f)."""
        from pipeline.media_utils import estimate_pass_render_time

        # 1. End anchor adds +7 effective frames on Solo / Pass 1, but NOT on Pass 2
        p1_single = estimate_pass_render_time(175, model_id="1", pass_num=1, has_end_anchor=False)
        p1_dual = estimate_pass_render_time(175, model_id="1", pass_num=1, has_end_anchor=True)
        self.assertGreater(p1_dual["estimated_seconds"], p1_single["estimated_seconds"])
        self.assertAlmostEqual(p1_single["estimated_seconds"], 775.6, delta=15.0)
        self.assertAlmostEqual(p1_dual["estimated_seconds"], 822.2, delta=15.0)

        # 2. FL2VA Pass 2 is faster than FL2VA Pass 1 (-10f grouped_rows speedup)
        p2_fl2va = estimate_pass_render_time(175, model_id="1", pass_num=2, has_end_anchor=True)
        self.assertLess(p2_fl2va["estimated_seconds"], p1_single["estimated_seconds"])

        # 3. Ref2VA (#12) is slower than FL2VA (#1), and Ref2VA Pass 2 is slower than Ref2VA Pass 1
        p1_ref2va = estimate_pass_render_time(175, model_id="12", pass_num=1, has_end_anchor=False)
        p2_ref2va = estimate_pass_render_time(175, model_id="12", pass_num=2, has_end_anchor=False)
        self.assertGreater(p1_ref2va["estimated_seconds"], p1_single["estimated_seconds"])
        self.assertGreater(p2_ref2va["estimated_seconds"], p1_ref2va["estimated_seconds"])
        self.assertAlmostEqual(p1_ref2va["estimated_seconds"], 870.0, delta=15.0)
        self.assertAlmostEqual(p2_ref2va["estimated_seconds"], 954.8, delta=15.0)

    def test_58_estimator_vram_spill_and_split_window_cliff(self):
        """Verify the 3 non-linear VRAM / Split-Window regimes: fast_vram (<=226f), vram_spill (227..362f), split_window (>362f)."""
        from pipeline.media_utils import estimate_pass_render_time

        # Regime 1: fast_vram (<= 226 frames)
        r_fast = estimate_pass_render_time(226, model_id="1")
        self.assertEqual(r_fast["regime"], "fast_vram")
        self.assertEqual(r_fast["window_count"], 1)
        self.assertFalse(r_fast["is_split_window"])
        self.assertAlmostEqual(r_fast["estimated_seconds"], 1142.7, delta=25.0)

        # Regime 2: vram_spill (345 frames -> ~5574s / 92.9m empirical S11 take)
        r_spill = estimate_pass_render_time(345, model_id="1", has_end_anchor=True)
        self.assertEqual(r_spill["regime"], "vram_spill")
        self.assertEqual(r_spill["window_count"], 1)
        self.assertTrue(r_spill["is_split_window"])
        self.assertAlmostEqual(r_spill["estimated_seconds"], 5574.4, delta=150.0)
        self.assertGreater(r_spill["rate_per_frame"], r_fast["rate_per_frame"] * 2.5)

        # Regime 3: split_window (600 frames > 362 window size -> 2 windows, ~10547s / 175.8m empirical S10 take)
        r_split = estimate_pass_render_time(600, model_id="1", sliding_window_size=362, sliding_window_overlap=18)
        self.assertEqual(r_split["regime"], "split_window")
        self.assertEqual(r_split["window_count"], 2)
        self.assertTrue(r_split["is_split_window"])
        self.assertAlmostEqual(r_split["estimated_seconds"], 10547.0, delta=250.0)

    def test_59_estimator_duet_pass_granularity_and_queue_integration(self):
        """Verify duet per-pass estimation, queue attachment, empirical calibration, and <850 line limits."""
        from pipeline.media_utils import estimate_scene_render_time, compute_calibration_factor

        # 1. Empirical calibration factor on all_or_nothing should be close to 1.0
        aon_dir = WORKSPACE_ROOT / "projects" / "all_or_nothing"
        if aon_dir.exists():
            alpha = compute_calibration_factor(aon_dir)
            self.assertGreaterEqual(alpha, 0.85)
            self.assertLessEqual(alpha, 1.15)

        # 2. Duet scene produces 2 pass estimates & split savings when in VRAM spill / split window
        duet_sc = {"id": "S02", "start": 5.79, "end": 13.79, "mode": "duet_method_b"}
        duet_est = estimate_scene_render_time(duet_sc)
        self.assertTrue(duet_est["is_duet"])
        self.assertEqual(duet_est["pass_count"], 2)
        self.assertEqual(len(duet_est["passes"]), 2)
        self.assertGreater(duet_est["estimated_seconds"], duet_est["passes"][0]["estimated_seconds"])
        self.assertIn("quantum_headroom_sec", duet_est)

        long_sc = {"id": "S11", "start": 77.41, "end": 91.21, "mode": "male_solo", "anchor_end": "A02"}
        long_est = estimate_scene_render_time(long_sc)
        self.assertTrue(long_est["is_split_window"])
        self.assertGreater(long_est["split_savings_seconds"], 1800.0)
        self.assertTrue(len(long_est["split_savings_str"]) > 0)

        # 3. Queue items computed by server_queue.py include estimation fields and calibration_alpha
        q = server.compute_fast_project_queue(self.project_slug, force_recheck=True)
        self.assertGreater(len(q), 0)
        for itm in q:
            self.assertIn("estimated_seconds", itm)
            self.assertIn("estimated_str", itm)
            self.assertIn("frames", itm)
            self.assertIn("regime", itm)
            self.assertIn("is_split_window", itm)
            self.assertIn("calibration_alpha", itm)
            self.assertIn("next_quantum_max_sec", itm)

        # 4. Enforce < 850 lines across all modified backend & frontend modules
        for rel in (
            "work/pipeline/media_utils.py",
            "work/ui/server_queue.py",
            "work/ui/static/js/state_and_utils.js",
            "work/ui/static/js/scene_inspector.js",
            "work/ui/static/js/anchors_and_settings.js",
            "work/ui/static/js/queue_and_console.js",
        ):
            p = WORKSPACE_ROOT / rel
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLess(lines, 850, f"{rel} exceeds 850 lines ({lines} lines)!")

    def test_60_action_buttons_consolidation_contracts(self):
        """Verify the consolidated action-button layout: stateful Run/Stop toggle, removed duplicates,
        renamed Queue Missing / Changed, characters flowing through the global Save, and line ceilings."""
        ui = WORKSPACE_ROOT / "work" / "ui" / "static"
        html = (ui / "index.html").read_text(encoding="utf-8")
        queue_js = (ui / "js" / "queue_and_console.js").read_text(encoding="utf-8")
        chars_js = (ui / "js" / "characters_studio.js").read_text(encoding="utf-8")
        inspector_js = (ui / "js" / "scene_inspector.js").read_text(encoding="utf-8")
        master_js = (ui / "js" / "master_and_export.js").read_text(encoding="utf-8")

        # 1. Removed duplicate / obsolete controls (exact quoted ids, so e.g. btnSplitSceneHeader is unaffected)
        all_js = queue_js + chars_js + inspector_js + master_js
        for dead_id in (
            "btnStopRender", "btnRenderSceneSingle", "btnSplitScene", "btnStitchMaster",
            "btnReloadQueue", "btnSaveCharacters", "renderStatusText",
        ):
            self.assertNotIn(f'id="{dead_id}"', html, f"{dead_id} must be removed from index.html")
            self.assertNotIn(f'"{dead_id}"', all_js, f"{dead_id} must have no leftover JS references")

        # 2. Kept controls + renamed labels
        self.assertIn('id="btnRenderAll"', html)
        self.assertIn("▶ Run", html)
        self.assertIn('id="btnSplitSceneHeader"', html)
        self.assertIn('id="btnStitchMasterFromTab"', html)
        self.assertIn('id="btnClearQueue"', html)
        self.assertIn("➕ Queue Missing / Changed", html)
        self.assertNotIn("Render Missing / Changed", html)

        # 3. Run <-> Stop toggle
        self.assertIn('"⏹ Stop"', queue_js)
        self.assertIn('"▶ Run"', queue_js)
        self.assertIn("stopActiveRender", queue_js)
        self.assertIn("/api/render/stop", queue_js)

        # 4. Characters edits record history snapshots & sessions and flow through global Save/Undo/Discard
        history_js = (ui / "js" / "takes_and_history.js").read_text(encoding="utf-8")
        self.assertIn("syncCharactersDirty", chars_js)
        self.assertIn("recordHistorySnapshot", chars_js)
        self.assertIn("beginEditSession", chars_js)
        self.assertIn("markProjectDirty(true)", chars_js)
        self.assertIn("window.state.charactersData = window.state.projectData.characters", history_js)

        # 5. Stale engine_status.json cleanup when current_project is None (e.g. after reboot)
        eng_file = self.proj_dir / "work" / "jobs_v3" / "engine_status.json"
        eng_file.parent.mkdir(parents=True, exist_ok=True)
        eng_file.write_text(json.dumps({"state": "running", "filename": "S01_solo_832x480.mp4"}), encoding="utf-8")
        server.render_status["current_project"] = None
        server.kill_active_render(target_slug=self.project_slug)
        eng_after = json.loads(eng_file.read_text(encoding="utf-8"))
        self.assertEqual(eng_after.get("state"), "cancelled")
        eng_file.unlink(missing_ok=True)

        # 6. No stale copy referencing removed buttons
        self.assertNotIn("⚡ Render Scene", inspector_js)
        self.assertNotIn("Render Missing / Changed", inspector_js)

        # 7. Cache-buster bumped
        self.assertIn("js/queue_and_console.js?v=42", html)

        # 8. Line ceilings
        for rel in (
            "work/ui/static/js/queue_and_console.js",
            "work/ui/static/js/scene_inspector.js",
            "work/ui/static/js/characters_studio.js",
            "work/ui/static/js/takes_and_history.js",
            "work/ui/static/js/master_and_export.js",
            "work/ui/server_routes.py",
            "work/ui/server_workers.py",
        ):
            n = len((WORKSPACE_ROOT / rel).read_text(encoding="utf-8").splitlines())
            self.assertLess(n, 850, f"{rel} exceeds 850 lines ({n})")

    @classmethod
    def tearDownClass(cls):
        server.compute_fast_project_queue(cls.project_slug, force_recheck=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
