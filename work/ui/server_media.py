"""
Media streaming and HTTP range request handler for Music Video Generation Studio.
Serves audio stems, anchor portraits, scene preview clips, and progressive masters.
"""

import json
import mimetypes
import re
import shutil
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs


def _srv():
    mod = sys.modules.get("work.ui.server") or sys.modules.get("server")
    if mod is not None:
        return mod
    try:
        from work.ui import server
        return server
    except ImportError:
        import server
        return server


def stream_file_with_range(handler, file_path: Path):
    """Streams a file over HTTP supporting 206 Partial Content range requests."""
    file_size = file_path.stat().st_size
    mime_type, _ = mimetypes.guess_type(str(file_path))
    mime_type = mime_type or "application/octet-stream"

    range_header = handler.headers.get("Range")
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            handler.send_response(HTTPStatus.PARTIAL_CONTENT)
            handler.send_header("Content-Type", mime_type)
            handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            handler.send_header("Content-Length", str(length))
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            handler.end_headers()

            try:
                with open(file_path, "rb") as f:
                    f.seek(start)
                    bytes_remaining = length
                    chunk_size = 64 * 1024
                    while bytes_remaining > 0:
                        chunk = f.read(min(chunk_size, bytes_remaining))
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        bytes_remaining -= len(chunk)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass
            return

    # Normal full file transfer
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Content-Length", str(file_size))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.end_headers()

    try:
        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, handler.wfile, length=64 * 1024)
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        pass


