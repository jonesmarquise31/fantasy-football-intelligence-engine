#!/usr/bin/env python3
"""Secure authentication bridge: capture ESPN session credentials into .env.

ESPN's fantasy v3 API has no public OAuth flow and no API keys. Access to a
*private* league is gated entirely on two browser cookies:

    SWID      the account's global identifier, a braced UUID
    espn_s2   a long-lived, URL-encoded session token

The only supported way to obtain them is to be logged in to ESPN in a real
browser. This script automates that honestly rather than pretending to:

    1. Launches a genuine Chrome binary via Playwright (``channel="chrome"``)
       against a *dedicated* profile directory under ``.chrome-profile/``.
    2. Opens ESPN's fantasy homepage and hands you the window.
    3. You log in once, by hand, in that window. Nothing types your password
       for you and nothing reads your password.
    4. Once the cookie jar contains both credentials, they are written to the
       local ``.env`` — which is gitignored and never leaves this machine.

Because the profile persists, subsequent runs usually find you already signed
in and complete without interaction. Re-run this whenever the assistant starts
returning 401s; ESPN rotates ``espn_s2`` on password change and roughly
annually otherwise.

A note on why this does NOT read your everyday Chrome profile: Chrome holds an
exclusive lock on a running profile, and its cookie store is encrypted with an
OS keychain entry. Prying that open would be both fragile and a bad security
posture. A dedicated profile is the correct boundary.

Usage:
    python auth_bridge.py                 # interactive capture
    python auth_bridge.py --headless      # only works if profile is warm
    python auth_bridge.py --print-only    # show cookies, do not write .env
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_ROOT / ".chrome-profile"
ENV_PATH = PROJECT_ROOT / ".env"

ESPN_LOGIN_URL = "https://www.espn.com/fantasy/football/"
REQUIRED_COOKIES = ("SWID", "espn_s2")
POLL_SECONDS = 2.0


def _fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\n  ERROR  {message}\n", file=sys.stderr)
    raise SystemExit(1)


def capture(headless: bool = False, timeout: int = 300) -> dict[str, str]:
    """Drive Chrome until both ESPN session cookies are present."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _fail(
            "Playwright is not installed.\n"
            "         pip install -r requirements.txt\n"
            "         python -m playwright install chromium"
        )

    PROFILE_DIR.mkdir(exist_ok=True)
    captured: dict[str, str] = {}

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=headless,
                viewport={"width": 1280, "height": 900},
            )
        except Exception as exc:
            _fail(
                "Could not launch the local Chrome binary via "
                f'channel="chrome".\n         {exc}\n'
                "         Install Google Chrome, or swap to bundled Chromium "
                "by removing the channel argument."
            )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(ESPN_LOGIN_URL, wait_until="domcontentloaded")

        print("\n  Chrome is open on ESPN Fantasy Football.")
        print("  Sign in there if you are not already signed in.")
        print("  This script is watching the cookie jar and will exit on its own.\n")

        deadline = time.time() + timeout
        while time.time() < deadline:
            jar = {
                cookie["name"]: cookie["value"]
                for cookie in context.cookies()
                if cookie["name"] in REQUIRED_COOKIES
            }
            if all(jar.get(name) for name in REQUIRED_COOKIES):
                captured = jar
                break
            time.sleep(POLL_SECONDS)

        context.close()

    if not captured:
        _fail(
            f"Timed out after {timeout}s without seeing both cookies.\n"
            "         Make sure you completed the ESPN login in the window."
        )

    swid = captured["SWID"]
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"
    captured["SWID"] = swid
    return captured


def write_env(cookies: dict[str, str]) -> None:
    """Merge captured cookies into ``.env``, preserving every other key."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
    else:
        example = PROJECT_ROOT / ".env.example"
        if example.exists():
            lines = example.read_text().splitlines()

    updates = {"SWID": cookies["SWID"], "ESPN_S2": cookies["espn_s2"]}
    seen: set[str] = set()
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out).rstrip() + "\n")
    ENV_PATH.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true",
                        help="run without a visible window (needs a warm profile)")
    parser.add_argument("--print-only", action="store_true",
                        help="print the cookies instead of writing .env")
    parser.add_argument("--timeout", type=int, default=300,
                        help="seconds to wait for login (default: 300)")
    args = parser.parse_args()

    cookies = capture(headless=args.headless, timeout=args.timeout)

    if args.print_only:
        print(f"SWID={cookies['SWID']}")
        print(f"ESPN_S2={cookies['espn_s2']}")
        return 0

    write_env(cookies)
    masked = cookies["espn_s2"][:8] + "..." + cookies["espn_s2"][-4:]
    print(f"  Captured SWID    {cookies['SWID']}")
    print(f"  Captured espn_s2 {masked}  ({len(cookies['espn_s2'])} chars)")
    print(f"  Written to       {ENV_PATH}  (mode 600, gitignored)")
    print("\n  Next:  python get_settings.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
