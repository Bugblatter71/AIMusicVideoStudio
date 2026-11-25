// ============================================================================
// Anchor Studio View
// ============================================================================
function compareAnchorIdsNatural(aId, bId) {
  const pa = String(aId || "").match(/^A(\d+)([a-z]?)$/i);
  const pb = String(bId || "").match(/^A(\d+)([a-z]?)$/i);
  if (pa && pb) {
    const na = parseInt(pa[1], 10);
    const nb = parseInt(pb[1], 10);
    if (na !== nb) return na - nb;
    return (pa[2] || "").localeCompare(pb[2] || "");
  }
  if (pa) return -1;
  if (pb) return 1;
  return String(aId || "").localeCompare(String(bId || ""));
}

function findDiskAnchorInfo(diskAnchors, filename, aid) {
  let diskInfo = (filename ? diskAnchors[filename] : null) || (aid ? diskAnchors[aid] : null);
  if (!diskInfo && aid) {
    const matchedKey = Object.keys(diskAnchors).find(
      (k) => extractAnchorId(k) === aid || k.startsWith(`${aid} `) || k.startsWith(`${aid}_`) || k.startsWith(`${aid}-`)
    );
    if (matchedKey) diskInfo = diskAnchors[matchedKey];
  }
  return diskInfo || null;
}

function getMergedAnchorsList() {
  const configAnchors = state.projectData?.anchors || [];
  const diskAnchors = state.assets?.anchors || {};
  const seenAids = new Set();
  const seenNames = new Set();
  const merged = [];

  configAnchors.forEach((a, idx) => {
    const aid = extractAnchorId(a.filename) || `A${String(idx + 1).padStart(2, "0")}`;
    const diskInfo = findDiskAnchorInfo(diskAnchors, a.filename, aid);
    const resolvedName = diskInfo?.filename || a.filename || aid;
    if (aid) seenAids.add(aid.toUpperCase());
    if (a.filename) seenNames.add(a.filename);
    if (resolvedName) seenNames.add(resolvedName);
    merged.push({
      anchor: a,
      aid,
      diskInfo,
      resolvedName,
      valueName: a.filename || resolvedName || aid,
      isDiskOnly: false,
    });
  });

  const diskOnlyItems = [];
  Object.values(diskAnchors).forEach((info) => {
    if (!info || !info.filename) return;
    const aid = info.aid || extractAnchorId(info.filename);
    if (!aid || !/^A\d{2}[a-z]?$/i.test(aid)) return;
    if (info.is_anchor_dir === false) return; // Exclude references/ and ref_sheets/
    if (seenAids.has(aid.toUpperCase()) || seenNames.has(info.filename)) return;
    seenAids.add(aid.toUpperCase());
    seenNames.add(info.filename);
    diskOnlyItems.push({
      anchor: { filename: info.filename, seed: null, prompt: "", external: true },
      aid,
      diskInfo: info,
      resolvedName: info.filename,
      valueName: info.filename,
      isDiskOnly: true,
    });
  });

  diskOnlyItems.sort((x, y) => compareAnchorIdsNatural(x.aid, y.aid));
  return merged.concat(diskOnlyItems);
}

window.registerDiskAnchor = function (filename) {
  if (!state.projectData || !filename) return;
  if (!Array.isArray(state.projectData.anchors)) {
    state.projectData.anchors = [];
  }
  const aid = extractAnchorId(filename);
  const exists = state.projectData.anchors.some(
    (a) => a.filename === filename || (aid && extractAnchorId(a.filename) === aid)
  );
  if (exists) {
    showToast(`Anchor [${aid || filename}] is already registered in project config.`);
    return;
  }
  state.projectData.anchors.push({
    filename,
    seed: 42,
    prompt: "",
    external: true,
  });
  markProjectDirty();
  window.recordHistorySnapshot?.(`Register Disk Anchor ${aid || filename}`);
  updateAnchorStudioUI();
  updateInspectorAnchorsDropdown();
  showToast(`Registered [${aid || filename}] in project config — Save (💾) to persist.`);
};

