import json

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    m = s.get('mode', '')
    if 'gva' in m or 'duet' in m:
        print(f"{s['id']:5s} | mode={m:28s} | split_x={s.get('split_x')} | anchor={s.get('anchor')}")

