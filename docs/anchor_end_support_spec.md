# Specification: End Anchor Support (`anchor_end`) for Seamless Scene Blending

**Document Version:** 1.1.0  
**Status:** Reviewed, Hardened & Implemented  
**Target:** Music Video Generation Studio (Backend, Pipeline, CLI & Web UI)

---

## 1. Executive Summary & Problem Statement

### 1.1 Problem
When long scenes (e.g., Scene 10, a 24.89s musical showcase with piano, strings, and brass) are broken into smaller manageable sub-scenes, standard video stitching results in visible discontinuities:
1. **Jump Cuts at the Seams:** Camera position, lighting direction, and musician placement shift abruptly.
2. **Cascading Degradation ("Photocopy of a Photocopy"):** If the final frame of an already-rendered lower-res video clip is extracted and used as the start anchor for the subsequent scene, 3D VAE lossy decoding, motion blur, and H.264 compression artifacts compound with every cut. By the third scene, faces and fine details degrade noticeably.

### 1.2 Solution
The studio's primary model family, **MiniMax H3 FL2VA** (Renderers 1–8), natively supports **First-and-Last Frame Conditioning** (`image_prompt_type: "SE"` with `image_start` and `image_end`).

By introducing an optional **`anchor_end`** property to scenes:
* The user generates **Anchor A (Start)** and **Anchor B (End)** as pristine **2K stills in Stage 1 Anchor Studio**.
* The video generation model synthesizes the camera path, physics, and lighting to travel smoothly from Anchor A to Anchor B.
* For the subsequent scene, Anchor B becomes the start anchor, guaranteeing that **both sides of the cut point reference the exact same crystal-clear 2K still file**.

---

## 2. Architecture & Data Model

### 2.1 Scene Configuration Schema (`projects/<slug>/*.json`)

Each scene dictionary in `scenes` supports an optional `anchor_end`:

```json
{
  "id": "S10a",
  "start": 66.30,
  "end": 74.50,
  "mode": "solo",
  "anchor": "A05 - 2Man_and_woman_looking_at_8410e52f_2K.jpg",
  "anchor_end": "A06 - 2Woman_playing_electric_guitar_f92b39a1_2K.jpg",
  "renderer": 1,
  "seed": 1000,
  "stem": "master",
  "prompt": "For the target video, at 0.00 seconds into the target video, <Picture 1> is fully referenced. At 8.20 seconds, <Picture 2> is fully referenced. The camera slowly glides forward and pans left from <Picture 1> across the soundstage floor toward the sapphire-blue spotlight to arrive cleanly at <Picture 2>."
}
```

* **`anchor`:** Mandatory start anchor (`image_start`, `<Picture 1>`).
* **`anchor_end`:** Optional ending anchor (`image_end`, `<Picture 2>`). Can be an anchor ID (`"A06"`), a filename string, or `null`/empty/omitted.

---

## 3. Component Touchpoints & Requirements

### 3.1 Pipeline Context (`work/pipeline/context.py` & `work/pipeline/media_utils.py`)

1. **`make_video_task(...)` Signature & Capability Guard:**
   * Extend signature with `image_end: Path | None = None`.
   * **Model & Pass Guard:** Ref2VA models (`is_ref2va=True`, Renderers 9–13) use multimodal subject references (`video_prompt_type="I"`) and do not use `image_end` / `image_prompt_type="SE"`. Likewise, Pass 2 GVA (`video_guide` and `video_mask`) is guided by Pass 1 video + spatial mask rather than an end still. Define:
     ```python
     supports_end = bool(rspec.get("supports_end_anchor", not is_ref2va)) and not (video_guide and video_mask)
     has_end = bool(supports_end and image_end and Path(image_end).exists())
     ```
   * When `has_end` is `True`:
     * `task["image_end"] = str(image_end)`
     * If `"E"` is not in `task.get("image_prompt_type", "")`:
       * Set `task["image_prompt_type"] = "SE"` (or append `"E"`).
   * If `has_end` is `False`, retain existing behavior (`image_prompt_type` defaults to `"S"` from rspec/shared).
