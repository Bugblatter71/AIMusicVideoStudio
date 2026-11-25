# Plan 3: Wan 2.2 MultiTalk 14B (`i2v_2_2_multitalk`) & LTX-2.3 JoyAI-Echo 22B (`joyai_echo`)

> **Target implementer:** Hydrogen coding agent  
> **Scope:** Wire two complementary local video models into Music Video Generation Studio as two cleanly separated, independently shippable tracks:
> - **Track A (`#22` — `i2v_2_2_multitalk`):** Dedicated 1- and 2-speaker audio-driven lip-sync reusing the already-downloaded Wan 2.2 14B I2V high/low weights + `chinese-wav2vec2-base` (zero 14B base download!). Unlocks native single-pass 2-speaker duets (`audio_guide` + `audio_guide2` + `speakers_locations`) directly from Studio's `duet_method_a` mode!
> - **Track B (`#23` — `joyai_echo`):** Multi-window story-consistent LTX-2.3 model with named visual + voice memories (`[/store_mem=...]`, `[/load_mem=...]`, `[/new_shot]`) and optional Control Video Memory (`video_prompt_type: "V1"`). *(Requires downloading the JoyAI-Echo 22B checkpoint on first run.)*
>
> **Non-goals:** Do not alter existing video renderers `#1`–`#18` or their hashes; do not run live GPU tests without explicit user confirmation; do not touch active user project files under `projects/`.

---

## 1. Verified Wan2GP Ground-Truth Contracts

Verified directly from local Wan2GP (`C:\Users\bugbl\Downloads\pinokio\api\wan.git\app`):

### Track A — Wan 2.2 MultiTalk 14B (`i2v_2_2_multitalk`)
Source: `defaults/i2v_2_2_multitalk.json`, `models/wan/wan_handler.py` (L47–54, L68–69, L272–277, L298–299, L335–355, L950–979, L1451–1455), `models/wan/multitalk/multitalk.py` (L187–203, L295–343), `wgp.py` (L1476–1480, L1546–1558, L7981–7985, L8151–8199, L12026–12030).

- **Weights reused:** `"URLs": "i2v_2_2"`, `"URLs2": "i2v_2_2"`, plus `chinese-wav2vec2-base` (ALL already in `ckpts/`!). Downloads only the `multitalk` adapter module.
- **Native FPS & frames:** **`fps = 25`** (NOT 16fps like `i2v_2_2`!), `frames_formula: "wan"` (`4*k + 1`, min 5 / 17).
- **Sampling & window defaults:**
  - `guidance_phases: 2`, `switch_threshold: 900`, `guidance_scale: 3.5`, `guidance2_scale: 3.5`, `audio_guidance_scale: 4.0`, `flow_shift: 5.0`, `num_inference_steps: 24` (or `20`–`30`), `sample_solver: "euler"`.
  - `sliding_window_size: 81`, `sliding_window_overlap: 9` (`test_oneframe_overlap` is `False` for MultiTalk), `sliding_window_discard_last_frames: 4`.
- **Image anchoring (`image_prompt_types_allowed: "SVL"`):**
  - Supports Start Image (`image_prompt_type: "S"`). **Does NOT support End Anchor (`"E"`)** and **does NOT support `image_refs`**.
- **Audio & 2-Speaker Routing (`wgp.py` L1476, L1546, L8152 & `multitalk.py` L295):**
  - **1-Speaker (Solo / `male_solo` / `female_solo`):**
    - `audio_prompt_type: "A"`
    - `audio_guide: <wav_path>`, `audio_guide2: None`, `speakers_locations: ""`
  - **2-Speaker (`duet_method_a` / `duet_a`):**
    - `audio_prompt_type: "AB"` (or `"ABN"` to auto-normalize speaker volumes; Wan2GP strips `"N"` automatically if not both `A` and `B`).
    - `audio_guide: <left_speaker_wav_path>`, `audio_guide2: <right_speaker_wav_path>` (MUST be **two separate WAV files**, NOT a stereo-merged WAV!).
    - `speakers_locations`: space-separated pair of locations in `0..100` integer/float percentages — either `"Left:Right Left:Right"` (horizontal strips, e.g., `"0:50 50:100"` derived from `split_x`!) OR `"Left:Top:Right:Bottom Left:Top:Right:Bottom"` (e.g., `"0:0:50:100 50:0:100:100"`). `parse_speakers_locations()` rejects anything that does not split into exactly 2 tokens with 2 or 4 colon-separated values in `[0, 100]`.
  - **Silent / Ambient:** MultiTalk is audio-driven (`audio_guide` drives lip motion). Warn if paired with silent stem, or pass silent WAV with `audio_prompt_type: "A"`.
