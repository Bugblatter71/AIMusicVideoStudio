# Specification: Character Management Studio & Multi-Reference System (v2 — Flash-Ready Implementation Blueprint)

**Status:** Approved Architecture / Implementation-Ready Blueprint (v2, post-critique)  
**Date:** 2026-11-08  
**Target Codebase:** Music Video Generation Studio (`work/pipeline/`, `work/ui/`, `tests/`)  
**Inspiration:** Google Flow Character & Asset Reference System  

---

## 0. Key Architectural Fixes Incorporated in v2 (Read First)

Before implementing, adhere strictly to these 8 architectural rules established during the v2 design review:

1. **Strict `<850` Line Modularity Guardrails:**
   - Do **NOT** add heavy logic to `work/ui/static/js/scene_inspector.js` (currently 871 lines), `work/pipeline/stage2_scenes.py` (currently 996 lines), `work/pipeline/context.py` (773 lines), or `work/ui/server_routes.py` (747 lines).
   - Put all backend character/reference logic, migration, upload handling, and `ffmpeg` frame cropping in a **new module**: `work/pipeline/characters.py` (~240 lines).
   - Put all frontend UI for the **`👤 Characters & Refs`** tab, the `@mention` caret autocomplete popover, and the reactive **"Attached References"** chip tray in a **new script**: `work/ui/static/js/characters_studio.js` (~350 lines).
2. **Zero-Invalidation Hash Guarantee for Existing Renders:**
   - Existing `all_or_nothing` FL2VA renders were hashed with `{MALE_STUDIO_DESC}` containing `"from <image1> and <image2>"`.
   - Each character look/variant supports an optional `"legacy_prompt_text"` field (populated automatically when migrating from `"variables"`).
   - In `compute_project_task_hashes()`, compute both the new expanded prompt hash **and** the legacy/tag-stripped fallback hash so migrating `{MALE_STUDIO_DESC}` $\rightarrow$ `@male_studio` never invalidates any of the 37 completed clips in `all_or_nothing`.
3. **Prompt Text is the Single Source of Truth for `@mentions`:**
   - Do **NOT** duplicate `@mention` paths into `sc["image_refs"]` when selecting from the autocomplete dropdown (which causes desync when users type or backspace `@tags` manually, and breaks Two-Pass Duets that have separate `prompt_m` and `prompt_f`).
   - Instead, `@mentions` are parsed live from the active prompt (`prompt`, `prompt_m`, or `prompt_f`). `sc["image_refs"]` is kept strictly as an optional manual override for legacy `<image1>` positional indexing.
4. **Unified Entity & Look Schema (`characters`):**
   - Do not split data between `"characters"` and `"asset_references"`. Use a single unified `"characters"` dictionary where each entity has `"kind": "character" | "ensemble" | "object"`, a `"default_look"` key (so typing `@marcus` automatically resolves to his default look `@male_studio`), and a `"looks"` dictionary.
5. **Role-Aware Ref2VA 6-Section Prompt Compilation:**
   - Differentiate between `kind == "character"` (uses `attribute_transfer - facial features and hair; wardrobe and lighting follow <Picture 1>`) and `kind in ("ensemble", "object")` (uses `subject_appearance_reference - instruments, group staging, and visual details follow <Picture N>`).
   - When the prompt contains camera movement keywords (`pan`, `zoom`, `track`, `crane`, `glide`, `sweep`) or references $>2$ subjects, set `<Picture 1>` retention in `retention_analysis` to `environment_and_style_reference (anchors [Shot 1] at 0.00s; camera framing moves dynamically)` instead of `fully_preserved` so Ref2VA does not lock the camera to Frame 0.
6. **Timeline Cue Marker Integration & Pacing Hint:**
   - `@mentions` work seamlessly alongside relative `<marker_name>` cues so multi-subject camera pans (like Orchestra S10) anchor each subject to a specific timestamp.
   - If $>3$ distinct subjects are referenced in a scene shorter than $8.0\text{s}$, show a non-blocking pacing tip in the UI tray.
