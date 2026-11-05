import subprocess

for idx in [0, 1, 2, 3]:
    cmd = [
        r'tools\ffmpeg.exe', '-y', '-i', 'work/renders_v3/S09_male_solo.mp4',
        '-vf', f'select=eq(n\\,{idx})', '-vframes', '1',
        f'work/s09_f{idx}.jpg'
    ]
    subprocess.run(cmd, capture_output=True)
print("Extracted frames 0, 1, 2, 3 of S09_male_solo.mp4")

