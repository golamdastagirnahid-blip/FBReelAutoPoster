"""Light, quality-preserving video enhancement via FFmpeg.

- eq: subtle contrast/saturation/brightness/gamma bump
- unsharp: gentle sharpening
- loudnorm: broadcast-style audio loudness normalization (EBU R128)
- Keeps original resolution / fps / aspect ratio.
- H.264 high profile, yuv420p (FB-friendly), faststart for streaming upload.
"""
from __future__ import annotations
import os
import subprocess


VIDEO_FILTER = (
    "eq=contrast=1.06:saturation=1.10:brightness=0.01:gamma=1.02,"
    "unsharp=5:5:0.6:5:5:0.0"
)
AUDIO_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"


def enhance(src: str, dst: str) -> str:
    """Run ffmpeg light enhance. Returns dst on success, raises on failure."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vf", VIDEO_FILTER,
        "-af", AUDIO_FILTER,
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
        "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-movflags", "+faststart",
        dst,
    ]
    subprocess.run(cmd, check=True)
    return dst
