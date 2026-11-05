"""
Prepare 1-frame H3 GVA inpaint task using the tight microphone mask so H3's 20B model
inpaints only the two microphone stands while preserving 100% of both characters and the Venn rings.
"""

import json
import subprocess
from pathlib import Path
import cv2
import numpy as np

PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
IMAGES = PROJECT / "images"
MASKS = PROJECT / "work" / "masks"
JOBS = PROJECT / "work" / "jobs"
FFMPEG = PROJECT / "tools" / "ffmpeg.exe"

img = cv2.imread(str(IMAGES / "Dual_Stage_FacingEachOther_ClosedMouth.jpg"))
h, w = img.shape[:2]  # 960 x 1664

# Tight mask around ONLY the two microphones and their thin stand poles/cables
mask = np.zeros((h, w), dtype=np.uint8)
cv2.rectangle(mask, (int(w * 0.415), int(h * 0.350)), (int(w * 0.605), h), 255, -1)

guide_832 = cv2.resize(img, (832, 480), interpolation=cv2.INTER_LANCZOS4)
mask_832 = cv2.resize(mask, (832, 480), interpolation=cv2.INTER_NEAREST)

guide_png = MASKS / "outro_guide_832.png"
mask_png = MASKS / "outro_mic_mask_832.png"
cv2.imwrite(str(guide_png), guide_832)
cv2.imwrite(str(mask_png), mask_832)

guide_mp4 = MASKS / "outro_guide_1f.mp4"
mask_mp4 = MASKS / "outro_mic_mask_1f.mp4"

for png_p, mp4_p in [(guide_png, guide_mp4), (mask_png, mask_mp4)]:
    subprocess.run(
        [
            str(FFMPEG), "-y", "-loop", "1", "-i", str(png_p),
            "-frames:v", "5", "-r", "25", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
            str(mp4_p),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

tasks = [
    {
        "id": 1,
        "params": {
            "model_type": "minimax_h3_fl2va_pruned_pdd",
            "prompt": (
                "summary: [still image generation] A cinematic 16:9 medium two-shot on a dark theatrical Venn soundstage where the Black male singer on the left and the chestnut-haired female singer on the right stand facing each other with open eyes and closed mouths, with empty space between them showing the glowing violet and golden Venn floor rings on the wooden stage floor and the soft-focus string orchestra in the background.\n"
                "detailed_description: Empty unobstructed space between the two singers with no microphones, showing only the dark stage haze, the glowing neon floor rings on the wooden stage floor, and the soft-focus chamber string orchestra behind them."
            ),
            "resolution": "832x480",
            "video_length": 1,
            "seed": 9901,
            "num_inference_steps": 8,
            "guidance_phases": 1,
            "flow_shift": 12.0,
            "sample_solver": "euler",
            "image_mode": 1,
            "image_prompt_type": "",
            "video_prompt_type": "GVA",
            "audio_prompt_type": "",
            "video_guide": str(guide_mp4),
            "video_mask": str(mask_mp4),
            "denoising_strength": 1.0,
            "masking_strength": 1.0,
            "custom_settings": {"h3_mask_mode": "grouped_rows"},
            "output_filename": "Outro_NoMics_H3",
        },
    }
]

job_path = JOBS / "outro_nomics_job.json"
job_path.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
print("Wrote", job_path)
