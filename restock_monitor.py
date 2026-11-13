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
    try:
        page.goto(url, wait_until="load", timeout=45000)
    except Exception as e:
        return "Page failed to load: " + str(e).splitlines()[0]
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(4000)
    return None


# ---------- Lazada ----------
LAZADA_JS = """() => {
    const q = document.querySelector('#module_quantity-input');
    const btns = Array.from(document.querySelectorAll('button, .pdp-button'))
        .map(b => (b.innerText || '').trim()).filter(Boolean);
    return { url: location.href, title: document.title,
             quantityText: q ? q.innerText.trim() : null,
             buttons: Array.from(new Set(btns)).slice(0, 30) };
}"""


def check_lazada(page, url):
    err = load(page, url)
    if err:
        return "blocked", {"error": err}
    # The page first shows a placeholder; wait until Lazada's live data has filled in
    # either the "Out of stock" label or a real button (buy / wishlist).
    dom = page.evaluate(LAZADA_JS)
    deadline = time.time() + 25
    while True:
        if re.search(r"punish|captcha", dom["url"] + " " + dom["title"], re.I):
            dom["error"] = "Lazada showed its captcha / verification page."
            dom["captcha"] = True
            return "blocked", dom
        oos = bool(OOS_RE.search(dom["quantityText"] or ""))
        buy = any(BUY_RE.search(b) for b in dom["buttons"])
        if oos or buy or time.time() > deadline:
            break
        page.wait_for_timeout(1500)
        dom = page.evaluate(LAZADA_JS)
    if oos:
        return "out_of_stock", dom
    if buy:
        # read again after a pause, so a half-loaded page can't cause a false alert
        page.wait_for_timeout(5000)
        dom = page.evaluate(LAZADA_JS)
        if OOS_RE.search(dom["quantityText"] or ""):
            return "out_of_stock", dom
        if any(BUY_RE.search(b) for b in dom["buttons"]):
            return "in_stock", dom
    # Neither an "Out of stock" label nor a buy button: the live stock data never arrived.
    dom["error"] = ("The page loaded without its live stock data (no 'Out of stock' label and "
                    "no buy button). Lazada is probably throttling the monitor.")
    dom["captcha"] = True  # treat like a captcha: pause everything
    return "blocked", dom


# ---------- Shopee ----------
SHOPEE_JS = """() => {
    const buy = /^(add to cart|buy now|pre-?order.*)$/i;
    const buttons = Array.from(document.querySelectorAll('button'))
        .map(b => ({ text: (b.innerText || '').trim(),
                     disabled: b.disabled || b.getAttribute('aria-disabled') === 'true' }))
        .filter(b => buy.test(b.text));
    const text = document.body ? document.body.innerText : '';
    const m = text.match(/(\\d[\\d,]*)\\s+pieces?\\s+available/i);
    return { url: location.href, title: document.title, buyButtons: buttons,
             piecesAvailable: m ? parseInt(m[1].replace(/,/g, ''), 10) : null,
             textLength: text.length };
}"""


def check_shopee(page, url):
    err = load(page, url)
    if err:
        return "blocked", {"error": err}
    dom = page.evaluate(SHOPEE_JS)
    if re.search(r"/buyer/login|/verify/|captcha", dom["url"], re.I):
        dom["error"] = ("Shopee redirected to its login / verification page. "
                        "Log in or complete the check in the monitor's Chrome window.")
        dom["captcha"] = True
        return "blocked", dom
    if not dom["buyButtons"]:
        dom["error"] = "Shopee product section did not load (no Buy Now / Add To Cart button found)."
        return "blocked", dom
    enabled = [b for b in dom["buyButtons"] if not b["disabled"]]
    if dom["piecesAvailable"] == 0 or not enabled:
        return "out_of_stock", dom
    return "in_stock", dom


def check(page, target):
    fn = check_shopee if "shopee." in target["url"] else check_lazada
    try:
        return fn(page, target["url"])
    except Exception as e:
        return "blocked", {"error": "Script error: " + str(e).splitlines()[0]}


