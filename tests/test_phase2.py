"""
Tests for Phase 2: ignore patterns, context managers, deferred fork_base fix.
"""


import pytest

from vex.agent_sdk import AgentSession
from vex.repo import Repository
from vex.state import AgentIdentity, WorldStateManager


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def repo_with_files(tmp_dir):
    (tmp_dir / "hello.py").write_text("print('hello')\n")
    (tmp_dir / "lib").mkdir()
    (tmp_dir / "lib" / "util.py").write_text("x = 1\n")
    repo = Repository.init(tmp_dir)
    yield repo
    repo.close()


# ── 1. Directory patterns in .vexignore ──────────────────────────────

class TestDirectoryPatterns:
    def test_trailing_slash_ignores_directory_not_file(self, tmp_dir):
        """``build/`` in .vexignore ignores dirs named build, not files."""
        (tmp_dir / ".vexignore").write_text("build/\n")
        # Create a *file* named build and a *directory* named build_output
        (tmp_dir / "build").write_text("I am a file named build\n")
        (tmp_dir / "builddir").mkdir()
        (tmp_dir / "builddir" / "artifact.o").write_bytes(b"\x00")
        # Also create a directory actually named "build"
        (tmp_dir / "build_actual").mkdir()
        # We can't have both a file and dir named "build" on disk,
        # so test with a separate dir pattern
        (tmp_dir / "src").mkdir()
        (tmp_dir / "src" / "main.py").write_text("pass\n")

        repo = Repository.init(tmp_dir)
        head = repo.head("main")
        state = repo.wsm.get_state(head)
        files = repo.wsm._flatten_tree(state["root_tree"])

        # The *file* named "build" should be present (not matched by dir pattern)
        assert "build" in files, "file named 'build' should NOT be ignored by 'build/'"
        # src/main.py should be present
        assert "src/main.py" in files
        repo.close()

    def test_dir_pattern_excludes_matching_directory(self, tmp_dir):
        """A directory whose name matches a dir-only pattern is excluded."""
        (tmp_dir / ".vexignore").write_text("dist/\n")
        (tmp_dir / "app.py").write_text("pass\n")
        (tmp_dir / "dist").mkdir()
        (tmp_dir / "dist" / "bundle.js").write_text("var x;")

        repo = Repository.init(tmp_dir)
        head = repo.head("main")
        state = repo.wsm.get_state(head)
        files = repo.wsm._flatten_tree(state["root_tree"])

        assert "app.py" in files
        assert "dist/bundle.js" not in files, "dist/ dir should be ignored"
        repo.close()


# ── 2. Negation patterns in .vexignore ───────────────────────────────

class TestNegationPatterns:
    def test_negation_reinclude(self, tmp_dir):
        """`*.log` + `!important.log` keeps important.log."""
        (tmp_dir / ".vexignore").write_text("*.log\n!important.log\n")
        (tmp_dir / "debug.log").write_text("debug stuff\n")
        (tmp_dir / "important.log").write_text("keep me\n")
        (tmp_dir / "app.py").write_text("pass\n")

        repo = Repository.init(tmp_dir)
        head = repo.head("main")
        state = repo.wsm.get_state(head)
        files = repo.wsm._flatten_tree(state["root_tree"])

        assert "app.py" in files
        assert "debug.log" not in files, "*.log should be ignored"
        assert "important.log" in files, "!important.log should re-include it"
        repo.close()

    def test_negation_unit(self):
        """_should_ignore respects negate parameter."""
        ignore = frozenset({"*.log"})
        negate = frozenset({"important.log"})
        # Updated signature: _should_ignore(name, rel_path, ignore, negate)
        assert WorldStateManager._should_ignore("debug.log", "debug.log", ignore, negate) is True
        assert WorldStateManager._should_ignore("important.log", "important.log", ignore, negate) is False
        assert WorldStateManager._should_ignore("app.py", "app.py", ignore, negate) is False


# ── 3. Repository context manager ───────────────────────────────────

class TestRepositoryContextManager:
    def test_with_statement_auto_closes(self, tmp_dir):
        (tmp_dir / "f.txt").write_text("data\n")
        with Repository.init(tmp_dir) as repo:
            assert repo.head("main") is not None
            conn = repo.store.conn
        # After exiting, the connection should be closed
        # Attempting to use it should raise
        with pytest.raises(Exception):
            conn.execute("SELECT 1")


# ── 4. AgentSession context manager ─────────────────────────────────

class TestAgentSessionContextManager:
    def test_with_statement_auto_closes(self, tmp_dir):
        (tmp_dir / "f.txt").write_text("data\n")
        repo = Repository.init(tmp_dir)
        repo.close()

        with AgentSession(
            repo_path=tmp_dir,
            agent_id="test",
            agent_type="test",
        ) as session:
            conn = session.repo.store.conn

        with pytest.raises(Exception):
            conn.execute("SELECT 1")


# ── 5. Non-auto promote accept updates fork_base ────────────────────

class TestPromoteManualAcceptForkBase:
    def test_manual_accept_updates_fork_base(self, repo_with_files):
        repo = repo_with_files
        main_head = repo.head("main")

        # Create feature lane and do some work
        repo.create_lane("feature-b", base=main_head)
        ws = repo.workspace_path("feature-b")
        (ws / "feature.txt").write_text("feature work\n")
        repo.quick_commit(
            workspace="feature-b",
            prompt="Add feature.txt",
            agent=AgentIdentity(agent_id="test", agent_type="test"),
            auto_accept=True,
        )

        # Promote WITHOUT auto_accept
        result = repo.promote(
            workspace="feature-b",
            target_lane="main",
            auto_accept=False,
        )
        assert result["status"] == "proposed"
        tid = result["transition_id"]

        # fork_base should NOT have changed yet
        fork_base_before = repo.wsm.get_lane_fork_base("feature-b")
        assert fork_base_before == main_head

        # Now manually accept
        repo.accept(tid)

        # fork_base should now be updated
        fork_base_after = repo.wsm.get_lane_fork_base("feature-b")
        assert fork_base_after == result["to_state"], (
            f"fork_base should advance to {result['to_state'][:12]}, "
            f"got {fork_base_after[:12] if fork_base_after else None}"
        )

    def test_auto_accept_still_updates_fork_base(self, repo_with_files):
        """Regression: auto_accept path should still work via accept()."""
        repo = repo_with_files
        main_head = repo.head("main")

        repo.create_lane("feature-c", base=main_head)
        ws = repo.workspace_path("feature-c")
        (ws / "thing.txt").write_text("stuff\n")
        repo.quick_commit(
            workspace="feature-c",
            prompt="Add thing.txt",
            agent=AgentIdentity(agent_id="test", agent_type="test"),
            auto_accept=True,
        )

        result = repo.promote(
            workspace="feature-c",
            target_lane="main",
            auto_accept=True,
        )
        assert result["status"] == "accepted"

        fork_base = repo.wsm.get_lane_fork_base("feature-c")
        assert fork_base == result["to_state"]
