import json
import subprocess
from pathlib import Path

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

trimmed_dir = Path('work/trimmed_v3')

cum_trimmed_time = 0.0
for i, sc in enumerate(d['scenes'][:12]):
    sid = sc['id']
    target_start = sc['start']
    target_end = sc['end']
    target_dur = target_end - target_start

    # check actual trimmed clip file
    clip_file = trimmed_dir / f"{i:02d}_{sid}.mp4"
    if not clip_file.exists():
        clip_file = trimmed_dir / f"{i:02d}_{sid}_placeholder.mp4"

    cmd = [r'tools\ffprobe.exe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(clip_file)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    actual_dur = target_dur
    actual_frames = 0
    if res.stdout:
        info = json.loads(res.stdout)
        actual_dur = float(info['format']['duration'])
        actual_frames = int(info['streams'][0].get('nb_frames', 0))

    drift = cum_trimmed_time - target_start
    print(f"{sid:5s} | Target: [{target_start:6.2f} - {target_end:6.2f}] (dur={target_dur:5.2f}s) | "
          f"Master Actual Start: {cum_trimmed_time:6.3f}s | Drift: {drift:+6.3f}s | "
          f"Trimmed dur: {actual_dur:6.3f}s ({actual_frames} frames)")
    cum_trimmed_time += actual_dur

