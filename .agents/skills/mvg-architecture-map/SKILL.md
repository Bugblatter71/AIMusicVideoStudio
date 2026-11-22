---
name: mvg-architecture-map
description: Fast architectural overview of the Music Video Generation Studio codebase, mapping core pipelines, cloud/ROI modules, server endpoints, and frontend scripts to prevent large whole-file context loading.
---

# MVG Architecture Map & Code Navigator

Use this skill whenever you need to locate, understand, or modify functionality in the Music Video Generation Studio codebase.

Instead of scanning whole files (all active modules are kept strictly `< 850` lines), use this map and `symbol_index.py` to pinpoint exact files and line ranges.

---

## 1. Directory & Module Map

### 1.1 Backend Pipeline (`work/pipeline/`)
`work/run_overnight_v3.py` is the backward-compatible facade re-exporting symbols from `work/pipeline/`:

| Module | Responsibility | Key Symbols / Entry Points |
| :--- | :--- | :--- |
| `cli.py` | CLI parsing, `init_new_project`, `--list`, `--stitch`, `upscale`, `repair-lipsync`, `main`. | `parse_cli_and_config`, `init_new_project`, `print_catalog`, `main` |
| `context.py` | `ProjectContext`, stem auto-discovery, task builders, padding resolution. | `ProjectContext`, `make_anchor_task`, `make_video_task`, `resolve_scene_padding` |
| `characters.py` | Character Studio catalog, look synthesis, `@tag` expansion & Ref2VA compilation. | `synthesize_default_characters`, `compile_character_prompt`, `handle_characters_api` |
| `prompts.py` | Anchor IDs, cue marker `<tag>` expansion, prompt adapters, `<imageN>` mapping. | `extract_anchor_id`, `resolve_marker_prompt`, `build_ref2va_prompt`, `resolve_scene_refs` |
| `media_utils.py` | File hashes, ffprobe metadata, rendered clip matching, backend/ROI-isolated ETAs. | `resolve_rendered_clip_path`, `populate_queue_estimates`, `check_scene_coverage` |
| `scene_ops.py` | Safe non-destructive scene split and delete timeline mutations. | `derive_split_scene_ids`, `split_scene_in_project_data`, `delete_scene_in_project_data` |
| `stage1_anchors.py` | Stage 1 character anchor image generation & directory imports. | `run_stage1_anchors`, `import_anchors_from_dir`, `match_anchor_id` |
| `stage2_scenes.py` | Stage 2 scene rendering, two-pass orchestration, vault/hash checks. | `run_stage2_scenes`, `match_scene_pass` |
| `stage2_hashes.py` | State hash computation, take-snapshot matching, parallel save locking. | `compute_project_task_hashes`, `check_pass_up_to_date` |
| `stage3_master.py` | Audio-synced FFmpeg concat master video assembly & fade/trim filters. | `assemble_progressive_master`, `run_stage3_assemble_master` |
| `duet_roi.py` | Cross-engine Pass 2 ROI cropping, feathered compositing & `repair-lipsync`. | `apply_pass2_guide_and_roi`, `composite_roi_onto_guide`, `run_repair_lipsync_cli` |
| `upscale.py` | Clip upscaling (FlashVSR+, SeedVR2, fal.ai), canonical stem resolution, filters. | `upscale_scene_clip`, `resolve_scene_canonical_stem`, `apply_filters_to_video_task` |
| `fal_catalog.py` | fal.ai & MiniMax profile catalog, Windows DPAPI secret storage, routing & cost. | `resolve_task_backend`, `resolve_fal_route`, `resolve_minimax_route`, `get_fal_status` |
| `fal_bridge.py` | fal.ai async queue job submission, polling, chunking >15s & ROI lip-sync. | `FalSession`, `_run_duet_pass2_roi_lipsync`, `run_chunked_video_sequence` |
| `minimax_bridge.py` | Native MiniMax API v2 session, uploads, `duet_pass2_roi_minimax` & polling. | `MiniMaxSession`, `_run_duet_pass2` |
| `takes.py` | Non-destructive Render Vault, `takes_manifest.json`, vault cache hits, checkpoints. | `before_scene_render`, `after_scene_render`, `try_auto_promote_vault_hit`, `rollback_project_checkpoint` |
| `wgp_bridge.py` | Local `WanGPSession` & `SessionRouter`, progress updates, cancellation files. | `LazyWgpSession`, `SessionRouter`, `execute_wgp_job`, `write_engine_status` |

### 1.2 Web UI Backend Server (`work/ui/`)
Standard-library HTTP server (`work/ui/server.py` preserves backward-compatible re-exports):

| Module | Responsibility | Key Symbols / Entry Points |
| :--- | :--- | :--- |
| `server.py` | Shared server state, locks, `enabled_engines` (`local` True, `fal`/`minimax` False), facade. | `resolve_engine_python`, `broadcast_log`, `read_project_engine_status` |
| `server_routes.py` | `StudioRequestHandler` HTTP routes (`/api/*`), SSE stream, queues & project save. | `StudioRequestHandler`, `run_server` |
| `server_media.py` | Byte-range media streaming, `?take=<id>` previews, manifest clip resolution. | `handle_media_stream`, `apply_active_manifest_clips`, `serve_file_with_range` |
| `server_queue.py` | Fast queue evaluation, vault hit auto-promotion, scene reordering, clip map. | `compute_fast_project_queue`, `prepare_targeted_queue_items`, `collect_project_clips` |
| `server_engines.py` | Multi-engine parallel scheduler (`local`/`fal`/`minimax`), OOM pause & retry loop. | `run_multi_engine_queue_worker`, `set_enabled_engines`, `get_multi_engine_render_status` |
| `server_engine_io.py` | File locks, slot status validation, OOM detection/retry, failure summaries. | `execute_slot_task_with_oom_retry`, `read_task_failure_summary`, `requeue_failed_items` |
| `server_workers.py` | Subprocess runner, process tree termination, unlinked clip restore, ad-hoc workers. | `execute_single_render`, `kill_active_render`, `run_anchor_worker`, `run_stitch_worker` |
| `server_fal.py` | `/api/fal/*` & `/api/minimax/*` endpoints, key testing & queue cost/model enrichment. | `handle_fal_get`, `handle_fal_post`, `enrich_queue_cloud_estimates` |
| `server_security.py` | Path traversal guards & bounded JSON body parsing (`400`/`404`). | `resolve_project_dir_safe`, `resolve_path_under_dir`, `read_json_dict` |

### 1.3 Web UI Frontend (`work/ui/static/`)
- Styles: `css/studio.css`, `css/scenes_tab.css`
- Scripts loaded in `index.html`:
  - `app.js`, `state_and_utils.js`, `timeline_and_playback.js`, `scene_inspector.js`, `scenes_tab.js`, `scene_jump_bar.js`, `markers_and_prompts.js`, `anchors_and_settings.js`, `characters_studio.js`, `duet_repair.js`, `takes_and_history.js`, `fal_settings.js`, `queue_engines.js`, `queue_and_console.js`, `master_and_export.js`

---

## 2. Navigating Symbols & Checking Modularity Fast

```bash
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --find compute_fast_project_queue
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --module server_engines
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --tests
python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits
```

---

## 3. State & API Contracts Reference
See [state_contracts.md](./references/state_contracts.md) for JSON schemas (`project.json`, `v3_queue.json`, `state_hashes.json`, `takes_manifest.json`, `engine_status*.json`) and HTTP `/api/*` routes.
