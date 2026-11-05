import json

matches = []
with open(r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl', 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        if idx > 3748: break
        l_lower = line.lower()
        if 'divider' in l_lower or 'split screen' in l_lower or 'split-screen' in l_lower or 'dividing line' in l_lower:
            d = json.loads(line)
            role = d.get('source') or d.get('type')
            content = d.get('content', '')
            matches.append((idx, role, content[:200]))

print(f"Total matches before 3748: {len(matches)}")
for idx, role, c in matches[-15:]:
    print(f"[{idx}] {role}: {c}")

