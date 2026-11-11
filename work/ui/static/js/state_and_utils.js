/**
 * Music Video Generation Studio - Main Application Logic
 * Pure Vanilla ES6 - Zero external CDN dependencies
 * Features Web Audio API waveform rendering, interactive timeline, and SSE terminal
 */

const state = {
  projects: [],
  currentSlug: null,
  projectData: null,
  assets: {},
  models: {},
  selectedSceneIdx: null,
  selectedAnchorIdx: null,
  selectedMarkerIdx: null,
  audioBuffer: null,
  audioContext: null,
  audioDuration: 0,
  isPlaying: false,
  playheadTime: 0,
  zoom: 1.0,
  pixelsPerSecond: 60,
  eventSource: null,
  isDraggingBoundary: false,
  draggedBoundaryIdx: null,
  hoveredBoundaryIdx: null,
  renderQueue: [],
  queueFilter: "all",
  activeRenderTask: null,
  renderTimerInterval: null,
  renderStartTime: null,
};
window.state = state;

// DOM References
const audioEl = document.getElementById("audioElement");
const canvas = document.getElementById("waveformCanvas");
const ctx = canvas.getContext("2d");
const playhead = document.getElementById("timelinePlayhead");
const scrollContainer = document.getElementById("timelineScrollContainer");
const timecodeDisplay = document.getElementById("timecodeDisplay");

// ============================================================================
// Initialization
// ============================================================================
window.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  setupKeyboardShortcuts();
  setupCanvasEvents();
  initMarkerModalEvents();
  initMediaSyncEvents();
  await loadModelProfiles();
  await loadProjects();
  initLogStream();

  // Manual refresh button
  document.getElementById("btnRefreshProject")?.addEventListener("click", () => {
    refreshProjectAssets(false);
  });

  // Auto-refresh when returning to this browser window (only when not actively playing media)
  window.addEventListener("focus", () => {
    const clipVideo = document.getElementById("clipVideoPlayer");
    const masterVideo = document.getElementById("masterVideoPlayer");
    const isPlaying =
      state.isPlaying ||
      (audioEl && !audioEl.paused) ||
      (clipVideo && !clipVideo.paused) ||
      (masterVideo && !masterVideo.paused);

    if (isPlaying) {
      // Don't interrupt active playback when returning to the window
      return;
    }
    refreshProjectAssets(true);
  });
});

function showToast(msg, duration = 3000) {
  const toast = document.getElementById("toastMessage");
  toast.textContent = msg;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), duration);
}

