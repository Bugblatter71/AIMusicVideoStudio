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
from pipeline.takes import after_scene_render, before_scene_render
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
        "work/anchors",
        "work/audio",
        "work/jobs",
        "work/renders",
        "work/trimmed",
        "work/frames",
    ):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)

    _write_wav(proj_dir / "audio" / "master.wav", duration_s=6.0)
    _write_wav(proj_dir / "audio" / "male.wav", duration_s=6.0)
    _write_wav(proj_dir / "audio" / "female.wav", duration_s=6.0)

    a01_path = proj_dir / "work" / "anchors" / "A01_Lead.jpg"
    a02_path = proj_dir / "work" / "anchors" / "A02_Wide.jpg"
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
                "anchors": "work/anchors",
                "audio_slices": "work/audio",
                "masks": "work/masks",
                "jobs": "work/jobs",
                "renders": "work/renders",
                "trimmed": "work/trimmed",
                "review_strips": "work/frames",
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
        with server.render_lock:
            server.log_history.clear()
            server.log_seq_slugs.clear()
            server.log_seq_counter = 0
            server._run_log_seq_floor = 0
            server.render_status.update({
                "running": False, "exit_code": None, "current_project": None, "args": [], "start_time": None,
            })

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
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2 && window.state?._projectLoadedSeq > 0 && window.state._projectLoadedSeq === window.state._projectLoadSeq", timeout=15000)

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
        page.wait_for_function("() => window.state?._projectLoadedSeq > 0 && window.state._projectLoadedSeq === window.state._projectLoadSeq", timeout=10000)
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

        # Verify Take 1 stayed active ("★ ACTIVE") after Take 2 rendered; click Preview -> Restore Take 2, then Restore Take 1
        rows = page.locator("#sceneTakesList .take-list-row")
        self.assertIn("ACTIVE", rows.nth(1).inner_text())
        self.assertIn("Use", rows.nth(0).inner_text())
        rows.nth(0).locator(".btn-take-preview").click()
        page.locator("#btnRestoreTake").wait_for(state="visible", timeout=5000)
        page.locator("#btnRestoreTake").click()
        page.wait_for_function("() => document.getElementById('sceneSeed').value === '2222'", timeout=8000)

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
        renders_dir = env["proj_dir"] / "work" / "renders"
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

        # Configure per-pass models, engines & custom Pass 2 ROI box + split ratio
        page.locator("#sceneRendererM").select_option("9")
        _accept_combo_warn_if_open()
        page.locator("#sceneRendererF").select_option("14")
        _accept_combo_warn_if_open()
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
        self.assertEqual(sc0.get("renderer_m"), 9)
        self.assertEqual(sc0.get("renderer_f"), 14)
        self.assertEqual(sc0.get("backend_m"), "local")
        self.assertEqual(sc0.get("backend_f"), "fal")
        self.assertEqual(sc0.get("pass2_roi_mode"), "custom")
        self.assertEqual(sc0.get("pass2_roi_box"), [0.5, 0.08, 0.96, 0.88])
        self.assertAlmostEqual(float(sc0.get("split_x")), 0.48, places=2)

        # Verify Scenes Tab Two-Pass sub-row preserves per-pass model/engine/stem/seed controls
        page.locator('.tab-btn[data-tab="scenes"]').click()
        page.wait_for_selector("#tab-scenes .scenes-duet-pass-row", timeout=5000)
        scenes_two_pass = page.evaluate("""() => {
          const row = document.querySelector('#tab-scenes .scenes-duet-pass-row');
          if (!row) return null;
          const lbls = Array.from(row.querySelectorAll('label')).map(l => l.textContent.trim());
          return {
            hasP1: Boolean(row.querySelector('.scenes-duet-pass-card--p1')),
            hasP2: Boolean(row.querySelector('.scenes-duet-pass-card--p2')),
            lbls,
          };
        }""")
        self.assertIsNotNone(scenes_two_pass, "Scenes Tab two-pass sub-row missing")
        self.assertTrue(scenes_two_pass["hasP1"] and scenes_two_pass["hasP2"])
        self.assertIn("P1 Model", scenes_two_pass["lbls"])
        self.assertIn("P2 Model", scenes_two_pass["lbls"])
        page.locator('.tab-btn[data-tab="timeline"]').click()

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
                  const c1 = r?.querySelector('.duet-pass-card--p1')?.getBoundingClientRect();
                  const c2 = r?.querySelector('.duet-pass-card--p2')?.getBoundingClientRect();
                  const m1 = document.getElementById('sceneRendererM')?.getBoundingClientRect();
                  const m2 = document.getElementById('sceneRendererF')?.getBoundingClientRect();
                  const cardsSeparate = Boolean(c1 && c2 && c1.right <= c2.left + 1);
                  const modelsVis = Boolean(m1 && m2 && m1.width > 50 && m2.width > 50);
                  return r && r.parentElement?.id === 'duetPassEngineHost' && r.getBoundingClientRect().height < 140 && cardsSeparate && modelsVis;
                }""")
                self.assertTrue(host_ok, "duetPassEngineRow must mount side-by-side Pass cards with model selectors (<140px tall) inside #duetPassEngineHost")
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

        # 6. Timeline Fit zoom, Characters adaptive grid & Native 4K (3840x2160) scaling
        # Verify short 6s song auto-fitted on load and Characters tab adapts smoothly in narrow split pane
        self.assertGreater(page.evaluate("() => window.state.zoom"), 1.0)
        page.locator('.tab-btn[data-tab="characters"]').click()
        char_cols = page.evaluate("""() => getComputedStyle(document.getElementById('charactersStudioLayout')).gridTemplateColumns.split(' ').length""")
        self.assertGreaterEqual(char_cols, 1)

        page.set_viewport_size({"width": 3840, "height": 2160})
        page.locator("#btnTimelineFit").click()
        fit_zoom = page.evaluate("() => window.state.zoom")
        self.assertGreaterEqual(fit_zoom, 0.2)
        self.assertLessEqual(fit_zoom, 3.5)
        canvas_h = page.evaluate("() => document.getElementById('waveformCanvas').height")
        self.assertGreaterEqual(canvas_h, 218)

        page.locator("#btnCheckpointsModal").click()
        cp_w = page.evaluate("() => document.querySelector('#checkpointsModal > div').getBoundingClientRect().width")
        self.assertGreaterEqual(cp_w, 750)
        page.locator("#btnCloseCheckpointsModal").click()
        self.assertEqual(env["page_errors"], [])

    def test_12_done_queue_preserves_completed_history_on_requeue(self):
        env = self._start_studio_session()
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        ctx = ProjectContext(env["cfg_path"])
        hashes = compute_project_task_hashes(ctx)
        s01_stem = next(k for k in hashes if k.startswith("S01_"))
        mp4_1 = ctx.renders_dir / f"{s01_stem}.mp4"
        _make_dummy_mp4(ffmpeg_bin, mp4_1, duration_s=1.5, color="navy")
        sh = ctx.load_state_hashes()
        sh.setdefault("tasks", {})[s01_stem] = hashes[s01_stem][0]
        after_scene_render(
            ctx, ctx.scenes[0], None, s01_stem, mp4_1,
            hashes[s01_stem][0], {"prompt": ctx.scenes[0]["prompt"], "seed": 1001}, 4.5, sh,
        )
        ctx.save_state_hashes(sh)

        page = env["page"]
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length >= 2", timeout=15000)

        page.locator('.tab-btn[data-tab="console"]').click()
        page.wait_for_function("() => Number(document.getElementById('countDone')?.textContent || '0') >= 1", timeout=10000)
        page.locator("#btnFilterDone").click()
        page.wait_for_function(
            "stem => document.getElementById('queueTableBody')?.innerText?.includes(stem)",
            arg=f"{s01_stem}.mp4",
            timeout=5000,
        )

        # Edit S01 prompt & save so S01 becomes re-queued into Pending
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.locator("#scenePrompt").fill("Updated S01 prompt to force re-queue")
        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=8000)

        # Verify S01 is in Pending AND STILL remains in Done queue (including across full reload)
        page.locator("#btnFilterQueued").click()
        page.wait_for_function(
            "stem => document.getElementById('queueTableBody')?.innerText?.includes(stem)",
            arg=f"{s01_stem}.mp4",
            timeout=5000,
        )
        page.locator("#btnFilterDone").click()
        page.wait_for_function(
            "stem => Number(document.getElementById('countDone')?.textContent || '0') >= 1 && document.getElementById('queueTableBody')?.innerText?.includes(stem)",
            arg=f"{s01_stem}.mp4",
            timeout=5000,
        )

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => Number(document.getElementById('countDone')?.textContent || '0') >= 1", timeout=15000)
        page.locator('.tab-btn[data-tab="console"]').click()
        page.locator("#btnFilterDone").click()
        page.wait_for_function(
            "stem => document.getElementById('queueTableBody')?.innerText?.includes(stem)",
            arg=f"{s01_stem}.mp4",
            timeout=5000,
        )
        self.assertEqual(env["page_errors"], [])

    def test_13_sort_queued_list_by_scene_engine_and_length(self):
        env = self._start_studio_session()
        page = env["page"]
        cfg = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        # Add S03 (short, fal) after S01 (long 2s, local) & S02 (4s, minimax)
        cfg["scenes"] = [
            {**cfg["scenes"][0], "id": "S01", "start": 0.0, "end": 3.0, "backend": "minimax"},
            {**cfg["scenes"][1], "id": "S02", "start": 3.0, "end": 4.2, "backend": "local"},
            {**cfg["scenes"][0], "id": "S03", "start": 4.2, "end": 6.0, "backend": "fal"},
        ]
        env["cfg_path"].write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length === 3", timeout=15000)

        page.locator('.tab-btn[data-tab="console"]').click()
        page.wait_for_function("() => document.querySelectorAll('#queueTableBody tr[data-id]').length === 3", timeout=10000)

        row_sids = lambda: page.evaluate("() => Array.from(document.querySelectorAll('#queueTableBody tr[data-id]')).map(r => r.dataset.unitSid)")

        # Sort by Length ascending -> S02 (1.2s), S03 (1.8s), S01 (3.0s)
        for sel, exp in [
            ("#btnSortLength", ["S02", "S03", "S01"]),
            ("#btnSortLength", ["S01", "S03", "S02"]),
            ("#btnSortEngine", ["S02", "S03", "S01"]),
            ('#queueTableHead th[data-sort="engine"]', ["S01", "S03", "S02"]),
            ("#btnSortScene", ["S01", "S02", "S03"]),
        ]:
            with page.expect_response("**/api/queue/reorder") as r_info:
                page.locator(sel).click()
            self.assertEqual(r_info.value.status, 200)
            self.assertEqual(row_sids(), exp)

        # Persists across full reload via /api/queue/reorder
        page.reload(wait_until="domcontentloaded")
        page.locator('.tab-btn[data-tab="console"]').click()
        page.wait_for_function("() => document.querySelectorAll('#queueTableBody tr[data-id]').length === 3", timeout=10000)
        self.assertEqual(row_sids(), ["S01", "S02", "S03"])
        self.assertEqual(env["page_errors"], [])

    def test_14_duplicate_scene_immediately_after_with_takes(self):
        env = self._start_studio_session()
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        ctx = ProjectContext(env["cfg_path"])
        hashes = compute_project_task_hashes(ctx)
        s01_stem = next(k for k in hashes if k.startswith("S01_"))
        mp4_1 = ctx.renders_dir / f"{s01_stem}.mp4"
        sh = ctx.load_state_hashes()

        _make_dummy_mp4(ffmpeg_bin, mp4_1, duration_s=1.5, color="navy")
        after_scene_render(
            ctx, {**ctx.scenes[0], "seed": 1001}, None, s01_stem, mp4_1,
            "hash_dup_t1", {"prompt": ctx.scenes[0]["prompt"], "seed": 1001}, 1.0, sh, created_at=1000.0,
        )
        before_scene_render(ctx, ctx.scenes[0], s01_stem, mp4_1, sh)
        _make_dummy_mp4(ffmpeg_bin, mp4_1, duration_s=1.5, color="teal")
        after_scene_render(
            ctx, ctx.scenes[0], None, s01_stem, mp4_1,
            hashes[s01_stem][0], {"prompt": ctx.scenes[0]["prompt"], "seed": 1001}, 1.2, sh, created_at=2000.0,
        )
        sh.setdefault("tasks", {})[s01_stem] = hashes[s01_stem][0]
        sh.setdefault("task_hashes", {})[s01_stem] = hashes[s01_stem][0]
        ctx.save_state_hashes(sh)

        page = env["page"]
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length === 2", timeout=15000)

        # Select S01 on timeline and click Inspector Duplicate button
        self._click_scene_on_canvas(page, time_s=0.5, expected_id="S01")
        page.locator("#btnDuplicateScene").wait_for(state="visible", timeout=5000)
        with page.expect_response("**/api/scene/duplicate") as resp_info:
            page.locator("#btnDuplicateScene").click()
        self.assertEqual(resp_info.value.status, 200)

        # Duplicate appears immediately after S01 in timeline, keeping S02 aligned, and auto-selects S01a with 2 copied takes
        page.wait_for_function(
            "() => (window.state?.projectData?.scenes || []).map(s => s.id).join(',') === 'S01,S01a,S02'",
            timeout=8000,
        )
        page.wait_for_function("() => document.getElementById('takesCountBadge')?.textContent === '2 takes'", timeout=8000)
        times = page.evaluate("() => window.state.projectData.scenes.map(s => [s.id, s.start, s.end])")
        self.assertEqual(times, [["S01", 0.0, 1.5], ["S01a", 1.5, 3.0], ["S02", 3.0, 6.0]])
        self.assertEqual(page.locator("#sceneTakeSelect option").count(), 2)
        self.assertEqual(page.evaluate("() => window.state.isDirty"), False)

        # Duplicate from Scenes tab card button also inserts immediately after clicked scene (S02 -> S02a)
        page.locator('.tab-btn[data-tab="scenes"]').click()
        page.locator('#tab-scenes .scene-card[data-scene-idx="2"]').wait_for(state="visible", timeout=5000)
        with page.expect_response("**/api/scene/duplicate") as resp2_info:
            page.locator('#tab-scenes .scene-card[data-scene-idx="2"] button[title*="Duplicate"]').click()
        self.assertEqual(resp2_info.value.status, 200)
        page.wait_for_function(
            "() => (window.state?.projectData?.scenes || []).map(s => s.id).join(',') === 'S01,S01a,S02,S02a'",
            timeout=8000,
        )

        # Reload confirms persisted order & copied takes on disk
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length === 4", timeout=15000)
        reloaded_ids = page.evaluate("() => window.state.projectData.scenes.map(s => s.id)")
        self.assertEqual(reloaded_ids, ["S01", "S01a", "S02", "S02a"])
        self.assertEqual(env["page_errors"], [])

    def test_15_uj15_anchor_studio_create_edit_prompt_from_scene_and_generate(self):
        env = self._start_studio_session(slug="anchor_studio_e2e", dev=True)
        page = env["page"]

        # Enable Dummy Render Mode from Console tab so Stage 1 generation runs hermetically without GPU
        page.locator('.tab-btn[data-tab="console"]').click()
        page.locator("#chkDummyRenderMode").wait_for(state="visible", timeout=5000)
        page.locator("#chkDummyRenderMode").check()

        # Select S01 on timeline and click "New" anchor button in Scene Inspector
        self._click_scene_on_canvas(page, time_s=1.0, expected_id="S01")
        page.locator("#btnNewAnchorFromCurrentScene").wait_for(state="visible", timeout=5000)
        page.locator("#btnNewAnchorFromCurrentScene").click()

        # Automatically switches to Anchors tab with new A03_S01.jpg card prefilled from S01
        a03_card = page.locator('#anchorCardsContainer .anchor-card[data-aid="A03"]')
        a03_card.wait_for(state="visible", timeout=5000)
        prefilled_prompt = a03_card.locator("[data-afield='prompt']").input_value()
        self.assertTrue(prefilled_prompt.startswith("Cinematic 16:9 still frame,"))
        self.assertEqual(a03_card.locator("[data-afield='filename']").input_value(), "A03_S01.jpg")
        self.assertEqual(page.evaluate("() => window.state.projectData.scenes[0].anchor"), "A03_S01.jpg")

        # Verify Default Anchor Resolution toolbar selector is visible & Default option shows actual resolution
        self.assertEqual(page.locator("#selDefaultAnchorResolution").input_value(), "1920x1080")
        self.assertIn("1920x1080", a03_card.locator("[data-afield='resolution'] option").first.inner_text())

        # Edit prompt, resolution, negative prompt, and randomize seed
        custom_prompt = "Cinematic close-up portrait on misty obsidian rooftop, silver rim lighting"
        a03_card.locator("[data-afield='prompt']").fill(custom_prompt)
        a03_card.locator("[data-afield='prompt']").dispatch_event("input")
        a03_card.locator("[data-afield='resolution']").select_option("1280x720")
        a03_card.locator(".anchor-adv-details summary").click()
        a03_card.locator("[data-afield='negative_prompt']").fill("blurry, distorted face")
        a03_card.locator("[data-afield='negative_prompt']").dispatch_event("input")
        a03_card.locator("[data-aact='randseed']").click()
        new_seed = int(a03_card.locator("[data-afield='seed']").input_value())
        self.assertIn("S01", a03_card.locator(".anchor-scene-usage").inner_text())
        self.assertIn("(1)", page.locator("#btnGenMissingAnchors").inner_text())

        # Duplicate A03 into A04_S01_Copy.jpg, verify cloned settings, then delete A04 from project config
        a03_card.locator("[data-aact='duplicate']").click()
        a04_card = page.locator('#anchorCardsContainer .anchor-card[data-aid="A04"]')
        a04_card.wait_for(state="visible", timeout=5000)
        self.assertEqual(a04_card.locator("[data-afield='prompt']").input_value(), custom_prompt)
        self.assertEqual(a04_card.locator("[data-afield='resolution']").input_value(), "1280x720")
        env["dialog_answers"].append(True)
        a04_card.locator("[data-aact='delete']").click()
        a04_card.wait_for(state="detached", timeout=5000)

        # Select 2 takes (x2 takes) on A03 and generate both takes in dummy mode
        gen_count_inp = a03_card.locator("[data-afield='gen_count']")
        self.assertEqual(gen_count_inp.get_attribute("type"), "number")
        gen_count_inp.fill("12")
        self.assertIn("(12 Takes)", a03_card.locator("button[data-action='generate-anchor']").inner_text())
        gen_count_inp.fill("2")
        a03_card.locator("button[data-action='generate-anchor']").click()
        a03_disk = env["proj_dir"] / "work" / "anchors" / "A03_S01.jpg"
        page.wait_for_function(
            "() => !window.state?.isDirty && !window.state?.isEngineRunning "
            "&& Boolean(window.state?.assets?.anchors?.['A03_S01.jpg']) "
            "&& (window.state?.anchorTakes?.A03?.takes?.length || 0) >= 2",
            timeout=30000,
        )
        self.assertTrue(a03_disk.is_file())
        self.assertGreater(a03_disk.stat().st_size, 1000)
        a03_card.locator(".anchor-take-tile").first.wait_for(state="visible", timeout=5000)
        self.assertEqual(a03_card.locator(".anchor-take-tile").count(), 2)

        # Open the Takes Gallery modal for A03, view Take #1 fullscreen and navigate left/right between takes without losing fullscreen
        a03_card.locator("[data-tact='open-gallery']").click()
        gallery = page.locator("#anchorTakesGalleryModal")
        gallery.wait_for(state="visible", timeout=5000)
        self.assertEqual(gallery.locator(".anchor-gallery-card").count(), 2)

        gallery.locator(".anchor-gallery-card").first.locator("img").click()
        lightbox = page.locator("#anchorTakeLightboxModal")
        lightbox.wait_for(state="visible", timeout=5000)
        self.assertIn("Take #1 of 2", lightbox.locator("#lightboxTakeTitle").inner_text())
        lightbox.locator("#btnLightboxNext").click()
        self.assertIn("Take #2 of 2", lightbox.locator("#lightboxTakeTitle").inner_text())
        page.keyboard.press("ArrowLeft")
        self.assertIn("Take #1 of 2", lightbox.locator("#lightboxTakeTitle").inner_text())
        page.keyboard.press("Escape")
        lightbox.wait_for(state="hidden", timeout=5000)
        self.assertTrue(gallery.is_visible())

        gallery.locator(".anchor-gallery-card").first.locator("[data-gtact='use']").click()
        page.wait_for_function(
            "expected => window.state?.projectData?.anchors?.find(a => a.filename === 'A03_S01.jpg')?.seed === expected",
            arg=new_seed,
            timeout=10000,
        )
        second_gcard = gallery.locator(".anchor-gallery-card").nth(1)
        second_gcard.locator(".gallery-new-anchor-label").fill("Rooftop_Alt")
        second_gcard.locator("[data-gtact='to-anchor']").click()
        page.wait_for_function("() => (window.state?.projectData?.anchors || []).length === 4", timeout=10000)

        env["dialog_answers"].append(True)
        gallery.locator(".anchor-gallery-card").nth(1).locator("[data-gtact='delete']").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#anchorTakesGalleryModal .anchor-gallery-card').length === 1",
            timeout=10000,
        )
        gallery.locator("#btnCloseAnchorGallery").click()
        gallery.wait_for(state="hidden", timeout=5000)
        page.locator('#anchorCardsContainer .anchor-card[data-aid="A04"]').wait_for(state="visible", timeout=10000)

        # Reload browser and confirm edited prompt, seed, resolution, neg prompt, and promoted A04 anchor persist
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.anchors?.length === 4", timeout=15000)
        page.locator('.tab-btn[data-tab="anchors"]').click()
        reloaded_a03 = page.locator('#anchorCardsContainer .anchor-card[data-aid="A03"]')
        reloaded_a03.wait_for(state="visible", timeout=5000)
        self.assertEqual(reloaded_a03.locator("[data-afield='prompt']").input_value(), custom_prompt)
        self.assertEqual(reloaded_a03.locator("[data-afield='resolution']").input_value(), "1280x720")
        self.assertEqual(int(reloaded_a03.locator("[data-afield='seed']").input_value()), new_seed)
        self.assertEqual(reloaded_a03.locator("[data-afield='negative_prompt']").input_value(), "blurry, distorted face")
        self.assertIn("On Disk", reloaded_a03.locator(".anchor-status-pill").inner_text())
        self.assertEqual(env["page_errors"], [])

    def test_16_recreated_scene_id_does_not_inherit_deleted_takes(self):
        env = self._start_studio_session()
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)
        ctx = ProjectContext(env["cfg_path"])
        hashes = compute_project_task_hashes(ctx)
        s02_stem = next(k for k in hashes if k.startswith("S02_"))
        mp4_2 = ctx.renders_dir / f"{s02_stem}.mp4"
        sh = ctx.load_state_hashes()
        _make_dummy_mp4(ffmpeg_bin, mp4_2, duration_s=3.0, color="maroon")
        after_scene_render(
            ctx, ctx.scenes[1], None, s02_stem, mp4_2,
            hashes[s02_stem][0], {"prompt": ctx.scenes[1]["prompt"], "seed": 2002}, 1.0, sh, created_at=1000.0,
        )
        sh.setdefault("tasks", {})[s02_stem] = hashes[s02_stem][0]
        sh.setdefault("task_hashes", {})[s02_stem] = hashes[s02_stem][0]
        ctx.save_state_hashes(sh)

        page = env["page"]
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length === 2", timeout=15000)
        self._click_scene_on_canvas(page, time_s=4.0, expected_id="S02")
        page.wait_for_function("() => (document.getElementById('takesCountBadge')?.textContent || '').includes('take')", timeout=8000)

        # Delete S02 and immediately add a new scene (gets id S02) before saving
        env["dialog_answers"].append(True)
        page.locator("#btnDeleteScene").click()
        page.wait_for_function("() => window.state.projectData.scenes.length === 1", timeout=5000)
        page.locator("#btnAddScene").click()
        page.wait_for_function(
            "() => window.state.projectData.scenes.length === 2 && window.state.projectData.scenes[1].id === 'S02' "
            "&& window.state.projectData.scenes[1]._freshSceneTakes === true",
            timeout=5000,
        )
        page.evaluate("() => window.selectScene(1)")
        page.wait_for_function("() => (document.getElementById('clipStatusBadge')?.textContent || '') === 'Missing'", timeout=5000)
        self.assertFalse(page.locator("#sceneTakesTray").is_visible())

        page.locator("#btnSaveProject").click()
        page.wait_for_function("() => !window.state.isDirty", timeout=8000)
        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        self.assertNotIn("_freshSceneTakes", saved_doc["scenes"][1])
        self.assertFalse(mp4_2.exists())
        takes_resp = page.evaluate(
            "async (b) => (await fetch(b + '/api/takes/e2e_proj/S02')).json()", env["base_url"]
        )
        self.assertEqual(takes_resp.get("takes", []), [])

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.state?.projectData?.scenes?.length === 2", timeout=15000)
        page.evaluate("() => window.selectScene(1)")
        page.wait_for_function("() => (document.getElementById('clipStatusBadge')?.textContent || '') === 'Missing'", timeout=5000)
        self.assertFalse(page.locator("#sceneTakesTray").is_visible())
        self.assertEqual(env["page_errors"], [])

    def test_17_uj17_anchor_gallery_multi_select_batch_delete(self):
        env = self._start_studio_session(slug="anchor_batch_e2e", dev=True)
        page = env["page"]

        page.locator('.tab-btn[data-tab="console"]').click()
        page.locator("#chkDummyRenderMode").wait_for(state="visible", timeout=5000)
        page.locator("#chkDummyRenderMode").check()

        # Generate 3 takes on A01
        page.locator('.tab-btn[data-tab="anchors"]').click()
        a01_card = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"]')
        a01_card.wait_for(state="visible", timeout=5000)
        a01_card.locator("[data-afield='gen_count']").fill("3")
        a01_card.locator("button[data-action='generate-anchor']").click()
        page.wait_for_function(
            "() => !window.state?.isEngineRunning && (window.state?.anchorTakes?.A01?.takes?.length || 0) >= 3",
            timeout=30000,
        )

        # Open Anchor Takes Gallery: A01 starts with 1 baseline + 3 generated = 4 takes; delete button disabled initially
        a01_card.locator("[data-tact='open-gallery']").click()
        gallery = page.locator("#anchorTakesGalleryModal")
        gallery.wait_for(state="visible", timeout=5000)
        initial_cnt = gallery.locator(".anchor-gallery-card").count()
        self.assertGreaterEqual(initial_cnt, 3)
        del_btn = gallery.locator("#btnGalleryDeleteSelected")
        self.assertTrue(del_btn.is_disabled())

        # Select Take #1 and Take #3 -> single Cancel keeps all takes intact
        gallery.locator(".anchor-gallery-card").nth(0).locator(".gallery-take-select-chk").check()
        gallery.locator(".anchor-gallery-card").nth(2).locator(".gallery-take-select-chk").check()
        self.assertEqual(gallery.locator(".anchor-gallery-card.is-selected").count(), 2)
        self.assertIn("(2)", del_btn.inner_text())

        env["seen_dialogs"].clear()
        env["dialog_answers"].append(False)
        del_btn.click()
        self.assertEqual(len(env["seen_dialogs"]), 1)
        self.assertEqual(gallery.locator(".anchor-gallery-card").count(), initial_cnt)

        # Confirm batch delete -> ONLY ONE confirmation dialog deletes both selected takes in one batch
        env["seen_dialogs"].clear()
        env["dialog_answers"].append(True)
        del_btn.click()
        expected_rem = initial_cnt - 2
        page.wait_for_function(
            "exp => document.querySelectorAll('#anchorTakesGalleryModal .anchor-gallery-card').length === exp",
            arg=expected_rem,
            timeout=10000,
        )
        self.assertEqual(len(env["seen_dialogs"]), 1)
        self.assertEqual(gallery.locator(".anchor-gallery-card").count(), expected_rem)
        self.assertTrue(del_btn.is_disabled())

        # Close gallery & reload to verify only expected_rem takes remain on disk
        gallery.locator("#btnCloseAnchorGallery").click()
        gallery.wait_for(state="hidden", timeout=5000)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "exp => (window.state?.anchorTakes?.A01?.takes?.length || 0) === exp",
            arg=expected_rem,
            timeout=15000,
        )
        self.assertEqual(env["page_errors"], [])

    def test_18_uj18_anchor_direct_source_images_without_characters(self):
        env = self._start_studio_session(slug="anchor_src_e2e", dev=True)
        page = env["page"]
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)

        # Strip characters so project has ZERO characters and ZERO references/ files
        cfg_data = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        cfg_data["characters"] = {}
        env["cfg_path"].write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")
        with page.expect_response("**/characters"):
            page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.state?._projectLoadedSeq > 0 && window.state._projectLoadedSeq === window.state._projectLoadSeq "
            "&& window.state?.projectData?.anchors?.length >= 2 && Object.keys(window.state?.projectData?.characters || {}).length === 0",
            timeout=15000,
        )

        page.locator('.tab-btn[data-tab="console"]').click()
        page.locator("#chkDummyRenderMode").wait_for(state="visible", timeout=5000)
        page.locator("#chkDummyRenderMode").check()

        page.locator('.tab-btn[data-tab="anchors"]').click()
        a01_card = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"]')
        a01_card.wait_for(state="visible", timeout=5000)
        src_panel = a01_card.locator(".anchor-source-refs-panel")
        src_panel.wait_for(state="visible", timeout=5000)

        # Upload two standalone source images directly onto A01 (multi-file upload, no character created)
        src1 = env["tmp_root"] / "hero_ref.jpg"
        src2 = env["tmp_root"] / "stage_plate.jpg"
        _make_tiny_jpg(ffmpeg_bin, src1, "teal")
        _make_tiny_jpg(ffmpeg_bin, src2, "purple")
        src_panel.locator(".anchor-src-file-input").set_input_files([str(src1), str(src2)])
        page.wait_for_function(
            "() => (window.state?.projectData?.anchors?.[0]?.image_refs || []).length === 2",
            timeout=10000,
        )
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 2)
        self.assertTrue(len(list((env["proj_dir"] / "work" / "anchor_sources").glob("*.*"))) == 2)

        # Click <image1> tag button to insert tag into prompt
        a01_card.locator(".anchor-src-tag-btn").first.click()
        self.assertIn("<image1>", a01_card.locator("[data-afield='prompt']").input_value())

        # Also pick an existing project anchor (A02) directly as a source reference, then detach it
        pick_sel = src_panel.locator(".anchor-src-pick-select")
        a02_opt = pick_sel.locator("option[value*='A02']").first.get_attribute("value")
        self.assertTrue(a02_opt)
        pick_sel.select_option(a02_opt)
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 3)
        a01_card.locator(".anchor-src-rm-btn").nth(2).click()
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 2)

        # Generate A01 directly -> auto-saves image_refs on disk and generates take cleanly
        a01_card.locator("button[data-action='generate-anchor']").click()
        page.wait_for_function(
            "() => !window.state?.isDirty && !window.state?.isEngineRunning && (window.state?.anchorTakes?.A01?.takes?.length || 0) >= 2",
            timeout=30000,
        )
        saved_doc = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        self.assertEqual(saved_doc.get("characters"), {})
        self.assertEqual(len(saved_doc["anchors"][0].get("image_refs", [])), 2)
        self.assertTrue(all(r.startswith("work/anchor_sources/") for r in saved_doc["anchors"][0]["image_refs"]))

        # Verify generated take recorded image_refs and displays them as source pills in Gallery Mode
        a01_card.locator("[data-tact='open-gallery']").click()
        gallery = page.locator("#anchorTakesGalleryModal")
        gallery.wait_for(state="visible", timeout=5000)
        self.assertEqual(gallery.locator("#gallerySourceRefsBar .anchor-src-chip").count(), 2)
        self.assertGreaterEqual(gallery.locator(".anchor-gallery-card .take-source-pill").count(), 2)
        gallery.locator("#btnCloseAnchorGallery").click()
        gallery.wait_for(state="hidden", timeout=5000)

        # Switching A02 (with 0 refs) from Auto -> Custom stays in Custom mode instead of snapping to Text-Only
        a02_card = page.locator('#anchorCardsContainer .anchor-card[data-aid="A02"]')
        a02_mode = a02_card.locator(".anchor-src-mode-select")
        a02_mode.select_option("custom")
        self.assertEqual(a02_mode.input_value(), "custom")

        # Open Uploaded Library drawer on A01 and delete 1 uploaded source file from disk -> detaches & deletes cleanly
        a01_card.locator(".anchor-src-lib-btn").click()
        a01_card.locator(".anchor-src-library").wait_for(state="visible", timeout=5000)
        env["dialog_answers"].append(True)
        a01_card.locator(".anchor-src-library [data-lact='delete-file']").first.click()
        page.wait_for_function(
            "() => (window.state?.projectData?.anchors?.[0]?.image_refs || []).length === 1",
            timeout=10000,
        )
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 1)
        self.assertEqual(len(list((env["proj_dir"] / "work" / "anchor_sources").glob("*.*"))), 1)
        self.assertEqual(env["page_errors"], [])

    def test_19_uj19_anchor_auto_source_inspection_and_per_image_removal(self):
        env = self._start_studio_session(slug="anchor_auto_src_e2e", dev=True)
        page = env["page"]
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)

        ref1 = env["proj_dir"] / "references" / "char_male" / "male_front.jpg"
        ref2 = env["proj_dir"] / "references" / "char_male" / "male_side.jpg"
        _make_tiny_jpg(ffmpeg_bin, ref1, "navy")
        _make_tiny_jpg(ffmpeg_bin, ref2, "olive")
        cfg_data = json.loads(env["cfg_path"].read_text(encoding="utf-8"))
        cfg_data["characters"]["char_male"]["looks"]["default"]["image_refs"] = [
            "references/char_male/male_front.jpg",
            "references/char_male/male_side.jpg",
        ]
        env["cfg_path"].write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")

        with page.expect_response("**/characters"):
            page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => window.state?._projectLoadedSeq > 0 && window.state._projectLoadedSeq === window.state._projectLoadSeq "
            "&& (window.state?.charactersData?.char_male?.looks?.default?.image_refs || []).length === 2",
            timeout=15000,
        )

        # Select S01 -> Scene Inspector should expose compact source badge for S01's anchor A01
        page.evaluate("() => window.selectScene(0)")
        page.locator("#inspectorAnchorSourcesHost .scene-anchor-sources-mini").wait_for(state="visible", timeout=5000)

        # Open Scene Anchors tab: A01 uses @male -> Auto mode displays both @male source chips with Remove buttons
        page.locator('.tab-btn[data-tab="anchors"]').click()
        a01_card = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"]')
        a01_card.wait_for(state="visible", timeout=5000)
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 2)
        self.assertEqual(a01_card.locator(".anchor-src-chip.is-auto-src").count(), 2)

        # Click × on the first Auto image -> converts remaining 1 image to Custom & shows ↺ Reset Auto
        a01_card.locator(".anchor-src-rm-btn").first.click()
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 1)
        self.assertEqual(a01_card.locator(".anchor-src-mode-select").input_value(), "custom")
        self.assertTrue(a01_card.locator(".anchor-src-clear-btn").is_visible())

        # Click ↺ Reset Auto -> restores both @male Auto source images cleanly
        a01_card.locator(".anchor-src-clear-btn").click()
        self.assertEqual(a01_card.locator(".anchor-src-chip").count(), 2)
        self.assertEqual(a01_card.locator(".anchor-src-mode-select").input_value(), "auto")
        self.assertEqual(env["page_errors"], [])

    def test_20_uj20_ltx25_msr_and_ingredients_ui(self):
        env = self._start_studio_session(slug="ltx25_ui_e2e")
        page = env["page"]
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)

        ref1 = env["proj_dir"].parent / "prop_guitar.jpg"
        ref2 = env["proj_dir"].parent / "prop_hat.jpg"
        _make_tiny_jpg(ffmpeg_bin, ref1, "gold")
        _make_tiny_jpg(ffmpeg_bin, ref2, "purple")

        page.evaluate("() => window.selectScene(0)")
        page.locator("#sceneDirectRefsHost").wait_for(state="attached", timeout=5000)

        # 1. Verify [LTX-MSR] and [Ingredients] badges appear in #sceneVideoRenderer
        opt19_txt = page.locator('#sceneVideoRenderer option[value="19"]').text_content() or ""
        opt20_txt = page.locator('#sceneVideoRenderer option[value="20"]').text_content() or ""
        self.assertIn("[LTX-MSR]", opt19_txt)
        self.assertIn("[Ingredients]", opt20_txt)

        # 2. Switch S01 to #19 LTX-2.5 MSR -> locks Engine to local, shows BG Slot 1 hint & MSR options
        page.locator("#sceneVideoRenderer").select_option("19")
        self.assertEqual(page.locator("#sceneBackend").input_value(), "local")
        self.assertTrue(page.locator("#sceneBackend").is_disabled())
        self.assertIn("BG Slot 1", page.locator("#anchorMsrBgHint").text_content() or "")
        self.assertIn("LTX-2.5 MSR Mode", page.locator("#refTraySingle").text_content() or "")
        self.assertTrue(page.locator("#sceneDirectRefsHost .sc-msr-bg-chk").is_checked())

        # 3. Upload 2 direct scene ref images & toggle MSR background option
        page.locator("#sceneDirectRefsHost .sc-ref-file-inp").set_input_files([str(ref1), str(ref2)])
        page.wait_for_function("() => (window.state?.projectData?.scenes?.[0]?.image_refs || []).length === 2", timeout=10000)
        self.assertEqual(page.locator("#sceneDirectRefsHost .anchor-src-chip").count(), 2)
        first_before = page.evaluate("() => window.state.projectData.scenes[0].image_refs[0]")
        page.locator("#sceneDirectRefsHost .sc-ref-mv-r").first.click()
        first_after = page.evaluate("() => window.state.projectData.scenes[0].image_refs[0]")
        self.assertNotEqual(first_before, first_after)

        page.locator("#sceneDirectRefsHost .sc-msr-bg-chk").uncheck()
        self.assertFalse(page.evaluate("() => window.state.projectData.scenes[0].msr_use_start_as_bg"))
        self.assertIn("Ignored", page.locator("#anchorMsrBgHint").text_content() or "")

        # 4. Combo warnings for MSR Pass 2 & >10s duration
        w_ids = page.evaluate("""() => {
          const sc = window.state.projectData.scenes[0];
          const wLong = window.evaluateComboWarnings(sc, { end: 15.0 }).map(w => w.id);
          const wP2 = window.evaluateComboWarnings(sc, { mode: "duet_method_b", renderer_f: 19 }).map(w => w.id);
          return { wLong, wP2 };
        }""")
        self.assertIn("ltx2_msr_long_scene", w_ids["wLong"])
        self.assertIn("ltx2_msr_pass2", w_ids["wP2"])

        # 5. Switch S01 to #20 Ingredients and verify direct refs stay visible in Modular Components mode
        page.locator("#sceneVideoRenderer").select_option("20")
        self.assertIn("Ingredients Reference Sheet", page.locator("#sceneDirectRefsHost .anchor-src-title").text_content() or "")
        self.assertIn("LTX-2.5 Ingredients Mode", page.locator("#refTraySingle").text_content() or "")
        page.locator("#btnPromptModeComponents").click()
        self.assertTrue(page.locator("#sceneDirectRefsHost .sc-ref-upload-btn").is_visible())

        # 6. Scenes Tab parity: local backend locked & ref picker rendered on card
        page.locator('.tab-btn[data-tab="scenes"]').click()
        card1 = page.locator('#tab-scenes .scene-card[data-scene-id="S01"]')
        card1.wait_for(state="visible", timeout=5000)
        self.assertTrue(card1.locator('select[data-role="scene-backend"]').is_disabled())
        self.assertEqual(card1.locator(".anchor-src-chip").count(), 2)
        self.assertEqual(env["page_errors"], [])

    def test_21_uj21_character_mask_studio_and_msr_autoskip(self):
        env = self._start_studio_session(slug="char_mask_e2e")
        page = env["page"]
        ffmpeg_bin = resolve_ffmpeg_path(WORKSPACE_ROOT, "", None)

        ref_img = env["proj_dir"].parent / "maya_crowd.jpg"
        _make_tiny_jpg(ffmpeg_bin, ref_img, "crimson")

        # 1. Open Characters tab, create character Maya, upload reference image
        page.locator('.tab-btn[data-tab="characters"]').click()
        page.locator("#btnNewCharacter").click()
        page.locator("#charModalInput").fill("Maya")
        page.locator("#btnCharModalConfirm").click()
        page.locator("#characterDetailPane .look-editor-card").wait_for(state="visible", timeout=5000)

        page.locator('#characterDetailPane .file-upload-input[data-entity="maya"][data-look="default"]').set_input_files(str(ref_img))
        page.locator("#characterDetailPane .char-ref-thumb-wrap").wait_for(state="visible", timeout=10000)
        self.assertIn("RAW", page.locator("#characterDetailPane .char-mask-status-pill").first.text_content() or "")

        # 2. Open Mask & Extraction Studio modal, switch views, draw cut stroke, Save Cutout & Apply
        page.locator("#characterDetailPane .btn-open-char-mask").first.click()
        modal = page.locator("#charMaskModalOverlay")
        modal.wait_for(state="visible", timeout=5000)
        page.wait_for_function("() => !document.getElementById('charMaskBusyText')?.textContent", timeout=10000)

        page.locator('.char-mask-view-btn[data-view="white"]').click()
        page.locator('.char-mask-view-btn[data-view="checker"]').click()
        page.locator('.char-mask-view-btn[data-view="split"]').click()
        self.assertTrue(page.locator("#charMaskSplitSlider").is_visible())
        page.locator('.char-mask-view-btn[data-view="tint"]').click()

        page.locator('.char-mask-tool-btn[data-tool="erase"]').click()
        box = page.locator("#charMaskGuideCanvas").bounding_box()
        self.assertIsNotNone(box)
        page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.2)
        page.mouse.up()

        page.locator("#btnCharMaskSaveApply").click()
        modal.wait_for(state="hidden", timeout=15000)

        # 3. Verify Look card badge flips to MASKED + Show Masks overlay checkbox + Toggle Raw/Cut works
        self.assertIn("MASKED", page.locator("#characterDetailPane .char-mask-status-pill").first.text_content() or "")
        page.locator("#characterDetailPane .chk-show-look-masks").check()
        self.assertTrue(page.locator("#characterDetailPane .look-editor-card.show-mask-overlay").is_visible())

        # 4. Select S01 with @maya on #19 MSR -> shows Clean cutouts active auto-skip pill
        page.locator('.tab-btn[data-tab="timeline"]').click()
        page.evaluate("""() => {
          window.selectScene(0);
          const ta = document.getElementById('scenePrompt');
          ta.value = '@maya walks through corridor';
          ta.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        page.locator("#sceneVideoRenderer").select_option("19")
        self.assertIn("Clean cutouts active", page.locator("#sceneDirectRefsHost").text_content() or "")
        self.assertFalse(page.locator("#sceneDirectRefsHost .sc-msr-rmbg-chk").is_checked())

        # 5. Quick toggle Maya ref back to Raw -> S01 MSR re-enables Remove Subject Backgrounds
        page.locator('.tab-btn[data-tab="characters"]').click()
        page.locator("#characterDetailPane .btn-toggle-char-mask").first.click()
        page.wait_for_function(
            "() => (document.querySelector('#characterDetailPane .char-mask-status-pill')?.textContent || '').includes('RAW')",
            timeout=10000,
        )
        page.locator('.tab-btn[data-tab="timeline"]').click()
        page.evaluate("() => window.renderSceneRefPickerUI?.(window.state.projectData.scenes[0])")
        self.assertTrue(page.locator("#sceneDirectRefsHost .sc-msr-rmbg-chk").is_checked())
        self.assertEqual(env["page_errors"], [])

    def test_22_h3_image_anchor_profiles_and_capabilities_ui(self):
        env = self._start_studio_session(slug="h3_image_ui_e2e", dev=True)
        page = env["page"]

        # 1. Settings tab lists #17, #18, #19 anchor renderers and 4MP/8MP/16MP anchor resolutions
        page.locator('.tab-btn[data-tab="settings"]').click()
        anchor_opts = page.evaluate("() => Array.from(document.querySelectorAll('#cfgDefaultAnchorRenderer option')).map((o) => o.value)")
        for rid in ("17", "18", "19"):
            self.assertIn(rid, anchor_opts)
        res_opts = page.evaluate("() => Array.from(document.querySelectorAll('#cfgDefaultAnchorResolution option')).map((o) => o.value)")
        for rv in ("2720x1536", "3872x2176", "5472x3072"):
            self.assertIn(rv, res_opts)

        # 2. Anchors tab exposes >4 ref limit, <Picture N> capability note, and model native resolutions
        page.locator('.tab-btn[data-tab="anchors"]').click()
        a01 = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"]')
        a01.wait_for(state="visible", timeout=5000)
        a01.locator("[data-afield='renderer']").select_option("17")
        caps_17 = a01.locator(".anchor-model-caps").inner_text()
        self.assertIn("Up to 16 refs", caps_17)
        self.assertIn("<Picture N>", caps_17)
        self.assertIn("6 steps", caps_17)
        card_res_vals = page.evaluate("() => Array.from(document.querySelectorAll('#anchorCardsContainer .anchor-card[data-aid=\"A01\"] [data-afield=\"resolution\"] option')).map((o) => o.value)")
        self.assertIn("5472x3072", card_res_vals)
        a01.locator("[data-afield='resolution']").select_option("5472x3072")

        # Attach 6 synthetic refs via state and verify Source Images panel displays (6/16), not capped at 4
        page.evaluate("""() => {
          const a = window.state.projectData.anchors[0];
          a.image_refs = [1,2,3,4,5,6].map((n) => `work/anchor_sources/s${n}.jpg`);
          window.updateAnchorStudioUI?.();
        }""")
        src_title = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"] .anchor-src-title').inner_text()
        self.assertIn("(6/16)", src_title)

        # Switching to #19 P-H3-Image switches capability hint to 8 refs / 8 steps and exposes 3872x2176
        a01_r = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"] [data-afield="renderer"]')
        a01_r.select_option("19")
        caps_19 = page.locator('#anchorCardsContainer .anchor-card[data-aid="A01"] .anchor-model-caps').inner_text()
        self.assertIn("Up to 8 refs", caps_19)
        self.assertIn("8 steps", caps_19)
        card_res_19 = page.evaluate("() => Array.from(document.querySelectorAll('#anchorCardsContainer .anchor-card[data-aid=\"A01\"] [data-afield=\"resolution\"] option')).map((o) => o.value)")
        self.assertIn("3872x2176", card_res_19)

        # 3. Characters Look Gen & Mask De-Occlusion expose H3-Image renderers & native resolutions
        page.locator('.tab-btn[data-tab="characters"]').click()
        char_model = page.locator("#characterDetailPane [data-cfield='gen_renderer']").first
        char_model.wait_for(state="visible", timeout=5000)
        char_model.select_option("17")
        char_caps = page.locator("#characterDetailPane [data-cfield='gen_caps']").first.inner_text()
        self.assertIn("Up to 16 refs", char_caps)
        char_res = page.evaluate("() => Array.from(document.querySelectorAll('#characterDetailPane [data-cfield=\"gen_resolution\"] option')).map((o) => o.value)")
        self.assertIn("5472x3072", char_res)

        ref_img = env["proj_dir"].parent / "h3_char_ref.jpg"
        _make_tiny_jpg(resolve_ffmpeg_path(WORKSPACE_ROOT, "", None), ref_img, "navy")
        page.locator('#characterDetailPane .file-upload-input[data-entity="char_male"][data-look="default"]').set_input_files(str(ref_img))
        page.locator("#characterDetailPane .btn-open-char-mask").first.wait_for(state="visible", timeout=10000)
        page.locator("#characterDetailPane .btn-open-char-mask").first.click()
        page.locator("#charMaskModalOverlay").wait_for(state="visible", timeout=10000)
        iso_mods = page.evaluate("() => Array.from(document.querySelectorAll('#charMaskIsolateRenderer option')).map((o) => o.value)")
        self.assertIn("17", iso_mods)
        self.assertIn("19", iso_mods)
        iso_res = page.evaluate("() => Array.from(document.querySelectorAll('#charMaskIsolateRes option')).map((o) => o.value)")
        self.assertIn("2720x1536", iso_res)
        page.locator("#btnCloseCharMaskModal").click()
        self.assertEqual(env["page_errors"], [])


if __name__ == "__main__":
    unittest.main()
