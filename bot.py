"""
Telegram music bot with two features:

  1. Download mode ("يوت"):
     Anyone writes "يوت <song name>" in a group or private chat, and the bot
     replies with the song as a playable audio file (caption: تم التحميل بنجاح ✅).

  2. Voice-chat mode ("تشغيل"):
     In a group with an active voice chat, anyone writes "تشغيل <song name>"
     and an assistant user account joins the voice chat and streams the song.
     Extra commands: "تخطي" (skip) and "إيقاف" (stop and clear the queue).

Architecture:
  - One Pyrogram client logged in with the bot token handles all messages.
  - A second Pyrogram client (the assistant, logged in with a session string
    from a regular Telegram account) plus pytgcalls streams audio into the
    group voice chat.

Environment variables:
  BOT_TOKEN       - token from @BotFather (required)
  API_ID          - from https://my.telegram.org (required)
  API_HASH        - from https://my.telegram.org (required)
  SESSION_STRING  - session string of the assistant account, generated with
                    gen_session.py (required for voice-chat mode only;
                    without it the bot runs in download-only mode)
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, StreamEnded

# ---------------------------------------------------------------------------
# .env loading (must run BEFORE the settings below are read)
# ---------------------------------------------------------------------------

KNOWN_ENV_KEYS = {"BOT_TOKEN", "API_ID", "API_HASH", "SESSION_STRING"}


def load_env_file() -> None:
    """Populate os.environ from a .env file sitting next to this script.

    Tolerant of Windows quirks: a UTF-8 BOM, a UTF-16 file saved by Notepad,
    a file accidentally saved as ".env.txt" by Explorer hiding extensions,
    spaces around the "=", quoted values, and invisible characters in keys.
    Values already present in the real environment win.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, ".env"),
        os.path.join(script_dir, ".env.txt"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        print(f"No .env file found next to bot.py in {script_dir}")
        return

    with open(path, "rb") as f:
        raw = f.read()

    # Detect the encoding: UTF-16 files start with a BOM (ff fe / fe ff) or
    # contain NUL bytes; anything else is treated as UTF-8 (BOM stripped).
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:200]:
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")

    loaded = 0
    for line in text.splitlines():
        line = line.strip().strip("\x00").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Normalize the key: strip whitespace, NULs, BOM and zero-width chars.
        key = key.strip().strip("\x00\ufeff\u200b\u200e\u200f").strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        # Match our known setting names case-insensitively.
        if key.upper() in KNOWN_ENV_KEYS:
            key = key.upper()
        os.environ.setdefault(key, value)
        loaded += 1
    print(f"Loaded {loaded} setting(s) from {path}")


load_env_file()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")

# Telegram bots cannot upload files larger than this (bytes).
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 MB, a small safety margin under 50 MB

# Triggers: a message that starts with the trigger word followed by the song name.
DOWNLOAD_RE = re.compile(r"^\s*يوت\s+(.+?)\s*$", re.DOTALL)
PLAY_RE = re.compile(r"^\s*تشغيل\s+(.+?)\s*$", re.DOTALL)
SKIP_RE = re.compile(r"^\s*تخطي\s*$")
STOP_RE = re.compile(r"^\s*(إيقاف|ايقاف)\s*$")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

