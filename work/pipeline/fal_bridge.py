"""fal.ai cloud execution session duck-typing WanGPSession for Stage 1 anchors, Stage 2 video, and upscaling."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .fal_catalog import (
    FAL_UPSCALE_ROUTES,
    read_local_config,
    resolve_fal_key,
    resolve_fal_route,
)
from .media_utils import _file_sha256, atomic_write_json, locked_file_op, probe_video_dimensions, probe_video_fps


def _adapt_prompt_for_fal(prompt: str, use_image_n: bool = True) -> str:
    txt = str(prompt or "")
    if use_image_n:
        txt = re.sub(r"<\s*Picture\s+(\d+)\s*>", r"Image \1", txt, flags=re.IGNORECASE)
        txt = re.sub(r"<\s*Subject\s+(\d+)\s*>", r"Subject \1", txt, flags=re.IGNORECASE)
    return re.sub(r"\n{2,}", "\n", txt).strip()


def _wav_duration_s(p: Path, fallback_s: float) -> float:
    try:
        with wave.open(str(p), "rb") as wf:
            return wf.getnframes() / max(1, wf.getframerate())
    except Exception:
        return max(0.0, float(fallback_s or 0.0))


def resolve_duet_cloud_audio_paths(task: dict, dur_s: float) -> tuple[list[str], bool]:
    """Returns ([audio_guide], False): MiniMax-H3 drops 2nd reference_audio if L/R stems are split."""
    audio_p = str(task.get("audio_guide") or "")
    if audio_p and Path(audio_p).is_file() and "silent" not in Path(audio_p).name.lower():
        return [audio_p], False
    return [], False


def adapt_duet_audio_prompt(prompt: str, _dual_audio: bool = False) -> str:
    """Normalizes any <Audio 1>/<Audio 2> tags for single combined stereo reference audio."""
    txt = str(prompt or "")
    if re.search(r"<\s*Audio\s+2\s*>", txt, re.IGNORECASE):
        txt = re.sub(r"<\s*Audio\s+1\s*>", "the left channel of <Audio 1>", txt, flags=re.IGNORECASE)
        txt = re.sub(r"<\s*Audio\s+2\s*>", "the right channel of <Audio 1>", txt, flags=re.IGNORECASE)
    return txt


def _parse_wh(res_str: Any) -> tuple[int, int]:
    parts = str(res_str or "1344x768").lower().split("x", 1)
    if len(parts) == 2:
        try:
            return max(64, int(parts[0])), max(64, int(parts[1]))
        except ValueError:
            pass
    return 1344, 768


def _res_enum(res_str: str, upper_p: bool = False) -> str:
    w, _ = _parse_wh(res_str)
    if w >= 1800:
        tier = "1080P" if upper_p else "1080p"
    elif w <= 900:
        tier = "480P" if upper_p else "480p"
    else:
        tier = "768P" if upper_p else "768p"
    return tier


_FAL_UA = "fal-client/0.5.9 (MusicVideoGenerationStudio/1.0)"


def _read_http_error(exc: urllib.error.HTTPError, url: str) -> RuntimeError:
    body = ""
    try:
        raw = exc.read()
        if raw:
            body = raw.decode("utf-8", errors="replace").strip()[:400]
    except Exception:
        pass
    provider = "MiniMax API" if "minimax.io" in str(url) else "fal.ai"
    msg = f"{provider} HTTP {exc.code} on {url}: {body or exc.reason}"
    return RuntimeError(msg)


def _urlopen_read(req: urllib.request.Request, timeout: float = 60.0) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise _read_http_error(exc, req.full_url) from exc


def _retry_call(fn, attempts: int = 3, delays: tuple[float, ...] = (2.0, 5.0)):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            last_exc = _read_http_error(exc, getattr(exc, "url", ""))
            if exc.code in (400, 401, 403, 404, 422) or i >= attempts - 1:
                raise last_exc from exc
            time.sleep(delays[min(i, len(delays) - 1)])
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            no_retry = bool(re.search(r"\b(?:400|401|403|404|422)\b", msg)) or any(
                k in msg for k in ("forbidden", "unauthorized", "invalid api", "policy", "top_up", "locked", "cancelled")
            )
            if no_retry or i >= attempts - 1:
                raise
            time.sleep(delays[min(i, len(delays) - 1)])
    raise last_exc  # pragma: no cover


# ---------------------------------------------------------------------------
# Lightweight stdlib fal REST Queue fallback when fal-client isn't installed
# ---------------------------------------------------------------------------
class _RestRequestHandle:
    def __init__(self, api_key: str, endpoint: str, submit_resp: dict, timeout_s: float = 600.0):
        self.api_key = api_key
        self.endpoint = endpoint
        self.request_id = submit_resp["request_id"]
        self.status_url = submit_resp.get("status_url") or f"https://queue.fal.run/{endpoint}/requests/{self.request_id}/status"
        self.response_url = submit_resp.get("response_url") or f"https://queue.fal.run/{endpoint}/requests/{self.request_id}"
        self.cancel_url = submit_resp.get("cancel_url") or f"https://queue.fal.run/{endpoint}/requests/{self.request_id}/cancel"
        self.timeout_s = timeout_s
        self._cancelled = False

    def _req(self, url: str, method: str = "GET", payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": _FAL_UA,
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        raw = _urlopen_read(req, timeout=60).decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def cancel(self) -> None:
        self._cancelled = True
        try:
            self._req(self.cancel_url, method="PUT")
        except Exception:
            pass

    def iter_events(self, with_logs: bool = True, interval: float = 1.0):
        t0 = time.time()
        while not self._cancelled:
            if time.time() - t0 > self.timeout_s:
                raise TimeoutError(f"fal.ai job {self.request_id} timed out after {self.timeout_s}s")
            sep = "&" if "?" in self.status_url else "?"
            url = f"{self.status_url}{sep}logs=1" if with_logs else self.status_url
            st = _retry_call(lambda: self._req(url))
            status = str(st.get("status") or "").upper()
            if status == "IN_QUEUE":
                yield SimpleNamespace(kind="queued", position=st.get("queue_position", 0))
            elif status == "IN_PROGRESS":
                yield SimpleNamespace(kind="in_progress", logs=st.get("logs") or [])
            elif status == "COMPLETED":
                yield SimpleNamespace(kind="completed", logs=st.get("logs") or [])
                return
            elif status in ("FAILED", "ERROR", "CANCELLED"):
                raise RuntimeError(st.get("error") or f"fal.ai job ended with status {status}")
            time.sleep(interval)

    def get(self) -> dict:
        return _retry_call(lambda: self._req(self.response_url))


class _RestFalClient:
    """Minimal fal.ai Queue + CDN Storage client backed strictly by stdlib urllib."""

    def __init__(self, api_key: str, timeout_s: float = 600.0):
        self.api_key = api_key
        self.timeout_s = timeout_s

    def upload_file(self, file_path: str | Path) -> str:
        p = Path(file_path)
        ext = p.suffix.lower()
        ctype = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
            ".mp4": "video/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg",
        }.get(ext, "application/octet-stream")
        init_body = json.dumps({"file_name": p.name, "content_type": ctype}).encode("utf-8")
        meta = None
        last_err: Exception | None = None
        for init_url in (
            "https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3",
            "https://rest.alpha.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3",
        ):
            req = urllib.request.Request(
                init_url, data=init_body, method="POST",
                headers={
                    "Authorization": f"Key {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": _FAL_UA,
                },
            )
            try:
                meta = json.loads(_urlopen_read(req, timeout=60).decode("utf-8"))
                break
            except Exception as exc:
                last_err = exc
                if "HTTP 404" in str(exc):
                    continue
                raise
        if not meta:
            raise last_err or RuntimeError("Failed to initiate fal.ai upload")
        upload_url, file_url = meta["upload_url"], meta["file_url"]
        put_req = urllib.request.Request(
            upload_url, data=p.read_bytes(), method="PUT",
            headers={"Content-Type": ctype, "User-Agent": _FAL_UA},
        )
        _urlopen_read(put_req, timeout=180)
        return file_url

    def submit(self, endpoint: str, arguments: dict) -> _RestRequestHandle:
        url = f"https://queue.fal.run/{endpoint}"
        body = json.dumps(arguments).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Key {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": _FAL_UA,
            },
        )
        def _send():
            return json.loads(_urlopen_read(req, timeout=60).decode("utf-8"))
        return _RestRequestHandle(self.api_key, endpoint, _retry_call(_send), timeout_s=self.timeout_s)

    def cancel(self, endpoint: str, request_id: str) -> None:
        url = f"https://queue.fal.run/{endpoint}/requests/{request_id}/cancel"
        req = urllib.request.Request(url, method="PUT", headers={"Authorization": f"Key {self.api_key}", "User-Agent": _FAL_UA})
        try:
            _urlopen_read(req, timeout=20)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Event Stream & Handle Duck-Typing Wan2GP JobHandle
# ---------------------------------------------------------------------------
class _EventStream:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._done = object()

    def push_progress(self, **kwargs):
        kwargs.setdefault("status", kwargs.get("raw_phase", ""))
        self._q.put(SimpleNamespace(kind="progress", data=SimpleNamespace(**kwargs)))

    def push_error(self, msg: str):
        self._q.put(SimpleNamespace(kind="error", data=SimpleNamespace(message=str(msg))))

    def close(self):
        self._q.put(self._done)

    def iter(self, timeout: float = 0.25):
        while True:
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                continue
            if item is self._done:
                break
            yield item

    def iter_with_heartbeats(self, timeout: float = 0.25):
        while True:
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                yield None
                continue
            if item is self._done:
                break
            yield item


@dataclass
class FalJobResult:
    success: bool
    generated_files: list[str]
    errors: list[Any]
    request_id: str = ""
    endpoint: str = ""


class FalJobHandle:
    def __init__(self):
        self.events = _EventStream()
        self._result: FalJobResult | None = None
        self._cancel_cb = None
        self.cancelled = False
        self._done_ev = threading.Event()

    def set_cancel_cb(self, cb):
        self._cancel_cb = cb
        if self.cancelled and cb:
            try:
                cb()
            except Exception:
                pass

    def cancel(self):
        self.cancelled = True
        if self._cancel_cb:
            try:
                self._cancel_cb()
            except Exception:
                pass

    def finish(self, result: FalJobResult):
        self._result = result
        self._done_ev.set()
        self.events.close()

    def result(self) -> FalJobResult:
        self._done_ev.wait()
        return self._result or FalJobResult(success=False, generated_files=[], errors=[SimpleNamespace(message="No result")])


def _extract_real_cloud_progress(payload: Any) -> tuple[float | None, int | None, int | None]:
    """Extracts ONLY genuine progress/step metrics returned by a cloud API; returns None when absent."""
    if isinstance(payload, dict):
        for k in ("progress", "percent", "percentage"):
            val = payload.get(k)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                pct = float(val) * 100.0 if isinstance(val, float) and 0.0 < float(val) <= 1.0 else float(val)
                return round(max(0.0, min(100.0, pct)), 1), None, None
        logs = payload.get("logs")
    else:
        logs = payload if isinstance(payload, list) else None
    if isinstance(logs, list):
        for entry in reversed(logs):
            msg = str(entry.get("message") if isinstance(entry, dict) else entry or "")
            m_step = re.search(r"\b(\d+)\s*/\s*(\d+)\b", msg)
            if m_step:
                cur, tot = int(m_step.group(1)), int(m_step.group(2))
                if 0 <= cur <= tot and tot > 0:
                    return round(100.0 * cur / tot, 1), cur, tot
            m_pct = re.search(r"\b(\d+(?:\.\d+)?)\s*%", msg)
            if m_pct:
                return round(max(0.0, min(100.0, float(m_pct.group(1)))), 1), None, None
    return None, None, None


def push_cloud_status(handle: FalJobHandle, phase: str, raw_phase: str, real_source: Any = None, win_idx: int = 1, win_cnt: int = 1) -> None:
    """Emits honest API status and real progress only if reported by the upstream response."""
    pct, step, tot = _extract_real_cloud_progress(real_source)
    handle.events.push_progress(
        phase=phase,
        raw_phase=raw_phase,
        status=raw_phase,
        current_step=step,
        total_steps=tot,
        window_index=win_idx,
        window_count=win_cnt,
        progress=pct,
    )


def normalize_video_stream(ctx, mp4_path: Path, target_fps: float) -> Path:
    ffmpeg = getattr(ctx, "ffmpeg", None)
    if not ffmpeg:
        return mp4_path
    fps = probe_video_fps(ffmpeg, mp4_path)
    if fps and abs(fps - float(target_fps)) <= 0.5:
        return mp4_path
    tmp = mp4_path.with_name(f".cloud_norm_{mp4_path.name}")
    cmd = [
        str(ffmpeg), "-y", "-i", str(mp4_path),
        "-r", f"{float(target_fps):.3f}", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", str(tmp),
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 10000:
        os.replace(str(tmp), str(mp4_path))
    elif tmp.exists():
        tmp.unlink(missing_ok=True)
    return mp4_path


def composite_duet_roi_pass2(ctx, task: dict, out_path: Path, render_crop_fn) -> Path:
    """Shared Two-Pass Duet Pass 2 ROI crop + cloud audio render + feathered composite."""
    from .duet_roi import composite_roi_cloud_pass2
    return composite_roi_cloud_pass2(ctx, task, out_path, render_crop_fn)


def run_chunked_video_sequence(ctx, task_cfg: dict, out_path: Path, fps: float, dur_s: float, max_s: float, run_chunk_fn, should_cancel_fn=None) -> None:
    """Shared sequential chunking with audio slicing & last-frame continuity for scenes longer than endpoint max."""
    ffmpeg = getattr(ctx, "ffmpeg", Path("ffmpeg"))
    out_dir = out_path.parent
    out_name = out_path.stem
    if dur_s <= max_s + 0.5:
        run_chunk_fn(task_cfg, dur_s, out_path, 1, 1)
        normalize_video_stream(ctx, out_path, fps)
        return

    chunks: list[tuple[float, float]] = []
    cur = 0.0
    while cur < dur_s - 0.1:
        cd = min(max_s, dur_s - cur)
        if dur_s - (cur + cd) < 4.0 and dur_s - cur > max_s:
            cd = round((dur_s - cur) / 2.0, 2)
        chunks.append((cur, cd))
        cur += cd

    cur_start_img = task_cfg.get("image_start")
    audio_src = str(task_cfg.get("audio_guide") or "")
    has_live_audio = bool(audio_src and Path(audio_src).is_file() and "silent" not in Path(audio_src).name.lower())
    tmp_files: list[Path] = []
    chunk_mp4s: list[Path] = []
    try:
        for idx, (c_off, c_dur) in enumerate(chunks, 1):
            if should_cancel_fn and should_cancel_fn():
                raise RuntimeError("Cancelled")
            c_mp4 = out_dir / f".cloud_chunk{idx}_{out_name}.mp4"
            c_wav = out_dir / f".cloud_chunk{idx}_{out_name}.wav"
            tmp_files.extend([c_mp4, c_wav])
            chunk_mp4s.append(c_mp4)
            sub_task = dict(task_cfg, image_start=str(cur_start_img), seed=int(task_cfg.get("seed", 42)) + idx - 1)
            if idx < len(chunks):
                sub_task.pop("image_end", None)
                sub_prompt = str(sub_task.get("prompt") or "")
                sub_prompt = re.sub(r"transitioning(?:\s+smoothly)?\s+(?:to|into)\s+<\s*Picture\s+2\s*>[^.]*\.?", "", sub_prompt, flags=re.IGNORECASE).strip()
                sub_task["prompt"] = sub_prompt
            if has_live_audio:
                subprocess.run(
                    [str(ffmpeg), "-y", "-ss", f"{c_off:.3f}", "-t", f"{c_dur:.3f}", "-i", audio_src, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(c_wav)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                sub_task["audio_guide"] = str(c_wav)
            else:
                sub_task["audio_guide"] = ""
            run_chunk_fn(sub_task, c_dur, c_mp4, idx, len(chunks))
            normalize_video_stream(ctx, c_mp4, fps)
            if idx < len(chunks):
                last_jpg = out_dir / f".cloud_last{idx}_{out_name}.jpg"
                tmp_files.append(last_jpg)
                subprocess.run(
                    [str(ffmpeg), "-y", "-sseof", "-0.05", "-i", str(c_mp4), "-vframes", "1", "-q:v", "2", str(last_jpg)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if last_jpg.is_file():
                    cur_start_img = last_jpg
        concat_txt = out_dir / f".cloud_concat_{out_name}.txt"
        concat_txt.write_text("\n".join(f"file '{p.as_posix()}'" for p in chunk_mp4s), encoding="utf-8")
        tmp_files.append(concat_txt)
        res_cat = subprocess.run(
            [str(ffmpeg), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(out_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if res_cat.returncode != 0:
            subprocess.run(
                [str(ffmpeg), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-r", f"{float(fps):.3f}", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    finally:
        for tmp in tmp_files:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Upload Cache & FalSession
# ---------------------------------------------------------------------------
class FalSession:
    """Executes anchor, video, duet ROI, chunked long-clip, and upscaling tasks via fal.ai."""

    def __init__(self, ctx, client=None):
        self.ctx = ctx
        self._client = client
        self._lock = threading.Lock()
        self.last_request_id = ""
        self.last_endpoint = ""
        self._cache_path = Path(ctx.jobs_dir) / "fal_upload_cache.json" if ctx and hasattr(ctx, "jobs_dir") else None
        self._upload_cache: dict[str, str] = self._load_upload_cache()

    def _load_upload_cache(self) -> dict[str, str]:
        if self._cache_path and self._cache_path.is_file():
            try:
                with locked_file_op(self._cache_path.with_suffix(".lock")):
                    d = json.loads(self._cache_path.read_text(encoding="utf-8"))
                    return d if isinstance(d, dict) else {}
            except Exception:
                pass
        return {}

    def _save_upload_cache(self):
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
                    disk.update(self._upload_cache)
                    self._upload_cache = disk
                    atomic_write_json(self._cache_path, disk)
            except Exception:
                pass

    def _ensure_client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            api_key, _ = resolve_fal_key(getattr(self.ctx, "project_dir", None))
            if not api_key:
                raise RuntimeError(
                    "fal.ai API key not configured. Set FAL_KEY environment variable or save it in Studio Settings (encrypted via Windows DPAPI)."
                )
            cfg = read_local_config(getattr(self.ctx, "project_dir", None))
            try:
                import fal_client  # type: ignore
                if hasattr(fal_client, "SyncClient"):
                    self._client = fal_client.SyncClient(key=api_key)
                else:
                    os.environ["FAL_KEY"] = api_key
                    self._client = fal_client
                return self._client
            except ImportError:
                self._client = _RestFalClient(api_key, timeout_s=float(cfg.get("fal_timeout_s", 900.0)))
                return self._client

    def upload_media(self, local_path: str | Path) -> str:
        s = str(local_path or "").strip()
        if not s:
            return ""
        if s.startswith(("http://", "https://", "data:")):
            return s
        p = Path(s)
        if not p.is_file():
            raise FileNotFoundError(f"fal.ai input file missing: {p}")
        fhash = _file_sha256(p)
        cache_key = f"{fhash}:{p.suffix.lower()}"
        with self._lock:
            cached = self._upload_cache.get(cache_key)
        if not cached and self._cache_path and self._cache_path.is_file():
            disk = self._load_upload_cache()
            with self._lock:
                self._upload_cache.update(disk)
                cached = self._upload_cache.get(cache_key)
        if cached:
            return cached
        client = self._ensure_client()
        url = _retry_call(lambda: client.upload_file(str(p)))
        with self._lock:
            self._upload_cache[cache_key] = url
            self._save_upload_cache()
        return url

    def get_default_settings(self, model_type: str) -> dict:
        return {"model_type": model_type, "num_inference_steps": 28, "guidance_scale": 4.0, "custom_settings": {}}

    def _extract_media_url(self, res: Any) -> str:
        if isinstance(res, dict):
            for k in ("video", "output", "image"):
                v = res.get(k)
                if isinstance(v, dict) and v.get("url"):
                    return str(v["url"])
                if isinstance(v, str) and v.startswith(("http", "data:")):
                    return v
            imgs = res.get("images")
            if isinstance(imgs, list) and imgs:
                item = imgs[0]
                return str(item["url"]) if isinstance(item, dict) else str(item)
            if res.get("url"):
                return str(res["url"])
        raise RuntimeError(f"Unexpected fal.ai response structure: {res!r}")

    def _download_file(self, url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_name(f".fal_dl_{out_path.name}")
        def _dl():
            req = urllib.request.Request(url, headers={"User-Agent": _FAL_UA})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_path, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        try:
            _retry_call(_dl)
            os.replace(str(tmp_path), str(out_path))
            return out_path
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _normalize_video_stream(self, mp4_path: Path, target_fps: float) -> Path:
        return normalize_video_stream(self.ctx, mp4_path, target_fps)

    def _build_arguments(self, route: dict, task: dict, dur_s: float) -> dict[str, Any]:
        adapter = route.get("adapter", "minimax_h3_lipsync")
        res_str = str(task.get("resolution") or getattr(self.ctx, "current_resolution", "1344x768"))
        w, h = _parse_wh(res_str)
        prompt = _adapt_prompt_for_fal(task.get("prompt", ""), use_image_n=True)
        seed = int(task.get("seed", 42))

        if adapter in ("flux_image", "qwen_image", "qwen_image_edit"):
            args: dict[str, Any] = {
                "prompt": prompt,
                "seed": seed,
                "num_inference_steps": int(task.get("num_inference_steps", 28)),
                "guidance_scale": float(task.get("guidance_scale", 4.0)),
            }
            if adapter == "qwen_image_edit":
                args["output_format"] = "jpeg"
                if w and h:
                    args["image_size"] = {"width": w, "height": h}
                refs = [self.upload_media(r) for r in (task.get("image_refs") or [])[:4]]
                if refs:
                    args["image_urls"] = refs
            else:
                args["image_size"] = {"width": w, "height": h}
                refs = [self.upload_media(r) for r in (task.get("image_refs") or [])[:4]]
                if refs:
                    args["image_url"] = refs[0]
                    if len(refs) > 1:
                        args["image_urls"] = refs
            return args

        if adapter == "minimax_h3_lipsync":
            args = {"image_url": self.upload_media(task["image_start"]), "resolution": _res_enum(res_str, upper_p=True), "enable_transcription": False, "seed": seed}
            ap = str(task.get("audio_guide") or "")
            if ap and Path(ap).is_file() and "silent" not in Path(ap).name.lower():
                args["audio_url"] = self.upload_media(ap)
            return args

        if adapter == "minimax_h3_fl2va":
            args = {"prompt": prompt, "image_url": self.upload_media(task["image_start"]), "duration": max(4, min(15, int(round(dur_s)))), "resolution": _res_enum(res_str, upper_p=True)}
            if task.get("image_end") and Path(str(task["image_end"])).is_file():
                args["end_image_url"] = self.upload_media(task["image_end"])
            return args

        if adapter == "minimax_h3_ref2v":
            imgs = [self.upload_media(task["image_start"])] if task.get("image_start") else []
            for r in (task.get("image_refs") or [])[:8]:
                u = self.upload_media(r)
                if u not in imgs:
                    imgs.append(u)
            is_max = "h3-max" in str(route.get("endpoint", ""))
            audio_paths, is_dual_audio = resolve_duet_cloud_audio_paths(task, dur_s)
            args = {
                "prompt": adapt_duet_audio_prompt(prompt, is_dual_audio),
                "duration": max(5 if is_max else 4, min(15, int(round(dur_s)))),
                "resolution": _res_enum(res_str, upper_p=True),
                "aspect_ratio": "16:9",
            }
            if imgs:
                args["reference_images"] = imgs
            if audio_paths:
                args["reference_audios"] = [self.upload_media(ap) for ap in audio_paths]
            return args

        if adapter == "ltx2_a2v":
            args = {"prompt": prompt, "duration": max(4, min(20, int(round(dur_s)))), "resolution": _res_enum(res_str, upper_p=False), "aspect_ratio": "16:9", "seed": seed}
            if task.get("image_start") and Path(str(task["image_start"])).is_file():
                args["image_url"] = self.upload_media(task["image_start"])
            if task.get("audio_guide") and Path(str(task["audio_guide"])).is_file():
                args["audio_url"] = self.upload_media(task["audio_guide"])
            return args

        from .fal_adapters import SCENIC_FAL_ADAPTERS, build_scenic_fal_arguments
        if adapter in SCENIC_FAL_ADAPTERS:
            su = self.upload_media(task["image_start"]) if task.get("image_start") and Path(str(task["image_start"])).is_file() else ""
            eu = self.upload_media(task["image_end"]) if task.get("image_end") and Path(str(task["image_end"])).is_file() else ""
            return build_scenic_fal_arguments(adapter, prompt, su, eu, w, dur_s, seed)

        # custom_wgp_app fallback
        payload = dict(task)
        for k in ("image_start", "image_end", "audio_guide", "video_guide", "video_mask"):
            if payload.get(k) and Path(str(payload[k])).is_file():
                payload[k] = self.upload_media(payload[k])
        if payload.get("image_refs"):
            payload["image_refs"] = [self.upload_media(r) for r in payload["image_refs"]]
        return payload

    def _run_endpoint_sync(self, endpoint: str, arguments: dict, out_path: Path, handle: FalJobHandle, win_idx: int = 1, win_cnt: int = 1) -> Path:
        if handle.cancelled:
            raise RuntimeError("Cancelled")
        client = self._ensure_client()
        self.last_endpoint = endpoint
        push_cloud_status(handle, "uploading", f"fal.ai: submitting to {endpoint}...", win_idx=win_idx, win_cnt=win_cnt)
        req_handle = client.submit(endpoint, arguments=arguments)
        req_id = getattr(req_handle, "request_id", "")
        self.last_request_id = req_id

        def _do_cancel():
            if hasattr(req_handle, "cancel"):
                req_handle.cancel()
            elif hasattr(client, "cancel") and req_id:
                client.cancel(endpoint, req_id)

        handle.set_cancel_cb(_do_cancel)
        proc_t0: float | None = None
        last_tick = -1
        for ev in req_handle.iter_events(with_logs=True):
            if handle.cancelled:
                raise RuntimeError("Cancelled")
            cls_name = type(ev).__name__.lower()
            kind = getattr(ev, "kind", "") or cls_name
            if "queued" in kind:
                pos = getattr(ev, "position", 0)
                push_cloud_status(handle, "queued", f"fal.ai: IN_QUEUE (pos {pos})", win_idx=win_idx, win_cnt=win_cnt)
            elif "progress" in kind:
                if proc_t0 is None:
                    proc_t0 = time.time()
                proc_e = max(0.0, time.time() - proc_t0)
                logs = getattr(ev, "logs", None) or []
                latest_log = str(logs[-1].get("message") if isinstance(logs[-1], dict) else logs[-1]).strip() if logs else ""
                tick = int(proc_e // 4)
                if latest_log or tick > last_tick:
                    last_tick = tick
                    lbl = latest_log or (f"fal.ai: IN_PROGRESS ({int(proc_e)}s)" if proc_e >= 2 else "fal.ai: IN_PROGRESS")
                    push_cloud_status(handle, "Denoising", lbl, real_source=logs, win_idx=win_idx, win_cnt=win_cnt)
            elif "completed" in kind:
                push_cloud_status(handle, "downloading", "fal.ai: downloading output...", win_idx=win_idx, win_cnt=win_cnt)
                break

        if handle.cancelled:
            raise RuntimeError("Cancelled")
        res_data = req_handle.get()
        media_url = self._extract_media_url(res_data)
        self._download_file(media_url, out_path)
        return out_path

    def _run_duet_pass2_roi_lipsync(self, task: dict, route: dict, out_path: Path, handle: FalJobHandle) -> Path:
        fps = float(getattr(self.ctx, "fps", 24) or 24)
        dur_s = max(4.0, float(task.get("video_length", 120)) / fps)
        max_s = float(route.get("max_duration_s", 15.0) or 15.0)
        def _do_crop(crop_in: Path, audio_p: Path, crop_out: Path, _cw: int, _ch: int):
            def _one(st_task, cd, cp, wi, wc):
                args = {"video_url": self.upload_media(st_task.get("video_guide") or crop_in), "audio_url": self.upload_media(st_task.get("audio_guide") or audio_p), "sync_mode": "remap"}
                self._run_endpoint_sync(route["endpoint"], args, cp, handle, win_idx=wi, win_cnt=wc)
            if dur_s <= max_s + 0.5:
                _one({"video_guide": crop_in, "audio_guide": audio_p}, dur_s, crop_out, 1, 1)
            else:
                run_chunked_video_sequence(self.ctx, dict(task, video_guide=str(crop_in), audio_guide=str(audio_p)), crop_out, fps, dur_s, max_s, _one, should_cancel_fn=lambda: handle.cancelled)
        return composite_duet_roi_pass2(self.ctx, task, out_path, _do_crop)

    def submit_task(self, task_cfg: dict) -> FalJobHandle:
        handle = FalJobHandle()

        def _worker():
            try:
                fal_spec = dict(task_cfg.get("_fal_spec") or {})
                stage = "anchor" if int(task_cfg.get("image_mode", 0)) == 1 else "video"
                cfg_local = read_local_config(getattr(self.ctx, "project_dir", None))
                route = fal_spec if fal_spec.get("endpoint") else resolve_fal_route(
                    task_cfg, sc=task_cfg.get("_fal_ctx"), pass_num=None, stage=stage, custom_fal_app=cfg_local.get("custom_fal_app", ""),
                )
                out_dir = Path(getattr(self.ctx, "anchors_dir" if stage == "anchor" else "renders_dir", Path(".")))
                ext = ".jpg" if stage == "anchor" else ".mp4"
                out_name = str(task_cfg.get("output_filename") or f"fal_out_{int(time.time())}")
                out_path = out_dir / (out_name if out_name.endswith(ext) else f"{out_name}{ext}")

                if stage == "anchor":
                    args = self._build_arguments(route, task_cfg, 0.0)
                    self._run_endpoint_sync(route["endpoint"], args, out_path, handle)
                    handle.finish(FalJobResult(True, [str(out_path)], [], self.last_request_id, route["endpoint"]))
                    return

                if route.get("adapter") == "duet_pass2_roi_lipsync":
                    self._run_duet_pass2_roi_lipsync(task_cfg, route, out_path, handle)
                    handle.finish(FalJobResult(True, [str(out_path)], [], self.last_request_id, route["endpoint"]))
                    return

                fps = float(getattr(self.ctx, "fps", 24) or 24)
                dur_s = max(4.0, float(task_cfg.get("video_length", 120)) / fps)
                max_s = float(route.get("max_duration_s", 15.0) or 15.0)
                run_chunked_video_sequence(
                    self.ctx, task_cfg, out_path, fps, dur_s, max_s,
                    lambda st, cd, cp, wi, wc: self._run_endpoint_sync(route["endpoint"], self._build_arguments(route, st, cd), cp, handle, win_idx=wi, win_cnt=wc),
                    should_cancel_fn=lambda: handle.cancelled,
                )
                handle.finish(FalJobResult(True, [str(out_path)], [], self.last_request_id, route["endpoint"]))
            except Exception as exc:
                err_msg = str(exc)
                handle.events.push_error(err_msg)
                handle.finish(FalJobResult(False, [], [SimpleNamespace(message=err_msg)], self.last_request_id, self.last_endpoint))

        threading.Thread(target=_worker, daemon=True).start()
        return handle

    def run_media_postprocessing(self, video_path: str, **kwargs) -> FalJobResult:
        method = str(kwargs.get("spatial_upsampling") or "").strip().lower()
        base = method.split("*", 1)[0]
        scale = float(method.rsplit("*", 1)[-1]) if "*" in method and method.rsplit("*", 1)[-1].replace(".", "", 1).isdigit() else 2.0
        r_key = "flashvsr" if base.startswith("flashvsr") else ("h3facerefine" if base == "h3facerefine" else ("ltx25_refine" if base.startswith("ltx25") else "seedvr2"))
        route = FAL_UPSCALE_ROUTES.get(r_key, FAL_UPSCALE_ROUTES["seedvr2"])
        src = Path(video_path)
        out_path = src.with_name(f".fal_up_{src.name}")
        if route["adapter"] == "sync_lipsync_v2":
            from .media_utils import probe_has_audio_stream
            ffmpeg = Path(getattr(self.ctx, "ffmpeg", Path("ffmpeg")))
            if not probe_has_audio_stream(ffmpeg, src):
                print(f"      [fal.ai] Skipping h3facerefine on {src.name}: source clip has no audio stream.", flush=True)
                return FalJobResult(True, [str(src)], [], self.last_request_id, route["endpoint"])
        url = self.upload_media(src)
        if route["adapter"] in ("flashvsr_video", "ltx25_refine_video"):
            args = {"video_url": url}
        elif route["adapter"] == "sync_lipsync_v2":
            args = {"video_url": url, "audio_url": url, "sync_mode": "remap"}
        else:
            args = {"video_url": url, "upscale_mode": "factor", "upscale_factor": scale, "noise_scale": 0.1, "seed": int(kwargs.get("seed", 42))}
        h, stop_ev = FalJobHandle(), threading.Event()
        if getattr(self.ctx, "project_dir", None):
            def _poll_cancel():
                from .wgp_bridge import check_cancel_requested
                while not stop_ev.wait(0.35):
                    if check_cancel_requested(self.ctx, backend="fal", task_stem=src.stem):
                        h.cancel(); break
            threading.Thread(target=_poll_cancel, daemon=True).start()
        try:
            self._run_endpoint_sync(route["endpoint"], args, out_path, h)
            return FalJobResult(True, [str(out_path)], [], self.last_request_id, route["endpoint"])
        except Exception as exc:
            out_path.unlink(missing_ok=True)
            return FalJobResult(False, [], [SimpleNamespace(message=str(exc))], self.last_request_id, route["endpoint"])
        finally:
            stop_ev.set()

    def close(self):
        with self._lock:
            self._save_upload_cache()
