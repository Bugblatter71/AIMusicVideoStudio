import subprocess, json

with open('all_or_nothing_v4.json', encoding='utf-8') as f:
    d = json.load(f)

master_mp4 = 'All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4'

# Let's inspect the timestamps of frames at scene boundaries in the master video
print("Checking scene transitions in the master video:")
cmd = [
    'tools/ffmpeg.exe', '-i', master_mp4,
    '-vf', 'select=gt(scene\\,0.4)', '-vsync', 'vfr',
    '-f', 'null', '-'
]

# Let's check the exact duration of each clip when concatenated vs master timeline
for idx, s in enumerate(d['scenes'][:8]):
    sid = s['id']
    st = s['start']
    en = s['end']
    clip_path = f"work/trimmed_v3/{idx:02d}_{sid}.mp4"
    # probe duration and frames
    p = subprocess.run([
        'tools/ffmpeg.exe', '-i', clip_path,
        '-v', 'error', '-select_streams', 'v:0',
        '-count_packets', '-show_entries', 'stream=nb_read_packets,r_frame_rate,duration',
        '-of', 'json', 'tools/ffprobe.exe'
    ], capture_output=True, text=True)
    # let's use simple ffmpeg
    p2 = subprocess.run([
        'tools/ffmpeg.exe', '-i', clip_path
    ], stderr=subprocess.PIPE, text=True)
    dur = "none"
    fps = "none"
    for line in p2.stderr.splitlines():
        if "Duration:" in line:
            dur = line.split("Duration:")[1].split(",")[0].strip()
        if "Stream #0:0" in line:
            fps = [x for x in line.split(",") if "fps" in x]
    print(f"Scene {sid} (expected {st:6.2f}s - {en:6.2f}s, len={en-st:.2f}s): clip={dur}, fps={fps}")

