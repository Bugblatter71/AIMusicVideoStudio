/**
 * Music Video Generation Studio - Duplicate Scene & Take Copy Handler
 * Duplicates a scene immediately after the original in timeline order and clones its vault takes.
 */
(function () {
  "use strict";

  async function duplicateScene(idx) {
    const scenes = window.state?.projectData?.scenes || [];
    const slug = window.state?.currentSlug;
    if (idx === null || idx === undefined || idx < 0 || idx >= scenes.length || !slug) return false;

    const sc = scenes[idx];
    const isLast = idx === scenes.length - 1;
    if (sc.locked && !isLast) {
      window.showToast?.(`🔒 Scene ${sc.id} is locked — unlock it first (duplicating shares its timeline slot)`);
      return false;
    }
    const dur = parseFloat(sc.end) - parseFloat(sc.start);
    if (!isLast && (Number.isNaN(dur) || dur < 0.2)) {
      window.showToast?.("Scene is too short to share its timeline slot with a duplicate (< 0.2s)");
      return false;
    }
    if (sc._dupPending) return false;

    window.commitEditSession?.();
    if (window.state?.isDirty && typeof window.saveProjectData === "function") {
      const saved = await window.saveProjectData(false);
      if (!saved) return false;
    }

    const activeScenes = window.state?.projectData?.scenes || [];
    const targetSc = activeScenes[idx] || sc;
    const inspBtn = document.getElementById("btnDuplicateScene");
    targetSc._dupPending = true;
    if (inspBtn) inspBtn.disabled = true;

    try {
      const resp = await fetch("/api/scene/duplicate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, scene_id: targetSc.id }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        const msg = data.error || `HTTP ${resp.status}`;
        window.showToast?.(`❌ Failed to duplicate Scene ${targetSc.id}: ${msg}`);
        return false;
      }

      if (Array.isArray(data.scenes)) {
        window.state.projectData.scenes = data.scenes;
      }
      const dupSc = data.duplicated_scene || {};
      const dupIdx = (window.state.projectData.scenes || []).findIndex((s) => s.id === dupSc.id);

      window.recordHistorySnapshot?.(`Duplicate Scene ${targetSc.id} → ${dupSc.id || "Copy"}`);
      window.patchSavedScenesArrayBaseline?.((arr) => {
        arr.splice(0, arr.length, ...JSON.parse(JSON.stringify(data.scenes || [])));
      });

      if (Array.isArray(data.queue)) {
        if (typeof window.applyQueueApiResponse === "function") window.applyQueueApiResponse(data);
        else window.state.renderQueue = data.queue;
      }
      window.renderQueueUI?.();

      if (typeof window.refreshProjectAssets === "function") {
        await window.refreshProjectAssets(true);
      }
      window.redrawTimeline?.();
      const nextSelIdx = dupIdx >= 0 ? dupIdx : Math.min(idx + 1, (window.state.projectData.scenes || []).length - 1);
      window.selectScene?.(nextSelIdx);
      window.renderSceneList?.(true);
      if (typeof window.loadProjectQueue === "function") {
        await window.loadProjectQueue(slug);
      }

      const nTakes = Number(data.copied_takes_count || 0);
      const takesNote = nTakes > 0 ? ` • ${nTakes} take${nTakes === 1 ? "" : "s"} copied` : "";
      const sVal = Number(dupSc.start ?? 0).toFixed(1);
      const eVal = Number(dupSc.end ?? 0).toFixed(1);
      window.showToast?.(`📋 Duplicated ${targetSc.id} → ${dupSc.id} (${sVal}s–${eVal}s${takesNote})`);
      return true;
    } catch (err) {
      window.showToast?.(`❌ Error duplicating Scene ${targetSc.id}: ${err.message}`);
      return false;
    } finally {
      delete targetSc._dupPending;
      if (inspBtn) inspBtn.disabled = false;
    }
  }

  function collectPendingPurgeSceneIds() {
    const scenes = window.state?.projectData?.scenes || [];
    const seen = new Set();
    const out = [];
    scenes.forEach((s) => {
      if (s && s._freshSceneTakes && s.id) {
        const sid = String(s.id).trim();
        const u = sid.toUpperCase();
        if (sid && !seen.has(u)) {
          seen.add(u);
          out.push(sid);
        }
      }
    });
    return out;
  }

  function purgeLocalSceneAssetsAfterSave(purgedIds = []) {
    const targets = new Set((purgedIds || []).map((x) => String(x || "").trim().toUpperCase()).filter(Boolean));
    (window.state?.projectData?.scenes || []).forEach((s) => {
      if (s && s._freshSceneTakes) delete s._freshSceneTakes;
    });
    if (!targets.size) return;
    const matchKey = (k) => {
      const u = String(k || "").trim().toUpperCase();
      for (const sidU of targets) {
        if (u === sidU || u.startsWith(`${sidU}_`) || u.startsWith(`${sidU}.`)) return true;
      }
      return false;
    };
    const clips = window.state?.assets?.clips;
    if (clips && typeof clips === "object") {
      Object.keys(clips).forEach((k) => { if (matchKey(k)) delete clips[k]; });
    }
    const strips = window.state?.assets?.strips;
    if (strips && typeof strips === "object") {
      Object.keys(strips).forEach((k) => { if (matchKey(k)) delete strips[k]; });
    }
    if (typeof window.canvasImageCache === "object" && window.canvasImageCache) {
      Object.keys(window.canvasImageCache).forEach((url) => {
        for (const sidU of targets) {
          if (url.toUpperCase().includes(`/STRIP/${encodeURIComponent(sidU)}`)) delete window.canvasImageCache[url];
        }
      });
    }
    for (const sid of purgedIds || []) window.invalidateSceneTakesCache?.(sid);
  }

  window.collectPendingPurgeSceneIds = collectPendingPurgeSceneIds;
  window.purgeLocalSceneAssetsAfterSave = purgeLocalSceneAssetsAfterSave;
  window.duplicateScene = duplicateScene;
  document.getElementById("btnDuplicateScene")?.addEventListener("click", () => {
    if (window.state?.selectedSceneIdx !== null && window.state?.selectedSceneIdx !== undefined) {
      duplicateScene(window.state.selectedSceneIdx);
    }
  });
})();
