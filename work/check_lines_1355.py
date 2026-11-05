with open('work/run_overnight_v3.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(1355, 1425):
    print(f"{i+1}: {lines[i].rstrip()}")

