    if proxy_config:
        options["proxy"] = proxy_config

    ctx = playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR), **options
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    log(f"Using residential proxy {proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}")
    return ctx, page


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
            log(f"Starting monitoring cycle with residential proxy "
                f"{proxy_index + 1}/{len(RESIDENTIAL_PROXIES)}")

            ctx = None
            captcha_on = None
            cycle_failed = False

            try:
                ctx, page = launch_context(p, proxy_index)

                for target_index, target in enumerate(TARGETS):
                    # Retry this same target on the next proxy only when the
                    # failure is clearly a proxy transport/authentication error.
                    proxy_attempts = 0
                    while True:
                        status, details = check(page, target)
                        log(f"{target['name']}: status={status}")

                        if not is_proxy_connection_error(details):
                            break

                        failed_proxy = proxy_index + 1
                        if ctx is not None:
                            ctx.close()
                            ctx = None

                        proxy_attempts += 1
                        if proxy_attempts >= len(RESIDENTIAL_PROXIES):
                            msg = (
                                f"PROXY ERROR: All {len(RESIDENTIAL_PROXIES)} configured "
                                f"proxies failed while checking {target['name']}. "
                                f"Last error: {details.get('error', 'unknown')}"
                            )
                            log(msg)
                            send_telegram(msg)
                            cycle_failed = True
                            break

                        proxy_index = (proxy_index + 1) % len(RESIDENTIAL_PROXIES)
                        msg = (
                            f"Proxy {failed_proxy}/{len(RESIDENTIAL_PROXIES)} failed "
                            f"while checking {target['name']}. Trying proxy "
                            f"{proxy_index + 1}/{len(RESIDENTIAL_PROXIES)} and retrying "
                            f"the same target."
                        )
                        log(msg)
                        send_telegram(msg)
                        ctx, page = launch_context(p, proxy_index)

                    if cycle_failed:
                        break

                    if once:
                        print(json.dumps(details, indent=2, ensure_ascii=False))
                        continue

                    # CAPTCHA and verification challenges do not trigger proxy
                    # rotation; the monitor pauses rather than bypassing them.
                    if details.get("captcha"):
                        captcha_on = target["name"]
                        captcha_reason = details.get("error") or "unknown"
                        break

                    prev = last.get(target["url"])
                    if status != prev and not (status == "out_of_stock" and prev is None):
                        msg = message_for(target, status, details)
                        log(msg.replace("\n", " "))
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
                cycle_failed = True
            finally:
                if ctx is not None:
                    ctx.close()

            if captcha_on:
                msg = (f"CAPTCHA / VERIFICATION CHALLENGE on {captcha_on}. "
                       f"Checks are paused for {CAPTCHA_PAUSE_MINUTES} minutes.\n"
                       f"Reason: {captcha_reason}\n"
                       "The monitor will not switch proxies to bypass a challenge.")
                log(msg)
                send_telegram(msg)
                time.sleep(CAPTCHA_PAUSE_MINUTES * 60)
                send_telegram("Restock monitor is retrying after the challenge pause.")
                continue

            # Continue the round-robin sequence after the last proxy used.
            proxy_index = (proxy_index + 1) % len(RESIDENTIAL_PROXIES)
            time.sleep(CHECK_EVERY_MINUTES * 60)
