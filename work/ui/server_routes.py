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
except ImportError:
    try:
        from work.pipeline.takes import handle_takes_api
    except ImportError:
        handle_takes_api = None


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

        # 3b. API: Dummy Render Mode setting
        if path == "/api/settings/dummy_mode":
            self.send_json({"success": True, "dummy_render": getattr(srv, "dummy_render_mode", False)})
            return

        # 3c. API: Takes & Checkpoints
        if path.startswith("/api/takes/"):
            if handle_takes_api:
                handle_takes_api(self, "get_takes", path=path)
            return
        if path.startswith("/api/checkpoints/"):
            if handle_takes_api:
                handle_takes_api(self, "get_checkpoints", path=path)
            return
        if path.startswith("/api/vault/stats/"):
            if handle_takes_api:
                handle_takes_api(self, "get_vault_stats", path=path)
            return

        # 4. API: Render Status
        if path == "/api/render/status":
            with srv.render_lock:
                st = dict(srv.render_status)
                st["history_count"] = len(srv.log_history)
            curr_proj = st.get("current_project")
            eng_status = srv.read_project_engine_status(curr_proj)
            if not eng_status and srv.PROJECTS_DIR.is_dir():
                for d in srv.PROJECTS_DIR.iterdir():
                    if d.is_dir():
                        cand = srv.read_project_engine_status(d.name)
                        if cand and cand.get("state") == "rendering":
                            eng_status = cand
                            curr_proj = d.name
                            break
            st["current_project"] = curr_proj
            st["engine_status"] = eng_status
            if eng_status and eng_status.get("state") == "rendering":
                st["running"] = True
            self.send_json(st)
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
        body = self.read_json_body()

        # Create new project
        if path == "/api/projects/new":
            name = (body or {}).get("name", "").strip()
            if not name:
                self.send_error_json("Project name is required")
                return
            slug = re.sub(r"[^\w\-]+", "_", name).strip("_").lower()
            proj_dir = srv.PROJECTS_DIR / slug
            if proj_dir.exists():
                self.send_error_json(f"Project '{slug}' already exists")
                return

            # Import runner's init logic
            sys.path.insert(0, str(srv.WORKSPACE_ROOT / "work"))
            try:
                import run_overnight_v3
                run_overnight_v3.init_new_project(slug, force=False)
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

        # Characters API Actions
        if "/characters/" in path:
            action = path.split("/characters/")[-1]
            try:
                from pipeline.characters import handle_characters_api
            except ImportError:
                from work.pipeline.characters import handle_characters_api
            handle_characters_api(self, action, body=body)
            return

        # Takes & Checkpoints Actions
        if path in ("/api/takes/activate", "/api/checkpoints/create", "/api/checkpoints/rollback"):
            if handle_takes_api:
                action = path.split("/")[-1]
                handle_takes_api(self, action, body=body)
            return

        # Queue management: Move item up/down
        if path == "/api/queue/move":
            slug = (body or {}).get("slug")
            item_id = (body or {}).get("id")
            direction = (body or {}).get("direction", "up")
            if not slug or not item_id:
                self.send_error_json("slug and id are required")
                return
            try:
                items = srv.move_in_project_queue(slug, item_id, direction)
                self.send_json({"success": True, "queue": items})
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Queue management: Remove item from queue
        if path == "/api/queue/remove":
            slug = (body or {}).get("slug")
            item_id = (body or {}).get("id")
            if not slug or not item_id:
                self.send_error_json("slug and id are required")
                return
            try:
                items = srv.remove_from_project_queue(slug, item_id)
                self.send_json({"success": True, "queue": items})
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Queue management: Enqueue single item
        if path == "/api/queue/enqueue":
            slug = (body or {}).get("slug")
            item_id = (body or {}).get("id")
            if not slug or not item_id:
                self.send_error_json("slug and id are required")
                return
            try:
                items = srv.enqueue_in_project_queue(slug, item_id)
                self.send_json({"success": True, "queue": items})
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Queue management: Enqueue all unrendered
        if path == "/api/queue/enqueue_all":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("slug is required")
                return
            queue = srv.compute_fast_project_queue(slug, force_recheck=True)
            self.send_json({"success": True, "queue": queue})
            return

        # Queue management: Clear queue
        if path == "/api/queue/clear":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("slug is required")
                return
            try:
                items = srv.clear_project_queue(slug)
                self.send_json({"success": True, "queue": items})
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Direct Restitch Endpoint (runs immediately without blocking or waiting for render queue)
        if path == "/api/stitch":
            slug = (body or {}).get("slug")
            if not slug:
                self.send_error_json("Project slug is required")
                return

            proj_dir = srv.PROJECTS_DIR / slug
            jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
            if not jsons:
                self.send_error_json(f"No project config found in {proj_dir}")
                return
            config_file = str(jsons[-1])

            threading.Thread(target=srv.run_stitch_worker, args=(config_file, slug), daemon=True).start()
            self.send_json({"success": True, "message": "Master video restitching started immediately."})
            return

        # Start render: Single-submission queue execution
        if path == "/api/render/start":
            slug = (body or {}).get("slug")
            args = (body or {}).get("args", [])
            if not slug:
                self.send_error_json("Project slug is required")
                return

            proj_dir = srv.PROJECTS_DIR / slug
            jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
            if not jsons:
                self.send_error_json(f"No project config found in {proj_dir}")
                return
            config_file = jsons[-1]

            # Direct restitch request
            if args == ["stitch"]:
                threading.Thread(target=srv.run_stitch_worker, args=(str(config_file), slug), daemon=True).start()
                self.send_json({"success": True, "queued": False, "args": args})
                return

            # Stage 1 Anchor generation request (e.g. ["--anchors-only", "A01"])
            if args and any(str(a).strip().lower() in ("--anchors-only", "anchors-only", "anchors") for a in args):
                threading.Thread(target=srv.run_anchor_worker, args=(str(config_file), slug, list(args)), daemon=True).start()
                self.send_json({"success": True, "queued": True, "args": args, "message": f"Anchor generation started ({args})."})
                return

            queue_file = proj_dir / "work" / "jobs_v3" / "v3_queue.json"
            if not queue_file.is_file():
                srv.compute_fast_project_queue(slug)

            # Targeted scene render (e.g. clicking "Render Scene")
            if args:
                target_sid = str(args[0]).strip().upper()
                try:
                    srv.prepare_targeted_queue_items(slug, target_sid)
                except Exception as e:
                    srv.broadcast_log(f"[Studio UI Error] Failed preparing queue for scene {target_sid}: {e}")
            else:
                # "Run": evaluate queue while preserving user ordering, FORCED items, and SKIPPED BY USER items
                try:
                    items = srv.compute_fast_project_queue(slug, force_recheck=False)
                    has_any_queued = any(x.get("status") == "queued" and x.get("needs_render") for x in items)
                    if not has_any_queued:
                        srv.broadcast_log("[Studio UI Queue] All passes up-to-date. Nothing to render.")
                        self.send_json({"success": True, "queued": False, "message": "All passes up-to-date. Nothing to render."})
                        return
                except Exception as e:
                    srv.broadcast_log(f"[Studio UI Error] Failed preparing render queue: {e}")

            # Start single-render queue worker (never killing an in-progress GPU render)
            srv.run_queue_worker(slug, force_restart=False)
            self.send_json({"success": True, "queued": True, "message": f"Queue running (target: {args or 'all pending'})."})
            return

        # Cancel active render
        if path == "/api/render/stop":
            stop_slug = (body or {}).get("slug") if isinstance(body, dict) else None
            srv.kill_active_render(target_slug=stop_slug)
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
                self.send_error_json("slug, scene_id, and split_time are required")
                return

            proj_dir = srv.PROJECTS_DIR / slug
            if not proj_dir.is_dir():
                self.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
                return

            jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
            if not jsons:
                self.send_error_json(f"No project config found in {proj_dir}", status=HTTPStatus.NOT_FOUND)
                return
            config_file = jsons[-1]

            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                split_fn = getattr(srv, "split_scene_in_project_data", None)
                if not split_fn:
                    from pipeline.context import split_scene_in_project_data as split_fn

                updated_data, sc1, sc2 = split_fn(data, scene_id, float(split_time), id1=id1, id2=id2)
                srv.backup_project_json(config_file, throttle_seconds=0.0)
                config_file.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")
                updated_queue = srv.compute_fast_project_queue(slug, force_recheck=False)
                srv.broadcast_log(f"[Studio UI] Scene {scene_id} split into {sc1.get('id')} ({sc1.get('start')}s-{sc1.get('end')}s) and {sc2.get('id')} ({sc2.get('start')}s-{sc2.get('end')}s)")
                self.send_json({
                    "success": True,
                    "scene1": sc1,
                    "scene2": sc2,
                    "scenes": updated_data.get("scenes", []),
                    "queue": updated_queue,
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
                self.send_error_json("slug and scene_id are required")
                return

            proj_dir = srv.PROJECTS_DIR / slug
            if not proj_dir.is_dir():
                self.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
                return

            jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
            if not jsons:
                self.send_error_json(f"No project config found in {proj_dir}", status=HTTPStatus.NOT_FOUND)
                return
            config_file = jsons[-1]

            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                del_fn = getattr(srv, "delete_scene_in_project_data", None)
                if not del_fn:
                    from pipeline.context import delete_scene_in_project_data as del_fn

                updated_data, del_sc = del_fn(data, scene_id, close_gap=bool(close_gap))
                srv.backup_project_json(config_file, throttle_seconds=0.0)
                config_file.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")
                updated_queue = srv.compute_fast_project_queue(slug, force_recheck=True)
                srv.broadcast_log(f"[Studio UI] Scene {scene_id} deleted (close_gap={close_gap})")
                self.send_json({
                    "success": True,
                    "deleted_scene": del_sc,
                    "scenes": updated_data.get("scenes", []),
                    "queue": updated_queue,
                })
            except Exception as e:
                self.send_error_json(str(e))
            return

        self.send_error_json("Endpoint not found", status=HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        srv = _srv()
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/project/"):
            slug = unquote(path[len("/api/project/"):].strip("/"))
            config_name = parse_qs(parsed.query).get("config", [None])[0]
            body = self.read_json_body()
            if not body or "project" not in body:
                self.send_error_json("Invalid project JSON payload")
                return

            proj_dir = srv.PROJECTS_DIR / slug
            if not proj_dir.is_dir():
                self.send_error_json(f"Project folder '{slug}' not found", status=HTTPStatus.NOT_FOUND)
                return

            target_json = None
            if config_name:
                cand = proj_dir / config_name
                if cand.exists():
                    target_json = cand
            if target_json is None:
                jsons = sorted(proj_dir.glob("*.json"))
                target_json = jsons[-1] if jsons else proj_dir / f"{slug}.json"

            # Back up before saving (throttled to at most once per 60s per file to avoid backup spam on auto-save)
            srv.backup_project_json(target_json, throttle_seconds=60.0)

            try:
                target_json.write_text(json.dumps(body, indent=2), encoding="utf-8")
                try:
                    srv.compute_fast_project_queue(slug, force_recheck=False)
                except Exception:
                    pass
                self.send_json({"success": True, "saved": str(target_json.name)})
            except Exception as e:
                self.send_error_json(f"Failed to write file: {e}")
            return

        self.send_error_json("Endpoint not found", status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return None
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

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
        proj_dir = srv.PROJECTS_DIR / slug
        if not proj_dir.is_dir():
            self.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
            return

        queue = srv.compute_fast_project_queue(slug)
        eng_status = srv.read_project_engine_status(slug)
        self.send_json({"success": True, "queue": queue, "engine_status": eng_status})

    def handle_get_project(self, slug: str, config_name: str | None = None):
        srv = _srv()
        proj_dir = srv.PROJECTS_DIR / slug
        if not proj_dir.is_dir():
            self.send_error_json(f"Project '{slug}' not found", status=HTTPStatus.NOT_FOUND)
            return

        jsons = [j for j in sorted(proj_dir.glob("*.json")) if not j.name.startswith("state_hashes")]
        if not jsons:
            self.send_error_json(f"No project config found in {slug}", status=HTTPStatus.NOT_FOUND)
            return

        cfg_file = None
        if config_name:
            for j in jsons:
                if j.name == config_name:
                    cfg_file = j
                    break
        if cfg_file is None:
            cfg_file = jsons[-1]
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            self.send_error_json(f"Error parsing {cfg_file.name}: {e}")
            return

        # Scan on-disk status for anchors, renders, master videos
        anchors_dir = proj_dir / data.get("project", {}).get("directories", {}).get("anchors", "work/anchors")

        # Collect existing anchor images (match by both filename and canonical ID like A01)
        existing_anchors = {}
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        anchor_search_dirs = [
            anchors_dir,
            proj_dir / "work" / "anchors_v3",
            proj_dir / "work" / "anchors",
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
        strips_dir = proj_dir / data.get("project", {}).get("directories", {}).get("review_strips", "work/frames_v3")
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

        # Collect master outputs
        master_videos = []
        for vid in sorted(proj_dir.glob("*Master*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            master_videos.append({
                "filename": vid.name,
                "size_mb": round(vid.stat().st_size / (1024 * 1024), 2),
                "mtime": vid.stat().st_mtime,
            })

        # Scan available audio files
        audio_dir = proj_dir / "audio"
        audio_files = []
        if audio_dir.is_dir():
            for a in audio_dir.glob("*.*"):
                if a.suffix.lower() in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
                    audio_files.append(a.name)

        # Scan reference images
        ref_dir = proj_dir / "references"
        ref_files = []
        if ref_dir.is_dir():
            for r in ref_dir.glob("*.*"):
                if r.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    ref_files.append(r.name)

        self.send_json({
            "slug": slug,
            "config_file": cfg_file.name,
            "data": data,
            "assets": {
                "anchors": existing_anchors,
                "clips": existing_clips,
                "strips": existing_strips,
                "master_videos": master_videos,
                "audio_files": audio_files,
                "reference_files": ref_files,
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
            is_running = srv.render_status["running"] or getattr(srv, "queue_worker_active", False)
            exit_code = srv.render_status["exit_code"]

        last_seen_seq = 0
        for seq, line in existing:
            payload = json.dumps({"text": line, "done": False})
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            last_seen_seq = seq
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
                    new_items = [item for item in srv.log_history if item[0] > last_seen_seq]

                if new_items:
                    for seq, line in new_items:
                        payload = json.dumps({"text": line, "done": False})
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
