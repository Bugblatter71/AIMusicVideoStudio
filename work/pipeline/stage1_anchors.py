import base64
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .context import ProjectContext
from .fal_catalog import resolve_fal_route, resolve_task_backend
from .media_utils import _file_sha256, _sha256, resolve_path
from .prompts import adapt_ref_tags, align_anchor_resolution, expand_variables, extract_anchor_id, find_ref_tags
from .wgp_bridge import (
    LazyWgpSession,
    check_cancel_requested,
    clear_cancel_request,
    execute_wgp_job,
    resolve_session_for_backend,
    write_engine_status,
)


def match_anchor_id(token: str, filename: str) -> bool:
    t = re.split(r"[:*]", token.strip(), maxsplit=1)[0].strip().upper()
    aid_t = extract_anchor_id(t).upper()
    aid_fn = extract_anchor_id(filename).upper()
    return (
        t in ("ALL", "ALL-ANCHORS")
        or aid_t == aid_fn
        or t in (filename.upper(), Path(filename).stem.upper())
    )


def compile_anchor_prompt_and_refs(
    ctx: ProjectContext, sp: dict, rspec: dict
) -> tuple[str, str, list[str]]:
    """Compiles an anchor prompt with {VAR} + @character expansion and resolves ordered reference images."""
    from .characters import expand_prompt_with_characters, find_prompt_entities

    raw_prompt = str(sp.get("prompt") or "")
    tag_lookup = getattr(ctx, "tag_lookup", None) or {}
    raw_var_expanded = expand_variables(raw_prompt, ctx.variables)
    canon_prompt, _ = expand_prompt_with_characters(raw_prompt, ctx.variables, tag_lookup)

    # Resolve ordered reference images: explicit sp["image_refs"] > @character refs > project shared anchor_refs
    resolved_refs: list[str] = []
    seen_paths: set[str] = set()
    matched_entities = (
        (find_prompt_entities(raw_prompt, tag_lookup) or find_prompt_entities(raw_var_expanded, tag_lookup))
        if tag_lookup
        else []
    )

    max_refs = max(0, int(rspec.get("max_image_refs") or 4))

    def _append_ref(p_like: str | Path) -> None:
        if len(resolved_refs) >= max_refs:
            return
        rp = resolve_path(ctx.project_dir, str(p_like)) if not Path(str(p_like)).is_absolute() else Path(str(p_like))
        try:
            key = str(rp.resolve())
        except OSError:
            key = str(rp)
        if rp.is_file() and key not in seen_paths:
            seen_paths.add(key)
            resolved_refs.append(str(rp))

    if isinstance(sp.get("image_refs"), list):
        for r in sp["image_refs"]:
            if r:
                _append_ref(r)
        prompt_for_model = canon_prompt
    elif find_ref_tags(raw_var_expanded):
        for r in ctx.anchor_refs:
            _append_ref(r)
        prompt_for_model = raw_var_expanded
    elif matched_entities:
        ent_pic_indices: dict[tuple[str, str], list[int]] = {}
        for ent in matched_entities:
            ek = (ent["entity_id"], ent["look_id"])
            ent_pic_indices[ek] = []
            for p in ent.get("image_refs", []):
                before_n = len(resolved_refs)
                _append_ref(p)
                rp = resolve_path(ctx.project_dir, str(p)) if not Path(str(p)).is_absolute() else Path(str(p))
                try:
                    rp_key = str(rp.resolve())
                except OSError:
                    rp_key = str(rp)
                if len(resolved_refs) > before_n:
                    ent_pic_indices[ek].append(len(resolved_refs))
                elif rp_key in seen_paths:
                    for i, cur in enumerate(resolved_refs, 1):
                        try:
                            if str(Path(cur).resolve()) == rp_key:
                                ent_pic_indices[ek].append(i)
                                break
                        except OSError:
                            pass

        def _repl_char(m):
            tok = m.group(1).lower()
            desc = tag_lookup.get(tok)
            if not desc:
                return m.group(1)
            idx_list = ent_pic_indices.get((desc["entity_id"], desc["look_id"])) or []
            base_txt = desc.get("text_expansion") or desc.get("name") or m.group(1)
            if idx_list:
                ref_tags = ", ".join(f"<image{n}>" for n in idx_list)
                return f"{base_txt} (from {ref_tags})"
            return base_txt

        prompt_for_model = re.sub(r"(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})", _repl_char, raw_prompt, flags=re.IGNORECASE)
        prompt_for_model = expand_variables(prompt_for_model, ctx.variables)
        if not resolved_refs:
            for r in ctx.anchor_refs:
                _append_ref(r)
    else:
        for r in ctx.anchor_refs:
            _append_ref(r)
        prompt_for_model = canon_prompt

    if not resolved_refs:
        prompt_for_model = re.sub(r"\s*\(from\s*<image\d+(?:,\s*<image\d+>)*>\)", "", prompt_for_model, flags=re.IGNORECASE)
        prompt_for_model = re.sub(r"<(?:image|Picture\s*)\d+>", "", prompt_for_model, flags=re.IGNORECASE)
        prompt_for_model = re.sub(r"  +", " ", prompt_for_model).strip()

    adapted = adapt_ref_tags(prompt_for_model, rspec.get("ref_tag_style", "<imageN>"))
    return canon_prompt, adapted, resolved_refs


