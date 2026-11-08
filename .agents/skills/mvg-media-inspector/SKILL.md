---
name: mvg-media-inspector
description: Inspects generated video clips, anchor portraits, filmstrips, and ffprobe stream metadata without heavy token overhead.
---

# MVG Media Inspector

Use this skill whenever you need to inspect rendered MP4 video clips, anchor portraits, audio streams, or filmstrips in the Music Video Generation Studio.

---

## 1. Fast CLI Commands (`inspect_clip.py`)

Inspect any media file without loading raw binary bytes or flooding the context with ffprobe noise:

### 1.1 Inspect Video Metadata
Outputs resolution, exact duration, frame count, FPS, video codec, audio sample rate, and channels:
```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/all_or_nothing/work/renders_v3/S01_male_solo_1344x768.mp4
```

### 1.2 Output JSON Summary
```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/all_or_nothing/work/renders_v3/S01_male_solo_1344x768.mp4 --json
```

### 1.3 Generate Contact Sheet / Filmstrip
Generates a 5x1 composite review filmstrip of the clip using local FFmpeg:
```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/all_or_nothing/work/renders_v3/S01_male_solo_1344x768.mp4 --strip
```
Writes `<filename>_strip.jpg` to the project's review strips directory.