def message_for(target, status, details):
    site, url = target["name"], target["url"]
    title = re.sub(r"\s*\|\s*(Lazada|Shopee).*$", "", details.get("title") or "item").strip()
    if status == "in_stock":
        return f"BACK IN STOCK on {site}: {title}\n{url}"
    if status == "out_of_stock":
        return f"{site} monitor: {title} is out of stock again.\n{url}"
    if status == "blocked":
        return (f"ERROR: {site} monitor is BLOCKED and cannot see stock.\n"
                f"Reason: {details.get('error') or 'unknown'}\n"
                "Open the monitor's Chrome window on your PC and sort it out there; "
                "it retries at the next interval.")
    return (f"{site} monitor could not confirm stock, but the page no longer says "
            f"'Out of stock'. Buttons seen: {' | '.join(details.get('buttons') or []) or 'none'}. "
            f"Check it now:\n{url}")


def playwright_proxy(proxy_url):
    """Convert an HTTP(S) proxy URL into Playwright's proxy configuration."""
    if not proxy_url:
        return None

    parsed = urllib.parse.urlsplit(proxy_url)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError(
            "Each proxy must be a full URL such as "
            "http://username:password@host:port"
        )

    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username is not None:
        config["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password is not None:
        config["password"] = urllib.parse.unquote(parsed.password)
    return config


def main():
    once = "--once" in sys.argv
    if not RESIDENTIAL_PROXIES:
        raise SystemExit(
            "Set RESIDENTIAL_PROXIES to a comma-separated list of authorized proxy URLs."
        )

    last = {}
    proxy_index = 0

    with sync_playwright() as p:
        while True:
            # One proxy is used for the entire pass through TARGETS. The browser
            # context is restarted between passes so the next pass uses the next IP.
            proxy_url = RESIDENTIAL_PROXIES[proxy_index]
            proxy_config = playwright_proxy(proxy_url)
            log(f"Starting monitoring cycle with residential proxy {proxy_index + 1}/"
                f"{len(RESIDENTIAL_PROXIES)}")

            ctx = None
            captcha_on = None
            try:
                launch_options = {
                    "headless": HEADLESS,
                    "viewport": {"width": 1366, "height": 900},
                }
                if proxy_config:
                    launch_options["proxy"] = proxy_config

                ctx = p.chromium.launch_persistent_context(
                    str(PROFILE_DIR), **launch_options
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()

                for target in TARGETS:
                    status, details = check(page, target)
                    log(f"{target['name']}: status={status}")
                    if once:
                        print(json.dumps(details, indent=2, ensure_ascii=False))
                        continue
                    if details.get("captcha"):
                        captcha_on = target["name"]
                        captcha_reason = details.get("error") or "unknown"
                        break  # stop this cycle; do not switch proxies to get around a challenge
                    prev = last.get(target["url"])
                    # alert on every change, except the first "out of stock"
                    if status != prev and not (status == "out_of_stock" and prev is None):
                        msg = message_for(target, status, details)
                        log(msg.replace("\n", " "))
                        send_telegram(msg)
                        if status == "in_stock":
                            beep()
                    last[target["url"]] = status

                    # Space out requests to separate targets within the same cycle.
                    if target is not TARGETS[-1]:
                        log(f"Waiting {TARGET_DELAY_SECONDS} seconds before the next target.")
                        time.sleep(TARGET_DELAY_SECONDS)

                if once:
                    return

            except KeyboardInterrupt:
                log("Stopped.")
                return
            except Exception as e:
                log(f"Monitoring cycle failed: {e}")
                send_telegram(f"Restock monitor cycle failed: {e}")
            finally:
                if ctx is not None:
                    ctx.close()

            if captcha_on:
                msg = (f"CAPTCHA / BLOCK HIT on {captcha_on}. Checks are paused for "
                       f"{CAPTCHA_PAUSE_MINUTES} minutes.\nReason: {captcha_reason}\n"
                       "The monitor will retry using the same proxy after the pause; "
                       "it will not switch IPs to bypass the challenge.")
                log(msg)
                send_telegram(msg)
                time.sleep(CAPTCHA_PAUSE_MINUTES * 60)
                send_telegram("Restock monitor is retrying after the captcha pause.")
                # Keep the same proxy index until the challenged session recovers.
                continue

            # Advance only after a complete, non-challenged pass through all targets.
            proxy_index = (proxy_index + 1) % len(RESIDENTIAL_PROXIES)
            time.sleep(CHECK_EVERY_MINUTES * 60)


if __name__ == "__main__":
    main()
