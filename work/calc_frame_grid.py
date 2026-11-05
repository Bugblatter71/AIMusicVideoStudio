import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

fps = 25
trim_first_s = 0.04

print(f"{'Scene':6s} | {'Target Start':12s} | {'Target Dur':10s} | {'Target Frames':13s} | {'Exact Dur':10s}")
print("-" * 65)

cum_time = 0.0
for s in d['scenes'][:12]:
    sid = s['id']
    st = s['start']
    en = s['end']
    dur = en - st
    # exact number of frames at 25 fps
    n_frames = int(round(dur * fps))
    exact_dur = n_frames / fps
    drift = cum_time - st
    print(f"{sid:6s} | {st:10.3f}s | {dur:8.3f}s | {n_frames:8d} frames | {cum_time:10.3f}s (drift={drift:+.3f}s)")
    cum_time += exact_dur

