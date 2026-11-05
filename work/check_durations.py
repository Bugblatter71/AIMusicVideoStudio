from pathlib import Path
import subprocess, json

renders_dir = Path('work/renders_v3')
trimmed_dir = Path('work/trimmed_v3')

for f in sorted(renders_dir.glob('S0*.mp4')):
    cmd = [r'tools\ffprobe.exe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(f)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout:
        d = json.loads(res.stdout)
        fmt = d.get('format', {})
        v_stream = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'), {})
        a_stream = next((s for s in d.get('streams', []) if s.get('codec_type') == 'audio'), {})
        dur = fmt.get('duration')
        frames = v_stream.get('nb_frames')
        fps = v_stream.get('r_frame_rate')
        print(f"RENDER: {f.name:25s} | dur={dur}s | frames={frames} | fps={fps} | a_sr={a_stream.get('sample_rate')}")

print("\n--- TRIMMED CLIPS ---")
for f in sorted(trimmed_dir.glob('*S0*.mp4')):
    cmd = [r'tools\ffprobe.exe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(f)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout:
        d = json.loads(res.stdout)
        fmt = d.get('format', {})
        v_stream = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'), {})
        dur = fmt.get('duration')
        frames = v_stream.get('nb_frames')
        fps = v_stream.get('r_frame_rate')
        print(f"TRIMMED: {f.name:25s} | dur={dur}s | frames={frames} | fps={fps}")

