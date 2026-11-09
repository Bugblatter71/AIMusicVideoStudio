// ============================================================================
// Render Console: Queue, Status & SSE Terminal Stream
// ============================================================================
function setConsoleView(mode) {
  const isQueue = mode === "queue";
  const queueView = document.getElementById("consoleQueueView");
  const logsView = document.getElementById("consoleLogsView");
  const btnQueue = document.getElementById("btnConsoleQueue");
  const btnLogs = document.getElementById("btnConsoleLogs");
  if (queueView) queueView.style.display = isQueue ? "flex" : "none";
  if (logsView) logsView.style.display = isQueue ? "none" : "flex";
  if (btnQueue) btnQueue.classList.toggle("active", isQueue);
  if (btnLogs) btnLogs.classList.toggle("active", !isQueue);
  if (!isQueue) {
    const term = document.getElementById("terminalOutput");
    if (term) term.scrollTop = term.scrollHeight;
  }
}

function formatActivePhaseLabel(task) {
  if (!task) return "Starting...";
  const isMultiWin = task.window_count && Number(task.window_count) > 1;
  const winPrefix = isMultiWin && task.window_index ? `Window ${task.window_index}/${task.window_count} • ` : "";
  const phaseLabel = task.raw_phase || task.phase || "Starting...";
  const pctVal = isMultiWin && task.window_pct !== undefined && task.window_pct !== null ? `${task.window_pct}%` : task.pct != null ? `${task.pct}%` : "0%";
  if (task.total_steps && Number(task.total_steps) > 0) {
    return `[${winPrefix}${phaseLabel}] Step ${task.step || 0}/${task.total_steps} (${pctVal})`;
  }
  return `[${winPrefix}${phaseLabel}] (${pctVal})`;
}

function formatQueueReason(reason) {
  if (!reason) return "";
  const r = String(reason).trim();
  if (/^PASS 1 RE-RENDERED/i.test(r)) return "Pass 1 re-rendered";
  if (/TIMELINE EXCEEDS RENDER|TIMELINE CHANGED/i.test(r)) return "Timeline changed";
  if (/UP-TO-DATE/i.test(r)) return "Up-to-date";
  if (/MISSING FILE/i.test(r)) return "Missing file";
  if (/PROMPT\/SEED\/SETTINGS/i.test(r)) return "Prompt/settings edited";
  if (/ANCHOR (\w+) EDITED/i.test(r)) return r.replace(/ANCHOR (\w+) EDITED/i, "Anchor $1 edited");
  if (/ANCHOR (\w+) NEWER/i.test(r)) return r.replace(/ANCHOR (\w+) NEWER ON DISK/i, "Anchor $1 newer");
  if (/^EDITED$/i.test(r)) return "Edited";
  if (/SKIPPED/i.test(r)) return "Skipped by user";
  if (/STOPPED/i.test(r)) return "Stopped by user";
  if (/FORCED/i.test(r)) return "Forced";
  return r.replace(/\s*\(\[\d+\.?\d*,\s*\d+\.?\d*\]\s*not covered by\s*\[\d+\.?\d*,\s*\d+\.?\d*\]\)/gi, "").trim();
}

