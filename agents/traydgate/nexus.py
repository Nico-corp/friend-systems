"""
NEXUS — TraydGate Research Agent.

Property management SaaS domain expert. Researches market landscape,
competitor features, pricing models, technology stacks, and user pain points.

Outputs insights to Telegram channel: TraydGate Research (-1002691671696).
NEXUS never sends work directly to BUILDER — everything flows through PRISM.
"""
import json
import os
import urllib.request
import urllib.error

from agents.traydgate.telegram import send_to_nexus

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are NEXUS, the research agent for TraydGate — a property management SaaS platform.

Your role:
- Deep research on property management software landscape
- Analyze competitor features, pricing, and positioning (Buildium, AppFolio, Rent Manager, etc.)
- Identify market gaps and underserved segments
- Study landlord/tenant pain points and workflow inefficiencies
- Track regulatory changes affecting property management
- Evaluate technology trends (AI, automation, integrations) relevant to proptech

Output format:
- Lead with the key insight in one sentence
- Support with evidence and specifics
- End with "NEXUS Signal:" — a one-line takeaway for PRISM to act on

Stay factual. Cite specifics when possible. No fluff."""


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
        "max_tokens": 2048,
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


def research(topic: str) -> str:
    """Run a research query and post the result to the NEXUS Telegram channel.

    Returns the research output text.
    """
    prompt = f"Research the following topic in the property management SaaS space:\n\n{topic}"
    result = call_claude(prompt)

    header = f"*NEXUS Research — {topic[:80]}*\n\n"
    send_to_nexus(header + result)

    return result


def market_scan() -> str:
    """Run a broad market scan of the property management SaaS landscape."""
    prompt = (
        "Provide a current market scan of the property management SaaS landscape. "
        "Cover: top players and recent moves, emerging startups, feature trends, "
        "pricing shifts, and any gaps you identify."
    )
    result = call_claude(prompt)

    send_to_nexus("*NEXUS Market Scan*\n\n" + result)
    return result


def competitor_analysis(competitor: str) -> str:
    """Deep dive on a specific competitor."""
    prompt = (
        f"Deep analysis of {competitor} as a property management SaaS platform. "
        f"Cover: core features, pricing tiers, target market, tech stack (if known), "
        f"strengths, weaknesses, and what TraydGate could learn or exploit."
    )
    result = call_claude(prompt)

    send_to_nexus(f"*NEXUS Competitor Analysis — {competitor}*\n\n" + result)
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m agents.traydgate.nexus <topic>")
        print("       python -m agents.traydgate.nexus --scan")
        print("       python -m agents.traydgate.nexus --competitor <name>")
        sys.exit(1)

    if sys.argv[1] == "--scan":
        print(market_scan())
    elif sys.argv[1] == "--competitor" and len(sys.argv) >= 3:
        print(competitor_analysis(sys.argv[2]))
    else:
        print(research(" ".join(sys.argv[1:])))
