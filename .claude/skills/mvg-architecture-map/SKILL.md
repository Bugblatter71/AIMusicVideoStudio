---
name: mvg-architecture-map
description: Fast architectural overview of the Music Video Generation Studio codebase, mapping core pipelines, submodules, server endpoints, and frontend scripts to prevent large whole-file context loading.
---

# MVG Architecture Map & Code Navigator

Use this skill whenever you need to locate, understand, or modify functionality in the Music Video Generation Studio codebase.

Instead of scanning whole files (which consumes thousands of context tokens), use this architectural reference and the included `symbol_index.py` tool to immediately pinpoint the exact file and line ranges you need.

---

## 1. Directory & Module Map

### 1.1 Backend Pipeline (`work/pipeline/`)
The overnight batch generator is decomposed into focused modules (<550 lines each). `work/run_overnight_v3.py` serves as the backward-compatible entry point and re-exports all symbols.

| Module | Responsibility | Key Symbols / Entry Points |
| :--- | :--- | :--- |
| `takes.py` | Non-destructive Render Vault, takes manifest, cache-hit auto-promotion, project-level checkpoints & rollback. | `safe_link_or_copy`, `ensure_baseline_takes_seeded`, `before_scene_render`, `after_scene_render`, `try_auto_promote_vault_hit`, `promote_take_to_active`, `create_project_checkpoint`, `rollback_project_checkpoint`, `handle_takes_api` |
| `characters.py` | Character Management Studio, multi-reference identity sheets, prompt compilation & tag expansion. | `compile_character_prompt`, `expand_prompt_with_characters`, `load_project_characters`, `save_project_characters`, `handle_characters_api` |
| `media_utils.py` | Media file validation, hashing, ffprobe helpers, path resolution, coverage checking, settings diffing. | `resolve_rendered_clip_path`, `check_scene_coverage`, `diff_scene_settings`, `compute_prompt_hash`, `get_video_info` |
| `prompts.py` | Anchor ID normalization, cue marker expansion, relative prompt parsing, Wan2GP prompt adapters, image refs. | `extract_anchor_id`, `expand_relative_prompt_markers`, `adapt_prompt_for_wan2gp`, `resolve_scene_refs` |
| `context.py` | Project context loading, audio stem discovery, task dictionary building for Stage 1 & Stage 2. | `ProjectContext`, `make_anchor_task`, `make_video_task`, `resolve_scene_audio`, `resolve_scene_image_refs` |
| `wgp_bridge.py` | Wan2GP session lifecycle, task dispatch, progress callbacks, cancellation checks, engine status persistence. | `LazyWgpSession`, `execute_wgp_job`, `write_engine_status`, `check_cancel_requested` |
| `stage1_anchors.py` | Stage 1 character anchor image generation, directory imports, anchor image promotion. | `run_stage1_anchors`, `import_anchors_from_dir`, `match_anchor_id` |
| `stage2_scenes.py` | Stage 2 scene video generation, multi-pass duet orchestration, hash caching, skipping logic, take hooks. | `run_stage2_scenes`, `compute_project_task_hashes`, `match_scene_pass` |
| `stage3_master.py` | Audio-synced progressive master video assembly with FFmpeg concat demuxer. | `assemble_progressive_master`, `run_stage3_assemble_master` |
| `cli.py` | Command-line argument parsing, project initialization, catalog printing, main dispatch. | `parse_args`, `init_new_project`, `print_catalog`, `main` |
| `__init__.py` | Package facade re-exporting all pipeline functions and classes. | Package namespace |

### 1.2 Web UI Backend Server (`work/ui/`)
The web UI backend is built with standard Python libraries with zero third-party requirements. `work/ui/server.py` holds shared state and re-exports all endpoints and workers.

