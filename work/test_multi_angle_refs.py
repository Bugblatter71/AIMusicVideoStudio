"""
Create multi-angle reference crops and 3-angle turnaround sheets for both characters
(using the verified front/3/4 and profile angles that match our characters 100%)
and test generating a new scene with:
1. Separate multi-angle reference images in `image_refs` (5 refs total)
2. 3-angle turnaround composite sheets in `image_refs` (3 refs total)
"""

import os
import sys
import time
from pathlib import Path
import cv2
import numpy as np

PINOKIO_BIN = r"C:\Users\bugbl\Downloads\pinokio\bin"
MINICONDA_LIB = r"C:\Users\bugbl\Downloads\pinokio\bin\miniconda\Library\bin"
FFMPEG_BIN = r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\ffmpeg_bins"
os.environ["PATH"] = f"{PINOKIO_BIN};{MINICONDA_LIB};{FFMPEG_BIN};" + os.environ.get("PATH", "")
os.environ["GIT_PYTHON_REFRESH"] = "quiet"

WGP_ROOT = Path(r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app")
PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
IMAGES = PROJECT / "images"
LOCAL_OUT = PROJECT / "work" / "local_images"
REF_DIR = PROJECT / "work" / "ref_sheets"
LOCAL_OUT.mkdir(parents=True, exist_ok=True)
REF_DIR.mkdir(parents=True, exist_ok=True)


def crop_box(img: np.ndarray, x0_pct: float, y0_pct: float, x1_pct: float, y1_pct: float, out_size=(512, 640)) -> np.ndarray:
    h, w = img.shape[:2]
    crop = img[int(h * y0_pct) : int(h * y1_pct), int(w * x0_pct) : int(w * x1_pct)]
    return cv2.resize(crop, out_size, interpolation=cv2.INTER_LANCZOS4)


# Load verified character anchors
m_front = cv2.imread(str(IMAGES / "Male_Anchor_2.jpg"))
m_stage = cv2.imread(str(IMAGES / "Male_VennStage_Mic.jpg"))
dual_verified = cv2.imread(str(LOCAL_OUT / "TestA_KI_EditScene_NoMics.jpg"))

f_front = cv2.imread(str(IMAGES / "Female_Anchor_2.jpg"))
f_left34 = cv2.imread(str(IMAGES / "Female_Stage_ProfileMic_ClosedMouth.jpg"))

# Male 3 angles:
# 1. Front/right 3/4 (Male_Anchor_2.jpg)
m_a1 = crop_box(m_front, 0.28, 0.02, 0.65, 0.85)
# 2. Front/left 3/4 (Male_VennStage_Mic.jpg)
m_a2 = crop_box(m_stage, 0.31, 0.05, 0.66, 0.88)
# 3. Side profile facing right (TestA_KI_EditScene_NoMics.jpg left singer)
m_a3 = crop_box(dual_verified, 0.16, 0.05, 0.44, 0.88)

# Female 3 angles:
# 1. Front/right 3/4 (Female_Anchor_2.jpg)
f_a1 = crop_box(f_front, 0.28, 0.02, 0.67, 0.88)
# 2. Left 3/4 (Female_Stage_ProfileMic_ClosedMouth.jpg)
f_a2 = crop_box(f_left34, 0.36, 0.02, 0.82, 0.92)
# 3. Side profile facing left in black suede jacket (TestA_KI_EditScene_NoMics.jpg right singer)
f_a3 = crop_box(dual_verified, 0.58, 0.14, 0.84, 0.92)

# Save individual angle crops
paths = {
    "m_a1": REF_DIR / "male_angle1_front34.jpg",
    "m_a2": REF_DIR / "male_angle2_stage34.jpg",
    "m_a3": REF_DIR / "male_angle3_profile.jpg",
    "f_a1": REF_DIR / "female_angle1_front34.jpg",
    "f_a2": REF_DIR / "female_angle2_left34.jpg",
    "f_a3": REF_DIR / "female_angle3_profile.jpg",
}
for k, arr in [("m_a1", m_a1), ("m_a2", m_a2), ("m_a3", m_a3), ("f_a1", f_a1), ("f_a2", f_a2), ("f_a3", f_a3)]:
    cv2.imwrite(str(paths[k]), arr, [cv2.IMWRITE_JPEG_QUALITY, 96])

# Also save 3-panel turnaround reference sheets (1536x640) for each character
m_sheet = np.hstack([m_a1, m_a2, m_a3])
f_sheet = np.hstack([f_a1, f_a2, f_a3])
m_sheet_path = REF_DIR / "male_turnaround_3angles.jpg"
f_sheet_path = REF_DIR / "female_turnaround_3angles.jpg"
cv2.imwrite(str(m_sheet_path), m_sheet, [cv2.IMWRITE_JPEG_QUALITY, 96])
cv2.imwrite(str(f_sheet_path), f_sheet, [cv2.IMWRITE_JPEG_QUALITY, 96])
print("Saved individual angle crops and 3-angle turnaround sheets.")

sys.path.insert(0, str(WGP_ROOT))
os.chdir(WGP_ROOT)

from shared.api import init

session = init(
    root=WGP_ROOT,
    config_path=WGP_ROOT / "wgp_config.json",
    output_dir=LOCAL_OUT,
    console_output=False,
)


def make_task(prompt: str, out_name: str, refs: list[str], ref_mode: str, seed: int = 7710) -> dict:
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
    # Test 1: Separate multi-angle reference images for each character (2 angles of him + 2 angles of her + stage geometry)
    make_task(
        prompt=(
            "On the dark theatrical soundstage with circular neon blue and golden Venn floor rings and seated string orchestra from image 1, "
            "place the Black male singer shown in image 2 and image 3 on the left side standing in three-quarter profile facing right (eyes open, mouth closed), "
            "and place the woman shown in image 4 and image 5 (keeping her exact facial features, nose, lips, wavy dark auburn hair with curtain bangs, and black suede biker jacket) "
            "on the right side standing in three-quarter profile facing left looking into his eyes (eyes open, mouth closed), with no microphones between them."
        ),
        out_name="Test_MultiAngle_SeparateRefs",
        refs=[
            str(IMAGES / "Orchestra_Venn_WideStage.jpg"),
            str(paths["m_a1"]),
            str(paths["m_a3"]),
            str(paths["f_a1"]),
            str(paths["f_a3"]),
        ],
        ref_mode="KI",
        seed=7710,
    ),
    # Test 2: 3-Angle Turnaround Reference Sheets (image 1 = stage, image 2 = 3-angle sheet of male, image 3 = 3-angle sheet of female)
    make_task(
        prompt=(
            "A single unified 16:9 medium-wide cinematic shot on the dark theatrical soundstage with circular neon blue and golden Venn floor rings and seated string orchestra from image 1. "
            "On the left side stands the Black male singer from the character reference sheet in image 2, facing right in three-quarter profile with open eyes and closed mouth. "
            "On the right side stands the woman from the character reference sheet in image 3, facing left in three-quarter profile with her exact face, wavy dark auburn hair with curtain bangs, black suede biker jacket, open eyes, and closed mouth, looking into his eyes."
        ),
        out_name="Test_MultiAngle_TurnaroundSheets",
        refs=[
            str(IMAGES / "Orchestra_Venn_WideStage.jpg"),
            str(m_sheet_path),
            str(f_sheet_path),
        ],
        ref_mode="KI",
        seed=7711,
    ),
]

t0 = time.time()
job = session.submit_manifest(tasks)
res = job.result()
print(f"Finished 2 multi-angle tests in {time.time() - t0:.1f}s: success={res.success}, files={res.generated_files}")

