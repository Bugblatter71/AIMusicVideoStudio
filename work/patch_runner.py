from pathlib import Path

runner_path = Path("work/run_overnight_v3.py")
content = runner_path.read_text(encoding="utf-8")

# 1. Add probe_video_fps after probe_video_dimensions
fps_func = '''
def probe_video_fps(ffmpeg: Path, mp4: Path) -> float | None:
    """Probes the average framerate of an MP4 file using ffprobe."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.exists():
        ffprobe = Path("ffprobe")
    try:
        out = subprocess.run(
            [
                str(ffprobe), "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", str(mp4),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        data = json.loads(out.stdout)
        streams = data.get("streams", [])
        if streams:
            rate_str = streams[0].get("avg_frame_rate") or streams[0].get("r_frame_rate", "")
            if "/" in rate_str:
                num, den = rate_str.split("/")
                if float(den) > 0:
                    return float(num) / float(den)
            elif rate_str:
                return float(rate_str)
    except Exception:
        pass
    return None
'''

if "def probe_video_fps" not in content:
    target = 'def probe_video_dimensions(ffmpeg: Path, mp4: Path) -> tuple[int, int] | None:'
    idx = content.find(target)
    if idx != -1:
        content = content[:idx] + fps_func + "\n\n" + content[idx:]
        print("Added probe_video_fps function")

# 2. Add fps check in assemble_progressive_master for dst_clip and dst_placeholder
old_clip_check = 'needs_encode = (not dst_clip.exists()) or (dst_clip.stat().st_mtime < src_clip.stat().st_mtime)'
new_clip_check = '''needs_encode = (not dst_clip.exists()) or (dst_clip.stat().st_mtime < src_clip.stat().st_mtime)
            if not needs_encode and dst_clip.exists():
                clip_fps = probe_video_fps(ctx.ffmpeg, dst_clip)
                if clip_fps is None or abs(clip_fps - ctx.fps) > 0.1:
                    needs_encode = True'''

if old_clip_check in content and "clip_fps = probe_video_fps(ctx.ffmpeg, dst_clip)" not in content:
    content = content.replace(old_clip_check, new_clip_check, 1)
    print("Updated dst_clip needs_encode check with fps validation")

old_ph_check = 'needs_encode = (not dst_placeholder.exists()) or (anchor_path.exists() and dst_placeholder.stat().st_mtime < anchor_path.stat().st_mtime)'
new_ph_check = '''needs_encode = (not dst_placeholder.exists()) or (anchor_path.exists() and dst_placeholder.stat().st_mtime < anchor_path.stat().st_mtime)
            if not needs_encode and dst_placeholder.exists():
                ph_fps = probe_video_fps(ctx.ffmpeg, dst_placeholder)
                if ph_fps is None or abs(ph_fps - ctx.fps) > 0.1:
                    needs_encode = True'''

if old_ph_check in content and "ph_fps = probe_video_fps(ctx.ffmpeg, dst_placeholder)" not in content:
    content = content.replace(old_ph_check, new_ph_check, 1)
    print("Updated dst_placeholder needs_encode check with fps validation")

# 3. Add -r str(ctx.fps) to concat command
old_concat = '''            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",'''

new_concat = '''            "-map", "0:v:0", "-map", "1:a:0",
            "-r", str(ctx.fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",'''

if old_concat in content:
    content = content.replace(old_concat, new_concat, 1)
    print("Added -r ctx.fps to concat command")

runner_path.write_text(content, encoding="utf-8")
print("Finished patching run_overnight_v3.py successfully!")

