"""
World States

The unit of versioning is a WorldState — a complete, immutable snapshot
of the entire project at a point in time. This is fundamentally different
from git's commit model:

- Git commits point to trees and track *changes*
- WorldStates ARE the tree — they represent the full state

Agents don't "commit changes." They propose a new world state.
This eliminates partial commits, dirty working directories,
and the entire class of "I forgot to add that file" problems.

WorldStates form a DAG through parent references, giving you
full history traversal. But the primary interface is "give me
the state of the world at this point" rather than "show me
what changed."
"""

import fnmatch
import json
import logging
import stat
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# Default file mode for files without stored mode (backward compatibility)
DEFAULT_FILE_MODE = 0o644
# Mask for executable bits
EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

from .cas import ContentStore, ObjectType  # noqa: E402


class TreeDepthLimitError(ValueError):
    """Raised when tree depth exceeds configured limit."""


class TransitionStatus(Enum):
    PROPOSED = "proposed"       # Agent has proposed this state
    EVALUATING = "evaluating"   # Currently being evaluated (tests, review)
    ACCEPTED = "accepted"       # Passed evaluation, part of canonical history
    REJECTED = "rejected"       # Failed evaluation
    SUPERSEDED = "superseded"   # Another transition replaced this one


@dataclass
class AgentIdentity:
    """Who made this change."""
    agent_id: str
    agent_type: str          # e.g. "coder", "reviewer", "refactorer"
    model: str | None = None  # e.g. "claude-sonnet-4-20250514"
    session_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentIdentity":
        return cls(**d)


@dataclass
class Intent:
    """
    Why a change was made. This is the key innovation over git.

    Git commit messages are free-text afterthoughts. Intents are
    structured, searchable records of the *instruction* that caused
    a change, not just a description of the change itself.
    """
    id: str
    prompt: str                      # The instruction/prompt that triggered this
    agent: AgentIdentity
    context_refs: list[str] = field(default_factory=list)  # Referenced state/file IDs
    tags: list[str] = field(default_factory=list)           # Semantic tags for search
    metadata: dict = field(default_factory=dict)            # Arbitrary extra context
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "agent": self.agent.to_dict(),
            "context_refs": self.context_refs,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Intent":
        return cls(
            id=d["id"],
            prompt=d["prompt"],
            agent=AgentIdentity.from_dict(d["agent"]),
            context_refs=d.get("context_refs", []),
            tags=d.get("tags", []),
            metadata=d.get("metadata", {}),
            created_at=d.get("created_at", time.time()),
        )


@dataclass
class EvaluationResult:
    """
    The result of evaluating a proposed state transition.

    This is a first-class concept, not a bolted-on CI check.
    Every transition carries its evaluation result permanently.
    """
    passed: bool
    evaluator: str                    # Who/what evaluated (agent ID, "test_suite", "human:kim")
    checks: dict[str, bool] = field(default_factory=dict)  # Individual check results
    summary: str = ""
    duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "evaluator": self.evaluator,
            "checks": self.checks,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvaluationResult":
        return cls(**d)