def run_stage1_anchors(
    ctx: ProjectContext,
    lazy_session: LazyWgpSession,
    state: dict,
    force_anchors: list[str],
    sync_only: bool = False,
    dummy_render: bool = False,
) -> set[str]:
    def_rid = str(ctx.default_anchor_renderer)
    def_model = (ctx.anchor_renderers.get(def_rid) or ctx.fal_anchor_renderers.get(def_rid) or {}).get("model_type", "qwen_image_21_turbo_7B")
    print("\n" + "=" * 72, flush=True)
    print(
        f"STAGE 1: Checking / Detecting {len(ctx.anchors)} Anchors in {ctx.anchors_dir} "
        f"(Default Renderer #{def_rid}: {def_model})",
        flush=True,
    )
    print("=" * 72, flush=True)

    regenerated_anchors: set[str] = set()
    anchor_hashes = state.setdefault("anchors", {})
    anchor_file_hashes = state.setdefault("anchor_file_hashes", {})
    shared_cfg = ctx.anchor_shared_cfg

    for idx, sp in enumerate(ctx.anchors, 1):
        aid, target_file = ctx.resolve_anchor_path(sp)
        fn = target_file.name
        rid, rspec = ctx.resolve_anchor_renderer(sp)
        model_type = rspec["model_type"]

        raw_expanded, adapted_prompt, resolved_refs = compile_anchor_prompt_and_refs(ctx, sp, rspec)
        neg_val = "" if rspec.get("no_negative_prompt") else (sp.get("negative_prompt") if sp.get("negative_prompt") is not None else shared_cfg.get("negative_prompt", ""))
        res_val = align_anchor_resolution(
            str(sp.get("resolution") or "").strip()
            or str(shared_cfg.get("resolution") or "").strip()
            or str(rspec.get("resolution") or "").strip()
            or "1920x1080",
            model_type,
        )

        hash_payload = {
            "filename": fn,
            "seed": sp.get("seed", 42),
            "prompt": raw_expanded,
            "resolution": res_val,
        }
        if resolved_refs:
            hash_payload["refs"] = resolved_refs
        if str(neg_val or "").strip():
            hash_payload["neg_prompt"] = str(neg_val).strip()
        if model_type != "qwen_image_21_turbo_7B":
            hash_payload["model_type"] = model_type
            hash_payload["steps"] = rspec.get("num_inference_steps")
            hash_payload["cfg"] = rspec.get("guidance_scale", rspec.get("embedded_guidance_scale"))
        anchor_backend = resolve_task_backend(ctx, sc=sp, rspec=rspec, stage="anchor")
        if anchor_backend == "fal":
            hash_payload["backend"] = "fal"
            hash_payload["fal_endpoint"] = resolve_fal_route(rspec, sc=sp, stage="anchor")["endpoint"]
        cur_hash = _sha256(hash_payload)

        file_exists = target_file.exists() and target_file.stat().st_size > 1000
        cur_file_hash = _file_sha256(target_file) if file_exists else "missing"
        prev_file_hash = anchor_file_hashes.get(aid)

        if sync_only and file_exists:
            anchor_hashes[fn] = cur_hash
            anchor_file_hashes[aid] = cur_file_hash
            continue

        has_anchor_filter = bool(force_anchors) and not any(
            tok.strip().upper() in ("ALL", "ALL-ANCHORS") for tok in force_anchors
        )
        forced = any(match_anchor_id(tok, fn) for tok in force_anchors)
        if has_anchor_filter and not forced:
            continue

        file_changed = (prev_file_hash is not None and prev_file_hash != cur_file_hash)
        is_new_anchor = (prev_file_hash is None and file_exists)
        prev_cfg_hash = anchor_hashes.get(fn)
        config_changed = (
            prev_cfg_hash is not None
            and prev_cfg_hash != cur_hash
            and not sp.get("external")
            and bool(str(sp.get("prompt") or "").strip())
        )

        if file_exists and not forced:
            if is_new_anchor or file_changed:
                reason = "NEW ANCHOR FILE DETECTED" if is_new_anchor else "ANCHOR FILE UPDATED ON DISK"
                print(f"  [{idx:02d}/{len(ctx.anchors)}] {aid} ({fn}) [{reason}].", flush=True)
                anchor_hashes[fn] = cur_hash
                anchor_file_hashes[aid] = cur_file_hash
                ctx.save_state_hashes(state)
                regenerated_anchors.add(aid)
                regenerated_anchors.add(fn)
                continue
            elif not config_changed:
                print(f"  [{idx:02d}/{len(ctx.anchors)}] {aid} ({fn}) [OK / Up-to-date].", flush=True)
                continue

        if sp.get("external") and not str(sp.get("prompt") or "").strip() and not forced:
            print(f"  [{idx:02d}/{len(ctx.anchors)}] {aid} ({fn}) [External / No prompt — skipped].", flush=True)
            continue

        reason = "FORCED" if forced else ("CONFIG CHANGED" if file_exists else "MISSING FILE")
        from .anchor_takes import (
            archive_anchor_take_file,
            clean_anchor_queue_stem,
            load_anchor_takes_manifest,
            parse_anchor_batch_count,
            parse_anchor_explicit_seed,
            select_unused_take_seeds,
        )

        if file_exists:
            archive_anchor_take_file(
                ctx.anchors_dir, ctx.project_dir, aid, target_file, sp,
                seed=int(sp.get("seed", 42)), model_type=model_type, resolution=res_val, make_active=False,
            )
        explicit_seed = parse_anchor_explicit_seed(force_anchors, fn) if forced else None
        base_seed = int(sp.get("seed", 42))
        if explicit_seed is not None:
            batch_count = 1
            batch_seeds = [explicit_seed]
        else:
            batch_count = parse_anchor_batch_count(force_anchors, fn, sp) if forced else 1
            existing_takes = (load_anchor_takes_manifest(ctx.anchors_dir).get("anchors", {}).get(aid.upper()) or {}).get("takes") or []
            batch_seeds = select_unused_take_seeds(existing_takes, base_seed, batch_count)

        for b_idx, seed_val in enumerate(batch_seeds):
            batch_lbl = f" [Take {b_idx + 1}/{batch_count}]" if batch_count > 1 else ""
            if explicit_seed is not None:
                q_stem = clean_anchor_queue_stem(target_file.stem, seed_val)
                q_fn = f"{q_stem}{target_file.suffix or '.jpg'}"
            else:
                q_stem = Path(fn).stem
                q_fn = fn
            print(
                f"  [{idx:02d}/{len(ctx.anchors)}] Generating {fn} [{reason}]{batch_lbl} "
                f"(Renderer #{rid}: {model_type}, seed={seed_val})...",
                flush=True,
            )
            print(
                f"[QUEUE_ACTIVE] {json.dumps({'idx': idx, 'total': len(ctx.anchors), 'filename': q_fn, 'reason': reason, 'model': model_type, 'frames': 1, 'resolution': res_val, 'take': b_idx + 1, 'take_count': batch_count, 'started_at': round(time.time(), 3)})}",
                flush=True,
            )

            if target_file.exists():
                target_file.unlink()

            if dummy_render:
                t0 = time.time()
                target_file.parent.mkdir(parents=True, exist_ok=True)
                write_engine_status(
                    ctx,
                    {
                        "state": "rendering",
                        "backend": anchor_backend,
                        "stage": "anchor",
                        "idx": idx,
                        "total": len(ctx.anchors),
                        "filename": q_fn,
                        "output_filename": q_stem,
                        "reason": reason,
                        "model": model_type,
                        "frames": 1,
                        "resolution": res_val,
                        "phase": "loading_model",
                        "raw_phase": "Loading anchor model...",
                        "status_text": "Loading anchor model...",
                        "step": None,
                        "total_steps": None,
                        "pct": 10.0,
                        "started_at": round(t0, 3),
                    },
                    merge=False,
                    backend=anchor_backend,
                    task_stem=q_stem,
                )
                for step in range(1, 3):
                    if check_cancel_requested(ctx, backend=anchor_backend, task_stem=q_stem):
                        clear_cancel_request(ctx, backend=anchor_backend, task_stem=q_stem, include_global=True)
                        write_engine_status(ctx, {"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user"}, backend=anchor_backend, task_stem=q_stem)
                        raise RuntimeError(f"Cancelled generating {fn}")
                    pct = round((step / 2) * 100, 1)
                    prog = {
                        "idx": idx,
                        "filename": q_fn,
                        "phase": "Sampling",
                        "raw_phase": "Sampling",
                        "status_text": f"Sampling {step}/2{batch_lbl}",
                        "step": step,
                        "total_steps": 2,
                        "pct": pct,
                        "window_index": None,
                        "window_count": None,
                    }
                    print(f"\n[QUEUE_PROGRESS] {json.dumps(prog)}", flush=True)
                    write_engine_status(ctx, prog, backend=anchor_backend, task_stem=q_stem)
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-f", "lavfi",
                        "-i", "color=c=0x1e293b:s=960x544:d=1",
                        "-vf", f"drawtext=text='[DUMMY ANCHOR {aid} s{seed_val}]':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=(h-text_h)/2",
                        "-frames:v", "1", "-q:v", "2", str(target_file),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                elapsed = round(time.time() - t0, 2)
                done_ts = round(time.time(), 3)
                print(f"      -> [DUMMY] Generated mock anchor {target_file.name}{batch_lbl} in {elapsed:.2f}s", flush=True)
            else:
                session = resolve_session_for_backend(lazy_session, anchor_backend)
                cfg = ctx.make_anchor_task(
                    session=session,
                    rspec=rspec,
                    output_filename=target_file.stem,
                    prompt=adapted_prompt,
                    seed=seed_val,
                    resolution=res_val,
                    sp=sp,
                    image_refs=resolved_refs,
                )
                anchor_exec_cfg = cfg if anchor_backend == "fal" else {k: v for k, v in cfg.items() if not str(k).startswith("_")}

                res, was_cancelled, t0 = execute_wgp_job(
                    ctx=ctx,
                    session=session,
                    task_cfg=anchor_exec_cfg,
                    idx=idx,
                    total=len(ctx.anchors),
                    filename=q_fn,
                    stage="anchor",
                    reason=reason,
                    model=model_type,
                    frames=1,
                    resolution=res_val,
                )
                if was_cancelled:
                    clear_cancel_request(ctx, backend=anchor_backend, task_stem=q_stem, include_global=True)
                    write_engine_status(ctx, {"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user"}, backend=anchor_backend, task_stem=q_stem)
                    raise RuntimeError(f"Cancelled generating {fn}")
                if not res.success or not res.generated_files:
                    err_list = [e.message for e in (res.errors or [])]
                    write_engine_status(ctx, {"state": "failed", "phase": "failed", "error": err_list}, backend=anchor_backend, task_stem=q_stem)
                    print(
                        f"[QUEUE_FAIL] {json.dumps({'idx': idx, 'filename': q_fn, 'error': err_list})}",
                        flush=True,
                    )
                    raise RuntimeError(f"Failed generating {fn}: {err_list}")

                gen_path = Path(res.generated_files[0])
                if gen_path.resolve() != target_file.resolve():
                    shutil.move(str(gen_path), str(target_file))
                elapsed = round(time.time() - t0, 1)
                done_ts = round(time.time(), 3)
                print(f"      -> Saved {target_file.name}{batch_lbl} in {elapsed:.1f}s", flush=True)

            archive_anchor_take_file(
                ctx.anchors_dir, ctx.project_dir, aid, target_file, sp,
                seed=seed_val, model_type=model_type, resolution=res_val, make_active=True,
                image_refs=to_proj_rel_paths(ctx.project_dir, resolved_refs),
            )
            anchor_hashes[fn] = cur_hash
            anchor_file_hashes[aid] = _file_sha256(target_file)
            state.setdefault("anchor_renders", {})[fn] = {"completed_at": done_ts, "elapsed": elapsed}
            ctx.save_state_hashes(state)
            print(
                f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': q_fn, 'elapsed': elapsed, 'completed_at': done_ts, 'take': b_idx + 1, 'take_count': batch_count})}",
                flush=True,
            )
            write_engine_status(
                ctx,
                {
                    "state": "completed",
                    "phase": "completed",
                    "raw_phase": "Completed",
                    "pct": 100.0,
                    "elapsed": elapsed,
                    "completed_at": done_ts,
                },
                backend=anchor_backend,
                task_stem=q_stem,
            )
            regenerated_anchors.add(aid)
            regenerated_anchors.add(fn)

    return regenerated_anchors


