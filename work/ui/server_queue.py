"""Queue computation, state validation, and scene ordering for Studio UI."""

from __future__ import annotations
import datetime, hashlib, json, re, shutil, sys, time
from pathlib import Path
try:
    from pipeline.project_paths import resolve_project_dirs, resolve_queue_file, resolve_state_hashes_paths
except ImportError:
    from work.pipeline.project_paths import resolve_project_dirs, resolve_queue_file, resolve_state_hashes_paths

try:
    from pipeline.context import ProjectContext as _DEF_PC, derive_split_scene_ids, derive_duplicate_scene_id, split_scene_in_project_data, delete_scene_in_project_data, duplicate_scene_in_project_data, duplicate_scene_with_takes, resolve_scene_padding
    from pipeline.stage2_hashes import compute_project_task_hashes as _DEF_CPTH
    from pipeline.takes import ensure_baseline_takes_seeded, try_auto_promote_vault_hit
    from pipeline.media_utils import attach_queue_time_estimates, resolve_rendered_clip_path as _DEF_RRCP
except ImportError:
    try:
        from work.pipeline.context import ProjectContext as _DEF_PC, derive_split_scene_ids, derive_duplicate_scene_id, split_scene_in_project_data, delete_scene_in_project_data, duplicate_scene_in_project_data, duplicate_scene_with_takes, resolve_scene_padding
        from work.pipeline.stage2_hashes import compute_project_task_hashes as _DEF_CPTH
        from work.pipeline.takes import ensure_baseline_takes_seeded, try_auto_promote_vault_hit
        from work.pipeline.media_utils import attach_queue_time_estimates, resolve_rendered_clip_path as _DEF_RRCP
    except ImportError:
        from run_overnight_v3 import ProjectContext as _DEF_PC, compute_project_task_hashes as _DEF_CPTH, resolve_rendered_clip_path as _DEF_RRCP, derive_split_scene_ids, derive_duplicate_scene_id, split_scene_in_project_data, delete_scene_in_project_data, duplicate_scene_in_project_data, duplicate_scene_with_takes, resolve_scene_padding
        ensure_baseline_takes_seeded = try_auto_promote_vault_hit = attach_queue_time_estimates = None
try:
    from ui.server_fal import enrich_queue_with_fal_metadata
    from ui.server_engines import active_rendering_map, dedupe_rendering_queue_items, extract_backend_overrides, read_queue_json_locked, reset_session_blocks_for_items, write_queue_json_locked
    from ui.server_queue_history import attach_queue_take_metadata, record_done_items
    from ui.server_security import resolve_project_dir_safe
except ImportError:
    try:
        from work.ui.server_fal import enrich_queue_with_fal_metadata
        from work.ui.server_engines import active_rendering_map, dedupe_rendering_queue_items, extract_backend_overrides, read_queue_json_locked, reset_session_blocks_for_items, write_queue_json_locked
        from work.ui.server_queue_history import attach_queue_take_metadata, record_done_items
        from work.ui.server_security import resolve_project_dir_safe
    except ImportError:
        from server_engines import active_rendering_map, dedupe_rendering_queue_items, extract_backend_overrides, read_queue_json_locked, reset_session_blocks_for_items, write_queue_json_locked
        from server_queue_history import attach_queue_take_metadata, record_done_items
        from server_security import resolve_project_dir_safe
        try: from server_fal import enrich_queue_with_fal_metadata
        except ImportError: enrich_queue_with_fal_metadata = None


def _srv():
    key = f"{__package__}.server" if __package__ else "server"
    mod = sys.modules.get(key) or sys.modules.get("work.ui.server") or sys.modules.get("ui.server") or sys.modules.get("server")
    if mod is not None: return mod
    try: from . import server; return server
    except ImportError:
        try: from work.ui import server; return server
        except ImportError: import server; return server


def extract_anchor_id(name_or_str: str | Path) -> str:
    """Extracts normalized anchor ID like 'A01', 'A05b' ... from any filename or string."""
    s = Path(str(name_or_str)).name.strip()
    m = re.match(r"^A(\d{1,2})([a-z]?)(?:[ _\-\.]|$)", s, re.IGNORECASE)
    if m: return f"A{int(m.group(1)):02d}{(m.group(2) or '').lower()}"
    parts = re.split(r"[-_ \.]", s)
    m2 = re.match(r"^A(\d+)([a-z]?)$", parts[0].strip(), re.IGNORECASE) if parts else None
    return f"A{int(m2.group(1)):02d}{(m2.group(2) or '').lower()}" if m2 else s.upper()


def backup_project_json(json_path: Path, throttle_seconds: float = 0.0):
    srv = _srv()
    json_path = Path(json_path)
    if not json_path.exists(): return
    now, key = time.time(), str(json_path.resolve())
    if throttle_seconds > 0 and key in srv.last_backup_times and (now - srv.last_backup_times[key] < throttle_seconds):
        return
    srv.last_backup_times[key] = now
    srv.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(json_path, srv.BACKUP_DIR / f"{json_path.stem}_{ts}.json")


def resolve_project_resolution(data: dict, override_val=None) -> str:
    models = data.get("models", {})
    vshared = models.get("video_shared_settings", {}) if isinstance(models.get("video_shared_settings"), dict) else {}
    has_ovr = override_val is not None and str(override_val).strip().lower() not in ("", "default", "inherit", "none", "null")
    raw = override_val if has_ovr else (
        models.get("default_video_resolution") or models.get("resolution") or vshared.get("resolution")
        or data.get("project", {}).get("master_resolution") or "4"
    )
    res_map = {"1": "832x480", "2": "960x544", "3": "1280x720", "4": "1344x768", "5": "1536x864", "6": "1920x1088"}
    s = str(raw).strip()
    return res_map.get(s, s.lower() if "x" in s.lower() else "1344x768")


def _strip_res_suffix(name: str) -> str:
    return re.sub(r"_\d+x\d+$", "", str(name or "").strip())


def resolve_scene_queue_resolution(data: dict, sc: dict | None, default_res: str) -> str:
    sc_res = sc.get("resolution") if isinstance(sc, dict) else None
    if sc_res is not None and str(sc_res).strip().lower() not in ("", "default", "inherit", "none", "null"):
        return resolve_project_resolution(data, override_val=sc_res)
    return default_res


