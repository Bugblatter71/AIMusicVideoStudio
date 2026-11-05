import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")

PINOKIO_BIN = r"C:\Users\bugbl\Downloads\pinokio\bin"
WAN_FFMPEG = r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\ffmpeg_bins"
for extra_dir in (PINOKIO_BIN, os.path.join(PINOKIO_BIN, "miniconda", "Library", "bin"), WAN_FFMPEG):
    if os.path.isdir(extra_dir):
        os.environ["PATH"] = extra_dir + os.pathsep + os.environ.get("PATH", "")

WAN_ROOT = Path(r"C:\Users\bugbl\Downloads\pinokio\api\wan.git\app")
PROJECT_ROOT = Path(r"C:\Users\bugbl\AppData\Local\Temp\MusicVideoGeneration")
OUTPUT_DIR = PROJECT_ROOT / "work" / "renders"

sys.path.insert(0, str(WAN_ROOT))
from shared.api import init  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("Usage: run_wan.py <manifest.json>")
        sys.exit(1)

    manifest_path = Path(sys.argv[1]).resolve()
    raw_tasks = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(raw_tasks, dict):
        raw_tasks = [raw_tasks]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tasks = []
    skipped = 0
    for t in raw_tasks:
        out_name = t.get("output_filename", "")
        ext = ".jpg" if t.get("image_mode", 0) == 1 else ".mp4"
        if out_name and (OUTPUT_DIR / f"{out_name}{ext}").exists():
            print(f"[run_wan] Skipping already completed: {out_name}{ext}", flush=True)
            skipped += 1
        else:
            tasks.append(t)

    if not tasks:
        print(f"[run_wan] All {skipped} task(s) already completed!", flush=True)
        sys.exit(0)

    t0 = time.time()
    print(f"[run_wan] Initializing Wan2GP session from {WAN_ROOT} ({len(tasks)} to run, {skipped} skipped)...", flush=True)
    session = init(
        root=WAN_ROOT,
        output_dir=OUTPUT_DIR,
        console_output=True,
        console_isatty=False,
    )
    print(f"[run_wan] Session initialized in {time.time() - t0:.1f}s. Submitting {len(tasks)} task(s)...", flush=True)

    job = session.submit_manifest(tasks)
    last_phase = None
    last_step = -1
    for event in job.events.iter(timeout=0.5):
        if event.kind == "progress":
            p = event.data
            if (p.phase, p.current_step) != (last_phase, last_step):
                last_phase, last_step = p.phase, p.current_step
                print(f"[progress] {p.phase}: step {p.current_step}/{p.total_steps} ({p.progress:.0%})", flush=True)
        elif event.kind == "error":
            e = event.data
            print(f"[error] task {e.task_index} ({e.stage}): {e.message}", flush=True)

    result = job.result()
    elapsed = time.time() - t0
    print(
        f"[run_wan] Done in {elapsed:.1f}s — success={result.success} "
        f"({result.successful_tasks}/{result.total_tasks} ok)",
        flush=True,
    )
    for f in result.generated_files:
        print(f"  -> {f}", flush=True)
    for err in result.errors:
        print(f"  !! task {err.task_index} [{err.stage}]: {err.message}", flush=True)
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()

