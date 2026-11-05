import subprocess
from pathlib import Path

PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
FFMPEG = PROJECT / "tools" / "ffmpeg.exe"
RENDERS = PROJECT / "work" / "renders"
AUDIO = PROJECT / "work" / "audio"


def main():
    male_v = RENDERS / "Duet_Male_Close.mp4"
    female_v = RENDERS / "Duet_Female_Close.mp4"
    single_v = RENDERS / "Duet_SinglePass_Both.mp4"
    master_a = AUDIO / "Duet_104.4_master.wav"

    # 1. Split-screen / side-by-side composite of the two close-ups + master audio
    out_split = RENDERS / "Duet_Composite_SplitScreen_Master.mp4"
    cmd_split = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(male_v),
        "-i", str(female_v),
        "-i", str(master_a),
        "-filter_complex",
        (
            "[0:v]crop=416:480:208:0[left];"
            "[1:v]crop=416:480:208:0[right];"
            "[left][right]hstack=inputs=2[v]"
        ),
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-crf", "16", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_split),
    ]
    subprocess.run(cmd_split, check=True)
    print(f"Created {out_split}")

    # 2. Venn-circle composite of the two close-ups (overlapping glowing blue & amber circles on dark canvas) + master audio
    out_venn = RENDERS / "Duet_Composite_VennCircles_Master.mp4"
    # Two radius-210 circles centered at (290, 240) and (542, 240) with soft feathering and subtle additive blend in overlap
    venn_filter = (
        "color=c=black:s=832x480:r=25:d=6.32[bg];"
        "[0:v]scale=480:480:force_original_aspect_ratio=increase,crop=480:480,format=yuva420p,"
        "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
        "a='255*clip((215-hypot(X-240,Y-240))/6,0,1)'[m_circ];"
        "[1:v]scale=480:480:force_original_aspect_ratio=increase,crop=480:480,format=yuva420p,"
        "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
        "a='255*clip((215-hypot(X-240,Y-240))/6,0,1)'[f_circ];"
        "[bg][m_circ]overlay=40:0[tmp];"
        "[tmp][f_circ]overlay=312:0:format=auto[v]"
    )
    cmd_venn = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(male_v),
        "-i", str(female_v),
        "-i", str(master_a),
        "-filter_complex", venn_filter,
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-crf", "16", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_venn),
    ]
    subprocess.run(cmd_venn, check=True)
    print(f"Created {out_venn}")

    # 3. Also mux the single-pass two-shot with master audio so comparison is apples-to-apples
    if single_v.exists():
        out_single_master = RENDERS / "Duet_SinglePass_Both_Master.mp4"
        cmd_single = [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(single_v),
            "-i", str(master_a),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(out_single_master),
        ]
        subprocess.run(cmd_single, check=True)
        print(f"Created {out_single_master}")


if __name__ == "__main__":
    main()

