import subprocess
from pathlib import Path

trimmed_dir = Path("work/trimmed_v3")
clips = sorted(list(trimmed_dir.glob("*_S*.mp4")))

print(f"{'Clip':<15} | {'Duration':<10} | {'FPS':<6} | {'Target Dur':<10}")
print("-" * 50)

import json
with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

for idx, c in enumerate(clips):
    if "placeholder" in c.name: continue
    cmd = ['tools/ffmpeg.exe', '-i', str(c)]
    p = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    dur = "?"
    fps = "?"
    for line in p.stderr.splitlines():
        if "Duration:" in line:
            dur = line.split("Duration:")[1].split(",")[0].strip()
        if "fps" in line and "Stream #0:0" in line:
            for part in line.split(","):
                if "fps" in part:
                    fps = part.strip()
    sc_dur = d['scenes'][idx]['end'] - d['scenes'][idx]['start']
    print(f"{c.name:<15} | {dur:<10} | {fps:<6} | {sc_dur:<10.3f}")

