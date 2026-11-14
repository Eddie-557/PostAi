"""
Checks Telegram for "Approve & Publish" button presses and starts the
publisher workflow on GitHub (repository_dispatch event "publish_video").

Runs inside a short GitHub Action every few minutes. Telegram keeps unconfirmed
button presses for 24 hours, so no database or state file is needed: we read
the pending presses, act on them, then confirm them so they are not seen again.
"""
import os
import sys
import traceback

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional here
    pass

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # provided automatically inside GitHub Actions
GITHUB_REPO = os.getenv("GITHUB_REPOSITORY") or "Eddie-557/PostAi"  # set automatically in Actions

CATEGORIES = ["News", "Movies", "Entertainment", "Christianity", "Trends"]
APPROVE_PREFIX = "approve_"  # must match callback_data in main.py


def log(msg):
    print(msg, flush=True)


# --- Telegram helpers (errors never include the URL, because the URL contains the bot token) ---

def tg_call(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        response = requests.post(url, json=params, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Telegram {method} failed: {type(e).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Telegram {method} returned a non-JSON reply (HTTP {response.status_code})"
        ) from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} error: {data.get('description')}")
    return data.get("result")


def get_updates():
    return tg_call("getUpdates", timeout=0, limit=100, allowed_updates=["callback_query"])


def confirm_updates(last_update_id):
    """Telling Telegram the next offset marks everything up to last_update_id as handled."""
    tg_call("getUpdates", offset=last_update_id + 1, limit=1, timeout=0,
            allowed_updates=["callback_query"])


def best_effort(description, method, **params):
    try:
        tg_call(method, **params)
    except Exception as e:
        log(f"[!] Could not {description}: {e}")


# --- Reading button presses ---

def extract_approval(update):
    """Returns a dict for a valid approval from the authorised chat, otherwise None."""
    cq = update.get("callback_query")
    if not cq:
        return None
    data = cq.get("data") or ""
    if not data.startswith(APPROVE_PREFIX):
        log("[*] Ignoring a button press that is not an approval.")
        return None

    category = data[len(APPROVE_PREFIX):]
    if category not in CATEGORIES:
        log(f"[!] Ignoring approval with unknown category '{category}'.")
        return None

    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if str(chat_id) != str(CHAT_ID):
        log("[!] Ignoring an approval that did not come from the authorised chat.")
        return None

    return {
        "update_id": update["update_id"],
        "query_id": cq.get("id"),
        "category": category,
        "chat_id": chat_id,
        "message_id": message.get("message_id"),
    }


# --- Starting the publisher workflow ---

def dispatch_publish(category):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"event_type": "publish_video", "client_payload": {"category": category}}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"GitHub dispatch failed: {type(e).__name__}") from None
    if response.status_code != 204:
        raise RuntimeError(
            f"GitHub dispatch failed (HTTP {response.status_code}): {response.text[:300]}"
        )


# --- Main ---

def main():
    missing = [
        name for name, value in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN),
            ("TELEGRAM_CHAT_ID", CHAT_ID),
            ("GITHUB_TOKEN", GITHUB_TOKEN),
        ] if not value
    ]
    if missing:
        log(f"[!] Missing environment variables/secrets: {', '.join(missing)}")
        return 1

    updates = get_updates()
    if not updates:
        log("[*] No pending button presses.")
        return 0
    log(f"[*] {len(updates)} pending update(s) from Telegram.")

    updates.sort(key=lambda u: u["update_id"])
    last_update_id = updates[-1]["update_id"]
    approvals = [a for a in (extract_approval(u) for u in updates) if a]

    if not approvals:
        confirm_updates(last_update_id)
        log("[*] Nothing to approve. Cleared the pending updates.")
        return 0

    # Only one video exists in the repo at a time, so publish once per run (the latest approval).
    chosen = approvals[-1]
    log(f"[*] Approval received for '{chosen['category']}'. Starting the publisher workflow...")
    dispatch_publish(chosen["category"])  # raises on failure: nothing is confirmed, so it is retried
    log("[+] Publisher workflow requested.")

    # Confirm right away so a later hiccup cannot cause a second dispatch
    confirm_updates(last_update_id)

    for a in approvals:
        if a is chosen:
            toast = f"Approved! Publishing {a['category']} now."
        else:
            toast = "Skipped: a newer approval was already sent."
        best_effort("answer the button press", "answerCallbackQuery",
                    callback_query_id=a["query_id"], text=toast)
        if a["chat_id"] is not None and a["message_id"] is not None:
            best_effort("remove the button", "editMessageReplyMarkup",
                        chat_id=a["chat_id"], message_id=a["message_id"],
                        reply_markup={"inline_keyboard": []})

    best_effort("send the confirmation message", "sendMessage", chat_id=CHAT_ID,
                text=f"✅ Approved: publishing the {chosen['category']} video now.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        log("[!] Approval listener FAILED. See the error above.")
        sys.exit(1)