import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, l in enumerate(lines):
    if '"id": "S02"' in l or '"id": "S04"' in l or '"id": "S08"' in l:
        print(f"Line {i+1}: {l.strip()}")

