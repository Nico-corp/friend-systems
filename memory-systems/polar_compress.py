#!/usr/bin/env python3
"""
polar_compress.py — Memory compression tool for daily logs.

Inspired by PolarQuant KV cache compression (Google Research / @aliestaha):
  - Pair related memory entries (same domain/decision thread/correction context)
  - Distill each pair into a single principle using LLM
  - Compress the daily log: fewer entries, same information density

Usage:
    python3 tools/polar_compress.py memory/2026-03-27.md
    python3 tools/polar_compress.py memory/2026-03-27.md --dry-run
    python3 tools/polar_compress.py --help
    echo "..." | python3 tools/polar_compress.py -
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKSPACE = Path(__file__).parent.parent
OPENAI_KEY_PATH = Path.home() / ".openclaw" / "secrets" / "openai_api_key.txt"
OUTPUT_DIR = WORKSPACE / "memory" / "compressed"

MODEL = "gpt-4o-mini"

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "options": [
        "strategy", "s011", "s017", "s018", "s002", "s003", "s004", "s005",
        "s006", "s007", "s009", "s010", "s015", "s016", "s017", "s018",
        "vix", "regime", "csp", "iron condor", "long call", "long put",
        "exit monitor", "paper trad", "whale", "flow", "uw ", "tradier",
        "delta", "iv ", "dte", "premium", "spread", "options", "position siz",
        "entry", "conviction score", "put_call", "vrp", "bear call",
        "bull put", "iron butterfly", "straddle", "pmcc",
    ],
    "portfolio": [
        "cml", "401k", "parametric", "portfolio", "ticker", "equity",
        "position", "drift", "allocation", "tax-loss", "earnings",
    ],
    "twitter": [
        "tweet", "twitter", "x api", "@friend0nclaw", "voice.md", "article",
        "reply", "engagement", "cron.*twitter", "twitter.*cron",
        "monday regime", "regime read",
    ],
    "pi": [
        "raspberry pi", "nicorp-pi", "/mnt/x9", "x9", "rsync", "backup",
        "fstab", "ext4", "ssh keypair", "backup.sh",
    ],
    "cron": [
        "cron", "isolated", "consecutive error", "cron.*health", "jobs.json",
        "heartbeat", "announce",
    ],
    "memory": [
        "memory.md", "memory-permanent", "daily log", "memory gap",
        "compaction", "flush", "session log", "memory update",
    ],
    "git": [
        "pr #", "branch", "merge", "commit", "push origin", "pull request",
        "git log", "feat/", "fix/",
    ],
    "corrections": [
        "correction", "log_correction", "corrections.jsonl", "nico correct",
        "hard rule", "standing order",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    try:
        key = OPENAI_KEY_PATH.read_text().strip()
        if not key:
            raise ValueError("API key file is empty")
        return key
    except FileNotFoundError:
        sys.exit(f"[polar_compress] API key not found at {OPENAI_KEY_PATH}")


def classify_domain(text: str) -> str:
    """Return the most-matched domain for a text snippet (deterministic)."""
    lower = text.lower()
    scores: dict[str, int] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in lower)
        if score:
            scores[domain] = score
    if not scores:
        return "general"
    return max(scores, key=lambda d: (scores[d], d))  # tie-break: alphabetical


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def parse_sections(content: str) -> list[dict]:
    """
    Split markdown into sections. Each section is a dict:
      { "header": str | None, "entries": list[str], "raw_prefix": str }

    An "entry" is a paragraph, bullet block, or table that conveys a fact.
    """
    lines = content.splitlines(keepends=True)
    sections = []
    current_header = None
    current_lines: list[str] = []

    for line in lines:
        if re.match(r"^#{1,3} ", line):
            if current_lines or current_header is not None:
                sections.append({
                    "header": current_header,
                    "raw": "".join(current_lines),
                })
            current_header = line.rstrip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines or current_header is not None:
        sections.append({
            "header": current_header,
            "raw": "".join(current_lines),
        })

    return sections


def split_entries(text: str) -> list[str]:
    """
    Split a section's raw text into logical entries (paragraphs / bullet groups).
    Keeps tables together. Returns list of non-empty string entries.
    """
    # Split on double newlines (paragraph boundaries)
    chunks = re.split(r"\n{2,}", text.strip())
    entries = []
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) > 30:  # ignore tiny whitespace chunks
            entries.append(chunk)
    return entries


# ---------------------------------------------------------------------------
# Pairing logic (deterministic)
# ---------------------------------------------------------------------------

def pair_entries(entries: list[str]) -> list[tuple[int, int] | int]:
    """
    Greedy deterministic pairing:
    1. Classify each entry by domain.
    2. Sort by (domain, index) so same-domain entries are adjacent.
    3. Pair consecutive same-domain entries.
    4. Unpaired → returned as solo ints.

    Returns list of (i, j) pairs or solo i indices, in original order
    to preserve output structure.
    """
    if len(entries) <= 1:
        return list(range(len(entries)))

    # Build domain assignments
    domain_map = {i: classify_domain(entries[i]) for i in range(len(entries))}

    # Group by domain (preserve insertion order via dict)
    from collections import defaultdict
    domain_groups: dict[str, list[int]] = defaultdict(list)
    for i, domain in sorted(domain_map.items()):
        domain_groups[domain].append(i)

    used: set[int] = set()
    pairs: list[tuple[int, int]] = []

    for domain, indices in domain_groups.items():
        # Only pair within the same domain
        i = 0
        while i + 1 < len(indices):
            a, b = indices[i], indices[i + 1]
            pairs.append((a, b))
            used.add(a)
            used.add(b)
            i += 2

    # Collect results in original order
    pair_map = {min(a, b): (a, b) for (a, b) in pairs}
    result = []
    seen = set()
    for i in range(len(entries)):
        if i in seen:
            continue
        if i in pair_map:
            a, b = pair_map[i]
            result.append((a, b))
            seen.add(a)
            seen.add(b)
        elif i not in used:
            result.append(i)
            seen.add(i)

    return result


# ---------------------------------------------------------------------------
# LLM distillation
# ---------------------------------------------------------------------------

def distill_pair(entry_a: str, entry_b: str, api_key: str) -> str:
    """Call gpt-4o-mini to distill two entries into one principle."""
    import urllib.request
    import json

    prompt = (
        "Given these two related memory entries, write a single concise principle "
        "(max 2 sentences) that captures both. "
        "Preserve all specific details (file paths, numbers, dates, PR numbers, ticker symbols). "
        "Output only the principle, no preamble.\n\n"
        f"Entry 1:\n{entry_a}\n\n"
        f"Entry 2:\n{entry_b}"
    )

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.0,
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        # Fallback: return first entry truncated (never fail silently on full loss)
        print(f"  [warn] LLM call failed ({e}), falling back to entry 1", file=sys.stderr)
        return entry_a


# ---------------------------------------------------------------------------
# Core compression
# ---------------------------------------------------------------------------

def compress_section(
    header: str | None,
    raw: str,
    api_key: str,
    dry_run: bool,
) -> tuple[str, int, int]:
    """
    Compress a single markdown section.
    Returns (compressed_text, original_entry_count, compressed_entry_count).
    """
    entries = split_entries(raw)
    if not entries:
        return (f"{header}\n" if header else "") + raw, 0, 0

    arrangement = pair_entries(entries)
    total_original = len(entries)
    total_compressed = 0
    output_parts = []

    for item in arrangement:
        if isinstance(item, tuple):
            a, b = item
            entry_a = entries[a]
            entry_b = entries[b]
            domain = classify_domain(entry_a + " " + entry_b)

            if dry_run:
                print(f"\n  [DRY-RUN PAIR] domain={domain}")
                print(f"    Entry A ({a}): {entry_a[:120].replace(chr(10), ' ')}...")
                print(f"    Entry B ({b}): {entry_b[:120].replace(chr(10), ' ')}...")
                # In dry-run, keep both entries unchanged
                output_parts.append(entry_a)
                output_parts.append(entry_b)
                total_compressed += 2
            else:
                print(f"    Distilling pair (domain={domain}, A={a}, B={b})...", end=" ", flush=True)
                principle = distill_pair(entry_a, entry_b, api_key)
                print("✓")
                # Tag the compressed entry for traceability
                tagged = f"<!-- compressed: {stable_hash(entry_a + entry_b)} -->\n{principle}"
                output_parts.append(tagged)
                total_compressed += 1
        else:
            # Solo entry — pass through
            output_parts.append(entries[item])
            total_compressed += 1

    # Reconstruct section
    body = "\n\n".join(output_parts)
    header_line = (f"{header}\n\n" if header else "")
    return header_line + body, total_original, total_compressed


def compress_file(
    input_path: Path,
    api_key: str,
    dry_run: bool,
) -> tuple[str, int, int]:
    """
    Full file compression. Returns (output_text, total_entries, compressed_entries).
    """
    content = input_path.read_text(encoding="utf-8")
    if not content.strip():
        print("[polar_compress] File is empty — nothing to compress.")
        return content, 0, 0

    sections = parse_sections(content)
    output_sections = []
    grand_original = 0
    grand_compressed = 0

    for section in sections:
        header = section["header"]
        raw = section["raw"]

        # Don't compress tiny sections (title, metadata blocks < 3 entries)
        entries = split_entries(raw)
        if len(entries) < 2:
            # Pass through as-is
            output_sections.append(
                (f"{header}\n\n" if header else "") + raw.lstrip("\n")
            )
            grand_original += len(entries)
            grand_compressed += len(entries)
            continue

        section_label = header or "(preamble)"
        print(f"  Section: {section_label[:60]} — {len(entries)} entries")

        compressed, orig, comp = compress_section(header, raw, api_key, dry_run)
        output_sections.append(compressed)
        grand_original += orig
        grand_compressed += comp

    return "\n\n".join(output_sections), grand_original, grand_compressed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "polar_compress — PolarQuant-inspired memory compression for daily logs.\n"
            "Pairs related facts, distills each pair into a single principle via LLM,\n"
            "and writes a compressed version to memory/compressed/."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        metavar="FILE",
        help="Path to daily memory file (e.g. memory/2026-03-27.md). Use - for stdin.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print identified pairs without calling the LLM or writing output.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(OUTPUT_DIR),
        metavar="DIR",
        help=f"Directory to write compressed output (default: {OUTPUT_DIR})",
    )

    args = parser.parse_args()

    # Resolve input
    if args.input == "-":
        # Read from stdin
        content = sys.stdin.read()
        input_path = None
        out_filename = "stdin-compressed.md"
    else:
        input_path = Path(args.input)
        if not input_path.is_absolute():
            input_path = WORKSPACE / input_path
        if not input_path.exists():
            sys.exit(f"[polar_compress] File not found: {input_path}")
        out_filename = input_path.name

    # Load API key (needed even for dry-run to avoid surprises later)
    api_key = load_api_key()

    # Print header
    print(f"[polar_compress] {'DRY RUN — ' if args.dry_run else ''}Compressing: {args.input}")
    print(f"[polar_compress] Model: {MODEL}")
    print()

    # Run compression
    if input_path is not None:
        output_text, orig, comp = compress_file(input_path, api_key, args.dry_run)
    else:
        # stdin path
        tmp = Path("/tmp/_polar_stdin.md")
        tmp.write_text(content)
        output_text, orig, comp = compress_file(tmp, api_key, args.dry_run)
        tmp.unlink(missing_ok=True)

    # Stats
    print()
    if orig == 0:
        print("[polar_compress] No compressible entries found.")
        return

    reduction = round((1 - comp / orig) * 100, 1) if orig > 0 else 0
    print(f"[polar_compress] Compressed {orig} entries → {comp} entries ({reduction}% reduction)")

    if args.dry_run:
        print("[polar_compress] DRY RUN complete — no files written.")
        return

    # Write output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / out_filename
    out_path.write_text(output_text, encoding="utf-8")
    print(f"[polar_compress] Written → {out_path}")
    print(f"[polar_compress] Original preserved at: {input_path or 'stdin (not preserved)'}")


if __name__ == "__main__":
    main()
