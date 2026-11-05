import subprocess, struct, math

def extract_raw(audio_path, start_s, dur_s):
    out = "work/temp_audio.raw"
    cmd = [
        'tools/ffmpeg.exe', '-y', '-ss', f"{start_s:.3f}", '-t', f"{dur_s:.3f}",
        '-i', str(audio_path), '-ar', '44100', '-ac', '1', '-f', 'f32le', out
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    with open(out, 'rb') as f:
        data = f.read()
    n = len(data) // 4
    return struct.unpack(f"{n}f", data)

def cross_correlate(sig1, sig2, max_lag_ms=1000, sr=44100):
    max_lag = int(max_lag_ms * sr / 1000)
    # Downsample by 10 for speed
    ds = 10
    s1 = sig1[::ds]
    s2 = sig2[::ds]
    l_max = max_lag // ds
    
    n = min(len(s1), len(s2)) - 2 * l_max
    if n <= 0:
        return 0, 0
    
    best_corr = -1e9
    best_lag = 0
    for lag in range(-l_max, l_max):
        corr = 0.0
        # dot product
        for i in range(0, n, 2):
            corr += s1[i + l_max] * s2[i + l_max + lag]
        if corr > best_corr:
            best_corr = corr
            best_lag = lag * ds
            
    # Fine search around best_lag
    best_fine = best_lag
    best_fine_corr = -1e9
    n_fine = min(len(sig1), len(sig2)) - 2 * max_lag
    for lag in range(best_lag - ds*2, best_lag + ds*2):
        if lag < -max_lag or lag > max_lag: continue
        corr = 0.0
        for i in range(0, n_fine, 4):
            corr += sig1[i + max_lag] * sig2[i + max_lag + lag]
        if corr > best_fine_corr:
            best_fine_corr = corr
            best_fine = lag
            
    offset_ms = (best_fine / sr) * 1000.0
    return offset_ms, best_fine_corr

master = "All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4"

# Check scenes 1 to 6
import json
with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes'][:7]:
    sid = s['id']
    st = s['start']
    en = s['end']
    dur = min(4.0, en - st)
    
    # Render audio
    if s['mode'] in ['gva_male_left_silent_right', 'duet_method_b']:
        render_file = f"work/renders_v3/{sid}_Pass2_Final.mp4"
    else:
        render_file = f"work/renders_v3/{sid}_male_solo.mp4"
        
    try:
        sig_master = extract_raw(master, st, dur)
        sig_render = extract_raw(render_file, 0.0, dur)
        
        offset_ms, corr = cross_correlate(sig_master, sig_render, max_lag_ms=500)
        print(f"Scene {sid} (start {st:.2f}s): Render Audio vs Master Audio Offset = {offset_ms:+.2f} ms")
    except Exception as e:
        print(f"Scene {sid}: Error {e}")

