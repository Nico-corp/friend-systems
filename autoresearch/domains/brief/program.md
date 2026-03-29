# Daily Brief Autoresearch Program

Optimize daily brief structure for maximum signal density and readability. Adjust section weights and length constraints. Never remove required sections. Prefer shorter, denser briefs over long ones.

## Scoring
Score = sections_present/total_required * 0.7 + structure_score * 0.3

## Constraints
- REQUIRED_SECTIONS must always contain ["regime", "positions", "signals", "news"]
- MAX_BRIEF_LENGTH_CHARS must stay >= MIN_BRIEF_LENGTH_CHARS
- MIN_BRIEF_LENGTH_CHARS should not drop below 300
- Section weights must sum to 1.0
