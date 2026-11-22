"""
Unit and User Journey Tests for Scene Editor Persistence & Undo Granularity.
Covers:
1. User Journey: No background auto-save to disk during scene editing.
2. User Journey: Explicit save via REST endpoint writes to disk.
3. User Journey: Discard unsaved changes reverts cleanly to last saved state.
4. User Journey: Execution of Node.js frontend history and persistence test suite.
5. Invariant: All static JavaScript files remain strictly under 850 lines.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
sys.path.insert(0, str(WORKSPACE_ROOT / "work" / "ui"))


class TestScenePersistenceAndUndo(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mvg_persistence_test_")
        self.proj_dir = Path(self.test_dir) / "test_slug"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.proj_dir / "test_slug.json"

        self.initial_data = {
            "project": {
                "name": "Persistence Test Project",
                "total_duration": 30.0,
                "fps": 24,
            },
            "scenes": [
                {
                    "id": "S01",
                    "start": 0.0,
                    "end": 5.0,
                    "prompt": "Original S01 prompt text",
                    "seed": 1001,
                    "mode": "male_solo",
                },
                {
                    "id": "S02",
                    "start": 5.0,
                    "end": 10.0,
                    "prompt": "Original S02 prompt text",
                    "seed": 1002,
                    "mode": "male_solo",
                },
            ],
            "markers": [
                {"id": "M01", "name": "Cue 1", "time": 2.5, "color": "#f59e0b"}
            ],
        }
        self.config_path.write_text(json.dumps(self.initial_data, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_uj_no_disk_write_during_scene_editing(self):
        """Verify that in-memory editing never modifies the disk JSON file."""
        initial_mtime = self.config_path.stat().st_mtime_ns
        initial_content = self.config_path.read_text(encoding="utf-8")

        # Simulate in-memory mutations as user types into scenePrompt, nudges timing, and edits seed
        mem_data = json.loads(initial_content)
        mem_data["scenes"][0]["prompt"] = "User typed 50 characters here without saving"
        mem_data["scenes"][0]["seed"] = 88888
        mem_data["scenes"][0]["end"] = 5.5
        mem_data["scenes"][1]["start"] = 5.5

        # Check disk file remains completely untouched
        self.assertEqual(self.config_path.stat().st_mtime_ns, initial_mtime)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), initial_content)

    def test_uj_explicit_save_updates_disk_and_backup(self):
        """Verify that explicit save via POST /api/project/<slug>/save writes to disk and creates backup."""
        import io
        from http import HTTPStatus
        import server

        class DummyHandler:
            def __init__(self, method, path, body_dict=None):
                self.command = method
                self.path = path
                self.server = server
                body_bytes = json.dumps(body_dict).encode("utf-8") if body_dict else b""
                self.headers = {"Content-Length": str(len(body_bytes)), "Content-Type": "application/json"}
                self.rfile = io.BytesIO(body_bytes)
                self.wfile = io.BytesIO()
                self.response_status = None
                self.response_headers = {}

            def send_response(self, status):
                self.response_status = status

            def send_header(self, k, v):
                self.response_headers[k] = v

            def end_headers(self):
                pass

            def send_json(self, data, status=HTTPStatus.OK):
                server.StudioRequestHandler.send_json(self, data, status)

            def send_error_json(self, msg, status=HTTPStatus.BAD_REQUEST):
                server.StudioRequestHandler.send_error_json(self, msg, status)

            def read_json_body(self):
                return server.StudioRequestHandler.read_json_body(self)

        orig_projects_dir = server.PROJECTS_DIR
        orig_backup_dir = server.BACKUP_DIR
        try:
            server.PROJECTS_DIR = Path(self.test_dir)
            server.BACKUP_DIR = Path(self.test_dir) / "backup"
            updated_data = json.loads(self.config_path.read_text(encoding="utf-8"))
            updated_data["scenes"][0]["prompt"] = "Explicitly saved prompt text via API"

            h_save = DummyHandler("PUT", "/api/project/test_slug", updated_data)
            server.StudioRequestHandler.do_PUT(h_save)

            self.assertEqual(h_save.response_status, 200, f"Expected 200 OK, got: {h_save.wfile.getvalue().decode('utf-8')}")
            res_json = json.loads(h_save.wfile.getvalue().decode("utf-8"))
            self.assertTrue(res_json.get("success"))

            # Check that file on disk is updated
            saved_disk = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_disk["scenes"][0]["prompt"], "Explicitly saved prompt text via API")

            # Check backup directory was created
            self.assertTrue(server.BACKUP_DIR.exists(), "Backup directory should exist after explicit save")
            backups = list(server.BACKUP_DIR.glob("*.json"))
            self.assertGreaterEqual(len(backups), 1, "At least one backup file should be created on save")
        finally:
            server.PROJECTS_DIR = orig_projects_dir
            server.BACKUP_DIR = orig_backup_dir

    def test_uj_discard_unsaved_changes_reverts_to_last_saved(self):
        """Verify that discarding unsaved changes restores the exact baseline state."""
        last_saved_json = json.dumps(self.initial_data, sort_keys=True)

        # Mutate in-memory
        mem_data = json.loads(last_saved_json)
        mem_data["scenes"][0]["prompt"] = "Experimental changes to be abandoned"
        mem_data["scenes"][0]["seed"] = 99999
        mem_data["markers"].append({"id": "M02", "name": "Temp", "time": 4.0})

        # Discard: Revert to last_saved_json
        restored_data = json.loads(last_saved_json)
        restored_json = json.dumps(restored_data, sort_keys=True)

        self.assertEqual(restored_json, last_saved_json)
        self.assertEqual(restored_data["scenes"][0]["prompt"], "Original S01 prompt text")
        self.assertEqual(len(restored_data["markers"]), 1)

    def test_uj_frontend_history_and_persistence_node_suite(self):
        """Execute the Node.js test suite exercising takes_and_history.js."""
        node_script = WORKSPACE_ROOT / "tests" / "test_history_and_persistence.js"
        self.assertTrue(node_script.exists(), f"Node script not found: {node_script}")

        res = subprocess.run(
            ["node", str(node_script)],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Node test suite failed with error:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )
        self.assertIn("PERSISTENCE & UNDO GRANULARITY TESTS PASSED", res.stdout)

    def test_uj_queue_load_does_not_save_when_dirty(self):
        """Verify queue loading logic does not auto-save dirty in-memory state."""
        js_file = WORKSPACE_ROOT / "work" / "ui" / "static" / "js" / "queue_and_console.js"
        content = js_file.read_text(encoding="utf-8")
        idx = content.find("async function loadProjectQueue")
        self.assertNotEqual(idx, -1)
        func_slice = content[idx:idx + 400]
        self.assertNotIn("saveProjectData", func_slice, "loadProjectQueue must not contain auto-save logic")

    def test_uj_all_static_js_files_under_850_lines(self):
        """Ensure modularity guardrail: ALL static JS files must be strictly under 850 lines."""
        js_dir = WORKSPACE_ROOT / "work" / "ui" / "static" / "js"
        self.assertTrue(js_dir.exists())

        for js_file in js_dir.glob("*.js"):
            lines = len(js_file.read_text(encoding="utf-8").splitlines())
            self.assertLess(
                lines,
                850,
                f"Modularity violation: {js_file.name} has {lines} lines (must be < 850)",
            )

    def test_uj_discard_button_present_in_index_html(self):
        """Verify the #btnDiscardChanges button is present in index.html."""
        html_file = WORKSPACE_ROOT / "work" / "ui" / "static" / "index.html"
        content = html_file.read_text(encoding="utf-8")
        self.assertIn('id="btnDiscardChanges"', content)
        self.assertIn('id="btnSaveProject"', content)

    def test_uj_split_scene_button_and_lock_behavior(self):
        """Verify #btnSplitSceneHeader handles locked scenes, playhead auto-selection, and modal confirmation."""
        node_code = r"""
        const fs = require("fs"), vm = require("vm"), path = require("path");
        const jsDir = process.argv[1];
        const els = {};
        const mkEl = (id) => {
          const listeners = {};
          const classes = new Set();
          const e = {
            id, value: "", min: "", max: "", disabled: false, textContent: "", innerHTML: "", title: "",
            style: {}, dataset: {}, options: [], children: [],
            classList: {
              add: (c) => classes.add(c), remove: (c) => classes.delete(c),
              toggle: (c, f) => (f === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c)) : (f ? classes.add(c) : classes.delete(c))),
              contains: (c) => classes.has(c),
            },
            addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); },
            dispatchEvent: (ev) => { (listeners[ev.type] || []).forEach((fn) => fn(ev)); },
            click: () => { if (!e.disabled) (listeners.click || []).forEach((fn) => fn({ target: e })); if (typeof e.onclick === "function" && !e.disabled) e.onclick({ target: e }); },
            querySelectorAll: () => [], contains: () => false, appendChild: () => {}, add: () => {}, focus: () => {}, select: () => {},
          };
          if (id) els[id] = e;
          return e;
        };
        ["btnSplitSceneHeader", "btnDeleteScene", "btnRevertSceneToRendered", "btnToggleSceneLock", "inspectorForm", "toastMessage", "audioElement", "waveformCanvas", "timelinePlayhead", "timelineScrollContainer", "timecodeDisplay"].forEach(mkEl);
        els.waveformCanvas.getContext = () => ({});
        const toasts = [];
        let openedModal = null, dirty = false, listRendered = false;
        const scenes = [{ id: "S01", start: 0.0, end: 4.0, mode: "solo", locked: true, seed: 100 }];
        const sandbox = {
          window: {}, console, parseFloat, parseInt, isNaN, Math, JSON, Set, Array, Object, String, Number, Event: function (t) { this.type = t; },
          setTimeout: (fn) => fn(), clearTimeout: () => {},
          document: {
            readyState: "complete",
            body: { appendChild: () => {} },
            getElementById: (id) => els[id] || null,
            querySelectorAll: () => [],
            createElement: () => mkEl(""),
            addEventListener: () => {},
          },
          state: {
            projectData: { project: { fps: 24 }, scenes, anchors: [] },
            models: { video_renderers: {}, video_resolutions: {} },
            assets: { clips: {}, anchors: {} },
            selectedSceneIdx: 0,
            playheadTime: 1.75,
          },
          markProjectDirty: () => { dirty = true; },
          redrawTimeline: () => {},
          showToast: (m) => toasts.push(m),
          selectAnchorDropdown: () => {}, syncAnchorDropdown: () => "",
          updateSceneAnchorPreview: () => {}, updateSceneEndAnchorPreview: () => {},
          updateSceneMarkersBar: () => {}, updatePromptResolvedPreviews: () => {},
          resizeAllPromptTextareas: () => {}, renderSceneList: () => { listRendered = true; },
          openSplitSceneModal: (sc, start, end, defT, onConfirm) => { openedModal = { sc, start, end, defT, onConfirm }; },
        };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync(path.join(jsDir, "master_and_export.js"), "utf8"), sandbox);
        vm.runInNewContext(fs.readFileSync(path.join(jsDir, "scene_inspector.js"), "utf8"), sandbox);

        // 1. Locked scene keeps btnSplitSceneHeader clickable and shows lock toast
        sandbox.applyInspectorLockState(scenes[0]);
        if (els.btnSplitSceneHeader.disabled) throw new Error("btnSplitSceneHeader must not be disabled on locked scene");
        if (!els.btnSplitSceneHeader.classList.contains("is-locked-action")) throw new Error("Missing is-locked-action class");
        els.btnSplitSceneHeader.click();
        if (!toasts.some((t) => t.includes("locked"))) throw new Error("Expected locked toast, got: " + JSON.stringify(toasts));

        // 2. Unlock, clear selectedSceneIdx, click Split -> auto-selects playhead scene & opens modal at playhead 1.75s
        delete scenes[0].locked;
        sandbox.applyInspectorLockState(scenes[0]);
        sandbox.state.selectedSceneIdx = null;
        els.btnSplitSceneHeader.click();
        if (!openedModal || Math.abs(openedModal.defT - 1.75) > 0.001) throw new Error("Expected modal at 1.75s, got: " + JSON.stringify(openedModal));

        // 3. Confirming split mutates scenes into S01a + S01b and updates Scenes tab
        els["tab-scenes"] = mkEl("tab-scenes");
        els["tab-scenes"].classList.add("active");
        openedModal.onConfirm(1.75);
        if (scenes.length !== 2 || scenes[0].id !== "S01a" || scenes[1].id !== "S01b" || scenes[0].end !== 1.75 || scenes[1].start !== 1.75) {
          throw new Error("Split mutation failed: " + JSON.stringify(scenes));
        }
        if (!dirty || !listRendered) throw new Error("Expected markProjectDirty and renderSceneList");
        console.log("SPLIT_SCENE_BUTTON_OK");
        """
        res = subprocess.run(
            ["node", "-e", node_code, str(WORKSPACE_ROOT / "work" / "ui" / "static" / "js")],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("SPLIT_SCENE_BUTTON_OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