- **Prompt style:** `"prose"` — visual description of expression, gaze, posture, camera, and environment; do NOT write replacement lyrics/dialogue.
- **No Pass 2 masked inpainting** (cannot run `GVA`/`MVGA` video-mask Pass 2).

### Track B — LTX-2.3 JoyAI-Echo 22B (`joyai_echo`)
Source: `defaults/joyai_echo.json`, `models/ltx2/ltx2_handler.py` (L217–285, L617–639, L930–935, L1136–1140, L1222–1223), `models/ltx2/joyai_echo.py` (L39–146, L269–285, L476–500, L741–832).

- **Weights:** `JoyAI-Echo_diffusion_model_quanto_bf16_int8.safetensors` + LTX-2.3 Gemma-3 / VAE / audio VAE / vocoder (downloaded on first run).
- **Native FPS & frames:** **`fps = 25`** (`ltx2_handler.py` L633), `frames_formula: "ltx2"` (`8*k + 1`), `resolution` aligned to **64px** (default `1280x720`; `joyai_echo.py` L764 rounds to 64px for control memory).
- **Hard runtime overrides (`ltx2_handler.py` L221–245, L930–935):**
  - `_joyai_settings()` enforces:
    - `num_inference_steps: 8`, `guidance_scale: 1.0`, `audio_guidance_scale: 1.0`, `alt_guidance_scale: 1.0`, `alt_scale: 0.0`, `guidance_phases: 1` (or `2`), **`audio_prompt_type: ""` (ALWAYS empty!)**.
    - `image_prompt_type`: `""`, `"S"` (Start Image), or `"V"` (Continue Video) — **NO End Anchor (`E`)** (`image_prompt_types_allowed: "TSV"`).
    - `video_prompt_type`: `""` (normal) OR **`"V1"`** (Control Video Memory — requires `video_guide` WITH an audio track + optional `custom_settings.joyai_control_memory_positions`, e.g. `"man=2s,woman=8s"` or `""` for auto non-silent selection; validated by `validate_control_memory_positions` with max 30s).
  - **CRITICAL:** JoyAI-Echo **generates its own dialogue/soundscape** conditioned on memory and prompt — it **does NOT lip-sync to an external music-video audio stem**! Best used for multi-shot cinematic narrative/dialogue sequences, intros/outros, or cutaway B-roll where cross-shot visual/voice continuity matters.
  - **CRITICAL PROMPT NEWLINE PRESERVATION:**
    - `multi_prompts_gen_type: "PW"` (Per-Window prompts separated by single newlines; NO blank lines or wrapping a single window across multiple lines!).
    - Supports bracket slash commands per window: `[/duration=8s,/store_mem=man1,man2]`, `[/new_shot,/load_mem=man1,woman1]`, `[/load_mem=]` (empty load = clean slate), `[/drop_mem=old]`, `[/overlap=0]`.
    - **Existing MVG trap:** `adapt_video_prompt()` (`work/pipeline/prompts.py` L275–302) joins all lines with `" ".join(lines)` — this would **destroy** JoyAI-Echo's per-window newlines and slash commands! Must add `prompt_style: "joyai_echo"` that preserves newlines & `[/...]` commands.

---

## 2. Critical Codebase Guardrails (Modularity & Line Budgets)

Every `.py`, `.js`, `.html`, `.css` file under `work/` MUST stay strictly `< 850` lines:
- `work/pipeline/context.py` is **848 lines** -> **DO NOT grow `context.py`**. Create a new helper module **`work/pipeline/special_video_models.py`** (< 250 lines) to apply MultiTalk & JoyAI-Echo task shaping, audio resolution, and hash metadata, called via 1–2 line hooks from `context.py`, `stage2_scenes.py`, and `stage2_hashes.py`.
- `work/pipeline/stage2_scenes.py` is **817 lines** -> keep changes to <= 10 lines (delegate 2-speaker audio slicing for MultiTalk to a helper in `special_video_models.py`).
- `work/ui/static/js/duet_repair.js` (837 lines) and `work/ui/static/js/scene_inspector.js` (838 lines) -> keep JS additions tight (<= 10 lines each), or put new JoyAI-Echo/MultiTalk inspector sub-controls in a clean new small JS file (e.g., `work/ui/static/js/model_specific_controls.js`, loaded from `index.html`) so no existing JS file crosses 850 lines!

