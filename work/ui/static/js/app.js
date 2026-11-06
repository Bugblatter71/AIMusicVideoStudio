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
    state.projectData = json.data;
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

    if (!silent) {
      showToast("Refreshed project assets and thumbnails from disk");
    }
  } catch (err) {
    console.error("Failed to refresh project assets:", err);
  }
}

// ============================================================================
// Audio & Waveform Engine (Web Audio API + Canvas)
// ============================================================================
async function loadProjectAudio() {
  const pcfg = state.projectData.project || {};
  let audioFile = pcfg.master_audio || "";
  if (audioFile.startsWith("audio/")) audioFile = audioFile.substring(6);

  if (!audioFile) {
    audioFile = (state.assets.audio_files || [])[0] || "";
  }

  if (!audioFile) {
    state.audioBuffer = null;
    state.audioDuration = pcfg.total_duration || 60;
    redrawTimeline();
    return;
  }

  const audioUrl = `/media/${state.currentSlug}/audio/${encodeURIComponent(audioFile)}`;
  audioEl.src = audioUrl;

  try {
    if (!state.audioContext) {
      state.audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    const res = await fetch(audioUrl);
    const arrayBuffer = await res.arrayBuffer();
    state.audioBuffer = await state.audioContext.decodeAudioData(arrayBuffer);
    state.audioDuration = state.audioBuffer.duration;
  } catch (e) {
    console.warn("Could not decode audio waveform via Web Audio API, using metadata duration:", e);
    state.audioDuration = pcfg.total_duration || 60;
  }

  redrawTimeline();
}

let isScrubbingRuler = false;

function togglePlayback() {
  if (state.isPlaying) {
    audioEl.pause();
    const clipVideo = document.getElementById("clipVideoPlayer");
    if (clipVideo && !clipVideo.paused) clipVideo.pause();
    const masterVideo = document.getElementById("masterVideoPlayer");
    if (masterVideo && !masterVideo.paused) masterVideo.pause();
  } else {
    audioEl.play().catch((e) => console.warn("Audio play prevented:", e));
  }
}

document.getElementById("btnPlayPause").addEventListener("click", togglePlayback);
document.getElementById("btnStop").addEventListener("click", () => {
  audioEl.pause();
  audioEl.currentTime = 0;
  const clipVideo = document.getElementById("clipVideoPlayer");
  if (clipVideo) {
    clipVideo.pause();
    clipVideo.currentTime = 0;
  }
  const masterVideo = document.getElementById("masterVideoPlayer");
  if (masterVideo) {
    masterVideo.pause();
    masterVideo.currentTime = 0;
  }
  state.isPlaying = false;
  document.getElementById("btnPlayPause").textContent = "▶ Play";
  seekTimeline(0, null, true);
});

function seekTimeline(timeSec, sourceElement = null, triggerAutoScroll = false) {
  const dur = Math.max(state.audioDuration || 60, 1);
  const clampedTime = Math.max(0, Math.min(timeSec, dur));

  // 1. Audio sync
  if (sourceElement !== audioEl) {
    if (Math.abs(audioEl.currentTime - clampedTime) > 0.05) {
      audioEl.currentTime = clampedTime;
    }
  }

  // 2. Master video sync
  const masterVideo = document.getElementById("masterVideoPlayer");
  if (masterVideo && sourceElement !== masterVideo && masterVideo.src && !isNaN(masterVideo.duration)) {
    if (Math.abs(masterVideo.currentTime - clampedTime) > 0.08) {
      masterVideo.currentTime = clampedTime;
    }
  }

  // 3. Scene selection and clip video sync
  const scenes = state.projectData?.scenes || [];
  const sceneIdx = scenes.findIndex((s) => clampedTime >= s.start && clampedTime < s.end);
  const clipVideo = document.getElementById("clipVideoPlayer");

  if (sceneIdx !== -1) {
    const sc = scenes[sceneIdx];
    if (state.selectedSceneIdx !== sceneIdx) {
      selectScene(sceneIdx);
    }
    if (clipVideo && sourceElement !== clipVideo && clipVideo.src && !isNaN(clipVideo.duration)) {
      const relTime = clampedTime - sc.start;
      if (relTime >= 0 && relTime <= (clipVideo.duration || (sc.end - sc.start))) {
        if (Math.abs(clipVideo.currentTime - relTime) > 0.08) {
          clipVideo.currentTime = relTime;
        }
      }
    }
  }

  // 4. Update visual playhead
  updatePlayheadVisual(clampedTime, triggerAutoScroll);
}

function updatePlayheadVisual(timeSec, autoScroll = false) {
  if (isNaN(timeSec) || timeSec < 0) timeSec = 0;
  state.playheadTime = timeSec;

  const pps = state.pixelsPerSecond * state.zoom;
  const x = timeSec * pps;
  playhead.style.left = `${x}px`;

  timecodeDisplay.textContent = `${formatTime(timeSec)} / ${formatTime(state.audioDuration)}`;

  // Find active scene
  const scenes = state.projectData?.scenes || [];
  const activeScene = scenes.find((s) => timeSec >= s.start && timeSec < s.end);
  const badge = document.getElementById("activeSceneBadge");
  if (activeScene) {
    badge.textContent = `Scene: ${activeScene.id} (${activeScene.mode || "solo"})`;
  } else {
    badge.textContent = "";
  }

  // Auto-scroll timeline to keep playhead within view
  if (autoScroll && scrollContainer) {
    const sl = scrollContainer.scrollLeft;
    const cw = scrollContainer.clientWidth;
    if (x > sl + cw - 40) {
      scrollContainer.scrollLeft = Math.max(0, x - 80);
    } else if (x < sl) {
      scrollContainer.scrollLeft = Math.max(0, x - 80);
    }
  }
}

function updateAudioPlayheadLoop() {
  if (audioEl.paused || audioEl.ended) return;
  const cur = audioEl.currentTime;
  updatePlayheadVisual(cur, true);

  // Sync scene clip preview video frame if inside current scene
  const clipVideo = document.getElementById("clipVideoPlayer");
  if (clipVideo && clipVideo.src && clipVideo.paused && state.selectedSceneIdx !== null) {
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (sc && cur >= sc.start && cur < sc.end) {
      const rel = cur - sc.start;
      if (Math.abs(clipVideo.currentTime - rel) > 0.08) {
        clipVideo.currentTime = rel;
      }
    }
  }

  requestAnimationFrame(updateAudioPlayheadLoop);
}

function updateClipVideoPlayheadLoop() {
  const clipVideo = document.getElementById("clipVideoPlayer");
  if (!clipVideo || clipVideo.paused || clipVideo.ended) return;
  if (state.selectedSceneIdx !== null) {
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (sc) {
      const globalTime = sc.start + clipVideo.currentTime;
      updatePlayheadVisual(globalTime, true);
    }
  }
  requestAnimationFrame(updateClipVideoPlayheadLoop);
}

function updateMasterVideoPlayheadLoop() {
  const masterVideo = document.getElementById("masterVideoPlayer");
  if (!masterVideo || masterVideo.paused || masterVideo.ended) return;
  updatePlayheadVisual(masterVideo.currentTime, true);
  requestAnimationFrame(updateMasterVideoPlayheadLoop);
}

function initMediaSyncEvents() {
  // Audio Element Events
  audioEl.addEventListener("play", () => {
    state.isPlaying = true;
    document.getElementById("btnPlayPause").textContent = "⏸ Pause";
    requestAnimationFrame(updateAudioPlayheadLoop);
  });
  audioEl.addEventListener("pause", () => {
    state.isPlaying = false;
    document.getElementById("btnPlayPause").textContent = "▶ Play";
  });
  audioEl.addEventListener("ended", () => {
    state.isPlaying = false;
    document.getElementById("btnPlayPause").textContent = "▶ Play";
    updatePlayheadVisual(audioEl.duration || 0);
  });
  audioEl.addEventListener("timeupdate", () => {
    if (!isScrubbingRuler && !audioEl.paused) {
      updatePlayheadVisual(audioEl.currentTime, true);
    }
  });

  // Scene Clip Video Player Events
  const clipVideo = document.getElementById("clipVideoPlayer");
  if (clipVideo) {
    clipVideo.addEventListener("play", () => {
      requestAnimationFrame(updateClipVideoPlayheadLoop);
    });
    clipVideo.addEventListener("timeupdate", () => {
      if (!isScrubbingRuler && state.selectedSceneIdx !== null) {
        const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
        if (sc) {
          updatePlayheadVisual(sc.start + clipVideo.currentTime, !clipVideo.paused);
        }
      }
    });
    clipVideo.addEventListener("seeked", () => {
      if (!isScrubbingRuler && state.selectedSceneIdx !== null) {
        const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
        if (sc) {
          updatePlayheadVisual(sc.start + clipVideo.currentTime, false);
        }
      }
    });
  }

  // Master Video Player Events
  const masterVideo = document.getElementById("masterVideoPlayer");
  if (masterVideo) {
    masterVideo.addEventListener("play", () => {
      requestAnimationFrame(updateMasterVideoPlayheadLoop);
    });
    masterVideo.addEventListener("timeupdate", () => {
      if (!isScrubbingRuler) {
        updatePlayheadVisual(masterVideo.currentTime, !masterVideo.paused);
      }
    });
    masterVideo.addEventListener("seeked", () => {
      if (!isScrubbingRuler) {
        updatePlayheadVisual(masterVideo.currentTime, false);
      }
    });
  }
}

// Canvas Image Cache for fast thumbnail/filmstrip rendering
const canvasImageCache = {};
function getCanvasImage(url) {
  if (!url) return null;
  if (canvasImageCache[url]) {
    return canvasImageCache[url];
  }
  const img = new Image();
  img.src = url;
  img.onload = () => {
    redrawTimeline();
  };
  img.onerror = () => {
    img.failed = true;
    redrawTimeline();
  };
  canvasImageCache[url] = img;
  return img;
}

// Waveform Canvas Rendering
function redrawTimeline() {
  const dur = Math.max(state.audioDuration, 1);
  const pps = state.pixelsPerSecond * state.zoom;
  const totalWidth = Math.max(dur * pps, scrollContainer.clientWidth);

  // Dynamically scale track height with zoom to maintain exact image aspect ratio
  const baseTrackHeight = 60;
  const trackHeight = Math.round(baseTrackHeight * state.zoom);
  const trackTop = 84;
  const bottomMargin = 16;
  const totalHeight = trackTop + trackHeight + bottomMargin;

  canvas.width = totalWidth;
  canvas.height = totalHeight;

  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";

  if (playhead) {
    playhead.style.height = `${totalHeight}px`;
  }

  const width = canvas.width;
  const height = canvas.height;

  // Background
  ctx.fillStyle = "#050811";
  ctx.fillRect(0, 0, width, height);

  // Timecode Ruler (Top 22px)
  ctx.fillStyle = "#0f172a";
  ctx.fillRect(0, 0, width, 22);
  ctx.strokeStyle = "#334155";
  ctx.beginPath();
  ctx.moveTo(0, 22);
  ctx.lineTo(width, 22);
  ctx.stroke();

  // Grid tick intervals
  const tickSec = state.zoom > 2 ? 1 : state.zoom > 1 ? 2 : 5;
  ctx.fillStyle = "#94a3b8";
  ctx.font = "10px monospace";

  for (let s = 0; s <= dur; s += tickSec) {
    const x = s * pps;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, 22);
    ctx.stroke();
    ctx.fillText(formatTime(s).split(".")[0], x + 4, 15);
  }

  // Draw Audio Waveform (Height: 24px - 80px)
  const waveTop = 24;
  const waveHeight = 56;
  const waveMid = waveTop + waveHeight / 2;

  if (state.audioBuffer) {
    const rawData = state.audioBuffer.getChannelData(0);
    const step = Math.ceil(rawData.length / width);
    ctx.strokeStyle = "#0284c7";
    ctx.lineWidth = 1;
    ctx.beginPath();

    for (let i = 0; i < width; i++) {
      let min = 1.0;
      let max = -1.0;
      for (let j = 0; j < step; j++) {
        const datum = rawData[i * step + j];
        if (datum < min) min = datum;
        if (datum > max) max = datum;
      }
      ctx.moveTo(i, waveMid + min * (waveHeight / 2));
      ctx.lineTo(i, waveMid + max * (waveHeight / 2));
    }
    ctx.stroke();
  } else {
    ctx.strokeStyle = "rgba(56, 189, 248, 0.2)";
    ctx.beginPath();
    ctx.moveTo(0, waveMid);
    ctx.lineTo(width, waveMid);
    ctx.stroke();
  }

  // Draw Scene Blocks Track (Height dynamically scales with zoom)
  const scenes = state.projectData?.scenes || [];
  const clips = state.assets?.clips || {};
  const strips = state.assets?.strips || {};
  const diskAnchors = state.assets?.anchors || {};

  scenes.forEach((sc, idx) => {
    const sx = sc.start * pps;
    const sw = (sc.end - sc.start) * pps;
    const isSelected = state.selectedSceneIdx === idx;

    // Check if video clip is rendered (matching by ID or strip)
    const hasClip = !!(clips[sc.id] || strips[sc.id] || Object.keys(clips).some((k) => k.startsWith(`${sc.id}_`) || k === `${sc.id}.mp4`));

    // Base block gradient style
    let grad;
    if (sc.mode === "duet_method_b" || sc.mode === "two_pass_gva") {
      grad = ctx.createLinearGradient(sx, trackTop, sx + sw, trackTop + trackHeight);
      grad.addColorStop(0, "#4c1d95");
      grad.addColorStop(1, "#2e1065");
    } else if (sc.mode === "silent_locked") {
      grad = ctx.createLinearGradient(sx, trackTop, sx + sw, trackTop + trackHeight);
      grad.addColorStop(0, "#78350f");
      grad.addColorStop(1, "#451a03");
    } else {
      grad = ctx.createLinearGradient(sx, trackTop, sx + sw, trackTop + trackHeight);
      grad.addColorStop(0, "#0369a1");
      grad.addColorStop(1, "#082f49");
    }

    ctx.fillStyle = grad;
    ctx.fillRect(sx, trackTop, sw, trackHeight);

    // 1. If Video rendered: Show multiple thumbnails from video (filmstrip)
    let stripDrawn = false;
    if (hasClip) {
      const clipInfo = clips[sc.id] || Object.values(clips).find((c) => c.filename && c.filename.startsWith(`${sc.id}_`));
      const stripInfo = strips[sc.id] || strips[sc.id.toUpperCase()] || strips[sc.id.toLowerCase()];
      const mtime = clipInfo?.mtime || stripInfo?.mtime || "";
      const stripUrl = `/media/${state.currentSlug}/strip/${encodeURIComponent(sc.id)}${mtime ? `?t=${mtime}` : ""}`;
      const stripImg = getCanvasImage(stripUrl);
      if (stripImg && !stripImg.failed && stripImg.complete && stripImg.naturalWidth > 0) {
        ctx.save();
        ctx.drawImage(stripImg, sx, trackTop, sw, trackHeight);

        // Frame separator lines across sw for distinct multiple thumbnails
        ctx.strokeStyle = "rgba(0, 0, 0, 0.45)";
        ctx.lineWidth = 1;
        for (let f = 1; f < 5; f++) {
          const fx = sx + (sw * f) / 5;
          ctx.beginPath();
          ctx.moveTo(fx, trackTop);
          ctx.lineTo(fx, trackTop + trackHeight);
          ctx.stroke();
        }
        ctx.restore();
        stripDrawn = true;
      }
    }

    // 2. If strip not drawn (or video not rendered): Show Anchor Image if available
    if (!stripDrawn) {
      const aid = extractAnchorId(sc.anchor);
      const diskInfo = diskAnchors[sc.anchor] || (aid ? diskAnchors[aid] : null);
      if (diskInfo) {
        const anchorMtime = diskInfo?.mtime || "";
        const anchorUrl = `/media/${state.currentSlug}/anchor/${encodeURIComponent(diskInfo.filename || sc.anchor)}${anchorMtime ? `?t=${anchorMtime}` : ""}`;
        const anchorImg = getCanvasImage(anchorUrl);
        if (anchorImg && !anchorImg.failed && anchorImg.complete && anchorImg.naturalWidth > 0) {
          ctx.save();
          const thumbW = Math.min(sw, trackHeight * (16 / 9));
          ctx.drawImage(anchorImg, sx, trackTop, thumbW, trackHeight);
          ctx.restore();
        }
      }
    }

    // Top header banner across the block for text readability
    ctx.fillStyle = "rgba(15, 23, 42, 0.78)";
    ctx.fillRect(sx, trackTop, sw, 18);

    // Scene ID & timing
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 11px sans-serif";
    ctx.fillText(`${sc.id} [${(sc.end - sc.start).toFixed(1)}s]`, sx + 6, trackTop + 13);

    // Status badge (Top right of block)
    const aid = extractAnchorId(sc.anchor);
    if (hasClip) {
      if (sw > 90) {
        ctx.fillStyle = "rgba(16, 185, 129, 0.95)";
        if (ctx.roundRect) {
          ctx.beginPath();
          ctx.roundRect(sx + sw - 48, trackTop + 2, 44, 14, 2);
          ctx.fill();
        } else {
          ctx.fillRect(sx + sw - 48, trackTop + 2, 44, 14);
        }
        ctx.fillStyle = "#ffffff";
        ctx.font = "bold 8.5px sans-serif";
        ctx.fillText("▶ VIDEO", sx + sw - 44, trackTop + 12);
      }
    } else if (aid) {
      if (sw > 90) {
        ctx.fillStyle = "rgba(56, 189, 248, 0.85)";
        if (ctx.roundRect) {
          ctx.beginPath();
          ctx.roundRect(sx + sw - 46, trackTop + 2, 42, 14, 2);
          ctx.fill();
        } else {
          ctx.fillRect(sx + sw - 46, trackTop + 2, 42, 14);
        }
        ctx.fillStyle = "#000000";
        ctx.font = "bold 8.5px sans-serif";
        ctx.fillText(`⚓ ${aid}`, sx + sw - 42, trackTop + 12);
      }
    }

    // Outline Border
    ctx.strokeStyle = isSelected ? "#38bdf8" : (hasClip ? "rgba(16, 185, 129, 0.55)" : "rgba(255, 255, 255, 0.25)");
    ctx.lineWidth = isSelected ? 2.5 : 1;
    ctx.strokeRect(sx, trackTop, sw, trackHeight);
  });

  // Draw Draggable Scene Boundaries with interactive resize handles
  scenes.forEach((sc, idx) => {
    const bx = sc.end * pps;
    const isHovered = state.hoveredBoundaryIdx === idx || state.draggedBoundaryIdx === idx;

    ctx.save();
    if (isHovered) {
      // Highlighted boundary line
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(bx, trackTop);
      ctx.lineTo(bx, trackTop + trackHeight);
      ctx.stroke();

      // Center pill handle with resize grip icon
      const handleW = 10;
      const handleH = Math.min(26, trackHeight * 0.45);
      const handleX = bx - handleW / 2;
      const handleY = trackTop + (trackHeight - handleH) / 2;
      ctx.fillStyle = "#38bdf8";
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(handleX, handleY, handleW, handleH, 3);
        ctx.fill();
      } else {
        ctx.fillRect(handleX, handleY, handleW, handleH);
      }

      ctx.strokeStyle = "#0f172a";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(bx - 1.5, handleY + 5);
      ctx.lineTo(bx - 1.5, handleY + handleH - 5);
      ctx.moveTo(bx + 1.5, handleY + 5);
      ctx.lineTo(bx + 1.5, handleY + handleH - 5);
      ctx.stroke();
    } else {
      // Subtle vertical boundary seam
      ctx.strokeStyle = "rgba(255, 255, 255, 0.4)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(bx, trackTop);
      ctx.lineTo(bx, trackTop + trackHeight);
      ctx.stroke();
    }
    ctx.restore();
  });

  // Draw Timeline Markers (Dashed vertical guidelines & Flags on ruler)
  const markers = state.projectData?.markers || [];
  markers.forEach((m, idx) => {
    const mx = m.time * pps;
    const color = m.color || "#f59e0b";

    // 1. Vertical guideline down through waveform and scenes
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(mx, 22);
    ctx.lineTo(mx, canvas.height);
    ctx.stroke();
    ctx.restore();

    // 2. Marker flag on ruler (top bar)
    ctx.save();
    const label = `🚩 ${m.name || "Cue"}`;
    ctx.font = "bold 9px sans-serif";
    const textWidth = ctx.measureText(label).width;
    const flagW = textWidth + 8;
    const flagH = 16;
    const flagX = mx;
    const flagY = 3;

    // Flag background pill
    ctx.fillStyle = color;
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(flagX, flagY, flagW, flagH, 3);
      ctx.fill();
    } else {
      ctx.fillRect(flagX, flagY, flagW, flagH);
    }

    // Flag text
    ctx.fillStyle = "#0f172a";
    ctx.fillText(label, flagX + 4, flagY + 11);
    ctx.restore();
  });
}

