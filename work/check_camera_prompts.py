import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

print("Camera instructions across scenes:")
for s in d['scenes']:
    sid = s['id']
    pm = s.get('prompt_m') or s.get('prompt') or ''
    pf = s.get('prompt_f') or ''
    print(f"\n=== {sid} ({s.get('mode')}) [{s.get('start')}s - {s.get('end')}s] ===")
    lines = pm.split('\n')
    for l in lines:
        if any(w in l.lower() for w in ['camera', 'push-in', 'tracking', 'drift', 'pan', 'zoom', 'orbit', 'movement', 'angle']):
            print(f"  M: {l}")
    if pf:
        lines_f = pf.split('\n')
        for l in lines_f:
            if any(w in l.lower() for w in ['camera', 'push-in', 'tracking', 'drift', 'pan', 'zoom', 'orbit', 'movement', 'angle']):
                print(f"  F: {l}")

