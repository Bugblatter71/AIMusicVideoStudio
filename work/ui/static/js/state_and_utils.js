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
  queueFilter: "queued",
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

  // Clicking active scene badge returns to timeline inspector
  document.getElementById("activeSceneBadge")?.addEventListener("click", () => {
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

const LAST_PROJECT_STORAGE_KEY = "mvg_last_project_v1";

function _saveLastOpenedProject(slug, configFile = null) {
  try { if (slug && typeof localStorage !== "undefined") localStorage.setItem(LAST_PROJECT_STORAGE_KEY, JSON.stringify({ slug, config_file: configFile || "" })); } catch (e) {}
  try {
    if (slug && window.location && window.history?.replaceState) {
      const u = new URL(window.location.href);
      if (u.searchParams.get("project") !== slug) { u.searchParams.set("project", slug); window.history.replaceState(null, "", u.toString()); }
    }
  } catch (e) {}
}

function _findPreferredProject(projects, requestedSlug) {
  if (!Array.isArray(projects) || !projects.length) return null;
  if (requestedSlug) {
    const low = String(requestedSlug).toLowerCase();
    return projects.find((p) => p.slug === requestedSlug) || projects.find((p) => p.name?.toLowerCase().includes(low)) || null;
  }
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(LAST_PROJECT_STORAGE_KEY) : null;
    const saved = raw ? JSON.parse(raw) : null;
    if (saved?.slug) return projects.find((p) => p.slug === saved.slug && (!saved.config_file || p.config_file === saved.config_file)) || projects.find((p) => p.slug === saved.slug) || null;
  } catch (e) {}
  return projects.find((p) => p.config_file && p.config_file.includes("v4")) || projects[0];
}

async function loadProjects() {
  try {
    const res = await fetch("/api/projects"), json = await res.json();
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
      const preferred = _findPreferredProject(state.projects, new URLSearchParams(window.location.search).get("project")) || state.projects[0];
      select.value = `${preferred.slug}:::${preferred.config_file}`;
      await selectProject(preferred.slug, preferred.config_file);
    }
    select.addEventListener("change", (e) => { const [slug, cfg] = e.target.value.split(":::"); selectProject(slug, cfg); });
  } catch (err) { console.error("Failed to load projects:", err); }
}

async function selectProject(slug, configFile = null) {
  const loadSeq = (state._projectLoadSeq = (state._projectLoadSeq || 0) + 1);
  Object.assign(state, { currentSlug: slug, currentConfigFile: configFile, renderQueue: [], doneQueue: [], _doneQueueSlug: slug });
  state.sseTaskStatuses?.clear?.();
  window.renderQueueUI?.();
  _saveLastOpenedProject(slug, configFile);
  const selEl = document.getElementById("projectSelect");
  if (selEl && configFile) selEl.value = `${slug}:::${configFile}`;
  const q = configFile ? `?config=${encodeURIComponent(configFile)}` : "";
  try {
    const res = await fetch(`/api/project/${slug}${q}`), json = await res.json();
    if (state.currentSlug !== slug || state._projectLoadSeq !== loadSeq) return;
    state.projectData = json.data; state.assets = json.assets; state.currentConfigFile = json.config_file;
    _saveLastOpenedProject(slug, json.config_file);
    if (selEl && json.config_file) selEl.value = `${slug}:::${json.config_file}`;
    window.initProjectHistory?.();
    updateProjectSettingsUI(); updateAnchorStudioUI(); updateInspectorAnchorsDropdown(); updateInspectorStemsDropdown();
    if (state.projectData?.project?.total_duration) {
      state.audioDuration = state.projectData.project.total_duration;
      updatePlayheadVisual(0, false); redrawTimeline();
    }
    await loadProjectAudio();
    if (state.currentSlug !== slug || state._projectLoadSeq !== loadSeq) return;
    updateMasterVideoUI(); loadProjectQueue(slug); window.loadProjectCharacters?.(slug);
    window.dispatchEvent(new CustomEvent("projectLoaded", { detail: { slug } }));
    state._projectLoadedSeq = loadSeq;
    showToast(`Loaded: ${state.projectData.project?.name || slug} (${state.currentConfigFile})`);
  } catch (err) { console.error(`Failed to load project ${slug}:`, err); }
}