def import_anchors_from_dir(ctx: ProjectContext, src_dir_str: str, state: dict) -> set[str]:
    src_dir = resolve_path(ctx.project_dir, src_dir_str)
    if not src_dir.exists() or not src_dir.is_dir():
        print(f"[Import Error] Directory '{src_dir}' does not exist or is not a directory.", flush=True)
        return set()

    print("\n" + "=" * 72, flush=True)
    print(f"IMPORTING ANCHOR IMAGES from: {src_dir}", flush=True)
    print("=" * 72, flush=True)

    exts = {".jpg", ".jpeg", ".png", ".webp"}
    candidates = sorted([p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in exts])
    imported: set[str] = set()
    anchor_hashes = state.setdefault("anchors", {})

    # Map candidate by pattern matching
    matched_candidates: dict[str, Path] = {}
    used_candidates: set[Path] = set()

    for sp in ctx.anchors:
        fn = sp["filename"]
        aid = fn.split("_")[0].upper()  # e.g. "A01"
        num_str = aid.lstrip("A").lstrip("0") or "0"
        two_digit = f"{int(num_str):02d}"

        for c in candidates:
            if c in used_candidates:
                continue
            c_stem = c.stem.upper().replace(" ", "_").replace("-", "_")
            if (
                c_stem == aid
                or c_stem == f"A{num_str}"
                or c_stem == f"SHOT_{num_str}"
                or c_stem == f"SHOT_{two_digit}"
                or c_stem == f"SHOT{num_str}"
                or c_stem == f"SHOT{two_digit}"
                or c_stem == f"SCENE_{num_str}"
                or c_stem == f"SCENE_{two_digit}"
                or c_stem == num_str
                or c_stem == two_digit
                or c_stem.startswith(f"{aid}_")
                or f"_{aid}_" in c_stem
                or f"_{aid}$" in c_stem
            ):
                matched_candidates[fn] = c
                used_candidates.add(c)
                break

    # Fallback: if no explicit pattern matched, but folder has 14 files, match sequentially in order
    if len(matched_candidates) == 0 and len(candidates) == len(ctx.anchors):
        print(f"[Import Notice] No A01..A14 names found, but found exactly {len(candidates)} images.")
        print("  Auto-mapping candidate images to A01..A14 in sequential file order.", flush=True)
        for sp, c in zip(ctx.anchors, candidates):
            matched_candidates[sp["filename"]] = c

    for sp in ctx.anchors:
        fn = sp["filename"]
        if fn not in matched_candidates:
            continue
        matched = matched_candidates[fn]
        target_file = ctx.anchors_dir / fn

        ctx.anchors_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(ctx.ffmpeg),
            "-y",
            "-i",
            str(matched),
            "-vf",
            "scale=832:480:force_original_aspect_ratio=decrease,pad=832:480:(ow-iw)/2:(oh-ih)/2:color=black",
            "-q:v",
            "2",
            str(target_file),
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            shutil.copy2(str(matched), str(target_file))

        raw_expanded = expand_variables(sp.get("prompt", ""), ctx.variables)
        rid, rspec = ctx.resolve_anchor_renderer(sp)
        hash_payload = {
            "filename": fn,
            "seed": sp["seed"],
            "prompt": raw_expanded,
            "refs": ctx.anchor_refs,
            "imported_file_hash": _file_sha256(target_file),
        }
        cur_hash = _sha256(hash_payload)
        anchor_hashes[fn] = cur_hash
        imported.add(fn)
        print(f"  [Imported] {matched.name} -> {target_file.name}", flush=True)

    ctx.save_state_hashes(state)
    print(f"Imported {len(imported)} anchor image(s) into {ctx.anchors_dir}.\n", flush=True)
    return imported


