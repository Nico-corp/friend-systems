#!/usr/bin/env python3
"""
Brief domain evaluator.
Validates that REQUIRED_SECTIONS are present in a canonical template string,
checks weight sum, and scores length constraints.
Score = sections_present/total_required * 0.7 + structure_score * 0.3
Prints a single float to stdout.
"""
import sys
import os

DOMAIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DOMAIN_DIR)
import target  # noqa: E402

# Canonical template representing a well-structured brief
TEMPLATE = """
## regime
Market regime: trending bullish. VIX at 16.2, below 20-day avg.

## positions
Current positions: 3 CSPs, 1 earnings play. Net delta: +0.45.

## signals
Top signals: NVDA momentum score 8.5, AMD earnings crush setup IV rank 62.

## news
Key news: Fed hold expected Wednesday. OPEC meeting Friday. Tech earnings next week.

## weekly_summary
Weekly: S&P +1.2%, Nasdaq +1.8%. Sector rotation into tech continues.

## sector_rotation
Top sectors: Technology (+2.1%), Energy (+1.4%). Laggards: Utilities (-0.8%).
"""

def score_sections(template_text):
    """Score based on required sections present."""
    present = sum(1 for s in target.REQUIRED_SECTIONS if f"## {s}" in template_text)
    return present / len(target.REQUIRED_SECTIONS) if target.REQUIRED_SECTIONS else 0.0

def structure_score():
    """
    Score the structural integrity of target.py settings:
    - Weights sum to ~1.0
    - Length constraints are sane
    - Optional sections are boolean
    """
    score = 0.0
    checks = 0

    # Weight sum
    weight_sum = (
        target.REGIME_SECTION_WEIGHT
        + target.POSITIONS_SECTION_WEIGHT
        + target.SIGNALS_SECTION_WEIGHT
        + target.NEWS_SECTION_WEIGHT
    )
    score += 1.0 if abs(weight_sum - 1.0) < 0.01 else max(0.0, 1.0 - abs(weight_sum - 1.0))
    checks += 1

    # Length sanity: min < max, min >= 300
    if target.MIN_BRIEF_LENGTH_CHARS < target.MAX_BRIEF_LENGTH_CHARS:
        score += 1.0
    checks += 1

    if target.MIN_BRIEF_LENGTH_CHARS >= 300:
        score += 1.0
    checks += 1

    # Prefer shorter max (signal density incentive): score higher if max <= 3000
    score += 1.0 if target.MAX_BRIEF_LENGTH_CHARS <= 3000 else 0.5
    checks += 1

    # Required sections never empty
    score += 1.0 if len(target.REQUIRED_SECTIONS) >= 4 else 0.0
    checks += 1

    return score / checks if checks > 0 else 0.0

def main():
    sec_score = score_sections(TEMPLATE)
    struct = structure_score()
    final = sec_score * 0.7 + struct * 0.3
    print(f"{final:.6f}")

if __name__ == "__main__":
    main()
