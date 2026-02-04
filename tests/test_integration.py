"""
Vex — Full Integration Test with Workspace Isolation

This simulates a realistic multi-agent workflow where agents
actually work in isolated directories:

1. Initialize a repo with existing code
2. Agent 1 gets a workspace for 'main' lane
3. Agent 2 gets a workspace for 'bugfix' lane
4. Both modify files in their own directories — true isolation
5. Both propose and get evaluated
6. Smart workspace update (incremental sync)
7. Workspace locking prevents double-use
8. Full history, lineage, diff, search
9. Cleanup

Run with: python -m tests.test_integration
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vex.repo import Repository
from vex.state import (
    AgentIdentity,
    CostRecord,
    EvaluationResult,
    TransitionStatus,
)
from vex.agent_sdk import AgentSession


def divider(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def test_full_workflow():
    test_dir = Path(tempfile.mkdtemp(prefix="vex_test_"))
    print(f"Test directory: {test_dir}\n")

    try:
        # ── Phase 1: Initialize with existing files ──────────────
        divider("Phase 1: Repository Initialization")

        # Create some initial project files
        (test_dir / "main.py").write_text(
            'def main():\n    print("Hello, World!")\n\nif __name__ == "__main__":\n    main()\n'
        )
        (test_dir / "config.json").write_text(
            '{"app_name": "MyApp", "version": "1.0.0", "debug": false}\n'
        )
        (test_dir / "lib").mkdir()
        (test_dir / "lib" / "utils.py").write_text(
            'def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n'
        )

        repo = Repository.init(test_dir)
        status = repo.status()
        print(f"Initialized repo at: {status['root']}")
        print(f"Head state: {status['current_head'][:12]}")
        print(f"Workspaces: {len(status['workspaces'])}")

        initial_head = repo.head()
        assert initial_head is not None, "Should have an initial head"

        # Verify files were moved out of repo root into workspace
        assert not (test_dir / "main.py").exists(), "Files should be moved to workspace"
        main_ws_path = repo.workspace_path("main")
        assert main_ws_path is not None, "Main workspace should exist"
        assert (main_ws_path / "main.py").exists(), "Files should be in workspace"
        print(f"✓ Files moved from repo root to workspace: {main_ws_path}")

        # ── Phase 2: Agent 1 works in main workspace ─────────────
        divider("Phase 2: Agent 1 — Feature in Main Workspace")

        agent1 = AgentIdentity(
            agent_id="coder-alpha",
            agent_type="feature_developer",
            model="claude-sonnet-4-20250514",
        )

        # Get the main workspace path and modify files there
        ws_main = repo.workspace_path("main")
        (ws_main / "main.py").write_text(
            'from lib.utils import add\nfrom lib.auth import authenticate\n\n'
            'def main():\n    if authenticate("admin"):\n        result = add(2, 3)\n'
            '        print(f"Result: {result}")\n\nif __name__ == "__main__":\n    main()\n'
        )
        (ws_main / "lib" / "auth.py").write_text(
            'USERS = {"admin": "secret123"}\n\n'
            'def authenticate(username: str) -> bool:\n'
            '    """Simple auth check."""\n'
            '    return username in USERS\n'
        )

        # Snapshot the main workspace and propose
        new_state = repo.snapshot("main", parent_id=initial_head)
        tid1 = repo.propose(
            from_state=initial_head,
            to_state=new_state,
            prompt="Add authentication module and integrate with main entry point",
            agent=agent1,
            tags=["feature", "auth", "security"],
            cost=CostRecord(tokens_in=1500, tokens_out=800, wall_time_ms=3200, api_calls=2),
        )
        print(f"Proposed: {tid1[:12]}")

        status1 = repo.accept(tid1, evaluator="test_suite_v2", summary="All 12 tests pass")
        print(f"Accepted: {status1.value}")
        print(f"New head: {repo.head()[:12]}")

        head_after_auth = repo.head()

        # ── Phase 3: Agent 2 in separate workspace ────────────────
        divider("Phase 3: Agent 2 — Bugfix in Isolated Workspace")

        # Create a new lane + workspace forked from the initial state
        repo.create_lane("bugfix-utils-edge-case", base=initial_head)
        print(f"Created lane + workspace 'bugfix-utils-edge-case'")

        ws_bugfix = repo.workspace_path("bugfix-utils-edge-case")
        assert ws_bugfix is not None, "Bugfix workspace should exist"
        assert ws_bugfix != ws_main, "Workspaces must be different directories"

        # Verify isolation: bugfix workspace has original files
        bugfix_main_content = (ws_bugfix / "main.py").read_text()
        main_main_content = (ws_main / "main.py").read_text()
        assert "authenticate" not in bugfix_main_content, "Bugfix should not see Agent 1's changes"
        assert "authenticate" in main_main_content, "Main should have Agent 1's changes"
        print(f"✓ Workspaces are isolated:")
        print(f"  main workspace:   {ws_main}")
        print(f"  bugfix workspace: {ws_bugfix}")
        print(f"  main has auth:    True")
        print(f"  bugfix has auth:  False")

        agent2 = AgentIdentity(
            agent_id="debugger-beta",
            agent_type="bugfix_agent",
            model="claude-sonnet-4-20250514",
        )

        # Agent 2 modifies files in the bugfix workspace only
        (ws_bugfix / "lib" / "utils.py").write_text(
            'def add(a, b):\n    """Add two numbers with type checking."""\n'
            '    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n'
            '        raise TypeError("Arguments must be numeric")\n'
            '    return a + b\n\n'
            'def multiply(a, b):\n    """Multiply two numbers with type checking."""\n'
            '    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n'
            '        raise TypeError("Arguments must be numeric")\n'
            '    return a * b\n'
        )

        bugfix_state = repo.snapshot("bugfix-utils-edge-case", parent_id=initial_head)
        tid2 = repo.propose(
            from_state=initial_head,
            to_state=bugfix_state,
            prompt="Add input validation to utils.add() and utils.multiply()",
            agent=agent2,
            lane="bugfix-utils-edge-case",
            tags=["bugfix", "utils", "validation"],
            cost=CostRecord(tokens_in=900, tokens_out=450),
        )
        repo.accept(tid2, evaluator="test_suite_v2", summary="Edge case tests pass")
        print(f"✓ Bugfix proposed and accepted")

        # Verify main workspace was NOT affected by bugfix work
        main_utils = (ws_main / "lib" / "utils.py").read_text()
        assert "TypeError" not in main_utils, "Main workspace must be unaffected by bugfix"
        print(f"✓ Main workspace unaffected by bugfix changes")

        # ── Phase 4: Quick commit on main ─────────────────────────
        divider("Phase 4: Quick Commit on Main Workspace")

        (ws_main / "config.json").write_text(json.dumps({
            "app_name": "MyApp",
            "version": "1.1.0",
            "debug": False,
            "auth": {"enabled": True, "session_timeout": 3600},
        }, indent=2) + "\n")

        result = repo.quick_commit(
            workspace="main",
            prompt="Update config to enable auth module and set session timeout",
            agent=agent1,
            tags=["config", "auth"],
            cost=CostRecord(tokens_in=400, tokens_out=200),
            auto_accept=True,
            evaluator="config_validator",
        )
        print(f"Quick commit: {result['transition_id'][:12]} [{result['status']}]")

        # ── Phase 5: Smart workspace update ───────────────────────
        divider("Phase 5: Smart Incremental Workspace Update")

        # Update bugfix workspace to latest main — should be incremental
        latest_main = repo.head("main")
        update_result = repo.workspace_update("bugfix-utils-edge-case", latest_main)
        print(f"Update mode: {update_result['mode']}")
        if update_result["mode"] == "incremental":
            print(f"  Added:     {update_result['added']}")
            print(f"  Modified:  {update_result['modified']}")
            print(f"  Removed:   {update_result['removed']}")
            print(f"  Unchanged: {update_result['unchanged']}")

        # Verify bugfix workspace now has auth.py from main
        assert (ws_bugfix / "lib" / "auth.py").exists(), "Bugfix should now have auth.py"
        print(f"✓ Bugfix workspace updated incrementally")

        # ── Phase 6: Workspace locking ────────────────────────────
        divider("Phase 6: Workspace Locking")

        # Acquire lock
        locked = repo.workspace_acquire("main", "coder-alpha")
        assert locked, "Should acquire lock"
        print(f"✓ Lock acquired by coder-alpha")

        # Try to acquire again — should fail
        locked2 = repo.workspace_acquire("main", "coder-beta")
        assert not locked2, "Should not acquire — already locked"
        print(f"✓ Second lock correctly rejected")

        # Release and re-acquire
        repo.workspace_release("main")
        locked3 = repo.workspace_acquire("main", "coder-beta")
        assert locked3, "Should acquire after release"
        print(f"✓ Lock released and re-acquired by coder-beta")
        repo.workspace_release("main")

        # ── Phase 7: Agent SDK with workspaces ────────────────────
        divider("Phase 7: Agent SDK — Workspace Context Manager")

        session = AgentSession(
            repo_path=test_dir,
            agent_id="refactorer-gamma",
            agent_type="refactorer",
            model="claude-sonnet-4-20250514",
        )

        with session.work("Refactor auth to use hashed passwords", tags=["refactor", "security"], auto_accept=True) as w:
            w.record_tokens(tokens_in=2000, tokens_out=1200)
            w.add_metadata("files_touched", ["lib/auth.py"])

            # w.path is the isolated workspace directory
            assert w.path.exists(), "Workspace path should exist"
            print(f"  Working in: {w.path}")

            (w.path / "lib" / "auth.py").write_text(
                'import hashlib\n\n'
                'USERS = {"admin": hashlib.sha256(b"secret123").hexdigest()}\n\n'
                'def authenticate(username: str, password: str = "") -> bool:\n'
                '    """Auth check with hashed passwords."""\n'
                '    expected = USERS.get(username)\n'
                '    if expected is None:\n'
                '        return False\n'
                '    return hashlib.sha256(password.encode()).hexdigest() == expected\n'
            )

        print(f"Work result: {w.result['status']}")
        print(f"Transition: {w.result['transition_id'][:12]}")

        # ── Phase 8: History & Lineage ────────────────────────────
        divider("Phase 8: History & Lineage")

        print("── Main lane history ──")
        history = repo.history(lane="main", limit=10)
        for entry in history:
            icon = "✓" if entry["status"] == "accepted" else "◉"
            print(f"  {icon} {entry['id'][:12]}  [{entry['status']}]")
            print(f"    {entry['intent_prompt'][:80]}")
            print()

        print("── Lineage trace ──")
        lineage = repo.trace(repo.head())
        for step in lineage:
            print(f"  {step['to_state'][:12]} ← {(step['from_state'] or 'root')[:12]}")
            print(f"    {step['agent']['agent_id']}: {step['intent_prompt'][:70]}")

        # ── Phase 9: Diff ─────────────────────────────────────────
        divider("Phase 9: Diff")

        diff = repo.diff(initial_head, repo.head())
        print(f"Diff: initial → current\n")
        for path in sorted(diff.get("added", {})):
            print(f"  + {path}")
        for path in sorted(diff.get("modified", {})):
            print(f"  ~ {path}")
        for path in sorted(diff.get("removed", {})):
            print(f"  - {path}")
        total = len(diff["added"]) + len(diff["modified"]) + len(diff["removed"])
        print(f"\n  {total} changed, {diff['unchanged_count']} unchanged")

        # ── Phase 10: Search ──────────────────────────────────────
        divider("Phase 10: Intent Search")

        for q in ["auth", "validation"]:
            print(f"Search: '{q}'")
            for r in repo.search(q):
                print(f"  {r['intent_id'][:12]} [{r.get('status', '?')}] {r['prompt'][:60]}")
            print()

        # ── Phase 11: Workspace listing ───────────────────────────
        divider("Phase 11: Workspace Overview")

        for ws in repo.workspaces():
            print(f"  {ws.name}")
            print(f"    Lane:  {ws.lane}")
            print(f"    Path:  {ws.path}")
            print(f"    Base:  {ws.base_state[:12] if ws.base_state else 'none'}")
            print(f"    Status: {ws.status}")
            print()

        # ── Phase 12: Promote — Clean (non-conflicting) ──────────────
        divider("Phase 12: Promote — Clean (no conflicts)")

        # The bugfix lane modified utils.py. Main lane modified auth.py,
        # config.json, main.py. No overlap → should promote cleanly.
        #
        # But first we need a fresh lane that has changes main doesn't.
        # Create a new lane from current main, add a new file, commit there.
        main_head_before_promote = repo.head("main")
        repo.create_lane("feature-logging", main_head_before_promote)

        # Add a logging module in the feature workspace
        feature_ws_path = repo.workspace_path("feature-logging")
        (feature_ws_path / "lib").mkdir(exist_ok=True)
        (feature_ws_path / "lib" / "logger.py").write_text(
            'import time\n\ndef log(msg: str):\n    print(f"[{time.time():.0f}] {msg}")\n'
        )

        # Commit the feature work into its own lane
        repo.quick_commit(
            workspace="feature-logging",
            prompt="Add logging module",
            agent=AgentIdentity(agent_id="coder-delta", agent_type="feature-agent"),
            auto_accept=True,
        )

        # Meanwhile, advance main with a different change
        main_ws_path = repo.workspace_path("main")
        (main_ws_path / "README.md").write_text("# My Project\nWith auth, config, and utils.\n")
        repo.quick_commit(
            workspace="main",
            prompt="Add README",
            agent=AgentIdentity(agent_id="coder-alpha", agent_type="code-gen"),
            auto_accept=True,
        )

        # Now promote feature-logging → main
        # Feature added lib/logger.py, main added README.md — no overlap
        result = repo.promote(
            workspace="feature-logging",
            target_lane="main",
            auto_accept=True,
        )
        assert result["status"] == "accepted", f"Expected accepted, got {result['status']}"
        print(f"✓ Promoted feature-logging → main")
        print(f"  Transition: {result['transition_id'][:12]}")

        # Verify main now has both README.md AND lib/logger.py
        main_head = repo.head("main")
        main_state = repo.wsm.get_state(main_head)
        main_files = repo.wsm._flatten_tree(main_state["root_tree"])
        assert "lib/logger.py" in main_files, "main should have logger.py after promote"
        assert "README.md" in main_files, "main should still have README.md"
        assert "lib/auth.py" in main_files, "main should still have auth.py"
        print(f"✓ Main now contains: {sorted(main_files.keys())}")

        # ── Phase 13: Promote — Conflict Detection ────────────────
        divider("Phase 13: Promote — Conflict Detection")

        # Create a new lane from current main, modify README.md there
        repo.create_lane("feature-docs", repo.head("main"))
        docs_ws_path = repo.workspace_path("feature-docs")
        (docs_ws_path / "README.md").write_text("# My Project (docs branch)\nUpdated by docs team.\n")
        repo.quick_commit(
            workspace="feature-docs",
            prompt="Update README with docs info",
            agent=AgentIdentity(agent_id="docs-agent", agent_type="docs"),
            auto_accept=True,
        )

        # Also modify README.md on main
        (main_ws_path / "README.md").write_text("# My Project (main branch)\nUpdated by main team.\n")
        repo.quick_commit(
            workspace="main",
            prompt="Update README on main",
            agent=AgentIdentity(agent_id="coder-alpha", agent_type="code-gen"),
            auto_accept=True,
        )

        # Now try to promote — README.md modified on both sides → conflict
        result = repo.promote(workspace="feature-docs", target_lane="main")
        assert result["status"] == "conflicts", f"Expected conflicts, got {result['status']}"
        print(f"✓ Conflict correctly detected")
        print(f"  Fork base: {result['fork_base'][:12]}")
        for c in result["conflicts"]:
            print(f"  ✗ {c['path']}  (lane: {c['lane_action']}, target: {c['target_action']})")

        # ── Phase 14: Final status ────────────────────────────────
        divider("Phase 14: Final Repository State")

        final = repo.status()
        print(f"Head:       {final['current_head'][:12]}")
        print(f"Pending:    {final['pending_proposals']}")
        print(f"Lanes:      {len(final['lanes'])}")
        print(f"Workspaces: {len(final['workspaces'])}")
        print(f"Objects:    {final['storage']['total_objects']}")
        print(f"Bytes:      {final['storage']['total_bytes']:,}")

        # ── Cleanup ───────────────────────────────────────────────
        repo.close()
        print(f"\n{'='*60}")
        print(f"  ALL TESTS PASSED ✓")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\n✗ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    test_full_workflow()
