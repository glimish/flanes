"""
Repository

The high-level API that agents and the CLI interact with.
This ties together the content store, world state manager,
and workspace manager into a clean interface.

Workspace-aware design:
    The repo root is NOT a working directory. Agents never modify
    files in the repo root directly. Instead, each lane gets its
    own workspace — a physically isolated directory materialized
    from the CAS.

    repo = Repository.init("/path/to/project")

    # Create a workspace for a lane
    ws = repo.workspace_create("feature-auth", lane="feature-auth")

    # Agent modifies files in ws.path (not repo root)
    # ...

    # Snapshot the workspace and propose
    state = repo.snapshot("feature-auth")
    tid = repo.propose(state, new_state, prompt="Add auth", agent=agent)

Design philosophy: Make the right thing easy and the wrong thing hard.
Agents get isolated directories. There's no way to accidentally
modify another agent's work.
"""

import json
import logging
import os
import platform
import tempfile
import time
import uuid
from pathlib import Path

from .budgets import (
    BudgetConfig,
    check_budget,
    compute_budget_status,
    set_lane_budget,
)
from .cas import ContentStore
from .gc import GCResult, collect_garbage
from .state import (
    AgentIdentity,
    CostRecord,
    EvaluationResult,
    Intent,
    TransitionStatus,
    WorldStateManager,
)
from .workspace import WorkspaceInfo, WorkspaceManager

logger = logging.getLogger(__name__)

REPO_DIR_NAME = ".fla"

# Current config version — bump when the config schema changes
CONFIG_VERSION = "0.3.0"

# Known config keys for validation
KNOWN_CONFIG_KEYS = frozenset(
    {
        "version",
        "default_lane",
        "created_at",
        "max_blob_size",
        "max_tree_depth",
        "blob_threshold",
        "evaluators",
        "remote_storage",
        "embedding_api_url",
        "embedding_api_key",
        "embedding_model",
        "embedding_dimensions",
        "api_token",
        "git_coexistence",
    }
)


class NotARepository(ValueError):  # noqa: N818
    """Raised when a command is run outside a Fla repository."""

    def __init__(self, start_path):
        super().__init__(
            f"Not inside a Fla repository (searched from {start_path})\n"
            f"  Run 'fla init' to create one, or use '-C <path>' to specify a directory."
        )


class ConcurrentAccessError(ValueError):  # noqa: N818
    """Raised when another machine is accessing the repository via shared filesystem."""

    def __init__(self, lock_info: dict):
        hostname = lock_info.get("hostname", "unknown")
        pid = lock_info.get("pid", "?")
        super().__init__(
            f"Another machine is accessing this repository "
            f"(host={hostname}, pid={pid}).\n"
            f"  SQLite does not support concurrent access over NFS/shared filesystems.\n"
            f"  Use 'fla remote push/pull' for multi-machine collaboration."
        )
        self.lock_info = lock_info


# Maximum age (seconds) before a lock is considered stale
_LOCK_STALE_AGE = 4 * 3600  # 4 hours


class Repository:
    """
    A Fla repository.

    Stores all data in a .fla directory at the repository root.
    Working directories live in .fla/workspaces/<name>/.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.fla_dir = self.root / REPO_DIR_NAME
        self.db_path = self.fla_dir / "store.db"

        if not self.fla_dir.exists():
            raise ValueError(f"Not a Fla repository: {self.root}\nRun `fla init` to create one.")

        config = self._read_config()
        self._validate_config(config)

        blob_threshold = config.get("blob_threshold", 0)
        max_blob_size = config.get("max_blob_size", 0)
        max_tree_depth = config.get("max_tree_depth", 0)

        # Validate limits - reject negative values
        if max_blob_size < 0:
            raise ValueError(
                f"Invalid config: max_blob_size must be >= 0, got {max_blob_size}\n"
                f"  Use 0 for default limit ({ContentStore.DEFAULT_MAX_BLOB_SIZE} bytes)\n"
                f"  Set to very large value (e.g., 10**12) for effectively unlimited"
            )
        if max_tree_depth < 0:
            raise ValueError(
                f"Invalid config: max_tree_depth must be >= 0, got {max_tree_depth}\n"
                f"  Use 0 for default limit ({WorldStateManager.DEFAULT_MAX_TREE_DEPTH} levels)\n"
                f"  Set to very large value (e.g., 10000) for effectively unlimited"
            )

        self.store = ContentStore(
            self.db_path, blob_threshold=blob_threshold, max_blob_size=max_blob_size
        )
        self.wsm = WorldStateManager(self.store, self.db_path, max_tree_depth=max_tree_depth)
        self.wm = WorkspaceManager(self.fla_dir, self.wsm)
        self._hooks = None  # Lazy-loaded plugin hooks

        # NFS safety: acquire instance lock
        self._lock_path = self.fla_dir / "instance.lock"
        self._machine_id = self._get_machine_id()
        self._acquire_instance_lock()

    # Template for .flaignore file created on init
    FLAIGNORE_TEMPLATE = """\
