import json

transcript_path = r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl'
with open(transcript_path, 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        data = json.loads(line)
        content = data.get('content', '') or ''
        thinking = data.get('thinking', '') or ''
        tool_calls = str(data.get('tool_calls', ''))
        txt = content + " " + thinking + " " + tool_calls
        lower = txt.lower()
        if 'split' in lower and ('screen' in lower or 'divider' in lower or 'vertical' in lower):
            print(f"Step {data.get('step_index')}:")
            for match_word in ['split screen', 'split-screen', 'divider', 'dividers']:
                pos = lower.find(match_word)
                if pos != -1:
                    start = max(0, pos - 100)
                    end = min(len(txt), pos + 150)
                    print(f"   [{match_word}]: {txt[start:end]!r}")

