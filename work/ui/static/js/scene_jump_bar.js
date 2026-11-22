/* Full-width proportional scene quick-jump & repeat bar above timeline */

function getSceneJumpModeClass(mode) {
  const m = String(mode || "").toLowerCase();
  if (["duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right"].includes(m)) return "mode-duet-b";
  if (["duet_method_a", "duet_a"].includes(m)) return "mode-duet-a";
  if (["silent", "silent_locked", "ambient"].includes(m)) return "mode-silent";
  return "mode-solo";
}

function syncSceneJumpBarHighlight() {
  const bar = document.getElementById("sceneJumpBar");
  if (!bar) return;
  const selIdx = state.selectedSceneIdx;
  const loopId = state.masterLoopSceneId || null;
  bar.querySelectorAll(".scene-jump-btn").forEach((btn) => {
    const idx = parseInt(btn.dataset.sceneIdx, 10);
    const scId = btn.dataset.sceneId || "";
    const isSel = selIdx !== null && selIdx !== undefined && idx === selIdx;
    const isLoop = Boolean(loopId && scId === loopId);
    btn.classList.toggle("is-selected", isSel);
    btn.classList.toggle("is-looping", isLoop);
    btn.setAttribute("aria-pressed", isSel ? "true" : "false");
    const rep = btn.querySelector(".scene-jump-repeat");
    if (rep) {
      rep.classList.toggle("is-looping", isLoop);
      rep.setAttribute("aria-pressed", isLoop ? "true" : "false");
    }
  });
}

function renderSceneJumpBar() {
  const bar = document.getElementById("sceneJumpBar");
  if (!bar) return;
  const scenes = state.projectData?.scenes || [];
  if (!scenes.length) {
    bar.innerHTML = "";
    bar.dataset.structKey = "";
    return;
  }

  const structKey = scenes.map((s) => `${s.id || ""}:${s.start}:${s.end}:${s.mode || ""}`).join("|");
  if (bar.dataset.structKey === structKey && bar.children.length === scenes.length) {
    syncSceneJumpBarHighlight();
    return;
  }

  bar.dataset.structKey = structKey;
  bar.innerHTML = "";
  const totalDur = scenes.reduce((acc, s) => acc + Math.max(0.05, Number(s.end || 0) - Number(s.start || 0)), 0) || 1;
  const repeatGlyph = `<svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 5.5h9.5l-2-2"/><path d="M12 5.5l-2 2"/><path d="M13.5 10.5H4l2 2"/><path d="M4 10.5l2-2"/></svg>`;

  scenes.forEach((sc, idx) => {
    const startT = Number(sc.start || 0);
    const endT = Number(sc.end || 0);
    const dur = Math.max(0.05, endT - startT);
    const pct = (dur / totalDur) * 100;

    const btn = document.createElement("div");
    btn.className = `scene-jump-btn ${getSceneJumpModeClass(sc.mode)}`;
    btn.setAttribute("role", "button");
    btn.setAttribute("tabindex", "0");
    btn.dataset.sceneIdx = String(idx);
    btn.dataset.sceneId = sc.id || "";
    btn.dataset.widthPct = pct.toFixed(4);
    btn.style.flex = `${dur} 1 0%`;
    btn.title = `${sc.id || `S${idx + 1}`} (${startT.toFixed(2)}s – ${endT.toFixed(2)}s, ${dur.toFixed(2)}s)`;

    const lbl = document.createElement("span");
    lbl.className = "scene-jump-label";
    lbl.textContent = sc.id || `S${idx + 1}`;
    btn.appendChild(lbl);

    const rep = document.createElement("button");
    rep.type = "button";
    rep.className = "scene-jump-repeat";
    rep.title = `Loop ${sc.id || `S${idx + 1}`} in Master Video`;
    rep.setAttribute("aria-label", `Repeat ${sc.id || `S${idx + 1}`}`);
    rep.innerHTML = repeatGlyph;
    rep.onclick = (e) => {
      e?.stopPropagation?.();
      window.toggleSceneMasterLoop?.(idx);
    };
    btn.appendChild(rep);

    btn.onclick = (e) => {
      if (e?.target?.closest?.(".scene-jump-repeat")) return;
      window.focusSceneInTimeline?.(idx);
    };
    btn.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        window.focusSceneInTimeline?.(idx);
      }
    };

    bar.appendChild(btn);
  });

  syncSceneJumpBarHighlight();
}

window.renderSceneJumpBar = renderSceneJumpBar;
window.syncSceneJumpBarHighlight = syncSceneJumpBarHighlight;
document.addEventListener?.("DOMContentLoaded", renderSceneJumpBar);
window.addEventListener?.("projectLoaded", () => {
  const bar = document.getElementById("sceneJumpBar");
  if (bar) bar.dataset.structKey = "";
  renderSceneJumpBar();
});
