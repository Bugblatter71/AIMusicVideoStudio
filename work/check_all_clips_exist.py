import json
from pathlib import Path

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

renders_dir = Path('work/renders_v3')
for s in d['scenes']:
    sid = s['id']
    mode = s.get('mode')
    clip_name = ""
    if mode == 'male_solo':
        clip_name = f"{sid}_male_solo.mp4"
    elif mode == 'silent_locked':
        clip_name = f"{sid}_silent.mp4"
    elif mode in ('gva_male_left_silent_right', 'duet_method_b'):
        clip_name = f"{sid}_Pass2_Final.mp4"
    p = renders_dir / clip_name
    print(f"{sid:5s} | expected: {clip_name:25s} | exists={p.exists()} ({p.stat().st_size if p.exists() else 0} bytes)")