function updateAnchorStudioUI() {
  const container = document.getElementById("anchorCardsContainer");
  if (!container) return;
  container.innerHTML = "";

  const entries = getMergedAnchorsList();

  entries.forEach(({ anchor: a, aid, diskInfo, resolvedName, isDiskOnly }) => {
    const card = document.createElement("div");
    card.className = "anchor-card";
    const anchorMtime = diskInfo?.mtime || "";
    const safeName = escapeHtml(resolvedName);
    const safeAid = escapeHtml(aid);
    const badgeHtml = isDiskOnly
      ? `<span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(16,185,129,0.18); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.4);">Disk Only</span>`
      : a.external
        ? `<span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(56,189,248,0.16); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.35);">External</span>`
        : `<span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #334155;">Seed: ${a.seed ?? "-"}</span>`;

    const previewText = isDiskOnly
      ? "Found in anchors folder (not yet in JSON config). Already selectable in Scene Inspector."
      : (a.prompt ? escapeHtml(a.prompt) : "(external / no prompt)");

    const encSlug = encodeURIComponent(state.currentSlug || "");
    card.innerHTML = `
      <div class="anchor-thumb">
        <img src="/media/${encSlug}/anchor/${encodeURIComponent(resolvedName)}${anchorMtime ? `?t=${anchorMtime}` : ""}" 
             alt="${safeName}" 
             loading="lazy">
      </div>
      <div class="anchor-body">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span class="anchor-id">${safeAid}</span>
          ${badgeHtml}
        </div>
        <div style="font-size: 11px; color: var(--text-main); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${safeName}">${safeName}</div>
        <div class="anchor-prompt-preview">${previewText}</div>
        <div class="anchor-card-actions" style="display: flex; gap: 6px; margin-top: auto;"></div>
      </div>
    `;
    const thumbImg = card.querySelector(".anchor-thumb img");
    if (thumbImg) thumbImg.onerror = () => window.handleAnchorImgError?.(thumbImg, state.currentSlug, aid);

    const actionsEl = card.querySelector(".anchor-card-actions");
    if (actionsEl) {
      if (isDiskOnly) {
        const regBtn = document.createElement("button");
        regBtn.className = "btn";
        regBtn.style.flex = "1";
        regBtn.textContent = "+ Register in Project";
        regBtn.title = "Add this disk anchor to the project JSON anchors list (optional; already usable in Scene Inspector)";
        regBtn.addEventListener("click", () => window.registerDiskAnchor(resolvedName));
        actionsEl.appendChild(regBtn);
      } else if (a.external && !String(a.prompt || "").trim()) {
        const noteSpan = document.createElement("span");
        noteSpan.style.fontSize = "11px";
        noteSpan.style.color = "var(--text-muted)";
        noteSpan.textContent = "Imported disk image (selectable in Scene Inspector)";
        actionsEl.appendChild(noteSpan);
      } else {
        const genBtn = document.createElement("button");
        genBtn.className = "btn btn-primary";
        genBtn.style.flex = "1";
        genBtn.textContent = "⚡ Generate";
        genBtn.addEventListener("click", () => window.renderSingleAnchor(aid));
        actionsEl.appendChild(genBtn);
      }
    }
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

function buildAnchorMenuItem(a, aid, resolvedName, thumbUrl, onSelect, isDiskOnly = false) {
  const item = document.createElement("div");
  item.className = "anchor-dropdown-item";
  item.dataset.value = a.filename;
  item.dataset.aid = aid;

  const img = document.createElement("img");
  img.src = thumbUrl;
  img.alt = aid;
  img.style.width = "100%";
  img.style.height = "120px";
  img.style.objectFit = "cover";
  img.style.borderRadius = "6px";
  img.style.border = "1px solid var(--border-color)";
  img.style.boxShadow = "0 2px 8px rgba(0, 0, 0, 0.5)";
  img.style.backgroundColor = "#000";
  img.style.display = "block";
  img.onerror = () => { img.style.display = "none"; };

  const textDiv = document.createElement("div");
  textDiv.style.width = "100%";
  textDiv.style.boxSizing = "border-box";
  textDiv.style.overflow = "hidden";

  const topRow = document.createElement("div");
  topRow.style.display = "flex";
  topRow.style.alignItems = "center";
  topRow.style.justifyContent = "space-between";
  topRow.style.marginBottom = "3px";

  const aidSpan = document.createElement("span");
  aidSpan.style.fontWeight = "700";
  aidSpan.style.color = "#f59e0b";
  aidSpan.style.fontSize = "13px";
  aidSpan.textContent = `[${aid}]`;
  topRow.appendChild(aidSpan);

  if (isDiskOnly) {
    const badge = document.createElement("span");
    badge.style.fontSize = "10px";
    badge.style.padding = "1px 5px";
    badge.style.borderRadius = "4px";
    badge.style.background = "rgba(16,185,129,0.2)";
    badge.style.color = "#6ee7b7";
    badge.textContent = "Disk";
    topRow.appendChild(badge);
  }

  const nameSpan = document.createElement("div");
  nameSpan.style.color = "var(--text-main)";
  nameSpan.style.fontSize = "12px";
  nameSpan.style.fontWeight = "500";
  nameSpan.style.lineHeight = "1.35";
  nameSpan.style.wordBreak = "break-word";
  nameSpan.textContent = resolvedName;

  textDiv.appendChild(topRow);
  textDiv.appendChild(nameSpan);

  item.appendChild(img);
  item.appendChild(textDiv);
  item.addEventListener("click", onSelect);
  return item;
}

function updateInspectorAnchorsDropdown() {
  const sel = document.getElementById("sceneAnchor");
  const selEnd = document.getElementById("sceneAnchorEnd");
  const menu = document.getElementById("anchorDropdownMenu");
  const menuEnd = document.getElementById("endAnchorDropdownMenu");
  if (sel) sel.innerHTML = "";
  if (selEnd) selEnd.innerHTML = '<option value="">(None / Single Anchor)</option>';
  if (menu) menu.innerHTML = "";
  if (menuEnd) {
    menuEnd.innerHTML = "";
    const noneItem = document.createElement("div");
    noneItem.className = "anchor-dropdown-item";
    noneItem.dataset.value = "";
    noneItem.style.gridColumn = "1 / -1";
    noneItem.innerHTML = `
      <div style="height: 48px; display: flex; align-items: center; justify-content: center; gap: 8px; border-radius: 6px; border: 1px dashed var(--border-color); background: rgba(0,0,0,0.3); color: var(--text-muted); font-size: 13px; font-weight: 500;">
        <span style="font-size: 16px;">🚫</span> (None / Single Anchor Mode)
      </div>
    `;
    noneItem.addEventListener("click", () => {
      if (selEnd) {
        selEnd.value = "";
        selEnd.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (typeof window.updateSceneEndAnchorPreview === "function") window.updateSceneEndAnchorPreview("");
      menuEnd.style.display = "none";
      menuEnd.classList.remove("open");
    });
    menuEnd.appendChild(noneItem);
  }

  const entries = getMergedAnchorsList();

  entries.forEach(({ anchor: a, aid, diskInfo, resolvedName, valueName, isDiskOnly }) => {
    const anchorMtime = diskInfo?.mtime || "";
    const thumbUrl = `/media/${state.currentSlug}/anchor/${encodeURIComponent(resolvedName)}${anchorMtime ? `?t=${anchorMtime}` : ""}`;
    const labelPrefix = isDiskOnly ? `[${aid}] ${resolvedName} (Disk)` : `[${aid}] ${resolvedName}`;

    // 1. Populate native selects
    if (sel) {
      const opt = document.createElement("option");
      opt.value = valueName;
      opt.dataset.aid = aid;
      opt.textContent = labelPrefix;
      sel.appendChild(opt);
    }
    if (selEnd) {
      const optEnd = document.createElement("option");
      optEnd.value = valueName;
      optEnd.dataset.aid = aid;
      optEnd.textContent = labelPrefix;
      selEnd.appendChild(optEnd);
    }

    // 2. Populate visual custom dropdown menus
    if (menu) {
      menu.appendChild(buildAnchorMenuItem(a, aid, resolvedName, thumbUrl, () => {
        if (sel) { sel.value = valueName; sel.dispatchEvent(new Event("change", { bubbles: true })); }
        if (typeof updateSceneAnchorPreview === "function") updateSceneAnchorPreview(valueName);
        menu.style.display = "none"; menu.classList.remove("open");
      }, isDiskOnly));
    }
    if (menuEnd) {
      menuEnd.appendChild(buildAnchorMenuItem(a, aid, resolvedName, thumbUrl, () => {
        if (selEnd) { selEnd.value = valueName; selEnd.dispatchEvent(new Event("change", { bubbles: true })); }
        if (typeof window.updateSceneEndAnchorPreview === "function") window.updateSceneEndAnchorPreview(valueName);
        menuEnd.style.display = "none"; menuEnd.classList.remove("open");
      }, isDiskOnly));
    }
  });

  // Re-sync current selection highlight if a scene is selected
  if (state.selectedSceneIdx !== null && state.projectData?.scenes) {
    const sc = state.projectData.scenes[state.selectedSceneIdx];
    if (sc) {
      if (typeof updateSceneAnchorPreview === "function") updateSceneAnchorPreview(sc.anchor);
      if (typeof window.updateSceneEndAnchorPreview === "function") window.updateSceneEndAnchorPreview(sc.anchor_end || "");
    }
  }
  initAnchorDropdownTrigger();
}

function initAnchorDropdownTrigger() {
  function bindTrigger(btnId, menuId, groupId) {
    const btn = document.getElementById(btnId);
    const menu = document.getElementById(menuId);
    const group = document.getElementById(groupId);
    if (!btn || !menu || btn.dataset.bound) return;
    btn.dataset.bound = "1";

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = !menu.classList.contains("open") && menu.style.display !== "grid";
      document.querySelectorAll(".custom-anchor-dropdown-menu").forEach((m) => {
        m.style.display = "none";
        m.classList.remove("open");
      });
      if (willOpen) {
        menu.style.display = "grid";
        menu.classList.add("open");
        const selected = menu.querySelector(".anchor-dropdown-item.selected");
        if (selected) selected.scrollIntoView({ block: "nearest" });
      }
    });

    document.addEventListener("click", (e) => {
      if (group && !group.contains(e.target)) {
        menu.style.display = "none";
        menu.classList.remove("open");
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        menu.style.display = "none";
        menu.classList.remove("open");
      }
    });
  }

  bindTrigger("btnAnchorDropdownTrigger", "anchorDropdownMenu", "anchorPickerGroup");
  bindTrigger("btnEndAnchorDropdownTrigger", "endAnchorDropdownMenu", "endAnchorPickerGroup");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initAnchorDropdownTrigger);
} else {
  initAnchorDropdownTrigger();
}

function getProjectStemsCatalog() {
  const stems = [];
  const seen = new Set();
  const addStem = (val, label) => {
    if (!val || seen.has(val.toLowerCase())) return;
    seen.add(val.toLowerCase());
    stems.push({ value: val, label: label || val });
  };

  const assets = state.assets || {};
  const cfgStems = state.projectData?.project?.stems || {};
  const audioFiles = assets.audio_files || [];
  const stemFilesMap = assets.stem_files && typeof assets.stem_files === "object" ? assets.stem_files : null;
  const missingSet = new Set((assets.missing_stems || []).map((k) => String(k).toLowerCase()));

  // 1. Configured stems from project JSON (excluding stems confirmed missing on disk)
  Object.keys(cfgStems).forEach((k) => {
    if (missingSet.has(k.toLowerCase())) return;
    const rawFile = stemFilesMap?.[k] || String(cfgStems[k] || "").split(/[\\/]/).pop();
    addStem(k, rawFile ? `${k} (${rawFile})` : k);
  });

  // 2. Additional audio files dropped into the project audio/ folder
  const rawMaster = String(state.projectData?.project?.master_audio || "").split(/[\\/]/).pop().toLowerCase();
  const resolvedMaster = String(assets.master_audio_file || "").split(/[\\/]/).pop().toLowerCase();
  const mappedFiles = new Set([
    ...Object.values(stemFilesMap || {}).map((p) => String(p || "").split(/[\\/]/).pop().toLowerCase()),
    ...Object.entries(cfgStems)
      .filter(([k]) => !missingSet.has(k.toLowerCase()))
      .map(([, p]) => String(p || "").split(/[\\/]/).pop().toLowerCase()),
  ]);

  audioFiles.forEach((fn) => {
    const fnLow = String(fn || "").toLowerCase();
    if (!fnLow || fnLow === rawMaster || (resolvedMaster && fnLow === resolvedMaster)) return;
    const stemName = fn.replace(/\.[^/.]+$/, "");
    if (!mappedFiles.has(fnLow)) {
      addStem(stemName, `${stemName} (${fn})`);
    }
  });

  // 3. Built-in special audio choices (master only if present or assets unscanned)
  if (assets.master_audio_exists !== false) {
    const masterLbl = assets.master_audio_file ? `full audio (${assets.master_audio_file})` : "full audio (master)";
    addStem("master", masterLbl);
  }
  addStem("silent", "silent (no lip sync)");
  return stems;
}

function pickDefaultStem(preferred) {
  const stems = getProjectStemsCatalog();
  const match = preferred && stems.find((s) => s.value.toLowerCase() === String(preferred).toLowerCase());
  if (match) return match.value;
  const vocal = stems.find((s) => /vocal|lead/i.test(s.value)) || stems.find((s) => s.value !== "master" && s.value !== "silent");
  if (vocal) return vocal.value;
  return stems.find((s) => s.value === "master")?.value || "silent";
}

if (typeof window !== "undefined") {
  window.getProjectStemsCatalog = getProjectStemsCatalog;
  window.pickDefaultStem = pickDefaultStem;
}

function updateInspectorStemsDropdown() {
  const stemSelects = [
    [document.getElementById("sceneStem"), ""],
    [document.getElementById("sceneStemLeft"), "L: "],
    [document.getElementById("sceneStemRight"), "R: "],
    [document.getElementById("sceneStemM"), ""],
    [document.getElementById("sceneStemF"), ""],
  ];

  const stems = getProjectStemsCatalog();

  stemSelects.forEach(([sel, prefix]) => {
    if (!sel) return;
    const prevVal = sel.value;
    sel.innerHTML = "";
    stems.forEach((st) => {
      const opt = document.createElement("option");
      opt.value = st.value;
      opt.textContent = prefix ? `${prefix}${st.label}` : st.label;
      sel.appendChild(opt);
    });
    const prevMatch = prevVal ? stems.find((s) => s.value.toLowerCase() === prevVal.toLowerCase()) : null;
    if (prevMatch) sel.value = prevMatch.value;
  });
}

window.renderSingleAnchor = window.renderSingleAnchor || function (identifier) {
  const aid = extractAnchorId(identifier) || identifier;
  triggerRender(["--anchors-only", aid]);
};

// ============================================================================
// Project & Models Configuration
// ============================================================================
function _appendGroupedOptions(sel, localMap, falMap, mmMap) {
  const appendGroup = (label, map) => {
    const keys = Object.keys(map || {});
    if (!keys.length) return;
    const grp = document.createElement("optgroup");
    grp.label = label;
    keys.forEach((k) => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = (k.startsWith("fal_") || k.startsWith("minimax_api_")) ? (map[k].name || map[k].model_type || k) : `[#${k}] ${map[k].name || map[k].model_type}`;
      grp.appendChild(opt);
    });
    sel.appendChild(grp);
  };
  appendGroup("🖥️ Local (Wan2GP)", localMap);
  appendGroup("☁️ Cloud (fal.ai)", falMap);
  appendGroup("🎥 Cloud (MiniMax Direct API)", mmMap);
}
window.appendGroupedModelOptions = _appendGroupedOptions;

