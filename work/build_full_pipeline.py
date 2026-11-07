"""
Build the complete 27-scene audio slices, Pass 1 & Pass 2 (Method B GVA) Wan2GP manifests,
and the master assembly script for 'All Or Nothing' (219.771s @ 25fps).
"""

import json
import math
import subprocess
from pathlib import Path

PROJECT = Path(r".")
FFMPEG = PROJECT / "tools" / "ffmpeg.exe"
IMAGES = PROJECT / "images"
AUDIO_DIR = PROJECT / "work" / "audio_full"
MASKS_DIR = PROJECT / "work" / "masks"
JOBS_DIR = PROJECT / "work" / "jobs"
RENDERS_DIR = PROJECT / "work" / "renders"

AUDIO_DIR.mkdir(parents=True, exist_ok=True)
MASKS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
RENDERS_DIR.mkdir(parents=True, exist_ok=True)

FPS = 25


def valid_h3_frames(min_seconds: float) -> int:
    """Return the smallest H3-valid frame count (17k + 5, >= 107) covering min_seconds at 25fps."""
    needed = math.ceil(min_seconds * FPS) + 2  # +2 frames safety margin for clean trimming
    needed = max(107, needed)
    k = math.ceil((needed - 5) / 17)
    return min(362, 17 * k + 5)


