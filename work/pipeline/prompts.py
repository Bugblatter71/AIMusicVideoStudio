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


def resolve_master_resolution(ctx, progressive: bool = False) -> str:
    """Returns target 'WxH' for Stage 3 master assembly (normalizing 1920x1088 -> 1920x1080)."""
    if progressive and str(ctx.project_cfg.get("progressive_stitch_mode", "always_master")).strip().lower() == "fast_preview":
        raw = ctx.current_resolution
    else:
        master_cfg = str(ctx.project_cfg.get("master_resolution", "match")).strip()
        if not master_cfg or master_cfg.lower() in ("match", "render", "auto", "default"):
            raw = ctx.current_resolution
        else:
            raw = ctx.resolve_resolution(master_cfg)
    return "1920x1080" if raw == "1920x1088" else raw


def _ensure_sentence_period(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    return s if s[-1] in ".!?" else f"{s}."


def compile_modular_scene_prompt(sc: dict, pass_num: int | None = None) -> str:
    """Compiles structured prompt components into the canonical H3 multimodal string.
    If sc.get('prompt_mode') != 'components', returns the raw prompt for pass_num.
    """
    if not isinstance(sc, dict):
        return ""
    if sc.get("prompt_mode") != "components":
        if pass_num == 1:
            return str(sc.get("prompt_m") or sc.get("prompt") or "")
        if pass_num == 2:
            return str(sc.get("prompt_f") or sc.get("prompt") or "")
        return str(sc.get("prompt") or sc.get("prompt_m") or sc.get("prompt_f") or "")

    lock_cam = sc.get("duet_lock_camera", True)
    if pass_num == 1 and not lock_cam and sc.get("prompt_camera_m"):
        camera = str(sc["prompt_camera_m"]).strip()
    elif pass_num == 2 and not lock_cam and sc.get("prompt_camera_f"):
        camera = str(sc["prompt_camera_f"]).strip()
    else:
        camera = str(sc.get("prompt_camera") or "").strip()

    atmo = str(sc.get("prompt_atmosphere") or "").strip()
    if pass_num == 1:
        perf = str(sc.get("prompt_performance_m") or sc.get("prompt_performance") or "").strip()
        sound = str(sc.get("prompt_soundscape_m") or sc.get("prompt_soundscape") or "Studio room tone and vocal performance.").strip()
    elif pass_num == 2:
        perf = str(sc.get("prompt_performance_f") or sc.get("prompt_performance") or "").strip()
        sound = str(sc.get("prompt_soundscape_f") or sc.get("prompt_soundscape") or "Quiet studio room tone, no speech.").strip()
    else:
        perf = str(sc.get("prompt_performance") or "").strip()
        sound = str(sc.get("prompt_soundscape") or "Studio room tone under the vocal performance.").strip()

    musicians = str(sc.get("prompt_musicians") or "").strip()
    has_end_anchor = bool(sc.get("anchor_end")) and pass_num != 2
    lead_in = (
        "[Shot 1] Continue seamlessly from <Picture 1> and transition smoothly into <Picture 2> by the end of the shot."
        if has_end_anchor
        else "[Shot 1] Continue directly from <Picture 1>."
    )

    visual_clauses = [_ensure_sentence_period(p) for p in (camera, atmo, perf, musicians) if p and p.strip()]
    visual_body = " ".join(visual_clauses)
    sound_clean = _ensure_sentence_period(sound) if sound and sound.lower() != "n/a" else (sound or "Studio room tone.")

    return (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
        f"integrated_multimodal_description: {lead_in}"
        + (f" {visual_body}" if visual_body else "")
        + f"\noverall_soundscape: {sound_clean}\n"
        "non_diegetic_music: N/A"
    )


def parse_raw_prompt_to_components(raw_prompt: str) -> dict[str, str]:
    """Non-destructively decomposes a raw H3 or freeform scene prompt into modular components:
    prompt_camera, prompt_atmosphere, prompt_performance, prompt_musicians, prompt_soundscape.
    Guarantees zero sentence loss across round-trip parsing.
    """
    res = {
        "prompt_camera": "",
        "prompt_atmosphere": "",
        "prompt_performance": "",
        "prompt_musicians": "",
        "prompt_soundscape": "Studio room tone under the vocal performance.",
    }
    if not raw_prompt or not str(raw_prompt).strip():
        return res

    desc_lines: list[str] = []
    sound_lines: list[str] = []
    section = "desc"

    for raw_line in str(raw_prompt).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("for the target video,"):
            continue
        if low.startswith("non_diegetic_music:"):
            section = "music"
            continue
        if low.startswith("overall_soundscape:"):
            section = "sound"
            val = line[len("overall_soundscape:") :].strip()
            if val:
                sound_lines.append(val)
            continue
        if low.startswith("integrated_multimodal_description:"):
            section = "desc"
            val = line[len("integrated_multimodal_description:") :].strip()
            if val:
                desc_lines.append(val)
            continue
        if section == "desc":
            desc_lines.append(line)
        elif section == "sound":
            sound_lines.append(line)

    if sound_lines:
        res["prompt_soundscape"] = " ".join(sound_lines).strip()

    full_desc = " ".join(desc_lines).strip()
    full_desc = re.sub(
        r"^\[Shot\s*1\]\s*Continue\s+(?:directly|seamlessly)\s+from\s+<Picture\s*1>"
        r"(?:\s+and\s+transition\s+smoothly\s+into\s+<Picture\s*2>\s+by\s+the\s+end\s+of\s+the\s+shot)?\s*\.?\s*",
        "",
        full_desc,
        flags=re.IGNORECASE,
    ).strip()

    if not full_desc:
        return res

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", full_desc) if s.strip()]
    cam_parts: list[str] = []
    atmo_parts: list[str] = []
    perf_parts: list[str] = []
    mus_parts: list[str] = []

    cam_re = re.compile(
        r"\b(camera|tripod|push-in|dolly|orbital|tracking\s+shot|pan|panning|tilt|zoom|wide-angle\s+shot|medium-wide\s+shot|crane\s+shot|fixed\s+horizon|locked-off)\b",
        re.IGNORECASE,
    )
    lead_re = re.compile(r"(\{MALE_|\{FEMALE_|@[a-zA-Z0-9_-]+|\bsings\b|\bsinging\b|\blistening\b|\blip\s+movements\b)", re.IGNORECASE)
    mus_re = re.compile(
        r"(\bdrummer\b|\bdrum\s+kit\b|\bdrumsticks\b|\bpianist\b|\bwoodwind\b|\bbassoon|\bclarinet|\bbrass\b|\btrumpet|\btrombone|\bbass\s+guitar|\bwait\s+and\s+don't\s+play\b|<[^>]+>)",
        re.IGNORECASE,
    )
    atmo_re = re.compile(
        r"^(Across\s+the\s+unified\s+wooden\s+floor|Inside\s+the\s+dark\s+recording\s+soundstage|Surrounding\s+her\s+in\s+atmospheric\s+bokeh|On\s+the\s+wooden\s+floor)\b",
        re.IGNORECASE,
    )

    for sent in sentences:
        has_lead = bool(lead_re.search(sent))
        has_cam = bool(cam_re.search(sent))
        has_mus = bool(mus_re.search(sent))
        if has_cam and not has_lead and not has_mus:
            cam_parts.append(sent)
        elif has_mus and not has_lead:
            mus_parts.append(sent)
        elif atmo_re.search(sent) and not has_lead and not has_mus:
            atmo_parts.append(sent)
        else:
            perf_parts.append(sent)

    res["prompt_camera"] = " ".join(cam_parts).strip()
    res["prompt_atmosphere"] = " ".join(atmo_parts).strip()
    res["prompt_performance"] = " ".join(perf_parts).strip()
    res["prompt_musicians"] = " ".join(mus_parts).strip()
    return res


def resolve_scene_negative_prompt(
    sc: dict,
    rspec: dict | None = None,
    vshared: dict | None = None,
    pass_num: int | None = None,
) -> tuple[str, str]:
    """Returns (effective_negative_prompt, custom_neg_signature).
    When the scene has no custom negative prompt and mode is 'inherit', custom_neg_signature is ''
    so legacy scene task hashes remain 100% unchanged.
    """
    rspec = rspec or {}
    vshared = vshared or {}
    sc = sc or {}
    global_neg = str(rspec.get("negative_prompt", vshared.get("negative_prompt", ""))).strip()
    base_scene_neg = str(sc.get("negative_prompt") or "").strip()
    pass_neg = ""
    if pass_num == 1:
        pass_neg = str(sc.get("negative_prompt_m") or "").strip()
    elif pass_num == 2:
        pass_neg = str(sc.get("negative_prompt_f") or "").strip()

    scene_combined = ", ".join(p for p in (base_scene_neg, pass_neg) if p)
    mode = str(sc.get("negative_prompt_mode") or "inherit").strip().lower()

    if not scene_combined and mode != "override":
        return global_neg, ""

    raw_sources = [scene_combined] if mode == "override" else [global_neg, scene_combined]
    seen: set[str] = set()
    deduped: list[str] = []
    for src in raw_sources:
        for tok in src.split(","):
            t = tok.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                deduped.append(t)

    effective = ", ".join(deduped)
    custom_sig = f"{mode}:{scene_combined}"
    return effective, custom_sig

