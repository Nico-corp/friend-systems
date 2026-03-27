#!/usr/bin/env python3
"""
engagement_engine.py — Twitter autonomous engagement engine for @Friend0nClaw.

Called by: twitter-engagement-heartbeat cron (every 30 min)
Manual test: python3 twitter/engagement_engine.py --dry-run

Flow:
  1. Probabilistic trigger (cooldown + ramp probability)
  2. Browse signal accounts (last 3h)
  3. Score tweets for reply-worthiness
  4. Draft replies via Claude API for high-score tweets
  5. Queue drafts to twitter/drafts/YYYY-MM-DD.jsonl
  6. Send Telegram DM to Nico (max 3 per run)
  7. Update engagement_state.json
"""

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Config ─────────────────────────────────────────────────────────────────────
WORKSPACE    = Path(__file__).parent.parent
TWITTER_DIR  = Path(__file__).parent
STATE_FILE   = TWITTER_DIR / 'engagement_state.json'
DRAFTS_DIR   = TWITTER_DIR / 'drafts'
CREDS_FILE   = Path.home() / '.openclaw' / 'twitter_creds.json'
API_BASE     = 'https://api.twitter.com/2'
NICO_CHAT_ID = '5463998499'
MAX_DMS_PER_RUN = 3
MAX_SEEN_IDS    = 500
LOOKBACK_HOURS  = 12   # 3h was too tight — missed most good reply targets

DRAFTS_DIR.mkdir(exist_ok=True)

# ── Signal accounts (from twitter_signal.py) ───────────────────────────────────
SIGNAL_ACCOUNTS = [
    # AI / Agents / Builders — primary audience post-rebrand
    'karpathy',
    'sama',
    'levelsio',
    'benedictevans',
    'molt_cornelius',       # OpenClaw community
    'TurboCorp_',           # OpenClaw community
    'swyx',                 # AI builder scene
    'emollick',             # AI research/commentary
    # Markets — kept as secondary (trading is proof of work)
    'unusual_whales',
    'morganhousel',
    'george__mack',
    'patrick_oshag',
]

# ── Scoring criteria ────────────────────────────────────────────────────────────
SCORING_CRITERIA = {
    "relevance": {
        "trading_options": ["VIX", "IV", "theta", "gamma", "spread", "premium", "regime", "expir"],
        "macro": ["Fed", "FOMC", "rates", "CPI", "SPX", "SPY", "recession"],
        "systems_ai": ["agent", "memory", "LLM", "model", "inference", "RAG", "tool"],
        "mental_models": ["Kelly", "Marks", "Taleb", "Thorp", "position sizing", "edge", "risk"],
    },
    "engagement_signals": ["?", "what do you", "thoughts", "anyone", "hot take"],
}

# Voice guard — reject if any of these appear in a draft reply
VOICE_REJECT_PHRASES = [
    "great", "love this", "couldn't agree", "so important", "thread incoming",
    "as an ai", "as an AI", "great thread", "love this take",
]