function findHoveredBoundary(clickX, clickY) {
  const pps = state.pixelsPerSecond * state.zoom;
  const trackTop = 84;
  const trackHeight = Math.round(60 * state.zoom);
  if (clickY < trackTop || clickY > trackTop + trackHeight) return null;

  const scenes = state.projectData?.scenes || [];
  for (let i = 0; i < scenes.length; i++) {
    const bx = scenes[i].end * pps;
    if (Math.abs(clickX - bx) <= 8) {
      return i;
    }
  }
  return null;
}

function setupCanvasEvents() {
  document.getElementById("timelineZoom").addEventListener("input", (e) => {
    state.zoom = parseFloat(e.target.value);
    redrawTimeline();
    updatePlayheadVisual(state.playheadTime || audioEl.currentTime);
  });

  // Top ruler & timeline positioning / scrubbing / boundary dragging
  canvas.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return; // Left click only
    const rect = canvas.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const clickY = e.clientY - rect.top;
    const pps = state.pixelsPerSecond * state.zoom;
    const clickTime = Math.max(0, Math.min(clickX / pps, state.audioDuration || 60));

    // 0. Check if clicked on a draggable scene boundary
    const boundaryIdx = findHoveredBoundary(clickX, clickY);
    if (boundaryIdx !== null) {
      state.isDraggingBoundary = true;
      state.draggedBoundaryIdx = boundaryIdx;
      selectScene(boundaryIdx);
      redrawTimeline();
      return;
    }

    // 1. Check if clicked in marker ruler bar (Y <= 24px)
    if (clickY <= 24) {
      const markers = state.projectData?.markers || [];
      for (let i = 0; i < markers.length; i++) {
        const mx = markers[i].time * pps;
        const label = `🚩 ${markers[i].name || "Cue"}`;
        ctx.font = "bold 9px sans-serif";
        const flagW = ctx.measureText(label).width + 8;
        // Hit test exact marker flag rectangle
        if (clickX >= mx && clickX <= mx + flagW && clickY >= 2 && clickY <= 20) {
          openMarkerModal(i);
          return;
        }
      }

      // Clicked on empty ruler space: seek immediately and start scrubbing!
      isScrubbingRuler = true;
      seekTimeline(clickTime, null, true);
      return;
    }

    // 2. Check if clicked inside a scene block (Y dynamically scales with zoom)
    const curTrackBottom = 84 + Math.round(60 * (state.zoom || 1));
    if (clickY >= 84 && clickY <= curTrackBottom) {
      const scenes = state.projectData?.scenes || [];
      const foundIdx = scenes.findIndex((sc) => clickTime >= sc.start && clickTime < sc.end);
      if (foundIdx !== -1) {
        selectScene(foundIdx);
      }
      seekTimeline(clickTime, null, true);
      return;
    }

    // 3. Clicked in waveform area (Y: 25px to 83px)
    seekTimeline(clickTime, null, true);
  });

  // Smooth scrubbing & boundary dragging while mouse moves across window
  window.addEventListener("mousemove", (e) => {
    if (state.isDraggingBoundary && state.draggedBoundaryIdx !== null) {
      const rect = canvas.getBoundingClientRect();
      const pps = state.pixelsPerSecond * state.zoom;
      const mouseTime = (e.clientX - rect.left) / pps;
      updateSceneEnd(state.draggedBoundaryIdx, mouseTime);
      redrawTimeline();
      updatePlayheadVisual(state.playheadTime || audioEl.currentTime);
      if (state.selectedSceneIdx !== null) {
        const sc = state.projectData.scenes[state.selectedSceneIdx];
        updateSceneMarkersBar(sc);
        updatePromptResolvedPreviews(sc);
      }
      return;
    }

    if (!isScrubbingRuler) return;
    const rect = canvas.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const pps = state.pixelsPerSecond * state.zoom;
    const scrubTime = Math.max(0, Math.min(clickX / pps, state.audioDuration || 60));
    seekTimeline(scrubTime, null, true);
  });

  window.addEventListener("mouseup", () => {
    if (state.isDraggingBoundary) {
      state.isDraggingBoundary = false;
      state.draggedBoundaryIdx = null;
      redrawTimeline();
    }
    if (isScrubbingRuler) {
      isScrubbingRuler = false;
    }
  });

  // Dynamic hover cursor over timeline canvas
  canvas.addEventListener("mousemove", (e) => {
    if (state.isDraggingBoundary) {
      canvas.style.cursor = "col-resize";
      return;
    }
    if (isScrubbingRuler) {
      canvas.style.cursor = "ew-resize";
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    // Check boundary hover first
    const bIdx = findHoveredBoundary(clickX, my);
    if (bIdx !== null) {
      canvas.style.cursor = "col-resize";
      if (state.hoveredBoundaryIdx !== bIdx) {
        state.hoveredBoundaryIdx = bIdx;
        redrawTimeline();
      }
      return;
    } else if (state.hoveredBoundaryIdx !== null) {
      state.hoveredBoundaryIdx = null;
      redrawTimeline();
    }

    const curTrackBottom = 84 + Math.round(60 * (state.zoom || 1));
    if (my <= 24) {
      canvas.style.cursor = "pointer";
    } else if (my >= 84 && my <= curTrackBottom) {
      canvas.style.cursor = "pointer";
    } else {
      canvas.style.cursor = "crosshair";
    }
  });

  // Double-click on ruler adds marker at click time
  canvas.addEventListener("dblclick", (e) => {
    const rect = canvas.getBoundingClientRect();
    const clickY = e.clientY - rect.top;
    if (clickY <= 26) {
      const clickX = e.clientX - rect.left;
      const pps = state.pixelsPerSecond * state.zoom;
      const clickTime = Math.max(0, clickX / pps);
      addMarkerAtTime(clickTime);
    }
  });

  // Add Marker button in transport bar
  document.getElementById("btnAddMarker")?.addEventListener("click", () => {
    addMarkerAtPlayhead();
  });

  window.addEventListener("resize", () => {
    redrawTimeline();
    updatePlayheadVisual(state.playheadTime || audioEl.currentTime);
  });
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
  if (!saveBtn) return;
  if (dirty) {
    saveBtn.classList.add("btn-primary");
    saveBtn.textContent = "💾 Save Changes *";
    saveBtn.style.boxShadow = "0 0 10px rgba(59, 130, 246, 0.6)";
  } else {
    saveBtn.textContent = "💾 Save Changes";
    saveBtn.style.boxShadow = "none";
  }
}

