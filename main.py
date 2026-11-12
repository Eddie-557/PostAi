import os
import re
import sys
import math
import random
import asyncio
import sqlite3
import traceback

import requests
import edge_tts
import numpy as np
import google.generativeai as genai
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import VideoFileClip, AudioFileClip, ImageClip, CompositeVideoClip
import moviepy.video.fx.all as vfx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Load environment variables (works locally via .env, and on GitHub via Secrets)
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Optional: set GEMINI_MODEL as an env var to force a specific model.
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

CATEGORIES = ["News", "Movies", "Entertainment", "Christianity", "Trends"]
DB_FILE = "automation.db"
TTS_VOICE = "en-US-ChristopherNeural"
TEMP_AUDIO = "temp_audio.mp3"
TEMP_VIDEO = "temp_video.mp4"
OUTPUT_FILE = "final_ready_video.mp4"

VIDEO_W, VIDEO_H = 1080, 1920
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024  # Telegram bots can upload up to 50 MB

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def log(msg):
    # flush=True so the GitHub Actions log shows lines in the right order
    print(msg, flush=True)


def check_env():
    missing = [
        name for name, value in [
            ("GEMINI_API_KEY", GEMINI_API_KEY),
            ("PIXABAY_API_KEY", PIXABAY_API_KEY),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN),
            ("TELEGRAM_CHAT_ID", CHAT_ID),
        ] if not value
    ]
    if missing:
        raise ValueError(f"Missing environment variables/secrets: {', '.join(missing)}")


# --- 1. ROTATION & SCRIPT ENGINE ---

