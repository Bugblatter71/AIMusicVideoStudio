import json
from pathlib import Path

# Let's inspect which video files in work/renders_v3 are newer than state hashes
# or which tasks in all_tasks would need render
with open('work/jobs_v3/state_hashes.json', encoding='utf-8') as f:
    sh = json.load(f)

with open('work/jobs_v3/v3_manifest.json', encoding='utf-8') as f:
    tasks = json.load(f)

print("Status of manifest tasks on disk:")
for t in tasks:
    fn = t['output_filename']
    mp4 = Path('work/renders_v3') / f"{fn}.mp4"
    exists = mp4.exists()
    sz = mp4.stat().st_size if exists else 0
    mtime = mp4.stat().st_mtime if exists else 0
    import datetime
    dt = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S') if exists else "N/A"
    print(f"  {fn:<25} | Exists: {str(exists):<5} | Size: {sz/1024/1024:5.1f}MB | MTime: {dt}")

