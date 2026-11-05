import json
from pathlib import Path

PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
IMAGES = PROJECT / "images"
AUDIO = PROJECT / "work" / "audio"
RENDERS = PROJECT / "work" / "renders"
MASKS = PROJECT / "work" / "masks"
JOBS = PROJECT / "work" / "jobs"

# Method B Pass 2: Masked Control Video (GVA) — keep Left half (Male from Pass 1) fixed, regenerate Right half (Female) with female_vocal stem
task_pass2_female = {
    "model_type": "minimax_h3_fl2va_pruned_pdd",
    "prompt": (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
        "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio, "
        "the female singer on the right sings a tender, emotional vocal phrase into her brass condenser microphone, "
        "speaking and singing with natural lip movements synchronized, then closes her mouth calmly when her phrase ends. "
        "Slow cinematic push-in matching the left side.\n"
        "overall_soundscape: Quiet studio room tone under the female vocal performance.\n"
        "non_diegetic_music: N/A"
    ),
    "resolution": "832x480",
    "video_length": 158,
    "seed": 4002,
    "force_fps": "25",
    "num_inference_steps": 8,
    "guidance_phases": 1,
    "flow_shift": 12.0,
    "sample_solver": "euler",
    "repeat_generation": 1,
    "multi_prompts_gen_type": "PW",
    "image_mode": 0,
    "image_prompt_type": "S",
    "image_start": str(IMAGES / "Dual_Anchor_Duet.jpg"),
    "video_prompt_type": "GVA",
    "video_guide": str(RENDERS / "Duet_MethodB_Pass1_MaleOnly.mp4"),
    "video_mask": str(MASKS / "mask_right_half_832x480.mp4"),
    "denoising_strength": 1.0,
    "masking_strength": 1.0,
    "audio_prompt_type": "A",
    "audio_guide": str(AUDIO / "Duet_104.4_female.wav"),
    "sliding_window_size": 362,
    "sliding_window_overlap": 18,
    "sliding_window_discard_last_frames": 0,
    "sliding_window_trim_first_frames": 0,
    "custom_settings": {"h3_mask_mode": "grouped_rows"},
    "output_filename": "Duet_MethodB_Pass2_BothIndependent",
}

out_b2 = JOBS / "duet_independent_control_part2.json"
out_b2.write_text(json.dumps([task_pass2_female], indent=2), encoding="utf-8")
print(f"Wrote {out_b2}")

