---
name: mvg-queue-and-wan2gp
description: Queue diagnostics, Wan2GP engine integration rules, state hashes validation, and cancellation recovery.
---

# MVG Queue Engine & Wan2GP Integration

Use this skill whenever you need to debug render queue states, understand why passes are marked queued or completed, validate Wan2GP model profile payloads, or troubleshoot cancellation recovery.

---

## 1. Fast Diagnostics Tool (`diagnose_queue_state.py`)

Diagnose why items in `v3_queue.json` are queued vs skipped without manual hash inspection:

```bash
# Diagnose queue items, hash diffs, and engine status for a project:
python .agents/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py all_or_nothing

# Output as JSON for automated parsing:
python .agents/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py all_or_nothing --json
```

---

## 2. Queue Evaluation Rules

`server_queue.py` and `work/pipeline/stage2_scenes.py` evaluate tasks using the following priority order:

1. **Active Engine Render**: If a Wan2GP process is currently running and generating a task, its status is preserved as `rendering`.
2. **User Curation**:
   - `FORCED`: Always renders regardless of existing files.
   - `SKIPPED BY USER`: User manually deleted the item from the queue. Holds `user_skipped_hash` and `user_skipped_anchor_mtime` to remain skipped until the user edits that scene.
3. **Missing File**: If rendered `.mp4` does not exist on disk (>50 KB), flagged as `queued` (`MISSING FILE`).
4. **Two-Pass Dependency**: For two-pass GVA / duet scenes, Pass 2 depends on Pass 1. If Pass 1 needs re-render or Pass 1 MP4 is newer on disk than Pass 2 MP4, Pass 2 is marked `queued`.
5. **Timeline Bounds Coverage**: If timeline was expanded beyond previously rendered bounds (`render_start <= scene_start + 0.05` and `render_end >= scene_end - 0.05`), flagged as `queued` (`TIMELINE EXCEEDS RENDER`).
6. **Anchor Modifications**: If anchor image file modification time is newer than rendered video or SHA-256 hash differs from `state_hashes.json`, flagged as `queued` (`ANCHOR EDITED`).
7. **Prompt / Stem / Settings Changes**: If SHA-256 task hash differs from `state_hashes.json`, flagged as `queued` (`PROMPT/SEED/SETTINGS EDITED`).
8. **Up to Date**: All conditions met -> `completed` (`UP-TO-DATE`).

---

## 3. Wan2GP Engine Profile Validation (`validate_wgp_profile.py`)

Validate all profiles in `model_profiles.json` against Wan2GP's expected parameters:

```bash
python .agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py
```

Checks:
- `audio_prompt_type` compatibility (`"AS"` for MiniMax Ref2VA / GVA, `"AP"` for text-only audio prompts).
- Valid macroblock dimension constraints (multiples of 16 for Flux/Wan).
- Guidance scale, sampling steps, and CFG scale range validity.
