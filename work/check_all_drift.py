import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

fps = 25
cum_time = 0.0
print(f"{'Scene':6s} | {'Target Start':12s} | {'Target Dur':10s} | {'Frames':8s} | {'Actual Start':12s} | {'Drift (ms)':10s}")
print("-" * 75)

max_abs_drift = 0.0
for s in d['scenes']:
    sid = s['id']
    st = s['start']
    en = s['end']
    dur = en - st
    n_frames = int(round(dur * fps))
    exact_dur = n_frames / fps
    drift_ms = (cum_time - st) * 1000
    if abs(drift_ms) > max_abs_drift:
        max_abs_drift = abs(drift_ms)
    print(f"{sid:6s} | {st:10.3f}s | {dur:8.3f}s | {n_frames:6d}f | {cum_time:10.3f}s | {drift_ms:+8.1f} ms")
    cum_time += exact_dur

print(f"\nTotal duration: {cum_time:.3f}s | Target duration: {d['project']['total_duration']:.3f}s")
print(f"Max absolute drift across whole video: {max_abs_drift:.1f} ms")

