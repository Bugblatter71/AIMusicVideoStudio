import subprocess
# Let's inspect the mouth of the woman in S02_Pass1_Left.mp4 vs S02_Pass2_Final.mp4
# Let's check several frames of S02_Pass1_Left.mp4 across the clip
cmd = [
    'tools/ffmpeg.exe', '-y', '-ss', '1.000', '-t', '4.000',
    '-i', 'work/renders_v3/S02_Pass1_Left.mp4',
    '-vf', 'fps=2,scale=640:360', 'work/s02_pass1_anim_%02d.jpg'
]
subprocess.run(cmd, check=True)
print("Extracted animation frames for S02_Pass1_Left")