function populateModelSelectors() {
  const anchorSel = document.getElementById("cfgDefaultAnchorRenderer");
  const videoSel = document.getElementById("cfgDefaultVideoRenderer");

  anchorSel.innerHTML = "";
  videoSel.innerHTML = "";

  _appendGroupedOptions(anchorSel, state.models?.anchor_renderers, state.models?.fal_anchor_renderers);
  _appendGroupedOptions(videoSel, state.models?.video_renderers, state.models?.fal_video_renderers, state.models?.minimax_video_renderers);
}

function updateProjectSettingsUI() {
  const p = state.projectData?.project || {};
  const m = state.projectData?.models || {};

  document.getElementById("cfgProjectName").value = p.name || "";
  document.getElementById("cfgMasterAudio").value = p.master_audio || "";
  const audioDatalist = document.getElementById("cfgMasterAudioList");
  if (audioDatalist) {
    audioDatalist.innerHTML = (state.assets?.audio_files || [])
      .map((fn) => `<option value="audio/${escapeHtml(fn)}"></option>`)
      .join("");
  }
  document.getElementById("cfgFps").value = p.fps || 24;
  document.getElementById("cfgFadeOut").value = p.fade_out_last_scene_seconds ?? 2.0;
  const fadeEndEl = document.getElementById("cfgFadeOutEnd");
  if (fadeEndEl) {
    const fe = p.fade_out_end_seconds;
    fadeEndEl.value = (fe !== undefined && fe !== null && fe !== "") ? fe : "";
    const scs = state.projectData?.scenes || [];
    const endS = scs.length ? Number(scs[scs.length - 1]?.end || 0) : Number(p.total_duration || 0);
    fadeEndEl.placeholder = endS > 0 ? `End of video (${endS.toFixed(2)}s)` : "End of video";
  }
  document.getElementById("cfgRenderOverlap").value = p.render_overlap_seconds ?? 1.0;

  document.getElementById("cfgDefaultAnchorRenderer").value = String(m.default_anchor_renderer || 3);
  document.getElementById("cfgDefaultVideoRenderer").value = String(m.default_video_renderer || 1);
  const aShared = m.anchor_shared_settings || {};
  const profDefAnchorRes = state.models?.anchor_shared_settings?.resolution || "1920x1080";
  const defAnchorRes = aShared.resolution || profDefAnchorRes;
  const cfgAnchorResEl = document.getElementById("cfgDefaultAnchorResolution");
  if (cfgAnchorResEl) {
    if (!Array.from(cfgAnchorResEl.options).some((o) => o.value === defAnchorRes)) {
      cfgAnchorResEl.add(new Option(`${defAnchorRes} (Custom)`, defAnchorRes));
    }
    cfgAnchorResEl.value = defAnchorRes;
  }
  const selToolbarAnchorRes = document.getElementById("selDefaultAnchorResolution");
  if (selToolbarAnchorRes && selToolbarAnchorRes.options.length > 0) {
    selToolbarAnchorRes.value = defAnchorRes;
  }
  const vShared = m.video_shared_settings || {};
  const rawDefUp = String(vShared.spatial_upsampling || "");
  const isLegacy1x = ["h3facerefine", "seedvr2*1", "flashvsrplus*1", "flashvsrplus2pass*1", "ltx25_refine*1", "dlss5*1"].includes(rawDefUp.toLowerCase());
  if (document.getElementById("cfgDefaultSpatialUpsampling")) {
    document.getElementById("cfgDefaultSpatialUpsampling").value = isLegacy1x ? "" : rawDefUp;
  }
  const fShared = vShared.filters || {};
  const ffShared = fShared.ffmpeg || {};
  const defRefiner = (fShared.ai_refiners && fShared.ai_refiners[0]) || (isLegacy1x ? rawDefUp : "");
  if (document.getElementById("cfgFilterAiRefiner")) document.getElementById("cfgFilterAiRefiner").value = defRefiner;
  if (document.getElementById("cfgFilterFaceStrength")) document.getElementById("cfgFilterFaceStrength").value = fShared.face_strength ?? 0.75;
  if (document.getElementById("cfgFilterFilmGrain")) document.getElementById("cfgFilterFilmGrain").value = fShared.film_grain_intensity ?? vShared.film_grain_intensity ?? 0;
  if (document.getElementById("cfgFilterWindowCC")) document.getElementById("cfgFilterWindowCC").value = fShared.window_color_correction ?? vShared.sliding_window_color_correction_strength ?? 0;
  if (document.getElementById("cfgFilterCas")) document.getElementById("cfgFilterCas").value = ffShared.cas ?? 0;
  if (document.getElementById("cfgFilterDenoise")) document.getElementById("cfgFilterDenoise").value = ffShared.denoise ?? 0;
  if (document.getElementById("cfgFilterDeband")) document.getElementById("cfgFilterDeband").checked = Boolean(ffShared.deband);
  document.getElementById("cfgNegativePrompt").value = vShared.negative_prompt || "";

  if (document.getElementById("cfgMasterResolution")) {
    document.getElementById("cfgMasterResolution").value = p.master_resolution || "match";
  }
  if (document.getElementById("cfgMasterScaleAlgo")) {
    document.getElementById("cfgMasterScaleAlgo").value = p.master_scale_algorithm || "lanczos";
  }
  if (document.getElementById("cfgMasterSharpen")) {
    document.getElementById("cfgMasterSharpen").value = String(p.master_sharpen ?? 0);
  }
  if (document.getElementById("cfgProgressiveStitchMode")) {
    document.getElementById("cfgProgressiveStitchMode").value = p.progressive_stitch_mode || "always_master";
  }

  renderResolutionCards(m.default_video_resolution || 4);
  if (typeof window.updateMasterResolutionInfoUI === "function") {
    window.updateMasterResolutionInfoUI();
  }
  window.syncProjectBackendSelectors?.();
  window.refreshFalStatus?.();
}

