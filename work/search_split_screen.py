import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl'
keywords = ['split screen', 'split-screen', 'divider', 'dividers', 'vertical line', 'seam', 'no split', 'border']

with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        idx = d.get('step_index', 0)
        c = str(d.get('content', ''))
        for kw in keywords:
            if kw in c.lower():
                lines = [l for l in c.splitlines() if kw in l.lower()]
                print(f"=== Step {idx} ({d.get('type')}) [kw: {kw}] ===")
                for l in lines[:3]:
                    print("  ", l.strip()[:140])
                break

