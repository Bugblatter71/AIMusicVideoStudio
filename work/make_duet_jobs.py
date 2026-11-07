import json
from pathlib import Path

PROJECT = Path(r".")
IMAGES = PROJECT / "images"
AUDIO = PROJECT / "work" / "audio"
JOBS = PROJECT / "work" / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)


def make_task(*, output_filename: str, prompt: str, image_start: Path, audio_path: Path, video_length: int, force_fps: str, seed: int) -> dict:
    return {
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "prompt": prompt,
        "resolution": "832x480",
        "video_length": video_length,
        "seed": seed,
        "force_fps": force_fps,
        "num_inference_steps": 8,
        "guidance_phases": 1,
        "flow_shift": 12.0,
        "sample_solver": "euler",
        "repeat_generation": 1,
        "multi_prompts_gen_type": "PW",
        "image_mode": 0,
        "image_prompt_type": "S",
        "image_start": str(image_start),
        "video_prompt_type": "",
        "audio_prompt_type": "A",
        "audio_guide": str(audio_path),
        "sliding_window_size": 362,
        "sliding_window_overlap": 18,
        "sliding_window_discard_last_frames": 0,
        "sliding_window_trim_first_frames": 0,
        "custom_settings": {"h3_mask_mode": "grouped_rows"},
        "output_filename": output_filename,
    }


tasks = [
    # 1. Close-up Male driven ONLY by male_vocal stem (104.4s - 110.72s)
    make_task(
        output_filename="Duet_Male_Close",
        image_start=IMAGES / "Male_Anchor_2.jpg",
        audio_path=AUDIO / "Duet_104.4_male.wav",
        video_length=158,  # 158 / 25 = 6.32s
        force_fps="25",
        seed=3001,
        prompt=(
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. The Black male singer at a vintage brass condenser microphone in a dark moody studio sings a soaring emotional chorus line with full vocal power, speaking and singing with natural lip movements synchronized. Slow cinematic push-in with subtle orbital drift and gentle parallax between the foreground singer and the teal-and-amber studio bokeh background.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    ),
    # 2. Close-up Female driven ONLY by female_vocal stem (104.4s - 110.72s)
    make_task(
        output_filename="Duet_Female_Close",
        image_start=IMAGES / "Female_Anchor_2.jpg",
        audio_path=AUDIO / "Duet_104.4_female.wav",
        video_length=158,  # 158 / 25 = 6.32s
        force_fps="25",
        seed=3002,
        prompt=(
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. The female singer at a vintage brass condenser microphone in a dark moody studio sings an emotional, tender vocal phrase, speaking and singing with natural lip movements synchronized. Slow cinematic push-in with subtle orbital drift and gentle parallax between the foreground singer and the teal-and-amber studio bokeh background.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    ),
    # 3. Single-pass Two-Shot driven by combined vocal stems (104.4s - 110.72s)
    make_task(
        output_filename="Duet_SinglePass_Both",
        image_start=IMAGES / "Dual_Anchor_Duet.jpg",
        audio_path=AUDIO / "Duet_104.4_both_vocals.wav",
        video_length=158,  # 158 / 25 = 6.32s
        force_fps="25",
        seed=3003,
        prompt=(
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio lit by sapphire-blue light on the left and warm golden-amber light on the right with a violet overlap in the middle, the Black male singer on the left and the female singer on the right sing an overlapping emotional duet into their brass condenser microphones, both singing with natural lip movements synchronized. Slow cinematic push-in with gentle parallax between the two foreground singers and the studio background.\n"
            "overall_soundscape: Quiet studio room tone under the vocal duet.\n"
            "non_diegetic_music: N/A"
        ),
    ),
]

out = JOBS / "duet_lipsync_trial.json"
out.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
print(f"Wrote {len(tasks)} tasks to {out}")

