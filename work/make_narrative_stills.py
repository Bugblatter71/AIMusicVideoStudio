import json
from pathlib import Path

PROJECT = Path(r".")
IMAGES = PROJECT / "images"
JOBS = PROJECT / "work" / "jobs"

tasks = [
    # 1. Still image: NightOut_Female_Narrative (turned toward her friends, laughing/smiling with mouth closed, NOT looking at camera)
    {
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "prompt": (
            "For the target image, <Picture 1> is referenced.\n"
            "Modify <Picture 1> while preserving the woman's exact facial identity, wavy shoulder-length auburn-brown hair, "
            "black suede biker jacket, silver necklace, and the upscale late-night lounge with the two interlocking illuminated "
            "circular brass Venn ring chandeliers overhead and warm golden-amber circular spotlight pool: turn the woman in "
            "three-quarter profile toward her two female friends on the right so she is completely in the moment laughing and "
            "smiling softly with her mouth closed, engaged with her friends and NOT looking at the camera."
        ),
        "resolution": "832x480",
        "video_length": 1,
        "seed": 5001,
        "num_inference_steps": 8,
        "guidance_phases": 1,
        "flow_shift": 12.0,
        "sample_solver": "euler",
        "image_mode": 1,
        "image_prompt_type": "S",
        "image_start": str(IMAGES / "NightOut_Female_Anchor.jpg"),
        "video_prompt_type": "",
        "audio_prompt_type": "",
        "output_filename": "NightOut_Female_Narrative_H3",
    },
    # 2. Still image: Dual_Anchor_NightOut_Narrative (he looks right at her singing in 3/4 profile; she is turned toward her friends smiling with mouth closed)
    {
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "prompt": (
            "For the target image, <Picture 1> is referenced.\n"
            "Modify <Picture 1> while preserving both lead characters' exact facial identities, hairstyles, jackets, and the "
            "upscale lounge with interlocking circular brass Venn chandeliers and sapphire-blue left / golden-amber right "
            "spotlight pools: on the left, the Black man amongst his friends turns his head in three-quarter profile looking "
            "across the room to the right toward the woman with longing while singing softly (not looking at the camera); "
            "on the right, the auburn-haired woman is turned toward her female friends smiling warmly with her mouth closed, "
            "completely in the moment with her friends and not looking at the camera."
        ),
        "resolution": "832x480",
        "video_length": 1,
        "seed": 5002,
        "num_inference_steps": 8,
        "guidance_phases": 1,
        "flow_shift": 12.0,
        "sample_solver": "euler",
        "image_mode": 1,
        "image_prompt_type": "S",
        "image_start": str(IMAGES / "Dual_Anchor_NightOut.jpg"),
        "video_prompt_type": "",
        "audio_prompt_type": "",
        "output_filename": "Dual_Anchor_NightOut_Narrative_H3",
    },
]

out = JOBS / "make_narrative_stills.json"
out.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
print(f"Wrote {out}")

