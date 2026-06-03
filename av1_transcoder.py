import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tqdm import tqdm


VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi",
    ".m4v", ".mpg", ".mpeg", ".wmv",
    ".flv", ".webm"
}

HARDWARE_AV1_ENCODERS = [
    "av1_nvenc",     # NVIDIA
    "av1_qsv",       # Intel (QSV)
    "av1_amf",       # AMD (Windows)
    "av1_vaapi",     # Linux (VA-API)
    "av1_vulkan",    # Cross-platform (FFmpeg 8.0+)
    "av1_mediacodec",# Android
]

HQ_CQ = 23
COMPRESSED_CQ = 45


def run(cmd, description=None, capture=False, verbose=False):
    if description and verbose:
        print(f"\n[ ] {description}")
        print(" ".join(str(x) for x in cmd) + "\n")

    try:
        if capture:
            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            return result.stdout.decode(
                "utf-8",
                errors="ignore"
            )
        else:
            subprocess.run(cmd, check=True)
            return ""
    except Exception:
        return None


def sanitize_path(path_str):
    return path_str.strip().strip("'\"")


def parse_ffmpeg_time(time_str):
    """Convert ffmpeg time stamp (HH:MM:SS.mm or MM:SS.mm) to seconds."""
    parts = time_str.split(':')
    if len(parts) == 3:                     # HH:MM:SS
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    elif len(parts) == 2:                   # MM:SS
        m, s = parts
        return float(m) * 60 + float(s)
    else:
        # Should not happen, but fallback to direct float
        return float(time_str)


