"""
Build pristine Dual_Outro_Orchestra_Rings_1.jpg and Dual_Outro_Orchestra_Rings_2.jpg
directly from Dual_Stage_FacingEachOther_ClosedMouth.jpg (zero inpainting smudges,
100% continuous with Chorus 3 on the Venn orchestra stage, mouths closed, eyes open).
"""

from pathlib import Path
import cv2
import numpy as np

PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
IMAGES = PROJECT / "images"

pristine = cv2.imread(str(IMAGES / "Dual_Stage_FacingEachOther_ClosedMouth.jpg"))
h, w = pristine.shape[:2]  # 960 x 1664

# 1. Dual_Outro_Orchestra_Rings_1.jpg: pristine medium two-shot at the end of the vocal performance
cv2.imwrite(str(IMAGES / "Dual_Outro_Orchestra_Rings_1.jpg"), pristine, [cv2.IMWRITE_JPEG_QUALITY, 96])
print("Saved pristine Dual_Outro_Orchestra_Rings_1.jpg")

# 2. Dual_Outro_Orchestra_Rings_2.jpg: pristine wider stage framing (90% scale with seamless dark stage border)
scale = 0.90
sw, sh = int(w * scale), int(h * scale)
scaled = cv2.resize(pristine, (sw, sh), interpolation=cv2.INTER_LANCZOS4)

bg_blur = cv2.GaussianBlur(pristine, (101, 101), 35.0)
canvas = (bg_blur.astype(np.float32) * 0.65).astype(np.uint8)
y0 = h - sh
x0 = (w - sw) // 2

vign = np.ones((sh, sw), dtype=np.float32)
pad_x, pad_y = 90, 80
rx = 0.5 * (1.0 - np.cos(np.pi * np.linspace(0, 1, pad_x, dtype=np.float32)))
ry = 0.5 * (1.0 - np.cos(np.pi * np.linspace(0, 1, pad_y, dtype=np.float32)))
vign[:, :pad_x] *= rx[None, :]
vign[:, -pad_x:] *= rx[::-1][None, :]
vign[:pad_y, :] *= ry[:, None]
vign3 = vign[..., None]

roi = canvas[y0 : y0 + sh, x0 : x0 + sw].astype(np.float32)
canvas[y0 : y0 + sh, x0 : x0 + sw] = (roi * (1.0 - vign3) + scaled.astype(np.float32) * vign3).astype(np.uint8)

cv2.imwrite(str(IMAGES / "Dual_Outro_Orchestra_Rings_2.jpg"), canvas, [cv2.IMWRITE_JPEG_QUALITY, 96])
print("Saved pristine Dual_Outro_Orchestra_Rings_2.jpg")
