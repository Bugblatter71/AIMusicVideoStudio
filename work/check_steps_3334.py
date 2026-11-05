import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        st = d.get('step_index')
        if 3334 <= st <= 3350:
            print(f"=== STEP {st} ({d.get('type')}) ===")
            c = d.get('content') or ''
            tc = str(d.get('tool_calls') or '')
            print((c + ' ' + tc)[:1000])

