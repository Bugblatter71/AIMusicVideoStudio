with open('work/run_overnight_v3.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(1415, 1475):
    print(f"{i+1}: {lines[i].rstrip()}")

