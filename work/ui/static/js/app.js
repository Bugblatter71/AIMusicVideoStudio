/**
 * Music Video Generation Studio - Client Web UI Entry Facade.
 *
 * Decomposed into 6 modular scripts (<950 lines each) for model efficiency:
 * 1. js/state_and_utils.js       - Global reactive state, DOM cache, utilities, and REST API sync
 * 2. js/timeline_and_playback.js - HTML5 Canvas multi-track rendering, audio playback, and scrubbing
 * 3. js/scene_inspector.js       - Scene editing, timing nudges, mode transitions, clip/anchor preview
 * 4. js/markers_and_prompts.js   - Relative timeline cue markers and prompt auto-expansion
 * 5. js/anchors_and_settings.js  - Character/anchor studio gallery, model presets, and resolution cards
 * 6. js/queue_and_console.js     - Render queue reordering, live SSE execution logs, and master stitch
 */

// If loaded directly without index.html script tags, load submodules dynamically
if (!window.state) {
  const scripts = [
    "js/state_and_utils.js",
    "js/timeline_and_playback.js",
    "js/scene_inspector.js",
    "js/takes_and_history.js",
    "js/characters_studio.js",
    "js/markers_and_prompts.js",
    "js/anchors_and_settings.js",
    "js/fal_settings.js",
    "js/duet_repair.js",
    "js/queue_engines.js",
    "js/queue_and_console.js",
    "js/master_and_export.js",
    "js/scenes_tab.js",
    "js/fal_settings.js",
    "js/duet_repair.js",
    "js/clip_import.js",
    "js/split_resizer.js",
  ];
  scripts.forEach((src) => {
    const el = document.createElement("script");
    el.src = src;
    el.async = false;
    document.head.appendChild(el);
  });
}
