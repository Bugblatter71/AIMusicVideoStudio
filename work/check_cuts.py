import subprocess

# Let's extract frames at expected cut points:
# Scene 0 -> 1: 6.8s
# Scene 1 -> 2: 13.6s
# Scene 2 -> 3: 20.4s
# Scene 3 -> 4: 27.2s
# Scene 4 -> 5: 33.4s
# Scene 5 -> 6: 39.6s
# Scene 6 -> 7: 48.4s

# But wait! Why does the user say:
# "the video is generating but so far the new stitched video has the audio out of time with the lipsync in the videos."
# Notice: "the video is generating but so far the new stitched video has the audio out of time with the lipsync in the videos."
# Which videos are generating? S01 to S06!
# Why would S01, S02, S03, S04, S05, S06 have the audio out of time with lipsync?
# Let's check the lip sync in S01_male_solo.mp4!

