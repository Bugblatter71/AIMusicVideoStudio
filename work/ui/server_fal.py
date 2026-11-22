"""HTTP API handlers and queue enrichment helpers for fal.ai cloud rendering."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs

try:
    from pipeline.fal_catalog import (
        estimate_fal_cost_usd,
        estimate_minimax_cost_usd,
        get_fal_profiles_for_kind,
        get_fal_status,
        get_minimax_profiles,
        resolve_fal_key,
        resolve_minimax_key,
        resolve_task_backend,
        save_fal_settings,
    )
except ImportError:
    from work.pipeline.fal_catalog import (
        estimate_fal_cost_usd,
        estimate_minimax_cost_usd,
        get_fal_profiles_for_kind,
        get_fal_status,
        get_minimax_profiles,
        resolve_fal_key,
        resolve_minimax_key,
        resolve_task_backend,
        save_fal_settings,
    )


try:
    from server_security import resolve_project_dir_safe
    from server_engine_io import parse_valid_engine
except ImportError:
    try:
        from work.ui.server_security import resolve_project_dir_safe
        from work.ui.server_engine_io import parse_valid_engine
    except ImportError:
        from ui.server_security import resolve_project_dir_safe
        from ui.server_engine_io import parse_valid_engine


def _srv():
    key = f"{__package__}.server" if __package__ else "server"
    mod = sys.modules.get(key) or sys.modules.get("work.ui.server") or sys.modules.get("ui.server") or sys.modules.get("server")
    if mod is not None:
        return mod
    try:
        from work.ui import server

        return server
    except ImportError:
        import server

        return server


def _project_dir_for_slug(slug: str | None) -> Path | None:
    if not slug:
        return None
    srv = _srv()
    cand = resolve_project_dir_safe(srv.PROJECTS_DIR, str(slug).strip())
    return cand if (cand is not None and cand.is_dir()) else None


def _require_optional_project(handler, slug) -> tuple[bool, Path | None]:
    """Resolves `slug` if provided; sends 404 and returns (False, None) when unknown/invalid."""
    if slug is None or not str(slug).strip():
        return True, None
    proj_dir = _project_dir_for_slug(str(slug))
    if proj_dir is None:
        handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
        return False, None
    return True, proj_dir


def _project_backend_defaults(proj_dir: Path | None) -> dict[str, str]:
    out = {"project_default_backend": "", "project_default_upscale_backend": ""}
    if not proj_dir or not proj_dir.is_dir():
        return out
    jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith(("state_hashes", "local_config"))]
    if not jsons:
        return out
    try:
        raw = json.loads(jsons[-1].read_text(encoding="utf-8"))
        models = raw.get("models", {}) if isinstance(raw, dict) else {}
        out["project_default_backend"] = str(models.get("default_backend") or "")
        out["project_default_upscale_backend"] = str(models.get("default_upscale_backend") or "")
    except Exception:
        pass
    return out


def _probe_fal_connection(api_key: str) -> tuple[bool, int, str]:
    req = urllib.request.Request(
        "https://api.fal.ai/v1/models?limit=1",
        headers={"Authorization": f"Key {api_key}", "User-Agent": "fal-client/0.5.9 (MusicVideoGenerationStudio/1.0)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = int(getattr(resp, "status", 200))
            return True, code, "Authenticated with fal.ai."
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", errors="replace").strip()[:250]
        except Exception:
            pass
        if exc.code in (401, 403):
            return False, exc.code, f"fal.ai rejected the API key (HTTP {exc.code}): {body or exc.reason}"
        if exc.code == 404:
            return True, 200, "Authenticated with fal.ai."
        return False, exc.code, f"fal.ai HTTP {exc.code}: {body or exc.reason}"
    except Exception as exc:
        return False, 0, f"Connection error: {exc}"


def _probe_minimax_connection(api_key: str) -> tuple[bool, int, str]:
    req = urllib.request.Request(
        "https://api.minimax.io/v1/files/retrieve?file_id=0",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "fal-client/0.5.9 (MusicVideoGenerationStudio/1.0)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            base = data.get("base_resp") or {}
            sc = int(base.get("status_code", 0) or 0)
            msg = str(base.get("status_msg") or "")
            if sc in (1004, 1002, 2049) or "invalid api key" in msg.lower() or "secret key" in msg.lower() or "insufficient balance" in msg.lower():
                return False, 401, f"MiniMax API error ({sc}): {msg}"
            return True, 200, "Authenticated with MiniMax API."
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", errors="replace").strip()[:250]
        except Exception:
            pass
        if exc.code in (401, 403):
            return False, exc.code, f"MiniMax rejected the API key (HTTP {exc.code}): {body or exc.reason}"
        if exc.code in (400, 404):
            return True, 200, "Authenticated with MiniMax API."
        return False, exc.code, f"MiniMax HTTP {exc.code}: {body or exc.reason}"
    except Exception as exc:
        return False, 0, f"Connection error: {exc}"


def handle_fal_get(handler, path: str, parsed) -> bool:
    if path not in ("/api/fal/status", "/api/minimax/status"):
        return False
    slug = parse_qs(parsed.query).get("slug", [None])[0]
    ok, proj_dir = _require_optional_project(handler, slug)
    if not ok:
        return True
    status = get_fal_status(root_dir=proj_dir)
    status.update(_project_backend_defaults(proj_dir))
    handler.send_json({"success": True, **status})
    return True


def handle_fal_post(handler, path: str, body: dict | None) -> bool:
    body = body or {}
    if path in ("/api/fal/config", "/api/minimax/config"):
        slug = body.get("slug")
        ok, proj_dir = _require_optional_project(handler, slug)
        if not ok:
            return True
        raw_key = body["fal_key"] if "fal_key" in body else None
        raw_mm = body["minimax_key"] if "minimax_key" in body else None
        try:
            save_fal_settings(
                root_dir=proj_dir,
                fal_key=str(raw_key).strip() if isinstance(raw_key, str) and raw_key.strip() else None,
                clear_key=(raw_key == ""),
                minimax_key=str(raw_mm).strip() if isinstance(raw_mm, str) and raw_mm.strip() else None,
                clear_minimax_key=(raw_mm == ""),
                default_execution_backend=body.get("default_execution_backend"),
                default_upscale_backend=body.get("default_upscale_backend"),
                custom_fal_app=body.get("custom_fal_app"),
                fal_timeout_s=body.get("fal_timeout_s"),
            )
            status = get_fal_status(root_dir=proj_dir)
        except Exception as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
            return True
        status.update(_project_backend_defaults(proj_dir))
        handler.send_json({"success": True, **status})
        return True

    if path == "/api/fal/test":
        ok_p, proj_dir = _require_optional_project(handler, body.get("slug"))
        if not ok_p:
            return True
        override_key = str(body.get("fal_key") or "").strip()
        key, source = (override_key, "input") if override_key else resolve_fal_key(proj_dir)
        if not key:
            handler.send_json({"success": False, "source": "none", "error": "No fal.ai API key configured."})
            return True
        ok, code, msg = _probe_fal_connection(key)
        handler.send_json({"success": ok, "source": source, "status_code": code, "message": msg, "error": "" if ok else msg})
        return True

    if path == "/api/minimax/test":
        ok_p, proj_dir = _require_optional_project(handler, body.get("slug"))
        if not ok_p:
            return True
        override_key = str(body.get("minimax_key") or "").strip()
        key, source = (override_key, "input") if override_key else resolve_minimax_key(proj_dir)
        if not key:
            handler.send_json({"success": False, "source": "none", "error": "No MiniMax API key configured."})
            return True
        ok, code, msg = _probe_minimax_connection(key)
        handler.send_json({"success": ok, "source": source, "status_code": code, "message": msg, "error": "" if ok else msg})
        return True

    if path == "/api/render/engines":
        srv = _srv()
        slug = body.get("slug")
        ok_p, _ = _require_optional_project(handler, slug)
        if not ok_p:
            return True
        raw_en, raw_mp = body.get("enabled_engines"), body.get("max_parallel")
        clean_en = None
        if raw_en is not None:
            if not isinstance(raw_en, dict):
                handler.send_error_json("enabled_engines must be an object", status=HTTPStatus.BAD_REQUEST)
                return True
            clean_en = {}
            for k, v in raw_en.items():
                bk = parse_valid_engine(str(k))
                if bk is None:
                    handler.send_error_json(f"Unsupported engine '{k}'", status=HTTPStatus.BAD_REQUEST)
                    return True
                clean_en[bk] = bool(v)
        clean_mp = None
        if raw_mp is not None:
            if not isinstance(raw_mp, dict):
                handler.send_error_json("max_parallel must be an object", status=HTTPStatus.BAD_REQUEST)
                return True
            clean_mp = {}
            for k, v in raw_mp.items():
                bk = parse_valid_engine(str(k))
                if bk is None:
                    handler.send_error_json(f"Unsupported engine '{k}'", status=HTTPStatus.BAD_REQUEST)
                    return True
                clean_mp[bk] = v
        st = srv.set_enabled_engines(srv, slug, clean_en, max_parallel=clean_mp)
        handler.send_json({"success": True, **st})
        return True

    if path == "/api/queue/set_engine":
        srv = _srv()
        slug = body.get("slug")
        item_id = body.get("id")
        if not slug or not item_id:
            handler.send_error_json("slug and id are required")
            return True
        if _project_dir_for_slug(slug) is None:
            handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
            return True
        raw_ovr = body.get("backend_override")
        ovr_norm = ""
        if raw_ovr is not None and str(raw_ovr).strip():
            ovr_norm = parse_valid_engine(str(raw_ovr)) or ""
            if not ovr_norm:
                handler.send_error_json(f"Unsupported engine '{raw_ovr}'", status=HTTPStatus.BAD_REQUEST)
                return True
        try:
            items = srv.set_queue_item_backend_override(srv, str(slug).strip(), item_id, ovr_norm)
            handler.send_json({"success": True, "queue": items})
        except KeyError as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.NOT_FOUND)
        except RuntimeError as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.CONFLICT)
        except Exception as exc:
            handler.send_error_json(str(exc), status=HTTPStatus.BAD_REQUEST)
        return True

    return False


def patch_models_payload(data: dict | None) -> dict:
    out = dict(data or {})
    out["fal_anchor_renderers"] = get_fal_profiles_for_kind("anchor", out)
    out["fal_video_renderers"] = get_fal_profiles_for_kind("video", out)
    out["minimax_video_renderers"] = get_minimax_profiles(out)
    return out


def enrich_queue_with_fal_metadata(
    queue: list[dict],
    scene_by_id: dict,
    ctx=None,
    proj_dir: Path | None = None,
    default_res: str = "1344x768",
) -> None:
    """Attaches effective execution backend, model_type, endpoint, and cloud cost estimate to UI queue items."""
    for itm in queue:
        sid = itm.get("scene_id")
        pass_num = itm.get("pass")
        sc = scene_by_id.get(sid) if isinstance(scene_by_id, dict) else None
        vrspec: dict = {}
        if ctx is not None and sc is not None:
            try:
                _, vrspec = ctx.resolve_video_renderer(sc, pass_num=pass_num)
            except Exception:
                vrspec = {}
        default_backend = resolve_task_backend(ctx, sc=sc, rspec=vrspec, pass_num=pass_num, stage="video") if (ctx is not None and sc is not None) else "local"
        ovr_raw = str(itm.get("backend_override") or "").strip().lower()
        backend = ovr_raw if ovr_raw in ("local", "fal", "minimax") else default_backend
        itm["resolved_default_backend"] = default_backend
        itm["backend"] = backend
        if vrspec.get("model_type"):
            itm["model_type"] = str(vrspec["model_type"])
        if backend in ("fal", "minimax"):
            dur_s = float(itm.get("duration") or 5.0)
            res_s = str(itm.get("resolution") or default_res)
            fn = estimate_minimax_cost_usd if backend == "minimax" else estimate_fal_cost_usd
            est = fn(dur_s, resolution=res_s, rspec=vrspec, sc=sc, pass_num=pass_num)
            itm["fal_endpoint"] = est["endpoint"]
            itm["estimated_cost_usd"] = est["estimated_cost_usd"]
            itm["cost_str"] = est["cost_str"]
            itm["auto_chunks"] = est["auto_chunks"]
            itm["fal_notes"] = est["notes"]
            try:
                from pipeline.media_utils import estimate_cloud_pass_render_time, format_duration_estimate
            except ImportError:
                from work.pipeline.media_utils import estimate_cloud_pass_render_time, format_duration_estimate
            est_sec = estimate_cloud_pass_render_time(dur_s, resolution=res_s, auto_chunks=int(est["auto_chunks"]), calibration_alpha=1.0)
            itm["estimated_seconds"] = est_sec
            itm["estimated_str"] = format_duration_estimate(est_sec)
        else:
            for k in ("fal_endpoint", "estimated_cost_usd", "cost_str", "auto_chunks", "fal_notes", "estimated_seconds", "estimated_str"):
                itm.pop(k, None)