def to_proj_rel_paths(proj_dir: Path | str, refs: list[str | Path] | None) -> list[str]:
    """Converts reference image paths to clean project-relative POSIX strings when inside proj_dir."""
    pdir = Path(proj_dir).resolve()
    out: list[str] = []
    for raw in refs or []:
        if not raw:
            continue
        p = Path(str(raw))
        try:
            rel = p.resolve().relative_to(pdir).as_posix() if p.is_absolute() else str(raw).replace("\\", "/").lstrip("/")
        except ValueError:
            rel = str(raw).replace("\\", "/")
        if rel and rel not in out:
            out.append(rel)
    return out


def collect_anchor_source_files(proj_dir: Path | str) -> list[str]:
    """Lists project-relative paths of standalone anchor source images and project reference files."""
    pdir = Path(proj_dir)
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    out: list[str] = []
    seen: set[str] = set()
    for base in (pdir / "work" / "anchor_sources", pdir / "references"):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            rel = p.relative_to(pdir).as_posix()
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def _decode_validated_anchor_image(data_url: str) -> bytes:
    """Decodes a base64/data-URL image payload and verifies size and JPG/PNG/WEBP magic bytes."""
    if not data_url:
        raise ValueError("data_url is required")
    raw_b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
    try:
        img_bytes = base64.b64decode(raw_b64, validate=False)
    except Exception as exc:
        raise ValueError(f"Invalid image encoding: {exc}") from exc
    if not img_bytes or len(img_bytes) > 25 * 1024 * 1024:
        raise ValueError("Anchor image is empty or exceeds 25 MB limit")
    is_valid_img = (
        img_bytes.startswith(b"\xff\xd8\xff")
        or img_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        or (img_bytes.startswith(b"RIFF") and b"WEBP" in img_bytes[:16])
    )
    if not is_valid_img:
        raise ValueError("Unsupported or corrupt image file (expected JPG, PNG, or WEBP)")
    return img_bytes


