#!/usr/bin/env python3
"""
Smoke test for TraydGate multi-agent system.

Validates environment, dependencies, config, and Telegram connectivity
before running the full pipeline.

Usage:
    python -m agents.traydgate.smoke_test
"""
import json
import os
import sys
import urllib.request
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHANNELS_PATH = os.path.join(REPO_ROOT, "config", "channels.json")

PASS = "  PASS"
FAIL = "  FAIL"
WARN = "  WARN"
failures = []


def check(label: str, ok: bool, msg: str = ""):
    tag = PASS if ok else FAIL
    suffix = f" — {msg}" if msg else ""
    print(f"{tag}  {label}{suffix}")
    if not ok:
        failures.append(label)
    return ok


def section(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")


def check_dependencies():
    section("Dependencies")

    try:
        import claude_agent_sdk  # noqa: F401
        check("claude-agent-sdk installed", True)
    except ImportError:
        check("claude-agent-sdk installed", False, "pip install claude-agent-sdk")

    try:
        import anyio  # noqa: F401
        check("anyio installed", True)
    except ImportError:
        check("anyio installed", False, "pip install anyio")

    # Check that Agent SDK imports work
    try:
        from claude_agent_sdk import (  # noqa: F401
            ClaudeSDKClient,
            ClaudeAgentOptions,
            AgentDefinition,
            tool,
            create_sdk_mcp_server,
        )
        check("Agent SDK classes importable", True)
    except (ImportError, AttributeError) as e:
        check("Agent SDK classes importable", False, str(e))


def check_env_vars():
    section("Environment Variables")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    check(
        "ANTHROPIC_API_KEY set",
        bool(api_key),
        "powers NEXUS and PRISM subagents" if not api_key else f"starts with {api_key[:10]}...",
    )

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    check(
        "TELEGRAM_BOT_TOKEN set",
        bool(bot_token),
        "needed for Telegram posting" if not bot_token else f"starts with {bot_token[:8]}...",
    )


def check_config():
    section("Config")

    check("config/channels.json exists", os.path.isfile(CHANNELS_PATH))

    try:
        with open(CHANNELS_PATH) as f:
            channels = json.load(f)
        tg = channels.get("traydgate", {})
        nexus_id = tg.get("nexus_research")
        prism_id = tg.get("prism_pm")
        check("nexus_research channel ID present", nexus_id == -1002691671696, str(nexus_id))
        check("prism_pm channel ID present", prism_id == -1002685826283, str(prism_id))
    except (json.JSONDecodeError, FileNotFoundError) as e:
        check("channels.json parseable", False, str(e))


def check_agent_definitions():
    section("Agent Definitions")

    try:
        from agents.traydgate.nexus import NEXUS_AGENT
        check("NEXUS_AGENT loads", True, f"tools={NEXUS_AGENT.tools}")
    except Exception as e:
        check("NEXUS_AGENT loads", False, str(e))

    try:
        from agents.traydgate.prism import PRISM_AGENT
        check("PRISM_AGENT loads", True, f"tools={PRISM_AGENT.tools}")
    except Exception as e:
        check("PRISM_AGENT loads", False, str(e))

    try:
        from agents.traydgate.telegram import telegram_mcp_server
        check("telegram MCP server loads", True)
    except Exception as e:
        check("telegram MCP server loads", False, str(e))


def check_telegram():
    section("Telegram Connectivity")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        print(f"{WARN}  Skipping — TELEGRAM_BOT_TOKEN not set")
        return

    # Test bot identity
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ok"):
            bot_name = data["result"].get("username", "unknown")
            check("Bot token valid", True, f"@{bot_name}")
        else:
            check("Bot token valid", False, str(data))
    except Exception as e:
        check("Bot token valid", False, str(e))

    # Test channel access for both channels
    with open(CHANNELS_PATH) as f:
        channels = json.load(f)

    for label, channel_id in [
        ("NEXUS channel reachable", channels["traydgate"]["nexus_research"]),
        ("PRISM channel reachable", channels["traydgate"]["prism_pm"]),
    ]:
        url = f"https://api.telegram.org/bot{bot_token}/getChat"
        payload = json.dumps({"chat_id": channel_id}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("ok"):
                title = data["result"].get("title", "untitled")
                check(label, True, f'"{title}" ({channel_id})')
            else:
                check(label, False, data.get("description", "unknown error"))
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            check(label, False, f"{e.code}: {body[:100]}")
        except Exception as e:
            check(label, False, str(e))


def main():
    print("\nTraydGate Smoke Test")
    print("=" * 50)

    check_dependencies()
    check_env_vars()
    check_config()
    check_agent_definitions()
    check_telegram()

    print(f"\n{'='*50}")
    if failures:
        print(f"  {len(failures)} check(s) failed: {', '.join(failures)}")
        print(f"  Fix the above before running the pipeline.")
        sys.exit(1)
    else:
        print("  All checks passed. Ready to run:")
        print('  python -m agents.traydgate.orchestrator --topic "tenant screening"')
        sys.exit(0)


if __name__ == "__main__":
    main()
