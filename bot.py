"""
Telegram music bot.

Features
  1. Download mode - anyone can ask for a song and get it as a playable audio
     file. Works in private chats and in groups:
         يوت <song name>          (Arabic trigger word)
         /song <song name>        (also /yt)
  2. Voice-chat mode ("تشغيل") - in a group with an active voice chat, an
     assistant account joins and streams the song.
     Extra words: "تخطي" (skip) and "إيقاف" (stop and clear the queue).
  3. Commands: /start and /help.

Architecture
  - One Pyrogram client logged in with the bot token handles all messages.
  - A second Pyrogram client (the assistant, logged in with a session string
    from a regular Telegram account) plus pytgcalls streams audio into the
    group voice chat.

Environment variables (put them in the .env file next to this script)
  BOT_TOKEN       - token from @BotFather (required)
  API_ID          - from https://my.telegram.org (required)
  API_HASH        - from https://my.telegram.org (required)
  SESSION_STRING  - assistant account session from gen_session.py
                    (optional; without it the bot runs in download-only mode)
  COOKIES_FILE    - optional path to a YouTube cookies.txt, only needed if
                    YouTube starts asking for a sign-in
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque

import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand, Message
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, StreamEnded


# ---------------------------------------------------------------------------
# Loading the .env file (must happen BEFORE the settings below are read)
# ---------------------------------------------------------------------------

def load_env_file() -> None:
    """Populate os.environ from a .env file sitting next to this script.

    Tolerant of Windows quirks: a UTF-8 BOM, a UTF-16 file saved by Notepad,
    and a file accidentally saved as ".env.txt" by Explorer hiding extensions.
    A value that is already set (and not empty) in the real environment wins;
    empty ones are filled from the file.
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

    # UTF-16 files start with a BOM (ff fe / fe ff) or contain NUL bytes;
    # anything else is treated as UTF-8 (BOM stripped).
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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            if not os.environ.get(key):
                os.environ[key] = value
            loaded += 1
    print(f"Loaded {loaded} setting(s) from {path}")


load_env_file()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
COOKIES_FILE = os.environ.get("COOKIES_FILE")

# Telegram bots cannot upload files larger than this (bytes).
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 MB, a small safety margin under 50 MB

# Songs longer than this are refused BEFORE downloading (saves time + bandwidth).
MAX_DURATION_SECONDS = 25 * 60

# Download robustness (the googlevideo.com "timed out" errors).
DOWNLOAD_ATTEMPTS = 3          # retries of the whole download on network errors
SOCKET_TIMEOUT = 30            # seconds, per connection
MAX_PARALLEL_DOWNLOADS = 2     # downloads running at the same time

# A user must wait this many seconds between requests (anti-spam).
COOLDOWN_SECONDS = 8

# Triggers: a message that starts with the trigger word followed by the song name.
DOWNLOAD_RE = re.compile(r"^\s*يوت\s+(.+?)\s*$", re.DOTALL)
PLAY_RE = re.compile(r"^\s*تشغيل\s+(.+?)\s*$", re.DOTALL)
SKIP_RE = re.compile(r"^\s*تخطي\s*$")
STOP_RE = re.compile(r"^\s*(إيقاف|ايقاف)\s*$")

# Words in a yt-dlp error that mean "network problem, worth retrying".
NETWORK_ERROR_HINTS = (
    "timed out", "timeout", "connection", "temporarily", "reset by peer",
    "read error", "incomplete read", "503", "502", "remote end closed",
)


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

# Shared state for the download side.
download_slots = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
last_request = {}  # user_id -> time.monotonic() of the last accepted request


def cooldown_left(user_id: int) -> float:
    """Seconds the user still has to wait (0 means they can go ahead)."""
    now = time.monotonic()
    left = COOLDOWN_SECONDS - (now - last_request.get(user_id, -1e9))
    if left > 0:
        return left
    last_request[user_id] = now
    return 0.0


