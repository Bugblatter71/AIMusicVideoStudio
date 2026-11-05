import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        data = json.loads(line)
        if data.get('step_index') == 624:
            print("=== STEP 624 ===")
            tc = data.get('tool_calls', [])
            for c in tc:
                print("Tool call:", c.get('name'))
                args = c.get('args', {})
                for k, v in args.items():
                    print(f"  {k}: {v}")

