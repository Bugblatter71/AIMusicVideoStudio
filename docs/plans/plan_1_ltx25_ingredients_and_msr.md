# Plan 1: LTX-2.5 Multi-Reference Video (`ltx2_25_22B_msr` & Ingredients Reference Sheet)

> **Target implementer:** Hydrogen coding agent  
> **Scope:** Wire two local LTX-2.5 multi-reference video workflows into Music Video Generation Studio using the already-downloaded `ltx-2.5-22b-dev` / `ltx-2.5-22b-distilled` base weights (only small LoRA/slot-embedding downloads on first GPU run).  
> **Non-goals:** Do not modify existing `#14` (`ltx2_25_22B_distilled`) or `#16` (`ltx2_25_22B`) default behavior/hashes for existing scenes; do not run live GPU tests (`RUN_LIVE_WAN2GP_TESTS=1`) without explicit user confirmation; do not touch active user project files under `projects/`.

---

## 1. Verified Wan2GP Ground-Truth Contracts

Verified directly from local Wan2GP (`C:\Users\bugbl\Downloads\pinokio\api\wan.git\app`):

### A. LTX-2.5 MSR (`ltx2_25_22B_msr`)
Source: `defaults/ltx2_25_22B_msr.json`, `models/ltx2/ltx2_handler.py` (L163–169, L555, L752–776, L980–994, L1224–1235), `models/ltx2/msr.py`, `models/ltx2/ltx2.py` (L1629–1636).

- **Weights reused:** `"URLs": "ltx2_25_22B_distilled"` (already downloaded). Auto-downloads only:
  - `LTX-2.5-Licon-MSR-V1_slot_embeddings_bf16.safetensors`
  - `loras/LTX-2.5-Licon-MSR-V1_bf16.safetensors`
- **Hard validation rules in `ltx2_handler.py` L980–994 (violation aborts job):**
  1. `image_prompt_type` **MUST NOT** contain `S`, `V`, or `L` (`image_prompt_types_allowed: "TE"`). **Never pass `image_start`**. Use `"T"` (or `"TE"` only when `image_end` is explicitly set and exists).
  2. `video_prompt_type` **MUST** contain `"I"` and **MUST NOT** contain any of `O P D E V G & F A`:
     - **`"KI"` (Background + 1–4 Subjects):** requires `2 <= len(image_refs) <= 5`, with **background/environment image FIRST (`image_refs[0]`)**, followed by 1–4 subject/object images. Wan2GP internally rotates `refs[1:] + refs[:1]` and removes backgrounds from subject slots when `remove_background_images_ref: 1`.
     - **`"I"` (Subjects / Objects Only):** requires `1 <= len(image_refs) <= 4` (no background slot).
  3. `audio_prompt_type` **MUST NOT** contain `K` or `2`. Use **`"AS"`** when conditioning on a vocal/soundtrack `audio_guide` (kept as output soundtrack), or **`""`** for silent/ambient scenes.
  4. **No sliding windows** (`"sliding_window": not msr` -> `False` at `ltx2_handler.py` L555). Single window only; default length `145` frames (~6s at 24fps). Long scenes (> ~241 frames / 10s) risk VRAM pressure.
  5. **No Pass 2 masked inpainting / control video** (`video_guide` / `video_mask` forbidden).
