"""
Assemble all 27 rendered scenes into the final 'All Or Nothing' music video (25fps)
and mux with the master soundtrack ('All Or Nothing.mp3').
"""

import json
import subprocess
from pathlib import Path

PROJECT = Path(r".")
FFMPEG = PROJECT / "tools" / "ffmpeg.exe"
RENDERS_DIR = PROJECT / "work" / "renders"
TRIMMED_DIR = PROJECT / "work" / "trimmed"
JOBS_DIR = PROJECT / "work" / "jobs"
MASTER_MP3 = PROJECT / "All Or Nothing.mp3"
FINAL_MP4 = PROJECT / "All_Or_Nothing_MusicVideo_832x480_25fps.mp4"

TRIMMED_DIR.mkdir(parents=True, exist_ok=True)


def build_scene_clip(sc: dict) -> Path:
    sid = sc["id"]
    dur = sc["dur"]
    mode = sc["mode"]
    out_clip = TRIMMED_DIR / f"{sid}_trimmed.mp4"

    if mode in ("male_solo", "visual_only", "duet_method_b"):
        src = RENDERS_DIR / sc["clip"]
        if not src.exists():
            raise FileNotFoundError(f"Missing render for {sid}: {src}")
        subprocess.run(
            [
                str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-t", f"{dur:.3f}",
                "-vf", "fps=25,scale=832:480,format=yuv420p",
                "-an",
                "-c:v", "libx264", "-crf", "14", "-preset", "fast",
                str(out_clip),
            ],
            check=True,
        )
    elif mode == "composite_m_f_silent":
        src_m = RENDERS_DIR / sc["clip_m"]
        src_f = RENDERS_DIR / sc["clip_f"]
        # Side-by-side Venn circle composite (left blue-tinted circle, right amber-tinted circle)
        venn_filter = (
            f"color=c=black:s=832x480:r=25:d={dur:.3f}[bg];"
            "[0:v]fps=25,scale=480:480:force_original_aspect_ratio=increase,crop=480:480,format=yuva420p,"
            "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
            "a='255*clip((215-hypot(X-240,Y-240))/6,0,1)'[m_circ];"
            "[1:v]fps=25,scale=480:480:force_original_aspect_ratio=increase,crop=480:480,format=yuva420p,"
            "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
            "a='255*clip((215-hypot(X-240,Y-240))/6,0,1)'[f_circ];"
            "[bg][m_circ]overlay=32:0[tmp];"
            "[tmp][f_circ]overlay=320:0:format=auto,format=yuv420p[v]"
        )
        subprocess.run(
            [
                str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src_m),
                "-i", str(src_f),
                "-t", f"{dur:.3f}",
                "-filter_complex", venn_filter,
                "-map", "[v]", "-an",
                "-c:v", "libx264", "-crf", "14", "-preset", "fast",
                str(out_clip),
            ],
            check=True,
        )
    elif mode == "composite_venn_drift":
        src_m = RENDERS_DIR / sc["clip_m"]
        src_f = RENDERS_DIR / sc["clip_f"]
        # Animated Venn circles that drift apart over the duration of S16 ("I need a life of my own")
        venn_drift_filter = (
            f"color=c=black:s=832x480:r=25:d={dur:.3f}[bg];"
            "[0:v]fps=25,scale=440:440:force_original_aspect_ratio=increase,crop=440:440,format=yuva420p,"
            "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
            "a='255*clip((195-hypot(X-220,Y-220))/6,0,1)'[m_circ];"
            "[1:v]fps=25,scale=440:440:force_original_aspect_ratio=increase,crop=440:440,format=yuva420p,"
            "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
            "a='255*clip((195-hypot(X-220,Y-220))/6,0,1)'[f_circ];"
            f"[bg][m_circ]overlay=x='80-70*(t/{dur:.3f})':y=20[tmp];"
            f"[tmp][f_circ]overlay=x='312+70*(t/{dur:.3f})':y=20:format=auto,format=yuv420p[v]"
        )
        subprocess.run(
            [
                str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src_m),
                "-i", str(src_f),
                "-t", f"{dur:.3f}",
                "-filter_complex", venn_drift_filter,
                "-map", "[v]", "-an",
                "-c:v", "libx264", "-crf", "14", "-preset", "fast",
                str(out_clip),
            ],
            check=True,
        )
    return out_clip


def main():
    meta_path = JOBS_DIR / "scenes_timeline.json"
    scenes = json.loads(meta_path.read_text(encoding="utf-8"))

    trimmed_files = []
    for sc in scenes:
        clip = build_scene_clip(sc)
        trimmed_files.append(clip)
        print(f"Prepared {sc['id']} ({sc['dur']:.2f}s) -> {clip.name}")

    concat_list = TRIMMED_DIR / "concat_list.txt"
    concat_list.write_text(
        "\n".join(f"file '{f.as_posix()}'" for f in trimmed_files),
        encoding="utf-8",
    )

    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-i", str(MASTER_MP3),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-crf", "15", "-preset", "medium",
            "-c:a", "aac", "-b:a", "320k",
            "-shortest",
            str(FINAL_MP4),
        ],
        check=True,
    )
    print(f"\n=== Master Music Video Assembled: {FINAL_MP4} ===")


if __name__ == "__main__":
    main()

