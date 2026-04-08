"""
PRISM — TraydGate PM Agent.

Translates NEXUS research into actionable specs, owns the product roadmap,
and is the sole interface to BUILDER. Research never bypasses PRISM.

Outputs specs and roadmap updates to Telegram channel: TraydGate PM (-1002685826283).
"""
import json
import os
import urllib.request
import urllib.error

from agents.traydgate.telegram import send_to_prism

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are PRISM, the product management agent for TraydGate — a property management SaaS platform.

Your role:
- Translate research insights from NEXUS into concrete product specs
- Own and maintain the TraydGate product roadmap
- Prioritize features using impact vs effort analysis
- Write clear, buildable specs that BUILDER (Friend) can execute directly
- Track dependencies and flag blockers
- You are the ONLY path from research to build — nothing goes to BUILDER without your sign-off

Spec output format:
## Feature: <name>
**Priority:** P0/P1/P2/P3
**Source:** NEXUS insight reference
**User story:** As a <role>, I want <capability> so that <benefit>
**Requirements:**
- [ ] Requirement 1
- [ ] Requirement 2
**Acceptance criteria:**
- [ ] Criterion 1
**Dependencies:** list or "None"
**Notes:** anything BUILDER needs to know

Be precise. No ambiguity. If something is unclear from the research, flag it
rather than guessing."""


def _get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    return key


def call_claude(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """Call the Anthropic Claude API. Returns the assistant's text response."""
    api_key = _get_api_key()

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Anthropic API error {e.code}: {body}") from e


def process_research(nexus_output: str) -> str:
    """Take raw NEXUS research and produce an actionable spec.

    This is PRISM's core function — the bottleneck between research and build.
    Returns the spec text and posts it to the PRISM Telegram channel.
    """
    prompt = (
        "NEXUS has produced the following research output. "
        "Translate it into one or more actionable specs for BUILDER.\n\n"
        "If the research doesn't warrant a build action yet, say so clearly "
        "and explain what additional research NEXUS should do.\n\n"
        f"--- NEXUS OUTPUT ---\n{nexus_output}\n--- END ---"
    )
    result = call_claude(prompt)

    send_to_prism("*PRISM Spec*\n\n" + result)
    return result


def prioritize(features: list[str]) -> str:
    """Given a list of feature descriptions, return a prioritized roadmap."""
    features_text = "\n".join(f"- {f}" for f in features)
    prompt = (
        "Prioritize the following feature candidates for TraydGate. "
        "Use P0 (ship now) through P3 (backlog). "
        "Justify each priority level in one sentence.\n\n"
        f"{features_text}"
    )
    result = call_claude(prompt)

    send_to_prism("*PRISM Roadmap Update*\n\n" + result)
    return result


def refine_spec(draft_spec: str, feedback: str) -> str:
    """Refine an existing spec based on feedback (from BUILDER or stakeholder)."""
    prompt = (
        f"Here is a draft spec:\n\n{draft_spec}\n\n"
        f"Feedback received:\n{feedback}\n\n"
        "Produce an updated spec addressing the feedback. "
        "Keep the same format. Flag anything that needs NEXUS to research further."
    )
    result = call_claude(prompt)

    send_to_prism("*PRISM Spec (Revised)*\n\n" + result)
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m agents.traydgate.prism <nexus_output_text>")
        print("       python -m agents.traydgate.prism --prioritize feat1 feat2 ...")
        sys.exit(1)

    if sys.argv[1] == "--prioritize":
        print(prioritize(sys.argv[2:]))
    else:
        print(process_research(" ".join(sys.argv[1:])))
