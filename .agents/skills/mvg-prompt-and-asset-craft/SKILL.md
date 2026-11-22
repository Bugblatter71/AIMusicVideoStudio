---
name: mvg-prompt-and-asset-craft
description: Best-practice authoring rules for H3/LTX-2 scene prompts, Character Studio @tag compilation, {VAR} / <imageN> references, timeline cue markers, anchor framing, and two-pass duet prompts.
---

# MVG Prompt, Character & Anchor Asset Craft

Use this skill when writing or refining scene prompts, character look entries, anchor image prompts, or timeline cue markers for any Studio project.

---

## 1. H3 Multimodal Video Prompt Format

Wan2GP MiniMax H3 profiles expect four structured sections:

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. <Action, lighting, expression, camera motion, lip-sync>.
overall_soundscape: Studio room tone and vocal performance.
non_diegetic_music: N/A
```

- If a scene specifies `anchor_end`, prompts cleanly describe transition from `<Picture 1>` (start anchor) to the end composition; end anchors are ignored for Pass 2.
- Keep `overall_soundscape` factual to diegetic/room audio (`non_diegetic_music: N/A`).

---

## 2. Characters, `@tags`, `{VARS}`, and Reference Limits

`work/pipeline/characters.py` (`compile_character_prompt`) compiles prompts dynamically based on the active renderer:

- **Use `@character_look` tags** (e.g. `@male_studio`, `@female_nightout`) or `{VAR}` variables so prompts stay concise and consistent across all scenes.
  - On **Ref2VA** (`supports_image_refs: true`, e.g. `minimax_h3_ref2va_pruned_ptd_refasymopd`), `@tag` resolves to `<Subject K>` and attaches unique `<Picture 2..10>` image references (**max 9 reference images per scene**!).
  - On **FL2VA / LTX-2** (`supports_image_refs: false`, e.g. `minimax_h3_fl2va_pruned_pdd`), `@tag` expands to natural-language clothing/physical description and automatically strips dangling `<imageN>` / `<Picture 2..>` references.
- **Anchor prompt consistency**: Reference sheets can use `models.anchor_shared_settings.image_refs` with `{VAR}` or `<imageN>` (note Qwen/FLUX tag adapters in `prompts.py` automatically convert `<imageN>` to `Image N`, `image N`, or `<ref_image_N0>` depending on anchor renderer).

---

## 3. Timeline Cue Markers (`<marker_name>`)

Define project-level markers in `markers: [{"id": "M01", "name": "Beat Drop", "time": 42.5, "color": "#f59e0b"}]`.
Inside any scene prompt covering that timestamp, reference `<Beat Drop>` (automatically converted to relative time from clip start):
- `<Beat Drop>` -> `2.50s` (or frame number if preceded/followed by `frame`/`frames`)
- `<Beat Drop:s>` / `<Beat Drop:sec>` -> `2.50s` / `2.50 seconds`
- `<Beat Drop:frame>` / `<Beat Drop:f>` -> `frame 60` / `60`

---

## 4. Two-Pass Duet & Silent Scene Craft

- **`duet_method_b` (Pass 1 Left -> Pass 2 Right)**:
  - Ensure the anchor portrait gives clear left (`< split_x`) and right (`> split_x`) spatial separation (default `split_x: 0.48`–`0.52`).
  - Avoid aggressive camera pans/sweeps across `split_x` on `local` Pass 1/Pass 2 unless using `fal-ai/sync-lipsync/v2` for Pass 2, because big subject shifts across the boundary complicate half-split masking.
  - Write focused `prompt_m` (left singer action/lipsync) and `prompt_f` (right singer reaction/counter-vocal).
- **Silent scenic / instrument scenes (`mode: "silent_locked"`)**: Keep lip/mouth motion minimal or closed on vocalists (`stem: "silent"`).
- **Duration & Frame Quantization**: Target `4s–14s` per scene cleanly (`valid_video_frames` aligns to MiniMax H3 grid `5 + 16*k` / Ref2VA grid, avoiding >15s sliding window boundary artifacts).

See [prompt_patterns.md](./references/prompt_patterns.md) for copy-ready prompt & character templates.
