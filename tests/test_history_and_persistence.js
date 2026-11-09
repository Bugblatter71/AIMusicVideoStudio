/**
 * Verification test suite for Scene Editor Persistence & Undo Granularity Engine.
 * Tests session coalescing, in-focus undo, timeline boundary dragging,
 * dirty state tracking, and discard behavior.
 */

const assert = require("assert");
const fs = require("fs");
const path = require("path");

// Set up mock browser environment
const domListeners = {};
const elements = {};

function createElementMock(id) {
  const el = {
    id,
    tagName: id.includes("Prompt") ? "TEXTAREA" : "INPUT",
    value: "",
    disabled: false,
    title: "",
    style: {},
    classList: {
      add: () => {},
      remove: () => {},
      toggle: () => {},
    },
    addEventListener: (evt, fn) => {
      domListeners[`${id}:${evt}`] = domListeners[`${id}:${evt}`] || [];
      domListeners[`${id}:${evt}`].push(fn);
    },
    dispatchEvent: (evt) => {
      const type = typeof evt === "string" ? evt : evt.type;
      (domListeners[`${id}:${type}`] || []).forEach((fn) => fn(evt));
    },
    focus: function () {
      global.document.activeElement = el;
      (domListeners[`${id}:focus`] || []).forEach((fn) => fn({ type: "focus" }));
    },
    blur: function () {
      if (global.document.activeElement === el) {
        global.document.activeElement = null;
      }
      (domListeners[`${id}:blur`] || []).forEach((fn) => fn({ type: "blur" }));
    },
  };
  return el;
}

global.window = {
  addEventListener: (evt, fn) => {
    domListeners[`window:${evt}`] = domListeners[`window:${evt}`] || [];
    domListeners[`window:${evt}`].push(fn);
  },
  state: {
    isDirty: false,
    projectData: null,
    selectedSceneIdx: 0,
    currentSlug: "test_project",
  },
  showToast: () => {},
  markProjectDirty: (dirty) => {
    window.state.isDirty = dirty !== undefined ? Boolean(dirty) : true;
    if (window.syncDirtyWithSavedState && dirty === undefined) {
      window.syncDirtyWithSavedState();
    }
  },
  redrawTimeline: () => {},
  renderSceneList: () => {},
  updateProjectSettingsUI: () => {},
  selectScene: (idx) => {
    window.state.selectedSceneIdx = idx;
  },
};

global.document = {
  activeElement: null,
  getElementById: (id) => {
    if (!elements[id]) {
      elements[id] = createElementMock(id);
    }
    return elements[id];
  },
  querySelectorAll: () => [],
};

// Load takes_and_history.js
const takesJsCode = fs.readFileSync(
  path.join(__dirname, "../work/ui/static/js/takes_and_history.js"),
  "utf8"
);
eval(takesJsCode);

console.log("takes_and_history.js loaded successfully.");

// ============================================================================
// Test 1: Baseline Initialization
// ============================================================================
function testBaselineInitialization() {
  window.state.projectData = {
    project: { name: "Test Project" },
    scenes: [
      { id: "S01", start: 0.0, end: 5.0, prompt: "Scene 1 baseline prompt" },
      { id: "S02", start: 5.0, end: 10.0, prompt: "Scene 2 baseline prompt" },
    ],
  };

  window.initProjectHistory();

  assert.strictEqual(window.state.isDirty, false, "State should be clean on init");
  assert.strictEqual(window._historyDebug.getUndoStack().length, 0, "Undo stack should be empty on init");
  assert.strictEqual(window._historyDebug.getRedoStack().length, 0, "Redo stack should be empty on init");
  assert.strictEqual(
    window._historyDebug.getLastSavedJson(),
    JSON.stringify(window.state.projectData),
    "lastSavedJson should match initial project data"
  );
  assert.strictEqual(
    window._historyDebug.getCommittedJson(),
    JSON.stringify(window.state.projectData),
    "committedJson should match initial project data"
  );
  console.log("✓ Test 1: Baseline Initialization passed.");
}

