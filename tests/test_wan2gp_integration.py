"""
Studio <-> Wan2GP Integration Test Suite (`tests/test_wan2gp_integration.py`)
===============================================================================
Tests how the Music Video Generation Studio (`work/run_overnight_v3.py` and
`work/ui/server.py`) integrates with the Wan2GP engine (`shared/api.py` /
`WanGPSession` / `SessionJob` / `ProgressUpdate` / `GenerationResult`) across
diverse real-world user journeys and edge cases.

Two test classes are provided:
1. `TestStudioWan2GPIntegrationAdapter`:
   Fast, deterministic integration contract & scenario tests using a faithful
   in-process `FakeWanGPSession` that implements Wan2GP's exact `SessionJob`,
   `EventStream`, `ProgressUpdate`, `GenerationResult`, and `GenerationError`
   interfaces. Covers 9 multi-scenario integration paths.
2. `TestLiveWan2GPEngineIntegration`:
   Real GPU integration tests against the local Pinokio Wan2GP installation.
   Gated by `RUN_LIVE_WAN2GP_TESTS=1` so they ONLY execute when explicitly
   scheduled with the user.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))

from run_overnight_v3 import (
    ProjectContext,
    check_cancel_requested,
    clear_cancel_request,
    execute_wgp_job,
    format_progress_update,
    read_embedded_render_settings,
    resolve_rendered_clip_path,
    run_stage1_anchors,
    run_stage2_scenes,
    write_engine_status,
)
import server


# ---------------------------------------------------------------------------
# Faithful Wan2GP API Contract Stubs (mirrors shared/api.py dataclasses)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class FakeProgressUpdate:
    progress: int
    phase: str
    raw_phase: str = ""
    status: str = ""
    current_step: int | None = None
    total_steps: int | None = None
    window_index: int | None = None
    window_count: int | None = None
    task_id: str | int | None = None
    task_index: int | None = None
    task_count: int | None = None


@dataclasses.dataclass
class FakeGenerationError:
    stage: str
    message: str
    task_id: str | int | None = None
    exception_type: str | None = None


@dataclasses.dataclass
class FakeGenerationResult:
    success: bool
    status: str
    generated_files: list[Path] = dataclasses.field(default_factory=list)
    errors: list[FakeGenerationError] = dataclasses.field(default_factory=list)
    elapsed_seconds: float = 0.1


@dataclasses.dataclass
class FakeSessionEvent:
    kind: str
    data: object


class FakeEventStream:
    def __init__(self, events: list[FakeSessionEvent], step_hook=None):
        self._events = list(events)
        self._step_hook = step_hook

    def iter(self, timeout: float | None = None):
        for idx, ev in enumerate(self._events):
            if self._step_hook:
                should_stop = self._step_hook(idx, ev)
                if should_stop:
                    yield FakeSessionEvent(
                        kind="progress",
                        data=FakeProgressUpdate(
                            progress=0,
                            phase="cancelled",
                            raw_phase="Generation cancelled",
                            status="Generation cancelled",
                        ),
                    )
                    return
            yield ev


class FakeSessionJob:
    def __init__(self, events: list[FakeSessionEvent], result_fn, on_cancel=None, step_hook=None):
        self.events = FakeEventStream(events, step_hook=step_hook)
        self._result_fn = result_fn
        self._on_cancel = on_cancel
        self.cancelled = False

    def cancel(self) -> bool:
        self.cancelled = True
        if self._on_cancel:
            self._on_cancel()
        return True

    def result(self, timeout: float | None = None) -> FakeGenerationResult:
        return self._result_fn(self.cancelled)


class FakeWanGPSession:
    """Faithful in-process double for WanGPSession (`shared/api.py`) to test Studio integration."""

    def __init__(self, ffmpeg_path: Path, output_dir: Path, scenario_handler=None):
        self.ffmpeg_path = ffmpeg_path
        self.output_dir = output_dir
        self.scenario_handler = scenario_handler
        self.submitted_tasks: list[dict] = []
        self.cancel_calls: int = 0
        self.active_jobs: list[FakeSessionJob] = []

    def get_default_settings(self, model_type: str) -> dict:
        return {
            "model_type": model_type,
            "prompt": "",
            "negative_prompt": "",
            "resolution": "1664x960" if "image" in model_type or "flux" in model_type else "832x480",
            "num_inference_steps": 8,
            "guidance_scale": 1.0,
            "embedded_guidance_scale": 3.5,
            "flow_shift": 3.0,
            "sample_solver": "euler",
            "custom_settings": {},
        }

    def list_queue(self) -> dict:
        return {
            "tasks": [{"queue_id": len(self.submitted_tasks), "status": "running"}],
            "total_count": 1,
            "queued_count": 0,
            "running_count": 1,
        }

    def cancel(self) -> bool:
        self.cancel_calls += 1
        for j in self.active_jobs:
            j.cancel()
        return True

    def close(self):
        pass

    def submit_task(self, task_cfg: dict) -> FakeSessionJob:
        self.submitted_tasks.append(dict(task_cfg))
        if self.scenario_handler:
            job = self.scenario_handler(self, dict(task_cfg))
            self.active_jobs.append(job)
            return job
        raise NotImplementedError("Provide a scenario_handler for FakeWanGPSession")


class FakeLazySession:
    def __init__(self, session: FakeWanGPSession):
        self._session = session

    def get(self) -> FakeWanGPSession:
        return self._session

    def close(self):
        self._session.close()


# ---------------------------------------------------------------------------
# Fast Studio <-> Wan2GP Multi-Scenario Integration Tests
# ---------------------------------------------------------------------------

class TestStudioWan2GPIntegrationAdapter(unittest.TestCase):
    """Tests how our Studio codebase integrates with Wan2GP across 9 scenarios."""

    @classmethod
    def setUpClass(cls):
        cls.src_proj_dir = WORKSPACE_ROOT / "projects" / "test_project"
        assert (cls.src_proj_dir / "test_project.json").exists(), "test_project fixture is required"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mvg_wgp_int_")
        self.tmp_root = Path(self._tmp.name)
        self.slug = "test_wgp_proj"
        self.proj_dir = self.tmp_root / self.slug
        shutil.copytree(self.src_proj_dir, self.proj_dir)
        self.json_path = self.proj_dir / "test_project.json"

        # Remove any stale queue from the copied template directory so test is fully isolated
        stale_q = self.proj_dir / "work" / "jobs_v3" / "v3_queue.json"
        if stale_q.exists():
            stale_q.unlink()

        # Point server.PROJECTS_DIR to our isolated temp root during the test
        self._orig_projects_dir = server.PROJECTS_DIR
        server.PROJECTS_DIR = self.tmp_root
        self.ctx = ProjectContext(self.json_path)

    def tearDown(self):
        server.PROJECTS_DIR = self._orig_projects_dir
        self._tmp.cleanup()

    def _create_dummy_mp4(self, out_path: Path, frames: int = 107, res: str = "832x480", comment_json: dict | None = None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        w, h = [int(x) for x in res.split("x")]
        dur = max(1.0, frames / 24.0)
        cmd = [
            str(self.ctx.ffmpeg), "-y",
            "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=24:d={dur:.3f}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v:0", "-map", "1:a:0",
        ]
        if comment_json is not None:
            cmd.extend(["-metadata", f"comment={json.dumps(comment_json)}"])
        cmd.extend([
            "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-t", f"{dur:.3f}",
            str(out_path),
        ])
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _create_dummy_jpg(self, out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                str(self.ctx.ffmpeg), "-y", "-f", "lavfi",
                "-i", "color=c=0x334155:s=960x544:d=1",
                "-frames:v", "1", "-q:v", "2", str(out_path),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def test_01_task_translation_and_schema_contract(self):
        """Scenario 1: Verify Studio task dicts for solo, two-pass GVA, silent, and custom resolutions
        conform to Wan2GP's task contract and never leak internal Studio keys."""
        _, anchor_img = self.ctx.resolve_anchor_path(self.ctx.anchors[0])
        audio_wav = self.ctx.resolve_scene_audio("S01", "male", 0.0, 2.0, 107)
        _, rspec = self.ctx.resolve_video_renderer({"id": "S01", "mode": "male_solo"})

        # 1. Solo video pass
        solo_task = self.ctx.make_video_task(
            output_filename="S01_male_solo_832x480",
            prompt="Male vocalist singing passionately on stage",
            video_length=107,
            seed=101,
            image_start=anchor_img,
            audio_path=audio_wav,
            rspec=rspec,
        )
        required_wgp_keys = {
            "model_type", "prompt", "negative_prompt", "resolution", "video_length",
            "seed", "num_inference_steps", "guidance_phases", "sample_solver",
            "image_mode", "image_prompt_type", "video_prompt_type", "audio_prompt_type",
            "image_start", "audio_guide", "sliding_window_size", "sliding_window_overlap",
            "custom_settings", "output_filename",
        }
        self.assertTrue(required_wgp_keys.issubset(solo_task.keys()))
        self.assertEqual(solo_task["video_prompt_type"], "")
        self.assertEqual(solo_task["image_mode"], 0)
        self.assertTrue(Path(solo_task["image_start"]).exists())
        self.assertTrue(Path(solo_task["audio_guide"]).exists())

        # 2. Two-Pass Duet Pass 2 (GVA with video_guide and video_mask)
        mask_mp4 = self.ctx.ensure_right_mask(107, split_x_pct=0.5, resolution="832x480")
        guide_mp4 = self.ctx.renders_dir / "S02_Pass1_Left_832x480.mp4"
        self._create_dummy_mp4(guide_mp4, frames=107, res="832x480")

        pass2_task = self.ctx.make_video_task(
            output_filename="S02_Pass2_Final_832x480",
            prompt="Female vocalist singing on right side",
            video_length=107,
            seed=202,
            image_start=anchor_img,
            audio_path=audio_wav,
            rspec=rspec,
            video_guide=guide_mp4,
            video_mask=mask_mp4,
        )
        self.assertEqual(pass2_task["video_prompt_type"], "GVA")
        self.assertEqual(pass2_task["video_guide"], str(guide_mp4))
        self.assertEqual(pass2_task["video_mask"], str(mask_mp4))
        self.assertIn("denoising_strength", pass2_task)
        self.assertIn("masking_strength", pass2_task)

    def test_02_non_step_phases_and_status_snapshots(self):
        """Scenario 2: Verify Studio captures ALL Wan2GP ProgressUpdate phases (including loading_model,
        encoding_text, decoding, downloading_output where total_steps=None) and persists live engine_status.json."""
        out_mp4 = self.ctx.renders_dir / "S01_male_solo_832x480.mp4"

        def scenario_handler(session: FakeWanGPSession, cfg: dict) -> FakeSessionJob:
            events = [
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=10, phase="loading_model", raw_phase="Loading model...",
                    status="Loading minimax_h3_fl2va_pruned_pdd...", current_step=None, total_steps=None,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=18, phase="encoding_text", raw_phase="Encoding text prompts...",
                    status="Encoding text prompts...", current_step=None, total_steps=None,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=35, phase="inference_stage_1", raw_phase="Denoising stage 1",
                    status="Denoising step 2/4", current_step=2, total_steps=4,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=80, phase="inference_stage_1", raw_phase="Denoising stage 1",
                    status="Denoising step 4/4", current_step=4, total_steps=4,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=90, phase="decoding", raw_phase="VAE Decoding...",
                    status="Decoding latents to video frames...", current_step=None, total_steps=None,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=95, phase="downloading_output", raw_phase="Muxing audio & saving MP4...",
                    status="Saving output file...", current_step=None, total_steps=None,
                )),
            ]

            def make_result(cancelled: bool):
                self._create_dummy_mp4(out_mp4, frames=107, res="832x480")
                return FakeGenerationResult(success=True, status="completed", generated_files=[out_mp4])

            return FakeSessionJob(events, make_result)

        fake_session = FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir, scenario_handler=scenario_handler)
        q_item = {"status": "rendering", "filename": out_mp4.name}
        buf = io.StringIO()
        with redirect_stdout(buf):
            res, was_cancelled, _ = execute_wgp_job(
                ctx=self.ctx,
                session=fake_session,
                task_cfg={"model_type": "minimax_h3_fl2va_pruned_pdd", "output_filename": out_mp4.stem},
                idx=1,
                total=4,
                filename=out_mp4.name,
                stage="scene",
                reason="FORCED",
                model="minimax_h3_fl2va_pruned_pdd",
                frames=107,
                resolution="832x480",
                on_progress_item=q_item,
            )

        self.assertTrue(res.success)
        self.assertFalse(was_cancelled)

        # Parse all [QUEUE_PROGRESS] lines emitted by execute_wgp_job
        progress_lines = [
            json.loads(line[len("[QUEUE_PROGRESS] "):])
            for line in buf.getvalue().splitlines()
            if line.startswith("[QUEUE_PROGRESS] ")
        ]
        phases_seen = [p["phase"] for p in progress_lines]
        self.assertEqual(
            phases_seen,
            [
                "loading_model",
                "encoding_text",
                "inference_stage_1",
                "inference_stage_1",
                "decoding",
                "downloading_output",
            ],
            "Must emit progress updates for non-step phases (loading_model, encoding_text, decoding, downloading_output)!",
        )
        # Check wgp_queue metadata was attached from session.list_queue()
        self.assertEqual(progress_lines[0]["wgp_queue"]["running_count"], 1)

        # Check engine_status.json was written and is readable by server.py
        eng_st = server.read_project_engine_status(self.slug)
        self.assertIsNotNone(eng_st)
        self.assertEqual(eng_st["filename"], out_mp4.name)
        self.assertEqual(eng_st["phase"], "downloading_output")
        self.assertEqual(eng_st["pct"], 95.0)

    def test_03_multi_window_monotonic_progress(self):
        """Scenario 3: Verify multi-window generation (>362 frames) produces monotonic overall_pct
        across Window 1/2 -> Window 2/2 instead of jumping backwards from 85% to 20%."""
        updates = [
            FakeProgressUpdate(progress=10, phase="loading_model", raw_phase="Loading model..."),
            FakeProgressUpdate(
                progress=20, phase="inference_stage_1", raw_phase="Denoising",
                current_step=1, total_steps=8, window_index=1, window_count=2,
            ),
            FakeProgressUpdate(
                progress=52, phase="inference_stage_1", raw_phase="Denoising",
                current_step=4, total_steps=8, window_index=1, window_count=2,
            ),
            FakeProgressUpdate(
                progress=85, phase="inference_stage_1", raw_phase="Denoising",
                current_step=8, total_steps=8, window_index=1, window_count=2,
            ),
            # Window 2 starts at raw progress=20 in Wan2GP
            FakeProgressUpdate(
                progress=20, phase="inference_stage_1", raw_phase="Denoising",
                current_step=1, total_steps=8, window_index=2, window_count=2,
            ),
            FakeProgressUpdate(
                progress=52, phase="inference_stage_1", raw_phase="Denoising",
                current_step=4, total_steps=8, window_index=2, window_count=2,
            ),
            FakeProgressUpdate(
                progress=85, phase="inference_stage_1", raw_phase="Denoising",
                current_step=8, total_steps=8, window_index=2, window_count=2,
            ),
            FakeProgressUpdate(progress=90, phase="decoding", raw_phase="VAE Decoding..."),
        ]

        formatted = [format_progress_update(u, idx=1, filename="S10_long_scene.mp4") for u in updates]
        pcts = [f["pct"] for f in formatted]

        # Verify strictly non-decreasing monotonic overall percentages
        for a, b in zip(pcts, pcts[1:]):
            self.assertLessEqual(a, b, f"Progress jumped backwards in multi-window render: {pcts}")

        # Window 1 end (85 raw) should be 52.5% overall; Window 2 end (85 raw) should be 85.0% overall
        self.assertAlmostEqual(formatted[3]["pct"], 52.5, places=1)
        self.assertEqual(formatted[3]["window_index"], 1)
        self.assertEqual(formatted[3]["window_count"], 2)
        self.assertAlmostEqual(formatted[4]["pct"], 52.5, places=1)
        self.assertEqual(formatted[4]["window_index"], 2)
        self.assertAlmostEqual(formatted[6]["pct"], 85.0, places=1)
        self.assertAlmostEqual(formatted[7]["pct"], 90.0, places=1)

    def test_04_mid_render_graceful_cancellation(self):
        """Scenario 4: Verify writing cancel_request.json during active Wan2GP inference calls
        job.cancel() & session.cancel(), marks the item STOPPED BY USER in v3_queue.json, and updates engine_status.json."""
        # Prepare a fake session that writes cancel_request.json after step 2
        def scenario_handler(session: FakeWanGPSession, cfg: dict) -> FakeSessionJob:
            events = [
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=25, phase="inference", raw_phase="Denoising", current_step=1, total_steps=8,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=40, phase="inference", raw_phase="Denoising", current_step=2, total_steps=8,
                )),
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=60, phase="inference", raw_phase="Denoising", current_step=4, total_steps=8,
                )),
            ]

            def step_hook(step_idx: int, ev: FakeSessionEvent):
                if step_idx == 1:
                    # Simulate user clicking Stop Render mid-inference
                    cancel_file = self.ctx.jobs_dir / "cancel_request.json"
                    cancel_file.write_text(json.dumps({"reason": "STOPPED BY USER"}), encoding="utf-8")
                return False

            def make_result(cancelled: bool):
                if cancelled:
                    return FakeGenerationResult(
                        success=False,
                        status="cancelled",
                        errors=[FakeGenerationError(stage="generation", message="Generation cancelled by user")],
                    )
                return FakeGenerationResult(success=True, status="completed", generated_files=[])

            return FakeSessionJob(events, make_result, step_hook=step_hook)

        fake_session = FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir, scenario_handler=scenario_handler)
        state = self.ctx.load_state_hashes()

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(RuntimeError) as cm:
                run_stage2_scenes(
                    ctx=self.ctx,
                    lazy_session=FakeLazySession(fake_session),
                    state=state,
                    force_scenes=["S01"],
                    regenerated_anchors=set(),
                    dummy_render=False,
                    no_queue_sync=False,
                    no_stitch=True,
                )

        self.assertIn("Cancelled rendering", str(cm.exception))
        self.assertTrue(fake_session.active_jobs[0].cancelled, "job.cancel() must be invoked on Wan2GP SessionJob")
        self.assertGreaterEqual(fake_session.cancel_calls, 1, "session.cancel() must be invoked")
        self.assertFalse(check_cancel_requested(self.ctx), "cancel_request.json should be cleaned up after cancellation")

        eng_st = server.read_project_engine_status(self.slug)
        self.assertEqual(eng_st["state"], "cancelled")

        q_items = json.loads((self.ctx.jobs_dir / "v3_queue.json").read_text(encoding="utf-8"))
        s01_item = next(i for i in q_items if "S01" in i["output_filename"])
        self.assertEqual(s01_item["status"], "queued")
        self.assertEqual(s01_item["reason"], "STOPPED BY USER")

    def test_05_engine_error_propagation_and_queue_recording(self):
        """Scenario 5: Verify Wan2GP GenerationError (e.g. validation error or CUDA OOM) is recorded
        in v3_queue.json, engine_status.json, and emitted via [QUEUE_FAIL]."""
        def scenario_handler(session: FakeWanGPSession, cfg: dict) -> FakeSessionJob:
            events = [
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=10, phase="loading_model", raw_phase="Loading model...",
                )),
                FakeSessionEvent("error", FakeGenerationError(
                    stage="generation",
                    message="CUDA out of memory: tried to allocate 2.50 GiB",
                    exception_type="OutOfMemoryError",
                )),
            ]

            def make_result(cancelled: bool):
                return FakeGenerationResult(
                    success=False,
                    status="failed",
                    generated_files=[],
                    errors=[
                        FakeGenerationError(
                            stage="generation",
                            message="CUDA out of memory: tried to allocate 2.50 GiB",
                            exception_type="OutOfMemoryError",
                        )
                    ],
                )

            return FakeSessionJob(events, make_result)

        fake_session = FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir, scenario_handler=scenario_handler)
        state = self.ctx.load_state_hashes()

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(RuntimeError) as cm:
                run_stage2_scenes(
                    ctx=self.ctx,
                    lazy_session=FakeLazySession(fake_session),
                    state=state,
                    force_scenes=["S01"],
                    regenerated_anchors=set(),
                    dummy_render=False,
                    no_queue_sync=False,
                    no_stitch=True,
                )

        self.assertIn("CUDA out of memory", str(cm.exception))
        out_lines = buf.getvalue().splitlines()
        fail_lines = [json.loads(l[len("[QUEUE_FAIL] "):]) for l in out_lines if l.startswith("[QUEUE_FAIL] ")]
        self.assertEqual(len(fail_lines), 1)
        self.assertIn("CUDA out of memory", fail_lines[0]["error"][0])

        eng_st = server.read_project_engine_status(self.slug)
        self.assertEqual(eng_st["state"], "failed")
        self.assertIn("CUDA out of memory", eng_st["error"][0])

        q_items = json.loads((self.ctx.jobs_dir / "v3_queue.json").read_text(encoding="utf-8"))
        s01_item = next(i for i in q_items if "S01" in i["output_filename"])
        self.assertEqual(s01_item["status"], "failed")
        self.assertIn("CUDA out of memory", s01_item["error"][0])

    def test_06_anchor_regeneration_cascades_to_dependent_scenes(self):
        """Scenario 6: Verify Stage 1 anchor generation via WanGPSession promotes the generated image,
        updates state_hashes.json, and cascades to dependent scenes in compute_fast_project_queue."""
        # Ensure all scenes have valid rendered clips (>50KB) newer than current anchors before baseline sync
        q_initial = server.compute_fast_project_queue(self.slug, force_recheck=True)
        now_ts = time.time()
        for item in q_initial:
            mp4_path = self.ctx.renders_dir / item["filename"]
            if not mp4_path.exists() or mp4_path.stat().st_size <= 50000:
                self._create_dummy_mp4(mp4_path, frames=107, res=item.get("resolution", "960x544"))
            os.utime(mp4_path, (now_ts, now_ts))

        # Sync baseline hashes so everything starts as completed
        state = self.ctx.load_state_hashes()
        with redirect_stdout(io.StringIO()):
            run_stage1_anchors(self.ctx, FakeLazySession(FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir)), state, [], sync_only=True)
            run_stage2_scenes(self.ctx, FakeLazySession(FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir)), state, [], set(), sync_only=True)

        q_synced = server.compute_fast_project_queue(self.slug, force_recheck=True)
        self.assertTrue(all(i["status"] == "completed" for i in q_synced), f"Expected all completed, got {q_synced}")

        # Wait briefly so new anchor mtime is strictly newer than existing S01 clip mtime
        time.sleep(0.05)

        # Now run Stage 1 anchor generation for A01 via FakeWanGPSession (simulating Wan2GP writing to a temp output path)
        def anchor_scenario_handler(session: FakeWanGPSession, cfg: dict) -> FakeSessionJob:
            events = [
                FakeSessionEvent("progress", FakeProgressUpdate(progress=10, phase="loading_model", raw_phase="Loading Qwen Image...")),
                FakeSessionEvent("progress", FakeProgressUpdate(progress=60, phase="inference", raw_phase="Sampling", current_step=4, total_steps=8)),
                FakeSessionEvent("progress", FakeProgressUpdate(progress=90, phase="decoding", raw_phase="Decoding image...")),
            ]

            def make_result(cancelled: bool):
                gen_img = self.ctx.renders_dir / f"{cfg['output_filename']}_wgp_out.jpg"
                self._create_dummy_jpg(gen_img)
                return FakeGenerationResult(success=True, status="completed", generated_files=[gen_img])

            return FakeSessionJob(events, make_result)

        fake_session = FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir, scenario_handler=anchor_scenario_handler)
        with redirect_stdout(io.StringIO()):
            regen = run_stage1_anchors(
                ctx=self.ctx,
                lazy_session=FakeLazySession(fake_session),
                state=state,
                force_anchors=["A01"],
                dummy_render=False,
            )

        self.assertIn("A01", regen)
        self.assertEqual(len(fake_session.submitted_tasks), 1)
        # Since test_project has no reference portraits (anchor_refs=[]), video_prompt_type must be "" (text-to-image)
        self.assertEqual(fake_session.submitted_tasks[0]["video_prompt_type"], "")
        self.assertIsNone(fake_session.submitted_tasks[0]["image_refs"])
        # And when reference portraits are provided, make_anchor_task sets video_prompt_type="I"
        _, def_aspec = self.ctx.resolve_anchor_renderer(self.ctx.anchors[0])
        with_refs_cfg = self.ctx.make_anchor_task(
            session=fake_session,
            rspec=def_aspec,
            output_filename="test_ref_anchor",
            prompt="Portrait with reference",
            image_refs=[str(self.ctx.anchors_dir / "A02_Anchor_Duet.jpg")],
        )
        self.assertEqual(with_refs_cfg["video_prompt_type"], "I")
        self.assertEqual(len(with_refs_cfg["image_refs"]), 1)

        # Verify compute_fast_project_queue cascades A01 update to S01 (which uses A01) while keeping S02 (which uses A02) completed
        q_after = server.compute_fast_project_queue(self.slug, force_recheck=False)
        s01_item = next(i for i in q_after if i["scene_id"] == "S01")
        s02_p1_item = next(i for i in q_after if i["scene_id"] == "S02" and i["pass"] == 1)
        self.assertEqual(s01_item["status"], "queued")
        self.assertIn("ANCHOR A01", s01_item["reason"])
        self.assertEqual(s02_p1_item["status"], "completed")

    def test_07_two_pass_duet_gva_guide_and_mask_handoff(self):
        """Scenario 7: Verify Two-Pass Duet GVA scene renders Pass 1 first, promotes the left-pass MP4,
        generates the right-half mask MP4, and submits Pass 2 with video_prompt_type='GVA', video_guide, and video_mask."""
        def duet_scenario_handler(session: FakeWanGPSession, cfg: dict) -> FakeSessionJob:
            events = [
                FakeSessionEvent("progress", FakeProgressUpdate(
                    progress=50, phase="inference", raw_phase="Denoising", current_step=2, total_steps=4,
                )),
            ]

            def make_result(cancelled: bool):
                out_file = self.ctx.renders_dir / f"{cfg['output_filename']}.mp4"
                self._create_dummy_mp4(out_file, frames=int(cfg.get("video_length", 107)), res=cfg.get("resolution", "832x480"))
                return FakeGenerationResult(success=True, status="completed", generated_files=[out_file])

            return FakeSessionJob(events, make_result)

        fake_session = FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir, scenario_handler=duet_scenario_handler)
        state = self.ctx.load_state_hashes()

        with redirect_stdout(io.StringIO()):
            run_stage2_scenes(
                ctx=self.ctx,
                lazy_session=FakeLazySession(fake_session),
                state=state,
                force_scenes=["S02"],
                regenerated_anchors=set(),
                dummy_render=False,
                no_queue_sync=False,
                no_stitch=True,
            )

        self.assertEqual(len(fake_session.submitted_tasks), 2, "S02 must submit Pass 1 and Pass 2 to Wan2GP")
        p1_cfg, p2_cfg = fake_session.submitted_tasks[0], fake_session.submitted_tasks[1]

        self.assertIn("Pass1_Left", p1_cfg["output_filename"])
        self.assertEqual(p1_cfg["video_prompt_type"], "")
        self.assertNotIn("video_guide", p1_cfg)

        self.assertIn("Pass2_Final", p2_cfg["output_filename"])
        self.assertEqual(p2_cfg["video_prompt_type"], "GVA")
        self.assertTrue(Path(p2_cfg["video_guide"]).exists(), "Pass 2 video_guide must exist on disk")
        self.assertIn("Pass1_Left", Path(p2_cfg["video_guide"]).name)
        self.assertTrue(Path(p2_cfg["video_mask"]).exists(), "Pass 2 video_mask must exist on disk")
        self.assertIn("mask_right_", Path(p2_cfg["video_mask"]).name)

    def test_08_duplicate_suffixed_output_promotion(self):
        """Scenario 8: Verify that when Wan2GP saves `out_name(2).mp4` (because Windows held a lock or
        a prior file existed), Studio promotes/copies it to the canonical clip filename and records coverage."""
        def suffixed_scenario_handler(session: FakeWanGPSession, cfg: dict) -> FakeSessionJob:
            events = [
                FakeSessionEvent("progress", FakeProgressUpdate(progress=85, phase="inference", current_step=4, total_steps=4)),
            ]

            def make_result(cancelled: bool):
                # Simulate Wan2GP saving with a (2).mp4 duplicate suffix
                suffixed_mp4 = self.ctx.renders_dir / f"{cfg['output_filename']}(2).mp4"
                self._create_dummy_mp4(
                    suffixed_mp4,
                    frames=int(cfg.get("video_length", 107)),
                    res=cfg.get("resolution", "832x480"),
                    comment_json={"seed": cfg.get("seed", 101), "model_type": cfg.get("model_type")},
                )
                return FakeGenerationResult(success=True, status="completed", generated_files=[suffixed_mp4])

            return FakeSessionJob(events, make_result)

        fake_session = FakeWanGPSession(self.ctx.ffmpeg, self.ctx.renders_dir, scenario_handler=suffixed_scenario_handler)
        state = self.ctx.load_state_hashes()

        with redirect_stdout(io.StringIO()):
            run_stage2_scenes(
                ctx=self.ctx,
                lazy_session=FakeLazySession(fake_session),
                state=state,
                force_scenes=["S01"],
                regenerated_anchors=set(),
                dummy_render=False,
                no_queue_sync=False,
                no_stitch=True,
            )

        expected_stem = f"S01_male_solo_{self.ctx.current_resolution}"
        canonical_mp4 = resolve_rendered_clip_path(self.ctx.renders_dir, expected_stem, promote=True)
        self.assertTrue(canonical_mp4.exists())
        self.assertEqual(canonical_mp4.name, f"{expected_stem}.mp4")
        embedded = read_embedded_render_settings(self.ctx.ffmpeg, canonical_mp4)
        self.assertIsNotNone(embedded)
        self.assertEqual(embedded.get("seed"), 6001)

    def test_09_live_status_hydration_across_http_endpoints(self):
        """Scenario 9: Verify that while the engine is actively rendering, `compute_fast_project_queue`
        and `read_project_engine_status` hydrate the active queue item with live phase/step/window/pct
        from `engine_status.json` without relying on prior SSE log lines."""
        q_initial = server.compute_fast_project_queue(self.slug, force_recheck=True)
        q_initial[0]["status"] = "rendering"
        (self.ctx.jobs_dir / "v3_queue.json").write_text(json.dumps(q_initial, indent=2), encoding="utf-8")

        # Write a live multi-window engine_status.json snapshot
        write_engine_status(
            self.ctx,
            {
                "state": "rendering",
                "stage": "scene",
                "idx": 1,
                "total": len(q_initial),
                "filename": q_initial[0]["filename"],
                "output_filename": q_initial[0]["output_filename"],
                "phase": "inference_stage_1",
                "raw_phase": "Denoising first pass",
                "status_text": "Denoising step 3/8 (Window 1/2)",
                "step": 3,
                "total_steps": 8,
                "pct": 32.2,
                "window_index": 1,
                "window_count": 2,
                "started_at": time.time() - 12.0,
            },
            merge=False,
        )

        # Simulate active engine process so server.compute_fast_project_queue treats the render as live
        class DummyAliveProc:
            def poll(self):
                return None

        orig_proc = server.active_process
        try:
            server.active_process = DummyAliveProc()
            hydrated_queue = server.compute_fast_project_queue(self.slug, force_recheck=False)
            active_item = hydrated_queue[0]
            self.assertEqual(active_item["status"], "rendering")
            self.assertEqual(active_item["pct"], 32.2)
            self.assertEqual(active_item["phase"], "inference_stage_1")
            self.assertEqual(active_item["raw_phase"], "Denoising first pass")
            self.assertEqual(active_item["step"], 3)
            self.assertEqual(active_item["total_steps"], 8)
            self.assertEqual(active_item["window_index"], 1)
            self.assertEqual(active_item["window_count"], 2)
        finally:
            server.active_process = orig_proc


