# Plan 4: Character Reference Mask Visibility, Interactive Extraction & AI De-Occlusion

> **Target implementer:** Hydrogen coding agent  
> **Scope:** Add visual mask inspection, a non-destructive interactive Mask & Extract Studio modal, tiered AI character extraction (`YOLOv8m-seg + PyMatting`, `U²-Net` MSR preview, `SAM 3.1` `+`/`-`/box/text, `BiRefNet General` edge polish, and `Qwen Image 2.1` / `fal_qwen_edit_2511` AI de-occlusion), and safe pipeline handoff to LTX-2.5 MSR (`#19`), Ingredients (`#20`/`#21`), MiniMax H3 Ref2VA (`#9`–`#13`), and Stage 1 Anchors.  
> **Non-goals:** Do not modify active user project JSONs/assets under `projects/`; do not run live GPU tests (`RUN_LIVE_WAN2GP_TESTS=1`) without explicit user confirmation; keep every `.py`, `.js`, `.html`, `.css` file under `work/` strictly `< 850` lines.

---

## 1. Verified Empirical Findings & Wan2GP Ground-Truth Contracts

### A. Root Cause Caught on `not_there_yet` (`S02` + `#19` MSR)
- When `S02` renders with `#19` (`ltx2_25_22B_msr`, `video_prompt_type: "KI"`, `remove_background_images_ref: 1`), Wan2GP (`shared/utils/utils.py` L701–753, `preprocessing/rembg/native.py` L71–142) keeps slot `0` (the Start Anchor environment) intact, places each subject ref onto a pure white `[255, 255, 255]` `1280x704` canvas (`fit_into_canvas=1`), and runs **`u2net_bf16.safetensors`** (`320x320` salient object mask + `PyMatting`).
- On crowded stills (`metropolis-maria-garden.png`), `u2net` cannot separate touching bystanders from the lead character, fusing dark figures behind her shoulders/arms into wing-like shapes on a white canvas — which LTX-2.5 MSR renders as a cape.
- **Critical `RGBA` Black-Matte Hazard (`wgp.py` L5964–5971):**
  `clean_image_list(..., keep_alpha=model_def.get("image_refs_keep_alpha", False))` converts `RGBA` inputs to `RGB` when `keep_alpha=False` (`ltx2_25_22B_msr`, `ltx2_25_22B`, `minimax_h3_*`, `qwen_image_21_*`). Converting `(0, 0, 0, 0)` transparent PNGs directly with `.convert("RGB")` turns transparent regions **solid black `(0, 0, 0)`** instead of white!
  **Mandatory contract:** Any exported cutout saved as an active `image_refs` file MUST be an **RGB PNG composited onto crisp white `(255, 255, 255)`** (`white_cutout`), while storing optional alpha preview/mask sidecars separately in `references/<entity>/masks/` for UI visibility & `RGBA`-aware workflows.

### B. Optimal Combination of Extraction Engines (Tested Locally)
Verified directly against `C:\Users\bugbl\Downloads\pinokio\api\wan.git\app`:

1. **Instant Local Zero-Download Instance Cutout (CPU or GPU): `YOLOv8m-seg` + `PyMatting`**
   - Model on disk: `ckpts/buffalo_l/person_yolov8m-seg.safetensors` loaded via `postprocessing/face_refiner/face.py` `_load_yolo()` (`ultralytics 8.4.189` in Wan2GP venv).
   - Verified empirically on CPU (`metropolis-maria-garden.png`): `9.72s`, separated `11` individual person instances (`det[0]` Maria `conf=0.903`, `84,245 px`), eliminating the crowd cape cleanly.
   - Multi-person subtraction: computing `target_mask - 0.85 * max(other_person_masks)` cleanly carves overlapping bystanders away from the chosen character, optionally refined along the boundary with `pymatting.estimate_alpha_cf` / `estimate_foreground_ml` inside a tight trimap ring.
2. **Pre-Render MSR Safety Check (Zero-Download CPU/GPU): `U²-Net` (`u2net_bf16.safetensors`)**
   - Running Wan2GP's exact `fit_into_canvas` + `U2NET` cutout recipe inside the Characters tab shows the user **exactly what `#19` MSR will feed into diffusion** before spending GPU time on a full clip.
