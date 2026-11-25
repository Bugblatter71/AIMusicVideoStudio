// Cross-Engine Two-Pass Duet Controls, ROI Selection & Post-Render Lip-Sync Repair UI
(function () {
  "use strict";

  const TWO_PASS_MODES = new Set(["duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right"]);
  const ENGINE_OPTS = [
    ["", "Inherit"],
    ["local", "🖥️ Local GPU"],
    ["fal", "☁️ fal.ai"],
    ["minimax", "🎬 MiniMax"],
  ];
  const ROI_OPTS = [
    ["", "Full Frame (Split-Masked)"],
    ["half_split", "Half Split (Fast ROI)"],
    ["tight_performer", "Tight Singer (Fastest ROI)"],
    ["custom", "Custom ROI Box"],
  ];

  function _esc(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function isTwoPassScene(sc) {
    if (!sc) return false;
    return TWO_PASS_MODES.has(String(sc.mode || "").toLowerCase().trim()) || (!sc.mode && (sc.prompt_m !== undefined || sc.prompt_f !== undefined));
  }
  window.isTwoPassScene = isTwoPassScene;

  function normalizeRoiModeJS(raw) {
    const s = String(raw || "").toLowerCase().trim();
    if (s === "half_split" || s === "half" || s === "split" || s === "roi") return "half_split";
    if (s === "tight_performer" || s === "tight" || s === "face") return "tight_performer";
    if (s === "custom") return "custom";
    return "full";
  }

  function parseRoiBoxStr(raw) {
    let nums;
    if (Array.isArray(raw) && raw.length === 4) {
      nums = raw.map((v) => (v === "" || v === null || typeof v === "boolean") ? NaN : Number(v));
    } else {
      const tokens = String(raw || "").split(",").map((x) => x.trim());
      if (tokens.length !== 4 || tokens.some((t) => !t)) return null;
      nums = tokens.map((t) => Number(t));
    }
    if (nums.some((n) => !Number.isFinite(n) || n < 0 || n > 1)) return null;
    const clamped = nums.map((n) => Number(n.toFixed(4)));
    return (clamped[2] > clamped[0] + 0.05 && clamped[3] > clamped[1] + 0.05) ? clamped : null;
  }

  const _cleanRid = (v) => (v !== null && v !== undefined && String(v).trim() !== "" && !["0", "none", "null", "default"].includes(String(v).trim().toLowerCase())) ? String(v).trim() : "";
  function resolvePassRendererId(sc, passNum) {
    return _cleanRid(passNum === 1 ? sc?.renderer_m : (passNum === 2 ? sc?.renderer_f : null))
      || _cleanRid(sc?.renderer)
      || String(window.state?.projectData?.models?.default_video_renderer || "1");
  }
  window.resolveDuetPassRenderer = resolvePassRendererId;

  function _rendererShortLabel(rId) {
    const m = window.state?.models?.video_renderers?.[rId] || window.state?.models?.fal_video_renderers?.[rId] || window.state?.models?.minimax_video_renderers?.[rId];
    return /^\d+$/.test(rId) ? `#${rId} ${m?.name || ""}`.trim() : (m?.name || rId);
  }

  function resolvePassBackend(sc, passNum) {
    if (!sc) return window.getEffectiveProjectBackend?.() || "local";
    const vrId = resolvePassRendererId(sc, passNum);
    if (window.state?.models?.fal_video_renderers?.[vrId]) return "fal";
    if (window.state?.models?.minimax_video_renderers?.[vrId]) return "minimax";
    const direct = String(passNum === 1 ? (sc.backend_m || "") : (sc.backend_f || "")).trim().toLowerCase();
    if (direct === "local" || direct === "fal" || direct === "minimax") return direct;
    const scB = String(sc.backend || "").trim().toLowerCase();
    if (scB === "local" || scB === "fal" || scB === "minimax") return scB;
    return window.getEffectiveProjectBackend?.() || "local";
  }
  window.resolveDuetPassBackend = resolvePassBackend;

  function _notifySceneUpdated(sc, label) {
    window.markProjectDirty?.();
    if (label) window.recordHistorySnapshot?.(label);
    window.updateInspectorSceneHeader?.(sc);
    window.updatePromptReferenceTray?.(sc);
    window.renderSceneList?.();
    window.syncSceneBackendUI?.(sc);
    window.refreshTakeDiffUI?.();
  }

  const COMBO_WARN_STORAGE_KEY = "mvg_dismissed_combo_warnings_v1";
  const _sessionDismissedComboWarnings = new Set();
  let _comboWarnModalCtx = null;

  function _getDismissedComboWarnings() {
    const out = new Set(_sessionDismissedComboWarnings);
    try {
      if (typeof localStorage !== "undefined") {
        const raw = localStorage.getItem(COMBO_WARN_STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        if (Array.isArray(parsed)) parsed.forEach((id) => out.add(String(id)));
      }
    } catch (e) {}
    return out;
  }

  function _dismissComboWarnings(ids) {
    const merged = _getDismissedComboWarnings();
    (ids || []).forEach((id) => {
      const s = String(id || "");
      if (s) {
        _sessionDismissedComboWarnings.add(s);
        merged.add(s);
      }
    });
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem(COMBO_WARN_STORAGE_KEY, JSON.stringify([...merged]));
      }
    } catch (e) {}
  }

  function isComboWarningDismissed(id) {
    return _getDismissedComboWarnings().has(String(id || ""));
  }

  function resetComboWarnings() {
    _sessionDismissedComboWarnings.clear();
    try {
      if (typeof localStorage !== "undefined") localStorage.removeItem(COMBO_WARN_STORAGE_KEY);
    } catch (e) {}
  }
  window.resetComboWarnings = resetComboWarnings;

  function _resolvePassVideoSpec(sc, passNum) {
    const rId = resolvePassRendererId(sc, passNum), m = window.state?.models || {};
    return m.fal_video_renderers?.[rId] || m.minimax_video_renderers?.[rId] || m.video_renderers?.[rId] || {};
  }
  function _isLtx2MvgaSpec(spec) {
    if (spec?.supports_pass2_inpaint === false || window.getVideoRefMode?.(spec) === "ltx2_msr") return false;
    const mt = String(spec?.model_type || "").toLowerCase(), vpt = String(spec?.pass2_video_prompt_type || spec?.video_prompt_type || "").toUpperCase();
    return mt.includes("ltx2") || vpt.startsWith("M");
  }
  const _isLtx2MsrSpec = (spec) => window.getVideoRefMode ? window.getVideoRefMode(spec) === "ltx2_msr" : String(spec?.model_type || "").toLowerCase().includes("msr");
  const _ltx2MvgaWarning = (isRepair) => ({
    id: "ltx2_duet_mvga_seam",
    title: isRepair ? "LTX-2 ROI repair (MVGA) erases interior & blends ~10% border" : "LTX-2 Pass 2 (MVGA) erases masked half & blends ~180px across Split X",
    body: isRepair
      ? "LTX-2 Inpainting IC-LoRA replaces the repaired interior with #66FF00 after Frame 0 and Laplacian-blends ~10% of crop width along border edges."
      : "Even in Full Frame, LTX-2 Inpainting IC-LoRA replaces the Pass 2 masked side with solid #66FF00 after Frame 0 and Laplacian-blends ~10% width (~180px at 1920px) across Split X, which can split crossing arms or instruments.",
    recommendation: "Keep subjects clear of Split X, switch to Stereo Duet (duet_method_a), or use MiniMax H3 (#1/#9) / fal.ai.",
  });

  function evaluateComboWarnings(sc, patch = null, repairOpts = null) {
    const warnings = [];
    if (repairOpts && repairOpts.isRepair) {
      const repB = String(repairOpts.backend || "local").trim().toLowerCase();
      const repRoi = normalizeRoiModeJS(repairOpts.roi_mode || "half_split");
      if (repB === "minimax") {
        warnings.push({
          id: "minimax_pass2_roi",
          title: "MiniMax API ROI ignores Pass 1 camera motion",
          body: "MiniMax Direct API cannot take a video guide or spatial mask, so ROI repair regenerates the cropped box from Frame 0 alone and can seam or disconnect if the camera moves.",
          recommendation: "Use Local GPU (Full Frame render) or fal.ai (Sync-v2 ROI) instead.",
        });
      } else if (repB === "local" && (repRoi === "tight_performer" || repRoi === "custom")) {
        warnings.push({
          id: "local_tight_roi",
          title: "Tight / Custom ROI on Local GPU can flicker & seam",
          body: "Narrow sub-crops on Local GPU often show a periodic ~0.7s (17-frame) brightness pulse inside the box and visible seams where camera motion or background objects cross the crop edges.",
          recommendation: "Use Full Frame Pass 2 on Local GPU, or fal.ai (Sync-v2 ROI) for lip-sync repair.",
        });
      } else if (repB === "local" && repRoi === "half_split") {
        warnings.push({
          id: "local_half_roi",
          title: "Half Split ROI on Local GPU is sensitive to camera drift",
          body: "Half Split locks a 64px vertical seam near Split X. Camera drift or background movement crossing center can blur or split along the seam.",
          recommendation: "Best for static camera shots; use Full Frame Pass 2 when the camera moves.",
        });
      }
      if (repB === "local" && _isLtx2MvgaSpec(_resolvePassVideoSpec(sc, repairOpts.side === "left" ? 1 : 2))) warnings.push(_ltx2MvgaWarning(true));
      return warnings;
    }

    if (!sc) return warnings;
    const cand = patch ? Object.assign({}, sc) : sc;
    if (patch) {
      Object.entries(patch).forEach(([k, v]) => {
        if (v === undefined) delete cand[k];
        else cand[k] = v;
      });
    }

    const twoPass = isTwoPassScene(cand);
    const b1 = twoPass ? resolvePassBackend(cand, 1) : (window.resolveEffectiveSceneBackend?.(cand) || "local");
    const b2 = twoPass ? resolvePassBackend(cand, 2) : b1;
    const roiMode = normalizeRoiModeJS(cand.pass2_roi_mode);

    if (twoPass) {
      const p2Spec = _resolvePassVideoSpec(cand, 2);
      const isLtx2P2 = b2 === "local" && _isLtx2MvgaSpec(p2Spec);
      if (_isLtx2MsrSpec(p2Spec)) warnings.push({ id: "ltx2_msr_pass2", title: "LTX-2.5 MSR (#19) cannot run Two-Pass Pass 2", body: "MSR forbids video_guide/video_mask inpainting on Pass 2.", recommendation: "Use #14/#16/#20 Ingredients or MiniMax H3 for Pass 2." });
      if (b2 === "minimax") {
        warnings.push({
          id: "minimax_pass2_roi",
          title: "MiniMax API Pass 2 ignores Pass 1 camera motion",
          body: "MiniMax Direct API has no video-guide or mask input, so Pass 2 regenerates from Frame 0 only and ignores Pass 1 camera drift/motion across boundaries.",
          recommendation: "Set Pass 2 Engine to Local GPU (Full Frame) or fal.ai (Sync-v2 ROI).",
        });
      } else if (b2 === "local" && (roiMode === "tight_performer" || roiMode === "custom")) {
        warnings.push({
          id: "local_tight_roi",
          title: "Tight / Custom ROI on Local GPU can flicker & seam",
          body: "Narrow sub-crops on Local GPU often show a ~0.7s (17-frame) brightness pulse inside the masked box and visible seams if the camera moves or background elements cross the edges.",
          recommendation: isLtx2P2 ? "Switch Pass 2 to #1/#9, fal.ai (Sync-v2 ROI), or Stereo Duet (duet_method_a)." : "Use Full Frame (Split-Masked) on Local GPU, or switch Pass 2 to fal.ai (Sync-v2 ROI).",
        });
      } else if (b2 === "local" && roiMode === "half_split") {
        warnings.push({
          id: "local_half_roi",
          title: "Half Split ROI on Local GPU is sensitive to camera motion",
          body: "Half Split locks a vertical border at Split X. Works well on static shots, but camera drift or background motion crossing center can create vertical seams or morphing.",
          recommendation: isLtx2P2 ? "Keep subjects clear of Split X (~10% blend zone), or switch Pass 2 to #1/#9 / duet_method_a." : "Use Full Frame (Split-Masked) for scenes with moving cameras.",
        });
      }
      if (isLtx2P2) warnings.push(_ltx2MvgaWarning(false));
    }

    const mode = String(cand.mode || "solo").toLowerCase().trim();
    const isSilentMode = mode === "silent" || mode === "silent_locked" || mode === "ambient";
    const stemActive = twoPass
      ? (String(cand.stem_m || "male").toLowerCase().trim() !== "silent" || String(cand.stem_f || "female").toLowerCase().trim() !== "silent")
      : !["silent", "none"].includes(String(cand.stem || "male").toLowerCase().trim());
    const p1Spec = _resolvePassVideoSpec(cand, 1), p1Mtype = String(p1Spec.model_type || "").toLowerCase();
    const defOv = Number(window.state?.projectData?.project?.render_overlap_seconds ?? 0);
    const effDur = Math.max(0, Number(cand.end || 0) - Number(cand.start || 0)) + Number(cand.render_pad_start ?? defOv) + Number(cand.render_pad_end ?? (cand.anchor_end ? 0 : defOv));
    if (_isLtx2MsrSpec(p1Spec) && effDur > 10.0) {
      warnings.push({ id: "ltx2_msr_long_scene", title: `LTX-2.5 MSR scene (${effDur.toFixed(1)}s) exceeds 10s single window`, body: "LTX-2.5 MSR has sliding windows disabled; clips >10s risk VRAM OOM or drift.", recommendation: "Split the scene <= 10s or switch to #20 Ingredients." });
    }
    if (cand.anchor_end && !isSilentMode && stemActive) {
      const isLtx2 = p1Mtype.includes("ltx2");
      const isRef2v = !isLtx2 && Boolean(p1Spec.supports_image_refs || p1Mtype.includes("ref2va") || p1Mtype.includes("fasth3") || p1Mtype.includes("singularity"));
      if (b1 === "minimax" || (b1 === "fal" && !isLtx2 && !isRef2v)) {
        warnings.push({
          id: "cloud_end_anchor_audio",
          title: "End Anchor on Cloud API disables lip-sync audio",
          body: "MiniMax Direct API (and standard fal.ai Start+End frame mode) cannot mix first+last frame interpolation with reference vocal audio, so the rendered clip will not lip-sync.",
          recommendation: "Remove the End Anchor to keep lip-sync, or use Local GPU for Start + End + Audio.",
        });
      }
    }
    return warnings;
  }
  window.evaluateComboWarnings = evaluateComboWarnings;

  function _ensureComboWarningModal() {
    let modal = document.getElementById("comboWarningModal");
    if (modal) return modal;
    if (typeof document === "undefined" || !document.body || typeof document.body.appendChild !== "function") return null;
    modal = document.createElement("div");
    modal.id = "comboWarningModal";
    modal.style.cssText = "display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:10050;align-items:center;justify-content:center;backdrop-filter:blur(2px);";
    modal.innerHTML = `<div style="background:#1e293b;border:1px solid #f59e0b;border-radius:10px;width:460px;max-width:92vw;padding:18px;box-shadow:0 16px 40px rgba(0,0,0,0.7);color:#e2e8f0;"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px;"><h4 style="margin:0;color:#fbbf24;font-size:15px;font-weight:700;">⚠️ Combination May Produce Poor Results</h4><button type="button" id="btnComboWarnClose" style="background:transparent;border:none;color:#94a3b8;font-size:20px;cursor:pointer;line-height:1;">&times;</button></div><div id="comboWarnList" style="display:flex;flex-direction:column;gap:8px;font-size:12px;line-height:1.45;"></div><label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#cbd5e1;margin-top:12px;cursor:pointer;user-select:none;"><input type="checkbox" id="comboWarnDontAskAgain" style="width:14px;height:14px;cursor:pointer;" /><span>Don't tell me again for this combination</span></label><div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;"><button type="button" class="btn btn-sm btn-secondary" id="btnComboWarnCancel">Cancel</button><button type="button" class="btn btn-sm btn-primary" id="btnComboWarnConfirm" style="background:#d97706;border-color:#f59e0b;">Use Anyway</button></div></div>`;
    document.body.appendChild(modal);

    const dismiss = (confirmed) => {
      const ctx = _comboWarnModalCtx;
      _comboWarnModalCtx = null;
      modal.style.display = "none";
      if (!ctx) return;
      if (confirmed) {
        if (document.getElementById("comboWarnDontAskAgain")?.checked) _dismissComboWarnings((ctx.warnings || []).map((w) => w.id));
        ctx.onConfirm?.();
      } else ctx.onCancel?.();
    };

    modal.addEventListener("click", (ev) => { if (ev.target === modal) dismiss(false); });
    document.getElementById("btnComboWarnClose")?.addEventListener("click", () => dismiss(false));
    document.getElementById("btnComboWarnCancel")?.addEventListener("click", () => dismiss(false));
    document.getElementById("btnComboWarnConfirm")?.addEventListener("click", () => dismiss(true));
    document.addEventListener?.("keydown", (ev) => {
      if (!_comboWarnModalCtx) return;
      if (ev.key === "Escape") { ev.preventDefault(); dismiss(false); }
    });
    return modal;
  }

  function confirmComboWarnings(warnings, { onConfirm, onCancel } = {}) {
    const active = (warnings || []).filter((w) => w && !isComboWarningDismissed(w.id));
    if (!active.length) {
      onConfirm?.();
      return;
    }
    const modal = _ensureComboWarningModal();
    if (!modal) {
      onConfirm?.();
      return;
    }
    const listEl = document.getElementById("comboWarnList");
    if (listEl) {
      listEl.innerHTML = active.map((w) => `
        <div style="background:#0f172a;border:1px solid rgba(245,158,11,0.35);border-radius:6px;padding:9px 10px;">
          <div style="font-weight:700;color:#fcd34d;margin-bottom:4px;">${_esc(w.title)}</div>
          <div style="color:#cbd5e1;margin-bottom:5px;">${_esc(w.body)}</div>
          <div style="color:#7dd3fc;font-size:11px;"><strong>Tip:</strong> ${_esc(w.recommendation)}</div>
        </div>
      `).join("");
    }
    const chk = document.getElementById("comboWarnDontAskAgain");
    if (chk) chk.checked = false;
    _comboWarnModalCtx = { warnings: active, onConfirm, onCancel };
    modal.style.display = "flex";
  }

  function guardSceneComboChange(sc, patch, { onApply, onRevert } = {}) {
    const prevIds = new Set(evaluateComboWarnings(sc).map((w) => w.id));
    const unseen = evaluateComboWarnings(sc, patch).filter((w) => !prevIds.has(w.id) && !isComboWarningDismissed(w.id));
    if (!unseen.length) {
      onApply?.();
      return;
    }
    confirmComboWarnings(unseen, { onConfirm: onApply, onCancel: onRevert });
  }
  window.guardSceneComboChange = guardSceneComboChange;

  function guardRepairComboAction(sc, repairOpts, onConfirm) {
    const unseen = evaluateComboWarnings(sc, null, repairOpts).filter((w) => !isComboWarningDismissed(w.id));
    if (!unseen.length) {
      onConfirm?.();
      return;
    }
    confirmComboWarnings(unseen, { onConfirm });
  }

  function _parseRendererInput(v) { return v ? (/^\d+$/.test(v) ? parseInt(v, 10) : v) : null; }

  function _buildRendererOpts(sel, inheritLabel, curVal) {
    if (!sel) return;
    if (typeof window.fillRendererSelect === "function") { window.fillRendererSelect(sel, inheritLabel, curVal); return; }
    sel.innerHTML = "";
    const mkO = (v, l) => { const o = document.createElement("option"); o.value = v; o.textContent = l; return o; };
    sel.appendChild(mkO("", inheritLabel));
    const mRoot = window.state?.models || {};
    [["", mRoot.video_renderers, false], ["☁️ ", mRoot.fal_video_renderers, true], ["🎥 ", mRoot.minimax_video_renderers, true]].forEach(([pfx, map, isC]) => {
      Object.keys(map || {}).forEach((k) => sel.appendChild(mkO(k, isC ? `${pfx}${map[k].name || map[k].model_type || k}` : `[#${k}] ${map[k].name || map[k].model_type}`)));
    });
    sel.value = curVal !== null && curVal !== undefined ? String(curVal) : "";
  }

  function ensureDuetPassControls(grp) {
    if (!grp || document.getElementById("duetPassEngineRow")) return;
    const host = document.getElementById("duetPassEngineHost") || grp;
    const row = document.createElement("div");
    row.id = "duetPassEngineRow"; row.className = "duet-pass-row-extracted"; row.style.display = "none";
    const optHtml = ENGINE_OPTS.map(([v, l]) => `<option value="${v}">${_esc(l)}</option>`).join("");
    const roiHtml = ROI_OPTS.map(([v, l]) => `<option value="${v}">${_esc(l)}</option>`).join("");
    row.innerHTML = `<div class="duet-pass-cards-grid">`
      + `<div class="duet-pass-card duet-pass-card--p1"><div class="duet-pass-card-hdr"><span class="duet-pass-card-title">Pass 1 · Left / Base</span><span id="duetPass1Badge" class="duet-pass-card-badge"></span></div>`
      + `<div class="duet-pass-card-fields"><div class="duet-pass-field duet-pass-field--model"><label for="sceneRendererM">Pass 1 Video Model</label><select id="sceneRendererM"></select></div>`
      + `<div class="duet-pass-field duet-pass-field--engine"><label for="sceneBackendM">Pass 1 Engine</label><select id="sceneBackendM">${optHtml}</select></div></div></div>`
      + `<div class="duet-pass-card duet-pass-card--p2"><div class="duet-pass-card-hdr"><span class="duet-pass-card-title">Pass 2 · Right / Lip-Sync</span><span id="duetPass2Badge" class="duet-pass-card-badge"></span></div>`
      + `<div class="duet-pass-card-fields"><div class="duet-pass-field duet-pass-field--model"><label for="sceneRendererF">Pass 2 Video Model</label><select id="sceneRendererF"></select></div>`
      + `<div class="duet-pass-field duet-pass-field--engine"><label for="sceneBackendF">Pass 2 Engine</label><select id="sceneBackendF">${optHtml}</select></div>`
      + `<div class="duet-pass-field duet-pass-field--roi"><label for="scenePass2Roi">Pass 2 ROI Scope</label><select id="scenePass2Roi">${roiHtml}</select></div>`
      + `<div id="scenePass2RoiSideWrap" class="duet-pass-field duet-pass-field--side" style="display:none;"><label for="scenePass2RoiSide">ROI Side</label><select id="scenePass2RoiSide"><option value="">Right (P2)</option><option value="left">Left</option></select></div>`
      + `<div id="scenePass2RoiBoxWrap" class="duet-pass-field duet-pass-field--box" style="display:none;"><label for="scenePass2RoiBox">ROI Box</label><input id="scenePass2RoiBox" type="text" placeholder="0.45,0.05,0.98,0.85" /></div>`
      + `</div></div></div>`;
    const hint = document.getElementById("sceneFalHint");
    if (hint && hint.parentElement === host) host.insertBefore(row, hint); else host.appendChild(row);

    const activeSc = () => window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
    [["renderer_m", "sceneRendererM", "Pass 1 Model", true], ["renderer_f", "sceneRendererF", "Pass 2 Model", true], ["backend_m", "sceneBackendM", "Pass 1 Engine", false], ["backend_f", "sceneBackendF", "Pass 2 Engine", false]].forEach(([prop, elId, lbl, isR]) => {
      document.getElementById(elId)?.addEventListener("change", (e) => {
        const sc = activeSc(); if (!sc || sc.locked) return;
        const v = isR ? _parseRendererInput(e.target.value) : e.target.value;
        const patchVal = isR ? (v === null ? undefined : v) : (v || undefined);
        guardSceneComboChange(sc, { [prop]: patchVal }, {
          onApply: () => { if (patchVal !== undefined) sc[prop] = v; else delete sc[prop]; _notifySceneUpdated(sc, `Change ${sc.id} ${lbl}`); },
          onRevert: () => { e.target.value = sc[prop] ?? ""; },
        });
      });
    });
    document.getElementById("scenePass2Roi")?.addEventListener("change", (e) => {
      const sc = activeSc(); if (!sc || sc.locked) return;
      const val = e.target.value;
      guardSceneComboChange(sc, { pass2_roi_mode: (!val || val === "full") ? undefined : val }, {
        onApply: () => {
          if (!val || val === "full") delete sc.pass2_roi_mode;
          else { sc.pass2_roi_mode = val; if (val === "custom" && !parseRoiBoxStr(sc.pass2_roi_box)) sc.pass2_roi_box = [0.45, 0.05, 0.98, 0.85]; }
          _notifySceneUpdated(sc, `Change ${sc.id} Pass 2 ROI`);
        },
        onRevert: () => { const p = normalizeRoiModeJS(sc.pass2_roi_mode); e.target.value = p === "full" ? "" : p; },
      });
    });
    document.getElementById("scenePass2RoiSide")?.addEventListener("change", (e) => {
      const sc = activeSc(); if (!sc || sc.locked) return;
      if (e.target.value === "left") sc.pass2_roi_side = "left"; else delete sc.pass2_roi_side;
      _notifySceneUpdated(sc, `Change ${sc.id} Pass 2 ROI Side`);
    });
    const roiBoxEl = document.getElementById("scenePass2RoiBox");
    if (roiBoxEl) {
      roiBoxEl.addEventListener("focus", () => { const sc = activeSc(); if (sc && !sc.locked) window.beginEditSession?.(`roi:${sc.id}`, `Edit ${sc.id} Pass 2 ROI Box`); });
      roiBoxEl.addEventListener("input", () => { const sc = activeSc(); if (!sc || sc.locked) return; const b = parseRoiBoxStr(roiBoxEl.value); if (b) { sc.pass2_roi_box = b; roiBoxEl.style.borderColor = ""; window.markProjectDirty?.(); } else roiBoxEl.style.borderColor = "#ef4444"; });
      roiBoxEl.addEventListener("blur", () => {
        const sc = activeSc();
        if (!sc || sc.locked) { window.commitEditSession?.(); return; }
        const b = parseRoiBoxStr(roiBoxEl.value);
        if (!b) { window.showToast?.("ROI Box must be 4 normalized floats x0,y0,x1,y1 in 0..1", 4000); roiBoxEl.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85"; roiBoxEl.style.borderColor = ""; }
        else { sc.pass2_roi_box = b; _notifySceneUpdated(sc, null); }
        window.commitEditSession?.();
      });
    }
  }
  window.ensureDuetPassControls = ensureDuetPassControls;

  function _syncClipSplitOverlay(sc, isTwo, isLtx2P2, sxOverride) {
    const ov = document.getElementById("clipSplitOverlay");
    if (ov) {
      if (!isTwo || !sc) { ov.style.display = "none"; }
      else {
        const sx = Math.max(0.05, Math.min(0.95, Number.isFinite(sxOverride) ? sxOverride : Number(sc.split_x ?? 0.5)));
        const pct = (sx * 100).toFixed(2), zoneL = (Math.max(0, sx - 0.1) * 100).toFixed(2), zoneW = ((sx - Math.max(0, sx - 0.1)) * 100).toFixed(2);
        ov.style.display = "block";
        ov.innerHTML = (isLtx2P2 ? `<div style="position:absolute;top:0;bottom:0;left:${zoneL}%;width:${zoneW}%;background:rgba(245,158,11,0.16);border-left:1px dotted rgba(245,158,11,0.55);"></div>` : "") + `<div style="position:absolute;top:0;bottom:0;left:${pct}%;border-left:2px dashed rgba(245,158,11,0.85);"></div><span style="position:absolute;top:6px;left:${pct}%;transform:translateX(-50%);background:rgba(15,23,42,0.82);color:#fbbf24;font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;white-space:nowrap;">Split X ${Math.round(sx * 100)}%${isLtx2P2 ? " · MVGA ~10% blend" : ""}</span>`;
      }
    }
    window.syncVideoRoiOverlays?.(sc, sxOverride);
  }
  window.normalizeRoiModeJS = normalizeRoiModeJS;
  window.parseRoiBoxStr = parseRoiBoxStr;
  window.resolveDuetPassVideoSpec = _resolvePassVideoSpec;
  window.isLtx2MvgaSpec = _isLtx2MvgaSpec;

  function _passSummary(sc, passNum, effB, roiMode, isLtx2 = false) {
    const dur = Math.max(4.0, (Number(sc.end || 0) - Number(sc.start || 0)) || 5.0);
    const roiLbl = roiMode === "tight_performer" ? "Tight ROI" : (roiMode === "custom" ? "Custom ROI" : "Half ROI");
    if (effB === "local") return passNum === 2 ? (isLtx2 ? (roiMode === "full" ? "Local MVGA (Full Mask, $0)" : `Local MVGA (${roiLbl}, $0)`) : (roiMode !== "full" ? `Local GVA (${roiLbl}, $0)` : "Local GPU ($0)")) : "Local GPU ($0)";
    if (effB === "fal") {
      if (passNum === 2 && roiMode !== "full") return `fal Sync-v2 ROI (~$${(dur * 0.05).toFixed(2)})`;
      const est = window.estimateSceneCloudCostFrontend?.(sc, "fal", passNum);
      return `fal.ai (${est?.costStr || `~$${(dur * 0.08).toFixed(2)}`})`;
    }
    if (effB === "minimax") {
      const est = window.estimateSceneCloudCostFrontend?.(sc, "minimax", passNum);
      return `MiniMax (${est?.costStr || `~$${(dur * 0.08).toFixed(2)}`})`;
    }
    return effB;
  }

  function syncDuetPassControlsUI(sc) {
    const grp = document.getElementById("videoRendererGroup");
    ensureDuetPassControls(grp);
    const row = document.getElementById("duetPassEngineRow");
    const isTwo = isTwoPassScene(sc);
    const vrLbl = document.querySelector?.('#videoRendererGroup label[for="sceneVideoRenderer"]');
    if (vrLbl) vrLbl.textContent = isTwo ? "Scene Default Model" : "Video Generation Model";
    if (!isTwo || !sc) { _syncClipSplitOverlay(sc, false, false); if (row) row.style.display = "none"; return; }
    if (!row) return;
    row.style.display = "flex";
    ["sceneSplitX", "sceneSplitXRange"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && !el.dataset?.splitOvBound) { if (el.dataset) el.dataset.splitOvBound = "1"; const fn = () => { const cur = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx]; if (cur) _syncClipSplitOverlay(cur, isTwoPassScene(cur), resolvePassBackend(cur, 2) === "local" && _isLtx2MvgaSpec(_resolvePassVideoSpec(cur, 2)), Number(el.value)); }; el.addEventListener("input", fn); el.addEventListener("change", fn); }
    });

    const locked = Boolean(sc.locked), effBase = window.resolveEffectiveSceneBackend?.(sc) || "local";
    const inhEngineLbl = `Inherit (${effBase})`;
    const sceneDefRid = _cleanRid(sc.renderer) || String(window.state?.projectData?.models?.default_video_renderer || "1");
    const inhModelLbl = `Inherit Scene (${_rendererShortLabel(sceneDefRid)})`;
    const mRoot = window.state?.models || {};

    [
      [1, "sceneRendererM", "sceneBackendM", sc.renderer_m, sc.backend_m, "duetPass1Badge"],
      [2, "sceneRendererF", "sceneBackendF", sc.renderer_f, sc.backend_f, "duetPass2Badge"],
    ].forEach(([pNum, rElId, bElId, rVal, bVal]) => {
      const rSel = document.getElementById(rElId), bSel = document.getElementById(bElId);
      const curR = rVal !== null && rVal !== undefined ? String(rVal) : "";
      if (rSel) { _buildRendererOpts(rSel, inhModelLbl, curR); rSel.disabled = locked; }
      const effR = resolvePassRendererId(sc, pNum);
      const isDedFal = Boolean(mRoot.fal_video_renderers?.[effR]), isDedMm = Boolean(mRoot.minimax_video_renderers?.[effR]);
      if (bSel) {
        if (bSel.options?.[0]) bSel.options[0].textContent = inhEngineLbl;
        bSel.value = isDedFal ? "fal" : (isDedMm ? "minimax" : (bVal || ""));
        bSel.disabled = locked || isDedFal || isDedMm;
      }
    });

    const selRoi = document.getElementById("scenePass2Roi"), selSide = document.getElementById("scenePass2RoiSide");
    const sideWrap = document.getElementById("scenePass2RoiSideWrap"), boxWrap = document.getElementById("scenePass2RoiBoxWrap"), boxInp = document.getElementById("scenePass2RoiBox");
    const roiMode = normalizeRoiModeJS(sc.pass2_roi_mode);
    if (selRoi) { selRoi.value = roiMode === "full" ? "" : roiMode; selRoi.disabled = locked; }
    if (sideWrap && selSide) { sideWrap.style.display = roiMode !== "full" ? "flex" : "none"; selSide.value = sc.pass2_roi_side === "left" ? "left" : ""; selSide.disabled = locked; }
    if (boxWrap && boxInp) {
      boxWrap.style.display = roiMode === "custom" ? "flex" : "none";
      if (!boxInp.matches?.(":focus")) { boxInp.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85"; boxInp.style.borderColor = ""; }
      boxInp.disabled = locked;
    }

    const b1 = resolvePassBackend(sc, 1), b2 = resolvePassBackend(sc, 2);
    const isLtx2P2 = b2 === "local" && _isLtx2MvgaSpec(_resolvePassVideoSpec(sc, 2));
    const s1 = _passSummary(sc, 1, b1, "full"), s2 = _passSummary(sc, 2, b2, roiMode, isLtx2P2);
    const p1B = document.getElementById("duetPass1Badge"), p2B = document.getElementById("duetPass2Badge");
    if (p1B) p1B.textContent = `${_rendererShortLabel(resolvePassRendererId(sc, 1))} · ${s1}`;
    if (p2B) p2B.textContent = `${_rendererShortLabel(resolvePassRendererId(sc, 2))} · ${s2}`;
    _syncClipSplitOverlay(sc, true, isLtx2P2);
    const hint = document.getElementById("sceneFalHint");
    if (hint) { hint.style.display = "block"; hint.textContent = isLtx2P2 ? `Two-Pass: P1 ${s1} -> P2 ${s2} · ⚠ LTX-2 MVGA blends ~10% across Split X` : `Two-Pass: P1 ${s1} -> P2 ${s2}`; }
  }
  window.syncDuetPassControlsUI = syncDuetPassControlsUI;

  function _mkSelect(opts, val, title, onChange) {
    const sel = document.createElement("select");
    sel.title = title;
    opts.forEach(([v, l]) => { const o = document.createElement("option"); o.value = v; o.textContent = l; sel.appendChild(o); });
    sel.value = val || "";
    sel.onchange = () => onChange(sel.value);
    return sel;
  }

  function _mkCardField(labelText, minW, ctrl) {
    const wrap = document.createElement("label");
    wrap.className = "scene-ctrl";
    wrap.style.cssText = `min-width:${minW};flex:0.85 1 ${minW};`;
    const sp = document.createElement("span");
    sp.className = "scene-ctrl-lbl";
    sp.textContent = labelText;
    wrap.appendChild(sp);
    wrap.appendChild(ctrl);
    return wrap;
  }

  function appendTwoPassEngineControls(hostEl, sc, idx, applyFieldSideEffects, cardHelpers) {
    if (!hostEl || !sc || !isTwoPassScene(sc)) return;
    const locked = Boolean(sc.locked), mRoot = window.state?.models || {};
    const sceneDefRid = _cleanRid(sc.renderer) || String(window.state?.projectData?.models?.default_video_renderer || "1");
    const inhModelLbl = `Inherit Scene (${_rendererShortLabel(sceneDefRid)})`;
    const syncAll = (structural = false) => {
      applyFieldSideEffects?.(idx, sc, structural ? { structural: true } : undefined);
      if (window.state?.selectedSceneIdx === idx) window.syncSceneBackendUI?.(sc);
    };
    const mkPassModelSel = (pNum, prop, curVal, title, bSel) => {
      const sel = document.createElement("select");
      sel.title = title;
      sel.disabled = locked;
      _buildRendererOpts(sel, inhModelLbl, curVal !== null && curVal !== undefined ? String(curVal) : "");
      sel.onchange = () => {
        const v = _parseRendererInput(sel.value);
        guardSceneComboChange(sc, { [prop]: v === null ? undefined : v }, {
          onApply: () => {
            if (v !== null) sc[prop] = v; else delete sc[prop];
            const effR = resolvePassRendererId(sc, pNum);
            const dedF = Boolean(mRoot.fal_video_renderers?.[effR]), dedM = Boolean(mRoot.minimax_video_renderers?.[effR]);
            if (bSel) { bSel.value = dedF ? "fal" : (dedM ? "minimax" : (sc[pNum === 1 ? "backend_m" : "backend_f"] || "")); bSel.disabled = locked || dedF || dedM; }
            window.recordHistorySnapshot?.(`Change ${sc.id} Pass ${pNum} Model`);
            syncAll();
          },
          onRevert: () => { sel.value = sc[prop] ?? ""; },
        });
      };
      return sel;
    };
    const mkPassEngineSel = (pNum, prop, curVal, title) => {
      const effR = resolvePassRendererId(sc, pNum);
      const dedF = Boolean(mRoot.fal_video_renderers?.[effR]), dedM = Boolean(mRoot.minimax_video_renderers?.[effR]);
      const sel = _mkSelect(ENGINE_OPTS, dedF ? "fal" : (dedM ? "minimax" : (curVal || "")), title, (v) => {
        guardSceneComboChange(sc, { [prop]: v || undefined }, {
          onApply: () => { if (v) sc[prop] = v; else delete sc[prop]; window.recordHistorySnapshot?.(`Change ${sc.id} Pass ${pNum} Engine`); syncAll(); },
          onRevert: () => { sel.value = sc[prop] || ""; },
        });
      });
      sel.disabled = locked || dedF || dedM;
      return sel;
    };
    const p1Eng = mkPassEngineSel(1, "backend_m", sc.backend_m, "Pass 1 execution engine");
    const p1Mod = mkPassModelSel(1, "renderer_m", sc.renderer_m, "Pass 1 video generation model", p1Eng);
    const p2Eng = mkPassEngineSel(2, "backend_f", sc.backend_f, "Pass 2 execution engine");
    const p2Mod = mkPassModelSel(2, "renderer_f", sc.renderer_f, "Pass 2 video generation model", p2Eng);
    const curRoi = normalizeRoiModeJS(sc.pass2_roi_mode);
    const roiSel = _mkSelect(ROI_OPTS, curRoi === "full" ? "" : curRoi, "Pass 2 ROI crop acceleration mode", (v) => {
      guardSceneComboChange(sc, { pass2_roi_mode: (!v || v === "full") ? undefined : v }, {
        onApply: () => {
          if (!v || v === "full") delete sc.pass2_roi_mode;
          else { sc.pass2_roi_mode = v; if (v === "custom" && !parseRoiBoxStr(sc.pass2_roi_box)) sc.pass2_roi_box = [0.45, 0.05, 0.98, 0.85]; }
          window.recordHistorySnapshot?.(`Change ${sc.id} Pass 2 ROI`);
          syncAll(true);
        },
        onRevert: () => { const p = normalizeRoiModeJS(sc.pass2_roi_mode); roiSel.value = p === "full" ? "" : p; },
      });
    });
    roiSel.disabled = locked;
    const mkSide = () => {
      const s = _mkSelect([["", "Right (P2)"], ["left", "Left"]], sc.pass2_roi_side === "left" ? "left" : "", "Pass 2 ROI side", (v) => {
        if (v === "left") sc.pass2_roi_side = "left"; else delete sc.pass2_roi_side;
        window.recordHistorySnapshot?.(`Change ${sc.id} Pass 2 ROI Side`); syncAll();
      });
      s.disabled = locked;
      return s;
    };
    const mkBox = () => {
      const bx = document.createElement("input");
      bx.type = "text"; bx.disabled = locked;
      bx.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85";
      bx.title = "Custom ROI box (x0,y0,x1,y1 in 0..1)";
      bx.onchange = () => {
        const parsed = parseRoiBoxStr(bx.value);
        if (!parsed) { window.showToast?.("ROI Box must be 4 normalized floats x0,y0,x1,y1 in 0..1", 4000); bx.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85"; return; }
        sc.pass2_roi_box = parsed; window.recordHistorySnapshot?.(`Edit ${sc.id} Pass 2 ROI Box`); syncAll();
      };
      return bx;
    };

    if (!cardHelpers) {
      hostEl.appendChild(_mkCardField("P1 Model", "125px", p1Mod));
      hostEl.appendChild(_mkCardField("P1 Engine", "96px", p1Eng));
      hostEl.appendChild(_mkCardField("P2 Model", "125px", p2Mod));
      hostEl.appendChild(_mkCardField("P2 Engine", "96px", p2Eng));
      hostEl.appendChild(_mkCardField("P2 ROI", "118px", roiSel));
      if (curRoi !== "full") hostEl.appendChild(_mkCardField("P2 Side", "86px", mkSide()));
      if (curRoi === "custom") hostEl.appendChild(_mkCardField("P2 ROI Box", "145px", mkBox()));
      return;
    }

    const { mkStemSel, mkNum, makeCtrl } = cardHelpers;
    const subRow = document.createElement("div");
    subRow.className = "scenes-duet-pass-row";
    const mkCard = (modCls, title, fields) => {
      const card = document.createElement("div");
      card.className = `scenes-duet-pass-card ${modCls}`;
      const hdr = document.createElement("div");
      hdr.className = "scenes-duet-pass-hdr";
      hdr.textContent = title;
      const body = document.createElement("div");
      body.className = "scenes-duet-pass-fields";
      fields.forEach((f) => body.appendChild(f));
      card.appendChild(hdr); card.appendChild(body);
      return card;
    };
    const p1Fields = [
      makeCtrl("P1 Model", "min-width:130px;flex:1.4 1 130px;", p1Mod),
      makeCtrl("P1 Engine", "min-width:94px;flex:0.85 1 94px;", p1Eng),
      makeCtrl("P1 Stem (L)", "min-width:105px;flex:0.95 1 105px;", mkStemSel(sc.stem_m, "male", (v) => { sc.stem_m = v; })),
      makeCtrl("P1 Seed", "flex:0 0 74px;", mkNum(sc, idx, sc.seed_m !== undefined ? sc.seed_m : 8201, "1", "0", "999999999", "8201", "100%", "seed_m", (v) => { const n = parseInt(v, 10); sc.seed_m = isNaN(n) ? 8201 : n; applyFieldSideEffects(idx, sc); })),
    ];
    const p2Fields = [
      makeCtrl("P2 Model", "min-width:130px;flex:1.4 1 130px;", p2Mod),
      makeCtrl("P2 Engine", "min-width:94px;flex:0.85 1 94px;", p2Eng),
      makeCtrl("P2 Stem (R)", "min-width:105px;flex:0.95 1 105px;", mkStemSel(sc.stem_f, sc.mode === "gva_male_left_silent_right" ? "silent" : "female", (v) => { sc.stem_f = v; })),
      makeCtrl("P2 Seed", "flex:0 0 74px;", mkNum(sc, idx, sc.seed_f !== undefined ? sc.seed_f : 8301, "1", "0", "999999999", "8301", "100%", "seed_f", (v) => { const n = parseInt(v, 10); sc.seed_f = isNaN(n) ? 8301 : n; applyFieldSideEffects(idx, sc); })),
      makeCtrl("P2 ROI", "min-width:112px;flex:1.0 1 112px;", roiSel),
    ];
    if (curRoi !== "full") p2Fields.push(makeCtrl("P2 Side", "flex:0 0 76px;", mkSide()));
    if (curRoi === "custom") p2Fields.push(makeCtrl("P2 ROI Box", "min-width:125px;flex:1 1 125px;", mkBox()));
    subRow.appendChild(mkCard("scenes-duet-pass-card--p1", "Pass 1 · Left / Base", p1Fields));
    subRow.appendChild(mkCard("scenes-duet-pass-card--p2", "Pass 2 · Right / Lip-Sync", p2Fields));
    hostEl.appendChild(subRow);
  }
  window.appendTwoPassEngineControls = appendTwoPassEngineControls;

  function _populateRepairStems(stemSel, scene, resetSelection = false) {
    if (!stemSel) return;
    const prev = resetSelection ? "" : stemSel.value;
    const seen = new Set([""]);
    const list = [["", "Auto (Match Side)"]];
    const pushStem = (raw) => {
      const s = String(raw || "").trim();
      const low = s.toLowerCase();
      if (!s || low === "silent" || seen.has(low)) return;
      seen.add(low);
      list.push([s, s]);
    };
    const missing = new Set((window.state?.assets?.missing_stems || []).map((k) => String(k).toLowerCase()));
    const catalog = window.getProjectStemsCatalog ? window.getProjectStemsCatalog() : [];
    catalog.forEach((st) => pushStem(st.value));
    if (!catalog.length) {
      (window.state?.assets?.audio_stems || []).forEach(pushStem);
      const refSel = document.getElementById("sceneStemLeft") || document.getElementById("sceneStem");
      if (refSel) {
        Array.from(refSel.options || []).forEach((o) => { if (!o.dataset?.missing) pushStem(o.value); });
      }
      [scene?.stem_f, scene?.stem_m, scene?.stem].forEach((k) => { if (k && !missing.has(String(k).toLowerCase())) pushStem(k); });
    }
    stemSel.innerHTML = "";
    list.forEach(([v, lbl]) => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = lbl;
      stemSel.appendChild(o);
    });
    if (prev && seen.has(prev.toLowerCase())) {
      const hit = list.find(([v]) => v.toLowerCase() === prev.toLowerCase());
      stemSel.value = hit ? hit[0] : "";
    } else {
      stemSel.value = "";
    }
  }

  function _ensureRepairUI() {
    const tray = document.getElementById("sceneTakesTray");
    const upBtn = document.getElementById("btnUpscaleTake");
    if (!tray || !upBtn || !upBtn.parentElement) return null;

    let toggleBtn = document.getElementById("btnToggleLipSyncRepair");
    if (!toggleBtn) {
      toggleBtn = document.createElement("button");
      toggleBtn.id = "btnToggleLipSyncRepair";
      toggleBtn.type = "button";
      toggleBtn.className = "btn btn-sm btn-secondary";
      toggleBtn.textContent = "👄 Fix Lip-Sync";
      toggleBtn.title = "Repair a single singer's lip-sync via cropped ROI without re-rendering Pass 1";
      upBtn.parentElement.insertBefore(toggleBtn, upBtn.nextSibling);
    }

    let bar = document.getElementById("lipSyncRepairBar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "lipSyncRepairBar";
      bar.style.cssText = "display:none;flex-direction:column;gap:6px;margin-top:6px;padding:8px;border-radius:6px;background:rgba(16,185,129,0.07);border:1px solid rgba(16,185,129,0.28);";
      bar.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
          <span style="font-size:10px;font-weight:700;color:#6ee7b7;text-transform:uppercase;letter-spacing:0.4px;">Cropped-ROI Lip-Sync Repair (Preserves Unchanged Region &amp; Audio)</span>
          <button id="btnCloseLipSyncRepair" type="button" class="btn btn-sm btn-secondary" style="padding:1px 6px;font-size:10px;">✕</button>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
          <select id="repSide" title="Which singer/region to repair" style="font-size:11px;padding:3px 6px;">
            <option value="right">Right Singer (Pass 2)</option>
            <option value="left">Left Singer (Pass 1)</option>
            <option value="custom">Custom Box</option>
          </select>
          <select id="repRoiMode" title="Cropped ROI size" style="font-size:11px;padding:3px 6px;">
            <option value="half_split">Half Split (Fast)</option>
            <option value="tight_performer">Tight Singer (Fastest)</option>
            <option value="custom">Custom Box</option>
          </select>
          <select id="repStem" title="Audio stem driving lip-sync" style="font-size:11px;padding:3px 6px;"></select>
          <select id="repBackend" title="Repair execution engine" style="font-size:11px;padding:3px 6px;">
            <option value="local">🖥️ Local GPU ($0 ROI)</option>
            <option value="fal">☁️ fal.ai Sync-v2 ($0.05/s)</option>
            <option value="minimax">🎬 MiniMax ROI</option>
          </select>
          <button id="btnRunLipSyncRepair" type="button" class="btn btn-sm btn-primary" style="font-size:11px;padding:3px 10px;">▶ Run Repair</button>
        </div>
        <div id="repBoxWrap" style="display:none;align-items:center;gap:6px;">
          <span style="font-size:10px;color:var(--text-muted);white-space:nowrap;">Custom ROI (x0,y0,x1,y1):</span>
          <input id="repRoiBox" type="text" value="0.45,0.05,0.98,0.85" style="flex:1;font-size:11px;padding:2px 6px;" />
        </div>
      `;
      tray.appendChild(bar);

      toggleBtn.onclick = () => {
        bar.style.display = bar.style.display === "none" ? "flex" : "none";
      };
      document.getElementById("btnCloseLipSyncRepair")?.addEventListener("click", () => {
        bar.style.display = "none";
      });
      const syncBoxVis = () => {
        const sd = document.getElementById("repSide")?.value;
        const rm = document.getElementById("repRoiMode")?.value;
        const bw = document.getElementById("repBoxWrap");
        if (bw) bw.style.display = (sd === "custom" || rm === "custom") ? "flex" : "none";
      };
      document.getElementById("repSide")?.addEventListener("change", (e) => {
        if (e.target.value === "custom") {
          const rmSel = document.getElementById("repRoiMode");
          if (rmSel) rmSel.value = "custom";
        }
        syncBoxVis();
      });
      document.getElementById("repRoiMode")?.addEventListener("change", syncBoxVis);

      document.getElementById("btnRunLipSyncRepair")?.addEventListener("click", async () => {
        const sc = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
        const slug = window.state?.currentSlug;
        if (!sc || sc.locked || !slug) return;
        const side = document.getElementById("repSide")?.value || "right";
        let roi_mode = document.getElementById("repRoiMode")?.value || "half_split";
        if (side === "custom") roi_mode = "custom";
        let roi_box = null;
        if (roi_mode === "custom" || side === "custom") {
          roi_box = parseRoiBoxStr(document.getElementById("repRoiBox")?.value);
          if (!roi_box) {
            alert("Custom ROI Box must be 4 normalized coordinates x0,y0,x1,y1 inside 0..1 (e.g. 0.45,0.05,0.98,0.85).");
            return;
          }
        }
        const stem = document.getElementById("repStem")?.value || "";
        const backend = document.getElementById("repBackend")?.value || "local";
        const take_id = document.getElementById("sceneTakeSelect")?.value || null;
        guardRepairComboAction(sc, { isRepair: true, backend, roi_mode, side }, async () => {
          const runBtn = document.getElementById("btnRunLipSyncRepair");
          if (runBtn) { runBtn.disabled = true; runBtn.textContent = "Starting..."; }
          try {
            if (window.state?.isDirty && typeof window.saveProjectData === "function") {
              const saved = await window.saveProjectData(false);
              if (!saved) { return; }
            }
            const resp = await fetch("/api/takes/repair_lipsync", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ project: slug, scene_id: sc.id, side, roi_mode, roi_box, stem, backend, take_id }),
            });
            const data = await resp.json();
            if (resp.ok && data.success) {
              window.showToast?.(`👄 Started lip-sync repair on ${sc.id} (${side} @ ${backend}) — watch Live Logs.`);
            } else {
              alert("Lip-sync repair failed: " + (data.error || `HTTP ${resp.status}`));
            }
          } catch (err) {
            alert("Lip-sync repair request failed: " + err.message);
          } finally {
            const curSc = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
            if (runBtn) { runBtn.disabled = Boolean(curSc?.locked); runBtn.textContent = "▶ Run Repair"; }
          }
        });
      });
    }
    return { toggleBtn, bar };
  }

  let _lastRepairSceneKey = null;
  function syncTakeRepairUI(scene) {
    const ui = _ensureRepairUI();
    if (!ui || !scene) return;
    const locked = Boolean(scene.locked);
    ui.toggleBtn.disabled = locked || ui.toggleBtn.dataset.forceDisabled === "1";
    const runBtn = document.getElementById("btnRunLipSyncRepair");
    if (runBtn) runBtn.disabled = locked;

    const mode = String(scene.mode || "solo").toLowerCase();
    const isDuet = mode.includes("duet") || mode.includes("gva") || mode.includes("stereo");
    const sceneKey = `${window.state?.currentSlug || ""}:${scene.id}:${isDuet}`;
    const switched = _lastRepairSceneKey !== sceneKey;
    const stemSel = document.getElementById("repStem");
    _populateRepairStems(stemSel, scene, switched);

    if (switched) {
      _lastRepairSceneKey = sceneKey;
      const sideSel = document.getElementById("repSide");
      const rmSel = document.getElementById("repRoiMode");
      const bw = document.getElementById("repBoxWrap");
      if (sideSel) sideSel.value = isDuet ? "right" : "custom";
      if (rmSel) rmSel.value = isDuet ? "half_split" : "custom";
      if (bw) bw.style.display = (sideSel?.value === "custom" || rmSel?.value === "custom") ? "flex" : "none";
      const boxInp = document.getElementById("repRoiBox");
      if (boxInp && !isDuet) boxInp.value = "0.25,0.05,0.75,0.85";
      else if (boxInp) boxInp.value = Array.isArray(scene.pass2_roi_box) ? scene.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85";
    }
  }
  window.syncTakeRepairUI = syncTakeRepairUI;
})();