2. **`_NON_SETTING_TASK_KEYS` (`work/pipeline/media_utils.py`):**
   * Include `"image_end"` alongside `"image_start"` in `_NON_SETTING_TASK_KEYS` so `image_end` is tracked via task/anchor hashes rather than treated as an opaque render setting override.
3. **Anchor Resolution:**
   * Existing `ctx.resolve_anchor_path(...)` resolves both anchor IDs (`"A06"`) and filenames, and is reused for `anchor_end`.

### 3.2 Task Hashing, Two-Pass Duet & Cascade Invalidation (`work/pipeline/stage2_scenes.py` & `work/ui/server_queue.py`)

1. **`compute_project_task_hashes(ctx, ...)`:**
   * For solo/single-pass scenes **and** Pass 1 (`p1_base`) of two-pass duet scenes, when the active renderer supports end anchors (`not is_ref2va`) and `sc.get("anchor_end")` resolves to an existing file on disk:
     ```python
     if supports_end and sc.get("anchor_end"):
         _, a_end_p = ctx.resolve_anchor_path(sc.get("anchor_end"))
         if a_end_p.exists():
             base_payload["anchor_end"] = a_end_p.name
             base_payload["a_end_hash"] = _file_sha256(a_end_p)
     ```
   * **Backward Compatibility:** When `anchor_end` is absent/empty (or on Ref2VA), `base_payload` / `p1_base` omits `anchor_end` keys. Existing render hashes remain 100% identical.
2. **`run_stage2_scenes(...)`:**
   * Pass `image_end=anchor_end_path` to `ctx.make_video_task` in **both** solo scenes and Pass 1 (`t_p1`) of two-pass duet scenes.
   * **Regeneration & Disk Cascade:** Check `anchor_end` alongside `anchor` for `anchor_was_regen`, `anchor_newer` / `anchor_newer_p1`, and record `image_end`'s SHA-256 in `state["anchor_file_hashes"]` upon render completion.
3. **Fast Queue Diagnostics (`work/ui/server_queue.py`):**
   * In `compute_fast_project_queue` (`_evaluate_pass`), check both `sc.get("anchor")` and `sc.get("anchor_end")` (when `pass_num != 2`) for disk mtime (`ANCHOR <id> NEWER ON DISK`) and SHA-256 changes (`ANCHOR <id> EDITED`), and use `max(start_mtime, end_mtime)` for user-skip mtime tracking.

### 3.3 Prompting Convention & Character Tag Compilation (`work/pipeline/characters.py` & `work/pipeline/prompts.py`)

* In FL2VA (MiniMax H3), when both start and end images are provided:
  * `<Picture 1>` references `image_start`.
  * `<Picture 2>` references `image_end`.
* **`compile_character_prompt` Preservation of `<Picture 2>`:**
  * When a prompt uses `@character` tags on a text-only renderer (`supports_refs=False`, such as FL2VA or LTX-2), `compile_character_prompt` expands `@tags` to text and strips unused `<Picture N>` reference tags.
  * When `has_end=True`, `<Picture 2>` is the **End Anchor**, so `compile_character_prompt(..., has_end=True)` MUST preserve `<Picture 2>` and only strip `<Picture 3..99>`.
* **`adapt_video_prompt` Adaptation (`work/pipeline/prompts.py`):**
  * For non-H3 prompt styles (e.g. LTX-2), replace `<Picture 1>` with `"the starting frame"` and `<Picture 2>` with `"the ending frame"`.

### 3.4 Take Rollback & Render Vault (`work/pipeline/takes.py` & `work/ui/static/js/takes_and_history.js`)

1. **`promote_take_to_active` (`work/pipeline/takes.py`):**
   * Exclude `"image_end"` from `render_settings` in `record_new_take` (matching `"image_start"`).
   * When restoring a take snapshot (`snap`), if `"anchor_end"` is not present in `snap`, explicitly remove any stale `"anchor_end"` from `target_sc` (`target_sc.pop("anchor_end", None)`).
   * Harmonize restored clip mtime above both `anchor` and `anchor_end` file mtimes on disk.
2. **Frontend Take Activation & Diff (`work/ui/static/js/takes_and_history.js`):**
   * When `/api/takes/activate` returns `restored_params`, sync `scene.anchor` and `scene.anchor_end` (deleting `scene.anchor_end` if absent in `restored_params`) before calling `saveProjectData(false)`.
   * Include `anchor_end` differences in `renderSelectedTakeState` historical take diff summary.

