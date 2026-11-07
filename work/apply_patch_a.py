from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "work"))
try:
    from run_overnight_v3 import auto_detect_wgp_root, find_local_config
    cfg = find_local_config(PROJECT_ROOT)
    wgp_env = os.environ.get("WGP_ROOT") or cfg.get("wgp_root")
    WGP_ROOT = Path(wgp_env).resolve() if (wgp_env and Path(wgp_env).is_dir()) else (auto_detect_wgp_root() or Path("."))
except Exception:
    WGP_ROOT = Path(os.environ.get("WGP_ROOT", "."))

target_path = WGP_ROOT / "models" / "minimax_h3" / "components" / "video_autoencoder.py"
backup_path = PROJECT_ROOT / "work" / "video_autoencoder.py.bak"

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

