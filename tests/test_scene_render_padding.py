"""
Tests for Scene-Level Render Padding & Overlap (render_pad_start / render_pad_end)
Verifies:
1. Pure padding resolution hierarchy and explicit zero override
2. Pre-roll clamping at song start 0.0s
3. Task hashes differentiate custom padding and preserve legacy hashes
4. Coverage check and fast queue detect padding expansion
5. Marker prompt resolves relative to padded render start
6. slice_audio invalidates cache when start shifts with same duration
7. Scene split preserves outer padding and clears inner cut padding
8. Takes rollback clears stale padding
9. CLI project_tool set-scene and audit padding validations
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from pipeline.context import ProjectContext, resolve_scene_padding
from pipeline.prompts import resolve_marker_prompt
from pipeline.scene_ops import split_scene_in_project_data
from pipeline.stage2_scenes import compute_project_task_hashes, check_scene_render_coverage
from pipeline.takes import promote_take_to_active
import server_queue


class TestSceneRenderPadding(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="mvg_test_padding_"))
        self.project_dir = self.test_dir / "projects" / "test_proj"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.anchors_dir = self.project_dir / "anchors"
        self.anchors_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.project_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.renders_dir = self.project_dir / "work" / "renders_v3"
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir = self.project_dir / "work" / "jobs_v3"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        # Anchors
        (self.anchors_dir / "A01 - Singer_2K.jpg").write_bytes(b"A01_DUMMY_BYTES_123")
        (self.anchors_dir / "A02 - Pianist_2K.jpg").write_bytes(b"A02_DUMMY_BYTES_456")

        # Audio
        self.master_wav = self.audio_dir / "master.wav"
        self.master_wav.write_bytes(b"RIFF" + b"\x00" * 200)

        self.project_json = self.project_dir / "test_proj.json"
        self.base_project_data = {
            "project": {
                "name": "Test Project",
                "slug": "test_proj",
                "fps": 24,
                "render_overlap_seconds": 1.0,
                "master_audio": "audio/master.wav",
                "directories": {
                    "anchors": "anchors",
                    "audio_slices": "audio",
                    "renders": "work/renders_v3",
                },
            },
            "models": {"default_video_renderer": 1, "default_anchor_renderer": 1},
            "video_shared": {
                "negative_prompt": "ugly, blurry",
                "image_prompt_type": "S",
            },
            "anchors": [
                {"id": "A01", "filename": "A01 - Singer_2K.jpg"},
                {"id": "A02", "filename": "A02 - Pianist_2K.jpg"},
            ],
            "scenes": [
                {
                    "id": "S01",
                    "start": 10.0,
                    "end": 15.0,
                    "mode": "solo",
                    "anchor": "A01 - Singer_2K.jpg",
                    "renderer": 1,
                    "seed": 1001,
                    "prompt": "Singer at microphone",
                    "stem": "master",
                }
            ],
        }
        self.project_json.write_text(json.dumps(self.base_project_data, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Padding resolution hierarchy and explicit zero override
    # -------------------------------------------------------------------------
    def test_01_padding_resolution_hierarchy_and_explicit_zero(self):
        """Hierarchy: scene explicit -> render_overlap -> project default (explicit 0.0 overrides default)."""
        # Inherits project default (1.0s) when omitted
        h, t = resolve_scene_padding({"start": 5.0, "end": 10.0}, proj_head=1.0, proj_tail=1.0)
        self.assertEqual((h, t), (1.0, 1.0))

        # Scene override takes precedence
        sc = {"start": 5.0, "end": 10.0, "render_pad_start": 1.5, "render_pad_end": 0.8}
        self.assertEqual(resolve_scene_padding(sc, proj_head=1.0, proj_tail=1.0), (1.5, 0.8))

        # Explicit 0.0 MUST override project default
        sc_zero = {"start": 5.0, "end": 10.0, "render_pad_start": 0.0, "render_pad_end": 0.0}
        self.assertEqual(resolve_scene_padding(sc_zero, proj_head=1.0, proj_tail=1.0), (0.0, 0.0))

        # Aliases
        sc_alias1 = {"start": 5.0, "end": 10.0, "pad_start": 1.2, "pad_end": 0.6}
        self.assertEqual(resolve_scene_padding(sc_alias1), (1.2, 0.6))
        sc_alias2 = {"start": 5.0, "end": 10.0, "head_overlap": 1.1, "tail_overlap": 0.4}
        self.assertEqual(resolve_scene_padding(sc_alias2), (1.1, 0.4))

        # Shorthand render_overlap
        sc_ov_dict = {"start": 5.0, "end": 10.0, "render_overlap": {"head": 1.4, "tail": 0.7}}
        self.assertEqual(resolve_scene_padding(sc_ov_dict), (1.4, 0.7))
        sc_ov_float = {"start": 5.0, "end": 10.0, "render_overlap": 0.9}
        self.assertEqual(resolve_scene_padding(sc_ov_float), (0.9, 0.9))

        # End Anchor tail default (0.0s) unless explicit
        sc_end_anchor = {"start": 5.0, "end": 10.0, "anchor_end": "A02"}
        self.assertEqual(resolve_scene_padding(sc_end_anchor, proj_head=1.0, proj_tail=1.0), (1.0, 0.0))
        sc_end_explicit = {"start": 5.0, "end": 10.0, "anchor_end": "A02", "render_pad_end": 0.5}
        self.assertEqual(resolve_scene_padding(sc_end_explicit, proj_head=1.0, proj_tail=1.0), (1.0, 0.5))

        # String numbers and empty strings
        sc_str = {"start": " 5.0 ", "end": " 10.0 ", "render_pad_start": " 1.5 ", "render_pad_end": ""}
        self.assertEqual(resolve_scene_padding(sc_str, proj_head=0.0, proj_tail=0.7), (1.5, 0.7))

        # ProjectContext method
        ctx = ProjectContext(self.project_json)
        self.assertEqual(ctx.get_scene_render_padding(sc), (1.5, 0.8))

    # -------------------------------------------------------------------------
    # 2. Pre-roll clamping at song start 0.0s
    # -------------------------------------------------------------------------
    def test_02_preroll_clamped_at_song_start_zero(self):
        """Pre-roll padding must never attempt to render before 0.0s audio start."""
        sc = {"start": 0.4, "end": 5.0, "render_pad_start": 1.5, "render_pad_end": 0.5}
        h, t = resolve_scene_padding(sc, proj_head=0.0, proj_tail=0.0)
        self.assertEqual(h, 0.4)
        self.assertEqual(t, 0.5)

        sc_zero_start = {"start": 0.0, "end": 5.0, "render_pad_start": 2.0}
        h0, _ = resolve_scene_padding(sc_zero_start)
        self.assertEqual(h0, 0.0)

    # -------------------------------------------------------------------------
    # 3. Task hashes differentiate custom padding and preserve legacy hashes
    # -------------------------------------------------------------------------
    def test_03_task_hashes_differentiate_custom_padding_and_preserve_legacy(self):
        """Unmodified scenes preserve legacy hashes; custom padding alters hash and prevents collisions."""
        # Unmodified scene (using default)
        ctx = ProjectContext(self.project_json)
        hashes_base = compute_project_task_hashes(ctx)
        out_name = list(hashes_base.keys())[0]
        h_base = hashes_base[out_name]

        # Add custom padding
        pdata = copy.deepcopy(self.base_project_data)
        pdata["scenes"][0]["render_pad_start"] = 1.5
        self.project_json.write_text(json.dumps(pdata, indent=2), encoding="utf-8")
        ctx_pad1 = ProjectContext(self.project_json)
        hashes_pad1 = compute_project_task_hashes(ctx_pad1)
        h_pad1 = hashes_pad1[out_name]

        self.assertNotEqual(h_base, h_pad1, "Custom padding must change task hash!")

        # Shrink padding from 1.5 to 0.5
        pdata["scenes"][0]["render_pad_start"] = 0.5
        self.project_json.write_text(json.dumps(pdata, indent=2), encoding="utf-8")
        ctx_pad2 = ProjectContext(self.project_json)
        hashes_pad2 = compute_project_task_hashes(ctx_pad2)
        h_pad2 = hashes_pad2[out_name]

        self.assertNotEqual(h_pad1, h_pad2, "Shrinking padding must change task hash!")
        self.assertNotEqual(h_base, h_pad2)

        # Duet mode Pass 1 and Pass 2
        pdata_duet = copy.deepcopy(self.base_project_data)
        pdata_duet["scenes"][0]["mode"] = "duet_method_b"
        pdata_duet["scenes"][0]["render_pad_start"] = 1.2
        self.project_json.write_text(json.dumps(pdata_duet, indent=2), encoding="utf-8")
        ctx_duet = ProjectContext(self.project_json)
        hashes_duet = compute_project_task_hashes(ctx_duet)
        self.assertTrue(any("Pass1" in k for k in hashes_duet))
        self.assertTrue(any("Pass2" in k for k in hashes_duet))

    # -------------------------------------------------------------------------
    # 4. Coverage and fast queue detect padding expansion
    # -------------------------------------------------------------------------
    def test_04_coverage_and_fast_queue_detect_padding_expansion(self):
        """When padding expands beyond rendered boundaries, fast queue and coverage check flag TIMELINE CHANGED."""
        ctx = ProjectContext(self.project_json)
        out_name = "S01_Singer_at_microphone_960x544"
        mp4_path = self.renders_dir / f"{out_name}.mp4"
        mp4_path.write_bytes(b"0" * 60000)

        # Mock rendered covering [9.0, 16.0] (matching default 1.0s overlap on 10.0-15.0)
        scene_renders = {
            out_name: {
                "render_start": 9.0,
                "render_end": 16.0,
            }
        }

        # Check coverage with default padding: req_start=9.0, req_end=16.0 -> COVERED
        covered, r_start, r_end = check_scene_render_coverage(
            ctx.ffmpeg, mp4_path, scene_renders, out_name, 9.0, 16.0
        )
        self.assertTrue(covered)

        # Expand padding: req_start = 8.0 -> NOT COVERED
        covered_expanded, _, _ = check_scene_render_coverage(
            ctx.ffmpeg, mp4_path, scene_renders, out_name, 8.0, 16.0
        )
        self.assertFalse(covered_expanded)

    # -------------------------------------------------------------------------
    # 5. Marker prompt resolves relative to padded render start
    # -------------------------------------------------------------------------
    def test_05_marker_prompt_resolves_relative_to_padded_render_start(self):
        """Relative cue marker offset in prompt must align with padded clip start."""
        sc = {"start": 10.0, "end": 15.0, "render_pad_start": 1.5}
        h_pad, _ = resolve_scene_padding(sc)
        req_start = max(0.0, sc["start"] - h_pad)  # 8.5s
        markers = [{"name": "drop", "time": 11.0}]

        # At 11.0s global time, relative to 8.5s is 2.50s
        resolved = resolve_marker_prompt("Beat drops at <drop>", req_start, markers, fps=24.0)
        self.assertIn("2.50s", resolved)

    # -------------------------------------------------------------------------
    # 6. slice_audio invalidates cache when start shifts with same duration
    # -------------------------------------------------------------------------
    def test_06_slice_audio_invalidates_cache_when_start_shifts_with_same_duration(self):
        """slice_audio sidecar must track start_s:dur_s and re-slice if start changes."""
        ctx = ProjectContext(self.project_json)
        out_wav = self.audio_dir / "test_slice.wav"
        meta_file = out_wav.with_suffix(".slice")

        def fake_ffmpeg(*args, **kwargs):
            out_wav.write_bytes(b"\x00" * 705644)

        with patch("subprocess.run", side_effect=fake_ffmpeg) as mock_run:
            # First slice (file does not exist initially)
            ctx.slice_audio(self.master_wav, 5.0, 4.0, out_wav)
            self.assertEqual(mock_run.call_count, 1)
            self.assertTrue(meta_file.exists())
            self.assertEqual(meta_file.read_text(encoding="utf-8").strip(), "5.000:4.000")

            # Same window -> cache hit (no subprocess.run)
            ctx.slice_audio(self.master_wav, 5.0, 4.0, out_wav)
            self.assertEqual(mock_run.call_count, 1)

            # Shift start by +0.5s with same duration (4.0s) -> invalidates cache and runs subprocess
            ctx.slice_audio(self.master_wav, 5.5, 4.0, out_wav)
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(meta_file.read_text(encoding="utf-8").strip(), "5.500:4.000")

    # -------------------------------------------------------------------------
    # 7. Scene split preserves outer padding and clears inner cut padding
    # -------------------------------------------------------------------------
    def test_07_split_scene_preserves_outer_padding_and_clears_inner_cut_padding(self):
        """Splitting a scene preserves head padding on part1 and tail padding on part2."""
        data = copy.deepcopy(self.base_project_data)
        data["scenes"][0]["render_pad_start"] = 1.2
        data["scenes"][0]["render_pad_end"] = 0.8

        updated_data, sc1, sc2 = split_scene_in_project_data(data, "S01", 12.5)

        # Part 1 preserves start padding, has no end padding
        self.assertEqual(sc1.get("render_pad_start"), 1.2)
        self.assertNotIn("render_pad_end", sc1)

        # Part 2 preserves end padding, has no start padding
        self.assertEqual(sc2.get("render_pad_end"), 0.8)
        self.assertNotIn("render_pad_start", sc2)

        # Shorthand render_overlap conversion
        data_ov = copy.deepcopy(self.base_project_data)
        data_ov["scenes"][0]["render_overlap"] = {"head": 1.5, "tail": 0.5}
        _, ov1, ov2 = split_scene_in_project_data(data_ov, "S01", 12.5)
        self.assertEqual(ov1.get("render_pad_start"), 1.5)
        self.assertNotIn("render_overlap", ov1)
        self.assertNotIn("render_pad_end", ov1)
        self.assertEqual(ov2.get("render_pad_end"), 0.5)
        self.assertNotIn("render_overlap", ov2)
        self.assertNotIn("render_pad_start", ov2)

    # -------------------------------------------------------------------------
    # 8. Takes rollback clears stale padding
    # -------------------------------------------------------------------------
    def test_08_take_rollback_clears_stale_padding(self):
        """Restoring a take captured without custom padding pops active padding settings."""
        # Active project has padding
        pdata = copy.deepcopy(self.base_project_data)
        pdata["scenes"][0]["render_pad_start"] = 2.0
        pdata["scenes"][0]["render_pad_end"] = 1.0
        self.project_json.write_text(json.dumps(pdata, indent=2), encoding="utf-8")

        # Fake vault take captured without padding
        vault_dir = self.renders_dir / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault_mp4 = vault_dir / "take_001.mp4"
        vault_mp4.write_bytes(b"VAULT_VIDEO")

        manifest = {
            "version": 1,
            "scenes": {
                "S01": {
                    "takes": [
                        {
                            "take_id": "take_001",
                            "take_number": 1,
                            "out_name": "S01_Singer_at_microphone_960x544",
                            "task_hash": "dummy_hash_v1",
                            "vault_rel_mp4": "vault/take_001.mp4",
                            "scene_snapshot": {
                                "id": "S01",
                                "start": 10.0,
                                "end": 15.0,
                                "prompt": "Singer at microphone",
                                "seed": 1001,
                            },
                        }
                    ],
                    "active_take_id": None,
                }
            },
        }
        manifest_file = self.project_dir / "work" / "takes_manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        res = promote_take_to_active(self.project_dir, self.project_json, "S01", "take_001")
        self.assertTrue(res["success"])

        # Verify active scene in project.json has stale padding cleared
        reloaded = json.loads(self.project_json.read_text(encoding="utf-8"))
        sc_reloaded = reloaded["scenes"][0]
        self.assertNotIn("render_pad_start", sc_reloaded)
        self.assertNotIn("render_pad_end", sc_reloaded)

    # -------------------------------------------------------------------------
    # 9. CLI project_tool set-scene and audit padding validations
    # -------------------------------------------------------------------------
    def test_09_project_tool_set_and_audit_padding(self):
        """project_tool set-scene accepts --pad-start/--pad-end, and audit validates boundaries."""
        import importlib.util
        tool_path = WORKSPACE_ROOT / ".agents" / "skills" / "mvg-project-ops" / "scripts" / "project_tool.py"
        spec = importlib.util.spec_from_file_location("project_tool", str(tool_path))
        project_tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(project_tool)

        # Set padding via CLI helper
        args_set = MagicMock()
        args_set.slug = "test_proj"
        args_set.scene_id = "S01"
        args_set.start = None
        args_set.end = None
        args_set.mode = None
        args_set.prompt = None
        args_set.anchor = None
        args_set.anchor_end = None
        args_set.stem = None
        args_set.renderer = None
        args_set.pad_start = "1.5"
        args_set.pad_end = "0.7"

        with patch("sys.stdout") as mock_out:
            with patch.object(project_tool, "find_latest_project_json", return_value=(self.project_json, json.loads(self.project_json.read_text(encoding="utf-8")))):
                with patch.object(project_tool, "backup_file", return_value=None):
                    project_tool.cmd_set_scene(args_set)

        data_updated = json.loads(self.project_json.read_text(encoding="utf-8"))
        self.assertEqual(data_updated["scenes"][0]["render_pad_start"], 1.5)
        self.assertEqual(data_updated["scenes"][0]["render_pad_end"], 0.7)

        # Clear padding via CLI helper
        args_clear = copy.copy(args_set)
        args_clear.pad_start = "clear"
        args_clear.pad_end = "default"
        with patch.object(project_tool, "find_latest_project_json", return_value=(self.project_json, json.loads(self.project_json.read_text(encoding="utf-8")))):
            with patch.object(project_tool, "backup_file", return_value=None):
                project_tool.cmd_set_scene(args_clear)

        data_cleared = json.loads(self.project_json.read_text(encoding="utf-8"))
        self.assertNotIn("render_pad_start", data_cleared["scenes"][0])
        self.assertNotIn("render_pad_end", data_cleared["scenes"][0])

        # Test audit warnings
        args_audit = MagicMock()
        args_audit.slug = "test_proj"
        pdata_audit = copy.deepcopy(self.base_project_data)
        pdata_audit["scenes"][0]["start"] = 0.5
        pdata_audit["scenes"][0]["render_pad_start"] = 2.0  # exceeds start
        pdata_audit["scenes"][0]["anchor_end"] = "A02 - Pianist_2K.jpg"
        pdata_audit["scenes"][0]["render_pad_end"] = 1.0  # end anchor + pad_end
        self.project_json.write_text(json.dumps(pdata_audit, indent=2), encoding="utf-8")

        with patch("sys.stdout") as mock_out:
            with patch.object(project_tool, "find_latest_project_json", return_value=(self.project_json, pdata_audit)):
                with patch("sys.exit"):
                    project_tool.cmd_audit(args_audit)

    # -------------------------------------------------------------------------
    # 10. Master assembly trims pre-roll and post-roll handles cleanly
    # -------------------------------------------------------------------------
    def test_10_master_assembly_trims_padding_handles(self):
        """assemble_progressive_master computes head_offset_s and target_frames to trim padding."""
        from pipeline.stage3_master import assemble_progressive_master
        ctx = ProjectContext(self.project_json)
        out_name = "S01_Singer_at_microphone_960x544"
        mp4_path = self.renders_dir / f"{out_name}.mp4"
        mp4_path.write_bytes(b"0" * 60000)

        # Scene timeline [10.0, 15.0] (5.0s = 120 frames at 24fps)
        # Render covers [8.5, 16.0] (render_start=8.5, render_end=16.0)
        state = {
            "scene_renders": {
                out_name: {
                    "render_start": 8.5,
                    "render_end": 16.0,
                }
            }
        }
        ctx.save_state_hashes(state)

        scene_meta = [
            {
                "id": "S01",
                "start": 10.0,
                "end": 15.0,
                "dur": 5.0,
                "mode": "solo",
                "clip": f"{out_name}.mp4",
                "render_start": 8.5,
                "render_end": 16.0,
                "anchor": "A01 - Singer_2K.jpg",
            }
        ]

        commands_run = []
        def fake_ffmpeg(cmd, *args, **kwargs):
            commands_run.append([str(c) for c in cmd])
            dst = Path(cmd[-1])
            if dst.suffix == ".mp4":
                dst.write_bytes(b"0" * 60000)

        with patch("subprocess.run", side_effect=fake_ffmpeg):
            with patch("pipeline.stage3_master.probe_video_fps", return_value=24.0):
                with patch("pipeline.stage3_master.probe_video_dimensions", return_value=(960, 544)):
                    with patch("pipeline.stage3_master.probe_video_frames", return_value=120):
                        assemble_progressive_master(ctx, scene_meta)

        trim_cmds = [cmd for cmd in commands_run if "-vf" in cmd]
        self.assertTrue(trim_cmds, "Expected ffmpeg -vf trim command to run")
        vf_arg = trim_cmds[0][trim_cmds[0].index("-vf") + 1]
        expected_trim = (10.0 - 8.5) + ctx.trim_first_frame_s
        self.assertIn(f"trim=start={expected_trim:.3f}", vf_arg, "Head offset plus trim_first_frame_s must be trimmed")
        self.assertIn("setpts=PTS-STARTPTS", vf_arg)
        frames_idx = trim_cmds[0].index("-frames:v")
        self.assertEqual(trim_cmds[0][frames_idx + 1], "120", "Target frames must match timeline cut duration (5s * 24fps)")

    def test_11_hold_last_frame_tail_pad_coverage(self):
        """Scenes with hold_last_frame=True allow physical renders that end early (fade to black) without flagging TIMELINE CHANGED."""
        ctx = ProjectContext(self.project_json)
        out_name = "S01_solo_960x544"
        mp4_path = self.renders_dir / f"{out_name}.mp4"
        mp4_path.write_bytes(b"0" * 60000)

        # Physical render covers [10.0, 13.5] while timeline spans [10.0, 15.0]
        scene_renders = {out_name: {"render_start": 10.0, "render_end": 13.5}}

        cov_no_pad, _, _ = check_scene_render_coverage(ctx.ffmpeg, mp4_path, scene_renders, out_name, 10.0, 15.0, allow_tail_pad=False)
        self.assertFalse(cov_no_pad, "Without allow_tail_pad, shorter render must not cover timeline")

        cov_with_pad, _, _ = check_scene_render_coverage(ctx.ffmpeg, mp4_path, scene_renders, out_name, 10.0, 15.0, allow_tail_pad=True)
        self.assertTrue(cov_with_pad, "With allow_tail_pad=True, shorter render must be accepted for tail-padding")


if __name__ == "__main__":
    unittest.main()
