"""Direct MiniMax Online API (platform.minimax.io / api.minimax.io) engine session for Stage 2 MiniMax-H3 video generation."""

from __future__ import annotations

import json
import math
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .fal_bridge import (
    _FAL_UA,
    FalJobHandle,
    FalJobResult,
    _adapt_prompt_for_fal,
    _retry_call,
    _urlopen_read,
    adapt_duet_audio_prompt,
    composite_duet_roi_pass2,
    push_cloud_status,
    resolve_duet_cloud_audio_paths,
    run_chunked_video_sequence,
)
from .fal_catalog import read_local_config, resolve_minimax_key, resolve_minimax_route
from .media_utils import _file_sha256, atomic_write_json, locked_file_op, probe_video_frames

_MINIMAX_CACHE_TTL_S = 6 * 86400  # MiniMax Files API keeps assets for 7 days


def minimax_h3_target_duration(req_dur_int: int, fps: int = 24) -> tuple[int, float]:
    """Return MiniMax-H3's discrete frame count (5 + 17*k) and duration in seconds."""
    dur_i = max(4, min(15, int(req_dur_int)))
    fps_i = max(1, int(round(float(fps or 24))))
    min_frames = dur_i * fps_i
    k = max(0, math.ceil((min_frames - 5) / 17.0))
    target_frames = 5 + 17 * k
    return target_frames, target_frames / float(fps_i)


