"""Import external rendered video clips (e.g., Google Flow / Veo) into scenes as registered takes."""

from __future__ import annotations

import copy
import datetime
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from .context import ProjectContext
from .media_utils import (
    WORKSPACE_ROOT,
    _file_sha256,
    probe_video_dimensions,
    probe_video_duration,
    probe_video_fps,
    probe_video_frames,
    render_settings_of,
)
from .prompts import extract_anchor_id
from .project_paths import path_rel_str
from .stage2_hashes import compute_project_task_hashes
from .takes import (
    _active_strip_path,
    _find_strip,
    _pass_key,
    before_scene_render,
    get_takes_manifest_path,
    load_takes_manifest,
    safe_link_or_copy,
    save_takes_manifest,
)
from .upscale import parse_res_wh, resolve_scene_canonical_stem

MAX_HTTP_IMPORT_BYTES = 512 * 1024 * 1024  # 512 MiB cap for uploaded MP4/MOV/WebM clips
_ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


def _generate_import_strip(ffmpeg: Path, mp4_path: Path, strip_path: Path, dur_s: float) -> bool:
    """Extracts a lightweight horizontal review thumbnail strip for the imported clip."""
    try:
        strip_path.parent.mkdir(parents=True, exist_ok=True)
        n_thumbs = max(2, min(10, round(max(1.0, dur_s) / 1.0)))
        r = n_thumbs / max(0.5, dur_s)
        subprocess.run(
            [
                str(ffmpeg), "-y", "-i", str(mp4_path),
                "-vf", f"fps={r:.4f},scale=-1:144,tile={n_thumbs}x1",
                "-frames:v", "1", "-q:v", "2", str(strip_path),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return strip_path.is_file() and strip_path.stat().st_size > 0
    except Exception:
        return False


def _clear_scene_trim_cache(ctx: ProjectContext, sid: str) -> None:
    if ctx.trimmed_dir.is_dir():
        for old_trim in ctx.trimmed_dir.glob(f"*_{sid}_*.mp4"):
            try:
                old_trim.unlink()
            except OSError:
                pass


def _clear_queue_override_for_stem(ctx: ProjectContext, out_name: str) -> None:
    """Clears stale FORCED/TARGETED/failed/skipped flags in v3_queue.json after external import."""
    import json
    from .media_utils import atomic_write_json, locked_file_op
    q_file = ctx.jobs_dir / "v3_queue.json"
    if not q_file.is_file():
        return
    try:
        with locked_file_op(q_file.with_suffix(".lock")):
            items = json.loads(q_file.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                return
            changed = False
            for itm in items:
                if isinstance(itm, dict) and itm.get("output_filename") == out_name:
                    for k in ("user_skipped_hash", "user_skipped_anchor_mtime"):
                        itm.pop(k, None)
                    itm["status"] = "completed"
                    itm["needs_render"] = False
                    itm["reason"] = "UP-TO-DATE (IMPORTED)"
                    changed = True
            if changed:
                atomic_write_json(q_file, items)
    except Exception:
        pass


def import_video_clip_as_take(
    proj_dir: Path | str,
    scene_id: str,
    src_video_path: Path | str,
    *,
    config_file: Path | str | None = None,
    pass_num: int | None = None,
    label: str | None = None,
    normalize: bool = True,
    source_name: str | None = None,
    backend_tag: str = "import",
) -> dict[str, Any]:
    """Normalizes an external video clip into a scene render, registers a new Take, and marks hashes current."""
    pdir = Path(proj_dir)
    if config_file is None:
        from .cli import is_project_config
        jsons = [j for j in sorted(pdir.glob("*.json")) if is_project_config(j)]
        if not jsons:
            jsons = [j for j in sorted(pdir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
        if not jsons:
            raise FileNotFoundError(f"No project config JSON found in {pdir}")
        config_path = jsons[-1]
    else:
        config_path = Path(config_file)

    src_path = Path(src_video_path)
    if not src_path.is_file() or src_path.stat().st_size < 1000:
        raise FileNotFoundError(f"Source video clip missing or empty: {src_path}")

    ctx = ProjectContext(config_path)
    sc = next((s for s in ctx.scenes if str(s.get("id", "")).upper() == str(scene_id).strip().upper()), None)
    if not sc:
        raise ValueError(f"Scene '{scene_id}' not found in {config_path.name}.")

    sid = str(sc["id"])
    mode = str(sc.get("mode", "solo")).lower()
    is_two_pass = mode in ("duet", "gva_male_left_silent_right", "duet_method_b", "two_pass_gva")
    req_pass = int(pass_num) if pass_num in (1, 2) else None

    vr_id, vrspec = ctx.resolve_video_renderer(sc, pass_num=1 if (is_two_pass and req_pass == 1) else None)
    res_str = ctx.resolve_scene_resolution(sc, vrspec)
    tw, th = parse_res_wh(res_str) or (1344, 768)

    if is_two_pass and req_pass == 1:
        out_name = f"{sid}_Pass1_Left_{res_str}"
        target_pass = 1
    else:
        out_name, _, canon_pass = resolve_scene_canonical_stem(sc, ctx.renders_dir, res_str)
        target_pass = canon_pass

    ctx.renders_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = ctx.renders_dir / f"{out_name}.mp4"

    disp_name = Path(source_name or src_path.name).name
    clean_label = str(label or "").strip()

    head_pad, tail_pad = ctx.get_scene_render_padding(sc)
    req_start = max(0.0, float(sc.get("start", 0.0)) - head_pad)
    req_end = float(sc.get("end", 0.0)) + tail_pad
    needed_dur = max(0.1, req_end - req_start)

    tmp_out = ctx.renders_dir / f".import_tmp_{sid}_{int(time.time() * 1000)}.mp4"
    try:
        if normalize:
            vf = (
                f"scale={tw}:{th}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={tw}:{th},setsar=1:1,fps={ctx.fps},scale=out_range=tv,format=yuv420p"
            )
            cmd = [
                str(ctx.ffmpeg), "-y", "-i", str(src_path),
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                "-color_range", "tv", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                str(tmp_out),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            shutil.copy2(str(src_path), str(tmp_out))

        if not tmp_out.is_file() or tmp_out.stat().st_size < 1000:
            raise RuntimeError(f"Imported clip produced empty output for {sid}")

        actual_dims = probe_video_dimensions(ctx.ffmpeg, tmp_out) or (tw, th)
        actual_res = f"{actual_dims[0]}x{actual_dims[1]}"
        actual_dur = float(probe_video_duration(ctx.ffmpeg, tmp_out) or 0.0)
        actual_fps = float(probe_video_fps(ctx.ffmpeg, tmp_out) or ctx.fps or 24.0)
        actual_frames = int(probe_video_frames(ctx.ffmpeg, tmp_out) or max(1, round(actual_dur * actual_fps)))

        warnings: list[str] = []
        if actual_dur > 0.0 and actual_dur < (needed_dur - 0.05):
            warnings.append(
                f"Imported clip duration ({actual_dur:.2f}s) is shorter than scene duration ({needed_dur:.2f}s); "
                f"Stage 3 will hold the final frame unless scene end is trimmed."
            )

        state = ctx.load_state_hashes()
        before_scene_render(ctx, sc, out_name, out_mp4, state)

        task_hashes_map = compute_project_task_hashes(ctx)
        cur_hash = (task_hashes_map.get(out_name) or [""])[0]
        digest = hashlib.sha256(f"{out_name}:{disp_name}:{time.time()}".encode()).hexdigest()[:12]
        take_id = f"take_imp_{digest}"
        vault_rel_mp4 = f"takes/{sid}/{out_name}__imp_{digest}.mp4"
        vault_mp4 = ctx.renders_dir / vault_rel_mp4
        vault_mp4.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_out), str(vault_mp4))
        safe_link_or_copy(vault_mp4, out_mp4)
        now_ts = time.time()
        try:
            os.utime(str(out_mp4), (now_ts, now_ts))
        except OSError:
            pass

        strip_path = _active_strip_path(ctx.project_dir, sid)
        vault_rel_strip = None
        if _generate_import_strip(ctx.ffmpeg, out_mp4, strip_path, max(1.0, actual_dur or needed_dur)):
            vault_rel_strip = f"takes/{sid}/{out_name}__imp_{digest}_strip.jpg"
            safe_link_or_copy(strip_path, ctx.renders_dir / vault_rel_strip)
            if getattr(ctx, "strips_dir", None):
                try:
                    safe_link_or_copy(strip_path, ctx.strips_dir / f"review_{sid}.jpg")
                except OSError:
                    pass
        elif _find_strip(ctx.project_dir, sid, out_name):
            s_found = _find_strip(ctx.project_dir, sid, out_name)
            vault_rel_strip = f"takes/{sid}/{out_name}__imp_{digest}_strip.jpg"
            safe_link_or_copy(s_found, ctx.renders_dir / vault_rel_strip)

        _clear_scene_trim_cache(ctx, sid)
        rev_strip = ctx.strips_dir / f"review_{sid}.jpg"
        if rev_strip.is_file():
            try:
                rev_strip.unlink()
            except OSError:
                pass

        # Update state_hashes so Studio recognizes this scene as up-to-date and stitch-ready
        used_aids: set[str] = set()
        anchor_sha = None
        for ak in (sc.get("anchor"), sc.get("anchor_end")):
            if ak:
                aid, apath = ctx.resolve_anchor_path(ak)
                if apath.is_file():
                    hval = _file_sha256(apath)
                    state.setdefault("anchor_file_hashes", {})[aid] = hval
                    used_aids.add(aid)
                    if anchor_sha is None and ak == sc.get("anchor"):
                        anchor_sha = hval

        if cur_hash:
            state.setdefault("tasks", {})[out_name] = cur_hash
            state.setdefault("task_hashes", {})[out_name] = cur_hash
        state.setdefault("scene_renders", {})[out_name] = {
            "render_start": round(req_start, 3),
            "render_end": round(req_end, 3),
            "frames": actual_frames,
            "elapsed": 0.0,
            "completed_at": round(now_ts, 3),
            "imported": True,
            "source_file": disp_name,
        }
        try:
            a_p = Path(ctx.resolve_anchor_path(sc.get("anchor", ""))[1])
            au_p = ctx.project_dir / "audio" / "master.wav"
            dummy_task = ctx.make_video_task(out_name, sc.get("prompt", ""), actual_frames, int(sc.get("seed") or 1000), a_p, au_p, vrspec, sc=sc, pass_num=target_pass, resolution=res_str)
            state.setdefault("task_settings", {})[out_name] = render_settings_of(dummy_task)
            state.setdefault("settings_store", {})[out_name] = dict(state["task_settings"][out_name])
        except Exception:
            pass
        ctx.save_state_hashes(state, updated_keys={out_name}, updated_anchor_ids=used_aids)

        from .media_utils import locked_file_op
        with locked_file_op(get_takes_manifest_path(ctx.project_dir).with_suffix(".lock")):
            manifest = load_takes_manifest(ctx.project_dir)
            sc_entry = manifest.setdefault("scenes", {}).setdefault(sid, {"active_takes": {}, "takes": []})
            existing = sc_entry.setdefault("takes", [])
            num = len(existing) + 1
            tag_label = clean_label or f"Take {num} (Import: {disp_name})"
            iso = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).isoformat()
            r_rel = path_rel_str(ctx.project_dir, ctx.renders_dir, "work/renders")
            take_entry = {
                "take_id": take_id,
                "take_number": num,
                "scene_id": sid,
                "pass_num": target_pass,
                "out_name": out_name,
                "task_hash": cur_hash,
                "seed": sc.get("seed"),
                "prompt": sc.get("prompt"),
                "anchor_id": sc.get("anchor"),
                "anchor_sha256": anchor_sha,
                "created_at": round(now_ts, 3),
                "created_iso": iso,
                "vault_rel_mp4": vault_rel_mp4,
                "vault_rel_strip": vault_rel_strip,
                "file_path": f"{r_rel}/{vault_rel_mp4}",
                "strip_path": f"{r_rel}/{vault_rel_strip}" if vault_rel_strip else None,
                "render_start": round(req_start, 3),
                "render_end": round(req_end, 3),
                "frames": actual_frames,
                "elapsed_s": 0.0,
                "size_bytes": vault_mp4.stat().st_size,
                "resolution": actual_res,
                "output_resolution": actual_res,
                "model_id": str(vr_id),
                "model_type": "external:import",
                "backend": backend_tag,
                "source_file": disp_name,
                "imported": True,
                "scene_snapshot": copy.deepcopy(sc),
                "label": tag_label,
            }
            existing.append(take_entry)
            sc_entry.setdefault("active_takes", {})[_pass_key(target_pass)] = take_id
            sc_entry["active_take_id"] = take_id
            save_takes_manifest(ctx.project_dir, manifest)

        _clear_queue_override_for_stem(ctx, out_name)
        print(f"[Import Clip] {sid} <- {disp_name} ({actual_res}, {actual_dur:.2f}s, {actual_frames}f) -> {out_mp4.name}", flush=True)
        return {
            "success": True,
            "scene_id": sid,
            "out_name": out_name,
            "active_mp4": str(out_mp4),
            "take": take_entry,
            "duration_s": round(actual_dur, 3),
            "needed_duration_s": round(needed_dur, 3),
            "resolution": actual_res,
            "normalized": bool(normalize),
            "warnings": warnings,
        }
    finally:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass


def run_import_clip_cli(args: list[str]) -> bool:
    """Handles CLI `python work/run_overnight_v3.py <config_or_slug> import-clip <scene_id> <video_path> [--label ...] [--pass 1|2] [--no-normalize]`."""
    idx = next((i for i, a in enumerate(args) if a.lower() in ("import-clip", "--import-clip")), None)
    if idx is None:
        return False

    from .cli import resolve_project_config

    pre = [a for i, a in enumerate(args) if i != idx]
    label, pass_num, normalize = None, None, True
    pos: list[str] = []
    i = 0
    while i < len(pre):
        tok = pre[i]
        if tok == "--label" and i + 1 < len(pre):
            label = pre[i + 1]; i += 2; continue
        if tok.startswith("--label="):
            label = tok.split("=", 1)[1]; i += 1; continue
        if tok == "--pass" and i + 1 < len(pre):
            pass_num = int(pre[i + 1]); i += 2; continue
        if tok.startswith("--pass="):
            pass_num = int(tok.split("=", 1)[1]); i += 1; continue
        if tok == "--no-normalize":
            normalize = False; i += 1; continue
        if not tok.startswith("-"):
            pos.append(tok)
        i += 1

    if len(pos) >= 3:
        config_file = resolve_project_config(pos[0])
        scene_id, video_path = pos[1], pos[2]
    elif len(pos) == 2:
        config_file = resolve_project_config(None)
        scene_id, video_path = pos[0], pos[1]
    else:
        print("Usage: python work/run_overnight_v3.py <config_or_slug> import-clip <scene_id> <video_path> [--label \"...\"] [--pass 1|2] [--no-normalize]", file=sys.stderr)
        sys.exit(2)

    res = import_video_clip_as_take(
        config_file.parent, scene_id, Path(video_path),
        config_file=config_file, pass_num=pass_num, label=label, normalize=normalize,
    )
    for w in res.get("warnings") or []:
        print(f"[Import Clip Warning] {w}", flush=True)
    return True


def handle_import_clip_http(handler, srv, parsed, body: dict | None = None) -> None:
    """HTTP handler for `POST /api/takes/import` supporting raw binary uploads or local-path JSON."""
    qs = parse_qs(parsed.query)
    if body is not None:
        slug = str(body.get("project") or body.get("slug") or "").strip()
        sid = str(body.get("scene_id") or "").strip()
        raw_path = str(body.get("path") or body.get("video_path") or "").strip()
        label = body.get("label")
        pass_num = int(body["pass_num"]) if body.get("pass_num") in (1, 2, "1", "2") else None
        normalize = bool(body.get("normalize", True))
        if not slug or not sid or not raw_path:
            handler.send_error_json("project, scene_id, and path are required"); return
        try:
            from server_security import resolve_project_dir_safe
        except ImportError:
            from work.ui.server_security import resolve_project_dir_safe
        pdir = resolve_project_dir_safe(srv.PROJECTS_DIR, slug)
        if not pdir or not pdir.is_dir():
            handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND); return
        src = Path(raw_path)
        if not src.is_absolute():
            src = (pdir / raw_path).resolve()
        try:
            res = import_video_clip_as_take(pdir, sid, src, pass_num=pass_num, label=label, normalize=normalize)
            srv.compute_fast_project_queue(slug, force_recheck=True)
            handler.send_json(res)
        except (FileNotFoundError, ValueError) as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler.send_error_json(f"Import clip failed: {exc}", status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return

    # Raw binary stream upload (?project=...&scene_id=...&filename=...&label=...&normalize=1)
    slug = unquote((qs.get("project") or qs.get("slug") or [""])[0]).strip()
    sid = unquote((qs.get("scene_id") or [""])[0]).strip()
    filename = Path(unquote((qs.get("filename") or ["imported_clip.mp4"])[0]).strip() or "imported_clip.mp4").name
    label = unquote((qs.get("label") or [""])[0]).strip() or None
    norm_raw = str((qs.get("normalize") or ["1"])[0]).strip().lower()
    normalize = norm_raw not in ("0", "false", "no")
    p_raw = (qs.get("pass_num") or [None])[0]
    pass_num = int(p_raw) if str(p_raw) in ("1", "2") else None

    if not slug or not sid:
        handler.send_error_json("project and scene_id query params are required"); return
    ext = Path(filename).suffix.lower() or ".mp4"
    if ext not in _ALLOWED_VIDEO_EXTS:
        handler.send_error_json(f"Unsupported video extension '{ext}' (allowed: {', '.join(sorted(_ALLOWED_VIDEO_EXTS))})"); return

    try:
        from server_security import resolve_project_dir_safe
    except ImportError:
        from work.ui.server_security import resolve_project_dir_safe
    pdir = resolve_project_dir_safe(srv.PROJECTS_DIR, slug)
    if not pdir or not pdir.is_dir():
        handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND); return

    raw_len = handler.headers.get("Content-Length")
    try:
        length = int(str(raw_len or "0").strip())
    except ValueError:
        handler.send_error_json("Invalid Content-Length header"); return
    if length <= 0 or length > MAX_HTTP_IMPORT_BYTES:
        handler.send_error_json(f"Upload size {length} invalid or exceeds 512 MiB limit"); return

    tmp_up: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mvg_import_", suffix=ext, delete=False) as tf:
            tmp_up = Path(tf.name)
            rem = length
            while rem > 0:
                chunk = handler.rfile.read(min(1024 * 1024, rem))
                if not chunk:
                    raise ValueError("Truncated upload stream")
                tf.write(chunk)
                rem -= len(chunk)
        res = import_video_clip_as_take(
            pdir, sid, tmp_up, pass_num=pass_num, label=label, normalize=normalize, source_name=filename,
        )
        srv.compute_fast_project_queue(slug, force_recheck=True)
        handler.send_json(res)
    except ValueError as exc:
        handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
    except Exception as exc:
        handler.send_error_json(f"Import clip failed: {exc}", status=HTTPStatus.INTERNAL_SERVER_ERROR)
    finally:
        if tmp_up is not None:
            tmp_up.unlink(missing_ok=True)
