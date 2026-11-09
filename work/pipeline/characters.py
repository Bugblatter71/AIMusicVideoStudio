"""
Character Management Studio & Multi-Reference System backend.
Provides character look synthesis, tag resolution, role-aware prompt compilation,
and REST/media helper functions. Kept strictly modular and <850 lines.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .media_utils import _file_sha256, resolve_ffmpeg_path, resolve_path
from .prompts import build_ref2va_prompt, resolve_scene_refs, strip_dangling_ref_tags

DEFAULT_MARCUS_TRAITS = "the exact 30-year-old Black male singer"
DEFAULT_MARCUS_STUDIO_DESC = (
    "short tapered fade haircut, neat short beard, wearing a dark slate-navy "
    "suede jacket over a burgundy crew-neck t-shirt and thin silver chain"
)
DEFAULT_MARCUS_STUDIO_LEGACY = (
    f"{DEFAULT_MARCUS_TRAITS} from <image1> and <image2> ({DEFAULT_MARCUS_STUDIO_DESC})"
)

DEFAULT_ELENA_TRAITS = "the exact late-20s woman"
DEFAULT_ELENA_STUDIO_DESC = (
    "shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, "
    "wearing a black suede biker jacket over a dark charcoal V-neck top and a thin silver necklace"
)
DEFAULT_ELENA_STUDIO_LEGACY = (
    f"{DEFAULT_ELENA_TRAITS} from <image3> and <image4> ({DEFAULT_ELENA_STUDIO_DESC})"
)

CAMERA_MOTION_REGEX = re.compile(
    r"\b(pan|pans|panning|zoom|zooms|zooming|track|tracks|tracking|crane|glide|gliding|sweep|sweeping)\b",
    re.IGNORECASE,
)


def synthesize_default_characters(raw_project: dict, project_dir: Path) -> dict:
    """Synthesizes default character objects if 'characters' key is absent or empty."""
    chars = raw_project.get("characters")
    if chars and isinstance(chars, dict) and len(chars) > 0:
        return copy.deepcopy(chars)

    vars_dict = raw_project.get("variables", {}) or {}
    has_male = "MALE_STUDIO_DESC" in vars_dict
    has_female = "FEMALE_STUDIO_DESC" in vars_dict
    ref_sheets_exist = (project_dir / "work" / "ref_sheets").is_dir()
    orchestra_dir = project_dir / "references" / "orchestra"

    if not has_male and not has_female and not ref_sheets_exist and not orchestra_dir.is_dir():
        return {}

    male_leg = vars_dict.get("MALE_STUDIO_DESC", DEFAULT_MARCUS_STUDIO_LEGACY)
    female_leg = vars_dict.get("FEMALE_STUDIO_DESC", DEFAULT_ELENA_STUDIO_LEGACY)

    result: dict[str, dict] = {
        "marcus": {
            "id": "marcus",
            "name": "Marcus (Lead Vocalist)",
            "kind": "character",
            "default_look": "studio",
            "avatar": "work/ref_sheets/male_angle1_front34.jpg",
            "physical_traits": DEFAULT_MARCUS_TRAITS,
            "looks": {
                "studio": {
                    "tag": "@male_studio",
                    "legacy_var": "MALE_STUDIO_DESC",
                    "name": "Studio Recording",
                    "description": DEFAULT_MARCUS_STUDIO_DESC,
                    "legacy_prompt_text": male_leg,
                    "image_refs": [
                        "work/ref_sheets/male_angle1_front34.jpg",
                        "work/ref_sheets/male_angle3_profile.jpg",
                    ],
                },
                "nightout": {
                    "tag": "@male_nightout",
                    "legacy_var": "MALE_NIGHTOUT_DESC",
                    "name": "Upscale Evening",
                    "description": "short tapered fade haircut, neat short beard, wearing a smart dark midnight-blue collared shirt",
                    "legacy_prompt_text": vars_dict.get(
                        "MALE_NIGHTOUT_DESC",
                        f"{DEFAULT_MARCUS_TRAITS} from <image1> and <image2> (short tapered fade haircut, neat short beard, wearing a smart dark midnight-blue collared shirt)",
                    ),
                    "image_refs": [
                        "references/nightout/male_nightout_front34.jpg",
                        "references/nightout/male_nightout_profile.jpg",
                    ],
                },
            },
        },
        "elena": {
            "id": "elena",
            "name": "Elena (Producer & Guitarist)",
            "kind": "character",
            "default_look": "studio",
            "avatar": "work/ref_sheets/female_angle1_front34.jpg",
            "physical_traits": DEFAULT_ELENA_TRAITS,
            "looks": {
                "studio": {
                    "tag": "@female_studio",
                    "legacy_var": "FEMALE_STUDIO_DESC",
                    "name": "Studio Control Room",
                    "description": DEFAULT_ELENA_STUDIO_DESC,
                    "legacy_prompt_text": female_leg,
                    "image_refs": [
                        "work/ref_sheets/female_angle1_front34.jpg",
                        "work/ref_sheets/female_angle3_profile.jpg",
                    ],
                },
                "nightout": {
                    "tag": "@female_nightout",
                    "legacy_var": "FEMALE_NIGHTOUT_DESC",
                    "name": "Evening Cocktail",
                    "description": "shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, wearing a chic sleeveless burgundy-black evening top",
                    "legacy_prompt_text": vars_dict.get(
                        "FEMALE_NIGHTOUT_DESC",
                        f"{DEFAULT_ELENA_TRAITS} from <image3> and <image4> (shoulder-length wavy dark auburn-chestnut hair with curtain bangs, warm hazel eyes, wearing a chic sleeveless burgundy-black evening top)",
                    ),
                    "image_refs": [
                        "references/nightout/female_nightout_front34.jpg",
                        "references/nightout/female_nightout_profile.jpg",
                    ],
                },
            },
        },
    }

    # Add orchestra ensemble if directory or references exist
    result["orchestra"] = {
        "id": "orchestra",
        "name": "Soundstage Orchestra Sections",
        "kind": "ensemble",
        "default_look": "strings",
        "avatar": "references/orchestra/strings.jpg",
        "physical_traits": "professional session musicians on the dark soundstage",
        "looks": {
            "pianist": {
                "tag": "@pianist",
                "name": "Grand Piano Soloist",
                "description": "classical pianist seated at an ebony concert grand piano in formal dark attire",
                "image_refs": ["references/orchestra/pianist.jpg"],
            },
            "strings": {
                "tag": "@strings",
                "name": "Chamber String Section",
                "description": "chamber string orchestra bowing their violins and cellos in sweeping unison",
                "image_refs": ["references/orchestra/strings.jpg"],
            },
            "brass": {
                "tag": "@brass",
                "name": "Brass Section",
                "description": "brass section playing trumpets and French horns under warm stage lights",
                "image_refs": ["references/orchestra/brass.jpg"],
            },
            "woodwinds": {
                "tag": "@woodwinds",
                "name": "Woodwind Section",
                "description": "woodwind section playing flutes and clarinets in synchronized harmony",
                "image_refs": ["references/orchestra/woodwinds.jpg"],
            },
        },
    }

    return result


def build_tag_lookup(characters: dict, project_dir: Path) -> dict[str, dict]:
    """Returns case-insensitive mapping from @tag, @entity, and {LEGACY_VAR} to descriptor dict."""
    lookup: dict[str, dict] = {}
    if not characters:
        return lookup

    for char_id, entity in characters.items():
        if not isinstance(entity, dict):
            continue
        c_id = str(entity.get("id") or char_id)
        c_name = str(entity.get("name") or c_id)
        kind = str(entity.get("kind") or "character")
        traits = str(entity.get("physical_traits") or "")
        avatar = str(entity.get("avatar") or "")
        default_look = str(entity.get("default_look") or "")
        looks = entity.get("looks", {}) or {}

        for look_id, look in looks.items():
            if not isinstance(look, dict):
                continue
            l_id = str(look_id)
            tag = str(look.get("tag") or f"@{l_id}")
            l_name = str(look.get("name") or l_id)
            desc = str(look.get("description") or "")
            legacy_var = look.get("legacy_var")
            legacy_prompt = look.get("legacy_prompt_text")

            raw_refs = look.get("image_refs", []) or []
            rel_refs = [str(p).replace("\\", "/") for p in raw_refs]
            res_refs = [resolve_path(project_dir, p) for p in raw_refs]
            # Use disk paths that exist, or retain resolved Paths for tests/mocking
            valid_res = [p for p in res_refs if p.exists()] or res_refs

            if traits and desc:
                expansion = f"{traits} ({desc})"
            elif desc:
                expansion = desc
            else:
                expansion = traits

            descriptor = {
                "tag": tag,
                "entity_id": c_id,
                "look_id": l_id,
                "name": f"{c_name} — {l_name}",
                "kind": kind,
                "avatar": avatar or (rel_refs[0] if rel_refs else ""),
                "physical_traits": traits,
                "description": desc,
                "text_expansion": expansion,
                "legacy_prompt_text": legacy_prompt or expansion,
                "image_refs": valid_res,
                "rel_image_refs": rel_refs,
            }

            lookup[tag.lower()] = descriptor
            if legacy_var:
                lvar_clean = str(legacy_var).strip("{}").lower()
                lookup[f"{{{lvar_clean}}}"] = descriptor
                lookup[lvar_clean] = descriptor

            if l_id == default_look or f"@{c_id.lower()}" not in lookup:
                lookup[f"@{c_id.lower()}"] = descriptor

    return lookup


def _normalize_legacy_expansions(prompt: str, tag_lookup: dict[str, dict]) -> str:
    """If a prompt contains @tags alongside already-expanded legacy variable text, normalize legacy text to @tag."""
    if not prompt or not tag_lookup or not re.search(r"@[a-zA-Z0-9_-]+", prompt):
        return prompt
    normalized = prompt
    seen_tags: set[str] = set()
    for desc in tag_lookup.values():
        tag = desc.get("tag")
        leg = desc.get("legacy_prompt_text")
        if tag and leg and tag not in seen_tags and len(leg) > 25 and leg in normalized:
            seen_tags.add(tag)
            normalized = normalized.replace(leg, tag)
    return normalized


def find_prompt_entities(prompt: str, tag_lookup: dict[str, dict]) -> list[dict]:
    """Finds all distinct character/look entities referenced in prompt in order of appearance."""
    if not prompt or not tag_lookup:
        return []
    norm_prompt = _normalize_legacy_expansions(prompt, tag_lookup)
    matches = re.finditer(r"(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})", norm_prompt, flags=re.IGNORECASE)
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for m in matches:
        token = m.group(1).lower()
        if token in tag_lookup:
            desc = tag_lookup[token]
            key = (desc["entity_id"], desc["look_id"])
            if key not in seen:
                seen.add(key)
                result.append(desc)
    return result


def expand_prompt_with_characters(
    raw_prompt: str, variables: dict, tag_lookup: dict[str, dict] | None
) -> tuple[str, str | None]:
    """
    Expands {VARS} and @tags for task hash computation.
    Returns (canonical_expanded, legacy_expanded_or_none).
    - If only legacy {VARS} are used and traits/description match legacy_prompt_text, canonical matches legacy.
    - If @tags are used, canonical expands @tags to current text_expansion (so editing traits invalidates hash),
      and if traits/description are unmodified from legacy_prompt_text, legacy_expanded is also returned so
      migrating {MALE_STUDIO_DESC} -> @male_studio preserves existing render hashes.
    """
    from .prompts import expand_variables

    base_exp = expand_variables(raw_prompt, variables)
    if not tag_lookup:
        return base_exp, None

    has_at_tag = bool(re.search(r"@[a-zA-Z0-9_-]+", raw_prompt))
    ents = find_prompt_entities(raw_prompt, tag_lookup)
    if not ents:
        return base_exp, None

    all_unmodified = True
    for e in ents:
        leg = e.get("legacy_prompt_text") or ""
        traits = e.get("physical_traits") or ""
        desc = e.get("description") or ""
        if not leg or (traits and traits not in leg) or (desc and desc not in leg):
            all_unmodified = False
            break

    if not has_at_tag and all_unmodified:
        return base_exp, None

    def _repl_canonical(match):
        tok = match.group(1).lower()
        if tok in tag_lookup:
            return tag_lookup[tok]["text_expansion"]
        return match.group(1)

    canon = re.sub(r"(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})", _repl_canonical, raw_prompt, flags=re.IGNORECASE)
    canon = expand_variables(canon, variables)

    if all_unmodified:
        def _repl_legacy(match):
            tok = match.group(1).lower()
            if tok in tag_lookup and tag_lookup[tok].get("legacy_prompt_text"):
                return tag_lookup[tok]["legacy_prompt_text"]
            return match.group(1)

        leg_str = re.sub(r"(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})", _repl_legacy, raw_prompt, flags=re.IGNORECASE)
        leg_str = expand_variables(leg_str, variables)
        return canon, (leg_str if leg_str != canon else None)

    return canon, None


def compile_character_prompt(
    prompt: str,
    tag_lookup: dict[str, dict],
    supports_refs: bool,
    fallback_refs: list[Path] | None = None,
    has_end: bool = False,
) -> tuple[str, list[Path]]:
    """
    Compiles prompt for video renderers:
    - Text-only (FL2VA/LTX-2): Expands tags to natural language traits, strips <imageN> and unused <Picture N> tags.
    - Multimodal (Ref2VA): Replaces tags with <Subject K>, allocates <Picture 2..10>, builds 6 sections.
    """
    if not prompt:
        return "", []

    norm_prompt = _normalize_legacy_expansions(prompt, tag_lookup)
    matched = find_prompt_entities(norm_prompt, tag_lookup)

    # 1. Text-Only Mode (supports_refs is False)
    if not supports_refs:
        def _replace_text(match):
            token = match.group(1).lower()
            if token in tag_lookup:
                return tag_lookup[token]["text_expansion"]
            return match.group(1)

        expanded = re.sub(r"(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})", _replace_text, norm_prompt, flags=re.IGNORECASE)
        cleaned = strip_dangling_ref_tags(expanded)
        pic_pat = r"(?:[3-9]|\d{2,})" if has_end else r"(?:[2-9]|\d{2,})"
        cleaned = re.sub(
            rf"\s*(?:from|in|matching)\s+<Picture\s*{pic_pat}>(?:\s*(?:and|,)\s*<Picture\s*{pic_pat}>)*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(rf"<Picture\s*{pic_pat}>", "", cleaned, flags=re.IGNORECASE)
        return cleaned, []

    # 2. Multimodal Mode (supports_refs is True)
    if not matched:
        # Fall back to legacy <image1> positional resolution if no @tags were used
        if fallback_refs:
            resolved, ref_map = resolve_scene_refs(prompt, fallback_refs)
            if resolved:
                return build_ref2va_prompt(prompt, ref_map), resolved
        return prompt, []

    # Allocate image references (max 9 images total across all subjects)
    allocated_paths: list[Path] = []
    path_to_pic: dict[str, int] = {}
    entity_pics: dict[tuple[str, str], list[int]] = {}

    for entry in matched:
        key = (entry["entity_id"], entry["look_id"])
        entity_pics[key] = []
        for p in entry.get("image_refs", []):
            p_str = str(p)
            if p_str in path_to_pic:
                entity_pics[key].append(path_to_pic[p_str])
            elif len(allocated_paths) < 9:
                pic_num = len(allocated_paths) + 2
                allocated_paths.append(p)
                path_to_pic[p_str] = pic_num
                entity_pics[key].append(pic_num)

    # Parse existing prompt structure
    lines = [l.strip() for l in norm_prompt.splitlines() if l.strip()]
    first_frame_line = ""
    desc_lines: list[str] = []
    soundscape = "Studio room tone and vocals."
    non_diegetic = "N/A"
    current_sec = "desc"

    for line in lines:
        if line.startswith("For the target video,"):
            first_frame_line = line
            continue
        if line.startswith("integrated_multimodal_description:"):
            current_sec = "desc"
            rem = line[len("integrated_multimodal_description:") :].strip()
            if rem:
                desc_lines.append(rem)
            continue
        elif line.startswith("detailed_description:"):
            current_sec = "desc"
            rem = line[len("detailed_description:") :].strip()
            if rem:
                desc_lines.append(rem)
            continue
        elif line.startswith("overall_soundscape:"):
            current_sec = "sound"
            soundscape = line[len("overall_soundscape:") :].strip()
            continue
        elif line.startswith("non_diegetic_music:"):
            current_sec = "music"
            non_diegetic = line[len("non_diegetic_music:") :].strip()
            continue
        elif any(line.startswith(pfx) for pfx in ("subject_definitions:", "retention_analysis:", "summary:")):
            current_sec = "skip"
            continue

        if current_sec == "desc":
            desc_lines.append(line)
        elif current_sec == "sound":
            soundscape += " " + line
        elif current_sec == "music":
            non_diegetic += " " + line

    desc_text = " ".join(desc_lines).strip()
    if not first_frame_line:
        first_frame_line = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."

    # Build entity token regex to replace mentions in narrative
    # Maps entity_key -> subject index (1..K)
    entity_to_sidx = { (e["entity_id"], e["look_id"]): i + 1 for i, e in enumerate(matched) }
    seen_entity_mentions: set[tuple[str, str]] = set()

    def _replace_narrative_entity(match):
        tok = match.group(1).lower()
        if tok not in tag_lookup:
            return match.group(1)
        desc = tag_lookup[tok]
        key = (desc["entity_id"], desc["look_id"])
        if key not in entity_to_sidx:
            return match.group(1)
        s_idx = entity_to_sidx[key]
        if key not in seen_entity_mentions:
            seen_entity_mentions.add(key)
            detail = desc["text_expansion"]
            return f"<Subject {s_idx}> ({detail})"
        return f"<Subject {s_idx}>"

    narrative_body = re.sub(
        r"(@[a-zA-Z0-9_-]+|\{[a-zA-Z0-9_]+\})",
        _replace_narrative_entity,
        desc_text,
        flags=re.IGNORECASE,
    )
    narrative_body = strip_dangling_ref_tags(narrative_body)

    # Ensure narrative starts cleanly with [Shot 1]
    if narrative_body.startswith("[Shot 1]"):
        if not narrative_body.startswith("[Shot 1] Continue directly from <Picture 1>."):
            clean_desc = "[Shot 1] Continue directly from <Picture 1>. " + narrative_body[len("[Shot 1]") :].strip()
        else:
            clean_desc = narrative_body
    else:
        clean_desc = f"[Shot 1] Continue directly from <Picture 1>. {narrative_body}"

    # Build subject definitions and retention analysis
    subj_defs: list[str] = []
    retention_lines: list[str] = []

    for i, entry in enumerate(matched):
        s_idx = i + 1
        key = (entry["entity_id"], entry["look_id"])
        pics = entity_pics.get(key, [])
        pics_str = " and ".join(f"<Picture {p}>" for p in sorted(pics)) if pics else "<Picture 1>"

        if entry.get("kind") == "character":
            subj_defs.append(
                f"<Subject {s_idx}> is {entry['physical_traits']} from {pics_str}, preserving facial identity, skin tone, eyes, and hair."
            )
            retention_lines.append(
                f"<Subject {s_idx}> (appears in [Shot 1]): attribute_transfer - facial features and hair follow {pics_str}; lighting and scene integration follow <Picture 1>."
            )
        else:
            subj_defs.append(
                f"<Subject {s_idx}> is {entry['description']} from {pics_str}, preserving instrument details, attire, and group staging."
            )
            retention_lines.append(
                f"<Subject {s_idx}> (appears in [Shot 1]): subject_appearance_reference - visual appearance and instruments follow {pics_str}."
            )

    has_camera_motion = bool(CAMERA_MOTION_REGEX.search(desc_text)) or len(matched) > 2
    if has_camera_motion:
        pic1_retention = (
            "<Picture 1> (anchors [Shot 1] at 0.00s): environment_and_style_reference - "
            "preserves soundstage lighting, color palette, and starting composition while camera moves dynamically."
        )
    else:
        pic1_retention = (
            "<Picture 1> (anchors [Shot 1] at 0.00s): fully_preserved - "
            "composition, background, camera framing, and wardrobe."
        )

    retention_lines.insert(0, pic1_retention)

    sections = [
        first_frame_line,
        "subject_definitions:",
        *subj_defs,
        "summary:",
        "[reference generation] Continue the scene established in <Picture 1>, preserving character identity.",
        "retention_analysis:",
        *retention_lines,
        "integrated_multimodal_description:",
        clean_desc,
        "overall_soundscape:",
        soundscape.strip(),
        "non_diegetic_music:",
        non_diegetic.strip(),
    ]
    six_section_prompt = "\n".join(s for s in sections if s)
    return six_section_prompt, allocated_paths


# ---------------------------------------------------------------------------
# REST & Media Helper Functions
# ---------------------------------------------------------------------------


def get_project_characters_payload(proj_dir: Path, data: dict, slug: str) -> dict:
    """Constructs payload for GET /api/project/<slug>/characters."""
    chars = synthesize_default_characters(data, proj_dir)
    lookup = build_tag_lookup(chars, proj_dir)

    # Autocomplete list
    autocomplete: list[dict] = []
    seen_tags: set[str] = set()

    for token, item in lookup.items():
        tag = item.get("tag")
        if tag and tag not in seen_tags:
            seen_tags.add(tag)
            autocomplete.append({
                "tag": tag,
                "name": item.get("name", tag),
                "kind": item.get("kind", "character"),
                "avatar": item.get("avatar", ""),
                "description": item.get("description", ""),
            })

    # Add entity shorthands
    for cid, cent in chars.items():
        stag = f"@{cid}"
        if stag not in seen_tags:
            seen_tags.add(stag)
            autocomplete.append({
                "tag": stag,
                "name": cent.get("name", cid),
                "kind": cent.get("kind", "character"),
                "avatar": cent.get("avatar", ""),
                "description": cent.get("physical_traits", ""),
            })

    # Add project anchors to autocomplete
    for anc in data.get("anchor_prompts", []):
        aid = anc.get("id")
        if aid:
            atag = f"@{aid}"
            if atag not in seen_tags:
                seen_tags.add(atag)
                autocomplete.append({
                    "tag": atag,
                    "name": anc.get("filename", aid),
                    "kind": "anchor",
                    "avatar": f"work/anchors_v3/{anc.get('filename', '')}",
                    "description": anc.get("prompt", ""),
                })

    # Scene usage map
    scene_usage: dict[str, list[str]] = {}
    for sc in data.get("scenes", []):
        sid = sc.get("id", "")
        prompts = [sc.get("prompt", ""), sc.get("prompt_m", ""), sc.get("prompt_f", "")]
        for p in prompts:
            if not p:
                continue
            ents = find_prompt_entities(p, lookup)
            for ent in ents:
                eid = ent["entity_id"]
                etag = ent["tag"]
                scene_usage.setdefault(eid, [])
                if sid not in scene_usage[eid]:
                    scene_usage[eid].append(sid)
                scene_usage.setdefault(etag, [])
                if sid not in scene_usage[etag]:
                    scene_usage[etag].append(sid)

    return {
        "success": True,
        "characters": chars,
        "autocomplete": autocomplete,
        "scene_usage": scene_usage,
    }


def save_project_characters(
    proj_dir: Path, cfg_file: Path, characters_data: dict, migrate_vars: bool = False
) -> dict:
    """Saves updated characters dictionary with timestamped backup in backup/."""
    if not cfg_file.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_file}")

    # Timestamped backup per Safety Rule 1
    backup_dir = proj_dir.parent.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{cfg_file.stem}_{ts}.json"
    shutil.copy2(cfg_file, backup_path)

    raw_text = cfg_file.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    data["characters"] = characters_data

    tmp_file = cfg_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_file.replace(cfg_file)

    return {"success": True, "characters": characters_data}


def save_uploaded_reference_image(
    proj_dir: Path, entity_id: str, look_id: str, filename: str, raw_bytes: bytes
) -> str:
    """Saves uploaded reference image to proj_dir / references / entity_id / filename."""
    clean_entity = re.sub(r"[^a-zA-Z0-9_-]", "_", entity_id).strip("_") or "character"
    clean_fn = Path(filename).name
    target_dir = proj_dir / "references" / clean_entity
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / clean_fn
    target_file.write_bytes(raw_bytes)
    return f"references/{clean_entity}/{clean_fn}"


def crop_reference_from_clip(
    proj_dir: Path,
    clip_name: str,
    timestamp: float,
    crop_box: dict | None,
    output_rel_path: str,
) -> str:
    """Extracts a frame from rendered clip using ffmpeg and writes to output_rel_path."""
    clean_clip = Path(clip_name).name
    candidate_paths = [
        proj_dir / "work" / "renders_v3" / clean_clip,
        proj_dir / clean_clip,
    ]
    clip_path = next((p for p in candidate_paths if p.exists()), None)
    if not clip_path:
        raise FileNotFoundError(f"Clip not found on disk: {clip_name}")

    out_file = proj_dir / output_rel_path
    out_file.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = resolve_ffmpeg_path(proj_dir, "", None)

    cmd = [
        str(ffmpeg_bin),
        "-ss",
        f"{float(timestamp):.3f}",
        "-i",
        str(clip_path),
        "-vframes",
        "1",
        "-q:v",
        "2",
    ]
    if crop_box and isinstance(crop_box, dict):
        w = int(crop_box.get("w", 512))
        h = int(crop_box.get("h", 640))
        x = int(crop_box.get("x", 0))
        y = int(crop_box.get("y", 0))
        vf = f"crop={w}:{h}:{x}:{y}"
        if "scale" in crop_box:
            vf += f",scale={crop_box['scale']}"
        cmd.extend(["-vf", vf])

    cmd.extend(["-y", str(out_file)])
    subprocess.run(cmd, check=True, capture_output=True)
    return output_rel_path.replace("\\", "/")


def _get_srv():
    try:
        from work.ui import server
        return server
    except ImportError:
        import server
        return server


def handle_characters_api(handler, action: str, slug: str | None = None, body: dict | None = None):
    """Dispatches Character Studio REST requests cleanly to keep server_routes.py <850 lines."""
    import base64
    from http import HTTPStatus

    srv = _get_srv()
    if action == "get_characters":
        if not slug:
            handler.send_error_json("Slug is required", status=HTTPStatus.BAD_REQUEST)
            return
        proj_dir = srv.PROJECTS_DIR / slug
        if not proj_dir.is_dir():
            handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND)
            return
        jsons = sorted(proj_dir.glob("*.json"))
        if not jsons:
            handler.send_error_json("Project JSON not found", status=HTTPStatus.NOT_FOUND)
            return
        try:
            data = json.loads(jsons[-1].read_text(encoding="utf-8"))
            res = get_project_characters_payload(proj_dir, data, slug)
            handler.send_json(res)
        except Exception as e:
            handler.send_error_json(str(e))
        return

    b = body or {}
    s = slug or b.get("slug")
    if not s:
        handler.send_error_json("slug is required", status=HTTPStatus.BAD_REQUEST)
        return
    proj_dir = srv.PROJECTS_DIR / s
    if not proj_dir.is_dir():
        handler.send_error_json("Project not found", status=HTTPStatus.NOT_FOUND)
        return

    if action == "save":
        chars = b.get("characters")
        if chars is None:
            handler.send_error_json("characters payload required")
            return
        jsons = sorted(proj_dir.glob("*.json"))
        if not jsons:
            handler.send_error_json("Project JSON not found", status=HTTPStatus.NOT_FOUND)
            return
        try:
            res = save_project_characters(proj_dir, jsons[-1], chars)
            handler.send_json(res)
        except Exception as e:
            handler.send_error_json(str(e))

    elif action == "upload-image":
        data_url = b.get("data_url", "")
        if not data_url:
            handler.send_error_json("data_url is required")
            return
        try:
            raw_b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
            raw_bytes = base64.b64decode(raw_b64)
            rel_path = save_uploaded_reference_image(
                proj_dir, b.get("entity_id", "character"), b.get("look_id", "look"), b.get("filename", "ref.jpg"), raw_bytes
            )
            handler.send_json({"success": True, "rel_path": rel_path})
        except Exception as e:
            handler.send_error_json(str(e))

    elif action == "crop-from-clip":
        clip_name = b.get("clip_name")
        if not clip_name:
            handler.send_error_json("clip_name is required")
            return
        try:
            ts = float(b.get("timestamp", 0.0))
            eid = b.get("entity_id", "character")
            fn = f"{Path(clip_name).stem}_frame_{int(ts * 1000)}.jpg"
            out_rel = f"references/{eid}/{fn}"
            res = crop_reference_from_clip(proj_dir, clip_name, ts, b.get("crop_box"), out_rel)
            handler.send_json({"success": True, "rel_path": res})
        except Exception as e:
            handler.send_error_json(str(e))
    else:
        handler.send_error_json(f"Unknown characters action: {action}", status=HTTPStatus.NOT_FOUND)