function formatTime(s) {
  if (isNaN(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  const ms = Math.floor((s % 1) * 100);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}.${String(ms).padStart(2, "0")}`;
}

function formatCompletedAt(ts) {
  if (!ts) return "—";
  try {
    const num = Number(ts);
    if (isNaN(num) || num <= 0) return "—";
    const d = new Date(num < 1e11 ? num * 1000 : num);
    if (isNaN(d.getTime())) return "—";
    const pad = (n) => String(n).padStart(2, "0");
    const yr = d.getFullYear();
    const mo = pad(d.getMonth() + 1);
    const day = pad(d.getDate());
    const hr = pad(d.getHours());
    const min = pad(d.getMinutes());
    const sec = pad(d.getSeconds());
    return `${yr}-${mo}-${day} ${hr}:${min}:${sec}`;
  } catch (e) {
    return "—";
  }
}

function formatElapsed(sec) {
  if (sec === undefined || sec === null || sec === "" || isNaN(Number(sec))) return "—";
  const s = Math.round(Number(sec));
  if (s < 0) return "—";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return `${h}h ${remM}m`;
}

function extractAnchorId(str) {
  if (!str) return "";
  const s = String(str).trim();
  const m = s.match(/^A(\d{1,2})([a-z]?)(?:[ _\-\.]|$)/i);
  if (m) {
    const num = parseInt(m[1], 10);
    const suf = m[2] ? m[2].toLowerCase() : "";
    return `A${String(num).padStart(2, "0")}${suf}`;
  }
  const parts = s.split(/[-_ \.]/);
  if (parts.length > 0) {
    const m2 = parts[0].match(/^A(\d+)([a-z]?)$/i);
    if (m2) {
      const num = parseInt(m2[1], 10);
      const suf = m2[2] ? m2[2].toLowerCase() : "";
      return `A${String(num).padStart(2, "0")}${suf}`;
    }
  }
  return s.toUpperCase();
}

function resolveMarkerPrompt(promptText, sceneStart, markers) {
  if (!promptText || !markers || markers.length === 0) return promptText;
  const markerMap = {};
  markers.forEach((m) => {
    if (m && m.name && m.time !== undefined) {
      markerMap[String(m.name).trim().toLowerCase()] = m;
    }
  });

  const fps = Number(state.projectData?.project?.fps) || 24;

  return promptText.replace(/<([^>]+)>/g, (match, inner, offset, fullStr) => {
    const parts = inner.trim().split(":");
    const tagName = parts[0].trim().toLowerCase();
    const modifier = parts[1] ? parts[1].trim().toLowerCase() : null;

    if (!markerMap[tagName]) return match;

    const marker = markerMap[tagName];
    const relTime = marker.time - sceneStart;
    const relFrame = Math.max(0, Math.round(relTime * fps));

    const preceding = fullStr.substring(Math.max(0, offset - 15), offset);
    const following = fullStr.substring(offset + match.length, offset + match.length + 15);
    const precededByFrame = /\b(frame|frames|f)\s*$/i.test(preceding);
    const followedByFrame = /^\s*(frames?|f\b)/i.test(following);
    const followedBySec = /^\s*(seconds?|s\b)/i.test(following);

    if (modifier === "frame" || modifier === "frames") {
      return precededByFrame || followedByFrame ? String(relFrame) : `frame ${relFrame}`;
    } else if (modifier === "f" || modifier === "raw_frame") {
      return String(relFrame);
    } else if (modifier === "sec" || modifier === "seconds") {
      return `${relTime.toFixed(2)} seconds`;
    } else if (modifier === "s") {
      return `${relTime.toFixed(2)}s`;
    } else if (modifier === "raw" || modifier === "num") {
      return relTime.toFixed(2);
    }

    if (precededByFrame || followedByFrame) {
      return String(relFrame);
    }
    if (followedBySec) {
      return relTime.toFixed(2);
    }
    return `${relTime.toFixed(2)}s`;
  });
}

// ============================================================================
// Navigation Tabs
// ============================================================================
function setupTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const tabName = btn.dataset.tab;
      const main = document.getElementById("appMain");
      // If clicking already open side tab, toggle back to full timeline
      if (tabName !== "timeline" && btn.classList.contains("active") && main?.classList.contains("layout-split")) {
        switchTab("timeline");
        return;
      }
      switchTab(tabName);
    });
  });

  // Close button on side pane returns to full timeline
  document.getElementById("btnSidePaneClose")?.addEventListener("click", () => {
    switchTab("timeline");
  });
}

function setupKeyboardShortcuts() {
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") {
      return;
    }
    if (e.code === "Space") {
      e.preventDefault();
      togglePlayback();
    } else if (e.code === "KeyM") {
      e.preventDefault();
      addMarkerAtPlayhead();
    } else if (e.code === "Delete" || e.code === "Backspace") {
      if (state.selectedSceneIdx !== null) {
        e.preventDefault();
        deleteScene(state.selectedSceneIdx);
      }
    } else if (e.code === "KeyS" && !e.ctrlKey && !e.metaKey) {
      if (state.selectedSceneIdx !== null && typeof splitScene === "function") {
        e.preventDefault();
        splitScene(state.selectedSceneIdx);
      }
    }
  });
}

// ============================================================================
// API Calls: Projects & Models
// ============================================================================
async function loadModelProfiles() {
  try {
    const res = await fetch("/api/models");
    state.models = await res.json();
    populateModelSelectors();
  } catch (err) {
    console.error("Failed to load models:", err);
  }
}

async function loadProjects() {
  try {
    const res = await fetch("/api/projects");
    const json = await res.json();
    state.projects = json.projects || [];

    const select = document.getElementById("projectSelect");
    select.innerHTML = "";
    state.projects.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = `${p.slug}:::${p.config_file}`;
      opt.textContent = p.label || `${p.name} (${p.config_file})`;
      select.appendChild(opt);
    });

    if (state.projects.length > 0) {
      const urlParams = new URLSearchParams(window.location.search);
      const requestedSlug = urlParams.get("project");
      let preferred = null;
      if (requestedSlug) {
        preferred = state.projects.find((p) => p.slug === requestedSlug || p.name.toLowerCase().includes(requestedSlug.toLowerCase()));
      }
      if (!preferred) {
        preferred = state.projects.find((p) => p.config_file && p.config_file.includes("v4")) || state.projects[0];
      }
      select.value = `${preferred.slug}:::${preferred.config_file}`;
      await selectProject(preferred.slug, preferred.config_file);
    }

    select.addEventListener("change", (e) => {
      const [slug, cfg] = e.target.value.split(":::");
      selectProject(slug, cfg);
    });
  } catch (err) {
    console.error("Failed to load projects:", err);
  }
}

async function selectProject(slug, configFile = null) {
  state.currentSlug = slug;
  state.currentConfigFile = configFile;
  const q = configFile ? `?config=${encodeURIComponent(configFile)}` : "";
  try {
    const res = await fetch(`/api/project/${slug}${q}`);
    const json = await res.json();
    state.projectData = json.data;
    state.assets = json.assets;
    state.currentConfigFile = json.config_file;

    if (typeof window.initProjectHistory === "function") {
      window.initProjectHistory();
    }

    updateProjectSettingsUI();
    updateAnchorStudioUI();
    updateInspectorAnchorsDropdown();
    updateInspectorStemsDropdown();

    // Initialize audio duration from metadata immediately so ruler & timecode are populated
    if (state.projectData?.project?.total_duration) {
      state.audioDuration = state.projectData.project.total_duration;
      updatePlayheadVisual(0, false);
      redrawTimeline();
    }

    await loadProjectAudio();
    updateMasterVideoUI();
    loadProjectQueue(slug);
    if (typeof window.loadProjectCharacters === "function") {
      window.loadProjectCharacters(slug);
    }

    showToast(`Loaded: ${state.projectData.project?.name || slug} (${state.currentConfigFile})`);
  } catch (err) {
    console.error(`Failed to load project ${slug}:`, err);
  }
}

async function refreshProjectAssets(silent = false) {
  if (!state.currentSlug) return;
  const q = state.currentConfigFile ? `?config=${encodeURIComponent(state.currentConfigFile)}` : "";
  try {
    const res = await fetch(`/api/project/${state.currentSlug}${q}`);
    const json = await res.json();
    if (!state.isDirty) {
      state.projectData = json.data;
      if (typeof window.initProjectHistory === "function") {
        window.initProjectHistory();
      }
    }
    state.assets = json.assets;

    // Clear canvas image cache so fresh re-rendered thumbnails load
    for (const k in canvasImageCache) {
      delete canvasImageCache[k];
    }

    updateAnchorStudioUI();
    updateInspectorAnchorsDropdown();
    updateInspectorStemsDropdown();
    redrawTimeline();
    if (state.selectedSceneIdx !== null && state.projectData?.scenes) {
      updateSceneClipPreview(state.projectData.scenes[state.selectedSceneIdx]);
    }
    updateMasterVideoUI();
    loadProjectQueue(state.currentSlug);
    if (typeof window.loadProjectCharacters === "function") {
      window.loadProjectCharacters(state.currentSlug);
    }

    if (!silent) {
      showToast("Refreshed project assets and thumbnails from disk");
    }
  } catch (err) {
    console.error("Failed to refresh project assets:", err);
  }
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function markProjectDirty(dirty = true) {
  state.isDirty = dirty;
  const saveBtn = document.getElementById("btnSaveProject");
  const discardBtn = document.getElementById("btnDiscardChanges");
  if (saveBtn) {
    if (dirty) {
      saveBtn.classList.add("btn-primary");
      saveBtn.textContent = "💾 Save Changes *";
      saveBtn.style.boxShadow = "0 0 10px rgba(59, 130, 246, 0.6)";
    } else {
      saveBtn.textContent = "💾 Save Changes";
      saveBtn.style.boxShadow = "none";
    }
  }
  if (discardBtn) {
    discardBtn.style.display = dirty ? "inline-block" : "none";
  }
}

window.addEventListener("beforeunload", (e) => {
  if (state.isDirty && !state._savingProject) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// --- Non-Linear Render Time & Split-Window Estimator ---
function formatDurationEstimate(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) {
    const mins = Math.round((s / 60.0) * 10) / 10;
    return mins >= 20.0 || mins === Math.floor(mins) ? `${Math.round(mins)}m` : `${mins.toFixed(1)}m`;
  }
  let h = Math.floor(s / 3600);
  let m = Math.round((s % 3600) / 60.0);
  if (m === 60) { h += 1; m = 0; }
  return m > 0 ? `${h}h ${String(m).padStart(2, "0")}m` : `${h}h`;
}

function quantizeVideoFrames(minSeconds, framesFormula = "h3", fps = 24.0, maxFrames = null) {
  const formula = String(framesFormula || "h3").trim().toLowerCase();
  const fpsVal = Number(fps) || 24.0;
  let needed = Math.ceil(Math.max(0.0, Number(minSeconds) || 0.0) * fpsVal) + 2;
  let frames, step;
  if (formula === "ltx2") {
    needed = Math.max(17, needed);
    const k = Math.ceil((needed - 1) / 8);
    frames = 8 * k + 1;
    step = 8;
  } else {
    const minF = formula === "h3_ref2va" ? 124 : 107;
    needed = Math.max(minF, needed);
    const k = Math.ceil((needed - 5) / 17);
    frames = 17 * k + 5;
    step = 17;
  }
  if (maxFrames && frames > maxFrames) frames = maxFrames;
  const nextQuantumMaxSec = Number(((frames - 2) / fpsVal).toFixed(2));
  return { frames, nextQuantumMaxSec, step };
}

function estimatePassRenderTime(frames, {
  modelType = "minimax_h3_fl2va_pruned_pdd",
  passNum = null,
  hasEndAnchor = false,
  resolution = "1344x768",
  steps = 8,
  slidingWindowSize = 362,
  slidingWindowOverlap = 18,
  calibrationAlpha = 1.0,
} = {}) {
  const F = Math.max(34, Number(frames) || 141);
  const wSize = Math.max(17, Number(slidingWindowSize) || 362);
  const wOvlp = Math.max(0, Math.min(wSize - 1, Number(slidingWindowOverlap) || 18));
  const hop = Math.max(1, wSize - wOvlp);

  let deltaF = 0;
  if (hasEndAnchor && passNum !== 2) deltaF += 7;
  const isRef2va = String(modelType || "").toLowerCase().includes("ref2va");
  if (isRef2va) {
    deltaF += 14;
    if (passNum === 2) deltaF += 12;
  } else if (passNum === 2) {
    deltaF -= 10;
  }

  let resMult = 1.0;
  try {
    const parts = String(resolution || "1344x768").toLowerCase().split("x");
    if (parts.length === 2) {
      const px = parseInt(parts[0], 10) * parseInt(parts[1], 10);
      if (px > 0) resMult = Math.pow(px / (1344 * 768), 1.15);
    }
  } catch (_) {}

  const stepMult = Math.max(1, Number(steps) || 8) / 8.0;
  const windows = [];
  if (F <= wSize) {
    windows.push(F);
  } else {
    const nWin = 1 + Math.ceil((F - wSize) / hop);
    let rem = F;
    for (let i = 0; i < nWin; i++) {
      windows.push(Math.min(rem, wSize));
      rem -= hop;
      if (rem <= 0) break;
    }
  }

  let vramPagerMult = 1.0;
  let regime = "fast_vram";
  if (F <= 226) {
    vramPagerMult = 1.0;
    regime = "fast_vram";
  } else if (windows.length === 1) {
    const ramp = Math.min(1.0, Math.max(0.0, (F - 226.0) / (345.0 - 226.0)));
    vramPagerMult = 1.0 + 1.40 * ramp;
    regime = "vram_spill";
  } else {
    vramPagerMult = 2.76;
    regime = "split_window";
  }

  let baseSum = 0.0;
  for (const wF of windows) {
    const fEff = Math.max(1.0, Number(wF) + deltaF);
    baseSum += (2.29 * fEff + 0.01224 * fEff * fEff) * vramPagerMult;
  }

  const estSec = Math.round(baseSum * resMult * stepMult * calibrationAlpha * 10) / 10;
  return {
    estimated_seconds: estSec,
    estimated_minutes: Math.round((estSec / 60.0) * 10) / 10,
    estimated_formatted: formatDurationEstimate(estSec),
    quantized_frames: F,
    effective_frames: F + deltaF,
    delta_f: deltaF,
    window_count: windows.length,
    is_split_window: F > 226,
    vram_pager_mult: Number(vramPagerMult.toFixed(3)),
    regime,
    model_type: modelType,
    pass_num: passNum,
    has_end_anchor: Boolean(hasEndAnchor && passNum !== 2),
  };
}

function estimateSceneRenderInfo(sc, _skipSplitCalc = false) {
  if (!sc) return null;
  const vs = state.projectData?.video_settings || {};
  const proj = state.projectData?.project || {};
  const models = state.projectData?.models || {};
  const resMap = { "1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088" };
  const rawRes = String(models.default_video_resolution || models.resolution || models.video_shared_settings?.resolution || proj.master_resolution || vs.target_resolution || "4").trim();
  const resolution = state.models?.video_resolutions?.[rawRes]?.resolution || resMap[rawRes] || (rawRes.toLowerCase().includes("x") ? rawRes.toLowerCase() : "1344x768");

  const editDur = Math.max(0.1, (Number(sc.end) || 0) - (Number(sc.start) || 0));
  const defOverlap = Number(proj.render_overlap_seconds ?? vs.default_head_pad_sec ?? 0);
  let rawHead = null;
  for (const k of ["render_pad_start", "pad_start", "head_overlap", "head_pad"]) {
    if (sc[k] !== undefined && sc[k] !== null && String(sc[k]).trim() !== "") { rawHead = Number(sc[k]); break; }
  }
  if (rawHead === null && sc.render_overlap !== undefined && sc.render_overlap !== null) {
    rawHead = typeof sc.render_overlap === "object" ? Number(sc.render_overlap.head || 0) : Number(sc.render_overlap);
  }
  if (rawHead === null) rawHead = defOverlap;

  let rawTail = null;
  for (const k of ["render_pad_end", "pad_end", "tail_overlap", "tail_pad"]) {
    if (sc[k] !== undefined && sc[k] !== null && String(sc[k]).trim() !== "") { rawTail = Number(sc[k]); break; }
  }
  if (rawTail === null && sc.render_overlap !== undefined && sc.render_overlap !== null) {
    rawTail = typeof sc.render_overlap === "object" ? Number(sc.render_overlap.tail || 0) : Number(sc.render_overlap);
  }
  const hasEndAnchor = Boolean(sc.anchor_end && String(sc.anchor_end).trim());
  if (rawTail === null) rawTail = hasEndAnchor ? 0 : defOverlap;

  const headPad = Math.max(0, Math.min(rawHead || 0, Math.max(0, Number(sc.start) || 0)));
  const tailPad = Math.max(0, rawTail || 0);
  const effectiveSec = Number((editDur + headPad + tailPad).toFixed(3));
  const fps = Number(proj.fps) || 24.0;
  const alpha = Number(state.calibrationAlpha) || 1.0;

  const mode = sc.mode || "solo";
  const isTwoPass = ["duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva"].includes(mode);
  const vModels = state.models?.video_renderers || {};
  const projVModels = models.video_renderers || {};
  const defRid = String(models.default_video_renderer || 1);

  const buildPassEst = (pNum) => {
    const rawR = (pNum === 1 ? sc.renderer_m : pNum === 2 ? sc.renderer_f : sc.renderer) ?? sc.renderer;
    const rId = rawR !== null && rawR !== undefined && !["", "0", "none", "null", "default"].includes(String(rawR).trim().toLowerCase()) ? String(rawR).trim() : defRid;
    const rSpec = { ...(vModels[rId] || {}), ...(projVModels[rId] || {}) };
    const rModel = rSpec.model_type || sc.model_type || vs.model_type || "";
    const isRef2va = ["9", "10", "11", "12", "13"].includes(rId) || String(rModel).toLowerCase().includes("ref2va");
    const isLtx2 = ["14", "15", "16"].includes(rId) || String(rModel).toLowerCase().includes("ltx");
    const formula = rSpec.frames_formula || (isLtx2 ? "ltx2" : isRef2va ? "h3_ref2va" : "h3");
    const qInfo = quantizeVideoFrames(effectiveSec, formula, fps);
    const modelType = rModel || (isRef2va ? "minimax_h3_ref2va_pruned_ptd_refasymopd" : "minimax_h3_fl2va_pruned_pdd");
    const steps = Number(sc.num_inference_steps || rSpec.num_inference_steps || (["5", "6", "7", "8", "13"].includes(rId) ? 20 : 8));
    const wSize = Number(sc.sliding_window_size || rSpec.sliding_window_size || (formula === "ltx2" ? 241 : 362));
    const wOvlp = Number(sc.sliding_window_overlap || rSpec.sliding_window_overlap || (formula === "ltx2" ? 9 : 18));
    const est = estimatePassRenderTime(qInfo.frames, {
      modelType, passNum: pNum, hasEndAnchor: hasEndAnchor && pNum !== 2, resolution, steps,
      slidingWindowSize: wSize, slidingWindowOverlap: wOvlp, calibrationAlpha: alpha,
    });
    est.model_id = rId;
    est.model_name = rSpec.name || `Model #${rId}`;
    est.next_quantum_max_sec = qInfo.nextQuantumMaxSec;
    est.quantum_step = qInfo.step;
    return est;
  };

  const passes = isTwoPass ? [buildPassEst(1), buildPassEst(2)] : [buildPassEst(null)];
  const totalSec = Number(passes.reduce((acc, p) => acc + p.estimated_seconds, 0).toFixed(1));
  const p0 = passes[0];

  let splitSavingsSec = 0;
  if (!_skipSplitCalc && p0.is_split_window && editDur >= 2.0) {
    const midS = Number(((Number(sc.start || 0) + Number(sc.end || 0)) / 2.0).toFixed(2));
    const scA = { ...sc, start: Number(sc.start || 0), end: midS };
    delete scA.anchor_end; delete scA.render_pad_end;
    const scB = { ...sc, start: midS, end: Number(sc.end || 0) };
    delete scB.render_pad_start;
    const estA = estimateSceneRenderInfo(scA, true);
    const estB = estimateSceneRenderInfo(scB, true);
    if (estA && estB) {
      splitSavingsSec = Math.max(0, Number((totalSec - (estA.total_estimated_seconds + estB.total_estimated_seconds)).toFixed(1)));
    }
  }

  return {
    scene_id: sc.id || "",
    mode,
    is_two_pass: isTwoPass,
    resolution,
    model_id: p0.model_id,
    model_name: p0.model_name,
    edit_duration: Number(editDur.toFixed(2)),
    head_pad: Number(headPad.toFixed(2)),
    tail_pad: Number(tailPad.toFixed(2)),
    effective_seconds: effectiveSec,
    quantized_frames: p0.quantized_frames,
    next_quantum_max_sec: p0.next_quantum_max_sec,
    quantum_headroom_sec: Number(Math.max(0, p0.next_quantum_max_sec - effectiveSec).toFixed(2)),
    quantum_step: p0.quantum_step,
    window_count: p0.window_count,
    is_split_window: p0.is_split_window,
    regime: p0.regime,
    vram_pager_mult: p0.vram_pager_mult,
    split_savings_seconds: splitSavingsSec,
    split_savings_formatted: splitSavingsSec > 0 ? formatDurationEstimate(splitSavingsSec) : "",
    passes,
    total_estimated_seconds: totalSec,
    total_estimated_minutes: Number((totalSec / 60.0).toFixed(1)),
    total_estimated_formatted: formatDurationEstimate(totalSec),
  };
}
window.formatDurationEstimate = formatDurationEstimate;
window.quantizeVideoFrames = quantizeVideoFrames;
window.estimatePassRenderTime = estimatePassRenderTime;
window.estimateSceneRenderInfo = estimateSceneRenderInfo;


