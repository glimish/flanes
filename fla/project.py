"""
Multi-Repo Project Management

A project coordinates multiple fla repos under a single root.
Configuration is stored in .fla-project.json at the project root.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .serializable import Serializable

logger = logging.getLogger(__name__)

PROJECT_FILE = ".fla-project.json"


@dataclass
class RepoMount(Serializable):
    """A fla repo mounted in the project."""
    repo_path: str
    mount_point: str
    lane: str = "main"


@dataclass
class ProjectConfig(Serializable):
    """Configuration for a multi-repo project."""
    name: str
    repos: list[RepoMount] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class Project:
    """Manages a multi-repo project."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config_path = self.root / PROJECT_FILE
        if not self.config_path.exists():
            raise ValueError(f"Not a fla project: {self.root} (no {PROJECT_FILE})")
        self.config = ProjectConfig.from_dict(
            json.loads(self.config_path.read_text())
        )

    @classmethod
    def init(cls, path: Path, name: str | None = None) -> "Project":
        """Initialize a new project at the given path."""
        root = Path(path).resolve()
        config_path = root / PROJECT_FILE
        if config_path.exists():
            raise ValueError(f"Project already exists at {root}")

        config = ProjectConfig(
            name=name or root.name,
            repos=[],
            created_at=time.time(),
        )
        root.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config.to_dict(), indent=2))
        return cls(root)

    @classmethod
    def find(cls, start_path: Path | None = None) -> "Project":
        """Find a project by walking up from the given path."""
        path = (start_path or Path.cwd()).resolve()
        while path != path.parent:
            if (path / PROJECT_FILE).exists():
                return cls(path)
            path = path.parent
        raise ValueError(f"No project found (searched from {start_path or Path.cwd()})")

    def _save(self):
        """Save config to disk."""
        self.config_path.write_text(json.dumps(self.config.to_dict(), indent=2))

    def add_repo(self, repo_path: str, mount_point: str, lane: str = "main"):
        """Add a repo to the project."""
        # Check for duplicate mount points
        for r in self.config.repos:
            if r.mount_point == mount_point:
                raise ValueError(f"Mount point '{mount_point}' already exists")

        mount = RepoMount(repo_path=repo_path, mount_point=mount_point, lane=lane)
        self.config.repos.append(mount)
        self._save()

    def remove_repo(self, mount_point: str):
        """Remove a repo from the project."""
        self.config.repos = [r for r in self.config.repos if r.mount_point != mount_point]
        self._save()

    def status(self) -> dict:
        """Get aggregated status across all repos."""
        from .repo import Repository

        statuses = {}
        for mount in self.config.repos:
            repo_path = self.root / mount.repo_path
            try:
                with Repository(repo_path) as repo:
                    statuses[mount.mount_point] = {
                        "repo_path": str(repo_path),
                        "lane": mount.lane,
                        "head": repo.head(mount.lane),
                        "status": "ok",
                    }
            except Exception as e:
                statuses[mount.mount_point] = {
                    "repo_path": str(repo_path),
                    "lane": mount.lane,
                    "head": None,
                    "status": f"error: {e}",
                }

        return {
            "project": self.config.name,
            "root": str(self.root),
            "repos": statuses,
        }

    def coordinated_snapshot(self) -> dict:
        """Snapshot all repos in the project."""
        from .repo import Repository

        snapshots = {}
        for mount in self.config.repos:
            repo_path = self.root / mount.repo_path
            try:
                with Repository(repo_path) as repo:
                    # Find the default workspace for this lane
                    workspaces = repo.workspaces()
                    ws_name = None
                    for ws in workspaces:
                        if ws.lane == mount.lane:
                            ws_name = ws.name
                            break
                    if ws_name:
                        state_id = repo.snapshot(ws_name)
                        snapshots[mount.mount_point] = {
                            "state_id": state_id,
                            "status": "ok",
                        }
                    else:
                        snapshots[mount.mount_point] = {
                            "state_id": None,
                            "status": "no workspace for lane",
                        }
            except Exception as e:
                snapshots[mount.mount_point] = {
                    "state_id": None,
                    "status": f"error: {e}",
                }

        return {
            "project": self.config.name,
            "snapshots": snapshots,
        }

    def close(self):
        """No-op for interface consistency."""
        pass
