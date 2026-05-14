"""Premium video enhancement via FFmpeg.

Pipeline applied per video:

1. **Color-grade filter preset** — chosen per ``filter_style`` env var.
   "random" (default) picks a different preset every post for variety
   (extra humanization on top of the scheduler's jitter).

   Available presets:
     - ``natural``   : gentle contrast + saturation + sharpening (subtle).
     - ``vivid``     : punchy colors, higher saturation + contrast.
     - ``sunset``    : warm golden-hour cast (boosted reds/yellows).
     - ``cinematic`` : teal-orange grade, soft contrast bump.
     - ``cool``      : crisp blue tone, slight desat in shadows.
     - ``warm``      : soft warm tint, friendly.

2. **Sharpening** — light unsharp mask (built into each preset).

3. **Watermark** — text overlay in the **top-right corner**, which is
   the safest zone on Facebook Reels:
       - bottom-right has the like/comment/share action buttons,
       - bottom-left has the page name and caption overlay,
       - top-left often has the "Reels" header / username overlay,
       - top-right is consistently free.
   Watermark is white text with a soft black shadow for legibility on
   any background, sized ~3% of frame height, opacity ~55% — visible
   but not intrusive.

4. **Audio loudness normalization** (EBU R128, -14 LUFS — broadcast /
   social-media standard).

5. **Container**: H.264 high-profile, yuv420p, faststart, 160k AAC.
   Original resolution / fps / aspect ratio preserved (good for 9:16
   reels at 1080x1920 or any other input size).
"""
from __future__ import annotations
import os
import random
import subprocess


# --- Color grading presets ----------------------------------------------

FILTER_PRESETS: dict[str, str] = {
    "natural": (
        "eq=contrast=1.06:saturation=1.10:brightness=0.01:gamma=1.02,"
        "unsharp=5:5:0.6:5:5:0.0"
    ),
    "vivid": (
        "eq=contrast=1.12:saturation=1.30:brightness=0.02:gamma=1.05,"
        "unsharp=5:5:0.8:5:5:0.0"
    ),
    "sunset": (
        "eq=contrast=1.08:saturation=1.22:gamma=1.03,"
        "colorbalance=rs=0.12:gs=-0.02:bs=-0.12:rm=0.05:bm=-0.05,"
        "unsharp=5:5:0.6:5:5:0.0"
    ),
    "cinematic": (
        "eq=contrast=1.10:saturation=1.05:gamma=1.02,"
        "colorchannelmixer=rr=1.05:gg=1.00:bb=0.95:aa=1,"
        "unsharp=5:5:0.6:5:5:0.0"
    ),
    "cool": (
        "eq=contrast=1.06:saturation=1.12:gamma=1.02,"
        "colorbalance=rs=-0.06:bs=0.10,"
        "unsharp=5:5:0.6:5:5:0.0"
    ),
    "warm": (
        "eq=contrast=1.06:saturation=1.12:gamma=1.02,"
        "colorbalance=rs=0.10:bs=-0.05,"
        "unsharp=5:5:0.6:5:5:0.0"
    ),
}

AUDIO_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"

# Default font locations per OS. CI runs on Ubuntu; DejaVu Sans Bold
# ships with the standard image. Windows uses Arial Bold.
DEFAULT_FONT_LINUX = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DEFAULT_FONT_WIN = "C:/Windows/Fonts/arialbd.ttf"


def _pick_style(style: str) -> tuple[str, str]:
    """Resolve ``style`` (possibly "random") to (style_name, filter_str)."""
    style = (style or "").strip().lower()
    if style == "random" or not style:
        name = random.choice(list(FILTER_PRESETS.keys()))
    elif style in FILTER_PRESETS:
        name = style
    else:
        print(f"[enhance] WARN: unknown FILTER_STYLE={style!r}; falling back to 'natural'")
        name = "natural"
    return name, FILTER_PRESETS[name]


def _ffmpeg_escape(text: str) -> str:
    """Escape characters that have meaning inside an ffmpeg filter argument.

    The filtergraph parser treats ``\\``, ``:``, ``'``, ``%`` and ``,`` as
    metacharacters. ``drawtext`` additionally needs ``{`` ``}`` escaped.
    """
    out = text
    out = out.replace("\\", "\\\\")
    out = out.replace(":", "\\:")
    out = out.replace("'", "\\'")
    out = out.replace("%", "\\%")
    out = out.replace(",", "\\,")
    return out


def _watermark_filter(text: str, font_file: str) -> str:
    """Build a ``drawtext`` clause that places ``text`` in the top-right
    corner of the frame with a soft shadow."""
    safe_text = _ffmpeg_escape(text)
    # NB: fontfile path can usually be passed unescaped on Linux. Wrap in
    # single quotes to be safe with any unusual chars.
    return (
        f"drawtext="
        f"text='{safe_text}':"
        f"fontfile='{font_file}':"
        f"fontcolor=white@0.55:"
        f"fontsize=h*0.032:"
        f"x=w-tw-(w*0.045):"
        f"y=h*0.045:"
        f"shadowcolor=black@0.5:"
        f"shadowx=2:"
        f"shadowy=2"
    )


def _default_font() -> str:
    return DEFAULT_FONT_LINUX if os.name == "posix" else DEFAULT_FONT_WIN


def enhance(
    src: str,
    dst: str,
    *,
    watermark_text: str = "",
    filter_style: str = "random",
    font_file: str = "",
) -> str:
    """Run ffmpeg with color grade + optional watermark + audio normalize.

    Returns ``dst`` on success; raises ``subprocess.CalledProcessError`` on
    ffmpeg failure.
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)

    style_name, style_filter = _pick_style(filter_style)
    parts = [style_filter]

    wm_used = False
    if watermark_text:
        font = font_file or _default_font()
        if os.path.exists(font):
            parts.append(_watermark_filter(watermark_text, font))
            wm_used = True
        else:
            print(f"[enhance] WARN: font not found at {font}; skipping watermark")

    vf = ",".join(parts)
    print(f"[enhance] style={style_name} watermark={'on' if wm_used else 'off'}")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vf", vf,
        "-af", AUDIO_FILTER,
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
        "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-movflags", "+faststart",
        dst,
    ]
    subprocess.run(cmd, check=True)
    return dst
