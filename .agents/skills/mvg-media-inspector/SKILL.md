---
name: mvg-media-inspector
description: Inspects generated video clips, anchor/reference images, audio files, filmstrips, and ffprobe stream metadata without heavy token overhead.
---

# MVG Media Inspector

Use this skill whenever you need to inspect rendered MP4 clips, anchor/character images, audio stems, or filmstrips in the Music Video Generation Studio.

---

## 1. Fast CLI Commands (`inspect_clip.py`)

### 1.1 Inspect Video, Image, or Audio File
Outputs resolution, exact duration, frame count, FPS, codec, pixel format, and audio sample rate/channels:
```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/all_or_nothing/work/renders_v3/S01_male_solo_1344x768.mp4
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/all_or_nothing/work/anchors_v3/A01.jpg --json
```

### 1.2 Generate Evenly-Spaced 5-Frame Review Filmstrip
Spaced proportionally across the full duration of the clip (works for any length clip):
```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py <path_to_mp4> --strip
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py <path_to_mp4> --strip --output-dir <dir>
```

### 1.3 Batch-Scan a Project's Audio Folder (Useful for New Projects)
Lists every audio track/stem in `projects/<slug>/audio/` with duration, sample rate, channels, and codec:
```bash
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/<slug>/audio --scan-audio
python .agents/skills/mvg-media-inspector/scripts/inspect_clip.py projects/<slug>/audio --scan-audio --json
```
