/**
 * Music Video Generation Studio - Direct Anchor Source Image Picker, Inspector & Uploader
 *
 * Shows every source image provided to each scene anchor (whether attached directly,
 * picked from existing project/anchor assets, or auto-resolved from @character / shared
 * refs), supports removing any source image, managing uploaded source files on disk,
 * and displaying per-take source provenance across Anchor Cards, Gallery, Lightbox & Scenes.
 */
(function () {
  "use strict";

  const DEFAULT_MAX_ANCHOR_REFS = 4;
  const openLibraryAids = new Set();

  function getAnchorRendererSpec(anchorObj, opts = {}) {
    const rawRid = (anchorObj?.renderer !== undefined && anchorObj?.renderer !== null && anchorObj?.renderer !== "")
      ? anchorObj.renderer
      : (opts?.renderer !== undefined && opts?.renderer !== null && opts?.renderer !== ""
        ? opts.renderer
        : (window.state?.projectData?.models?.default_anchor_renderer || 3));
    const rid = String(rawRid);
    return window.state?.models?.anchor_renderers?.[rid] || window.state?.models?.fal_anchor_renderers?.[rid] || {};
  }

  function getMaxAnchorRefs(anchorObj, opts = {}) {
    const rspec = getAnchorRendererSpec(anchorObj, opts);
    return Math.max(1, Number(rspec?.max_image_refs) || DEFAULT_MAX_ANCHOR_REFS);
  }
  function describeAnchorModelCaps(modelIdOrSpec) {
    const spec = (typeof modelIdOrSpec === "object" && modelIdOrSpec) ? modelIdOrSpec : getAnchorRendererSpec({ renderer: modelIdOrSpec }, {});
    if (!spec || !spec.model_type) return "";
    const parts = [`Up to ${Math.max(1, Number(spec.max_image_refs) || DEFAULT_MAX_ANCHOR_REFS)} refs`];
    const ts = String(spec.ref_tag_style || "<imageN>").trim();
    if (ts !== "<imageN>") parts[0] += ` (<imageN>→${ts})`;
    if (spec.num_inference_steps) parts.push(`${spec.num_inference_steps} steps${spec.sample_solver ? ` ${spec.sample_solver}` : ""}`);
    if (spec.resolution) parts.push(`Native ${spec.resolution}`);
    if (spec.no_negative_prompt) parts.push("No negative prompt (CFG 1.0)");
    return parts.join(" • ");
  }
  window.getAnchorRendererSpec = getAnchorRendererSpec;
  window.getMaxAnchorRefs = getMaxAnchorRefs;
  window.describeAnchorModelCaps = describeAnchorModelCaps;

  function normalizeRelPath(rp) {
    return String(rp || "").replace(/\\/g, "/").replace(/^\/+/, "");
  }

  function encodeMediaSubpath(relPath) {
    return normalizeRelPath(relPath).split("/").filter(Boolean).map(encodeURIComponent).join("/");
  }

  function classifySourceOrigin(rp) {
    const clean = normalizeRelPath(rp);
    if (clean.startsWith("work/anchor_sources/")) return "Uploaded";
    if (clean.startsWith("work/anchors/")) return "Anchor";
    if (clean.startsWith("references/")) return "Ref";
    return "Source";
  }

  function buildMediaRefUrl(relPath) {
    const encSlug = encodeURIComponent(window.state?.currentSlug || "");
    return `/media/${encSlug}/ref/${encodeMediaSubpath(relPath)}`;
  }

  function getActiveCharsMap() {
    const c = window.state?.charactersData;
    return (c && Object.keys(c).length) ? c : (window.state?.projectData?.characters || {});
  }

  function expandPromptVars(prompt) {
    const vars = window.state?.projectData?.variables || {};
    return String(prompt || "").replace(/\{([a-zA-Z0-9_]+)\}/g, (m, k) => (vars[k] !== undefined ? String(vars[k]) : m));
  }

  function resolveEffectiveAnchorSources(anchorObj, opts = {}) {
    const maxRefs = getMaxAnchorRefs(anchorObj, opts);
    const sharedRefs = Array.isArray(window.state?.projectData?.models?.anchor_shared_settings?.image_refs)
      ? window.state.projectData.models.anchor_shared_settings.image_refs : [];

    // Compute what Auto (@character / Shared) would provide so UI can always compare/reset cleanly
    const rawPrompt = String(anchorObj?.prompt || "");
    const varExpanded = expandPromptVars(rawPrompt);
    const hasExplicitImgTags = /<(?:image|Picture\s*)\d+>/i.test(varExpanded);
    const chars = getActiveCharsMap();
    const tagEntries = [];
    Object.entries(chars).forEach(([eid, ent]) => {
      const defLook = ent?.default_look;
      Object.entries(ent?.looks || {}).forEach(([lid, lk]) => {
        const refsList = Array.isArray(lk?.image_refs) ? lk.image_refs.filter(Boolean) : [];
        const tag = String(lk?.tag || `@${lid}`).trim();
        if (tag) tagEntries.push({ token: tag.toLowerCase(), label: tag, refs: refsList });
        if (lid === defLook && eid) tagEntries.push({ token: `@${eid}`.toLowerCase(), label: `@${eid}`, refs: refsList });
      });
    });

    const autoItems = [];
    const seenAuto = new Set();
    const pushAuto = (rp, label) => {
      if (autoItems.length >= maxRefs) return;
      const clean = normalizeRelPath(rp);
      if (!clean || seenAuto.has(clean.toLowerCase())) return;
      seenAuto.add(clean.toLowerCase());
      autoItems.push({ rel_path: clean, sourceLabel: label, isAuto: true, tag: `<image${autoItems.length + 1}>`, index: autoItems.length });
    };

    if (hasExplicitImgTags) {
      sharedRefs.forEach((rp) => pushAuto(rp, "Shared"));
    } else if (tagEntries.length) {
      const matches = [];
      const searchSrc = varExpanded;
      tagEntries.forEach((entry) => {
        const re = new RegExp(`(^|[^\\w])${entry.token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=[^\\w]|$)`, "gi");
        const m = re.exec(searchSrc);
        if (m) matches.push({ pos: m.index, entry });
      });
      matches.sort((a, b) => a.pos - b.pos);
      matches.forEach(({ entry }) => entry.refs.forEach((rp) => pushAuto(rp, entry.label)));
      if (!autoItems.length) sharedRefs.forEach((rp) => pushAuto(rp, "Shared"));
    } else {
      sharedRefs.forEach((rp) => pushAuto(rp, "Shared"));
    }

    if (Array.isArray(anchorObj?.image_refs)) {
      const customItems = anchorObj.image_refs.slice(0, maxRefs).map((rp, idx) => {
        const clean = normalizeRelPath(rp);
        return { rel_path: clean, sourceLabel: classifySourceOrigin(clean), isAuto: false, tag: `<image${idx + 1}>`, index: idx };
      });
      return {
        mode: customItems.length ? "custom" : "none",
        explicit: true,
        customEmpty: false,
        items: customItems,
        autoItems,
        hasAutoSources: autoItems.length > 0,
      };
    }
    if (anchorObj?._customSourceMode) {
      return { mode: "custom", explicit: false, customEmpty: true, items: [], autoItems, hasAutoSources: autoItems.length > 0 };
    }
    return {
      mode: "auto",
      explicit: false,
      customEmpty: false,
      items: autoItems,
      autoItems,
      hasAutoSources: autoItems.length > 0,
    };
  }
  window.resolveEffectiveAnchorSources = resolveEffectiveAnchorSources;

  function gatherAnchorSourceGroups(excludeSet) {
    const srcFiles = [], anchorFiles = [], refFiles = [], allUploaded = [];
    const seen = new Set(Array.from(excludeSet || []).map((x) => normalizeRelPath(x)));
    const seenUp = new Set();
    const addUnique = (arr, rp) => {
      const clean = normalizeRelPath(rp);
      if (!clean || seen.has(clean)) return;
      seen.add(clean);
      arr.push(clean);
    };

    (window.state?.assets?.anchor_source_files || []).forEach((rp) => {
      const clean = normalizeRelPath(rp);
      if (clean.startsWith("work/anchor_sources/")) {
        if (!seenUp.has(clean)) { seenUp.add(clean); allUploaded.push(clean); }
        addUnique(srcFiles, clean);
      } else {
        addUnique(refFiles, clean);
      }
    });
    (window.state?.assets?.reference_files || window.state?.assets?.ref_files || []).forEach((f) => {
      addUnique(refFiles, String(f).includes("/") ? f : `references/${f}`);
    });
    (window.state?.projectData?.anchors || []).forEach((a) => {
      if (Array.isArray(a?.image_refs)) {
        a.image_refs.forEach((rp) => {
          const clean = normalizeRelPath(rp);
          if (clean.startsWith("work/anchor_sources/")) {
            if (!seenUp.has(clean)) { seenUp.add(clean); allUploaded.push(clean); }
            addUnique(srcFiles, clean);
          } else if (!clean.startsWith("work/anchors/")) {
            addUnique(refFiles, clean);
          }
        });
      }
    });

    const ancDirRel = normalizeRelPath(window.state?.projectData?.project?.directories?.anchors || "work/anchors").replace(/\/+$/, "");
    Object.values(window.state?.assets?.anchors || {}).forEach((inf) => {
      if (inf?.filename && inf.is_anchor_dir !== false) addUnique(anchorFiles, `${ancDirRel}/${inf.filename}`);
    });

    Object.values(getActiveCharsMap()).forEach((c) => {
      Object.values(c?.looks || {}).forEach((l) => (l?.image_refs || []).forEach((rp) => addUnique(refFiles, rp)));
    });
    return { srcFiles, anchorFiles, refFiles, allUploaded };
  }

  function describeAnchorSourceRefs(a, fallbackText = "", opts = {}) {
    const maxRefs = getMaxAnchorRefs(a, opts);
    const rspec = getAnchorRendererSpec(a, opts);
    const tagStyle = String(rspec.ref_tag_style || "<imageN>").trim();
    const styleNote = tagStyle && tagStyle !== "<imageN>" ? ` (<imageN> → ${tagStyle})` : "";
    const res = resolveEffectiveAnchorSources(a, opts);
    if (res.mode === "none") return "Source images off (pure text-to-image override)";
    if (res.mode === "custom" && res.items.length) {
      return `Custom source images (${res.items.length}/${maxRefs})${styleNote}: click <imageN> to prompt or × to remove`;
    }
    if (res.mode === "auto" && res.items.length) {
      const labels = Array.from(new Set(res.items.map((x) => x.sourceLabel))).join(", ");
      return `Provided via ${labels} (${res.items.length}/${maxRefs})${styleNote}: click × on any image to exclude it`;
    }
    return fallbackText || `Text-to-image (attach up to ${maxRefs} source images below${styleNote} or type @character)`;
  }
  window.describeAnchorSourceRefs = describeAnchorSourceRefs;

  function insertTagIntoPrompt(ta, tag) {
    if (!ta) return;
    const st = typeof ta.selectionStart === "number" ? ta.selectionStart : ta.value.length;
    const en = typeof ta.selectionEnd === "number" ? ta.selectionEnd : st;
    const v = ta.value || "";
    const pre = st > 0 && !/\s$/.test(v.slice(0, st)) ? " " : "";
    const suf = en < v.length && !/^\s/.test(v.slice(en)) ? " " : "";
    ta.value = v.slice(0, st) + pre + tag + suf + v.slice(en);
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.focus();
  }

  function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error(`Failed to read ${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  function findAnchorByIdInState(aid) {
    const up = String(aid || "").trim().toUpperCase();
    const list = window.state?.projectData?.anchors || [];
    if (!up) return null;
    return list.find((a) => {
      const id = String(a?.id || (String(a?.filename || "").match(/^(A\d+[a-z]*)/i) || [])[1] || "").trim().toUpperCase();
      return id === up;
    }) || null;
  }

  function resolveMutableAnchor(anchorObj, opts, aid) {
    if (typeof opts?.ensureAnchorObj === "function") return opts.ensureAnchorObj() || anchorObj;
    return (aid ? findAnchorByIdInState(aid) : null) || anchorObj;
  }

  async function uploadAndAttachSourceFiles(files, aid, anchorObj, opts, onRefresh) {
    const slug = window.state?.currentSlug;
    const targetObj = resolveMutableAnchor(anchorObj, opts, aid);
    if (!slug || !targetObj) return;
    const list = Array.from(files || []).filter((f) => !f.type || f.type.startsWith("image/") || /\.(jpe?g|png|webp)$/i.test(f.name || ""));
    if (!list.length) {
      window.showToast?.("Select JPG, PNG, or WEBP image file(s) to attach as source images.");
      return;
    }
    const maxRefs = getMaxAnchorRefs(targetObj, opts);
    const existingLen = Array.isArray(targetObj.image_refs) ? targetObj.image_refs.length : 0;
    const remaining = Math.max(0, maxRefs - existingLen);
    if (remaining === 0) {
      window.showToast?.(`Maximum ${maxRefs} source images per anchor reached — remove one first.`);
      return;
    }
    const batch = list.slice(0, remaining);
    if (list.length > remaining) {
      window.showToast?.(`Attaching first ${remaining} image(s) (max ${maxRefs} source images per anchor).`);
    }

    const uploaded = [];
    for (const file of batch) {
      try {
        const dataUrl = await readFileAsDataUrl(file);
        const resp = await fetch("/api/anchors/upload-source", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slug, aid, filename: file.name, data_url: dataUrl }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.success || !data.rel_path) {
          window.showToast?.(`❌ Source upload failed (${file.name}): ${data.error || `HTTP ${resp.status}`}`);
          continue;
        }
        if (!uploaded.includes(data.rel_path)) uploaded.push(data.rel_path);
        if (window.state?.assets) {
          const curAssets = window.state.assets.anchor_source_files || [];
          window.state.assets.anchor_source_files = data.anchor_source_files || Array.from(new Set([data.rel_path, ...curAssets]));
        }
      } catch (err) {
        window.showToast?.(`❌ Failed uploading ${file.name}: ${err.message}`);
      }
    }
    if (uploaded.length > 0 && window.state?.currentSlug === slug) {
      const liveTarget = resolveMutableAnchor(targetObj, opts, aid) || targetObj;
      const cur = Array.isArray(liveTarget.image_refs) ? [...liveTarget.image_refs] : [];
      for (const rp of uploaded) {
        if (cur.length >= maxRefs) break;
        if (!cur.some((x) => normalizeRelPath(x) === normalizeRelPath(rp))) cur.push(rp);
      }
      delete liveTarget._customSourceMode;
      liveTarget.image_refs = cur;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Attach ${uploaded.length} Source Image${uploaded.length === 1 ? "" : "s"} to ${aid}`);
      onRefresh();
      await window.refreshProjectAssets?.(true);
      window.showToast?.(`📎 Attached ${uploaded.length} source image${uploaded.length === 1 ? "" : "s"} to [${aid}]`);
    }
  }

  async function deleteUploadedSourceFromDisk(relPath, anchorObj, aid, onRefresh) {
    const slug = window.state?.currentSlug;
    if (!slug || !relPath) return;
    const baseName = normalizeRelPath(relPath).split("/").pop() || relPath;
    if (!confirm(`Delete uploaded source file '${baseName}' from disk and detach it from all anchors?`)) return;
    const resp = await fetch("/api/anchors/delete-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug, rel_path: relPath }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.success) {
      window.showToast?.(`❌ Failed deleting source: ${data.error || `HTTP ${resp.status}`}`);
      return;
    }
    const cleanTarget = normalizeRelPath(data.rel_path || relPath);
    (window.state?.projectData?.anchors || []).forEach((a) => {
      if (Array.isArray(a?.image_refs)) {
        const rem = a.image_refs.filter((r) => normalizeRelPath(r) !== cleanTarget);
        if (rem.length !== a.image_refs.length) {
          if (rem.length) a.image_refs = rem;
          else delete a.image_refs;
        }
      }
    });
    if (window.state?.assets && Array.isArray(data.anchor_source_files)) {
      window.state.assets.anchor_source_files = data.anchor_source_files;
    }
    window.markProjectDirty?.();
    window.recordHistorySnapshot?.(`Delete Uploaded Source ${baseName}`);
    onRefresh();
    await window.refreshProjectAssets?.(true);
    window.showToast?.(`🗑 Deleted uploaded source file '${baseName}'`);
  }

  function renderAnchorSourceRefsPanel(hostEl, anchorObj, aid, opts = {}) {
    if (!hostEl || !anchorObj) return;
    const esc = window.escapeHtml || ((x) => String(x || ""));
    const maxRefs = getMaxAnchorRefs(anchorObj, opts);
    const rspec = getAnchorRendererSpec(anchorObj, opts);
    const tagStyle = String(rspec.ref_tag_style || "<imageN>").trim();
    const resolved = resolveEffectiveAnchorSources(anchorObj, opts);
    const items = resolved.items;
    const groups = gatherAnchorSourceGroups(resolved.explicit ? items.map((x) => x.rel_path) : []);
    const hasChoices = groups.srcFiles.length + groups.anchorFiles.length + groups.refFiles.length > 0;
    const aidKey = String(aid || "").toUpperCase();
    const libOpen = openLibraryAids.has(aidKey);

    let statusLabel = `Source Images (0/${maxRefs})`;
    if (resolved.mode === "custom") statusLabel = `Source Images (${items.length}/${maxRefs})`;
    else if (resolved.mode === "none") statusLabel = `Source Images: Off (0/${maxRefs})`;
    else statusLabel = items.length ? `Auto Sources (${items.length}/${maxRefs})` : `Source Images: Auto (0/${maxRefs})`;

    hostEl.innerHTML = `
      <div class="anchor-source-refs-panel${opts.compact ? " is-compact" : ""}">
        <div class="anchor-src-toolbar">
          <span class="anchor-src-title">${esc(statusLabel)}</span>
          <select class="anchor-src-mode-select" title="Choose Auto (@character / Shared), Custom source images, or Text-Only">
            <option value="auto">Auto</option>
            <option value="custom">Custom</option>
            <option value="none">Text-Only</option>
          </select>
          <button type="button" class="btn-mini anchor-src-upload-btn" title="Upload source image(s) from disk (no Character or Ref required)">📁 + Source</button>
          <input type="file" class="anchor-src-file-input" accept=".jpg,.jpeg,.png,.webp,image/*" multiple style="display:none;">
          ${hasChoices ? `<select class="anchor-src-pick-select" title="Attach an existing uploaded source, anchor, or ref image"><option value="">📎 Pick Existing...</option></select>` : ""}
          ${groups.allUploaded.length ? `<button type="button" class="btn-mini anchor-src-lib-btn${libOpen ? " is-open" : ""}" title="View & manage uploaded source images on disk">🗂 Library (${groups.allUploaded.length})</button>` : ""}
          ${resolved.explicit ? `<button type="button" class="btn-mini anchor-src-clear-btn" title="${resolved.hasAutoSources ? "Reset back to Auto (@character / Shared)" : "Clear all custom source images"}">${resolved.hasAutoSources ? "↺ Reset Auto" : "✕ Clear"}</button>` : ""}
        </div>
        <div class="anchor-src-body"></div>
        ${libOpen && groups.allUploaded.length ? `<div class="anchor-src-library"></div>` : ""}
      </div>`;

    const panel = hostEl.querySelector(".anchor-source-refs-panel");
    const modeSel = hostEl.querySelector(".anchor-src-mode-select");
    const uploadBtn = hostEl.querySelector(".anchor-src-upload-btn");
    const fileInp = hostEl.querySelector(".anchor-src-file-input");
    const pickSel = hostEl.querySelector(".anchor-src-pick-select");
    const libBtn = hostEl.querySelector(".anchor-src-lib-btn");
    const clearBtn = hostEl.querySelector(".anchor-src-clear-btn");
    const body = hostEl.querySelector(".anchor-src-body");
    const libEl = hostEl.querySelector(".anchor-src-library");

    modeSel.value = resolved.mode;
    const refreshPanel = () => {
      const liveAnchor = resolveMutableAnchor(anchorObj, opts, aid);
      opts.onChange?.();
      if (hostEl.isConnected) renderAnchorSourceRefsPanel(hostEl, liveAnchor, aid, opts);
      else window.updateAnchorStudioUI?.();
    };

    if (pickSel) {
      const addGroup = (label, arr) => {
        if (!arr.length) return;
        const og = document.createElement("optgroup");
        og.label = label;
        arr.forEach((rp) => {
          const fn = rp.split("/").pop() || rp;
          og.appendChild(new Option(fn, rp));
        });
        pickSel.appendChild(og);
      };
      addGroup("Uploaded Sources", groups.srcFiles);
      addGroup("Project Anchors", groups.anchorFiles);
      addGroup("Reference Library", groups.refFiles);
      pickSel.onchange = () => {
        const chosen = pickSel.value;
        pickSel.value = "";
        if (!chosen) return;
        const target = resolveMutableAnchor(anchorObj, opts);
        const curMax = getMaxAnchorRefs(target, opts);
        const next = Array.isArray(target.image_refs) ? [...target.image_refs] : [];
        if (next.length >= curMax) {
          window.showToast?.(`Maximum ${curMax} source images per anchor reached — remove one first.`);
          return;
        }
        if (!next.includes(chosen)) next.push(chosen);
        delete target._customSourceMode;
        target.image_refs = next;
        window.markProjectDirty?.();
        window.recordHistorySnapshot?.(`Attach Source Image to ${aid}`);
        refreshPanel();
      };
    }

    modeSel.onchange = () => {
      const target = resolveMutableAnchor(anchorObj, opts);
      const val = modeSel.value;
      if (val === "auto") {
        delete target.image_refs;
        delete target._customSourceMode;
      } else if (val === "none") {
        delete target._customSourceMode;
        target.image_refs = [];
      } else {
        if (resolved.mode === "auto" && resolved.autoItems.length) {
          delete target._customSourceMode;
          target.image_refs = resolved.autoItems.map((x) => x.rel_path);
        } else if (Array.isArray(target.image_refs) && target.image_refs.length) {
          delete target._customSourceMode;
        } else {
          delete target.image_refs;
          target._customSourceMode = true;
        }
      }
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Change ${aid} Source Images Mode`);
      refreshPanel();
    };

    clearBtn?.addEventListener("click", () => {
      const target = resolveMutableAnchor(anchorObj, opts);
      delete target.image_refs;
      delete target._customSourceMode;
      window.markProjectDirty?.();
      window.recordHistorySnapshot?.(`Clear ${aid} Source Images`);
      refreshPanel();
    });

    libBtn?.addEventListener("click", () => {
      if (openLibraryAids.has(aidKey)) openLibraryAids.delete(aidKey);
      else openLibraryAids.add(aidKey);
      renderAnchorSourceRefsPanel(hostEl, resolveMutableAnchor(anchorObj, opts), aid, opts);
    });

    uploadBtn?.addEventListener("click", () => { fileInp.value = ""; fileInp.click(); });
    fileInp?.addEventListener("change", (e) => {
      if (e.target.files?.length) uploadAndAttachSourceFiles(e.target.files, aid, anchorObj, opts, refreshPanel);
    });

    panel.addEventListener("dragover", (e) => { e.preventDefault(); panel.classList.add("is-dragover"); });
    panel.addEventListener("dragleave", () => panel.classList.remove("is-dragover"));
    panel.addEventListener("drop", (e) => {
      e.preventDefault();
      panel.classList.remove("is-dragover");
      if (e.dataTransfer?.files?.length) uploadAndAttachSourceFiles(e.dataTransfer.files, aid, anchorObj, opts, refreshPanel);
    });

    if (items.length) {
      const strip = document.createElement("div");
      strip.className = "anchor-src-strip";
      items.forEach((item, idx) => {
        const rp = item.rel_path;
        const chip = document.createElement("div");
        chip.className = `anchor-src-chip${item.isAuto ? " is-auto-src" : ""}`;
        const baseName = String(rp || "").split("/").pop() || rp;
        const imgUrl = buildMediaRefUrl(rp);
        chip.innerHTML = `
          <img src="${esc(imgUrl)}" alt="${esc(baseName)}" title="${esc(rp)} — click to view full image">
          <div class="anchor-src-chip-meta">
            <div style="display:flex;align-items:center;gap:4px;">
              <button type="button" class="anchor-src-tag-btn" title="Click to insert ${esc(item.tag)}${tagStyle === "<Picture N>" ? ` (sent as <Picture ${idx + 1}>)` : ""} into prompt">${esc(item.tag)}</button>
              <span class="anchor-src-origin${item.isAuto ? " is-auto" : ""}">${esc(item.sourceLabel)}</span>
            </div>
            <span class="anchor-src-fname" title="${esc(rp)}">${esc(baseName)}</span>
          </div>
          <button type="button" class="anchor-src-rm-btn" aria-label="Remove source image ${esc(baseName)}" title="${item.isAuto ? `Exclude ${esc(baseName)} from [${esc(aid)}]` : `Remove ${esc(baseName)} from [${esc(aid)}]`}">×</button>`;
        const imgEl = chip.querySelector("img");
        imgEl?.addEventListener("click", (e) => { e.stopPropagation(); window.openImageInNewTab?.(imgUrl); });
        imgEl?.addEventListener("error", () => { imgEl.alt = "Missing"; imgEl.style.opacity = "0.4"; });
        chip.querySelector(".anchor-src-tag-btn")?.addEventListener("click", () => insertTagIntoPrompt(opts.promptInput, item.tag));
        chip.querySelector(".anchor-src-rm-btn")?.addEventListener("click", () => {
          const target = resolveMutableAnchor(anchorObj, opts);
          delete target._customSourceMode;
          if (item.isAuto) {
            target.image_refs = resolved.autoItems.filter((_, j) => j !== idx).map((x) => x.rel_path);
          } else {
            const next = (target.image_refs || []).filter((_, j) => j !== idx);
            if (next.length) target.image_refs = next;
            else if (resolved.hasAutoSources) target.image_refs = [];
            else delete target.image_refs;
          }
          window.markProjectDirty?.();
          window.recordHistorySnapshot?.(`Remove Source Image from ${aid}`);
          refreshPanel();
        });
        strip.appendChild(chip);
      });
      body.appendChild(strip);
    } else if (!opts.compact || resolved.mode === "none" || resolved.customEmpty) {
      const hint = document.createElement("div");
      hint.className = "anchor-src-empty-hint";
      hint.textContent = resolved.mode === "none"
        ? "Text-only override active — no source images will be sent."
        : resolved.customEmpty
          ? `Custom mode — drop images or click "📁 + Source" / "📎 Pick Existing..." above to add up to ${maxRefs} images.`
          : `No source images provided — drop or click "📁 + Source" to attach up to ${maxRefs} images.`;
      body.appendChild(hint);
    }

    if (libEl && groups.allUploaded.length) {
      const attachedSet = new Set(items.filter((x) => !x.isAuto).map((x) => normalizeRelPath(x.rel_path)));
      groups.allUploaded.forEach((rp) => {
        const fn = rp.split("/").pop() || rp;
        const imgUrl = buildMediaRefUrl(rp);
        const isAttached = attachedSet.has(normalizeRelPath(rp));
        const row = document.createElement("div");
        row.className = "anchor-src-lib-item";
        row.innerHTML = `
          <img src="${esc(imgUrl)}" alt="${esc(fn)}" title="${esc(rp)}">
          <span class="anchor-src-fname" title="${esc(rp)}">${esc(fn)}</span>
          ${isAttached
            ? `<span style="font-size:9px;color:#34d399;font-weight:700;">On [${esc(aid)}]</span>`
            : `<button type="button" class="btn-mini" data-lact="attach" style="font-size:9.5px;padding:1px 5px;height:20px;">+ Attach</button>`}
          <button type="button" class="btn-mini" data-lact="delete-file" title="Delete uploaded file from disk" style="font-size:9.5px;padding:1px 5px;height:20px;color:#f87171;">🗑</button>`;
        row.querySelector("img")?.addEventListener("click", (e) => { e.stopPropagation(); window.openImageInNewTab?.(imgUrl); });
        row.querySelector("[data-lact='attach']")?.addEventListener("click", () => {
          const target = resolveMutableAnchor(anchorObj, opts, aid);
          const curMax = getMaxAnchorRefs(target, opts);
          const next = Array.isArray(target.image_refs) ? [...target.image_refs] : [];
          if (next.length >= curMax) { window.showToast?.(`Maximum ${curMax} source images per anchor reached.`); return; }
          if (!next.includes(rp)) next.push(rp);
          delete target._customSourceMode;
          target.image_refs = next;
          window.markProjectDirty?.();
          window.recordHistorySnapshot?.(`Attach Source Image to ${aid}`);
          refreshPanel();
        });
        row.querySelector("[data-lact='delete-file']")?.addEventListener("click", () => deleteUploadedSourceFromDisk(rp, anchorObj, aid, refreshPanel));
        libEl.appendChild(row);
      });
    }
  }

  function renderTakeSourcesSummary(hostEl, imageRefs, opts = {}) {
    if (!hostEl) return;
    hostEl.innerHTML = "";
    const refs = Array.isArray(imageRefs) ? imageRefs.filter(Boolean) : [];
    if (!refs.length) return;
    const esc = window.escapeHtml || ((x) => String(x || ""));
    const lbl = document.createElement("span");
    lbl.className = "take-sources-lbl";
    lbl.textContent = opts.label || `Sources (${refs.length}):`;
    hostEl.appendChild(lbl);
    refs.forEach((rp, idx) => {
      const fn = String(rp).split("/").pop() || rp;
      const imgUrl = buildMediaRefUrl(rp);
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "take-source-pill";
      pill.title = `<image${idx + 1}> ${rp} — click to open full image`;
      pill.innerHTML = `<img src="${esc(imgUrl)}" alt="${esc(fn)}"><span>&lt;image${idx + 1}&gt; ${esc(fn)}</span>`;
      pill.onclick = (e) => { e.stopPropagation(); window.openImageInNewTab?.(imgUrl); };
      hostEl.appendChild(pill);
    });
  }

  function findAnchorObjByVal(anchorVal) {
    if (!anchorVal) return { a: null, aid: "" };
    const aid = (window.extractAnchorId?.(anchorVal) || "").toUpperCase();
    const list = window.state?.projectData?.anchors || [];
    const a = list.find((x) => x.filename === anchorVal || (aid && (window.extractAnchorId?.(x.filename) || "").toUpperCase() === aid)) || null;
    return { a, aid: aid || (a ? (window.extractAnchorId?.(a.filename) || "").toUpperCase() : "") };
  }

  function renderSceneAnchorSourcesMini(hostEl, anchorVal) {
    if (!hostEl) return;
    hostEl.innerHTML = "";
    const { a, aid } = findAnchorObjByVal(anchorVal);
    if (!a) return;
    const res = resolveEffectiveAnchorSources(a);
    if (!res.items.length) return;
    const esc = window.escapeHtml || ((x) => String(x || ""));
    const wrap = document.createElement("button");
    wrap.type = "button";
    wrap.className = "scene-anchor-sources-mini";
    wrap.title = `${res.items.length} source image(s) provided to [${aid}] — click to view or edit in Scene Anchors`;
    wrap.innerHTML = res.items.map((it) => `<img src="${esc(buildMediaRefUrl(it.rel_path))}" alt="${esc(it.rel_path)}">`).join("")
      + `<span>📎 ${res.items.length} src${res.items.length === 1 ? "" : "s"} (${res.mode === "auto" ? "Auto" : "Custom"})</span>`;
    wrap.onclick = (e) => { e.stopPropagation(); window.focusAnchorInStudio?.(aid || anchorVal); };
    hostEl.appendChild(wrap);
  }

  function syncInspectorAnchorSourcesBadge(anchorVal) {
    const grp = document.getElementById("anchorPickerGroup");
    if (!grp) return;
    let host = document.getElementById("inspectorAnchorSourcesHost");
    if (!host) {
      host = document.createElement("div");
      host.id = "inspectorAnchorSourcesHost";
      grp.appendChild(host);
    }
    renderSceneAnchorSourcesMini(host, anchorVal);
  }

  window.renderAnchorSourceRefsPanel = renderAnchorSourceRefsPanel;
  window.renderTakeSourcesSummary = renderTakeSourcesSummary;
  window.renderSceneAnchorSourcesMini = renderSceneAnchorSourcesMini;
  window.syncInspectorAnchorSourcesBadge = syncInspectorAnchorSourcesBadge;
})();
