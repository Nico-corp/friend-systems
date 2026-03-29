#!/usr/bin/env python3
"""
Autoresearch loop — generic Karpathy-style improvement runner.

Usage:
    python3 autoresearch/loop.py --domain options --max-experiments 20

For each experiment:
  1. Ask Claude to propose a change to target.py
  2. Apply the change
  3. Run eval.py → capture score
  4. Keep if score improved, otherwise revert
  5. Log to autoresearch/logs/<domain>_<date>.jsonl
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import textwrap

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTORESEARCH_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(AUTORESEARCH_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


def domain_dir(domain):
    return os.path.join(AUTORESEARCH_DIR, "domains", domain)


def read_file(path):
    with open(path) as f:
        return f.read()


def write_file(path, content):
    with open(path, "w") as f:
        f.write(content)


def run_eval(domain):
    """Run eval.py for the domain. Returns float score or None on failure."""
    eval_path = os.path.join(domain_dir(domain), "eval.py")
    try:
        result = subprocess.run(
            [sys.executable, eval_path],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=WORKSPACE,
        )
        stdout = result.stdout.strip()
        if not stdout:
            print(f"  [eval] no output. stderr: {result.stderr.strip()[:200]}")
            return None
        return float(stdout)
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError) as e:
        print(f"  [eval] error: {e}")
        return None


def git_revert_target(domain):
    target_path = os.path.join("autoresearch", "domains", domain, "target.py")
    subprocess.run(
        ["git", "checkout", "--", target_path],
        cwd=WORKSPACE,
        capture_output=True,
    )


def git_commit(domain, n, score, delta):
    target_path = os.path.join("autoresearch", "domains", domain, "target.py")
    subprocess.run(["git", "add", target_path], cwd=WORKSPACE, capture_output=True)
    msg = f"autoresearch({domain}): experiment {n} score {score:.4f} +{delta:.4f}"
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  [git] commit failed: {result.stderr.strip()[:200]}")


def build_claude_prompt(domain, program_md, current_target, history):
    history_text = ""
    if history:
        recent = history[-5:]  # last 5 experiments for context
        history_text = "\n\n## Recent experiment history\n"
        for h in recent:
            kept_str = "KEPT" if h["kept"] else "REVERTED"
            history_text += (
                f"- Exp {h['n']}: {h['description']} → score {h['score_after']:.4f} [{kept_str}]\n"
            )

    prompt = textwrap.dedent(f"""
        You are an autoresearch agent. Your job is to propose ONE small improvement
        to the target.py configuration file for the `{domain}` domain.

        ## Research direction (program.md)
        {program_md}
        {history_text}

        ## Current target.py
        ```python
        {current_target}
        ```

        ## Instructions
        1. Propose exactly ONE parameter change (or two closely related ones).
        2. Explain your reasoning in 1-2 sentences.
        3. Output the COMPLETE new target.py file content (all lines), with your change applied.

        ## Output format (follow exactly)
        DESCRIPTION: <one sentence description of the change>
        TARGET_PY:
        ```python
        <full new target.py content>
        ```
    """).strip()
    return prompt


def parse_claude_output(output):
    """
    Parse Claude's response. Returns (description, new_target_content) or (None, None).
    """
    description = None
    new_target = None

    lines = output.splitlines()

    for i, line in enumerate(lines):
        if line.startswith("DESCRIPTION:"):
            description = line[len("DESCRIPTION:"):].strip()
            break

    # Find TARGET_PY block
    in_block = False
    block_lines = []
    for line in lines:
        if line.strip() == "TARGET_PY:":
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("```python"):
                continue
            if line.strip() == "```":
                if block_lines:
                    break
                continue
            block_lines.append(line)

    if block_lines:
        new_target = "\n".join(block_lines).strip() + "\n"

    return description, new_target


def call_claude(prompt):
    """Call claude CLI with --print flag. Returns stdout string or None."""
    try:
        result = subprocess.run(
            ["claude", "--print", "--permission-mode", "bypassPermissions"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"  [claude] error (rc={result.returncode}): {result.stderr.strip()[:300]}")
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  [claude] failed: {e}")
        return None


def log_experiment(log_path, entry):
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_loop(domain, max_experiments):
    d_dir = domain_dir(domain)
    program_path = os.path.join(d_dir, "program.md")
    target_path = os.path.join(d_dir, "target.py")

    if not os.path.isdir(d_dir):
        print(f"ERROR: domain '{domain}' not found at {d_dir}")
        sys.exit(1)

    program_md = read_file(program_path)
    date_str = datetime.date.today().isoformat()
    log_path = os.path.join(LOGS_DIR, f"{domain}_{date_str}.jsonl")

    print(f"\n=== Autoresearch: domain={domain}, max_experiments={max_experiments} ===")
    print(f"Log: {log_path}\n")

    # Baseline score
    best_score = run_eval(domain)
    if best_score is None:
        best_score = 0.0
    print(f"Baseline score: {best_score:.4f}")

    history = []
    kept_count = 0

    for n in range(1, max_experiments + 1):
        print(f"\n--- Experiment {n}/{max_experiments} ---")

        current_target = read_file(target_path)
        prompt = build_claude_prompt(domain, program_md, current_target, history)

        print("  Calling Claude...")
        claude_output = call_claude(prompt)
        if claude_output is None:
            print("  Skipping (Claude unavailable).")
            continue

        description, new_target = parse_claude_output(claude_output)
        if new_target is None:
            print("  Could not parse Claude output. Skipping.")
            continue

        print(f"  Proposed: {description or '(no description)'}")

        # Apply change
        write_file(target_path, new_target)

        # Evaluate
        new_score = run_eval(domain)
        if new_score is None:
            print("  Eval failed. Reverting.")
            git_revert_target(domain)
            continue

        delta = new_score - best_score
        kept = new_score > best_score

        if kept:
            print(f"  Score: {best_score:.4f} → {new_score:.4f} (+{delta:.4f}) ✓ KEPT")
            git_commit(domain, n, new_score, delta)
            best_score = new_score
            kept_count += 1
        else:
            print(f"  Score: {best_score:.4f} → {new_score:.4f} ({delta:+.4f}) ✗ REVERTED")
            git_revert_target(domain)

        entry = {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "n": n,
            "description": description or "",
            "score_before": round(best_score if not kept else best_score - delta, 6),
            "score_after": round(new_score, 6),
            "kept": kept,
            "change_summary": (new_target[:200] if kept else ""),
        }
        log_experiment(log_path, entry)
        history.append(entry)

    print(f"\n=== Done. Best score: {best_score:.4f} | Kept: {kept_count}/{max_experiments} ===")
    print(f"Log: {log_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Autoresearch loop — Karpathy-style autonomous improvement runner"
    )
    parser.add_argument("--domain", required=True, help="Domain name (options, sds, brief)")
    parser.add_argument("--max-experiments", type=int, default=20, help="Number of experiments (default 20)")
    args = parser.parse_args()

    run_loop(args.domain, args.max_experiments)


if __name__ == "__main__":
    main()
