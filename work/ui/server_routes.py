"""HTTP request handling, REST APIs, and SSE log streaming for Studio UI."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from pipeline.takes import handle_takes_api
    from pipeline.clip_import import handle_import_clip_http
    from pipeline.project_paths import resolve_project_dirs, resolve_queue_file, scan_project_audio_assets
    from pipeline.scene_purge import handle_scene_split_post_purge, purge_deleted_scene_artifacts, save_project_json_with_purge
except ImportError:
    try:
        from work.pipeline.takes import handle_takes_api
        from work.pipeline.clip_import import handle_import_clip_http
        from work.pipeline.project_paths import resolve_project_dirs, resolve_queue_file, scan_project_audio_assets
        from work.pipeline.scene_purge import handle_scene_split_post_purge, purge_deleted_scene_artifacts, save_project_json_with_purge
    except ImportError:
        handle_takes_api = handle_import_clip_http = resolve_project_dirs = resolve_queue_file = scan_project_audio_assets = None
        handle_scene_split_post_purge = purge_deleted_scene_artifacts = save_project_json_with_purge = None

try:
    from server_fal import handle_fal_get, handle_fal_post, patch_models_payload
    from server_engine_io import load_persisted_max_parallel, norm_engine, parse_valid_engine, requeue_failed_items, reset_session_blocks_for_items
    from server_security import read_json_dict, resolve_config_in_project_safe, resolve_project_dir_safe
except ImportError:
    try:
        from work.ui.server_fal import handle_fal_get, handle_fal_post, patch_models_payload
        from work.ui.server_engine_io import load_persisted_max_parallel, norm_engine, parse_valid_engine, requeue_failed_items, reset_session_blocks_for_items
        from work.ui.server_security import read_json_dict, resolve_config_in_project_safe, resolve_project_dir_safe
    except ImportError:
        from ui.server_fal import handle_fal_get, handle_fal_post, patch_models_payload
        from ui.server_engine_io import load_persisted_max_parallel, norm_engine, parse_valid_engine, requeue_failed_items, reset_session_blocks_for_items
        from ui.server_security import read_json_dict, resolve_config_in_project_safe, resolve_project_dir_safe


def _srv():
    key = f"{__package__}.server" if __package__ else "server"
    mod = sys.modules.get(key) or sys.modules.get("work.ui.server") or sys.modules.get("ui.server") or sys.modules.get("server")
    if mod is not None: return mod
    try:
        from work.ui import server
        return server
    except ImportError:
        import server
        return server


def _require_project(handler, srv, slug: str | None, config_name: str | None = None, need_cfg: bool = True) -> tuple[Path | None, Path | None]:
    pdir = resolve_project_dir_safe(srv.PROJECTS_DIR, slug)
    if pdir is None or not pdir.is_dir():
        handler.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
        return None, None
    if not need_cfg:
        return pdir, None
    cfg = resolve_config_in_project_safe(pdir, config_name=config_name)
    if cfg is None:
        handler.send_error_json(f"Project config not found in '{slug}'", status=HTTPStatus.NOT_FOUND)
        return None, None
    return pdir, cfg


class StudioRequestHandler(BaseHTTPRequestHandler):
    server_version = "MusicVideoStudio/1.0"

    def log_message(self, format, *args):
        # Silence routine static asset logs to keep console clean
        if "/media/" in self.path or "/static/" in self.path:
            return
        super().log_message(format, *args)

    def send_json(self, data: dict | list, status=HTTPStatus.OK):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self.send_json({"error": message}, status=status)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.end_headers()

    def do_GET(self):
        srv = _srv()
        parsed = urlparse(self.path)
        path = parsed.path

        # 1. API: List Projects
        if path == "/api/projects":
            self.handle_list_projects()
            return

        # 2. API: Project Details & Queue
        if path.startswith("/api/project/") and path.endswith("/queue"):
            slug = unquote(path[len("/api/project/"): -len("/queue")].strip("/"))
            self.handle_get_project_queue(slug)
            return

        # 2b. API: Project Characters
        if path.startswith("/api/project/") and path.endswith("/characters"):
            slug = unquote(path[len("/api/project/"): -len("/characters")].strip("/"))
            try:
                from pipeline.characters import handle_characters_api
            except ImportError:
                from work.pipeline.characters import handle_characters_api
            handle_characters_api(self, "get_characters", slug=slug)
            return

        if path.startswith("/api/project/"):
            slug = unquote(path[len("/api/project/"):].strip("/"))
            config_name = parse_qs(parsed.query).get("config", [None])[0]
            self.handle_get_project(slug, config_name=config_name)
            return

        # 3. API: Model Profiles
        if path == "/api/models":
            self.handle_get_models()
            return

        # 3b. API: Dummy Render Mode & fal.ai Status
        if path == "/api/settings/dummy_mode":
            self.send_json({"success": True, "dummy_render": getattr(srv, "dummy_render_mode", False)})
            return
        if handle_fal_get and handle_fal_get(self, path, parsed):
            return

        # 3c. API: Takes & Checkpoints
        for pfx, act in (("/api/takes/", "get_takes"), ("/api/checkpoints/", "get_checkpoints"), ("/api/vault/stats/", "get_vault_stats")):
            if path.startswith(pfx):
                if handle_takes_api: handle_takes_api(self, act, path=path)
                return

        # 4. API: Render Status
        if path == "/api/render/status":
            self.send_json(srv.get_multi_engine_render_status(srv))
            return

        # 5. API: Log Stream (Server-Sent Events)
        if path == "/api/render/events":
            self.handle_sse_stream()
            return

        # 6. Media Streaming (Audio, Video, Images with Range support)
        if path.startswith("/media/"):
            self.handle_media_stream(path[len("/media/"):])
            return

        # 7. Static UI Files
        self.handle_static_file(path)

    def do_POST(self):
        srv = _srv()
        parsed = urlparse(self.path)
        path = parsed.path
        ctype = (self.headers.get("Content-Type") or "").lower()
        if path == "/api/takes/import" and "application/json" not in ctype:
            if handle_import_clip_http:
                handle_import_clip_http(self, srv, parsed, body=None)
            return
        body, json_err = read_json_dict(self)
        if json_err:
            self.send_error_json(json_err, status=HTTPStatus.BAD_REQUEST)
            return

        # Create new project
        if path == "/api/projects/new":
            name = (body or {}).get("name", "").strip()
            slug = re.sub(r"[^\w\-]+", "_", name).strip("_").lower()
            proj_dir = resolve_project_dir_safe(srv.PROJECTS_DIR, slug)
            if not name or proj_dir is None:
                self.send_error_json("Valid project name is required")
                return
            if proj_dir.exists():
                self.send_error_json(f"Project '{slug}' already exists")
                return

            # Import runner's init logic
            sys.path.insert(0, str(srv.WORKSPACE_ROOT / "work"))
            try:
                import run_overnight_v3
                run_overnight_v3.init_new_project(str(proj_dir), force=False)
            except Exception as e:
                self.send_error_json(f"Failed to create project: {e}")
                return

            self.send_json({"success": True, "slug": slug})
            return

        # Settings: Toggle Dummy Render Mode
        if path == "/api/settings/dummy_mode":
            val = bool((body or {}).get("enabled", True))
            srv.dummy_render_mode = val
            srv.broadcast_log(f"[Studio UI Settings] Dummy render mode {'ENABLED' if srv.dummy_render_mode else 'DISABLED'}")
            self.send_json({"success": True, "dummy_render": srv.dummy_render_mode})
            return

        # Characters & Anchors API Actions
        if "/characters/" in path or path.startswith("/api/anchors/"):
            try:
                from pipeline.stage1_anchors import handle_anchors_api; from pipeline.characters import handle_characters_api
            except ImportError:
                from work.pipeline.stage1_anchors import handle_anchors_api; from work.pipeline.characters import handle_characters_api
            handle_anchors_api(self, srv, path, body=body, require_project=_require_project) if path.startswith("/api/anchors/") else handle_characters_api(self, path.split("/characters/")[-1], body=body)
            return

        # Takes & Checkpoints & fal.ai Actions
        if handle_fal_post and handle_fal_post(self, path, body):
            return
        if path == "/api/takes/import":
            if handle_import_clip_http:
                handle_import_clip_http(self, srv, parsed, body=body)
            return

        if path in ("/api/takes/activate", "/api/takes/delete", "/api/checkpoints/create", "/api/checkpoints/rollback"):
            if handle_takes_api:
                action = "delete_take" if path == "/api/takes/delete" else path.split("/")[-1]
                handle_takes_api(self, action, body=body)
            return

        if path == "/api/queue/move":
            slug, item_id, direction = (body or {}).get("slug"), (body or {}).get("id"), (body or {}).get("direction", "up")
            if not slug or not item_id:
                self.send_error_json("slug and id are required"); return
            if not _require_project(self, srv, slug, need_cfg=False)[0]: return
            try: q = srv.move_in_project_queue(slug, item_id, direction); self.send_json({"success": True, "slug": slug, "queue": q, "done_queue": srv.get_done_queue_history(srv, slug, q)})
            except Exception as e: self.send_error_json(str(e))
            return

        if path == "/api/queue/reorder":
            slug, item_id, target_id = (body or {}).get("slug"), (body or {}).get("id") or (body or {}).get("item_id"), (body or {}).get("target_id")
            position, order = (body or {}).get("position", "before"), (body or {}).get("order")
            if not slug or (not order and (not item_id or not target_id)):
                self.send_error_json("slug and (order or (id and target_id)) are required"); return
            if not _require_project(self, srv, slug, need_cfg=False)[0]: return
            try: q = srv.reorder_in_project_queue(slug, item_id=item_id, target_id=target_id, position=position, order=order); self.send_json({"success": True, "slug": slug, "queue": q, "done_queue": srv.get_done_queue_history(srv, slug, q)})
            except Exception as e: self.send_error_json(str(e))
            return

        if path == "/api/queue/remove":
            slug, item_id = (body or {}).get("slug"), (body or {}).get("id")
            if not slug or not item_id:
                self.send_error_json("slug and id are required"); return
            if not _require_project(self, srv, slug, need_cfg=False)[0]: return
            try: q = srv.remove_from_project_queue(slug, item_id); self.send_json({"success": True, "slug": slug, "queue": q, "done_queue": srv.get_done_queue_history(srv, slug, q)})
            except Exception as e: self.send_error_json(str(e))
            return

        if path == "/api/queue/enqueue":
            slug, item_id = (body or {}).get("slug"), (body or {}).get("id")
            if not slug or not item_id:
                self.send_error_json("slug and id are required"); return
            if not _require_project(self, srv, slug, need_cfg=False)[0]: return
            try:
                items = srv.enqueue_in_project_queue(slug, item_id)
                srv.run_queue_worker(slug, force_restart=False)
                self.send_json({"success": True, "slug": slug, "queue": items, "done_queue": srv.get_done_queue_history(srv, slug, items)})
            except RuntimeError as e: self.send_error_json(str(e), status=HTTPStatus.CONFLICT)
            except Exception as e: self.send_error_json(str(e))
            return

        if path == "/api/queue/enqueue_all":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("slug is required"); return
            if not _require_project(self, srv, slug, need_cfg=False)[0]: return
            queue = srv.compute_fast_project_queue(slug, force_recheck=True)
            reset_session_blocks_for_items(srv, [x for x in (queue or []) if x.get("status") == "queued" and x.get("needs_render")], clear_failed=True)
            try: srv.run_queue_worker(slug, force_restart=False)
            except RuntimeError as e: self.send_error_json(str(e), status=HTTPStatus.CONFLICT); return
            self.send_json({"success": True, "slug": slug, "queue": queue, "done_queue": srv.get_done_queue_history(srv, slug, queue)})
            return

        if path == "/api/queue/clear":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("slug is required"); return
            if not _require_project(self, srv, slug, need_cfg=False)[0]: return
            try: q = srv.clear_project_queue(slug); self.send_json({"success": True, "slug": slug, "queue": q, "done_queue": srv.get_done_queue_history(srv, slug, q)})
            except Exception as e: self.send_error_json(str(e))
            return

        if path == "/api/stitch":
            slug = (body or {}).get("slug")
            if not slug: self.send_error_json("Project slug is required"); return
            proj_dir, cfg_file = _require_project(self, srv, slug)
            if not proj_dir: return
            threading.Thread(target=srv.run_stitch_worker, args=(str(cfg_file), slug), daemon=True).start()
            self.send_json({"success": True, "message": "Master video restitching started immediately."})
            return

        if path == "/api/render/start":
            slug, args = (body or {}).get("slug"), (body or {}).get("args", [])
            if not slug: self.send_error_json("Project slug is required"); return
            proj_dir, config_file = _require_project(self, srv, slug)
            if not proj_dir: return
            if args == ["stitch"]:
                threading.Thread(target=srv.run_stitch_worker, args=(str(config_file), slug), daemon=True).start()
                self.send_json({"success": True, "queued": False, "args": args}); return
            is_anchor_req = bool(args and any(str(a).strip().lower() in ("--anchors-only", "anchors-only", "anchors") for a in args))
            queue_file = resolve_queue_file(proj_dir, for_write=True) if resolve_queue_file else (proj_dir / "work" / "jobs" / "v3_queue.json")
            if not queue_file.is_file(): srv.compute_fast_project_queue(slug)
            raw_en, raw_mp = (body or {}).get("enabled_engines"), (body or {}).get("max_parallel")
            enabled_map = max_par_map = None
            if raw_en is not None:
                if not isinstance(raw_en, dict): self.send_error_json("enabled_engines must be an object"); return
                enabled_map = {}
                for k, v in raw_en.items():
                    bk = parse_valid_engine(str(k))
                    if bk is None: self.send_error_json(f"Unsupported engine '{k}'"); return
                    enabled_map[bk] = bool(v)
            if raw_mp is not None:
                if not isinstance(raw_mp, dict): self.send_error_json("max_parallel must be an object"); return
                max_par_map = {}
                for k, v in raw_mp.items():
                    bk = parse_valid_engine(str(k))
                    if bk is None: self.send_error_json(f"Unsupported engine '{k}'"); return
                    max_par_map[bk] = v
            if isinstance(enabled_map, dict):
                with srv.render_lock:
                    for b, v in enabled_map.items(): srv.enabled_engines[b] = v
            with srv.render_lock:
                if (getattr(srv, "queue_worker_active", False) or (srv.active_process is not None and srv.active_process.poll() is None)) and srv.render_status.get("current_project") not in (None, slug):
                    self.send_error_json(f"Another project ('{srv.render_status.get('current_project')}') is already rendering", status=HTTPStatus.CONFLICT); return
            if is_anchor_req:
                try:
                    from pipeline.anchor_takes import prepare_targeted_anchor_queue_items
                except ImportError:
                    from work.pipeline.anchor_takes import prepare_targeted_anchor_queue_items
                queued_ancs = prepare_targeted_anchor_queue_items(srv, slug, list(args))
                if not queued_ancs:
                    self.send_json({"success": True, "queued": False, "args": args, "message": "All requested anchors are already up-to-date."}); return
            elif args:
                target_sid = str(args[0]).strip().upper()
                try: srv.prepare_targeted_queue_items(slug, target_sid)
                except Exception as e: srv.broadcast_log(f"[Studio UI Error] Failed preparing queue for scene {target_sid}: {e}")
            else:
                try:
                    requeue_failed_items(srv, slug, enabled_map)
                    items = srv.compute_fast_project_queue(slug, force_recheck=False)
                    queued_now = [x for x in items if x.get("status") == "queued" and x.get("needs_render") and (not isinstance(enabled_map, dict) or enabled_map.get(norm_engine(x.get("backend_override") or x.get("backend") or "local"), True))]
                    if not queued_now:
                        srv.broadcast_log("[Studio UI Queue] No pending passes for selected engines.")
                        self.send_json({"success": True, "queued": False, "message": "No pending passes for selected engines."}); return
                    reset_session_blocks_for_items(srv, queued_now, clear_failed=True)
                except Exception as e: srv.broadcast_log(f"[Studio UI Error] Failed preparing render queue: {e}")
            try:
                srv.run_queue_worker(slug, force_restart=False, enabled_engines=enabled_map, max_parallel=max_par_map) if (enabled_map is not None or max_par_map is not None) else srv.run_queue_worker(slug, force_restart=False)
            except RuntimeError as e:
                self.send_error_json(str(e), status=HTTPStatus.CONFLICT); return
            self.send_json({"success": True, "queued": True, "message": f"Queue running (target: {args or 'all pending'})."})
            return

        if path == "/api/render/stop":
            stop_slug = (body or {}).get("slug") if isinstance(body, dict) else None
            stop_b = (body or {}).get("backend") if isinstance(body, dict) else None
            stop_fn = (body or {}).get("filename") if isinstance(body, dict) else None
            if stop_slug and not _require_project(self, srv, str(stop_slug), need_cfg=False)[0]: return
            parsed_b = None
            if stop_b is not None and str(stop_b).strip():
                parsed_b = parse_valid_engine(str(stop_b))
                if parsed_b is None:
                    self.send_error_json(f"Unsupported engine '{stop_b}'"); return
            srv.kill_active_render(target_slug=stop_slug, backend=parsed_b, filename=stop_fn) if (parsed_b or stop_fn) else srv.kill_active_render(target_slug=stop_slug)
            self.send_json({"success": True, "stopped": True})
            return

        # Split Scene Endpoint
        if path == "/api/scene/split" or (path.startswith("/api/project/") and path.endswith("/scene/split")):
            slug = (body or {}).get("slug")
            if not slug and path.startswith("/api/project/"):
                slug = unquote(path[len("/api/project/"): -len("/scene/split")].strip("/"))
            scene_id = (body or {}).get("scene_id")
            split_time = (body or {}).get("split_time")
            id1 = (body or {}).get("id1")
            id2 = (body or {}).get("id2")

            if not slug or not scene_id or split_time is None:
                self.send_error_json("slug, scene_id, and split_time are required"); return
            proj_dir, config_file = _require_project(self, srv, slug)
            if not proj_dir: return

            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                split_fn = getattr(srv, "split_scene_in_project_data", None)
                if not split_fn:
                    from pipeline.context import split_scene_in_project_data as split_fn

                updated_data, sc1, sc2 = split_fn(data, scene_id, float(split_time), id1=id1, id2=id2)
                srv.backup_project_json(config_file, throttle_seconds=0.0)
                config_file.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")
                if handle_scene_split_post_purge: handle_scene_split_post_purge(proj_dir, config_file, scene_id, sc1, sc2, updated_data)
                updated_queue = srv.compute_fast_project_queue(slug, force_recheck=False)
                srv.broadcast_log(f"[Studio UI] Scene {scene_id} split into {sc1.get('id')} ({sc1.get('start')}s-{sc1.get('end')}s) and {sc2.get('id')} ({sc2.get('start')}s-{sc2.get('end')}s)")
                self.send_json({
                    "success": True,
                    "scene1": sc1,
                    "scene2": sc2,
                    "scenes": updated_data.get("scenes", []),
                    "slug": slug, "queue": updated_queue, "done_queue": srv.get_done_queue_history(srv, slug, updated_queue),
                })
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Delete Scene Endpoint
        if path == "/api/scene/delete" or (path.startswith("/api/project/") and path.endswith("/scene/delete")):
            slug = (body or {}).get("slug")
            if not slug and path.startswith("/api/project/"):
                slug = unquote(path[len("/api/project/"): -len("/scene/delete")].strip("/"))
            scene_id = (body or {}).get("scene_id")
            close_gap = (body or {}).get("close_gap", True)

            if not slug or not scene_id:
                self.send_error_json("slug and scene_id are required"); return
            proj_dir, config_file = _require_project(self, srv, slug)
            if not proj_dir: return

            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                del_fn = getattr(srv, "delete_scene_in_project_data", None)
                if not del_fn:
                    from pipeline.context import delete_scene_in_project_data as del_fn

                updated_data, del_sc = del_fn(data, scene_id, close_gap=bool(close_gap))
                srv.backup_project_json(config_file, throttle_seconds=0.0)
                config_file.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")
                if purge_deleted_scene_artifacts: purge_deleted_scene_artifacts(proj_dir, [del_sc.get("id") or scene_id], config_data=updated_data, config_file=config_file)
                updated_queue = srv.compute_fast_project_queue(slug, force_recheck=True)
                srv.broadcast_log(f"[Studio UI] Scene {scene_id} deleted (close_gap={close_gap})")
                self.send_json({
                    "success": True,
                    "deleted_scene": del_sc,
                    "scenes": updated_data.get("scenes", []),
                    "slug": slug, "queue": updated_queue, "done_queue": srv.get_done_queue_history(srv, slug, updated_queue),
                })
            except Exception as e:
                self.send_error_json(str(e))
            return

        if path == "/api/scene/duplicate" or (path.startswith("/api/project/") and path.endswith("/scene/duplicate")):
            try:
                from pipeline.scene_ops import handle_duplicate_scene_api
            except ImportError:
                from work.pipeline.scene_ops import handle_duplicate_scene_api
            handle_duplicate_scene_api(self, srv, body, path=path, require_project=_require_project)
            return

        # Persist ONLY scene.locked without saving unrelated unsaved edits
        if path == "/api/scene/lock":
            if not isinstance(body, dict):
                self.send_error_json("JSON object body required"); return
            slug, scene_id, locked = body.get("slug"), body.get("scene_id"), body.get("locked")
            if not slug or not scene_id or not isinstance(locked, bool):
                self.send_error_json("slug, scene_id, and boolean locked are required"); return
            proj_dir, config_file = _require_project(self, srv, str(slug))
            if not proj_dir: return
            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                target_sc = next((s for s in data.get("scenes", []) if str(s.get("id") or "") == str(scene_id)), None)
                if not target_sc:
                    self.send_error_json(f"Scene '{scene_id}' not found", status=HTTPStatus.NOT_FOUND); return
                if locked: target_sc["locked"] = True
                else: target_sc.pop("locked", None)
                srv.backup_project_json(config_file, throttle_seconds=0.0)
                config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                srv.broadcast_log(f"[Studio UI] Scene {scene_id} {'locked' if locked else 'unlocked'} (saved lock state only)")
                self.send_json({"success": True, "scene_id": scene_id, "locked": locked})
            except Exception as e:
                self.send_error_json(str(e))
            return

        if path == "/api/takes/upscale":
            try:
                from pipeline.upscale import handle_upscale_api
            except ImportError:
                from work.pipeline.upscale import handle_upscale_api
            handle_upscale_api(self, srv, body)
            return

        if path == "/api/takes/repair_lipsync":
            try:
                from pipeline.duet_roi import handle_repair_lipsync_api
            except ImportError:
                from work.pipeline.duet_roi import handle_repair_lipsync_api
            handle_repair_lipsync_api(self, srv, body)
            return

        self.send_error_json("Endpoint not found", status=HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        srv = _srv()
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/project/"):
            slug = unquote(path[len("/api/project/"):].strip("/"))
            config_name = parse_qs(parsed.query).get("config", [None])[0]
            body, json_err = read_json_dict(self)
            if json_err:
                self.send_error_json(json_err, status=HTTPStatus.BAD_REQUEST); return
            if not body or "project" not in body:
                self.send_error_json("Invalid project JSON payload"); return

            proj_dir = resolve_project_dir_safe(srv.PROJECTS_DIR, slug)
            if proj_dir is None or not proj_dir.is_dir():
                self.send_error_json(f"Project folder '{slug}' not found", status=HTTPStatus.NOT_FOUND); return

            if config_name:
                cn = str(config_name).strip()
                if cn in (".", "..") or "/" in cn or "\\" in cn or "\x00" in cn or cn.startswith("state_hashes"):
                    self.send_error_json("Invalid config filename", status=HTTPStatus.BAD_REQUEST); return
                target_json = (proj_dir / cn).resolve()
                if target_json.parent != proj_dir.resolve() or not target_json.exists():
                    self.send_error_json(f"Config '{config_name}' not found", status=HTTPStatus.NOT_FOUND); return
            else:
                jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
                target_json = jsons[-1] if jsons else proj_dir / f"{proj_dir.name}.json"

            try:
                purged_ids = save_project_json_with_purge(srv, proj_dir, target_json, slug, body, parse_qs(parsed.query)) if save_project_json_with_purge else []
                queue = None
                try:
                    queue = srv.compute_fast_project_queue(slug, force_recheck=False)
                    if getattr(srv, "queue_worker_active", False):
                        reset_session_blocks_for_items(srv, [x for x in (queue or []) if x.get("status") == "queued" and x.get("needs_render")], clear_failed=False)
                        srv.wake_enabled_engine_workers(srv, slug)
                except Exception:
                    pass
                self.send_json({"success": True, "slug": slug, "saved": str(target_json.name), "purged_scene_ids": purged_ids, "queue": queue, "done_queue": srv.get_done_queue_history(srv, slug, queue)})
            except Exception as e:
                self.send_error_json(f"Failed to write file: {e}")
            return

        self.send_error_json("Endpoint not found", status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict | None:
        data, err = read_json_dict(self)
        return None if err else data

    def handle_list_projects(self):
        srv = _srv()
        projects = []
        if srv.PROJECTS_DIR.is_dir():
            for d in sorted(srv.PROJECTS_DIR.iterdir()):
                if d.is_dir():
                    jsons = [j for j in sorted(d.glob("*.json")) if not j.name.startswith("state_hashes")]
                    for j in jsons:
                        try:
                            data = json.loads(j.read_text(encoding="utf-8"))
                            pcfg = data.get("project", {})
                            display_name = pcfg.get("name") or d.name.replace("_", " ").title()
                            projects.append({
                                "slug": d.name,
                                "name": display_name,
                                "label": f"{display_name} ({j.name})",
                                "config_file": j.name,
                                "fps": pcfg.get("fps", 24),
                                "total_duration": pcfg.get("total_duration", 0),
                                "anchors_count": len(data.get("anchors", [])),
                                "scenes_count": len(data.get("scenes", [])),
                                "markers_count": len(data.get("markers", [])),
                                "master_output": pcfg.get("master_output", ""),
                            })
                        except Exception:
                            projects.append({
                                "slug": d.name,
                                "name": d.name,
                                "label": f"{d.name} ({j.name})",
                                "config_file": j.name,
                                "error": "Failed to parse JSON",
                            })
        self.send_json({"projects": projects})

    def handle_get_project_queue(self, slug: str):
        srv = _srv()
        if not _require_project(self, srv, slug, need_cfg=False)[0]:
            return
        queue = srv.compute_fast_project_queue(slug)
        eng_status = srv.read_project_engine_status(slug)
        self.send_json({"success": True, "slug": slug, "queue": queue, "done_queue": srv.get_done_queue_history(srv, slug, queue), "engine_status": eng_status})

    def handle_get_project(self, slug: str, config_name: str | None = None):
        srv = _srv()
        proj_dir, cfg_file = _require_project(self, srv, slug, config_name=config_name)
        if not proj_dir:
            return
        lp = load_persisted_max_parallel(proj_dir)
        if lp:
            with srv.render_lock:
                if not getattr(srv, "queue_worker_active", False):
                    if hasattr(srv, "max_parallel") and isinstance(srv.max_parallel, dict):
                        srv.max_parallel.update(lp)
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            self.send_error_json(f"Error parsing {cfg_file.name}: {e}")
            return

        # Scan on-disk status for anchors, renders, master videos
        pdirs = resolve_project_dirs(proj_dir, config_data=data) if resolve_project_dirs else None
        anchors_dir = pdirs["anchors"] if pdirs else (proj_dir / data.get("project", {}).get("directories", {}).get("anchors", "work/anchors"))

        # Collect existing anchor images (match by both filename and canonical ID like A01)
        existing_anchors = {}
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        anchor_search_dirs = [
            anchors_dir,
            proj_dir / "references",
            proj_dir / "work" / "ref_sheets",
        ]
        scanned_dirs = set()
        for sdir in anchor_search_dirs:
            if sdir.is_dir():
                resolved_sdir = str(sdir.resolve())
                if resolved_sdir in scanned_dirs:
                    continue
                scanned_dirs.add(resolved_sdir)
                for img in sdir.iterdir():
                    if img.is_file() and any(img.name.lower().endswith(ext) for ext in image_exts):
                        aid = srv.extract_anchor_id(img.name)
                        info = {
                            "filename": img.name,
                            "aid": aid,
                            "is_anchor_dir": sdir == anchors_dir,
                            "size_bytes": img.stat().st_size,
                            "mtime": img.stat().st_mtime,
                        }
                        if img.name not in existing_anchors:
                            existing_anchors[img.name] = info
                        # Map aid (e.g. A01) -> info, prioritizing primary anchors_dir
                        if aid and (aid not in existing_anchors or sdir == anchors_dir):
                            existing_anchors[aid] = info

        # Collect rendered clips across renders_dir and trimmed_dir
        existing_clips = srv.collect_project_clips(proj_dir, data)
        strips_dir = pdirs["strips"] if pdirs else (proj_dir / data.get("project", {}).get("directories", {}).get("review_strips", "work/frames"))
        strips_dir.mkdir(parents=True, exist_ok=True)

        # Collect existing filmstrip images
        existing_strips = {}
        if strips_dir.is_dir():
            for s in strips_dir.glob("*_strip.jpg"):
                raw_sid = s.stem.replace("_strip", "")
                strip_info = {
                    "filename": s.name,
                    "mtime": s.stat().st_mtime,
                }
                for sid_variant in (raw_sid, raw_sid.upper(), raw_sid.lower()):
                    existing_strips[sid_variant] = strip_info

        # Collect master outputs and available audio / reference files
        master_videos = [
            {"filename": vid.name, "size_mb": round(vid.stat().st_size / (1024 * 1024), 2), "mtime": vid.stat().st_mtime}
            for vid in sorted(proj_dir.glob("*Master*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        ]
        audio_dir, ref_dir = proj_dir / "audio", proj_dir / "references"
        audio_info = scan_project_audio_assets(proj_dir, data) if scan_project_audio_assets else {
            "audio_files": [a.name for a in audio_dir.glob("*.*") if a.suffix.lower() in (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac")] if audio_dir.is_dir() else []
        }
        ref_files = [r.name for r in ref_dir.glob("*.*") if r.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")] if ref_dir.is_dir() else []
        try: from pipeline.stage1_anchors import collect_anchor_source_files
        except ImportError: from work.pipeline.stage1_anchors import collect_anchor_source_files

        self.send_json({
            "slug": slug,
            "config_file": cfg_file.name,
            "data": data,
            "assets": {
                "anchors": existing_anchors,
                "clips": existing_clips,
                "strips": existing_strips,
                "master_videos": master_videos,
                **audio_info,
                "reference_files": ref_files,
                "anchor_source_files": collect_anchor_source_files(proj_dir),
            },
        })

    def handle_get_models(self):
        srv = _srv()
        data = {}
        if srv.PROFILES_FILE.is_file():
            try:
                data = json.loads(srv.PROFILES_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        if patch_models_payload:
            data = patch_models_payload(data)
        self.send_json(data)

    def handle_sse_stream(self):
        srv = _srv()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Send existing log history first
        with srv.render_lock:
            existing = list(srv.log_history)
            seq_slugs = dict(getattr(srv, "log_seq_slugs", {}))
            run_floor = int(getattr(srv, "_run_log_seq_floor", 0) or 0)
            is_running = srv.render_status["running"] or getattr(srv, "queue_worker_active", False)
            exit_code = srv.render_status["exit_code"]

        last_seen_seq = 0
        for seq, line in existing:
            last_seen_seq = seq
            if (seq <= run_floor and any(tok in line for tok in ("[QUEUE_ACTIVE]", "[QUEUE_PROGRESS]", "[QUEUE_DONE]", "[QUEUE_FAIL]", "[QUEUE_CANCEL]", "[QUEUE_SKIP]"))) or any(tok in line for tok in ("[ANCHORS_UPDATED]", "[MASTER_UPDATED]", "[MASTER_LOCKED]", "[MASTER_ERROR]", "[MASTER_STITCH_", "[UPSCALE_DONE]", "[REPAIR_LIPSYNC_DONE]")):
                continue
            ev_slug = seq_slugs.get(seq)
            payload = json.dumps({"text": line, "done": False, **({"slug": ev_slug} if ev_slug else {})})
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

        has_sent_done = not is_running and exit_code is not None
        ping_timer = 0.0

        # Stream new lines until client disconnects
        while True:
            try:
                time.sleep(0.15)
                ping_timer += 0.15
                with srv.render_lock:
                    is_running = srv.render_status["running"] or getattr(srv, "queue_worker_active", False)
                    exit_code = srv.render_status["exit_code"]
                    # If log_history was cleared or reset behind us
                    if srv.log_history and srv.log_history[-1][0] < last_seen_seq:
                        last_seen_seq = 0
                    new_items = [(s, l, getattr(srv, "log_seq_slugs", {}).get(s)) for s, l in srv.log_history if s > last_seen_seq]

                if new_items:
                    for seq, line, ev_slug in new_items:
                        payload = json.dumps({"text": line, "done": False, **({"slug": ev_slug} if ev_slug else {})})
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        last_seen_seq = seq
                    self.wfile.flush()

                if is_running:
                    has_sent_done = False
                elif exit_code is not None and not has_sent_done:
                    has_sent_done = True
                    payload = json.dumps({
                        "text": f"[Status] Process stopped (Exit code {exit_code})",
                        "done": True,
                        "exit_code": exit_code,
                    })
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()

                # Send periodic keep-alive ping comment
                if ping_timer >= 15.0:
                    ping_timer = 0.0
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                break

    def handle_media_stream(self, subpath: str):
        try:
            from work.ui import server_media
        except ImportError:
            import server_media
        server_media.handle_media_stream(self, subpath)

    def stream_file_with_range(self, file_path: Path):
        try:
            from work.ui import server_media
        except ImportError:
            import server_media
        server_media.stream_file_with_range(self, file_path)

    def handle_static_file(self, raw_path: str):
        srv = _srv()
        rel_path = raw_path.strip("/")
        if not rel_path or rel_path == "index.html":
            file_path = srv.STATIC_DIR / "index.html"
        else:
            file_path = (srv.STATIC_DIR / rel_path).resolve()

        # Security check: prevent directory traversal outside static dir
        try:
            file_path.relative_to(srv.STATIC_DIR)
        except ValueError:
            self.send_error_json("Access denied", status=HTTPStatus.FORBIDDEN)
            return

        if not file_path.is_file():
            self.send_error_json("Asset not found", status=HTTPStatus.NOT_FOUND)
            return

        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "text/plain"
        if file_path.suffix in (".html", ".css", ".js"):
            mime_type += "; charset=utf-8"

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


def run_server(port: int = 7860):
    srv = _srv()
    host = "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), StudioRequestHandler)
    print("=" * 76)
    print(f"  Music Video Generation Studio - UI Server")
    print("=" * 76)
    print(f"  URL        : http://{host}:{port}")
    print(f"  Projects   : {srv.PROJECTS_DIR}")
    print(f"  Profiles   : {srv.PROFILES_FILE}")
    print(f"  Root       : {srv.WORKSPACE_ROOT}")
    print("=" * 76)
    print("Press Ctrl+C to stop the server.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        httpd.server_close()