def collect_project_clips(proj_dir: Path, data: dict) -> dict[str, dict]:
    """Collect rendered clips across renders_dir and trimmed_dir, prioritizing
    Pass2_Final over Pass1, renders with audio over trimmed, and strictly
    excluding placeholders from scene mapping.
    """
    proj_dir = Path(proj_dir)
    trimmed_dir = proj_dir / data.get("project", {}).get("directories", {}).get("trimmed", "work/trimmed")
    renders_dir = proj_dir / data.get("project", {}).get("directories", {}).get("renders", "work/renders")

    clip_search_dirs = [trimmed_dir, renders_dir]

    def _clip_score(cname: str, cpath: str) -> int:
        low_name, low_path = cname.lower(), cpath.lower()
        if "placeholder" in low_name: return -100
        if "pass2_final" in low_name: return 50
        if "pass2" in low_name: return 45
        if "renders" in low_path and "pass1" not in low_name: return 40
        if "pass1" in low_name: return 30
        return 20 if "trimmed" in low_path else 10

    existing_clips = {}
    canonical_scores = {}
    scanned_clip_dirs = set()
    scene_renders = {}
    for hf in resolve_state_hashes_paths(proj_dir):
        if hf.is_file():
            try: scene_renders = json.loads(hf.read_text(encoding="utf-8")).get("scene_renders", {}); break
            except Exception: pass
    for cdir in clip_search_dirs:
        if cdir.is_dir():
            rcdir = str(cdir.resolve())
            if rcdir in scanned_clip_dirs:
                continue
            scanned_clip_dirs.add(rcdir)
            for clip in cdir.glob("*.mp4"):
                if clip.name.endswith(".tmp.mp4"):
                    continue
                rel_path = clip.relative_to(proj_dir).as_posix()
                score = _clip_score(clip.name, rel_path)
                info = {
                    "filename": clip.name, "path": rel_path,
                    "size_bytes": clip.stat().st_size, "mtime": clip.stat().st_mtime,
                }
                r_inf = scene_renders.get(clip.stem)
                if r_inf and "render_start" in r_inf:
                    info["render_start"], info["render_end"] = r_inf["render_start"], r_inf.get("render_end")
                if clip.name not in existing_clips:
                    existing_clips[clip.name] = info

                # Exclude placeholders from canonical scene ID mapping
                if "placeholder" in clip.name.lower():
                    continue

                m = re.search(r"(?:^|_)(S\d{1,2}[a-z]?)(?:[_\.]|$)", clip.name, re.IGNORECASE)
                if m:
                    matched_sid = m.group(1)
                    for sid_variant in (matched_sid, matched_sid.upper(), matched_sid.lower()):
                        prev_score = canonical_scores.get(sid_variant, -999)
                        prev_info = existing_clips.get(sid_variant)
                        should_replace = False
                        if prev_info is None or score > prev_score:
                            should_replace = True
                        elif score == prev_score and info["mtime"] > prev_info["mtime"]:
                            should_replace = True

                        if should_replace:
                            canonical_scores[sid_variant] = score
                            existing_clips[sid_variant] = info

    try:
        from ui.server_media import apply_active_manifest_clips
    except ImportError:
        try: from work.ui.server_media import apply_active_manifest_clips
        except ImportError: from server_media import apply_active_manifest_clips
    apply_active_manifest_clips(proj_dir, existing_clips)

    return existing_clips


def compute_fast_project_queue(slug: str, force_recheck: bool = False) -> list[dict]:
    """Instantly evaluates all scenes in the project JSON against disk, anchor mtimes/hashes, and state hashes."""
    srv = _srv()
    lk = getattr(srv, "queue_file_lock", None)
    if lk is not None:
        with lk:
            return _compute_fast_project_queue_impl(slug, force_recheck=force_recheck)
    return _compute_fast_project_queue_impl(slug, force_recheck=force_recheck)


