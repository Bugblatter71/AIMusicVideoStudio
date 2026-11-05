import json

with open("all_or_nothing_v4.json", "r", encoding="utf-8") as f:
    d = json.load(f)

print("=== ANCHORS A03 & A04 in all_or_nothing_v4.json ===")
for a in d.get("anchors", []):
    fn = a.get("filename", "")
    if "A03" in fn or "A04" in fn:
        print(f"File: {fn}")
        print(f"Seed: {a.get('seed')}")
        print(f"Prompt: {a.get('prompt')[:200]}...")
        print()

print("=== SCENES S02, S04, S08 in all_or_nothing_v4.json ===")
for sc in d.get("scenes", []):
    sid = sc.get("id")
    if sid in ("S02", "S04", "S08"):
        print(f"Scene: {sid}, Anchor: {sc.get('anchor')}")
        print(f"split_x: {sc.get('split_x')}")
        print(f"Prompt M:\n{sc.get('prompt_m')}")
        print(f"Prompt F:\n{sc.get('prompt_f')}")
        print("-" * 50)

