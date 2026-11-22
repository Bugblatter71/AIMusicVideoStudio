// ============================================================================
// Master Video Assembly, Video Player & Tab Navigation
// ============================================================================

function unloadMasterVideoForStitch(phaseText = "🧵 Stitching Master Video...", detailText = "Unloading video player & preparing scenes...") {
  const wasStitching = Boolean(state.isStitchingMaster);
  state.isStitchingMaster = true;
  const player = document.getElementById("masterVideoPlayer");
  if (player) {
    if (!wasStitching) {
      state._masterWasPlaying = !player.paused;
      state._masterPrevTime = Number(player.currentTime) || 0;
    }
    try { player.pause?.(); } catch (e) {}
    player.removeAttribute("src");
    player.removeAttribute("data-loaded-src");
    try { player.load?.(); } catch (e) {}
    player.style.display = "none";
  }
  const ov = document.getElementById("masterStitchOverlay");
  if (ov) ov.style.display = "flex";
  const ph = document.getElementById("masterStitchPhase"), dt = document.getElementById("masterStitchDetail");
  const bar = document.getElementById("masterStitchBar"), pct = document.getElementById("masterStitchPct");
  if (ph) ph.textContent = phaseText;
  if (dt) dt.textContent = detailText;
  if (bar) bar.style.width = "2%";
  if (pct) pct.textContent = "0%";
  const badge = document.getElementById("masterVideoBadge"), meta = document.getElementById("masterVideoMeta");
  if (badge) { badge.textContent = "Stitching..."; badge.style.background = "#2563eb"; }
  if (meta) meta.textContent = "Master video unloaded while stitching...";
  const btn = document.getElementById("btnStitchMasterFromTab"), refBtn = document.getElementById("btnRefreshMasterVideo");
  if (btn) { btn.dataset.origText = btn.dataset.origText || btn.textContent; btn.textContent = "⏳ Stitching..."; btn.disabled = true; }
  if (refBtn) refBtn.disabled = true;
}

function updateMasterStitchProgress(payload = {}) {
  if (!state.isStitchingMaster) unloadMasterVideoForStitch();
  const pct = Math.max(0, Math.min(100, Math.round(Number(payload.pct) || 0)));
  const ph = document.getElementById("masterStitchPhase"), dt = document.getElementById("masterStitchDetail");
  const bar = document.getElementById("masterStitchBar"), pctEl = document.getElementById("masterStitchPct");
  const badge = document.getElementById("masterVideoBadge");
  if (ph) ph.textContent = `🧵 ${payload.phase || "Stitching Master Video..."}`;
  if (dt) dt.textContent = payload.detail || "";
  if (bar) bar.style.width = `${pct}%`;
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (badge) { badge.textContent = `Stitching ${pct}%`; badge.style.background = "#2563eb"; }
}

async function finishMasterStitch(success, errMsg = "") {
  if (state._stitchPollTimer) { clearInterval(state._stitchPollTimer); state._stitchPollTimer = null; }
  state.isStitchingMaster = false;
  state._masterReloadNonce = Date.now();
  const ov = document.getElementById("masterStitchOverlay"), player = document.getElementById("masterVideoPlayer");
  if (ov) ov.style.display = "none";
  if (player) player.style.display = "block";
  const btn = document.getElementById("btnStitchMasterFromTab"), refBtn = document.getElementById("btnRefreshMasterVideo");
  if (btn) { btn.textContent = btn.dataset.origText || "🧵 Stitch Master Video"; btn.disabled = false; }
  if (refBtn) refBtn.disabled = false;
  if (success) {
    if (typeof refreshProjectAssets === "function") await refreshProjectAssets(true);
    updateMasterVideoUI(true);
  } else {
    updateMasterVideoUI(true);
    const meta = errMsg ? document.getElementById("masterVideoMeta") : null;
    if (meta) meta.textContent = errMsg;
  }
}

function handleMasterStitchSSE(text) {
  if (!text) return false;
  if (text.includes("[MASTER_STITCH_START]")) {
    unloadMasterVideoForStitch("🧵 Stitching Master Video...", "Unloading video player & starting stitch worker...");
    return true;
  }
  const progIdx = text.indexOf("[MASTER_STITCH_PROGRESS]");
  if (progIdx !== -1) {
    try { updateMasterStitchProgress(JSON.parse(text.slice(progIdx + 24).trim())); } catch (e) {}
    return true;
  }
  if (text.includes("[MASTER_UPDATED]")) {
    updateMasterStitchProgress({ pct: 100, phase: "Complete — Reloading video player...", detail: "Stitch finished" });
    finishMasterStitch(true);
    return true;
  }
  if (text.includes("[MASTER_LOCKED]")) { finishMasterStitch(false, "Master file locked by another player — close player and retry."); return true; }
  if (text.includes("[MASTER_ERROR]")) { finishMasterStitch(false, "Master video restitch failed — check console logs."); return true; }
  return false;
}

async function triggerImmediateRestitch() {
  if (state.isDirty) {
    showToast("💾 Auto-saving project changes before restitch...");
    if (!(await saveProjectData(false))) {
      alert("Auto-save failed; aborting restitch so unsaved changes are not ignored.");
      return;
    }
  }
  unloadMasterVideoForStitch("🧵 Stitching Master Video...", "Unloading video player & starting stitch worker...");
  showToast("🧵 Restitching master video now...");
  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) initLogStream();
  await new Promise((r) => setTimeout(r, 60));
  try {
    const slug = state.currentSlug || "all_or_nothing";
    const res = await fetch("/api/stitch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug }) });
    const json = await res.json();
    if (!json.success) {
      const err = `Stitch error: ${json.error || "Unknown"}`;
      showToast(err);
      await finishMasterStitch(false, err);
    } else if (typeof setInterval === "function" && typeof fetch === "function") {
      if (state._stitchPollTimer) clearInterval(state._stitchPollTimer);
      const startedAt = Date.now();
      let seenActive = false;
      state._stitchPollTimer = setInterval(async () => {
        if (!state.isStitchingMaster) { clearInterval(state._stitchPollTimer); state._stitchPollTimer = null; return; }
        try {
          const st = await (await fetch("/api/render/status")).json();
          if (st?.stitch_in_progress) { seenActive = true; if (st.stitch_progress) updateMasterStitchProgress(st.stitch_progress); }
          else if (seenActive || (Date.now() - startedAt > 3000)) { await finishMasterStitch(true); }
        } catch (e) {}
      }, 1000);
    }
  } catch (err) {
    showToast(`Failed to trigger restitch: ${err}`);
    await finishMasterStitch(false, `Failed to trigger restitch: ${err}`);
  }
}

