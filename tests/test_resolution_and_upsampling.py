"""Unit tests for per-scene resolution overrides, project & late spatial upsampling,
OOM guards, and lossless Stage 3 1920x1088 -> 1920x1080 center-crop behavior.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from work.pipeline.context import ProjectContext
from work.pipeline.media_utils import probe_video_dimensions, resolve_ffmpeg_path
from work.pipeline.stage2_hashes import compute_project_task_hashes
from work.pipeline.stage3_master import assemble_progressive_master, build_stage3_scale_filters
from work.pipeline.upscale import (
    explain_spatial_upsampling_skip,
    handle_upscale_api,
    parse_upsampling_scale,
    resolve_effective_spatial_upsampling,
    resolve_scene_filters,
    upscale_scene_clip,
    validate_late_upsampling_method,
)
from work.ui import server as srv


class TestResolutionAndUpsampling(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mvg_res_up_"))
        self.proj_dir = self.tmp_dir / "demo_proj"
        for sub in ("audio", "work/anchors", "work/audio", "work/masks", "work/jobs", "work/renders", "work/trimmed", "work/frames"):
            (self.proj_dir / sub).mkdir(parents=True, exist_ok=True)
        # Copy minimal model_profiles.json or reuse repo one
        shutil.copy2(WORKSPACE_ROOT / "model_profiles.json", self.tmp_dir / "model_profiles.json")
        # Create valid dummy anchor images & silent wav using ffmpeg
        self.ffmpeg = Path(shutil.which("ffmpeg") or resolve_ffmpeg_path(self.proj_dir, "", ""))
        anchor_path = self.proj_dir / "work/anchors/A01_test.jpg"
        subprocess.run(
            [str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=0.1", "-frames:v", "1", str(anchor_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        audio_path = self.proj_dir / "audio/master.mp3"
        subprocess.run(
            [str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=12", str(audio_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        self.cfg_path = self.proj_dir / "demo_proj.json"
        self.cfg_data = {
            "project": {
                "name": "Demo Res Up",
                "project_dir": ".",
                "master_audio": "audio/master.mp3",
                "stems": {"male": "audio/master.mp3", "female": "audio/master.mp3"},
                "master_resolution": "1920x1080",
                "master_scale_algorithm": "lanczos",
                "master_sharpen": 0.25,
                "fps": 24,
                "directories": {
                    "anchors": "work/anchors",
                    "audio_slices": "work/audio",
                    "masks": "work/masks",
                    "jobs": "work/jobs",
                    "renders": "work/renders",
                    "trimmed": "work/trimmed",
                    "review_strips": "work/frames",
                },
            },
            "profiles_file": "../model_profiles.json",
            "models": {
                "default_anchor_renderer": 3,
                "default_video_renderer": 1,
                "default_video_resolution": 2,  # 960x544 default
                "video_shared_settings": {
                    "spatial_upsampling": "h3_vae*2",
                },
            },
            "anchors": [{"id": "A01", "filename": "A01_test.jpg", "prompt": "Singer"}],
            "scenes": [
                {
                    "id": "S01",
                    "start": 0.0,
                    "end": 5.0,
                    "mode": "male_solo",
                    "anchor": "A01_test.jpg",
                    "prompt": "Default 540p scene",
                    "seed": 101,
                },
                {
                    "id": "S02",
                    "start": 5.0,
                    "end": 10.0,
                    "mode": "male_solo",
                    "anchor": "A01_test.jpg",
                    "resolution": 6,  # Hero 1920x1088 override -> must auto-skip h3_vae*2!
                    "prompt": "Hero 1088p scene",
                    "seed": 102,
                },
                {
                    "id": "S03",
                    "start": 10.0,
                    "end": 15.0,
                    "mode": "duet_method_b",
                    "anchor": "A01_test.jpg",
                    "resolution": 3,  # 1280x720 override
                    "spatial_upsampling": "h3_vae*1.5",
                    "prompt_m": "Pass 1 left",
                    "prompt_f": "Pass 2 right",
                    "seed_m": 201,
                    "seed_f": 202,
                },
            ],
        }
        self.cfg_path.write_text(json.dumps(self.cfg_data, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_per_scene_resolution_and_upsampling_guards(self):
        ctx = ProjectContext(self.cfg_path)
        s1, s2, s3 = ctx.scenes

        self.assertEqual(ctx.resolve_scene_resolution(s1), "960x544")
        self.assertEqual(ctx.resolve_scene_resolution(s2), "1920x1088")
        self.assertEqual(ctx.resolve_scene_resolution(s3), "1280x720")

        # S01 (960x544): inherits h3_vae*2
        self.assertEqual(ctx.resolve_scene_spatial_upsampling(s1), "h3_vae*2")
        # S02 (1920x1088): auto-skips scale > 1.0 upscaler to prevent 4K OOM on 8GB GPU
        self.assertEqual(ctx.resolve_scene_spatial_upsampling(s2), "")
        # Unless 1x refiner (h3facerefine): allowed at 1920x1088
        self.assertEqual(resolve_effective_spatial_upsampling("h3facerefine", "1920x1088", "minimax_h3_fl2va"), "h3facerefine")
        # S03 two-pass duet: Pass 1 suppresses upsampling so video_guide matches mask; Pass 2 keeps h3_vae*1.5
        self.assertEqual(ctx.resolve_scene_spatial_upsampling(s3, res_str="1280x720", pass_num=1), "")
        self.assertEqual(ctx.resolve_scene_spatial_upsampling(s3, res_str="1280x720", pass_num=2), "h3_vae*1.5")

        # Non-H3 model automatically skips h3_vae*
        self.assertEqual(resolve_effective_spatial_upsampling("h3_vae*2", "960x544", "wan22_ti2v"), "")
        self.assertEqual(parse_upsampling_scale("h3_vae*1.5"), 1.5)

    def test_02_task_dict_and_hash_keys_follow_scene_overrides(self):
        ctx = ProjectContext(self.cfg_path)
        _, vrspec = ctx.resolve_video_renderer(ctx.scenes[1])
        t_s2 = ctx.make_video_task(
            "S02_male_solo_1920x1088", "Hero prompt", 124, 102,
            self.proj_dir / "work/anchors/A01_test.jpg",
            self.proj_dir / "audio/master.mp3",
            vrspec, sc=ctx.scenes[1],
        )
        self.assertEqual(t_s2["resolution"], "1920x1088")
        self.assertNotIn("spatial_upsampling", t_s2)

        hashes = compute_project_task_hashes(ctx)
        self.assertIn("S01_male_solo_960x544", hashes)
        self.assertIn("S02_male_solo_1920x1088", hashes)
        self.assertIn("S03_Pass1_Left_1280x720", hashes)
        self.assertIn("S03_Pass2_Final_1280x720", hashes)

    def test_03_stage3_center_crop_1920x1088_no_scale_no_unsharp(self):
        # 1920x1088 -> 1920x1080 must center-crop 4px top/bottom with NO scale and NO unsharp
        f_1088 = build_stage3_scale_filters("1920:1080", scale_algo="lanczos", sharpen=0.25, preserve_aspect=True, src_dims=(1920, 1088))
        self.assertEqual(f_1088, ["crop=1920:1080:0:4", "setsar=1:1"])

        # Exact 1920x1080 -> 1920x1080 must do NO scale, NO crop, NO unsharp
        f_1080 = build_stage3_scale_filters("1920:1080", scale_algo="lanczos", sharpen=0.25, preserve_aspect=True, src_dims=(1920, 1080))
        self.assertEqual(f_1080, ["setsar=1:1"])

        # Smaller 1280x720 -> 1920x1080 must scale + crop + apply unsharp
        f_720 = build_stage3_scale_filters("1920:1080", scale_algo="lanczos", sharpen=0.25, preserve_aspect=True, src_dims=(1280, 720))
        self.assertTrue(any(x.startswith("scale=1920:1080:") for x in f_720))
        self.assertTrue(any(x.startswith("unsharp=") for x in f_720))

        # Integration with FFmpeg on a real 1920x1088 clip -> 1920x1080
        ctx = ProjectContext(self.cfg_path)
        s2_mp4 = ctx.renders_dir / "S02_male_solo_1920x1088.mp4"
        subprocess.run(
            [str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1088:r=24:d=5", "-c:v", "libx264", "-preset", "ultrafast", str(s2_mp4)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ok = assemble_progressive_master(ctx, [
            {"id": "S02", "start": 0.0, "end": 5.0, "dur": 5.0, "mode": "male_solo", "clip": "S02_male_solo_1920x1088.mp4"}
        ])
        self.assertTrue(ok)
        trimmed = ctx.trimmed_dir / "00_S02_1920x1080.mp4"
        self.assertTrue(trimmed.is_file())
        self.assertEqual(probe_video_dimensions(ctx.ffmpeg, trimmed), (1920, 1080))

    def test_04_queue_resolves_per_scene_resolutions(self):
        with mock.patch.object(srv, "PROJECTS_DIR", self.tmp_dir):
            q = srv.compute_fast_project_queue("demo_proj", force_recheck=True)
        by_sid = {item["scene_id"]: item for item in q}
        self.assertEqual(by_sid["S01"]["resolution"], "960x544")
        self.assertEqual(by_sid["S02"]["resolution"], "1920x1088")
        self.assertEqual(by_sid["S03"]["resolution"], "1280x720")

    def test_05_late_upscale_take_dummy_and_validation(self):
        self.assertIsNotNone(validate_late_upsampling_method("h3_vae*2"))
        self.assertIsNone(validate_late_upsampling_method("flashvsrplus*2"))
        self.assertIsNone(validate_late_upsampling_method("seedvr2*2"))
        self.assertIsNone(validate_late_upsampling_method("h3facerefine"))

        ctx = ProjectContext(self.cfg_path)
        s1_mp4 = ctx.renders_dir / "S01_male_solo_960x544.mp4"
        subprocess.run(
            [str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=green:s=960x544:r=24:d=2", "-c:v", "libx264", "-preset", "ultrafast", str(s1_mp4)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        take = upscale_scene_clip(ctx, None, "S01", "flashvsrplus*2", dummy_render=True)
        self.assertEqual(take["output_resolution"], "1920x1088")
        self.assertEqual(take["spatial_upsampling"], "flashvsrplus*2")
        self.assertEqual(probe_video_dimensions(ctx.ffmpeg, s1_mp4), (1920, 1088))

        # API validation and dispatch check
        responses = []
        class _DummyHandler:
            def send_json(self, payload, status=200):
                responses.append(("ok", status, payload))
            def send_error_json(self, msg, status=400):
                responses.append(("err", status, msg))

        with mock.patch.object(srv, "PROJECTS_DIR", self.tmp_dir), \
             mock.patch.object(srv, "execute_single_render", return_value=0) as m_exec, \
             mock.patch.object(srv, "broadcast_log"):
            handle_upscale_api(_DummyHandler(), srv, {"project": "demo_proj", "scene_id": "S01", "method": "h3_vae*2"})
            self.assertEqual(responses[-1][0], "err")
            handle_upscale_api(_DummyHandler(), srv, {"project": "demo_proj", "scene_id": "S01", "method": "flashvsrplus*2"})
            self.assertEqual(responses[-1][0], "ok")
            self.assertTrue(responses[-1][2]["started"])
            # Wait briefly for daemon thread to invoke execute_single_render
            for _ in range(20):
                if m_exec.called:
                    break
                import time; time.sleep(0.02)
            self.assertTrue(m_exec.called)
            self.assertEqual(m_exec.call_args[0][0][1:5], ["upscale", "S01", "--method", "flashvsrplus*2"])

    def test_06_target_resolution_skip_guard(self):
        # 1280x720 source when target is 1280x720 must skip scale > 1.0 upscalers
        self.assertEqual(
            resolve_effective_spatial_upsampling("h3_vae*2", "1280x720", "minimax_h3_fl2va_pruned_pdd", target_res_str="1280x720"),
            "",
        )
        self.assertIn("target", explain_spatial_upsampling_skip("h3_vae*2", "1280x720", target_res_str="1280x720").lower())
        # 1920x1088 source when target is 1920x1080 must skip scale > 1.0 upscalers
        self.assertEqual(
            resolve_effective_spatial_upsampling("h3_vae*1.5", "1920x1088", "minimax_h3_fl2va_pruned_pdd", target_res_str="1920x1080"),
            "",
        )
        # 960x544 source when target is 1920x1080 keeps h3_vae*2 active
        self.assertEqual(
            resolve_effective_spatial_upsampling("h3_vae*2", "960x544", "minimax_h3_fl2va_pruned_pdd", target_res_str="1920x1080"),
            "h3_vae*2",
        )

    def test_07_composable_filters_and_refiners(self):
        # Configure scene S01 (960x544, spatial_upsampling=h3_vae*2) with composable 1x filters & grain
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["scenes"][0]["filters"] = {
            "ai_refiners": ["h3facerefine", "seedvr2*1"],
            "face_strength": 0.6,
            "face_count": 2,
            "film_grain_intensity": 0.15,
            "film_grain_saturation": 0.4,
            "window_color_correction": 0.5,
        }
        # Scene S02 (1920x1088, skips h3_vae*2) also inherits project-level 1x refiner
        cfg["scenes"][1]["filters"] = {"ai_refiners": ["h3facerefine"], "face_strength": 0.85}
        # Legacy scene S04 puts 1x refiner inside spatial_upsampling -> must auto-migrate into ai_refiners
        cfg["scenes"].append({
            "id": "S04", "start": 15.0, "end": 18.0, "mode": "male_solo", "anchor": "A01_test.jpg",
            "spatial_upsampling": "h3facerefine", "prompt": "Legacy 1x refiner", "seed": 401,
        })
        self.cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        ctx = ProjectContext(self.cfg_path)
        _, vr1 = ctx.resolve_video_renderer(ctx.scenes[0])
        t1 = ctx.make_video_task(
            "S01_male_solo_960x544", "Prompt", 124, 101,
            self.proj_dir / "work/anchors/A01_test.jpg", self.proj_dir / "audio/master.mp3", vr1, sc=ctx.scenes[0],
        )
        self.assertEqual(t1["spatial_upsampling"], "h3_vae*2")
        self.assertEqual(t1["_chained_ai_refiners"], ["h3facerefine", "seedvr2*1"])
        self.assertAlmostEqual(t1["film_grain_intensity"], 0.15)
        self.assertAlmostEqual(t1["sliding_window_color_correction_strength"], 0.5)

        _, vr2 = ctx.resolve_video_renderer(ctx.scenes[1])
        t2 = ctx.make_video_task(
            "S02_male_solo_1920x1088", "Hero", 124, 102,
            self.proj_dir / "work/anchors/A01_test.jpg", self.proj_dir / "audio/master.mp3", vr2, sc=ctx.scenes[1],
        )
        # h3_vae*2 is skipped at 1920x1088, leaving spatial_upsampling slot free for h3facerefine inline!
        self.assertEqual(t2["spatial_upsampling"], "h3facerefine")
        self.assertEqual(t2.get("spatial_postprocessing_parameters"), {"face_strength": 0.85, "face_count": 1})
        self.assertNotIn("_chained_ai_refiners", t2)

        f4 = ctx.resolve_scene_filters(ctx.scenes[3])
        self.assertEqual(f4["ai_refiners"], ["h3facerefine"])
        self.assertEqual(resolve_scene_filters({}, raw_spatial_upsampling="seedvr2*1")["ai_refiners"], ["seedvr2*1"])

    def test_08_stage3_ffmpeg_filters(self):
        ff_filters = {
            "deband": True,
            "denoise": 2.0,
            "cas": 0.4,
            "eq": {"brightness": 0.05, "contrast": 1.1, "saturation": 1.0, "gamma": 1.0},
        }
        chain = build_stage3_scale_filters(
            "1920:1080", scale_algo="lanczos", sharpen=0.25, preserve_aspect=True,
            src_dims=(1920, 1088), ffmpeg_filters=ff_filters,
        )
        self.assertTrue(chain[0].startswith("hqdn3d="))
        self.assertIn("crop=1920:1080:0:4", chain)
        self.assertTrue(any(f.startswith("deband=") for f in chain))
        self.assertTrue(any(f.startswith("eq=brightness=0.050:contrast=1.100") for f in chain))
        self.assertIn("cas=strength=0.40", chain)
        self.assertEqual(chain[-1], "setsar=1:1")

    def test_09_fast_queue_per_scene_resolution_hash_sync(self):
        # Verify Bug #1 fix: scene S02 (resolution override 6 -> 1920x1088) matches stored state hash and file on disk -> skipped!
        ctx = ProjectContext(self.cfg_path)
        hashes = compute_project_task_hashes(ctx)
        s2_out = "S02_male_solo_1920x1088"
        s2_mp4 = ctx.renders_dir / f"{s2_out}.mp4"
        subprocess.run(
            [str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1088:r=24:d=5.1", "-c:v", "libx264", "-preset", "ultrafast", str(s2_mp4)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        state = {
            "tasks": {s2_out: hashes[s2_out][0]},
            "scene_renders": {s2_out: {"render_start": 5.0, "render_end": 10.1, "frames": 124}},
        }
        ctx.save_state_hashes(state)

        with mock.patch.object(srv, "PROJECTS_DIR", self.tmp_dir):
            q = srv.compute_fast_project_queue("demo_proj", force_recheck=True)
        by_sid = {item["scene_id"]: item for item in q}
        self.assertFalse(by_sid["S02"]["needs_render"])
        self.assertIn("UP-TO-DATE", by_sid["S02"]["reason"])

    def test_10_render_time_estimates_follow_render_resolution(self):
        """Both backend and JS estimators must use scene/renderer GPU render resolution, not output/default resolution."""
        from pipeline.media_utils import estimate_scene_render_time, attach_queue_time_estimates

        ctx = ProjectContext(self.cfg_path)
        s_hi = {"id": "S91", "start": 0.0, "end": 5.0, "mode": "solo", "resolution": 6}
        s_lo = {"id": "S92", "start": 0.0, "end": 5.0, "mode": "solo", "resolution": 3, "spatial_upsampling": "h3_vae*1.5"}
        est_hi = estimate_scene_render_time(s_hi, ctx=ctx, calibration_alpha=1.0)
        est_lo = estimate_scene_render_time(s_lo, ctx=ctx, calibration_alpha=1.0)
        self.assertEqual(est_hi["resolution"], "1920x1088")
        self.assertEqual(est_lo["resolution"], "1280x720")
        self.assertLess(est_lo["estimated_seconds"], est_hi["estimated_seconds"] * 0.5)

        q = [
            {"scene_id": "S91", "resolution": "1920x1088", "pass": None, "status": "queued", "duration": 5.0},
            {"scene_id": "S92", "resolution": "1280x720", "pass": None, "status": "queued", "duration": 5.0},
        ]
        attach_queue_time_estimates(q, {"S91": s_hi, "S92": s_lo}, ctx=ctx, proj_dir=ctx.project_dir, default_res="1920x1088")
        self.assertEqual(q[0]["estimated_seconds"], est_hi["estimated_seconds"])
        self.assertEqual(q[1]["estimated_seconds"], est_lo["estimated_seconds"])

        # Resolution-scoped calibration isolates 1280x720 from 1920x1088 VRAM spill takes
        from pipeline.media_utils import compute_calibration_factor, estimate_pass_render_time
        manifest_p = ctx.project_dir / "work" / "takes_manifest.json"
        orig_manifest = manifest_p.read_text(encoding="utf-8") if manifest_p.exists() else None
        try:
            pred_720 = estimate_pass_render_time(158, resolution="1280x720", calibration_alpha=1.0)["estimated_seconds"]
            pred_1080 = estimate_pass_render_time(158, resolution="1920x1088", calibration_alpha=1.0)["estimated_seconds"]
            mock_takes = [
                {"resolution": "1280x720", "frames": 158, "elapsed_s": pred_720 * 0.98, "created_at": 100 + i}
                for i in range(4)
            ] + [
                {"resolution": "1920x1088", "frames": 158, "elapsed_s": pred_1080 * 2.4, "created_at": 200 + i}
                for i in range(4)
            ]
            manifest_p.write_text(json.dumps({"scenes": {"S01": {"takes": mock_takes}}}), encoding="utf-8")
            self.assertLess(compute_calibration_factor(ctx.project_dir, resolution="1280x720"), 1.05)
            self.assertGreater(compute_calibration_factor(ctx.project_dir, resolution="1920x1088"), 2.2)
            q_cal = [
                {"scene_id": "S91", "resolution": "1920x1088", "pass": None, "status": "queued", "duration": 5.0},
                {"scene_id": "S92", "resolution": "1280x720", "pass": None, "status": "queued", "duration": 5.0},
            ]
            cal_meta = attach_queue_time_estimates(q_cal, {"S91": s_hi, "S92": s_lo}, ctx=ctx, proj_dir=ctx.project_dir, default_res="1920x1088")
            self.assertLess(q_cal[1]["calibration_alpha"], 1.05)
            self.assertGreater(q_cal[0]["calibration_alpha"], 2.2)
            self.assertIn("1280x720", cal_meta["calibration_alpha_by_res"])
        finally:
            if orig_manifest is not None:
                manifest_p.write_text(orig_manifest, encoding="utf-8")
            elif manifest_p.exists():
                manifest_p.unlink()

        # Frontend estimator parity in Node
        state_js_path = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "state_and_utils.js"
        node_code = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const sliceStart = src.indexOf('// --- Non-Linear Render Time & Split-Window Estimator ---');
const fnSrc = src.slice(sliceStart);
const ctx = {
  window: {},
  document: { getElementById: () => null },
  state: {
    calibrationAlpha: 1.0,
    models: {
      video_resolutions: { "3": { resolution: "1280x720" }, "6": { resolution: "1920x1088" } },
      video_renderers: { "1": { name: "Default", model_type: "minimax_h3_fl2va_pruned_pdd", num_inference_steps: 8 } },
    },
    projectData: {
      project: { fps: 24, master_resolution: "1920x1080", render_overlap_seconds: 0 },
      models: { default_video_resolution: 6, default_video_renderer: 1 },
    },
  },
  addEventListener() {},
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
const eHi = ctx.estimateSceneRenderInfo({ id: 'S91', start: 0, end: 5, mode: 'solo' });
const eLo = ctx.estimateSceneRenderInfo({ id: 'S92', start: 0, end: 5, mode: 'solo', resolution: 3, spatial_upsampling: 'h3_vae*1.5' });
if (eHi.resolution !== '1920x1088') throw new Error('Expected 1920x1088, got ' + eHi.resolution);
if (eLo.resolution !== '1280x720') throw new Error('Expected 1280x720, got ' + eLo.resolution);
if (!(eLo.total_estimated_seconds < eHi.total_estimated_seconds * 0.5)) throw new Error('Expected 1280x720 time << 1920x1088');
console.log('EST_PARITY_OK:' + eLo.total_estimated_seconds + '<' + eHi.total_estimated_seconds);
"""
        res = subprocess.run(["node", "-e", node_code, str(state_js_path)], capture_output=True, text=True, check=True)
        self.assertIn("EST_PARITY_OK:", res.stdout)

    def test_11_scenes_tab_ui_and_sync_contracts(self):
        """Verify vertical Scenes tab DOM/script hooks, line-count ceiling, and JS helper parity."""
        ui_dir = WORKSPACE_ROOT / "work" / "ui" / "static"
        index_html = (ui_dir / "index.html").read_text(encoding="utf-8")
        for token in ('data-tab="scenes"', 'id="tab-scenes"', "css/scenes_tab.css", "js/scenes_tab.js"):
            self.assertIn(token, index_html)

        scenes_js = (ui_dir / "js" / "scenes_tab.js").read_text(encoding="utf-8")
        scenes_css = (ui_dir / "css" / "scenes_tab.css").read_text(encoding="utf-8")
        self.assertIn("flex-shrink: 0;", scenes_css)
        self.assertIn("overflow-y: auto;", scenes_css)
        master_js = (ui_dir / "js" / "master_and_export.js").read_text(encoding="utf-8")
        takes_js = (ui_dir / "js" / "takes_and_history.js").read_text(encoding="utf-8")
        app_js = (ui_dir / "js" / "app.js").read_text(encoding="utf-8")

        for sym in ("renderSceneList", "syncScenesTabCard", "isSceneUnsavedDirty", "expandedFilterIds", "+ End Anchor (Blend)"):
            self.assertIn(sym, scenes_js)
        for removed in ("Match Next", "Play scene audio slice", "✂️ Split", "🎬 Clip", "Start (s)", "End (s)", "P1 Seed", "Random Seed", '["edited", "Modified"]'):
            self.assertNotIn(removed, scenes_js)
        for sym in ("getSceneOptionCatalog", "applySceneFilterValue", "buildAdvSummaryBadgesHtml", "initInspectorCollapsibleGroups"):
            self.assertIn(sym, master_js)
        self.assertIn("revertSceneToActiveTake", takes_js)
        self.assertIn("js/scenes_tab.js", app_js)

        oversized = [
            (len(p.read_text(encoding="utf-8").splitlines()), str(p.relative_to(WORKSPACE_ROOT)))
            for p in (WORKSPACE_ROOT / "work").rglob("*")
            if p.is_file() and p.suffix in (".py", ".js", ".html", ".css")
            and len(p.read_text(encoding="utf-8").splitlines()) >= 850
        ]
        self.assertEqual(oversized, [], f"Files >= 850 lines: {oversized}")

        node_code = r"""
        const fs = require("fs"), vm = require("vm"), path = require("path");
        const jsDir = process.argv[1];
        for (const f of fs.readdirSync(jsDir)) {
          if (f.endsWith(".js")) new vm.Script(fs.readFileSync(path.join(jsDir, f), "utf8"), { filename: f });
        }
        const noop = () => ({});
        const stubEl = () => ({ addEventListener() {}, classList: { toggle() {}, add() {}, remove() {} }, style: {}, options: [{}], add() {} });
        const sandbox = {
          window: {},
          document: { getElementById: () => stubEl(), querySelectorAll: () => [], createElement: () => stubEl() },
          state: {
            projectData: {
              project: { master_resolution: "1280x720", stems: { custom_vox: "audio/custom.mp3" } },
              models: { default_video_resolution: "3", video_shared_settings: { spatial_upsampling: "lanczos*1.5", filters: { ffmpeg: { deband: true } } } },
            },
            models: {
              video_resolutions: { "3": { width: 1280, height: 720, name: "720p" }, "5": { width: 1536, height: 864, name: "864p" } },
              video_renderers: { "1": { name: "LTX2" } },
            },
            assets: { audio_files: ["backing.wav"] },
            selectedSceneIdx: null,
          },
          markProjectDirty() {}, showToast() {},
        };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync(path.join(jsDir, "master_and_export.js"), "utf8"), sandbox);
        const sc720 = { id: "s01", resolution: "3" };
        const cat720 = sandbox.getSceneOptionCatalog(sc720);
        if (!cat720.skipTarget || cat720.skipOom) throw new Error("Expected skipTarget=true, skipOom=false for 720p");
        const sc864 = { id: "s02", resolution: "5" };
        const cat864 = sandbox.getSceneOptionCatalog(sc864);
        if (!cat864.skipOom) throw new Error("Expected skipOom=true for >=1536px");
        sandbox.applySceneFilterValue(sc720, "ai_refiner", "seedvr2*1");
        sandbox.applySceneFilterValue(sc720, "cas", "0.35");
        sandbox.applySceneFilterValue(sc720, "brightness", "-0.05");
        if (sc720.filters.ai_refiners[0] !== "seedvr2*1") throw new Error("ai_refiners mismatch");
        if (sc720.filters.ffmpeg.cas !== 0.35 || sc720.filters.ffmpeg.eq.brightness !== -0.05) throw new Error("ffmpeg filter mismatch");
        const badgeHtml = sandbox.buildAdvSummaryBadgesHtml(sc720);
        if (!badgeHtml.includes("seedvr2*1") || !badgeHtml.includes("CAS 0.35") || !badgeHtml.includes("Bright -0.05")) throw new Error("Summary badge mismatch: " + badgeHtml);
        sandbox.applySceneFilterValue(sc720, "ai_refiner", "");
        sandbox.applySceneFilterValue(sc720, "cas", "");
        sandbox.applySceneFilterValue(sc720, "brightness", "");
        if (sc720.filters !== undefined) throw new Error("Expected empty filters cleanup");
        if (!sandbox.buildAdvSummaryBadgesHtml(sc720).includes("Project Defaults")) throw new Error("Expected default badge fallback");
        console.log("SCENES_TAB_PARITY_OK");
        """
        res = subprocess.run(["node", "-e", node_code, str(ui_dir / "js")], capture_output=True, text=True, check=True)
        self.assertIn("SCENES_TAB_PARITY_OK", res.stdout)

    def test_12_resolution_change_replaces_existing_queue_entry(self):
        cfg_file = self.proj_dir / "demo_proj.json"
        cfg = {
            "project": {"name": "demo_proj", "fps": 24},
            "models": {"default_video_resolution": "6"},
            "scenes": [
                {"id": "S01", "start": 0.0, "end": 2.0, "mode": "solo", "resolution": 3, "prompt": "p1"},
                {"id": "S02", "start": 2.0, "end": 4.0, "mode": "duet", "resolution": 3, "prompt_m": "pm", "prompt_f": "pf"},
            ],
        }
        cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        with mock.patch.object(srv, "PROJECTS_DIR", self.tmp_dir):
            q1 = srv.compute_fast_project_queue("demo_proj", force_recheck=False)
            self.assertEqual([x["output_filename"] for x in q1], ["S01_solo_1280x720", "S02_Pass1_Left_1280x720", "S02_Pass2_Final_1280x720"])

            # Change S01 & S02 to resolution 6 (1920x1088) and save
            cfg["scenes"][0]["resolution"] = 6
            cfg["scenes"][1]["resolution"] = 6
            cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            q2 = srv.compute_fast_project_queue("demo_proj", force_recheck=False)
            self.assertEqual([x["output_filename"] for x in q2], ["S01_solo_1920x1088", "S02_Pass1_Left_1920x1088", "S02_Pass2_Final_1920x1088"])
            self.assertEqual(len(q2), 3)

            # Even if a stale old-resolution row was injected into v3_queue.json, prepare_targeted_queue_items replaces it cleanly
            q_file = self.proj_dir / "work/jobs/v3_queue.json"
            stale = list(q2) + [{"idx": 4, "id": "S01_solo_1280x720", "scene_id": "S01", "target_arg": "S01", "pass": None, "filename": "S01_solo_1280x720.mp4", "output_filename": "S01_solo_1280x720", "status": "queued", "needs_render": True, "resolution": "1280x720"}]
            q_file.write_text(json.dumps(stale, indent=2), encoding="utf-8")
            srv.prepare_targeted_queue_items("demo_proj", "S01", is_worker_running=False)
            q3 = json.loads(q_file.read_text(encoding="utf-8"))
            s01_entries = [x for x in q3 if x["scene_id"] == "S01"]
            self.assertEqual([x["output_filename"] for x in s01_entries], ["S01_solo_1920x1088"])

    def test_13_takes_list_delete_and_scene_lock_contracts(self):
        """Verify multi-take list/delete UI, scene locking contracts, Node VM helpers, and <850 line ceiling."""
        ui_dir = WORKSPACE_ROOT / "work" / "ui" / "static"
        routes_py = (WORKSPACE_ROOT / "work" / "ui" / "server_routes.py").read_text(encoding="utf-8")
        index_html = (ui_dir / "index.html").read_text(encoding="utf-8")
        master_js = (ui_dir / "js" / "master_and_export.js").read_text(encoding="utf-8")
        takes_js = (ui_dir / "js" / "takes_and_history.js").read_text(encoding="utf-8")
        timeline_js = (ui_dir / "js" / "timeline_and_playback.js").read_text(encoding="utf-8")
        inspector_js = (ui_dir / "js" / "scene_inspector.js").read_text(encoding="utf-8")
        scenes_js = (ui_dir / "js" / "scenes_tab.js").read_text(encoding="utf-8")
        scenes_css = (ui_dir / "css" / "scenes_tab.css").read_text(encoding="utf-8")

        self.assertIn('"/api/takes/delete"', routes_py)
        self.assertIn('"delete_take"', routes_py)
        for token in ('id="btnToggleSceneLock"', 'id="btnDeleteTake"', 'id="sceneTakesList"'):
            self.assertIn(token, index_html)
        for sym in ("mvg_inspector_collapsed_v2", "formatTakeSummaryLabel", "deleteSceneTake", "renderTakesListItems", "applyInspectorLockState", "toggleSceneLock"):
            self.assertIn(sym, master_js)
        for sym in ("restoreSceneToTake", "renderTakesListItems", "btnDeleteTake"):
            self.assertIn(sym, takes_js)
        self.assertIn('sc.locked ? "🔒 " : ""', timeline_js)
        self.assertIn("scA?.locked || scB?.locked", timeline_js)
        self.assertIn("🔒 LOCKED", inspector_js)
        self.assertIn("applyInspectorLockState", inspector_js)
        for sym in ("Takes Vault", 'dataset.action = "lock"', "🔒 Locked", "🔓 Lock", "is-locked"):
            self.assertIn(sym, scenes_js)
        for sym in (".scene-takes-list", ".take-list-row", ".scene-takes-drawer", ".scene-card.is-locked"):
            self.assertIn(sym, scenes_css)

        oversized = [
            (len(p.read_text(encoding="utf-8").splitlines()), str(p.relative_to(WORKSPACE_ROOT)))
            for p in (WORKSPACE_ROOT / "work").rglob("*")
            if p.is_file() and p.suffix in (".py", ".js", ".html", ".css")
            and len(p.read_text(encoding="utf-8").splitlines()) >= 850
        ]
        self.assertEqual(oversized, [], f"Files >= 850 lines: {oversized}")

        node_code = r"""
        const fs = require("fs"), vm = require("vm"), path = require("path");
        const jsDir = process.argv[1];
        const stubEl = () => ({
          addEventListener() {},
          classList: { toggle() {}, add() {}, remove() {} },
          style: {},
          options: [{}],
          add() {},
          querySelectorAll() { return []; },
          querySelector() { return null; },
        });
        const btnLock = stubEl();
        const formEl = stubEl();
        const badgeEl = stubEl();
        const sandbox = {
          window: {},
          document: {
            getElementById(id) {
              if (id === "btnToggleSceneLock") return btnLock;
              if (id === "inspectorForm") return formEl;
              if (id === "inspSceneHeader") return badgeEl;
              return stubEl();
            },
            querySelectorAll: () => [],
            createElement: () => stubEl(),
          },
          state: {
            projectData: {
              project: { master_resolution: "1920x1088" },
              models: { default_video_resolution: "6" },
              scenes: [{ id: "S01", start: 0, end: 5, prompt: "p1", locked: false }],
            },
            models: { video_resolutions: { "6": { width: 1920, height: 1088, name: "1088p" } }, video_renderers: {} },
            assets: { audio_files: [] },
            selectedSceneIdx: 0,
          },
          markProjectDirty() {},
          showToast() {},
          redrawTimeline() {},
          updateInspectorSceneHeader() {},
        };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync(path.join(jsDir, "master_and_export.js"), "utf8"), sandbox);
        const label = sandbox.formatTakeSummaryLabel({ take_number: 2, created_at: "2026-11-14T12:00:00Z", seed: 42 }, true);
        if (!String(label).includes("Take 2") || !String(label).includes("42")) throw new Error("Unexpected take label: " + label);
        sandbox.toggleSceneLock(0);
        if (sandbox.state.projectData.scenes[0].locked !== true) throw new Error("Expected scene S01 locked=true");
        if (!btnLock.textContent.includes("Locked")) throw new Error("Expected btnLock text to reflect Locked: " + btnLock.textContent);
        sandbox.toggleSceneLock(0);
        if (sandbox.state.projectData.scenes[0].locked !== undefined) throw new Error("Expected unlocked scene S01 to delete locked property");
        if (!btnLock.textContent.includes("Lock")) throw new Error("Expected btnLock text to reflect Lock: " + btnLock.textContent);
        console.log("TAKES_LOCK_CONTRACTS_OK");
        """
        res = subprocess.run(["node", "-e", node_code, str(ui_dir / "js")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("TAKES_LOCK_CONTRACTS_OK", res.stdout)

    def test_14_master_stitch_unload_progress_and_reload(self):
        """Verify Stage 3 emits stitch progress, worker forwards events, and Master UI unloads/shows progress/reloads."""
        import contextlib
        import io
        from work.ui import server_workers as sw

        # 1. Stage 3 assemble_progressive_master emits [MASTER_STITCH_PROGRESS] payloads reaching 100%
        ctx = ProjectContext(self.cfg_path)
        s1_mp4 = ctx.renders_dir / "S01_male_solo_960x544.mp4"
        subprocess.run(
            [str(self.ffmpeg), "-y", "-f", "lavfi", "-i", "testsrc2=s=960x544:r=24:d=5", "-c:v", "libx264", "-preset", "ultrafast", str(s1_mp4)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = assemble_progressive_master(ctx, [
                {"id": "S01", "start": 0.0, "end": 5.0, "dur": 5.0, "mode": "male_solo", "clip": "S01_male_solo_960x544.mp4"}
            ])
        self.assertTrue(ok)
        out_lines = buf.getvalue().splitlines()
        prog_events = [
            json.loads(line.split("[MASTER_STITCH_PROGRESS]", 1)[1].strip())
            for line in out_lines if line.startswith("[MASTER_STITCH_PROGRESS]")
        ]
        self.assertGreaterEqual(len(prog_events), 3)
        self.assertTrue(all(0 <= ev["pct"] <= 100 for ev in prog_events))
        self.assertEqual(prog_events[-1]["pct"], 100)
        self.assertTrue(any("[Progressive Master Updated]" in l for l in out_lines))

        # 2. server_workers emits [MASTER_STITCH_START], forwards [MASTER_STITCH_PROGRESS] unprefixed, emits [MASTER_UPDATED]
        fake_stdout = io.StringIO(
            '[MASTER_STITCH_PROGRESS] {"pct": 45, "phase": "Muxing", "detail": "S01"}\n'
            '[Progressive Master Updated] -> master.mp4 (1/1 scenes rendered (100% complete))\n'
        )
        fake_proc = mock.MagicMock()
        fake_proc.stdout = fake_stdout
        fake_proc.returncode = 0
        fake_proc.wait.return_value = 0
        logs = []
        with mock.patch.object(srv, "broadcast_log", side_effect=logs.append), \
             mock.patch.object(sw.subprocess, "Popen", return_value=fake_proc):
            sw.run_stitch_worker(str(self.cfg_path), "demo_proj")
        self.assertTrue(any(l.startswith("[MASTER_STITCH_START]") for l in logs))
        self.assertIn('[MASTER_STITCH_PROGRESS] {"pct": 45, "phase": "Muxing", "detail": "S01"}', logs)
        self.assertIn("[MASTER_UPDATED]", logs)
        self.assertFalse(getattr(srv, "stitch_in_progress", False))
        self.assertIsNone(getattr(srv, "stitch_progress", None))

        err_logs = []
        with mock.patch.object(srv, "broadcast_log", side_effect=err_logs.append), \
             mock.patch.object(sw.subprocess, "Popen", side_effect=OSError("spawn failed")):
            sw.run_stitch_worker(str(self.cfg_path), "demo_proj")
        self.assertTrue(any(l.startswith("[MASTER_ERROR]") for l in err_logs))
        self.assertFalse(getattr(srv, "stitch_in_progress", False))

        # 3. HTML/JS DOM + SSE + cache-buster parity + line-count ceiling (<850) contracts
        ui_dir = WORKSPACE_ROOT / "work" / "ui" / "static"
        index_html = (ui_dir / "index.html").read_text(encoding="utf-8")
        queue_js = (ui_dir / "js" / "queue_and_console.js").read_text(encoding="utf-8")
        for dom_id in ('id="masterVideoContainer"', 'id="masterStitchOverlay"', 'id="masterStitchPhase"', 'id="masterStitchBar"', 'id="masterStitchDetail"', 'id="masterStitchPct"'):
            self.assertIn(dom_id, index_html)
        import re as _re
        for js_name in ("queue_and_console.js", "master_and_export.js"):
            self.assertRegex(index_html, rf'js/{_re.escape(js_name)}\?v=\d+')
        self.assertIn("handleMasterStitchSSE", queue_js)

        oversized = [
            (len(p.read_text(encoding="utf-8").splitlines()), str(p.relative_to(WORKSPACE_ROOT)))
            for p in (WORKSPACE_ROOT / "work").rglob("*")
            if p.is_file() and p.suffix in (".py", ".js", ".html", ".css")
            and len(p.read_text(encoding="utf-8").splitlines()) >= 850
        ]
        self.assertEqual(oversized, [], f"Files >= 850 lines: {oversized}")

        # 4. Node VM behavioral verification of unload -> progress -> block intermediate refresh -> reload
        node_code = r"""
        const fs = require("fs"), vm = require("vm"), path = require("path");
        const jsDir = process.argv[1];
        function mkEl(init = {}) {
          const attrs = {}, listeners = {};
          const el = {
            style: {}, dataset: {}, options: [{}], classList: { toggle() {}, add() {}, remove() {} },
            paused: true, currentTime: 0, textContent: "", disabled: false, loadCalls: 0, pauseCalled: false, playCalled: false,
            setAttribute(k, v) { attrs[k] = String(v); if (k === "src") el._src = String(v); },
            getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
            removeAttribute(k) { delete attrs[k]; if (k === "src") delete el._src; },
            addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
            pause() { el.pauseCalled = true; el.paused = true; },
            load() { el.loadCalls++; (listeners.loadedmetadata || []).splice(0).forEach((fn) => fn()); },
            play() { el.playCalled = true; el.paused = false; return Promise.resolve(); },
            add() {}, querySelectorAll() { return []; }, querySelector() { return null; },
            ...init,
          };
          Object.defineProperty(el, "src", {
            get() { return el.getAttribute("src") || ""; },
            set(v) { if (v) el.setAttribute("src", v); else el.removeAttribute("src"); },
          });
          return el;
        }
        const player = mkEl({ paused: false, currentTime: 12.5 });
        player.setAttribute("src", "/media/demo_proj/master?t=100");
        player.setAttribute("data-loaded-src", "/media/demo_proj/master?t=100");
        const overlay = mkEl({ style: { display: "none" } });
        const phase = mkEl(), bar = mkEl(), detail = mkEl(), pct = mkEl(), badge = mkEl(), meta = mkEl();
        const btnStitch = mkEl({ textContent: "🧵 Stitch Master Video" }), btnRefresh = mkEl();
        const map = {
          masterVideoPlayer: player, masterStitchOverlay: overlay, masterStitchPhase: phase,
          masterStitchBar: bar, masterStitchDetail: detail, masterStitchPct: pct,
          masterVideoBadge: badge, masterVideoMeta: meta,
          btnStitchMasterFromTab: btnStitch, btnRefreshMasterVideo: btnRefresh,
        };
        const sandbox = {
          window: {}, Date, Promise,
          document: { getElementById: (id) => map[id] || mkEl(), querySelectorAll: () => [], createElement: () => mkEl() },
          state: {
            currentSlug: "demo_proj",
            projectData: { project: { master_resolution: "1920x1080" }, models: { default_video_resolution: "4" } },
            models: { video_resolutions: {}, video_renderers: {} },
            assets: { master_videos: [{ filename: "master.mp4", size_mb: 1.5, mtime: 100 }] },
            selectedSceneIdx: null,
          },
          markProjectDirty() {}, showToast() {},
          async refreshProjectAssets() { sandbox.state.assets.master_videos[0].mtime = 200; },
        };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync(path.join(jsDir, "master_and_export.js"), "utf8"), sandbox);
        (async () => {
          sandbox.unloadMasterVideoForStitch();
          if (!sandbox.state.isStitchingMaster) throw new Error("Expected isStitchingMaster=true");
          if (!player.pauseCalled || player.getAttribute("src") !== null || player.getAttribute("data-loaded-src") !== null || player.loadCalls < 1) {
            throw new Error("Master player was not unloaded cleanly");
          }
          if (player.style.display !== "none" || overlay.style.display !== "flex") throw new Error("Overlay visibility mismatch");
          sandbox.handleMasterStitchSSE("[MASTER_STITCH_START] {}");
          sandbox.updateMasterVideoUI();
          if (player.getAttribute("src") !== null) throw new Error("Intermediate refresh reloaded video during stitch");
          sandbox.handleMasterStitchSSE('[MASTER_STITCH_PROGRESS] {"pct":65,"phase":"Muxing audio","detail":"Concatenating 3 scenes"}');
          if (bar.style.width !== "65%" || pct.textContent !== "65%" || detail.textContent !== "Concatenating 3 scenes") {
            throw new Error("Progress overlay mismatch: " + bar.style.width + " " + pct.textContent);
          }
          sandbox.handleMasterStitchSSE("[MASTER_UPDATED]");
          await new Promise((r) => setTimeout(r, 20));
          if (sandbox.state.isStitchingMaster !== false) throw new Error("Expected isStitchingMaster=false after completion");
          if (overlay.style.display !== "none" || player.style.display !== "block") throw new Error("Player not restored");
          if (!String(player.getAttribute("src")).includes("/media/demo_proj/master?t=200")) throw new Error("Master video did not reload new mtime: " + player.getAttribute("src"));
          if (Math.abs(player.currentTime - 12.5) > 0.01 || !player.playCalled) throw new Error("Playback state not restored after reload");
          if (btnStitch.disabled || btnRefresh.disabled) throw new Error("Buttons remained disabled");
          console.log("MASTER_STITCH_UI_OK");
        })().catch((err) => { console.error(err); process.exit(1); });
        """
        res = subprocess.run(["node", "-e", node_code, str(ui_dir / "js")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("MASTER_STITCH_UI_OK", res.stdout)

    def test_15_queue_hash_fallback_and_non_empty_state_hashes(self):
        """Queue computation must fall back if srv.ProjectContext is None and never poison state_hashes with empty strings."""
        from work.ui import server as srv
        from work.ui import server_queue as sq

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "projects" / "duet_demo"
            anchors_dir = proj_dir / "work" / "anchors"
            renders_dir = proj_dir / "work" / "renders"
            jobs_dir = proj_dir / "work" / "jobs"
            audio_dir = proj_dir / "audio"
            for d in (anchors_dir, renders_dir, jobs_dir, audio_dir):
                d.mkdir(parents=True, exist_ok=True)
            (anchors_dir / "A01 - Duet.jpg").write_bytes(b"img-a01")
            (renders_dir / "S20_Pass1_Left_1280x720.mp4").write_bytes(b"\x00" * 60000)
            (renders_dir / "S20_Pass2_Final_1280x720.mp4").write_bytes(b"\x00" * 60000)

            cfg = {
                "project": {"name": "Duet Demo", "fps": 25, "total_duration": 10.0, "master_resolution": "1920x1080", "master_audio": "audio/song.mp3"},
                "models": {"default_video_renderer": 1, "default_video_resolution": 3},
                "anchors": [{"id": "A01", "filename": "A01 - Duet.jpg"}],
                "scenes": [{
                    "id": "S20", "start": 0, "end": 5, "mode": "duet_method_b",
                    "anchor": "A01 - Duet.jpg", "prompt_m": "Left prompt", "prompt_f": "Right prompt",
                    "seed_m": 100, "seed_f": 200, "split_x": 0.5, "stem_m": "male", "stem_f": "female",
                    "resolution": 3,
                }],
            }
            cfg_path = proj_dir / "duet_demo.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            sh_file = jobs_dir / "state_hashes.json"
            sh_file.write_text(json.dumps({"tasks": {}, "task_hashes": {}}), encoding="utf-8")

            with mock.patch.object(srv, "PROJECTS_DIR", tmp_path / "projects"), \
                 mock.patch.object(srv, "ProjectContext", None), \
                 mock.patch.object(srv, "compute_project_task_hashes", None):
                q1 = sq.compute_fast_project_queue("duet_demo")
                self.assertEqual(len(q1), 2)
                self.assertTrue(all(it["task_hash"] for it in q1))

                # Edit spatial_upsampling on S20 -> Pass 2 must queue immediately
                cfg["scenes"][0]["spatial_upsampling"] = "h3_vae*1.5"
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                q2 = sq.compute_fast_project_queue("duet_demo")
                p2 = next(it for it in q2 if it["pass"] == 2)
                self.assertEqual(p2["status"], "queued")
                self.assertIn("EDITED", p2["reason"])

                # If hash computation throws, log warning and never save empty hash strings to state_hashes.json
                sh_file.write_text(json.dumps({"tasks": {}, "task_hashes": {}}), encoding="utf-8")
                logs = []
                with mock.patch.object(srv, "broadcast_log", side_effect=logs.append), \
                     mock.patch.object(sq, "_DEF_CPTH", side_effect=RuntimeError("boom")):
                    sq.compute_fast_project_queue("duet_demo")
                self.assertTrue(any("boom" in l for l in logs))
                saved = json.loads(sh_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["tasks"], {})


if __name__ == "__main__":
    unittest.main()
