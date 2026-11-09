function isTwoPassMode(mode) {
  return ["duet_method_b", "two_pass_gva", "gva_male_left_silent_right"].includes(mode);
}

function isTwoPassScene(sc) {
  return !sc ? false : isTwoPassMode(sc.mode) || (!sc.mode && (sc.prompt_m !== undefined || sc.prompt_f !== undefined));
}

function getDefaultSinglePrompt(mode) {
  if (mode === "female_solo") return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. {FEMALE_STUDIO_DESC} leans into the microphone with open eyes and natural posture, singing with expressive, synchronized lip movements.\noverall_soundscape: Studio room tone under the vocal performance.\nnon_diegetic_music: N/A";
  if (mode === "silent_locked") return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Atmospheric continuous shot across the studio floor. The performers and instruments remain in place with subtle natural breathing and room movement, with no singing or dialogue.\noverall_soundscape: Quiet studio room tone and ambient atmosphere.\nnon_diegetic_music: N/A";
  return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. {MALE_STUDIO_DESC} leans into the brass microphone with open eyes and natural sway, singing an emotional phrase with synchronized lip movements.\noverall_soundscape: Studio room tone under the vocal performance.\nnon_diegetic_music: N/A";
}

function getDefaultPass1Prompt(mode) {
  return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A seamless, continuous wide shot across the studio floor. On the left side inside his circle of light, {MALE_STUDIO_DESC} at the brass microphone sings with open eyes, natural body movement, and synchronized lip movements, while the partner on the right listens quietly.\noverall_soundscape: Studio room tone and male vocal.\nnon_diegetic_music: N/A";
}

function getDefaultPass2Prompt(mode) {
  if (mode === "gva_male_left_silent_right") return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A seamless, continuous wide shot across the studio floor. On the right side, {FEMALE_STUDIO_DESC} keeps her mouth completely closed and listens quietly, reacting with subtle eye and head movements.\noverall_soundscape: Quiet studio room tone, no speech.\nnon_diegetic_music: N/A";
  return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\nintegrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A seamless, continuous wide shot across the studio floor. On the right side, {FEMALE_STUDIO_DESC} sings her vocal harmony part with open eyes and expressive, synchronized lip movements.\noverall_soundscape: Studio room tone and vocal performance.\nnon_diegetic_music: N/A";
}

function setSelectValueSafely(selectEl, value, defaultValue) {
  if (!selectEl) return;
  const targetVal = value || defaultValue;
  if (!targetVal) return;
  const exists = Array.from(selectEl.options).some((opt) => opt.value === targetVal || opt.value.toLowerCase() === targetVal.toLowerCase());
  if (!exists) {
    const opt = document.createElement("option");
    opt.value = targetVal;
    opt.textContent = `${targetVal} (custom)`;
    selectEl.appendChild(opt);
  }
  selectEl.value = targetVal;
}

function populateSceneVideoRenderer(sc) {
  const sel = document.getElementById("sceneVideoRenderer");
  if (!sel) return;
  sel.innerHTML = "";
  const defaultId = String(state.projectData?.models?.default_video_renderer || 1);
  const vModels = state.models?.video_renderers || {};
  const defaultModelName = vModels[defaultId]?.name || `Model #${defaultId}`;
  sel.add(new Option(`Default (Project: [#${defaultId}] ${defaultModelName})`, ""));
  Object.keys(vModels).forEach((k) => {
    const isRef = vModels[k]?.supports_image_refs || String(vModels[k]?.frames_formula || "").includes("ref2va");
    sel.add(new Option(`[#${k}] ${vModels[k].name || vModels[k].model_type}${isRef ? " [Ref2VA]" : ""}`, k));
  });
  const curVal = sc.renderer !== null && sc.renderer !== undefined ? String(sc.renderer) : "";
  sel.value = curVal;
  const badge = document.getElementById("sceneRendererBadge");
  if (badge) badge.style.display = curVal ? "inline-block" : "none";
}

