import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        data = json.loads(line)
        st = data.get('step_index')
        if st in [975, 976, 977, 978]:
            print(f"=== STEP {st} ===")
            print((data.get('content') or '')[:1500])

