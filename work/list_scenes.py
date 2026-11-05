import json
with open('all_or_nothing_v4.json') as f:
    d = json.load(f)
for s in d['scenes']:
    print(f"{s['id']}: mode={s.get('mode')} start={s.get('start')} end={s.get('end')} anchor={s.get('anchor')}")

