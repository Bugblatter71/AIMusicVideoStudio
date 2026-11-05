import json

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    for pk in ['prompt', 'prompt_m', 'prompt_f']:
        p = s.get(pk, '') or ''
        if any(w in p.lower() for w in ['split', 'divider', 'vertical']):
            print(f"{s['id']} [{pk}]:")
            for line in p.splitlines():
                if any(w in line.lower() for w in ['split', 'divider', 'vertical']):
                    print("   ", line.strip()[:140])

