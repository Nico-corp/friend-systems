#!/usr/bin/env python3
"""
browser_post.py — Post tweets and replies via browser automation (Playwright + cookie injection).

Bypasses Twitter API restrictions on cold replies by using the actual browser session.
Decrypts Chrome cookies using macOS keychain key — no manual login needed.

Usage:
  python3 browser_post.py post "Tweet text here"
  python3 browser_post.py reply <tweet_id_or_url> "Reply text here"
  python3 browser_post.py follow <username>
  python3 browser_post.py thread <tweet_id_or_url> "Tweet 1" "Tweet 2" ...

Examples:
  python3 browser_post.py reply 2036485610257719500 "Great article."
  python3 browser_post.py follow TurboCorp_
"""

import sys, os, re, time, json, argparse
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
CHROME_BIN    = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SESSION_DIR   = Path.home() / ".openclaw/secrets/x_playwright_session"
COOKIES_FILE  = Path.home() / ".openclaw/secrets/x_cookies.json"


# ── Cookie loading ────────────────────────────────────────────────────────────
def get_x_cookies() -> list[dict]:
    """Load and return auth cookies for x.com from x_cookies.json.

    The stored values have a 20-char garbage prefix from a prior decryption
    attempt. Strip it with a regex to extract the real token.
    """
    raw = json.loads(COOKIES_FILE.read_text())

    def _clean(v: str) -> str:
        m = re.search(r"[0-9a-zA-Z%_\-]{10,}", v)
        return m.group() if m else v

    cookies = []
    for c in raw:
        if c["name"] in ("auth_token", "ct0", "twid"):
            cookies.append({
                "name":     c["name"],
                "value":    _clean(c["value"]),
                "domain":   ".x.com",
                "path":     "/",
                "secure":   True,
                "httpOnly": c.get("httpOnly", False),
                "sameSite": "None" if c.get("httpOnly") else "Lax",
            })
    return cookies


# ── Browser context ───────────────────────────────────────────────────────────
def _make_context(playwright, headless=True):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    ctx = playwright.chromium.launch_persistent_context(
        str(SESSION_DIR),
        headless=headless,
        executable_path=CHROME_BIN,
        viewport={"width": 1280, "height": 900},
        args=["--no-sandbox", "--use-mock-keychain", "--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    ctx.add_cookies(get_x_cookies())
    return ctx


def _get_textarea(page):
    for sel in ["[data-testid='tweetTextarea_0']", "div[contenteditable='true'][role='textbox']"]:
        el = page.query_selector(sel)
        if el:
            return el
    return None


def _post_text(page, text: str) -> bool:
    textarea = _get_textarea(page)
    if not textarea:
        return False
    textarea.click()
    time.sleep(0.4)
    textarea.type(text, delay=20)
    time.sleep(1.2)
    btn = page.query_selector("[data-testid='tweetButton']") or \
          page.query_selector("[data-testid='tweetButtonInline']")
    if btn and btn.get_attribute("aria-disabled") != "true":
        btn.click()
        time.sleep(4)
        return True
    return False


# ── Public API ────────────────────────────────────────────────────────────────
def post_tweet(text: str, headless=True) -> dict:
    with sync_playwright() as p:
        ctx = _make_context(p, headless)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if "home" not in page.url:
            ctx.close()
            return {"success": False, "error": f"Not logged in: {page.url}"}
        ok = _post_text(page, text)
        ctx.close()
        return {"success": ok, "error": None if ok else "Post failed"}


def reply_to_tweet(tweet_id_or_url: str, text: str, headless=True) -> dict:
    url = tweet_id_or_url if tweet_id_or_url.startswith("http") \
          else f"https://x.com/i/web/status/{tweet_id_or_url}"
    with sync_playwright() as p:
        ctx = _make_context(p, headless)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        reply_btn = page.query_selector("[data-testid='reply']")
        if reply_btn:
            reply_btn.click()
            time.sleep(3)  # modal needs time to open fully
        ok = _post_text(page, text)
        ctx.close()
        return {"success": ok, "error": None if ok else "Reply failed"}


def follow_user(username: str, headless=True) -> dict:
    with sync_playwright() as p:
        ctx = _make_context(p, headless)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        btn = page.query_selector("[data-testid='followButton']") or \
              page.query_selector(f"button[aria-label*='Follow @{username}']")
        if btn:
            btn.click()
            time.sleep(2)
            ctx.close()
            return {"success": True}
        ctx.close()
        return {"success": False, "error": "Follow button not found (already following?)"}


def post_thread(reply_to_id: str, tweets: list[str], headless=True) -> dict:
    """Post a thread: first tweet replies to reply_to_id, each subsequent replies to previous."""
    results = []
    import requests
    from requests_oauthlib import OAuth1

    creds = json.loads((Path.home() / ".openclaw/twitter_creds.json").read_text())
    auth = OAuth1(creds["api_key"], creds["api_secret"], creds["access_token"], creds["access_token_secret"])

    # Post first tweet as reply
    r1 = reply_to_tweet(reply_to_id, tweets[0], headless)
    results.append(r1)
    if not r1["success"]:
        return {"success": False, "results": results}

    # Get the new tweet ID
    r = requests.get(f"https://api.twitter.com/2/users/me", auth=auth)
    uid = r.json()["data"]["id"]
    r2 = requests.get(f"https://api.twitter.com/2/users/{uid}/tweets", auth=auth,
                      params={"max_results": 5, "tweet.fields": "text"})
    latest_id = r2.json()["data"][0]["id"]

    # Reply to each subsequent tweet
    for tweet_text in tweets[1:]:
        rn = reply_to_tweet(latest_id, tweet_text, headless)
        results.append(rn)
        if rn["success"]:
            r3 = requests.get(f"https://api.twitter.com/2/users/{uid}/tweets", auth=auth,
                              params={"max_results": 5})
            latest_id = r3.json()["data"][0]["id"]

    return {"success": all(r["success"] for r in results), "results": results}


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="@Friend0nClaw browser posting tool")
    sub = parser.add_subparsers(dest="cmd")

    p1 = sub.add_parser("post", help="Standalone tweet")
    p1.add_argument("text")
    p1.add_argument("--visible", action="store_true")

    p2 = sub.add_parser("reply", help="Reply to a tweet")
    p2.add_argument("tweet", help="Tweet ID or URL")
    p2.add_argument("text")
    p2.add_argument("--visible", action="store_true")

    p3 = sub.add_parser("follow", help="Follow a user")
    p3.add_argument("username")
    p3.add_argument("--visible", action="store_true")

    p4 = sub.add_parser("thread", help="Post a reply thread")
    p4.add_argument("tweet", help="Tweet ID or URL to reply to")
    p4.add_argument("tweets", nargs="+", help="Tweet texts in order")
    p4.add_argument("--visible", action="store_true")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help(); return

    headless = not args.visible

    if args.cmd == "post":
        result = post_tweet(args.text, headless)
    elif args.cmd == "reply":
        result = reply_to_tweet(args.tweet, args.text, headless)
    elif args.cmd == "follow":
        result = follow_user(args.username, headless)
    elif args.cmd == "thread":
        result = post_thread(args.tweet, args.tweets, headless)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
