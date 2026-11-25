/**
 * Music Video Generation Studio - Direct Per-Scene Reference Image Picker & LTX-2.5 Controls
 *
 * Lets users attach/upload/pick/reorder per-scene `sc.image_refs` for LTX-2.5 MSR (#19),
 * LTX-2.5 Ingredients (#20/#21), and MiniMax H3 Ref2VA (#9-#13), plus toggle MSR
 * background slot options in both Scene Inspector and the Vertical Scenes Tab.
 */
(function () {
  "use strict";

  function normalizeRel(rp) {
    return String(rp || "").replace(/\\/g, "/").replace(/^\/+/, "");
  }

  function encodeSubpath(rp) {
    return normalizeRel(rp).split("/").filter(Boolean).map(encodeURIComponent).join("/");
  }

  function buildRefUrl(rp) {
    const slug = encodeURIComponent(window.state?.currentSlug || "");
    return `/media/${slug}/ref/${encodeSubpath(rp)}`;
  }

  function resolveSceneVideoSpec(sc, passNum = null) {
    const mRoot = window.state?.models || {};
    const defRid = String(window.state?.projectData?.models?.default_video_renderer || 1);
    const rawR = passNum === 1 ? (sc?.renderer_m ?? sc?.renderer) : (passNum === 2 ? (sc?.renderer_f ?? sc?.renderer) : sc?.renderer);
    const rid = (rawR !== null && rawR !== undefined && String(rawR).trim() !== "") ? String(rawR) : defRid;
    const spec = {
      ...(mRoot.video_renderers?.[rid] || mRoot.fal_video_renderers?.[rid] || mRoot.minimax_video_renderers?.[rid] || {}),
      ...(window.state?.projectData?.models?.video_renderers?.[rid] || {}),
    };
    return { rid, spec };
  }

  function getVideoRefMode(rspec) {
    if (!rspec || typeof rspec !== "object") return "";
    const explicit = String(rspec.ref_mode || "").trim().toLowerCase();
    if (explicit) return explicit;
    const mtype = String(rspec.model_type || "").toLowerCase();
    if (mtype.includes("ltx2")) return mtype.includes("msr") ? "ltx2_msr" : "";
    if (rspec.supports_image_refs || String(rspec.frames_formula || "").toLowerCase().includes("ref2va") || mtype.includes("ref2va") || mtype.includes("fasth3") || mtype.includes("singularity")) {
      return "h3_ref2va";
    }
    return "";
  }
  window.getVideoRefMode = getVideoRefMode;

  function formatVideoModelBadge(m, isCloud = false) {
    const mode = getVideoRefMode(m);
    if (mode === "ltx2_msr") return " [LTX-MSR]";
    if (mode === "ltx2_ingredients") return " [Ingredients]";
    if (mode === "h3_ref2va") return isCloud ? " [Ref2V]" : " [Ref2VA]";
    return "";
  }
  window.formatVideoModelBadge = formatVideoModelBadge;

  function gatherPickableRefs(excludePaths) {
    const seen = new Set((excludePaths || []).map((x) => normalizeRel(x)));
    const out = [];
    const add = (rp, grp) => {
      const clean = normalizeRel(rp);
      if (!clean || seen.has(clean)) return;
      seen.add(clean);
      out.push({ rel_path: clean, group: grp, label: clean.split("/").pop() || clean });
    };
    (window.state?.assets?.anchor_source_files || []).forEach((rp) => add(rp, "Uploaded Sources"));
    const ancDir = normalizeRel(window.state?.projectData?.project?.directories?.anchors || "work/anchors").replace(/\/+$/, "");
    Object.values(window.state?.assets?.anchors || {}).forEach((inf) => {
      if (inf?.filename && inf.is_anchor_dir !== false) add(`${ancDir}/${inf.filename}`, "Project Anchors");
    });
    (window.state?.assets?.reference_files || []).forEach((f) => add(String(f).includes("/") ? f : `references/${f}`, "Reference Library"));
    const chars = window.state?.charactersData || window.state?.projectData?.characters || {};
    Object.values(chars).forEach((c) => Object.values(c?.looks || {}).forEach((l) => (l?.image_refs || []).forEach((rp) => add(rp, "Characters"))));
    return out;
  }

  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error(`Failed to read ${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  async function uploadSceneRefs(files, sc, maxRefs, onDone) {
    const slug = window.state?.currentSlug;
    if (!slug || !sc || sc.locked) return;
    const valid = Array.from(files || []).filter((f) => !f.type || f.type.startsWith("image/") || /\.(jpe?g|png|webp)$/i.test(f.name || ""));
    if (!valid.length) { window.showToast?.("Select JPG, PNG, or WEBP image(s) to attach."); return; }
    const cur = Array.isArray(sc.image_refs) ? [...sc.image_refs] : [];
    const rem = Math.max(0, maxRefs - cur.length);
    if (rem === 0) { window.showToast?.(`Maximum ${maxRefs} reference image slots reached for this model.`); return; }
    const added = [];
    for (const file of valid.slice(0, rem)) {
      const dataUrl = await readFileAsDataUrl(file);
      const resp = await fetch("/api/anchors/upload-source", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, aid: sc.id || "SCENE", filename: file.name, data_url: dataUrl }),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok && data.success && data.rel_path) {
        if (!cur.some((x) => normalizeRel(x) === normalizeRel(data.rel_path))) {
          cur.push(data.rel_path);
          added.push(data.rel_path);
        }
        if (window.state?.assets) window.state.assets.anchor_source_files = data.anchor_source_files || window.state.assets.anchor_source_files;
      } else {
        window.showToast?.(`❌ Upload failed (${file.name}): ${data.error || `HTTP ${resp.status}`}`);
      }
    }
    if (added.length) {
      sc.image_refs = cur;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Attach ${added.length} Ref Image(s) to ${sc.id}`);
      window.updatePromptReferenceTray?.(sc);
      onDone?.();
      window.showToast?.(`📎 Attached ${added.length} reference image(s) to ${sc.id}`);
    }
  }

  function syncStartAnchorMsrHint(mode, sc) {
    const lbl = document.querySelector('#anchorPickerGroup .form-label-row label');
    if (!lbl) return;
    let hint = document.getElementById("anchorMsrBgHint");
    if (mode === "ltx2_msr") {
      if (!hint) {
        hint = document.createElement("span");
        hint.id = "anchorMsrBgHint";
        hint.style.cssText = "font-size:10px;font-weight:600;margin-left:5px;";
        lbl.appendChild(hint);
      }
      const useBg = sc?.msr_use_start_as_bg !== false;
      hint.style.color = useBg ? "#38bdf8" : "#94a3b8";
      hint.textContent = useBg ? "(BG Slot 1 — MSR)" : "(Ignored — MSR Subjects Only)";
    } else if (hint) {
      hint.remove();
    }
  }

  function paintPanel(hostEl, sc, { compact = false, onMutate = null } = {}) {
    if (!hostEl) return;
    if (!sc) { hostEl.innerHTML = ""; return; }
    const { spec } = resolveSceneVideoSpec(sc, null);
    const mode = getVideoRefMode(spec);
    if (!compact) syncStartAnchorMsrHint(mode, sc);
    if (!mode) { hostEl.innerHTML = ""; return; }

    const esc = window.escapeHtml || ((x) => String(x || ""));
    const maxRefs = mode === "ltx2_msr" ? 4 : (mode === "ltx2_ingredients" ? (Number(spec.max_image_refs) || 6) : 9);
    const refs = Array.isArray(sc.image_refs) ? sc.image_refs.filter(Boolean) : [];
    const pickChoices = gatherPickableRefs(refs);
    const titleTxt = mode === "ltx2_msr"
      ? `🎭 Direct Scene Subject Refs (${refs.length}/4 Subject Slots)`
      : (mode === "ltx2_ingredients" ? `🧩 Ingredients Reference Sheet (${refs.length}/${maxRefs} Pieces)` : `🖼️ Scene Reference Images (${refs.length}/${maxRefs})`);
    const lockedAttr = sc.locked ? "disabled" : "";
    function sceneSubjRefsAllPremasked(sc) {
      const chars = window.state?.charactersData || window.state?.projectData?.characters || {};
      const maskedSet = new Set();
      Object.values(chars).forEach((ent) => Object.values(ent?.looks || {}).forEach((lk) => {
        Object.entries(lk?.ref_masks || {}).forEach(([k, m]) => { if (m && m.premasked_white !== false) maskedSet.add(normalizeRel(k)); });
      }));
      if (!maskedSet.size) return false;
      const subj = [...(Array.isArray(sc.image_refs) ? sc.image_refs : [])];
      const rawP = String(sc.prompt || sc.prompt_m || "").toLowerCase();
      Object.values(chars).forEach((ent) => Object.entries(ent?.looks || {}).forEach(([lid, lk]) => {
        const tok = String(lk?.tag || `@${lid}`).toLowerCase();
        const base = `@${String(ent?.id || "").toLowerCase()}`;
        if ((tok && rawP.includes(tok)) || (base && lid === ent?.default_look && rawP.includes(base))) {
          (lk?.image_refs || []).forEach((rp) => subj.push(rp));
        }
      }));
      const clean = Array.from(new Set(subj.map(normalizeRel).filter(Boolean)));
      return clean.length > 0 && clean.every((rp) => maskedSet.has(rp));
    }

    const allPremasked = mode === "ltx2_msr" ? sceneSubjRefsAllPremasked(sc) : false;
    const rmbgChecked = allPremasked ? (sc.msr_remove_bg === true) : (sc.msr_remove_bg !== false);
    const msrRow = mode === "ltx2_msr" ? `
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;font-size:11px;color:#cbd5e1;padding-top:2px;">
        <label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;" title="Send Start Anchor as background reference image 1 (KI) or use subject refs only (I)">
          <input type="checkbox" class="sc-msr-bg-chk" ${sc.msr_use_start_as_bg !== false ? "checked" : ""} ${lockedAttr}> Use Start Anchor as Background (KI)
        </label>
        <label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;" title="Automatically strip backgrounds behind MSR subject/object references">
          <input type="checkbox" class="sc-msr-rmbg-chk" ${rmbgChecked ? "checked" : ""} ${lockedAttr}> Remove Subject Backgrounds
        </label>
        ${allPremasked && sc.msr_remove_bg !== true ? `<span class="msr-premasked-pill" style="font-size:10px;color:#34d399;background:rgba(16,185,129,0.14);border:1px solid rgba(16,185,129,0.35);padding:1px 6px;border-radius:4px;">✓ Clean cutouts active (MSR u2net auto-skipped)</span>` : ""}
      </div>` : "";

    hostEl.innerHTML = `
      <div class="anchor-source-refs-panel${compact ? " is-compact" : ""}" style="margin-top:${compact ? "4px" : "6px"};">
        <div class="anchor-src-toolbar">
          <span class="anchor-src-title">${esc(titleTxt)}</span>
          <button type="button" class="btn-mini sc-ref-upload-btn" title="Upload non-character prop, object, or reference image(s)" ${lockedAttr}>📁 + Ref Image</button>
          <input type="file" class="sc-ref-file-inp" accept=".jpg,.jpeg,.png,.webp,image/*" multiple style="display:none;">
          ${pickChoices.length ? `<select class="anchor-src-pick-select sc-ref-pick-sel" ${lockedAttr}><option value="">📎 Pick Existing...</option></select>` : ""}
          ${refs.length ? `<button type="button" class="btn-mini sc-ref-clear-btn" ${lockedAttr}>✕ Clear</button>` : ""}
        </div>
        ${msrRow}
        <div class="anchor-src-body"></div>
      </div>`;

    const rerender = () => {
      paintPanel(hostEl, sc, { compact, onMutate });
      window.updatePromptReferenceTray?.(sc);
      onMutate?.();
    };
    const fileInp = hostEl.querySelector(".sc-ref-file-inp");
    hostEl.querySelector(".sc-ref-upload-btn")?.addEventListener("click", () => { if (!sc.locked) { fileInp.value = ""; fileInp.click(); } });
    fileInp?.addEventListener("change", (e) => { if (e.target.files?.length) uploadSceneRefs(e.target.files, sc, maxRefs, rerender); });

    const pickSel = hostEl.querySelector(".sc-ref-pick-sel");
    if (pickSel) {
      const byGrp = {};
      pickChoices.forEach((it) => { (byGrp[it.group] = byGrp[it.group] || []).push(it); });
      Object.entries(byGrp).forEach(([grp, list]) => {
        const og = document.createElement("optgroup");
        og.label = grp;
        list.forEach((it) => og.appendChild(new Option(it.label, it.rel_path)));
        pickSel.appendChild(og);
      });
      pickSel.onchange = () => {
        const val = pickSel.value;
        pickSel.value = "";
        if (!val || sc.locked) return;
        const next = Array.isArray(sc.image_refs) ? [...sc.image_refs] : [];
        if (next.length >= maxRefs) { window.showToast?.(`Maximum ${maxRefs} reference slots reached.`); return; }
        if (!next.includes(val)) next.push(val);
        sc.image_refs = next;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Attach Ref Image to ${sc.id}`);
        rerender();
      };
    }

    hostEl.querySelector(".sc-ref-clear-btn")?.addEventListener("click", () => {
      if (sc.locked) return;
      delete sc.image_refs;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Clear Direct Refs on ${sc.id}`);
      rerender();
    });
    hostEl.querySelector(".sc-msr-bg-chk")?.addEventListener("change", (e) => {
      if (sc.locked) return;
      if (e.target.checked) delete sc.msr_use_start_as_bg; else sc.msr_use_start_as_bg = false;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Toggle ${sc.id} MSR Background Anchor`);
      rerender();
    });
    hostEl.querySelector(".sc-msr-rmbg-chk")?.addEventListener("change", (e) => {
      if (sc.locked) return;
      if (allPremasked) { if (e.target.checked) sc.msr_remove_bg = true; else delete sc.msr_remove_bg; }
      else { if (e.target.checked) delete sc.msr_remove_bg; else sc.msr_remove_bg = false; }
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Toggle ${sc.id} MSR Background Removal`);
      rerender();
    });

    const body = hostEl.querySelector(".anchor-src-body");
    if (!refs.length) {
      body.innerHTML = `<div class="anchor-src-empty-hint">Optional: attach props, objects, or pre-made reference sheets (up to ${maxRefs}), or use <code>@character</code> tags in the prompt.</div>`;
      return;
    }
    const strip = document.createElement("div");
    strip.className = "anchor-src-strip";
    refs.forEach((rp, i) => {
      const chip = document.createElement("div");
      chip.className = "anchor-src-chip";
      const base = String(rp).split("/").pop() || rp;
      const tagTxt = mode === "ltx2_msr" ? `Subject ${i + 1}` : (mode === "ltx2_ingredients" ? `Cell ${i + 1}` : `<image${i + 1}>`);
      chip.innerHTML = `
        <img src="${esc(buildRefUrl(rp))}" alt="${esc(base)}" title="${esc(rp)}">
        <div class="anchor-src-chip-meta">
          <span class="anchor-src-origin">${esc(tagTxt)}</span>
          <span class="anchor-src-fname" title="${esc(rp)}">${esc(base)}</span>
        </div>
        ${i > 0 ? `<button type="button" class="btn-mini sc-ref-mv-l" style="padding:0 4px;font-size:10px;" title="Move earlier" ${lockedAttr}>‹</button>` : ""}
        ${i < refs.length - 1 ? `<button type="button" class="btn-mini sc-ref-mv-r" style="padding:0 4px;font-size:10px;" title="Move later" ${lockedAttr}>›</button>` : ""}
        <button type="button" class="anchor-src-rm-btn sc-ref-rm" title="Remove ${esc(base)}" ${lockedAttr}>×</button>`;
      chip.querySelector("img")?.addEventListener("click", (e) => { e.stopPropagation(); window.openImageInNewTab?.(buildRefUrl(rp)); });
      const swap = (j) => {
        if (sc.locked) return;
        const arr = [...sc.image_refs];
        [arr[i], arr[j]] = [arr[j], arr[i]];
        sc.image_refs = arr;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Reorder Refs on ${sc.id}`);
        rerender();
      };
      chip.querySelector(".sc-ref-mv-l")?.addEventListener("click", () => swap(i - 1));
      chip.querySelector(".sc-ref-mv-r")?.addEventListener("click", () => swap(i + 1));
      chip.querySelector(".sc-ref-rm")?.addEventListener("click", () => {
        if (sc.locked) return;
        const next = (sc.image_refs || []).filter((_, j) => j !== i);
        if (next.length) sc.image_refs = next; else delete sc.image_refs;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Remove Ref Image from ${sc.id}`);
        rerender();
      });
      strip.appendChild(chip);
    });
    body.appendChild(strip);
  }

  window.renderSceneRefPickerUI = function (sc) {
    paintPanel(document.getElementById("sceneDirectRefsHost"), sc, { compact: false });
  };
  window.appendSceneCardRefPicker = function (ctrls, sc, idx, applyFieldSideEffects) {
    const host = document.createElement("div");
    paintPanel(host, sc, { compact: true, onMutate: () => applyFieldSideEffects?.(idx, sc) });
    if (host.innerHTML) ctrls.appendChild(host);
  };
})();
