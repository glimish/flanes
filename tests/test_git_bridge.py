"""
Tests for Git Bridge — export/import between Flanes and Git repositories.
"""

import os
import subprocess
from pathlib import Path

import pytest


def _has_git():
    """Check if git is available on the system."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


requires_git = pytest.mark.skipif(not _has_git(), reason="git not available")


@pytest.fixture
def fla_repo(tmp_path):
    """Create a Flanes repository with some history."""
    from flanes.repo import Repository
    from flanes.state import AgentIdentity

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("print('hello')\n")
    (project_dir / "lib").mkdir()
    (project_dir / "lib" / "utils.py").write_text("def add(a, b): return a + b\n")

    repo = Repository.init(project_dir)

    # Create a second commit
    ws_path = repo.workspace_path("main")
    (ws_path / "main.py").write_text("print('hello world')\n")
    (ws_path / "README.md").write_text("# My Project\n")

    agent = AgentIdentity(agent_id="test-agent", agent_type="test")
    repo.quick_commit(
        workspace="main",
        prompt="Add README and update main",
        agent=agent,
        auto_accept=True,
    )

    return repo


@requires_git
class TestExportToGit:
    def test_export_creates_git_repo(self, fla_repo, tmp_path):
        from flanes.git_bridge import export_to_git

        target = tmp_path / "export"
        result = export_to_git(fla_repo, target, lane="main")

        assert result["commits"] >= 1
        assert (target / ".git").is_dir()

    def test_export_contains_files(self, fla_repo, tmp_path):
        from flanes.git_bridge import export_to_git

        target = tmp_path / "export"
        export_to_git(fla_repo, target, lane="main")

        # The final state should have all files
        assert (target / "main.py").exists()
        assert (target / "lib" / "utils.py").exists()
        assert (target / "README.md").exists()

        # Content should match
        assert "hello world" in (target / "main.py").read_text()

    def test_export_git_log_has_commits(self, fla_repo, tmp_path):
        from flanes.git_bridge import export_to_git

        target = tmp_path / "export"
        result = export_to_git(fla_repo, target, lane="main")

        # Verify git log works
        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        assert log.returncode == 0
        lines = [line for line in log.stdout.strip().split("\n") if line]
        assert len(lines) == result["commits"]

    def test_export_preserves_agent_info(self, fla_repo, tmp_path):
        from flanes.git_bridge import export_to_git

        target = tmp_path / "export"
        export_to_git(fla_repo, target, lane="main")

        # Check author in git log
        log = subprocess.run(
            ["git", "log", "--format=%an", "-1"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        assert log.returncode == 0
        # Should contain an agent name
        assert log.stdout.strip() != ""

    def test_export_empty_lane(self, tmp_path):
        """Exporting a lane with only the initial .flanesignore commit."""
        from flanes.git_bridge import export_to_git
        from flanes.repo import Repository

        project_dir = tmp_path / "empty_project"
        project_dir.mkdir()
        repo = Repository.init(project_dir)

        target = tmp_path / "export"
        result = export_to_git(repo, target, lane="main")

        # .flanesignore auto-creation causes one initial commit
        assert result["commits"] == 1
        repo.close()


@requires_git
class TestImportFromGit:
    def _make_git_repo(self, path: Path):
        """Create a simple git repo with commits."""
        path.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True, env=env)

        (path / "file1.txt").write_text("content1\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(path), capture_output=True, check=True, env=env
        )
        subprocess.run(
            ["git", "commit", "-m", "First commit"],
            cwd=str(path),
            capture_output=True,
            check=True,
            env=env,
        )

        (path / "file2.txt").write_text("content2\n")
        (path / "file1.txt").write_text("content1 updated\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(path), capture_output=True, check=True, env=env
        )
        subprocess.run(
            ["git", "commit", "-m", "Second commit"],
            cwd=str(path),
            capture_output=True,
            check=True,
            env=env,
        )

    def test_import_creates_transitions(self, tmp_path):
        from flanes.git_bridge import import_from_git
        from flanes.repo import Repository

        # Create git repo
        git_dir = tmp_path / "git_source"
        self._make_git_repo(git_dir)

        # Create flanes repo
        flanes_dir = tmp_path / "fla_target"
        flanes_dir.mkdir()
        repo = Repository.init(flanes_dir)

        result = import_from_git(git_dir, repo, lane="main")
        assert result["commits_imported"] == 2

        # Verify history
        history = repo.history(lane="main", status="accepted")
        assert len(history) >= 2

        repo.close()

    def test_import_preserves_content(self, tmp_path):
        from flanes.git_bridge import import_from_git
        from flanes.repo import Repository

        git_dir = tmp_path / "git_source"
        self._make_git_repo(git_dir)

        flanes_dir = tmp_path / "fla_target"
        flanes_dir.mkdir()
        repo = Repository.init(flanes_dir)

        import_from_git(git_dir, repo, lane="main")

        # Check head state has the right files
        head = repo.head("main")
        assert head is not None
        state = repo.wsm.get_state(head)
        files = repo.wsm._flatten_tree(state["root_tree"])
        assert "file1.txt" in files
        assert "file2.txt" in files

        # Verify content
        obj = repo.store.retrieve(files["file1.txt"])
        assert b"content1 updated" in obj.data

        repo.close()

    def test_import_not_a_git_repo(self, tmp_path):
        from flanes.git_bridge import import_from_git
        from flanes.repo import Repository

        flanes_dir = tmp_path / "fla_target"
        flanes_dir.mkdir()
        repo = Repository.init(flanes_dir)

        with pytest.raises(ValueError, match="Not a git repository"):
            import_from_git(tmp_path / "nonexistent", repo)

        repo.close()


@requires_git
class TestRoundTrip:
    def test_export_then_import(self, fla_repo, tmp_path):
        """Export flanes→git, then import git→flanes, verify content matches."""
        from flanes.git_bridge import export_to_git, import_from_git
        from flanes.repo import Repository

        # Export
        git_dir = tmp_path / "git_export"
        export_to_git(fla_repo, git_dir, lane="main")

        # Import into a fresh flanes repo
        fla2_dir = tmp_path / "fla2"
        fla2_dir.mkdir()
        repo2 = Repository.init(fla2_dir)
        result = import_from_git(git_dir, repo2, lane="main")
        assert result["commits_imported"] >= 1

        # Both repos should have the same files in head
        head1 = fla_repo.head("main")
        head2 = repo2.head("main")
        state1 = fla_repo.wsm.get_state(head1)
        state2 = repo2.wsm.get_state(head2)
        files1 = fla_repo.wsm._flatten_tree(state1["root_tree"])
        files2 = repo2.wsm._flatten_tree(state2["root_tree"])

        assert set(files1.keys()) == set(files2.keys())

        repo2.close()