7. **Media Subdirectory Serving (`server_media.py`):**
   - Add a `category == "ref"` handler (and relative path fallback in `category == "anchor"`) in `work/ui/server_media.py` so nested paths like `references/nightout/male_nightout_front34.jpg` and `references/orchestra/pianist.jpg` stream cleanly over `/media/<slug>/ref/<rel_path>`.
8. **Clear Tab Naming:**
   - Rename `🎨 Anchors` to `🖼️ Scene Anchors` (Frame 0 starting shots) and add `👤 Characters & Refs` right next to it so their roles are 100% distinct.

---

## 1. Unified Data Schema (`project.json`)

Characters, ensembles, and props live under the top-level `"characters"` key in `project.json`. If `"characters"` is absent or empty, the backend synthesizes default entries in-memory from `"variables"` (`MALE_STUDIO_DESC`, `FEMALE_STUDIO_DESC`, `MALE_NIGHTOUT_DESC`, `FEMALE_NIGHTOUT_DESC`) and `models.anchor_shared_settings.image_refs`.

```json
{
  "characters": {
    "marcus": {
      "id": "marcus",
      "name": "Marcus (Lead Vocalist)",
      "kind": "character",
      "default_look": "studio",
      "avatar": "work/ref_sheets/male_angle1_front34.jpg",
      "physical_traits": "the exact 30-year-old Black male singer",
      "looks": {
        "studio": {
          "tag": "@male_studio",
          "legacy_var": "MALE_STUDIO_DESC",
          "name": "Studio Recording",
          "description": "short tapered fade haircut, neat short beard, wearing a dark slate-navy suede jacket over a burgundy crew-neck t-shirt and thin silver chain",
          "legacy_prompt_text": "the exact 30-year-old Black male singer from <image1> and <image2> (short tapered fade haircut, neat short beard, wearing a dark slate-navy suede jacket over a burgundy crew-neck t-shirt and thin silver chain)",
          "image_refs": [
            "work/ref_sheets/male_angle1_front34.jpg",
            "work/ref_sheets/male_angle3_profile.jpg"
          ]
        },
        "nightout": {
          "tag": "@male_nightout",
          "legacy_var": "MALE_NIGHTOUT_DESC",
          "name": "Upscale Evening",
          "description": "short tapered fade haircut, neat short beard, wearing a smart dark midnight-blue collared shirt",
          "legacy_prompt_text": "the exact 30-year-old Black male singer from <image1> and <image2> (short tapered fade haircut, neat short beard, wearing a smart dark midnight-blue collared shirt)",
          "image_refs": [
            "references/nightout/male_nightout_front34.jpg",
            "references/nightout/male_nightout_profile.jpg"
          ]
        }
      }
    },
    "elena": {
      "id": "elena",
      "name": "Elena (Producer & Guitarist)",
      "kind": "character",
      "default_look": "studio",
      "avatar": "work/ref_sheets/female_angle1_front34.jpg",
      "physical_traits": "the exact late-20s woman",
      "looks": {
        "studio": {
          "tag": "@female_studio",
          "legacy_var": "FEMALE_STUDIO_DESC",
          "name": "Studio Control Room",
          "description": "shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, wearing a black suede biker jacket over a dark charcoal V-neck top and a thin silver necklace",
          "legacy_prompt_text": "the exact late-20s woman from <image3> and <image4> (shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, wearing a black suede biker jacket over a dark charcoal V-neck top and a thin silver necklace)",
          "image_refs": [
            "work/ref_sheets/female_angle1_front34.jpg",
            "work/ref_sheets/female_angle3_profile.jpg"
          ]
        },
        "nightout": {
          "tag": "@female_nightout",
          "legacy_var": "FEMALE_NIGHTOUT_DESC",
          "name": "Evening Cocktail",
          "description": "shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, wearing a chic sleeveless burgundy-black evening top",
          "legacy_prompt_text": "the exact late-20s woman from <image3> and <image4> (shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, wearing a chic sleeveless burgundy-black evening top)",
          "image_refs": [
            "references/nightout/female_nightout_front34.jpg",
            "references/nightout/female_nightout_profile.jpg"
          ]
        }
      }
    },
    "orchestra": {
      "id": "orchestra",
      "name": "Soundstage Orchestra Sections",
      "kind": "ensemble",
      "default_look": "strings",
      "avatar": "work/anchors_v3/A07 - Musicians_playing_on_soundstage_…_2c7b892e_2K - Copy.jpg",
      "physical_traits": "professional session musicians on the dark soundstage",
      "looks": {
        "pianist": {
          "tag": "@pianist",
          "name": "Grand Piano Soloist",
          "description": "classical pianist seated at an ebony concert grand piano in formal dark attire",
          "image_refs": ["references/orchestra/pianist.jpg"]
        },
        "strings": {
          "tag": "@strings",
          "name": "Chamber String Section",
          "description": "chamber string orchestra bowing their violins and cellos in sweeping unison",
          "image_refs": ["references/orchestra/strings.jpg"]
        },
        "brass": {
          "tag": "@brass",
          "name": "Brass Section",
          "description": "brass section playing trumpets and French horns under warm stage lights",
          "image_refs": ["references/orchestra/brass.jpg"]
        },
        "woodwinds": {
          "tag": "@woodwinds",
          "name": "Woodwind Section",
          "description": "woodwind section playing flutes and clarinets in synchronized harmony",
          "image_refs": ["references/orchestra/woodwinds.jpg"]
        }
      }
    }
  }
}
```

