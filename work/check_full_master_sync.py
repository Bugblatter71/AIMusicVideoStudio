import sys
sys.path.append('.')
from work.test_offsets import extract_raw, cross_correlate
import json

master = "All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4"

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

print("Checking Audio Offset across the Master Video:")
for s in d['scenes']:
    sid = s['id']
    st = s['start']
    en = s['end']
    dur = min(3.5, en - st)
    mode = s['mode']
    if 'silent' in mode: continue
    
    # check raw render file
    if mode in ['gva_male_left_silent_right', 'duet_method_b']:
        render_file = f"work/renders_v3/{sid}_Pass1_Left.mp4"
    else:
        render_file = f"work/renders_v3/{sid}_male_solo.mp4"
        
    try:
        sig_m = extract_raw(master, st, dur)
        sig_r = extract_raw(render_file, 0.0, dur)
        offset, corr = cross_correlate(sig_m, sig_r, max_lag_ms=300)
        print(f"  {sid:5s} ({st:6.2f}s - {en:6.2f}s): Render vs Master Audio Lag = {offset:+.2f} ms")
    except Exception as e:
        print(f"  {sid:5s}: error {e}")
