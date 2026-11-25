# Plan 2: MiniMax H3-Image & P-H3-Image Multi-Reference Anchor Renderers

> **Target implementer:** Hydrogen coding agent  
> **Scope:** Wire MiniMax's H3 still-image family (`minimax_p_h3_image`, `minimax_h3_image_pruned`, and `minimax_h3_image`) into Studio's Stage 1 Anchor Generator, unlocking up to **8–16 source/reference images per anchor**, native `<Picture N>` tag translation, and 32px-aligned high-res (4 MP / 8 MP / 16 MP) anchor rendering.  
> **Zero-base-download highlight:** `minimax_p_h3_image` reuses the already-downloaded `MiniMax-H3-FL2VA-pruned` 21 GB checkpoint and only downloads `p-h3-image_lora_bf16.safetensors` (+ tiny VAE/tokenizer files).  
> **Non-goals:** Do not alter existing anchor renderers `#1`–`#16` or their hashes; do not run live GPU tests without explicit user confirmation; do not touch active user project files under `projects/`.

---

## 1. Verified Wan2GP Ground-Truth Contracts

Verified directly from local Wan2GP (`C:\Users\bugbl\Downloads\pinokio\api\wan.git\app`):
- `defaults/minimax_p_h3_image.json`
- `defaults/minimax_h3_image_pruned.json`
- `defaults/minimax_h3_image.json`
- `models/minimax_h3/h3_image.py`
- `models/minimax_h3/p_h3_image.py`
- `models/minimax_h3/minimax_h3_handler.py` (L1535–1540, L1701–1709)
- `models/minimax_h3/pipeline.py` (L574–580, L681–688, L892–894, L1015–1016)

### A. `minimax_p_h3_image` (P-H3-Image 20B — Fastest & Smallest Download)
- **Weights:** `"URLs": "minimax_h3_fl2va_pruned"` (already on disk!) + `loras/p-h3-image_lora_bf16.safetensors` + small VAE/tokenizer assets.
- **Max reference images:** **8** (`image_refs[0]` is edited, `image_refs[1..7]` are additional references).
- **Prompt reference tag syntax:** **`<Picture 1>`, `<Picture 2>`, ..., `<Picture 8>`**.
  - Note (`pipeline.py` L600): when `image_refs` is non-empty and the prompt does not start with `"Edit <Picture 1>."` or `"subject_definitions:"`, Wan2GP automatically prepends `"Edit <Picture 1>. "`.
