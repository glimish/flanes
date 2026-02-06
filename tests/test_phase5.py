"""
Tests for Phase 5: Performance & Scale features.

Covers batch transactions, stat cache, garbage collection,
filesystem blob storage, and the gc CLI subcommand.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from fla.cas import ContentStore
from fla.gc import collect_garbage
from fla.repo import Repository
from fla.state import (
    AgentIdentity,
    EvaluationResult,
    Intent,
    WorldStateManager,
)

# ── Helpers ──────────────────────────────────────────────────────

def run_fla(*args, cwd=None, expect_fail=False):
    cmd = [sys.executable, "-X", "utf8", "-m", "fla.cli"] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
    )
    if not expect_fail and result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
    return result.returncode, result.stdout, result.stderr


def make_agent():
    return AgentIdentity(agent_id="test-agent", agent_type="test")


def make_intent(prompt="test change"):
    return Intent(
        id=str(uuid.uuid4()),
        prompt=prompt,
        agent=make_agent(),
        tags=["test"],
    )


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = ContentStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def store_with_threshold(tmp_path):
    s = ContentStore(tmp_path / "test.db", blob_threshold=100)
    yield s
    s.close()


@pytest.fixture
def wsm(store):
    return WorldStateManager(store, store.db_path)


@pytest.fixture
def repo_dir(tmp_path):
    (tmp_path / "hello.txt").write_text("Hello, World!\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.txt").write_text("some data\n")
    repo = Repository.init(tmp_path)
    yield tmp_path
    repo.close()


# ── Batch Transactions ───────────────────────────────────────────

class TestBatchTransactions:
    def test_batch_commits_once(self, store):
        """Objects are visible after batch exits, single commit."""
        hashes = []
        with store.batch():
            for i in range(10):
                h = store.store_blob(f"content-{i}".encode())
                hashes.append(h)

        # All should be retrievable after batch
        for h in hashes:
            assert store.retrieve(h) is not None

    def test_batch_rollback_on_error(self, store):
        """Exception in batch rolls back all stores."""
        h_before = store.store_blob(b"before-batch")

        try:
            with store.batch():
                h_inside = store.store_blob(b"inside-batch-rollback")
                raise ValueError("intentional error")
        except ValueError:
            pass

        # Object stored before batch should still exist
        assert store.retrieve(h_before) is not None
        # Object stored inside failed batch should be rolled back
        assert store.retrieve(h_inside) is None

    def test_nested_batch_passthrough(self, store):
        """Nested batch doesn't double-commit or error."""
        with store.batch():
            h1 = store.store_blob(b"outer")
            with store.batch():
                h2 = store.store_blob(b"inner")
            h3 = store.store_blob(b"after-inner")

        assert store.retrieve(h1) is not None
        assert store.retrieve(h2) is not None
        assert store.retrieve(h3) is not None

    def test_store_without_batch_still_commits(self, store):
        """Regression: existing auto-commit behavior unchanged."""
        h = store.store_blob(b"auto-commit-test")
        assert store.retrieve(h) is not None

        # Verify it persists across a fresh connection
        store2 = ContentStore(store.db_path)
        assert store2.retrieve(h) is not None
        store2.close()


# ── Stat Cache ───────────────────────────────────────────────────

