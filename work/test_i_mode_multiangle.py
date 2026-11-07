"""
Test Qwen Image 2.1 Turbo 7B (qwen_image_21_turbo_7B, 8 steps, Qwen3-VL-8B Vision-Language Encoder)
with the 4 multi-angle portraits (<image1> & <image2> = male front & profile; <image3> & <image4> = female front & profile).
"""

import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "work"))
try:
    from run_overnight_v3 import auto_detect_wgp_root, find_local_config
    cfg = find_local_config(PROJECT)
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
LOCAL_OUT = PROJECT / "work" / "local_images"
REF_DIR = PROJECT / "work" / "ref_sheets"

sys.path.insert(0, str(WGP_ROOT))
os.chdir(WGP_ROOT)

from shared.api import init

session = init(
    root=WGP_ROOT,
    config_path=WGP_ROOT / "wgp_config.json",
    output_dir=LOCAL_OUT,
    console_output=True,
    console_isatty=False,
)

cfg = session.get_default_settings("qwen_image_21_turbo_7B")
cfg["model_type"] = "qwen_image_21_turbo_7B"
cfg["image_mode"] = 1
cfg["resolution"] = "1664x960"
cfg["seed"] = 7720
cfg["output_filename"] = "Test_Qwen21Turbo_4AnglePortraits"
cfg["remove_background_images_ref"] = 0
cfg["video_prompt_type"] = "I"
cfg["image_refs"] = [
    str(REF_DIR / "male_angle1_front34.jpg"),
    str(REF_DIR / "male_angle3_profile.jpg"),
    str(REF_DIR / "female_angle1_front34.jpg"),
    str(REF_DIR / "female_angle3_profile.jpg"),
]
cfg["prompt"] = (
    "A cinematic eye-level 16:9 medium two-shot inside a warm wood-paneled recording studio. "
    "On the left half stands the exact Black male singer from <image1> and <image2> (keep his exact face, nose, lips, short tapered fade, neat beard, and dark slate-navy suede jacket over burgundy shirt), "
    "turned in three-quarter profile facing right toward the woman with his eyes open and mouth closed. "
    "On the right half stands the exact woman from <image3> and <image4> (keep her exact face, nose, lips, wavy dark auburn shoulder-length hair with curtain bangs, hazel eyes, black suede biker jacket over dark V-neck top and silver necklace), "
    "turned in three-quarter profile facing left toward the man with her eyes open and mouth closed, holding an acoustic guitar, "
    "with a cellist and upright pianist seated in overlapping warm golden light circles on the wooden floor in the background."
)

t0 = time.time()
job = session.submit_task(cfg)
for event in job.events.iter(timeout=0.5):
    if event.kind == "progress":
        p = event.data
        if p.total_steps and p.current_step in (1, p.total_steps // 2, p.total_steps):
            print(f"  [{p.phase}] step {p.current_step}/{p.total_steps} ({p.progress:.0f}%)", flush=True)
res = job.result()
print(f"Finished Qwen 2.1 Turbo 4-portrait test in {time.time() - t0:.1f}s: success={res.success}, files={res.generated_files}")
if not res.success:
    for err in res.errors:
        print("ERROR:", err.message)
