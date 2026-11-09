# Specification: Non-Destructive Rollback & Render Vault System (v2 — Implementation-Ready Blueprint)

**Status:** Approved Architecture / Implementation-Ready Blueprint (v2, post-review)  
**Date:** 2026-11-08  
**Target Codebase:** Music Video Generation Studio (`work/pipeline/`, `work/ui/`, `tests/`)  
**Scope:** Lossless Undo/Redo (`Ctrl+Z`/`Ctrl+Y`), Scene-Level Take Switching, Automatic Vault Cache-Hits, and Project-Level Checkpoints  

---

## 0. Key Architectural Fixes Incorporated in v2 (Read First)

During the v2 architectural critique against the live codebase (`stage2_scenes.py`, `server_queue.py`, `server_media.py`, `scene_inspector.js`), **8 critical engineering safeguards** were added. Any implementation **MUST** follow these rules:

1. **CRITICAL — Unlink-Before-Write Rule for NTFS Hardlinks (`safe_replace_active_file`):**
   - When `renders_v3/{out_name}.mp4` is an NTFS hardlink to `renders_v3/takes/{sid}/{take_file}.mp4`, both paths share the **exact same filesystem inode / MFT record**.
   - If Python's `shutil.copy2(src, out_mp4)` or `ffmpeg -y ... out_mp4` opens `out_mp4` in `'wb'` (truncate-in-place) mode, **it will overwrite and corrupt the archived take inside the vault!**
   - **Mandatory Rule:** Before writing, copying, hardlinking, or running `ffmpeg` to an active `out_mp4` (or `_strip.jpg`), **always call `out_mp4.unlink(missing_ok=True)` first**. Unlinking removes only the active directory entry, leaving the vault file's link count at `1` and its bytes 100% untouched.