// ============================================================================
// Scene Inspector
// ============================================================================
function isTwoPassMode(mode) {
  const twoPassModes = ["duet_method_b", "two_pass_gva", "gva_male_left_silent_right"];
  return twoPassModes.includes(mode);
}

function isTwoPassScene(sc) {
  if (!sc) return false;
  if (isTwoPassMode(sc.mode)) return true;
  if (!sc.mode && (sc.prompt_m !== undefined || sc.prompt_f !== undefined)) return true;
  return false;
}

function getDefaultSinglePrompt(mode) {
  if (mode === "female_solo") {
    return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. {FEMALE_STUDIO_DESC} leans into the microphone with open eyes and natural posture, singing with expressive, synchronized lip movements.\noverall_soundscape: Studio room tone under the vocal performance.\nnon_diegetic_music: N/A";
  }
  if (mode === "silent_locked") {
    return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Atmospheric continuous shot across the studio floor. The performers and instruments remain in place with subtle natural breathing and room movement, with no singing or dialogue.\noverall_soundscape: Quiet studio room tone and ambient atmosphere.\nnon_diegetic_music: N/A";
  }
  return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. {MALE_STUDIO_DESC} leans into the brass microphone with open eyes and natural sway, singing an emotional phrase with synchronized lip movements.\noverall_soundscape: Studio room tone under the vocal performance.\nnon_diegetic_music: N/A";
}

function getDefaultPass1Prompt(mode) {
  return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A seamless, continuous wide shot across the studio floor. On the left side inside his circle of light, {MALE_STUDIO_DESC} at the brass microphone sings with open eyes, natural body movement, and synchronized lip movements, while the partner on the right listens quietly.\noverall_soundscape: Studio room tone and male vocal.\nnon_diegetic_music: N/A";
}

function getDefaultPass2Prompt(mode) {
  if (mode === "gva_male_left_silent_right") {
    return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A seamless, continuous wide shot across the studio floor. On the right side, {FEMALE_STUDIO_DESC} keeps her mouth completely closed and listens quietly, reacting with subtle eye and head movements.\noverall_soundscape: Quiet studio room tone, no speech.\nnon_diegetic_music: N/A";
  }
  return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A seamless, continuous wide shot across the studio floor. On the right side, {FEMALE_STUDIO_DESC} sings her vocal harmony part with open eyes and expressive, synchronized lip movements.\noverall_soundscape: Studio room tone and vocal performance.\nnon_diegetic_music: N/A";
}

function setSelectValueSafely(selectEl, value, defaultValue) {
  if (!selectEl) return;
  const targetVal = value || defaultValue;
  if (!targetVal) return;
  const exists = Array.from(selectEl.options).some(
    (opt) => opt.value === targetVal || opt.value.toLowerCase() === targetVal.toLowerCase()
  );
  if (!exists) {
    const opt = document.createElement("option");
    opt.value = targetVal;
    opt.textContent = `${targetVal} (custom)`;
    selectEl.appendChild(opt);
  }
  selectEl.value = targetVal;
}

function updateSceneInspectorInputs(sc) {
  const isTwoPass = isTwoPassScene(sc);
  const singlePromptGroup = document.getElementById("singlePromptGroup");
  const duetPromptsGroup = document.getElementById("duetPromptsGroup");
  const singleSeedGroup = document.getElementById("singleSeedGroup");
  const singleStemGroup = document.getElementById("singleStemGroup");

  if (singlePromptGroup) singlePromptGroup.style.display = isTwoPass ? "none" : "block";
  if (duetPromptsGroup) duetPromptsGroup.style.display = isTwoPass ? "block" : "none";
  if (singleSeedGroup) singleSeedGroup.style.display = isTwoPass ? "none" : "block";
  if (singleStemGroup) singleStemGroup.style.display = isTwoPass ? "none" : "block";

  if (isTwoPass) {
    const promptMEl = document.getElementById("scenePromptM");
    const promptFEl = document.getElementById("scenePromptF");
    const seedMEl = document.getElementById("sceneSeedM");
    const seedFEl = document.getElementById("sceneSeedF");
    const splitXEl = document.getElementById("sceneSplitX");
    const stemMEl = document.getElementById("sceneStemM");
    const stemFEl = document.getElementById("sceneStemF");

    if (promptMEl) promptMEl.value = sc.prompt_m || "";
    if (promptFEl) promptFEl.value = sc.prompt_f || "";
    if (seedMEl) seedMEl.value = sc.seed_m !== undefined ? sc.seed_m : 8201;
    if (seedFEl) seedFEl.value = sc.seed_f !== undefined ? sc.seed_f : 8301;
    if (splitXEl) splitXEl.value = sc.split_x !== undefined ? sc.split_x : 0.48;
    setSelectValueSafely(stemMEl, sc.stem_m, "male");
    setSelectValueSafely(stemFEl, sc.stem_f, sc.mode === "gva_male_left_silent_right" ? "silent" : "female");
  } else {
    const promptEl = document.getElementById("scenePrompt");
    const seedEl = document.getElementById("sceneSeed");
    const stemEl = document.getElementById("sceneStem");

    if (promptEl) promptEl.value = sc.prompt || "";
    if (seedEl) seedEl.value = sc.seed !== undefined ? sc.seed : 8201;
    setSelectValueSafely(stemEl, sc.stem, sc.mode === "silent_locked" ? "silent" : sc.mode === "female_solo" ? "female" : "male");
  }

  resizeAllPromptTextareas();
  updatePromptResolvedPreviews(sc);
}

function handleSceneModeChange(sc, newMode) {
  const oldMode = sc.mode;
  sc.mode = newMode;

  const wasTwoPass = isTwoPassMode(oldMode);
  const isTwoPass = isTwoPassMode(newMode);

  // 1. Single-Pass -> Two-Pass (e.g. male_solo -> gva_male_left_silent_right or duet_method_b)
  if (!wasTwoPass && isTwoPass) {
    if (!sc.prompt_m || !sc.prompt_m.trim()) {
      sc.prompt_m = sc.prompt || getDefaultPass1Prompt(newMode);
    }
    if (!sc.prompt_f || !sc.prompt_f.trim()) {
      sc.prompt_f = getDefaultPass2Prompt(newMode);
    }
    if (sc.seed_m === undefined) sc.seed_m = sc.seed !== undefined ? sc.seed : 8201;
    if (sc.seed_f === undefined) sc.seed_f = sc.seed !== undefined ? sc.seed + 100 : 8301;
    if (sc.split_x === undefined) sc.split_x = 0.48;

    sc.stem_m = "male";
    sc.stem_f = (newMode === "gva_male_left_silent_right") ? "silent" : "female";

    delete sc.prompt;
    delete sc.seed;
    delete sc.stem;
  }
  // 2. Two-Pass -> Single-Pass (e.g. gva_male_left_silent_right -> male_solo or silent_locked)
  else if (wasTwoPass && !isTwoPass) {
    if (!sc.prompt || !sc.prompt.trim()) {
      if (newMode === "female_solo") {
        sc.prompt = sc.prompt_f || sc.prompt_m || getDefaultSinglePrompt(newMode);
      } else if (newMode === "silent_locked" && oldMode === "gva_male_left_silent_right") {
        sc.prompt = sc.prompt_f || sc.prompt_m || getDefaultSinglePrompt(newMode);
      } else {
        sc.prompt = sc.prompt_m || sc.prompt_f || getDefaultSinglePrompt(newMode);
      }
    }
    if (sc.seed === undefined) {
      sc.seed = (newMode === "female_solo" ? sc.seed_f : sc.seed_m) || 8201;
    }
    if (newMode === "male_solo") sc.stem = "male";
    else if (newMode === "female_solo") sc.stem = "female";
    else if (newMode === "silent_locked") sc.stem = "silent";
    else if (newMode === "solo") sc.stem = "lead_vocal";
    else sc.stem = "silent";

    delete sc.prompt_m;
    delete sc.prompt_f;
    delete sc.seed_m;
    delete sc.seed_f;
    delete sc.split_x;
    delete sc.stem_m;
    delete sc.stem_f;
  }
  // 3. Single-Pass -> Single-Pass (e.g. male_solo -> silent_locked or female_solo)
  else if (!wasTwoPass && !isTwoPass) {
    if (newMode === "silent_locked") {
      sc.stem = "silent";
    } else if (newMode === "male_solo") {
      if (sc.stem === "silent" || sc.stem === "female") sc.stem = "male";
    } else if (newMode === "female_solo") {
      if (sc.stem === "silent" || sc.stem === "male") sc.stem = "female";
    } else if (newMode === "solo") {
      if (sc.stem === "silent") sc.stem = "lead_vocal";
    }
    if (!sc.prompt || !sc.prompt.trim()) {
      sc.prompt = getDefaultSinglePrompt(newMode);
    }
  }
  // 4. Two-Pass -> Two-Pass (e.g. gva_male_left_silent_right <-> duet_method_b)
  else if (wasTwoPass && isTwoPass) {
    if (newMode === "duet_method_b") {
      sc.stem_f = "female";
      if (sc.prompt_f && sc.prompt_f.includes("keeps her mouth completely closed")) {
        sc.prompt_f = sc.prompt_f.replace(
          "keeps her mouth completely closed and listens quietly",
          "sings her harmony vocal part with synchronized lip movements"
        ).replace(
          "keeps her mouth completely closed",
          "sings with synchronized lip movements"
        );
      }
    } else if (newMode === "gva_male_left_silent_right") {
      sc.stem_f = "silent";
      if (sc.prompt_f && (sc.prompt_f.includes("sings with synchronized") || sc.prompt_f.includes("sings her harmony"))) {
        sc.prompt_f = sc.prompt_f.replace(
          /sings (her harmony vocal part with |)synchronized lip movements/g,
          "keeps her mouth completely closed and listens quietly"
        );
      }
    }
  }

  updateSceneInspectorInputs(sc);
  markProjectDirty();
  redrawTimeline();
}