@dataclass
class CostRecord:
    """Resource consumption for a transition."""
    tokens_in: int = 0
    tokens_out: int = 0
    wall_time_ms: float = 0.0
    api_calls: int = 0

    def to_dict(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "wall_time_ms": self.wall_time_ms,
            "api_calls": self.api_calls,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CostRecord":
        return cls(**d)


class WorldStateManager:
    """
    Manages world states and transitions.

    This is the core of the version control system. It handles:
    - Creating world states from directory trees
    - Proposing, evaluating, and accepting transitions
    - Querying history and lineage
    - Lane (workstream) management
    """

    # Default: 100 levels of directory depth
    # Note: 0 or missing value uses DEFAULT_MAX_TREE_DEPTH
    #       For effectively unlimited, set to very large value (e.g., 10000)
    DEFAULT_MAX_TREE_DEPTH = 100

    def __init__(self, store: ContentStore, db_path: Path, max_tree_depth: int = 0):
        self.store = store
        self.db_path = db_path
        # If max_tree_depth is 0 or not provided, use default
        self.max_tree_depth = max_tree_depth if max_tree_depth > 0 else self.DEFAULT_MAX_TREE_DEPTH
        # Use same connection as store for simplicity
        self.conn = store.conn
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS world_states (
                id TEXT PRIMARY KEY,
                root_tree TEXT NOT NULL,
                parent_id TEXT,
                created_at REAL NOT NULL,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (parent_id) REFERENCES world_states(id)
            );

            CREATE TABLE IF NOT EXISTS intents (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                agent_json TEXT NOT NULL,
                context_refs TEXT DEFAULT '[]',
                tags TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transitions (
                id TEXT PRIMARY KEY,
                from_state TEXT,
                to_state TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                lane TEXT DEFAULT 'main',
                status TEXT NOT NULL DEFAULT 'proposed',
                evaluation_json TEXT,
                cost_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (from_state) REFERENCES world_states(id),
                FOREIGN KEY (to_state) REFERENCES world_states(id),
                FOREIGN KEY (intent_id) REFERENCES intents(id)
            );

            CREATE TABLE IF NOT EXISTS lanes (
                name TEXT PRIMARY KEY,
                head_state TEXT,
                fork_base TEXT,
                created_at REAL NOT NULL,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (head_state) REFERENCES world_states(id),
                FOREIGN KEY (fork_base) REFERENCES world_states(id)
            );

            CREATE INDEX IF NOT EXISTS idx_transitions_lane
                ON transitions(lane);
            CREATE INDEX IF NOT EXISTS idx_transitions_status
                ON transitions(status);
            CREATE INDEX IF NOT EXISTS idx_transitions_from
                ON transitions(from_state);
            CREATE INDEX IF NOT EXISTS idx_transitions_to
                ON transitions(to_state);
            CREATE INDEX IF NOT EXISTS idx_intents_tags
                ON intents(tags);
            CREATE INDEX IF NOT EXISTS idx_world_states_parent
                ON world_states(parent_id);

            CREATE TABLE IF NOT EXISTS intent_embeddings (
                intent_id TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (intent_id) REFERENCES intents(id)
            );
        """)
        self.conn.commit()

    # ── World State Creation ──────────────────────────────────────

    def snapshot_directory(
        self, path: Path, parent_id: str | None = None, use_cache: bool = True,
    ) -> str:
        """
        Create a world state from a directory on disk.

        Recursively walks the directory, stores all files as blobs,
        builds tree objects, and creates a world state pointing to
        the root tree.

        Respects .vexignore if present (one filename pattern per line).
        Supports directory patterns (trailing ``/``), negation (``!`` prefix).

        Returns the world state ID (which is a content hash).
        """
        ignore_names = set(self.DEFAULT_IGNORE)
        ignore_dirs: set[str] = set()
        negate_patterns: set[str] = set()

        # Load .vexignore from snapshot root if present
        vexignore = path / ".vexignore"
        if vexignore.is_file():
            for line in vexignore.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("!"):
                    # Negation — strip the '!' and optional trailing '/'
                    pat = line[1:].rstrip("/")
                    if pat:
                        negate_patterns.add(pat)
                elif line.endswith("/"):
                    # Directory-only pattern
                    ignore_dirs.add(line.rstrip("/"))
                else:
                    ignore_names.add(line)

        with self.store.batch():
            root_tree_hash = self._hash_directory(
                path,
                frozenset(ignore_names),
                frozenset(ignore_dirs),
                frozenset(negate_patterns),
                use_cache=use_cache,
            )
        return self._create_world_state(root_tree_hash, parent_id)

    # Paths to always ignore when snapshotting (matched against filename)
    # Includes VCS dirs, build artifacts, OS noise, and security-sensitive files
    DEFAULT_IGNORE = frozenset({
        # Version control
        ".vex", ".git", ".svn", ".hg",
        # Build artifacts and caches
        "__pycache__", "node_modules", ".DS_Store", "Thumbs.db",
        # Environment and secrets (prevent accidental exposure)
        ".env", ".env.local", ".env.development", ".env.production",
        ".env.test", ".env.staging",
        # Credentials and keys
        "*.pem", "*.key", "*.p12", "*.pfx",
        "credentials.json", "service-account.json",
        # IDE and editor
        ".idea", ".vscode",
    })

    def _hash_directory(
        self,
        path: Path,
        ignore_names: frozenset | None = None,
        ignore_dirs: frozenset = frozenset(),
        negate: frozenset = frozenset(),
        use_cache: bool = True,
        current_depth: int = 0,
        relative_prefix: str = "",
    ) -> str:
        """
        Recursively hash a directory into the CAS.

        Skips entries matching the ignore sets. By default skips VCS dirs,
        build artifacts, and OS noise — but NOT project dotfiles like
        .env, .editorconfig, .gitignore, .npmrc, etc.

        When use_cache is True, checks the stat cache (mtime_ns + size)
        before reading file contents. Cache hits skip read_bytes + store_blob.

        Symlinks are skipped by default to prevent reading files outside
        the workspace (Fix #1 from audit).

        File modes (especially executable bit) are preserved in tree entries
        as a third element: (type, hash, mode) (Fix #2 from audit).

        Ignore patterns support both basename and relative path matching
        (Fix #3 from audit).

        Raises TreeDepthLimitError if current_depth exceeds max_tree_depth.
        """
        if current_depth >= self.max_tree_depth:
            raise TreeDepthLimitError(
                f"Tree depth {current_depth} exceeds limit of {self.max_tree_depth} at {path}"
            )

        ignore_names = ignore_names or self.DEFAULT_IGNORE
        entries = {}

        for item in sorted(path.iterdir()):
            # Fix #1: Skip symlinks to prevent reading files outside workspace
            if item.is_symlink():
                logger.debug(f"Skipping symlink: {item}")
                continue

            # Compute relative path for path-based ignore matching (Fix #3)
            rel_path = f"{relative_prefix}{item.name}" if relative_prefix else item.name

            if item.is_file():
                if self._should_ignore(item.name, rel_path, ignore_names, negate):
                    continue

                blob_hash = None
                st = item.stat()

                if use_cache:
                    cache_key = str(item)
                    cached = self.store.check_stat_cache(cache_key, st.st_mtime_ns, st.st_size)
                    if cached is not None:
                        blob_hash = cached

                if blob_hash is None:
                    content = item.read_bytes()
                    blob_hash = self.store.store_blob(content)
                    if use_cache:
                        self.store.update_stat_cache(
                            str(item), st.st_mtime_ns, st.st_size, blob_hash)

                # Fix #2: Capture file mode (especially executable bit)
                file_mode = st.st_mode & 0o777
                entries[item.name] = ("blob", blob_hash, file_mode)

            elif item.is_dir():
                # Directories check both ignore_names and ignore_dirs
                if self._should_ignore(item.name, rel_path, ignore_names | ignore_dirs, negate):
                    continue
                subtree_hash = self._hash_directory(
                    item, ignore_names, ignore_dirs, negate, use_cache, current_depth + 1,
                    relative_prefix=f"{rel_path}/",
                )
                entries[item.name] = ("tree", subtree_hash, 0o755)

        return self.store.store_tree(entries)

    @staticmethod
    def _should_ignore(
        name: str,
        rel_path: str,
        ignore: frozenset,
        negate: frozenset = frozenset(),
    ) -> bool:
        """Check if a file/directory should be ignored.

        Fix #3 from audit: Now supports both basename and relative path matching.
        Patterns containing '/' are matched against the relative path,
        patterns without '/' are matched against just the basename.

        Fast-path exact match via ``in``, then falls back to
        fnmatch for patterns containing glob characters. If the name/path
        also matches a *negate* pattern it is re-included (not ignored).
        """
        matched = False

        def matches_pattern(pattern: str, check_name: str, check_path: str) -> bool:
            """Check if pattern matches name or path."""
            # If pattern contains '/', match against relative path
            # Otherwise match against basename only
            target = check_path if '/' in pattern else check_name
            if target == pattern:
                return True
            if any(c in pattern for c in ("*", "?", "[")):
                if fnmatch.fnmatch(target, pattern):
                    return True
            return False

        # Check ignore patterns
        if name in ignore:
            matched = True
        else:
            for pattern in ignore:
                if matches_pattern(pattern, name, rel_path):
                    matched = True
                    break

        if not matched:
            return False

        # Check negation patterns — if matched, the file is re-included
        if negate:
            if name in negate:
                return False
            for pattern in negate:
                if matches_pattern(pattern, name, rel_path):
                    return False

        return True

    def _create_world_state(
        self,
        root_tree: str,
        parent_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Create a world state record."""
        # State ID is hash of (root_tree, parent_id, timestamp, nonce) for uniqueness
        # even if two states have the same tree (e.g., a revert) or are created
        # within the same time.time() tick (especially on Windows with ~15ms granularity)
        now = time.time()
        state_content = json.dumps({
            "root_tree": root_tree,
            "parent_id": parent_id,
            "created_at": now,
            "nonce": str(uuid.uuid4()),
        }).encode()
        state_id = self.store.hash_content(state_content, ObjectType.STATE)

        self.conn.execute(
            """INSERT OR IGNORE INTO world_states
               (id, root_tree, parent_id, created_at, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (state_id, root_tree, parent_id, now, json.dumps(metadata or {}))
        )
        self.conn.commit()
        return state_id

    def create_state_from_tree(
        self,
        tree_hash: str,
        parent_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Create a world state from an already-stored tree hash."""
        return self._create_world_state(tree_hash, parent_id, metadata)

    # ── Intent Management ─────────────────────────────────────────

    def record_intent(self, intent: Intent) -> str:
        """Store an intent record."""
        self.conn.execute(
            """INSERT OR IGNORE INTO intents
               (id, prompt, agent_json, context_refs, tags, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                intent.id,
                intent.prompt,
                json.dumps(intent.agent.to_dict()),
                json.dumps(intent.context_refs),
                json.dumps(intent.tags),
                json.dumps(intent.metadata),
                intent.created_at,
            )
        )
        self.conn.commit()
        return intent.id

    def get_intent(self, intent_id: str) -> Intent | None:
        row = self.conn.execute(
            """SELECT id, prompt, agent_json, context_refs, tags, metadata, created_at
               FROM intents WHERE id = ?""",
            (intent_id,)
        ).fetchone()
        if row is None:
            return None
        return Intent(
            id=row[0],
            prompt=row[1],
            agent=AgentIdentity.from_dict(json.loads(row[2])),
            context_refs=json.loads(row[3]),
            tags=json.loads(row[4]),
            metadata=json.loads(row[5]),
            created_at=row[6],
        )

    # ── Transitions ───────────────────────────────────────────────

    def propose(
        self,
        from_state: str | None,
        to_state: str,
        intent: Intent,
        lane: str = "main",
        cost: CostRecord | None = None,
    ) -> str:
        """
        Propose a state transition.

        This is the primary way agents interact with the system.
        An agent says "I want to move from state X to state Y,
        and here's why (intent)."

        The transition starts as PROPOSED and must be evaluated
        before it can be accepted.
        """
        self.record_intent(intent)

        transition_id = str(uuid.uuid4())
        now = time.time()

        self.conn.execute(
            """INSERT INTO transitions
               (id, from_state, to_state, intent_id, lane, status,
                cost_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                transition_id,
                from_state,
                to_state,
                intent.id,
                lane,
                TransitionStatus.PROPOSED.value,
                json.dumps((cost or CostRecord()).to_dict()),
                now,
                now,
            )
        )

        # Ensure lane exists
        self.conn.execute(
            """INSERT OR IGNORE INTO lanes
               (name, head_state, fork_base, created_at) VALUES (?, ?, ?, ?)""",
            (lane, from_state, from_state, now)
        )

        self.conn.commit()
        return transition_id

    def evaluate(
        self,
        transition_id: str,
        result: EvaluationResult,
    ) -> TransitionStatus:
        """
        Record the evaluation result for a transition.

        If the evaluation passes, the transition is accepted and
        the lane head advances. If it fails, the transition is rejected.

        This is where the "gating" happens — the fundamental mechanism
        that replaces git's merge-based integration.

        Safety: uses BEGIN IMMEDIATE to atomically validate that the
        transition's from_state still matches the lane head. If another
        transition was accepted first (moving the head), this one is
        rejected as stale to prevent silent data loss.
        """
        # Use BEGIN IMMEDIATE for atomic check-then-act.
        # This prevents two concurrent accepts from both passing
        # the stale check before either commits.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT from_state, to_state, lane, status FROM transitions WHERE id = ?",
                (transition_id,)
            ).fetchone()

            if row is None:
                raise ValueError(f"Transition not found: {transition_id}")

            from_state, to_state, lane, current_status = row

            if current_status != TransitionStatus.PROPOSED.value:
                raise ValueError(
                    f"Transition {transition_id} is {current_status}, not proposed"
                )

            new_status = TransitionStatus.ACCEPTED if result.passed else TransitionStatus.REJECTED
            now = time.time()

            # If accepting, verify from_state still matches lane head
            # This prevents two concurrent accepts from silently overwriting each other
            if result.passed:
                current_head = self.get_lane_head(lane)
                if from_state is not None and current_head != from_state:
                    logger.warning(
                        "Stale accept: transition %s from_state %s != lane head %s",
                        transition_id, from_state, current_head,
                    )
                    new_status = TransitionStatus.REJECTED
                    result = EvaluationResult(
                        passed=False,
                        evaluator=result.evaluator,
                        checks=result.checks,
                        summary=f"Stale: lane head moved to {current_head} "
                                f"(expected {from_state}). Re-propose from current head.",
                    )

            self.conn.execute(
                """UPDATE transitions
                   SET status = ?, evaluation_json = ?, updated_at = ?
                   WHERE id = ?""",
                (new_status.value, json.dumps(result.to_dict()), now, transition_id)
            )

            # If accepted, advance the lane head
            if new_status == TransitionStatus.ACCEPTED:
                self.conn.execute(
                    "UPDATE lanes SET head_state = ? WHERE name = ?",
                    (to_state, lane)
                )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return new_status

    # ── Lane Management ───────────────────────────────────────────

    @staticmethod
    def _validate_lane_name(name: str):
        """Validate lane name to prevent path traversal and injection."""
        if not name:
            raise ValueError("Lane name cannot be empty")
        if "\0" in name:
            raise ValueError(f"Lane name contains null byte: {name!r}")
        if ".." in name:
            raise ValueError(f"Lane name contains '..': {name!r}")
        if "/" in name or "\\" in name:
            raise ValueError(
                f"Lane name contains path separator: {name!r}. "
                f"Use '-' instead of '/' (e.g., 'feature-auth' not 'feature/auth')"
            )

    def create_lane(
        self,
        name: str,
        base_state: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """
        Create a new lane (isolated workstream).

        Lanes are NOT branches. They don't merge. An agent works
        in a lane, produces candidates, and those candidates get
        accepted into the target lane through gating.

        Records fork_base — the exact state this lane was forked from.
        This is used by promote to compute deltas without graph walking.
        """
        self._validate_lane_name(name)
        now = time.time()
        self.conn.execute(
            """INSERT INTO lanes (name, head_state, fork_base, created_at, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (name, base_state, base_state, now, json.dumps(metadata or {}))
        )
        self.conn.commit()
        return name

    def get_lane_head(self, lane: str = "main") -> str | None:
        """Get the current head state of a lane."""
        row = self.conn.execute(
            "SELECT head_state FROM lanes WHERE name = ?",
            (lane,)
        ).fetchone()
        return row[0] if row else None

    def get_lane_fork_base(self, lane: str) -> str | None:
        """Get the fork base of a lane — the state it was forked from."""
        row = self.conn.execute(
            "SELECT fork_base FROM lanes WHERE name = ?",
            (lane,)
        ).fetchone()
        return row[0] if row else None

    def list_lanes(self) -> list[dict]:
        """List all lanes with their current state."""
        rows = self.conn.execute(
            """SELECT name, head_state, fork_base, created_at, metadata
               FROM lanes ORDER BY created_at"""
        ).fetchall()
        return [
            {
                "name": r[0],
                "head_state": r[1],
                "fork_base": r[2],
                "created_at": r[3],
                "metadata": json.loads(r[4]),
            }
            for r in rows
        ]

    # ── Querying ──────────────────────────────────────────────────

    def get_state(self, state_id: str) -> dict | None:
        """Get a world state by ID."""
        row = self.conn.execute(
            "SELECT id, root_tree, parent_id, created_at, metadata FROM world_states WHERE id = ?",
            (state_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "root_tree": row[1],
            "parent_id": row[2],
            "created_at": row[3],
            "metadata": json.loads(row[4]),
        }

    def history(
        self,
        lane: str = "main",
        limit: int = 50,
        status_filter: TransitionStatus | None = None,
    ) -> list[dict]:
        """
        Get the transition history for a lane.

        Returns transitions in reverse chronological order with
        their full intent and evaluation records.
        """
        query = """
            SELECT t.id, t.from_state, t.to_state, t.intent_id, t.lane,
                   t.status, t.evaluation_json, t.cost_json, t.created_at,
                   i.prompt, i.agent_json, i.tags
            FROM transitions t
            JOIN intents i ON t.intent_id = i.id
            WHERE t.lane = ?
        """
        params: list = [lane]

        if status_filter:
            query += " AND t.status = ?"
            params.append(status_filter.value)

        query += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "from_state": r[1],
                "to_state": r[2],
                "intent_id": r[3],
                "lane": r[4],
                "status": r[5],
                "evaluation": json.loads(r[6]) if r[6] else None,
                "cost": json.loads(r[7]),
                "created_at": r[8],
                "intent_prompt": r[9],
                "agent": json.loads(r[10]),
                "tags": json.loads(r[11]),
            }
            for r in rows
        ]

    def trace(self, state_id: str, max_depth: int = 50) -> list[dict]:
        """
        Trace the lineage of a world state back through its history.

        Returns the chain of transitions that led to this state,
        giving you full causal provenance — not just "what changed"
        but "why it changed and who changed it."
        """
        lineage = []
        current = state_id

        for _ in range(max_depth):
            # Find the transition that produced this state
            row = self.conn.execute(
                """SELECT t.id, t.from_state, t.to_state, t.status,
                          i.prompt, i.agent_json, i.tags, t.created_at
                   FROM transitions t
                   JOIN intents i ON t.intent_id = i.id
                   WHERE t.to_state = ? AND t.status = 'accepted'
                   ORDER BY t.created_at DESC LIMIT 1""",
                (current,)
            ).fetchone()

            if row is None:
                break

            lineage.append({
                "transition_id": row[0],
                "from_state": row[1],
                "to_state": row[2],
                "status": row[3],
                "intent_prompt": row[4],
                "agent": json.loads(row[5]),
                "tags": json.loads(row[6]),
                "created_at": row[7],
            })

            current = row[1]  # Follow to parent
            if current is None:
                break

        return lineage

    def search_intents(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search intents by prompt text or tags.

        This is the basic text search. In production, you'd layer
        embedding-based semantic search on top of this for queries
        like "show me everything related to authentication."
        """
        rows = self.conn.execute(
            """SELECT i.id, i.prompt, i.agent_json, i.tags, i.created_at,
                      t.id, t.from_state, t.to_state, t.status, t.lane
               FROM intents i
               LEFT JOIN transitions t ON t.intent_id = i.id
               WHERE i.prompt LIKE ? OR i.tags LIKE ?
               ORDER BY i.created_at DESC
               LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit)
        ).fetchall()

        return [
            {
                "intent_id": r[0],
                "prompt": r[1],
                "agent": json.loads(r[2]),
                "tags": json.loads(r[3]),
                "created_at": r[4],
                "transition_id": r[5],
                "from_state": r[6],
                "to_state": r[7],
                "status": r[8],
                "lane": r[9],
            }
            for r in rows
        ]

    # ── Embedding Storage ────────────────────────────────────────

    def store_embedding(self, intent_id: str, embedding: bytes, model: str, dimensions: int):
        """Store an embedding for an intent."""
        now = time.time()
        self.conn.execute(
            """INSERT OR REPLACE INTO intent_embeddings
               (intent_id, embedding, model, dimensions, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (intent_id, embedding, model, dimensions, now),
        )
        self.conn.commit()

    def get_embedding(self, intent_id: str):
        """Get an embedding for an intent. Returns raw bytes or None."""
        row = self.conn.execute(
            "SELECT embedding FROM intent_embeddings WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        return row[0] if row else None

    def all_embeddings(self) -> list:
        """Get all stored embeddings as (intent_id, embedding_bytes) pairs."""
        rows = self.conn.execute(
            "SELECT intent_id, embedding FROM intent_embeddings"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ── Diff Support ──────────────────────────────────────────────

    def diff_states(self, state_a: str, state_b: str) -> dict:
        """
        Compute the difference between two world states.

        Returns added, removed, and modified files. This is available
        on-demand (unlike git where diffs are the primary interface).
        Agents don't need diffs — they produced the new state. Humans
        reviewing agent work do.
        """
        sa = self.get_state(state_a)
        sb = self.get_state(state_b)
        if not sa or not sb:
            raise ValueError("State not found")

        files_a = self._flatten_tree(sa["root_tree"])
        files_b = self._flatten_tree(sb["root_tree"])

        all_paths = set(files_a.keys()) | set(files_b.keys())

        added = {}
        removed = {}
        modified = {}
        unchanged = []

        for path in sorted(all_paths):
            hash_a = files_a.get(path)
            hash_b = files_b.get(path)

            if hash_a is None:
                added[path] = hash_b
            elif hash_b is None:
                removed[path] = hash_a
            elif hash_a != hash_b:
                modified[path] = {"before": hash_a, "after": hash_b}
            else:
                unchanged.append(path)

        return {
            "added": added,
            "removed": removed,
            "modified": modified,
            "unchanged_count": len(unchanged),
        }

    def _flatten_tree(self, tree_hash: str, prefix: str = "") -> dict[str, str]:
        """Flatten a tree into {path: blob_hash} mapping (without modes)."""
        entries = self.store.read_tree(tree_hash)
        result = {}

        for name, entry in entries.items():
            # Handle both old (type, hash) and new (type, hash, mode) formats
            typ, hash_val = entry[0], entry[1]
            full_path = f"{prefix}/{name}" if prefix else name
            if typ == "blob":
                result[full_path] = hash_val
            elif typ == "tree":
                result.update(self._flatten_tree(hash_val, full_path))

        return result

    def _flatten_tree_with_modes(
        self, tree_hash: str, prefix: str = ""
    ) -> dict[str, tuple[str, int]]:
        """Flatten a tree into {path: (blob_hash, mode)} mapping.

        Fix #2 from audit: Include file modes for proper permission restoration.
        """
        entries = self.store.read_tree(tree_hash)
        result = {}

        for name, entry in entries.items():
            typ, hash_val = entry[0], entry[1]
            mode = entry[2] if len(entry) > 2 else (0o755 if typ == "tree" else DEFAULT_FILE_MODE)
            full_path = f"{prefix}/{name}" if prefix else name
            if typ == "blob":
                result[full_path] = (hash_val, mode)
            elif typ == "tree":
                result.update(self._flatten_tree_with_modes(hash_val, full_path))

        return result

    # ── Materialization ───────────────────────────────────────────

    def materialize(self, state_id: str, target_dir: Path):
        """
        Reconstruct a world state on disk.

        This is the equivalent of `git checkout` — take a world state
        and write its contents to a directory. Used for:
        - Giving agents a working directory
        - Exporting states for human review
        - Recovering from agent mistakes (just materialize the last good state)
        """
        state = self.get_state(state_id)
        if not state:
            raise ValueError(f"State not found: {state_id}")

        target_dir.mkdir(parents=True, exist_ok=True)
        self._materialize_tree(state["root_tree"], target_dir)

    def _materialize_tree(self, tree_hash: str, target_dir: Path, current_depth: int = 0):
        """
        Recursively write a tree to disk.

        Fix #2 from audit: Now restores file modes (especially executable bit).

        Raises TreeDepthLimitError if current_depth exceeds max_tree_depth.
        """
        if current_depth >= self.max_tree_depth:
            raise TreeDepthLimitError(
                f"Tree depth {current_depth} exceeds limit of {self.max_tree_depth} at {target_dir}"
            )

        entries = self.store.read_tree(tree_hash)

        for name, entry in entries.items():
            # Handle both old (type, hash) and new (type, hash, mode) formats
            typ, hash_val = entry[0], entry[1]
            mode = entry[2] if len(entry) > 2 else (0o755 if typ == "tree" else DEFAULT_FILE_MODE)
            target = target_dir / name

            if typ == "blob":
                obj = self.store.retrieve(hash_val)
                if obj:
                    target.write_bytes(obj.data)
                    # Fix #2: Restore file mode
                    try:
                        target.chmod(mode)
                    except OSError:
                        # chmod may fail on some filesystems (e.g., FAT32, some network mounts)
                        pass
                else:
                    logger.warning(
                        "Missing blob %s for file %s during materialization",
                        hash_val, target,
                    )

            elif typ == "tree":
                target.mkdir(parents=True, exist_ok=True)
                self._materialize_tree(hash_val, target, current_depth + 1)
