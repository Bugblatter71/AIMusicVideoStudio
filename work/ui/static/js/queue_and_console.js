// ============================================================================
// Render Queue: Queue, Status & SSE Terminal Stream
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
  const rawPct = isMultiWin && task.window_pct != null ? task.window_pct : task.pct;
  const pctSuffix = rawPct !== null && rawPct !== undefined ? ` (${rawPct}%)` : "";
  if (task.total_steps && Number(task.total_steps) > 0) {
    return `[${winPrefix}${phaseLabel}] Step ${task.step ?? 0}/${task.total_steps}${pctSuffix}`;
  }
  return `[${winPrefix}${phaseLabel}]${pctSuffix}`;
}

function updateActiveProgressBarDOM(task) {
  const hasNullPct = task?.pct === null || task?.pct === undefined;
  const phaseStr = `${task?.phase || ""} ${task?.raw_phase || ""}`.toLowerCase();
  const indetLabel = phaseStr.includes("download") ? "Downloading" : (phaseStr.includes("upload") || phaseStr.includes("submit")) ? "Uploading" : (phaseStr.includes("queue") || phaseStr.includes("prepar")) ? "Queued" : "Live";
  const pb = document.getElementById("activeProgressBar");
  if (pb) {
    pb.classList.toggle("is-indeterminate", hasNullPct);
    pb.style.width = hasNullPct ? "100%" : `${task.pct}%`;
  }
  const pctText = document.getElementById("activeProgressPct");
  if (pctText) pctText.textContent = hasNullPct ? indetLabel : `${task.pct}%`;
  const phaseText = document.getElementById("activeProgressPhase");
  if (phaseText) phaseText.textContent = formatActivePhaseLabel(task);
}

