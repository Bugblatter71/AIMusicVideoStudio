"""
Prepare and crop all v2 anchor images to exact 1664x960 (2x of 832x480)
and create the seamless unified NightOut anchors and microphone-free Outro anchor mask.
"""

import shutil
from pathlib import Path
import cv2
import numpy as np

BRAIN = Path(r"C:\Users\bugbl\.gemini\antigravity\brain\11e6839d-e38f-40d5-8ee5-0f5c88dbb4a5")
PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
IMAGES = PROJECT / "images"
MASKS = PROJECT / "work" / "masks"
IMAGES.mkdir(parents=True, exist_ok=True)
MASKS.mkdir(parents=True, exist_ok=True)

TARGET_W, TARGET_H = 1664, 960  # Exact 1.7333 aspect ratio matching 832x480


def crop_to_target(img: np.ndarray, target_w: int = TARGET_W, target_h: int = TARGET_H) -> np.ndarray:
    """Center-crop (or aspect-fit crop) to 1.7333 and resize with Lanczos to 1664x960."""
    h, w = img.shape[:2]
    target_aspect = target_w / target_h
    cur_aspect = w / h
    if cur_aspect > target_aspect:
        # Slightly wider (e.g., 16:9 = 1.7778 -> 1.7333): trim ~1.2% off left and right edges
        new_w = int(round(h * target_aspect))
        x0 = (w - new_w) // 2
        cropped = img[:, x0 : x0 + new_w]
    else:
        new_h = int(round(w / target_aspect))
        y0 = (h - new_h) // 2
        cropped = img[y0 : y0 + new_h, :]
    return cv2.resize(cropped, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)


# 1. Map new artifacts to canonical v2 filenames in images/
MAPPING = {
    "Male_VennStage_Mic.jpg": BRAIN / "male_vennstage_mic_1793492558203.jpg",
    "Dual_Duet_Studio_FacingEachOther.jpg": BRAIN / "dual_duet_studio_facingeachother_1793492579458.jpg",
    "Dual_Duet_Stage_FacingEachOther.jpg": BRAIN / "dual_duet_stage_facingeachother_1793492600314.jpg",
    "Dual_Duet_Stage_CloseFacing.jpg": BRAIN / "dual_duet_stage_closefacing_1793492624519.jpg",
    "Studio_Rehearsal_MultiCircle_1.jpg": BRAIN / "studio_rehearsal_multicircle_1_1793492572912.jpg",
    "Studio_Rehearsal_Band_GVA.jpg": BRAIN / "studio_rehearsal_band_gva_1793492591625.jpg",
    "Orchestra_Venn_HerCircles.jpg": BRAIN / "orchestra_venn_hercircles_fixed_1793492696078.jpg",
    "Orchestra_Venn_WideStage.jpg": BRAIN / "orchestra_venn_widestage_1793492630293.jpg",
    "Dual_Stage_FacingEachOther_ClosedMouth.jpg": BRAIN / "dual_stage_facingeachother_closedmouth_1793492582845.jpg",
    "Female_Stage_ProfileMic_ClosedMouth.jpg": BRAIN / "female_stage_profilemic_closedmouth_1793492599616.jpg",
}

for dest_name, src_path in MAPPING.items():
    img = cv2.imread(str(src_path))
    if img is None:
        raise FileNotFoundError(f"Could not read {src_path}")
    out_img = crop_to_target(img)
    cv2.imwrite(str(IMAGES / dest_name), out_img, [cv2.IMWRITE_JPEG_QUALITY, 96])
    print(f"Saved {dest_name} ({out_img.shape[1]}x{out_img.shape[0]})")

