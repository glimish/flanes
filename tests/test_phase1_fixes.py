"""
Tests for Phase 1 correctness & robustness fixes.

One test per fix, verifying the specific behaviour each fix addresses.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from vex.repo import Repository
from vex.state import AgentIdentity, WorldStateManager
from vex.workspace import _replace_with_retry, _atomic_write


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory for repo initialisation."""
    return tmp_path


@pytest.fixture
def repo_with_files(tmp_dir):
    """Create a repo initialised with some starter files."""
    (tmp_dir / "hello.py").write_text("print('hello')\n")
    (tmp_dir / "lib").mkdir()
    (tmp_dir / "lib" / "util.py").write_text("x = 1\n")
    repo = Repository.init(tmp_dir)
    yield repo
    repo.close()


# ── Fix 1: init() workspace metadata matches wm.create() schema ─────

class TestFix1InitWorkspaceMetadata:
    def test_init_workspace_has_all_required_fields(self, tmp_dir):
        (tmp_dir / "app.py").write_text("pass\n")
        repo = Repository.init(tmp_dir)

        info = repo.wm.get("main")
        assert info is not None, "main workspace must exist after init"
        assert info.name == "main"
        assert info.lane == "main"
        assert info.path.exists(), "workspace directory must exist"
        assert info.base_state is not None, "base_state must be set"
        assert info.status in ("idle", "active")
        assert info.created_at > 0
        assert info.updated_at > 0

        # Verify files were moved into workspace
        assert (info.path / "app.py").exists()
        assert not (tmp_dir / "app.py").exists()
        repo.close()


# ── Fix 2: promote updates fork_base ────────────────────────────────

class TestFix2PromoteUpdatesForkBase:
    def test_fork_base_advances_after_promote(self, repo_with_files):
        repo = repo_with_files
        main_head = repo.head("main")

        # Create feature lane
        repo.create_lane("feature-a", base=main_head)
        ws = repo.workspace_path("feature-a")
        (ws / "new_file.txt").write_text("feature work\n")
        repo.quick_commit(
            workspace="feature-a",
            prompt="Add new_file",
            agent=AgentIdentity(agent_id="test", agent_type="test"),
            auto_accept=True,
        )

        # Promote into main
        result = repo.promote(
            workspace="feature-a",
            target_lane="main",
            auto_accept=True,
        )
        assert result["status"] == "accepted"
        new_state = result["to_state"]

        # fork_base should now be the promoted state
        fork_base = repo.wsm.get_lane_fork_base("feature-a")
        assert fork_base == new_state, (
            f"fork_base should advance to {new_state[:12]}, got {fork_base[:12] if fork_base else None}"
        )

        # Second promote with no new changes should still work (no false conflicts)
        (ws / "another.txt").write_text("more work\n")
        repo.quick_commit(
            workspace="feature-a",
            prompt="Add another file",
            agent=AgentIdentity(agent_id="test", agent_type="test"),
            auto_accept=True,
        )
        result2 = repo.promote(
            workspace="feature-a",
            target_lane="main",
            auto_accept=True,
        )
        assert result2["status"] == "accepted"


# ── Fix 3: .vexignore fnmatch patterns ──────────────────────────────

class TestFix3VexignoreFnmatch:
    def test_glob_patterns_in_vexignore(self, tmp_dir):
        (tmp_dir / ".vexignore").write_text("*.pyc\ntest_*\n")
        (tmp_dir / "app.py").write_text("pass\n")
        (tmp_dir / "app.pyc").write_bytes(b"\x00compiled")
        (tmp_dir / "test_app.py").write_text("pass\n")
        (tmp_dir / "conftest.py").write_text("pass\n")

        repo = Repository.init(tmp_dir)
        head = repo.head("main")
        state = repo.wsm.get_state(head)
        files = repo.wsm._flatten_tree(state["root_tree"])

        assert "app.py" in files, "app.py should be included"
        assert "conftest.py" in files, "conftest.py should be included"
        assert "app.pyc" not in files, "*.pyc should be ignored"
        assert "test_app.py" not in files, "test_* should be ignored"
        repo.close()

    def test_should_ignore_static_method(self):
        ignore = frozenset({"exact_match", "*.log", "test_*"})
        assert WorldStateManager._should_ignore("exact_match", ignore) is True
        assert WorldStateManager._should_ignore("foo.log", ignore) is True
        assert WorldStateManager._should_ignore("test_something", ignore) is True
        assert WorldStateManager._should_ignore("production.py", ignore) is False
        assert WorldStateManager._should_ignore("log", ignore) is False


