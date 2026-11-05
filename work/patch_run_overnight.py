with open('work/run_overnight_v3.py', 'r', encoding='utf-8') as f:
    text = f.read()

target = """        if src_clip.exists() and src_clip.stat().st_size > 50000:
            rendered_count += 1
            active_clip = dst_clip
            needs_encode = (not dst_clip.exists()) or (dst_clip.stat().st_mtime < src_clip.stat().st_mtime)
            if needs_encode:
                vf_filters = [
                    f"trim=start={ctx.trim_first_frame_s:.3f}:duration={dur:.3f}",
                    "setpts=PTS-STARTPTS",
                    f"scale={res_str}:flags=lanczos",
                    f"fps={ctx.fps}",
                ]
                if i == last_idx and ctx.fade_out_last_s > 0:
                    fade_start = max(0.0, dur - ctx.fade_out_last_s)
                    vf_filters.append(f"fade=t=out:st={fade_start:.3f}:d={ctx.fade_out_last_s:.3f}")
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-i", str(src_clip),
                        "-vf", ",".join(vf_filters),
                        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p",
                        str(dst_clip),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            if dst_placeholder.exists():
                try:
                    dst_placeholder.unlink()
                except Exception:
                    pass
        else:
            placeholder_count += 1
            active_clip = dst_placeholder
            anchor_fn = sc.get("anchor")
            aid, anchor_path = ctx.resolve_anchor_path(anchor_fn)
            needs_encode = (not dst_placeholder.exists()) or (anchor_path.exists() and dst_placeholder.stat().st_mtime < anchor_path.stat().st_mtime)
            if needs_encode:
                vf_filters = [
                    f"scale={res_str}:flags=lanczos",
                    f"fps={ctx.fps}",
                ]
                if i == last_idx and ctx.fade_out_last_s > 0:
                    fade_start = max(0.0, dur - ctx.fade_out_last_s)
                    vf_filters.append(f"fade=t=out:st={fade_start:.3f}:d={ctx.fade_out_last_s:.3f}")
                if anchor_path.exists():
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-loop", "1", "-t", f"{dur:.3f}",
                        "-i", str(anchor_path),
                        "-vf", ",".join(vf_filters),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                else:
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-f", "lavfi", "-t", f"{dur:.3f}",
                        "-i", f"color=c=black:s={res_str}:r={ctx.fps}",
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"""

replacement = """        # Lock exact frame bounds on the master fps grid (eliminates cumulative timing drift)
        start_frame = int(round(sc["start"] * ctx.fps))
        end_frame = int(round(sc["end"] * ctx.fps))
        target_frames = max(1, end_frame - start_frame)

        if src_clip.exists() and src_clip.stat().st_size > 50000:
            rendered_count += 1
            active_clip = dst_clip
            needs_encode = (not dst_clip.exists()) or (dst_clip.stat().st_mtime < src_clip.stat().st_mtime)
            if needs_encode:
                vf_filters = []
                if ctx.trim_first_frame_s > 0:
                    vf_filters.extend([
                        f"trim=start={ctx.trim_first_frame_s:.3f}",
                        "setpts=PTS-STARTPTS",
                    ])
                vf_filters.extend([
                    f"scale={res_str}:flags=lanczos",
                    f"fps={ctx.fps}",
                ])
                if i == last_idx and ctx.fade_out_last_s > 0:
                    fade_start = max(0.0, dur - ctx.fade_out_last_s)
                    vf_filters.append(f"fade=t=out:st={fade_start:.3f}:d={ctx.fade_out_last_s:.3f}")
                subprocess.run(
                    [
                        str(ctx.ffmpeg), "-y", "-i", str(src_clip),
                        "-vf", ",".join(vf_filters),
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p",
                        str(dst_clip),
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            if dst_placeholder.exists():
                try:
                    dst_placeholder.unlink()
                except Exception:
                    pass
        else:
            placeholder_count += 1
            active_clip = dst_placeholder
            anchor_fn = sc.get("anchor")
            aid, anchor_path = ctx.resolve_anchor_path(anchor_fn)
            needs_encode = (not dst_placeholder.exists()) or (anchor_path.exists() and dst_placeholder.stat().st_mtime < anchor_path.stat().st_mtime)
            if needs_encode:
                vf_filters = [
                    f"scale={res_str}:flags=lanczos",
                    f"fps={ctx.fps}",
                ]
                if i == last_idx and ctx.fade_out_last_s > 0:
                    fade_start = max(0.0, dur - ctx.fade_out_last_s)
                    vf_filters.append(f"fade=t=out:st={fade_start:.3f}:d={ctx.fade_out_last_s:.3f}")
                if anchor_path.exists():
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-loop", "1",
                        "-i", str(anchor_path),
                        "-vf", ",".join(vf_filters),
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                else:
                    cmd = [
                        str(ctx.ffmpeg), "-y", "-f", "lavfi",
                        "-i", f"color=c=black:s={res_str}:r={ctx.fps}",
                        "-frames:v", str(target_frames),
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dst_placeholder),
                    ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"""

# normalize CRLF
text_norm = text.replace('\r\n', '\n')
target_norm = target.replace('\r\n', '\n')
replacement_norm = replacement.replace('\r\n', '\n')

if target_norm in text_norm:
    new_text = text_norm.replace(target_norm, replacement_norm, 1)
    with open('work/run_overnight_v3.py', 'w', encoding='utf-8') as f:
        f.write(new_text)
    print("Updated work/run_overnight_v3.py successfully!")
else:
    print("Target NOT FOUND in text_norm!")