// ============================================================================
// Test 2: Typing Session Coalescing (Zero snapshots while typing, 1 on blur)
// ============================================================================
function testTypingSessionCoalescing() {
  const originalPrompt = window.state.projectData.scenes[0].prompt;

  // 1. Focus into prompt field
  window.beginEditSession("inspector:scenePrompt", "Edit Scene S01 Prompt");
  assert.ok(window._historyDebug.getActiveSession(), "Active session should be set on focus");
  assert.strictEqual(window._historyDebug.getUndoStack().length, 0, "Undo stack must be empty during focus");

  // 2. Simulate 50 keystrokes
  let currentText = originalPrompt;
  for (let i = 0; i < 50; i++) {
    currentText += String.fromCharCode(65 + (i % 26));
    window.state.projectData.scenes[0].prompt = currentText;
    window.markProjectDirty();
    assert.strictEqual(
      window._historyDebug.getUndoStack().length,
      0,
      `Undo stack must stay 0 at keystroke ${i + 1}`
    );
  }

  assert.strictEqual(window.state.isDirty, true, "Dirty flag should be true while editing");

  // 3. User blurs field
  window.commitEditSession();
  assert.strictEqual(window._historyDebug.getActiveSession(), null, "Active session should clear on blur");
  assert.strictEqual(window._historyDebug.getUndoStack().length, 1, "Exactly 1 snapshot must be pushed on blur");

  const snapshot = window._historyDebug.getUndoStack()[0];
  assert.strictEqual(
    snapshot.data.scenes[0].prompt,
    originalPrompt,
    "Snapshot must store the pre-mutation baseline prompt"
  );

  // 4. Perform Undo
  window.performUndo();
  assert.strictEqual(
    window.state.projectData.scenes[0].prompt,
    originalPrompt,
    "Single undo must restore the entire original prompt"
  );
  assert.strictEqual(window.state.isDirty, false, "Undoing back to saved state must clear isDirty");
  assert.strictEqual(window._historyDebug.getRedoStack().length, 1, "Redo stack must have 1 entry");

  // 5. Perform Redo
  window.performRedo();
  assert.strictEqual(
    window.state.projectData.scenes[0].prompt,
    currentText,
    "Single redo must restore the full 50-character typed prompt"
  );
  assert.strictEqual(window.state.isDirty, true, "Redoing unsaved text must set isDirty to true");

  console.log("✓ Test 2: Typing Session Coalescing passed.");
}

// ============================================================================
// Test 3: In-Focus Undo & Subsequent Typing Session
// ============================================================================
function testInFocusUndo() {
  // Reset baseline
  window.initProjectHistory();
  const baselinePrompt = window.state.projectData.scenes[0].prompt;

  // Focus and type without blurring
  const promptEl = document.getElementById("scenePrompt");
  promptEl.focus();
  window.beginEditSession("inspector:scenePrompt", "Edit Scene S01 Prompt");
  window.state.projectData.scenes[0].prompt = "Completely new prompt without blur";
  window.markProjectDirty();

  // User hits Ctrl+Z (performUndo) directly while still focused
  window.performUndo();

  assert.strictEqual(
    window.state.projectData.scenes[0].prompt,
    baselinePrompt,
    "In-focus Undo must immediately commit session and revert back to baseline prompt"
  );
  assert.strictEqual(window.state.isDirty, false, "isDirty should be false after in-focus undo to baseline");

  // User resumes typing in the same focused box without losing focus
  window.beginEditSession("inspector:scenePrompt", "Edit Scene S01 Prompt");
  window.state.projectData.scenes[0].prompt = "Second attempt typing";
  window.markProjectDirty();
  window.commitEditSession(); // Now user blurs

  assert.strictEqual(window._historyDebug.getUndoStack().length, 1, "Must have 1 snapshot for second typing session");
  window.performUndo();
  assert.strictEqual(
    window.state.projectData.scenes[0].prompt,
    baselinePrompt,
    "Second typing session must be undone back to baseline"
  );

  promptEl.blur();
  console.log("✓ Test 3: In-Focus Undo & Subsequent Typing passed.");
}

