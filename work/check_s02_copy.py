import subprocess

cmd = [
    'tools/ffmpeg.exe', '-y', '-ss', '3.000', '-i', 'work/renders_v3/S02_Pass2_Final - Copy.mp4',
    '-vframes', '1', 'work/s02_copy_frame.jpg'
]
subprocess.run(cmd, check=True)
print("Extracted frame from S02_Pass2_Final - Copy.mp4")

