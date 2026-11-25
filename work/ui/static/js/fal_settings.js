// Cloud Rendering & fal.ai DPAPI Key / Backend Override Controller
(function () {
  "use strict";

  let falStatusCache = null;

  function _esc(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function getEffectiveProjectBackend() {
    const pb = String(window.state?.projectData?.models?.default_backend || "").trim().toLowerCase();
    if (pb === "fal" || pb === "minimax" || pb === "local") return pb;
    const mb = String(falStatusCache?.default_execution_backend || "").trim().toLowerCase();
    return (mb === "fal" || mb === "minimax") ? mb : "local";
  }
  window.getEffectiveProjectBackend = getEffectiveProjectBackend;

  function resolveEffectiveSceneBackend(sc) {
    if (!sc) return getEffectiveProjectBackend();
    const vrId = String(sc.renderer ?? window.state?.projectData?.models?.default_video_renderer ?? "1");
    if (window.state?.models?.fal_video_renderers?.[vrId]) return "fal";
    if (window.state?.models?.minimax_video_renderers?.[vrId]) return "minimax";
    if (window.state?.models?.video_renderers?.[vrId]?.local_only) return "local";
    const scB = String(sc.backend || "").trim().toLowerCase();
    if (scB === "fal" || scB === "minimax" || scB === "local") return scB;
    return getEffectiveProjectBackend();
  }
  window.resolveEffectiveSceneBackend = resolveEffectiveSceneBackend;

  function estimateSceneCloudCostFrontend(sc, effBackend, passNum = null) {
    if (!sc) return null;
    const rawR = passNum === 1 ? (sc.renderer_m ?? sc.renderer) : (passNum === 2 ? (sc.renderer_f ?? sc.renderer) : sc.renderer);
    const vrId = String((rawR !== null && rawR !== undefined && rawR !== "") ? rawR : (window.state?.projectData?.models?.default_video_renderer ?? "1"));
    const vSpec = window.state?.models?.fal_video_renderers?.[vrId] || window.state?.models?.minimax_video_renderers?.[vrId] || window.state?.models?.video_renderers?.[vrId] || {};
    const mtype = String(vSpec.model_type || "").toLowerCase();
    const dur = Math.max(4.0, (Number(sc.end || 0) - Number(sc.start || 0)) || 5.0);
    const hasEnd = passNum !== 2 && Boolean(sc.anchor_end);
    const mode = String(sc.mode || "solo").toLowerCase();
    const resStr = String(sc.resolution || window.state?.projectData?.models?.default_video_resolution || "4").toLowerCase();
    const tier = (resStr === "7" || resStr.includes("2k") || resStr.includes("1440") || resStr.includes("2048")) ? "2k" : ((resStr === "6" || resStr.includes("1088") || resStr.includes("1080")) ? "1080p" : (resStr === "1" || resStr.includes("480") ? "480p" : "768p"));
    const isTwoPass = ["duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right"].includes(mode);

    if (effBackend === "minimax") {
      let mmModel = vSpec.minimax_model || "MiniMax-H3";
      if (mmModel === "MiniMax-H3" && tier === "480p") mmModel = "MiniMax-H3-Highspeed";
      let rate = tier === "2k" ? 0.16 : (tier === "1080p" ? 0.12 : (tier === "480p" ? 0.04 : 0.08));
      if (vSpec.price_per_sec_usd && typeof vSpec.price_per_sec_usd === "object") {
        rate = Number(vSpec.price_per_sec_usd[tier] || vSpec.price_per_sec_usd["768p"] || rate);
      }
      const cost = Math.round(dur * rate * 100) / 100;
      const chunks = dur > 15.0 ? Math.ceil(dur / 15.0) : 1;
      return { endpoint: `api.minimax.io/v2/${mmModel}`, costStr: `~$${cost.toFixed(2)}`, dur, chunks, isTwoPass };
    }

    let endpoint = vSpec.fal_endpoint || "";
    let rate = 0.08;
    let maxDur = Number(vSpec.max_duration_s || 15.0);
    const rateFor = (p480, p768, p1080, p2k) => (tier === "2k" ? p2k : (tier === "1080p" ? p1080 : (tier === "480p" ? p480 : p768)));
    if (vSpec.price_per_sec_usd && typeof vSpec.price_per_sec_usd === "object") {
      rate = Number(vSpec.price_per_sec_usd[tier] || vSpec.price_per_sec_usd["768p"] || 0.08);
    }
    if (!endpoint) {
      if (mtype.includes("ltx2")) {
        const is25 = mtype.includes("25");
        endpoint = is25 ? "fal-ai/ltx-2.5/audio-to-video" : "fal-ai/ltx-2.3/audio-to-video";
        rate = is25 ? rateFor(0.05, 0.07, 0.10, 0.14) : rateFor(0.04, 0.06, 0.09, 0.12);
        maxDur = 20.0;
      } else if (vSpec.supports_image_refs || mtype.includes("ref2va") || mtype.includes("fasth3")) {
        endpoint = "minimax/h3/reference-to-video";
        rate = rateFor(0.05, 0.08, 0.12, 0.16);
      } else if (hasEnd) {
        endpoint = "minimax/h3/image-to-video";
        rate = rateFor(0.04, 0.07, 0.10, 0.14);
      } else {
        endpoint = "minimax/h3-max/lip-sync/image-to-video";
        rate = rateFor(0.05, 0.08, 0.12, 0.16);
      }
    }
    const cost = Math.round(dur * rate * 100) / 100;
    const chunks = maxDur > 0 && dur > maxDur ? Math.ceil(dur / maxDur) : 1;
    return { endpoint, costStr: `~$${cost.toFixed(2)}`, dur, chunks, isTwoPass };
  }
  window.estimateSceneCloudCostFrontend = estimateSceneCloudCostFrontend;

  function _applyBadgeState(badge, clearBtn, st, envName) {
    if (!badge) return;
    if (!st) {
      badge.textContent = "Checking...";
      badge.style.color = "#94a3b8";
      return;
    }
    if (st.configured) {
      const srcLabel = st.source === "dpapi" ? "Encrypted via Windows DPAPI" : (st.source === "env" ? `From ${envName} Env Var` : "Configured");
      badge.textContent = `● Connected (${srcLabel}: ${st.hint || "***"})`;
      badge.style.color = "#34d399";
      if (clearBtn) clearBtn.style.display = st.source === "dpapi" ? "inline-flex" : "none";
    } else {
      badge.textContent = st.dpapi_supported ? "○ No Key Saved (DPAPI Ready)" : "○ No Key Configured";
      badge.style.color = "#fbbf24";
      if (clearBtn) clearBtn.style.display = "none";
    }
  }

  function renderStatusBadge() {
    _applyBadgeState(document.getElementById("falKeyStatusBadge"), document.getElementById("btnClearFalKey"), falStatusCache, "FAL_KEY");
    _applyBadgeState(document.getElementById("minimaxKeyStatusBadge"), document.getElementById("btnClearMinimaxKey"), falStatusCache?.minimax, "MINIMAX_API_KEY");
    const appInp = document.getElementById("falCustomAppInput");
    if (appInp && !appInp.matches(":focus") && falStatusCache?.custom_fal_app !== undefined) {
      appInp.value = falStatusCache.custom_fal_app || "";
    }
    const mDef = document.getElementById("falMachineBackend");
    if (mDef && !mDef.matches(":focus") && falStatusCache?.default_execution_backend) mDef.value = falStatusCache.default_execution_backend;
    syncProjectBackendSelectors();
    if (window.state?.selectedSceneIdx !== null) {
      const sc = window.state.projectData?.scenes?.[window.state.selectedSceneIdx];
      if (sc) syncSceneBackendUI(sc);
    }
  }

  async function refreshFalStatus() {
    try {
      const slug = window.state?.currentSlug || "";
      const q = slug ? `?slug=${encodeURIComponent(slug)}` : "";
      const resp = await fetch(`/api/fal/status${q}`);
      if (!resp.ok) return;
      falStatusCache = await resp.json();
      renderStatusBadge();
    } catch (e) {
      /* non-fatal when offline */
    }
  }
  window.refreshFalStatus = refreshFalStatus;

  function syncProjectBackendSelectors() {
    const models = window.state?.projectData?.models || {};
    const bSel = document.getElementById("cfgDefaultBackend");
    if (bSel) bSel.value = String(models.default_backend || "");
    const uSel = document.getElementById("cfgDefaultUpscaleBackend");
    if (uSel) uSel.value = String(models.default_upscale_backend || "");
  }
  window.syncProjectBackendSelectors = syncProjectBackendSelectors;

  function ensureSettingsSection() {
    const tabSettings = document.getElementById("tab-settings");
    if (!tabSettings || document.getElementById("falSettingsSection")) return;

    const negGroup = document.getElementById("cfgNegativePrompt")?.closest(".form-group");
    const sec = document.createElement("div");
    sec.id = "falSettingsSection";
    sec.style.cssText = "margin-top: 22px; padding: 14px 16px; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 8px; background: rgba(15, 23, 42, 0.45);";
    sec.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:8px;">
        <h3 style="font-size:14px;margin:0;color:#38bdf8;">☁️ Cloud Rendering (fal.ai &amp; MiniMax Direct API)</h3>
        <span id="falKeyStatusBadge" style="font-size:11px;font-weight:600;color:#94a3b8;">Checking...</span>
      </div>
      <p style="font-size:11px;color:var(--text-muted);margin:0 0 12px 0;">
        Top dropdowns control this project's execution defaults (saved in project JSON). <strong>API keys and machine defaults below are shared across all projects</strong>, encrypted locally via Windows DPAPI.
      </p>
      <div class="form-grid">
        <div class="form-group">
          <label for="cfgDefaultBackend">Project Default Execution Backend</label>
          <select id="cfgDefaultBackend">
            <option value="">Machine Default (Local Wan2GP)</option>
            <option value="local">🖥️ Local GPU (Wan2GP)</option>
            <option value="minimax">🎥 Cloud (MiniMax Direct API — H3)</option>
            <option value="fal">☁️ Cloud (fal.ai)</option>
          </select>
        </div>
        <div class="form-group">
          <label for="cfgDefaultUpscaleBackend">Default Late AI Upscale Backend</label>
          <select id="cfgDefaultUpscaleBackend">
            <option value="">Inherit Execution Default</option>
            <option value="local">🖥️ Local GPU (Wan2GP)</option>
            <option value="fal">☁️ Cloud (fal.ai SeedVR2 / FlashVSR)</option>
          </select>
        </div>
        <div class="form-group">
          <label for="falMachineBackend">Machine Fallback Backend <span style="opacity:0.7;">(All Projects)</span></label>
          <select id="falMachineBackend">
            <option value="local">🖥️ Local GPU (Wan2GP)</option>
            <option value="minimax">🎥 Cloud (MiniMax Direct API — H3)</option>
            <option value="fal">☁️ Cloud (fal.ai)</option>
          </select>
        </div>
        <div class="form-group">
          <label for="falCustomAppInput">Custom Wan2GP fal App ID <span style="opacity:0.7;">(Shared)</span></label>
          <input type="text" id="falCustomAppInput" placeholder="e.g. username/wan2gp-worker">
        </div>
      </div>
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.4px;color:#7dd3fc;margin:10px 0 4px 0;">Shared API Credentials (Used by All Projects)</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin-top:4px;">
        <div class="form-group" style="flex:1 1 260px;margin:0;">
          <label for="falKeyInput">fal.ai API Key (Shared Across Projects — Windows DPAPI)</label>
          <input type="password" id="falKeyInput" placeholder="Paste key_id:key_secret to encrypt and store locally" autocomplete="off">
        </div>
        <button type="button" class="btn btn-primary" id="btnSaveFalKey" style="height:34px;">🔐 Save Cloud Config</button>
        <button type="button" class="btn" id="btnTestFalKey" style="height:34px;">🧪 Test Connection</button>
        <button type="button" class="btn btn-danger" id="btnClearFalKey" style="height:34px;display:none;">🗑 Clear Key</button>
      </div>
      <div id="falTestResultMsg" style="font-size:11px;margin-top:6px;display:none;"></div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin-top:12px;padding-top:10px;border-top:1px solid rgba(148,163,184,0.16);">
        <div class="form-group" style="flex:1 1 260px;margin:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <label for="minimaxKeyInput" style="margin:0;">MiniMax API Key (Shared Across Projects — Windows DPAPI)</label>
            <span id="minimaxKeyStatusBadge" style="font-size:10px;font-weight:600;color:#94a3b8;">Checking...</span>
          </div>
          <input type="password" id="minimaxKeyInput" placeholder="Paste MiniMax Bearer API key for MiniMax-H3" autocomplete="off">
        </div>
        <button type="button" class="btn btn-primary" id="btnSaveMinimaxKey" style="height:34px;">🔐 Save MiniMax Key</button>
        <button type="button" class="btn" id="btnTestMinimaxKey" style="height:34px;">🧪 Test MiniMax</button>
        <button type="button" class="btn btn-danger" id="btnClearMinimaxKey" style="height:34px;display:none;">🗑 Clear</button>
      </div>
      <div id="minimaxTestResultMsg" style="font-size:11px;margin-top:6px;display:none;"></div>
    `;
    if (negGroup && negGroup.parentNode === tabSettings) tabSettings.insertBefore(sec, negGroup);
    else tabSettings.appendChild(sec);

    const onProjBackendChange = (fieldKey, label) => (e) => {
      if (!window.state?.projectData) return;
      const m = (window.state.projectData.models = window.state.projectData.models || {});
      const val = e.target.value;
      if (val) m[fieldKey] = val;
      else delete m[fieldKey];
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Change Project ${label}`);
      if (window.state.selectedSceneIdx !== null) {
        const sc = window.state.projectData.scenes?.[window.state.selectedSceneIdx];
        if (sc) syncSceneBackendUI(sc);
      }
      window.renderSceneList?.();
    };
    document.getElementById("cfgDefaultBackend")?.addEventListener("change", onProjBackendChange("default_backend", "Default Backend"));
    document.getElementById("cfgDefaultUpscaleBackend")?.addEventListener("change", onProjBackendChange("default_upscale_backend", "Default Upscale Backend"));

    const saveCloudConfig = async (opts = {}) => {
      const { clearFal = false, clearMinimax = false, msgId = "falTestResultMsg" } = opts;
      const falEl = document.getElementById("falKeyInput");
      const mmEl = document.getElementById("minimaxKeyInput");
      const msgEl = document.getElementById(msgId);
      const payload = {
        slug: window.state?.currentSlug || "",
        default_execution_backend: document.getElementById("falMachineBackend")?.value || "local",
        custom_fal_app: document.getElementById("falCustomAppInput")?.value || "",
      };
      if (clearFal) payload.fal_key = "";
      else if (falEl && falEl.value.trim()) payload.fal_key = falEl.value.trim();
      if (clearMinimax) payload.minimax_key = "";
      else if (mmEl && mmEl.value.trim()) payload.minimax_key = mmEl.value.trim();
      try {
        const resp = await fetch("/api/fal/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await resp.json();
        if (!resp.ok || !data.success) throw new Error(data.error || "Save failed");
        falStatusCache = data;
        if (falEl) falEl.value = "";
        if (mmEl) mmEl.value = "";
        renderStatusBadge();
        window.renderSceneList?.();
        window.loadProjectQueue?.(window.state?.currentSlug);
        window.showToast?.((clearFal || clearMinimax) ? "Cleared saved API key." : "🔐 Saved cloud configuration cleanly!");
        if (msgEl) msgEl.style.display = "none";
      } catch (err) {
        const msg = /endpoint not found/i.test(err.message) ? "Endpoint not found — restart launch_ui.bat to load the updated Studio server." : err.message;
        if (msgEl) { msgEl.style.display = "block"; msgEl.style.color = "#ef4444"; msgEl.textContent = msg; }
      }
    };

    const runTestConnection = async (endpoint, inputId, bodyKey, btnId, msgId, idleLabel) => {
      const msgEl = document.getElementById(msgId);
      const btn = document.getElementById(btnId);
      if (btn) { btn.disabled = true; btn.textContent = "Testing..."; }
      try {
        const rawKey = document.getElementById(inputId)?.value?.trim() || "";
        const resp = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slug: window.state?.currentSlug || "", [bodyKey]: rawKey || undefined }),
        });
        const data = await resp.json();
        if (msgEl) {
          msgEl.style.display = "block";
          msgEl.style.color = data.success ? "#34d399" : "#ef4444";
          const rawErr = data.error || "Test failed";
          const errText = /endpoint not found/i.test(rawErr) ? "Endpoint not found — restart launch_ui.bat to load the updated Studio server." : rawErr;
          msgEl.textContent = data.success ? `✅ ${data.message || "Connection verified!"} (${data.source})` : `❌ ${errText}`;
        }
      } catch (err) {
        if (msgEl) { msgEl.style.display = "block"; msgEl.style.color = "#ef4444"; msgEl.textContent = `❌ ${err.message}`; }
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = idleLabel; }
      }
    };

    document.getElementById("btnSaveFalKey")?.addEventListener("click", () => saveCloudConfig());
    document.getElementById("btnClearFalKey")?.addEventListener("click", () => saveCloudConfig({ clearFal: true }));
    document.getElementById("btnTestFalKey")?.addEventListener("click", () => runTestConnection("/api/fal/test", "falKeyInput", "fal_key", "btnTestFalKey", "falTestResultMsg", "🧪 Test Connection"));
    document.getElementById("btnSaveMinimaxKey")?.addEventListener("click", () => saveCloudConfig({ msgId: "minimaxTestResultMsg" }));
    document.getElementById("btnClearMinimaxKey")?.addEventListener("click", () => saveCloudConfig({ clearMinimax: true, msgId: "minimaxTestResultMsg" }));
    document.getElementById("btnTestMinimaxKey")?.addEventListener("click", () => runTestConnection("/api/minimax/test", "minimaxKeyInput", "minimax_key", "btnTestMinimaxKey", "minimaxTestResultMsg", "🧪 Test MiniMax"));
    syncProjectBackendSelectors();
    refreshFalStatus();
  }

  function ensureSceneBackendControl() {
    const grp = document.getElementById("videoRendererGroup");
    if (grp && !document.getElementById("sceneBackend")) {
      const wrap = document.createElement("div");
      wrap.id = "sceneBackendRow";
      wrap.style.cssText = "display:flex;align-items:center;gap:6px;margin-top:6px;";
      wrap.innerHTML = `
        <span style="font-size:10px;color:var(--text-muted);font-weight:600;white-space:nowrap;">Engine:</span>
        <select id="sceneBackend" style="flex:1;font-size:11px;padding:3px 6px;">
          <option value="">Inherit Project</option>
          <option value="local">🖥️ Local GPU</option>
          <option value="minimax">🎥 MiniMax API</option>
          <option value="fal">☁️ fal.ai Cloud</option>
        </select>
        <span id="sceneBackendBadge" style="display:none;font-size:10px;padding:1px 6px;border-radius:4px;font-weight:600;white-space:nowrap;"></span>
      `;
      grp.appendChild(wrap);
      const hintHost = document.getElementById("duetPassEngineHost") || grp;
      window.ensureDuetPassControls?.(grp);
      const hint = document.createElement("div");
      hint.id = "sceneFalHint";
      hint.style.cssText = "font-size:10px;color:#38bdf8;margin-top:2px;display:none;";
      hintHost.appendChild(hint);

      document.getElementById("sceneBackend")?.addEventListener("change", (e) => {
        if (window.state?.selectedSceneIdx === null) return;
        const sc = window.state.projectData?.scenes?.[window.state.selectedSceneIdx];
        if (!sc || sc.locked) return;
        const v = e.target.value;
        const apply = () => {
          if (v) sc.backend = v;
          else delete sc.backend;
          window.markProjectDirty?.();
          window.redrawTimeline?.();
          window.updateInspectorSceneHeader?.(sc);
          syncSceneBackendUI(sc);
          window.recordHistorySnapshot?.(`Change ${sc.id} Execution Backend`);
          window.refreshTakeDiffUI?.();
        };
        if (window.guardSceneComboChange) {
          window.guardSceneComboChange(sc, { backend: v || undefined }, {
            onApply: apply,
            onRevert: () => { e.target.value = sc.backend || ""; },
          });
        } else {
          apply();
        }
      });
    }

    const upSel = document.getElementById("sceneLateUpscaleMethod");
    if (upSel && upSel.parentElement && !document.getElementById("sceneLateUpscaleBackend")) {
      const bUp = document.createElement("select");
      bUp.id = "sceneLateUpscaleBackend";
      bUp.title = "Execution backend for late AI upscaling";
      bUp.style.cssText = "font-size:11px;padding:3px 6px;";
      bUp.innerHTML = `<option value="">Default Engine</option><option value="local">🖥️ Local GPU</option><option value="fal">☁️ fal.ai</option>`;
      upSel.parentElement.insertBefore(bUp, upSel.nextSibling);
    }
  }

  function syncSceneBackendUI(sc) {
    ensureSceneBackendControl();
    const sel = document.getElementById("sceneBackend");
    const badge = document.getElementById("sceneBackendBadge");
    const hint = document.getElementById("sceneFalHint");
    if (!sel || !sc) return;

    const projDef = getEffectiveProjectBackend();
    const firstOpt = sel.options[0];
    if (firstOpt) firstOpt.textContent = `Inherit (${projDef === "fal" ? "☁️ fal.ai" : (projDef === "minimax" ? "🎥 MiniMax" : "🖥️ Local")})`;

    const vrId = String(sc.renderer ?? window.state?.projectData?.models?.default_video_renderer ?? "1");
    const isDedicatedFal = Boolean(window.state?.models?.fal_video_renderers?.[vrId]);
    const isDedicatedMm = Boolean(window.state?.models?.minimax_video_renderers?.[vrId]);
    const isLocalOnly = Boolean(window.state?.models?.video_renderers?.[vrId]?.local_only) && !isDedicatedFal && !isDedicatedMm;
    const forceDis = isDedicatedFal || isDedicatedMm || isLocalOnly;
    sel.dataset.forceDisabled = forceDis ? "1" : "";
    sel.value = isDedicatedFal ? "fal" : (isDedicatedMm ? "minimax" : (isLocalOnly ? "local" : (sc.backend || "")));
    sel.disabled = Boolean(sc.locked) || forceDis;
    const upB = document.getElementById("sceneLateUpscaleBackend");
    if (upB && upB.options[0]) {
      const upDef = String(window.state?.projectData?.models?.default_upscale_backend || "").trim().toLowerCase() || projDef;
      upB.options[0].textContent = upDef === "fal" ? "Default (☁️ fal.ai)" : "Default (🖥️ Local)";
    }

    const eff = resolveEffectiveSceneBackend(sc);
    if (badge) {
      if (sc.backend || isDedicatedFal || isDedicatedMm || isLocalOnly) {
        badge.style.display = "inline-block";
        badge.textContent = eff === "fal" ? "☁️ fal.ai" : (eff === "minimax" ? "🎥 MiniMax" : "🖥️ Local");
        badge.style.background = eff === "fal" ? "rgba(56,189,248,0.18)" : (eff === "minimax" ? "rgba(168,85,247,0.18)" : "rgba(148,163,184,0.18)");
        badge.style.color = eff === "fal" ? "#38bdf8" : (eff === "minimax" ? "#c084fc" : "#cbd5e1");
      } else {
        badge.style.display = "none";
      }
    }
    if (hint) {
      if (eff === "fal" || eff === "minimax") {
        const est = estimateSceneCloudCostFrontend(sc, eff);
        const notes = [];
        if (est.chunks > 1) notes.push(`${est.chunks} auto-chunks`);
        if (est.isTwoPass) notes.push(eff === "minimax" ? "Pass 2 MiniMax ROI" : "Pass 2 ROI lip-sync");
        hint.textContent = `${eff === "minimax" ? "🎥" : "☁️"} ${est.endpoint} (${est.costStr}${notes.length ? `, ${notes.join(", ")}` : ""})`;
        hint.style.color = eff === "minimax" ? "#c084fc" : "#38bdf8";
        hint.style.display = "block";
      } else {
        hint.style.display = "none";
      }
    }
    window.syncDuetPassControlsUI?.(sc);
    window.renderSceneRefPickerUI?.(sc);
  }
  window.syncSceneBackendUI = syncSceneBackendUI;

  document.addEventListener?.("DOMContentLoaded", () => {
    ensureSettingsSection();
    ensureSceneBackendControl();
  });
  window.addEventListener?.("projectLoaded", () => {
    ensureSettingsSection();
    ["falTestResultMsg", "minimaxTestResultMsg"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "none";
    });
    syncProjectBackendSelectors();
    refreshFalStatus();
  });
})();