def _compute_fast_project_queue_impl(slug: str, force_recheck: bool = False) -> list[dict]:
    srv = _srv()
    proj_dir = srv.PROJECTS_DIR / slug
    if not proj_dir.is_dir(): return []
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
    if not jsons: return []
    config_file = jsons[-1]
    try: data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception: return []
    if ensure_baseline_takes_seeded:
        try: ensure_baseline_takes_seeded(proj_dir, config_file)
        except Exception: pass

    pdirs = resolve_project_dirs(proj_dir, config_data=data)
    renders_dir, anchors_dir, jobs_dir = pdirs["renders"], pdirs["anchors"], pdirs["jobs"]
    jobs_dir.mkdir(parents=True, exist_ok=True)
    queue_file, hashes_file = jobs_dir / "v3_queue.json", next((hf for hf in resolve_state_hashes_paths(proj_dir, config_data=data) if hf.is_file()), jobs_dir / "state_hashes.json")
    hashes, stored_anchor_hashes, scene_renders, hashes_changed = {}, {}, {}, [False]
    if hashes_file.is_file():
        try:
            h_data = json.loads(hashes_file.read_text(encoding="utf-8"))
            hashes, stored_anchor_hashes, scene_renders = (h_data.get("tasks") or h_data.get("task_hashes") or {}), h_data.get("anchor_file_hashes", {}), h_data.get("scene_renders", {})
        except Exception: pass
    manifest_scenes, mf_path = {}, proj_dir / "work" / "takes_manifest.json"
    if mf_path.is_file():
        try: manifest_scenes = json.loads(mf_path.read_text(encoding="utf-8")).get("scenes", {})
        except Exception: pass
    def _active_take(sc_id: str, p_num):
        se = manifest_scenes.get(sc_id) or {}
        tid = (se.get("active_takes") or {}).get("p1" if p_num == 1 else ("p2" if p_num == 2 else "default"))
        tks = se.get("takes") or []
        return next((x for x in tks if x.get("take_id") == tid), None) if tid else None

    # Scan anchor files on disk in anchors_dir
    disk_anchors: dict[str, dict] = {}
    if anchors_dir.exists():
        for p in anchors_dir.iterdir():
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                aid = extract_anchor_id(p.name)
                if re.match(r"^A\d{2}[a-z]?$", aid):
                    try:
                        disk_anchors[aid] = {"path": p, "mtime": p.stat().st_mtime, "hash": hashlib.sha256(p.read_bytes()).hexdigest()[:16]}
                    except Exception: pass

    scenes = data.get("scenes", [])
    scene_by_id = {sc.get("id"): sc for sc in scenes if sc.get("id")}
    default_res = resolve_project_resolution(data)

    # Build canonical map of valid passes for the current project scenes and modes
    valid_passes: dict[str, dict] = {}
    valid_by_key: dict[tuple[str, int | None], tuple[str, dict]] = {}
    valid_p1_for_sid: dict[str, str] = {}
    for sc in scenes:
        sid = sc.get("id", "")
        if not sid: continue
        mode, res_str = sc.get("mode", "solo"), resolve_scene_queue_resolution(data, sc, default_res)
        if mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
            p1_name, p2_name = f"{sid}_Pass1_Left_{res_str}", f"{sid}_Pass2_Final_{res_str}"
            valid_p1_for_sid[sid] = p1_name
            valid_passes[p1_name] = {"scene_id": sid, "target_arg": f"{sid}:p1", "pass": 1, "resolution": res_str}
            valid_passes[p2_name] = {"scene_id": sid, "target_arg": f"{sid}:p2", "pass": 2, "resolution": res_str}
            valid_by_key[(sid.upper(), 1)], valid_by_key[(sid.upper(), 2)] = (p1_name, valid_passes[p1_name]), (p2_name, valid_passes[p2_name])
        else:
            if mode in ("duet_method_a", "duet_a"):
                cand = next((c for c in (f"{sid}_duet_a", f"{sid}_duet_method_a", f"{sid}_duet_stereo") if (renders_dir / f"{c}_{res_str}.mp4").exists()), None) if not sc.get("output_name") else None
                raw_name = sc.get("output_name", cand or f"{sid}_duet_a")
            else:
                def_mode = "male_solo" if mode == "male_solo" else ("female_solo" if mode == "female_solo" else ("silent" if mode in ("silent_locked", "silent", "ambient") else mode))
                cand = next((c for c in (f"{sid}_{def_mode}", f"{sid}_solo", f"{sid}_male_solo", f"{sid}_female_solo") if (renders_dir / f"{c}_{res_str}.mp4").exists()), None) if not sc.get("output_name") and mode in ("solo", "male_solo", "female_solo") else None
                raw_name = sc.get("output_name", cand or f"{sid}_{def_mode}")
            out_fn = f"{_strip_res_suffix(raw_name)}_{res_str}"
            valid_passes[out_fn] = {"scene_id": sid, "target_arg": sid, "pass": None, "resolution": res_str}
            valid_by_key[(sid.upper(), None)] = (out_fn, valid_passes[out_fn])
    existing_items = read_queue_json_locked(srv, queue_file)
    normalize_queue_units(existing_items, srv)
    b_overrides, task_hashes_current, ctx = extract_backend_overrides(existing_items), {}, None
    ProjectContext = getattr(srv, "ProjectContext", None) or _DEF_PC
    compute_project_task_hashes = getattr(srv, "compute_project_task_hashes", None) or _DEF_CPTH
    resolve_rendered_clip_path = getattr(srv, "resolve_rendered_clip_path", None) or _DEF_RRCP
    if ProjectContext and compute_project_task_hashes and config_file.is_file():
        try:
            ctx = ProjectContext(config_file)
            task_hashes_current = compute_project_task_hashes(ctx, backend_overrides=b_overrides) if b_overrides else compute_project_task_hashes(ctx)
        except Exception as _hash_err:
            if hasattr(srv, "broadcast_log"): srv.broadcast_log(f"[Queue Warning] Failed computing task hashes: {_hash_err}")
    queue, seen, seen_keys, p1_needs_rerender, done_snaps, _anc_seed_cache = [], set(), set(), {}, [], {}
    live_eng = srv.read_project_engine_status(slug)
    is_engine_active, active_eng_map = active_rendering_map(srv, slug, legacy_eng=live_eng)

    # Helper to evaluate pass state
    def _evaluate_pass(out_name: str, sid: str, pass_num: int | None, current_item: dict | None = None) -> tuple[str, str, bool, str, float]:
        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        file_exists = mp4_path.exists() and mp4_path.stat().st_size > 50000
        curr_hashes = task_hashes_current.get(out_name)
        cur_task_hash = curr_hashes[0] if curr_hashes else ""
        sc = scene_by_id.get(sid)
        aid = extract_anchor_id(sc.get("anchor", "")) if sc else ""
        aid_end = extract_anchor_id(sc.get("anchor_end", "")) if (sc and pass_num != 2 and sc.get("anchor_end")) else ""
        anchor_info, end_info = disk_anchors.get(aid), (disk_anchors.get(aid_end) if aid_end else None)
        cur_anchor_mtime = max(float(anchor_info["mtime"]) if anchor_info else 0.0, float(end_info["mtime"]) if end_info else 0.0)
        # Preserve active rendering status for any pass actively rendering on any engine
        active_st = active_eng_map.get(out_name) or active_eng_map.get(f"{out_name}.mp4")
        if is_engine_active and (active_st is not None or (not active_eng_map and current_item and current_item.get("status") == "rendering")):
            ren_reason = (active_st.get("reason") if isinstance(active_st, dict) else None) or (current_item.get("reason") if current_item else None) or "RENDERING"
            return "rendering", str(ren_reason), True, cur_task_hash, cur_anchor_mtime

        # Preserve explicit user curation (FORCED/TARGETED) across rechecks; preserve failed/skipped when unchanged
        if current_item:
            cur_status, cur_reason = current_item.get("status"), str(current_item.get("reason") or "")
            if cur_status == "queued" and (cur_reason == "FORCED" or "FORCED)" in cur_reason or cur_reason.startswith("TARGETED")):
                return "queued", cur_reason, True, cur_task_hash, cur_anchor_mtime
            if not force_recheck:
                if cur_status == "failed" and sid not in p1_needs_rerender and (not cur_task_hash or current_item.get("task_hash") == cur_task_hash):
                    return "failed", cur_reason or "FAILED", True, cur_task_hash, cur_anchor_mtime
                if cur_status == "skipped" and cur_reason == "SKIPPED BY USER":
                    s_h, s_m = current_item.get("user_skipped_hash"), float(current_item.get("user_skipped_anchor_mtime") or 0.0)
                    if (s_h == cur_task_hash) and (not anchor_info or not s_m or cur_anchor_mtime <= s_m + 0.001):
                        return "skipped", "SKIPPED BY USER", False, cur_task_hash, cur_anchor_mtime

        if not file_exists:
            return "queued", "MISSING FILE", True, cur_task_hash, cur_anchor_mtime

        if pass_num == 2:
            if sid in p1_needs_rerender: return "queued", f"PASS 1 RE-RENDERED ({p1_needs_rerender[sid]})", True, cur_task_hash, cur_anchor_mtime
            p1_name = valid_p1_for_sid.get(sid)
            if p1_name:
                p1_mp4 = resolve_rendered_clip_path(renders_dir, p1_name) if resolve_rendered_clip_path else (renders_dir / f"{p1_name}.mp4")
                if p1_mp4.exists() and p1_mp4.stat().st_size > 50000 and mp4_path.stat().st_mtime < p1_mp4.stat().st_mtime:
                    t_p1, t_p2 = _active_take(sid, 1), _active_take(sid, 2)
                    p2_built_after = bool(t_p1 and t_p2 and float(t_p2.get("created_at") or 0) >= float(t_p1.get("created_at") or 0) and (not t_p2.get("parent_p1_task_hash") or t_p2.get("parent_p1_task_hash") == t_p1.get("task_hash")))
                    if not p2_built_after: return "queued", "PASS 1 NEWER ON DISK", True, cur_task_hash, cur_anchor_mtime
        if sc:
            has_custom = any(sc.get(k) is not None for k in ("render_pad_start", "render_pad_end", "pad_start", "pad_end", "render_overlap", "head_overlap", "tail_overlap"))
            h_pad, t_pad = (ctx.get_scene_render_padding(sc) if ctx else resolve_scene_padding(sc)) if has_custom else (0.0, 0.0)
            req_s, req_e = max(0.0, float(sc.get("start", 0)) - h_pad), float(sc.get("end", 0)) + t_pad
            r_info, allow_pad = scene_renders.get(out_name), bool(sc.get("hold_last_frame") or sc.get("pad_tail") or sc.get("allow_tail_pad"))
            if r_info and "render_start" in r_info and "render_end" in r_info:
                if not ((float(r_info["render_start"]) <= req_s + 0.05) and (allow_pad or float(r_info["render_end"]) >= req_e - 0.05)):
                    return "queued", "TIMELINE CHANGED", True, cur_task_hash, cur_anchor_mtime
        for a_id, a_inf in ((aid, anchor_info), (aid_end, end_info)):
            if a_id and a_inf and mp4_path.stat().st_mtime < a_inf["mtime"]:
                return "queued", f"ANCHOR {a_id} NEWER ON DISK", True, cur_task_hash, cur_anchor_mtime
            if a_id and a_inf and stored_anchor_hashes.get(a_id) and a_inf["hash"] != stored_anchor_hashes.get(a_id):
                act_tk = _active_take(sid, pass_num)
                already_cur = bool(act_tk and ((act_tk.get("anchor_sha256") == a_inf["hash"]) or (not act_tk.get("anchor_sha256") and float(act_tk.get("created_at") or 0) >= a_inf["mtime"])))
                if not already_cur: return "queued", f"ANCHOR {a_id} EDITED", True, cur_task_hash, cur_anchor_mtime

        # Check character reference images if model supports reference images
        if ctx and sc:
            try:
                vrid, vrspec = ctx.resolve_video_renderer(sc, pass_num=pass_num)
                if vrspec.get("supports_image_refs") or any(k in vrspec.get("model_type", "").lower() for k in ("ref2va", "fasth3", "msr")):
                    from pipeline.prompts import compile_modular_scene_prompt
                    from pipeline.ltx_refs import collect_scene_active_ref_paths
                    raw_p = compile_modular_scene_prompt(sc, pass_num=pass_num)
                    for r_path in collect_scene_active_ref_paths(ctx, sc, vrspec, raw_p, pass_num=pass_num):
                        if r_path.exists() and mp4_path.stat().st_mtime < r_path.stat().st_mtime:
                            return "queued", f"REF {r_path.name} NEWER ON DISK", True, cur_task_hash, cur_anchor_mtime
            except Exception:
                pass

        # Check if prompt, seed, stem, renderer, or settings have been edited
        if curr_hashes:
            prev_h = hashes.get(out_name)
            if prev_h is not None:
                if prev_h not in curr_hashes:
                    promoted = False
                    if try_auto_promote_vault_hit and cur_task_hash:
                        try:
                            if try_auto_promote_vault_hit(proj_dir, config_file, sid, pass_num, cur_task_hash, curr_hashes=curr_hashes):
                                file_exists = mp4_path.exists() and mp4_path.stat().st_size > 50000
                                if file_exists:
                                    hashes[out_name] = cur_task_hash
                                    hashes_changed[0] = True
                                    promoted = True
                        except Exception:
                            pass
                    if promoted:
                        return "completed", "UP-TO-DATE (VAULT HIT)", False, cur_task_hash, cur_anchor_mtime
                    else:
                        return "queued", "PROMPT/SEED/SETTINGS EDITED", True, cur_task_hash, cur_anchor_mtime
            else:
                hashes[out_name] = cur_task_hash
                hashes_changed[0] = True
        elif cur_task_hash and out_name not in hashes:
            hashes[out_name] = cur_task_hash
            hashes_changed[0] = True

        # File exists and is up-to-date with anchor and settings
        return "completed", "UP-TO-DATE", False, cur_task_hash, cur_anchor_mtime

    # 1. Preserve existing user-ordered items; if resolution changed, replace in-place cleanly (never leave stale resolution entries)
    for item in existing_items:
        old_name = item.get("output_filename")
        if item.get("stage") == "anchor":
            from pipeline.anchor_takes import reconcile_existing_anchor_queue_item
            if old_name and old_name not in seen and reconcile_existing_anchor_queue_item(item, is_engine_active, active_eng_map, force_recheck, done_snaps, anchors_dir=anchors_dir, seed_cache=_anc_seed_cache):
                seen.add(old_name); queue.append(item)
            continue
        sid_guess = str(item.get("scene_id") or (srv.derive_scene_id(old_name) if old_name else "") or "").strip().upper()
        p_guess = item.get("pass")
        if p_guess is None and old_name:
            p_guess = 1 if "pass1" in old_name.lower() else (2 if "pass2" in old_name.lower() else None)

        if old_name in valid_passes:
            out_name, spec = old_name, valid_passes[old_name]
        elif (sid_guess, p_guess) in valid_by_key:
            out_name, spec = valid_by_key[(sid_guess, p_guess)]
            if item.get("status") == "completed": done_snaps.append(dict(item))
            item.update({"id": out_name, "output_filename": out_name, "filename": f"{out_name}.mp4", "resolution": spec["resolution"]})
            for k in ("status", "reason", "completed_at", "elapsed", "user_skipped_hash", "user_skipped_anchor_mtime", "estimated_seconds", "estimated_str"):
                item.pop(k, None)
        else:
            continue

        sid, pass_num = spec["scene_id"], spec["pass"]
        key = (sid.upper(), pass_num)
        if out_name in seen or key in seen_keys:
            continue
        seen.add(out_name); seen_keys.add(key)
        sc = scene_by_id.get(sid)
        if sc:
            item["duration"] = round(float(sc.get("end", 0)) - float(sc.get("start", 0)), 2)
        item.update({"scene_id": sid, "target_arg": spec["target_arg"], "pass": pass_num, "resolution": spec["resolution"]})

        status, reason, needs_render, t_hash, a_mtime = _evaluate_pass(out_name, sid, pass_num, current_item=item)
        if item.get("status") == "completed" and status != "completed": done_snaps.append(dict(item))
        item.update({"status": status, "reason": reason, "needs_render": needs_render, "task_hash": t_hash, "anchor_mtime": a_mtime})
        if status != "failed":
            item.pop("error", None)
        if status != "skipped" or reason != "SKIPPED BY USER":
            item.pop("user_skipped_hash", None); item.pop("user_skipped_anchor_mtime", None)
        if status != "rendering":
            for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "estimated_remaining_seconds", "estimated_remaining_str"):
                item.pop(k, None)

        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        render_info = scene_renders.get(out_name) or {}
        if not item.get("completed_at"):
            item["completed_at"] = render_info.get("completed_at") or (round(mp4_path.stat().st_mtime, 3) if mp4_path.exists() and mp4_path.stat().st_size > 50000 else None)
        if not item.get("elapsed") and render_info.get("elapsed"): item["elapsed"] = render_info.get("elapsed")

        if pass_num == 1 and needs_render: p1_needs_rerender[sid] = reason
        queue.append(item)

    # 2. Add any project passes not already in the queue
    for out_name, spec in valid_passes.items():
        key = (spec["scene_id"].upper(), spec["pass"])
        if out_name in seen or key in seen_keys: continue
        seen.add(out_name); seen_keys.add(key); sid, tgt_arg, pass_num, res_str = spec["scene_id"], spec["target_arg"], spec["pass"], spec["resolution"]
        sc = scene_by_id.get(sid)
        dur_val = round(float(sc.get("end", 0)) - float(sc.get("start", 0)), 2) if sc else 0.0

        status, reason, needs_render, t_hash, a_mtime = _evaluate_pass(out_name, sid, pass_num)
        if pass_num == 1 and needs_render: p1_needs_rerender[sid] = reason

        mp4_path = resolve_rendered_clip_path(renders_dir, out_name) if resolve_rendered_clip_path else (renders_dir / f"{out_name}.mp4")
        completed_at = round(mp4_path.stat().st_mtime, 3) if mp4_path.exists() and mp4_path.stat().st_size > 50000 else None
        elapsed_val = None
        render_info = scene_renders.get(out_name)
        if render_info:
            if render_info.get("completed_at"): completed_at = render_info.get("completed_at")
            if render_info.get("elapsed"): elapsed_val = render_info.get("elapsed")

        queue.append({
            "idx": len(queue) + 1, "id": out_name, "scene_id": sid, "target_arg": tgt_arg,
            "pass": pass_num, "filename": f"{out_name}.mp4", "output_filename": out_name,
            "duration": dur_val, "completed_at": completed_at, "elapsed": elapsed_val,
            "status": status, "reason": reason, "needs_render": needs_render,
            "task_hash": t_hash, "anchor_mtime": a_mtime,
            "model_type": "minimax_h3_fl2va_pruned_pdd", "resolution": res_str,
        })

    dedupe_rendering_queue_items(queue, active_eng_map=active_eng_map, live_eng=live_eng)
    normalize_queue_units(queue, srv)
    for i, itm in enumerate(queue, 1):
        itm["idx"], itm["total"] = i, len(queue)
        eng_src = (active_eng_map.get(itm.get("filename")) or active_eng_map.get(itm.get("output_filename")) or live_eng) if is_engine_active else None
        if itm.get("status") == "rendering" and isinstance(eng_src, dict):
            eng_fn = eng_src.get("filename") or ""
            if not eng_fn or eng_fn in (itm.get("filename"), itm.get("output_filename")):
                for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "window_pct", "started_at"):
                    if k in eng_src and eng_src[k] is not None: itm[k] = eng_src[k]
        elif itm.get("status") != "rendering":
            for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "window_pct", "started_at", "estimated_remaining_seconds", "estimated_remaining_str"):
                itm.pop(k, None)

    if enrich_queue_with_fal_metadata:
        try: enrich_queue_with_fal_metadata(queue, scene_by_id, ctx=ctx, proj_dir=proj_dir, default_res=default_res)
        except Exception: pass
    if attach_queue_time_estimates:
        try: attach_queue_time_estimates(queue, scene_by_id, ctx=ctx, proj_dir=proj_dir, default_res=default_res, live_eng=live_eng, active_eng_map=active_eng_map if is_engine_active else None)
        except Exception: pass

    if hashes_changed[0] and hashes_file.is_file():
        try:
            h_data = json.loads(hashes_file.read_text(encoding="utf-8"))
            h_data.setdefault("tasks", {}).update(hashes); h_data.setdefault("task_hashes", {}).update(hashes)
            h_txt, alt_h = json.dumps(h_data, indent=2), proj_dir / "work" / "state_hashes.json"
            hashes_file.write_text(h_txt, encoding="utf-8")
            if alt_h != hashes_file and alt_h.is_file(): alt_h.write_text(h_txt, encoding="utf-8")
        except Exception: pass

    try:
        attach_queue_take_metadata(proj_dir, queue)
        write_queue_json_locked(srv, queue_file, queue)
        record_done_items(srv, proj_dir, done_snaps + [q for q in queue if q.get("status") == "completed"])
    except Exception: pass
    return queue


