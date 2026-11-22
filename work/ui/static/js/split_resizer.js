/**
 * Split-Pane Draggable Resizer, Timeline Fit-to-Song Zoom,
 * and Viewport-Clamped Floating Anchor Pickers.
 */
(function () {
  const STORAGE_KEY = "mvg_split_left_pct";
  const DEFAULT_PCT = 52;
  const MIN_LEFT_PX = 440;
  const MIN_RIGHT_PX = 350;

  function clampPct(pct, containerWidth) {
    if (!containerWidth || containerWidth <= MIN_LEFT_PX + MIN_RIGHT_PX) {
      return Math.max(30, Math.min(72, Number(pct) || DEFAULT_PCT));
    }
    const minPct = (MIN_LEFT_PX / containerWidth) * 100;
    const maxPct = ((containerWidth - MIN_RIGHT_PX) / containerWidth) * 100;
    const raw = Number(pct) || DEFAULT_PCT;
    return Math.max(Math.max(25, minPct), Math.min(Math.min(78, maxPct), raw));
  }

  function applySplitPercent(pct, persist = false) {
    const main = document.getElementById("appMain");
    if (!main) return;
    const lower = document.getElementById("lowerWorkspace");
    const width = lower?.getBoundingClientRect().width || window.innerWidth;
    const clamped = Number(clampPct(pct, width).toFixed(2));
    main.style.setProperty("--split-left-pct", `${clamped}%`);
    if (persist) {
      try {
        localStorage.setItem(STORAGE_KEY, String(clamped));
      } catch (_e) {}
    }
    window.redrawTimeline?.();
  }

  function initWorkspaceSplitter() {
    const splitter = document.getElementById("workspaceSplitter");
    const lower = document.getElementById("lowerWorkspace");
    if (!splitter || !lower || splitter.dataset.wired) return;
    splitter.dataset.wired = "1";

    let saved = DEFAULT_PCT;
    try {
      const raw = parseFloat(localStorage.getItem(STORAGE_KEY));
      if (!isNaN(raw)) saved = raw;
    } catch (_e) {}
    applySplitPercent(saved, false);

    let dragging = false;

    const onMove = (clientX) => {
      if (!dragging) return;
      const rect = lower.getBoundingClientRect();
      if (!rect.width) return;
      const pct = ((clientX - rect.left) / rect.width) * 100;
      applySplitPercent(pct, false);
    };

    const stopDrag = () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      const main = document.getElementById("appMain");
      const cur = parseFloat(main?.style.getPropertyValue("--split-left-pct") || saved);
      applySplitPercent(isNaN(cur) ? DEFAULT_PCT : cur, true);
    };

    splitter.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      dragging = true;
      splitter.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    });

    window.addEventListener("mousemove", (e) => onMove(e.clientX));
    window.addEventListener("mouseup", stopDrag);

    splitter.addEventListener("dblclick", () => {
      applySplitPercent(DEFAULT_PCT, true);
    });

    if (typeof ResizeObserver === "function") {
      const sc = document.getElementById("timelineScrollContainer");
      const ro = new ResizeObserver(() => {
        window.redrawTimeline?.();
        if (window.state && typeof window.updatePlayheadVisual === "function") {
          const audioEl = document.getElementById("audioElement");
          window.updatePlayheadVisual(window.state.playheadTime || audioEl?.currentTime || 0, false);
        }
      });
      if (sc) ro.observe(sc);
      ro.observe(lower);
    }
  }

  function initTimelineFitZoom() {
    const btnFit = document.getElementById("btnTimelineFit");
    if (!btnFit || btnFit.dataset.wired) return;
    btnFit.dataset.wired = "1";

    btnFit.addEventListener("click", () => {
      const sc = document.getElementById("timelineScrollContainer");
      const slider = document.getElementById("timelineZoom");
      const st = window.state;
      if (!sc || !st) return;
      const dur = Math.max(st.audioDuration || st.projectData?.project?.total_duration || 60, 1);
      const basePps = st.pixelsPerSecond || 60;
      const availW = Math.max(320, sc.clientWidth - 20);
      const fitZoom = Math.max(0.2, Math.min(3.5, availW / (dur * basePps)));
      st.zoom = Number(fitZoom.toFixed(2));
      if (slider) {
        if (st.zoom < 1.0) { slider.min = "0.2"; slider.step = "0.05"; }
        slider.value = String(st.zoom);
      }
      sc.scrollLeft = 0;
      window.redrawTimeline?.();
      window.updatePlayheadVisual?.(st.playheadTime || 0, false);
    });
  }

  function positionAnchorDropdownInViewport(btnId, menuId) {
    const btn = document.getElementById(btnId);
    const menu = document.getElementById(menuId);
    if (!btn || !menu || !menu.classList.contains("open")) return;
    const rect = btn.getBoundingClientRect();
    const pad = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const prefW = Math.min(vw - pad * 2, Math.max(rect.width + 120, 420));
    let left = rect.left;
    if (left + prefW > vw - pad) left = Math.max(pad, vw - pad - prefW);
    if (left < pad) left = pad;

    const spaceBelow = vh - rect.bottom - pad;
    const spaceAbove = rect.top - pad;
    const openAbove = spaceBelow < 230 && spaceAbove > spaceBelow;
    const availH = Math.max(180, Math.min(520, openAbove ? spaceAbove - 6 : spaceBelow - 6));

    menu.classList.add("is-floating-fixed");
    menu.style.left = `${Math.round(left)}px`;
    menu.style.width = `${Math.round(prefW)}px`;
    menu.style.maxHeight = `${Math.round(availH)}px`;
    menu.style.right = "auto";
    if (openAbove) {
      menu.style.top = "auto";
      menu.style.bottom = `${Math.round(vh - rect.top + 4)}px`;
    } else {
      menu.style.bottom = "auto";
      menu.style.top = `${Math.round(rect.bottom + 4)}px`;
    }
  }

  function initFloatingAnchorDropdowns() {
    [
      ["btnAnchorDropdownTrigger", "anchorDropdownMenu"],
      ["btnEndAnchorDropdownTrigger", "endAnchorDropdownMenu"],
    ].forEach(([btnId, menuId]) => {
      const btn = document.getElementById(btnId);
      if (!btn || btn.dataset.viewportClampWired) return;
      btn.dataset.viewportClampWired = "1";
      btn.addEventListener("click", () => {
        requestAnimationFrame(() => positionAnchorDropdownInViewport(btnId, menuId));
      });
    });

    window.addEventListener("resize", () => {
      positionAnchorDropdownInViewport("btnAnchorDropdownTrigger", "anchorDropdownMenu");
      positionAnchorDropdownInViewport("btnEndAnchorDropdownTrigger", "endAnchorDropdownMenu");
    });
  }

  function boot() {
    initWorkspaceSplitter();
    initTimelineFitZoom();
    initFloatingAnchorDropdowns();
  }

  window.applySplitPercent = applySplitPercent;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