# Also ensure existing studio solo anchors have 1664x960 versions so no aspect jump occurs
for existing_name in ["Male_Anchor_1.jpg", "Male_Anchor_2.jpg", "Female_Anchor_2.jpg"]:
    img = cv2.imread(str(IMAGES / existing_name))
    if img is not None:
        out_img = crop_to_target(img)
        cv2.imwrite(str(IMAGES / existing_name), out_img, [cv2.IMWRITE_JPEG_QUALITY, 96])
        print(f"Standardized {existing_name} to {out_img.shape[1]}x{out_img.shape[0]}")

# 2. Build the seamless Unified Nightclub anchors
# Left half of nightout_unified_maleangle (he looks right toward her, his friends talk to each other)
# + Right half of nightout_unified_bothinroom (she in her cream knit top laughs & clinks glasses with her friends)
img_m_angle = cv2.imread(str(BRAIN / "nightout_unified_maleangle_1793492616914.jpg"))
img_both = cv2.imread(str(BRAIN / "nightout_unified_bothinroom_1793492640276.jpg"))

h, w = img_m_angle.shape[:2]
# Smooth cosine feather across x = 50%..56% (between his right shoulder and her back)
alpha = np.zeros((h, w, 1), dtype=np.float32)
x1, x2 = int(w * 0.50), int(w * 0.56)
alpha[:, x2:, 0] = 1.0
ramp = 0.5 * (1.0 - np.cos(np.pi * np.linspace(0, 1, x2 - x1, dtype=np.float32)))
alpha[:, x1:x2, 0] = ramp[None, :]

unified_master_full = (img_m_angle.astype(np.float32) * (1.0 - alpha) + img_both.astype(np.float32) * alpha).astype(np.uint8)
unified_master = crop_to_target(unified_master_full)
cv2.imwrite(str(IMAGES / "NightOut_Unified_BothInRoom.jpg"), unified_master, [cv2.IMWRITE_JPEG_QUALITY, 96])
print("Saved NightOut_Unified_BothInRoom.jpg (seamless combined master)")

# Also create NightOut_Unified_MaleAngle.jpg where she is still in her cream knit top in the background on the right
# (with soft depth-of-field blur on the right background so S17 focuses on him & his friends in the foreground while she's clearly visible in the same room)
blurred_right = cv2.GaussianBlur(unified_master_full, (15, 15), 4.0)
dof_alpha = np.zeros((h, w, 1), dtype=np.float32)
bx1, bx2 = int(w * 0.53), int(w * 0.62)
dof_alpha[:, bx2:, 0] = 0.65
dof_ramp = 0.65 * 0.5 * (1.0 - np.cos(np.pi * np.linspace(0, 1, bx2 - bx1, dtype=np.float32)))
dof_alpha[:, bx1:bx2, 0] = dof_ramp[None, :]
male_focus_full = (unified_master_full.astype(np.float32) * (1.0 - dof_alpha) + blurred_right.astype(np.float32) * dof_alpha).astype(np.uint8)
male_focus = crop_to_target(male_focus_full)
cv2.imwrite(str(IMAGES / "NightOut_Unified_MaleAngle.jpg"), male_focus, [cv2.IMWRITE_JPEG_QUALITY, 96])
print("Saved NightOut_Unified_MaleAngle.jpg")

# And create NightOut_Unified_FemaleFocus.jpg: a medium-wide framing of the exact same room shifted slightly right/zoomed 18%
# so she and her friends are prominent on the right while he is still visible on the left edge looking right at her!
zoom_w = int(w * 0.84)
zoom_h = int(h * 0.84)
zx0 = w - zoom_w  # anchored to right side so she and her friends + his profile on the left are both in frame
zy0 = int(h * 0.10)
female_focus_crop = unified_master_full[zy0 : zy0 + zoom_h, zx0 : zx0 + zoom_w]
female_focus = crop_to_target(female_focus_crop)
cv2.imwrite(str(IMAGES / "NightOut_Unified_FemaleFocus.jpg"), female_focus, [cv2.IMWRITE_JPEG_QUALITY, 96])
print("Saved NightOut_Unified_FemaleFocus.jpg")