def is_scene_match(sid: str, out_fn: str, target_base: str) -> bool:
    """Exact scene ID match (S1 matches S01, never S10/S11)."""
    if not target_base:
        return False
    target_base = target_base.strip().upper()
    sid_clean = (sid or "").strip().upper()
    out_clean = (out_fn or "").strip().upper()
    if sid_clean == target_base or out_clean == target_base or out_clean.startswith(f"{target_base}_"):
        return True
    m_tgt = re.match(r"^S0*(\d+)([A-Z]?)$", target_base)
    if m_tgt:
        tgt_num, tgt_sub = int(m_tgt.group(1)), m_tgt.group(2)
        m_sid = re.match(r"^S0*(\d+)([A-Z]?)$", sid_clean)
        if m_sid and tgt_num == int(m_sid.group(1)) and tgt_sub == m_sid.group(2): return True
        m_out = re.match(r"^S0*(\d+)([A-Z]?)$", out_clean.split("_")[0])
        if m_out and tgt_num == int(m_out.group(1)) and tgt_sub == m_out.group(2): return True
    return False


def _group_queue_units(items: list[dict], srv=None) -> list[list[dict]]:
    """Groups queue items into atomic SceneUnits, keeping Pass 1 and Pass 2 of any two-pass scene contiguous
    in [Pass 1, Pass 2] order, and pins any unit containing an actively rendering task ahead of all queued units."""
    srv = srv or _srv()
    by_sid: dict[str, list[dict]] = {}
    units: list[list[dict]] = []
    for itm in items:
        out_fn = itm.get("output_filename") or itm.get("filename") or ""
        sid = str(itm.get("scene_id") or srv.derive_scene_id(out_fn) or "").strip()
        if sid: itm["scene_id"] = sid
        p_num = itm.get("pass")
        if p_num is None:
            p_num = 1 if "pass1" in out_fn.lower() else (2 if "pass2" in out_fn.lower() else None)
            if p_num is not None: itm["pass"] = p_num
        sid_k = sid.upper()
        if sid_k and p_num in (1, 2):
            if sid_k not in by_sid: by_sid[sid_k] = []; units.append(by_sid[sid_k])
            by_sid[sid_k].append(itm)
        else:
            units.append([itm])
    for u in by_sid.values():
        u.sort(key=lambda x: x.get("pass") or 0)
        if len(u) == 2:
            p1, p2 = u[0], u[1]
            if p2.get("status") == "rendering" and p1.get("status") in ("queued", "rendering"):
                p1.update({"status": "completed", "needs_render": False})
            elif p1.get("status") in ("queued", "rendering") or p1.get("needs_render"):
                if p2.get("status") not in ("queued", "rendering") or not p2.get("needs_render"):
                    r = p1.get("reason") or "QUEUED"
                    p2.update({"status": "queued", "needs_render": True, "reason": f"PASS 1 RE-RENDERED ({r})"})
                    p2.pop("user_skipped_hash", None); p2.pop("user_skipped_anchor_mtime", None)
    ren_idx = next((i for i, u in enumerate(units) if any(x.get("status") == "rendering" for x in u)), None)
    first_q = next((i for i, u in enumerate(units) if i != ren_idx and any(x.get("status") == "queued" for x in u)), None)
    if ren_idx is not None and first_q is not None and ren_idx > first_q:
        units.insert(first_q, units.pop(ren_idx))
    return units