---

## 3. Step-by-Step Implementation Plan

### Step 1: Add Reserved Video Profiles `#22` & `#23` to `model_profiles.json`
Append to `video_renderers` in `model_profiles.json`:

```json
  "22": {
   "name": "Wan 2.2 MultiTalk 14B (25fps 1- or 2-Speaker Audio Lip-Sync)",
   "model_type": "i2v_2_2_multitalk",
   "suitability_notes": "Rank #22 (Reuses Wan 2.2 14B I2V + wav2vec2 Base): Dedicated audio-driven lip-sync at 25fps. In Solo mode uses single audio stem (A); in Stereo Duet (duet_method_a) mode sends BOTH Left & Right vocal stems separately (audio_guide + audio_guide2, audio_prompt_type=AB) with speakers_locations derived from split_x for true single-pass 2-speaker lip-sync! No End Anchor, no image_refs, no Pass 2 mask inpainting.",
   "frames_formula": "wan",
   "prompt_style": "prose",
   "special_mode": "multitalk",
   "local_only": true,
   "force_fps": 25,
   "num_inference_steps": 24,
   "guidance_phases": 2,
   "switch_threshold": 900,
   "guidance_scale": 3.5,
   "guidance2_scale": 3.5,
   "audio_guidance_scale": 4.0,
   "flow_shift": 5.0,
   "sample_solver": "euler",
   "image_prompt_type": "S",
   "audio_prompt_type": "A",
   "supports_end_anchor": false,
   "supports_image_refs": false,
   "supports_pass2_inpaint": false,
   "sliding_window_size": 81,
   "sliding_window_overlap": 9,
   "sliding_window_discard_last_frames": 4,
   "skip_steps_cache_type": "",
   "custom_settings": {},
   "negative_prompt": "split-screen, morphing, distorted anatomy, deformed mouth, watermark, blurry"
  },
  "23": {
   "name": "LTX-2 2.3 JoyAI-Echo 22B (25fps Multi-Shot Visual & Voice Memory)",
   "model_type": "joyai_echo",
   "suitability_notes": "Rank #23: Connected multi-window story model (25fps, 8 steps) with up to 7 named visual+voice memories ([/store_mem=], [/load_mem=], [/new_shot]) and optional Control Video Memory (V1). Note: JoyAI-Echo generates its own dialogue/soundscape rather than lip-syncing to an external song stem; best for narrative intros, B-roll sequences, and cross-cut continuity.",
   "frames_formula": "ltx2",
   "prompt_style": "joyai_echo",
   "special_mode": "joyai_echo",
   "local_only": true,
   "force_fps": 25,
   "num_inference_steps": 8,
   "guidance_phases": 1,
   "guidance_scale": 1.0,
   "audio_guidance_scale": 1.0,
   "alt_guidance_scale": 1.0,
   "alt_scale": 0.0,
   "sample_solver": "euler",
   "multi_prompts_gen_type": "PW",
   "image_prompt_type": "S",
   "audio_prompt_type": "",
   "supports_end_anchor": false,
   "supports_image_refs": false,
   "supports_pass2_inpaint": false,
   "sliding_window_size": 249,
   "sliding_window_overlap": 9,
   "skip_steps_cache_type": "",
   "custom_settings": {
    "joyai_control_memory_positions": ""
   },
   "negative_prompt": "split-screen, split frame, vertical divider, border, blurry"
  }
```

### Step 2: Create Dedicated Module `work/pipeline/special_video_models.py` (< 240 lines)

Implement clean helpers for both Track A and Track B:

#### Track A Helpers (`multitalk`)
1. **`is_multitalk_spec(rspec: dict | None) -> bool`:** checks `rspec.get("special_mode") == "multitalk"` or `"multitalk" in model_type.lower()`.
2. **`format_multitalk_speakers_locations(sc: dict | None) -> str`:**
   - If `sc.get("speakers_locations")` is an explicit non-empty string: validate it matches Wan2GP's exact `parse_speakers_locations` contract (2 space-separated tokens, each 2 or 4 colon-separated floats in `0..100`) and return normalized string.
   - Otherwise derive cleanly from `split_x = float(sc.get("split_x", 0.50))` clamped to `[0.15, 0.85]`:
     `pct = int(round(split_x * 100))` -> return `f"0:{pct} {pct}:100"`.