async function refreshProjectAssets(silent = false) {
  if (!state.currentSlug) return;
  const q = state.currentConfigFile ? `?config=${encodeURIComponent(state.currentConfigFile)}` : "";
  try {
    const res = await fetch(`/api/project/${state.currentSlug}${q}`), json = await res.json();
    if (!state.isDirty) { state.projectData = json.data; window.initProjectHistory?.(); }
    state.assets = json.assets;
    for (const k in canvasImageCache) delete canvasImageCache[k];
    updateAnchorStudioUI(); updateInspectorAnchorsDropdown(); updateInspectorStemsDropdown(); redrawTimeline();
    window.invalidateSceneTakesCache?.();
    const selSc = state.selectedSceneIdx !== null ? state.projectData?.scenes?.[state.selectedSceneIdx] : null;
    if (selSc) { updateSceneClipPreview(selSc); window.updateSceneTakesUI?.(selSc); }
    updateMasterVideoUI(); loadProjectQueue(state.currentSlug);
    if (!state.isDirty) window.loadProjectCharacters?.(state.currentSlug);
    if (document.getElementById("tab-scenes")?.classList?.contains("active")) window.renderSceneList?.();
    if (!silent) showToast("Refreshed project assets and thumbnails from disk");
  } catch (err) { console.error("Failed to refresh project assets:", err); }
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function markProjectDirty(dirty = true) {
  state.isDirty = dirty;
  const saveBtn = document.getElementById("btnSaveProject"), discardBtn = document.getElementById("btnDiscardChanges");
  if (saveBtn) {
    if (dirty) saveBtn.classList.add("btn-primary");
    saveBtn.textContent = dirty ? "💾 Save Changes *" : "💾 Save Changes";
    saveBtn.style.boxShadow = dirty ? "0 0 10px rgba(59, 130, 246, 0.6)" : "none";
  }
  if (discardBtn) discardBtn.style.display = dirty ? "inline-block" : "none";
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
  } else if (formula === "wan") {
    needed = Math.max(17, needed);
    const k = Math.ceil((needed - 1) / 4);
    frames = 4 * k + 1;
    step = 4;
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
      if (px > 0) {
        resMult = Math.pow(px / (1344 * 768), 1.15);
        if (px > 1400000) resMult *= 1.0 + 1.40 * Math.min(1.5, (px - 1400000) / 688960);
      }
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

  let vramPagerMult = 1.0, regime = "fast_vram";
  if (F <= 226) { vramPagerMult = 1.0; regime = "fast_vram"; }
  else if (windows.length === 1) { vramPagerMult = 1.0 + 1.40 * Math.min(1.0, Math.max(0.0, (F - 226.0) / 119.0)); regime = "vram_spill"; }
  else { vramPagerMult = 2.76; regime = "split_window"; }
  let baseSum = 0.0;
  for (const wF of windows) { const fEff = Math.max(1.0, Number(wF) + deltaF); baseSum += (2.29 * fEff + 0.01224 * fEff * fEff) * vramPagerMult; }
  const estSec = Math.round(baseSum * resMult * stepMult * calibrationAlpha * 10) / 10;
  return {
    estimated_seconds: estSec, estimated_minutes: Math.round((estSec / 60.0) * 10) / 10,
    estimated_formatted: formatDurationEstimate(estSec), quantized_frames: F, effective_frames: F + deltaF,
    delta_f: deltaF, window_count: windows.length, is_split_window: F > 226,
    vram_pager_mult: Number(vramPagerMult.toFixed(3)), regime, model_type: modelType,
    pass_num: passNum, has_end_anchor: Boolean(hasEndAnchor && passNum !== 2),
  };
}

function estimateSceneRenderInfo(sc, _skipSplitCalc = false) {
  if (!sc) return null;
  const vs = state.projectData?.video_settings || {};
  const proj = state.projectData?.project || {};
  const models = state.projectData?.models || {};
  const resMap = { "1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088" };
  const resolveRenderRes = (rawVal) => {
    if (rawVal === null || rawVal === undefined) return "";
    const s = String(rawVal).trim();
    if (!s || ["default", "inherit", "none", "null"].includes(s.toLowerCase())) return "";
    const mEntry = state.models?.video_resolutions?.[s] || state.models?.resolutions?.[s];
    if (mEntry) {
      if (mEntry.resolution) return String(mEntry.resolution).toLowerCase();
      if (mEntry.width && mEntry.height) return `${mEntry.width}x${mEntry.height}`;
    }
    if (resMap[s]) return resMap[s];
    if (s.toLowerCase().includes("x")) return s.toLowerCase();
    return "";
  };
  const projDefRes = resolveRenderRes(models.default_video_resolution)
    || resolveRenderRes(models.resolution)
    || resolveRenderRes(models.video_shared_settings?.resolution)
    || resolveRenderRes(vs.target_resolution)
    || "1344x768";
  const sceneResOverride = resolveRenderRes(sc.resolution);
  let resolution = sceneResOverride || projDefRes;

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
  const vModels = state.models?.video_renderers || {}, fModels = state.models?.fal_video_renderers || {}, mmModels = state.models?.minimax_video_renderers || {}, projVModels = models.video_renderers || {}, defRid = String(models.default_video_renderer || 1);
  const defBk = String(state.falStatus?.default_execution_backend || models.default_backend || "local").toLowerCase();
  const resolvePassRoiRes = (pNum, baseRes, bk) => {
    const r = String(sc.pass2_roi_mode || "full").trim().toLowerCase();
    if (pNum !== 2 || bk !== "local" || !baseRes.includes("x") || ["full", "full_frame", "none", "off", ""].includes(r)) return baseRes;
    const [w, h] = baseRes.split("x").map((v) => parseInt(v, 10) || 0);
    if (!w || !h) return baseRes;
    const sx = Math.max(0.15, Math.min(0.85, Number(sc.split_x ?? 0.5))), side = String(sc.pass2_roi_side || "right").toLowerCase();
    const snap = (v, m) => Math.max(Math.min((m >> 5) << 5, 256), Math.min((m >> 5) << 5, Math.round(v / 32) * 32));
    if (r === "tight_performer" || r === "tight") { const hw = side === "left" ? w * sx : w * (1 - sx); return `${snap(Math.max(hw * 0.9, w * 0.45), w)}x${snap(h * 0.72, h)}`; }
    const cw = snap(side === "left" ? w * sx : w * (1 - sx), w), ch = (h >> 4) << 4;
    return `${cw}x${ch}`;
  };
  const buildPassEst = (pNum) => {
    const rawR = (pNum === 1 ? sc.renderer_m : pNum === 2 ? sc.renderer_f : sc.renderer) ?? sc.renderer;
    const rId = rawR !== null && rawR !== undefined && !["", "0", "none", "null", "default"].includes(String(rawR).trim().toLowerCase()) ? String(rawR).trim() : defRid;
    const rSpec = { ...(vModels[rId] || fModels[rId] || mmModels[rId] || {}), ...(projVModels[rId] || {}) };
    const rModel = rSpec.model_type || sc.model_type || vs.model_type || "";
    const isLtx2 = ["14", "15", "16", "17", "18", "19", "20", "21"].includes(rId) || String(rModel).toLowerCase().includes("ltx");
    const isRef2va = !isLtx2 && (["9", "10", "11", "12", "13"].includes(rId) || String(rModel).toLowerCase().includes("ref2va"));
    const formula = rSpec.frames_formula || (isLtx2 ? "ltx2" : isRef2va ? "h3_ref2va" : "h3");
    const qInfo = quantizeVideoFrames(effectiveSec, formula, Number(rSpec.force_fps) || fps);
    const modelType = rModel || (isRef2va ? "minimax_h3_ref2va_pruned_ptd_refasymopd" : "minimax_h3_fl2va_pruned_pdd");
    const steps = Number(sc.num_inference_steps || rSpec.num_inference_steps || (["5", "6", "7", "8", "13"].includes(rId) ? 20 : 8));
    const wSize = Number(sc.sliding_window_size || rSpec.sliding_window_size || (formula === "ltx2" ? 241 : 362));
    const wOvlp = Number(sc.sliding_window_overlap || rSpec.sliding_window_overlap || (formula === "ltx2" ? 9 : 18));
    const baseRes = sceneResOverride || resolveRenderRes(rSpec.resolution) || projDefRes;
    const cleanBk = (b) => (b && !["auto", "default", "null", "none"].includes(String(b).toLowerCase()) ? String(b).toLowerCase() : "");
    const rawPassBk = pNum === 1 ? sc.backend_m : (pNum === 2 ? sc.backend_f : null);
    const isDedicatedCloud = Boolean((fModels[rId] || mmModels[rId]) && !vModels[rId] && rSpec.backend);
    const passBk = isDedicatedCloud ? String(rSpec.backend).toLowerCase() : (cleanBk(rawPassBk) || cleanBk(sc.backend) || cleanBk(rSpec.backend) || defBk);
    const effRes = resolvePassRoiRes(pNum, baseRes, passBk);
    const passAlpha = Math.max(0.4, Math.min(4.0, Number(state.calibrationAlphaByRes?.[effRes] ?? (passBk === "local" ? alpha : 1.0)) || 1.0));
    const est = estimatePassRenderTime(qInfo.frames, { modelType, passNum: pNum, hasEndAnchor: hasEndAnchor && pNum !== 2, resolution: effRes, steps, slidingWindowSize: wSize, slidingWindowOverlap: wOvlp, calibrationAlpha: passAlpha });
    if (passBk === "fal" || passBk === "minimax") {
      const chunks = effectiveSec > 15 ? Math.max(1, Math.floor((effectiveSec + 14.99) / 15)) : 1;
      const baseS = (baseRes.includes("1088") || baseRes.includes("1080")) ? 285 : (baseRes.includes("768") ? 120 : 75);
      est.estimated_seconds = Number((chunks * Math.max(45, baseS * Math.max(0.8, (effectiveSec / chunks) / 7.5))).toFixed(1));
      est.estimated_minutes = Math.round((est.estimated_seconds / 60) * 10) / 10; est.estimated_formatted = formatDurationEstimate(est.estimated_seconds);
    }
    est.resolution = effRes; est.model_id = rId; est.model_name = rSpec.name || `Model #${rId}`;
    est.next_quantum_max_sec = qInfo.nextQuantumMaxSec; est.quantum_step = qInfo.step;
    return est;
  };

  const passes = isTwoPass ? [buildPassEst(1), buildPassEst(2)] : [buildPassEst(null)];
  const totalSec = Number(passes.reduce((acc, p) => acc + p.estimated_seconds, 0).toFixed(1));
  const p0 = passes[0];
  resolution = p0.resolution || resolution;

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

(function wireNewProjectButton() {
  const btnNew = document.getElementById("btnNewProject");
  if (!btnNew || btnNew.dataset.wired) return;
  btnNew.dataset.wired = "1";
  btnNew.addEventListener("click", async () => {
    const rawName = prompt("Enter slug or name for the new project (e.g. my_song):");
    if (!rawName || !rawName.trim()) return;
    try {
      const res = await fetch("/api/projects/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: rawName.trim() }),
      });
      const json = await res.json();
      if (!json.success) {
        alert(`Failed to create project: ${json.error || "Unknown error"}`);
        return;
      }
      await loadProjects();
      const sel = document.getElementById("projectSelect");
      if (sel && json.slug) {
        const matchOpt = Array.from(sel.options).find((o) => o.value.startsWith(`${json.slug}:::`));
        if (matchOpt) {
          sel.value = matchOpt.value;
          const [sSlug, sCfg] = matchOpt.value.split(":::");
          await selectProject(sSlug, sCfg);
        }
      }
      showToast(`Created project: ${json.slug}`);
    } catch (err) {
      alert(`Error creating project: ${err.message}`);
    }
  });
})();

let _splitModalCtx = null;
function openSplitSceneModal(sc, start, end, defT, onConfirm) {
  let modal = document.getElementById("splitSceneModal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "splitSceneModal";
    modal.style.cssText = "display:none;position:fixed;inset:0;background:rgba(0,0,0,0.68);z-index:9999;align-items:center;justify-content:center;backdrop-filter:blur(2px);";
    modal.innerHTML = `<div style="background:#1e293b;border:1px solid #475569;border-radius:8px;width:410px;max-width:92vw;padding:18px;box-shadow:0 12px 32px rgba(0,0,0,0.65);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h4 id="splitSceneModalTitle" style="margin:0;color:#38bdf8;font-size:15px;font-weight:700;"></h4>
        <button type="button" id="btnSplitModalClose" style="background:transparent;border:none;color:#94a3b8;font-size:20px;cursor:pointer;line-height:1;">&times;</button>
      </div>
      <div id="splitSceneRange" style="font-size:12px;color:#cbd5e1;margin-bottom:10px;"></div>
      <div class="form-group" style="margin-bottom:10px;">
        <label for="splitTimeInput">Split Timestamp (seconds)</label>
        <div style="display:flex;gap:6px;align-items:center;">
          <input type="number" id="splitTimeInput" step="0.01" style="flex:1;">
          <button type="button" class="btn btn-sm" id="btnSplitUsePlayhead" style="padding:4px 8px;font-size:11px;">⏱ Playhead</button>
          <button type="button" class="btn btn-sm" id="btnSplitUseMidpoint" style="padding:4px 8px;font-size:11px;">➗ Mid</button>
        </div>
      </div>
      <div id="splitPreviewText" style="font-size:11px;color:#94a3b8;background:#0f172a;border:1px solid #334155;border-radius:5px;padding:7px 9px;margin-bottom:14px;"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button type="button" class="btn" id="btnCancelSplitScene">Cancel</button>
        <button type="button" class="btn btn-primary" id="btnConfirmSplitScene">✂️ Split Scene</button>
      </div>
    </div>`;
    document.body.appendChild(modal);
    const close = () => { modal.style.display = "none"; _splitModalCtx = null; };
    const confirm = () => {
      if (!_splitModalCtx) return;
      const t = parseFloat(parseFloat(document.getElementById("splitTimeInput")?.value).toFixed(3));
      const { start: s, end: e, onConfirm: cb } = _splitModalCtx;
      if (isNaN(t) || t <= s + 0.05 || t >= e - 0.05) { showToast(`Invalid split time. Must be strictly between ${(s + 0.05).toFixed(2)}s and ${(e - 0.05).toFixed(2)}s.`); return; }
      close(); cb(t);
    };
    modal.addEventListener("click", (ev) => { if (ev.target === modal) close(); });
    document.getElementById("btnSplitModalClose").onclick = close;
    document.getElementById("btnCancelSplitScene").onclick = close;
    document.getElementById("btnConfirmSplitScene").onclick = confirm;
    const setSplitVal = (v) => { const inp = document.getElementById("splitTimeInput"); inp.value = v.toFixed(2); inp.dispatchEvent(new Event("input")); };
    document.getElementById("btnSplitUseMidpoint").onclick = () => { if (_splitModalCtx) setSplitVal((_splitModalCtx.start + _splitModalCtx.end) / 2.0); };
    document.getElementById("btnSplitUsePlayhead").onclick = () => { if (_splitModalCtx && _splitModalCtx.playT !== null) setSplitVal(_splitModalCtx.playT); };
    const inp = document.getElementById("splitTimeInput");
    inp.addEventListener("input", () => {
      if (!_splitModalCtx) return;
      const prev = document.getElementById("splitPreviewText"), t = parseFloat(inp.value), { start: s, end: e } = _splitModalCtx;
      prev.textContent = (isNaN(t) || t <= s + 0.05 || t >= e - 0.05)
        ? `Enter a timestamp between ${(s + 0.05).toFixed(2)}s and ${(e - 0.05).toFixed(2)}s.`
        : `Part 1: ${s.toFixed(2)}s – ${t.toFixed(2)}s (${(t - s).toFixed(2)}s)  |  Part 2: ${t.toFixed(2)}s – ${e.toFixed(2)}s (${(e - t).toFixed(2)}s)`;
    });
    inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); confirm(); } else if (ev.key === "Escape") { ev.preventDefault(); close(); } });
  }
  const playT = (typeof state.playheadTime === "number" && state.playheadTime > start + 0.05 && state.playheadTime < end - 0.05)
    ? state.playheadTime : (audioEl && audioEl.currentTime > start + 0.05 && audioEl.currentTime < end - 0.05) ? audioEl.currentTime : null;
  _splitModalCtx = { start, end, playT, onConfirm };
  document.getElementById("splitSceneModalTitle").textContent = `✂️ Split Scene "${sc.id}"`;
  document.getElementById("splitSceneRange").textContent = `Range: ${start.toFixed(2)}s – ${end.toFixed(2)}s (Duration: ${(end - start).toFixed(2)}s)`;
  const phBtn = document.getElementById("btnSplitUsePlayhead");
  if (phBtn) phBtn.style.display = playT !== null ? "inline-flex" : "none";
  const inp = document.getElementById("splitTimeInput");
  inp.min = (start + 0.06).toFixed(2); inp.max = (end - 0.06).toFixed(2); inp.value = defT.toFixed(2);
  inp.dispatchEvent(new Event("input")); modal.style.display = "flex";
  setTimeout(() => { inp.focus(); inp.select(); }, 0);
}
window.openSplitSceneModal = openSplitSceneModal;