class TestStatCache:
    def test_cache_hit_skips_file_read(self, tmp_path):
        """Second snapshot reuses cached hashes — same tree hash."""
        project = tmp_path / "project"
        project.mkdir()
        for i in range(5):
            (project / f"file{i}.txt").write_text(f"content-{i}\n")

        store = ContentStore(tmp_path / "test.db")
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        state1 = wsm.snapshot_directory(project, use_cache=True)
        state2 = wsm.snapshot_directory(project, use_cache=True)

        # Same files → same tree → same root_tree
        s1 = wsm.get_state(state1)
        s2 = wsm.get_state(state2)
        assert s1["root_tree"] == s2["root_tree"]
        store.close()

    def test_cache_miss_on_content_change(self, tmp_path):
        """Modifying a file produces a new hash."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "file.txt").write_text("original\n")

        store = ContentStore(tmp_path / "test.db")
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        state1 = wsm.snapshot_directory(project, use_cache=True)

        # Modify the file
        time.sleep(0.05)  # Ensure mtime changes
        (project / "file.txt").write_text("modified\n")

        state2 = wsm.snapshot_directory(project, use_cache=True)

        s1 = wsm.get_state(state1)
        s2 = wsm.get_state(state2)
        assert s1["root_tree"] != s2["root_tree"]
        store.close()

    def test_snapshot_with_cache_matches_without(self, tmp_path):
        """Same tree hash whether cache is used or not."""
        project = tmp_path / "project"
        project.mkdir()
        for i in range(3):
            (project / f"f{i}.txt").write_text(f"data-{i}\n")

        store = ContentStore(tmp_path / "test.db")
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        state_cached = wsm.snapshot_directory(project, use_cache=True)
        state_uncached = wsm.snapshot_directory(project, use_cache=False)

        s1 = wsm.get_state(state_cached)
        s2 = wsm.get_state(state_uncached)
        assert s1["root_tree"] == s2["root_tree"]
        store.close()


# ── Garbage Collection ───────────────────────────────────────────

class TestGarbageCollection:
    def _setup_repo_with_transitions(self, tmp_path):
        """Create a repo with accepted and rejected transitions."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "keep.txt").write_text("keep this\n")

        store = ContentStore(tmp_path / "test.db")
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        # Create accepted transition
        state1 = wsm.snapshot_directory(project)
        intent1 = make_intent("initial")
        tid1 = wsm.propose(None, state1, intent1)
        wsm.evaluate(tid1, EvaluationResult(passed=True, evaluator="test"))

        # Create rejected transition with unique content
        (project / "reject.txt").write_text("this will be rejected\n")
        state2 = wsm.snapshot_directory(project)
        intent2 = make_intent("rejected change")
        tid2 = wsm.propose(state1, state2, intent2)
        wsm.evaluate(tid2, EvaluationResult(passed=False, evaluator="test"))

        # Backdate the rejected transition so it's older than threshold
        old_time = time.time() - (31 * 86400)
        wsm.conn.execute(
            "UPDATE transitions SET created_at = ? WHERE id = ?",
            (old_time, tid2)
        )
        wsm.conn.commit()

        return store, wsm, state1, state2

    def test_gc_preserves_accepted_blobs(self, tmp_path):
        store, wsm, state1, _state2 = self._setup_repo_with_transitions(tmp_path)

        collect_garbage(store, wsm, dry_run=False, max_age_days=30)

        # Accepted state's blobs should still be retrievable
        s = wsm.get_state(state1)
        assert s is not None
        entries = store.read_tree(s["root_tree"])
        for _name, entry in entries.items():
            _typ, h = entry[0], entry[1]  # Handle (type, hash, mode) tuples
            assert store.retrieve(h) is not None
        store.close()

    def test_gc_removes_rejected_blobs(self, tmp_path):
        store, wsm, _state1, state2 = self._setup_repo_with_transitions(tmp_path)

        # Get the unique blob hash from the rejected state before GC
        s2 = wsm.get_state(state2)
        files2 = wsm._flatten_tree(s2["root_tree"])
        reject_blob = files2.get("reject.txt")
        assert reject_blob is not None

        result = collect_garbage(store, wsm, dry_run=False, max_age_days=30)
        assert result.deleted_objects > 0

        # The unique blob should be gone
        assert store.retrieve(reject_blob) is None
        store.close()

    def test_gc_dry_run_deletes_nothing(self, tmp_path):
        store, wsm, _state1, state2 = self._setup_repo_with_transitions(tmp_path)

        stats_before = store.stats()
        result = collect_garbage(store, wsm, dry_run=True, max_age_days=30)

        assert result.dry_run is True
        assert result.deleted_objects > 0  # would delete
        stats_after = store.stats()
        assert stats_before["total_objects"] == stats_after["total_objects"]
        store.close()

    def test_gc_preserves_shared_blobs(self, tmp_path):
        """Blob referenced by both accepted and rejected survives."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "shared.txt").write_text("shared content\n")

        store = ContentStore(tmp_path / "test.db")
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        # Accepted transition with shared.txt
        state1 = wsm.snapshot_directory(project)
        intent1 = make_intent("accepted")
        tid1 = wsm.propose(None, state1, intent1)
        wsm.evaluate(tid1, EvaluationResult(passed=True, evaluator="test"))

        # Rejected transition also has shared.txt (plus extra)
        (project / "extra.txt").write_text("extra\n")
        state2 = wsm.snapshot_directory(project)
        intent2 = make_intent("rejected")
        tid2 = wsm.propose(state1, state2, intent2)
        wsm.evaluate(tid2, EvaluationResult(passed=False, evaluator="test"))
        old_time = time.time() - (31 * 86400)
        wsm.conn.execute("UPDATE transitions SET created_at = ? WHERE id = ?", (old_time, tid2))
        wsm.conn.commit()

        # Get shared blob hash
        s1 = wsm.get_state(state1)
        files1 = wsm._flatten_tree(s1["root_tree"])
        shared_hash = files1["shared.txt"]

        collect_garbage(store, wsm, dry_run=False, max_age_days=30)

        # Shared blob must survive
        assert store.retrieve(shared_hash) is not None
        store.close()

    def test_gc_respects_age_threshold(self, tmp_path):
        """Recent rejected transitions are kept."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "f.txt").write_text("content\n")

        store = ContentStore(tmp_path / "test.db")
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        state1 = wsm.snapshot_directory(project)
        intent1 = make_intent("accepted")
        tid1 = wsm.propose(None, state1, intent1)
        wsm.evaluate(tid1, EvaluationResult(passed=True, evaluator="test"))

        (project / "new.txt").write_text("new\n")
        state2 = wsm.snapshot_directory(project)
        intent2 = make_intent("recently rejected")
        tid2 = wsm.propose(state1, state2, intent2)
        wsm.evaluate(tid2, EvaluationResult(passed=False, evaluator="test"))
        # Don't backdate — it's recent

        result = collect_garbage(store, wsm, dry_run=False, max_age_days=30)

        # Recent rejected transition should not be deleted
        assert result.deleted_transitions == 0

        # The rejected state's objects should still exist (within age)
        s2 = wsm.get_state(state2)
        assert s2 is not None
        store.close()


