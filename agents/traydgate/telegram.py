"""
Telegram output layer for TraydGate agents.

Sends messages to designated channels via the Telegram Bot API.
Bot token sourced from TELEGRAM_BOT_TOKEN in .env.
Channel IDs sourced from config/channels.json.
"""
import json
import os
import urllib.request
import urllib.error
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHANNELS_PATH = os.path.join(REPO_ROOT, "config", "channels.json")


def _load_channels():
    with open(CHANNELS_PATH) as f:
        return json.load(f)


def _get_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")
    return token


def send_message(channel_id: int, text: str, parse_mode: str = "Markdown") -> dict:
    """Send a message to a Telegram channel. Returns the API response."""
    token = _get_token()
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = json.dumps({
        "chat_id": channel_id,
        "text": text,
        "parse_mode": parse_mode,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Telegram API error {e.code}: {body}") from e


def send_to_nexus(text: str) -> dict:
    """Send a message to the TraydGate Research channel (NEXUS output)."""
    channels = _load_channels()
    channel_id = channels["traydgate"]["nexus_research"]
    return send_message(channel_id, text)


def send_to_prism(text: str) -> dict:
    """Send a message to the TraydGate PM channel (PRISM output)."""
    channels = _load_channels()
    channel_id = channels["traydgate"]["prism_pm"]
    return send_message(channel_id, text)
