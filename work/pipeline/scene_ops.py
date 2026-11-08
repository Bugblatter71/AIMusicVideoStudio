"""
Scene operations module for Music Video Generation Studio.
Handles scene lifecycle mutations:
- derive_split_scene_ids
- split_scene_in_project_data
- delete_scene_in_project_data
"""

import copy
import re


def derive_split_scene_ids(orig_id: str, existing_ids: set[str] | list[str]) -> tuple[str, str]:
    """Derives sensible unique IDs for splitting a scene into two parts.
    Examples:
      'S02'  -> ('S02a', 'S02b')
      'S12a' -> ('S12a', 'S12b')  (if S12b is unused)
      'S12b' -> ('S12b', 'S12c')
      'Intro' -> ('Intro_a', 'Intro_b')
    """
    clean_id = str(orig_id).strip()
    upper_existing = {str(x).strip().upper() for x in existing_ids}

    # Case 1: Standard S-prefixed number without letter suffix: e.g. S01, S2, S10
    m_num = re.match(r"^(S0*\d+)$", clean_id, re.IGNORECASE)
    if m_num:
        cand1 = f"{clean_id}a"
        cand2 = f"{clean_id}b"
        if cand1.upper() not in upper_existing and cand2.upper() not in upper_existing:
            return cand1, cand2

    # Case 2: Number with single letter suffix: e.g. S12a, S26a
    m_letter = re.match(r"^(S0*\d+)([a-z])$", clean_id, re.IGNORECASE)
    if m_letter:
        prefix = m_letter.group(1)
        cur_char = m_letter.group(2).lower()
        part1 = clean_id
        for offset in range(1, 26):
            nxt_char = chr((ord(cur_char) - ord("a") + offset) % 26 + ord("a"))
            cand = f"{prefix}{nxt_char}"
            if cand.upper() not in upper_existing:
                return part1, cand

    # Case 3: Letter suffix for alphanumeric names: e.g. Intro -> Intro_a, Intro_b
    cand1 = f"{clean_id}_a"
    cand2 = f"{clean_id}_b"
    if cand1.upper() not in upper_existing and cand2.upper() not in upper_existing:
        return cand1, cand2

    for offset in range(1, 26):
        cand = f"{clean_id}_{offset+1}"
        if cand.upper() not in upper_existing:
            return clean_id, cand

    return f"{clean_id}_part1", f"{clean_id}_part2"


def split_scene_in_project_data(
    data: dict,
    scene_id: str,
    split_time: float,
    id1: str | None = None,
    id2: str | None = None,
) -> tuple[dict, dict, dict]:
    """Splits a scene in project data into two sequential sub-scenes at split_time.

    Args:
        data: Project configuration dictionary containing a "scenes" list.
        scene_id: ID of the scene to split (e.g. "S02", "S12a").
        split_time: Point in seconds where the cut occurs (start < split_time < end).
        id1: Optional custom ID for the first part.
        id2: Optional custom ID for the second part.

    Returns:
        tuple of (modified_data, first_scene, second_scene)
    """
    scenes = data.get("scenes", [])
    target_id = str(scene_id).strip().upper()

    target_idx = -1
    target_scene = None
    for i, sc in enumerate(scenes):
        sid = str(sc.get("id", "")).strip().upper()
        if sid == target_id:
            target_idx = i
            target_scene = sc
            break

    # Fallback: Zero-padded match (e.g. 'S2' matches 'S02')
    if target_scene is None:
        m_num = re.match(r"^S0*(\d+)([A-Z]?)$", target_id)
        if m_num:
            t_num, t_sub = int(m_num.group(1)), m_num.group(2)
            for i, sc in enumerate(scenes):
                sid = str(sc.get("id", "")).strip().upper()
                m_sid = re.match(r"^S0*(\d+)([A-Z]?)$", sid)
                if m_sid and int(m_sid.group(1)) == t_num and m_sid.group(2) == t_sub:
                    target_idx = i
                    target_scene = sc
                    break

    if target_scene is None:
        raise ValueError(f"Scene '{scene_id}' not found in project.")

    start = float(target_scene.get("start", 0))
    end = float(target_scene.get("end", 0))
    split_time = round(float(split_time), 3)

    if not (start < split_time < end):
        raise ValueError(
            f"Split time {split_time:.2f}s must be strictly between scene start ({start:.2f}s) and end ({end:.2f}s)."
        )
    if (split_time - start < 0.05) or (end - split_time < 0.05):
        raise ValueError(
            f"Split time {split_time:.2f}s leaves a segment shorter than 0.05s (start: {start:.2f}s, end: {end:.2f}s)."
        )

    orig_id = str(target_scene.get("id", scene_id)).strip()
    existing_ids = {str(s.get("id", "")).strip() for s in scenes if s.get("id")}
    auto_id1, auto_id2 = derive_split_scene_ids(orig_id, existing_ids)
    final_id1 = str(id1).strip() if id1 else auto_id1
    final_id2 = str(id2).strip() if id2 else auto_id2

    if final_id1.upper() == final_id2.upper():
        raise ValueError(f"Split scene IDs cannot be identical ('{final_id1}').")

    other_ids = {s.get("id", "").strip().upper() for j, s in enumerate(scenes) if j != target_idx}
    if final_id1.upper() in other_ids:
        raise ValueError(f"Scene ID '{final_id1}' already exists in project.")
    if final_id2.upper() in other_ids:
        raise ValueError(f"Scene ID '{final_id2}' already exists in project.")

    scene1 = copy.deepcopy(target_scene)
    scene2 = copy.deepcopy(target_scene)

    scene1["id"] = final_id1
    scene1["start"] = start
    scene1["end"] = split_time

    scene2["id"] = final_id2
    scene2["start"] = split_time
    scene2["end"] = end

    if "title" in target_scene:
        orig_title = target_scene["title"]
        scene1["title"] = f"{orig_title} (Part 1)"
        scene2["title"] = f"{orig_title} (Part 2)"

    if "output_name" in target_scene:
        out_orig = target_scene["output_name"]
        if out_orig.startswith(orig_id):
            scene1["output_name"] = final_id1 + out_orig[len(orig_id):]
            scene2["output_name"] = final_id2 + out_orig[len(orig_id):]
        else:
            scene1["output_name"] = f"{out_orig}_{final_id1}"
            scene2["output_name"] = f"{out_orig}_{final_id2}"

    # Increment seeds so the two separate video clips don't generate identical random noise
    for k in ("seed", "seed_m", "seed_f"):
        if k in scene2 and isinstance(scene2[k], int):
            scene2[k] += 1

    scenes[target_idx : target_idx + 1] = [scene1, scene2]
    return data, scene1, scene2