3. **`resolve_multitalk_audio_paths(ctx, sc, sid, mode, target_render_start, gen_dur, frames)`:**
   - For `mode in ("duet_method_a", "duet_a")`:
     - Resolves `stem_l = sc.get("stem_m") or sc.get("stem_left") or sc.get("stem_l") or "male"` and `stem_r = sc.get("stem_f") or sc.get("stem_right") or sc.get("stem_r") or "female"`.
     - Slices TWO independent WAV files via `ctx.resolve_scene_audio(sid, stem_l, ...)` and `ctx.resolve_scene_audio(sid, stem_r, ...)`, returning `(wav_l, wav_r)`.
   - For solo / other single-pass modes: returns `(wav_single, None)`.
4. **`apply_multitalk_task(task: dict, rspec: dict, sc: dict | None, audio_path2: Path | None, video_guide, video_mask)`:**
   - Rejects Two-Pass Pass 2 (`if video_guide and video_mask: raise ValueError("Wan 2.2 MultiTalk does not support Two-Pass masked inpainting (Pass 2). Use Stereo Duet (duet_method_a) for native 2-speaker lip-sync.")`).
   - Copies Wan 2.2 MultiTalk sampling params from `rspec` (or `sc` override if present): `guidance_scale`, `guidance2_scale`, `audio_guidance_scale`, `switch_threshold`.
   - Forces `image_prompt_type = "S"` and strips `image_end` (`task.pop("image_end", None)`).
   - If `audio_path2 is not None` (2-speaker `duet_method_a`):
     - Sets `task["audio_prompt_type"] = "ABN"` (or `"AB"` if `sc.get("multitalk_normalize_audio") is False`).
     - Sets `task["audio_guide2"] = str(audio_path2)`.
     - Sets `task["speakers_locations"] = format_multitalk_speakers_locations(sc)`.
   - Else (1-speaker):
     - Sets `task["audio_prompt_type"] = "A"`, `task.pop("audio_guide2", None)`, `task["speakers_locations"] = ""`.

