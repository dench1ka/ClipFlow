import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_TG_BYTES = 50 * 1024 * 1024  # Telegram Bot API hard limit

# Quality ladder: try from best to worst until file fits
QUALITY_LADDER = [
    "best[format_id!*=portrait]",
    "best[format_id!*=portrait][height<=720]",
    "best[format_id!*=portrait][height<=480]",
    "best[format_id!*=portrait][height<=360]",
    "worst[format_id!*=portrait]/worst",
]

TWITCH_CLIP_PATTERN = re.compile(
    r"https?://(?:www\.|clips\.)?twitch\.tv/(?:[^/]+/clip/|clip/)?([A-Za-z0-9_-]+)"
)


def is_twitch_clip(url: str) -> bool:
    return bool(TWITCH_CLIP_PATTERN.search(url))


def clean_url(url: str) -> str:
    """Strip query params and fragments from Twitch clip URL."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


async def _run(func):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func)


async def get_clip_info(url: str) -> dict:
    def _info():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)

    return await _run(_info)


async def download_clip(url: str, output_dir: str, fmt: str = "best") -> str:
    """Download clip with given format string, return file path."""
    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "clip.%(ext)s"),
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
    """Find downloaded file in directory."""
    for ext in ("mp4", "mkv", "webm", "mov"):
        files = list(Path(directory).glob(f"*.{ext}"))
        if files:
            return str(files[0])
    files = list(Path(directory).glob("*"))
    return str(files[0]) if files else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь мне ссылку на клип с Twitch.\n\n"
        "Поддерживаемые форматы:\n"
        "• https://clips.twitch.tv/ClipName\n"
        "• https://www.twitch.tv/channel/clip/ClipName\n\n"
        "Всегда пришлю видеофайл — если нужно, автоматически подберу качество."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if not is_twitch_clip(text):
        await update.message.reply_text(
            "Это не похоже на ссылку на Twitch клип. Отправь ссылку вида:\n"
            "https://clips.twitch.tv/..."
        )
        return

    status_msg = await update.message.reply_text("Получаю информацию о клипе...")

    text = clean_url(text)

    try:
        info = await get_clip_info(text)
    except Exception as e:
        logger.error("Failed to fetch clip info: %s", e)
        await status_msg.edit_text(f"Не удалось получить информацию о клипе:\n{e}")
        return

    title = info.get("title", "Без названия")
    duration = info.get("duration", 0)
    uploader = info.get("uploader", "Неизвестно")

    # Pick dimensions from the best landscape format
    vid_width, vid_height = 1920, 1080
    for f in reversed(info.get("formats", [])):
        if "portrait" not in f.get("format_id", "") and f.get("width") and f.get("height"):
            vid_width, vid_height = f["width"], f["height"]
            break

    for attempt, fmt in enumerate(QUALITY_LADDER):
        quality_label = _fmt_label(fmt)

        if attempt == 0:
            await status_msg.edit_text(
                f"Скачиваю клип...\n\n"
                f"Название: {title}\n"
                f"Канал: {uploader}\n"
                f"Длительность: {int(duration)} сек"
            )
        else:
            await status_msg.edit_text(
                f"Файл не влезает в Telegram, пробую {quality_label}...\n\n"
                f"{title}"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                filepath = await download_clip(text, tmpdir, fmt)
            except Exception as e:
                logger.error("Download failed (fmt=%s): %s", fmt, e)
                if attempt == len(QUALITY_LADDER) - 1:
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

            if file_size > MAX_TG_BYTES:
                if attempt < len(QUALITY_LADDER) - 1:
                    logger.info("File %.1fMB too large, trying lower quality", size_mb)
                    continue
                # Last resort — send whatever we have and warn user
                await status_msg.edit_text(
                    f"Даже на минимальном качестве файл {size_mb:.1f} MB.\n"
                    f"Это очень необычно для Twitch клипа. Попробуй другой клип."
                )
                return

            caption = f"<b>{title}</b>\nКанал: {uploader}"
            if attempt > 0:
                caption += f"\nКачество: {quality_label}"

            await status_msg.edit_text("Отправляю видео...")
            try:
                with open(filepath, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=f"{title}.mp4",
                        caption=caption,
                        parse_mode="HTML",
                        read_timeout=180,
                        write_timeout=180,
                    )
                await status_msg.delete()
                return
            except Exception as e:
                logger.error("Failed to send video: %s", e)
                await status_msg.edit_text(f"Ошибка при отправке видео:\n{e}")
                return


def _fmt_label(fmt: str) -> str:
    if fmt == "best":
        return "лучшее качество"
    if "720" in fmt:
        return "720p"
    if "480" in fmt:
        return "480p"
    if "360" in fmt:
        return "360p"
    return "минимальное качество"


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env файле")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
