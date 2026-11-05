import json

with open(r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl', 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        if idx in range(3748, 3760):
            d = json.loads(line)
            role = d.get('source') or d.get('type')
            content = d.get('content', '')[:300]
            print(f"[{idx}] {role}: {content}\n")

