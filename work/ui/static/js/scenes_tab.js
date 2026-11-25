/**
 * Music Video Generation Studio - Vertical Scenes Tab (Multi-Scene Visual Editor)
 * Renders all scenes vertically with Starting Frame thumbnails, inline editable render settings,
 * expandable prompts/negatives/markers, and real-time bidirectional sync with Scene Inspector.
 */
(function () {
  "use strict";

  const CAM_PRESETS = [
    ["🔒 Tripod", "Locked-off stationary tripod shot, fixed horizon line, zero camera pan, tilt, or zoom."],
    ["🔍 Push-In", "Slow, steady cinematic dolly push-in toward the subject."],
    ["🔄 Orbit", "Subtle, smooth orbital camera drift around the performance."],
    ["↔️ Wide", "A seamless, continuous wide-angle master shot across the studio floor."],
  ];
  const NEG_PRESETS = [
    ["+ Lock Cam", "camera pan, camera movement, zoom, tilt, camera drift, handheld shake, orbiting"],
    ["+ No Singing", "singing, open mouth, talking, lip movement, vocalizing, teeth showing"],
    ["+ No Crowd", "extra people, crowd, background bystander, duplicate person, extra limbs"],
    ["+ Clean", "face warping, morphing, flickering, blurry eyes, deformed hands"],
  ];

  let activeFilter = "all";
  let searchQuery = "";
  let expandAllPrompts = false;
  let expandAllFilters = false;
  let staleList = false;
  let isSyncingFromInspector = false;
  let openPopover = null;
  const expandedPromptIds = new Set();
  const expandedFilterIds = new Set();
  const expandedEndAnchorIds = new Set();
  const expandedTakesIds = new Set();
  const sceneTakesCache = new Map();
  const sceneTakesSelected = new Map();
  window.invalidateSceneTakesCache = (sid) => { if (sid) sceneTakesCache.delete(sid); else sceneTakesCache.clear(); };
  window.addEventListener?.("projectLoaded", () => sceneTakesCache.clear());

  const getTabRoot = () => document.getElementById("tab-scenes");
  const isTabActive = () => Boolean(getTabRoot()?.classList.contains("active"));
  const isEditingInTab = () => {
    const ae = document.activeElement, root = getTabRoot();
    return Boolean(ae && root && root.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/i.test(ae.tagName));
  };

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => {
      if (v === undefined || v === null) return;
      if (k === "style") node.style.cssText = v;
      else if (k === "className") node.className = v;
      else if (k === "dataset") Object.entries(v).forEach(([dk, dv]) => { node.dataset[dk] = dv; });
      else node[k] = v;
    });
    children.forEach((c) => {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function mkBtn(textContent, title, onclick, style = "") {
    return el("button", { type: "button", className: "btn-mini", textContent, title, onclick, style });
  }

  function bindLiveText(inp, key, label, applyFn, afterFn) {
    inp.onfocus = () => window.beginEditSession?.(`scenesTab:${key}`, label);
    inp.oninput = () => {
      window.beginEditSession?.(`scenesTab:${key}`, label);
      applyFn(inp.value);
      afterFn();
    };
    inp.onblur = () => window.commitEditSession?.();
  }

  function closeAnchorPopover() {
    if (openPopover) { openPopover.remove(); openPopover = null; }
  }
  document.addEventListener("click", (e) => { if (openPopover && !openPopover.contains(e.target)) closeAnchorPopover(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAnchorPopover(); });

  function isSceneUnsavedDirty(sc) {
    if (!state.isDirty || !sc?.id) return false;
    const raw = window._historyDebug?.getLastSavedJson?.();
    if (!raw) return Boolean(state.isDirty);
    try {
      const savedSc = (JSON.parse(raw).scenes || []).find((s) => s.id === sc.id);
      if (!savedSc) return true;
      const clean = (s) => { const c = { ...s }; delete c.locked; return JSON.stringify(c); };
      return clean(sc) !== clean(savedSc);
    } catch (e) { return Boolean(state.isDirty); }
  }

  function getSceneQueueMeta(sc) {
    if (sc?._freshSceneTakes) return { qItems: [], item: null, isRendering: false, isQueued: false, clipKey: "", statusText: "New (Unsaved)", statusBg: "rgba(245,158,11,0.2)", statusColor: "#fcd34d", needsRevert: false };
    const sid = String(sc.id || "").toUpperCase();
    const qItems = (state.renderQueue || []).filter((t) => String(t.scene_id || "").toUpperCase() === sid);
    const ren = qItems.find((t) => t.status === "rendering");
    const que = qItems.find((t) => t.status === "queued" || t.needs_render);
    const item = ren || que || qItems[0];
    const clips = state.assets?.clips || {};
    const canon = clips[sc.id] || clips[sc.id?.toUpperCase()] || clips[sc.id?.toLowerCase()];
    const clipKey = canon?.filename || Object.keys(clips).find((k) => k.startsWith(`${sc.id}_`) || k === `${sc.id}.mp4`) || "";
    let statusText = "Unqueued", statusBg = "#334155", statusColor = "#cbd5e1";
    if (ren) { statusText = "Rendering..."; statusBg = "rgba(56,189,248,0.22)"; statusColor = "#7dd3fc"; }
    else if (que) { statusText = `Queued (${item?.reason || "Changed"})`; statusBg = "rgba(245,158,11,0.2)"; statusColor = "#fcd34d"; }
    else if (item) { statusText = item.status === "skipped" ? "Skipped" : "Up-to-Date"; statusBg = "rgba(16,185,129,0.2)"; statusColor = "#6ee7b7"; }
    const needsRevert = Boolean(clipKey && ((que && String(item?.reason || "").toUpperCase() !== "FORCED") || isSceneUnsavedDirty(sc)));
    return { qItems, item, isRendering: Boolean(ren), isQueued: Boolean(que), clipKey, statusText, statusBg, statusColor, needsRevert };
  }

  function buildMetaSummaryHtml(sc) {
    const dur = Math.max(0, Number(sc.end || 0) - Number(sc.start || 0)).toFixed(2);
    const est = window.estimateSceneRenderInfo?.(sc);
    if (!est) {
      const fps = state.projectData?.project?.fps || 24;
      return `${dur}s (${Math.round(dur * fps)}f)`;
    }
    const padTot = Number((est.head_pad + est.tail_pad).toFixed(2));
    const padNote = padTot > 0 ? ` (+${padTot}s pad)` : "";
    return `${est.effective_seconds.toFixed(2)}s (${est.quantized_frames}f)${padNote}`;
  }

  function applyFieldSideEffects(idx, sc, { structural = false, timingChanged = false } = {}) {
    markProjectDirty();
    redrawTimeline();
    updateCardHeaderAndBadges(idx, sc);
    if (timingChanged) {
      if (idx > 0) updateNeighborTimingDom(idx - 1);
      if (idx < (state.projectData?.scenes?.length || 0) - 1) updateNeighborTimingDom(idx + 1);
    }
    if (structural) replaceCardInPlace(idx, sc);
    if (idx === state.selectedSceneIdx) syncInspectorFromScenesTab(idx, sc);
    window.refreshTakeDiffUI?.();
  }

  function updateCardHeaderAndBadges(idx, sc) {
    const card = getTabRoot()?.querySelector(`.scene-card[data-scene-idx="${idx}"]`);
    if (!card) return;
    const metaEl = card.querySelector(".scene-time-meta");
    if (metaEl) metaEl.innerHTML = buildMetaSummaryHtml(sc);
    const cat = window.getSceneOptionCatalog?.(sc);
    if (!cat) return;
    const toggleBadge = (sel, vis, text) => {
      const b = card.querySelector(sel);
      if (!b) return;
      if (text !== undefined) b.textContent = text;
      b.style.display = vis ? "inline" : "none";
    };
    toggleBadge("[data-badge='res']", Boolean(sc.resolution));
    toggleBadge("[data-badge='model']", Boolean(sc.renderer));
    const vrId = String(sc.renderer ?? state.projectData?.models?.default_video_renderer ?? "1");
    const isDedFal = Boolean(cat.fModels?.[vrId]), isDedMm = Boolean(cat.mmModels?.[vrId]);
    const effB = window.resolveEffectiveSceneBackend?.(sc) || sc.backend || "local";
    const bLbl = effB === "fal" ? "☁️ fal.ai" : (effB === "minimax" ? "🎥 MiniMax" : "🖥️ Local");
    toggleBadge("[data-badge='backend']", Boolean(sc.backend || isDedFal || isDedMm), bLbl);
    const bEl = card.querySelector("[data-badge='backend']");
    if (bEl) bEl.style.color = effB === "fal" ? "#38bdf8" : (effB === "minimax" ? "#c084fc" : "#cbd5e1");
    const bSel = card.querySelector("[data-role='scene-backend']");
    if (bSel) {
      bSel.value = isDedFal ? "fal" : (isDedMm ? "minimax" : (sc.backend || ""));
      bSel.disabled = Boolean(sc.locked) || isDedFal || isDedMm;
    }
    toggleBadge("[data-badge='up']", cat.skipOom || cat.skipTarget || cat.curUp, cat.skipOom ? "Skip≥1536" : (cat.skipTarget ? "Skip==Tgt" : "Custom"));
    toggleBadge("[data-badge='flt']", cat.hasAnyFilter || cat.isCustomFilter, cat.effRef || (cat.isCustomFilter ? "Custom" : "Default"));
    const advB = card.querySelector("[data-role='adv-summary-badges']");
    if (advB && window.buildAdvSummaryBadgesHtml) advB.innerHTML = window.buildAdvSummaryBadgesHtml(sc, cat);
    const padReset = card.querySelector("[data-action='reset-pad']");
    if (padReset) padReset.style.display = (sc.render_pad_start !== undefined || sc.render_pad_end !== undefined) ? "inline-flex" : "none";
    const rBtn = card.querySelector("[data-action='revert']");
    if (rBtn) rBtn.style.display = getSceneQueueMeta(sc).needsRevert ? "inline-flex" : "none";
  }

  function updateNeighborTimingDom(nIdx) {
    const nSc = state.projectData?.scenes?.[nIdx];
    const nCard = getTabRoot()?.querySelector(`.scene-card[data-scene-idx="${nIdx}"]`);
    if (!nSc || !nCard) return;
    const sEl = nCard.querySelector("[data-field='start']"), eEl = nCard.querySelector("[data-field='end']");
    if (sEl && document.activeElement !== sEl) sEl.value = nSc.start;
    if (eEl && document.activeElement !== eEl) eEl.value = nSc.end;
    updateCardHeaderAndBadges(nIdx, nSc);
  }

  function syncInspectorFromScenesTab(idx, sc) {
    if (isSyncingFromInspector || idx !== state.selectedSceneIdx) return;
    const insp = document.getElementById("sceneInspector");
    if (insp && insp.contains(document.activeElement)) return;
    const idEl = (id) => document.getElementById(id);
    if (idEl("sceneMode")) idEl("sceneMode").value = canonicalizeSceneMode(sc.mode) || "solo";
    if (idEl("sceneAnchor")) sc.anchor = syncAnchorDropdown(idEl("sceneAnchor"), sc.anchor);
    updateSceneAnchorPreview?.(sc.anchor);
    if (idEl("sceneAnchorEnd")) syncAnchorDropdown(idEl("sceneAnchorEnd"), sc.anchor_end || "");
    updateSceneEndAnchorPreview?.(sc.anchor_end || "");
    if (idEl("btnClearEndAnchor")) idEl("btnClearEndAnchor").style.display = sc.anchor_end ? "inline" : "none";
    populateSceneVideoRenderer?.(sc);
    window.populateSceneResolutionAndUpsampling?.(sc);
    if (idEl("sceneStart")) idEl("sceneStart").value = sc.start;
    if (idEl("sceneEnd")) idEl("sceneEnd").value = sc.end;
    if (idEl("scenePadStart")) idEl("scenePadStart").value = sc.render_pad_start ?? "";
    if (idEl("scenePadEnd")) idEl("scenePadEnd").value = sc.render_pad_end ?? "";
    if (idEl("btnResetScenePadding")) idEl("btnResetScenePadding").style.display = (sc.render_pad_start !== undefined || sc.render_pad_end !== undefined) ? "inline" : "none";
    updateSceneInspectorInputs?.(sc);
    updateSceneMarkersBar?.(sc);
    updateInspectorSceneHeader?.(sc);
    window.refreshTakeDiffUI?.();
  }

  function populateOptions(sel, opts, curVal, customSuffix = "custom") {
    sel.innerHTML = "";
    opts.forEach(([val, lbl, dataset]) => {
      const o = new Option(lbl, val);
      if (dataset) Object.entries(dataset).forEach(([k, v]) => { o.dataset[k] = v; });
      sel.add(o);
    });
    const strVal = curVal !== undefined && curVal !== null ? String(curVal) : "";
    if (strVal && !Array.from(sel.options).some((o) => o.value === strVal)) sel.add(new Option(`${strVal} (${customSuffix})`, strVal));
    sel.value = strVal;
  }

  function openAnchorPopover(triggerEl, sc, idx, isEndAnchor) {
    if (sc?.locked) { window.showToast?.(`🔒 Scene ${sc.id} is locked — unlock it first`); return; }
    closeAnchorPopover();
    const list = typeof getMergedAnchorsList === "function" ? getMergedAnchorsList() : [];
    const rect = triggerEl.getBoundingClientRect();
    const pop = el("div", {
      className: "scenes-anchor-popover",
      style: `left:${Math.min(window.innerWidth - 460, Math.max(8, rect.left))}px;top:${Math.min(window.innerHeight - 360, Math.max(8, rect.bottom + 4))}px;`,
    });
    if (isEndAnchor) {
      pop.appendChild(el("div", {
        className: "scenes-anchor-pop-item",
        style: "grid-column: 1 / -1; text-align:center; padding:6px;",
        textContent: "🚫 (None / Single Anchor)",
        onclick: () => {
          delete sc.anchor_end;
          window.recordHistorySnapshot?.(`Clear ${sc.id} End Anchor`);
          closeAnchorPopover();
          applyFieldSideEffects(idx, sc, { structural: true });
        },
      }));
    }
    const cur = isEndAnchor ? (sc.anchor_end || "") : (sc.anchor || ""), curAid = extractAnchorId(cur);
    const actBar = el("div", { style: "grid-column:1/-1;display:flex;gap:6px;padding-bottom:4px;border-bottom:1px solid #334155;" });
    if (!isEndAnchor) actBar.appendChild(mkBtn(`✨ New Anchor from ${sc.id}`, "Create new anchor prefilled from this scene", () => { closeAnchorPopover(); window.createAnchorFromScene?.(idx); }, "flex:1;padding:4px 8px;color:#38bdf8;border-color:rgba(56,189,248,0.45);"));
    if (curAid) actBar.appendChild(mkBtn(`🎨 Edit/Gen [${curAid}]`, "Open this anchor in Anchor Studio", () => { closeAnchorPopover(); window.focusAnchorInStudio?.(curAid); }, "flex:1;padding:4px 8px;"));
    if (actBar.childNodes.length) pop.appendChild(actBar);
    list.forEach(({ aid, diskInfo, resolvedName, valueName }) => {
      const active = valueName === cur || (aid && aid === curAid);
      const thumbUrl = `/media/${state.currentSlug}/anchor/${encodeURIComponent(resolvedName)}${diskInfo?.mtime ? `?t=${diskInfo.mtime}` : ""}`;
      const item = el("div", {
        className: `scenes-anchor-pop-item${active ? " active" : ""}`,
        innerHTML: `<img src="${thumbUrl}" alt="${escapeHtml(aid)}" onerror="this.style.display='none'"><div style="font-weight:700;color:#fbbf24;">[${escapeHtml(aid)}]${diskInfo ? "" : " <span style='font-size:9px;color:#fcd34d;'>(Pending)</span>"}</div><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(resolvedName)}</div>`,
        onclick: () => {
          closeAnchorPopover();
          const apply = () => {
            if (isEndAnchor) sc.anchor_end = valueName; else sc.anchor = valueName;
            window.recordHistorySnapshot?.(`Change ${sc.id} ${isEndAnchor ? "End " : ""}Anchor`);
            applyFieldSideEffects(idx, sc, { structural: true });
          };
          if (isEndAnchor && window.guardSceneComboChange) {
            window.guardSceneComboChange(sc, { anchor_end: valueName }, { onApply: apply });
          } else {
            apply();
          }
        },
      });
      pop.appendChild(item);
    });
    document.body.appendChild(pop);
    openPopover = pop;
  }

  function makeCtrl(labelHtml, widthStyle, inputEl) {
    return el("div", { className: "scene-ctrl", style: widthStyle || "" }, el("label", { innerHTML: labelHtml }), inputEl);
  }

  function mkNum(sc, idx, val, step, min, max, ph, width, fieldAttr, onInp) {
    const inp = el("input", { type: "number", step, value: val ?? "", placeholder: ph || "", style: `width:${width};`, dataset: fieldAttr ? { field: fieldAttr } : undefined });
    if (min !== null) inp.min = min;
    if (max !== null) inp.max = max;
    bindLiveText(inp, `${sc.id}:${fieldAttr || ph}`, `Edit ${sc.id}`, (v) => onInp(v, inp), () => {});
    return inp;
  }

  function buildPromptDrawer(sc, idx) {
    const drawer = el("div", { className: "scene-prompt-drawer" });
    const isTwo = isTwoPassScene(sc), isMod = sc.prompt_mode === "components";

    const modeRow = el("div", {
      style: "display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;",
      innerHTML: `<div style="display:flex;gap:4px;">`
        + `<button type="button" class="btn-mini" data-pmode="raw" style="padding:2px 8px;font-size:10px;background:${!isMod ? "#2563eb" : "#1e293b"};color:#fff;">📝 Raw Prompt</button>`
        + `<button type="button" class="btn-mini" data-pmode="components" style="padding:2px 8px;font-size:10px;background:${isMod ? "#2563eb" : "#1e293b"};color:#fff;">🧩 Modular Components</button>`
        + `</div><span style="font-size:10px;color:var(--text-muted);">Use <code>&lt;marker&gt;</code> &amp; <code>@Ref</code></span>`,
    });
    modeRow.querySelectorAll("[data-pmode]").forEach((b) => {
      b.onclick = () => {
        const nextMode = b.dataset.pmode;
        if (nextMode === sc.prompt_mode || (!sc.prompt_mode && nextMode === "raw")) return;
        if (nextMode === "raw" && sc.prompt_mode === "components") window.syncCanonicalPromptsFromComponents?.(sc);
        if (nextMode === "components") window.hydrateSceneComponentsFromRawIfNeeded?.(sc, true);
        sc.prompt_mode = nextMode;
        window.recordHistorySnapshot?.(`Switch ${sc.id} Prompt Mode`);
        applyFieldSideEffects(idx, sc, { structural: true });
      };
    });
    drawer.appendChild(modeRow);

    const promptAreas = [];
    if (!isMod) {
      const grid = el("div", { style: isTwo ? "display:grid;grid-template-columns:1fr 1fr;gap:8px;" : "display:flex;flex-direction:column;gap:4px;" });
      const specs = isTwo ? [["prompt_m", "Pass 1 Prompt (Left)"], ["prompt_f", "Pass 2 Prompt (Right)"]] : [["prompt", "Scene Prompt"]];
      specs.forEach(([prop, title]) => {
        const ta = el("textarea", { rows: 4, value: sc[prop] || "" });
        bindLiveText(ta, `${sc.id}:${prop}`, `Edit ${sc.id} ${title}`, (v) => { sc[prop] = v; }, () => applyFieldSideEffects(idx, sc));
        promptAreas.push(ta);
        grid.appendChild(el("div", {}, el("div", { style: "font-size:10px;color:#94a3b8;font-weight:600;margin-bottom:2px;", textContent: title }), ta));
      });
      drawer.appendChild(grid);
    } else {
      window.hydrateSceneComponentsFromRawIfNeeded?.(sc);
      const compWrap = el("div", { style: "display:flex;flex-direction:column;gap:6px;padding:8px;background:rgba(15,23,42,0.7);border:1px solid #1e293b;border-radius:5px;" });
      const bindComp = (label, prop, rows, presets) => {
        const top = el("div", { style: "display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:4px;margin-bottom:2px;", innerHTML: `<span style="font-size:10px;font-weight:600;color:#7dd3fc;">${label}</span>` });
        const inp = rows > 1 ? el("textarea", { rows, value: sc[prop] || "" }) : el("input", { type: "text", value: sc[prop] || "" });
        bindLiveText(inp, `${sc.id}:${prop}`, `Edit ${sc.id} ${label}`, (v) => { sc[prop] = v; window.syncCanonicalPromptsFromComponents?.(sc); }, () => applyFieldSideEffects(idx, sc));
        if (presets) {
          const pRow = el("div", { style: "display:flex;gap:3px;flex-wrap:wrap;" });
          presets.forEach(([pt, pv]) => pRow.appendChild(mkBtn(pt, pt, () => {
            sc[prop] = pv; inp.value = pv;
            window.syncCanonicalPromptsFromComponents?.(sc);
            window.recordHistorySnapshot?.(`Preset ${sc.id} ${label}`);
            applyFieldSideEffects(idx, sc);
          }, "font-size:9px;padding:1px 5px;")));
          top.appendChild(pRow);
        }
        compWrap.appendChild(el("div", {}, top, inp));
      };
      if (isTwo) {
        const lck = el("label", { style: "font-size:10px;color:#94a3b8;display:flex;align-items:center;gap:5px;cursor:pointer;", innerHTML: `<input type="checkbox" ${sc.duet_lock_camera !== false ? "checked" : ""}> 🔗 Shared Camera across Pass 1 & Pass 2` });
        lck.querySelector("input").onchange = (e) => { sc.duet_lock_camera = Boolean(e.target.checked); window.syncCanonicalPromptsFromComponents?.(sc); applyFieldSideEffects(idx, sc); window.recordHistorySnapshot?.(`Toggle ${sc.id} Lock Camera`); };
        compWrap.appendChild(lck);
      }
      bindComp("🎥 Camera & Framing", "prompt_camera", 2, CAM_PRESETS);
      bindComp("💡 Setting & Lighting", "prompt_atmosphere", 1);
      if (isTwo) {
        bindComp("🎭 Pass 1 Subject (Left)", "prompt_performance_m", 2);
        bindComp("🎭 Pass 2 Subject (Right)", "prompt_performance_f", 2);
        bindComp("🔊 Pass 1 Soundscape", "prompt_soundscape_m", 1);
        bindComp("🔊 Pass 2 Soundscape", "prompt_soundscape_f", 1);
      } else {
        bindComp("🎭 Lead Subject & Vocal", "prompt_performance", 2);
        bindComp("🔊 Audio Soundscape", "prompt_soundscape", 1);
      }
      bindComp("🎻 Diegetic Musicians & Cues", "prompt_musicians", 2);
      drawer.appendChild(compWrap);
    }

    const markers = (state.projectData?.markers || []).filter((m) => Number(m.time) >= Number(sc.start) - 0.05 && Number(m.time) <= Number(sc.end) + 0.05);
    if (markers.length > 0 && promptAreas[0]) {
      const mBar = el("div", { style: "display:flex;flex-wrap:wrap;gap:4px;align-items:center;font-size:10px;color:#f59e0b;", innerHTML: `<span style="font-weight:600;">🚩 Scene Markers:</span>` });
      markers.forEach((m) => mBar.appendChild(mkBtn(`<${m.name}> @${Number(m.time).toFixed(1)}s`, "Insert relative marker into prompt", () => {
        const ta = promptAreas[0], tag = `<${m.name}>`, s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? ta.value.length;
        ta.value = ta.value.slice(0, s) + tag + ta.value.slice(e);
        sc[isTwo ? "prompt_m" : "prompt"] = ta.value;
        window.recordHistorySnapshot?.(`Insert Marker ${tag}`);
        applyFieldSideEffects(idx, sc);
      }, "font-size:10px;padding:1px 6px;border-color:rgba(245,158,11,0.45);color:#fcd34d;")));
      drawer.appendChild(mBar);
    }

    // Negative prompt card
    const negBox = el("div", { style: "padding:7px 8px;background:rgba(127,29,29,0.12);border:1px solid rgba(239,68,68,0.28);border-radius:5px;display:flex;flex-direction:column;gap:5px;" });
    const negMode = el("select", { style: "font-size:10px;padding:2px 5px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:4px;" });
    populateOptions(negMode, [["inherit", "Inherit & Combine w/ Global"], ["override", "Override Global Negative"]], sc.negative_prompt_mode || "inherit");
    negMode.onchange = () => { sc.negative_prompt_mode = negMode.value || "inherit"; window.recordHistorySnapshot?.(`Change ${sc.id} Negative Mode`); applyFieldSideEffects(idx, sc); };
    negBox.appendChild(el("div", { style: "display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:5px;" }, el("span", { style: "font-size:10px;font-weight:700;color:#fca5a5;", textContent: "🚫 SCENE NEGATIVE PROMPT" }), negMode));

    const nTa = el("textarea", { rows: 2, placeholder: "Scene negative tokens...", value: sc.negative_prompt || "" });
    bindLiveText(nTa, `${sc.id}:neg`, `Edit ${sc.id} Negative Prompt`, (v) => { if (v.trim()) sc.negative_prompt = v; else delete sc.negative_prompt; }, () => applyFieldSideEffects(idx, sc));
    const chips = el("div", { style: "display:flex;flex-wrap:wrap;gap:4px;" });
    NEG_PRESETS.forEach(([lbl, toks]) => chips.appendChild(mkBtn(lbl, toks, () => {
      const cToks = toks.split(",").map((t) => t.trim()), cur = String(sc.negative_prompt || "").split(",").map((t) => t.trim()).filter(Boolean);
      const curLow = new Set(cur.map((t) => t.toLowerCase())), rmLow = new Set(cToks.map((x) => x.toLowerCase()));
      const next = cToks.every((t) => curLow.has(t.toLowerCase())) ? cur.filter((t) => !rmLow.has(t.toLowerCase())) : [...cur, ...cToks.filter((t) => !curLow.has(t.toLowerCase()))];
      if (next.length) sc.negative_prompt = next.join(", "); else delete sc.negative_prompt;
      nTa.value = sc.negative_prompt || "";
      window.recordHistorySnapshot?.(`Toggle ${sc.id} Negative Preset`);
      applyFieldSideEffects(idx, sc);
    }, "font-size:9.5px;padding:1px 6px;")));
    negBox.appendChild(chips);
    negBox.appendChild(nTa);

    if (isTwo) {
      const nDuet = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:6px;" });
      [["negative_prompt_m", "Pass 1 Extra Negatives"], ["negative_prompt_f", "Pass 2 Extra Negatives"]].forEach(([np, ph]) => {
        const ni = el("input", { type: "text", placeholder: ph, value: sc[np] || "" });
        bindLiveText(ni, `${sc.id}:${np}`, `Edit ${sc.id} ${ph}`, (v) => { if (v.trim()) sc[np] = v; else delete sc[np]; }, () => applyFieldSideEffects(idx, sc));
        nDuet.appendChild(ni);
      });
      negBox.appendChild(nDuet);
    }
    drawer.appendChild(negBox);
    return drawer;
  }

  function buildSceneHeader(sc, idx, qMeta) {
    const lockPill = sc.locked ? `<span class="scene-badge-pill" style="background:rgba(245,158,11,0.22);color:#fbbf24;border:1px solid rgba(245,158,11,0.45);">🔒 Locked</span>` : "";
    const titleRow = el("div", {
      className: "scene-card-title-row",
      innerHTML: `<span class="scene-id-pill">Scene ${escapeHtml(sc.id)}</span>${lockPill}`
        + `<span class="scene-time-meta">${buildMetaSummaryHtml(sc)}</span>`
        + `<span class="scene-badge-pill" style="background:${qMeta.statusBg};color:${qMeta.statusColor};">${escapeHtml(qMeta.statusText)}</span>`,
      onclick: () => {
        if (window.focusSceneInTimeline) window.focusSceneInTimeline(idx);
        else state.selectedSceneIdx = idx;
        if (state.scenesTabSplitMode && typeof window.selectScene === "function" && !window.focusSceneInTimeline) window.selectScene(idx);
        else syncCardSelectionStyles();
      },
    });
    const isLooping = Boolean(sc.id && state.masterLoopSceneId === sc.id);
    const repBtn = mkBtn(
      isLooping ? "🔁 Looping" : "🔁 Repeat",
      "Loop this scene in the Master Video player",
      (e) => { e?.stopPropagation?.(); window.toggleSceneMasterLoop?.(idx); },
      isLooping ? "color:#7dd3fc;border-color:rgba(56,189,248,0.65);background:rgba(14,165,233,0.22);padding:2px 7px;font-size:10px;font-weight:700;" : "padding:2px 7px;font-size:10px;color:#cbd5e1;"
    );
    repBtn.dataset.action = "repeat";
    const lockBtn = mkBtn(
      sc.locked ? "🔒 Locked" : "🔓 Lock",
      sc.locked ? "Unlock scene to allow edits" : "Lock scene to prevent accidental edits",
      (e) => { e?.stopPropagation?.(); window.toggleSceneLock?.(idx); },
      sc.locked ? "color:#fcd34d;border-color:rgba(245,158,11,0.55);background:rgba(245,158,11,0.2);padding:2px 7px;font-size:10px;font-weight:700;" : "padding:2px 7px;font-size:10px;"
    );
    lockBtn.dataset.action = "lock";
    const revBtn = mkBtn("↺ Revert", "Revert scene parameters back to rendered active take on disk", () => window.revertSceneToActiveTake?.(sc.id, revBtn), "color:#c4b5fd;border-color:rgba(167,139,250,0.45);background:rgba(139,92,246,0.16);padding:2px 7px;font-size:10px;");
    revBtn.dataset.action = "revert";
    revBtn.style.display = qMeta.needsRevert ? "inline-flex" : "none";
    revBtn.disabled = Boolean(sc.locked);

    const dupBtn = mkBtn("📋", "Duplicate scene (with takes) immediately after it", () => window.duplicateScene?.(idx), "padding:2px 7px;font-size:10px;color:#38bdf8;border-color:rgba(56,189,248,0.35);");
    const delBtn = mkBtn("🗑️", "Delete scene", () => window.deleteScene?.(idx), "padding:2px 7px;font-size:10px;color:#f87171;border-color:rgba(239,68,68,0.35);");
    delBtn.disabled = Boolean(sc.locked);
    const actions = el("div", { className: "scene-card-actions" }, repBtn, lockBtn, revBtn, dupBtn, delBtn);
    return el("div", { className: "scene-card-header" }, titleRow, actions);
  }

  function buildVisualColumn(sc, idx, qMeta, anchorOpts) {
    const vis = el("div", { className: "scene-card-visual" });
    const startInfo = typeof resolveAnchorDetails === "function" ? resolveAnchorDetails(sc.anchor) : null;
    const thumbBox = el("div", {
      className: "scene-thumb-box",
      title: "Click to pick Starting Frame Anchor visually",
      innerHTML: startInfo ? `<img src="${startInfo.url}" alt="${escapeHtml(startInfo.aid)}"><span class="scene-thumb-aid">[${escapeHtml(startInfo.aid || "Start")}] Start</span>` : `<div class="scene-thumb-placeholder">No Starting Frame</div>`,
      onclick: (e) => { e.stopPropagation(); openAnchorPopover(thumbBox, sc, idx, false); },
    });
    const anchorSel = el("select", { className: "scene-visual-select" });
    populateOptions(anchorSel, anchorOpts, sc.anchor || "");
    anchorSel.onchange = () => { sc.anchor = anchorSel.value; applyFieldSideEffects(idx, sc, { structural: true }); window.recordHistorySnapshot?.(`Change ${sc.id} Anchor`); };
    vis.appendChild(thumbBox);
    vis.appendChild(anchorSel);
    const srcMiniHost = el("div"); window.renderSceneAnchorSourcesMini?.(srcMiniHost, sc.anchor); if ((srcMiniHost.children || srcMiniHost.childNodes || []).length) vis.appendChild(srcMiniHost);

    if (!sc.anchor_end && !expandedEndAnchorIds.has(sc.id)) {
      vis.appendChild(el("button", {
        type: "button",
        className: "scene-end-anchor-chip",
        textContent: "+ End Anchor (Blend)",
        title: "Add an optional End Anchor frame for seamless blending",
        onclick: () => { expandedEndAnchorIds.add(sc.id); replaceCardInPlace(idx, sc); },
      }));
    } else {
      const endBox = el("div", { className: "scene-end-anchor-box" });
      const endInfo = sc.anchor_end && typeof resolveAnchorDetails === "function" ? resolveAnchorDetails(sc.anchor_end) : null;
      const endRow = el("div", { className: "scene-end-thumb-row" });
      if (endInfo) {
        const endThumb = el("img", { src: endInfo.url, alt: endInfo.aid, title: "Click to pick End Anchor visually", style: "cursor:pointer;" });
        endThumb.onclick = (e) => { e.stopPropagation(); openAnchorPopover(endThumb, sc, idx, true); };
        endRow.appendChild(endThumb);
      } else {
        endRow.appendChild(mkBtn("🖼️", "Pick End Anchor visually", (e) => { e.stopPropagation(); openAnchorPopover(endRow, sc, idx, true); }, "height:26px;padding:0 5px;font-size:11px;min-width:26px;"));
      }
      const endSel = el("select", { className: "scene-visual-select" });
      populateOptions(endSel, [["", "(End Anchor: None)"], ...anchorOpts], sc.anchor_end || "");
      endSel.onchange = () => {
        const val = endSel.value;
        const apply = () => {
          if (val) sc.anchor_end = val; else { delete sc.anchor_end; expandedEndAnchorIds.delete(sc.id); }
          applyFieldSideEffects(idx, sc, { structural: true });
          window.recordHistorySnapshot?.(`Change ${sc.id} End Anchor`);
        };
        if (window.guardSceneComboChange) window.guardSceneComboChange(sc, { anchor_end: val || undefined }, { onApply: apply, onRevert: () => replaceCardInPlace(idx, sc) });
        else apply();
      };
      endRow.appendChild(endSel);
      endRow.appendChild(mkBtn("✕", "Clear End Anchor", () => {
        const had = Boolean(sc.anchor_end);
        delete sc.anchor_end;
        expandedEndAnchorIds.delete(sc.id);
        if (had) { applyFieldSideEffects(idx, sc, { structural: true }); window.recordHistorySnapshot?.(`Clear ${sc.id} End Anchor`); }
        else replaceCardInPlace(idx, sc);
      }, "font-size:10px;padding:1px 5px;height:24px;min-width:22px;color:#f87171;"));
      endBox.appendChild(endRow);
      vis.appendChild(endBox);
    }
    return vis;
  }

  function buildControlsColumn(sc, idx, cat, pOpen, fOpen) {
    const isTwo = isTwoPassScene(sc), isStereo = isStereoDuetMode(sc.mode);
    const ctrls = el("div", { className: "scene-card-controls" });

    // Row 1 (Pinned Core Row): Mode, Audio Stem(s), Video Model, Resolution, Upsampling
    const guardCard = (patch, onApply) => {
      if (window.guardSceneComboChange) window.guardSceneComboChange(sc, patch, { onApply, onRevert: () => replaceCardInPlace(idx, sc) });
      else onApply();
    };
    const modeSel = el("select");
    populateOptions(modeSel, [["solo", "Solo (Single Pass)"], ["duet_method_a", "Stereo Duet (Method A)"], ["duet_method_b", "Two-Pass Duet (Split Screen)"], ["silent_locked", "Ambient / Cutaway (Silent)"]], canonicalizeSceneMode(sc.mode) || "solo");
    modeSel.onchange = () => guardCard({ mode: modeSel.value }, () => { handleSceneModeChange(sc, modeSel.value); applyFieldSideEffects(idx, sc, { structural: true }); window.recordHistorySnapshot?.(`Change ${sc.id} Mode`); });

    const stemOpts = (cat.stems || []).map((st) => [st.value, st.label]);
    const mkStemSel = (cur, defFallback, onCh) => {
      const s = el("select");
      populateOptions(s, stemOpts, cur || (window.pickDefaultStem?.(defFallback) || defFallback), "missing audio file");
      s.onchange = () => { onCh(s.value); applyFieldSideEffects(idx, sc); window.recordHistorySnapshot?.(`Change ${sc.id} Stem`); };
      return s;
    };

    const modOpts = [["", `Default ([#${cat.defaultModelId}] ${cat.defaultModelName})`]];
    Object.keys(cat.vModels || {}).forEach((k) => { const m = cat.vModels[k], t = window.formatVideoModelBadge ? window.formatVideoModelBadge(m, false) : (m?.supports_image_refs ? " [Ref2VA]" : ""); modOpts.push([k, `🖥️ [#${k}] ${m.name || m.model_type}${t}`]); });
    Object.keys(cat.fModels || {}).forEach((k) => { const m = cat.fModels[k]; modOpts.push([k, `☁️ ${m.name || m.model_type || k}`]); });
    Object.keys(cat.mmModels || {}).forEach((k) => { const m = cat.mmModels[k]; modOpts.push([k, `🎥 ${m.name || m.model_type || k}`]); });
    const modSel = el("select");
    populateOptions(modSel, modOpts, sc.renderer !== null && sc.renderer !== undefined ? String(sc.renderer) : "");
    modSel.onchange = () => { const rVal = modSel.value ? (/^\d+$/.test(modSel.value) ? parseInt(modSel.value, 10) : modSel.value) : null; guardCard({ renderer: rVal }, () => { sc.renderer = rVal; applyFieldSideEffects(idx, sc); window.recordHistorySnapshot?.(`Change ${sc.id} Model`); }); };

    const effDefB = window.getEffectiveProjectBackend?.();
    const projDefB = effDefB === "fal" ? "☁️ fal.ai" : (effDefB === "minimax" ? "🎥 MiniMax" : "🖥️ Local");
    const vrId = String(sc.renderer ?? state.projectData?.models?.default_video_renderer ?? "1");
    const isDedFal = Boolean(cat.fModels?.[vrId]), isDedMm = Boolean(cat.mmModels?.[vrId]), isLocOnly = Boolean(cat.vModels?.[vrId]?.local_only) && !isDedFal && !isDedMm;
    const effCardB = isDedFal ? "fal" : (isDedMm ? "minimax" : (isLocOnly ? "local" : (sc.backend || effDefB || "local")));
    const backSel = el("select");
    backSel.dataset.role = "scene-backend";
    populateOptions(backSel, [["", `Inherit (${projDefB})`], ["local", "🖥️ Local GPU"], ["fal", "☁️ fal.ai Cloud"], ["minimax", "🎥 MiniMax API"]], isDedFal ? "fal" : (isDedMm ? "minimax" : (isLocOnly ? "local" : (sc.backend || ""))));
    backSel.disabled = Boolean(sc.locked) || isDedFal || isDedMm || isLocOnly;
    backSel.onchange = () => { const bVal = backSel.value; guardCard({ backend: bVal || undefined }, () => { if (bVal) sc.backend = bVal; else delete sc.backend; applyFieldSideEffects(idx, sc); window.recordHistorySnapshot?.(`Change ${sc.id} Backend`); }); };

    const resOpts = [["", `Default (${cat.defResLabel})`]];
    Object.keys(cat.resDefs || {}).forEach((k) => { const r = cat.resDefs[k]; resOpts.push([k, `#${k}: ${r.resolution || `${r.width}x${r.height}`} — ${r.name || ""}`]); });
    const resSel = el("select");
    populateOptions(resSel, resOpts, sc.resolution !== null && sc.resolution !== undefined ? String(sc.resolution) : "");
    resSel.onchange = () => { if (resSel.value) sc.resolution = /^\d+$/.test(resSel.value) ? parseInt(resSel.value, 10) : resSel.value; else delete sc.resolution; applyFieldSideEffects(idx, sc); window.recordHistorySnapshot?.(`Change ${sc.id} Resolution`); };

    const upOpts = [["", `Default (${cat.upLabels?.[cat.defUp] || cat.defUp || "None"})`], ["none", "None (Force Disable)"]];
    Object.entries(cat.upLabels || {}).forEach(([k, lbl]) => { if (k) upOpts.push([k, lbl]); });
    const upSel = el("select");
    populateOptions(upSel, upOpts, cat.curUp || "");
    upSel.onchange = () => { if (upSel.value) sc.spatial_upsampling = upSel.value; else delete sc.spatial_upsampling; applyFieldSideEffects(idx, sc); window.recordHistorySnapshot?.(`Change ${sc.id} Spatial Upsampling`); };
    const upNote = cat.skipOom ? "Skip≥1536" : (cat.skipTarget ? "Skip==Tgt" : "Custom");

    const row1 = el("div", { className: "scene-settings-row" }, makeCtrl("Mode", "min-width:140px;flex:1 1 140px;", modeSel));
    if (isTwo) {
      row1.appendChild(makeCtrl("Split X", "flex:0 0 68px;", mkNum(sc, idx, sc.split_x !== undefined ? sc.split_x : 0.48, "0.01", "0.1", "0.9", "0.50", "100%", "split_x", (v) => { const n = parseFloat(v); sc.split_x = isNaN(n) ? 0.5 : n; applyFieldSideEffects(idx, sc); })));
    } else if (isStereo) {
      row1.appendChild(makeCtrl("L Channel", "min-width:120px;flex:0.95 1 120px;", mkStemSel(sc.stem_m || sc.stem_left, "male", (v) => { sc.stem_m = v; })));
      row1.appendChild(makeCtrl("R Channel", "min-width:120px;flex:0.95 1 120px;", mkStemSel(sc.stem_f || sc.stem_right, "female", (v) => { sc.stem_f = v; })));
    } else {
      const defS = sc.mode === "silent_locked" ? "silent" : (sc.mode === "female_solo" ? "female" : "male");
      row1.appendChild(makeCtrl("Audio Stem", "min-width:145px;flex:1 1 145px;", mkStemSel(sc.stem, defS, (v) => { sc.stem = v; })));
    }
    row1.appendChild(makeCtrl(`${isTwo ? "Default Engine" : "Backend"} <span data-badge="backend" style="display:${(sc.backend || isDedFal || isDedMm || isLocOnly) ? "inline" : "none"};color:${effCardB === "fal" ? "#38bdf8" : (effCardB === "minimax" ? "#c084fc" : "#cbd5e1")};">${effCardB === "fal" ? "☁️ fal.ai" : (effCardB === "minimax" ? "🎥 MiniMax" : "🖥️ Local")}</span>`, "min-width:118px;flex:0.85 1 118px;", backSel));
    row1.appendChild(makeCtrl(`${isTwo ? "Default Model" : "Video Model"} <span data-badge="model" style="display:${sc.renderer ? "inline" : "none"};color:#60a5fa;">Custom</span>`, "min-width:165px;flex:1.25 1 165px;", modSel));
    row1.appendChild(makeCtrl(`Resolution <span data-badge="res" style="display:${sc.resolution ? "inline" : "none"};color:#34d399;">Custom</span>`, "min-width:145px;flex:1.1 1 145px;", resSel));
    row1.appendChild(makeCtrl(`Upsampling <span data-badge="up" style="display:${(cat.skipOom || cat.skipTarget || cat.curUp) ? "inline" : "none"};color:#c084fc;">${upNote}</span>`, "min-width:145px;flex:1.1 1 145px;", upSel));
    ctrls.appendChild(row1);
    if (isTwo) window.appendTwoPassEngineControls?.(ctrls, sc, idx, applyFieldSideEffects, { mkStemSel, mkNum, makeCtrl });
    window.appendSceneCardRefPicker?.(ctrls, sc, idx, applyFieldSideEffects);

    // Collapsible Bar 1: Padding, 1x Refiner & Look Filters
    ctrls.appendChild(el("div", {
      className: "scene-collapsible-bar",
      innerHTML: `<span style="font-weight:600;color:#cbd5e1;white-space:nowrap;">${fOpen ? "▾" : "▸"} 🎛️ Padding, 1x Refiner &amp; Look</span>`
        + `<span style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;justify-content:flex-end;"><span data-role="adv-summary-badges" style="display:inline-flex;gap:4px;flex-wrap:wrap;">${window.buildAdvSummaryBadgesHtml ? window.buildAdvSummaryBadgesHtml(sc, cat) : ""}</span><span style="color:#94a3b8;font-size:10px;white-space:nowrap;">${fOpen ? "▲ Hide" : "▾ Configure"}</span></span>`,
      onclick: () => {
        if (expandedFilterIds.has(sc.id)) expandedFilterIds.delete(sc.id); else expandedFilterIds.add(sc.id);
        replaceCardInPlace(idx, sc);
      },
    }));

    if (fOpen) {
      const defPad = state.projectData?.project?.render_overlap_seconds ?? 0;
      const setPad = (prop, v) => { const n = parseFloat(v); if (v === "" || isNaN(n)) delete sc[prop]; else sc[prop] = Math.max(0, n); applyFieldSideEffects(idx, sc); };
      const padReset = mkBtn("✕", "Reset padding", () => { delete sc.render_pad_start; delete sc.render_pad_end; window.recordHistorySnapshot?.(`Reset ${sc.id} Padding`); applyFieldSideEffects(idx, sc, { structural: true }); }, `color:#f87171;padding:2px 6px;font-size:10px;display:${(sc.render_pad_start !== undefined || sc.render_pad_end !== undefined) ? "inline-flex" : "none"};`);
      padReset.dataset.action = "reset-pad";
      const aiSel = el("select");
      populateOptions(aiSel, [["", `Default (${cat.shRef || "None"})`], ...(cat.aiRefinerOptions || [])], cat.scRefVal || "");
      aiSel.onchange = () => { window.applySceneFilterValue?.(sc, "ai_refiner", aiSel.value); window.recordHistorySnapshot?.(`Change ${sc.id} Refiner`); applyFieldSideEffects(idx, sc); };

      const advTop = el("div", { className: "scene-settings-row" },
        makeCtrl("Pad Pre / Post (s)", "flex:0 1 auto;", el("div", { style: "display:flex;gap:5px;align-items:center;" },
          mkNum(sc, idx, sc.render_pad_start ?? "", "0.1", "0", "10", `Def(${defPad})`, "70px", "padS", (v) => setPad("render_pad_start", v)),
          mkNum(sc, idx, sc.render_pad_end ?? "", "0.1", "0", "10", `Def(${sc.anchor_end ? 0 : defPad})`, "70px", "padE", (v) => setPad("render_pad_end", v)),
          padReset,
        )),
        makeCtrl(`1x Refiner <span data-badge="flt" style="display:${(cat.hasAnyFilter || cat.isCustomFilter) ? "inline" : "none"};color:#34d399;">${escapeHtml(cat.effRef || (cat.isCustomFilter ? "Custom" : "Default"))}</span>`, "min-width:175px;flex:1.1 1 175px;", aiSel),
      );

      const ffRow = el("div", { className: "scene-filters-strip" });
      const debSel = el("select");
      populateOptions(debSel, [["", "Def"], ["true", "On"], ["false", "Off"]], cat.scFF?.deband === true ? "true" : (cat.scFF?.deband === false ? "false" : ""));
      debSel.onchange = () => { window.applySceneFilterValue?.(sc, "deband", debSel.value); window.recordHistorySnapshot?.(`Change ${sc.id} Deband`); applyFieldSideEffects(idx, sc); };
      ffRow.appendChild(makeCtrl("Deband", "", debSel));
      [
        ["Grain", "film_grain_intensity", cat.scF?.film_grain_intensity, "0.05", "0", "1", "Def"],
        ["CAS", "cas", cat.scFF?.cas, "0.05", "0", "1", "Def"],
        ["Denoise", "denoise", cat.scFF?.denoise, "0.5", "0", "10", "Def"],
        ["Bright", "brightness", cat.scEq?.brightness, "0.02", "-0.5", "0.5", "0"],
        ["Contrast", "contrast", cat.scEq?.contrast, "0.05", "0.5", "1.5", "1"],
        ["Sat", "saturation", cat.scEq?.saturation, "0.05", "0", "2", "1"],
      ].forEach(([lbl, fKey, cur, stp, mn, mx, ph]) => {
        ffRow.appendChild(makeCtrl(lbl, "", mkNum(sc, idx, cur ?? "", stp, mn, mx, ph, "100%", `flt:${fKey}`, (v) => { window.applySceneFilterValue?.(sc, fKey, v); applyFieldSideEffects(idx, sc); })));
      });
      ctrls.appendChild(el("div", { className: "scene-adv-drawer" }, advTop, ffRow));
    }

    // Collapsible Bar 2: Prompt Summary Bar + Expandable Drawer
    const rawPreview = isTwo ? `P1: ${sc.prompt_m || ""} | P2: ${sc.prompt_f || ""}` : (sc.prompt || "");
    const badges = [];
    if (sc.prompt_mode === "components") badges.push('<span class="scene-badge-pill" style="background:rgba(37,99,235,0.2);color:#93c5fd;">🧩 Modular</span>');
    if (sc.negative_prompt || sc.negative_prompt_mode === "override") badges.push('<span class="scene-badge-pill" style="background:rgba(239,68,68,0.18);color:#fca5a5;">🚫 Neg</span>');
    ctrls.appendChild(el("div", {
      className: "scene-prompt-preview-bar",
      innerHTML: `<span class="scene-prompt-snippet">${escapeHtml(rawPreview || "(Empty prompt — click to edit)")}</span>`
        + `<span style="display:flex;gap:4px;align-items:center;">${badges.join("")}<span style="color:#94a3b8;font-size:10px;">${pOpen ? "▲ Hide" : "▾ Edit Prompt & Neg"}</span></span>`,
      onclick: () => {
        if (expandedPromptIds.has(sc.id)) expandedPromptIds.delete(sc.id); else expandedPromptIds.add(sc.id);
        replaceCardInPlace(idx, sc);
      },
    }));
    if (pOpen) ctrls.appendChild(buildPromptDrawer(sc, idx));

    // Collapsible Bar 3: Takes Vault (All Takes, Preview, Restore, Delete)
    const tOpen = expandedTakesIds.has(sc.id), cachedTakes = sc._freshSceneTakes ? { success: true, takes: [] } : sceneTakesCache.get(sc.id);
    const tBadgeHtml = cachedTakes?.takes ? `<span class="scene-badge-pill" data-role="takes-count" style="background:rgba(167,139,250,0.2);color:#ddd6fe;">${cachedTakes.takes.length} take${cachedTakes.takes.length === 1 ? "" : "s"}</span>` : `<span class="scene-badge-pill" data-role="takes-count" style="display:none;"></span>`;
    const takesBar = el("div", {
      className: "scene-collapsible-bar",
      innerHTML: `<span style="font-weight:600;color:#c4b5fd;white-space:nowrap;">${tOpen ? "▾" : "▸"} 🎬 Takes Vault</span>`
        + `<span style="display:flex;gap:5px;align-items:center;">${tBadgeHtml}<span style="color:#94a3b8;font-size:10px;white-space:nowrap;">${tOpen ? "▲ Hide" : "▾ View All / Delete"}</span></span>`,
      onclick: () => {
        if (expandedTakesIds.has(sc.id)) expandedTakesIds.delete(sc.id); else expandedTakesIds.add(sc.id);
        replaceCardInPlace(idx, sc);
      },
    });
    ctrls.appendChild(takesBar);

    if (tOpen) {
      const tDrawer = el("div", { className: "scene-takes-drawer" });
      const prevWrap = el("div", { className: "scene-take-inline-preview", style: "display:none;" });
      const vid = el("video", { controls: true, style: "width:100%;max-height:220px;border-radius:4px;background:#000;" });
      const prevLbl = el("div", { style: "font-size:10px;color:#c4b5fd;font-weight:600;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center;" });
      prevWrap.appendChild(prevLbl); prevWrap.appendChild(vid);
      const tList = el("div", { className: "scene-takes-list" });
      tDrawer.appendChild(prevWrap); tDrawer.appendChild(tList);
      const paintTakes = (data) => {
        if (!tList.isConnected) return;
        const cntEl = takesBar.querySelector("[data-role='takes-count']");
        if (cntEl && data?.takes) { cntEl.textContent = `${data.takes.length} take${data.takes.length === 1 ? "" : "s"}`; cntEl.style.display = "inline-flex"; }
        window.renderTakesListItems?.(tList, sc, data, sceneTakesSelected.get(sc.id), (t) => {
          sceneTakesSelected.set(sc.id, t.take_id);
          const outName = t.out_name || sc.output_name || sc.id;
          vid.src = `/media/${encodeURIComponent(state.currentSlug)}/clip/${encodeURIComponent(outName)}?take=${encodeURIComponent(t.take_id)}&t=${Date.now()}`;
          prevLbl.innerHTML = `<span>Previewing Take ${t.take_number || "?"} ${t.take_id === data.active_take_id ? "(★ ACTIVE)" : "(Vault)"}</span>`;
          prevWrap.style.display = "block"; vid.load();
          paintTakes(data);
        }, { onDeleted: (d) => sceneTakesCache.set(sc.id, d) });
      };
      if (cachedTakes) setTimeout(() => paintTakes(cachedTakes), 0);
      else tList.innerHTML = `<div style="font-size:11px;color:#94a3b8;padding:4px 2px;">Loading takes...</div>`;
      if (!sc._freshSceneTakes && state.currentSlug) {
        fetch(`/api/takes/${encodeURIComponent(state.currentSlug)}/${encodeURIComponent(sc.id)}`)
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => { if (d?.success) { sceneTakesCache.set(sc.id, d); paintTakes(d); } })
          .catch(() => {});
      }
      ctrls.appendChild(tDrawer);
    }
    return ctrls;
  }

  function buildSceneCard(sc, idx) {
    const qMeta = getSceneQueueMeta(sc);
    const cat = window.getSceneOptionCatalog ? window.getSceneOptionCatalog(sc) : {};
    const anchorOpts = (typeof getMergedAnchorsList === "function" ? getMergedAnchorsList() : []).map(({ aid, resolvedName, valueName, isDiskOnly }) => [
      valueName, isDiskOnly ? `[${aid}] ${resolvedName} (Disk)` : `[${aid}] ${resolvedName}`, { aid },
    ]);
    const pOpen = expandAllPrompts || expandedPromptIds.has(sc.id);
    const fOpen = expandAllFilters || expandedFilterIds.has(sc.id);
    const card = el("div", {
      className: `scene-card${idx === state.selectedSceneIdx ? " is-selected" : ""}${sc.locked ? " is-locked" : ""}${qMeta.isRendering ? " is-rendering" : qMeta.isQueued ? " is-queued" : ""}`,
      dataset: { sceneIdx: String(idx), sceneId: sc.id || "" },
      onclick: (e) => {
        if (e.target?.closest?.("input, select, textarea, button, video, a, .scene-thumb-box, .scene-collapsible-bar, .scene-prompt-preview-bar, .scenes-anchor-popover, .scene-takes-drawer, .scene-card-title-row")) return;
        if (window.focusSceneInTimeline) window.focusSceneInTimeline(idx);
        syncCardSelectionStyles();
      },
    },
      buildSceneHeader(sc, idx, qMeta),
      el("div", { className: "scene-card-body" }, buildVisualColumn(sc, idx, qMeta, anchorOpts), buildControlsColumn(sc, idx, cat, pOpen, fOpen)),
    );
    if (sc.locked) {
      card.querySelectorAll(".scene-card-body input, .scene-card-body select, .scene-card-body textarea, .scene-card-body button").forEach((n) => {
        if (!n.classList.contains("btn-take-preview")) n.disabled = true;
      });
    }
    return card;
  }

  function replaceCardInPlace(idx, sc) {
    const oldCard = getTabRoot()?.querySelector(`.scene-card[data-scene-idx="${idx}"]`);
    if (oldCard) oldCard.replaceWith(buildSceneCard(sc, idx));
  }

  function syncCardSelectionStyles() {
    getTabRoot()?.querySelectorAll(".scene-card").forEach((c) => {
      c.classList.toggle("is-selected", parseInt(c.dataset.sceneIdx, 10) === state.selectedSceneIdx);
    });
  }

  function renderSceneList(force = false) {
    const root = getTabRoot();
    if (!root) return;
    if ((!isTabActive() && !force) || (!force && isEditingInTab())) { staleList = true; return; }
    staleList = false;

    const scenes = state.projectData?.scenes || [];
    const totalDur = scenes.reduce((acc, s) => acc + Math.max(0, Number(s.end || 0) - Number(s.start || 0)), 0);
    const qNeed = scenes.filter((s) => getSceneQueueMeta(s).isQueued || getSceneQueueMeta(s).isRendering).length;
    const lockedCount = scenes.filter((s) => Boolean(s.locked)).length;
    const unlockedCount = scenes.length - lockedCount;
    const prevScroll = root.querySelector(".scenes-list-container")?.scrollTop || 0;

    root.innerHTML = "";
    if (activeFilter === "edited") activeFilter = "all";
    const mm = Math.floor(totalDur / 60), ss = Math.round(totalDur % 60);
    const gL = el("div", { className: "scenes-toolbar-group", innerHTML: `<span class="scenes-summary-badge">${scenes.length} Scenes • ${mm}:${String(ss).padStart(2, "0")} • ${qNeed} Queued</span>` });
    [["all", "All"], ["needs_render", `Needs Render (${qNeed})`], ["duet", "Duets"], ["silent", "Silent"], ["locked", `Locked (${lockedCount})`], ["unlocked", `Unlocked (${unlockedCount})`]].forEach(([k, lbl]) => {
      gL.appendChild(el("button", { type: "button", className: `scenes-filter-pill${activeFilter === k ? " active" : ""}`, dataset: { filterKey: k }, textContent: lbl, onclick: () => { activeFilter = k; renderSceneList(true); } }));
    });

    const listEl = el("div", { className: "scenes-list-container" });
    const renderCardsOnly = () => {
      listEl.innerHTML = "";
      const q = searchQuery.trim().toLowerCase();
      let shown = 0;
      scenes.forEach((sc, idx) => {
        const qMeta = getSceneQueueMeta(sc), mc = canonicalizeSceneMode(sc.mode);
        if (activeFilter === "needs_render" && !qMeta.isQueued && !qMeta.isRendering) return;
        if (activeFilter === "duet" && mc !== "duet_method_a" && mc !== "duet_method_b") return;
        if (activeFilter === "silent" && mc !== "silent_locked") return;
        if (activeFilter === "locked" && !sc.locked) return;
        if (activeFilter === "unlocked" && sc.locked) return;
        if (q && ![sc.id, sc.anchor, sc.anchor_end, sc.prompt, sc.prompt_m, sc.prompt_f, sc.negative_prompt].filter(Boolean).join(" ").toLowerCase().includes(q)) return;
        shown++;
        listEl.appendChild(buildSceneCard(sc, idx));
      });
      if (shown === 0) listEl.innerHTML = `<div style="padding:36px;text-align:center;color:var(--text-muted);">No scenes match the current filter.</div>`;
      if (prevScroll > 0 && !q) listEl.scrollTop = prevScroll;
    };

    const sInp = el("input", { type: "search", className: "scenes-search-input", placeholder: "Filter ID, anchor (A15), or prompt...", value: searchQuery, oninput: () => { searchQuery = sInp.value; renderCardsOnly(); } });
    const splitBtn = el("button", {
      type: "button", className: "btn btn-sm",
      textContent: state.scenesTabSplitMode ? "⤢ Full Width" : "⇄ Split w/ Inspector",
      title: "Toggle full-width list vs side-by-side with Scene Inspector",
      onclick: () => {
        state.scenesTabSplitMode = !state.scenesTabSplitMode;
        document.getElementById("appMain")?.classList.toggle("layout-scenes-wide", isTabActive() && !state.scenesTabSplitMode);
        splitBtn.textContent = state.scenesTabSplitMode ? "⤢ Full Width" : "⇄ Split w/ Inspector";
        if (state.scenesTabSplitMode && state.selectedSceneIdx !== null) window.selectScene?.(state.selectedSceneIdx);
      },
    });
    const gR = el("div", { className: "scenes-toolbar-group" },
      sInp,
      el("button", { type: "button", className: "btn btn-sm", textContent: expandAllFilters ? "▲ Collapse Filters" : "🎛️ Expand All Filters", onclick: () => { expandAllFilters = !expandAllFilters; renderSceneList(true); } }),
      el("button", { type: "button", className: "btn btn-sm", textContent: expandAllPrompts ? "▲ Collapse Prompts" : "📝 Expand All Prompts", onclick: () => { expandAllPrompts = !expandAllPrompts; renderSceneList(true); } }),
      splitBtn,
      el("button", { type: "button", className: "btn btn-sm btn-primary", textContent: "+ Add Scene", onclick: () => document.getElementById("btnAddScene")?.click() }),
    );
    root.appendChild(el("div", { className: "scenes-tab-shell" }, el("div", { className: "scenes-tab-toolbar" }, gL, gR), listEl));
    renderCardsOnly();
  }

  function syncScenesTabCard(sc) {
    if (!sc || isEditingInTab()) return;
    if (!isTabActive()) { staleList = true; return; }
    if (activeFilter === "locked" || activeFilter === "unlocked") { renderSceneList(true); return; }
    const scenes = state.projectData?.scenes || [];
    const lockedCount = scenes.filter((s) => Boolean(s.locked)).length;
    const lp = getTabRoot()?.querySelector('.scenes-filter-pill[data-filter-key="locked"]');
    const up = getTabRoot()?.querySelector('.scenes-filter-pill[data-filter-key="unlocked"]');
    if (lp) lp.textContent = `Locked (${lockedCount})`;
    if (up) up.textContent = `Unlocked (${scenes.length - lockedCount})`;
    const idx = scenes.indexOf(sc);
    if (idx >= 0) replaceCardInPlace(idx, sc);
  }

  function installLiveSyncHooks() {
    const wrapFn = (fnName, onAfter, preventInspSync = false) => {
      const orig = window[fnName];
      if (typeof orig !== "function" || orig._scenesTabHooked) return;
      const wrapped = function () {
        if (preventInspSync) isSyncingFromInspector = true;
        try { orig.apply(this, arguments); } finally { if (preventInspSync) isSyncingFromInspector = false; }
        onAfter();
      };
      wrapped._scenesTabHooked = true;
      window[fnName] = wrapped;
    };
    wrapFn("selectScene", () => { if (isTabActive() && !isEditingInTab()) syncCardSelectionStyles(); }, true);
    wrapFn("renderQueueUI", () => { if (isTabActive() && !isEditingInTab()) renderSceneList(); else staleList = true; });
    wrapFn("updateInspectorAnchorsDropdown", () => { if (isTabActive() && !isEditingInTab()) renderSceneList(); else staleList = true; });

    const inspForm = document.getElementById("inspectorForm");
    if (inspForm && !inspForm.dataset.scenesTabSyncBound) {
      inspForm.dataset.scenesTabSyncBound = "1";
      let tmr = null;
      const onInspMutate = () => {
        clearTimeout(tmr);
        tmr = setTimeout(() => {
          const sc = state.projectData?.scenes?.[state.selectedSceneIdx];
          if (sc && isTabActive() && !isEditingInTab()) syncScenesTabCard(sc);
        }, 80);
      };
      inspForm.addEventListener("input", onInspMutate);
      inspForm.addEventListener("change", onInspMutate);
      inspForm.addEventListener("click", (e) => { if (e.target?.closest("button, .label-actions span, .comp-cam-chip, .comp-snd-chip, .scene-neg-chip")) onInspMutate(); });
    }
  }

  window.renderSceneList = renderSceneList;
  window.syncScenesTabCard = syncScenesTabCard;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installLiveSyncHooks);
  else installLiveSyncHooks();
})();
