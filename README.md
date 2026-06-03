# AV1 Transcoder

Hardware‑accelerated AV1 video encoder with automatic quality selection and full metadata preservation.

## Features

- Automatically detects the best available hardware AV1 encoder (NVIDIA, Intel QSV, AMD AMF, VA‑API, Vulkan, MediaCodec) or falls back to efficient software encoding via `libsvtav1`.
- Classifies videos as **HQ** (high‑quality source: 4K+, high bitrate, DJI/GoPro/Insta360/Sony camera) or **compressed** to choose an appropriate constant quality level.
- Preserves **all metadata tracks** (GPS, telemetry, timecodes, etc.) from the original file using MP4Box.
- Supports both single files and batch processing of directories.
- Clear progress bars with real‑time encoding speed, bitrate, and final disk‑space savings.

## Requirements

- **Python 3.8+**
- **FFmpeg** – built with SVT‑AV1 (`libsvtav1`) and at least one of the hardware encoders if you want GPU acceleration.
- **GPAC** – provides the `MP4Box` tool for metadata merging.
- **tqdm** – Python progress bar library (`pip install tqdm`).

### Installing system dependencies

**Linux (Debian/Ubuntu)**
```bash
sudo apt update && sudo apt install ffmpeg gpac
```

**macOS (Homebrew)**
```bash
brew install ffmpeg gpac
```

**Windows**
Download pre‑built binaries:
- FFmpeg: https://ffmpeg.org/download.html
- GPAC: https://gpac.io/downloads/gpac-nightly-builds/

Ensure `ffmpeg`, `ffprobe`, and `MP4Box` are in your system PATH.

Python dependency:
```bash
pip install tqdm
```

## Usage

```bash
python av1_transcoder.py <file_or_directory> [options]
```

### Arguments

| Argument         | Description                                                                 |
|------------------|-----------------------------------------------------------------------------|
| `path`           | Input video file or directory containing videos.                            |
| `--cq CQ`        | Override automatic CQ value. (23 for HQ, 45 for compressed by default).     |
| `--software`     | Force software encoding even if a hardware encoder is available.            |
| `--verbose`      | Show detailed per‑step output.                                              |

## How it works

1. **Probe** – ffprobe extracts stream and format metadata.
2. **Classify** – The video is tagged as `hq` or `compressed` based on resolution, bitrate, and camera brand.
3. **Encode** – ffmpeg encodes the video track (and copies audio) using the chosen hardware encoder or `libsvtav1`. A constant quality (CQ/CRF) value controls the output quality/size trade‑off.
4. **Merge** – MP4Box copies all non‑video‑audio tracks (e.g., GPS, telemetry) from the original into the new AV1 file, ensuring no metadata is lost.
5. **Cleanup** – The intermediate file is removed, and disk savings are reported.

### Automatic CQ selection

- **HQ** (high‑quality source) → CQ **23** (visually lossless)
- **Compressed** → CQ **45** (good compression)

You can override with `--cq` if needed.

### Supported hardware encoders

| Encoder            | Backend         |
|--------------------|-----------------|
| `av1_nvenc`        | NVIDIA GPU      |
| `av1_qsv`          | Intel QuickSync |
| `av1_amf`          | AMD AMF         |
| `av1_vaapi`        | VA‑API (Linux)  |
| `av1_vulkan`       | Vulkan (FFmpeg 8.0+) |
| `av1_mediacodec`   | Android         |

If none are found, software SVT‑AV1 is used automatically.

## Output naming

The encoded file is saved in the same directory as the source, with a suffix:

```
original_stem_av1_cq_<value>.mp4
```

Example: `my_video.mp4` → `my_video_av1_cq_23.mp4`

## Examples

Single file, verbose output:
```bash
python av1_transcoder.py vacation.mp4 --verbose
```

Batch process all videos in a folder, force software encoding:
```bash
python av1_transcoder.py ./footage/ --software
```

Override quality for a high‑compression re‑encode:
```bash
python av1_transcoder.py drone_shot.mov --cq 40
```

## License

This project is licensed under the MIT License – see the LICENSE file for details.
