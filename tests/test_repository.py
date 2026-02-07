"""Repository unit tests."""

import pytest

from fla.repo import Repository
from fla.state import AgentIdentity, TransitionStatus


def _agent():
    return AgentIdentity(agent_id="test-agent", agent_type="test")


@pytest.fixture
def repo(tmp_path):
    """Empty initialized repository."""
    r = Repository.init(tmp_path / "project")
    yield r
    r.close()


@pytest.fixture
def repo_with_files(tmp_path):
    """Repository initialized with existing files."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.txt").write_text("hello world")
    (project / "sub").mkdir()
    (project / "sub" / "data.txt").write_text("data")
    r = Repository.init(project)
    yield r
    r.close()


class TestReject:
    def test_reject_does_not_advance_head(self, repo_with_files):
        repo = repo_with_files
        head_before = repo.head()
        ws = repo.workspace_path("main")

        (ws / "new.txt").write_text("new file")
        new_state = repo.snapshot("main")
        tid = repo.propose(
            from_state=head_before,
            to_state=new_state,
            prompt="add new file",
            agent=_agent(),
        )
        status = repo.reject(tid, evaluator="test", summary="bad")
        assert status == TransitionStatus.REJECTED
        assert repo.head() == head_before


class TestRestore:
    def test_restore_reverts_workspace(self, repo_with_files):
        repo = repo_with_files
        ws = repo.workspace_path("main")
        original_head = repo.head()

        # Modify a file, commit
        (ws / "hello.txt").write_text("modified")
        repo.quick_commit(
            workspace="main",
            prompt="modify hello",
            agent=_agent(),
            auto_accept=True,
        )
        assert (ws / "hello.txt").read_text() == "modified"

        # Restore to original state
        repo.restore("main", original_head)
        assert (ws / "hello.txt").read_text() == "hello world"


class TestFind:
    def test_find_walks_up(self, repo_with_files):
        repo = repo_with_files
        # Create a nested dir inside the repo root
        nested = repo.root / "deep" / "nested"
        nested.mkdir(parents=True)
        found = Repository.find(nested)
        assert found.root == repo.root
        found.close()

    def test_find_raises_when_no_repo(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError, match="Not inside a Fla repository"):
            Repository.find(empty)


class TestInit:
    def test_init_on_existing_raises(self, repo_with_files):
        with pytest.raises(ValueError, match="already exists"):
            Repository.init(repo_with_files.root)

    def test_init_empty_dir_creates_flaignore(self, tmp_path):
        project = tmp_path / "empty_project"
        project.mkdir()
        repo = Repository.init(project)
        # .flaignore is auto-created, so there's an initial snapshot
        assert repo.head() is not None
        # Workspace should exist with .flaignore
        ws = repo.workspace_path("main")
        assert ws is not None
        # .flaignore should exist (it starts with . so is a dotfile)
        assert (ws / ".flaignore").exists()
        # Only .fla dir and .flaignore should exist
        all_files = list(ws.iterdir())
        names = {f.name for f in all_files}
        assert ".flaignore" in names
        assert ".fla" in names
        repo.close()


class TestAcceptNonPromote:
    def test_accept_non_promote_no_fork_base_change(self, repo_with_files):
        repo = repo_with_files
        ws = repo.workspace_path("main")

        # Create a side lane
        repo.create_lane("feature", base=repo.head())
        original_fork_base = repo.wsm.get_lane_fork_base("feature")

        # Accept a normal (non-promote) transition on main
        (ws / "hello.txt").write_text("changed")
        new_state = repo.snapshot("main")
        tid = repo.propose(
            from_state=repo.head(),
            to_state=new_state,
            prompt="normal change",
            agent=_agent(),
            tags=["bugfix"],  # no "promote" tag
        )
        repo.accept(tid, evaluator="test")

        # Feature lane's fork_base should be unchanged
        assert repo.wsm.get_lane_fork_base("feature") == original_fork_base


class TestDeleteLane:
    """Tests for lane deletion and lane_exists."""

    def test_delete_lane_removes_lane_and_workspace(self, repo_with_files):
        repo = repo_with_files
        repo.create_lane("feature-x", base=repo.head())
        assert repo.wsm.lane_exists("feature-x")
        assert repo.wm.exists("feature-x")

        deleted = repo.delete_lane("feature-x")
        assert deleted is True
        assert not repo.wsm.lane_exists("feature-x")
        assert not repo.wm.exists("feature-x")

    def test_delete_lane_nonexistent_returns_false(self, repo_with_files):
        repo = repo_with_files
        deleted = repo.delete_lane("nonexistent")
        assert deleted is False

    def test_delete_main_raises(self, repo_with_files):
        repo = repo_with_files
        with pytest.raises(ValueError, match="Cannot delete.*main"):
            repo.delete_lane("main")

    def test_lane_exists_true(self, repo_with_files):
        repo = repo_with_files
        assert repo.wsm.lane_exists("main") is True

    def test_lane_exists_false(self, repo_with_files):
        repo = repo_with_files
        assert repo.wsm.lane_exists("nonexistent") is False

    def test_delete_lane_with_only_db_record(self, repo_with_files):
        """Lane in DB but workspace already gone — delete should still succeed."""
        repo = repo_with_files
        repo.create_lane("orphan-lane", base=repo.head())
        # Manually remove workspace but leave DB record
        repo.wm.remove("orphan-lane", force=True)
        assert not repo.wm.exists("orphan-lane")
        assert repo.wsm.lane_exists("orphan-lane")

        deleted = repo.delete_lane("orphan-lane")
        assert deleted is True
        assert not repo.wsm.lane_exists("orphan-lane")

    def test_create_lane_atomic_rollback(self, repo_with_files, monkeypatch):
        """If workspace creation fails, lane record should be rolled back."""
        repo = repo_with_files

        def failing_create(*args, **kwargs):
            raise OSError("Simulated workspace creation failure")

        monkeypatch.setattr(repo.wm, "create", failing_create)

        with pytest.raises(OSError, match="Simulated"):
            repo.create_lane("doomed-lane", base=repo.head())

        # Lane record should NOT exist (rolled back)
        assert not repo.wsm.lane_exists("doomed-lane")

    def test_delete_lane_force_locked(self, repo_with_files):
        """Force delete works even on a locked workspace."""
        repo = repo_with_files
        repo.create_lane("locked-lane", base=repo.head())
        repo.wm.acquire("locked-lane", "some-agent")

        # Without force, should raise
        with pytest.raises(Exception):
            repo.delete_lane("locked-lane", force=False)

        # With force, should succeed
        deleted = repo.delete_lane("locked-lane", force=True)
        assert deleted is True
        assert not repo.wsm.lane_exists("locked-lane")


class TestInstanceLock:
    """NFS safety: instance lock file tests."""

    def test_instance_lock_created_on_open(self, tmp_path):
        """Opening a repo creates an instance.lock file."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "file.txt").write_text("hello")
        repo = Repository.init(project)
        lock_path = repo.fla_dir / "instance.lock"
        assert lock_path.exists()
        import json

        lock_data = json.loads(lock_path.read_text())
        assert "hostname" in lock_data
        assert "pid" in lock_data
        assert "machine_id" in lock_data
        assert "started_at" in lock_data
        import os

        assert lock_data["pid"] == os.getpid()
        repo.close()

    def test_instance_lock_removed_on_close(self, tmp_path):
        """Closing a repo removes the instance.lock file."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "file.txt").write_text("hello")
        repo = Repository.init(project)
        lock_path = repo.fla_dir / "instance.lock"
        assert lock_path.exists()
        repo.close()
        assert not lock_path.exists()

    def test_stale_lock_reclaimed(self, tmp_path):
        """A lock from a dead PID on the same host is reclaimed."""
        import json
        import os
        import platform

        project = tmp_path / "project"
        project.mkdir()
        (project / "file.txt").write_text("hello")
        repo = Repository.init(project)
        lock_path = repo.fla_dir / "instance.lock"
        repo.close()

        # Write a fake lock from a dead PID on same host
        fake_lock = {
            "hostname": platform.node(),
            "pid": 999999999,  # almost certainly dead
            "machine_id": str(os.getpid()),  # same machine
            "started_at": 1000000.0,  # ancient
        }
        lock_path.write_text(json.dumps(fake_lock))

        # Should be able to open the repo (stale lock reclaimed)
        repo2 = Repository(project)
        assert lock_path.exists()
        lock_data = json.loads(lock_path.read_text())
        assert lock_data["pid"] == os.getpid()
        repo2.close()

    def test_foreign_machine_lock_rejected(self, tmp_path):
        """A lock from a different machine raises ConcurrentAccessError."""
        import json
        import time

        from fla.repo import ConcurrentAccessError

        project = tmp_path / "project"
        project.mkdir()
        (project / "file.txt").write_text("hello")
        repo = Repository.init(project)
        lock_path = repo.fla_dir / "instance.lock"
        repo.close()

        # Write a fake lock from a different machine
        fake_lock = {
            "hostname": "other-machine.example.com",
            "pid": 12345,
            "machine_id": "999999999999",  # different from local
            "started_at": time.time(),
        }
        lock_path.write_text(json.dumps(fake_lock))

        with pytest.raises(ConcurrentAccessError, match="Another machine"):
            Repository(project)

    def test_context_manager_cleans_lock(self, tmp_path):
        """Using 'with' statement cleans up the lock."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "file.txt").write_text("hello")
        repo = Repository.init(project)
        repo.close()

        lock_path = project / ".fla" / "instance.lock"
        with Repository(project) as _:
            assert lock_path.exists()
        assert not lock_path.exists()
