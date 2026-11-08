# Music Video Generation Studio - Agent Instructions

Welcome, AI Agent (Google Antigravity / Gemini / Claude).

This repository is optimized for token-efficient agentic workflows. All monolithic scripts have been decomposed into focused modules (<850 lines) with dedicated skills and CLI diagnostic utilities.

---

## 1. Quick Navigation: Do NOT Scan Large Files

Instead of scanning whole files into context:

1. **Locate Symbols Fast**:
   Run the symbol indexer to pinpoint the exact file and line ranges:
   ```bash
   python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --find <symbol_name>
   python .agents/skills/mvg-architecture-map/scripts/symbol_index.py --module <module_name>
   ```

2. **Inspect Projects & Scenes**:
   ```bash
   python .agents/skills/mvg-project-ops/scripts/project_tool.py summary <project_slug>
   python .agents/skills/mvg-project-ops/scripts/project_tool.py audit <project_slug>
   python .agents/skills/mvg-project-ops/scripts/project_tool.py show-scene <project_slug> <scene_id>
   ```

3. **Diagnose Render Queue**:
   ```bash
   python .agents/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py <project_slug>
   ```

4. **Inspect Rendered Clips & Audio**:
   ```bash
   python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py <path_to_mp4>
   ```

---

## 2. Codebase Architecture

```
MusicVideoGeneration/
├── work/
│   ├── run_overnight_v3.py        # 67-line backward-compatible pipeline facade
│   ├── pipeline/                  # Modular backend generation engine
│   │   ├── cli.py                 # CLI argument parsing, init_new_project, main
│   │   ├── context.py             # ProjectContext, task dictionary generation
│   │   ├── media_utils.py         # File hashing, ffprobe helpers, path resolution
│   │   ├── prompts.py             # Anchor IDs, relative cue marker expansion
│   │   ├── stage1_anchors.py      # Stage 1 character anchor image generation
│   │   ├── stage2_scenes.py       # Stage 2 scene video generation, multi-pass duet
│   │   ├── stage3_master.py       # Stage 3 audio-synced master video assembly
│   │   └── wgp_bridge.py          # WanGPSession lifecycle, engine status, cancellation
│   └── ui/
│       ├── server.py              # Shared server state & backward-compatible facade
│       ├── server_workers.py      # Background render worker thread & subprocess tree
│       ├── server_queue.py        # Fast queue evaluation, scene matching, reordering
│       ├── server_routes.py       # StudioRequestHandler HTTP routes & SSE streaming
│       └── static/
│           ├── index.html         # Web UI layout
│           └── js/
│               ├── app.js                   # Dynamic fallback loader
│               ├── state_and_utils.js       # Reactive state, DOM cache, REST sync
│               ├── timeline_and_playback.js # Canvas waveform, audio loop, scrubbing
│               ├── scene_inspector.js       # Scene property editing, timing nudges
│               ├── markers_and_prompts.js   # Cue markers modal & prompt previews
│               ├── anchors_and_settings.js  # Anchor studio & model profile presets
│               └── queue_and_console.js     # Queue list, SSE logs, render controls
├── tests/
│   ├── test_backend_queue.py      # 25 backend unit & user journey tests
│   └── test_wan2gp_integration.py # 14 Wan2GP engine integration tests
└── .agents/skills/ (and .claude/skills/)
    ├── mvg-architecture-map/      # Codebase symbols indexer & state contracts
    ├── mvg-project-ops/           # Project summary, audit, and safe scene editing
    ├── mvg-queue-and-wan2gp/      # Queue diagnostics & profile validation
    └── mvg-media-inspector/       # Clip ffprobe metadata & filmstrip generation
```

---

## 3. Critical Safety Rules

1. **User Active Project**:
   `projects/all_or_nothing/all_or_nothing_v4.json` contains active user creative work.
   - Never overwrite or modify it directly without creating a timestamped backup in `backup/`.
   - Prefer using `project_tool.py set-scene ...` which creates backups automatically.

2. **No GPU Tests Without Confirmation**:
   - Never set `RUN_LIVE_WAN2GP_TESTS=1` unless the user explicitly confirms that the GPU is idle and ready.
   - Run the mock/unit tests:
     ```bash
     python -m unittest tests/test_backend_queue.py tests/test_wan2gp_integration.py -v
     ```

3. **Maintain Modular Facades**:
   - Always preserve re-exports in `work/run_overnight_v3.py` and `work/ui/server.py` so existing batch files and test imports remain 100% functional.