def normalize_queue_units(items: list[dict], srv=None) -> list[dict]:
    """Flattens grouped SceneUnits back into `items` in-place and re-numbers 1-based `idx`."""
    flat = [itm for u in _group_queue_units(items, srv) for itm in u]
    for i, itm in enumerate(flat, 1):
        itm["idx"], itm["total"] = i, len(flat)
    items[:] = flat
    return items


def reorder_in_project_queue(
    slug: str, item_id: str = "", target_id: str = "", position: str = "before", order: list[str] | None = None
) -> list[dict]:
    """Reorders queued items by moving item_id's SceneUnit before/after target_id's SceneUnit,
    or according to sequence in order. Multiple passes of the same scene remain strictly coupled in [Pass 1, Pass 2] order.
    Actively rendering items remain pinned at the top of the queue.
    """
    srv = _srv()
    queue_file = resolve_queue_file(srv.PROJECTS_DIR / slug, for_write=True)
    with srv.queue_file_lock:
        if not queue_file.is_file():
            compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        units = _group_queue_units(items, srv)
        is_ren = lambda u: any(x.get("status") == "rendering" for x in u)
        is_q = lambda u: (not is_ren(u)) and any(x.get("status") == "queued" for x in u)

        if order:
            order_keys = [re.sub(r"\.(mp4|jpg|jpeg|png|webp)$", "", str(x), flags=re.IGNORECASE).upper() for x in order]
            def _unit_rank(u):
                for x in u:
                    for k in (x.get("scene_id", ""), x.get("output_filename", ""), x.get("filename", ""), x.get("id", "")):
                        k_clean = re.sub(r"\.(mp4|jpg|jpeg|png|webp)$", "", str(k), flags=re.IGNORECASE).upper()
                        if k_clean in order_keys: return order_keys.index(k_clean)
                return 999999
            is_sort_q = lambda u: (not is_ren(u)) and any(x.get("status") in ("queued", "failed") for x in u)
            ren_units = [u for u in units if is_ren(u)]
            q_units = [u for u in units if is_sort_q(u)]
            other_units = [u for u in units if not is_ren(u) and not is_sort_q(u)]
            q_units.sort(key=_unit_rank)
            units = ren_units + q_units + other_units
        elif item_id and target_id:
            clean_src = re.sub(r"\.(mp4|jpg|jpeg|png|webp)$", "", str(item_id), flags=re.IGNORECASE)
            clean_dst = re.sub(r"\.(mp4|jpg|jpeg|png|webp)$", "", str(target_id), flags=re.IGNORECASE)
            def _match_unit(val, cval):
                return next((i for i, u in enumerate(units) if any(x.get("output_filename") in (val, cval) or x.get("filename") in (val, cval) or x.get("id") in (val, cval) or (x.get("stage") != "anchor" and str(x.get("scene_id", "")).upper() == cval.upper()) for x in u)), None)
            src_idx, dst_idx = _match_unit(item_id, clean_src), _match_unit(target_id, clean_dst)
            if src_idx is not None and dst_idx is not None and src_idx != dst_idx and is_q(units[src_idx]):
                src_u = units.pop(src_idx)
                dst_idx = _match_unit(target_id, clean_dst)
                if dst_idx is not None:
                    units.insert(dst_idx + 1 if (is_ren(units[dst_idx]) or position == "after") else dst_idx, src_u)

        flat = [itm for u in _group_queue_units([x for u in units for x in u], srv) for itm in u]
        for i, itm in enumerate(flat, 1):
            itm["idx"], itm["total"] = i, len(flat)
        write_queue_json_locked(srv, queue_file, flat)
        return flat


