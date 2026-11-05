import json
from pathlib import Path

PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
AUDIO = PROJECT / "work" / "audio"
JOBS = PROJECT / "work" / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)


def make_task(*, output_filename: str, prompt: str, audio_path: Path, video_length: int, force_fps: str, seed: int) -> dict:
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
        "image_prompt_type": "",
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
    # Test 1A: 25 fps + Venn spotlight aesthetic (single male singer in overlap pool)
    make_task(
        output_filename="T1_25fps_venn_spotlights",
        video_length=141,  # 141 / 25 = 5.64s
        force_fps="25",
        seed=1001,
        audio_path=AUDIO / "T1_male_40.00_25fps.wav",
        prompt=(
            "integrated_multimodal_description: [Shot 1] Wide-to-medium high-angle cinematic shot inside a dark soundstage "
            "where two large circular overhead spotlights project onto the dark wooden floor like a glowing Venn diagram: "
            "a cool sapphire-blue circle on the left and a warm golden-amber circle on the right, intersecting in a bright "
            "violet-white overlap zone at center. The male singer in a dark tailored coat stands inside the central overlap "
            "zone singing with rising emotional intensity, with natural lip movements synchronized. Slow cinematic push-in "
            "with subtle orbital drift and gentle parallax between the foreground singer and the glowing floor circles.\n"
            "overall_soundscape: Quiet soundstage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    ),
    # Test 1B: 24 fps control (same prompt & seed, native 24 fps) to compare lip-sync & motion smoothness
    make_task(
        output_filename="T1b_24fps_venn_spotlights",
        video_length=141,  # 141 / 24 = 5.875s
        force_fps="",
        seed=1001,
        audio_path=AUDIO / "T1b_male_40.00_24fps.wav",
        prompt=(
            "integrated_multimodal_description: [Shot 1] Wide-to-medium high-angle cinematic shot inside a dark soundstage "
            "where two large circular overhead spotlights project onto the dark wooden floor like a glowing Venn diagram: "
            "a cool sapphire-blue circle on the left and a warm golden-amber circle on the right, intersecting in a bright "
            "violet-white overlap zone at center. The male singer in a dark tailored coat stands inside the central overlap "
            "zone singing with rising emotional intensity, with natural lip movements synchronized. Slow cinematic push-in "
            "with subtle orbital drift and gentle parallax between the foreground singer and the glowing floor circles.\n"
            "overall_soundscape: Quiet soundstage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    ),
    # Test 2: Silent co-presence in a single H3 shot (male singing in blue circle, female silent in amber circle)
    make_task(
        output_filename="T2_copresence_silent_female",
        video_length=141,
        force_fps="25",
        seed=2002,
        audio_path=AUDIO / "T2_male_45.60_25fps.wav",
        prompt=(
            "integrated_multimodal_description: [Shot 1] Wide cinematic shot of a dark stage lit by two separate circular "
            "pools of light on the floor: a cool blue circle on the left and a warm amber circle on the right. In the left "
            "blue circle, the male singer stands facing the camera and sings passionately with natural lip movements "
            "synchronized. In the right amber circle in the background, a woman with her mouth closed sits quietly at a "
            "small table sketching in a notebook, absorbed in her own world and not speaking. Slow cinematic dolly drift "
            "across the stage with gentle parallax between the two pools of light.\n"
            "overall_soundscape: Quiet stage ambience and faint pencil rustle under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    ),
]

out = JOBS / "pilot_tests.json"
out.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
print(f"Wrote {len(tasks)} tasks to {out}")

