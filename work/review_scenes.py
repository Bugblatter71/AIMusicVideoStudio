import json

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    p = s.get('prompt') or (s.get('prompt_m', '') + ' | ' + s.get('prompt_f', ''))
    print(f"=== {s['id']} [{s.get('mode')}] (start={s['start']}, end={s['end']}, anchor={s.get('anchor')}) ===")
    print(p.strip())
    print()

