import subprocess, json
from pathlib import Path

renders_dir = Path('work/renders_v3')
clips = [
    'S01_male_solo.mp4',
    'S02_Pass2_Final.mp4',
    'S03_male_solo.mp4',
    'S04_male_solo.mp4',
    'S05_male_solo.mp4',
    'S06_male_solo.mp4',
    'S07_male_solo.mp4',
    'S08_Pass2_Final.mp4',
    'S09_male_solo.mp4'
]

for c in clips:
    p = renders_dir / c
    if not p.exists():
        continue
    # extract frame 0 and frame 1
    f0 = f"work/check_{c}_f0.jpg"
    f1 = f"work/check_{c}_f1.jpg"
    subprocess.run([r'tools\ffmpeg.exe', '-y', '-i', str(p), '-vf', 'select=eq(n\\,0)', '-vframes', '1', f0], capture_output=True)
    subprocess.run([r'tools\ffmpeg.exe', '-y', '-i', str(p), '-vf', 'select=eq(n\\,1)', '-vframes', '1', f1], capture_output=True)

    # compare mean brightness of f0 vs f1 using ffmpeg psnr or ssim
    cmd = [
        r'tools\ffmpeg.exe', '-i', f0, '-i', f1,
        '-filter_complex', 'psnr', '-f', 'null', '-'
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    for line in res.stderr.splitlines():
        if 'PSNR' in line:
            print(f"{c:25s}: {line.strip()}")