# Fla ignore patterns (like .gitignore)
# Lines starting with # are comments
# Patterns ending with / match directories only
# Patterns starting with ! negate a previous match

# Environment files (uncomment as needed)
# .env
# .env.*

# Credentials (uncomment as needed)
# *.pem
# *.key
# credentials.json

# Build artifacts
# dist/
# build/
# *.pyc

# Logs
# *.log
# logs/
"""

    @classmethod
    def init(cls, path: Path, initial_lane: str = "main") -> "Repository":
        """
        Initialize a new repository.

        Creates the .fla directory, database, initial 'main' lane,
        and a workspace for it. Unlike git, the main workspace IS the
        repo root — files stay in place, no movement needed.

        Feature lanes will get isolated workspaces under .fla/workspaces/.
        """
        root = Path(path).resolve()
        fla_dir = root / REPO_DIR_NAME

        if fla_dir.exists():
            raise ValueError(f"Repository already exists at {root}")

        fla_dir.mkdir(parents=True)
        git_detected = (root / ".git").exists()
        config_data = {
            "version": CONFIG_VERSION,
            "default_lane": initial_lane,
            "created_at": time.time(),
            "max_blob_size": 100 * 1024 * 1024,  # 100 MB default
            "max_tree_depth": 100,  # 100 levels default
        }
        if git_detected:
            config_data["git_coexistence"] = True
        (fla_dir / "config.json").write_text(json.dumps(config_data, indent=2))

        # Auto-create .flaignore if it doesn't exist
        flaignore_path = root / ".flaignore"
        if not flaignore_path.exists():
            flaignore_path.write_text(cls.FLAIGNORE_TEMPLATE)

        repo = cls(root)
        repo.wsm.create_lane(initial_lane)

        # If there are existing files, create initial snapshot from repo root.
        # Include dotfiles like .env, .editorconfig — exclude only .fla itself.
        user_files = [f for f in root.iterdir() if f.name != REPO_DIR_NAME]

        if user_files:
            state_id = repo.wsm.snapshot_directory(root, parent_id=None)
            agent = AgentIdentity(agent_id="system", agent_type="init")
            intent = Intent(
                id=str(uuid.uuid4()),
                prompt="Initial snapshot",
                agent=agent,
                tags=["init"],
            )
            tid = repo.wsm.propose(
                from_state=None,
                to_state=state_id,
                intent=intent,
                lane=initial_lane,
            )
            repo.wsm.evaluate(
                tid,
                EvaluationResult(
                    passed=True,
                    evaluator="system",
                    summary="Initial snapshot accepted",
                ),
            )

            # Create main workspace metadata pointing to repo root
            # Files stay in place — no movement needed (git-style)
            repo.wm.create(initial_lane, lane=initial_lane, state_id=None)
            repo.wm._update_meta(initial_lane, base_state=state_id)
        else:
            # Empty repo — create workspace metadata (repo root already exists)
            repo.wm.create(initial_lane, lane=initial_lane, state_id=None)

        return repo

    # ── Workspace Operations ──────────────────────────────────────

    def workspace_create(
        self,
        name: str,
        lane: str | None = None,
        state_id: str | None = None,
        agent_id: str | None = None,
    ) -> WorkspaceInfo:
        """
        Create a new workspace.

        If lane is not specified, uses the workspace name as the lane name.
        If state_id is not specified, uses the lane's current head.
        If the lane doesn't exist yet, creates it.
        """
        lane = lane or name

        # Ensure lane exists
        existing_lanes = {ln["name"] for ln in self.wsm.list_lanes()}
        if lane not in existing_lanes:
            base = state_id or self.head()
            self.wsm.create_lane(lane, base)

        if state_id is None:
            state_id = self.head(lane)

        return self.wm.create(name, lane=lane, state_id=state_id, agent_id=agent_id)

    def workspace_remove(self, name: str, force: bool = False):
        """Remove a workspace."""
        logger.info("Removing workspace '%s' (force=%s)", name, force)
        self.wm.remove(name, force=force)

    def workspace_update(self, name: str, state_id: str | None = None) -> dict:
        """
        Update a workspace to a new state (smart incremental update).

        If state_id is not specified, updates to the lane's current head.
        """
        if state_id is None:
            info = self.wm.get(name)
            if info is None:
                raise ValueError(f"Workspace '{name}' not found")
            state_id = self.head(info.lane)
            if state_id is None:
                raise ValueError(f"Lane '{info.lane}' has no head state")

        return self.wm.update(name, state_id)

    def workspace_path(self, name: str) -> Path | None:
        """Get the filesystem path for a workspace."""
        info = self.wm.get(name)
        return info.path if info else None

    def workspace_acquire(self, name: str, agent_id: str) -> bool:
        """Acquire exclusive lock on a workspace."""
        return self.wm.acquire(name, agent_id)

    def workspace_release(self, name: str):
        """Release lock on a workspace."""
        self.wm.release(name)

    def workspaces(self) -> list[WorkspaceInfo]:
        """List all workspaces."""
        return self.wm.list()

    # ── Core Operations ───────────────────────────────────────────

    def snapshot(self, workspace: str, parent_id: str | None = None) -> str:
        """
        Snapshot a workspace — capture its current files into the CAS.

        This replaces the old snapshot() that operated on the repo root.
        Now it targets a specific workspace, ensuring isolation.
        """
        self.verify_instance_lock()
        info = self.wm.get(workspace)
        if info is None:
            raise ValueError(f"Workspace '{workspace}' not found")

        if parent_id is None:
            parent_id = info.base_state or self.head(info.lane)

        return self.wsm.snapshot_directory(info.path, parent_id)

    def _fire_hooks(self, event: str, context: dict) -> None:
        """Fire lifecycle hooks for a given event.

        Hooks are discovered via the ``fla.hooks`` entry point group.
        Each hook is called with the event name and a context dict.
        Hook failures are logged but never block the operation.
        """
        if self._hooks is None:
            from .plugins import discover_hooks

            self._hooks = discover_hooks()

        for name, hook_fn in self._hooks.items():
            try:
                hook_fn(event, context)
            except Exception:
                logger.warning(
                    "Hook %s failed for event %s",
                    name,
                    event,
                    exc_info=True,
                )

    def propose(
        self,
        from_state: str | None,
        to_state: str,
        prompt: str,
        agent: AgentIdentity,
        lane: str | None = None,
        tags: list[str] | None = None,
        cost: CostRecord | None = None,
        context_refs: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        """
        Propose a transition from one state to another.

        This is the primary agent-facing API. The agent says:
        "I was working from state X, I produced state Y,
        here's why I did it (prompt), and here's what it cost."
        """
        self.verify_instance_lock()
        intent = Intent(
            id=str(uuid.uuid4()),
            prompt=prompt,
            agent=agent,
            context_refs=context_refs or [],
            tags=tags or [],
            metadata=metadata or {},
        )

        lane = lane or self._default_lane()

        # Budget check — raise BudgetError if limit exceeded
        additional = cost.to_dict() if cost else None
        budget_status = check_budget(self.wsm, lane, additional_cost=additional)
        if budget_status and budget_status.warnings:
            import sys

            for w in budget_status.warnings:
                print(f"Budget warning ({lane}): {w} approaching limit", file=sys.stderr)

        self._fire_hooks(
            "pre_propose",
            {
                "lane": lane,
                "from_state": from_state,
                "to_state": to_state,
                "prompt": prompt,
                "agent": agent.to_dict(),
            },
        )
        tid = self.wsm.propose(from_state, to_state, intent, lane, cost)
        self._fire_hooks(
            "post_propose",
            {
                "lane": lane,
                "transition_id": tid,
                "from_state": from_state,
                "to_state": to_state,
            },
        )
        return tid

    def accept(
        self,
        transition_id: str,
        evaluator: str = "manual",
        summary: str = "",
        checks: dict[str, bool] | None = None,
    ) -> TransitionStatus:
        """Accept a proposed transition (evaluation passed).

        If the transition is a promote (intent tagged ``"promote"``),
        also updates the source lane's fork_base so subsequent promotes
        compute deltas correctly.
        """
        self.verify_instance_lock()
        self._fire_hooks("pre_accept", {"transition_id": transition_id})
        result = EvaluationResult(
            passed=True,
            evaluator=evaluator,
            checks=checks or {},
            summary=summary,
        )
        status = self.wsm.evaluate(transition_id, result)

        if status == TransitionStatus.ACCEPTED:
            # Check if this was a promote — update source lane's fork_base.
            # This runs in its own transaction. If it fails or is interrupted,
            # the consequence is a stale fork_base (promote recomputes a larger
            # delta), not data corruption.
            try:
                row = self.wsm.conn.execute(
                    "SELECT to_state, intent_id FROM transitions WHERE id = ?", (transition_id,)
                ).fetchone()
                if row:
                    to_state = row[0]
                    intent = self.wsm.get_intent(row[1])
                    if intent and "promote" in intent.tags:
                        for tag in intent.tags:
                            if tag.startswith("from:"):
                                source_lane = tag[5:]
                                self.wsm.conn.execute(
                                    "UPDATE lanes SET fork_base = ? WHERE name = ?",
                                    (to_state, source_lane),
                                )
                                self.wsm.conn.commit()
                                break
            except Exception:
                logger.warning("Failed to update fork_base after accept", exc_info=True)

        self._fire_hooks(
            "post_accept",
            {
                "transition_id": transition_id,
                "status": status.value,
            },
        )
        return status

    def reject(
        self,
        transition_id: str,
        evaluator: str = "manual",
        summary: str = "",
        checks: dict[str, bool] | None = None,
    ) -> TransitionStatus:
        """Reject a proposed transition (evaluation failed)."""
        self.verify_instance_lock()
        self._fire_hooks("pre_reject", {"transition_id": transition_id})
        result = EvaluationResult(
            passed=False,
            evaluator=evaluator,
            checks=checks or {},
            summary=summary,
        )
        status = self.wsm.evaluate(transition_id, result)
        self._fire_hooks(
            "post_reject",
            {
                "transition_id": transition_id,
                "status": status.value,
            },
        )
        return status

    def quick_commit(
        self,
        workspace: str,
        prompt: str,
        agent: AgentIdentity,
        lane: str | None = None,
        tags: list[str] | None = None,
        cost: CostRecord | None = None,
        auto_accept: bool = False,
        evaluator: str = "auto",
    ) -> dict:
        """
        Convenience: snapshot workspace + propose (+ optionally accept).

        When auto_accept=True, evaluators are still run but failures
        only produce warnings (they don't block the accept). This ensures
        evaluation data is captured even for auto-accepted commits.
        """
        info = self.wm.get(workspace)
        if info is None:
            raise ValueError(f"Workspace '{workspace}' not found")

        lane = lane or info.lane
        head = self.head(lane)
        new_state = self.snapshot(workspace, parent_id=head)

        tid = self.propose(
            from_state=head,
            to_state=new_state,
            prompt=prompt,
            agent=agent,
            lane=lane,
            tags=tags,
            cost=cost,
        )

        status = TransitionStatus.PROPOSED
        eval_result = None
        eval_summary = "Auto-accepted"

        if auto_accept:
            # Run evaluators even with auto-accept to capture evaluation data
            try:
                eval_result = self.run_evaluators(workspace)
                if eval_result and not eval_result.passed:
                    logger.warning(
                        "Evaluators failed but auto-accepting: %s",
                        eval_result.summary,
                    )
                    eval_summary = f"Auto-accepted (eval failed: {eval_result.summary})"
                elif eval_result and eval_result.passed:
                    eval_summary = f"Auto-accepted (eval passed: {eval_result.summary})"
            except Exception as e:
                logger.warning("Evaluator error during auto-accept: %s", e)
                eval_summary = f"Auto-accepted (eval error: {e})"

            status = self.accept(tid, evaluator=evaluator, summary=eval_summary)
            # Update workspace base state to track the new head
            self.wm._update_meta(workspace, base_state=new_state)

        result = {
            "transition_id": tid,
            "from_state": head,
            "to_state": new_state,
            "status": status.value,
        }

        # Include evaluation info if we ran evaluators
        if eval_result:
            result["evaluation"] = {
                "passed": eval_result.passed,
                "summary": eval_result.summary,
                "checks": eval_result.checks,
            }

        return result

    # ── Lane Operations ───────────────────────────────────────────

    def create_lane(
        self,
        name: str,
        base: str | None = None,
        create_workspace: bool = True,
    ) -> str:
        """
        Create a new lane, optionally with an associated workspace.

        If base is None, forks from the current main lane head.
        By default, also creates a workspace for the new lane.
        """
        if base is None:
            base = self.head()
        self.wsm.create_lane(name, base)

        if create_workspace and base:
            self.wm.create(name, lane=name, state_id=base)

        return name

    def lanes(self) -> list[dict]:
        return self.wsm.list_lanes()

    # ── Query Operations ──────────────────────────────────────────

    def head(self, lane: str | None = None) -> str | None:
        """Get the current head state of a lane."""
        lane = lane or self._default_lane()
        return self.wsm.get_lane_head(lane)

    def history(
        self,
        lane: str | None = None,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict]:
        """Get transition history for a lane."""
        lane = lane or self._default_lane()
        status_filter = TransitionStatus(status) if status else None
        return self.wsm.history(lane, limit, status_filter)

    def trace(self, state_id: str | None = None, max_depth: int = 50) -> list[dict]:
        """Trace the causal lineage of a state."""
        state_id = state_id or self.head()
        if state_id is None:
            return []
        return self.wsm.trace(state_id, max_depth)

    def diff(self, state_a: str, state_b: str) -> dict:
        """Diff two world states."""
        return self.wsm.diff_states(state_a, state_b)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search intents by text."""
        return self.wsm.search_intents(query, limit)

    def status(self) -> dict:
        """Get repository status."""
        lane_list = self.lanes()
        head = self.head()
        pending = self.history(status="proposed")
        ws_list = self.workspaces()

        return {
            "root": str(self.root),
            "lanes": lane_list,
            "current_head": head,
            "pending_proposals": len(pending),
            "workspaces": [w.to_dict() for w in ws_list],
            "storage": self.store.stats(),
        }

    # ── Promote ────────────────────────────────────────────────────

    def promote(
        self,
        workspace: str,
        target_lane: str | None = None,
        prompt: str | None = None,
        agent: AgentIdentity | None = None,
        tags: list[str] | None = None,
        auto_accept: bool = False,
        evaluator: str = "auto",
        force: bool = False,
    ) -> dict:
        """
        Promote a workspace's work into a target lane.

        Contract:
            Inputs:
                lane_head   = snapshot of workspace (the agent's work)
                target_head = current head of target lane
                fork_base   = recorded at lane creation (not computed)

            Compute:
                lane_delta   = diff(fork_base → lane_head)
                target_delta = diff(fork_base → target_head)

            Conflicts:
                conflicts = changed_paths(lane_delta) ∩ changed_paths(target_delta)

            If conflicts: return them, stop.
            Else:
                Apply target_delta onto workspace (write only target-changed files)
                Snapshot → propose into target lane (from=target_head, to=new_state)

        This is NOT a merge. No three-way content resolution ever happens.
        The orchestrator decides how to handle conflicts.

        Returns:
            {"status": "conflicts", "conflicts": [...]}  — if paths collide
            {"status": "proposed"/"accepted", ...}        — if clean
        """
        info = self.wm.get(workspace)
        if info is None:
            raise ValueError(f"Workspace '{workspace}' not found")

        target_lane = target_lane or self._default_lane()
        source_lane = info.lane
        target_head = self.head(target_lane)

        if target_head is None:
            raise ValueError(f"Target lane '{target_lane}' has no head state")

        # Get fork_base from lane metadata — recorded at creation, no graph walk
        fork_base = self.wsm.get_lane_fork_base(source_lane)
        if fork_base is None:
            raise ValueError(
                f"Lane '{source_lane}' has no fork_base. "
                f"Cannot promote — was this lane created before fork_base tracking?"
            )

        # Fast path: if target hasn't moved since fork, no rebase needed
        if fork_base == target_head:
            new_state = self.snapshot(workspace, parent_id=target_head)
            return self._finalize_promote(
                workspace,
                target_lane,
                target_head,
                new_state,
                prompt,
                agent,
                source_lane,
                fork_base,
                tags,
                auto_accept,
                evaluator,
            )

        # Snapshot the workspace to capture the agent's current work
        lane_head = self.snapshot(workspace, parent_id=info.base_state)

        # Compute deltas from the fork point
        lane_delta = self.wsm.diff_states(fork_base, lane_head)
        target_delta = self.wsm.diff_states(fork_base, target_head)

        # Detect conflicts: paths touched on both sides
        conflict_info = self._detect_path_conflicts(
            lane_delta,
            target_delta,
            fork_base,
            source_lane,
            target_lane,
        )

        if conflict_info["has_conflicts"] and not force:
            return conflict_info

        # If force=True, we ignore conflicts and just use lane's version
        # (the agent's work overwrites target changes on conflicting paths)

        # No conflicts — apply target's delta onto the workspace.
        # This writes only files that changed on the target side.
        # Agent's files (on non-conflicting paths) are untouched.
        self._apply_target_delta(workspace, target_delta, target_head)

        # Snapshot the rebased workspace — target state + agent's changes
        new_state = self.snapshot(workspace, parent_id=target_head)

        return self._finalize_promote(
            workspace,
            target_lane,
            target_head,
            new_state,
            prompt,
            agent,
            source_lane,
            fork_base,
            tags,
            auto_accept,
            evaluator,
        )

    def _detect_path_conflicts(
        self,
        lane_delta: dict,
        target_delta: dict,
        fork_base: str,
        source_lane: str,
        target_lane: str,
    ) -> dict:
        """Detect file-level conflicts between two deltas from the same fork point."""
        lane_touched = (
            set(lane_delta["added"].keys())
            | set(lane_delta["modified"].keys())
            | set(lane_delta["removed"].keys())
        )
        target_touched = (
            set(target_delta["added"].keys())
            | set(target_delta["modified"].keys())
            | set(target_delta["removed"].keys())
        )

        conflicting_paths = sorted(lane_touched & target_touched)

        if not conflicting_paths:
            return {"has_conflicts": False}

        conflicts = []
        for path in conflicting_paths:
            lane_action = (
                "added"
                if path in lane_delta["added"]
                else "modified"
                if path in lane_delta["modified"]
                else "removed"
            )
            target_action = (
                "added"
                if path in target_delta["added"]
                else "modified"
                if path in target_delta["modified"]
                else "removed"
            )
            conflicts.append(
                {
                    "path": path,
                    "lane_action": lane_action,
                    "target_action": target_action,
                }
            )

        return {
            "status": "conflicts",
            "has_conflicts": True,
            "source_lane": source_lane,
            "target_lane": target_lane,
            "fork_base": fork_base,
            "conflicts": conflicts,
            "lane_only": sorted(lane_touched - target_touched),
            "target_only": sorted(target_touched - lane_touched),
        }

    def _apply_target_delta(self, workspace: str, target_delta: dict, target_head: str):
        """Apply target lane's changes onto a workspace (non-conflicting rebase)."""
        target_state = self.wsm.get_state(target_head)
        if target_state is None:
            raise ValueError(f"Target state not found: {target_head}")
        target_files = self.wsm._flatten_tree(target_state["root_tree"])
        info = self.wm.get(workspace)
        if info is None:
            raise ValueError(f"Workspace '{workspace}' not found")
        ws_path = info.path

        # Remove files the target deleted
        for path in target_delta["removed"]:
            fp = ws_path / path
            if fp.exists():
                fp.unlink()

        # Write files the target added or modified
        for path in list(target_delta["added"].keys()) + list(target_delta["modified"].keys()):
            blob_hash = target_files.get(path)
            if blob_hash is None:
                continue
            obj = self.store.retrieve(blob_hash)
            if obj is None:
                continue
            fp = ws_path / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(obj.data)

        # Update workspace base to reflect new target head
        self.wm._update_meta(workspace, base_state=target_head)

    def _finalize_promote(
        self,
        workspace: str,
        target_lane: str,
        target_head: str,
        new_state: str,
        prompt: str | None,
        agent: AgentIdentity | None,
        source_lane: str,
        fork_base: str,
        tags: list[str] | None,
        auto_accept: bool,
        evaluator: str,
    ) -> dict:
        """Finalize a promote by proposing (and optionally accepting) into the target lane."""
        agent = agent or AgentIdentity(agent_id="system", agent_type="promote")
        prompt = prompt or f"Promote work from '{source_lane}' into '{target_lane}'"
        tags = (tags or []) + ["promote", f"from:{source_lane}"]

        tid = self.propose(
            from_state=target_head,
            to_state=new_state,
            prompt=prompt,
            agent=agent,
            lane=target_lane,
            tags=tags,
        )

        status = TransitionStatus.PROPOSED
        if auto_accept:
            status = self.accept(tid, evaluator=evaluator, summary=f"Promoted from {source_lane}")
            self.wm._update_meta(workspace, base_state=new_state)

        return {
            "status": status.value,
            "transition_id": tid,
            "from_state": target_head,
            "to_state": new_state,
            "workspace": workspace,
            "source_lane": source_lane,
            "target_lane": target_lane,
            "fork_base": fork_base,
        }

    # ── Restore ───────────────────────────────────────────────────

    def restore(self, workspace: str, state_id: str):
        """
        Restore a workspace to a specific world state.

        Uses smart incremental update — only writes files that differ
        between the workspace's current state and the target state.
        """
        return self.wm.update(workspace, state_id)

    # ── Garbage Collection ─────────────────────────────────────────

    def gc(self, dry_run: bool = True, max_age_days: int = 30) -> GCResult:
        """Run garbage collection on the repository."""
        return collect_garbage(self.store, self.wsm, dry_run=dry_run, max_age_days=max_age_days)

    # ── Budget Operations ─────────────────────────────────────────

    def set_budget(self, lane: str, **kwargs) -> None:
        """Set a budget on a lane."""
        config = BudgetConfig(**kwargs)
        set_lane_budget(self.wsm, lane, config)

    def get_budget_status(self, lane: str):
        """Get budget status for a lane."""
        return compute_budget_status(self.wsm, lane)

    # ── Template Operations ───────────────────────────────────────

    def get_template_manager(self):
        from .templates import TemplateManager

        return TemplateManager(self.fla_dir)

    # ── Evaluator Operations ──────────────────────────────────────

    def run_evaluators(self, workspace: str):
        """Load evaluator config and run all evaluators on a workspace."""
        from .evaluators import load_evaluators, run_all_evaluators

        config_path = self.fla_dir / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        evaluators = load_evaluators(config)
        if not evaluators:
            return EvaluationResult(
                passed=True,
                evaluator="plugin_runner",
                summary="No evaluators configured",
            )

        ws_info = self.wm.get(workspace)
        if ws_info is None:
            raise ValueError(f"Workspace '{workspace}' not found")

        return run_all_evaluators(evaluators, ws_info.path)

    def evaluate_transition(self, transition_id: str, workspace: str):
        """Run evaluators and apply result to a transition."""
        result = self.run_evaluators(workspace)
        return self.wsm.evaluate(transition_id, result)

    # ── Semantic Search ───────────────────────────────────────────

    def semantic_search(self, query: str, limit: int = 10) -> list:
        """Search intents semantically. Falls back to text search if no API configured."""
        from .embeddings import (
            bytes_to_embedding,
            cosine_similarity,
            get_embedding_client,
        )

        config_path = self.fla_dir / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        client = get_embedding_client(config)

        if client is None:
            return self.search(query, limit)

        query_embedding = client.embed_single(query)
        all_embeddings = self.wsm.all_embeddings()

        if not all_embeddings:
            return self.search(query, limit)

        scored = []
        for intent_id, emb_bytes in all_embeddings:
            stored_emb = bytes_to_embedding(emb_bytes)
            score = cosine_similarity(query_embedding, stored_emb)
            scored.append((score, intent_id))

        scored.sort(reverse=True)
        top = scored[:limit]

        results = []
        for score, intent_id in top:
            intent = self.wsm.get_intent(intent_id)
            if intent:
                results.append(
                    {
                        "intent_id": intent_id,
                        "prompt": intent.prompt,
                        "score": score,
                        "tags": intent.tags,
                    }
                )
        return results

    # ── Remote Operations ─────────────────────────────────────────

    def get_remote_sync_manager(self):
        """Create a RemoteSyncManager from config."""
        from .remote import RemoteSyncManager, create_backend

        config_path = self.fla_dir / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        if "remote_storage" not in config:
            raise ValueError("No remote storage configured in config.json")

        backend = create_backend(config)
        cache_dir = self.fla_dir / "remote_cache"
        return RemoteSyncManager(self.store, backend, cache_dir)

    # ── Helpers ───────────────────────────────────────────────────

    def _read_config(self) -> dict:
        """Read repository configuration."""
        config_path = self.fla_dir / "config.json"
        if config_path.exists():
            return json.loads(config_path.read_text())
        return {}

    @staticmethod
    def _validate_config(config: dict) -> None:
        """Validate config version and warn on unknown keys."""
        repo_version = config.get("version")
        if repo_version:
            # Refuse to open repos from a future version
            if repo_version > CONFIG_VERSION:
                raise ValueError(
                    f"Repository config version {repo_version} is newer than "
                    f"this version of Fla ({CONFIG_VERSION}). "
                    f"Please upgrade Fla to open this repository."
                )
            # Run migrations for older versions
            if repo_version < CONFIG_VERSION:
                logger.info(
                    "Repository config version %s is older than current %s",
                    repo_version,
                    CONFIG_VERSION,
                )

        # Warn on unknown keys (don't reject — forward compatibility)
        unknown_keys = set(config.keys()) - KNOWN_CONFIG_KEYS
        if unknown_keys:
            logger.warning("Unknown config keys (ignored): %s", ", ".join(sorted(unknown_keys)))

    def _default_lane(self) -> str:
        config_path = self.fla_dir / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            return config.get("default_lane", "main")
        return "main"

    @classmethod
    def find(cls, start_path: Path | None = None) -> "Repository":
        """Find a repository by walking up from the given path."""
        path = (start_path or Path.cwd()).resolve()
        # Check the path itself and then walk up parents
        while True:
            if (path / REPO_DIR_NAME).exists():
                return cls(path)
            parent = path.parent
            if parent == path:
                break
            path = parent
        raise NotARepository(start_path or Path.cwd())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._release_instance_lock()
        self.store.close()

    # ── NFS Safety: Instance Lock ─────────────────────────────────

    @staticmethod
    def _get_machine_id() -> str:
        """Get a unique machine identifier (MAC-based via uuid.getnode)."""
        return str(uuid.getnode())

    def _acquire_instance_lock(self) -> None:
        """Write an instance lock file for NFS safety.

        If a lock exists from another machine and isn't stale, raises
        ConcurrentAccessError. Same-machine access is allowed since
        SQLite WAL mode handles local concurrency safely. Stale locks
        (dead PID on different host, or older than 4 hours) are reclaimed.
        """
        if self._lock_path.exists():
            try:
                existing = json.loads(self._lock_path.read_text())
            except (json.JSONDecodeError, OSError):
                existing = None

            if existing and not self._is_lock_stale(existing):
                # Lock held by another machine — reject
                if existing.get("machine_id") != self._machine_id:
                    raise ConcurrentAccessError(existing)
                # Same machine — safe (SQLite WAL handles concurrent local access)
                return

        # Write our lock (best-effort, tolerate races on same machine)
        lock_data = {
            "hostname": platform.node(),
            "pid": os.getpid(),
            "machine_id": self._machine_id,
            "started_at": time.time(),
        }
        try:
            self._write_lock_atomic(lock_data)
        except OSError:
            # Race with another process on same machine — acceptable
            pass

    def _release_instance_lock(self) -> None:
        """Remove the instance lock if it's ours."""
        try:
            if self._lock_path.exists():
                existing = json.loads(self._lock_path.read_text())
                if (
                    existing.get("machine_id") == self._machine_id
                    and existing.get("pid") == os.getpid()
                ):
                    self._lock_path.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            # Best-effort cleanup
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _is_lock_stale(self, lock_info: dict) -> bool:
        """Check if a lock file is stale (reclaimable).

        A lock is stale if:
        - The PID is dead on the same hostname
        - The lock is older than _LOCK_STALE_AGE (4 hours)
        """
        age = time.time() - lock_info.get("started_at", 0)
        if age > _LOCK_STALE_AGE:
            logger.info("Reclaiming stale lock (age=%.0fs)", age)
            return True

        # If same hostname, check if PID is alive
        if lock_info.get("hostname") == platform.node():
            pid = lock_info.get("pid")
            if pid and not self._pid_alive(pid):
                logger.info("Reclaiming lock from dead process (pid=%s)", pid)
                return True

        return False

    def _write_lock_atomic(self, data: dict) -> None:
        """Write lock file atomically via tempfile + rename."""
        content = json.dumps(data, indent=2).encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(dir=str(self.fla_dir), prefix=".instance.lock.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
            Path(tmp_path).replace(self._lock_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but owned by different user
            return True
        except OSError:
            return False

    def verify_instance_lock(self) -> None:
        """Verify our instance lock is still valid.

        Call before write operations to detect if another machine
        has taken over. Raises ConcurrentAccessError if the lock
        is no longer ours.
        """
        if not self._lock_path.exists():
            # Lock was removed externally — reclaim it
            self._acquire_instance_lock()
            return

        try:
            current = json.loads(self._lock_path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupt lock — reclaim
            self._acquire_instance_lock()
            return

        if current.get("machine_id") != self._machine_id or current.get("pid") != os.getpid():
            raise ConcurrentAccessError(current)
