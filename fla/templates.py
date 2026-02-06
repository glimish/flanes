"""
Workspace Templates

Templates stored as JSON in .fla/templates/<name>.json.
A template captures a set of files, directories, and flaignore
patterns that can be applied when creating a new workspace.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .serializable import Serializable

logger = logging.getLogger(__name__)


@dataclass
class TemplateFile(Serializable):
    """A file in a template."""
    _skip_none = True

    path: str
    content: str | None = None
    source_hash: str | None = None


@dataclass
class WorkspaceTemplate(Serializable):
    """A workspace template."""
    name: str
    description: str = ""
    files: list[TemplateFile] = field(default_factory=list)
    directories: list = field(default_factory=list)
    flaignore_patterns: list = field(default_factory=list)


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
    """Manages workspace templates stored in .fla/templates/."""

    def __init__(self, fla_dir: Path):
        self.templates_dir = fla_dir / "templates"

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

        Creates files, directories, and .flaignore as specified.
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

        # Write .flaignore
        if template.flaignore_patterns:
            flaignore_path = workspace_path / ".flaignore"
            flaignore_path.write_text("\n".join(template.flaignore_patterns) + "\n")
