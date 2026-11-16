import os
import re
import sys
import json
import glob
import random
import shutil
import asyncio
import sqlite3
import tempfile
import subprocess
import traceback
from datetime import datetime, timedelta, timezone

import requests
import edge_tts
import google.generativeai as genai
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import sources

# Load environment variables (works locally via .env, and on GitHub via Secrets)
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Optional settings (GitHub: Settings > Secrets and variables > Actions > Variables)
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
TTS_VOICE = os.getenv("TTS_VOICE") or ""
VOICEOVER = (os.getenv("VOICEOVER") or "1") != "0"
MUSIC_VOLUME = float(os.getenv("MUSIC_VOLUME") or 0.18)
CHANNEL_HANDLE = (os.getenv("CHANNEL_HANDLE") or "").strip()  # e.g. @YourChannel, shown as a watermark

CATEGORIES = ["News", "Movies", "Entertainment", "Christianity", "Trends"]
# Each category gets its own video style
LAYOUTS = {
    "News": "news",              # blurred background, date badge, lower-third subtitles, several stories
    "Movies": "meme_bar",        # white caption bar on top, footage below, subtitles
    "Entertainment": "countdown",  # 5th, 4th, ... numbered story countdown
    "Christianity": "verse",     # verse card over calm footage
    "Trends": "overlay",         # meme hook caption over full-screen footage
}
DB_FILE = "automation.db"
OUTPUT_FILE = "final_ready_video.mp4"

W, H, FPS = 1080, 1920, 30
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024
MIN_BEAT, MAX_BEAT = 3.0, 11.0

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".ogg", ".aac")

# Used if a beat's own keywords find nothing on Pixabay
FALLBACK_QUERIES = {
    "News": ["city street", "newsroom", "crowd of people"],
    "Movies": ["cinema", "movie theater"],
    "Entertainment": ["concert crowd", "party lights"],
    "Christianity": ["sunrise sky", "church", "praying hands"],
    "Trends": ["smartphone scrolling", "city night"],
}

WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
ORANGE = (255, 153, 0, 255)
YELLOW = (255, 204, 0, 255)

# Emoji and symbols the caption font cannot draw
UNRENDERABLE = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200d]")
NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


class StorySkipped(Exception):
    """Gemini judged the story unsuitable (for example, a tragedy)."""


def log(msg):
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


# --- 1. ROTATION ---

def setup_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS rotation_state (id INTEGER PRIMARY KEY, last_category TEXT)")
    if conn.execute("SELECT COUNT(*) FROM rotation_state").fetchone()[0] == 0:
        conn.execute("INSERT INTO rotation_state (id, last_category) VALUES (1, ?)", (CATEGORIES[-1],))
    conn.commit()
    conn.close()
    sources.ensure_tables(DB_FILE)


def peek_next_category():
    """Next category WITHOUT saving it. Saved only after a successful send."""
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT last_category FROM rotation_state WHERE id = 1").fetchone()
    conn.close()
    last = row[0] if row else CATEGORIES[-1]
    try:
        index = (CATEGORIES.index(last) + 1) % len(CATEGORIES)
    except ValueError:
        index = 0
    return CATEGORIES[index]


def save_category(category):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE rotation_state SET last_category = ? WHERE id = 1", (category,))
    conn.commit()
    conn.close()


# --- 2. SCRIPTS (Gemini turns REAL stories into captions) ---