| Module | Responsibility | Key Symbols / Entry Points |
| :--- | :--- | :--- |
| `server.py` | Shared state (`PROJECTS_DIR`, `render_status`, `render_lock`, `log_history`), interpreter resolver, log broadcast. | `resolve_engine_python`, `broadcast_log`, `read_project_engine_status` |
| `server_media.py` | High-performance Range video/audio/strip streaming, historical take resolution (`?take=<id>`), anchor serving. | `handle_media_stream`, `serve_file_with_range` |
| `server_workers.py` | Render worker thread, process tree management, graceful cancellation, single-task execution loop. | `run_queue_worker`, `execute_single_render`, `kill_active_render`, `run_anchor_worker`, `run_stitch_worker` |
| `server_queue.py` | Fast queue evaluation against disk/hashes, scene ID disambiguation, reordering, vault cache hits. | `compute_fast_project_queue`, `prepare_targeted_queue_items`, `move_in_project_queue`, `remove_from_project_queue`, `collect_project_clips` |
| `server_routes.py` | HTTP request handler (`StudioRequestHandler`), REST APIs (`/api/*`), takes & checkpoints APIs, SSE log stream. | `StudioRequestHandler`, `run_server` |

### 1.3 Web UI Frontend (`work/ui/static/js/`)
The browser frontend is decomposed into 8 specialized scripts loaded sequentially in `index.html`.

| Script | Responsibility | Key Elements & Functions |
| :--- | :--- | :--- |
| `state_and_utils.js` | Global state (`window.state`), DOM cache, time formatters, toast notifications, project REST sync. | `state`, `formatTime`, `showToast`, `loadProjects`, `selectProject`, `saveProjectData`, `markProjectDirty` |
| `timeline_and_playback.js` | Waveform rendering, audio playback loop, canvas interactions, scrubbing, zooming, visual playhead. | `redrawTimeline`, `seekTimeline`, `togglePlayback`, `setupCanvasEvents`, `updatePlayheadVisual` |
| `scene_inspector.js` | Scene property inputs, mode transitions, start/end time nudges, clip and anchor preview panels. | `selectScene`, `updateSceneInspectorInputs`, `handleSceneModeChange`, `nudgeTiming`, `deleteScene` |
| `markers_and_prompts.js` | Marker modal, timeline cue markers, relative prompt tag insertion, auto-expanding textareas. | `openMarkerModal`, `addMarkerAtPlayhead`, `insertMarkerTagIntoPrompt`, `updatePromptResolvedPreviews` |
| `anchors_and_settings.js` | Character anchor gallery, reference image management, model profiles, resolution cards. | `updateAnchorStudioUI`, `populateModelSelectors`, `renderResolutionCards`, `updateProjectSettingsUI` |
| `characters_studio.js` | Multi-character catalog, identity reference sheets, auto-tag compilation. | `openCharacterStudioModal`, `renderCharacterGallery`, `saveCharacter` |
| `takes_and_history.js` | Undo/Redo stack (`Ctrl+Z`/`Ctrl+Y`), Scene Takes tray, historical preview & diff, Project Checkpoints modal. | `updateHistoryButtons`, `performUndo`, `performRedo`, `updateSceneTakesUI`, `openCheckpointsModal`, `rollbackToCheckpoint` |
| `queue_and_console.js` | Queue list rendering, live SSE log streaming, render initiation, master video restitch, dummy toggle. | `renderQueueUI`, `initLogStream`, `triggerRender`, `triggerImmediateRestitch`, `updateMasterVideoUI`, `switchTab` |

---

## 2. Navigating Symbols Fast

To find exact line numbers and definitions without opening entire files:

```bash
# Search for any symbol across the entire codebase:
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --find compute_fast_project_queue

# List all functions and classes in a specific module:
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --module server_queue

# Output JSON for programmatic tooling:
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --find run_queue_worker --json
```

---

## 3. State & File Contracts Reference
Read [state_contracts.md](file:///c:/Users/bugbl/AppData/Local/Temp/MusicVideoGeneration/.agents/skills/mvg-architecture-map/references/state_contracts.md) for full JSON schema details of:
- `project.json`
- `v3_queue.json`
- `state_hashes.json`
- `engine_status.json`
