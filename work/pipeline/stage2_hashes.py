import re
from pathlib import Path

from .context import ProjectContext, resolve_scene_padding
from .media_utils import _file_sha256, _sha256
from .prompts import resolve_marker_prompt, resolve_scene_refs


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


def compute_project_task_hashes(ctx: ProjectContext, target_resolution: str | None = None) -> dict[str, tuple[str, ...]]:
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

        if mode in ("solo", "male_solo", "female_solo", "silent_locked", "single_pass"):
            vrid, vrspec = ctx.resolve_video_renderer(sc)
            vmodel = vrspec["model_type"]
            res_str = res_override or ctx.resolve_resolution(vrspec.get("resolution", ctx.current_resolution))
            if mode == "male_solo":
                default_stem = "male"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_male_solo")
            elif mode == "female_solo":
                default_stem = "female"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_female_solo")
            elif mode == "silent_locked":
                default_stem = "silent"
                stem_key = "silent"
                raw_out_name = sc.get("output_name", f"{sid}_silent")
            elif mode == "solo":
                default_stem = "male"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_solo")
            else:
                default_stem = "silent"
                stem_key = sc.get("stem", default_stem)
                raw_out_name = sc.get("output_name", f"{sid}_{stem_key}")

            out_name = f"{raw_out_name}_{res_str}" if res_str not in raw_out_name else raw_out_name

            raw_prompt = sc.get("prompt") or sc.get("prompt_m") or sc.get("prompt_f") or ""
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
                "spatial_upsampling": vrspec.get("spatial_upsampling", vshared.get("spatial_upsampling", "")),
                "config": vrspec.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            if has_custom_pad:
                base_payload["pad"] = [head_pad, tail_pad]
            is_ref2va = bool(
                vrspec.get("supports_image_refs")
                or str(vrspec.get("frames_formula", "")).lower() == "h3_ref2va"
                or "ref2va" in vmodel.lower()
                or "fasth3" in vmodel.lower()
                or "singularity" in vmodel.lower()
            )
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

        elif mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
            vrid_p1, vrspec_p1 = ctx.resolve_video_renderer(sc, pass_num=1)
            vrid_p2, vrspec_p2 = ctx.resolve_video_renderer(sc, pass_num=2)
            vmodel_p1 = vrspec_p1["model_type"]
            vmodel_p2 = vrspec_p2["model_type"]
            res_str = res_override or ctx.resolve_resolution(vrspec_p2.get("resolution", ctx.current_resolution))
            out_p1 = f"{sid}_Pass1_Left_{res_str}"
            out_p2 = f"{sid}_Pass2_Final_{res_str}"

            default_stem_m = "male"
            stem_m = sc.get("stem_m") or sc.get("stem_p1") or sc.get("stem") or default_stem_m
            default_stem_f = "female" if mode == "duet_method_b" else "silent"
            stem_f = sc.get("stem_f") or sc.get("stem_p2") or default_stem_f

            raw_m = sc.get("prompt_m") or sc.get("prompt") or ""
            raw_f = sc.get("prompt_f") or sc.get("prompt") or ""
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
                "spatial_upsampling": vrspec_p1.get("spatial_upsampling", vshared.get("spatial_upsampling", "")),
                "config": vrspec_p1.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec_p1.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            if has_custom_pad:
                p1_base["pad"] = [head_pad, tail_pad]
            is_ref2va_p1 = bool(
                vrspec_p1.get("supports_image_refs")
                or str(vrspec_p1.get("frames_formula", "")).lower() == "h3_ref2va"
                or "ref2va" in vmodel_p1.lower()
                or "fasth3" in vmodel_p1.lower()
                or "singularity" in vmodel_p1.lower()
            )
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
                "spatial_upsampling": vrspec_p2.get("spatial_upsampling", vshared.get("spatial_upsampling", "")),
                "config": vrspec_p2.get("config", vshared.get("config", "")),
                "skip_steps_cache_type": vrspec_p2.get("skip_steps_cache_type", vshared.get("skip_steps_cache_type", "")),
            }
            if bool(vrspec_p2.get("supports_image_refs") or "ref2va" in vmodel_p2.lower() or "fasth3" in vmodel_p2.lower()):
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
