import os
import asyncio
import sqlite3
import requests
import edge_tts
import google.generativeai as genai
from dotenv import load_dotenv
from moviepy.editor import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip
import moviepy.video.fx.all as vfx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# Load environment variables (works locally via .env, and seamlessly on GitHub via Secrets)
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([GEMINI_API_KEY, PIXABAY_API_KEY, TELEGRAM_TOKEN, CHAT_ID]):
    raise ValueError("Missing one or more API keys in environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

CATEGORIES = ["News", "Movies", "Entertainment", "Christianity", "Trends"]
DB_FILE = "automation.db"
TTS_VOICE = "en-US-ChristopherNeural"
TEMP_AUDIO = "temp_audio.mp3"
TEMP_VIDEO = "temp_video.mp4"
OUTPUT_FILE = "final_ready_video.mp4"

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
        cursor.execute('INSERT INTO rotation_state (last_category) VALUES (?)', (CATEGORIES[-1],))
    conn.commit()
    conn.close()

def get_next_category():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT last_category FROM rotation_state WHERE id = 1')
    last_category = cursor.fetchone()[0]
    
    try:
        last_index = CATEGORIES.index(last_category)
        next_index = (last_index + 1) % len(CATEGORIES)
    except ValueError:
        next_index = 0
        
    next_category = CATEGORIES[next_index]
    cursor.execute('UPDATE rotation_state SET last_category = ? WHERE id = 1', (next_category,))
    conn.commit()
    conn.close()
    return next_category

def generate_script(category):
    print(f"[*] Generating script for category: {category}...")
    model = genai.GenerativeModel('gemini-1.5-flash-latest')
    prompt = f"""
    Write a highly engaging, 45-second video script for this category: {category}.
    Provide ONLY the spoken text. Do not include visual directions, stage directions, or timestamps.
    The text must be ready to be read directly by an AI voiceover engine.
    """
    response = model.generate_content(prompt)
    return response.text.strip()

# --- 2. MEDIA GENERATOR ENGINE ---

async def generate_audio(text, output_filename):
    print("[*] Generating AI Voiceover...")
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(output_filename)

def download_background_video(category, output_filename):
    print(f"[*] Downloading Pixabay video for '{category}'...")
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={category}&per_page=3"
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        if data.get("hits") and len(data["hits"]) > 0:
            hit = data["hits"][0]
            video_url = None
            for size in ['large', 'medium', 'small', 'tiny']:
                if size in hit['videos'] and hit['videos'][size]['url']:
                    video_url = hit['videos'][size]['url']
                    break
            if not video_url:
                raise Exception("Could not find a valid video URL.")
            
            vid_data = requests.get(video_url)
            with open(output_filename, 'wb') as f:
                f.write(vid_data.content)
        else:
            raise Exception(f"No videos found for {category}")
    else:
        raise Exception(f"Pixabay API Error: {response.text}")

def assemble_video(audio_path, video_path, category, output_path):
    print("[*] Assembling final video...")
    audio_clip = AudioFileClip(audio_path)
    video_clip = VideoFileClip(video_path)
    
    if video_clip.duration < audio_clip.duration:
        video_clip = vfx.loop(video_clip, duration=audio_clip.duration)
    else:
        video_clip = video_clip.subclip(0, audio_clip.duration)
        
    video_clip = video_clip.set_audio(audio_clip)
    video_clip = video_clip.resize(height=1920).crop(x_center=video_clip.w/2, width=1080)
    
    txt_clip = TextClip(
        f"{category.upper()} UPDATE", 
        fontsize=90, 
        color='white', 
        font='Arial-Bold',
        bg_color='rgba(0,0,0,0.6)',
        method='caption',
        size=(video_clip.w * 0.8, None)
    )
    txt_clip = txt_clip.set_position('center').set_duration(audio_clip.duration)
    
    final_video = CompositeVideoClip([video_clip, txt_clip])
    final_video.write_videofile(
        output_path, fps=24, codec="libx264", audio_codec="aac", preset="ultrafast", threads=4
    )

# --- 3. TELEGRAM MESSENGER ---

async def send_for_approval(category, script_text, video_path):
    print(f"[*] Sending final video to Telegram for approval...")
    bot = Bot(token=TELEGRAM_TOKEN)
    keyboard = [[InlineKeyboardButton("✅ Approve & Publish", callback_data=f"approve_{category}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = f"🎬 *New Video Ready: {category}*\n\n*Script Preview:*\n{script_text[:150]}...\n\nTap Approve to publish to all platforms."
    
    with open(video_path, 'rb') as video:
        await bot.send_video(
            chat_id=CHAT_ID,
            video=video,
            caption=caption,
            parse_mode='Markdown',
            reply_markup=reply_markup,
            read_timeout=120,
            write_timeout=120
        )

# --- 4. MAIN PIPELINE EXECUTOR ---

async def main():
    try:
        # Step 1: Prep database and get category
        setup_db()
        target_category = get_next_category()
        print(f"[*] Pipeline started for category: {target_category}")
        
        # Step 2: Generate text script
        video_script = generate_script(target_category)
        
        # Step 3: Generate media files
        await generate_audio(video_script, TEMP_AUDIO)
        download_background_video(target_category, TEMP_VIDEO)
        
        # Step 4: Stitch it together
        assemble_video(TEMP_AUDIO, TEMP_VIDEO, target_category, OUTPUT_FILE)
        
        # Step 5: Send for human approval
        await send_for_approval(target_category, video_script, OUTPUT_FILE)
        
        print("[*] Pipeline completed successfully. Shutting down.")
        
    except Exception as e:
        print(f"[!] Error in pipeline: {e}")
        
    finally:
        # Cleanup temp files before the GitHub server shuts down
        if os.path.exists(TEMP_AUDIO): os.remove(TEMP_AUDIO)
        if os.path.exists(TEMP_VIDEO): os.remove(TEMP_VIDEO)

if __name__ == "__main__":
    asyncio.run(main())