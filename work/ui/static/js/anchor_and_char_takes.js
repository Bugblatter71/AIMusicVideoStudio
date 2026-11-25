/**
 * Music Video Generation Studio - Anchor Takes Vault & Character Look Reference Generator
 *
 * Implements:
 * 1. Multi-take gallery on each Anchor card (Use active take, Delete take, Turn take into a separate Anchor)
 * 2. AI Reference Image Generator & Takes manager on each Character Studio Look card (#tab-characters)
 * 3. Click-to-open-in-new-tab on all anchor & character reference thumbnails (including Scene Inspector previews)
 */
(function () {
  "use strict";

  let anchorTakesCache = {}, activeGalleryAid = null;
  let selectedGalleryTakeIds = new Set(), lastGallerySelectIdx = null;

  function openImageInNewTab(url) {
    if (!url) return;
    window.open(url, "_blank", "noopener");
  }
  window.openImageInNewTab = openImageInNewTab;

  async function activateAnchorTake(aid, takeId, seed, idxLabel) {
    const res = await fetch("/api/anchors/take-use", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: window.state.currentSlug, aid, take_id: takeId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) {
      window.showToast?.(`❌ Failed to activate take: ${data.error || res.status}`);
      return false;
    }
    anchorTakesCache = data.anchor_takes || anchorTakesCache;
    if (window.state) window.state.anchorTakes = anchorTakesCache;
    const aEntry = (window.state.projectData?.anchors || []).find((a) => (window.extractAnchorId?.(a.filename) || "").toUpperCase() === aid);
    if (aEntry && seed) aEntry.seed = seed;
    await window.refreshProjectAssets?.(true);
    if (activeGalleryAid) renderAnchorTakesGalleryModal();
    window.showToast?.(`✓ Activated take ${idxLabel} (seed ${seed}) for [${aid}]`);
    return true;
  }

  async function promoteAnchorTakeToNewAnchor(aid, takeId, customLabel, idxLabel, stayInGallery = false) {
    const res = await fetch("/api/anchors/take-to-anchor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: window.state.currentSlug, aid, take_id: takeId, label: customLabel || "" }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) {
      window.showToast?.(`❌ Failed to create anchor from take: ${data.error || res.status}`);
      return null;
    }
    anchorTakesCache = data.anchor_takes || anchorTakesCache;
    if (window.state) window.state.anchorTakes = anchorTakesCache;
    if (data.new_anchor && Array.isArray(window.state.projectData?.anchors)) {
      const exists = window.state.projectData.anchors.some((a) => a.filename === data.new_anchor.filename);
      if (!exists) window.state.projectData.anchors.push(data.new_anchor);
    }
    await window.refreshProjectAssets?.(true);
    if (stayInGallery && activeGalleryAid) {
      renderAnchorTakesGalleryModal();
    } else {
      closeAnchorTakesGallery();
      window.focusAnchorInStudio?.(data.new_aid);
    }
    window.showToast?.(`✨ Created new Anchor [${data.new_aid}] (${data.new_anchor?.filename}) from take ${idxLabel}`);
    return data;
  }

  async function deleteAnchorTakes(aid, takeIds, confirmMsg, doneMsg) {
    const ids = Array.from(new Set((takeIds || []).map((x) => String(x || "").trim()).filter(Boolean)));
    if (!ids.length || !confirm(confirmMsg)) return false;
    const payload = ids.length === 1
      ? { slug: window.state.currentSlug, aid, take_id: ids[0] }
      : { slug: window.state.currentSlug, aid, take_ids: ids };
    const res = await fetch("/api/anchors/take-delete", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) { window.showToast?.(`❌ Delete failed: ${data.error || res.status}`); return false; }
    ids.forEach((id) => selectedGalleryTakeIds.delete(id));
    anchorTakesCache = data.anchor_takes || anchorTakesCache;
    if (window.state) window.state.anchorTakes = anchorTakesCache;
    await window.refreshProjectAssets?.(true);
    if (activeGalleryAid) renderAnchorTakesGalleryModal();
    window.showToast?.(doneMsg);
    return true;
  }
  const deleteAnchorTake = (aid, takeId, idxLabel) =>
    deleteAnchorTakes(aid, [takeId], `Delete take ${idxLabel} (${takeId}) for [${aid}]?`, `🗑 Deleted take ${idxLabel} for [${aid}]`);
  window.anchorTakeActions = {
    getSlot: (aid) => anchorTakesCache[aid] || { active_take_id: "", takes: [] },
    activate: activateAnchorTake, promote: promoteAnchorTakeToNewAnchor, deleteSingle: deleteAnchorTake,
  };

  function ensureGalleryModalElement() {
    let overlay = document.getElementById("anchorTakesGalleryModal");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "anchorTakesGalleryModal";
    overlay.className = "anchor-gallery-overlay";
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeAnchorTakesGallery();
    });
    document.addEventListener("keydown", (e) => {
      const lb = document.getElementById("anchorTakeLightboxModal");
      if (lb && lb.style.display === "flex") return;
      if (e.key === "Escape" && overlay.style.display === "flex") closeAnchorTakesGallery();
    });
    document.body.appendChild(overlay);
    return overlay;
  }

  function closeAnchorTakesGallery() {
    activeGalleryAid = null; selectedGalleryTakeIds.clear(); lastGallerySelectIdx = null;
    const overlay = document.getElementById("anchorTakesGalleryModal");
    if (overlay) overlay.style.display = "none";
  }

  async function openAnchorTakesGallery(identifier) {
    const aid = (window.extractAnchorId?.(identifier || "") || String(identifier || "")).toUpperCase();
    if (!aid) return;
    if (activeGalleryAid !== aid) { selectedGalleryTakeIds.clear(); lastGallerySelectIdx = null; }
    activeGalleryAid = aid;
    const overlay = ensureGalleryModalElement();
    overlay.style.display = "flex";
    renderAnchorTakesGalleryModal();
    await fetchAnchorTakes(window.state?.currentSlug);
  }

  function renderAnchorTakesGalleryModal() {
    const overlay = document.getElementById("anchorTakesGalleryModal");
    if (!overlay || !activeGalleryAid || overlay.style.display !== "flex") return;
    const aid = activeGalleryAid.toUpperCase();
    const slug = encodeURIComponent(window.state?.currentSlug || "");
    const esc = window.escapeHtml || ((x) => String(x || ""));
    const anchors = window.state?.projectData?.anchors || [];
    const anchorObj = anchors.find((a) => (window.extractAnchorId?.(a.filename) || "").toUpperCase() === aid) || { filename: `${aid}_Scene.jpg`, seed: 1001, prompt: "" };
    const slotData = anchorTakesCache[aid] || { active_take_id: "", takes: [] };
    const takes = slotData.takes || [];
    const activeId = slotData.active_take_id || "";
    const validTakeIds = new Set(takes.map((t) => t.take_id));
    selectedGalleryTakeIds.forEach((id) => { if (!validTakeIds.has(id)) selectedGalleryTakeIds.delete(id); });

    overlay.innerHTML = `
      <div class="anchor-gallery-modal" role="dialog" aria-label="Anchor Takes Gallery">
        <div class="anchor-gallery-header">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <span style="font-size:15px;font-weight:700;color:#f8fafc;">🖼️ Anchor Takes Gallery</span>
            <select id="galleryAnchorSwitcher" title="Switch Anchor in Gallery" style="font-size:12px;font-weight:600;padding:4px 8px;background:#0f172a;color:#38bdf8;border:1px solid #334155;border-radius:5px;"></select>
            <span style="font-size:11px;padding:2px 8px;border-radius:4px;background:rgba(167,139,250,0.2);color:#c4b5fd;font-weight:600;">${takes.length} take${takes.length === 1 ? "" : "s"}</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <button type="button" class="btn btn-sm" id="btnGallerySelectAllTakes" style="font-size:11px;padding:4px 9px;display:${takes.length ? "inline-flex" : "none"};"></button>
            <button type="button" class="btn btn-sm" id="btnGalleryDeleteSelected" title="Delete all selected takes in one batch" style="font-size:11px;padding:4px 10px;color:#f87171;border-color:rgba(248,113,113,0.45);display:${takes.length ? "inline-flex" : "none"};" disabled></button>
            <button type="button" class="btn btn-sm" id="btnGalleryNewBlankAnchor" title="Create a brand-new blank Anchor and open it in the gallery" style="font-size:11px;padding:4px 10px;color:#34d399;border-color:rgba(52,211,153,0.4);">+ New Blank Anchor</button>
            <button type="button" class="btn btn-sm" id="btnGalleryDeleteAnchor" title="Delete Anchor [${esc(aid)}] from project" style="font-size:11px;padding:4px 10px;color:#f87171;border-color:rgba(248,113,113,0.4);">🗑 Delete Anchor [${esc(aid)}]</button>
            <button type="button" class="btn btn-sm" id="btnCloseAnchorGallery" style="font-size:13px;padding:3px 10px;">✕ Close</button>
          </div>
        </div>
        <div class="anchor-gallery-toolbar">
          <input type="text" id="galleryPromptInput" value="${esc(anchorObj.prompt || "")}" placeholder="Anchor prompt (type @character)..." style="flex:1;min-width:240px;font-family:monospace;font-size:11.5px;padding:5px 8px;background:#0b1220;color:#f1f5f9;border:1px solid #334155;border-radius:5px;">
          <label style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-muted);">Seed:
            <input type="number" id="gallerySeedInput" value="${anchorObj.seed ?? 1001}" style="width:78px;font-size:11px;padding:4px 6px;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:4px;">
            <button type="button" class="btn-mini" id="btnGalleryRandSeed" title="Randomize Seed">🎲</button>
          </label>
          <label style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-muted);">Takes:
            <input type="number" id="galleryGenCountSelect" min="1" max="99" step="1" title="Number of takes to generate (1-99)" style="width:58px;font-size:11px;padding:4px 6px;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:4px;">
          </label>
          <button type="button" class="btn btn-primary" id="btnGalleryGenerateTakes" style="font-size:11.5px;padding:5px 12px;">⚡ Generate Take(s)</button>
        </div>
        <div id="gallerySourceRefsBar" class="anchor-gallery-sources-bar"></div>
        <div class="anchor-gallery-grid" id="anchorTakesGalleryGrid"></div>
      </div>`;

    const switcher = overlay.querySelector("#galleryAnchorSwitcher");
    (window.getMergedAnchorsList?.() || []).forEach((item) => {
      const tCnt = (anchorTakesCache[item.aid?.toUpperCase()]?.takes || []).length;
      const opt = new Option(`[${item.aid}] ${item.resolvedName} (${tCnt} take${tCnt === 1 ? "" : "s"})`, item.aid);
      switcher.add(opt);
    });
    switcher.value = aid;
    switcher.onchange = () => { activeGalleryAid = switcher.value; selectedGalleryTakeIds.clear(); lastGallerySelectIdx = null; renderAnchorTakesGalleryModal(); };

    const gPrompt = overlay.querySelector("#galleryPromptInput");
    const gSeed = overlay.querySelector("#gallerySeedInput");
    const gCnt = overlay.querySelector("#galleryGenCountSelect");
    const gGenBtn = overlay.querySelector("#btnGalleryGenerateTakes");
    gCnt.value = String(anchorObj.gen_count || 2);
    const syncGalleryGenBtn = () => {
      const n = Math.max(1, Math.min(99, parseInt(gCnt.value, 10) || 1));
      if (gGenBtn) gGenBtn.textContent = n > 1 ? `⚡ Generate ${n} Takes` : "⚡ Generate Take";
    };
    syncGalleryGenBtn();
    const ensureRegistered = () => {
      const list = (window.state.projectData = window.state.projectData || {}).anchors = window.state.projectData.anchors || [];
      let found = list.find((a) => (window.extractAnchorId?.(a.filename) || "").toUpperCase() === aid);
      if (!found) { found = { ...anchorObj, filename: anchorObj.filename || `${aid}_Scene.jpg` }; list.push(found); }
      return found;
    };
    window.renderAnchorSourceRefsPanel?.(overlay.querySelector("#gallerySourceRefsBar"), anchorObj, aid, {
      promptInput: gPrompt, compact: true, ensureAnchorObj: ensureRegistered, onChange: () => window.updateAnchorStudioUI?.(),
    });
    window.bindCharacterMentionAutocomplete?.(gPrompt);
    gPrompt.addEventListener("input", () => { anchorObj.prompt = gPrompt.value; window.markProjectDirty?.(); });
    gSeed.addEventListener("change", () => { anchorObj.seed = parseInt(gSeed.value, 10) || 1001; window.markProjectDirty?.(); });
    gCnt.addEventListener("input", () => {
      const n = Math.max(1, Math.min(99, parseInt(gCnt.value, 10) || 1));
      if (gCnt.value !== "" && String(n) !== gCnt.value) gCnt.value = String(n);
      anchorObj.gen_count = n;
      syncGalleryGenBtn();
      window.markProjectDirty?.();
    });
    overlay.querySelector("#btnGalleryRandSeed")?.addEventListener("click", () => {
      const s = Math.floor(Math.random() * 90000) + 1000;
      anchorObj.seed = s;
      gSeed.value = s;
      window.markProjectDirty?.();
    });
    gGenBtn?.addEventListener("click", async () => {
      anchorObj.prompt = gPrompt.value.trim();
      anchorObj.seed = parseInt(gSeed.value, 10) || 1001;
      const cnt = Math.max(1, Math.min(99, parseInt(gCnt.value, 10) || 1));
      gCnt.value = String(cnt);
      anchorObj.gen_count = cnt;
      window.markProjectDirty?.();
      await window.renderSingleAnchor?.(aid, cnt);
    });
    overlay.querySelector("#btnGalleryNewBlankAnchor")?.addEventListener("click", () => {
      const created = window.createNewAnchor?.({ label: "Gallery", prompt: gPrompt.value.trim() });
      if (created) {
        activeGalleryAid = (window.extractAnchorId?.(created.filename) || "").toUpperCase();
        renderAnchorTakesGalleryModal();
      }
    });
    overlay.querySelector("#btnGalleryDeleteAnchor")?.addEventListener("click", async () => {
      if (!confirm(`Remove Anchor [${aid}] (${anchorObj.filename}) from project config?`)) return;
      const idx = anchors.indexOf(anchorObj);
      if (idx >= 0) anchors.splice(idx, 1);
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Delete Anchor ${aid}`);
      await window.refreshProjectAssets?.(true);
      const remaining = window.state?.projectData?.anchors || [];
      if (remaining.length > 0) {
        activeGalleryAid = (window.extractAnchorId?.(remaining[0].filename) || "A01").toUpperCase();
        renderAnchorTakesGalleryModal();
      } else {
        closeAnchorTakesGallery();
      }
    });
    overlay.querySelector("#btnCloseAnchorGallery")?.addEventListener("click", closeAnchorTakesGallery);

    const grid = overlay.querySelector("#anchorTakesGalleryGrid");
    const btnSelAll = overlay.querySelector("#btnGallerySelectAllTakes"), btnDelSel = overlay.querySelector("#btnGalleryDeleteSelected");
    const syncGallerySelUI = () => {
      const cnt = selectedGalleryTakeIds.size;
      if (btnSelAll) btnSelAll.textContent = takes.length > 0 && cnt === takes.length ? "☐ Clear Selection" : `☑ Select All (${takes.length})`;
      if (btnDelSel) {
        btnDelSel.textContent = cnt > 0 ? `🗑 Delete Selected (${cnt})` : "🗑 Delete Selected";
        btnDelSel.disabled = cnt === 0; btnDelSel.style.background = cnt > 0 ? "rgba(239,68,68,0.16)" : "";
      }
    };
    const repaintCardSelections = () => {
      grid.querySelectorAll(".anchor-gallery-card").forEach((c) => {
        const s = selectedGalleryTakeIds.has(c.dataset.takeId); c.classList.toggle("is-selected", s);
        const ck = c.querySelector(".gallery-take-select-chk"); if (ck) ck.checked = s;
      });
      syncGallerySelUI();
    };
    syncGallerySelUI();
    btnSelAll?.addEventListener("click", () => {
      if (selectedGalleryTakeIds.size === takes.length) selectedGalleryTakeIds.clear();
      else takes.forEach((t) => selectedGalleryTakeIds.add(t.take_id));
      repaintCardSelections();
    });
    btnDelSel?.addEventListener("click", async () => {
      const ids = takes.filter((t) => selectedGalleryTakeIds.has(t.take_id)).map((t) => t.take_id);
      if (!ids.length) return;
      btnDelSel.disabled = true;
      const ok = await deleteAnchorTakes(aid, ids, `Delete ${ids.length} selected take${ids.length === 1 ? "" : "s"} for [${aid}]?`, `🗑 Deleted ${ids.length} take${ids.length === 1 ? "" : "s"} for [${aid}]`);
      if (!ok) syncGallerySelUI();
    });
    if (takes.length === 0) {
      grid.innerHTML = `<div style="grid-column:1/-1;padding:40px;text-align:center;color:var(--text-muted);border:1px dashed #334155;border-radius:8px;">No rendered takes yet for <strong>[${esc(aid)}]</strong>. Pick a take count above and click <strong>⚡ Generate Take(s)</strong>.</div>`;
      return;
    }

    const baseStem = String(anchorObj.filename || aid).replace(/^A\d+[a-z]?[-_\s]*/i, "").replace(/\.[a-z0-9]+$/i, "") || "Scene";
    takes.forEach((t, idx) => {
      const isAct = t.take_id === activeId, isSel = selectedGalleryTakeIds.has(t.take_id);
      const card = document.createElement("div");
      card.className = `anchor-gallery-card${isAct ? " is-active" : ""}${isSel ? " is-selected" : ""}`;
      card.dataset.takeId = t.take_id;
      const imgUrl = `/media/${slug}/anchor/${t.rel_path}?t=${t.created_at || ""}`;
      const defNewLabel = `${baseStem}_s${t.seed}`;
      card.innerHTML = `
        <div class="anchor-gallery-imgwrap">
          <img src="${esc(imgUrl)}" alt="${esc(t.take_id)}" title="Click to view fullscreen &amp; use Left/Right arrows to compare takes">
          <label class="gallery-take-check-badge" title="Select take for batch delete (Shift+click for range)"><input type="checkbox" class="gallery-take-select-chk" ${isSel ? "checked" : ""}> Take #${idx + 1}</label>
          ${isAct ? `<span style="position:absolute;top:8px;right:8px;font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:4px;background:rgba(16,185,129,0.22);color:#6ee7b7;border:1px solid rgba(16,185,129,0.5);">✓ ACTIVE ANCHOR</span>` : ""}
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#cbd5e1;">
          <span><strong>Seed:</strong> <code>${esc(t.seed)}</code></span>
          <span><strong>Res:</strong> ${esc(t.resolution || "default")}</span>
          <span style="color:var(--text-muted);">${esc(t.model_type || "")}</span>
        </div>
        ${t.prompt ? `<div style="font-size:10.5px;color:var(--text-muted);line-height:1.35;max-height:38px;overflow:hidden;text-overflow:ellipsis;">${esc(t.prompt)}</div>` : ""}
        ${Array.isArray(t.image_refs) && t.image_refs.length ? `<div class="take-sources-row" data-role="take-sources"></div>` : ""}
        <div style="display:flex;gap:5px;align-items:center;margin-top:auto;padding-top:4px;">
          <input type="text" class="gallery-new-anchor-label" value="${esc(defNewLabel)}" title="Name suffix when creating a new Anchor from this take" style="flex:1;min-width:90px;font-size:11px;font-family:monospace;padding:4px 6px;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:4px;">
          <button type="button" class="btn btn-sm" data-gtact="to-anchor" title="Create a new separate Anchor from this take" style="font-size:11px;padding:4px 8px;color:#38bdf8;border-color:rgba(56,189,248,0.4);white-space:nowrap;">+ New Anchor</button>
        </div>
        <div style="display:flex;gap:6px;">
          <button type="button" class="btn btn-sm ${isAct ? "" : "btn-primary"}" data-gtact="use" ${isAct ? "disabled" : ""} style="flex:1;font-size:11px;padding:4px 8px;">${isAct ? "✓ Currently Active" : `✓ Use for [${esc(aid)}]`}</button>
          <button type="button" class="btn btn-sm" data-gtact="delete" title="Delete this take from disk" style="font-size:11px;padding:4px 10px;color:#f87171;border-color:rgba(248,113,113,0.4);">🗑 Delete Take</button>
        </div>`;

      const chk = card.querySelector(".gallery-take-select-chk");
      let shiftClick = false;
      card.querySelector(".gallery-take-check-badge")?.addEventListener("click", (e) => e.stopPropagation());
      chk?.addEventListener("click", (e) => { e.stopPropagation(); shiftClick = Boolean(e.shiftKey); });
      chk?.addEventListener("change", () => {
        const on = Boolean(chk.checked), lo = shiftClick && lastGallerySelectIdx !== null ? Math.min(lastGallerySelectIdx, idx) : idx, hi = shiftClick && lastGallerySelectIdx !== null ? Math.max(lastGallerySelectIdx, idx) : idx;
        for (let k = lo; k <= hi; k++) { const tid = takes[k]?.take_id; if (tid) { if (on) selectedGalleryTakeIds.add(tid); else selectedGalleryTakeIds.delete(tid); } }
        lastGallerySelectIdx = idx; shiftClick = false; repaintCardSelections();
      });
      card.querySelector("img")?.addEventListener("click", (e) => {
        e.stopPropagation();
        openAnchorTakeLightbox(aid, idx);
      });
      card.querySelector("[data-gtact='use']")?.addEventListener("click", () => {
        activateAnchorTake(aid, t.take_id, t.seed, `#${idx + 1}`);
      });
      card.querySelector("[data-gtact='to-anchor']")?.addEventListener("click", () => {
        const lbl = card.querySelector(".gallery-new-anchor-label")?.value?.trim() || defNewLabel;
        promoteAnchorTakeToNewAnchor(aid, t.take_id, lbl, `#${idx + 1}`, true);
      });
      card.querySelector("[data-gtact='delete']")?.addEventListener("click", () => {
        deleteAnchorTake(aid, t.take_id, `#${idx + 1}`);
      });
      if (Array.isArray(t.image_refs) && t.image_refs.length) window.renderTakeSourcesSummary?.(card.querySelector("[data-role='take-sources']"), t.image_refs);
      grid.appendChild(card);
    });
  }

  async function fetchAnchorTakes(slug) {
    const s = slug || window.state?.currentSlug;
    if (!s) return {};
    try {
      const res = await fetch("/api/anchors/takes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: s }),
      });
      if (!res.ok) return anchorTakesCache;
      const data = await res.json();
      if (data?.anchor_takes) {
        anchorTakesCache = data.anchor_takes;
        if (window.state) window.state.anchorTakes = anchorTakesCache;
        renderAllAnchorTakeStrips();
        if (activeGalleryAid) renderAnchorTakesGalleryModal();
      }
    } catch (e) {}
    return anchorTakesCache;
  }

  function renderAllAnchorTakeStrips() {
    const slug = encodeURIComponent(window.state?.currentSlug || "");
    const slots = document.querySelectorAll?.("#anchorCardsContainer .anchor-takes-slot") || [];
    slots.forEach((slotEl) => {
      const aid = String(slotEl.dataset.aid || "").toUpperCase();
      const slotData = anchorTakesCache[aid];
      const takes = slotData?.takes || [];
      const activeId = slotData?.active_take_id || "";
      slotEl.innerHTML = "";
      if (!takes.length) return;

      const box = document.createElement("div");
      box.className = "anchor-takes-box";
      const hdr = document.createElement("div");
      hdr.style.cssText = "display:flex;justify-content:space-between;align-items:center;font-size:10.5px;font-weight:700;color:#c4b5fd;";
      hdr.innerHTML = `<span style="cursor:pointer;" title="Open full Takes Gallery for [${aid}]">🖼️ Takes Vault (${takes.length})</span><button type="button" class="btn-mini" data-tact="open-gallery" style="font-size:10px;padding:1px 7px;color:#38bdf8;border-color:rgba(56,189,248,0.4);">🔍 Open Gallery (${takes.length})</button>`;
      hdr.querySelector("span")?.addEventListener("click", () => openAnchorTakesGallery(aid));
      hdr.querySelector("[data-tact='open-gallery']")?.addEventListener("click", () => openAnchorTakesGallery(aid));
      box.appendChild(hdr);

      const strip = document.createElement("div");
      strip.className = "anchor-takes-strip";

      takes.forEach((t, idx) => {
        const isAct = t.take_id === activeId;
        const tile = document.createElement("div");
        tile.className = `anchor-take-tile${isAct ? " is-active" : ""}`;
        tile.dataset.takeId = t.take_id;
        const imgUrl = `/media/${slug}/anchor/${t.rel_path}?t=${t.created_at || ""}`;
        const esc = window.escapeHtml || ((x) => String(x || ""));
        tile.innerHTML = `
          <img src="${esc(imgUrl)}" alt="${esc(t.take_id)}" title="Click to open take #${idx + 1} (seed ${t.seed}${t.resolution ? `, ${t.resolution}` : ""}) in new tab">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:700;color:${isAct ? "#6ee7b7" : "#cbd5e1"};" title="${esc(t.resolution || "")}">#${idx + 1} • s${esc(t.seed)}</span>
            ${isAct ? `<span style="font-size:8.5px;padding:1px 4px;border-radius:3px;background:rgba(16,185,129,0.2);color:#6ee7b7;font-weight:700;">ACTIVE</span>` : (t.resolution ? `<span style="font-size:8.5px;color:var(--text-muted);">${esc(t.resolution)}</span>` : "")}
          </div>
          <div class="anchor-take-btns">
            ${!isAct ? `<button type="button" data-tact="use" title="Use this take as the active [${esc(aid)}] anchor image">✓ Use</button>` : ""}
            <button type="button" data-tact="to-anchor" title="Turn this take into a separate new Stage 1 Anchor">+ Anchor</button>
            <button type="button" data-tact="delete" title="Delete this take" style="color:#f87171;flex:0 0 22px;">🗑</button>
          </div>`;

        tile.querySelector("img")?.addEventListener("click", (e) => {
          e.stopPropagation();
          openAnchorTakeLightbox(aid, idx);
        });
        tile.querySelector("[data-tact='use']")?.addEventListener("click", () => {
          activateAnchorTake(aid, t.take_id, t.seed, `#${idx + 1}`);
        });
        tile.querySelector("[data-tact='to-anchor']")?.addEventListener("click", () => {
          promoteAnchorTakeToNewAnchor(aid, t.take_id, "", `#${idx + 1}`, false);
        });
        tile.querySelector("[data-tact='delete']")?.addEventListener("click", () => {
          deleteAnchorTake(aid, t.take_id, `#${idx + 1}`);
        });
        strip.appendChild(tile);
      });

      box.appendChild(strip);
      slotEl.appendChild(box);
    });
  }

  function buildDefaultCharLookPrompt(ent, look, eid, lid) {
    const traits = String(ent?.physical_traits || ent?.name || eid || "character").trim();
    const desc = String(look?.description || look?.name || lid || "").trim();
    return `Cinematic 16:9 character reference portrait of ${traits}${desc ? `, ${desc}` : ""}, studio lighting, sharp focus`;
  }

  function enhanceCharactersDetailPane() {
    const detailEl = document.getElementById("characterDetailPane");
    if (!detailEl) return;
    const slug = encodeURIComponent(window.state?.currentSlug || "");
    const esc = window.escapeHtml || ((x) => String(x || ""));

    // Make roster avatars clickable to open in new tab
    document.querySelectorAll?.("#characterRosterList .character-roster-card img")?.forEach((img) => {
      if (img.dataset.zoomBound) return;
      img.dataset.zoomBound = "1";
      img.style.cursor = "zoom-in";
      img.title = "Click to open avatar image in new tab";
      img.addEventListener("click", (e) => {
        e.stopPropagation();
        openImageInNewTab(img.currentSrc || img.getAttribute("src"));
      });
    });

    detailEl.querySelectorAll(".look-editor-card").forEach((card) => {
      if (card.querySelector(".char-gen-box")) return;
      const nameInp = card.querySelector(".input-look-name");
      const eid = nameInp?.dataset?.entity;
      const lid = nameInp?.dataset?.look;
      const ent = window.state?.charactersData?.[eid];
      const look = ent?.looks?.[lid];
      if (!eid || !lid || !ent || !look) return;

      // Make existing reference thumbnails clickable to open in new tab
      card.querySelectorAll("img").forEach((img) => {
        img.style.cursor = "zoom-in";
        img.title = "Click to open reference image in new tab";
        img.addEventListener("click", (e) => {
          e.stopPropagation();
          openImageInNewTab(img.currentSrc || img.getAttribute("src"));
        });
      });

      if (!look.gen_prompt) {
        look.gen_prompt = buildDefaultCharLookPrompt(ent, look, eid, lid);
      }

      const genBox = document.createElement("div");
      genBox.className = "char-gen-box";
      genBox.dataset.entity = eid;
      genBox.dataset.look = lid;
      genBox.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
          <span style="font-size:11px;font-weight:700;color:#38bdf8;">🎨 AI Reference Image Generator &amp; Takes</span>
          <button type="button" class="btn-mini" data-cact="autofill" title="Rebuild prompt from Character Physical Traits + Look Description" style="font-size:10px;padding:1px 7px;color:#93c5fd;">✨ Build from Traits &amp; Look</button>
        </div>
        <div class="anchor-card-settings-grid">
          <div class="form-group"><label>Model</label><select data-cfield="gen_renderer"></select></div>
          <div class="form-group"><label>Backend</label><select data-cfield="gen_backend"><option value="">Auto</option><option value="local">🖥️ Local</option><option value="fal">☁️ fal.ai</option></select></div>
          <div class="form-group"><label>Resolution</label><select data-cfield="gen_resolution"></select></div>
          <div class="form-group"><label>Seed</label><div style="display:flex;gap:3px;"><input type="number" data-cfield="gen_seed" value="${look.gen_seed ?? 1001}"><button type="button" class="btn-mini" data-cact="randseed" title="Randomize Seed">🎲</button></div></div>
        </div>
        <div class="anchor-model-caps" data-cfield="gen_caps" style="font-size:10px;color:#94a3b8;"></div>
        <textarea class="anchor-card-prompt" data-cfield="gen_prompt" rows="2" placeholder="Describe character reference frame...">${esc(look.gen_prompt || "")}</textarea>
        <input type="text" data-cfield="gen_negative_prompt" value="${esc(look.gen_negative_prompt || "")}" placeholder="Optional negative prompt (e.g. blurry, extra limbs)..." style="font-size:11px;padding:4px 6px;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:4px;">
        <div style="display:flex;gap:6px;align-items:center;">
          <input type="number" data-cfield="gen_count" min="1" max="99" step="1" title="Number of reference image takes to generate (1-99)" style="width:58px;font-size:11px;padding:4px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:4px;">
          <button type="button" class="btn btn-primary" data-cact="generate" style="flex:1;font-size:11.5px;padding:5px 10px;">⚡ Generate Reference Image(s)</button>
        </div>
        <div class="char-look-takes-slot"></div>`;

      const modelSel = genBox.querySelector("[data-cfield='gen_renderer']");
      const backendSel = genBox.querySelector("[data-cfield='gen_backend']");
      const resSel = genBox.querySelector("[data-cfield='gen_resolution']");
      const seedInp = genBox.querySelector("[data-cfield='gen_seed']");
      const promptTa = genBox.querySelector("[data-cfield='gen_prompt']");
      const negInp = genBox.querySelector("[data-cfield='gen_negative_prompt']");
      const cntSel = genBox.querySelector("[data-cfield='gen_count']");
      const genBtn = genBox.querySelector("[data-cact='generate']");
      const capsEl = genBox.querySelector("[data-cfield='gen_caps']");

      window.populateAnchorModelSelect?.(modelSel, look.gen_renderer ?? "");
      const syncCharModelCaps = () => {
        window.populateAnchorResSelect?.(resSel, look.gen_resolution || "", look.gen_renderer ?? "");
        const spec = window.getAnchorRendererSpec?.({ renderer: look.gen_renderer ?? "" }, {}) || {};
        if (capsEl) capsEl.textContent = window.describeAnchorModelCaps?.(spec) || "";
        negInp.placeholder = spec?.no_negative_prompt ? "Unused by this model (CFG=1.0)" : "Optional negative prompt (e.g. blurry, extra limbs)...";
      };
      syncCharModelCaps();
      backendSel.value = String(look.gen_renderer || "").startsWith("fal_") ? "fal" : (look.gen_backend || "");
      cntSel.value = String(look.gen_count || 1);
      const syncGenBtnText = () => {
        const n = parseInt(cntSel.value, 10) || 1;
        genBtn.textContent = n > 1 ? `⚡ Generate ${n} Ref Takes` : "⚡ Generate Reference Image";
      };
      syncGenBtnText();
      window.bindCharacterMentionAutocomplete?.(promptTa);

      const markCharChanged = () => {
        if (window.state?.projectData && window.state?.charactersData) {
          window.state.projectData.characters = window.state.charactersData;
        }
        window.markProjectDirty?.();
      };

      promptTa.addEventListener("input", () => { look.gen_prompt = promptTa.value; markCharChanged(); });
      negInp.addEventListener("input", () => {
        if (negInp.value.trim()) look.gen_negative_prompt = negInp.value; else delete look.gen_negative_prompt;
        markCharChanged();
      });
      seedInp.addEventListener("change", () => { look.gen_seed = parseInt(seedInp.value, 10) || 1001; markCharChanged(); });
      modelSel.addEventListener("change", () => {
        const v = modelSel.value;
        look.gen_renderer = v ? (/^\d+$/.test(v) ? parseInt(v, 10) : v) : null;
        if (v.startsWith("fal_")) { backendSel.value = "fal"; backendSel.disabled = true; } else backendSel.disabled = false;
        syncCharModelCaps();
        markCharChanged();
      });
      backendSel.addEventListener("change", () => {
        if (backendSel.value) look.gen_backend = backendSel.value; else delete look.gen_backend;
        markCharChanged();
      });
      resSel.addEventListener("change", () => {
        if (resSel.value) look.gen_resolution = resSel.value; else delete look.gen_resolution;
        markCharChanged();
      });
      cntSel.addEventListener("input", () => {
        const n = Math.max(1, Math.min(99, parseInt(cntSel.value, 10) || 1));
        look.gen_count = n;
        syncGenBtnText();
        markCharChanged();
      });
      genBox.querySelector("[data-cact='randseed']")?.addEventListener("click", () => {
        const s = Math.floor(Math.random() * 90000) + 1000;
        look.gen_seed = s;
        seedInp.value = s;
        markCharChanged();
      });
      genBox.querySelector("[data-cact='autofill']")?.addEventListener("click", () => {
        look.gen_prompt = buildDefaultCharLookPrompt(ent, look, eid, lid);
        promptTa.value = look.gen_prompt;
        markCharChanged();
      });
      genBtn.addEventListener("click", async () => {
        const cnt = Math.max(1, Math.min(99, parseInt(cntSel.value, 10) || 1));
        cntSel.value = String(cnt);
        look.gen_prompt = promptTa.value.trim() || buildDefaultCharLookPrompt(ent, look, eid, lid);
        look.gen_seed = parseInt(seedInp.value, 10) || 1001;
        look.gen_count = cnt;
        markCharChanged();
        await window.triggerRender?.(["--anchors-only", `CHAR:${eid}:${lid}:x${cnt}`]);
      });

      // Build unified Takes Gallery for this Look (all generated_takes + any uploaded image_refs)
      const takesSlot = genBox.querySelector(".char-look-takes-slot");
      const activeRefs = new Set(Array.isArray(look.image_refs) ? look.image_refs : []);
      const genTakes = Array.isArray(look.generated_takes) ? [...look.generated_takes] : [];
      const seenRels = new Set(genTakes.map((t) => t.rel_path));
      (look.image_refs || []).forEach((rp, i) => {
        if (rp && !seenRels.has(rp)) {
          seenRels.add(rp);
          genTakes.push({ take_id: `ref_${i + 1}`, rel_path: rp, seed: look.gen_seed || 1001, prompt: look.gen_prompt || "" });
        }
      });

      if (genTakes.length > 0) {
        const strip = document.createElement("div");
        strip.className = "anchor-takes-strip";
        genTakes.forEach((t, idx) => {
          const isUsed = activeRefs.has(t.rel_path);
          const tile = document.createElement("div");
          tile.className = `anchor-take-tile${isUsed ? " is-active" : ""}`;
          const imgUrl = `/media/${slug}/ref/${t.rel_path}?t=${t.created_at || ""}`;
          tile.innerHTML = `
            <img src="${esc(imgUrl)}" alt="${esc(t.take_id)}" title="Click to open character take #${idx + 1} in new tab">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span style="font-weight:700;color:${isUsed ? "#6ee7b7" : "#cbd5e1"};">#${idx + 1}${t.seed ? ` • s${esc(t.seed)}` : ""}</span>
              ${isUsed ? `<span style="font-size:8.5px;padding:1px 4px;border-radius:3px;background:rgba(16,185,129,0.2);color:#6ee7b7;font-weight:700;">IN USE</span>` : ""}
            </div>
            <div class="anchor-take-btns">
              <button type="button" data-ctact="toggle" title="${isUsed ? "Remove from active look references" : "Use in this look's active references"}">${isUsed ? "✓ Active" : "Use"}</button>
              <button type="button" data-ctact="mask" title="Mask or extract subject from this take">✂️</button>
              <button type="button" data-ctact="to-anchor" title="Turn this character reference take into a separate Stage 1 Anchor">+ Anchor</button>
              <button type="button" data-ctact="delete" title="Delete this take" style="color:#f87171;flex:0 0 22px;">🗑</button>
            </div>`;
          tile.querySelector("img")?.addEventListener("click", (e) => {
            e.stopPropagation();
            ensureGalleryModalElement();
            openCharTakeLightbox(eid, lid, idx, () =>
              genTakes.map((ct, cIdx) => ({
                take_id: ct.take_id,
                index: cIdx,
                title: `[${ent.name || eid} — ${look.name || lid}] Take #${cIdx + 1} of ${genTakes.length}`,
                isActive: activeRefs.has(ct.rel_path),
                activeLabel: "✓ IN USE",
                url: `/media/${slug}/ref/${ct.rel_path}?t=${ct.created_at || ""}`,
                seed: ct.seed,
                resolution: ct.resolution || "",
                model_type: ct.model_type || "",
                prompt: ct.prompt || "",
              }))
            );
          });
          const runCharTakeApi = async (subAction) => {
            const res = await fetch("/api/anchors/char-take-action", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ slug: window.state.currentSlug, sub_action: subAction, entity_id: eid, look_id: lid, rel_path: t.rel_path, take_id: t.take_id }),
            });
            return res.json().catch(() => ({}));
          };
          tile.querySelector("[data-ctact='mask']")?.addEventListener("click", (ev) => {
            ev.stopPropagation();
            window.openCharMaskModal?.(eid, lid, t.rel_path);
          });
          tile.querySelector("[data-ctact='toggle']")?.addEventListener("click", async () => {
            const data = await runCharTakeApi("toggle_use");
            if (data.success) {
              window.state.charactersData = data.characters;
              await window.loadProjectCharacters?.(window.state.currentSlug, true);
            }
          });
          tile.querySelector("[data-ctact='to-anchor']")?.addEventListener("click", async () => {
            const data = await runCharTakeApi("to_anchor");
            if (data.success && data.new_anchor) {
              if (Array.isArray(window.state.projectData?.anchors) && !window.state.projectData.anchors.some((a) => a.filename === data.new_anchor.filename)) {
                window.state.projectData.anchors.push(data.new_anchor);
              }
              await window.refreshProjectAssets?.(true);
              await fetchAnchorTakes(window.state.currentSlug);
              window.focusAnchorInStudio?.(data.new_aid);
              window.showToast?.(`✨ Created new Stage 1 Anchor [${data.new_aid}] from character take #${idx + 1}`);
            }
          });
          tile.querySelector("[data-ctact='delete']")?.addEventListener("click", async () => {
            if (!confirm(`Delete character reference take #${idx + 1}?`)) return;
            const data = await runCharTakeApi("delete");
            if (data.success) {
              window.state.charactersData = data.characters;
              await window.loadProjectCharacters?.(window.state.currentSlug, true);
              window.showToast?.(`🗑 Deleted character take #${idx + 1}`);
            }
          });
          strip.appendChild(tile);
        });
        takesSlot.appendChild(strip);
      }

      card.appendChild(genBox);
    });
  }

  function bindInspectorAnchorThumbZoom() {
    ["anchorTriggerThumb", "endAnchorTriggerThumb"].forEach((id) => {
      const img = document.getElementById(id);
      if (!img || img.dataset.zoomBound) return;
      img.dataset.zoomBound = "1";
      img.style.cursor = "zoom-in";
      img.title = "Click thumbnail to open anchor image in new tab";
      img.addEventListener("click", (e) => {
        const src = img.currentSrc || img.getAttribute("src");
        if (src && img.style.display !== "none") {
          e.stopPropagation();
          openImageInNewTab(src);
        }
      });
    });
  }

  function initTakesAndCharGenHooks() {
    bindInspectorAnchorThumbZoom();

    const detailPane = document.getElementById("characterDetailPane");
    if (detailPane && typeof MutationObserver !== "undefined") {
      const obs = new MutationObserver(() => enhanceCharactersDetailPane());
      obs.observe(detailPane, { childList: true });
    }

    if (typeof window.refreshProjectAssets === "function" && !window.refreshProjectAssets._takesHooked) {
      const origRefresh = window.refreshProjectAssets;
      const wrappedRefresh = async function () {
        const r = await origRefresh.apply(this, arguments);
        await fetchAnchorTakes(window.state?.currentSlug);
        if (!window.state?.isDirty) enhanceCharactersDetailPane();
        return r;
      };
      wrappedRefresh._takesHooked = true;
      window.refreshProjectAssets = wrappedRefresh;
    }

    window.addEventListener("projectLoaded", (ev) => {
      fetchAnchorTakes(ev?.detail?.slug || window.state?.currentSlug);
      setTimeout(enhanceCharactersDetailPane, 100);
    });
    enhanceCharactersDetailPane();
  }

  window.fetchAnchorTakes = fetchAnchorTakes;
  window.renderAllAnchorTakeStrips = renderAllAnchorTakeStrips;
  window.openAnchorTakesGallery = openAnchorTakesGallery;
  window.closeAnchorTakesGallery = closeAnchorTakesGallery;
  window.openAnchorTakeLightbox = openAnchorTakeLightbox;
  window.closeTakeLightbox = closeTakeLightbox;
  window.stepTakeLightbox = stepTakeLightbox;
  window.enhanceCharactersDetailPane = enhanceCharactersDetailPane;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initTakesAndCharGenHooks);
  else initTakesAndCharGenHooks();
})();