function renderResolutionCards(activeRes) {
  const container = document.getElementById("resolutionCardsContainer");
  if (container) container.innerHTML = "";
  const vResSelect = document.getElementById("cfgDefaultVideoResolutionSelect");
  if (vResSelect) vResSelect.innerHTML = "";

  const resDefs = state.models?.video_resolutions || state.models?.resolutions || {
    "1": { width: 832, height: 480, name: "480p Fast Preview", speed_factor: "~1 min/s", aspect: "16:9", vram_footprint: "Lowest", description: "Ultra-fast draft pass for testing camera motion and lip-sync." },
    "2": { width: 960, height: 544, name: "540p qHD Balanced", speed_factor: "~1.5 min/s", aspect: "16:9", vram_footprint: "Light", description: "Balanced speed/quality option. +30% more detail than 480p." },
    "3": { width: 1280, height: 720, name: "720p HD Standard", speed_factor: "~2.5 min/s", aspect: "16:9", vram_footprint: "Moderate", description: "Standard 720p HD widescreen resolution. Sharp, clean presentation." },
    "4": { width: 1344, height: 768, name: "768p WXGA High-Res (DEFAULT)", speed_factor: "~3.2 min/s", aspect: "16:9", vram_footprint: "Recommended", description: "Recommended native DiT training sweet spot." },
    "5": { width: 1536, height: 864, name: "864p Near-1080p", speed_factor: "~4.5 min/s", aspect: "16:9", vram_footprint: "High", description: "High fidelity generation with exceptional texture definition." },
    "6": { width: 1920, height: 1088, name: "1080p Full HD Block-Aligned", speed_factor: "~6-7 min/s", aspect: "16:9", vram_footprint: "Maximum", description: "Wan2GP official 1080p block-aligned preset (1088 height divisible by 16)." },
  };

  const applyVideoResOption = (k) => {
    const r = resDefs[k] || {};
    const resDim = r.resolution || (r.width && r.height ? `${r.width}x${r.height}` : String(k));
    state.projectData.models.default_video_resolution = /^\d+$/.test(String(k)) ? parseInt(k, 10) : k;
    markProjectDirty();
    renderResolutionCards(k);
    if (state.selectedSceneIdx !== null && state.projectData?.scenes?.[state.selectedSceneIdx]) {
      const selSc = state.projectData.scenes[state.selectedSceneIdx];
      window.updateInspectorSceneHeader?.(selSc);
      window.populateSceneResolutionAndUpsampling?.(selSc);
      window.syncSceneBackendUI?.(selSc);
    }
    window.updateMasterResolutionInfoUI?.();
    window.recordHistorySnapshot?.(`Select Resolution Option #${k}`);
    showToast(`Selected Default Video Resolution #${k} (${resDim})`);
  };

  Object.keys(resDefs).forEach((k) => {
    const r = resDefs[k] || {};
    const resDim = r.resolution || (r.width && r.height ? `${r.width}x${r.height}` : String(k));
    const isAct = String(activeRes) === String(k);
    if (vResSelect) {
      const opt = new Option(`Option #${k}: ${resDim} — ${r.name || ""}`, String(k));
      vResSelect.add(opt);
    }
    if (!container) return;
    const card = document.createElement("div");
    card.className = `res-card ${isAct ? "active" : ""}`;
    card.innerHTML = `
      <div class="res-title">
        <span>Option ${escapeHtml(k)}: ${escapeHtml(resDim)}</span>
        ${isAct ? '<span style="font-size: 11px; color: var(--primary);">ACTIVE</span>' : ""}
      </div>
      <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">${escapeHtml(r.name || "")}</div>
      <div class="res-desc">${escapeHtml(r.description || "")}</div>
      <div style="font-size: 11px; color: var(--primary); margin-top: 8px;">Speed: ${escapeHtml(r.speed_factor || "-")} | VRAM: ${escapeHtml(r.vram_footprint || "-")}</div>
    `;
    card.addEventListener("click", () => applyVideoResOption(k));
    container.appendChild(card);
  });
  if (vResSelect) {
    vResSelect.value = String(activeRes);
    vResSelect.onchange = () => applyVideoResOption(vResSelect.value);
  }
}