function updateMasterResolutionInfoUI() {
  const p = state.projectData?.project || {};
  const m = state.projectData?.models || {};
  const resMap = { "1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088" };
  const renderResKey = String(m.default_video_resolution || "4");
  const renderRes = resMap[renderResKey] || (renderResKey.includes("x") ? renderResKey : "1344x768");
  const masterCfg = p.master_resolution || "match";
  const targetMaster = (!masterCfg || masterCfg === "match") ? renderRes : masterCfg;
  const algo = (p.master_scale_algorithm || "lanczos").toUpperCase();
  const sharpen = Number(p.master_sharpen || 0);

  const sel = document.getElementById("masterTabResSelect");
  if (sel && sel.value !== masterCfg) {
    sel.value = masterCfg;
  }
  const cfgSel = document.getElementById("cfgMasterResolution");
  if (cfgSel && cfgSel.value !== masterCfg) {
    cfgSel.value = masterCfg;
  }

  const infoBadge = document.getElementById("masterScaleInfoBadge");
  if (infoBadge) {
    if (targetMaster === renderRes) {
      infoBadge.textContent = `Default: ${renderRes} → Master: ${targetMaster} (1:1 Native)`;
    } else if (renderRes === "1920x1088" && targetMaster === "1920x1080") {
      infoBadge.textContent = `Default: 1920x1088 → Master: 1920x1080 (Center Crop 8px, 1:1 Pixels)`;
    } else {
      const sharpTxt = sharpen > 0 ? ` + Sharpen ${sharpen}` : "";
      infoBadge.textContent = `Default: ${renderRes} → Master: ${targetMaster} (${algo}${sharpTxt})`;
    }
  }
}
window.updateMasterResolutionInfoUI = updateMasterResolutionInfoUI;

document.getElementById("masterTabResSelect")?.addEventListener("change", (e) => {
  if (!state.projectData) return;
  const p = state.projectData.project || (state.projectData.project = {});
  p.master_resolution = e.target.value || "match";
  updateMasterResolutionInfoUI();
  markProjectDirty();
  if (typeof window.recordHistorySnapshot === "function") {
    window.recordHistorySnapshot(`Change Master Resolution to ${p.master_resolution}`);
  }
  showToast(`Master video resolution set to ${p.master_resolution === "match" ? "Match Render" : p.master_resolution}`);
});

function updateMasterVideoUI(forceReload = false) {
  updateMasterResolutionInfoUI();
  if (state.isStitchingMaster) return;
  const masters = state.assets?.master_videos || [];
  const player = document.getElementById("masterVideoPlayer");
  const meta = document.getElementById("masterVideoMeta");
  const badge = document.getElementById("masterVideoBadge");

  if (!player) return;

  if (masters.length > 0) {
    const latest = masters[0];
    const mtime = latest.mtime || "";
    const nonce = state._masterReloadNonce ? `r=${state._masterReloadNonce}` : "";
    const qs = [mtime ? `t=${mtime}` : "", nonce].filter(Boolean).join("&");
    const newSrc = `/media/${state.currentSlug}/master${qs ? `?${qs}` : ""}`;
    const currentSrc = player.getAttribute("data-loaded-src") || player.getAttribute("src") || "";

    if (currentSrc !== newSrc || forceReload) {
      const wasPlaying = Boolean(state._masterWasPlaying ?? !player.paused);
      const prevTime = Number(state._masterPrevTime ?? player.currentTime) || 0;
      delete state._masterWasPlaying;
      delete state._masterPrevTime;
      const applyResume = () => {
        if (prevTime > 0 && isFinite(prevTime)) {
          try { player.currentTime = prevTime; } catch (e) {}
        }
        if (wasPlaying) player.play?.().catch?.(() => {});
      };
      if (typeof player.addEventListener === "function") player.addEventListener("loadedmetadata", applyResume, { once: true });
      player.setAttribute("data-loaded-src", newSrc);
      player.src = newSrc;
      try { player.load?.(); } catch (e) {}
      if (typeof player.addEventListener !== "function") applyResume();
    }
    if (meta) meta.textContent = `File: ${latest.filename} (${latest.size_mb} MB)`;
    if (badge) { badge.textContent = "Available"; badge.style.background = "#10b981"; }
  } else {
    player.removeAttribute("src");
    player.removeAttribute("data-loaded-src");
    try { player.load?.(); } catch (e) {}
    if (meta) meta.textContent = "No master MP4 render generated yet on disk.";
    if (badge) { badge.textContent = "Missing"; badge.style.background = "#64748b"; }
  }
}

