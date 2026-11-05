import subprocess, json
from pathlib import Path

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

fps = 25
res_str = "1920x1080"
renders_dir = Path('work/renders_v3')
test_trimmed = Path('work/test_trimmed_exact')
test_trimmed.mkdir(parents=True, exist_ok=True)

concat_lines = []
cum_frames = 0

print("Generating exact-frame trimmed clips:")
for i, s in enumerate(d['scenes'][:9]):
    sid = s['id']
    st = s['start']
    en = s['end']
    dur = en - st
    start_frame = int(round(st * fps))
    end_frame = int(round(en * fps))
    target_frames = end_frame - start_frame

    # find src clip
    src = None
    if s.get('mode') == 'male_solo':
        src = renders_dir / f"{sid}_male_solo.mp4"
    else:
        src = renders_dir / f"{sid}_Pass2_Final.mp4"
        if not src.exists():
            src = renders_dir / f"{sid}_Pass1_Left.mp4"

    out_clip = test_trimmed / f"{i:02d}_{sid}.mp4"
    
    cmd = [
        r'tools\ffmpeg.exe', '-y', '-i', str(src),
        '-vf', f'scale={res_str}:flags=lanczos,fps={fps}',
        '-frames:v', str(target_frames),
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p',
        str(out_clip)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    actual_start_s = cum_frames / fps
    print(f"  {sid:5s}: target_start={st:6.3f}s | actual_start={actual_start_s:6.3f}s | frames={target_frames:3d} ({target_frames/fps:6.3f}s)")
    cum_frames += target_frames
    concat_lines.append(f"file '{out_clip.as_posix()}'")

concat_file = test_trimmed / "concat.txt"
concat_file.write_text("\n".join(concat_lines), encoding='utf-8')

# Assemble test master up to 66.3s
test_master = Path('work/test_master_exact_s01_s09.mp4')
cmd_master = [
    r'tools\ffmpeg.exe', '-y',
    '-f', 'concat', '-safe', '0', '-i', str(concat_file),
    '-i', 'All Or Nothing.mp3',
    '-map', '0:v:0', '-map', '1:a:0',
    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k',
    '-t', f"{cum_frames/fps:.3f}",
    str(test_master)
]
subprocess.run(cmd_master, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"\nCreated {test_master} ({cum_frames/fps:.3f}s)")