# 27 contiguous scenes covering 0.000s -> 219.771s
SCENES = [
    # ACT I: Verse 1, Pre-Chorus 1, Chorus 1 (0.00 - 66.30s)
    {
        "id": "S01",
        "start": 0.00,
        "end": 7.15,
        "mode": "male_solo",
        "anchor": "Male_Anchor_5.jpg",
        "seed": 6101,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Inside a shadowy industrial loft apartment at night lit by cool blue moonlight and warm tungsten floor lamps with an interlocking two-circle Venn diagram rug on the wooden floor behind him, the Black male singer stands facing the camera and sings a quiet, vulnerable opening vocal phrase, speaking and singing with natural lip movements synchronized. Slow cinematic push-in with gentle parallax between the foreground singer and the Venn rug background.\n"
            "overall_soundscape: Quiet loft room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S02",
        "start": 7.15,
        "end": 13.75,
        "mode": "composite_m_f_silent",
        "anchor_m": "Male_Anchor_1.jpg",
        "anchor_f": "Female_Anchor_5.jpg",
        "seed": 6102,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio with teal acoustic panels and warm amber rim light, the Black male singer at a brass vintage condenser microphone sings an emotional vocal phrase, speaking and singing with natural lip movements synchronized. Slow cinematic push-in with subtle orbital drift.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a bright sunlit creative loft atelier with a two-circle Venn diagram rug on the wooden floor in the background, the auburn-haired woman sits calmly at her wooden desk sketching in her sketchbook with her mouth closed, completely absorbed in her own independent life. Slow gentle cinematic drift across the sunlit room.\n"
            "overall_soundscape: Quiet sunlit room tone and soft pencil strokes on paper.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S03",
        "start": 13.75,
        "end": 19.90,
        "mode": "male_solo",
        "anchor": "Male_Anchor_2.jpg",
        "seed": 6103,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. The Black male singer at a vintage brass condenser microphone in a dark moody studio sings with resigned emotional weight, speaking and singing with natural lip movements synchronized. Subtle cinematic orbital pan with gentle parallax between the foreground subject and the teal-and-amber studio bokeh.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S04",
        "start": 19.90,
        "end": 26.95,
        "mode": "male_solo",
        "anchor": "Male_Anchor_5.jpg",
        "seed": 6104,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the shadowy industrial loft apartment with the interlocking two-circle Venn diagram rug on the floor behind him, the Black male singer sings an earnest, aching vocal line directly into the lens, speaking and singing with natural lip movements synchronized. Low-angle cinematic push-in with gentle parallax.\n"
            "overall_soundscape: Quiet loft room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S05",
        "start": 26.95,
        "end": 33.35,
        "mode": "male_solo",
        "anchor": "Male_Anchor_2.jpg",
        "seed": 6105,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. The Black male singer at a vintage brass condenser microphone in a dark moody studio sings with rising pre-chorus intensity, speaking and singing with natural lip movements synchronized. Slow cinematic push-in with orbital drift and gentle parallax.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S06",
        "start": 33.35,
        "end": 39.60,
        "mode": "male_solo",
        "anchor": "Male_Anchor_3.jpg",
        "seed": 6106,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Standing on a dark theatrical soundstage inside the glowing violet intersection of a sapphire-blue spotlight circle on the left and a warm golden-amber spotlight circle on the right, the Black male singer delivers an emotional pre-chorus vocal phrase into the lens, speaking and singing with natural lip movements synchronized. Tight cinematic push-in with gentle parallax against the glowing Venn floor circles.\n"
            "overall_soundscape: Quiet soundstage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S07",
        "start": 39.60,
        "end": 47.50,
        "mode": "male_solo",
        "anchor": "Male_Anchor_3.jpg",
        "seed": 6107,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark soundstage where the sapphire-blue and golden-amber circular spotlight pools intersect in a bright violet Venn overlap on the wooden floor, the Black male singer belts a soaring chorus line with full vocal power, speaking and singing with natural lip movements synchronized. Sweeping cinematic orbital tracking shot with gentle parallax.\n"
            "overall_soundscape: Quiet soundstage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S08",
        "start": 47.50,
        "end": 55.40,
        "mode": "male_solo",
        "anchor": "Dual_Anchor_Venn_Stage.jpg",
        "seed": 6108,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark theatrical soundstage lit by intersecting sapphire-blue and golden-amber spotlight beams with a violet Venn intersection between them, the Black male singer on the left sings a powerful emotional chorus line with natural lip movements synchronized, while the auburn-haired woman on the right stands quietly with her mouth closed looking calmly into the distance. Slow cinematic dolly drift across the stage.\n"
            "overall_soundscape: Quiet soundstage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S09",
        "start": 55.40,
        "end": 66.30,
        "mode": "male_solo",
        "anchor": "Male_Anchor_1.jpg",
        "seed": 6109,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Tight close-up of the Black male singer at the brass vintage condenser microphone in the dark moody studio singing the aching final lines of the chorus, speaking and singing with natural lip movements synchronized. Slow cinematic pull-back with subtle orbital drift and shallow depth-of-field bokeh.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },

    # ACT II: Orchestral Interlude (66.30 - 104.40s) + Chorus 2 Duet 1 (104.40 - 130.60s)
    {
        "id": "S10",
        "start": 66.30,
        "end": 79.00,
        "mode": "visual_only",
        "anchor": "Female_Anchor_5.jpg",
        "seed": 6110,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In her bright sunlit creative loft atelier with the interlocking two-circle Venn diagram rug on the wooden floor behind her, the auburn-haired woman sits at her wooden worktable sketching in her notebook with her mouth closed, then pauses thoughtfully, lowers her pencil, and looks toward the warm sunlight streaming through the factory windows. Smooth high-angle cinematic crane drift.\n"
            "overall_soundscape: Quiet sunlit loft ambience.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S11",
        "start": 79.00,
        "end": 91.70,
        "mode": "visual_only",
        "anchor": None,
        "seed": 6111,
        "prompt": (
            "integrated_multimodal_description: [Shot 1] High-angle overhead cinematic shot looking down at a dark polished wooden soundstage floor as two giant circular spotlight beams switch on and sweep slowly toward each other through volumetric haze: a cool sapphire-blue circle on the left and a warm golden-amber circle on the right. As the two circles of light intersect in the center, their overlapping lens glows brilliant violet-white like a living Venn diagram while the camera descends in a smooth orbital crane move.\n"
            "overall_soundscape: Quiet theatrical stage ambience.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S12",
        "start": 91.70,
        "end": 104.40,
        "mode": "visual_only",
        "anchor": "Female_Anchor_2.jpg",
        "seed": 6112,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the dark moody recording studio lit by teal acoustic panels and warm golden-amber rim light, the auburn-haired female singer stands before the brass vintage condenser microphone with her mouth closed, taking a calm, focused breath and raising her eyes toward the microphone as she prepares to sing. Slow cinematic push-in with shallow depth-of-field bokeh.\n"
            "overall_soundscape: Quiet studio room tone.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S13",
        "start": 104.40,
        "end": 110.45,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Duet.jpg",
        "seed_m": 6113,
        "seed_f": 7113,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio, the Black male singer on the left sings a soaring emotional chorus line into his brass condenser microphone, speaking and singing with natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed listening. Slow cinematic push-in with gentle parallax.\n"
            "overall_soundscape: Quiet studio room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio, the female singer on the right sings a tender, emotional vocal phrase into her brass condenser microphone, speaking and singing with natural lip movements synchronized, then closes her mouth calmly when her phrase ends. Slow cinematic push-in matching the left side.\n"
            "overall_soundscape: Quiet studio room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S14",
        "start": 110.45,
        "end": 117.10,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Venn_Stage.jpg",
        "seed_m": 6114,
        "seed_f": 7114,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On a dark theatrical soundstage lit by a sapphire-blue spotlight beam on the left and a golden-amber spotlight beam on the right intersecting in a glowing violet Venn overlap in the center, the Black male singer on the left sings passionately with natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed. Slow cinematic push-in.\n"
            "overall_soundscape: Quiet soundstage room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark theatrical soundstage under the golden-amber spotlight beam on the right, the auburn-haired female singer sings two distinct expressive vocal phrases ('But there's more... to my life'), speaking and singing with natural lip movements synchronized. Slow cinematic push-in matching the left side.\n"
            "overall_soundscape: Quiet soundstage room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S15",
        "start": 117.10,
        "end": 124.00,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Duet.jpg",
        "seed_m": 6115,
        "seed_f": 7115,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio, the Black male singer on the left sings an emotional vocal line into his brass condenser microphone, speaking and singing with natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed. Subtle cinematic orbital drift.\n"
            "overall_soundscape: Quiet studio room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In a dark moody recording studio, the female singer on the right sings an earnest vocal phrase ('You want to build a home') into her brass condenser microphone, speaking and singing with natural lip movements synchronized. Subtle cinematic orbital drift matching the left side.\n"
            "overall_soundscape: Quiet studio room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S16",
        "start": 124.00,
        "end": 130.60,
        "mode": "composite_venn_drift",
        "anchor_m": "Male_Anchor_2.jpg",
        "anchor_f": "Female_Anchor_2.jpg",
        "seed": 6116,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. The Black male singer at a vintage brass condenser microphone in a dark moody studio sings the pleading final phrase of the chorus, speaking and singing with natural lip movements synchronized. Slow cinematic pull-back.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. The auburn-haired female singer at a vintage brass condenser microphone in a dark moody studio sings a soaring, resolute vocal phrase ('I need a life of my own') and holds the final note, speaking and singing with natural lip movements synchronized. Slow cinematic pull-back.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },

    # ACT III: Verse 2 (Night-Out Narrative Storytelling) & Pre-Chorus 2 (130.60 - 173.00s)
    {
        "id": "S17",
        "start": 130.60,
        "end": 138.30,
        "mode": "male_solo",
        "anchor": "NightOut_Male_Anchor.jpg",
        "seed": 6117,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Inside an upscale moody late-night lounge under a circular sapphire-blue overhead light fixture, the Black male singer is seated amongst his smiling closed-mouth friends at a booth, keeping his head and gaze turned in three-quarter profile to the right looking across the room toward her table with longing while speaking and singing with natural lip movements synchronized (never looking at the camera). Slow cinematic push-in.\n"
            "overall_soundscape: Quiet lounge room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S18",
        "start": 138.30,
        "end": 144.30,
        "mode": "male_solo",
        "anchor": "Dual_Anchor_NightOut_Narrative.jpg",
        "seed": 6118,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Split narrative view in the late-night lounge: on the left under sapphire-blue circular light, the Black male singer amongst his friends looks across to the right in three-quarter profile and sings with natural lip movements synchronized; on the right under warm golden-amber interlocking Venn ring chandeliers, the auburn-haired woman is turned away toward her friends smiling warmly with her mouth closed, completely in the moment with her friends and not looking at the camera. Subtle cinematic drift.\n"
            "overall_soundscape: Quiet lounge ambience under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S19",
        "start": 144.30,
        "end": 152.10,
        "mode": "composite_m_f_silent",
        "anchor_m": "NightOut_Male_Anchor.jpg",
        "anchor_f": "NightOut_Female_Narrative.jpg",
        "seed": 6119,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the late-night lounge under the sapphire-blue circular light pool amongst his friends, the Black male singer looks across the room to the right in three-quarter profile with aching vulnerability and sings with natural lip movements synchronized, never looking at the camera. Slow cinematic drift.\n"
            "overall_soundscape: Quiet lounge room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the warm golden-amber light pool of the lounge under the interlocking circular brass Venn chandeliers, the auburn-haired woman is turned in profile toward her female friends, smiling and nodding happily in the moment with her mouth closed as a third warm circular spotlight pool softly illuminates the background beside her friends, never looking at the camera. Slow cinematic drift.\n"
            "overall_soundscape: Quiet warm lounge ambience.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S20",
        "start": 152.10,
        "end": 159.00,
        "mode": "male_solo",
        "anchor": "Male_Anchor_3.jpg",
        "seed": 6120,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark theatrical soundstage with the intersecting sapphire-blue and golden-amber Venn spotlight circles on the floor behind him, the Black male singer sings with newfound self-worth and quiet resolve into the camera, speaking and singing with natural lip movements synchronized. Slow cinematic push-in.\n"
            "overall_soundscape: Quiet soundstage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S21",
        "start": 159.00,
        "end": 166.00,
        "mode": "male_solo",
        "anchor": "Male_Anchor_2.jpg",
        "seed": 6121,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the dark moody recording studio at the brass vintage condenser microphone, the Black male singer delivers a passionate pre-chorus vocal phrase, speaking and singing with natural lip movements synchronized. Dynamic cinematic orbital push-in.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S22",
        "start": 166.00,
        "end": 173.00,
        "mode": "male_solo",
        "anchor": "Male_Anchor_1.jpg",
        "seed": 6122,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Tight medium close-up of the Black male singer at the brass studio microphone singing with direct emotional conviction into the lens ('I think we know it's not me it's you'), speaking and singing with natural lip movements synchronized. Tight cinematic push-in.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },

    # ACT IV: Chorus 3 / Final Duet (173.00 - 201.60s) & Tangent Venn Outro (201.60 - 219.771s)
    {
        "id": "S23",
        "start": 173.00,
        "end": 178.85,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Venn_Stage.jpg",
        "seed_m": 6123,
        "seed_f": 7123,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark theatrical soundstage lit by the sapphire-blue beam on the left and golden-amber beam on the right with a glowing violet Venn intersection between them, the Black male singer on the left belts the final chorus with full vocal power and natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed. Slow cinematic push-in.\n"
            "overall_soundscape: Quiet soundstage room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark theatrical soundstage under the golden-amber spotlight beam on the right, the auburn-haired female singer sings an emotional vocal phrase ('And I love you') with natural lip movements synchronized, then closes her mouth calmly when her phrase ends. Slow cinematic push-in matching the left side.\n"
            "overall_soundscape: Quiet soundstage room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S24",
        "start": 178.85,
        "end": 185.60,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Duet.jpg",
        "seed_m": 6124,
        "seed_f": 7124,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the dark moody recording studio, the Black male singer on the left sings a soaring chorus line into his brass condenser microphone with natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed. Subtle cinematic orbital drift.\n"
            "overall_soundscape: Quiet studio room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the dark moody recording studio, the female singer on the right sings two expressive vocal phrases ('But there's more... to my life') into her brass condenser microphone, speaking and singing with natural lip movements synchronized. Subtle cinematic orbital drift matching the left side.\n"
            "overall_soundscape: Quiet studio room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S25",
        "start": 185.60,
        "end": 192.55,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Venn_Stage.jpg",
        "seed_m": 6125,
        "seed_f": 7125,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark soundstage with the violet Venn spotlight overlap glowing between them, the Black male singer on the left sings with decisive emotional power ('Now I know what to do') and natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed. Slow cinematic pull-back.\n"
            "overall_soundscape: Quiet soundstage room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark soundstage under the golden-amber beam on the right, the auburn-haired female singer sings passionately ('You want to build a home') with natural lip movements synchronized. Slow cinematic pull-back matching the left side.\n"
            "overall_soundscape: Quiet soundstage room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S26",
        "start": 192.55,
        "end": 201.60,
        "mode": "duet_method_b",
        "anchor": "Dual_Anchor_Duet.jpg",
        "seed_m": 6126,
        "seed_f": 7126,
        "prompt_m": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the dark moody recording studio, the Black male singer on the left sings the climactic final lines ('Now it's all or nothing, so is it all or nothing') into his brass microphone with natural lip movements synchronized, while the woman on the right stands quietly with her mouth closed. Slow cinematic pull-back.\n"
            "overall_soundscape: Quiet studio room tone under the male vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
        "prompt_f": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the dark moody recording studio, the female singer on the right sings her final overlapping phrases ('I need a life of my own') into her brass microphone with natural lip movements synchronized, then closes her mouth softly at the end. Slow cinematic pull-back matching the left side.\n"
            "overall_soundscape: Quiet studio room tone under the female vocal performance.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S27a",
        "start": 201.60,
        "end": 210.70,
        "mode": "visual_only",
        "anchor": "Dual_Anchor_Venn_Stage.jpg",
        "seed": 6127,
        "prompt": (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the dark theatrical soundstage, both the Black male singer on the left and the auburn-haired female singer on the right stand silently with their mouths closed as the sapphire-blue spotlight beam and golden-amber spotlight beam slowly drift apart until the violet overlap between them narrows to a single glowing tangent line of light. Slow cinematic pull-back.\n"
            "overall_soundscape: Quiet soundstage ambience.\n"
            "non_diegetic_music: N/A"
        ),
    },
    {
        "id": "S27b",
        "start": 210.70,
        "end": 219.771,
        "mode": "visual_only",
        "anchor": None,
        "seed": 6128,
        "prompt": (
            "integrated_multimodal_description: [Shot 1] Wide high-angle cinematic shot looking down at a dark theatrical soundstage floor where two large circular pools of spotlight—a cool sapphire-blue circle on the left and a warm golden-amber circle on the right—sit side by side with their rims barely touching at a single glowing tangent point in the center. A man stands quietly in the left blue circle and a woman stands quietly in the right amber circle, looking across the single point of light at each other as the stage lights slowly dim toward darkness.\n"
            "overall_soundscape: Quiet stage room tone fading to silence.\n"
            "non_diegetic_music: N/A"
        ),
    },
]


def slice_audio(src_mp3: Path, start: float, duration: float, out_wav: Path):
    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(src_mp3),
            "-ac", "2", "-ar", "44100",
            str(out_wav),
        ],
        check=True,
    )


