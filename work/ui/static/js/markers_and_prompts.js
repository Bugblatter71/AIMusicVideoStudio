// ============================================================================
// Marker System (Timeline Cues & Relative Prompt Tags)
// ============================================================================
function addMarkerAtPlayhead() {
  const t = state.playheadTime || audioEl.currentTime || 0;
  addMarkerAtTime(t);
}

function addMarkerAtTime(t) {
  state.projectData.markers = state.projectData.markers || [];
  const existingNames = new Set(state.projectData.markers.map((m) => m.name));
  let defaultName = `Cue at ${formatTime(t).split(".")[0]}`;
  let counter = 1;
  while (existingNames.has(defaultName)) {
    defaultName = `Cue ${counter++}`;
  }

  const newMarker = {
    id: `M${String(state.projectData.markers.length + 1).padStart(2, "0")}`,
    name: defaultName,
    time: parseFloat(t.toFixed(2)),
    color: "#f59e0b",
  };

  state.projectData.markers.push(newMarker);
  state.projectData.markers.sort((a, b) => a.time - b.time);
  const newIdx = state.projectData.markers.findIndex((m) => m === newMarker);

  markProjectDirty();
  redrawTimeline();
  if (state.selectedSceneIdx !== null) {
    const sc = state.projectData.scenes[state.selectedSceneIdx];
    updateSceneMarkersBar(sc);
    updatePromptResolvedPreviews(sc);
  }
  if (typeof window.recordHistorySnapshot === "function") {
    window.recordHistorySnapshot(`Add Marker ${newMarker.name}`);
  }
  openMarkerModal(newIdx !== -1 ? newIdx : state.projectData.markers.length - 1);
  showToast(`Placed marker at ${formatTime(t)}`);
}

function openMarkerModal(markerIdx) {
  state.selectedMarkerIdx = markerIdx;
  const m = state.projectData?.markers?.[markerIdx];
  if (!m) return;

  document.getElementById("markerModalTitle").textContent = `🚩 Edit Marker: ${m.name || "Cue"}`;
  document.getElementById("markerNameInput").value = m.name || "";
  document.getElementById("markerTimeInput").value = m.time !== undefined ? m.time.toFixed(2) : 0;

  const swatches = document.querySelectorAll("#markerColorPalette .color-swatch");
  swatches.forEach((s) => {
    if (s.dataset.color === (m.color || "#f59e0b")) {
      s.classList.add("active");
      s.style.border = "2px solid #fff";
    } else {
      s.classList.remove("active");
      s.style.border = "2px solid transparent";
    }
  });

  const modal = document.getElementById("markerModal");
  modal.style.display = "flex";
  document.getElementById("markerNameInput").focus();
}

function closeMarkerModal() {
  const modal = document.getElementById("markerModal");
  if (modal) modal.style.display = "none";
  state.selectedMarkerIdx = null;
}

function initMarkerModalEvents() {
  document.getElementById("btnMarkerModalClose")?.addEventListener("click", closeMarkerModal);

  document.getElementById("markerModal")?.addEventListener("click", (e) => {
    if (e.target.id === "markerModal") closeMarkerModal();
  });

  document.querySelectorAll("#markerColorPalette .color-swatch").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#markerColorPalette .color-swatch").forEach((b) => {
        b.classList.remove("active");
        b.style.border = "2px solid transparent";
      });
      btn.classList.add("active");
      btn.style.border = "2px solid #fff";
    });
  });

  document.getElementById("btnMarkerNudgeDown")?.addEventListener("click", () => {
    const input = document.getElementById("markerTimeInput");
    input.value = Math.max(0, parseFloat((parseFloat(input.value || 0) - 0.1).toFixed(2)));
  });

  document.getElementById("btnMarkerNudgeUp")?.addEventListener("click", () => {
    const input = document.getElementById("markerTimeInput");
    input.value = Math.max(0, parseFloat((parseFloat(input.value || 0) + 0.1).toFixed(2)));
  });

  document.getElementById("btnSaveMarker")?.addEventListener("click", () => {
    if (state.selectedMarkerIdx === null) return;
    const m = state.projectData?.markers?.[state.selectedMarkerIdx];
    if (!m) return;

    const newName = document.getElementById("markerNameInput").value.trim() || m.name;
    const newTime = Math.max(0, parseFloat(parseFloat(document.getElementById("markerTimeInput").value || 0).toFixed(2)));
    const activeColorBtn = document.querySelector("#markerColorPalette .color-swatch.active");
    const newColor = activeColorBtn ? activeColorBtn.dataset.color : (m.color || "#f59e0b");

    m.name = newName;
    m.time = newTime;
    m.color = newColor;

    state.projectData.markers.sort((a, b) => a.time - b.time);
    markProjectDirty();
    redrawTimeline();
    if (state.selectedSceneIdx !== null) {
      const sc = state.projectData.scenes[state.selectedSceneIdx];
      updateSceneMarkersBar(sc);
      updatePromptResolvedPreviews(sc);
    }
    if (typeof window.recordHistorySnapshot === "function") {
      window.recordHistorySnapshot(`Edit Marker ${newName}`);
    }
    closeMarkerModal();
    showToast(`Saved marker: "${newName}" (${newTime}s)`);
  });

  document.getElementById("btnDeleteMarker")?.addEventListener("click", () => {
    if (state.selectedMarkerIdx === null) return;
    const m = state.projectData?.markers?.[state.selectedMarkerIdx];
    const name = m ? m.name : "marker";
    state.projectData.markers.splice(state.selectedMarkerIdx, 1);
    markProjectDirty();
    redrawTimeline();
    if (state.selectedSceneIdx !== null) {
      const sc = state.projectData.scenes[state.selectedSceneIdx];
      updateSceneMarkersBar(sc);
      updatePromptResolvedPreviews(sc);
    }
    if (typeof window.recordHistorySnapshot === "function") {
      window.recordHistorySnapshot(`Delete Marker ${name}`);
    }
    closeMarkerModal();
    showToast(`Deleted marker: "${name}"`);
  });

  document.getElementById("btnJumpToMarker")?.addEventListener("click", () => {
    if (state.selectedMarkerIdx === null) return;
    const m = state.projectData?.markers?.[state.selectedMarkerIdx];
    if (m) {
      seekTimeline(m.time, null, true);
      closeMarkerModal();
    }
  });
}

