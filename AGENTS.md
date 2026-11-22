# Music Video Generation Studio - Agent Instructions

Welcome, AI Agent (Google Antigravity / Gemini / Claude).

This repository is optimized for token-efficient agentic workflows. All active modules are strictly `< 850` lines with dedicated skills and CLI diagnostic utilities.

---

## 1. Quick Navigation & Workspace Skills

Instead of scanning large files manually, use our CLI tools and `.agents/skills/` workflows:

### App Architecture, Maintenance & Testing Skills
- **`mvg-architecture-map`**: Symbol indexer & state/API contracts (`work/` modules, JSON schemas, HTTP routes).
  ```bash
  python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --find <symbol_name>
  python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --check-limits
  ```
- **`mvg-dev-and-testing`**: Modularity rules, test suite matrix, and repo health check.
  ```bash
  python .agents/skills/mvg-dev-and-testing/scripts/verify_repo_health.py
  ```
- **`mvg-queue-and-wan2gp`**: Queue evaluation rules, multi-engine scheduler diagnostics, CUDA OOM retry & `model_profiles.json` validation.
  ```bash
  python .agents/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py <project_slug>
  python .agents/skills/mvg-queue-and-wan2gp/scripts/validate_wgp_profile.py
  ```
- **`mvg-cloud-and-duet-ops`**: `fal.ai` / `MiniMax` routing precedence, DPAPI keys, Two-Pass Duet Pass 2 ROI / lip-sync nuances, `repair-lipsync`, `upscale`, and VRAM management.

### Project Operations & New Video Setup Skills
- **`mvg-new-project-setup`**: End-to-end onboarding for a new music video—audio stem section detection & storyboard scaffolding.
  ```bash
  python .agents/skills/mvg-new-project-setup/scripts/analyze_audio_sections.py projects/<slug>/audio
  python .agents/skills/mvg-new-project-setup/scripts/scaffold_project.py <slug> --spec <storyboard_spec.json>
  ```
- **`mvg-prompt-and-asset-craft`**: H3 multimodal prompt authoring, `@character_look` / `{VAR}` compilation, `<marker>` cue tags, anchor framing, and two-pass duet prompts.
- **`mvg-project-ops`**: Safe inspection, audit, and scene editing with automatic backups.
  ```bash
  python .agents/skills/mvg-project-ops/scripts/project_tool.py summary <project_slug>
  python .agents/skills/mvg-project-ops/scripts/project_tool.py audit <project_slug>
  python .agents/skills/mvg-project-ops/scripts/project_tool.py show-scene <project_slug> <scene_id>
  ```
- **`mvg-media-inspector`**: FFprobe stream inspection, audio folder scans, and proportional 5-frame review strips.
  ```bash
  python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py <path_to_media_or_audio_dir>
  ```

---

## 2. Codebase Architecture

