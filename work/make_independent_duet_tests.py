import json
import subprocess
from pathlib import Path

PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
FFMPEG = PROJECT / "tools" / "ffmpeg.exe"
IMAGES = PROJECT / "images"
AUDIO = PROJECT / "work" / "audio"
JOBS = PROJECT / "work" / "jobs"
MASKS = PROJECT / "work" / "masks"
MASKS.mkdir(parents=True, exist_ok=True)

# Generate a right-half white / left-half black mask video (832x480, 25fps, 6.32s)
# White (255) = editable area (right half: Female), Black (0) = fixed area (left half: Male)
right_mask = MASKS / "mask_right_half_832x480.mp4"
subprocess.run(
    [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=416x480:r=25:d=6.32",
        "-f", "lavfi", "-i", "color=c=white:s=416x480:r=25:d=6.32",
        "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", "10",
        str(right_mask),
    ],
    check=True,
)
print(f"Created mask {right_mask}")


def base_task(*, output_filename: str, prompt: str, image_start: Path, audio_path: Path, seed: int) -> dict:
    return {
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "prompt": prompt,
        "resolution": "832x480",
        "video_length": 158,  # 6.32s @ 25fps
        "seed": seed,
        "force_fps": "25",
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


# Method A: Single-Pass Hard-Panned L/R Stereo + Speaker IDs (S1)/(S2)
task_stereo = base_task(
    output_filename="Duet_MethodA_HardStereo_S1S2",
    image_start=IMAGES / "Dual_Anchor_Duet.jpg",
    audio_path=AUDIO / "Duet_104.4_hard_panned_LR.wav",
    seed=3003,
    prompt=(
        "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
        "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio "
        "lit by sapphire-blue light on the left and warm golden-amber light on the right, two singers perform distinct "
        "independent vocal parts into their brass condenser microphones: on the left, the Black male singer (S1) sings "
        "continuously with strong open-mouth vocal phrasing and natural lip movements synchronized to the left channel; "
        "on the right, the female singer (S2) sings a short tender phrase at the start and then closes her mouth and "
        "listens calmly during her pauses, with natural lip movements synchronized to the right channel. Slow cinematic "
        "push-in with gentle parallax between the two foreground singers and the studio background.\n"
        "overall_soundscape: Quiet studio room tone under the stereo vocal duet.\n"
        "non_diegetic_music: N/A"
    ),
)

# Method B Pass 1: Two-shot driven ONLY by Male stem (Prompt explicitly tells Female to stay silent/listening in Pass 1)
task_pass1_male = base_task(
    output_filename="Duet_MethodB_Pass1_MaleOnly",
    image_start=IMAGES / "Dual_Anchor_Duet.jpg",
    audio_path=AUDIO / "Duet_104.4_male.wav",
    seed=4001,
    prompt=(
        "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
        "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio, "
        "the Black male singer on the left sings a soaring emotional chorus line into his brass condenser microphone, "
        "speaking and singing with natural lip movements synchronized, while the woman on the right stands quietly with "
        "her mouth closed listening. Slow cinematic push-in with gentle parallax.\n"
        "overall_soundscape: Quiet studio room tone under the male vocal performance.\n"
        "non_diegetic_music: N/A"
    ),
)

out_ab = JOBS / "duet_independent_control_part1.json"
out_ab.write_text(json.dumps([task_stereo, task_pass1_male], indent=2), encoding="utf-8")
print(f"Wrote 2 tasks to {out_ab}")