async function loadProjectQueue(slug = state.currentSlug) {
  if (!slug) return;
  try {
    const [qRes, stRes] = await Promise.all([
      fetch(`/api/project/${encodeURIComponent(slug)}/queue`),
      fetch("/api/render/status").catch(() => null),
    ]);
    const json = await qRes.json();
    const st = stRes ? await stRes.json().catch(() => null) : null;
    const engStatus = (st && st.engine_status) || json.engine_status || null;
    const isRunning = Boolean((st && st.running) || (engStatus && engStatus.state === "rendering"));
    state.isEngineRunning = isRunning;

    if (json.success && json.queue) {
      if (!state.sseTaskStatuses) state.sseTaskStatuses = new Map();

      // If engine is not running, clear stale SSE overrides so server queue state is authoritative
      if (!isRunning) {
        state.isEngineRunning = false;
        state.activeRenderTask = null;
        state.sseTaskStatuses.clear();
      }

      // Reconcile with active engine status if present
      if (engStatus && engStatus.state === "rendering" && engStatus.filename) {
        const engFn = engStatus.filename;
        json.queue.forEach((t) => {
          if (t.filename === engFn || t.output_filename === engFn || (t.filename && t.filename.startsWith(engFn.replace(/\.mp4$/i, "")))) {
            t.status = "rendering";
            t.pct = engStatus.pct ?? t.pct ?? 0;
            t.step = engStatus.step ?? t.step ?? 0;
            ["phase", "raw_phase", "total_steps", "window_index", "window_count"].forEach((k) => {
              if (engStatus[k] !== undefined) t[k] = engStatus[k];
            });
          }
        });
      }

      // Reconcile with any buffered SSE task events while engine is actively running
      json.queue.forEach((t) => {
        const sse = isRunning
          ? (state.sseTaskStatuses.get(t.filename) || state.sseTaskStatuses.get(t.output_filename))
          : null;
        if (sse && sse.status === "rendering") {
          t.status = "rendering";
          const ssePct = Number(sse.pct) || 0;
          const sseStep = Number(sse.step) || 0;
          const tPct = Number(t.pct) || 0;
          const tStep = Number(t.step) || 0;
          t.pct = Math.max(tPct, ssePct);
          t.step = Math.max(tStep, sseStep);
          ["phase", "raw_phase", "total_steps", "window_index", "window_count"].forEach((k) => {
            if (sse[k] !== undefined) t[k] = sse[k];
          });
        }
        if (!isRunning && t.status === "rendering") {
          t.status = t.needs_render ? "queued" : "skipped";
        }
      });

      state.renderQueue = json.queue;
      if (json.queue.length > 0 && json.queue[0].calibration_alpha) {
        state.calibrationAlpha = Number(json.queue[0].calibration_alpha) || 1.0;
        if (state.selectedSceneIdx !== null && state.projectData?.scenes?.[state.selectedSceneIdx]) {
          window.updateInspectorSceneHeader?.(state.projectData.scenes[state.selectedSceneIdx]);
        }
      }

      if (isRunning) {
        let activeItem = state.renderQueue.find((t) => t.status === "rendering");
        if (!activeItem && engStatus && engStatus.filename) {
          activeItem = state.renderQueue.find((t) => t.filename === engStatus.filename || t.output_filename === engStatus.filename);
          if (activeItem) activeItem.status = "rendering";
        }
        if (activeItem) {
          state.activeRenderTask = {
            idx: activeItem.idx, total: activeItem.total || state.renderQueue.length,
            filename: activeItem.filename, reason: activeItem.reason,
            model: activeItem.model_type || engStatus?.model,
            frames: activeItem.video_length || engStatus?.frames,
            resolution: activeItem.resolution || engStatus?.resolution,
            pct: activeItem.pct ?? engStatus?.pct ?? state.activeRenderTask?.pct ?? 0,
            window_pct: activeItem.window_pct ?? engStatus?.window_pct ?? state.activeRenderTask?.window_pct,
            phase: activeItem.phase || engStatus?.phase || state.activeRenderTask?.phase,
            raw_phase: activeItem.raw_phase || engStatus?.raw_phase || state.activeRenderTask?.raw_phase,
            step: activeItem.step ?? engStatus?.step ?? state.activeRenderTask?.step,
            total_steps: activeItem.total_steps ?? engStatus?.total_steps ?? state.activeRenderTask?.total_steps,
            window_index: activeItem.window_index ?? engStatus?.window_index ?? state.activeRenderTask?.window_index,
            window_count: activeItem.window_count ?? engStatus?.window_count ?? state.activeRenderTask?.window_count,
          };
          if (!state.renderStartTime) {
            state.renderStartTime = engStatus?.started_at ? engStatus.started_at * 1000 : Date.now();
          }
          if (!state.renderTimerInterval) {
            state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
          }
        }
      }

      renderQueueUI();
    }
  } catch (err) {
    console.warn("Failed to load project queue:", err);
    const tbody = document.getElementById("queueTableBody");
    if (tbody && (!state.renderQueue || state.renderQueue.length === 0)) {
      tbody.innerHTML = '<tr><td colspan="8" style="padding: 16px; text-align: center; color: #f87171;">⚠️ Unable to reach server for render queue. Retrying...</td></tr>';
    }
    setTimeout(() => { if (!state.renderQueue || state.renderQueue.length === 0) loadProjectQueue(slug); }, 1500);
  }
}