3. **Precision Interactive Cutout (`SAM 3.1` + optional `BiRefNet General` edge polish):**
   - `SAM 3.1` (`preprocessing/sam3/preprocessor.py`, `sam3_video_inference.py`): auto-downloads `sam3.1_multiplex_bf16.safetensors` on first GPU use (`load_sam3_mask_predictor(include_text_encoder=..., postprocess_batch_size=1)`). Works on arbitrary objects/props *and* overlapping people via `+`/`-` clicks, boxes, or text prompts (`"woman in gray dress"`).
   - **Hard SAM 3.1 Assertion Contract (`sam3_video_inference.py` L864, L887, L1379–1393):**
     - Pass 1 (seed object): `add_prompt` MUST supply **either** `text="..."` **or** `bounding_boxes=[[x, y, w, h]]` + `bounding_box_labels=[1]` (normalized `[0, 1]`, **NOT** `[x1, y1, x2, y2]`!) — with `points=None`.
     - Pass 2 (point refinement): `add_prompt` MUST pass `obj_id=<int>` from Pass 1 output (`out_obj_ids[0]`), `points=[[x, y], ...]` (normalized `[0, 1]`), `point_labels=[1 or 0, ...]`, and **MUST set `text=None, bounding_boxes=None`** (otherwise `sam3_video_inference.py` L1381 raises `AssertionError`!).
   - **`BiRefNet General` (`birefnet-general_bf16.safetensors`, MIT, `1024x1024`):** Auto-downloads via `preprocessing/rembg/assets.py` `ensure_assets(model_name="birefnet-general")` when first invoked. Ideal when run on the cropped bounding box / isolated instance for fine hair and collar lace matting (avoid running `BiRefNet` unboxed on dense crowds where it can grab adjacent touching people).
4. **Generative AI De-Occlusion (`Qwen Image 2.1 Turbo 7B` / `Base 7B` local on disk, or `fal_qwen_edit_2511` cloud):**
   - Solves the hard limitation verified visually on `yolo_det0_raw.png`: segmentation masks **cannot** remove a bystander's hand resting in front of Maria's waist or restore an occluded forearm.
   - Generates a clean-plate un-occluded standing/bust portrait onto a plain neutral white backdrop (or transparent `rgba: "Enabled"` PNG converted onto white for MSR) using the existing queue scheduler (`CHAR:<eid>:<lid>:isolate` — never colliding with active GPU jobs!), saving the output as a selectable Take in `look.generated_takes` while keeping the original still untouched.

---

## 2. File Size Guardrails & Clean Module Boundaries

Verified current line counts (`python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits`):
- `work/ui/server_routes.py`: **848 lines** (1 line spare) -> **DO NOT EDIT!** Line 218–224 already dispatches all `POST /api/.../characters/<action>` requests to `work/pipeline/characters.py` `handle_characters_api()`.
- `work/ui/static/css/studio.css`: **846 lines** (3 lines spare) -> **DO NOT EDIT!** Add **`work/ui/static/css/char_mask_editor.css`** and link it in `index.html`.
- `work/ui/static/js/characters_studio.js`: **835 lines** -> keep edits **<= 12 lines** (mount mask badge + click handler hooking `window.openCharMaskModal(...)`). Put all canvas UI, overlays, brush engine, and extraction controls into **`work/ui/static/js/char_mask_editor.js`** (`< 650 lines`).
- `work/ui/static/js/anchor_and_char_takes.js`: **724 lines** -> tiny (`<= 10 line`) enhancement so Character Takes tiles also show mask badges and open the Mask Studio instead of only opening a raw tab.
- `work/pipeline/context.py`: **847 lines** -> **DO NOT ADD BULK CODE!** Keep all mask-bypass logic inside `work/pipeline/ltx_refs.py` (449 lines).
- `work/pipeline/characters.py`: **798 lines** -> only add a `<= 6 line` dispatcher in `handle_characters_api` routing `mask-*` actions to new module **`work/pipeline/char_masking.py`**.
- **Runtime Python Separation (`server.py` vs Wan2GP venv):**
  Studio UI's HTTP server can run from standard-library System Python (`C:\Python315\python.exe`, without `PIL`, `cv2`, `numpy`, `torch`, or `ultralytics`). Therefore:
  - Keep HTTP handling, metadata CRUD, and project JSON locking inside **`work/pipeline/char_masking.py`** (`< 350 lines`, pure stdlib + subprocess calls to `server.resolve_engine_python()` / `wgp_root` Python).
  - Put all `PIL`, `numpy`, `cv2`, `torch`, `ultralytics`, `pymatting`, `u2net`, `birefnet`, and `sam3` ML extraction code in a standalone runnable script **`work/pipeline/char_mask_worker.py`** (`< 500 lines`) invoked via subprocess (`resolve_engine_python()`), returning structured JSON on stdout.