// ============================================================================
// Test 4: Timeline Boundary Dragging Coalescing
// ============================================================================
function testTimelineBoundaryDragging() {
  window.initProjectHistory();
  const originalEnd = window.state.projectData.scenes[0].end; // 5.0

  // mousedown on boundary
  window.beginEditSession("boundary:0", "Drag Scene Boundary S01");

  // Simulate 20 mousemove events (changing sceneEnd continuously)
  for (let step = 1; step <= 20; step++) {
    window.state.projectData.scenes[0].end = originalEnd + step * 0.1;
    window.state.projectData.scenes[1].start = originalEnd + step * 0.1;
    window.markProjectDirty();
    assert.strictEqual(window._historyDebug.getUndoStack().length, 0, "No snapshots during mouse drag");
  }

  assert.strictEqual(window.state.isDirty, true, "isDirty must be true during drag");

  // mouseup
  window.commitEditSession();
  assert.strictEqual(window._historyDebug.getUndoStack().length, 1, "Exactly 1 snapshot on mouseup");

  // Undo
  window.performUndo();
  assert.strictEqual(
    window.state.projectData.scenes[0].end,
    originalEnd,
    "Undo must restore scene 0 end to original in 1 step"
  );
  assert.strictEqual(window.state.isDirty, false, "isDirty should be false after undoing drag");

  console.log("✓ Test 4: Timeline Boundary Dragging passed.");
}

// ============================================================================
// Test 5: Discard Unsaved Changes
// ============================================================================
function testDiscardUnsavedChanges() {
  window.initProjectHistory();
  const savedState = JSON.stringify(window.state.projectData);

  // Make multiple modifications
  window.beginEditSession("inspector:scenePrompt", "Edit Prompt");
  window.state.projectData.scenes[0].prompt = "Modified prompt";
  window.commitEditSession();

  window.recordHistorySnapshot("Randomize S01 Seed");
  window.state.projectData.scenes[0].seed = 99999;
  window.recordHistorySnapshot("Randomize S01 Seed");

  window.state.projectData.scenes[0].end = 7.5;
  window.markProjectDirty();

  assert.strictEqual(window.state.isDirty, true, "State should be dirty before discard");

  // Discard
  window.discardUnsavedChanges();

  assert.strictEqual(
    JSON.stringify(window.state.projectData),
    savedState,
    "Discard must restore projectData to match lastSavedJson 100%"
  );
  assert.strictEqual(window.state.isDirty, false, "State should be clean after discard");

  console.log("✓ Test 5: Discard Unsaved Changes passed.");
}

// ============================================================================
// Test 6: Discrete Actions (Nudge, Seed, Snap, Markers)
// ============================================================================
function testDiscreteActions() {
  window.initProjectHistory();
  const baseSeed = 1234;
  window.state.projectData.scenes[0].seed = baseSeed;
  window.markHistorySaved();

  // 1. Random seed
  window.state.projectData.scenes[0].seed = 5678;
  window.recordHistorySnapshot("Randomize S01 Seed");

  assert.strictEqual(window._historyDebug.getUndoStack().length, 1);
  assert.strictEqual(window.state.isDirty, true);

  // 2. Undo
  window.performUndo();
  assert.strictEqual(window.state.projectData.scenes[0].seed, baseSeed);
  assert.strictEqual(window.state.isDirty, false);

  console.log("✓ Test 6: Discrete Actions passed.");
}

// ============================================================================
// Test 7: Settings Form In-Memory Mutation & Undo
// ============================================================================
function testSettingsFormUndo() {
  window.initProjectHistory();
  const baseName = window.state.projectData.project.name;

  // Edit settings project name
  window.beginEditSession("settings:cfgProjectName", "Edit Project Name");
  window.state.projectData.project.name = "Brand New Project Name";
  window.markProjectDirty();

  assert.strictEqual(window.state.isDirty, true, "isDirty should be true after editing project name");

  window.commitEditSession();
  assert.strictEqual(window._historyDebug.getUndoStack().length, 1, "Must have 1 snapshot after blurring settings input");

  window.performUndo();
  assert.strictEqual(
    window.state.projectData.project.name,
    baseName,
    "Undo must restore project name to original baseline"
  );
  assert.strictEqual(window.state.isDirty, false, "isDirty should be false after undoing settings edit");

  console.log("✓ Test 7: Settings Form In-Memory Mutation & Undo passed.");
}

// ============================================================================
// Run all tests
// ============================================================================
testBaselineInitialization();
testTypingSessionCoalescing();
testInFocusUndo();
testTimelineBoundaryDragging();
testDiscardUnsavedChanges();
testDiscreteActions();
testSettingsFormUndo();

console.log("\n=======================================================");
console.log("ALL 7 PERSISTENCE & UNDO GRANULARITY TESTS PASSED 100%!");
console.log("=======================================================");
