import subprocess, json
from pathlib import Path

for f in Path('work/audio_v3').glob('*S09*'):
    cmd = [r'tools\ffprobe.exe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(f)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout:
        d = json.loads(res.stdout)
        dur = d.get('format', {}).get('duration')
        sr = d.get('streams', [{}])[0].get('sample_rate')
        print(f"AUDIO: {f.name} | dur={dur}s | sr={sr}")

