import json

with open('all_or_nothing_v3.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

print("Scene keys and prompt sample in all_or_nothing_v3.json:")
for s in d.get('scenes', [])[:6]:
    sid = s['id']
    print(f"=== {sid} ===")
    for k in ['prompt', 'prompt_m', 'prompt_f']:
        if k in s:
            print(f"  {k}: {s[k][:160]}...")

