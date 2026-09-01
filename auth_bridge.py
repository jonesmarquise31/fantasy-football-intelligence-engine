#!/usr/bin/env python3
"""Command-line ESPN session capture.

The desktop app's "Connect ESPN Account" button does the same job; this is the
equivalent for developers working from source.

ESPN's fantasy v3 API has no public OAuth flow and no API keys. Access to a
private league is gated entirely on two browser cookies:

    SWID      the account's global identifier, a braced UUID
    espn_s2   a long-lived, URL-encoded session token

The only supported way to obtain them is to be logged in to ESPN in a real
browser. This script automates that honestly rather than pretending to: it
launches a genuine Chrome binary, opens ESPN's login page, and hands you the
window. You log in by hand. Nothing types your password and nothing reads it.

On not reading your everyday Chrome profile: Chrome holds an exclusive lock on
a running profile, and its cookie store is encrypted against an OS keychain
entry. Prying that open would be fragile and a poor security posture. A separate
browser session is the correct boundary, and the cost is one manual login.

Usage:
    python auth_bridge.py
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ENV_PATH = Path(__file__).resolve().parent / ".env"

# How long to wait for a manual login before giving up. Without a ceiling a
# failed login leaves the script spinning forever with no way to tell whether
# it is working.
LOGIN_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2


def write_env(updates: dict, path: Path = ENV_PATH) -> Path:
    """Merge keys into .env, preserving any this script does not manage.

    Overwriting the file wholesale would discard LEAGUE_ID, SEASON, and TEAM_ID
    every time credentials are refreshed. Written 0600: the file holds a live
    session token, and anything looser leaves it readable by other accounts.
    """
    existing = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            existing[key.strip()] = value.strip()

    existing.update({k: v for k, v in updates.items() if v not in (None, "")})

    path.write_text("".join(f"{k}={v}\n" for k, v in existing.items()))
    path.chmod(0o600)
    return path


def run() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("\nOpening Chrome. Log in to ESPN in that window.")
        page.goto("https://www.espn.com/login")

        swid, espn_s2 = None, None
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS

        while not (swid and espn_s2):
            if time.monotonic() > deadline:
                browser.close()
                print(
                    f"\n[TIMEOUT] No ESPN session found after "
                    f"{LOGIN_TIMEOUT_SECONDS}s. Nothing was written.",
                    file=sys.stderr,
                )
                return 1

            time.sleep(POLL_INTERVAL_SECONDS)
            for cookie in context.cookies():
                if cookie["name"] == "SWID":
                    swid = cookie["value"]
                elif cookie["name"] == "espn_s2":
                    espn_s2 = cookie["value"]

        # LEAGUE_ID / SEASON / TEAM_ID are the user's own and are left alone.
        path = write_env({"SWID": swid, "ESPN_S2": espn_s2})
        browser.close()

    # The token is never printed in full; a masked tail is enough to confirm
    # which session was captured without putting the credential on screen.
    print(f"\n[SUCCESS] Credentials written to {path} (mode 600).")
    print(f"          SWID {swid[:10]}...  ESPN_S2 ...{espn_s2[-6:]}")
    if "LEAGUE_ID" not in path.read_text():
        print("\n  Next: set LEAGUE_ID in .env, or use Sync League in the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
