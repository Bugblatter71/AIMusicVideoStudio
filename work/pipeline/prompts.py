import re
from pathlib import Path


def extract_anchor_id(name_or_str: str | Path) -> str:
    """Extracts normalized anchor ID like 'A01', 'A05b' ... 'A14' from any filename or reference string.
    Supports formats like:
      'A01 - Man_singing...', 'A05b_Woodwinds...', 'A01.jpg', 'A1', 'a01'
    """
    s = Path(str(name_or_str)).name.strip()
    m = re.match(r"^A(\d{1,2})([a-z]?)(?:[ _\-\.]|$)", s, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        suf = m.group(2).lower() if m.group(2) else ""
        return f"A{num:02d}{suf}"
    parts = re.split(r"[-_ \.]", s)
    if parts:
        p0 = parts[0].strip()
        m2 = re.match(r"^A(\d+)([a-z]?)$", p0, re.IGNORECASE)
        if m2:
            num = int(m2.group(1))
            suf = m2.group(2).lower() if m2.group(2) else ""
            return f"A{num:02d}{suf}"
    return s.upper()


def expand_variables(text: str, variables: dict[str, str]) -> str:
    out = text
    for k, v in variables.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def resolve_marker_prompt(prompt_text: str, scene_start: float, markers: list[dict], fps: float = 24.0) -> str:
    """Replaces <marker_name> tags with the marker's relative offset from clip start."""
    if not prompt_text or not markers:
        return prompt_text

    marker_map = {m["name"].strip().lower(): m for m in markers if "name" in m and "time" in m}
    if not marker_map:
        return prompt_text

    def repl(match):
        full_inner = match.group(1).strip()
        parts = [p.strip() for p in full_inner.split(":", 1)]
        tag_name = parts[0].lower()
        modifier = parts[1].lower() if len(parts) > 1 else None

        if tag_name not in marker_map:
            return match.group(0)

        marker = marker_map[tag_name]
        rel_time = float(marker["time"]) - float(scene_start)
        rel_frame = max(0, round(rel_time * float(fps)))

        # Context lookaround
        start_idx = match.start()
        end_idx = match.end()
        preceding = prompt_text[max(0, start_idx - 15) : start_idx]
        following = prompt_text[end_idx : end_idx + 15]

        preceded_by_frame = bool(re.search(r"\b(frame|frames|f)\s*$", preceding, re.IGNORECASE))
        followed_by_frame = bool(re.match(r"^\s*(frames?|f\b)", following, re.IGNORECASE))
        followed_by_sec = bool(re.match(r"^\s*(seconds?|s\b)", following, re.IGNORECASE))

        # Explicit modifiers
        if modifier in ("frame", "frames"):
            return str(rel_frame) if (preceded_by_frame or followed_by_frame) else f"frame {rel_frame}"
        elif modifier in ("f", "raw_frame"):
            return str(rel_frame)
        elif modifier in ("sec", "seconds"):
            return f"{rel_time:.2f} seconds"
        elif modifier == "s":
            return f"{rel_time:.2f}s"
        elif modifier in ("raw", "num"):
            return f"{rel_time:.2f}"

        # Context-dependent defaults
        if preceded_by_frame or followed_by_frame:
            return str(rel_frame)
        if followed_by_sec:
            return f"{rel_time:.2f}"
        return f"{rel_time:.2f}s"

    return re.sub(r"<([^>]+)>", repl, prompt_text)


def adapt_ref_tags(prompt: str, ref_tag_style: str) -> str:
    """Translates <image1>..<image4> tags to match the selected renderer's reference syntax."""
    style = (ref_tag_style or "<imageN>").strip()
    if style == "<imageN>":
        return prompt
    out = prompt
    for i in range(1, 10):
        src = f"<image{i}>"
        if src not in out:
            continue
        if style == "<ref_image_N0>":
            out = out.replace(src, f"<ref_image_{i - 1}>")
        elif style == "Image N":
            out = out.replace(src, f"Image {i}")
        elif style == "image N":
            out = out.replace(src, f"image {i}")
    return out


def find_ref_tags(prompt: str) -> list[int]:
    """Finds all unique 1-based indices from <imageN> tags, sorted ascending."""
    if not prompt:
        return []
    matches = re.findall(r"<image(\d+)>", prompt, flags=re.IGNORECASE)
    return sorted(set(int(m) for m in matches))


def resolve_scene_refs(
    prompt: str,
    ref_list: list[Path | str] | None,
) -> tuple[list[Path], dict[int, int]]:
    """
    Given an expanded prompt and a list of available reference image paths,
    resolves which references are actually cited via <imageN>.
    Returns (selected_paths, tag_to_picture_map).
    Picture numbers begin at 2 (since <Picture 1> is the starting frame anchor).
    """
    tags = find_ref_tags(prompt)
    if not tags or not ref_list:
        return [], {}

    selected_paths: list[Path] = []
    path_to_pic: dict[str, int] = {}
    tag_to_pic: dict[int, int] = {}

    for t in tags:
        idx = t - 1
        if 0 <= idx < len(ref_list):
            raw_p = ref_list[idx]
            p = Path(raw_p) if isinstance(raw_p, (str, Path)) else None
            if p and p.exists():
                p_resolved = p.resolve()
                p_key = str(p_resolved).lower()
                if p_key not in path_to_pic:
                    if len(selected_paths) >= 9:
                        raise ValueError("MiniMax H3 Ref2VA accepts at most 9 reference images (got >9)")
                    selected_paths.append(p)
                    pic_num = 1 + len(selected_paths)
                    path_to_pic[p_key] = pic_num
                tag_to_pic[t] = path_to_pic[p_key]

    return selected_paths, tag_to_pic


def remap_ref_tags_to_pictures(prompt: str, mapping: dict[int, int]) -> str:
    """Replaces <imageN> tags with <Picture mapping[N]>."""
    if not prompt or not mapping:
        return prompt
    remapped = prompt
    for img_idx, pic_idx in sorted(mapping.items(), key=lambda x: -x[0]):
        remapped = re.sub(rf"<image{img_idx}>", f"<Picture {pic_idx}>", remapped, flags=re.IGNORECASE)
    return remapped


def strip_dangling_ref_tags(prompt: str) -> str:
    """Removes 'from <image1> and <image2>' clauses when rendering on non-reference models."""
    if not prompt:
        return ""
    cleaned = re.sub(
        r"\s*\(?\s*from\s+<image\d+>(?:\s*(?:and|,)\s*<image\d+>)*\s*\)?",
        " ",
        prompt,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r" +", " ", cleaned)
    cleaned = re.sub(r" +([,\.!\?])", r"\1", cleaned)
    return cleaned.strip()


def build_ref2va_prompt(prompt: str, mapping: dict[int, int]) -> str:
    """
    Transforms an FL2VA structured prompt into Ref2VA's native six-section prompt:
    subject_definitions, summary, retention_analysis, detailed_description,
    overall_soundscape, non_diegetic_music.
    Ensures zero internal blank lines so sliding windows are never unintentionally broken.
    """
    remapped = remap_ref_tags_to_pictures(prompt, mapping)
    lines = [l.strip() for l in remapped.splitlines() if l.strip()]

    first_frame_line = ""
    desc_lines = []
    soundscape = "Studio ambience and vocals."
    non_diegetic = "N/A"
    current_sec = "desc"

    for line in lines:
        if line.lower().startswith("for the target video,"):
            first_frame_line = line
            continue
        if line.lower().startswith("integrated_multimodal_description:"):
            current_sec = "desc"
            content = line[len("integrated_multimodal_description:") :].strip()
            if content:
                desc_lines.append(content)
            continue
        elif line.lower().startswith("overall_soundscape:"):
            current_sec = "sound"
            soundscape = line[len("overall_soundscape:") :].strip() or soundscape
            continue
        elif line.lower().startswith("non_diegetic_music:"):
            current_sec = "music"
            non_diegetic = line[len("non_diegetic_music:") :].strip() or non_diegetic
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

    pic_numbers = sorted(set(mapping.values()))
    male_pics = [mapping[k] for k in (1, 2) if k in mapping]
    female_pics = [mapping[k] for k in (3, 4) if k in mapping]

    subj_defs = []
    retention_lines = [
        "<Picture 1> (anchors [Shot 1] at 0.00s): fully_preserved - composition, background, camera framing, and wardrobe."
    ]

    s_idx = 1
    if male_pics:
        pics_str = " and ".join(f"<Picture {p}>" for p in sorted(male_pics))
        subj_defs.append(f"<Subject {s_idx}> is the male lead from {pics_str}, preserving facial identity, skin tone, haircut and beard.")
        retention_lines.append(
            f"<Subject {s_idx}> (appears in [Shot 1]): attribute_transfer - facial features and hair; wardrobe and lighting follow <Picture 1>."
        )
        s_idx += 1

    if female_pics:
        pics_str = " and ".join(f"<Picture {p}>" for p in sorted(female_pics))
        subj_defs.append(f"<Subject {s_idx}> is the female lead from {pics_str}, preserving facial identity, eyes, and hair.")
        retention_lines.append(
            f"<Subject {s_idx}> (appears in [Shot 1]): attribute_transfer - facial features and hair; wardrobe and lighting follow <Picture 1>."
        )
        s_idx += 1

    other_pics = [p for p in pic_numbers if p not in male_pics and p not in female_pics]
    if other_pics and not subj_defs:
        pics_str = " and ".join(f"<Picture {p}>" for p in other_pics)
        subj_defs.append(f"<Subject 1> is the character from {pics_str}, preserving recognizable facial identity.")
        retention_lines.append(
            "<Subject 1> (appears in [Shot 1]): attribute_transfer - facial features and hair; wardrobe and lighting follow <Picture 1>."
        )

    sections = [
        first_frame_line,
        "subject_definitions:",
        *subj_defs,
        "summary:",
        "[reference generation] Continue the scene established in <Picture 1>, preserving character identity.",
        "retention_analysis:",
        *retention_lines,
        "detailed_description:",
        f"The target video is an audiovisual scene. {desc_text}" if not desc_text.startswith("[Shot 1]") else desc_text,
        "overall_soundscape:",
        soundscape,
        "non_diegetic_music:",
        non_diegetic,
    ]
    return "\n".join(s for s in sections if s)


def adapt_video_prompt(prompt: str, prompt_style: str) -> str:
    """Adapts H3 structured prompts when switching to non-H3 video renderers."""
    style = (prompt_style or "h3").strip().lower()
    if style == "h3":
        return prompt
    lines = []
    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("For the target video,"):
            continue
        if line.startswith("non_diegetic_music:"):
            continue
        if line.startswith("integrated_multimodal_description:"):
            line = line[len("integrated_multimodal_description:") :].strip()
        if line.startswith("[Shot 1]"):
            line = line[len("[Shot 1]") :].strip()
        if line.startswith("Continue directly from <Picture 1>."):
            line = line[len("Continue directly from <Picture 1>.") :].strip()
        line = line.replace("<Picture 1>", "the starting frame").replace("<Picture 2>", "the ending frame")
        if line.startswith("overall_soundscape:"):
            snd = line[len("overall_soundscape:") :].strip()
            if snd and snd.lower() != "n/a":
                lines.append(f"Audio soundscape: {snd}")
            continue
        lines.append(line)
    return " ".join(lines)