function syncSettingsFormToState(id) {
  if (!state.projectData) return;
  const p = state.projectData.project || (state.projectData.project = {});
  const m = state.projectData.models || (state.projectData.models = {});
  const el = document.getElementById(id);
  if (!el) return;
  const val = el.value;
  if (id === "cfgProjectName") p.name = val;
  else if (id === "cfgMasterAudio") p.master_audio = val;
  else if (id === "cfgFps") p.fps = parseInt(val) || 24;
  else if (id === "cfgFadeOut") { const n = parseFloat(val); p.fade_out_last_scene_seconds = isNaN(n) ? 2.0 : Math.max(0, n); }
  else if (id === "cfgFadeOutEnd") { const n = parseFloat(val); if (val === "" || isNaN(n)) delete p.fade_out_end_seconds; else p.fade_out_end_seconds = Math.max(0, n); }
  else if (id === "cfgRenderOverlap") { const n = parseFloat(val); p.render_overlap_seconds = (val === "" || isNaN(n)) ? 1.0 : Math.max(0, n); }
  else if (id === "cfgDefaultAnchorRenderer") m.default_anchor_renderer = /^\d+$/.test(val) ? parseInt(val, 10) : (val || 3);
  else if (id === "cfgDefaultAnchorResolution") {
    const profDef = state.models?.anchor_shared_settings?.resolution || "1920x1080";
    m.anchor_shared_settings = m.anchor_shared_settings || {};
    m.anchor_shared_settings.resolution = val || profDef;
    const tbSel = document.getElementById("selDefaultAnchorResolution");
    if (tbSel) tbSel.value = m.anchor_shared_settings.resolution;
    window.updateAnchorStudioUI?.();
  }
  else if (id === "cfgDefaultVideoRenderer") m.default_video_renderer = /^\d+$/.test(val) ? parseInt(val, 10) : (val || 1);
  else if (id === "cfgMasterResolution") p.master_resolution = val || "match";
  else if (id === "cfgMasterScaleAlgo") p.master_scale_algorithm = val || "lanczos";
  else if (id === "cfgMasterSharpen") p.master_sharpen = parseFloat(val) || 0;
  else if (id === "cfgProgressiveStitchMode") p.progressive_stitch_mode = val || "always_master";
  else if (id === "cfgDefaultSpatialUpsampling") {
    m.video_shared_settings = m.video_shared_settings || {};
    if (val) m.video_shared_settings.spatial_upsampling = val;
    else delete m.video_shared_settings.spatial_upsampling;
  }
  else if (id.startsWith("cfgFilter")) {
    const vs = (m.video_shared_settings = m.video_shared_settings || {});
    const rawCurUp = String(vs.spatial_upsampling || "").toLowerCase();
    if (["h3facerefine", "seedvr2*1", "flashvsrplus*1", "flashvsrplus2pass*1", "ltx25_refine*1", "dlss5*1"].includes(rawCurUp)) {
      delete vs.spatial_upsampling;
    }
    const f = (vs.filters = vs.filters || {});
    if (id === "cfgFilterAiRefiner") { if (val) f.ai_refiners = [val]; else delete f.ai_refiners; }
    else if (id === "cfgFilterFaceStrength") {
      const fs = parseFloat(val);
      if (!isNaN(fs) && (fs !== 0.75 || f.face_strength !== undefined || f.ai_refiners?.length)) {
        f.face_strength = Math.max(0, Math.min(1, fs));
      }
    }
    else if (id === "cfgFilterFilmGrain") { const g = parseFloat(val) || 0; if (g > 0) f.film_grain_intensity = g; else delete f.film_grain_intensity; }
    else if (id === "cfgFilterWindowCC") { const w = parseFloat(val) || 0; if (w > 0) f.window_color_correction = w; else delete f.window_color_correction; }
    else if (id === "cfgFilterDeband" || id === "cfgFilterCas" || id === "cfgFilterDenoise") {
      const ff = (f.ffmpeg = f.ffmpeg || {});
      if (id === "cfgFilterDeband") { if (el.checked) ff.deband = true; else delete ff.deband; }
      else if (id === "cfgFilterCas") { const c = parseFloat(val) || 0; if (c > 0) ff.cas = c; else delete ff.cas; }
      else if (id === "cfgFilterDenoise") { const d = parseFloat(val) || 0; if (d > 0) ff.denoise = d; else delete ff.denoise; }
      if (Object.keys(ff).length === 0) delete f.ffmpeg;
    }
    if (Object.keys(f).length === 0) delete vs.filters;
  }
  else if (id === "cfgNegativePrompt") {
    m.video_shared_settings = m.video_shared_settings || {};
    m.video_shared_settings.negative_prompt = val;
  }
  if (typeof window.updateMasterResolutionInfoUI === "function") {
    window.updateMasterResolutionInfoUI();
  }
  if (state.selectedSceneIdx !== null && state.projectData?.scenes?.[state.selectedSceneIdx]) {
    const selSc = state.projectData.scenes[state.selectedSceneIdx];
    window.updateInspectorSceneHeader?.(selSc);
    window.populateSceneVideoRenderer?.(selSc);
    window.populateSceneResolutionAndUpsampling?.(selSc);
    window.syncSceneBackendUI?.(selSc);
  }
}

