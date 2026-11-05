from work.check_vocal_start import get_audio_peaks

print("Vocal RMS in male_vocal.mp3 for S02 (6.8 to 13.6s):")
p_vocal = get_audio_peaks("male_vocal.mp3", 6.8, 6.8, 0.2)
for t, rms in p_vocal:
    bar = '#' * int(rms * 100)
    print(f"{t:4.2f}s: {rms:6.3f} | {bar}")

