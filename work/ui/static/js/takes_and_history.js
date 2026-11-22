/**
 * Music Video Generation Studio - Non-Destructive Rollback & Render Vault System
 * 
 * Implements:
 * Tier 1: In-Memory Undo/Redo stack (Ctrl+Z, Ctrl+Y, transport bar buttons)
 * Tier 2: Scene-level Takes Tray in Scene Inspector (historical take preview, diff summary, activation)
 * Tier 3: Project-level Checkpoints Modal (pre-render auto snapshots, manual checkpoints, zero-GPU rollback)
 */

(function () {
  "use strict";

  // =========================================================================
  // Tier 1: In-Memory Undo / Redo Manager (Baseline-First Session Engine)
  // =========================================================================
  const MAX_HISTORY = 50;
  const undoStack = [];
  const redoStack = [];
  let isApplyingHistory = false;
  let lastSavedJson = null;
  let committedJson = null;
  let activeSession = null;

  function updateHistoryButtons() {
    const btnUndo = document.getElementById("btnUndo") || document.getElementById("btnTimelineUndo");
    const btnRedo = document.getElementById("btnRedo") || document.getElementById("btnTimelineRedo");
    if (btnUndo) {
      btnUndo.disabled = undoStack.length === 0 && (!activeSession || activeSession.baselineJson === JSON.stringify(window.state?.projectData));
      const lastUndo = undoStack[undoStack.length - 1];
      const desc = activeSession ? activeSession.desc : (lastUndo ? lastUndo.desc : null);
      btnUndo.title = desc ? `Undo: ${desc} (Ctrl+Z)` : "Undo (Ctrl+Z)";
    }
    if (btnRedo) {
      btnRedo.disabled = redoStack.length === 0;
      const lastRedo = redoStack[redoStack.length - 1];
      btnRedo.title = lastRedo ? `Redo: ${lastRedo.desc} (Ctrl+Y)` : "Redo (Ctrl+Y)";
    }
  }

  function initProjectHistory() {
    if (!window.state || !window.state.projectData) return;
    const snap = JSON.stringify(window.state.projectData);
    lastSavedJson = snap;
    committedJson = snap;
    activeSession = null;
    undoStack.length = 0;
    redoStack.length = 0;
    updateHistoryButtons();
    if (typeof window.markProjectDirty === "function") {
      window.markProjectDirty(false);
    }
  }

  function markHistorySaved() {
    if (!window.state || !window.state.projectData) return;
    commitEditSession();
    lastSavedJson = JSON.stringify(window.state.projectData);
    committedJson = lastSavedJson;
    if (typeof window.markProjectDirty === "function") {
      window.markProjectDirty(false);
    }
  }

  function syncDirtyWithSavedState() {
    if (!window.state || !window.state.projectData) return;
    const currentJson = JSON.stringify(window.state.projectData);
    const isDirty = lastSavedJson !== null ? currentJson !== lastSavedJson : true;
    if (typeof window.markProjectDirty === "function") {
      window.markProjectDirty(isDirty);
    }
  }

  function beginEditSession(sessionKey, desc = "Edit") {
    if (isApplyingHistory || !window.state || !window.state.projectData) return;
    if (activeSession && activeSession.key === sessionKey) {
      if (desc) activeSession.desc = desc;
      return;
    }
    commitEditSession();
    if (committedJson === null) {
      committedJson = JSON.stringify(window.state.projectData);
    }
    activeSession = {
      key: sessionKey,
      desc: desc,
      baselineJson: committedJson,
    };
    updateHistoryButtons();
  }

  function commitEditSession() {
    if (!activeSession || isApplyingHistory || !window.state || !window.state.projectData) {
      activeSession = null;
      return;
    }
    const { desc, baselineJson } = activeSession;
    activeSession = null;

    const currentJson = JSON.stringify(window.state.projectData);
    if (currentJson === baselineJson) {
      updateHistoryButtons();
      return;
    }

    if (undoStack.length >= MAX_HISTORY) {
      undoStack.shift();
    }
    undoStack.push({
      desc: desc || "Edit",
      time: Date.now(),
      data: JSON.parse(baselineJson),
    });
    committedJson = currentJson;
    redoStack.length = 0;
    updateHistoryButtons();
    syncDirtyWithSavedState();
  }

  function recordHistorySnapshot(desc = "Edit") {
    if (isApplyingHistory || !window.state || !window.state.projectData) return;
    if (activeSession) {
      commitEditSession();
    }
    try {
      const currentJson = JSON.stringify(window.state.projectData);
      if (committedJson === null) {
        committedJson = currentJson;
        return;
      }
      if (currentJson === committedJson) {
        syncDirtyWithSavedState();
        return;
      }

      if (undoStack.length >= MAX_HISTORY) {
        undoStack.shift();
      }
      undoStack.push({
        desc: desc,
        time: Date.now(),
        data: JSON.parse(committedJson),
      });

      committedJson = currentJson;
      redoStack.length = 0;
      updateHistoryButtons();
      syncDirtyWithSavedState();
    } catch (err) {
      console.warn("[History] Failed to record snapshot:", err);
    }
  }

  async function performUndo() {
    if (isApplyingHistory || !window.state || !window.state.projectData) return;

    if (activeSession) {
      commitEditSession();
    }

    if (undoStack.length === 0) return;

    try {
      isApplyingHistory = true;
      const currentJson = JSON.stringify(window.state.projectData);
      const snapshot = undoStack.pop();

      redoStack.push({
        desc: snapshot.desc,
        time: Date.now(),
        data: JSON.parse(currentJson),
      });

      window.state.projectData = JSON.parse(JSON.stringify(snapshot.data));
      if (window.state.projectData.characters) {
        window.state.charactersData = window.state.projectData.characters;
      }
      committedJson = JSON.stringify(window.state.projectData);

      // Re-render UI
      if (typeof window.redrawTimeline === "function") window.redrawTimeline();
      if (typeof window.renderSceneList === "function") window.renderSceneList();
      if (typeof window.updateProjectSettingsUI === "function") window.updateProjectSettingsUI();
      if (typeof window.renderCharactersTab === "function" && document.getElementById("tab-characters")?.classList.contains("active")) {
        window.renderCharactersTab();
      }
      if (window.state.selectedSceneIdx !== null && typeof window.selectScene === "function") {
        window.selectScene(window.state.selectedSceneIdx);
      }

      syncDirtyWithSavedState();

      if (typeof window.showToast === "function") {
        window.showToast(`↶ Undone: ${snapshot.desc}`);
      }
    } catch (err) {
      console.error("[History] Undo error:", err);
    } finally {
      isApplyingHistory = false;
      if (typeof document !== "undefined" && document.activeElement) {
        const activeEl = document.activeElement;
        if (activeEl.tagName === "INPUT" || activeEl.tagName === "TEXTAREA") {
          const activeId = activeEl.id || "";
          const sc = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
          beginEditSession(activeId ? (activeId.startsWith("cfg") ? `settings:${activeId}` : `inspector:${activeId}`) : "field", `Edit ${sc ? sc.id : "Field"}`);
        }
      }
      updateHistoryButtons();
    }
  }

  async function performRedo() {
    if (isApplyingHistory || !window.state || !window.state.projectData) return;

    if (activeSession) {
      commitEditSession();
    }

    if (redoStack.length === 0) return;

    try {
      isApplyingHistory = true;
      const currentJson = JSON.stringify(window.state.projectData);
      const snapshot = redoStack.pop();

      undoStack.push({
        desc: snapshot.desc,
        time: Date.now(),
        data: JSON.parse(currentJson),
      });

      window.state.projectData = JSON.parse(JSON.stringify(snapshot.data));
      if (window.state.projectData.characters) {
        window.state.charactersData = window.state.projectData.characters;
      }
      committedJson = JSON.stringify(window.state.projectData);

      // Re-render UI
      if (typeof window.redrawTimeline === "function") window.redrawTimeline();
      if (typeof window.renderSceneList === "function") window.renderSceneList();
      if (typeof window.updateProjectSettingsUI === "function") window.updateProjectSettingsUI();
      if (typeof window.renderCharactersTab === "function" && document.getElementById("tab-characters")?.classList.contains("active")) {
        window.renderCharactersTab();
      }
      if (window.state.selectedSceneIdx !== null && typeof window.selectScene === "function") {
        window.selectScene(window.state.selectedSceneIdx);
      }

      syncDirtyWithSavedState();

      if (typeof window.showToast === "function") {
        window.showToast(`↷ Redone: ${snapshot.desc}`);
      }
    } catch (err) {
      console.error("[History] Redo error:", err);
    } finally {
      isApplyingHistory = false;
      if (typeof document !== "undefined" && document.activeElement) {
        const activeEl = document.activeElement;
        if (activeEl.tagName === "INPUT" || activeEl.tagName === "TEXTAREA") {
          const activeId = activeEl.id || "";
          const sc = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
          beginEditSession(activeId ? (activeId.startsWith("cfg") ? `settings:${activeId}` : `inspector:${activeId}`) : "field", `Edit ${sc ? sc.id : "Field"}`);
        }
      }
      updateHistoryButtons();
    }
  }

  function discardUnsavedChanges() {
    if (!lastSavedJson || isApplyingHistory || !window.state || !window.state.projectData) return;
    if (activeSession) {
      commitEditSession();
    }
    const currentJson = JSON.stringify(window.state.projectData);
    if (currentJson === lastSavedJson) return;

    try {
      isApplyingHistory = true;
      if (undoStack.length >= MAX_HISTORY) undoStack.shift();
      undoStack.push({
        desc: "Discard Unsaved Changes",
        time: Date.now(),
        data: JSON.parse(currentJson),
      });

      window.state.projectData = JSON.parse(lastSavedJson);
      if (window.state.projectData.characters) {
        window.state.charactersData = window.state.projectData.characters;
      }
      committedJson = lastSavedJson;
      redoStack.length = 0;

      if (typeof window.redrawTimeline === "function") window.redrawTimeline();
      if (typeof window.renderSceneList === "function") window.renderSceneList();
      if (typeof window.updateProjectSettingsUI === "function") window.updateProjectSettingsUI();
      if (typeof window.renderCharactersTab === "function" && document.getElementById("tab-characters")?.classList.contains("active")) {
        window.renderCharactersTab();
      }
      if (window.state.selectedSceneIdx !== null && typeof window.selectScene === "function") {
        window.selectScene(window.state.selectedSceneIdx);
      }

      syncDirtyWithSavedState();

      if (typeof window.showToast === "function") {
        window.showToast("✕ Discarded unsaved changes (reverted to last save)");
      }
    } catch (err) {
      console.error("[History] Discard error:", err);
    } finally {
      isApplyingHistory = false;
      updateHistoryButtons();
    }
  }

  // Global Hotkeys
  window.addEventListener("keydown", (e) => {
    const isCtrlOrMeta = e.ctrlKey || e.metaKey;
    if (!isCtrlOrMeta) return;

    const key = e.key.toLowerCase();
    if (key === "z") {
      e.preventDefault();
      if (e.shiftKey) {
        performRedo();
      } else {
        performUndo();
      }
    } else if (key === "y") {
      e.preventDefault();
      performRedo();
    } else if (key === "s") {
      e.preventDefault();
      if (typeof window.saveProjectData === "function") {
        window.saveProjectData(true);
      }
    }
  });

  // Expose global methods
  window.initProjectHistory = initProjectHistory;
  window.markHistorySaved = markHistorySaved;
  window.syncDirtyWithSavedState = syncDirtyWithSavedState;
  window.beginEditSession = beginEditSession;
  window.commitEditSession = commitEditSession;
  window.recordHistorySnapshot = recordHistorySnapshot;
  window.performUndo = performUndo;
  window.performRedo = performRedo;
  window.discardUnsavedChanges = discardUnsavedChanges;
  window._historyDebug = {
    getUndoStack: () => undoStack,
    getRedoStack: () => redoStack,
    getActiveSession: () => activeSession,
    getLastSavedJson: () => lastSavedJson,
    getCommittedJson: () => committedJson,
  };

  // =========================================================================
  // Tier 2: Scene-Level Takes Tray in Scene Inspector
  // =========================================================================
  let currentTakesData = null;
  let activeTakeObject = null;
  let currentTakesSceneId = null;

  function formatTakeDate(take) {
    const iso = take?.created_iso || (typeof take?.created_at === "string" ? take.created_at : "");
    if (iso) return iso.slice(0, 16).replace("T", " ");
    if (typeof take?.created_at === "number") return new Date(take.created_at * 1000).toISOString().slice(0, 16).replace("T", " ");
    return "";
  }

  function canonicalFieldVal(v) {
    if (v === undefined || v === null || v === "") return "";
    if (Array.isArray(v)) return JSON.stringify(v);
    if (typeof v === "object") {
      const out = {};
      Object.keys(v).sort().forEach((k) => {
        if (v[k] !== undefined && v[k] !== null && v[k] !== "" && canonicalFieldVal(v[k]) !== "") out[k] = v[k];
      });
      return Object.keys(out).length ? JSON.stringify(out) : "";
    }
    return String(v).trim();
  }

  function computeSceneTakeDiffs(scene, take) {
    if (!scene || !take) return { diffs: [], hasTimingDiff: false };
    const snap = take.scene_snapshot;
    const diffs = [];
    let hasTimingDiff = false;
    if (snap && typeof snap === "object") {
      const groups = [
        ["Prompt", ["prompt", "prompt_m", "prompt_f", "prompt_mode", "prompt_components"]],
        ["Neg Prompt", ["negative_prompt", "negative_mode", "negative_prompt_m", "negative_prompt_f"]],
        ["Mode", ["mode"]],
        ["Anchor", ["anchor", "anchor_end"]],
        ["Seed", ["seed", "seed_m", "seed_f"]],
        ["Stem/Split", ["stem", "stem_m", "stem_f", "split_x"]],
        ["Model", ["backend", "backend_m", "backend_f", "pass2_roi_mode", "pass2_roi_side", "pass2_roi_box", "video_renderer", "renderer", "renderer_m", "renderer_f"]],
        ["Upscale", ["spatial_upsampling"]],
        ["Filters", ["filters"]],
        ["Pad", ["render_pad_start", "render_pad_end"]],
      ];
      groups.forEach(([label, keys]) => {
        if (keys.some((k) => canonicalFieldVal(scene[k]) !== canonicalFieldVal(snap[k]))) diffs.push(label);
      });
      const pd = window.state?.projectData || {};
      const rm = pd.models || {};
      const rMap = { "1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088" };
      Object.entries(rm.video_resolutions || {}).forEach(([k, v]) => { rMap[String(k)] = String(v?.resolution || v || ""); });
      const resOf = (scObj) => {
        const raw = canonicalFieldVal(scObj?.resolution);
        const effRaw = (raw && !["default", "inherit", "none", "null"].includes(raw.toLowerCase())) ? raw : canonicalFieldVal(rm.video_renderers?.[String(scObj?.renderer || scObj?.video_renderer || "")]?.resolution || rm.default_video_resolution || rm.resolution || rm.video_shared_settings?.resolution || pd.master_resolution || 4);
        return rMap[effRaw] || effRaw;
      };
      const curEffRes = resOf(scene);
      const takeRes = canonicalFieldVal(take.resolution) || (String(take.out_name || "").match(/_(\d+x\d+)$/)?.[1] || "");
      const snapEffRes = canonicalFieldVal(snap.resolution) ? resOf(snap) : (takeRes || resOf(snap));
      if (curEffRes && snapEffRes ? (curEffRes !== snapEffRes) : (canonicalFieldVal(scene.resolution) !== canonicalFieldVal(snap.resolution))) diffs.push("Resolution");
      if (snap.start !== undefined && snap.end !== undefined) {
        if (Math.abs(Number(scene.start) - Number(snap.start)) > 0.02 || Math.abs(Number(scene.end) - Number(snap.end)) > 0.02) {
          hasTimingDiff = true;
          diffs.push(`Timing (${snap.start}s–${snap.end}s vs ${scene.start}s–${scene.end}s)`);
        }
      }
    } else {
      if ((scene.prompt || scene.prompt_m || "").trim() !== (take.prompt || "").trim()) diffs.push("Prompt");
      if (take.seed !== undefined && take.seed !== scene.seed) diffs.push("Seed");
      if (take.render_start !== undefined && (Math.abs(Number(scene.start) - Number(take.render_start)) > 0.02 || Math.abs(Number(scene.end) - Number(take.render_end)) > 0.02)) {
        hasTimingDiff = true;
        diffs.push("Timing");
      }
    }
    return { diffs, hasTimingDiff };
  }

  function _patchSceneJson(jsonStr, sceneId, updater) {
    if (!jsonStr) return jsonStr;
    try {
      const obj = JSON.parse(jsonStr);
      const idx = (obj.scenes || []).findIndex((s) => s.id === sceneId);
      if (idx >= 0) updater(obj.scenes, idx);
      return JSON.stringify(obj);
    } catch (e) { return jsonStr; }
  }

  function patchSavedSceneBaseline(restoredScene) {
    if (!window.state?.projectData) return;
    lastSavedJson = _patchSceneJson(lastSavedJson, restoredScene.id, (arr, idx) => { arr[idx] = JSON.parse(JSON.stringify(restoredScene)); });
    committedJson = JSON.stringify(window.state.projectData);
    syncDirtyWithSavedState(); updateHistoryButtons();
  }

  function patchSavedSceneLockBaseline(sceneId, isLocked) {
    if (!window.state?.projectData || !sceneId) return;
    const setLock = (arr, idx) => { if (isLocked) arr[idx].locked = true; else delete arr[idx].locked; };
    lastSavedJson = _patchSceneJson(lastSavedJson, sceneId, setLock);
    committedJson = _patchSceneJson(committedJson, sceneId, setLock);
    if (activeSession?.baselineJson) activeSession.baselineJson = _patchSceneJson(activeSession.baselineJson, sceneId, setLock);
    [undoStack, redoStack].forEach((stk) => stk.forEach((snap) => {
      if (snap?.data?.scenes) { const i = snap.data.scenes.findIndex((s) => s.id === sceneId); if (i >= 0) setLock(snap.data.scenes, i); }
    }));
    syncDirtyWithSavedState(); updateHistoryButtons();
  }
  window.patchSavedSceneLockBaseline = patchSavedSceneLockBaseline;

  function updateRevertHeaderBtn(scene) {
    const btn = document.getElementById("btnRevertSceneToRendered");
    if (!btn) return;
    if (!scene || !activeTakeObject || currentTakesSceneId !== scene.id) {
      btn.style.display = "none";
      return;
    }
    const { diffs } = computeSceneTakeDiffs(scene, activeTakeObject);
    if (diffs.length > 0) {
      btn.style.display = "inline-flex";
      btn.title = `Revert ${scene.id} to rendered Take ${activeTakeObject.take_number} (changes: ${diffs.join(", ")})`;
      btn.onclick = () => restoreSceneToTake(scene, activeTakeObject, btn);
    } else {
      btn.style.display = "none";
    }
  }

  async function restoreSceneToTake(scene, chosenTake, btnEl) {
    if (!scene || !chosenTake || !window.state?.currentSlug) return;
    if (scene.locked) { window.showToast?.("🔒 Scene is locked — unlock it first"); return; }
    const { hasTimingDiff } = computeSceneTakeDiffs(scene, chosenTake);
    let restoreTiming = false;
    if (hasTimingDiff && chosenTake.scene_snapshot?.start !== undefined) {
      const s0 = Number(chosenTake.scene_snapshot.start), e0 = Number(chosenTake.scene_snapshot.end);
      const overlap = (window.state.projectData?.scenes || []).find((s) => s.id !== scene.id && Number(s.start) < e0 - 0.02 && Number(s.end) > s0 + 0.02);
      restoreTiming = overlap ? confirm(`Restoring rendered timing (${s0}s–${e0}s) overlaps ${overlap.id}.\n\nRevert Start/End timing too?`) : true;
    }
    const origText = btnEl ? btnEl.textContent : "";
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = "Reverting..."; }
    try {
      const resp = await fetch("/api/takes/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: window.state.currentSlug, scene_id: scene.id, take_id: chosenTake.take_id, restore_timing: restoreTiming }),
      });
      const actData = await resp.json();
      if (!actData.success) { alert("Failed to revert scene: " + (actData.error || "Unknown error")); return; }
      const restored = actData.restored_params || {};
      Object.keys(scene).forEach((k) => { delete scene[k]; });
      Object.assign(scene, JSON.parse(JSON.stringify(restored)));
      recordHistorySnapshot(`Revert ${scene.id} to Take ${chosenTake.take_number}`);
      patchSavedSceneBaseline(scene);
      window.redrawTimeline?.();
      window.renderSceneList?.();
      if (typeof window.selectScene === "function" && window.state.selectedSceneIdx !== null) window.selectScene(window.state.selectedSceneIdx);
      window.loadProjectQueue?.();
      window.showToast?.(`↺ Reverted ${scene.id} to Take ${chosenTake.take_number} (using clip on disk)`);
    } catch (err) {
      alert("Error reverting scene: " + err.message);
    } finally {
      if (btnEl) { btnEl.disabled = false; btnEl.textContent = origText; }
    }
  }
  window.restoreSceneToTake = restoreSceneToTake;
  window.revertSceneToActiveTake = async (sceneId, btnEl) => {
    const sc = (window.state?.projectData?.scenes || []).find((s) => s.id === sceneId);
    if (!sc || !window.state?.currentSlug) return;
    const r = await fetch(`/api/takes/${encodeURIComponent(window.state.currentSlug)}/${encodeURIComponent(sceneId)}`);
    const d = r.ok ? await r.json() : null;
    const take = d?.takes?.find((t) => t.take_id === d.active_take_id) || d?.takes?.[d?.takes?.length - 1];
    if (take) await restoreSceneToTake(sc, take, btnEl);
    else alert(`No rendered take found in vault for ${sceneId}.`);
  };

  async function updateSceneTakesUI(scene) {
    const tray = document.getElementById("sceneTakesTray");
    const countBadge = document.getElementById("takesCountBadge");
    const selectEl = document.getElementById("sceneTakeSelect");
    const btnRestore = document.getElementById("btnRestoreTake");

    if (!tray || !selectEl || !scene || !window.state || !window.state.currentSlug) {
      currentTakesData = null; activeTakeObject = null; currentTakesSceneId = null;
      updateRevertHeaderBtn(null);
      if (tray) tray.style.display = "none";
      return;
    }

    try {
      const resp = await fetch(`/api/takes/${encodeURIComponent(window.state.currentSlug)}/${encodeURIComponent(scene.id)}`);
      if (!resp.ok) { currentTakesData = null; activeTakeObject = null; currentTakesSceneId = null; updateRevertHeaderBtn(null); tray.style.display = "none"; return; }
      const data = await resp.json();
      currentTakesData = data; currentTakesSceneId = scene.id;

      if (!data.success || !data.takes || data.takes.length === 0) {
        activeTakeObject = null; selectEl.innerHTML = ""; updateRevertHeaderBtn(null);
        const repBtn = document.getElementById("btnToggleLipSyncRepair");
        if (repBtn) { repBtn.dataset.forceDisabled = "1"; repBtn.disabled = true; }
        tray.style.display = "none"; return;
      }

      tray.style.display = "block";
      const totalTakes = data.takes.length;
      if (countBadge) countBadge.textContent = `${totalTakes} take${totalTakes > 1 ? "s" : ""}`;

      selectEl.innerHTML = "";
      const activeId = data.active_take_id;
      let activeTake = null;
      data.takes.forEach((take) => {
        const opt = document.createElement("option");
        opt.value = take.take_id;
        if (take.take_id === activeId) activeTake = take;
        opt.textContent = window.formatTakeSummaryLabel
          ? window.formatTakeSummaryLabel(take, activeId)
          : `Take ${take.take_number}: ${formatTakeDate(take)}${take.take_id === activeId ? " ★ [ACTIVE]" : ""}`;
        selectEl.appendChild(opt);
      });

      activeTakeObject = activeTake || data.takes[data.takes.length - 1] || null;
      if (activeId) selectEl.value = activeId;
      else if (data.takes.length > 0) selectEl.value = data.takes[data.takes.length - 1].take_id;

      renderSelectedTakeState(scene);
      selectEl.onchange = () => renderSelectedTakeState(scene);
      if (btnRestore) btnRestore.onclick = () => {
        const chosenTake = (currentTakesData?.takes || []).find((t) => t.take_id === selectEl.value);
        if (chosenTake) restoreSceneToTake(scene, chosenTake, btnRestore);
      };

      const btnUpscale = document.getElementById("btnUpscaleTake");
      const upSel = document.getElementById("sceneLateUpscaleMethod"), fltSel = document.getElementById("sceneLateFilterMethod");
      if (btnUpscale && (upSel || fltSel)) {
        btnUpscale.onclick = async () => {
          const method = upSel ? upSel.value : "", ai_refiner = fltSel ? fltSel.value : "";
          if (!method && !ai_refiner) { alert("Select a spatial upscaler or a 1x filter first."); return; }
          btnUpscale.disabled = true; btnUpscale.textContent = "Starting...";
          try {
            const upResp = await fetch("/api/takes/upscale", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ project: window.state.currentSlug, scene_id: scene.id, method, ai_refiner, take_id: selectEl.value || null, backend: document.getElementById("sceneLateUpscaleBackend")?.value || undefined }),
            });
            const upData = await upResp.json();
            if (upData.success) window.showToast?.(`✨ Started ${[method, ai_refiner].filter(Boolean).join(" + ")} on ${scene.id} (watch Live Logs).`);
            else alert("Upscale/Filter failed: " + (upData.error || "Unknown error"));
          } catch (err) { alert("Upscale request failed: " + err.message); }
          finally { btnUpscale.disabled = Boolean(scene.locked); btnUpscale.textContent = "✨ Upscale / Filter"; }
        };
      }
      window.syncTakeRepairUI?.(scene);
      window.applyInspectorLockState?.(scene);
    } catch (err) { console.warn("[Takes UI] Failed to load takes:", err); tray.style.display = "none"; }
  }

  function renderSelectedTakeState(scene, updateVideo = true) {
    const selectEl = document.getElementById("sceneTakeSelect");
    const btnRestore = document.getElementById("btnRestoreTake");
    const btnDeleteTake = document.getElementById("btnDeleteTake");
    const listEl = document.getElementById("sceneTakesList");
    const diffEl = document.getElementById("takeDiffSummary");
    const videoPlayer = document.getElementById("clipVideoPlayer");
    const clipBadge = document.getElementById("clipStatusBadge");
    if (!selectEl || !currentTakesData) return;
    const chosenTake = (currentTakesData.takes || []).find((t) => t.take_id === selectEl.value);
    if (!chosenTake) return;

    const isActive = chosenTake.take_id === currentTakesData.active_take_id;
    const { diffs } = computeSceneTakeDiffs(scene, chosenTake);
    updateRevertHeaderBtn(scene);

    if (btnRestore) {
      btnRestore.style.display = (!isActive || diffs.length > 0) ? "inline-block" : "none";
      btnRestore.textContent = isActive ? "↺ Revert Scene to Rendered" : "↺ Restore Take as Active";
      btnRestore.disabled = Boolean(scene.locked);
    }
    if (btnDeleteTake) {
      btnDeleteTake.disabled = Boolean(scene.locked);
      btnDeleteTake.onclick = () => window.deleteSceneTake?.(scene, chosenTake, btnDeleteTake);
    }
    if (listEl && window.renderTakesListItems) {
      window.renderTakesListItems(listEl, scene, currentTakesData, chosenTake.take_id, (t) => {
        selectEl.value = t.take_id;
        renderSelectedTakeState(scene, true);
      });
    }
    if (clipBadge) {
      clipBadge.textContent = isActive ? (diffs.length > 0 ? "Active Clip (Edited)" : "Active Take") : `Take ${chosenTake.take_number} (Vault)`;
      clipBadge.style.background = isActive ? (diffs.length > 0 ? "#d97706" : "#10b981") : "#8b5cf6";
    }
    if (updateVideo && videoPlayer) {
      const outName = chosenTake.out_name || scene.output_name || scene.id;
      const newSrc = `/media/${encodeURIComponent(window.state.currentSlug)}/clip/${encodeURIComponent(outName)}?take=${encodeURIComponent(chosenTake.take_id)}&t=${Date.now()}`;
      videoPlayer.setAttribute("data-loaded-src", newSrc);
      videoPlayer.src = newSrc;
      videoPlayer.load();
      const infoEl = document.getElementById("clipFileInfo");
      if (infoEl && chosenTake.out_name) {
        const szMb = chosenTake.size_bytes ? ` (${(chosenTake.size_bytes / (1024 * 1024)).toFixed(1)} MB)` : "";
        infoEl.textContent = `${chosenTake.out_name}.mp4${szMb}`;
      }
    }
    if (diffEl) {
      const dt = formatTakeDate(chosenTake), dStr = diffs.map(escapeHtmlText).join(" &bull; ");
      if (isActive && diffs.length === 0) diffEl.innerHTML = `<span style="color: #10b981; font-weight: 600;">✓ Active Take on Timeline</span> &bull; Rendered ${dt}`;
      else if (isActive) diffEl.innerHTML = `<span style="color: #f59e0b; font-weight: 600;">⚠ Edited since Take ${chosenTake.take_number} rendered:</span> ${dStr} <span style="color: var(--text-muted);">(${dt})</span>`;
      else diffEl.innerHTML = `<span style="color: #a78bfa; font-weight: 600;">Historical Take:</span> ${dStr || "Identical settings to current"} <span style="color: var(--text-muted); margin-left: 6px;">(${dt})</span>`;
    }
    const toggleRepBtn = document.getElementById("btnToggleLipSyncRepair");
    if (toggleRepBtn) {
      const isP1 = chosenTake.pass_num === 1 || String(chosenTake.out_name || "").includes("_Pass1_");
      toggleRepBtn.dataset.forceDisabled = isP1 ? "1" : "";
      toggleRepBtn.title = isP1 ? "Select the Pass 2 / Final take to repair lip-sync" : "Repair a single singer's lip-sync via cropped ROI without re-rendering Pass 1";
      const bar = document.getElementById("lipSyncRepairBar");
      if (isP1 && bar) bar.style.display = "none";
    }
    window.applyInspectorLockState?.(scene);
  }

  window.updateSceneTakesUI = updateSceneTakesUI;
  window.refreshTakeDiffUI = () => {
    const sc = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
    if (sc && currentTakesData && currentTakesSceneId === sc.id) renderSelectedTakeState(sc, false);
  };

  // =========================================================================
  // Tier 3: Project-Level Checkpoints Modal
  // =========================================================================
  async function openCheckpointsModal() {
    const modal = document.getElementById("checkpointsModal");
    if (!modal || !window.state || !window.state.currentSlug) return;
    modal.style.display = "flex";
    await refreshCheckpointsList();
  }

  function closeCheckpointsModal() {
    const modal = document.getElementById("checkpointsModal");
    if (modal) modal.style.display = "none";
  }

  async function refreshCheckpointsList() {
    const listEl = document.getElementById("checkpointsList");
    const statsEl = document.getElementById("vaultStatsDisplay");
    if (!listEl || !window.state.currentSlug) return;

    listEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 20px;">Loading checkpoints...</div>`;

    try {
      const resp = await fetch(`/api/checkpoints/${encodeURIComponent(window.state.currentSlug)}`);
      if (!resp.ok) {
        listEl.innerHTML = `<div style="color: #ef4444; padding: 12px;">Failed to load checkpoints.</div>`;
        return;
      }
      const data = await resp.json();
      const checkpoints = data.checkpoints || [];
      const stats = data.vault_stats || {};

      if (statsEl) {
        statsEl.innerHTML = `<span><strong>Vault Storage:</strong> ${stats.total_mb || 0} MB</span> &bull; <span><strong>Takes:</strong> ${stats.total_takes || 0}</span> &bull; <span><strong>Scenes:</strong> ${stats.total_scenes || 0}</span>`;
      }

      if (checkpoints.length === 0) {
        listEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 24px;">No checkpoints created yet. Checkpoints are automatically generated before rendering batches or you can create one manually below.</div>`;
        return;
      }

      listEl.innerHTML = "";
      checkpoints.forEach((cp) => {
        const item = document.createElement("div");
        item.className = "checkpoint-item";
        item.style.cssText =
          "background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 12px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; gap: 12px;";

        const triggerColor =
          cp.trigger === "manual" ? "#3b82f6" : cp.trigger === "pre_rollback" ? "#f59e0b" : "#8b5cf6";
        const triggerLabel =
          cp.trigger === "manual" ? "Manual" : cp.trigger === "pre_rollback" ? "Pre-Rollback" : "Auto Pre-Render";

        const left = document.createElement("div");
        left.style.cssText = "flex: 1; overflow: hidden;";
        left.innerHTML = `
          <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px;">
            <span style="background: ${triggerColor}22; color: ${triggerColor}; border: 1px solid ${triggerColor}44; font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px; text-transform: uppercase;">${triggerLabel}</span>
            <strong style="color: #f8fafc; font-size: 13px;">${escapeHtmlText(cp.name || "Checkpoint")}</strong>
            <span style="color: var(--text-muted); font-size: 11px;">(${cp.created_at ? cp.created_at.slice(0, 16).replace("T", " ") : ""})</span>
          </div>
          <div style="font-size: 11px; color: var(--text-muted);">
            <span>${cp.scenes_count || 0} scenes</span>
            ${cp.comment ? ` &bull; <em>${escapeHtmlText(cp.comment)}</em>` : ""}
          </div>
        `;

        const btnRollback = document.createElement("button");
        btnRollback.className = "btn btn-sm btn-primary";
        btnRollback.textContent = "↺ Rollback";
        btnRollback.style.cssText = "white-space: nowrap; padding: 4px 12px; font-size: 11px;";
        btnRollback.onclick = () => rollbackToCheckpoint(cp);

        item.appendChild(left);
        item.appendChild(btnRollback);
        listEl.appendChild(item);
      });
    } catch (err) {
      listEl.innerHTML = `<div style="color: #ef4444; padding: 12px;">Error: ${escapeHtmlText(err.message)}</div>`;
    }
  }

  async function createManualCheckpoint() {
    if (!window.state || !window.state.currentSlug) return;
    const name = prompt("Enter a name for this checkpoint (e.g., 'Before Chorus Redesign'):", "");
    if (name === null) return;
    const comment = prompt("Optional comment or notes:", "");
    if (comment === null) return;

    try {
      const resp = await fetch("/api/checkpoints/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: window.state.currentSlug, name: name.trim() || "Manual Checkpoint", comment: (comment || "").trim() }),
      });
      const data = await resp.json();
      if (data.success) { window.showToast?.("✓ Checkpoint created successfully!"); await refreshCheckpointsList(); }
      else alert("Failed to create checkpoint: " + (data.error || "Unknown error"));
    } catch (err) {
      alert("Error creating checkpoint: " + err.message);
    }
  }

  async function rollbackToCheckpoint(cp) {
    if (!window.state || !window.state.currentSlug) return;
    if (!confirm(`Rollback project to checkpoint '${cp.name}'?\n\nA pre-rollback safety snapshot will be created automatically so you can undo this rollback if needed.`)) return;

    try {
      const resp = await fetch("/api/checkpoints/rollback", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: window.state.currentSlug, checkpoint_id: cp.id }),
      });
      const data = await resp.json();
      if (data.success) {
        window.showToast?.("✓ Project rolled back successfully!");
        closeCheckpointsModal();
        if (typeof window.selectProject === "function") await window.selectProject(window.state.currentSlug);
      } else {
        alert("Rollback failed: " + (data.error || "Unknown error"));
      }
    } catch (err) {
      alert("Error during rollback: " + err.message);
    }
  }

  function escapeHtmlText(str) {
    return str ? String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;") : "";
  }
  window.openCheckpointsModal = openCheckpointsModal;
  window.closeCheckpointsModal = closeCheckpointsModal;

  function initTakesAndHistoryUI() {
    const bindClick = (ids, fn) => {
      const el = ids.map((id) => document.getElementById(id)).find(Boolean);
      if (el) el.onclick = fn;
    };
    bindClick(["btnUndo", "btnTimelineUndo"], () => performUndo());
    bindClick(["btnRedo", "btnTimelineRedo"], () => performRedo());
    bindClick(["btnCheckpointsModal", "btnOpenCheckpoints"], () => openCheckpointsModal());
    bindClick(["btnCloseCheckpointsModal"], () => closeCheckpointsModal());
    bindClick(["btnCreateCheckpoint"], () => createManualCheckpoint());
    bindClick(["btnDiscardChanges"], () => {
      if (confirm("Discard all unsaved edits and revert back to the last saved state?")) discardUnsavedChanges();
    });
    const formEl = document.getElementById("inspectorForm");
    if (formEl) {
      let tmr = null;
      const sched = () => { clearTimeout(tmr); tmr = setTimeout(() => window.refreshTakeDiffUI?.(), 100); };
      formEl.addEventListener("input", sched);
      formEl.addEventListener("change", sched);
      formEl.addEventListener("click", (e) => { if (e.target?.closest("button, .label-actions span")) sched(); });
    }
    updateHistoryButtons();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initTakesAndHistoryUI);
  else initTakesAndHistoryUI();
})();