// Mark project dirty when any Project Settings field is edited
const settingsFieldIds = [
  "cfgProjectName", "cfgMasterAudio", "cfgFps", "cfgFadeOut", "cfgFadeOutEnd", "cfgRenderOverlap",
  "cfgDefaultAnchorRenderer", "cfgDefaultAnchorResolution", "cfgDefaultVideoRenderer", "cfgDefaultSpatialUpsampling", "cfgNegativePrompt",
  "cfgMasterResolution", "cfgMasterScaleAlgo", "cfgMasterSharpen", "cfgProgressiveStitchMode",
  "cfgFilterAiRefiner", "cfgFilterFaceStrength", "cfgFilterFilmGrain", "cfgFilterWindowCC",
  "cfgFilterDeband", "cfgFilterCas", "cfgFilterDenoise",
];

settingsFieldIds.forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  const label = id.replace("cfg", "");
  if (el.type === "checkbox" || el.tagName === "SELECT") {
    el.addEventListener("change", () => {
      syncSettingsFormToState(id);
      markProjectDirty();
      window.recordHistorySnapshot?.(`Change Settings ${label}`);
      if ((id === "cfgMasterResolution" || id === "cfgDefaultSpatialUpsampling" || id.startsWith("cfgFilter")) && state.selectedSceneIdx !== null) {
        const selSc = state.projectData?.scenes?.[state.selectedSceneIdx];
        if (selSc) window.populateSceneResolutionAndUpsampling?.(selSc);
      }
    });
  } else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
    el.addEventListener("focus", () => {
      window.beginEditSession?.(`settings:${id}`, `Edit Settings ${label}`);
    });
    el.addEventListener("input", () => {
      window.beginEditSession?.(`settings:${id}`, `Edit Settings ${label}`);
      syncSettingsFormToState(id);
      markProjectDirty();
    });
    el.addEventListener("blur", () => {
      window.commitEditSession?.();
      if (id.startsWith("cfgFilter") && state.selectedSceneIdx !== null) {
        const selSc = state.projectData?.scenes?.[state.selectedSceneIdx];
        if (selSc) window.populateSceneResolutionAndUpsampling?.(selSc);
      }
    });
  }
});