function renderQueueUI() {
  const queue = state.renderQueue || [];
  const tbody = document.getElementById("queueTableBody");
  if (!tbody) return;

  const totalCount = queue.length;
  const isEngineRunning = Boolean(
    state.isEngineRunning ||
    (state.activeRenderTask && state.activeRenderTask.status === "rendering") ||
    queue.some((t) => t.status === "rendering")
  );
  const runBtn = document.getElementById("btnRenderAll");
  if (runBtn) {
    runBtn.textContent = isEngineRunning ? "⏹ Stop" : "▶ Run";
    runBtn.classList.toggle("btn-danger", isEngineRunning);
    runBtn.classList.toggle("btn-primary", !isEngineRunning);
    runBtn.title = isEngineRunning ? "Stop active render and pause queue" : "Run pending items in the render queue";
  }
  const renderingCount = queue.filter((t) => t.status === "rendering").length;
  const queuedOnlyCount = queue.filter((t) => t.status === "queued").length;
  const queuedCount = renderingCount + queuedOnlyCount;
  const doneCount = queue.filter((t) => t.status === "completed").length;
  const skippedCount = queue.filter((t) => t.status === "skipped").length;
  const pendingEstSec = queue
    .filter((t) => t.status === "queued" || t.status === "rendering")
    .reduce((acc, t) => acc + (Number(t.estimated_remaining_seconds ?? t.remaining_seconds ?? t.estimated_seconds) || 0), 0);
  const pendingEtaStr = pendingEstSec > 0 && typeof formatDurationEstimate === "function"
    ? ` (Est. ~${formatDurationEstimate(pendingEstSec)}${renderingCount > 0 ? " remaining" : ""})` : "";

  const lblQueued = document.getElementById("lblQueuedTab");
  if (lblQueued) lblQueued.textContent = isEngineRunning ? "Queued" : "Pending";

  [["countAll", totalCount], ["countQueued", queuedCount], ["countDone", doneCount], ["countSkipped", skippedCount]].forEach(([id, val]) => {
    const el = document.getElementById(id); if (el) el.textContent = val;
  });

  // Active Task Hero Card
  const activeTask = state.activeRenderTask;
  const titleEl = document.getElementById("activeTaskTitle");
  const headingEl = document.getElementById("activeTaskHeading");
  const detailsEl = document.getElementById("activeTaskDetails");
  const timerEl = document.getElementById("activeTaskTimer");
  const progContainer = document.getElementById("activeProgressBarContainer");

  if (activeTask) {
    if (headingEl) {
      headingEl.textContent = `⚡ RENDERING TASK [${String(activeTask.idx).padStart(2, "0")}/${activeTask.total || totalCount}]`;
      headingEl.style.color = "#38bdf8";
    }
    if (titleEl) titleEl.textContent = activeTask.filename;
    const actQueueItem = queue.find((x) => x.status === "rendering" || x.filename === activeTask.filename);
    const actRemSec = actQueueItem?.estimated_remaining_seconds ?? actQueueItem?.remaining_seconds ?? actQueueItem?.estimated_seconds;
    const actEtaBadge = actRemSec && typeof formatDurationEstimate === "function"
      ? `<span id="activeTaskEtaBadge" style="background: rgba(16,185,129,0.15); color: #34d399; padding: 2px 6px; border-radius: 4px;">⏱ Rem: ~${formatDurationEstimate(actRemSec)}</span>` : "";
    const queueSummaryBadge = queuedOnlyCount > 0 && pendingEstSec > 0 && typeof formatDurationEstimate === "function"
      ? `<span style="background: rgba(255,255,255,0.06); color: var(--text-muted); padding: 2px 6px; border-radius: 4px;">+${queuedOnlyCount} Queued (Total ~${formatDurationEstimate(pendingEstSec)})</span>` : "";
    if (detailsEl) {
      detailsEl.innerHTML = `
        <span style="background: rgba(56,189,248,0.15); color: #38bdf8; padding: 2px 6px; border-radius: 4px;">${activeTask.model || "GPU Model"}</span>
        <span style="background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px;">${activeTask.frames ? `${activeTask.frames} frames` : ""}</span>
        <span style="background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px;">${activeTask.resolution || ""}</span>
        ${actEtaBadge}${queueSummaryBadge}
        <span style="background: rgba(245,158,11,0.15); color: #f59e0b; padding: 2px 6px; border-radius: 4px;">Reason: ${activeTask.reason || "QUEUED"}</span>
      `;
    }
    if (timerEl) timerEl.style.display = "inline";
    if (progContainer) progContainer.style.display = "block";
    const pb = document.getElementById("activeProgressBar"); if (pb) pb.style.width = `${activeTask.pct || 0}%`;
    const pctText = document.getElementById("activeProgressPct"); if (pctText) pctText.textContent = `${activeTask.pct || 0}%`;
    const phaseText = document.getElementById("activeProgressPhase"); if (phaseText) phaseText.textContent = formatActivePhaseLabel(activeTask);
  } else {
    if (headingEl) { headingEl.textContent = "CURRENT STATUS"; headingEl.style.color = "var(--text-muted)"; }
    if (titleEl) {
      const waitLabel = isEngineRunning ? "waiting in queue" : "pending render";
      titleEl.textContent = queuedCount > 0 ? `${queuedCount} file(s) ${waitLabel}${pendingEtaStr}` : "All files up-to-date";
    }
    if (detailsEl) {
      const qWord = isEngineRunning ? "Queued" : "Pending";
      const qBreakdown = renderingCount > 0 ? `${renderingCount} Rendering + ${queuedOnlyCount} Queued` : `${queuedCount} ${qWord}`;
      detailsEl.innerHTML = `<span id="queueStatsSummary">${totalCount} Total Passes • ${qBreakdown}${pendingEtaStr} • ${doneCount} Finished • ${skippedCount} Up-to-date</span>`;
    }
    if (timerEl) timerEl.style.display = "none";
    if (progContainer) progContainer.style.display = "none";
  }

  // Filter items
  const filter = state.queueFilter || "all";
  const isDoneFilter = filter === "completed";
  const showActions = filter === "queued" || filter === "skipped";
  const showReason = filter !== "completed";

  let filtered = queue.filter((t) => {
    if (filter === "all") return true;
    if (filter === "queued") return t.status === "queued" || t.status === "rendering";
    if (filter === "completed") return t.status === "completed";
    if (filter === "skipped") return t.status === "skipped";
    return true;
  });

  if (isDoneFilter) {
    filtered = [...filtered].sort((a, b) => {
      const timeA = a.completed_at ? Number(a.completed_at) : 0;
      const timeB = b.completed_at ? Number(b.completed_at) : 0;
      if (timeB !== timeA) return timeB - timeA;
      return a.idx - b.idx;
    });
  }

  // Calculate filename & reason column widths based on widest elements in list + padding
  let maxFnWidth = 110, maxReasonWidth = 55;
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.font = "500 11px monospace";
      filtered.forEach((t) => { maxFnWidth = Math.max(maxFnWidth, ctx.measureText(t.filename || t.output_filename || "").width); });
      ctx.font = "10px sans-serif";
      filtered.forEach((t) => { maxReasonWidth = Math.max(maxReasonWidth, ctx.measureText(formatQueueReason(t.reason)).width); });
      ctx.font = "11px sans-serif";
      maxFnWidth = Math.max(maxFnWidth, ctx.measureText("File to Render").width);
      maxReasonWidth = Math.max(maxReasonWidth, ctx.measureText("Reason").width);
    }
  } catch (e) {
    maxFnWidth = Math.max(...filtered.map((t) => (t.filename || "").length), 14) * 7.5;
    maxReasonWidth = Math.max(...filtered.map((t) => formatQueueReason(t.reason).length), 6) * 6.5;
  }
  const fnColWidth = Math.min(Math.max(Math.ceil(maxFnWidth + 22), 120), 320);
  const reasonColWidth = Math.max(Math.ceil(maxReasonWidth + 22), 65);

  const lenColWidth = isDoneFilter ? 58 : 94;
  const thead = document.getElementById("queueTableHead");
  if (thead) {
    thead.innerHTML = `<tr>
      <th style="padding: 6px 10px; width: ${fnColWidth}px; color: var(--text-muted);">File to Render</th>
      <th style="padding: 6px 10px; width: ${lenColWidth}px; color: var(--text-muted);">Length</th>
      <th style="padding: 6px 10px; width: 95px; color: var(--text-muted);">Status</th>
      ${isDoneFilter ? '<th style="padding: 6px 10px; width: 140px; color: var(--text-muted);">Completed</th><th style="padding: 6px 10px; width: 85px; color: var(--text-muted);">Time Taken</th>' : ''}
      ${showReason ? `<th style="padding: 6px 10px; width: ${reasonColWidth}px; color: var(--text-muted);">Reason</th>` : ''}
      ${showActions ? '<th style="padding: 6px 10px; width: 90px; text-align: center; color: var(--text-muted);">Actions</th>' : ''}
      <th style="width: auto;"></th>
    </tr>`;
  }

  if (filtered.length === 0) {
    const filterLabel = (filter === "queued" && !isEngineRunning) ? "pending" : filter;
    const colCount = (isDoneFilter || showActions) ? 6 : 5;
    tbody.innerHTML = `<tr><td colspan="${colCount}" style="padding: 24px; text-align: center; color: var(--text-muted);">No files match filter "${filterLabel}".</td></tr>`;
    return;
  }

  const queuedIndices = queue.map((t, i) => (t.status === "queued" ? i : -1)).filter((i) => i !== -1);
  const firstQueuedIdx = queuedIndices[0] ?? -1;
  const lastQueuedIdx = queuedIndices[queuedIndices.length - 1] ?? -1;

  tbody.innerHTML = filtered
    .map((t) => {
      const isRendering = t.status === "rendering";
      const isCompleted = t.status === "completed";
      const isQueued = t.status === "queued";
      const isFailed = t.status === "failed";

      let statusBadge = "";
      if (isRendering) {
        statusBadge = `<span class="active-row-progress-badge" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); padding: 2px 8px; border-radius: 4px; font-weight: 600; white-space: nowrap;">⚡ Rendering</span>`;
      } else if (isQueued) {
        statusBadge = isEngineRunning
          ? `<span style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); padding: 2px 8px; border-radius: 4px; font-weight: 600;">⏳ Queued</span>`
          : `<span style="background: rgba(245, 158, 11, 0.12); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.25); padding: 2px 8px; border-radius: 4px; font-weight: 600;">⏳ Pending</span>`;
      } else if (isCompleted) {
        statusBadge = `<span style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); padding: 2px 8px; border-radius: 4px; font-weight: 600;">✅ Done</span>`;
      } else if (isFailed) {
        statusBadge = `<span style="background: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); padding: 2px 8px; border-radius: 4px; font-weight: 600;">❌ Failed</span>`;
      } else {
        statusBadge = `<span style="background: rgba(100, 116, 139, 0.15); color: #94a3b8; padding: 2px 8px; border-radius: 4px;">⏩ Skipped</span>`;
      }

      const rowStyle = isRendering
        ? "background: rgba(56, 189, 248, 0.1); border-left: 3px solid #38bdf8;"
        : "border-bottom: 1px solid rgba(255, 255, 255, 0.04);";

      const scMatch = state.projectData?.scenes?.find((s) => s.id === t.scene_id);
      const durSec = t.duration != null ? Number(t.duration) : (scMatch ? Number(scMatch.end - scMatch.start) : 0);
      const lenStr = durSec > 0 ? `${durSec.toFixed(1)}s` : "—";
      const estStr = !isDoneFilter && !isCompleted && (t.estimated_remaining_str || t.estimated_str) ? (t.estimated_remaining_str || t.estimated_str) : "";
      const estBadge = estStr
        ? ` <span style="color: ${t.is_split_window ? "#f59e0b" : "#38bdf8"}; font-size: 10px;" title="Est. Render Time: ~${estStr} (${t.frames || t.quantized_frames || ""}f, ${t.regime || "fast_vram"})">(~${estStr}${t.is_split_window ? "⚠️" : ""})</span>`
        : "";
      const completedStr = isCompleted ? formatCompletedAt(t.completed_at) : (isRendering ? '<span style="color: #38bdf8;">Rendering...</span>' : "—");
      const elapsedStr = isCompleted ? formatElapsed(t.elapsed) : (isRendering && t.elapsed ? formatElapsed(t.elapsed) : "—");
      const shortReason = formatQueueReason(t.reason);
      const targetId = t.filename || t.id || t.output_filename;

      const globalIdx = queue.findIndex((x) => (x.filename || x.output_filename) === targetId || x.idx === t.idx);
      const prevQueuedIdx = isQueued ? [...queuedIndices].reverse().find((i) => i < globalIdx) : undefined;
      const nextQueuedIdx = isQueued ? queuedIndices.find((i) => i > globalIdx) : undefined;
      const blockedUpByPass1 = prevQueuedIdx !== undefined && t.pass === 2 && queue[prevQueuedIdx]?.scene_id === t.scene_id && queue[prevQueuedIdx]?.pass === 1;
      const blockedDownByPass2 = nextQueuedIdx !== undefined && t.pass === 1 && queue[nextQueuedIdx]?.scene_id === t.scene_id && queue[nextQueuedIdx]?.pass === 2;
      const canUp = isQueued && globalIdx > firstQueuedIdx && !blockedUpByPass1;
      const canDown = isQueued && globalIdx < lastQueuedIdx && !blockedDownByPass2;

      let actionsHtml = "";
      if (isRendering) {
        actionsHtml = `<span style="color: #38bdf8; font-size: 10px; font-weight: 600;">Active</span>`;
      } else if (isQueued) {
        const upBtn = `<button type="button" class="btn-queue-action btn-queue-up" data-id="${targetId}" title="Move Up" ${canUp ? "" : "disabled"} style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 3px; opacity: ${canUp ? "1" : "0.3"}; cursor: ${canUp ? "pointer" : "default"};">⬆️</button>`;
        const downBtn = `<button type="button" class="btn-queue-action btn-queue-down" data-id="${targetId}" title="Move Down" ${canDown ? "" : "disabled"} style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 3px; opacity: ${canDown ? "1" : "0.3"}; cursor: ${canDown ? "pointer" : "default"};">⬇️</button>`;
        const delBtn = `<button type="button" class="btn-queue-action btn-queue-del" data-id="${targetId}" title="Remove from Queue" style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(239,68,68,0.3); border-radius: 3px; cursor: pointer; color: #ef4444;">🗑️</button>`;
        actionsHtml = `<div style="display: flex; gap: 4px; justify-content: center; align-items: center;">${upBtn}${downBtn}${delBtn}</div>`;
      } else {
        actionsHtml = `<div style="display: flex; justify-content: center; align-items: center;"><button type="button" class="btn-queue-action btn-queue-add" data-id="${targetId}" title="Add to Queue" style="padding: 1px 6px; font-size: 10px; cursor: pointer; background: rgba(255,255,255,0.06); border: 1px solid var(--border); border-radius: 3px; color: var(--text-muted);">➕ Queue</button></div>`;
      }

      const timeCols = isDoneFilter ? `<td style="padding: 6px 10px; width: 140px; color: var(--text-muted); font-family: monospace; white-space: nowrap;">${completedStr}</td><td style="padding: 6px 10px; width: 85px; color: var(--text-muted); font-family: monospace; white-space: nowrap;">${elapsedStr}</td>` : "";
      const reasonCol = showReason ? `<td style="padding: 6px 10px; width: ${reasonColWidth}px; color: var(--text-muted); font-size: 10px; white-space: nowrap;"><span title="${shortReason}">${shortReason}</span></td>` : "";
      const actionsCol = showActions ? `<td style="padding: 6px 10px; width: 90px; text-align: center; white-space: nowrap;">${actionsHtml}</td>` : "";

      return `<tr style="${rowStyle}">
        <td style="padding: 6px 10px; width: ${fnColWidth}px; font-weight: 500;"><div style="color: ${isRendering ? "#38bdf8" : isCompleted ? "#10b981" : "var(--text-main)"}; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${t.filename}">${t.filename}</div></td>
        <td style="padding: 6px 10px; width: ${lenColWidth}px; color: var(--text-muted); font-family: monospace; white-space: nowrap;">${lenStr}${estBadge}</td>
        <td style="padding: 6px 10px; width: 95px; white-space: nowrap;">${statusBadge}</td>
        ${timeCols}${reasonCol}${actionsCol}<td style="width: auto;"></td>
      </tr>`;
    })
    .join("");
}