def handle_anchors_api(handler, srv, path: str, body: dict | None = None, require_project=None) -> None:
    """HTTP POST endpoints for uploading custom anchor/source images or deleting disk anchor files."""
    from http import HTTPStatus
    from .project_paths import resolve_project_dirs

    payload = body if isinstance(body, dict) else {}
    slug = str(payload.get("slug") or payload.get("project") or "").strip()
    if not slug:
        handler.send_error_json("slug is required", status=HTTPStatus.BAD_REQUEST)
        return
    proj_dir, config_file = require_project(handler, srv, slug, need_cfg=False) if require_project else (srv.PROJECTS_DIR / slug, None)
    if not proj_dir:
        return

    config_data = None
    if not config_file and Path(proj_dir).is_dir():
        cand_jsons = [j for j in sorted(Path(proj_dir).glob("*.json")) if not j.name.startswith("state_hashes")]
        if cand_jsons:
            config_file = cand_jsons[-1]
    if config_file and Path(config_file).is_file():
        try:
            config_data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        except Exception:
            config_data = None
    anchors_dir = resolve_project_dirs(proj_dir, config_file=config_file, config_data=config_data)["anchors"]
    action = path.rstrip("/").split("/")[-1].lower()

    if action == "upload":
        raw_input = str(payload.get("filename") or "").strip()
        if not raw_input or raw_input in (".", "..") or "\x00" in raw_input or "/" in raw_input or "\\" in raw_input:
            handler.send_error_json("Valid anchor filename is required", status=HTTPStatus.BAD_REQUEST)
            return
        raw_name = Path(raw_input).name.strip()
        if Path(raw_name).suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            raw_name = f"{Path(raw_name).stem}.jpg"
        try:
            img_bytes = _decode_validated_anchor_image(str(payload.get("data_url") or ""))
        except ValueError as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
            return

        anchors_dir.mkdir(parents=True, exist_ok=True)
        dest = (anchors_dir / raw_name).resolve()
        if dest.parent != anchors_dir.resolve():
            handler.send_error_json("Invalid target anchor path", status=HTTPStatus.BAD_REQUEST)
            return
        dest.write_bytes(img_bytes)
        try:
            from .anchor_takes import archive_anchor_take_file
            sp = next((a for a in (config_data or {}).get("anchors", []) if extract_anchor_id(a.get("filename", "")).upper() == extract_anchor_id(raw_name).upper()), None) or {"filename": raw_name}
            archive_anchor_take_file(anchors_dir, proj_dir, extract_anchor_id(raw_name), dest, sp, make_active=True)
        except Exception:
            pass
        try:
            srv.compute_fast_project_queue(slug, force_recheck=True)
        except Exception:
            pass
        if hasattr(srv, "broadcast_log"):
            srv.broadcast_log(f"[Studio UI] Uploaded anchor image -> {raw_name}")
            srv.broadcast_log("[ANCHORS_UPDATED]")
        handler.send_json({
            "success": True,
            "filename": raw_name,
            "aid": extract_anchor_id(raw_name),
            "mtime": dest.stat().st_mtime,
            "size_bytes": dest.stat().st_size,
        })
        return

    if action == "upload-source":
        try:
            img_bytes = _decode_validated_anchor_image(str(payload.get("data_url") or ""))
        except ValueError as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
            return
        raw_name = Path(str(payload.get("filename") or "source.jpg").replace("\\", "/")).name.strip()
        ext = Path(raw_name).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".png" if img_bytes.startswith(b"\x89PNG") else (".webp" if img_bytes.startswith(b"RIFF") else ".jpg")
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(raw_name).stem).strip("_")[:48] or "source"
        sha8 = hashlib.sha256(img_bytes).hexdigest()[:8]
        out_name = f"{stem}_{sha8}{ext}"
        src_dir = (Path(proj_dir) / "work" / "anchor_sources").resolve()
        src_dir.mkdir(parents=True, exist_ok=True)
        dest = (src_dir / out_name).resolve()
        if dest.parent != src_dir:
            handler.send_error_json("Invalid source image path", status=HTTPStatus.BAD_REQUEST)
            return
        if not dest.is_file():
            dest.write_bytes(img_bytes)
        rel_path = f"work/anchor_sources/{out_name}"
        if hasattr(srv, "broadcast_log"):
            srv.broadcast_log(f"[Studio UI] Uploaded anchor source image -> {rel_path}")
        handler.send_json({
            "success": True,
            "filename": out_name,
            "rel_path": rel_path,
            "anchor_source_files": collect_anchor_source_files(proj_dir),
            "size_bytes": dest.stat().st_size,
        })
        return

    if action == "delete-source":
        raw_rel = str(payload.get("rel_path") or payload.get("filename") or "").replace("\\", "/").strip().lstrip("/")
        if not raw_rel or "\x00" in raw_rel:
            handler.send_error_json("Valid source rel_path is required", status=HTTPStatus.BAD_REQUEST)
            return
        src_dir = (Path(proj_dir) / "work" / "anchor_sources").resolve()
        cand = (src_dir / Path(raw_rel).name).resolve() if "/" not in raw_rel else (Path(proj_dir) / raw_rel).resolve()
        if cand.parent != src_dir:
            handler.send_error_json("Only uploaded files under work/anchor_sources/ can be deleted here", status=HTTPStatus.BAD_REQUEST)
            return
        deleted = cand.is_file()
        cand.unlink(missing_ok=True)
        norm_rel = f"work/anchor_sources/{cand.name}"
        if config_file and isinstance(config_data, dict):
            changed = False
            for a in config_data.get("anchors", []) or []:
                if isinstance(a, dict) and isinstance(a.get("image_refs"), list) and norm_rel in a["image_refs"]:
                    rem = [r for r in a["image_refs"] if r != norm_rel]
                    if rem:
                        a["image_refs"] = rem
                    else:
                        a.pop("image_refs", None)
                    changed = True
            if changed:
                try:
                    srv.backup_project_json(Path(config_file), throttle_seconds=0.0)
                except Exception:
                    pass
                Path(config_file).write_text(json.dumps(config_data, indent=2), encoding="utf-8")
        try:
            srv.compute_fast_project_queue(slug, force_recheck=True)
        except Exception:
            pass
        if hasattr(srv, "broadcast_log") and deleted:
            srv.broadcast_log(f"[Studio UI] Deleted anchor source image -> {norm_rel}")
        handler.send_json({
            "success": True,
            "rel_path": norm_rel,
            "deleted": deleted,
            "anchor_source_files": collect_anchor_source_files(proj_dir),
        })
        return

    from .anchor_takes import handle_anchor_takes_http
    if handle_anchor_takes_http(handler, srv, action, proj_dir, Path(config_file) if config_file else None, config_data, anchors_dir, payload):
        return

    if action == "delete":
        raw_input = str(payload.get("filename") or "").strip()
        if not raw_input or raw_input in (".", "..") or "\x00" in raw_input or "/" in raw_input or "\\" in raw_input:
            handler.send_error_json("Valid filename is required", status=HTTPStatus.BAD_REQUEST)
            return
        raw_name = Path(raw_input).name.strip()
        target = (anchors_dir / raw_name).resolve()
        if target.parent != anchors_dir.resolve():
            handler.send_error_json("Invalid anchor path", status=HTTPStatus.BAD_REQUEST)
            return
        deleted = target.is_file()
        target.unlink(missing_ok=True)
        try:
            srv.compute_fast_project_queue(slug, force_recheck=True)
        except Exception:
            pass
        handler.send_json({"success": True, "filename": raw_name, "deleted": deleted})
        return

    handler.send_error_json(f"Unknown anchor action: {action}", status=HTTPStatus.NOT_FOUND)


