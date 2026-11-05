import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        c = (d.get('content') or '') + ' ' + (d.get('thinking') or '')
        if 'trim_first_frame' in c or 'flash' in c.lower():
            if 'trim_first_frame' in c:
                print(f"Step {d.get('step_index')}:")
                pos = c.find('trim_first_frame')
                print(c[max(0, pos-80):min(len(c), pos+200)])
                print('-'*40)