function switchTab(tabName) {
  const main = document.getElementById("appMain");
  const sidePane = document.getElementById("sidePane");
  const sidePaneTitle = document.getElementById("sidePaneTitle");

  const tabTitles = {
    scenes: "📋 Scenes — Starting Frames & Render Settings",
    characters: "👤 Characters & Reference Library",
    anchors: "🖼️ Scene Anchors",
    settings: "⚙️ Project & Model Settings",
    console: "🚀 Render Console",
    master: "📺 Master Video Player",
  };

  if (!main || !sidePane) return;

  if (tabName === "timeline") {
    // 1. Full Screen Timeline Mode
    main.classList.remove("layout-split", "layout-scenes-wide");
    main.classList.add("layout-full");
    sidePane.style.display = "none";

    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === "timeline");
    });
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
  } else {
    // 2. Tab Below Timeline: Full-width or Split View
    main.classList.remove("layout-full");
    main.classList.add("layout-split");
    main.classList.toggle("layout-scenes-wide", tabName === "scenes" && !state.scenesTabSplitMode);
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

    if (tabName === "scenes") {
      if (window.renderSceneList) window.renderSceneList(true);
    } else if (tabName === "characters") {
      if (window.renderCharactersTab) window.renderCharactersTab();
    } else if (tabName === "anchors") {
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

const LEGACY_1X_UPSAMPLE_VALS = ["h3facerefine", "seedvr2*1", "flashvsrplus*1", "flashvsrplus2pass*1", "ltx25_refine*1", "dlss5*1"];
const SPATIAL_UP_LABELS = {
  "": "None (Stage 3 FFmpeg Scale)", "h3_vae*2": "MiniMax H3 Detail VAE x2", "h3_vae*1.5": "MiniMax H3 Detail VAE x1.5",
  "flashvsrplus*2": "FlashVSR+ Turbo x2", "flashvsrplus2pass*2": "FlashVSR+ 2-Pass x2", "seedvr2*2": "SeedVR2 x2",
};
const AI_REFINER_OPTIONS = [
  ["none", "None (Disable)"], ["h3facerefine", "H3 Face Refine 1x"],
  ["seedvr2*1", "SeedVR2 1x Detail"], ["flashvsrplus*1", "FlashVSR+ 1x Polish"],
];

function getSceneOptionCatalog(sc) {
  sc = sc || {};
  const resDefs = state.models?.video_resolutions || state.models?.resolutions || {
    "1": { width: 832, height: 480, name: "480p Fast Preview" }, "2": { width: 960, height: 544, name: "540p qHD Balanced" },
    "3": { width: 1280, height: 720, name: "720p HD Standard" }, "4": { width: 1344, height: 768, name: "768p WXGA High-Res" },
    "5": { width: 1536, height: 864, name: "864p Near-1080p" }, "6": { width: 1920, height: 1088, name: "1080p Full HD Block-Aligned" },
  };
  const defResKey = String(state.projectData?.models?.default_video_resolution || "4");
  const defResObj = resDefs[defResKey];
  const defResLabel = defResObj ? `#${defResKey} ${defResObj.width || defResObj.resolution?.split("x")[0]}x${defResObj.height || defResObj.resolution?.split("x")[1]}` : defResKey;
  const vModels = state.models?.video_renderers || {}, fModels = state.models?.fal_video_renderers || {}, mmModels = state.models?.minimax_video_renderers || {};
  const defaultModelId = String(state.projectData?.models?.default_video_renderer || 1);
  const defaultModelName = (vModels[defaultModelId] || fModels[defaultModelId] || mmModels[defaultModelId])?.name || `Model #${defaultModelId}`;
  const isLegacy1x = (v) => LEGACY_1X_UPSAMPLE_VALS.includes(String(v || "").toLowerCase());
  const vShared = state.projectData?.models?.video_shared_settings || {};
  const rawDefUp = String(vShared.spatial_upsampling || "");
  const defUp = isLegacy1x(rawDefUp) ? "" : rawDefUp;
  const effKey = sc.resolution ? String(sc.resolution) : defResKey;
  const effResStr = resDefs[effKey]?.resolution || (resDefs[effKey]?.width ? `${resDefs[effKey].width}x${resDefs[effKey].height}` : effKey);
  const [effW, effH] = String(effResStr).toLowerCase().split("x").map((n) => parseInt(n, 10) || 0);
  const masterCfg = String(state.projectData?.project?.master_resolution || "match").toLowerCase();
  const [tgtW, tgtH] = (masterCfg && masterCfg !== "match" && masterCfg.includes("x")) ? masterCfg.split("x").map((n) => parseInt(n, 10) || 0) : [0, 0];
  const rawCurUp = sc.spatial_upsampling !== null && sc.spatial_upsampling !== undefined ? String(sc.spatial_upsampling) : "";
  const curUp = isLegacy1x(rawCurUp) ? "" : rawCurUp;
  const effectiveVal = curUp === "none" ? "" : (curUp || defUp);
  const isScale = effectiveVal.includes("*") && parseFloat(effectiveVal.split("*").pop()) > 1.0;
  const skipTarget = isScale && tgtW > 0 && effW >= tgtW && effH >= tgtH;
  const skipOom = isScale && effW >= 1536;

  const scF = sc.filters || {}, scFF = scF.ffmpeg || {}, scEq = scFF.eq || {};
  const shF = vShared.filters || {}, shRef = (shF.ai_refiners && shF.ai_refiners[0]) || (isLegacy1x(rawDefUp) ? rawDefUp : "");
  const scRefArr = scF.ai_refiners;
  const scRefVal = Array.isArray(scRefArr) ? (scRefArr.length === 0 ? "none" : String(scRefArr[0] || ""))
    : (typeof scRefArr === "string" && scRefArr ? scRefArr : (isLegacy1x(sc.spatial_upsampling) ? String(sc.spatial_upsampling) : ""));
  const effRef = scRefVal === "none" ? "" : (scRefVal || shRef);
  const hasAnyFilter = Boolean(effRef) || (scF.film_grain_intensity ?? shF.film_grain_intensity ?? 0) > 0
    || Boolean(scFF.deband ?? shF.ffmpeg?.deband) || (scFF.cas ?? shF.ffmpeg?.cas ?? 0) > 0
    || (scFF.denoise ?? shF.ffmpeg?.denoise ?? 0) > 0 || Object.keys(scEq).length > 0;
  const isCustomFilter = Boolean(scRefVal) || Object.keys(scF).length > 0;

  const stems = [], seenStems = new Set();
  const addStem = (v, lbl) => { if (v && !seenStems.has(v.toLowerCase())) { seenStems.add(v.toLowerCase()); stems.push({ value: v, label: lbl || v }); } };
  const cfgStems = state.projectData?.project?.stems || {};
  Object.keys(cfgStems).forEach((k) => addStem(k, `${k} (${cfgStems[k].split("/").pop()})`));
  addStem("male", "male (male_vocal.mp3)");
  addStem("female", "female (female_vocal.mp3)");
  const masterAudio = (state.projectData?.project?.master_audio || "").split("/").pop();
  (state.assets?.audio_files || []).forEach((fn) => {
    if (fn !== masterAudio && !Object.values(cfgStems).some((p) => p.endsWith(fn))) addStem(fn.replace(/\.[^/.]+$/, ""), `${fn.replace(/\.[^/.]+$/, "")} (${fn})`);
  });
  addStem("master", "full audio (master)");
  addStem("silent", "silent (no lip sync)");

  return {
    resDefs, defResKey, defResLabel, vModels, fModels, mmModels, defaultModelId, defaultModelName,
    upLabels: SPATIAL_UP_LABELS, aiRefinerOptions: AI_REFINER_OPTIONS,
    defUp, curUp, skipOom, skipTarget, effResStr,
    scF, scFF, scEq, shRef, scRefVal, effRef, hasAnyFilter, isCustomFilter, stems,
  };
}

// Scene-level Resolution & Spatial Upsampling Inspector Controls
function populateSceneResolutionAndUpsampling(sc) {
  const cat = getSceneOptionCatalog(sc);
  const resSel = document.getElementById("sceneResolution");
  if (resSel) {
    resSel.innerHTML = "";
    resSel.add(new Option(`Default (Project: ${cat.defResLabel})`, ""));
    Object.keys(cat.resDefs).forEach((k) => {
      const r = cat.resDefs[k], dim = r.resolution || `${r.width}x${r.height}`;
      resSel.add(new Option(`#${k}: ${dim} — ${r.name || ""}`, k));
    });
    const curRes = sc.resolution !== null && sc.resolution !== undefined ? String(sc.resolution) : "";
    if (curRes && !Array.from(resSel.options).some((o) => o.value === curRes)) resSel.add(new Option(`${curRes} (custom)`, curRes));
    resSel.value = curRes;
    const rBadge = document.getElementById("sceneResolutionBadge");
    if (rBadge) rBadge.style.display = curRes ? "inline-block" : "none";
  }

  const upSel = document.getElementById("sceneSpatialUpsampling");
  if (upSel) {
    upSel.innerHTML = "";
    upSel.add(new Option(`Default (Project: ${cat.upLabels[cat.defUp] || cat.defUp || "None"})`, ""));
    upSel.add(new Option("None (Force Disable Upscaling)", "none"));
    Object.entries(cat.upLabels).forEach(([k, lbl]) => { if (k) upSel.add(new Option(lbl, k)); });
    if (cat.curUp && !Array.from(upSel.options).some((o) => o.value === cat.curUp)) upSel.add(new Option(`${cat.curUp} (custom)`, cat.curUp));
    upSel.value = cat.curUp;
    const uBadge = document.getElementById("sceneUpsamplingBadge");
    if (uBadge) {
      uBadge.textContent = cat.skipOom ? "Skipped ≥1536px" : (cat.skipTarget ? "Skipped (== Target)" : "Custom Override");
      uBadge.style.display = (cat.skipOom || cat.skipTarget || cat.curUp) ? "inline-block" : "none";
    }
  }

  const aiSel = document.getElementById("sceneFilterAiRefiner");
  if (aiSel) {
    if (aiSel.options[0]) aiSel.options[0].textContent = `Default (${cat.shRef || "None"})`;
    if (cat.scRefVal && !Array.from(aiSel.options).some((o) => o.value === cat.scRefVal)) aiSel.add(new Option(`${cat.scRefVal} (custom)`, cat.scRefVal));
    aiSel.value = cat.scRefVal;
  }
  const setVal = (elId, v) => { const el = document.getElementById(elId); if (el) el.value = (v !== undefined && v !== null) ? String(v) : ""; };
  setVal("sceneFilterFilmGrain", cat.scF.film_grain_intensity);
  setVal("sceneFilterDeband", cat.scFF.deband === true ? "true" : (cat.scFF.deband === false ? "false" : ""));
  setVal("sceneFilterCas", cat.scFF.cas);
  setVal("sceneFilterDenoise", cat.scFF.denoise);
  setVal("sceneFilterEqBrightness", cat.scEq.brightness);
  setVal("sceneFilterEqContrast", cat.scEq.contrast);
  setVal("sceneFilterEqSaturation", cat.scEq.saturation);

  const fBadge = document.getElementById("sceneFiltersBadge");
  if (fBadge) {
    fBadge.textContent = cat.effRef ? `${cat.effRef} (1x Active)` : (cat.isCustomFilter ? "Custom Filters" : "Default Filters");
    fBadge.style.display = cat.hasAnyFilter || cat.isCustomFilter ? "inline-block" : "none";
  }
  refreshInspectorCollapsibleBadges(sc);
}

function applySceneFilterValue(sc, fieldKey, rawValue) {
  if (!sc) return;
  const val = String(rawValue ?? "").trim();
  const f = (sc.filters = sc.filters || {});
  if (LEGACY_1X_UPSAMPLE_VALS.slice(0, 4).includes(String(sc.spatial_upsampling || "").toLowerCase())) {
    delete sc.spatial_upsampling;
  }
  const normKey = {
    sceneFilterAiRefiner: "ai_refiner", sceneFilterFilmGrain: "film_grain_intensity",
    sceneFilterDeband: "deband", sceneFilterCas: "cas", sceneFilterDenoise: "denoise",
    sceneFilterEqBrightness: "brightness", sceneFilterEqContrast: "contrast", sceneFilterEqSaturation: "saturation",
  }[fieldKey] || fieldKey;

  if (normKey === "ai_refiner") {
    if (!val) delete f.ai_refiners; else if (val === "none") f.ai_refiners = []; else f.ai_refiners = [val];
  } else if (normKey === "film_grain_intensity") {
    if (val === "") delete f.film_grain_intensity; else f.film_grain_intensity = parseFloat(val) || 0;
  } else {
    const ff = (f.ffmpeg = f.ffmpeg || {});
    if (normKey === "deband") {
      if (val === "") delete ff.deband; else ff.deband = (val === "true");
    } else if (normKey === "cas" || normKey === "denoise") {
      if (val === "") delete ff[normKey]; else ff[normKey] = parseFloat(val) || 0;
    } else if (normKey === "brightness" || normKey === "contrast" || normKey === "saturation") {
      const eq = (ff.eq = ff.eq || {}), num = parseFloat(val);
      if (val === "" || isNaN(num)) delete eq[normKey]; else eq[normKey] = num;
      if (Object.keys(eq).length === 0) delete ff.eq;
    }
    if (Object.keys(ff).length === 0) delete f.ffmpeg;
  }
  if (Object.keys(f).length === 0) delete sc.filters;
}

function syncSceneFiltersToState(fieldId) {
  if (state.selectedSceneIdx === null) return;
  const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
  const el = document.getElementById(fieldId);
  if (!sc || !el) return;
  applySceneFilterValue(sc, fieldId, el.value);
  populateSceneResolutionAndUpsampling(sc);
  markProjectDirty();
  window.syncScenesTabCard?.(sc);
}

[
  "sceneFilterAiRefiner", "sceneFilterFilmGrain", "sceneFilterDeband",
  "sceneFilterCas", "sceneFilterDenoise", "sceneFilterEqBrightness",
  "sceneFilterEqContrast", "sceneFilterEqSaturation",
].forEach((fid) => {
  const el = document.getElementById(fid);
  if (!el) return;
  if (el.tagName === "SELECT") {
    el.addEventListener("change", () => {
      syncSceneFiltersToState(fid);
      window.recordHistorySnapshot?.(`Change Scene Filter`);
    });
  } else {
    el.addEventListener("focus", () => window.beginEditSession?.(`inspector:${fid}`, "Edit Scene Filter"));
    el.addEventListener("input", () => {
      window.beginEditSession?.(`inspector:${fid}`, "Edit Scene Filter");
      syncSceneFiltersToState(fid);
    });
    el.addEventListener("blur", () => window.commitEditSession?.());
  }
});

// Shared Summary Badges & Timeline Inspector Collapsible Groups
function _escHtml(s) {
  if (typeof escapeHtml === "function") return escapeHtml(s);
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function buildAdvSummaryBadgesHtml(sc, cat) {
  sc = sc || {};
  cat = cat || getSceneOptionCatalog(sc);
  const pills = [];
  if (sc.render_pad_start !== undefined || sc.render_pad_end !== undefined) {
    const defPad = state.projectData?.project?.render_overlap_seconds ?? 0;
    const pre = sc.render_pad_start ?? defPad;
    const post = sc.render_pad_end ?? (sc.anchor_end ? 0 : defPad);
    pills.push(`<span class="scene-badge-pill" style="background:rgba(56,189,248,0.18);color:#7dd3fc;">⏱ Pad ${pre}/${post}s</span>`);
  }
  if (cat.effRef || sc.filters?.ai_refiners !== undefined) {
    pills.push(`<span class="scene-badge-pill" style="background:rgba(16,185,129,0.18);color:#34d399;">✨ ${_escHtml(cat.effRef || "Refiner Off")}</span>`);
  }
  const ffPairs = [
    [cat.scFF?.deband === true || cat.scFF?.deband === false, `Deband ${cat.scFF?.deband ? "On" : "Off"}`],
    [cat.scF?.film_grain_intensity !== undefined, `Grain ${cat.scF?.film_grain_intensity}`],
    [cat.scFF?.cas !== undefined, `CAS ${cat.scFF?.cas}`],
    [cat.scFF?.denoise !== undefined, `Denoise ${cat.scFF?.denoise}`],
    [cat.scEq?.brightness !== undefined, `Bright ${cat.scEq?.brightness}`],
    [cat.scEq?.contrast !== undefined, `Contrast ${cat.scEq?.contrast}`],
    [cat.scEq?.saturation !== undefined, `Sat ${cat.scEq?.saturation}`],
  ];
  ffPairs.forEach(([on, lbl]) => {
    if (on) pills.push(`<span class="scene-badge-pill" style="background:rgba(168,85,247,0.18);color:#d8b4fe;">${_escHtml(lbl)}</span>`);
  });
  return pills.length ? pills.join("") : `<span style="color:#64748b;font-size:10px;">Project Defaults</span>`;
}

const INSP_COLLAPSE_KEY = "mvg_inspector_collapsed_v2";
const INSP_COLLAPSE_DEFAULTS = { filters: true, prompts: false, negatives: true, takes: false };
let inspCollapseState = null;

function getInspCollapseState() {
  if (inspCollapseState) return inspCollapseState;
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(INSP_COLLAPSE_KEY) : null;
    inspCollapseState = Object.assign({}, INSP_COLLAPSE_DEFAULTS, raw ? JSON.parse(raw) : {});
  } catch (e) {
    inspCollapseState = { ...INSP_COLLAPSE_DEFAULTS };
  }
  return inspCollapseState;
}

function setInspSectionCollapsed(key, collapsed) {
  const st = getInspCollapseState();
  st[key] = Boolean(collapsed);
  try { if (typeof localStorage !== "undefined") localStorage.setItem(INSP_COLLAPSE_KEY, JSON.stringify(st)); } catch (e) {}
  const body = document.querySelector(`[data-insp-body="${key}"]`);
  const caret = document.querySelector(`[data-insp-caret="${key}"]`);
  const hdr = document.querySelector(`[data-insp-header="${key}"]`);
  if (body) body.style.display = st[key] ? "none" : "";
  if (caret) caret.textContent = st[key] ? "▸" : "▾";
  if (hdr && (key === "negatives" || key === "takes")) hdr.style.marginBottom = st[key] ? "0" : (key === "negatives" ? "6px" : "8px");
  if (key === "prompts" && !st[key]) window.resizeAllPromptTextareas?.();
}

function createCollapsibleSection(id, key, titleHtml) {
  const sec = document.createElement("div");
  sec.className = "inspector-collapsible-section";
  sec.id = id;
  const hdr = document.createElement("div");
  hdr.className = "inspector-collapsible-header";
  hdr.dataset.inspHeader = key;
  hdr.innerHTML = `<div data-insp-title-wrap="${key}" style="display:flex;align-items:center;gap:6px;"><span data-insp-caret="${key}" style="color:#94a3b8;">▾</span><span style="font-weight:700;color:#cbd5e1;text-transform:uppercase;letter-spacing:0.5px;">${titleHtml}</span></div>`
    + `<div data-insp-badges="${key}" style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;min-width:0;max-width:65%;"></div>`;
  const body = document.createElement("div");
  body.className = "inspector-collapsible-body";
  body.dataset.inspBody = key;
  hdr.addEventListener("click", () => setInspSectionCollapsed(key, !getInspCollapseState()[key]));
  sec.appendChild(hdr);
  sec.appendChild(body);
  return { sec, hdr, body };
}

function syncTakesHeaderStatusPill() {
  const pill = document.getElementById("takesHeaderStatusPill");
  const diffEl = document.getElementById("takeDiffSummary");
  if (!pill || !diffEl) return;
  const txt = (diffEl.textContent || "").trim();
  if (!txt) { pill.style.display = "none"; return; }
  pill.style.display = "inline-block";
  if (txt.includes("Active Take")) {
    pill.textContent = "✓ Active Take";
    pill.style.background = "rgba(16,185,129,0.2)"; pill.style.color = "#6ee7b7";
  } else if (txt.includes("Edited since")) {
    pill.textContent = "⚠ Edited";
    pill.style.background = "rgba(245,158,11,0.22)"; pill.style.color = "#fcd34d";
  } else {
    pill.textContent = "Historical Preview";
    pill.style.background = "rgba(167,139,250,0.2)"; pill.style.color = "#c4b5fd";
  }
}

function refreshInspectorCollapsibleBadges(sc) {
  if (!sc || typeof document === "undefined" || !document.querySelector) return;
  const fBadges = document.querySelector('[data-insp-badges="filters"]');
  if (fBadges) fBadges.innerHTML = buildAdvSummaryBadgesHtml(sc);

  const pBadges = document.querySelector('[data-insp-badges="prompts"]');
  if (pBadges) {
    const isMod = sc.prompt_mode === "components";
    const isTwo = typeof isTwoPassScene === "function" ? isTwoPassScene(sc) : false;
    const markers = (state.projectData?.markers || []).filter((m) => Number(m.time) >= Number(sc.start) - 0.05 && Number(m.time) <= Number(sc.end) + 0.05);
    const snippet = (isTwo ? `P1: ${sc.prompt_m || ""} | P2: ${sc.prompt_f || ""}` : (sc.prompt || "")).replace(/\s+/g, " ").trim();
    const parts = [`<span class="scene-badge-pill" style="background:rgba(37,99,235,0.22);color:#93c5fd;">${isMod ? "🧩 Modular" : "📝 Raw"}</span>`];
    if (markers.length) parts.push(`<span class="scene-badge-pill" style="background:rgba(245,158,11,0.2);color:#fcd34d;">🚩 ${markers.length} marker${markers.length > 1 ? "s" : ""}</span>`);
    if (snippet) parts.push(`<span style="color:#94a3b8;font-family:monospace;font-size:10px;max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_escHtml(snippet)}</span>`);
    pBadges.innerHTML = parts.join("");
  }
  syncTakesHeaderStatusPill();
}

function initInspectorCollapsibleGroups() {
  if (typeof document === "undefined" || !document.createElement) return;
  const inspForm = document.getElementById("inspectorForm");
  if (!inspForm || !inspForm.parentNode || inspForm.dataset?.collapsiblesMounted === "1") return;
  inspForm.dataset.collapsiblesMounted = "1";
  const st = getInspCollapseState();

  const fRow = document.getElementById("sceneFiltersRow");
  const padGrp = document.getElementById("sceneRenderPaddingGroup");
  if (fRow?.parentNode) {
    const { sec, body } = createCollapsibleSection("inspectorGroupFilters", "filters", "🎛️ Padding, 1x Refiner &amp; Look Filters");
    const fBadge = document.getElementById("sceneFiltersBadge");
    if (fBadge) sec.querySelector('[data-insp-title-wrap="filters"]')?.appendChild(fBadge);
    fRow.parentNode.insertBefore(sec, fRow);
    if (padGrp) { padGrp.style.marginBottom = "8px"; body.appendChild(padGrp); }
    fRow.style.marginTop = "0"; fRow.style.marginBottom = "0";
    body.appendChild(fRow);
    setInspSectionCollapsed("filters", st.filters);
  }

  const mBar = document.getElementById("sceneMarkersBar");
  if (mBar?.parentNode) {
    const { sec, body } = createCollapsibleSection("inspectorGroupPrompts", "prompts", "📝 Scene Prompts &amp; Cue Markers");
    mBar.parentNode.insertBefore(sec, mBar);
    mBar.style.marginTop = "0";
    ["sceneMarkersBar", "promptModeHeaderBar", "singlePromptGroup", "duetPromptsGroup", "modularPromptsGroup"].forEach((id) => {
      const node = document.getElementById(id);
      if (node) body.appendChild(node);
    });
    setInspSectionCollapsed("prompts", st.prompts);
  }

  const mountInnerCollapse = (rootId, key, stopTargetEl) => {
    const root = document.getElementById(rootId);
    if (!root || !root.firstElementChild) return;
    const hdr = root.firstElementChild;
    hdr.dataset.inspHeader = key;
    hdr.style.cursor = "pointer"; hdr.style.userSelect = "none";
    const caret = document.createElement("span");
    caret.dataset.inspCaret = key; caret.style.cssText = "color:#94a3b8;font-size:11px;";
    hdr.firstElementChild?.prepend(caret);
    if (stopTargetEl) stopTargetEl.addEventListener("click", (e) => e.stopPropagation());
    const body = document.createElement("div");
    body.dataset.inspBody = key;
    Array.from(root.children).slice(1).forEach((ch) => body.appendChild(ch));
    root.appendChild(body);
    hdr.addEventListener("click", () => setInspSectionCollapsed(key, !getInspCollapseState()[key]));
    return hdr;
  };

  const negGrp = document.getElementById("sceneNegativePromptGroup");
  mountInnerCollapse("sceneNegativePromptGroup", "negatives", negGrp?.firstElementChild?.lastElementChild);
  setInspSectionCollapsed("negatives", st.negatives);

  const takesHdr = mountInnerCollapse("sceneTakesTray", "takes", document.getElementById("btnRestoreTake"));
  if (takesHdr?.firstElementChild) {
    const pill = document.createElement("span");
    pill.id = "takesHeaderStatusPill"; pill.className = "scene-badge-pill"; pill.style.display = "none";
    takesHdr.firstElementChild.appendChild(pill);
  }
  setInspSectionCollapsed("takes", st.takes);

  const diffEl = document.getElementById("takeDiffSummary");
  if (diffEl && typeof MutationObserver === "function") {
    new MutationObserver(syncTakesHeaderStatusPill).observe(diffEl, { childList: true, subtree: true, characterData: true });
  }

  let badgeTmr = null;
  const schedBadges = () => {
    clearTimeout(badgeTmr);
    badgeTmr = setTimeout(() => {
      const cur = state.projectData?.scenes?.[state.selectedSceneIdx];
      if (cur) refreshInspectorCollapsibleBadges(cur);
    }, 50);
  };
  inspForm.addEventListener("input", schedBadges);
  inspForm.addEventListener("change", schedBadges);
  inspForm.addEventListener("click", (e) => { if (e.target?.closest("button, .label-actions span")) schedBadges(); });
  document.getElementById("btnToggleSceneLock")?.addEventListener("click", () => {
    if (state.selectedSceneIdx !== null) toggleSceneLock(state.selectedSceneIdx);
  });
}

function formatTakeDateShared(take) {
  const iso = take?.created_iso || (typeof take?.created_at === "string" ? take.created_at : "");
  if (iso) return iso.slice(0, 16).replace("T", " ");
  if (typeof take?.created_at === "number") return new Date(take.created_at * 1000).toISOString().slice(0, 16).replace("T", " ");
  return "";
}

function formatTakeSummaryLabel(take, activeId) {
  const num = take?.take_number || "?";
  const res = take?.resolution || (take?.out_name?.match(/_(\d+x\d+)$/)?.[1]) || "";
  const pass = take?.pass_num === 1 ? "P1" : (take?.pass_num === 2 ? "P2" : "");
  const dt = formatTakeDateShared(take);
  const sz = take?.size_bytes ? `${(take.size_bytes / (1024 * 1024)).toFixed(1)}MB` : "";
  const seed = take?.seed !== undefined && take?.seed !== null ? `#${take.seed}` : "";
  const meta = [res, pass, take?.model_type || "", seed, sz].filter(Boolean).join(" • ");
  return `Take ${num}${take?.take_id === activeId ? " [★ ACTIVE]" : ""}: ${dt}${meta ? ` (${meta})` : ""}`;
}

async function deleteSceneTake(scene, chosenTake, btnEl, opts = {}) {
  if (!scene || !chosenTake || !state.currentSlug) return false;
  if (scene.locked) { showToast("🔒 Scene is locked — unlock it first"); return false; }
  const dt = formatTakeDateShared(chosenTake);
  if (!confirm(`Delete Take ${chosenTake.take_number || "?"} (${dt}) from vault and disk?\n\nThis cannot be undone.`)) return false;
  const orig = btnEl ? btnEl.textContent : "";
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = "..."; }
  try {
    const resp = await fetch("/api/takes/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: state.currentSlug, scene_id: scene.id, take_id: chosenTake.take_id }),
    });
    const data = await resp.json();
    if (!data.success) { alert("Failed to delete take: " + (data.error || "Unknown error")); return false; }
    showToast(`🗑️ Deleted Take ${chosenTake.take_number || "?"} from ${scene.id}`);
    opts.onDeleted?.(data);
    await window.refreshProjectAssets?.(true);
    await window.loadProjectQueue?.();
    if (state.projectData?.scenes?.[state.selectedSceneIdx]?.id === scene.id) {
      await window.updateSceneTakesUI?.(scene);
    }
    window.renderSceneList?.(true);
    return true;
  } catch (err) {
    alert("Error deleting take: " + err.message);
    return false;
  } finally {
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = orig; }
  }
}

