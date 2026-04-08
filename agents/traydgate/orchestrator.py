"""
TraydGate Orchestrator — spawns NEXUS and PRISM as subagents via the Claude Agent SDK.

Enforces the flow: research never reaches BUILDER without passing through PRISM.
NEXUS output → Telegram Research channel → PRISM processing → Telegram PM channel → BUILDER

Usage:
    python -m agents.traydgate.orchestrator --topic "tenant screening automation"
    python -m agents.traydgate.orchestrator --scan
    python -m agents.traydgate.orchestrator --competitor AppFolio
"""
import argparse

import anyio
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from agents.traydgate.nexus import NEXUS_AGENT
from agents.traydgate.prism import PRISM_AGENT
from agents.traydgate.telegram import telegram_mcp_server

SYSTEM_PROMPT = """\
You are the TraydGate orchestrator. You manage a strict two-agent pipeline for \
building a property management SaaS platform.

## Agents
- **nexus**: Research agent. Spawns via the Agent tool. Returns deep research on \
property management SaaS topics.
- **prism**: PM agent. Spawns via the Agent tool. Translates research into \
buildable specs for BUILDER (Friend).

## Pipeline (execute in this exact order)
1. Spawn the **nexus** agent with the research topic. Wait for its full output.
2. Post the NEXUS output to Telegram using the **send_to_nexus_channel** tool.
3. Spawn the **prism** agent, passing it the complete NEXUS output. Ask it to \
produce actionable specs.
4. Post the PRISM spec output to Telegram using the **send_to_prism_channel** tool.
5. Summarize what was produced.

## Hard rules
- Research NEVER goes directly to BUILDER. PRISM is the mandatory bottleneck.
- Always post to both Telegram channels — that's the delivery mechanism.
- Include full context when spawning PRISM. Don't summarize the NEXUS output; \
pass it verbatim so PRISM has everything."""


async def run_pipeline(prompt: str):
    """Run the NEXUS → PRISM pipeline for a given prompt."""
    options = ClaudeAgentOptions(
        allowed_tools=["Agent"],
        agents={
            "nexus": NEXUS_AGENT,
            "prism": PRISM_AGENT,
        },
        mcp_servers={"telegram": telegram_mcp_server},
        system_prompt=SYSTEM_PROMPT,
        max_turns=20,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
            elif isinstance(message, ResultMessage):
                if message.result:
                    print(f"\n=== Pipeline complete ===\n{message.result}")


def main():
    parser = argparse.ArgumentParser(description="TraydGate multi-agent orchestrator")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--topic", type=str, help="Research a specific topic")
    group.add_argument("--scan", action="store_true", help="Run a broad market scan")
    group.add_argument("--competitor", type=str, help="Deep-dive on a competitor")
    args = parser.parse_args()

    if args.topic:
        prompt = (
            f"Research the following topic in the property management SaaS space, "
            f"then produce specs:\n\n{args.topic}"
        )
    elif args.scan:
        prompt = (
            "Run a broad market scan of the property management SaaS landscape. "
            "Cover: top players, emerging startups, feature trends, pricing shifts, "
            "and gaps. Then produce specs for the most promising opportunities."
        )
    else:
        prompt = (
            f"Deep analysis of {args.competitor} as a property management SaaS platform. "
            f"Cover: core features, pricing, target market, strengths, weaknesses, "
            f"and what TraydGate could learn or exploit. Then produce specs."
        )

    anyio.run(run_pipeline, prompt)


if __name__ == "__main__":
    main()
