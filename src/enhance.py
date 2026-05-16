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


def _probe_duration(path: str) -> float:
    """Return media duration in seconds via ffprobe. Returns 0.0 if unknown
    (caller falls back to fade-less mix). Never raises."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return float(r.stdout.strip() or 0.0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 0.0


def _stealth_params(seed_str: str) -> dict:
    """Return per-video randomized stealth parameters.

    Seeded by ``seed_str`` (typically the source filename) so the same
    video produces the same params across retries — but every *different*
    video gets a unique fingerprint-disrupting transform set.
    """
    rng = random.Random(seed_str)
    return {
        "crop_pct":   rng.uniform(0.06, 0.10),       # 6-10% edge crop
        "rotate_deg": rng.uniform(-0.4, 0.4),        # micro rotation
        "zoom":       rng.uniform(1.01, 1.04),       # subtle zoom-in
        "fps":        rng.choice([29, 30, 31]),      # vary fps
        "crf":        rng.choice([19, 20, 21, 22]),  # vary quality
        "noise":      rng.uniform(1.5, 3.5),         # imperceptible film grain
        "vignette":   rng.uniform(0.10, 0.25),       # soft vignette
        "eq_bright":  rng.uniform(-0.02, 0.03),      # micro brightness jitter
        "eq_contr":   rng.uniform(0.98, 1.04),       # micro contrast jitter
        "eq_satur":   rng.uniform(0.96, 1.06),       # micro saturation jitter
        "eq_hue":     rng.uniform(-4.0, 4.0),        # micro hue shift (deg)
        "speed":      rng.uniform(0.97, 1.03),       # ±3% speed (audio pitch shifts with it)
        "trim_start": rng.uniform(0.05, 0.30),       # trim head
        "trim_end":   rng.uniform(0.05, 0.30),       # trim tail
        "audio_pitch": rng.uniform(0.97, 1.03),      # extra audio pitch tweak
        "gop":        rng.choice([48, 60, 75, 90]),  # vary GOP size
    }


def enhance(
    src: str,
    dst: str,
    *,
    watermark_text: str = "",
    filter_style: str = "random",
    font_file: str = "",
    music_path: str = "",
    music_mix: str = "duck",   # "duck" | "full" | "off"
    music_volume: float = 0.95,
    original_duck_volume: float = 0.18,
) -> str:
    """Stealth-grade FFmpeg pipeline that defeats FB's perceptual-hash,
    audio-fingerprint, and 3rd-party-watermark detectors.

    Layers applied (every video gets a unique seeded combination):

    Video:
      1. Edge crop 6-10%   -> removes TikTok/IG/YT watermarks at any corner
      2. Micro rotation    -> breaks frame-aligned hashing
      3. Subtle zoom       -> defeats spatial fingerprints
      4. Color-grade preset (random)
      5. Micro EQ jitter   -> brightness/contrast/sat/hue drift per video
      6. Soft vignette     -> alters edge luma signature
      7. Imperceptible film grain
      8. Variable fps (29/30/31)
      9. Output: 1080x1920, H.264 high, randomized CRF + GOP
     10. Top-right text watermark (your brand)
     11. Strip ALL source metadata

    Audio:
      1. Pitch shift ±3% (asetrate + atempo)
      2. EBU R128 loudness normalize to -14 LUFS
      3. AAC 160k @ 44.1kHz

    Container:
      - faststart, no source metadata, randomized encoder params

    Returns ``dst`` on success; raises ``subprocess.CalledProcessError``.
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)

    style_name, style_filter = _pick_style(filter_style)
    sp = _stealth_params(os.path.basename(src))

    # --- Video filter chain (order matters) ------------------------------
    crop_keep = 1.0 - sp["crop_pct"]                      # e.g. 0.92
    inner_w = f"iw*{crop_keep:.4f}"
    inner_h = f"ih*{crop_keep:.4f}"
    zoom = sp["zoom"]
    rot_rad = sp["rotate_deg"] * 3.14159265 / 180.0

    normalize = (
        # 1. crop edges (kill 3rd-party watermarks)
        f"crop={inner_w}:{inner_h},"
        # 2. tiny rotation w/ same-color fill
        f"rotate={rot_rad:.5f}:ow=rotw({rot_rad:.5f}):oh=roth({rot_rad:.5f}):c=black,"
        # 3. zoom-in slightly to hide rotation borders
        f"scale=trunc(iw*{zoom:.4f}/2)*2:trunc(ih*{zoom:.4f}/2)*2,"
        # 4. force final 9:16 1080x1920
        f"scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,"
        # 5. lock fps
        f"fps={sp['fps']}"
    )

    micro_eq = (
        f"eq=contrast={sp['eq_contr']:.3f}"
        f":brightness={sp['eq_bright']:.3f}"
        f":saturation={sp['eq_satur']:.3f},"
        f"hue=h={sp['eq_hue']:.2f}"
    )

    vignette = f"vignette=PI/{4.0 + sp['vignette']*4:.2f}"
    grain = f"noise=alls={sp['noise']:.1f}:allf=t"

    parts = [normalize, style_filter, micro_eq, vignette, grain]

    wm_used = False
    if watermark_text:
        font = font_file or _default_font()
        if os.path.exists(font):
            parts.append(_watermark_filter(watermark_text, font))
            wm_used = True
        else:
            print(f"[enhance] WARN: font not found at {font}; skipping watermark")

    vf_chain = ",".join(parts)

    # --- Audio chain ------------------------------------------------------
    pitch = sp["audio_pitch"]
    have_music = bool(music_path) and os.path.exists(music_path) and music_mix != "off"
    music_mode = music_mix if have_music else "none"

    print(
        f"[enhance] style={style_name} watermark={'on' if wm_used else 'off'} "
        f"crop={sp['crop_pct']*100:.1f}% rot={sp['rotate_deg']:.2f}deg "
        f"zoom={sp['zoom']:.3f} fps={sp['fps']} crf={sp['crf']} "
        f"pitch={pitch:.3f} music={music_mode}"
    )

    base_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
    ]

    if have_music:
        # We use filter_complex so we can both video-filter and mix audio
        # from two sources in one pass.
        base_cmd += ["-stream_loop", "-1", "-i", music_path]

        # Professional fade-in/out on the music track so the soundtrack
        # never starts or ends abruptly. Fade durations are short enough
        # to feel natural (similar to FB's built-in music composer).
        fade_in = 1.5
        fade_out = 2.5
        video_duration = _probe_duration(src)
        # Trim music to video length, then fade in at 0, fade out at the tail.
        if video_duration > (fade_in + fade_out + 1.0):
            fade_out_start = max(0.0, video_duration - fade_out)
            music_chain = (
                f"[1:a]aresample=44100,volume={music_volume:.3f},"
                f"atrim=duration={video_duration:.3f},"
                f"afade=t=in:st=0:d={fade_in:.2f},"
                f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.2f}[mus]"
            )
        else:
            # Very short video or duration unknown: skip fades to avoid
            # silencing the whole track.
            music_chain = (
                f"[1:a]aresample=44100,volume={music_volume:.3f}[mus]"
            )

        if music_mix == "full":
            # Drop original audio entirely; use only the music track.
            filter_complex = (
                f"[0:v]{vf_chain}[vout];"
                f"{music_chain};"
                f"[mus]{AUDIO_FILTER}[aout]"
            )
        else:
            # duck-mix: keep original ambience faintly under the music
            filter_complex = (
                f"[0:v]{vf_chain}[vout];"
                f"[0:a]asetrate=44100*{pitch:.4f},aresample=44100,"
                f"atempo={1/pitch:.4f},volume={original_duck_volume:.3f}[orig];"
                f"{music_chain};"
                f"[orig][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];"
                f"[mix]{AUDIO_FILTER}[aout]"
            )

        cmd = base_cmd + [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-shortest",
            "-map_metadata", "-1",
            "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
            "-crf", str(sp["crf"]), "-pix_fmt", "yuv420p",
            "-g", str(sp["gop"]),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-movflags", "+faststart",
            dst,
        ]
    else:
        # Legacy single-input path (no music): use -vf / -af.
        af = (
            f"asetrate=44100*{pitch:.4f},"
            f"aresample=44100,"
            f"atempo={1/pitch:.4f},"
            f"{AUDIO_FILTER}"
        )
        cmd = base_cmd + [
            "-vf", vf_chain,
            "-af", af,
            "-map_metadata", "-1",
            "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
            "-crf", str(sp["crf"]), "-pix_fmt", "yuv420p",
            "-g", str(sp["gop"]),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-movflags", "+faststart",
            dst,
        ]

    subprocess.run(cmd, check=True)
    return dst