#### Track B Helpers (`joyai_echo`)
1. **`is_joyai_echo_spec(rspec: dict | None) -> bool`:** checks `rspec.get("special_mode") == "joyai_echo"` or `model_type.lower() == "joyai_echo"`.
2. **`resolve_joyai_control_memory_video(ctx, sc: dict | None) -> Path | None`:**
   - Reads optional `sc.get("joyai_memory_clip")` (can be a scene ID like `"S01"` resolved to its active rendered MP4 in `ctx.renders_dir`, or a project-relative video file path).
   - Verifies the file exists AND has an audio track (`probe_has_audio_stream(ctx.ffmpeg, path)` — because Wan2GP's `_validate_joyai_inputs` at `ltx2_handler.py` L283–284 rejects silent control videos!). If missing/silent, raises a clear `ValueError`.
3. **`validate_joyai_positions_syntax(raw: str) -> str`:**
   - Lightweight mirror of Wan2GP's `_parse_control_memory_positions` (`name=2s, woman=8s` or `4s, 14s`, rejecting pure-numeric memory names like `123=4s`).
4. **`apply_joyai_echo_task(ctx, task: dict, rspec: dict, sc: dict | None, video_guide, video_mask)`:**
   - Rejects Two-Pass Pass 2 (`if video_guide and video_mask: raise ValueError("JoyAI-Echo does not support Two-Pass masked inpainting (Pass 2).")`).
   - Copies `guidance_scale`, `audio_guidance_scale`, `alt_guidance_scale`, `alt_scale` from `rspec`.
   - Enforces `task["audio_prompt_type"] = ""` and `task["audio_guide"] = ""` (matches `_joyai_settings()`).
   - Enforces `task["multi_prompts_gen_type"] = "PW"` and `task["image_prompt_type"] = "S" if task.get("image_start") else ""` (strips `image_end`).
   - If `sc.get("joyai_memory_clip")` is configured:
     - Resolves the clip path via `resolve_joyai_control_memory_video(ctx, sc)`.
     - Sets `task["video_guide"] = str(mem_clip_path)`, `task["video_prompt_type"] = "V1"`.
     - Sets `task["custom_settings"]["joyai_control_memory_positions"] = validate_joyai_positions_syntax(sc.get("joyai_memory_positions", ""))`.
   - Else sets `task["video_prompt_type"] = ""` and `task["custom_settings"].setdefault("joyai_control_memory_positions", "")`.

### Step 3: Preserve Newlines for `joyai_echo` Prompts (`work/pipeline/prompts.py`)
In `adapt_video_prompt(prompt: str, prompt_style: str) -> str` (L275–302):
- Add a dedicated branch for `style == "joyai_echo"`:
  - If the prompt already contains JoyAI bracket commands (`[/`) or does not look like H3 structured syntax, return lines joined with **`"\n"`** (removing only blank lines, preserving exact single-newline window boundaries and `[/...]` tags!).
  - If an H3 structured prompt is passed (e.g., user switched an existing H3 scene to `#23`), strip H3 boilerplate headers (`For the target video,`, `integrated_multimodal_description:`, `non_diegetic_music:`, `[Shot 1] Continue directly from <Picture 1>.`) and join the clean text on one line so it still works out of the box.

### Step 4: Wire Pipeline, Task Hashing, ETA & Validation
1. **`work/pipeline/context.py` (`make_video_task`, L663–789):**
   - Accept optional `audio_path2: Path | None = None` parameter.
   - Delegate MultiTalk and JoyAI-Echo task adjustments via a single call to `apply_special_video_model_task(self, task, rspec=rspec, sc=sc, audio_path2=audio_path2, video_guide=video_guide, video_mask=video_mask)` from `.special_video_models` (adds only ~3 lines; trim 3 redundant lines or extract a 5-line block so `context.py` stays strictly `<= 848` lines!).
2. **`work/pipeline/stage2_scenes.py` (L181–201):**
   - In the single-pass `mode in ("duet_method_a", "duet_a")` branch:
     - If `is_multitalk_spec(vrspec)`: call `wav_path, wav_path2 = resolve_multitalk_audio_paths(...)` instead of `resolve_scene_stereo_audio(...)`, and pass `audio_path2=wav_path2` to `ctx.make_video_task(...)`.
     - For all other renderers (MiniMax H3, LTX-2), keep `resolve_scene_stereo_audio(...)` unchanged!
3. **`work/pipeline/stage2_hashes.py` (`compute_project_task_hashes`):**
   - When `is_multitalk_spec(vrspec)`: include `speakers_locations` (for `duet_method_a`), `guidance_scale`, `guidance2_scale`, `audio_guidance_scale`, and `switch_threshold` in `base_payload` so editing `split_x` or `speakers_locations` invalidates the clip hash and queues re-render.
   - When `is_joyai_echo_spec(vrspec)`: include `joyai_memory_clip` (name + `_file_sha256` if exists) and `joyai_memory_positions` in `base_payload`, and ensure `prompt_style == "joyai_echo"` preserves newlines before hashing.
4. **`work/pipeline/media_utils.py`:**
   - Add `"audio_guide2"` to `_NON_SETTING_TASK_KEYS` (L32–35) so file-path changes don't clutter settings-diff messages.
   - Add `"wan"` branch to `quantize_video_frames()` (L383–391 — note `context.py` L516 already has `formula == "wan"` `4*k+1`, while `media_utils.py` L383 currently only handles `ltx2` and `h3`!).
   - In `estimate_scene_render_time()` fallback (L616): recognize `"22"` -> `formula = "wan", fps = 25.0`, `"23"` -> `formula = "ltx2", fps = 25.0`.
5. **`.agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py`:**
   - Validate `special_mode == "multitalk"` (`force_fps == 25`, `frames_formula == "wan"`, `supports_end_anchor is False`) and `special_mode == "joyai_echo"` (`force_fps == 25`, `audio_prompt_type == ""`, `multi_prompts_gen_type == "PW"`).

### Step 5: Frontend UI Controls, Scenes Tab Parity & Combo Warnings
Keep existing JS files strictly `< 850` lines by creating a focused new module **`work/ui/static/js/model_specific_controls.js`** (< 220 lines) and loading `<script src="js/model_specific_controls.js?v=1"></script>` in `work/ui/static/index.html` (L802, right before `</body>`):

1. **Exact DOM Mount & Lifecycle Wiring (`index.html`, `fal_settings.js`, `scenes_tab.js`):**
   - Add `<div id="modelSpecificControlsHost" style="margin-top: 6px;"></div>` in `work/ui/static/index.html` right below `<div id="duetPassEngineHost" ...></div>` (L175).
   - Call `window.syncModelSpecificControlsUI?.(sc);` as a single line inside `fal_settings.js` `syncSceneBackendUI(sc)` (L412, right next to `window.syncDuetPassControlsUI?.(sc)`). Because `syncSceneBackendUI` already runs on `selectScene()`, `#sceneMode` change, `#sceneVideoRenderer` change, and project-default model changes, this keeps Scene Inspector controls synchronized automatically with zero bloat in `scene_inspector.js`.
   - Every control rendered by `model_specific_controls.js` must respect `Boolean(sc.locked)` (`disabled = true`), and on edit call `window.markProjectDirty?.()`, `window.updateInspectorSceneHeader?.(sc)`, and `window.recordHistorySnapshot?.(...)`.
   - **Scenes Tab parity (`work/ui/static/js/scenes_tab.js` L576):** add a single 1-line hook `window.appendSceneCardModelControls?.(ctrls, sc, idx, applyFieldSideEffects, { makeCtrl, mkNum });` right below `appendTwoPassEngineControls`, implemented in `model_specific_controls.js` to render compact inline MultiTalk `Split X` + `Speakers Locations` (for `#22` + `duet_method_a`) or JoyAI-Echo `Memory Clip` + `Memory Positions` (for `#23`).
2. **`local_only` Backend Dropdown Lock (`fal_settings.js` L375–381 & `scenes_tab.js` L539–545):**
   - Check `const isLocalOnly = Boolean(window.state?.models?.video_renderers?.[vrId]?.local_only);` and lock `#sceneBackend` / `backSel` to `"local"` (`disabled = true`, `dataset.forceDisabled = "1"`) when `#22` or `#23` is active so users cannot accidentally leave a local-only model pointed at `fal.ai` or `minimax`.
3. **MultiTalk (`#22`) controls (Scene Inspector + Scenes Tab):**
   - When active scene renderer has `special_mode === "multitalk"` and mode is `duet_method_a` (Stereo Duet):
     - Show an inline **Speaker Split / BBox** control row (`split_x` slider/number + optional custom `speakers_locations` text input with placeholder `0:50 50:100` and tooltip explaining `"Left:Right Left:Right"` or `"L:T:R:B L:T:R:B"`).
     - Show an info pill: `🎙️ Native 2-Speaker MultiTalk (Left stem -> Left speaker, Right stem -> Right speaker, 25fps)`.
4. **JoyAI-Echo (`#23`) controls & Modular-Mode Guard (Scene Inspector + Scenes Tab):**
   - When active scene renderer has `special_mode === "joyai_echo"`:
     - Show a compact **JoyAI-Echo Memory Panel** with:
       - **Control Video Memory Clip** dropdown (`(None)` + list of other rendered scene clips in `state.assets.clips`).
       - **Memory Positions** input (`sc.joyai_memory_positions`, placeholder e.g. `man=2s, woman=8s`).
       - Quick-insert prompt chips (insert at cursor in `#scenePrompt`): `[/duration=8s]`, `[/store_mem=char1]`, `[/load_mem=char1]`, `[/new_shot]`, `[/load_mem=]`.
     - Show a clear amber info pill: `🎬 JoyAI-Echo generates its own audio/dialogue from prompt + memory (external vocal stem is not synced)`.
     - **Modular Components Mode Guard:** If `sc.prompt_mode === "components"` while `#23` is active, display an inline warning banner inside `#modelSpecificControlsHost` explaining that Modular Components compiles single-shot H3 boilerplate and prevents multi-window `[/store_mem=...]` / `[/new_shot]` prompts, with a one-click **`Switch to Raw Prompt`** button that clicks `#btnPromptModeRaw` (or sets `sc.prompt_mode = "raw"` and refreshes the prompt editor).
5. **Combo Warnings (`work/ui/static/js/duet_repair.js` `evaluateComboWarnings`):**
   - `multitalk_pass2`: warns if `#22` is assigned to Two-Pass Duet Pass 2 (recommends switching mode to **Stereo Duet (`duet_method_a`)** for native single-pass 2-speaker MultiTalk!).
   - `multitalk_end_anchor`: warns if `anchor_end` is set with `#22` (MultiTalk ignores End Anchor).
   - `joyai_echo_vocal_stem`: warns if `#23` is selected on a scene with an active vocal stem (reminds user that JoyAI-Echo generates its own audio rather than lip-syncing the song stem).
   - `joyai_echo_pass2`: warns if `#23` is assigned to Two-Pass Duet Pass 2.
6. **Dropdown Badges (`scene_inspector.js` L62 & `scenes_tab.js` L530):**
   - Show `[MultiTalk 2-Spk]` for `special_mode === "multitalk"` and `[Story Memory]` for `special_mode === "joyai_echo"`.

---

## 4. Verification & Test Plan

Add unit tests in `tests/test_multitalk_and_joyai.py` (all offline, zero GPU):
1. **Profile schema & validator:** `validate_wgp_profile.py` passes with `#22` and `#23` registered.
2. **Track A — MultiTalk (`#22`):**
   - `model_fps` == `25.0`, `valid_video_frames` uses `4*k + 1` formula.
   - Solo scene -> `audio_prompt_type == "A"`, `audio_guide` set, `"audio_guide2" not in task`, `speakers_locations == ""`, `guidance_scale == 3.5`, `guidance2_scale == 3.5`, `audio_guidance_scale == 4.0`, `switch_threshold == 900`, `image_prompt_type == "S"`, `image_end` omitted even if `sc["anchor_end"]` is set.
   - `duet_method_a` scene with `split_x = 0.45` -> slices TWO separate WAVs (`audio_guide`, `audio_guide2`), sets `audio_prompt_type == "ABN"`, `speakers_locations == "0:45 45:100"`.
   - Custom `speakers_locations = "5:10:45:90 55:10:95:90"` passes validation; malformed strings (e.g. `"0:50"`, `"0:150 50:100"`) raise `ValueError`.
   - Pass 2 (`video_guide` + `video_mask`) -> raises `ValueError`.
   - Changing `split_x` or `speakers_locations` changes the task hash in `compute_project_task_hashes()`.
3. **Track B — JoyAI-Echo (`#23`):**
   - `model_fps` == `25.0`, `valid_video_frames` uses `8*k + 1` formula.
   - Multi-window prompt with `[/duration=8s,/store_mem=man1]\n[/new_shot,/load_mem=man1]` preserves single newlines and bracket commands verbatim through `adapt_video_prompt(..., "joyai_echo")` and `make_video_task()`.
   - `audio_prompt_type == ""`, `audio_guide == ""`, `multi_prompts_gen_type == "PW"`, `guidance_scale == 1.0`, `num_inference_steps == 8`.
   - Scene with `joyai_memory_clip` (valid MP4 with audio) + `joyai_memory_positions = "man=2s, woman=6s"` -> sets `video_prompt_type == "V1"`, `video_guide == <clip_path>`, `custom_settings["joyai_control_memory_positions"] == "man=2s, woman=6s"`.
   - Invalid pure-numeric memory name `"123=2s"` raises `ValueError`.
   - Pass 2 masked inpainting raises `ValueError`.
4. **Repo health & Playwright E2E:**
   - Inside sandbox:
     ```bash
     python .agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py
     python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits
     python .agents/skills/mvg-dev-and-testing/scripts/verify_repo_health.py
     python -m unittest tests/test_multitalk_and_joyai.py tests/test_regression_fixes.py tests/test_backend_queue.py tests/test_resolution_and_upsampling.py -v
     ```
   - Outside sandbox (`BypassSandbox: true`, per `AGENTS.md` Rule 4):
     ```bash
     python -m unittest tests/test_ui_e2e_playwright.py -v
     ```
   - Manual GPU smoke test *(only after user confirms GPU is idle)*:
     - **Track A:** render 1 solo scene and 1 `duet_method_a` 2-speaker scene on `#22` (`i2v_2_2_multitalk`) at 480p/540p and inspect lip-sync & speaker separation via `inspect_clip.py`.
     - **Track B:** render 1 two-window scene on `#23` (`joyai_echo`) with `[/store_mem=...]` -> `[/new_shot,/load_mem=...]` and verify continuity & audio.