# ---------------------------------------------------------------------------
# YouTube search + download (blocking, runs in a worker thread)
# ---------------------------------------------------------------------------

def is_network_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(hint in text for hint in NETWORK_ERROR_HINTS)


def search_and_download(query: str, workdir: str, on_found=None) -> dict:
    """Search YouTube for `query`, download the top result as an m4a file.

    m4a is what Telegram's player likes, and YouTube already serves AAC audio
    in an m4a container, so most of the time ffmpeg just copies it (no slow
    re-encoding, unlike the old mp3 conversion).

    `on_found(title)` is called once from the worker thread when the download
    starts. Returns a dict with: path, workdir, title, performer, duration, query.
    Raises ValueError("not_found") / ValueError("too_long").
    """
    output_template = os.path.join(workdir, "%(id)s.%(ext)s")
    state = {"too_long": False, "announced": False}

    def match_filter(info, *, incomplete=False):
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > MAX_DURATION_SECONDS:
            state["too_long"] = True
            return "too long"
        return None

    def progress_hook(d):
        if state["announced"] or on_found is None or d.get("status") != "downloading":
            return
        state["announced"] = True
        try:
            on_found((d.get("info_dict") or {}).get("title") or "")
        except Exception:
            pass

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "match_filter": match_filter,
        "progress_hooks": [progress_hook],
        # --- network robustness / speed ---
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "source_address": "0.0.0.0",        # force IPv4 (IPv6 often times out)
        "http_chunk_size": 10 * 1024 * 1024,  # avoids YouTube throttling
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            }
        ],
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    info = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            break
        except yt_dlp.utils.DownloadError as e:
            if attempt < DOWNLOAD_ATTEMPTS and is_network_error(e):
                logger.warning("Network error (attempt %s/%s): %s", attempt,
                               DOWNLOAD_ATTEMPTS, e)
                time.sleep(2 * attempt)
                continue
            raise

    if state["too_long"]:
        raise ValueError("too_long")
    if not info or not info.get("entries"):
        raise ValueError("not_found")

    video = info["entries"][0]
    if video is None:
        raise ValueError("not_found")

    # The finished file is <workdir>/<video id>.m4a. Fall back to any audio
    # file in the folder (ignoring half-finished .part files).
    audio_path = os.path.join(workdir, f"{video['id']}.m4a")
    if not os.path.exists(audio_path):
        found = [
            os.path.join(workdir, f) for f in os.listdir(workdir)
            if f.lower().endswith((".m4a", ".mp3", ".opus", ".webm", ".ogg"))
        ]
        if not found:
            raise ValueError("not_found")
        audio_path = max(found, key=os.path.getsize)

    return {
        "path": audio_path,
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


def friendly_download_error(error: Exception) -> str:
    """Turn a yt-dlp error into a short Arabic message for the user."""
    text = str(error).lower()
    if is_network_error(error):
        return "النت عندي بيقطع وأنا بحمّل 😞 جرّب تاني كمان شوية."
    if "sign in" in text or "not a bot" in text or "cookies" in text:
        return "يوتيوب طالب تسجيل دخول للفيديو ده 😕 جرّب أغنية تانية."
    if "age" in text and "restrict" in text:
        return "الفيديو ده مقيّد بالسن ومقدرش أحمّله 😕 جرّب نسخة تانية."
    if "private" in text or "unavailable" in text or "not available" in text:
        return "الفيديو ده مش متاح عندي 😕 جرّب أغنية تانية أو اكتب الاسم بشكل مختلف."
    return "حصلت مشكلة وأنا بحمّل الأغنية 😞 جرّب تاني بعد شوية."


async def download_track(query: str, status_msg: Message):
    """Download a song, editing `status_msg` along the way.

    Returns the track dict, or None if the user was already told why it failed.
    """
    loop = asyncio.get_running_loop()

    def on_found(title: str) -> None:
        # Called from the worker thread: hop back to the event loop.
        if title:
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(f"⬇️ بحمّل: {title}"), loop
            )

    workdir = tempfile.mkdtemp(prefix="musicbot_")
    try:
        async with download_slots:
            track = await asyncio.to_thread(
                search_and_download, query, workdir, on_found
            )
    except ValueError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        if str(e) == "too_long":
            minutes = MAX_DURATION_SECONDS // 60
            await status_msg.edit_text(
                f"الأغنية دي أطول من {minutes} دقيقة 😕 جرّب نسخة أقصر."
            )
        else:
            await status_msg.edit_text(
                f"معلش، ملقيتش \"{query}\" على يوتيوب 😕 جرّب اسم تاني أو اكتبه بطريقة تانية."
            )
        return None
    except yt_dlp.utils.DownloadError as e:
        logger.exception("yt-dlp failed for query: %s", query)
        shutil.rmtree(workdir, ignore_errors=True)
        await status_msg.edit_text(friendly_download_error(e))
        return None
    except Exception:
        logger.exception("Unexpected error for query: %s", query)
        shutil.rmtree(workdir, ignore_errors=True)
        await status_msg.edit_text("حصل خطأ غير متوقع 😞 جرّب تاني بعد شوية.")
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
# Commands: /start and /help
# ---------------------------------------------------------------------------

