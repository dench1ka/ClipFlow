import math
import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InputMediaDocument, Message
import yt_dlp

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

TG_MAX_BYTES = 2 * 1024 * 1024 * 1024        # Telegram hard limit
PART_TARGET_BYTES = 1_900 * 1024 * 1024      # target per part (safety margin)

TWITCH_VOD_PATTERN = re.compile(
    r"https?://(?:www\.)?twitch\.tv/videos/(\d+)"
)
TWITCH_CLIP_PATTERN = re.compile(
    r"https?://(?:clips\.twitch\.tv/|(?:www\.)?twitch\.tv/[^/]+/clip/)([A-Za-z0-9_-]+)"
)
YOUTUBE_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/watch\?(?:[^&\s]*&)*v=|youtu\.be/)([A-Za-z0-9_-]{11})"
)

QUALITY_LADDERS = {
    "twitch_clip": [
        "best[format_id!*=portrait]",
        "worst[format_id!*=portrait]/worst",
    ],
    "twitch_vod": [
        "best",
        "worst",
    ],
    "youtube": [
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]",
        "worst",
    ],
}


def detect_url(text: str) -> tuple[str | None, str | None]:
    if m := TWITCH_VOD_PATTERN.search(text):
        return "twitch_vod", f"https://www.twitch.tv/videos/{m.group(1)}"
    if TWITCH_CLIP_PATTERN.search(text):
        parsed = urlparse(text)
        clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return "twitch_clip", clean
    if m := YOUTUBE_PATTERN.search(text):
        return "youtube", f"https://www.youtube.com/watch?v={m.group(1)}"
    return None, None


async def _run(func):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func)


async def get_info(url: str) -> dict:
    def _info():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)

    return await _run(_info)


async def download_video(url: str, output_dir: str, fmt: str) -> str:
    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "video.%(ext)s"),
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                path = str(Path(path).with_suffix(".mp4"))
            return path

    return await _run(_download)


def find_file(directory: str) -> str | None:
    for ext in ("mp4", "mkv", "webm", "mov", "ts"):
        files = list(Path(directory).glob(f"*.{ext}"))
        if files:
            return str(files[0])
    files = [f for f in Path(directory).glob("*") if f.is_file()]
    return str(files[0]) if files else None


async def _probe_duration(filepath: str) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except (ValueError, AttributeError):
        return None


