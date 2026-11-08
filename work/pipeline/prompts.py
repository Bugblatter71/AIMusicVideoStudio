import re
from pathlib import Path


def extract_anchor_id(name_or_str: str | Path) -> str:
    """Extracts normalized anchor ID like 'A01', 'A02' ... 'A14' from any filename or reference string.
    Supports formats like:
      'A01 - Man_singing...', 'A01_Male_Orchestra...', 'A01.jpg', 'A1', 'a01'
    """
    s = Path(str(name_or_str)).name.strip()
    m = re.match(r"^(A\d{1,2})([ _\-\.]|$)", s, re.IGNORECASE)
    if m:
        num = int(m.group(1)[1:])
        return f"A{num:02d}"
    parts = re.split(r"[-_ \.]", s)
    if parts:
        p0 = parts[0].strip().upper()
        if p0.startswith("A") and p0[1:].isdigit():
            num = int(p0[1:])
            return f"A{num:02d}"
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
        line = line.replace("<Picture 1>", "the starting frame")
        if line.startswith("overall_soundscape:"):
            snd = line[len("overall_soundscape:") :].strip()
            if snd and snd.lower() != "n/a":
                lines.append(f"Audio soundscape: {snd}")
            continue
        lines.append(line)
    return " ".join(lines)