function renderTakesListItems(containerEl, scene, takesData, selectedTakeId, onSelectTake, opts = {}) {
  if (!containerEl) return;
  containerEl.innerHTML = "";
  const takes = takesData?.takes || [];
  if (!takes.length) {
    containerEl.innerHTML = `<div style="font-size:11px;color:#64748b;padding:4px 2px;">No takes in vault yet.</div>`;
    return;
  }
  const activeId = takesData?.active_take_id;
  [...takes].reverse().forEach((take) => {
    const isAct = take.take_id === activeId;
    const isSel = take.take_id === (selectedTakeId || activeId);
    const res = take.resolution || (take.out_name?.match(/_(\d+x\d+)$/)?.[1]) || "";
    const pass = take.pass_num === 1 ? "P1" : (take.pass_num === 2 ? "P2" : "");
    const sz = take.size_bytes ? `${(take.size_bytes / (1024 * 1024)).toFixed(1)} MB` : "";
    const dt = formatTakeDateShared(take);
    const row = document.createElement("div");
    row.className = `take-list-row${isAct ? " is-active" : ""}${isSel ? " is-selected" : ""}`;
    row.innerHTML = `<div class="take-row-info">`
      + `<span class="scene-badge-pill" style="background:rgba(139,92,246,0.22);color:#ddd6fe;">Take ${take.take_number || "?"}</span>`
      + (isAct ? `<span class="scene-badge-pill" style="background:rgba(16,185,129,0.22);color:#6ee7b7;">★ ACTIVE</span>` : "")
      + (res ? `<span class="scene-badge-pill" style="background:rgba(56,189,248,0.16);color:#7dd3fc;">${_escHtml(res)}</span>` : "")
      + (pass ? `<span class="scene-badge-pill" style="background:rgba(245,158,11,0.18);color:#fcd34d;">${pass}</span>` : "")
      + `<span class="take-row-meta">${_escHtml([take.model_type, take.seed !== undefined && take.seed !== null ? `Seed ${take.seed}` : "", sz, dt].filter(Boolean).join(" • "))}</span>`
      + `</div><div class="take-row-actions"></div>`;
    const acts = row.querySelector(".take-row-actions");
    const mkTakeBtn = (txt, title, cls, fn, dis = false, sty = "") => {
      const b = document.createElement("button");
      b.type = "button"; b.className = cls; b.textContent = txt; b.title = title; b.disabled = dis;
      if (sty) b.style.cssText = sty;
      b.onclick = (e) => { e.stopPropagation(); fn(b); };
      acts.appendChild(b);
    };
    mkTakeBtn("▶ Preview", "Preview this take clip", "btn-mini btn-take-preview", () => onSelectTake?.(take), false, "padding:2px 7px;font-size:10px;color:#93c5fd;border-color:rgba(59,130,246,0.4);");
    if (!isAct) {
      mkTakeBtn("↺ Use", "Restore this take as active on timeline", "btn-mini", (b) => window.restoreSceneToTake?.(scene, take, b), Boolean(scene?.locked), "padding:2px 7px;font-size:10px;color:#c4b5fd;border-color:rgba(167,139,250,0.45);");
    }
    mkTakeBtn("🗑️", "Delete take from vault and disk", "btn-mini", (b) => deleteSceneTake(scene, take, b, opts), Boolean(scene?.locked), "padding:2px 6px;font-size:10px;color:#f87171;border-color:rgba(239,68,68,0.4);");
    row.onclick = () => onSelectTake?.(take);
    containerEl.appendChild(row);
  });
}

