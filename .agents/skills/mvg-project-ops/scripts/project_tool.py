#!/usr/bin/env python3
"""MVG Project Operations & Integrity Tool.

Provides fast inspection, auditing, and safe JSON editing for Studio projects without
forcing models to read multi-megabyte project configurations.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
PROJECTS_DIR = WORKSPACE_ROOT / "projects"
BACKUP_DIR = WORKSPACE_ROOT / "backup"


def find_latest_project_json(slug: str) -> tuple[Path, dict] | tuple[None, None]:
    pdir = PROJECTS_DIR / slug
    if not pdir.is_dir():
        return None, None
    jsons = [j for j in sorted(pdir.glob("*.json")) if not j.name.startswith("state_hashes")]
    if not jsons:
        return None, None
    latest = jsons[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        return latest, data
    except Exception:
        return latest, {}


def backup_file(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"{path.stem}_{ts}.json"
    shutil.copy2(path, dest)
    return dest


def cmd_list(args):
    print("=" * 80)
    print(f"  Available Projects in {PROJECTS_DIR}")
    print("=" * 80)
    if not PROJECTS_DIR.is_dir():
        print("  No projects directory found.")
        return

    found = False
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir():
            pjson, data = find_latest_project_json(d.name)
            if pjson and data:
                found = True
                pcfg = data.get("project", {})
                name = pcfg.get("name") or d.name
                dur = pcfg.get("total_duration", 0)
                scenes = len(data.get("scenes", []))
                anchors = len(data.get("anchors", []))
                print(f"📁 {d.name:<25} | '{name}' | {dur:.1f}s | {scenes} scenes | {anchors} anchors ({pjson.name})")

    if not found:
        print("  No valid project JSON files found.")
    print("-" * 80)


def _extract_aid(val: str) -> str:
    m = re.match(r"^(A\d+)", str(val or "").strip(), re.IGNORECASE)
    return m.group(1).upper() if m else str(val or "").strip()


def cmd_summary(args):
    pjson, data = find_latest_project_json(args.slug)
    if not pjson or not data:
        print(f"❌ Error: Could not find valid project JSON for slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)

    pcfg = data.get("project", {})
    scenes = data.get("scenes", [])
    anchors = data.get("anchors", [])
    markers = data.get("markers", [])
    models = data.get("models", {})

    print("=" * 80)
    print(f"  Project Summary: {pcfg.get('name', args.slug)} ({args.slug})")
    print(f"  Config File    : {pjson.name}")
    print("=" * 80)
    print(f"  Total Duration : {pcfg.get('total_duration', 0):.2f}s (FPS: {pcfg.get('fps', 24)})")
    print(f"  Master Res     : {pcfg.get('master_resolution', models.get('default_video_resolution', '1344x768'))}")
    print(f"  Audio File     : {pcfg.get('audio', pcfg.get('master_audio', 'N/A'))}")
    print(f"  Default Model  : {models.get('default_model', models.get('default_video_renderer', 'N/A'))}")
    print(f"  Anchors ({len(anchors)}) : {', '.join(_extract_aid(a.get('id') or a.get('filename', '?')) for a in anchors)}")
    print(f"  Markers ({len(markers)}) : {len(markers)} timeline cue points")
    print(f"  Total Scenes   : {len(scenes)}")

    solo_count = sum(1 for s in scenes if "solo" in s.get("mode", "solo") or s.get("mode") == "silent_locked")
    duet_count = sum(1 for s in scenes if "duet" in s.get("mode", "") or "two_pass" in s.get("mode", "") or "gva" in s.get("mode", ""))
    print(f"    - Solo Scenes: {solo_count}")
    print(f"    - Duet Scenes: {duet_count}")

    pdir = PROJECTS_DIR / args.slug
    manifest_file = pdir / "work" / "takes_manifest.json"
    if manifest_file.is_file():
        try:
            m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            scs = m_data.get("scenes", {})
            total_takes = sum(len(s.get("takes", [])) for s in scs.values())
            print(f"  Render Vault   : {total_takes} takes recorded across {len(scs)} scenes")
        except Exception:
            pass

    ckpts_file = pdir / "checkpoints" / "index.json"
    if ckpts_file.is_file():
        try:
            c_data = json.loads(ckpts_file.read_text(encoding="utf-8"))
            print(f"  Checkpoints    : {len(c_data.get('checkpoints', []))} saved milestones")
        except Exception:
            pass

    chars_file = pdir / "characters.json"
    if chars_file.is_file():
        try:
            ch_data = json.loads(chars_file.read_text(encoding="utf-8"))
            print(f"  Characters     : {len(ch_data.get('characters', []))} registered character tags")
        except Exception:
            pass

    print("-" * 80)


def cmd_audit(args):
    pjson, data = find_latest_project_json(args.slug)
    if not pjson or not data:
        print(f"❌ Error: Could not find valid project JSON for slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)

    pdir = PROJECTS_DIR / args.slug
    scenes = data.get("scenes", [])
    anchors: dict[str, dict] = {}
    for a in data.get("anchors", []):
        for k in (a.get("id"), a.get("filename"), _extract_aid(a.get("id") or a.get("filename", ""))):
            if k:
                anchors[str(k)] = a
    issues: list[str] = []
    warnings: list[str] = []

    print("=" * 80)
    print(f"  Auditing Project Integrity: {args.slug} ({pjson.name})")
    print("=" * 80)

    # 1. Timeline continuity
    for i in range(len(scenes) - 1):
        cur = scenes[i]
        nxt = scenes[i + 1]
        c_end = float(cur.get("end", 0))
        n_start = float(nxt.get("start", 0))
        diff = n_start - c_end
        if abs(diff) > 0.05:
            if diff > 0:
                issues.append(f"Timeline Gap of {diff:.2f}s between {cur.get('id')} (end {c_end}s) and {nxt.get('id')} (start {n_start}s)")
            else:
                issues.append(f"Timeline Overlap of {abs(diff):.2f}s between {cur.get('id')} (end {c_end}s) and {nxt.get('id')} (start {n_start}s)")

    # 2. Audio stem checks
    audio_dir = pdir / "audio"
    available_stems = set()
    if audio_dir.is_dir():
        available_stems = {f.name.lower() for f in audio_dir.iterdir() if f.is_file()}

    for s in scenes:
        sid = s.get("id", "?")
        mode = s.get("mode", "solo")
        if "duet" in mode or "two_pass" in mode or "gva" in mode:
            stems_to_check = [("stem_m", s.get("stem_m")), ("stem_f", s.get("stem_f"))]
        else:
            stems_to_check = [("stem", s.get("stem"))]

        for role, st in stems_to_check:
            if st and st != "silent" and available_stems:
                clean_st = Path(st).name.lower()
                if clean_st not in available_stems and f"{clean_st}.mp3" not in available_stems:
                    warnings.append(f"Scene {sid} {role} '{st}' not found in {audio_dir.name}/")

        # Check render padding
        for pk in ("render_pad_start", "pad_start", "head_overlap"):
            if s.get(pk) is not None:
                val = float(s[pk])
                if val < 0:
                    warnings.append(f"Scene {sid} {pk} is negative ({val}s)")
                elif float(s.get("start", 0)) - val < -0.001:
                    warnings.append(f"Scene {sid} render_pad_start ({val}s) exceeds scene start ({s.get('start', 0)}s); will clamp to 0.0s")
                break
        for pk in ("render_pad_end", "pad_end", "tail_overlap"):
            if s.get(pk) is not None:
                val = float(s[pk])
                if val < 0:
                    warnings.append(f"Scene {sid} {pk} is negative ({val}s)")
                elif s.get("anchor_end") and val > 0:
                    warnings.append(f"Scene {sid} sets both anchor_end and render_pad_end ({val}s); end anchor frame will be trimmed in master")
                break

    # 3. Anchor references & disk existence
    cfg_anchors_rel = data.get("project", {}).get("directories", {}).get("anchors", "work/anchors")
    anchor_dirs = [d for d in (pdir / cfg_anchors_rel, pdir / "work" / "anchors_v3", pdir / "work" / "anchors", pdir / "anchors") if d.is_dir()]
    for s in scenes:
        sid = s.get("id", "?")
        for aid_key in ("anchor", "anchor_end", "anchor_female"):
            aid_val = s.get(aid_key)
            if not aid_val:
                continue
            aid_short = _extract_aid(aid_val)
            if aid_val not in anchors and aid_short not in anchors:
                warnings.append(f"Scene {sid} references undefined {aid_key} '{aid_val}'")
            elif anchor_dirs:
                found_on_disk = any(
                    (adir / aid_val).is_file() or any(f.is_file() for f in adir.glob(f"{aid_short}*"))
                    for adir in anchor_dirs
                )
                if not found_on_disk:
                    warnings.append(f"Scene {sid} {aid_key} '{aid_val}' file not found on disk")

    # 4. Render coverage check
    renders_dir = pdir / "work" / "renders_v3"
    trimmed_dir = pdir / "work" / "trimmed_v3"
    unrendered_count = 0
    for s in scenes:
        sid = s.get("id", "")
        # Look for any matching rendered MP4
        found_clip = False
        for cdir in (renders_dir, trimmed_dir):
            if cdir.is_dir():
                for c in cdir.glob("*.mp4"):
                    if re.search(rf"(?:^|_)({re.escape(sid)})(?:[_\.]|$)", c.name, re.IGNORECASE):
                        found_clip = True
                        break
            if found_clip:
                break
        if not found_clip:
            unrendered_count += 1

    # Output report
    if not issues and not warnings:
        print("  ✅ All checks passed! No timeline gaps, missing stems, or undefined anchors.")
    else:
        if issues:
            print(f"  ❌ {len(issues)} Timeline Continuity Issues:")
            for iss in issues:
                print(f"     - {iss}")
        if warnings:
            print(f"  ⚠️ {len(warnings)} Warnings / Missing Assets:")
            for w in warnings:
                print(f"     - {w}")

    print(f"\n  Render Status: {len(scenes) - unrendered_count}/{len(scenes)} scenes rendered ({unrendered_count} pending)")
    print("-" * 80)


def cmd_show_scene(args):
    pjson, data = find_latest_project_json(args.slug)
    if not pjson or not data:
        print(f"❌ Error: Could not find valid project JSON for slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)

    target_id = args.scene_id.strip().upper()
    scene = next((s for s in data.get("scenes", []) if s.get("id", "").upper() == target_id), None)
    if not scene:
        # Try zero-padded match: e.g. S1 -> S01
        m = re.match(r"^S0*(\d+)$", target_id)
        if m:
            num = int(m.group(1))
            scene = next((s for s in data.get("scenes", []) if s.get("id", "").upper() == f"S{num:02d}"), None)

    if not scene:
        print(f"❌ Error: Scene '{args.scene_id}' not found in {pjson.name}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(scene, indent=2))
        return

    print("=" * 80)
    print(f"  Scene {scene.get('id')}: {scene.get('title', 'Untitled')} [{args.slug}]")
    print("=" * 80)
    print(f"  Timing   : {scene.get('start', 0)}s -> {scene.get('end', 0)}s ({float(scene.get('end', 0)) - float(scene.get('start', 0)):.2f}s duration)")
    print(f"  Mode     : {scene.get('mode', 'solo')} | Renderer: {scene.get('renderer', 'default')}")
    print(f"  Anchor   : {scene.get('anchor', 'N/A')}" + (f" | End: {scene.get('anchor_end')}" if scene.get('anchor_end') else "") + (f" | Female: {scene.get('anchor_female')}" if scene.get('anchor_female') else ""))
    if scene.get("render_pad_start") is not None or scene.get("render_pad_end") is not None:
        print(f"  Padding  : Head +{scene.get('render_pad_start', 0.0)}s | Tail +{scene.get('render_pad_end', 0.0)}s")
    print(f"  Stem     : {scene.get('stem', 'N/A')}")
    print(f"  Out Name : {scene.get('output_name', 'N/A')}")
    if "prompt_pass1" in scene:
        print(f"  Prompt P1: {scene.get('prompt_pass1')}")
        print(f"  Prompt P2: {scene.get('prompt_pass2')}")
    else:
        print(f"  Prompt   : {scene.get('prompt', 'N/A')}")
    print("-" * 80)


def cmd_set_scene(args):
    pjson, data = find_latest_project_json(args.slug)
    if not pjson or not data:
        print(f"❌ Error: Could not find valid project JSON for slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)

    target_id = args.scene_id.strip().upper()
    scene = next((s for s in data.get("scenes", []) if s.get("id", "").upper() == target_id), None)
    if not scene:
        m = re.match(r"^S0*(\d+)$", target_id)
        if m:
            num = int(m.group(1))
            scene = next((s for s in data.get("scenes", []) if s.get("id", "").upper() == f"S{num:02d}"), None)

    if not scene:
        print(f"❌ Error: Scene '{args.scene_id}' not found in {pjson.name}", file=sys.stderr)
        sys.exit(1)

    changes = []
    if args.start is not None:
        old_val = scene.get("start")
        scene["start"] = float(args.start)
        changes.append(f"start: {old_val} -> {args.start}")
    if args.end is not None:
        old_val = scene.get("end")
        scene["end"] = float(args.end)
        changes.append(f"end: {old_val} -> {args.end}")
    if args.mode is not None:
        old_val = scene.get("mode")
        scene["mode"] = args.mode
        changes.append(f"mode: {old_val} -> {args.mode}")
    if args.prompt is not None:
        old_val = scene.get("prompt")
        scene["prompt"] = args.prompt
        changes.append(f"prompt: '{old_val}' -> '{args.prompt}'")
    if args.anchor is not None:
        old_val = scene.get("anchor")
        scene["anchor"] = args.anchor
        changes.append(f"anchor: '{old_val}' -> '{args.anchor}'")
    if getattr(args, "anchor_end", None) is not None:
        old_val = scene.get("anchor_end")
        if str(args.anchor_end).lower() in ("none", "null", "clear", ""):
            scene.pop("anchor_end", None)
            new_val = None
        else:
            scene["anchor_end"] = args.anchor_end
            new_val = args.anchor_end
        changes.append(f"anchor_end: '{old_val}' -> '{new_val}'")
    if args.stem is not None:
        old_val = scene.get("stem")
        scene["stem"] = args.stem
        changes.append(f"stem: '{old_val}' -> '{args.stem}'")
    if getattr(args, "renderer", None) is not None:
        old_val = scene.get("renderer")
        new_val = None if str(args.renderer).lower() in ("null", "none", "default") else (int(args.renderer) if str(args.renderer).isdigit() else args.renderer)
        scene["renderer"] = new_val
        changes.append(f"renderer: {old_val} -> {new_val}")
    if getattr(args, "pad_start", None) is not None:
        old_val = scene.get("render_pad_start")
        if str(args.pad_start).strip().lower() in ("none", "null", "clear", "default", ""):
            scene.pop("render_pad_start", None)
            new_val = None
        else:
            val = float(args.pad_start)
            if val < 0:
                print("❌ Error: --pad-start cannot be negative", file=sys.stderr)
                sys.exit(1)
            scene["render_pad_start"] = val
            new_val = val
        changes.append(f"render_pad_start: {old_val} -> {new_val}")
    if getattr(args, "pad_end", None) is not None:
        old_val = scene.get("render_pad_end")
        if str(args.pad_end).strip().lower() in ("none", "null", "clear", "default", ""):
            scene.pop("render_pad_end", None)
            new_val = None
        else:
            val = float(args.pad_end)
            if val < 0:
                print("❌ Error: --pad-end cannot be negative", file=sys.stderr)
                sys.exit(1)
            scene["render_pad_end"] = val
            new_val = val
        changes.append(f"render_pad_end: {old_val} -> {new_val}")

    if not changes:
        print("ℹ️ No property modifications provided. Scene unchanged.")
        return

    # Backup before writing
    bak = backup_file(pjson)
    pjson.write_text(json.dumps(data, indent=2), encoding="utf-8")

    try:
        sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
        from ui.server_queue import compute_fast_project_queue
        compute_fast_project_queue(args.slug)
    except Exception:
        pass

    print(f"✅ Successfully updated scene {scene.get('id')} in {pjson.name}")
    if bak:
        print(f"   Backup written to: {bak.name}")
    for ch in changes:
        print(f"   - {ch}")


def cmd_split_scene(args):
    pjson, data = find_latest_project_json(args.slug)
    if not pjson or not data:
        print(f"❌ Error: Could not find valid project JSON for slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)

    sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
    try:
        from pipeline.context import split_scene_in_project_data
    except ImportError:
        from run_overnight_v3 import split_scene_in_project_data

    try:
        updated_data, sc1, sc2 = split_scene_in_project_data(
            data,
            args.scene_id,
            float(args.split_time),
            id1=args.id1,
            id2=args.id2,
        )
    except Exception as e:
        print(f"❌ Split Error: {e}", file=sys.stderr)
        sys.exit(1)

    bak = backup_file(pjson)
    pjson.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")

    print(f"✅ Successfully split scene {args.scene_id} in {pjson.name}:")
    print(f"   - {sc1.get('id')}: {sc1.get('start', 0):.2f}s -> {sc1.get('end', 0):.2f}s ({sc1.get('end', 0) - sc1.get('start', 0):.2f}s duration)")
    print(f"   - {sc2.get('id')}: {sc2.get('start', 0):.2f}s -> {sc2.get('end', 0):.2f}s ({sc2.get('end', 0) - sc2.get('start', 0):.2f}s duration)")
    print(f"   Backup written to: {bak.name}")


def cmd_delete_scene(args):
    pjson, data = find_latest_project_json(args.slug)
    if not pjson or not data:
        print(f"❌ Error: Could not find valid project JSON for slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)

    sys.path.insert(0, str(WORKSPACE_ROOT / "work"))
    try:
        from pipeline.context import delete_scene_in_project_data
    except ImportError:
        from run_overnight_v3 import delete_scene_in_project_data

    close_gap = not getattr(args, "no_close_gap", False)
    try:
        updated_data, deleted_sc = delete_scene_in_project_data(
            data,
            args.scene_id,
            close_gap=close_gap,
        )
    except Exception as e:
        print(f"❌ Delete Error: {e}", file=sys.stderr)
        sys.exit(1)

    bak = backup_file(pjson)
    pjson.write_text(json.dumps(updated_data, indent=2), encoding="utf-8")

    print(f"✅ Successfully deleted scene {args.scene_id} in {pjson.name}:")
    print(f"   - Removed {deleted_sc.get('id')} ({deleted_sc.get('start', 0):.2f}s -> {deleted_sc.get('end', 0):.2f}s, close_gap={close_gap})")
    print(f"   - Remaining scenes: {len(updated_data.get('scenes', []))}")
    print(f"   Backup written to: {bak.name}")


def main():
    parser = argparse.ArgumentParser(description="MVG Project Operations & Integrity Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    subparsers.add_parser("list", help="List all available projects")

    # summary
    p_sum = subparsers.add_parser("summary", help="Show project summary")
    p_sum.add_argument("slug", help="Project slug (e.g. all_or_nothing)")

    # audit
    p_aud = subparsers.add_parser("audit", help="Audit project timeline, stems, and anchors")
    p_aud.add_argument("slug", help="Project slug")

    # show-scene
    p_show = subparsers.add_parser("show-scene", help="Display details of a single scene")
    p_show.add_argument("slug", help="Project slug")
    p_show.add_argument("scene_id", help="Scene ID (e.g. S01, S12a)")
    p_show.add_argument("--json", action="store_true", help="Output as JSON")

    # set-scene
    p_set = subparsers.add_parser("set-scene", help="Safely edit scene properties")
    p_set.add_argument("slug", help="Project slug")
    p_set.add_argument("scene_id", help="Scene ID")
    p_set.add_argument("--start", type=float, help="Start time in seconds")
    p_set.add_argument("--end", type=float, help="End time in seconds")
    p_set.add_argument("--mode", help="Generation mode")
    p_set.add_argument("--prompt", help="Prompt text")
    p_set.add_argument("--anchor", help="Anchor ID (e.g. A01)")
    p_set.add_argument("--anchor-end", help="End Anchor ID (e.g. A02) or 'none'/'clear' to remove")
    p_set.add_argument("--stem", help="Audio stem filename")
    p_set.add_argument("--renderer", help="Renderer profile index or ID (e.g. 1, 9, or null/default)")
    p_set.add_argument("--pad-start", help="Head render padding in seconds, or 'none'/'clear'/'default' to remove")
    p_set.add_argument("--pad-end", help="Tail render padding in seconds, or 'none'/'clear'/'default' to remove")

    # split-scene
    p_split = subparsers.add_parser("split-scene", help="Split a scene into two sequential sub-scenes")
    p_split.add_argument("slug", help="Project slug")
    p_split.add_argument("scene_id", help="Scene ID to split (e.g. S02, S12)")
    p_split.add_argument("split_time", type=float, help="Split time in seconds (must be within scene start and end)")
    p_split.add_argument("--id1", help="Optional custom ID for the first part (defaults to auto e.g. S02a)")
    p_split.add_argument("--id2", help="Optional custom ID for the second part (defaults to auto e.g. S02b)")

    # delete-scene
    p_del = subparsers.add_parser("delete-scene", help="Delete a scene and close timeline gap")
    p_del.add_argument("slug", help="Project slug")
    p_del.add_argument("scene_id", help="Scene ID to delete (e.g. S02, S12a)")
    p_del.add_argument("--no-close-gap", action="store_true", help="Do not automatically adjust adjacent scene start/end")

    args = parser.parse_args()
    if args.command == "list":
        cmd_list(args)
    elif args.command == "summary":
        cmd_summary(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "show-scene":
        cmd_show_scene(args)
    elif args.command == "set-scene":
        cmd_set_scene(args)
    elif args.command == "split-scene":
        cmd_split_scene(args)
    elif args.command == "delete-scene":
        cmd_delete_scene(args)


if __name__ == "__main__":
    main()
