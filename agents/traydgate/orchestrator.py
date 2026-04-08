"""
TraydGate Orchestrator — runs the NEXUS → PRISM → BUILDER pipeline.

Enforces the flow: research never reaches BUILDER without passing through PRISM.

Usage:
    python -m agents.traydgate.orchestrator --topic "tenant screening automation"
    python -m agents.traydgate.orchestrator --scan
    python -m agents.traydgate.orchestrator --competitor AppFolio
"""
import argparse
import datetime
import json
import os
import sys

from agents.traydgate.nexus import research, market_scan, competitor_analysis
from agents.traydgate.prism import process_research
from agents.traydgate.telegram import send_to_prism

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(REPO_ROOT, "agents", "traydgate", "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


def _log(entry: dict):
    date_str = datetime.date.today().isoformat()
    log_path = os.path.join(LOGS_DIR, f"orchestrator_{date_str}.jsonl")
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_pipeline(nexus_fn, nexus_label: str, **nexus_kwargs) -> dict:
    """Execute the full NEXUS → PRISM pipeline.

    1. NEXUS produces research (and posts to its Telegram channel)
    2. PRISM processes the research into specs (and posts to its channel)
    3. Orchestrator logs the run

    BUILDER (Friend) reads specs from the PRISM channel only.
    Research never bypasses PRISM.
    """
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    print(f"[{timestamp}] Starting pipeline: {nexus_label}")

    # Step 1: NEXUS researches
    print("  [NEXUS] Researching...")
    nexus_output = nexus_fn(**nexus_kwargs)
    print(f"  [NEXUS] Done. Output length: {len(nexus_output)} chars")

    # Step 2: PRISM processes — the mandatory bottleneck
    print("  [PRISM] Processing research into specs...")
    prism_output = process_research(nexus_output)
    print(f"  [PRISM] Done. Spec length: {len(prism_output)} chars")

    # Step 3: Log the run
    entry = {
        "timestamp": timestamp,
        "pipeline": nexus_label,
        "nexus_output_len": len(nexus_output),
        "prism_output_len": len(prism_output),
        "status": "complete",
    }
    _log(entry)

    print(f"  [ORCHESTRATOR] Pipeline complete. Spec posted to PRISM channel.")
    print(f"  [ORCHESTRATOR] BUILDER picks up from PRISM channel only.\n")

    return {
        "nexus_output": nexus_output,
        "prism_spec": prism_output,
    }


def topic_pipeline(topic: str) -> dict:
    """Research a specific topic end-to-end."""
    return run_pipeline(research, f"topic: {topic}", topic=topic)


def scan_pipeline() -> dict:
    """Run a full market scan end-to-end."""
    return run_pipeline(market_scan, "market_scan")


def competitor_pipeline(competitor: str) -> dict:
    """Run a competitor analysis end-to-end."""
    return run_pipeline(competitor_analysis, f"competitor: {competitor}", competitor=competitor)


def main():
    parser = argparse.ArgumentParser(description="TraydGate multi-agent orchestrator")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--topic", type=str, help="Research a specific topic")
    group.add_argument("--scan", action="store_true", help="Run a broad market scan")
    group.add_argument("--competitor", type=str, help="Deep-dive on a competitor")
    args = parser.parse_args()

    if args.topic:
        result = topic_pipeline(args.topic)
    elif args.scan:
        result = scan_pipeline()
    elif args.competitor:
        result = competitor_pipeline(args.competitor)

    print("=== PRISM SPEC (for BUILDER) ===")
    print(result["prism_spec"])


if __name__ == "__main__":
    main()
