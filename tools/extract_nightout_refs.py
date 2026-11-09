"""
Utility to extract character reference sheets for the night-out context
directly from approved video renders (e.g. S18_solo_1344x768.mp4).
"""
import os
import subprocess
from pathlib import Path


def extract_nightout_references(
    video_path: Path,
    output_dir: Path,
    ffmpeg_bin: Path | str = "ffmpeg",
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_str = str(ffmpeg_bin)
    created: list[Path] = []

    crops = [
        # 1. Male front 3/4 at t=3.0s
        ("male_nightout_front34.jpg", "3.0", "crop=360:460:320:120,scale=512:640"),
        # 2. Male profile at t=7.0s
        ("male_nightout_profile.jpg", "7.0", "crop=300:380:10:80,scale=512:640"),
        # 3. Female front 3/4 at t=7.0s
        ("female_nightout_front34.jpg", "7.0", "crop=320:400:260:140,scale=512:640"),
        # 4. Female profile at t=3.0s
        ("female_nightout_profile.jpg", "3.0", "crop=380:480:600:200,scale=512:640"),
    ]

    for filename, timestamp, vf in crops:
        out_file = output_dir / filename
        cmd = [
            ffmpeg_str, "-y",
            "-ss", timestamp,
            "-i", str(video_path),
            "-vf", vf,
            "-vframes", "1",
            "-q:v", "2",
            str(out_file),
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and out_file.exists() and out_file.stat().st_size > 1000:
            created.append(out_file)

    return created


if __name__ == "__main__":
    proj_root = Path(__file__).resolve().parent.parent
    src_video = proj_root / "projects" / "all_or_nothing" / "work" / "renders_v3" / "S18_solo_1344x768.mp4"
    dest_dir = proj_root / "projects" / "all_or_nothing" / "references" / "nightout"
    tools_ffmpeg = proj_root / "tools" / "ffmpeg.exe"
    ff = tools_ffmpeg if tools_ffmpeg.exists() else "ffmpeg"

    if src_video.exists():
        refs = extract_nightout_references(src_video, dest_dir, ff)
        print(f"Extracted {len(refs)} night-out references into {dest_dir}:")
        for r in refs:
            print(f"  - {r.name} ({r.stat().st_size} bytes)")
    else:
        print(f"Source video not found: {src_video}")
