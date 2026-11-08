# MVG State & File Contracts Reference

This document defines the schema, contracts, and lifecycle of the core JSON data structures used across the Music Video Generation Studio.

---

## 1. Project Configuration JSON (`projects/<slug>/<slug>.json`)

The primary declaration of a music video project.

```json
{
  "project": {
    "name": "Project Display Name",
    "slug": "project_slug",
    "total_duration": 180.5,
    "fps": 24,
    "master_resolution": "1344x768",
    "audio": "audio/song.mp3",
    "directories": {
      "anchors": "work/anchors_v3",
      "renders": "work/renders_v3",
      "trimmed": "work/trimmed_v3",
      "jobs": "work/jobs_v3"
    }
  },
  "models": {
    "default_model": "minimax_h3_fl2va_pruned_pdd",
    "default_video_resolution": "4",
    "video_shared_settings": {
      "cfg_scale": 5.0,
      "steps": 25,
      "seed": 42
    }
  },
  "anchors": [
    {
      "id": "A01",
      "name": "Lead Vocalist",
      "prompt": "Cinematic portrait of...",
      "reference_image": "references/portrait.jpg",
      "model": "flux_dev"
    }
  ],
  "scenes": [
    {
      "id": "S01",
      "title": "Intro Scene",
      "start": 0.0,
      "end": 8.5,
      "mode": "male_solo",
      "anchor": "A01",
      "stem": "male.mp3",
      "prompt": "Vocalist singing intensely, neon backlight",
      "output_name": "S01_male_solo"
    },
    {
      "id": "S02",
      "title": "Duet Scene",
      "start": 8.5,
      "end": 18.0,
      "mode": "gva_male_left_silent_right",
      "anchor": "A01",
      "anchor_female": "A02",
      "stem_m": "male.mp3",
      "stem_f": "female.mp3",
      "prompt_pass1": "Vocalist on left half, empty room right",
      "prompt_pass2": "Female vocalist singing on right half",
      "output_name": "S02_duet"
    }
  ],
  "markers": [
    {
      "id": "m1",
      "name": "Chorus Drop",
      "time": 45.2,
      "color": "#f59e0b"
    }
  ]
}
```

---

## 2. Queue State JSON (`work/jobs_v3/v3_queue.json`)

The live render queue evaluated by `server_queue.py` and displayed in the UI console.

```json
[
  {
    "idx": 1,
    "id": "S01_male_solo_1344x768",
    "scene_id": "S01",
    "target_arg": "S01",
    "pass": null,
    "filename": "S01_male_solo_1344x768.mp4",
    "output_filename": "S01_male_solo_1344x768",
    "status": "completed",
    "reason": "UP-TO-DATE",
    "needs_render": false,
    "task_hash": "a1b2c3d4e5f67890",
    "anchor_mtime": 1731000000.0,
    "model_type": "minimax_h3_fl2va_pruned_pdd",
    "resolution": "1344x768"
  },
  {
    "idx": 2,
    "id": "S02_Pass1_Left_1344x768",
    "scene_id": "S02",
    "target_arg": "S02:p1",
    "pass": 1,
    "filename": "S02_Pass1_Left_1344x768.mp4",
    "output_filename": "S02_Pass1_Left_1344x768",
    "status": "queued",
    "reason": "FORCED",
    "needs_render": true,
    "task_hash": "f6e5d4c3b2a10987",
    "anchor_mtime": 1731000100.0,
    "model_type": "minimax_h3_fl2va_pruned_pdd",
    "resolution": "1344x768"
  }
]
```

### Status Codes
- `completed`: Valid MP4 exists on disk and task hashes match.
- `queued`: Missing from disk or flagged as modified (`needs_render = True`).
- `rendering`: Currently in progress by active Wan2GP subprocess.
- `skipped`: Explicitly skipped by user (`reason = "SKIPPED BY USER"`). Holds `user_skipped_hash` and `user_skipped_anchor_mtime` to remain skipped until user edits that scene.
- `failed`: Non-zero exit code or engine generation error.

---

## 3. State Hashes Cache (`work/jobs_v3/state_hashes.json`)

Persistent record of generated tasks, anchor file hashes, and time bounds.

```json
{
  "tasks": {
    "S01_male_solo_1344x768": "a1b2c3d4e5f67890",
    "S02_Pass1_Left_1344x768": "f6e5d4c3b2a10987"
  },
  "anchor_file_hashes": {
    "A01": "e3b0c44298fc1c14",
    "A02": "4b227777d4dd1fc6"
  },
  "scene_renders": {
    "S01_male_solo_1344x768": {
      "render_start": 0.0,
      "render_end": 8.5
    }
  }
}
```

---

## 4. Engine Status JSON (`work/jobs_v3/engine_status.json`)

Written by `wgp_bridge.py` during render jobs and polled by the UI via `/api/render/status` and SSE.

```json
{
  "state": "running",
  "phase": "inference",
  "raw_phase": "Denoising",
  "step": 14,
  "total_steps": 25,
  "pct": 56.0,
  "overall_pct": 28.0,
  "window_index": 1,
  "window_count": 2,
  "filename": "S01_male_solo_1344x768",
  "updated_at": 1731000500.123
}
```

### Valid Phases
- `loading_model`: Model weights loading into GPU VRAM.
- `encoding_audio`: Extracting audio features / spectrograms.
- `encoding_anchor`: Processing character anchor portrait.
- `inference`: Sampling/denoising steps (contains `step` / `total_steps`).
- `saving`: Writing and encoding MP4 to disk.
- `completed`: Successfully finished.
- `cancelled`: Cancelled via `cancel_request.json`.
- `failed`: Terminated with an error.
