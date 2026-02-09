"""
Plugin Discovery

Discovers and loads plugins via Python entry points (importlib.metadata).
Three plugin groups are supported:

- ``flanes.evaluators``  — Python callable evaluators
- ``flanes.storage``     — Remote storage backends
- ``flanes.hooks``       — Lifecycle hooks (pre/post propose, accept, reject)
"""

import logging
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

# Entry point group names
EVALUATOR_GROUP = "flanes.evaluators"
STORAGE_GROUP = "flanes.storage"
HOOK_GROUP = "flanes.hooks"


def discover(group: str) -> dict:
    """Discover all entry points for a given group.

    Returns a dict mapping entry point names to loaded objects.
    Invalid entry points are logged and skipped.
    """
    plugins = {}
    eps = entry_points()

    # Python 3.12+ returns a SelectableGroups, 3.10-3.11 returns a dict
    if hasattr(eps, "select"):
        selected = eps.select(group=group)
    elif isinstance(eps, dict):
        selected = eps.get(group, ())  # type: ignore[arg-type]
    else:
        selected = ()

    for ep in selected:
        try:
            obj = ep.load()
            plugins[ep.name] = obj
            logger.debug("Loaded plugin %s:%s", group, ep.name)
        except Exception as e:
            logger.warning("Failed to load plugin %s:%s: %s", group, ep.name, e)

    return plugins


def discover_evaluators() -> dict:
    """Discover evaluator plugins.

    Each entry point should resolve to a callable with signature:
        (workspace_path: Path) -> EvaluatorResult
    """
    return discover(EVALUATOR_GROUP)


def discover_storage_backends() -> dict:
    """Discover storage backend plugins.

    Each entry point should resolve to a callable (factory) with signature:
        (config: dict) -> RemoteBackend
    """
    return discover(STORAGE_GROUP)


def discover_hooks() -> dict:
    """Discover hook plugins.

    Each entry point should resolve to a callable with signature:
        (event: str, context: dict) -> None

    Hook names should follow the pattern: ``pre_propose``, ``post_accept``, etc.
    """
    return discover(HOOK_GROUP)
