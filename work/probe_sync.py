import subprocess, json

def probe(file_path):
    cmd = [
        'tools/ffmpeg.exe', '-i', str(file_path)
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stderr.splitlines():
        if 'Duration:' in line or 'Stream #0:0' in line or 'fps' in line:
            print(f"  {line.strip()}")

print("--- PROBING NEW TRIMMED CLIPS ---")
for f in ['work/trimmed_v3/00_S01.mp4', 'work/trimmed_v3/01_S02.mp4', 'work/trimmed_v3/05_S06.mp4', 'work/trimmed_v3/06_S07.mp4']:
    print(f"File: {f}")
    probe(f)

print("\n--- PROBING RAW RENDERS ---")
for f in ['work/renders_v3/S01_Pass1_Raw.mp4', 'work/renders_v3/S06_Pass1_Raw.mp4']:
    print(f"File: {f}")
    probe(f)

print("\n--- PROBING MASTER VIDEO ---")
probe('All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4')

