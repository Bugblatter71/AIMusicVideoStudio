"""Cleanup and isolation of takes, active clips, strips, and state hashes for deleted or replaced scenes."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterable

from .media_utils import atomic_write_json, locked_file_op
from .project_paths import (
    resolve_project_dirs,
    resolve_queue_file,
    resolve_state_hashes_paths,
    resolve_vault_media_path,
)
from .takes import (
    get_state_hashes_file,
    get_takes_manifest_path,
    load_takes_manifest,
    safe_link_or_copy,
    save_state_hashes_data,
    save_takes_manifest,
)

_TRANSIENT_SCENE_KEYS = ("_freshSceneTakes", "_dupPending", "_lockPending")


def _is_scene_artifact_match(name: str, sid_u: str, known_out_names: set[str]) -> bool:
    """Returns True if filename or stem strictly belongs to `sid_u` (never matches e.g. S01a for S01)."""
    p = Path(name)
    stem, stem_u = p.stem, p.stem.upper()
    if stem in known_out_names or stem_u in known_out_names:
        return True
    if stem_u == sid_u or stem_u.startswith(f"{sid_u}_"):
        return True
    return bool(re.match(rf"^(?:\d+_)?{re.escape(sid_u)}(?:_|\.)", p.name, re.IGNORECASE))


def _collect_checkpoint_take_refs(proj_dir: Path, target_uppers: set[str] | None = None) -> set[str]:
    """Returns take_ids referenced by any existing checkpoint's active_takes.json."""
    refs: set[str] = set()
    ckpts_dir = Path(proj_dir) / "checkpoints"
    if not ckpts_dir.is_dir():
        return refs
    for act_file in ckpts_dir.glob("*/active_takes.json"):
        try:
            amap = json.loads(act_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(amap, dict):
            continue
        for sid, pmap in amap.items():
            if target_uppers and str(sid).strip().upper() not in target_uppers:
                continue
            if isinstance(pmap, dict):
                for tid in pmap.values():
                    if tid:
                        refs.add(str(tid))
    return refs


def collect_orphaned_manifest_scene_ids(
    proj_dir: Path,
    current_scenes: list[dict] | None,
    *,
    config_data: dict | None = None,
) -> list[str]:
    """Lists scene keys in takes_manifest or renders/takes/ no longer present in `current_scenes`."""
    proj_dir = Path(proj_dir)
    active_u = {str(s.get("id") or "").strip().upper() for s in (current_scenes or []) if isinstance(s, dict) and s.get("id")}
    orphans: list[str] = []
    seen_u: set[str] = set()
    mf = load_takes_manifest(proj_dir)
    for k in (mf.get("scenes") or {}):
        ku = str(k).strip().upper()
        if ku and ku not in active_u and ku not in seen_u:
            seen_u.add(ku)
            orphans.append(str(k))
    takes_root = resolve_project_dirs(proj_dir, config_data=config_data)["renders"] / "takes"
    if takes_root.is_dir():
        for d in sorted(takes_root.iterdir()):
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            du = d.name.strip().upper()
            if du not in active_u and du not in seen_u:
                seen_u.add(du)
                orphans.append(d.name)
    return orphans


def _archive_take_for_checkpoint(renders_dir: Path, tk: dict, proj_dir: Path, manifest: dict) -> None:
    """Moves checkpoint-referenced take files out of active renders/takes/<sid>/ into _checkpoint_archive/."""
    tid = str(tk.get("take_id") or "")
    if not tid:
        return
    arch_dir = renders_dir / "_checkpoint_archive" / tid
    v_src = resolve_vault_media_path(proj_dir, renders_dir, tk)
    s_src = resolve_vault_media_path(proj_dir, renders_dir, tk, path_key="strip_path", rel_key="vault_rel_strip")
    rec = dict(tk)
    if v_src and v_src.is_file():
        arch_dir.mkdir(parents=True, exist_ok=True)
        v_dst = arch_dir / v_src.name
        safe_link_or_copy(v_src, v_dst)
        v_src.unlink(missing_ok=True)
        rec["vault_rel_mp4"] = f"_checkpoint_archive/{tid}/{v_dst.name}"
        rec["file_path"] = None
    if s_src and s_src.is_file():
        arch_dir.mkdir(parents=True, exist_ok=True)
        s_dst = arch_dir / s_src.name
        safe_link_or_copy(s_src, s_dst)
        s_src.unlink(missing_ok=True)
        rec["vault_rel_strip"] = f"_checkpoint_archive/{tid}/{s_dst.name}"
        rec["strip_path"] = None
    manifest.setdefault("archived_takes", {})[tid] = rec


def restore_archived_takes_for_checkpoint(proj_dir: Path, manifest: dict, active_map: dict, renders_dir: Path) -> bool:
    """Restores archived deleted-scene takes referenced by a checkpoint back into active scenes/vault."""
    archived = manifest.get("archived_takes")
    if not isinstance(archived, dict) or not archived or not isinstance(active_map, dict):
        return False
    changed = False
    scenes_map = manifest.setdefault("scenes", {})
    for sid, pmap in active_map.items():
        if not isinstance(pmap, dict):
            continue
        sc_entry = scenes_map.setdefault(sid, {"active_takes": {}, "takes": []})
        takes = sc_entry.setdefault("takes", [])
        known_tids = {t.get("take_id") for t in takes}
        for tid in pmap.values():
            if not tid or tid in known_tids or tid not in archived:
                continue
            tk = dict(archived.pop(tid))
            dst_dir = renders_dir / "takes" / sid
            dst_dir.mkdir(parents=True, exist_ok=True)
            v_src = resolve_vault_media_path(proj_dir, renders_dir, tk)
            if v_src and v_src.is_file():
                v_dst = dst_dir / v_src.name
                safe_link_or_copy(v_src, v_dst)
                v_src.unlink(missing_ok=True)
                tk["vault_rel_mp4"] = f"takes/{sid}/{v_dst.name}"
                tk["file_path"] = None
            s_src = resolve_vault_media_path(proj_dir, renders_dir, tk, path_key="strip_path", rel_key="vault_rel_strip")
            if s_src and s_src.is_file():
                s_dst = dst_dir / s_src.name
                safe_link_or_copy(s_src, s_dst)
                s_src.unlink(missing_ok=True)
                tk["vault_rel_strip"] = f"takes/{sid}/{s_dst.name}"
                tk["strip_path"] = None
            arch_dir = renders_dir / "_checkpoint_archive" / tid
            if arch_dir.is_dir():
                shutil.rmtree(arch_dir, ignore_errors=True)
            takes.append(tk)
            known_tids.add(tid)
            changed = True
    if not archived:
        manifest.pop("archived_takes", None)
    return changed


def purge_deleted_scene_artifacts(
    proj_dir: Path | str,
    scene_ids: Iterable[str],
    *,
    config_data: dict | None = None,
    config_file: Path | None = None,
    archive_for_checkpoints: bool = True,
    clear_queue: bool = True,
) -> dict:
    """Removes active takes, vault dirs, active/trimmed clips, strips, and state hashes for deleted/replaced scene IDs."""
    proj_dir = Path(proj_dir)
    target_uppers = {str(s or "").strip().upper() for s in (scene_ids or []) if str(s or "").strip()}
    if not target_uppers:
        return {"purged_scene_ids": [], "removed_takes": 0}

    pdirs = resolve_project_dirs(proj_dir, config_data=config_data, config_file=config_file)
    renders_dir, trimmed_dir, strips_dir = pdirs["renders"], pdirs["trimmed"], pdirs["strips"]
    known_out_names: set[str] = set()
    removed_takes_count = 0

    manifest_lock = get_takes_manifest_path(proj_dir).with_suffix(".lock")
    manifest_lock.parent.mkdir(parents=True, exist_ok=True)
    with locked_file_op(manifest_lock):
        manifest = load_takes_manifest(proj_dir)
        scenes_map = manifest.setdefault("scenes", {})
        matched_keys = [k for k in list(scenes_map) if str(k).strip().upper() in target_uppers]
        ckpt_refs = _collect_checkpoint_take_refs(proj_dir, target_uppers) if archive_for_checkpoints else set()
        popped_takes: list[dict] = []
        for mk in matched_keys:
            entry = scenes_map.pop(mk, None)
            if isinstance(entry, dict):
                for tk in (entry.get("takes") or []):
                    if isinstance(tk, dict):
                        popped_takes.append(tk)
                        oname = str(tk.get("out_name") or "").strip()
                        if oname:
                            known_out_names.add(oname)
                            known_out_names.add(oname.upper())
        removed_takes_count = len(popped_takes)
        remaining = [t for s in scenes_map.values() if isinstance(s, dict) for t in (s.get("takes") or [])]
        rem_mp4s = {t.get("vault_rel_mp4") for t in remaining if t.get("vault_rel_mp4")}
        rem_strips = {t.get("vault_rel_strip") for t in remaining if t.get("vault_rel_strip")}

        for tk in popped_takes:
            tid = str(tk.get("take_id") or "")
            if tid and tid in ckpt_refs:
                _archive_take_for_checkpoint(renders_dir, tk, proj_dir, manifest)
                continue
            vr_m, vr_s = tk.get("vault_rel_mp4"), tk.get("vault_rel_strip")
            if vr_m and vr_m not in rem_mp4s:
                (renders_dir / vr_m).unlink(missing_ok=True)
            if vr_s and vr_s not in rem_strips:
                (renders_dir / vr_s).unlink(missing_ok=True)
            for pk in ("file_path", "strip_path"):
                fp = tk.get(pk)
                if fp and not (pk == "file_path" and vr_m in rem_mp4s) and not (pk == "strip_path" and vr_s in rem_strips):
                    cand = proj_dir / str(fp)
                    if cand.is_file() and "takes" in cand.parts:
                        cand.unlink(missing_ok=True)

        takes_root = renders_dir / "takes"
        if takes_root.is_dir():
            for d in sorted(takes_root.iterdir()):
                if d.is_dir() and d.name.strip().upper() in target_uppers:
                    shutil.rmtree(d, ignore_errors=True)
        if matched_keys or popped_takes:
            save_takes_manifest(proj_dir, manifest)

    # Remove active renders, trimmed clips, and review strips strictly belonging to deleted scene IDs
    for cdir, exts in ((renders_dir, (".mp4",)), (trimmed_dir, (".mp4",)), (strips_dir, (".jpg",)), (proj_dir / "work" / "frames", (".jpg",)), (renders_dir, (".jpg",))):
        if not cdir.is_dir():
            continue
        for f in list(cdir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            if exts == (".jpg",) and "_strip" not in f.stem.lower():
                continue
            check_name = re.sub(r"_strip(?:_\d+f)?$", "", f.stem, flags=re.I) + f.suffix if exts == (".jpg",) else f.name
            if any(_is_scene_artifact_match(check_name, sid_u, known_out_names) for sid_u in target_uppers):
                f.unlink(missing_ok=True)

    # Purge matching state_hashes entries
    st_file = get_state_hashes_file(proj_dir)
    if st_file.is_file():
        try:
            st_data = json.loads(st_file.read_text(encoding="utf-8"))
        except Exception:
            st_data = {}
        st_changed = False
        if isinstance(st_data, dict):
            for section in ("tasks", "task_hashes", "scene_renders", "settings_store"):
                sec_d = st_data.get(section)
                if not isinstance(sec_d, dict):
                    continue
                for k in list(sec_d):
                    ku = str(k).upper()
                    if k in known_out_names or ku in known_out_names or any(ku == sid_u or ku.startswith(f"{sid_u}_") for sid_u in target_uppers):
                        sec_d.pop(k, None)
                        st_changed = True
            if st_changed:
                save_state_hashes_data(proj_dir, st_data)

    # Clear non-rendering queue entries so recreated IDs don't inherit stale completed/skipped state
    if clear_queue:
        qf = resolve_queue_file(proj_dir, config_data=config_data)
        if qf.is_file():
            try:
                raw_q = json.loads(qf.read_text(encoding="utf-8"))
                q_list = raw_q if isinstance(raw_q, list) else (raw_q.get("queue", []) if isinstance(raw_q, dict) else None)
                if isinstance(q_list, list):
                    kept = [
                        it for it in q_list
                        if not isinstance(it, dict)
                        or it.get("stage") == "anchor"
                        or it.get("status") == "rendering"
                        or str(it.get("scene_id") or "").strip().upper() not in target_uppers
                    ]
                    if len(kept) != len(q_list):
                        atomic_write_json(qf, kept if isinstance(raw_q, list) else {**raw_q, "queue": kept})
            except Exception:
                pass

    return {"purged_scene_ids": sorted(target_uppers), "removed_takes": removed_takes_count}


def strip_transient_scene_ui_fields(project_data: dict) -> list[str]:
    """Strips ephemeral UI flags from scene dicts in-place and returns scene IDs marked for fresh takes reset."""
    fresh_ids: list[str] = []
    for sc in (project_data.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        if sc.get("_freshSceneTakes") and sc.get("id"):
            fresh_ids.append(str(sc["id"]).strip())
        for k in _TRANSIENT_SCENE_KEYS:
            sc.pop(k, None)
    return fresh_ids


def save_project_json_with_purge(
    srv,
    proj_dir: Path,
    target_json: Path,
    slug: str,
    body: dict,
    query_params: dict | None = None,
) -> list[str]:
    """Saves `body` cleanly to `target_json` and purges takes/artifacts of any deleted or replaced scenes."""
    prev_ids: list[str] = []
    if target_json.is_file():
        try:
            prev_data = json.loads(target_json.read_text(encoding="utf-8"))
            prev_ids = [str(s.get("id") or "").strip() for s in (prev_data.get("scenes") or []) if isinstance(s, dict) and s.get("id")]
        except Exception:
            prev_ids = []

    fresh_ids = strip_transient_scene_ui_fields(body)
    new_scenes = body.get("scenes") or []
    new_uppers = {str(s.get("id") or "").strip().upper() for s in new_scenes if isinstance(s, dict) and s.get("id")}
    removed_ids = [sid for sid in prev_ids if sid.upper() not in new_uppers]

    explicit_ids: list[str] = []
    if isinstance(query_params, dict):
        for raw_val in query_params.get("purge_scenes", []):
            explicit_ids.extend(tok.strip() for tok in str(raw_val or "").split(",") if tok.strip())

    orphan_ids = collect_orphaned_manifest_scene_ids(proj_dir, new_scenes, config_data=body)
    purge_ids = list(dict.fromkeys([*removed_ids, *fresh_ids, *explicit_ids, *orphan_ids]))

    srv.backup_project_json(target_json, throttle_seconds=60.0)
    target_json.write_text(json.dumps(body, indent=2), encoding="utf-8")

    if purge_ids:
        res = purge_deleted_scene_artifacts(proj_dir, purge_ids, config_data=body, config_file=target_json)
        return res.get("purged_scene_ids", [])
    return []


def handle_scene_split_post_purge(proj_dir: Path, config_file: Path, orig_scene_id: str, sc1: dict, sc2: dict, updated_data: dict) -> None:
    """Purges takes for removed pre-split scene ID and any freshly created split IDs so stale ghost takes never leak."""
    orig_u = str(orig_scene_id or "").strip().upper()
    new_ids = [str(s.get("id") or "").strip() for s in (sc1, sc2) if isinstance(s, dict) and s.get("id")]
    new_uppers = {s.upper() for s in new_ids}
    to_purge = [s for s in new_ids if s.upper() != orig_u]
    if orig_u and orig_u not in new_uppers:
        to_purge.append(str(orig_scene_id).strip())
    if to_purge:
        purge_deleted_scene_artifacts(proj_dir, to_purge, config_data=updated_data, config_file=config_file)