def help_text() -> str:
    lines = [
        "🎵 أهلاً! أنا بوت الأغاني.",
        "",
        "📥 لتحميل أغنية:",
        "• اكتب: يوت اسم الأغنية",
        "• أو: /song اسم الأغنية",
        "",
    ]
    if VOICE_ENABLED:
        lines += [
            "🎧 للمكالمات الصوتية (في الجروبات):",
            "• تشغيل اسم الأغنية",
            "• تخطي",
            "• إيقاف",
            "",
        ]
    lines += [
        "مثال: يوت عصام صاصا",
        f"⏱ أقصى مدة للأغنية {MAX_DURATION_SECONDS // 60} دقيقة.",
    ]
    return "\n".join(lines)


@bot.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message) -> None:
    await message.reply_text(help_text())


# ---------------------------------------------------------------------------
# Feature 1: "يوت <song name>" or "/song <song name>" -> audio file
# ---------------------------------------------------------------------------

async def send_song(message: Message, query: str) -> None:
    """Shared by the "يوت" trigger and the /song command."""
    user_id = message.from_user.id if message.from_user else message.chat.id
    wait = cooldown_left(user_id)
    if wait:
        await message.reply_text(f"استنى {int(wait) + 1} ثواني وابعت تاني ⏳")
        return

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


@bot.on_message(filters.text & filters.regex(DOWNLOAD_RE))
async def download_handler(client: Client, message: Message) -> None:
    query = DOWNLOAD_RE.match(message.text).group(1).strip()
    await send_song(message, query)


@bot.on_message(filters.command(["song", "yt"]))
async def song_command_handler(client: Client, message: Message) -> None:
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("اكتب اسم الأغنية بعد الأمر، مثال:\n/song عصام صاصا")
        return
    await send_song(message, query)


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
# Health endpoint + self keep-alive (needed for the free web host: it only
# hosts "web services", so the bot answers HTTP on the port it is given and
# pings its own public URL every few minutes to stay awake)
# ---------------------------------------------------------------------------

def start_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("music bot is alive".encode("utf-8"))

        def log_message(self, *args):
            pass  # keep the logs clean

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
        raise SystemExit(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Check the README for setup steps."
        )

    start_health_server()
    bot.start()
    logger.info("Bot client started.")

    # Show the commands in Telegram's "/" menu.
    try:
        bot.set_bot_commands([
            BotCommand("start", "ابدأ واعرف الأوامر"),
            BotCommand("help", "شرح الأوامر"),
            BotCommand("song", "حمّل أغنية: /song اسم الأغنية"),
        ])
    except Exception:
        logger.warning("Could not register the command menu.", exc_info=True)

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
