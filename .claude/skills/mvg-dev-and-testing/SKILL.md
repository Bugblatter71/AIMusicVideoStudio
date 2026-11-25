---
name: mvg-dev-and-testing
description: Verification workflows, test suite matrix, modularity ceilings (<850 lines), facade rules, and Windows sandbox/PowerShell guidelines for maintaining the MVG codebase.
---

# MVG Development, Maintenance & Test Verification

Use this skill whenever editing Studio backend or frontend source code, adding UI controls, refactoring modules, or running tests.

---

## 1. Non-Negotiable Codebase Guardrails

1. **Modularity Ceiling (`< 850` lines)**:
   Every `.py`, `.js`, `.html`, and `.css` file under `work/` (excluding `work/Wan2GP`) **must remain strictly `< 850` lines**. Multiple tests (`test_backend_queue.py`, `test_resolution_and_upsampling.py`, `test_scene_persistence.py`) enforce this automatically.
2. **Facade Compatibility**:
   Preserve re-exports in `work/run_overnight_v3.py` and `work/ui/server.py` (and `ensure_engine_state` / shared locks) so batch files and tests importing them never break.
3. **Preserve Tested DOM & Asset Contracts in `work/ui/static/index.html`**:
   Several integration tests check exact strings such as `js/fal_settings.js?v=1`, `js/queue_and_console.js?v=45`, `js/master_and_export.js?v=47`, and core element IDs (`#masterVideoContainer`, `#masterStitchOverlay`, `#multiEngineActiveLanes`, etc.). Do not change `?v=` suffixes without checking `tests/` first.
4. **User Active Project Safety**:
   Never mutate `projects/all_or_nothing/all_or_nothing_v4.json` directly without creating a timestamped backup in `backup/`.
5. **No Live GPU Tests Without Explicit Confirmation**:
   Never set `RUN_LIVE_WAN2GP_TESTS=1` unless the user explicitly confirms the GPU is idle.

---

## 2. Fast Pre-Flight Repo Health Check (`verify_repo_health.py`)

Runs four fast offline checks in one command (`< 850` line limit, Python bytecode compilation across `work/` & skills, `node -c` syntax check across all `work/ui/static/js/*.js`, and `.agents/skills` <-> `.claude/skills` parity):

```bash
python .agents/skills/mvg-dev-and-testing/scripts/verify_repo_health.py
```

---

## 3. Test Suite Matrix

### Standard Regression Suite (No GPU Required)
```bash
python -m unittest tests/test_regression_fixes.py tests/test_backend_queue.py tests/test_fal_integration.py tests/test_takes_and_rollback.py tests/test_resolution_and_upsampling.py tests/test_wan2gp_integration.py -v
```

### Targeted Component Tests
- Scene mutations & persistence: `python -m unittest tests/test_scene_persistence.py tests/test_anchor_end.py tests/test_scene_render_padding.py -v`
- Cross-engine Duet ROI & lip-sync repair: `python -m unittest tests/test_duet_roi.py -v`
- General unit coverage: `python -m unittest tests/test_unit_coverage.py -v`
- **Playwright E2E UI tests (`BypassSandbox: true` required on Windows)**: Never run `tests/test_ui_e2e_playwright.py` or `tests/ui_render_queue.spec.js` inside the Windows sandbox — Chromium subprocesses and localhost HTTP binding hang inside the sandbox until canceled. Always invoke Playwright E2E directly with `BypassSandbox: true` on the first attempt, while running `verify_repo_health.py`, `symbol_index.py`, and non-browser unit suites inside the sandbox.

> [!NOTE]
> On Windows PowerShell, separate multiple commands in one shell call with `;` (never `&&`). Tests in `test_backend_queue.py` mutate temporary copies or `projects/test_project/work/`; keep tests hermetic by backing up/restoring queue state in `try/finally`.