# ── Filesystem Blob Storage ──────────────────────────────────────

class TestFilesystemBlobStorage:
    def test_large_blob_stored_on_fs(self, tmp_path):
        """File over threshold is stored on filesystem."""
        store = ContentStore(tmp_path / "test.db", blob_threshold=100)
        large = b"x" * 200
        h = store.store_blob(large)

        # Check filesystem
        fs_path = store._blob_fs_path(h)
        assert fs_path.exists()
        assert fs_path.read_bytes() == large

        # Check DB has empty data with location='fs'
        row = store.conn.execute(
            "SELECT data, location FROM objects WHERE hash = ?", (h,)
        ).fetchone()
        assert row[0] == b""
        assert row[1] == "fs"
        store.close()

    def test_small_blob_stays_inline(self, tmp_path):
        """Blob below threshold stays in SQLite."""
        store = ContentStore(tmp_path / "test.db", blob_threshold=100)
        small = b"tiny"
        h = store.store_blob(small)

        row = store.conn.execute(
            "SELECT data, location FROM objects WHERE hash = ?", (h,)
        ).fetchone()
        assert row[0] == small
        assert row[1] is None
        store.close()

    def test_retrieve_fs_blob_roundtrip(self, tmp_path):
        """Store and retrieve large blob correctly."""
        store = ContentStore(tmp_path / "test.db", blob_threshold=100)
        large = b"A" * 500
        h = store.store_blob(large)
        obj = store.retrieve(h)
        assert obj is not None
        assert obj.data == large
        assert obj.size == 500
        store.close()

    def test_threshold_zero_all_inline(self, tmp_path):
        """Default behavior: everything in SQLite."""
        store = ContentStore(tmp_path / "test.db", blob_threshold=0)
        data = b"Z" * 1000
        h = store.store_blob(data)

        row = store.conn.execute(
            "SELECT data, location FROM objects WHERE hash = ?", (h,)
        ).fetchone()
        assert row[0] == data
        assert row[1] is None
        store.close()

    def test_gc_cleans_fs_blobs(self, tmp_path):
        """GC deletes filesystem blobs for unreachable objects."""
        project = tmp_path / "project"
        project.mkdir()
        # Create a large file to trigger FS storage
        (project / "big.bin").write_bytes(b"B" * 200)

        store = ContentStore(tmp_path / "test.db", blob_threshold=100)
        wsm = WorldStateManager(store, store.db_path)
        wsm.create_lane("main")

        state1 = wsm.snapshot_directory(project)
        intent1 = make_intent("accepted")
        tid1 = wsm.propose(None, state1, intent1)
        wsm.evaluate(tid1, EvaluationResult(passed=True, evaluator="test"))

        # Create rejected state with different large file
        (project / "big.bin").write_bytes(b"C" * 200)
        state2 = wsm.snapshot_directory(project)
        intent2 = make_intent("rejected")
        tid2 = wsm.propose(state1, state2, intent2)
        wsm.evaluate(tid2, EvaluationResult(passed=False, evaluator="test"))
        old_time = time.time() - (31 * 86400)
        wsm.conn.execute("UPDATE transitions SET created_at = ? WHERE id = ?", (old_time, tid2))
        wsm.conn.commit()

        # Get the rejected blob's fs path
        s2 = wsm.get_state(state2)
        files2 = wsm._flatten_tree(s2["root_tree"])
        rejected_hash = files2["big.bin"]
        fs_path = store._blob_fs_path(rejected_hash)
        assert fs_path.exists()

        collect_garbage(store, wsm, dry_run=False, max_age_days=30)

        # FS blob should be deleted
        assert not fs_path.exists()
        store.close()


# ── CLI GC Command ───────────────────────────────────────────────

class TestGCCLI:
    def test_gc_command_dry_run(self, repo_dir):
        """fla gc prints counts without deleting."""
        rc, out, err = run_fla("--json", "gc", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert data["dry_run"] is True
        assert "reachable_objects" in data

    def test_gc_command_confirm(self, repo_dir):
        """fla gc --confirm actually runs GC."""
        rc, out, err = run_fla("--json", "gc", "--confirm", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert data["dry_run"] is False
