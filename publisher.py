import os
import sys
import traceback

import requests
from dotenv import load_dotenv

load_dotenv()

VIDEO_PATH = "final_ready_video.mp4"

PUBLISHED, SKIPPED, FAILED = "published", "skipped", "failed"

# Meta Credentials
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
# Meta retires old Graph API versions. If Meta ever returns a "version" error,
# set META_GRAPH_VERSION (for example v24.0) instead of editing this file.
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION") or "v23.0"

# YouTube Credentials
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")

# Dynamic Metadata
METADATA = {
    "News": {"title": "Latest News Update 🌍", "tags": "#News #Update #Trending"},
    "Movies": {"title": "Movie Highlight of the Day 🎬", "tags": "#Movies #Film #Cinema"},
    "Entertainment": {"title": "Entertainment Highlights 🍿", "tags": "#Entertainment #PopCulture"},
    "Christianity": {"title": "Daily Devotional 🙏", "tags": "#Christianity #Faith"},
    "Trends": {"title": "What's Trending Today 📈", "tags": "#Trending #Viral"},
}

# An empty or unknown CATEGORY falls back to "News"
CATEGORY = (os.getenv("CATEGORY") or "").strip()
if CATEGORY not in METADATA:
    CATEGORY_USED = "News"
else:
    CATEGORY_USED = CATEGORY

title = METADATA[CATEGORY_USED]["title"]
tags = METADATA[CATEGORY_USED]["tags"]
description = f"{title}\n\n{tags}"


def log(msg):
    print(msg, flush=True)


def publish_to_facebook():
    """Publishes to the FB Page. (Turn ON 'Auto-Share to Instagram' in Meta Business Suite for IG.)"""
    log("[*] Starting Facebook/Instagram upload...")
    if not META_ACCESS_TOKEN or not FB_PAGE_ID:
        log("[!] Missing Meta credentials (META_ACCESS_TOKEN / FB_PAGE_ID). Skipping.")
        return SKIPPED

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE_ID}/videos"
    payload = {"title": title, "description": description, "access_token": META_ACCESS_TOKEN}

    with open(VIDEO_PATH, "rb") as f:
        response = requests.post(url, data=payload, files={"source": f}, timeout=(30, 900))

    if response.status_code == 200:
        try:
            video_id = response.json().get("id")
        except ValueError:
            video_id = None
        log(f"[+] Published to Facebook (video id: {video_id}).")
        return PUBLISHED

    log(f"[!] Meta upload failed ({response.status_code}): {response.text[:500]}")
    return FAILED


def publish_to_youtube():
    """Uses the Refresh Token to get a fresh access token, then uploads a Short."""
    log("[*] Starting YouTube upload...")
    if not all([YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN]):
        log("[!] Missing YouTube credentials. Skipping.")
        return SKIPPED

    # Imported here so a problem with the Google libraries only affects YouTube
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
    )
    # Refresh now so a bad/expired refresh token gives a clear error here
    credentials.refresh(Request())

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    body = {
        "snippet": {
            "title": f"{title} #Shorts",
            "description": description,
            "tags": tags.replace("#", "").split(),
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }

    media = MediaFileUpload(VIDEO_PATH, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    try:
        response = request.execute()
    except HttpError as e:
        detail = e.content.decode("utf-8", "replace")[:500] if e.content else str(e)
        log(f"[!] YouTube upload failed (HTTP {e.resp.status}): {detail}")
        return FAILED

    log(f"[+] Published to YouTube! Video ID: {response.get('id')}")
    return PUBLISHED


# Add new platforms here in the future
# def publish_to_tiktok(): ...


def main():
    log(f"=== PUBLISHING: {CATEGORY_USED} ===")
    if CATEGORY and CATEGORY != CATEGORY_USED:
        log(f"[!] Unknown category '{CATEGORY}', using '{CATEGORY_USED}' metadata.")

    if not os.path.exists(VIDEO_PATH):
        log(f"[!] {VIDEO_PATH} not found in the repository. Nothing to publish.")
        return 1

    results = {}
    for name, func in (("Facebook", publish_to_facebook), ("YouTube", publish_to_youtube)):
        try:
            results[name] = func()
        except Exception:
            traceback.print_exc()
            results[name] = FAILED

    log("=== SUMMARY ===")
    for name, outcome in results.items():
        log(f"  {name}: {outcome}")

    if FAILED in results.values():
        return 1  # the Action turns red instead of pretending it worked
    if PUBLISHED not in results.values():
        log("[!] Nothing was published (all platforms skipped).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())