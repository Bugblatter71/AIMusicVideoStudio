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

  async function updateSceneTakesUI(scene) {
    const tray = document.getElementById("sceneTakesTray");
    const countBadge = document.getElementById("takesCountBadge");
    const selectEl = document.getElementById("sceneTakeSelect");
    const btnRestore = document.getElementById("btnRestoreTake");
    const diffEl = document.getElementById("takeDiffSummary");

    if (!tray || !selectEl || !scene || !window.state || !window.state.currentSlug) {
      if (tray) tray.style.display = "none";
      return;
    }

    try {
      const resp = await fetch(
        `/api/takes/${encodeURIComponent(window.state.currentSlug)}/${encodeURIComponent(scene.id)}`
      );
      if (!resp.ok) {
        tray.style.display = "none";
        return;
      }
      const data = await resp.json();
      currentTakesData = data;

      if (!data.success || !data.takes || data.takes.length === 0) {
        tray.style.display = "none";
        return;
      }

      tray.style.display = "block";
      const totalTakes = data.takes.length;
      if (countBadge) {
        countBadge.textContent = `${totalTakes} take${totalTakes > 1 ? "s" : ""}`;
      }

      selectEl.innerHTML = "";
      const activeId = data.active_take_id;
      let activeTake = null;

      data.takes.forEach((take) => {
        const opt = document.createElement("option");
        opt.value = take.take_id;
        const isActive = take.take_id === activeId;
        if (isActive) activeTake = take;

        const dateStr = take.created_at ? take.created_at.slice(0, 16).replace("T", " ") : "";
        const tag = isActive ? "★ [ACTIVE]" : "";
        const modelStr = take.model_type ? ` [${take.model_type}]` : "";
        opt.textContent = `Take ${take.take_number}: ${dateStr} ${tag}${modelStr}`;
        selectEl.appendChild(opt);
      });

      activeTakeObject = activeTake;

      // Select active take by default
      if (activeId) {
        selectEl.value = activeId;
      } else if (data.takes.length > 0) {
        selectEl.value = data.takes[data.takes.length - 1].take_id;
      }

      renderSelectedTakeState(scene);

      selectEl.onchange = () => {
        renderSelectedTakeState(scene);
      };

      if (btnRestore) {
        btnRestore.onclick = async () => {
          const selectedTakeId = selectEl.value;
          const chosenTake = (currentTakesData?.takes || []).find((t) => t.take_id === selectedTakeId);
          if (!chosenTake) return;

          btnRestore.disabled = true;
          btnRestore.textContent = "Restoring...";

          try {
            const actResp = await fetch("/api/takes/activate", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                project: window.state.currentSlug,
                scene_id: scene.id,
                take_id: chosenTake.take_id,
              }),
            });
            const actData = await actResp.json();
            if (actData.success) {
              // Record history before mutating scene params
              recordHistorySnapshot(`Restore Take ${chosenTake.take_number} for Scene ${scene.id}`);

              // Update in-memory scene attributes
              const restored = actData.restored_params || {};
              if (restored.prompt !== undefined) scene.prompt = restored.prompt;
              if (restored.prompt_m !== undefined) scene.prompt_m = restored.prompt_m;
              if (restored.prompt_f !== undefined) scene.prompt_f = restored.prompt_f;
              if (restored.seed !== undefined) scene.seed = restored.seed;
              if (restored.video_renderer !== undefined) scene.video_renderer = restored.video_renderer;
              if (restored.mode !== undefined) scene.mode = restored.mode;
              if (restored.stem !== undefined) scene.stem = restored.stem;
              if (restored.anchor !== undefined) scene.anchor = restored.anchor;
              if (restored.anchor_end !== undefined) scene.anchor_end = restored.anchor_end;
              else delete scene.anchor_end;
              if (restored.render_pad_start !== undefined) scene.render_pad_start = restored.render_pad_start;
              else delete scene.render_pad_start;
              if (restored.render_pad_end !== undefined) scene.render_pad_end = restored.render_pad_end;
              else delete scene.render_pad_end;

              // Save silently to project config
              if (typeof window.saveProjectData === "function") {
                await window.saveProjectData(false);
              }

              // Re-select scene to refresh form controls
              if (typeof window.selectScene === "function" && window.state.selectedSceneIdx !== null) {
                window.selectScene(window.state.selectedSceneIdx);
              }

              // Refresh queue in console if present
              if (typeof window.loadProjectQueue === "function") {
                window.loadProjectQueue();
              }

              if (typeof window.showToast === "function") {
                window.showToast(`↺ Restored Take ${chosenTake.take_number} as Active!`);
              }
            } else {
              alert("Failed to restore take: " + (actData.error || "Unknown error"));
            }
          } catch (err) {
            alert("Error restoring take: " + err.message);
          } finally {
            btnRestore.disabled = false;
            btnRestore.textContent = "↺ Restore Take as Active";
          }
        };
      }
    } catch (err) {
      console.warn("[Takes UI] Failed to load takes:", err);
      tray.style.display = "none";
    }
  }

  function renderSelectedTakeState(scene) {
    const selectEl = document.getElementById("sceneTakeSelect");
    const btnRestore = document.getElementById("btnRestoreTake");
    const diffEl = document.getElementById("takeDiffSummary");
    const videoPlayer = document.getElementById("clipVideoPlayer");
    const clipBadge = document.getElementById("clipStatusBadge");

    if (!selectEl || !currentTakesData) return;
    const selectedTakeId = selectEl.value;
    const chosenTake = (currentTakesData.takes || []).find((t) => t.take_id === selectedTakeId);
    if (!chosenTake) return;

    const isActive = chosenTake.take_id === currentTakesData.active_take_id;

    if (isActive) {
      if (btnRestore) btnRestore.style.display = "none";
      if (diffEl) {
        diffEl.innerHTML = `<span style="color: #10b981; font-weight: 600;">✓ Active Take on Timeline</span> &bull; Rendered ${chosenTake.created_at || ""}`;
      }
      if (clipBadge) {
        clipBadge.textContent = "Active Take";
        clipBadge.style.background = "#10b981";
      }
      // Preview active file normal stream
      if (videoPlayer) {
        const outName = scene.output_name || scene.id;
        videoPlayer.src = `/media/${encodeURIComponent(window.state.currentSlug)}/clip/${encodeURIComponent(outName)}?t=${Date.now()}`;
        videoPlayer.load();
      }
    } else {
      if (btnRestore) btnRestore.style.display = "inline-block";
      if (clipBadge) {
        clipBadge.textContent = `Take ${chosenTake.take_number} (Vault)`;
        clipBadge.style.background = "#8b5cf6";
      }
      // Preview historical take file directly using ?take= param
      if (videoPlayer) {
        const outName = scene.output_name || scene.id;
        videoPlayer.src = `/media/${encodeURIComponent(window.state.currentSlug)}/clip/${encodeURIComponent(outName)}?take=${encodeURIComponent(chosenTake.take_id)}`;
        videoPlayer.load();
      }

      // Compute diff summary vs active scene
      if (diffEl) {
        const diffs = [];
        const curPrompt = (scene.prompt || scene.prompt_m || "").trim();
        const takePrompt = (chosenTake.prompt || chosenTake.prompt_m || "").trim();
        if (curPrompt !== takePrompt) {
          diffs.push(`Prompt differed`);
        }
        if (chosenTake.seed !== undefined && chosenTake.seed !== scene.seed) {
          diffs.push(`Seed: ${chosenTake.seed} (current: ${scene.seed ?? "default"})`);
        }
        if (chosenTake.model_type && chosenTake.model_type !== (scene.video_renderer || "")) {
          diffs.push(`Model: ${chosenTake.model_type}`);
        }
        if (chosenTake.duration && Math.abs(chosenTake.duration - (scene.end - scene.start)) > 0.05) {
          diffs.push(`Duration: ${chosenTake.duration}s`);
        }
        const snapEnd = chosenTake.scene_snapshot?.anchor_end || "";
        const curEnd = scene.anchor_end || "";
        if (snapEnd !== curEnd) {
          diffs.push(`End Anchor: ${snapEnd || "None"}`);
        }
        const snapPadStart = chosenTake.scene_snapshot?.render_pad_start ?? "";
        const curPadStart = scene.render_pad_start ?? "";
        const snapPadEnd = chosenTake.scene_snapshot?.render_pad_end ?? "";
        const curPadEnd = scene.render_pad_end ?? "";
        if (snapPadStart !== curPadStart || snapPadEnd !== curPadEnd) {
          diffs.push(`Pad: Head +${snapPadStart || 0}s / Tail +${snapPadEnd || 0}s (current: +${curPadStart || 0}s / +${curPadEnd || 0}s)`);
        }

        const diffStr = diffs.length > 0 ? diffs.join(" &bull; ") : "Identical settings to current";
        diffEl.innerHTML = `<span style="color: #a78bfa; font-weight: 600;">Historical Take:</span> ${diffStr} <span style="color: var(--text-muted); margin-left: 6px;">(${chosenTake.created_at || ""})</span>`;
      }
    }
  }

  window.updateSceneTakesUI = updateSceneTakesUI;

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
      listEl.innerHTML = `<div style="color: #ef4444; padding: 12px;">Error: ${err.message}</div>`;
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
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project: window.state.currentSlug,
          name: name.trim() || "Manual Checkpoint",
          comment: (comment || "").trim(),
        }),
      });
      const data = await resp.json();
      if (data.success) {
        if (typeof window.showToast === "function") {
          window.showToast("✓ Checkpoint created successfully!");
        }
        await refreshCheckpointsList();
      } else {
        alert("Failed to create checkpoint: " + (data.error || "Unknown error"));
      }
    } catch (err) {
      alert("Error creating checkpoint: " + err.message);
    }
  }

  async function rollbackToCheckpoint(cp) {
    if (!window.state || !window.state.currentSlug) return;
    const confirmMsg = `Rollback project to checkpoint '${cp.name}'?\n\nA pre-rollback safety snapshot will be created automatically so you can undo this rollback if needed.`;
    if (!confirm(confirmMsg)) return;

    try {
      const resp = await fetch("/api/checkpoints/rollback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project: window.state.currentSlug,
          checkpoint_id: cp.id,
        }),
      });
      const data = await resp.json();
      if (data.success) {
        if (typeof window.showToast === "function") {
          window.showToast("✓ Project rolled back successfully!");
        }
        closeCheckpointsModal();
        // Reload project to update all views
        if (typeof window.selectProject === "function") {
          await window.selectProject(window.state.currentSlug);
        }
      } else {
        alert("Rollback failed: " + (data.error || "Unknown error"));
      }
    } catch (err) {
      alert("Error during rollback: " + err.message);
    }
  }

  function escapeHtmlText(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  window.openCheckpointsModal = openCheckpointsModal;
  window.closeCheckpointsModal = closeCheckpointsModal;

  // =========================================================================
  // Initialize UI Bindings
  // =========================================================================
  function initTakesAndHistoryUI() {
    const btnUndo = document.getElementById("btnUndo") || document.getElementById("btnTimelineUndo");
    const btnRedo = document.getElementById("btnRedo") || document.getElementById("btnTimelineRedo");
    const btnModalOpen = document.getElementById("btnCheckpointsModal") || document.getElementById("btnOpenCheckpoints");
    const btnModalClose = document.getElementById("btnCloseCheckpointsModal");
    const btnCreateCp = document.getElementById("btnCreateCheckpoint");
    const btnDiscard = document.getElementById("btnDiscardChanges");

    if (btnUndo) btnUndo.onclick = () => performUndo();
    if (btnRedo) btnRedo.onclick = () => performRedo();
    if (btnModalOpen) btnModalOpen.onclick = () => openCheckpointsModal();
    if (btnModalClose) btnModalClose.onclick = () => closeCheckpointsModal();
    if (btnCreateCp) btnCreateCp.onclick = () => createManualCheckpoint();
    if (btnDiscard) {
      btnDiscard.onclick = () => {
        if (confirm("Discard all unsaved edits and revert back to the last saved state?")) {
          discardUnsavedChanges();
        }
      };
    }

    updateHistoryButtons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTakesAndHistoryUI);
  } else {
    initTakesAndHistoryUI();
  }
})();
