import json
import subprocess
from pathlib import Path

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

master = 'All_Or_Nothing_v4_1_Master_1920x1080_25fps.mp4'
out_dir = Path('work/video_analysis')
out_dir.mkdir(parents=True, exist_ok=True)

print("Extracting analysis frames for each scene...")
for idx, s in enumerate(d['scenes']):
    sid = s['id']
    st = s['start']
    en = s['end']
    dur = en - st
    t_start = st + 0.1
    t_mid = st + dur / 2.0
    t_end = en - 0.1

    f_start = out_dir / f"{idx:02d}_{sid}_start.jpg"
    f_mid = out_dir / f"{idx:02d}_{sid}_mid.jpg"
    f_end = out_dir / f"{idx:02d}_{sid}_end.jpg"

    for t, out_path in [(t_start, f_start), (t_mid, f_mid), (t_end, f_end)]:
        cmd = [
            r'tools\ffmpeg.exe', '-y', '-ss', f"{t:.3f}", '-i', master,
            '-vf', 'scale=640:360', '-vframes', '1', str(out_path)
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print("Extracted start, mid, end frames for all 29 scenes!")