- **Sampling & format defaults:**
  - `frames_formula`: `"ltx2"` (`8*k + 1`, min `17`, 24 fps)
  - `num_inference_steps`: `8`, `guidance_phases`: `1` (distilled pipeline overrides guidance/steps to 8, CFG 1.0), `sample_solver`: `"euler"`
  - `remove_background_images_ref`: `1`
  - `resolution`: 32px-aligned (Wan2GP default `1280x704`; 64px-aligned also safe for two-stage/tiling).
  - `prompt_style`: `"ltx2"` (natural prose describing subjects, actions, and each reference's role — **NEVER** MiniMax H3 6-section `<Picture N>` / `<Subject K>` markup!).

### B. LTX-2.5 Ingredients Reference Sheet (`video_prompt_type: "I"` on `ltx2_25_22B_distilled` / `ltx2_25_22B`)
Source: `models/ltx2/infos.py` (L67, L114, L171), `models/ltx2/ltx2_handler.py` (L136, L734, L748), `models/ltx2/ltx2.py` (L137–139, L1358–1359, L1650–1657).

- **Weights reused:** `ltx2_25_22B_distilled` or `ltx2_25_22B` (already downloaded). Auto-downloads only `ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors` (weight `1.4`, auto-injected by Wan2GP).
- **Activation contract (`ltx2.py` L137–139):**
  - `_ltx2_main_ingredients_enabled` is `True` iff `"I" in video_prompt_type` and **NONE** of `K F V O P D E M A &` are in `video_prompt_type`.
  - Therefore, **mutually exclusive with Pass 2 `MVGA`** (`M`, `V`, `A` present -> Ingredients must be disabled automatically on Pass 2!).
- **Single composite sheet requirement (`ltx2_handler.py` L748 & `infos.py` L67):**
  - Wan2GP sets `one_image_ref_only = True` and duplicates the single `image_refs[0]` image as the IC-LoRA guide video.
  - Best input format per Wan2GP docs: **one clean composite reference-sheet image on a white background, with thin black separator lines between individual pieces and NO text labels**.
  - Unlike MSR, Ingredients **DOES** support `image_start` (`image_prompt_type: "S"` or `"SE"`), `audio_guide` (`audio_prompt_type: "AS"` or `"A"`), AND sliding windows (`241f`, `9f` overlap)!

---

## 2. Critical Codebase Guardrails (Modularity & Existing Pitfalls)

Check file line counts before editing — **every file under `work/` MUST stay strictly `< 850` lines** (`python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits`):
- `work/pipeline/context.py` is currently **848 lines** (only 2 lines of headroom!). **DO NOT add multi-line logic into `context.py`**. Create a dedicated new module **`work/pipeline/ltx_refs.py`** and call concise 1–3 line delegates from `context.py` (or extract existing helpers if needed).
- `work/ui/static/js/duet_repair.js` (837 lines), `work/ui/static/js/scene_inspector.js` (838 lines), `work/ui/static/js/scenes_tab.js` (843 lines), `work/ui/static/js/characters_studio.js` (840 lines), and `work/ui/server_queue.py` (844 lines) are all near 850 lines — keep edits minimal/surgical or factor shared helpers cleanly.
- **CRITICAL EXISTING ARCHITECTURAL BUG TO FIX FIRST:**
  In `work/pipeline/fal_catalog.py` L585–593:
  ```python
  def is_ref2va_spec(rspec: dict | None, spec_or_sc: dict | None = None) -> bool:
      ...
      if str(r.get("frames_formula") or "").lower() == "h3_ref2va" or r.get("supports_image_refs"):
          return True
  ```
  Because `is_ref2va_spec()` currently treats ANY `supports_image_refs: True` model as MiniMax H3 Ref2VA:
  1. `context.py` L683–715 would run MiniMax H3's 6-section `build_ref2va_prompt` / `<Picture N>` / `<Subject K>` compiler on LTX-2.5!
  2. `fal_catalog.py` L651/L667 would misroute LTX-2.5 ref profiles to MiniMax Ref2VA endpoints!
  3. `media_utils.py` L415/L496 would misclassify LTX-2.5 ref profiles as Ref2VA in ETA calculations!

  **Fix:** Introduce an explicit capability field **`ref_mode`** on `video_renderers` profiles (`"h3_ref2va"`, `"ltx2_msr"`, `"ltx2_ingredients"`, or `""`) and tighten `is_ref2va_spec()` so it returns `False` for non-H3 models (when `ref_mode` starts with `"ltx2"` or `"ltx2" in model_type`).

---

## 3. Step-by-Step Implementation Plan

### Step 1: Add Reserved Profiles to `model_profiles.json`
Add three new local entries to `video_renderers` in `model_profiles.json` (keeping `#14`, `#15`, `#16` untouched so existing projects keep identical hashes):

```json
  "19": {
   "name": "LTX-2 2.5 MSR Ref Distilled 22B (Multi-Subject Ref + Audio)",
   "model_type": "ltx2_25_22B_msr",
   "suitability_notes": "Rank #19 (Reuses LTX-2.5 Distilled Base): Multiple Subject Reference LoRA. Uses Start Anchor as Background + up to 4 subject refs (KI mode, 2-5 images) or 1-4 subjects only (I mode), plus audio soundtrack (AS). Single-window only (no sliding window, no Pass 2 inpainting, no Start Image S-flag).",
   "frames_formula": "ltx2",
   "prompt_style": "ltx2",
   "ref_mode": "ltx2_msr",
   "local_only": true,
   "supports_image_refs": true,
   "supports_start_anchor": false,
   "supports_end_anchor": true,
   "supports_pass2_inpaint": false,
   "max_image_refs": 5,
   "num_inference_steps": 8,
   "guidance_phases": 1,
   "sample_solver": "euler",
   "image_prompt_type": "T",
   "video_prompt_type": "KI",
   "audio_prompt_type": "AS",
   "remove_background_images_ref": 1,
   "sliding_window_size": 241,
   "sliding_window_overlap": 0,
   "skip_steps_cache_type": "",
   "custom_settings": {},
   "negative_prompt": "split-screen, split frame, vertical divider, dividing line, vertical seam, border, two panels, split image"
  },
  "20": {
   "name": "LTX-2 2.5 Distilled 22B + Ingredients Ref Sheet (8-Step)",
   "model_type": "ltx2_25_22B_distilled",
   "suitability_notes": "Rank #20 (Reuses LTX-2.5 Distilled Base + Ingredients IC-LoRA): Auto-composes scene/character reference images into a clean white-background grid sheet (video_prompt_type=I) while keeping Start/End anchors, audio lip-sync (AS), and sliding windows. Automatically disables Ingredients on Two-Pass Pass 2 (MVGA).",
   "frames_formula": "ltx2",
   "prompt_style": "ltx2",
   "ref_mode": "ltx2_ingredients",
   "local_only": true,
   "supports_image_refs": true,
   "supports_end_anchor": true,
   "max_image_refs": 6,
   "num_inference_steps": 8,
   "guidance_phases": 1,
   "sample_solver": "euler",
   "audio_prompt_type": "AS",
   "pass2_video_prompt_type": "MVGA",
   "denoising_strength": 1.0,
   "masking_strength": 0.0,
   "sliding_window_size": 241,
   "sliding_window_overlap": 9,
   "skip_steps_cache_type": "",
   "custom_settings": {},
   "negative_prompt": "split-screen, split frame, vertical divider, dividing line, vertical seam, border, two panels, split image"
  },
  "21": {
   "name": "LTX-2 2.5 Dev 22B + Ingredients Ref Sheet (2-Phase Quality)",
   "model_type": "ltx2_25_22B",
   "suitability_notes": "Rank #21 (Reuses LTX-2.5 Dev Base + Ingredients IC-LoRA): Full 2-phase LTX-2.5 Dev with Ingredients reference sheet (video_prompt_type=I), Start/End anchors, and audio soundtrack (AS). Higher detail & adherence than #20.",
   "frames_formula": "ltx2",
   "prompt_style": "ltx2",
   "ref_mode": "ltx2_ingredients",
   "local_only": true,
   "supports_image_refs": true,
   "supports_end_anchor": true,
   "max_image_refs": 6,
   "num_inference_steps": 8,
   "guidance_phases": 2,
   "sample_solver": "distilled_8_steps_ancestral",
   "audio_prompt_type": "AS",
   "pass2_video_prompt_type": "MVGA",
   "denoising_strength": 1.0,
   "masking_strength": 0.0,
   "sliding_window_size": 241,
   "sliding_window_overlap": 9,
   "skip_steps_cache_type": "",
   "custom_settings": {},
   "negative_prompt": "split-screen, split frame, vertical divider, dividing line, vertical seam, border, two panels, split image"
  }
```

### Step 2: Create Dedicated Module `work/pipeline/ltx_refs.py` (< 280 lines)
Create `work/pipeline/ltx_refs.py` to encapsulate all LTX-2.5 reference-image logic cleanly without bloating `context.py`:

1. **Mode detection helpers:**
   - `get_video_ref_mode(rspec: dict | None) -> str`: returns explicit `rspec.get("ref_mode")` or infers `"ltx2_msr"` if `"msr"` in `model_type` and `"ltx2"` in `model_type`; else `"h3_ref2va"` if `is_ref2va_spec(rspec)` else `""`.
   - `is_ltx2_msr(rspec)` / `is_ltx2_ingredients(rspec)`.
2. **Entity/reference collection for LTX-2.5 (`collect_ltx2_scene_refs`):**
   - Given `(ctx, sc, raw_prompt, fallback_refs, max_refs)`:
     - Collects distinct existing image paths in order:
       1. Explicit `sc["image_refs"]` (if list & non-empty).
       2. `@character` / `{LEGACY_VAR}` entities referenced in `raw_prompt` via `find_prompt_entities(raw_prompt, ctx.tag_lookup)` (taking each entity look's `image_refs`).
       3. `fallback_refs` (from `ctx.resolve_scene_image_refs`) if `<imageN>` tags appear or as fallback when no `@character` refs matched.
     - Deduplicates by resolved lowercase path and caps at `max_refs`.
   - Also expands `@character` / `{VAR}` tokens to **plain natural prose** (reusing `compile_character_prompt(..., supports_refs=False)` + `adapt_video_prompt(..., "ltx2")` + stripping `<imageN>`/`<Picture N>`), NEVER H3 6-section format!
3. **MSR slot assembly (`prepare_ltx2_msr_task`):**
   - Rejects Pass 2 (`if video_guide and video_mask: raise ValueError("LTX-2.5 MSR does not support Two-Pass masked inpainting (Pass 2). Use LTX-2.5 Standard/Ingredients or MiniMax H3 for Pass 2.")`).
   - Collects up to 4 subject refs (`subject_refs[:4]`).
   - Checks `image_start` (the scene's Start Anchor) and optional scene flags `sc.msr_use_start_as_bg` (default `True`) and `sc.msr_remove_bg` (default `True`):
     - `remove_bg = 0 if (sc and sc.get("msr_remove_bg") is False) else int(rspec.get("remove_background_images_ref", 1))`
     - `use_start_as_bg = not (sc and sc.get("msr_use_start_as_bg") is False)`
     - If `use_start_as_bg` and `image_start` exists and `subject_refs` is non-empty (and `image_start` is not already in `subject_refs`):
       - **Background + Subjects (`KI`) mode:** `msr_refs = [image_start] + subject_refs[:4]` (2–5 images), `video_prompt_type = "KI"`, `remove_background_images_ref = remove_bg`.
     - Else if `subject_refs` has 1–4 images (or only `image_start` exists):
       - **Subjects Only (`I`) mode:** `msr_refs = subject_refs[:4] or ([image_start] if image_start and Path(image_start).exists() else [])`, `video_prompt_type = "I"`, `remove_background_images_ref = remove_bg`.
     - If `msr_refs` is empty: raise a clear `ValueError("LTX-2.5 MSR requires at least 1 reference image (or a Start Anchor + 1 subject ref).")`.
   - Forces `image_prompt_type = "TE" if has_end else "T"` and **removes `image_start` from the task dict** (`task.pop("image_start", None)`).
   - Sets `audio_prompt_type = "AS"` when an active (non-silent) `audio_guide` is present, else `""` (clearing `audio_guide = ""`).
   - Sets `task["image_refs"] = [str(p) for p in msr_refs]` and `task["video_prompt_type"] = video_prompt_type`.
4. **Ingredients Reference Sheet builder (`compose_ingredients_sheet` & `prepare_ltx2_ingredients_task`):**
   - If `video_guide and video_mask` (Pass 2 `MVGA`): **do NOT touch `video_prompt_type` or `image_refs`** (leave Pass 2 cleanly on `MVGA`).
   - Collects up to 6 distinct refs via `collect_ltx2_scene_refs`.
   - If `len(refs) == 0`: keep `video_prompt_type = ""` (runs as normal LTX-2.5 I2V/A2V).
   - If `len(refs) == 1`: use `refs[0]` directly as `task["image_refs"] = [str(refs[0])]`, `task["video_prompt_type"] = "I"`.
   - If `len(refs) >= 2`: compose a deterministic **white-background (`#FFFFFF`) collage grid with 4px solid black (`#000000`) separator lines and zero text** (e.g., 2x1 for 2 images, 3x1 or 2x2 for 3–4, 3x2 for 5–6; target canvas `1536x1024` or `1280x768`, each cell aspect-ratio-preserved via `ImageOps.contain` / LANCZOS centered on white padding).
     - Use Pillow (`PIL.Image`, already available in Studio/Wan2GP env) with a clean FFmpeg fallback if Pillow is absent in a minimal test environment.
     - Cache output JPEG at `ctx.jobs_dir / "ingredients_cache" / f"sheet_{sha16}.jpg"` keyed by sorted-order `(path, file_sha256)` so identical ref sets reuse the cached sheet instantly!
     - Set `task["image_refs"] = [str(sheet_path)]`, `task["video_prompt_type"] = "I"`.
5. **Resolution 32px alignment helper (`snap_ltx2_resolution`):**
   - Ensures LTX-2.5 `resolution` dimensions are multiples of 32 (note: `832x480`, `960x544`, `1344x768`, `1536x864`, `1920x1088` are ALREADY multiples of 32! Only `1280x720` has `720 % 32 == 16` -> keep `1280x704` or let Wan2GP pad; document in helper and snap `1280x720` -> `1280x704` specifically for `ltx2_25_22B_msr` if desired, or verify Wan2GP pads automatically via `ltx2.py` L1461).

### Step 3: Fix `is_ref2va_spec()` & Wire `ltx_refs.py` into Pipeline
1. **`work/pipeline/fal_catalog.py` (L585–594 & L651):**
   - Update `is_ref2va_spec(rspec, spec_or_sc)`:
     - Immediately return `False` if `str(r.get("ref_mode") or "").lower().startswith("ltx2")` or `"ltx2" in mtype`.
     - Otherwise keep existing Ref2VA logic (`h3_ref2va`, `ref2va`, `fasth3`, `singularity`, or `supports_image_refs` for non-LTX2 H3/cloud specs).
   - Ensure `resolve_fal_route()` still maps `"ltx2" in mtype` to `fal_ltx25_a2v` (with a note that fal.ai LTX-2.5 does not support local MSR/Ingredients LoRAs, so `local_only` or clear note applies).
2. **`work/pipeline/context.py` (`make_video_task`, L681–789):**
   - Keep diff **tiny** (net 0 or negative lines in `context.py` to stay `< 850` lines!):
   - Import `apply_ltx2_ref_mode` from `.ltx_refs`.
   - When `is_ltx2_msr(rspec)` or `is_ltx2_ingredients(rspec)`, bypass the H3 `supports_refs` / `build_ref2va_prompt` branch (so `compile_character_prompt` runs in text-only mode and `adapt_video_prompt(..., "ltx2")` produces clean prose), and call `apply_ltx2_ref_mode(self, task, rspec=rspec, sc=sc, raw_prompt=prompt, image_start=image_start, image_refs=image_refs, has_end=has_end, video_guide=video_guide, video_mask=video_mask)` right before `attach_fal_task_meta`.
3. **`work/pipeline/stage2_hashes.py` (`compute_project_task_hashes`):**
   - Currently lines 227–237, 325–335, 388–398 only hash `refs` when `is_ref2va` is True!
   - Extend to also hash `refs` + `ref_mode` (+ `msr_use_start_as_bg`, `msr_remove_bg` for `ltx2_msr`) when `get_video_ref_mode(vrspec) in ("ltx2_msr", "ltx2_ingredients")`:
     - Call `resolve_ltx2_hash_refs(ctx, sc, vrspec, raw_prompt, anchor_path, pass_num)` from `ltx_refs.py` and set `base_payload["refs"] = [(p.name, _file_sha256(p)) for p in resolved]` and `base_payload["ref_mode"] = ref_mode` (and for `ltx2_msr`, `msr_use_start_as_bg` / `msr_remove_bg`).
   - Keep `#14`, `#15`, `#16` (`ref_mode == ""`) 100% untouched so existing scene hashes do not change!
4. **`work/ui/server_queue.py` (L341):**
   - Line 341 already checks `vrspec.get("supports_image_refs")` for reference-file mtime staleness; ensure it also checks `@character`-resolved refs (via `collect_ltx2_scene_refs` when `ref_mode.startswith("ltx2")`) so editing a character reference image marks LTX-2.5 MSR/Ingredients scenes stale in the queue.
5. **`work/pipeline/media_utils.py` (L415, L496, L616):**
   - Add `"19", "20", "21"` (or check `"ltx2" in mtype`) to the `formula == "ltx2"` fallback when `ctx is None`, and ensure `_is_ref2va_model()` does NOT flag LTX-2.5 models.
6. **`.agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py`:**
   - Add validation checks for `ref_mode == "ltx2_msr"` (`image_prompt_type` must not contain `S`/`V`/`L`, `video_prompt_type` must be `"KI"` or `"I"`, `audio_prompt_type` must not contain `K`/`2`).

### Step 4: Frontend UI Updates (Direct Scene Refs, MSR Toggles, Local-Only Lock & Badges)
Keep existing JS files strictly `< 850` lines by putting the new per-scene reference picker and MSR options in a dedicated small module **`work/ui/static/js/scene_ref_picker.js`** (< 200 lines), loaded in `work/ui/static/index.html` right after `characters_studio.js`:

1. **Direct Scene Reference Image Picker & MSR Options (`work/ui/static/js/scene_ref_picker.js`):**
   - Today, scenes only get `image_refs` indirectly via `@character` tags—users have no UI to attach non-character props, objects, or a pre-made Ingredients sheet to `sc.image_refs`.
   - Expose `window.renderSceneRefPickerUI(sc)` and hook it with a 1-line call from `fal_settings.js` `syncSceneBackendUI(sc)` (L412) and `characters_studio.js` `updatePromptReferenceTray(sc)` (L620). Mount a compact host `<div id="sceneDirectRefsHost"></div>` right above `#refTraySingle` in `work/ui/static/index.html` (L249) (and a 1-line hook `window.appendSceneCardRefPicker?.(ctrls, sc, idx, applyFieldSideEffects)` in `scenes_tab.js` L576).
   - Visible whenever the scene's effective video renderer has `supports_image_refs: true` (`ltx2_msr`, `ltx2_ingredients`, or `h3_ref2va`):
     - Displays existing `sc.image_refs` as thumbnail chips with `×` remove and left/right reorder buttons (disabled when `sc.locked`).
     - Provides **`+ Upload Ref`** (uploads via existing ref-image upload endpoint into `refs/` and appends to `sc.image_refs`) and **`Pick Existing`** dropdown (lists project anchors & `refs/` images already on disk).
     - Enforces the model's slot cap (`max_image_refs`: `4` subject slots for `ltx2_msr`, `6` for `ltx2_ingredients`, `9` for `h3_ref2va`) and calls `markProjectDirty()` + `recordHistorySnapshot()`.
   - When `ref_mode === "ltx2_msr"`, also renders two compact checkboxes in the same bar:
     - `Use Start Anchor as Background (KI)` (`sc.msr_use_start_as_bg !== false`; unchecking sets `sc.msr_use_start_as_bg = false` for Subjects-Only `"I"` mode).
     - `Remove Subject Backgrounds` (`sc.msr_remove_bg !== false`; maps to `remove_background_images_ref: 1` vs `0`).
   - Also adds/toggles a subtle hint `(BG Slot 1 — MSR)` on the Start Anchor label (`label[for="sceneAnchor"]`) when `#19` is active so users understand the Start Anchor becomes the background reference rather than a locked Frame 0.
2. **`local_only` Backend Dropdown Lock (`work/ui/static/js/fal_settings.js` L375–381 & `work/ui/static/js/scenes_tab.js` L539–545):**
   - Currently `syncSceneBackendUI()` and `scenes_tab.js` only lock `#sceneBackend` / `backSel` for `fal_video_renderers` and `minimax_video_renderers`.
   - Also check `const isLocalOnly = Boolean(window.state?.models?.video_renderers?.[vrId]?.local_only);` and when true, set `sel.value = "local"`, `sel.disabled = true`, `sel.dataset.forceDisabled = "1"`, and display the `🖥️ Local` badge so users cannot leave `#19`/`#20`/`#21` pointed at `fal.ai`.
3. **Dropdown Badges (`work/ui/static/js/scene_inspector.js` L62 & `work/ui/static/js/scenes_tab.js` L530):**
   - Instead of labeling every `supports_image_refs` model `[Ref2VA]`, show:
     - `[LTX-MSR]` when `m?.ref_mode === "ltx2_msr"`
     - `[Ingredients]` when `m?.ref_mode === "ltx2_ingredients"`
     - `[Ref2VA]` otherwise when `supports_image_refs` or `ref2va`.
4. **Attached References Chip Tray (`work/ui/static/js/characters_studio.js` L604–720):**
   - Update `renderTrayContainer` badge & slot cap dynamically from the selected model spec:
     - `ltx2_msr`: `"✓ LTX-2.5 MSR Mode (BG + X/4 Subject Slots)"` (warns if >4 subject images).
     - `ltx2_ingredients`: `"✓ LTX-2.5 Ingredients Mode (Auto-composed into 1 Reference Sheet, X/6 images)"`.
     - `h3_ref2va`: existing `"✓ Multimodal Ref2VA Mode (X/9 Image Slots)"`.
5. **Combo Warnings (`work/ui/static/js/duet_repair.js` `evaluateComboWarnings`):**
   - Add two targeted warnings:
     - `ltx2_msr_pass2`: if `ref_mode === "ltx2_msr"` is chosen for Two-Pass Duet Pass 2 (explains MSR forbids `video_guide`/`video_mask`; recommends `#14`/`#16`/`#20` or MiniMax H3 for Pass 2).
     - `ltx2_msr_long_scene`: if `ref_mode === "ltx2_msr"` and scene effective duration > 10.0s (explains MSR has no sliding window; recommends splitting <= 10s or using `#20` Ingredients).
6. **Render Time Estimator (`work/ui/static/js/state_and_utils.js` L566–568):**
   - Ensure `isLtx2` matches `String(rModel).toLowerCase().includes("ltx")` (already true for `ltx2_25_22B_msr`!) and `isRef2va` does NOT accidentally match LTX-2.5.

---

## 4. Verification & Test Plan

Add unit tests in `tests/test_ltx25_refs.py` (all offline, zero GPU):
1. **Profile schema validation:** `model_profiles.json` loads cleanly; `validate_wgp_profile.py` exits 0 with 0 errors.
2. **MSR task contract (`#19`):**
   - Start Anchor + 2 `@character` refs -> `video_prompt_type == "KI"`, `image_prompt_type == "T"`, `"image_start" not in task`, `image_refs == [anchor, ref1, ref2]`, `remove_background_images_ref == 1`, `audio_prompt_type == "AS"`, prompt is plain LTX prose (NO `subject_definitions:` or `<Picture 2>`).
   - Start Anchor + End Anchor + 1 ref -> `image_prompt_type == "TE"`, `image_end` set.
   - Subjects only (no existing start anchor) with 1–4 refs -> `video_prompt_type == "I"`.
   - Silent scene -> `audio_prompt_type == ""`, `audio_guide == ""`.
   - Pass 2 (`video_guide` + `video_mask`) -> raises `ValueError`.
3. **Ingredients task contract (`#20`, `#21`):**
   - 1 ref -> passes single image directly with `video_prompt_type == "I"`, keeps `image_start` (`image_prompt_type == "S"`), keeps `audio_prompt_type == "AS"`.
   - 3 refs -> composes a valid JPEG grid sheet under `jobs_dir/ingredients_cache/` (verify file exists, RGB dimensions, white border / black divider pixels, deterministic cache hit on second call), passes `[sheet_path]` (length 1!) with `video_prompt_type == "I"`.
   - 0 refs -> `video_prompt_type == ""`, no `image_refs`.
   - Pass 2 (`video_guide` + `video_mask`) -> `video_prompt_type == "MVGA"`, Ingredients disabled cleanly.
4. **Hash stability & invalidation:**
   - Existing `#14` / `#16` task hashes are byte-for-byte unchanged.
   - Modifying any reference image file changes the `#19` / `#20` / `#21` task hash in `compute_project_task_hashes()`.
5. **Repo health & Playwright E2E:**
   - Inside sandbox:
     ```bash
     python .agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py
     python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits
     python .agents/skills/mvg-dev-and-testing/scripts/verify_repo_health.py
     python -m unittest tests/test_ltx25_refs.py tests/test_regression_fixes.py tests/test_backend_queue.py tests/test_fal_integration.py -v
     ```
   - Outside sandbox (`BypassSandbox: true`, per `AGENTS.md` Rule 4):
     ```bash
     python -m unittest tests/test_ui_e2e_playwright.py -v
     ```
   - Manual GPU smoke test *(only after user confirms GPU is idle)*: render 1 short 5s scene on `#19` (`ltx2_25_22B_msr`) and 1 on `#20` (`ltx2_25_22B_distilled` Ingredients) and inspect via `inspect_clip.py`.