# ── Fix 4: _atomic_write retries on Windows PermissionError ─────────

class TestFix4AtomicWriteRetry:
    def test_replace_with_retry_succeeds_after_transient_failure(self, tmp_path):
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("new content")
        dst.write_text("old content")

        call_count = 0
        original_replace = Path.replace

        def mock_replace(self_path, target):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise PermissionError("File in use")
            return original_replace(self_path, target)

        with patch("vex.workspace.os.name", "nt"), \
             patch.object(Path, "replace", mock_replace), \
             patch("vex.workspace.time.sleep"):
            _replace_with_retry(src, dst)

        assert dst.read_text() == "new content"
        assert call_count == 3

    def test_replace_with_retry_raises_after_max_attempts(self, tmp_path):
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("content")

        with patch("vex.workspace.os.name", "nt"), \
             patch.object(Path, "replace", side_effect=PermissionError("locked")), \
             patch("vex.workspace.time.sleep"), \
             pytest.raises(PermissionError):
            _replace_with_retry(src, dst)

    def test_replace_posix_raises_immediately(self, tmp_path):
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("content")

        with patch("vex.workspace.os.name", "posix"), \
             patch.object(Path, "replace", side_effect=PermissionError("nope")), \
             pytest.raises(PermissionError):
            _replace_with_retry(src, dst)


# ── Fix 5: wm.list() ignores lockdir/owner.json ─────────────────────

class TestFix5ListIgnoresLockdir:
    def test_list_excludes_lockdir_json(self, repo_with_files):
        repo = repo_with_files

        # Acquire a lock to create a .lockdir/owner.json
        repo.workspace_acquire("main", "test-agent")

        lockdir = repo.wm._lock_path("main")
        assert lockdir.exists(), "lockdir should exist after acquire"
        assert (lockdir / "owner.json").exists()

        workspaces = repo.wm.list()
        names = [ws.name for ws in workspaces]
        # Should only contain actual workspaces, not lockdir artefacts
        assert "main" in names
        for ws in workspaces:
            assert ".lockdir" not in ws.name, f"lockdir leaked into list: {ws.name}"

        repo.workspace_release("main")


# ── Fix 6: PRAGMA busy_timeout ───────────────────────────────────────

class TestFix6BusyTimeout:
    def test_busy_timeout_is_set(self, repo_with_files):
        repo = repo_with_files
        row = repo.store.conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000, f"busy_timeout should be 5000, got {row[0]}"


# ── Fix 7: work() propagates original exception ─────────────────────

class TestFix7WorkExceptionShadowing:
    def test_original_exception_propagates_not_cleanup(self, tmp_dir):
        (tmp_dir / "file.txt").write_text("data\n")
        repo = Repository.init(tmp_dir)

        session = MagicMock()
        session.workspace_path = repo.workspace_path("main")
        session.begin = MagicMock()
        session.end = MagicMock()
        session.propose = MagicMock(side_effect=RuntimeError("cleanup kaboom"))

        from vex.agent_sdk import AgentSession, WorkContext

        real_session = AgentSession(
            repo_path=tmp_dir,
            agent_id="test",
            agent_type="test",
        )

        # The agent raises ValueError; propose() in cleanup raises RuntimeError.
        # We must see ValueError, not RuntimeError.
        with pytest.raises(ValueError, match="agent broke"):
            with real_session.work("test prompt") as w:
                raise ValueError("agent broke")

        repo.close()


# ── Fix 8: version strings match ────────────────────────────────────

class TestFix8VersionMatch:
    def test_versions_consistent(self):
        import vex

        project_root = Path(vex.__file__).parent.parent

        # Read setup.py version
        setup_py = project_root / "setup.py"
        setup_text = setup_py.read_text()
        # Extract version="X.Y.Z" from setup.py
        import re
        m = re.search(r'version="([^"]+)"', setup_text)
        assert m, "Could not find version in setup.py"
        setup_version = m.group(1)

        # Read pyproject.toml version
        pyproject = project_root / "pyproject.toml"
        pyproject_text = pyproject.read_text()
        m2 = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
        assert m2, "Could not find version in pyproject.toml"
        pyproject_version = m2.group(1)

        assert vex.__version__ == setup_version, (
            f"__init__.py ({vex.__version__}) != setup.py ({setup_version})"
        )
        assert vex.__version__ == pyproject_version, (
            f"__init__.py ({vex.__version__}) != pyproject.toml ({pyproject_version})"
        )
