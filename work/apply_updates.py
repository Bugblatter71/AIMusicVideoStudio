import json

# 1. Load all_or_nothing_v4.json
with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

# 2. Update Project Settings
d['project']['fps'] = 24
d['project']['master_output'] = "All_Or_Nothing_v4_1_Master_1920x1080_24fps.mp4"

# 3. Update Scene Prompts
scenes = d['scenes']

for s in scenes:
    sid = s['id']
    
    # S01
    if sid == 'S01':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Inside the dark recording soundstage "
            "within his single large sapphire-blue Venn circle of light with string players bowing softly in background bokeh, "
            "{MALE_STUDIO_DESC} leans into the vintage brass condenser microphone with his eyes open, natural head and upper-body sway, "
            "singing a quiet, vulnerable opening phrase with synchronized lip movements. Slow cinematic push-in.\n"
            "overall_soundscape: Quiet studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )
    
    # S02
    elif sid == 'S02':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). On the left side inside his single large sapphire-blue circle of light, {MALE_STUDIO_DESC} at the studio brass microphone sings directly toward the woman on the right with open eyes, natural head and body movement, and synchronized lip movements, while the woman on the right listens quietly with her mouth closed.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). On the right side inside her three intersecting warm golden-amber Venn floor circles by the mixing console, {FEMALE_STUDIO_DESC} keeps her mouth completely closed and her eyes open as she looks at the lyric sheets and glances up at him singing, realizing the song they are recording is about their own relationship.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )

    # S03: keep subtle orbital camera drift, use MALE_STUDIO_DESC
    elif sid == 'S03':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Medium waist-up shot on the recording soundstage where {MALE_STUDIO_DESC} grips the vintage brass stand microphone inside his single large sapphire-blue Venn circle while the chamber string orchestra and pianist play behind him. He sways naturally with open, expressive eyes, singing with synchronized lip movements. Subtle orbital camera drift.\n"
            "overall_soundscape: Studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )

    # S04
    elif sid == 'S04':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A single continuous, undivided medium-wide shot inside one open wood-paneled recording studio (no split-screen, no vertical divider, continuous studio room and continuous wooden floor across the entire frame). On the left side inside his sapphire-blue floor circle, {MALE_STUDIO_DESC} at the brass microphone sings emotionally toward the woman on the right with open eyes, natural hand and head movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S05
    elif sid == 'S05':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Pre-chorus intensity rises as {MALE_STUDIO_DESC} grips the brass stand microphone inside his single giant sapphire-blue circle with the string orchestra bowing behind him, singing earnestly with open eyes, natural body movement, and synchronized lip movements. Slow push-in.\n"
            "overall_soundscape: Studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )

    # S06
    elif sid == 'S06':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Tight close-up on {MALE_STUDIO_DESC} at the vintage brass condenser microphone as he sings the climax of the pre-chorus into the microphone with eyes open, intense emotional focus, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )

    # S07: keep dynamic orbital tracking shot, use MALE_STUDIO_DESC
    elif sid == 'S07':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Chorus 1 erupts: {MALE_STUDIO_DESC} performs passionately into the vintage brass stand microphone inside his single all-encompassing sapphire-blue Venn circle while the chamber string orchestra bows vigorously behind him. Expressive body and head motion, eyes open (blinking naturally), and synchronized lip movements. Dynamic orbital tracking shot.\n"
            "overall_soundscape: Studio room tone under the powerful vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S08
    elif sid == 'S08':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). On the left side inside his large sapphire-blue circle, {MALE_STUDIO_DESC} pours his heart into the studio microphone facing right toward her, with animated upper-body movement, open eyes locked on her, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). On the right side inside her three overlapping warm golden Venn floor circles by the mixing console, {FEMALE_STUDIO_DESC} keeps her mouth completely closed and her eyes open as she marks the arrangement on her music stand and looks back at him with deep emotion.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )

    # S09
    elif sid == 'S09':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Close-up at the vintage brass microphone as {MALE_STUDIO_DESC} delivers the final lines of the first chorus with open eyes, natural chest and head movement, and synchronized lip movements, then gently closes his mouth and exhales at the end as the strings take over.\n"
            "overall_soundscape: Studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )

    # S10: keep crane glide, complete sentence cleanly
    elif sid == 'S10':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. High-angle crane shot slowly gliding over the dark soundstage floor illuminated by the glowing Venn diagram: inside the giant sapphire-blue circle on the left, the chamber string orchestra bows their violins and cellos in sweeping unison; in the center violet intersection, the light glows softly.\n"
            "overall_soundscape: Quiet room tone, no singing or speech.\n"
            "non_diegetic_music: N/A"
        )

    # S11: fix anchor typo, set static camera
    elif sid == 'S11':
        s['anchor'] = "A07_Interlude_BothAtMics_ClosedMouth.jpg"
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Steady medium two-shot at eye-level on the soundstage, static camera. {MALE_STUDIO_DESC} on the left and {FEMALE_STUDIO_DESC} on the right stand at their microphones inside their respective glowing Venn circles with mouths closed, as the string players bow passionately in the background.\n"
            "overall_soundscape: Quiet room tone, no singing or speech.\n"
            "non_diegetic_music: N/A"
        )

    # S12a
    elif sid == 'S12a':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. As the guitar enters the arrangement, the camera slowly pushes in on {FEMALE_STUDIO_DESC} standing inside her three intersecting warm golden-amber Venn circles with her guitar strapped over her shoulder. Her mouth is completely closed and her eyes are open as she plays a brief melodic phrase on her guitar and looks up toward the male singer in the blue circle.\n"
            "overall_soundscape: Quiet room tone, no singing or speech.\n"
            "non_diegetic_music: N/A"
        )

    # S12b
    elif sid == 'S12b':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Steady medium two-shot at eye-level on the soundstage, static camera. {MALE_STUDIO_DESC} and {FEMALE_STUDIO_DESC} stand quietly at their microphones with mouths closed and eyes open, preparing to sing the duet, as the strings and brass build intensity in the background.\n"
            "overall_soundscape: Quiet room tone, no singing or speech.\n"
            "non_diegetic_music: N/A"
        )

    # S13
    elif sid == 'S13':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the left half inside his sapphire-blue light circle, {MALE_STUDIO_DESC} in three-quarter profile at his brass microphone sings directly to the woman on the right with his eyes clearly open, natural head movement, and synchronized lip movements, while the woman on the right stands with her mouth closed.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the right half inside her warm golden Venn circles, {FEMALE_STUDIO_DESC} in three-quarter profile at her brass microphone sings her counter-melody directly to the man on the left with her eyes clearly open, expressive emotion, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S14
    elif sid == 'S14':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the left half of the Venn soundstage inside his giant sapphire-blue floor circle, {MALE_STUDIO_DESC} at his stand microphone faces right toward the woman and sings directly to her with open eyes, natural body sway, and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the right half of the Venn soundstage inside her three overlapping warm golden floor circles, {FEMALE_STUDIO_DESC} at her stand microphone faces left toward the man and sings directly to him with open eyes, expressive head movement, and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S15
    elif sid == 'S15':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Inside the studio, {MALE_STUDIO_DESC} on the left faces right toward the woman and sings passionately to her with open eyes locked on hers, natural upper-body movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Inside the studio, {FEMALE_STUDIO_DESC} on the right faces left toward the man and sings her counter-lyric ('You want to build a home') directly to him with open eyes locked on his and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S16
    elif sid == 'S16':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the Venn stage, {MALE_STUDIO_DESC} on the left and {FEMALE_STUDIO_DESC} on the right sing in vocal counterpoint: he delivers his lines with open eyes and synchronized lip movements directly toward her.\n"
            "overall_soundscape: Stage room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the Venn stage, {FEMALE_STUDIO_DESC} on the right sings her soaring counter-melody ('I want to live my life') directly toward the man with open eyes and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S17a
    elif sid == 'S17a':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the studio as the heavy drums enter, {MALE_STUDIO_DESC} on the left sings with increasing desperation into the brass microphone, with open eyes, dynamic head sway, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the studio on the right, {FEMALE_STUDIO_DESC} stands by her microphone, her mouth completely closed and eyes open, listening with a conflicted expression as the arrangement builds.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )

    # S17b: STANDING AT PARTY (No table)
    elif sid == 'S17b':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Standing at an upscale evening party under a single sapphire-blue ceiling light ring on the left, {MALE_NIGHTOUT_DESC} stands just outside the lively group, turned in three-quarter profile toward the woman right beside him, singing 'You're with me and you're not, how do you feel?' directly to her with open, aching eyes and synchronized lip movements (never looking at the camera). Static eye-level camera.\n"
            "overall_soundscape: Quiet party ambience and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Standing right beside him under three overlapping warm golden ceiling rings, {FEMALE_NIGHTOUT_DESC} is happily engaged with a circle of friends on the right, laughing with mouth closed, holding a drink, gesturing naturally, and completely engrossed in conversation without noticing him singing to her. Static eye-level camera.\n"
            "overall_soundscape: Quiet party ambience, no speech.\n"
            "non_diegetic_music: N/A"
        )

    # S18: STANDING AT PARTY (No table)
    elif sid == 'S18':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Standing at the party under his single sapphire-blue light ring, {MALE_NIGHTOUT_DESC} continues to look across at the woman beside him, feeling emotionally distant amid the celebration, singing 'We're staying together / Until someone new comes along' with open, pained eyes, subtle head sway, and synchronized lip movements. Static eye-level camera.\n"
            "overall_soundscape: Quiet party ambience and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Standing under the cluster of warm golden rings, {FEMALE_NIGHTOUT_DESC} and her friends smile, converse, and enjoy the party with closed mouths, immersed in their own world and completely oblivious to his song. Static eye-level camera.\n"
            "overall_soundscape: Quiet party ambience, no speech.\n"
            "non_diegetic_music: N/A"
        )

    # S19
    elif sid == 'S19':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Returning to the dark soundstage, {MALE_STUDIO_DESC} grips the brass microphone inside his sapphire-blue circle and sings 'I'm with you and I'm not / Oh I know how it feels' with raw vulnerability, open eyes, and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S20
    elif sid == 'S20':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Medium shot on the soundstage as {MALE_STUDIO_DESC} delivers the building pre-chorus lines with open eyes, natural upper-body movement, and synchronized lip movements while the full string ensemble and bass player groove passionately behind him.\n"
            "overall_soundscape: Stage room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )

    # S21
    elif sid == 'S21':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Extreme close-up on {MALE_STUDIO_DESC} at the brass condenser microphone as he delivers the final soaring line before Chorus 3, with wide open emotional eyes, subtle head movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone under the vocal performance.\n"
            "non_diegetic_music: N/A"
        )

    # S22
    elif sid == 'S22':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Chorus 3 full-ensemble explosion: on the left side of the stage inside his giant sapphire-blue circle, {MALE_STUDIO_DESC} belts out Chorus 3 with maximum passion into his stand microphone facing right toward her, with open eyes, full chest movement, and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Chorus 3 full-ensemble explosion: on the right side of the stage inside her three warm golden circles, {FEMALE_STUDIO_DESC} belts her counter-melody into her stand microphone facing left toward him, with open eyes, passionate head and arm movement, and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S23
    elif sid == 'S23':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the studio, {MALE_STUDIO_DESC} on the left faces right toward the woman and sings 'Got to start to live for me' directly into her eyes with open eyes, expressive head movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. In the studio, {FEMALE_STUDIO_DESC} on the right faces left toward the man and sings 'to my life / You want to build a home' directly into his eyes with open eyes and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S24
    elif sid == 'S24':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the full-ensemble Venn soundstage, {MALE_STUDIO_DESC} on the left faces right toward the woman and sings 'Who I am is all I need to be' directly to her with open eyes and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the full-ensemble Venn soundstage, {FEMALE_STUDIO_DESC} on the right faces left toward the man and sings 'I need a life of my own' directly to him with open eyes and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S25
    elif sid == 'S25':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the Venn soundstage, {MALE_STUDIO_DESC} on the left sings the final vocal lines 'Now it's all or nothing / So is it all or nothing' directly to the woman on the right with open eyes and synchronized lip movements.\n"
            "overall_soundscape: Stage room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. On the Venn soundstage, {FEMALE_STUDIO_DESC} on the right finishes singing her final phrase 'I need a life of my own' directly to him with open eyes and synchronized lip movements, then gently closes her mouth while holding eye contact with him.\n"
            "overall_soundscape: Stage room tone and female vocal.\n"
            "non_diegetic_music: N/A"
        )

    # S26a: Outro Part 1 (Singers stationary, circles touch at tangent)
    elif sid == 'S26a':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Instrumental outro Part 1: {MALE_STUDIO_DESC} on the left and {FEMALE_STUDIO_DESC} on the right stand stationary in the center of the frame facing each other with mouths completely closed and eyes open, holding a tender, lingering gaze after the final vocal note. On the floor around them, his single sapphire-blue Venn circle and her cluster of golden Venn circles touch at a bright tangent point between them. Static locked-off camera on tripod.\n"
            "overall_soundscape: Quiet room tone, no singing or speech.\n"
            "non_diegetic_music: N/A"
        )

    # S26b: Outro Part 2 (Singers stationary, circles drift apart as scene fades)
    elif sid == 'S26b':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. Instrumental outro Part 2: {MALE_STUDIO_DESC} and {FEMALE_STUDIO_DESC} remain standing in their exact positions central in the frame facing each other with mouths closed and eyes locked across the space. As the camera slowly pulls back, the sapphire-blue circle on the left and the cluster of golden Venn circles on the right slowly drift and separate apart across the dark soundstage floor, opening an expanse of dark space between them as the scene gently fades out to black.\n"
            "overall_soundscape: Quiet room tone, no singing or speech.\n"
            "non_diegetic_music: N/A"
        )

# 4. Save updated JSON
with open('all_or_nothing_v4.json', 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)

print("Successfully updated all_or_nothing_v4.json with all approved changes!")