bot = Client(
    "music_bot",
    api_id=int(API_ID) if API_ID else None,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

VOICE_ENABLED = bool(SESSION_STRING)
assistant = None
calls = None
if VOICE_ENABLED:
    assistant = Client(
        "assistant",
        api_id=int(API_ID),
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )
    calls = PyTgCalls(assistant)

# ---------------------------------------------------------------------------
# Voice-chat playback state (per group chat)
# ---------------------------------------------------------------------------
# queues[chat_id]   : deque of track dicts waiting to play
# current[chat_id]  : the track dict currently streaming

queues = defaultdict(deque)
current = {}


# ---------------------------------------------------------------------------
# YouTube search + download (blocking, runs in a worker thread)
# ---------------------------------------------------------------------------

def search_and_download(query: str, workdir: str) -> dict:
    """Search YouTube for `query`, download the top result as mp3.

    Returns a dict with: path, workdir, title, performer, duration, query.
    Raises ValueError("not_found") when YouTube has no results.
    """
    output_template = os.path.join(workdir, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch1",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=True)

    if not info or not info.get("entries"):
        raise ValueError("not_found")

    video = info["entries"][0]
    if video is None:
        raise ValueError("not_found")

    # After the ffmpeg post-processor runs, the file on disk ends with .mp3.
    # The path matches the output template: <workdir>/<video id>.mp3
    mp3_path = os.path.join(workdir, f"{video['id']}.mp3")
    if not os.path.exists(mp3_path):
        # Fallback: find any mp3 written into the workdir.
        candidates = [f for f in os.listdir(workdir) if f.endswith(".mp3")]
        if not candidates:
            raise ValueError("not_found")
        mp3_path = os.path.join(workdir, candidates[0])

    return {
        "path": mp3_path,
        "workdir": workdir,
        "title": video.get("title") or query,
        "performer": video.get("uploader") or video.get("channel") or "",
        "duration": int(video.get("duration") or 0),
        "query": query,
    }


def cleanup_track(track: dict) -> None:
    """Delete a downloaded track and its temp directory."""
    try:
        shutil.rmtree(track["workdir"], ignore_errors=True)
    except Exception:
        pass


async def download_track(query: str, status_msg: Message):
    """Download a song, editing `status_msg` along the way.

    Returns the track dict, or None if the user was already told why it failed.
    """
    workdir = tempfile.mkdtemp(prefix="musicbot_")
    try:
        track = await asyncio.to_thread(search_and_download, query, workdir)
    except ValueError:
        await status_msg.edit_text(
            f"معلش، ملقيتش \"{query}\" على يوتيوب 😕 جرّب اسم تاني أو اكتبه بطريقة تانية."
        )
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    except yt_dlp.utils.DownloadError:
        logger.exception("yt-dlp failed for query: %s", query)
        await status_msg.edit_text(
            "حصلت مشكلة وأنا بحمّل الأغنية 😞 جرّب تاني بعد شوية."
        )
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    except Exception:
        logger.exception("Unexpected error for query: %s", query)
        await status_msg.edit_text(
            "حصل خطأ غير متوقع 😞 جرّب تاني بعد شوية."
        )
        shutil.rmtree(workdir, ignore_errors=True)
        return None

    file_size = os.path.getsize(track["path"])
    if file_size > MAX_FILE_SIZE:
        await status_msg.edit_text(
            "الأغنية دي أكبر من 50 ميجا، وتليجرام مش بيسمح للبوتات تبعت ملفات بالحجم ده 😕 جرّب نسخة أقصر."
        )
        cleanup_track(track)
        return None

    return track


# ---------------------------------------------------------------------------
# Feature 1: "يوت <song name>" -> send the song as an audio file
# ---------------------------------------------------------------------------

@bot.on_message(filters.text & filters.regex(DOWNLOAD_RE))
async def download_handler(client: Client, message: Message) -> None:
    query = DOWNLOAD_RE.match(message.text).group(1).strip()

    status = await message.reply_text(f"🔍 بدور على: {query} ...")
    track = await download_track(query, status)
    if track is None:
        return

    try:
        await status.edit_text("⬆️ لقيتها! برفعها دلوقتي...")
        await message.reply_audio(
            audio=track["path"],
            title=track["title"],
            performer=track["performer"],
            duration=track["duration"],
            caption="تم التحميل بنجاح ✅",
        )
        await status.delete()
    except Exception:
        logger.exception("Failed to send audio for query: %s", query)
        try:
            await status.edit_text(
                "حصلت مشكلة وأنا ببعت الأغنية 😞 جرّب تاني بعد شوية."
            )
        except Exception:
            pass
    finally:
        cleanup_track(track)


# ---------------------------------------------------------------------------
# Feature 2: voice-chat streaming ("تشغيل" / "تخطي" / "إيقاف")
# ---------------------------------------------------------------------------

def make_stream(track: dict) -> MediaStream:
    """Build an audio-only stream for a downloaded track."""
    return MediaStream(
        track["path"],
        audio_parameters=AudioQuality.HIGH,
        video_flags=MediaStream.Flags.IGNORE,
    )


async def start_stream(chat_id: int, track: dict) -> None:
    """Stream `track` into the voice chat of `chat_id` and mark it current."""
    await calls.play(chat_id, make_stream(track))
    current[chat_id] = track


async def play_next(chat_id: int) -> None:
    """Advance to the next queued track, or leave the call if the queue is empty."""
    old = current.pop(chat_id, None)
    if old:
        cleanup_track(old)
    if queues[chat_id]:
        await start_stream(chat_id, queues[chat_id].popleft())
    else:
        await calls.leave_call(chat_id)


if VOICE_ENABLED:

    @bot.on_message(filters.group & filters.text & filters.regex(PLAY_RE))
    async def play_handler(client: Client, message: Message) -> None:
        chat_id = message.chat.id
        query = PLAY_RE.match(message.text).group(1).strip()

        status = await message.reply_text(f"🔍 بدور على: {query} ...")
        track = await download_track(query, status)
        if track is None:
            return

        if chat_id in current:
            # Something is already playing: add to the queue.
            queues[chat_id].append(track)
            await status.edit_text(
                f"🎵 اتضافت للطابور (رقم {len(queues[chat_id])}): {track['title']}"
            )
            return

        try:
            await start_stream(chat_id, track)
        except NoActiveGroupCall:
            await status.edit_text(
                "مفيش مكالمة صوتية شغالة في الجروب دلوقتي 😕 افتح مكالمة الأول وبعدين اكتب تشغيل تاني."
            )
            cleanup_track(track)
            return
        except Exception:
            logger.exception("Failed to start stream in chat %s", chat_id)
            await status.edit_text(
                "حصلت مشكلة وأنا بشغّل الأغنية في المكالمة 😞 تأكد إن الحساب المساعد موجود في الجروب وجرّب تاني."
            )
            cleanup_track(track)
            return

        await status.edit_text(f"🎶 شغالة دلوقتي: {track['title']}")

    @bot.on_message(filters.group & filters.text & filters.regex(SKIP_RE))
    async def skip_handler(client: Client, message: Message) -> None:
        chat_id = message.chat.id
        if chat_id not in current:
            await message.reply_text("مفيش أغنية شغالة دلوقتي عشان أتخطاها 😅")
            return
        try:
            # play() on a chat that is already streaming replaces the stream,
            # so skipping is just: drop the current track and play the next.
            # A replaced stream does not fire a stream-end update, so the
            # queue only advances here, never twice.
            await play_next(chat_id)
        except Exception:
            logger.exception("Failed to skip in chat %s", chat_id)
            await message.reply_text("حصلت مشكلة وأنا بتخطى 😞")
            return
        if chat_id in current:
            await message.reply_text(f"⏭ تخطيت! شغالة دلوقتي: {current[chat_id]['title']}")
        else:
            await message.reply_text("⏭ تخطيت! مفيش أغانى تانية في الطابور، فقفلت التشغيل.")

    @bot.on_message(filters.group & filters.text & filters.regex(STOP_RE))
    async def stop_handler(client: Client, message: Message) -> None:
        chat_id = message.chat.id
        if chat_id not in current:
            await message.reply_text("مفيش حاجة شغالة أصلاً 😅")
            return
        while queues[chat_id]:
            cleanup_track(queues[chat_id].popleft())
        old = current.pop(chat_id, None)
        if old:
            cleanup_track(old)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            logger.exception("Failed to leave call in chat %s", chat_id)
        await message.reply_text("⏹ وقفت التشغيل ومسحت الطابور.")

    @calls.on_update(call_filters.stream_end)
    async def stream_end_handler(client: PyTgCalls, update: StreamEnded) -> None:
        """The current song finished naturally: play the next one in the queue."""
        chat_id = update.chat_id
        try:
            await play_next(chat_id)
        except Exception:
            logger.exception("Failed to advance queue in chat %s", chat_id)
            current.pop(chat_id, None)
            return
        if chat_id in current:
            try:
                await bot.send_message(
                    chat_id, f"🎶 شغالة دلوقتي: {current[chat_id]['title']}"
                )
            except Exception:
                pass
        else:
            try:
                await bot.send_message(chat_id, "✅ خلصت كل الأغاني في الطابور.")
            except Exception:
                pass

    @calls.on_update(
        call_filters.chat_update(
            ChatUpdate.Status.CLOSED_VOICE_CHAT
            | ChatUpdate.Status.KICKED
            | ChatUpdate.Status.LEFT_GROUP
        )
    )
    async def call_closed_handler(client: PyTgCalls, update: ChatUpdate) -> None:
        """The voice chat ended or the assistant was removed: reset the state."""
        chat_id = update.chat_id
        while queues[chat_id]:
            cleanup_track(queues[chat_id].popleft())
        old = current.pop(chat_id, None)
        if old:
            cleanup_track(old)


else:

    @bot.on_message(filters.group & filters.text & filters.regex(PLAY_RE))
    async def play_disabled_handler(client: Client, message: Message) -> None:
        await message.reply_text(
            "ميزة التشغيل في المكالمات مش متظبطة لسه (ناقص SESSION_STRING). "
            "بس تقدر تكتب \"يوت\" واسم الأغنية وأنا أنزلهالك على طول 🎵"
        )


# ---------------------------------------------------------------------------
# Health endpoint + self keep-alive (for free-tier web hosting)
# ---------------------------------------------------------------------------

def start_health_server() -> None:
    """Tiny HTTP server so the hosting platform sees a healthy web service.

    Free tiers (Render) only host web services, so the bot binds a port and
    answers 200 on every path. It also pings its own public URL every few
    minutes: the request comes back in through the platform's load balancer
    and counts as inbound traffic, which keeps the free instance awake.
    """
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("music bot is alive".encode("utf-8"))

        def log_message(self, *args):
            pass  # keep logs clean

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on port %s", port)

    public_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
    if public_url:
        def keep_alive():
            while True:
                try:
                    urllib.request.urlopen(public_url, timeout=15)
                except Exception:
                    pass
                threading.Event().wait(300)  # ping every 5 minutes

        threading.Thread(target=keep_alive, daemon=True).start()
        logger.info("Self keep-alive pinging %s every 5 minutes", public_url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("API_ID", API_ID),
            ("API_HASH", API_HASH),
        )
        if not value
    ]
    if missing:
        # Self-diagnosing output: show which setting keys are actually
        # visible (names only, never values) so a screenshot tells the story.
        visible = sorted(
            k for k in os.environ if k.strip().upper() in KNOWN_ENV_KEYS
        )
        print(f"Setting keys currently visible to the bot: {visible}")
        raise SystemExit(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Check the README for setup steps."
        )

    start_health_server()
    bot.start()
    logger.info("Bot client started.")
    if VOICE_ENABLED:
        calls.start()  # starts the assistant client and the calls binding
        logger.info("Assistant + voice calls started.")
    else:
        logger.warning(
            "SESSION_STRING is not set: running in download-only mode "
            "(the 'يوت' feature works, voice-chat 'تشغيل' is disabled)."
        )

    idle()  # run until Ctrl+C

    bot.stop()
    if VOICE_ENABLED:
        calls.stop()


if __name__ == "__main__":
    main()
