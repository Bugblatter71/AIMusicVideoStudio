// ============================================================================
// Multi-Engine Queue Controls, Concurrency Spinners & Parallel Active Lanes
// ============================================================================
window.ENGINES = ["local", "fal", "minimax"];
window.ENGINE_META = {
  local: { label: "🖥️ Local GPU", short: "Local", bg: "rgba(100,116,139,0.18)", color: "#cbd5e1", max: 1 },
  fal: { label: "☁️ fal.ai", short: "fal.ai", bg: "rgba(56,189,248,0.16)", color: "#7dd3fc", max: 40 },
  minimax: { label: "🎥 MiniMax", short: "MiniMax", bg: "rgba(168,85,247,0.18)", color: "#d8b4fe", max: 30 },
};

function normQueueEngine(raw) {
  const b = String(raw || "").trim().toLowerCase();
  if (b === "fal" || b === "fal.ai" || b === "cloud") return "fal";
  if (b === "minimax" || b === "minimax_api" || b === "hailuo") return "minimax";
  return "local";
}

function clampEngineParallel(b, rawVal) {
  const hi = window.ENGINE_META[normQueueEngine(b)]?.max || 1;
  const n = parseInt(rawVal, 10);
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(hi, n);
}

function getEnabledEnginesMap() {
  if (!state.enabledEngines) state.enabledEngines = { local: true, fal: false, minimax: false };
  return { local: state.enabledEngines.local !== false, fal: Boolean(state.enabledEngines.fal), minimax: Boolean(state.enabledEngines.minimax) };
}

function getMaxParallelMap() {
  if (!state.maxParallel) state.maxParallel = { local: 1, fal: 1, minimax: 1 };
  return { local: 1, fal: clampEngineParallel("fal", state.maxParallel.fal), minimax: clampEngineParallel("minimax", state.maxParallel.minimax) };
}
window.getEnabledEnginesMap = getEnabledEnginesMap;
window.getMaxParallelMap = getMaxParallelMap;
window.normQueueEngine = normQueueEngine;

function _ensureActiveJobs() {
  if (!(state.activeJobs instanceof Map)) state.activeJobs = new Map();
  return state.activeJobs;
}

function _jobKey(backend, fn) {
  const stem = String(fn || "").replace(/\.(mp4|jpg|png|webp)$/i, "");
  return `${normQueueEngine(backend)}:${stem}`;
}

function hasAnyOtherEngineRunning(excludeFn = "") {
  const exStem = String(excludeFn || "").replace(/\.(mp4|jpg|png|webp)$/i, "");
  const jobs = Array.from(_ensureActiveJobs().values());
  if (jobs.some((j) => j.running && (!exStem || j.stem !== exStem))) return true;
  return window.ENGINES.some((b) => state.engineLive?.[b]?.running && (!excludeFn || state.engineLive[b].filename !== excludeFn));
}
window.hasAnyOtherEngineRunning = hasAnyOtherEngineRunning;
window.activeEngineCount = () => Math.max(_ensureActiveJobs().size, window.ENGINES.filter((b) => state.engineLive?.[b]?.running).length);

function _clearTransientRenderMeta(t) {
  ["estimated_remaining_seconds", "estimated_remaining_str", "pct", "step", "window_pct"].forEach((k) => delete t[k]);
}

function reconcileQueueStatuses(queue, st, engStatus, isRunning) {
  const activeProj = st?.current_project || state.renderStatus?.current_project, curProj = state.currentSlug;
  if (activeProj && curProj && activeProj !== curProj) {
    queue.forEach((t) => { if (t.status === "rendering") { t.status = t.needs_render === false ? "skipped" : "queued"; _clearTransientRenderMeta(t); } });
    return;
  }
  const actStatuses = [];
  if (st?.engines) {
    Object.values(st.engines).forEach((ei) => {
      const list = (Array.isArray(ei?.active_tasks) && ei.active_tasks.length) ? ei.active_tasks : (ei?.running && (ei.filename || ei.engine_status?.filename) ? [ei] : []);
      list.forEach((tk) => {
        const isLive = tk.engine_status?.state === "rendering" || tk.engine_status?.state === "cancelling";
        const es = isLive ? tk.engine_status : { filename: tk.filename || tk.engine_status?.filename };
        if (es.filename) actStatuses.push(es);
      });
    });
  }
  if (actStatuses.length === 0 && engStatus && engStatus.state === "rendering" && engStatus.filename) actStatuses.push(engStatus);
  const actSet = new Set();
  actStatuses.forEach((es) => {
    const fn = es.filename, stem = fn.replace(/\.(mp4|jpg|jpeg|png|webp)$/i, "");
    if (fn) actSet.add(fn);
    if (stem) actSet.add(stem);
    queue.forEach((t) => {
      if (fn && (t.filename === fn || t.output_filename === fn || t.output_filename === stem)) {
        t.status = "rendering";
        ["pct", "step", "phase", "raw_phase", "total_steps", "window_index", "window_count", "window_pct"].forEach((k) => { if (es[k] !== undefined) t[k] = es[k]; });
      }
    });
  });
  const hasAuthEngines = Boolean(st?.engines);
  queue.forEach((t) => {
    const inAct = actSet.has(t.filename) || actSet.has(t.output_filename);
    const sseCur = state.sseTaskStatuses?.get(t.filename) || state.sseTaskStatuses?.get(t.output_filename);
    if (hasAuthEngines && !inAct) {
      if (sseCur?.status === "rendering") { state.sseTaskStatuses.delete(t.filename); state.sseTaskStatuses.delete(t.output_filename); }
      if (t.status === "rendering") { t.status = sseCur?.status === "completed" ? "completed" : (t.needs_render === false ? "skipped" : "queued"); _clearTransientRenderMeta(t); }
      return;
    }
    const sse = isRunning ? sseCur : null;
    if (sse && sse.status === "rendering" && t.status !== "completed" && t.status !== "skipped") {
      t.status = "rendering";
      t.pct = (sse.pct != null || t.pct != null) ? Math.max(Number(t.pct) || 0, Number(sse.pct) || 0) : (sse.pct === null ? null : t.pct);
      t.step = (sse.step != null || t.step != null) ? Math.max(Number(t.step) || 0, Number(sse.step) || 0) : (sse.step === null ? null : t.step);
      ["phase", "raw_phase", "total_steps", "window_index", "window_count", "window_pct"].forEach((k) => { if (sse[k] !== undefined) t[k] = sse[k]; });
    }
    if (!isRunning && t.status === "rendering") { t.status = t.needs_render === false ? "skipped" : "queued"; _clearTransientRenderMeta(t); }
  });
}
window.reconcileQueueStatuses = reconcileQueueStatuses;

