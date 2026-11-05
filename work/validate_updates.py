import json

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

print("FPS:", d['project']['fps'])
print("Master Output:", d['project']['master_output'])

m_count = 0
f_count = 0
for s in d['scenes']:
    p = (s.get('prompt') or '') + (s.get('prompt_m') or '') + (s.get('prompt_f') or '')
    if 'MALE_STUDIO_DESC' in p or 'MALE_NIGHTOUT_DESC' in p:
        m_count += 1
    if 'FEMALE_STUDIO_DESC' in p or 'FEMALE_NIGHTOUT_DESC' in p:
        f_count += 1

print(f"Scenes with male desc: {m_count} / {len(d['scenes'])}")
print(f"Scenes with female desc: {f_count} / {len(d['scenes'])}")
print("S11 Anchor:", [s for s in d['scenes'] if s['id']=='S11'][0]['anchor'])
print("S17b prompt_m:", [s for s in d['scenes'] if s['id']=='S17b'][0]['prompt_m'])
print("S26b prompt:", [s for s in d['scenes'] if s['id']=='S26b'][0]['prompt'])

