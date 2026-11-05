import json

with open('all_or_nothing_v4.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

# 1. Update video_shared_settings negative_prompt
neg_text = "split-screen, split frame, vertical divider, dividing line, vertical seam, border, two panels, split image"
d['models']['video_shared_settings']['negative_prompt'] = neg_text

# Also ensure video_renderers have negative_prompt if applicable
for k, v in d['models']['video_renderers'].items():
    v['negative_prompt'] = neg_text

# 2. Update scenes S02, S04, S08
for s in d['scenes']:
    sid = s['id']
    if sid == 'S02':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A seamless, continuous wide-angle shot across the open recording soundstage. "
            "Across the unified wooden floor, on the left side inside his single large sapphire-blue circle of light, "
            "{MALE_STUDIO_DESC} at the studio brass microphone sings directly toward the woman on the right with open eyes, "
            "natural head and body movement, and synchronized lip movements, while the woman on the right listens quietly with her mouth closed.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A seamless, continuous wide-angle shot across the open recording soundstage. "
            "Across the unified wooden floor, on the right side inside her three intersecting warm golden-amber Venn floor circles by the mixing console, "
            "{FEMALE_STUDIO_DESC} keeps her mouth completely closed and her eyes open as she looks at the lyric sheets and glances up at him singing, "
            "realizing the song they are recording is about their own relationship.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )
    elif sid == 'S04':
        s['prompt'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A seamless, continuous medium-wide shot inside one open wood-paneled recording studio with unified wooden flooring. "
            "On the left side inside his sapphire-blue floor circle, {MALE_STUDIO_DESC} at the brass microphone sings emotionally "
            "toward the woman on the right with open eyes, natural hand and head movement, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
    elif sid == 'S08':
        s['prompt_m'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A seamless, continuous wide-angle shot across the open recording soundstage with unified wooden flooring. "
            "On the left side inside his large sapphire-blue circle, {MALE_STUDIO_DESC} pours his heart into the studio microphone "
            "facing right toward her, with animated upper-body movement, open eyes locked on her, and synchronized lip movements.\n"
            "overall_soundscape: Studio room tone and male vocal.\n"
            "non_diegetic_music: N/A"
        )
        s['prompt_f'] = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            "integrated_multimodal_description: [Shot 1] Continue directly from <Picture 1>. "
            "A seamless, continuous wide-angle shot across the open recording soundstage with unified wooden flooring. "
            "On the right side inside her three intersecting warm golden Venn floor circles by the mixing console, "
            "{FEMALE_STUDIO_DESC} keeps her mouth completely closed and her eyes open as she marks the arrangement on her music stand "
            "and looks back at him with deep emotion.\n"
            "overall_soundscape: Quiet studio room tone, no speech.\n"
            "non_diegetic_music: N/A"
        )

with open('all_or_nothing_v4.json', 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)

print("Successfully updated all_or_nothing_v4.json with negative prompts and cleaned prompts!")