def ensure_right_mask(frames: int) -> Path:
    dur = frames / FPS
    mask_path = MASKS_DIR / f"mask_right_half_{frames}f.mp4"
    if not mask_path.exists():
        subprocess.run(
            [
                str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s=416x480:r={FPS}:d={dur:.3f}",
                "-f", "lavfi", "-i", f"color=c=white:s=416x480:r={FPS}:d={dur:.3f}",
                "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
                "-map", "[v]", "-c:v", "libx264", "-crf", "10",
                str(mask_path),
            ],
            check=True,
        )
    return mask_path


def make_fl2va_task(
    *,
    output_filename: str,
    prompt: str,
    video_length: int,
    seed: int,
    image_start: Path | None = None,
    audio_path: Path | None = None,
    video_guide: Path | None = None,
    video_mask: Path | None = None,
) -> dict:
    task = {
        "model_type": "minimax_h3_fl2va_pruned_pdd",
        "prompt": prompt,
        "resolution": "832x480",
        "video_length": video_length,
        "seed": seed,
        "force_fps": str(FPS),
        "num_inference_steps": 8,
        "guidance_phases": 1,
        "flow_shift": 12.0,
        "sample_solver": "euler",
        "repeat_generation": 1,
        "multi_prompts_gen_type": "PW",
        "image_mode": 0,
        "image_prompt_type": "S" if image_start else "",
        "video_prompt_type": "GVA" if (video_guide and video_mask) else "",
        "audio_prompt_type": "A" if audio_path else "",
        "sliding_window_size": 362,
        "sliding_window_overlap": 18,
        "sliding_window_discard_last_frames": 0,
        "sliding_window_trim_first_frames": 0,
        "custom_settings": {"h3_mask_mode": "grouped_rows"},
        "output_filename": output_filename,
    }
    if image_start:
        task["image_start"] = str(image_start)
    if audio_path:
        task["audio_guide"] = str(audio_path)
    if video_guide and video_mask:
        task["video_guide"] = str(video_guide)
        task["video_mask"] = str(video_mask)
        task["denoising_strength"] = 1.0
        task["masking_strength"] = 1.0
    return task


