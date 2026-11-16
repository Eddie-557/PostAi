"""
Finds REAL, current material for each category, so the videos never rely on
Gemini making facts up.

  News / Movies / Entertainment -> Nigerian news and entertainment RSS feeds
  Trends                        -> Google Trends "Trending now" for Nigeria (with its linked news)
  Christianity                  -> a Bible verse from bible-api.com (public-domain translation)

NOTE ON THE FEED LIST: the addresses below come from public feed directories.
They have NOT been checked from GitHub's servers (some sites block them).
The code tries several feeds per category and prints which ones worked, so edit
the FEEDS list freely: remove dead ones, add your own.
"""
import html
import random
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

USER_AGENT = "Mozilla/5.0 (compatible; PostAI/1.0)"
MAX_AGE_HOURS = 72          # only use stories from the last 3 days
MAX_VERSE_CHARS = 260       # keeps the on-screen verse readable

# (kind, url, display name). kind is "rss" or "trends".
FEEDS = {
    "News": [
        ("rss", "https://www.vanguardngr.com/feed/", "Vanguard"),
        ("rss", "https://www.premiumtimesng.com/feed", "Premium Times"),
        ("rss", "https://dailypost.ng/feed/", "Daily Post"),
        ("rss", "https://thenewsguru.com/feed", "The NewsGuru"),
        ("rss", "https://dailyreport.ng/politics/feed", "Daily Report"),
    ],
    "Movies": [
        ("rss", "https://www.bellanaija.com/nollywood/feed", "BellaNaija Nollywood"),
        ("rss", "https://kemifilani.ng/movies/feed", "Kemi Filani Movies"),
        ("rss", "https://nollycritic.com/feed", "Nollycritic"),
    ],
    "Entertainment": [
        ("rss", "https://www.bellanaija.com/feed", "BellaNaija"),
        ("rss", "https://www.lindaikejisblog.com/feed", "Linda Ikeji's Blog"),
        ("rss", "https://gistvic.com/feed", "Gistvic"),
        ("rss", "https://naijamixed.com.ng/feed", "Naijamixed"),
    ],
    "Trends": [
        ("trends", "https://trends.google.com/trending/rss?geo=NG", "Google Trends Nigeria"),
        ("rss", "https://thenewsguru.com/feed", "The NewsGuru"),
    ],
    "Christianity": [],  # handled by Bible verses below
}

# Well-known passages. Only the REFERENCE is stored here; the text always comes
# from the API, so nothing is quoted from memory.
VERSES = [
    "John 3:16", "Psalm 23:1-3", "Philippians 4:13", "Jeremiah 29:11",
    "Proverbs 3:5-6", "Romans 8:28", "Isaiah 41:10", "Matthew 6:33",
    "Psalm 46:1", "Joshua 1:9", "2 Timothy 1:7", "Psalm 121:1-2",
    "Matthew 11:28", "Philippians 4:6-7", "Romans 12:2", "Psalm 27:1",
    "Isaiah 40:31", "Hebrews 11:1", "Psalm 91:1-2", "Proverbs 18:10",
    "Galatians 6:9", "Lamentations 3:22-23", "Psalm 37:4", "Matthew 5:16",
    "James 1:5", "Colossians 3:23", "Psalm 118:24", "1 Peter 5:7",
    "Ephesians 2:8-9", "Proverbs 16:3",
]

# Stories containing these are skipped: no jokes on tragedy.
SENSITIVE = re.compile(
    r"\b(kill(ed|s|ing)?|dead|death|dies|died|dying|murder(ed|s)?|rape[ds]?|abduct(ed|ion)?|"
    r"kidnap(ped|ping)?|bomb(s|ing)?|blast|explosion|shooting|shot|gunmen|bandits?|terror\w*|"
    r"massacre|crash(ed)?|accident|flood(s|ed|ing)?|collapse[ds]?|fire outbreak|burnt|"
    r"suicide|drown(ed|ing)?|funeral|burial|tragedy|victims?|injur(ed|y|ies)|"
    r"assault(ed)?|abuse[ds]?|stabbed|lynch\w*|riot\w*|attack(ed|s)?|"
    r"mourn\w*|condolence\w*|cancer|outbreak|epidemic)\b",
    re.IGNORECASE,
)


class NoStoryError(Exception):
    """No suitable real story could be found."""


@dataclass
class Story:
    category: str
    title: str
    summary: str
    link: str
    source: str
    published: datetime | None
    key: str


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(parent, *names):
    for child in parent:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def clean_html(raw):
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw or "")
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"The post .{0,200}? appeared first on .{0,100}", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _entry_link(entry):
    text = _child_text(entry, "link")
    if text:
        return text
    for child in entry:  # Atom style: <link href="...">
        if _local(child.tag) == "link" and child.get("href"):
            if child.get("rel") in (None, "alternate"):
                return child.get("href")
    return ""


def parse_feed(xml_bytes, source_name, category):
    """Reads RSS 2.0 or Atom and returns a list of Story objects."""
    root = ET.fromstring(xml_bytes)
    stories = []
    for entry in root.iter():
        if _local(entry.tag) not in ("item", "entry"):
            continue
        title = clean_html(_child_text(entry, "title"))
        link = _entry_link(entry).strip()
        summary = clean_html(_child_text(entry, "description", "summary", "content", "encoded"))
        published = parse_date(_child_text(entry, "pubDate", "published", "updated", "date"))
        if not title or not link:
            continue
        stories.append(Story(category, title, summary[:700], link, source_name, published, link))
    return stories


