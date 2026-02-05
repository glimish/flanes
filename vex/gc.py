"""
Garbage Collector

Mark-and-sweep garbage collection for the Vex content store.
Removes unreachable objects (blobs, trees) and expired transitions
to reclaim storage space.

GC is never automatic — it must be explicitly invoked.
"""

import json
import time
from dataclasses import dataclass

from .cas import ContentStore, ObjectType
from .state import WorldStateManager


@dataclass
class GCResult:
    reachable_objects: int
    deleted_objects: int
    deleted_bytes: int
    deleted_states: int
    deleted_transitions: int
    pruned_cache_entries: int  # Fix #4: Track pruned stat cache entries
    dry_run: bool
    elapsed_ms: float

    def to_dict(self) -> dict:
        return {
            "reachable_objects": self.reachable_objects,
            "deleted_objects": self.deleted_objects,
            "deleted_bytes": self.deleted_bytes,
            "deleted_states": self.deleted_states,
            "deleted_transitions": self.deleted_transitions,
            "pruned_cache_entries": self.pruned_cache_entries,
            "dry_run": self.dry_run,
            "elapsed_ms": self.elapsed_ms,
        }


def _mark_phase(conn, store, max_age_days):
    """Collect reachable hashes and live states within a read transaction.

    Returns (reachable_hashes, all_live_states, cutoff_timestamp).
    Caller is responsible for BEGIN/COMMIT around this call.
    """
    # 1. Collect live state IDs
    live_state_ids = set()

    # From lanes: head_state and fork_base
    for row in conn.execute("SELECT head_state, fork_base FROM lanes"):
        if row[0]:
            live_state_ids.add(row[0])
        if row[1]:
            live_state_ids.add(row[1])

    # From non-rejected transitions: from_state and to_state
    for row in conn.execute(
        "SELECT from_state, to_state FROM transitions WHERE status != 'rejected'"
    ):
        if row[0]:
            live_state_ids.add(row[0])
        if row[1]:
            live_state_ids.add(row[1])

    # Also keep states from recent rejected transitions (within age threshold)
    cutoff = time.time() - (max_age_days * 86400)
    for row in conn.execute(
        """SELECT from_state, to_state FROM transitions
           WHERE status = 'rejected' AND created_at >= ?""",
        (cutoff,)
    ):
        if row[0]:
            live_state_ids.add(row[0])
        if row[1]:
            live_state_ids.add(row[1])

    # 2. Walk parent chains from all live states to find full lineage
    all_live_states = set(live_state_ids)
    frontier = list(live_state_ids)
    while frontier:
        batch = frontier[:500]
        frontier = frontier[500:]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT parent_id FROM world_states WHERE id IN ({placeholders})",
            batch,
        ):
            if row[0] and row[0] not in all_live_states:
                all_live_states.add(row[0])
                frontier.append(row[0])

    # 3. Collect reachable object hashes from live states' root trees
    reachable_hashes = set()

    root_trees = set()
    for state_id in all_live_states:
        row = conn.execute(
            "SELECT root_tree FROM world_states WHERE id = ?", (state_id,)
        ).fetchone()
        if row and row[0]:
            root_trees.add(row[0])

    # Walk all trees recursively to collect tree + blob hashes
    tree_frontier = list(root_trees)
    visited_trees = set()
    while tree_frontier:
        tree_hash = tree_frontier.pop()
        if tree_hash in visited_trees:
            continue
        visited_trees.add(tree_hash)
        reachable_hashes.add(tree_hash)

        obj = store.retrieve(tree_hash)
        if obj is None or obj.type != ObjectType.TREE:
            continue
        try:
            entries = json.loads(obj.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for _name, entry in entries:
            # Handle both old (type, hash) and new (type, hash, mode) formats
            typ, h = entry[0], entry[1]
            reachable_hashes.add(h)
            if typ == "tree":
                tree_frontier.append(h)

    return reachable_hashes, all_live_states, cutoff


def collect_garbage(
    store: ContentStore,
    wsm: WorldStateManager,
    dry_run: bool = True,
    max_age_days: int = 30,
) -> GCResult:
    """
    Run mark-and-sweep garbage collection.

    Mark phase: find all reachable object hashes by walking from
    live lane heads/fork_bases and non-rejected transitions.

    Sweep phase: delete unreachable objects and expired transitions.
    """
    start = time.monotonic()
    conn = store.conn

    # ── Mark Phase ────────────────────────────────────────────────
    # Use a deferred transaction to get a consistent snapshot of
    # the database during the mark phase. Without this, concurrent
    # accepts could advance lane heads mid-scan, causing the mark
    # phase to miss reachable objects.
    conn.execute("BEGIN DEFERRED")
    try:
        reachable_hashes, all_live_states, cutoff = _mark_phase(conn, store, max_age_days)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

    # ── Sweep Phase ───────────────────────────────────────────────

    # Count what would be deleted
    all_hashes = set()
    hash_sizes = {}
    for row in conn.execute("SELECT hash, size, location FROM objects"):
        all_hashes.add(row[0])
        hash_sizes[row[0]] = (row[1], row[2])  # (size, location)

    unreachable = all_hashes - reachable_hashes
    deleted_bytes = sum(hash_sizes[h][0] for h in unreachable)

    # Find transitions to delete (rejected/superseded older than max_age_days)
    expired_transitions = conn.execute(
        "SELECT id FROM transitions WHERE status IN ('rejected', 'superseded') AND created_at < ?",
        (cutoff,)
    ).fetchall()
    deletable_transition_ids = [row[0] for row in expired_transitions]

    # Find orphan states (not referenced by any transition or lane after sweep)
    # We'll compute this after deleting transitions
    # For now, count states not in all_live_states
    all_state_ids = {row[0] for row in conn.execute("SELECT id FROM world_states")}
    orphan_states = all_state_ids - all_live_states

    # Fix #4: Count stale cache entries for dry run
    stale_cache_dry_run = 0
    for row in conn.execute("SELECT blob_hash FROM stat_cache"):
        if row[0] in unreachable:
            stale_cache_dry_run += 1

    if dry_run:
        elapsed = (time.monotonic() - start) * 1000
        return GCResult(
            reachable_objects=len(reachable_hashes),
            deleted_objects=len(unreachable),
            deleted_bytes=deleted_bytes,
            deleted_states=len(orphan_states),
            deleted_transitions=len(deletable_transition_ids),
            pruned_cache_entries=stale_cache_dry_run,
            dry_run=True,
            elapsed_ms=elapsed,
        )

    # Fix #4: Prune stat cache entries for deleted blobs
    # Count entries whose blob_hash is no longer in the store
    stale_cache_count = 0
    for row in conn.execute("SELECT path, blob_hash FROM stat_cache"):
        if row[1] in unreachable:
            stale_cache_count += 1

    # Actually delete — DB changes in batch, filesystem after commit
    # Collect fs blobs to delete after DB transaction succeeds
    fs_blobs_to_delete = []
    for h in unreachable:
        _size, location = hash_sizes[h]
        if location == "fs":
            fs_blobs_to_delete.append(h)

    with store.batch():
        # Delete unreachable objects from DB
        for h in unreachable:
            conn.execute("DELETE FROM objects WHERE hash = ?", (h,))

        # Delete expired transitions
        for tid in deletable_transition_ids:
            conn.execute("DELETE FROM transitions WHERE id = ?", (tid,))

        # Delete orphaned intents (not referenced by any remaining transition)
        conn.execute("""
            DELETE FROM intents WHERE id NOT IN (
                SELECT DISTINCT intent_id FROM transitions
            )
        """)

        # Delete orphan states
        for sid in orphan_states:
            conn.execute("DELETE FROM world_states WHERE id = ?", (sid,))

        # Fix #4: Prune stale stat cache entries
        # Delete entries whose blob_hash references a deleted object
        if unreachable:
            placeholders = ",".join("?" for _ in unreachable)
            conn.execute(
                f"DELETE FROM stat_cache WHERE blob_hash IN ({placeholders})",
                list(unreachable)
            )

    # Delete filesystem blobs after DB transaction committed successfully
    for h in fs_blobs_to_delete:
        store.delete_fs_blob(h)

    elapsed = (time.monotonic() - start) * 1000
    return GCResult(
        reachable_objects=len(reachable_hashes),
        deleted_objects=len(unreachable),
        deleted_bytes=deleted_bytes,
        deleted_states=len(orphan_states),
        deleted_transitions=len(deletable_transition_ids),
        pruned_cache_entries=stale_cache_count,
        dry_run=False,
        elapsed_ms=elapsed,
    )