def delete_scene_in_project_data(
    data: dict,
    scene_id: str,
    close_gap: bool = True,
) -> tuple[dict, dict]:
    """Deletes a scene from project configuration by its scene ID.

    Args:
        data: Project configuration dictionary containing a "scenes" list.
        scene_id: ID of the scene to delete (e.g. "S02", "S12a").
        close_gap: If True (default), automatically adjusts adjacent scenes
                   to prevent empty gaps in the timeline:
                   - If deleted scene was first (index 0): next scene (now index 0)
                     has its 'start' adjusted to 0.00.
                   - If deleted scene was in the middle: previous scene has its
                     'end' extended to match the next scene's 'start'.
                   - If deleted scene was the last scene: previous scene has its
                     'end' extended to fill up to project.total_duration (or the
                     deleted scene's end).

    Returns:
        tuple of (modified_data, deleted_scene_dict)

    Raises:
        ValueError: If scene_id is not found in the project.
    """
    scenes = data.get("scenes", [])
    target_id = str(scene_id).strip().upper()

    target_idx = -1
    for i, sc in enumerate(scenes):
        sid = str(sc.get("id", "")).strip().upper()
        if sid == target_id:
            target_idx = i
            break

    # Fallback: Zero-padded match (e.g. 'S2' matches 'S02')
    if target_idx < 0:
        m_num = re.match(r"^S0*(\d+)([A-Z]?)$", target_id)
        if m_num:
            t_num, t_sub = int(m_num.group(1)), m_num.group(2)
            for i, sc in enumerate(scenes):
                sid = str(sc.get("id", "")).strip().upper()
                m_sid = re.match(r"^S0*(\d+)([A-Z]?)$", sid)
                if m_sid and int(m_sid.group(1)) == t_num and m_sid.group(2) == t_sub:
                    target_idx = i
                    break

    if target_idx < 0:
        raise ValueError(f"Scene '{scene_id}' not found in project.")

    deleted_scene = scenes.pop(target_idx)

    if close_gap and scenes:
        if target_idx == 0:
            # First scene was removed: pull next scene back to 0.00
            scenes[0]["start"] = 0.00
        elif target_idx < len(scenes):
            # Middle scene was removed: extend previous scene's end to touch the next scene's start
            prev_scene = scenes[target_idx - 1]
            next_scene = scenes[target_idx]
            prev_scene["end"] = next_scene.get("start", prev_scene.get("end", 0))
        else:
            # Trailing scene was removed: extend new last scene to project total_duration or deleted scene's end
            last_scene = scenes[-1]
            total_dur = data.get("project", {}).get("total_duration")
            if isinstance(total_dur, (int, float)) and float(total_dur) > float(last_scene.get("start", 0)):
                last_scene["end"] = round(float(total_dur), 2)
            elif float(deleted_scene.get("end", 0)) > float(last_scene.get("start", 0)):
                last_scene["end"] = round(float(deleted_scene["end"]), 2)

    return data, deleted_scene
