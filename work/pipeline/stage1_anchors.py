import json
import shutil
import subprocess
import time
from pathlib import Path

from .context import ProjectContext
from .media_utils import _file_sha256, _sha256, resolve_path
from .prompts import adapt_ref_tags, expand_variables, extract_anchor_id
from .wgp_bridge import (
    LazyWgpSession,
    check_cancel_requested,
    clear_cancel_request,
    execute_wgp_job,
    write_engine_status,
)


def match_anchor_id(token: str, filename: str) -> bool:
    t = token.strip().upper()
    aid_t = extract_anchor_id(t).upper()
    aid_fn = extract_anchor_id(filename).upper()
    return (
        t in ("ALL", "ALL-ANCHORS")
        or aid_t == aid_fn
        or t in (filename.upper(), Path(filename).stem.upper())
    )


def run_stage1_anchors(
    ctx: ProjectContext,
    lazy_session: LazyWgpSession,
    state: dict,
    force_anchors: list[str],
    sync_only: bool = False,
    dummy_render: bool = False,
) -> set[str]:
    def_rid = str(ctx.default_anchor_renderer)
    def_model = ctx.anchor_renderers.get(def_rid, {}).get("model_type", "qwen_image_21_turbo_7B")
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

        raw_expanded = expand_variables(sp.get("prompt", ""), ctx.variables)
        adapted_prompt = adapt_ref_tags(raw_expanded, rspec.get("ref_tag_style", "<imageN>"))

        hash_payload = {
            "filename": fn,
            "seed": sp["seed"],
            "prompt": raw_expanded,
            "refs": ctx.anchor_refs,
        }
        if model_type != "qwen_image_21_turbo_7B":
            hash_payload["model_type"] = model_type
            hash_payload["steps"] = rspec.get("num_inference_steps")
            hash_payload["cfg"] = rspec.get("guidance_scale", rspec.get("embedded_guidance_scale"))
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
            else:
                print(f"  [{idx:02d}/{len(ctx.anchors)}] {aid} ({fn}) [OK / Up-to-date].", flush=True)
                continue

        reason = "FORCED" if forced else "MISSING FILE"
        seed_val = sp.get("seed", 42)
        res_val = sp.get("resolution", rspec.get("resolution", shared_cfg.get("resolution", "1664x960")))
        print(
            f"  [{idx:02d}/{len(ctx.anchors)}] Generating {fn} [{reason}] "
            f"(Renderer #{rid}: {model_type}, seed={seed_val})...",
            flush=True,
        )
        print(
            f"[QUEUE_ACTIVE] {json.dumps({'idx': idx, 'total': len(ctx.anchors), 'filename': fn, 'reason': reason, 'model': model_type, 'frames': 1, 'resolution': res_val})}",
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
                    "stage": "anchor",
                    "idx": idx,
                    "total": len(ctx.anchors),
                    "filename": fn,
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
            )
            for step in range(1, 3):
                if check_cancel_requested(ctx):
                    clear_cancel_request(ctx)
                    write_engine_status(ctx, {"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user"})
                    raise RuntimeError(f"Cancelled generating {fn}")
                pct = round((step / 2) * 100, 1)
                prog = {
                    "idx": idx,
                    "filename": fn,
                    "phase": "Sampling",
                    "raw_phase": "Sampling",
                    "status_text": f"Sampling {step}/2",
                    "step": step,
                    "total_steps": 2,
                    "pct": pct,
                    "window_index": None,
                    "window_count": None,
                }
                print(f"\n[QUEUE_PROGRESS] {json.dumps(prog)}", flush=True)
                write_engine_status(ctx, prog)
            subprocess.run(
                [
                    str(ctx.ffmpeg), "-y", "-f", "lavfi",
                    "-i", "color=c=0x1e293b:s=960x544:d=1",
                    "-vf", f"drawtext=text='[DUMMY ANCHOR {aid}]':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=(h-text_h)/2",
                    "-frames:v", "1", "-q:v", "2", str(target_file),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            elapsed = round(time.time() - t0, 2)
            print(f"      -> [DUMMY] Generated mock anchor {target_file.name} in {elapsed:.2f}s", flush=True)
            print(
                f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': fn, 'elapsed': elapsed})}",
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
                },
            )
            anchor_hashes[fn] = cur_hash
            anchor_file_hashes[aid] = _file_sha256(target_file)
            ctx.save_state_hashes(state)
            regenerated_anchors.add(aid)
            regenerated_anchors.add(fn)
            continue

        session = lazy_session.get()
        cfg = ctx.make_anchor_task(
            session=session,
            rspec=rspec,
            output_filename=target_file.stem,
            prompt=adapted_prompt,
            seed=seed_val,
            resolution=res_val,
            sp=sp,
        )

        res, was_cancelled, t0 = execute_wgp_job(
            ctx=ctx,
            session=session,
            task_cfg=cfg,
            idx=idx,
            total=len(ctx.anchors),
            filename=fn,
            stage="anchor",
            reason=reason,
            model=model_type,
            frames=1,
            resolution=res_val,
        )
        if was_cancelled:
            clear_cancel_request(ctx)
            write_engine_status(ctx, {"state": "cancelled", "phase": "cancelled", "raw_phase": "Stopped by user"})
            raise RuntimeError(f"Cancelled generating {fn}")
        if not res.success or not res.generated_files:
            err_list = [e.message for e in (res.errors or [])]
            write_engine_status(ctx, {"state": "failed", "phase": "failed", "error": err_list})
            print(
                f"[QUEUE_FAIL] {json.dumps({'idx': idx, 'filename': fn, 'error': err_list})}",
                flush=True,
            )
            raise RuntimeError(f"Failed generating {fn}: {err_list}")

        gen_path = Path(res.generated_files[0])
        if gen_path.resolve() != target_file.resolve():
            shutil.move(str(gen_path), str(target_file))
        elapsed = round(time.time() - t0, 1)
        print(f"      -> Saved {target_file.name} in {elapsed:.1f}s", flush=True)
        print(
            f"[QUEUE_DONE] {json.dumps({'idx': idx, 'filename': fn, 'elapsed': elapsed})}",
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
            },
        )

        anchor_hashes[fn] = cur_hash
        anchor_file_hashes[aid] = _file_sha256(target_file)
        ctx.save_state_hashes(state)
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