def clean_caption(text):
    text = UNRENDERABLE.sub("", str(text or ""))
    text = re.sub(r"[*_#`>~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _sources_block(stories):
    lines = []
    for i, s in enumerate(stories, 1):
        published = s.published.strftime("%Y-%m-%d") if s.published else "unknown date"
        lines.append(f"[{i}] Title: {s.title}\n    Summary: {s.summary}\n    From: {s.source} ({published})")
    return "\n".join(lines)


JSON_NOTE = "Reply with ONLY valid JSON in exactly this shape, with no markdown:"
COMMON_RULES = (
    "Never invent names, numbers, quotes, dates, prices, places or events: use ONLY facts in the sources. "
    "No emojis. The captions are read aloud by a voice, so write normal sentences."
)


def prompt_meme(category, story):
    tone = {
        "Movies": "Playful movie-fan humor (Nollywood and cinema culture). The first line is shown as a meme caption at the top of the video.",
        "Trends": "Meme-style humor about why Nigerians are searching or talking about this topic.",
    }[category]
    published = story.published.strftime("%Y-%m-%d") if story.published else "unknown"
    return f"""You write short vertical-video scripts for a Nigerian audience in a meme / skit-caption style.

CATEGORY: {category}
STYLE: {tone}

SOURCE (the ONLY facts you may use):
Title: {story.title}
Summary: {story.summary}
Published: {published}
From: {story.source}

RULES:
1. {COMMON_RULES} If a joke would need an invented fact, write a plain accurate statement instead.
2. Write 4 beats. Each beat is one short caption line (under 110 characters). The FIRST beat is the hook: a scroll-stopping meme-style line such as "Nigerians after hearing about ..." or "POV: ...". The beats move the story forward in order and the last one lands the punchline or takeaway.
3. Joke about the situation, never about individuals, tribes, religion, victims or disabilities. No insults, no vulgar words. Light Nigerian English or light Pidgin is welcome.
4. If the SOURCE is about death, violence, abuse, an accident, a disaster or any tragedy, reply with exactly {{"skip": true}}.
5. Each beat has "keywords": 2 or 3 plain English words to search stock video for a matching background (for example "lagos traffic", "money counting").
6. "post_caption": 1 or 2 sentences for the social post plus 3 to 5 hashtags, under 220 characters.

{JSON_NOTE}
{{"beats": [{{"caption": "...", "keywords": "..."}}], "post_caption": "..."}}"""


def prompt_news(stories):
    return f"""You write the script for a short Nigerian news roundup video: neutral, factual and clear.

SOURCES (the ONLY facts you may use), numbered:
{_sources_block(stories)}

RULES:
1. {COMMON_RULES}
2. Write exactly {len(stories)} items, one per source, in the same order.
3. Each "caption" is one or two plain sentences (under 160 characters) that a news anchor would read aloud. Say what happened. Attribute claims with "according to <From>" or "reportedly"; never state allegations as fact.
4. Neutral tone: no jokes, no opinions, no insults, no speculation.
5. "keywords": 2 or 3 plain English words for stock footage that matches the TOPIC (for example "fuel station", "stock market", "hospital corridor"), never a person's name.
6. "post_caption": 1 or 2 sentences for the social post plus 3 to 5 hashtags, under 220 characters.

{JSON_NOTE}
{{"items": [{{"caption": "...", "keywords": "..."}}], "post_caption": "..."}}"""


def prompt_countdown(stories):
    return f"""You write the script for a short Nigerian entertainment countdown video ("{len(stories)} entertainment stories you missed").

SOURCES (the ONLY facts you may use), numbered:
{_sources_block(stories)}

RULES:
1. {COMMON_RULES}
2. Write exactly {len(stories)} items, one per source, in the same order.
3. "title": a punchy label of 2 to 6 words (under 45 characters) naming the story, using only the source.
4. "caption": one upbeat sentence (under 130 characters) saying what happened. Fun but accurate: never state rumours as fact, no insults.
5. "keywords": 2 or 3 plain English words for stock footage that matches the mood (for example "concert crowd", "red carpet"), never a person's name.
6. "post_caption": 1 or 2 sentences for the social post plus 3 to 5 hashtags, under 220 characters.

{JSON_NOTE}
{{"items": [{{"title": "...", "caption": "...", "keywords": "..."}}], "post_caption": "..."}}"""


def prompt_verse(story):
    return f"""You write two short lines that go before and after a Bible verse in a short, calm devotional video for a Nigerian audience.

VERSE (shown and read aloud automatically): {story.title}: "{story.summary}"

RULES:
1. {COMMON_RULES}
2. "opening": a relatable line (under 100 characters) that leads into the verse, such as "When life feels heavy, remember this".
3. "closing": a gentle encouragement or short prayer line (under 100 characters).
4. Do not quote or paraphrase the verse. Do not claim it means more than its plain reading. Never promise specific outcomes such as healing or wealth. Never mock faith, God, pastors or churches.
5. Each line has "keywords": 2 or 3 plain English words for calm stock footage (for example "sunrise sky", "quiet mountains").
6. "post_caption": 1 or 2 sentences for the social post plus 3 to 5 hashtags, under 220 characters.

{JSON_NOTE}
{{"opening": {{"caption": "...", "keywords": "..."}}, "closing": {{"caption": "...", "keywords": "..."}}, "post_caption": "..."}}"""


def _call_gemini(prompt):
    """Tries the configured model, then any other flash model this API key can use."""
    tried, last_error = [], None
    names = [GEMINI_MODEL]
    for round_number in (1, 2):
        for name in names:
            try:
                log(f"[*] Using Gemini model: {name}")
                response = genai.GenerativeModel(name).generate_content(prompt)
                try:
                    return response.text
                except ValueError:
                    raise RuntimeError(f"Gemini returned no text. Feedback: {response.prompt_feedback}")
            except Exception as e:
                tried.append(name)
                last_error = e
                log(f"[!] Model '{name}' failed: {type(e).__name__}: {str(e)[:200]}")
        if round_number == 1:
            log("[*] Looking for another usable model on this API key...")
            available = [
                m.name.replace("models/", "") for m in genai.list_models()
                if "generateContent" in m.supported_generation_methods
            ]
            log(f"[*] Models available to this key: {available}")
            names = [n for n in available if "flash" in n and n not in tried]
    raise RuntimeError(f"No Gemini model worked. Tried: {tried}. Last error: {last_error}")


def extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def _line(raw, field="caption", low=5, high=170):
    if not isinstance(raw, dict):
        raise ValueError("each item must be an object")
    text = clean_caption(raw.get(field))
    if not low <= len(text) <= high:
        raise ValueError(f"'{field}' length out of range: {text!r}")
    return text


def _post_caption(data):
    return clean_caption(data.get("post_caption"))[:300]


def validate_meme(data):
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    if data.get("skip"):
        raise StorySkipped()
    raw = data.get("beats")
    if not isinstance(raw, list) or not 3 <= len(raw) <= 6:
        raise ValueError("'beats' must be a list of 3 to 6 items")
    beats = []
    for r in raw:
        caption = _line(r, "caption", 5, 150)
        beats.append({"caption": caption, "speech": caption, "keywords": clean_caption(r.get("keywords"))[:60]})
    return {"beats": beats, "post_caption": _post_caption(data)}


def validate_items(data, count):
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    raw = data.get("items")
    if not isinstance(raw, list) or len(raw) != count:
        raise ValueError(f"'items' must be a list of exactly {count}")
    return raw, _post_caption(data)


def validate_verse(data):
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    opening, closing = data.get("opening"), data.get("closing")
    return {
        "opening": {"caption": _line(opening, "caption", 5, 130), "keywords": clean_caption((opening or {}).get("keywords"))[:60]},
        "closing": {"caption": _line(closing, "caption", 5, 130), "keywords": clean_caption((closing or {}).get("keywords"))[:60]},
        "post_caption": _post_caption(data),
    }


def _ask(prompt, validator):
    last_error = None
    for attempt in range(1, 4):
        text = _call_gemini(prompt)
        try:
            return validator(extract_json(text))
        except StorySkipped:
            raise
        except ValueError as e:  # includes JSONDecodeError
            last_error = e
            log(f"[!] Script attempt {attempt} was unusable ({e}). Retrying...")
    raise RuntimeError(f"Gemini did not return a usable script after 3 tries: {last_error}")


def speakable_reference(ref):
    """'Psalm 23:1-3' -> 'Psalm 23, verses 1 to 3' so the voice does not read it as a time or a sum."""
    ref = re.sub(r"^1 ", "First ", ref)
    ref = re.sub(r"^2 ", "Second ", ref)
    ref = re.sub(r"^3 ", "Third ", ref)
    ref = re.sub(r"(\d+):(\d+)-(\d+)", r"\1, verses \2 to \3", ref)
    ref = re.sub(r"(\d+):(\d+)", r"\1, verse \2", ref)
    return ref


def ordinal(n):
    return f"{n}{'TH' if 10 <= n % 100 <= 20 else {1: 'ST', 2: 'ND', 3: 'RD'}.get(n % 10, 'TH')}"


def make_plan(category, stories):
    """Turns real stories into a plan: a layout plus a list of beats (one caption + one clip each)."""
    layout = LAYOUTS[category]
    log(f"[*] Writing the {layout} script for {category}...")

    if layout in ("overlay", "meme_bar"):
        story = stories[0]
        script = _ask(prompt_meme(category, story), validate_meme)
        beats = script["beats"]
        post = script["post_caption"]

    elif layout == "news":
        items, post = _ask(prompt_news(stories), lambda d: validate_items(d, len(stories)))
        beats = [{"caption": "Here are the top stories in Nigeria right now.", "speech": "Here are the top stories in Nigeria right now.", "keywords": "newsroom"}]
        for r in items:
            caption = _line(r, "caption", 15, 200)
            beats.append({"caption": caption, "speech": caption, "keywords": clean_caption(r.get("keywords"))[:60]})
        beats.append({"caption": "Follow for more daily updates.", "speech": "Follow for more daily updates.", "keywords": "city skyline"})

    elif layout == "countdown":
        stories = list(reversed(stories))  # oldest first, so the newest story is the number-one finale
        items, post = _ask(prompt_countdown(stories), lambda d: validate_items(d, len(stories)))
        beats = []
        total = len(items)
        for i, r in enumerate(items):
            rank = total - i  # counts down: 5th, 4th, ... 1st
            caption = _line(r, "caption", 10, 170)
            beats.append({
                "caption": caption,
                "speech": f"Number {NUMBER_WORDS[rank] if rank < len(NUMBER_WORDS) else rank}. {caption}",
                "keywords": clean_caption(r.get("keywords"))[:60],
                "title": _line(r, "title", 3, 60), "rank": rank,
            })
        beats.append({"caption": "Follow for the next countdown.", "speech": "Follow for the next countdown.", "keywords": "concert lights"})

    elif layout == "verse":
        story = stories[0]
        script = _ask(prompt_verse(story), validate_verse)
        post = script["post_caption"]
        verse = {
            "caption": story.summary, "ref": story.title, "translation": story.source.split(" (")[0],
            "speech": f"{speakable_reference(story.title)}. {story.summary}", "keywords": "sunrise sky",
            "is_verse": True,
        }
        beats = [
            {"caption": script["opening"]["caption"], "speech": script["opening"]["caption"], "keywords": script["opening"]["keywords"]},
            verse,
            {"caption": script["closing"]["caption"], "speech": script["closing"]["caption"], "keywords": script["closing"]["keywords"]},
        ]
    else:
        raise RuntimeError(f"Unknown layout: {layout}")

    return {"layout": layout, "category": category, "beats": beats, "post_caption": post, "stories": stories}


# --- 3. VOICE ---

def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError("FFmpeg not found (install imageio-ffmpeg or ffmpeg).")


def run_ffmpeg(args):
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (exit code {result.returncode}):\n{result.stderr[-1500:] or '(no error text; the process may have been killed, for example for lack of memory)'}"
        )


