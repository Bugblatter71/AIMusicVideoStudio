import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

print("Scene timings in all_or_nothing_v4.json:")
expected_start = 0.0
for s in d['scenes'][:12]:
    st = s.get('start', 0)
    en = s.get('end', 0)
    dur = en - st
    discrepancy = st - expected_start
    print(f"{s['id']}: start={st:.3f}, end={en:.3f}, dur={dur:.3f}, expected_start_from_prev_dur={expected_start:.3f}, gap={discrepancy:+.3f}")
    expected_start += dur

