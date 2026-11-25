# MVG Prompt & Asset Templates

## 1. Anchor Portrait Prompt (16:9 Keyframe)
```text
Cinematic 16:9 medium close-up of {LEAD_DESC} inside a dimly lit neon jazz club. Warm tungsten key light from stage right highlights facial features and expressive open eyes; rain-streaked glass bokeh in background. Shot on 35mm anamorphic lens, shallow depth of field.
```
*For two-pass duets (`duet_method_b`): keep Singer A cleanly framed on the left half (`x < 0.48`) and Singer B cleanly framed on the right half (`x > 0.52`) without overlapping torsos, limbs, or instruments; for LTX-2 (`#14/#15/#16` `MVGA`), also leave a `~10%` clear buffer `[split_x - 0.10, split_x]` for Laplacian seam blending.*

## 2. Solo Singing Scene Prompt (`mode: "solo"`)
```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Slow, steady dolly push-in. @lead_studio leans subtly toward the microphone and sings earnestly with natural breathing, expressive eye contact, and tight lip-sync to the vocal stem; at <Chorus Hit>, shoulders drop slightly and hand gestures outward in emphasis.
overall_soundscape: Warm room tone and isolated lead vocal.
non_diegetic_music: N/A
```

## 3. Atmospheric / B-Roll / Instrumental Scene Prompt (`mode: "silent_locked"`)
```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Gentle handheld lateral drift across the dimly lit stage as @lead_studio stands thoughtfully with lips closed, watching rain droplets slide down the studio window under cool blue rim light.
overall_soundscape: Quiet rain and ambient room tone.
non_diegetic_music: N/A
```

## 4. Two-Pass Duet Scene (`mode: "duet_method_b"`)
- `prompt_m`: Describe Pass 1 (male left singer singing his phrase, female right singer attentive/listening cleanly on right).
- `prompt_f`: Describe Pass 2 (female right singer responding with clear synchronized lip movement while keeping frame continuity).
- Keep `negative_prompt` from `video_shared_settings`:
  `"split-screen, split frame, vertical divider, dividing line, vertical seam, border, two panels, split image"`.

## 5. Character Catalog Entry Template (`project.json -> "characters"`)
```json
"lead": {
  "id": "lead",
  "name": "Alex (Lead Singer)",
  "kind": "character",
  "default_look": "studio",
  "avatar": "references/alex_front34.jpg",
  "physical_traits": "early-30s woman with copper wavy bob and green eyes",
  "looks": {
    "studio": {
      "tag": "@lead_studio",
      "name": "Velvet Blazer",
      "description": "wearing an emerald velvet blazer over a cream silk camisole",
      "image_refs": [
        "references/alex_front34.jpg",
        "references/alex_profile.jpg"
      ]
    }
  }
}
```
