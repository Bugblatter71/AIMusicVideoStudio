import json
with open('all_or_nothing_v4.json') as f:
    d = json.load(f)
for a in d.get('anchors', []):
    if a.get('id') in ['A03', 'A04'] or 'A03' in a.get('filename', '') or 'A04' in a.get('filename', ''):
        print(a)

