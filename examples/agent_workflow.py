#!/usr/bin/env python3
"""
Fla Agent Workflow Example

Demonstrates a full agent workflow using the Fla Python SDK:
  1. Initialize a repository
  2. Agent makes changes on main lane
  3. Agent creates a feature lane and works in isolation
  4. Promote feature work back to main
  5. Query history and show diffs

Usage:
    python examples/agent_workflow.py          # Run with temp directory (cleaned up)
    python examples/agent_workflow.py --keep    # Keep repo for inspection
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure the fla package is importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fla.agent_sdk import AgentSession
from fla.repo import Repository


def step(n: int, msg: str):
    print(f"\n{'='*60}")
    print(f"  Step {n}: {msg}")
    print(f"{'='*60}\n")


def run_demo(repo_path: Path):
    # ── Step 1: Initialize ──────────────────────────────────────
    step(1, "Initialize a Fla repository")

    # Create some starter files
    (repo_path / "app.py").write_text(
        'def main():\n    print("Hello from app")\n\nif __name__ == "__main__":\n    main()\n'
    )
    (repo_path / "README.md").write_text("# My Project\n\nA demo project for Fla.\n")

    with Repository.init(repo_path) as repo:
        head = repo.head()
        print(f"  Repository initialized at: {repo_path}")
        print(f"  Initial snapshot: {head[:12] if head else 'empty'}")
        print("  Files captured: app.py, README.md")

    # ── Step 2: Agent commits on main ───────────────────────────
    step(2, "Agent makes changes on the main lane")

    session = AgentSession(
        repo_path=repo_path,
        agent_id="coder-alpha",
        agent_type="feature_developer",
        model="gpt-4",
    )

    with session.work("Add utility module", tags=["feature"], auto_accept=True) as w:
        # w.path points to the main workspace (repo root for main lane)
        (w.path / "utils.py").write_text(
            'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
        )
        # Update app.py to use the utility
        (w.path / "app.py").write_text(
            'from utils import greet\n\ndef main():\n    print(greet("World"))\n\n'
            'if __name__ == "__main__":\n    main()\n'
        )
        w.record_tokens(tokens_in=1500, tokens_out=800)
        print("  Agent wrote: utils.py, updated app.py")
        print(f"  Workspace: {w.path}")

    print("  Transition auto-accepted on main lane")

    # ── Step 3: Agent works on a feature lane ───────────────────
    step(3, "Create feature lane and work in isolation")

    session2 = AgentSession(
        repo_path=repo_path,
        agent_id="auth-agent",
        agent_type="security_developer",
        model="claude-sonnet",
    )

    # Create a feature lane — gets its own workspace directory
    session2.begin()
    session2.create_lane("feature-auth")
    print("  Created lane: feature-auth")
    print(f"  Isolated workspace: {session2.workspace_path}")

    # Write auth module in the isolated workspace
    auth_dir = session2.workspace_path / "auth"
    auth_dir.mkdir(exist_ok=True)
    (auth_dir / "__init__.py").write_text("")
    (auth_dir / "login.py").write_text(
        'def authenticate(username: str, password: str) -> bool:\n'
        '    """Validate credentials."""\n'
        '    # Placeholder — real implementation would check a database\n'
        '    return username == "admin" and password == "secret"\n'
    )
    session2.record_tokens(tokens_in=2000, tokens_out=1200)
    print("  Agent wrote: auth/__init__.py, auth/login.py")

    result = session2.checkpoint(
        prompt="Add authentication module",
        tags=["auth", "security"],
        auto_accept=True,
    )
    print(f"  Transition: {result['status']}")
    session2.end()
    session2.close()

    # Show that main is unaffected
    print("\n  Main workspace still has original files:")
    for f in sorted(repo_path.iterdir()):
        if f.name not in (".fla", ".flaignore"):
            print(f"    {f.name}")

    # ── Step 4: Promote feature work to main ────────────────────
    step(4, "Promote feature-auth into main")

    with Repository.find(repo_path) as repo:
        from fla.state import AgentIdentity

        promote_result = repo.promote(
            workspace="feature-auth",
            target_lane="main",
            prompt="Merge authentication feature",
            agent=AgentIdentity(agent_id="orchestrator", agent_type="promote"),
            auto_accept=True,
        )
        if promote_result.get("conflicts"):
            print(f"  Conflicts detected: {promote_result['conflicts']}")
        else:
            print("  Promoted successfully!")
            print(f"  Status: {promote_result.get('status', 'unknown')}")

    # ── Step 5: Query history ───────────────────────────────────
    step(5, "Query transition history")

    with Repository.find(repo_path) as repo:
        transitions = repo.history(lane="main", limit=10)
        print(f"  Found {len(transitions)} transitions on main:\n")
        for t in transitions:
            agent = t.get("agent", {})
            status_icon = {"accepted": "+", "proposed": "?", "rejected": "-"}.get(
                t["status"], "o"
            )
            print(f"    [{status_icon}] {t.get('intent_prompt', 'N/A')}")
            print(f"        Agent: {agent.get('agent_id', '?')} ({agent.get('agent_type', '?')})")
            print(f"        State: {t['to_state'][:12]}")
            print()

    print("Demo complete!")


def main():
    parser = argparse.ArgumentParser(description="Fla Agent Workflow Example")
    parser.add_argument("--keep", action="store_true", help="Keep the repo after the demo")
    parser.add_argument("--path", type=str, default=None, help="Use a specific directory")
    args = parser.parse_args()

    if args.path:
        repo_path = Path(args.path).resolve()
        repo_path.mkdir(parents=True, exist_ok=True)
        run_demo(repo_path)
        print(f"\nRepository at: {repo_path}")
    elif args.keep:
        repo_path = Path(tempfile.mkdtemp(prefix="fla-demo-"))
        run_demo(repo_path)
        print(f"\nRepository kept at: {repo_path}")
        print("  Inspect with: fla -C {repo_path} status")
    else:
        repo_path = Path(tempfile.mkdtemp(prefix="fla-demo-"))
        try:
            run_demo(repo_path)
        finally:
            shutil.rmtree(repo_path, ignore_errors=True)
            print("\nTemp directory cleaned up.")


if __name__ == "__main__":
    main()
