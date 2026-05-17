"""
Garmin OAuth background worker.
Module-level state persists across HTTP requests within the same worker process.
"""

import asyncio
import json
import os
import re
import shutil
import threading
import time
from urllib.parse import urlencode

import i18n

_SSO = "https://sso.garmin.com/sso"
_ANDROID = "https://mobile.integration.garmin.com/gcm/android"
_SIGNIN_URL = f"{_SSO}/signin?" + urlencode({
    "id":                              "gauth-widget",
    "embedWidget":                     "true",
    "gauthHost":                       _SSO,
    "service":                         _ANDROID,
    "source":                          "https://connect.garmin.com/signin/",
    "redirectAfterAccountLoginUrl":    _ANDROID,
    "redirectAfterAccountCreationUrl": _ANDROID,
})

# Module-level state — shared across HTTP requests in the same worker process
_state: dict = {
    "status":       "idle",  # idle | running | mfa_needed | success | error
    "error":        "",
    "mfa_code":     None,    # None = not yet submitted
    "display_name": "",
}
_thread: threading.Thread | None = None


def get_status() -> dict:
    return dict(_state)


def start_auth(email: str, password: str, session_dir: str) -> None:
    global _thread
    _state.update({"status": "running", "error": "", "mfa_code": None, "display_name": ""})
    # ContextVars don't propagate to new threads — capture the request's
    # locale here and re-set it inside the worker so error messages use
    # the user's chosen language.
    locale = i18n.current_locale()
    _thread = threading.Thread(
        target=_auth_worker, args=(email, password, session_dir, locale), daemon=True
    )
    _thread.start()


def submit_mfa(code: str) -> None:
    _state["mfa_code"] = code.strip()


def reset() -> None:
    global _thread
    _state.update({"status": "idle", "error": "", "mfa_code": None, "display_name": ""})
    _thread = None


def _auth_worker(email: str, password: str, session_dir: str, locale: str = "") -> None:
    if locale:
        i18n.set_request_locale(locale)
    # Playwright needs an event loop on non-main threads
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        from playwright.sync_api import sync_playwright
        from garth.http import Client
        from garth.sso import get_oauth1_token, exchange

        ticket_holder: list[str] = []

        with sync_playwright() as p:
            # --no-sandbox required when running as root inside Docker;
            # --disable-dev-shm-usage avoids /dev/shm OOMs in small containers
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ))
            page = ctx.new_page()

            def _capture(route):
                url = route.request.url
                if "ticket=" in url:
                    ticket_holder.append(url)
                try:
                    route.abort()
                except Exception:
                    pass

            page.route("https://mobile.integration.garmin.com/**", _capture)
            page.goto(_SIGNIN_URL)
            page.wait_for_load_state("networkidle")
            page.fill('input[name="username"]', email)
            page.fill('input[name="password"]', password)
            page.click("#login-btn-signin")

            # Wait for ticket redirect or MFA page
            deadline = time.time() + 30
            while time.time() < deadline:
                if ticket_holder:
                    break
                cur = page.url
                if "verifyMFA" in cur or "loginEnterMfaCode" in cur:
                    break
                page.wait_for_timeout(300)

            if not ticket_holder:
                if "verifyMFA" not in page.url and "loginEnterMfaCode" not in page.url:
                    browser.close()
                    _state["status"] = "error"
                    _state["error"] = i18n.t("ui.auth.login_failed", page=page.url[:80])
                    return

                # Signal the UI to show the MFA input, then wait
                _state["status"] = "mfa_needed"
                mfa_deadline = time.time() + 120
                while time.time() < mfa_deadline:
                    if _state.get("mfa_code"):
                        break
                    time.sleep(0.3)
                else:
                    browser.close()
                    _state["status"] = "error"
                    _state["error"] = i18n.t("ui.auth.mfa_timeout")
                    return

                mfa_code = _state["mfa_code"]

                # Fill MFA — single-char grid first, then single input box
                all_inputs = page.query_selector_all("input")
                visible = [i for i in all_inputs if i.is_visible()]
                single = [
                    i for i in visible
                    if i.get_attribute("maxlength") == "1"
                    and i.get_attribute("type") not in
                    ("hidden", "password", "email", "submit", "button")
                ]
                if len(single) >= len(mfa_code):
                    for inp, digit in zip(single, mfa_code):
                        inp.click()
                        inp.fill(digit)
                else:
                    filled = False
                    for sel in [
                        'input[name="mfa-code"]', 'input[id="mfa-code"]',
                        'input[name="otpCode"]',  'input[id="otpCode"]',
                        'input[name="mfaCode"]',  'input[name="code"]',
                        'input[type="tel"]', 'input[type="number"]', 'input[type="text"]',
                    ]:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.click()
                            el.fill(mfa_code)
                            filled = True
                            break
                    if not filled:
                        for inp in visible:
                            t = inp.get_attribute("type") or "text"
                            if t not in ("hidden", "password", "email", "submit", "button"):
                                inp.click()
                                inp.fill(mfa_code)
                                filled = True
                                break
                    if not filled:
                        page.keyboard.type(mfa_code)

                page.keyboard.press("Enter")

                deadline2 = time.time() + 30
                while time.time() < deadline2:
                    if ticket_holder:
                        break
                    page.wait_for_timeout(300)

                if not ticket_holder:
                    browser.close()
                    _state["status"] = "error"
                    _state["error"] = i18n.t("ui.auth.mfa_no_ticket")
                    return

            browser.close()

        # Exchange CAS ticket for OAuth tokens
        m = re.search(r"[?&]ticket=(ST-[^&\s]+)", ticket_holder[0])
        if not m:
            _state["status"] = "error"
            _state["error"] = i18n.t("ui.auth.no_cas_ticket")
            return

        ticket = m.group(1)
        client = Client()
        oauth1 = get_oauth1_token(ticket, client)
        oauth2 = exchange(oauth1, client, login=True)
        client.configure(oauth1_token=oauth1, oauth2_token=oauth2)

        shutil.rmtree(session_dir, ignore_errors=True)
        os.makedirs(session_dir)
        client.dump(session_dir)

        display_name = ""
        try:
            profile = client.connectapi("/userprofile-service/socialProfile")
            raw = profile.get("displayName") or profile.get("fullName") or ""
            if raw and not ("-" in raw and len(raw) > 20):
                display_name = raw.split()[0]
            if display_name:
                with open(os.path.join(session_dir, "profile.json"), "w") as f:
                    json.dump({"displayName": display_name}, f)
        except Exception:
            pass

        _state["status"] = "success"
        _state["display_name"] = display_name

    except Exception as e:
        _state["status"] = "error"
        _state["error"] = str(e)
