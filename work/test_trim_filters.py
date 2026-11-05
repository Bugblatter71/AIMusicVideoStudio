import subprocess, json
from pathlib import Path

# Test trimming S01 with different filter chains
src = Path('work/renders_v3/S01_male_solo.mp4')
dur = 6.800
target_fps = 25
res_str = "1920x1080"

test_commands = {
    "current": [
        r'tools\ffmpeg.exe', '-y', '-i', str(src),
        '-vf', f'trim=start=0.040:duration={dur:.3f},setpts=PTS-STARTPTS,scale={res_str}:flags=lanczos,fps={target_fps}',
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p',
        'work/test_trim_current.mp4'
    ],
    "fps_first": [
        r'tools\ffmpeg.exe', '-y', '-i', str(src),
        '-vf', f'fps={target_fps},trim=start=0.040:duration={dur:.3f},setpts=PTS-STARTPTS,scale={res_str}:flags=lanczos',
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p',
        'work/test_trim_fps_first.mp4'
    ],
    "exact_frames": [
        r'tools\ffmpeg.exe', '-y', '-i', str(src),
        '-vf', f'trim=start=0.040,setpts=PTS-STARTPTS,fps={target_fps},scale={res_str}:flags=lanczos',
        '-frames:v', str(int(round(dur * target_fps))),
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p',
        'work/test_trim_exact_frames.mp4'
    ],
}

for name, cmd in test_commands.items():
    subprocess.run(cmd, capture_output=True)
    probe_cmd = [r'tools\ffprobe.exe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', f'work/test_trim_{name}.mp4']
    res = subprocess.run(probe_cmd, capture_output=True, text=True)
    d = json.loads(res.stdout)
    fmt_dur = float(d['format']['duration'])
    nb_frames = int(d['streams'][0]['nb_frames'])
    print(f"{name:15s}: dur={fmt_dur:.4f}s | frames={nb_frames} | expected={int(round(dur*target_fps))} frames ({dur:.3f}s)")

