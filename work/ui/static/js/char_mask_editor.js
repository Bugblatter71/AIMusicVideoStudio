/**
 * Character Reference Mask Visibility & Interactive Extraction Studio (char_mask_editor.js)
 *
 * Provides:
 * 1. Per-ref Mask Status badges + live Rubylith overlay visibility directly on Look cards.
 * 2. Interactive Canvas Modal: Keep(+)/Erase(-) Brush, Subject Box, SAM3 +/- Points, Split Wipe,
 *    and White MSR vs Checkerboard Transparency vs Red Mask Tint views.
 * 3. Tiered AI extraction: YOLOv8m-seg Person Split, SAM 3.1 Prompt, BiRefNet 1024px,
 *    U2-Net #19 MSR Simulator, and Qwen Image 2.1 AI Clean-Plate De-Occlusion.
 */
(function () {
  "use strict";

  const _esc = (s) => (window.escapeHtml ? window.escapeHtml(s) : String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
  const _norm = (p) => String(p || "").replace(/\\/g, "/").replace(/^\/+/, "");
  const _mediaRef = (slug, rp) => `/media/${encodeURIComponent(slug)}/ref/${_norm(rp).split("/").filter(Boolean).map(encodeURIComponent).join("/")}`;

  const _overlayCache = new Map();
  const _showMaskLooks = new Set();
  let _modalState = null;

  async function _syncDirtyProjectFirst() {
    if (window.state?.isDirty && typeof window.saveProjectData === "function") {
      return Boolean(await window.saveProjectData(false));
    }
    return true;
  }

  function _applyUpdatedCharsState(chars) {
    if (chars) {
      window.state.charactersData = chars;
      if (window.state.projectData) window.state.projectData.characters = chars;
      if (!window.state?.isDirty && typeof window.markHistorySaved === "function") window.markHistorySaved();
    }
    window.renderCharactersTab?.();
    if (window.state?.selectedSceneIdx !== null && window.state?.projectData?.scenes?.[window.state.selectedSceneIdx]) {
      const sc = window.state.projectData.scenes[window.state.selectedSceneIdx];
      window.updatePromptReferenceTray?.(sc);
      window.renderSceneRefPickerUI?.(sc);
    }
  }

  function _findMaskMeta(look, relPath) {
    const clean = _norm(relPath);
    const rmap = look?.ref_masks;
    if (!rmap || typeof rmap !== "object") return null;
    if (rmap[clean]) return { cutoutRel: clean, meta: rmap[clean], isCutout: true };
    for (const [k, m] of Object.entries(rmap)) {
      if (m && _norm(m.source_rel) === clean) return { cutoutRel: k, meta: m, isCutout: false };
    }
    return null;
  }

  function _paintRedOverlayUrl(maskUrl, imgEl) {
    if (!maskUrl || !imgEl) return;
    if (_overlayCache.has(maskUrl)) {
      imgEl.src = _overlayCache.get(maskUrl);
      imgEl.classList.add("has-overlay");
      return;
    }
    const im = new Image();
    im.crossOrigin = "anonymous";
    im.onload = () => {
      const c = document.createElement("canvas");
      c.width = im.naturalWidth || 256;
      c.height = im.naturalHeight || 256;
      const ctx = c.getContext("2d");
      ctx.drawImage(im, 0, 0);
      const id = ctx.getImageData(0, 0, c.width, c.height);
      const d = id.data;
      for (let i = 0; i < d.length; i += 4) {
        const keep = d[i];
        if (keep < 230) {
          d[i] = 225; d[i + 1] = 29; d[i + 2] = 72; d[i + 3] = Math.round((255 - keep) * 0.65);
        } else {
          d[i + 3] = 0;
        }
      }
      ctx.putImageData(id, 0, 0);
      const url = c.toDataURL("image/png");
      _overlayCache.set(maskUrl, url);
      imgEl.src = url;
      imgEl.classList.add("has-overlay");
    };
    im.src = maskUrl;
  }

  function enhanceCharacterLookMasks(rootEl) {
    const root = rootEl || document.getElementById("characterDetailPane");
    if (!root) return;
    const slug = window.state?.currentSlug || "";

    root.querySelectorAll(".look-editor-card").forEach((card) => {
      const nameInp = card.querySelector(".input-look-name");
      const eid = nameInp?.dataset?.entity;
      const lid = nameInp?.dataset?.look;
      const ent = window.state?.charactersData?.[eid];
      const look = ent?.looks?.[lid];
      if (!eid || !lid || !look) return;

      const hdrBtns = card.querySelector(".btn-crop-clip")?.parentElement;
      const lookKey = `${eid}:${lid}`;
      if (hdrBtns && !hdrBtns.querySelector(".btn-toggle-look-masks")) {
        const lbl = document.createElement("label");
        lbl.className = "btn-mini btn-toggle-look-masks";
        lbl.style.cssText = "display:inline-flex;align-items:center;gap:4px;cursor:pointer;padding:2px 7px;border:1px solid #334155;border-radius:4px;background:#0f172a;font-size:11px;color:#cbd5e1;";
        lbl.title = "Overlay saved Keep/Cut red tint directly on Reference thumbnails";
        lbl.innerHTML = `<input type="checkbox" class="chk-show-look-masks" ${_showMaskLooks.has(lookKey) ? "checked" : ""}> 👁️ Show Masks`;
        hdrBtns.prepend(lbl);
        const chk = lbl.querySelector(".chk-show-look-masks");
        const syncCardOverlay = () => {
          if (chk.checked) _showMaskLooks.add(lookKey); else _showMaskLooks.delete(lookKey);
          card.classList.toggle("show-mask-overlay", Boolean(chk.checked));
        };
        chk.addEventListener("change", syncCardOverlay);
        syncCardOverlay();
      }

      const isoSet = new Set((look.generated_takes || []).filter((t) => t?.isolate).map((t) => _norm(t.rel_path)));

      card.querySelectorAll(".char-ref-thumb-wrap").forEach((wrap) => {
        if (wrap.dataset.maskEnhanced === "1") return;
        wrap.dataset.maskEnhanced = "1";
        const rPath = wrap.dataset.refPath || "";
        const mInfo = _findMaskMeta(look, rPath);
        const isCut = Boolean(mInfo?.isCutout);
        const hasMask = Boolean(mInfo?.meta?.mask_rel);
        const isIso = isoSet.has(_norm(rPath));
        if (isCut) wrap.classList.add("is-masked");

        const pillCls = isCut ? "is-masked" : (isIso ? "is-isolated" : "");
        const pillTxt = isCut ? "✂️ MASKED" : (hasMask ? "RAW (MASK READY)" : (isIso ? "🪄 AI ISOLATED" : "RAW"));

        const mediaBox = wrap.querySelector(".char-ref-thumb-media");
        if (mediaBox) {
          const badge = document.createElement("span");
          badge.className = `char-mask-status-pill ${pillCls}`;
          badge.textContent = pillTxt;
          mediaBox.appendChild(badge);

          if (mInfo?.meta?.mask_rel) {
            const ov = document.createElement("img");
            ov.className = "char-ref-thumb-overlay";
            mediaBox.appendChild(ov);
            const mUrl = `${_mediaRef(slug, mInfo.meta.mask_rel)}?t=${mInfo.meta.updated_at || Date.now()}`;
            _paintRedOverlayUrl(mUrl, ov);
          }
        }

        const actBar = document.createElement("div");
        actBar.className = "char-ref-thumb-actions";
        actBar.innerHTML = `
          <button type="button" class="btn-open-char-mask" title="Open interactive mask & extraction editor">✂️ Mask</button>
          ${hasMask ? `<button type="button" class="btn-toggle-char-mask" title="${isCut ? "Switch back to Raw original" : "Switch to saved clean cutout"}">${isCut ? "↺ Raw" : "✓ Cut"}</button>` : `<button type="button" class="btn-preview-msr" title="Preview what #19 LTX-2.5 MSR U²-Net cuts out">🔮 MSR</button>`}`;
        wrap.appendChild(actBar);

        actBar.querySelector(".btn-open-char-mask")?.addEventListener("click", (e) => {
          e.stopPropagation();
          openCharMaskModal(eid, lid, rPath);
        });
        actBar.querySelector(".btn-preview-msr")?.addEventListener("click", (e) => {
          e.stopPropagation();
          openCharMaskModal(eid, lid, rPath, { autoMsrPreview: true });
        });
        actBar.querySelector(".btn-toggle-char-mask")?.addEventListener("click", async (e) => {
          e.stopPropagation();
          try {
            if (!(await _syncDirtyProjectFirst())) return;
            const resp = await fetch("/api/project/characters/mask-toggle", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ slug, entity_id: eid, look_id: lid, rel_path: rPath }),
            });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.success) {
              window.showToast?.(data.using_cutout ? "✓ Switched reference to clean white cutout" : "↺ Switched back to raw original reference");
              _applyUpdatedCharsState(data.characters);
            } else {
              window.showToast?.(`❌ Toggle failed: ${data.error || resp.status}`);
            }
          } catch (err) {
            window.showToast?.(`❌ Toggle failed: ${err.message}`);
          }
        });
      });
    });
  }
  window.enhanceCharacterLookMasks = enhanceCharacterLookMasks;

  function _ensureModalDom() {
    let ov = document.getElementById("charMaskModalOverlay");
    if (ov) return ov;
    ov = document.createElement("div");
    ov.id = "charMaskModalOverlay";
    ov.className = "char-mask-modal-overlay";
    ov.innerHTML = `
      <div class="char-mask-modal" role="dialog" aria-label="Character Mask & Extraction Studio">
        <div class="char-mask-header">
          <div style="display:flex;align-items:center;gap:10px;min-width:0;">
            <span style="font-size:15px;font-weight:700;color:#f8fafc;">✂️ Mask &amp; Character Extraction Studio</span>
            <span id="charMaskHeaderFile" style="font-size:11px;color:#38bdf8;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>
          </div>
          <button type="button" id="btnCloseCharMaskModal" class="btn-mini" style="font-size:14px;padding:2px 9px;">✕ Close</button>
        </div>
        <div class="char-mask-body">
          <div class="char-mask-stage-col">
            <div class="char-mask-viewbar">
              <span style="font-size:11px;font-weight:700;color:#94a3b8;">View:</span>
              <button type="button" class="btn-mini char-mask-view-btn is-active" data-view="tint">🎨 Red Mask Tint</button>
              <button type="button" class="btn-mini char-mask-view-btn" data-view="white">⬜ White MSR View</button>
              <button type="button" class="btn-mini char-mask-view-btn" data-view="checker">🏁 Checkerboard RGBA</button>
              <button type="button" class="btn-mini char-mask-view-btn" data-view="split">↔️ Split Wipe</button>
              <input type="range" id="charMaskSplitSlider" min="5" max="95" value="50" style="display:none;width:90px;accent-color:#38bdf8;" title="Split Wipe Position">
              <button type="button" class="btn-mini char-mask-view-btn" id="btnViewMsrCompare" data-view="msr" style="display:none;color:#fbbf24;border-color:rgba(251,191,36,0.4);">🔮 U²-Net MSR Preview</button>
            </div>
            <div class="char-mask-canvas-wrap">
              <div class="char-mask-canvas-stack" id="charMaskCanvasStack">
                <canvas id="charMaskViewCanvas"></canvas>
                <canvas id="charMaskGuideCanvas"></canvas>
              </div>
            </div>
            <div class="char-mask-statusbar">
              <span id="charMaskMetaStatus">Loading image...</span>
              <span id="charMaskBusyText" style="color:#38bdf8;font-weight:600;"></span>
            </div>
          </div>
          <div class="char-mask-sidebar">
            <div class="char-mask-panel">
              <h4>Manual Brush &amp; Selection</h4>
              <div class="char-mask-tool-grid">
                <button type="button" class="btn-mini char-mask-tool-btn is-active" data-tool="keep">🖌️ Paint Keep (+)</button>
                <button type="button" class="btn-mini char-mask-tool-btn" data-tool="erase">🧹 Erase Cut (-)</button>
                <button type="button" class="btn-mini char-mask-tool-btn" data-tool="box">⬜ Subject Box</button>
                <button type="button" class="btn-mini char-mask-tool-btn" data-tool="pt_pos">📍 SAM3 +Keep</button>
                <button type="button" class="btn-mini char-mask-tool-btn" data-tool="pt_neg">📍 SAM3 -Drop</button>
                <button type="button" class="btn-mini" id="btnCharMaskClearGuides">Clear Box/Pts</button>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px;color:#cbd5e1;">
                <label>Brush <span id="charMaskBrushVal">32</span>px
                  <input type="range" id="charMaskBrushSize" min="4" max="120" value="32" style="width:100%;accent-color:#38bdf8;">
                </label>
                <label>Feather <span id="charMaskFeatherVal">1</span>px
                  <input type="range" id="charMaskFeather" min="0" max="8" step="0.5" value="1" style="width:100%;accent-color:#38bdf8;">
                </label>
              </div>
              <div style="display:flex;flex-wrap:wrap;gap:5px;">
                <button type="button" class="btn-mini" id="btnCharMaskUndo">↩️ Undo</button>
                <button type="button" class="btn-mini" id="btnCharMaskRedo">↪️ Redo</button>
                <button type="button" class="btn-mini" id="btnCharMaskInvert">🔁 Invert</button>
                <button type="button" class="btn-mini" id="btnCharMaskReset">🔄 Reset Full</button>
              </div>
              <label style="font-size:11px;color:#cbd5e1;display:flex;align-items:center;gap:6px;cursor:pointer;">
                <input type="checkbox" id="chkCharMaskPyMatting" checked> Smooth Hair/Edges on Extract (PyMatting)
              </label>
            </div>
            <div class="char-mask-panel">
              <h4>AI Extraction Engines</h4>
              <button type="button" class="char-mask-ai-btn" id="btnRunYoloSplit">
                <span>⚡ Auto-Split People (YOLOv8m-seg)</span><span class="char-mask-tag" id="tagCkptYolo">CPU/GPU</span>
              </button>
              <div id="charMaskDetPills" class="char-mask-det-pills" style="display:none;"></div>
              <div style="display:flex;gap:5px;">
                <input type="text" id="charMaskSamText" placeholder="Optional SAM3 text (e.g. woman in gray dress)" style="flex:1;font-size:11px;padding:4px 6px;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:4px;">
              </div>
              <button type="button" class="char-mask-ai-btn" id="btnRunSam3Cut">
                <span>✂️ Precision Cutout (SAM 3.1)</span><span class="char-mask-tag" id="tagCkptSam3">GPU</span>
              </button>
              <button type="button" class="char-mask-ai-btn" id="btnRunBirefnetCut">
                <span>✨ Hair &amp; Edge Cutout (BiRefNet)</span><span class="char-mask-tag" id="tagCkptBirefnet">GPU</span>
              </button>
              <button type="button" class="char-mask-ai-btn" id="btnRunMsrPreview">
                <span>🔮 Preview What #19 MSR Cuts Out (U²-Net)</span><span class="char-mask-tag" id="tagCkptU2net">On Disk</span>
              </button>
            </div>
            <div class="char-mask-panel">
              <h4>AI Clean-Plate De-Occlusion</h4>
              <div style="font-size:10.5px;color:#94a3b8;">Generates a brand-new un-occluded studio take into Takes (removes foreground hands/crowds blocking clothing).</div>
              <textarea id="charMaskIsolatePrompt" rows="3" style="font-size:11px;font-family:monospace;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:4px;padding:5px;resize:vertical;"></textarea>
              <div style="display:grid;grid-template-columns:1fr 95px;gap:5px;">
                <select id="charMaskIsolateRenderer" style="font-size:11px;">
                  <option value="1">#1 Qwen 2.1 Turbo (Local 8-step)</option>
                  <option value="2">#2 Qwen 2.1 Base (Local 40-step)</option>
                  <option value="fal_qwen_edit_2511">☁️ [fal.ai] Qwen Edit 2511</option>
                </select>
                <select id="charMaskIsolateRes" style="font-size:11px;">
                  <option value="1024x1024">1024x1024</option>
                  <option value="896x1152">896x1152</option>
                  <option value="1344x768">1344x768</option>
                  <option value="2048x2048">2048x2048 (4 MP Square)</option>
                  <option value="2720x1536">2720x1536 (4 MP 16:9)</option>
                  <option value="2912x2912">2912x2912 (8 MP P-H3)</option>
                  <option value="3872x2176">3872x2176 (8 MP 16:9)</option>
                  <option value="4096x4096">4096x4096 (16 MP Square)</option>
                </select>
              </div>
              <button type="button" class="btn btn-primary" id="btnRunAiIsolate" style="font-size:11.5px;padding:6px;">🪄 Queue AI De-Occlude Take</button>
            </div>
          </div>
        </div>
        <div class="char-mask-footer">
          <div style="font-size:11px;color:#94a3b8;">Cutout saves as white-backdrop RGB PNG (+ alpha sidecar) so #19 MSR never turns edges black.</div>
          <div style="display:flex;gap:8px;">
            <button type="button" class="btn" id="btnCharMaskKeepBoth">➕ Save &amp; Keep Both</button>
            <button type="button" class="btn btn-primary" id="btnCharMaskSaveApply">💾 Save Cutout &amp; Apply</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(ov);
    _bindModalEvents(ov);
    return ov;
  }

  function _pushHistory() {
    if (!_modalState) return;
    const { maskCanvas, undoStack, redoStack } = _modalState;
    const mctx = maskCanvas.getContext("2d");
    undoStack.push(mctx.getImageData(0, 0, maskCanvas.width, maskCanvas.height));
    if (undoStack.length > 25) undoStack.shift();
    redoStack.length = 0;
  }

  function _renderCanvas() {
    if (!_modalState || !_modalState.sourceImg) return;
    const st = _modalState;
    const vc = document.getElementById("charMaskViewCanvas");
    const gc = document.getElementById("charMaskGuideCanvas");
    if (!vc || !gc) return;
    const w = st.width, h = st.height;
    if (vc.width !== w || vc.height !== h) {
      vc.width = w; vc.height = h; gc.width = w; gc.height = h;
    }
    const ctx = vc.getContext("2d");
    const mCtx = st.maskCanvas.getContext("2d");
    const srcCanvas = st.sourceCanvas;

    if (st.viewMode === "msr" && st.msrCanvas) {
      ctx.drawImage(st.msrCanvas, 0, 0, w, h);
    } else if (st.viewMode === "white" || st.viewMode === "checker" || st.viewMode === "split") {
      // Build composited cutout onto scratch canvas
      const fgData = srcCanvas.getContext("2d").getImageData(0, 0, w, h);
      const mkData = mCtx.getImageData(0, 0, w, h).data;
      const out = ctx.createImageData(w, h);
      const fd = fgData.data, od = out.data;
      const onWhite = st.viewMode === "white";
      for (let i = 0; i < fd.length; i += 4) {
        const a = mkData[i] / 255;
        if (onWhite) {
          od[i] = Math.round(fd[i] * a + 255 * (1 - a));
          od[i + 1] = Math.round(fd[i + 1] * a + 255 * (1 - a));
          od[i + 2] = Math.round(fd[i + 2] * a + 255 * (1 - a));
          od[i + 3] = 255;
        } else {
          od[i] = fd[i]; od[i + 1] = fd[i + 1]; od[i + 2] = fd[i + 2]; od[i + 3] = mkData[i];
        }
      }
      ctx.clearRect(0, 0, w, h);
      ctx.putImageData(out, 0, 0);
      if (st.viewMode === "split") {
        const sx = Math.round((st.splitPct / 100) * w);
        ctx.save();
        ctx.beginPath(); ctx.rect(0, 0, sx, h); ctx.clip();
        ctx.drawImage(srcCanvas, 0, 0, w, h);
        ctx.restore();
        ctx.strokeStyle = "#38bdf8";
        ctx.lineWidth = Math.max(2, Math.round(w / 320));
        ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke();
      }
    } else {
      // Default: Red Mask Tint ("tint")
      const fgData = srcCanvas.getContext("2d").getImageData(0, 0, w, h);
      const mkData = mCtx.getImageData(0, 0, w, h).data;
      const out = ctx.createImageData(w, h);
      const fd = fgData.data, od = out.data;
      for (let i = 0; i < fd.length; i += 4) {
        const cut = 1 - mkData[i] / 255;
        const mix = cut * 0.58;
        od[i] = Math.round(fd[i] * (1 - mix) + 225 * mix);
        od[i + 1] = Math.round(fd[i + 1] * (1 - mix) + 29 * mix);
        od[i + 2] = Math.round(fd[i + 2] * (1 - mix) + 72 * mix);
        od[i + 3] = 255;
      }
      ctx.putImageData(out, 0, 0);
    }

    // Draw interactive guides (YOLO boxes, manual Subject Box, SAM points)
    const gctx = gc.getContext("2d");
    gctx.clearRect(0, 0, w, h);
    (st.detections || []).forEach((d) => {
      const [bx0, by0, bx1, by1] = d.box_norm;
      const sel = d.idx === st.selectedDetIdx;
      gctx.strokeStyle = sel ? "#10b981" : "rgba(148, 163, 184, 0.55)";
      gctx.lineWidth = sel ? 3 : 1.5;
      gctx.setLineDash(sel ? [] : [5, 4]);
      gctx.strokeRect(bx0 * w, by0 * h, (bx1 - bx0) * w, (by1 - by0) * h);
      gctx.setLineDash([]);
    });
    if (st.cropBox) {
      const [x0, y0, x1, y1] = st.cropBox;
      gctx.strokeStyle = "#38bdf8";
      gctx.lineWidth = 2.5;
      gctx.strokeRect(x0 * w, y0 * h, (x1 - x0) * w, (y1 - y0) * h);
    }
    (st.points || []).forEach((pt, idx) => {
      const lbl = st.pointLabels[idx];
      const px = pt[0] * w, py = pt[1] * h;
      gctx.beginPath();
      gctx.arc(px, py, Math.max(5, Math.round(w / 110)), 0, Math.PI * 2);
      gctx.fillStyle = lbl > 0 ? "#22c55e" : "#ef4444";
      gctx.fill();
      gctx.lineWidth = 2; gctx.strokeStyle = "#fff"; gctx.stroke();
    });

    const mk = mCtx.getImageData(0, 0, w, h).data;
    let kept = 0;
    for (let i = 0; i < mk.length; i += 4) if (mk[i] >= 128) kept++;
    const pct = ((kept / Math.max(1, w * h)) * 100).toFixed(1);
    const metaEl = document.getElementById("charMaskMetaStatus");
    if (metaEl) metaEl.textContent = `${w}×${h}px • Kept Foreground: ${pct}% • Method: ${st.method || "raw"}${st.dirty ? " • Unsaved edits" : ""}`;
  }

  function _loadImageIntoCanvas(url, cb) {
    const im = new Image();
    im.crossOrigin = "anonymous";
    im.onload = () => cb(im);
    im.onerror = () => cb(null);
    im.src = url;
  }

  async function _loadMaskFromUrl(maskUrl, targetKey = "maskCanvas") {
    if (!_modalState) return;
    return new Promise((resolve) => {
      _loadImageIntoCanvas(`${maskUrl}${maskUrl.includes("?") ? "&" : "?"}t=${Date.now()}`, (im) => {
        if (im && _modalState) {
          const st = _modalState;
          if (targetKey === "msrCanvas") {
            const c = document.createElement("canvas");
            c.width = st.width; c.height = st.height;
            c.getContext("2d").drawImage(im, 0, 0, st.width, st.height);
            st.msrCanvas = c;
          } else {
            const mctx = st.maskCanvas.getContext("2d");
            mctx.drawImage(im, 0, 0, st.width, st.height);
          }
        }
        resolve(Boolean(im));
      });
    });
  }

  function _setBusy(msg) {
    const el = document.getElementById("charMaskBusyText");
    if (el) el.textContent = msg || "";
    document.querySelectorAll(".char-mask-ai-btn, #btnCharMaskSaveApply, #btnCharMaskKeepBoth").forEach((b) => { b.disabled = Boolean(msg); });
  }

  async function _runExtract(method, opts = {}) {
    if (!_modalState) return;
    const st = _modalState;
    _setBusy(`Running ${method}...`);
    try {
      const resp = await fetch("/api/project/characters/mask-extract", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          slug: st.slug,
          entity_id: st.entityId,
          look_id: st.lookId,
          rel_path: st.relPath,
          method,
          det_idx: opts.det_idx ?? st.selectedDetIdx ?? 0,
          crop_box: st.cropBox,
          auto_box: opts.auto_box ?? false,
          text_prompt: document.getElementById("charMaskSamText")?.value || "",
          points: st.points,
          point_labels: st.pointLabels,
          feather_px: Number(document.getElementById("charMaskFeather")?.value || 0),
          refine_edges: Boolean(document.getElementById("chkCharMaskPyMatting")?.checked),
          preview_only: method === "u2net_msr_preview",
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        window.showToast?.(`❌ ${data.error || `HTTP ${resp.status}`}`);
        return;
      }
      if (method === "u2net_msr_preview") {
        await _loadMaskFromUrl(_mediaRef(st.slug, data.cutout_rel), "msrCanvas");
        const msrBtn = document.getElementById("btnViewMsrCompare");
        if (msrBtn) msrBtn.style.display = "inline-block";
        _switchView("msr");
        window.showToast?.("🔮 Loaded #19 MSR U²-Net preview");
      } else {
        _pushHistory();
        await _loadMaskFromUrl(_mediaRef(st.slug, data.mask_rel), "maskCanvas");
        st.method = method;
        st.dirty = true;
        _renderCanvas();
        window.showToast?.(`✓ Extracted mask using ${method}`);
      }
    } catch (err) {
      window.showToast?.(`❌ Extraction failed: ${err.message}`);
    } finally {
      _setBusy("");
    }
  }

  function _switchView(mode) {
    if (!_modalState) return;
    _modalState.viewMode = mode;
    document.querySelectorAll(".char-mask-view-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.view === mode));
    const sl = document.getElementById("charMaskSplitSlider");
    if (sl) sl.style.display = mode === "split" ? "inline-block" : "none";
    _renderCanvas();
  }

  async function _saveCanvasMask(keepBoth) {
    if (!_modalState) return;
    const st = _modalState;
    _setBusy("Saving cutout & mask...");
    try {
      if (!(await _syncDirtyProjectFirst())) return;
      const maskUrl = st.maskCanvas.toDataURL("image/png");
      const resp = await fetch("/api/project/characters/mask-save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          slug: st.slug,
          entity_id: st.entityId,
          look_id: st.lookId,
          rel_path: st.relPath,
          mask_data_url: maskUrl,
          feather_px: Number(document.getElementById("charMaskFeather")?.value || 0),
          refine_edges: false,
          method: st.method || "manual_brush",
          keep_both: Boolean(keepBoth),
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        window.showToast?.(`❌ Save failed: ${data.error || `HTTP ${resp.status}`}`);
        return;
      }
      _overlayCache.clear();
      document.getElementById("charMaskModalOverlay").style.display = "none";
      _modalState = null;
      window.showToast?.(`✓ Saved clean white cutout & mask (${data.cutout_rel})`);
      _applyUpdatedCharsState(data.characters);
    } catch (err) {
      window.showToast?.(`❌ Save failed: ${err.message}`);
    } finally {
      _setBusy("");
    }
  }

  function _bindModalEvents(ov) {
    const close = () => { ov.style.display = "none"; _modalState = null; };
    ov.querySelector("#btnCloseCharMaskModal").onclick = close;
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });

    ov.querySelectorAll(".char-mask-view-btn").forEach((b) => b.addEventListener("click", () => _switchView(b.dataset.view)));
    ov.querySelector("#charMaskSplitSlider").addEventListener("input", (e) => {
      if (_modalState) { _modalState.splitPct = Number(e.target.value); _renderCanvas(); }
    });
    ov.querySelectorAll(".char-mask-tool-btn").forEach((b) => b.addEventListener("click", () => {
      if (!_modalState) return;
      _modalState.tool = b.dataset.tool;
      ov.querySelectorAll(".char-mask-tool-btn").forEach((x) => x.classList.toggle("is-active", x === b));
    }));
    ov.querySelector("#charMaskBrushSize").addEventListener("input", (e) => { ov.querySelector("#charMaskBrushVal").textContent = e.target.value; });
    ov.querySelector("#charMaskFeather").addEventListener("input", (e) => { ov.querySelector("#charMaskFeatherVal").textContent = e.target.value; });

    ov.querySelector("#btnCharMaskUndo").onclick = () => {
      if (!_modalState || !_modalState.undoStack.length) return;
      const mctx = _modalState.maskCanvas.getContext("2d");
      _modalState.redoStack.push(mctx.getImageData(0, 0, _modalState.width, _modalState.height));
      mctx.putImageData(_modalState.undoStack.pop(), 0, 0);
      _renderCanvas();
    };
    ov.querySelector("#btnCharMaskRedo").onclick = () => {
      if (!_modalState || !_modalState.redoStack.length) return;
      const mctx = _modalState.maskCanvas.getContext("2d");
      _modalState.undoStack.push(mctx.getImageData(0, 0, _modalState.width, _modalState.height));
      mctx.putImageData(_modalState.redoStack.pop(), 0, 0);
      _renderCanvas();
    };
    ov.querySelector("#btnCharMaskInvert").onclick = () => {
      if (!_modalState) return;
      _pushHistory();
      const mctx = _modalState.maskCanvas.getContext("2d");
      const id = mctx.getImageData(0, 0, _modalState.width, _modalState.height);
      for (let i = 0; i < id.data.length; i += 4) { const v = 255 - id.data[i]; id.data[i] = id.data[i + 1] = id.data[i + 2] = v; }
      mctx.putImageData(id, 0, 0);
      _modalState.dirty = true;
      _renderCanvas();
    };
    ov.querySelector("#btnCharMaskReset").onclick = () => {
      if (!_modalState) return;
      _pushHistory();
      const mctx = _modalState.maskCanvas.getContext("2d");
      mctx.fillStyle = "#ffffff";
      mctx.fillRect(0, 0, _modalState.width, _modalState.height);
      _modalState.dirty = true;
      _renderCanvas();
    };
    ov.querySelector("#btnCharMaskClearGuides").onclick = () => {
      if (!_modalState) return;
      _modalState.cropBox = null; _modalState.points = []; _modalState.pointLabels = []; _modalState.detections = [];
      const pills = document.getElementById("charMaskDetPills");
      if (pills) { pills.innerHTML = ""; pills.style.display = "none"; }
      _renderCanvas();
    };

    const gc = ov.querySelector("#charMaskGuideCanvas");
    const _getCanvasPos = (ev) => {
      const rect = gc.getBoundingClientRect();
      const sx = gc.width / Math.max(1, rect.width);
      const sy = gc.height / Math.max(1, rect.height);
      return { x: (ev.clientX - rect.left) * sx, y: (ev.clientY - rect.top) * sy };
    };
    const _dabBrush = (x, y, prevX, prevY, isKeep) => {
      const mctx = _modalState.maskCanvas.getContext("2d");
      const r = Number(document.getElementById("charMaskBrushSize")?.value || 32);
      mctx.strokeStyle = isKeep ? "#ffffff" : "#000000";
      mctx.fillStyle = isKeep ? "#ffffff" : "#000000";
      mctx.lineCap = "round";
      mctx.lineWidth = r;
      mctx.beginPath();
      mctx.moveTo(prevX ?? x, prevY ?? y);
      mctx.lineTo(x, y);
      mctx.stroke();
      mctx.beginPath();
      mctx.arc(x, y, r / 2, 0, Math.PI * 2);
      mctx.fill();
    };

    let dragMode = null, startPt = null, lastPt = null;
    gc.addEventListener("pointerdown", (ev) => {
      if (!_modalState) return;
      gc.setPointerCapture(ev.pointerId);
      const p = _getCanvasPos(ev);
      if (_modalState.tool === "keep" || _modalState.tool === "erase") {
        _pushHistory();
        dragMode = "brush";
        _dabBrush(p.x, p.y, p.x, p.y, _modalState.tool === "keep");
        lastPt = p;
        _modalState.dirty = true;
        _modalState.method = `${_modalState.method || "manual"}+brush`;
        _renderCanvas();
      } else if (_modalState.tool === "box") {
        dragMode = "box"; startPt = p;
      } else if (_modalState.tool === "pt_pos" || _modalState.tool === "pt_neg") {
        _modalState.points.push([p.x / _modalState.width, p.y / _modalState.height]);
        _modalState.pointLabels.push(_modalState.tool === "pt_pos" ? 1 : 0);
        _renderCanvas();
      }
    });
    gc.addEventListener("pointermove", (ev) => {
      if (!_modalState || !dragMode) return;
      const p = _getCanvasPos(ev);
      if (dragMode === "brush") {
        _dabBrush(p.x, p.y, lastPt?.x, lastPt?.y, _modalState.tool === "keep");
        lastPt = p;
        _renderCanvas();
      } else if (dragMode === "box" && startPt) {
        _modalState.cropBox = [
          Math.min(startPt.x, p.x) / _modalState.width, Math.min(startPt.y, p.y) / _modalState.height,
          Math.max(startPt.x, p.x) / _modalState.width, Math.max(startPt.y, p.y) / _modalState.height,
        ];
        _renderCanvas();
      }
    });
    gc.addEventListener("pointerup", () => { dragMode = null; startPt = null; lastPt = null; });

    ov.querySelector("#btnRunYoloSplit").onclick = async () => {
      if (!_modalState) return;
      _setBusy("Detecting people (YOLOv8m-seg)...");
      try {
        const resp = await fetch("/api/project/characters/mask-detect", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slug: _modalState.slug, entity_id: _modalState.entityId, look_id: _modalState.lookId, rel_path: _modalState.relPath }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.success || !(data.detections || []).length) {
          window.showToast?.(`⚠️ ${data.error || "No people detected in image"}`);
          return;
        }
        _modalState.detections = data.detections;
        _modalState.selectedDetIdx = 0;
        const pills = document.getElementById("charMaskDetPills");
        pills.style.display = "flex";
        pills.innerHTML = data.detections.map((d) => `<button type="button" class="btn-mini char-det-pill" data-det-idx="${d.idx}">Person #${d.idx + 1} (${Math.round(d.conf * 100)}%)</button>`).join("");
        pills.querySelectorAll(".char-det-pill").forEach((pb) => pb.addEventListener("click", () => {
          _modalState.selectedDetIdx = Number(pb.dataset.detIdx);
          _runExtract("yolo_person", { det_idx: _modalState.selectedDetIdx });
        }));
        await _runExtract("yolo_person", { det_idx: 0 });
      } finally { _setBusy(""); }
    };
    ov.querySelector("#btnRunSam3Cut").onclick = () => _runExtract("sam3");
    ov.querySelector("#btnRunBirefnetCut").onclick = () => _runExtract("birefnet", { auto_box: !_modalState?.cropBox });
    ov.querySelector("#btnRunMsrPreview").onclick = () => _runExtract("u2net_msr_preview");

    ov.querySelector("#btnRunAiIsolate").onclick = async () => {
      if (!_modalState) return;
      const st = _modalState;
      _setBusy("Queueing AI De-Occlude Take...");
      try {
        if (!(await _syncDirtyProjectFirst())) return;
        const renderer = document.getElementById("charMaskIsolateRenderer")?.value || "1";
        const resp = await fetch("/api/project/characters/isolate-ai", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slug: st.slug, entity_id: st.entityId, look_id: st.lookId,
            source_rel: st.sourceRel || st.relPath,
            prompt: document.getElementById("charMaskIsolatePrompt")?.value || "",
            renderer: /^\d+$/.test(renderer) ? parseInt(renderer, 10) : renderer,
            backend: renderer.startsWith("fal_") ? "fal" : "",
            resolution: document.getElementById("charMaskIsolateRes")?.value || "1024x1024",
          }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.success) {
          window.showToast?.(`❌ Isolate setup failed: ${data.error || resp.status}`);
          return;
        }
        close();
        window.showToast?.("🪄 AI De-Occlude queued — check Takes strip when done!");
        await window.triggerRender?.(["--anchors-only", data.target_token]);
      } finally { _setBusy(""); }
    };

    ov.querySelector("#btnCharMaskSaveApply").onclick = () => _saveCanvasMask(false);
    ov.querySelector("#btnCharMaskKeepBoth").onclick = () => _saveCanvasMask(true);
  }

  async function openCharMaskModal(entityId, lookId, relPath, opts = {}) {
    const ov = _ensureModalDom();
    const slug = window.state?.currentSlug || "";
    const ent = window.state?.charactersData?.[entityId] || {};
    const look = ent?.looks?.[lookId] || {};
    ov.style.display = "flex";
    document.getElementById("charMaskHeaderFile").textContent = `${ent.name || entityId} (@${lookId}) — ${relPath}`;
    document.getElementById("btnViewMsrCompare").style.display = "none";
    document.getElementById("charMaskDetPills").style.display = "none";

    const isoSel = document.getElementById("charMaskIsolateRenderer");
    if (isoSel && typeof window.appendGroupedModelOptions === "function" && Object.keys(window.state?.models?.anchor_renderers || {}).length) {
      const prev = isoSel.value || String(look.isolate_spec?.renderer || "1");
      isoSel.innerHTML = "";
      window.appendGroupedModelOptions(isoSel, window.state.models.anchor_renderers, window.state.models.fal_anchor_renderers);
      if (Array.from(isoSel.options).some((o) => o.value === prev)) isoSel.value = prev;
    }
    const traits = String(ent.physical_traits || ent.name || "the main subject").trim();
    const ldesc = String(look.description || look.name || "").trim();
    document.getElementById("charMaskIsolatePrompt").value =
      `Extract and isolate only ${traits}${ldesc ? ` (${ldesc})` : ""} from <image1>, preserving exact face, hair, expression, and clothing details. Center the subject alone on a solid pure white studio background with even lighting, removing all other people and overlapping hands.`;

    _setBusy("Loading reference & mask info...");
    let info = {};
    try {
      const resp = await fetch("/api/project/characters/mask-info", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, entity_id: entityId, look_id: lookId, rel_path: relPath }),
      });
      info = await resp.json().catch(() => ({}));
    } catch (e) {}

    const srcRel = info.source_rel || relPath;
    const mods = info.models || {};
    document.getElementById("tagCkptYolo").textContent = mods.yolo_person ? "On Disk" : "Missing";
    document.getElementById("tagCkptSam3").textContent = mods.sam3 ? "On Disk" : "Auto-DL";
    document.getElementById("tagCkptBirefnet").textContent = mods.birefnet ? "On Disk" : "Auto-DL";
    document.getElementById("tagCkptU2net").textContent = mods.u2net ? "On Disk" : "Missing";

    _loadImageIntoCanvas(_mediaRef(slug, srcRel), async (srcImg) => {
      if (!srcImg) { _setBusy(""); window.showToast?.("❌ Failed loading source image."); return; }
      const w = srcImg.naturalWidth || 512, h = srcImg.naturalHeight || 512;
      const srcCanvas = document.createElement("canvas");
      srcCanvas.width = w; srcCanvas.height = h;
      srcCanvas.getContext("2d").drawImage(srcImg, 0, 0, w, h);

      const maskCanvas = document.createElement("canvas");
      maskCanvas.width = w; maskCanvas.height = h;
      const mctx = maskCanvas.getContext("2d");
      mctx.fillStyle = "#ffffff";
      mctx.fillRect(0, 0, w, h);

      _modalState = {
        slug, entityId, lookId, relPath, sourceRel: srcRel,
        sourceImg: srcImg, sourceCanvas: srcCanvas, maskCanvas, msrCanvas: null,
        width: w, height: h, viewMode: "tint", splitPct: 50, tool: "keep",
        cropBox: null, points: [], pointLabels: [], detections: [], selectedDetIdx: 0,
        undoStack: [], redoStack: [], method: info.meta?.method || "raw", dirty: false,
      };
      if (info.mask_rel) await _loadMaskFromUrl(_mediaRef(slug, info.mask_rel), "maskCanvas");
      _renderCanvas();
      _setBusy("");
      if (opts.autoMsrPreview) _runExtract("u2net_msr_preview");
    });
  }
  window.openCharMaskModal = openCharMaskModal;
})();
