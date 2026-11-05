"""
List all already-downloaded models in Wan2GP and print full schemas for qwen_image_21_turbo_7B and flux2_klein_9b.
"""

import json
import os
import sys
from pathlib import Path

PINOKIO_BIN = r"C:\Users\bugbl\Downloads\pinokio\bin"
MINICONDA_LIB = r"C:\Users\bugbl\Downloads\pinokio\bin\miniconda\Library\bin"
os.environ["PATH"] = f"{PINOKIO_BIN};{MINICONDA_LIB};" + os.environ.get("PATH", "")
os.environ["GIT_PYTHON_REFRESH"] = "quiet"

WGP_ROOT = Path(r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app")
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
