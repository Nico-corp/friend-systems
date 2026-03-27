#!/usr/bin/env python3
"""
log_correction.py — Log a correction from Nico into corrections.jsonl.

Usage (Friend calls this directly mid-conversation when Nico corrects something):
    python3 tools/log_correction.py \
        --category "voice" \
        --correction "Don't use the word 'leverage' in tweets" \
        --context "Nico corrected tweet draft, Mar 25 2026"

Categories:
    voice       — tone, word choice, style preferences
    data        — how to source or present data
    trading     — options/CML rules or execution preferences
    process     — workflow, format, structure
    persona     — personality, relationship, communication style
    system      — infra, routing, cron behavior

Output: appends to memory/corrections.jsonl, prints count for that correction text.
If count reaches GRADUATION_THRESHOLD (3), prints a GRADUATE signal.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CORRECTIONS_FILE = Path(__file__).parent.parent / "memory" / "corrections.jsonl"
GRADUATION_THRESHOLD = 3


def normalize(text: str) -> str:
    """Lowercase + strip for dedup comparison."""
    return text.lower().strip()


def load_all() -> list:
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


def log_correction(category: str, correction: str, context: str) -> dict:
    """Append correction to log. Returns result dict with count + graduate flag."""
    CORRECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    entries = load_all()
    existing_count = count_occurrences(entries, correction)
    new_count = existing_count + 1

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "correction": correction,
        "context": context,
        "count": new_count,
    }

    with open(CORRECTIONS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    result = {
        "logged": True,
        "correction": correction,
        "category": category,
        "count": new_count,
        "graduate": new_count >= GRADUATION_THRESHOLD,
    }

    return result


def main():
    parser = argparse.ArgumentParser(description="Log a correction from Nico")
    parser.add_argument("--category", default=None,
                        choices=["voice", "data", "trading", "process", "persona", "system"],
                        help="Correction category")
    parser.add_argument("--correction", default=None,
                        help="The correction text (what to remember)")
    parser.add_argument("--context", default="",
                        help="Context — when/why this correction was made")
    parser.add_argument("--list", action="store_true",
                        help="List all corrections and their counts")
    parser.add_argument("--graduation-candidates", action="store_true",
                        help="List corrections at or above the graduation threshold")
    args = parser.parse_args()

    if args.list:
        entries = load_all()
        # Aggregate by normalized correction text
        agg: dict = {}
        for e in entries:
            key = normalize(e["correction"])
            if key not in agg:
                agg[key] = {"correction": e["correction"], "category": e["category"], "count": 0, "last_ts": ""}
            agg[key]["count"] += 1
            agg[key]["last_ts"] = max(agg[key]["last_ts"], e.get("ts", ""))
        for item in sorted(agg.values(), key=lambda x: -x["count"]):
            flag = " ⚡ GRADUATE" if item["count"] >= GRADUATION_THRESHOLD else ""
            print(f"[{item['count']}x] [{item['category']}] {item['correction']}{flag}")
            print(f"      Last seen: {item['last_ts'][:10]}")
        return

    if args.graduation_candidates:
        entries = load_all()
        agg: dict = {}
        for e in entries:
            key = normalize(e["correction"])
            if key not in agg:
                agg[key] = {"correction": e["correction"], "category": e["category"], "count": 0}
            agg[key]["count"] += 1
        candidates = [v for v in agg.values() if v["count"] >= GRADUATION_THRESHOLD]
        if candidates:
            print(f"GRADUATION CANDIDATES ({len(candidates)}):")
            for c in sorted(candidates, key=lambda x: -x["count"]):
                print(f"  [{c['count']}x] [{c['category']}] {c['correction']}")
        else:
            print("No graduation candidates yet.")
        return

    if not args.category or not args.correction:
        parser.error("--category and --correction are required when not using --list or --graduation-candidates")

    result = log_correction(args.category, args.correction, args.context)
    print(json.dumps(result))

    if result["graduate"]:
        print(f"\n⚡ GRADUATE: This correction has been seen {result['count']}x — promote to MEMORY-PERMANENT.md")
        print(f"   Category: {result['category']}")
        print(f"   Rule: {result['correction']}")


if __name__ == "__main__":
    main()
