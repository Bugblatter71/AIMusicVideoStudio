import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    if s['id'] in ['S02', 'S04', 'S08']:
        print(f"=== {s['id']} ===")
        print("prompt_m:", repr(s.get('prompt_m')))
        print("prompt_f:", repr(s.get('prompt_f')))

