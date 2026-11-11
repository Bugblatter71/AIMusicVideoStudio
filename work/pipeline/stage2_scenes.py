import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .context import ProjectContext, resolve_scene_padding
from .media_utils import (
    _file_sha256,
    _sha256,
    check_scene_render_coverage,
    probe_video_dimensions,
    render_settings_of,
    resolve_rendered_clip_path,
    settings_change_reason,
)
from .prompts import (
    compile_modular_scene_prompt,
    expand_variables,
    extract_anchor_id,
    resolve_marker_prompt,
    resolve_scene_refs,
)
from .stage1_anchors import match_anchor_id
from .stage3_master import assemble_progressive_master
from .takes import (
    after_scene_render,
    before_scene_render,
    restore_previous_active_take_if_missing,
)
from .stage2_hashes import (
    _scene_id_matches,
    match_scene_pass,
    _stem_matches_legacy_render,
    compute_project_task_hashes,
)
from .wgp_bridge import (
    LazyWgpSession,
    check_cancel_requested,
    clear_cancel_request,
    execute_wgp_job,
    write_engine_status,
)


def run_stage2_scenes(
    ctx: ProjectContext,
    lazy_session: LazyWgpSession,
    state: dict,
    force_scenes: list[str],
    regenerated_anchors: set[str],
    cascade_anchors: bool = True,
    sync_only: bool = False,
    stitch_only: bool = False,
    dummy_render: bool = False,
    no_queue_sync: bool = False,
    no_stitch: bool = False,
) -> list[dict]:
    def_vrid = str(ctx.default_video_renderer)
    def_vmodel = ctx.video_renderers.get(def_vrid, {}).get("model_type", "minimax_h3_fl2va_pruned_pdd")
    print("\n" + "=" * 72, flush=True)
    print(
        f"STAGE 2: Slicing Audio & Checking / Rendering {len(ctx.scenes)} Video Scenes "
        f"(Default Renderer #{def_vrid}: {def_vmodel})",
        flush=True,
    )
    print("=" * 72, flush=True)

    anchor_hashes = state.setdefault("anchors", {})
    task_hashes = state.setdefault("tasks", {})
    settings_store = state.setdefault("task_settings", {})
    project_task_hashes = compute_project_task_hashes(ctx)

    all_tasks: list[tuple[dict, str, str, bool]] = []
    scene_meta: list[dict] = []

    has_scene_filter = bool(force_scenes) and not any(
        x.strip().upper() in ("ALL", "ALL-SCENES") for x in force_scenes
    )
    if has_scene_filter:
        print(f"[Filter] Targeted scene rendering active: {', '.join(force_scenes)} (all other scenes will be skipped)", flush=True)

    for sc in ctx.scenes:
        sid = sc["id"]
        start = float(sc["start"])
        end = float(sc["end"])
        edit_dur = round(end - start, 3)
        mode = sc["mode"]
        aid, anchor_path = ctx.resolve_anchor_path(sc["anchor"])
        anchor_fn = anchor_path.name
        aid_end, anchor_end_path = ("", None)
        if sc.get("anchor_end"):
            aid_end_cand, a_end_p = ctx.resolve_anchor_path(sc.get("anchor_end"))
            if a_end_p.exists():
                aid_end, anchor_end_path = aid_end_cand, a_end_p
        anchor_was_regen = cascade_anchors and (
            anchor_fn in regenerated_anchors
            or aid in regenerated_anchors
            or (anchor_end_path is not None and (anchor_end_path.name in regenerated_anchors or aid_end in regenerated_anchors))
        )
        head_pad, tail_pad = ctx.get_scene_render_padding(sc)
        has_custom_pad = any(sc.get(k) is not None for k in ("render_pad_start", "render_pad_end", "pad_start", "pad_end", "render_overlap", "head_overlap", "tail_overlap"))
        req_start = max(0.0, start - head_pad)
        req_end = end + tail_pad
        req_dur = req_end - req_start
        cov_s = req_start if has_custom_pad else start
        cov_e = req_end if has_custom_pad else end

        if mode in ("solo", "male_solo", "female_solo", "silent_locked", "silent", "ambient", "single_pass", "duet_method_a", "duet_a"):
            vrid, vrspec = ctx.resolve_video_renderer(sc)
            vmodel = vrspec["model_type"]

            # Overlap calculation: render with configured padding beyond timeline edit boundaries
            frames = ctx.valid_video_frames(req_dur, vrspec)
            gen_dur = frames / ctx.model_fps(vrspec)
            target_render_start = req_start
            target_render_end = target_render_start + gen_dur

            res_str = ctx.resolve_resolution(vrspec.get("resolution", ctx.current_resolution))
            if mode in ("duet_method_a", "duet_a"):
                stem_l = sc.get("stem_m") or sc.get("stem_left") or sc.get("stem_l") or "male"
                stem_r = sc.get("stem_f") or sc.get("stem_right") or sc.get("stem_r") or "female"
                stem_key = f"{stem_l}_{stem_r}"
                cand = next((c for c in (f"{sid}_duet_a", f"{sid}_duet_method_a", f"{sid}_duet_stereo") if (ctx.renders_dir / f"{c}_{res_str}.mp4").exists()), None)
                raw_out_name = sc.get("output_name", cand or f"{sid}_duet_a")
            elif mode in ("solo", "male_solo", "female_solo"):
                stem_key = sc.get("stem") or ("female" if mode == "female_solo" else "male")
                cand = next((c for c in (f"{sid}_{mode}", f"{sid}_solo", f"{sid}_male_solo", f"{sid}_female_solo", f"{sid}_{stem_key}") if (ctx.renders_dir / f"{c}_{res_str}.mp4").exists()), None) if not sc.get("output_name") else None
                raw_out_name = sc.get("output_name", cand or (f"{sid}_{mode}" if mode != "solo" else f"{sid}_solo"))
            elif mode in ("silent_locked", "silent", "ambient"):
                stem_key, raw_out_name = "silent", sc.get("output_name", f"{sid}_silent")
            else:
                stem_key = sc.get("stem", "silent")
                raw_out_name = sc.get("output_name", f"{sid}_{stem_key}")

            if res_str not in raw_out_name:
                out_name = f"{raw_out_name}_{res_str}"
            else:
                out_name = raw_out_name

            # Fast skip: if targeting specific scene(s) and this scene is not targeted, skip audio slicing and disk checks
            if has_scene_filter and not match_scene_pass(force_scenes, sid, None):
                all_tasks.append((
                    {"output_filename": out_name, "model_type": vmodel, "video_length": frames, "resolution": res_str},
                    "", "SKIPPED (NOT TARGETED)", False,
                ))
                scene_renders = state.setdefault("scene_renders", {})
                r_info = scene_renders.get(out_name)
                act_r_start = float(r_info["render_start"]) if (r_info and "render_start" in r_info) else target_render_start
                act_r_end = float(r_info["render_end"]) if (r_info and "render_end" in r_info) else target_render_end
                scene_meta.append({
                    "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                    "clip": f"{out_name}.mp4", "render_start": act_r_start, "render_end": act_r_end,
                    "anchor": sc.get("anchor"),
                })
                continue

            out_mp4 = resolve_rendered_clip_path(ctx.renders_dir, out_name, promote=True)
            if not out_mp4.exists():
                legacy_mp4 = ctx.renders_dir / f"{raw_out_name}.mp4"
                if legacy_mp4.exists() and legacy_mp4.stat().st_size > 50000:
                    dims = probe_video_dimensions(ctx.ffmpeg, legacy_mp4)
                    if dims and f"{dims[0]}x{dims[1]}" == res_str:
                        shutil.copy2(str(legacy_mp4), str(out_mp4))

            allow_pad = bool(sc.get("hold_last_frame") or sc.get("pad_tail") or sc.get("allow_tail_pad"))
            scene_renders = state.setdefault("scene_renders", {})
            is_covered, rendered_start, rendered_end = check_scene_render_coverage(
                ctx.ffmpeg, out_mp4, scene_renders, out_name, cov_s, cov_e, allow_tail_pad=allow_pad
            )
            actual_render_start = rendered_start if is_covered else target_render_start
            actual_render_end = rendered_end if is_covered else target_render_end

            if mode in ("duet_method_a", "duet_a"):
                wav_path = ctx.resolve_scene_stereo_audio(sid, stem_l, stem_r, target_render_start, gen_dur, frames)
            else:
                wav_path = ctx.resolve_scene_audio(sid, stem_key, target_render_start, gen_dur, frames)
            raw_prompt = compile_modular_scene_prompt(sc, pass_num=None)
            prompt_for_task = resolve_marker_prompt(raw_prompt, target_render_start, ctx.markers, fps=ctx.model_fps(vrspec))
            scene_seed = int(sc.get("seed") if sc.get("seed") is not None else (sc.get("seed_m") or 1000))

            t_dict = ctx.make_video_task(
                output_filename=out_name,
                prompt=prompt_for_task,
                video_length=frames,
                seed=scene_seed,
                image_start=anchor_path,
                audio_path=wav_path,
                rspec=vrspec,
                image_refs=ctx.resolve_scene_image_refs(sc, raw_prompt),
                image_end=anchor_end_path,
                sc=sc,
                pass_num=None,
            )
            t_dict["render_start"] = target_render_start
            t_dict["render_end"] = target_render_end

            valid_hashes = project_task_hashes.get(out_name, ("",))
            t_hash = valid_hashes[0]

            file_exists = out_mp4.exists() and out_mp4.stat().st_size > 50000
            prev_hash = task_hashes.get(out_name)
            changed = (prev_hash is not None) and (prev_hash not in valid_hashes)

            if sync_only and file_exists:
                task_hashes[out_name] = t_hash
                settings_store[out_name] = render_settings_of(t_dict)
                needs_render, reason = False, "SYNCED"
            elif stitch_only:
                needs_render, reason = False, "STITCH-ONLY"
            elif has_scene_filter:
                forced = match_scene_pass(force_scenes, sid, None)
                if not forced:
                    needs_render, reason = False, "SKIPPED (NOT TARGETED)"
                else:
                    settings_reason = settings_change_reason(ctx.ffmpeg, settings_store, out_name, t_dict, out_mp4, file_exists)
                    if not file_exists:
                        needs_render, reason = True, "MISSING FILE (TARGETED)"
                    elif settings_reason:
                        needs_render, reason = True, f"FORCED ({settings_reason})"
                    elif changed:
                        needs_render, reason = True, "FORCED (EDITED)"
                    elif not is_covered:
                        needs_render, reason = True, f"FORCED (BOUNDS NOT COVERED: [{req_start:.2f}, {req_end:.2f}] vs [{rendered_start:.2f}, {rendered_end:.2f}])"
                    else:
                        needs_render, reason = True, "FORCED"
            else:
                forced = match_scene_pass(force_scenes, sid, None)
                anchor_newer = file_exists and (
                    (anchor_path.exists() and out_mp4.stat().st_mtime < anchor_path.stat().st_mtime)
                    or (anchor_end_path is not None and anchor_end_path.exists() and out_mp4.stat().st_mtime < anchor_end_path.stat().st_mtime)
                )
                settings_reason = settings_change_reason(ctx.ffmpeg, settings_store, out_name, t_dict, out_mp4, file_exists)

                if forced:
                    needs_render, reason = True, "FORCED"
                elif not file_exists:
                    needs_render, reason = True, "MISSING FILE"
                elif anchor_was_regen:
                    needs_render, reason = True, f"ANCHOR {aid} UPDATED"
                elif anchor_newer:
                    needs_render, reason = True, f"ANCHOR {aid} NEWER ON DISK"
                elif settings_reason:
                    needs_render, reason = True, settings_reason
                elif changed:
                    needs_render, reason = True, "PROMPT/SEED/RENDERER/ANCHOR EDITED"
                elif not is_covered:
                    r_str = f"[{rendered_start:.2f}, {rendered_end:.2f}]" if rendered_start is not None else "none"
                    needs_render, reason = True, f"TIMELINE EXCEEDS RENDER ([{req_start:.2f}, {req_end:.2f}] not covered by {r_str})"
                else:
                    needs_render, reason = False, "UP-TO-DATE (COVERED BY RENDER)"

                if not needs_render:
                    task_hashes[out_name] = t_hash
                    settings_store[out_name] = render_settings_of(t_dict)

            all_tasks.append((t_dict, t_hash, reason, needs_render))
            scene_meta.append({
                "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                "clip": out_mp4.name, "render_start": actual_render_start, "render_end": actual_render_end,
                "anchor": sc.get("anchor")
            })

        elif mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
            vrid_p1, vrspec_p1 = ctx.resolve_video_renderer(sc, pass_num=1)
            vrid_p2, vrspec_p2 = ctx.resolve_video_renderer(sc, pass_num=2)
            vmodel_p1 = vrspec_p1["model_type"]
            vmodel_p2 = vrspec_p2["model_type"]

            # Overlap calculation
            frames = ctx.valid_video_frames(req_dur, vrspec_p2)
            gen_dur = frames / ctx.model_fps(vrspec_p2)
            target_render_start = req_start
            target_render_end = target_render_start + gen_dur

            res_str = ctx.resolve_resolution(vrspec_p2.get("resolution", ctx.current_resolution))
            out_p1 = f"{sid}_Pass1_Left_{res_str}"
            out_p2 = f"{sid}_Pass2_Final_{res_str}"

            # Fast skip: if targeting specific scene(s) and neither pass of this scene is targeted, skip audio and masks
            if has_scene_filter and not (match_scene_pass(force_scenes, sid, 1) or match_scene_pass(force_scenes, sid, 2)):
                all_tasks.append((
                    {"output_filename": out_p1, "model_type": vmodel_p1, "video_length": frames, "resolution": res_str},
                    "", "SKIPPED (NOT TARGETED)", False,
                ))
                all_tasks.append((
                    {"output_filename": out_p2, "model_type": vmodel_p2, "video_length": frames, "resolution": res_str},
                    "", "SKIPPED (NOT TARGETED)", False,
                ))
                scene_renders = state.setdefault("scene_renders", {})
                r_info = scene_renders.get(out_p2)
                act_r_start = float(r_info["render_start"]) if (r_info and "render_start" in r_info) else target_render_start
                act_r_end = float(r_info["render_end"]) if (r_info and "render_end" in r_info) else target_render_end
                scene_meta.append({
                    "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                    "clip": f"{out_p2}.mp4", "render_start": act_r_start, "render_end": act_r_end,
                    "anchor": sc.get("anchor"),
                })
                continue

            stem_m = sc.get("stem_m") or sc.get("stem_p1") or sc.get("stem") or "male"
            default_f = "silent" if mode == "gva_male_left_silent_right" else "female"
            stem_f = sc.get("stem_f") or sc.get("stem_p2") or default_f

            wav_m = ctx.resolve_scene_audio(sid, stem_m, target_render_start, gen_dur, frames)
            wav_f = ctx.resolve_scene_audio(sid, stem_f, target_render_start, gen_dur, frames)

            split_x = float(sc.get("split_x", 0.50))
            mask_path = ctx.ensure_right_mask(frames, split_x, res_str)

            raw_m = compile_modular_scene_prompt(sc, pass_num=1)
            raw_f = compile_modular_scene_prompt(sc, pass_num=2)
            prompt_m_for_task = resolve_marker_prompt(raw_m, target_render_start, ctx.markers, fps=ctx.model_fps(vrspec_p1))
            prompt_f_for_task = resolve_marker_prompt(raw_f, target_render_start, ctx.markers, fps=ctx.model_fps(vrspec_p2))

            seed_m = int(sc.get("seed_m") if sc.get("seed_m") is not None else (sc.get("seed") or 1000))
            seed_f = int(sc.get("seed_f") if sc.get("seed_f") is not None else (sc.get("seed") or 1000))

            t_p1 = ctx.make_video_task(
                output_filename=out_p1,
                prompt=prompt_m_for_task,
                video_length=frames,
                seed=seed_m,
                image_start=anchor_path,
                audio_path=wav_m,
                rspec=vrspec_p1,
                image_refs=ctx.resolve_scene_image_refs(sc, raw_m),
                image_end=anchor_end_path,
                sc=sc,
                pass_num=1,
            )
            t_p1["render_start"] = target_render_start
            t_p1["render_end"] = target_render_end

            t_p2 = ctx.make_video_task(
                output_filename=out_p2,
                prompt=prompt_f_for_task,
                video_length=frames,
                seed=seed_f,
                image_start=anchor_path,
                audio_path=wav_f,
                rspec=vrspec_p2,
                video_guide=ctx.renders_dir / f"{out_p1}.mp4",
                video_mask=mask_path,
                image_refs=ctx.resolve_scene_image_refs(sc, raw_f),
                sc=sc,
                pass_num=2,
            )
            t_p2["render_start"] = target_render_start
            t_p2["render_end"] = target_render_end

            valid_p1 = project_task_hashes.get(out_p1, ("",))
            valid_p2 = project_task_hashes.get(out_p2, ("",))
            h_p1 = valid_p1[0]
            h_p2 = valid_p2[0]

            mp4_p1 = resolve_rendered_clip_path(ctx.renders_dir, out_p1, promote=True)
            mp4_p2 = resolve_rendered_clip_path(ctx.renders_dir, out_p2, promote=True)
            if not mp4_p1.exists():
                legacy_p1 = ctx.renders_dir / f"{sid}_Pass1_Left.mp4"
                if legacy_p1.exists() and legacy_p1.stat().st_size > 50000:
                    dims = probe_video_dimensions(ctx.ffmpeg, legacy_p1)
                    if dims and f"{dims[0]}x{dims[1]}" == res_str:
                        shutil.copy2(str(legacy_p1), str(mp4_p1))
            if not mp4_p2.exists():
                legacy_p2 = ctx.renders_dir / f"{sid}_Pass2_Final.mp4"
                if legacy_p2.exists() and legacy_p2.stat().st_size > 50000:
                    dims = probe_video_dimensions(ctx.ffmpeg, legacy_p2)
                    if dims and f"{dims[0]}x{dims[1]}" == res_str:
                        shutil.copy2(str(legacy_p2), str(mp4_p2))
            ex_p1 = mp4_p1.exists() and mp4_p1.stat().st_size > 50000
            ex_p2 = mp4_p2.exists() and mp4_p2.stat().st_size > 50000
            prev_p1 = task_hashes.get(out_p1)
            prev_p2 = task_hashes.get(out_p2)

            allow_pad = bool(sc.get("hold_last_frame") or sc.get("pad_tail") or sc.get("allow_tail_pad"))
            scene_renders = state.setdefault("scene_renders", {})
            cov_p1, r_start_p1, r_end_p1 = check_scene_render_coverage(ctx.ffmpeg, mp4_p1, scene_renders, out_p1, cov_s, cov_e, allow_tail_pad=allow_pad)
            cov_p2, r_start_p2, r_end_p2 = check_scene_render_coverage(ctx.ffmpeg, mp4_p2, scene_renders, out_p2, cov_s, cov_e, allow_tail_pad=allow_pad)

            actual_render_start = r_start_p2 if cov_p2 else target_render_start
            actual_render_end = r_end_p2 if cov_p2 else target_render_end

            if sync_only and ex_p1 and ex_p2:
                task_hashes[out_p1] = h_p1
                task_hashes[out_p2] = h_p2
                settings_store[out_p1] = render_settings_of(t_p1)
                settings_store[out_p2] = render_settings_of(t_p2)
                nr_p1, r_p1 = False, "SYNCED"
                nr_p2, r_p2 = False, "SYNCED"
            elif stitch_only:
                nr_p1, r_p1 = False, "STITCH-ONLY"
                nr_p2, r_p2 = False, "STITCH-ONLY"
            elif has_scene_filter:
                force_p1 = match_scene_pass(force_scenes, sid, 1)
                force_p2 = match_scene_pass(force_scenes, sid, 2)
                set_p1 = settings_change_reason(ctx.ffmpeg, settings_store, out_p1, t_p1, mp4_p1, ex_p1)
                set_p2 = settings_change_reason(ctx.ffmpeg, settings_store, out_p2, t_p2, mp4_p2, ex_p2)

                if force_p1:
                    nr_p1, r_p1 = True, (f"FORCED ({set_p1})" if set_p1 else "FORCED")
                elif force_p2 and not ex_p1:
                    nr_p1, r_p1 = True, "REQUIRED FOR PASS 2"
                elif force_p2 and not cov_p1:
                    nr_p1, r_p1 = True, f"FORCED PASS 1 (BOUNDS NOT COVERED)"
                else:
                    nr_p1, r_p1 = False, "SKIPPED (NOT TARGETED)"

                if force_p2:
                    nr_p2, r_p2 = True, (f"FORCED ({set_p2})" if set_p2 else "FORCED")
                else:
                    nr_p2, r_p2 = False, "SKIPPED (NOT TARGETED)"
            else:
                force_p1 = match_scene_pass(force_scenes, sid, 1)
                force_p2 = match_scene_pass(force_scenes, sid, 2)
                chg_p1 = (prev_p1 is not None) and (prev_p1 not in valid_p1)
                anchor_newer_p1 = ex_p1 and (
                    (anchor_path.exists() and mp4_p1.stat().st_mtime < anchor_path.stat().st_mtime)
                    or (anchor_end_path is not None and anchor_end_path.exists() and mp4_p1.stat().st_mtime < anchor_end_path.stat().st_mtime)
                )
                anchor_newer_p2 = ex_p2 and anchor_path.exists() and (mp4_p2.stat().st_mtime < anchor_path.stat().st_mtime)
                p1_newer_than_p2 = ex_p1 and ex_p2 and (mp4_p2.stat().st_mtime < mp4_p1.stat().st_mtime)
                set_p1 = settings_change_reason(ctx.ffmpeg, settings_store, out_p1, t_p1, mp4_p1, ex_p1)
                set_p2 = settings_change_reason(ctx.ffmpeg, settings_store, out_p2, t_p2, mp4_p2, ex_p2)

                if force_p1:
                    nr_p1, r_p1 = True, "FORCED"
                elif not ex_p1:
                    nr_p1, r_p1 = True, "MISSING FILE"
                elif anchor_was_regen:
                    nr_p1, r_p1 = True, f"ANCHOR {aid} UPDATED"
                elif anchor_newer_p1:
                    nr_p1, r_p1 = True, f"ANCHOR {aid} NEWER ON DISK"
                elif set_p1:
                    nr_p1, r_p1 = True, set_p1
                elif chg_p1:
                    nr_p1, r_p1 = True, "PASS 1 (prompt_m/seed_m) EDITED"
                elif not cov_p1:
                    r_str = f"[{r_start_p1:.2f}, {r_end_p1:.2f}]" if r_start_p1 is not None else "none"
                    nr_p1, r_p1 = True, f"TIMELINE EXCEEDS RENDER ([{req_start:.2f}, {req_end:.2f}] not covered by {r_str})"
                else:
                    nr_p1, r_p1 = False, "UP-TO-DATE (COVERED BY RENDER)"

                chg_p2 = (prev_p2 is not None) and (prev_p2 not in valid_p2)
                if force_p2:
                    nr_p2, r_p2 = True, "FORCED"
                elif nr_p1:
                    nr_p2, r_p2 = True, f"PASS 1 RE-RENDERED ({r_p1})"
                elif not ex_p2:
                    nr_p2, r_p2 = True, "MISSING FILE"
                elif anchor_was_regen:
                    nr_p2, r_p2 = True, f"ANCHOR {aid} UPDATED"
                elif anchor_newer_p2:
                    nr_p2, r_p2 = True, f"ANCHOR {aid} NEWER ON DISK"
                elif p1_newer_than_p2:
                    nr_p2, r_p2 = True, "PASS 1 NEWER ON DISK"
                elif set_p2:
                    nr_p2, r_p2 = True, set_p2
                elif chg_p2:
                    nr_p2, r_p2 = True, "PASS 2 (prompt_f/seed_f/split_x) EDITED"
                elif not cov_p2:
                    r_str = f"[{r_start_p2:.2f}, {r_end_p2:.2f}]" if r_start_p2 is not None else "none"
                    nr_p2, r_p2 = True, f"TIMELINE EXCEEDS RENDER ([{start:.2f}, {end:.2f}] not covered by {r_str})"
                else:
                    nr_p2, r_p2 = False, "UP-TO-DATE (COVERED BY RENDER)"

                if not nr_p1:
                    task_hashes[out_p1] = h_p1
                    settings_store[out_p1] = render_settings_of(t_p1)
                if not nr_p2:
                    task_hashes[out_p2] = h_p2
                    settings_store[out_p2] = render_settings_of(t_p2)

            all_tasks.append((t_p1, h_p1, r_p1, nr_p1))
            all_tasks.append((t_p2, h_p2, r_p2, nr_p2))
            scene_meta.append({
                "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                "clip": mp4_p2.name, "render_start": actual_render_start, "render_end": actual_render_end,
                "anchor": sc.get("anchor")
            })

    ctx.save_state_hashes(state)
    (ctx.jobs_dir / "v3_manifest.json").write_text(json.dumps([t[0] for t in all_tasks], indent=2), encoding="utf-8")
    (ctx.jobs_dir / "v3_timeline.json").write_text(json.dumps(scene_meta, indent=2), encoding="utf-8")

    queue_summary = []
    for idx, (t, t_hash, reason, needs_render) in enumerate(all_tasks, 1):
        queue_summary.append({
            "idx": idx,
            "total": len(all_tasks),
            "scene_id": t["output_filename"].split("_")[0],
            "filename": f"{t['output_filename']}.mp4",
            "output_filename": t["output_filename"],
            "reason": reason,
            "needs_render": needs_render,
            "status": "queued" if needs_render else "skipped",
            "model_type": t.get("model_type"),
            "video_length": t.get("video_length"),
            "resolution": t.get("resolution"),
        })
    queue_file = ctx.jobs_dir / "v3_queue.json"
    if not (has_scene_filter or no_queue_sync):
        try:
            queue_file.write_text(json.dumps(queue_summary, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _sync_queue():
        if no_queue_sync:
            return
        try:
            if has_scene_filter and queue_file.exists():
                existing_q = json.loads(queue_file.read_text(encoding="utf-8"))
                if isinstance(existing_q, list):
                    by_out = {
                        q["output_filename"]: q
                        for q in queue_summary
                        if q.get("reason") != "SKIPPED (NOT TARGETED)"
                    }
                    for item in existing_q:
                        out_fn = item.get("output_filename")
                        if out_fn in by_out:
                            src = by_out[out_fn]
                            item["status"] = src.get("status", item.get("status"))
                            item["reason"] = src.get("reason", item.get("reason"))
                            for k in ("pct", "phase", "raw_phase", "step", "total_steps", "window_index", "window_count", "elapsed", "error"):
                                if k in src:
                                    item[k] = src[k]
                    queue_file.write_text(json.dumps(existing_q, indent=2), encoding="utf-8")
                    return
            queue_file.write_text(json.dumps(queue_summary, indent=2), encoding="utf-8")
        except Exception:
            pass

    if sync_only or stitch_only:
        return scene_meta

    pending_count = sum(1 for _, _, _, nr in all_tasks if nr)
    print(f"  -> {pending_count} of {len(all_tasks)} video passes queued for GPU rendering.", flush=True)

    # Initial progressive master assembly (displays all existing clips and anchor placeholders)
    if not (has_scene_filter or no_stitch):
        assemble_progressive_master(ctx, scene_meta, log_prefix="  ")

    for idx, (t, t_hash, reason, needs_render) in enumerate(all_tasks, 1):
        out_name = t["output_filename"]
        out_mp4 = ctx.renders_dir / f"{out_name}.mp4"

        # Check dynamic user queue requests from UI
        req_file = ctx.jobs_dir / "queue_requests.json"
        if req_file.exists():
            try:
                dyn_reqs = json.loads(req_file.read_text(encoding="utf-8"))
                matched = False
                for r in list(dyn_reqs):
                    r_clean = str(r).strip()
                    if r_clean and (r_clean in out_name or out_name.startswith(f"{r_clean}_") or out_name == r_clean):
                        matched = True
                        needs_render = True
                        reason = f"QUEUED BY USER"
                        queue_summary[idx - 1]["needs_render"] = True
                        queue_summary[idx - 1]["reason"] = reason
                        dyn_reqs.remove(r)
                        break
                if matched:
                    req_file.write_text(json.dumps(dyn_reqs, indent=2), encoding="utf-8")
            except Exception:
                pass

        if not needs_render:
            queue_summary[idx - 1]["status"] = "skipped"
            _sync_queue()
            if not (has_scene_filter or no_queue_sync):
                print(f"  [{idx:02d}/{len(all_tasks)}] {out_mp4.name} [{reason}] — skipping.", flush=True)
                print(f"[QUEUE_SKIP] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'reason': reason})}", flush=True)
            continue

        sid = out_name.split("_")[0]
        sc_obj = next((s for s in ctx.scenes if s.get("id") == sid), {"id": sid})
        pass_num = 1 if "_Pass1" in out_name else (2 if "_Pass2" in out_name else None)

        if dummy_render:
            t0 = time.time()
            print(
                f"  [{idx:02d}/{len(all_tasks)}] [DUMMY] Simulating render for {out_mp4.name} [{reason}] "
                f"({t.get('video_length')}f, {t.get('resolution')})...",
                flush=True,
            )
            print(
                f"[QUEUE_ACTIVE] {json.dumps({'idx': idx, 'total': len(all_tasks), 'filename': out_mp4.name, 'reason': reason, 'model': t.get('model_type'), 'frames': t.get('video_length'), 'resolution': t.get('resolution')})}",
                flush=True,
            )
            queue_summary[idx - 1]["status"] = "rendering"
            for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "estimated_remaining_seconds", "estimated_remaining_str"):
                queue_summary[idx - 1].pop(k, None)
            _sync_queue()
            before_scene_render(ctx, sc_obj, out_name, out_mp4, state)
            write_engine_status(
                ctx,
                {
                    "state": "rendering",
                    "stage": "scene",
                    "idx": idx,
                    "total": len(all_tasks),
                    "filename": out_mp4.name,
                    "output_filename": out_name,
                    "reason": reason,
                    "model": t.get("model_type"),
                    "frames": t.get("video_length"),
                    "resolution": t.get("resolution"),
                    "phase": "loading_model",
                    "raw_phase": "Loading model...",
                    "status_text": "Loading model...",
                    "step": None,
                    "total_steps": None,
                    "pct": 10.0,
                    "window_index": None,
                    "window_count": None,
                    "started_at": round(t0, 3),
                },
                merge=False,
            )

            # Emit mock progress steps
            total_dummy_steps = 5
            for step in range(1, total_dummy_steps + 1):
                if check_cancel_requested(ctx):
                    restore_previous_active_take_if_missing(ctx, sid, out_name, out_mp4)
                    clear_cancel_request(ctx)
                    queue_summary[idx - 1]["status"] = "queued"
                    queue_summary[idx - 1]["reason"] = "STOPPED BY USER"
                    _sync_queue()
                    write_engine_status(ctx, {"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user"})
                    raise RuntimeError(f"Cancelled rendering {out_mp4.name}")
                time.sleep(0.04)
                pct = round((step / total_dummy_steps) * 100, 1)
                prog = {
                    "idx": idx,
                    "filename": out_mp4.name,
                    "phase": "Sampling",
                    "raw_phase": "Sampling",
                    "status_text": f"Sampling {step}/{total_dummy_steps}",
                    "step": step,
                    "total_steps": total_dummy_steps,
                    "pct": pct,
                    "window_index": None,
                    "window_count": None,
                }
                print(
                    f"\n[QUEUE_PROGRESS] {json.dumps(prog)}",
                    flush=True,
                )
                write_engine_status(ctx, prog)
                queue_summary[idx - 1]["pct"] = pct
                _sync_queue()

            # Generate mock video with FFmpeg
            frames = t.get("video_length", 107)
            dur = frames / ctx.fps
            res = t.get("resolution") or ctx.current_resolution
            w, h = [int(x) for x in res.split("x")]
            cmd = [
                str(ctx.ffmpeg), "-y",
                "-f", "lavfi", "-i", f"color=c=0x1e293b:s={w}x{h}:r={ctx.fps}:d={dur:.3f}",
            ]
            audio_file = t.get("audio_guide")
            if audio_file and Path(audio_file).exists() and Path(audio_file).stat().st_size > 100:
                cmd.extend(["-i", str(audio_file), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest"])
            else:
                cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest"])

            cmd.extend([
                "-vf", f"drawtext=text='[DUMMY RENDER - {out_name}]':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", f"{dur:.3f}", str(out_mp4),
            ])
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elapsed = round(time.time() - t0, 1)
            print(f"      -> [DUMMY] Generated mock {out_mp4.name} in {elapsed:.1f}s", flush=True)
            print(
                f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'elapsed': elapsed})}",
                flush=True,
            )
            queue_summary[idx - 1]["status"] = "completed"
            queue_summary[idx - 1]["elapsed"] = elapsed
            for k in ("estimated_remaining_seconds", "estimated_remaining_str"): queue_summary[idx - 1].pop(k, None)
            _sync_queue()
            write_engine_status(
                ctx,
                {
                    "state": "completed",
                    "phase": "completed",
                    "raw_phase": "Completed",
                    "pct": 100.0,
                    "elapsed": elapsed,
                },
            )

            task_hashes[out_name] = t_hash
            settings_store[out_name] = render_settings_of(t)
            scene_renders = state.setdefault("scene_renders", {})
            scene_renders[out_name] = {
                "render_start": t.get("render_start", 0.0),
                "render_end": t.get("render_end", 0.0),
                "frames": t.get("video_length", 0),
                "elapsed": elapsed,
                "completed_at": round(time.time(), 3),
            }
            for img_k in ("image_start", "image_end"):
                img_p = t.get(img_k)
                if img_p and Path(img_p).exists():
                    aid_k = extract_anchor_id(Path(img_p).name)
                    if aid_k.startswith("A") and aid_k[1:].isdigit():
                        state.setdefault("anchor_file_hashes", {})[aid_k] = _file_sha256(Path(img_p))
            ctx.save_state_hashes(state)
            after_scene_render(ctx, sc_obj, pass_num, out_name, out_mp4, t_hash, t, elapsed, state)

            if "_Pass1_Left" not in out_name and not (has_scene_filter or no_stitch):
                assemble_progressive_master(ctx, scene_meta, log_prefix="      ")
            continue

        session = lazy_session.get()
        print(
            f"  [{idx:02d}/{len(all_tasks)}] Rendering {out_mp4.name} [{reason}] "
            f"(model={t['model_type']}, {t['video_length']}f, vpt={t['video_prompt_type'] or 'S'})...",
            flush=True,
        )
        print(
            f"[QUEUE_ACTIVE] {json.dumps({'idx': idx, 'total': len(all_tasks), 'filename': out_mp4.name, 'reason': reason, 'model': t.get('model_type'), 'frames': t.get('video_length'), 'resolution': t.get('resolution')})}",
            flush=True,
        )
        queue_summary[idx - 1]["status"] = "rendering"
        for k in ("pct", "phase", "raw_phase", "status_text", "step", "total_steps", "window_index", "window_count", "estimated_remaining_seconds", "estimated_remaining_str"):
            queue_summary[idx - 1].pop(k, None)
        _sync_queue()
        before_scene_render(ctx, sc_obj, out_name, out_mp4, state)

        wangp_task = {k: v for k, v in t.items() if k not in ("render_start", "render_end")}
        try:
            res, was_cancelled, t0 = execute_wgp_job(
                ctx=ctx, session=session, task_cfg=wangp_task, idx=idx, total=len(all_tasks),
                filename=out_mp4.name, stage="scene", reason=reason, model=str(t.get("model_type") or ""),
                frames=int(t.get("video_length") or 0), resolution=str(t.get("resolution") or ""),
                on_progress_item=queue_summary[idx - 1], sync_queue_fn=_sync_queue,
            )
        except BaseException:
            restore_previous_active_take_if_missing(ctx, sid, out_name, out_mp4)
            raise
        if was_cancelled:
            restore_previous_active_take_if_missing(ctx, sid, out_name, out_mp4)
            clear_cancel_request(ctx)
            queue_summary[idx - 1]["status"] = "queued"
            queue_summary[idx - 1]["reason"] = "STOPPED BY USER"
            _sync_queue()
            write_engine_status(ctx, {"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user"})
            raise RuntimeError(f"Cancelled rendering {out_mp4.name}")
        if not res.success or not res.generated_files:
            restore_previous_active_take_if_missing(ctx, sid, out_name, out_mp4)
            err_list = [e.message for e in (res.errors or [])]
            queue_summary[idx - 1]["status"] = "failed"
            queue_summary[idx - 1]["error"] = err_list
            _sync_queue()
            write_engine_status(ctx, {"state": "failed", "phase": "failed", "error": err_list})
            print(
                f"[QUEUE_FAIL] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'error': err_list})}",
                flush=True,
            )
            raise RuntimeError(f"Failed rendering {out_mp4.name}: {err_list}")

        gen_path = Path(res.generated_files[0])
        if gen_path.resolve() != out_mp4.resolve():
            shutil.copy2(gen_path, out_mp4)
        elapsed = round(time.time() - t0, 1)
        print(f"      -> Completed {out_mp4.name} in {elapsed:.1f}s", flush=True)
        print(
            f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': out_mp4.name, 'elapsed': elapsed})}",
            flush=True,
        )
        queue_summary[idx - 1]["status"] = "completed"
        queue_summary[idx - 1]["elapsed"] = elapsed
        for k in ("estimated_remaining_seconds", "estimated_remaining_str"): queue_summary[idx - 1].pop(k, None)
        _sync_queue()
        write_engine_status(
            ctx,
            {
                "state": "completed",
                "phase": "completed",
                "raw_phase": "Completed",
                "pct": 100.0,
                "elapsed": elapsed,
            },
        )

        task_hashes[out_name] = t_hash
        settings_store[out_name] = render_settings_of(t)
        scene_renders = state.setdefault("scene_renders", {})
        scene_renders[out_name] = {
            "render_start": t.get("render_start", 0.0),
            "render_end": t.get("render_end", 0.0),
            "frames": t.get("video_length", 0),
            "elapsed": elapsed,
            "completed_at": round(time.time(), 3),
        }
        for img_k in ("image_start", "image_end"):
            img_p = t.get(img_k)
            if img_p and Path(img_p).exists():
                aid_k = extract_anchor_id(Path(img_p).name)
                if aid_k.startswith("A") and aid_k[1:].isdigit():
                    state.setdefault("anchor_file_hashes", {})[aid_k] = _file_sha256(Path(img_p))
        ctx.save_state_hashes(state)
        after_scene_render(ctx, sc_obj, pass_num, out_name, out_mp4, t_hash, t, elapsed, state)

        # Update progressive master if this pass completed a full scene (not an intermediate pass 1 guide)
        if "_Pass1_Left" not in out_name and not (has_scene_filter or no_stitch):
            assemble_progressive_master(ctx, scene_meta, log_prefix="      ", progressive=True)

    return scene_meta
