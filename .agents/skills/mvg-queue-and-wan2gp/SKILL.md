---
name: mvg-queue-and-wan2gp
description: Queue diagnostics, multi-engine scheduling (Local GPU / fal.ai / MiniMax), Wan2GP integration rules, CUDA OOM auto-retry, state hashes validation, and cancellation recovery.
---

# MVG Queue Engine & Wan2GP Integration

Use this skill to debug render queue states, understand multi-engine scheduling, inspect `failed` / `CUDA OOM` items, validate `model_profiles.json`, or trace cancellation recovery.

---

## 1. Fast Diagnostics (`diagnose_queue_state.py`)

Reads `work/jobs` and per-backend `engine_status_{local,fal,minimax}*.json` files:

```bash
python .agents/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py <slug>
python .agents/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py <slug> --json
```

---

## 2. Queue Evaluation & Scheduling Rules

`work/ui/server_queue.py` + `work/pipeline/stage2_hashes.py` evaluate each pass in priority order:

1. **Active Render**: Matching pass running in any backend slot (`local`, `fal`, `minimax`) stays `rendering`.
2. **User Curation**: `FORCED` / `TARGETED` queued items and unchanged `failed` items (`status == "failed"`) remain preserved across soft queue refreshes so failed items stay visible in the **Pending** filter (`queued || rendering || failed`) with their failure reason (`CUDA OOM` / `FAILED (code N)`) and tooltip error.
3. **Missing File / Two-Pass Dependency**: Missing MP4 (`>50 KB`) or Pass 1 dirty/newer triggers `queued` (`PASS 2` compares `takes_manifest` `created_at` & `parent_p1_task_hash` to avoid false requeues).
4. **Timeline Coverage & Anchors / Refs**: Timeline exceeding `render_start..render_end` -> `TIMELINE CHANGED`; anchor SHA-256 / mtime changes (with vault `anchor_sha256` equality check) -> `ANCHOR <ID> EDITED`; ref images newer -> `REF ... NEWER ON DISK`.
5. **Hash Diff + Vault Auto-Promotion**: `try_auto_promote_vault_hit` promotes matching historical takes in `<50ms` (`UP-TO-DATE (VAULT HIT)`); otherwise marked `queued`.

### Multi-Engine Execution & OOM Recovery
- `enabled_engines` defaults safely to `{"local": True, "fal": False, "minimax": False}` so cloud spending never triggers unintentionally.
- When **▶ Run** starts, `requeue_failed_items` resets failed items on currently enabled engines back to `queued`.
- Local CUDA OOM crashes trigger `execute_slot_task_with_oom_retry` (`local_oom_retries = 1`) in a fresh subprocess, resetting `started_at` on retry; if retry still fails, `read_task_failure_summary` captures `"CUDA OOM"` and error text on the queue item and pauses only that engine.
- `is_status_valid_for_slot` allows up to `14400s` (4h) between step updates on heavy local renders so `1920x1088` denoising doesn't silently expire.

---

## 3. Cancellation & Unlinked Clip Recovery

Before Stage 2 overwrites a clip, `before_scene_render` hardlinks the old take in `takes/` and unlinks the working file. On cancellation (`cancel_request.json`) or non-zero exit, `_restore_if_unlinked` / `restore_previous_active_take_if_missing` restores the prior active take so no slot vanishes from disk.

---

## 4. Validate Model Profiles (`validate_wgp_profile.py`)

```bash
python .agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py
```

Verifies multiple-of-16 resolutions (`video_resolutions`), required fields across `anchor_renderers` / `video_renderers`, and cross-checks cloud sections against `fal_catalog.py`.

---

## 5. Offline Wan2GP Render Forensics & Validation

When diagnosing glitchy, mismatched, or seaming takes without touching the live GPU:

1. **Inspect Embedded MP4 Render Settings (`read_embedded_render_settings` in `work/pipeline/media_utils.py`)**:
   Wan2GP writes the actual task parameters into each rendered MP4's `format_tags.comment` JSON (`model_type`, `video_prompt_type`, `denoising_strength`, `masking_strength`, `guidance_phases`, `num_inference_steps`). Check this first to confirm which model profile and mask mode produced a clip.
2. **Verify Spatial Seam & Mask Coordinates Quantitatively**:
   - Inspect raw grayscale values of `work/jobs/.../mask_*.mp4` at `round(split_x * width)` to confirm the exact hard mask boundary.
   - Compare Pass 1 vs. Pass 2 frames at identical timestamps across horizontal `x`-buckets; a ramp across `~10%` of frame width (`~180px` at `1920px`) left of `split_x` is characteristic of LTX-2 `MVGA`'s `_apply_ltx2_mask_blend` (`LTX2_INPAINTING_LAPLACIAN_MASK_LOW_RES_DILATION = 6` at `long_side = 64`), combined with `#66FF00` erasure of masked guide frames after Frame 0.
3. **CPU-Only `wgp.validate_task` Probing (`BypassSandbox: true`, zero GPU inference)**:
   To test candidate `video_prompt_type` / `masking_strength` / `denoising_strength` combinations before editing `model_profiles.json`, run Wan2GP's Python (`BypassSandbox: true`) against `wgp.validate_task` and verify installed LoRA weights in Wan2GP's `loras/` directory (e.g., whether an `in-outpainting` or `union-control` IC-LoRA actually exists on disk).