def setup_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rotation_state (
            id INTEGER PRIMARY KEY,
            last_category TEXT
        )
    ''')
    cursor.execute('SELECT COUNT(*) FROM rotation_state')
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            'INSERT INTO rotation_state (id, last_category) VALUES (1, ?)',
            (CATEGORIES[-1],)
        )
    conn.commit()
    conn.close()


def peek_next_category():
    """Returns the next category WITHOUT saving it. Saved only after a successful send."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT last_category FROM rotation_state WHERE id = 1')
    row = cursor.fetchone()
    conn.close()
    last_category = row[0] if row else CATEGORIES[-1]

    try:
        next_index = (CATEGORIES.index(last_category) + 1) % len(CATEGORIES)
    except ValueError:
        next_index = 0
    return CATEGORIES[next_index]


def save_category(category):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE rotation_state SET last_category = ? WHERE id = 1', (category,))
    conn.commit()
    conn.close()


def clean_script(text):
    """Remove markdown and [stage directions] so the voice engine reads clean text."""
    text = re.sub(r"\[[^\]]*\]", "", text)      # [Music], [Intro] ...
    text = re.sub(r"\([^)]*\)", "", text)       # (pause), (dramatic) ...
    text = re.sub(r"[*_#`>~]", "", text)        # markdown characters
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _try_generate(model_name, prompt):
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt)
    try:
        text = response.text
    except ValueError:
        # Raised when the response was blocked or empty
        raise RuntimeError(
            f"Gemini returned no text (model {model_name}). Feedback: {response.prompt_feedback}"
        )
    text = clean_script(text)
    if len(text) < 50:
        raise RuntimeError(f"Script too short from {model_name}: {text!r}")
    return text


def generate_script(category):
    log(f"[*] Generating script for category: {category}...")
    prompt = f"""
    Write a highly engaging, 45-second video script for this category: {category}.
    Provide ONLY the spoken text, as plain sentences. No titles, no markdown, no emojis,
    no visual directions, no stage directions, no timestamps.
    The text must be ready to be read directly by an AI voiceover engine.
    """

    tried = []
    last_error = None

    # Attempt 1: the configured model
    try:
        log(f"[*] Using Gemini model: {GEMINI_MODEL}")
        return _try_generate(GEMINI_MODEL, prompt)
    except Exception as e:
        tried.append(GEMINI_MODEL)
        last_error = e
        log(f"[!] Model '{GEMINI_MODEL}' failed: {type(e).__name__}: {e}")

    # Attempt 2: ask the API which models this key can actually use
    log("[*] Looking for another usable model on this API key...")
    available = []
    for m in genai.list_models():
        if "generateContent" in m.supported_generation_methods:
            available.append(m.name.replace("models/", ""))
    log(f"[*] Models available to this key: {available}")

    for name in [n for n in available if "flash" in n and n not in tried]:
        try:
            log(f"[*] Trying fallback model: {name}")
            return _try_generate(name, prompt)
        except Exception as e:
            tried.append(name)
            last_error = e
            log(f"[!] Model '{name}' failed: {type(e).__name__}: {e}")

    raise RuntimeError(f"No Gemini model worked. Tried: {tried}. Last error: {last_error}")


# --- 2. MEDIA GENERATOR ENGINE ---

async def generate_audio(text, output_filename):
    log("[*] Generating AI Voiceover...")
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(output_filename)
    if not os.path.exists(output_filename) or os.path.getsize(output_filename) == 0:
        raise RuntimeError("edge-tts produced no audio file.")


def download_background_video(category, output_filename):
    log(f"[*] Downloading Pixabay video for '{category}'...")
    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": PIXABAY_API_KEY, "q": category, "per_page": 20, "safesearch": "true"},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Pixabay API error {response.status_code}: {response.text[:300]}")

    hits = response.json().get("hits", [])
    if not hits:
        raise RuntimeError(f"No Pixabay videos found for '{category}'")

    # Collect usable URLs (best quality available per hit), then pick one at random for variety
    options = []
    for hit in hits:
        for size in ("large", "medium", "small", "tiny"):
            info = hit.get("videos", {}).get(size)
            if info and info.get("url"):
                options.append(info["url"])
                break
    if not options:
        raise RuntimeError("Pixabay returned hits but none had a usable video URL.")

    video_url = random.choice(options)
    with requests.get(video_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(output_filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

    if os.path.getsize(output_filename) == 0:
        raise RuntimeError("Downloaded Pixabay video is empty.")


def find_font_path():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def make_title_clip(text, duration):
    """Builds the title overlay with Pillow (no ImageMagick needed). Returns None if no font."""
    font_path = find_font_path()
    if not font_path:
        log("[!] No TrueType font found. Skipping the title overlay.")
        return None

    max_text_w = int(VIDEO_W * 0.8)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # Shrink the font until the text fits in the allowed width
    font_size = 90
    while True:
        font = ImageFont.truetype(font_path, font_size)
        bbox = probe.textbbox((0, 0), text, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if text_w <= max_text_w or font_size <= 30:
            break
        font_size -= 4

    pad = 40
    img = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, img.width - 1, img.height - 1), radius=24, fill=(0, 0, 0, 160)
    )
    draw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(255, 255, 255, 255))

    clip = ImageClip(np.array(img))
    return clip.set_duration(duration).set_position("center")


def assemble_video(audio_path, video_path, category, output_path):
    log("[*] Assembling final video...")
    audio_clip = AudioFileClip(audio_path)
    source = VideoFileClip(video_path)
    clips_to_close = [audio_clip, source]

    try:
        duration = audio_clip.duration

        # Match the video length to the voiceover
        if source.duration < duration:
            video_clip = source.fx(vfx.loop, duration=duration)
        else:
            video_clip = source.subclip(0, duration)

        # Scale so the clip fully covers 1080x1920, then crop from the center
        scale = max(VIDEO_W / source.w, VIDEO_H / source.h)
        new_w = max(VIDEO_W, math.ceil(source.w * scale))
        new_h = max(VIDEO_H, math.ceil(source.h * scale))
        video_clip = video_clip.resize(newsize=(new_w, new_h))

        x1 = (new_w - VIDEO_W) // 2
        y1 = (new_h - VIDEO_H) // 2
        video_clip = video_clip.crop(x1=x1, y1=y1, x2=x1 + VIDEO_W, y2=y1 + VIDEO_H)
        video_clip = video_clip.set_audio(audio_clip)

        layers = [video_clip]
        title_clip = make_title_clip(f"{category.upper()} UPDATE", duration)
        if title_clip is not None:
            layers.append(title_clip)

        final_video = CompositeVideoClip(layers, size=(VIDEO_W, VIDEO_H))
        clips_to_close.append(final_video)
        final_video.write_videofile(
            output_path,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="veryfast",
            bitrate="4000k",
            audio_bitrate="128k",
            threads=2,
            ffmpeg_params=["-pix_fmt", "yuv420p"],
            temp_audiofile="temp_moviepy_audio.m4a",
            remove_temp=True,
        )
    finally:
        for c in clips_to_close:
            try:
                c.close()
            except Exception:
                pass

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("MoviePy finished but the output video is missing or empty.")
    log(f"[+] Video created: {os.path.getsize(output_path) / 1e6:.1f} MB")


# --- 3. TELEGRAM MESSENGER ---

async def send_for_approval(category, script_text, video_path):
    log("[*] Sending final video to Telegram for approval...")

    size = os.path.getsize(video_path)
    if size > TELEGRAM_MAX_BYTES:
        raise RuntimeError(
            f"Video is {size / 1e6:.1f} MB, over Telegram's 50 MB bot upload limit."
        )

    keyboard = [[InlineKeyboardButton("✅ Approve & Publish", callback_data=f"approve_{category}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Plain text on purpose: AI-written text with * or _ breaks Telegram's Markdown parser.
    caption = (
        f"🎬 New Video Ready: {category}\n\n"
        f"Script Preview:\n{script_text[:150]}...\n\n"
        f"Tap Approve to publish to all platforms."
    )

    async with Bot(token=TELEGRAM_TOKEN) as bot:
        me = await bot.get_me()
        log(f"[*] Telegram bot connected as @{me.username}")
        with open(video_path, "rb") as video:
            message = await bot.send_video(
                chat_id=CHAT_ID,
                video=video,
                caption=caption,
                reply_markup=reply_markup,
                width=VIDEO_W,
                height=VIDEO_H,
                supports_streaming=True,
                read_timeout=180,
                write_timeout=180,
                connect_timeout=30,
            )
    log(f"[+] Telegram message sent (message_id={message.message_id}).")


# --- 4. MAIN PIPELINE EXECUTOR ---

async def main():
    try:
        check_env()
        genai.configure(api_key=GEMINI_API_KEY)

        # Step 1: Prep database and pick the category (not saved until success)
        setup_db()
        target_category = peek_next_category()
        log(f"[*] Pipeline started for category: {target_category}")

        # Step 2: Generate text script
        video_script = generate_script(target_category)
        log(f"[*] Script ready ({len(video_script)} characters).")

        # Step 3: Generate media files
        await generate_audio(video_script, TEMP_AUDIO)
        download_background_video(target_category, TEMP_VIDEO)

        # Step 4: Stitch it together
        assemble_video(TEMP_AUDIO, TEMP_VIDEO, target_category, OUTPUT_FILE)

        # Step 5: Send for human approval
        await send_for_approval(target_category, video_script, OUTPUT_FILE)

        # Step 6: Only now advance the rotation
        save_category(target_category)
        log("[*] Pipeline completed successfully. Shutting down.")

    finally:
        # Cleanup temp files before the GitHub server shuts down
        for path in (TEMP_AUDIO, TEMP_VIDEO, "temp_moviepy_audio.m4a"):
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    log("[*] Waking up PostAI...")
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        log("[!] Pipeline FAILED. See the error above.")
        sys.exit(1)  # makes the GitHub Action turn red instead of green.