---

## 3. Data Model & Non-Destructive File Layout

Store all generated masks/cutouts cleanly inside the project folder so `/media/<slug>/ref/<rel_path>` serves them automatically (`work/ui/server_media.py` L215–229 already allows any subpath under `proj_dir` / `references/`):

1. **Disk layout under `projects/<slug>/references/<clean_entity>/`:**
   - Untouched uploaded/cropped source: `references/<entity>/<name>.jpg`
   - Mask metadata folder: `references/<entity>/masks/`
     - Binary/soft alpha mask PNG (`L` 8-bit): `<stem>_mask.png`
     - Transparent RGBA cutout PNG (for UI checkerboard & `rgba`-aware models): `<stem>_rgba.png`
     - **Active White-Composited RGB Cutout PNG (`<stem>_cutout.png`)** — safe for `#19` MSR, `#20` Ingredients, `#9-#13` H3, and Stage 1 Anchors!
     - Ephemeral MSR simulator cache: `<stem>_msr_preview.png`

2. **Per-look schema inside `project.json` (`characters[eid].looks[lid]`):**
   Keep full backward compatibility with `image_refs: list[str]` (pipelines continue reading `look.image_refs` directly) and add an optional dict **`ref_masks`**:
   ```json
   "ref_masks": {
     "references/maria/masks/metropolis-maria-garden_cutout.png": {
       "source_rel": "references/maria/metropolis-maria-garden.png",
       "mask_rel": "references/maria/masks/metropolis-maria-garden_mask.png",
       "rgba_rel": "references/maria/masks/metropolis-maria-garden_rgba.png",
       "cutout_rel": "references/maria/masks/metropolis-maria-garden_cutout.png",
       "method": "yolo_matting+brush",
       "premasked_white": true,
       "updated_at": 1795556000.0
     }
   }
   ```
   - When the user saves an extraction/mask for `raw_rel`:
     - Keep `raw_rel` archived in `look.generated_takes` (if not already present) OR tracked via `source_rel`, so the raw photo is **never lost** and **re-opening Mask Editor on `cutout_rel` automatically loads `source_rel` underneath the existing `mask_rel`!**
     - Replace `raw_rel` with `cutout_rel` at the same index in `look.image_refs` (and update matching entries in `ent.avatar` or explicit `sc.image_refs` if requested/optional, or keep reversible toggle `"Use Cutout" / "Use Raw"`).

---

## 4. Step-by-Step Implementation Plan

### Step 1: Create ML Extraction CLI Worker (`work/pipeline/char_mask_worker.py`, < 500 lines)
Executed by Studio via `resolve_engine_python()` with JSON stdin/args (`--action ...`) and structured JSON stdout (last line `{...}`). Must support CPU fallback (`yolov8m-seg`, `u2net`, `pymatting`, pure canvas composite) AND GPU acceleration for heavy models (`sam3`, `birefnet-general`, `bria-rmbg`):

1. **`detect_instances` (Zero-Download Fast Analysis):**
   - Loads `ckpts/buffalo_l/person_yolov8m-seg.safetensors` via Wan2GP's `_load_yolo()` on GPU if idle else CPU.
   - Runs `model.predict(..., conf=0.25, retina_masks=True)` on the source image.
   - Returns sorted person detections (largest/most central first): `[{idx, box_norm: [x0,y0,x1,y1], conf, area_px, mask_rle_or_png_b64}]` plus writes individual preview mask thumbnails or combined JSON map so the canvas UI highlights every detected person and lets the user click any person!
