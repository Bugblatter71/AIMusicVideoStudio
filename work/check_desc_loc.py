import json

with open('all_or_nothing_v3.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

for section in ['anchors', 'scenes']:
    for idx, item in enumerate(d.get(section, [])):
        txt = json.dumps(item)
        if 'MALE_STUDIO_DESC' in txt:
            print(f"In {section}[{idx}] ({item.get('id') or item.get('filename')}): MALE_STUDIO_DESC is present!")