### Tag Resolution Rules
1. **Explicit Look Tag (`@male_studio`, `@pianist`):** Resolves directly to that look's `description`, `image_refs`, and parent entity's `physical_traits` and `kind`.
2. **Entity Shorthand (`@marcus`, `@elena`, `@orchestra`):** Resolves to `entity.looks[entity.default_look]`.
3. **Legacy Variable (`{MALE_STUDIO_DESC}`):** Resolves identically to the look whose `legacy_var == "MALE_STUDIO_DESC"`.

---

## 2. Backend Implementation Blueprint

### 2.1 New Module: `work/pipeline/characters.py` (~240 lines)

Create `work/pipeline/characters.py` with the following exact functions:

1. `synthesize_default_characters(raw_project: dict, project_dir: Path) -> dict`:
   - If `raw_project.get("characters")` is non-empty, returns a deep copy of it.
   - Otherwise, inspects `raw_project.get("variables", {})` and `raw_project.get("models", {}).get("anchor_shared_settings", {}).get("image_refs", [])` to build the default `marcus` and `elena` dictionary structure shown in Section 1 (only when `MALE_STUDIO_DESC` or `FEMALE_STUDIO_DESC` is present in `variables`, or `work/ref_sheets` exists).
2. `build_tag_lookup(characters: dict, project_dir: Path) -> dict[str, dict]`:
   - Returns a case-insensitive lookup mapping every `@tag` (e.g. `"@male_studio"`), entity shorthand (`"@marcus"`), and legacy variable name (`"{male_studio_desc}"`) to a normalized descriptor dict:
     ```python
     {
         "tag": "@male_studio",
         "entity_id": "marcus",
         "look_id": "studio",
         "name": "Marcus — Studio Recording",
         "kind": "character",  # "character" | "ensemble" | "object"
         "physical_traits": "the exact 30-year-old Black male singer",
         "description": "short tapered fade haircut...",
         "text_expansion": "the exact 30-year-old Black male singer (short tapered fade haircut...)",
         "legacy_prompt_text": "the exact 30-year-old Black male singer from <image1> and <image2> (...)",
         "image_refs": [Path(...)],          # resolved Paths that exist on disk
         "rel_image_refs": ["work/ref_sheets/male_angle1_front34.jpg", ...],
     }
     ```
3. `find_prompt_entities(prompt: str, tag_lookup: dict[str, dict]) -> list[dict]`:
   - Scans `prompt` in order of appearance for `@[a-zA-Z0-9_-]+` tokens and `{[A-Z0-9_]+}` tokens that exist in `tag_lookup`.
   - Returns deduplicated list of matched descriptor dicts in order of first appearance.