function syncMasterLoopUI() {
  const badge = document.getElementById("masterLoopBadge"), txt = document.getElementById("masterLoopText");
  const loopSc = state.masterLoopSceneId ? (state.projectData?.scenes || []).find((s) => s.id === state.masterLoopSceneId) : null;
  if (badge && txt) { txt.textContent = loopSc ? `🔁 Looping ${loopSc.id} (${Number(loopSc.start || 0).toFixed(2)}s–${Number(loopSc.end || 0).toFixed(2)}s)` : ""; badge.style.display = loopSc ? "inline-flex" : "none"; }
  document.querySelectorAll?.(".scene-card[data-scene-id]").forEach((card) => {
    const isL = Boolean(loopSc && card.dataset.sceneId === loopSc.id), btn = card.querySelector("[data-action='repeat']");
    if (btn) { btn.textContent = isL ? "🔁 Looping" : "🔁 Repeat"; btn.style.cssText = isL ? "color:#7dd3fc;border-color:rgba(56,189,248,0.65);background:rgba(14,165,233,0.22);padding:2px 7px;font-size:10px;font-weight:700;" : "padding:2px 7px;font-size:10px;color:#cbd5e1;"; }
  });
  window.redrawTimeline?.(); window.syncSceneJumpBarHighlight?.();
}
function clearMasterSceneLoop() { state.masterLoopSceneId = null; syncMasterLoopUI(); }
function enforceMasterSceneLoop(mv) {
  if (!mv || !state.masterLoopSceneId) return false;
  const sc = (state.projectData?.scenes || []).find((s) => s.id === state.masterLoopSceneId);
  if (!sc) { clearMasterSceneLoop(); return false; }
  const startT = Math.max(0, Number(sc.start || 0)), rawEnd = Math.max(startT, Number(sc.end || 0));
  const endT = (mv.duration && isFinite(mv.duration)) ? Math.min(mv.duration, rawEnd) : rawEnd;
  if (endT > startT + 0.05 && (!mv.paused || mv.ended) && (mv.currentTime >= endT - 0.03 || mv.ended)) {
    try { mv.currentTime = startT; } catch (e) {}
    if (mv.ended) mv.play?.().catch?.(() => {});
    return true;
  }
  return false;
}
function focusSceneInTimeline(idx) {
  const sc = state.projectData?.scenes?.[idx];
  if (!sc) return;
  const tStart = Math.max(0, Number(sc.start || 0)), tEnd = Math.max(tStart, Number(sc.end || 0));
  const seekT = tEnd > tStart + 0.002 ? Math.min(tEnd - 0.001, tStart + 0.001) : tStart;
  if (typeof window.selectScene === "function") window.selectScene(idx); else state.selectedSceneIdx = idx;
  if (typeof seekTimeline === "function") seekTimeline(seekT, null, false);
  if (typeof scrollContainer !== "undefined" && scrollContainer) {
    const pps = (state.pixelsPerSecond || 60) * (state.zoom || 1), cw = scrollContainer.clientWidth || 600;
    scrollContainer.scrollLeft = Math.max(0, tStart * pps - Math.min(80, Math.round(cw * 0.12)));
  }
  window.redrawTimeline?.();
}
function toggleSceneMasterLoop(idx) {
  const sc = state.projectData?.scenes?.[idx];
  if (!sc) return;
  if (state.masterLoopSceneId === sc.id) { clearMasterSceneLoop(); return; }
  state.masterLoopSceneId = sc.id; focusSceneInTimeline(idx);
  const startT = Math.max(0, Number(sc.start || 0)), clipVid = document.getElementById("clipVideoPlayer");
  if (typeof audioEl !== "undefined" && audioEl && !audioEl.paused) audioEl.pause();
  if (clipVid && !clipVid.paused) clipVid.pause();
  state._masterPrevTime = startT; state._masterWasPlaying = true;
  if (!document.getElementById("tab-master")?.classList.contains("active")) window.switchTab?.("master");
  const mv = document.getElementById("masterVideoPlayer");
  if (mv && mv.getAttribute("src")) {
    const go = () => { try { mv.currentTime = startT; } catch (e) {} mv.play?.().catch?.(() => {}); };
    if (mv.readyState >= 1) go(); else mv.addEventListener?.("loadedmetadata", go, { once: true });
  } else { showToast?.(`🔁 Loop set to ${sc.id} (${startT.toFixed(2)}s–${Number(sc.end || 0).toFixed(2)}s)`); }
  syncMasterLoopUI();
}
function unloadClipVideoPlayer() {
  const vp = typeof document !== "undefined" ? document.getElementById("clipVideoPlayer") : null;
  if (!vp) return false;
  try { vp.pause?.(); } catch (e) {}
  vp.removeAttribute?.("src");
  vp.removeAttribute?.("data-loaded-src");
  try { vp.load?.(); } catch (e) {}
  return true;
}
function resolveSceneModelType(scObj, passNum = null, takeObj = null) {
  const rm = state.projectData?.models || {};
  const vrMap = Object.assign({}, state.models?.video_renderers || {}, state.models?.fal_video_renderers || {}, state.models?.minimax_video_renderers || {}, rm.video_renderers || {});
  const p = passNum ?? takeObj?.pass_num ?? null;
  const tModel = String(takeObj?.model_type || takeObj?.render_settings?.model_type || "").trim();
  const raw = (p === 1 ? scObj?.renderer_m : (p === 2 ? scObj?.renderer_f : null)) ?? scObj?.renderer ?? scObj?.video_renderer;
  const rKey = (raw !== undefined && raw !== null && !["", "0", "none", "null", "default", "inherit"].includes(String(raw).trim().toLowerCase())) ? String(raw).trim() : "";
  if (tModel && !rKey) return tModel;
  const effId = rKey || String(rm.default_video_renderer || rm.video_renderer || 1);
  return String(vrMap[effId]?.model_type || tModel || effId || "").trim();
}
document.getElementById("btnClearMasterLoop")?.addEventListener("click", clearMasterSceneLoop);
window.addEventListener?.("projectLoaded", clearMasterSceneLoop);
Object.assign(window, { syncMasterLoopUI, clearMasterSceneLoop, enforceMasterSceneLoop, focusSceneInTimeline, toggleSceneMasterLoop, unloadClipVideoPlayer, resolveSceneModelType });


