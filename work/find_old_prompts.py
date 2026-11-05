import json, glob

files = glob.glob('*.json') + glob.glob('work/*.json') + glob.glob('work/*.py')
print("Searching files for MALE_STUDIO_DESC in scenes:")
for fn in files:
    try:
        with open(fn, 'r', encoding='utf-8') as f:
            content = f.read()
            if 'MALE_STUDIO_DESC' in content and 'scenes' in content:
                print(f"Found in {fn}!")
    except Exception:
        pass