# ── State management ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "last_run_ts": None,
        "last_engaged_ts": None,
        "seen_tweet_ids": [],
        "total_drafts_queued": 0,
        "total_posted": 0,
        "runs_today": 0,
        "ai_card_used_week": None,   # ISO date of last AI card use
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def hours_since_last(ts_iso: str | None) -> float:
    """Return hours since an ISO timestamp. Returns 999 if None."""
    if not ts_iso:
        return 999.0
    try:
        then = datetime.fromisoformat(ts_iso.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return (now - then).total_seconds() / 3600
    except Exception:
        return 999.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Probabilistic trigger ───────────────────────────────────────────────────────

def should_engage(state: dict) -> tuple[bool, str]:
    """Decide whether to engage this run based on cooldown + probability."""
    hours_since = hours_since_last(state.get('last_engaged_ts'))

    if hours_since < 1.5:
        return False, f"cooldown (last engaged {hours_since:.1f}h ago)"

    if hours_since < 3:
        prob = 0.20
    elif hours_since < 5:
        prob = 0.50
    else:
        prob = 0.85

    fire = random.random() < prob
    reason = f"prob={prob:.0%} hours_since={hours_since:.1f}"
    return fire, reason


# ── Twitter API helpers ─────────────────────────────────────────────────────────

def load_creds() -> dict:
    env_creds = {
        'api_key':             os.environ.get('X_API_KEY'),
        'api_secret':          os.environ.get('X_API_SECRET'),
        'access_token':        os.environ.get('X_ACCESS_TOKEN'),
        'access_token_secret': os.environ.get('X_ACCESS_TOKEN_SECRET'),
        'bearer_token':        os.environ.get('X_BEARER_TOKEN'),
    }
    if env_creds.get('bearer_token'):
        return env_creds
    if CREDS_FILE.exists():
        return json.loads(CREDS_FILE.read_text())
    raise RuntimeError("No X credentials found. Check ~/.openclaw/twitter_creds.json")


def bearer_headers(creds: dict) -> dict:
    token = creds.get('bearer_token')
    if not token:
        raise RuntimeError("Bearer token required for timeline reads.")
    return {'Authorization': f'Bearer {token}'}


def get_user_id(handle: str, headers: dict) -> str | None:
    try:
        r = requests.get(
            f'{API_BASE}/users/by/username/{handle}',
            headers=headers,
            timeout=10,
        )
        if r.status_code == 429:
            raise RateLimitError("Rate limit hit on user lookup")
        if r.status_code == 200:
            return r.json().get('data', {}).get('id')
    except RateLimitError:
        raise
    except Exception:
        pass
    return None


def get_recent_tweets(user_id: str, handle: str, headers: dict, hours_back: int = 3) -> list:
    """Fetch tweets from last N hours. Returns list of dicts."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(
            f'{API_BASE}/users/{user_id}/tweets',
            headers=headers,
            params={
                'max_results': 20,
                'start_time': since,
                'tweet.fields': 'created_at,public_metrics,text',
                'exclude': 'replies',  # include RTs for filtering below, but exclude replies
            },
            timeout=10,
        )
        if r.status_code == 429:
            raise RateLimitError("Rate limit hit on tweets fetch")
        if r.status_code == 200:
            tweets = r.json().get('data', []) or []
            # Attach author handle for later use
            for t in tweets:
                t['author_handle'] = handle
            return tweets
    except RateLimitError:
        raise
    except Exception:
        pass
    return []


class RateLimitError(Exception):
    pass


# ── Tweet filtering ─────────────────────────────────────────────────────────────

def is_valid_tweet(tweet: dict, seen_ids: list) -> bool:
    """Filter tweets for engagement candidacy."""
    text = tweet.get('text', '')
    tweet_id = tweet.get('id', '')

    # Min length
    if len(text) < 20:
        return False

    # Skip already seen
    if tweet_id in seen_ids:
        return False

    # Skip retweets from non-signal accounts
    if text.startswith('RT @'):
        rt_handle = text.split('RT @')[1].split(':')[0].strip().lower()
        if rt_handle not in [a.lower() for a in SIGNAL_ACCOUNTS]:
            return False

    return True


# ── Scoring ─────────────────────────────────────────────────────────────────────

def score_tweet(tweet: dict) -> tuple[float, str]:
    """
    Score a tweet 0-10 for reply-worthiness.
    Returns (score, reason).

    7+ = draft reply
    4-6 = interesting find (DM Nico only)
    <4  = skip
    """
    text = tweet.get('text', '')
    score = 0.0
    reasons = []

    # Relevance keywords — up to 6 points
    for pillar, keywords in SCORING_CRITERIA["relevance"].items():
        matches = [kw for kw in keywords if kw.lower() in text.lower()]
        if matches:
            pillar_score = min(len(matches) * 1.5, 3.0)
            score += pillar_score
            reasons.append(f"{pillar}: {', '.join(matches[:3])}")

    # Engagement signals — up to 2 points
    eng_matches = [sig for sig in SCORING_CRITERIA["engagement_signals"] if sig.lower() in text.lower()]
    if eng_matches:
        score += min(len(eng_matches) * 1.0, 2.0)
        reasons.append(f"engagement signal ({', '.join(eng_matches[:2])})")

    # Engagement metrics boost — up to 2 points
    metrics = tweet.get('public_metrics', {})
    engagement = (
        metrics.get('like_count', 0) * 3 +
        metrics.get('retweet_count', 0) * 5 +
        metrics.get('reply_count', 0) * 2 +
        metrics.get('quote_count', 0) * 4
    )
    if engagement > 100:
        score += 2.0
        reasons.append(f"high engagement ({engagement})")
    elif engagement > 20:
        score += 1.0
        reasons.append(f"good engagement ({engagement})")

    score = min(score, 10.0)
    reason = '; '.join(reasons) if reasons else 'no strong signals'
    return round(score, 1), reason


# ── Claude reply drafting ───────────────────────────────────────────────────────

def draft_reply(tweet_text: str, author_handle: str, voice_rules: str, state: dict) -> str | None:
    """
    Generate a reply draft via Claude API.
    Returns draft text or None if generation fails or voice check fails.
    """
    try:
        import anthropic
    except ImportError:
        return None

    # Check AI card weekly limit
    ai_card_date = state.get('ai_card_used_week')
    week_start = (datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())).date().isoformat()
    ai_card_available = (ai_card_date is None or ai_card_date < week_start)
    ai_card_instruction = (
        "The AI angle (mentioning you're an AI) is available this week — use it AT MOST ONCE, and only if it adds genuine texture."
        if ai_card_available
        else "DO NOT mention being an AI this week — the weekly AI card has already been played."
    )

    system_prompt = f"""You are Friend (@Friend0nClaw), an AI running a real trading desk.

Voice rules (MUST follow):
- Precise practitioner. Confident, not arrogant. Clear point of view.
- Never: "Great thread!", "Couldn't agree more", hedged takes, asking for engagement
- Never lead with "as an AI..."
- Short: 1-3 sentences max for replies. No thread replies.
- If it's a loss/miss take: own it flat, no cope.
- {ai_card_instruction}
- Read the original tweet carefully. Reply adds something specific, not generic.

Voice spec excerpt:
{voice_rules[:500]}

Write ONE reply option. No explanation. No prefix like "Reply:" or "Draft:". Just the reply text."""

    user_prompt = f"""Original tweet by @{author_handle}:
{tweet_text}

Write ONE reply option."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=150,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        draft = response.content[0].text.strip()
    except Exception as e:
        return None

    # Voice guard
    draft_lower = draft.lower()
    for phrase in VOICE_REJECT_PHRASES:
        if phrase.lower() in draft_lower:
            return None  # Rejected — regenerate would cost another API call, skip

    # Length guard
    if len(draft) > 280:
        return None

    return draft


# ── Draft queue ─────────────────────────────────────────────────────────────────

def queue_draft(entry: dict):
    """Append-only write to today's JSONL draft file."""
    today = datetime.now().strftime('%Y-%m-%d')
    draft_file = DRAFTS_DIR / f'{today}.jsonl'
    with open(draft_file, 'a') as f:
        f.write(json.dumps(entry) + '\n')


# ── Telegram DM ─────────────────────────────────────────────────────────────────

def send_telegram_dm(message: str, dry_run: bool = False) -> bool:
    """Send a Telegram DM to Nico via OpenClaw CLI."""
    if dry_run:
        print(f"\n[DRY RUN — Telegram DM would send]\n{message}\n{'─'*50}")
        return True
    try:
        result = subprocess.run(
            ['openclaw', 'message', 'send',
             '--channel', 'telegram',
             '--to', NICO_CHAT_ID,
             '--message', message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Telegram DM failed: {e}", file=sys.stderr)
        return False


def format_reply_dm(entry: dict) -> str:
    author = entry['original_author']
    score = entry['score']
    original = entry['original_tweet_text'][:100]
    if len(entry['original_tweet_text']) > 100:
        original += '...'
    draft = entry['draft_text']
    return (
        f"🐦 Draft ready — {author} ({score}/10)\n\n"
        f"Original: \"{original}\"\n\n"
        f"My draft: \"{draft}\"\n\n"
        f"Reply ✅ or skip ❌?\n"
        f"[use: python3 twitter/approve_draft.py --list to manage]"
    )


def format_find_dm(entry: dict) -> str:
    author = entry['original_author']
    text = entry['original_tweet_text'][:120]
    if len(entry['original_tweet_text']) > 120:
        text += '...'
    note = entry.get('note', '')
    return (
        f"👀 Found something interesting\n\n"
        f"{author}: \"{text}\"\n\n"
        f"{note}\n\n"
        f"Worth looking into?"
    )


# ── Main ─────────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, force: bool = False, verbose: bool = False):
    """Main engagement engine run."""
    state = load_state()
    ts_now = now_iso()

    # Update run counter
    today = datetime.now().strftime('%Y-%m-%d')
    state_date = (state.get('last_run_ts') or '')[:10]
    if state_date != today:
        state['runs_today'] = 0
    state['runs_today'] = state.get('runs_today', 0) + 1
    state['last_run_ts'] = ts_now

    if verbose:
        print(f"[{ts_now[:16]}] Run #{state['runs_today']} today")

    # ── Step 1: Probabilistic trigger ──────────────────────────────────────────
    if not force:
        engage, reason = should_engage(state)
        if not engage:
            if verbose:
                print(f"[skip] Not engaging this run — {reason}")
            save_state(state)
            return
        if verbose:
            print(f"[trigger] Engaging — {reason}")
    else:
        if verbose:
            print("[force] Forced engagement mode")

    # ── Step 2: Browse signal accounts ─────────────────────────────────────────
    try:
        creds = load_creds()
        headers = bearer_headers(creds)
    except RuntimeError as e:
        print(f"[error] Credentials: {e}", file=sys.stderr)
        save_state(state)
        return

    seen_ids = set(state.get('seen_tweet_ids', []))
    candidate_tweets = []

    for handle in SIGNAL_ACCOUNTS:
        if verbose:
            print(f"  checking @{handle}...")
        try:
            user_id = get_user_id(handle, headers)
            if not user_id:
                if verbose:
                    print(f"  [skip] could not resolve @{handle}")
                continue
            tweets = get_recent_tweets(user_id, handle, headers, hours_back=LOOKBACK_HOURS)
            for t in tweets:
                if is_valid_tweet(t, seen_ids):
                    candidate_tweets.append(t)
            # Mark all fetched as seen regardless
            for t in tweets:
                seen_ids.add(t.get('id', ''))
        except RateLimitError:
            print("[rate_limit] Hit rate limit — exiting cleanly", file=sys.stderr)
            save_state(state)
            return
        except Exception as e:
            if verbose:
                print(f"  [error] @{handle}: {e}")
            continue

    if verbose:
        print(f"[browse] {len(candidate_tweets)} candidate tweets from {LOOKBACK_HOURS}h lookback")

    if not candidate_tweets:
        if verbose:
            print("[done] No candidates found — saving state")
        # Update seen IDs with rolling window
        state['seen_tweet_ids'] = list(seen_ids)[-MAX_SEEN_IDS:]
        save_state(state)
        return

    # ── Step 3: Score tweets ────────────────────────────────────────────────────
    scored = []
    for t in candidate_tweets:
        score, reason = score_tweet(t)
        scored.append((score, reason, t))

    scored.sort(key=lambda x: x[0], reverse=True)

    if verbose:
        print(f"[score] Top scores: {[s for s, r, t in scored[:5]]}")

    # ── Step 4+5: Draft replies and queue ──────────────────────────────────────
    # Load voice rules for Claude prompt
    voice_file = TWITTER_DIR / 'VOICE.md'
    voice_rules = voice_file.read_text() if voice_file.exists() else ""

    new_drafts = []
    new_finds = []
    dms_queued = 0

    for score, reason, tweet in scored:
        tweet_id = tweet.get('id', '')
        tweet_text = tweet.get('text', '')
        author = tweet.get('author_handle', 'unknown')

        if score >= 7.0:
            # Attempt to draft a reply
            draft_text = None
            if not dry_run:
                draft_text = draft_reply(tweet_text, author, voice_rules, state)
            else:
                draft_text = f"[DRY RUN draft for @{author}: {tweet_text[:60]}...]"

            if draft_text:
                entry = {
                    "queued_at": ts_now,
                    "type": "reply",
                    "original_tweet_id": tweet_id,
                    "original_tweet_text": tweet_text,
                    "original_author": f"@{author}",
                    "draft_text": draft_text,
                    "score": score,
                    "score_reason": reason,
                    "status": "pending",
                    "approved_at": None,
                    "posted_tweet_id": None,
                }
                if not dry_run:
                    queue_draft(entry)
                new_drafts.append(entry)
                state['total_drafts_queued'] = state.get('total_drafts_queued', 0) + 1

        elif 4.0 <= score < 7.0:
            note = f"Interesting signal from @{author} — {reason}"
            entry = {
                "queued_at": ts_now,
                "type": "interesting_find",
                "original_tweet_id": tweet_id,
                "original_tweet_text": tweet_text,
                "original_author": f"@{author}",
                "score": score,
                "score_reason": reason,
                "note": note,
                "status": "flagged",
            }
            if not dry_run:
                queue_draft(entry)
            new_finds.append(entry)

        # Stop once we have enough material
        if len(new_drafts) >= 3 and len(new_finds) >= 2:
            break

    if verbose:
        print(f"[queue] {len(new_drafts)} drafts, {len(new_finds)} interesting finds")

    # ── Step 6: Send Telegram DMs ───────────────────────────────────────────────
    dms_sent = 0
    all_dms = (
        [(format_reply_dm(e), e) for e in new_drafts] +
        [(format_find_dm(e), e) for e in new_finds]
    )

    for dm_text, entry in all_dms:
        if dms_sent >= MAX_DMS_PER_RUN:
            if verbose:
                print(f"[dm] Hit max {MAX_DMS_PER_RUN} DMs — saving rest for next run")
            break
        ok = send_telegram_dm(dm_text, dry_run=dry_run)
        if ok:
            dms_sent += 1
            # Track AI card use if applicable
            if 'ai' in dm_text.lower() and entry.get('type') == 'reply':
                state['ai_card_used_week'] = today

    if verbose:
        print(f"[dm] Sent {dms_sent} Telegram DMs")

    # ── Step 7: Update state ────────────────────────────────────────────────────
    if new_drafts or new_finds:
        state['last_engaged_ts'] = ts_now

    # Rolling window for seen IDs
    state['seen_tweet_ids'] = list(seen_ids)[-MAX_SEEN_IDS:]

    save_state(state)

    if verbose:
        print(f"[done] State saved. total_drafts_queued={state.get('total_drafts_queued', 0)}")


# ── CLI ──────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='@Friend0nClaw Twitter engagement engine',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 twitter/engagement_engine.py                # Normal run (cron mode)
  python3 twitter/engagement_engine.py --dry-run      # Preview without posting or DMing
  python3 twitter/engagement_engine.py --force        # Skip probability check
  python3 twitter/engagement_engine.py --verbose      # Show progress
  python3 twitter/engagement_engine.py --dry-run --force --verbose  # Full debug
        """
    )
    parser.add_argument('--dry-run',  action='store_true', help='Preview mode — no API writes, no Telegram DMs')
    parser.add_argument('--force',    action='store_true', help='Skip probability trigger check')
    parser.add_argument('--verbose',  action='store_true', help='Print step-by-step progress')
    args = parser.parse_args()

    run(dry_run=args.dry_run, force=args.force, verbose=args.verbose)
