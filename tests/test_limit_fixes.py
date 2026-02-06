"""
Regression tests for limit validation fixes.

Tests for issues found during production readiness audit:
1. Negative limits validation
2. Deduplication with lowered limits
3. Zero value behavior
"""

import json

import pytest

from fla.cas import ContentStoreLimitError
from fla.repo import Repository


def test_reject_negative_blob_size(tmp_path):
    """Regression: Negative max_blob_size should be rejected."""
    # Create valid repo
    (tmp_path / "test.txt").write_text("test")
    repo = Repository.init(tmp_path)
    repo.store.close()

    # Set negative blob size in config
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_blob_size"] = -100
    config_path.write_text(json.dumps(config, indent=2))

    # Should raise ValueError when opening repo
    with pytest.raises(ValueError, match="max_blob_size must be >= 0"):
        Repository(tmp_path)


def test_reject_negative_tree_depth(tmp_path):
    """Regression: Negative max_tree_depth should be rejected."""
    # Create valid repo
    (tmp_path / "test.txt").write_text("test")
    repo = Repository.init(tmp_path)
    repo.store.close()

    # Set negative tree depth in config
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_tree_depth"] = -10
    config_path.write_text(json.dumps(config, indent=2))

    # Should raise ValueError when opening repo
    with pytest.raises(ValueError, match="max_tree_depth must be >= 0"):
        Repository(tmp_path)


def test_deduplication_with_lowered_limits(tmp_path):
    """Regression: Lowering limits should not break existing large blobs via dedup."""
    # Create repo with large file
    (tmp_path / "large.bin").write_bytes(b"X" * 5000)
    repo = Repository.init(tmp_path)
    initial_head = repo.head()
    repo.store.close()

    # Lower the limit below the existing file size
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_blob_size"] = 2000  # Lower than 5000
    config_path.write_text(json.dumps(config, indent=2))

    # Re-open repo with lower limit
    repo2 = Repository(tmp_path)

    # Re-snapshot the same large file (should deduplicate)
    ws = repo2.workspace_path("main")
    (ws / "large.bin").write_bytes(b"X" * 5000)  # Same content

    # Should succeed because blob already exists (deduplication)
    state_id = repo2.snapshot("main", parent_id=initial_head)
    assert state_id is not None

    repo2.store.close()


def test_new_large_blob_rejected_after_lowering_limit(tmp_path):
    """New large blobs should still be rejected after lowering limits."""
    # Create repo with small file
    (tmp_path / "small.txt").write_text("small")
    repo = Repository.init(tmp_path)
    initial_head = repo.head()
    repo.store.close()

    # Lower the limit
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_blob_size"] = 2000
    config_path.write_text(json.dumps(config, indent=2))

    # Re-open repo
    repo2 = Repository(tmp_path)

    # Try to add a NEW large file (not in CAS yet)
    ws = repo2.workspace_path("main")
    (ws / "new_large.bin").write_bytes(b"Y" * 5000)  # Different content

    # Should fail because this is a new blob
    with pytest.raises(ContentStoreLimitError, match="exceeds limit"):
        repo2.snapshot("main", parent_id=initial_head)

    repo2.store.close()


def test_zero_blob_size_uses_default(tmp_path):
    """Regression: Zero max_blob_size should use default, not unlimited."""
    # Create repo
    (tmp_path / "test.txt").write_text("test")
    repo = Repository.init(tmp_path)
    default_size = repo.store.DEFAULT_MAX_BLOB_SIZE
    repo.store.close()

    # Set to 0
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_blob_size"] = 0
    config_path.write_text(json.dumps(config, indent=2))

    # Should use default
    repo2 = Repository(tmp_path)
    assert repo2.store.max_blob_size == default_size
    repo2.store.close()


def test_zero_tree_depth_uses_default(tmp_path):
    """Regression: Zero max_tree_depth should use default, not unlimited."""
    # Create repo
    (tmp_path / "test.txt").write_text("test")
    repo = Repository.init(tmp_path)
    default_depth = repo.wsm.DEFAULT_MAX_TREE_DEPTH
    repo.store.close()

    # Set to 0
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_tree_depth"] = 0
    config_path.write_text(json.dumps(config, indent=2))

    # Should use default
    repo2 = Repository(tmp_path)
    assert repo2.wsm.max_tree_depth == default_depth
    repo2.store.close()


def test_custom_limits_respected(tmp_path):
    """Custom limit values should be respected."""
    # Create repo
    (tmp_path / "test.txt").write_text("test")
    repo = Repository.init(tmp_path)
    repo.store.close()

    # Set custom limits
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_blob_size"] = 50000
    config["max_tree_depth"] = 50
    config_path.write_text(json.dumps(config, indent=2))

    # Should use custom values
    repo2 = Repository(tmp_path)
    assert repo2.store.max_blob_size == 50000
    assert repo2.wsm.max_tree_depth == 50
    repo2.store.close()


def test_very_large_limit_acts_as_unlimited(tmp_path):
    """Very large limits should effectively act as unlimited."""
    # Create repo with custom very large limits
    (tmp_path / "test.txt").write_text("test")
    repo = Repository.init(tmp_path)
    repo.store.close()

    # Set very large limits
    config_path = tmp_path / ".fla" / "config.json"
    config = json.loads(config_path.read_text())
    config["max_blob_size"] = 10**12  # 1 TB
    config["max_tree_depth"] = 10000
    config_path.write_text(json.dumps(config, indent=2))

    repo2 = Repository(tmp_path)
    assert repo2.store.max_blob_size == 10**12
    assert repo2.wsm.max_tree_depth == 10000

    # Large file should work
    ws = repo2.workspace_path("main")
    (ws / "large.bin").write_bytes(b"X" * 100000)
    state_id = repo2.snapshot("main")
    assert state_id is not None

    repo2.store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