2. **`extract_mask` (Unified Cutout Runner):**
   Accepts `method` in:
   - `"yolo_person"` (zero download, CPU-safe): uses `det_idx` (default `0`, main central person), subtracts overlapping neighbor instances `det_k != det_idx` (`clean = clip(target - 0.85 * others, 0, 1)`), optionally runs tight boundary `pymatting` (`erode=7, dilate=9`, capped for speed) when `refine_edges=True`.
   - `"u2net"` / `"u2net_msr_preview"` (zero download, CPU-safe via `U2NET` in pure PyTorch on CPU/GPU): simulates Wan2GP's exact `fit_into_canvas` `[1280x704]` white-canvas `U²-Net + _matting` output (for `"Preview MSR Cutout"`), OR runs full-frame cutout at source aspect ratio.
   - `"birefnet"` / `"bria_rmbg"` (Wan2GP `NativeSession("birefnet-general" | "bria-rmbg")`, auto-downloads via `ensure_assets` on GPU): if `crop_box` (`[x0,y0,x1,y1]` normalized or user-selected YOLO box + 12% pad) is provided, crops first so `BiRefNet` focuses 100% of its `1024x1024` resolution on the single character and excludes bystanders outside the box, then pastes the resulting alpha matte back into full image coordinates!
   - `"sam3"` (Wan2GP `preprocessing/sam3/preprocessor.py`, auto-downloads on GPU):
     - Starts a 1-frame session `[Image.open(src).convert("RGB")]` via `load_sam3_mask_predictor(...)`.
     - If `text_prompt` or `crop_box` is provided, sends `add_prompt(frame_index=0, text=text_prompt or bounding_boxes=xywh, bounding_box_labels=[1])`. (Wait: if neither box nor text given, use `yolo det[0]` box or `"person"`).
     - If `points` / `point_labels` (`1` = keep, `0` = exclude) are also provided, takes `obj_id = int(out_obj_ids[0])` and issues a follow-up `add_prompt(frame_index=0, obj_id=obj_id, points=..., point_labels=..., text=None, bounding_boxes=None)` per Wan2GP's assertion rules, cleanly closing session & calling `_cleanup()` in `finally`.
     - Optionally runs fast `BiRefNet` edge feathering or hole-fill (`fill_sam3_binary_mask_holes`) if toggled.
   - Writes `<stem>_mask.png` (`L`), `<stem>_rgba.png` (`RGBA`), and `<stem>_cutout.png` (`RGB` over `(255, 255, 255)`).
3. **`composite_mask` (Manual Canvas Mask Save):**
   - Takes `source_path` + user-edited grayscale mask PNG (`L` from HTML5 canvas brush/eraser + optional `feather_px`), resizes mask cleanly to original image dimensions with `LANCZOS`, saves `<stem>_mask.png`, `<stem>_rgba.png`, and `<stem>_cutout.png` (white RGB composite).

> **GPU Contention Guard in `char_mask_worker.py` / `char_masking.py`:**  
> Before invoking GPU-only methods (`sam3`, `birefnet`, `bria_rmbg`), check `server_engines.active_rendering_map` (or `wgp_lock`). If a local GPU render is currently active, run `yolo_person` / `u2net` / `composite_mask` on **CPU** (`CUDA_VISIBLE_DEVICES=""`) immediately so interactive masking stays instant and non-blocking, and return a clear `409 Conflict` / toast if a GPU-only model (`SAM 3.1` / `BiRefNet`) is requested mid-render!

### Step 2: Create Studio API & Metadata Manager (`work/pipeline/char_masking.py`, < 380 lines) + Wire `characters.py`
1. Wire in `work/pipeline/characters.py` (`handle_characters_api`, +6 lines):
   - Dispatch actions `"mask-info"`, `"mask-detect"`, `"mask-extract"`, `"mask-save"`, `"mask-toggle"`, and `"isolate-ai"` to `handle_char_masking_api(handler, action, proj_dir, body)`.
2. Implement `handle_char_masking_api` cleanly in `work/pipeline/char_masking.py`:
   - Enforce `server_security.resolve_path_under_dir` on `source_rel` / `rel_path` (strictly inside `proj_dir`; never allow path traversal).
   - Auto-create timestamped backup via `_backup_and_save_project_json` before mutating `characters[eid].looks[lid]`.
   - `mask-info`: given `(slug, entity_id, look_id, rel_path)`, resolves `{source_rel, mask_rel, rgba_rel, cutout_rel, is_cutout, meta}` plus checks disk existence and Wan2GP available engines (e.g., whether `person_yolov8m-seg.safetensors`, `u2net_bf16.safetensors`, `birefnet-general_bf16.safetensors`, `sam3.1_multiplex_bf16.safetensors`, `qwen_image_21_turbo_7B` are already cached on disk vs `"auto-downloads on 1st GPU run"`).
   - `mask-detect` / `mask-extract`: invokes `char_mask_worker.py` with `srv.resolve_engine_python()`, saves mask/cutout files (for `"u2net_msr_preview"`, writes to cache without mutating active `image_refs`), optionally updates `look.ref_masks` + `look.image_refs` + `ent.avatar` when applying cutout.
   - `mask-save`: decodes canvas grayscale mask PNG data URL, invokes `char_mask_worker.py --action composite_mask`, records/updates `look.ref_masks[cutout_rel]`, swaps `cutout_rel` into `look.image_refs` at `raw_rel`'s slot (preserving `raw_rel` in `source_rel` & `generated_takes` if needed), and returns `{success: true, characters, cutout_rel, mask_rel, rgba_rel, source_rel}`.
   - `mask-toggle`: switches between `cutout_rel` and `source_rel` in `look.image_refs` in 1 click without deleting either file.
   - `isolate-ai`: generates a clean-plate de-occluded reference image via **`Qwen Image 2.1 Turbo 7B` (local, already on disk)** or **`[fal.ai] Qwen Image Edit 2511`**!

