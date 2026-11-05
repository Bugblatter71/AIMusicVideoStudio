import subprocess

# Let's inspect the center column of s02_pass1_frame.jpg vs s02_pass2_frame.jpg
# Or check the anchor A03!
anchor_path = "work/anchors_v3/A03 - 2Man_singing_and_woman_listening_90de80da_2K.jpg"
print(f"Anchor A03 exists: {subprocess.run(['dir', str(anchor_path)], shell=True).returncode == 0}")