def format_size(size_bytes):
    """Human‑readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def check_dependencies():
    required = ["ffmpeg", "ffprobe", "MP4Box"]
    missing = [tool for tool in required if shutil.which(tool) is None]

    if not missing:
        return

    print("[!] Missing required tools:\n")
    for tool in missing:
        print(f"    - {tool}")

    print("\nInstall them:\n")
    if sys.platform.startswith("linux"):
        print("    sudo apt update && sudo apt install ffmpeg gpac")
    elif sys.platform == "darwin":
        print("    brew install ffmpeg gpac")
    else:
        print("    Windows: Download FFmpeg from https://ffmpeg.org/download.html")
        print("             Download GPAC from https://gpac.io/downloads/gpac-nightly-builds/")
    sys.exit(11)


def encoder_available(encoder_name):
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True
        )
        return encoder_name in result.stdout
    except Exception:
        return False


# Return the first available hardware AV1 encoder, or None.
def get_best_hardware_encoder():
    for enc in HARDWARE_AV1_ENCODERS:
        if encoder_available(enc):
            return enc
    return None


def probe_streams(video_path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                str(video_path)
            ],
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"ffprobe output is not valid JSON for '{video_path}': {e}"
        ) from e


def classify_video(probe):
    video = next(
        s for s in probe["streams"]
        if s.get("codec_type") == "video"
    )

    tags = video.get("tags", {})

    width = video.get("width", 0)
    height = video.get("height", 0)
    bitrate = int(video.get("bit_rate", 0) or 0)

    encoder = (
        tags.get("encoder", "")
        + " "
        + tags.get("handler_name", "")
    ).lower()

    if (
        width >= 3840
        or height >= 2160
        or bitrate >= 50_000_000
        or any(
            vendor in encoder
            for vendor in (
                "dji",
                "gopro",
                "insta360",
                "sony"
            )
        )
    ):
        return "hq"

    return "compressed"


def choose_cq(content_type, forced_cq=None):
    if forced_cq is not None:
        return forced_cq
    return HQ_CQ if content_type == "hq" else COMPRESSED_CQ


def get_color_args(probe):
    video = next(
        s for s in probe["streams"]
        if s.get("codec_type") == "video"
    )

    args = []

    mapping = {
        "color_primaries": "-color_primaries",
        "color_transfer": "-color_trc",
        "color_space": "-colorspace",
        "color_range": "-color_range",
    }

    for key, ff_arg in mapping.items():
        value = video.get(key)

        if value and value != "unknown":
            args.extend([ff_arg, value])

    return args


def get_mp4box_tracks(video_path, verbose=False):

    output = run(
        [
            "MP4Box",
            "-info",
            str(video_path)
        ],
        capture=True,
        verbose=verbose
    )

    if not output:
        print("\n[!] MP4Box -info failed. Metadata tracks will not be preserved.")
        return []

    tracks = []

    current_track = None

    track_pattern = re.compile(
        r"# Track\s+(\d+)\s+Info\s+-\s+ID\s+(\d+)"
    )

    media_pattern = re.compile(
        r"Media Type:\s+(.+)"
    )

    for line in output.splitlines():
        match = track_pattern.search(line)
        if match:
            if current_track:
                tracks.append(current_track)
            current_track = {
                "track_number": int(match.group(1)),
                "id": int(match.group(2)),
                "media_type": None
            }
            continue

        if current_track:
            media_match = media_pattern.search(line)
            if media_match:
                current_track["media_type"] = (
                    media_match.group(1)
                    .split(":")[0]
                    .lower()
                )

    if current_track:
        tracks.append(current_track)

    return tracks


def encode_av1(input_path, output_path, cq, probe, content_type,
               verbose=False, position=None, hw_encoder=None):
    total_seconds = float(probe.get("format", {}).get("duration", 0))
    color_args = get_color_args(probe)

    if hw_encoder:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", hw_encoder,
            "-c:a", "copy",
        ]
        if hw_encoder == "av1_nvenc":
            cmd += ["-preset",
                    "p7",
                    "-tune",
                    "hq",
                    "-rc",
                    "vbr",
                    "-cq", str(cq),
                    "-b:v", "0"]
        elif hw_encoder == "av1_qsv":
            cmd += ["-global_quality", str(cq), "-preset", "slow"]
        elif hw_encoder == "av1_amf":
            cmd += ["-qp_i", str(cq), "-qp_p", str(cq)]

        cmd.extend(color_args)
        cmd.append(str(output_path))

    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libsvtav1",
            "-c:a", "copy",
            "-crf", str(cq),
            "-preset", "4",
            *color_args,
            str(output_path)
        ]

    if verbose:
        enc_type = "hardware" if hw_encoder else "software"
        print(f"\n[ ] {enc_type.capitalize()} AV1 encoding ({content_type})")
        if hw_encoder:
            print(f"[*] Encoder: {hw_encoder}")
        print(" ".join(str(x) for x in cmd), "\n")

    process = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        errors="replace"
    )

    progress_re = re.compile(
        r"frame=\s*\d+\s+fps=\s*[\d.]+\s+q=[\d.\-]+\s+(?:L)?size=\s*\S+\s+time=(\S+)\s+bitrate=\s*(\S+)\s+speed=\s*(\S+)"
    )

    with tqdm(
            total=total_seconds,
            unit="s",
            bar_format="{l_bar}{bar}| {n:.0f}/{total:.0f}s [{elapsed}, ETA: {remaining}{postfix}]",
            position=position,
            leave=False
    ) as pbar:
        try:
            for line in process.stderr:
                match = progress_re.search(line)
                if match:
                    time_str = match.group(1)
                    bitrate = match.group(2)
                    speed = match.group(3)

                    if time_str == 'N/A' or time_str == 'N/A':
                        continue

                    try:
                        current_sec = parse_ffmpeg_time(time_str)
                    except ValueError:
                        current_sec = 0.0

                    # Convert time to seconds
                    current_sec = parse_ffmpeg_time(time_str)

                    pbar.n = min(current_sec, total_seconds)
                    pbar.set_postfix_str(f"{speed}, {bitrate}")
                    pbar.update(0)
        except KeyboardInterrupt:
            process.terminate()
            process.wait()

    # Wait for ffmpeg to finish and check return code
    return_code = process.wait()
    if return_code != 0:
        print("[!] Encoding failed")
        return False
    return True


def merge_tracks(encoded_video, original_video, output_video, verbose=False):

    tracks = get_mp4box_tracks(original_video)

    # Import all tracks from the encoded file (video + copied audio)
    cmd = [
        "MP4Box",
        "-quiet",
        "-keep-sys",
        "-add",
        str(encoded_video)
    ]

    preserved = 0
    for track in tracks:
        media_type = (track["media_type"] or "").lower()
        # Skip video and audio tracks – already present from the encoded file
        if media_type.startswith("vide"):
            continue
        if media_type.startswith("soun"):
            continue

        cmd.extend([
            "-add",
            f"{original_video}#{track['track_number']}"
        ])
        preserved += 1
        if verbose:
            print(
                f"[*] Preserving metadata track "
                f"{track['track_number']} "
                f"({media_type})"
            )

    if verbose:
        print(f"[*] Preserving {preserved} metadata track(s)")

    cmd.extend([
        "-new",
        str(output_video)
    ])

    return run(cmd, "Merging tracks", verbose=verbose) is not None


def process_file(input_path, cq, hw_encoder=None, verbose=False, position=None):
    input_path = Path(input_path)

    if not input_path.exists():
        print(f"[!] Missing file: {input_path}")
        return False

    try:
        probe = probe_streams(input_path)
    except Exception as e:
        print(f"[!] Failed to probe {input_path}: {e}")
        return False

    content_type = classify_video(probe)
    chosen_cq = choose_cq(content_type, cq)

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"[*] Processing: {input_path.name}")
        print(f"[*] Classification: {content_type}")
        print(f"[*] Selected CQ: {cq}")

    temp_av1 = input_path.with_name(
        f"{input_path.stem}_temp_av1.mp4"
    )

    final_output = input_path.with_name(
        f"{input_path.stem}_av1_cq_{chosen_cq}.mp4"
    )

    try:
        success = encode_av1(
            input_path, temp_av1, chosen_cq, probe, content_type,
            hw_encoder=hw_encoder,
            verbose=verbose,
            position=position
        )
        if not success:
            return False
        success = merge_tracks(temp_av1, input_path, final_output, verbose=verbose)
        return success
    finally:
        if temp_av1.exists():
            temp_av1.unlink()

    success = merge_tracks(
        temp_av1,
        input_path,
        final_output
    )

    if not success:
        print("[!] Merge failed")
        return False

    if temp_av1.exists():
        temp_av1.unlink()

    if verbose:
        print(f"[+] Finished: {final_output.name}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Hardware‑accelerated AV1 transcoder with metadata preservation."
        )
    )

    parser.add_argument(
        "path",
        nargs="?",
        help="Input file or directory"
    )

    parser.add_argument(
        "--cq",
        type=int,
        default=None,
        help="Override automatic CQ"
    )

    parser.add_argument(
        "--software",
        action="store_true",
        help="Force software AV1 encoding"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed output",
        default=False
    )

    args = parser.parse_args()

    if not args.path:
        print("\nNo path supplied.\n")
        parser.print_help()

        try:
            args.path = input(
                "\nEnter file or directory: "
            ).strip()

        except KeyboardInterrupt:
            print("\n")
            sys.exit(2)

    print()

    if not args.path:
        sys.exit(3)

    check_dependencies()

    hw_encoder = get_best_hardware_encoder()
    if args.software:
        hw_encoder = None
        print("[*] Software encoding triggered by user flag\n")
    elif hw_encoder:
        print(f"[*] Using hardware encoder: {hw_encoder}\n")
    else:
        print("[!] No supported hardware AV1 encoder found – falling back to software AV1")
        print("[!] Video transcoding may take a long time\n")
        if not encoder_available("libsvtav1"):
            print("[!] libsvtav1 encoder also not found. "
                  "Please install an ffmpeg build with SVT-AV1 support.")
            sys.exit(4)

    target = Path(
        sanitize_path(args.path)
    )

    if target.is_file():
        success = process_file(
            target,
            args.cq,
            hw_encoder=hw_encoder,
            verbose=args.verbose
        )
        if success:
            # Calculate savings for a single file
            probe = probe_streams(target)
            content_type = classify_video(probe)
            chosen_cq = choose_cq(content_type, args.cq)
            final_out = target.with_name(
                f"{target.stem}_av1_cq_{chosen_cq}.mp4"
            )
            if final_out.exists():
                orig_size = target.stat().st_size
                out_size = final_out.stat().st_size
                saved = orig_size - out_size
                pct = (saved / orig_size * 100) if orig_size else 0
                summary = (
                    f"Total savings: {format_size(saved)} "
                    f"({pct:.1f}% less) across 1 file(s)"
                )
                if args.verbose:
                    print(summary)
                else:
                    tqdm.write(summary)
        print("\n[*] Done.\n")

    elif target.is_dir():

        files = [
            f for f in target.iterdir()
            if (
                    f.is_file()
                    and
                    f.suffix.lower()
                    in VIDEO_EXTENSIONS
            )
        ]

        if not files:
            print("[!] No video files found\n")
            return

        print(
            f"[*] Found "
            f"{len(files)} video(s)\n"
        )

        total_orig = 0
        total_out = 0
        files_ok = 0

        if not args.verbose:
            with tqdm(
                    total=len(files),
                    desc="Overall",
                    unit="file",
                    position=0
            ) as overall_bar:
                for file in files:
                    success = process_file(
                        file, args.cq, hw_encoder=hw_encoder,
                        verbose=False, position=1
                    )
                    if success:
                        # Determine final output path for size calculation
                        probe = probe_streams(file)
                        content_type = classify_video(probe)
                        chosen_cq = choose_cq(content_type, args.cq)
                        final_out = file.with_name(
                            f"{file.stem}_av1_cq_{chosen_cq}.mp4"
                        )
                        if final_out.exists():
                            total_orig += file.stat().st_size
                            total_out += final_out.stat().st_size
                            files_ok += 1
                    overall_bar.update(1)
        else:
            for idx, file in enumerate(files, start=1):
                print(f"\n[{idx}/{len(files)}]")
                success = process_file(
                    file, args.cq, hw_encoder=hw_encoder,
                    verbose=True
                )
                if success:
                    probe = probe_streams(file)
                    content_type = classify_video(probe)
                    chosen_cq = choose_cq(content_type, args.cq)
                    final_out = file.with_name(
                        f"{file.stem}_av1_cq_{chosen_cq}.mp4"
                    )
                    if final_out.exists():
                        total_orig += file.stat().st_size
                        total_out += final_out.stat().st_size
                        files_ok += 1

        if files_ok:
            saved = total_orig - total_out
            pct = (saved / total_orig * 100) if total_orig else 0
            summary = (
                f"\nTotal savings: {format_size(saved)} "
                f"({pct:.1f}% less) across {files_ok} file(s)"
            )
            if args.verbose:
                print(summary)
            else:
                tqdm.write(summary)

        print("\n[*] Done.\n")

    else:
        print(
            f"[!] Invalid path: {target}\n"
        )

if __name__ == "__main__":
    main()
