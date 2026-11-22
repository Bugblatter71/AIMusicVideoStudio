---
name: mvg-project-ops
description: CLI tools and inspection workflows for Music Video Generation project JSONs, scene timeline validations, audio stems, anchor definitions, and safe backups.
---

# MVG Project Operations & Validation

Use this skill whenever you need to inspect, audit, or safely modify a Studio project configuration (`projects/<slug>/<slug>[_vN].json`).

> [!IMPORTANT]
> Never edit `projects/all_or_nothing/all_or_nothing_v4.json` directly without creating a timestamped backup in `backup/`. Prefer `project_tool.py`, which backs up automatically.

---

## 1. Fast CLI Commands (`project_tool.py`)

All commands automatically resolve the latest versioned JSON file in `projects/<slug>/` and support both new (`work/renders`, `work/jobs`) and legacy (`work/renders_v3`, `work/jobs_v3`) layouts:

```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py list
python .agents/skills/mvg-project-ops/scripts/project_tool.py summary <slug>
python .agents/skills/mvg-project-ops/scripts/project_tool.py audit <slug>
python .agents/skills/mvg-project-ops/scripts/project_tool.py show-scene <slug> <scene_id> [--json]
```

### Safely Edit Scene Properties (`set-scene`)
Automatically creates a backup in `backup/` and recalculates the queue cache:
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py set-scene <slug> S01 --start 0.0 --end 8.5 --mode solo
```

Supported flags:
- Timing / padding: `--start`, `--end`, `--pad-start`, `--pad-end`, `--hold-last-frame`
- Mode / renderer / backend: `--mode`, `--renderer`, `--backend`, `--backend-f`, `--pass2-roi-mode`
- Stems / anchors / seeds: `--stem`, `--stem-m`, `--stem-f`, `--anchor`, `--anchor-end`, `--seed-m`, `--seed-f`, `--split-x`
- Prompts: `--prompt`, `--prompt-m`, `--prompt-f`

### Split or Delete Scenes (Preserving Zero-Gap Continuity)
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py split-scene <slug> S02 12.5 [--id1 S02a --id2 S02b]
python .agents/skills/mvg-project-ops/scripts/project_tool.py delete-scene <slug> S02 [--no-close-gap]
```

---

## 2. Pipeline CLI Inspection & Assembly (`work/run_overnight_v3.py`)

```bash
python work/run_overnight_v3.py <slug> --list        # Prints model catalog, resolutions, anchors & scenes
python work/run_overnight_v3.py <slug> --sync-hashes # Syncs state_hashes.json baseline (without rendering)
python work/run_overnight_v3.py <slug> stitch        # Assembles Stage 3 master video from existing renders
python work/run_overnight_v3.py <slug> import-clip <scene_id> <video.mp4> [--label "..."] [--pass 1|2] [--no-normalize]
```
*(In the Web UI Scene Inspector, use **`📥 Import Clip`** on `#sceneClipPreviewPane` to upload an external Google Flow / Veo / pre-rendered MP4 directly into Render Vault and mark `state_hashes.json` current.)*
