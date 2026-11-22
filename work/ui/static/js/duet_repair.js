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
    ["", "Full Frame (Standard)"],
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

  function resolvePassBackend(sc, passNum) {
    if (!sc) return window.getEffectiveProjectBackend?.() || "local";
    const direct = String(passNum === 1 ? (sc.backend_m || "") : (sc.backend_f || "")).trim().toLowerCase();
    if (direct === "local" || direct === "fal" || direct === "minimax") return direct;
    const vrKey = passNum === 1 ? (sc.renderer_m ?? sc.renderer) : (sc.renderer_f ?? sc.renderer);
    const vrId = String(vrKey ?? window.state?.projectData?.models?.default_video_renderer ?? "1");
    if (window.state?.models?.fal_video_renderers?.[vrId]) return "fal";
    if (window.state?.models?.minimax_video_renderers?.[vrId]) return "minimax";
    const scB = String(sc.backend || "").trim().toLowerCase();
    if (scB === "local" || scB === "fal" || scB === "minimax") return scB;
    return window.getEffectiveProjectBackend?.() || "local";
  }
  window.resolveDuetPassBackend = resolvePassBackend;

  function _notifySceneUpdated(sc, label) {
    window.markProjectDirty?.();
    if (label) window.recordHistorySnapshot?.(label);
    window.updateInspectorSceneHeader?.(sc);
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
          recommendation: "Use Full Frame (Standard) on Local GPU, or switch Pass 2 to fal.ai (Sync-v2 ROI).",
        });
      } else if (b2 === "local" && roiMode === "half_split") {
        warnings.push({
          id: "local_half_roi",
          title: "Half Split ROI on Local GPU is sensitive to camera motion",
          body: "Half Split locks a 64px vertical border at Split X. Works well on static shots, but camera drift or background motion crossing center can create vertical seams or morphing.",
          recommendation: "Use Full Frame (Standard) for scenes with moving cameras.",
        });
      }
    }

    const mode = String(cand.mode || "solo").toLowerCase().trim();
    const isSilentMode = mode === "silent" || mode === "silent_locked" || mode === "ambient";
    const stemActive = twoPass
      ? (String(cand.stem_m || "male").toLowerCase().trim() !== "silent" || String(cand.stem_f || "female").toLowerCase().trim() !== "silent")
      : !["silent", "none"].includes(String(cand.stem || "male").toLowerCase().trim());
    if (cand.anchor_end && !isSilentMode && stemActive) {
      const vrId = String(cand.renderer ?? window.state?.projectData?.models?.default_video_renderer ?? "1");
      const vSpec = window.state?.models?.fal_video_renderers?.[vrId] || window.state?.models?.minimax_video_renderers?.[vrId] || window.state?.models?.video_renderers?.[vrId] || {};
      const mtype = String(vSpec.model_type || "").toLowerCase();
      const isLtx2 = mtype.includes("ltx2");
      const isRef2v = Boolean(vSpec.supports_image_refs || mtype.includes("ref2va") || mtype.includes("fasth3") || mtype.includes("singularity"));
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
    modal.innerHTML = `
      <div style="background:#1e293b;border:1px solid #f59e0b;border-radius:10px;width:460px;max-width:92vw;padding:18px;box-shadow:0 16px 40px rgba(0,0,0,0.7);color:#e2e8f0;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px;">
          <h4 style="margin:0;color:#fbbf24;font-size:15px;font-weight:700;">⚠️ Combination May Produce Poor Results</h4>
          <button type="button" id="btnComboWarnClose" style="background:transparent;border:none;color:#94a3b8;font-size:20px;cursor:pointer;line-height:1;">&times;</button>
        </div>
        <div id="comboWarnList" style="display:flex;flex-direction:column;gap:8px;font-size:12px;line-height:1.45;"></div>
        <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:#cbd5e1;margin-top:12px;cursor:pointer;user-select:none;">
          <input type="checkbox" id="comboWarnDontAskAgain" style="width:14px;height:14px;cursor:pointer;" />
          <span>Don't tell me again for this combination</span>
        </label>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
          <button type="button" class="btn btn-sm btn-secondary" id="btnComboWarnCancel">Cancel</button>
          <button type="button" class="btn btn-sm btn-primary" id="btnComboWarnConfirm" style="background:#d97706;border-color:#f59e0b;">Use Anyway</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    const dismiss = (confirmed) => {
      const ctx = _comboWarnModalCtx;
      _comboWarnModalCtx = null;
      modal.style.display = "none";
      if (!ctx) return;
      if (confirmed) {
        const chk = document.getElementById("comboWarnDontAskAgain");
        if (chk?.checked) _dismissComboWarnings((ctx.warnings || []).map((w) => w.id));
        ctx.onConfirm?.();
      } else {
        ctx.onCancel?.();
      }
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

  function ensureDuetPassControls(grp) {
    if (!grp || document.getElementById("duetPassEngineRow")) return;
    const host = document.getElementById("duetPassEngineHost") || grp;
    const row = document.createElement("div");
    row.id = "duetPassEngineRow";
    row.className = "duet-pass-row-extracted";
    row.style.cssText = "display:none;flex-direction:column;gap:5px;margin-top:6px;padding:7px 8px;border-radius:6px;background:rgba(56,189,248,0.06);border:1px solid rgba(56,189,248,0.22);";
    const optHtml = ENGINE_OPTS.map(([v, l]) => `<option value="${v}">${_esc(l)}</option>`).join("");
    const roiHtml = ROI_OPTS.map(([v, l]) => `<option value="${v}">${_esc(l)}</option>`).join("");
    row.innerHTML = `
      <div class="duet-pass-hdr" style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
        <span style="font-size:10px;color:#7dd3fc;font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">Cross-Engine Pass Engines &amp; ROI</span>
      </div>
      <div class="duet-pass-fields-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:6px;align-items:end;">
        <label style="display:flex;flex-direction:column;gap:2px;font-size:10px;color:var(--text-muted);white-space:nowrap;">
          <span>Pass 1 (Male / Base)</span>
          <select id="sceneBackendM" style="font-size:11px;padding:3px 5px;">${optHtml}</select>
        </label>
        <label style="display:flex;flex-direction:column;gap:2px;font-size:10px;color:var(--text-muted);white-space:nowrap;">
          <span>Pass 2 (Lipsync)</span>
          <select id="sceneBackendF" style="font-size:11px;padding:3px 5px;">${optHtml}</select>
        </label>
        <label style="display:flex;flex-direction:column;gap:2px;font-size:10px;color:var(--text-muted);white-space:nowrap;">
          <span>Pass 2 ROI Scope</span>
          <select id="scenePass2Roi" style="font-size:11px;padding:3px 5px;">${roiHtml}</select>
        </label>
        <label id="scenePass2RoiSideWrap" style="display:none;flex-direction:column;gap:2px;font-size:10px;color:var(--text-muted);white-space:nowrap;">
          <span>ROI Side</span>
          <select id="scenePass2RoiSide" style="font-size:11px;padding:3px 5px;">
            <option value="">Right (Pass 2)</option>
            <option value="left">Left</option>
          </select>
        </label>
        <div id="scenePass2RoiBoxWrap" style="display:none;align-items:center;gap:6px;">
          <span style="font-size:10px;color:var(--text-muted);white-space:nowrap;">ROI Box:</span>
          <input id="scenePass2RoiBox" type="text" placeholder="0.45,0.05,0.98,0.85" style="flex:1;font-size:11px;padding:2px 6px;" />
        </div>
      </div>
    `;
    const hint = document.getElementById("sceneFalHint");
    if (hint && hint.parentElement === host) host.insertBefore(row, hint);
    else host.appendChild(row);

    const activeSc = () => window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
    document.getElementById("sceneBackendM")?.addEventListener("change", (e) => {
      const sc = activeSc();
      if (!sc || sc.locked) return;
      const v = e.target.value;
      guardSceneComboChange(sc, { backend_m: v || undefined }, {
        onApply: () => {
          if (v) sc.backend_m = v; else delete sc.backend_m;
          _notifySceneUpdated(sc, `Change ${sc.id} Pass 1 Engine`);
        },
        onRevert: () => { e.target.value = sc.backend_m || ""; },
      });
    });
    document.getElementById("sceneBackendF")?.addEventListener("change", (e) => {
      const sc = activeSc();
      if (!sc || sc.locked) return;
      const v = e.target.value;
      guardSceneComboChange(sc, { backend_f: v || undefined }, {
        onApply: () => {
          if (v) sc.backend_f = v; else delete sc.backend_f;
          _notifySceneUpdated(sc, `Change ${sc.id} Pass 2 Engine`);
        },
        onRevert: () => { e.target.value = sc.backend_f || ""; },
      });
    });
    document.getElementById("scenePass2Roi")?.addEventListener("change", (e) => {
      const sc = activeSc();
      if (!sc || sc.locked) return;
      const val = e.target.value;
      const roiPatchVal = (!val || val === "full") ? undefined : val;
      guardSceneComboChange(sc, { pass2_roi_mode: roiPatchVal }, {
        onApply: () => {
          if (!val || val === "full") {
            delete sc.pass2_roi_mode;
          } else {
            sc.pass2_roi_mode = val;
            if (val === "custom" && !parseRoiBoxStr(sc.pass2_roi_box)) {
              sc.pass2_roi_box = [0.45, 0.05, 0.98, 0.85];
            }
          }
          _notifySceneUpdated(sc, `Change ${sc.id} Pass 2 ROI`);
        },
        onRevert: () => {
          const prevRoi = normalizeRoiModeJS(sc.pass2_roi_mode);
          e.target.value = prevRoi === "full" ? "" : prevRoi;
        },
      });
    });
    document.getElementById("scenePass2RoiSide")?.addEventListener("change", (e) => {
      const sc = activeSc();
      if (!sc || sc.locked) return;
      if (e.target.value === "left") sc.pass2_roi_side = "left"; else delete sc.pass2_roi_side;
      _notifySceneUpdated(sc, `Change ${sc.id} Pass 2 ROI Side`);
    });
    const roiBoxEl = document.getElementById("scenePass2RoiBox");
    if (roiBoxEl) {
      roiBoxEl.addEventListener("focus", () => {
        const sc = activeSc();
        if (!sc || sc.locked) return;
        window.beginEditSession?.(`roi:${sc.id}`, `Edit ${sc.id} Pass 2 ROI Box`);
      });
      roiBoxEl.addEventListener("input", () => {
        const sc = activeSc();
        if (!sc || sc.locked) return;
        const box = parseRoiBoxStr(roiBoxEl.value);
        if (box) {
          sc.pass2_roi_box = box;
          roiBoxEl.style.borderColor = "";
          window.markProjectDirty?.();
        } else {
          roiBoxEl.style.borderColor = "#ef4444";
        }
      });
      roiBoxEl.addEventListener("blur", () => {
        const sc = activeSc();
        if (!sc || sc.locked) { window.commitEditSession?.(); return; }
        const box = parseRoiBoxStr(roiBoxEl.value);
        if (!box) {
          window.showToast?.("ROI Box must be 4 normalized floats x0,y0,x1,y1 in 0..1", 4000);
          roiBoxEl.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85";
          roiBoxEl.style.borderColor = "";
        } else {
          sc.pass2_roi_box = box;
          _notifySceneUpdated(sc, null);
        }
        window.commitEditSession?.();
      });
    }
  }
  window.ensureDuetPassControls = ensureDuetPassControls;

  function _passSummary(sc, passNum, effB, roiMode) {
    const dur = Math.max(4.0, (Number(sc.end || 0) - Number(sc.start || 0)) || 5.0);
    if (effB === "local") {
      if (passNum === 2 && roiMode !== "full") return `Local GVA (${roiMode === "tight_performer" ? "Tight ROI" : (roiMode === "custom" ? "Custom ROI" : "Half ROI")}, $0)`;
      return "Local GPU ($0)";
    }
    if (effB === "fal") {
      if (passNum === 2 && roiMode !== "full") return `fal Sync-v2 ROI (~$${(dur * 0.05).toFixed(2)})`;
      const est = window.estimateSceneCloudCostFrontend?.(sc, "fal");
      return `fal.ai (${est?.costStr || `~$${(dur * 0.08).toFixed(2)}`})`;
    }
    if (effB === "minimax") {
      const est = window.estimateSceneCloudCostFrontend?.(sc, "minimax");
      return `MiniMax (${est?.costStr || `~$${(dur * 0.08).toFixed(2)}`})`;
    }
    return effB;
  }

  function syncDuetPassControlsUI(sc) {
    const grp = document.getElementById("videoRendererGroup");
    ensureDuetPassControls(grp);
    const row = document.getElementById("duetPassEngineRow");
    if (!row || !sc) return;
    const isTwo = isTwoPassScene(sc);
    row.style.display = isTwo ? "flex" : "none";
    if (!isTwo) return;

    const selM = document.getElementById("sceneBackendM");
    const selF = document.getElementById("sceneBackendF");
    const selRoi = document.getElementById("scenePass2Roi");
    const selSide = document.getElementById("scenePass2RoiSide");
    const sideWrap = document.getElementById("scenePass2RoiSideWrap");
    const boxWrap = document.getElementById("scenePass2RoiBoxWrap");
    const boxInp = document.getElementById("scenePass2RoiBox");
    const locked = Boolean(sc.locked);

    const effBase = window.resolveEffectiveSceneBackend?.(sc) || "local";
    const inhLbl = `Inherit (${effBase})`;
    if (selM) {
      if (selM.options[0]) selM.options[0].textContent = inhLbl;
      selM.value = sc.backend_m || "";
      selM.disabled = locked;
    }
    if (selF) {
      if (selF.options[0]) selF.options[0].textContent = inhLbl;
      selF.value = sc.backend_f || "";
      selF.disabled = locked;
    }
    const roiMode = normalizeRoiModeJS(sc.pass2_roi_mode);
    if (selRoi) {
      selRoi.value = roiMode === "full" ? "" : roiMode;
      selRoi.disabled = locked;
    }
    if (sideWrap && selSide) {
      sideWrap.style.display = roiMode !== "full" ? "flex" : "none";
      selSide.value = sc.pass2_roi_side === "left" ? "left" : "";
      selSide.disabled = locked;
    }
    if (boxWrap && boxInp) {
      boxWrap.style.display = roiMode === "custom" ? "flex" : "none";
      if (!boxInp.matches?.(":focus")) {
        boxInp.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85";
        boxInp.style.borderColor = "";
      }
      boxInp.disabled = locked;
    }

    const hint = document.getElementById("sceneFalHint");
    if (hint) {
      const b1 = resolvePassBackend(sc, 1);
      const b2 = resolvePassBackend(sc, 2);
      hint.style.display = "block";
      hint.textContent = `Two-Pass: P1 ${_passSummary(sc, 1, b1, "full")} -> P2 ${_passSummary(sc, 2, b2, roiMode)}`;
    }
  }
  window.syncDuetPassControlsUI = syncDuetPassControlsUI;

  function _mkSelect(opts, val, title, onChange) {
    const sel = document.createElement("select");
    sel.title = title;
    opts.forEach(([v, l]) => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = l;
      sel.appendChild(o);
    });
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

  function appendTwoPassEngineControls(row1, sc, idx, applyFieldSideEffects) {
    if (!row1 || !sc || !isTwoPassScene(sc)) return;
    const syncAll = () => {
      applyFieldSideEffects?.(idx, sc);
      if (window.state?.selectedSceneIdx === idx) window.syncSceneBackendUI?.(sc);
    };
    const p1Sel = _mkSelect(ENGINE_OPTS, sc.backend_m || "", "Pass 1 execution engine", (v) => {
      guardSceneComboChange(sc, { backend_m: v || undefined }, {
        onApply: () => {
          if (v) sc.backend_m = v; else delete sc.backend_m;
          window.recordHistorySnapshot?.(`Change ${sc.id} Pass 1 Engine`);
          syncAll();
        },
        onRevert: () => { p1Sel.value = sc.backend_m || ""; },
      });
    });
    const p2Sel = _mkSelect(ENGINE_OPTS, sc.backend_f || "", "Pass 2 execution engine", (v) => {
      guardSceneComboChange(sc, { backend_f: v || undefined }, {
        onApply: () => {
          if (v) sc.backend_f = v; else delete sc.backend_f;
          window.recordHistorySnapshot?.(`Change ${sc.id} Pass 2 Engine`);
          syncAll();
        },
        onRevert: () => { p2Sel.value = sc.backend_f || ""; },
      });
    });
    const curRoi = normalizeRoiModeJS(sc.pass2_roi_mode);
    const roiSel = _mkSelect(ROI_OPTS, curRoi === "full" ? "" : curRoi, "Pass 2 ROI crop acceleration mode", (v) => {
      const roiPatchVal = (!v || v === "full") ? undefined : v;
      guardSceneComboChange(sc, { pass2_roi_mode: roiPatchVal }, {
        onApply: () => {
          if (!v || v === "full") delete sc.pass2_roi_mode;
          else {
            sc.pass2_roi_mode = v;
            if (v === "custom" && !parseRoiBoxStr(sc.pass2_roi_box)) sc.pass2_roi_box = [0.45, 0.05, 0.98, 0.85];
          }
          window.recordHistorySnapshot?.(`Change ${sc.id} Pass 2 ROI`);
          syncAll();
        },
        onRevert: () => {
          const prev = normalizeRoiModeJS(sc.pass2_roi_mode);
          roiSel.value = prev === "full" ? "" : prev;
        },
      });
    });
    row1.appendChild(_mkCardField("P1 Engine", "96px", p1Sel));
    row1.appendChild(_mkCardField("P2 Engine", "96px", p2Sel));
    row1.appendChild(_mkCardField("P2 ROI", "118px", roiSel));
    if (curRoi !== "full") {
      const sideSel = _mkSelect([["", "Right (P2)"], ["left", "Left"]], sc.pass2_roi_side === "left" ? "left" : "", "Pass 2 ROI side", (v) => {
        if (v === "left") sc.pass2_roi_side = "left"; else delete sc.pass2_roi_side;
        window.recordHistorySnapshot?.(`Change ${sc.id} Pass 2 ROI Side`);
        syncAll();
      });
      row1.appendChild(_mkCardField("P2 Side", "86px", sideSel));
    }
    if (curRoi === "custom") {
      const bx = document.createElement("input");
      bx.type = "text";
      bx.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85";
      bx.title = "Custom ROI box (x0,y0,x1,y1 in 0..1)";
      bx.onchange = () => {
        const parsed = parseRoiBoxStr(bx.value);
        if (!parsed) {
          window.showToast?.("ROI Box must be 4 normalized floats x0,y0,x1,y1 in 0..1", 4000);
          bx.value = Array.isArray(sc.pass2_roi_box) ? sc.pass2_roi_box.join(", ") : "0.45,0.05,0.98,0.85";
          return;
        }
        sc.pass2_roi_box = parsed;
        window.recordHistorySnapshot?.(`Edit ${sc.id} Pass 2 ROI Box`);
        syncAll();
      };
      row1.appendChild(_mkCardField("P2 ROI Box", "145px", bx));
    }
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
    (window.state?.assets?.audio_stems || []).forEach(pushStem);
    const refSel = document.getElementById("sceneStemLeft") || document.getElementById("sceneStem");
    if (refSel) {
      Array.from(refSel.options || []).forEach((o) => pushStem(o.value));
    }
    pushStem(scene?.stem_f);
    pushStem(scene?.stem_m);
    pushStem(scene?.stem);
    if (list.length <= 1) ["female", "male", "backing", "lead"].forEach(pushStem);
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
        guardRepairComboAction(sc, { isRepair: true, backend, roi_mode }, async () => {
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
