// Audio & Waveform Engine (Web Audio API + Canvas)
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

let isScrubbingRuler = false, isRightClickDragging = false, rightClickStartX = 0, rightClickStartScrollLeft = 0;

function togglePlayback() {
  if (state.isPlaying) {
    audioEl.pause();
    ["clipVideoPlayer", "masterVideoPlayer"].forEach((id) => { const v = document.getElementById(id); if (v && !v.paused) v.pause(); });
  } else {
    audioEl.play().catch((e) => console.warn("Audio play prevented:", e));
  }
}

document.getElementById("btnPlayPause").addEventListener("click", togglePlayback);
document.getElementById("btnStop").addEventListener("click", () => {
  audioEl.pause(); audioEl.currentTime = 0;
  ["clipVideoPlayer", "masterVideoPlayer"].forEach((id) => { const v = document.getElementById(id); if (v) { v.pause(); v.currentTime = 0; } });
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
  if (autoScroll && scrollContainer && !isRightClickDragging) {
    const sl = scrollContainer.scrollLeft;
    const cw = scrollContainer.clientWidth;
    if (x > sl + cw - 40) {
      scrollContainer.scrollLeft = Math.max(0, x - 80);
    } else if (x < sl) {
      scrollContainer.scrollLeft = Math.max(0, x - 80);
    }
  }
}

function getClipRenderStart(sc) {
  const clips = state.assets?.clips || {};
  const c = clips[sc.id] || clips[sc.id?.toUpperCase()] || clips[sc.id?.toLowerCase()];
  if (c && typeof c.render_start === "number") return c.render_start;
  const k = Object.keys(clips).find((x) => x.startsWith(`${sc.id}_`) || x === `${sc.id}.mp4`);
  return (k && typeof clips[k]?.render_start === "number") ? clips[k].render_start : sc.start;
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
      const rel = cur - getClipRenderStart(sc);
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
    if (sc) updatePlayheadVisual(getClipRenderStart(sc) + clipVideo.currentTime, true);
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
        if (sc) updatePlayheadVisual(getClipRenderStart(sc) + clipVideo.currentTime, !clipVideo.paused);
      }
    });
    clipVideo.addEventListener("seeked", () => {
      if (!isScrubbingRuler && state.selectedSceneIdx !== null) {
        const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
        if (sc) updatePlayheadVisual(getClipRenderStart(sc) + clipVideo.currentTime, false);
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
  // Enforce maximum safe canvas width below browser/GPU texture limits to prevent white canvas bug
  const maxSafeWidth = 60000;
  const totalWidth = Math.min(maxSafeWidth, Math.max(dur * pps, scrollContainer.clientWidth));

  // Dynamic vertical scaling: track height and scene thumbnails resize proportionally with zoom
  const baseTrackHeight = 60;
  const trackHeight = Math.max(30, Math.round(baseTrackHeight * state.zoom));
  const trackTop = 84;
  const bottomMargin = 16;
  const totalHeight = trackTop + trackHeight + bottomMargin;

  if (canvas.width !== totalWidth) {
    canvas.width = totalWidth;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
  }
  if (canvas.height !== totalHeight) canvas.height = totalHeight;

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
    if (sc.render_pad_start > 0 || sc.render_pad_end > 0) {
      ctx.fillStyle = "#38bdf8";
      ctx.fillRect(sx, trackTop, sw, 2);
    }

    // 1. If Video rendered: Show multiple thumbnails from video (filmstrip)
    let stripDrawn = false;
    if (hasClip) {
      const clipInfo =
        clips[sc.id] ||
        clips[sc.id.toUpperCase()] ||
        clips[sc.id.toLowerCase()] ||
        Object.values(clips).find(
          (c) =>
            c.filename &&
            (c.filename.startsWith(`${sc.id}_`) ||
              c.filename.includes(`_${sc.id}_`))
        );
      const stripInfo = strips[sc.id] || strips[sc.id.toUpperCase()] || strips[sc.id.toLowerCase()];
      const mtime = clipInfo?.mtime || stripInfo?.mtime || "";
      const stripUrl = `/media/${state.currentSlug}/strip/${encodeURIComponent(sc.id)}${mtime ? `?t=${mtime}` : ""}`;
      const stripImg = getCanvasImage(stripUrl);
      if (stripImg && !stripImg.failed && stripImg.complete && stripImg.naturalWidth > 0) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(sx, trackTop, sw, trackHeight);
        ctx.clip();

        // Maintain original 16:9 aspect ratio for each clip thumbnail
        const frameAspect = stripImg.naturalHeight > 0 ? stripImg.naturalWidth / stripImg.naturalHeight : 1;
        const numFrames = Math.max(1, Math.round(frameAspect / (16 / 9)));
        const thumbAspect = 16 / 9;
        const thumbW = Math.max(20, trackHeight * thumbAspect);
        const srcFrameW = stripImg.naturalWidth / numFrames;
        const numThumbs = Math.ceil(sw / thumbW);

        for (let i = 0; i < numThumbs; i++) {
          const curX = sx + i * thumbW;
          const f = Math.min(numFrames - 1, Math.floor((i / Math.max(1, numThumbs)) * numFrames));
          ctx.drawImage(stripImg, f * srcFrameW, 0, srcFrameW, stripImg.naturalHeight, curX, trackTop, thumbW, trackHeight);
          if (i > 0) {
            ctx.strokeStyle = "rgba(0, 0, 0, 0.45)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(curX, trackTop);
            ctx.lineTo(curX, trackTop + trackHeight);
            ctx.stroke();
          }
        }
        ctx.restore();
        stripDrawn = true;
      }
    }

    // 2. If strip not drawn (or video not rendered): Tile Anchor Image to fill the entire strip
    if (!stripDrawn) {
      const aid = extractAnchorId(sc.anchor);
      const diskInfo = diskAnchors[sc.anchor] || (aid ? diskAnchors[aid] : null);
      if (diskInfo) {
        const anchorMtime = diskInfo?.mtime || "";
        const anchorUrl = `/media/${state.currentSlug}/anchor/${encodeURIComponent(diskInfo.filename || sc.anchor)}${anchorMtime ? `?t=${anchorMtime}` : ""}`;
        const anchorImg = getCanvasImage(anchorUrl);
        if (anchorImg && !anchorImg.failed && anchorImg.complete && anchorImg.naturalWidth > 0) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(sx, trackTop, sw, trackHeight);
          ctx.clip();

          const thumbW = Math.max(20, trackHeight * (16 / 9));
          const numThumbs = Math.ceil(sw / thumbW);
          for (let i = 0; i < numThumbs; i++) {
            const curX = sx + i * thumbW;
            ctx.drawImage(anchorImg, curX, trackTop, thumbW, trackHeight);
            if (i > 0) {
              ctx.strokeStyle = "rgba(0, 0, 0, 0.45)";
              ctx.lineWidth = 1;
              ctx.beginPath();
              ctx.moveTo(curX, trackTop);
              ctx.lineTo(curX, trackTop + trackHeight);
              ctx.stroke();
            }
          }
          ctx.restore();
        }
      }
    }

    // Top header banner across the block for text readability
    ctx.fillStyle = "rgba(15, 23, 42, 0.78)";
    ctx.fillRect(sx, trackTop, sw, 18);

    // Status badge calculations
    const aid = extractAnchorId(sc.anchor);
    const badgeW = (hasClip && sw > 90) ? 48 : (aid && sw > 90 ? 46 : 0);
    const rightLimit = sx + sw - (badgeW > 0 ? badgeW + 6 : 6);

    // Sticky Scene ID & timing (stays visible while scrolling left until no room)
    const curScrollLeft = scrollContainer ? scrollContainer.scrollLeft : 0;
    const labelX = Math.max(sx + 6, curScrollLeft + 6);
    const fullLabel = `${sc.id} [${(sc.end - sc.start).toFixed(1)}s]`;
    ctx.font = "bold 11px sans-serif";
    const fullW = ctx.measureText(fullLabel).width;
    const shortW = ctx.measureText(sc.id).width;
    const labelToDraw = (labelX + fullW <= rightLimit) ? fullLabel : ((labelX + shortW <= rightLimit) ? sc.id : null);
    if (labelToDraw) {
      ctx.fillStyle = "#ffffff";
      ctx.fillText(labelToDraw, labelX, trackTop + 13);
    }

    const qItem = (state.renderQueue || []).find((t) => (t.scene_id || "").toUpperCase() === (sc.id || "").toUpperCase());
    const isQueued = qItem && (qItem.status === "queued" || qItem.needs_render);
    const isRendering = qItem && qItem.status === "rendering";

    // Status badge (Top right of block)
    if (badgeW > 0) {
      let bBg = hasClip ? (isQueued ? "rgba(245, 158, 11, 0.95)" : "rgba(16, 185, 129, 0.95)") : "rgba(56, 189, 248, 0.85)";
      let bTxt = hasClip ? (isQueued ? "▶ QUEUED" : "▶ VIDEO") : `⚓ ${aid}`;
      let tCol = hasClip ? "#ffffff" : "#000000";
      if (isRendering) { bBg = "rgba(56, 189, 248, 0.95)"; bTxt = "⚡ RENDERING"; tCol = "#ffffff"; }
      ctx.fillStyle = bBg;
      const bx = sx + sw - badgeW;
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(bx, trackTop + 2, badgeW - 4, 14, 2); ctx.fill(); }
      else { ctx.fillRect(bx, trackTop + 2, badgeW - 4, 14); }
      ctx.fillStyle = tCol;
      ctx.font = "bold 8.5px sans-serif";
      ctx.fillText(bTxt, bx + 4, trackTop + 12);
    }

    // Outline Border
    let borderCol = "rgba(255, 255, 255, 0.25)";
    if (isSelected) borderCol = "#38bdf8";
    else if (isRendering) borderCol = "rgba(56, 189, 248, 0.8)";
    else if (hasClip && isQueued) borderCol = "rgba(245, 158, 11, 0.75)";
    else if (hasClip) borderCol = "rgba(16, 185, 129, 0.55)";
    ctx.strokeStyle = borderCol;
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
  const trackHeight = Math.max(30, Math.round(60 * (state.zoom || 1)));
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
  const zoomSlider = document.getElementById("timelineZoom");

  if (zoomSlider) {
    zoomSlider.min = "1.0";
    zoomSlider.max = "3.5";
    zoomSlider.step = "0.25";
    zoomSlider.value = String(state.zoom || 1.0);

    zoomSlider.addEventListener("input", (e) => {
      const val = parseFloat(e.target.value);
      state.zoom = Math.max(1.0, isNaN(val) ? 1.0 : val);
      redrawTimeline();
      updatePlayheadVisual(state.playheadTime || audioEl.currentTime);
    });
  }

  // Top ruler & timeline positioning / scrubbing / boundary dragging / right-click drag scrolling
  if (scrollContainer) {
    let scrollRafId = null;
    scrollContainer.addEventListener("scroll", () => {
      if (!scrollRafId) {
        scrollRafId = requestAnimationFrame(() => {
          scrollRafId = null;
          redrawTimeline();
        });
      }
    });
    scrollContainer.addEventListener("contextmenu", (e) => e.preventDefault());
    scrollContainer.addEventListener("mousedown", (e) => {
      if (e.button === 2 && e.target !== canvas) {
        isRightClickDragging = true;
        rightClickStartX = e.clientX;
        rightClickStartScrollLeft = scrollContainer.scrollLeft;
        scrollContainer.style.cursor = "grabbing";
        canvas.style.cursor = "grabbing";
        e.preventDefault();
      }
    });
  }
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());

  canvas.addEventListener("mousedown", (e) => {
    if (e.button === 2) {
      // Right-click: start timeline drag scrolling
      isRightClickDragging = true;
      rightClickStartX = e.clientX;
      rightClickStartScrollLeft = scrollContainer ? scrollContainer.scrollLeft : 0;
      canvas.style.cursor = "grabbing";
      if (scrollContainer) scrollContainer.style.cursor = "grabbing";
      e.preventDefault();
      return;
    }

    if (e.button !== 0) return; // Left click only
    const rect = canvas.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const clickY = e.clientY - rect.top;
    const pps = state.pixelsPerSecond * state.zoom;
    const clickTime = Math.max(0, Math.min(clickX / pps, state.audioDuration || 60));

    // 0. Check if clicked on a draggable scene boundary
    const boundaryIdx = findHoveredBoundary(clickX, clickY);
    if (boundaryIdx !== null) {
      if (typeof window.beginEditSession === "function") {
        const sc = state.projectData?.scenes?.[boundaryIdx];
        window.beginEditSession(`boundary:${boundaryIdx}`, `Drag Scene Boundary ${sc ? sc.id : ""}`.trim());
      }
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

    // 2. Check if clicked inside a scene block
    const curTrackBottom = 84 + Math.max(30, Math.round(60 * (state.zoom || 1)));
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

  // Smooth scrubbing, boundary dragging & right-click panning while mouse moves across window
  window.addEventListener("mousemove", (e) => {
    if (isRightClickDragging && scrollContainer) {
      const dx = e.clientX - rightClickStartX;
      scrollContainer.scrollLeft = rightClickStartScrollLeft - dx;
      return;
    }

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
    if (isRightClickDragging) {
      isRightClickDragging = false;
      canvas.style.cursor = "";
      if (scrollContainer) scrollContainer.style.cursor = "";
    }
    if (state.isDraggingBoundary) {
      state.isDraggingBoundary = false;
      state.draggedBoundaryIdx = null;
      redrawTimeline();
      if (typeof window.commitEditSession === "function") {
        window.commitEditSession();
      }
    }
    if (isScrubbingRuler) {
      isScrubbingRuler = false;
    }
  });

  // Dynamic hover cursor over timeline canvas
  canvas.addEventListener("mousemove", (e) => {
    if (isRightClickDragging) {
      canvas.style.cursor = "grabbing";
      return;
    }
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

    const curTrackBottom = 84 + Math.max(30, Math.round(60 * (state.zoom || 1)));
    canvas.style.cursor = (my <= 24 || (my >= 84 && my <= curTrackBottom)) ? "pointer" : "crosshair";
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
  document.getElementById("btnAddMarker")?.addEventListener("click", addMarkerAtPlayhead);

  window.addEventListener("resize", () => {
    redrawTimeline();
    updatePlayheadVisual(state.playheadTime || audioEl.currentTime);
  });
}

