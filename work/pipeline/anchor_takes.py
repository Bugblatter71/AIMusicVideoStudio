"""Anchor Takes Vault & Character Look Reference Image Generation.

Supports:
1. Multi-take Stage 1 anchor generation (1..99 images per run) archived under work/anchors/takes/<AID>/
2. Choosing which anchor take is active, deleting takes, and promoting any take into a separate anchor
3. Generating multi-take reference images for Character Studio looks (--gen-char-ref <entity>:<look>[:x<count>])
4. Managing character look takes (toggle/use in look, set avatar, delete, or turn into a separate Stage 1 anchor)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from http import HTTPStatus
from pathlib import Path

from .context import ProjectContext
from .fal_catalog import resolve_fal_route, resolve_task_backend
from .media_utils import _file_sha256, _sha256
from .project_paths import resolve_project_dirs
from .prompts import extract_anchor_id
from .wgp_bridge import (
    LazyWgpSession,
    check_cancel_requested,
    clear_cancel_request,
    execute_wgp_job,
    resolve_session_for_backend,
    write_engine_status,
)


def parse_anchor_batch_count(force_anchors: list[str], fn: str, sp: dict) -> int:
    """Extracts requested batch take count (1..99) from force_anchors tokens (e.g. 'A01:x3' or 'A01*3') or sp['gen_count']."""
    aid_fn = extract_anchor_id(fn).upper()
    fn_up = fn.upper()
    stem_up = Path(fn).stem.upper()
    for raw_tok in force_anchors or []:
        parts = re.split(r"[:*]", raw_tok.strip(), maxsplit=1)
        base = parts[0].strip().upper()
        aid_t = extract_anchor_id(base).upper()
        matched = base in ("ALL", "ALL-ANCHORS") or aid_t == aid_fn or base in (fn_up, stem_up)
        if matched and len(parts) == 2:
            m = re.search(r"(\d+)", parts[1])
            if m:
                return max(1, min(99, int(m.group(1))))
    try:
        cfg_cnt = int(sp.get("gen_count") or 1)
        return max(1, min(99, cfg_cnt))
    except (TypeError, ValueError):
        return 1


def parse_anchor_explicit_seed(force_anchors: list[str], fn: str) -> int | None:
    """Extracts an explicit single-take seed if force_anchors has '<AID>:s<seed>' or '<AID>@s<seed>'."""
    aid_fn = extract_anchor_id(fn).upper()
    fn_up = fn.upper()
    stem_up = Path(fn).stem.upper()
    for raw_tok in force_anchors or []:
        parts = re.split(r"[:*@]", raw_tok.strip(), maxsplit=1)
        base = parts[0].strip().upper()
        aid_t = extract_anchor_id(base).upper()
        matched = aid_t == aid_fn or base in (fn_up, stem_up)
        if matched and len(parts) == 2:
            m = re.match(r"^s(\d+)$", parts[1].strip(), re.IGNORECASE)
            if m:
                return int(m.group(1))
    return None


def clean_anchor_queue_stem(stem: str, seed_val: int) -> str:
    clean_base = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(stem or "").strip())[:64]
    return f"{clean_base}_s{int(seed_val)}"


def select_unused_take_seeds(existing_takes: list[dict] | None, base_seed: int, batch_count: int) -> list[int]:
    """Selects `batch_count` unique seeds starting at `base_seed`, skipping any seed already used in `existing_takes`."""
    used_seeds: set[int] = set()
    for t in existing_takes or []:
        if isinstance(t, dict) and t.get("seed") is not None:
            try:
                used_seeds.add(int(t["seed"]))
            except (TypeError, ValueError):
                pass
    selected: list[int] = []
    cand = int(base_seed)
    while len(selected) < max(1, int(batch_count)):
        if cand not in used_seeds:
            selected.append(cand)
            used_seeds.add(cand)
        cand += 1
    return selected


def _manifest_path(anchors_dir: Path) -> Path:
    return anchors_dir / "anchor_takes.json"


def load_anchor_takes_manifest(anchors_dir: Path) -> dict:
    mp = _manifest_path(anchors_dir)
    if mp.is_file():
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("anchors", {})
                return data
        except Exception:
            pass
    return {"anchors": {}}


def save_anchor_takes_manifest(anchors_dir: Path, data: dict) -> None:
    anchors_dir.mkdir(parents=True, exist_ok=True)
    mp = _manifest_path(anchors_dir)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(mp)


def _backup_and_save_project_json(proj_dir: Path, cfg_file: Path, data: dict) -> None:
    backup_dir = proj_dir.parent.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(cfg_file, backup_dir / f"{cfg_file.stem}_{ts}.json")
    tmp = cfg_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(cfg_file)


def _next_free_anchor_id(anchors_dir: Path, config_data: dict) -> str:
    used: set[int] = set()
    for a in (config_data or {}).get("anchors", []):
        m = re.match(r"^A(\d+)", extract_anchor_id(a.get("filename") or a.get("id") or ""), re.IGNORECASE)
        if m:
            used.add(int(m.group(1)))
    if anchors_dir.is_dir():
        for p in anchors_dir.iterdir():
            if p.is_file():
                m = re.match(r"^A(\d+)", extract_anchor_id(p.name), re.IGNORECASE)
                if m:
                    used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"A{n:02d}"


def _probe_image_dims(path: Path) -> str:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return f"{im.width}x{im.height}"
    except Exception:
        return ""


def archive_anchor_take_file(
    anchors_dir: Path,
    proj_dir: Path,
    aid: str,
    src_file: Path,
    sp: dict,
    *,
    seed: int | None = None,
    model_type: str = "",
    resolution: str = "",
    make_active: bool = True,
    image_refs: list[str] | None = None,
) -> dict | None:
    """Copies src_file into work/anchors/takes/<AID>/ and records it in anchor_takes.json."""
    if not src_file.is_file() or src_file.stat().st_size == 0:
        return None
    aid_up = (aid or extract_anchor_id(src_file.name) or "A01").upper()
    manifest = load_anchor_takes_manifest(anchors_dir)
    slot = manifest["anchors"].setdefault(aid_up, {"active_take_id": "", "takes": []})
    takes: list[dict] = slot.setdefault("takes", [])

    file_sha = _file_sha256(src_file)
    for existing in takes:
        ex_path = proj_dir / existing.get("rel_path", "")
        if existing.get("file_sha") == file_sha and ex_path.is_file():
            changed = False
            if image_refs and not existing.get("image_refs"):
                existing["image_refs"] = list(image_refs)
                changed = True
            if make_active and slot.get("active_take_id") != existing["take_id"]:
                slot["active_take_id"] = existing["take_id"]
                changed = True
            if changed:
                save_anchor_takes_manifest(anchors_dir, manifest)
            return existing

    take_num = len(takes) + 1
    existing_ids = {t.get("take_id") for t in takes}
    eff_seed = int(seed if seed is not None else (sp.get("seed") or 1001))
    ext = src_file.suffix.lower() if src_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    while True:
        take_id = f"{aid_up}_t{take_num:03d}_s{eff_seed}"
        if take_id not in existing_ids:
            break
        take_num += 1

    take_dir = anchors_dir / "takes" / aid_up
    take_dir.mkdir(parents=True, exist_ok=True)
    take_file = take_dir / f"{take_id}{ext}"
    shutil.copy2(src_file, take_file)
    try:
        rel_path = take_file.resolve().relative_to(proj_dir.resolve()).as_posix()
    except ValueError:
        rel_path = f"work/anchors/takes/{aid_up}/{take_file.name}"

    actual_dims = _probe_image_dims(take_file)
    take_entry = {
        "take_id": take_id,
        "aid": aid_up,
        "filename": take_file.name,
        "rel_path": rel_path,
        "seed": eff_seed,
        "prompt": str(sp.get("prompt") or ""),
        "negative_prompt": str(sp.get("negative_prompt") or ""),
        "renderer": sp.get("renderer"),
        "backend": sp.get("backend"),
        "model_type": model_type,
        "resolution": actual_dims or resolution or str(sp.get("resolution") or ""),
        "file_sha": file_sha,
        "created_at": round(time.time(), 3),
        **({"image_refs": list(image_refs)} if image_refs else {}),
    }
    takes.append(take_entry)
    if make_active or not slot.get("active_take_id"):
        slot["active_take_id"] = take_id
    save_anchor_takes_manifest(anchors_dir, manifest)
    return take_entry


def collect_project_anchor_takes(proj_dir: Path, anchors_dir: Path, config_data: dict | None = None) -> dict:
    """Returns per-AID takes dictionary, auto-archiving any existing disk anchor images not yet in the vault."""
    manifest = load_anchor_takes_manifest(anchors_dir)
    cfg_anchors = (config_data or {}).get("anchors", [])
    by_aid: dict[str, dict] = {}
    for sp in cfg_anchors:
        fn = sp.get("filename") or ""
        aid = extract_anchor_id(fn).upper()
        if aid:
            by_aid[aid] = sp

    if anchors_dir.is_dir():
        for img in sorted(anchors_dir.iterdir()):
            if not img.is_file() or img.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            aid = extract_anchor_id(img.name).upper()
            if not aid:
                continue
            slot = manifest["anchors"].get(aid)
            if not slot or not slot.get("takes"):
                sp = by_aid.get(aid) or {"filename": img.name, "seed": 1001, "prompt": ""}
                archive_anchor_take_file(anchors_dir, proj_dir, aid, img, sp, make_active=True)

    manifest = load_anchor_takes_manifest(anchors_dir)
    cleaned: dict[str, dict] = {}
    for aid, slot in manifest.get("anchors", {}).items():
        valid_takes = [
            t for t in slot.get("takes", [])
            if t.get("rel_path") and (proj_dir / t["rel_path"]).is_file()
        ]
        act_id = slot.get("active_take_id") or (valid_takes[-1]["take_id"] if valid_takes else "")
        if valid_takes and not any(t.get("take_id") == act_id for t in valid_takes):
            act_id = valid_takes[-1]["take_id"]
        cleaned[aid.upper()] = {"active_take_id": act_id, "takes": valid_takes}
    return cleaned


def run_character_ref_generation(
    ctx: ProjectContext,
    lazy_session: LazyWgpSession,
    spec_token: str,
    dummy_render: bool = False,
) -> list[str]:
    """Generates 1..N reference image takes for a Character Studio look (--gen-char-ref <entity>:<look>[:x<count>])."""
    from .stage1_anchors import compile_anchor_prompt_and_refs

    parts = [p.strip() for p in spec_token.split(":") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"Expected --gen-char-ref <entity_id>:<look_id>[:x<count>|:s<seed>], got '{spec_token}'")
    entity_id, look_id = parts[0], parts[1]
    is_isolate = any(p.lower() == "isolate" for p in parts[2:])
    batch_count = 1
    explicit_seed: int | None = None
    for extra in parts[2:]:
        if extra.lower() == "isolate": continue
        m_seed = re.match(r"^s(\d+)$", extra, re.IGNORECASE)
        if m_seed: explicit_seed = int(m_seed.group(1))
        elif re.search(r"(\d+)", extra): batch_count = max(1, min(99, int(re.search(r"(\d+)", extra).group(1))))

    raw_data = json.loads(ctx.json_path.read_text(encoding="utf-8"))
    chars = raw_data.setdefault("characters", {})
    ent = chars.get(entity_id)
    if not isinstance(ent, dict): raise ValueError(f"Character entity '{entity_id}' not found in {ctx.json_path.name}")
    look = (ent.setdefault("looks", {})).get(look_id)
    if not isinstance(look, dict): raise ValueError(f"Look '{look_id}' not found on character '{entity_id}'")

    if is_isolate:
        from .char_masking import build_isolate_sp_like
        sp_like, iso = build_isolate_sp_like(ctx, ent, look, entity_id, look_id, explicit_seed)
        raw_prompt, base_seed, batch_count = sp_like["prompt"], sp_like["seed"], 1
    else:
        iso = {}
        if len(parts) < 3 and look.get("gen_count"):
            try: batch_count = max(1, min(99, int(look["gen_count"])))
            except (TypeError, ValueError): batch_count = 1
        traits = str(ent.get("physical_traits") or ent.get("name") or entity_id).strip()
        look_desc = str(look.get("description") or look.get("name") or look_id).strip()
        raw_prompt = str(look.get("gen_prompt") or "").strip() or f"Cinematic 16:9 character reference portrait of {traits}, {look_desc}, studio lighting, sharp focus"
        base_seed = int(look.get("gen_seed") or 1001)
        sp_like = {
            "filename": f"CHAR_{entity_id}_{look_id}.jpg", "prompt": raw_prompt, "negative_prompt": look.get("gen_negative_prompt"),
            "renderer": look.get("gen_renderer"), "backend": look.get("gen_backend"), "resolution": look.get("gen_resolution"),
            "seed": base_seed, "image_refs": look.get("gen_image_refs") if isinstance(look.get("gen_image_refs"), list) else [],
        }
    rid, rspec = ctx.resolve_anchor_renderer(sp_like)
    model_type = rspec["model_type"]
    shared_cfg = ctx.anchor_shared_cfg
    from .prompts import align_anchor_resolution
    res_val = align_anchor_resolution(
        str(sp_like.get("resolution") or "").strip()
        or str(shared_cfg.get("resolution") or "").strip()
        or str(rspec.get("resolution") or "").strip()
        or "1920x1080",
        model_type,
    )
    _, adapted_prompt, resolved_refs = compile_anchor_prompt_and_refs(ctx, sp_like, rspec)
    anchor_backend = resolve_task_backend(ctx, sc=sp_like, rspec=rspec, stage="anchor")

    clean_ent = re.sub(r"[^a-zA-Z0-9_-]", "_", entity_id).strip("_") or "character"
    clean_look = re.sub(r"[^a-zA-Z0-9_-]", "_", look_id).strip("_") or "look"
    target_dir = ctx.project_dir / "references" / clean_ent
    target_dir.mkdir(parents=True, exist_ok=True)

    gen_takes: list[dict] = look.setdefault("generated_takes", [])
    existing_ids = {t.get("take_id") for t in gen_takes if isinstance(t, dict)}
    created_rels: list[str] = []
    batch_seeds = [explicit_seed] if explicit_seed is not None else select_unused_take_seeds(gen_takes, base_seed, batch_count)

    for b_idx, cur_seed in enumerate(batch_seeds):
        t_num = len(gen_takes) + 1
        while f"{clean_ent}_{clean_look}_t{t_num:03d}_s{cur_seed}" in existing_ids:
            t_num += 1
        take_id = f"{clean_ent}_{clean_look}_t{t_num:03d}_s{cur_seed}"
        existing_ids.add(take_id)
        out_file = target_dir / f"{take_id}.jpg"
        fn = out_file.name
        q_fn = f"CHAR_{clean_ent}_{clean_look}_s{cur_seed}.jpg" if explicit_seed is not None else fn

        print(
            f"  [CharRef {b_idx + 1}/{batch_count}] Generating {fn} for @{clean_look} "
            f"(Renderer #{rid}: {model_type}, seed={cur_seed}, res={res_val})...",
            flush=True,
        )
        print(
            f"[QUEUE_ACTIVE] {json.dumps({'idx': b_idx + 1, 'total': batch_count, 'filename': q_fn, 'reason': 'CHAR REF', 'model': model_type, 'frames': 1, 'resolution': res_val, 'started_at': round(time.time(), 3)})}",
            flush=True,
        )

        if dummy_render:
            t0 = time.time()
            subprocess.run(
                [
                    str(ctx.ffmpeg), "-y", "-f", "lavfi",
                    "-i", "color=c=0x1e293b:s=960x544:d=1",
                    "-vf", f"drawtext=text='[CHAR REF {clean_ent}/{clean_look} s{cur_seed}]':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2",
                    "-frames:v", "1", "-q:v", "2", str(out_file),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            elapsed = round(time.time() - t0, 2)
        else:
            session = resolve_session_for_backend(lazy_session, anchor_backend)
            cfg = ctx.make_anchor_task(
                session=session,
                rspec=rspec,
                output_filename=out_file.stem,
                prompt=adapted_prompt,
                seed=cur_seed,
                resolution=res_val,
                sp=sp_like,
                image_refs=resolved_refs,
            )
            exec_cfg = cfg if anchor_backend == "fal" else {k: v for k, v in cfg.items() if not str(k).startswith("_")}
            res, was_cancelled, t0 = execute_wgp_job(
                ctx=ctx, session=session, task_cfg=exec_cfg, idx=b_idx + 1, total=batch_count,
                filename=q_fn, stage="anchor", reason="CHAR REF", model=model_type, frames=1, resolution=res_val,
            )
            if was_cancelled:
                clear_cancel_request(ctx, backend=anchor_backend, task_stem=Path(q_fn).stem, include_global=True)
                raise RuntimeError(f"Cancelled generating character ref {fn}")
            if not res.success or not res.generated_files:
                err_list = [e.message for e in (res.errors or [])]
                raise RuntimeError(f"Failed generating character ref {fn}: {err_list}")
            gen_path = Path(res.generated_files[0])
            if gen_path.resolve() != out_file.resolve():
                shutil.move(str(gen_path), str(out_file))
            elapsed = round(time.time() - t0, 1)

        rel_path = f"references/{clean_ent}/{out_file.name}"
        take_meta = {
            "take_id": take_id,
            "rel_path": rel_path,
            "filename": out_file.name,
            "seed": cur_seed,
            "prompt": raw_prompt,
            "negative_prompt": str(sp_like.get("negative_prompt") or ""),
            "renderer": sp_like.get("renderer"),
            "backend": sp_like.get("backend"),
            "model_type": model_type,
            "resolution": res_val,
            **({"isolate": True, "source_rel": iso.get("source_rel", "")} if is_isolate else {}),
            "created_at": round(time.time(), 3),
        }
        gen_takes.append(take_meta)
        img_refs = look.setdefault("image_refs", [])
        if rel_path not in img_refs:
            img_refs.append(rel_path)
        if not ent.get("avatar"):
            ent["avatar"] = rel_path
        if not is_isolate:
            look["gen_seed"] = cur_seed
            look["gen_prompt"] = raw_prompt
        created_rels.append(rel_path)
        print(
            f"[QUEUE_DONE] {json.dumps({'idx': b_idx + 1, 'filename': q_fn, 'elapsed': elapsed, 'completed_at': round(time.time(), 3)})}",
            flush=True,
        )

    _backup_and_save_project_json(ctx.project_dir, ctx.json_path, raw_data)
    return created_rels


def handle_anchor_takes_http(
    handler, srv, action: str, proj_dir: Path, config_file: Path | None, config_data: dict | None, anchors_dir: Path, payload: dict
) -> bool:
    """Handles POST /api/anchors/takes, take-use, take-delete, take-to-anchor, and char-take-action."""
    slug = str(payload.get("slug") or payload.get("project") or "").strip()
    if action == "takes":
        takes_map = collect_project_anchor_takes(proj_dir, anchors_dir, config_data)
        handler.send_json({"success": True, "anchor_takes": takes_map})
        return True

    if action == "take-use":
        aid = str(payload.get("aid") or "").strip().upper()
        take_id = str(payload.get("take_id") or "").strip()
        if not aid or not take_id or not config_file or not isinstance(config_data, dict):
            handler.send_error_json("aid, take_id, and valid project config are required", status=HTTPStatus.BAD_REQUEST); return True
        manifest = load_anchor_takes_manifest(anchors_dir)
        take = next((t for t in (manifest.get("anchors", {}).get(aid) or {}).get("takes", []) if t.get("take_id") == take_id), None)
        if not take:
            handler.send_error_json(f"Take '{take_id}' not found for {aid}", status=HTTPStatus.NOT_FOUND); return True
        src_file = (proj_dir / take.get("rel_path", "")).resolve()
        if not src_file.is_file():
            handler.send_error_json("Take image file is missing on disk", status=HTTPStatus.NOT_FOUND); return True
        slot = manifest["anchors"][aid]
        sp = next((a for a in config_data.get("anchors", []) if extract_anchor_id(a.get("filename", "")).upper() == aid), None)
        target_fn = (sp.get("filename") if sp else None) or f"{aid}_Scene{src_file.suffix}"
        dest_file = anchors_dir / Path(target_fn).name
        anchors_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        slot["active_take_id"] = take_id
        save_anchor_takes_manifest(anchors_dir, manifest)
        if sp is not None:
            if take.get("seed"):
                sp["seed"] = int(take["seed"])
            if take.get("prompt"):
                sp["prompt"] = take["prompt"]
            _backup_and_save_project_json(proj_dir, config_file, config_data)
        try:
            srv.compute_fast_project_queue(slug, force_recheck=True)
        except Exception:
            pass
        if hasattr(srv, "broadcast_log"):
            srv.broadcast_log("[ANCHORS_UPDATED]")
        handler.send_json({
            "success": True,
            "aid": aid,
            "active_take_id": take_id,
            "anchor_takes": collect_project_anchor_takes(proj_dir, anchors_dir, config_data),
        })
        return True

    if action == "take-delete":
        aid = str(payload.get("aid") or "").strip().upper()
        raw_ids = payload.get("take_ids")
        req_ids = [str(x).strip() for x in raw_ids if str(x).strip()] if isinstance(raw_ids, list) else (
            [str(payload.get("take_id") or "").strip()] if str(payload.get("take_id") or "").strip() else []
        )
        manifest = load_anchor_takes_manifest(anchors_dir)
        slot = manifest.get("anchors", {}).get(aid)
        if not slot or not req_ids:
            handler.send_error_json(f"No takes found for {aid}", status=HTTPStatus.NOT_FOUND)
            return True
        takes, del_set = slot.get("takes", []), set(req_ids)
        targets = [t for t in takes if t.get("take_id") in del_set]
        if not targets:
            handler.send_error_json(f"Take '{req_ids[0]}' not found", status=HTTPStatus.NOT_FOUND)
            return True
        for target in targets:
            t_path = (proj_dir / target.get("rel_path", "")).resolve()
            if t_path.is_file() and anchors_dir.resolve() in t_path.parents:
                t_path.unlink(missing_ok=True)
        slot["takes"] = [t for t in takes if t.get("take_id") not in del_set]
        if slot.get("active_take_id") in del_set:
            slot["active_take_id"] = slot["takes"][-1]["take_id"] if slot["takes"] else ""
            if slot["active_take_id"] and isinstance(config_data, dict):
                nf = (proj_dir / slot["takes"][-1].get("rel_path", "")).resolve()
                sp = next((a for a in config_data.get("anchors", []) if extract_anchor_id(a.get("filename", "")).upper() == aid), None)
                if nf.is_file() and sp and sp.get("filename"):
                    shutil.copy2(nf, anchors_dir / Path(sp["filename"]).name)
        save_anchor_takes_manifest(anchors_dir, manifest)
        handler.send_json({
            "success": True,
            "aid": aid,
            "deleted_count": len(targets),
            "anchor_takes": collect_project_anchor_takes(proj_dir, anchors_dir, config_data),
        })
        return True

    if action == "take-to-anchor":
        aid = str(payload.get("aid") or "").strip().upper()
        take_id = str(payload.get("take_id") or "").strip()
        if not config_file or not isinstance(config_data, dict):
            handler.send_error_json("Valid project config required", status=HTTPStatus.BAD_REQUEST); return True
        manifest = load_anchor_takes_manifest(anchors_dir)
        take = next((t for t in (manifest.get("anchors", {}).get(aid) or {}).get("takes", []) if t.get("take_id") == take_id), None)
        if not take:
            handler.send_error_json(f"Take '{take_id}' not found", status=HTTPStatus.NOT_FOUND); return True
        src_file = (proj_dir / take.get("rel_path", "")).resolve()
        if not src_file.is_file():
            handler.send_error_json("Take image file missing on disk", status=HTTPStatus.NOT_FOUND); return True
        new_aid = _next_free_anchor_id(anchors_dir, config_data)
        ext = src_file.suffix.lower() if src_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
        raw_lbl = str(payload.get("label") or "").strip()
        clean_lbl = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_lbl).strip("_") if raw_lbl else f"From_{aid}_s{take.get('seed') or 1001}"
        if clean_lbl.upper().startswith(f"{new_aid}_"):
            clean_lbl = clean_lbl[len(new_aid) + 1:] or f"From_{aid}"
        new_fn = f"{new_aid}_{clean_lbl}{ext}"
        dest_file = anchors_dir / new_fn
        anchors_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        new_entry = {
            "filename": new_fn,
            "renderer": take.get("renderer"),
            "seed": int(take.get("seed") or 1001),
            "prompt": str(take.get("prompt") or ""),
        }
        for k in ("backend", "resolution", "negative_prompt"):
            if take.get(k): new_entry[k] = take[k]
        parent_sp = next((a for a in config_data.get("anchors", []) if extract_anchor_id(a.get("filename", "")).upper() == aid), None)
        eff_refs = take.get("image_refs") or (parent_sp.get("image_refs") if isinstance(parent_sp, dict) else None)
        if isinstance(eff_refs, list) and eff_refs: new_entry["image_refs"] = list(eff_refs)
        config_data.setdefault("anchors", []).append(new_entry)
        _backup_and_save_project_json(proj_dir, config_file, config_data)
        archive_anchor_take_file(anchors_dir, proj_dir, new_aid, dest_file, new_entry, seed=new_entry["seed"], make_active=True, image_refs=eff_refs if isinstance(eff_refs, list) else None)
        if hasattr(srv, "broadcast_log"): srv.broadcast_log("[ANCHORS_UPDATED]")
        handler.send_json({"success": True, "new_aid": new_aid, "new_anchor": new_entry, "anchor_takes": collect_project_anchor_takes(proj_dir, anchors_dir, config_data)})
        return True

    if action == "char-take-action":
        if not config_file or not isinstance(config_data, dict):
            handler.send_error_json("Valid project config required", status=HTTPStatus.BAD_REQUEST)
            return True
        sub_act = str(payload.get("sub_action") or "").strip().lower()
        eid = str(payload.get("entity_id") or "").strip()
        lid = str(payload.get("look_id") or "").strip()
        rel_path = str(payload.get("rel_path") or "").strip().replace("\\", "/")
        take_id = str(payload.get("take_id") or "").strip()
        chars = config_data.setdefault("characters", {})
        ent = chars.get(eid)
        look = (ent.get("looks", {}) if isinstance(ent, dict) else {}).get(lid)
        if not isinstance(ent, dict) or not isinstance(look, dict):
            handler.send_error_json("Character or look not found", status=HTTPStatus.NOT_FOUND)
            return True

        img_refs: list[str] = look.setdefault("image_refs", [])
        gen_takes: list[dict] = look.setdefault("generated_takes", [])
        take_obj = next((t for t in gen_takes if t.get("take_id") == take_id or t.get("rel_path") == rel_path), None)

        if sub_act == "use_only":
            if rel_path:
                look["image_refs"] = [rel_path]
        elif sub_act == "toggle_use":
            if rel_path in img_refs:
                img_refs.remove(rel_path)
            elif rel_path:
                img_refs.append(rel_path)
        elif sub_act == "set_avatar":
            if rel_path:
                ent["avatar"] = rel_path
        elif sub_act == "delete":
            if rel_path in img_refs:
                img_refs.remove(rel_path)
            look["generated_takes"] = [t for t in gen_takes if t.get("take_id") != take_id and t.get("rel_path") != rel_path]
            if rel_path:
                cand = (proj_dir / rel_path).resolve()
                ref_root = (proj_dir / "references").resolve()
                if cand.is_file() and ref_root in cand.parents:
                    cand.unlink(missing_ok=True)
        elif sub_act == "to_anchor":
            src_file = (proj_dir / rel_path).resolve() if rel_path else None
            if not src_file or not src_file.is_file():
                handler.send_error_json("Reference image file not found on disk", status=HTTPStatus.NOT_FOUND)
                return True
            new_aid = _next_free_anchor_id(anchors_dir, config_data)
            ext = src_file.suffix.lower() if src_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
            clean_lbl = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{ent.get('name') or eid}_{look.get('name') or lid}").strip("_")
            new_fn = f"{new_aid}_{clean_lbl}{ext}"
            dest_file = anchors_dir / new_fn
            anchors_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            traits = str(ent.get("physical_traits") or ent.get("name") or eid).strip()
            ldesc = str(look.get("description") or look.get("name") or lid).strip()
            new_entry = {
                "filename": new_fn,
                "renderer": (take_obj or {}).get("renderer") or look.get("gen_renderer"),
                "seed": int((take_obj or {}).get("seed") or look.get("gen_seed") or 1001),
                "prompt": str((take_obj or {}).get("prompt") or look.get("gen_prompt") or f"Cinematic 16:9 still frame, {traits}, {ldesc}"),
            }
            if (take_obj or {}).get("resolution") or look.get("gen_resolution"):
                new_entry["resolution"] = (take_obj or {}).get("resolution") or look.get("gen_resolution")
            if (take_obj or {}).get("negative_prompt") or look.get("gen_negative_prompt"):
                new_entry["negative_prompt"] = (take_obj or {}).get("negative_prompt") or look.get("gen_negative_prompt")
            config_data.setdefault("anchors", []).append(new_entry)
            _backup_and_save_project_json(proj_dir, config_file, config_data)
            archive_anchor_take_file(anchors_dir, proj_dir, new_aid, dest_file, new_entry, seed=new_entry["seed"], make_active=True)
            if hasattr(srv, "broadcast_log"):
                srv.broadcast_log("[ANCHORS_UPDATED]")
            handler.send_json({"success": True, "new_aid": new_aid, "new_anchor": new_entry, "characters": chars})
            return True
        else:
            handler.send_error_json(f"Unknown char-take-action sub_action: {sub_act}", status=HTTPStatus.BAD_REQUEST)
            return True

        _backup_and_save_project_json(proj_dir, config_file, config_data)
        handler.send_json({"success": True, "characters": chars})
        return True

    return False


from .stage1_anchors import _manifest_used_seeds, reconcile_existing_anchor_queue_item  # noqa: F401


def prepare_targeted_anchor_queue_items(
    srv, slug: str, anchor_args: list[str], is_worker_running: bool | None = None
) -> list[dict]:
    """Queues Stage 1 anchor and character look reference image generation, one queue entry per image to be generated."""
    from .stage1_anchors import match_anchor_id
    from .project_paths import resolve_queue_file
    try:
        from ui.server_engines import active_rendering_map, read_queue_json_locked, reset_session_blocks_for_items, write_queue_json_locked
        from ui.server_fal import enrich_queue_with_fal_metadata
        from pipeline.media_utils import attach_queue_time_estimates
    except ImportError:
        from work.ui.server_engines import active_rendering_map, read_queue_json_locked, reset_session_blocks_for_items, write_queue_json_locked
        from work.ui.server_fal import enrich_queue_with_fal_metadata
        from work.pipeline.media_utils import attach_queue_time_estimates

    proj_dir = srv.PROJECTS_DIR / slug
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
    if not jsons:
        return []
    config_file = jsons[-1]
    ctx = ProjectContext(config_file)
    queue_file = resolve_queue_file(proj_dir, for_write=True)

    with srv.queue_file_lock:
        if not queue_file.is_file():
            srv.compute_fast_project_queue(slug)
        items = read_queue_json_locked(srv, queue_file)
        if is_worker_running is None:
            is_any_act, _ = active_rendering_map(srv, slug)
            is_worker_running = bool(is_any_act or any(x.get("status") == "rendering" for x in items))

        tokens = [
            str(a).strip() for a in (anchor_args or [])
            if str(a).strip() and str(a).strip().lower() not in ("--anchors-only", "anchors-only", "anchors", "--no-stitch", "--no-queue-sync")
        ]
        char_tokens = [t[5:] for t in tokens if t.upper().startswith("CHAR:")]
        anc_tokens = [t for t in tokens if not t.upper().startswith("CHAR:")]

        new_entries: list[dict] = []
        target_aids: set[str] = set()
        shared_cfg = ctx.anchor_shared_cfg
        raw_data = json.loads(config_file.read_text(encoding="utf-8"))

        # 1. Character look reference image generation tokens (CHAR:<entity>:<look>[:x<count>])
        chars_dict = raw_data.get("characters") or {}
        for cr_tok in char_tokens:
            parts = [p.strip() for p in cr_tok.split(":") if p.strip()]
            if len(parts) < 2:
                continue
            eid, lid = parts[0], parts[1]
            ent = chars_dict.get(eid) if isinstance(chars_dict, dict) else None
            look = (ent.get("looks") or {}).get(lid) if isinstance(ent, dict) else None
            if not isinstance(ent, dict) or not isinstance(look, dict):
                continue
            is_iso = any(p.lower() == "isolate" for p in parts[2:])
            cnt = 1
            if not is_iso:
                if len(parts) >= 3 and re.search(r"(\d+)", parts[2]):
                    cnt = max(1, min(99, int(re.search(r"(\d+)", parts[2]).group(1))))
                elif look.get("gen_count"):
                    try: cnt = max(1, min(99, int(look["gen_count"])))
                    except (TypeError, ValueError): cnt = 1
            iso_cfg = (look.get("isolate_spec") or {}) if is_iso else {}
            base_seed = int(iso_cfg.get("seed") or look.get("gen_seed") or 1001)
            sp_like = {
                "filename": f"CHAR_{eid}_{lid}.jpg",
                "renderer": iso_cfg.get("renderer", 1) if is_iso else look.get("gen_renderer"),
                "backend": iso_cfg.get("backend") if is_iso else look.get("gen_backend"),
                "resolution": iso_cfg.get("resolution", "1024x1024") if is_iso else look.get("gen_resolution"),
                "seed": base_seed,
            }
            _, rspec = ctx.resolve_anchor_renderer(sp_like)
            from .prompts import align_anchor_resolution
            res_val = align_anchor_resolution(str(sp_like.get("resolution") or shared_cfg.get("resolution") or rspec.get("resolution") or "1920x1080").strip(), rspec.get("model_type", ""))
            aid_key = f"CHAR:{eid}:{lid}"
            target_aids.add(aid_key.upper())
            queued_seeds = [{"seed": it.get("seed")} for it in items if str(it.get("anchor_id") or "").upper() == aid_key.upper() and it.get("status") in ("queued", "rendering") and it.get("seed") is not None]
            batch_seeds = [base_seed] if is_iso else select_unused_take_seeds(list(look.get("generated_takes") or []) + queued_seeds, base_seed, cnt)
            clean_ent = re.sub(r"[^a-zA-Z0-9_-]", "_", eid).strip("_") or "character"
            clean_look = re.sub(r"[^a-zA-Z0-9_-]", "_", lid).strip("_") or "look"
            for b_idx, seed_val in enumerate(batch_seeds):
                stem = f"CHAR_{clean_ent}_{clean_look}_s{seed_val}"
                fn = f"{stem}.jpg"
                reason = f"CHAR ISOLATE @{clean_look} (s{seed_val})" if is_iso else (f"CHAR REF @{clean_look} {b_idx + 1}/{cnt} (s{seed_val})" if cnt > 1 else f"CHAR REF @{clean_look} (s{seed_val})")
                new_entries.append({
                    "idx": 0, "id": stem, "stage": "anchor", "anchor_id": aid_key, "scene_id": aid_key,
                    "target_arg": f"CHAR:{eid}:{lid}:isolate:s{seed_val}" if is_iso else f"CHAR:{eid}:{lid}:s{seed_val}", "pass": None, "seed": seed_val,
                    "take_index": b_idx + 1, "take_total": cnt, "filename": fn, "output_filename": stem,
                    "duration": 0.0, "frames": 1, "num_inference_steps": int(rspec.get("num_inference_steps") or 8),
                    "status": "queued", "reason": reason, "needs_render": True,
                    "task_hash": f"char:{stem}:{seed_val}", "model_type": rspec.get("model_type", "flux2_klein_9b_kv"),
                    "backend": resolve_task_backend(ctx, sc=sp_like, rspec=rspec, stage="anchor"), "resolution": res_val,
                })

        # 2. Stage 1 Anchor image generation tokens (single anchor, batch A03:xN, all-anchors, or missing)
        if anc_tokens or not char_tokens:
            manifest = load_anchor_takes_manifest(ctx.anchors_dir)
            has_all = any(t.upper() in ("ALL", "ALL-ANCHORS", "--ALL-ANCHORS") for t in anc_tokens)
            for sp in ctx.anchors:
                aid, target_file = ctx.resolve_anchor_path(sp)
                fn = target_file.name
                if sp.get("external") and not str(sp.get("prompt") or "").strip():
                    continue
                if anc_tokens:
                    if not has_all and not any(match_anchor_id(tok, fn) for tok in anc_tokens):
                        continue
                    cnt = parse_anchor_batch_count(anc_tokens, fn, sp)
                else:
                    if target_file.is_file() and target_file.stat().st_size > 1000:
                        continue
                    cnt = 1
                _, rspec = ctx.resolve_anchor_renderer(sp)
                from .prompts import align_anchor_resolution
                res_val = align_anchor_resolution(str(sp.get("resolution") or shared_cfg.get("resolution") or rspec.get("resolution") or "1920x1080").strip(), rspec.get("model_type", ""))
                base_seed = int(sp.get("seed", 42))
                target_aids.add(aid.upper())
                existing_takes = list((manifest.get("anchors", {}).get(aid.upper()) or {}).get("takes") or [])
                queued_seeds = [{"seed": it.get("seed")} for it in items if str(it.get("anchor_id") or it.get("scene_id") or "").upper() == aid.upper() and it.get("status") in ("queued", "rendering") and it.get("seed") is not None]
                batch_seeds = select_unused_take_seeds(existing_takes + queued_seeds, base_seed, cnt)
                ext = target_file.suffix.lower() if target_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
                for b_idx, seed_val in enumerate(batch_seeds):
                    stem = clean_anchor_queue_stem(target_file.stem, seed_val)
                    q_fn = f"{stem}{ext}"
                    reason = f"ANCHOR {aid} TAKE {b_idx + 1}/{cnt} (s{seed_val})" if cnt > 1 else f"ANCHOR {aid} (s{seed_val})"
                    new_entries.append({
                        "idx": 0, "id": stem, "stage": "anchor", "anchor_id": aid, "scene_id": aid,
                        "target_arg": f"{aid}:s{seed_val}", "pass": None, "seed": seed_val,
                        "take_index": b_idx + 1, "take_total": cnt, "filename": q_fn, "output_filename": stem,
                        "duration": 0.0, "frames": 1, "num_inference_steps": int(rspec.get("num_inference_steps") or 8),
                        "status": "queued", "reason": reason, "needs_render": True,
                        "task_hash": f"anchor:{stem}:{seed_val}", "model_type": rspec.get("model_type", "flux2_klein_9b_kv"),
                        "backend": resolve_task_backend(ctx, sc=sp, rspec=rspec, stage="anchor"), "resolution": res_val,
                    })

        # Drop stale/completed/already-rendered anchor rows for targeted anchor IDs; defer unstarted scenes if idle
        used_seeds = _manifest_used_seeds(ctx.anchors_dir, manifest=manifest) if (anc_tokens or not char_tokens) else set()
        has_explicit_seed = any((":s" in t.lower() or "@s" in t.lower()) for t in anc_tokens)
        kept_items: list[dict] = []
        for it in items:
            if it.get("stage") == "anchor":
                it_aid = str(it.get("anchor_id") or it.get("scene_id") or "").upper()
                rsn_u = str(it.get("reason") or "").upper()
                if rsn_u == "COMPLETED" and it.get("completed_at"):
                    it.update({"status": "completed", "needs_render": False})
                if it_aid in target_aids:
                    if it.get("status") not in ("queued", "rendering"):
                        continue
                    if not has_explicit_seed and it.get("status") == "queued" and "FORCED" not in rsn_u:
                        try: s_val = int(it["seed"]) if it.get("seed") is not None else None
                        except (TypeError, ValueError): s_val = None
                        if s_val is not None and (it_aid, s_val) in used_seeds:
                            continue
            elif not is_worker_running and it.get("status") in ("queued", "rendering"):
                it.update({"status": "skipped", "needs_render": False, "reason": "DEFERRED (ANCHOR RENDER)"})
            kept_items.append(it)

        # Insert new entries right after any actively rendering items and any already-queued anchor items
        insert_at = 0
        for idx_i, it in enumerate(kept_items):
            if it.get("status") == "rendering" or (it.get("stage") == "anchor" and it.get("status") == "queued"):
                insert_at = idx_i + 1
        kept_items[insert_at:insert_at] = new_entries
        if hasattr(srv, "normalize_queue_units"):
            srv.normalize_queue_units(kept_items, srv)
        else:
            for i, itm in enumerate(kept_items, 1):
                itm["idx"], itm["total"] = i, len(kept_items)

        sc_map = {s.get("id"): s for s in ctx.scenes if s.get("id")}
        try:
            enrich_queue_with_fal_metadata(kept_items, sc_map, ctx=ctx, proj_dir=proj_dir, default_res=ctx.current_resolution)
            attach_queue_time_estimates(kept_items, sc_map, ctx=ctx, proj_dir=proj_dir, default_res=ctx.current_resolution)
        except Exception:
            pass
        reset_session_blocks_for_items(srv, new_entries, clear_failed=True)
        write_queue_json_locked(srv, queue_file, kept_items)
        return new_entries
