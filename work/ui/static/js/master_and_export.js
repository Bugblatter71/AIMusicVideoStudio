// ============================================================================
// Master Video Assembly, Video Player & Tab Navigation
// ============================================================================

async function triggerImmediateRestitch() {
  if (state.isDirty) {
    showToast("💾 Auto-saving project changes before restitch...");
    const saved = await saveProjectData(false);
    if (!saved) {
      alert("Auto-save failed; aborting restitch so unsaved changes are not ignored.");
      return;
    }
  }
  const btn2 = document.getElementById("btnStitchMasterFromTab");
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
      if (btn2) {
        btn2.textContent = btn2.dataset.origText || "🧵 Stitch Master Video";
        btn2.disabled = false;
      }
    }, 1500);
  }
}

function updateMasterResolutionInfoUI() {
  const p = state.projectData?.project || {};
  const m = state.projectData?.models || {};
  const resMap = { "1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1080" };
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
      infoBadge.textContent = `Render: ${renderRes} → Master: ${targetMaster} (1:1 Native)`;
    } else {
      const sharpTxt = sharpen > 0 ? ` + Sharpen ${sharpen}` : "";
      infoBadge.textContent = `Render: ${renderRes} → Master: ${targetMaster} (${algo}${sharpTxt})`;
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

function updateMasterVideoUI() {
  updateMasterResolutionInfoUI();
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

function switchTab(tabName) {
  const main = document.getElementById("appMain");
  const sidePane = document.getElementById("sidePane");
  const sidePaneTitle = document.getElementById("sidePaneTitle");

  const tabTitles = {
    characters: "👤 Characters & Reference Library",
    anchors: "🖼️ Scene Anchors",
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

    if (tabName === "characters") {
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

// Global Exports
window.triggerImmediateRestitch = triggerImmediateRestitch;
window.updateMasterVideoUI = updateMasterVideoUI;
window.switchTab = switchTab;

// Event Listeners
document.getElementById("btnRefreshMasterVideo")?.addEventListener("click", () => {
  updateMasterVideoUI();
  showToast("Master video reloaded");
});
document.getElementById("btnStitchMasterFromTab")?.addEventListener("click", () => {
  triggerImmediateRestitch();
});
