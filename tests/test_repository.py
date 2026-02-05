"""Repository unit tests."""


import pytest

from vex.repo import Repository
from vex.state import AgentIdentity, TransitionStatus


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
        with pytest.raises(ValueError, match="Not inside a Vex repository"):
            Repository.find(empty)


class TestInit:
    def test_init_on_existing_raises(self, repo_with_files):
        with pytest.raises(ValueError, match="already exists"):
            Repository.init(repo_with_files.root)

    def test_init_empty_dir_creates_vexignore(self, tmp_path):
        project = tmp_path / "empty_project"
        project.mkdir()
        repo = Repository.init(project)
        # .vexignore is auto-created, so there's an initial snapshot
        assert repo.head() is not None
        # Workspace should exist with .vexignore
        ws = repo.workspace_path("main")
        assert ws is not None
        # .vexignore should exist (it starts with . so is a dotfile)
        assert (ws / ".vexignore").exists()
        # Only .vex dir and .vexignore should exist
        all_files = list(ws.iterdir())
        names = {f.name for f in all_files}
        assert ".vexignore" in names
        assert ".vex" in names
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
