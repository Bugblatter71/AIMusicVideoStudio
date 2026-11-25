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
    (proj_dir / "work" / "renders").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "anchors").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "audio_stems").mkdir(parents=True, exist_ok=True)
    (proj_dir / "work" / "jobs").mkdir(parents=True, exist_ok=True)
    anchor_jpg = proj_dir / "work" / "anchors" / "A01.jpg"
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
                "renders": "work/renders",
                "anchors": "work/anchors",
                "jobs": "work/jobs",
                "audio_slices": "work/audio",
                "masks": "work/masks",
                "trimmed": "work/trimmed",
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
        "anchors": [{"id": "A01", "file": "work/anchors/A01.jpg"}],
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
            state: {
              currentSlug: "demo",
              selectedSceneIdx: 0,
              projectData: { scenes: [sc], models: { default_backend: "minimax", default_video_renderer: 1 } },
              models: {
                video_renderers: { "1": { name: "FL2VA" }, "9": { name: "Ref2VA" } },
                fal_video_renderers: { fal_kling_26: { name: "Kling 2.6", backend: "fal" } },
                minimax_video_renderers: {},
              },
            },
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
        const rSelM = els.get("sceneRendererM");
        const rSelF = els.get("sceneRendererF");
        const selF = els.get("sceneBackendF");
        const selRoi = els.get("scenePass2Roi");
        if (!rSelM || !rSelF) throw new Error("Pass-level model selectors missing");
        rSelM.value = "9"; rSelM.dispatchEvent({ type: "change", target: rSelM });
        rSelF.value = "fal_kling_26"; rSelF.dispatchEvent({ type: "change", target: rSelF });
        if (sc.renderer_m !== 9 || sc.renderer_f !== "fal_kling_26") {
          throw new Error("Pass model bindings failed: " + JSON.stringify(sc));
        }
        if (ctx.window.resolveDuetPassBackend(sc, 2) !== "fal" || !selF.disabled || selF.value !== "fal") {
          throw new Error("Dedicated cloud pass model must auto-lock Pass 2 engine to fal");
        }
        rSelF.value = ""; rSelF.dispatchEvent({ type: "change", target: rSelF });
        if (sc.renderer_f !== undefined || selF.disabled) {
          throw new Error("Clearing Pass 2 model should delete renderer_f and unlock backend");
        }
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

    def test_09_resolve_roi_border_px_ltx2_mvga_and_pass2_meta(self):
        ltx_cfg = {"model_type": "ltx2_25_22B_distilled", "video_prompt_type": "MVGA"}
        h3_cfg = {"model_type": "minimax_h3_fl2va_pruned_pdd", "video_prompt_type": "GVA"}
        self.assertEqual(duet_roi.resolve_roi_border_px(960, 1088, ltx_cfg), 128)
        self.assertEqual(duet_roi.resolve_roi_border_px(960, 1088, h3_cfg), 64)
        self.assertEqual(duet_roi.resolve_roi_border_px(544, 544, ltx_cfg), 64)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx, _ = _build_temp_project(
                root, self.ffmpeg,
                {"resolution": "6", "split_x": 0.48, "pass2_roi_mode": "half_split"},
            )
            p1_hd = ctx.renders_dir / "S01_Pass1_1920x1088.mp4"
            _make_av_mp4(self.ffmpeg, p1_hd, w=1920, h=1088, fps=24, dur=0.15, channels=2)
            sc = dict(ctx.scenes[0], split_x=0.48, pass2_roi_mode="half_split")
            base_task = {
                "output_filename": "S01_Pass2_Final_1920x1088",
                "image_start": str(ctx.anchors_dir / "A01.jpg"),
                "video_guide": str(p1_hd),
                "resolution": "1920x1088",
                "video_length": 9,
            }
            _, meta_ltx = duet_roi.prepare_local_roi_pass2_task(
                ctx, p1_hd, dict(base_task, **ltx_cfg), sc, "1920x1088", 9, 24.0,
            )
            _, meta_h3 = duet_roi.prepare_local_roi_pass2_task(
                ctx, p1_hd, dict(base_task, **h3_cfg), sc, "1920x1088", 9, 24.0,
            )
            self.assertEqual(meta_ltx["border_px"], 128)
            self.assertEqual(meta_h3["border_px"], 64)

    def test_10_duet_repair_js_ltx2_mvga_warning_and_split_overlay(self):
        js_path = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "duet_repair.js"
        script = r"""
        const fs = require("fs");
        const vm = require("vm");
        const els = new Map();
        const mkEl = (tag) => {
          const el = {
            tagName: tag.toUpperCase(), style: {}, dataset: {}, options: [], children: [],
            listeners: {}, className: "", value: "", textContent: "",
            addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
            dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); },
            appendChild(c) { c.parentElement = this; this.children.push(c); if (c.id) els.set(c.id, c); return c; },
            insertBefore(c) { return this.appendChild(c); },
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
        const overlay = mkEl("div"); overlay.id = "clipSplitOverlay"; overlay.style.display = "none"; els.set("clipSplitOverlay", overlay);
        const splitInp = mkEl("input"); splitInp.id = "sceneSplitX"; splitInp.value = "0.48"; els.set("sceneSplitX", splitInp);
        const splitRng = mkEl("input"); splitRng.id = "sceneSplitXRange"; splitRng.value = "0.48"; els.set("sceneSplitXRange", splitRng);

        const scLtx = { id: "S12a", mode: "duet_method_b", split_x: 0.48, start: 67.3, end: 74.6, renderer: 14, stem_m: "male", stem_f: "female" };
        const scStereo = { id: "S06", mode: "duet_method_a", split_x: 0.5, start: 30, end: 36, renderer: 14 };
        const ctx = vm.createContext({
          document: { getElementById: (id) => els.get(id) || null, createElement: mkEl },
          window: {
            state: {
              currentSlug: "demo",
              selectedSceneIdx: 0,
              projectData: { scenes: [scLtx, scStereo], models: { default_backend: "local", default_video_renderer: 14 } },
              models: {
                video_renderers: {
                  "1": { name: "FL2VA", model_type: "minimax_h3_fl2va_pruned_pdd", pass2_video_prompt_type: "GVA" },
                  "9": { name: "Ref2VA", model_type: "minimax_h3_ref2va_pruned", pass2_video_prompt_type: "GVA" },
                  "14": { name: "LTX-2.5", model_type: "ltx2_25_22B_distilled", pass2_video_prompt_type: "MVGA" },
                },
                fal_video_renderers: {},
                minimax_video_renderers: {},
              },
            },
            resolveEffectiveSceneBackend: () => "local",
            getEffectiveProjectBackend: () => "local",
            syncSceneBackendUI: (s) => ctx.window.syncDuetPassControlsUI(s),
          },
        });
        ctx.window.window = ctx.window;
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);

        // Full-frame inherited #14 triggers ltx2_duet_mvga_seam; explicit #9 or duet_method_a or cloud clears it
        const wFull = ctx.window.evaluateComboWarnings(scLtx);
        if (!wFull.some((w) => w.id === "ltx2_duet_mvga_seam")) throw new Error("Expected ltx2_duet_mvga_seam on Full Frame LTX-2 #14");
        const wInheritDef = ctx.window.evaluateComboWarnings({ id: "S12b", mode: "duet_method_b", stem_m: "m", stem_f: "f" });
        if (!wInheritDef.some((w) => w.id === "ltx2_duet_mvga_seam")) throw new Error("Expected inherited default #14 to warn");
        if (ctx.window.evaluateComboWarnings(scLtx, { renderer_f: 9 }).some((w) => w.id === "ltx2_duet_mvga_seam")) throw new Error("H3 #9 should not warn MVGA");
        if (ctx.window.evaluateComboWarnings(scStereo).some((w) => w.id === "ltx2_duet_mvga_seam")) throw new Error("duet_method_a should not warn MVGA");
        const wRep = ctx.window.evaluateComboWarnings(scLtx, null, { isRepair: true, backend: "local", roi_mode: "half_split", side: "right" });
        if (!wRep.some((w) => w.id === "ltx2_duet_mvga_seam")) throw new Error("Local LTX-2 ROI repair should warn MVGA");

        ctx.window.ensureDuetPassControls(grp);
        ctx.window.syncDuetPassControlsUI(scLtx);
        if (!hint.textContent.includes("Local MVGA (Full Mask, $0)") || !hint.textContent.includes("MVGA blends ~10%")) {
          throw new Error("Unexpected sceneFalHint: " + hint.textContent);
        }
        if (overlay.style.display !== "block" || !overlay.innerHTML.includes("Split X 48%") || !overlay.innerHTML.includes("MVGA ~10% blend")) {
          throw new Error("Unexpected overlay on S12a: " + overlay.innerHTML);
        }
        splitInp.value = "0.58";
        splitInp.dispatchEvent({ type: "input", target: splitInp });
        if (!overlay.innerHTML.includes("Split X 58%")) throw new Error("Split X input did not live-update overlay");

        ctx.window.state.selectedSceneIdx = 1;
        ctx.window.syncDuetPassControlsUI(scStereo);
        if (overlay.style.display !== "none") throw new Error("Expected Stereo Duet to hide Split X overlay");
        console.log("LTX2_MVGA_JS_OK");
        """
        res = subprocess.run(["node", "-e", script, str(js_path)], capture_output=True, text=True, check=True)
        self.assertIn("LTX2_MVGA_JS_OK", res.stdout)

    def test_11_interactive_video_roi_overlay_master_and_clip_dragging(self):
        duet_js = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "duet_repair.js"
        overlay_js = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "video_roi_overlay.js"
        py_half = duet_roi.compute_roi_box(1920, 1088, split_x=0.48, side="right", roi_mode="half_split")
        py_tight = duet_roi.compute_roi_box(1920, 1088, split_x=0.48, side="right", roi_mode="tight_performer")
        py_cust = duet_roi.compute_roi_box(1920, 1088, split_x=0.48, side="right", roi_mode="custom", custom_box=[0.40, 0.08, 0.96, 0.88])

        script = r"""
        const fs = require("fs");
        const vm = require("vm");
        const els = new Map();
        const docListeners = {};
        const mkEl = (tag, id = "") => {
          const el = {
            tagName: tag.toUpperCase(), id, style: {}, dataset: {}, options: [], children: [],
            listeners: {}, className: "", value: "", textContent: "", paused: true, currentTime: 0,
            videoWidth: 1920, videoHeight: 1088,
            addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
            dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); },
            appendChild(c) { c.parentElement = this; this.children.push(c); if (c.id) els.set(c.id, c); return c; },
            insertBefore(c) { return this.appendChild(c); },
            getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 566.67 }),
          };
          Object.defineProperty(el, "innerHTML", {
            get() { return this._html || ""; },
            set(v) {
              this._html = String(v);
              const ids = [...this._html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
              ids.forEach((cid) => {
                if (!els.has(cid)) {
                  const child = mkEl("div", cid);
                  child.options = [{ textContent: "" }];
                  els.set(cid, child);
                }
              });
            }
          });
          if (id) els.set(id, el);
          return el;
        };
        const body = mkEl("body", "body");
        mkEl("div", "videoRendererGroup");
        mkEl("div", "sceneFalHint");
        mkEl("div", "clipSplitOverlay");
        mkEl("div", "masterRoiOverlay");
        mkEl("video", "clipVideoPlayer");
        const masterVid = mkEl("video", "masterVideoPlayer");
        mkEl("audio", "audioElement");
        const toggleBtn = mkEl("button", "btnToggleMasterRoiOverlay");
        mkEl("input", "sceneSplitX").value = "0.48";
        mkEl("input", "sceneSplitXRange").value = "0.48";
        mkEl("span", "sceneSplitXVal");
        mkEl("select", "scenePass2Roi");
        mkEl("div", "scenePass2RoiSideWrap");
        mkEl("div", "scenePass2RoiBoxWrap");
        mkEl("input", "scenePass2RoiBox");

        const sc0 = { id: "S01", mode: "solo", start: 0, end: 30, resolution: "6" };
        const sc1 = { id: "S12a", mode: "duet_method_b", split_x: 0.48, pass2_roi_mode: "half_split", start: 67.3, end: 74.6, renderer: 14, resolution: "6", stem_m: "m", stem_f: "f" };
        let editCommitted = 0;

        const ctx = vm.createContext({
          document: {
            body,
            getElementById: (id) => els.get(id) || null,
            createElement: (t) => mkEl(t),
            addEventListener(ev, fn) { (docListeners[ev] ||= []).push(fn); },
          },
          window: {
            state: {
              currentSlug: "demo",
              selectedSceneIdx: 0,
              playheadTime: 10.0,
              projectData: { scenes: [sc0, sc1], models: { default_backend: "local", default_video_renderer: 14, default_video_resolution: "6" } },
              models: {
                video_renderers: { "14": { name: "LTX-2.5", model_type: "ltx2_25_22B_distilled", pass2_video_prompt_type: "MVGA" } },
                fal_video_renderers: {},
                minimax_video_renderers: {},
              },
            },
            resolveEffectiveSceneBackend: () => "local",
            getEffectiveProjectBackend: () => "local",
            selectScene: (idx) => { ctx.window.state.selectedSceneIdx = idx; },
            beginEditSession: () => {},
            commitEditSession: () => { editCommitted++; },
            markProjectDirty: () => {},
            addEventListener: () => {},
          },
          setTimeout: (fn) => fn(),
        });
        ctx.window.window = ctx.window;
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
        vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

        // 1. Verify JS computeRoiBoxJS matches Python compute_roi_box pixel-for-pixel
        const bHalf = ctx.window.computeRoiBoxJS(1920, 1088, 0.48, "right", "half_split");
        const bTight = ctx.window.computeRoiBoxJS(1920, 1088, 0.48, "right", "tight_performer");
        const bCust = ctx.window.computeRoiBoxJS(1920, 1088, 0.48, "right", "custom", [0.40, 0.08, 0.96, 0.88]);
        console.log("BOXES", JSON.stringify([[bHalf.cx, bHalf.cy, bHalf.cw, bHalf.ch], [bTight.cx, bTight.cy, bTight.cw, bTight.ch], [bCust.cx, bCust.cy, bCust.cw, bCust.ch]]));

        // 2. When Master Video plays into S12a (t=70.0s), masterRoiOverlay auto-displays S12a Split X + Half Split ROI box
        masterVid.paused = false;
        masterVid.currentTime = 70.0;
        masterVid.dispatchEvent({ type: "timeupdate" });
        const masterOv = els.get("masterRoiOverlay");
        if (masterOv.style.display !== "block" || !masterOv.innerHTML.includes("S12a Split X 48%") || !masterOv.innerHTML.includes("Half Split ROI")) {
          throw new Error("Master overlay failed to track S12a at t=70s: " + masterOv.innerHTML);
        }

        // 3. Simulate dragging Split X handle on Master Video to x=320px / 1000px = 0.32
        const fakeStage = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 566.67 }) };
        const splitHandle = {
          dataset: { handle: "split_x", target: "master" },
          closest: (sel) => (sel === ".roi-drag-handle" ? splitHandle : fakeStage),
        };
        masterOv.dispatchEvent({ type: "mousedown", target: splitHandle, clientX: 480, clientY: 200 });
        (docListeners.mousemove || []).forEach((fn) => fn({ clientX: 320, clientY: 200 }));
        (docListeners.mouseup || []).forEach((fn) => fn({}));
        if (sc1.split_x !== 0.32 || String(els.get("sceneSplitX").value) !== "0.32" || ctx.window.state.selectedSceneIdx !== 1) {
          throw new Error("Dragging Split X on Master failed: " + JSON.stringify(sc1));
        }

        // 4. Simulate dragging ROI box left edge (box_w) right by +100px (+0.10) -> auto-promotes to custom ROI box & hides unused Split X guide
        const boxHandle = {
          dataset: { handle: "box_w", target: "master" },
          closest: (sel) => (sel === ".roi-drag-handle" ? boxHandle : fakeStage),
        };
        masterOv.dispatchEvent({ type: "mousedown", target: boxHandle, clientX: 320, clientY: 200 });
        (docListeners.mousemove || []).forEach((fn) => fn({ clientX: 420, clientY: 200 }));
        (docListeners.mouseup || []).forEach((fn) => fn({}));
        if (sc1.pass2_roi_mode !== "custom" || !Array.isArray(sc1.pass2_roi_box) || sc1.pass2_roi_box[0] <= 0.35) {
          throw new Error("Dragging ROI box edge failed to promote to custom box: " + JSON.stringify(sc1));
        }
        if (masterOv.innerHTML.includes("Split X") || !masterOv.innerHTML.includes("S12a · Custom ROI")) {
          throw new Error("Custom ROI mode should show only bounding box with scene prefix, not Split X: " + masterOv.innerHTML);
        }

        // 5. Toggle Guides Off hides overlays; clicking again restores them
        toggleBtn.dispatchEvent({ type: "click" });
        if (masterOv.style.display !== "none" || !toggleBtn.textContent.includes("Off")) {
          throw new Error("Expected Guides toggle to hide master overlay");
        }
        toggleBtn.dispatchEvent({ type: "click" });
        if (masterOv.style.display !== "block" || !toggleBtn.textContent.includes("On")) {
          throw new Error("Expected Guides toggle to restore master overlay");
        }
        console.log("OVERLAY_DRAG_OK", editCommitted);
        """
        res = subprocess.run(["node", "-e", script, str(duet_js), str(overlay_js)], capture_output=True, text=True, check=True)
        self.assertIn("OVERLAY_DRAG_OK", res.stdout)
        box_line = next(line for line in res.stdout.splitlines() if line.startswith("BOXES "))
        js_boxes = json.loads(box_line[len("BOXES "):])
        self.assertEqual(tuple(js_boxes[0]), py_half)
        self.assertEqual(tuple(js_boxes[1]), py_tight)
        self.assertEqual(tuple(js_boxes[2]), py_cust)


if __name__ == "__main__":
    unittest.main()