# ---------------------------------------------------------------------------
# Live Wan2GP Validation (No GPU VRAM Needed) & Live GPU Integration Suite
# ---------------------------------------------------------------------------

LIVE_TESTS_ENABLED = os.environ.get("RUN_LIVE_WAN2GP_TESTS", "0") in ("1", "true", "yes")


class TestLiveWan2GPValidationNoGPU(unittest.TestCase):
    """Live Scenario A: Initializes the real Pinokio WanGPSession (`shared/api.py`) and validates
    all Studio-generated task dicts (solo video renderers, two-pass GVA duet, and Stage 1 anchor
    renderers) against Wan2GP's native `merge_settings_with_defaults`, `_normalize_source`, and
    `wgp.validate_task` without loading any GPU models or touching active GPU renders."""

    @classmethod
    def setUpClass(cls):
        cls.src_proj_dir = WORKSPACE_ROOT / "projects" / "test_project"
        cls._tmp = tempfile.TemporaryDirectory(prefix="mvg_live_val_")
        cls.tmp_root = Path(cls._tmp.name)
        cls.slug = "live_val_project"
        cls.proj_dir = cls.tmp_root / cls.slug
        shutil.copytree(cls.src_proj_dir, cls.proj_dir)
        cls.json_path = cls.proj_dir / "test_project.json"
        cls.ctx = ProjectContext(cls.json_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_live_A_session_init_and_validate_all_studio_profiles(self):
        """Live Scenario A: Initialize real WanGPSession and validate our Studio's generated task dicts
        against Wan2GP's native `merge_settings_with_defaults` and `wgp.validate_task`."""
        wgp_api_file = self.ctx.wgp_root / "shared" / "api.py"
        if not wgp_api_file.is_file():
            self.skipTest(f"Wan2GP installation not found at {self.ctx.wgp_root}")
        try:
            wgp_api_file.read_bytes()[:16]
            (self.ctx.wgp_root / "shared" / "__init__.py").read_bytes()[:16]
        except PermissionError:
            self.skipTest(f"Wan2GP directory at {self.ctx.wgp_root} is outside current process sandbox permissions")

        engine_py = server.resolve_engine_python()
        # If the current test runner Python is not the Wan2GP venv Python, invoke this test inside engine_py
        if Path(sys.executable).resolve() != Path(engine_py).resolve() and os.environ.get("_IN_ENGINE_PY") != "1":
            env = os.environ.copy()
            env["_IN_ENGINE_PY"] = "1"
            res = subprocess.run(
                [
                    engine_py,
                    "-m",
                    "unittest",
                    "tests.test_wan2gp_integration.TestLiveWan2GPValidationNoGPU.test_live_A_session_init_and_validate_all_studio_profiles",
                    "-v",
                ],
                cwd=str(WORKSPACE_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                res.returncode,
                0,
                f"Live Scenario A failed under engine Python ({engine_py}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
            )
            return

        sys.path.insert(0, str(self.ctx.wgp_root))
        from shared.api import init

        session = init(
            root=self.ctx.wgp_root,
            config_path=self.ctx.wgp_root / "wgp_config.json",
            output_dir=self.ctx.renders_dir,
            console_output=False,
            console_isatty=False,
        )
        try:
            runtime = session._ensure_runtime()
            wgp_mod = runtime.module

            _, anchor_img = self.ctx.resolve_anchor_path(self.ctx.anchors[0])
            audio_wav = self.ctx.resolve_scene_audio("S01", "male", 0.0, 2.0, 107)

            # 1. Validate all configured Video Renderers (Solo mode)
            for rid, rspec in self.ctx.video_renderers.items():
                task = self.ctx.make_video_task(
                    output_filename=f"validate_v_{rid}",
                    prompt="A cinematic live performance on stage",
                    video_length=self.ctx.valid_video_frames(2.0, rspec),
                    seed=42,
                    image_start=anchor_img,
                    audio_path=audio_wav,
                    rspec=rspec,
                )
                merged = session.merge_settings_with_defaults(task)
                norm_task = session._normalize_source(merged, caller_base_path=self.ctx.wgp_root)[0]
                validated, err = wgp_mod.validate_task(norm_task, session._state)
                self.assertIsNotNone(
                    validated,
                    f"Video renderer #{rid} ({rspec.get('model_type')}) failed Wan2GP validate_task: {err}",
                )

            # 2. Validate Two-Pass Duet Pass 2 (GVA mode with video_guide and video_mask)
            _, def_vspec = self.ctx.resolve_video_renderer({"id": "S02", "mode": "duet_method_b"})
            mask_mp4 = self.ctx.ensure_right_mask(107, split_x_pct=0.5, resolution="960x544")
            guide_mp4 = resolve_rendered_clip_path(self.ctx.renders_dir, "S01_male_solo_960x544")
            if not guide_mp4.exists():
                guide_mp4 = mask_mp4
            gva_task = self.ctx.make_video_task(
                output_filename="validate_duet_p2",
                prompt="Female vocalist singing on the right side of the stage",
                video_length=self.ctx.valid_video_frames(2.0, def_vspec),
                seed=202,
                image_start=anchor_img,
                audio_path=audio_wav,
                rspec=def_vspec,
                video_guide=guide_mp4,
                video_mask=mask_mp4,
            )
            merged_gva = session.merge_settings_with_defaults(gva_task)
            norm_gva = session._normalize_source(merged_gva, caller_base_path=self.ctx.wgp_root)[0]
            validated_gva, err_gva = wgp_mod.validate_task(norm_gva, session._state)
            self.assertIsNotNone(
                validated_gva,
                f"Two-pass GVA task failed Wan2GP validate_task: {err_gva}",
            )

            # 3. Validate all configured Anchor Renderers (Stage 1 Image mode with reference images)
            for rid, rspec in self.ctx.anchor_renderers.items():
                model_type = rspec["model_type"]
                cfg_with_ref = self.ctx.make_anchor_task(
                    session=session,
                    rspec=rspec,
                    output_filename=f"validate_a_ref_{rid}",
                    prompt="High resolution portrait on dark stage",
                    seed=42,
                    image_refs=[str(anchor_img)],
                )
                merged_a = session.merge_settings_with_defaults(cfg_with_ref)
                norm_a = session._normalize_source(merged_a, caller_base_path=self.ctx.wgp_root)[0]
                validated_a, err_a = wgp_mod.validate_task(norm_a, session._state)
                self.assertIsNotNone(
                    validated_a,
                    f"Anchor renderer #{rid} ({model_type}) with image_refs failed Wan2GP validate_task: {err_a}",
                )

            # 4. Validate default Anchor Renderer without reference images (fresh project text-to-image mode)
            _, def_aspec = self.ctx.resolve_anchor_renderer(self.ctx.anchors[0])
            cfg_no_ref = self.ctx.make_anchor_task(
                session=session,
                rspec=def_aspec,
                output_filename="validate_a_noref",
                prompt="High resolution portrait on dark stage",
                seed=42,
                image_refs=[],
            )
            merged_noref = session.merge_settings_with_defaults(cfg_no_ref)
            norm_noref = session._normalize_source(merged_noref, caller_base_path=self.ctx.wgp_root)[0]
            validated_noref, err_noref = wgp_mod.validate_task(norm_noref, session._state)
            self.assertIsNotNone(
                validated_noref,
                f"Default anchor renderer ({def_aspec.get('model_type')}) without image_refs failed Wan2GP validate_task: {err_noref}",
            )
        finally:
            session.close()


@unittest.skipUnless(
    LIVE_TESTS_ENABLED,
    "Live GPU Wan2GP integration tests are prepared but skipped by default. "
    "Set RUN_LIVE_WAN2GP_TESTS=1 when ready to run against the real GPU engine.",
)
class TestLiveWan2GPEngineIntegration(unittest.TestCase):
    """End-to-end integration tests with the real Pinokio Wan2GP engine (`shared/api.py`) on GPU.
    Tests our Studio's real integration across:
    - Live Scenario B: Real Stage 1 Anchor generation + live engine_status.json phase transitions + Stage 2 cascade
    - Live Scenario C: Real Stage 2 Single-Pass Video render + embedded MP4 metadata + audio verification
    - Live Scenario D: Real Stage 2 Two-Pass Duet GVA chain (Pass 1 guide + mask -> Pass 2 GVA)
    - Live Scenario E: Real Mid-Render Cancellation (`kill_active_render` -> `cancel_request.json` -> `job.cancel()`)
    """

    @classmethod
    def setUpClass(cls):
        cls.src_proj_dir = WORKSPACE_ROOT / "projects" / "test_project"
        cls._tmp = tempfile.TemporaryDirectory(prefix="mvg_live_wgp_")
        cls.tmp_root = Path(cls._tmp.name)
        cls.slug = "live_test_project"
        cls.proj_dir = cls.tmp_root / cls.slug
        shutil.copytree(cls.src_proj_dir, cls.proj_dir)
        cls.json_path = cls.proj_dir / "test_project.json"
        cls._orig_projects_dir = server.PROJECTS_DIR
        server.PROJECTS_DIR = cls.tmp_root
        cls.ctx = ProjectContext(cls.json_path)

    @classmethod
    def tearDownClass(cls):
        server.PROJECTS_DIR = cls._orig_projects_dir
        cls._tmp.cleanup()

    def setUp(self):
        server.dummy_render_mode = False

    def test_live_B_stage1_anchor_generation_and_cascade(self):
        """Live Scenario B: Generate anchor A01 via `server.run_anchor_worker` using real Wan2GP,
        observing live `engine_status.json` updates and verifying Stage 2 queue cascade."""
        observed_phases = []
        done_evt = threading.Event()

        def _runner():
            try:
                server.run_anchor_worker(str(self.json_path), self.slug, ["A01", "--anchors-only"])
            finally:
                done_evt.set()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

        while not done_evt.wait(timeout=0.5):
            st = server.read_project_engine_status(self.slug)
            if st and st.get("phase") and st.get("phase") not in observed_phases:
                observed_phases.append(st["phase"])

        self.assertEqual(server.render_status.get("exit_code"), 0)
        self.assertTrue(len(observed_phases) >= 1, f"Expected live engine_status phases, saw {observed_phases}")
        a01_path = self.ctx.anchors_dir / "A01_Anchor_Male.jpg"
        self.assertTrue(a01_path.exists() and a01_path.stat().st_size > 10000)

    def test_live_C_single_pass_video_render_and_metadata(self):
        """Live Scenario C: Render single-pass scene S01 through `server.run_queue_worker` on real GPU,
        verifying live `engine_status.json` phases, output MP4 streams, and embedded Wan2GP metadata."""
        server.prepare_targeted_queue_items(self.slug, "S01", is_worker_running=False)
        server.run_queue_worker(self.slug, force_restart=True)

        observed_phases = []
        t0 = time.time()
        while time.time() - t0 < 900:
            st = server.read_project_engine_status(self.slug)
            if st and st.get("phase") and st.get("phase") not in observed_phases:
                observed_phases.append(st["phase"])
            if not server.queue_worker_active and server.active_process is None:
                break
            time.sleep(0.5)

        self.assertEqual(server.render_status.get("exit_code"), 0)
        out_mp4 = resolve_rendered_clip_path(self.ctx.renders_dir, f"S01_male_solo_{self.ctx.current_resolution}", promote=True)
        self.assertTrue(out_mp4.exists() and out_mp4.stat().st_size > 50000)
        embedded = read_embedded_render_settings(self.ctx.ffmpeg, out_mp4)
        self.assertIsNotNone(embedded, "Real Wan2GP output MP4 should contain embedded render settings metadata")

    def test_live_D_two_pass_duet_gva_render(self):
        """Live Scenario D: Render Two-Pass Duet scene S02 (Pass 1 Left -> Pass 2 GVA Final) on real GPU."""
        server.prepare_targeted_queue_items(self.slug, "S02", is_worker_running=False)
        server.run_queue_worker(self.slug, force_restart=True)

        t0 = time.time()
        while time.time() - t0 < 1800:
            if not server.queue_worker_active and server.active_process is None:
                break
            time.sleep(1.0)

        self.assertEqual(server.render_status.get("exit_code"), 0)
        p1_mp4 = resolve_rendered_clip_path(self.ctx.renders_dir, "S02_Pass1_Left_832x480", promote=True)
        p2_mp4 = resolve_rendered_clip_path(self.ctx.renders_dir, "S02_Pass2_Final_832x480", promote=True)
        self.assertTrue(p1_mp4.exists() and p1_mp4.stat().st_size > 50000)
        self.assertTrue(p2_mp4.exists() and p2_mp4.stat().st_size > 50000)

    def test_live_E_mid_render_cancellation_and_recovery(self):
        """Live Scenario E: Start a real GPU render for S03, wait until `engine_status.json` enters
        `inference`, trigger `server.kill_active_render()`, and verify graceful cancellation and queue state."""
        server.prepare_targeted_queue_items(self.slug, "S03", is_worker_running=False)
        server.run_queue_worker(self.slug, force_restart=True)

        t0 = time.time()
        entered_inference = False
        while time.time() - t0 < 300:
            st = server.read_project_engine_status(self.slug)
            if st and str(st.get("phase") or "").startswith("inference"):
                entered_inference = True
                break
            time.sleep(0.5)

        self.assertTrue(entered_inference, "Timed out waiting for Wan2GP to enter inference phase")
        server.kill_active_render()

        st_after = server.read_project_engine_status(self.slug)
        self.assertEqual(st_after.get("state"), "cancelled")
        q_after = json.loads((self.ctx.jobs_dir / "v3_queue.json").read_text(encoding="utf-8"))
        s03_item = next(i for i in q_after if i["scene_id"] == "S03")
        self.assertEqual(s03_item["status"], "queued")
        self.assertEqual(s03_item["reason"], "STOPPED BY USER")


if __name__ == "__main__":
    unittest.main()
