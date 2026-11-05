import subprocess

master = "All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4"

# Probe packet timestamps of the master file from 0s to 40s
cmd = [
    'tools/ffprobe.exe', '-v', 'quiet', '-select_streams', 'v:0',
    '-show_packets', '-show_entries', 'packet=pts_time,duration_time',
    '-read_intervals', '0%45', master
]
p = subprocess.run(cmd, capture_output=True, text=True)

pts_times = []
for line in p.stdout.splitlines():
    if line.startswith('pts_time='):
        pts_times.append(float(line.split('=')[1]))

print(f"Total video packets in first 45s: {len(pts_times)}")
print(f"PTS at index 0: {pts_times[0]:.4f}s")
# 163 frames = S01 end
print(f"PTS at index 162 (end of S01): {pts_times[162]:.4f}s")
print(f"PTS at index 163 (start of S02): {pts_times[163]:.4f}s")
# 163 + 163 = 326 (end of S02)
print(f"PTS at index 325 (end of S02): {pts_times[325]:.4f}s")
print(f"PTS at index 326 (start of S03): {pts_times[326]:.4f}s")
# 326 + 164 = 490 (end of S03)
print(f"PTS at index 489 (end of S03): {pts_times[489]:.4f}s")
print(f"PTS at index 490 (start of S04): {pts_times[490]:.4f}s")
# S06 end: index 949
print(f"PTS at index 949 (end of S06): {pts_times[949]:.4f}s")
print(f"PTS at index 950 (start of S07): {pts_times[950]:.4f}s")
# Look at packet delta after index 950 (when it hits the 25fps clip):
deltas = [pts_times[i+1] - pts_times[i] for i in range(945, 960)]
print("Packet deltas around S06->S07 boundary:")
for i, d in enumerate(deltas):
    idx = 945 + i
    print(f"  Frame {idx} -> {idx+1}: delta = {d:.5f}s ({1/d:.2f} fps)")