```text
MusicVideoGeneration/
├── work/
│   ├── run_overnight_v3.py        # Backward-compatible pipeline facade
│   ├── pipeline/                  # Modular generation engine (<850 lines/file)
│   │   ├── cli.py                 # CLI parsing, init_new_project, main
│   │   ├── context.py             # ProjectContext, stems & task dict builders
│   │   ├── characters.py          # Character Studio, look synthesis, prompt compile
│   │   ├── prompts.py             # Anchor IDs, cue marker expansion, image refs
│   │   ├── media_utils.py         # Hashes, ffprobe helpers, path & ETA math
│   │   ├── scene_ops.py           # Safe split-scene & delete-scene mutations
│   │   ├── stage1_anchors.py      # Stage 1 anchor image generation & imports
│   │   ├── stage2_scenes.py       # Stage 2 scene rendering & two-pass duet
│   │   ├── stage2_hashes.py       # Task/anchor hash computation & state saving
│   │   ├── stage3_master.py       # Stage 3 audio-synced master video assembly
│   │   ├── duet_roi.py            # Cross-engine Pass 2 ROI crop/composite & repair-lipsync
│   │   ├── upscale.py             # Clip upscaling (FlashVSR+, SeedVR2, fal.ai) & filters
│   │   ├── fal_catalog.py         # Cloud catalogs, DPAPI key storage, routing & cost
│   │   ├── fal_bridge.py          # fal.ai async client, chunking & ROI lip-sync
│   │   ├── minimax_bridge.py      # MiniMax v2 API client & duet Pass 2 ROI
│   │   ├── takes.py               # Render Vault, takes manifest, checkpoints & rollback
│   │   └── wgp_bridge.py          # Local WanGPSession & SessionRouter lifecycle
│   └── ui/
│       ├── server.py              # Shared state & backward-compatible facade
│       ├── server_routes.py       # StudioRequestHandler HTTP routes & SSE logs
│       ├── server_media.py        # Range video/audio/strip streaming & take previews
│       ├── server_queue.py        # Fast queue evaluation, vault hits & reordering
│       ├── server_engines.py      # Multi-engine parallel scheduler (local/fal/minimax)
│       ├── server_engine_io.py    # Locks, slot validation, OOM auto-retry & failure reads
│       ├── server_workers.py      # Subprocess runner, process termination, ad-hoc workers
│       ├── server_fal.py          # Cloud API routes (/api/fal/*, /api/minimax/*)
│       ├── server_security.py     # Path traversal & JSON input validation guards
│       └── static/
│           ├── index.html         # Web UI layout
│           ├── css/               # studio.css, scenes_tab.css
│           └── js/                # app, state_and_utils, timeline_and_playback, scene_inspector,
│                                  # scenes_tab, scene_jump_bar, markers_and_prompts, anchors_and_settings,
│                                  # characters_studio, duet_repair, takes_and_history, fal_settings,
│                                  # queue_engines, queue_and_console, master_and_export
├── tests/                         # Regression, backend queue, cloud, takes, ROI, Wan2GP & E2E tests
└── .agents/skills/ (and .claude/skills/)
    ├── mvg-architecture-map/      # Codebase symbols indexer, limits checker & contracts
    ├── mvg-dev-and-testing/       # Repo health verification & test suite guide
    ├── mvg-queue-and-wan2gp/      # Queue diagnostics & profile validation
    ├── mvg-cloud-and-duet-ops/    # Cloud routing, two-pass ROI, lip-sync & VRAM guide
    ├── mvg-new-project-setup/     # Audio phrase analyzer & storyboard project scaffolder
    ├── mvg-prompt-and-asset-craft/# H3 prompts, @characters, markers & anchor craft
    ├── mvg-project-ops/           # Project summary, audit & safe scene editing
    └── mvg-media-inspector/       # Clip/image/audio metadata & 5-frame filmstrips
```

---

## 3. Critical Safety Rules

1. **User Active Project**:
   `projects/all_or_nothing/all_or_nothing_v4.json` contains active user creative work.
   - Never overwrite or modify it directly without creating a timestamped backup in `backup/`.
   - Prefer using `project_tool.py set-scene ...` which creates backups automatically.

2. **No GPU Tests Without Confirmation**:
   - Never set `RUN_LIVE_WAN2GP_TESTS=1` unless the user explicitly confirms that the GPU is idle and ready.
   - Run offline mock/unit tests:
     ```bash
     python -m unittest tests/test_regression_fixes.py tests/test_backend_queue.py tests/test_fal_integration.py tests/test_takes_and_rollback.py tests/test_resolution_and_upsampling.py tests/test_wan2gp_integration.py -v
     ```

3. **Maintain Modularity & Facades**:
   - Keep every `.py`, `.js`, `.html`, `.css` file under `work/` (excluding `Wan2GP`) strictly `< 850` lines.
   - Always preserve re-exports in `work/run_overnight_v3.py` and `work/ui/server.py` so existing batch files and test imports remain 100% functional.
