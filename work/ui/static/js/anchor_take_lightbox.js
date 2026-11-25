/**
 * Music Video Generation Studio - Fullscreen Take Lightbox Modal
 *
 * Supports keyboard/button cycling between Anchor takes or Character Look takes
 * without exiting fullscreen, plus single-take activation, promotion, and deletion.
 */
(function () {
  "use strict";

  let lightboxState = null;

  function ensureLightboxModalElement() {
    let el = document.getElementById("anchorTakeLightboxModal");
    if (el) return el;
    el = document.createElement("div");
    el.id = "anchorTakeLightboxModal";
    el.className = "take-lightbox-overlay";
    el.addEventListener("click", (e) => {
      if (e.target === el || e.target.classList?.contains("take-lightbox-stage")) closeTakeLightbox();
    });
    document.addEventListener("keydown", (e) => {
      if (el.style.display !== "flex") return;
      if (e.key === "ArrowLeft") { e.preventDefault(); stepTakeLightbox(-1); return; }
      if (e.key === "ArrowRight") { e.preventDefault(); stepTakeLightbox(1); return; }
      if (e.key === "Escape") { e.preventDefault(); closeTakeLightbox(); }
    });
    document.body.appendChild(el);
    return el;
  }

  function closeTakeLightbox() {
    lightboxState = null;
    const el = document.getElementById("anchorTakeLightboxModal");
    if (el) el.style.display = "none";
    if (document.fullscreenElement && document.fullscreenElement === el) {
      document.exitFullscreen?.().catch(() => {});
    }
  }

  function stepTakeLightbox(delta) {
    if (!lightboxState) return;
    const items = lightboxState.getItems();
    if (!items || items.length < 2) return;
    lightboxState.index = (lightboxState.index + delta + items.length) % items.length;
    renderTakeLightbox();
  }

  function openAnchorTakeLightbox(aid, initialIndex = 0) {
    const cleanAid = (window.extractAnchorId?.(aid || "") || String(aid || "")).toUpperCase();
    if (!cleanAid) return;
    lightboxState = {
      kind: "anchor",
      aid: cleanAid,
      index: Math.max(0, parseInt(initialIndex, 10) || 0),
      getItems: () => {
        const slug = encodeURIComponent(window.state?.currentSlug || "");
        const slot = window.anchorTakeActions?.getSlot?.(cleanAid) || { active_take_id: "", takes: [] };
        const takes = slot.takes || [];
        return takes.map((t, i) => ({
          take_id: t.take_id,
          index: i,
          title: `[${cleanAid}] Take #${i + 1} of ${takes.length}`,
          isActive: t.take_id === slot.active_take_id,
          activeLabel: "✓ ACTIVE ANCHOR",
          url: `/media/${slug}/anchor/${t.rel_path}?t=${t.created_at || ""}`,
          seed: t.seed,
          resolution: t.resolution || "default",
          model_type: t.model_type || "",
          prompt: t.prompt || "",
          image_refs: Array.isArray(t.image_refs) ? t.image_refs : [],
        }));
      },
    };
    const el = ensureLightboxModalElement();
    el.style.display = "flex";
    renderTakeLightbox();
  }

  function openCharTakeLightbox(eid, lid, initialIndex = 0, getCharTakesFn = null) {
    lightboxState = {
      kind: "char",
      eid,
      lid,
      index: Math.max(0, parseInt(initialIndex, 10) || 0),
      getItems: getCharTakesFn || (() => []),
    };
    const el = ensureLightboxModalElement();
    el.style.display = "flex";
    renderTakeLightbox();
  }

  function renderTakeLightbox() {
    const el = document.getElementById("anchorTakeLightboxModal");
    if (!el || !lightboxState || el.style.display !== "flex") return;
    const items = lightboxState.getItems();
    if (!items || items.length === 0) {
      closeTakeLightbox();
      return;
    }
    if (lightboxState.index >= items.length) lightboxState.index = items.length - 1;
    if (lightboxState.index < 0) lightboxState.index = 0;
    const cur = items[lightboxState.index];
    const esc = window.escapeHtml || ((x) => String(x || ""));
    const isNativeFs = Boolean(document.fullscreenElement === el);

    el.innerHTML = `
      <div class="take-lightbox-topbar">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <span id="lightboxTakeTitle" style="font-size:15px;font-weight:700;color:#f8fafc;">${esc(cur.title)}</span>
          ${cur.isActive ? `<span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;background:rgba(16,185,129,0.22);color:#6ee7b7;border:1px solid rgba(16,185,129,0.5);">${esc(cur.activeLabel || "✓ ACTIVE")}</span>` : ""}
          <span style="font-size:12px;color:#cbd5e1;"><strong>Seed:</strong> <code>${esc(cur.seed ?? "—")}</code></span>
          ${cur.resolution ? `<span style="font-size:12px;color:#cbd5e1;"><strong>Res:</strong> ${esc(cur.resolution)}</span>` : ""}
          ${cur.model_type ? `<span style="font-size:12px;color:#7dd3fc;font-family:monospace;">${esc(cur.model_type)}</span>` : ""}
          <span style="font-size:11px;color:var(--text-muted);">(Use ← / → arrow keys to switch takes)</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          ${lightboxState.kind === "anchor" ? `
            <button type="button" class="btn btn-sm ${cur.isActive ? "" : "btn-primary"}" id="btnLightboxUseTake" ${cur.isActive ? "disabled" : ""} style="font-size:11px;padding:4px 10px;">${cur.isActive ? "✓ Active Anchor" : `✓ Use for [${esc(lightboxState.aid)}]`}</button>
            <button type="button" class="btn btn-sm" id="btnLightboxToAnchor" style="font-size:11px;padding:4px 10px;color:#38bdf8;border-color:rgba(56,189,248,0.4);">+ New Anchor</button>
            <button type="button" class="btn btn-sm" id="btnLightboxDeleteTake" style="font-size:11px;padding:4px 10px;color:#f87171;border-color:rgba(248,113,113,0.4);">🗑 Delete Take</button>
          ` : ""}
          <button type="button" class="btn btn-sm" id="btnLightboxNativeFs" title="Toggle native browser fullscreen" style="font-size:11px;padding:4px 10px;">${isNativeFs ? "🗗 Exit Fullscreen" : "⛶ Fullscreen"}</button>
          <button type="button" class="btn btn-sm" id="btnLightboxNewTab" title="Open raw image in new browser tab" style="font-size:11px;padding:4px 10px;">↗ New Tab</button>
          <button type="button" class="btn btn-sm" id="btnLightboxClose" style="font-size:13px;padding:3px 10px;">✕ Close</button>
        </div>
      </div>
      <div class="take-lightbox-stage">
        <button type="button" class="take-lightbox-nav is-prev" id="btnLightboxPrev" ${items.length < 2 ? "disabled" : ""} title="Previous Take (Left Arrow)">‹</button>
        <img class="take-lightbox-img" id="lightboxMainImage" src="${esc(cur.url)}" alt="${esc(cur.title)}">
        <button type="button" class="take-lightbox-nav is-next" id="btnLightboxNext" ${items.length < 2 ? "disabled" : ""} title="Next Take (Right Arrow)">›</button>
      </div>
      <div class="take-lightbox-footer">
        ${cur.prompt ? `<div style="font-size:11.5px;color:#cbd5e1;text-align:center;font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(cur.prompt)}">${esc(cur.prompt)}</div>` : ""}
        <div id="lightboxSourcesRow" class="take-sources-row" style="justify-content:center;"></div>
        <div class="take-lightbox-strip">
          ${items.map((it, idx) => `
            <div class="take-lightbox-thumb${idx === lightboxState.index ? " is-current" : ""}${it.isActive ? " is-active-take" : ""}" data-lbidx="${idx}" title="Take #${idx + 1} (seed ${esc(it.seed ?? "")})">
              <img src="${esc(it.url)}" alt="Take #${idx + 1}">
            </div>
          `).join("")}
        </div>
      </div>`;

    el.querySelector("#btnLightboxPrev")?.addEventListener("click", (e) => { e.stopPropagation(); stepTakeLightbox(-1); });
    window.renderTakeSourcesSummary?.(el.querySelector("#lightboxSourcesRow"), cur.image_refs, { label: `Source Images Used (${(cur.image_refs || []).length}):` });
    el.querySelector("#btnLightboxNext")?.addEventListener("click", (e) => { e.stopPropagation(); stepTakeLightbox(1); });
    el.querySelector("#btnLightboxClose")?.addEventListener("click", closeTakeLightbox);
    el.querySelector("#btnLightboxNewTab")?.addEventListener("click", () => window.openImageInNewTab?.(cur.url));
    el.querySelector("#btnLightboxNativeFs")?.addEventListener("click", () => {
      if (document.fullscreenElement === el) {
        document.exitFullscreen?.().then(() => renderTakeLightbox()).catch(() => {});
      } else {
        el.requestFullscreen?.().then(() => renderTakeLightbox()).catch(() => {});
      }
    });
    el.querySelectorAll(".take-lightbox-thumb").forEach((th) => {
      th.addEventListener("click", (e) => {
        e.stopPropagation();
        lightboxState.index = parseInt(th.dataset.lbidx, 10) || 0;
        renderTakeLightbox();
      });
    });
    if (lightboxState.kind === "anchor") {
      const aid = lightboxState.aid;
      el.querySelector("#btnLightboxUseTake")?.addEventListener("click", async () => {
        await window.anchorTakeActions?.activate?.(aid, cur.take_id, cur.seed, `#${lightboxState.index + 1}`);
        renderTakeLightbox();
      });
      el.querySelector("#btnLightboxToAnchor")?.addEventListener("click", async () => {
        await window.anchorTakeActions?.promote?.(aid, cur.take_id, "", `#${lightboxState.index + 1}`, true);
        renderTakeLightbox();
      });
      el.querySelector("#btnLightboxDeleteTake")?.addEventListener("click", async () => {
        const deleted = await window.anchorTakeActions?.deleteSingle?.(aid, cur.take_id, `#${lightboxState.index + 1}`);
        if (deleted) renderTakeLightbox();
      });
    }
  }

  window.openAnchorTakeLightbox = openAnchorTakeLightbox;
  window.openCharTakeLightbox = openCharTakeLightbox;
  window.closeTakeLightbox = closeTakeLightbox;
  window.stepTakeLightbox = stepTakeLightbox;
})();
