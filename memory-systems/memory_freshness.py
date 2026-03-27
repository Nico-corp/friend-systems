#!/usr/bin/env python3
"""
memory_freshness.py — Scan MEMORY.md for valid_until tags and flag stale facts.

Usage:
    python3 tools/memory_freshness.py              # print stale warnings
    python3 tools/memory_freshness.py --json       # JSON output for cron consumption
    python3 tools/memory_freshness.py --silent     # exit 1 if stale, no output (gate mode)

Output (default):
    ⚠️  Stale memory entries detected:
      - VIX level / regime (expired 2h 14m ago)
      - Portfolio totals (expired 6h 3m ago)

    ✅ Fresh: paper trade count, earnings dates

Exit codes:
    0 = all fresh (or no valid_until tags found)
    1 = one or more stale entries detected
"""

import re
import sys
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path(__file__).parent.parent
MEMORY_FILE = WORKSPACE / "MEMORY.md"
ET = ZoneInfo("America/New_York")

# Regex: captures label + valid_until value
# Matches patterns like:
#   **Regime: BEAR** (VIX 28.28 — valid_until: 2026-03-27 market open)
#   **Paper trades: 2 open** (valid_until: 2026-03-27T09:30 ET)
# Label = bold text only. valid_until = everything after "valid_until:" inside parens.
PATTERN = re.compile(
    r"\*\*([^*\n]+)\*\*\s*\([^)\n]*?valid_until:\s*([^)]+)\)",
    re.IGNORECASE
)

# Special string tokens → resolved datetime
SPECIAL_TOKENS = {
    "market open": "09:30",
    "next market open": "09:30",
    "market close": "16:00",
    "next market close": "16:00",
}


def parse_valid_until(raw: str, now: datetime) -> datetime | None:
    """Parse a valid_until string into a UTC-aware datetime."""
    raw = raw.strip()

    # Resolve special tokens (e.g. "2026-03-27 market open")
    for token, time_str in SPECIAL_TOKENS.items():
        if token in raw.lower():
            date_part = re.search(r"\d{4}-\d{2}-\d{2}", raw)
            if date_part:
                dt_str = f"{date_part.group()}T{time_str}"
                try:
                    dt = datetime.fromisoformat(dt_str).replace(tzinfo=ET)
                    return dt.astimezone(timezone.utc)
                except ValueError:
                    pass
            return None

    # Try ISO 8601 variants
    # Strip trailing timezone labels like "ET"
    clean = re.sub(r"\s*(ET|UTC|EST|EDT)$", "", raw, flags=re.IGNORECASE).strip()

    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(clean, fmt)
            # Assume ET if no tz info
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def human_delta(seconds: float) -> str:
    """Convert seconds to human-readable duration."""
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m {s}s" if s else f"{m}m"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}d {h}h" if h else f"{d}d"


def scan_memory(memory_path: Path) -> list[dict]:
    """Scan MEMORY.md and return list of fact entries with freshness status."""
    if not memory_path.exists():
        return []

    content = memory_path.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)
    results = []

    for match in PATTERN.finditer(content):
        label = match.group(1).strip()
        raw_until = match.group(2).strip()
        valid_until = parse_valid_until(raw_until, now)

        if valid_until is None:
            results.append({
                "label": label,
                "raw_until": raw_until,
                "status": "unparseable",
                "stale": False,
                "expired_seconds": None,
            })
            continue

        expired_seconds = (now - valid_until).total_seconds()
        stale = expired_seconds > 0

        results.append({
            "label": label,
            "raw_until": raw_until,
            "valid_until_utc": valid_until.isoformat(),
            "status": "stale" if stale else "fresh",
            "stale": stale,
            "expired_seconds": expired_seconds if stale else None,
            "expires_in_seconds": -expired_seconds if not stale else None,
        })

    return results


def format_warning(results: list[dict]) -> str:
    """Format human-readable output."""
    if not results:
        return "✅ No valid_until tags found in MEMORY.md"

    stale = [r for r in results if r["stale"]]
    fresh = [r for r in results if not r["stale"] and r["status"] != "unparseable"]
    unparseable = [r for r in results if r["status"] == "unparseable"]

    lines = []

    if stale:
        lines.append("⚠️  Stale memory entries detected:")
        for r in stale:
            delta = human_delta(r["expired_seconds"])
            lines.append(f"  • {r['label']} (expired {delta} ago)")

    if fresh:
        labels = ", ".join(r["label"] for r in fresh)
        lines.append(f"✅ Fresh: {labels}")

    if unparseable:
        labels = ", ".join(f"{r['label']} ({r['raw_until']})" for r in unparseable)
        lines.append(f"⚠️  Could not parse valid_until for: {labels}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Check MEMORY.md for stale valid_until entries")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--silent", action="store_true", help="No output; exit 1 if stale")
    parser.add_argument("--memory", default=str(MEMORY_FILE), help="Path to MEMORY.md")
    args = parser.parse_args()

    memory_path = Path(args.memory)
    results = scan_memory(memory_path)
    any_stale = any(r["stale"] for r in results)

    if args.silent:
        sys.exit(1 if any_stale else 0)

    if args.json:
        output = {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "memory_file": str(memory_path),
            "any_stale": any_stale,
            "entries": results,
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_warning(results))

    sys.exit(1 if any_stale else 0)


if __name__ == "__main__":
    main()
