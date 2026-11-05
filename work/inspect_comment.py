import subprocess
import json
import re

v_file = "2026-11-03-21h01m09s_seed752991305_The male singer in close-up sings the pivotal pre-.mp4"
cmd = [r"tools\ffmpeg.exe", "-i", v_file, "-f", "ffmetadata", "-"]
p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

comment_raw = None
for line in p.stdout.splitlines():
    if line.startswith("comment="):
        comment_raw = line[len("comment="):]
        break

if not comment_raw:
    print("No comment found!")
    exit(0)

# Extract fields using regex for robustness
print("Raw comment length:", len(comment_raw))

# Parse JSON safely
clean_raw = comment_raw.replace(r"\#", "#")
# The prompt has unescaped quotes or extra quotes at the end
# Replace prompt with dummy to parse everything else
m = re.search(r'"prompt":"(.*?)(?<!\\)","resolution"', clean_raw)
if m:
    prompt_val = m.group(1)
    clean_raw = clean_raw[:m.start()] + '"prompt":"__PROMPT__","resolution"' + clean_raw[m.end():]
    data = json.loads(clean_raw)
    data["prompt"] = prompt_val
else:
    data = json.loads(clean_raw)

print(f"\n{'FIELD':<36} | VALUE")
print("-" * 80)
for k, v in sorted(data.items()):
    print(f"{k:<36} | {v}")

