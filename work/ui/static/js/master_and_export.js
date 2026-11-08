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

// Global Exports
window.triggerImmediateRestitch = triggerImmediateRestitch;
window.updateMasterVideoUI = updateMasterVideoUI;
window.switchTab = switchTab;

// Event Listeners
document.getElementById("btnStitchMaster")?.addEventListener("click", () => triggerImmediateRestitch());
document.getElementById("btnRefreshMasterVideo")?.addEventListener("click", () => {
  updateMasterVideoUI();
  showToast("Master video reloaded");
});
document.getElementById("btnStitchMasterFromTab")?.addEventListener("click", () => {
  triggerImmediateRestitch();
});