function applyInspectorLockState(sc) {
  const locked = Boolean(sc?.locked);
  const btnLock = document.getElementById("btnToggleSceneLock");
  if (btnLock) {
    btnLock.textContent = locked ? "🔒 Locked" : "🔓 Lock";
    btnLock.style.cssText = locked ? "background:rgba(245,158,11,0.22);border-color:rgba(245,158,11,0.6);color:#fcd34d;font-weight:700;" : "";
  }
  ["btnDeleteScene", "btnRevertSceneToRendered"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = locked;
  });
  const btnSplit = document.getElementById("btnSplitSceneHeader");
  if (btnSplit) { btnSplit.classList.toggle("is-locked-action", locked); btnSplit.title = locked ? `Scene ${sc?.id || ""} is locked — unlock it first` : "Split current scene at playhead (Hotkey: S)"; }
  const inspForm = document.getElementById("inspectorForm");
  if (!inspForm) return;
  inspForm.classList?.toggle("is-locked", locked);
  inspForm.querySelectorAll?.("input, select, textarea, button").forEach((el) => {
    const keep = el.id === "sceneTakeSelect" || el.classList?.contains("btn-take-preview");
    el.disabled = keep ? false : (locked || el.dataset?.forceDisabled === "1");
  });
  ["btnClearEndAnchor", "btnMatchNextAnchor", "btnResetScenePadding"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) { el.style.pointerEvents = locked ? "none" : ""; el.style.opacity = locked ? "0.45" : ""; }
  });
}