function selectScene(idx) {
  window.selectScene = selectScene;
  state.selectedSceneIdx = idx;
  redrawTimeline();

  const sc = state.projectData?.scenes?.[idx];
  if (!sc) return;

  try {
    const dur = Math.max(0, sc.end - sc.start).toFixed(2);
    const fps = state.projectData?.project?.fps || 24;
    const frames = Math.round(dur * fps);
    const sceneIdEl = document.getElementById("inspectorSceneId");
    if (sceneIdEl) {
      sceneIdEl.innerHTML = `<span style="font-size: 16px; font-weight: 700; color: var(--primary);">Scene ${escapeHtml(sc.id)}</span> <span style="font-size: 12px; font-weight: normal; color: var(--text-muted); margin-left: 10px;">${sc.start.toFixed(1)}s – ${sc.end.toFixed(1)}s &bull; ${dur}s (${frames} frames)</span>`;
    }
    const actionsEl = document.getElementById("inspectorActions");
    const formEl = document.getElementById("inspectorForm");
    const emptyEl = document.getElementById("inspectorEmptyState");
    if (actionsEl) actionsEl.style.display = "flex";
    if (formEl) formEl.style.display = "block";
    if (emptyEl) emptyEl.style.display = "none";

    const modeSel = document.getElementById("sceneMode");
    if (modeSel) {
      modeSel.value = sc.mode || "solo";
      if (sc.mode && modeSel.value !== sc.mode) {
        const opt = document.createElement("option");
        opt.value = sc.mode;
        opt.textContent = sc.mode;
        modeSel.appendChild(opt);
        modeSel.value = sc.mode;
      }
    }
    
    // Resolve anchor dropdown by exact match or anchor ID (e.g. A01)
    const anchorSel = document.getElementById("sceneAnchor");
    if (anchorSel) {
      const targetAid = extractAnchorId(sc.anchor);
      let matchedOption = Array.from(anchorSel.options).find(
        (opt) => opt.value === sc.anchor || (targetAid && opt.dataset.aid === targetAid)
      );
      if (matchedOption) {
        anchorSel.value = matchedOption.value;
        sc.anchor = matchedOption.value;
      } else if (sc.anchor) {
        const opt = document.createElement("option");
        opt.value = sc.anchor;
        opt.dataset.aid = targetAid;
        opt.textContent = `${targetAid ? `[${targetAid}] ` : ""}${sc.anchor}`;
        anchorSel.appendChild(opt);
        anchorSel.value = sc.anchor;
      } else {
        anchorSel.value = "";
      }
    }
    updateSceneAnchorPreview(sc.anchor);

    const startInput = document.getElementById("sceneStart");
    const endInput = document.getElementById("sceneEnd");
    if (startInput) startInput.value = sc.start;
    if (endInput) endInput.value = sc.end;

    // Populate prompt textareas, seeds, and stem according to active mode
    updateSceneInspectorInputs(sc);

    // Update Clip Preview Video
    updateSceneClipPreview(sc);

    // Markers
    updateSceneMarkersBar(sc);
  } catch (err) {
    console.error("Error in selectScene:", err);
  }
}
window.selectScene = selectScene;

function updateSceneClipPreview(sc) {
  const videoPlayer = document.getElementById("clipVideoPlayer");
  const badge = document.getElementById("clipStatusBadge");
  const info = document.getElementById("clipFileInfo");
  const meta = document.getElementById("clipExtraMeta");

  if (!videoPlayer) return;

  // Check matching clip in assets
  const clips = state.assets?.clips || {};
  const matchedKey = Object.keys(clips).find((k) => k.startsWith(`${sc.id}_`) || k === `${sc.id}.mp4`);

  if (matchedKey) {
    if (badge) {
      badge.textContent = "Rendered";
      badge.style.background = "#10b981";
    }
    const mtime = clips[matchedKey].mtime || "";
    const newSrc = `/media/${state.currentSlug}/clip/${encodeURIComponent(matchedKey)}${mtime ? `?t=${mtime}` : ""}`;
    const currentSrc = videoPlayer.getAttribute("data-loaded-src") || videoPlayer.getAttribute("src") || "";

    if (currentSrc !== newSrc) {
      const wasPlaying = !videoPlayer.paused;
      const prevTime = videoPlayer.currentTime;
      videoPlayer.setAttribute("data-loaded-src", newSrc);
      videoPlayer.src = newSrc;
      if (wasPlaying) {
        videoPlayer.currentTime = prevTime;
        videoPlayer.play().catch(() => {});
      }
    }
    const szMb = (clips[matchedKey].size_bytes / (1024 * 1024)).toFixed(1);
    if (info) info.textContent = `${matchedKey} (${szMb} MB)`;
    if (meta) {
      meta.innerHTML = `<div style="color: var(--text-primary); font-weight: 600; margin-bottom: 2px;">Clip ready on disk</div>
        <div>Automatically syncs with the timeline scrubber and audio playback. To re-render, click <strong>⚡ Render Scene</strong> above.</div>`;
    }
  } else {
    if (badge) {
      badge.textContent = "Missing";
      badge.style.background = "#ef4444";
    }
    videoPlayer.removeAttribute("src");
    videoPlayer.load();
    if (info) info.textContent = "Not rendered yet";
    if (meta) {
      meta.innerHTML = `<div style="color: #f59e0b; font-weight: 600; margin-bottom: 2px;">No rendered MP4 clip found on disk</div>
        <div>Click <strong>⚡ Render Scene</strong> above to generate this scene's AI video pass, or start an overnight batch in the Console tab.</div>`;
    }
  }
}

function updateSceneAnchorPreview(anchorVal) {
  const container = document.getElementById("sceneAnchorThumbContainer");
  const imgEl = document.getElementById("sceneAnchorThumb");
  const infoEl = document.getElementById("sceneAnchorInfo");
  const triggerThumb = document.getElementById("anchorTriggerThumb");
  const triggerAid = document.getElementById("anchorTriggerAid");
  const triggerName = document.getElementById("anchorTriggerName");

  const aid = extractAnchorId(anchorVal);
  const diskAnchors = state.assets?.anchors || {};
  let diskInfo = diskAnchors[anchorVal] || (aid ? diskAnchors[aid] : null);
  if (!diskInfo && aid) {
    const matchedKey = Object.keys(diskAnchors).find(
      (k) => extractAnchorId(k) === aid || k.startsWith(`${aid} `) || k.startsWith(`${aid}_`) || k.startsWith(`${aid}-`)
    );
    if (matchedKey) {
      diskInfo = diskAnchors[matchedKey];
    }
  }

  // Canonical filename from projectData anchors or diskInfo
  const projAnchor = (state.projectData?.anchors || []).find((a) => extractAnchorId(a.filename) === aid);
  const resolved = diskInfo?.filename || projAnchor?.filename || anchorVal || aid;

  if (resolved) {
    const anchorMtime = diskInfo?.mtime || Date.now();
    const url = `/media/${state.currentSlug}/anchor/${encodeURIComponent(resolved)}?t=${anchorMtime}`;

    if (imgEl) {
      imgEl.onerror = () => {
        if (!imgEl.dataset.fallbackTried && aid) {
          imgEl.dataset.fallbackTried = "1";
          imgEl.src = `/media/${state.currentSlug}/anchor/${encodeURIComponent(aid)}?fallback=1`;
        }
      };
      imgEl.src = url;
    }
    if (infoEl) {
      infoEl.textContent = `${aid ? `[${aid}] ` : ""}${resolved}`;
    }
    if (container) container.style.display = "flex";

    if (triggerThumb) {
      triggerThumb.src = url;
      triggerThumb.style.display = "block";
    }
    if (triggerAid) triggerAid.textContent = aid ? `[${aid}]` : "";
    if (triggerName) triggerName.textContent = resolved;
  } else {
    if (container) container.style.display = "none";
    if (triggerThumb) triggerThumb.style.display = "none";
    if (triggerAid) triggerAid.textContent = "";
    if (triggerName) triggerName.textContent = "Select Anchor...";
  }

  // Update selection highlight in visual menu
  document.querySelectorAll(".anchor-dropdown-item").forEach((item) => {
    const isSelected = item.dataset.value === anchorVal || (aid && item.dataset.aid === aid);
    item.classList.toggle("selected", isSelected);
  });
}

function updateSceneStart(sceneIdx, newStart) {
  const scenes = state.projectData?.scenes || [];
  if (sceneIdx < 0 || sceneIdx >= scenes.length) return;
  const sc = scenes[sceneIdx];
  const minDur = 0.5;

  if (sceneIdx === 0) {
    sc.start = Math.max(0, Math.min(newStart, sc.end - minDur));
    sc.start = parseFloat(sc.start.toFixed(2));
  } else {
    const prevSc = scenes[sceneIdx - 1];
    const minTime = prevSc.start + minDur;
    const maxTime = sc.end - minDur;
    const clamped = Math.max(minTime, Math.min(newStart, maxTime));
    const t = parseFloat(clamped.toFixed(2));
    sc.start = t;
    prevSc.end = t; // Prevent gaps: previous scene end matches this start!
  }

  const startInput = document.getElementById("sceneStart");
  if (state.selectedSceneIdx === sceneIdx && startInput && document.activeElement !== startInput) {
    startInput.value = sc.start;
  }
  const prevEndInput = document.getElementById("sceneEnd");
  if (sceneIdx > 0 && state.selectedSceneIdx === sceneIdx - 1 && prevEndInput && document.activeElement !== prevEndInput) {
    prevEndInput.value = scenes[sceneIdx - 1].end;
  }
}

function updateSceneEnd(sceneIdx, newEnd) {
  const scenes = state.projectData?.scenes || [];
  if (sceneIdx < 0 || sceneIdx >= scenes.length) return;
  const sc = scenes[sceneIdx];
  const minDur = 0.5;

  if (sceneIdx < scenes.length - 1) {
    const nextSc = scenes[sceneIdx + 1];
    const minTime = sc.start + minDur;
    const maxTime = nextSc.end - minDur;
    const clamped = Math.max(minTime, Math.min(newEnd, maxTime));
    const t = parseFloat(clamped.toFixed(2));
    sc.end = t;
    nextSc.start = t; // Prevent gaps: next scene start matches this end!
  } else {
    // Last scene
    sc.end = Math.max(sc.start + minDur, newEnd);
    sc.end = parseFloat(sc.end.toFixed(2));
  }

  const endInput = document.getElementById("sceneEnd");
  if (state.selectedSceneIdx === sceneIdx && endInput && document.activeElement !== endInput) {
    endInput.value = sc.end;
  }
  const nextStartInput = document.getElementById("sceneStart");
  if (sceneIdx < scenes.length - 1 && state.selectedSceneIdx === sceneIdx + 1 && nextStartInput && document.activeElement !== nextStartInput) {
    nextStartInput.value = scenes[sceneIdx + 1].start;
  }
}