### Step 3: Wire Clean AI De-Occlusion Into Existing Take Queue (`work/pipeline/stage1_anchors.py` / `work/pipeline/anchor_takes.py`)
Keep `anchor_takes.py` (`832` lines) and `stage1_anchors.py` (`799` lines) `< 850` lines by delegating prompt/spec building to a helper in **`work/pipeline/char_masking.py`**:
- Support queue token `CHAR:<entity_id>:<look_id>:isolate:<idx>` (or store isolated prompt/ref config on `look._pending_isolate` / REST action `isolate-ai` preparing a `CHAR:<eid>:<lid>` take with `sp_like["image_refs"] = [selected_src_or_cutout]`, `renderer = look.gen_renderer or "1"` (`qwen_image_21_turbo_7B`) or `"fal_qwen_edit_2511"`, default prompt:
  `"Extract and isolate only the main subject (<physical_traits>, <look_description>) from <image1>, preserving exact face, hair, expression, and wardrobe details. Place the subject cleanly centered alone against a solid pure white studio background with even soft studio lighting. Remove all other people, background crowds, overlapping hands, and clutter; naturally reconstruct any hidden clothing edges or arms."`).
- When complete, `generate_char_look_takes` saves the generated JPG cleanly into `look.generated_takes` and makes it selectable/inspectable right inside the Look card & Mask Studio!

### Step 4: Pipeline Integration — LTX-2.5 MSR Auto-Protection & Hash Tracking (`work/pipeline/ltx_refs.py`, `work/pipeline/stage2_hashes.py`, `work/ui/static/js/scene_ref_picker.js`)
1. **Helper `is_ref_premasked_white(ctx, img_path)` in `work/pipeline/ltx_refs.py`:**
   - Checks loaded `characters` metadata (`ref_masks` where `cutout_rel` matches `img_path` relative to `project_dir` and `premasked_white is True`).
2. **Smart `remove_background_images_ref` in `prepare_ltx2_msr_task` (`work/pipeline/ltx_refs.py` L316):**
   - Look at current logic: `remove_bg = 0 if (isinstance(sc, dict) and sc.get("msr_remove_bg") is False) else int(rspec.get("remove_background_images_ref", 1))`.
   - Upgrade cleanly:
     - If `sc` explicitly sets `sc["msr_remove_bg"] = True` -> `1`.
     - If `sc` explicitly sets `sc["msr_remove_bg"] = False` -> `0`.
     - Otherwise (auto/default): if `subj_refs` is non-empty and **every** active `subj_ref` is a pre-masked white cutout (`all(is_ref_premasked_white(ctx, r) for r in subj_refs)`), automatically set `remove_background_images_ref = 0` so `u2net` does NOT re-cutout or chew crisp white collars! If any ref is still raw/unmasked, keep `remove_background_images_ref = 1`.
3. **Sync `stage2_hashes.py` (`L245`, `L354`) & UI indicator (`scene_ref_picker.js`):**
   - Extract shared helper `resolve_msr_remove_bg_flag(ctx, sc, vrspec, subj_refs)` in `ltx_refs.py` and call it from BOTH `prepare_ltx2_msr_task` AND `stage2_hashes.py` (`msr_rmbg`), ensuring hash parity!
   - Remember `_file_sha256(p)` in `stage2_hashes.py` & `compose_ingredients_sheet` ALREADY hashes the exact PNG file bytes, so updating a brush/AI cutout automatically invalidates affected scene caches & queue items cleanly!
   - In `scene_ref_picker.js`, show a small green status pill next to `Remove Subject Backgrounds` when all active subject refs for that scene are already clean Studio cutouts (`✓ Pre-masked cutouts active — MSR u2net bypassed`).

