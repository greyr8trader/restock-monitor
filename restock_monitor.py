"""
Restock monitor for Lazada and Shopee - supports rotating authorized proxies.

Opens each product page in Chrome every few minutes, reads
the stock label / buy buttons, and sends a Telegram message when an item
becomes buyable. Also sends a Telegram error if a site blocks it.

Setup (once):
    python -m pip install playwright
    python -m playwright install chromium

Set RESIDENTIAL_PROXIES to comma-separated proxy URLs before running.

Run:
    python restock_monitor.py            # keeps checking
    python restock_monitor.py --once     # single check of every item, prints details

If a site shows a slider/captcha, the script sends a Telegram message, pauses
all checks for CAPTCHA_PAUSE_MINUTES, then messages again when it restarts.
Solve the slider yourself in the monitor's Chrome window; cookies are kept.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ---------------- CONFIG ----------------
TARGETS = [
    {"name": "Lazada ETB", "url": "https://www.lazada.com.ph/products/pdp-i15615776535-s133613012504.html"},
    {"name": "Lazada Booster Bundle", "url": "https://www.lazada.com.ph/products/pokemon-tcg-30th-celebration-booster-bundle-i15642615145-s133705007888.html"},
]
CHECK_EVERY_MINUTES = 3
TARGET_DELAY_SECONDS = 60  # wait between checking consecutive targets
CAPTCHA_PAUSE_MINUTES = 30   # when a captcha appears, stop all checks for this long
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")  # or paste between the quotes
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")      # or paste between the quotes
PROFILE_DIR = Path(__file__).with_name("browser-profile")      # keeps cookies/logins between runs
HEADLESS = os.environ.get("HEADLESS") == "1"

# Comma-separated proxy URLs, for example:
# http://username:password@host1:10000,http://username:password@host2:10000
# Keep credentials in environment variables, not in source control.
RESIDENTIAL_PROXIES = [
    value.strip()
    for value in os.environ.get("RESIDENTIAL_PROXIES", "").split(",")
    if value.strip()
]
# ----------------------------------------

# Optional: put the bot token on line 1 and the chat ID on line 2 of a file named
# telegram.txt next to this script. Then script updates never wipe your settings.
_tg_file = Path(__file__).with_name("telegram.txt")
if _tg_file.exists():
    _lines = [l.strip() for l in _tg_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(_lines) >= 2:
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = _lines[0], _lines[1]

BUY_RE = re.compile(r"add to cart|buy now|pre-?order", re.I)
OOS_RE = re.compile(r"out of stock|sold out", re.I)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log("Telegram not configured; alert shown here only.")
        return
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=20).read()
    except Exception as e:  # never let a failed alert stop the monitor
        log(f"Telegram send failed: {e}")


def beep():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 400)
    except Exception:
        print("\a", end="", flush=True)


def load(page, url):
    """Open the page and give it time to load its live data. Returns an error string or None."""