function handleQueueSSEEvent(line) {
  try {
    const spaceIdx = line.indexOf(" ");
    if (spaceIdx === -1) return;
    const tag = line.slice(0, spaceIdx);
    const jsonStr = line.slice(spaceIdx + 1);
    const payload = JSON.parse(jsonStr);

    if (!state.sseTaskStatuses) state.sseTaskStatuses = new Map();
    const fnKey = payload.filename || "";
    const stemKey = fnKey.replace(/\.(mp4|jpg|png|webp)$/i, "");
    const findQueueItem = () =>
      state.renderQueue.find(
        (t) =>
          (fnKey && (t.filename === fnKey || t.output_filename === fnKey || t.output_filename === stemKey)) ||
          (!fnKey && t.status === "rendering")
      );

    if (tag === "[QUEUE_ACTIVE]") {
      state.isEngineRunning = true;
      state.activeRenderTask = payload;
      state.renderStartTime = Date.now();
      if (!state.renderTimerInterval) {
        state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
      }
      if (fnKey) state.sseTaskStatuses.set(fnKey, { status: "rendering", payload });
      if (stemKey) state.sseTaskStatuses.set(stemKey, { status: "rendering", payload });

      const match = findQueueItem();
      if (match) {
        match.status = "rendering";
        match.reason = payload.reason || match.reason;
        state.activeRenderTask.idx = match.idx;
        state.activeRenderTask.total = state.renderQueue.length;
      }
      renderQueueUI();
    } else if (tag === "[QUEUE_PROGRESS]") {
      if (state.activeRenderTask) {
        Object.assign(state.activeRenderTask, {
          phase: payload.phase, raw_phase: payload.raw_phase, pct: payload.pct,
          window_pct: payload.window_pct !== undefined ? payload.window_pct : payload.pct,
          step: payload.step, total_steps: payload.total_steps,
          window_index: payload.window_index, window_count: payload.window_count,
        });
      }
      const activeFn = payload.filename || state.activeRenderTask?.filename || "";
      if (activeFn && state.sseTaskStatuses.has(activeFn)) {
        Object.assign(state.sseTaskStatuses.get(activeFn), {
          pct: payload.pct, window_pct: payload.window_pct !== undefined ? payload.window_pct : payload.pct,
          step: payload.step, total_steps: payload.total_steps, phase: payload.phase,
          raw_phase: payload.raw_phase, window_index: payload.window_index, window_count: payload.window_count,
        });
      }
      const match = findQueueItem() || state.renderQueue.find((t) => t.status === "rendering");
      if (match) {
        match.pct = payload.pct;
        match.window_pct = payload.window_pct !== undefined ? payload.window_pct : payload.pct;
        match.phase = payload.phase;
        match.raw_phase = payload.raw_phase;
        match.step = payload.step;
        match.total_steps = payload.total_steps;
        match.window_index = payload.window_index;
        match.window_count = payload.window_count;
      }

      // Direct DOM update for 60fps performance
      const pb = document.getElementById("activeProgressBar");
      if (pb) pb.style.width = `${payload.pct}%`;
      const pctText = document.getElementById("activeProgressPct");
      if (pctText) pctText.textContent = `${payload.pct}%`;
      const phaseText = document.getElementById("activeProgressPhase");
      if (phaseText) {
        phaseText.textContent = formatActivePhaseLabel(payload);
      }
      const rowBadge = document.querySelector(".active-row-progress-badge");
      if (rowBadge) {
        rowBadge.textContent = "⚡ Rendering";
      }
    } else if (tag === "[QUEUE_CANCEL]") {
      state.isEngineRunning = false;
      const match = findQueueItem() || state.renderQueue.find((t) => t.status === "rendering");
      if (match) {
        match.status = "queued";
        match.reason = payload.reason || "STOPPED BY USER";
      }
      state.isEngineRunning = false;
  state.activeRenderTask = null;
      renderQueueUI();
    } else if (tag === "[QUEUE_DONE]") {
      if (fnKey) state.sseTaskStatuses.set(fnKey, { status: "completed", elapsed: payload.elapsed });
      if (stemKey) state.sseTaskStatuses.set(stemKey, { status: "completed", elapsed: payload.elapsed });

      const match = findQueueItem();
      if (match) {
        match.status = "completed";
        match.needs_render = false;
        match.elapsed = payload.elapsed;
        match.completed_at = payload.completed_at || Math.floor(Date.now() / 1000);
      }
      if (state.activeRenderTask && (!fnKey || state.activeRenderTask.filename === fnKey)) {
        state.activeRenderTask = null;
      }
      renderQueueUI();
      refreshProjectAssets(true);
    } else if (tag === "[QUEUE_SKIP]") {
      if (payload.reason === "SKIPPED (NOT TARGETED)") {
        return;
      }
      const match = findQueueItem();
      if (match) {
        match.status = "skipped";
        match.reason = payload.reason || match.reason;
      }
      renderQueueUI();
    } else if (tag === "[QUEUE_FAIL]") {
      if (fnKey) state.sseTaskStatuses.set(fnKey, { status: "failed", error: payload.error });
      if (stemKey) state.sseTaskStatuses.set(stemKey, { status: "failed", error: payload.error });

      const match = findQueueItem();
      if (match) {
        match.status = "failed";
        match.error = payload.error;
      }
      if (state.activeRenderTask && (!fnKey || state.activeRenderTask.filename === fnKey)) {
        state.activeRenderTask = null;
      }
      renderQueueUI();
    }
  } catch (err) {
    console.error("Queue SSE parse error:", err);
  }
}