def ensure_padded_reference_audio(audio_path: str | Path, target_dur_s: float) -> str:
    """Pad a reference WAV with tail digital silence to match MiniMax-H3's output duration.

    MiniMax Online stretches any reference_audio shorter than the generated integer-second
    clip (`5 + 17*k` frames @ 24fps), shifting lip-sync progressively out of time.
    Writes the padded copy inside a subfolder preserving `.name` and `.stem`.
    """
    p = Path(audio_path)
    if not p.is_file() or p.suffix.lower() != ".wav" or target_dur_s <= 0:
        return str(audio_path)
    try:
        with wave.open(str(p), "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            comptype = wf.getcomptype()
            if framerate <= 0 or comptype != "NONE":
                return str(audio_path)
            cur_dur = nframes / float(framerate)
            if cur_dur >= float(target_dur_s) - 0.002:
                return str(audio_path)
            target_samples = max(nframes, int(math.ceil(float(target_dur_s) * framerate)))
            pad_samples = target_samples - nframes
            if pad_samples <= 0:
                return str(audio_path)
            raw_frames = wf.readframes(nframes)
    except (wave.Error, OSError):
        return str(audio_path)

    pad_dir = p.parent / f".mm_pad_{int(round(float(target_dur_s) * 1000))}ms"
    pad_path = pad_dir / p.name
    try:
        if pad_path.is_file() and pad_path.stat().st_mtime >= p.stat().st_mtime:
            with wave.open(str(pad_path), "rb") as pwf:
                if pwf.getnframes() >= target_samples and pwf.getframerate() == framerate:
                    return str(pad_path)
    except (wave.Error, OSError):
        pass

    pad_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = pad_dir / f".tmp_{p.name}"
    silence_byte = b"\x80" if sampwidth == 1 else b"\x00"
    silence_bytes = silence_byte * (pad_samples * nchannels * sampwidth)
    try:
        with wave.open(str(tmp_path), "wb") as out_wf:
            out_wf.setnchannels(nchannels)
            out_wf.setsampwidth(sampwidth)
            out_wf.setframerate(framerate)
            out_wf.writeframes(raw_frames + silence_bytes)
        os.replace(str(tmp_path), str(pad_path))
        print(
            f"  [MiniMax API] Padded reference_audio {p.name}: {cur_dur:.3f}s -> "
            f"{target_samples / float(framerate):.3f}s (tail silence, prevents audio stretch)",
            flush=True,
        )
        return str(pad_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def trim_minimax_output_to_task(
    ctx: Any,
    out_path: str | Path,
    task: dict,
    dur_s: float,
    win_cnt: int = 1,
) -> Path:
    """Losslessly trim over-generated MiniMax frames back to task frame count with clean audio."""
    out = Path(out_path)
    if not out.is_file():
        return out
    ffmpeg_raw = getattr(ctx, "ffmpeg", None) or shutil.which("ffmpeg")
    if not ffmpeg_raw:
        return out
    ffmpeg_path = Path(ffmpeg_raw)

    fps = float(getattr(ctx, "fps", 0) or 24)
    if int(win_cnt or 1) == 1:
        target_frames = int(task.get("video_length") or 0) or int(round(float(dur_s or 0) * fps))
    else:
        target_frames = int(round(float(dur_s or 0) * fps))
    if target_frames <= 0:
        return out

    actual_frames = int(probe_video_frames(ffmpeg_path, out) or 0)
    if actual_frames <= 0 or actual_frames <= target_frames:
        return out

    audio_guide = str(task.get("audio_guide") or "")
    has_guide = bool(audio_guide and Path(audio_guide).is_file())
    tmp = out.with_name(f".mm_trim_{out.name}")
    cmd = [str(ffmpeg_path), "-y", "-v", "error", "-i", str(out)]
    if has_guide:
        cmd.extend(["-i", audio_guide, "-map", "0:v:0", "-map", "1:a:0"])
    else:
        cmd.extend(["-map", "0:v:0", "-map", "0:a?"])
    cmd.extend(["-frames:v", str(target_frames), "-c:v", "copy"])
    if has_guide:
        cmd.extend(["-c:a", "aac", "-ar", "44100", "-ac", "2"])
    else:
        cmd.extend(["-c:a", "copy"])
    cmd.extend(["-movflags", "+faststart", str(tmp)])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            os.replace(str(tmp), str(out))
            print(
                f"  [MiniMax API] Trimmed {out.name}: {actual_frames} -> {target_frames} frames "
                f"({target_frames / fps:.3f}s){' + clean audio_guide' if has_guide else ''}",
                flush=True,
            )
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return out


def _encode_multipart(field_name: str, file_path: Path, extra_fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----MVGStudioBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for k, v in extra_fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )
    ctype = mimetypes.guess_type(file_path.name)[0] or {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".mp4": "video/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg",
    }.get(file_path.suffix.lower(), "application/octet-stream")
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"{file_path.name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        + file_path.read_bytes()
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _extract_minimax_error(resp: dict, context: str = "") -> None:
    base = resp.get("base_resp")
    if isinstance(base, dict):
        code = int(base.get("status_code", 0) or 0)
        if code != 0:
            msg = str(base.get("status_msg") or "Unknown error")
            raise RuntimeError(f"MiniMax API error {code}{f' ({context})' if context else ''}: {msg}")


class MiniMaxSession:
    """Executes Stage 2 video tasks natively on api.minimax.io/v2/video_generation, duck-typing WanGPSession."""

    def __init__(self, ctx, base_url: str = "https://api.minimax.io"):
        self.ctx = ctx
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self.last_request_id = ""
        self.last_endpoint = ""
        self._cache_path = Path(ctx.jobs_dir) / "minimax_upload_cache.json" if ctx and hasattr(ctx, "jobs_dir") else None
        self._upload_cache: dict[str, dict[str, Any]] = self._load_cache()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if self._cache_path and self._cache_path.is_file():
            try:
                with locked_file_op(self._cache_path.with_suffix(".lock")):
                    d = json.loads(self._cache_path.read_text(encoding="utf-8"))
                    return d if isinstance(d, dict) else {}
            except Exception:
                pass
        return {}

    def _save_cache(self):
        if self._cache_path:
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                with locked_file_op(self._cache_path.with_suffix(".lock")):
                    disk = {}
                    if self._cache_path.is_file():
                        try:
                            loaded = json.loads(self._cache_path.read_text(encoding="utf-8"))
                            if isinstance(loaded, dict):
                                disk = loaded
                        except Exception:
                            pass
                    now_c = time.time()
                    disk = {k: v for k, v in disk.items() if isinstance(v, dict) and (now_c - float(v.get("uploaded_at", 0))) < _MINIMAX_CACHE_TTL_S}
                    for k, v in self._upload_cache.items():
                        if isinstance(v, dict) and (now_c - float(v.get("uploaded_at", 0))) < _MINIMAX_CACHE_TTL_S:
                            disk[k] = v
                    self._upload_cache = disk
                    atomic_write_json(self._cache_path, disk)
            except Exception:
                pass

    def _get_key(self) -> str:
        key, _ = resolve_minimax_key(getattr(self.ctx, "project_dir", None))
        if not key:
            raise RuntimeError(
                "MiniMax API key not configured. Set MINIMAX_API_KEY env var or save it in Studio Settings (encrypted via Windows DPAPI)."
            )
        return key

    def _json_req(self, path_or_url: str, method: str = "GET", payload: dict | None = None) -> dict:
        key = self._get_key()
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": _FAL_UA,
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        raw = _urlopen_read(req, timeout=60).decode("utf-8", errors="replace")
        res = json.loads(raw) if raw.strip() else {}
        if isinstance(res, dict):
            _extract_minimax_error(res, url)
        return res

    def upload_media(self, local_path: str | Path) -> str:
        s = str(local_path or "").strip()
        if not s:
            return ""
        if s.startswith(("http://", "https://", "mm_file://", "data:")):
            return s
        p = Path(s)
        if not p.is_file():
            raise FileNotFoundError(f"MiniMax input file missing: {p}")
        cache_key = f"{_file_sha256(p)}:{p.suffix.lower()}"
        now = time.time()
        with self._lock:
            hit = self._upload_cache.get(cache_key)
            if isinstance(hit, dict) and hit.get("url") and (now - float(hit.get("uploaded_at", 0)) < _MINIMAX_CACHE_TTL_S):
                return str(hit["url"])
        if self._cache_path and self._cache_path.is_file():
            disk = self._load_cache()
            with self._lock:
                self._upload_cache.update(disk)
                hit = self._upload_cache.get(cache_key)
                if isinstance(hit, dict) and hit.get("url") and (now - float(hit.get("uploaded_at", 0)) < _MINIMAX_CACHE_TTL_S):
                    return str(hit["url"])

        key = self._get_key()
        body, ctype = _encode_multipart("file", p, {"purpose": "video_generation_input"})
        last_err: Exception | None = None
        resp_data: dict | None = None
        for up_path in ("/v2/files", "/v1/files/upload"):
            req = urllib.request.Request(
                f"{self.base_url}{up_path}", data=body, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": ctype, "User-Agent": _FAL_UA},
            )
            try:
                raw = _retry_call(lambda: _urlopen_read(req, timeout=180).decode("utf-8", errors="replace"))
                resp_data = json.loads(raw) if raw.strip() else {}
                _extract_minimax_error(resp_data, up_path)
                break
            except Exception as exc:
                last_err = exc
                if "HTTP 404" in str(exc):
                    continue
                raise
        if not resp_data:
            raise last_err or RuntimeError("MiniMax file upload failed")

        file_id = (
            resp_data.get("file_id")
            or (resp_data.get("file") or {}).get("file_id")
            or (resp_data.get("data") or {}).get("file_id")
        )
        if not file_id:
            raise RuntimeError(f"MiniMax file upload response missing file_id: {resp_data!r}")
        mm_url = f"mm_file://{file_id}"
        with self._lock:
            self._upload_cache[cache_key] = {"url": mm_url, "file_id": str(file_id), "uploaded_at": round(now, 1)}
            self._save_cache()
        return mm_url

    def _build_payload(self, route: dict, task: dict, dur_s: float) -> dict[str, Any]:
        req_dur_int = max(4, min(15, int(round(dur_s))))
        _, mm_target_dur_s = minimax_h3_target_duration(
            req_dur_int,
            fps=int(round(float(getattr(self.ctx, "fps", 0) or 24))),
        )
        prompt = _adapt_prompt_for_fal(task.get("prompt", ""), use_image_n=True) or "Smooth natural motion matching the scene."
        audio_paths, is_dual_audio = resolve_duet_cloud_audio_paths(task, dur_s)
        prompt = adapt_duet_audio_prompt(prompt, is_dual_audio)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

        img_start = str(task.get("image_start") or "")
        has_start = bool(img_start and Path(img_start).is_file())
        img_end = str(task.get("image_end") or "")
        has_end = bool(img_end and Path(img_end).is_file())
        has_audio = bool(audio_paths)

        seen_paths = {img_start, img_end}
        extra_refs: list[str] = []
        for r in (task.get("image_refs") or []):
            rp = str(r or "")
            if not rp or rp in seen_paths or not Path(rp).is_file():
                continue
            seen_paths.add(rp)
            extra_refs.append(rp)

        # MiniMax /v2/video_generation strictly forbids mixing frame roles
        # (first_frame/last_frame) with reference roles (reference_image/reference_audio).
        use_reference_mode = bool((has_audio or extra_refs) and not has_end)
        if use_reference_mode:
            ref_count = 0
            if has_start:
                content.append({"type": "image_url", "image_url": {"url": self.upload_media(img_start)}, "role": "reference_image"})
                ref_count += 1
            for rp in extra_refs:
                if ref_count >= 8:
                    break
                content.append({"type": "image_url", "image_url": {"url": self.upload_media(rp)}, "role": "reference_image"})
                ref_count += 1
            for ap in audio_paths:
                padded_ap = ensure_padded_reference_audio(ap, mm_target_dur_s)
                content.append({"type": "audio_url", "audio_url": {"url": self.upload_media(padded_ap)}, "role": "reference_audio"})
            ratio = "adaptive" if route.get("adapter") == "duet_pass2_roi_minimax" else "16:9"
        else:
            if has_end:
                import re as _re
                prompt = _re.sub(r"\(?\s*(?:<\s*Subject\s+\d+\s*>|Subject\s+\d+)\s*\)?", "", prompt, flags=_re.IGNORECASE)
                prompt = _re.sub(r"(?:and\s+)?\bImage\s+(?:[2-9]|\d{2,})\b(?:\s+as\s+reference)?", "", prompt, flags=_re.IGNORECASE).strip()
                content[0]["text"] = prompt
            if has_end and has_audio:
                print(
                    "  [MiniMax API] Note: Scene uses start+end frame interpolation; omitting reference_audio "
                    "(MiniMax V2 cannot mix first_frame/last_frame with reference_audio).",
                    flush=True,
                )
            if has_start:
                content.append({"type": "image_url", "image_url": {"url": self.upload_media(img_start)}, "role": "first_frame"})
            if has_end:
                content.append({"type": "image_url", "image_url": {"url": self.upload_media(img_end)}, "role": "last_frame"})
            ratio = "adaptive"

        return {
            "model": route.get("minimax_model", "MiniMax-H3"),
            "content": content,
            "resolution": route.get("target_resolution", "768P"),
            "duration": req_dur_int,
            "ratio": ratio,
            "aigc_watermark": False,
        }

    def _poll_until_done(self, task_id: str, handle: FalJobHandle, win_idx: int, win_cnt: int) -> str:
        cfg_local = read_local_config(getattr(self.ctx, "project_dir", None))
        timeout_s = max(900.0, float(cfg_local.get("fal_timeout_s", 900.0)))
        t0 = time.time()
        proc_t0: float | None = None
        last_q_tick = -1
        last_p_tick = -1
        while not handle.cancelled:
            if time.time() - t0 > timeout_s:
                raise TimeoutError(f"MiniMax job {task_id} timed out after {timeout_s}s")
            try:
                st = _retry_call(lambda: self._json_req(f"/v2/query/video_generation/{task_id}"))
            except Exception as exc:
                if "HTTP 404" in str(exc):
                    st = _retry_call(lambda: self._json_req(f"/v2/query/video_generation?task_id={task_id}"))
                else:
                    raise
            t_obj = st.get("task") if isinstance(st.get("task"), dict) else st
            status = str(t_obj.get("status") or st.get("status") or "").strip().lower()
            if status in ("prepared", "preparing", "queueing", "queued"):
                q_e = max(0.0, time.time() - t0)
                qt = int(q_e // 4)
                if qt > last_q_tick:
                    last_q_tick = qt
                    push_cloud_status(handle, "queued", f"MiniMax: {status} ({int(q_e)}s)", real_source=t_obj, win_idx=win_idx, win_cnt=win_cnt)
            elif status in ("succeeded", "success", "completed"):
                content_obj = t_obj.get("content") or st.get("content") or {}
                url = (content_obj.get("url") if isinstance(content_obj, dict) else None) or st.get("file_url") or t_obj.get("file_url")
                if url:
                    return str(url)
                fid = t_obj.get("file_id") or st.get("file_id")
                if fid:
                    meta = _retry_call(lambda: self._json_req(f"/v1/files/retrieve?file_id={fid}"))
                    dl = (meta.get("file") or {}).get("download_url") or meta.get("download_url")
                    if dl:
                        return str(dl)
                raise RuntimeError(f"MiniMax task {task_id} succeeded without output URL: {st!r}")
            elif status in ("failed", "fail", "error", "cancelled", "canceled"):
                raise RuntimeError(str(t_obj.get("error") or st.get("error") or f"MiniMax task {task_id} {status}"))
            else:
                if proc_t0 is None:
                    proc_t0 = time.time()
                proc_e = max(0.0, time.time() - proc_t0)
                pt = int(proc_e // 4)
                if pt > last_p_tick:
                    last_p_tick = pt
                    st_lbl = status or "processing"
                    lbl = f"MiniMax: {st_lbl} ({int(proc_e)}s)" if proc_e >= 2 else f"MiniMax: {st_lbl}"
                    push_cloud_status(handle, "Denoising", lbl, real_source=t_obj, win_idx=win_idx, win_cnt=win_cnt)
            time.sleep(2.0)
        raise RuntimeError("Cancelled")

    def _run_one_sync(self, route: dict, task: dict, dur_s: float, out_path: Path, handle: FalJobHandle, win_idx: int = 1, win_cnt: int = 1) -> Path:
        if handle.cancelled:
            raise RuntimeError("Cancelled")
        model_id = route.get("minimax_model", "MiniMax-H3")
        self.last_endpoint = f"api.minimax.io/v2/{model_id}"
        push_cloud_status(handle, "uploading", f"MiniMax: submitting ({model_id})...", win_idx=win_idx, win_cnt=win_cnt)
        payload = self._build_payload(route, task, dur_s)
        sub_resp = _retry_call(lambda: self._json_req("/v2/video_generation", method="POST", payload=payload))
        task_id = str(sub_resp.get("task_id") or (sub_resp.get("task") or {}).get("id") or "")
        if not task_id:
            raise RuntimeError(f"MiniMax /v2/video_generation missing task_id: {sub_resp!r}")
        self.last_request_id = task_id

        dl_url = self._poll_until_done(task_id, handle, win_idx, win_cnt)
        if handle.cancelled:
            raise RuntimeError("Cancelled")
        push_cloud_status(handle, "downloading", "MiniMax: downloading output...", win_idx=win_idx, win_cnt=win_cnt)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(f".mm_dl_{out_path.name}")
        def _dl():
            req = urllib.request.Request(dl_url, headers={"User-Agent": _FAL_UA})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as fh:
                try:
                    shutil.copyfileobj(resp, fh)
                except TypeError:
                    data = resp.read()
                    if data:
                        fh.write(data)
        try:
            _retry_call(_dl)
            os.replace(str(tmp), str(out_path))
            trim_minimax_output_to_task(self.ctx, out_path, task, dur_s, win_cnt=win_cnt)
            return out_path
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _run_duet_pass2(self, task_cfg: dict, route: dict, out_path: Path, handle: FalJobHandle, dur_s: float) -> Path:
        ffmpeg = getattr(self.ctx, "ffmpeg", Path("ffmpeg"))
        fps = float(getattr(self.ctx, "fps", 24) or 24)
        max_s = float(route.get("max_duration_s", 15.0) or 15.0)
        import re as _re
        clean_prompt = _re.sub(r"\(?\s*<\s*Subject\s+\d+\s*>\s*\)?", "", str(task_cfg.get("prompt") or ""), flags=_re.IGNORECASE).strip()
        def _render_crop(crop_in: Path, audio_p: Path, crop_out: Path, cw: int, ch: int):
            def _one(st_task, cd, cp, wi, wc):
                first_jpg = cp.with_name(f".mm_roi_first_{cp.stem}_{wi}.jpg")
                try:
                    subprocess.run([str(ffmpeg), "-y", "-i", str(crop_in), "-vframes", "1", "-q:v", "2", str(first_jpg)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    sub = dict(st_task, prompt=clean_prompt, image_start=str(first_jpg), image_end="", image_refs=[], audio_guide=str(st_task.get("audio_guide") or audio_p), resolution=f"{cw}x{ch}")
                    self._run_one_sync(route, sub, cd, cp, handle, win_idx=wi, win_cnt=wc)
                finally:
                    first_jpg.unlink(missing_ok=True)
            if dur_s <= max_s + 0.5:
                _one(dict(task_cfg, audio_guide=str(audio_p)), dur_s, crop_out, 1, 1)
            else:
                run_chunked_video_sequence(
                    self.ctx, dict(task_cfg, audio_guide=str(audio_p)), crop_out, fps, dur_s, max_s, _one,
                    should_cancel_fn=lambda: handle.cancelled,
                )
        return composite_duet_roi_pass2(self.ctx, task_cfg, out_path, _render_crop)

    def submit_task(self, task_cfg: dict) -> FalJobHandle:
        handle = FalJobHandle()

        def _worker():
            try:
                spec = dict(task_cfg.get("_fal_spec") or {})
                route = spec if spec.get("minimax_model") else resolve_minimax_route(
                    task_cfg, sc=task_cfg.get("_fal_ctx"), resolution=str(task_cfg.get("resolution") or "1344x768"),
                )
                out_dir = Path(getattr(self.ctx, "renders_dir", Path(".")))
                out_name = str(task_cfg.get("output_filename") or f"minimax_out_{int(time.time())}")
                out_path = out_dir / (out_name if out_name.endswith(".mp4") else f"{out_name}.mp4")
                fps = float(getattr(self.ctx, "fps", 24) or 24)
                dur_s = max(4.0, float(task_cfg.get("video_length", 120)) / fps)

                if route.get("adapter") == "duet_pass2_roi_minimax":
                    self._run_duet_pass2(task_cfg, route, out_path, handle, dur_s)
                    handle.finish(FalJobResult(True, [str(out_path)], [], self.last_request_id, self.last_endpoint))
                    return

                max_s = float(route.get("max_duration_s", 15.0) or 15.0)
                run_chunked_video_sequence(
                    self.ctx, task_cfg, out_path, fps, dur_s, max_s,
                    lambda st, cd, cp, wi, wc: self._run_one_sync(route, st, cd, cp, handle, win_idx=wi, win_cnt=wc),
                    should_cancel_fn=lambda: handle.cancelled,
                )
                handle.finish(FalJobResult(True, [str(out_path)], [], self.last_request_id, self.last_endpoint))
            except Exception as exc:
                err_msg = str(exc)
                handle.events.push_error(err_msg)
                handle.finish(FalJobResult(False, [], [SimpleNamespace(message=err_msg)], self.last_request_id, self.last_endpoint))

        threading.Thread(target=_worker, daemon=True).start()
        return handle

    def get_default_settings(self, model_type: str = "") -> dict:
        return {"model_type": model_type, "custom_settings": {}}

    def close(self):
        with self._lock:
            self._save_cache()
