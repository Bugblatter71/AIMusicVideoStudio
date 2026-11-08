---
name: mvg-project-ops
description: CLI tools and inspection workflows for Music Video Generation project JSONs, scene timeline validations, audio stems, and anchor definitions.
---

# MVG Project Operations & Validation

Use this skill whenever you need to inspect, audit, or safely modify a Music Video Generation project configuration (`projects/<slug>/<slug>.json`).

---

## 1. Fast CLI Commands (`project_tool.py`)

All commands automatically locate the latest versioned JSON file for the project (e.g. `all_or_nothing_v4.json`).

### 1.1 List All Projects
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py list
```

### 1.2 Summary of a Project
Prints high-level stats: duration, FPS, resolution, anchor count, scene count, marker count, models:
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py summary all_or_nothing
```

### 1.3 Full Project Audit
Performs a deep integrity check:
- Timeline bounds continuity (detects any gaps or overlapping scenes)
- Missing audio stem files on disk
- Missing anchor reference portraits
- Unrendered scenes or scenes whose timeline bounds exceed rendered video bounds
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py audit all_or_nothing
```

### 1.4 Inspect a Single Scene
Outputs the complete scene JSON block with relative cue markers expanded:
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py show-scene all_or_nothing S01
```

### 1.5 Safely Edit Scene Properties
Edits a scene's properties with an automated pre-modification timestamped backup to `backup/`:
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py set-scene all_or_nothing S01 --start 0.0 --end 8.5 --mode male_solo
```
Flags available for `set-scene`:
- `--start <seconds>`
- `--end <seconds>`
- `--mode <mode>` (`male_solo`, `female_solo`, `gva_male_left_silent_right`, `duet_method_b`, `silent_locked`)
- `--prompt <text>`
- `--anchor <anchor_id>` (e.g. `A01`)
- `--stem <stem_file>` (e.g. `male.mp3`)

### 1.6 Split a Scene into Two
Splits an existing scene into two sequential sub-scenes at a given timeline timestamp with an automated pre-modification timestamped backup to `backup/`:
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py split-scene all_or_nothing S02 12.5
python .agents/skills/mvg-project-ops/scripts/project_tool.py split-scene all_or_nothing S02 12.5 --id1 S02a --id2 S02b
```

### 1.7 Delete a Scene
Deletes a scene by ID, automatically closing timeline gaps (or optionally preserving them) with an automated pre-modification timestamped backup to `backup/`:
```bash
python .agents/skills/mvg-project-ops/scripts/project_tool.py delete-scene all_or_nothing S02
python .agents/skills/mvg-project-ops/scripts/project_tool.py delete-scene all_or_nothing S02 --no-close-gap
```
