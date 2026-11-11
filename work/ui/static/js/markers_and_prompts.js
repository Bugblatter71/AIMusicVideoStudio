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
[
  "scenePrompt", "scenePromptM", "scenePromptF",
  "compMusicians", "compPerformance", "compPerformanceM", "compPerformanceF", "compCamera",
].forEach((id) => {
  document.getElementById(id)?.addEventListener("focus", (e) => {
    lastFocusedPromptTextarea = e.target;
  });
});

function insertMarkerTagIntoPrompt(tag) {
  let textarea = lastFocusedPromptTextarea;
  const sc = state.selectedSceneIdx !== null ? state.projectData?.scenes?.[state.selectedSceneIdx] : null;
  if (!textarea || textarea.offsetParent === null) {
    if (sc && sc.prompt_mode === "components") {
      textarea = document.getElementById("compMusicians") || document.getElementById("compPerformance");
    } else if (sc && isTwoPassScene(sc)) {
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

function ensureSentencePeriodJS(text) {
  const s = String(text || "").trim();
  if (!s) return "";
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

function compileModularPromptJS(sc, passNum = null) {
  if (!sc) return "";
  if (sc.prompt_mode !== "components") {
    if (passNum === 1) return sc.prompt_m || sc.prompt || "";
    if (passNum === 2) return sc.prompt_f || sc.prompt || "";
    return sc.prompt || sc.prompt_m || sc.prompt_f || "";
  }
  const lockCam = sc.duet_lock_camera !== false;
  let camera = "";
  if (passNum === 1 && !lockCam && sc.prompt_camera_m) camera = String(sc.prompt_camera_m).trim();
  else if (passNum === 2 && !lockCam && sc.prompt_camera_f) camera = String(sc.prompt_camera_f).trim();
  else camera = String(sc.prompt_camera || "").trim();

  const atmo = String(sc.prompt_atmosphere || "").trim();
  let perf = "";
  let sound = "";
  if (passNum === 1) {
    perf = String(sc.prompt_performance_m || sc.prompt_performance || "").trim();
    sound = String(sc.prompt_soundscape_m || sc.prompt_soundscape || "Studio room tone and vocal performance.").trim();
  } else if (passNum === 2) {
    perf = String(sc.prompt_performance_f || sc.prompt_performance || "").trim();
    sound = String(sc.prompt_soundscape_f || sc.prompt_soundscape || "Quiet studio room tone, no speech.").trim();
  } else {
    perf = String(sc.prompt_performance || "").trim();
    sound = String(sc.prompt_soundscape || "Studio room tone under the vocal performance.").trim();
  }
  const musicians = String(sc.prompt_musicians || "").trim();
  const hasEndAnchor = Boolean(sc.anchor_end) && passNum !== 2;
  const leadIn = hasEndAnchor
    ? "[Shot 1] Continue seamlessly from <Picture 1> and transition smoothly into <Picture 2> by the end of the shot."
    : "[Shot 1] Continue directly from <Picture 1>.";

  const visualClauses = [camera, atmo, perf, musicians]
    .map((p) => ensureSentencePeriodJS(p))
    .filter(Boolean);
  const visualBody = visualClauses.join(" ");
  const soundClean = sound && sound.toLowerCase() !== "n/a" ? ensureSentencePeriodJS(sound) : (sound || "Studio room tone.");

  return (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n" +
    `integrated_multimodal_description: ${leadIn}${visualBody ? " " + visualBody : ""}\n` +
    `overall_soundscape: ${soundClean}\n` +
    "non_diegetic_music: N/A"
  );
}

function parseRawPromptToComponentsJS(rawPrompt) {
  const res = {
    prompt_camera: "",
    prompt_atmosphere: "",
    prompt_performance: "",
    prompt_musicians: "",
    prompt_soundscape: "Studio room tone under the vocal performance.",
  };
  if (!rawPrompt || !String(rawPrompt).trim()) return res;

  const descLines = [];
  const soundLines = [];
  let section = "desc";

  String(rawPrompt).split(/\r?\n/).forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) return;
    const low = line.toLowerCase();
    if (low.startsWith("for the target video,")) return;
    if (low.startsWith("non_diegetic_music:")) { section = "music"; return; }
    if (low.startsWith("overall_soundscape:")) {
      section = "sound";
      const val = line.slice("overall_soundscape:".length).trim();
      if (val) soundLines.push(val);
      return;
    }
    if (low.startsWith("integrated_multimodal_description:")) {
      section = "desc";
      const val = line.slice("integrated_multimodal_description:".length).trim();
      if (val) descLines.push(val);
      return;
    }
    if (section === "desc") descLines.push(line);
    else if (section === "sound") soundLines.push(line);
  });

  if (soundLines.length > 0) res.prompt_soundscape = soundLines.join(" ").trim();

  let fullDesc = descLines.join(" ").trim();
  fullDesc = fullDesc.replace(
    /^\[Shot\s*1\]\s*Continue\s+(?:directly|seamlessly)\s+from\s+<Picture\s*1>(?:\s+and\s+transition\s+smoothly\s+into\s+<Picture\s*2>\s+by\s+the\s+end\s+of\s+the\s+shot)?\s*\.?\s*/i,
    ""
  ).trim();
  if (!fullDesc) return res;

  const sentences = fullDesc.split(/(?<=[.!?])\s+/).map((s) => s.trim()).filter(Boolean);
  const camParts = [], atmoParts = [], perfParts = [], musParts = [];
  const camRe = /\b(camera|tripod|push-in|dolly|orbital|tracking\s+shot|pan|panning|tilt|zoom|wide-angle\s+shot|medium-wide\s+shot|crane\s+shot|fixed\s+horizon|locked-off)\b/i;
  const leadRe = /(\{MALE_|\{FEMALE_|@[a-zA-Z0-9_-]+|\bsings\b|\bsinging\b|\blistening\b|\blip\s+movements\b)/i;
  const musRe = /(\bdrummer\b|\bdrum\s+kit\b|\bdrumsticks\b|\bpianist\b|\bwoodwind\b|\bbassoon|\bclarinet|\bbrass\b|\btrumpet|\btrombone|\bbass\s+guitar|\bwait\s+and\s+don't\s+play\b|<[^>]+>)/i;
  const atmoRe = /^(Across\s+the\s+unified\s+wooden\s+floor|Inside\s+the\s+dark\s+recording\s+soundstage|Surrounding\s+her\s+in\s+atmospheric\s+bokeh|On\s+the\s+wooden\s+floor)\b/i;

  sentences.forEach((sent) => {
    const hasLead = leadRe.test(sent);
    const hasCam = camRe.test(sent);
    const hasMus = musRe.test(sent);
    if (hasCam && !hasLead && !hasMus) camParts.push(sent);
    else if (hasMus && !hasLead) musParts.push(sent);
    else if (atmoRe.test(sent) && !hasLead && !hasMus) atmoParts.push(sent);
    else perfParts.push(sent);
  });

  res.prompt_camera = camParts.join(" ").trim();
  res.prompt_atmosphere = atmoParts.join(" ").trim();
  res.prompt_performance = perfParts.join(" ").trim();
  res.prompt_musicians = musParts.join(" ").trim();
  return res;
}

function hydrateSceneComponentsFromRawIfNeeded(sc) {
  if (!sc) return;
  const isTwo = isTwoPassScene(sc);
  const hasAnyComp = Boolean(
    sc.prompt_camera || sc.prompt_atmosphere || sc.prompt_performance ||
    sc.prompt_performance_m || sc.prompt_performance_f || sc.prompt_musicians
  );
  if (hasAnyComp) return;

  if (isTwo) {
    const p1 = parseRawPromptToComponentsJS(sc.prompt_m || sc.prompt || "");
    const p2 = parseRawPromptToComponentsJS(sc.prompt_f || sc.prompt || "");
    sc.prompt_camera = p1.prompt_camera || p2.prompt_camera || "";
    sc.prompt_atmosphere = p1.prompt_atmosphere || p2.prompt_atmosphere || "";
    sc.prompt_performance_m = p1.prompt_performance || "";
    sc.prompt_performance_f = p2.prompt_performance || "";
    sc.prompt_musicians = p1.prompt_musicians || p2.prompt_musicians || "";
    sc.prompt_soundscape_m = p1.prompt_soundscape || "Studio room tone and male vocal.";
    sc.prompt_soundscape_f = p2.prompt_soundscape || "Quiet studio room tone, no speech.";
  } else {
    const parsed = parseRawPromptToComponentsJS(sc.prompt || "");
    sc.prompt_camera = parsed.prompt_camera;
    sc.prompt_atmosphere = parsed.prompt_atmosphere;
    sc.prompt_performance = parsed.prompt_performance;
    sc.prompt_musicians = parsed.prompt_musicians;
    sc.prompt_soundscape = parsed.prompt_soundscape;
  }
}

function syncCanonicalPromptsFromComponents(sc) {
  if (!sc || sc.prompt_mode !== "components") return;
  const isTwo = isTwoPassScene(sc);
  const el = (id) => document.getElementById(id);
  if (isTwo) {
    sc.prompt_m = compileModularPromptJS(sc, 1);
    sc.prompt_f = compileModularPromptJS(sc, 2);
    if (el("scenePromptM")) el("scenePromptM").value = sc.prompt_m;
    if (el("scenePromptF")) el("scenePromptF").value = sc.prompt_f;
    if (el("compCompiledPreviewText")) {
      el("compCompiledPreviewText").textContent = `[Pass 1 — Left]\n${sc.prompt_m}\n\n[Pass 2 — Right]\n${sc.prompt_f}`;
    }
  } else {
    sc.prompt = compileModularPromptJS(sc, null);
    if (el("scenePrompt")) el("scenePrompt").value = sc.prompt;
    if (el("compCompiledPreviewText")) {
      el("compCompiledPreviewText").textContent = sc.prompt;
    }
  }
}

function updateSceneNegativeBadgeAndChips(sc) {
  const el = (id) => document.getElementById(id);
  const badge = el("sceneNegBadge");
  const mode = sc?.negative_prompt_mode || "inherit";
  const rawNeg = [sc?.negative_prompt, sc?.negative_prompt_m, sc?.negative_prompt_f].filter(Boolean).join(", ");
  const tokens = rawNeg.split(",").map((s) => s.trim()).filter(Boolean);
  if (badge) {
    if (mode === "override") {
      badge.textContent = `Overriding Global (${tokens.length} tokens)`;
      badge.style.background = "rgba(239, 68, 68, 0.25)";
      badge.style.color = "#fca5a5";
    } else if (tokens.length > 0) {
      badge.textContent = `+${tokens.length} Scene Negatives Active`;
      badge.style.background = "rgba(245, 158, 11, 0.2)";
      badge.style.color = "#fcd34d";
    } else {
      badge.textContent = "Inheriting Global";
      badge.style.background = "#1e293b";
      badge.style.color = "#94a3b8";
    }
  }
  const lowerSet = new Set(String(sc?.negative_prompt || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean));
  document.querySelectorAll(".scene-neg-chip").forEach((btn) => {
    const chipToks = String(btn.dataset.neg || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
    const allPresent = chipToks.length > 0 && chipToks.every((t) => lowerSet.has(t));
    btn.style.borderColor = allPresent ? "#ef4444" : "";
    btn.style.color = allPresent ? "#fca5a5" : "";
  });
}

function syncSceneModularAndNegativeUI(sc) {
  if (!sc) return;
  const el = (id) => document.getElementById(id);
  const isTwo = isTwoPassScene(sc);
  const isMod = sc.prompt_mode === "components";

  const btnRaw = el("btnPromptModeRaw");
  const btnComp = el("btnPromptModeComponents");
  if (btnRaw && btnComp) {
    btnRaw.style.background = !isMod ? "#2563eb" : "transparent";
    btnRaw.style.color = !isMod ? "#fff" : "#94a3b8";
    btnComp.style.background = isMod ? "#2563eb" : "transparent";
    btnComp.style.color = isMod ? "#fff" : "#94a3b8";
  }

  if (el("singlePromptGroup")) el("singlePromptGroup").style.display = (!isTwo && !isMod) ? "block" : "none";
  if (el("duetRawPromptsGrid")) el("duetRawPromptsGrid").style.display = (isTwo && !isMod) ? "grid" : "none";
  if (el("modularPromptsGroup")) el("modularPromptsGroup").style.display = isMod ? "block" : "none";

  if (isMod) {
    hydrateSceneComponentsFromRawIfNeeded(sc);
    if (el("duetLockCameraWrap")) el("duetLockCameraWrap").style.display = isTwo ? "inline-flex" : "none";
    if (el("chkDuetLockCamera")) el("chkDuetLockCamera").checked = sc.duet_lock_camera !== false;
    if (el("compPerformanceSingleWrap")) el("compPerformanceSingleWrap").style.display = isTwo ? "none" : "block";
    if (el("compPerformanceDuetWrap")) el("compPerformanceDuetWrap").style.display = isTwo ? "grid" : "none";
    if (el("compSoundscapeSingleWrap")) el("compSoundscapeSingleWrap").style.display = isTwo ? "none" : "block";
    if (el("compSoundscapeDuetWrap")) el("compSoundscapeDuetWrap").style.display = isTwo ? "grid" : "none";

    const setIfIdle = (id, val) => {
      const node = el(id);
      if (node && document.activeElement !== node) node.value = val || "";
    };
    setIfIdle("compCamera", sc.prompt_camera);
    setIfIdle("compAtmosphere", sc.prompt_atmosphere);
    setIfIdle("compPerformance", sc.prompt_performance);
    setIfIdle("compPerformanceM", sc.prompt_performance_m || sc.prompt_performance);
    setIfIdle("compPerformanceF", sc.prompt_performance_f);
    setIfIdle("compMusicians", sc.prompt_musicians);
    setIfIdle("compSoundscape", sc.prompt_soundscape || "Studio room tone under the vocal performance.");
    setIfIdle("compSoundscapeM", sc.prompt_soundscape_m || sc.prompt_soundscape || "Studio room tone and male vocal.");
    setIfIdle("compSoundscapeF", sc.prompt_soundscape_f || "Quiet studio room tone, no speech.");
    syncCanonicalPromptsFromComponents(sc);
  }

  // Populate Scene Negative Prompt UI
  const setNegIfIdle = (id, val) => {
    const node = el(id);
    if (node && document.activeElement !== node) node.value = val || "";
  };
  setNegIfIdle("sceneNegativePrompt", sc.negative_prompt);
  setNegIfIdle("sceneNegativePromptM", sc.negative_prompt_m);
  setNegIfIdle("sceneNegativePromptF", sc.negative_prompt_f);
  if (el("sceneNegativeMode")) el("sceneNegativeMode").value = sc.negative_prompt_mode || "inherit";
  if (el("sceneNegativeDuetRow")) el("sceneNegativeDuetRow").style.display = isTwo ? "grid" : "none";
  updateSceneNegativeBadgeAndChips(sc);
}
window.syncSceneModularAndNegativeUI = syncSceneModularAndNegativeUI;

function initModularAndNegativePromptEvents() {
  const getActiveSc = () => (state.selectedSceneIdx !== null ? state.projectData?.scenes?.[state.selectedSceneIdx] : null);

  document.getElementById("btnPromptModeRaw")?.addEventListener("click", () => {
    const sc = getActiveSc();
    if (!sc || sc.prompt_mode !== "components") return;
    syncCanonicalPromptsFromComponents(sc);
    sc.prompt_mode = "raw";
    syncSceneModularAndNegativeUI(sc);
    resizeAllPromptTextareas();
    markProjectDirty();
    window.recordHistorySnapshot?.(`Switch ${sc.id} to Raw Prompt Mode`);
  });

  document.getElementById("btnPromptModeComponents")?.addEventListener("click", () => {
    const sc = getActiveSc();
    if (!sc || sc.prompt_mode === "components") return;
    hydrateSceneComponentsFromRawIfNeeded(sc);
    sc.prompt_mode = "components";
    syncSceneModularAndNegativeUI(sc);
    markProjectDirty();
    window.recordHistorySnapshot?.(`Switch ${sc.id} to Modular Prompt Mode`);
  });

  const compMap = {
    compCamera: "prompt_camera",
    compAtmosphere: "prompt_atmosphere",
    compPerformance: "prompt_performance",
    compPerformanceM: "prompt_performance_m",
    compPerformanceF: "prompt_performance_f",
    compMusicians: "prompt_musicians",
    compSoundscape: "prompt_soundscape",
    compSoundscapeM: "prompt_soundscape_m",
    compSoundscapeF: "prompt_soundscape_f",
  };

  Object.entries(compMap).forEach(([id, prop]) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.addEventListener("input", (e) => {
      const sc = getActiveSc();
      if (!sc) return;
      sc[prop] = e.target.value;
      syncCanonicalPromptsFromComponents(sc);
      if (window.updatePromptReferenceTray) window.updatePromptReferenceTray(sc);
      markProjectDirty();
    });
  });

  document.getElementById("chkDuetLockCamera")?.addEventListener("change", (e) => {
    const sc = getActiveSc();
    if (!sc) return;
    sc.duet_lock_camera = Boolean(e.target.checked);
    syncCanonicalPromptsFromComponents(sc);
    markProjectDirty();
  });

  document.querySelectorAll(".comp-cam-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sc = getActiveSc();
      if (!sc) return;
      sc.prompt_camera = btn.dataset.cam || "";
      const camEl = document.getElementById("compCamera");
      if (camEl) camEl.value = sc.prompt_camera;
      syncCanonicalPromptsFromComponents(sc);
      markProjectDirty();
      window.recordHistorySnapshot?.(`Set ${sc.id} Camera Preset`);
    });
  });

  document.querySelectorAll(".comp-snd-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sc = getActiveSc();
      if (!sc) return;
      sc.prompt_soundscape = btn.dataset.snd || "";
      const sndEl = document.getElementById("compSoundscape");
      if (sndEl) sndEl.value = sc.prompt_soundscape;
      syncCanonicalPromptsFromComponents(sc);
      markProjectDirty();
    });
  });

  // Negative Prompt Controls
  const negMap = {
    sceneNegativePrompt: "negative_prompt",
    sceneNegativePromptM: "negative_prompt_m",
    sceneNegativePromptF: "negative_prompt_f",
  };
  Object.entries(negMap).forEach(([id, prop]) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.addEventListener("input", (e) => {
      const sc = getActiveSc();
      if (!sc) return;
      const val = e.target.value;
      if (val && val.trim()) sc[prop] = val;
      else delete sc[prop];
      updateSceneNegativeBadgeAndChips(sc);
      markProjectDirty();
    });
  });

  document.getElementById("sceneNegativeMode")?.addEventListener("change", (e) => {
    const sc = getActiveSc();
    if (!sc) return;
    sc.negative_prompt_mode = e.target.value || "inherit";
    updateSceneNegativeBadgeAndChips(sc);
    markProjectDirty();
    window.recordHistorySnapshot?.(`Change ${sc.id} Negative Prompt Mode`);
  });

  document.querySelectorAll(".scene-neg-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sc = getActiveSc();
      if (!sc) return;
      const chipTokens = String(btn.dataset.neg || "").split(",").map((s) => s.trim()).filter(Boolean);
      const curTokens = String(sc.negative_prompt || "").split(",").map((s) => s.trim()).filter(Boolean);
      const curLower = new Set(curTokens.map((t) => t.toLowerCase()));
      const allPresent = chipTokens.every((t) => curLower.has(t.toLowerCase()));
      let nextTokens;
      if (allPresent) {
        const removeSet = new Set(chipTokens.map((t) => t.toLowerCase()));
        nextTokens = curTokens.filter((t) => !removeSet.has(t.toLowerCase()));
      } else {
        nextTokens = [...curTokens];
        chipTokens.forEach((t) => {
          if (!curLower.has(t.toLowerCase())) {
            curLower.add(t.toLowerCase());
            nextTokens.push(t);
          }
        });
      }
      if (nextTokens.length > 0) sc.negative_prompt = nextTokens.join(", ");
      else delete sc.negative_prompt;
      const negEl = document.getElementById("sceneNegativePrompt");
      if (negEl) negEl.value = sc.negative_prompt || "";
      updateSceneNegativeBadgeAndChips(sc);
      markProjectDirty();
      window.recordHistorySnapshot?.(`Toggle ${sc.id} Negative Preset`);
    });
  });
}
initModularAndNegativePromptEvents();

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
  syncSceneModularAndNegativeUI(sc);
  resizeAllPromptTextareas();
  if (window.updatePromptReferenceTray) {
    window.updatePromptReferenceTray(sc);
  }
}

