# Specification: Character Identity Reference Images for Scene Generation

**Status:** Approved / Architecture Finalized (v3, Second Pass incorporating user decisions)  
**Date:** 2026-11-08  
**Target Codebase:** Music Video Generation Studio (`work/pipeline`, `work/ui`, `model_profiles.json`)  
**Engine Verified Against:** `pinokio/api/wan.git/app/models/minimax_h3/minimax_h3_handler.py` and `prompt_enhancer.py`

---

## 0. Key User Decisions & Architectural Alignment

Following user review of v2, the following strategic decisions govern this implementation:

| Decision Area | User Directive | Implementation Strategy |
|---|---|---|
| **Selective Per-Scene Re-render** | *"I don't want to re-render every scene using Ref2VA, most of them are fine, it's only the ones where characters are small in the start image."* | Add a **Video Model** selector in the Scene Inspector. Default option is **`Default (Project: #1 FL2VA)`**. Only scenes explicitly overridden (e.g., to `#12 Ref2VA`) will re-render. All existing FL2VA renders keep their hashes and cached clips. |
| **Night-Out Wardrobe Leakage** | *"Make night-out ref sheets from the existing video if possible."* | Extract sharp, high-res face crops directly from `S18_solo_1344x768.mp4` ($t=3.0\text{s}$) showing the male lead in midnight-blue shirt and female lead in dark sleeveless dress. Saved to `references/nightout/` and mapped to night-out scenes. |
| **Dangling Tag Cleanup** | Approved. | For models that do not take identity refs (FL2VA / LTX-2), strip dangling `"from <image1> and <image2>"` text after hashing so text encoders receive clean descriptors without invalidating existing clip hashes. |
| **Pilot Testing** | *"Use shorter scenes and use the test project for them."* | All initial mock and GPU verification will run on `projects/test_project/` using 4-second scenes: **S01** (male solo, 4s), **S02** (duet method B, 4s), and **S03** (female solo, 4s). Active project `all_or_nothing` remains untouched until verified. |

---

## 1. Problem Statement & Root Cause