### Step 5: Frontend UI — Characters Tab Visibility & Interactive Mask Studio (`work/ui/static/js/char_mask_editor.js` + `work/ui/static/css/char_mask_editor.css`)
Keep `characters_studio.js` `< 850` lines (`+8` lines max) and `studio.css` untouched:

1. **Look Card Thumbnail Badges & Quick Actions (`characters_studio.js` + `char_mask_editor.js`):**
   - Upgrade each reference thumbnail in `.look-editor-card` (`72x90` -> `92x112` card slot with crisp visual status):
     - Badge top-left: **`✂️ MASKED`** (green pill) when `ref_masks[rPath]` exists, **`AI ISOLATED`** (cyan) for de-occluded takes, or **`RAW`** (amber/slate).
     - Toggleable **Card Overlay Preview** right on the Look card: checkbox/pill `👁️ Show Masks` in the Reference Images toolbar overlays the Rubylith red cut-tint (`<stem>_mask.png`) or checkerboard transparency directly onto all thumbnails in that look so you immediately see *what is kept vs cut out* across every reference at a glance!
     - Per-thumbnail action buttons (bottom bar): **`✂️ Mask / Extract`** (opens Mask Studio Modal), **`👁️ Raw/Cut`** quick toggle if masked, and **`🔮 MSR Check`** (`u2net_msr_preview` quick preview).
   - Sync scene ref trays & character roster avatars automatically whenever a cutout is saved or toggled.

2. **Interactive Mask & Extraction Modal (`#charMaskModalOverlay` in `char_mask_editor.js`):**
   - **Left/Center Interactive Viewport (HTML5 `<canvas>` stack):**
     - Stacked aligned canvases (pan/zoom `0.5x–4x`, crisp image rendering):
       1. `sourceCanvas` — untouched original photo
       2. `overlayCanvas` — renders live visibility mode:
          - **Red Keep/Cut Tint (Rubylith)**: masked-out pixels tinted semi-transparent crimson (`rgba(220, 38, 38, 0.62)`) so character boundaries and accidental leaks jump out immediately
          - **Cutout on White (MSR View)**: shows exact `[255, 255, 255]` output `#19` MSR receives
          - **Cutout on Checkerboard (RGBA)**: shows alpha cutout cleanly
          - **Split Wipe Slider / Side-by-Side**: drag vertical divider comparing Raw vs Cutout
          - **`#19` MSR `U²-Net` Simulator Overlay**: displays what Wan2GP's built-in remover would do to the unmasked image vs your edited mask!
       3. `guideCanvas` — interactive **bounding-box drag rectangle**, **`+` / `-` SAM3 point pins**, and clickable **detected `YOLOv8m-seg` person outlines** (`Person #1 (90%)`, `Person #2 (63%)`, ... hovered/highlighted cleanly!).
   - **Top/Right Controls Panel:**
     - **Manual Tools:**
       - Mode selector: `🖌️ Paint Keep (+)`, `🧹 Erase / Mask Out (-)`, `⬜ Draw Subject Box`, `📍 SAM3 +Point`, `📍 SAM3 -Point`
       - Brush **Size** slider (`4–120 px`), **Edge Softness / Feather** slider (`0–8 px`), **Undo (`Ctrl+Z`)**, **Redo**, **Invert Mask**, **Reset to Full / Clear**, **Edge Refine (`PyMatting`)** button.
     - **AI Auto-Extract Panel (with on-disk status badges & honest quality notes):**
       - **`⚡ Auto-Split People (YOLOv8m-seg — Fast Local)`**: Detects all people in ~5–10s (even on CPU while GPU renders!), draws selectable boxes/masks on canvas; clicking **any person** isolates them while subtracting overlapping neighbors!
       - **`✂️ Precision Cutout (SAM 3.1 — Best Edge & Crowds)`**: Uses active Subject Box + optional `+`/`-` points + optional text prompt (`"woman in gray dress"`), runs Wan2GP `sam3` on GPU (shows `"Will auto-download SAM 3.1 weights on 1st use"` badge if missing locally).
       - **`✨ Hair & Edge Cutout (BiRefNet 1024px)`**: Runs Wan2GP `birefnet-general` scoped inside the current Subject Box (or full frame for solo portraits).
       - **`🔮 Preview What #19 MSR Cuts Out (U²-Net)`**: Instant visual check of Wan2GP's built-in MSR remover so users verify whether extra cleanup is needed.
       - **`🪄 AI De-Occlude / Clean Plate (Qwen Image 2.1)`**: Editable prompt + backend (`Local GPU Qwen 2.1 Turbo` or `fal.ai Qwen Edit 2511`) to generate an un-occluded solo studio ref into `generated_takes` when hands/people block the character!
     - **Footer Actions:** `💾 Save Cutout & Apply to Look` (saves `_mask.png`, `_rgba.png`, `_cutout.png`, swaps into `look.image_refs` while keeping original safely referenced), `Save & Keep Both`, `Cancel`.