// Bind inspector form changes to in-memory state
["sceneMode", "sceneAnchor", "sceneStart", "sceneEnd", "sceneSeed", "sceneSeedM", "sceneSeedF", "sceneSplitX", "sceneStem", "sceneStemM", "sceneStemF", "scenePrompt", "scenePromptM", "scenePromptF"].forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;

  const handleInputEvent = (e) => {
    if (state.selectedSceneIdx === null) return;
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (!sc) return;

    if (id === "sceneStart") {
      updateSceneStart(state.selectedSceneIdx, parseFloat(e.target.value) || 0);
      updateSceneMarkersBar(sc);
      updatePromptResolvedPreviews(sc);
      markProjectDirty();
    } else if (id === "sceneEnd") {
      updateSceneEnd(state.selectedSceneIdx, parseFloat(e.target.value) || 0);
      updateSceneMarkersBar(sc);
      updatePromptResolvedPreviews(sc);
      markProjectDirty();
    } else if (id === "sceneSeed") {
      sc.seed = parseInt(e.target.value) || 1000;
      markProjectDirty();
    } else if (id === "sceneSeedM") {
      sc.seed_m = parseInt(e.target.value) || 1000;
      markProjectDirty();
    } else if (id === "sceneSeedF") {
      sc.seed_f = parseInt(e.target.value) || 1000;
      markProjectDirty();
    } else if (id === "sceneSplitX") {
      sc.split_x = parseFloat(e.target.value) || 0.50;
      markProjectDirty();
    } else if (id === "sceneMode") {
      handleSceneModeChange(sc, e.target.value);
    } else if (id === "sceneAnchor") {
      sc.anchor = e.target.value;
      updateSceneAnchorPreview(sc.anchor);
      markProjectDirty();
    } else if (id === "sceneStem") {
      sc.stem = e.target.value;
      markProjectDirty();
    } else if (id === "sceneStemM") {
      sc.stem_m = e.target.value;
      markProjectDirty();
    } else if (id === "sceneStemF") {
      sc.stem_f = e.target.value;
      markProjectDirty();
    } else if (id === "scenePrompt") {
      sc.prompt = e.target.value;
      autoResizeTextarea(e.target);
      markProjectDirty();
    } else if (id === "scenePromptM") {
      sc.prompt_m = e.target.value;
      autoResizeTextarea(e.target);
      markProjectDirty();
    } else if (id === "scenePromptF") {
      sc.prompt_f = e.target.value;
      autoResizeTextarea(e.target);
      markProjectDirty();
    }

    redrawTimeline();
  };

  el.addEventListener("input", handleInputEvent);
  el.addEventListener("change", handleInputEvent);
});

// Nudge buttons
document.getElementById("btnNudgeStartDown").addEventListener("click", () => nudgeTiming("start", -0.1));
document.getElementById("btnNudgeStartUp").addEventListener("click", () => nudgeTiming("start", 0.1));
document.getElementById("btnNudgeEndDown").addEventListener("click", () => nudgeTiming("end", -0.1));
document.getElementById("btnNudgeEndUp").addEventListener("click", () => nudgeTiming("end", 0.1));

function nudgeTiming(type, delta) {
  if (state.selectedSceneIdx === null) return;
  const sc = state.projectData.scenes[state.selectedSceneIdx];
  if (type === "start") {
    updateSceneStart(state.selectedSceneIdx, sc.start + delta);
  } else {
    updateSceneEnd(state.selectedSceneIdx, sc.end + delta);
  }
  redrawTimeline();
  updateSceneMarkersBar(sc);
  updatePromptResolvedPreviews(sc);
}

// Snap Start & End to nearest Marker
document.getElementById("btnSnapStartMarker")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const sc = state.projectData.scenes[state.selectedSceneIdx];
  const markers = state.projectData?.markers || [];
  if (markers.length === 0) {
    showToast("No markers available to snap to.");
    return;
  }
  let closest = markers[0];
  let minDiff = Math.abs(markers[0].time - sc.start);
  for (const m of markers) {
    const diff = Math.abs(m.time - sc.start);
    if (diff < minDiff) {
      minDiff = diff;
      closest = m;
    }
  }
  updateSceneStart(state.selectedSceneIdx, closest.time);
  redrawTimeline();
  updateSceneMarkersBar(sc);
  updatePromptResolvedPreviews(sc);
  showToast(`Snapped start to "${closest.name}" (${sc.start}s)`);
});

document.getElementById("btnSnapEndMarker")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const sc = state.projectData.scenes[state.selectedSceneIdx];
  const markers = state.projectData?.markers || [];
  if (markers.length === 0) {
    showToast("No markers available to snap to.");
    return;
  }
  let closest = markers[0];
  let minDiff = Math.abs(markers[0].time - sc.end);
  for (const m of markers) {
    const diff = Math.abs(m.time - sc.end);
    if (diff < minDiff) {
      minDiff = diff;
      closest = m;
    }
  }
  updateSceneEnd(state.selectedSceneIdx, closest.time);
  redrawTimeline();
  updateSceneMarkersBar(sc);
  updatePromptResolvedPreviews(sc);
  showToast(`Snapped end to "${closest.name}" (${sc.end}s)`);
});

document.getElementById("btnRandomSeed")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const seed = Math.floor(Math.random() * 90000) + 1000;
  state.projectData.scenes[state.selectedSceneIdx].seed = seed;
  document.getElementById("sceneSeed").value = seed;
});

document.getElementById("btnRandomSeedM")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const seed = Math.floor(Math.random() * 90000) + 1000;
  state.projectData.scenes[state.selectedSceneIdx].seed_m = seed;
  document.getElementById("sceneSeedM").value = seed;
});

document.getElementById("btnRandomSeedF")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const seed = Math.floor(Math.random() * 90000) + 1000;
  state.projectData.scenes[state.selectedSceneIdx].seed_f = seed;
  document.getElementById("sceneSeedF").value = seed;
});

// Play Scene Audio Only
document.getElementById("btnPlaySceneAudio").addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const sc = state.projectData.scenes[state.selectedSceneIdx];
  audioEl.currentTime = sc.start;
  audioEl.play();
  state.isPlaying = true;
  document.getElementById("btnPlayPause").textContent = "⏸ Pause";

  const checkEnd = () => {
    if (audioEl.currentTime >= sc.end) {
      audioEl.pause();
      state.isPlaying = false;
      document.getElementById("btnPlayPause").textContent = "▶ Play";
      audioEl.removeEventListener("timeupdate", checkEnd);
    }
  };
  audioEl.addEventListener("timeupdate", checkEnd);
});

// Render Single Scene Trigger
document.getElementById("btnRenderSceneSingle").addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const sc = state.projectData.scenes[state.selectedSceneIdx];
  switchTab("console");
  triggerRender([sc.id]);
});

// Add Scene Button
document.getElementById("btnAddScene").addEventListener("click", () => {
  const scenes = state.projectData.scenes || [];
  const last = scenes[scenes.length - 1];
  const start = last ? last.end : 0;
  const end = start + 5.0;
  const idNum = scenes.length + 1;
  const id = `S${String(idNum).padStart(2, "0")}`;

  const newScene = {
    id: id,
    renderer: null,
    start: start,
    end: end,
    mode: "male_solo",
    stem: "male",
    anchor: (state.projectData.anchors?.[0]?.filename) || "",
    seed: 1000 + idNum,
    prompt: "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>.",
  };

  scenes.push(newScene);
  markProjectDirty();
  redrawTimeline();
  selectScene(scenes.length - 1);
  showToast(`Added ${id} (${start.toFixed(1)}s - ${end.toFixed(1)}s)`);
});

// Delete Scene Function & Button
function deleteScene(idx) {
  const scenes = state.projectData?.scenes || [];
  if (idx === null || idx < 0 || idx >= scenes.length) return;
  const sc = scenes[idx];

  const dur = (sc.end - sc.start).toFixed(1);
  if (!confirm(`Delete Scene "${sc.id}" (${sc.start.toFixed(1)}s - ${sc.end.toFixed(1)}s, ${dur}s)?\n\nThis will remove the scene and close the timeline gap.`)) {
    return;
  }

  // Remove scene
  scenes.splice(idx, 1);

  // Close timeline gap to preserve continuous playback without empty holes
  if (scenes.length > 0) {
    if (idx === 0) {
      scenes[0].start = 0.00;
    } else if (idx < scenes.length) {
      scenes[idx - 1].end = scenes[idx].start;
    }
  }

  markProjectDirty();
  redrawTimeline();

  if (scenes.length === 0) {
    state.selectedSceneIdx = null;
    document.getElementById("inspectorActions").style.display = "none";
    document.getElementById("inspectorForm").style.display = "none";
    document.getElementById("inspectorEmptyState").style.display = "block";
    document.getElementById("inspectorSceneId").textContent = "No Scenes";
  } else {
    const nextIdx = Math.min(idx, scenes.length - 1);
    selectScene(nextIdx);
  }

  showToast(`🗑️ Deleted Scene ${sc.id}`);
}

