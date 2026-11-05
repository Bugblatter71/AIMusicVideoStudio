import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        data = json.loads(line)
        st = data.get('step_index')
        if 625 <= st <= 645:
            print(f"=== STEP {st} ({data.get('type')}) ===")
            txt = data.get('content') or ''
            print(txt[:400])
            tc = data.get('tool_calls')
            if tc:
                print('TOOL CALLS:', str(tc)[:400])

