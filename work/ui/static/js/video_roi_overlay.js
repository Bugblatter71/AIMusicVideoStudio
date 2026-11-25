// Interactive Split-X & Cropped-ROI Bounding Box Overlay for Master Video and Scene Clip Players
(function () {
  "use strict";

  const RES_MAP = { "1": [832, 480], "2": [960, 544], "3": [1280, 720], "4": [1344, 768], "5": [1536, 864], "6": [1920, 1088] };
  let _guidesEnabled = true;
  let _dragState = null;

  function _snapDim(rawVal, maxVal, align = 32) {
    const step = (align === 32 && maxVal >= 288) ? 32 : 16;
    const maxAligned = maxVal >= step ? Math.floor(maxVal / step) * step : (maxVal & ~1);
    const minDim = Math.min(maxAligned, 256);
    const snapped = Math.floor((Math.round(rawVal) + Math.floor(step / 2)) / step) * step;
    return Math.max(minDim, Math.min(maxAligned, snapped));
  }

  function resolveSceneRenderDims(sc) {
    const rawKey = String(sc?.resolution || window.state?.projectData?.models?.default_video_resolution || "4").trim();
    if (RES_MAP[rawKey]) return RES_MAP[rawKey];
    if (rawKey.includes("x")) {
      const parts = rawKey.toLowerCase().split("x").map((n) => parseInt(n, 10));
      if (parts.length === 2 && parts[0] > 0 && parts[1] > 0) return parts;
    }
    return [1344, 768];
  }

  function computeRoiBoxJS(width, height, splitX = 0.5, side = "right", roiMode = "half_split", customBox = null) {
    const w = Math.max(32, Math.round(Number(width) || 1920));
    const h = Math.max(32, Math.round(Number(height) || 1088));
    const rm = window.normalizeRoiModeJS ? window.normalizeRoiModeJS(roiMode) : (roiMode || "half_split");
    const sd = String(side || "right").toLowerCase().trim() === "left" ? "left" : (String(side || "").toLowerCase().trim() === "custom" ? "custom" : "right");
    const sx = Math.max(0.15, Math.min(0.85, Number.isFinite(Number(splitX)) ? Number(splitX) : 0.5));
    const cbox = window.parseRoiBoxStr ? window.parseRoiBoxStr(customBox) : null;

    if ((rm === "custom" || sd === "custom") && cbox) {
      const [rx0, ry0, rx1, ry1] = cbox;
      const cw = _snapDim(Math.round((rx1 - rx0) * w), w, 32);
      const ch = _snapDim(Math.round((ry1 - ry0) * h), h, 32);
      const cx = Math.max(0, Math.min(w - cw, Math.round(((rx0 + rx1) * 0.5 * w - cw * 0.5) / 2) * 2));
      const cy = Math.max(0, Math.min(h - ch, Math.round(((ry0 + ry1) * 0.5 * h - ch * 0.5) / 2) * 2));
      return { cx, cy, cw, ch, w, h, norm: [cx / w, cy / h, (cx + cw) / w, (cy + ch) / h] };
    }
    if (rm === "full") {
      const cw = Math.floor(w / 16) * 16, ch = Math.floor(h / 16) * 16;
      const cx = Math.max(0, Math.floor((w - cw) / 2)) & ~1, cy = Math.max(0, Math.floor((h - ch) / 2)) & ~1;
      return { cx, cy, cw, ch, w, h, norm: [cx / w, cy / h, (cx + cw) / w, (cy + ch) / h] };
    }
    const splitPx = Math.round(w * sx) & ~1;
    if (rm === "tight_performer") {
      const halfW = sd === "right" ? (w - splitPx) : splitPx;
      const cw = _snapDim(Math.round(Math.max(halfW * 0.90, w * 0.45)), w, 32);
      const ch = _snapDim(Math.round(h * 0.72), h, 32);
      const cx = sd === "right" ? Math.max(0, (w - cw) & ~1) : 0;
      const cy = Math.max(0, Math.min(h - ch, Math.round((h * 0.04) / 2) * 2));
      return { cx, cy, cw, ch, w, h, norm: [cx / w, cy / h, (cx + cw) / w, (cy + ch) / h] };
    }
    const ch = Math.floor(h / 16) * 16;
    const cy = Math.max(0, Math.floor((h - ch) / 2)) & ~1;
    if (sd === "left") {
      const cw = _snapDim(splitPx, w, 32);
      return { cx: 0, cy, cw, ch, w, h, norm: [0, cy / h, cw / w, (cy + ch) / h] };
    }
    const cw = _snapDim(w - splitPx, w, 32);
    const cx = Math.max(0, Math.min(w - cw, (w - cw) & ~1));
    return { cx, cy, cw, ch, w, h, norm: [cx / w, cy / h, (cx + cw) / w, (cy + ch) / h] };
  }
  window.computeRoiBoxJS = computeRoiBoxJS;

  function resolveRoiBorderPxJS(cw, ch, isLtx2) {
    let need = 64;
    if (isLtx2) {
      const dilatePx = Math.floor((6 * Math.max(cw, ch) + 63) / 64);
      need = Math.max(need, Math.floor((dilatePx + 31) / 32) * 32);
    }
    const maxB = Math.min(Math.floor(cw / 5), Math.floor(ch / 5));
    if (need >= 32 && maxB >= 32) return Math.max(32, Math.floor(Math.min(need, maxB) / 32) * 32);
    return Math.max(4, Math.min(need, Math.floor(cw / 8), Math.floor(ch / 8)));
  }
  window.resolveRoiBorderPxJS = resolveRoiBorderPxJS;

  function _isRepairBarOpen() {
    const bar = document.getElementById("lipSyncRepairBar");
    return Boolean(bar && bar.style && bar.style.display !== "none");
  }

  function _resolveMasterActiveScene() {
    const scenes = window.state?.projectData?.scenes || [];
    if (!scenes.length) return { sc: null, idx: -1 };
    const mv = document.getElementById("masterVideoPlayer");
    const t = (mv && !mv.paused && Number.isFinite(mv.currentTime) && mv.currentTime > 0)
      ? Number(mv.currentTime)
      : Number(window.state?.playheadTime ?? -1);
    if (Number.isFinite(t) && t >= 0) {
      const hitIdx = scenes.findIndex((s) => t >= Number(s.start || 0) && t < Number(s.end || 0));
      if (hitIdx !== -1) return { sc: scenes[hitIdx], idx: hitIdx };
    }
    const selIdx = Number(window.state?.selectedSceneIdx ?? -1);
    if (selIdx >= 0 && scenes[selIdx]) return { sc: scenes[selIdx], idx: selIdx };
    return { sc: scenes[0], idx: 0 };
  }

  function _computeVideoStageStyle(videoEl, overlayEl) {
    if (!videoEl || !overlayEl || typeof videoEl.getBoundingClientRect !== "function") {
      return "position:absolute;inset:0;pointer-events:none;overflow:hidden;";
    }
    const vRect = videoEl.getBoundingClientRect();
    const oRect = overlayEl.getBoundingClientRect();
    if (!vRect.width || !vRect.height || !oRect.width || !oRect.height) {
      return "position:absolute;inset:0;pointer-events:none;overflow:hidden;";
    }
    const vw = Number(videoEl.videoWidth) || 1920;
    const vh = Number(videoEl.videoHeight) || 1088;
    const videoAspect = vw / Math.max(1, vh);
    const boxAspect = vRect.width / Math.max(1, vRect.height);
    let drawW = vRect.width, drawH = vRect.height;
    if (boxAspect > videoAspect) {
      drawH = vRect.height;
      drawW = drawH * videoAspect;
    } else {
      drawW = vRect.width;
      drawH = drawW / videoAspect;
    }
    const left = (vRect.left - oRect.left) + (vRect.width - drawW) * 0.5;
    const top = (vRect.top - oRect.top) + (vRect.height - drawH) * 0.5;
    return `position:absolute;left:${left.toFixed(1)}px;top:${top.toFixed(1)}px;width:${drawW.toFixed(1)}px;height:${drawH.toFixed(1)}px;pointer-events:none;overflow:hidden;`;
  }

  function _buildOverlayHTML(sc, targetKind, sxOverride, stageStyle) {
    const isTwo = Boolean(window.isTwoPassScene?.(sc));
    const repairOpen = _isRepairBarOpen();
    if (!sc || (!_guidesEnabled) || (!isTwo && !repairOpen)) return "";

    const b2 = window.resolveDuetPassBackend ? window.resolveDuetPassBackend(sc, 2) : "local";
    const spec2 = window.resolveDuetPassVideoSpec ? window.resolveDuetPassVideoSpec(sc, 2) : {};
    const isLtx2P2 = b2 === "local" && Boolean(window.isLtx2MvgaSpec?.(spec2));
    const sx = Math.max(0.15, Math.min(0.85, Number.isFinite(sxOverride) ? sxOverride : Number(sc.split_x ?? 0.5)));
    const pct = (sx * 100).toFixed(2);
    const [rw, rh] = resolveSceneRenderDims(sc);
    const locked = Boolean(sc.locked);

    const roiMode = repairOpen
      ? (document.getElementById("repRoiMode")?.value || "half_split")
      : (window.normalizeRoiModeJS ? window.normalizeRoiModeJS(sc.pass2_roi_mode) : (sc.pass2_roi_mode || "full"));
    const roiSide = repairOpen
      ? (document.getElementById("repSide")?.value || "right")
      : (sc.pass2_roi_side === "left" ? "left" : "right");
    const showSplitGuide = isTwo && !repairOpen && (roiMode === "full" || (roiMode === "half_split" && roiSide !== "custom"));
    const showMvgaSplitBand = showSplitGuide && roiMode === "full" && isLtx2P2;

    let html = `<div class="roi-overlay-stage" data-target="${targetKind}" data-scene-id="${sc.id || ""}" style="${stageStyle}">`;

    // 1. Split-X Line & LTX-2 ~10% Blend Zone (only for modes that actually split along Split X: Full Frame / Half Split)
    if (showSplitGuide) {
      const zoneL = (Math.max(0, sx - 0.1) * 100).toFixed(2);
      const zoneW = ((sx - Math.max(0, sx - 0.1)) * 100).toFixed(2);
      if (showMvgaSplitBand) {
        html += `<div style="position:absolute;top:0;bottom:0;left:${zoneL}%;width:${zoneW}%;background:rgba(245,158,11,0.16);border-left:1px dotted rgba(245,158,11,0.6);"></div>`;
      }
      html += `<div style="position:absolute;top:0;bottom:0;left:${pct}%;border-left:2px dashed rgba(245,158,11,0.92);"></div>`;
      html += `<div class="roi-drag-handle" data-handle="split_x" data-target="${targetKind}" title="Drag left/right to move ${sc.id} Split X (${Math.round(sx * 100)}%)" style="position:absolute;top:0;bottom:38px;left:calc(${pct}% - 8px);width:16px;pointer-events:${locked ? "none" : "auto"};cursor:ew-resize;z-index:6;display:flex;align-items:flex-start;justify-content:center;">`
        + `<span style="margin-top:6px;background:rgba(15,23,42,0.88);border:1px solid rgba(245,158,11,0.75);color:#fbbf24;font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;white-space:nowrap;box-shadow:0 2px 6px rgba(0,0,0,0.6);">⇄ ${sc.id} Split X ${Math.round(sx * 100)}%${showMvgaSplitBand ? " · MVGA ~10% blend" : ""}</span>`
        + `</div>`;
    }

    // 2. Bounding Box (when Pass 2 ROI mode != full OR Lip-Sync Repair bar is open)
    const customBoxRaw = repairOpen
      ? (document.getElementById("repRoiBox")?.value || [0.45, 0.05, 0.98, 0.85])
      : (sc.pass2_roi_box || [0.45, 0.05, 0.98, 0.85]);

    if (roiMode !== "full" || repairOpen) {
      const box = computeRoiBoxJS(rw, rh, sx, roiSide, roiMode, customBoxRaw);
      const x0Pct = (box.cx / box.w * 100).toFixed(2);
      const y0Pct = (box.cy / box.h * 100).toFixed(2);
      const wPct = (box.cw / box.w * 100).toFixed(2);
      const hPct = (box.ch / box.h * 100).toFixed(2);
      const borderPx = resolveRoiBorderPxJS(box.cw, box.ch, isLtx2P2);
      const bLeftPct = box.cx > 0 ? (borderPx / box.cw * 100).toFixed(1) : "0";
      const bRightPct = (box.cx + box.cw) < box.w ? (borderPx / box.cw * 100).toFixed(1) : "0";
      const bTopPct = box.cy > 0 ? (borderPx / box.ch * 100).toFixed(1) : "0";
      const bBotPct = (box.cy + box.ch) < box.h ? (borderPx / box.ch * 100).toFixed(1) : "0";
      const boxColor = repairOpen ? "#34d399" : "#38bdf8";
      const boxBg = repairOpen ? "rgba(16,185,129,0.10)" : "rgba(56,189,248,0.10)";
      const modeLbl = repairOpen ? `Repair ROI (${roiMode})` : (roiMode === "tight_performer" ? "Tight ROI" : (roiMode === "custom" ? "Custom ROI" : "Half Split ROI"));
      const sidPrefix = showSplitGuide ? "" : `${sc.id} · `;
      const mvgaTag = (isLtx2P2 && !repairOpen) ? ` · MVGA ${borderPx}px seam` : "";

      html += `<div class="roi-box-frame" data-roi-mode="${roiMode}" style="position:absolute;left:${x0Pct}%;top:${y0Pct}%;width:${wPct}%;height:${hPct}%;border:2px solid ${boxColor};background:${boxBg};box-sizing:border-box;z-index:5;pointer-events:none;">`
        + `<div title="${borderPx}px 0-locked seam border" style="position:absolute;left:${bLeftPct}%;right:${bRightPct}%;top:${bTopPct}%;bottom:${bBotPct}%;border:1px dashed rgba(255,255,255,0.42);pointer-events:none;"></div>`
        + `<div class="roi-drag-handle" data-handle="box_move" data-target="${targetKind}" title="Drag to move ROI bounding box (${box.cw}x${box.ch})" style="position:absolute;bottom:6px;left:6px;background:rgba(15,23,42,0.9);border:1px solid ${boxColor};color:${boxColor};font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;white-space:nowrap;pointer-events:${locked ? "none" : "auto"};cursor:move;">✥ ${sidPrefix}${modeLbl} ${box.cw}×${box.ch}${mvgaTag}</div>`;

      if (!locked) {
        const edges = [
          ["box_w", "left:-5px;top:10px;bottom:10px;width:10px;cursor:ew-resize;"],
          ["box_e", "right:-5px;top:10px;bottom:10px;width:10px;cursor:ew-resize;"],
          ["box_n", "top:-5px;left:10px;right:10px;height:10px;cursor:ns-resize;"],
          ["box_s", "bottom:-5px;left:10px;right:10px;height:10px;cursor:ns-resize;"],
          ["box_nw", "left:-6px;top:-6px;width:12px;height:12px;cursor:nwse-resize;background:" + boxColor + ";border-radius:50%;"],
          ["box_ne", "right:-6px;top:-6px;width:12px;height:12px;cursor:nesw-resize;background:" + boxColor + ";border-radius:50%;"],
          ["box_sw", "left:-6px;bottom:-6px;width:12px;height:12px;cursor:nesw-resize;background:" + boxColor + ";border-radius:50%;"],
          ["box_se", "right:-6px;bottom:-6px;width:12px;height:12px;cursor:nwse-resize;background:" + boxColor + ";border-radius:50%;"],
        ];
        edges.forEach(([hType, css]) => {
          html += `<div class="roi-drag-handle" data-handle="${hType}" data-target="${targetKind}" style="position:absolute;${css}pointer-events:auto;"></div>`;
        });
      }
      html += `</div>`;
    }

    html += `</div>`;
    return html;
  }

  function syncVideoRoiOverlays(scOverride = null, sxOverride = undefined) {
    // 1. Scene Clip Preview Overlay (#clipSplitOverlay)
    const clipOv = document.getElementById("clipSplitOverlay");
    const clipVid = document.getElementById("clipVideoPlayer");
    const selSc = scOverride || window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
    if (clipOv) {
      const stageStyle = _computeVideoStageStyle(clipVid, clipOv);
      const html = _buildOverlayHTML(selSc, "clip", sxOverride, stageStyle);
      if (!html) {
        clipOv.style.display = "none";
      } else {
        clipOv.style.display = "block";
        clipOv.innerHTML = html;
      }
    }

    // 2. Master Video Overlay (#masterRoiOverlay)
    const masterOv = document.getElementById("masterRoiOverlay");
    const masterVid = document.getElementById("masterVideoPlayer");
    if (masterOv) {
      const { sc: masterSc } = _resolveMasterActiveScene();
      const targetSc = masterSc || selSc;
      const useSx = (targetSc && selSc && targetSc.id === selSc.id) ? sxOverride : undefined;
      const stageStyle = _computeVideoStageStyle(masterVid, masterOv);
      const html = _buildOverlayHTML(targetSc, "master", useSx, stageStyle);
      if (!html) {
        masterOv.style.display = "none";
        masterOv.innerHTML = "";
      } else {
        masterOv.style.display = "block";
        masterOv.innerHTML = html;
      }
    }
  }
  window.syncVideoRoiOverlays = syncVideoRoiOverlays;

  function _applySplitXUpdate(sc, newSx) {
    const clamped = Number(Math.max(0.15, Math.min(0.85, newSx)).toFixed(2));
    sc.split_x = clamped;
    const inp = document.getElementById("sceneSplitX");
    const rng = document.getElementById("sceneSplitXRange");
    const lbl = document.getElementById("sceneSplitXVal");
    if (inp) inp.value = clamped;
    if (rng) rng.value = clamped;
    if (lbl) lbl.textContent = clamped.toFixed(2);
    window.markProjectDirty?.();
    syncVideoRoiOverlays(sc, clamped);
  }

  function _applyRoiBoxUpdate(sc, nextBox, isRepair) {
    const clamped = [
      Number(Math.max(0, Math.min(0.92, nextBox[0])).toFixed(4)),
      Number(Math.max(0, Math.min(0.92, nextBox[1])).toFixed(4)),
      Number(Math.max(0.08, Math.min(1, nextBox[2])).toFixed(4)),
      Number(Math.max(0.08, Math.min(1, nextBox[3])).toFixed(4)),
    ];
    if (clamped[2] <= clamped[0] + 0.06) clamped[2] = Number(Math.min(1, clamped[0] + 0.08).toFixed(4));
    if (clamped[3] <= clamped[1] + 0.06) clamped[3] = Number(Math.min(1, clamped[1] + 0.08).toFixed(4));

    if (isRepair) {
      const rmSel = document.getElementById("repRoiMode");
      const bw = document.getElementById("repBoxWrap");
      const boxInp = document.getElementById("repRoiBox");
      if (rmSel) rmSel.value = "custom";
      if (bw) bw.style.display = "flex";
      if (boxInp) boxInp.value = clamped.join(", ");
    } else {
      sc.pass2_roi_mode = "custom";
      sc.pass2_roi_box = clamped;
      const selRoi = document.getElementById("scenePass2Roi");
      const sideWrap = document.getElementById("scenePass2RoiSideWrap");
      const boxWrap = document.getElementById("scenePass2RoiBoxWrap");
      const boxInp = document.getElementById("scenePass2RoiBox");
      if (selRoi) selRoi.value = "custom";
      if (sideWrap) sideWrap.style.display = "flex";
      if (boxWrap) boxWrap.style.display = "flex";
      if (boxInp) boxInp.value = clamped.join(", ");
      window.markProjectDirty?.();
    }
    syncVideoRoiOverlays(sc);
  }

  function _onPointerDown(ev) {
    const handleEl = ev.target?.closest?.(".roi-drag-handle");
    if (!handleEl) return;
    const handle = handleEl.dataset?.handle;
    const targetKind = handleEl.dataset?.target || "clip";
    if (!handle) return;

    const resolved = targetKind === "master"
      ? _resolveMasterActiveScene()
      : { sc: window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx], idx: Number(window.state?.selectedSceneIdx ?? -1) };
    const sc = resolved.sc;
    if (!sc || sc.locked) return;

    if (resolved.idx >= 0 && window.state && window.state.selectedSceneIdx !== resolved.idx) {
      window.selectScene?.(resolved.idx);
    }

    const stageEl = handleEl.closest?.(".roi-overlay-stage") || handleEl.parentElement;
    const rect = stageEl?.getBoundingClientRect?.() || { left: 0, top: 0, width: 340, height: 190 };
    if (!rect.width || !rect.height) return;

    ev.preventDefault?.();
    ev.stopPropagation?.();

    const isRepair = _isRepairBarOpen();
    const [rw, rh] = resolveSceneRenderDims(sc);
    const sx = Number(sc.split_x ?? 0.5);
    const roiMode = isRepair
      ? (document.getElementById("repRoiMode")?.value || "half_split")
      : (window.normalizeRoiModeJS ? window.normalizeRoiModeJS(sc.pass2_roi_mode) : (sc.pass2_roi_mode || "full"));
    const roiSide = isRepair
      ? (document.getElementById("repSide")?.value || "right")
      : (sc.pass2_roi_side === "left" ? "left" : "right");
    const customBoxRaw = isRepair
      ? (document.getElementById("repRoiBox")?.value || [0.45, 0.05, 0.98, 0.85])
      : (sc.pass2_roi_box || [0.45, 0.05, 0.98, 0.85]);
    const startBox = computeRoiBoxJS(rw, rh, sx, roiSide, roiMode === "full" ? "half_split" : roiMode, customBoxRaw).norm;

    if (handle === "split_x") {
      window.beginEditSession?.(`split_x:${sc.id}`, `Drag ${sc.id} Split X`);
    } else if (!isRepair) {
      window.beginEditSession?.(`roi_box:${sc.id}`, `Drag ${sc.id} Pass 2 ROI Box`);
    }

    _dragState = {
      handle,
      sc,
      isRepair,
      rect,
      startX: Number(ev.clientX) || 0,
      startY: Number(ev.clientY) || 0,
      startSx: sx,
      startBox: [...startBox],
      mutated: false,
    };
  }

  function _onPointerMove(ev) {
    if (!_dragState) return;
    const { handle, sc, isRepair, rect, startX, startY, startBox } = _dragState;
    const clientX = Number(ev.clientX) || 0;
    const clientY = Number(ev.clientY) || 0;
    _dragState.mutated = true;

    if (handle === "split_x") {
      const relX = (clientX - rect.left) / Math.max(1, rect.width);
      _applySplitXUpdate(sc, relX);
      return;
    }

    const dx = (clientX - startX) / Math.max(1, rect.width);
    const dy = (clientY - startY) / Math.max(1, rect.height);
    let [x0, y0, x1, y1] = startBox;

    if (handle === "box_move") {
      const bw = x1 - x0, bh = y1 - y0;
      x0 = Math.max(0, Math.min(1 - bw, x0 + dx));
      y0 = Math.max(0, Math.min(1 - bh, y0 + dy));
      x1 = x0 + bw;
      y1 = y0 + bh;
    } else {
      if (handle.includes("w")) x0 = Math.max(0, Math.min(x1 - 0.08, x0 + dx));
      if (handle.includes("e")) x1 = Math.min(1, Math.max(x0 + 0.08, x1 + dx));
      if (handle.includes("n")) y0 = Math.max(0, Math.min(y1 - 0.08, y0 + dy));
      if (handle.includes("s")) y1 = Math.min(1, Math.max(y0 + 0.08, y1 + dy));
    }
    _applyRoiBoxUpdate(sc, [x0, y0, x1, y1], isRepair);
  }

  function _onPointerUp() {
    if (!_dragState) return;
    const { sc, mutated, handle, isRepair } = _dragState;
    _dragState = null;
    if (!mutated) {
      window.commitEditSession?.();
      return;
    }
    if (typeof window.commitEditSession === "function") {
      window.commitEditSession();
    } else if (!isRepair && typeof window.recordHistorySnapshot === "function") {
      window.recordHistorySnapshot(handle === "split_x" ? `Drag ${sc.id} Split X` : `Drag ${sc.id} Pass 2 ROI Box`);
    }
    window.syncDuetPassControlsUI?.(sc);
    window.renderSceneList?.();
    window.redrawTimeline?.();
    syncVideoRoiOverlays(sc);
  }

  function initVideoRoiOverlays() {
    if (typeof document === "undefined") return;
    const toggleBtn = document.getElementById("btnToggleMasterRoiOverlay");
    if (toggleBtn && !toggleBtn.dataset?.roiBound) {
      if (toggleBtn.dataset) toggleBtn.dataset.roiBound = "1";
      toggleBtn.addEventListener("click", () => {
        _guidesEnabled = !_guidesEnabled;
        toggleBtn.textContent = _guidesEnabled ? "📐 Guides: On" : "📐 Guides: Off";
        toggleBtn.style.opacity = _guidesEnabled ? "1" : "0.65";
        syncVideoRoiOverlays();
      });
    }

    ["clipSplitOverlay", "masterRoiOverlay"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && !el.dataset?.roiDragBound) {
        if (el.dataset) el.dataset.roiDragBound = "1";
        el.addEventListener("mousedown", _onPointerDown);
      }
    });

    if (!document.body?.dataset?.roiGlobalDragBound) {
      if (document.body?.dataset) document.body.dataset.roiGlobalDragBound = "1";
      document.addEventListener?.("mousemove", _onPointerMove);
      document.addEventListener?.("mouseup", _onPointerUp);
    }

    ["masterVideoPlayer", "clipVideoPlayer", "audioElement"].forEach((id) => {
      const mediaEl = document.getElementById(id);
      if (mediaEl && !mediaEl.dataset?.roiMediaBound) {
        if (mediaEl.dataset) mediaEl.dataset.roiMediaBound = "1";
        ["timeupdate", "seeked", "loadedmetadata", "play", "pause"].forEach((evName) => {
          mediaEl.addEventListener(evName, () => { if (!_dragState) syncVideoRoiOverlays(); });
        });
      }
    });

    ["repSide", "repRoiMode", "repRoiBox", "btnToggleLipSyncRepair", "btnCloseLipSyncRepair"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && !el.dataset?.roiRepBound) {
        if (el.dataset) el.dataset.roiRepBound = "1";
        ["change", "input", "click"].forEach((evName) => {
          el.addEventListener(evName, () => setTimeout(() => syncVideoRoiOverlays(), 0));
        });
      }
    });

    if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
      window.addEventListener("resize", () => { if (!_dragState) syncVideoRoiOverlays(); });
    }
    syncVideoRoiOverlays();
  }
  window.initVideoRoiOverlays = initVideoRoiOverlays;

  // Hook syncTakeRepairUI so opening the repair drawer binds its inputs immediately
  const _origSyncTakeRepairUI = window.syncTakeRepairUI;
  if (typeof _origSyncTakeRepairUI === "function") {
    window.syncTakeRepairUI = function (scene) {
      const res = _origSyncTakeRepairUI.apply(this, arguments);
      initVideoRoiOverlays();
      return res;
    };
  }

  initVideoRoiOverlays();
})();
