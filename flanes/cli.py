"""
Flanes CLI

Commands designed for both human operators and AI agents.
Every command outputs structured JSON when --json is passed,
making it trivial for agents to parse. Human-readable output
is the default.

Workspace auto-detection:
    Commands that operate on a workspace (snapshot, propose, commit,
    restore) auto-detect which workspace you're in by checking if
    your current directory is inside .flanes/workspaces/<name>/. If not,
    they default to the 'main' workspace. You can always override
    with --workspace.

Usage:
    flanes init [path]
    flanes status
    flanes snapshot [--workspace NAME]
    flanes propose --prompt "..." --agent-id ID --agent-type TYPE [--workspace NAME]
    flanes accept TRANSITION_ID [--evaluator NAME] [--summary "..."]
    flanes reject TRANSITION_ID [--evaluator NAME] [--summary "..."]
    flanes commit --prompt "..." --agent-id ID --agent-type TYPE [--auto-accept]
    flanes history [--lane LANE] [--limit N] [--status STATUS]
    flanes log [--lane LANE] [--limit N] [--status STATUS]
    flanes trace [STATE_ID]
    flanes diff STATE_A STATE_B [--content]
    flanes search QUERY
    flanes lanes
    flanes lane create NAME [--base STATE_ID]
    flanes workspace list
    flanes workspace create NAME [--lane LANE] [--base STATE_ID]
    flanes workspace remove NAME [--force]
    flanes workspace update NAME [--state STATE_ID]
    flanes restore STATE_ID [--workspace NAME]
    flanes info STATE_ID
    flanes show STATE_ID PATH
    flanes doctor [--fix]
    flanes completion SHELL
"""

import argparse
import base64
import difflib
import json
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import flanes as _flanes_pkg

from .completions import BASH_COMPLETION, FISH_COMPLETION, ZSH_COMPLETION
from .repo import NotARepository, Repository
from .state import AgentIdentity, CostRecord, TransitionStatus


@contextmanager
def open_repo(args):
    """Open a Repository with guaranteed cleanup on any exit path."""
    repo = Repository.find(Path(args.path or "."))
    try:
        yield repo
    finally:
        repo.close()


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def short_hash(h: str) -> str:
    return h[:12] if h else "none"


def print_json(data):
    print(json.dumps(data, indent=2, default=str))


def get_verbosity(args) -> int:
    """Return verbosity level: 0=quiet, 1=normal, 2=verbose."""
    if getattr(args, "json", False):
        return 1
    if getattr(args, "verbose", False):
        return 2
    if getattr(args, "quiet", False):
        return 0
    return 1


def _display_hash(h: str, verbosity: int) -> str:
    """Return full or short hash based on verbosity."""
    if not h:
        return "none"
    if verbosity >= 2:
        return h
    return h[:12]


def detect_workspace(repo: Repository, explicit: str | None = None) -> str:
    """
    Detect which workspace the user is in.

    Priority:
    1. Explicit --workspace flag
    2. Current working directory is inside .flanes/workspaces/<name>/
    3. Default to 'main'
    """
    if explicit:
        return explicit

    cwd = Path.cwd().resolve()
    workspaces_dir = repo.flanes_dir / "workspaces"

    if workspaces_dir.exists():
        try:
            rel = cwd.relative_to(workspaces_dir)
            # First component of the relative path is the workspace name
            # For nested names like bugfix/utils, we need to match against known workspaces
            parts = rel.parts
            if parts:
                # Try progressively longer paths to find the workspace
                for i in range(len(parts), 0, -1):
                    candidate = str(Path(*parts[:i]))
                    if repo.wm.exists(candidate):
                        return candidate
                # Fallback: just use the first component
                return parts[0]
        except ValueError:
            pass

    return repo._default_lane()


def _blob_lines(store, blob_hash):
    """Retrieve blob content as lines for diffing. Returns None for binary."""
    obj = store.retrieve(blob_hash)
    if obj is None:
        return None
    if b"\x00" in obj.data[:8192]:
        return None  # binary
    return obj.data.decode("utf-8", errors="replace").splitlines(keepends=True)


# ── Commands ──────────────────────────────────────────────────


def cmd_init(args):
    v = get_verbosity(args)
    path = Path(args.path or ".").resolve()
    with Repository.init(path) as repo:
        head = repo.head()
        ws_path = repo.workspace_path(repo._default_lane())
        git_detected = repo._read_config().get("git_coexistence", False)

        if args.json:
            result = {
                "root": str(path),
                "head": head,
                "workspace": str(ws_path),
                "lane": repo._default_lane(),
            }
            if git_detected:
                result["git_detected"] = True
            print_json(result)
        elif v == 0:
            if head:
                print(head)
        else:
            print(f"✓ Initialized Flanes repository at {path}")
            if head:
                print(f"  Initial snapshot: {_display_hash(head, v)}")
            print(f"  Workspace: {ws_path}")
            print(f"  Lane: {repo._default_lane()}")
            if v >= 2 and head:
                print(f"  Full head: {head}")
            if git_detected:
                print()
                print("  Note: Detected existing Git repository.")
                print("    Add '.flanes/' to your .gitignore:")
                print("      echo '.flanes/' >> .gitignore")


