"""
Generic Self-Service Music Video Generation Pipeline (Wan2GP Local Image + Video + FFmpeg Assembly).

This file serves as the main CLI entry point and facade, re-exporting all components from the
focused `pipeline` package (`work/pipeline/`):
- `media_utils.py`   : FFprobe/FFmpeg helpers, video resolution, duration & frame rate probes, clipping
- `prompts.py`       : Prompt adaptation, variable expansion, marker cues, and reference tags
- `context.py`       : ProjectContext configuration loader & parameter resolver
- `wgp_bridge.py`    : WanGPSession adapter, engine_status.json streaming & cancellation
- `stage1_anchors.py`: Stage 1 character anchor generation & import
- `stage2_scenes.py` : Stage 2 video rendering, change detection, and queue synchronization
- `stage3_master.py` : Flash-free progressive master video concat & review strip extraction
- `cli.py`           : CLI argument parsing, catalog printing, and project initialization

USAGE:
------
  python work/run_overnight_v3.py <project_config.json> [options / Anchor IDs / Scene IDs]
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `work` is in sys.path when executed directly or imported
_WORK_DIR = Path(__file__).resolve().parent
if str(_WORK_DIR) not in sys.path:
    sys.path.insert(0, str(_WORK_DIR))

from pipeline import (
    _EMBEDDED_CHECK_KEYS,
    _NON_SETTING_TASK_KEYS,
    WORKSPACE_ROOT,
    LazyWgpSession,
    ProjectContext,
    derive_split_scene_ids,
    split_scene_in_project_data,
    _file_sha256,
    _norm_setting,
    _scene_id_matches,
    _sha256,
    _stem_matches_legacy_render,
    adapt_ref_tags,
    adapt_video_prompt,
    assemble_progressive_master,
    attach_queue_time_estimates,
    auto_detect_wgp_root,
    check_cancel_requested,
    check_scene_render_coverage,
    clear_cancel_request,
    compute_calibration_factor,
    compute_project_task_hashes,
    delete_scene_in_project_data,
    derive_split_scene_ids,
    estimate_pass_render_time,
    estimate_scene_render_time,
    execute_wgp_job,
    expand_variables,
    extract_anchor_id,
    find_local_config,
    format_duration_estimate,
    format_progress_update,
    import_anchors_from_dir,
    init_new_project,
    is_project_config,
    main,
    match_anchor_id,
    match_scene_pass,
    parse_cli_and_config,
    print_catalog,
    probe_video_dimensions,
    probe_video_duration,
    probe_video_fps,
    probe_video_frames,
    quantize_video_frames,
    read_embedded_render_settings,
    render_settings_of,
    resolve_ffmpeg_path,
    resolve_marker_prompt,
    resolve_path,
    resolve_project_config,
    resolve_rendered_clip_path,
    resolve_scene_padding,
    run_stage1_anchors,
    run_stage2_scenes,
    run_stage3_assemble_master,
    settings_change_reason,
    write_engine_status,
)

if __name__ == "__main__":
    main()