def move_in_project_queue(slug: str, item_id: str, direction: str = "up") -> list[dict]:
    """Moves a queued item (and its coupled pass, if two-pass) up or down as an atomic unit among queued units,
    keeping active renders pinned at the top of the queued list and Pass 1 strictly before Pass 2."""
    srv = _srv()
    queue_file = resolve_queue_file(srv.PROJECTS_DIR / slug, for_write=True)
    with srv.queue_file_lock:
        if not queue_file.is_file(): compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        units = _group_queue_units(items, srv)
        clean_id = str(item_id).replace(".mp4", "")
        u_idx = next((i for i, u in enumerate(units) if any(x.get("output_filename") in (item_id, clean_id) or x.get("filename") in (item_id, clean_id) or x.get("id") in (item_id, clean_id) for x in u)), None)
        if u_idx is not None:
            is_ren = lambda u: any(x.get("status") == "rendering" for x in u)
            is_q = lambda u: (not is_ren(u)) and any(x.get("status") == "queued" for x in u)
            if is_q(units[u_idx]):
                swap_u = None
                if direction == "up":
                    for i in range(u_idx - 1, -1, -1):
                        if is_ren(units[i]): break
                        if is_q(units[i]): swap_u = i; break
                else:
                    for i in range(u_idx + 1, len(units)):
                        if is_q(units[i]): swap_u = i; break
                if swap_u is not None:
                    tgt_id = units[swap_u][0].get("output_filename") or units[swap_u][0].get("id")
                    return reorder_in_project_queue(slug, item_id=item_id, target_id=tgt_id, position="before" if direction == "up" else "after")
        flat = [itm for u in units for itm in u]
        for i, itm in enumerate(flat, 1):
            itm["idx"], itm["total"] = i, len(flat)
        write_queue_json_locked(srv, queue_file, flat)
        return flat


