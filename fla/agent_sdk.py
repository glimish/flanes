"""
Agent SDK

A clean Python API for AI agents to interact with Fla.
Workspace-aware: each agent gets an isolated working directory.

    from fla.agent_sdk import AgentSession

    session = AgentSession(
        repo_path="/path/to/project",
        agent_id="coder-1",
        agent_type="coder",
        model="claude-sonnet-4-20250514",
    )

    # Get an isolated workspace — physically separate directory
    with session.work("Refactor auth module", tags=["auth"]) as w:
        # w.path is the workspace directory — modify files here
        (w.path / "lib" / "auth.py").write_text("...")
        w.record_tokens(tokens_in=2000, tokens_out=1200)

    # On exit: snapshots workspace, proposes transition, handles errors

The SDK handles workspace creation, locking, snapshotting, and
cleanup so agents only think about modifying files.
"""

import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

from .repo import Repository  # noqa: E402
from .state import AgentIdentity, CostRecord, TransitionStatus  # noqa: E402


class AgentSession:
    """
    A session represents a single agent's interaction with a repository.

    Each session operates in a workspace — a physically isolated
    directory. Multiple AgentSessions can run concurrently without
    interfering with each other, as long as they use different
    workspaces (which they should — one workspace per lane/agent).
    """

    def __init__(
        self,
        repo_path: str | Path,
        agent_id: str,
        agent_type: str,
        model: str | None = None,
        lane: str | None = None,
        workspace: str | None = None,
        session_id: str | None = None,
    ):
        self.repo = Repository.find(Path(repo_path))
        self.agent = AgentIdentity(
            agent_id=agent_id,
            agent_type=agent_type,
            model=model,
            session_id=session_id or str(uuid.uuid4()),
        )
        self.lane = lane or "main"
        self.workspace_name = workspace or f"{self.lane}"
        self.base_state: str | None = None
        self._start_time: float | None = None
        self._token_count_in: int = 0
        self._token_count_out: int = 0
        self._api_calls: int = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        """Close the underlying repository connection."""
        self.repo.close()

    @property
    def workspace_path(self) -> Path | None:
        """Get the filesystem path of the current workspace."""
        return self.repo.workspace_path(self.workspace_name)

    def begin(self, from_state: str | None = None) -> str | None:
        """
        Begin a work session.

        Ensures the workspace exists and is up to date with the lane head.
        Acquires the workspace lock for this agent.
        Returns the state ID the agent is working from.
        """
        # Ensure workspace exists
        if not self.repo.wm.exists(self.workspace_name):
            self.repo.workspace_create(
                self.workspace_name,
                lane=self.lane,
                state_id=from_state,
                agent_id=self.agent.agent_id,
            )
        else:
            # Update workspace to latest lane head (or specified state)
            target = from_state or self.repo.head(self.lane)
            if target:
                info = self.repo.wm.get(self.workspace_name)
                if info and info.base_state != target:
                    self.repo.workspace_update(self.workspace_name, target)

        # Acquire lock
        acquired = self.repo.workspace_acquire(self.workspace_name, self.agent.agent_id)
        if not acquired:
            info = self.repo.wm.get(self.workspace_name)
            current_agent = info.agent_id if info else "unknown"
            raise RuntimeError(
                f"Workspace '{self.workspace_name}' is locked by agent '{current_agent}'. "
                f"Each agent needs its own workspace."
            )

        # Record starting state
        info = self.repo.wm.get(self.workspace_name)
        self.base_state = info.base_state if info else from_state
        self._start_time = time.time()
        self._token_count_in = 0
        self._token_count_out = 0
        self._api_calls = 0

        return self.base_state

    def end(self):
        """End a work session. Releases the workspace lock."""
        self.repo.workspace_release(self.workspace_name)

    def record_tokens(self, tokens_in: int = 0, tokens_out: int = 0):
        """Record token usage (call this as the agent works)."""
        self._token_count_in += tokens_in
        self._token_count_out += tokens_out
        self._api_calls += 1

    def propose(
        self,
        prompt: str,
        tags: list[str] | None = None,
        context_refs: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        """
        Snapshot the workspace and propose a transition.
        Returns the transition ID.
        """
        if self._start_time is None:
            raise RuntimeError("Must call begin() before propose()")
        new_state = self.repo.snapshot(self.workspace_name, parent_id=self.base_state)

        cost = CostRecord(
            tokens_in=self._token_count_in,
            tokens_out=self._token_count_out,
            wall_time_ms=(time.time() - self._start_time) * 1000 if self._start_time else 0,
            api_calls=self._api_calls,
        )

        tid = self.repo.propose(
            from_state=self.base_state,
            to_state=new_state,
            prompt=prompt,
            agent=self.agent,
            lane=self.lane,
            tags=tags,
            cost=cost,
            context_refs=context_refs,
            metadata=metadata,
        )

        return tid

    def checkpoint(
        self,
        prompt: str,
        auto_accept: bool = False,
        evaluator: str = "auto",
        tags: list[str] | None = None,
    ) -> dict:
        """
        Quick checkpoint: snapshot workspace + propose + optionally accept.
        """
        if self._start_time is None:
            raise RuntimeError("Must call begin() before checkpoint()")
        cost = CostRecord(
            tokens_in=self._token_count_in,
            tokens_out=self._token_count_out,
            wall_time_ms=(time.time() - self._start_time) * 1000 if self._start_time else 0,
            api_calls=self._api_calls,
        )

        result = self.repo.quick_commit(
            workspace=self.workspace_name,
            prompt=prompt,
            agent=self.agent,
            lane=self.lane,
            tags=tags,
            cost=cost,
            auto_accept=auto_accept,
            evaluator=evaluator,
        )

        # Update base state if accepted
        if result["status"] == TransitionStatus.ACCEPTED.value:
            self.base_state = result["to_state"]

        return result

    def create_lane(self, name: str) -> str:
        """
        Create a new lane with its own workspace from the current base state.
        Switches this session to the new lane/workspace.
        """
        # Release lock on current workspace before switching
        try:
            self.repo.workspace_release(self.workspace_name)
        except Exception:
            logger.warning(
                "Failed to release workspace '%s' during create_lane",
                self.workspace_name, exc_info=True)
        self.repo.create_lane(name, self.base_state)
        self.lane = name
        self.workspace_name = name
        # Acquire lock on new workspace
        self.repo.workspace_acquire(self.workspace_name, self.agent.agent_id)
        return name

    def switch_lane(self, name: str):
        """Switch to a different lane (and its workspace)."""
        # Release current workspace if we hold the lock
        try:
            self.repo.workspace_release(self.workspace_name)
        except Exception:
            logger.warning(
                "Failed to release workspace '%s' during switch_lane",
                self.workspace_name, exc_info=True)

        self.lane = name
        self.workspace_name = name
        self.base_state = self.repo.head(name)

        # Acquire lock on new workspace if it exists
        if self.repo.wm.exists(self.workspace_name):
            self.repo.workspace_acquire(self.workspace_name, self.agent.agent_id)

    @contextmanager
    def work(self, prompt: str, tags: list[str] | None = None, auto_accept: bool = False):
        """
        Context manager for a unit of work.

        Usage:
            with session.work("Implement feature X", tags=["feature"]) as w:
                # w.path is the isolated workspace directory
                (w.path / "src" / "feature.py").write_text("...")
                w.record_tokens(tokens_in=2000, tokens_out=1200)

        On successful exit: snapshots, proposes, and optionally accepts.
        On exception: snapshots, proposes, rejects, records the error.
        Always releases the workspace lock on exit.
        """
        self.begin()
        ctx = WorkContext(self)
        agent_error = None
        try:
            yield ctx
        except Exception as e:
            agent_error = e
            ctx.metadata["error"] = str(e)
            ctx.metadata["error_type"] = type(e).__name__
        finally:
            try:
                if agent_error is not None:
                    try:
                        tid = self.propose(
                            prompt=f"[FAILED] {prompt}",
                            tags=(tags or []) + ["failed"],
                            metadata=ctx.metadata,
                        )
                        err_msg = str(ctx.metadata.get("error"))
                        self.repo.reject(tid, evaluator="auto", summary=err_msg)
                    except Exception:
                        logger.warning("Failed to propose/reject after agent error", exc_info=True)
                else:
                    try:
                        result = self.checkpoint(
                            prompt=prompt,
                            tags=tags,
                            auto_accept=auto_accept,
                        )
                        ctx.result = result
                    except Exception:
                        logger.warning("Failed to checkpoint after agent work", exc_info=True)
            finally:
                # Always release the workspace lock
                try:
                    self.end()
                except Exception:
                    logger.warning("Failed to release workspace lock", exc_info=True)

        # Re-raise the original agent error after cleanup is complete,
        # so it can never be shadowed by cleanup failures.
        if agent_error is not None:
            raise agent_error


class WorkContext:
    """Context object yielded by AgentSession.work()."""

    def __init__(self, session: AgentSession):
        self._session = session
        self.metadata: dict = {}
        self.result: dict | None = None

    @property
    def path(self) -> Path:
        """The workspace directory — where the agent should modify files."""
        p = self._session.workspace_path
        if p is None:
            raise RuntimeError("No workspace path available")
        return p

    def record_tokens(self, tokens_in: int = 0, tokens_out: int = 0):
        self._session.record_tokens(tokens_in, tokens_out)

    def add_metadata(self, key: str, value):
        self.metadata[key] = value