def cmd_status(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        status = repo.status()

        if args.json:
            print_json(status)
        elif v == 0:
            print(status["current_head"] or "none")
        else:
            print(f"Repository: {status['root']}")
            print(f"Head:       {_display_hash(status['current_head'], v)}")
            print(f"Pending:    {status['pending_proposals']} proposals")
            print(
                f"Storage:    {status['storage']['total_objects']} objects, "
                f"{status['storage']['total_bytes']:,} bytes"
            )
            print("\nLanes:")
            for lane in status["lanes"]:
                marker = "→" if lane["head_state"] == status["current_head"] else " "
                print(f"  {marker} {lane['name']}: {_display_hash(lane['head_state'], v)}")
            if status.get("workspaces"):
                print("\nWorkspaces:")
                for ws in status["workspaces"]:
                    lock = " [locked]" if ws["status"] == "active" else ""
                    print(f"  {ws['name']}: {ws['path']}{lock}")
                    if v >= 2:
                        print(f"    Base: {ws.get('base_state', 'unknown')}")


def cmd_snapshot(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        ws_name = detect_workspace(repo, args.workspace)
        state_id = repo.snapshot(ws_name)

        if args.json:
            print_json({"state_id": state_id, "workspace": ws_name})
        elif v == 0:
            print(state_id)
        else:
            print(f"✓ Snapshot: {_display_hash(state_id, v)}")
            print(f"  Workspace: {ws_name}")
            if v >= 2:
                print(f"  Full ID:   {state_id}")


def cmd_propose(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        ws_name = detect_workspace(repo, args.workspace)

        agent = AgentIdentity(
            agent_id=args.agent_id,
            agent_type=args.agent_type,
            model=args.model,
        )

        ws_info = repo.wm.get(ws_name)
        lane = args.lane or (ws_info.lane if ws_info else "main")
        head = repo.head(lane)
        new_state = repo.snapshot(ws_name, parent_id=head)

        tags = args.tags.split(",") if args.tags else []
        cost = None
        if args.tokens_in or args.tokens_out:
            cost = CostRecord(
                tokens_in=args.tokens_in or 0,
                tokens_out=args.tokens_out or 0,
            )

        tid = repo.propose(
            from_state=head,
            to_state=new_state,
            prompt=args.prompt,
            agent=agent,
            lane=lane,
            tags=tags,
            cost=cost,
        )

        if args.json:
            print_json(
                {
                    "transition_id": tid,
                    "from_state": head,
                    "to_state": new_state,
                    "workspace": ws_name,
                    "lane": lane,
                    "status": "proposed",
                }
            )
        elif v == 0:
            print(tid)
        else:
            print(f"✓ Proposed transition: {_display_hash(tid, v)}")
            print(f"  Workspace: {ws_name}")
            print(f"  From:      {_display_hash(head, v)}")
            print(f"  To:        {_display_hash(new_state, v)}")
            print(f"  Lane:      {lane}")
            print(f"  Prompt:    {args.prompt[:80]}")
            if v >= 2 and cost:
                print(f"  Cost:      {cost.tokens_in} in / {cost.tokens_out} out")


def cmd_accept(args):
    with open_repo(args) as repo:
        status = repo.accept(
            args.transition_id,
            evaluator=args.evaluator,
            summary=args.summary or "",
        )

        if args.json:
            print_json({"transition_id": args.transition_id, "status": status.value})
        else:
            print(f"✓ Accepted: {args.transition_id[:12]}")


def cmd_reject(args):
    with open_repo(args) as repo:
        status = repo.reject(
            args.transition_id,
            evaluator=args.evaluator,
            summary=args.summary or "",
        )

        if args.json:
            print_json({"transition_id": args.transition_id, "status": status.value})
        else:
            print(f"✗ Rejected: {args.transition_id[:12]}")
            if args.summary:
                print(f"  Reason: {args.summary}")


def cmd_commit(args):
    """Quick commit: snapshot workspace + propose + optionally accept."""
    v = get_verbosity(args)
    with open_repo(args) as repo:
        ws_name = detect_workspace(repo, args.workspace)

        # Warn about auto-accept behavior
        if args.auto_accept and v > 0 and not args.json:
            print(
                "Note: --auto-accept will run evaluators but won't block on failures",
                file=sys.stderr,
            )

        agent = AgentIdentity(
            agent_id=args.agent_id,
            agent_type=args.agent_type,
            model=args.model,
        )

        tags = args.tags.split(",") if args.tags else []
        cost = None
        if args.tokens_in or args.tokens_out:
            cost = CostRecord(
                tokens_in=args.tokens_in or 0,
                tokens_out=args.tokens_out or 0,
            )

        result = repo.quick_commit(
            workspace=ws_name,
            prompt=args.prompt,
            agent=agent,
            lane=args.lane,
            tags=tags,
            cost=cost,
            auto_accept=args.auto_accept,
            evaluator=args.evaluator or "auto",
        )

        if args.json:
            result["workspace"] = ws_name
            print_json(result)
        elif v == 0:
            print(result["transition_id"])
        else:
            status_icon = "✓" if result["status"] == "accepted" else "◉"
            print(f"{status_icon} Committed: {_display_hash(result['transition_id'], v)}")
            print(f"  Workspace: {ws_name}")
            print(f"  From:      {_display_hash(result['from_state'], v)}")
            print(f"  To:        {_display_hash(result['to_state'], v)}")
            print(f"  Status:    {result['status']}")
            print(f"  Prompt:    {args.prompt[:80]}")
            if result.get("evaluation"):
                ev = result["evaluation"]
                ev_icon = "✓" if ev["passed"] else "✗"
                print(f"  Eval:      {ev_icon} {ev['summary']}")
            if v >= 2 and result.get("cost"):
                print(f"  Cost:      {result['cost']}")


def cmd_history(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        entries = repo.history(
            lane=args.lane,
            limit=args.limit,
            status=args.status,
        )

        if args.json:
            print_json(entries)
        elif v == 0:
            for e in entries:
                print(e["id"])
        else:
            if not entries:
                print("No transitions found.")
            else:
                for e in entries:
                    status_icons = {
                        "accepted": "✓",
                        "rejected": "✗",
                        "proposed": "◉",
                        "evaluating": "⟳",
                        "superseded": "○",
                    }
                    icon = status_icons.get(e["status"], "?")
                    ts = format_time(e["created_at"])

                    print(f"{icon} {_display_hash(e['id'], v)}  {ts}  [{e['status']}]")
                    from_h = _display_hash(e["from_state"], v)
                    to_h = _display_hash(e["to_state"], v)
                    print(f"  {from_h} → {to_h}")
                    print(f"  Agent: {e['agent']['agent_id']} ({e['agent']['agent_type']})")
                    print(f"  {e['intent_prompt'][:100]}")
                    if e.get("tags"):
                        print(f"  Tags: {', '.join(e['tags'])}")
                    if v >= 2:
                        if e.get("cost"):
                            print(f"  Cost: {e['cost']}")
                        if e.get("evaluation"):
                            print(f"  Evaluation: {e['evaluation']}")
                    print()


def cmd_trace(args):
    with open_repo(args) as repo:
        state_id = args.state_id or repo.head()
        lineage = repo.trace(state_id)

        if args.json:
            print_json(lineage)
        else:
            if not lineage:
                print("No lineage found (this may be the initial state).")
            else:
                print(f"Lineage for {short_hash(state_id)}:\n")
                for i, entry in enumerate(lineage):
                    connector = "  ├─" if i < len(lineage) - 1 else "  └─"
                    prefix = "  │ " if i < len(lineage) - 1 else "    "
                    to_h = short_hash(entry["to_state"])
                    from_h = short_hash(entry["from_state"])
                    print(f"{connector} {to_h} ← {from_h}")
                    agent = entry["agent"]
                    print(f"{prefix}   {agent['agent_id']} ({agent['agent_type']})")
                    print(f"{prefix}   {entry['intent_prompt'][:80]}")
                    if entry.get("tags"):
                        print(f"{prefix}   Tags: {', '.join(entry['tags'])}")
                    print()


def cmd_diff(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        result = repo.diff(args.state_a, args.state_b)

        show_content = getattr(args, "content", False)

        if args.json:
            data = dict(result)
            if show_content:
                content_diffs = []
                for path in sorted(result.get("added", {})):
                    added = result["added"]
                    blob_hash = added[path] if isinstance(added, dict) else None
                    if blob_hash:
                        lines = _blob_lines(repo.store, blob_hash)
                        if lines is None:
                            diff = f"Binary file {path} differs"
                        else:
                            diff = "".join(
                                difflib.unified_diff(
                                    [],
                                    lines,
                                    fromfile="/dev/null",
                                    tofile=f"b/{path}",
                                )
                            )
                    else:
                        diff = ""
                    content_diffs.append({"path": path, "type": "added", "diff": diff})
                for path in sorted(result.get("removed", {})):
                    removed = result["removed"]
                    blob_hash = removed[path] if isinstance(removed, dict) else None
                    if blob_hash:
                        lines = _blob_lines(repo.store, blob_hash)
                        if lines is None:
                            diff = f"Binary file {path} differs"
                        else:
                            diff = "".join(
                                difflib.unified_diff(
                                    lines,
                                    [],
                                    fromfile=f"a/{path}",
                                    tofile="/dev/null",
                                )
                            )
                    else:
                        diff = ""
                    content_diffs.append({"path": path, "type": "removed", "diff": diff})
                for path in sorted(result.get("modified", {})):
                    modified = result["modified"]
                    mod = modified[path] if isinstance(modified, dict) else None
                    if mod and isinstance(mod, dict):
                        old_lines = _blob_lines(repo.store, mod.get("before", ""))
                        new_lines = _blob_lines(repo.store, mod.get("after", ""))
                        if old_lines is None or new_lines is None:
                            diff = f"Binary file {path} differs"
                        else:
                            diff = "".join(
                                difflib.unified_diff(
                                    old_lines,
                                    new_lines,
                                    fromfile=f"a/{path}",
                                    tofile=f"b/{path}",
                                )
                            )
                    else:
                        diff = ""
                    content_diffs.append({"path": path, "type": "modified", "diff": diff})
                data["content_diffs"] = content_diffs
            print_json(data)
        else:
            print(f"Diff: {_display_hash(args.state_a, v)} → {_display_hash(args.state_b, v)}\n")

            # Get trees for content diff if needed
            state_a_obj = repo.wsm.get_state(args.state_a) if show_content else None
            state_b_obj = repo.wsm.get_state(args.state_b) if show_content else None
            files_a = repo.wsm._flatten_tree(state_a_obj["root_tree"]) if state_a_obj else {}
            files_b = repo.wsm._flatten_tree(state_b_obj["root_tree"]) if state_b_obj else {}

            if result["added"]:
                for path in sorted(result["added"]):
                    print(f"  + {path}")
                    if show_content and path in files_b:
                        lines = _blob_lines(repo.store, files_b[path])
                        if lines is None:
                            print(f"    Binary file {path} differs")
                        else:
                            diff = difflib.unified_diff(
                                [], lines, fromfile="/dev/null", tofile=f"b/{path}"
                            )
                        for line in diff:
                            print(f"    {line}", end="" if line.endswith("\n") else "\n")
            if result["removed"]:
                for path in sorted(result["removed"]):
                    print(f"  - {path}")
                    if show_content and path in files_a:
                        lines = _blob_lines(repo.store, files_a[path])
                        if lines is None:
                            print(f"    Binary file {path} differs")
                        else:
                            diff = difflib.unified_diff(
                                lines, [], fromfile=f"a/{path}", tofile="/dev/null"
                            )
                        for line in diff:
                            print(f"    {line}", end="" if line.endswith("\n") else "\n")
            if result["modified"]:
                for path in sorted(result["modified"]):
                    print(f"  ~ {path}")
                    if show_content and path in files_a and path in files_b:
                        old_lines = _blob_lines(repo.store, files_a[path])
                        new_lines = _blob_lines(repo.store, files_b[path])
                        if old_lines is None or new_lines is None:
                            print(f"    Binary file {path} differs")
                        else:
                            diff = difflib.unified_diff(
                                old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
                            )
                            for line in diff:
                                print(f"    {line}", end="" if line.endswith("\n") else "\n")

            if not result["added"] and not result["removed"] and not result["modified"]:
                print("  No differences.")
            else:
                total = len(result["added"]) + len(result["removed"]) + len(result["modified"])
                print(f"\n  {total} files changed, {result['unchanged_count']} unchanged")


def cmd_search(args):
    with open_repo(args) as repo:
        results = repo.search(args.query, limit=args.limit)

        if args.json:
            print_json(results)
        else:
            if not results:
                print(f"No results for '{args.query}'")
            else:
                print(f"Results for '{args.query}':\n")
                for r in results:
                    ts = format_time(r["created_at"])
                    print(f"  {r['intent_id'][:12]}  {ts}  [{r.get('status', '?')}]")
                    print(f"    Agent: {r['agent']['agent_id']}")
                    print(f"    {r['prompt'][:100]}")
                    if r.get("tags"):
                        print(f"    Tags: {', '.join(r['tags'])}")
                    print()


def cmd_lanes(args):
    with open_repo(args) as repo:
        lanes = repo.lanes()

        if args.json:
            print_json(lanes)
        else:
            head = repo.head()
            for lane in lanes:
                marker = "→" if lane["head_state"] == head else " "
                ts = format_time(lane["created_at"])
                fork_base = lane.get("fork_base")
                fork = f"  fork:{short_hash(fork_base)}" if fork_base else ""
                head = short_hash(lane["head_state"])
                name = lane["name"]
                print(f"  {marker} {name}: {head}{fork}  (created {ts})")


def cmd_lane_create(args):
    with open_repo(args) as repo:
        base = args.base or repo.head()
        repo.create_lane(args.name, base)

        ws_path = repo.workspace_path(args.name)
        if args.json:
            print_json({"name": args.name, "base": base, "workspace": str(ws_path)})
        else:
            print(f"✓ Created lane '{args.name}' from {short_hash(base)}")
            print(f"  Workspace: {ws_path}")


def cmd_lane_delete(args):
    with open_repo(args) as repo:
        deleted = repo.delete_lane(args.name, force=args.force)

        if args.json:
            print_json({"name": args.name, "deleted": deleted})
        elif deleted:
            print(f"✓ Deleted lane '{args.name}' (lane record + workspace)")
        else:
            print(f"Lane '{args.name}' not found")


# ── Workspace commands ────────────────────────────────────────


def cmd_workspace_list(args):
    with open_repo(args) as repo:
        workspaces = repo.workspaces()

        if args.json:
            print_json([w.to_dict() for w in workspaces])
        else:
            if not workspaces:
                print("No workspaces.")
            else:
                for ws in workspaces:
                    lock_info = ""
                    if ws.status == "active":
                        lock_info = f" [locked by {ws.agent_id}]"
                    print(f"  {ws.name}")
                    print(f"    Lane:   {ws.lane}")
                    print(f"    Path:   {ws.path}")
                    print(f"    Base:   {short_hash(ws.base_state)}")
                    print(f"    Status: {ws.status}{lock_info}")
                    print()


def cmd_workspace_create(args):
    with open_repo(args) as repo:
        ws = repo.workspace_create(
            args.name,
            lane=args.lane,
            state_id=args.base,
        )

        if args.json:
            print_json(ws.to_dict())
        else:
            print(f"✓ Created workspace '{ws.name}'")
            print(f"  Lane: {ws.lane}")
            print(f"  Path: {ws.path}")
            print(f"  Base: {short_hash(ws.base_state)}")


def cmd_workspace_remove(args):
    with open_repo(args) as repo:
        repo.workspace_remove(args.name, force=args.force)

        if args.json:
            print_json({"removed": args.name})
        else:
            print(f"✓ Removed workspace '{args.name}'")


def cmd_workspace_update(args):
    with open_repo(args) as repo:
        result = repo.workspace_update(args.name, state_id=args.state)

        if args.json:
            result["workspace"] = args.name
            print_json(result)
        else:
            print(f"✓ Updated workspace '{args.name}'")
            if result["mode"] == "incremental":
                print(f"  Added:     {result['added']}")
                print(f"  Modified:  {result['modified']}")
                print(f"  Removed:   {result['removed']}")
                print(f"  Unchanged: {result['unchanged']}")
            else:
                print(f"  Mode: {result['mode']}")


def cmd_restore(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        ws_name = detect_workspace(repo, args.workspace)

        if not args.force:
            print(f"This will update workspace '{ws_name}' to state {short_hash(args.state_id)}")
            resp = input("Continue? [y/N] ")
            if resp.lower() != "y":
                print("Aborted.")
                return

        result = repo.restore(ws_name, args.state_id)

        if args.json:
            result["workspace"] = ws_name
            print_json(result)
        elif v == 0:
            print(args.state_id)
        else:
            print(f"✓ Restored workspace '{ws_name}' to state {_display_hash(args.state_id, v)}")


def cmd_info(args):
    v = get_verbosity(args)
    with open_repo(args) as repo:
        state = repo.wsm.get_state(args.state_id)

        if state is None:
            print(f"State not found: {args.state_id}")
            return

        if args.json:
            print_json(state)
        elif v == 0:
            print(state["id"])
        else:
            print(f"State:   {_display_hash(state['id'], v)}")
            print(f"Tree:    {_display_hash(state['root_tree'], v)}")
            print(f"Parent:  {_display_hash(state['parent_id'], v)}")
            print(f"Created: {format_time(state['created_at'])}")

            files = repo.wsm._flatten_tree(state["root_tree"])
            print(f"\nFiles ({len(files)}):")
            for path in sorted(files):
                if v >= 2:
                    print(f"  {path}  ({files[path]})")
                else:
                    print(f"  {path}")


def cmd_promote(args):
    """Promote workspace work into a target lane (default: main)."""
    v = get_verbosity(args)
    with open_repo(args) as repo:
        ws_name = detect_workspace(repo, args.workspace)

        agent = None
        if args.agent_id and args.agent_type:
            agent = AgentIdentity(
                agent_id=args.agent_id,
                agent_type=args.agent_type,
                model=args.model,
            )

        result = repo.promote(
            workspace=ws_name,
            target_lane=args.target,
            prompt=args.prompt,
            agent=agent,
            auto_accept=args.auto_accept,
            evaluator=args.evaluator or "auto",
            force=getattr(args, "force", False),
        )

        if args.json:
            print_json(result)
        elif result["status"] == "conflicts":
            target_lane = result["target_lane"]
            source_lane = result.get("source_lane", ws_name)
            fork_base = result["fork_base"]
            target_head = repo.head(target_lane)

            print(f"✗ Conflicts detected — cannot promote '{ws_name}' into '{target_lane}'")
            print()
            print(f"  Fork base:   {_display_hash(fork_base, v)}")
            print(f"  Your head:   {_display_hash(repo.head(source_lane), v)}")
            print(f"  Target head: {_display_hash(target_head, v)}")
            print()
            print(f"  Conflicting files ({len(result['conflicts'])}):")
            for c in result["conflicts"]:
                print(f"    {c['path']}")
                print(f"      Your change:   {c['lane_action']}")
                print(f"      Their change:  {c['target_action']}")

            if result["lane_only"]:
                print(f"\n  Lane-only changes ({len(result['lane_only'])}):")
                for p in result["lane_only"][:5]:
                    print(f"    {p}")
                if len(result["lane_only"]) > 5:
                    print(f"    ... and {len(result['lane_only']) - 5} more")

            if result["target_only"]:
                print(f"\n  Target-only changes ({len(result['target_only'])}):")
                for p in result["target_only"][:5]:
                    print(f"    {p}")
                if len(result["target_only"]) > 5:
                    print(f"    ... and {len(result['target_only']) - 5} more")

            print("\n  How to resolve:")
            print()
            print("    Option 1: Update workspace to target and manually fix")
            state_hash = _display_hash(target_head, v)
            print(f"      flanes workspace update {ws_name} --state {state_hash}")
            print("      # Edit conflicting files in workspace")
            print(
                "      flanes commit -m 'Resolve conflicts' --agent-id <agent> --agent-type <type>"
            )
            print(f"      flanes promote -w {ws_name} --target {target_lane}")
            print()
            print("    Option 2: Re-run agent from updated base")
            print(f"      flanes lane create {ws_name}-v2 --base {_display_hash(target_head, v)}")
            print("      # Run agent again on new lane")
            print()
            print("    Option 3: Force promote (overwrites their changes)")
            print(f"      flanes promote -w {ws_name} --target {target_lane} --force")
        elif v == 0:
            print(result.get("transition_id", ""))
        else:
            icon = "✓" if result["status"] == "accepted" else "◉"
            print(f"{icon} Promoted: {ws_name} → {result['target_lane']}")
            print(f"  Transition: {_display_hash(result['transition_id'], v)}")
            print(f"  From:       {_display_hash(result['from_state'], v)}")
            print(f"  To:         {_display_hash(result['to_state'], v)}")
            print(f"  Status:     {result['status']}")


def cmd_show(args):
    """Show file content at a given state."""
    with open_repo(args) as repo:
        state = repo.wsm.get_state(args.state_id)

        if state is None:
            raise ValueError(f"State not found: {args.state_id}")

        files = repo.wsm._flatten_tree(state["root_tree"])
        blob_hash = files.get(args.file_path)

        if blob_hash is None:
            raise ValueError(f"File not found in state {args.state_id}: {args.file_path}")

        obj = repo.store.retrieve(blob_hash)
        if obj is None:
            raise ValueError(f"Blob not found: {blob_hash}")

        if args.json:
            print_json(
                {
                    "state_id": args.state_id,
                    "path": args.file_path,
                    "blob_hash": blob_hash,
                    "size": len(obj.data),
                    "content_base64": base64.b64encode(obj.data).decode("ascii"),
                }
            )
        else:
            sys.stdout.buffer.write(obj.data)


def cmd_doctor(args):
    """Check repository health and optionally fix issues."""
    with open_repo(args) as repo:
        fix = getattr(args, "fix", False)
        findings = []
        fixed_count = 0

        # Check 1: Dirty workspaces
        for ws in repo.wm.list():
            dirty = repo.wm.is_dirty(ws.name)
            if dirty:
                finding = {
                    "check": "dirty_workspace",
                    "workspace": ws.name,
                    "detail": f"Workspace '{ws.name}' has interrupted operation marker",
                    "fixable": True,
                }
                if fix:
                    # Re-materialize from base_state
                    try:
                        ws_path = ws.path
                        dirty_path = ws_path / ".flanes_materializing"
                        dirty_path.unlink(missing_ok=True)
                        finding["fixed"] = True
                        fixed_count += 1
                    except Exception as e:
                        finding["fixed"] = False
                        finding["fix_error"] = str(e)
                findings.append(finding)

        # Check 2: Stale locks
        for ws in repo.wm.list():
            owner = repo.wm.lock_holder(ws.name)
            if owner and repo.wm._is_lock_stale(owner):
                finding = {
                    "check": "stale_lock",
                    "workspace": ws.name,
                    "detail": f"Workspace '{ws.name}' has a stale lock (pid: {owner.get('pid')})",
                    "fixable": True,
                }
                if fix:
                    try:
                        repo.wm.release(ws.name)
                        finding["fixed"] = True
                        fixed_count += 1
                    except Exception as e:
                        finding["fixed"] = False
                        finding["fix_error"] = str(e)
                findings.append(finding)

        # Check 3: Orphaned directories (dirs in workspaces/ with no .json metadata)
        workspaces_dir = repo.wm.workspaces_dir
        if workspaces_dir.exists():
            for item in workspaces_dir.iterdir():
                if item.is_dir() and not item.name.endswith(".lockdir"):
                    meta_path = workspaces_dir / f"{item.name}.json"
                    if not meta_path.exists():
                        finding = {
                            "check": "orphaned_directory",
                            "workspace": item.name,
                            "detail": f"Directory '{item.name}' has no metadata file",
                            "fixable": True,
                        }
                        if fix:
                            try:
                                shutil.rmtree(item)
                                finding["fixed"] = True
                                fixed_count += 1
                            except Exception as e:
                                finding["fixed"] = False
                                finding["fix_error"] = str(e)
                        findings.append(finding)

        # Check 4: Missing directories (.json metadata but no workspace dir)
        if workspaces_dir.exists():
            for meta_file in workspaces_dir.glob("*.json"):
                ws_name = meta_file.stem
                ws_dir = workspaces_dir / ws_name
                # Skip lockdir-related files
                if any(part.endswith(".lockdir") for part in meta_file.parts):
                    continue
                if not ws_dir.exists():
                    finding = {
                        "check": "missing_directory",
                        "workspace": ws_name,
                        "detail": f"Metadata for '{ws_name}' exists but directory is missing",
                        "fixable": True,
                    }
                    if fix:
                        try:
                            meta_file.unlink()
                            finding["fixed"] = True
                            fixed_count += 1
                        except Exception as e:
                            finding["fixed"] = False
                            finding["fix_error"] = str(e)
                    findings.append(finding)

        # Check 5: Lane-workspace desync (lane in DB but no workspace)
        known_ws_names = {ws.name for ws in repo.wm.list()}
        for lane in repo.lanes():
            lane_name = lane["name"]
            if lane_name == "main":
                continue  # Main workspace is always the repo root
            if lane_name not in known_ws_names:
                finding = {
                    "check": "lane_without_workspace",
                    "workspace": lane_name,
                    "detail": (
                        f"Lane '{lane_name}' exists in database but has no workspace "
                        f"(use 'flanes lane delete {lane_name}' to clean up)"
                    ),
                    "fixable": True,
                }
                if fix:
                    try:
                        repo.delete_lane(lane_name, force=True)
                        finding["fixed"] = True
                        fixed_count += 1
                    except Exception as e:
                        finding["fixed"] = False
                        finding["fix_error"] = str(e)
                findings.append(finding)

        # Check 6: Workspace without lane (workspace on disk but lane deleted from DB)
        for ws in repo.wm.list():
            if ws.name == "main":
                continue
            if not repo.wsm.lane_exists(ws.lane):
                finding = {
                    "check": "workspace_without_lane",
                    "workspace": ws.name,
                    "detail": (
                        f"Workspace '{ws.name}' tracks lane '{ws.lane}' "
                        f"which no longer exists in database"
                    ),
                    "fixable": True,
                }
                if fix:
                    try:
                        repo.wm.remove(ws.name, force=True)
                        finding["fixed"] = True
                        fixed_count += 1
                    except Exception as e:
                        finding["fixed"] = False
                        finding["fix_error"] = str(e)
                findings.append(finding)

        # Check 7: Version mismatch
        config_path = repo.flanes_dir / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            repo_version = config.get("version", "unknown")
            if repo_version != _flanes_pkg.__version__:
                findings.append(
                    {
                        "check": "version_mismatch",
                        "detail": (
                            f"Repository version '{repo_version}' differs"
                            f" from flanes version '{_flanes_pkg.__version__}'"
                        ),
                        "fixable": False,
                    }
                )

        if args.json:
            print_json({"findings": findings, "fixed": fixed_count})
        else:
            if not findings:
                print("✓ No issues found.")
            else:
                for f in findings:
                    if fix and f.get("fixed"):
                        marker = "[X]"
                    elif f.get("fixable"):
                        marker = "[!]"
                    else:
                        marker = "[!]"
                    print(f"  {marker} {f['detail']}")
                print()
                if fix:
                    print(f"  Fixed {fixed_count} issue(s).")
                else:
                    fixable = sum(1 for f in findings if f.get("fixable"))
                    if fixable:
                        print(f"  {fixable} issue(s) can be fixed with 'flanes doctor --fix'.")


def cmd_gc(args):
    """Garbage collect unreachable objects and expired transitions."""
    v = get_verbosity(args)
    with open_repo(args) as repo:
        dry_run = not args.confirm
        max_age = args.older_than

        result = repo.gc(dry_run=dry_run, max_age_days=max_age)

        if args.json:
            print_json(result.to_dict())
        elif v == 0:
            print(f"{result.deleted_objects}")
        else:
            mode = "DRY RUN" if result.dry_run else "COMPLETED"
            print(f"GC {mode}")
            print(f"  Reachable objects:     {result.reachable_objects}")
            print(f"  Deletable objects:     {result.deleted_objects}")
            print(f"  Reclaimable bytes:     {result.deleted_bytes:,}")
            print(f"  Deletable states:      {result.deleted_states}")
            print(f"  Deletable transitions: {result.deleted_transitions}")
            print(f"  Elapsed:               {result.elapsed_ms:.1f}ms")
            if result.dry_run and (result.deleted_objects or result.deleted_transitions):
                print("\n  Run 'flanes gc --confirm' to actually delete.")


def cmd_cat_file(args):
    """Low-level CAS object inspector."""
    with open_repo(args) as repo:
        obj = repo.store.retrieve(args.hash)
        source = "cas"

        if obj is None:
            # Try world_states table
            state = repo.wsm.get_state(args.hash)
            if state is not None:
                source = "state"
            else:
                raise ValueError(f"Object not found: {args.hash}")

        if source == "state":
            state = repo.wsm.get_state(args.hash)
            if args.type and args.type != "state":
                raise ValueError(f"Type mismatch: object is a state, expected {args.type}")
            if args.json:
                print_json(
                    {
                        "hash": args.hash,
                        "type": "state",
                        "root_tree": state["root_tree"],
                        "parent_id": state["parent_id"],
                        "created_at": state["created_at"],
                        "metadata": state["metadata"],
                    }
                )
            else:
                print("type: state")
                print(f"root_tree: {state['root_tree']}")
                print(f"parent_id: {state['parent_id'] or 'none'}")
                print(f"created_at: {format_time(state['created_at'])}")
                if state["metadata"]:
                    print(f"metadata: {json.dumps(state['metadata'])}")
        else:
            obj_type = obj.type.value
            if args.type and args.type != obj_type:
                raise ValueError(f"Type mismatch: object is a {obj_type}, expected {args.type}")

            if obj_type == "blob":
                if args.json:
                    print_json(
                        {
                            "hash": obj.hash,
                            "type": "blob",
                            "size": obj.size,
                            "content_base64": base64.b64encode(obj.data).decode("ascii"),
                        }
                    )
                else:
                    sys.stdout.buffer.write(obj.data)
            elif obj_type == "tree":
                import json as _json

                entries = _json.loads(obj.data.decode())
                if args.json:
                    result_entries = []
                    for name, entry in entries:
                        typ, h = entry[0], entry[1]
                        mode = entry[2] if len(entry) > 2 else (0o755 if typ == "tree" else 0o644)
                        result_entries.append(
                            {
                                "name": name,
                                "type": typ,
                                "hash": h,
                                "mode": oct(mode),
                            }
                        )
                    print_json(
                        {
                            "hash": obj.hash,
                            "type": "tree",
                            "entries": result_entries,
                        }
                    )
                else:
                    for name, entry in entries:
                        typ, h = entry[0], entry[1]
                        mode = entry[2] if len(entry) > 2 else (0o755 if typ == "tree" else 0o644)
                        print(f"{oct(mode)} {typ} {h} {name}")
            else:
                if args.json:
                    print_json(
                        {
                            "hash": obj.hash,
                            "type": obj_type,
                            "size": obj.size,
                            "content_base64": base64.b64encode(obj.data).decode("ascii"),
                        }
                    )
                else:
                    sys.stdout.buffer.write(obj.data)


def cmd_export_git(args):
    """Export Flanes history to a git repository."""
    from .git_bridge import export_to_git

    with open_repo(args) as repo:
        target = Path(args.target_dir).resolve()
        lane = args.lane or repo._default_lane()

        result = export_to_git(repo, target, lane=lane)

        if args.json:
            print_json(result)
        else:
            print(f"Exported {result['commits']} commits to {result['target']}")


def cmd_import_git(args):
    """Import git history into a Flanes repository."""
    from .git_bridge import import_from_git

    with open_repo(args) as repo:
        source = Path(args.source_dir).resolve()
        lane = args.lane or "main"

        result = import_from_git(source, repo, lane=lane)

        if args.json:
            print_json(result)
        else:
            print(f"Imported {result['commits_imported']} commits into lane '{result['lane']}'")


def cmd_serve(args):
    """Start the Flanes REST API server."""
    from .server import serve

    serve(
        args.path or ".",
        host=args.host,
        port=args.port,
        api_token=args.token,
        web=args.web,
        insecure=args.insecure,
    )


def cmd_mcp(args):
    """Start the MCP tool server on stdio."""
    from .mcp_server import run_mcp_server

    run_mcp_server(Path(args.path or "."))


def cmd_completion(args):
    """Print shell completion script."""
    scripts = {
        "bash": BASH_COMPLETION,
        "zsh": ZSH_COMPLETION,
        "fish": FISH_COMPLETION,
    }
    print(scripts[args.shell])


# ── Budget commands ───────────────────────────────────────────


def cmd_budget_show(args):
    """Show budget status for a lane."""
    with open_repo(args) as repo:
        lane = args.lane

        status = repo.get_budget_status(lane)

        if args.json:
            print_json(status.to_dict() if status else {"budget": None})
        else:
            if status is None:
                print(f"No budget configured for lane '{lane}'.")
            else:
                print(f"Budget for lane '{lane}':")
                c = status.config
                s = status

                def _budget_line(label, used, limit, fmt=","):
                    p = (used / limit * 100) if limit else 0
                    print(f"  {label}{used:{fmt}} / {limit:{fmt}} ({p:.1f}%)")

                if c.max_tokens_in is not None:
                    _budget_line("Tokens in:  ", s.total_tokens_in, c.max_tokens_in)
                if c.max_tokens_out is not None:
                    _budget_line("Tokens out: ", s.total_tokens_out, c.max_tokens_out)
                if c.max_api_calls is not None:
                    _budget_line("API calls:  ", s.total_api_calls, c.max_api_calls)
                if c.max_wall_time_ms is not None:
                    _budget_line(
                        "Wall time:  ",
                        s.total_wall_time_ms,
                        c.max_wall_time_ms,
                        fmt=",.0f",
                    )
                if status.warnings:
                    print(f"  Warnings:   {', '.join(status.warnings)}")
                if status.exceeded:
                    print(f"  EXCEEDED:   {', '.join(status.exceeded)}")


def cmd_budget_set(args):
    """Set budget for a lane."""
    with open_repo(args) as repo:
        lane = args.lane

        kwargs = {}
        if args.max_tokens_in is not None:
            kwargs["max_tokens_in"] = args.max_tokens_in
        if args.max_tokens_out is not None:
            kwargs["max_tokens_out"] = args.max_tokens_out
        if args.max_api_calls is not None:
            kwargs["max_api_calls"] = args.max_api_calls
        if args.alert_threshold is not None:
            kwargs["alert_threshold_pct"] = args.alert_threshold

        repo.set_budget(lane, **kwargs)

        if args.json:
            print_json({"lane": lane, "budget": kwargs})
        else:
            print(f"Budget set for lane '{lane}'.")


# ── Template commands ─────────────────────────────────────────


def cmd_template_list(args):
    """List available templates."""
    with open_repo(args) as repo:
        tm = repo.get_template_manager()
        templates = tm.list()

        if args.json:
            print_json([t.to_dict() for t in templates])
        else:
            if not templates:
                print("No templates.")
            else:
                for t in templates:
                    desc = f"  {t.description}" if t.description else ""
                    print(f"  {t.name}{desc}")
                    print(f"    Files: {len(t.files)}, Dirs: {len(t.directories)}")


def cmd_template_create(args):
    """Create a new template."""
    from .templates import WorkspaceTemplate

    with open_repo(args) as repo:
        tm = repo.get_template_manager()

        template = WorkspaceTemplate(
            name=args.name,
            description=args.description or "",
        )
        path = tm.save(template)

        if args.json:
            print_json({"name": args.name, "path": str(path)})
        else:
            print(f"Created template '{args.name}' at {path}")


def cmd_template_show(args):
    """Show template details."""
    with open_repo(args) as repo:
        tm = repo.get_template_manager()
        template = tm.load(args.name)

        if template is None:
            if args.json:
                print_json({"error": f"Template '{args.name}' not found"})
            else:
                print(f"Template '{args.name}' not found.")
            return

        if args.json:
            print_json(template.to_dict())
        else:
            print(f"Template: {template.name}")
            if template.description:
                print(f"Description: {template.description}")
            if template.files:
                print("Files:")
                for f in template.files:
                    print(f"  {f.path}")
            if template.directories:
                print("Directories:")
                for d in template.directories:
                    print(f"  {d}/")
            if template.flanesignore_patterns:
                print("Flaignore patterns:")
                for p in template.flanesignore_patterns:
                    print(f"  {p}")


# ── Evaluate command ──────────────────────────────────────────


def cmd_evaluate(args):
    """Run evaluators on a transition."""
    with open_repo(args) as repo:
        ws_name = detect_workspace(repo, args.workspace)

        if args.transition_id:
            status = repo.evaluate_transition(args.transition_id, ws_name)
            if args.json:
                print_json({"transition_id": args.transition_id, "status": status.value})
            else:
                icon = "✓" if status == TransitionStatus.ACCEPTED else "✗"
                print(f"{icon} Evaluation: {status.value}")
        else:
            result = repo.run_evaluators(ws_name)
            if args.json:
                print_json(result.to_dict())
            else:
                icon = "✓" if result.passed else "✗"
                print(f"{icon} Evaluation: {'passed' if result.passed else 'FAILED'}")
                print(f"  Summary: {result.summary}")
                if result.checks:
                    for name, passed in result.checks.items():
                        check_icon = "✓" if passed else "✗"
                        print(f"  {check_icon} {name}")


# ── Semantic search command ───────────────────────────────────


def cmd_semantic_search(args):
    """Search intents using semantic similarity."""
    with open_repo(args) as repo:
        results = repo.semantic_search(args.query, limit=args.limit)

        if args.json:
            print_json(results)
        else:
            if not results:
                print(f"No results for '{args.query}'")
            else:
                print(f"Results for '{args.query}':\n")
                for r in results:
                    score = r.get("score")
                    score_str = f"  (score: {score:.3f})" if score is not None else ""
                    print(f"  {r['intent_id'][:12]}{score_str}")
                    print(f"    {r['prompt'][:100]}")
                    if r.get("tags"):
                        print(f"    Tags: {', '.join(r['tags'])}")
                    print()


# ── Project commands ──────────────────────────────────────────


def cmd_project_init(args):
    """Initialize a multi-repo project."""
    from .project import Project

    path = Path(args.path or ".").resolve()
    project = Project.init(path, name=args.name)

    if args.json:
        print_json({"name": project.config.name, "root": str(project.root)})
    else:
        print(f"Initialized project '{project.config.name}' at {project.root}")

    project.close()


def cmd_project_add(args):
    """Add a repo to the project."""
    from .project import Project

    project = Project.find(Path(args.path or "."))
    project.add_repo(args.repo_path, args.mount_point, lane=args.lane or "main")

    if args.json:
        print_json({"repo_path": args.repo_path, "mount_point": args.mount_point})
    else:
        print(f"Added repo '{args.repo_path}' as '{args.mount_point}'")

    project.close()


def cmd_project_status(args):
    """Show project status."""
    from .project import Project

    project = Project.find(Path(args.path or "."))
    status = project.status()

    if args.json:
        print_json(status)
    else:
        print(f"Project: {status['project']}")
        print(f"Root:    {status['root']}")
        if status["repos"]:
            print("\nRepos:")
            for name, info in status["repos"].items():
                head_str = short_hash(info["head"]) if info["head"] else "none"
                print(f"  {name}: {head_str} [{info['status']}]")
        else:
            print("  No repos configured.")

    project.close()


def cmd_project_snapshot(args):
    """Snapshot all repos in the project."""
    from .project import Project

    project = Project.find(Path(args.path or "."))
    result = project.coordinated_snapshot()

    if args.json:
        print_json(result)
    else:
        print(f"Project: {result['project']}")
        for name, info in result["snapshots"].items():
            state_str = short_hash(info["state_id"]) if info["state_id"] else "none"
            print(f"  {name}: {state_str} [{info['status']}]")

    project.close()


# ── Remote commands ───────────────────────────────────────────


def cmd_remote_push(args):
    """Push objects to remote storage."""
    with open_repo(args) as repo:
        try:
            sync = repo.get_remote_sync_manager()
        except ValueError as e:
            if args.json:
                print_json({"error": str(e)})
            else:
                print(f"Error: {e}", file=sys.stderr)
            return

        result = sync.push()
        if args.json:
            print_json(result)
        else:
            print(f"Pushed {result['pushed']} objects ({result['skipped']} already synced)")

        if args.metadata:
            meta_result = sync.push_metadata(repo.wsm)
            if args.json:
                print_json(meta_result)
            else:
                print(f"Pushed metadata for {meta_result['pushed_lanes']} lanes")


def cmd_remote_pull(args):
    """Pull objects from remote storage."""
    with open_repo(args) as repo:
        try:
            sync = repo.get_remote_sync_manager()
        except ValueError as e:
            if args.json:
                print_json({"error": str(e)})
            else:
                print(f"Error: {e}", file=sys.stderr)
            return

        result = sync.pull()
        if args.json:
            print_json(result)
        else:
            print(f"Pulled {result['pulled']} objects ({result['skipped']} already local)")

        if args.metadata:
            meta_result = sync.pull_metadata(repo.wsm)
            if args.json:
                print_json(meta_result)
            else:
                print(
                    f"Pulled metadata: {meta_result['lanes_pulled']} lanes, "
                    f"{meta_result['transitions_imported']} transitions, "
                    f"{meta_result['intents_imported']} intents"
                )
                if meta_result["conflicts"]:
                    print("\nConflicts detected:")
                    for c in meta_result["conflicts"]:
                        print(
                            f"  Lane '{c['lane']}': local={c['local_head'][:12]}, "
                            f"remote={c['remote_head'][:12]}"
                        )


def cmd_remote_status(args):
    """Show remote sync status."""
    with open_repo(args) as repo:
        try:
            sync = repo.get_remote_sync_manager()
        except ValueError as e:
            if args.json:
                print_json({"error": str(e)})
            else:
                print(f"Error: {e}", file=sys.stderr)
            return

        status = sync.status()
        if args.json:
            print_json(status)
        else:
            print("Remote sync status:")
            print(f"  Local only:  {len(status['local_only'])}")
            print(f"  Remote only: {len(status['remote_only'])}")
            print(f"  Synced:      {len(status['synced'])}")


# ── Argument Parser ───────────────────────────────────────────

COMMAND_ALIASES = {
    "ci": "commit",
    "st": "status",
    "sn": "snapshot",
    "hist": "history",
}

GROUPED_HELP = """\
commands:
  Core:
    init              Initialize a new repository
    status (st)       Show repository status
    snapshot (sn)     Snapshot a workspace
    commit (ci)       Quick commit: snapshot + propose + accept
    propose           Propose a state transition
    accept            Accept a proposed transition
    reject            Reject a proposed transition

  History & Query:
    history (hist)    Show transition history
    log               Alias for history
    trace             Show causal lineage of a state
    diff              Compare two world states
    search            Search intents by text/tags
    semantic-search   Embedding-based semantic search
    info              Show state details
    show              Display file content at a given state

  Lanes & Workspaces:
    lanes             List all lanes
    lane create       Create a new lane
    workspace         Manage workspaces (list, create, remove, update)
    restore           Restore a workspace to any state
    promote           Promote workspace work into a target lane

  Evaluators & Budgets:
    evaluate          Run evaluators on a workspace or transition
    budget            Show or set budget limits for a lane

  Admin & Integration:
    gc                Garbage collect unreachable objects
    doctor            Check and fix repository health
    serve             Start REST API server
    mcp               Start MCP tool server (stdio)
    export-git        Export Flanes history to a git repository
    import-git        Import git history into Flanes
    remote            Remote storage operations (push, pull, status)
    project           Multi-repo project management
    template          Workspace templates (list, create, show)
    cat-file          Low-level CAS object inspector
    completion        Generate shell completion scripts
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flanes",
        description="Flanes — Version Control for Agentic AI Systems",
        epilog=GROUPED_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ver = _flanes_pkg.__version__
    parser.add_argument("--version", "-V", action="version", version=f"flanes {ver}")
    parser.add_argument("--path", "-C", default=".", help="Repository path")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    verbosity.add_argument("--quiet", "-q", action="store_true", help="Quiet output")

    sub = parser.add_subparsers(dest="command")

    # init
    p = sub.add_parser("init", help="Initialize a new repository")
    p.set_defaults(func=cmd_init)

    # status
    p = sub.add_parser("status", help="Show repository status")
    p.set_defaults(func=cmd_status)

    # snapshot
    p = sub.add_parser("snapshot", help="Snapshot a workspace")
    p.add_argument(
        "--workspace", "-w", default=None, help="Workspace name (auto-detected from cwd if omitted)"
    )
    p.set_defaults(func=cmd_snapshot)

    # propose
    p = sub.add_parser("propose", help="Propose a state transition")
    p.add_argument("--prompt", "-m", required=True, help="Intent description")
    p.add_argument("--agent-id", required=True)
    p.add_argument("--agent-type", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--lane", default=None)
    p.add_argument("--workspace", "-w", default=None)
    p.add_argument("--tags", default=None, help="Comma-separated tags")
    p.add_argument("--tokens-in", type=int, default=0)
    p.add_argument("--tokens-out", type=int, default=0)
    p.set_defaults(func=cmd_propose)

    # accept
    p = sub.add_parser("accept", help="Accept a proposed transition")
    p.add_argument("transition_id")
    p.add_argument("--evaluator", default="manual")
    p.add_argument("--summary", default="")
    p.set_defaults(func=cmd_accept)

    # reject
    p = sub.add_parser("reject", help="Reject a proposed transition")
    p.add_argument("transition_id")
    p.add_argument("--evaluator", default="manual")
    p.add_argument("--summary", default="")
    p.set_defaults(func=cmd_reject)

    # commit (quick)
    p = sub.add_parser("commit", help="Quick commit: snapshot + propose + optionally accept")
    p.add_argument("--prompt", "-m", required=True)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--agent-type", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--lane", default=None)
    p.add_argument("--workspace", "-w", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--tokens-in", type=int, default=0)
    p.add_argument("--tokens-out", type=int, default=0)
    p.add_argument("--auto-accept", "-a", action="store_true")
    p.add_argument("--evaluator", default=None)
    p.set_defaults(func=cmd_commit)

    # history
    p = sub.add_parser("history", help="Show transition history")
    p.add_argument("--lane", default=None)
    p.add_argument("--limit", "-n", type=int, default=20)
    p.add_argument("--status", default=None, choices=["proposed", "accepted", "rejected"])
    p.set_defaults(func=cmd_history)

    # log (alias for history)
    p = sub.add_parser("log", help="Show transition history (alias for 'history')")
    p.add_argument("--lane", default=None)
    p.add_argument("--limit", "-n", type=int, default=20)
    p.add_argument("--status", default=None, choices=["proposed", "accepted", "rejected"])
    p.set_defaults(func=cmd_history)

    # trace
    p = sub.add_parser("trace", help="Trace the lineage of a state")
    p.add_argument("state_id", nargs="?", default=None)
    p.set_defaults(func=cmd_trace)

    # diff
    p = sub.add_parser("diff", help="Diff two world states")
    p.add_argument("state_a")
    p.add_argument("state_b")
    p.add_argument(
        "--content", "-c", action="store_true", help="Show unified diff of file contents"
    )
    p.set_defaults(func=cmd_diff)

    # search
    p = sub.add_parser("search", help="Search intents")
    p.add_argument("query")
    p.add_argument("--limit", "-n", type=int, default=20)
    p.set_defaults(func=cmd_search)

    # lanes
    p = sub.add_parser("lanes", help="List lanes")
    p.set_defaults(func=cmd_lanes)

    # lane create
    p = sub.add_parser("lane", help="Lane management")
    lane_sub = p.add_subparsers(dest="lane_command")
    lc = lane_sub.add_parser("create", help="Create a new lane (with workspace)")
    lc.add_argument("name")
    lc.add_argument("--base", default=None)
    lc.set_defaults(func=cmd_lane_create)

    ld = lane_sub.add_parser("delete", help="Delete a lane and its workspace")
    ld.add_argument("name")
    ld.add_argument("--force", action="store_true", help="Force delete even if workspace is locked")
    ld.set_defaults(func=cmd_lane_delete)

    # workspace
    p = sub.add_parser("workspace", help="Workspace management")
    ws_sub = p.add_subparsers(dest="ws_command")

    wl = ws_sub.add_parser("list", help="List workspaces")
    wl.set_defaults(func=cmd_workspace_list)

    wc = ws_sub.add_parser("create", help="Create a workspace")
    wc.add_argument("name")
    wc.add_argument("--lane", default=None)
    wc.add_argument("--base", default=None, help="State ID to materialize")
    wc.set_defaults(func=cmd_workspace_create)

    wr = ws_sub.add_parser("remove", help="Remove a workspace")
    wr.add_argument("name")
    wr.add_argument("--force", "-f", action="store_true")
    wr.set_defaults(func=cmd_workspace_remove)

    wu = ws_sub.add_parser("update", help="Update workspace to a state")
    wu.add_argument("name")
    wu.add_argument("--state", default=None, help="Target state (default: lane head)")
    wu.set_defaults(func=cmd_workspace_update)

    # restore
    p = sub.add_parser("restore", help="Restore a workspace to a state")
    p.add_argument("state_id")
    p.add_argument("--workspace", "-w", default=None)
    p.add_argument("--force", "-f", action="store_true")
    p.set_defaults(func=cmd_restore)

    # info
    p = sub.add_parser("info", help="Show details about a world state")
    p.add_argument("state_id")
    p.set_defaults(func=cmd_info)

    # promote
    p = sub.add_parser("promote", help="Promote workspace work into a target lane")
    p.add_argument(
        "--workspace", "-w", default=None, help="Source workspace (auto-detected from cwd)"
    )
    p.add_argument("--target", "-t", default=None, help="Target lane (default: main)")
    p.add_argument("--prompt", "-m", default=None)
    p.add_argument("--agent-id", default=None)
    p.add_argument("--agent-type", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--auto-accept", "-a", action="store_true")
    p.add_argument("--evaluator", default=None)
    p.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force promote, overwriting conflicting changes in target",
    )
    p.set_defaults(func=cmd_promote)

    # show
    p = sub.add_parser("show", help="Show file content at a given state")
    p.add_argument("state_id")
    p.add_argument("file_path")
    p.set_defaults(func=cmd_show)

    # gc
    p = sub.add_parser("gc", help="Garbage collect unreachable objects")
    p.add_argument("--confirm", action="store_true", help="Actually delete (default is dry-run)")
    p.add_argument(
        "--older-than",
        type=int,
        default=30,
        help="Only delete transitions older than N days (default: 30)",
    )
    p.set_defaults(func=cmd_gc)

    # doctor
    p = sub.add_parser("doctor", help="Check repository health")
    p.add_argument("--fix", action="store_true", help="Attempt to fix issues")
    p.set_defaults(func=cmd_doctor)

    # cat-file
    p = sub.add_parser("cat-file", help="Inspect a CAS object by hash")
    p.add_argument("hash", help="Object hash to inspect")
    p.add_argument(
        "--type", choices=["blob", "tree", "state"], default=None, help="Verify object type"
    )
    p.set_defaults(func=cmd_cat_file)

    # export-git
    p = sub.add_parser("export-git", help="Export Flanes history to a git repository")
    p.add_argument("target_dir", help="Target directory for git repo")
    p.add_argument("--lane", default=None, help="Lane to export (default: main)")
    p.set_defaults(func=cmd_export_git)

    # import-git
    p = sub.add_parser("import-git", help="Import git history into Flanes")
    p.add_argument("source_dir", help="Source git repository directory")
    p.add_argument("--lane", default=None, help="Target lane (default: main)")
    p.set_defaults(func=cmd_import_git)

    # serve
    p = sub.add_parser("serve", help="Start the Flanes REST API server")
    p.add_argument("--port", type=int, default=7654, help="Port (default: 7654)")
    p.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    p.add_argument(
        "--token", default=None,
        help="Bearer token for API auth (or set FLANES_API_TOKEN)",
    )
    p.add_argument(
        "--insecure", action="store_true",
        help="Allow non-loopback binding without auth token",
    )
    p.add_argument("--web", action="store_true", help="Serve web viewer at /web/")
    p.set_defaults(func=cmd_serve)

    # mcp
    p = sub.add_parser("mcp", help="Start MCP tool server on stdio")
    p.set_defaults(func=cmd_mcp)

    # completion
    p = sub.add_parser("completion", help="Generate shell completion script")
    p.add_argument("shell", choices=["bash", "zsh", "fish"])
    p.set_defaults(func=cmd_completion)

    # budget
    p = sub.add_parser("budget", help="Cost budget management")
    budget_sub = p.add_subparsers(dest="budget_command")

    bs = budget_sub.add_parser("show", help="Show budget status for a lane")
    bs.add_argument("lane")
    bs.set_defaults(func=cmd_budget_show)

    bset = budget_sub.add_parser("set", help="Set budget for a lane")
    bset.add_argument("lane")
    bset.add_argument("--max-tokens-in", type=int, default=None)
    bset.add_argument("--max-tokens-out", type=int, default=None)
    bset.add_argument("--max-api-calls", type=int, default=None)
    bset.add_argument("--alert-threshold", type=float, default=None)
    bset.set_defaults(func=cmd_budget_set)

    # template
    p = sub.add_parser("template", help="Workspace template management")
    tmpl_sub = p.add_subparsers(dest="template_command")

    tl = tmpl_sub.add_parser("list", help="List templates")
    tl.set_defaults(func=cmd_template_list)

    tc = tmpl_sub.add_parser("create", help="Create a template")
    tc.add_argument("name")
    tc.add_argument("--description", default=None)
    tc.set_defaults(func=cmd_template_create)

    ts = tmpl_sub.add_parser("show", help="Show template details")
    ts.add_argument("name")
    ts.set_defaults(func=cmd_template_show)

    # evaluate
    p = sub.add_parser("evaluate", help="Run evaluators on a workspace")
    p.add_argument("transition_id", nargs="?", default=None)
    p.add_argument("--workspace", "-w", default=None)
    p.set_defaults(func=cmd_evaluate)

    # semantic-search
    p = sub.add_parser("semantic-search", help="Search intents semantically")
    p.add_argument("query")
    p.add_argument("--limit", "-n", type=int, default=10)
    p.set_defaults(func=cmd_semantic_search)

    # project
    p = sub.add_parser("project", help="Multi-repo project management")
    proj_sub = p.add_subparsers(dest="project_command")

    pi = proj_sub.add_parser("init", help="Initialize a project")
    pi.add_argument("--name", default=None)
    pi.set_defaults(func=cmd_project_init)

    pa = proj_sub.add_parser("add", help="Add a repo to the project")
    pa.add_argument("repo_path")
    pa.add_argument("mount_point")
    pa.add_argument("--lane", default=None)
    pa.set_defaults(func=cmd_project_add)

    ps = proj_sub.add_parser("status", help="Show project status")
    ps.set_defaults(func=cmd_project_status)

    psnap = proj_sub.add_parser("snapshot", help="Snapshot all repos")
    psnap.set_defaults(func=cmd_project_snapshot)

    # remote
    p = sub.add_parser("remote", help="Remote storage operations")
    remote_sub = p.add_subparsers(dest="remote_command")

    rpush = remote_sub.add_parser("push", help="Push objects to remote")
    rpush.add_argument(
        "--metadata", action="store_true", help="Also push lane metadata (transitions, intents)"
    )
    rpush.set_defaults(func=cmd_remote_push)

    rpull = remote_sub.add_parser("pull", help="Pull objects from remote")
    rpull.add_argument(
        "--metadata", action="store_true", help="Also pull lane metadata (transitions, intents)"
    )
    rpull.set_defaults(func=cmd_remote_pull)

    rstat = remote_sub.add_parser("status", help="Show remote sync status")
    rstat.set_defaults(func=cmd_remote_status)

    return parser


def _error_hint(msg: str) -> str | None:
    """Return a hint for common error messages, or None."""
    lower = msg.lower()
    if "workspace" in lower and "not found" in lower:
        return "Hint: Use 'flanes workspace list' to see available workspaces."
    if "lane" in lower and ("not found" in lower or "does not exist" in lower):
        return "Hint: Use 'flanes lanes' to see available lanes."
    if "transition" in lower and "not found" in lower:
        return "Hint: Use 'flanes history' to see recent transitions."
    if "state" in lower and "not found" in lower:
        return "Hint: Use 'flanes history --json' to find valid state IDs."
    return None


_KNOWN_COMMANDS = [
    "init",
    "status",
    "snapshot",
    "commit",
    "propose",
    "accept",
    "reject",
    "history",
    "log",
    "trace",
    "diff",
    "search",
    "semantic-search",
    "lanes",
    "lane",
    "workspace",
    "restore",
    "promote",
    "info",
    "show",
    "evaluate",
    "budget",
    "gc",
    "doctor",
    "serve",
    "mcp",
    "export-git",
    "import-git",
    "remote",
    "project",
    "template",
    "cat-file",
    "completion",
]


def main():
    # Resolve command aliases before parsing
    if len(sys.argv) > 1 and sys.argv[1] in COMMAND_ALIASES:
        sys.argv[1] = COMMAND_ALIASES[sys.argv[1]]

    # Check for "did you mean?" before argparse (which exits with code 2)
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        attempted = sys.argv[1]
        all_names = _KNOWN_COMMANDS + list(COMMAND_ALIASES.keys())
        if attempted not in all_names:
            matches = difflib.get_close_matches(attempted, all_names, n=3, cutoff=0.6)
            if matches:
                print(f"Unknown command: '{attempted}'", file=sys.stderr)
                print(f"  Did you mean: {', '.join(matches)}?", file=sys.stderr)
                sys.exit(1)

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if hasattr(args, "func"):
        try:
            args.func(args)
        except NotARepository as e:
            if getattr(args, "json", False):
                print_json({"error": str(e)})
            else:
                print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            msg = str(e)
            if getattr(args, "json", False):
                print_json({"error": msg})
            else:
                print(f"Error: {msg}", file=sys.stderr)
                hint = _error_hint(msg)
                if hint:
                    print(f"  {hint}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
