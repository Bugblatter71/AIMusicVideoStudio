"""Offline unit & integration tests for Plan 1: LTX-2.5 MSR (#19) & Ingredients Ref Sheet (#20, #21)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_ROOT / "work"
SKILLS_QUEUE_SCRIPTS = WORKSPACE_ROOT / ".agents" / "skills" / "mvg-queue-and-wan2gp" / "scripts"
for p in (WORK_DIR, WORK_DIR / "ui", SKILLS_QUEUE_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from pipeline.context import ProjectContext
from pipeline.fal_catalog import is_ref2va_spec, resolve_fal_route, resolve_task_backend
from pipeline.ltx_refs import compose_ingredients_sheet, get_video_ref_mode, snap_ltx2_resolution
from pipeline.media_utils import _is_ref2va_model, estimate_scene_render_time, probe_video_dimensions, resolve_ffmpeg_path
from pipeline.stage2_hashes import compute_project_task_hashes
import server
import validate_wgp_profile


def _write_wav(path: Path, dur_s: float = 4.0, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x01\x00" * int(dur_s * sr))


def _make_color_jpg(ffmpeg: Path, path: Path, color: str, size: str = "320x240") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:d=0.1", "-vframes", "1", "-q:v", "2", str(path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_dummy_mp4(ffmpeg: Path, path: Path, dur_s: float = 4.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"color=c=navy:s=320x180:d={dur_s}:r=24", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if path.stat().st_size <= 55000:
        with open(path, "ab") as fh:
            fh.write(b"\x00" * (60000 - path.stat().st_size))


class TestLtx25MultiReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        cls.profiles = json.loads((WORKSPACE_ROOT / "model_profiles.json").read_text(encoding="utf-8"))

    def _build_proj(self, root: Path, slug: str = "ltx_ref_proj") -> tuple[Path, Path]:
        pdir = root / slug
        for sub in ("audio", "references/maya", "references/liam", "work/anchors", "work/renders", "work/jobs"):
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        (pdir / "local_config.json").write_text("{}", encoding="utf-8")

        _write_wav(pdir / "audio" / "master.wav", 6.0)
        _write_wav(pdir / "audio" / "female.wav", 6.0)
        _make_color_jpg(self.ffmpeg, pdir / "work" / "anchors" / "A01_stage.jpg", "navy", "640x360")
        _make_color_jpg(self.ffmpeg, pdir / "work" / "anchors" / "A02_end.jpg", "teal", "640x360")
        _make_color_jpg(self.ffmpeg, pdir / "references" / "maya" / "front.jpg", "crimson", "240x320")
        _make_color_jpg(self.ffmpeg, pdir / "references" / "liam" / "front.jpg", "forestgreen", "240x320")
        _make_color_jpg(self.ffmpeg, pdir / "references" / "prop_guitar.jpg", "gold", "320x200")
        _make_color_jpg(self.ffmpeg, pdir / "references" / "prop_hat.jpg", "purple", "200x200")

        cfg = {
            "project": {
                "name": "LTX Ref Test",
                "fps": 24,
                "master_audio": "audio/master.wav",
                "stems": {"female": "audio/female.wav"},
                "render_overlap_seconds": 0.0,
                "directories": {"anchors": "work/anchors", "renders": "work/renders", "jobs": "work/jobs"},
            },
            "models": {
                "default_anchor_renderer": 3,
                "default_video_renderer": 14,
                "default_video_resolution": 4,
                "video_resolutions": self.profiles["video_resolutions"],
                "video_renderers": self.profiles["video_renderers"],
                "video_shared_settings": {"resolution": "1344x768", "negative_prompt": "blurry"},
            },
            "characters": {
                "maya": {
                    "id": "maya", "name": "Maya", "kind": "character", "default_look": "stage",
                    "physical_traits": "red-haired singer",
                    "looks": {"stage": {"tag": "@maya_stage", "description": "sequined jacket", "image_refs": ["references/maya/front.jpg"]}},
                },
                "liam": {
                    "id": "liam", "name": "Liam", "kind": "character", "default_look": "stage",
                    "physical_traits": "tall guitarist",
                    "looks": {"stage": {"tag": "@liam_stage", "description": "denim vest", "image_refs": ["references/liam/front.jpg"]}},
                },
            },
            "anchors": [
                {"id": "A01", "filename": "A01_stage.jpg", "seed": 11, "prompt": "Stage"},
                {"id": "A02", "filename": "A02_end.jpg", "seed": 12, "prompt": "End"},
            ],
            "scenes": [
                {"id": "S01", "start": 0.0, "end": 4.0, "mode": "solo", "stem": "female", "anchor": "A01_stage.jpg", "renderer": 19, "prompt": "@maya_stage and @liam_stage perform together on stage.", "seed": 101},
                {"id": "S02", "start": 0.0, "end": 4.0, "mode": "solo", "stem": "female", "anchor": "A01_stage.jpg", "renderer": 20, "prompt": "@maya_stage holds her vintage guitar.", "image_refs": ["references/prop_guitar.jpg", "references/prop_hat.jpg"], "seed": 202},
                {"id": "S03", "start": 0.0, "end": 4.0, "mode": "solo", "stem": "female", "anchor": "A01_stage.jpg", "renderer": 14, "prompt": "@maya_stage sings.", "seed": 303},
            ],
        }
        cfg_path = pdir / f"{slug}.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return pdir, cfg_path

    def test_01_profile_schema_and_classification(self):
        errs, _ = validate_wgp_profile.validate_profiles(WORKSPACE_ROOT / "model_profiles.json")
        self.assertEqual(errs, [], f"model_profiles validation errors: {errs}")

        vr = self.profiles["video_renderers"]
        for rid, mtype, rmode in (("19", "ltx2_25_22B_msr", "ltx2_msr"), ("20", "ltx2_25_22B_distilled", "ltx2_ingredients"), ("21", "ltx2_25_22B", "ltx2_ingredients")):
            self.assertIn(rid, vr)
            spec = vr[rid]
            self.assertEqual(spec["model_type"], mtype)
            self.assertEqual(spec["ref_mode"], rmode)
            self.assertTrue(spec["local_only"])
            self.assertTrue(spec["supports_image_refs"])
            self.assertFalse(is_ref2va_spec(spec), f"#{rid} must NOT trigger H3 Ref2VA mode")
            self.assertEqual(get_video_ref_mode(spec), rmode)
            self.assertFalse(_is_ref2va_model(rid, mtype))

        self.assertTrue(is_ref2va_spec(vr["9"]))
        self.assertEqual(get_video_ref_mode(vr["9"]), "h3_ref2va")
        self.assertEqual(snap_ltx2_resolution("1280x720"), "1280x704")
        self.assertEqual(snap_ltx2_resolution("1344x768"), "1344x768")

    def test_02_msr_task_contract_and_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir, cfg_path = self._build_proj(Path(tmp))
            ctx = ProjectContext(cfg_path)
            ctx.raw["models"]["default_backend"] = "fal"  # Must still resolve to local because local_only=True
            spec19 = ctx.video_renderers["19"]
            a01 = pdir / "work" / "anchors" / "A01_stage.jpg"
            a02 = pdir / "work" / "anchors" / "A02_end.jpg"
            wav = pdir / "audio" / "female.wav"

            # 1. Start Anchor (BG) + 2 @character subjects -> KI mode, no image_start, natural prose prompt
            task = ctx.make_video_task(
                "S01_solo_1344x768", "@maya_stage and @liam_stage perform together.", 97, 101,
                a01, wav, spec19, sc=ctx.scenes[0], resolution="1280x720",
            )
            self.assertEqual(task["_backend"], "local")
            self.assertEqual(resolve_task_backend(ctx, sc=dict(ctx.scenes[0], backend="fal"), rspec=spec19), "local")
            self.assertEqual(task["video_prompt_type"], "KI")
            self.assertEqual(task["image_prompt_type"], "T")
            self.assertNotIn("image_start", task)
            self.assertEqual(task["resolution"], "1280x704")
            self.assertEqual(task["remove_background_images_ref"], 1)
            self.assertEqual(task["audio_prompt_type"], "AS")
            self.assertEqual(len(task["image_refs"]), 3)
            self.assertEqual(Path(task["image_refs"][0]).name, "A01_stage.jpg")
            self.assertNotIn("subject_definitions:", task["prompt"])
            self.assertNotIn("<Picture 2>", task["prompt"])
            self.assertIn("red-haired singer", task["prompt"])

            # 2. With End Anchor -> TE
            task_te = ctx.make_video_task(
                "S01_te", "@maya_stage turns toward the light.", 97, 102,
                a01, wav, spec19, image_end=a02, sc=ctx.scenes[0],
            )
            self.assertEqual(task_te["image_prompt_type"], "TE")
            self.assertEqual(task_te["image_end"], str(a02))

            # 3. Subjects-only I mode + remove_bg=False + silent mode
            sc_i = {"id": "S99", "mode": "silent_locked", "stem": "silent", "msr_use_start_as_bg": False, "msr_remove_bg": False}
            task_i = ctx.make_video_task(
                "S99_silent", "@maya_stage stands calmly.", 97, 103,
                a01, ctx.ensure_silent_wav(4.0, 97), spec19, sc=sc_i,
            )
            self.assertEqual(task_i["video_prompt_type"], "I")
            self.assertEqual(task_i["remove_background_images_ref"], 0)
            self.assertEqual(task_i["audio_prompt_type"], "")
            self.assertEqual(task_i["audio_guide"], "")
            self.assertEqual([Path(p).name for p in task_i["image_refs"]], ["front.jpg"])

            # 4. Pass 2 forbidden -> ValueError
            with self.assertRaises(ValueError):
                ctx.make_video_task("S_p2", "Pass 2", 97, 1, a01, wav, spec19, video_guide=a01, video_mask=a02)

            # 5. Missing refs and disabled start anchor -> ValueError
            with self.assertRaises(ValueError):
                ctx.make_video_task("S_empty", "Nobody here", 97, 1, Path(""), wav, spec19, sc={"id": "SX", "image_refs": []})

            route_note = resolve_fal_route(spec19, sc=ctx.scenes[0])
            self.assertTrue(any("local-only" in n.lower() for n in route_note["notes"]))

    def test_03_ingredients_sheet_composition_and_pass2_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir, cfg_path = self._build_proj(Path(tmp))
            ctx = ProjectContext(cfg_path)
            spec20 = ctx.video_renderers["20"]
            a01 = pdir / "work" / "anchors" / "A01_stage.jpg"
            a02 = pdir / "work" / "anchors" / "A02_end.jpg"
            wav = pdir / "audio" / "female.wav"
            guitar = pdir / "references" / "prop_guitar.jpg"

            # 1. Single ref -> passed directly without compositing
            t1 = ctx.make_video_task("S_one", "@maya_stage holds prop.", 97, 11, a01, wav, spec20, sc={"id": "S1", "mode": "solo"})
            self.assertEqual(t1["video_prompt_type"], "I")
            self.assertEqual(t1["image_prompt_type"], "S")
            self.assertEqual(t1["image_start"], str(a01))
            self.assertEqual(t1["audio_prompt_type"], "AS")
            self.assertEqual(t1["remove_background_images_ref"], 0)
            self.assertEqual(len(t1["image_refs"]), 1)
            self.assertTrue(t1["image_refs"][0].endswith("front.jpg"))

            # 2. Three refs (2 sc.image_refs + 1 @maya_stage) -> cached composite grid sheet JPG
            t3 = ctx.make_video_task("S_three", ctx.scenes[1]["prompt"], 97, 12, a01, wav, spec20, sc=ctx.scenes[1])
            self.assertEqual(t3["video_prompt_type"], "I")
            self.assertEqual(len(t3["image_refs"]), 1)
            sheet_p = Path(t3["image_refs"][0])
            self.assertTrue(sheet_p.is_file())
            self.assertIn("ingredients_cache", str(sheet_p))
            self.assertGreater(sheet_p.stat().st_size, 1000)
            self.assertEqual(probe_video_dimensions(self.ffmpeg, sheet_p), (1536, 640))

            # Verify top-left corner white & center vertical separator dark via raw RGB inspect
            raw_proc = subprocess.run(
                [str(self.ffmpeg), "-v", "quiet", "-i", str(sheet_p), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, check=True,
            )
            rgb = raw_proc.stdout
            w, h = 1536, 640
            self.assertEqual(len(rgb), w * h * 3)
            # Top-left pixel near white background padding around portrait cell
            tl = (rgb[0], rgb[1], rgb[2])
            self.assertGreater(min(tl), 235, f"Expected white sheet corner, got {tl}")
            # Vertical separator bar sits around x=510 (for 3 cols in 1536px with 4px sep)
            mid_y = h // 2
            sep_samples = [rgb[(mid_y * w + x) * 3 : (mid_y * w + x) * 3 + 3] for x in range(508, 514)]
            darkest = min(sum(px) for px in sep_samples)
            self.assertLess(darkest, 60, f"Expected dark separator bar around x=510, got samples={sep_samples}")

            # Cache hit verification (same path without recomposition)
            mtime0 = sheet_p.stat().st_mtime_ns
            t3_again = compose_ingredients_sheet(
                [guitar, pdir / "references" / "prop_hat.jpg", pdir / "references" / "maya" / "front.jpg"],
                cache_dir=ctx.jobs_dir / "ingredients_cache",
                ffmpeg_bin=self.ffmpeg,
            )
            self.assertEqual(t3_again, sheet_p)
            self.assertEqual(sheet_p.stat().st_mtime_ns, mtime0)

            # 3. Zero refs -> standard I2V/A2V
            t0 = ctx.make_video_task("S_zero", "Pure landscape shot.", 97, 13, a01, wav, spec20, sc={"id": "S0", "image_refs": []})
            self.assertEqual(t0["video_prompt_type"], "")
            self.assertNotIn("image_refs", t0)

            # 4. Pass 2 -> Ingredients disabled, stays MVGA
            t_p2 = ctx.make_video_task(
                "S_p2", ctx.scenes[1]["prompt"], 97, 14, a01, wav, spec20,
                video_guide=a01, video_mask=a02, sc=ctx.scenes[1], pass_num=2,
            )
            self.assertEqual(t_p2["video_prompt_type"], "MVGA")
            self.assertNotIn("image_refs", t_p2)
            self.assertEqual(t_p2["remove_background_images_ref"], 0)

    def test_04_hash_invalidation_queue_staleness_and_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir, cfg_path = self._build_proj(Path(tmp), slug="hash_proj")
            ctx = ProjectContext(cfg_path)
            h1 = compute_project_task_hashes(ctx)

            # Baseline check on #14 unchanged when editing #19/#20 scenes
            s03_name = "S03_solo_1344x768"
            s01_name = "S01_solo_1344x768"
            s02_name = "S02_solo_1344x768"
            self.assertIn(s01_name, h1)
            self.assertIn(s02_name, h1)
            self.assertIn(s03_name, h1)

            # Mutate MSR toggle -> S01 hash changes, S03 unchanged
            ctx.scenes[0]["msr_remove_bg"] = False
            h2 = compute_project_task_hashes(ctx)
            self.assertNotEqual(h1[s01_name][0], h2[s01_name][0])
            self.assertEqual(h1[s03_name][0], h2[s03_name][0])

            # Mutate ref image file bytes -> S02 Ingredients hash changes
            hat = pdir / "references" / "prop_hat.jpg"
            _make_color_jpg(self.ffmpeg, hat, "cyan", "200x200")
            h3 = compute_project_task_hashes(ctx)
            self.assertNotEqual(h2[s02_name][0], h3[s02_name][0])

            # Queue staleness when ref file mtime is newer than rendered MP4
            renders = pdir / "work" / "renders"
            _make_dummy_mp4(self.ffmpeg, renders / f"{s02_name}.mp4")
            a01 = pdir / "work" / "anchors" / "A01_stage.jpg"
            guitar = pdir / "references" / "prop_guitar.jpg"
            maya_ref = pdir / "references" / "maya" / "front.jpg"
            old_t = time.time() - 120
            for p_old in (a01, guitar, maya_ref):
                os.utime(p_old, (old_t, old_t))
            os.utime(renders / f"{s02_name}.mp4", (old_t + 30, old_t + 30))
            os.utime(hat, (time.time() + 120, time.time() + 120))
            (pdir / "work" / "jobs" / "state_hashes.json").write_text(
                json.dumps({"tasks": {s02_name: h3[s02_name][0]}}), encoding="utf-8",
            )
            with mock.patch.object(server, "PROJECTS_DIR", Path(tmp)):
                q = server.compute_fast_project_queue("hash_proj", force_recheck=True)
            s02_q = next(it for it in q if it.get("scene_id") == "S02")
            self.assertEqual(s02_q["status"], "queued")
            self.assertIn("NEWER ON DISK", s02_q["reason"])
            self.assertIn("prop_hat.jpg", s02_q["reason"])

            # ETA check without ctx fallback
            est19 = estimate_scene_render_time(ctx.scenes[0], ctx=None)
            self.assertGreater(est19["estimated_seconds"], 0)
            self.assertEqual((est19["frames"] - 1) % 8, 0)


if __name__ == "__main__":
    unittest.main()
