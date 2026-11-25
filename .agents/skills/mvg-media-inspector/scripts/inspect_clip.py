#!/usr/bin/env python3
"""MVG Media Inspector: FFprobe metadata extraction, audio folder scanning, and review strip generator."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}


def find_binary(name: str) -> str | None:
    local_bin = WORKSPACE_ROOT / "tools" / f"{name}.exe" if sys.platform == "win32" else WORKSPACE_ROOT / "tools" / name
    if local_bin.is_file():
        return str(local_bin)
    system_bin = shutil.which(name)
    if system_bin:
        return system_bin
    for wgp_cand in sorted((Path.home() / "Downloads").glob(f"**/ffmpeg_bins/{name}*")):
        if wgp_cand.is_file():
            return str(wgp_cand)
    return None


def probe_file(file_path: Path) -> dict:
    ffprobe = find_binary("ffprobe")
    if not ffprobe:
        return {"error": "ffprobe binary not found in tools/ or system PATH"}

    cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(file_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return {"error": f"ffprobe error (code {proc.returncode}): {proc.stderr}"}
        raw = json.loads(proc.stdout)
    except Exception as e:
        return {"error": f"Failed running ffprobe: {e}"}

    v_stream = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = raw.get("format", {})

    dur = float(fmt.get("duration") or (v_stream or {}).get("duration") or (a_stream or {}).get("duration") or 0)
    size_mb = round(int(fmt.get("size", file_path.stat().st_size)) / (1024 * 1024), 2)

    info = {
        "file": file_path.name,
        "path": str(file_path),
        "size_mb": size_mb,
        "duration_seconds": dur,
        "format": fmt.get("format_name", ""),
        "video": None,
        "audio": None,
    }

    if v_stream:
        r_frame_rate = v_stream.get("r_frame_rate", "24/1")
        fps = 24.0
        try:
            num, den = map(int, r_frame_rate.split("/"))
            fps = round(num / den, 2) if den else 24.0
        except Exception:
            pass
        nb_raw = v_stream.get("nb_frames")
        nb_frames = int(nb_raw) if (nb_raw and str(nb_raw).isdigit()) else int(round(dur * fps))
        w, h = int(v_stream.get("width", 0) or 0), int(v_stream.get("height", 0) or 0)
        info["video"] = {
            "codec": v_stream.get("codec_name"),
            "width": w,
            "height": h,
            "resolution": f"{w}x{h}",
            "fps": fps,
            "nb_frames": nb_frames,
            "pixel_format": v_stream.get("pix_fmt"),
        }

    if a_stream:
        info["audio"] = {
            "codec": a_stream.get("codec_name"),
            "sample_rate": int(a_stream.get("sample_rate", 0) or 0),
            "channels": int(a_stream.get("channels", 0) or 0),
            "channel_layout": a_stream.get("channel_layout", ""),
        }

    return info


def make_filmstrip(file_path: Path, output_file: Path | None = None, duration_seconds: float = 0.0) -> Path | None:
    ffmpeg = find_binary("ffmpeg")
    if not ffmpeg:
        print("ffmpeg binary not found. Cannot generate filmstrip.", file=sys.stderr)
        return None

    if output_file is None:
        resolved = file_path.resolve()
        renders_anc = next((p for p in resolved.parents if p.name.lower() == "renders"), None)
        if renders_anc is not None:
            frames_dir = renders_anc.parent / "frames"
            output_file = frames_dir / f"{file_path.stem}_strip.jpg"
        else:
            output_file = resolved.parent / f"{file_path.stem}_strip.jpg"

    dur = max(0.5, float(duration_seconds or 5.0))
    fps_expr = f"5/{dur:.3f}"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-i", str(file_path),
        "-vf", f"fps={fps_expr},scale=-1:144,tile=5x1",
        "-frames:v", "1", "-q:v", "2", str(output_file),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode == 0 and output_file.is_file():
            return output_file
        print(f"ffmpeg failed: {proc.stderr}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error generating filmstrip: {e}", file=sys.stderr)
        return None


def scan_audio_dir(target_dir: Path, as_json: bool = False) -> None:
    files = sorted(f for f in target_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    results = [probe_file(f) for f in files]
    if as_json:
        print(json.dumps({"directory": str(target_dir), "files": results}, indent=2))
        return
    print("=" * 80)
    print(f"  Audio Directory Scan: {target_dir}")
    print("=" * 80)
    if not results:
        print("  No audio files found.")
    for info in results:
        if "error" in info:
            print(f"  [ERR] {info.get('file', '?')}: {info['error']}")
            continue
        a = info.get("audio") or {}
        print(f"  {info['file']:<28} | {info['duration_seconds']:>7.2f}s | {info['size_mb']:>5.2f} MB | {a.get('codec','?')} {a.get('sample_rate',0)}Hz {a.get('channels',0)}ch")
    print("-" * 80)


def main():
    parser = argparse.ArgumentParser(description="MVG Media Inspector")
    parser.add_argument("file", help="Path to media file or audio directory (with --scan-audio)")
    parser.add_argument("--strip", action="store_true", help="Generate a 5-frame filmstrip contact sheet JPEG")
    parser.add_argument("--scan-audio", action="store_true", help="Scan a directory of audio tracks/stems")
    parser.add_argument("--output-dir", help="Output directory for generated filmstrip")
    parser.add_argument("--json", action="store_true", help="Output info as JSON")
    args = parser.parse_args()

    target = Path(args.file)
    if not target.exists():
        cand = WORKSPACE_ROOT / args.file
        if cand.exists():
            target = cand

    if args.scan_audio or target.is_dir():
        if not target.is_dir():
            print(f"Error: Directory not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        scan_audio_dir(target, as_json=args.json)
        return

    if not target.is_file():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    info = probe_file(target)
    if args.strip:
        out_dest = (Path(args.output_dir) / f"{target.stem}_strip.jpg") if args.output_dir else None
        strip_path = make_filmstrip(target, output_file=out_dest, duration_seconds=info.get("duration_seconds", 0.0))
        if strip_path:
            info["filmstrip_generated"] = str(strip_path)

    if args.json:
        print(json.dumps(info, indent=2))
        return

    if "error" in info:
        print(f"Error: {info['error']}", file=sys.stderr)
        sys.exit(1)

    print("=" * 80)
    print(f"  Media Inspection: {info['file']}")
    print("=" * 80)
    print(f"  Size       : {info['size_mb']} MB")
    print(f"  Duration   : {info['duration_seconds']:.2f}s")
    print(f"  Format     : {info['format']}")
    v = info.get("video")
    if v:
        print(f"  Video      : {v['resolution']} ({v['codec']}) @ {v['fps']} fps ({v['nb_frames']} frames) | {v['pixel_format']}")
    else:
        print("  Video      : None")
    a = info.get("audio")
    if a:
        print(f"  Audio      : {a['codec']} | {a['sample_rate']} Hz | {a['channels']} channels ({a['channel_layout']})")
    else:
        print("  Audio      : None")
    if info.get("filmstrip_generated"):
        print(f"  Filmstrip  : {info['filmstrip_generated']}")
    print("-" * 80)


if __name__ == "__main__":
    main()
