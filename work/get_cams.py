import json, re

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    p = s.get('prompt') or s.get('prompt_m', '')
    sentences = [sent.strip() for sent in re.split(r'\. |\n', p) if any(w in sent.lower() for w in ['camera', 'shot', 'angle', 'pan', 'tilt', 'zoom', 'orbit', 'glide', 'drift', 'crane', 'track'])]
    print(f"{s['id']} (anchor: {s.get('anchor')}):")
    for sent in sentences:
        print(f"   -> {sent}")

