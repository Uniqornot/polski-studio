"""ASS cards use the audio sample timeline, with safe measured Unicode wrapping."""
from pathlib import Path
import subprocess
from PIL import ImageFont
from tts import SAMPLE_RATE, probe, ffmpeg
from control import run_process, check
import unicodedata
from functools import lru_cache
import time

VIDEO_FPS = 10

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def wrap_text(text, size, bold=False):
    font = ImageFont.truetype(str(BOLD if bold else FONT), size)
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if font.getlength(candidate) <= 880:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        clusters = []
        for char in word:
            if clusters and unicodedata.combining(char):
                clusters[-1] += char
            else:
                clusters.append(char)
        for char in clusters:
            if font.getlength(line + char) > 880:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines


@lru_cache(maxsize=2048)
def layout(text, language):
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    size = 87 if language == "pl" else 63
    while size >= 18:
        lines = wrap_text(text, size, language == "pl")
        if len(lines) * size * 1.4 <= 440:
            return lines, size
        size -= 2
    raise ValueError("Text does not fit a card")


def preflight(pairs):
    errors = []
    for number,pair in enumerate(pairs,1):
        for language in ('pl','ru'):
            try:
                layout(pair[language],language)
            except ValueError:
                errors.append(dict(line=number,text=pair[language],message='Текст не помещается на карточке; сократите фразу'))
    return errors


def ass_text(lines):
    # Neutralize ASS override syntax in untrusted input. Keep wrapping generated here.
    return r"\N".join(line.replace("\\", "＼").replace("{", "｛").replace("}", "｝") for line in lines)


def timestamp(seconds):
    centiseconds = round(seconds * 100)
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    sec, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{sec:02d}.{cs:02d}"


def create_subtitles(pairs, report, path):
    if not FONT.exists() or not BOLD.exists():
        raise RuntimeError("Install system fonts-dejavu-core")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Card,DejaVu Sans,84,&H00FFFFFF,&H00FFFFFF,&H00120D09,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,100,100,100,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    intervals = []
    pair, active = 1, "pl"
    for entry in report["timeline"]:
        if entry["kind"] == "speech":
            pair, active = entry["pair"], entry["language"]
        start, end = entry["start_frame"] / SAMPLE_RATE, entry["end_frame"] / SAMPLE_RATE
        if intervals and intervals[-1][2:] == [pair, active]:
            intervals[-1][1] = end
        else:
            intervals.append([start, end, pair, active])
    intervals[-1][1] = report["duration_seconds"]
    events = []
    for start, end, pair, active in intervals:
        for language in ("pl", "ru"):
            lines, size = layout(pairs[pair - 1][language], language)
            y = 740 if language == "pl" else 1230
            color = "&HD7F39C&" if language == "pl" and active == language else "&HFFFFFF&" if active == language else "&H89776A&"
            tags = f"{{\\an5\\pos(540,{y})\\fs{size}\\b{1 if language == 'pl' else 0}\\c{color}}}"
            events.append(f"Dialogue: 0,{timestamp(start)},{timestamp(end)},Card,,0,0,0,,{tags}{ass_text(lines)}\n")
    path.write_text(header + "".join(events), encoding="utf-8")


def render_video(directory, duration):
    started = time.monotonic()
    # Constant generated filenames; user text enters only the UTF-8 ASS file.
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y", "-f", "lavfi", "-i", f"color=c=0x0b1220:s=1080x1920:r={VIDEO_FPS}",
               "-i", "audio.mp3", "-vf", "ass=subtitles.ass", "-t", f"{duration:.3f}",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-maxrate", "3M", "-bufsize", "6M",
               "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-movflags", "+faststart", "-map", "0:v:0", "-map", "1:a:0", "video.partial.mp4"]
    run_process(command,cwd=directory)
    temporary = directory / "video.partial.mp4"
    details = probe(temporary)
    video = next(s for s in details["streams"] if s["codec_type"] == "video")
    audio = next(s for s in details["streams"] if s["codec_type"] == "audio")
    if (video["width"], video["height"], video["codec_name"], video["pix_fmt"], video["r_frame_rate"], audio["codec_name"]) != (1080, 1920, "h264", "yuv420p", f"{VIDEO_FPS}/1", "aac"):
        raise ValueError("Unexpected video encoding")
    ffmpeg("-i", temporary, "-f", "null", "-")
    temporary.replace(directory / "video.mp4")
    return {"duration_seconds": float(details["format"]["duration"]), "size_bytes": (directory / "video.mp4").stat().st_size,
            "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac", "pixel_format": "yuv420p", "fps": VIDEO_FPS, "decode": "PASS", "render_seconds": time.monotonic()-started}