function updateSceneInspectorInputs(sc) {
  const isTwoPass = isTwoPassScene(sc);
  const showDuet = isTwoPass ? "block" : "none";
  const showSingle = isTwoPass ? "none" : "block";
  const el = (id) => document.getElementById(id);
  ["singlePromptGroup", "singleSeedGroup", "singleStemGroup"].forEach((id) => { if (el(id)) el(id).style.display = showSingle; });
  if (el("duetPromptsGroup")) el("duetPromptsGroup").style.display = showDuet;

  if (isTwoPass) {
    if (el("scenePromptM")) el("scenePromptM").value = sc.prompt_m || "";
    if (el("scenePromptF")) el("scenePromptF").value = sc.prompt_f || "";
    if (el("sceneSeedM")) el("sceneSeedM").value = sc.seed_m !== undefined ? sc.seed_m : 8201;
    if (el("sceneSeedF")) el("sceneSeedF").value = sc.seed_f !== undefined ? sc.seed_f : 8301;
    const sxVal = sc.split_x !== undefined ? sc.split_x : 0.48;
    if (el("sceneSplitX")) el("sceneSplitX").value = sxVal;
    if (el("sceneSplitXRange")) el("sceneSplitXRange").value = sxVal;
    if (el("sceneSplitXVal")) el("sceneSplitXVal").textContent = Number(sxVal).toFixed(2);
    setSelectValueSafely(el("sceneStemM"), sc.stem_m, "male");
    setSelectValueSafely(el("sceneStemF"), sc.stem_f, sc.mode === "gva_male_left_silent_right" ? "silent" : "female");
  } else {
    if (el("scenePrompt")) el("scenePrompt").value = sc.prompt || "";
    if (el("sceneSeed")) el("sceneSeed").value = sc.seed !== undefined ? sc.seed : 8201;
    setSelectValueSafely(el("sceneStem"), sc.stem, sc.mode === "silent_locked" ? "silent" : sc.mode === "female_solo" ? "female" : "male");
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
    if (!sc.prompt_m || !sc.prompt_m.trim()) sc.prompt_m = sc.prompt || getDefaultPass1Prompt(newMode);
    if (!sc.prompt_f || !sc.prompt_f.trim()) sc.prompt_f = getDefaultPass2Prompt(newMode);
    if (sc.seed_m === undefined) sc.seed_m = sc.seed !== undefined ? sc.seed : 8201;
    if (sc.seed_f === undefined) sc.seed_f = sc.seed !== undefined ? sc.seed + 100 : 8301;
    if (sc.split_x === undefined) sc.split_x = 0.48;
    sc.stem_m = "male";
    sc.stem_f = (newMode === "gva_male_left_silent_right") ? "silent" : "female";
    delete sc.prompt; delete sc.seed; delete sc.stem;
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

    delete sc.prompt_m; delete sc.prompt_f; delete sc.seed_m; delete sc.seed_f;
    delete sc.split_x; delete sc.stem_m; delete sc.stem_f;
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
  updateInspectorSceneHeader(sc);
  markProjectDirty();
  redrawTimeline();
}

function updateInspectorSceneHeader(sc) {
  const sceneIdEl = document.getElementById("inspectorSceneId");
  if (!sceneIdEl || !sc) return;
  const dur = Math.max(0, sc.end - sc.start).toFixed(2);
  const est = typeof window.estimateSceneRenderInfo === "function" ? window.estimateSceneRenderInfo(sc) : null;
  if (!est) {
    const fps = state.projectData?.project?.fps || 24;
    sceneIdEl.innerHTML = `<span style="font-size: 16px; font-weight: 700; color: var(--primary);">Scene ${escapeHtml(sc.id)}</span> <span style="font-size: 12px; font-weight: normal; color: var(--text-muted); margin-left: 10px;">${sc.start.toFixed(1)}s – ${sc.end.toFixed(1)}s &bull; ${dur}s (${Math.round(dur * fps)} frames)</span>`;
    return;
  }
  const padTotal = Number((est.head_pad + est.tail_pad).toFixed(2));
  const padNote = padTotal > 0 ? ` (+${padTotal}s pad)` : "";
  const isSpill = est.is_split_window;
  const isMultiWin = est.window_count > 1;
  const badgeBg = isMultiWin ? "rgba(239,68,68,0.18)" : isSpill ? "rgba(245,158,11,0.18)" : "rgba(56,189,248,0.14)";
  const badgeBorder = isMultiWin ? "rgba(239,68,68,0.5)" : isSpill ? "rgba(245,158,11,0.5)" : "rgba(56,189,248,0.35)";
  const badgeColor = isMultiWin ? "#fca5a5" : isSpill ? "#fcd34d" : "#7dd3fc";
  const saveNote = est.split_savings_seconds >= 120 ? ` — Split ≤9.3s to save ~${est.split_savings_formatted}` : "";
  const warnLabel = isMultiWin ? ` (${est.window_count} Windows${saveNote})` : isSpill ? ` (VRAM Spill${saveNote})` : "";
  const passDetail = est.is_two_pass ? `2 passes: ~${est.passes.map((p) => p.estimated_formatted).join(" + ~")}` : "1 pass";
  const tip = `Quantized Frames: ${est.quantized_frames}f (ceiling: ${est.next_quantum_max_sec}s — ${est.quantum_headroom_sec}s headroom before +${est.quantum_step || 17}f step)&#10;Effective Length: ${est.effective_seconds}s (${dur}s edit${padNote})&#10;Model: [#${est.model_id || 1}] ${escapeHtml(est.model_name || "Video Model")} (${passDetail})&#10;Windowing: ${est.window_count} window${est.window_count > 1 ? "s" : ""} (${est.regime} regime @ ${est.resolution})&#10;Estimated Render Time: ~${est.total_estimated_formatted}${est.split_savings_seconds >= 60 ? ` (Splitting in half saves ~${est.split_savings_formatted})` : ""}`;
  sceneIdEl.innerHTML = `<span style="font-size: 16px; font-weight: 700; color: var(--primary);">Scene ${escapeHtml(sc.id)}</span> <span style="font-size: 12px; font-weight: normal; color: var(--text-muted); margin-left: 10px;">${sc.start.toFixed(1)}s – ${sc.end.toFixed(1)}s${padNote} &bull; ${est.effective_seconds.toFixed(2)}s (${est.quantized_frames} frames)</span> <span title="${tip}" style="font-size: 11px; font-weight: 600; margin-left: 8px; padding: 2px 7px; border-radius: 4px; background: ${badgeBg}; border: 1px solid ${badgeBorder}; color: ${badgeColor}; cursor: help;">${isMultiWin ? "🚨" : isSpill ? "⚠️" : "⏱️"} Est: ~${est.total_estimated_formatted}${warnLabel}</span>`;
}
window.updateInspectorSceneHeader = updateInspectorSceneHeader;

function syncAnchorDropdown(sel, val) {
  if (!sel) return "";
  const targetAid = extractAnchorId(val);
  const matched = Array.from(sel.options).find((opt) => opt.value === val || (targetAid && opt.dataset.aid === targetAid));
  if (matched) {
    sel.value = matched.value;
    return matched.value;
  }
  if (val) {
    const opt = document.createElement("option");
    opt.value = val;
    opt.dataset.aid = targetAid;
    opt.textContent = `${targetAid ? `[${targetAid}] ` : ""}${val}`;
    sel.appendChild(opt);
    sel.value = val;
    return val;
  }
  sel.value = "";
  return "";
}

function selectScene(idx) {
  if (typeof window.commitEditSession === "function") {
    window.commitEditSession();
  }
  window.selectScene = selectScene;
  state.selectedSceneIdx = idx;
  redrawTimeline();

  const sc = state.projectData?.scenes?.[idx];
  if (!sc) return;

  try {
    updateInspectorSceneHeader(sc);
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
      sc.anchor = syncAnchorDropdown(anchorSel, sc.anchor);
    }
    updateSceneAnchorPreview(sc.anchor);

    const anchorEndSel = document.getElementById("sceneAnchorEnd");
    if (anchorEndSel) {
      syncAnchorDropdown(anchorEndSel, sc.anchor_end || "");
      const btnClear = document.getElementById("btnClearEndAnchor");
      if (btnClear) btnClear.style.display = sc.anchor_end ? "inline" : "none";
      const btnMatch = document.getElementById("btnMatchNextAnchor");
      if (btnMatch) btnMatch.style.display = state.projectData?.scenes?.[idx + 1]?.anchor ? "inline" : "none";
    }
    updateSceneEndAnchorPreview(sc.anchor_end || "");
    populateSceneVideoRenderer(sc);

    const startInput = document.getElementById("sceneStart");
    const endInput = document.getElementById("sceneEnd");
    if (startInput) startInput.value = sc.start;
    if (endInput) endInput.value = sc.end;

    const pStart = document.getElementById("scenePadStart"), pEnd = document.getElementById("scenePadEnd");
    const defPad = state.projectData?.project?.render_overlap_seconds ?? 0;
    if (pStart) { pStart.value = sc.render_pad_start ?? ""; pStart.placeholder = `Def (${defPad}s)`; }
    if (pEnd) { pEnd.value = sc.render_pad_end ?? ""; pEnd.placeholder = `Def (${sc.anchor_end ? 0 : defPad}s)`; }
    const btnR = document.getElementById("btnResetScenePadding");
    if (btnR) btnR.style.display = (sc.render_pad_start !== undefined || sc.render_pad_end !== undefined) ? "inline" : "none";

    // Populate prompt textareas, seeds, and stem according to active mode
    updateSceneInspectorInputs(sc);

    // Update Clip Preview Video
    updateSceneClipPreview(sc);

    // Markers
    updateSceneMarkersBar(sc);
    if (window.updateSceneTakesUI) window.updateSceneTakesUI(sc);
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
  const qItem = (state.renderQueue || []).find((t) => (t.scene_id || "").toUpperCase() === (sc.id || "").toUpperCase());
  const isQueued = qItem && (qItem.status === "queued" || qItem.needs_render);
  const isRendering = qItem && qItem.status === "rendering";

  if (matchedKey) {
    if (badge) {
      if (isRendering) { badge.textContent = "Rendering..."; badge.style.background = "#38bdf8"; }
      else if (isQueued) { badge.textContent = `Queued (${qItem.reason || "Changed"})`; badge.style.background = "#f59e0b"; }
      else { badge.textContent = "Rendered"; badge.style.background = "#10b981"; }
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
      if (isQueued) {
        meta.innerHTML = `<div style="color: #f59e0b; font-weight: 600; margin-bottom: 2px;">Clip on disk is outdated (${qItem.reason || "Settings/Timeline changed"})</div>
          <div>Queued for rendering. Click <strong>▶ Run</strong> in the <strong>🚀 Render Console</strong> tab to render.</div>`;
      } else {
        meta.innerHTML = `<div style="color: var(--text-primary); font-weight: 600; margin-bottom: 2px;">Clip ready on disk</div>
          <div>Automatically syncs with the timeline scrubber and audio playback.</div>`;
      }
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
        <div>Save changes and click <strong>▶ Run</strong> in the <strong>🚀 Render Console</strong> tab to generate this clip.</div>`;
    }
  }
}

function resolveAnchorDetails(anchorVal) {
  if (!anchorVal) return null;
  const aid = extractAnchorId(anchorVal);
  const diskAnchors = state.assets?.anchors || {};
  let diskInfo = diskAnchors[anchorVal] || (aid ? diskAnchors[aid] : null);
  if (!diskInfo && aid) {
    const matchedKey = Object.keys(diskAnchors).find(
      (k) => extractAnchorId(k) === aid || k.startsWith(`${aid} `) || k.startsWith(`${aid}_`) || k.startsWith(`${aid}-`)
    );
    if (matchedKey) diskInfo = diskAnchors[matchedKey];
  }
  const projAnchor = (state.projectData?.anchors || []).find((a) => extractAnchorId(a.filename) === aid);
  const resolved = diskInfo?.filename || projAnchor?.filename || anchorVal || aid;
  if (!resolved) return null;
  const anchorMtime = diskInfo?.mtime || Date.now();
  const url = `/media/${state.currentSlug}/anchor/${encodeURIComponent(resolved)}?t=${anchorMtime}`;
  return { aid, resolved, url };
}

function updateSceneAnchorPreview(anchorVal) {
  const info = resolveAnchorDetails(anchorVal);
  const triggerThumb = document.getElementById("anchorTriggerThumb");
  const triggerAid = document.getElementById("anchorTriggerAid");
  const triggerName = document.getElementById("anchorTriggerName");

  if (info) {
    if (triggerThumb) { triggerThumb.src = info.url; triggerThumb.style.display = "block"; }
    if (triggerAid) triggerAid.textContent = info.aid ? `[${info.aid}]` : "";
    if (triggerName) triggerName.textContent = info.resolved;
  } else {
    if (triggerThumb) triggerThumb.style.display = "none";
    if (triggerAid) triggerAid.textContent = "";
    if (triggerName) triggerName.textContent = "Select Anchor...";
  }

  document.querySelectorAll("#anchorDropdownMenu .anchor-dropdown-item").forEach((item) => {
    item.classList.toggle("selected", item.dataset.value === anchorVal || (info?.aid && item.dataset.aid === info.aid));
  });
}

function updateSceneEndAnchorPreview(anchorVal) {
  const info = resolveAnchorDetails(anchorVal);
  const triggerThumb = document.getElementById("endAnchorTriggerThumb");
  const triggerPlaceholder = document.getElementById("endAnchorTriggerPlaceholder");
  const triggerAid = document.getElementById("endAnchorTriggerAid");
  const triggerName = document.getElementById("endAnchorTriggerName");

  if (info) {
    if (triggerThumb) { triggerThumb.src = info.url; triggerThumb.style.display = "block"; }
    if (triggerPlaceholder) triggerPlaceholder.style.display = "none";
    if (triggerAid) triggerAid.textContent = info.aid ? `[${info.aid}]` : "";
    if (triggerName) triggerName.textContent = info.resolved;
  } else {
    if (triggerThumb) triggerThumb.style.display = "none";
    if (triggerPlaceholder) triggerPlaceholder.style.display = "flex";
    if (triggerAid) triggerAid.textContent = "";
    if (triggerName) triggerName.textContent = "(None / Single Anchor)";
  }

  document.querySelectorAll("#endAnchorDropdownMenu .anchor-dropdown-item").forEach((item) => {
    item.classList.toggle("selected", item.dataset.value === (anchorVal || "") || (info?.aid && item.dataset.aid === info.aid));
  });
}
window.updateSceneEndAnchorPreview = updateSceneEndAnchorPreview;

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
  if (state.selectedSceneIdx === sceneIdx && startInput && document.activeElement !== startInput) startInput.value = sc.start;
  const prevEndInput = document.getElementById("sceneEnd");
  if (sceneIdx > 0 && state.selectedSceneIdx === sceneIdx - 1 && prevEndInput && document.activeElement !== prevEndInput) prevEndInput.value = scenes[sceneIdx - 1].end;
  if (state.selectedSceneIdx !== null && scenes[state.selectedSceneIdx]) updateInspectorSceneHeader(scenes[state.selectedSceneIdx]);
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
  if (state.selectedSceneIdx === sceneIdx && endInput && document.activeElement !== endInput) endInput.value = sc.end;
  const nextStartInput = document.getElementById("sceneStart");
  if (sceneIdx < scenes.length - 1 && state.selectedSceneIdx === sceneIdx + 1 && nextStartInput && document.activeElement !== nextStartInput) nextStartInput.value = scenes[sceneIdx + 1].start;
  if (state.selectedSceneIdx !== null && scenes[state.selectedSceneIdx]) updateInspectorSceneHeader(scenes[state.selectedSceneIdx]);
  markProjectDirty();
}

// Bind inspector form changes to in-memory state & focus sessions
const textInputLabels = {
  scenePrompt: "Prompt",
  scenePromptM: "Pass 1 Prompt",
  scenePromptF: "Pass 2 Prompt",
  sceneStart: "Start",
  sceneEnd: "End",
  scenePadStart: "Head Padding",
  scenePadEnd: "Tail Padding",
  sceneSeed: "Seed",
  sceneSeedM: "Seed Pass 1",
  sceneSeedF: "Seed Pass 2",
  sceneSplitX: "Split X",
  sceneSplitXRange: "Split Slider",
};

Object.entries(textInputLabels).forEach(([id, label]) => {
  const el = document.getElementById(id);
  if (!el) return;

  el.addEventListener("focus", () => {
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    window.beginEditSession?.(`inspector:${id}`, `Edit ${sc ? sc.id : "Scene"} ${label}`);
  });

  el.addEventListener("input", (e) => {
    if (state.selectedSceneIdx === null) return;
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (!sc) return;
    window.beginEditSession?.(`inspector:${id}`, `Edit ${sc.id} ${label}`);

    const val = e.target.value;
    if (id === "scenePrompt") { sc.prompt = val; autoResizeTextarea(e.target); }
    else if (id === "scenePromptM") { sc.prompt_m = val; autoResizeTextarea(e.target); }
    else if (id === "scenePromptF") { sc.prompt_f = val; autoResizeTextarea(e.target); }
    else if (id === "sceneStart") { updateSceneStart(state.selectedSceneIdx, parseFloat(val) || 0); updateSceneMarkersBar(sc); updatePromptResolvedPreviews(sc); }
    else if (id === "sceneEnd") { updateSceneEnd(state.selectedSceneIdx, parseFloat(val) || 0); updateSceneMarkersBar(sc); updatePromptResolvedPreviews(sc); }
    else if (id === "scenePadStart" || id === "scenePadEnd") {
      const prop = id === "scenePadStart" ? "render_pad_start" : "render_pad_end";
      if (val === "" || val === null) delete sc[prop];
      else sc[prop] = Math.max(0, parseFloat(val) || 0);
      const btnR = document.getElementById("btnResetScenePadding");
      if (btnR) btnR.style.display = (sc.render_pad_start !== undefined || sc.render_pad_end !== undefined) ? "inline" : "none";
      updateSceneMarkersBar(sc); updatePromptResolvedPreviews(sc); updateInspectorSceneHeader(sc);
    }
    else if (id === "sceneSeed") sc.seed = parseInt(val) || 1000;
    else if (id === "sceneSeedM") sc.seed_m = parseInt(val) || 1000;
    else if (id === "sceneSeedF") sc.seed_f = parseInt(val) || 1000;
    else if (id === "sceneSplitX" || id === "sceneSplitXRange") {
      sc.split_x = parseFloat(val) || 0.50;
      const otherId = id === "sceneSplitX" ? "sceneSplitXRange" : "sceneSplitX";
      const otherEl = document.getElementById(otherId);
      if (otherEl && document.activeElement !== otherEl) otherEl.value = sc.split_x;
      const lbl = document.getElementById("sceneSplitXVal");
      if (lbl) lbl.textContent = Number(sc.split_x).toFixed(2);
    }
    markProjectDirty();
    redrawTimeline();
  });

  el.addEventListener("blur", () => {
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (sc) {
      if (id === "sceneStart") el.value = sc.start;
      if (id === "sceneEnd") el.value = sc.end;
    }
    window.commitEditSession?.();
  });
});

const selectLabels = {
  sceneMode: "Mode",
  sceneAnchor: "Anchor",
  sceneAnchorEnd: "End Anchor",
  sceneVideoRenderer: "Video Renderer",
  sceneStem: "Stem",
  sceneStemM: "Stem Pass 1",
  sceneStemF: "Stem Pass 2",
};

Object.entries(selectLabels).forEach(([id, label]) => {
  const el = document.getElementById(id);
  if (!el) return;

  el.addEventListener("change", (e) => {
    if (state.selectedSceneIdx === null) return;
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (!sc) return;

    const val = e.target.value;
    if (id === "sceneMode") handleSceneModeChange(sc, val);
    else if (id === "sceneAnchor") { sc.anchor = val; updateSceneAnchorPreview(sc.anchor); updatePromptResolvedPreviews(sc); }
    else if (id === "sceneAnchorEnd") {
      if (val) sc.anchor_end = val; else delete sc.anchor_end;
      const btnClear = document.getElementById("btnClearEndAnchor");
      if (btnClear) btnClear.style.display = sc.anchor_end ? "inline" : "none";
      const pe = document.getElementById("scenePadEnd");
      if (pe) pe.placeholder = `Def (${sc.anchor_end ? 0 : (state.projectData?.project?.render_overlap_seconds ?? 0)}s)`;
      updateSceneEndAnchorPreview(sc.anchor_end || "");
      updatePromptResolvedPreviews(sc);
      updateInspectorSceneHeader(sc);
    }
    else if (id === "sceneVideoRenderer") {
      sc.renderer = val ? (parseInt(val) || val) : null;
      const badge = document.getElementById("sceneRendererBadge");
      if (badge) badge.style.display = sc.renderer ? "inline-block" : "none";
      updateInspectorSceneHeader(sc);
    }
    else if (id === "sceneStem") sc.stem = val;
    else if (id === "sceneStemM") sc.stem_m = val;
    else if (id === "sceneStemF") sc.stem_f = val;
    markProjectDirty();
    redrawTimeline();
    window.recordHistorySnapshot?.(`Change ${sc.id} ${label}`);
  });
});

document.getElementById("btnClearEndAnchor")?.addEventListener("click", () => {
  const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
  if (!sc) return;
  delete sc.anchor_end;
  const sel = document.getElementById("sceneAnchorEnd"); if (sel) sel.value = "";
  document.getElementById("btnClearEndAnchor").style.display = "none";
  const pe = document.getElementById("scenePadEnd");
  if (pe) pe.placeholder = `Def (${state.projectData?.project?.render_overlap_seconds ?? 0}s)`;
  updateSceneEndAnchorPreview("");
  updatePromptResolvedPreviews(sc); updateInspectorSceneHeader(sc); markProjectDirty(); redrawTimeline();
  window.recordHistorySnapshot?.(`Clear ${sc.id} End Anchor`);
});
document.getElementById("btnResetScenePadding")?.addEventListener("click", () => {
  const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
  if (!sc) return;
  delete sc.render_pad_start; delete sc.render_pad_end;
  const ps = document.getElementById("scenePadStart"), pe = document.getElementById("scenePadEnd");
  if (ps) ps.value = ""; if (pe) pe.value = "";
  document.getElementById("btnResetScenePadding").style.display = "none";
  updateSceneMarkersBar(sc); updatePromptResolvedPreviews(sc); updateInspectorSceneHeader(sc); markProjectDirty(); redrawTimeline();
  window.recordHistorySnapshot?.(`Reset ${sc.id} Render Padding`);
});
document.getElementById("btnMatchNextAnchor")?.addEventListener("click", () => {
  const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
  const nextSc = state.projectData?.scenes?.[state.selectedSceneIdx + 1];
  if (!sc || !nextSc?.anchor) return;
  sc.anchor_end = syncAnchorDropdown(document.getElementById("sceneAnchorEnd"), nextSc.anchor);
  const btnClear = document.getElementById("btnClearEndAnchor"); if (btnClear) btnClear.style.display = sc.anchor_end ? "inline" : "none";
  const pe = document.getElementById("scenePadEnd");
  if (pe) pe.placeholder = `Def (${sc.anchor_end ? 0 : (state.projectData?.project?.render_overlap_seconds ?? 0)}s)`;
  updateSceneEndAnchorPreview(sc.anchor_end || "");
  updatePromptResolvedPreviews(sc); updateInspectorSceneHeader(sc); markProjectDirty(); redrawTimeline();
  window.recordHistorySnapshot?.(`Match ${sc.id} End Anchor to ${nextSc.id}`);
  showToast(`🔗 Linked ${sc.id} End Anchor to ${nextSc.id}'s Start Anchor`);
});

// Random Seed buttons
[
  { id: "btnRandomSeed", prop: "seed", input: "sceneSeed", label: "Seed" },
  { id: "btnRandomSeedM", prop: "seed_m", input: "sceneSeedM", label: "Seed Pass 1" },
  { id: "btnRandomSeedF", prop: "seed_f", input: "sceneSeedF", label: "Seed Pass 2" },
].forEach(({ id, prop, input, label }) => {
  document.getElementById(id)?.addEventListener("click", () => {
    if (state.selectedSceneIdx === null) return;
    const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
    if (!sc) return;
    const seed = Math.floor(Math.random() * 90000) + 1000;
    sc[prop] = seed;
    const el = document.getElementById(input); if (el) el.value = seed;
    markProjectDirty();
    if (typeof window.recordHistorySnapshot === "function") window.recordHistorySnapshot(`Randomize ${sc.id} ${label}`);
  });
});

// Play Scene Audio Only
document.getElementById("btnPlaySceneAudio")?.addEventListener("click", () => {
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

// Add Scene Button
document.getElementById("btnAddScene")?.addEventListener("click", () => {
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
  if (typeof window.recordHistorySnapshot === "function") {
    window.recordHistorySnapshot(`Add Scene ${id}`);
  }
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

  scenes.splice(idx, 1);
  if (scenes.length > 0) {
    if (idx === 0) {
      scenes[0].start = 0.00;
    } else if (idx < scenes.length) {
      scenes[idx - 1].end = scenes[idx].start;
    } else {
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
  if (typeof window.recordHistorySnapshot === "function") {
    window.recordHistorySnapshot(`Delete Scene ${sc.id}`);
  }

  if (scenes.length === 0) {
    state.selectedSceneIdx = null;
    ["inspectorActions", "inspectorForm"].forEach(id => { const el = document.getElementById(id); if (el) el.style.display = "none"; });
    const emptyEl = document.getElementById("inspectorEmptyState"); if (emptyEl) emptyEl.style.display = "block";
    const titleEl = document.getElementById("inspectorSceneId"); if (titleEl) titleEl.textContent = "No Scenes";
  } else {
    selectScene(Math.min(idx, scenes.length - 1));
  }

  showToast(`🗑️ Deleted Scene ${sc.id}`);
}

document.getElementById("btnDeleteScene")?.addEventListener("click", () => {
  if (state.selectedSceneIdx !== null) deleteScene(state.selectedSceneIdx);
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

  let defaultTime = (typeof targetTime === "number" && targetTime > start + 0.05 && targetTime < end - 0.05) ? targetTime :
    (audioEl && audioEl.currentTime > start + 0.05 && audioEl.currentTime < end - 0.05) ? audioEl.currentTime : (start + end) / 2.0;

  const promptMsg = `✂️ Split Scene "${sc.id}" (${start.toFixed(2)}s - ${end.toFixed(2)}s)\n\nEnter split timestamp in seconds (between ${start.toFixed(2)}s and ${end.toFixed(2)}s):`;
  const timeInput = prompt(promptMsg, defaultTime.toFixed(2));
  if (timeInput === null) return;

  const splitTime = parseFloat(parseFloat(timeInput).toFixed(3));
  if (isNaN(splitTime) || splitTime <= start + 0.05 || splitTime >= end - 0.05) {
    showToast(`Invalid split time. Must be strictly between ${(start + 0.05).toFixed(2)}s and ${(end - 0.05).toFixed(2)}s.`);
    return;
  }

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

  const scene1 = JSON.parse(JSON.stringify(sc));
  const scene2 = JSON.parse(JSON.stringify(sc));
  delete scene1.anchor_end;
  ["render_pad_end", "pad_end", "tail_overlap", "render_overlap"].forEach((k) => delete scene1[k]);
  ["render_pad_start", "pad_start", "head_overlap", "render_overlap"].forEach((k) => delete scene2[k]);
  scene1.id = id1; scene1.start = start; scene1.end = splitTime;
  scene2.id = id2; scene2.start = splitTime; scene2.end = end;
  if (sc.title) { scene1.title = `${sc.title} (Part 1)`; scene2.title = `${sc.title} (Part 2)`; }
  if (sc.output_name) {
    scene1.output_name = sc.output_name.startsWith(sc.id) ? id1 + sc.output_name.slice(sc.id.length) : `${sc.output_name}_${id1}`;
    scene2.output_name = sc.output_name.startsWith(sc.id) ? id2 + sc.output_name.slice(sc.id.length) : `${sc.output_name}_${id2}`;
  }
  ["seed", "seed_m", "seed_f"].forEach((k) => {
    if (typeof scene2[k] === "number") scene2[k] += 1;
  });

  scenes.splice(idx, 1, scene1, scene2);
  markProjectDirty();
  redrawTimeline();
  selectScene(idx + 1);
  if (typeof window.recordHistorySnapshot === "function") {
    window.recordHistorySnapshot(`Split Scene ${sc.id} into ${id1} & ${id2}`);
  }
  showToast(`✂️ Split Scene ${sc.id} into ${id1} (${start.toFixed(2)}s-${splitTime.toFixed(2)}s) and ${id2} (${splitTime.toFixed(2)}s-${end.toFixed(2)}s)`);
}

document.getElementById("btnSplitSceneHeader")?.addEventListener("click", () => {
  if (state.selectedSceneIdx === null) showToast("Select a scene to split first");
  else splitScene(state.selectedSceneIdx);
});

