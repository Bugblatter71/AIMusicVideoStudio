# Music Video Generation Studio 🎬🎶

> **An AI-native, DAW-style music video workstation powered by Pinokio, Wan2GP, and Python.**  
> Effortlessly orchestrate, generate, lip-sync, and stitch full-length music videos with character consistency and zero drift.

---

## 🌟 Key Features

### 🎚️ 1. DAW-Style Interactive Timeline
- **Multi-Track Audio & Video Canvas**: Visual timeline with zoom, pan, and smooth sub-second scrubbing.
- **Audio Stem Waveform Visualization**: See lead vocal and backing track stems right underneath video clips.
- **Timeline Markers & Sections**: Place markers (`[Intro]`, `[Verse 1]`, `[Chorus]`, `[Guitar Solo]`) to organize your storyboard.
- **Dynamic Cut Boundaries**: Adjust scene start and end times visually with real-time audio slice preview.

### 🎭 2. Anchor Studio & Character Consistency
- **Persistent Character Identities**: Generate and manage multi-angle anchor sheets for characters, performers, and recurring scene environments.
- **Reference-Guided Video Generation**: Directly feeds character anchors into diffusion models to ensure singers look identical across 30+ separate scenes.
- **Local Model Generation**: Supports local image generation via **Qwen 2.1 Turbo**, **Flux 2 Klein**, or custom image uploads.

### 🎤 3. Drift-Free Audio Stem Lip-Sync
- **Scene-Exact Audio Slicing**: Automatically slices audio files into sample-accurate scene segments.
- **Lead Vocal Lip-Sync Guidance**: Pairs Wan2GP's audio-conditioned animation directly with the clean vocal stem so mouths articulate precisely on lyrics rather than drumbeats.

### 👥 4. Two-Pass Duet GVA (Guaranteed Vocal Acting)
- Solves the classic AI problem where two singers sing simultaneously or mirror each other's words.
- **Pass 1 (Cinematography)**: Generates the full scene with camera motion and physical acting.
- **Pass 2 (Per-Vocalist Masking)**: Re-renders singer facial regions independently driven only by their respective vocal track.

### ⚡ 5. Incremental Hash-Based Render Queue
- **Smart Change Detection**: Computes cryptographic hashes of prompts, model settings, anchors, and seed values.
- **Never Re-renders Unchanged Clips**: If you change scene 10, only scene 10 is sent to the GPU.
- **Full Queue Management**: Re-order, pause, restart, or prioritize tasks from the Studio Web UI.

### 🧵 6. Progressive Master Restitching
- As each scene finishes rendering, the master video is automatically updated in real-time.
- Zero GPU overhead: Uses stream-copy FFmpeg concatenation to build the full song video in under 3 seconds.

---

## 🚀 Quick Start

### 1. Requirements
- Windows 10/11 (64-bit)
- NVIDIA RTX GPU (12GB+ VRAM recommended)
- [Git for Windows](https://git-scm.com/download/win)

### 2. Install Pinokio & Wan2GP
Follow our friendly **[Complete Installation Guide (INSTALL.md)](INSTALL.md)** to download Pinokio and install Wan2GP in 3 clicks.

### 3. Setup This Repository
Clone the repository and run the setup wizard:
```cmd
git clone https://github.com/<your-username>/MusicVideoGeneration.git
cd MusicVideoGeneration
.\setup.bat
```
The wizard auto-detects your Pinokio installation and configures everything.

### 4. Launch the Studio Web UI
Double-click **`launch_ui.bat`** or run:
```cmd
.\launch_ui.bat
```
Your browser will launch automatically at **`http://127.0.0.1:7860`**.

---

## 🛠️ Command-Line Interface (CLI)

Prefer working from the terminal or running batch overnight jobs? Use `render_video.bat`:

| Command | Description |
| :--- | :--- |
| `.\launch_ui.bat` | Launch the Web UI Studio on port 7860 |
| `.\render_video.bat --new <project_name>` | Create a fresh project folder and JSON template |
| `.\render_video.bat <project>` | Auto-detect modified scenes and render only what changed |
| `.\render_video.bat <project> --list` | List all scenes, anchors, and render statuses |
| `.\render_video.bat <project> --res 4` | Switch render resolution on the fly (options 1 to 6) |
| `.\render_video.bat <project> --vr 1` | Switch video model on the fly |
| `.\render_video.bat <project> S01 S05` | Render specific scene(s) and restitch master |
| `.\render_video.bat <project> --anchors-only A01` | Regenerate specific character anchor image |
| `.\render_video.bat <project> stitch` | Restitch master MP4 immediately (0s GPU time) |

---

## 📐 Resolution Profiles Reference

| Preset | Name | Dimensions | VRAM | Best For |
| :---: | :--- | :---: | :---: | :--- |
| **1** | 480p Fast Preview | 832x480 | ~10-12 GB | Rapid motion, timing, and framing tests |
| **2** | 540p qHD Balanced | 960x544 | ~12-14 GB | Fast full-song preview drafts |
| **3** | 720p HD Standard | 1280x720 | ~14-16 GB | High quality preview |
| **4** | 768p WXGA High-Res | 1344x768 | ~16-18 GB | **Recommended Default** for native DiT quality |
| **5** | 864p Near-1080p | 1536x864 | ~18-20 GB | Crisp hero clips |
| **6** | 1080p Full HD | 1920x1088 | ~22-24 GB | Cinema-grade final renders |

---

## 📁 Repository Structure

```text
MusicVideoGeneration/
├── launch_ui.bat             # Studio Web UI launcher
├── render_video.bat          # Command-line overnight render pipeline
├── setup.bat                 # Interactive setup & environment wizard
├── publish_to_github.bat     # Helper script to link and push to your GitHub repo
├── model_profiles.json       # Central video & anchor profile definitions
├── project_template.json     # Blank project template
├── INSTALL.md                # Comprehensive Pinokio & Wan2GP setup guide
├── requirements.txt          # Python dependency documentation
├── projects/                 # User project directories
│   └── all_or_nothing/       # Included full-song demo project
│       ├── audio/            # Master audio & stem tracks
│       ├── all_or_nothing.json # Song timeline, prompts & scene graph
│       └── work/             # Sliced audio, anchors, rendered clips
├── work/                     # Studio backend engines
│   ├── run_overnight_v3.py   # Core headless pipeline & hashing engine
│   ├── run_wan.py            # Pinokio Wan2GP runtime bridge
│   └── ui/                   # Web UI backend & frontend static assets
└── tests/                    # Automated regression test suite
```

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request or open an Issue for bug reports or feature ideas.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
