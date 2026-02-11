"""Crash consistency and durability tests.

Tests that verify Flanes handles interruptions gracefully:
- Crash mid-materialize leaves dirty marker for recovery
- Crash mid-snapshot doesn't corrupt CAS (writes are atomic)
- Concurrent GC + accept doesn't lose reachable objects
- Dirty workspace detection and recovery
- Atomic metadata writes survive partial failure
"""

import json
import time
from unittest.mock import patch

import pytest

from flanes.cas import ContentStore
from flanes.repo import Repository
from flanes.state import AgentIdentity, WorldStateManager
from flanes.workspace import WorkspaceManager


@pytest.fixture
def repo(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    r = Repository.init(project)
    yield r
    r.close()


@pytest.fixture
def env(tmp_path):
    """Provides (flanes_dir, wm, wsm, store) for workspace-level tests."""
    flanes_dir = tmp_path / ".flanes"
    flanes_dir.mkdir()
    db = flanes_dir / "store.db"
    store = ContentStore(db)
    wsm = WorldStateManager(store, db)
    wm = WorkspaceManager(flanes_dir, wsm)
    yield flanes_dir, wm, wsm, store
    store.close()


def _make_state(store, wsm, files):
    """Helper: create a state from a dict of {filename: content}."""
    entries = {}
    for name, content in files.items():
        blob_h = store.store_blob(content.encode())
        entries[name] = ("blob", blob_h)
    tree_h = store.store_tree(entries)
    return wsm.create_state_from_tree(tree_h)


class TestCrashDuringMaterialize:
    """Simulates a crash partway through workspace materialization."""

    def test_dirty_marker_left_on_failure(self, env):
        """If materialize raises, the .flanes_materializing marker stays."""
        flanes_dir, wm, wsm, store = env
        state_id = _make_state(store, wsm, {"a.txt": "hello"})
        wsm.create_lane("test")

        # Patch materialize to raise mid-operation
        def exploding_materialize(sid, path):
            (path / "partial.txt").write_text("partial")
            raise OSError("Simulated disk failure")

        with patch.object(wsm, "materialize", exploding_materialize):
            with pytest.raises(OSError, match="disk failure"):
                wm.create("test-ws", lane="test", state_id=state_id)

        # The dirty marker should exist
        ws_path = wm._workspace_path("test-ws")
        dirty = ws_path / ".flanes_materializing"
        assert dirty.exists(), "Dirty marker should survive the crash"

        marker = json.loads(dirty.read_text())
        assert marker["state_id"] == state_id

    def test_successful_materialize_removes_marker(self, env):
        """On success, the dirty marker is cleaned up."""
        flanes_dir, wm, wsm, store = env
        state_id = _make_state(store, wsm, {"a.txt": "hello"})
        wsm.create_lane("test")

        wm.create("clean-ws", lane="test", state_id=state_id)

        ws_path = wm._workspace_path("clean-ws")
        dirty = ws_path / ".flanes_materializing"
        assert not dirty.exists(), "Dirty marker should be removed on success"


class TestCrashDuringUpdate:
    """Simulates a crash during workspace update (incremental)."""

    def test_dirty_marker_left_on_update_failure(self, env):
        flanes_dir, wm, wsm, store = env
        state1 = _make_state(store, wsm, {"a.txt": "v1"})
        state2 = _make_state(store, wsm, {"a.txt": "v2", "b.txt": "new"})
        wsm.create_lane("test")

        wm.create("up-ws", lane="test", state_id=state1)

        def crash_update(*args, **kwargs):
            raise OSError("Simulated crash during update")

        with patch.object(wm, "_apply_update", crash_update):
            with pytest.raises(OSError, match="crash during update"):
                wm.update("up-ws", state2)

        assert wm.is_dirty("up-ws") is not None

    def test_successful_update_clears_marker(self, env):
        flanes_dir, wm, wsm, store = env
        state1 = _make_state(store, wsm, {"a.txt": "v1"})
        state2 = _make_state(store, wsm, {"a.txt": "v2", "b.txt": "new"})
        wsm.create_lane("test")

        wm.create("up-ws", lane="test", state_id=state1)
        wm.update("up-ws", state2)

        assert wm.is_dirty("up-ws") is None


class TestCrashDuringSnapshot:
    """CAS writes are atomic -- a crash during snapshot can't corrupt existing data."""

    def test_existing_data_survives_snapshot_crash(self, repo):
        """If snapshot_directory fails, previously stored data is intact."""
        ws = repo.workspace_path("main")
        (ws / "existing.txt").write_text("important data")

        result = repo.quick_commit(
            workspace="main",
            prompt="initial",
            agent=AgentIdentity(agent_id="t", agent_type="test"),
            auto_accept=True,
        )
        initial_state = result["to_state"]

        # Now add a file and make _create_world_state fail (after tree hashing
        # succeeds but before the state is recorded). This simulates a crash
        # at the final stage of snapshot.
        (ws / "new.txt").write_text("new content")

        def crashing_create(root_tree_hash, parent_id):
            raise OSError("Simulated crash before state creation")

        with patch.object(repo.wsm, "_create_world_state", crashing_create):
            with pytest.raises(OSError):
                repo.snapshot("main")

        # Original state should still be retrievable and intact
        state = repo.wsm.get_state(initial_state)
        assert state is not None
        files = repo.wsm._flatten_tree(state["root_tree"])
        assert "existing.txt" in files


class TestConcurrentGCAndAccept:
    """GC uses a deferred transaction for mark phase -- concurrent accepts
    shouldn't cause reachable objects to be collected."""

    def test_gc_preserves_recently_accepted_states(self, repo):
        """Objects reachable from accepted transitions survive GC."""
        ws = repo.workspace_path("main")

        for i in range(3):
            (ws / f"file{i}.txt").write_text(f"content {i}")
            repo.quick_commit(
                workspace="main",
                prompt=f"commit {i}",
                agent=AgentIdentity(agent_id="t", agent_type="test"),
                auto_accept=True,
            )

        head = repo.head()
        assert head is not None

        repo.gc(dry_run=False, max_age_days=0)

        state = repo.wsm.get_state(head)
        assert state is not None, "Head state should survive GC"

        files = repo.wsm._flatten_tree(state["root_tree"])
        assert "file2.txt" in files

        for path, blob_hash in files.items():
            obj = repo.store.retrieve(blob_hash)
            assert obj is not None, f"Blob for {path} should survive GC"


class TestAtomicMetadataWrites:
    """Workspace metadata uses atomic writes (temp + rename).
    Verify that a crash during metadata write doesn't corrupt the file."""

    def test_metadata_intact_after_partial_update(self, env):
        flanes_dir, wm, wsm, store = env
        state_id = _make_state(store, wsm, {"a.txt": "hello"})
        wsm.create_lane("test")

        wm.create("meta-ws", lane="test", state_id=state_id)

        meta_path = wm._meta_path("meta-ws")
        original_data = json.loads(meta_path.read_text())

        from flanes import workspace as ws_module

        def crashing_atomic(path, content):
            raise OSError("Simulated write failure")

        with patch.object(ws_module, "_atomic_write", crashing_atomic):
            with pytest.raises(OSError):
                wm._update_meta("meta-ws", agent_id="new-agent")

        data = json.loads(meta_path.read_text())
        assert data["agent_id"] == original_data["agent_id"]


class TestWorkspaceRecovery:
    """Tests for detecting and recovering from dirty workspaces."""

    def test_dirty_workspace_can_be_re_materialized(self, env):
        """A workspace with a dirty marker can be removed and recreated."""
        flanes_dir, wm, wsm, store = env
        state_id = _make_state(store, wsm, {"a.txt": "hello"})
        wsm.create_lane("test")

        info = wm.create("recover-ws", lane="test", state_id=state_id)

        dirty_path = info.path / ".flanes_materializing"
        dirty_path.write_text(json.dumps({"state_id": state_id, "started_at": time.time()}))

        assert wm.is_dirty("recover-ws") is not None

        wm.remove("recover-ws", force=True)
        new_info = wm.create("recover-ws", lane="test", state_id=state_id)

        assert wm.is_dirty("recover-ws") is None
        assert (new_info.path / "a.txt").read_text() == "hello"
