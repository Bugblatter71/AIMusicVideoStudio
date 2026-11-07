from pathlib import Path
import os
import shutil
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

shutil.copy2(backup_path, target_path)
print(f"Successfully restored video_autoencoder.py from {backup_path}!")

