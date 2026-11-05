import subprocess
from pathlib import Path

out_dir = Path("work/s01_mouth_check")
out_dir.mkdir(exist_ok=True)

# Extract frames every 2 frames around 0.5s to 1.5s
cmd = [
    'tools/ffmpeg.exe', '-y', '-ss', '0.500', '-t', '1.000',
    '-i', 'work/trimmed_v3/00_S01.mp4',
    '-vf', 'fps=12', str(out_dir / 'frame_%02d.jpg')
]
subprocess.run(cmd, check=True)
print("Extracted mouth check frames for S01")

