---
name: mvg-cloud-and-duet-ops
description: Guide for fal.ai and MiniMax cloud backends, DPAPI credentials, backend routing precedence, Two-Pass Duet Pass 2 ROI / lip-sync repair, upscaling, and GPU VRAM OOM trade-offs.
---

# MVG Cloud Backends, Two-Pass Duet & Upscaling

Use this skill when configuring `fal.ai` / `MiniMax` cloud rendering, deciding backends for Two-Pass Duet (`duet_method_b`) scenes, troubleshooting CUDA OOM crashes, or running `repair-lipsync` / `upscale`.

---

## 1. Backend Routing Precedence & Spend Safety

1. **Resolution Order** (`fal_catalog.resolve_task_backend`):
   `queue item backend_override` > `scene backend_f` (for Pass 2) / `scene backend` > `project models.default_backend` > `renderer profile backend` > `"local"`.
2. **Default Toggle Safety**:
   In both backend (`server.enabled_engines`) and UI checkboxes, **`local` starts ticked (`True`) while `fal` and `minimax` start unticked (`False`)** so queued cloud items never spend credits until explicitly checked.
3. **API Credentials & Testing**:
   - Credentials are stored DPAPI-encrypted via `fal_catalog.py` / Studio UI (`fal_settings.js`).
   - Status/key test endpoints: `GET /api/fal/status`, `GET /api/minimax/status`, `POST /api/fal/test`, `POST /api/minimax/test`.

---

## 2. Two-Pass Duet (`duet_method_b`) Across Engines

Two-Pass Duet renders Pass 1 (`<SID>_Pass1_Left_<res>.mp4`, male/left audio stem) then Pass 2 (`<SID>_Pass2_Final_<res>.mp4`, female/right audio stem):

| Backend (Pass 2) | Adapter / Behavior | Strengths & Trade-Offs |
| :--- | :--- | :--- |
| **`local`** (Wan2GP VACE guide) | Uses full Pass 1 video + spatial right-side mask as guide frames (`backend_f: "local"`). | Follows Pass 1 camera motion, lighting & seam best; heavy on VRAM at `1920x1088` (~175f+ can OOM on 24GB). |
| **`fal`** (`fal-ai/sync-lipsync/v2`) | Video-to-video lip-sync directly on cropped right ROI (`duet_pass2_roi_lipsync`), composited with feathered edge (`18px`). | Preserves exact Pass 1 framing & orbital camera drift cleanly without local VRAM limits. |
| **`minimax`** (`duet_pass2_roi_minimax`) | Regenerates right ROI crop from single `reference_image` + `reference_audio` (`adaptive` ratio) and composites onto Pass 1. | Fast & offloads VRAM, **but** does not take guide video frames—use on locked/static cameras rather than moving orbital shots. |

- Control Pass 2 ROI crop with `pass2_roi_mode`: `"full"`, `"half_split"`, `"tight_performer"`, or `"custom"`.
- Notice cloud clips >15s automatically chunk in `run_chunked_video_sequence`, and MiniMax H3 native caps at `768P` / `2K` (`fal_catalog.resolve_minimax_route`) with Stage 3 scaling to master resolution.

---

## 3. Targeted Lip-Sync Repair & Upscaling CLI

```bash
# Repair only the right performer's lip-sync on a rendered duet clip:
python work/run_overnight_v3.py <slug> repair-lipsync <scene_id> --side right --roi-mode half_split [--backend local|fal]

# Upscale a scene's active take (FlashVSR+ / SeedVR2 / fal.ai):
python work/run_overnight_v3.py <slug> upscale <scene_id> --method flashvsrplus*2 [--upscale-backend local|fal]
```

---

## 4. Non-Lip-Sync / Scenic Profile Catalog (`model_profiles.json` & `fal_adapters.py`)
- **Local Wan2GP (`video_renderers`)**: `#17` **`i2v_2_2`** (Wan 2.2 14B MoE, `16fps`, `wan` `4k+1` frames, start+end anchor) and `#18` **`ti2v_2_2`** (Wan 2.2 5B Fast, `24fps` budget scenic).
- **`fal.ai` (`fal_video_renderers`)**: `fal_kling_v3_std` / `fal_kling_v3_pro` (start+end I2V), `fal_seedance_2_fast` / `fal_seedance_v1_pro` (action/choreography), `fal_wan_26_i2v` (atmospheric/artistic), `fal_veo31_fast` / `fal_veo31_lite` (`generate_audio: false`).
- **Google Flow / Nano Banana Hybrid Workflow**: Generate external Veo/Flow clips interactively and ingest them via **`📥 Import Clip`** (UI) or `import-clip` (CLI) so Stage 3 stitches them cleanly at project FPS/resolution without re-rendering over them.

---

## 5. VRAM & CUDA OOM Playbook (Local Wan2GP)
- **`1344x768`** (~16–18 GB) and **`1536x864`** (~18–20 GB) avoid host/VRAM thrashing; **`1920x1088`** pushes 24 GB GPUs to the limit on long (~175+ frame) sequences and can crash at late denoising/VAE decode (`CUDA OOM`).
- Options when a `1920x1088` pass hits `CUDA OOM`: render at `1344x768` / `1536x864` and upscale (`flashvsrplus*2` or `upscale`), switch Pass 2 to `fal` (`sync-lipsync/v2`) when preserving camera motion, or ensure no other GPU processes are running before clicking `🔄 Retry` / `▶ Run`.
