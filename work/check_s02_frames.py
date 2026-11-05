import subprocess

cmd1 = [
    'tools/ffmpeg.exe', '-y', '-ss', '3.000', '-i', 'work/renders_v3/S02_Pass1_Left.mp4',
    '-vframes', '1', 'work/s02_pass1_frame.jpg'
]
cmd2 = [
    'tools/ffmpeg.exe', '-y', '-ss', '3.000', '-i', 'work/renders_v3/S02_Pass2_Final.mp4',
    '-vframes', '1', 'work/s02_pass2_frame.jpg'
]
subprocess.run(cmd1, check=True)
subprocess.run(cmd2, check=True)
print("Extracted frames from S02 Pass1 and Pass2")

