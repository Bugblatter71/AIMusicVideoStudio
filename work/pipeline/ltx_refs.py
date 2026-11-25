"""LTX-2.5 multi-reference video workflows (MSR Subject Ref & Ingredients Reference Sheet)."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from .media_utils import _file_sha256, resolve_ffmpeg_path, resolve_path
from .prompts import adapt_video_prompt, expand_variables, find_ref_tags, strip_dangling_ref_tags


def get_video_ref_mode(rspec: dict | None) -> str:
    """Returns canonical video reference mode: 'ltx2_msr', 'ltx2_ingredients', 'h3_ref2va', or ''."""
    r = rspec or {}
    explicit = str(r.get("ref_mode") or "").strip().lower()
    if explicit:
        return explicit
    mtype = str(r.get("model_type") or "").strip().lower()
    if "ltx2" in mtype and "msr" in mtype:
        return "ltx2_msr"
    from .fal_catalog import is_ref2va_spec

    return "h3_ref2va" if is_ref2va_spec(r) else ""


def is_ltx2_msr(rspec: dict | None) -> bool:
    return get_video_ref_mode(rspec) == "ltx2_msr"


def is_ltx2_ingredients(rspec: dict | None) -> bool:
    return get_video_ref_mode(rspec) == "ltx2_ingredients"


def is_ltx2_ref_spec(rspec: dict | None) -> bool:
    return get_video_ref_mode(rspec) in ("ltx2_msr", "ltx2_ingredients")


def snap_ltx2_resolution(res_str: str) -> str:
    """Snaps WxH to nearest 32px-aligned dimensions required by LTX-2.5 VAE (e.g. 1280x720 -> 1280x704)."""
    raw = str(res_str or "1280x704").strip().lower()
    if "x" not in raw:
        return raw
    try:
        w, h = [int(p.split()[0]) for p in raw.split("x", 1)]
        if w <= 0 or h <= 0:
            return raw
        if w % 32 == 0 and h % 32 == 0:
            return f"{w}x{h}"
        nw = max(64, ((w + 15) // 32) * 32)
        nh = max(64, ((h + 15) // 32) * 32)
        return f"{nw}x{nh}"
    except ValueError:
        return raw


def _existing_file_or_none(p_like: Path | str | None) -> Path | None:
    if not p_like or not str(p_like).strip():
        return None
    p = Path(p_like)
    return p if p.is_file() else None


def _path_key(p: Path) -> str:
    try:
        return str(p.resolve()).lower()
    except OSError:
        return str(p).lower()


def clean_ltx2_ref_prompt(
    ctx,
    raw_prompt: str,
    has_end: bool = False,
) -> str:
    """Expands variables and @character tokens into clean natural LTX-2.5 prose (never H3 markup)."""
    from .characters import compile_character_prompt

    expanded = expand_variables(str(raw_prompt or ""), getattr(ctx, "variables", {}) or {})
    tag_lookup = getattr(ctx, "tag_lookup", None) or {}
    if tag_lookup:
        compiled, _ = compile_character_prompt(expanded, tag_lookup, supports_refs=False, has_end=has_end)
    else:
        compiled = expanded
    prose = adapt_video_prompt(compiled, "ltx2")
    prose = strip_dangling_ref_tags(prose)
    prose = re.sub(r"\s*<(?:image|Picture\s*)\d+>", "", prose, flags=re.IGNORECASE)
    prose = re.sub(r"[ \t]+", " ", prose)
    prose = re.sub(r" ?\n ?", "\n", prose)
    prose = re.sub(r"\n{2,}", "\n", prose).strip()
    return prose


def collect_ltx2_scene_refs(
    ctx,
    sc: dict | None,
    raw_prompt: str = "",
    fallback_refs: list[Path | str] | None = None,
    max_refs: int = 6,
    exclude_paths: list[Path | str] | None = None,
) -> list[Path]:
    """Collects deduplicated existing reference image Paths for LTX-2.5 MSR / Ingredients."""
    from .characters import find_prompt_entities

    proj_dir = getattr(ctx, "project_dir", Path("."))
    tag_lookup = getattr(ctx, "tag_lookup", None) or {}
    excluded = {_path_key(Path(x)) for x in (exclude_paths or []) if x and str(x).strip()}
    collected: list[Path] = []
    seen: set[str] = set(excluded)

    def _add(cand: Path | str | None) -> None:
        if len(collected) >= max_refs or not cand or not str(cand).strip():
            return
        p = Path(cand)
        if not p.is_absolute():
            p = resolve_path(proj_dir, str(cand))
        if not p.is_file():
            return
        k = _path_key(p)
        if k in seen:
            return
        seen.add(k)
        collected.append(p)

    has_explicit_empty = isinstance(sc, dict) and "image_refs" in sc and isinstance(sc["image_refs"], list) and len(sc["image_refs"]) == 0
    if isinstance(sc, dict) and isinstance(sc.get("image_refs"), list):
        for r in sc["image_refs"]:
            _add(r)

    if raw_prompt and tag_lookup:
        ents = find_prompt_entities(raw_prompt, tag_lookup)
        if not ents and getattr(ctx, "variables", None):
            ents = find_prompt_entities(expand_variables(raw_prompt, ctx.variables), tag_lookup)
        for ent in ents:
            for r in ent.get("image_refs", []) or []:
                _add(r)

    if not collected and not has_explicit_empty and fallback_refs:
        exp_prompt = expand_variables(raw_prompt, getattr(ctx, "variables", {}) or {})
        has_img_tag = bool(find_ref_tags(exp_prompt))
        # When explicit fallback_refs exist (from scene image_refs / vshared / test caller) or <imageN> is cited, collect them
        if has_img_tag or not isinstance(sc, dict) or "image_refs" in sc or bool(fallback_refs):
            for r in fallback_refs:
                _add(r)

    return collected[:max_refs]


def _grid_geometry(count: int) -> tuple[int, int, int, int]:
    """Returns (cols, rows, width, height) for a clean 32px-aligned Ingredients reference sheet."""
    if count <= 2:
        return 2, 1, 1536, 768
    if count == 3:
        return 3, 1, 1536, 640
    if count == 4:
        return 2, 2, 1536, 1024
    return 3, 2, 1536, 1024


def _cell_boxes(cols: int, rows: int, width: int, height: int, sep: int) -> tuple[list[tuple[int, int, int, int]], list[int], list[int]]:
    avail_w = width - (cols - 1) * sep
    avail_h = height - (rows - 1) * sep
    cw_base, cw_rem = divmod(avail_w, cols)
    ch_base, ch_rem = divmod(avail_h, rows)

    col_x: list[int] = []
    col_w: list[int] = []
    v_lines: list[int] = []
    cur_x = 0
    for c in range(cols):
        w = cw_base + (1 if c < cw_rem else 0)
        col_x.append(cur_x)
        col_w.append(w)
        cur_x += w
        if c < cols - 1:
            v_lines.append(cur_x)
            cur_x += sep

    row_y: list[int] = []
    row_h: list[int] = []
    h_lines: list[int] = []
    cur_y = 0
    for r in range(rows):
        h = ch_base + (1 if r < ch_rem else 0)
        row_y.append(cur_y)
        row_h.append(h)
        cur_y += h
        if r < rows - 1:
            h_lines.append(cur_y)
            cur_y += sep

    boxes: list[tuple[int, int, int, int]] = []
    for r in range(rows):
        for c in range(cols):
            boxes.append((col_x[c], row_y[r], col_w[c], row_h[r]))
    return boxes, v_lines, h_lines


def _compose_with_pillow(refs: list[Path], out_path: Path, boxes, v_lines, h_lines, width: int, height: int, sep: int) -> None:
    from PIL import Image, ImageDraw, ImageOps

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    for p, (bx, by, bw, bh) in zip(refs, boxes):
        with Image.open(p) as im:
            rgb = im.convert("RGB")
            fitted = ImageOps.contain(rgb, (bw, bh), method=resample)
            px = bx + (bw - fitted.width) // 2
            py = by + (bh - fitted.height) // 2
            canvas.paste(fitted, (px, py))
    draw = ImageDraw.Draw(canvas)
    for vx in v_lines:
        draw.rectangle([vx, 0, vx + sep - 1, height - 1], fill=(0, 0, 0))
    for hy in h_lines:
        draw.rectangle([0, hy, width - 1, hy + sep - 1], fill=(0, 0, 0))
    canvas.save(out_path, format="JPEG", quality=95)


def _compose_with_ffmpeg(ffmpeg: Path, refs: list[Path], out_path: Path, boxes, v_lines, h_lines, width: int, height: int, sep: int) -> None:
    cmd = [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"color=c=white:s={width}x{height}:d=1"]
    for p in refs:
        cmd.extend(["-i", str(p)])
    steps = []
    last_label = "[0:v]format=rgb24[base0]"
    steps.append(last_label)
    cur_bg = "base0"
    for i, (_, (bx, by, bw, bh)) in enumerate(zip(refs, boxes)):
        in_idx = i + 1
        cell_lbl = f"c{i}"
        next_lbl = f"bg{i + 1}"
        steps.append(
            f"[{in_idx}:v]scale=w={bw}:h={bh}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={bw}:{bh}:(ow-iw)/2:(oh-ih)/2:color=white,format=rgb24[{cell_lbl}]"
        )
        steps.append(f"[{cur_bg}][{cell_lbl}]overlay=x={bx}:y={by}:shortest=1[{next_lbl}]")
        cur_bg = next_lbl
    draw_ops = [f"drawbox=x={vx}:y=0:w={sep}:h={height}:color=black@1.0:t=fill" for vx in v_lines]
    draw_ops.extend(f"drawbox=x=0:y={hy}:w={width}:h={sep}:color=black@1.0:t=fill" for hy in h_lines)
    if draw_ops:
        steps.append(f"[{cur_bg}]{','.join(draw_ops)}[out]")
        map_lbl = "[out]"
    else:
        map_lbl = f"[{cur_bg}]"
    cmd.extend(["-filter_complex", ";".join(steps), "-map", map_lbl, "-frames:v", "1", "-q:v", "2", str(out_path)])
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def compose_ingredients_sheet(
    refs: list[Path],
    cache_dir: Path,
    ffmpeg_bin: Path | None = None,
    sep_px: int = 4,
) -> Path:
    """Composes 2-6 reference images into a deterministic white-background black-grid JPEG sheet."""
    if not refs:
        raise ValueError("compose_ingredients_sheet requires at least 1 image")
    if len(refs) == 1:
        return refs[0]

    items = list(refs[:6])
    sig_parts = [f"{p.resolve()}:{_file_sha256(p)}" for p in items]
    sig = hashlib.sha256(("|".join(sig_parts) + f"|sep={sep_px}").encode("utf-8")).hexdigest()[:16]
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"sheet_{sig}.jpg"
    if out_path.is_file() and out_path.stat().st_size > 200:
        return out_path

    cols, rows, width, height = _grid_geometry(len(items))
    boxes, v_lines, h_lines = _cell_boxes(cols, rows, width, height, sep_px)
    try:
        _compose_with_pillow(items, out_path, boxes, v_lines, h_lines, width, height, sep_px)
        return out_path
    except ImportError:
        ff = Path(ffmpeg_bin) if ffmpeg_bin else resolve_ffmpeg_path(Path(__file__).resolve().parent.parent.parent, "", None)
        _compose_with_ffmpeg(ff, items, out_path, boxes, v_lines, h_lines, width, height, sep_px)
        return out_path


def _is_scene_audio_silent(task: dict, sc: dict | None) -> bool:
    mode = str((sc or {}).get("mode") or "").strip().lower()
    stem = str((sc or {}).get("stem") or "").strip().lower()
    if mode in ("silent_locked", "silent", "ambient"):
        return True
    if stem in ("silent", "none") and mode not in ("duet_method_a", "duet_a"):
        return True
    ag = str(task.get("audio_guide") or "").strip()
    if not ag or Path(ag).name.lower().startswith("silent_"):
        return True
    return False


def is_ref_premasked_white(ctx, ref_path: Path | str | None) -> bool:
    """Returns True when `ref_path` is a recorded white-composited Studio cutout in `characters[*].looks[*].ref_masks`."""
    if not ref_path:
        return False
    proj_dir = getattr(ctx, "project_dir", None)
    chars = getattr(ctx, "characters", None)
    if not isinstance(chars, dict) or not chars:
        return False
    ref_p = Path(ref_path)
    try:
        ref_rel = ref_p.resolve().relative_to(Path(proj_dir).resolve()).as_posix() if (proj_dir and ref_p.is_absolute()) else str(ref_path).replace("\\", "/").lstrip("/")
    except Exception:
        ref_rel = str(ref_path).replace("\\", "/").lstrip("/")
    ref_key = _path_key(resolve_path(proj_dir, ref_rel)) if proj_dir else _path_key(ref_p)

    for ent in chars.values():
        if not isinstance(ent, dict):
            continue
        for look in (ent.get("looks") or {}).values():
            if not isinstance(look, dict):
                continue
            rmap = look.get("ref_masks")
            if not isinstance(rmap, dict):
                continue
            meta = rmap.get(ref_rel)
            if isinstance(meta, dict) and meta.get("premasked_white", True):
                return True
            for k, v in rmap.items():
                if isinstance(v, dict) and v.get("premasked_white", True):
                    cand = resolve_path(proj_dir, k) if proj_dir else Path(k)
                    if _path_key(cand) == ref_key:
                        return True
    return False


def resolve_msr_remove_bg_flag(ctx, sc: dict | None, rspec: dict, subj_refs: list[Path | str] | None) -> int:
    """Computes `remove_background_images_ref` for LTX-2.5 MSR, auto-skipping u2net when all subject refs are Studio cutouts."""
    if isinstance(sc, dict):
        if sc.get("msr_remove_bg") is True:
            return 1
        if sc.get("msr_remove_bg") is False:
            return 0
    if subj_refs and all(is_ref_premasked_white(ctx, r) for r in subj_refs):
        return 0
    return int(rspec.get("remove_background_images_ref", 1))


def prepare_ltx2_msr_task(
    ctx,
    task: dict,
    *,
    rspec: dict,
    sc: dict | None,
    raw_prompt: str,
    image_start: Path | str | None,
    image_refs: list[Path | str] | None,
    has_end: bool,
    video_guide: Path | None,
    video_mask: Path | None,
) -> dict:
    """Configures a Wan2GP task dict strictly adhering to LTX-2.5 MSR validation constraints."""
    if video_guide and video_mask:
        raise ValueError(
            "LTX-2.5 MSR does not support Two-Pass masked inpainting (Pass 2). "
            "Use LTX-2.5 Standard/Ingredients or MiniMax H3 for Pass 2."
        )

    start_file = _existing_file_or_none(image_start)
    use_start_as_bg = not (isinstance(sc, dict) and sc.get("msr_use_start_as_bg") is False)

    subj_refs = collect_ltx2_scene_refs(
        ctx, sc, raw_prompt=raw_prompt, fallback_refs=image_refs, max_refs=4,
        exclude_paths=[start_file] if (use_start_as_bg and start_file) else None,
    )
    remove_bg = resolve_msr_remove_bg_flag(ctx, sc, rspec, subj_refs)

    if use_start_as_bg and start_file and subj_refs:
        msr_refs = [start_file] + subj_refs[:4]
        vpt = "KI"
    elif subj_refs:
        msr_refs = subj_refs[:4]
        vpt = "I"
    elif use_start_as_bg and start_file:
        msr_refs = [start_file]
        vpt = "I"
    else:
        raise ValueError("LTX-2.5 MSR requires at least 1 reference image (or a Start Anchor + 1 subject ref).")

    task.pop("image_start", None)
    task["image_prompt_type"] = "TE" if (has_end and task.get("image_end")) else "T"
    task["video_prompt_type"] = vpt
    task["image_refs"] = [str(p) for p in msr_refs]
    task["remove_background_images_ref"] = remove_bg
    task["image_refs_relative_size"] = int(rspec.get("image_refs_relative_size", 100))
    task["resolution"] = snap_ltx2_resolution(task.get("resolution", "1280x704"))

    if _is_scene_audio_silent(task, sc):
        task["audio_prompt_type"] = ""
        task["audio_guide"] = ""
    else:
        task["audio_prompt_type"] = "AS"
    return task


def prepare_ltx2_ingredients_task(
    ctx,
    task: dict,
    *,
    rspec: dict,
    sc: dict | None,
    raw_prompt: str,
    image_refs: list[Path | str] | None,
    video_guide: Path | None,
    video_mask: Path | None,
) -> dict:
    """Composes 1-6 refs into a single white-background Ingredients sheet (disabled automatically on Pass 2 MVGA)."""
    task["remove_background_images_ref"] = 0
    if video_guide and video_mask:
        task.pop("image_refs", None)
        if "I" in str(task.get("video_prompt_type") or ""):
            task["video_prompt_type"] = str(task["video_prompt_type"]).replace("I", "")
        return task

    refs = collect_ltx2_scene_refs(ctx, sc, raw_prompt=raw_prompt, fallback_refs=image_refs, max_refs=6)
    if not refs:
        task["video_prompt_type"] = ""
        task.pop("image_refs", None)
        return task

    if len(refs) == 1:
        sheet = refs[0]
    else:
        cache_dir = getattr(ctx, "jobs_dir", Path(".")) / "ingredients_cache"
        sheet = compose_ingredients_sheet(refs, cache_dir=cache_dir, ffmpeg_bin=getattr(ctx, "ffmpeg", None))

    task["image_refs"] = [str(sheet)]
    task["video_prompt_type"] = "I"
    task["image_refs_relative_size"] = int(rspec.get("image_refs_relative_size", 100))
    return task


def apply_ltx2_ref_mode(
    ctx,
    task: dict,
    *,
    rspec: dict,
    sc: dict | None,
    raw_prompt: str,
    image_start: Path | str | None,
    image_refs: list[Path | str] | None,
    has_end: bool,
    video_guide: Path | None,
    video_mask: Path | None,
) -> dict:
    """Applies MSR or Ingredients reference formatting and prompt cleanup to an LTX-2.5 task."""
    task["prompt"] = clean_ltx2_ref_prompt(ctx, raw_prompt, has_end=has_end)
    mode = get_video_ref_mode(rspec)
    if mode == "ltx2_msr":
        return prepare_ltx2_msr_task(
            ctx, task, rspec=rspec, sc=sc, raw_prompt=raw_prompt, image_start=image_start,
            image_refs=image_refs, has_end=has_end, video_guide=video_guide, video_mask=video_mask,
        )
    if mode == "ltx2_ingredients":
        return prepare_ltx2_ingredients_task(
            ctx, task, rspec=rspec, sc=sc, raw_prompt=raw_prompt, image_refs=image_refs,
            video_guide=video_guide, video_mask=video_mask,
        )
    return task


def resolve_ltx2_hash_refs(ctx, sc: dict, vrspec: dict, raw_prompt: str, anchor_path: Path | None, pass_num: int | None = None) -> list[Path]:
    """Resolves ordered reference file paths included in task hashing and queue staleness checks."""
    mode = get_video_ref_mode(vrspec)
    if pass_num == 2 and mode == "ltx2_ingredients":
        return []
    fallback = ctx.resolve_scene_image_refs(sc, raw_prompt) if hasattr(ctx, "resolve_scene_image_refs") else None
    if mode == "ltx2_msr":
        use_bg = not (isinstance(sc, dict) and sc.get("msr_use_start_as_bg") is False)
        start_f = _existing_file_or_none(anchor_path)
        subj = collect_ltx2_scene_refs(ctx, sc, raw_prompt=raw_prompt, fallback_refs=fallback, max_refs=4, exclude_paths=[start_f] if (use_bg and start_f) else None)
        if use_bg and start_f and subj:
            return [start_f] + subj
        return subj or ([start_f] if (use_bg and start_f) else [])
    if mode == "ltx2_ingredients":
        return collect_ltx2_scene_refs(ctx, sc, raw_prompt=raw_prompt, fallback_refs=fallback, max_refs=6)
    return []


def collect_scene_active_ref_paths(ctx, sc: dict, vrspec: dict, raw_prompt: str, pass_num: int | None = None) -> list[Path]:
    """Unified queue-staleness ref resolver for H3 Ref2VA and LTX-2.5 ref workflows."""
    from .fal_catalog import is_ref2va_spec
    from .prompts import resolve_scene_refs

    if is_ltx2_ref_spec(vrspec):
        anc_p = None
        if sc.get("anchor"):
            _, anc_p = ctx.resolve_anchor_path(sc.get("anchor"))
        return resolve_ltx2_hash_refs(ctx, sc, vrspec, raw_prompt, anc_p, pass_num=pass_num)
    if is_ref2va_spec(vrspec):
        cand_refs = ctx.resolve_scene_image_refs(sc, raw_prompt)
        resolved, _ = resolve_scene_refs(raw_prompt, cand_refs)
        return resolved
    return []
