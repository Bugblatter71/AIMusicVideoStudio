import os
import sys

target_path = r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\models\minimax_h3\components\video_autoencoder.py"
backup_path = os.path.abspath(r"work\video_autoencoder.py.bak")

if not os.path.exists(backup_path):
    print(f"Error: Backup file {backup_path} not found!")
    sys.exit(1)

with open(target_path, "r", encoding="utf-8") as f:
    content = f.read()

target_str = """                    if j == 0:
                        if overlap is not None:
                            chunk = self._blend(overlap, chunk, self.frame_overlap, dim=-3)
                        write_chunk(chunk)"""

replacement_str = """                    if j == 0:
                        if overlap is not None:
                            # Patch A: Calibrate un-normalized ViT boundary DC offset between chunk tail and head
                            overlap_diff = chunk[:, :, :self.frame_overlap].mean(dim=(-2, -1), keepdim=True) - overlap.mean(dim=(-2, -1), keepdim=True)
                            overlap = overlap + overlap_diff
                            chunk = self._blend(overlap, chunk, self.frame_overlap, dim=-3)
                        write_chunk(chunk)"""

# Handle both CRLF and LF
if target_str in content:
    new_content = content.replace(target_str, replacement_str, 1)
elif target_str.replace("\n", "\r\n") in content:
    new_content = content.replace(target_str.replace("\n", "\r\n"), replacement_str.replace("\n", "\r\n"), 1)
else:
    print("Error: Target string not found in video_autoencoder.py!")
    sys.exit(1)

with open(target_path, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Patch A successfully applied to video_autoencoder.py!")

