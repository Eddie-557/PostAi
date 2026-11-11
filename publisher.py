import os
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

CATEGORY = os.getenv("CATEGORY", "News")
VIDEO_PATH = "final_ready_video.mp4"

# Meta Credentials
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")

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
    "Trends": {"title": "What's Trending Today 📈", "tags": "#Trending #Viral"}
}

title = METADATA.get(CATEGORY, METADATA["News"])["title"]
tags = METADATA.get(CATEGORY, METADATA["News"])["tags"]
description = f"{title}\n\n{tags}"

def publish_to_facebook():
    """Publishes to FB Page. (Ensure 'Auto-Share to Instagram' is ON in Meta Business Suite)."""
    print("[*] Starting Facebook/Instagram upload...")
    if not META_ACCESS_TOKEN or not FB_PAGE_ID:
        print("[!] Missing Meta credentials. Skipping.")
        return

    url = f"https://graph.facebook.com/v18.0/{FB_PAGE_ID}/videos"
    payload = {'title': title, 'description': description, 'access_token': META_ACCESS_TOKEN}
    
    with open(VIDEO_PATH, 'rb') as f:
        response = requests.post(url, data=payload, files={'source': f})
        
    if response.status_code == 200:
        print("[+] Successfully published to Facebook (and Instagram via Auto-Share)!")
    else:
        print(f"[!] Meta upload failed: {response.text}")

def publish_to_youtube():
    """Uses the Refresh Token to get a fresh access token and publish a Short."""
    print("[*] Starting YouTube upload...")
    if not all([YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN]):
        print("[!] Missing YouTube credentials. Skipping.")
        return

    # 1. Automatically refresh the access token
    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET
    )

    # 2. Build the YouTube API client
    youtube = build("youtube", "v3", credentials=credentials)

    # 3. Upload the video
    body = {
        "snippet": {
            "title": f"{title} #Shorts",
            "description": description,
            "tags": tags.replace("#", "").split(),
            "categoryId": "22"
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    }

    media = MediaFileUpload(VIDEO_PATH, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    print(f"[+] Successfully published to YouTube! Video ID: {response.get('id')}")

# Add new platforms here in the future
# def publish_to_tiktok(): ...

if __name__ == "__main__":
    print(f"=== PUBLISHING: {CATEGORY} ===")
    publish_to_facebook()
    publish_to_youtube()