def parse_trends(xml_bytes, source_name, category):
    """Google Trends RSS: each trend carries linked news items, which give us real facts."""
    root = ET.fromstring(xml_bytes)
    stories = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title = clean_html(_child_text(item, "title"))
        traffic = _child_text(item, "approx_traffic")
        published = parse_date(_child_text(item, "pubDate"))

        news = []
        for child in item:
            if _local(child.tag) == "news_item":
                n_title = clean_html(_child_text(child, "news_item_title"))
                n_snip = clean_html(_child_text(child, "news_item_snippet"))
                n_url = _child_text(child, "news_item_url")
                n_src = clean_html(_child_text(child, "news_item_source"))
                if n_title:
                    news.append((n_title, n_snip, n_url, n_src))
        if not title or not news:
            continue  # a bare search term has no facts to build on

        parts = [f"{t} ({s}): {sn}" if sn else f"{t} ({s})" for t, sn, _, s in news[:3]]
        summary = f"Trending in Nigeria (about {traffic or 'many'} searches). Related news: " + " | ".join(parts)
        link = next((u for _, _, u, _ in news if u), "")
        if not link:
            continue
        stories.append(Story(category, title, summary[:700], link, source_name, published, link))
    return stories


def is_safe_for_humor(text):
    return SENSITIVE.search(text or "") is None


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_url(url):
    response = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=20)
    response.raise_for_status()
    return response.content


def fetch_candidates(category):
    candidates = []
    feeds = list(FEEDS.get(category, []))
    random.shuffle(feeds)
    for kind, url, name in feeds:
        try:
            content = fetch_url(url)
            parser = parse_trends if kind == "trends" else parse_feed
            found = parser(content, name, category)
            log(f"[*] Feed OK: {name} ({len(found)} items)")
            candidates.extend(found)
        except Exception as e:
            log(f"[!] Feed failed: {name}: {type(e).__name__}: {str(e)[:120]}")
    return candidates


def fetch_verse(reference):
    url = "https://bible-api.com/" + reference.replace(" ", "+")
    response = requests.get(url, params={"translation": "web"}, headers={"User-Agent": USER_AGENT}, timeout=20)
    response.raise_for_status()
    data = response.json()
    text = " ".join((data.get("text") or "").split())
    if not text:
        raise ValueError(f"Empty verse text for {reference}")
    if len(text) > MAX_VERSE_CHARS:
        raise ValueError(f"{reference} is too long for the screen ({len(text)} characters)")
    ref = data.get("reference") or reference
    name = data.get("translation_name") or "World English Bible"
    return Story("Christianity", ref, text, url, f"{name} (public domain)", None, f"verse:{reference}")


def choose_verse(used, exclude):
    pool = [r for r in VERSES if f"verse:{r}" not in used and f"verse:{r}" not in exclude]
    if not pool:  # everything used once: reuse the least recently used
        pool = sorted(
            (r for r in VERSES if f"verse:{r}" not in exclude),
            key=lambda r: used.get(f"verse:{r}", ""),
        )[:5]
    random.shuffle(pool)
    last_error = None
    for ref in pool[:6]:
        try:
            return fetch_verse(ref)
        except Exception as e:
            last_error = e
            log(f"[!] Could not use verse {ref}: {type(e).__name__}: {str(e)[:120]}")
    raise NoStoryError(f"No Bible verse could be fetched ({last_error})")


def usable_stories(category, used, exclude=()):
    """All real, recent, unused, safe stories for the category, newest first."""
    candidates = fetch_candidates(category)
    if not candidates:
        raise NoStoryError(f"no feed for '{category}' returned any items")

    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=MAX_AGE_HOURS)
    usable = []
    for s in candidates:
        if s.key in used or s.key in exclude:
            continue
        if s.published and now - s.published > max_age:
            continue
        if len(s.title) < 15:
            continue
        if not is_safe_for_humor(f"{s.title} {s.summary}"):
            continue
        usable.append(s)

    if not usable:
        raise NoStoryError(f"no recent, unused, suitable story found for '{category}'")

    floor = datetime.min.replace(tzinfo=timezone.utc)
    usable.sort(key=lambda s: s.published or floor, reverse=True)
    return usable


def choose_story(category, used, exclude=()):
    """Returns one real story for the category."""
    if category == "Christianity":
        return choose_verse(used, set(exclude))
    return random.choice(usable_stories(category, used, exclude)[:8])  # a little variety


def _title_words(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3}


def _similar(a, b):
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def choose_stories(category, used, count, min_count=3, exclude=()):
    """Several different real stories (for roundups and countdowns), newest first.
    Skips near-duplicates, such as two outlets reporting the same event."""
    picked = []
    for s in usable_stories(category, used, exclude):
        if any(_similar(s.title, p.title) for p in picked):
            continue
        picked.append(s)
        if len(picked) == count:
            break
    if len(picked) < min_count:
        raise NoStoryError(f"only {len(picked)} distinct stories found for '{category}' (need {min_count})")
    return picked


# --------------------------------------------------------------------------
# Remembering what was already used (stored in automation.db)
# --------------------------------------------------------------------------

def ensure_tables(db_file):
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE IF NOT EXISTS used_stories (story_key TEXT PRIMARY KEY, used_at TEXT)")
    conn.commit()
    conn.close()


def load_used(db_file):
    conn = sqlite3.connect(db_file)
    rows = conn.execute("SELECT story_key, used_at FROM used_stories").fetchall()
    conn.close()
    return {k: v for k, v in rows}


def mark_used(db_file, key):
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT OR REPLACE INTO used_stories (story_key, used_at) VALUES (?, ?)",
        (key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def prune(db_file, keep_days=60):
    """Forget old news links (verses are kept so they cycle fairly)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    conn = sqlite3.connect(db_file)
    conn.execute("DELETE FROM used_stories WHERE used_at < ? AND story_key NOT LIKE 'verse:%'", (cutoff,))
    conn.commit()
    conn.close()