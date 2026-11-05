import subprocess

# Let's inspect the frame timestamps and audio timestamps in the stitched master
# at each scene boundary!
master = "All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4"

# Check S01 (0s), S02 (6.8s), S03 (13.6s), S04 (20.4s), S05 (27.2s), S06 (33.4s), S07 (39.6s), S08 (48.4s), S09 (57.2s)
scenes_to_check = [
    ("S01", 0.0),
    ("S02", 6.8),
    ("S03", 13.6),
    ("S04", 20.4),
    ("S05", 27.2),
    ("S06", 33.4),
    ("S07", 39.6),
    ("S08", 48.4),
    ("S09", 57.2),
]

for sid, expected_t in scenes_to_check:
    # Let's probe frame around expected_t
    cmd = [
        'tools/ffmpeg.exe', '-ss', f"{expected_t:.3f}", '-i', master,
        '-vf', 'showinfo', '-frames:v', '1', '-f', 'null', '-'
    ]
    p = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    for line in p.stderr.splitlines():
        if 'showinfo' in line and 'pts_time:' in line:
            for part in line.split():
                if part.startswith('pts_time:'):
                    print(f"Scene {sid} expected {expected_t:5.2f}s -> Actual PTS in Master = {part}")

