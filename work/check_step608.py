import json
transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get('step_index') in [604, 608]:
            print(f"=== STEP {d.get('step_index')} ({d.get('type')}) ===")
            print((d.get('content') or '')[:1500])