def media_duration(path):
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return 0.0
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


async def synthesize_beats(beats, workdir):
    """One audio file per beat, so the pictures can follow the voice exactly."""
    log("[*] Generating voiceover...")
    voices = [v for v in (TTS_VOICE, "en-NG-AbeoNeural", "en-US-ChristopherNeural") if v]
    working = None
    for i, beat in enumerate(beats):
        path = os.path.join(workdir, f"voice_{i}.mp3")
        done, last_error = False, None
        for voice in ([working] if working else voices):
            try:
                await edge_tts.Communicate(beat["speech"], voice).save(path)
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    working, done = voice, True
                    break
            except Exception as e:
                last_error = e
                log(f"[!] Voice '{voice}' failed: {type(e).__name__}: {str(e)[:120]}")
        if not done:
            raise RuntimeError(f"Voiceover failed for every voice tried. Last error: {last_error}")
        beat["voice_path"] = path
        beat["voice_dur"] = media_duration(path)
    log(f"[*] Voice in use: {working}")


def plan_durations(beats, voiceover):
    for beat in beats:
        words = len(beat["speech"].split())
        if voiceover:
            dur = max(MIN_BEAT, beat["voice_dur"] + 0.6)
        else:
            dur = min(MAX_BEAT, max(MIN_BEAT, words / 2.6 + 1.2))
        beat["dur"] = round(dur, 2)
    beats[-1]["dur"] = round(beats[-1]["dur"] + 0.8, 2)