def remove_from_project_queue(slug: str, item_id: str) -> list[dict]:
    """Marks a queued item as explicitly skipped by user, cascading to Pass 2 if Pass 1 is missing on disk or Pass 2 depends on it."""
    srv, proj_dir = _srv(), _srv().PROJECTS_DIR / slug
    queue_file = resolve_queue_file(proj_dir, for_write=True)
    with srv.queue_file_lock:
        if not queue_file.is_file(): compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        normalize_queue_units(items, srv)
        clean_id = str(item_id).replace(".mp4", "")
        for item in items:
            if item.get("output_filename") in (item_id, clean_id) or item.get("filename") in (item_id, clean_id) or item.get("id") in (item_id, clean_id):
                item.update({"status": "skipped", "needs_render": False, "reason": "SKIPPED BY USER", "user_skipped_hash": item.get("task_hash", ""), "user_skipped_anchor_mtime": item.get("anchor_mtime", 0.0)})
                if item.get("pass") == 1:
                    p1_mp4 = resolve_project_dirs(proj_dir)["renders"] / f"{item.get('output_filename', '')}.mp4"
                    p1_exists = p1_mp4.is_file() and p1_mp4.stat().st_size > 50000
                    for sib in items:
                        if sib.get("scene_id") == item.get("scene_id") and sib.get("pass") == 2 and sib.get("status") == "queued":
                            if not p1_exists or str(sib.get("reason") or "").startswith("PASS 1"):
                                sib.update({"status": "skipped", "needs_render": False, "reason": "SKIPPED BY USER", "user_skipped_hash": sib.get("task_hash", ""), "user_skipped_anchor_mtime": sib.get("anchor_mtime", 0.0)})
                break
        normalize_queue_units(items, srv)
        write_queue_json_locked(srv, queue_file, items)
        return items


def enqueue_in_project_queue(slug: str, item_id: str) -> list[dict]:
    """Forces an item into the render queue, cascading Pass 1 -> Pass 2 and keeping any active render pinned ahead of queued items."""
    srv = _srv()
    queue_file = resolve_queue_file(srv.PROJECTS_DIR / slug, for_write=True)
    with srv.queue_file_lock:
        if not queue_file.is_file(): compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        normalize_queue_units(items, srv)
        clean_id = str(item_id).replace(".mp4", "")
        target_item = next((it for it in items if it.get("output_filename") in (item_id, clean_id) or it.get("filename") in (item_id, clean_id) or it.get("id") in (item_id, clean_id)), None)
        if not target_item:
            items = compute_fast_project_queue(slug, force_recheck=True)
            target_item = next((it for it in items if it.get("output_filename") in (item_id, clean_id) or it.get("filename") in (item_id, clean_id) or it.get("id") in (item_id, clean_id)), None)
        if target_item:
            try: record_done_items(srv, slug, items)
            except Exception: pass
            target_item.update({"status": "queued", "needs_render": True, "reason": "FORCED"})
            target_item.pop("user_skipped_hash", None); target_item.pop("user_skipped_anchor_mtime", None)
            touched = [target_item]
            sid = target_item.get("scene_id") or srv.derive_scene_id(target_item.get("output_filename", ""))
            if target_item.get("stage") != "anchor" and target_item.get("pass") in (1, 2):
                for sib in items:
                    sib_sid = sib.get("scene_id") or srv.derive_scene_id(sib.get("output_filename", ""))
                    if sib_sid == sid and sib is not target_item and sib.get("status") != "rendering":
                        sib.update({"status": "queued", "needs_render": True, "reason": "PASS 1 RE-RENDERED (FORCED)" if target_item.get("pass") == 1 else "COUPLED PASS (FORCED)"})
                        sib.pop("user_skipped_hash", None); sib.pop("user_skipped_anchor_mtime", None)
                        touched.append(sib)
            reset_session_blocks_for_items(srv, touched, clear_failed=True)
        normalize_queue_units(items, srv)
        write_queue_json_locked(srv, queue_file, items)
        return items


def clear_project_queue(slug: str) -> list[dict]:
    """Marks all currently queued items as skipped by user until edited or Queue All is clicked."""
    srv = _srv()
    queue_file = resolve_queue_file(srv.PROJECTS_DIR / slug, for_write=True)
    with srv.queue_file_lock:
        if not queue_file.is_file(): compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        for item in items:
            if item.get("status") == "queued":
                item.update({"status": "skipped", "needs_render": False, "reason": "SKIPPED BY USER", "user_skipped_hash": item.get("task_hash", ""), "user_skipped_anchor_mtime": item.get("anchor_mtime", 0.0)})
        write_queue_json_locked(srv, queue_file, items)
        return items


