import json
import re
from pathlib import Path

from .context import ProjectContext, resolve_scene_padding
from .fal_catalog import resolve_fal_route, resolve_minimax_route, resolve_task_backend
from .media_utils import _file_sha256, _sha256
from .upscale import stage2_filters_hash_payload
from .prompts import (
    compile_modular_scene_prompt,
    resolve_marker_prompt,
    resolve_scene_negative_prompt,
    resolve_scene_refs,
)


def _scene_id_matches(tok: str, sid: str) -> bool:
    t = tok.strip().upper()
    s = sid.strip().upper()
    if t == s:
        return True
    m_tok = re.match(r"^S0*(\d+)([A-Z_]*)$", t)
    m_sid = re.match(r"^S0*(\d+)([A-Z_]*)$", s)
    if m_tok and m_sid:
        tok_num, tok_suf = int(m_tok.group(1)), m_tok.group(2)
        sid_num, sid_suf = int(m_sid.group(1)), m_sid.group(2)
        return tok_num == sid_num and tok_suf == sid_suf
    return False


def match_scene_pass(force_scenes: list[str], sid: str, pass_num: int | None) -> bool:
    for raw in force_scenes:
        tok = raw.strip().upper()
        if tok in ("ALL", "ALL-SCENES"):
            return True
        if ":" in tok:
            base, sub = tok.split(":", 1)
            if _scene_id_matches(base, sid):
                if sub in ("P1", "PASS1", "1", "LEFT", "M") and pass_num in (None, 1):
                    return True
                if sub in ("P2", "PASS2", "2", "RIGHT", "F") and pass_num in (None, 2):
                    return True
        elif _scene_id_matches(tok, sid):
            return True
    return False


def _stem_matches_legacy_render(ctx: ProjectContext, sid: str, stem_key: str, default_stem: str) -> bool:
    """Returns True if stem_key is either the mode's default stem or already has a sliced audio file on disk."""
    if stem_key == default_stem:
        return True
    skey = (stem_key or "").strip().lower()
    if not skey or skey in ("silent", "none"):
        return default_stem in ("", "silent", "none")
    safe_stem_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem_key)
    return (ctx.audio_dir / f"{sid}_{safe_stem_tag}.wav").exists()


def _resolve_override_backend(ctx: ProjectContext, sc: dict, vrspec: dict, pass_num: int | None, out_name: str, backend_overrides: dict | None) -> str:
    if isinstance(backend_overrides, dict):
        sid = str(sc.get("id") or "")
        ovr = backend_overrides.get(out_name) or backend_overrides.get((sid.upper(), pass_num)) or backend_overrides.get((sid, pass_num)) or backend_overrides.get(sid.upper()) or backend_overrides.get(sid)
        norm = str(ovr or "").strip().lower()
        if norm in ("local", "fal", "minimax"):
            return norm
    return resolve_task_backend(ctx, sc=sc, rspec=vrspec, pass_num=pass_num, stage="video")


def load_queue_backend_overrides(ctx: ProjectContext, force_scenes: list[str] | None = None) -> dict:
    """Loads persisted per-pass backend overrides from v3_queue.json overlaid with active CLI target --backend."""
    overrides: dict = {}
    q_file = getattr(ctx, "jobs_dir", Path(".")) / "v3_queue.json"
    if q_file.is_file():
        try:
            q_data = json.loads(q_file.read_text(encoding="utf-8"))
            items = q_data if isinstance(q_data, list) else (q_data.get("queue", []) if isinstance(q_data, dict) else [])
            for itm in items:
                if not isinstance(itm, dict):
                    continue
                ovr = str(itm.get("backend_override") or itm.get("override_backend") or "").strip().lower()
                if ovr in ("local", "fal", "minimax"):
                    if itm.get("output_filename"):
                        overrides[str(itm["output_filename"])] = ovr
                    if itm.get("scene_id"):
                        overrides[(str(itm["scene_id"]).upper(), itm.get("pass"))] = ovr
        except (OSError, ValueError):
            pass
    cli_b = str(getattr(ctx, "cli_backend", "") or "").strip().lower()
    if cli_b in ("local", "fal", "minimax") and force_scenes:
        for sc in getattr(ctx, "scenes", []):
            sid = str(sc.get("id") or "")
            for p_n in (None, 1, 2):
                if match_scene_pass(force_scenes, sid, p_n):
                    overrides[(sid.upper(), p_n)] = cli_b
    return overrides