async function toggleSceneLock(sceneIdx) {
  const sc = (state.projectData?.scenes || [])[sceneIdx];
  if (!sc || sc._lockPending) return;
  const prevLocked = Boolean(sc.locked), nextLocked = !prevLocked;
  if (nextLocked) sc.locked = true; else delete sc.locked;
  const syncLockUI = () => { redrawTimeline(); if (sceneIdx === state.selectedSceneIdx) { applyInspectorLockState(sc); window.updateInspectorSceneHeader?.(sc); } window.syncScenesTabCard?.(sc); };
  syncLockUI();
  if (!state.currentSlug || typeof fetch !== "function") return;
  sc._lockPending = true;
  try {
    const resp = await fetch("/api/scene/lock", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug: state.currentSlug, scene_id: sc.id, locked: nextLocked }) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.success) throw new Error(data.error || `HTTP ${resp.status}`);
    delete sc._lockPending;
    window.patchSavedSceneLockBaseline?.(sc.id, nextLocked);
    showToast(nextLocked ? `🔒 Scene ${sc.id} locked` : `🔓 Scene ${sc.id} unlocked`);
  } catch (err) {
    delete sc._lockPending;
    if (prevLocked) sc.locked = true; else delete sc.locked;
    syncLockUI();
    showToast(`❌ Failed to ${nextLocked ? "lock" : "unlock"} Scene ${sc.id}: ${err.message}`);
  }
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initInspectorCollapsibleGroups);
  else initInspectorCollapsibleGroups();
}

Object.assign(window, {
  getSceneOptionCatalog, applySceneFilterValue, populateSceneResolutionAndUpsampling,
  buildAdvSummaryBadgesHtml, initInspectorCollapsibleGroups, refreshInspectorCollapsibleBadges,
  formatTakeSummaryLabel, deleteSceneTake, renderTakesListItems, applyInspectorLockState,
  toggleSceneLock, unloadMasterVideoForStitch, updateMasterStitchProgress, finishMasterStitch,
  handleMasterStitchSSE, triggerImmediateRestitch, updateMasterVideoUI, switchTab,
});

// Event Listeners
document.getElementById("btnRefreshMasterVideo")?.addEventListener("click", () => {
  updateMasterVideoUI();
  showToast("Master video reloaded");
});
document.getElementById("btnStitchMasterFromTab")?.addEventListener("click", () => {
  triggerImmediateRestitch();
});
