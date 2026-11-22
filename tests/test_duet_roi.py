"""
Unit & integration tests for Cross-Engine Pass 2, Cropped-ROI Local GPU Acceleration,
and Post-Render Single-Pass Lip-Sync Repair (Zero Live GPU / Zero Network).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from pipeline import duet_roi
from pipeline.context import ProjectContext
from pipeline.fal_catalog import resolve_task_backend
from pipeline.media_utils import probe_video_dimensions, probe_video_frames
from pipeline.stage2_hashes import compute_project_task_hashes
from pipeline.takes import load_takes_manifest
import server
import server_routes


def _find_ffmpeg() -> Path:
    local_ff = WORKSPACE_ROOT / "tools" / "ffmpeg.exe"
    if local_ff.is_file():
        return local_ff
    found = shutil.which("ffmpeg")
    if not found:
        raise unittest.SkipTest("ffmpeg not available")
    return Path(found)


def _make_av_mp4(ffmpeg: Path, out_mp4: Path, w: int = 640, h: int = 368, fps: int = 24, dur: float = 0.6, channels: int = 2):
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg), "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", str(channels), str(out_mp4),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_wav(ffmpeg: Path, out_wav: Path, dur: float = 1.0, freq: int = 520):
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg), "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
            "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(out_wav),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_jpg(ffmpeg: Path, out_jpg: Path, w: int = 640, h: int = 368):
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"color=c=navy:s={w}x{h}:d=0.05", "-vframes", "1", "-q:v", "2", str(out_jpg)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _probe_audio_channels(ffmpeg: Path, media_path: Path) -> int:
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.name.lower().endswith(".exe") else "ffprobe")
    exe = ffprobe if ffprobe.is_file() else (shutil.which("ffprobe") or "ffprobe")
    res = subprocess.run(
        [str(exe), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0", str(media_path)],
        capture_output=True, text=True,
    )
    try:
        return int(res.stdout.strip().splitlines()[0])
    except Exception:
        return 0


def _build_temp_project(root: Path, ffmpeg: Path, extra_scene: dict | None = None) -> tuple[ProjectContext, Path]:
    proj_dir = root / "proj"
    (proj_dir / "work" / "renders_v3").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "anchors_v3").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "audio_stems").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "jobs_v3").mkdir(parents=True, exist_ok=True)
    anchor_jpg = proj_dir / "work" / "anchors_v3" / "A01.jpg"
    _make_jpg(ffmpeg, anchor_jpg)
    _make_wav(ffmpeg, proj_dir / "work" / "audio_stems" / "male.wav", dur=2.0, freq=330)
    _make_wav(ffmpeg, proj_dir / "work" / "audio_stems" / "female.wav", dur=2.0, freq=660)
    _make_wav(ffmpeg, proj_dir / "work" / "audio_stems" / "full_mix.wav", dur=2.0, freq=440)

    sc = {
        "id": "S01",
        "start": 0.0,
        "end": 1.0,
        "mode": "duet_method_b",
        "anchor": "A01",
        "stem": "male",
        "stem_m": "male",
        "stem_f": "female",
        "split_x": 0.50,
        "renderer": "1",
        "prompt_m": "Man singing left",
        "prompt_f": "Woman singing right",
        "seed": 1234,
    }
    if extra_scene:
        sc.update(extra_scene)

    cfg = {
        "environment": {
            "ffmpeg": str(ffmpeg),
        },
        "project": {
            "title": "ROI Test",
            "fps": 24,
            "version": "3",
            "master_audio": "work/audio_stems/full_mix.wav",
            "stems": {
                "male": "work/audio_stems/male.wav",
                "female": "work/audio_stems/female.wav",
            },
            "directories": {
                "renders": "work/renders_v3",
                "anchors": "work/anchors_v3",
                "jobs": "work/jobs_v3",
                "audio_slices": "work/audio_v3",
                "masks": "work/masks_v3",
                "trimmed": "work/trimmed_v3",
            },
        },
        "models": {
            "default_video_renderer": "1",
            "default_video_resolution": "4",
            "video_resolutions": {
                "99": {"resolution": "640x368", "name": "640x368 Test"},
            },
            "video_renderers": {
                "1": {"model_type": "minimax_h3_fl2va_pruned_pdd", "name": "Local H3"},
                "14": {"model_type": "minimax_h3_v2", "name": "MiniMax H3", "backend": "minimax"},
            },
            "fal_video_renderers": {},
            "minimax_video_renderers": {
                "14": {"model_type": "minimax_h3_v2", "name": "MiniMax H3", "backend": "minimax"},
            },
        },
        "characters": {},
        "anchors": [{"id": "A01", "file": "work/anchors_v3/A01.jpg"}],
        "scenes": [sc],
    }
    cfg_path = proj_dir / "roi_test.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return ProjectContext(cfg_path), cfg_path


class TestDuetRoiAndLipSyncRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = _find_ffmpeg()

    def test_01_compute_roi_box_alignment_and_bounds(self):
        for w, h in ((1920, 1088), (1344, 768)):
            for mode in ("full", "half_split", "tight_performer", "custom"):
                for side in ("left", "right"):
                    cx, cy, cw, ch = duet_roi.compute_roi_box(
                        w, h, split_x=0.48, side=side, roi_mode=mode, custom_box=[0.42, 0.08, 0.96, 0.88]
                    )
                    self.assertEqual(cw % 16, 0, f"{mode}/{side} cw={cw}")
                    self.assertEqual(ch % 16, 0, f"{mode}/{side} ch={ch}")
                    self.assertEqual(cx % 2, 0)
                    self.assertEqual(cy % 2, 0)
                    self.assertGreaterEqual(cx, 0)
                    self.assertGreaterEqual(cy, 0)
                    self.assertLessEqual(cx + cw, w)
                    self.assertLessEqual(cy + ch, h)

    def test_02_border_ring_and_feather_pgm_masks(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            ring = p / "ring.pgm"
            duet_roi.write_border_ring_pgm(ring, 320, 320, full_w=640, full_h=368, cx=320, cy=24, border_px=20)
            raw = ring.read_bytes()
            hdr, body = raw.split(b"255\n", 1)
            self.assertIn(b"320 320", hdr)
            self.assertEqual(len(body), 320 * 320)
            # Top/bottom/left borders share edges with Pass 1 -> locked (0); right edge is outer frame edge -> unlocked (255)
            self.assertEqual(body[0], 0)
            self.assertEqual(body[160 * 320 + 0], 0)
            self.assertEqual(body[160 * 320 + 319], 255)
            self.assertEqual(body[160 * 320 + 160], 255)
            self.assertEqual(body[319 * 320 + 160], 0)

            alpha = p / "alpha.pgm"
            duet_roi.write_feather_alpha_pgm(alpha, 320, 320, full_w=640, full_h=368, cx=320, cy=24, feather_px=20)
            _, abody = alpha.read_bytes().split(b"255\n", 1)
            self.assertLess(abody[160 * 320 + 0], abody[160 * 320 + 10])
            self.assertLess(abody[160 * 320 + 10], 255)
            self.assertEqual(abody[160 * 320 + 319], 255)

            # Verify default border_px=64 survives Wan2GP's 32x32 patch max-pooling on half_split & tight_performer
            half_ring = p / "half_ring.pgm"
            box_half = duet_roi.compute_roi_box(1920, 1088, split_x=0.5, side="right", roi_mode="half_split")
            self.assertEqual(box_half[2] % 32, 0)
            duet_roi.write_border_ring_pgm(half_ring, box_half[2], box_half[3], full_w=1920, full_h=1088, cx=box_half[0], cy=box_half[1])
            _, hbody = half_ring.read_bytes().split(b"255\n", 1)
            cw_h, ch_h = box_half[2], box_half[3]
            # First two 32x32 patch columns (x=0..63) must remain strictly locked (max == 0); interior column x>=64 is 255
            for px_col in range(2):
                cell_vals = [hbody[y * cw_h + x] for y in range(0, ch_h, 32) for x in range(px_col * 32, (px_col + 1) * 32)]
                self.assertEqual(max(cell_vals), 0, f"32px patch col {px_col} was eroded by max-pooling")
            self.assertEqual(hbody[(ch_h // 2) * cw_h + 64], 255)

            tight_ring = p / "tight_ring.pgm"
            box_tight = duet_roi.compute_roi_box(1920, 1088, split_x=0.5, side="right", roi_mode="tight_performer")
            self.assertEqual(box_tight, (1056, 44, 864, 768))
            self.assertEqual(box_tight[0] + box_tight[2], 1920)
            self.assertEqual(box_tight[2] % 32, 0)
            self.assertEqual(box_tight[3] % 32, 0)
            duet_roi.write_border_ring_pgm(tight_ring, box_tight[2], box_tight[3], full_w=1920, full_h=1088, cx=box_tight[0], cy=box_tight[1])
            _, tbody = tight_ring.read_bytes().split(b"255\n", 1)
            cw_t, ch_t = box_tight[2], box_tight[3]
            # Top/bottom/left share edges with Pass 1 -> locked (0); outer right frame edge is unlocked (255)
            self.assertEqual(max(tbody[y * cw_t + x] for y in range(64) for x in range(cw_t)), 0)
            self.assertEqual(max(tbody[y * cw_t + x] for y in range(ch_t - 64, ch_t) for x in range(cw_t)), 0)
            self.assertEqual(max(tbody[y * cw_t + x] for y in range(ch_t) for x in range(64)), 0)
            self.assertEqual(tbody[(ch_t // 2) * cw_t + (cw_t - 1)], 255)
            self.assertEqual(tbody[(ch_t // 2) * cw_t + (cw_t // 2)], 255)

            # Verify shifted feather alpha discards 3D-VAE mask=0 border and blends smoothly across x=60..80
            tight_alpha = p / "tight_alpha.pgm"
            duet_roi.write_feather_alpha_pgm(
                tight_alpha, cw_t, ch_t, full_w=1920, full_h=1088, cx=box_tight[0], cy=box_tight[1], feather_px=20, border_px=64
            )
            _, tabody = tight_alpha.read_bytes().split(b"255\n", 1)
            mid_row = (ch_t // 2) * cw_t
            self.assertEqual(max(tabody[mid_row + x] for x in range(60)), 0)
            self.assertLess(0, tabody[mid_row + 64])
            self.assertLess(tabody[mid_row + 64], tabody[mid_row + 75])
            self.assertEqual(tabody[mid_row + 80], 255)
            self.assertEqual(tabody[mid_row + (cw_t - 1)], 255)

            # Interior custom box still locks all 4 interior edges
            custom_ring = p / "custom_ring.pgm"
            box_c = duet_roi.compute_roi_box(1920, 1088, roi_mode="custom", custom_box=[0.55, 0.05, 0.95, 0.75])
            duet_roi.write_border_ring_pgm(custom_ring, box_c[2], box_c[3], full_w=1920, full_h=1088, cx=box_c[0], cy=box_c[1])
            _, cbody = custom_ring.read_bytes().split(b"255\n", 1)
            cw_c, ch_c = box_c[2], box_c[3]
            self.assertEqual(max(cbody[y * cw_c + x] for y in range(64) for x in range(cw_c)), 0)
            self.assertEqual(max(cbody[y * cw_c + x] for y in range(ch_c - 64, ch_c) for x in range(cw_c)), 0)
            self.assertEqual(max(cbody[y * cw_c + x] for y in range(ch_c) for x in range(64)), 0)
            self.assertEqual(max(cbody[y * cw_c + x] for y in range(ch_c) for x in range(cw_c - 64, cw_c)), 0)

    def test_03_align_guide_and_composite_roi_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            src = p / "src_2k_ish.mp4"
            _make_av_mp4(self.ffmpeg, src, w=960, h=544, fps=25, dur=0.5, channels=2)
            aligned = duet_roi.ensure_aligned_pass2_guide(self.ffmpeg, src, "640x368", 24.0, p / "cache")
            self.assertNotEqual(aligned, src)
            self.assertEqual(probe_video_dimensions(self.ffmpeg, aligned), (640, 368))
            # Already aligned returns original file without re-encoding
            same = duet_roi.ensure_aligned_pass2_guide(self.ffmpeg, aligned, "640x368", 24.0, p / "cache")
            self.assertEqual(same, aligned)

            box = duet_roi.compute_roi_box(640, 368, split_x=0.5, side="right", roi_mode="half_split")
            crop_in = p / "crop_in.mp4"
            mask_mp4 = p / "mask.mp4"
            first_jpg = p / "first.jpg"
            duet_roi.prepare_roi_crop_and_mask(self.ffmpeg, aligned, box, 640, 368, 12, 24.0, crop_in, mask_mp4, first_jpg)
            self.assertEqual(probe_video_dimensions(self.ffmpeg, crop_in), (box[2], box[3]))
            self.assertEqual(probe_video_dimensions(self.ffmpeg, mask_mp4), (box[2], box[3]))
            self.assertTrue(first_jpg.is_file() and first_jpg.stat().st_size > 200)

            out_comp = p / "composited.mp4"
            duet_roi.composite_roi_onto_guide(self.ffmpeg, aligned, crop_in, box, out_comp, preserve_base_audio=True)
            self.assertEqual(probe_video_dimensions(self.ffmpeg, out_comp), (640, 368))
            self.assertEqual(probe_video_frames(self.ffmpeg, out_comp), probe_video_frames(self.ffmpeg, aligned))

    def test_04_cross_engine_backend_and_pass2_hash_invariance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx_full, cfg_path = _build_temp_project(root, self.ffmpeg, {"renderer": "14", "backend_m": "minimax", "backend_f": "local"})
            sc = ctx_full.scenes[0]
            # Per-pass explicit override beats inherited dedicated cloud renderer
            self.assertEqual(resolve_task_backend(ctx_full, sc=sc, rspec=ctx_full.video_renderers["14"], pass_num=1), "minimax")
            self.assertEqual(resolve_task_backend(ctx_full, sc=sc, rspec=ctx_full.video_renderers["14"], pass_num=2), "local")

            h_default = compute_project_task_hashes(ctx_full)
            k_p1 = next(k for k in h_default if k.startswith("S01") and "_Pass1" in k)
            k_p2 = next(k for k in h_default if k.startswith("S01") and "_Pass2" in k)
            p1_default = h_default[k_p1][0]
            p2_default = h_default[k_p2][0]

            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw["scenes"][0]["pass2_roi_mode"] = "full"
            cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            h_explicit_full = compute_project_task_hashes(ProjectContext(cfg_path))
            self.assertEqual(h_explicit_full[k_p2][0], p2_default)

            raw["scenes"][0]["pass2_roi_mode"] = "tight_performer"
            cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            h_tight = compute_project_task_hashes(ProjectContext(cfg_path))
            self.assertEqual(h_tight[k_p1][0], p1_default)
            self.assertNotEqual(h_tight[k_p2][0], p2_default)

    def test_05_local_roi_pass2_cross_engine_pipeline_mock(self):
        from types import SimpleNamespace
        from pipeline import stage2_scenes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx, _ = _build_temp_project(
                root, self.ffmpeg,
                {"resolution": "99", "backend_m": "minimax", "backend_f": "local", "pass2_roi_mode": "tight_performer"},
            )
            h_map = compute_project_task_hashes(ctx)
            k_p1 = next(k for k in h_map if k.startswith("S01") and "_Pass1" in k)
            k_p2 = next(k for k in h_map if k.startswith("S01") and "_Pass2" in k)
            state_dict = {"video": {}, "anchors": {}, "images": {}}

            seen_tasks = []
            def _fake_exec(ctx, session, task_cfg, **kwargs):
                seen_tasks.append(dict(task_cfg))
                out_p = ctx.renders_dir / f"{task_cfg['output_filename']}.mp4"
                if "_Pass1" in task_cfg["output_filename"]:
                    # Simulate MiniMax Pass 1 outputting cross-engine 960x544 @ 25fps
                    _make_av_mp4(self.ffmpeg, out_p, w=960, h=544, fps=25, dur=1.2, channels=2)
                else:
                    cw, ch = [int(v) for v in task_cfg["resolution"].split("x")]
                    _make_av_mp4(self.ffmpeg, out_p, w=cw, h=ch, fps=24, dur=1.2, channels=1)
                return SimpleNamespace(success=True, generated_files=[str(out_p)], errors=[]), False, 0.0

            with (
                mock.patch.object(stage2_scenes, "execute_wgp_job", side_effect=_fake_exec),
                mock.patch.object(stage2_scenes, "assemble_progressive_master"),
            ):
                stage2_scenes.run_stage2_scenes(
                    ctx, mock.MagicMock(), state_dict, force_scenes=[], regenerated_anchors=set(), no_queue_sync=True
                )

            self.assertEqual(len(seen_tasks), 2)
            p1_task, exec_task = seen_tasks[0], seen_tasks[1]
            self.assertEqual(p1_task["_backend"], "minimax")
            self.assertEqual(exec_task["video_prompt_type"], "GVA")
            self.assertNotIn("spatial_upsampling", exec_task)
            cw, ch = [int(v) for v in exec_task["resolution"].split("x")]
            self.assertLess(cw, 640)
            self.assertEqual(cw % 16, 0)
            self.assertEqual(ch % 16, 0)

            out_p1 = ctx.renders_dir / f"{k_p1}.mp4"
            out_p2 = ctx.renders_dir / f"{k_p2}.mp4"
            self.assertEqual(probe_video_dimensions(self.ffmpeg, out_p1), (960, 544))
            self.assertTrue(out_p2.is_file())
            self.assertEqual(probe_video_dimensions(self.ffmpeg, out_p2), (640, 368))
            crop_tmp = ctx.renders_dir / f"{exec_task['output_filename']}.mp4"
            self.assertFalse(crop_tmp.exists())

    def test_06_repair_scene_lipsync_clip_stereo_and_api(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx, _ = _build_temp_project(
                root, self.ffmpeg,
                {"mode": "duet_method_a", "resolution": "99", "stem": "female", "stem_m": "male", "stem_f": "female"},
            )
            sc = ctx.scenes[0]
            out_name = "S01_duet_a_640x368"
            active_mp4 = ctx.renders_dir / f"{out_name}.mp4"
            _make_av_mp4(self.ffmpeg, active_mp4, w=640, h=368, fps=24, dur=0.6, channels=2)

            take = duet_roi.repair_scene_lipsync_clip(
                ctx, None, "S01", side="right", roi_mode="half_split", stem="female", backend="local", dummy_render=True
            )
            self.assertTrue(take["take_id"].startswith("take_rep_"))
            self.assertTrue(active_mp4.is_file())
            self.assertEqual(probe_video_dimensions(self.ffmpeg, active_mp4), (640, 368))
            # Stereo duet audio must remain intact after single-side ROI visual repair
            self.assertEqual(_probe_audio_channels(self.ffmpeg, active_mp4), 2)
            manifest = load_takes_manifest(ctx.project_dir)
            self.assertEqual(manifest["scenes"]["S01"]["active_take_id"], take["take_id"])

            # Verify cloud fal.ai Sync-v2 repair routing + stereo audio preservation on duet_method_a
            from types import SimpleNamespace
            from pipeline import wgp_bridge
            fal_calls = []
            def _fake_fal_exec(ctx, session, task_cfg, **kwargs):
                fal_calls.append(dict(task_cfg))
                out_p = ctx.renders_dir / f"{task_cfg['output_filename']}.mp4"
                _make_av_mp4(self.ffmpeg, out_p, w=640, h=368, fps=24, dur=0.6, channels=1)
                return SimpleNamespace(success=True, generated_files=[str(out_p)], errors=[]), False, 0.0
            with mock.patch.object(wgp_bridge, "execute_wgp_job", side_effect=_fake_fal_exec):
                fal_take = duet_roi.repair_scene_lipsync_clip(
                    ctx, mock.MagicMock(), "S01", side="right", roi_mode="tight_performer", backend="fal", dummy_render=False
                )
            self.assertEqual(len(fal_calls), 1)
            self.assertEqual(fal_calls[0]["_fal_spec"]["endpoint"], "fal-ai/sync-lipsync/v2")
            self.assertEqual(fal_calls[0]["_fal_ctx"]["pass2_roi_mode"], "tight_performer")
            self.assertEqual(fal_take["backend"], "fal")
            self.assertEqual(_probe_audio_channels(self.ffmpeg, active_mp4), 2)

            # Verify HTTP API busy lock (409) & async dispatch (200)
            class _H:
                def __init__(self):
                    self.status = 200
                    self.payload = None
                    self.err = None
                def send_json(self, d, status=200):
                    self.status, self.payload = int(status), d
                def send_error_json(self, m, status=400):
                    self.status, self.err = int(status), m

            h_busy = _H()
            with (
                mock.patch.object(server, "PROJECTS_DIR", root),
                mock.patch.object(server, "queue_worker_active", True),
                mock.patch.object(server, "engine_workers", {"local": {"running": True}, "fal": {"running": False}, "minimax": {"running": False}}),
            ):
                duet_roi.handle_repair_lipsync_api(h_busy, server, {"project": "proj", "scene_id": "S01", "backend": "local"})
            self.assertEqual(h_busy.status, 409)

            h_ok = _H()
            started_cmds = []
            def _fake_exec(cmd_args, slug):
                started_cmds.append((cmd_args, slug))
                return 0
            with (
                mock.patch.object(server, "PROJECTS_DIR", root),
                mock.patch.object(server, "render_status", {"running": False}),
                mock.patch.object(server, "queue_worker_active", False),
                mock.patch.object(server, "engine_workers", {"local": {"running": False}, "fal": {"running": False}, "minimax": {"running": False}}),
                mock.patch.object(server, "execute_single_render", side_effect=_fake_exec),
                mock.patch.object(server, "broadcast_log"),
            ):
                duet_roi.handle_repair_lipsync_api(
                    h_ok, server, {"project": "proj", "scene_id": "S01", "side": "right", "roi_mode": "tight_performer", "backend": "fal"}
                )
            self.assertEqual(h_ok.status, 200)
            self.assertTrue(h_ok.payload.get("success"))
            # Verify server_routes dispatches /api/takes/repair_lipsync
            src_routes = Path(WORKSPACE_ROOT / "work" / "ui" / "server_routes.py").read_text(encoding="utf-8")
            self.assertIn("/api/takes/repair_lipsync", src_routes)

    def test_07_frontend_duet_repair_js_node_vm(self):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not installed")
        js_path = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "duet_repair.js"
        script = r"""
        const fs = require("fs");
        const vm = require("vm");
        const els = new Map();
        const mkEl = (tag) => {
          const el = {
            tagName: tag.toUpperCase(), style: {}, dataset: {}, options: [], children: [],
            listeners: {}, className: "", value: "", textContent: "", innerHTML: "",
            disabled: false, parentElement: null,
            appendChild(c) { c.parentElement = this; this.children.push(c); if (c.id) els.set(c.id, c); return c; },
            insertBefore(c) { return this.appendChild(c); },
            addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
            dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); },
          };
          Object.defineProperty(el, "innerHTML", {
            get() { return this._html || ""; },
            set(v) {
              this._html = String(v);
              const ids = [...this._html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
              ids.forEach((id) => {
                if (!els.has(id)) {
                  const child = mkEl("div");
                  child.id = id;
                  child.options = [{ textContent: "" }];
                  els.set(id, child);
                }
              });
            }
          });
          return el;
        };
        const grp = mkEl("div"); grp.id = "videoRendererGroup"; els.set("videoRendererGroup", grp);
        const hint = mkEl("div"); hint.id = "sceneFalHint"; grp.appendChild(hint); els.set("sceneFalHint", hint);
        const tray = mkEl("div"); tray.id = "sceneTakesTray"; els.set("sceneTakesTray", tray);
        const btnWrap = mkEl("div"); tray.appendChild(btnWrap);
        const upBtn = mkEl("button"); upBtn.id = "btnUpscaleTake"; btnWrap.appendChild(upBtn); els.set("btnUpscaleTake", upBtn);
        const sc = { id: "S15", mode: "duet_method_b", start: 40, end: 52, stem_m: "male", stem_f: "female" };
        const ctx = vm.createContext({
          document: {
            getElementById: (id) => els.get(id) || null,
            createElement: mkEl,
          },
          window: {
            state: { currentSlug: "demo", selectedSceneIdx: 0, projectData: { scenes: [sc], models: { default_backend: "minimax" } }, models: {} },
            resolveEffectiveSceneBackend: () => "minimax",
            getEffectiveProjectBackend: () => "minimax",
            recordHistorySnapshot: () => {},
            markProjectDirty: () => {},
            scheduleAutoSave: () => {},
            syncSceneBackendUI: (s) => ctx.window.syncDuetPassControlsUI(s),
          },
          fetch: async () => ({ json: async () => ({ success: true }) }),
          alert: () => {},
        });
        ctx.window.window = ctx.window;
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
        ctx.window.ensureDuetPassControls(grp);
        ctx.window.syncDuetPassControlsUI(sc);
        const selF = els.get("sceneBackendF");
        const selRoi = els.get("scenePass2Roi");
        selF.value = "local"; selF.dispatchEvent({ type: "change", target: selF });
        selRoi.value = "tight_performer"; selRoi.dispatchEvent({ type: "change", target: selRoi });
        if (sc.backend_f !== "local" || sc.pass2_roi_mode !== "tight_performer") {
          throw new Error("Scene Inspector bindings failed: " + JSON.stringify(sc));
        }
        ctx.window.syncTakeRepairUI(sc);
        if (!els.get("btnToggleLipSyncRepair") || !els.get("btnRunLipSyncRepair")) {
          throw new Error("Takes Tray repair UI missing");
        }
        console.log("VM_OK", hint.textContent);
        """
        res = subprocess.run(["node", "-e", script, str(js_path)], capture_output=True, text=True, check=True)
        self.assertIn("VM_OK", res.stdout)
        self.assertIn("Local GVA (Tight ROI", res.stdout)

        # Full DOM + localStorage verification for risky-combo confirmation modal & "don't tell me again"
        modal_script = r"""
        const fs = require("fs");
        const vm = require("vm");
        const els = new Map();
        const store = new Map();
        const localStorage = {
          getItem: (k) => (store.has(k) ? store.get(k) : null),
          setItem: (k, v) => store.set(k, String(v)),
          removeItem: (k) => store.delete(k),
        };
        const mkEl = (tag) => {
          const el = {
            tagName: tag.toUpperCase(), style: {}, dataset: {}, options: [], children: [],
            listeners: {}, className: "", value: "", textContent: "", checked: false,
            disabled: false, parentElement: null,
            appendChild(c) { c.parentElement = this; this.children.push(c); if (c.id) els.set(c.id, c); return c; },
            insertBefore(c) { return this.appendChild(c); },
            addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
            dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); },
          };
          Object.defineProperty(el, "innerHTML", {
            get() { return this._html || ""; },
            set(v) {
              this._html = String(v);
              const ids = [...this._html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
              ids.forEach((id) => {
                if (!els.has(id)) {
                  const child = mkEl("div");
                  child.id = id;
                  child.options = [{ textContent: "" }];
                  els.set(id, child);
                }
              });
            }
          });
          return el;
        };
        const body = mkEl("body");
        const grp = mkEl("div"); grp.id = "videoRendererGroup"; els.set("videoRendererGroup", grp);
        const hint = mkEl("div"); hint.id = "sceneFalHint"; grp.appendChild(hint); els.set("sceneFalHint", hint);
        const tray = mkEl("div"); tray.id = "sceneTakesTray"; els.set("sceneTakesTray", tray);
        const upBtn = mkEl("button"); upBtn.id = "btnUpscaleTake"; tray.appendChild(upBtn); els.set("btnUpscaleTake", upBtn);
        const sc = { id: "S15", mode: "duet_method_b", start: 40, end: 50, stem_m: "male", stem_f: "female", backend_m: "minimax", backend_f: "local" };
        let repairPosts = 0;
        const ctx = vm.createContext({
          document: {
            body,
            getElementById: (id) => els.get(id) || null,
            createElement: mkEl,
            addEventListener: () => {},
          },
          localStorage,
          window: {
            state: { currentSlug: "demo", selectedSceneIdx: 0, projectData: { scenes: [sc], models: { default_backend: "minimax" } }, models: {} },
            resolveEffectiveSceneBackend: (s) => s.backend || "minimax",
            getEffectiveProjectBackend: () => "minimax",
            recordHistorySnapshot: () => {},
            markProjectDirty: () => {},
            showToast: () => {},
            syncSceneBackendUI: (s) => ctx.window.syncDuetPassControlsUI(s),
          },
          fetch: async () => { repairPosts++; return { ok: true, json: async () => ({ success: true }) }; },
          alert: () => {},
        });
        ctx.window.window = ctx.window;
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
        ctx.window.ensureDuetPassControls(grp);
        ctx.window.syncDuetPassControlsUI(sc);

        // Rule evaluator checks
        const soloSafe = { id: "S01", mode: "male_solo", stem: "male", backend: "minimax" };
        if (ctx.window.evaluateComboWarnings(soloSafe).length !== 0) throw new Error("Expected no warning on minimax solo");
        const endWarn = ctx.window.evaluateComboWarnings(soloSafe, { anchor_end: "A02.jpg" });
        if (!endWarn.some((w) => w.id === "cloud_end_anchor_audio")) throw new Error("Expected cloud_end_anchor_audio warning");
        const falTight = ctx.window.evaluateComboWarnings(sc, { backend_f: "fal", pass2_roi_mode: "tight_performer" });
        if (falTight.length !== 0) throw new Error("Expected fal Sync-v2 ROI not to warn");

        // 1. Selecting tight_performer opens modal; Cancel reverts value and skips mutation
        const selRoi = els.get("scenePass2Roi");
        selRoi.value = "tight_performer";
        selRoi.dispatchEvent({ type: "change", target: selRoi });
        const modal = els.get("comboWarningModal");
        if (!modal || modal.style.display !== "flex") throw new Error("Expected comboWarningModal to open");
        els.get("btnComboWarnCancel").dispatchEvent({ type: "click" });
        if (sc.pass2_roi_mode !== undefined) throw new Error("Expected cancel to leave pass2_roi_mode unset");
        if (selRoi.value !== "") throw new Error("Expected cancel to revert dropdown value, got: " + selRoi.value);

        // 2. Confirm with "Don't tell me again" ticked applies and saves dismissal for local_tight_roi
        selRoi.value = "tight_performer";
        selRoi.dispatchEvent({ type: "change", target: selRoi });
        els.get("comboWarnDontAskAgain").checked = true;
        els.get("btnComboWarnConfirm").dispatchEvent({ type: "click" });
        if (sc.pass2_roi_mode !== "tight_performer") throw new Error("Expected confirm to apply tight_performer");
        const saved = JSON.parse(store.get("mvg_dismissed_combo_warnings_v1") || "[]");
        if (!saved.includes("local_tight_roi")) throw new Error("Expected local_tight_roi persisted in localStorage");

        // 3. Re-selecting local_tight_roi no longer prompts, but selecting minimax_pass2_roi still prompts
        delete sc.pass2_roi_mode;
        selRoi.value = "tight_performer";
        selRoi.dispatchEvent({ type: "change", target: selRoi });
        if (modal.style.display === "flex" || sc.pass2_roi_mode !== "tight_performer") {
          throw new Error("Expected dismissed warning not to re-prompt");
        }
        const selF = els.get("sceneBackendF");
        selF.value = "minimax";
        selF.dispatchEvent({ type: "change", target: selF });
        if (modal.style.display !== "flex") throw new Error("Expected distinct rule minimax_pass2_roi to still warn");
        els.get("btnComboWarnCancel").dispatchEvent({ type: "click" });
        if (sc.backend_f !== "local") throw new Error("Expected cancel to keep backend_f=local");

        // 4. Repair Run button is gated by modal until confirmed
        ctx.window.syncTakeRepairUI(sc);
        els.get("repBackend").value = "local";
        els.get("repRoiMode").value = "half_split";
        els.get("btnRunLipSyncRepair").dispatchEvent({ type: "click" });
        if (modal.style.display !== "flex" || repairPosts !== 0) throw new Error("Expected repair click to pause on warning");
        els.get("btnComboWarnCancel").dispatchEvent({ type: "click" });
        if (repairPosts !== 0) throw new Error("Expected cancel not to trigger repair POST");
        console.log("MODAL_WARN_OK");
        """
        res2 = subprocess.run(["node", "-e", modal_script, str(js_path)], capture_output=True, text=True, check=True)
        self.assertIn("MODAL_WARN_OK", res2.stdout)

    def test_08_full_frame_pass2_start_image_alignment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx, _ = _build_temp_project(
                root, self.ffmpeg,
                {"resolution": "6", "backend_m": "minimax", "backend_f": "local", "pass2_roi_mode": "full"},
            )
            anchor_jpg = ctx.anchors_dir / "A01.jpg"
            # 1) Cross-engine cloud Pass 1 (e.g., 2560x1440 / 16:9 @ 24fps simulated at 1280x720) without embedded Wan2GP metadata
            cloud_p1 = ctx.renders_dir / "S01_Pass1_Left_1920x1088.mp4"
            _make_av_mp4(self.ffmpeg, cloud_p1, w=1280, h=720, fps=24, dur=0.5, channels=2)
            sc = dict(ctx.scenes[0], pass2_roi_mode="full")
            exec_task = {
                "output_filename": "S01_Pass2_Final_1920x1088",
                "image_start": str(anchor_jpg),
                "video_guide": str(cloud_p1),
                "resolution": "1920x1088",
                "video_length": 12,
            }
            updated, meta = duet_roi.prepare_local_roi_pass2_task(
                ctx, cloud_p1, exec_task, sc, "1920x1088", 12, 24.0
            )
            self.assertIsNone(meta)
            aligned_path = Path(updated["video_guide"])
            self.assertNotEqual(aligned_path, cloud_p1)
            self.assertEqual(probe_video_dimensions(self.ffmpeg, aligned_path), (1920, 1088))
            start_img = Path(updated["image_start"])
            self.assertNotEqual(start_img, anchor_jpg)
            self.assertTrue(start_img.is_file() and start_img.name.startswith(".guide_start_"))
            self.assertEqual(probe_video_dimensions(self.ffmpeg, start_img), (1920, 1088))

            # 2) Same-dimension cloud Pass 1 (no embedded Wan2GP metadata) still extracts Frame 0 so framing matches guide
            cloud_same_p1 = ctx.renders_dir / "S01_Pass1_SameDim.mp4"
            _make_av_mp4(self.ffmpeg, cloud_same_p1, w=640, h=368, fps=24, dur=0.5, channels=2)
            updated_same, meta_same = duet_roi.prepare_local_roi_pass2_task(
                ctx, cloud_same_p1, dict(exec_task, video_guide=str(cloud_same_p1), resolution="640x368"), sc, "640x368", 12, 24.0
            )
            self.assertIsNone(meta_same)
            self.assertEqual(Path(updated_same["video_guide"]), cloud_same_p1)
            self.assertNotEqual(Path(updated_same["image_start"]), anchor_jpg)
            self.assertTrue(Path(updated_same["image_start"]).is_file())

            # 3) Native local Pass 1 (640x368 @ 24fps with embedded Wan2GP comment JSON) preserves original anchor image_start
            local_p1 = ctx.renders_dir / "S01_Pass1_Local.mp4"
            subprocess.run(
                [
                    str(self.ffmpeg), "-y", "-i", str(cloud_same_p1),
                    "-c", "copy", "-metadata", 'comment={"model_type":"minimax_h3_fl2va_pruned_pdd"}', str(local_p1),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            updated_local, meta_local = duet_roi.prepare_local_roi_pass2_task(
                ctx, local_p1, dict(exec_task, video_guide=str(local_p1), resolution="640x368"), sc, "640x368", 12, 24.0
            )
            self.assertIsNone(meta_local)
            self.assertEqual(Path(updated_local["video_guide"]), local_p1)
            self.assertEqual(Path(updated_local["image_start"]), anchor_jpg)


if __name__ == "__main__":
    unittest.main()