def compute_project_task_hashes(
    ctx: ProjectContext,
    target_resolution: str | None = None,
    backend_overrides: dict | None = None,
) -> dict[str, tuple[str, ...]]:
    """Computes (current_hash, *compat_hashes) for every video pass in the project.
    Single source of truth used by both server.py and run_stage2_scenes.
    Includes audio stem selection (`stem`, `stem_m`, `stem_f`) in `current_hash`, while
    preserving backward-compatible hashes for existing unedited scenes.
    """
    results: dict[str, tuple[str, ...]] = {}
    vshared = ctx.video_shared_cfg
    res_override = ctx.resolve_resolution(target_resolution) if target_resolution else None

    for sc in ctx.scenes:
        sid = sc["id"]
        start = float(sc.get("start", 0))
        end = float(sc.get("end", 0))
        mode = sc.get("mode", "solo")
        aid, anchor_path = ctx.resolve_anchor_path(sc.get("anchor", ""))
        anchor_fn = anchor_path.name
        a_hash = f"{_file_sha256(anchor_path)}"
        head_pad, tail_pad = ctx.get_scene_render_padding(sc)
        default_head, default_tail = resolve_scene_padding({"start": start, "end": end, "anchor_end": sc.get("anchor_end")}, ctx.head_overlap_s, ctx.tail_overlap_s)
        has_custom_pad = (head_pad, tail_pad) != (default_head, default_tail)

        if mode in ("solo", "male_solo", "female_solo", "silent_locked", "silent", "ambient", "single_pass", "duet_method_a", "duet_a"):
            vrid, vrspec = ctx.resolve_video_renderer(sc)
            vmodel = vrspec["model_type"]
            res_str = ctx.resolve_scene_resolution(sc, vrspec, res_override=res_override)
            eff_up = ctx.resolve_scene_spatial_upsampling(sc, vrspec, res_str=res_str, pass_num=None)
            if mode in ("duet_method_a", "duet_a"):
                stem_l = sc.get("stem_m") or sc.get("stem_left") or sc.get("stem_l") or "male"
                stem_r = sc.get("stem_f") or sc.get("stem_right") or sc.get("stem_r") or "female"
                stem_key = f"{stem_l}_{stem_r}"
                default_stem = stem_key
                cand_name = None
                if ctx and hasattr(ctx, "renders_dir") and not sc.get("output_name"):
                    for c in (f"{sid}_duet_a", f"{sid}_duet_method_a", f"{sid}_duet_stereo"):
                        if (ctx.renders_dir / f"{c}_{res_str}.mp4").exists():
                            cand_name = c
                            break
                raw_out_name = sc.get("output_name", cand_name or f"{sid}_duet_a")
            elif mode == "male_solo":
                default_stem = "male"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_male_solo")
            elif mode == "female_solo":
                default_stem = "female"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_female_solo")
            elif mode in ("silent_locked", "silent", "ambient"):
                default_stem = "silent"
                stem_key = "silent"
                raw_out_name = sc.get("output_name", f"{sid}_silent")
            elif mode == "solo":
                default_stem = "male"
                stem_key = sc.get("stem", default_stem)
                cand_name = None
                if ctx and hasattr(ctx, "renders_dir") and not sc.get("output_name"):
                    for c in (f"{sid}_solo", f"{sid}_male_solo", f"{sid}_female_solo", f"{sid}_{stem_key}"):
                        if (ctx.renders_dir / f"{c}_{res_str}.mp4").exists():
                            cand_name = c
                            break
                raw_out_name = sc.get("output_name", cand_name or f"{sid}_solo")
            else:
                default_stem = "silent"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_{stem_key}")

            out_name = f"{raw_out_name}_{res_str}" if res_str not in raw_out_name else raw_out_name

            raw_prompt = compile_modular_scene_prompt(sc, pass_num=None)
            from .characters import compile_character_prompt, expand_prompt_with_characters, find_prompt_entities

            expanded_prompt, leg_prompt_raw = expand_prompt_with_characters(
                raw_prompt, ctx.variables, getattr(ctx, "tag_lookup", None)
            )
            req_start = max(0.0, start - head_pad)
            m_fps = ctx.model_fps(vrspec)
            expanded_prompt = resolve_marker_prompt(expanded_prompt, req_start, ctx.markers, fps=m_fps)
            leg_expanded_prompt = (
                resolve_marker_prompt(leg_prompt_raw, req_start, ctx.markers, fps=m_fps)
                if leg_prompt_raw is not None
                else None
            )
            scene_seed = int(sc.get("seed") if sc.get("seed") is not None else (sc.get("seed_m") or 1000))

            flow_shift = vrspec.get("flow_shift", vshared.get("flow_shift", 12.0)) if ("flow_shift" in vrspec or "flow_shift" in vshared) else None
            base_payload = {
                "sid": sid, "mode": mode,
                "anchor": anchor_fn, "a_hash": a_hash,
                "prompt": expanded_prompt, "seed": scene_seed,
                "resolution": res_str,
                "model_type": vmodel,
                "steps": vrspec.get("num_inference_steps"),
                "phases": vrspec.get("guidance_phases"),
                "flow_shift": flow_shift,
                "sample_solver": vrspec.get("sample_solver", vshared.get("sample_solver", "euler")),
                "spatial_upsampling": eff_up,
                "config": vrspec.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            _, neg_sig = resolve_scene_negative_prompt(sc, vrspec, vshared, pass_num=None)
            if neg_sig:
                base_payload["neg_prompt"] = neg_sig
            base_payload.update(stage2_filters_hash_payload(ctx.resolve_scene_filters(sc, vrspec, pass_num=None), eff_up))
            if has_custom_pad:
                base_payload["pad"] = [head_pad, tail_pad]
            eff_backend = _resolve_override_backend(ctx, sc, vrspec, None, out_name, backend_overrides)
            if eff_backend == "fal":
                base_payload["backend"] = "fal"
                base_payload["fal_endpoint"] = resolve_fal_route(vrspec, sc=sc, stage="video")["endpoint"]
            from .fal_catalog import is_ref2va_spec, read_local_config
            custom_app = read_local_config(getattr(ctx, "project_dir", None)).get("custom_fal_app", "")
            if eff_backend == "fal":
                base_payload["backend"] = "fal"
                base_payload["fal_endpoint"] = resolve_fal_route(vrspec, sc=sc, stage="video", custom_fal_app=custom_app)["endpoint"]
            elif eff_backend == "minimax":
                mm_rt = resolve_minimax_route(vrspec, sc=sc, resolution=res_str)
                base_payload["backend"] = "minimax"
                base_payload["minimax_model"] = mm_rt["minimax_model"]
            is_ref2va = is_ref2va_spec(vrspec)
            supports_end = bool(vrspec.get("supports_end_anchor", not is_ref2va))
            if supports_end and sc.get("anchor_end"):
                _, a_end_p = ctx.resolve_anchor_path(sc.get("anchor_end"))
                if a_end_p.exists():
                    base_payload["anchor_end"] = a_end_p.name
                    base_payload["a_end_hash"] = f"{_file_sha256(a_end_p)}"
            if is_ref2va:
                cand_refs = ctx.resolve_scene_image_refs(sc, raw_prompt)
                resolved_refs: list[Path] = []
                if getattr(ctx, "tag_lookup", None):
                    if find_prompt_entities(raw_prompt, ctx.tag_lookup):
                        _, resolved_refs = compile_character_prompt(raw_prompt, ctx.tag_lookup, True, fallback_refs=cand_refs)
                if not resolved_refs:
                    resolved_refs, _ = resolve_scene_refs(expanded_prompt, cand_refs)
                if resolved_refs:
                    base_payload["refs"] = [(p.name, _file_sha256(p)) for p in resolved_refs]
                    base_payload["ref_prompt_v"] = 1
            t_payload = dict(base_payload, stem=stem_key)
            t_hash = _sha256(t_payload)
            t_payload_leg = dict(t_payload, start=start, end=end)
            valid_hashes = [t_hash, _sha256(t_payload_leg)]

            if leg_expanded_prompt is not None:
                leg_payload = dict(base_payload, prompt=leg_expanded_prompt, stem=stem_key)
                valid_hashes.extend([_sha256(leg_payload), _sha256(dict(leg_payload, start=start, end=end))])

            if _stem_matches_legacy_render(ctx, sid, stem_key, default_stem):
                h_nostem = _sha256(base_payload)
                h_nostem_leg = _sha256(dict(base_payload, start=start, end=end))
                valid_hashes.extend([h_nostem, h_nostem_leg])

            results[out_name] = tuple(valid_hashes)

        elif mode in ("duet", "duet_method_b", "two_pass_gva", "gva_male_left_silent_right"):
            vrid_p1, vrspec_p1 = ctx.resolve_video_renderer(sc, pass_num=1)
            vrid_p2, vrspec_p2 = ctx.resolve_video_renderer(sc, pass_num=2)
            vmodel_p1 = vrspec_p1["model_type"]
            vmodel_p2 = vrspec_p2["model_type"]
            res_str = ctx.resolve_scene_resolution(sc, vrspec_p2, res_override=res_override)
            eff_up_p1 = ctx.resolve_scene_spatial_upsampling(sc, vrspec_p1, res_str=res_str, pass_num=1)
            eff_up_p2 = ctx.resolve_scene_spatial_upsampling(sc, vrspec_p2, res_str=res_str, pass_num=2)
            out_p1 = f"{sid}_Pass1_Left_{res_str}"
            out_p2 = f"{sid}_Pass2_Final_{res_str}"

            default_stem_m = "male"
            stem_m = sc.get("stem_m") or sc.get("stem_p1") or sc.get("stem") or default_stem_m
            default_stem_f = "silent" if mode == "gva_male_left_silent_right" else "female"
            stem_f = sc.get("stem_f") or sc.get("stem_p2") or default_stem_f

            raw_m = compile_modular_scene_prompt(sc, pass_num=1)
            raw_f = compile_modular_scene_prompt(sc, pass_num=2)
            from .characters import compile_character_prompt, expand_prompt_with_characters, find_prompt_entities

            exp_prompt_m, leg_m_raw = expand_prompt_with_characters(raw_m, ctx.variables, getattr(ctx, "tag_lookup", None))
            req_start = max(0.0, start - head_pad)
            fps_p1 = ctx.model_fps(vrspec_p1)
            fps_p2 = ctx.model_fps(vrspec_p2)
            exp_prompt_m = resolve_marker_prompt(exp_prompt_m, req_start, ctx.markers, fps=fps_p1)
            leg_prompt_m = resolve_marker_prompt(leg_m_raw, req_start, ctx.markers, fps=fps_p1) if leg_m_raw is not None else None
            exp_prompt_f, leg_f_raw = expand_prompt_with_characters(raw_f, ctx.variables, getattr(ctx, "tag_lookup", None))
            exp_prompt_f = resolve_marker_prompt(exp_prompt_f, req_start, ctx.markers, fps=fps_p2)
            leg_prompt_f = resolve_marker_prompt(leg_f_raw, req_start, ctx.markers, fps=fps_p2) if leg_f_raw is not None else None

            seed_m = int(sc.get("seed_m") if sc.get("seed_m") is not None else (sc.get("seed") or 1000))
            seed_f = int(sc.get("seed_f") if sc.get("seed_f") is not None else (sc.get("seed") or 1000))
            split_x = float(sc.get("split_x", 0.50))

            p1_flow = vrspec_p1.get("flow_shift", vshared.get("flow_shift", 12.0)) if ("flow_shift" in vrspec_p1 or "flow_shift" in vshared) else None
            p1_base = {
                "sid": sid, "pass": 1, "mode": mode,
                "anchor": anchor_fn, "a_hash": a_hash,
                "prompt_m": exp_prompt_m, "seed_m": seed_m,
                "resolution": res_str,
                "model_type": vmodel_p1,
                "steps": vrspec_p1.get("num_inference_steps"),
                "phases": vrspec_p1.get("guidance_phases"),
                "flow_shift": p1_flow,
                "sample_solver": vrspec_p1.get("sample_solver", vshared.get("sample_solver", "euler")),
                "spatial_upsampling": eff_up_p1,
                "config": vrspec_p1.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec_p1.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            _, neg_sig_p1 = resolve_scene_negative_prompt(sc, vrspec_p1, vshared, pass_num=1)
            if neg_sig_p1:
                p1_base["neg_prompt"] = neg_sig_p1
            if has_custom_pad:
                p1_base["pad"] = [head_pad, tail_pad]
            p1_base.update(stage2_filters_hash_payload(ctx.resolve_scene_filters(sc, vrspec_p1, pass_num=1), eff_up_p1))
            from .fal_catalog import is_ref2va_spec, read_local_config
            custom_app = read_local_config(getattr(ctx, "project_dir", None)).get("custom_fal_app", "")
            eff_backend_p1 = _resolve_override_backend(ctx, sc, vrspec_p1, 1, out_p1, backend_overrides)
            if eff_backend_p1 == "fal":
                p1_base["backend"] = "fal"
                p1_base["fal_endpoint"] = resolve_fal_route(vrspec_p1, sc=sc, pass_num=1, stage="video", custom_fal_app=custom_app)["endpoint"]
            elif eff_backend_p1 == "minimax":
                mm_rt1 = resolve_minimax_route(vrspec_p1, sc=sc, pass_num=1, resolution=res_str)
                p1_base["backend"] = "minimax"
                p1_base["minimax_model"] = mm_rt1["minimax_model"]
            is_ref2va_p1 = is_ref2va_spec(vrspec_p1)
            if bool(vrspec_p1.get("supports_end_anchor", not is_ref2va_p1)) and sc.get("anchor_end"):
                _, a_end_p1 = ctx.resolve_anchor_path(sc.get("anchor_end"))
                if a_end_p1.exists():
                    p1_base["anchor_end"] = a_end_p1.name
                    p1_base["a_end_hash"] = f"{_file_sha256(a_end_p1)}"
            if is_ref2va_p1:
                cand_refs = ctx.resolve_scene_image_refs(sc, raw_m)
                resolved_p1: list[Path] = []
                if getattr(ctx, "tag_lookup", None):
                    if find_prompt_entities(raw_m, ctx.tag_lookup):
                        _, resolved_p1 = compile_character_prompt(raw_m, ctx.tag_lookup, True, fallback_refs=cand_refs)
                if not resolved_p1:
                    resolved_p1, _ = resolve_scene_refs(exp_prompt_m, cand_refs)
                if resolved_p1:
                    p1_base["refs"] = [(p.name, _file_sha256(p)) for p in resolved_p1]
                    p1_base["ref_prompt_v"] = 1
            p1_payload = dict(p1_base, stem_m=stem_m)
            h_p1 = _sha256(p1_payload)
            h_p1_leg = _sha256(dict(p1_payload, start=start, end=end))
            h_p1_nostem = _sha256(p1_base)
            h_p1_nostem_leg = _sha256(dict(p1_base, start=start, end=end))

            p1_valid = [h_p1, h_p1_leg]
            if leg_prompt_m is not None:
                p1_leg_base = dict(p1_base, prompt_m=leg_prompt_m, stem_m=stem_m)
                p1_valid.extend([_sha256(p1_leg_base), _sha256(dict(p1_leg_base, start=start, end=end))])
            p1_matches_legacy = _stem_matches_legacy_render(ctx, sid, stem_m, default_stem_m)
            if p1_matches_legacy:
                p1_valid.extend([h_p1_nostem, h_p1_nostem_leg])
            results[out_p1] = tuple(p1_valid)

            p2_flow = vrspec_p2.get("flow_shift", vshared.get("flow_shift", 12.0)) if ("flow_shift" in vrspec_p2 or "flow_shift" in vshared) else None
            p2_base = {
                "sid": sid, "pass": 2, "mode": mode,
                "h_p1": h_p1, "split_x": split_x,
                "prompt_f": exp_prompt_f, "seed_f": seed_f,
                "resolution": res_str,
                "model_type": vmodel_p2,
                "steps": vrspec_p2.get("num_inference_steps"),
                "phases": vrspec_p2.get("guidance_phases"),
                "flow_shift": p2_flow,
                "sample_solver": vrspec_p2.get("sample_solver", vshared.get("sample_solver", "euler")),
                "spatial_upsampling": eff_up_p2,
                "config": vrspec_p2.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec_p2.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            _, neg_sig_p2 = resolve_scene_negative_prompt(sc, vrspec_p2, vshared, pass_num=2)
            if neg_sig_p2:
                p2_base["neg_prompt"] = neg_sig_p2
            from .duet_roi import normalize_roi_box, normalize_roi_mode, normalize_roi_side
            roi_mode_p2 = normalize_roi_mode(sc.get("pass2_roi_mode"), default="full")
            if roi_mode_p2 != "full":
                p2_base["roi_mode"] = roi_mode_p2
                roi_side_p2 = normalize_roi_side(sc.get("pass2_roi_side"), default="right")
                if roi_side_p2 != "right":
                    p2_base["roi_side"] = roi_side_p2
                roi_box_p2 = normalize_roi_box(sc.get("pass2_roi_box"))
                if roi_mode_p2 == "custom" and roi_box_p2:
                    p2_base["roi_box"] = roi_box_p2
            p2_base.update(stage2_filters_hash_payload(ctx.resolve_scene_filters(sc, vrspec_p2, pass_num=2), eff_up_p2))
            eff_backend_p2 = _resolve_override_backend(ctx, sc, vrspec_p2, 2, out_p2, backend_overrides)
            if eff_backend_p2 == "fal":
                p2_base["backend"] = "fal"
                p2_base["fal_endpoint"] = resolve_fal_route(vrspec_p2, sc=sc, pass_num=2, stage="video", custom_fal_app=custom_app)["endpoint"]
            elif eff_backend_p2 == "minimax":
                mm_rt2 = resolve_minimax_route(vrspec_p2, sc=sc, pass_num=2, resolution=res_str)
                p2_base["backend"] = "minimax"
                p2_base["minimax_model"] = mm_rt2["minimax_model"]
            if is_ref2va_spec(vrspec_p2):
                cand_refs = ctx.resolve_scene_image_refs(sc, raw_f)
                resolved_p2: list[Path] = []
                if getattr(ctx, "tag_lookup", None):
                    if find_prompt_entities(raw_f, ctx.tag_lookup):
                        _, resolved_p2 = compile_character_prompt(raw_f, ctx.tag_lookup, True, fallback_refs=cand_refs)
                if not resolved_p2:
                    resolved_p2, _ = resolve_scene_refs(exp_prompt_f, cand_refs)
                if resolved_p2:
                    p2_base["refs"] = [(p.name, _file_sha256(p)) for p in resolved_p2]
                    p2_base["ref_prompt_v"] = 1
            p2_payload = dict(p2_base, stem_f=stem_f)
            h_p2 = _sha256(p2_payload)
            h_p2_leg = _sha256(dict(p2_payload, start=start, end=end, h_p1=h_p1_leg))

            p2_valid = [h_p2, h_p2_leg]
            if leg_prompt_f is not None or leg_prompt_m is not None:
                h_p1_for_leg = _sha256(dict(p1_base, prompt_m=leg_prompt_m or exp_prompt_m, stem_m=stem_m))
                p2_leg_base = dict(p2_base, h_p1=h_p1_for_leg, prompt_f=leg_prompt_f or exp_prompt_f, stem_f=stem_f)
                p2_valid.extend([_sha256(p2_leg_base), _sha256(dict(p2_leg_base, start=start, end=end))])
            if p1_matches_legacy and _stem_matches_legacy_render(ctx, sid, stem_f, default_stem_f):
                p2_nostem = dict(p2_base, h_p1=h_p1_nostem)
                p2_nostem_leg = dict(p2_base, h_p1=h_p1_nostem_leg, start=start, end=end)
                p2_valid.extend([_sha256(p2_nostem), _sha256(p2_nostem_leg)])
            results[out_p2] = tuple(p2_valid)

    return results


__all__ = [
    "_scene_id_matches",
    "match_scene_pass",
    "_stem_matches_legacy_render",
    "compute_project_task_hashes",
]
