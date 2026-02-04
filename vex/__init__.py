"""
Vex — Version Control for Agentic AI Systems

A version controller designed from the ground up for AI agents,
replacing git's line-diff model with intent-based snapshots,
lane isolation, and first-class evaluation gating.
"""

__version__ = "0.2.0"

__all__ = [
    # Core
    "Repository",
    "NotARepository",
    # Agent SDK
    "AgentSession",
    "WorkContext",
    # Content-addressed store
    "ContentStore",
    "CASObject",
    "ObjectType",
    "ContentStoreLimitError",
    # State management
    "WorldStateManager",
    "AgentIdentity",
    "CostRecord",
    "EvaluationResult",
    "TransitionStatus",
    "TreeDepthLimitError",
    # Garbage collection
    "GCResult",
    "collect_garbage",
]

# Lazy imports — only resolve when accessed
def __getattr__(name):
    if name in ("Repository", "NotARepository"):
        from .repo import Repository, NotARepository
        return Repository if name == "Repository" else NotARepository
    if name in ("AgentSession", "WorkContext"):
        from .agent_sdk import AgentSession, WorkContext
        return AgentSession if name == "AgentSession" else WorkContext
    if name in ("ContentStore", "CASObject", "ObjectType", "ContentStoreLimitError"):
        from .cas import ContentStore, CASObject, ObjectType, ContentStoreLimitError
        return {"ContentStore": ContentStore, "CASObject": CASObject,
                "ObjectType": ObjectType, "ContentStoreLimitError": ContentStoreLimitError}[name]
    if name in ("WorldStateManager", "AgentIdentity", "CostRecord",
                "EvaluationResult", "TransitionStatus", "TreeDepthLimitError"):
        from .state import (WorldStateManager, AgentIdentity, CostRecord,
                           EvaluationResult, TransitionStatus, TreeDepthLimitError)
        return {"WorldStateManager": WorldStateManager, "AgentIdentity": AgentIdentity,
                "CostRecord": CostRecord, "EvaluationResult": EvaluationResult,
                "TransitionStatus": TransitionStatus, "TreeDepthLimitError": TreeDepthLimitError}[name]
    if name in ("GCResult", "collect_garbage"):
        from .gc import GCResult, collect_garbage
        return GCResult if name == "GCResult" else collect_garbage
    raise AttributeError(f"module 'vex' has no attribute {name!r}")
