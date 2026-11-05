import json

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

print("--- ANCHOR A11 ---")
for a in d['anchors']:
    if 'A11' in a.get('filename', ''):
        print(a.get('filename'))
        print(a.get('prompt'))

print("\n--- SCENES S17b & S18 ---")
for s in d['scenes']:
    if s['id'] in ['S17b', 'S18']:
        print(f"=== {s['id']} [{s.get('mode')}] (anchor: {s.get('anchor')}) ===")
        print("prompt_m:", s.get('prompt_m'))
        print("prompt_f:", s.get('prompt_f'))
        print("prompt:", s.get('prompt'))