### 3.5 CLI & Operations (`.agents/skills/` & `.claude/skills/mvg-project-ops/scripts/project_tool.py`)

1. **`show-scene`:**
   * Print `Anchor   : <anchor> | End: <anchor_end>` when `anchor_end` is present.
2. **`set-scene`:**
   * `--anchor-end <val>` sets `anchor_end`, or clears it when `<val>` is `""`, `"none"`, `"null"`, or `"clear"`.
3. **`audit` & `summary`:**
   * Extract canonical anchor IDs (`A01`..`A99`) from `a.get("id") or a.get("filename")` so projects whose `anchors` array stores `"filename"` without `"id"` are properly indexed.
   * Verify that both `anchor` and `anchor_end` exist in project anchor definitions **and** resolve to an existing file on disk in the project's anchors directory.
   * Keep `.agents/skills/` and `.claude/skills/` copies of `project_tool.py` 100% synchronized.

### 3.6 Scene Operations (`work/pipeline/scene_ops.py`)

1. **`split_scene_in_project_data(...)`:**
   * When splitting a scene that had an `anchor_end`:
     * Part 1 (`scene1`) retains the original `anchor` (start) and pops `anchor_end`.
     * Part 2 (`scene2`) inherits the original `anchor_end` as its terminal anchor.

### 3.7 Studio Web UI (`work/ui/static/index.html` & `scene_inspector.js`)

1. **HTML Layout (`index.html`):**
   * Add `End Anchor (Optional)` control in `#sceneInspectorForm` with `<select id="sceneAnchorEnd">`, a `🔗 Match Next` quick-action (`#btnMatchNextAnchor`) to copy the next scene's start anchor in 1 click, and a `Clear` action (`#btnClearEndAnchor`).
2. **State & Event Synchronization (`scene_inspector.js`):**
   * Populate `sceneAnchorEnd` options during `updateInspectorAnchorsDropdown()`.
   * On scene selection, sync `sceneAnchorEnd` and toggle `#btnClearEndAnchor` and `#btnMatchNextAnchor` visibility.
   * On change / clear / match-next: mutate `sc.anchor_end`, refresh prompt resolved previews (`updatePromptResolvedPreviews(sc)`), call `markProjectDirty()`, and record an undo history snapshot.
   * In `splitScene(idx)` (frontend), also `delete scene1.anchor_end` to match backend `split_scene_in_project_data`.

---

## 4. Verification & Testing Plan

1. **Unit Tests (`tests/test_anchor_end.py`):**
   * **Test 1:** `make_video_task` with `image_end` sets `image_prompt_type="SE"` and `task["image_end"]` string path.
   * **Test 2:** `make_video_task` without `image_end` defaults to `"S"` and has no `image_end` key.
   * **Test 3:** `compute_project_task_hashes` produces different hashes when `anchor_end` is populated vs when it is absent.
   * **Test 4:** `compute_project_task_hashes` produces identical hashes to legacy code for scenes without `anchor_end`.
   * **Test 5:** `project_tool.py set-scene` sets and clears `--anchor-end` cleanly.
   * **Test 6:** `project_tool.py audit` reports valid and missing end anchors.
2. **Regression Tests:**
   * Run full test suite:
     `python -m unittest tests/test_backend_queue.py tests/test_takes_and_rollback.py tests/test_wan2gp_integration.py`
3. **Line Count & Modular Integrity:**
   * Ensure modified files (`context.py`, `stage2_scenes.py`, `scene_inspector.js`, `anchors_and_settings.js`) strictly remain under the 850-line limit.

---

## 5. Implementation Stages

* **Phase 1: Pipeline Core (`context.py`, `stage2_scenes.py`)** — Data structures, task dictionary injection, and hash caching.
* **Phase 2: CLI & Diagnostics (`project_tool.py`, `scene_ops.py`)** — Scene splitting, audit, and editing commands.
* **Phase 3: Web UI (`index.html`, `scene_inspector.js`, `anchors_and_settings.js`)** — End anchor selector, reactive binding, dirty state.
* **Phase 4: Test Suite & Verification** — Comprehensive unit and regression test execution.
