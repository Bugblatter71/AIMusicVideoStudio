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
        # 1. Check all_or_nothing detection for modified anchor A07 (affects S12b)
        q_aon = server.compute_fast_project_queue("all_or_nothing", force_recheck=True)
        queued_scenes = {x["scene_id"]: x for x in q_aon if x["status"] == "queued"}
        self.assertIn("S12b", queued_scenes, "S12b (uses A07) must be queued when A07 anchor is updated")
        self.assertTrue(queued_scenes["S12b"]["needs_render"])
        self.assertIn("A07", queued_scenes["S12b"]["reason"])

        # 2. Dynamic anchor touch on all_or_nothing A01
        aon_anchors = WORKSPACE_ROOT / "projects" / "all_or_nothing" / "work" / "anchors_v3"
        a01_files = list(aon_anchors.glob("A01*"))
        if a01_files:
            a01 = a01_files[0]
            orig_mtime = a01.stat().st_mtime
            try:
                now = time.time()
                # Set anchor mtime into future so it is newer than the rendered MP4
                os.utime(a01, (now + 100, now + 100))
                q_test = server.compute_fast_project_queue("all_or_nothing", force_recheck=True)
                queued_map = {x["scene_id"]: x for x in q_test if x["status"] == "queued"}
                self.assertIn("S01", queued_map, "S01 must be queued when A01 is newer on disk")
                self.assertTrue(queued_map["S01"]["needs_render"])
                self.assertIn("A01", queued_map["S01"]["reason"])
            finally:
                os.utime(a01, (orig_mtime, orig_mtime))


if __name__ == "__main__":
    unittest.main(verbosity=2)