async function updateRenderTimer() {
  if (!state.renderStartTime) return;
  const elapsedSec = Math.floor((Date.now() - state.renderStartTime) / 1000);
  const m = Math.floor(elapsedSec / 60);
  const s = elapsedSec % 60;
  const timerEl = document.getElementById("activeTaskTimer");
  if (timerEl) {
    timerEl.textContent = `⏱ ${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  const etaBadgeEl = document.getElementById("activeTaskEtaBadge");
  if (etaBadgeEl && state.activeRenderTask && typeof formatDurationEstimate === "function") {
    const actItem = (state.renderQueue || []).find((x) => x.status === "rendering" || x.filename === state.activeRenderTask.filename);
    const baseEst = Number(actItem?.estimated_seconds || 0);
    if (baseEst > 0) {
      const pct = Number(state.activeRenderTask.pct || 0);
      const step = Number(state.activeRenderTask.step || 0);
      let remSec = Math.max(5, baseEst - elapsedSec);
      if (step >= 1 && pct >= 5.0 && elapsedSec > 0) {
        const pFrac = Math.max(0.05, Math.min(0.99, pct / 100.0));
        const w = Math.pow(pFrac, 1.2);
        const blendedTotal = (1.0 - w) * baseEst + w * (elapsedSec / pFrac);
        remSec = Math.max(5, blendedTotal - elapsedSec);
      }
      etaBadgeEl.textContent = `⏱ Rem: ~${formatDurationEstimate(remSec)}`;
    }
  }

  // Continuous engine polling: directly query engine_status.json via /api/render/status every second
  if (elapsedSec > 0 && !state._pollingEngineStatus) {
    state._pollingEngineStatus = true;
    try {
      const res = await fetch("/api/render/status");
      const json = await res.json();
      if (json && json.engine_status && (json.running || json.engine_status.state === "rendering")) {
        const eng = json.engine_status;
        const curStep = state.activeRenderTask?.step || 0;
        const curPct = state.activeRenderTask?.pct || 0;
        const curWinPct = state.activeRenderTask?.window_pct || 0;
        if (!state.activeRenderTask) {
          state.activeRenderTask = { ...eng };
        }
        if (eng.step > curStep || eng.pct > curPct || eng.window_pct !== curWinPct) {
          state.activeRenderTask.step = eng.step;
          state.activeRenderTask.total_steps = eng.total_steps || state.activeRenderTask.total_steps;
          state.activeRenderTask.pct = eng.pct;
          state.activeRenderTask.window_pct = eng.window_pct !== undefined ? eng.window_pct : eng.pct;
          state.activeRenderTask.phase = eng.phase || state.activeRenderTask.phase;
          state.activeRenderTask.raw_phase = eng.raw_phase || state.activeRenderTask.raw_phase;
          state.activeRenderTask.window_index = eng.window_index || state.activeRenderTask.window_index;
          state.activeRenderTask.window_count = eng.window_count || state.activeRenderTask.window_count;

          const pb = document.getElementById("activeProgressBar");
          if (pb) pb.style.width = `${eng.pct}%`;
          const pctText = document.getElementById("activeProgressPct");
          if (pctText) pctText.textContent = `${eng.pct}%`;
          const phaseText = document.getElementById("activeProgressPhase");
          if (phaseText) phaseText.textContent = formatActivePhaseLabel(state.activeRenderTask);
        }
      }
    } catch (e) {
    } finally {
      state._pollingEngineStatus = false;
    }
  }
}

function initLogStream() {
  if (state.eventSource) {
    try {
      state.eventSource.close();
    } catch (e) {}
  }

  const es = new EventSource("/api/render/events");
  const term = document.getElementById("terminalOutput");

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.text) {
        if (data.text.includes("[MASTER_UPDATED]")) {
          showToast("🎬 Master video restitched!");
          refreshProjectAssets(true);
          return;
        }
        if (data.text.includes("[MASTER_LOCKED]")) {
          showToast("⚠️ Master video is locked by a video player. Close your player and restitch.", 9000);
          return;
        }
        if (data.text.includes("[MASTER_ERROR]")) {
          showToast("❌ Master video restitch failed. See terminal logs.", 6000);
          return;
        }
        if (data.text.includes("[ANCHORS_UPDATED]")) {
          showToast("🎨 Anchor image updated!");
          refreshProjectAssets(true);
          return;
        }
        const queueIdx = data.text.indexOf("[QUEUE_");
        if (queueIdx !== -1) {
          const queueText = data.text.slice(queueIdx);
          handleQueueSSEEvent(queueText);
          const prefix = data.text.slice(0, queueIdx).trim();
          if (prefix) {
            const line = document.createElement("div");
            line.textContent = prefix;
            term.appendChild(line);
            term.scrollTop = term.scrollHeight;
          }
          return;
        }
        const line = document.createElement("div");
        line.textContent = data.text;
        term.appendChild(line);
        term.scrollTop = term.scrollHeight;
      }

      if (data.done) {
        resetEngineUI();
        // Refresh project assets & master video without clobbering unsaved UI edits or playhead
        refreshProjectAssets(true);
      }
    } catch (err) {
      console.error("SSE parse error:", err);
    }
  };

  es.onerror = () => {
    console.warn("SSE connection status change or reconnecting...");
  };

  state.eventSource = es;
}

function resetEngineUI() {
  if (state.renderTimerInterval) { clearInterval(state.renderTimerInterval); state.renderTimerInterval = null; }
  state.isEngineRunning = false;
  state.activeRenderTask = null;
  state.sseTaskStatuses?.clear();
  (state.renderQueue || []).forEach((t) => { if (t.status === "rendering") t.status = t.needs_render === false ? "skipped" : "queued"; });
  renderQueueUI();
}

async function triggerRender(args = []) {
  if (state.isDirty) {
    showToast("💾 Auto-saving project changes before render...");
    const saved = await saveProjectData(false);
    if (!saved) {
      alert("Auto-save failed; aborting render so unsaved changes are not ignored.");
      return;
    }
  }
  const term = document.getElementById("terminalOutput");
  term.innerHTML = "";
  state.isEngineRunning = true;

  // Clear previous task state so stale tasks (e.g. S22) never linger
  state.activeRenderTask = null;
  state.renderStartTime = Date.now();
  if (args && args.length > 0) {
    const targetSid = String(args[0]).trim().toUpperCase();
    const targetBase = targetSid.split(":")[0];
    const match = state.renderQueue?.find(
      (t) => (t.scene_id || "").toUpperCase() === targetBase || (t.output_filename || "").toUpperCase().startsWith(`${targetBase}_`) || (t.output_filename || "").toUpperCase() === targetBase
    );
    if (match) {
      state.activeRenderTask = {
        idx: match.idx,
        total: state.renderQueue.length,
        filename: match.filename,
        reason: `TARGETED (${targetSid})`,
        model: match.model_type,
        frames: match.video_length,
        resolution: match.resolution,
      };
    }
  }
  renderQueueUI();

  // Re-establish SSE connection if absent or closed
  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) {
    initLogStream();
  }

  try {
    const res = await fetch("/api/render/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: state.currentSlug, args: args }),
    });
    const json = await res.json();
    if (!json.success) {
      resetEngineUI();
      alert(`Render error: ${json.error}`);
    } else if (json.queued === false) {
      resetEngineUI();
      showToast(`✔ ${json.message || "All passes up-to-date"}`);
      loadProjectQueue(state.currentSlug);
    } else {
      showToast(`⚡ ${json.message || "Render started"}`);
      loadProjectQueue(state.currentSlug);
    }
  } catch (err) {
    resetEngineUI();
    alert(`Failed to start render: ${err}`);
  }
}
window.triggerRender = triggerRender;
window.loadProjectQueue = loadProjectQueue;

document.getElementById("btnConsoleQueue")?.addEventListener("click", () => setConsoleView("queue"));
document.getElementById("btnConsoleLogs")?.addEventListener("click", () => setConsoleView("logs"));

document.querySelectorAll(".btn-filter[data-filter]").forEach((b) => {
  b.addEventListener("click", (e) => {
    document.querySelectorAll(".btn-filter[data-filter]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.queueFilter = b.dataset.filter;
    renderQueueUI();
  });
});

document.getElementById("btnQueueAll")?.addEventListener("click", async () => {
  try {
    if (state.isDirty) await saveProjectData(false);
    const slug = state.currentSlug || "all_or_nothing";
    const res = await fetch("/api/queue/enqueue_all", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug }) });
    const data = await res.json();
    if (data.queue) { state.sseTaskStatuses?.clear(); state.renderQueue = data.queue; renderQueueUI(); showToast("All unrendered items enqueued"); }
  } catch (err) { showToast("Failed to queue all items"); }
});

document.getElementById("btnClearQueue")?.addEventListener("click", async () => {
  try {
    const slug = state.currentSlug || "all_or_nothing";
    const res = await fetch("/api/queue/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug }) });
    const data = await res.json();
    if (data.queue) { state.sseTaskStatuses?.clear(); state.renderQueue = data.queue; renderQueueUI(); showToast("Queue cleared"); }
  } catch (err) { showToast("Failed to clear queue"); }
});

// Event delegation for table action buttons (Move Up, Move Down, Delete, Add)
document.getElementById("queueTableBody")?.addEventListener("click", async (e) => {
  const btn = e.target.closest(".btn-queue-action");
  if (!btn || btn.disabled) return;
  const id = btn.dataset.id;
  if (!id) return;
  const slug = state.currentSlug || "all_or_nothing";
  let endpoint = null, body = { slug, id };

  if (btn.classList.contains("btn-queue-up")) { endpoint = "/api/queue/move"; body.direction = "up"; }
  else if (btn.classList.contains("btn-queue-down")) { endpoint = "/api/queue/move"; body.direction = "down"; }
  else if (btn.classList.contains("btn-queue-del")) endpoint = "/api/queue/remove";
  else if (btn.classList.contains("btn-queue-add")) endpoint = "/api/queue/enqueue";

  if (endpoint) {
    try {
      const res = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json();
      if (data.queue) {
        state.sseTaskStatuses?.clear(); state.renderQueue = data.queue; renderQueueUI();
        if (endpoint.includes("remove")) showToast("Removed from queue");
        if (endpoint.includes("enqueue")) showToast("Added to queue");
      }
    } catch (err) { console.error(err); }
  }
});

async function stopActiveRender() {
  resetEngineUI();
  try {
    await fetch("/api/render/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug: state.currentSlug }) });
    showToast("⏹ Render stopped.");
    if (state.currentSlug) loadProjectQueue(state.currentSlug);
  } catch (err) {
    showToast(`Error stopping render: ${err}`);
  }
}

document.getElementById("btnRenderAll")?.addEventListener("click", () => {
  const isRunning = Boolean(
    state.isEngineRunning ||
    (state.activeRenderTask && state.activeRenderTask.status === "rendering") ||
    (state.renderQueue || []).some((t) => t.status === "rendering")
  );
  if (isRunning) stopActiveRender();
  else triggerRender([]);
});

const dummyContainer = document.getElementById("dummyModeContainer");
const chkDummyMode = document.getElementById("chkDummyRenderMode");

// Dummy mode is hidden by default to prevent accidental selection by regular users.
// Only expose in UI if explicitly requested via URL parameter (?dev=1 or ?dummy=1).
const devUrlParams = new URLSearchParams(window.location.search);
const showDevControls = devUrlParams.get("dev") === "1" || devUrlParams.get("dummy") === "1";
if (dummyContainer) dummyContainer.style.display = showDevControls ? "flex" : "none";

if (chkDummyMode) {
  fetch("/api/settings/dummy_mode")
    .then((r) => r.json())
    .then((data) => { if (data && data.dummy_render !== undefined) chkDummyMode.checked = !!data.dummy_render; })
    .catch(() => {});

  chkDummyMode.addEventListener("change", async () => {
    try {
      await fetch("/api/settings/dummy_mode", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: chkDummyMode.checked }),
      });
      showToast(chkDummyMode.checked ? "🧪 Dummy render engine enabled (Fast mock simulation)" : "⚡ Real Wan2GP engine enabled");
    } catch (e) { console.error("Failed to toggle dummy mode:", e); }
  });
}

window.setDummyMode = async function (enabled = true) {
  try {
    const res = await fetch("/api/settings/dummy_mode", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!enabled }),
    });
    const data = await res.json();
    if (chkDummyMode) chkDummyMode.checked = !!data.dummy_render;
    return data;
  } catch (e) { console.error("Failed to set dummy mode:", e); }
};

