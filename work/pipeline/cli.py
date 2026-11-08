import json
import re
import sys
import time
from pathlib import Path

from .context import ProjectContext
from .media_utils import WORKSPACE_ROOT
from .prompts import extract_anchor_id
from .stage1_anchors import import_anchors_from_dir, match_anchor_id, run_stage1_anchors
from .stage2_scenes import _scene_id_matches, run_stage2_scenes
from .stage3_master import run_stage3_assemble_master
from .wgp_bridge import LazyWgpSession, write_engine_status


def print_catalog(ctx: ProjectContext):
    print("\n" + "=" * 88)
    prof_info = f" | profiles: {ctx.profiles_path.name}" if ctx.profiles_path else ""
    print(f"PROJECT: {ctx.project_name} ({ctx.json_path.name}{prof_info})")
    print("=" * 88)
    print(f"AVAILABLE ANCHOR RENDERERS (default_anchor_renderer = {ctx.default_anchor_renderer}):")
    print("-" * 88)
    for rid in sorted(ctx.anchor_renderers.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        spec = ctx.anchor_renderers[rid]
        marker = "*DEFAULT*" if rid == str(ctx.default_anchor_renderer) else "         "
        steps = spec.get("num_inference_steps", "-")
        cfg_val = spec.get("guidance_scale", spec.get("embedded_guidance_scale", "-"))
        print(f"  [{rid:>2s}] {marker} | {spec['model_type']:28s} | steps={str(steps):2s}, cfg={str(cfg_val):4s} | {spec.get('name', '')}")

    print("\n" + "-" * 88)
    print(f"AVAILABLE VIDEO RENDERERS (default_video_renderer = {ctx.default_video_renderer}):")
    print("-" * 88)
    for rid in sorted(ctx.video_renderers.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        spec = ctx.video_renderers[rid]
        marker = "*DEFAULT*" if rid == str(ctx.default_video_renderer) else "         "
        steps = spec.get("num_inference_steps", "-")
        phases = spec.get("guidance_phases", 1)
        print(f"  [{rid:>2s}] {marker} | {spec['model_type']:40s} | steps={str(steps):2s}, ph={phases} | {spec.get('name', '')}")

    print("\n" + "-" * 88)
    cur_res_num = ctx.get_resolution_number(ctx.current_resolution)
    print(f"AVAILABLE VIDEO RESOLUTIONS (current = #{cur_res_num} [{ctx.current_resolution}]):")
    print("-" * 88)
    for r_num in sorted(ctx.video_resolutions.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        entry = ctx.video_resolutions[r_num]
        r_str = entry["resolution"] if isinstance(entry, dict) else str(entry)
        r_name = entry.get("name", "") if isinstance(entry, dict) else ""
        r_desc = entry.get("description", "") if isinstance(entry, dict) else ""
        marker = "*ACTIVE* " if str(cur_res_num) == str(r_num) else "         "
        print(f"  [{r_num:>2s}] {marker} | {r_str:10s} | {r_name:32s} | {r_desc}")

    print("\n" + "-" * 88)
    print(f"ANCHOR IMAGES ({len(ctx.anchors)} total in {ctx.anchors_dir}):")
    print("-" * 88)
    for sp in ctx.anchors:
        aid, target_path = ctx.resolve_anchor_path(sp)
        fn = target_path.name
        rid, rspec = ctx.resolve_anchor_renderer(sp)
        ovr = f"#{rid}(ovr)" if sp.get("renderer") is not None else f"#{rid}(def)"
        used_by = [sc["id"] for sc in ctx.scenes if extract_anchor_id(sc["anchor"]) == aid]
        exists = "OK" if target_path.exists() else "MISSING"
        print(f"  {aid:4s} | {ovr:8s} {rspec['model_type']:24s} | seed={sp['seed']} | [{exists}] {fn:52s} -> {', '.join(used_by)}")

    print("\n" + "-" * 88)
    print(f"VIDEO SCENES ({len(ctx.scenes)} total in {ctx.renders_dir}):")
    print("-" * 88)
    for sc in ctx.scenes:
        sid = sc["id"]
        dur = sc["end"] - sc["start"]
        aid, apath = ctx.resolve_anchor_path(sc["anchor"])
        mode = sc["mode"]
        vrid, vrspec = ctx.resolve_video_renderer(sc)
        vovr = f"#{vrid}(ovr)" if sc.get("renderer") is not None else f"#{vrid}(def)"
        print(f"  {sid:4s} | {vovr:8s} {vrspec['model_type']:28s} | {sc['start']:6.2f}s - {sc['end']:6.2f}s ({dur:5.2f}s) | Anchor: {aid:4s} | {mode}")
    print("=" * 88 + "\n")


def is_project_config(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and "project" in data
    except Exception:
        return False


def resolve_project_config(arg: str | None) -> Path | None:
    if arg:
        raw_p = Path(arg)
        if raw_p.is_file() and is_project_config(raw_p):
            return raw_p.resolve()
        if raw_p.with_suffix(".json").is_file() and is_project_config(raw_p.with_suffix(".json")):
            return raw_p.with_suffix(".json").resolve()

        ws_p = WORKSPACE_ROOT / arg
        if ws_p.is_file() and is_project_config(ws_p):
            return ws_p.resolve()
        if ws_p.with_suffix(".json").is_file() and is_project_config(ws_p.with_suffix(".json")):
            return ws_p.with_suffix(".json").resolve()

        proj_p = WORKSPACE_ROOT / "projects" / arg
        if proj_p.is_file() and is_project_config(proj_p):
            return proj_p.resolve()
        if proj_p.with_suffix(".json").is_file() and is_project_config(proj_p.with_suffix(".json")):
            return proj_p.with_suffix(".json").resolve()

        for cand_dir in [raw_p, ws_p, proj_p]:
            if cand_dir.is_dir():
                jsons = [j for j in sorted(cand_dir.glob("*.json")) if is_project_config(j)]
                if jsons:
                    return jsons[-1].resolve()

        return None

    # Fallback when no arg is given
    cwd_jsons = [j for j in sorted(Path.cwd().glob("*.json")) if is_project_config(j) and j.name not in ("model_profiles.json", "project_template.json")]
    if len(cwd_jsons) == 1:
        return cwd_jsons[0].resolve()

    proj_dir = WORKSPACE_ROOT / "projects"
    if proj_dir.is_dir():
        proj_folders = [d for d in proj_dir.iterdir() if d.is_dir()]
        if len(proj_folders) == 1:
            jsons = [j for j in sorted(proj_folders[0].glob("*.json")) if is_project_config(j)]
            if jsons:
                return jsons[-1].resolve()

    return None


def parse_cli_and_config(argv: list[str]):
    json_arg: str | None = None
    cli_renderer: str | None = None
    cli_video_renderer: str | None = None
    cli_resolution: str | None = None
    cli_profiles: str | None = None
    cli_import_dir: str | None = None
    cli_skip_anchors: bool = False
    other_tokens: list[str] = []

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--config", "-c") and i + 1 < len(argv):
            json_arg = argv[i + 1]
            i += 2
            continue
        if tok in ("--profiles", "--profiles-file", "-p") and i + 1 < len(argv):
            cli_profiles = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--profiles=") or tok.lower().startswith("--profiles-file="):
            cli_profiles = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--renderer", "--anchor-renderer", "-r") and i + 1 < len(argv):
            cli_renderer = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--renderer=") or tok.lower().startswith("--anchor-renderer="):
            cli_renderer = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--video-renderer", "--vrenderer", "-vr") and i + 1 < len(argv):
            cli_video_renderer = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--video-renderer=") or tok.lower().startswith("--vrenderer="):
            cli_video_renderer = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--res", "--resolution") and i + 1 < len(argv):
            cli_resolution = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--res=") or tok.lower().startswith("--resolution="):
            cli_resolution = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in ("--import-anchors", "--import", "-ia") and i + 1 < len(argv):
            cli_import_dir = argv[i + 1]
            i += 2
            continue
        if tok.lower().startswith("--import-anchors=") or tok.lower().startswith("--import="):
            cli_import_dir = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.lower() in ("--skip-anchors", "--no-anchors", "--video-only", "-vo"):
            cli_skip_anchors = True
            i += 1
            continue
        if tok.lower().endswith(".json") and json_arg is None:
            json_arg = tok
            i += 1
            continue
        other_tokens.append(tok)
        i += 1

    if json_arg is None:
        cand = None
        if other_tokens:
            cand = resolve_project_config(other_tokens[0])
            if cand:
                json_path = cand
                other_tokens.pop(0)
        if cand is None:
            cand = resolve_project_config(None)
            if cand:
                json_path = cand
            else:
                raise SystemExit(
                    "Usage: render_video.bat <project_name / config.json> [options / Anchor IDs / Scene IDs]\n"
                    "Example: render_video.bat all_or_nothing --list\n"
                    "         render_video.bat --new my_new_song"
                )
    else:
        cand = resolve_project_config(json_arg)
        if cand:
            json_path = cand
        else:
            raise FileNotFoundError(f"Configuration JSON or project not found: {json_arg}")

    ctx = ProjectContext(json_path, profiles_path=Path(cli_profiles) if cli_profiles else None)
    if cli_renderer is not None:
        ctx.default_anchor_renderer = str(cli_renderer).strip()
    if cli_video_renderer is not None:
        ctx.default_video_renderer = str(cli_video_renderer).strip()
    if cli_resolution is not None:
        ctx.set_resolution(cli_resolution)
    rc = ctx.run_controls

    force_anchors = [str(x) for x in rc.get("force_rerender_anchors", [])]
    force_scenes = [str(x) for x in rc.get("force_rerender_scenes", [])]
    anchors_only = bool(rc.get("anchors_only", False))
    cascade_anchors = bool(rc.get("cascade_anchors", True))
    stitch_only = bool(rc.get("stitch_only", False))
    skip_anchors = bool(rc.get("skip_anchors", False)) or cli_skip_anchors
    sync_only = False
    list_only = False
    dummy_render = (
        bool(sys.modules.get("os", {}).environ.get("DUMMY_RENDER", "0") in ("1", "true", "yes"))
        or bool(rc.get("dummy_render", False))
    )

    valid_scene_ids = {sc["id"].upper() for sc in ctx.scenes}

    for raw in other_tokens:
        t = raw.strip()
        if not t:
            continue
        tu = t.upper()
        if tu in ("--LIST", "-L", "LIST"):
            list_only = True
        elif tu in ("--DUMMY-RENDER", "--DUMMY", "--MOCK", "-M"):
            dummy_render = True
        elif tu in ("--SYNC-HASHES", "SYNC-HASHES"):
            sync_only = True
        elif tu in ("--STITCH-ONLY", "--STITCH", "STITCH"):
            stitch_only = True
        elif tu in ("--ANCHORS-ONLY", "ANCHORS-ONLY", "ANCHORS"):
            anchors_only = True
        elif tu in ("--SKIP-ANCHORS", "SKIP-ANCHORS", "--VIDEO-ONLY", "VIDEO-ONLY"):
            skip_anchors = True
        elif tu in ("--NO-CASCADE", "NO-CASCADE"):
            cascade_anchors = False
        elif tu in ("--NO-STITCH", "NO-STITCH"):
            ctx.no_stitch = True
        elif tu in ("--NO-QUEUE-SYNC", "NO-QUEUE-SYNC"):
            ctx.no_queue_sync = True
        elif tu in ("--ALL-ANCHORS", "ALL-ANCHORS"):
            force_anchors.append("ALL")
        elif tu in ("--ALL-SCENES", "ALL-SCENES", "--ALL", "ALL"):
            force_scenes.append("ALL")
        elif any(match_anchor_id(tu, sp.get("filename") or sp.get("id") or "") for sp in ctx.anchors):
            force_anchors.append(tu)
        elif any(_scene_id_matches(tu.split(":", 1)[0], sc_id) for sc_id in valid_scene_ids):
            force_scenes.append(tu)
        else:
            print(f"[Warning] Unrecognized argument '{raw}' (use --list to see valid Anchor/Scene IDs)", flush=True)

    return (
        ctx,
        force_anchors,
        force_scenes,
        anchors_only,
        cascade_anchors,
        stitch_only,
        sync_only,
        list_only,
        cli_import_dir,
        skip_anchors,
        dummy_render,
    )


def init_new_project(target_name: str, force: bool = False):
    target_raw = Path(target_name)
    if target_raw.suffix.lower() == ".json":
        slug = target_raw.stem
        parent_dir = target_raw.parent
    else:
        slug = target_raw.name
        parent_dir = target_raw.parent

    if parent_dir == Path(".") or str(parent_dir) in ("", "."):
        project_dir = (WORKSPACE_ROOT / "projects" / slug).resolve()
    else:
        project_dir = (parent_dir / slug).resolve() if not target_raw.name.endswith(".json") else parent_dir.resolve()

    json_path = project_dir / f"{slug}.json"

    if json_path.exists() and not force:
        print(f"\n[Error] Project '{slug}' already exists at {project_dir}!", flush=True)
        print(f"To overwrite, use: render_video.bat --new {slug} --force\n", flush=True)
        sys.exit(1)

    template_candidates = [
        WORKSPACE_ROOT / "project_template.json",
        Path.cwd() / "project_template.json",
    ]
    template_data = None
    for cand in template_candidates:
        if cand.exists():
            try:
                template_data = json.loads(cand.read_text(encoding="utf-8"))
                break
            except Exception:
                pass

    if template_data is None:
        print("[Error] Could not locate 'project_template.json' template.", flush=True)
        sys.exit(1)

    dirs_to_create = [
        project_dir,
        project_dir / "audio",
        project_dir / "references",
        project_dir / "work",
        project_dir / "work" / "anchors",
        project_dir / "work" / "audio",
        project_dir / "work" / "jobs",
        project_dir / "work" / "masks",
        project_dir / "work" / "renders",
        project_dir / "work" / "trimmed",
        project_dir / "work" / "frames",
    ]
    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)

    audio_readme = project_dir / "audio" / "README.txt"
    if not audio_readme.exists():
        audio_readme.write_text(
            f"Audio Files for Project: {slug}\n"
            "============================================================================\n"
            "Place your song audio files here:\n"
            "  1. master_audio.mp3  - Full song audio used for the final master video\n"
            "  2. lead_vocal.mp3    - Isolated vocal stem used for lipsync alignment\n"
            "  3. backing_track.mp3 - (Optional) Instrumental / backing track\n\n"
            f"Update paths in {slug}.json under 'project.master_audio' and 'project.stems'.\n",
            encoding="utf-8",
        )

    ref_readme = project_dir / "references" / "README.txt"
    if not ref_readme.exists():
        ref_readme.write_text(
            f"Reference Images for Project: {slug}\n"
            "============================================================================\n"
            "Place character reference images and visual style sheets here.\n"
            f"Reference them in {slug}.json under 'models.anchor_shared_settings.image_refs'.\n",
            encoding="utf-8",
        )

    local_bat = project_dir / "render.bat"
    local_bat.write_text(
        "@echo off\n"
        "setlocal\n"
        'cd /d "%~dp0"\n'
        'for %%I in ("%~dp0..\\..") do set "ROOT_DIR=%%~fI"\n'
        f'call "%ROOT_DIR%\\render_video.bat" "%~dp0{slug}.json" %*\n'
        "exit /b %ERRORLEVEL%\n",
        encoding="utf-8",
    )

    clean_name = slug.replace("_", " ").replace("-", " ").title()
    safe_name = re.sub(r"[^\w\-]+", "_", slug).strip("_")

    template_data.setdefault("project", {})["name"] = clean_name
    template_data["project"]["project_dir"] = "."
    template_data["project"]["master_output"] = f"{safe_name}_Master_{{resolution}}_{{fps}}fps.mp4"
    template_data["project"]["master_audio"] = "audio/master_audio.mp3"
    template_data["project"]["stems"] = {
        "lead_vocal": "audio/lead_vocal.mp3",
        "backing_track": "audio/backing_track.mp3",
    }
    template_data["project"]["directories"] = {
        "anchors": "work/anchors",
        "audio_slices": "work/audio",
        "masks": "work/masks",
        "jobs": "work/jobs",
        "renders": "work/renders",
        "trimmed": "work/trimmed",
        "review_strips": "work/frames",
    }
    template_data["profiles_file"] = "model_profiles.json"

    json_path.write_text(json.dumps(template_data, indent=2), encoding="utf-8")

    rel_proj = project_dir.relative_to(WORKSPACE_ROOT) if project_dir.is_relative_to(WORKSPACE_ROOT) else project_dir

    print("\n" + "=" * 76, flush=True)
    print(f"  FRESH PROJECT FOLDER STRUCTURE CREATED: {rel_proj}/", flush=True)
    print("=" * 76, flush=True)
    print(f"  Project Directory : {project_dir}", flush=True)
    print(f"  Configuration     : {json_path.name}", flush=True)
    print(f"  Audio Folder      : {project_dir / 'audio'}", flush=True)
    print(f"  References Folder : {project_dir / 'references'}", flush=True)
    print(f"  Work / Caches     : {project_dir / 'work'}", flush=True)
    print(f"  Local Launcher    : {project_dir / 'render.bat'}", flush=True)
    print("-" * 76, flush=True)
    print("  Next Steps:", flush=True)
    print(f"  1. Place master song audio at: {rel_proj}/audio/master_audio.mp3", flush=True)
    print(f"     (and isolated vocal stem at: {rel_proj}/audio/lead_vocal.mp3)", flush=True)
    print(f"  2. Edit {rel_proj}/{json_path.name} to customize characters and prompts", flush=True)
    print(f"  3. Preview timeline:   render_video.bat {slug} --list", flush=True)
    print(f"  4. Render video:       render_video.bat {slug}", flush=True)
    print(f"     (or inside {rel_proj}/, simply run: render.bat)", flush=True)
    print("=" * 76 + "\n", flush=True)


def main():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.lower() in ("--new", "new", "--init", "init"):
            force = any(x.lower() in ("--force", "-f") for x in args)
            remaining = [x for j, x in enumerate(args) if j != i and not x.startswith("-")]
            target = remaining[0] if remaining else "new_project.json"
            init_new_project(target, force=force)
            return

    t_start = time.time()
    (
        ctx,
        force_anchors,
        force_scenes,
        anchors_only,
        cascade_anchors,
        stitch_only,
        sync_only,
        list_only,
        cli_import_dir,
        skip_anchors,
        dummy_render,
    ) = parse_cli_and_config(args)

    if list_only:
        print_catalog(ctx)
        return

    print(f"[Config] Loaded project '{ctx.project_name}' from: {ctx.json_path}", flush=True)
    state = ctx.load_state_hashes()
    lazy_session = LazyWgpSession(ctx)

    if sync_only:
        run_stage1_anchors(ctx, lazy_session, state, [], sync_only=True)
        run_stage2_scenes(ctx, lazy_session, state, [], set(), sync_only=True)
        print(f"[Sync] Baseline {ctx.state_hash_file} synced with current JSON state.", flush=True)
        return

    if stitch_only:
        print("\n[Stitch] Immediate master restitch requested. Skipping anchor and audio prep...", flush=True)
        timeline_file = ctx.jobs_dir / "v3_timeline.json"
        scene_meta = None
        if timeline_file.is_file():
            try:
                scene_meta = json.loads(timeline_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        if not scene_meta:
            scene_meta = []
            res_str = ctx.resolve_resolution(ctx.current_resolution)
            for sc in ctx.scenes:
                sid = sc["id"]
                start = float(sc["start"])
                end = float(sc["end"])
                edit_dur = round(end - start, 3)
                mode = sc["mode"]
                if mode in ("gva_male_left_silent_right", "duet_method_b", "two_pass_gva"):
                    out_clip = f"{sid}_Pass2_Final_{res_str}.mp4"
                elif mode == "female_solo":
                    out_clip = f"{sid}_female_solo_{res_str}.mp4"
                elif mode == "silent_locked":
                    out_clip = f"{sid}_silent_{res_str}.mp4"
                else:
                    out_clip = f"{sid}_male_solo_{res_str}.mp4"
                scene_meta.append({
                    "id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode,
                    "clip": out_clip, "render_start": start, "render_end": end,
                    "anchor": sc.get("anchor"),
                })
        run_stage3_assemble_master(ctx, scene_meta)
        print(f"\n[Stitch] Master video restitched in {time.time() - t_start:.1f}s.", flush=True)
        return

    imported_anchors: set[str] = set()
    try:
        if cli_import_dir:
            imported_anchors = import_anchors_from_dir(ctx, cli_import_dir, state)

        if skip_anchors or (force_scenes and not force_anchors):
            print("\n[Anchors] Skipping Stage 1 anchor checks (single-scene render mode).", flush=True)
            regen_anchors = set(imported_anchors)
        else:
            regen_anchors = run_stage1_anchors(ctx, lazy_session, state, force_anchors, dummy_render=dummy_render).union(imported_anchors)

        if anchors_only:
            write_engine_status(ctx, {"state": "idle"})
            print(f"\n[Anchors-Only Mode] Finished anchor processing in {time.time() - t_start:.1f}s.", flush=True)
            print(f"Preview your updated anchors in: {ctx.anchors_dir}", flush=True)
            return

        scene_meta = run_stage2_scenes(
            ctx,
            lazy_session,
            state,
            force_scenes,
            regen_anchors,
            cascade_anchors=cascade_anchors,
            stitch_only=stitch_only,
            dummy_render=dummy_render,
            no_queue_sync=bool(force_scenes) or getattr(ctx, "no_queue_sync", False),
            no_stitch=getattr(ctx, "no_stitch", False),
        )
        if not getattr(ctx, "no_stitch", False):
            run_stage3_assemble_master(ctx, scene_meta)
        write_engine_status(ctx, {"state": "idle"})
        print(f"\nPipeline finished in {(time.time() - t_start) / 60:.1f} minutes.", flush=True)
    finally:
        lazy_session.close()
