#!/usr/bin/env python3
"""
self_eval.py — Proactive post-session self-evaluation for Friend.

Reads a session summary or daily memory log, scores it against known failure
modes, and logs high-confidence hits to memory/corrections.jsonl (same file
used by log_correction.py). Feeds the existing 3x graduation loop.

Inspired by: Hyperagents paper (arxiv 2603.19461) — meta-level improvements
that accumulate across runs.

Usage:
    python3 tools/self_eval.py memory/2026-03-25.md
    cat memory/2026-03-25.md | python3 tools/self_eval.py --stdin
    python3 tools/self_eval.py memory/2026-03-25.md --dry-run
    python3 tools/self_eval.py --list-modes
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CORRECTIONS_FILE = Path(__file__).parent.parent / "memory" / "corrections.jsonl"
GRADUATION_THRESHOLD = 3

# Confidence thresholds: minimum keyword hits to reach each level
# high  = 2+ distinct keywords from the mode's list found in text
# medium = 1 keyword found
# low   = 0 keywords (no signal)
HIGH_THRESHOLD = 2
MEDIUM_THRESHOLD = 1

FAILURE_MODES = [
    {
        "id": "FM01",
        "category": "data",
        "description": "Stated a portfolio number without reading source file first",
        "keywords": ["portfolio", "position", "weight", "%", "allocation", "CML", "401k"],
        "correction": "Always read portfolio/data/portfolio_state.json or holdings.json before stating any portfolio number",
    },
    {
        "id": "FM02",
        "category": "data",
        "description": "Stated an earnings date without reading earnings_calendar.json",
        "keywords": ["earnings", "reports", "Q1", "Q2", "Q3", "Q4"],
        "correction": "Always read portfolio/data/earnings_calendar.json before stating any earnings date",
    },
    {
        "id": "FM03",
        "category": "trading",
        "description": "Referenced options regime or VIX level without running regime check",
        "keywords": ["regime", "VIX", "BEAR", "BULL", "NEUTRAL"],
        "correction": "Always run options/signals/regime.py or read live briefing before stating regime or VIX level",
    },
    {
        "id": "FM04",
        "category": "process",
        "description": "Deferred an action with next session or later language",
        "keywords": ["next session", "next time", "later", "tomorrow", "I'll do that later", "we can do that later"],
        "correction": "Never defer actions with next session language — do it now or explicitly ask Nico to defer",
    },
    {
        "id": "FM05",
        "category": "process",
        "description": "Missed startup sequence file reads",
        "keywords": ["startup", "session start", "compaction"],
        "correction": "Run full startup sequence on every session start including post-compaction",
    },
    {
        "id": "FM06",
        "category": "system",
        "description": "Used exec/curl for provider messaging instead of OpenClaw routing",
        "keywords": ["curl", "send message", "webhook"],
        "correction": "Never use exec/curl for provider messaging — OpenClaw handles all routing internally",
    },
    {
        "id": "FM07",
        "category": "trading",
        "description": "Mentioned or suggested paper trade as public tweet content",
        "keywords": ["paper trade", "tweet", "post", "paper position"],
        "correction": "Paper trades are never posted publicly — tweet_trade.py is hard-gated until LIVE_MODE=True",
    },
    {
        "id": "FM08",
        "category": "voice",
        "description": "Used padding phrases",
        "keywords": ["great question", "as I mentioned", "that said", "certainly", "absolutely", "of course"],
        "correction": "No padding phrases — answer first, no filler",
    },
]


# ---------------------------------------------------------------------------
# Scoring logic
# ---------------------------------------------------------------------------

def find_keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """Return list of keywords (case-insensitive) found in text. Deduped."""
    text_lower = text.lower()
    hits = []
    seen = set()
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower not in seen and re.search(re.escape(kw_lower), text_lower):
            hits.append(kw)
            seen.add(kw_lower)
    return hits


def score_mode(text: str, mode: dict) -> dict:
    """
    Score a single failure mode against the session text.

    Returns:
        {
            "id": str,
            "confidence": "high" | "medium" | "low",
            "hits": [str, ...],   # matching keywords
            "hit_count": int,
        }
    """
    hits = find_keyword_hits(text, mode["keywords"])
    count = len(hits)

    if count >= HIGH_THRESHOLD:
        confidence = "high"
    elif count >= MEDIUM_THRESHOLD:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "id": mode["id"],
        "confidence": confidence,
        "hits": hits,
        "hit_count": count,
    }


def score_session(text: str) -> list[dict]:
    """Score all failure modes. Returns list of score dicts."""
    return [score_mode(text, mode) for mode in FAILURE_MODES]


# ---------------------------------------------------------------------------
# Corrections persistence
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return text.lower().strip()


def load_corrections() -> list:
    if not CORRECTIONS_FILE.exists():
        return []
    entries = []
    with open(CORRECTIONS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def count_occurrences(entries: list, correction: str) -> int:
    key = normalize(correction)
    return sum(1 for e in entries if normalize(e.get("correction", "")) == key)


def log_hit(mode: dict, score: dict, session_label: str, dry_run: bool = False) -> dict:
    """
    Append a high-confidence hit to corrections.jsonl.
    Returns the entry dict.
    """
    CORRECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    entries = load_corrections()
    existing_count = count_occurrences(entries, mode["correction"])
    new_count = existing_count + 1

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": mode["category"],
        "correction": mode["correction"],
        "context": f"Self-eval {mode['id']} detected in session {session_label}",
        "count": new_count,
        "source": "self",
        "failure_mode": mode["id"],
        "confidence": score["confidence"],
    }

    if not dry_run:
        with open(CORRECTIONS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    return {**entry, "graduate": new_count >= GRADUATION_THRESHOLD}


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

CONF_ICON = {"high": "🔴", "medium": "🟡", "low": "⚪"}


def print_summary(scores: list[dict], logged: list[dict], dry_run: bool) -> None:
    """Print a human-readable scored summary."""
    high = [s for s in scores if s["confidence"] == "high"]
    medium = [s for s in scores if s["confidence"] == "medium"]

    if not high and not medium:
        print("✅ CLEAN — no failure mode signals detected")
        return

    print(f"{'[DRY RUN] ' if dry_run else ''}Session Self-Eval Report")
    print("=" * 50)

    if high:
        print(f"\n🔴 HIGH CONFIDENCE ({len(high)} mode{'s' if len(high) != 1 else ''}) — logged to corrections.jsonl:")
        for s in high:
            mode = next(m for m in FAILURE_MODES if m["id"] == s["id"])
            log_entry = next((l for l in logged if l.get("failure_mode") == s["id"]), None)
            graduate_tag = " ⚡ GRADUATE" if log_entry and log_entry.get("graduate") else ""
            print(f"  {s['id']} [{mode['category']}] {mode['description']}")
            print(f"       Keywords matched: {', '.join(s['hits'])}")
            if log_entry:
                print(f"       Count: {log_entry['count']}x{graduate_tag}")
            print(f"       → Rule: {mode['correction']}")

    if medium:
        print(f"\n🟡 MEDIUM CONFIDENCE ({len(medium)} mode{'s' if len(medium) != 1 else ''}) — warning only, NOT logged:")
        for s in medium:
            mode = next(m for m in FAILURE_MODES if m["id"] == s["id"])
            print(f"  {s['id']} [{mode['category']}] {mode['description']}")
            print(f"       Keywords matched: {', '.join(s['hits'])}")

    graduates = [l for l in logged if l.get("graduate")]
    if graduates:
        print(f"\n⚡ GRADUATION SIGNALS ({len(graduates)}):")
        for g in graduates:
            print(f"  {g['failure_mode']} reached {g['count']}x — promote to MEMORY-PERMANENT.md")
            print(f"  Rule: {g['correction']}")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="self_eval.py",
        description=(
            "Post-session self-evaluation: score session text against known failure modes "
            "and log high-confidence hits to memory/corrections.jsonl."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 tools/self_eval.py memory/2026-03-25.md
  cat memory/2026-03-25.md | python3 tools/self_eval.py --stdin
  python3 tools/self_eval.py memory/2026-03-25.md --dry-run
  python3 tools/self_eval.py --list-modes
        """,
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to session summary or daily memory log",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read session text from stdin instead of a file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print results but do NOT write to corrections.jsonl",
    )
    parser.add_argument(
        "--list-modes",
        action="store_true",
        help="List all registered failure modes and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON (machine-readable)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # --list-modes
    if args.list_modes:
        print(f"Registered failure modes ({len(FAILURE_MODES)}):\n")
        for mode in FAILURE_MODES:
            print(f"  {mode['id']}  [{mode['category']}]  {mode['description']}")
            print(f"       Keywords: {', '.join(mode['keywords'])}")
            print(f"       Rule:     {mode['correction']}")
            print()
        return 0

    # Read session text
    text = ""

    if args.stdin:
        text = sys.stdin.read()
    elif args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"⚠️  File not found: {path}", file=sys.stderr)
            # Not a crash — score empty text (will be clean)
            text = ""
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"⚠️  Could not read {path}: {e}", file=sys.stderr)
                text = ""
    else:
        parser.print_help()
        return 0

    # Derive session label for context field
    if args.file:
        # e.g. "memory/2026-03-25.md" → "2026-03-25"
        stem = Path(args.file).stem
        session_label = stem
    else:
        session_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Score
    scores = score_session(text)

    # Log high-confidence hits
    logged = []
    for score in scores:
        if score["confidence"] == "high":
            mode = next(m for m in FAILURE_MODES if m["id"] == score["id"])
            entry = log_hit(mode, score, session_label, dry_run=args.dry_run)
            logged.append(entry)

    # Output
    if args.json_output:
        output = {
            "session": session_label,
            "dry_run": args.dry_run,
            "scores": scores,
            "logged": logged,
        }
        print(json.dumps(output, indent=2))
    else:
        print_summary(scores, logged, dry_run=args.dry_run)

    # Exit code: 1 if any failures detected (high or medium), 0 if clean
    any_failures = any(s["confidence"] in ("high", "medium") for s in scores)
    return 1 if any_failures else 0


if __name__ == "__main__":
    sys.exit(main())