def _manifest_used_seeds(anchors_dir: Path | None, seed_cache: dict | None = None, manifest: dict | None = None) -> set[tuple[str, int]]:
    if seed_cache is not None and "used" in seed_cache:
        return seed_cache["used"]
    if manifest is None and anchors_dir:
        from .anchor_takes import load_anchor_takes_manifest
        manifest = load_anchor_takes_manifest(anchors_dir)
    used: set[tuple[str, int]] = set()
    for ak, slot in ((manifest or {}).get("anchors") or {}).items():
        for tk in (slot or {}).get("takes") or []:
            if isinstance(tk, dict) and tk.get("seed") is not None:
                try:
                    used.add((str(ak).upper(), int(tk["seed"])))
                except (TypeError, ValueError):
                    pass
    if seed_cache is not None:
        seed_cache["used"] = used
    return used


def reconcile_existing_anchor_queue_item(
    item: dict, is_engine_active: bool, active_eng_map: dict, force_recheck: bool, done_snaps: list[dict],
    anchors_dir: Path | None = None, seed_cache: dict | None = None,
) -> bool:
    """Preserves and reconciles an existing stage=='anchor' item in v3_queue.json across queue re-evaluations."""
    out_name = str(item.get("output_filename") or Path(str(item.get("filename") or "")).stem).strip()
    fn = str(item.get("filename") or f"{out_name}.jpg").strip()
    if not out_name:
        return False
    aid_u = str(item.get("anchor_id") or item.get("scene_id") or "").strip().upper()
    reason_u = str(item.get("reason") or "").upper()
    try:
        seed_i = int(item["seed"]) if item.get("seed") is not None else None
    except (TypeError, ValueError):
        seed_i = None
    used_seeds = _manifest_used_seeds(anchors_dir, seed_cache) if (anchors_dir and aid_u and not aid_u.startswith("CHAR:") and seed_i is not None) else set()
    is_dup_done = item.get("status") == "queued" and "FORCED" not in reason_u and not reason_u.startswith("TARGETED") and (aid_u, seed_i) in used_seeds
    if item.get("status") == "completed" or (reason_u == "COMPLETED" and item.get("completed_at")) or is_dup_done:
        item.update({"status": "completed", "needs_render": False})
        if not reason_u or is_dup_done:
            item["reason"] = "COMPLETED"
        done_snaps.append(dict(item))
        return True
    active_st = active_eng_map.get(out_name) or active_eng_map.get(fn)
    if is_engine_active and (active_st is not None or (not active_eng_map and item.get("status") == "rendering")):
        item["status"] = "rendering"
        item["needs_render"] = True
        return True
    if item.get("status") == "rendering":
        item["status"] = "queued" if item.get("needs_render", True) else "completed"
    if item.get("status") == "completed":
        done_snaps.append(dict(item))
        item["needs_render"] = False
        return True
    if item.get("status") == "skipped" and force_recheck:
        return False
    return True