---

## 5. Verification & Test Plan

### A. Offline Unit & Integration Tests (`tests/test_char_masking.py`, zero GPU)
1. **Mask worker compositing & white-canvas contract:**
   - Test `char_mask_worker.py` `--action composite_mask` (with a small synthetic test image + binary mask PNG via Wan2GP venv Python or mocked PIL fallback in test): verify `<stem>_mask.png` is mode `L`, `<stem>_rgba.png` has alpha `0` in background, and `<stem>_cutout.png` is RGB mode with `(255, 255, 255)` pure white background (NO black alpha artifacts!).
2. **Yolo neighbor-subtraction & trimap math:**
   - Unit-test `_subtract_bystander_masks(target, others)` cleanly suppresses overlapping neighbor masks while preserving target pixels.
3. **SAM 3.1 prompt payload builder:**
   - Verify Pass 1 builds normalized `[x, y, w, h]` `bounding_boxes` with `points=None`, and Pass 2 passes `obj_id` with `text=None, bounding_boxes=None` + `points`/`point_labels` (strictly satisfying `sam3_video_inference.py` assertions).
4. **Characters API security & non-destructive persistence:**
   - Test `handle_char_masking_api` rejects path traversal (`../../outside.png`), creates backup in `backup/`, populates `look.ref_masks`, swaps `cutout_rel` into `look.image_refs` at original index, and toggles cleanly back to `source_rel`.
5. **MSR `#19` pipeline auto-bypass & hash invalidation:**
   - When `sc` omits `msr_remove_bg` and all `subj_refs` have valid `ref_masks[p].premasked_white == True`: `prepare_ltx2_msr_task` sets `remove_background_images_ref == 0`, matching `compute_project_task_hashes` `msr_rmbg == 0`.
   - When one ref is unmasked OR `sc.msr_remove_bg = True`: `remove_background_images_ref == 1`.
   - Updating `_cutout.png` changes `stage2_hashes` hash cleanly.
6. **`CHAR:isolate` task construction:**
   - Verify generated AI de-occlusion task uses `qwen_image_21_turbo_7B` (or `fal_qwen_edit_2511`), attaches `[source_or_cutout]`, and writes into `look.generated_takes` without touching `#1`–`#18` scene hashes.

### B. Repo Health & Browser E2E Verification
- **Inside sandbox:**
  ```bash
  python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits
  python .agents/skills/mvg-dev-and-testing/scripts/verify_repo_health.py
  python -m unittest tests/test_char_masking.py tests/test_ltx25_refs.py tests/test_regression_fixes.py tests/test_backend_queue.py tests/test_takes_and_rollback.py -v
  ```
- **Outside sandbox (`BypassSandbox: true`, per `AGENTS.md` Rule 4):**
  - Extend `tests/test_ui_e2e_playwright.py` (`test_21`) using a mocked `/api/project/characters/mask-*` handler or real temp-project upload to verify:
    1. Characters tab displays `RAW` / `MASKED` badge + `👁️ Show Masks` overlay toggle on Look cards.
    2. Clicking `✂️ Mask / Extract` opens `#charMaskModalOverlay`, switches view modes (`Rubylith Tint` / `White MSR` / `Checkerboard`), paints on canvas, saves mask, and updates Look card to `✂️ MASKED` without console/page errors.
  ```bash
  python -m unittest tests/test_ui_e2e_playwright.py -v
  ```
- **Optional Manual GPU Verification *(only after user confirms GPU is idle)*:**
  Run `YOLOv8m-seg` + `SAM 3.1` + `Qwen Image 2.1` De-Occlude on a test project ref photo and verify cutout visual quality and MSR `#19` clip output via `inspect_clip.py`.
