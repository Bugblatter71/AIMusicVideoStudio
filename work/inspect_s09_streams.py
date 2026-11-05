import subprocess, json

for name in ['work/renders_v3/S09_male_solo.mp4', 'work/trimmed_v3/08_S09.mp4']:
    cmd = [r'tools\ffprobe.exe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', name]
    res = subprocess.run(cmd, capture_output=True, text=True)
    d = json.loads(res.stdout)
    print(f"=== {name} ===")
    print("Format duration:", d['format'].get('duration'))
    for s in d['streams']:
        print(f"Stream {s['index']}: type={s['codec_type']} codec={s['codec_name']} dur={s.get('duration')} frames={s.get('nb_frames')} fps={s.get('r_frame_rate')} sr={s.get('sample_rate')}")

