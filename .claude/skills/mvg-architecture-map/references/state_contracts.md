# MVG State & HTTP API Contracts

Reference schemas so agents do not need to dump multi-megabyte project state files or read full HTTP handlers.

---

## 1. Project Configuration (`projects/<slug>/<slug>[_vN].json`)
Latest sorted non-`state_hashes*.json` JSON inside `projects/<slug>/` is loaded by `ProjectContext` and `find_latest_project_json`.

```json
{
  "project": {
    "name": "Song Title",
    "project_dir": ".",
    "master_audio": "audio/master_audio.mp3",
    "stems": { "lead_vocal": "audio/lead_vocal.mp3", "male": "audio/male.mp3", "female": "audio/female.mp3" },
    "master_output": "Slug_Master_{resolution}_{fps}fps.mp4",
    "master_resolution": "",
    "fps": 24,
    "total_duration": 180.0,
    "trim_first_frame_seconds": 0.04,
    "fade_out_last_scene_seconds": 2.0,
    "directories": {
      "anchors": "work/anchors",
      "audio_slices": "work/audio",
      "masks": "work/masks",
      "jobs": "work/jobs",
      "renders": "work/renders",
      "trimmed": "work/trimmed",
      "review_strips": "work/frames"
    }
  },
  "profiles_file": "model_profiles.json",
  "models": {
    "default_anchor_renderer": 3,
    "default_video_renderer": 1,
    "default_video_resolution": 4,
    "default_backend": "local",
    "anchor_shared_settings": { "image_refs": [] },
    "video_shared_settings": { "negative_prompt": "..." }
  },
  "variables": { "CHAR_DESC": "..." },
  "characters": { "<char_id>": { "id": "...", "name": "...", "kind": "character", "default_look": "default", "looks": { "<look_id>": { "tag": "@tag", "description": "...", "image_refs": [] } } } },
  "markers": [ { "id": "M01", "name": "Chorus Drop", "time": 42.5, "color": "#f59e0b" } ],
  "anchors": [ { "filename": "A01_Shot.jpg", "renderer": null, "seed": 8101, "prompt": "..." } ],
  "scenes": [
    {
      "id": "S01",
      "start": 0.0,
      "end": 6.0,
      "mode": "solo",
      "stem": "lead_vocal",
      "anchor": "A01_Shot.jpg",
      "seed": 9101,
      "renderer": null,
      "backend": null,
      "prompt": "..."
    }
  ]
}
```

### Supported Scene Modes
- Solo / Silent: `solo`, `male_solo`, `female_solo`, `silent`, `silent_locked`, `ambient`, `duet_method_a` (`duet_a`, single-pass stereo duet).
- Two-Pass Duet: `duet_method_b`, `gva_male_left_silent_right`, `duet`, `two_pass_gva` (generates `_Pass1_Left_<res>.mp4` then `_Pass2_Final_<res>.mp4`; supports `stem_m`, `stem_f`, `seed_m`, `seed_f`, `prompt_m`, `prompt_f`, `split_x`, `backend`, `backend_f`, `pass2_roi_mode` = `full | half_split | tight_performer | custom`).

---

## 2. Queue & State Files (`work/jobs[_v3]/` and `work/`)
- **`v3_queue.json`**: Array of pass objects with `idx`, `id`, `scene_id`, `pass` (`null | 1 | 2`), `filename`, `output_filename`, `status` (`queued | rendering | completed | failed | skipped`), `needs_render`, `reason`, `error`, `task_hash`, `anchor_mtime`, `model_type`, `resolution`, `frames`, `backend`, `backend_override`, `resolved_default_backend`, `estimated_seconds`, `estimated_str`, `estimated_cost_usd`, `cost_str`.
- **`state_hashes.json`**: `{ "anchors": {aid: hash}, "tasks": {out_name: hash}, "scene_renders": {out_name: {render_start, render_end, frames, elapsed, completed_at}}, "backend_history": {...} }`. Written atomically with file locks; never write partial task hashes in parallel without reload-merge.
- **`takes_manifest.json`**: `{ "version": 1, "scenes": { "<SID>": { "active_take_id": "...", "active_takes": {"p1": "...", "p2": "...", "default": "..."}, "takes": [...] } } }`.
- **`engine_status_{local,fal,minimax}[__<out_stem>].json`**: Live engine & per-slot progress `{state, backend, filename, output_filename, phase, raw_phase, step, total_steps, pct, window_pct, started_at, updated_at, error}`.
- **`cancel_request.json`**: Checked cooperatively by `wgp_bridge.py` and cloud polling loops.

---

## 3. Core Studio REST Endpoints (`work/ui/server_routes.py` & `server_fal.py`)
- `GET /api/projects`, `GET /api/project/<slug>`, `GET /api/project/<slug>/queue`, `GET /api/project/<slug>/characters`, `GET /api/models`
- `GET /api/takes/<slug>`, `GET /api/checkpoints/<slug>`, `GET /api/vault/stats/<slug>`
- `GET /api/render/status`, `GET /api/render/events` (SSE), `GET /api/fal/status`, `GET /api/minimax/status`
- `POST /api/projects/new`, `POST /api/project/<slug>` (save config)
- `POST /api/render/start`, `POST /api/render/stop`, `POST /api/render/engines` (`enabled_engines`, `max_parallel`), `POST /api/render/anchors`, `POST /api/render/stitch`
- `POST /api/queue/move`, `POST /api/queue/reorder`, `POST /api/queue/remove`, `POST /api/queue/enqueue`, `POST /api/queue/enqueue_all`, `POST /api/queue/clear`, `POST /api/queue/set_engine`
- `POST /api/takes/activate`, `POST /api/takes/delete`, `POST /api/checkpoints/create`, `POST /api/checkpoints/rollback`, `POST /api/scene/repair_lipsync`, `POST /api/scene/upscale`
