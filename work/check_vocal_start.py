import subprocess, struct

def get_audio_peaks(audio_path, start_s, dur_s, step_s=0.1):
    out = "work/temp_stem.raw"
    cmd = [
        'tools/ffmpeg.exe', '-y', '-ss', f"{start_s:.3f}", '-t', f"{dur_s:.3f}",
        '-i', str(audio_path), '-ar', '44100', '-ac', '1', '-f', 'f32le', out
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    with open(out, 'rb') as f:
        data = f.read()
    n = len(data) // 4
    samples = struct.unpack(f"{n}f", data)
    step_samples = int(step_s * 44100)
    peaks = []
    for i in range(0, n, step_samples):
        chunk = samples[i:i+step_samples]
        if chunk:
            rms = (sum(x*x for x in chunk)/len(chunk))**0.5
            peaks.append((round(start_s + i/44100, 2), rms))
    return peaks

print("Vocal RMS in male_vocal.mp3 for S01 (0 to 6.8s):")
p_vocal = get_audio_peaks("male_vocal.mp3", 0.0, 6.8, 0.2)
for t, rms in p_vocal:
    bar = '#' * int(rms * 100)
    print(f"{t:4.2f}s: {rms:6.3f} | {bar}")

