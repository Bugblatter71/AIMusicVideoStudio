"""
Local Image Generator using Pinokio Wan2GP (`flux2_klein_4b`, `flux2_klein_9b`, or `qwen_image_21_turbo_7B`).
Supports text-to-image, multi-character reference generation (`image_refs`), and masked editing (`image_guide` + `image_mask`)
with zero cloud quota limits.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

PINOKIO_BIN = r"C:\Users\bugbl\Downloads\pinokio\bin"
MINICONDA_LIB = r"C:\Users\bugbl\Downloads\pinokio\bin\miniconda\Library\bin"
FFMPEG_BIN = r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\ffmpeg_bins"
os.environ["PATH"] = f"{PINOKIO_BIN};{MINICONDA_LIB};{FFMPEG_BIN};" + os.environ.get("PATH", "")
os.environ["GIT_PYTHON_REFRESH"] = "quiet"

WGP_ROOT = Path(r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app")
PROJECT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
IMAGES = PROJECT / "images"
LOCAL_OUT = PROJECT / "work" / "local_images"
LOCAL_OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(WGP_ROOT))
os.chdir(WGP_ROOT)

from shared.api import init


def build_image_task(
    session,
    model_type: str,
    prompt: str,
    output_filename: str,
    resolution: str = "1664x960",
    seed: int = 42,
    image_refs: list[str] | None = None,
    ref_mode: str = "I",  # "I" = People/Objects, "KI" = First image is main scene/landscape followed by people/objects
) -> dict:
    cfg = session.get_default_settings(model_type)
    cfg["model_type"] = model_type
    cfg["image_mode"] = 1
    cfg["prompt"] = prompt
    cfg["resolution"] = resolution
    cfg["seed"] = seed
    cfg["output_filename"] = output_filename
    cfg["remove_background_images_ref"] = 0

    if image_refs:
        cfg["video_prompt_type"] = ref_mode
        cfg["image_refs"] = [str(p) for p in image_refs]
    else:
        cfg["video_prompt_type"] = ""
        cfg["image_refs"] = None

    return cfg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="flux2_klein_4b", choices=["flux2_klein_4b", "flux2_klein_9b", "qwen_image_21_turbo_7B"])
    args = parser.parse_args()

    t0 = time.time()
    session = init(
        root=WGP_ROOT,
        config_path=WGP_ROOT / "wgp_config.json",
        output_dir=LOCAL_OUT,
        console_output=True,
        console_isatty=False,
    )
    print(f"[Init] Session ready in {time.time() - t0:.1f}s. Running local multi-reference test with {args.model}...")

    male_ref = IMAGES / "Male_Anchor_2.jpg"
    female_ref = IMAGES / "Female_Anchor_2.jpg"

    if args.model.startswith("qwen"):
        prompt = (
            "A cinematic 16:9 medium-wide shot on a dark theatrical Venn soundstage. "
            "On the left side inside a single glowing sapphire-blue circle of light on the wooden floor stands the Black male singer from <image1> "
            "(wearing his dark slate-navy suede jacket over a burgundy shirt, eyes open, mouth completely closed, facing right toward the woman). "
            "On the right side inside multiple overlapping warm golden-amber and rose circles of light on the wooden floor stands the chestnut-haired woman from <image2> "
            "(wearing her cream ribbed knit top, eyes open, mouth completely closed, facing left toward the man). "
            "Between them, the glowing sapphire-blue floor circle and the warm golden floor circles touch at a single bright tangent point of light, "
            "and in the background inside the glowing rings sits a chamber string orchestra with violins and cellos in soft cinematic bokeh."
        )
    else:
        prompt = (
            "A cinematic 16:9 medium-wide shot on a dark theatrical Venn soundstage. "
            "On the left side inside a single glowing sapphire-blue circle of light on the wooden floor stands the Black male singer from image 1 "
            "(wearing his dark slate-navy suede jacket over a burgundy shirt, eyes open, mouth completely closed, facing right toward the woman, no microphone). "
            "On the right side inside multiple overlapping warm golden-amber and rose circles of light on the wooden floor stands the chestnut-haired woman from image 2 "
            "(wearing her cream ribbed knit top, eyes open, mouth completely closed, facing left toward the man, no microphone). "
            "Between them, the glowing sapphire-blue floor circle and the warm golden floor circles touch at a single bright tangent point of light, "
            "and in the background inside the glowing rings sits a chamber string orchestra with violins and cellos in soft cinematic bokeh."
        )

    task = build_image_task(
        session=session,
        model_type=args.model,
        prompt=prompt,
        output_filename=f"Test_Outro_Local_{args.model}",
        resolution="1664x960",
        seed=7701,
        image_refs=[str(male_ref), str(female_ref)],
        ref_mode="I",
    )

    t1 = time.time()
    job = session.submit_task(task)
    for event in job.events.iter(timeout=0.5):
        if event.kind == "progress":
            p = event.data
            if p.total_steps and p.current_step in (1, p.total_steps // 2, p.total_steps):
                print(f"  [{p.phase}] step {p.current_step}/{p.total_steps} ({p.progress:.0f}%)", flush=True)
    res = job.result()
    print(f"[Done] success={res.success} in {time.time() - t1:.1f}s -> {res.generated_files}")
    if not res.success:
        for err in res.errors:
            print("ERROR:", err.message)


if __name__ == "__main__":
    main()

