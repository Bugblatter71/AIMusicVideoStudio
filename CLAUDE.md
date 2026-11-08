# Music Video Generation Studio - Claude Instructions

This repository is optimized for token-efficient agentic workflows with Anthropic Claude models.
Refer to [AGENTS.md](AGENTS.md) for full architecture details and safety rules.

## Quick CLI Tools (Avoid Reading Whole Files)

- **Search Code Symbols**:
  ```bash
  python .claude/skills/mvg-architecture-map/scripts/symbol_index.py --find <symbol>
  python .claude/skills/mvg-architecture-map/scripts/symbol_index.py --module <module_name>
  ```
- **Audit Project Timeline & Stems**:
  ```bash
  python .claude/skills/mvg-project-ops/scripts/project_tool.py audit <project_slug>
  ```
- **Diagnose Render Queue**:
  ```bash
  python .claude/skills/mvg-queue-and-wan2gp/scripts/diagnose_queue_state.py <project_slug>
  ```
- **Inspect Rendered Clips & Audio**:
  ```bash
  python .claude/skills/mvg-media-inspector/scripts/inspect_clip.py <path_to_mp4>
  ```

## Test Suite
```bash
python -m unittest tests/test_backend_queue.py tests/test_wan2gp_integration.py -v
```
*(Do not set RUN_LIVE_WAN2GP_TESTS=1 unless user explicitly confirms GPU availability)*
