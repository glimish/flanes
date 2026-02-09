"""WorkspaceManager unit tests."""

import json
import os
import time

import pytest

from flanes.cas import ContentStore
from flanes.state import WorldStateManager
from flanes.workspace import WorkspaceManager


@pytest.fixture
def env(tmp_path):
    """Provides (flanes_dir, wm, wsm, store)."""
    flanes_dir = tmp_path / ".flanes"
    flanes_dir.mkdir()
    db = flanes_dir / "store.db"
    store = ContentStore(db)
    wsm = WorldStateManager(store, db)
    wm = WorkspaceManager(flanes_dir, wsm)
    yield flanes_dir, wm, wsm, store
    store.close()


def _create_workspace(wm, wsm, store, name="ws1", lane="main"):
    """Helper: create a workspace backed by a real state."""
    blob_h = store.store_blob(b"content")
    tree_h = store.store_tree({"file.txt": ("blob", blob_h)})
    state_id = wsm.create_state_from_tree(tree_h)
    try:
        wsm.create_lane(lane)
    except Exception:
        pass
    return wm.create(name, lane=lane, state_id=state_id)


class TestLockHolder:
    def test_returns_none_when_unlocked(self, env):
        flanes_dir, wm, wsm, store = env
        _create_workspace(wm, wsm, store)
        assert wm.lock_holder("ws1") is None

    def test_returns_owner_when_locked(self, env):
        flanes_dir, wm, wsm, store = env
        _create_workspace(wm, wsm, store)
        wm.acquire("ws1", "agent-1")
        holder = wm.lock_holder("ws1")
        assert holder is not None
        assert holder["agent_id"] == "agent-1"
        wm.release("ws1")


class TestIsDirty:
    def test_returns_none_when_clean(self, env):
        flanes_dir, wm, wsm, store = env
        _create_workspace(wm, wsm, store)
        assert wm.is_dirty("ws1") is None

    def test_returns_marker_when_materializing(self, env):
        flanes_dir, wm, wsm, store = env
        info = _create_workspace(wm, wsm, store)
        marker = {"state_id": "s123", "started_at": time.time()}
        (info.path / ".flanes_materializing").write_text(json.dumps(marker))
        result = wm.is_dirty("ws1")
        assert result is not None
        assert result["state_id"] == "s123"

    def test_returns_error_for_corrupt_marker(self, env):
        flanes_dir, wm, wsm, store = env
        info = _create_workspace(wm, wsm, store)
        (info.path / ".flanes_materializing").write_text("NOT JSON{{{")
        result = wm.is_dirty("ws1")
        assert result is not None
        assert "error" in result


class TestCleanStale:
    def test_removes_old_idle_skips_active(self, env):
        flanes_dir, wm, wsm, store = env
        # Create two workspaces
        _create_workspace(wm, wsm, store, name="old-idle", lane="lane1")
        _create_workspace(wm, wsm, store, name="active-ws", lane="lane2")

        # Make old-idle look old by backdating updated_at
        meta_path = wm._meta_path("old-idle")
        data = json.loads(meta_path.read_text())
        data["updated_at"] = time.time() - 200
        meta_path.write_text(json.dumps(data))

        # Make active-ws active
        wm.acquire("active-ws", "agent-1")

        removed = wm.clean_stale(max_age_seconds=100)
        assert "old-idle" in removed
        assert "active-ws" not in removed
        wm.release("active-ws")


class TestIsLockStale:
    def test_stale_when_age_exceeds_max(self, env):
        _, wm, _, _ = env
        owner = {
            "agent_id": "a",
            "acquired_at": time.time() - WorkspaceManager.LOCK_MAX_AGE_SECONDS - 1,
            "pid": os.getpid(),
            "hostname": "otherhost",
        }
        assert wm._is_lock_stale(owner) is True

    def test_stale_when_pid_dead_same_host(self, env):
        _, wm, _, _ = env
        from flanes.workspace import _hostname

        owner = {
            "agent_id": "a",
            "acquired_at": time.time(),
            "pid": 99999999,  # almost certainly not a running PID
            "hostname": _hostname(),
        }
        assert wm._is_lock_stale(owner) is True

    def test_not_stale_different_hostname(self, env):
        _, wm, _, _ = env
        owner = {
            "agent_id": "a",
            "acquired_at": time.time(),
            "pid": 99999999,
            "hostname": "some-other-host-that-is-not-ours",
        }
        assert wm._is_lock_stale(owner) is False


class TestRemove:
    def test_raises_when_active_without_force(self, env):
        flanes_dir, wm, wsm, store = env
        _create_workspace(wm, wsm, store, name="active", lane="main")
        wm.acquire("active", "agent-1")
        # Update meta to reflect active status
        with pytest.raises(ValueError, match="active"):
            wm.remove("active")
        wm.release("active")

    def test_force_removes_active(self, env):
        flanes_dir, wm, wsm, store = env
        _create_workspace(wm, wsm, store, name="active2", lane="main")
        wm.acquire("active2", "agent-1")
        wm.remove("active2", force=True)
        assert wm.exists("active2") is False