function formatQueueReason(reason) {
  if (!reason) return "";
  const r = String(reason).trim();
  if (/^PASS 1 RE-RENDERED/i.test(r)) return "Pass 1 re-rendered";
  if (/TIMELINE/i.test(r)) return "Timeline changed";
  if (/UP-TO-DATE/i.test(r)) return "Up-to-date";
  if (/MISSING FILE/i.test(r)) return "Missing file";
  if (/PROMPT\/SEED\/SETTINGS/i.test(r)) return "Prompt/settings edited";
  if (/ANCHOR (\w+) EDITED/i.test(r)) return r.replace(/ANCHOR (\w+) EDITED/i, "Anchor $1 edited");
  if (/ANCHOR (\w+) NEWER/i.test(r)) return r.replace(/ANCHOR (\w+) NEWER ON DISK/i, "Anchor $1 newer");
  if (/SKIPPED/i.test(r)) return "Skipped by user";
  if (/STOPPED/i.test(r)) return "Stopped by user";
  return (/FORCED|^EDITED$/i.test(r)) ? r : r.replace(/\s*\(\[\d+\.?\d*,\s*\d+\.?\d*\]\s*not covered by\s*\[\d+\.?\d*,\s*\d+\.?\d*\]\)/gi, "").trim();
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
        if (state.renderTimerInterval) { clearInterval(state.renderTimerInterval); state.renderTimerInterval = null; }
        state.isEngineRunning = false;
        state.activeRenderTask = null;
        state.renderStartTime = null;
        state._authoritativeStartFilename = null;
        state.sseTaskStatuses.clear();
        window.resetMultiEngineUI?.();
      }

      // Reconcile queue rows against active per-engine statuses and SSE stream
      window.reconcileQueueStatuses?.(json.queue, st, engStatus, isRunning);

      state.renderQueue = json.queue;
      state.calibrationAlphaByRes = {};
      json.queue.forEach((t) => {
        const resKey = String(t.effective_resolution || t.resolution || "").toLowerCase();
        if (resKey && t.calibration_alpha && (!t.backend || t.backend === "local")) state.calibrationAlphaByRes[resKey] = Number(t.calibration_alpha);
      });
      if (json.queue.length > 0 && json.queue[0].calibration_alpha) {
        state.calibrationAlpha = Number(json.queue[0].calibration_alpha) || 1.0;
        if (state.selectedSceneIdx !== null && state.projectData?.scenes?.[state.selectedSceneIdx]) window.updateInspectorSceneHeader?.(state.projectData.scenes[state.selectedSceneIdx]);
      }

      if (isRunning) {
        let activeItem = state.renderQueue.find((t) => t.status === "rendering");
        if (!activeItem && engStatus && engStatus.filename) {
          activeItem = state.renderQueue.find((t) => t.filename === engStatus.filename || t.output_filename === engStatus.filename);
          if (activeItem) activeItem.status = "rendering";
        }
        if (activeItem) {
          const liveEng = (engStatus && (engStatus.state === "rendering" || engStatus.state === "cancelling")) ? engStatus : null;
          state.activeRenderTask = {
            idx: activeItem.idx, total: activeItem.total || state.renderQueue.length,
            filename: activeItem.filename, reason: activeItem.reason,
            model: activeItem.model_type || liveEng?.model,
            frames: activeItem.video_length || liveEng?.frames,
            resolution: activeItem.resolution || liveEng?.resolution,
            pct: activeItem.pct !== undefined ? activeItem.pct : (liveEng?.pct !== undefined ? liveEng.pct : (state.activeRenderTask?.pct ?? null)),
            window_pct: activeItem.window_pct ?? liveEng?.window_pct ?? state.activeRenderTask?.window_pct,
            phase: activeItem.phase || liveEng?.phase || state.activeRenderTask?.phase,
            raw_phase: activeItem.raw_phase || liveEng?.raw_phase || state.activeRenderTask?.raw_phase,
            step: activeItem.step ?? liveEng?.step ?? state.activeRenderTask?.step,
            total_steps: activeItem.total_steps ?? liveEng?.total_steps ?? state.activeRenderTask?.total_steps,
            window_index: activeItem.window_index ?? liveEng?.window_index ?? state.activeRenderTask?.window_index,
            window_count: activeItem.window_count ?? liveEng?.window_count ?? state.activeRenderTask?.window_count,
            started_at: liveEng?.started_at || state.activeRenderTask?.started_at || null,
          };
          if (!syncRenderStartTime(liveEng, activeItem.filename || "") && !state.renderStartTime) {
            state.renderStartTime = Date.now();
          }
          if (!state.renderTimerInterval) {
            state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
          }
        }
      }

      if (st) window.syncMultiEngineStatusUI?.(st);
      renderQueueUI();
      if (isRunning && state.renderStartTime) updateRenderTimer(true);
    }
  } catch (err) {
    console.warn("Failed to load project queue:", err);
    const tbody = document.getElementById("queueTableBody");
    if (tbody && (!state.renderQueue || state.renderQueue.length === 0)) {
      tbody.innerHTML = '<tr><td colspan="9" style="padding: 16px; text-align: center; color: #f87171;">⚠️ Unable to reach server for render queue. Retrying...</td></tr>';
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
    runBtn.textContent = isEngineRunning ? ((window.activeEngineCount?.() || 0) > 1 ? "⏹ Stop All" : "⏹ Stop") : "▶ Run";
    runBtn.classList.toggle("btn-danger", isEngineRunning);
    runBtn.classList.toggle("btn-primary", !isEngineRunning);
    runBtn.title = isEngineRunning ? "Stop all active engines and pause queue" : "Run pending items for checked engines";
  }
  window.updateEngineToggleCounts?.(queue);
  const renderingCount = queue.filter((t) => t.status === "rendering").length;
  const queuedOnlyCount = queue.filter((t) => t.status === "queued").length;
  const failedCount = queue.filter((t) => t.status === "failed").length;
  const queuedCount = renderingCount + queuedOnlyCount + failedCount;
  const doneCount = queue.filter((t) => t.status === "completed").length;
  const skippedCount = queue.filter((t) => t.status === "skipped").length;
  const pendingEstSec = queue
    .filter((t) => t.status === "queued" || t.status === "rendering" || t.status === "failed")
    .reduce((acc, t) => acc + (t.status === "rendering" ? (Number(t.estimated_remaining_seconds ?? t.remaining_seconds ?? t.estimated_seconds) || 0) : (Number(t.estimated_seconds) || 0)), 0);
  const pendingEtaStr = pendingEstSec > 0 && typeof formatDurationEstimate === "function"
    ? ` (Est. ~${formatDurationEstimate(pendingEstSec)}${renderingCount > 0 ? " remaining" : ""})` : "";

  const lblQueued = document.getElementById("lblQueuedTab");
  if (lblQueued) lblQueued.textContent = isEngineRunning ? "Queued" : "Pending";

  [["countAll", totalCount], ["countQueued", queuedCount], ["countDone", doneCount]].forEach(([id, val]) => {
    const el = document.getElementById(id); if (el) el.textContent = val;
  });

  // Active Task Hero Card
  const activeTask = state.activeRenderTask;
  const headerRow = document.getElementById("activeTaskHeaderRow");
  const titleEl = document.getElementById("activeTaskTitle");
  const headingEl = document.getElementById("activeTaskHeading");
  const detailsEl = document.getElementById("activeTaskDetails");
  const timerEl = document.getElementById("activeTaskTimer");
  const progContainer = document.getElementById("activeProgressBarContainer");
  const hasLanes = Boolean((window.activeEngineCount?.() || 0) > 0 || document.getElementById("multiEngineActiveLanes")?.children?.length);

  if (activeTask) {
    if (headingEl) headingEl.textContent = "";
    if (headerRow) headerRow.style.display = hasLanes ? "none" : "flex";
    if (titleEl) { titleEl.textContent = activeTask.filename; titleEl.style.display = hasLanes ? "none" : ""; }
    if (detailsEl) { detailsEl.style.display = "none"; detailsEl.innerHTML = ""; }
    if (timerEl) timerEl.style.display = hasLanes ? "none" : "inline";
    if (progContainer) progContainer.style.display = hasLanes ? "none" : "block";
    updateActiveProgressBarDOM(activeTask);
  } else {
    if (headerRow) headerRow.style.display = "none";
    if (headingEl) headingEl.textContent = "";
    if (titleEl) {
      titleEl.style.display = "";
      const waitLabel = isEngineRunning ? "waiting in queue" : "pending render";
      titleEl.textContent = queuedCount > 0 ? `${queuedCount} file(s) ${failedCount ? `(${failedCount} failed, hit ▶ Run to retry)` : waitLabel}${pendingEtaStr}` : "Idle • All files up-to-date";
    }
    if (detailsEl) {
      detailsEl.style.display = "flex";
      detailsEl.style.marginTop = "2px";
      const qWord = isEngineRunning ? "Queued" : "Pending";
      const qBreakdown = renderingCount > 0 ? `${renderingCount} Rendering + ${queuedOnlyCount} Queued` : `${queuedCount} ${qWord}`;
      const cloudItems = queue.filter((x) => (x.status === "queued" || x.status === "rendering") && (x.backend === "fal" || x.backend === "minimax"));
      const cloudCostSum = cloudItems.reduce((s, x) => s + (Number(x.estimated_cost_usd) || 0), 0);
      const cloudStr = cloudItems.length ? ` • ☁️ Cloud Est: ~$${cloudCostSum.toFixed(2)}` : "";
      detailsEl.innerHTML = `<span id="queueStatsSummary">${totalCount} Total Passes • ${qBreakdown}${pendingEtaStr}${cloudStr} • ${doneCount} Finished • ${skippedCount} Up-to-date</span>`;
    }
    if (timerEl) timerEl.style.display = "none";
    if (progContainer) progContainer.style.display = "none";
  }
  window.renderMultiEngineActiveLanes?.();

  // Group into SceneUnits (keeping Pass 1 & Pass 2 coupled in [Pass 1, Pass 2] order) and pin active render unit ahead of queued units
  const getPassNum = (t) => t.pass || (/Pass1/i.test(t.output_filename || t.filename || "") ? 1 : (/Pass2/i.test(t.output_filename || t.filename || "") ? 2 : null));
  const unitMap = new Map(), allUnits = [];
  queue.forEach((t, i) => {
    const pNum = getPassNum(t), sid = (t.scene_id || "").trim().toUpperCase(), k = (sid && pNum) ? `sid:${sid}` : `idx:${i}`;
    if (!unitMap.has(k)) { const u = []; unitMap.set(k, u); allUnits.push(u); }
    unitMap.get(k).push(t);
  });
  allUnits.forEach((u) => {
    if (u.length > 1) u.sort((a, b) => (getPassNum(a) || 0) - (getPassNum(b) || 0));
    if (u.length === 2) {
      if (u[1].status === "rendering" && u[0].status === "queued") u[0].status = "completed";
      else if ((u[0].status === "queued" || u[0].status === "rendering") && u[1].status !== "queued" && u[1].status !== "rendering") {
        u[1].status = "queued";
        u[1].needs_render = true;
        u[1].reason = `PASS 1 RE-RENDERED (${u[0].reason || "QUEUED"})`;
      }
    }
  });
  const renU = [], restU = [];
  allUnits.forEach((u) => (u.some((x) => x.status === "rendering") ? renU : restU).push(u));
  const orderedQueue = renU.concat(restU).flat();
  const movableUnits = allUnits.filter((u) => !u.some((x) => x.status === "rendering") && u.some((x) => x.status === "queued"));

  // Filter items
  const filter = state.queueFilter || "queued";
  const isDoneFilter = filter === "completed";
  const showActions = filter === "queued";
  const showReason = filter !== "completed";

  let filtered = orderedQueue.filter((t) => {
    if (filter === "all") return true;
    if (filter === "queued") return t.status === "queued" || t.status === "rendering" || t.status === "failed";
    if (filter === "completed") return t.status === "completed";
    return true;
  });

  if (isDoneFilter) {
    filtered = [...filtered].sort((a, b) => {
      const timeA = a.completed_at ? Number(a.completed_at) : 0, timeB = b.completed_at ? Number(b.completed_at) : 0;
      return timeB !== timeA ? timeB - timeA : a.idx - b.idx;
    });
  }

  // Calculate filename & reason column widths based on widest elements in list + padding
  const getModelStr = (t) => (t.backend === "fal" || t.backend === "minimax") ? (t.fal_endpoint || t.model_type || t.model || "cloud") : (t.model_type || t.model || "—");
  let maxFnWidth = 110, maxModelWidth = 90, maxReasonWidth = 55;
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.font = "500 11px monospace";
      filtered.forEach((t) => { maxFnWidth = Math.max(maxFnWidth, ctx.measureText(t.filename || t.output_filename || "").width); });
      ctx.font = "10px monospace";
      filtered.forEach((t) => { maxModelWidth = Math.max(maxModelWidth, ctx.measureText(getModelStr(t)).width); });
      ctx.font = "10px sans-serif";
      filtered.forEach((t) => { maxReasonWidth = Math.max(maxReasonWidth, ctx.measureText(formatQueueReason(t.reason)).width); });
      ctx.font = "11px sans-serif";
      maxFnWidth = Math.max(maxFnWidth, ctx.measureText("File to Render").width);
      maxModelWidth = Math.max(maxModelWidth, ctx.measureText("Model").width);
      maxReasonWidth = Math.max(maxReasonWidth, ctx.measureText("Reason").width);
    }
  } catch (e) {
    maxFnWidth = Math.max(...filtered.map((t) => (t.filename || "").length), 14) * 7.5;
    maxModelWidth = Math.max(...filtered.map((t) => getModelStr(t).length), 8) * 6.5;
    maxReasonWidth = Math.max(...filtered.map((t) => formatQueueReason(t.reason).length), 6) * 6.5;
  }
  const fnColWidth = Math.min(Math.max(Math.ceil(maxFnWidth + 22), 120), 320);
  const modelColWidth = Math.min(Math.max(Math.ceil(maxModelWidth + 18), 110), 210);
  const reasonColWidth = Math.max(Math.ceil(maxReasonWidth + 22), 65);

  const lenColWidth = 62, estColWidth = isDoneFilter ? 0 : 126;
  const thead = document.getElementById("queueTableHead");
  if (thead) {
    thead.innerHTML = `<tr>
      <th style="padding: 6px 0; width: 28px; text-align: center; color: var(--text-muted);" title="Drag row to reorder queue">⠿</th>
      <th style="padding: 6px 10px; width: ${fnColWidth}px; color: var(--text-muted);">File to Render</th>
      <th style="padding: 6px 8px; width: 118px; color: var(--text-muted);">Engine</th>
      <th style="padding: 6px 8px; width: ${modelColWidth}px; color: var(--text-muted);">Model</th>
      <th style="padding: 6px 10px; width: ${lenColWidth}px; color: var(--text-muted);">Length</th>
      ${!isDoneFilter ? `<th style="padding: 6px 8px; width: ${estColWidth}px; color: var(--text-muted);">Est / Cost</th>` : ''}
      <th style="padding: 6px 10px; width: 95px; color: var(--text-muted);">Status</th>
      ${isDoneFilter ? '<th style="padding: 6px 10px; width: 140px; color: var(--text-muted);">Completed</th><th style="padding: 6px 10px; width: 85px; color: var(--text-muted);">Time Taken</th>' : ''}
      ${showReason ? `<th style="padding: 6px 10px; width: ${reasonColWidth}px; color: var(--text-muted);">Reason</th>` : ''}
      ${showActions ? '<th style="padding: 6px 10px; width: 90px; text-align: center; color: var(--text-muted);">Actions</th>' : ''}
      <th style="width: auto;"></th>
    </tr>`;
  }

  if (filtered.length === 0) {
    const filterLabel = (filter === "queued" && !isEngineRunning) ? "pending" : filter;
    const colCount = (!isDoneFilter ? 1 : 0) + ((isDoneFilter || showActions) ? 9 : 8);
    tbody.innerHTML = `<tr><td colspan="${colCount}" style="padding: 24px; text-align: center; color: var(--text-muted);">No files match filter "${filterLabel}".</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered
    .map((t) => {
      const isRendering = t.status === "rendering", isCompleted = t.status === "completed", isQueued = t.status === "queued", isFailed = t.status === "failed";
      let statusBadge = `<span style="background: rgba(100, 116, 139, 0.15); color: #94a3b8; padding: 2px 8px; border-radius: 4px;">⏩ Skipped</span>`;
      if (isRendering) statusBadge = `<span class="active-row-progress-badge" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); padding: 2px 8px; border-radius: 4px; font-weight: 600; white-space: nowrap;">⚡ Rendering</span>`;
      else if (isQueued) statusBadge = `<span style="background: rgba(245, 158, 11, ${isEngineRunning ? '0.15' : '0.12'}); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); padding: 2px 8px; border-radius: 4px; font-weight: 600;">⏳ ${isEngineRunning ? 'Queued' : 'Pending'}</span>`;
      else if (isCompleted) statusBadge = `<span style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); padding: 2px 8px; border-radius: 4px; font-weight: 600;">✅ Done</span>`;
      else if (isFailed) statusBadge = `<span style="background: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); padding: 2px 8px; border-radius: 4px; font-weight: 600;" title="${escapeHtml(t.error || t.reason || "Render failed")}">❌ Failed</span>`;

      const rowStyle = isRendering ? "background: rgba(56, 189, 248, 0.1); border-left: 3px solid #38bdf8;" : "border-bottom: 1px solid rgba(255, 255, 255, 0.04);";
      const scMatch = state.projectData?.scenes?.find((s) => s.id === t.scene_id);
      const durSec = t.duration != null ? Number(t.duration) : (scMatch ? Number(scMatch.end - scMatch.start) : 0);
      const lenStr = durSec > 0 ? `${durSec.toFixed(1)}s` : "—";
      const estStr = !isDoneFilter && !isCompleted ? (isRendering ? (t.estimated_remaining_str || t.estimated_str || "") : (t.estimated_str || "")) : "";
      const cloudTitle = escapeHtml((t.fal_endpoint || "fal.ai") + (Array.isArray(t.fal_notes) && t.fal_notes.length ? " — " + t.fal_notes.join("; ") : ""));
      const estBadge = (!isDoneFilter && !isCompleted)
        ? ((t.backend === "fal" || t.backend === "minimax")
          ? `${estStr ? `<span style="color:#a78bfa;font-size:10px;">~${escapeHtml(estStr)}${isRendering ? " rem" : ""}</span> ` : ""}<span style="color:${t.backend === "minimax" ? "#c084fc" : "#38bdf8"};font-size:10px;background:${t.backend === "minimax" ? "rgba(168,85,247,0.16)" : "rgba(56,189,248,0.14)"};padding:1px 5px;border-radius:3px;" title="${cloudTitle}">${t.backend === "minimax" ? "🎥" : "☁️"} ${escapeHtml(t.cost_str || t.backend)}${t.auto_chunks > 1 ? ` · ${escapeHtml(t.auto_chunks)}x` : ""}</span>`
          : (estStr ? `<span style="color: ${t.is_split_window ? "#f59e0b" : "#38bdf8"}; font-size: 10px;" title="Est. Render Time: ~${escapeHtml(estStr)} (${escapeHtml(t.frames || t.quantized_frames || "")}f${t.resolution ? `, ${escapeHtml(t.resolution)}` : ""}, ${escapeHtml(t.regime || "fast_vram")})">~${escapeHtml(estStr)}${isRendering ? " rem" : ""}${t.is_split_window ? "⚠️" : ""}</span>` : '<span style="color:#64748b;font-size:10px;">—</span>'))
        : '<span style="color:#64748b;font-size:10px;">—</span>';
      const completedStr = isCompleted ? escapeHtml(formatCompletedAt(t.completed_at)) : (isRendering ? '<span style="color: #38bdf8;">Rendering...</span>' : "—");
      const elapsedStr = isCompleted ? escapeHtml(formatElapsed(t.elapsed)) : (isRendering && t.elapsed ? escapeHtml(formatElapsed(t.elapsed)) : "—");
      const shortReason = escapeHtml(formatQueueReason(t.reason)), targetId = escapeHtml(t.filename || t.id || t.output_filename || "");
      const escFn = escapeHtml(t.filename || t.output_filename || "");

      const curUnit = allUnits.find((u) => u.includes(t)), movIdx = isQueued ? movableUnits.indexOf(curUnit) : -1;
      const canUp = movIdx > 0, canDown = movIdx > -1 && movIdx < movableUnits.length - 1;
      const isCoupled = curUnit && curUnit.filter((x) => x.status === "queued").length > 1;
      const isLockedToP1 = isQueued && movIdx === -1 && curUnit && curUnit.some((x) => x.status === "rendering");
      const canDrag = isQueued && !isRendering && !isLockedToP1;
      const gripTitle = isCoupled ? 'Drag Scene (Pass 1 & 2 coupled)' : 'Drag to reorder';
      const gripIcon = isRendering ? '<span style="color: #38bdf8; font-size: 11px;" title="Actively rendering (pinned)">⚡</span>' : (isLockedToP1 ? '<span style="color: #f59e0b; font-size: 11px;" title="Locked to actively rendering Pass 1">🔒</span>' : (canDrag ? `<span class="queue-drag-grip" title="${gripTitle}" style="color: var(--text-muted); font-size: 13px;">⠿</span>` : '<span style="color: var(--text-muted); font-size: 10px; opacity: 0.25;">—</span>'));
      const gripCol = `<td class="queue-drag-grip-cell" style="padding: 6px 0; width: 28px; text-align: center; cursor: ${canDrag ? 'grab' : 'default'}; user-select: none;">${gripIcon}</td>`;

      const upTitle = isLockedToP1 ? "Locked to actively rendering Pass 1" : (isCoupled ? "Move Scene (Pass 1 & 2) Up" : "Move Up");
      const downTitle = isLockedToP1 ? "Locked to actively rendering Pass 1" : (isCoupled ? "Move Scene (Pass 1 & 2) Down" : "Move Down");

      let actionsHtml = `<div style="display: flex; justify-content: center; align-items: center;"><button type="button" class="btn-queue-action btn-queue-add" data-id="${targetId}" title="Add to Queue" style="padding: 1px 6px; font-size: 10px; cursor: pointer; background: rgba(255,255,255,0.06); border: 1px solid var(--border); border-radius: 3px; color: var(--text-muted);">➕ Queue</button></div>`;
      if (isRendering) actionsHtml = `<span style="color: #38bdf8; font-size: 10px; font-weight: 600;">Active</span>`;
      else if (isQueued || isFailed) {
        const delBtn = `<button type="button" class="btn-queue-action btn-queue-del" data-id="${targetId}" title="Remove from Queue" style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(239,68,68,0.3); border-radius: 3px; cursor: pointer; color: #ef4444;">🗑️</button>`;
        const leadBtns = isQueued
          ? `<button type="button" class="btn-queue-action btn-queue-up" data-id="${targetId}" title="${upTitle}" ${canUp ? "" : "disabled"} style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 3px; opacity: ${canUp ? "1" : "0.3"}; cursor: ${canUp ? "pointer" : "default"};">⬆️</button><button type="button" class="btn-queue-action btn-queue-down" data-id="${targetId}" title="${downTitle}" ${canDown ? "" : "disabled"} style="padding: 1px 4px; font-size: 11px; background: transparent; border: 1px solid rgba(255,255,255,0.1); border-radius: 3px; opacity: ${canDown ? "1" : "0.3"}; cursor: ${canDown ? "pointer" : "default"};">⬇️</button>`
          : `<button type="button" class="btn-queue-action btn-queue-add" data-id="${targetId}" title="Re-queue failed render" style="padding: 1px 6px; font-size: 10px; cursor: pointer; background: rgba(239,68,68,0.16); border: 1px solid rgba(239,68,68,0.4); border-radius: 3px; color: #fca5a5;">🔄 Retry</button>`;
        actionsHtml = `<div style="display: flex; gap: 4px; justify-content: center; align-items: center;">${leadBtns}${delBtn}</div>`;
      }

      const timeCols = isDoneFilter ? `<td style="padding: 6px 10px; width: 140px; color: var(--text-muted); font-family: monospace; white-space: nowrap;">${completedStr}</td><td style="padding: 6px 10px; width: 85px; color: var(--text-muted); font-family: monospace; white-space: nowrap;">${elapsedStr}</td>` : "";
      const reasonCol = showReason ? `<td style="padding: 6px 10px; width: ${reasonColWidth}px; color: var(--text-muted); font-size: 10px; white-space: nowrap;"><span title="${shortReason}">${shortReason}</span></td>` : "";
      const actionsCol = showActions ? `<td style="padding: 6px 10px; width: 90px; text-align: center; white-space: nowrap;">${actionsHtml}</td>` : "";

      const escModel = escapeHtml(getModelStr(t));
      return `<tr data-id="${targetId}" data-unit-sid="${escapeHtml((t.scene_id || '').toUpperCase())}" data-is-queued="${isQueued ? '1' : '0'}" data-is-rendering="${isRendering ? '1' : '0'}" draggable="${canDrag ? 'true' : 'false'}" style="${rowStyle}; user-select: none;">
        ${gripCol}
        <td style="padding: 6px 10px; width: ${fnColWidth}px; font-weight: 500;"><div style="color: ${isRendering ? "#38bdf8" : isCompleted ? "#10b981" : "var(--text-main)"}; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escFn}">${escFn}</div></td>
        ${window.renderQueueEngineCell ? window.renderQueueEngineCell(t) : `<td style="padding: 6px 8px; width: 118px;">${escapeHtml(t.backend || "local")}</td>`}
        <td style="padding: 6px 8px; width: ${modelColWidth}px;"><div style="color: #7dd3fc; font-size: 10px; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escModel}">${escModel}</div></td>
        <td style="padding: 6px 10px; width: ${lenColWidth}px; color: var(--text-muted); font-family: monospace; white-space: nowrap;">${lenStr}</td>
        ${!isDoneFilter ? `<td class="queue-est-col" style="padding: 6px 8px; width: ${estColWidth}px; font-family: monospace;">${estBadge}</td>` : ''}
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
      const evBackend = payload.backend || "local";
      state.activeRenderTask = payload;
      if (!syncRenderStartTime(payload, fnKey)) {
        if (!state.renderStartTime || (state._authoritativeStartFilename && fnKey && state._authoritativeStartFilename !== fnKey)) {
          state.renderStartTime = Date.now();
          state._authoritativeStartFilename = null;
        }
      }
      if (!state.renderTimerInterval) state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
      if (fnKey) state.sseTaskStatuses.set(fnKey, { status: "rendering", payload });
      if (stemKey) state.sseTaskStatuses.set(stemKey, { status: "rendering", payload });
      const match = findQueueItem();
      if (match) {
        match.status = "rendering";
        match.reason = payload.reason || match.reason;
        state.activeRenderTask.idx = match.idx;
        state.activeRenderTask.total = state.renderQueue.length;
      }
      window.handleMultiEngineSSEEvent?.(tag, payload);
      renderQueueUI();
    } else if (tag === "[QUEUE_PROGRESS]") {
      if (state.activeRenderTask && (!fnKey || state.activeRenderTask.filename === fnKey)) {
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
        Object.assign(match, {
          pct: payload.pct, window_pct: payload.window_pct !== undefined ? payload.window_pct : payload.pct,
          phase: payload.phase, raw_phase: payload.raw_phase, step: payload.step,
          total_steps: payload.total_steps, window_index: payload.window_index, window_count: payload.window_count,
        });
      }

      // Direct DOM update for 60fps performance
      if (!state.activeRenderTask || !fnKey || state.activeRenderTask.filename === fnKey) updateActiveProgressBarDOM(payload);
      window.handleMultiEngineSSEEvent?.(tag, payload);
      const rowBadge = document.querySelector(".active-row-progress-badge");
      if (rowBadge) rowBadge.textContent = "⚡ Rendering";
    } else if (tag === "[QUEUE_CANCEL]") {
      if (fnKey) state.sseTaskStatuses.delete(fnKey);
      if (stemKey) state.sseTaskStatuses.delete(stemKey);
      const match = findQueueItem();
      if (match) { match.status = match.needs_render === false ? "skipped" : "queued"; match.reason = payload.reason || "STOPPED BY USER"; }
      window.handleMultiEngineSSEEvent?.(tag, payload);
      if (window.promoteActiveRenderTaskFromState) window.promoteActiveRenderTaskFromState(fnKey);
      else if (!window.hasAnyOtherEngineRunning?.(fnKey)) { state.isEngineRunning = false; state.activeRenderTask = null; }
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
      window.handleMultiEngineSSEEvent?.(tag, payload);
      if (state.activeRenderTask && (!fnKey || state.activeRenderTask.filename === fnKey)) {
        if (window.promoteActiveRenderTaskFromState) window.promoteActiveRenderTaskFromState(fnKey);
        else state.activeRenderTask = null;
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
      window.handleMultiEngineSSEEvent?.(tag, payload);
      if (state.activeRenderTask && (!fnKey || state.activeRenderTask.filename === fnKey)) {
        if (window.promoteActiveRenderTaskFromState) window.promoteActiveRenderTaskFromState(fnKey);
        else state.activeRenderTask = null;
      }
      renderQueueUI();
    }
  } catch (err) {
    console.error("Queue SSE parse error:", err);
  }
}

function syncRenderStartTime(eng, activeFn = "") {
  if (eng?.state && eng.state !== "rendering" && eng.state !== "cancelling") return false;
  if (eng?.started_at && Number(eng.started_at) > 0 && (!eng.filename || !activeFn || eng.filename === activeFn)) {
    state.renderStartTime = Number(eng.started_at) * 1000;
    state._authoritativeStartFilename = eng.filename || activeFn || "";
    return true;
  }
  return false;
}

function renderClockAndEta() {
  window.tickMultiEngineClocks?.();
  if (state.activeRenderTask?.filename && state._activeEngineJobs instanceof Map) {
    for (const s of state._activeEngineJobs.values()) {
      if (s?.running && s.filename === state.activeRenderTask.filename && s.started_at) { state.renderStartTime = Number(s.started_at) * 1000; break; }
    }
  }
  if (!state.renderStartTime) return;
  const elapsedSec = Math.max(0, Math.floor((Date.now() - state.renderStartTime) / 1000));
  const timerEl = document.getElementById("activeTaskTimer");
  if (timerEl) timerEl.textContent = `⏱ ${String(Math.floor(elapsedSec / 60)).padStart(2, "0")}:${String(elapsedSec % 60).padStart(2, "0")}`;
  const etaBadgeEl = document.getElementById("activeTaskEtaBadge");
  if (etaBadgeEl && state.activeRenderTask && typeof formatDurationEstimate === "function") {
    const actItem = (state.renderQueue || []).find((x) => x.filename === state.activeRenderTask.filename) || (state.renderQueue || []).find((x) => x.status === "rendering");
    const baseEst = Number(actItem?.estimated_seconds || 0);
    if (baseEst > 0) {
      const pct = Number(state.activeRenderTask.pct || 0), step = Number(state.activeRenderTask.step || 0);
      if (state.activeRenderTask.phase === "completed" || pct >= 99.5) {
        etaBadgeEl.textContent = "⏱ Finishing...";
      } else {
        let remSec = Math.max(5, baseEst - elapsedSec);
        if (step >= 1 && pct >= 5.0 && pct < 99.0 && elapsedSec > 0) {
          const pFrac = Math.max(0.05, Math.min(0.99, pct / 100.0)), w = Math.pow(pFrac, 1.2);
          remSec = Math.max(5, (1.0 - w) * baseEst + w * (elapsedSec / pFrac) - elapsedSec);
        }
        etaBadgeEl.textContent = `⏱ Rem: ~${formatDurationEstimate(remSec)}`;
      }
    }
  }
}

async function updateRenderTimer(skipPoll = false) {
  if (!state.renderStartTime) return;
  renderClockAndEta();
  if (!skipPoll && !state._pollingEngineStatus) {
    state._pollingEngineStatus = true;
    try {
      const res = await fetch("/api/render/status");
      const json = await res.json();
      window.syncMultiEngineStatusUI?.(json);
      if (json && json.engine_status && (json.engine_status.state === "rendering" || json.engine_status.state === "cancelling")) {
        const eng = json.engine_status;
        if (syncRenderStartTime(eng, state.activeRenderTask?.filename || "")) renderClockAndEta();
        const cur = state.activeRenderTask || {};
        const stepFwd = (eng.step ?? -1) > (cur.step ?? -1) || (eng.pct != null && (cur.pct == null || eng.pct > cur.pct));
        const metaChg = (eng.phase && eng.phase !== cur.phase) || (eng.raw_phase && eng.raw_phase !== cur.raw_phase) || eng.window_pct !== cur.window_pct;
        const missingStart = !state.activeRenderTask;
        if (missingStart) state.activeRenderTask = { ...eng };
        if (missingStart || stepFwd || (eng.pct == null && metaChg)) {
          Object.assign(state.activeRenderTask, {
            step: eng.step ?? null, total_steps: eng.total_steps || state.activeRenderTask.total_steps,
            pct: eng.pct !== undefined ? eng.pct : null, window_pct: eng.window_pct !== undefined ? eng.window_pct : (eng.pct ?? null),
            phase: eng.phase || state.activeRenderTask.phase, raw_phase: eng.raw_phase || state.activeRenderTask.raw_phase,
            window_index: eng.window_index || state.activeRenderTask.window_index, window_count: eng.window_count || state.activeRenderTask.window_count,
          });
          updateActiveProgressBarDOM(state.activeRenderTask);
        }
      }
    } catch (e) {
    } finally { state._pollingEngineStatus = false; }
  }
}

function initLogStream() {
  if (state.eventSource) { try { state.eventSource.close(); } catch (e) {} }
  const es = new EventSource("/api/render/events"), term = document.getElementById("terminalOutput");
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.text) {
        if (data.text.includes("[MASTER_STITCH_START]") || data.text.includes("[MASTER_STITCH_PROGRESS]")) { window.handleMasterStitchSSE?.(data.text); return; }
        if (data.text.includes("[MASTER_UPDATED]")) { showToast("🎬 Master video restitched!"); if (!window.handleMasterStitchSSE?.(data.text)) refreshProjectAssets(true); return; }
        if (data.text.includes("[MASTER_LOCKED]")) { window.handleMasterStitchSSE?.(data.text); showToast("⚠️ Master video is locked by a video player. Close your player and restitch.", 9000); return; }
        if (data.text.includes("[MASTER_ERROR]")) { window.handleMasterStitchSSE?.(data.text); showToast("❌ Master video restitch failed. See terminal logs.", 6000); return; }
        if (data.text.includes("[ANCHORS_UPDATED]")) { showToast("🎨 Anchor image updated!"); refreshProjectAssets(true); return; }
        if (data.text.includes("[UPSCALE_DONE]") || data.text.includes("[REPAIR_LIPSYNC_DONE]")) {
          window.handleCompletedAssetSSE?.(data.text.includes("[UPSCALE_DONE]") ? "[UPSCALE_DONE]" : "[REPAIR_LIPSYNC_DONE]", data.text);
          return;
        }
        const queueIdx = data.text.indexOf("[QUEUE_");
        if (queueIdx !== -1) {
          handleQueueSSEEvent(data.text.slice(queueIdx));
          const prefix = data.text.slice(0, queueIdx).trim();
          if (prefix) { const line = document.createElement("div"); line.textContent = prefix; term.appendChild(line); term.scrollTop = term.scrollHeight; }
          return;
        }
        const line = document.createElement("div"); line.textContent = data.text; term.appendChild(line); term.scrollTop = term.scrollHeight;
      }
      if (data.done) { resetEngineUI(); refreshProjectAssets(true); }
    } catch (err) { console.error("SSE parse error:", err); }
  };
  es.onerror = () => { console.warn("SSE connection status change or reconnecting..."); };
  state.eventSource = es;
}

function resetEngineUI() {
  if (state.renderTimerInterval) { clearInterval(state.renderTimerInterval); state.renderTimerInterval = null; }
  state.isEngineRunning = false;
  state.activeRenderTask = null;
  state.renderStartTime = null;
  state._authoritativeStartFilename = null;
  state.sseTaskStatuses?.clear();
  window.resetMultiEngineUI?.();
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
  const wasRunning = Boolean(state.isEngineRunning || (state.renderQueue || []).some((t) => t.status === "rendering"));
  const term = document.getElementById("terminalOutput");
  if (term) term.innerHTML = "";
  if (!wasRunning) {
    state.sseTaskStatuses?.clear();
    window.resetMultiEngineUI?.();
  }
  state.isEngineRunning = true;

  // Clear previous task state so stale tasks (e.g. S22) never linger
  state.activeRenderTask = null;
  state._authoritativeStartFilename = null;
  state.renderStartTime = Date.now();
  if (!state.renderTimerInterval && typeof setInterval === "function") {
    state.renderTimerInterval = setInterval(updateRenderTimer, 1000);
  }
  if (args && args.length > 0) {
    const targetSid = String(args[0]).trim().toUpperCase();
    const targetBase = targetSid.split(":")[0];
    const match = state.renderQueue?.find(
      (t) => (t.scene_id || "").toUpperCase() === targetBase || (t.output_filename || "").toUpperCase().startsWith(`${targetBase}_`) || (t.output_filename || "").toUpperCase() === targetBase
    );
    if (match) {
      state.activeRenderTask = {
        idx: match.idx, total: state.renderQueue.length, filename: match.filename,
        reason: `TARGETED (${targetSid})`, model: match.model_type, frames: match.video_length, resolution: match.resolution,
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
      body: JSON.stringify({ slug: state.currentSlug, args: args, enabled_engines: window.getEnabledEnginesMap?.() || { local: true, fal: false, minimax: false }, max_parallel: window.getMaxParallelMap?.() || { local: 1, fal: 1, minimax: 1 } }),
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
  if (!btn || btn.disabled || !btn.dataset.id) return;
  const id = btn.dataset.id, slug = state.currentSlug || "all_or_nothing";
  let endpoint = null, body = { slug, id };
  if (btn.classList.contains("btn-queue-up")) { endpoint = "/api/queue/move"; body.direction = "up"; }
  else if (btn.classList.contains("btn-queue-down")) { endpoint = "/api/queue/move"; body.direction = "down"; }
  else if (btn.classList.contains("btn-queue-del")) endpoint = "/api/queue/remove";
  else if (btn.classList.contains("btn-queue-add")) {
    if (state.isDirty) await saveProjectData(false);
    endpoint = "/api/queue/enqueue";
  }

  if (endpoint) {
    try {
      const res = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json();
      if (data.queue) {
        state.renderQueue = data.queue; renderQueueUI();
        if (endpoint.includes("remove")) showToast("Removed from queue");
        if (endpoint.includes("enqueue")) showToast("Added to queue (will render automatically)");
      }
    } catch (err) { console.error(err); }
  }
});

// Drag and drop reordering for queued table rows
let draggedQueueRow = null, draggedUnitSid = null;
const qTbody = document.getElementById("queueTableBody");
if (qTbody && !qTbody.dataset.dndBound) {
  qTbody.dataset.dndBound = "1";
  qTbody.addEventListener("dragstart", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || tr.getAttribute("draggable") !== "true") { e.preventDefault(); return; }
    draggedQueueRow = tr;
    draggedUnitSid = tr.dataset.unitSid;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", tr.dataset.id);
    document.querySelectorAll(`#queueTableBody tr[data-unit-sid="${draggedUnitSid}"]`).forEach((r) => { r.style.opacity = "0.45"; r.style.outline = "1px dashed #38bdf8"; });
  });
  const _clearDnd = () => {
    document.querySelectorAll("#queueTableBody tr").forEach((r) => { r.style.opacity = ""; r.style.outline = ""; r.style.borderTop = ""; r.style.borderBottom = ""; });
    draggedQueueRow = null; draggedUnitSid = null;
  };
  qTbody.addEventListener("dragend", _clearDnd);
  qTbody.addEventListener("dragover", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || !draggedQueueRow || tr === draggedQueueRow || (tr.dataset.unitSid && tr.dataset.unitSid === draggedUnitSid) || (tr.dataset.isQueued !== "1" && tr.dataset.isRendering !== "1")) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    const rect = tr.getBoundingClientRect(), isTop = e.clientY < (rect.top + rect.height / 2);
    document.querySelectorAll("#queueTableBody tr").forEach((r) => { if (r !== tr) { r.style.borderTop = ""; r.style.borderBottom = ""; } });
    tr.style.borderTop = (tr.dataset.isRendering !== "1" && isTop) ? "2px solid #38bdf8" : "";
    tr.style.borderBottom = (tr.dataset.isRendering === "1" || !isTop) ? "2px solid #38bdf8" : "";
  });
  qTbody.addEventListener("drop", async (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || !draggedQueueRow || tr === draggedQueueRow || (tr.dataset.unitSid && tr.dataset.unitSid === draggedUnitSid) || (tr.dataset.isQueued !== "1" && tr.dataset.isRendering !== "1")) return;
    e.preventDefault();
    const rect = tr.getBoundingClientRect();
    let isBefore = e.clientY < (rect.top + rect.height / 2);
    if (tr.dataset.isRendering === "1") isBefore = false;
    const srcId = draggedQueueRow.dataset.id, targetId = tr.dataset.id, sid = draggedUnitSid;
    _clearDnd();
    try {
      const res = await fetch("/api/queue/reorder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug: state.currentSlug || "all_or_nothing", id: srcId, target_id: targetId, position: isBefore ? "before" : "after" }) });
      const data = await res.json();
      if (data.queue) { state.renderQueue = data.queue; renderQueueUI(); showToast(`↕️ Reordered ${sid || "scene"} in queue`); }
      else if (data.error) showToast(`Reorder failed: ${data.error}`);
    } catch (err) { console.error("Reorder error:", err); }
  });
}
async function stopActiveRender(backend = null, filename = null) {
  if (!backend && !filename) resetEngineUI();
  else {
    const stem = String(filename || "").replace(/\.(mp4|jpg|png|webp)$/i, "");
    if (filename) { state.sseTaskStatuses?.delete(filename); if (stem) state.sseTaskStatuses?.delete(stem); }
    window.handleMultiEngineSSEEvent?.("[QUEUE_CANCEL]", { filename: filename || "", backend: backend || "" });
    if (filename) window.promoteActiveRenderTaskFromState?.(filename);
  }
  try {
    await fetch("/api/render/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug: state.currentSlug, ...(backend ? { backend } : {}), ...(filename ? { filename } : {}) }) });
    showToast(filename ? `⏹ Stopping ${filename}...` : (backend ? `⏹ Stopping ${backend} engine...` : "⏹ Render stopped."));
    if (state.currentSlug) loadProjectQueue(state.currentSlug);
  } catch (err) { showToast(`Error stopping render: ${err}`); }
}
window.stopActiveRender = stopActiveRender;
document.getElementById("btnRenderAll")?.addEventListener("click", () => {
  const isRunning = Boolean(state.isEngineRunning || state.activeRenderTask?.status === "rendering" || (state.renderQueue || []).some((t) => t.status === "rendering"));
  if (isRunning) stopActiveRender(); else triggerRender([]);
});
const dummyContainer = document.getElementById("dummyModeContainer"), chkDummyMode = document.getElementById("chkDummyRenderMode");
const devUrlParams = new URLSearchParams(window.location.search);
if (dummyContainer) dummyContainer.style.display = (devUrlParams.get("dev") === "1" || devUrlParams.get("dummy") === "1") ? "flex" : "none";
async function _postDummyMode(enabled) {
  return (await fetch("/api/settings/dummy_mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !!enabled }) })).json();
}
if (chkDummyMode) {
  fetch("/api/settings/dummy_mode").then((r) => r.json()).then((d) => { if (d?.dummy_render !== undefined) chkDummyMode.checked = !!d.dummy_render; }).catch(() => {});
  chkDummyMode.addEventListener("change", async () => {
    try { await _postDummyMode(chkDummyMode.checked); showToast(chkDummyMode.checked ? "🧪 Dummy render engine enabled (Fast mock simulation)" : "⚡ Real Wan2GP engine enabled"); }
    catch (e) { console.error("Failed to toggle dummy mode:", e); }
  });
}
window.setDummyMode = async (enabled = true) => {
  try { const data = await _postDummyMode(enabled); if (chkDummyMode) chkDummyMode.checked = !!data.dummy_render; return data; }
  catch (e) { console.error("Failed to set dummy mode:", e); }
};
