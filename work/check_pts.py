import subprocess

def extract_frame_pts(video_path, start_s, count=10):
    cmd = [
        'tools/ffmpeg.exe', '-ss', f"{start_s:.3f}", '-i', str(video_path),
        '-vf', 'showinfo', '-frames:v', str(count), '-f', 'null', '-'
    ]
    p = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    pts_list = []
    for line in p.stderr.splitlines():
        if 'showinfo' in line and 'pts_time:' in line:
            parts = line.split()
            for part in parts:
                if part.startswith('pts_time:'):
                    pts_list.append(part.split(':')[1])
    return pts_list

print("Master 24fps PTS around 0.8s:", extract_frame_pts('All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4', 0.7, 5))
print("Raw render PTS around 0.8s:", extract_frame_pts('work/renders_v3/S01_male_solo.mp4', 0.7, 5))
print("Trimmed 00_S01 PTS around 0.8s:", extract_frame_pts('work/trimmed_v3/00_S01.mp4', 0.7, 5))