document.getElementById("btnDeleteScene")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  deleteScene(state.selectedSceneIdx);
});

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

  redrawTimeline();
  if (state.selectedSceneIdx !== null) {
    const sc = state.projectData.scenes[state.selectedSceneIdx];
    updateSceneMarkersBar(sc);
    updatePromptResolvedPreviews(sc);
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
    redrawTimeline();
    if (state.selectedSceneIdx !== null) {
      const sc = state.projectData.scenes[state.selectedSceneIdx];
      updateSceneMarkersBar(sc);
      updatePromptResolvedPreviews(sc);
    }
    closeMarkerModal();
    showToast(`Saved marker: "${newName}" (${newTime}s)`);
  });

  document.getElementById("btnDeleteMarker")?.addEventListener("click", () => {
    if (state.selectedMarkerIdx === null) return;
    const m = state.projectData?.markers?.[state.selectedMarkerIdx];
    const name = m ? m.name : "marker";
    state.projectData.markers.splice(state.selectedMarkerIdx, 1);
    redrawTimeline();
    if (state.selectedSceneIdx !== null) {
      const sc = state.projectData.scenes[state.selectedSceneIdx];
      updateSceneMarkersBar(sc);
      updatePromptResolvedPreviews(sc);
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

  bar.style.display = "block";
  list.innerHTML = "";
  inScene.forEach((m) => {
    const rel = m.time - sc.start;
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
}

// ============================================================================
// Anchor Studio View
// ============================================================================
function updateAnchorStudioUI() {
  const container = document.getElementById("anchorCardsContainer");
  if (!container) return;
  container.innerHTML = "";

  const anchors = state.projectData?.anchors || [];
  const diskAnchors = state.assets?.anchors || {};

  anchors.forEach((a, idx) => {
    const card = document.createElement("div");
    card.className = "anchor-card";

    const aid = extractAnchorId(a.filename) || `A${String(idx + 1).padStart(2, "0")}`;

    // Multiple match strategies:
    // 1. Direct filename match
    // 2. Canonical aid match (A01, A02)
    // 3. Scan disk keys for prefix
    let diskInfo = diskAnchors[a.filename] || (aid ? diskAnchors[aid] : null);
    if (!diskInfo && aid) {
      const matchedKey = Object.keys(diskAnchors).find(
        (k) => extractAnchorId(k) === aid || k.startsWith(`${aid} `) || k.startsWith(`${aid}_`) || k.startsWith(`${aid}-`)
      );
      if (matchedKey) {
        diskInfo = diskAnchors[matchedKey];
      }
    }

    const resolvedName = diskInfo?.filename || a.filename || aid;
    const anchorMtime = diskInfo?.mtime || "";

    card.innerHTML = `
      <div class="anchor-thumb">
        <img src="/media/${state.currentSlug}/anchor/${encodeURIComponent(resolvedName)}${anchorMtime ? `?t=${anchorMtime}` : ""}" 
             alt="${resolvedName}" 
             loading="lazy"
             onerror="window.handleAnchorImgError(this, '${state.currentSlug}', '${aid}')">
      </div>
      <div class="anchor-body">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span class="anchor-id">${aid}</span>
          <span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #334155;">Seed: ${a.seed ?? "-"}</span>
        </div>
        <div style="font-size: 11px; color: var(--text-main); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${resolvedName}">${resolvedName}</div>
        <div class="anchor-prompt-preview">${a.prompt || "(no prompt)"}</div>
        <div style="display: flex; gap: 6px; margin-top: auto;">
          <button class="btn btn-primary" style="flex: 1;" onclick="renderSingleAnchor('${aid}')">⚡ Generate</button>
        </div>
      </div>
    `;
    container.appendChild(card);
  });
}

window.handleAnchorImgError = function (imgEl, slug, aid) {
  if (!imgEl) return;
  if (!imgEl.dataset.fallbackTried) {
    imgEl.dataset.fallbackTried = "1";
    // Fallback 1: request directly by canonical anchor ID (e.g. A01, A02)
    imgEl.src = `/media/${slug}/anchor/${encodeURIComponent(aid)}?fallback=1`;
  } else {
    // Both resolved filename and canonical ID failed
    const parent = imgEl.parentElement;
    if (parent) {
      parent.innerHTML = `<div class="missing-placeholder">Image Not Rendered Yet<br><span style="font-size: 10px; color: var(--accent-red);">[MISSING ON DISK]</span></div>`;
    }
  }
};

function updateInspectorAnchorsDropdown() {
  const sel = document.getElementById("sceneAnchor");
  sel.innerHTML = "";
  const anchors = state.projectData?.anchors || [];
  const diskAnchors = state.assets?.anchors || {};

  anchors.forEach((a, idx) => {
    const aid = extractAnchorId(a.filename) || `A${String(idx + 1).padStart(2, "0")}`;
    const diskInfo = diskAnchors[a.filename] || diskAnchors[aid];
    const resolvedName = diskInfo?.filename || a.filename;

    const opt = document.createElement("option");
    opt.value = a.filename;
    opt.dataset.aid = aid;
    opt.textContent = `[${aid}] ${resolvedName}`;
    sel.appendChild(opt);
  });
}

function updateInspectorStemsDropdown() {
  const stemSelects = [
    document.getElementById("sceneStem"),
    document.getElementById("sceneStemM"),
    document.getElementById("sceneStemF"),
  ];

  const stems = [];
  const seen = new Set();

  const addStem = (val, label) => {
    if (!val || seen.has(val.toLowerCase())) return;
    seen.add(val.toLowerCase());
    stems.push({ value: val, label: label || val });
  };

  // 1. Configured stems from project JSON (e.g. male, female)
  const cfgStems = state.projectData?.project?.stems || {};
  Object.keys(cfgStems).forEach((k) => {
    const rawFile = cfgStems[k].split("/").pop();
    addStem(k, `${k} (${rawFile})`);
  });

  // Ensure default vocal stems exist as baseline
  addStem("male", "male (male_vocal.mp3)");
  addStem("female", "female (female_vocal.mp3)");

  // 2. Any additional audio files dropped into the project audio/ folder
  const audioFiles = state.assets?.audio_files || [];
  const masterAudio = (state.projectData?.project?.master_audio || "").split("/").pop();

  audioFiles.forEach((fn) => {
    if (fn === masterAudio) return;
    const stemName = fn.replace(/\.[^/.]+$/, "");
    // Check if this audio file is already covered by a configured stem key
    const isMapped = Object.values(cfgStems).some((p) => p.endsWith(fn));
    if (!isMapped) {
      addStem(stemName, `${stemName} (${fn})`);
    }
  });

  // 3. Built-in special audio choices
  addStem("master", "full audio (master)");
  addStem("silent", "silent (no lip sync)");

  stemSelects.forEach((sel) => {
    if (!sel) return;
    const prevVal = sel.value;
    sel.innerHTML = "";
    stems.forEach((st) => {
      const opt = document.createElement("option");
      opt.value = st.value;
      opt.textContent = st.label;
      sel.appendChild(opt);
    });
    // Restore previous selection if valid
    if (prevVal && stems.some((s) => s.value.toLowerCase() === prevVal.toLowerCase())) {
      sel.value = prevVal;
    }
  });
}

window.renderSingleAnchor = function (identifier) {
  const aid = extractAnchorId(identifier) || identifier;
  switchTab("console");
  triggerRender(["--anchors-only", aid]);
};

document.getElementById("btnGenAllAnchors").addEventListener("click", () => {
  switchTab("console");
  triggerRender(["--anchors-only", "all-anchors"]);
});

// ============================================================================
// Project & Models Configuration
// ============================================================================
function populateModelSelectors() {
  const anchorSel = document.getElementById("cfgDefaultAnchorRenderer");
  const videoSel = document.getElementById("cfgDefaultVideoRenderer");

  anchorSel.innerHTML = "";
  videoSel.innerHTML = "";

  const aModels = state.models?.anchor_renderers || {};
  Object.keys(aModels).forEach((k) => {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = `[#${k}] ${aModels[k].name || aModels[k].model_type}`;
    anchorSel.appendChild(opt);
  });

  const vModels = state.models?.video_renderers || {};
  Object.keys(vModels).forEach((k) => {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = `[#${k}] ${vModels[k].name || vModels[k].model_type}`;
    videoSel.appendChild(opt);
  });
}

function updateProjectSettingsUI() {
  const p = state.projectData?.project || {};
  const m = state.projectData?.models || {};

  document.getElementById("cfgProjectName").value = p.name || "";
  document.getElementById("cfgMasterAudio").value = p.master_audio || "";
  document.getElementById("cfgFps").value = p.fps || 24;
  document.getElementById("cfgFadeOut").value = p.fade_out_last_scene_seconds || 2.0;
  document.getElementById("cfgRenderOverlap").value = p.render_overlap_seconds ?? 1.0;

  document.getElementById("cfgDefaultAnchorRenderer").value = String(m.default_anchor_renderer || 3);
  document.getElementById("cfgDefaultVideoRenderer").value = String(m.default_video_renderer || 1);
  document.getElementById("cfgNegativePrompt").value = m.video_shared_settings?.negative_prompt || "";

  renderResolutionCards(m.default_video_resolution || 4);
}

function renderResolutionCards(activeRes) {
  const container = document.getElementById("resolutionCardsContainer");
  container.innerHTML = "";

  const resDefs = state.models?.video_resolutions || state.models?.resolutions || {
    "1": { width: 832, height: 480, name: "480p Fast Preview", speed_factor: "~1 min/s", aspect: "16:9", vram_footprint: "Lowest", description: "Ultra-fast draft pass for testing camera motion and lip-sync." },
    "2": { width: 960, height: 544, name: "540p qHD Balanced", speed_factor: "~1.5 min/s", aspect: "16:9", vram_footprint: "Light", description: "Balanced speed/quality option. +30% more detail than 480p." },
    "3": { width: 1280, height: 720, name: "720p HD Standard", speed_factor: "~2.5 min/s", aspect: "16:9", vram_footprint: "Moderate", description: "Standard 720p HD widescreen resolution. Sharp, clean presentation." },
    "4": { width: 1344, height: 768, name: "768p WXGA High-Res (DEFAULT)", speed_factor: "~3.2 min/s", aspect: "16:9", vram_footprint: "Recommended", description: "Recommended native DiT training sweet spot." },
    "5": { width: 1536, height: 864, name: "864p Near-1080p", speed_factor: "~4.5 min/s", aspect: "16:9", vram_footprint: "High", description: "High fidelity generation with exceptional texture definition." },
    "6": { width: 1920, height: 1088, name: "1080p Full HD Block-Aligned", speed_factor: "~6-7 min/s", aspect: "16:9", vram_footprint: "Maximum", description: "Wan2GP official 1080p block-aligned preset (1088 height divisible by 16)." },
  };

  Object.keys(resDefs).forEach((k) => {
    const r = resDefs[k];
    const isAct = String(activeRes) === String(k);
    const card = document.createElement("div");
    card.className = `res-card ${isAct ? "active" : ""}`;
    card.innerHTML = `
      <div class="res-title">
        <span>Option ${k}: ${r.width}x${r.height}</span>
        ${isAct ? '<span style="font-size: 11px; color: var(--primary);">ACTIVE</span>' : ""}
      </div>
      <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">${r.name}</div>
      <div class="res-desc">${r.description}</div>
      <div style="font-size: 11px; color: var(--primary); margin-top: 8px;">Speed: ${r.speed_factor} | VRAM: ${r.vram_footprint}</div>
    `;
    card.addEventListener("click", () => {
      state.projectData.models.default_video_resolution = parseInt(k);
      renderResolutionCards(k);
      showToast(`Selected Resolution Option #${k} (${r.width}x${r.height})`);
    });
    container.appendChild(card);
  });
}

// Save Project Changes
document.getElementById("btnSaveProject").addEventListener("click", async () => {
  // Sync form values into state safely
  const p = state.projectData.project || (state.projectData.project = {});
  const m = state.projectData.models || (state.projectData.models = {});

  const cfgName = document.getElementById("cfgProjectName");
  if (cfgName) p.name = cfgName.value;
  const cfgAudio = document.getElementById("cfgMasterAudio");
  if (cfgAudio) p.master_audio = cfgAudio.value;
  const cfgFps = document.getElementById("cfgFps");
  if (cfgFps) p.fps = parseInt(cfgFps.value) || 24;
  const cfgFade = document.getElementById("cfgFadeOut");
  if (cfgFade) p.fade_out_last_scene_seconds = parseFloat(cfgFade.value) || 2.0;
  const cfgOverlap = document.getElementById("cfgRenderOverlap");
  if (cfgOverlap) p.render_overlap_seconds = parseFloat(cfgOverlap.value) ?? 1.0;

  const cfgAnchor = document.getElementById("cfgDefaultAnchorRenderer");
  if (cfgAnchor) m.default_anchor_renderer = parseInt(cfgAnchor.value) || 3;
  const cfgVideo = document.getElementById("cfgDefaultVideoRenderer");
  if (cfgVideo) m.default_video_renderer = parseInt(cfgVideo.value) || 1;
  const cfgNeg = document.getElementById("cfgNegativePrompt");
  if (cfgNeg) {
    m.video_shared_settings = m.video_shared_settings || {};
    m.video_shared_settings.negative_prompt = cfgNeg.value;
  }

  try {
    const res = await fetch(`/api/project/${state.currentSlug}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.projectData),
    });
    const json = await res.json();
    if (json.success) {
      markProjectDirty(false);
      showToast("💾 Project saved! (Backup archived in backup/)");
    } else {
      alert(`Save failed: ${json.error}`);
    }
  } catch (err) {
    alert(`Error saving project: ${err}`);
  }
});

// ============================================================================
// Render Console: Queue, Status & SSE Terminal Stream
// ============================================================================
function setConsoleView(mode) {
  const isQueue = mode === "queue";
  const queueView = document.getElementById("consoleQueueView");
  const logsView = document.getElementById("consoleLogsView");
  const btnQueue = document.getElementById("btnConsoleQueue");
  const btnLogs = document.getElementById("btnConsoleLogs");
  if (queueView) queueView.style.display = isQueue ? "flex" : "none";
  if (logsView) logsView.style.display = isQueue ? "none" : "flex";
  if (btnQueue) btnQueue.classList.toggle("active", isQueue);
  if (btnLogs) btnLogs.classList.toggle("active", !isQueue);
  if (!isQueue) {
    const term = document.getElementById("terminalOutput");
    if (term) term.scrollTop = term.scrollHeight;
  }
}

async function loadProjectQueue(slug = state.currentSlug) {
  if (!slug) return;
  try {
    const [qRes, stRes] = await Promise.all([
      fetch(`/api/project/${encodeURIComponent(slug)}/queue`),
      fetch("/api/render/status").catch(() => null),
    ]);
    const json = await qRes.json();
    const st = stRes ? await stRes.json().catch(() => null) : null;
    const isRunning = Boolean(st && st.running);

    if (json.success && json.queue) {
      if (!state.sseTaskStatuses) state.sseTaskStatuses = new Map();

      // If engine is not running, purge any stale 'rendering' statuses
      if (!isRunning) {
        state.activeRenderTask = null;
        for (const [k, v] of state.sseTaskStatuses.entries()) {
          if (v && v.status === "rendering") state.sseTaskStatuses.delete(k);
        }
      }

      // Reconcile with any buffered SSE task events
      json.queue.forEach((t) => {
        const sse = state.sseTaskStatuses.get(t.idx) || state.sseTaskStatuses.get(t.filename);
        if (sse) {
          if (isRunning || sse.status !== "rendering") {
            t.status = sse.status;
          }
          if (sse.elapsed !== undefined) t.elapsed = sse.elapsed;
          if (sse.reason !== undefined) t.reason = sse.reason;
          if (sse.pct !== undefined) t.pct = sse.pct;
          if (sse.error !== undefined) t.error = sse.error;
        }
        if (!isRunning && t.status === "rendering") {
          t.status = t.needs_render ? "queued" : "skipped";
        }
      });

      state.renderQueue = json.queue;

      if (isRunning && !state.activeRenderTask) {
        const activeItem = state.renderQueue.find((t) => t.status === "rendering");
        if (activeItem) {
          state.activeRenderTask = {
            idx: activeItem.idx,
            total: activeItem.total || state.renderQueue.length,
            filename: activeItem.filename,
            reason: activeItem.reason,
            model: activeItem.model_type,
            frames: activeItem.video_length,
            resolution: activeItem.resolution,
          };
          if (!state.renderStartTime) state.renderStartTime = Date.now();
          if (!state.renderTimerInterval) {
            state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
          }
        }
      }

      renderQueueUI();
    }
  } catch (err) {
    console.warn("Failed to load project queue:", err);
  }
}

function renderQueueUI() {
  const queue = state.renderQueue || [];
  const tbody = document.getElementById("queueTableBody");
  if (!tbody) return;

  const totalCount = queue.length;
  const queuedCount = queue.filter((t) => t.status === "queued" || t.status === "rendering").length;
  const doneCount = queue.filter((t) => t.status === "completed").length;
  const skippedCount = queue.filter((t) => t.status === "skipped").length;

  const countAll = document.getElementById("countAll");
  const countQueued = document.getElementById("countQueued");
  const countDone = document.getElementById("countDone");
  const countSkipped = document.getElementById("countSkipped");
  if (countAll) countAll.textContent = totalCount;
  if (countQueued) countQueued.textContent = queuedCount;
  if (countDone) countDone.textContent = doneCount;
  if (countSkipped) countSkipped.textContent = skippedCount;

  // Active Task Hero Card
  const activeTask = state.activeRenderTask;
  const titleEl = document.getElementById("activeTaskTitle");
  const headingEl = document.getElementById("activeTaskHeading");
  const detailsEl = document.getElementById("activeTaskDetails");
  const timerEl = document.getElementById("activeTaskTimer");
  const progContainer = document.getElementById("activeProgressBarContainer");

  if (activeTask) {
    if (headingEl) {
      headingEl.textContent = `⚡ RENDERING TASK [${String(activeTask.idx).padStart(2, "0")}/${activeTask.total || totalCount}]`;
      headingEl.style.color = "#38bdf8";
    }
    if (titleEl) titleEl.textContent = activeTask.filename;
    if (detailsEl) {
      detailsEl.innerHTML = `
        <span style="background: rgba(56,189,248,0.15); color: #38bdf8; padding: 2px 6px; border-radius: 4px;">${activeTask.model || "GPU Model"}</span>
        <span style="background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px;">${activeTask.frames ? `${activeTask.frames} frames` : ""}</span>
        <span style="background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px;">${activeTask.resolution || ""}</span>
        <span style="background: rgba(245,158,11,0.15); color: #f59e0b; padding: 2px 6px; border-radius: 4px;">Reason: ${activeTask.reason || "QUEUED"}</span>
      `;
    }
    if (timerEl) timerEl.style.display = "inline";
    if (progContainer) progContainer.style.display = "block";
  } else {
    if (headingEl) {
      headingEl.textContent = "CURRENT STATUS";
      headingEl.style.color = "var(--text-muted)";
    }
    if (titleEl) {
      titleEl.textContent = queuedCount > 0 ? `${queuedCount} file(s) waiting in queue` : "All files up-to-date";
    }
    if (detailsEl) {
      detailsEl.innerHTML = `
        <span id="queueStatsSummary">${totalCount} Total Passes • ${queuedCount} Queued • ${doneCount} Finished • ${skippedCount} Up-to-date</span>
      `;
    }
    if (timerEl) timerEl.style.display = "none";
    if (progContainer) progContainer.style.display = "none";
  }

  // Filter items
  const filter = state.queueFilter || "all";
  const filtered = queue.filter((t) => {
    if (filter === "all") return true;
    if (filter === "queued") return t.status === "queued" || t.status === "rendering";
    if (filter === "completed") return t.status === "completed";
    if (filter === "skipped") return t.status === "skipped";
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="padding: 24px; text-align: center; color: var(--text-muted);">No files match filter "${filter}".</td></tr>`;
    return;
  }

  const queuedIndices = queue
    .map((t, i) => (t.status === "queued" ? i : -1))
    .filter((i) => i !== -1);
  const firstQueuedIdx = queuedIndices.length > 0 ? queuedIndices[0] : -1;
  const lastQueuedIdx = queuedIndices.length > 0 ? queuedIndices[queuedIndices.length - 1] : -1;

  tbody.innerHTML = filtered
    .map((t) => {
      const isRendering = t.status === "rendering";
      const isCompleted = t.status === "completed";
      const isQueued = t.status === "queued";
      const isFailed = t.status === "failed";

      let statusBadge = "";
      if (isRendering) {
        statusBadge = `<span style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); padding: 2px 8px; border-radius: 4px; font-weight: 600;">⚡ Rendering (${t.pct || 0}%)</span>`;
      } else if (isQueued) {
        statusBadge = `<span style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); padding: 2px 8px; border-radius: 4px; font-weight: 600;">⏳ Queued</span>`;
      } else if (isCompleted) {
        statusBadge = `<span style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); padding: 2px 8px; border-radius: 4px; font-weight: 600;">✅ Done ${t.elapsed ? `(${t.elapsed}s)` : ""}</span>`;
      } else if (isFailed) {
        statusBadge = `<span style="background: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); padding: 2px 8px; border-radius: 4px; font-weight: 600;">❌ Failed</span>`;
      } else {
        statusBadge = `<span style="background: rgba(100, 116, 139, 0.15); color: #94a3b8; padding: 2px 8px; border-radius: 4px;">⏩ Skipped</span>`;
      }

      const rowStyle = isRendering
        ? "background: rgba(56, 189, 248, 0.1); border-left: 3px solid #38bdf8;"
        : "border-bottom: 1px solid rgba(255, 255, 255, 0.04);";

      const idxStr = String(t.idx).padStart(2, "0");
      const shortReason = (t.reason || "").replace(/UP-TO-DATE \(COVERED BY RENDER\)/i, "UP-TO-DATE");
      const targetId = t.filename || t.id || t.output_filename;

      const globalIdx = queue.findIndex((x) => (x.filename || x.output_filename) === targetId || x.idx === t.idx);
      const canUp = isQueued && globalIdx > firstQueuedIdx;
      const canDown = isQueued && globalIdx < lastQueuedIdx;

      let actionsHtml = "";
      if (isRendering) {
        actionsHtml = `<span style="color: #38bdf8; font-size: 10px; font-weight: 600;">Active</span>`;
      } else if (isQueued) {
        actionsHtml = `
          <div style="display: flex; gap: 4px; justify-content: center; align-items: center;">
            <button type="button" class="btn-queue-action btn-queue-up" data-id="${targetId}" title="Move Up" ${canUp ? "" : "disabled"} style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 3px; opacity: ${canUp ? "1" : "0.3"}; cursor: ${canUp ? "pointer" : "default"};">⬆️</button>
            <button type="button" class="btn-queue-action btn-queue-down" data-id="${targetId}" title="Move Down" ${canDown ? "" : "disabled"} style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 3px; opacity: ${canDown ? "1" : "0.3"}; cursor: ${canDown ? "pointer" : "default"};">⬇️</button>
            <button type="button" class="btn-queue-action btn-queue-del" data-id="${targetId}" title="Remove from Queue" style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(239,68,68,0.3); border-radius: 3px; cursor: pointer; color: #ef4444;">🗑️</button>
          </div>
        `;
      } else {
        actionsHtml = `
          <div style="display: flex; justify-content: center; align-items: center;">
            <button type="button" class="btn-queue-action btn-queue-add" data-id="${targetId}" title="Add to Queue" style="padding: 1px 6px; font-size: 10px; cursor: pointer; background: rgba(255,255,255,0.06); border: 1px solid var(--border); border-radius: 3px; color: var(--text-muted);">➕ Queue</button>
          </div>
        `;
      }

      return `
        <tr style="${rowStyle}">
          <td style="padding: 6px 10px; color: var(--text-muted); font-family: monospace;">${idxStr}</td>
          <td style="padding: 6px 10px; font-weight: 500;">
            <div style="color: ${isRendering ? "#38bdf8" : isCompleted ? "#10b981" : "var(--text-main)"}; font-family: monospace;">${t.filename}</div>
          </td>
          <td style="padding: 6px 10px;">${statusBadge}</td>
          <td style="padding: 6px 10px; color: var(--text-muted); font-size: 10px;">
            <span>${shortReason}</span>
          </td>
          <td style="padding: 6px 10px; text-align: center;">
            ${actionsHtml}
          </td>
        </tr>
      `;
    })
    .join("");
}

function handleQueueSSEEvent(line) {
  try {
    const spaceIdx = line.indexOf(" ");
    if (spaceIdx === -1) return;
    const tag = line.slice(0, spaceIdx);
    const jsonStr = line.slice(spaceIdx + 1);
    const payload = JSON.parse(jsonStr);

    if (!state.sseTaskStatuses) state.sseTaskStatuses = new Map();

    if (tag === "[QUEUE_ACTIVE]") {
      state.activeRenderTask = payload;
      state.renderStartTime = Date.now();
      if (!state.renderTimerInterval) {
        state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
      }
      state.sseTaskStatuses.set(payload.idx, { status: "rendering", payload });
      if (payload.filename) state.sseTaskStatuses.set(payload.filename, { status: "rendering", payload });

      const match = state.renderQueue.find((t) => t.idx === payload.idx || t.filename === payload.filename);
      if (match) {
        match.status = "rendering";
        match.reason = payload.reason || match.reason;
      }
      renderQueueUI();
    } else if (tag === "[QUEUE_PROGRESS]") {
      if (state.activeRenderTask && state.activeRenderTask.idx === payload.idx) {
        state.activeRenderTask.phase = payload.phase;
        state.activeRenderTask.pct = payload.pct;
        state.activeRenderTask.step = payload.step;
        state.activeRenderTask.total_steps = payload.total_steps;
      }
      const existing = state.sseTaskStatuses.get(payload.idx);
      if (existing) {
        existing.pct = payload.pct;
        existing.step = payload.step;
        existing.total_steps = payload.total_steps;
        existing.phase = payload.phase;
      }
      const match = state.renderQueue.find((t) => t.idx === payload.idx);
      if (match) match.pct = payload.pct;

      // Direct DOM update for 60fps performance
      const pb = document.getElementById("activeProgressBar");
      if (pb) pb.style.width = `${payload.pct}%`;
      const pctText = document.getElementById("activeProgressPct");
      if (pctText) pctText.textContent = `${payload.pct}%`;
      const phaseText = document.getElementById("activeProgressPhase");
      if (phaseText) {
        phaseText.textContent = `[${payload.phase}] Step ${payload.step}/${payload.total_steps} (${payload.pct}%)`;
      }
    } else if (tag === "[QUEUE_DONE]") {
      state.sseTaskStatuses.set(payload.idx, { status: "completed", elapsed: payload.elapsed });
      if (payload.filename) state.sseTaskStatuses.set(payload.filename, { status: "completed", elapsed: payload.elapsed });

      const match = state.renderQueue.find((t) => t.idx === payload.idx || t.filename === payload.filename);
      if (match) {
        match.status = "completed";
        match.elapsed = payload.elapsed;
      }
      if (state.activeRenderTask && (state.activeRenderTask.idx === payload.idx || state.activeRenderTask.filename === payload.filename)) {
        state.activeRenderTask = null;
      }
      renderQueueUI();
    } else if (tag === "[QUEUE_SKIP]") {
      state.sseTaskStatuses.set(payload.idx, { status: "skipped", reason: payload.reason });
      if (payload.filename) state.sseTaskStatuses.set(payload.filename, { status: "skipped", reason: payload.reason });

      const match = state.renderQueue.find((t) => t.idx === payload.idx || t.filename === payload.filename);
      if (match) {
        match.status = "skipped";
        match.reason = payload.reason || match.reason;
      }
      renderQueueUI();
    } else if (tag === "[QUEUE_FAIL]") {
      state.sseTaskStatuses.set(payload.idx, { status: "failed", error: payload.error });
      if (payload.filename) state.sseTaskStatuses.set(payload.filename, { status: "failed", error: payload.error });

      const match = state.renderQueue.find((t) => t.idx === payload.idx || t.filename === payload.filename);
      if (match) {
        match.status = "failed";
        match.error = payload.error;
      }
      if (state.activeRenderTask && (state.activeRenderTask.idx === payload.idx || state.activeRenderTask.filename === payload.filename)) {
        state.activeRenderTask = null;
      }
      renderQueueUI();
    }
  } catch (err) {
    console.error("Queue SSE parse error:", err);
  }
}

function updateRenderTimer() {
  if (!state.renderStartTime) return;
  const elapsedSec = Math.floor((Date.now() - state.renderStartTime) / 1000);
  const m = Math.floor(elapsedSec / 60);
  const s = elapsedSec % 60;
  const timerEl = document.getElementById("activeTaskTimer");
  if (timerEl) {
    timerEl.textContent = `⏱ ${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
}

function initLogStream() {
  if (state.eventSource) {
    try {
      state.eventSource.close();
    } catch (e) {}
  }

  const es = new EventSource("/api/render/events");
  const term = document.getElementById("terminalOutput");

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.text) {
        if (data.text.includes("[MASTER_UPDATED]")) {
          showToast("🎬 Master video restitched!");
          selectProject(state.currentSlug);
          return;
        }
        if (data.text.startsWith("[QUEUE_")) {
          handleQueueSSEEvent(data.text);
          return;
        }
        const line = document.createElement("div");
        line.textContent = data.text;
        term.appendChild(line);
        term.scrollTop = term.scrollHeight;
      }

      if (data.done) {
        if (state.renderTimerInterval) {
          clearInterval(state.renderTimerInterval);
          state.renderTimerInterval = null;
        }
        state.activeRenderTask = null;
        renderQueueUI();
        document.getElementById("renderStatusText").textContent = `Finished (Code ${data.exit_code})`;
        document.getElementById("renderStatusText").style.color = data.exit_code === 0 ? "var(--accent-green)" : "var(--accent-red)";
        // Refresh project assets & master video
        selectProject(state.currentSlug);
      }
    } catch (err) {
      console.error("SSE parse error:", err);
    }
  };

  es.onerror = () => {
    console.warn("SSE connection status change or reconnecting...");
  };

  state.eventSource = es;
}

async function triggerRender(args = []) {
  const term = document.getElementById("terminalOutput");
  term.innerHTML = "";
  document.getElementById("renderStatusText").textContent = "Running...";
  document.getElementById("renderStatusText").style.color = "var(--primary)";

  // Clear previous task state so stale tasks (e.g. S22) never linger
  state.activeRenderTask = null;
  state.renderStartTime = Date.now();
  if (args && args.length > 0) {
    const targetSid = String(args[0]).trim().toUpperCase();
    const match = state.renderQueue?.find(
      (t) => (t.scene_id || "").toUpperCase() === targetSid || (t.filename || "").toUpperCase().includes(targetSid)
    );
    if (match) {
      state.activeRenderTask = {
        idx: match.idx,
        total: state.renderQueue.length,
        filename: match.filename,
        reason: `TARGETED (${targetSid})`,
        model: match.model_type,
        frames: match.video_length,
        resolution: match.resolution,
      };
    }
  }
  renderQueueUI();

  // Re-establish SSE connection if absent or closed
  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) {
    initLogStream();
  }

  try {
    const res = await fetch("/api/render/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: state.currentSlug, args: args }),
    });
    const json = await res.json();
    if (!json.success) {
      alert(`Render error: ${json.error}`);
    } else if (json.queued === false) {
      document.getElementById("renderStatusText").textContent = "Up to date";
      document.getElementById("renderStatusText").style.color = "var(--success)";
      showToast(`✔ ${json.message || "All passes up-to-date"}`);
      loadProjectQueue(state.currentSlug);
    } else {
      showToast(`⚡ ${json.message || "Render started"}`);
      loadProjectQueue(state.currentSlug);
    }
  } catch (err) {
    alert(`Failed to start render: ${err}`);
  }
}

document.getElementById("btnConsoleQueue")?.addEventListener("click", () => setConsoleView("queue"));
document.getElementById("btnConsoleLogs")?.addEventListener("click", () => setConsoleView("logs"));

document.querySelectorAll(".btn-filter[data-filter]").forEach((b) => {
  b.addEventListener("click", (e) => {
    document.querySelectorAll(".btn-filter[data-filter]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.queueFilter = b.dataset.filter;
    renderQueueUI();
  });
});

document.getElementById("btnReloadQueue")?.addEventListener("click", () => {
  loadProjectQueue(state.currentSlug);
  showToast("Queue refreshed");
});

document.getElementById("btnQueueAll")?.addEventListener("click", async () => {
  try {
    const slug = state.currentSlug || "all_or_nothing";
    const res = await fetch("/api/queue/enqueue_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    const data = await res.json();
    if (data.queue) {
      state.renderQueue = data.queue;
      renderQueueUI();
      showToast("All unrendered items enqueued");
    }
  } catch (err) {
    showToast("Failed to queue all items");
  }
});

document.getElementById("btnClearQueue")?.addEventListener("click", async () => {
  try {
    const slug = state.currentSlug || "all_or_nothing";
    const res = await fetch("/api/queue/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    const data = await res.json();
    if (data.queue) {
      state.renderQueue = data.queue;
      renderQueueUI();
      showToast("Queue cleared");
    }
  } catch (err) {
    showToast("Failed to clear queue");
  }
});

// Event delegation for table action buttons (Move Up, Move Down, Delete, Add)
document.getElementById("queueTableBody")?.addEventListener("click", async (e) => {
  const btn = e.target.closest(".btn-queue-action");
  if (!btn || btn.disabled) return;
  const id = btn.dataset.id;
  if (!id) return;
  const slug = state.currentSlug || "all_or_nothing";

  if (btn.classList.contains("btn-queue-up")) {
    try {
      const res = await fetch("/api/queue/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, id, direction: "up" }),
      });
      const data = await res.json();
      if (data.queue) {
        state.renderQueue = data.queue;
        renderQueueUI();
      }
    } catch (err) {
      console.error(err);
    }
  } else if (btn.classList.contains("btn-queue-down")) {
    try {
      const res = await fetch("/api/queue/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, id, direction: "down" }),
      });
      const data = await res.json();
      if (data.queue) {
        state.renderQueue = data.queue;
        renderQueueUI();
      }
    } catch (err) {
      console.error(err);
    }
  } else if (btn.classList.contains("btn-queue-del")) {
    try {
      const res = await fetch("/api/queue/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, id }),
      });
      const data = await res.json();
      if (data.queue) {
        state.renderQueue = data.queue;
        renderQueueUI();
        showToast("Removed from queue");
      }
    } catch (err) {
      console.error(err);
    }
  } else if (btn.classList.contains("btn-queue-add")) {
    try {
      const res = await fetch("/api/queue/enqueue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, id }),
      });
      const data = await res.json();
      if (data.queue) {
        state.renderQueue = data.queue;
        renderQueueUI();
        showToast("Added to queue");
      }
    } catch (err) {
      console.error(err);
    }
  }
});

async function triggerImmediateRestitch() {
  const btn1 = document.getElementById("btnStitchMaster");
  const btn2 = document.getElementById("btnStitchMasterFromTab");
  if (btn1) {
    btn1.dataset.origText = btn1.textContent;
    btn1.textContent = "⏳ Restitching...";
    btn1.disabled = true;
  }
  if (btn2) {
    btn2.dataset.origText = btn2.textContent;
    btn2.textContent = "⏳ Restitching...";
    btn2.disabled = true;
  }
  showToast("🧵 Restitching master video now...");

  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) {
    initLogStream();
  }

  try {
    const slug = state.currentSlug || "all_or_nothing";
    const res = await fetch("/api/stitch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    const json = await res.json();
    if (!json.success) {
      showToast(`Stitch error: ${json.error || "Unknown"}`);
    }
  } catch (err) {
    showToast(`Failed to trigger restitch: ${err}`);
  } finally {
    setTimeout(() => {
      if (btn1) {
        btn1.textContent = btn1.dataset.origText || "🎬 Restitch Master MP4";
        btn1.disabled = false;
      }
      if (btn2) {
        btn2.textContent = btn2.dataset.origText || "🧵 Stitch Master Video";
        btn2.disabled = false;
      }
    }, 1500);
  }
}

document.getElementById("btnRenderAll").addEventListener("click", () => triggerRender([]));
document.getElementById("btnStitchMaster").addEventListener("click", () => triggerImmediateRestitch());

const chkDummyMode = document.getElementById("chkDummyRenderMode");
if (chkDummyMode) {
  fetch("/api/settings/dummy_mode")
    .then((r) => r.json())
    .then((data) => {
      if (data && data.dummy_render !== undefined) {
        chkDummyMode.checked = !!data.dummy_render;
      }
    })
    .catch(() => {});

  chkDummyMode.addEventListener("change", async () => {
    try {
      const res = await fetch("/api/settings/dummy_mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: chkDummyMode.checked }),
      });
      const data = await res.json();
      showToast(chkDummyMode.checked ? "🧪 Dummy render engine enabled (Fast mock simulation)" : "⚡ Real Wan2GP engine enabled");
    } catch (e) {
      console.error("Failed to toggle dummy mode:", e);
    }
  });
}
document.getElementById("btnStopRender").addEventListener("click", async () => {
  const statusElem = document.getElementById("renderStatusText");
  statusElem.textContent = "Stopped (Ready)";
  statusElem.style.color = "var(--text-muted)";
  if (state.renderTimerInterval) {
    clearInterval(state.renderTimerInterval);
    state.renderTimerInterval = null;
  }
  state.activeRenderTask = null;
  renderQueueUI();
  try {
    const res = await fetch("/api/render/stop", { method: "POST" });
    const json = await res.json();
    showToast("Render process terminated.");
  } catch (err) {
    showToast(`Error stopping render: ${err}`);
  }
});

function updateMasterVideoUI() {
  const masters = state.assets?.master_videos || [];
  const player = document.getElementById("masterVideoPlayer");
  const meta = document.getElementById("masterVideoMeta");
  const badge = document.getElementById("masterVideoBadge");

  if (!player) return;

  if (masters.length > 0) {
    const latest = masters[0];
    const mtime = latest.mtime || "";
    const newSrc = `/media/${state.currentSlug}/master${mtime ? `?t=${mtime}` : ""}`;
    const currentSrc = player.getAttribute("data-loaded-src") || player.getAttribute("src") || "";

    if (currentSrc !== newSrc) {
      const wasPlaying = !player.paused;
      const prevTime = player.currentTime;
      player.setAttribute("data-loaded-src", newSrc);
      player.src = newSrc;
      if (wasPlaying) {
        player.currentTime = prevTime;
        player.play().catch(() => {});
      }
    }
    if (meta) {
      meta.textContent = `File: ${latest.filename} (${latest.size_mb} MB)`;
    }
    if (badge) {
      badge.textContent = "Available";
      badge.style.background = "#10b981";
    }
  } else {
    player.removeAttribute("src");
    player.load();
    if (meta) {
      meta.textContent = "No master MP4 render generated yet on disk.";
    }
    if (badge) {
      badge.textContent = "Missing";
      badge.style.background = "#64748b";
    }
  }
}

document.getElementById("btnRefreshMasterVideo")?.addEventListener("click", () => {
  updateMasterVideoUI();
  showToast("Master video reloaded");
});

document.getElementById("btnStitchMasterFromTab")?.addEventListener("click", () => {
  triggerImmediateRestitch();
});

function switchTab(tabName) {
  const main = document.getElementById("appMain");
  const sidePane = document.getElementById("sidePane");
  const sidePaneTitle = document.getElementById("sidePaneTitle");

  const tabTitles = {
    anchors: "🎨 Character & Scene Anchors",
    settings: "⚙️ Project & Model Settings",
    console: "🚀 Render Console",
    master: "📺 Master Video Player",
  };

  if (!main || !sidePane) return;

  if (tabName === "timeline") {
    // 1. Full Screen Timeline
    main.classList.remove("layout-split");
    main.classList.add("layout-full");
    sidePane.style.display = "none";

    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === "timeline");
    });
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
  } else {
    // 2. Split Screen: Timeline on left, tab pane on right
    main.classList.remove("layout-full");
    main.classList.add("layout-split");
    sidePane.style.display = "flex";

    if (sidePaneTitle && tabTitles[tabName]) {
      sidePaneTitle.textContent = tabTitles[tabName];
    }

    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tabName);
    });

    document.querySelectorAll(".tab-pane").forEach((p) => {
      p.classList.toggle("active", p.id === `tab-${tabName}`);
    });

    if (tabName === "anchors") {
      updateAnchorStudioUI();
    } else if (tabName === "console") {
      setConsoleView("queue");
      loadProjectQueue(state.currentSlug);
    } else if (tabName === "master") {
      updateMasterVideoUI();
    }
  }

  // Smoothly recalculate canvas timeline
  setTimeout(() => {
    redrawTimeline();
  }, 50);
}
