#!/usr/bin/env python3
"""
@Friend0nDesk Twitter/X posting tool.
Uses X API v2 (OAuth 1.0a for user context — required for posting).

Setup:
  1. Create account at x.com (@Friend0nDesk)
  2. Go to developer.x.com → create app → get keys
  3. Add to ~/.openclaw/.env or set environment variables:
       X_API_KEY=...
       X_API_SECRET=...
       X_ACCESS_TOKEN=...
       X_ACCESS_TOKEN_SECRET=...
  4. Run: python3 post.py --text "your tweet"
"""

import os
import sys
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime
from requests_oauthlib import OAuth1

# ── Config ────────────────────────────────────────────────────────────────────
CREDS_FILE = Path.home() / '.openclaw' / 'twitter_creds.json'
LOG_FILE   = Path(__file__).parent / 'post_log.jsonl'
API_BASE   = 'https://api.twitter.com/2'

def load_creds() -> dict:
    """Load X API credentials from file or environment."""
    # Try env vars first
    env_creds = {
        'api_key':              os.environ.get('X_API_KEY'),
        'api_secret':           os.environ.get('X_API_SECRET'),
        'access_token':         os.environ.get('X_ACCESS_TOKEN'),
        'access_token_secret':  os.environ.get('X_ACCESS_TOKEN_SECRET'),
    }
    if all(env_creds.values()):
        return env_creds

    # Fall back to creds file
    if CREDS_FILE.exists():
        with open(CREDS_FILE) as f:
            return json.load(f)

    raise RuntimeError(
        f"No X credentials found.\n"
        f"Set X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET\n"
        f"or create {CREDS_FILE} with those keys."
    )

def get_auth(creds: dict) -> OAuth1:
    return OAuth1(
        creds['api_key'],
        creds['api_secret'],
        creds['access_token'],
        creds['access_token_secret'],
    )

def post_tweet(text: str, reply_to_id: str = None) -> dict:
    """Post a single tweet. Returns the API response."""
    creds = load_creds()
    auth  = get_auth(creds)

    payload = {'text': text}
    if reply_to_id:
        payload['reply'] = {'in_reply_to_tweet_id': reply_to_id}

    resp = requests.post(
        f'{API_BASE}/tweets',
        json=payload,
        auth=auth,
        headers={'Content-Type': 'application/json'},
    )

    if not resp.ok:
        raise RuntimeError(f"Tweet failed [{resp.status_code}]: {resp.text}")

    data = resp.json()
    tweet_id = data['data']['id']

    # Log to JSONL
    log_entry = {
        'ts':       datetime.utcnow().isoformat() + 'Z',
        'tweet_id': tweet_id,
        'text':     text,
        'reply_to': reply_to_id,
    }
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(log_entry) + '\n')

    return data

def post_thread(tweets: list[str]) -> list[dict]:
    """Post a list of strings as a thread (each reply to the previous)."""
    results = []
    prev_id = None
    for i, text in enumerate(tweets):
        result = post_tweet(text, reply_to_id=prev_id)
        prev_id = result['data']['id']
        results.append(result)
        print(f"  [{i+1}/{len(tweets)}] posted: {result['data']['id']}")
    return results

def follow_user(username: str) -> dict:
    """Follow a user by username."""
    creds = load_creds()
    auth  = get_auth(creds)

    # Get our own user ID
    me = requests.get(f'{API_BASE}/users/me', auth=auth)
    if not me.ok:
        raise RuntimeError(f"Could not get own user id: {me.text}")
    my_id = me.json()['data']['id']

    # Look up target user ID
    lookup = requests.get(f'{API_BASE}/users/by/username/{username}', auth=auth)
    if not lookup.ok:
        raise RuntimeError(f"User lookup failed [{lookup.status_code}]: {lookup.text}")
    target_id = lookup.json()['data']['id']

    # Follow
    resp = requests.post(
        f'{API_BASE}/users/{my_id}/following',
        json={'target_user_id': target_id},
        auth=auth,
        headers={'Content-Type': 'application/json'},
    )
    if not resp.ok:
        raise RuntimeError(f"Follow failed [{resp.status_code}]: {resp.text}")
    return resp.json()

def save_creds(api_key: str, api_secret: str, access_token: str, access_token_secret: str):
    """Save credentials to the creds file."""
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CREDS_FILE, 'w') as f:
        json.dump({
            'api_key':             api_key,
            'api_secret':          api_secret,
            'access_token':        access_token,
            'access_token_secret': access_token_secret,
        }, f, indent=2)
    CREDS_FILE.chmod(0o600)
    print(f"Credentials saved to {CREDS_FILE}")

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='@Friend0nDesk Twitter posting tool')
    sub = parser.add_subparsers(dest='cmd')

    # post
    p_post = sub.add_parser('post', help='Post a single tweet')
    p_post.add_argument('--text', required=True, help='Tweet text (max 280 chars)')
    p_post.add_argument('--reply-to', help='Tweet ID to reply to')

    # thread
    p_thread = sub.add_parser('thread', help='Post a thread from a JSON array of strings')
    p_thread.add_argument('--file', required=True, help='JSON file with array of tweet strings')

    # setup
    p_setup = sub.add_parser('setup', help='Save API credentials')
    p_setup.add_argument('--api-key',              required=True)
    p_setup.add_argument('--api-secret',           required=True)
    p_setup.add_argument('--access-token',         required=True)
    p_setup.add_argument('--access-token-secret',  required=True)

    # follow
    p_follow = sub.add_parser('follow', help='Follow a user by username')
    p_follow.add_argument('--user', required=True, help='Username to follow (no @)')

    # test
    sub.add_parser('test', help='Verify credentials with a GET /users/me call')

    args = parser.parse_args()

    if args.cmd == 'post':
        if len(args.text) > 280:
            print(f"WARNING: tweet is {len(args.text)} chars (max 280). Will be truncated by X.")
        result = post_tweet(args.text, reply_to_id=args.reply_to)
        print(f"Posted: https://x.com/Friend0nDesk/status/{result['data']['id']}")

    elif args.cmd == 'thread':
        with open(args.file) as f:
            tweets = json.load(f)
        print(f"Posting thread of {len(tweets)} tweets...")
        results = post_thread(tweets)
        first_id = results[0]['data']['id']
        print(f"Thread live: https://x.com/Friend0nDesk/status/{first_id}")

    elif args.cmd == 'setup':
        save_creds(args.api_key, args.api_secret, args.access_token, args.access_token_secret)

    elif args.cmd == 'follow':
        result = follow_user(args.user)
        following = result.get('data', {}).get('following', False)
        pending   = result.get('data', {}).get('pending_follow', False)
        if following:
            print(f"Now following @{args.user}")
        elif pending:
            print(f"Follow request sent to @{args.user} (protected account)")
        else:
            print(f"Follow result: {result}")

    elif args.cmd == 'test':
        creds = load_creds()
        auth  = get_auth(creds)
        resp  = requests.get(f'{API_BASE}/users/me', auth=auth)
        if resp.ok:
            data = resp.json()
            print(f"Auth OK — logged in as: @{data['data']['username']} (id: {data['data']['id']})")
        else:
            print(f"Auth FAILED [{resp.status_code}]: {resp.text}")

    else:
        parser.print_help()

if __name__ == '__main__':
    main()
