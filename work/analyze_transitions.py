import json
from pathlib import Path
import subprocess

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

scenes = d['scenes']
analysis_dir = Path('work/video_analysis')

print("=== SCENE-BY-SCENE TRANSITIONS & CAMERA MOVEMENT ANALYSIS ===")
for i in range(len(scenes) - 1):
    s_curr = scenes[i]
    s_next = scenes[i + 1]
    sid_curr = s_curr['id']
    sid_next = s_next['id']

    # compare end frame of current scene with start frame of next scene
    f_end_curr = analysis_dir / f"{i:02d}_{sid_curr}_end.jpg"
    f_start_next = analysis_dir / f"{i+1:02d}_{sid_next}_start.jpg"
    
    # compare start vs end frame of current scene to evaluate camera/motion extent
    f_start_curr = analysis_dir / f"{i:02d}_{sid_curr}_start.jpg"
    
    # compute PSNR between start and end of current scene (motion magnitude)
    cmd_motion = [
        r'tools\ffmpeg.exe', '-i', str(f_start_curr), '-i', str(f_end_curr),
        '-filter_complex', 'psnr', '-f', 'null', '-'
    ]
    res_m = subprocess.run(cmd_motion, capture_output=True, text=True)
    psnr_motion = "N/A"
    for line in res_m.stderr.splitlines():
        if 'PSNR' in line:
            psnr_motion = line.split('average:')[-1].split()[0]
            
    print(f"[{sid_curr} -> {sid_next}] at {s_curr['end']:.2f}s | {sid_curr} internal motion PSNR: {psnr_motion} dB | Modes: {s_curr.get('mode')} -> {s_next.get('mode')}")