def prepare_targeted_queue_items(slug: str, target_sid: str, is_worker_running: bool | None = None) -> list[dict]:
    """Prepares the queue for a targeted scene render:
    - When idle (is_worker_running=False): defers other queued items so ONLY the targeted scene renders, placing it at index 0.
    - When running (is_worker_running=True): keeps the active render running and places the targeted scene immediately after the active item.
    """
    srv = _srv()
    target_sid = str(target_sid).strip().upper()
    target_base = target_sid.split(":")[0].strip()
    target_pass = None
    if ":" in target_sid:
        p_tok = target_sid.split(":")[1].lower().replace("p", "").strip()
        if p_tok.isdigit():
            target_pass = int(p_tok)

    proj_dir = resolve_project_dir_safe(srv.PROJECTS_DIR, slug)
    if proj_dir is None:
        raise ValueError(f"Invalid project slug '{slug}'")
    queue_file = resolve_queue_file(proj_dir, for_write=True)
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
    config_file = jsons[-1] if jsons else None

    with srv.queue_file_lock:
        if not queue_file.is_file():
            compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        if is_worker_running is None:
            is_any_act, _ = active_rendering_map(srv, slug)
            is_worker_running = bool(is_any_act or any(x.get("status") == "rendering" for x in items))

        # Resolve canonical targets for target scene so stale-resolution entries are replaced rather than duplicated
        desired_by_pass: dict[int | None, tuple[str, str, str, str]] = {}
        cfg_d, sc_map, def_res = None, {}, "1344x768"
        if config_file and config_file.is_file():
            try:
                cfg_d = json.loads(config_file.read_text(encoding="utf-8"))
                def_res = resolve_project_resolution(cfg_d)
                sc_map = {s.get("id"): s for s in cfg_d.get("scenes", []) if s.get("id")}
                sc = next((s for s in cfg_d.get("scenes", []) if is_scene_match(s.get("id", ""), "", target_base)), None)
                if sc:
                    res_str = resolve_scene_queue_resolution(cfg_d, sc, def_res)
                    sid = sc.get("id", target_base)
                    mode = sc.get("mode", "solo")
                    is_two = mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
                    if is_two:
                        desired_by_pass = {1: (f"{sid}_Pass1_Left_{res_str}", f"{sid}:p1", sid, res_str), 2: (f"{sid}_Pass2_Final_{res_str}", f"{sid}:p2", sid, res_str)}
                    else:
                        s_fn = f"{_strip_res_suffix(sc.get('output_name', f'{sid}_duet_a' if mode in ('duet_method_a', 'duet_a') else f'{sid}_{mode}'))}_{res_str}"
                        desired_by_pass = {None: (s_fn, sid, sid, res_str)}
            except Exception as ge:
                srv.broadcast_log(f"[Studio UI Warning] Could not resolve target scene queue metadata for {target_sid}: {ge}")

        try: record_done_items(srv, proj_dir, items)
        except Exception: pass
        matched_items, pruned_items = [], []
        for item in items:
            out_fn = item.get("output_filename", "")
            sid = item.get("scene_id") or srv.derive_scene_id(out_fn)
            item["scene_id"] = sid
            if not item.get("target_arg"):
                item["target_arg"] = srv.derive_target_arg(out_fn)
            pass_num = item.get("pass")
            if pass_num is None:
                pass_num = 1 if "Pass1" in out_fn else (2 if "Pass2" in out_fn else None)
                item["pass"] = pass_num

            is_match = is_scene_match(sid, out_fn, target_base) and (target_pass is None or pass_num == target_pass)
            if is_match and pass_num in desired_by_pass:
                exp_fn, exp_tgt, exp_sid, exp_res = desired_by_pass[pass_num]
                if out_fn != exp_fn:
                    if is_worker_running and item.get("status") == "rendering":
                        pruned_items.append(item)
                        continue
                    if any(x is not item and x.get("output_filename") == exp_fn for x in items):
                        continue  # drop stale resolution duplicate when target resolution entry already exists
                    item.update({"id": exp_fn, "output_filename": exp_fn, "filename": f"{exp_fn}.mp4", "target_arg": exp_tgt, "scene_id": exp_sid, "resolution": exp_res})
                    for k in ("completed_at", "elapsed", "estimated_seconds", "estimated_str"):
                        item.pop(k, None)

            if is_match:
                if not (is_worker_running and item.get("status") == "rendering"):
                    item.update({"status": "queued", "needs_render": True, "reason": f"TARGETED ({target_sid})"})
                    item.pop("user_skipped_hash", None); item.pop("user_skipped_anchor_mtime", None)
                matched_items.append(item)
            else:
                if not is_worker_running and item.get("status") in ("queued", "rendering"):
                    item.update({"status": "skipped", "needs_render": False, "reason": "DEFERRED (SINGLE SCENE RENDER)"})
            pruned_items.append(item)
        items = pruned_items

        # If targeted scene was not found in existing queue, generate it from project config
        if not matched_items and desired_by_pass:
            for p_num, (out_fn, tgt_arg, sid, res_str) in desired_by_pass.items():
                if target_pass is not None and p_num != target_pass: continue
                new_item = {
                    "idx": len(items) + 1, "id": out_fn, "scene_id": sid, "target_arg": tgt_arg,
                    "pass": p_num, "filename": f"{out_fn}.mp4", "output_filename": out_fn,
                    "status": "queued", "reason": f"TARGETED ({target_sid})", "needs_render": True,
                    "model_type": "minimax_h3_fl2va_pruned_pdd", "resolution": res_str,
                }
                items.insert(0, new_item); matched_items.append(new_item)

        matched_items.sort(key=lambda x: x.get("pass") or 0)
        if matched_items:
            for m in reversed([x for x in matched_items if x.get("status") != "rendering"]):
                if m in items: items.remove(m)
                render_idx = next((i for i, x in enumerate(items) if x.get("status") == "rendering"), -1) if is_worker_running else -1
                items.insert(render_idx + 1 if is_worker_running else 0, m)
            normalize_queue_units(items, srv)

        if cfg_d:
            PC = getattr(srv, "ProjectContext", None) or _DEF_PC
            try: ctx_t = PC(config_file) if (PC and config_file) else None
            except Exception: ctx_t = None
            if enrich_queue_with_fal_metadata:
                try: enrich_queue_with_fal_metadata(items, sc_map, ctx=ctx_t, proj_dir=proj_dir, default_res=def_res)
                except Exception: pass
            if attach_queue_time_estimates:
                try: attach_queue_time_estimates(items, sc_map, ctx=ctx_t, proj_dir=proj_dir, default_res=def_res)
                except Exception: pass
        reset_session_blocks_for_items(srv, [x for x in matched_items if x.get("status") == "queued"], clear_failed=True)
        write_queue_json_locked(srv, queue_file, items)
        return matched_items
