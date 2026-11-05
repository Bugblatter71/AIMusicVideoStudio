import subprocess

# Let's inspect the concat list and cumulative durations of the clips in concat_list.txt!
with open('work/trimmed_v3/concat_list.txt', 'r', encoding='utf-8') as f:
    lines = [line.strip().split("'")[1] for line in f if line.strip()]

import json
with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

cum_time = 0.0
print(f"{'Scene':<5} | {'Expected Start':<14} | {'Trimmed Dur':<12} | {'Cumul Time':<12} | {'Diff':<8} | {'FPS':<6}")
print("-" * 75)

for idx, p in enumerate(lines):
    sc = d['scenes'][idx]
    sid = sc['id']
    st = sc['start']
    
    # get duration of clip
    cmd = ['tools/ffmpeg.exe', '-i', p]
    res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    dur = 0.0
    fps = "none"
    for line in res.stderr.splitlines():
        if "Duration:" in line:
            t_str = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = t_str.split(":")
            dur = float(h)*3600 + float(m)*60 + float(s)
        if "Stream #0:0" in line:
            for part in line.split(","):
                if "fps" in part:
                    fps = part.strip()
                    
    diff = cum_time - st
    print(f"{sid:<5} | {st:<14.3f} | {dur:<12.3f} | {cum_time:<12.3f} | {diff:<+8.3f} | {fps:<6}")
    cum_time += dur

