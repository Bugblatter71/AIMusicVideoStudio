import json, glob

# Search all json files, python files, text files for how S02 was phrased originally!
for fn in glob.glob('*.json') + glob.glob('backup/*.json') + glob.glob('work/*.py') + glob.glob('work/*.json'):
    try:
        with open(fn, 'r', encoding='utf-8') as f:
            content = f.read()
        if 'S02' in content and 'prompt' in content:
            # try to parse as json
            try:
                data = json.loads(content)
                if 'scenes' in data:
                    s2 = [s for s in data['scenes'] if s.get('id') == 'S02']
                    if s2:
                        print(f"File {fn}: S02 prompt_m:")
                        print(s2[0].get('prompt_m') or s2[0].get('prompt'))
                        print("-" * 50)
            except Exception:
                pass
    except Exception:
        pass

