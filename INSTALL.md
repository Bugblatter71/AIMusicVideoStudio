# Installation & Setup Guide

This guide walks you step-by-step through setting up **Music Video Generation Studio**, including installing **Pinokio**, installing the **Wan2GP** AI engine, and connecting everything.

---

## Hardware & System Requirements

- **Operating System**: Windows 10 or 11 (64-bit)
- **GPU**: NVIDIA RTX Graphics Card with at least 12 GB VRAM (RTX 3060 12GB, RTX 3080, RTX 4070, RTX 4080, RTX 4090, RTX 50-series recommended).
- **RAM**: 16 GB system RAM or more.
- **Storage**: 50–100 GB free disk space (for AI model weights and video renders).
- **Git**: [Git for Windows](https://git-scm.com/download/win) installed.

---

## Step 1: Install Pinokio

[Pinokio](https://pinokio.computer) is a free, open-source AI browser that manages isolated Python virtual environments, PyTorch, CUDA binaries, and Git repositories with a single click—no manual environment wrestling required.

1. Go to [https://pinokio.computer](https://pinokio.computer).
2. Download the Windows installer (`Pinokio-Setup-...exe`).
3. Run the installer and launch Pinokio.
4. Pinokio will prompt you to choose an installation directory (default is usually `C:\pinokio` or `%USERPROFILE%\pinokio`). Choose a drive with plenty of free disk space.

---

## Step 2: Install Wan2GP Inside Pinokio

**Wan2GP** is the underlying generation engine that generates video frames and character anchors using state-of-the-art diffusion models.

1. Open the **Pinokio** app.
2. Click on the **Discover** tab in Pinokio's left-hand navigation.
3. In the search box, type `Wan2GP` (or search for `wan`).
4. Click the **Wan2GP** card by *deepghs* or *pinokio*.
5. Click the **Install** / **Download** button.
6. Pinokio will automatically download and install:
   - A dedicated Python virtual environment
   - PyTorch with CUDA acceleration
   - Required libraries (Diffusers, Transformers, OpenCV)
   - Bundled FFmpeg binaries
7. Once the installation finishes, click **Start** in Pinokio to launch Wan2GP once. Verify that its web interface opens, then you can stop or minimize it.

---

## Step 3: Download AI Models in Wan2GP

Inside Wan2GP's web interface (or its model manager tab):
1. **Video Model (Recommended)**:
   - **MiniMax H3** (`minimax_h3`): Superb camera motion, fluid character acting, and fast generation speed.
   - **Wan 2.1 14B / 1.3B** (`wan21`): High-resolution cinema detail.
2. **Anchor / Image Model (Recommended)**:
   - **Qwen 2.1 Turbo 7B** or **Flux 2 Klein (4B / 9B)**: Used by Anchor Studio to create character reference frames and storyboards.

---

## Step 4: Install Music Video Generation Studio

1. Open **Command Prompt** or **PowerShell** and clone this repository:
   ```cmd
   git clone https://github.com/<your-username>/MusicVideoGeneration.git
   cd MusicVideoGeneration
   ```
   *(Or download and extract the ZIP archive from GitHub).*

2. Run the interactive setup wizard:
   ```cmd
   .\setup.bat
   ```
   *(Or double-click `setup.bat` in Windows Explorer).*

3. The setup wizard will:
   - Check your Git and Python setup.
   - Auto-detect your Pinokio Wan2GP directory (`api\wan.git\app`).
   - Create local configuration files (`local_config.json` and `config.local.bat`) ignored by Git.
   - Detect and configure FFmpeg.
   - Run a 15-check verification suite to confirm everything is working properly.

---

## Step 5: Launch the Studio Web UI

To open the timeline editor and render queue:

- Double-click **`launch_ui.bat`** (or run `.\launch_ui.bat` in a terminal).
- Your default web browser will open automatically at:
  ```
  http://127.0.0.1:7860
  ```

---

## Quick Reference / Command Line Usage

You can also run the studio pipeline entirely from the command line:

```cmd
# Start Studio Web UI
.\launch_ui.bat

# Create a new project template for your song
.\render_video.bat --new my_song

# Auto-detect modified scenes and render only what changed
.\render_video.bat all_or_nothing

# Render specific scenes only
.\render_video.bat all_or_nothing S01 S02

# Switch render resolution on the fly (presets 1=480p up to 6=1080p)
.\render_video.bat all_or_nothing --res 4

# Instantly re-stitch master audio & all rendered scenes (0s GPU time)
.\render_video.bat all_or_nothing stitch

# Inspect project scene & anchor catalog
.\render_video.bat all_or_nothing --list
```

---

## Troubleshooting & FAQ

### Pinokio is on another drive (e.g., `D:\Pinokio`)
`setup.bat` will ask you for your path if it doesn't find it in default locations. You can also manually create/edit `config.local.bat` in the project root:
```bat
@echo off
set "PINOKIO_PY=D:\Pinokio\api\wan.git\app\venv\Scripts\python.exe"
set "WGP_ROOT=D:\Pinokio\api\wan.git\app"
set "PATH=D:\Pinokio\bin;D:\Pinokio\bin\miniconda\Library\bin;D:\Pinokio\api\wan.git\app\ffmpeg_bins;%PATH%"
```

### Out of VRAM (CUDA OOM)
- When previewing scene framing and lip-sync motion, switch to **Resolution Preset 1 (480p: 832x480)** or **Preset 2 (540p: 960x544)** in the Studio UI or via `--res 1`.
- Reserve **Preset 4 (768p)** or **Preset 6 (1080p)** for final overnight renders.

### FFmpeg not found
If you didn't install Wan2GP via Pinokio, download FFmpeg from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or install it with Windows Package Manager:
```cmd
winget install Gyan.FFmpeg
```
Alternatively, place `ffmpeg.exe` directly inside the `tools/` folder.
