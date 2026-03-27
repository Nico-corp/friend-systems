#!/usr/bin/env python3
"""
twitter_read.py — Read tweets, threads, and X Articles by URL or ID.

Usage:
    python3 twitter_read.py <tweet_url_or_id>
    python3 twitter_read.py https://x.com/user/status/123456
    python3 twitter_read.py https://x.com/i/article/123456
    python3 twitter_read.py 123456789           # tweet ID only

Output: plain text to stdout (title + content). 
        Add --json for structured output.

Auth strategy:
  1. Bearer token (API) for standard tweets — fast, no browser needed
  2. x_cookies.json (Playwright) for X Articles and cookie-gated content
  3. Playwright login fallback if cookies expired

Friend use: digest_tweet("<url>")  — returns clean text for analysis
"""

import asyncio
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

CREDS_FILE    = Path.home() / '.openclaw' / 'twitter_creds.json'
COOKIES_FILE  = Path.home() / '.openclaw' / 'secrets' / 'x_cookies.json'
SESSION_FILE  = Path(__file__).parent / 'twitter_session.json'
SESSION_DIR   = Path.home() / '.openclaw' / 'secrets' / 'x_chrome_session'
CHROME_BIN    = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
CHROME_COOKIES = Path.home() / 'Library/Application Support/Google/Chrome/Default/Cookies'
KEYCHAIN_KEY  = 'V7ugV1jk3uYkAO+gADqzzQ=='
API_BASE      = 'https://api.twitter.com/2'

# ── Regex patterns ─────────────────────────────────────────────────────────────
RE_TWEET_URL   = re.compile(r'x\.com/\w+/status/(\d+)')
RE_ARTICLE_URL = re.compile(r'x\.com/i/article/(\d+)')
RE_TWEET_ID    = re.compile(r'^\d{10,}$')


def load_creds() -> dict:
    if CREDS_FILE.exists():
        with open(CREDS_FILE) as f:
            return json.load(f)
    return {}


def bearer_headers(creds: dict) -> dict:
    token = creds.get('bearer_token')
    if not token:
        raise RuntimeError("No bearer_token in twitter_creds.json")
    return {'Authorization': f'Bearer {token}'}


# ── API tweet fetch (fast path) ────────────────────────────────────────────────

