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
  zoom: 0.35,
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

function extractAnchorId(str) {
  if (!str) return "";
  const s = String(str).trim();
  const m = s.match(/^(A\d{1,2})([ _\-\.]|$)/i);
  if (m) {
    const num = parseInt(m[1].slice(1), 10);
    return `A${String(num).padStart(2, "0")}`;
  }
  const parts = s.split(/[-_ \.]/);
  if (parts.length > 0 && /^A\d+$/i.test(parts[0])) {
    const num = parseInt(parts[0].slice(1), 10);
    return `A${String(num).padStart(2, "0")}`;
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

let dirtyQueueSyncTimer = null;
function markProjectDirty(dirty = true) {
  state.isDirty = dirty;
  const saveBtn = document.getElementById("btnSaveProject");
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
  if (dirty) {
    const consolePane = document.getElementById("tab-console");
    if (consolePane && consolePane.classList.contains("active")) {
      if (dirtyQueueSyncTimer) clearTimeout(dirtyQueueSyncTimer);
      dirtyQueueSyncTimer = setTimeout(() => {
        if (state.isDirty) saveProjectData(false);
      }, 500);
    }
  }
}

