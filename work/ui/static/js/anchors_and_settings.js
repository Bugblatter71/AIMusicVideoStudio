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
  const menu = document.getElementById("anchorDropdownMenu");
  if (sel) sel.innerHTML = "";
  if (menu) menu.innerHTML = "";

  const anchors = state.projectData?.anchors || [];
  const diskAnchors = state.assets?.anchors || {};

  anchors.forEach((a, idx) => {
    const aid = extractAnchorId(a.filename) || `A${String(idx + 1).padStart(2, "0")}`;
    const diskInfo = diskAnchors[a.filename] || diskAnchors[aid];
    const resolvedName = diskInfo?.filename || a.filename;
    const anchorMtime = diskInfo?.mtime || "";
    const thumbUrl = `/media/${state.currentSlug}/anchor/${encodeURIComponent(resolvedName)}${anchorMtime ? `?t=${anchorMtime}` : ""}`;

    // 1. Populate native select
    if (sel) {
      const opt = document.createElement("option");
      opt.value = a.filename;
      opt.dataset.aid = aid;
      opt.textContent = `[${aid}] ${resolvedName}`;
      sel.appendChild(opt);
    }

    // 2. Populate visual custom dropdown menu item (2-column layout, doubled size)
    if (menu) {
      const item = document.createElement("div");
      item.className = "anchor-dropdown-item";
      item.dataset.value = a.filename;
      item.dataset.aid = aid;

      // Thumbnail (High-res preview, doubled size ~240px x 120px in 2 columns)
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
      img.onerror = () => {
        img.style.display = "none";
      };

      // Label container
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

      item.addEventListener("click", () => {
        if (sel) {
          sel.value = a.filename;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        }
        updateSceneAnchorPreview(a.filename);
        menu.style.display = "none";
        menu.classList.remove("open");
      });

      menu.appendChild(item);
    }
  });

  // Re-sync current selection highlight if a scene is selected
  if (state.selectedSceneIdx !== null && state.projectData?.scenes) {
    const sc = state.projectData.scenes[state.selectedSceneIdx];
    if (sc) {
      updateSceneAnchorPreview(sc.anchor);
    }
  }
}

function initAnchorDropdownTrigger() {
  const btn = document.getElementById("btnAnchorDropdownTrigger");
  const menu = document.getElementById("anchorDropdownMenu");
  const group = document.getElementById("anchorPickerGroup");

  if (!btn || !menu) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isShown = menu.style.display === "grid" || menu.classList.contains("open");
    if (isShown) {
      menu.style.display = "none";
      menu.classList.remove("open");
    } else {
      menu.style.display = "grid";
      menu.classList.add("open");
      const selected = menu.querySelector(".anchor-dropdown-item.selected");
      if (selected) {
        selected.scrollIntoView({ block: "nearest" });
      }
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

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initAnchorDropdownTrigger);
} else {
  initAnchorDropdownTrigger();
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
      markProjectDirty();
      renderResolutionCards(k);
      showToast(`Selected Resolution Option #${k} (${r.width}x${r.height})`);
    });
    container.appendChild(card);
  });
}

// Mark project dirty when any Project Settings field is edited
[
  "cfgProjectName",
  "cfgMasterAudio",
  "cfgFps",
  "cfgFadeOut",
  "cfgRenderOverlap",
  "cfgDefaultAnchorRenderer",
  "cfgDefaultVideoRenderer",
  "cfgNegativePrompt",
].forEach((id) => {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener("input", () => markProjectDirty());
    el.addEventListener("change", () => markProjectDirty());
  }
});

// Save Project Changes
async function saveProjectData(showNotice = true) {
  if (!state.currentSlug || !state.projectData) return false;
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

  state._savingProject = true;
  try {
    const res = await fetch(`/api/project/${state.currentSlug}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.projectData),
    });
    const json = await res.json();
    if (json.success) {
      markProjectDirty(false);
      state._savingProject = false;
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

