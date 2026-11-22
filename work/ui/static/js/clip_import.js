// External Video Clip Importer (Google Flow / Veo / Pre-rendered MP4s -> Render Vault Take)
(function () {
  "use strict";

  let _fileInput = null;
  let _uploading = false;

  function _currentScene() {
    const idx = window.state?.selectedSceneIdx;
    if (idx === null || idx === undefined) return null;
    return window.state?.projectData?.scenes?.[idx] || null;
  }

  function _syncButtonState(sc) {
    const btn = document.getElementById("btnImportClipTake");
    if (!btn) return;
    if (_uploading) {
      btn.disabled = true;
      btn.textContent = "Importing...";
      return;
    }
    btn.textContent = "📥 Import Clip";
    btn.disabled = Boolean(sc && sc.locked);
  }

  async function _uploadSelectedFile(file) {
    const sc = _currentScene();
    const slug = window.state?.currentSlug;
    if (!sc || !slug || !file) return;

    _uploading = true;
    _syncButtonState(sc);
    try {
      const qs = new URLSearchParams({
        project: slug,
        scene_id: sc.id,
        filename: file.name || "imported_clip.mp4",
        normalize: "1",
      });
      const resp = await fetch(`/api/takes/import?${qs.toString()}`, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        alert("Import clip failed: " + (data.error || `HTTP ${resp.status}`));
        return;
      }

      try {
        const projResp = await fetch(`/api/project/${encodeURIComponent(slug)}`);
        if (projResp.ok) {
          const projData = await projResp.json();
          if (projData.assets && window.state) window.state.assets = projData.assets;
        }
      } catch (_) {}

      await window.loadProjectQueue?.(slug);
      window.renderSceneList?.();
      window.redrawTimeline?.();
      const curSc = _currentScene() || sc;
      window.updateSceneClipPreview?.(curSc);
      await window.updateSceneTakesUI?.(curSc);
      window.updateInspectorSceneHeader?.(curSc);

      const warnMsg = (data.warnings && data.warnings.length) ? ` ⚠️ ${data.warnings[0]}` : "";
      window.showToast?.(`📥 Imported "${file.name}" as active Take for ${curSc.id} (${data.resolution}, ${data.duration_s}s).${warnMsg}`, 6000);
    } catch (err) {
      alert("Import clip failed: " + (err?.message || String(err)));
    } finally {
      _uploading = false;
      _syncButtonState(_currentScene());
    }
  }

  function _ensureImportControls() {
    const previewPane = document.getElementById("sceneClipPreviewPane");
    const badge = document.getElementById("clipStatusBadge");
    if (!previewPane || !badge || document.getElementById("btnImportClipTake")) return;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "btnImportClipTake";
    btn.className = "btn";
    btn.title = "Import an external MP4/MOV/WebM clip (e.g., from Google Flow / Veo) as this scene's active Render Vault take";
    btn.style.cssText = "padding: 2px 9px; font-size: 11px; background: rgba(56, 189, 248, 0.16); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.42); white-space: nowrap;";
    btn.textContent = "📥 Import Clip";

    _fileInput = document.createElement("input");
    _fileInput.type = "file";
    _fileInput.id = "clipImportFileInput";
    _fileInput.accept = ".mp4,.mov,.webm,.mkv,.m4v,video/*";
    _fileInput.style.display = "none";

    btn.addEventListener("click", () => {
      const sc = _currentScene();
      if (!sc || sc.locked || _uploading) return;
      _fileInput.value = "";
      _fileInput.click();
    });

    _fileInput.addEventListener("change", () => {
      const file = _fileInput.files && _fileInput.files[0];
      if (file) _uploadSelectedFile(file);
    });

    const headerRow = badge.parentElement;
    if (headerRow) {
      headerRow.appendChild(btn);
      headerRow.appendChild(_fileInput);
    }
    _syncButtonState(_currentScene());
  }

  document.addEventListener?.("DOMContentLoaded", () => {
    _ensureImportControls();
  });

  const origApplyLock = window.applyInspectorLockState;
  window.applyInspectorLockState = function (sc) {
    if (typeof origApplyLock === "function") origApplyLock(sc);
    _ensureImportControls();
    _syncButtonState(sc);
  };
})();
