#!/usr/bin/env python3
"""
promote_corrections.py — Scan corrections.jsonl for graduation candidates
and promote them to MEMORY-PERMANENT.md.

Called by: self-improvement-weekly cron (Sunday 10 PM ET)
Manual:    python3 tools/promote_corrections.py [--dry-run]

What it does:
1. Reads memory/corrections.jsonl
2. Finds corrections with count >= GRADUATION_THRESHOLD (3)
3. Checks if already in MEMORY-PERMANENT.md (skip if so)
4. Appends new permanent rules under ## Graduated Corrections section
5. Marks promoted entries in corrections.jsonl with "promoted": true
6. Prints a summary for the weekly self-improvement cron to include in its report
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CORRECTIONS_FILE    = Path(__file__).parent.parent / "memory" / "corrections.jsonl"
PERMANENT_FILE      = Path(__file__).parent.parent / "MEMORY-PERMANENT.md"
GRADUATION_THRESHOLD = 3
SECTION_HEADER      = "## Graduated Corrections"


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


def normalize(text: str) -> str:
    return text.lower().strip()


def aggregate(entries: list) -> dict:
    """Group by normalized correction text, sum counts."""
    agg: dict = {}
    for e in entries:
        key = normalize(e.get("correction", ""))
        if not key:
            continue
        if key not in agg:
            agg[key] = {
                "correction": e["correction"],
                "category": e["category"],
                "count": 0,
                "promoted": e.get("promoted", False),
                "last_ts": "",
                "contexts": [],
            }
        agg[key]["count"] += 1
        agg[key]["last_ts"] = max(agg[key]["last_ts"], e.get("ts", ""))
        if e.get("context") and e["context"] not in agg[key]["contexts"]:
            agg[key]["contexts"].append(e["context"])
        if e.get("promoted"):
            agg[key]["promoted"] = True
    return agg


def already_in_permanent(text: str, permanent_content: str) -> bool:
    """Check if the correction text is already captured in MEMORY-PERMANENT.md."""
    return normalize(text)[:40] in normalize(permanent_content)


def promote(candidates: list, dry_run: bool = False) -> list:
    """Append candidates to MEMORY-PERMANENT.md under Graduated Corrections section."""
    if not candidates:
        return []

    permanent_content = PERMANENT_FILE.read_text() if PERMANENT_FILE.exists() else ""
    new_rules = []

    for c in candidates:
        if already_in_permanent(c["correction"], permanent_content):
            print(f"  [skip — already permanent] {c['correction'][:60]}")
            continue
        new_rules.append(c)

    if not new_rules:
        print("  All candidates already in MEMORY-PERMANENT.md.")
        return []

    if dry_run:
        print(f"\n[DRY RUN] Would promote {len(new_rules)} rules:")
        for r in new_rules:
            print(f"  [{r['category']}] {r['correction']}")
        return new_rules

    # Build the new rules block
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"\n### Promoted {today}\n"]
    for r in new_rules:
        lines.append(f"- **[{r['category']}]** {r['correction']}")
        if r.get("contexts"):
            lines.append(f"  *(Corrected {r['count']}x. Context: {r['contexts'][-1]})*")
    block = "\n".join(lines) + "\n"

    # Insert or append section
    if SECTION_HEADER in permanent_content:
        updated = permanent_content + block
    else:
        updated = permanent_content.rstrip() + f"\n\n---\n\n{SECTION_HEADER}\n{block}"

    PERMANENT_FILE.write_text(updated)
    print(f"  ✅ Promoted {len(new_rules)} rules to MEMORY-PERMANENT.md")

    # Mark as promoted in corrections.jsonl
    all_entries = load_corrections()
    promoted_keys = {normalize(r["correction"]) for r in new_rules}
    updated_entries = []
    for e in all_entries:
        if normalize(e.get("correction", "")) in promoted_keys:
            e["promoted"] = True
        updated_entries.append(e)

    with open(CORRECTIONS_FILE, "w") as f:
        for e in updated_entries:
            f.write(json.dumps(e) + "\n")

    return new_rules


def main():
    dry_run = "--dry-run" in sys.argv

    entries = load_corrections()
    if not entries:
        print("No corrections logged yet.")
        return

    agg = aggregate(entries)
    total = len(agg)
    candidates = [v for v in agg.values()
                  if v["count"] >= GRADUATION_THRESHOLD and not v["promoted"]]

    print(f"Corrections summary: {total} unique corrections logged")
    print(f"Graduation candidates (>= {GRADUATION_THRESHOLD}x, not yet promoted): {len(candidates)}")

    if candidates:
        print("\nCandidates:")
        for c in sorted(candidates, key=lambda x: -x["count"]):
            print(f"  [{c['count']}x] [{c['category']}] {c['correction']}")
        print()
        promoted = promote(candidates, dry_run=dry_run)
        if promoted and not dry_run:
            print("\nSUMMARY FOR WEEKLY REPORT:")
            print(f"Promoted {len(promoted)} correction(s) to permanent rules:")
            for r in promoted:
                print(f"  • [{r['category']}] {r['correction']}")
    else:
        print("No new candidates to promote.")
        # Still show near-threshold ones
        near = [v for v in agg.values()
                if v["count"] == GRADUATION_THRESHOLD - 1 and not v["promoted"]]
        if near:
            print(f"\nNear threshold ({GRADUATION_THRESHOLD - 1}x — one more repetition promotes):")
            for c in near:
                print(f"  [{c['count']}x] [{c['category']}] {c['correction']}")


if __name__ == "__main__":
    main()
