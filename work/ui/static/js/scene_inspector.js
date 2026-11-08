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
  markProjectDirty();
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
  markProjectDirty();
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
  markProjectDirty();
});

document.getElementById("btnRandomSeedM")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const seed = Math.floor(Math.random() * 90000) + 1000;
  state.projectData.scenes[state.selectedSceneIdx].seed_m = seed;
  document.getElementById("sceneSeedM").value = seed;
  markProjectDirty();
});

document.getElementById("btnRandomSeedF")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  const seed = Math.floor(Math.random() * 90000) + 1000;
  state.projectData.scenes[state.selectedSceneIdx].seed_f = seed;
  document.getElementById("sceneSeedF").value = seed;
  markProjectDirty();
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
    } else {
      // Deleted trailing scene: extend new last scene to fill timeline to total_duration
      const lastScene = scenes[scenes.length - 1];
      const totalDur = state.projectData?.project?.total_duration;
      if (typeof totalDur === "number" && totalDur > lastScene.start) {
        lastScene.end = parseFloat(totalDur.toFixed(2));
      } else if (sc && sc.end > lastScene.start) {
        lastScene.end = parseFloat(Number(sc.end).toFixed(2));
      }
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

// Split Scene Function & Buttons
function splitScene(idx, targetTime = null) {
  const scenes = state.projectData?.scenes || [];
  if (idx === null || idx < 0 || idx >= scenes.length) return;
  const sc = scenes[idx];

  const start = parseFloat(sc.start);
  const end = parseFloat(sc.end);
  if (end - start < 0.2) {
    showToast("Scene is too short to split (< 0.2s)");
    return;
  }

  // Determine default split time:
  // If targetTime provided and inside (start, end), use it.
  // Else if audioEl.currentTime is inside (start, end), use it.
  // Otherwise use the exact midpoint.
  let defaultTime;
  if (typeof targetTime === "number" && targetTime > start + 0.05 && targetTime < end - 0.05) {
    defaultTime = targetTime;
  } else if (audioEl && audioEl.currentTime > start + 0.05 && audioEl.currentTime < end - 0.05) {
    defaultTime = audioEl.currentTime;
  } else {
    defaultTime = (start + end) / 2.0;
  }

  const promptMsg = `✂️ Split Scene "${sc.id}" (${start.toFixed(2)}s - ${end.toFixed(2)}s)\n\nEnter split timestamp in seconds (between ${start.toFixed(2)}s and ${end.toFixed(2)}s):`;
  const timeInput = prompt(promptMsg, defaultTime.toFixed(2));
  if (timeInput === null) return; // user cancelled

  const splitTime = parseFloat(parseFloat(timeInput).toFixed(3));
  if (isNaN(splitTime) || splitTime <= start + 0.05 || splitTime >= end - 0.05) {
    showToast(`Invalid split time. Must be strictly between ${(start + 0.05).toFixed(2)}s and ${(end - 0.05).toFixed(2)}s.`);
    return;
  }

  // Derive new IDs
  const cleanId = String(sc.id || "S01").trim();
  const existingIds = new Set(scenes.map((s) => String(s.id || "").trim().toUpperCase()));
  let id1 = cleanId + "a";
  let id2 = cleanId + "b";

  const mLetter = cleanId.match(/^(S0*\d+)([a-z])$/i);
  if (mLetter) {
    const prefix = mLetter[1];
    const curCode = mLetter[2].toLowerCase().charCodeAt(0);
    id1 = cleanId;
    for (let offset = 1; offset < 26; offset++) {
      const cand = prefix + String.fromCharCode(curCode + offset);
      if (!existingIds.has(cand.toUpperCase())) {
        id2 = cand;
        break;
      }
    }
  } else if (existingIds.has(id1.toUpperCase()) || existingIds.has(id2.toUpperCase())) {
    id1 = cleanId + "_part1";
    id2 = cleanId + "_part2";
  }

  // Clone scenes
  const scene1 = JSON.parse(JSON.stringify(sc));
  const scene2 = JSON.parse(JSON.stringify(sc));

  scene1.id = id1;
  scene1.start = start;
  scene1.end = splitTime;

  scene2.id = id2;
  scene2.start = splitTime;
  scene2.end = end;

  if (sc.title) {
    scene1.title = `${sc.title} (Part 1)`;
    scene2.title = `${sc.title} (Part 2)`;
  }

  if (sc.output_name) {
    if (sc.output_name.startsWith(sc.id)) {
      scene1.output_name = id1 + sc.output_name.slice(sc.id.length);
      scene2.output_name = id2 + sc.output_name.slice(sc.id.length);
    } else {
      scene1.output_name = `${sc.output_name}_${id1}`;
      scene2.output_name = `${sc.output_name}_${id2}`;
    }
  }

  // Increment seeds in second part so the two clips get unique latents
  ["seed", "seed_m", "seed_f"].forEach((k) => {
    if (typeof scene2[k] === "number") {
      scene2[k] += 1;
    }
  });

  // Replace original scene with the two new split scenes
  scenes.splice(idx, 1, scene1, scene2);

  markProjectDirty();
  redrawTimeline();
  selectScene(idx + 1); // select the new second segment

  showToast(`✂️ Split Scene ${sc.id} into ${id1} (${start.toFixed(2)}s-${splitTime.toFixed(2)}s) and ${id2} (${splitTime.toFixed(2)}s-${end.toFixed(2)}s)`);
}

document.getElementById("btnSplitScene")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) return;
  splitScene(state.selectedSceneIdx);
});

document.getElementById("btnSplitSceneHeader")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) {
    showToast("Select a scene to split first");
    return;
  }
  splitScene(state.selectedSceneIdx);
});

