"""True end-to-end Playwright browser tests for all Music Video Studio UI User Journeys.

Starts an ephemeral live ThreadingHTTPServer bound to 127.0.0.1:0 with PROJECTS_DIR
and BACKUP_DIR isolated inside a temporary directory (never touching live projects/),
drives a real headless Chromium/Chrome/Edge browser via mouse clicks and form edits,
asserts zero uncaught page errors, and verifies persisted backend files on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = WORKSPACE_ROOT / "work"
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

from pipeline.context import ProjectContext
from pipeline.media_utils import resolve_ffmpeg_path
from pipeline.stage2_hashes import compute_project_task_hashes
from pipeline.takes import after_scene_render
from ui import server
from ui.server_routes import StudioRequestHandler


def _write_wav(path: Path, duration_s: float = 6.0, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * int(duration_s * sr))


def _make_dummy_mp4(ffmpeg_bin: Path, path: Path, duration_s: float = 2.0, color: str = "navy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg_bin), "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration_s}:r=25",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(duration_s),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Pad tail metadata slightly so file size comfortably clears resolve_rendered_clip_path's 50KB check
    if path.stat().st_size <= 55000:
        with open(path, "ab") as f:
            f.write(b"\x00" * (60000 - path.stat().st_size))


def _make_tiny_jpg(ffmpeg_bin: Path, path: Path, color: str = "darkgreen") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg_bin), "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d=0.1",
            "-vframes", "1",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _create_e2e_project(projects_dir: Path, slug: str = "e2e_proj") -> tuple[Path, Path]:
    ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
    proj_dir = projects_dir / slug
    for sub in (
        "audio",
        "references",
        "work/anchors_v3",
        "work/audio_v3",
        "work/jobs_v3",
        "work/renders_v3",
        "work/trimmed_v3",
        "work/frames_v3",
    ):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)

    _write_wav(proj_dir / "audio" / "master.wav", duration_s=6.0)
    _write_wav(proj_dir / "audio" / "male.wav", duration_s=6.0)
    _write_wav(proj_dir / "audio" / "female.wav", duration_s=6.0)

    a01_path = proj_dir / "work" / "anchors_v3" / "A01_Lead.jpg"
    a02_path = proj_dir / "work" / "anchors_v3" / "A02_Wide.jpg"
    _make_tiny_jpg(ffmpeg_bin, a01_path, "darkblue")
    _make_tiny_jpg(ffmpeg_bin, a02_path, "darkred")

    cfg = {
        "project": {
            "name": "E2E Studio Demo",
            "project_dir": ".",
            "master_audio": "audio/master.wav",
            "stems": {"male": "audio/male.wav", "female": "audio/female.wav"},
            "master_output": "{project_name}_Master_{resolution}_{fps}fps.mp4",
            "master_resolution": "640x360",
            "fps": 25,
            "total_duration": 6.0,
            "fade_out_last_scene_seconds": 0.5,
            "directories": {
                "anchors": "work/anchors_v3",
                "audio_slices": "work/audio_v3",
                "masks": "work/masks",
                "jobs": "work/jobs_v3",
                "renders": "work/renders_v3",
                "trimmed": "work/trimmed_v3",
                "review_strips": "work/frames_v3",
            },
        },
        "models": {
            "default_anchor_renderer": 3,
            "default_video_renderer": 1,
            "default_video_resolution": 1,
            "anchor_renderers": {
                "3": {"model_type": "z_image", "name": "Z-Image", "num_inference_steps": 6, "guidance_scale": 1.0}
            },
            "video_renderers": {
                "1": {"model_type": "ltx23_i2v_22B", "name": "LTX 2.3"}
            },
            "video_resolutions": {
                "1": {"width": 832, "height": 480, "resolution": "832x480", "name": "480p Fast"},
                "2": {"width": 960, "height": 544, "resolution": "960x544", "name": "540p Balanced"},
            },
            "video_shared_settings": {"negative_prompt": "blurry, split-screen"},
            "anchor_shared_settings": {"image_refs": []},
        },
        "variables": {"LEAD_SINGER": "expressive lead vocalist"},
        "markers": [],
        "characters": {
            "char_male": {
                "id": "char_male",
                "name": "Male Singer",
                "kind": "character",
                "default_look": "default",
                "avatar": "",
                "physical_traits": "charismatic male performer",
                "looks": {
                    "default": {
                        "tag": "@male",
                        "name": "Default",
                        "description": "concert coat under stage light",
                        "legacy_prompt_text": "charismatic male performer",
                        "image_refs": [],
                    }
                },
            }
        },
        "anchors": [
            {"id": "A01", "filename": "A01_Lead.jpg", "seed": 1001, "prompt": "Close-up of @male"},
            {"id": "A02", "filename": "A02_Wide.jpg", "seed": 1002, "prompt": "Wide shot of concert hall"},
        ],
        "scenes": [
            {
                "id": "S01",
                "start": 0.0,
                "end": 3.0,
                "mode": "solo",
                "stem": "male",
                "anchor": "A01_Lead.jpg",
                "seed": 1001,
                "prompt": "Medium close-up of @male singing under spotlight",
            },
            {
                "id": "S02",
                "start": 3.0,
                "end": 6.0,
                "mode": "silent_locked",
                "anchor": "A02_Wide.jpg",
                "seed": 2002,
                "prompt": "Wide dolly across string section",
            },
        ],
    }
    cfg_path = proj_dir / f"{slug}.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return proj_dir, cfg_path


def _launch_browser(pw):
    exe = Path(pw.chromium.executable_path)
    if exe.is_file():
        return pw.chromium.launch(headless=True)
    for ch in ("chrome", "msedge"):
        try:
            return pw.chromium.launch(channel=ch, headless=True)
        except Exception:
            continue
    raise unittest.SkipTest("No Chromium, Chrome, or Edge browser available for Playwright E2E.")


class TestStudioUiPlaywrightE2E(unittest.TestCase):
    """Real browser click-through E2E suite covering all 10 Studio UI User Journeys."""

    _pw_ctx = None
    _pw = None
    _browser = None

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"playwright not installed: {exc}")
        cls._pw_ctx = sync_playwright()
        cls._pw = cls._pw_ctx.start()
        cls._browser = _launch_browser(cls._pw)

    @classmethod
    def tearDownClass(cls):
        if cls._browser is not None:
            try:
                cls._browser.close()
            except Exception:
                pass
        if cls._pw_ctx is not None:
            try:
                cls._pw_ctx.stop()
            except Exception:
                pass

    def _start_studio_session(self, slug: str = "e2e_proj", dev: bool = False):
        tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_root = Path(tmp_dir.name)
        projects_dir = tmp_root / "projects"
        backup_dir = tmp_root / "backup"
        projects_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        proj_dir, cfg_path = _create_e2e_project(projects_dir, slug=slug)

        orig_projects = server.PROJECTS_DIR
        orig_backup = server.BACKUP_DIR
        orig_dummy = server.dummy_render_mode
        server.PROJECTS_DIR = projects_dir
        server.BACKUP_DIR = backup_dir
        server.dummy_render_mode = False

        class _QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        httpd = _QuietServer(("127.0.0.1", 0), StudioRequestHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        ctx = self._browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        dialog_answers: list[str | bool | None] = []
        seen_dialogs: list[tuple[str, str]] = []

        def _on_dialog(dialog):
            seen_dialogs.append((dialog.type, dialog.message))
            ans = dialog_answers.pop(0) if dialog_answers else True
            if ans is False:
                dialog.dismiss()
            elif isinstance(ans, str):
                dialog.accept(ans)
            else:
                dialog.accept()

        page.on("dialog", _on_dialog)

        def _cleanup():
            try:
                server.kill_active_render(target_slug=slug)
            except Exception:
                pass
            try:
                page.goto("about:blank", wait_until="domcontentloaded")
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
            server.PROJECTS_DIR = orig_projects
            server.BACKUP_DIR = orig_backup
            server.dummy_render_mode = orig_dummy
            tmp_dir.cleanup()

        self.addCleanup(_cleanup)

        url_suffix = "&dev=1" if dev else ""
        page.goto(f"http://127.0.0.1:{port}/?project={slug}{url_suffix}", wait_until="domcontentloaded")
        page.locator("#inspectorSceneId").wait_for(state="visible", timeout=15000)
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)

        return {
            "tmp_root": tmp_root,
            "projects_dir": projects_dir,
            "backup_dir": backup_dir,
            "proj_dir": proj_dir,
            "cfg_path": cfg_path,
            "port": port,
            "base_url": f"http://127.0.0.1:{port}",
            "page": page,
            "page_errors": page_errors,
            "dialog_answers": dialog_answers,
            "seen_dialogs": seen_dialogs,
        }

    def _click_scene_on_canvas(self, page, time_s: float = 1.0, expected_id: str | None = None):
        canvas = page.locator("#waveformCanvas")
        canvas.wait_for(state="visible", timeout=10000)
        box = canvas.bounding_box()
        self.assertIsNotNone(box)
        pps_info = page.evaluate("() => ({ pps: window.state.pixelsPerSecond * (window.state.zoom || 1) })")
        x = box["x"] + time_s * pps_info["pps"]
        page.mouse.click(x, box["y"] + 105)
        if expected_id:
            page.wait_for_function(
                "eid => (document.getElementById('inspectorSceneId')?.textContent || '').includes(eid)",
                arg=expected_id,
                timeout=5000,
            )

    def test_01_uj1_timeline_select_edit_undo_redo_save_discard_new_project(self):
        env = self._start_studio_session()
        page = env["page"]
        cfg_path = env["cfg_path"]

        # Click inside S02 region on #waveformCanvas (y=105 inside scene track band 84..144)
        canvas = page.locator("#waveformCanvas")
        box = canvas.bounding_box()
        self.assertIsNotNone(box)
        pps_info = page.evaluate("() => ({ pps: window.state.pixelsPerSecond * (window.state.zoom || 1) })")
        s02_x = box["x"] + 4.0 * pps_info["pps"]
        s01_x = box["x"] + 1.0 * pps_info["pps"]
        page.mouse.click(s02_x, box["y"] + 105)
        page.wait_for_function("() => document.getElementById('inspectorSceneId')?.textContent?.includes('S02')", timeout=5000)

        # Click back onto S01 on #waveformCanvas
        page.mouse.click(s01_x, box["y"] + 105)
        page.wait_for_function("() => document.getElementById('inspectorSceneId')?.textContent?.includes('S01')", timeout=5000)

        # Edit prompt and test Undo / Redo buttons
        orig_prompt = page.locator("#scenePrompt").input_value()
        edited_prompt = "Close-up of @male singing powerfully on stage"
        page.locator("#scenePrompt").fill(edited_prompt)
        page.locator("#sceneSeed").fill("7777")
        page.locator("#sceneSeed").dispatch_event("change")

        page.locator("#btnUndo").click()
        page.wait_for_function("() => document.getElementById('scenePrompt').value !== ''", timeout=3000)
        page.locator("#btnRedo").click()
        page.locator("#scenePrompt").fill(edited_prompt)

        # Save changes & verify persisted in backend JSON
        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)
        saved_doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_doc["scenes"][0]["prompt"], edited_prompt)

        # Edit again then click Discard Changes -> reverts cleanly
        page.locator("#scenePrompt").fill("Temporary unsaved scratch text")
        env["dialog_answers"].append(True)
        page.locator("#btnDiscardChanges").click()
        page.wait_for_function(
            "expected => document.getElementById('scenePrompt').value === expected",
            arg=edited_prompt,
            timeout=5000,
        )

        # Reload browser and confirm saved prompt survived full reload
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        self.assertEqual(page.locator("#scenePrompt").input_value(), edited_prompt)

        # Click + New Project -> create isolated new project via prompt
        env["dialog_answers"].append("pw_brand_new")
        page.locator("#btnNewProject").click()
        page.wait_for_function("() => window.state?.currentSlug === 'pw_brand_new'", timeout=10000)
        self.assertTrue((env["projects_dir"] / "pw_brand_new" / "pw_brand_new.json").is_file())
        self.assertEqual(env["page_errors"], [])
        self.assertNotEqual(orig_prompt, edited_prompt)

    def test_02_uj2_add_split_lock_unlock_and_delete_scene(self):
        env = self._start_studio_session()
        page = env["page"]

        # Add Scene via button
        page.locator("#btnAddScene").click()
        page.wait_for_function("() => window.state.projectData.scenes.length === 3", timeout=5000)

        # Select S01 and Lock -> verify Inspector + Scenes tab lock state
        page.evaluate("() => window.selectScene(0)")
        page.locator("#btnToggleSceneLock").click()
        self.assertTrue(page.locator("#scenePrompt").is_disabled())
        self.assertTrue(page.locator("#btnDeleteScene").is_disabled())

        page.locator('.tab-btn[data-tab="scenes"]').click()
        first_card_lock = page.locator('#tab-scenes .scene-card[data-scene-idx="0"] [data-action="lock"]')
        self.assertTrue(first_card_lock.is_visible())
        page.locator('.tab-btn[data-tab="timeline"]').click()

        # Clicking Split on a locked scene shows lock toast instead of doing nothing
        self.assertFalse(page.locator("#btnSplitSceneHeader").is_disabled())
        page.locator("#btnSplitSceneHeader").click()
        page.locator("#toastMessage.show").wait_for(state="visible", timeout=4000)

        # Unlock S01 -> controls become enabled
        page.locator("#btnToggleSceneLock").click()
        self.assertFalse(page.locator("#scenePrompt").is_disabled())

        # Split S01 at 1.50s via in-app Split Scene modal
        page.locator("#btnSplitSceneHeader").click()
        page.locator("#splitSceneModal").wait_for(state="visible", timeout=5000)
        page.locator("#splitTimeInput").fill("1.50")
        page.locator("#btnConfirmSplitScene").click()
        page.locator("#splitSceneModal").wait_for(state="hidden", timeout=5000)
        page.wait_for_function("() => window.state.projectData.scenes.length === 4", timeout=8000)

        # Delete last scene via #btnDeleteScene + confirm
        page.evaluate("() => window.selectScene(window.state.projectData.scenes.length - 1)")
        env["dialog_answers"].append(True)
        page.locator("#btnDeleteScene").click()
        page.wait_for_function("() => window.state.projectData.scenes.length === 3", timeout=8000)
        self.assertEqual(env["page_errors"], [])

    def test_03_uj3_cue_markers_modal_modular_prompts_and_negative_mode(self):
        env = self._start_studio_session()
        page = env["page"]

        # Open Marker modal and save marker with nudge + second color swatch
        page.locator("#btnAddMarker").click()
        page.locator("#markerModal").wait_for(state="visible", timeout=5000)
        page.locator("#markerNameInput").fill("Chorus Hit")
        page.locator("#btnMarkerNudgeUp").click()
        page.locator("#markerColorPalette .color-swatch").nth(1).click()
        page.locator("#btnSaveMarker").click()
        page.locator("#markerModal").wait_for(state="hidden", timeout=5000)
        page.wait_for_function("() => (window.state.projectData.markers || []).some(m => m.name === 'Chorus Hit')", timeout=5000)

        # Select S01 on timeline canvas so Inspector opens
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")

        # Switch Prompt Mode -> Modular Components and edit Camera field
        page.locator("#btnPromptModeComponents").click()
        page.locator("#modularPromptsGroup").wait_for(state="visible", timeout=3000)
        page.locator("#compCamera").fill("Slow dolly tracking left to right")
        page.locator("#compCamera").dispatch_event("input")

        # Expand Negative Prompts collapsible section, select override mode and fill prompt
        page.locator('[data-insp-header="negatives"]').click()
        page.locator("#sceneNegativeMode").wait_for(state="visible", timeout=3000)
        page.locator("#sceneNegativeMode").select_option("override")
        page.locator("#sceneNegativePrompt").fill("static frame, text overlay")
        page.locator("#sceneNegativePrompt").dispatch_event("input")

        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)
        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        self.assertTrue(any(m.get("name") == "Chorus Hit" for m in saved_doc.get("markers", [])))
        self.assertEqual(saved_doc["scenes"][0].get("negative_prompt_mode"), "override")
        self.assertIn("static frame", saved_doc["scenes"][0].get("negative_prompt", ""))
        self.assertEqual(env["page_errors"], [])

    def test_04_uj4_scenes_tab_filters_collapsibles_inline_edit_and_revert(self):
        env = self._start_studio_session()
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        ctx = ProjectContext(env["cfg_path"])
        hashes = compute_project_task_hashes(ctx)
        s01_stem = next(k for k in hashes if k.startswith("S01_"))
        mp4_path = ctx.renders_dir / f"{s01_stem}.mp4"
        _make_dummy_mp4(ffmpeg_bin, mp4_path, duration_s=2.0)
        after_scene_render(
            ctx, ctx.scenes[0], None, s01_stem, mp4_path, hashes[s01_stem][0],
            {"prompt": ctx.scenes[0]["prompt"], "seed": 1001}, 1.0, ctx.load_state_hashes(),
        )

        page = env["page"]
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length === 2", timeout=15000)

        # Open Scenes tab and interact with filter pills, search box & collapsible bar
        page.locator('.tab-btn[data-tab="scenes"]').click()
        page.locator("#tab-scenes .scene-card").first.wait_for(state="visible", timeout=5000)
        self.assertEqual(page.locator("#tab-scenes .scene-card").count(), 2)

        page.locator("#tab-scenes .scenes-search-input").fill("dolly")
        self.assertEqual(page.locator("#tab-scenes .scene-card").count(), 1)
        page.locator("#tab-scenes .scenes-search-input").fill("")
        self.assertEqual(page.locator("#tab-scenes .scene-card").count(), 2)

        page.locator('#tab-scenes .scene-card[data-scene-idx="0"] .scene-collapsible-bar').first.click()

        # Modify S01 prompt inline in Inspector, then Revert to Rendered
        page.locator('.tab-btn[data-tab="timeline"]').click()
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.locator("#scenePrompt").wait_for(state="visible", timeout=5000)
        orig_prompt = page.locator("#scenePrompt").input_value()
        page.locator("#scenePrompt").fill("Altered prompt needing revert")
        page.locator("#btnRevertSceneToRendered").wait_for(state="visible", timeout=5000)
        env["dialog_answers"].append(True)
        page.locator("#btnRevertSceneToRendered").click()
        page.wait_for_function(
            "expected => document.getElementById('scenePrompt').value === expected",
            arg=orig_prompt,
            timeout=8000,
        )
        self.assertEqual(env["page_errors"], [])

    def test_05_uj5_takes_history_restore_delete_and_checkpoints_rollback(self):
        env = self._start_studio_session()
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        ctx = ProjectContext(env["cfg_path"])
        hashes = compute_project_task_hashes(ctx)
        s01_stem = next(k for k in hashes if k.startswith("S01_"))
        mp4_1 = ctx.renders_dir / f"{s01_stem}.mp4"

        # Record Take 1 (seed 1001) and Take 2 (seed 2222)
        state_hashes = ctx.load_state_hashes()
        _make_dummy_mp4(ffmpeg_bin, mp4_1, duration_s=1.5, color="navy")
        after_scene_render(
            ctx, {**ctx.scenes[0], "seed": 1001}, None, s01_stem, mp4_1,
            "hash_take_1", {"prompt": ctx.scenes[0]["prompt"], "seed": 1001}, 1.0, state_hashes,
        )
        _make_dummy_mp4(ffmpeg_bin, mp4_1, duration_s=1.5, color="purple")
        after_scene_render(
            ctx, {**ctx.scenes[0], "seed": 2222}, None, s01_stem, mp4_1,
            hashes[s01_stem][0], {"prompt": ctx.scenes[0]["prompt"], "seed": 2222}, 1.2, state_hashes,
        )

        page = env["page"]
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.wait_for_function("() => document.querySelectorAll('#sceneTakesList .take-list-row').length === 2", timeout=15000)

        # Click Preview on Take 1 (second row since takes are reversed newest-first), then Restore Take as Active
        rows = page.locator("#sceneTakesList .take-list-row")
        rows.nth(1).locator(".btn-take-preview").click()
        page.locator("#btnRestoreTake").wait_for(state="visible", timeout=5000)
        page.locator("#btnRestoreTake").click()
        page.wait_for_function("() => document.getElementById('sceneSeed').value === '1001'", timeout=8000)

        # Delete inactive Take 2 via delete button + confirm
        env["dialog_answers"].append(True)
        page.locator("#sceneTakesList .take-list-row").first.locator("button:has-text('🗑️')").click()
        page.wait_for_function("() => document.querySelectorAll('#sceneTakesList .take-list-row').length === 1", timeout=8000)

        # Checkpoints modal: Create checkpoint -> Edit prompt -> Save -> Rollback checkpoint
        page.locator("#btnCheckpointsModal").click()
        page.locator("#checkpointsModal").wait_for(state="visible", timeout=5000)
        env["dialog_answers"].extend(["Pre-Change Snapshot", "Before experiment"])
        page.locator("#btnCreateCheckpoint").click()
        page.locator("#checkpointsList .checkpoint-item").first.wait_for(state="visible", timeout=8000)
        page.locator("#btnCloseCheckpointsModal").click()

        before_edit = page.locator("#scenePrompt").input_value()
        page.locator("#scenePrompt").fill("Experimental post-checkpoint prompt")
        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)

        page.locator("#btnCheckpointsModal").click()
        env["dialog_answers"].append(True)
        page.locator("#checkpointsList .checkpoint-item button:has-text('Rollback')").first.click()
        page.locator("#checkpointsModal").wait_for(state="hidden", timeout=8000)
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.wait_for_function(
            "expected => document.getElementById('scenePrompt').value === expected",
            arg=before_edit,
            timeout=10000,
        )
        self.assertEqual(env["page_errors"], [])

    def test_06_uj6_characters_studio_add_edit_save_reload(self):
        env = self._start_studio_session()
        page = env["page"]

        page.locator('.tab-btn[data-tab="characters"]').click()
        page.locator("#characterRosterList").wait_for(state="visible", timeout=5000)

        page.locator("#btnNewCharacter").click()
        page.locator("#charInputModalOverlay").wait_for(state="visible", timeout=5000)
        page.locator("#charModalInput").fill("Elena")
        page.locator("#btnCharModalConfirm").click()
        page.locator("#charInputModalOverlay").wait_for(state="hidden", timeout=5000)
        page.locator("#characterRosterList").get_by_text("Elena", exact=True).wait_for(state="visible", timeout=5000)

        # Simulate OS/browser focus return right after dialog close -> must NOT wipe unsaved character!
        page.evaluate("() => window.dispatchEvent(new Event('focus'))")
        page.wait_for_timeout(300)
        self.assertTrue(page.locator("#characterRosterList").get_by_text("Elena", exact=True).is_visible())

        page.locator("#editEntityTraits").fill("concert violinist in emerald velvet gown")
        page.locator("#editEntityTraits").dispatch_event("input")

        page.locator("#btnAddLook").click()
        page.locator("#charInputModalOverlay").wait_for(state="visible", timeout=5000)
        page.locator("#charModalInput").fill("concert")
        page.locator("#btnCharModalConfirm").click()
        page.locator("#charInputModalOverlay").wait_for(state="hidden", timeout=5000)

        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=8000)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)
        page.locator('.tab-btn[data-tab="characters"]').click()
        page.locator("#characterRosterList").get_by_text("Elena", exact=True).click()
        self.assertIn("emerald velvet gown", page.locator("#editEntityTraits").input_value())
        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        elena_ent = next((c for c in (saved_doc.get("characters") or {}).values() if c.get("name") == "Elena"), None)
        self.assertIsNotNone(elena_ent)
        self.assertIn("concert", elena_ent.get("looks", {}))
        self.assertEqual(env["page_errors"], [])

    def test_07_uj7_anchors_and_settings_tabs_save_persist(self):
        env = self._start_studio_session()
        page = env["page"]

        # Anchors tab: verify A01 card renders and scan anchors button works
        page.locator('.tab-btn[data-tab="anchors"]').click()
        first_card = page.locator("#anchorCardsContainer .anchor-card").first
        first_card.wait_for(state="visible", timeout=5000)
        self.assertIn("A01", first_card.inner_text())
        page.locator("#btnAddAnchor").click()

        # Settings tab: update Project Name, FPS & select Resolution Option 2
        page.locator('.tab-btn[data-tab="settings"]').click()
        page.locator("#cfgProjectName").fill("Updated Studio Title")
        page.locator("#cfgProjectName").dispatch_event("input")
        page.locator("#cfgFps").fill("25")
        page.locator("#cfgFps").dispatch_event("input")
        page.locator("#resolutionCardsContainer .res-card").nth(1).click()

        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)
        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        self.assertEqual(saved_doc["project"]["name"], "Updated Studio Title")
        self.assertEqual(int(saved_doc["project"]["fps"]), 25)
        self.assertEqual(saved_doc["models"]["default_video_resolution"], 2)
        self.assertEqual(env["page_errors"], [])

    def test_08_uj8_render_console_dummy_render_and_master_restitch(self):
        env = self._start_studio_session(dev=True)
        page = env["page"]

        # Open Console tab, enable Dummy Mode, verify pending queue, Reorder/Remove, then Queue Missing & Render
        page.locator('.tab-btn[data-tab="console"]').click()
        page.locator("#chkDummyRenderMode").wait_for(state="visible", timeout=5000)
        page.locator("#chkDummyRenderMode").check()
        page.wait_for_function("() => document.querySelectorAll('#queueTableBody tr .btn-queue-del').length >= 2", timeout=8000)

        # Move second queued item up, then remove the first queued item
        page.locator("#queueTableBody tr").nth(1).locator(".btn-queue-up").click()
        page.wait_for_function(
            "() => (document.querySelector('#queueTableBody tr')?.dataset?.unitSid || '') === 'S02'",
            timeout=5000,
        )
        page.locator("#queueTableBody tr").first.locator(".btn-queue-del").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#queueTableBody tr .btn-queue-del').length === 1",
            timeout=5000,
        )

        # Queue Missing / Changed re-enqueues remaining missing scene & starts Dummy Render worker
        page.locator("#btnQueueAll").click()
        # Wait for dummy renders of both scenes (approx ~4-8s) to finish
        page.wait_for_function(
            "() => Number(document.getElementById('countDone')?.textContent || '0') >= 2 && !window.state?.isEngineRunning",
            timeout=45000,
        )

        # Verify at least S01 dummy MP4 created on disk
        renders_dir = env["proj_dir"] / "work" / "renders_v3"
        self.assertGreaterEqual(len(list(renders_dir.glob("*.mp4"))), 2)

        # Switch to Master Video tab and click Restitch Master Video
        page.locator('.tab-btn[data-tab="master"]').click()
        page.locator("#btnStitchMasterFromTab").click()
        page.wait_for_function(
            "() => (document.getElementById('masterVideoPlayer')?.getAttribute('src') || '').includes('/media/') "
            "&& document.getElementById('masterStitchOverlay')?.style.display === 'none'",
            timeout=30000,
        )
        master_mp4s = list(env["proj_dir"].glob("*_Master_*.mp4"))
        self.assertEqual(len(master_mp4s), 1)
        self.assertGreater(master_mp4s[0].stat().st_size, 5000)
        self.assertEqual(env["page_errors"], [])

    def test_09_uj9_duet_roi_clip_import_repair_and_upscale_controls(self):
        env = self._start_studio_session()
        page = env["page"]
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)

        # Select S01 and switch to Two-Pass Duet mode
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.locator("#sceneMode").select_option("duet_method_b")
        page.locator("#duetPassEngineRow").wait_for(state="visible", timeout=5000)

        def _accept_combo_warn_if_open():
            btn = page.locator("#btnComboWarnConfirm")
            try:
                btn.wait_for(state="visible", timeout=800)
                btn.click()
            except Exception:
                pass

        # Configure per-pass engines & custom Pass 2 ROI box + split ratio
        page.locator("#sceneBackendM").select_option("local")
        _accept_combo_warn_if_open()
        page.locator("#sceneBackendF").select_option("fal")
        _accept_combo_warn_if_open()
        page.locator("#scenePass2Roi").select_option("custom")
        _accept_combo_warn_if_open()

        page.locator("#scenePass2RoiBox").wait_for(state="visible", timeout=3000)
        page.locator("#scenePass2RoiBox").fill("0.50,0.08,0.96,0.88")
        page.locator("#sceneSplitX").fill("0.48")
        page.locator("#sceneSplitX").dispatch_event("input")

        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)
        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        sc0 = saved_doc["scenes"][0]
        self.assertEqual(sc0.get("mode"), "duet_method_b")
        self.assertEqual(sc0.get("backend_m"), "local")
        self.assertEqual(sc0.get("backend_f"), "fal")
        self.assertEqual(sc0.get("pass2_roi_mode"), "custom")
        self.assertEqual(sc0.get("pass2_roi_box"), [0.5, 0.08, 0.96, 0.88])
        self.assertAlmostEqual(float(sc0.get("split_x")), 0.48, places=2)

        # Import external clip via #clipImportFileInput -> becomes Active Take in Vault
        ext_mp4 = env["tmp_root"] / "external_veo_flow_clip.mp4"
        _make_dummy_mp4(ffmpeg_bin, ext_mp4, duration_s=3.0, color="teal")
        page.locator("#clipImportFileInput").set_input_files(str(ext_mp4))
        page.wait_for_function(
            "() => document.querySelectorAll('#sceneTakesList .take-list-row').length >= 1 "
            "&& (document.getElementById('clipStatusBadge')?.textContent || '') !== 'Missing'",
            timeout=15000,
        )

        # Open & configure Lip-Sync Repair bar on imported Active Take
        page.locator("#btnToggleLipSyncRepair").wait_for(state="visible", timeout=5000)
        page.locator("#btnToggleLipSyncRepair").click()
        page.locator("#lipSyncRepairBar").wait_for(state="visible", timeout=5000)
        page.locator("#repSide").select_option("custom")
        page.locator("#repBoxWrap").wait_for(state="visible", timeout=3000)
        page.locator("#repRoiBox").fill("0.45,0.05,0.95,0.85")
        page.locator("#repBackend").select_option("fal")
        page.locator("#btnCloseLipSyncRepair").click()
        page.locator("#lipSyncRepairBar").wait_for(state="hidden", timeout=3000)

        # Verify Late AI Upscale & 1x Filter backend selector controls inside Takes tray
        page.locator("#sceneLateUpscaleMethod").select_option("flashvsrplus*2")
        page.locator("#sceneLateUpscaleBackend").select_option("fal")
        self.assertEqual(page.locator("#sceneLateUpscaleMethod").input_value(), "flashvsrplus*2")
        self.assertEqual(page.locator("#sceneLateUpscaleBackend").input_value(), "fal")
        self.assertEqual(env["page_errors"], [])

    def test_10_uj10_jump_bar_loop_filters_cloud_settings_and_engine_override(self):
        env = self._start_studio_session()
        page = env["page"]

        # Quick-Jump Bar selection & Master Scene Loop pill
        jump_btns = page.locator("#sceneJumpBar .scene-jump-btn")
        self.assertEqual(jump_btns.count(), 2)
        jump_btns.nth(1).click()
        page.wait_for_function("() => document.getElementById('inspectorSceneId')?.textContent?.includes('S02')", timeout=5000)
        jump_btns.nth(0).locator(".scene-jump-repeat").click()
        page.locator("#masterLoopBadge").wait_for(state="visible", timeout=5000)
        self.assertIn("S01", page.locator("#masterLoopText").inner_text())
        page.locator("#btnClearMasterLoop").click()
        page.locator("#masterLoopBadge").wait_for(state="hidden", timeout=3000)

        # Back to Timeline -> expand 1x Refiner & Look Filters collapsible section
        page.locator('.tab-btn[data-tab="timeline"]').click()
        page.locator('[data-insp-header="filters"]').click()
        page.locator("#sceneFilterAiRefiner").wait_for(state="visible", timeout=5000)
        page.locator("#sceneFilterAiRefiner").select_option("h3facerefine")
        page.locator("#sceneFilterFilmGrain").fill("0.15")
        page.locator("#sceneFilterFilmGrain").dispatch_event("change")
        page.locator("#sceneFilterDeband").select_option("true")
        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)

        # Settings tab -> verify Wan 2.2 (#17/#18) & fal scenic presets in model dropdowns + Cloud settings
        page.locator('.tab-btn[data-tab="settings"]').click()
        renderer_vals = page.eval_on_selector_all(
            "#cfgDefaultVideoRenderer option",
            "opts => opts.map(o => o.value)",
        )
        self.assertIn("17", renderer_vals)
        self.assertIn("18", renderer_vals)
        self.assertIn("fal_kling_v3_pro", renderer_vals)
        self.assertIn("fal_veo31_fast", renderer_vals)
        page.locator("#falSettingsSection").wait_for(state="visible", timeout=5000)

        # Render Console -> per-row engine override, lane toggle & Logs/Queue switch
        page.locator('.tab-btn[data-tab="console"]').click()
        page.wait_for_function("() => document.querySelectorAll('#queueTableBody .queue-engine-select').length >= 1", timeout=8000)
        first_eng_sel = page.locator("#queueTableBody .queue-engine-select").first
        first_eng_sel.select_option("fal")
        page.wait_for_function(
            "() => document.querySelector('#queueTableBody .queue-engine-select')?.classList.contains('is-override')",
            timeout=5000,
        )
        fal_chk = page.locator('.chk-engine-run[data-engine="fal"]')
        was_checked = fal_chk.is_checked()
        fal_chk.set_checked(not was_checked)
        fal_chk.set_checked(was_checked)

        page.locator("#btnConsoleLogs").click()
        page.locator("#consoleLogsView").wait_for(state="visible", timeout=3000)
        page.locator("#terminalOutput").wait_for(state="attached", timeout=3000)
        page.locator("#btnConsoleQueue").click()

        # Master tab -> change Master Delivery Resolution override and save
        page.locator('.tab-btn[data-tab="master"]').click()
        page.locator("#masterTabResSelect").select_option("1920x1080")
        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=5000)

        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        self.assertEqual(saved_doc["project"].get("master_resolution"), "1920x1080")
        self.assertEqual(saved_doc["scenes"][0].get("filters", {}).get("ai_refiners"), ["h3facerefine"])
        self.assertAlmostEqual(float(saved_doc["scenes"][0].get("filters", {}).get("film_grain_intensity")), 0.15, places=2)
        self.assertTrue(saved_doc["scenes"][0].get("filters", {}).get("ffmpeg", {}).get("deband"))
        self.assertEqual(env["page_errors"], [])

    def test_11_layout_no_overlaps_or_clipping_across_modes(self):
        env = self._start_studio_session()
        page = env["page"]
        page.set_viewport_size({"width": 2560, "height": 1440})

        # Select S01 and open Split View with Render Console
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.locator('.tab-btn[data-tab="console"]').click()
        page.locator("#workspaceSplitter").wait_for(state="visible", timeout=5000)

        # 1. Verify draggable splitter resizes #timelineBottom and persists, and double-click resets
        initial_w = page.evaluate("() => document.getElementById('timelineBottom').getBoundingClientRect().width")
        sbox = page.locator("#workspaceSplitter").bounding_box()
        page.mouse.move(sbox["x"] + sbox["width"] / 2, sbox["y"] + sbox["height"] / 2)
        page.mouse.down()
        page.mouse.move(sbox["x"] + 160, sbox["y"] + sbox["height"] / 2, steps=5)
        page.mouse.up()
        after_drag_w = page.evaluate("() => document.getElementById('timelineBottom').getBoundingClientRect().width")
        self.assertGreater(after_drag_w - initial_w, 90)
        self.assertIsNotNone(page.evaluate("() => localStorage.getItem('mvg_split_left_pct')"))

        page.locator("#workspaceSplitter").dblclick()
        reset_w = page.evaluate("() => document.getElementById('timelineBottom').getBoundingClientRect().width")
        self.assertLess(abs(reset_w - initial_w), 20)

        # 2. Check all 4 scene modes in Split View (2560x1440): zero Row 1 overlaps & extracted Duet ROI bar
        for mode_val in ("solo", "duet_method_a", "duet_method_b", "silent_locked"):
            page.locator("#sceneMode").select_option(mode_val)
            page.wait_for_timeout(120)
            row1_rects = page.evaluate("""() => {
              const items = Array.from(document.querySelectorAll('.inspector-row-visuals > .form-group'));
              return items.map(el => {
                const r = el.getBoundingClientRect();
                return { id: el.id || el.className, x: r.left, y: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height };
              }).filter(o => o.w > 0 && o.h > 0);
            }""")
            self.assertEqual(len(row1_rects), 6, f"Expected 6 visible Row 1 controls in mode={mode_val}")
            for i in range(len(row1_rects)):
                for j in range(i + 1, len(row1_rects)):
                    a, b = row1_rects[i], row1_rects[j]
                    x_ov = min(a["r"], b["r"]) - max(a["x"], b["x"])
                    y_ov = min(a["b"], b["b"]) - max(a["y"], b["y"])
                    self.assertFalse(x_ov > 2 and y_ov > 2, f"Row 1 overlap in {mode_val}: {a} vs {b}")

            if mode_val == "duet_method_b":
                host_ok = page.evaluate("""() => {
                  const r = document.getElementById('duetPassEngineRow');
                  return r && r.parentElement?.id === 'duetPassEngineHost' && r.getBoundingClientRect().height < 140;
                }""")
                self.assertTrue(host_ok, "duetPassEngineRow must mount compactly inside #duetPassEngineHost")
                # Even with Prompts accordion collapsed, Split Boundary X stays visible in Row 2 & >=75px wide
                split_info = page.evaluate("""() => {
                  const sx = document.getElementById('sceneSplitX');
                  const r = sx.getBoundingClientRect();
                  return { visible: r.width >= 75 && r.height >= 20, offsetW: sx.offsetWidth };
                }""")
                self.assertTrue(split_info["visible"], f"Split X input too narrow or hidden: {split_info}")

        # 3. Viewport-clamped Anchor Dropdown check
        page.locator("#btnAnchorDropdownTrigger").click()
        menu_bounds = page.evaluate("""() => {
          const m = document.getElementById('anchorDropdownMenu');
          const r = m.getBoundingClientRect();
          return { open: m.classList.contains('open'), left: r.left, right: r.right, top: r.top, bottom: r.bottom, vw: window.innerWidth, vh: window.innerHeight };
        }""")
        self.assertTrue(menu_bounds["open"])
        self.assertGreaterEqual(menu_bounds["left"], 0)
        self.assertLessEqual(menu_bounds["right"], menu_bounds["vw"])
        self.assertLessEqual(menu_bounds["bottom"], menu_bounds["vh"])
        page.keyboard.press("Escape")

        # 4. Render Console Queue row cells have zero horizontal overlap
        page.wait_for_function("() => document.querySelectorAll('#queueTableBody tr').length >= 1", timeout=8000)
        q_overlap = page.evaluate("""() => {
          const row = document.querySelector('#queueTableBody tr');
          const cells = Array.from(row.querySelectorAll('td')).map(td => {
            const r = td.getBoundingClientRect();
            return { x: r.left, r: r.right, w: r.width };
          }).filter(c => c.w > 0);
          for (let i = 0; i + 1 < cells.length; i++) {
            if (cells[i].r > cells[i + 1].x + 1) return [cells[i], cells[i + 1]];
          }
          return null;
        }""")
        self.assertIsNone(q_overlap, f"Queue columns overlap: {q_overlap}")

        # 5. Master Video Tab maintains landscape container ratio
        page.locator('.tab-btn[data-tab="master"]').click()
        mv = page.evaluate("""() => {
          const v = document.getElementById('masterVideoPlayer');
          const r = v.getBoundingClientRect();
          return { w: r.width, h: r.height };
        }""")
        self.assertGreaterEqual(mv["w"], mv["h"])
        self.assertGreater(mv["w"], 250)

        # 6. Timeline Fit zoom & Native 4K (3840x2160) modal scaling
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.locator("#btnTimelineFit").click()
        fit_zoom = page.evaluate("() => window.state.zoom")
        self.assertGreaterEqual(fit_zoom, 0.2)
        self.assertLessEqual(fit_zoom, 3.5)

        page.locator("#btnCheckpointsModal").click()
        cp_w = page.evaluate("() => document.querySelector('#checkpointsModal > div').getBoundingClientRect().width")
        self.assertGreaterEqual(cp_w, 750)
        page.locator("#btnCloseCheckpointsModal").click()
        self.assertEqual(env["page_errors"], [])


if __name__ == "__main__":
    unittest.main()
