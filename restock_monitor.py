"""
Restock monitor for Lazada and Shopee - supports rotating authorized proxies.

Opens each product page in Chrome every few minutes, reads
the stock label / buy buttons, and sends a Telegram message when an item
becomes buyable. Rotates to the next proxy IP on any connection failure,
missing stock data, or captcha/verification challenge.

Setup (once):
    python -m pip install playwright
    python -m playwright install chromium

Set RESIDENTIAL_PROXIES to comma-separated proxy URLs before running.

Run:
    python restock_monitor.py            # keeps checking
    python restock_monitor.py --once     # single check of every item, prints details
    python restock_monitor.py --test-proxies  # test all proxy sessions from Chromium
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
    {"name": "Sylveon", "url": "https://s.lazada.com.ph/s.ZFcVDv"},
    {"name": "Greninja", "url": "https://s.lazada.com.ph/s.ZFcVEQ"},
]
CHECK_EVERY_MINUTES = 3
TARGET_DELAY_SECONDS = 60  # wait between checking consecutive targets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")  # or paste between the quotes
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")      # or paste between the quotes
BASE_PROFILE_DIR = Path(__file__).with_name("browser-profile") # base folder for isolated proxy profiles
HEADLESS = os.environ.get("HEADLESS") == "1"

# Comma-separated proxy URLs, for example:
# http://username:password@host1:10000,http://username:password@host2:10000
RESIDENTIAL_PROXIES = [
    value.strip()
    for value in os.environ.get("RESIDENTIAL_PROXIES", "").split(",")
    if value.strip()
]
# ----------------------------------------

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
    except Exception as e:
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
    dom = page.evaluate(LAZADA_JS)
    deadline = time.time() + 25
    while True:
        if re.search(r"punish|captcha", dom["url"] + " " + dom["title"], re.I):
            dom["error"] = "Lazada showed its captcha / verification page."
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
        page.wait_for_timeout(5000)
        dom = page.evaluate(LAZADA_JS)
        if OOS_RE.search(dom["quantityText"] or ""):
            return "out_of_stock", dom
        if any(BUY_RE.search(b) for b in dom["buttons"]):
            return "in_stock", dom
    dom["error"] = ("No stock label and no buy button appeared. "
                    "Proxy IP may be throttled or slow.")
    return "blocked", dom


# ---------- Shopee ----------
SHOPEE_JS = """() => {
    const buy = /^(add to cart|buy now|pre-?order.*)$/i;
    const buttons = Array.from(document.querySelectorAll('button'))
        .map(b => ({ text: (b.innerText || '').trim(),
                     disabled: b.disabled || b.getAttribute('aria-disabled') === 'true' }))
        .filter(b => buy.test(b.text));
    const text = document.body ? document.body.innerText : '';
    const m = text.match(/(\d[\d,]*)\s+pieces?\s+available/i);
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
        dom["error"] = "Shopee redirected to its login / verification page."
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
        return (f"ERROR: {site} monitor is BLOCKED on all proxies.\n"
                f"Last reason: {details.get('error') or 'unknown'}\n"
                "It will retry at the next interval.")
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


def should_rotate_proxy(details):
    """Any transport error, missing stock element, or captcha/login triggers proxy rotation."""
    error = str(details.get("error") or "").lower()
    markers = (
        "captcha",
        "punish",
        "verification",
        "login",
        "no stock label",
        "shopee product section did not load",
        "page failed to load",
        "timeout",
        "err_tunnel_connection_failed",
        "err_proxy_connection_failed",
        "err_no_supported_proxies",
        "err_proxy_certificate_invalid",
        "err_connection_reset",
        "err_connection_closed",
        "err_timed_out",
        "proxy authentication required",
        "proxy authentication failed",
        "407 proxy",
    )
    return any(marker in error for marker in markers)


def open_browser_context(playwright, proxy_index):
    proxy_config = playwright_proxy(RESIDENTIAL_PROXIES[proxy_index])
    options = {
        "headless": HEADLESS,
        "viewport": {"width": 1366, "height": 900},
    }
    if proxy_config:
        options["proxy"] = proxy_config

    profile_path = Path(f"{BASE_PROFILE_DIR}-{proxy_index + 1}")
    context = playwright.chromium.launch_persistent_context(
        str(profile_path), **options
    )
    page = context.pages[0] if context.pages else context.new_page()
    log(f"Connected browser using residential proxy "
        f"{proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}")
    return context, page


def test_proxy_route(page):
    """Test Chromium's route through the configured proxy."""
    probe_url = "https://api.ipify.org?format=json"
    try:
        response = page.goto(probe_url, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        error = str(e).splitlines()[0]
        return False, f"Proxy connection/timeout failure: {error}"

    status = response.status if response else None
    if status != 200:
        return False, f"Proxy probe failed with HTTP {status}"

    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception as e:
        return False, f"Could not read probe response: {str(e).splitlines()[0]}"

    match = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
    if match:
        return True, f"Exit IP: {match.group(1)}"

    return False, f"Invalid IP response: {body[:100]!r}"


def main():
    once = "--once" in sys.argv
    if not RESIDENTIAL_PROXIES:
        raise SystemExit(
            "Set RESIDENTIAL_PROXIES to a comma-separated list of authorized proxy URLs."
        )

    log(f"Monitor initialized with {len(RESIDENTIAL_PROXIES)} proxies. Launching Playwright...")
    last = {}
    proxy_index = 0

    with sync_playwright() as p:
        if "--test-proxies" in sys.argv:
            passed = 0
            for index in range(len(RESIDENTIAL_PROXIES)):
                ctx = None
                try:
                    log(f"Testing proxy {index + 1}/{len(RESIDENTIAL_PROXIES)} inside Chromium...")
                    ctx, probe_page = open_browser_context(p, index)
                    probe_ok, probe_message = test_proxy_route(probe_page)
                    if probe_ok is True:
                        passed += 1
                        log(f"RESULT proxy {index + 1}: PASS — {probe_message}")
                    else:
                        log(f"RESULT proxy {index + 1}: FAIL — {probe_message}")
                except Exception as e:
                    log(f"RESULT proxy {index + 1}: ERROR — {str(e).splitlines()[0]}")
                finally:
                    if ctx is not None:
                        ctx.close()
            log(f"Proxy test finished: {passed}/{len(RESIDENTIAL_PROXIES)} confirmed.")
            return

        while True:
            log(f"Starting monitoring cycle; initial proxy "
                f"{proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}")

            ctx = None
            page = None
            all_proxies_failed = False

            try:
                tested = 0
                while tested < len(RESIDENTIAL_PROXIES):
                    ctx, page = open_browser_context(p, proxy_index)
                    probe_ok, probe_message = test_proxy_route(page)

                    if probe_ok is True:
                        log(f"Proxy {proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}: CONNECTED — {probe_message}")
                        break

                    log(f"Proxy {proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}: FAILED — {probe_message}")
                    if ctx is not None:
                        ctx.close()
                        ctx = None
                        page = None

                    tested += 1
                    if tested >= len(RESIDENTIAL_PROXIES):
                        msg = (
                            f"PROXY ERROR: All {len(RESIDENTIAL_PROXIES)} configured proxies "
                            f"failed Chromium's connection test. Last result: {probe_message}"
                        )
                        log(msg)
                        send_telegram(msg)
                        all_proxies_failed = True
                        break

                    failed_proxy = proxy_index + 1
                    proxy_index = (proxy_index + 1) % len(RESIDENTIAL_PROXIES)
                    log(f"Proxy {failed_proxy} failed connection test. Moving to proxy "
                        f"{proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}.")

                if not all_proxies_failed:
                    for target_index, target in enumerate(TARGETS):
                        status, details = check(page, target)
                        log(f"{target['name']}: status={status}")

                        if should_rotate_proxy(details):
                            failed_proxy = proxy_index + 1
                            reason = details.get("error") or "unknown error"
                            log(f"Proxy {failed_proxy} blocked/failed ({reason}). Ending session and moving to next IP...")

                            if ctx is not None:
                                ctx.close()
                                ctx = None

                            attempts = 1
                            recovered = False
                            while attempts < len(RESIDENTIAL_PROXIES):
                                proxy_index = (proxy_index + 1) % len(RESIDENTIAL_PROXIES)
                                log(f"Trying proxy {proxy_index + 1}/{len(RESIDENTIAL_PROXIES)} for {target['name']}...")
                                ctx, page = open_browser_context(p, proxy_index)
                                probe_ok, probe_message = test_proxy_route(page)

                                if probe_ok is True:
                                    log(f"New proxy connected: {probe_message}")
                                    status, details = check(page, target)
                                    log(f"{target['name']} retry: status={status}")

                                    if not should_rotate_proxy(details):
                                        recovered = True
                                        break

                                log(f"Proxy {proxy_index + 1} also failed ({details.get('error') or probe_message}).")
                                if ctx is not None:
                                    ctx.close()
                                    ctx = None
                                attempts += 1

                            if not recovered:
                                msg = f"BLOCKED: All {len(RESIDENTIAL_PROXIES)} proxies failed or hit CAPTCHA on {target['name']}."
                                log(msg)
                                send_telegram(msg)
                                all_proxies_failed = True
                                break

                        if once:
                            print(json.dumps(details, indent=2, ensure_ascii=False))
                            continue

                        prev = last.get(target["url"])
                        if status != prev and not (status == "out_of_stock" and prev is None):
                            msg = message_for(target, status, details)
                            log(" ".join(msg.splitlines()))
                            send_telegram(msg)
                            if status == "in_stock":
                                beep()
                        last[target["url"]] = status

                        if target_index < len(TARGETS) - 1:
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

            proxy_index = (proxy_index + 1) % len(RESIDENTIAL_PROXIES)
            time.sleep(CHECK_EVERY_MINUTES * 60)


if __name__ == "__main__":
    main()