Scenes are currently generated from a single timeline start image (`image_start` = the scene's anchor). When characters are framed in **wide shots**, **medium full shots**, or **environmental establishing frames** (e.g., A05, A07, A10, A11, A12, A14), their faces turn generic, blur, or distort during motion:

1. **Resolution Starvation at Distance:** In a wide shot, a character's face may occupy only $24 \times 24$ to $40 \times 40\text{ px}$. While anchors are generated in 2K, they are downscaled to video render resolution ($1344 \times 768$ or $960 \times 544$) before feeding the DiT diffusion backbone. At heavy VAE compression, the face is reduced to too few latent tokens to retain anatomical identity.
2. **Anchor Multi-Tasking Conflict:** The anchor serves simultaneously as **Frame 0 composition** and **only visual appearance reference**. In wide framing, zero high-resolution facial data reaches the model.
3. **Dangling Prompt Descriptors:** Current scene prompts state characters are *"from `<image1>` and `<image2>`"*, but in FL2VA no reference images are provided. The model must rely on generic text hallucinations.

---

## 2. Engine Capabilities Summary

Wan2GP's MiniMax H3 implementation natively supports reference-based identity conditioning via the **Ref2VA** family:

| Family | Model Profiles | `image_start` | Auxiliary `image_refs` | Video Prompt Type | Prompt Structure |
|---|---|:---:|:---:|:---:|---|
| **MiniMax H3 FL2VA** | #1–#8 | Yes | No (FI only) | `""` (Pass 1) / `GVA` (Pass 2) | 3-field standard prompt |
| **MiniMax H3 Ref2VA** | #9, #10, #12, #13 (+ #11 FastH3 Omni) | Yes | **Yes (up to 9 images)** | `I` (Pass 1) / `GVAI` (Pass 2) | 6-section structured prompt |
| **LTX-2** | #14–#16 | Yes | No | `MVGA` | LTX prompt style |

### Verified Ref2VA Mechanics
- **Picture Mapping:** `<Picture 1>` is strictly reserved for the starting frame (`image_start`). General reference images follow in order: `<Picture 2>`, `<Picture 3>`, etc.
- **Sliding Window Resilience:** When a scene exceeds one window (e.g., S18, S26a), `<Picture 1>` carries over the overlap continuity frame, while `<Picture 2..N>` remain identity reference anchors across all windows.
- **Resolution Control:** `video_prompt_type: "I"` maintains the selected video output dimensions ($1344 \times 768$, $960 \times 544$, etc.). The `"KI"` flag must never be used as it derives output dimensions from the reference image.
- **Reference Sizing:** `image_refs_relative_size: 100` (default) allocates full native pixel budget for identity tokens.

---

## 3. Per-Scene Video Model Selector Architecture

To prevent unnecessary re-rendering across the 26 scenes in `all_or_nothing`, the studio will adopt a hierarchical model resolution architecture.

### 3.1 Project Hierarchy
1. **Global Default:** Defined in `project.json` under `models.default_video_renderer` (currently `1` = MiniMax H3 FL2VA Pruned PDD).
2. **Scene-Level Property:** Each scene in `project.json` has an optional `renderer` field:
   - `renderer: null` or `"default"` $\rightarrow$ Inherits global default (Model 1).
   - `renderer: 12` $\rightarrow$ Overrides this specific scene to Model 12 (Ref2VA Pruned PDD).
3. **Duet Scene Granularity:** For duet scenes with separate passes, `renderer_m` (Pass 1) and `renderer_f` (Pass 2) allow overriding individual passes if only one singer requires identity injection.

### 3.2 UI Integration (`work/ui/static/`)
In [index.html](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/ui/static/index.html) and [scene_inspector.js](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/ui/static/js/scene_inspector.js):
- Add a **Video Model** dropdown immediately below the Scene Anchor selector:
  ```html
  <div class="form-group" style="margin-bottom: 12px;">
    <label style="display: flex; justify-content: space-between; font-size: 11px; color: var(--text-muted); margin-bottom: 4px;">
      <span>Video Generation Model</span>
      <span id="sceneRendererBadge" class="badge" style="display: none;">Custom Override</span>
    </label>
    <select id="sceneVideoRenderer" class="form-control" style="font-size: 12px;"></select>
  </div>
  ```
- **Options Populated Dynamically:**
  1. `Default (Project: #1 MiniMax H3 FL2VA Pruned PDD)` (value: `""`)
  2. `#12 MiniMax H3 Ref2VA Pruned PDD 8-Step (Auxiliary Ref Images)` (value: `"12"`)
  3. `#11 MiniMax H3 FastH3 Omni 8-Step (Ref2VA Student)` (value: `"11"`)
  4. `#1 MiniMax H3 FL2VA Pruned PDD 8-Step` (value: `"1"`)
  5. *(...remaining models from `model_profiles.json`)*
- **Inspector Event Handling:**
  - Selecting `Default` sets `sc.renderer = null`, automatically reverting to project default.
  - Selecting an explicit model sets `sc.renderer = parseInt(val)`.
  - Changes trigger standard timestamped project auto-backup and refresh the queue status.

### 3.3 Zero-Invalidation Guarantee for Existing Clips
In `compute_project_task_hashes()` ([stage2_scenes.py](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/pipeline/stage2_scenes.py)):
- For scenes where `renderer` is `null` (or `"default"`), `vmodel` resolves to `minimax_h3_fl2va_pruned_pdd`.
- Because reference conditioning is skipped for FL2VA, the computed hash payload is **byte-for-byte identical** to the existing hashes on disk.
- **Result:** Existing renders for close-up and medium scenes remain marked `UP-TO-DATE (VALID MATCH)`. Only scenes switched to Ref2VA will queue for rendering.

---

## 4. Reference Sets & Night-Out Crops

### 4.1 Studio vs Night-Out Reference Sets
To prevent wardrobe leakage (e.g. the male lead's teal-tinted suede studio jacket bleeding into evening scenes), reference sheets are grouped by context:

```
projects/all_or_nothing/
├── work/ref_sheets/                    # Studio Recording Context
│   ├── male_angle1_front34.jpg
│   ├── male_angle3_profile.jpg
│   ├── female_angle1_front34.jpg
│   └── female_angle3_profile.jpg
└── references/nightout/                # Upscale Evening Party Context
    ├── male_nightout_front34.jpg       # Midnight-blue collared shirt
    ├── male_nightout_profile.jpg
    ├── female_nightout_front34.jpg     # Dark sleeveless cocktail dress
    └── female_nightout_profile.jpg
```

### 4.2 Automated Extraction from Existing S18 Video
A dedicated utility script (`extract_nightout_refs.py`) extracts reference portraits directly from the approved master video render:
- **Source:** `projects/all_or_nothing/work/renders_v3/S18_solo_1344x768.mp4`
- **Timestamp:** $t = 3.0\text{s}$ (stable lighting under the sapphire ceiling ring).
- **Male Crop:** Coordinates `x=320..680, y=120..580`, scaled to $512 \times 640\text{ px}$.
- **Female Crop:** Coordinates `x=600..980, y=200..680`, scaled to $512 \times 640\text{ px}$.

### 4.3 Scene Reference Resolution Rule
When a scene runs on a model supporting image references:
1. If the scene prompt contains `{MALE_NIGHTOUT_DESC}` or `{FEMALE_NIGHTOUT_DESC}`, or the scene's anchor is `A11`, the pipeline resolves references from `references/nightout/`.
2. Otherwise, it resolves from the default studio sheets in `work/ref_sheets/`.
3. If a scene explicitly defines `sc["image_refs"]`, those paths take absolute precedence. Setting `sc["image_refs"] = []` completely disables references for that scene.

---

## 5. Pipeline & Prompt Engineering

### 5.1 FL2VA Dangling Tag Cleanup (`strip_dangling_ref_tags`)
In [prompts.py](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/pipeline/prompts.py):
When a scene renders with FL2VA (or LTX-2), phrases referencing `<image1>` through `<image4>` are removed:
```python
def strip_dangling_ref_tags(prompt: str) -> str:
    """Removes 'from <image1> and <image2>' clauses when rendering on non-reference models."""
    cleaned = re.sub(
        r"\s*\(?\s*from\s+<image\d+>(?:\s*(?:and|,)\s*<image\d+>)*\s*\)?",
        "",
        prompt,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()
```
*Note:* This cleanup is applied during task compilation in `make_video_task()` **after** task hash computation, ensuring that existing cache hashes remain 100% stable.

### 5.2 Ref2VA Six-Section Prompt Construction
When rendering on Ref2VA (#12 or #11), the pipeline automatically transforms the prompt into MiniMax's native 6-section structure:

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
subject_definitions:
<Subject 1> is the 30-year-old Black male singer from <Picture 2> and <Picture 3>, preserving his facial identity, skin tone, short tapered fade and neat short beard.
summary:
[reference generation] Continue the scene established in <Picture 1>, preserving the character identity of <Subject 1>.
retention_analysis:
<Picture 1> (anchors [Shot 1] at 0.00s): fully_preserved - composition, background, camera framing, and wardrobe.
<Subject 1> (appears in [Shot 1]): attribute_transfer - facial features, eyes, mouth, and hair; wardrobe and lighting follow <Picture 1>.
detailed_description:
The target video is ... [Shot 1] <Subject 1> stands at the microphone singing with natural lip movements synchronized...
overall_soundscape:
Studio ambience and male vocal.
non_diegetic_music:
N/A
```

*Crucial Engine Constraints:*
- **No internal blank lines:** Ref2VA uses blank lines to separate sliding windows. All 6 sections are joined with single newlines.
- **`attribute_transfer` Mode:** Used for subjects so that lighting, clothing, and environment are inherited from Frame 0 (`<Picture 1>`), while identity is transferred from `<Picture 2..N>`.

---

## 6. Pilot Testing Protocol on `projects/test_project`

To guarantee zero disruption to active production work, validation will proceed on `test_project`:

### 6.1 Test Suite Configuration
`projects/test_project/test_project.json` contains three 4.0-second scenes (96 frames at 24fps):
- **S01** (`male_solo`, 0.0s – 4.0s, stem: `male`)
- **S02** (`duet_method_b`, 4.0s – 8.0s, stems: `male` / `female`)
- **S03** (`female_solo`, 8.0s – 12.0s, stem: `female`)

### 6.2 Test Matrix
1. **Baseline Invariance:** Run `test_backend_queue.py` to confirm all 67 existing tests pass and all `all_or_nothing` hashes remain unchanged.
2. **Model Override Test:** Set `S01.renderer = 12` in `test_project.json`. Confirm that:
   - S01 queue status changes to `queued` (reason: `MODEL CHANGED` or `PROMPT/SEED/SETTINGS EDITED`).
   - S02 and S03 remain `completed` / `skipped`.
   - Task dictionary for S01 contains `image_refs`, `video_prompt_type: "I"`, and the 6-section prompt.
3. **Duet Ref Test:** Set `S02.renderer = 12`. Verify that Pass 1 resolves male references (`<Picture 2, 3>`) and Pass 2 resolves female references (`<Picture 2, 3>`).
4. **Execution Dry Run:** Validate with `--dummy` render mode to confirm pipeline end-to-end flow in <2 seconds.
5. **Live GPU Test (Only upon user confirmation):** Render a single 4-second scene using Model 12 on GPU and evaluate facial consistency via filmstrip inspection.

---

## 7. Implementation Checklist

### Phase 1: Pure Logic & Prompt Helpers (`work/pipeline/prompts.py`)
- [ ] Implement `find_ref_tags(prompt: str) -> list[int]`.
- [ ] Implement `resolve_scene_refs(prompt, ref_list) -> (list[Path], dict[int, int])`.
- [ ] Implement `strip_dangling_ref_tags(prompt: str) -> str`.
- [ ] Implement `build_ref2va_prompt(prompt, subjects, ref_map) -> str`.
- [ ] Unit tests for prompt remapping and dangling tag stripping in `tests/test_backend_queue.py`.

### Phase 2: Profiles & Task Compilation (`model_profiles.json`, `context.py`)
- [ ] Add `"supports_image_refs": true` to video models #9, #10, #11, #12, #13 in `model_profiles.json`.
- [ ] Update `make_video_task()` in `context.py` to accept `image_refs` and conditionally append `"I"` to `video_prompt_type`.
- [ ] Apply `strip_dangling_ref_tags()` when rendering on FL2VA/LTX-2 models.

### Phase 3: Hash Calculation & Queue Diagnostics (`stage2_scenes.py`, `server_queue.py`)
- [ ] Update `compute_project_task_hashes()`: add `refs` payload **only** when `supports_image_refs` is True and refs are present.
- [ ] Add queue reason `CHARACTER REF <fn> EDITED` / `NEWER ON DISK` in `server_queue.py`.
- [ ] Store `ref_hashes` in `state_hashes.json`.

### Phase 4: Night-Out Reference Extraction Tool
- [ ] Create script `tools/extract_nightout_refs.py`.
- [ ] Extract and verify crops for male and female leads from `S18_solo_1344x768.mp4`.
- [ ] Save crops to `projects/all_or_nothing/references/nightout/`.

### Phase 5: Web UI Video Model Dropdown (`index.html`, `scene_inspector.js`)
- [ ] Add `#sceneVideoRenderer` dropdown to Scene Inspector in `index.html`.
- [ ] Populate dropdown in `scene_inspector.js` with `Default (Project: #1 FL2VA...)` followed by available models.
- [ ] Wire change listener to persist `sc.renderer` through existing REST API and update the "Custom Override" badge.

### Phase 6: Pilot Verification on `test_project`
- [ ] Run automated regression test suite (`tests/test_backend_queue.py`).
- [ ] Run dummy render tests on `test_project` for S01 and S02.
- [ ] Verify line counts across all modified files remain within the <850 line architecture standard.
