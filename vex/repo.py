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

REPO_DIR_NAME = ".vex"


class NotARepository(ValueError):
    """Raised when a command is run outside a Vex repository."""
    def __init__(self, start_path):
        super().__init__(
            f"Not inside a Vex repository (searched from {start_path})\n"
            f"  Run 'vex init' to create one, or use '-C <path>' to specify a directory."
        )


class Repository:
    """
    A Vex repository.

    Stores all data in a .vex directory at the repository root.
    Working directories live in .vex/workspaces/<name>/.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.vex_dir = self.root / REPO_DIR_NAME
        self.db_path = self.vex_dir / "store.db"

        if not self.vex_dir.exists():
            raise ValueError(
                f"Not a Vex repository: {self.root}\n"
                f"Run `vex init` to create one."
            )

        config = self._read_config()
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

        self.store = ContentStore(self.db_path, blob_threshold=blob_threshold, max_blob_size=max_blob_size)
        self.wsm = WorldStateManager(self.store, self.db_path, max_tree_depth=max_tree_depth)
        self.wm = WorkspaceManager(self.vex_dir, self.wsm)

    @classmethod
    def init(cls, path: Path, initial_lane: str = "main") -> "Repository":
        """
        Initialize a new repository.

        Creates the .vex directory, database, initial 'main' lane,
        and a workspace for it. Unlike git, the main workspace IS the
        repo root — files stay in place, no movement needed.

        Feature lanes will get isolated workspaces under .vex/workspaces/.
        """
        root = Path(path).resolve()
        vex_dir = root / REPO_DIR_NAME

        if vex_dir.exists():
            raise ValueError(f"Repository already exists at {root}")

        vex_dir.mkdir(parents=True)
        (vex_dir / "config.json").write_text(json.dumps({
            "version": "0.3.0",  # Bump version for git-style main
            "default_lane": initial_lane,
            "created_at": time.time(),
            "max_blob_size": 100 * 1024 * 1024,  # 100 MB default
            "max_tree_depth": 100,  # 100 levels default
        }, indent=2))

        repo = cls(root)
        repo.wsm.create_lane(initial_lane)

        # If there are existing files, create initial snapshot from repo root.
        # Include dotfiles like .env, .editorconfig — exclude only .vex itself.
        user_files = [
            f for f in root.iterdir()
            if f.name != REPO_DIR_NAME
        ]

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
            repo.wsm.evaluate(tid, EvaluationResult(
                passed=True,
                evaluator="system",
                summary="Initial snapshot accepted",
            ))

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
        existing_lanes = {l["name"] for l in self.wsm.list_lanes()}
        if lane not in existing_lanes:
            base = state_id or self.head()
            self.wsm.create_lane(lane, base)

        if state_id is None:
            state_id = self.head(lane)

        return self.wm.create(name, lane=lane, state_id=state_id, agent_id=agent_id)

    def workspace_remove(self, name: str, force: bool = False):
        """Remove a workspace."""
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
        info = self.wm.get(workspace)
        if info is None:
            raise ValueError(f"Workspace '{workspace}' not found")

        if parent_id is None:
            parent_id = info.base_state or self.head(info.lane)

        return self.wsm.snapshot_directory(info.path, parent_id)

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

        return self.wsm.propose(from_state, to_state, intent, lane, cost)

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
                    "SELECT to_state, intent_id FROM transitions WHERE id = ?",
                    (transition_id,)
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
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to update fork_base after accept", exc_info=True
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
        result = EvaluationResult(
            passed=False,
            evaluator=evaluator,
            checks=checks or {},
            summary=summary,
        )
        return self.wsm.evaluate(transition_id, result)

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
        if auto_accept:
            status = self.accept(tid, evaluator=evaluator, summary="Auto-accepted")
            # Update workspace base state to track the new head
            self.wm._update_meta(workspace, base_state=new_state)

        return {
            "transition_id": tid,
            "from_state": head,
            "to_state": new_state,
            "status": status.value,
        }

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
                workspace, target_lane, target_head, new_state,
                prompt, agent, source_lane, fork_base, tags, auto_accept, evaluator,
            )

        # Snapshot the workspace to capture the agent's current work
        lane_head = self.snapshot(workspace, parent_id=info.base_state)

        # Compute deltas from the fork point
        lane_delta = self.wsm.diff_states(fork_base, lane_head)
        target_delta = self.wsm.diff_states(fork_base, target_head)

        # Detect conflicts: paths touched on both sides
        conflict_info = self._detect_path_conflicts(
            lane_delta, target_delta, fork_base, source_lane, target_lane,
        )

        if conflict_info["has_conflicts"]:
            return conflict_info

        # No conflicts — apply target's delta onto the workspace.
        # This writes only files that changed on the target side.
        # Agent's files (on non-conflicting paths) are untouched.
        self._apply_target_delta(workspace, target_delta, target_head)

        # Snapshot the rebased workspace — target state + agent's changes
        new_state = self.snapshot(workspace, parent_id=target_head)

        return self._finalize_promote(
            workspace, target_lane, target_head, new_state,
            prompt, agent, source_lane, fork_base, tags, auto_accept, evaluator,
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
                "added" if path in lane_delta["added"]
                else "modified" if path in lane_delta["modified"]
                else "removed"
            )
            target_action = (
                "added" if path in target_delta["added"]
                else "modified" if path in target_delta["modified"]
                else "removed"
            )
            conflicts.append({
                "path": path,
                "lane_action": lane_action,
                "target_action": target_action,
            })

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
        return TemplateManager(self.vex_dir)

    # ── Evaluator Operations ──────────────────────────────────────

    def run_evaluators(self, workspace: str):
        """Load evaluator config and run all evaluators on a workspace."""
        from .evaluators import load_evaluators, run_all_evaluators

        config_path = self.vex_dir / "config.json"
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

        config_path = self.vex_dir / "config.json"
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
                results.append({
                    "intent_id": intent_id,
                    "prompt": intent.prompt,
                    "score": score,
                    "tags": intent.tags,
                })
        return results

    # ── Remote Operations ─────────────────────────────────────────

    def get_remote_sync_manager(self):
        """Create a RemoteSyncManager from config."""
        from .remote import RemoteSyncManager, create_backend

        config_path = self.vex_dir / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        if "remote_storage" not in config:
            raise ValueError("No remote storage configured in config.json")

        backend = create_backend(config)
        cache_dir = self.vex_dir / "remote_cache"
        return RemoteSyncManager(self.store, backend, cache_dir)

    # ── Helpers ───────────────────────────────────────────────────

    def _read_config(self) -> dict:
        """Read repository configuration."""
        config_path = self.vex_dir / "config.json"
        if config_path.exists():
            return json.loads(config_path.read_text())
        return {}

    def _default_lane(self) -> str:
        config_path = self.vex_dir / "config.json"
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
        self.store.close()
