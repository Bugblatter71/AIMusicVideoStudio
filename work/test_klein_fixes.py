"""
Test two improvements with Flux 2 Klein 4B (already cached, ~12s/image):
1. Test A (KI Scene-Edit mode): Take Dual_Stage_FacingEachOther_ClosedMouth.jpg as image 1 and Female_Anchor_2.jpg as image 2;
   remove the two microphones and match her black suede biker jacket while preserving her exact face, hair, and circular Venn rings.
2. Test B (Tight Portrait Reference Crops + Accurate Hair/Wardrobe Prompt): Crop the portraits of Male_Anchor_2.jpg and Female_Anchor_2.jpg
   so reference tokens focus on their faces/hair ( instead of 88% studio background) and generate a new scene from scratch.
"""

import os
import sys
import time
from pathlib import Path
import cv2

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
IMAGES = PROJECT / "images"
LOCAL_OUT = PROJECT / "work" / "local_images"
LOCAL_OUT.mkdir(parents=True, exist_ok=True)

# 1. Create tight portrait crops of Male_Anchor_2.jpg and Female_Anchor_2.jpg so 75%+ of the image is their face, hair, and jacket
m_img = cv2.imread(str(IMAGES / "Male_Anchor_2.jpg"))
f_img = cv2.imread(str(IMAGES / "Female_Anchor_2.jpg"))
mh, mw = m_img.shape[:2]
fh, fw = f_img.shape[:2]

# In Female_Anchor_2.jpg (1664x960), her head & shoulders are in x = 26%..68%, y = 2%..95%
m_crop = m_img[int(mh * 0.02) : int(mh * 0.95), int(mw * 0.25) : int(mw * 0.70)]
f_crop = f_img[int(fh * 0.02) : int(fh * 0.95), int(fw * 0.26) : int(fw * 0.68)]

m_crop_path = LOCAL_OUT / "ref_male_portrait.jpg"
f_crop_path = LOCAL_OUT / "ref_female_portrait.jpg"
cv2.imwrite(str(m_crop_path), m_crop, [cv2.IMWRITE_JPEG_QUALITY, 96])
cv2.imwrite(str(f_crop_path), f_crop, [cv2.IMWRITE_JPEG_QUALITY, 96])

sys.path.insert(0, str(WGP_ROOT))
os.chdir(WGP_ROOT)

from shared.api import init

session = init(
    root=WGP_ROOT,
    config_path=WGP_ROOT / "wgp_config.json",
    output_dir=LOCAL_OUT,
    console_output=False,
)


def make_task(prompt: str, out_name: str, refs: list[str], ref_mode: str, seed: int = 7702) -> dict:
    cfg = session.get_default_settings("flux2_klein_4b")
    cfg["model_type"] = "flux2_klein_4b"
    cfg["image_mode"] = 1
    cfg["prompt"] = prompt
    cfg["resolution"] = "1664x960"
    cfg["seed"] = seed
    cfg["output_filename"] = out_name
    cfg["remove_background_images_ref"] = 0
    cfg["video_prompt_type"] = ref_mode
    cfg["image_refs"] = [str(r) for r in refs]
    return cfg


tasks = [
    # Test A: KI mode (Scene + Reference) -> edit Dual_Stage_FacingEachOther_ClosedMouth.jpg to remove mics & wear her black suede jacket
    make_task(
        prompt=(
            "Edit image 1: completely remove the two brass microphones and microphone stands in the center gap between the man and the woman, "
            "and change the woman's white sweater to the black suede biker jacket over a dark V-neck top from image 2. "
            "Keep the man's exact face, profile, and jacket on the left, keep the woman's exact face, profile, and wavy dark auburn hair with curtain bangs on the right, "
            "and keep the glowing circular neon blue and golden Venn rings on the wooden stage floor and the string orchestra in the background exactly as in image 1."
        ),
        out_name="TestA_KI_EditScene_NoMics",
        refs=[str(IMAGES / "Dual_Stage_FacingEachOther_ClosedMouth.jpg"), str(f_crop_path)],
        ref_mode="KI",
        seed=7702,
    ),
    # Test B: New scene generation using tight portrait crops + Orchestra_Venn_WideStage.jpg as scene geometry reference (KI mode: image 1 = stage, image 2 = male portrait, image 3 = female portrait)
    make_task(
        prompt=(
            "Using the theatrical soundstage with the circular neon blue and golden Venn floor rings and seated string orchestra from image 1 as the background setting, "
            "place the Black man from image 2 on the left side standing in three-quarter profile facing right with open eyes and closed mouth, "
            "and place the woman from image 3 (with her exact face, wavy dark auburn shoulder-length hair with curtain bangs, hazel eyes open, mouth closed, wearing her black suede biker jacket over a dark V-neck top and thin silver necklace) "
            "on the right side standing in three-quarter profile facing left looking into his eyes, with no microphones between them."
        ),
        out_name="TestB_KI_3Refs_StagePlusPortraits",
        refs=[str(IMAGES / "Orchestra_Venn_WideStage.jpg"), str(m_crop_path), str(f_crop_path)],
        ref_mode="KI",
        seed=7703,
    ),
]

t0 = time.time()
job = session.submit_manifest(tasks)
res = job.result()
print(f"Finished 2 tests in {time.time() - t0:.1f}s: success={res.success}, files={res.generated_files}")

