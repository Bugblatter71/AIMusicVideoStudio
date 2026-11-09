/**
 * Characters & Reference Library Studio Module (characters_studio.js)
 * Implements:
 * 1. Character roster & master-detail look management
 * 2. In-browser reference image upload & clip frame extraction
 * 3. Caret-aware @mention autocomplete popover
 * 4. Reactive Attached References chip tray for Scene Inspector
 */

(function () {
  let currentEntityId = null;
  let activeAutocompleteIndex = 0;
  let activeAutocompleteMatches = [];
  let activeTextarea = null;

  let popoverEl = document.getElementById("promptMentionPopover");
  if (!popoverEl) {
    popoverEl = document.createElement("div");
    popoverEl.id = "promptMentionPopover";
    popoverEl.style.cssText =
      "position: absolute; display: none; z-index: 1000; background: #0f172a; border: 1px solid #334155; " +
      "border-radius: 8px; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.7); max-height: 240px; overflow-y: auto; " +
      "min-width: 260px; font-family: sans-serif;";
    document.body.appendChild(popoverEl);
  }

  // ---------------------------------------------------------------------------
  // 1. Data Loading & API Sync
  // ---------------------------------------------------------------------------

  async function loadProjectCharacters(slug) {
    const projSlug = slug || state.currentSlug;
    if (!projSlug) return;
    try {
      const res = await fetch(`/api/project/${projSlug}/characters`);
      if (!res.ok) return;
      const data = await res.json();
      state.charactersData = data.characters || {};
      state.characterAutocomplete = data.autocomplete || [];
      state.characterSceneUsage = data.scene_usage || {};
      if (state.projectData) {
        const wasDirty = Boolean(state.isDirty);
        state.projectData.characters = state.charactersData;
        if (!wasDirty && typeof window.markHistorySaved === "function") {
          window.markHistorySaved();
        }
      }

      if (!currentEntityId && Object.keys(state.charactersData).length > 0) {
        currentEntityId = Object.keys(state.charactersData)[0];
      }

      if (document.getElementById("tab-characters")?.classList.contains("active")) {
        renderCharactersTab();
      }
      if (state.selectedSceneIdx !== null && state.projectData?.scenes?.[state.selectedSceneIdx]) {
        updatePromptReferenceTray(state.projectData.scenes[state.selectedSceneIdx]);
      }
    } catch (e) {
      console.error("Failed to load project characters:", e);
    }
  }

  // ---------------------------------------------------------------------------
  // 2. Master-Detail Character Studio Tab
  // ---------------------------------------------------------------------------

  function renderCharactersTab() {
    const rosterEl = document.getElementById("characterRosterList");
    const detailEl = document.getElementById("characterDetailPane");
    if (!rosterEl || !detailEl) return;

    const chars = state.charactersData || {};
    const entityIds = Object.keys(chars);

    if (entityIds.length === 0) {
      rosterEl.innerHTML = `<div style="color: var(--text-muted); font-size: 12px; padding: 12px;">No characters or reference entities found.</div>`;
      detailEl.innerHTML = `<div style="color: var(--text-muted); font-size: 13px; text-align: center; padding: 40px 0;">Click "+ New Character / Group" to add a character, ensemble, or prop.</div>`;
      return;
    }

    if (!currentEntityId || !chars[currentEntityId]) {
      currentEntityId = entityIds[0];
    }

    const slug = state.currentSlug || "test_project";
    rosterEl.innerHTML = entityIds
      .map((eid) => {
        const ent = chars[eid];
        const isActive = eid === currentEntityId;
        const kindBadge = escapeHtml((ent.kind || "character").toUpperCase());
        const lookCount = Object.keys(ent.looks || {}).length;
        const usageCount = (state.characterSceneUsage?.[eid] || []).length;
        const avatarUrl = ent.avatar ? `/media/${slug}/ref/${ent.avatar}` : "";

        return `
        <div class="character-roster-card" data-entity-id="${escapeHtml(eid)}" style="display: flex; gap: 10px; align-items: center; padding: 10px; border-radius: 6px; cursor: pointer; border: 1px solid ${
          isActive ? "var(--primary-color)" : "var(--border-color)"
        }; background: ${isActive ? "rgba(59, 130, 246, 0.12)" : "rgba(30, 41, 59, 0.4)"}; transition: all 0.15s ease;">
          <div style="width: 44px; height: 44px; border-radius: 6px; background: #1e293b; overflow: hidden; display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
            ${avatarUrl ? `<img src="${escapeHtml(avatarUrl)}" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.style.display='none'">` : `<span style="font-size: 20px;">👤</span>`}
          </div>
          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px;">
              <span style="font-size: 13px; font-weight: 600; color: var(--text-main); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(ent.name || eid)}</span>
              <span style="font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 3px; background: #334155; color: #94a3b8;">${kindBadge}</span>
            </div>
            <div style="display: flex; gap: 6px; font-size: 11px; color: var(--text-muted);">
              <span>@${escapeHtml(eid)}</span>
              <span>•</span>
              <span>${lookCount} look${lookCount === 1 ? "" : "s"}</span>
              <span>•</span>
              <span>${usageCount} scene${usageCount === 1 ? "" : "s"}</span>
            </div>
          </div>
        </div>`;
      })
      .join("");

    rosterEl.querySelectorAll(".character-roster-card").forEach((card) => {
      card.addEventListener("click", () => {
        currentEntityId = card.dataset.entityId;
        renderCharactersTab();
      });
    });

    renderCharacterDetail(currentEntityId);
  }

  function renderCharacterDetail(eid) {
    const detailEl = document.getElementById("characterDetailPane");
    if (!detailEl) return;
    const ent = state.charactersData?.[eid];
    if (!ent) {
      detailEl.innerHTML = `<div style="color: var(--text-muted); padding: 20px;">Select an entity from the roster.</div>`;
      return;
    }

    const slug = state.currentSlug || "test_project";
    const looks = ent.looks || {};
    const lookKeys = Object.keys(looks);
    const sceneUsages = state.characterSceneUsage?.[eid] || [];

    const looksHtml = lookKeys
      .map((lid) => {
        const look = looks[lid];
        const refs = look.image_refs || [];
        const thumbStrip = refs
          .map((rPath, rIdx) => {
            const url = `/media/${slug}/ref/${rPath}`;
            return `
            <div style="position: relative; width: 72px; height: 90px; border-radius: 4px; overflow: hidden; background: #0f172a; border: 1px solid #334155; flex-shrink: 0;">
              <img src="${escapeHtml(url)}" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.src='';">
              <button class="btn-remove-ref" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}" data-idx="${rIdx}" style="position: absolute; top: 2px; right: 2px; background: rgba(0,0,0,0.7); border: none; color: #ef4444; width: 18px; height: 18px; border-radius: 50%; font-size: 11px; cursor: pointer; display: flex; align-items: center; justify-content: center;">&times;</button>
            </div>`;
          })
          .join("");

        return `
        <div class="look-editor-card" style="border: 1px solid #334155; border-radius: 6px; padding: 12px; margin-bottom: 12px; background: rgba(15, 23, 42, 0.4);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="display: flex; gap: 8px; align-items: center;">
              <span style="font-weight: 600; color: var(--text-main); font-size: 13px;">${escapeHtml(look.name || lid)}</span>
              <code style="color: #38bdf8; font-size: 12px; background: rgba(56, 189, 248, 0.1); padding: 2px 6px; border-radius: 4px;">${escapeHtml(look.tag || "@" + lid)}</code>
            </div>
            <button class="btn-mini btn-delete-look" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}" style="color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3);">🗑 Remove Look</button>
          </div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px;">
            <div>
              <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Look Name</label>
              <input type="text" class="input-look-name" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}" value="${escapeHtml(look.name || "")}" style="width: 100%; box-sizing: border-box;">
            </div>
            <div>
              <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Tag Token</label>
              <input type="text" class="input-look-tag" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}" value="${escapeHtml(look.tag || "@" + lid)}" style="width: 100%; box-sizing: border-box;">
            </div>
          </div>
          <div style="margin-bottom: 8px;">
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Wardrobe & Appearance Description</label>
            <textarea class="input-look-desc" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}" rows="2" style="width: 100%; box-sizing: border-box; font-size: 12px;">${escapeHtml(look.description || "")}</textarea>
          </div>
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
              <span style="font-size: 11px; color: var(--text-muted); font-weight: 600;">Reference Images (${refs.length})</span>
              <div style="display: flex; gap: 6px;">
                <label class="btn-mini" style="cursor: pointer; background: #1e293b; border: 1px solid #475569; padding: 2px 8px; border-radius: 4px; font-size: 11px;">
                  + Upload Image
                  <input type="file" accept="image/*" class="file-upload-input" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}" style="display: none;">
                </label>
                <button type="button" class="btn-mini btn-crop-clip" data-entity="${escapeHtml(eid)}" data-look="${escapeHtml(lid)}">📸 Crop from Clip</button>
              </div>
            </div>
            <div style="display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px;">
              ${thumbStrip || `<span style="font-size: 11px; color: var(--text-muted); padding: 8px 0;">No reference images uploaded for this look.</span>`}
            </div>
          </div>
        </div>`;
      })
      .join("");

    const scenePills = sceneUsages
      .map((sid) => `<button type="button" class="btn-mini btn-jump-scene" data-scene-id="${escapeHtml(sid)}" style="background: rgba(56, 189, 248, 0.15); border: 1px solid #0284c7; color: #38bdf8;">${escapeHtml(sid)}</button>`)
      .join("");

    detailEl.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid var(--border-color); padding-bottom: 10px;">
        <div>
          <span style="font-size: 16px; font-weight: 700; color: var(--text-main);">${escapeHtml(ent.name || eid)}</span>
          <code style="font-size: 12px; color: var(--text-muted); margin-left: 8px;">(id: ${escapeHtml(eid)})</code>
        </div>
        <button class="btn btn-mini" id="btnDeleteCurrentEntity" style="color: #ef4444; border-color: rgba(239, 68, 68, 0.4);">🗑 Delete Entity</button>
      </div>
      <div style="display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 10px; margin-bottom: 12px;">
        <div>
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Display Name</label>
          <input type="text" id="editEntityName" value="${escapeHtml(ent.name || "")}" style="width: 100%; box-sizing: border-box;">
        </div>
        <div>
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Entity Kind</label>
          <select id="editEntityKind" style="width: 100%;">
            <option value="character" ${ent.kind === "character" ? "selected" : ""}>Character (Lead/Actor)</option>
            <option value="ensemble" ${ent.kind === "ensemble" ? "selected" : ""}>Ensemble (Group/Section)</option>
            <option value="object" ${ent.kind === "object" ? "selected" : ""}>Object (Prop/Instrument)</option>
          </select>
        </div>
        <div>
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Default Look</label>
          <select id="editEntityDefaultLook" style="width: 100%;">
            ${lookKeys.map((k) => `<option value="${escapeHtml(k)}" ${ent.default_look === k ? "selected" : ""}>${escapeHtml(looks[k]?.name || k)}</option>`).join("")}
          </select>
        </div>
      </div>
      <div style="margin-bottom: 14px;">
        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 2px;">Permanent Physical Traits</label>
        <input type="text" id="editEntityTraits" value="${escapeHtml(ent.physical_traits || "")}" placeholder="e.g. the exact 30-year-old Black male singer" style="width: 100%; box-sizing: border-box;">
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
        <h3 style="font-size: 13px; font-weight: 700; color: var(--text-main); margin: 0;">Looks & Outfits</h3>
        <button type="button" class="btn-mini" id="btnAddLook">+ Add Look</button>
      </div>
      <div id="looksContainer">${looksHtml}</div>
      <div style="margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--border-color);">
        <span style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 6px;">Appears in Scenes:</span>
        <div style="display: flex; flex-wrap: wrap; gap: 6px;">
          ${scenePills || `<span style="font-size: 11px; color: var(--text-muted);">Not currently tagged in any scene prompt.</span>`}
        </div>
      </div>`;

    document.getElementById("editEntityName")?.addEventListener("input", (e) => {
      ent.name = e.target.value;
      syncCharactersDirty(`Edit ${eid} Name`, `char:${eid}:name`);
    });
    document.getElementById("editEntityKind")?.addEventListener("change", (e) => {
      ent.kind = e.target.value;
      syncCharactersDirty(`Edit ${eid} Kind`);
    });
    document.getElementById("editEntityDefaultLook")?.addEventListener("change", (e) => {
      ent.default_look = e.target.value;
      syncCharactersDirty(`Edit ${eid} Default Look`);
    });
    document.getElementById("editEntityTraits")?.addEventListener("input", (e) => {
      ent.physical_traits = e.target.value;
      syncCharactersDirty(`Edit ${eid} Traits`, `char:${eid}:traits`);
    });

    document.getElementById("btnDeleteCurrentEntity")?.addEventListener("click", () => {
      if (confirm(`Delete entity "${ent.name || eid}"?`)) {
        delete state.charactersData[eid];
        currentEntityId = Object.keys(state.charactersData)[0] || null;
        syncCharactersDirty(`Delete Entity ${eid}`);
        renderCharactersTab();
      }
    });

    document.getElementById("btnAddLook")?.addEventListener("click", () => {
      const lid = prompt("Enter new look ID (e.g. concert, casual, piano):");
      if (!lid) return;
      const cleanLid = lid.toLowerCase().replace(/[^a-z0-9_-]/g, "_");
      ent.looks = ent.looks || {};
      ent.looks[cleanLid] = {
        tag: `@${cleanLid}`,
        name: lid.charAt(0).toUpperCase() + lid.slice(1),
        description: "",
        image_refs: [],
      };
      if (!ent.default_look) ent.default_look = cleanLid;
      syncCharactersDirty(`Add Look ${cleanLid}`);
      renderCharactersTab();
    });

    detailEl.querySelectorAll(".input-look-name").forEach((inp) => {
      inp.addEventListener("input", (e) => {
        const { entity, look } = e.target.dataset;
        if (state.charactersData[entity]?.looks?.[look]) state.charactersData[entity].looks[look].name = e.target.value;
        syncCharactersDirty(`Edit Look ${look} Name`, `char:${entity}:${look}:name`);
      });
    });
    detailEl.querySelectorAll(".input-look-tag").forEach((inp) => {
      inp.addEventListener("input", (e) => {
        const { entity, look } = e.target.dataset;
        if (state.charactersData[entity]?.looks?.[look]) state.charactersData[entity].looks[look].tag = e.target.value;
        syncCharactersDirty(`Edit Look ${look} Tag`, `char:${entity}:${look}:tag`);
      });
    });
    detailEl.querySelectorAll(".input-look-desc").forEach((inp) => {
      inp.addEventListener("input", (e) => {
        const { entity, look } = e.target.dataset;
        if (state.charactersData[entity]?.looks?.[look]) state.charactersData[entity].looks[look].description = e.target.value;
        syncCharactersDirty(`Edit Look ${look} Description`, `char:${entity}:${look}:desc`);
      });
    });
    detailEl.querySelectorAll(".btn-delete-look").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const { entity, look } = e.target.dataset;
        if (confirm(`Remove look "${look}"?`)) {
          delete state.charactersData[entity]?.looks?.[look];
          syncCharactersDirty(`Delete Look ${look}`);
          renderCharactersTab();
        }
      });
    });

    detailEl.querySelectorAll(".btn-remove-ref").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const { entity, look, idx } = e.target.dataset;
        const arr = state.charactersData[entity]?.looks?.[look]?.image_refs;
        if (arr) {
          arr.splice(parseInt(idx), 1);
          syncCharactersDirty("Remove Reference Image");
          renderCharactersTab();
        }
      });
    });

    detailEl.querySelectorAll(".file-upload-input").forEach((inp) => {
      inp.addEventListener("change", async (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        const { entity, look } = e.target.dataset;
        const reader = new FileReader();
        reader.onload = async () => {
          try {
            const res = await fetch("/api/project/characters/upload-image", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                slug: state.currentSlug,
                entity_id: entity,
                look_id: look,
                filename: file.name,
                data_url: reader.result,
              }),
            });
            const d = await res.json();
            if (d.success && d.rel_path) {
              state.charactersData[entity].looks[look].image_refs = state.charactersData[entity].looks[look].image_refs || [];
              state.charactersData[entity].looks[look].image_refs.push(d.rel_path);
              showToast("Reference image uploaded");
              syncCharactersDirty();
              renderCharactersTab();
            }
          } catch (err) {
            console.error("Upload failed", err);
          }
        };
        reader.readAsDataURL(file);
      });
    });

    detailEl.querySelectorAll(".btn-crop-clip").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const { entity, look } = e.target.dataset;
        openCropClipModal(entity, look);
      });
    });

    detailEl.querySelectorAll(".btn-jump-scene").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sid = btn.dataset.sceneId;
        const sidx = state.projectData?.scenes?.findIndex((s) => s.id === sid);
        if (sidx !== undefined && sidx >= 0 && window.selectScene) {
          window.switchTab("timeline");
          window.selectScene(sidx);
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // 3. Crop Reference From Rendered Clip Modal
  // ---------------------------------------------------------------------------

  function openCropClipModal(entityId, lookId) {
    const clipNames = Object.keys(state.assets?.clips || {}).sort();
    const clipOptions = clipNames.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");

    const modal = document.createElement("div");
    modal.id = "cropClipModalOverlay";
    modal.style.cssText =
      "position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); " +
      "display: flex; align-items: center; justify-content: center; z-index: 2000;";

    modal.innerHTML = `
      <div style="background: #0f172a; border: 1px solid #334155; border-radius: 8px; width: 560px; max-width: 90vw; padding: 16px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
          <h3 style="font-size: 15px; color: var(--text-main); margin: 0;">📸 Crop Reference Frame from Clip</h3>
          <button id="btnCloseCropModal" style="background: none; border: none; color: #94a3b8; font-size: 18px; cursor: pointer;">&times;</button>
        </div>
        <div style="margin-bottom: 10px;">
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Select Rendered Clip</label>
          <select id="cropModalClipSelect" style="width: 100%;">
            ${clipOptions || '<option value="">No clips rendered yet</option>'}
          </select>
        </div>
        <div style="width: 100%; height: 260px; background: #000; border-radius: 6px; overflow: hidden; margin-bottom: 10px; display: flex; align-items: center; justify-content: center;">
          <video id="cropModalVideo" controls style="max-width: 100%; max-height: 100%;"></video>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span id="cropModalTimeDisplay" style="font-size: 12px; font-family: monospace; color: var(--text-muted);">Frame time: 0.00s</span>
          <button id="btnCaptureCropFrame" class="btn btn-primary" style="background: #3b82f6;">Capture Frame as Reference</button>
        </div>
      </div>`;

    document.body.appendChild(modal);

    const sel = modal.querySelector("#cropModalClipSelect");
    const vid = modal.querySelector("#cropModalVideo");
    const timeDisp = modal.querySelector("#cropModalTimeDisplay");
    const slug = state.currentSlug;

    function updateVideoSrc() {
      if (sel.value) {
        vid.src = `/media/${slug}/clip/${sel.value}`;
        vid.load();
      }
    }
    updateVideoSrc();
    sel.addEventListener("change", updateVideoSrc);
    vid.addEventListener("timeupdate", () => {
      timeDisp.textContent = `Frame time: ${vid.currentTime.toFixed(2)}s`;
    });
    modal.querySelector("#btnCloseCropModal").addEventListener("click", () => modal.remove());

    modal.querySelector("#btnCaptureCropFrame").addEventListener("click", async () => {
      if (!sel.value) return;
      try {
        const res = await fetch("/api/project/characters/crop-from-clip", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slug: slug,
            entity_id: entityId,
            look_id: lookId,
            clip_name: sel.value,
            timestamp: vid.currentTime,
          }),
        });
        const d = await res.json();
        if (d.success && d.rel_path) {
          state.charactersData[entityId].looks[lookId].image_refs = state.charactersData[entityId].looks[lookId].image_refs || [];
          state.charactersData[entityId].looks[lookId].image_refs.push(d.rel_path);
          showToast("Captured reference frame from clip");
          syncCharactersDirty();
          modal.remove();
          renderCharactersTab();
        }
      } catch (err) {
        console.error("Crop frame failed", err);
      }
    });
  }

  // ---------------------------------------------------------------------------
  // 4. Caret-Aware @mention Autocomplete Popover
  // ---------------------------------------------------------------------------

  function setupPromptAutocomplete() {
    ["scenePrompt", "scenePromptM", "scenePromptF"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener("input", () => handleAutocompleteInput(el));
      el.addEventListener("keydown", (e) => handleAutocompleteKeydown(e, el));
    });

    document.addEventListener("click", (e) => {
      if (popoverEl && !popoverEl.contains(e.target) && e.target !== activeTextarea) {
        popoverEl.style.display = "none";
      }
    });
  }

  function handleAutocompleteInput(textarea) {
    activeTextarea = textarea;
    const pos = textarea.selectionStart;
    const textBefore = textarea.value.substring(0, pos);
    const match = textBefore.match(/@([a-zA-Z0-9_-]*)$/);

    if (!match) {
      popoverEl.style.display = "none";
      return;
    }

    const query = match[1].toLowerCase();
    const items = state.characterAutocomplete || [];
    activeAutocompleteMatches = items.filter((item) => {
      const t = (item.tag || "").toLowerCase();
      const n = (item.name || "").toLowerCase();
      return t.includes(query) || n.includes(query);
    });

    if (activeAutocompleteMatches.length === 0) {
      popoverEl.style.display = "none";
      return;
    }

    activeAutocompleteIndex = 0;
    renderAutocompletePopover(textarea);
  }

  function renderAutocompletePopover(textarea) {
    const slug = state.currentSlug || "test_project";
    const rect = textarea.getBoundingClientRect();
    popoverEl.style.left = `${rect.left + window.scrollX}px`;
    popoverEl.style.top = `${rect.bottom + window.scrollY + 4}px`;
    popoverEl.style.display = "block";

    popoverEl.innerHTML = activeAutocompleteMatches
      .map((item, idx) => {
        const isSel = idx === activeAutocompleteIndex;
        const avatarUrl = item.avatar ? `/media/${slug}/ref/${item.avatar}` : "";
        return `
        <div class="popover-item" data-idx="${idx}" style="display: flex; gap: 8px; align-items: center; padding: 6px 10px; cursor: pointer; background: ${
          isSel ? "rgba(59, 130, 246, 0.25)" : "transparent"
        };">
          <div style="width: 24px; height: 24px; border-radius: 4px; overflow: hidden; background: #1e293b; display: flex; align-items: center; justify-content: center; flex-shrink: 0;">
            ${avatarUrl ? `<img src="${escapeHtml(avatarUrl)}" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.style.display='none'">` : `<span style="font-size: 11px;">👤</span>`}
          </div>
          <div style="flex: 1; min-width: 0;">
            <div style="font-size: 12px; font-weight: 600; color: #f8fafc;">${escapeHtml(item.tag)} <span style="font-size: 10px; color: #94a3b8; font-weight: 400;">${escapeHtml(item.name)}</span></div>
          </div>
        </div>`;
      })
      .join("");

    popoverEl.querySelectorAll(".popover-item").forEach((it) => {
      it.addEventListener("click", () => selectAutocompleteItem(parseInt(it.dataset.idx)));
    });
  }

  function handleAutocompleteKeydown(e, textarea) {
    if (popoverEl.style.display === "none") return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeAutocompleteIndex = (activeAutocompleteIndex + 1) % activeAutocompleteMatches.length;
      renderAutocompletePopover(textarea);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeAutocompleteIndex = (activeAutocompleteIndex - 1 + activeAutocompleteMatches.length) % activeAutocompleteMatches.length;
      renderAutocompletePopover(textarea);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      selectAutocompleteItem(activeAutocompleteIndex);
    } else if (e.key === "Escape") {
      popoverEl.style.display = "none";
    }
  }

  function selectAutocompleteItem(idx) {
    const item = activeAutocompleteMatches[idx];
    if (!item || !activeTextarea) return;

    const pos = activeTextarea.selectionStart;
    const text = activeTextarea.value;
    const match = text.substring(0, pos).match(/@([a-zA-Z0-9_-]*)$/);

    if (match) {
      const start = pos - match[0].length;
      const rep = `${item.tag} `;
      activeTextarea.value = text.substring(0, start) + rep + text.substring(pos);
      activeTextarea.selectionStart = activeTextarea.selectionEnd = start + rep.length;
      activeTextarea.dispatchEvent(new Event("input", { bubbles: true }));
    }

    popoverEl.style.display = "none";
  }

  // ---------------------------------------------------------------------------
  // 5. Reactive "Attached References" Chip Tray
  // ---------------------------------------------------------------------------

  function updatePromptReferenceTray(sc) {
    if (!sc) return;
    const isTwoPass = isTwoPassScene(sc);
    const vrid = sc.video_renderer || sc.renderer || state.projectData?.models?.default_video_renderer || 1;
    const rspec = state.models?.video_renderers?.[String(vrid)] || {};
    const mtype = String(rspec.model_type || "").toLowerCase();
    const supportsRefs = Boolean(
      rspec.supports_image_refs ||
        mtype.includes("ref2va") ||
        mtype.includes("fasth3") ||
        mtype.includes("singularity")
    );

    if (isTwoPass) {
      renderTrayContainer(document.getElementById("refTrayPass1"), sc.prompt_m || "", supportsRefs, sc, "scenePromptM");
      renderTrayContainer(document.getElementById("refTrayPass2"), sc.prompt_f || "", supportsRefs, sc, "scenePromptF");
    } else {
      renderTrayContainer(document.getElementById("refTraySingle"), sc.prompt || "", supportsRefs, sc, "scenePrompt");
    }
  }

  function renderTrayContainer(container, promptText, supportsRefs, sc, textareaId) {
    if (!container) return;
    const chars = state.charactersData || {};
    const matchedTokens = promptText.match(/(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})/gi) || [];
    const distinctEntities = [];
    const seen = new Set();
    const slug = state.currentSlug || "test_project";

    matchedTokens.forEach((tok) => {
      const cleanTok = tok.toLowerCase();
      let matchedDesc = null;

      for (const [cid, cent] of Object.entries(chars)) {
        if (`@${cid}`.toLowerCase() === cleanTok) {
          const dLook = cent.default_look || Object.keys(cent.looks || {})[0];
          matchedDesc = {
            id: cid,
            name: cent.name,
            tag: tok,
            kind: cent.kind,
            look: dLook,
            image_refs: cent.looks?.[dLook]?.image_refs || [],
            avatar: cent.avatar,
            desc: cent.physical_traits,
          };
          break;
        }
        for (const [lid, lobj] of Object.entries(cent.looks || {})) {
          if ((lobj.tag || `@${lid}`).toLowerCase() === cleanTok || (lobj.legacy_var && `{${lobj.legacy_var.toLowerCase()}}` === cleanTok)) {
            matchedDesc = {
              id: cid,
              name: `${cent.name} (${lobj.name || lid})`,
              tag: tok,
              kind: cent.kind,
              look: lid,
              image_refs: lobj.image_refs || [],
              avatar: lobj.image_refs?.[0] || cent.avatar,
              desc: `${cent.physical_traits} - ${lobj.description}`,
            };
            break;
          }
        }
        if (matchedDesc) break;
      }

      if (matchedDesc && !seen.has(matchedDesc.name)) {
        seen.add(matchedDesc.name);
        distinctEntities.push(matchedDesc);
      }
    });

    if (distinctEntities.length === 0) {
      container.innerHTML = "";
      return;
    }

    let totalImages = 0;
    distinctEntities.forEach((e) => (totalImages += (e.image_refs || []).length));

    const headerBadge = supportsRefs
      ? `<div style="display: flex; align-items: center; gap: 6px; font-size: 11px; margin-bottom: 4px;">
          <span style="color: #22c55e; font-weight: 600;">✓ Multimodal Ref2VA Mode (${Math.min(totalImages, 9)}/9 Image Slots)</span>
          ${totalImages > 9 ? '<span style="color: #f59e0b;">⚠️ only first 9 images sent</span>' : ""}
        </div>`
      : `<div style="font-size: 11px; color: #38bdf8; margin-bottom: 4px;">
          ℹ Text-Only Model (Ref Images Bypassed — Expands to Natural Language Description)
        </div>`;

    const dur = Math.max(0, (sc.end || 0) - (sc.start || 0));
    const pacingTip =
      distinctEntities.length > 3 && dur < 8.0
        ? `<div style="font-size: 10px; color: #fbbf24; margin-top: 4px; padding: 2px 6px; background: rgba(245, 158, 11, 0.1); border-radius: 4px;">
            💡 Tip: ${distinctEntities.length} subjects in ${dur.toFixed(1)}s — use &lt;marker&gt; cues to sequence camera pans cleanly.
          </div>`
        : "";

    let picCounter = 2;
    const chipsHtml = distinctEntities
      .map((e) => {
        const assignedPics = [];
        (e.image_refs || []).forEach(() => {
          if (picCounter <= 10) {
            assignedPics.push(`<Picture ${picCounter}>`);
            picCounter++;
          }
        });
        const picsLabel = supportsRefs && assignedPics.length ? assignedPics.join(", ") : "TXT Expansion";
        const avatarUrl = e.avatar ? `/media/${slug}/ref/${e.avatar}` : "";

        return `
        <div class="ref-chip" style="display: flex; align-items: center; gap: 6px; background: rgba(30, 41, 59, 0.8); border: 1px solid #475569; border-radius: 6px; padding: 3px 8px; font-size: 11px;" title="${escapeHtml(e.desc || "")}">
          ${avatarUrl ? `<img src="${escapeHtml(avatarUrl)}" style="width: 18px; height: 18px; border-radius: 3px; object-fit: cover;" onerror="this.style.display='none'">` : ""}
          <span style="font-weight: 600; color: #f8fafc;">${escapeHtml(e.tag)}</span>
          <span style="color: #94a3b8; font-size: 10px;">[${escapeHtml(picsLabel)}]</span>
          <button type="button" class="btn-remove-chip-tag" data-tag="${escapeHtml(e.tag)}" data-textarea="${escapeHtml(textareaId)}" style="background: none; border: none; color: #94a3b8; cursor: pointer; font-size: 12px; padding: 0 2px;" title="Remove tag from prompt">&times;</button>
        </div>`;
      })
      .join("");

    container.innerHTML = `
      <div style="padding: 6px 10px; background: rgba(15, 23, 42, 0.6); border: 1px dashed #334155; border-radius: 6px;">
        ${headerBadge}
        <div style="display: flex; flex-wrap: wrap; gap: 6px;">${chipsHtml}</div>
        ${pacingTip}
      </div>`;

    container.querySelectorAll(".btn-remove-chip-tag").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tagToRemove = btn.dataset.tag;
        const ta = document.getElementById(btn.dataset.textarea);
        if (ta && tagToRemove) {
          ta.value = ta.value.replace(tagToRemove, "").replace(/\s{2,}/g, " ");
          ta.dispatchEvent(new Event("input", { bubbles: true }));
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Global Event Wiring & Exports
  // ---------------------------------------------------------------------------

  // Character edits flow through the global 💾 Save Changes button (saveProjectData persists characters).
  function syncCharactersDirty(desc = "Edit Characters", sessionKey = null) {
    if (state.projectData) state.projectData.characters = state.charactersData;
    if (sessionKey && typeof window.beginEditSession === "function") {
      window.beginEditSession(sessionKey, desc);
      if (typeof window.syncDirtyWithSavedState === "function") window.syncDirtyWithSavedState();
      else if (typeof window.markProjectDirty === "function") window.markProjectDirty(true);
    } else if (typeof window.recordHistorySnapshot === "function") {
      window.recordHistorySnapshot(desc);
    } else if (typeof window.markProjectDirty === "function") {
      window.markProjectDirty(true);
    }
  }

  function initCharactersStudio() {
    setupPromptAutocomplete();

    document.getElementById("sceneVideoRenderer")?.addEventListener("change", () => {
      if (state.selectedSceneIdx !== null && state.projectData?.scenes?.[state.selectedSceneIdx]) {
        updatePromptReferenceTray(state.projectData.scenes[state.selectedSceneIdx]);
      }
    });

    document.getElementById("btnNewCharacter")?.addEventListener("click", () => {
      const name = prompt("Enter character or entity name (e.g. Maya, Brass Section, Vintage Microphone):");
      if (!name) return;
      const cid = name.toLowerCase().replace(/[^a-z0-9_-]/g, "_");
      state.charactersData = state.charactersData || {};
      state.charactersData[cid] = {
        id: cid,
        name: name,
        kind: "character",
        default_look: "default",
        avatar: "",
        physical_traits: name,
        looks: {
          default: {
            tag: `@${cid}`,
            name: "Default Look",
            description: "",
            image_refs: [],
          },
        },
      };
      currentEntityId = cid;
      syncCharactersDirty();
      renderCharactersTab();
    });

    if (state.currentSlug) {
      loadProjectCharacters(state.currentSlug);
    }
  }

  window.loadProjectCharacters = loadProjectCharacters;
  window.renderCharactersTab = renderCharactersTab;
  window.updatePromptReferenceTray = updatePromptReferenceTray;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCharactersStudio);
  } else {
    initCharactersStudio();
  }
})();