# --- 4. FOOTAGE ---

def local_clips(category):
    for folder in (os.path.join("clips", category), "clips"):
        files = [p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith(VIDEO_EXTS)]
        if files:
            return files
    return []


def pixabay_search(query):
    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": PIXABAY_API_KEY, "q": query, "per_page": 20, "safesearch": "true"},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Pixabay API error {response.status_code}: {response.text[:200]}")
    return response.json().get("hits", [])


def pick_video_url(hit, max_bytes=60 * 1024 * 1024):
    for size in ("large", "medium", "small", "tiny"):
        info = (hit.get("videos") or {}).get(size) or {}
        url = info.get("url")
        if url and (not info.get("size") or info["size"] <= max_bytes):
            return url
    return None


def download_file(url, path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
    if os.path.getsize(path) == 0:
        raise RuntimeError("Downloaded video is empty.")


def fetch_stock_clip(keywords, category, path, used_urls):
    queries = []
    keywords = (keywords or "").strip()
    if keywords:
        queries.append(keywords)
        parts = keywords.split()
        if len(parts) > 1:
            queries += [parts[-1], parts[0]]
    queries += FALLBACK_QUERIES.get(category, ["city street"])
    seen = set()
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        urls = [u for u in (pick_video_url(h) for h in pixabay_search(query)) if u and u not in used_urls]
        if urls:
            url = random.choice(urls)
            log(f"[*] Footage for '{query}'")
            download_file(url, path)
            used_urls.add(url)
            return
    raise RuntimeError(f"No Pixabay footage found for: {queries}")


def get_clip(beat, category, workdir, index, used_urls, local_pool):
    if local_pool:
        choice = random.choice(local_pool)
        if len(local_pool) > 1:
            local_pool.remove(choice)  # avoid repeats within one video
        log(f"[*] Using your own clip: {os.path.basename(choice)}")
        return choice
    path = os.path.join(workdir, f"clip_{index}.mp4")
    fetch_stock_clip(beat["keywords"], category, path, used_urls)
    return path


# --- 5. ON-SCREEN GRAPHICS (drawn with Pillow as full-frame transparent PNGs) ---

_font_cache = {}


def find_font_path():
    candidates = [os.getenv("FONT_PATH")]
    candidates += sorted(glob.glob("fonts/*.ttf") + glob.glob("fonts/*.otf"))
    candidates += FONT_CANDIDATES
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _font(size):
    if size not in _font_cache:
        path = find_font_path()
        if not path:
            raise RuntimeError(
                "No bold TrueType font found. Put a .ttf file in a 'fonts' folder in the repo, or set FONT_PATH."
            )
        _font_cache[size] = ImageFont.truetype(path, size)
    return _font_cache[size]


def _wrap(draw, text, font, max_width):
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_text(draw, text, max_w, max_h, start, min_size=32, outline=False, max_lines=8):
    """Largest font size (down to min_size) at which the wrapped text fits the box."""
    size = start
    while True:
        font = _font(size)
        lines = _wrap(draw, text, font, max_w)
        block = "\n".join(lines)
        stroke = max(3, size // 12) if outline else 0
        spacing = int(size * 0.22)
        bbox = draw.multiline_textbbox((0, 0), block, font=font, spacing=spacing, align="center", stroke_width=stroke)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if (h <= max_h and len(lines) <= max_lines) or size <= min_size:
            return {"font": font, "block": block, "stroke": stroke, "spacing": spacing, "bbox": bbox, "w": w, "h": h}
        size -= 2


def draw_fit(draw, fit, cx, cy, fill, stroke_fill=None):
    x = cx - fit["w"] / 2 - fit["bbox"][0]
    y = cy - fit["h"] / 2 - fit["bbox"][1]
    draw.multiline_text((x, y), fit["block"], font=fit["font"], fill=fill, spacing=fit["spacing"],
                        align="center", stroke_width=fit["stroke"], stroke_fill=stroke_fill)


def draw_outlined(draw, text, cy, max_w=900, max_h=820, start=78, fill=WHITE):
    fit = fit_text(draw, text, max_w, max_h, start, min_size=38, outline=True, max_lines=7)
    draw_fit(draw, fit, W / 2, cy, fill, stroke_fill=BLACK)


def draw_subtitle(draw, text, cy=1480):
    """News-style lower third: white text on a dark translucent band."""
    fit = fit_text(draw, text, 900, 330, 52, min_size=30, max_lines=6)
    pad_x, pad_y = 32, 24
    draw.rounded_rectangle(
        (W / 2 - fit["w"] / 2 - pad_x, cy - fit["h"] / 2 - pad_y, W / 2 + fit["w"] / 2 + pad_x, cy + fit["h"] / 2 + pad_y),
        radius=24, fill=(0, 0, 0, 170),
    )
    draw_fit(draw, fit, W / 2, cy, WHITE)


def draw_date_badge(draw):
    lagos = datetime.now(timezone.utc) + timedelta(hours=1)
    text = lagos.strftime("%b %d, %Y").replace(" 0", " ")
    font = _font(34)
    tw = draw.textlength(text, font=font)
    x0, y0 = 36, 64
    draw.rounded_rectangle((x0, y0, x0 + tw + 92, y0 + 58), radius=14, fill=(0, 0, 0, 160))
    draw.rounded_rectangle((x0 + 10, y0 + 10, x0 + 48, y0 + 48), radius=8, fill=YELLOW)
    draw.text((x0 + 62, y0 + 10), text, font=font, fill=WHITE)


def draw_handle(draw, y=76):
    if not CHANNEL_HANDLE:
        return
    font = _font(32)
    tw = draw.textlength(CHANNEL_HANDLE, font=font)
    draw.text((W - 40 - tw, y), CHANNEL_HANDLE, font=font, fill=(255, 255, 255, 190))


def render_overlay(plan, index, out_path):
    """One full-frame transparent PNG per beat: every piece of on-screen text for that beat."""
    layout, beat = plan["layout"], plan["beats"][index]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    probe = draw

    if layout == "overlay":
        draw_outlined(draw, beat["caption"], cy=int(H * 0.42))
        draw_handle(draw)

    elif layout == "meme_bar":
        hook = plan["beats"][0]["caption"]
        fit = fit_text(probe, hook, 960, 250, 62, min_size=34, max_lines=4)
        draw_fit(draw, fit, W / 2, 155, BLACK)
        if index > 0:
            draw_subtitle(draw, beat["caption"], cy=1440)
        draw_handle(draw, y=340)

    elif layout == "news":
        draw_date_badge(draw)
        draw_handle(draw)
        draw_subtitle(draw, beat["caption"], cy=1500)

    elif layout == "countdown":
        if "rank" in beat:
            rank_fit = fit_text(probe, ordinal(beat["rank"]), 700, 260, 190, min_size=90, outline=True, max_lines=1)
            draw_fit(draw, rank_fit, W / 2, 260, WHITE, stroke_fill=BLACK)
            title_fit = fit_text(probe, beat["title"].upper(), 940, 360, 100, min_size=48, outline=True, max_lines=4)
            draw_fit(draw, title_fit, W / 2, 800, WHITE, stroke_fill=BLACK)
            draw_subtitle(draw, beat["caption"], cy=1480)
        else:
            draw_outlined(draw, beat["caption"], cy=int(H * 0.45))
        draw_handle(draw, y=76)

    elif layout == "verse":
        draw.rectangle((0, 0, W, H), fill=(0, 0, 0, 105))  # darkens the footage so text is readable
        if beat.get("is_verse"):
            ref_fit = fit_text(probe, beat["ref"], 860, 120, 60, min_size=40, max_lines=2)
            body_fit = fit_text(probe, beat["caption"], 860, 760, 64, min_size=34, max_lines=14)
            tag_fit = fit_text(probe, beat["translation"], 860, 60, 30, min_size=24, max_lines=1)
            gap = 30
            total_h = ref_fit["h"] + gap + body_fit["h"] + gap + tag_fit["h"]
            cy = H / 2
            top = cy - total_h / 2
            draw.rounded_rectangle((W / 2 - 480, top - 60, W / 2 + 480, top + total_h + 60), radius=40, fill=(0, 0, 0, 175))
            draw_fit(draw, ref_fit, W / 2, top + ref_fit["h"] / 2, ORANGE)
            draw_fit(draw, body_fit, W / 2, top + ref_fit["h"] + gap + body_fit["h"] / 2, WHITE)
            draw_fit(draw, tag_fit, W / 2, top + total_h - tag_fit["h"] / 2, (200, 200, 200, 255))
        else:
            draw_outlined(draw, beat["caption"], cy=int(H * 0.45), max_h=600, start=70)
        draw_handle(draw)

    img.save(out_path)


# --- 6. VIDEO ASSEMBLY (FFmpeg) ---

def segment_graph(layout):
    """Filter graph that turns any clip into a 1080x1920 background for the layout. Output label: [bg]."""
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    tail = f"setsar=1,fps={FPS},format=yuv420p"
    if layout == "meme_bar":
        return (f"[0:v]scale={W}:1300:force_original_aspect_ratio=increase,crop={W}:1300,"
                f"pad={W}:{H}:0:310:white,{tail}[bg]")
    if layout == "news":
        return (
            f"[0:v]split=2[a][b];"
            f"[a]{cover},scale=iw/12:ih/12,boxblur=3:2,scale={W}:{H}[bgblur];"
            f"[b]scale={W}:-2,crop={W}:'min(ih,1000)'[fg];"
            f"[bgblur][fg]overlay=(W-w)/2:(H-h)/2-80,{tail}[bg]"
        )
    return f"[0:v]{cover},{tail}[bg]"


def make_segment(clip_path, overlay_png, dur, layout, out_path):
    """One beat: the footage, shaped for the layout, with that beat's graphics drawn on it."""
    clip_dur = media_duration(clip_path)
    start = random.uniform(0, clip_dur - dur - 0.2) if clip_dur > dur + 0.4 else 0.0
    graph = segment_graph(layout) + ";[bg][1:v]overlay=0:0,format=yuv420p[v]"
    run_ffmpeg([
        "-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", clip_path,
        "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.2f}", "-i", overlay_png,
        "-filter_complex", graph, "-map", "[v]", "-an", "-t", f"{dur:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "6M", "-bufsize", "12M",
        "-pix_fmt", "yuv420p", out_path,
    ])


def pick_music():
    files = [p for p in glob.glob(os.path.join("music", "*")) if p.lower().endswith(AUDIO_EXTS)]
    return random.choice(files) if files else None


def assemble_video(plan, workdir, output_path, voiceover, music_path):
    log("[*] Assembling the video...")
    beats, layout = plan["beats"], plan["layout"]
    total = round(sum(b["dur"] for b in beats), 2)

    # 1. Each beat becomes a short clip with its own graphics on it
    segments = []
    for i, beat in enumerate(beats):
        seg = os.path.join(workdir, f"seg_{i}.mp4")
        make_segment(beat["clip"], beat["overlay_png"], beat["dur"], layout, seg)
        segments.append(seg)

    # 2. Join them without re-encoding
    list_file = os.path.join(workdir, "segments.txt")
    with open(list_file, "w") as f:
        for seg in segments:
            f.write("file '" + seg.replace("'", "'\\''") + "'\n")
    silent_video = os.path.join(workdir, "video_only.mp4")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", silent_video])

    # 3. Audio: the voice (one file per beat, padded to the beat), music, or silence
    args = ["-i", silent_video]
    next_index = 1
    voice_indexes = []
    if voiceover:
        for beat in beats:
            args += ["-i", beat["voice_path"]]
            voice_indexes.append(next_index)
            next_index += 1
    music_index = silent_index = None
    if music_path:
        args += ["-stream_loop", "-1", "-i", music_path]
        music_index = next_index
        next_index += 1
    if not voiceover and not music_path:
        args += ["-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
        silent_index = next_index

    filters = []
    norm = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if voiceover:
        for k, beat in enumerate(beats):
            filters.append(
                f"[{voice_indexes[k]}:a]{norm},apad=whole_dur={beat['dur']:.3f},"
                f"atrim=duration={beat['dur']:.3f},asetpts=N/SR/TB[va{k}]"
            )
        joined = "".join(f"[va{k}]" for k in range(len(beats)))
        filters.append(f"{joined}concat=n={len(beats)}:v=0:a=1[voice]")
    if music_path:
        fade_start = max(0.0, total - 1.5)
        gain = "" if voiceover else ",volume=0.6"
        filters.append(
            f"[{music_index}:a]{norm},atrim=duration={total:.3f},asetpts=N/SR/TB,"
            f"afade=t=out:st={fade_start:.2f}:d=1.5{gain}[music]"
        )
    if voiceover and music_path:
        filters.append(
            f"[voice][music]amix=inputs=2:duration=first:weights='1 {MUSIC_VOLUME}':dropout_transition=0[aout]"
        )
    elif voiceover:
        filters.append("[voice]anull[aout]")
    elif music_path:
        filters.append("[music]anull[aout]")
    else:
        filters.append(f"[{silent_index}:a]anull[aout]")

    args += [
        "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        "-t", f"{total:.2f}", output_path,
    ]
    run_ffmpeg(args)

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("FFmpeg finished but the output video is missing or empty.")
    log(f"[+] Video created: {os.path.getsize(output_path) / 1e6:.1f} MB, {total:.0f} seconds")


# --- 7. TELEGRAM ---

def build_telegram_caption(plan):
    category, layout = plan["category"], plan["layout"]
    lines = [f"🎬 New {category} video ready ({layout} style)", ""]
    if layout in ("overlay", "meme_bar"):
        lines += [f"Hook: {plan['beats'][0]['caption']}", ""]
    lines.append("Sources (check the facts before approving):")
    for s in plan["stories"][:6]:
        lines.append(f"- {s.title[:70]} ({s.source})\n  {s.link}")
    lines += ["", "Tap Approve to publish."]
    return "\n".join(lines)[:1000]


async def send_for_approval(category, caption, video_path):
    log("[*] Sending final video to Telegram for approval...")
    size = os.path.getsize(video_path)
    if size > TELEGRAM_MAX_BYTES:
        raise RuntimeError(f"Video is {size / 1e6:.1f} MB, over Telegram's 50 MB bot upload limit.")

    keyboard = [[InlineKeyboardButton("✅ Approve & Publish", callback_data=f"approve_{category}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    async with Bot(token=TELEGRAM_TOKEN) as bot:
        me = await bot.get_me()
        log(f"[*] Telegram bot connected as @{me.username}")
        with open(video_path, "rb") as video:
            message = await bot.send_video(
                chat_id=CHAT_ID, video=video, caption=caption, reply_markup=reply_markup,
                width=W, height=H, supports_streaming=True,
                read_timeout=180, write_timeout=180, connect_timeout=30,
            )
    log(f"[+] Telegram message sent (message_id={message.message_id}).")


# --- 8. MAIN ---

def find_stories(category, used, tried):
    layout = LAYOUTS[category]
    if layout == "news":
        return sources.choose_stories("News", used, 5, min_count=3, exclude=tried)
    if layout == "countdown":
        return sources.choose_stories("Entertainment", used, 5, min_count=3, exclude=tried)
    return [sources.choose_story(category, used, exclude=tried)]


def pick_content(used):
    """Finds real stories and a plan for them. If a category has nothing usable, tries the next."""
    first = peek_next_category()
    start = CATEGORIES.index(first)
    order = CATEGORIES[start:] + CATEGORIES[:start]
    problems = []
    for category in order:
        tried = set()
        for _ in range(3):
            try:
                stories = find_stories(category, used, tried)
            except sources.NoStoryError as e:
                log(f"[!] {category}: {e}")
                problems.append(f"{category}: {e}")
                break
            tried.update(s.key for s in stories)
            for s in stories:
                log(f"[*] Story for {category}: {s.title} [{s.source}]")
            try:
                return category, make_plan(category, stories)
            except StorySkipped:
                log("[*] Gemini marked this story unsuitable. Trying another.")
                for s in stories:
                    sources.mark_used(DB_FILE, s.key)
    raise RuntimeError("No suitable real story found in any category. " + " | ".join(problems))


async def main():
    workdir = tempfile.mkdtemp(prefix="postai_")
    try:
        check_env()
        genai.configure(api_key=GEMINI_API_KEY)
        setup_db()
        used = sources.load_used(DB_FILE)

        category, plan = pick_content(used)
        log(f"[*] Pipeline started for category: {category} ({plan['layout']} layout)")
        beats = plan["beats"]

        if VOICEOVER:
            await synthesize_beats(beats, workdir)
        plan_durations(beats, VOICEOVER)

        used_urls, pool = set(), local_clips(category)
        for i, beat in enumerate(beats):
            beat["clip"] = get_clip(beat, category, workdir, i, used_urls, pool)
            beat["overlay_png"] = os.path.join(workdir, f"overlay_{i}.png")
            render_overlay(plan, i, beat["overlay_png"])

        assemble_video(plan, workdir, OUTPUT_FILE, VOICEOVER, pick_music())

        await send_for_approval(category, build_telegram_caption(plan), OUTPUT_FILE)

        # Only now remember what was used
        save_category(category)
        for s in plan["stories"]:
            sources.mark_used(DB_FILE, s.key)
        sources.prune(DB_FILE)
        log("[*] Pipeline completed successfully. Shutting down.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    log("[*] Waking up PostAI...")
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        log("[!] Pipeline FAILED. See the error above.")
        sys.exit(1)  # makes the GitHub Action turn red instead of green