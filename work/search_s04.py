import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl'

with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        idx = d.get('step_index', 0)
        c = str(d.get('content', ''))
        if 's04' in c.lower() and any(k in c.lower() for k in ['prompt', 'split', 'divider', 'mask', 'seamless', 'pass2', 'two-pass']):
            print(f"=== Step {idx} ({d.get('type')}) ===")
            for l in c.splitlines():
                if any(k in l.lower() for k in ['s04', 'prompt', 'split', 'divider', 'mask', 'seamless', 'pass2']):
                    print("  ", l.strip()[:140])

