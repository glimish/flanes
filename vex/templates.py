"""
Workspace Templates

Templates stored as JSON in .vex/templates/<name>.json.
A template captures a set of files, directories, and vexignore
patterns that can be applied when creating a new workspace.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TemplateFile:
    """A file in a template."""
    path: str
    content: str | None = None
    source_hash: str | None = None

    def to_dict(self) -> dict:
        d = {"path": self.path}
        if self.content is not None:
            d["content"] = self.content
        if self.source_hash is not None:
            d["source_hash"] = self.source_hash
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TemplateFile":
        return cls(
            path=d["path"],
            content=d.get("content"),
            source_hash=d.get("source_hash"),
        )


@dataclass
class WorkspaceTemplate:
    """A workspace template."""
    name: str
    description: str = ""
    files: list = field(default_factory=list)
    directories: list = field(default_factory=list)
    vexignore_patterns: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "files": [f.to_dict() if isinstance(f, TemplateFile) else f for f in self.files],
            "directories": self.directories,
            "vexignore_patterns": self.vexignore_patterns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkspaceTemplate":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            files=[TemplateFile.from_dict(f) for f in d.get("files", [])],
            directories=d.get("directories", []),
            vexignore_patterns=d.get("vexignore_patterns", []),
        )


def _validate_name(name: str):
    """Validate a template name contains no path traversal characters."""
    if not name or ".." in name or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(f"Invalid template name: {name!r}")


def _validate_path_within(base: Path, target: Path):
    """Validate that target resolves inside base directory."""
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {target} is outside {base}"
        )


class TemplateManager:
    """Manages workspace templates stored in .vex/templates/."""

    def __init__(self, vex_dir: Path):
        self.templates_dir = vex_dir / "templates"

    def save(self, template: WorkspaceTemplate) -> Path:
        """Save a template to disk."""
        _validate_name(template.name)
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        path = self.templates_dir / f"{template.name}.json"
        path.write_text(json.dumps(template.to_dict(), indent=2))
        return path

    def load(self, name: str) -> WorkspaceTemplate | None:
        """Load a template by name. Returns None if not found or corrupted."""
        _validate_name(name)
        path = self.templates_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return WorkspaceTemplate.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Corrupted template %s: %s", name, e)
            return None

    def list(self) -> list:
        """List all templates."""
        if not self.templates_dir.exists():
            return []
        templates = []
        for path in sorted(self.templates_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                templates.append(WorkspaceTemplate.from_dict(data))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping corrupted template %s: %s", path.name, e)
                continue
        return templates

    def delete(self, name: str) -> bool:
        """Delete a template by name."""
        _validate_name(name)
        path = self.templates_dir / f"{name}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def apply(self, template: WorkspaceTemplate, workspace_path: Path, store=None):
        """Apply a template to a workspace directory.

        Creates files, directories, and .vexignore as specified.
        If store is provided, resolves source_hash references from the CAS.
        """
        # Create directories
        for dir_path in template.directories:
            target = workspace_path / dir_path
            _validate_path_within(workspace_path, target)
            target.mkdir(parents=True, exist_ok=True)

        # Create files
        for tf in template.files:
            file_path = workspace_path / tf.path
            _validate_path_within(workspace_path, file_path)
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if tf.content is not None:
                file_path.write_text(tf.content)
            elif tf.source_hash is not None and store is not None:
                obj = store.retrieve(tf.source_hash)
                if obj is not None:
                    file_path.write_bytes(obj.data)
                else:
                    logger.warning(
                        "Template file %s references hash %s but blob not found in store",
                        tf.path, tf.source_hash,
                    )
            elif tf.source_hash is not None and store is None:
                logger.warning(
                    "Template file %s references hash %s but no store provided",
                    tf.path, tf.source_hash,
                )
            else:
                logger.warning(
                    "Template file %s has neither content nor source_hash, skipping",
                    tf.path,
                )

        # Write .vexignore
        if template.vexignore_patterns:
            vexignore_path = workspace_path / ".vexignore"
            vexignore_path.write_text("\n".join(template.vexignore_patterns) + "\n")