def main():
    all_tasks = []
    scene_meta = []

    male_mp3 = PROJECT / "male_vocal.mp3"
    female_mp3 = PROJECT / "female_vocal.mp3"

    for sc in SCENES:
        sid = sc["id"]
        start = sc["start"]
        end = sc["end"]
        edit_dur = round(end - start, 3)
        frames = valid_h3_frames(edit_dur)
        gen_dur = frames / FPS
        mode = sc["mode"]

        if mode == "male_solo":
            wav_m = AUDIO_DIR / f"{sid}_male.wav"
            slice_audio(male_mp3, start, gen_dur, wav_m)
            out_name = f"{sid}_male_solo"
            all_tasks.append(
                make_fl2va_task(
                    output_filename=out_name,
                    prompt=sc["prompt"],
                    video_length=frames,
                    seed=sc["seed"],
                    image_start=IMAGES / sc["anchor"],
                    audio_path=wav_m,
                )
            )
            scene_meta.append({"id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode, "clip": f"{out_name}.mp4"})

        elif mode == "visual_only":
            out_name = f"{sid}_visual"
            all_tasks.append(
                make_fl2va_task(
                    output_filename=out_name,
                    prompt=sc["prompt"],
                    video_length=frames,
                    seed=sc["seed"],
                    image_start=(IMAGES / sc["anchor"]) if sc.get("anchor") else None,
                    audio_path=None,
                )
            )
            scene_meta.append({"id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode, "clip": f"{out_name}.mp4"})

        elif mode in ("composite_m_f_silent", "composite_venn_drift"):
            wav_m = AUDIO_DIR / f"{sid}_male.wav"
            slice_audio(male_mp3, start, gen_dur, wav_m)
            out_m = f"{sid}_M"
            out_f = f"{sid}_F"
            all_tasks.append(
                make_fl2va_task(
                    output_filename=out_m,
                    prompt=sc["prompt_m"],
                    video_length=frames,
                    seed=sc["seed"],
                    image_start=IMAGES / sc["anchor_m"],
                    audio_path=wav_m,
                )
            )
            if mode == "composite_venn_drift":
                wav_f = AUDIO_DIR / f"{sid}_female.wav"
                slice_audio(female_mp3, start, gen_dur, wav_f)
                all_tasks.append(
                    make_fl2va_task(
                        output_filename=out_f,
                        prompt=sc["prompt_f"],
                        video_length=frames,
                        seed=sc["seed"] + 500,
                        image_start=IMAGES / sc["anchor_f"],
                        audio_path=wav_f,
                    )
                )
            else:
                all_tasks.append(
                    make_fl2va_task(
                        output_filename=out_f,
                        prompt=sc["prompt_f"],
                        video_length=frames,
                        seed=sc["seed"] + 500,
                        image_start=IMAGES / sc["anchor_f"],
                        audio_path=None,
                    )
                )
            scene_meta.append(
                {
                    "id": sid,
                    "start": start,
                    "end": end,
                    "dur": edit_dur,
                    "mode": mode,
                    "clip_m": f"{out_m}.mp4",
                    "clip_f": f"{out_f}.mp4",
                }
            )

        elif mode == "duet_method_b":
            wav_m = AUDIO_DIR / f"{sid}_male.wav"
            wav_f = AUDIO_DIR / f"{sid}_female.wav"
            slice_audio(male_mp3, start, gen_dur, wav_m)
            slice_audio(female_mp3, start, gen_dur, wav_f)
            mask_path = ensure_right_mask(frames)

            out_p1 = f"{sid}_Pass1_Male"
            out_p2 = f"{sid}_Pass2_Duet"

            # Reuse already-rendered S13 if time range matches (104.40 - 110.45 is covered by Duet_MethodB_Pass2_BothIndependent.mp4!)
            all_tasks.append(
                make_fl2va_task(
                    output_filename=out_p1,
                    prompt=sc["prompt_m"],
                    video_length=frames,
                    seed=sc["seed_m"],
                    image_start=IMAGES / sc["anchor"],
                    audio_path=wav_m,
                )
            )
            all_tasks.append(
                make_fl2va_task(
                    output_filename=out_p2,
                    prompt=sc["prompt_f"],
                    video_length=frames,
                    seed=sc["seed_f"],
                    image_start=IMAGES / sc["anchor"],
                    audio_path=wav_f,
                    video_guide=RENDERS_DIR / f"{out_p1}.mp4",
                    video_mask=mask_path,
                )
            )
            scene_meta.append({"id": sid, "start": start, "end": end, "dur": edit_dur, "mode": mode, "clip": f"{out_p2}.mp4"})

    manifest_path = JOBS_DIR / "full_music_video_batch.json"
    manifest_path.write_text(json.dumps(all_tasks, indent=2), encoding="utf-8")

    meta_path = JOBS_DIR / "scenes_timeline.json"
    meta_path.write_text(json.dumps(scene_meta, indent=2), encoding="utf-8")

    total_frames = sum(t["video_length"] for t in all_tasks)
    print(f"Created {len(all_tasks)} tasks across {len(SCENES)} scenes ({total_frames} total frames = {total_frames/FPS:.1f}s of H3 video).")
    print(f"Estimated render time on RTX 3070: ~{total_frames/FPS * 0.52 / 60:.1f} hours.")


if __name__ == "__main__":
    main()

