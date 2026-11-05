import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

for s in d['scenes']:
    if s['id'] == 'S02':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). "
            "On the left side inside his single large sapphire-blue circle of light, the Black male singer at the studio brass microphone sings directly toward the woman on the right with open eyes, natural head and body movement, and synchronized lip movements, while the woman on the right listens quietly with her mouth closed.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). "
            "On the right side inside her three intersecting warm golden-amber Venn floor circles by the mixing console, the auburn-haired woman keeps her mouth completely closed and her eyes open as she looks at the lyric sheets and glances up at him singing, realizing the song they are recording is about their own relationship.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )
    elif s['id'] == 'S04':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided medium-wide shot inside one open wood-paneled recording studio (no split-screen, no vertical divider, continuous studio room and continuous wooden floor across the entire frame). "
            "On the left side inside his sapphire-blue floor circle, the Black male singer at the brass microphone sings emotionally toward the woman on the right with open eyes, natural hand and head movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided medium-wide shot inside one open wood-paneled recording studio (no split-screen, no vertical divider, continuous studio room and continuous wooden floor across the entire frame). "
            "On the right side inside her overlapping warm golden Venn circles, sitting on a studio stool beside a music stand with sheet music, her acoustic guitar on a stand beside her and the cellist bowing in the background doorway, the auburn-haired woman keeps her mouth completely closed and her eyes open, watching him thoughtfully.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )
    elif s['id'] == 'S08':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). "
            "On the left side inside his large sapphire-blue circle, the Black male singer pours his heart into the studio microphone facing right toward her, with animated upper-body movement, open eyes locked on her, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A single continuous, undivided wide-angle shot inside one open recording soundstage (no split-screen, no vertical divider line, continuous wooden floor and continuous dark studio wall across the entire frame). "
            "On the right side inside her three overlapping warm golden Venn circles by the mixing console, the auburn-haired woman keeps her mouth completely closed and her eyes open as she marks the arrangement on her music stand and looks back at him with deep emotion.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )

with open('all_or_nothing_v4.json', 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2)

print("Updated all_or_nothing_v4.json successfully!")