def handle_media_stream(handler, subpath: str):
    """Resolves and serves media streams (audio, anchor, clips, master) for a project."""
    srv = _srv()
    subpath = unquote(subpath)
    parts = subpath.strip("/").split("/")
    if len(parts) < 2:
        handler.send_error_json("Invalid media path", status=HTTPStatus.NOT_FOUND)
        return

    slug = parts[0]
    proj_dir = srv.PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND)
        return

    target_file: Path | None = None
    category = parts[1]

    parsed_url = urlparse(getattr(handler, "path", ""))
    qs = parse_qs(parsed_url.query)
    req_take_id = qs.get("take", [None])[0]

    if req_take_id and category in ("clip", "strip"):
        manifest_file = proj_dir / "work" / "takes_manifest.json"
        if manifest_file.is_file():
            try:
                m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                for s_data in m_data.get("scenes", {}).values():
                    for t in s_data.get("takes", []):
                        if t.get("take_id") == req_take_id:
                            rel_val = t.get("file_path" if category == "clip" else "strip_path")
                            if rel_val:
                                cand = proj_dir / rel_val
                                if cand.is_file():
                                    target_file = cand
                                    break
                            v_rel = t.get("vault_rel_mp4" if category == "clip" else "vault_rel_strip")
                            if v_rel:
                                cand = proj_dir / "work" / "renders_v3" / v_rel
                                if cand.is_file():
                                    target_file = cand
                                    break
                    if target_file is not None:
                        break
            except Exception:
                pass
        if target_file is None:
            takes_root = proj_dir / "work" / "renders_v3" / "takes"
            if takes_root.is_dir():
                cand_name = f"{req_take_id}.mp4" if category == "clip" else f"{req_take_id}_strip.jpg"
                for cand in takes_root.rglob(cand_name):
                    if cand.is_file():
                        target_file = cand
                        break

    if target_file is not None:
        pass
    elif category == "audio" and len(parts) >= 3:
        rel = "/".join(parts[2:])
        target_file = proj_dir / "audio" / rel
        if not target_file.exists():
            target_file = proj_dir / rel
    elif category == "ref" and len(parts) >= 3:
        rel_target = "/".join(parts[2:])
        cand = (proj_dir / rel_target).resolve()
        try:
            cand.relative_to(proj_dir.resolve())
            if cand.is_file():
                target_file = cand
        except ValueError:
            pass
        if target_file is None:
            for bdir in [proj_dir / "references", proj_dir / "work" / "ref_sheets", proj_dir]:
                c2 = (bdir / rel_target).resolve()
                try:
                    c2.relative_to(proj_dir.resolve())
                    if c2.is_file():
                        target_file = c2
                        break
                except ValueError:
                    pass
    elif category == "anchor" and len(parts) >= 3:
        raw_target = "/".join(parts[2:])
        rel_cand = (proj_dir / raw_target).resolve()
        try:
            rel_cand.relative_to(proj_dir.resolve())
            if rel_cand.is_file():
                target_file = rel_cand
        except ValueError:
            pass
        filename = Path(raw_target).name
        aid = srv.extract_anchor_id(filename)

        cfg_anchors_dir = None
        jsons = sorted(proj_dir.glob("*.json"))
        if jsons:
            try:
                pcfg = json.loads(jsons[-1].read_text(encoding="utf-8")).get("project", {})
                dpath = pcfg.get("directories", {}).get("anchors")
                if dpath:
                    cfg_anchors_dir = proj_dir / dpath
            except Exception:
                pass

        primary_anchor_dirs = [d for d in [cfg_anchors_dir, proj_dir / "work" / "anchors_v3", proj_dir / "work" / "anchors"] if d and d.is_dir()]
        ref_dirs = [d for d in [proj_dir / "references", proj_dir / "work" / "ref_sheets"] if d and d.is_dir()]
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}

        if aid:
            for cdir in primary_anchor_dirs:
                for img in sorted(cdir.iterdir()):
                    if img.is_file() and any(img.name.lower().endswith(ext) for ext in image_exts):
                        if srv.extract_anchor_id(img.name) == aid:
                            target_file = img
                            break
                if target_file is not None:
                    break

            if target_file is None:
                for cdir in ref_dirs:
                    for img in sorted(cdir.iterdir()):
                        if img.is_file() and any(img.name.lower().endswith(ext) for ext in image_exts):
                            if srv.extract_anchor_id(img.name) == aid:
                                target_file = img
                                break
                    if target_file is not None:
                        break

        if target_file is None:
            candidate_dirs = [
                proj_dir / "work" / "anchors_v3",
                proj_dir / "work" / "anchors",
                proj_dir / "references",
                proj_dir / "work" / "ref_sheets",
                proj_dir,
            ]
            for cdir in candidate_dirs:
                cand = cdir / filename
                if cand.is_file():
                    target_file = cand
                    break
                for ext in image_exts:
                    cand_ext = cdir / f"{Path(filename).stem}{ext}"
                    if cand_ext.is_file():
                        target_file = cand_ext
                        break
                if target_file is not None:
                    break

        if target_file is None and aid:
            for cand_dir in [proj_dir / "work" / "anchors_v3", proj_dir / "work" / "anchors"]:
                for ext in image_exts:
                    c = cand_dir / f"{aid}{ext}"
                    if c.is_file():
                        target_file = c
                        break
                if target_file is not None:
                    break
    elif category == "clip" and len(parts) >= 3:
        clip_name = Path("/".join(parts[2:])).name
        target_stem = Path(clip_name).stem

        cfg_trimmed_dir = None
        cfg_renders_dir = None
        jsons = sorted(proj_dir.glob("*.json"))
        if jsons:
            try:
                pcfg = json.loads(jsons[-1].read_text(encoding="utf-8")).get("project", {})
                dirs = pcfg.get("directories", {})
                if dirs.get("trimmed"):
                    cfg_trimmed_dir = proj_dir / dirs["trimmed"]
                if dirs.get("renders"):
                    cfg_renders_dir = proj_dir / dirs["renders"]
            except Exception:
                pass

        candidate_dirs = [
            cfg_trimmed_dir,
            cfg_renders_dir,
            proj_dir / "work" / "trimmed_v3",
            proj_dir / "work" / "renders_v3",
            proj_dir / "work" / "trimmed",
            proj_dir / "work" / "renders",
        ]
        candidate_dirs = [d for d in candidate_dirs if d and d.is_dir()]

        for cdir in candidate_dirs:
            cand = cdir / clip_name
            if cand.is_file():
                target_file = cand
                break

        if target_file is None:
            for cdir in candidate_dirs:
                if not cdir.is_dir():
                    continue
                for mp4 in cdir.glob("*.mp4"):
                    if mp4.stem == target_stem or mp4.stem.startswith(f"{target_stem}_"):
                        target_file = mp4
                        break
                if target_file is not None:
                    break

        if target_file is None:
            m_scene = re.match(r"^(S\d{1,2}[a-z]?)", target_stem, re.IGNORECASE)
            if m_scene:
                sid = m_scene.group(1).upper()
                for cdir in candidate_dirs:
                    if not cdir.is_dir():
                        continue
                    matching_clips = [
                        f for f in cdir.glob("*.mp4")
                        if srv.is_scene_match(sid, f.stem, sid)
                    ]
                    if matching_clips:
                        target_file = max(matching_clips, key=lambda f: (
                            50 if "pass2_final" in f.name.lower() else (
                                30 if "pass1" in f.name.lower() else (
                                    20 if "trimmed" in str(f).lower() else 10
                                )
                            ),
                            f.stat().st_mtime
                        ))
                        break
    elif category == "master":
        if len(parts) >= 3:
            master_name = "/".join(parts[2:])
            cand = proj_dir / master_name
            if cand.is_file():
                target_file = cand
            else:
                cand_work = proj_dir / "work" / master_name
                if cand_work.is_file():
                    target_file = cand_work
        if target_file is None:
            candidates = []
            for pattern in ("*Master*.mp4", "*master*.mp4"):
                for c in proj_dir.glob(pattern):
                    if not c.name.endswith(".tmp.mp4") and c.is_file():
                        candidates.append(c)
                for c in (proj_dir / "work").glob(pattern):
                    if not c.name.endswith(".tmp.mp4") and c.is_file():
                        candidates.append(c)
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                target_file = candidates[0]
    elif category == "strip" and len(parts) >= 3:
        raw_target = "/".join(parts[2:])
        raw_sid = Path(raw_target).stem.replace("_strip", "")

        cfg_strips_dir = None
        jsons = sorted(proj_dir.glob("*.json"))
        if jsons:
            try:
                pcfg = json.loads(jsons[-1].read_text(encoding="utf-8")).get("project", {})
                dpath = pcfg.get("directories", {}).get("review_strips")
                if dpath:
                    cfg_strips_dir = proj_dir / dpath
            except Exception:
                pass

        strips_dirs = [
            d for d in [
                cfg_strips_dir,
                proj_dir / "work" / "frames_v3",
                proj_dir / "work" / "frames",
                proj_dir / "work" / "strips_v3",
                proj_dir / "work" / "strips",
            ] if d and d.is_dir()
        ]
        if not strips_dirs:
            strips_dir = proj_dir / "work" / "frames_v3"
            strips_dir.mkdir(parents=True, exist_ok=True)
            strips_dirs = [strips_dir]
        else:
            strips_dir = strips_dirs[0]

        # 1. Search for existing strip file
        strip_file = None
        for sdir in strips_dirs:
            exact_cand = sdir / f"{raw_sid}_strip.jpg"
            if exact_cand.is_file():
                strip_file = exact_cand
                break
            for s in sdir.glob("*_strip.jpg"):
                if s.stem.replace("_strip", "").lower() == raw_sid.lower():
                    strip_file = s
                    break
            if strip_file:
                break

        # 2. Find best rendered clip for this scene
        candidate_clips = []
        for cdir in [
            proj_dir / "work" / "trimmed_v3",
            proj_dir / "work" / "trimmed",
            proj_dir / "work" / "renders_v3",
            proj_dir / "work" / "renders",
        ]:
            if cdir.is_dir():
                for c in cdir.glob("*.mp4"):
                    if c.name.endswith(".tmp.mp4"):
                        continue
                    if re.search(rf"(?:^|_)({re.escape(raw_sid)})(?:[_\.]|$)", c.name, re.IGNORECASE):
                        candidate_clips.append(c)

        matched_clip = None
        if candidate_clips:
            candidate_clips.sort(key=lambda p: (
                50 if "pass2_final" in p.name.lower() else (
                    45 if "pass2" in p.name.lower() else (
                        40 if "renders" in str(p).lower() and "pass1" not in p.name.lower() else (
                            30 if "pass1" in p.name.lower() else (
                                20 if "trimmed" in str(p).lower() else 10
                            )
                        )
                    )
                ),
                p.stat().st_mtime
            ), reverse=True)
            matched_clip = candidate_clips[0]

        # 3. Auto-generate strip if missing, outdated, or frame count doesn't match scene duration
        needs_regen = False
        target_n = 5
        dur = 5.0
        if matched_clip:
            ffprobe_bin = srv.WORKSPACE_ROOT / "tools" / "ffprobe.exe"
            if ffprobe_bin.exists():
                try:
                    cmd_probe = [
                        str(ffprobe_bin), "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        str(matched_clip)
                    ]
                    dur = float(subprocess.check_output(cmd_probe).decode().strip() or 5.0)
                except Exception:
                    dur = 5.0

            # Scale thumbnail count proportionally with scene length: 1 frame per second (min 2, max 30)
            target_n = max(2, min(30, round(dur / 1.0)))

            if not strip_file or not strip_file.exists():
                needs_regen = True
            elif matched_clip.stat().st_mtime > strip_file.stat().st_mtime:
                needs_regen = True
            elif ffprobe_bin.exists():
                # Re-generate legacy 5-frame strips to match exact scene duration
                try:
                    cmd_dim = [
                        str(ffprobe_bin), "-v", "error",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0",
                        str(strip_file)
                    ]
                    dim_str = subprocess.check_output(cmd_dim).decode().strip()
                    if dim_str:
                        dim_parts = dim_str.split(",")
                        if len(dim_parts) == 2 and dim_parts[0].isdigit() and dim_parts[1].isdigit():
                            w, h = int(dim_parts[0]), int(dim_parts[1])
                            if w >= 100 and h >= 50:
                                cur_n = max(1, round((w / max(1, h)) / (16.0 / 9.0)))
                                if cur_n != target_n:
                                    needs_regen = True
                except Exception:
                    pass

        if needs_regen and matched_clip:
            ffmpeg_bin = srv.WORKSPACE_ROOT / "tools" / "ffmpeg.exe"
            if ffmpeg_bin.exists():
                out_cand = strips_dir / f"{raw_sid}_strip.jpg"
                try:
                    r = target_n / max(dur, 0.5)
                    cmd = [
                        str(ffmpeg_bin), "-y", "-i", str(matched_clip),
                        "-vf", f"fps={r:.4f},scale=-1:144,tile={target_n}x1",
                        "-frames:v", "1", "-update", "1", "-q:v", "2", str(out_cand)
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=10)
                    if out_cand.exists():
                        strip_file = out_cand
                except Exception as e:
                    print(f"[Strip Regen Error] {e}")

        if strip_file and strip_file.exists():
            target_file = strip_file

    if target_file is None or not target_file.is_file():
        handler.send_error_json(f"File not found: {subpath}", status=HTTPStatus.NOT_FOUND)
        return

    stream_file_with_range(handler, target_file)
