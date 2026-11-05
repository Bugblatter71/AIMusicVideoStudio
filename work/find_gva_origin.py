import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript_full.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        c = (d.get('content') or '') + ' ' + (d.get('thinking') or '')
        if 'gva_male_left_silent_right' in c:
            print(f"Step {d.get('step_index')}:")
            pos = c.find('gva_male_left_silent_right')
            print(c[max(0, pos-100):min(len(c), pos+300)])
            print('-'*40)