def fetch_tweet_api(tweet_id: str) -> dict | None:
    """Fetch a tweet via v2 API. Returns None if not accessible."""
    try:
        creds = load_creds()
        headers = bearer_headers(creds)
        url = (
            f'{API_BASE}/tweets/{tweet_id}'
            '?tweet.fields=text,author_id,created_at,conversation_id,referenced_tweets'
            '&expansions=author_id,referenced_tweets.id'
            '&user.fields=name,username'
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return None


def parse_api_tweet(data: dict) -> str:
    """Format API response into readable text."""
    if not data or 'data' not in data:
        return ""

    tweet   = data['data']
    users   = {u['id']: u for u in data.get('includes', {}).get('users', [])}
    author  = users.get(tweet.get('author_id', ''), {})
    name    = author.get('name', '')
    handle  = author.get('username', '')
    created = tweet.get('created_at', '')[:10]
    text    = tweet.get('text', '')

    lines = []
    if name or handle:
        lines.append(f"@{handle} ({name}) — {created}")
    lines.append(text)

    # Referenced tweets (quotes, replies)
    ref_tweets = data.get('includes', {}).get('tweets', [])
    if ref_tweets:
        lines.append("\n--- Referenced Tweet ---")
        for rt in ref_tweets:
            rt_author = users.get(rt.get('author_id', ''), {})
            rt_handle = rt_author.get('username', 'unknown')
            lines.append(f"@{rt_handle}: {rt.get('text', '')}")

    return '\n'.join(lines)


# ── Chrome cookie decryption ───────────────────────────────────────────────────

def get_x_cookies_from_chrome() -> list:
    """Decrypt x.com auth cookies from Chrome's local SQLite DB."""
    import hashlib, sqlite3, shutil, tempfile, os
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return []

    key = hashlib.pbkdf2_hmac('sha1', KEYCHAIN_KEY.encode('utf8'), b'saltysalt', 1003, dklen=16)

    tmp = tempfile.mktemp(suffix='.db')
    try:
        shutil.copy2(CHROME_COOKIES, tmp)
    except Exception:
        return []

    try:
        conn = sqlite3.connect(tmp)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, encrypted_value FROM cookies "
            "WHERE name IN ('auth_token','ct0','twid','kdt') AND host_key LIKE '%.x.com'"
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

    def decrypt(enc_val: bytes) -> str:
        raw = enc_val[3:]
        iv, data = raw[:16], raw[16:]
        dec = AES.new(key, AES.MODE_CBC, IV=iv).decrypt(data)
        pad = dec[-1] if 1 <= dec[-1] <= 16 else 0
        val = (dec[:-pad] if pad else dec).decode('ascii', 'ignore')
        import re as _re
        match = _re.search(r'[\w%=]{10,}', val)
        return match.group() if match else val.strip()

    cookie_map = {name: decrypt(enc) for name, enc in rows}
    if 'auth_token' not in cookie_map or 'ct0' not in cookie_map:
        return []

    cookies = [
        {'name': 'auth_token', 'value': cookie_map['auth_token'], 'domain': '.x.com',
         'path': '/', 'secure': True, 'httpOnly': True, 'sameSite': 'None'},
        {'name': 'ct0',        'value': cookie_map['ct0'],        'domain': '.x.com',
         'path': '/', 'secure': True, 'httpOnly': False, 'sameSite': 'Lax'},
    ]
    if 'twid' in cookie_map:
        cookies.append(
            {'name': 'twid', 'value': cookie_map['twid'], 'domain': '.x.com',
             'path': '/', 'secure': True, 'httpOnly': True, 'sameSite': 'None'}
        )
    return cookies


# ── Playwright fetch (article / cookie path) ───────────────────────────────────

async def fetch_with_browser(url: str) -> str:
    """Use Playwright + saved cookies to load X Articles or paywalled content."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"

    async with async_playwright() as p:
        # Use decrypted Chrome cookies — same approach as browser_post.py
        cookies = get_x_cookies_from_chrome()
        browser = await p.chromium.launch(headless=True)
        browser_context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        )
        if cookies:
            await browser_context.add_cookies(cookies)
        page = await browser_context.new_page()

        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(5)
            # Scroll to trigger lazy-loaded content (X Articles need aggressive scrolling)
            for _ in range(10):
                await page.keyboard.press('End')
                await asyncio.sleep(1.2)
            await page.keyboard.press('Home')
            await asyncio.sleep(1)

            # Check if login gate appeared
            if 'login' in page.url.lower():
                await browser_context.close()
                return "Error: Not logged in. Run: python3 twitter_read.py --save-session"

            # Extract content — try article selectors first, then general
            content = await page.evaluate("""
                () => {
                    // X Article selectors
                    const articleSelectors = [
                        '[data-testid="article-body"]',
                        '[data-testid="article-content"]',
                        '.article-body',
                        'article',
                    ];
                    for (const sel of articleSelectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.length > 200) return el.innerText;
                    }

                    // Tweet selectors
                    const tweetSelectors = [
                        '[data-testid="tweetText"]',
                        '[data-testid="tweet"]',
                    ];
                    const tweets = [];
                    for (const sel of tweetSelectors) {
                        document.querySelectorAll(sel).forEach(el => {
                            if (el.innerText.length > 20) tweets.push(el.innerText);
                        });
                    }
                    if (tweets.length) return tweets.join('\\n\\n---\\n\\n');

                    // Fallback: main column text
                    const main = document.querySelector('[data-testid="primaryColumn"]') || document.querySelector('main');
                    return main ? main.innerText : document.body.innerText;
                }
            """)

            title = await page.title()

            await browser_context.close()
            result = ""
            if title and title != 'X':
                result += f"# {title}\n\n"
            result += content or "(No content extracted)"
            return result

        except Exception as e:
            await browser_context.close()
            return f"Error loading page: {e}"


def _normalize_cookies(raw) -> list:
    """Normalize cookies from various formats to Playwright format."""
    VALID_SAMESITE = {'Strict', 'Lax', 'None'}
    if isinstance(raw, list):
        out = []
        for c in raw:
            if isinstance(c, dict) and 'name' in c and 'value' in c:
                cookie = {
                    'name': c['name'],
                    'value': str(c['value']),
                    'domain': c.get('domain', '.x.com'),
                    'path': c.get('path', '/'),
                    'secure': bool(c.get('secure', True)),
                    'httpOnly': bool(c.get('httpOnly', False)),
                }
                # sameSite must be Strict / Lax / None (exact casing)
                same_site = c.get('sameSite', '')
                if same_site in VALID_SAMESITE:
                    cookie['sameSite'] = same_site
                else:
                    # Normalize common variants
                    ss_map = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None', '': 'Lax'}
                    cookie['sameSite'] = ss_map.get(same_site.lower(), 'Lax')
                if 'expires' in c and c['expires'] is not None:
                    cookie['expires'] = int(c['expires'])
                out.append(cookie)
        return out
    return []


# ── Main entry point ───────────────────────────────────────────────────────────

def classify_input(inp: str) -> tuple[str, str]:
    """Returns (type, id_or_url). type = 'tweet' | 'article' | 'unknown'"""
    m = RE_TWEET_URL.search(inp)
    if m:
        return ('tweet', m.group(1))
    m = RE_ARTICLE_URL.search(inp)
    if m:
        return ('article', m.group(1))
    if RE_TWEET_ID.match(inp.strip()):
        return ('tweet', inp.strip())
    return ('unknown', inp)


def digest(inp: str, as_json: bool = False) -> str:
    """
    Main function. Give it a URL or tweet ID, get readable text back.
    This is the function Friend calls directly.
    """
    kind, id_or_url = classify_input(inp)

    if kind == 'tweet':
        # Try API first (fast)
        data = fetch_tweet_api(id_or_url)
        if data and 'data' in data:
            text = parse_api_tweet(data)
            if as_json:
                return json.dumps({'type': 'tweet', 'id': id_or_url, 'text': text, 'raw': data}, indent=2)
            return text
        # API failed — try browser
        url = inp if inp.startswith('http') else f'https://x.com/i/web/status/{id_or_url}'
        text = asyncio.run(fetch_with_browser(url))
        if as_json:
            return json.dumps({'type': 'tweet', 'id': id_or_url, 'text': text}, indent=2)
        return text

    elif kind == 'article':
        url = f'https://x.com/i/article/{id_or_url}'
        text = asyncio.run(fetch_with_browser(url))
        if as_json:
            return json.dumps({'type': 'article', 'id': id_or_url, 'text': text}, indent=2)
        return text

    else:
        # Unknown — try as URL directly
        if inp.startswith('http'):
            text = asyncio.run(fetch_with_browser(inp))
            if as_json:
                return json.dumps({'type': 'url', 'url': inp, 'text': text}, indent=2)
            return text
        return f"Error: Could not parse input as tweet URL, article URL, or tweet ID: {inp}"


# ── Save session helper ────────────────────────────────────────────────────────

async def save_session_interactive():
    """Log in interactively and save session for future headless runs."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Error: playwright not installed")
        return

    creds = load_creds()
    username = creds.get('username', 'Friend0nDesk')
    pw_file = Path.home() / '.openclaw' / 'twitter_pw.txt'
    password = pw_file.read_text().strip() if pw_file.exists() else ''

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headed for interactive
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto('https://x.com/login', wait_until='domcontentloaded')
        await asyncio.sleep(2)

        # Username
        await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
        await page.fill('input[autocomplete="username"]', username)
        await page.keyboard.press('Enter')
        await asyncio.sleep(2)

        # Handle possible confirm step
        try:
            confirm = await page.query_selector('input[data-testid="ocfEnterTextTextInput"]')
            if confirm:
                await confirm.fill(username)
                await page.keyboard.press('Enter')
                await asyncio.sleep(2)
        except Exception:
            pass

        # Password
        if password:
            await page.wait_for_selector('input[name="password"]', timeout=10000)
            await page.fill('input[name="password"]', password)
            await page.keyboard.press('Enter')
            await asyncio.sleep(4)
        else:
            print("No password found in ~/.openclaw/twitter_pw.txt")
            print("Please log in manually in the browser window, then press Enter here...")
            input()

        await context.storage_state(path=str(SESSION_FILE))
        print(f"Session saved to {SESSION_FILE}")
        await browser.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    args = sys.argv[1:]

    if not args or '--help' in args:
        print(__doc__)
        sys.exit(0)

    if '--save-session' in args:
        asyncio.run(save_session_interactive())
        sys.exit(0)

    as_json = '--json' in args
    targets = [a for a in args if not a.startswith('--')]

    for target in targets:
        result = digest(target, as_json=as_json)
        print(result)
        if len(targets) > 1:
            print('\n' + '─' * 60 + '\n')