async def split_into_parts(filepath: str, tmpdir: str) -> list[str]:
    """Split file into ~1.9 GB segments. Returns [filepath] if no split needed."""
    file_size = os.path.getsize(filepath)
    if file_size <= TG_MAX_BYTES:
        return [filepath]

    duration = await _probe_duration(filepath)
    if not duration:
        logger.warning("Cannot probe duration, skipping split")
        return [filepath]

    num_parts = math.ceil(file_size / PART_TARGET_BYTES)
    seg_secs = math.ceil(duration / num_parts)

    pattern = os.path.join(tmpdir, "part_%03d.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", filepath,
        "-c", "copy", "-map", "0",
        "-f", "segment",
        "-segment_time", str(seg_secs),
        "-reset_timestamps", "1",
        pattern, "-y",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    parts = sorted(Path(tmpdir).glob("part_*.mp4"))
    if len(parts) < 2:
        return [filepath]

    logger.info("Split into %d parts (seg=%ds)", len(parts), seg_secs)
    return [str(p) for p in parts]


async def send_parts(
    message: Message,
    status_msg: Message,
    parts: list[str],
    title: str,
    uploader: str,
    duration_str: str,
    quality_label: str | None,
) -> None:
    total = len(parts)
    base_caption = f"<b>{title}</b>\n{uploader} • {duration_str}"
    if quality_label:
        base_caption += f"\nКачество: {quality_label}"

    # Send in batches of 10 (Telegram media group limit)
    for batch_start in range(0, total, 10):
        batch = parts[batch_start : batch_start + 10]
        batch_end = batch_start + len(batch)

        await status_msg.edit_text(
            f"Отправляю части {batch_start + 1}–{batch_end} из {total}...\n{title}"
        )

        media = []
        for i, path in enumerate(batch):
            part_num = batch_start + i + 1
            size_mb = os.path.getsize(path) / 1024 / 1024
            if i == 0:
                cap = f"{base_caption}\n\nЧасть {part_num} из {total} • {size_mb:.0f} MB"
            else:
                cap = f"Часть {part_num} из {total} • {size_mb:.0f} MB"
            media.append(InputMediaDocument(media=path, caption=cap, parse_mode="html"))

        await message.reply_media_group(media=media)


def _fmt_duration(seconds) -> str:
    if not seconds:
        return "—"
    h, m, s = int(seconds) // 3600, int(seconds) % 3600 // 60, int(seconds) % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_label(fmt: str) -> str:
    for q in ("1080", "720", "480", "360"):
        if q in fmt:
            return f"{q}p"
    if "worst" in fmt:
        return "минимальное качество"
    return "лучшее качество"


app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@app.on_message(filters.command("start"))
async def start(_: Client, message: Message) -> None:
    await message.reply_text(
        "Привет! Отправь мне ссылку на видео.\n\n"
        "Поддерживаемые платформы:\n"
        "• Twitch клипы — clips.twitch.tv/...\n"
        "• Twitch стримы (VOD) — twitch.tv/videos/...\n"
        "• YouTube — youtube.com/watch?v=... или youtu.be/...\n\n"
        "Если видео больше 2 GB — разобью на части и пришлю всё."
    )


@app.on_message(filters.text & ~filters.regex(r"^/"))
async def handle_message(_: Client, message: Message) -> None:
    text = message.text.strip()
    platform, url = detect_url(text)

    if not platform:
        await message.reply_text(
            "Это не похоже на поддерживаемую ссылку.\n\n"
            "Поддерживаются:\n"
            "• Twitch клипы: https://clips.twitch.tv/...\n"
            "• Twitch стримы: https://twitch.tv/videos/...\n"
            "• YouTube: https://youtube.com/watch?v=..."
        )
        return

    status_msg = await message.reply_text("Получаю информацию о видео...")

    try:
        info = await get_info(url)
    except Exception as e:
        logger.error("Failed to fetch info: %s", e)
        await status_msg.edit_text(f"Не удалось получить информацию:\n{e}")
        return

    title = info.get("title", "Без названия")
    duration = info.get("duration", 0)
    uploader = info.get("uploader", "Неизвестно")
    duration_str = _fmt_duration(duration)

    quality_ladder = QUALITY_LADDERS[platform]

    for attempt, fmt in enumerate(quality_ladder):
        if attempt == 0:
            await status_msg.edit_text(
                f"Скачиваю...\n\n"
                f"Название: {title}\n"
                f"Канал: {uploader}\n"
                f"Длительность: {duration_str}"
            )
        else:
            await status_msg.edit_text(f"Пробую {_fmt_label(fmt)}...\n\n{title}")

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                filepath = await download_video(url, tmpdir, fmt)
            except Exception as e:
                logger.error("Download failed (fmt=%s): %s", fmt, e)
                if attempt == len(quality_ladder) - 1:
                    await status_msg.edit_text(f"Ошибка при скачивании:\n{e}")
                    return
                continue

            if not os.path.exists(filepath):
                filepath = find_file(tmpdir)

            if not filepath:
                await status_msg.edit_text("Файл не найден после скачивания.")
                return

            file_size = os.path.getsize(filepath)
            size_mb = file_size / 1024 / 1024
            quality_label = _fmt_label(fmt) if attempt > 0 else None

            # Split if file exceeds Telegram's 2 GB limit
            if file_size > TG_MAX_BYTES:
                await status_msg.edit_text(
                    f"Файл {size_mb:.0f} MB — разбиваю на части...\n{title}"
                )
                parts = await split_into_parts(filepath, tmpdir)
            else:
                parts = [filepath]

            if len(parts) > 1:
                try:
                    await send_parts(
                        message, status_msg, parts,
                        title, uploader, duration_str, quality_label,
                    )
                    await status_msg.delete()
                except Exception as e:
                    logger.error("Failed to send parts: %s", e)
                    await status_msg.edit_text(f"Ошибка при отправке частей:\n{e}")
                return

            # Single file upload with progress
            caption = f"<b>{title}</b>\n{uploader} • {duration_str}"
            if quality_label:
                caption += f"\nКачество: {quality_label}"

            await status_msg.edit_text(f"Отправляю видео ({size_mb:.1f} MB)...")

            last_step = [-1]

            async def progress(current, total):
                if not total:
                    return
                step = int(current / total * 10)
                if step != last_step[0]:
                    last_step[0] = step
                    sent = current / 1024 / 1024
                    try:
                        await status_msg.edit_text(
                            f"Отправляю... {step * 10}%\n{sent:.0f} / {size_mb:.0f} MB"
                        )
                    except Exception:
                        pass

            try:
                await message.reply_document(
                    document=filepath,
                    caption=caption,
                    parse_mode="html",
                    progress=progress,
                )
                await status_msg.delete()
                return
            except Exception as e:
                logger.error("Failed to send video: %s", e)
                await status_msg.edit_text(f"Ошибка при отправке:\n{e}")
                return


if __name__ == "__main__":
    missing = [v for v in ("API_ID", "API_HASH", "BOT_TOKEN") if not os.getenv(v)]
    if missing:
        raise ValueError(f"Не заданы в .env: {', '.join(missing)}")
    app.run()
