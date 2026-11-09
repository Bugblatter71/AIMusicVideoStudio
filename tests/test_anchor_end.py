import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "work"))

from pipeline.context import ProjectContext
from pipeline.scene_ops import split_scene_in_project_data
from pipeline.stage2_scenes import compute_project_task_hashes


class TestAnchorEndSupport(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="mvg_test_anchor_end_"))
        self.project_dir = self.test_dir / "projects" / "test_proj"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.anchors_dir = self.project_dir / "anchors"
        self.anchors_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.project_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        # Create dummy anchor files
        self.anchor_a01 = self.anchors_dir / "A01 - Singer_2K.jpg"
        self.anchor_a01.write_bytes(b"DUMMY_A01_BYTES_123456789")
        self.anchor_a02 = self.anchors_dir / "A02 - Pianist_2K.jpg"
        self.anchor_a02.write_bytes(b"DUMMY_A02_BYTES_987654321")

        # Dummy audio
        (self.audio_dir / "master.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        self.project_json = self.project_dir / "test_proj.json"
        self.base_project_data = {
            "project": {
                "name": "Test Project",
                "slug": "test_proj",
                "fps": 24,
                "master_audio": "audio/master.wav",
                "directories": {
                    "anchors": "anchors",
                    "audio_slices": "audio",
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
                    "start": 0.0,
                    "end": 8.0,
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

    def test_make_video_task_without_anchor_end(self):
        """Verifies default single-anchor video task creation (image_prompt_type='S')."""
        ctx = ProjectContext(self.project_json)
        _, rspec = ctx.resolve_video_renderer(self.base_project_data["scenes"][0])
        task = ctx.make_video_task(
            output_filename="test_clip",
            prompt="Camera pans left",
            video_length=107,
            seed=1001,
            image_start=self.anchor_a01,
            audio_path=self.audio_dir / "master.wav",
            rspec=rspec,
        )
        self.assertEqual(task["image_prompt_type"], "S")
        self.assertEqual(task["image_start"], str(self.anchor_a01))
        self.assertNotIn("image_end", task)

    def test_make_video_task_with_anchor_end(self):
        """Verifies two-anchor video task creation sets image_prompt_type='SE' and task['image_end']."""
        ctx = ProjectContext(self.project_json)
        _, rspec = ctx.resolve_video_renderer(self.base_project_data["scenes"][0])
        task = ctx.make_video_task(
            output_filename="test_clip",
            prompt="Camera pans from <Picture 1> to <Picture 2>",
            video_length=107,
            seed=1001,
            image_start=self.anchor_a01,
            audio_path=self.audio_dir / "master.wav",
            rspec=rspec,
            image_end=self.anchor_a02,
        )
        self.assertEqual(task["image_prompt_type"], "SE")
        self.assertEqual(task["image_start"], str(self.anchor_a01))
        self.assertEqual(task["image_end"], str(self.anchor_a02))

    def test_task_hashes_differentiate_anchor_end(self):
        """Task hash computation changes when anchor_end is added or modified."""
        ctx_without = ProjectContext(self.project_json)
        hashes_without = compute_project_task_hashes(ctx_without)

        # Now add anchor_end to scene S01
        data_with = copy.deepcopy(self.base_project_data)
        data_with["scenes"][0]["anchor_end"] = "A02 - Pianist_2K.jpg"
        self.project_json.write_text(json.dumps(data_with, indent=2), encoding="utf-8")

        ctx_with = ProjectContext(self.project_json)
        hashes_with = compute_project_task_hashes(ctx_with)

        out_name = list(hashes_without.keys())[0]
        hash_without = hashes_without[out_name][0]
        hash_with = hashes_with[out_name][0]

        self.assertNotEqual(hash_without, hash_with)

    def test_split_scene_preserves_anchor_end_on_tail(self):
        """Splitting a scene that has anchor_end clears it on part 1 and keeps it on part 2."""
        data = copy.deepcopy(self.base_project_data)
        data["scenes"][0]["anchor_end"] = "A02 - Pianist_2K.jpg"

        updated, sc1, sc2 = split_scene_in_project_data(data, "S01", 4.0)
        self.assertNotIn("anchor_end", sc1)
        self.assertEqual(sc2.get("anchor_end"), "A02 - Pianist_2K.jpg")
        self.assertEqual(sc1["end"], 4.0)
        self.assertEqual(sc2["start"], 4.0)

    def test_project_tool_set_scene_anchor_end(self):
        """project_tool.py set-scene updates and clears anchor_end property."""
        from types import SimpleNamespace
        import importlib.util
        
        pt_path = Path(__file__).resolve().parent.parent / ".agents" / "skills" / "mvg-project-ops" / "scripts" / "project_tool.py"
        spec = importlib.util.spec_from_file_location("project_tool", pt_path)
        pt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pt)

        # Set anchor_end via set-scene
        args = SimpleNamespace(
            slug="test_proj",
            scene_id="S01",
            start=None,
            end=None,
            mode=None,
            prompt=None,
            anchor=None,
            anchor_end="A02",
            stem=None,
            renderer=None,
        )
        with patch.object(pt, "PROJECTS_DIR", self.project_dir.parent), patch.object(pt, "BACKUP_DIR", self.test_dir / "backup"):
            pt.cmd_set_scene(args)
            saved = json.loads(self.project_json.read_text(encoding="utf-8"))
            self.assertEqual(saved["scenes"][0].get("anchor_end"), "A02")

            # Clear anchor_end
            args_clear = SimpleNamespace(
                slug="test_proj",
                scene_id="S01",
                start=None,
                end=None,
                mode=None,
                prompt=None,
                anchor=None,
                anchor_end="clear",
                stem=None,
                renderer=None,
            )
            pt.cmd_set_scene(args_clear)
            saved_cleared = json.loads(self.project_json.read_text(encoding="utf-8"))
            self.assertNotIn("anchor_end", saved_cleared["scenes"][0])

    def test_character_prompt_preserves_picture_2_when_has_end(self):
        """On FL2VA (supports_refs=False), @character expansion preserves <Picture 2> when has_end=True."""
        ctx = ProjectContext(self.project_json)
        ctx.tag_lookup = {
            "@singer": {
                "entity_id": "singer",
                "look_id": "default",
                "text_expansion": "a male vocalist in a dark suit",
            }
        }
        _, rspec = ctx.resolve_video_renderer(self.base_project_data["scenes"][0])
        prompt = "At 0.00s <Picture 1> is referenced. @singer walks across stage to <Picture 2> (from <Picture 3>)."
        task = ctx.make_video_task(
            output_filename="test_char_end",
            prompt=prompt,
            video_length=107,
            seed=1001,
            image_start=self.anchor_a01,
            audio_path=self.audio_dir / "master.wav",
            rspec=rspec,
            image_end=self.anchor_a02,
        )
        self.assertIn("a male vocalist in a dark suit", task["prompt"])
        self.assertIn("<Picture 1>", task["prompt"])
        self.assertIn("<Picture 2>", task["prompt"])
        self.assertNotIn("<Picture 3>", task["prompt"])

    def test_ref2va_and_pass2_gva_ignore_image_end(self):
        """Ref2VA models and Pass 2 GVA tasks do not set image_prompt_type='SE' or image_end."""
        ctx = ProjectContext(self.project_json)
        ref2va_rspec = {
            "model_type": "minimax_h3_ref2va_pruned_pdd",
            "frames_formula": "h3_ref2va",
            "supports_image_refs": True,
            "image_prompt_type": "S",
        }
        task_ref = ctx.make_video_task(
            output_filename="test_ref2va",
            prompt="Camera pans from <Picture 1>",
            video_length=105,
            seed=1001,
            image_start=self.anchor_a01,
            audio_path=self.audio_dir / "master.wav",
            rspec=ref2va_rspec,
            image_end=self.anchor_a02,
        )
        self.assertEqual(task_ref["image_prompt_type"], "S")
        self.assertNotIn("image_end", task_ref)

        # Pass 2 GVA with video_guide and video_mask
        _, fl2va_rspec = ctx.resolve_video_renderer(self.base_project_data["scenes"][0])
        dummy_guide = self.project_dir / "guide.mp4"
        dummy_mask = self.project_dir / "mask.mp4"
        dummy_guide.write_bytes(b"0" * 100)
        dummy_mask.write_bytes(b"0" * 100)
        task_p2 = ctx.make_video_task(
            output_filename="test_p2",
            prompt="Pass 2 prompt",
            video_length=107,
            seed=1001,
            image_start=self.anchor_a01,
            audio_path=self.audio_dir / "master.wav",
            rspec=fl2va_rspec,
            video_guide=dummy_guide,
            video_mask=dummy_mask,
            image_end=self.anchor_a02,
        )
        self.assertEqual(task_p2["image_prompt_type"], "S")
        self.assertNotIn("image_end", task_p2)

    def test_two_pass_duet_pass1_hashes_anchor_end(self):
        """Two-pass duet Pass 1 task hash changes when anchor_end is added, cascading to Pass 2."""
        duet_data = copy.deepcopy(self.base_project_data)
        duet_data["scenes"][0]["mode"] = "duet_method_b"
        duet_data["scenes"][0]["prompt_m"] = "Male sings on left"
        duet_data["scenes"][0]["prompt_f"] = "Female sings on right"
        self.project_json.write_text(json.dumps(duet_data, indent=2), encoding="utf-8")
        hashes_without = compute_project_task_hashes(ProjectContext(self.project_json))

        duet_data["scenes"][0]["anchor_end"] = "A02 - Pianist_2K.jpg"
        self.project_json.write_text(json.dumps(duet_data, indent=2), encoding="utf-8")
        hashes_with = compute_project_task_hashes(ProjectContext(self.project_json))

        p1_key = next(k for k in hashes_without if "Pass1" in k)
        p2_key = next(k for k in hashes_without if "Pass2" in k)
        self.assertNotEqual(hashes_without[p1_key][0], hashes_with[p1_key][0])
        self.assertNotEqual(hashes_without[p2_key][0], hashes_with[p2_key][0])

    def test_take_rollback_clears_stale_anchor_end(self):
        """Rolling back to a take recorded without anchor_end removes any later-added anchor_end."""
        from pipeline.takes import promote_take_to_active, save_takes_manifest

        renders_dir = self.project_dir / "work" / "renders_v3"
        vault_dir = renders_dir / "takes" / "S01"
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault_mp4 = vault_dir / "take_001.mp4"
        vault_mp4.write_bytes(b"X" * 60000)

        # Snapshot in take_001 has NO anchor_end
        manifest = {
            "version": 1,
            "scenes": {
                "S01": {
                    "active_take_id": "take_001",
                    "takes": [
                        {
                            "take_id": "take_001",
                            "take_number": 1,
                            "out_name": "S01_solo_1344x768",
                            "task_hash": "abc12345",
                            "vault_rel_mp4": "takes/S01/take_001.mp4",
                            "scene_snapshot": copy.deepcopy(self.base_project_data["scenes"][0]),
                        }
                    ],
                }
            },
        }
        save_takes_manifest(self.project_dir, manifest)

        # User later adds anchor_end to project.json
        data_with_end = copy.deepcopy(self.base_project_data)
        data_with_end["scenes"][0]["anchor_end"] = "A02 - Pianist_2K.jpg"
        self.project_json.write_text(json.dumps(data_with_end, indent=2), encoding="utf-8")

        # Promote take_001 back to active
        res = promote_take_to_active(self.project_dir, self.project_json, "S01", "take_001")
        self.assertNotIn("anchor_end", res)
        reloaded = json.loads(self.project_json.read_text(encoding="utf-8"))
        self.assertNotIn("anchor_end", reloaded["scenes"][0])


if __name__ == "__main__":
    unittest.main()