function updateSceneMarkersBar(sc) {
  const bar = document.getElementById("sceneMarkersBar");
  const list = document.getElementById("sceneMarkersList");
  if (!bar || !list) return;

  const markers = state.projectData?.markers || [];
  const inScene = markers.filter((m) => m.time >= sc.start && m.time <= sc.end);

  if (inScene.length === 0) {
    bar.style.display = "none";
    list.innerHTML = "";
    return;
  }

  let rawHead = null;
  for (const k of ["render_pad_start", "pad_start", "head_overlap"]) {
    if (sc[k] !== undefined && sc[k] !== null && String(sc[k]).trim() !== "") { rawHead = Number(sc[k]); break; }
  }
  if (rawHead === null && sc.render_overlap !== undefined && sc.render_overlap !== null) {
    rawHead = typeof sc.render_overlap === "object" ? Number(sc.render_overlap.head || 0) : Number(sc.render_overlap);
  }
  if (rawHead === null) rawHead = Number(state.projectData?.project?.render_overlap_seconds || 0);
  const headPad = Math.max(0, Math.min(rawHead, Number(sc.start || 0)));
  const reqStart = Math.max(0, Number(sc.start || 0) - headPad);

  bar.style.display = "block";
  list.innerHTML = "";
  inScene.forEach((m) => {
    const rel = m.time - reqStart;
    const fps = Number(state.projectData?.project?.fps) || 24;
    const relFrames = Math.max(0, Math.round(rel * fps));
    const pill = document.createElement("div");
    pill.style.cssText = `display: flex; align-items: center; gap: 6px; padding: 3px 8px; border-radius: 4px; background: rgba(15, 23, 42, 0.7); border: 1px solid ${m.color || "#f59e0b"}; font-size: 11px;`;
    pill.innerHTML = `
      <span style="color: ${m.color || "#f59e0b"}; font-weight: 600;">🚩 ${escapeHtml(m.name)}</span>
      <span style="color: var(--text-muted); font-size: 10px;">(+${rel.toFixed(2)}s / F${relFrames})</span>
      <button type="button" class="btn btn-tag-s" style="padding: 1px 6px; font-size: 10px; background: #334155; cursor: pointer;" title="Insert <${escapeHtml(m.name)}> (+${rel.toFixed(2)}s) into prompt">+ Tag (s)</button>
      <button type="button" class="btn btn-tag-f" style="padding: 1px 6px; font-size: 10px; background: #334155; cursor: pointer;" title="Insert <${escapeHtml(m.name)}:frame> (frame ${relFrames}) into prompt">+ Tag (frame)</button>
    `;
    pill.querySelector(".btn-tag-s")?.addEventListener("click", () => {
      insertMarkerTagIntoPrompt(`<${m.name}>`);
    });
    pill.querySelector(".btn-tag-f")?.addEventListener("click", () => {
      insertMarkerTagIntoPrompt(`<${m.name}:frame>`);
    });
    list.appendChild(pill);
  });
}

let lastFocusedPromptTextarea = null;
["scenePrompt", "scenePromptM", "scenePromptF"].forEach((id) => {
  document.getElementById(id)?.addEventListener("focus", (e) => {
    lastFocusedPromptTextarea = e.target;
  });
});

function insertMarkerTagIntoPrompt(tag) {
  let textarea = lastFocusedPromptTextarea;
  if (!textarea || textarea.offsetParent === null) {
    const sc = state.selectedSceneIdx !== null ? state.projectData?.scenes?.[state.selectedSceneIdx] : null;
    if (sc && isTwoPassScene(sc)) {
      textarea = document.getElementById("scenePromptM");
    } else {
      textarea = document.getElementById("scenePrompt");
    }
  }
  if (!textarea) return;
  const start = textarea.selectionStart || textarea.value.length;
  const end = textarea.selectionEnd || textarea.value.length;
  const val = textarea.value;
  textarea.value = val.substring(0, start) + tag + val.substring(end);
  textarea.selectionStart = textarea.selectionEnd = start + tag.length;
  textarea.focus();

  textarea.dispatchEvent(new Event("input"));
  if (typeof window.recordHistorySnapshot === "function") {
    window.recordHistorySnapshot(`Insert Tag ${tag}`);
  }
  showToast(`Inserted ${tag}`);
}

function autoResizeTextarea(el) {
  if (!el || el.offsetParent === null) return;
  el.style.height = "auto";
  const newHeight = Math.max(160, el.scrollHeight + 4);
  el.style.height = `${newHeight}px`;
}

function resizeAllPromptTextareas() {
  requestAnimationFrame(() => {
    autoResizeTextarea(document.getElementById("scenePrompt"));
    autoResizeTextarea(document.getElementById("scenePromptM"));
    autoResizeTextarea(document.getElementById("scenePromptF"));
  });
}
window.addEventListener("resize", resizeAllPromptTextareas);

function updatePromptResolvedPreviews(sc) {
  resizeAllPromptTextareas();
  if (window.updatePromptReferenceTray) {
    window.updatePromptReferenceTray(sc);
  }
}

