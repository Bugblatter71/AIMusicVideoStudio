import subprocess
import numpy as np

# Extract 5 seconds of audio from S09_male_solo.mp4
cmd1 = [
    r'tools\ffmpeg.exe', '-y', '-i', 'work/renders_v3/S09_male_solo.mp4',
    '-t', '5', '-ar', '44100', '-ac', '1', '-f', 'f32le', 'work/s09_clip_audio.raw'
]
subprocess.run(cmd1, capture_output=True)

# Extract 5 seconds of audio from All Or Nothing.mp3 starting at 57.200s
cmd2 = [
    r'tools\ffmpeg.exe', '-y', '-ss', '57.200', '-i', 'All Or Nothing.mp3',
    '-t', '5', '-ar', '44100', '-ac', '1', '-f', 'f32le', 'work/master_57_2_audio.raw'
]
subprocess.run(cmd2, capture_output=True)

# Cross-correlate to find exact audio offset between S09's internal audio and the master track at 57.2s
a1 = np.fromfile('work/s09_clip_audio.raw', dtype=np.float32)
a2 = np.fromfile('work/master_57_2_audio.raw', dtype=np.float32)

min_len = min(len(a1), len(a2))
a1 = a1[:min_len]
a2 = a2[:min_len]

# normalize
a1 = a1 - np.mean(a1)
a2 = a2 - np.mean(a2)

# cross-correlation
corr = np.correlate(a1, a2, mode='full')
lag = np.argmax(corr) - (len(a2) - 1)
time_offset_ms = (lag / 44100.0) * 1000

print(f"Correlation peak lag: {lag} samples = {time_offset_ms:.2f} ms")

