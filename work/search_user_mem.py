import json

# Let's search transcript for when the user said:
# "remember you'd previously specified no split screen, no dividers. see if you can find the previous instructions we gave for these scenes"
with open(r'C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5\.system_generated\logs\transcript.jsonl', 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        if 'remember you\'d previously specified' in line:
            print(f"Match at line {idx}!")
            # search earlier lines in transcript for where we specified it!
            break

