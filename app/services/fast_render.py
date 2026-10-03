import os
import re
import shutil
import json
import subprocess
from loguru import logger
from app.utils import utils

def get_ffmpeg_binary() -> str:
    """Find the best available ffmpeg binary."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"

def hex_to_ass_color(hex_str: str, default: str = "&H000DDDDE") -> str:
    """Convert hex color (#RRGGBB) to ASS/SSA format (&HAABBGGRR)."""
    if not hex_str:
        return default
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 6:
        r, g, b = clean[0:2], clean[2:4], clean[4:6]
        return f"&H00{b}{g}{r}".upper()
    return default

def srt_time_to_ass(srt_time: str) -> str:
    """Convert SRT time '00:01:23,456' to ASS time '0:01:23.46'."""
    match = re.match(r"(\d+):(\d+):(\d+)[,\.](\d+)", srt_time.strip())
    if not match:
        return "0:00:00.00"
    h, m, s, ms = match.groups()
    hours = int(h)
    mins = int(m)
    secs = int(s)
    cs = round(int(ms[:3].ljust(3, "0")) / 10.0)
    if cs >= 100:
        cs = 99
    return f"{hours}:{mins:02d}:{secs:02d}.{cs:02d}"


def convert_srt_to_ass(
    srt_path: str,
    ass_path: str,
    video_width: int,
    video_height: int,
    font_name: str,
    font_size: int,
    text_color: str,
    outline_color: str,
    outline_width: float,
    position: str = "bottom",
) -> bool:
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            srt_content = f.read()

        blocks = re.split(r"\n\s*\n", srt_content.strip())
        dialogues = []
        for b in blocks:
            lines = [l.strip() for l in b.strip().split("\n") if l.strip()]
            if len(lines) >= 2:
                time_line = lines[1] if re.match(r"^\d+$", lines[0]) else lines[0]
                text_lines = lines[2:] if re.match(r"^\d+$", lines[0]) else lines[1:]
                time_match = re.search(r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", time_line)
                if time_match and text_lines:
                    start_ass = srt_time_to_ass(time_match.group(1))
                    end_ass = srt_time_to_ass(time_match.group(2))
                    text = "\\N".join(text_lines)
                    dialogues.append(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text}")

        primary_ass = hex_to_ass_color(text_color, "&H000DDDDE")
        outline_ass = hex_to_ass_color(outline_color, "&H00000000")

        pos_lower = position.lower()
        if pos_lower == "top":
            align = 8
            margin_v = int(video_height * 0.08)
        elif pos_lower == "center":
            align = 5
            margin_v = 0
        elif pos_lower in ("two_thirds_bottom", "two-thirds", "two_thirds", "2/3_bottom", "2/3"):
            align = 8
            margin_v = int(video_height * 0.33)
        else:  # bottom
            align = 2
            margin_v = int(video_height * 0.10)

        margin_lr = int(video_width * 0.07)

        # Normalize font size to video resolution
        scaled_font_size = int(font_size)
        if scaled_font_size <= 28:
            scaled_font_size = 55
        scaled_font_size = max(20, min(int(video_height * 0.08), scaled_font_size))

        base_dim = min(video_width, video_height)
        scaled_outline = max(1.0, float(outline_width) * (base_dim / 720.0))

        ass_content = f"""[Script Info]
Title: MoneyPrinterTurbo Fast Restyle
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {video_width}
PlayResY: {video_height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{scaled_font_size},{primary_ass},&H000000FF,{outline_ass},&H00000000,1,0,0,0,100,100,0,0,1,{scaled_outline:.1f},1.5,{align},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(dialogues) + "\n"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        return True
    except Exception as e:
        logger.warning(f"failed to convert srt to ass: {e}")
        return False


def fast_restyle_task(
    task_path: str,
    font_name: str = "Noto Sans Devanagari",
    font_size: int = 55,
    text_color: str = "#ddde0d",
    outline_color: str = "#000000",
    outline_width: float = 2.5,
    position: str = "bottom",
    updated_subtitles_text: str | None = None,
    voice_name: str | None = None,
    voice_rate: float | None = None,
    video_file: str | None = None,
) -> tuple[bool, str]:
    """
    Fast hardware-accelerated re-rendering of final video with updated subtitles and voice.
    Takes existing combined-1.mp4 and audio.mp3, burns updated subtitles via multi-threaded
    FFmpeg, and updates final-1.mp4 in ~30s.
    """
    import time
    if not os.path.isdir(task_path):
        return False, f"Task directory not found: {task_path}"

    combined_mp4 = os.path.join(task_path, "combined-1.mp4")
    if not os.path.isfile(combined_mp4):
        candidates = [
            os.path.join(task_path, f)
            for f in os.listdir(task_path)
            if f.startswith("combined") and f.endswith(".mp4")
        ]
        if candidates:
            combined_mp4 = sorted(candidates)[0]
        else:
            return False, "combined-1.mp4 not found. Full render required first."

    audio_mp3 = os.path.join(task_path, "audio.mp3")
    subtitle_srt = os.path.join(task_path, "subtitle.srt")
    final_mp4 = video_file if (video_file and os.path.isfile(video_file)) else os.path.join(task_path, "final-1.mp4")

    if not os.path.isfile(audio_mp3):
        return False, "audio.mp3 not found."
    if not os.path.isfile(subtitle_srt):
        return False, "subtitle.srt not found."

    # Update subtitle.srt if user edited it
    if updated_subtitles_text is not None and updated_subtitles_text.strip():
        try:
            with open(subtitle_srt, "w", encoding="utf-8") as f:
                f.write(updated_subtitles_text.strip() + "\n")
        except Exception as e:
            logger.warning(f"failed to update subtitle.srt: {e}")

    # Resolve font family
    font_family = font_name
    if font_name.endswith(".ttf") or font_name.endswith(".ttc"):
        font_family = os.path.splitext(font_name)[0]
    
    # Handle known font mappings
    if "nirmala" in font_family.lower():
        font_family = "Nirmala"
    elif "noto" in font_family.lower() and "deva" in font_family.lower():
        font_family = "Noto Sans Devanagari"
    elif "yahei" in font_family.lower():
        font_family = "Microsoft YaHei"

    fonts_dir = utils.font_dir()
    escaped_fonts_dir = fonts_dir.replace("\\", "/").replace(":", "\\:")
    escaped_sub = subtitle_srt.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    primary_ass = hex_to_ass_color(text_color, "&H000DDDDE")
    outline_ass = hex_to_ass_color(outline_color, "&H00000000")

    # Alignment and margins fallback
    pos_lower = position.lower()
    if pos_lower == "top":
        align = 8
        margin_v = 35
    elif pos_lower == "center":
        align = 5
        margin_v = 10
    elif pos_lower in ("two_thirds_bottom", "two-thirds", "2/3"):
        align = 2
        margin_v = 120
    else:  # bottom
        align = 2
        margin_v = 35

    # Safe font size and outline for fallback
    safe_font_size = max(16, min(40, int(font_size)))
    safe_outline = max(0.0, min(8.0, float(outline_width)))

    # Determine video dimensions and duration
    video_width = 1080
    video_height = 1920
    video_duration = 0.0
    try:
        from moviepy import VideoFileClip
        with VideoFileClip(combined_mp4) as clip:
            video_duration = float(clip.duration or 0.0)
            if clip.w and clip.h:
                video_width = int(clip.w)
                video_height = int(clip.h)
    except Exception:
        pass

    # Convert SRT to ASS with exact video resolution for pixel-perfect typography
    ass_file = os.path.join(task_path, "restyle_subtitles.ass")
    ass_converted = convert_srt_to_ass(
        srt_path=subtitle_srt,
        ass_path=ass_file,
        video_width=video_width,
        video_height=video_height,
        font_name=font_family,
        font_size=font_size,
        text_color=text_color,
        outline_color=outline_color,
        outline_width=outline_width,
        position=position,
    )

    if ass_converted and os.path.isfile(ass_file):
        escaped_ass = ass_file.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        sub_filter = f",subtitles='{escaped_ass}':fontsdir='{escaped_fonts_dir}'"
    else:
        # Fallback to SRT if ASS conversion fails
        sub_filter = (
            f",subtitles='{escaped_sub}':fontsdir='{escaped_fonts_dir}':force_style='"
            f"FontName={font_family},FontSize={safe_font_size},Bold=1,"
            f"PrimaryColour={primary_ass},OutlineColour={outline_ass},"
            f"BorderStyle=1,Outline={safe_outline},Shadow=1,Alignment={align},MarginV={margin_v}'"
        )

    temp_final = os.path.join(task_path, "final-restyle-temp.mp4")
    ffmpeg_bin = get_ffmpeg_binary()

    # Determine audio and video duration to avoid infinite stream looping
    import math
    audio_duration = 0.0
    try:
        from app.services import voice
        audio_duration = float(voice.get_audio_duration(audio_mp3) or 0.0)
    except Exception:
        pass

    # Finite loop only if audio is strictly longer than video
    loop_args = []
    if audio_duration > 0 and video_duration > 0 and audio_duration > video_duration:
        loops = int(math.ceil(audio_duration / video_duration)) - 1
        if loops > 0:
            loop_args = ["-stream_loop", str(loops)]

    # Hard stop duration guarantees termination without hanging or infinite loops
    duration_args = ["-t", f"{audio_duration:.2f}"] if audio_duration > 0 else ["-shortest"]

    cmd = [
        ffmpeg_bin, "-y",
        *loop_args,
        "-i", combined_mp4,
        "-i", audio_mp3,
        "-filter_complex", f"[0:v]format=yuv420p{sub_filter}[v];[1:a]volume=1.0[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-threads", "0",
        "-c:a", "aac", "-b:a", "192k",
        *duration_args,
        "-movflags", "+faststart",
        temp_final
    ]

    logger.info(f"Running fast restyle FFmpeg for {task_path} (audio_dur={audio_duration:.2f}s, video_dur={video_duration:.2f}s)...")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            logger.error(f"FFmpeg fast restyle error: {res.stderr[-500:]}")
            return False, f"FFmpeg failed: {res.stderr[-200:]}"
    except Exception as e:
        logger.exception(f"FFmpeg execution failed: {e}")
        return False, str(e)

    if not os.path.isfile(temp_final) or os.path.getsize(temp_final) < 10000:
        return False, "Rendered file is missing or invalid."

    # Atomically replace final file with retry and copy fallback
    replaced = False
    last_err = ""
    for attempt in range(6):
        try:
            if os.path.isfile(final_mp4):
                try:
                    os.remove(final_mp4)
                except Exception:
                    pass
            os.replace(temp_final, final_mp4)
            replaced = True
            break
        except Exception as err:
            last_err = str(err)
            time.sleep(0.3)

    if not replaced:
        try:
            shutil.copyfile(temp_final, final_mp4)
            if os.path.isfile(temp_final):
                try:
                    os.remove(temp_final)
                except Exception:
                    pass
            replaced = True
        except Exception as e2:
            return False, f"Failed to update final video file: {e2 or last_err}"

    # Update script.json params with new subtitle styling and voice
    script_file = os.path.join(task_path, "script.json")
    if os.path.isfile(script_file):
        try:
            with open(script_file, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            params = sdata.setdefault("params", {})
            params["font_name"] = font_name
            params["font_size"] = safe_font_size
            params["text_fore_color"] = text_color
            params["stroke_color"] = outline_color
            params["stroke_width"] = safe_outline
            params["subtitle_position"] = position
            if voice_name:
                params["voice_name"] = voice_name
            if voice_rate is not None:
                params["voice_rate"] = float(voice_rate)
            with open(script_file, "w", encoding="utf-8") as f:
                json.dump(sdata, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"failed to update script.json: {e}")

    # If running inside Modal container, commit volume
    try:
        import modal
        vol = modal.Volume.from_name("bangai-storage")
        vol.commit()
    except Exception:
        pass

    return True, "Success"