- **Sampling defaults:** `image_mode: 1`, `num_inference_steps: 8`, `guidance_phases: 1`, `guidance_scale: 1.0`, `sample_solver: "euler"`, `flow_shift: 12.0`, `no_negative_prompt: True`, `no_background_removal: True`.
- **Resolutions:** `block_size: 32`, `vae_block_size: 32`. Official 16:9 presets: **`2720x1536` (4 MP)** and **`3872x2176` (8 MP)**. Any custom resolution MUST have both width and height divisible by 32 (e.g., `1920x1088` works; `1920x1080` is NOT divisible by 32 and must be snapped!).
- **`video_prompt_type`:**
  - `""` -> pure text-to-image (no `image_refs`).
  - **`"I"`** -> edit/compose at the **exact selected output `resolution`** (REQUIRED default for Studio so 16:9 widescreen anchors are never squashed to a portrait headshot's aspect ratio!).
  - `"KI"` -> match first image's aspect ratio (avoid as Studio default).

### B. `minimax_h3_image_pruned` (20B) & `minimax_h3_image` (33B Full)
- **Weights:** Separate `MiniMax-H3-Image-pruned_rank8_int8_convrot.safetensors` (or unpruned 33B) + `minimax_h3_image_vae_fp16.safetensors` (reuses `Qwen3-VL` tokenizer/encoder).
- **Max reference images:** **16** (`H3_IMAGE_MAX_IMAGES = 16`, `h3_image.py` L37).
- **Prompt reference tag syntax:** **`<Picture 1>`, `<Picture 2>`, ..., `<Picture 16>`** (`h3_image.py` L40, L96, L150).
- **Hard validation rules (`pipeline.py` L892–894):**
  ```python
  if self.h3_image and (int(guide_phases) > 1 or any(sigmas is not None for sigmas in custom_sigmas or ())
                        or starting_sigma is not None or sample_solver != "euler" or self.transformer.cache is not None):
      raise ValueError("H3-Image samples one phase of 6 Euler steps on its own schedule, without custom sigmas or step skipping")
  ```
  - **MUST** send `guidance_phases: 1`, `sample_solver: "euler"`, `num_inference_steps: 6`, `guidance_scale: 1.0`, **NO `flow_shift`**, **NO `negative_prompt`**, **NO `skip_steps_cache_type`**.
- **Resolutions:** `block_size: 32`, `vae_block_size: 32`. Official 16:9 presets: **`2720x1536` (4 MP)** and **`5472x3072` (16 MP)**. Requests below 4 MP (`H3_IMAGE_MIN_AREA = 2048*2048`) are internally generated at 4 MP and downscaled back to target dimensions, so **`2720x1536`** gives full native 4 MP detail at the same render cost as `1920x1088`!
- **`video_prompt_type`:** `""` (text-to-image) or **`"I"`** (multi-ref at exact selected `resolution`).

---

## 2. Critical Codebase Guardrails & Existing Bottlenecks

- **File-size cap (`< 850` lines per file under `work/`):**
  - `work/pipeline/context.py` is **848 lines** — DO NOT add bulk code directly into `make_anchor_task()`. Either extract anchor task sanitization into a helper in `work/pipeline/stage1_anchors.py` (currently 799 lines, 50 lines spare) or a small helper module `work/pipeline/anchor_task_utils.py`.
  - `work/ui/static/js/anchors_studio.js` is **809 lines** — keep edits concise (~20 lines max).
  - `work/ui/static/js/anchor_source_refs.js` is **602 lines** — plenty of headroom for dynamic `max_image_refs`.
- **Three existing hardcoded limits that MUST be unlocked cleanly:**
  1. `work/pipeline/prompts.py` L88–104 (`adapt_ref_tags`): only iterates `range(1, 10)` (caps at 9, breaks `<image10>`..`<image16>`) and has no `"<Picture N>"` branch!
  2. `work/pipeline/stage1_anchors.py` L56 (`compile_anchor_prompt_and_refs`): hardcodes `if len(resolved_refs) >= 4: return`!
  3. `work/ui/static/js/anchor_source_refs.js` L12: hardcodes `const MAX_ANCHOR_REFS = 4;`!

---

## 3. Step-by-Step Implementation Plan

### Step 1: Add Reserved Anchor Profiles `#17`, `#18`, `#19` to `model_profiles.json`
Append to `anchor_renderers` in `model_profiles.json`:

```json
  "17": {
   "name": "MiniMax P-H3-Image 20B (8-Step, Up to 8 Refs — Reuses FL2VA Base)",
   "model_type": "minimax_p_h3_image",
   "suitability_notes": "Rank #17 (Zero 20GB Base Download): Reuses your already-installed MiniMax H3 FL2VA-Pruned 20B weights + P-H3-Image LoRA. Supports up to 8 ordered <Picture N> reference images in 8 Euler steps (CFG=1.0, no negative prompt). Best at 4 MP (2720x1536) or 8 MP (3872x2176); 32px-aligned. Note: <Picture 1> is treated as the primary image to edit when refs are attached.",
   "ref_tag_style": "<Picture N>",
   "max_image_refs": 8,
   "resolution": "2720x1536",
   "resolution_align": 32,
   "video_prompt_type": "I",
   "num_inference_steps": 8,
   "guidance_phases": 1,
   "guidance_scale": 1.0,
   "flow_shift": 12.0,
   "sample_solver": "euler",
   "local_only": true,
   "no_negative_prompt": true,
   "remove_background_images_ref": 0
  },
  "18": {
   "name": "MiniMax H3-Image Pruned 20B (6-Step, Up to 16 Refs)",
   "model_type": "minimax_h3_image_pruned",
   "suitability_notes": "Rank #18: Dedicated MiniMax H3-Image Pruned 20B model supporting up to 16 ordered <Picture N> reference images in 6 Euler steps (CFG=1.0, no negative prompt). Reuses Qwen3-VL encoder. Generates internally at >=4 MP (2720x1536 native 16:9 sweet spot; 5472x3072 for 16 MP). 32px-aligned.",
   "ref_tag_style": "<Picture N>",
   "max_image_refs": 16,
   "resolution": "2720x1536",
   "resolution_align": 32,
   "video_prompt_type": "I",
   "num_inference_steps": 6,
   "guidance_phases": 1,
   "guidance_scale": 1.0,
   "sample_solver": "euler",
   "local_only": true,
   "no_negative_prompt": true,
   "remove_background_images_ref": 0
  },
  "19": {
   "name": "MiniMax H3-Image 33B Full (6-Step, Up to 16 Refs)",
   "model_type": "minimax_h3_image",
   "suitability_notes": "Rank #19: Full 33B unpruned MiniMax H3-Image supporting up to 16 ordered <Picture N> reference images in 6 Euler steps (CFG=1.0, no negative prompt). Highest fidelity in the H3-Image family; heavier RAM/VRAM offloading than #18.",
   "ref_tag_style": "<Picture N>",
   "max_image_refs": 16,
   "resolution": "2720x1536",
   "resolution_align": 32,
   "video_prompt_type": "I",
   "num_inference_steps": 6,
   "guidance_phases": 1,
   "guidance_scale": 1.0,
   "sample_solver": "euler",
   "local_only": true,
   "no_negative_prompt": true,
   "remove_background_images_ref": 0
  }
```

### Step 2: Upgrade Reference Tag Translation (`work/pipeline/prompts.py`)
In `adapt_ref_tags(prompt: str, ref_tag_style: str) -> str` (L88–104):
1. Replace the `range(1, 10)` loop with a regex substitution so **any** `<imageN>` (`1..16+`) translates cleanly without accidental substring collisions (`<image1>` vs `<image10>`):
   ```python
   def adapt_ref_tags(prompt: str, ref_tag_style: str) -> str:
       style = (ref_tag_style or "<imageN>").strip()
       if style == "<imageN>" or not prompt:
           return prompt
       def _repl(m: re.Match) -> str:
           idx = int(m.group(1))
           if style == "<ref_image_N0>":
               return f"<ref_image_{idx - 1}>"
           if style == "<Picture N>":
               return f"<Picture {idx}>"
           if style == "Image N":
               return f"Image {idx}"
           if style == "image N":
               return f"image {idx}"
           return m.group(0)
       return re.sub(r"<image(\d+)>", _repl, prompt, flags=re.IGNORECASE)
   ```
2. Verify existing styles (`<imageN>`, `<ref_image_N0>`, `Image N`, `image N`) produce identical outputs to before.

### Step 3: Per-Model `max_image_refs` & Anchor Task Sanitization
1. **`work/pipeline/stage1_anchors.py` (`compile_anchor_prompt_and_refs`, L55–57):**
   - Replace hardcoded `4` with `max_refs = max(1, int(rspec.get("max_image_refs") or 4))`.
   - Keep default `4` when `max_image_refs` is omitted so all existing `#1`–`#16` anchor renderers behave identically!
   - Also in `run_stage1_anchors` (L161–186):
     - If `rspec.get("no_negative_prompt")`, ignore `neg_val` (don't include unused `neg_prompt` in `hash_payload` or task).
     - Resolution precedence: when `sp.get("resolution")` is empty and `rspec` specifies a native `resolution` (like `2720x1536`) with `resolution_align`, verify whether `shared_cfg.resolution` (`1920x1080`) should still take precedence — keep existing precedence `sp.resolution -> shared_cfg.resolution -> rspec.resolution -> 1920x1080`, **and apply alignment snapping** via a shared helper `normalize_anchor_resolution(res_val, rspec)` so both the task hash (`run_stage1_anchors`) and `make_anchor_task` see the exact snapped `WxH`!
2. **Resolution alignment & H3-Image task cleanup (`work/pipeline/context.py` + helper in `stage1_anchors.py`):**
   - Add `normalize_anchor_resolution(res_val: str, rspec: dict) -> str` in `stage1_anchors.py`:
     - Reads `align = int(rspec.get("resolution_align") or (16 if "flux3" in model_type else 0))` (or `32` if `model_type in ("minimax_h3_image", "minimax_h3_image_pruned", "minimax_p_h3_image")`).
     - If `align > 1` and `(rw % align != 0 or rh % align != 0)`: rounds each dimension to nearest multiple of `align` (`max(align * 2, ((d + align // 2) // align) * align)`). Example: `1920x1080` -> `1920x1088` (divisible by 32: `1920 = 60*32`, `1088 = 34*32`).
   - In `make_anchor_task()` (`context.py` L798–841):
     - Delegate resolution snapping to `normalize_anchor_resolution(res_val, rspec)` (saves ~5 lines in `context.py`!).
     - Include `"guidance_phases"` in the optional-key copy loop (L827).
     - If `rspec.get("no_negative_prompt")`: pop `"negative_prompt"` (or set `""`) and do not attach `sp["negative_prompt"]`.
     - If `model_type in ("minimax_h3_image", "minimax_h3_image_pruned")`: ensure `cfg["guidance_phases"] = 1`, `cfg["sample_solver"] = "euler"`, `cfg.pop("flow_shift", None)`, `cfg.pop("skip_steps_cache_type", None)` so Wan2GP's strict `pipeline.py` L892 guard never raises `ValueError`.
     - Keep `video_prompt_type = "I"` when `has_refs` else `""`.
3. **Cloud routing fallback guard (`work/pipeline/fal_catalog.py` L620–636):**
   - `minimax_h3_image*` and `minimax_p_h3_image` are **local-only** Wan2GP models (not on fal.ai). Set `"local_only": True` on `#17`, `#18`, `#19` in `model_profiles.json` — `resolve_task_backend()` (L553) ALREADY honors `rspec.get("local_only")` and cleanly forces `"local"`!
4. **Profile validator (`.agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py`):**
   - Add check: any `anchor_renderers` entry with `resolution_align` (e.g. `32`) must have its default `resolution` (if present) divisible by `resolution_align`.

### Step 4: Frontend UI Updates (Dynamic Ref Cap, 4 MP/8 MP Presets & Local-Only Backend Lock)
1. **`work/ui/static/js/anchor_source_refs.js`:**
   - Replace static `MAX_ANCHOR_REFS = 4` usage with a helper `getMaxAnchorRefs(anchorObj)`:
     ```js
     function getAnchorRendererSpec(anchorObj) {
       const rid = String(anchorObj?.renderer ?? window.state?.projectData?.models?.default_anchor_renderer ?? 3);
       return window.state?.models?.anchor_renderers?.[rid] || window.state?.models?.fal_anchor_renderers?.[rid] || {};
     }
     function getMaxAnchorRefs(anchorObj) {
       return Math.max(1, Number(getAnchorRendererSpec(anchorObj)?.max_image_refs) || 4);
     }
     ```
   - Use `getMaxAnchorRefs(anchorObj)` in `resolveEffectiveAnchorSources`, `describeAnchorSourceRefs`, `uploadAndAttachSourceFiles`, `renderAnchorSourceRefsPanel` (`statusLabel`, `pickSel.onchange`, `libEl` attach button, and empty-hint text showing `up to ${maxRefs} images`).
   - Show the active renderer's native tag preview on hover (e.g., tooltip `Insert <imageN> (sent as <Picture N> for H3-Image)` while still inserting canonical `<imageN>` into the prompt so switching anchor renderers remains 100% portable!).
   - When the resolved anchor renderer is `minimax_p_h3_image` (`#17`) and at least 1 source image is attached, display a concise inline note in the source-refs panel: `Note: <image1> (<Picture 1>) is treated as the primary image to edit`.
2. **`work/ui/static/js/anchors_studio.js`:**
   - Add `["2720x1536", "2720×1536 (4 MP H3-Image 16:9)"]` and `["3872x2176", "3872×2176 (8 MP P-H3-Image 16:9)"]` to `ANCHOR_RES_OPTIONS` (L14–18).
   - In `modelSel.addEventListener("change", ...)` (L478–486):
     - Also handle `local_only` anchor models (lock `backendSel.value = "local"; backendSel.disabled = true;` just like `isFal` locks `"fal"`).
     - Re-render the anchor's source-refs panel (`paintSrcRefs()` + `refSummaryEl.textContent = describeAnchorRefs(a)`) immediately when the user switches an anchor's Model dropdown (so the cap updates live from `4` -> `8` or `16`!).
     - Sync `negInp` muted hint/placeholder: when the resolved anchor renderer has `no_negative_prompt: true` (`#17`–`#19`), set `negInp.placeholder = "Negative prompt (ignored by MiniMax H3-Image)"` and dim opacity slightly (keeping value editable/preserved if user switches back to FLUX/Qwen).
   - Also sync initial `backendSel` lock and `negInp` hint at L419–421 when the resolved renderer has `local_only: true` / `no_negative_prompt: true`.
3. **`work/ui/static/js/anchors_and_settings.js` (L740–748):**
   - In the `settingsFieldIds` `change` listener, when `id === "cfgDefaultAnchorRenderer"`, call `window.renderAnchorStudio?.()` so anchors inheriting the project default immediately refresh their `X/8` or `X/16` source-image caps, `local_only` backend lock, and `no_negative_prompt` hint.
4. **`work/ui/static/index.html`:**
   - Add `<option value="2720x1536">2720×1536 (4 MP H3-Image 16:9)</option>` and `<option value="3872x2176">3872×2176 (8 MP P-H3-Image 16:9)</option>` to `#cfgDefaultAnchorResolution` in Project Settings so project defaults match Anchor Studio.

---

## 4. Verification & Test Plan

Add unit tests in `tests/test_h3_image_anchors.py` (all offline, zero GPU):
1. **Profile schema & validator:** `validate_wgp_profile.py` passes with `#17`, `#18`, `#19` registered.
2. **Tag adaptation (`prompts.adapt_ref_tags`):**
   - `<image1>`..`<image16>` with `"<Picture N>"` -> `<Picture 1>`..`<Picture 16>` (verify `<image10>` does NOT turn into `<Picture 1>0`!).
   - Existing styles `<imageN>`, `<ref_image_N0>`, `Image N`, `image N` remain 100% backward-compatible.
3. **Multi-ref cap (`stage1_anchors.compile_anchor_prompt_and_refs`):**
   - Renderer `#3` (`flux2_klein_9b_kv`) with 10 explicit `image_refs` caps cleanly at `4`.
   - Renderer `#17` (`minimax_p_h3_image`) with 10 explicit `image_refs` keeps `8`.
   - Renderer `#18` (`minimax_h3_image_pruned`) with 16 explicit `image_refs` keeps all `16`, and adapts prompt tags `<image1>`..`<image16>` to `<Picture 1>`..`<Picture 16>`.
4. **Task contract & resolution alignment (`context.make_anchor_task`):**
   - `1920x1080` on `#17` / `#18` / `#19` snaps to `1920x1088` (divisible by 32).
   - `2720x1536` stays `2720x1536`.
   - `#18` / `#19` (`h3_image*`) task has `image_mode == 1`, `num_inference_steps == 6`, `guidance_phases == 1`, `sample_solver == "euler"`, `video_prompt_type == "I"` (when refs present) or `""` (when no refs), NO `flow_shift`, NO `negative_prompt`, NO `skip_steps_cache_type`.
   - `#17` (`minimax_p_h3_image`) task has `num_inference_steps == 8`, `flow_shift == 12.0`, `sample_solver == "euler"`, NO `negative_prompt`, and `resolve_task_backend()` resolves to `"local"` even if project `default_backend == "fal"`.
5. **Repo health & Playwright E2E:**
   - Inside sandbox:
     ```bash
     python .agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py
     python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits
     python .agents/skills/mvg-dev-and-testing/scripts/verify_repo_health.py
     python -m unittest tests/test_h3_image_anchors.py tests/test_regression_fixes.py tests/test_unit_coverage.py -v
     ```
   - Outside sandbox (`BypassSandbox: true`, per `AGENTS.md` Rule 4):
     ```bash
     python -m unittest tests/test_ui_e2e_playwright.py -v
     ```
   - Manual GPU smoke test *(only after user confirms GPU is idle)*: generate 1 test anchor on `#17` (`minimax_p_h3_image`, zero base download) with 2+ source images at `2720x1536` and inspect output dimensions & likeness.
