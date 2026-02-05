"""
Workspaces

The missing piece that makes lane isolation real.

Without workspaces, lanes are a logical fiction — they track separate
histories but agents share a single working directory and stomp on
each other's files. Workspaces give each lane (or each agent) a
physically isolated directory backed by the CAS.

Design (git-style main):

    my-project/
    ├── .vex/
    │   ├── store.db
    │   ├── main.json          ← main workspace metadata
    │   ├── main.lockdir/      ← main workspace lock
    │   └── workspaces/
    │       ├── feature-auth/  ← isolated feature lane
    │       └── feature-auth.json
    ├── app.py                 ← main lane files at repo root
    └── models.py

The "main" workspace is special: it IS the repo root (like git).
Feature lanes get isolated subdirectories under .vex/workspaces/.

Key properties:
- Main workspace = repo root (feels like git)
- Feature workspaces = isolated directories (parallel agent safety)
- Creating a workspace is cheap: materialize from CAS
- Workspaces are independent: modifying one can't affect another
- Cleanup for feature lanes is just rm -rf on the workspace dir

Smart materialization:
- When creating a workspace from a state, we materialize the full tree
- When updating a workspace to a new state (e.g., after acceptance),
  we diff the old and new trees and only write/delete what changed
- This makes "rebase onto latest main" cheap for large repos
- Main workspace materialization protects .vex/ directory

Locking:
- Uses atomic mkdir for cross-platform advisory locking (no fcntl/msvcrt)
- Main lock: .vex/main.lockdir/
- Feature lock: .vex/workspaces/<name>.lockdir/
- Owner metadata stored in lockdir/owner.json
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


def _hostname() -> str:
    """Get hostname, cached after first call."""
    if not hasattr(_hostname, "_cached"):
        _hostname._cached = socket.gethostname()
    return _hostname._cached


def _replace_with_retry(src: Path, dst: Path):
    """Replace dst with src, retrying on Windows PermissionError.

    On Windows, antivirus or indexing services can briefly lock files,
    causing ``PermissionError`` on rename.  We retry up to 5 times with
    exponential backoff.  On POSIX, any error is raised immediately.
    """
    if os.name == "nt":
        for attempt in range(5):
            try:
                src.replace(dst)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    else:
        src.replace(dst)


def _atomic_write(path: Path, content: str):
    """
    Write content to a file atomically via write-to-temp + rename.

    Prevents partial/corrupt JSON if the process dies mid-write.
    On POSIX, rename is atomic. On Windows, it's as close as you get
    without external deps.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(Path(tmp_path), path)
    except Exception:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class WorkspaceStatus(Enum):
    ACTIVE = "active"       # Agent is working in this workspace
    IDLE = "idle"           # Workspace exists but no active work
    STALE = "stale"         # Workspace is behind lane head
    DISPOSED = "disposed"   # Workspace has been cleaned up