2. **CRITICAL — `st_mtime` & `completed_at` Harmonization on Take Promotion:**
   - `work/ui/server_queue.py` (`_evaluate_pass`, lines 333, 348, 363) checks `mp4_path.stat().st_mtime < anchor_info["mtime"]`, `mp4_path.stat().st_mtime < r_path.stat().st_mtime`, and (for Pass 2) `mp4_path.stat().st_mtime < p1_mp4.stat().st_mtime`.
   - If an older take from yesterday is promoted without updating its `mtime`, `server_queue.py` will falsely flag it as `ANCHOR NEWER ON DISK` or `PASS 1 NEWER ON DISK` and demand a re-render!
   - **Mandatory Rule:** When promoting a take to active:
     - Set `os.utime(active_mp4, (safe_mtime, safe_mtime))` where `safe_mtime = max(time.time(), anchor_mtime + 1.0, max_ref_mtime + 1.0, p1_mtime + 1.0)`. (When promoting Pass 1 and Pass 2 together, set Pass 2's `mtime` to `p1_mtime + 1.0`).
     - Simultaneously, write the take's **original** creation timestamp into `state_hashes["scene_renders"][out_name]["completed_at"] = take["created_at"]` so the Render Console's **"Done"** list preserves true chronological completion order (`server_queue.py` line 416 reads `render_info["completed_at"]` before `st_mtime`).

3. **Automatic Vault Cache-Hit (Zero-Click Auto-Restore on Manual Edit or `Ctrl+Z`):**
   - Users shouldn't have to manually hunt through a dropdown if they simply press `Ctrl+Z` or type an old seed/prompt back in.
   - Whenever `compute_fast_project_queue()` (or `stage2_scenes.py`) sees that `cur_task_hash != prev_hash`, it checks `takes_manifest.json` for an existing take of `out_name` with `take.task_hash in curr_hashes` whose `[render_start, render_end]` covers `[sc.start, sc.end]` and whose `anchor_sha256` matches the current anchor.
   - If a matching take exists in the vault, **automatically promote it to active in-place**! The queue immediately turns green (`UP-TO-DATE`) in `<5ms` with zero GPU rendering.

4. **Retroactive Baseline Vault Seeding (`ensure_baseline_takes_seeded`):**
   - Existing projects (like `all_or_nothing` with 37+ rendered clips) already have hours of completed renders in `renders_v3/`.
   - On project load / queue evaluation, any valid `renders_v3/*.mp4` that has a hash in `state_hashes.json` but is not yet in `takes_manifest.json` is **automatically registered and hardlinked into `renders_v3/takes/<sid>/` as `Take 1 (Baseline)`**. Every existing scene is protected immediately with zero manual setup.

5. **Timeline Boundary Safety (Scene Take Switch vs. Project Checkpoint Rollback):**
   - **Scene Take Switch (`/api/takes/activate`):** Restores all **generation parameters** (`mode`, `prompt`, `prompt_m`, `prompt_f`, `seed`, `seed_m`, `seed_f`, `stem`, `stem_m`, `stem_f`, `split_x`, `anchor`, `renderer`, `image_refs`) from `take["scene_snapshot"]`, updates `scene_renders[out_name]` with the take's `render_start` and `render_end`, and promotes the MP4—**while keeping the scene's current `sc["start"]` and `sc["end"]`** so it never overlaps adjacent scenes (`S02`, `S03`) on the timeline.
   - **Project Checkpoint Rollback (`/api/checkpoints/rollback`) & Timeline Undo:** Restores the entire `project["scenes"]` array atomically so all scene boundaries remain 100% contiguous.

6. **Non-Intrusive Take Preview Before Restore:**
   - Selecting an inactive take in the Scene Inspector dropdown streams `/media/<slug>/clip/<sid>?take=<take_id>` into the Scene Clip Video Player and shows a compact diff badge (e.g. `Seed 8201 vs 9140 | Prompt changed`) *without* overwriting the inspector inputs until the user clicks **`⏪ Restore This Take`**.

7. **Focus-Aware Undo/Redo Keybindings (`Ctrl+Z` / `Ctrl+Y`):**
   - Pressing `Ctrl+Z` or `Ctrl+Y` when focus is inside a `<textarea>` or `<input>` must allow native browser text undo/redo. When focus is outside text inputs (or when clicking the transport `↶ Undo` / `↷ Redo` buttons), it pops the Studio Project History stack, saves, and triggers automatic Vault Cache-Hit promotion.

8. **Strict `<850` Line Modularity Guardrails:**
   - Do **NOT** bloat `stage2_scenes.py` (currently 1,035 lines), `server_queue.py` (806 lines), `scene_inspector.js` (845 lines), or `timeline_and_playback.js` (844 lines).
   - Put all backend vault/checkpoint logic in **`work/pipeline/takes.py`** (~380 lines) and all frontend Undo/Takes/Checkpoint UI in **`work/ui/static/js/takes_and_history.js`** (~360 lines).

---

## 1. Executive Summary & Three-Tier Versioning Model

In AI music video production, rendering a single 6-second clip at `1344x768` takes 2–8 minutes of GPU compute. Experimenting with a new prompt, seed, or model profile must **never** destroy previous renders or force a re-render when reverting.

```mermaid
flowchart TD
    subgraph UI ["Studio Frontend (takes_and_history.js)"]
        UndoBar["Tier 1: Transport Undo / Redo<br/>(Ctrl+Z / Ctrl+Y In-Memory Stack)"]
        Inspector["Tier 2: Scene Takes Tray<br/>(Preview & Switch Take 1 / Take 2 / Take 3)"]
        CkptModal["Tier 3: Project Checkpoints Modal<br/>(Auto Pre-Render & Named Snapshots)"]
    end

    subgraph Engine ["Backend Vault Engine (work/pipeline/takes.py)"]
        AutoHit["Vault Cache-Hit Detector<br/>(Matches task_hash automatically)"]
        Promoter["Atomic Take Promoter<br/>(Unlink + Hardlink + Safe mtime)"]
        Seeder["Baseline Take Seeder<br/>(Protects existing renders_v3/*.mp4)"]
    end

    subgraph Storage ["Project Disk Storage"]
        ActiveMP4["Active Clip<br/>renders_v3/S01_male_solo_1344x768.mp4"]
        VaultMP4["Render Vault (Immutable Inodes)<br/>renders_v3/takes/S01/S01_..._h9a8b7c6.mp4"]
        CkptDir["Checkpoints<br/>checkpoints/ckpt_20261108_193000/"]
    end

    UndoBar -->|Save Reverted JSON| AutoHit
    Inspector -->|POST /api/takes/activate| Promoter
    CkptModal -->|POST /api/checkpoints/rollback| Promoter
    AutoHit -->|Hash Found in Vault| Promoter
    Seeder -->|0-Byte Hardlink| VaultMP4
    Promoter -->|1. unlink() Active<br/>2. os.link() from Vault<br/>3. Harmonize mtime & hashes| ActiveMP4
    VaultMP4 -.->|Shared NTFS Inode| ActiveMP4
```

### 1.1 How the Three Tiers Work Together

| Tier | Mechanism | User Workflow | How Renders Are Restored |
| :--- | :--- | :--- | :--- |
| **1. Quick Undo / Redo** | `Ctrl+Z` / `Ctrl+Y` & `↶` / `↷` buttons in transport bar | Undo recent boundary nudges, splits, deletes, or prompt/seed edits | Saving the undone project state triggers **Vault Cache-Hit**, automatically promoting the matching take MP4 in `<5ms`. |
| **2. Scene Takes** | `🎬 Takes & Versions` tray in Scene Inspector | Compare Take 1 vs Take 2 vs Take 3 for a specific scene (`S01`, `S02:p2`) and restore any earlier take | Previews historical take in clip player; clicking **`⏪ Restore This Take`** restores scene parameters + hardlinks the take MP4 to active. |
| **3. Project Checkpoints** | `📸 Checkpoints` button in transport bar + auto pre-render snapshots | Save named milestones (e.g. *"Act I Complete"*) or roll back an entire multi-scene experiment | Saves a safety backup of current state, restores `project.json`, and bulk-promotes every scene's recorded take. |

---

## 2. Storage Hierarchy & Data Schemas

### 2.1 Directory Layout

```
projects/<project_slug>/
├── <project_slug>.json                  # Active project configuration
├── checkpoints/                         # Tier 3: Project-Level Checkpoints
│   ├── index.json                       # Ordered list of checkpoint metadata
│   ├── ckpt_20261108_180000_manual/
│   │   ├── project.json                 # Full snapshot of project configuration
│   │   ├── state_hashes.json            # Snapshot of state_hashes.json
│   │   └── active_takes.json            # Map of out_name -> take_id
│   └── ckpt_20261108_192500_prerender/
└── work/
    ├── state_hashes.json                # Active queue hashes & scene_renders
    ├── takes_manifest.json              # Tier 2: Master index of all vaulted takes
    └── renders_v3/
        ├── S01_male_solo_1344x768.mp4   # Active clip (Hardlinked to active take)
        ├── S01_male_solo_1344x768_strip.jpg
        └── takes/                       # Immutable Render Vault
            ├── S01/
            │   ├── S01_male_solo_1344x768__h9a8b7c62e1f.mp4
            │   ├── S01_male_solo_1344x768__h9a8b7c62e1f_strip.jpg
            │   ├── S01_male_solo_1344x768__h3d4e5f68a7b.mp4
            │   └── S01_male_solo_1344x768__h3d4e5f68a7b_strip.jpg
            └── S02/
                ├── S02_Pass1_Left_1344x768__71fa82bc9012.mp4
                └── S02_Pass2_Final_1344x768__99b0c1d2e3f4.mp4
```

### 2.2 Takes Manifest Schema (`work/takes_manifest.json`)

Takes are keyed by `scene_id` (e.g. `"S01"`, `"S02"`). Each take stores both its exact `out_name` (`S01_male_solo_1344x768` or `S02_Pass2_Final_1344x768`), `pass_num` (`null`, `1`, or `2`), `task_hash`, `anchor_sha256`, and a complete `scene_snapshot` of the scene dictionary so restoring a take can reconstruct every single-pass or two-pass field accurately:

```json
{
  "version": 1,
  "scenes": {
    "S01": {
      "active_takes": {
        "default": "take_h3d4e5f68a7b"
      },
      "takes": [
        {
          "take_id": "take_h9a8b7c62e1f",
          "take_number": 1,
          "scene_id": "S01",
          "pass_num": null,
          "out_name": "S01_male_solo_1344x768",
          "task_hash": "h9a8b7c62e1f409a8b7c62e1f409a8b7c62e1f409a8b7c62e1f409a8b7c62e1f",
          "anchor_id": "A01",
          "anchor_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "parent_p1_task_hash": null,
          "created_at": 1731089400.0,
          "created_iso": "2026-11-07T14:30:00Z",
          "vault_rel_mp4": "takes/S01/S01_male_solo_1344x768__h9a8b7c62e1f.mp4",
          "vault_rel_strip": "takes/S01/S01_male_solo_1344x768__h9a8b7c62e1f_strip.jpg",
          "render_start": 0.0,
          "render_end": 6.8,
          "frames": 169,
          "elapsed_s": 242.5,
          "size_bytes": 10951765,
          "resolution": "1344x768",
          "model_id": "1",
          "model_type": "vace_14B",
          "scene_snapshot": {
            "id": "S01",
            "start": 0.0,
            "end": 6.8,
            "mode": "male_solo",
            "anchor": "A01",
            "stem": "male",
            "seed": 8201,
            "renderer": null,
            "prompt": "Marcus leans into the brass microphone..."
          },
          "render_settings": {
            "prompt": "Marcus leans into the brass microphone...",
            "seed": 8201,
            "model_type": "vace_14B",
            "resolution": "1344x768",
            "video_length": 169
          },
          "label": "Take 1 (Baseline)"
        }
      ]
    }
  }
}
```

> [!NOTE]
> For Two-Pass scenes (e.g. `S02`), `active_takes` tracks `"p1"` and `"p2"` (e.g. `{"p1": "take_71fa82bc9012", "p2": "take_99b0c1d2e3f4"}`), and Pass 2 takes store `"parent_p1_task_hash"` so restoring a Pass 2 take can automatically verify and restore its matching Pass 1 guide video if Pass 1 was also changed.

---

## 3. Backend Engine Mechanics (`work/pipeline/takes.py`)

### 3.1 Safe Hardlink & File Replacement Helper

```python
def safe_link_or_copy(src: Path, dst: Path) -> None:
    """Safely replace dst with a hardlink (or copy) to src.
    CRITICAL: Always unlinks dst first so we never truncate a shared NTFS inode in-place!
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"Vault source missing: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        if dst.resolve() == src.resolve() and dst.stat().st_ino == src.stat().st_ino:
            return
        dst.unlink(missing_ok=True)
    try:
        os.link(str(src), str(dst))
    except OSError:
        shutil.copy2(str(src), str(dst))
```

### 3.2 Pre-Render & Post-Render Hooks in `stage2_scenes.py`

In `work/pipeline/stage2_scenes.py`, right before executing a render (both `--dummy` and live `execute_wgp_job`):

1. **`before_scene_render(ctx, sc, out_name, out_mp4, state)`**:
   - Ensures any existing `out_mp4` on disk is archived in `renders_v3/takes/<sid>/` and indexed in `takes_manifest.json` before the render begins.
   - **Calls `out_mp4.unlink(missing_ok=True)`** so `ffmpeg` or `shutil.copy2` writes to a brand-new inode and cannot corrupt the vaulted hardlink!
   - Wait — what if the render fails or is canceled mid-way?
     - In the `except` / cancellation handler of `stage2_scenes.py`, call **`restore_previous_active_take_if_missing(ctx, sc["id"], out_name, out_mp4)`**, which immediately re-links the previous active take back to `out_mp4` so a canceled render leaves the workspace 100% intact!

2. **`after_scene_render(ctx, sc, pass_num, out_name, out_mp4, t_hash, t_dict, elapsed, state)`**:
   - Archives the newly generated `out_mp4` into `renders_v3/takes/<sid>/{out_name}__{t_hash[:12]}.mp4` via `safe_link_or_copy`.
   - Generates or links the corresponding filmstrip thumbnail `{out_name}__{t_hash[:12]}_strip.jpg`.
   - Appends the take entry to `takes_manifest.json` and updates `active_takes`.

### 3.3 Automatic Vault Cache-Hit Lookup (`try_auto_promote_vault_hit`)

In `work/ui/server_queue.py` (`_evaluate_pass`) and `work/pipeline/stage2_scenes.py`:
- When evaluating `out_name` for `sid` (`pass_num`), if the active file is missing or `prev_h not in curr_hashes`:
  - Call `try_auto_promote_vault_hit(proj_dir, sid, pass_num, out_name, curr_hashes, sc_start, sc_end, cur_anchor_hash, state, state_file)`.
  - This function checks `takes_manifest.json` (in $<1\text{ms}$ in-memory cached lookup):
    1. Is there a take for `sid` (`pass_num`) where `take["out_name"] == out_name` and `take["task_hash"] in curr_hashes`?
    2. Does the vault MP4 exist on disk with `st_size > 50000`?
    3. Do `take["render_start"] <= sc_start + 0.05` and `take["render_end"] >= sc_end - 0.05` cover the current scene bounds?
    4. Does `take["anchor_sha256"]` match `cur_anchor_hash` (if an anchor is used)?
  - If all 4 conditions hold:
    - It calls `promote_take_to_active(...)` automatically!
    - `_evaluate_pass` immediately returns `("completed", "UP-TO-DATE", False, cur_task_hash, cur_anchor_mtime)`!

### 3.4 Explicit Take Promotion (`promote_take_to_active`)

When the user selects an earlier take in the UI and clicks **`⏪ Restore This Take`**:
1. Load `project.json`, `state_hashes.json`, and `takes_manifest.json`.
2. Backup `project.json` via `backup_project_json`.
3. Update the target scene `sc` in `project.json` from `take["scene_snapshot"]`:
   - If `pass_num == 1` (Two-Pass Pass 1 only): restore `prompt_m`, `seed_m`, `stem_m`, `anchor`, `split_x`.
   - If `pass_num == 2` (Two-Pass Pass 2 only): restore `prompt_f`, `seed_f`, `stem_f`, `renderer` (and if `parent_p1_task_hash` differs from current Pass 1 and exists in vault, optionally restore Pass 1 too).
   - If `pass_num is None` (Single-Pass or full scene restore): restore `mode`, `prompt`, `prompt_m`, `prompt_f`, `seed`, `seed_m`, `seed_f`, `stem`, `stem_m`, `stem_f`, `split_x`, `anchor`, `renderer`, `image_refs`.
   - **Preserve `sc["start"]` and `sc["end"]`** (unless `restore_timing=True` is explicitly requested and does not overlap neighbors).
4. Link `vault_rel_mp4` $\rightarrow$ `renders_v3/{out_name}.mp4` using `safe_link_or_copy`.
5. Link `vault_rel_strip` $\rightarrow$ `renders_v3/{out_name}_strip.jpg` (if present) and remove stale dynamic strip cache files (`{out_name}_strip_*f.jpg`) so the timeline immediately shows the restored take's filmstrip.
6. Harmonize `os.utime(active_mp4, (safe_mtime, safe_mtime))` so `st_mtime` is newer than the anchor, reference images, and Pass 1 MP4.
7. Update `state_hashes.json`:
   - `task_hashes[out_name] = take["task_hash"]`
   - `settings_store[out_name] = take["render_settings"]`
   - `scene_renders[out_name] = {"render_start": take["render_start"], "render_end": take["render_end"], "frames": take["frames"], "elapsed": take["elapsed_s"], "completed_at": take["created_at"]}`
8. Save `project.json`, `state_hashes.json`, and `takes_manifest.json`, then recompute the fast project queue.

---

## 4. Project Checkpoints & Rollback (`work/pipeline/takes.py`)

### 4.1 Automatic Deduplicated Pre-Render Checkpoints
- Right before `server_workers.py` launches `run_queue_worker` or `execute_single_render`:
  - Call `create_project_checkpoint(proj_dir, name=f"Auto: Before render ({target_desc})", trigger="pre_render")`.
  - **Spam Prevention (Deduplication):** Compute a SHA-256 fingerprint of `(project.json bytes + state_hashes["task_hashes"])`. If the most recent checkpoint in `checkpoints/index.json` already has the identical state fingerprint, skip creating a duplicate checkpoint and return the existing checkpoint ID.
  - Retain up to `MAX_AUTO_CHECKPOINTS = 30` automatic checkpoints (manual checkpoints are never auto-pruned).

### 4.2 Rollback Execution (`rollback_project_checkpoint`)
When the user clicks **`⏪ Rollback`** on a checkpoint `ckpt_id`:
1. **Safety Net First:** Automatically create a checkpoint of the current state (`trigger="pre_rollback"`, `name="Auto: State before rollback to <ckpt_name>"`) so the user can even undo a rollback!
2. Restore `project.json` from `checkpoints/<ckpt_id>/project.json`.
3. Restore `state_hashes.json` from `checkpoints/<ckpt_id>/state_hashes.json`.
4. Iterate over `active_takes.json` from the checkpoint:
   - For each `out_name -> take_id`, call `promote_take_to_active` (file link + `mtime` harmonization) so every scene's active MP4 on disk matches the checkpoint state.
5. Recompute `compute_fast_project_queue(slug, force_recheck=True)` and return the restored project payload + queue to the UI.

---

## 5. REST API Specification (`work/ui/server_routes.py` & `server_media.py`)

### 5.1 Endpoints in `server_routes.py`

| Method & Path | Request / Query Params | Response Summary |
| :--- | :--- | :--- |
| `GET /api/takes/<slug>/<scene_id>` | Optional `?pass=1` or `?pass=2` | Returns `{ "scene_id": "S01", "active_takes": {...}, "takes": [...] }` (seeds baseline takes first if needed). |
| `POST /api/takes/activate` | `{ "slug": "...", "scene_id": "S01", "take_id": "take_..." }` | Promotes take, updates scene settings & hashes, returns `{ "success": true, "scene": {...}, "queue": [...] }`. |
| `GET /api/checkpoints/<slug>` | — | Returns `{ "checkpoints": [...], "vault_stats": { "total_takes": 38, "disk_mb": 412.5 } }`. |
| `POST /api/checkpoints/create` | `{ "slug": "...", "name": "...", "note": "..." }` | Creates manual checkpoint, returns `{ "success": true, "checkpoint": {...} }`. |
| `POST /api/checkpoints/rollback` | `{ "slug": "...", "checkpoint_id": "ckpt_..." }` | Creates safety snapshot, restores checkpoint & takes, returns `{ "success": true, "project": {...}, "queue": [...] }`. |

### 5.2 Take Preview Streaming in `server_media.py`
In `work/ui/server_media.py` (`serve_media`), parse query parameter `?take=<take_id>`:
- When `category in ("clip", "strip")` and `take_id` is provided:
  - Look up `take_id` in `takes_manifest.json` for `item_id` (`scene_id`).
  - Serve `proj_dir / "work" / "renders_v3" / take["vault_rel_mp4"]` (with full HTTP Range support for video scrubbing!).

---

## 6. Frontend UI / UX Specification (`work/ui/static/js/takes_and_history.js`)

### 6.1 Transport Bar: Undo / Redo & Checkpoints Button (`index.html`)
Placed in the timeline header transport bar right before the Zoom slider:
```html
<div style="display: flex; align-items: center; gap: 4px; border-right: 1px solid var(--border-color); padding-right: 10px; margin-right: 4px;">
  <button class="btn" id="btnTimelineUndo" title="Undo last project change (Ctrl+Z)" disabled style="padding: 4px 8px;">↶ Undo</button>
  <button class="btn" id="btnTimelineRedo" title="Redo project change (Ctrl+Y)" disabled style="padding: 4px 8px;">↷</button>
  <button class="btn" id="btnOpenCheckpoints" title="Project Checkpoints & Rollback History" style="padding: 4px 8px;">
    🕒 History <span id="checkpointCountBadge" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border-radius: 8px; padding: 0 5px; font-size: 10px; margin-left: 3px;">0</span>
  </button>
</div>
```

### 6.2 Scene Inspector: Takes & Versions Tray (`index.html`)
Inserted in the Scene Inspector right above `#singlePromptGroup`:
```html
<div class="form-group" id="sceneTakesTray" style="background: rgba(15, 23, 42, 0.55); border: 1px solid var(--border-color); border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; display: none;">
  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
    <label style="margin: 0; font-size: 11px; color: #38bdf8; font-weight: 600;">
      🎬 Recorded Takes <span id="sceneTakesCountBadge" style="color: var(--text-muted); font-weight: normal;">(1)</span>
    </label>
    <span id="sceneTakePreviewBadge" style="display: none; font-size: 10px; background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); padding: 1px 6px; border-radius: 4px;">
      👁️ Previewing Historical Take
    </span>
  </div>
  <div style="display: flex; gap: 6px; align-items: center;">
    <select id="sceneTakeSelect" style="flex: 1; font-size: 11px; font-family: monospace;"></select>
    <button class="btn btn-primary" id="btnRestoreTake" style="display: none; font-size: 11px; padding: 4px 10px; white-space: nowrap;">
      ⏪ Restore This Take
    </button>
  </div>
  <div id="sceneTakeDiffSummary" style="display: none; font-size: 10px; color: var(--text-muted); margin-top: 6px; padding-top: 6px; border-top: 1px dashed rgba(255,255,255,0.1); line-height: 1.4;"></div>
</div>
```

#### Interactive Workflow:
1. When a scene is selected, `takes_and_history.js` fetches `/api/takes/<slug>/<scene_id>`.
2. If $\ge 1$ take exists, `#sceneTakesTray` is shown. The active take is marked `★ Take 2 (Active) — 18:45 [Seed 9140, vace_14B]`.
3. Selecting an earlier take from `#sceneTakeSelect`:
   - Loads `/media/<slug>/clip/<sid>?take=<take_id>` into `#clipVideoPlayer` so the user can watch the old take immediately.
   - Displays `#sceneTakeDiffSummary` showing how that take differs from current settings (e.g., `Seed: 8201 (current: 9140) | Prompt: "Marcus leans into the brass..."`).
   - Reveals the **`⏪ Restore This Take`** button.
4. Clicking **`⏪ Restore This Take`** posts to `/api/takes/activate`, updates the scene in `state.projectData`, refreshes the inspector inputs, reloads the timeline filmstrip, and shows a toast: `⏪ Restored S01 to Take 1 (Up to date — no re-render needed)`.

---

## 7. Strict Line-Budget & Modularity Plan

All existing files are close to the 850-line ceiling (`scene_inspector.js`: 845, `timeline_and_playback.js`: 844, `server_queue.py`: 806). Also, `stage2_scenes.py` is currently 1,035 lines and should have its take hooks kept to minimal 2-line delegations:

| File | Current Lines | Planned Change | Final Lines | Limit Check |
| :--- | :--- | :--- | :--- | :--- |
| **`work/pipeline/takes.py`** *(NEW)* | 0 | Full Render Vault, baseline seeding, hardlinks, cache-hit auto-promotion, checkpoints & rollback | ~380 | ✅ `< 850` |
| **`work/ui/static/js/takes_and_history.js`** *(NEW)* | 0 | Undo/Redo ring buffer, `Ctrl+Z`/`Ctrl+Y`, Scene Takes tray, take preview & diff, Checkpoints modal | ~360 | ✅ `< 850` |
| `work/ui/server_media.py` | 437 | Support `?take=<take_id>` in `serve_media` for clip & strip preview | ~458 | ✅ `< 850` |
| `work/ui/server_workers.py` | 474 | Call `create_project_checkpoint(..., trigger="pre_render")` before worker starts | ~482 | ✅ `< 850` |
| `work/ui/server_routes.py` | 767 | Add `/api/takes/*` and `/api/checkpoints/*` thin route handlers delegating to `takes.py` | ~815 | ✅ `< 850` |
| `work/ui/server_queue.py` | 806 | 3-line call to `try_auto_promote_vault_hit` in `_evaluate_pass` | ~812 | ✅ `< 850` |
| `work/ui/static/js/scene_inspector.js` | 845 | 1-line hook `if (window.updateSceneTakesUI) window.updateSceneTakesUI(sc);` in `selectScene` | 846 | ✅ `< 850` |
| `work/ui/static/index.html` | 542 | Transport Undo/Redo/History buttons, Scene Takes tray, Checkpoints modal, `<script src="js/takes_and_history.js">` | ~605 | ✅ `< 850` |
| **`tests/test_takes_and_rollback.py`** *(NEW)* | 0 | End-to-end unit & user-journey tests for takes, hardlink safety, cache-hits, and checkpoint rollback | ~320 | ✅ `< 850` |

---

## 8. Verification & Test Suite Specification (`tests/test_takes_and_rollback.py`)

1. **`test_01_baseline_takes_retroactive_seeding`**:
   - Given an existing project with rendered MP4s in `renders_v3/`, calling `ensure_baseline_takes_seeded()` registers `Take 1 (Baseline)` in `takes_manifest.json` and creates hardlinks in `renders_v3/takes/<sid>/` without duplicating disk space.
2. **`test_02_hardlink_unlink_before_write_safety`**:
   - Verify that re-rendering a scene (`--dummy`) archives Take 1, unlinks `out_mp4` before writing Take 2, and leaves Take 1's vault MP4 100% intact with its original byte size and content.
3. **`test_03_activate_previous_take_zero_rerender`**:
   - Render Take 1 (Seed 100), edit seed to 200 and render Take 2. Call `/api/takes/activate` for Take 1.
   - Verify: `sc["seed"] == 100`, `out_mp4` matches Take 1's bytes, `st_mtime` is harmonized above anchor `mtime`, `scene_renders[out_name]["completed_at"]` equals Take 1's timestamp, and `compute_fast_project_queue` reports `status == "completed"`, `needs_render == False`.
4. **`test_04_automatic_vault_cache_hit_on_manual_revert`**:
   - With Take 1 (Seed 100) and Take 2 (Seed 200) in the vault, manually edit `sc["seed"] = 100` in `project.json` and run `compute_fast_project_queue()`.
   - Verify that `_evaluate_pass` detects the vault hit, auto-promotes Take 1's MP4, and marks the scene `completed` (`UP-TO-DATE`) with zero GPU work.
5. **`test_05_project_checkpoint_create_and_rollback`**:
   - Create checkpoint `C1`, modify 3 scenes (prompts, timings, seeds) and render new takes, then call `/api/checkpoints/rollback` to `C1`.
   - Verify a `pre_rollback` safety checkpoint is created, all 3 scenes revert to `C1` configuration and `C1` MP4s, and 0 scenes require rendering.
6. **`test_06_canceled_or_failed_render_preserves_active_take`**:
   - Simulate a failed/canceled render after `before_scene_render` unlinks `out_mp4`; verify `restore_previous_active_take_if_missing` restores Take 1 immediately so the scene never loses its active video.