4. `compile_character_prompt(prompt: str, tag_lookup: dict[str, dict], supports_refs: bool, fallback_refs: list[Path] | None = None) -> tuple[str, list[Path]]`:
   - **When `supports_refs` is False (FL2VA / LTX-2):**
     - Replaces each `@tag` or `{VAR}` with `entry["text_expansion"]`.
     - Calls `strip_dangling_ref_tags()` to clean any remaining `from <imageN>` clauses.
     - Returns `(cleaned_prompt, [])`.
   - **When `supports_refs` is True (Ref2VA #9, #10, #11, #12, #13):**
     - Finds all matched entities via `find_prompt_entities(prompt, tag_lookup)`.
     - If `@tags` are used (or `{VAR}` maps to a character with `image_refs` and no raw `<imageN>` tags are needed):
       - Allocates `<Picture 2>` .. `<Picture 10>` (max 9 images total across all matched entities; if an image path is already allocated, reuses its Picture number).
       - Replaces each `@tag` (or `{VAR}`) in the narrative body with `<Subject K> ({entry['physical_traits']}, {entry['description']})` on first mention and `<Subject K>` on subsequent mentions.
       - Builds `subject_definitions` and `retention_analysis` lines per subject:
         - If `kind == "character"`:
           - `subject_definitions`: `<Subject K> is {physical_traits} from {pics_str}, preserving facial identity, skin tone, eyes, and hair.`
           - `retention_analysis`: `<Subject K> (appears in [Shot 1]): attribute_transfer - facial features and hair follow {pics_str}; lighting and scene integration follow <Picture 1>.`
         - If `kind in ("ensemble", "object")`:
           - `subject_definitions`: `<Subject K> is {description} from {pics_str}, preserving instrument details, attire, and group staging.`
           - `retention_analysis`: `<Subject K> (appears in [Shot 1]): subject_appearance_reference - visual appearance and instruments follow {pics_str}.`
       - Checks if prompt narrative contains camera movement words (`r"\b(pan|pans|panning|zoom|zooms|zooming|track|tracks|tracking|crane|glide|gliding|sweep|sweeping)\b"`) or `len(subjects) > 2`:
         - If True: `<Picture 1> (anchors [Shot 1] at 0.00s): environment_and_style_reference - preserves soundstage lighting, color palette, and starting composition while camera moves dynamically.`
         - If False: `<Picture 1> (anchors [Shot 1] at 0.00s): fully_preserved - composition, background, camera framing, and wardrobe.`
       - Returns `(six_section_prompt, selected_paths)`.
     - If no `@tags` are present, falls back to existing `resolve_scene_refs(prompt, fallback_refs)` + `build_ref2va_prompt(prompt, ref_map)` so 100% of existing `<image1>` tests continue to pass!
5. **REST Helper Functions in `work/pipeline/characters.py`:**
   - `get_project_characters_payload(proj_dir: Path, data: dict, slug: str) -> dict`:
     - Returns `{"characters": characters_dict, "autocomplete": autocomplete_list, "scene_usage": usage_map}` where `scene_usage` maps each `entity_id` and `@tag` to the list of scene IDs (`["S01", "S02", ...]`) whose prompts reference it.
   - `save_project_characters(proj_dir: Path, cfg_file: Path, characters_data: dict, migrate_vars: bool = False) -> dict`:
     - Creates a timestamped backup of `cfg_file` in `backup/` (Critical Safety Rule 1).
     - Updates `data["characters"] = characters_data`.
     - Writes `cfg_file` atomically.
   - `save_uploaded_reference_image(proj_dir: Path, entity_id: str, look_id: str, filename: str, raw_bytes: bytes) -> str`:
     - Sanitizes `entity_id`, `look_id`, and `filename`, writes to `proj_dir / "references" / entity_id / filename`, and returns the relative path `"references/<entity_id>/<filename>"`.
   - `crop_reference_from_clip(proj_dir: Path, clip_name: str, timestamp: float, crop_box: dict | None, output_rel_path: str) -> str`:
     - Resolves `clip_name` in `work/renders_v3` or `proj_dir`, runs `ffmpeg` to extract a high-quality JPEG frame at `timestamp` (with optional `crop=w:h:x:y,scale=512:640`), saves to `proj_dir / output_rel_path`, and returns `output_rel_path`.

---

### 2.2 Minimal Hooks in `context.py` and `stage2_scenes.py`

1. In [`work/pipeline/context.py`](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/pipeline/context.py):
   - In `ProjectContext.__init__`:
     ```python
     from .characters import build_tag_lookup, compile_character_prompt, synthesize_default_characters
     self.characters = synthesize_default_characters(raw, self.project_dir)
     self.tag_lookup = build_tag_lookup(self.characters, self.project_dir)
     ```
   - In `ProjectContext.make_video_task()`:
     - If `self.tag_lookup` matches any `@tag` in `prompt`, delegate to `compile_character_prompt(expanded, self.tag_lookup, supports_refs, fallback_refs=image_refs)`.
     - Otherwise keep the existing `resolve_scene_refs` / `strip_dangling_ref_tags` path.
2. In [`work/pipeline/stage2_scenes.py`](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/pipeline/stage2_scenes.py):
   - In `compute_project_task_hashes()`:
     - When `@tags` are present in `raw_prompt`, also resolve character refs via `compile_character_prompt()` when `supports_refs` is True so `base_payload["refs"]` includes their `(p.name, _file_sha256(p))`.
     - For `valid_hashes`, if a scene uses `@male_studio` (or migrated variables), also compute a legacy fallback hash where `@tag` is replaced with `entry["legacy_prompt_text"]` so switching `{MALE_STUDIO_DESC}` $\rightarrow$ `@male_studio` preserves `UP-TO-DATE` status on FL2VA renders.

---

### 2.3 Media Streaming & REST Routes (`server_media.py` & `server_routes.py`)

1. In [`work/ui/server_media.py`](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/ui/server_media.py):
   - Add `elif category == "ref" and len(parts) >= 3:`:
     ```python
     rel_target = "/".join(parts[2:])
     cand = (proj_dir / rel_target).resolve()
     try:
         cand.relative_to(proj_dir.resolve())
         if cand.is_file():
             target_file = cand
     except ValueError:
         pass
     ```
   - Also add the same relative path check inside `category == "anchor"` before falling back to flat `Path(raw_target).name`, so any relative path like `work/ref_sheets/male_angle1_front34.jpg` or `references/nightout/male_nightout_front34.jpg` works in both `/media/<slug>/ref/...` and `/media/<slug>/anchor/...`.
2. In [`work/ui/server_routes.py`](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/work/ui/server_routes.py):
   - Add thin route delegations (<25 lines total):
     - `GET /api/project/<slug>/characters` $\rightarrow$ calls `characters.get_project_characters_payload(...)`
     - `POST /api/project/characters/save` $\rightarrow$ calls `characters.save_project_characters(...)`
     - `POST /api/project/characters/upload-image` $\rightarrow$ accepts JSON `{"slug", "entity_id", "look_id", "filename", "data_url"}` (base64 data URL from `FileReader` in browser—simpler, zero-dependency, and cleaner than multipart parsing!) and calls `characters.save_uploaded_reference_image(...)`
     - `POST /api/project/characters/crop-from-clip` $\rightarrow$ accepts JSON `{"slug", "entity_id", "look_id", "clip_name", "timestamp", "crop_box"}` and calls `characters.crop_reference_from_clip(...)`

---

## 3. Frontend Implementation Blueprint

### 3.1 HTML Changes in `work/ui/static/index.html`

1. **Top Navigation Bar (lines 18–25):**
   ```html
   <div class="nav-tabs">
     <button class="tab-btn active" data-tab="timeline">🎬 Timeline</button>
     <button class="tab-btn" data-tab="characters">👤 Characters & Refs</button>
     <button class="tab-btn" data-tab="anchors">🖼️ Scene Anchors</button>
     <button class="tab-btn" data-tab="settings">⚙️ Project & Models</button>
     <button class="tab-btn" data-tab="console">🚀 Render Console</button>
     <button class="tab-btn" data-tab="master">📺 Master Video</button>
   </div>
   ```
2. **Attached References Containers in Scene Inspector:**
   - Immediately below `#scenePrompt` (in `#singlePromptGroup`), add:
     `<div id="refTraySingle" class="ref-chip-tray" style="margin-top: 6px;"></div>`
   - Immediately below `#scenePromptM` and `#scenePromptF` (in `#duetPromptGroup`), add:
     `<div id="refTrayPass1" class="ref-chip-tray" style="margin-top: 6px;"></div>`
     `<div id="refTrayPass2" class="ref-chip-tray" style="margin-top: 6px;"></div>`
3. **Characters Studio Tab Pane (`#tab-characters`):**
   - Inside `#sidePaneContent` (right above `#tab-anchors`), add:
     ```html
     <div id="tab-characters" class="tab-pane" style="padding: 16px;">
       <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
         <div>
           <h2 style="font-size: 18px; color: var(--text-main);">Characters & Reference Library</h2>
           <p style="font-size: 12px; color: var(--text-muted);">Manage character identities, wardrobe looks, and multi-subject reference sheets for @mention prompt injection.</p>
         </div>
         <div style="display: flex; gap: 8px;">
           <button class="btn" id="btnNewCharacter">+ New Character / Group</button>
           <button class="btn btn-primary" id="btnSaveCharacters">💾 Save Characters</button>
         </div>
       </div>
       <div id="charactersStudioLayout" style="display: grid; grid-template-columns: 260px 1fr; gap: 16px; align-items: start;">
         <div id="characterRosterList" style="display: flex; flex-direction: column; gap: 8px;"></div>
         <div id="characterDetailPane" style="background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border-color); border-radius: 8px; padding: 16px;"></div>
       </div>
     </div>
     ```
4. **Script Tag at Bottom of `index.html`:**
   - Add `<script src="js/characters_studio.js?v=30"></script>` right before `</body>` (and add `"js/characters_studio.js"` to the fallback array in `work/ui/static/js/app.js`).

---

### 3.2 New Frontend Module: `work/ui/static/js/characters_studio.js` (~350 lines)

Implement the following self-contained components in `characters_studio.js`:

1. **State & Initialization (`loadProjectCharacters()`):**
   - Fetches `GET /api/project/${state.currentProjectSlug}/characters` whenever a project loads (hooked via `window.onProjectLoadedForCharacters` or polling `state.projectData`).
   - Stores `state.charactersData`, `state.characterAutocomplete`, and `state.characterSceneUsage`.
2. **Master-Detail Character Studio (`renderCharactersTab()`):**
   - **Left Roster (`#characterRosterList`):**
     - Renders cards for each entity (`marcus`, `elena`, `orchestra`, etc.) showing avatar thumbnail (`/media/${slug}/ref/${avatar}`), name, kind badge (`Character`, `Ensemble`, `Object`), default `@tag`, look count, and scene usage count.
   - **Right Detail Editor (`#characterDetailPane`):**
     - **Header:** Editable Name, ID, Kind selector (`character` | `ensemble` | `object`), Default Look selector, and Delete button.
     - **Base Physical Traits:** Text input for permanent traits (`physical_traits`).
     - **Looks / Variants Section (`+ Add Look`):**
       - For each look (`studio`, `nightout`, `pianist`, `strings`, etc.):
         - Editable `@tag` (e.g. `@male_studio`), Look Name, and Wardrobe/Detail Description.
         - **Reference Image Strip:** Displays thumbnail cards for each path in `look.image_refs` with a remove `(×)` button, plus:
           - **`+ Upload Image` button:** Opens a hidden `<input type="file" accept="image/*">`, reads via `FileReader.readAsDataURL()`, posts to `/api/project/characters/upload-image`, and appends the returned path to `look.image_refs`.
           - **`📸 Crop from Clip` button:** Opens a compact modal listing rendered project clips (`state.projectAssets.clips`), a `<video>` preview player allowing the user to scrub to any frame, and a "Capture Reference Frame" button that calls `/api/project/characters/crop-from-clip` and appends the resulting image to `look.image_refs`.
     - **Timeline Scene Casting Pills:**
       - Lists all scenes referencing this character/entity as clickable pills (`S01`, `S02`, `S18`). Clicking a pill calls `selectScene(idx)` so the timeline jumps straight to that scene.
3. **`@mention` Caret Autocomplete Popover (`setupPromptAutocomplete()`):**
   - Attaches `input` and `keydown` listeners to `#scenePrompt`, `#scenePromptM`, and `#scenePromptF`.
   - When the text before `textarea.selectionStart` ends with `@([a-zA-Z0-9_-]*)$`:
     - Filters `state.characterAutocomplete` (which includes all `@look_tags`, `@entity_shorthands`, and `@A01..A14` anchors) by the typed prefix.
     - Renders a floating `#promptMentionPopover` menu with thumbnail, `@tag`, name, and kind badge.
     - `ArrowDown` / `ArrowUp` cycles active item; `Enter` or `Tab` replaces the `@prefix` at the cursor with `@tag ` and triggers `input` event; `Escape` closes the popover.
4. **Reactive "Attached References" Chip Tray (`window.updatePromptReferenceTray(sc)`):**
   - Called automatically whenever `#scenePrompt`, `#scenePromptM`, `#scenePromptF`, or `#sceneVideoRenderer` changes, and inside `selectScene()`.
   - Parses `@tags` and `{LEGACY_VARS}` live from the prompt text.
   - Checks if the scene's active video renderer (resolved from `sc.renderer` or `state.projectData.models.default_video_renderer`) supports image refs (`supports_image_refs === true`).
   - **If Ref2VA is active:**
     - Shows green badge: `✓ Multimodal Ref2VA Mode (${totalImages}/9 Image Slots)`
     - Renders chips with miniature thumbnails for each matched `@tag` / `{VAR}` showing assigned `<Picture N>` slots and a `(×)` button to remove the tag from the prompt.
     - If `totalImages > 9`, shows an amber warning badge: `⚠️ ${totalImages}/9 slots used (only first 9 sent to engine)`.
     - If `matchedEntities.length > 3` and `(sc.end - sc.start) < 8.0`, shows a subtle pacing tip: `💡 Tip: ${matchedEntities.length} subjects in ${(sc.end - sc.start).toFixed(1)}s — use <marker> cues to sequence camera pans cleanly.`
   - **If FL2VA / Text-Only Model is active:**
     - Shows info badge: `ℹ Text-Only Model (Ref Images Bypassed — Expands to Text Description)`
     - Renders chips showing `[TXT] @tag` with a tooltip displaying the expanded natural language description.

---

## 4. Verification & Testing Checklist

Add `test_55_character_studio_and_multi_reference_compilation` in [`tests/test_backend_queue.py`](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/tests/test_backend_queue.py) verifying:
1. **Default Synthesis & Zero-Invalidation:** Loading `all_or_nothing_v4.json` synthesizes `marcus` and `elena` character objects, and all 37 existing passes in `all_or_nothing` retain their exact task hashes (0 unintended invalidations).
2. **Dual-Mode Prompt Compilation (`@male_studio`):**
   - On Model 1 (FL2VA): `@male_studio` expands to clean natural language description with `image_refs=None` and zero `<imageN>` or `<Picture N>` tags in the character description.
   - On Model 12 (Ref2VA): `@male_studio` maps its 2 studio images to `<Picture 2>` and `<Picture 3>` and builds `<Subject 1>` with `attribute_transfer`.
3. **Multi-Subject Ensemble Compilation (S10 Orchestra):**
   - A prompt with `@strings`, `@pianist`, `@brass`, `@woodwinds`, and `@female_studio` on Model 12 resolves all 5 subjects into `<Subject 1>`..`<Subject 5>`, uses `subject_appearance_reference` for the orchestra ensembles and `attribute_transfer` for `@female_studio`, and sets `<Picture 1>` to `environment_and_style_reference` because the prompt contains camera pan/zoom motion.
4. **REST Endpoints & Media Streaming:**
   - Verify `GET /api/project/test_project/characters`, `POST /api/project/characters/save`, `POST /api/project/characters/upload-image`, and `GET /media/test_project/ref/...` return `200 OK`.
5. **Module Line Counts:**
   - Confirm `work/pipeline/characters.py`, `work/pipeline/prompts.py`, `work/pipeline/context.py`, `work/ui/server_routes.py`, and `work/ui/static/js/characters_studio.js` are all strictly `< 850` lines.
