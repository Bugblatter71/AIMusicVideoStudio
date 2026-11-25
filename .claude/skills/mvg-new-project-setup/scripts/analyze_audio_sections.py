#!/usr/bin/env python3
"""Analyze audio & vocal stem activity intervals for planning music video scene cuts."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}


def _find_bin(name: str) -> str | None:
    local_bin = WORKSPACE_ROOT / "tools" / f"{name}.exe" if sys.platform == "win32" else WORKSPACE_ROOT / "tools" / name
    if local_bin.is_file():
        return str(local_bin)
    found = shutil.which(name)
    if found:
        return found
    for cand in sorted((Path.home() / "Downloads").glob(f"**/ffmpeg_bins/{name}*")):
        if cand.is_file():
            return str(cand)
    return None


def _read_pcm_windows(audio_file: Path, ffmpeg_bin: str, step_s: float = 0.5) -> tuple[float, list[float]]:
    """Decodes any audio track to 8kHz 16-bit mono WAV in temp and returns (duration_s, rms_db_per_step)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "mono.wav"
        cmd = [
            ffmpeg_bin, "-y", "-i", str(audio_file),
            "-ac", "1", "-ar", "8000", "-sample_fmt", "s16",
            "-vn", str(wav_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not wav_path.is_file():
            raise RuntimeError(f"ffmpeg decode failed for {audio_file.name}: {proc.stderr[:300]}")

        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            n_frames = wf.getnframes()
            duration_s = round(n_frames / float(sr or 8000), 3)
            win_samples = max(1, int(round(sr * step_s)))
            db_bins: list[float] = []
            while True:
                raw = wf.readframes(win_samples)
                if not raw:
                    break
                count = len(raw) // 2
                if count == 0:
                    break
                samples = struct.unpack(f"<{count}h", raw[: count * 2])
                sq = sum(s * s for s in samples) / float(count)
                rms = math.sqrt(sq) / 32768.0
                db = 20.0 * math.log10(max(rms, 1e-6))
                db_bins.append(round(db, 1))
            return duration_s, db_bins


def extract_active_segments(db_bins: list[float], duration_s: float, step_s: float, threshold_rel_db: float = -24.0, min_silence_s: float = 0.8) -> list[dict]:
    if not db_bins:
        return []
    peak_db = max(db_bins)
    cutoff = max(-55.0, peak_db + threshold_rel_db)
    active = [v >= cutoff for v in db_bins]

    # Bridge brief pauses shorter than min_silence_s
    max_gap_bins = max(0, int(round(min_silence_s / step_s)) - 1)
    if max_gap_bins > 0:
        i = 0
        while i < len(active):
            if not active[i]:
                j = i
                while j < len(active) and not active[j]:
                    j += 1
                if i > 0 and j < len(active) and (j - i) <= max_gap_bins:
                    for k in range(i, j):
                        active[k] = True
                i = j
            else:
                i += 1

    segs: list[dict] = []
    i = 0
    while i < len(active):
        cur_state = active[i]
        j = i
        while j < len(active) and active[j] == cur_state:
            j += 1
        t0 = round(i * step_s, 2)
        t1 = round(min(duration_s, j * step_s), 2)
        if t1 > t0:
            avg_db = round(sum(db_bins[i:j]) / max(1, j - i), 1)
            segs.append({"start": t0, "end": t1, "duration": round(t1 - t0, 2), "active": cur_state, "avg_db": avg_db})
        i = j
    return segs


def analyze_directory(audio_dir: Path, step_s: float = 0.5, threshold_rel_db: float = -24.0, min_silence_s: float = 0.8) -> dict:
    ffmpeg = _find_bin("ffmpeg")
    if not ffmpeg:
        return {"error": "ffmpeg binary not found in tools/, PATH, or Pinokio ffmpeg_bins"}

    files = sorted(f for f in audio_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    if not files:
        return {"error": f"No audio files found in {audio_dir}"}

    tracks = []
    max_dur = 0.0
    for f in files:
        dur, bins = _read_pcm_windows(f, ffmpeg, step_s=step_s)
        max_dur = max(max_dur, dur)
        segs = extract_active_segments(bins, dur, step_s, threshold_rel_db=threshold_rel_db, min_silence_s=min_silence_s)
        tracks.append({
            "file": f.name,
            "stem": f.stem,
            "duration_seconds": dur,
            "peak_db": max(bins) if bins else -120.0,
            "active_segments": [s for s in segs if s["active"]],
            "all_segments": segs,
        })

    return {"directory": str(audio_dir), "total_duration": max_dur, "step_seconds": step_s, "tracks": tracks}


def main():
    parser = argparse.ArgumentParser(description="Analyze audio stems for new MVG scene timeline layout")
    parser.add_argument("target", help="Audio directory (e.g. projects/my_song/audio) or single audio file")
    parser.add_argument("--step", type=float, default=0.5, help="Analysis window in seconds (default: 0.5)")
    parser.add_argument("--rel-db", type=float, default=-24.0, help="Relative dB below track peak for activity (default: -24)")
    parser.add_argument("--min-silence", type=float, default=0.8, help="Min silence duration (s) to split vocal phrases (default: 0.8)")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    args = parser.parse_args()

    target = Path(args.target)
    if not target.exists():
        cand = WORKSPACE_ROOT / args.target
        if cand.exists():
            target = cand
    if not target.exists():
        print(f"Error: Path not found: {args.target}", file=sys.stderr)
        sys.exit(1)

    if target.is_file():
        ffmpeg = _find_bin("ffmpeg")
        if not ffmpeg:
            print("Error: ffmpeg not found", file=sys.stderr)
            sys.exit(1)
        dur, bins = _read_pcm_windows(target, ffmpeg, step_s=args.step)
        segs = extract_active_segments(bins, dur, args.step, threshold_rel_db=args.rel_db, min_silence_s=args.min_silence)
        report = {"directory": str(target.parent), "total_duration": dur, "step_seconds": args.step, "tracks": [{"file": target.name, "stem": target.stem, "duration_seconds": dur, "peak_db": max(bins) if bins else -120.0, "active_segments": [s for s in segs if s["active"]], "all_segments": segs}]}
    else:
        report = analyze_directory(target, step_s=args.step, threshold_rel_db=args.rel_db, min_silence_s=args.min_silence)

    if "error" in report:
        print(f"Error: {report['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("=" * 88)
    print(f"  Audio Section & Vocal Activity Analysis (Duration: {report['total_duration']:.2f}s)")
    print("=" * 88)
    for tr in report["tracks"]:
        print(f"\n  Track: {tr['file']} ({tr['duration_seconds']:.2f}s | peak {tr['peak_db']:.1f} dB)")
        print("  " + "-" * 76)
        for seg in tr["all_segments"]:
            kind = "VOCAL/ACTIVE" if seg["active"] else "quiet/break "
            print(f"    {seg['start']:6.2f}s -> {seg['end']:6.2f}s  ({seg['duration']:5.2f}s) | {kind} | {seg['avg_db']:>5.1f} dB")
    print("=" * 88)


if __name__ == "__main__":
    main()
