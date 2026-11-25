"""Offline unit & integration tests for Plan 4: Character Masking, Extraction & MSR Auto-Skip."""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_ROOT / "work"
for p in (WORK_DIR, WORK_DIR / "ui"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from pipeline.anchor_takes import prepare_targeted_anchor_queue_items, run_character_ref_generation
from pipeline.char_mask_worker import box_xyxy_to_xywh_norm, build_sam3_prompt_requests, find_wgp_ckpt_status
from pipeline.char_masking import build_char_isolate_prompt, handle_char_masking_api, resolve_proj_rel_safe, resolve_ref_mask_state
from pipeline.context import ProjectContext
from pipeline.ltx_refs import is_ref_premasked_white, prepare_ltx2_msr_task, resolve_msr_remove_bg_flag
from pipeline.media_utils import resolve_ffmpeg_path
from pipeline.stage2_hashes import compute_project_task_hashes
import server


def _minimal_png_bytes(width: int = 8, height: int = 8, fill_byte: int = 255) -> bytes:
    """Builds a valid RGB PNG byte stream using only Python stdlib."""
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes([fill_byte, fill_byte, fill_byte]) * width
    idat = zlib.compress(row * height)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _write_wav(path: Path, dur_s: float = 4.0, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x01\x00" * int(dur_s * sr))


def _make_color_jpg(ffmpeg: Path, path: Path, color: str, size: str = "160x120") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:d=0.1", "-vframes", "1", "-q:v", "2", str(path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class _DummyHandler:
    def __init__(self):
        self.status = None
        self.payload = None

    def send_json(self, data, status=200):
        self.status = status
        self.payload = data

    def send_error_json(self, message, status=400):
        self.status = int(status)
        self.payload = {"error": message}


class TestCharacterMasking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        cls.profiles = json.loads((WORKSPACE_ROOT / "model_profiles.json").read_text(encoding="utf-8"))

    def _make_proj(self, root: Path, slug: str = "mask_proj") -> tuple[Path, Path]:
        pdir = root / slug
        for sub in ("audio", "references/maya", "work/anchors", "work/renders", "work/jobs"):
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        (pdir / "local_config.json").write_text("{}", encoding="utf-8")
        _write_wav(pdir / "audio" / "master.wav", 6.0)
        _make_color_jpg(self.ffmpeg, pdir / "work" / "anchors" / "A01_tunnel.jpg", "navy", "320x180")
        _make_color_jpg(self.ffmpeg, pdir / "references" / "maya" / "crowd.jpg", "red", "200x160")

        cfg = {
            "project": {
                "name": "Mask Test",
                "fps": 24,
                "master_audio": "audio/master.wav",
                "master_output": "MaskTest.mp4",
                "directories": {
                    "anchors": "work/anchors", "audio_slices": "work/audio", "masks": "work/masks",
                    "jobs": "work/jobs", "renders": "work/renders", "trimmed": "work/trimmed",
                },
            },
            "models": {
                "default_anchor_renderer": 1,
                "default_video_renderer": 19,
                "anchor_renderers": self.profiles["anchor_renderers"],
                "video_renderers": self.profiles["video_renderers"],
            },
            "characters": {
                "maya": {
                    "id": "maya",
                    "name": "Maya",
                    "kind": "character",
                    "default_look": "default",
                    "avatar": "references/maya/crowd.jpg",
                    "physical_traits": "young woman with blonde curls",
                    "looks": {
                        "default": {
                            "tag": "@maya",
                            "name": "Default",
                            "description": "gray laced dress with white collar",
                            "image_refs": ["references/maya/crowd.jpg"],
                            "generated_takes": [],
                        }
                    },
                }
            },
            "anchors": [{"filename": "A01_tunnel.jpg", "seed": 1001, "prompt": "dark brick tunnel"}],
            "scenes": [
                {
                    "id": "S01",
                    "start": 0.0,
                    "end": 5.0,
                    "mode": "male_solo",
                    "anchor": "A01_tunnel.jpg",
                    "stem": "master",
                    "renderer": 19,
                    "prompt": "@maya walks carefully down the corridor",
                }
            ],
        }
        cfg_file = pdir / f"{slug}.json"
        cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return pdir, cfg_file

    def test_sam3_prompt_sequence_and_box_normalization(self):
        xywh = box_xyxy_to_xywh_norm([100, 50, 300, 450], 1000, 500)
        self.assertEqual(xywh, [0.1, 0.1, 0.2, 0.8])

        reqs = build_sam3_prompt_requests(
            session_id="sess1",
            box_norm_xyxy=[0.2, 0.1, 0.6, 0.9],
            text_prompt="woman in gray dress",
            points=[[0.4, 0.5], [0.8, 0.5]],
            point_labels=[1, 0],
            obj_id=7,
        )
        # Seed uses bounding_boxes ONLY (no points); refine uses obj_id + points ONLY (no text/boxes)
        self.assertIn("bounding_boxes", reqs["seed"])
        self.assertNotIn("points", reqs["seed"])
        self.assertNotIn("text", reqs["seed"])
        self.assertEqual(reqs["refine"]["obj_id"], 7)
        self.assertNotIn("text", reqs["refine"])
        self.assertNotIn("bounding_boxes", reqs["refine"])
        self.assertEqual(reqs["refine"]["point_labels"], [1, 0])

        # Fallback text seed when no box provided
        reqs_txt = build_sam3_prompt_requests(text_prompt="singer")
        self.assertEqual(reqs_txt["seed"]["text"], "singer")
        self.assertIsNone(reqs_txt["refine"])

    def test_security_and_ckpt_status_paths(self):
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td)
            (pdir / "references" / "maya").mkdir(parents=True)
            f = pdir / "references" / "maya" / "a.jpg"
            f.write_bytes(b"123")
            self.assertIsNotNone(resolve_proj_rel_safe(pdir, "references/maya/a.jpg"))
            self.assertIsNone(resolve_proj_rel_safe(pdir, "../secret.txt"))
            self.assertIsNone(resolve_proj_rel_safe(pdir, "references/../../etc/passwd"))

            status = find_wgp_ckpt_status(pdir)
            self.assertFalse(any(status.values()))
            (pdir / "ckpts" / "buffalo_l").mkdir(parents=True)
            (pdir / "ckpts" / "buffalo_l" / "person_yolov8m-seg.safetensors").write_bytes(b"0")
            self.assertTrue(find_wgp_ckpt_status(pdir)["yolo_person"])

    def test_api_mask_save_toggle_and_msr_autoskip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdir, cfg_file = self._make_proj(root)
            hashes0 = compute_project_task_hashes(ProjectContext(str(cfg_file)))
            s01_k = next(k for k in hashes0 if k.startswith("S01_"))
            h_before = hashes0[s01_k][0]

            with mock.patch.object(server, "compute_fast_project_queue", return_value=[]), \
                 mock.patch.object(server, "render_status", {"running": False}), \
                 mock.patch("pipeline.char_masking._is_local_gpu_busy", return_value=False):

                def _fake_worker(srv_obj, payload, force_cpu=False):
                    for k in ("out_mask_path", "out_rgba_path", "out_cutout_path"):
                        outp = payload.get(k)
                        if outp:
                            Path(outp).parent.mkdir(parents=True, exist_ok=True)
                            Path(outp).write_bytes(_minimal_png_bytes(fill_byte=255 if "cutout" in k else 200))
                    return {"success": True, "width": 200, "height": 160, "fg_ratio": 0.61}

                with mock.patch("pipeline.char_masking._run_mask_worker", side_effect=_fake_worker):
                    h1 = _DummyHandler()
                    b64_png = "data:image/png;base64," + base64.b64encode(_minimal_png_bytes()).decode("ascii")
                    handle_char_masking_api(h1, "mask-save", pdir, "mask_proj", {
                        "entity_id": "maya",
                        "look_id": "default",
                        "rel_path": "references/maya/crowd.jpg",
                        "mask_data_url": b64_png,
                        "method": "yolo_person",
                    })
                    self.assertEqual(h1.status, 200)
                    self.assertTrue(h1.payload["success"])
                    cut_rel = h1.payload["cutout_rel"]
                    self.assertEqual(h1.payload["characters"]["maya"]["looks"]["default"]["image_refs"], [cut_rel])
                    self.assertTrue((pdir / cut_rel).is_file())
                    self.assertTrue((pdir / h1.payload["mask_rel"]).is_file())
                    self.assertTrue((pdir / h1.payload["rgba_rel"]).is_file())

                    # Pipeline & hash checks when pre-masked white cutout is active
                    ctx = ProjectContext(str(cfg_file))
                    self.assertTrue(is_ref_premasked_white(ctx, pdir / cut_rel))
                    sc = ctx.scenes[0]
                    rspec = ctx.video_renderers["19"]
                    self.assertEqual(resolve_msr_remove_bg_flag(ctx, sc, rspec, [pdir / cut_rel]), 0)
                    task = prepare_ltx2_msr_task(
                        ctx, {"prompt": sc["prompt"], "resolution": "1280x704", "audio_guide": str(pdir / "audio/master.wav")},
                        rspec=rspec, sc=sc, raw_prompt=sc["prompt"],
                        image_start=pdir / "work/anchors/A01_tunnel.jpg", image_refs=None, has_end=False,
                        video_guide=None, video_mask=None,
                    )
                    self.assertEqual(task["remove_background_images_ref"], 0)
                    self.assertEqual(task["video_prompt_type"], "KI")
                    # Explicit override sc["msr_remove_bg"] = True wins
                    self.assertEqual(resolve_msr_remove_bg_flag(ctx, {**sc, "msr_remove_bg": True}, rspec, [pdir / cut_rel]), 1)

                    hashes1 = compute_project_task_hashes(ctx)
                    h_after_mask = hashes1[s01_k][0]
                    self.assertNotEqual(h_before, h_after_mask)

                    # Re-opening mask-info on cutout resolves original source_rel!
                    look_saved = json.loads(cfg_file.read_text(encoding="utf-8"))["characters"]["maya"]["looks"]["default"]
                    st_cut = resolve_ref_mask_state(pdir, look_saved, cut_rel)
                    self.assertEqual(st_cut["source_rel"], "references/maya/crowd.jpg")
                    self.assertTrue(st_cut["is_cutout"])

                    # Toggle back to raw original -> msr_remove_bg automatically returns to 1
                    h2 = _DummyHandler()
                    handle_char_masking_api(h2, "mask-toggle", pdir, "mask_proj", {
                        "entity_id": "maya",
                        "look_id": "default",
                        "rel_path": cut_rel,
                    })
                    self.assertEqual(h2.status, 200)
                    self.assertFalse(h2.payload["using_cutout"])
                    ctx_raw = ProjectContext(str(cfg_file))
                    self.assertEqual(resolve_msr_remove_bg_flag(ctx_raw, ctx_raw.scenes[0], rspec, [pdir / "references/maya/crowd.jpg"]), 1)

    def test_gpu_busy_blocks_sam3_but_allows_cpu_yolo(self):
        with tempfile.TemporaryDirectory() as td:
            pdir, _ = self._make_proj(Path(td))
            with mock.patch("pipeline.char_masking._is_local_gpu_busy", return_value=True):
                h_sam = _DummyHandler()
                handle_char_masking_api(h_sam, "mask-extract", pdir, "mask_proj", {
                    "entity_id": "maya", "look_id": "default", "rel_path": "references/maya/crowd.jpg", "method": "sam3",
                })
                self.assertEqual(h_sam.status, 409)

                called_force_cpu = []

                def _fake_yolo(srv_obj, payload, force_cpu=False):
                    called_force_cpu.append(force_cpu)
                    Path(payload["out_mask_path"]).parent.mkdir(parents=True, exist_ok=True)
                    Path(payload["out_mask_path"]).write_bytes(_minimal_png_bytes())
                    Path(payload["out_cutout_path"]).write_bytes(_minimal_png_bytes())
                    Path(payload["out_rgba_path"]).write_bytes(_minimal_png_bytes())
                    return {"success": True, "width": 200, "height": 160, "fg_ratio": 0.5}

                with mock.patch("pipeline.char_masking._run_mask_worker", side_effect=_fake_yolo):
                    h_yolo = _DummyHandler()
                    handle_char_masking_api(h_yolo, "mask-extract", pdir, "mask_proj", {
                        "entity_id": "maya", "look_id": "default", "rel_path": "references/maya/crowd.jpg", "method": "yolo_person",
                    })
                    self.assertEqual(h_yolo.status, 200)
                    self.assertEqual(called_force_cpu, [True])

    def test_isolate_ai_and_dummy_queue_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdir, cfg_file = self._make_proj(root)
            h = _DummyHandler()
            handle_char_masking_api(h, "isolate-ai", pdir, "mask_proj", {
                "entity_id": "maya", "look_id": "default", "source_rel": "references/maya/crowd.jpg",
                "renderer": 1, "resolution": "1024x1024", "seed": 2025,
            })
            self.assertEqual(h.status, 200)
            self.assertEqual(h.payload["target_token"], "CHAR:maya:default:isolate")

            iso_prompt = build_char_isolate_prompt(
                {"physical_traits": "woman", "name": "Maya"},
                {"description": "gray dress", "name": "Default"},
            )
            self.assertIn("<image1>", iso_prompt)

            with mock.patch.object(server, "PROJECTS_DIR", root), mock.patch.object(server, "PROFILES_FILE", WORKSPACE_ROOT / "model_profiles.json"):
                server.queue_worker_active = False
                entries = prepare_targeted_anchor_queue_items(server, "mask_proj", ["--anchors-only", "CHAR:maya:default:isolate"])
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["target_arg"], "CHAR:maya:default:isolate:s2025")
                self.assertEqual(entries[0]["resolution"], "1024x1024")

                ctx = ProjectContext(str(cfg_file))
                created = run_character_ref_generation(ctx, None, "maya:default:isolate:s2025", dummy_render=True)
                self.assertEqual(len(created), 1)
                updated_look = json.loads(cfg_file.read_text(encoding="utf-8"))["characters"]["maya"]["looks"]["default"]
                iso_take = [t for t in updated_look["generated_takes"] if t.get("isolate")]
                self.assertEqual(len(iso_take), 1)
                self.assertEqual(iso_take[0]["source_rel"], "references/maya/crowd.jpg")

    def test_worker_composite_mask_white_background_contract(self):
        """Executes real char_mask_worker.py via Wan2GP venv Python if PIL is available."""
        py = server.resolve_engine_python()
        chk = subprocess.run([py, "-c", "import PIL, numpy, cv2"], capture_output=True)
        if chk.returncode != 0:
            self.skipTest("PIL/numpy/cv2 not installed in resolved engine python")

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            src = tdir / "src.png"
            msk = tdir / "msk.png"
            out_m = tdir / "out_mask.png"
            out_r = tdir / "out_rgba.png"
            out_c = tdir / "out_cutout.png"
            # Left half kept (255), right half masked out (0) on pure red source
            subprocess.run([py, "-c", f"""
from PIL import Image
import numpy as np
Image.new('RGB', (32, 32), (200, 40, 40)).save({str(src)!r})
arr = np.zeros((32, 32), dtype='uint8')
arr[:, :16] = 255
Image.fromarray(arr, 'L').save({str(msk)!r})
"""], check=True)
            req = {
                "action": "composite_mask", "source_path": str(src), "mask_path": str(msk),
                "out_mask_path": str(out_m), "out_rgba_path": str(out_r), "out_cutout_path": str(out_c),
            }
            worker = WORK_DIR / "pipeline" / "char_mask_worker.py"
            proc = subprocess.run([py, str(worker), "--request", "-"], input=json.dumps(req), text=True, capture_output=True, check=True)
            res = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(res["success"])
            self.assertAlmostEqual(res["fg_ratio"], 0.5, places=2)

            # Verify masked-out right side is PURE WHITE (255,255,255) in cutout RGB and alpha=0 in RGBA
            verify_px = subprocess.check_output([py, "-c", f"""
from PIL import Image
c = Image.open({str(out_c)!r})
r = Image.open({str(out_r)!r})
print(c.mode, c.getpixel((4, 4)), c.getpixel((24, 4)), r.mode, r.getpixel((24, 4)))
"""], text=True).strip()
            self.assertIn("RGB (200, 40, 40) (255, 255, 255) RGBA", verify_px)
            self.assertTrue(verify_px.endswith(", 0)"))


if __name__ == "__main__":
    unittest.main()
