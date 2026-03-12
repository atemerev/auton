#!/usr/bin/env python3
"""Demo: Multi-level research — coordinator discovers VC funds, spawns sub-agents.

Usage:
    # Start the server first:
    uv run uvicorn auton.api:app --port 8000

    # Then run this script:
    uv run python examples/parallel_research.py

The coordinator:
1. Researches VC funds in Geneva investing in AI
2. Spawns one sub-agent per fund discovered
3. Each fund sub-agent may spawn further children for key people
4. Coordinator collects all dossiers into a combined report.md

Total budget: 700K tokens, carved up across the hierarchy.
"""

import sys
import time

import httpx

BASE = "http://localhost:8000"

COORDINATOR_PROMPT = """\
You are a research coordinator. You do NOT research yourself — you
delegate ALL research to sub-agents and combine their results.

Your workflow:

1. Call check_budget to see your total token budget. You must share it
   with your sub-agents. Plan the allocation:
   - Keep ~100K tokens for yourself (spawning, polling, report writing)
   - Divide the rest equally among fund sub-agents (~80K each)

2. Spawn one sub-agent per fund using spawn_child. Each fund's child_id
   should be a slug (e.g. "forestay-capital", "btov-partners").
   Give each child ~80K tokens.
   Check your budget after each spawn — child budgets are reserved
   from yours.

3. After spawning ALL children, poll with check_child_status until
   every child is "idle" or "suspended". Be patient — they take time.
   Poll each child every 30 seconds or so.

4. Once all children are done, use read_child_file to get each
   child's dossier.md.

5. Write a combined report.md in YOUR workspace with:
   - Executive summary of the Geneva AI VC landscape
   - Per-fund sections with key findings
   - A table: fund name, AUM, AI focus, notable portfolio companies

6. Call finish when done.
"""

FUND_RESEARCHER_PROMPT = """\
You are a thorough research agent investigating a specific VC fund.

Your job:
1. Research the fund's background, founding, AUM, and investment thesis.
2. Identify their AI/ML portfolio companies and notable investments.
3. Find key partners and decision-makers at the fund.
4. If you find particularly important people (managing partners, AI-focused
   partners), you MAY spawn child agents to research them in depth — but
   only if budget allows. Allocate no more than 15K tokens per person agent.
   Use check_budget before spawning children.
5. Write all findings to dossier.md in your workspace.
6. Call finish when done.

Focus on verifiable facts. Deduplicate findings. Be thorough but efficient
with your token budget.
"""

FUNDS = [
    ("Forestay Capital", "Geneva-based, deep tech and AI, founded by Fred Modinger"),
    ("Wingman Ventures", "Swiss early-stage VC, strong AI/ML portfolio"),
    ("Polytech Ventures", "EPFL-connected, deep tech focus"),
    ("VI Partners", "Swiss VC, enterprise tech and AI"),
    ("Investiere / Verve Ventures", "Swiss startup investment platform"),
    ("ACE & Company", "Geneva-based multi-family office with tech investments"),
]

GOAL_TEMPLATE = (
    "Research these Geneva-area VC funds that invest in AI. "
    "Spawn one sub-agent per fund, wait for all to finish, "
    "then combine their dossiers into report.md.\n\n"
    "Funds to research:\n{fund_list}"
)


def _count_tokens_recursive(agent_data: dict) -> int:
    """Sum tokens across an agent and all its descendants."""
    total = agent_data["health"]["tokens_total"]
    for child in agent_data.get("children", []):
        total += _count_tokens_recursive(child)
    return total


def _print_tree(agent: dict, indent: int = 0) -> None:
    """Print agent tree with indentation."""
    prefix = "  " * indent
    tokens = agent["health"]["tokens_total"]
    children = agent.get("children", [])
    child_count = len(children)
    line = f"{prefix}{agent['id']}: state={agent['state']}, tokens={tokens:,}"
    if child_count:
        line += f", children={child_count}"
    print(line)
    for child in children:
        _print_tree(child, indent + 1)


def main():
    fund_list = "\n".join(f"- {name}: {desc}" for name, desc in FUNDS)
    goal = GOAL_TEMPLATE.format(fund_list=fund_list)

    spec = {
        "id": "coordinator",
        "spec": {
            "name": "Geneva AI VC Research Coordinator",
            "description": "Coordinates multi-level research into Geneva-based AI venture capital",
            "system_prompt": COORDINATOR_PROMPT + "\n\n"
                "Use this prompt for fund research sub-agents:\n"
                f"---\n{FUND_RESEARCHER_PROMPT}---\n",
            "goal": goal,
            "model": "openrouter/anthropic/claude-sonnet-4-6",
            "tools": [
                "spawn_child", "check_child_status", "list_children",
                "read_child_file", "message_agent",
                "write_file", "read_file", "list_files",
                "check_budget", "finish",
            ],
            "max_tokens": 700_000,
            "max_turns": 60,
            "max_runtime_seconds": 1800,
            "max_children": 15,
            "max_depth": 3,
        },
    }

    # Spawn coordinator
    print(f"Spawning coordinator: research {len(FUNDS)} Geneva AI VC funds")
    print(f"  Budget: 700K tokens, max depth: 3 levels\n")
    resp = httpx.post(f"{BASE}/agents", json=spec, timeout=10)
    if resp.status_code == 409:
        print("Coordinator already exists. Delete it first or restart the server.")
        sys.exit(1)
    resp.raise_for_status()
    data = resp.json()
    print(f"  Coordinator: {data['id']} (state={data['state']})")

    # Monitor loop
    print("\nMonitoring progress...\n")
    while True:
        time.sleep(15)
        try:
            r = httpx.get(f"{BASE}/agents/coordinator", timeout=10)
            r.raise_for_status()
            agent = r.json()
        except Exception as e:
            print(f"  Error polling: {e}")
            continue

        state = agent["state"]
        total_tokens = _count_tokens_recursive(agent)

        print(f"--- [{time.strftime('%H:%M:%S')}] Total tokens: {total_tokens:,} ---")
        _print_tree(agent)
        print()

        if state in ("idle", "dead", "suspended"):
            print(f"Coordinator finished (state={state})")
            break

    # Check results
    print("\n=== Results ===")
    try:
        ws = httpx.get(f"{BASE}/agents/coordinator/workspace", timeout=10)
        ws.raise_for_status()
        files = ws.json()
        print(f"Coordinator workspace: {files['count']} file(s)")
        for f in files["files"]:
            print(f"  {f['path']} ({f['size']} bytes)")
    except Exception as e:
        print(f"Could not read workspace: {e}")

    # Print total tokens across entire tree
    try:
        tree = httpx.get(f"{BASE}/agents/coordinator", timeout=10).json()
        total = _count_tokens_recursive(tree)
        print(f"\nTotal tokens (full tree): {total:,}")
        print(f"Budget: 700,000 tokens")
        print(f"Usage: {total / 700_000 * 100:.1f}%")
    except Exception:
        pass


if __name__ == "__main__":
    main()
