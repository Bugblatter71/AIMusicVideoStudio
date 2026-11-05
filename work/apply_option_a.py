import json
from pathlib import Path
import shutil

# 1. Update all_or_nothing_v4.json
json_path = Path('all_or_nothing_v4.json')
with open(json_path, 'r', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    if s['id'] == 'S04':
        s['mode'] = 'male_solo'
        s['seed'] = 8204
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided medium-wide shot inside one open wood-paneled recording studio (no split-screen, no vertical divider, continuous studio room and continuous wooden floor across the entire frame). "
            "On the left side inside his sapphire-blue floor circle, the Black male singer at the brass microphone sings emotionally toward the woman on the right with open eyes, natural hand and head movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        # remove 2-pass fields
        s.pop('split_x', None)
        s.pop('seed_m', None)
        s.pop('seed_f', None)
        s.pop('prompt_m', None)
        s.pop('prompt_f', None)
        print("Updated S04 to mode: male_solo")

with open(json_path, 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2)

# 2. Copy S04_Pass1_Left.mp4 to S04_male_solo.mp4
src = Path('work/renders_v3/S04_Pass1_Left.mp4')
dst = Path('work/renders_v3/S04_male_solo.mp4')
if src.exists():
    shutil.copy2(src, dst)
    print(f"Copied {src} -> {dst} ({dst.stat().st_size} bytes)")
else:
    print(f"Source not found: {src}")