window.reconcileSseActiveTask = function (match, evBackend) {
  if (!match || normQueueEngine(evBackend) !== "local") return;
  const activeSid = String(match.scene_id || "").trim().toUpperCase();
  (state.renderQueue || []).forEach((t) => {
    if (t === match || t.status !== "rendering" || normQueueEngine(t.backend_override || t.backend) !== "local") return;
    if (t.filename) state.sseTaskStatuses?.delete(t.filename);
    if (t.output_filename) state.sseTaskStatuses?.delete(t.output_filename);
    const sameSceneP1 = activeSid && match.pass === 2 && t.pass === 1 && String(t.scene_id || "").trim().toUpperCase() === activeSid;
    t.status = (sameSceneP1 || t.needs_render === false) ? "completed" : "queued";
    _clearTransientRenderMeta(t);
  });
};

window.syncRenderStatusWithQueue = function (json) {
  const q = state.renderQueue;
  if (!json?.engines || !Array.isArray(q) || !q.length) return;
  const renKey = () => q.filter((t) => t.status === "rendering").map((t) => t.output_filename || t.filename).join("|");
  const before = renKey();
  reconcileQueueStatuses(q, json, json.engine_status, Boolean(json.running || json.queue_worker_active));
  if (before !== renKey() && typeof window.renderQueueUI === "function") window.renderQueueUI();
};

function resetMultiEngineUI() {
  state.engineLive = { local: null, fal: null, minimax: null };
  _ensureActiveJobs().clear();
  const lanes = document.getElementById("multiEngineActiveLanes");
  if (lanes) { lanes.innerHTML = ""; lanes.style.display = "none"; }
}
window.resetMultiEngineUI = resetMultiEngineUI;

function updateEngineToggleCounts(queue = state.renderQueue || []) {
  const counts = { local: 0, fal: 0, minimax: 0 };
  (queue || []).forEach((t) => {
    if (t.status === "queued" || t.status === "rendering" || t.status === "failed") {
      counts[normQueueEngine(t.backend_override || t.backend)] += 1;
    }
  });
  const mapIds = { local: "engCntLocal", fal: "engCntFal", minimax: "engCntMinimax" };
  Object.entries(mapIds).forEach(([eng, id]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = counts[eng] > 0 ? `(${counts[eng]})` : "";
  });
}
window.updateEngineToggleCounts = updateEngineToggleCounts;

