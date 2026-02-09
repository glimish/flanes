"""
Edge case tests for file size and tree depth limits.
"""

import json
import tempfile
from pathlib import Path

import pytest

from flanes.cas import ContentStoreLimitError
from flanes.repo import Repository


def test_deduplication_with_overlimit_blob():
    """Existing large blobs can be re-stored after limits are lowered (dedup)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create repo with high limit, store a 1000-byte file
        (tmp_path / "test.txt").write_text("x" * 1000)
        with Repository.init(tmp_path):
            pass  # init snapshots the file

        # Lower the limit
        config_path = tmp_path / ".flanes" / "config.json"
        config = json.loads(config_path.read_text())
        config["max_blob_size"] = 500  # Lower than existing file
        config_path.write_text(json.dumps(config, indent=2))

        # Re-open repo with lower limit, re-snapshot same content
        with Repository(tmp_path) as repo2:
            ws = repo2.workspace_path("main")
            (ws / "test.txt").write_text("x" * 1000)  # Same content

            # Should succeed via deduplication — blob already exists
            state = repo2.snapshot("main")
            assert state is not None


def test_new_large_blob_rejected_after_lowering():
    """New blobs exceeding the lowered limit are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        (tmp_path / "small.txt").write_text("small")
        with Repository.init(tmp_path):
            pass

        # Lower the limit
        config_path = tmp_path / ".flanes" / "config.json"
        config = json.loads(config_path.read_text())
        config["max_blob_size"] = 500
        config_path.write_text(json.dumps(config, indent=2))

        with Repository(tmp_path) as repo2:
            ws = repo2.workspace_path("main")
            (ws / "big.txt").write_bytes(b"x" * 600)  # New file, over limit

            with pytest.raises(ContentStoreLimitError):
                repo2.snapshot("main")


def test_explicit_zero_limits_use_defaults():
    """Zero limits should use defaults, not disable limits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        (tmp_path / "test.txt").write_text("small")
        with Repository.init(tmp_path):
            pass

        # Set limits to 0
        config_path = tmp_path / ".flanes" / "config.json"
        config = json.loads(config_path.read_text())
        config["max_blob_size"] = 0
        config["max_tree_depth"] = 0
        config_path.write_text(json.dumps(config, indent=2))

        with Repository(tmp_path) as repo2:
            # 0 should fall back to defaults (100MB, 100 depth)
            assert repo2.store.max_blob_size == repo2.store.DEFAULT_MAX_BLOB_SIZE
            assert repo2.wsm.max_tree_depth == repo2.wsm.DEFAULT_MAX_TREE_DEPTH


def test_negative_limits_rejected():
    """Negative limits should raise ValueError on init."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        (tmp_path / "test.txt").write_text("test")
        with Repository.init(tmp_path):
            pass

        # Set negative limits
        config_path = tmp_path / ".flanes" / "config.json"
        config = json.loads(config_path.read_text())
        config["max_blob_size"] = -1
        config["max_tree_depth"] = -1
        config_path.write_text(json.dumps(config, indent=2))

        with pytest.raises(ValueError, match="max_blob_size"):
            Repository(tmp_path)


def test_very_large_tree_object():
    """A directory with many files creates a large tree object but succeeds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        with Repository.init(tmp_path) as repo:
            ws = repo.workspace_path("main")
            test_dir = ws / "many_files"
            test_dir.mkdir()

            # Create 1000 files (smaller count for faster tests)
            for i in range(1000):
                (test_dir / f"file_{i}.txt").write_text(f"content {i}")

            state_id = repo.snapshot("main")
            assert state_id is not None


def test_exact_limit_boundary():
    """Test behavior exactly at the limit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        (tmp_path / "dummy.txt").write_text("x")
        with Repository.init(tmp_path):
            pass

        # Set limit to exactly 1000 bytes
        config_path = tmp_path / ".flanes" / "config.json"
        config = json.loads(config_path.read_text())
        config["max_blob_size"] = 1000
        config_path.write_text(json.dumps(config, indent=2))

        with Repository(tmp_path) as repo2:
            ws = repo2.workspace_path("main")

            # At limit (1000 bytes is NOT > 1000, should pass)
            (ws / "at_limit.txt").write_bytes(b"x" * 1000)
            state1 = repo2.snapshot("main")
            assert state1 is not None

            # Over limit by 1 byte (should fail)
            (ws / "over_limit.txt").write_bytes(b"x" * 1001)
            with pytest.raises(ContentStoreLimitError):
                repo2.snapshot("main")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
