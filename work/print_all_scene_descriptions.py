import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    sid = s['id']
    st = s['start']
    en = s['end']
    mode = s.get('mode')
    anchor = s.get('anchor')
    p = s.get('prompt') or s.get('prompt_m')
    p_lines = p.strip().split('\n')
    desc = ""
    for l in p_lines:
        if 'integrated_multimodal_description:' in l:
            desc = l.replace('integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>.', '').strip()
    print(f"[{sid}] ({st:.1f}s - {en:.1f}s, {en-st:.1f}s) Anchor: {anchor} | Mode: {mode}")
    print(f"   Prompt: {desc}")

