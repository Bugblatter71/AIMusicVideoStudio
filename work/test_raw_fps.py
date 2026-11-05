import subprocess, json
from pathlib import Path

# Let's test re-trimming 06_S07 through 08_S09 to 24fps from their raw renders,
# or see what happens if S08, S09 are trimmed at 24fps!
# S08: raw exists: work/renders_v3/S08_Pass2_Final.mp4 (24fps)
# S09: raw exists: work/renders_v3/S09_male_solo.mp4 (24fps)

print("Testing raw render fps:")
for fn in ['S01_male_solo.mp4', 'S08_Pass2_Final.mp4', 'S09_male_solo.mp4']:
    cmd = ['tools/ffmpeg.exe', '-i', f'work/renders_v3/{fn}']
    res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    for l in res.stderr.splitlines():
        if 'fps' in l and 'Stream #0:0' in l:
            print(f"  {fn}: {[p.strip() for p in l.split(',') if 'fps' in p]}")

