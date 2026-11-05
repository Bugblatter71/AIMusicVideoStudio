import json, subprocess
from pathlib import Path

# Run stitch-only or assemble with our patched runner
cmd = [
    'python', 'work/run_overnight_v3.py', 'all_or_nothing_v4.json', '--stitch-only'
]
print("Running run_overnight_v3.py --stitch-only to re-trim all 25fps clips and re-stitch master at 24fps...")
p = subprocess.run(cmd, capture_output=True, text=True)
print(p.stdout)
if p.stderr:
    print("Stderr:", p.stderr[:500])

