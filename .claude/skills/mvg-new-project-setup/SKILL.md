---
name: mvg-new-project-setup
description: End-to-end onboarding workflow for setting up a brand new music video project—scanning audio & vocal stems, mapping vocal phrases to a zero-gap scene timeline, defining characters/variables/anchors, and validating assets before rendering.
---

# MVG New Project Setup Workflow

Use this skill whenever the user wants to start a new music video project, analyze new song audio/stems, design the scene timeline, or set up anchors, characters, and assets.

---

## Step 1: Initialize Project Workspace

Create the project directory under `projects/<slug>/`:

```bash
python work/run_overnight_v3.py --new <slug>
```

Expected layout created inside `projects/<slug>/`:
- `<slug>.json` (initialized from `project_template.json`)
- `audio/` (place `master_audio.*` and isolated stems like `lead_vocal.*`, `backing_track.*`, or `male.*` / `female.*`)
- `references/` (character identity reference sheets / location plates)
- `work/` (`anchors/`, `audio/`, `jobs/`, `masks/`, `renders/`, `trimmed/`, `frames/`)

---

## Step 2: Inspect & Analyze Song Audio + Stems

Scan audio metadata first, then detect vocal activity windows & natural cut points across `master_audio` and vocal stems:

```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/<slug>/audio --scan-audio
python .agents/skills/mvg-new-project-setup/scripts/analyze_audio_sections.py projects/<slug>/audio --step 0.5
python .agents/skills/mvg-new-project-setup/scripts/analyze_audio_sections.py projects/<slug>/audio --json
```

Use the output to:
- Set exact `project.total_duration` from `master_audio`.
- Map singing phrases vs instrumental breaks (`solo` / `duet_method_b` when vocal stems are active vs `silent` / `silent_locked` for scenic/instrumental shots).
- Align scene cuts to phrase boundaries (typical scene length `4s–10s`; keep scenes under ~15s where possible to match Wan2GP / MiniMax native 15s single-window limits).

---

## Step 3: Build & Validate the Storyboard (`characters`, `anchors`, `scenes`)

1. Generate a starter storyboard spec or author a spec JSON with `project`, `models`, `variables`, `characters`, `markers`, `anchors`, and zero-gap `scenes` (use `mvg-prompt-and-asset-craft` for prompt & `@tag` structure):
   ```bash
   python .agents/skills/mvg-new-project-setup/scripts/scaffold_project.py <slug> --export-starter projects/<slug>/storyboard_spec.json
   ```
2. Apply the storyboard spec cleanly (automatically backs up to `backup/` and validates zero-gap timing, stem files, anchor references, valid modes, and unique seeds):
   ```bash
   python .agents/skills/mvg-new-project-setup/scripts/scaffold_project.py <slug> --spec projects/<slug>/storyboard_spec.json
   ```
   *(If updating a live config iteratively, keep per-scene tweaks safe with `project_tool.py set-scene` / `split-scene` / `delete-scene`.)*

---

## Step 4: Prepare Reference Sheets & Stage 1 Anchors

1. **Character References**: Drop front/3/4/profile portraits in `projects/<slug>/references/` (or generate with `image-generator` subagent / Fal / Wan2GP / Google Flow Nano Banana), and wire paths into `characters.<id>.looks.<look>.image_refs` and `models.anchor_shared_settings.image_refs`.
2. **Anchor Portraits (`A01..Ann`)**: Place existing 16:9 stills in `work/anchors/` OR render anchors in Studio (`default_anchor_renderer: 3` or Fal `fal_qwen_edit_2511` / `fal_flux2_klein_9b`).
   - Run `python work/run_overnight_v3.py <slug> --anchors-only` (or render individually in Anchor Studio UI).
3. **External Video Clips (e.g., Google Flow / Veo)**: Generate external MP4 clips from an anchor and import them directly into any scene as a Render Vault take via UI **`📥 Import Clip`** (`POST /api/takes/import`) or `python work/run_overnight_v3.py <slug> import-clip <scene_id> <clip.mp4>`.

---

## Step 5: Audit & Smoke-Test Before Real GPU Renders

```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py summary <slug>
python .agents/skills/mvg-project-ops/scripts/project_tool.py audit <slug>
python work/run_overnight_v3.py <slug> --list
python work/run_overnight_v3.py <slug> --dummy   # Optional <10s FFmpeg mock timeline/stitch check
```

Recommend starting draft motion/lip-sync checks at `832x480` (`default_video_resolution: 1`) or `960x544` (`2`) before switching up to `1344x768` (`4`) / `1536x864` (`5`) for production.
