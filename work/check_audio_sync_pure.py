import subprocess, struct

cmd1 = [
    r'tools\ffmpeg.exe', '-y', '-i', 'work/renders_v3/S09_male_solo.mp4',
    '-t', '5', '-ar', '44100', '-ac', '1', '-f', 'f32le', 'work/s09_clip_audio.raw'
]
subprocess.run(cmd1, check=True)

cmd2 = [
    r'tools\ffmpeg.exe', '-y', '-ss', '57.200', '-i', 'All Or Nothing.mp3',
    '-t', '5', '-ar', '44100', '-ac', '1', '-f', 'f32le', 'work/master_57_2_audio.raw'
]
subprocess.run(cmd2, check=True)

with open('work/s09_clip_audio.raw', 'rb') as f:
    b1 = f.read()
with open('work/master_57_2_audio.raw', 'rb') as f:
    b2 = f.read()

n = min(len(b1), len(b2)) // 4
n = min(n, 44100 * 3) # 3 seconds
a1 = struct.unpack(f'{n}f', b1[:n*4])
a2 = struct.unpack(f'{n}f', b2[:n*4])

mean1 = sum(a1) / n
mean2 = sum(a2) / n
a1 = [x - mean1 for x in a1]
a2 = [x - mean2 for x in a2]

best_corr = -1e9
best_lag = 0
for lag in range(-2000, 2001, 5):
    s = 0.0
    count = 0
    for i in range(max(0, -lag), min(n, n - lag), 10):
        s += a1[i] * a2[i + lag]
        count += 1
    if count > 0:
        c = s / count
        if c > best_corr:
            best_corr = c
            best_lag = lag

print(f"Best coarse lag: {best_lag} samples = {(best_lag / 44100.0)*1000:.2f} ms")

fine_best = best_corr
fine_lag = best_lag
for lag in range(best_lag - 10, best_lag + 11):
    s = 0.0
    count = 0
    for i in range(max(0, -lag), min(n, n - lag)):
        s += a1[i] * a2[i + lag]
        count += 1
    if count > 0 and (s / count) > fine_best:
        fine_best = s / count
        fine_lag = lag

print(f"Refined audio offset: {fine_lag} samples = {(fine_lag / 44100.0)*1000:.2f} ms")
