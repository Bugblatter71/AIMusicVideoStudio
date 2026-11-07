"""
List all already-downloaded models in Wan2GP and print full schemas for qwen_image_21_turbo_7B and flux2_klein_9b.
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "work"))
try:
    from run_overnight_v3 import auto_detect_wgp_root, find_local_config
    cfg = find_local_config(PROJECT_ROOT)
    wgp_env = os.environ.get("WGP_ROOT") or cfg.get("wgp_root")
    WGP_ROOT = Path(wgp_env).resolve() if (wgp_env and Path(wgp_env).is_dir()) else (auto_detect_wgp_root() or Path("."))
except Exception:
    WGP_ROOT = Path(os.environ.get("WGP_ROOT", "."))

if WGP_ROOT.is_dir():
    try:
        pin_base = WGP_ROOT.parent.parent.parent
        for extra_dir in (
            str(pin_base / "bin"),
            str(pin_base / "bin" / "miniconda" / "Library" / "bin"),
            str(WGP_ROOT / "ffmpeg_bins"),
        ):
            if os.path.isdir(extra_dir):
                os.environ["PATH"] = extra_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

os.environ["GIT_PYTHON_REFRESH"] = "quiet"
sys.path.insert(0, str(WGP_ROOT))
os.chdir(WGP_ROOT)

from shared.api import init

session = init(
    root=WGP_ROOT,
    config_path=WGP_ROOT / "wgp_config.json",
    console_output=False,
)

all_avail = session.list_model_availability()
downloaded = [m for m in all_avail if m.get("available") or m.get("status") != "missing"]
print("Already downloaded models:", json.dumps(downloaded, indent=2))

for mtype in ["qwen_image_21_turbo_7B", "flux2_klein_9b"]:
    print(f"=== Schema {mtype} ===")
    print(json.dumps(session.get_model_schema(mtype), indent=2))
