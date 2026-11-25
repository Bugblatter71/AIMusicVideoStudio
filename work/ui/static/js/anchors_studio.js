/**
 * Music Video Generation Studio - Interactive Anchor Studio
 * Allows creating anchors (blank or from scenes), editing prompts with @character
 * autocomplete, choosing generator models/resolutions/seeds/refs, importing images,
 * and generating single, missing, or all Stage 1 anchor frames directly from UI.
 */
(function () {
  "use strict";

  let highlightedAnchorId = null;
  let activeUploadTarget = null;
  let liveAnchorJob = null;

  const ANCHOR_RES_OPTIONS = [
    ["", "Default (Model / Shared)"], ["1920x1080", "1920×1080 (1080p Full HD)"], ["1920x1088", "1920×1088 (1080p Block-Aligned)"],
    ["1664x960", "1664×960 (2K Qwen Native)"], ["1344x768", "1344×768 (768p Widescreen)"], ["1280x720", "1280×720 (720p HD)"],
    ["960x544", "960×544 (540p Balanced)"], ["832x480", "832×480 (480p Fast Draft)"],
    ["2720x1536", "2720×1536 (16:9, 4 MP H3-Image Native)"], ["3872x2176", "3872×2176 (16:9, 8 MP P-H3 Native)"],
    ["5472x3072", "5472×3072 (16:9, 16 MP H3-Image Best)"], ["2048x2048", "2048×2048 (1:1, 4 MP Square)"],
    ["2912x2912", "2912×2912 (1:1, 8 MP Square)"], ["4096x4096", "4096×4096 (1:1, 16 MP Square)"],
  ];

  function cleanMotionPrompt(txt) {
    return String(txt || "")
      .replace(/^For the target video,[^\n]*(?:\n|$)/gim, "")
      .replace(/^(?:integrated_multimodal_description|detailed_description):\s*(?:\[Shot\s*\d+\]\s*)?/gim, "")
      .replace(/(?:^|\s)Continue directly from\s*<Picture\s*\d+>\.?\s*/gim, " ")
      .replace(/^(?:overall_soundscape|non_diegetic_music|soundtrack):[^\n]*(?:\n|$)/gim, "")
      .replace(/\[Shot\s*\d+\]\s*|<Picture\s*\d+>|<(?!image\d*)([^>]+)>/gi, "")
      .replace(/\s+with\s+(?:expressive,\s*)?synchronized\s+lip\s+movements/gi, "")
      .replace(/\s+/g, " ").trim();
  }

  function deriveAnchorPromptFromScene(sc) {
    if (!sc) return "";
    let raw = "";
    if (sc.prompt && String(sc.prompt).trim()) {
      raw = cleanMotionPrompt(sc.prompt);
    } else if (sc.prompt_m || sc.prompt_f) {
      const p1 = cleanMotionPrompt(sc.prompt_m || ""), p2 = cleanMotionPrompt(sc.prompt_f || "");
      if (p1 && p2) {
        const p1Low = p1.toLowerCase();
        const extra = p2.split(/(?<=[.!?])\s+/).map((s) => s.trim()).filter((s) => s && !p1Low.includes(s.toLowerCase()));
        raw = extra.length ? `${p1.replace(/,?\s*while the partner[^.!?]*/i, "")}. ${extra.join(" ")}` : p1;
      } else raw = p1 || p2;
    } else if (sc.prompt_components && typeof sc.prompt_components === "object") {
      const pc = sc.prompt_components;
      raw = cleanMotionPrompt([pc.camera, pc.atmosphere, pc.performance, pc.musicians].filter(Boolean).join(" "));
    }
    raw = raw.replace(/\.\s*\./g, ".").replace(/\s+/g, " ").trim();
    return raw ? (/^cinematic\s+16:9\s+still\s+frame/i.test(raw) ? raw : `Cinematic 16:9 still frame, ${raw}`) : "";
  }

  function nextAnchorId() {
    const used = new Set();
    const collect = (v) => { const m = (window.extractAnchorId?.(v) || "").match(/^A(\d+)/i); if (m) used.add(parseInt(m[1], 10)); };
    (window.state?.projectData?.anchors || []).forEach((a) => collect(a.filename || a.id));
    Object.values(window.state?.assets?.anchors || {}).forEach((inf) => collect(inf.filename || inf.aid));
    let n = 1;
    while (used.has(n)) n++;
    return `A${String(n).padStart(2, "0")}`;
  }

  function sanitizeAnchorFilename(rawVal, fallbackAid) {
    const clean = String(rawVal || "").replace(/[\\/\0:*?"<>|]+/g, "_").trim();
    if (!clean) return `${fallbackAid}_Scene.jpg`;
    return /\.(jpe?g|png|webp)$/i.test(clean) ? clean : `${clean}.jpg`;
  }

  function getCharacterQuickTags() {
    const chars = (window.state?.charactersData && Object.keys(window.state.charactersData).length) ? window.state.charactersData : (window.state?.projectData?.characters || {});
    const out = [], seen = new Set();
    Object.entries(chars).forEach(([cid, ent]) => {
      const looks = ent?.looks || {}, defLook = ent?.default_look && looks[ent.default_look] ? ent.default_look : Object.keys(looks)[0];
      Object.entries(looks).forEach(([lid, lk]) => {
        const tag = lk?.tag || `@${cid}`, key = tag.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        out.push({ tag, name: ent?.name || cid, look: lid, isDefault: lid === defLook, refCount: Array.isArray(lk?.image_refs) ? lk.image_refs.length : 0 });
      });
    });
    return out;
  }

  function getAnchorSceneUsage(aid, filename) {
    const targetAid = String(aid || "").toUpperCase(), used = [];
    (window.state?.projectData?.scenes || []).forEach((sc, idx) => {
      if ((filename && sc.anchor === filename) || (targetAid && (window.extractAnchorId?.(sc.anchor) || "").toUpperCase() === targetAid)) used.push({ idx, id: sc.id, role: "Start" });
      if ((filename && sc.anchor_end === filename) || (targetAid && (window.extractAnchorId?.(sc.anchor_end) || "").toUpperCase() === targetAid)) used.push({ idx, id: sc.id, role: "End" });
    });
    return used;
  }

  function syncScenesViews() {
    window.updateInspectorAnchorsDropdown?.();
    if (document.getElementById("tab-scenes")?.classList?.contains("active")) window.renderSceneList?.();
  }

  function describeAnchorRefs(a) {
    if (window.describeAnchorSourceRefs) return window.describeAnchorSourceRefs(a);
    if (Array.isArray(a?.image_refs)) return a.image_refs.length ? `Custom source images (${a.image_refs.length})` : "Source images off (pure text-to-image override)";
    const prompt = String(a?.prompt || "");
    const tags = getCharacterQuickTags().filter((c) => new RegExp(`(^|\\W)${c.tag.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=\\W|$)`, "i").test(prompt));
    if (tags.length) return `Character refs: ${tags.map((t) => `${t.tag} (${t.refCount} img)`).join(", ")}`;
    const shCount = (window.state?.projectData?.models?.anchor_shared_settings?.image_refs || []).length;
    if (/\{[a-z0-9_]+\}|<image\d+>/i.test(prompt)) return shCount ? `Using ${shCount} shared anchor ref(s) for <imageN>` : "Uses {VAR} / <imageN> tags";
    return shCount ? `Auto: ${shCount} project shared ref(s) (or attach source images below)` : "Text-to-image (attach source images below or type @character)";
  }

  function renderBanner() {
    const bar = document.getElementById("anchorGenStatusBanner");
    if (!bar) return;
    if (!liveAnchorJob) { bar.style.display = "none"; return; }
    bar.style.display = "flex";
    const isErr = liveAnchorJob.state === "failed", isDone = liveAnchorJob.state === "completed";
    bar.style.background = isErr ? "rgba(239,68,68,0.12)" : isDone ? "rgba(16,185,129,0.12)" : "rgba(56,189,248,0.12)";
    bar.style.borderColor = isErr ? "rgba(239,68,68,0.4)" : isDone ? "rgba(16,185,129,0.4)" : "rgba(56,189,248,0.4)";
    const pctTxt = typeof liveAnchorJob.pct === "number" ? ` (${Math.round(liveAnchorJob.pct)}%)` : "";
    bar.innerHTML = `<span style="font-size:12px;font-weight:600;color:#e2e8f0;">${window.escapeHtml(liveAnchorJob.label || "Anchor Generation")}${pctTxt}</span>
      <span style="display:flex;gap:8px;align-items:center;">
        <button type="button" class="btn btn-sm" id="btnAnchorBannerQueue" style="font-size:11px;padding:2px 8px;">View Render Queue</button>
        <button type="button" class="btn btn-sm" id="btnAnchorBannerConsole" style="font-size:11px;padding:2px 8px;">View Console Logs</button>
        ${isDone || isErr ? `<button type="button" class="btn btn-sm" id="btnAnchorBannerDismiss" style="font-size:11px;padding:2px 8px;">✕</button>` : ""}
      </span>`;
    bar.querySelector("#btnAnchorBannerQueue")?.addEventListener("click", () => { window.switchTab?.("console"); window.setConsoleView?.("queue"); });
    bar.querySelector("#btnAnchorBannerConsole")?.addEventListener("click", () => { window.switchTab?.("console"); window.setConsoleView?.("logs"); });
    bar.querySelector("#btnAnchorBannerDismiss")?.addEventListener("click", () => { liveAnchorJob = null; renderBanner(); });
  }

  async function runAnchorRender(args, label) {
    if (!window.state?.currentSlug || typeof window.triggerRender !== "function") return;
    liveAnchorJob = { state: "rendering", label: label || "Generating anchor image...", pct: 5 };
    renderBanner();
    renderAnchorStudio();
    await window.triggerRender(args);
  }

  async function uploadAnchorFile(file, filename, aid) {
    const slug = window.state?.currentSlug;
    if (!slug || !file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      try {
        const resp = await fetch("/api/anchors/upload", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slug, filename, data_url: ev.target.result }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.success) {
          window.showToast?.(`❌ Anchor upload failed: ${data.error || `HTTP ${resp.status}`}`);
          return;
        }
        const entry = (window.state.projectData?.anchors || []).find((x) => x.filename === filename || window.extractAnchorId?.(x.filename) === aid);
        if (entry && !entry.filename) entry.filename = data.filename;
        await window.refreshProjectAssets?.(true);
        window.showToast?.(`📁 Uploaded [${data.aid}] ${data.filename}`);
      } catch (err) {
        window.showToast?.(`❌ Upload failed: ${err.message}`);
      }
    };
    reader.readAsDataURL(file);
  }

  function renderAnchorScenePrefillSelect(sel, targetAnchor) {
    sel.innerHTML = `<option value="">✨ Prefill from Scene...</option>`;
    (window.state?.projectData?.scenes || []).forEach((sc, idx) => {
      const dur = (Number(sc.end || 0) - Number(sc.start || 0)).toFixed(1);
      const opt = new Option(`${sc.id} (${dur}s): ${String(sc.prompt || sc.prompt_m || "").slice(0, 42)}...`, String(idx));
      sel.add(opt);
    });
    sel.onchange = () => {
      const idx = parseInt(sel.value, 10);
      sel.value = "";
      const sc = window.state?.projectData?.scenes?.[idx];
      if (!sc || !targetAnchor) return;
      const derived = deriveAnchorPromptFromScene(sc);
      if (derived) targetAnchor.prompt = derived;
      if (sc.seed && !targetAnchor.seed) targetAnchor.seed = sc.seed;
      delete targetAnchor.external;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Prefill ${window.extractAnchorId?.(targetAnchor.filename)} from ${sc.id}`);
      renderAnchorStudio();
      window.showToast?.(`✨ Prefilled anchor prompt from ${sc.id}`);
    };
  }

  function getDefaultAnchorRes() {
    return window.state?.projectData?.models?.anchor_shared_settings?.resolution
      || window.state?.models?.anchor_shared_settings?.resolution
      || "1920x1080";
  }

  function populateAnchorModelSelect(sel, curVal) {
    const defId = String(window.state?.projectData?.models?.default_anchor_renderer || 3);
    const defSpec = window.state?.models?.anchor_renderers?.[defId] || window.state?.models?.fal_anchor_renderers?.[defId];
    const defLbl = defSpec ? `Default ([#${defId}] ${defSpec.name || defSpec.model_type})` : `Default (Model #${defId})`;
    sel.innerHTML = `<option value="">${window.escapeHtml ? window.escapeHtml(defLbl) : defLbl}</option>`;
    if (typeof window.appendGroupedModelOptions === "function") {
      window.appendGroupedModelOptions(sel, window.state?.models?.anchor_renderers, window.state?.models?.fal_anchor_renderers);
    }
    const sv = curVal !== undefined && curVal !== null ? String(curVal) : "";
    if (sv && !Array.from(sel.options).some((o) => o.value === sv)) sel.add(new Option(`Custom (${sv})`, sv));
    sel.value = sv;
  }

  function populateAnchorResSelect(sel, curVal, modelId = null) {
    sel.innerHTML = "";
    sel.add(new Option(`Default (${getDefaultAnchorRes()})`, ""));
    const spec = window.getAnchorRendererSpec ? window.getAnchorRendererSpec({ renderer: modelId }, {}) : {};
    const recRes = Array.isArray(spec?.resolutions) ? spec.resolutions : [];
    const seen = new Set([""]);
    const addOpt = (parent, [v, l]) => { if (!seen.has(v)) { seen.add(v); parent.appendChild(new Option(l, v)); } };
    if (recRes.length) {
      const ogRec = document.createElement("optgroup"); ogRec.label = "Model Native Resolutions";
      recRes.forEach((p) => addOpt(ogRec, p));
      const ogStd = document.createElement("optgroup"); ogStd.label = "Standard Studio Resolutions";
      ANCHOR_RES_OPTIONS.forEach((p) => addOpt(ogStd, p));
      sel.append(ogRec, ogStd);
    } else { ANCHOR_RES_OPTIONS.forEach((p) => addOpt(sel, p)); }
    const sv = String(curVal || "").trim();
    if (sv && !seen.has(sv)) sel.add(new Option(`${sv} (Custom)`, sv));
    sel.value = sv;
  }
  window.populateAnchorModelSelect = populateAnchorModelSelect;
  window.populateAnchorResSelect = populateAnchorResSelect;

  function renderAnchorStudio() {
    const container = document.getElementById("anchorCardsContainer");
    if (!container) return;

    const ae = document.activeElement;
    const savedFocus = (ae && container.contains(ae) && ae.dataset?.afield)
      ? {
          aid: ae.closest(".anchor-card")?.dataset?.aid || "",
          field: ae.dataset.afield,
          start: typeof ae.selectionStart === "number" ? ae.selectionStart : null,
          end: typeof ae.selectionEnd === "number" ? ae.selectionEnd : null,
        }
      : null;

    container.innerHTML = "";

    const rawAnchors = window.state?.projectData?.anchors || [];
    const entries = window.getMergedAnchorsList?.() || [];
    const charTags = getCharacterQuickTags();
    const esc = window.escapeHtml || ((s) => String(s || ""));
    const encSlug = encodeURIComponent(window.state?.currentSlug || "");

    const missingCount = entries.filter((e) => !e.isDiskOnly && !e.diskInfo && !(e.anchor?.external && !String(e.anchor?.prompt || "").trim())).length;
    const genMissingBtn = document.getElementById("btnGenMissingAnchors");
    if (genMissingBtn) {
      genMissingBtn.textContent = missingCount > 0 ? `⚡ Generate Missing (${missingCount})` : "⚡ Generate Missing";
    }
    const tbResSel = document.getElementById("selDefaultAnchorResolution");
    if (tbResSel) {
      if (!tbResSel.options.length) {
        ANCHOR_RES_OPTIONS.filter(([v]) => v).forEach(([v, l]) => tbResSel.add(new Option(l, v)));
      }
      tbResSel.value = getDefaultAnchorRes();
    }

    if (entries.length === 0) {
      container.innerHTML = `<div style="grid-column:1/-1;padding:32px;text-align:center;color:var(--text-muted);border:1px dashed var(--border-color);border-radius:8px;">No anchors yet — click <strong>+ New Anchor</strong> or pick a scene from <strong>✨ New from Scene</strong> to create and generate anchor frames.</div>`;
    }

    entries.forEach(({ anchor: a, aid, diskInfo, resolvedName, isDiskOnly }) => {
      const card = document.createElement("div");
      const isHilite = highlightedAnchorId && highlightedAnchorId.toUpperCase() === aid.toUpperCase();
      card.className = `anchor-card anchor-card-editor${isHilite ? " is-highlighted" : ""}`;
      card.dataset.aid = aid;

      const anchorMtime = diskInfo?.mtime || "";
      const safeName = esc(resolvedName || a.filename || aid);
      const safeAid = esc(aid);
      const hasFile = Boolean(diskInfo);
      const qMatches = (window.state?.renderQueue || []).filter((t) => t.stage === "anchor" && (t.anchor_id || t.scene_id || "").toUpperCase() === aid.toUpperCase());
      const qCnt = qMatches.filter((t) => t.status === "queued").length;
      const isRenderingThis = qMatches.some((t) => t.status === "rendering") || (liveAnchorJob?.state === "rendering" && (!liveAnchorJob.aid || liveAnchorJob.aid.toUpperCase() === aid.toUpperCase()));
      let statusTxt = hasFile ? "On Disk" : "Missing — Ready to Generate";
      let statusCss = hasFile ? "background:rgba(16,185,129,0.18);color:#6ee7b7;border:1px solid rgba(16,185,129,0.4);" : "background:rgba(245,158,11,0.16);color:#fcd34d;border:1px solid rgba(245,158,11,0.4);";
      if (isDiskOnly) statusTxt = "Disk Only";
      else if (isRenderingThis || qCnt > 0) {
        statusTxt = isRenderingThis ? (qCnt ? `⏳ Generating (+${qCnt} queued)` : "⏳ Generating...") : `⏳ Queued (${qCnt})`;
        statusCss = "background:rgba(56,189,248,0.2);color:#7dd3fc;border:1px solid rgba(56,189,248,0.45);";
      }

      card.innerHTML = `
        <div class="anchor-thumb">
          <div class="anchor-thumb-imgwrap" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;">
            <img src="/media/${encSlug}/anchor/${encodeURIComponent(resolvedName || aid)}${anchorMtime ? `?t=${anchorMtime}` : ""}" alt="${safeName}" loading="lazy">
          </div>
          <span class="anchor-status-pill" style="${statusCss}">${statusTxt}</span>
          <span class="anchor-drop-hint">Drop image file to replace</span>
        </div>
        <div class="anchor-body">
          <div class="anchor-card-toprow">
            <span class="anchor-id">[${safeAid}]</span>
            ${isDiskOnly
              ? `<div style="flex:1;font-size:12px;color:var(--text-main);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${safeName}</div>`
              : `<input type="text" class="anchor-filename-input" data-afield="filename" value="${esc(a.filename || resolvedName)}" title="Anchor filename in anchors directory">`
            }
            <div class="anchor-card-mini-btns">
              ${!isDiskOnly ? `<button type="button" class="btn-mini" data-aact="gallery" title="Open Takes Gallery for [${safeAid}]">🖼️</button><button type="button" class="btn-mini" data-aact="import" title="Import image file from disk">📁</button><button type="button" class="btn-mini" data-aact="duplicate" title="Duplicate anchor config">📋</button><button type="button" class="btn-mini" data-aact="randseed" title="Randomize Seed">🎲</button><button type="button" class="btn-mini" data-aact="delete" title="Delete anchor" style="color:#f87171;">🗑️</button>` : `<button type="button" class="btn-mini" data-aact="delete-disk" title="Delete image file from disk" style="color:#f87171;">🗑️</button>`}
            </div>
          </div>
          <div class="anchor-scene-usage"></div>
          <div class="anchor-card-body-slot"></div>
          <div class="anchor-takes-slot" data-aid="${safeAid}"></div>
          <div class="anchor-card-actions" style="display:flex;gap:6px;align-items:center;margin-top:auto;padding-top:4px;"></div>
        </div>`;

      const thumbWrap = card.querySelector(".anchor-thumb");
      const thumbImg = thumbWrap?.querySelector("img");
      if (thumbImg) {
        thumbImg.onerror = () => window.handleAnchorImgError?.(thumbImg, window.state.currentSlug, aid);
        thumbImg.style.cursor = "zoom-in";
        thumbImg.title = "Click to view fullscreen (or drag an image file here to replace)";
        thumbImg.addEventListener("click", (e) => {
          e.stopPropagation();
          const slot = window.state?.anchorTakes?.[aid.toUpperCase()];
          if (slot?.takes?.length && typeof window.openAnchorTakeLightbox === "function") {
            return window.openAnchorTakeLightbox(aid, Math.max(0, slot.takes.findIndex((t) => t.take_id === slot.active_take_id)));
          }
          window.open(thumbImg.currentSrc || thumbImg.getAttribute("src") || `/media/${encSlug}/anchor/${encodeURIComponent(resolvedName || aid)}`, "_blank", "noopener");
        });
      }

      const usedScenes = getAnchorSceneUsage(aid, a.filename || resolvedName);
      const usageEl = card.querySelector(".anchor-scene-usage");
      if (usageEl) {
        if (usedScenes.length === 0) {
          usageEl.innerHTML = `<span style="font-size:10px;color:var(--text-muted);">Unused in timeline</span>`;
        } else {
          const lbl = document.createElement("span");
          lbl.style.cssText = "font-size:10px;color:var(--text-muted);margin-right:2px;";
          lbl.textContent = "Used in:";
          usageEl.appendChild(lbl);
          usedScenes.forEach((u) => {
            const pill = document.createElement("button");
            pill.type = "button"; pill.className = "anchor-scene-pill";
            pill.textContent = u.role === "End" ? `${u.id} (End)` : u.id;
            pill.title = `Jump to Scene ${u.id} (${u.role} Anchor)`;
            pill.onclick = () => (window.focusSceneInTimeline || window.selectScene)?.(u.idx);
            usageEl.appendChild(pill);
          });
        }
      }

      const uploadName = a.filename || resolvedName || `${aid}.jpg`;
      if (thumbWrap) {
        thumbWrap.addEventListener("dragover", (e) => { e.preventDefault(); thumbWrap.classList.add("anchor-drop-active"); });
        thumbWrap.addEventListener("dragleave", () => thumbWrap.classList.remove("anchor-drop-active"));
        thumbWrap.addEventListener("drop", (e) => {
          e.preventDefault();
          thumbWrap.classList.remove("anchor-drop-active");
          const file = e.dataTransfer?.files?.[0];
          if (file) uploadAnchorFile(file, uploadName, aid);
        });
      }

      const bodySlot = card.querySelector(".anchor-card-body-slot");
      const actionsEl = card.querySelector(".anchor-card-actions");
      if (isDiskOnly) {
        const note = document.createElement("div");
        note.style.cssText = "font-size:11px;color:var(--text-muted);line-height:1.4;";
        note.textContent = "Found in anchors folder (not in project JSON yet). Register to edit its prompt & seed for regeneration.";
        bodySlot.appendChild(note);
        const regBtn = document.createElement("button");
        regBtn.type = "button";
        regBtn.className = "btn btn-primary";
        regBtn.style.flex = "1";
        regBtn.textContent = "+ Register & Edit";
        regBtn.onclick = () => {
          window.registerDiskAnchor?.(resolvedName);
          highlightedAnchorId = aid;
        };
        actionsEl.appendChild(regBtn);
        card.querySelector("[data-aact='delete-disk']")?.addEventListener("click", async () => {
          if (!confirm(`Delete disk image '${resolvedName}' from the anchors folder?`)) return;
          await fetch("/api/anchors/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slug: window.state.currentSlug, filename: resolvedName }),
          });
          await window.refreshProjectAssets?.(true);
        });
        container.appendChild(card);
        return;
      }

      const editorWrap = document.createElement("div");
      editorWrap.className = "anchor-editor-fields";
      editorWrap.innerHTML = `
        <div class="anchor-card-settings-grid">
          <div class="form-group"><label>Model</label><select data-afield="renderer"></select></div>
          <div class="form-group"><label>Backend</label><select data-afield="backend"><option value="">Auto</option><option value="local">🖥️ Local</option><option value="fal">☁️ fal.ai</option></select></div>
          <div class="form-group"><label>Resolution</label><select data-afield="resolution"></select></div>
          <div class="form-group"><label>Seed</label><input type="number" data-afield="seed" value="${a.seed ?? 1001}"></div>
        </div>
        <div class="anchor-model-caps" style="font-size:10px;color:#94a3b8;margin-top:3px;"></div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-top:6px;">
          <label style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;">Prompt</label>
          <select class="anchor-prefill-select" title="Prefill prompt from an existing scene"></select>
        </div>
        <textarea class="anchor-card-prompt" data-afield="prompt" rows="4" placeholder="Describe the shot/character frame. Type @character to insert cast likeness...">${esc(a.prompt || "")}</textarea>
        <div class="anchor-char-chips"></div>
        <div class="anchor-ref-summary">${esc(describeAnchorRefs(a))}</div>
        <div class="anchor-source-refs-slot"></div>
        <details class="anchor-adv-details">
          <summary>⚙ Negative Prompt &amp; Advanced${a.negative_prompt || a.external ? " ●" : ""}</summary>
          <div style="display:flex;flex-direction:column;gap:6px;margin-top:6px;">
            <input type="text" data-afield="negative_prompt" value="${esc(a.negative_prompt || "")}" placeholder="Negative prompt (optional)">
            <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-muted);">
              <input type="checkbox" data-afield="external" ${a.external ? "checked" : ""}> External manual image (skip auto-generation unless clicked)
            </label>
          </div>
        </details>`;
      bodySlot.appendChild(editorWrap);

      const modelSel = editorWrap.querySelector("[data-afield='renderer']");
      const backendSel = editorWrap.querySelector("[data-afield='backend']");
      const resSel = editorWrap.querySelector("[data-afield='resolution']");
      const seedInp = editorWrap.querySelector("[data-afield='seed']");
      const negInp = editorWrap.querySelector("[data-afield='negative_prompt']");
      const extChk = editorWrap.querySelector("[data-afield='external']");
      const promptTa = editorWrap.querySelector("[data-afield='prompt']");
      const refSummaryEl = editorWrap.querySelector(".anchor-ref-summary");
      const capsEl = editorWrap.querySelector(".anchor-model-caps");
      const chipsEl = editorWrap.querySelector(".anchor-char-chips");
      const fnInp = card.querySelector("[data-afield='filename']");

      populateAnchorModelSelect(modelSel, a.renderer ?? "");
      populateAnchorResSelect(resSel, a.resolution || "", a.renderer ?? "");
      const syncCardCaps = () => {
        const spec = window.getAnchorRendererSpec ? window.getAnchorRendererSpec({ renderer: a.renderer ?? "" }, {}) : {};
        if (capsEl) capsEl.textContent = window.describeAnchorModelCaps?.(spec) || "";
        if (negInp) negInp.placeholder = spec?.no_negative_prompt ? "Unused by this model (CFG=1.0)" : "Negative prompt (optional)";
      };
      syncCardCaps();
      const isFalModel = String(a.renderer || "").startsWith("fal_");
      backendSel.value = isFalModel ? "fal" : (a.backend || "");
      backendSel.disabled = isFalModel;

      renderAnchorScenePrefillSelect(editorWrap.querySelector(".anchor-prefill-select"), a);

      charTags.forEach((ct) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "anchor-char-chip";
        chip.textContent = ct.tag;
        chip.title = `${ct.name} (${ct.refCount} ref image${ct.refCount === 1 ? "" : "s"}) — click to insert`;
        chip.onclick = () => {
          const st = promptTa.selectionStart ?? promptTa.value.length;
          const en = promptTa.selectionEnd ?? st;
          const v = promptTa.value;
          const prefix = st > 0 && !/\s$/.test(v.slice(0, st)) ? " " : "";
          const suffix = en < v.length && !/^\s/.test(v.slice(en)) ? " " : "";
          promptTa.value = v.slice(0, st) + prefix + ct.tag + suffix + v.slice(en);
          promptTa.dispatchEvent(new Event("input", { bubbles: true }));
          promptTa.focus();
        };
        chipsEl.appendChild(chip);
      });

      const srcRefsSlot = editorWrap.querySelector(".anchor-source-refs-slot");
      const paintSrcRefs = () => {
        window.renderAnchorSourceRefsPanel?.(srcRefsSlot, a, aid, {
          promptInput: promptTa,
          onChange: () => { refSummaryEl.textContent = describeAnchorRefs(a); syncScenesViews(); },
        });
      };
      paintSrcRefs();

      window.bindCharacterMentionAutocomplete?.(promptTa);
      promptTa.addEventListener("focus", () => window.beginEditSession?.(`anchor:${aid}:prompt`, `Edit ${aid} Prompt`));
      promptTa.addEventListener("input", () => {
        window.beginEditSession?.(`anchor:${aid}:prompt`, `Edit ${aid} Prompt`);
        a.prompt = promptTa.value;
        if (a.external && promptTa.value.trim()) { delete a.external; extChk.checked = false; }
        refSummaryEl.textContent = describeAnchorRefs(a);
        if (!Array.isArray(a.image_refs)) paintSrcRefs();
        window.markProjectDirty?.();
      });
      promptTa.addEventListener("blur", () => window.commitEditSession?.());

      negInp.addEventListener("focus", () => window.beginEditSession?.(`anchor:${aid}:neg`, `Edit ${aid} Negative Prompt`));
      negInp.addEventListener("input", () => {
        window.beginEditSession?.(`anchor:${aid}:neg`, `Edit ${aid} Negative Prompt`);
        if (negInp.value.trim()) a.negative_prompt = negInp.value; else delete a.negative_prompt;
        window.markProjectDirty?.();
      });
      negInp.addEventListener("blur", () => window.commitEditSession?.());

      seedInp.addEventListener("change", () => {
        a.seed = parseInt(seedInp.value, 10) || 1001;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Change ${aid} Seed`);
      });
      modelSel.addEventListener("change", () => {
        const v = modelSel.value;
        a.renderer = v ? (/^\d+$/.test(v) ? parseInt(v, 10) : v) : null;
        const isFal = v.startsWith("fal_");
        backendSel.disabled = isFal;
        if (isFal) backendSel.value = "fal";
        populateAnchorResSelect(resSel, a.resolution || "", a.renderer ?? "");
        syncCardCaps();
        refSummaryEl.textContent = describeAnchorRefs(a);
        paintSrcRefs();
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Change ${aid} Generator Model`);
      });
      backendSel.addEventListener("change", () => {
        if (backendSel.value) a.backend = backendSel.value; else delete a.backend;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Change ${aid} Backend`);
      });
      resSel.addEventListener("change", () => {
        if (resSel.value) a.resolution = resSel.value; else delete a.resolution;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Change ${aid} Resolution`);
      });
      extChk.addEventListener("change", () => {
        if (extChk.checked) a.external = true; else delete a.external;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Toggle ${aid} External`);
      });
      fnInp?.addEventListener("change", () => {
        const oldName = a.filename;
        const newName = sanitizeAnchorFilename(fnInp.value, aid);
        fnInp.value = newName;
        if (newName === oldName) return;
        a.filename = newName;
        (window.state.projectData?.scenes || []).forEach((s) => {
          if (s.anchor === oldName) s.anchor = newName;
          if (s.anchor_end === oldName) s.anchor_end = newName;
        });
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Rename Anchor ${oldName} -> ${newName}`);
        syncScenesViews();
        renderAnchorStudio();
      });

      card.querySelector("[data-aact='gallery']")?.addEventListener("click", () => {
        window.openAnchorTakesGallery?.(aid);
      });
      card.querySelector("[data-aact='duplicate']")?.addEventListener("click", () => {
        duplicateAnchor(a, aid);
      });
      card.querySelector("[data-aact='randseed']")?.addEventListener("click", () => {
        const seed = Math.floor(Math.random() * 90000) + 1000;
        a.seed = seed;
        seedInp.value = seed;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Randomize ${aid} Seed`);
      });
      card.querySelector("[data-aact='import']")?.addEventListener("click", () => {
        const inp = document.getElementById("anchorFileUploadInput");
        if (!inp) return;
        activeUploadTarget = { filename: uploadName, aid };
        inp.value = "";
        inp.click();
      });
      card.querySelector("[data-aact='delete']")?.addEventListener("click", async () => {
        const usedMsg = usedScenes.length ? ` (currently used by ${usedScenes.map((u) => u.id).join(", ")})` : "";
        if (!confirm(`Remove Anchor [${aid}] (${uploadName})${usedMsg} from project config?`)) return;
        const idx = rawAnchors.indexOf(a);
        if (idx >= 0) rawAnchors.splice(idx, 1);
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Delete Anchor ${aid}`);
        if (hasFile && confirm(`Also delete '${resolvedName}' image file from disk?`)) {
          await fetch("/api/anchors/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slug: window.state.currentSlug, filename: resolvedName }),
          });
          await window.refreshProjectAssets?.(true);
        } else {
          renderAnchorStudio();
          syncScenesViews();
        }
      });

      const countSel = document.createElement("input");
      countSel.type = "number"; countSel.min = "1"; countSel.max = "99"; countSel.step = "1";
      countSel.className = "anchor-gen-count";
      countSel.dataset.afield = "gen_count";
      countSel.title = "How many image takes to generate for this prompt (1-99)";
      countSel.style.cssText = "width:58px;font-size:11px;padding:5px 4px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:5px;";
      countSel.value = String(a.gen_count || 1);
      countSel.oninput = countSel.onchange = () => {
        const n = Math.max(1, Math.min(99, parseInt(countSel.value, 10) || 1));
        if (countSel.value !== "" && String(n) !== countSel.value) countSel.value = String(n);
        if (n > 1) a.gen_count = n; else delete a.gen_count;
        genBtn.textContent = `${hasFile ? "⚡ Regenerate" : "⚡ Generate"}${n > 1 ? ` (${n} Takes)` : " Image"}`;
        window.markProjectDirty?.();
      };

      const genBtn = document.createElement("button");
      genBtn.type = "button";
      genBtn.className = "btn btn-primary";
      genBtn.style.flex = "1";
      genBtn.dataset.action = "generate-anchor";
      const curCnt = parseInt(countSel.value, 10) || 1;
      genBtn.textContent = `${hasFile ? "⚡ Regenerate" : "⚡ Generate"}${curCnt > 1 ? ` (${curCnt} Takes)` : " Image"}`;
      genBtn.onclick = () => {
        if (!String(a.prompt || "").trim()) {
          window.showToast?.(`Write a prompt for [${aid}] (or pick a scene from "Prefill from Scene") first.`);
          promptTa.focus();
          return;
        }
        delete a.external;
        extChk.checked = false;
        window.renderSingleAnchor(aid, parseInt(countSel.value, 10) || 1);
      };
      actionsEl.append(countSel, genBtn);
      container.appendChild(card);

      if (isHilite) {
        setTimeout(() => { card.scrollIntoView({ behavior: "smooth", block: "nearest" }); promptTa.focus(); }, 60);
        highlightedAnchorId = null;
      }
    });
    if (savedFocus && savedFocus.aid && savedFocus.field) {
      const targetEl = container.querySelector(`.anchor-card[data-aid="${savedFocus.aid}"] [data-afield="${savedFocus.field}"]`);
      if (targetEl) {
        targetEl.focus();
        if (savedFocus.start !== null && typeof targetEl.setSelectionRange === "function") {
          try { targetEl.setSelectionRange(savedFocus.start, savedFocus.end ?? savedFocus.start); } catch (e) {}
        }
      }
    }
    renderBanner();
    window.renderAllAnchorTakeStrips?.();
  }

  function createNewAnchor(opts = {}) {
    if (!window.state?.projectData) return null;
    if (!Array.isArray(window.state.projectData.anchors)) window.state.projectData.anchors = [];
    const aid = nextAnchorId();
    const safeTag = String(opts.label || "Scene").replace(/[^a-zA-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || "Scene";
    const filename = sanitizeAnchorFilename(`${aid}_${safeTag}.jpg`, aid);
    const entry = {
      filename,
      renderer: opts.renderer ?? null,
      seed: opts.seed || (Math.floor(Math.random() * 90000) + 1000),
      prompt: String(opts.prompt || "").trim(),
    };
    if (opts.backend) entry.backend = opts.backend;
    if (opts.resolution) entry.resolution = opts.resolution;
    if (opts.negative_prompt) entry.negative_prompt = opts.negative_prompt;
    if (Array.isArray(opts.image_refs)) entry.image_refs = [...opts.image_refs];
    window.state.projectData.anchors.push(entry);
    highlightedAnchorId = aid;
    window.markProjectDirty?.();
    window.recordHistorySnapshot?.(`Add Anchor ${aid}`);
    renderAnchorStudio();
    syncScenesViews();
    window.showToast?.(`Created Anchor [${aid}] (${filename}) — edit prompt & click ⚡ Generate Image.`);
    return entry;
  }

  function duplicateAnchor(srcAnchorOrId, fallbackAid = "") {
    const rawAnchors = window.state?.projectData?.anchors || [];
    const src = typeof srcAnchorOrId === "object" && srcAnchorOrId
      ? srcAnchorOrId
      : rawAnchors.find((x) => x.filename === srcAnchorOrId || (window.extractAnchorId?.(x.filename) || "").toUpperCase() === String(srcAnchorOrId || "").toUpperCase());
    if (!src) return null;
    const baseStem = String(src.filename || fallbackAid || "Anchor").replace(/^A\d+[a-z]?[-_\s]*/i, "").replace(/\.[a-z0-9]+$/i, "") || "Variant";
    return createNewAnchor({
      label: `${baseStem}_Copy`,
      prompt: src.prompt || "",
      renderer: src.renderer ?? null,
      backend: src.backend,
      resolution: src.resolution,
      negative_prompt: src.negative_prompt,
      image_refs: Array.isArray(src.image_refs) ? [...src.image_refs] : undefined,
      seed: (Number(src.seed) || 1000) + 1,
    });
  }

  function focusAnchorInStudio(identifier) {
    const aid = window.extractAnchorId?.(identifier || "") || String(identifier || "");
    if (aid) highlightedAnchorId = aid;
    window.switchTab?.("anchors");
    renderAnchorStudio();
  }

  function createAnchorFromScene(sceneIdx) {
    const sc = window.state?.projectData?.scenes?.[sceneIdx];
    if (!sc) return null;
    if (sc.locked) {
      window.showToast?.(`🔒 Scene ${sc.id} is locked — unlock it first to attach a new anchor.`);
      return null;
    }
    const created = createNewAnchor({
      label: sc.id || "Scene",
      prompt: deriveAnchorPromptFromScene(sc),
      seed: Number(sc.seed || sc.seed_m) || undefined,
    });
    if (!created) return null;
    sc.anchor = created.filename;
    window.markProjectDirty?.();
    syncScenesViews();
    window.updateSceneAnchorPreview?.(sc.anchor);
    window.redrawTimeline?.();
    window.switchTab?.("anchors");
    return created;
  }

  function initAnchorStudioControls() {
    document.getElementById("selDefaultAnchorResolution")?.addEventListener("change", (e) => {
      const val = e.target.value || "1664x960";
      const m = (window.state.projectData.models = window.state.projectData.models || {});
      m.anchor_shared_settings = m.anchor_shared_settings || {};
      m.anchor_shared_settings.resolution = val;
      const cfgSel = document.getElementById("cfgDefaultAnchorResolution");
      if (cfgSel) cfgSel.value = val;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Change Default Anchor Resolution to ${val}`);
      renderAnchorStudio();
      window.showToast?.(`Default Anchor Resolution set to ${val}`);
    });
    document.getElementById("btnAddAnchor")?.addEventListener("click", () => {
      createNewAnchor({ label: "Scene" });
    });
    document.getElementById("btnScanAnchors")?.addEventListener("click", async () => {
      await window.refreshProjectAssets?.(true);
      const diskOnly = (window.getMergedAnchorsList?.() || []).filter((x) => x.isDiskOnly).map((x) => x.aid);
      window.showToast?.(diskOnly.length ? `Discovered disk anchor(s): ${diskOnly.join(", ")}` : "Scanned anchors folder.");
    });
    document.getElementById("btnGenMissingAnchors")?.addEventListener("click", () => {
      runAnchorRender(["--anchors-only"], "Generating missing anchors...");
    });
    document.getElementById("btnGenAllAnchors")?.addEventListener("click", () => {
      runAnchorRender(["--anchors-only", "all-anchors"], "Regenerating all configured anchors...");
    });
    document.getElementById("anchorFileUploadInput")?.addEventListener("change", (e) => {
      const f = e.target.files?.[0];
      if (f && activeUploadTarget) uploadAnchorFile(f, activeUploadTarget.filename, activeUploadTarget.aid);
      activeUploadTarget = null;
    });

    const scenePicker = document.getElementById("selNewAnchorFromScene");
    const syncScenePicker = () => {
      if (!scenePicker) return;
      scenePicker.innerHTML = `<option value="">✨ New Anchor from Scene...</option>`;
      (window.state?.projectData?.scenes || []).forEach((sc, i) => {
        scenePicker.add(new Option(`${sc.id} (${Number(sc.start || 0).toFixed(1)}s–${Number(sc.end || 0).toFixed(1)}s)`, String(i)));
      });
    };
    scenePicker?.addEventListener("focus", syncScenePicker);
    scenePicker?.addEventListener("change", () => {
      const idx = parseInt(scenePicker.value, 10);
      scenePicker.value = "";
      if (!Number.isNaN(idx)) createNewAnchor({
        label: window.state.projectData.scenes[idx]?.id || "Scene",
        prompt: deriveAnchorPromptFromScene(window.state.projectData.scenes[idx]),
        seed: Number(window.state.projectData.scenes[idx]?.seed || window.state.projectData.scenes[idx]?.seed_m) || undefined,
      });
    });

    document.getElementById("btnEditSceneAnchor")?.addEventListener("click", () => {
      const sc = window.state?.projectData?.scenes?.[window.state?.selectedSceneIdx];
      focusAnchorInStudio(sc?.anchor || "");
    });
    document.getElementById("btnNewAnchorFromCurrentScene")?.addEventListener("click", () => {
      if (window.state?.selectedSceneIdx !== null && window.state?.selectedSceneIdx !== undefined) {
        createAnchorFromScene(window.state.selectedSceneIdx);
      }
    });
    document.querySelector?.('.tab-btn[data-tab="anchors"]')?.addEventListener("click", () => {
      syncScenePicker();
      renderAnchorStudio();
    });

    if (typeof window.loadProjectCharacters === "function" && !window.loadProjectCharacters._anchorHooked) {
      const origLoadChars = window.loadProjectCharacters;
      const wrappedLoadChars = async function () {
        const res = await origLoadChars.apply(this, arguments);
        renderAnchorStudio();
        return res;
      };
      wrappedLoadChars._anchorHooked = true;
      window.loadProjectCharacters = wrappedLoadChars;
    }

    if (typeof window.handleQueueSSEEvent === "function" && !window.handleQueueSSEEvent._anchorHooked) {
      const origSse = window.handleQueueSSEEvent;
      const wrappedSse = function (line, evSlug) {
        origSse.apply(this, arguments);
        const s = String(line || "");
        const jIdx = s.indexOf("{");
        if (jIdx < 0) return;
        try {
          const obj = JSON.parse(s.slice(jIdx));
          const aid = window.extractAnchorId?.(obj.filename || "") || "";
          if (s.startsWith("[QUEUE_ACTIVE]") && /^A\d+/i.test(aid)) {
            liveAnchorJob = { state: "rendering", aid, label: `Generating ${obj.filename}`, pct: 5 };
            renderBanner();
          } else if (s.startsWith("[QUEUE_PROGRESS]") && /^A\d+/i.test(aid)) {
            liveAnchorJob = { state: "rendering", aid, label: `Generating ${obj.filename}: ${obj.status_text || obj.phase || "Working"}`, pct: obj.pct ?? 50 };
            renderBanner();
          } else if (s.startsWith("[QUEUE_DONE]") && /^A\d+/i.test(aid)) {
            liveAnchorJob = { state: "completed", aid, label: `Generated ${obj.filename} (${obj.elapsed ?? ""}s)`, pct: 100 };
            renderBanner();
            Promise.resolve(window.refreshProjectAssets?.(true)).then(() => renderAnchorStudio());
          } else if (s.startsWith("[QUEUE_FAIL]") && /^A\d+/i.test(aid)) {
            liveAnchorJob = { state: "failed", aid, label: `Failed ${obj.filename}: ${(obj.error || [])[0] || "Error"}` };
            renderBanner();
          }
        } catch (e) {}
      };
      wrappedSse._anchorHooked = true;
      window.handleQueueSSEEvent = wrappedSse;
    }

    window.addEventListener("projectLoaded", () => { syncScenePicker(); renderAnchorStudio(); });
  }

  window.updateAnchorStudioUI = renderAnchorStudio;
  window.deriveAnchorPromptFromScene = deriveAnchorPromptFromScene;
  window.createNewAnchor = createNewAnchor;
  window.duplicateAnchor = duplicateAnchor;
  window.focusAnchorInStudio = focusAnchorInStudio;
  window.createAnchorFromScene = createAnchorFromScene;
  window.renderSingleAnchor = function (identifier, count = 1) {
    const aid = window.extractAnchorId?.(identifier) || identifier;
    const cnt = Math.max(1, Math.min(99, parseInt(count, 10) || 1));
    const tok = cnt > 1 ? `${aid}:x${cnt}` : aid;
    return runAnchorRender(["--anchors-only", tok], cnt > 1 ? `Generating ${cnt} Takes for Anchor [${aid}]...` : `Generating Anchor [${aid}]...`);
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initAnchorStudioControls);
  else initAnchorStudioControls();
})();