function renderQueueEngineCell(t) {
  const targetId = String(t.filename || t.id || t.output_filename || "").replace(/"/g, "&quot;");
  const defB = normQueueEngine(t.resolved_default_backend || t.backend || "local");
  const curOvr = t.backend_override ? normQueueEngine(t.backend_override) : "";
  const effB = curOvr || normQueueEngine(t.backend || defB);
  const meta = window.ENGINE_META[effB] || window.ENGINE_META.local;
  const defMeta = window.ENGINE_META[defB] || window.ENGINE_META.local;

  const readOnlyView = state.queueFilter === "all" || state.queueFilter === "completed";
  if (t.status === "rendering" || readOnlyView) {
    return `<td style="padding:4px 8px;width:118px;white-space:nowrap;">
      <span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600;background:${meta.bg};color:${meta.color};border:1px solid ${curOvr ? "rgba(245,158,11,0.5)" : "rgba(255,255,255,0.08)"};" title="${curOvr ? "Queue override" : "Inherited default"}">${meta.label}</span>
    </td>`;
  }
  const cls = curOvr ? "queue-engine-select is-override" : "queue-engine-select";
  return `<td style="padding:4px 8px;width:118px;white-space:nowrap;">
    <select class="${cls}" data-id="${targetId}" title="${curOvr ? "Per-pass queue override active (independent of scene setting)" : `Inheriting ${defMeta.short} from scene/project`}">
      <option value="" ${!curOvr ? "selected" : ""}>Auto (${defMeta.short})</option>
      <option value="local" ${curOvr === "local" ? "selected" : ""}>🖥️ Local GPU</option>
      <option value="fal" ${curOvr === "fal" ? "selected" : ""}>☁️ fal.ai</option>
      <option value="minimax" ${curOvr === "minimax" ? "selected" : ""}>🎥 MiniMax</option>
    </select>
  </td>`;
}
window.renderQueueEngineCell = renderQueueEngineCell;

function _findTaskBackendForPayload(payload) {
  if (payload?.backend) return normQueueEngine(payload.backend);
  const fn = payload?.filename || "";
  const stem = fn.replace(/\.(mp4|jpg|png|webp)$/i, "");
  const m = (state.renderQueue || []).find((t) => fn && (t.filename === fn || t.output_filename === fn || t.output_filename === stem));
  return m ? normQueueEngine(m.backend_override || m.backend) : "local";
}

function handleMultiEngineSSEEvent(tag, payload) {
  if (!state.engineLive) state.engineLive = { local: null, fal: null, minimax: null };
  const jobs = _ensureActiveJobs();
  const b = _findTaskBackendForPayload(payload);
  const fn = payload?.filename || "";
  const stem = fn.replace(/\.(mp4|jpg|png|webp)$/i, "");
  const jk = _jobKey(b, fn);
  if (tag === "[QUEUE_ACTIVE]") {
    const entry = {
      key: jk, backend: b, stem, running: true, draining: !getEnabledEnginesMap()[b],
      filename: fn, started_at: payload.started_at || Date.now() / 1000,
      phase: "starting", raw_phase: `Starting on ${window.ENGINE_META[b].short}...`,
      pct: b === "local" ? 0 : null, window_pct: b === "local" ? 0 : null,
      step: null, total_steps: null, window_index: payload.window_index, window_count: payload.window_count,
    };
    jobs.set(jk, entry);
    state.engineLive[b] = entry;
  } else if (tag === "[QUEUE_PROGRESS]") {
    const prev = (fn ? jobs.get(jk) : null) || state.engineLive[b] || Array.from(jobs.values()).find((j) => j.backend === b) || {};
    const effFn = fn || prev.filename || "";
    const effStem = effFn.replace(/\.(mp4|jpg|png|webp)$/i, "");
    const effJk = _jobKey(b, effFn);
    const entry = {
      ...prev, key: effJk, backend: b, stem: effStem, running: true, draining: prev.draining ?? !getEnabledEnginesMap()[b],
      filename: effFn, started_at: prev.started_at || Date.now() / 1000,
      phase: payload.phase || prev.phase, raw_phase: payload.raw_phase || prev.raw_phase,
      pct: payload.pct !== undefined ? payload.pct : prev.pct,
      window_pct: payload.window_pct !== undefined ? payload.window_pct : prev.window_pct,
      step: payload.step !== undefined ? payload.step : prev.step,
      total_steps: payload.total_steps !== undefined ? payload.total_steps : prev.total_steps,
      window_index: payload.window_index ?? prev.window_index,
      window_count: payload.window_count ?? prev.window_count,
    };
    if (entry.filename) jobs.set(effJk, entry);
    state.engineLive[b] = entry;
  } else if (tag === "[QUEUE_DONE]" || tag === "[QUEUE_FAIL]" || tag === "[QUEUE_CANCEL]") {
    if (fn) jobs.delete(jk);
    else if (b) Array.from(jobs.entries()).forEach(([k, v]) => { if (v?.backend === b) jobs.delete(k); });
    const rem = Array.from(jobs.values()).find((j) => j.backend === b);
    state.engineLive[b] = rem || null;
  }
  renderMultiEngineActiveLanes();
}
window.handleMultiEngineSSEEvent = handleMultiEngineSSEEvent;

function promoteActiveRenderTaskFromState(excludeFn = "") {
  const activeProj = state.renderStatus?.current_project;
  if (activeProj && state.currentSlug && activeProj !== state.currentSlug) {
    state.isEngineRunning = false;
    state.activeRenderTask = null;
    return false;
  }
  const exStem = String(excludeFn || "").replace(/\.(mp4|jpg|png|webp)$/i, "");
  const q = state.renderQueue || [];
  const nextSlot = Array.from(_ensureActiveJobs().values()).find((j) => j.running && (!exStem || j.stem !== exStem));
  if (nextSlot) {
    const qm = q.find((t) => (nextSlot.filename && t.filename === nextSlot.filename) || (nextSlot.stem && t.output_filename === nextSlot.stem) || t.status === "rendering");
    state.isEngineRunning = true;
    state.activeRenderTask = {
      idx: qm?.idx || 1, total: qm?.total || q.length, filename: nextSlot.filename || qm?.filename || "",
      backend: nextSlot.backend || qm?.backend || "local", reason: qm?.reason || "QUEUED",
      model: qm?.model_type, frames: qm?.video_length, resolution: qm?.resolution,
      pct: nextSlot.pct ?? null, window_pct: nextSlot.window_pct ?? null,
      phase: nextSlot.phase, raw_phase: nextSlot.raw_phase,
      step: nextSlot.step, total_steps: nextSlot.total_steps,
      window_index: nextSlot.window_index, window_count: nextSlot.window_count,
      started_at: nextSlot.started_at || null,
    };
    return true;
  }
  const remQ = q.find((t) => t.status === "rendering" && (!excludeFn || t.filename !== excludeFn));
  if (remQ) {
    state.isEngineRunning = true;
    state.activeRenderTask = {
      idx: remQ.idx || 1, total: remQ.total || q.length, filename: remQ.filename,
      backend: remQ.backend_override || remQ.backend || "local", reason: remQ.reason || "QUEUED",
      model: remQ.model_type, frames: remQ.video_length, resolution: remQ.resolution,
      pct: remQ.pct ?? null, window_pct: remQ.window_pct ?? null, phase: remQ.phase, raw_phase: remQ.raw_phase,
    };
    return true;
  }
  state.isEngineRunning = false;
  state.activeRenderTask = null;
  if (typeof resetEngineUI === "function" && !hasAnyOtherEngineRunning(excludeFn)) {
    if (state.renderTimerInterval) { clearInterval(state.renderTimerInterval); state.renderTimerInterval = null; }
    state.renderStartTime = null;
  }
  return false;
}
window.promoteActiveRenderTaskFromState = promoteActiveRenderTaskFromState;

function syncRenderAllButtonUI() {
  const runBtn = document.getElementById("btnRenderAll");
  if (!runBtn) return;
  const queue = state.renderQueue || [];
  const activeProj = state.renderStatus?.current_project;
  const otherBusy = Boolean(activeProj && state.currentSlug && activeProj !== state.currentSlug && (state.renderStatus?.running || state.renderStatus?.queue_worker_active || (window.activeEngineCount?.() || 0) > 0));
  const running = Boolean(!otherBusy && (state.isEngineRunning || state.activeRenderTask?.status === "rendering" || queue.some((t) => t.status === "rendering")));
  runBtn.textContent = otherBusy ? `⏳ Busy (${activeProj})` : (running ? ((window.activeEngineCount?.() || 0) > 1 ? "⏹ Stop All" : "⏹ Stop") : "▶ Run");
  runBtn.classList.toggle("btn-danger", running);
  runBtn.classList.toggle("btn-primary", !running && !otherBusy);
  runBtn.title = otherBusy ? `Render engine is active on project '${activeProj}'. Stop it or wait for completion.` : (running ? "Stop all active engines and pause queue" : "Run pending items for checked engines");
}
window.syncRenderAllButtonUI = syncRenderAllButtonUI;

function syncMultiEngineStatusUI(json) {
  if (!json) return;
  state.renderStatus = { ...(state.renderStatus || {}), ...json };
  if (json.enabled_engines) {
    state.enabledEngines = { ...getEnabledEnginesMap(), ...json.enabled_engines };
    document.querySelectorAll(".chk-engine-run").forEach((chk) => {
      const eng = chk.dataset.engine;
      if (eng && eng in state.enabledEngines && document.activeElement !== chk) chk.checked = Boolean(state.enabledEngines[eng]);
    });
  }
  if (json.max_parallel) {
    state.maxParallel = { local: 1, fal: clampEngineParallel("fal", json.max_parallel.fal), minimax: clampEngineParallel("minimax", json.max_parallel.minimax) };
    document.querySelectorAll(".engine-parallel-spinner").forEach((inp) => {
      const eng = inp.dataset.engine;
      if (eng && eng in state.maxParallel && document.activeElement !== inp) inp.value = String(state.maxParallel[eng]);
    });
  }
  if (json.engines) {
    if (!state.engineLive) state.engineLive = { local: null, fal: null, minimax: null };
    const jobs = _ensureActiveJobs();
    const seenKeys = new Set();
    window.ENGINES.forEach((b) => {
      const info = json.engines[b];
      if (!info || !info.running) {
        state.engineLive[b] = null;
        return;
      }
      const taskList = (Array.isArray(info.active_tasks) && info.active_tasks.length)
        ? info.active_tasks
        : [{ filename: info.filename || "", started_at: info.started_at, draining: info.draining, engine_status: info.engine_status }];
      taskList.forEach((tk, idx) => {
        const fn = tk.filename || tk.engine_status?.filename || "";
        const stem = fn.replace(/\.(mp4|jpg|png|webp)$/i, "");
        const jk = _jobKey(b, fn || `slot${idx}`);
        seenKeys.add(jk);
        const fSt = tk.engine_status || {};
        const cur = jobs.get(jk) || {};
        const entry = {
          ...cur, key: jk, backend: b, stem, running: true, draining: Boolean(tk.draining ?? info.draining),
          filename: fn || cur.filename || "",
          started_at: tk.started_at || fSt.started_at || cur.started_at || Date.now() / 1000,
          phase: fSt.phase || cur.phase || "rendering",
          raw_phase: fSt.raw_phase || cur.raw_phase || "Working...",
          pct: fSt.pct !== undefined ? fSt.pct : cur.pct,
          window_pct: fSt.window_pct !== undefined ? fSt.window_pct : (fSt.pct !== undefined ? fSt.pct : cur.window_pct),
          step: fSt.step !== undefined ? fSt.step : cur.step,
          total_steps: fSt.total_steps || cur.total_steps,
          window_index: fSt.window_index || cur.window_index,
          window_count: fSt.window_count || cur.window_count,
        };
        jobs.set(jk, entry);
        if (idx === 0) state.engineLive[b] = entry;
      });
    });
    for (const k of Array.from(jobs.keys())) {
      if (!seenKeys.has(k)) jobs.delete(k);
    }
  }
  syncRenderAllButtonUI();
  renderMultiEngineActiveLanes();
}
window.syncMultiEngineStatusUI = syncMultiEngineStatusUI;

function _fmtLaneElapsed(startedSec) {
  if (!startedSec) return "00:00";
  const el = Math.max(0, Math.floor(Date.now() / 1000 - Number(startedSec)));
  return `${String(Math.floor(el / 60)).padStart(2, "0")}:${String(el % 60).padStart(2, "0")}`;
}

function _safeClockId(k) { return `laneClock_${String(k).replace(/[^A-Za-z0-9_-]/g, "_")}`; }

function tickMultiEngineClocks() {
  _ensureActiveJobs().forEach((slot) => {
    const clk = document.getElementById(_safeClockId(slot.key));
    if (slot?.running && clk) clk.textContent = `⏱ ${_fmtLaneElapsed(slot.started_at)}`;
  });
}
window.tickMultiEngineClocks = tickMultiEngineClocks;

function _syncHeroLaneChrome(hasLanes) {
  const hdr = document.getElementById("activeTaskHeaderRow");
  const ttl = document.getElementById("activeTaskTitle");
  const bar = document.getElementById("activeProgressBarContainer");
  const det = document.getElementById("activeTaskDetails");
  if (hasLanes) {
    if (hdr) hdr.style.display = "none";
    if (ttl) ttl.style.display = "none";
    if (bar) bar.style.display = "none";
    if (det) det.style.display = "none";
  } else {
    if (ttl) ttl.style.display = "";
    if (det) { det.style.display = "flex"; det.style.marginTop = "2px"; }
  }
}

function renderMultiEngineActiveLanes() {
  const container = document.getElementById("multiEngineActiveLanes");
  if (!container) return;
  const active = Array.from(_ensureActiveJobs().values()).filter((s) => s.running).sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
  if (active.length === 0) {
    container.innerHTML = "";
    container.style.display = "none";
    _syncHeroLaneChrome(false);
    return;
  }
  _syncHeroLaneChrome(true);

  container.style.display = "flex";
  const activeProj = state.renderStatus?.current_project;
  const otherBusyBanner = (activeProj && state.currentSlug && activeProj !== state.currentSlug)
    ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 8px;border-radius:5px;background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.35);font-size:11px;color:#fcd34d;">
        <span>🔒 Engine busy on project <b>${typeof escapeHtml === "function" ? escapeHtml(activeProj) : activeProj}</b> — Queue/Done lists below remain isolated to this project.</span>
        <button type="button" onclick="window.selectProject?.('${String(activeProj).replace(/'/g, "\\'")}')" style="padding:1px 8px;font-size:10px;background:rgba(245,158,11,0.2);color:#fef3c7;border:1px solid rgba(245,158,11,0.45);border-radius:4px;cursor:pointer;">Switch to ${typeof escapeHtml === "function" ? escapeHtml(activeProj) : activeProj}</button>
      </div>`
    : "";
  container.innerHTML = otherBusyBanner + active.map((slot) => {
    const b = slot.backend || "local";
    const meta = window.ENGINE_META[b] || window.ENGINE_META.local;
    const hasNull = slot.pct === null || slot.pct === undefined;
    const pStr = `${slot.phase || ""} ${slot.raw_phase || ""}`.toLowerCase();
    const indetLbl = pStr.includes("download") ? "Downloading" : (pStr.includes("upload") || pStr.includes("submit")) ? "Uploading" : (pStr.includes("queue") || pStr.includes("prepar")) ? "Queued" : "Live";
    const rightTxt = hasNull ? indetLbl : `${slot.pct}%`;
    const phaseTxt = typeof formatActivePhaseLabel === "function" ? formatActivePhaseLabel(slot) : (slot.raw_phase || "Working...");
    const drainTag = slot.draining ? `<span style="background:rgba(245,158,11,0.18);color:#fbbf24;padding:1px 5px;border-radius:3px;font-size:9px;">Draining after pass</span>` : "";
    const escFn = typeof escapeHtml === "function" ? escapeHtml(slot.filename || "") : (slot.filename || "");
    const escPhase = typeof escapeHtml === "function" ? escapeHtml(phaseTxt) : phaseTxt;
    return `<div class="multi-engine-lane" data-engine="${b}" data-filename="${escFn}">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px;font-size:11px;">
        <div style="display:flex;align-items:center;gap:6px;min-width:0;">
          <span style="background:${meta.bg};color:${meta.color};padding:1px 6px;border-radius:4px;font-weight:600;white-space:nowrap;">${meta.label}</span>
          <span style="font-family:monospace;color:var(--text-main);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escFn}">${escFn || "Starting..."}</span>
          ${drainTag}
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
          <span id="${_safeClockId(slot.key)}" style="font-family:monospace;color:#38bdf8;">⏱ ${_fmtLaneElapsed(slot.started_at)}</span>
          <button type="button" class="btn btn-stop-engine" data-engine="${b}" data-filename="${escFn}" style="padding:1px 7px;font-size:10px;background:rgba(239,68,68,0.18);color:#fca5a5;border:1px solid rgba(239,68,68,0.4);" title="Stop this ${meta.short} job">⏹ Stop</button>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px;color:#7dd3fc;">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escPhase}</span>
        <span style="font-weight:600;margin-left:8px;white-space:nowrap;">${rightTxt}</span>
      </div>
      <div style="width:100%;height:5px;background:#1e293b;border-radius:3px;overflow:hidden;">
        <div class="${hasNull ? "is-indeterminate" : ""}" style="width:${hasNull ? 100 : slot.pct}%;height:100%;background:#38bdf8;transition:width 0.2s ease;"></div>
      </div>
    </div>`;
  }).join("");
}
window.renderMultiEngineActiveLanes = renderMultiEngineActiveLanes;

async function _postEngineSettings(payload, toastMsg = "", onRevert = null) {
  try {
    const res = await fetch("/api/render/engines", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: state.currentSlug, ...payload }),
    });
    const data = await res.json().catch(() => null);
    if (res.ok && data?.success) {
      syncMultiEngineStatusUI(data);
      if (toastMsg) showToast(toastMsg);
    } else {
      onRevert?.();
      showToast?.(`Engine settings failed: ${data?.error || `HTTP ${res.status}`}`);
    }
  } catch (err) {
    onRevert?.();
    console.error("Engine settings update error:", err);
  }
}

function initMultiEngineControls() {
  document.querySelectorAll(".chk-engine-run").forEach((chk) => {
    chk.addEventListener("change", () => {
      const b = chk.dataset.engine;
      if (!b) return;
      const map = getEnabledEnginesMap();
      const prevVal = map[b];
      map[b] = Boolean(chk.checked);
      state.enabledEngines = map;
      const msg = state.isEngineRunning ? (map[b] ? `▶ Engaged ${window.ENGINE_META[b].short} lane` : `⏸ Pausing ${window.ENGINE_META[b].short} after current pass`) : "";
      _postEngineSettings({ enabled_engines: map }, msg, () => {
        map[b] = prevVal;
        state.enabledEngines = { ...map };
        chk.checked = prevVal;
      });
    });
  });

  document.querySelectorAll(".engine-parallel-spinner").forEach((inp) => {
    inp.addEventListener("change", () => {
      const b = inp.dataset.engine;
      if (!b) return;
      const mp = getMaxParallelMap();
      const prevVal = mp[b];
      const clamped = clampEngineParallel(b, inp.value);
      inp.value = String(clamped);
      mp[b] = clamped;
      state.maxParallel = mp;
      _postEngineSettings({ max_parallel: mp }, `⚙️ ${window.ENGINE_META[b].short} max parallel: ×${clamped}`, () => {
        mp[b] = prevVal;
        state.maxParallel = { ...mp };
        inp.value = String(prevVal);
      });
    });
  });

  document.getElementById("queueTableBody")?.addEventListener("change", async (e) => {
    const sel = e.target.closest(".queue-engine-select");
    if (!sel || !sel.dataset.id) return;
    const slug = state.currentSlug || "all_or_nothing";
    const ovr = sel.value || "";
    try {
      const res = await fetch("/api/queue/set_engine", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, id: sel.dataset.id, backend_override: ovr }),
      });
      const data = await res.json();
      if (data?.success && data.queue) {
        if (typeof window.applyQueueApiResponse === "function") window.applyQueueApiResponse(data);
        else state.renderQueue = data.queue;
        renderQueueUI();
        showToast(ovr ? `🎛️ ${sel.dataset.id} → ${window.ENGINE_META[ovr].short}` : `🎛️ ${sel.dataset.id} → Auto engine`);
      } else if (data?.error) {
        showToast(`Engine override error: ${data.error}`);
        loadProjectQueue(slug);
      }
    } catch (err) { console.error("Set engine error:", err); }
  });

  document.getElementById("multiEngineActiveLanes")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-stop-engine");
    if (!btn || !btn.dataset.engine) return;
    window.stopActiveRender?.(btn.dataset.engine, btn.dataset.filename || null);
  });

  document.querySelectorAll?.("#queueSortControls .btn-queue-sort")?.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.sort) window.sortRenderQueue?.(btn.dataset.sort);
    });
  });

  document.getElementById("queueTableHead")?.addEventListener("click", (e) => {
    const th = e.target?.closest?.("th[data-sort]");
    if (th && th.dataset.sort && (state.queueFilter || "queued") !== "completed") {
      window.sortRenderQueue?.(th.dataset.sort);
    }
  });
}

(function () {
  const SORT_LABELS = { scene: "🎬 Scene Order", engine: "⚙️ Engine", length: "⏱ Length" };
  const ENGINE_RANK = { local: 0, fal: 1, minimax: 2 };

  function _passNumOf(t) {
    return t?.pass || (/Pass1/i.test(t?.output_filename || t?.filename || "") ? 1 : (/Pass2/i.test(t?.output_filename || t?.filename || "") ? 2 : null));
  }

  function _groupUnitsForSort(queue) {
    const unitMap = new Map(), units = [];
    (Array.isArray(queue) ? queue : []).forEach((t, i) => {
      const pNum = _passNumOf(t), sid = String(t?.scene_id || "").trim().toUpperCase();
      const k = (sid && pNum) ? `sid:${sid}` : `idx:${i}`;
      if (!unitMap.has(k)) { const u = []; unitMap.set(k, u); units.push(u); }
      unitMap.get(k).push(t);
    });
    units.forEach((u) => { if (u.length > 1) u.sort((a, b) => (_passNumOf(a) || 0) - (_passNumOf(b) || 0)); });
    return units;
  }

  function _sceneOrderKey(unit) {
    const t0 = unit[0] || {};
    const sid = String(t0.scene_id || "").trim().toUpperCase();
    const scenes = window.state?.projectData?.scenes || [];
    const idx = sid ? scenes.findIndex((s) => String(s?.id || "").trim().toUpperCase() === sid) : -1;
    if (idx >= 0) return [0, idx, ""];
    const m = (sid || String(t0.output_filename || t0.filename || "").split("_")[0].toUpperCase()).match(/^S0*(\d+)([A-Z]*)$/);
    return m ? [1, parseInt(m[1], 10), m[2] || ""] : [2, Number(t0.idx) || 999999, sid];
  }

  function _cmpSceneOrder(uA, uB) {
    const a = _sceneOrderKey(uA), b = _sceneOrderKey(uB);
    if (a[0] !== b[0]) return a[0] - b[0];
    if (a[1] !== b[1]) return a[1] - b[1];
    return a[2] < b[2] ? -1 : (a[2] > b[2] ? 1 : 0);
  }

  function _unitEngPair(unit) {
    const active = unit.filter((x) => x.status === "queued" || x.status === "failed");
    const list = active.length ? active : unit;
    const r0 = ENGINE_RANK[normQueueEngine(list[0]?.backend_override || list[0]?.backend)] ?? 9;
    const r1 = list[1] ? (ENGINE_RANK[normQueueEngine(list[1]?.backend_override || list[1]?.backend)] ?? 9) : r0;
    return [r0, r1];
  }

  function _unitLength(unit) {
    const t0 = unit[0] || {};
    if (t0.duration != null && Number.isFinite(Number(t0.duration))) return Number(t0.duration);
    const sid = String(t0.scene_id || "").trim().toUpperCase();
    const sc = (window.state?.projectData?.scenes || []).find((s) => String(s?.id || "").trim().toUpperCase() === sid);
    return sc ? Math.max(0, Number(sc.end || 0) - Number(sc.start || 0)) : 0;
  }

  function syncQueueSortUI() {
    const st = window.state || {};
    const sortBar = document.getElementById("queueSortControls");
    const filter = st.queueFilter || "queued";
    if (sortBar) sortBar.style.display = filter === "completed" ? "none" : "inline-flex";
    const cur = st.queueSort;
    document.querySelectorAll?.("#queueSortControls .btn-queue-sort")?.forEach((btn) => {
      const k = btn.dataset.sort;
      const isAct = Boolean(cur && cur.key === k);
      btn.classList?.toggle("active", isAct);
      btn.textContent = `${SORT_LABELS[k] || k}${isAct ? (cur.dir === "desc" ? " ↓" : " ↑") : ""}`;
    });
    document.querySelectorAll?.("#queueTableHead th[data-sort]")?.forEach((th) => {
      const k = th.dataset.sort;
      const isAct = Boolean(cur && cur.key === k && filter !== "completed");
      th.style.color = isAct ? "#7dd3fc" : "var(--text-muted)";
      const ind = th.querySelector?.(".qsort-ind");
      if (ind) ind.textContent = isAct ? (cur.dir === "desc" ? " ↓" : " ↑") : (filter !== "completed" ? " ↕" : "");
    });
  }
  window.syncQueueSortUI = syncQueueSortUI;

  window.clearQueueSortState = function () {
    if (window.state) window.state.queueSort = null;
    syncQueueSortUI();
  };

  window.sortRenderQueue = async function (sortKey, explicitDir = null) {
    if (!window.state || !SORT_LABELS[sortKey]) return [];
    const prev = window.state.queueSort;
    const dir = explicitDir || ((prev && prev.key === sortKey) ? (prev.dir === "asc" ? "desc" : "asc") : "asc");
    window.state.queueSort = { key: sortKey, dir };
    const mult = dir === "desc" ? -1 : 1;

    const units = _groupUnitsForSort(window.state.renderQueue || []);
    const renU = [], sortU = [], otherU = [];
    units.forEach((u, origIdx) => {
      if (u.some((x) => x.status === "rendering")) renU.push(u);
      else if (u.some((x) => x.status === "queued" || x.status === "failed")) sortU.push({ u, origIdx });
      else otherU.push(u);
    });

    sortU.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "scene") {
        cmp = _cmpSceneOrder(a.u, b.u) * mult;
      } else if (sortKey === "engine") {
        const [eA0, eA1] = _unitEngPair(a.u), [eB0, eB1] = _unitEngPair(b.u);
        cmp = (eA0 - eB0) * mult || (eA1 - eB1) * mult || _cmpSceneOrder(a.u, b.u);
      } else if (sortKey === "length") {
        const dA = _unitLength(a.u), dB = _unitLength(b.u);
        cmp = (dA - dB) * mult || _cmpSceneOrder(a.u, b.u);
      }
      return cmp || (a.origIdx - b.origIdx);
    });

    const nextFlat = [...renU.flat(), ...sortU.map((x) => x.u).flat(), ...otherU.flat()];
    nextFlat.forEach((t, i) => { t.idx = i + 1; t.total = nextFlat.length; });
    window.state.renderQueue = nextFlat;
    window.renderQueueUI?.();
    syncQueueSortUI();

    const order = sortU
      .map(({ u }) => String(u[0]?.output_filename || u[0]?.filename || u[0]?.id || u[0]?.scene_id || "").replace(/\.(mp4|jpg|jpeg|png|webp)$/i, ""))
      .filter(Boolean);
    const slug = window.state.currentSlug;
    if (slug && order.length > 0 && typeof fetch === "function") {
      try {
        const res = await fetch("/api/queue/reorder", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slug, order }),
        });
        const data = await res.json().catch(() => null);
        if (res?.ok && data?.queue) {
          if (typeof window.applyQueueApiResponse === "function") window.applyQueueApiResponse(data);
          else window.state.renderQueue = data.queue;
          window.renderQueueUI?.();
          syncQueueSortUI();
        }
      } catch (e) {}
    }
    window.showToast?.(`Sorted queue by ${SORT_LABELS[sortKey].replace(/^[^\s]+\s+/, "")} (${dir === "desc" ? "↓" : "↑"})`);
    return nextFlat;
  };
})();
window.handleCompletedAssetSSE = function (tag, rawText) {
  let doneSid = "";
  const bIdx = String(rawText || "").indexOf("{");
  if (bIdx !== -1) {
    try { doneSid = String(JSON.parse(rawText.slice(bIdx)).scene_id || "").trim(); } catch (e) {}
  }
  window.invalidateSceneTakesCache?.(doneSid || null);
  showToast?.(tag === "[UPSCALE_DONE]" ? "✨ Take upscaled and activated!" : "👄 Lip-sync repair complete and activated!");
  refreshProjectAssets?.(true).then(() => {
    const idx = state.selectedSceneIdx;
    const sc = idx !== null ? state.projectData?.scenes?.[idx] : null;
    if (sc && (!doneSid || String(sc.id || "").toUpperCase() === doneSid.toUpperCase())) {
      window.selectScene?.(idx);
    }
  });
};

(function () {
  function _ensureDoneStore() {
    if (!window.state) return [];
    const slug = window.state.currentSlug || "";
    if (window.state._doneQueueSlug !== slug) {
      window.state._doneQueueSlug = slug;
      window.state.doneQueue = [];
    }
    if (!Array.isArray(window.state.doneQueue)) window.state.doneQueue = [];
    return window.state.doneQueue;
  }

  const _FILL_DONE_KEYS = ["elapsed", "duration", "fal_endpoint", "resolution", "take_id", "take_number", "scene_id", "pass", "model_type", "backend"];

  function _doneMatchDistance(ex, rec) {
    if (!ex || ex.output_filename !== rec.output_filename) return null;
    if (ex.take_id && rec.take_id) return ex.take_id === rec.take_id ? 0 : null;
    const exT = Number(ex.completed_at || 0), recT = Number(rec.completed_at || 0);
    if (Boolean(ex._fromSse) !== Boolean(rec._fromSse) && Math.abs(exT - recT) <= 60) return Math.abs(exT - recT);
    if (Boolean(ex.take_id) !== Boolean(rec.take_id)) {
      const tTake = ex.take_id ? exT : recT, tShadow = ex.take_id ? recT : exT;
      return (tShadow - tTake >= -5 && tShadow - tTake <= 60) ? Math.abs(tShadow - tTake) : null;
    }
    return Math.abs(exT - recT) <= 5 ? Math.abs(exT - recT) : null;
  }

  function _mergeOneDone(list, rawItem) {
    if (!rawItem || typeof rawItem !== "object") return;
    const outFn = String(rawItem.output_filename || String(rawItem.filename || "").replace(/\.(mp4|jpg|jpeg|png|webp)$/i, "")).trim();
    if (!outFn) return;
    const comp = rawItem.completed_at !== undefined && rawItem.completed_at !== null ? Number(rawItem.completed_at) : null;
    if (comp === null || isNaN(comp) || comp <= 0) return;
    const rec = {
      ...rawItem,
      id: rawItem.id || outFn,
      output_filename: outFn,
      filename: rawItem.filename || `${outFn}.mp4`,
      status: "completed",
      needs_render: false,
      completed_at: comp,
    };
    let hit = null, bestDist = null;
    for (const ex of list) {
      const dist = _doneMatchDistance(ex, rec);
      if (dist !== null && (bestDist === null || dist < bestDist)) {
        hit = ex; bestDist = dist;
        if (dist === 0 && ex.take_id === rec.take_id) break;
      }
    }
    if (hit) {
      if (rec.take_id && !hit.take_id) {
        const saved = {};
        _FILL_DONE_KEYS.forEach((k) => { if (hit[k] !== undefined && hit[k] !== null && hit[k] !== "") saved[k] = hit[k]; });
        ["history_id", "take_id", "take_number", "completed_at", "reason", "elapsed", "duration", ..._FILL_DONE_KEYS].forEach((k) => {
          if (rec[k] !== undefined && rec[k] !== null && rec[k] !== "") hit[k] = rec[k];
        });
        Object.keys(saved).forEach((k) => { if (hit[k] === undefined || hit[k] === null || hit[k] === "") hit[k] = saved[k]; });
        delete hit._fromSse;
        return;
      }
      if (hit._fromSse && !rec._fromSse) {
        delete hit._fromSse;
        ["history_id", "take_id", "take_number", "completed_at", "reason", "elapsed", "duration"].forEach((k) => {
          if (rec[k] !== undefined && rec[k] !== null && rec[k] !== "") hit[k] = rec[k];
        });
      }
      _FILL_DONE_KEYS.forEach((k) => {
        if ((hit[k] === undefined || hit[k] === null || hit[k] === "") && rec[k] !== undefined && rec[k] !== null && rec[k] !== "") hit[k] = rec[k];
      });
    } else {
      list.push(rec);
    }
  }

  window.preserveDoneQueueItems = function (items) {
    const store = _ensureDoneStore();
    (Array.isArray(items) ? items : []).forEach((t) => {
      if (t && t.status === "completed") _mergeOneDone(store, t);
    });
    return store;
  };

  window.recordDoneQueueItem = function (item, payload = null) {
    const store = _ensureDoneStore();
    const base = item && typeof item === "object" ? { ...item } : {};
    if (payload && typeof payload === "object") {
      if (payload.filename && !base.filename) base.filename = payload.filename;
      if (payload.elapsed !== undefined && payload.elapsed !== null) base.elapsed = payload.elapsed;
      if (payload.completed_at !== undefined && payload.completed_at !== null) base.completed_at = payload.completed_at;
      if (payload.take_id) base.take_id = payload.take_id;
      if (payload.take_number !== undefined && payload.take_number !== null) base.take_number = payload.take_number;
    }
    if (!base.completed_at) base.completed_at = Math.floor(Date.now() / 1000);
    base._fromSse = true;
    base.status = "completed";
    base.needs_render = false;
    _mergeOneDone(store, base);
    return store;
  };

  window.getDoneQueueEntries = function (queue) {
    const store = _ensureDoneStore();
    const merged = store.map((x) => ({ ...x }));
    (Array.isArray(queue) ? queue : []).forEach((t) => {
      if (t && t.status === "completed") {
        if (t.completed_at !== undefined && t.completed_at !== null && Number(t.completed_at) > 0) {
          _mergeOneDone(merged, t);
        } else if (!merged.some((ex) => (ex.output_filename || ex.filename) === (t.output_filename || t.filename))) {
          merged.push({ ...t, status: "completed" });
        }
      }
    });
    return merged;
  };

  window.isQueueEventForCurrentProject = function (evSlug = "") {
    const st = window.state;
    if (!st) return false;
    const activeProj = String(evSlug || ((st.renderStatus?.running || st.renderStatus?.queue_worker_active) ? st.renderStatus?.current_project : "") || "").trim();
    return !(activeProj && st.currentSlug && activeProj !== st.currentSlug);
  };

  window.applyQueueApiResponse = function (data, expectedSlug = null) {
    if (!window.state || !data) return;
    const curSlug = String(window.state.currentSlug || "").trim();
    const respSlug = String(data.slug || expectedSlug || "").trim();
    if (respSlug && curSlug && respSlug !== curSlug) return;
    if (!window.state._renderQueueSlug || window.state._renderQueueSlug === curSlug) {
      window.preserveDoneQueueItems(window.state.renderQueue);
    }
    const store = _ensureDoneStore();
    if (Array.isArray(data.done_queue)) {
      data.done_queue.forEach((d) => _mergeOneDone(store, d));
    }
    if (Array.isArray(data.queue)) {
      if (window.state._renderQueueSlug !== curSlug) window.state.queueSort = null;
      window.state._renderQueueSlug = curSlug;
      window.preserveDoneQueueItems(data.queue);
      window.state.renderQueue = data.queue;
    }
  };
})();

initMultiEngineControls();
