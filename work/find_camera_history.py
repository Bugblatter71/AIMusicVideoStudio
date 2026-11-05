import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        c = d.get('content') or ''
        th = d.get('thinking') or ''
        txt = c + ' ' + th
        if 'restore the previous camera instructions' in txt or 'unnatural movements in order to stay in shot' in txt:
            print(f"Step {d.get('step_index')}:")
            print(c[:1000])
            print("="*60)

