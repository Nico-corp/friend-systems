# Autoresearch — Karpathy Loop for Workspace Domains

The Karpathy Loop is a generic autonomous improvement framework: an agent proposes a small change to a config file, runs an evaluator to measure the score, keeps the change if score improves, and reverts otherwise — repeating N times. Anything with a score can be auto-improved overnight across 100+ experiments while you sleep.

## How to run

```bash
# Options strategy tuning (⚠️ NOT during market hours — see warning below)
python3 autoresearch/loop.py --domain options --max-experiments 20

# SDS scoring weight tuning
python3 autoresearch/loop.py --domain sds --max-experiments 20

# Daily brief structure tuning
python3 autoresearch/loop.py --domain brief --max-experiments 20
```

Each experiment is logged to `autoresearch/logs/<domain>_<date>.jsonl`. Improvements are auto-committed to the current branch.

## Directory structure

```
autoresearch/
├── loop.py                  # Generic runner
├── README.md                # This file
├── logs/                    # Per-domain JSONL experiment logs
└── domains/
    ├── options/
    │   ├── program.md       # Research direction (what to optimize)
    │   ├── eval.py          # Locked scorer (do not edit)
    │   └── target.py        # Editable config (auto-tuned)
    ├── sds/
    │   ├── program.md
    │   ├── eval.py
    │   └── target.py
    └── brief/
        ├── program.md
        ├── eval.py
        └── target.py
```

## How to add a new domain

1. Create `autoresearch/domains/<your_domain>/`
2. Add three files:
   - `program.md` — plain-English description of what to optimize and constraints
   - `target.py` — Python constants only (no logic), these are what gets tuned
   - `eval.py` — prints a single float score to stdout; exit 0 on success
3. Run: `python3 autoresearch/loop.py --domain <your_domain> --max-experiments 20`

**eval.py contract:**
- Must print exactly one float to stdout (e.g., `0.734521`)
- Must exit 0 on success
- Should print `0.0` and exit cleanly when insufficient data is available
- Timeout: 60 seconds per eval

## ⚠️ Warning: options domain

**Never run `--domain options` during market hours (9:30 AM–4:00 PM ET, Monday–Friday).** The options evaluator reads from the live paper trades database. Running experiments during market hours risks polluting strategy parameters while active positions are open.

Schedule overnight runs only:
```bash
# Example: run at 6 AM ET before market open
# Add to cron: 0 6 * * 1-5 cd /path/to/workspace && python3 autoresearch/loop.py --domain options
```
