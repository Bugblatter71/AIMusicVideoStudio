import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl'

with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        idx = d.get('step_index', 0)
        if 615 <= idx <= 650:
            c = str(d.get('content', ''))
            print(f"=== Step {idx} ({d.get('type')}) ===")
            print(c[:400])
            print()

