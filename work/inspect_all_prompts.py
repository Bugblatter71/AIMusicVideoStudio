import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    sid = s['id']
    mode = s.get('mode')
    prompt = s.get('prompt')
    pm = s.get('prompt_m')
    pf = s.get('prompt_f')
    has_var = False
    for p in [prompt, pm, pf]:
        if p and ('{MALE_STUDIO_DESC}' in p or '{FEMALE_STUDIO_DESC}' in p or 'MALE_STUDIO_DESC' in p):
            has_var = True
    print(f"{sid:5s} | mode={mode:25s} | has_var={has_var} | prompt_keys={[k for k in s if 'prompt' in k]}")
    # print snippet of first prompt
    p_main = prompt or pm or ''
    first_line = p_main.split('\n')[1] if '\n' in p_main else p_main[:100]
    print(f"       -> {first_line[:120]}")