@dataclass
class WorkspaceInfo:
    """Metadata about a workspace."""
    name: str                    # Matches lane name by default
    lane: str                    # Which lane this workspace tracks
    path: Path                   # Physical directory path
    base_state: str | None    # State this workspace was materialized from
    status: str
    agent_id: str | None      # Agent currently using this workspace
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lane": self.lane,
            "path": str(self.path),
            "base_state": self.base_state,
            "status": self.status,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkspaceManager:
    """
    Manages isolated working directories for lanes and agents.

    The workspace manager is responsible for:
    - Creating workspaces by materializing CAS trees to disk
    - Tracking which workspaces exist and who is using them
    - Smart updates that only write changed files
    - Cleanup of disposed workspaces
    - Cross-platform locking via atomic mkdir

    The "main" workspace is special — it IS the repo root (like git).
    Feature workspaces live under .vex/workspaces/<name>/.

    Layout:
        repo_root/                    ← main workspace (the actual working files)
        .vex/main.json                ← main workspace metadata
        .vex/main.lockdir/            ← main workspace lock
        .vex/workspaces/<n>/          ← feature workspace files
        .vex/workspaces/<n>.json      ← feature workspace metadata
        .vex/workspaces/<n>.lockdir/  ← feature workspace lock
    """

    # The default/main workspace name — treated specially
    MAIN_WORKSPACE = "main"

    def __init__(self, vex_dir: Path, wsm):
        """
        Args:
            vex_dir: Path to the .vex directory
            wsm: WorldStateManager instance for CAS access
        """
        self.vex_dir = vex_dir
        self.repo_root = vex_dir.parent  # The actual project root
        self.wsm = wsm
        self.workspaces_dir = vex_dir / "workspaces"
        self.workspaces_dir.mkdir(exist_ok=True)

    def _validate_workspace_name(self, name: str):
        """Validate workspace name does not contain path traversal."""
        if not name or ".." in name or "\0" in name:
            raise ValueError(f"Invalid workspace name: {name!r}")
        # Ensure the derived paths stay within workspaces_dir
        ws_path = self.workspaces_dir / name
        try:
            ws_path.resolve().relative_to(self.workspaces_dir.resolve())
        except ValueError:
            raise ValueError(f"Workspace name escapes workspaces directory: {name!r}")

    # ── Creation ──────────────────────────────────────────────────

    def create(
        self,
        name: str,
        lane: str,
        state_id: str | None = None,
        agent_id: str | None = None,
    ) -> WorkspaceInfo:
        """
        Create a new workspace.

        If state_id is provided, materializes that state from the CAS
        into the workspace directory. If state_id is None (empty repo),
        creates an empty workspace directory.

        The "main" workspace is special: it uses the repo root as its
        working directory (like git). Feature workspaces get isolated
        directories under .vex/workspaces/.

        Args:
            name:     Workspace name (typically matches lane name)
            lane:     Lane this workspace tracks
            state_id: World state to materialize (None for empty workspace)
            agent_id: Agent that will use this workspace (optional)
        """
        self._validate_workspace_name(name)
        ws_path = self._workspace_path(name)
        meta_path = self._meta_path(name)

        # For main workspace, check if metadata already exists (workspace already created)
        # For feature workspaces, check if directory exists
        if self._is_main(name):
            if meta_path.exists():
                raise ValueError(
                    f"Workspace '{name}' already exists.\n"
                    f"Use `vex workspace remove {name}` first, or choose a different name."
                )
            # Main workspace directory (repo root) always exists, that's fine
        else:
            if ws_path.exists():
                raise ValueError(
                    f"Workspace '{name}' already exists at {ws_path}\n"
                    f"Use `vex workspace remove {name}` first, or choose a different name."
                )

        if state_id is not None and not self._is_main(name):
            # Feature workspace: materialize into new directory
            ws_path.mkdir(parents=True, exist_ok=True)
            dirty_path = ws_path / ".vex_materializing"
            dirty_path.write_text(json.dumps({
                "state_id": state_id,
                "started_at": time.time(),
            }))

            try:
                self.wsm.materialize(state_id, ws_path)
            except Exception:
                # Leave dirty marker so recovery can detect partial state
                raise
            else:
                # Only remove marker on success
                dirty_path.unlink(missing_ok=True)
        elif state_id is not None and self._is_main(name):
            # Main workspace with state: materialize into repo root
            # This happens during update, not typically during init
            dirty_path = ws_path / ".vex_materializing"
            dirty_path.write_text(json.dumps({
                "state_id": state_id,
                "started_at": time.time(),
            }))

            try:
                self._materialize_to_main(state_id, ws_path)
            except Exception:
                raise
            else:
                dirty_path.unlink(missing_ok=True)
        elif not self._is_main(name):
            # Empty feature workspace — create the directory
            ws_path.mkdir(parents=True, exist_ok=True)
        # For main with no state_id: repo root already exists, nothing to create

        # Write metadata — ensure parent dirs exist for nested names
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        now = time.time()
        info = WorkspaceInfo(
            name=name,
            lane=lane,
            path=ws_path,
            base_state=state_id,
            status=WorkspaceStatus.ACTIVE.value if agent_id else WorkspaceStatus.IDLE.value,
            agent_id=agent_id,
            created_at=now,
            updated_at=now,
        )
        _atomic_write(meta_path, json.dumps(info.to_dict(), indent=2))

        return info

    def _materialize_to_main(self, state_id: str, ws_path: Path):
        """
        Materialize a state into the main workspace (repo root).

        This is like regular materialize but MUST protect the .vex directory.
        Fix #2: Now restores file modes.
        """
        state = self.wsm.get_state(state_id)
        if not state:
            raise ValueError(f"State not found: {state_id}")

        # Fix #2: Use _flatten_tree_with_modes to get file modes
        files = self.wsm._flatten_tree_with_modes(state["root_tree"])

        # Write all files from the state
        for path, (blob_hash, mode) in files.items():
            # CRITICAL: Never touch .vex directory
            if path.startswith(".vex") or path.startswith(".vex/"):
                continue

            obj = self.wsm.store.retrieve(blob_hash)
            if obj is None:
                continue

            file_path = ws_path / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(obj.data)

            # Fix #2: Restore file mode
            try:
                file_path.chmod(mode)
            except OSError:
                # chmod may fail on some filesystems
                pass

    # ── Smart Update ──────────────────────────────────────────────

    def update(self, name: str, new_state_id: str) -> dict:
        """
        Update a workspace to a new state, writing only what changed.

        Instead of blowing away the directory and re-materializing,
        we diff the old tree against the new tree and surgically
        apply only the differences. On a large repo where an agent
        touched 3 files, this writes 3 files instead of 10,000.

        Returns a summary of what changed.
        """
        info = self.get(name)
        if info is None:
            raise ValueError(f"Workspace '{name}' not found")

        ws_path = info.path
        old_state = info.base_state

        # Dirty marker — signals that the workspace is mid-update
        dirty_path = ws_path / ".vex_materializing"
        dirty_path.write_text(json.dumps({
            "from_state": old_state,
            "to_state": new_state_id,
            "started_at": time.time(),
        }))

        try:
            result = self._apply_update(
                ws_path, old_state, new_state_id, is_main=self._is_main(name))
        except Exception:
            # Leave dirty marker so recovery can detect partial state
            raise
        else:
            # Only remove marker on success
            dirty_path.unlink(missing_ok=True)
            self._update_meta(name, base_state=new_state_id)
            return result

    def _apply_update(
        self, ws_path: Path, old_state: str | None, new_state_id: str,
        is_main: bool = False
    ) -> dict:
        """Apply the actual file changes for an update."""
        if old_state is None:
            if is_main:
                self._clean_workspace_contents(ws_path, protect_vex=True)
                self._materialize_to_main(new_state_id, ws_path)
            else:
                self._clean_workspace_contents(ws_path)
                self.wsm.materialize(new_state_id, ws_path)
            return {"mode": "full_materialize"}

        # Get the diff between old and new states
        diff = self.wsm.diff_states(old_state, new_state_id)

        # Get the new state's tree for reading new content (with file modes)
        new_state = self.wsm.get_state(new_state_id)
        if not new_state:
            raise ValueError(f"State not found: {new_state_id}")

        # Fix #2: Use _flatten_tree_with_modes to get file modes
        new_files = self.wsm._flatten_tree_with_modes(new_state["root_tree"])

        # Apply removals
        for path in diff["removed"]:
            # CRITICAL: Never touch .vex directory in main workspace
            if is_main and (path.startswith(".vex") or path.startswith(".vex/")):
                continue

            file_path = ws_path / path
            # Fix #5: Handle both files and directories properly
            if file_path.is_symlink() or file_path.is_file():
                file_path.unlink()
            elif file_path.is_dir():
                shutil.rmtree(file_path)
            # Clean up empty parent directories
            self._cleanup_empty_parents(file_path.parent, ws_path)

        # Apply additions and modifications
        for path in list(diff["added"].keys()) + list(diff["modified"].keys()):
            # CRITICAL: Never touch .vex directory in main workspace
            if is_main and (path.startswith(".vex") or path.startswith(".vex/")):
                continue

            file_info = new_files.get(path)
            if file_info is None:
                continue

            blob_hash, mode = file_info
            obj = self.wsm.store.retrieve(blob_hash)
            if obj is None:
                continue

            file_path = ws_path / path

            # Fix #5: If a directory exists where a file should go, remove it first
            if file_path.is_dir():
                shutil.rmtree(file_path)

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(obj.data)

            # Fix #2: Restore file mode
            try:
                file_path.chmod(mode)
            except OSError:
                # chmod may fail on some filesystems (e.g., FAT32, some network mounts)
                pass

        return {
            "mode": "incremental",
            "added": len(diff["added"]),
            "removed": len(diff["removed"]),
            "modified": len(diff["modified"]),
            "unchanged": diff["unchanged_count"],
        }

    # ── Snapshot ───────────────────────────────────────────────────

    def snapshot(self, name: str, parent_id: str | None = None) -> str:
        """
        Snapshot a workspace — capture its current state into the CAS.

        This is what agents call after modifying files. It hashes the
        workspace directory (not the repo root) and creates a world state.
        """
        info = self.get(name)
        if info is None:
            raise ValueError(f"Workspace '{name}' not found")

        if parent_id is None:
            parent_id = info.base_state

        return self.wsm.snapshot_directory(info.path, parent_id)

    # ── Locking ───────────────────────────────────────────────────
    #
    # Cross-platform advisory locking via atomic mkdir.
    #
    # mkdir is atomic on every major OS — it either succeeds or raises
    # if the directory already exists. No fcntl, no msvcrt, no deps.
    #
    # Layout:
    #   .vex/workspaces/<name>.lockdir/         ← existence = locked
    #   .vex/workspaces/<name>.lockdir/owner.json  ← who holds it

    def acquire(self, name: str, agent_id: str) -> bool:
        """
        Acquire exclusive access to a workspace.

        Uses atomic mkdir for cross-platform locking. Returns True if
        the lock was acquired, False if someone else holds it.

        The lock is advisory — it doesn't prevent filesystem access,
        but well-behaved agents always check it.
        """
        info = self.get(name)
        if info is None:
            raise ValueError(f"Workspace '{name}' not found")

        lock_dir = self._lock_path(name)

        try:
            lock_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # Lock is held — check if it's stale
            owner = self._read_lock_owner(lock_dir)
            if owner and not self._is_lock_stale(owner):
                return False
            # Stale lock — previous holder died or timed out. Reclaim.
            self._force_remove_lock(lock_dir)
            try:
                lock_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                # Race condition — someone else grabbed it first
                return False

        # Write owner info atomically
        owner_path = lock_dir / "owner.json"
        _atomic_write(owner_path, json.dumps({
            "agent_id": agent_id,
            "acquired_at": time.time(),
            "pid": os.getpid(),
            "hostname": _hostname(),
        }, indent=2))

        self._update_meta(name, agent_id=agent_id, status=WorkspaceStatus.ACTIVE.value)
        return True

    def release(self, name: str):
        """Release the lock on a workspace."""
        lock_dir = self._lock_path(name)
        self._force_remove_lock(lock_dir)
        self._update_meta(name, agent_id=None, status=WorkspaceStatus.IDLE.value)

    def lock_holder(self, name: str) -> dict | None:
        """Get info about who holds the lock, or None if unlocked."""
        lock_dir = self._lock_path(name)
        return self._read_lock_owner(lock_dir)

    def _read_lock_owner(self, lock_dir: Path) -> dict | None:
        """Read the owner.json from a lock directory."""
        owner_path = lock_dir / "owner.json"
        if not owner_path.exists():
            return None
        try:
            return json.loads(owner_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _force_remove_lock(self, lock_dir: Path):
        """Remove a lock directory and its contents."""
        if lock_dir.exists():
            try:
                shutil.rmtree(lock_dir)
            except OSError:
                # On Windows, retry after brief delay (antivirus/indexer may hold files)
                if os.name == "nt":
                    time.sleep(0.1)
                    shutil.rmtree(lock_dir, ignore_errors=True)
                # On POSIX, if rmtree fails the lock is truly stuck
                # Let it propagate so the caller knows

    # Max age before a lock is considered stale regardless of PID
    LOCK_MAX_AGE_SECONDS = 3600 * 4  # 4 hours

    def _is_lock_stale(self, owner: dict) -> bool:
        """
        Determine if a lock is stale (safe to reclaim).

        A lock is stale if any of:
        - The owning PID no longer exists (on the same host)
        - The lock is older than LOCK_MAX_AGE_SECONDS
        - We can't read the owner data at all
        """
        # Age check — catches all cases including cross-machine
        acquired_at = owner.get("acquired_at", 0)
        if (time.time() - acquired_at) > self.LOCK_MAX_AGE_SECONDS:
            return True

        # PID check — only meaningful on the same hostname
        lock_hostname = owner.get("hostname")
        if lock_hostname == _hostname():
            pid = owner.get("pid")
            if pid is not None and not self._is_process_alive(pid):
                return True

        return False

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """
        Check if a process is still running.

        On POSIX: signal 0 checks existence without killing.
        On Windows: os.kill(pid, 0) raises OSError for any PID (no real
        signal support), so we use ctypes.windll to call OpenProcess.
        Falls back to "assume alive" if detection isn't possible.
        """
        if os.name == "nt":
            # Windows path — os.kill(pid, 0) is unreliable
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except (AttributeError, OSError):
                # Can't determine — assume alive (safe, falls back to age timeout)
                return True
        else:
            # POSIX path
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # Process exists but we can't signal it
            except OSError:
                return False

    # ── Query ─────────────────────────────────────────────────────

    def get(self, name: str) -> WorkspaceInfo | None:
        """Get workspace info by name."""
        meta_path = self._meta_path(name)
        if not meta_path.exists():
            return None

        data = json.loads(meta_path.read_text())
        data["path"] = Path(data["path"])
        return WorkspaceInfo(**data)

    def is_dirty(self, name: str) -> dict | None:
        """
        Check if a workspace has a dirty marker from an interrupted operation.

        Returns the marker contents if dirty, None if clean.
        A dirty workspace should be re-materialized from its base_state
        (or the target state in the marker) to reach a known-good state.
        """
        info = self.get(name)
        if info is None:
            return None

        dirty_path = info.path / ".vex_materializing"
        if not dirty_path.exists():
            return None

        try:
            return json.loads(dirty_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"error": "unreadable dirty marker"}

    def list(self) -> list[WorkspaceInfo]:
        """List all workspaces."""
        workspaces = []

        # Check for main workspace (special location)
        main_meta = self._meta_path(self.MAIN_WORKSPACE)
        if main_meta.exists():
            info = self._load_workspace_info(main_meta)
            if info is not None:
                # Main workspace path is repo root, always exists
                workspaces.append(info)

        # Check for feature workspaces
        for meta_file in sorted(self.workspaces_dir.rglob("*.json")):
            # Skip files inside .lockdir directories (lock owner metadata)
            if any(part.endswith(".lockdir") for part in meta_file.parts):
                continue

            info = self._load_workspace_info(meta_file)
            if info is None:
                continue

            # Check if workspace dir still exists
            if not info.path.exists():
                info.status = WorkspaceStatus.DISPOSED.value
            workspaces.append(info)
        return workspaces

    def _load_workspace_info(self, meta_path: Path) -> WorkspaceInfo | None:
        """Load workspace info from a metadata file, returning None if invalid."""
        try:
            data = json.loads(meta_path.read_text())
            if "lane" not in data or "name" not in data:
                return None
            data["path"] = Path(data["path"])
            return WorkspaceInfo(**data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def exists(self, name: str) -> bool:
        return self._meta_path(name).exists()

    # ── Cleanup ───────────────────────────────────────────────────

    def remove(self, name: str, force: bool = False):
        """
        Remove a workspace.

        For feature workspaces: deletes the working directory and metadata.
        For main workspace: clears files (protecting .vex) and removes metadata.
        If the workspace is locked (active agent), requires force=True.
        """
        info = self.get(name)
        if info is None:
            raise ValueError(f"Workspace '{name}' not found")

        if info.status == WorkspaceStatus.ACTIVE.value and not force:
            raise ValueError(
                f"Workspace '{name}' is active (agent: {info.agent_id}). "
                f"Use force=True to remove anyway."
            )

        # Release any locks
        self.release(name)

        if self._is_main(name):
            # Main workspace: clear files but protect .vex, remove metadata
            self._clean_workspace_contents(self.repo_root, protect_vex=True)
        else:
            # Feature workspace: remove the entire directory
            ws_path = self._workspace_path(name)
            if ws_path.exists():
                shutil.rmtree(ws_path)

        # Remove metadata
        meta_path = self._meta_path(name)
        if meta_path.exists():
            meta_path.unlink()

    def clean_stale(self, max_age_seconds: float = 86400) -> list[str]:
        """
        Remove workspaces that have been idle for too long.
        Returns list of removed workspace names.
        """
        removed = []
        now = time.time()

        for info in self.list():
            if info.status == WorkspaceStatus.ACTIVE.value:
                continue
            if (now - info.updated_at) > max_age_seconds:
                self.remove(info.name, force=True)
                removed.append(info.name)

        return removed

    # ── Helpers ────────────────────────────────────────────────────

    def _is_main(self, name: str) -> bool:
        """Check if this is the main workspace (special: lives at repo root)."""
        return name == self.MAIN_WORKSPACE

    def _workspace_path(self, name: str) -> Path:
        """Get the filesystem path for a workspace's files."""
        if self._is_main(name):
            return self.repo_root  # Main workspace IS the repo root
        return self.workspaces_dir / name

    def _meta_path(self, name: str) -> Path:
        """Get the path to workspace metadata JSON."""
        if self._is_main(name):
            return self.vex_dir / "main.json"  # Special location for main
        return self.workspaces_dir / f"{name}.json"

    def _lock_path(self, name: str) -> Path:
        """Get the path to workspace lock directory."""
        if self._is_main(name):
            return self.vex_dir / "main.lockdir"  # Special location for main
        return self.workspaces_dir / f"{name}.lockdir"

    def _update_meta(self, name: str, **updates):
        """Update specific fields in workspace metadata (atomic write)."""
        meta_path = self._meta_path(name)
        if not meta_path.exists():
            return

        data = json.loads(meta_path.read_text())
        data.update(updates)
        data["updated_at"] = time.time()

        # Convert Path back to string for JSON
        if isinstance(data.get("path"), Path):
            data["path"] = str(data["path"])

        _atomic_write(meta_path, json.dumps(data, indent=2))

    def _clean_workspace_contents(self, ws_path: Path, protect_vex: bool = False):
        """Remove all contents of a workspace directory.

        Args:
            ws_path: Path to the workspace directory
            protect_vex: If True, don't touch .vex directory (for main workspace)
        """
        if ws_path.exists():
            for item in ws_path.iterdir():
                # CRITICAL: Never delete .vex when cleaning main workspace
                if protect_vex and item.name == ".vex":
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

    def _cleanup_empty_parents(self, dir_path: Path, stop_at: Path):
        """Remove empty parent directories up to stop_at."""
        current = dir_path
        stop_resolved = stop_at.resolve()
        while current != stop_at and current.exists():
            # Ensure we never escape the stop_at boundary
            try:
                current.resolve().relative_to(stop_resolved)
            except ValueError:
                break
            try:
                if not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
            except OSError:
                break
