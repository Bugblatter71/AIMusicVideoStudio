import subprocess
import glob
import re

v_files = sorted(glob.glob("work/renders_v3/*S01*.mp4"))
for v_file in v_files:
    cmd = [r"tools\ffmpeg.exe", "-i", v_file, "-f", "ffmetadata", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    for line in p.stdout.splitlines():
        if line.startswith("comment="):
            raw = line[len("comment="):]
            m = re.search(r'"model_type":("[^"]*")', raw)
            r = re.search(r'"resolution":("[^"]*")', raw)
            l = re.search(r'"video_length":(\d+)', raw)
            sw = re.search(r'"sliding_window_size":(\d+)', raw)
            ap = re.search(r'"audio_prompt_type":("[^"]*")', raw)
            mp = re.search(r'"multi_prompts_gen_type":("[^"]*")', raw)
            m_val = m.group(1) if m else "None"
            r_val = r.group(1) if r else "None"
            l_val = l.group(1) if l else "None"
            sw_val = sw.group(1) if sw else "None"
            ap_val = ap.group(1) if ap else "None"
            mp_val = mp.group(1) if mp else "None"
            print(f"{v_file}:\n  model={m_val}, res={r_val}, len={l_val}, win={sw_val}, audio={ap_val}, mp={mp_val}")