// Save Project Changes
async function saveProjectData(showNotice = true) {
  if (!state.currentSlug || !state.projectData) return false;
  // Sync form values into state safely
  settingsFieldIds.forEach(syncSettingsFormToState);
  if (state.charactersData && Object.keys(state.charactersData).length > 0) {
    state.projectData.characters = state.charactersData;
  }

  const purgeIds = window.collectPendingPurgeSceneIds
    ? window.collectPendingPurgeSceneIds()
    : (state.projectData.scenes || []).filter((s) => s && s._freshSceneTakes && s.id).map((s) => String(s.id).trim());
  const payloadJson = JSON.stringify(state.projectData, (k, v) => (k === "_freshSceneTakes" || k === "_dupPending" || k === "_lockPending" || k === "_customSourceMode" ? undefined : v));
  const putUrl = `/api/project/${state.currentSlug}${purgeIds.length ? `?purge_scenes=${encodeURIComponent(purgeIds.join(","))}` : ""}`;

  state._savingProject = true;
  try {
    const res = await fetch(putUrl, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: payloadJson,
    });
    const json = await res.json();
    if (json.success) {
      const clearedIds = Array.isArray(json.purged_scene_ids) ? json.purged_scene_ids : purgeIds;
      window.purgeLocalSceneAssetsAfterSave?.(clearedIds);
      (state.projectData.scenes || []).forEach((s) => { if (s && s._freshSceneTakes) delete s._freshSceneTakes; });
      if (typeof window.markHistorySaved === "function") {
        window.markHistorySaved();
      }
      markProjectDirty(false);
      state._savingProject = false;
      if (Array.isArray(json.queue)) {
        if (typeof window.applyQueueApiResponse === "function") window.applyQueueApiResponse(json);
        else state.renderQueue = json.queue;
        if (typeof renderQueueUI === "function") renderQueueUI();
      }
      if (clearedIds.length && typeof window.refreshProjectAssets === "function") await window.refreshProjectAssets(true);
      loadProjectQueue(state.currentSlug);
      if (showNotice) showToast("💾 Project saved! (Backup archived in backup/)");
      return true;
    } else {
      state._savingProject = false;
      if (showNotice) alert(`Save failed: ${json.error}`);
      return false;
    }
  } catch (err) {
    state._savingProject = false;
    if (showNotice) alert(`Error saving project: ${err}`);
    return false;
  }
}
window.saveProjectData = saveProjectData;
document.getElementById("btnSaveProject").addEventListener("click", () => saveProjectData(true));

