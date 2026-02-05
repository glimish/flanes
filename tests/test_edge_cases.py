"""Edge case tests."""

import random

import pytest

from vex.cas import ContentStore
from vex.repo import Repository
from vex.state import AgentIdentity


@pytest.fixture
def store(tmp_path):
    s = ContentStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def repo(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    r = Repository.init(project)
    yield r
    r.close()


class TestEmptyFile:
    def test_empty_blob_round_trip(self, store):
        h = store.store_blob(b"")
        obj = store.retrieve(h)
        assert obj is not None
        assert obj.data == b""
        assert obj.size == 0


class TestBinaryNullBytes:
    def test_binary_round_trip(self, store):
        data = bytes(range(256)) * 4  # 1024 bytes including nulls
        h = store.store_blob(data)
        obj = store.retrieve(h)
        assert obj is not None
        assert obj.data == data


class TestUnicodeFilename:
    def test_unicode_snapshot_and_materialize(self, repo, tmp_path):
        ws = repo.workspace_path("main")
        (ws / "café.txt").write_text("latte", encoding="utf-8")

        result = repo.quick_commit(
            workspace="main",
            prompt="add unicode file",
            agent=AgentIdentity(agent_id="t", agent_type="test"),
            auto_accept=True,
        )
        assert result["status"] == "accepted"

        # Materialize into a fresh directory and verify
        out = tmp_path / "materialized"
        repo.wsm.materialize(result["to_state"], out)
        assert (out / "café.txt").exists()
        assert (out / "café.txt").read_text(encoding="utf-8") == "latte"


class TestDeeplyNestedDirectory:
    def test_deep_nesting_snapshot_and_materialize(self, repo, tmp_path):
        ws = repo.workspace_path("main")
        # Build 10 levels deep
        nested = ws
        for i in range(10):
            nested = nested / f"level{i}"
        nested.mkdir(parents=True)
        (nested / "deep.txt").write_text("bottom")

        result = repo.quick_commit(
            workspace="main",
            prompt="add deeply nested file",
            agent=AgentIdentity(agent_id="t", agent_type="test"),
            auto_accept=True,
        )
        assert result["status"] == "accepted"

        out = tmp_path / "materialized"
        repo.wsm.materialize(result["to_state"], out)
        target = out
        for i in range(10):
            target = target / f"level{i}"
        assert (target / "deep.txt").read_text() == "bottom"


class TestEmptyDirectory:
    def test_empty_dir_snapshot(self, repo):
        # Empty workspace (no files) should produce a valid state
        state_id = repo.snapshot("main")
        state = repo.wsm.get_state(state_id)
        assert state is not None
        tree = repo.wsm.store.read_tree(state["root_tree"])
        assert tree == {}


class TestDuplicateContentDeduplication:
    def test_duplicate_files_single_blob(self, repo):
        ws = repo.workspace_path("main")
        content = "identical content across files"
        (ws / "file_a.txt").write_text(content)
        (ws / "file_b.txt").write_text(content)
        (ws / "file_c.txt").write_text(content)

        repo.quick_commit(
            workspace="main",
            prompt="add duplicate files",
            agent=AgentIdentity(agent_id="t", agent_type="test"),
            auto_accept=True,
        )
        stats = repo.store.stats()
        # Should have 1 blob for the content (deduplicated), plus tree objects
        assert stats["by_type"]["blob"]["count"] == 1


class TestLargeFile:
    def test_large_blob_round_trip(self, store):
        rng = random.Random(42)
        data = bytes(rng.getrandbits(8) for _ in range(1_000_000))
        h = store.store_blob(data)
        obj = store.retrieve(h)
        assert obj is not None
        assert obj.data == data
        assert obj.size == 1_000_000
