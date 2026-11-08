"""Music Video Generation Pipeline Package.
Modularized components of the overnight render and assembly engine.
"""

from .cli import (
    init_new_project,
    is_project_config,
    main,
    parse_cli_and_config,
    print_catalog,
    resolve_project_config,
)
from .context import (
    ProjectContext,
    derive_split_scene_ids,
    split_scene_in_project_data,
    delete_scene_in_project_data,
)
from .scene_ops import (
    derive_split_scene_ids,
    split_scene_in_project_data,
    delete_scene_in_project_data,
)
from .media_utils import (
    _EMBEDDED_CHECK_KEYS,
    _NON_SETTING_TASK_KEYS,
    WORKSPACE_ROOT,
    _file_sha256,
    _norm_setting,
    _sha256,
    auto_detect_wgp_root,
    check_scene_render_coverage,
    find_local_config,
    probe_video_dimensions,
    probe_video_duration,
    probe_video_fps,
    probe_video_frames,
    read_embedded_render_settings,
    render_settings_of,
    resolve_ffmpeg_path,
    resolve_path,
    resolve_rendered_clip_path,
    settings_change_reason,
)
from .prompts import (
    adapt_ref_tags,
    adapt_video_prompt,
    expand_variables,
    extract_anchor_id,
    resolve_marker_prompt,
)
from .stage1_anchors import (
    import_anchors_from_dir,
    match_anchor_id,
    run_stage1_anchors,
)
from .stage2_scenes import (
    _scene_id_matches,
    _stem_matches_legacy_render,
    compute_project_task_hashes,
    match_scene_pass,
    run_stage2_scenes,
)
from .stage3_master import (
    assemble_progressive_master,
    run_stage3_assemble_master,
)
from .wgp_bridge import (
    LazyWgpSession,
    check_cancel_requested,
    clear_cancel_request,
    execute_wgp_job,
    format_progress_update,
    write_engine_status,
)

__all__ = [
    "WORKSPACE_ROOT",
    "_sha256",
    "_file_sha256",
    "_NON_SETTING_TASK_KEYS",
    "_EMBEDDED_CHECK_KEYS",
    "render_settings_of",
    "_norm_setting",
    "read_embedded_render_settings",
    "probe_video_fps",
    "probe_video_dimensions",
    "probe_video_duration",
    "probe_video_frames",
    "resolve_rendered_clip_path",
    "check_scene_render_coverage",
    "find_local_config",
    "auto_detect_wgp_root",
    "resolve_ffmpeg_path",
    "resolve_path",
    "settings_change_reason",
    "extract_anchor_id",
    "expand_variables",
    "resolve_marker_prompt",
    "adapt_ref_tags",
    "adapt_video_prompt",
    "ProjectContext",
    "derive_split_scene_ids",
    "split_scene_in_project_data",
    "LazyWgpSession",
    "write_engine_status",
    "check_cancel_requested",
    "clear_cancel_request",
    "format_progress_update",
    "execute_wgp_job",
    "match_anchor_id",
    "_scene_id_matches",
    "match_scene_pass",
    "print_catalog",
    "run_stage1_anchors",
    "import_anchors_from_dir",
    "_stem_matches_legacy_render",
    "compute_project_task_hashes",
    "run_stage2_scenes",
    "assemble_progressive_master",
    "run_stage3_assemble_master",
    "is_project_config",
    "resolve_project_config",
    "parse_cli_and_config",
    "init_new_project",
    "main",
]
