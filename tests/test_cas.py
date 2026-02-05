"""ContentStore unit tests."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from vex.cas import CASObject, ContentStore, ObjectType


@pytest.fixture
def store(tmp_path):
    s = ContentStore(tmp_path / "test.db")
    yield s
    s.close()


class TestStoreRetrieveRoundTrip:
    def test_blob_round_trip(self, store):
        data = b"hello world"
        h = store.store(data, ObjectType.BLOB)
        obj = store.retrieve(h)
        assert obj is not None
        assert obj.type == ObjectType.BLOB
        assert obj.data == data
        assert obj.size == len(data)
        assert obj.hash == h

    def test_tree_round_trip(self, store):
        data = b'[["a.txt", ["blob", "abc123"]]]'
        h = store.store(data, ObjectType.TREE)
        obj = store.retrieve(h)
        assert obj is not None
        assert obj.type == ObjectType.TREE
        assert obj.data == data


class TestStoreBlobDeduplication:
    def test_same_content_same_hash(self, store):
        data = b"duplicate content"
        h1 = store.store_blob(data)
        h2 = store.store_blob(data)
        assert h1 == h2

    def test_different_content_different_hash(self, store):
        h1 = store.store_blob(b"content A")
        h2 = store.store_blob(b"content B")
        assert h1 != h2


class TestStoreTreeDeterministic:
    def test_different_insertion_order_same_hash(self, store):
        entries_a = {"b.txt": ("blob", "hash_b"), "a.txt": ("blob", "hash_a")}
        entries_b = {"a.txt": ("blob", "hash_a"), "b.txt": ("blob", "hash_b")}
        h1 = store.store_tree(entries_a)
        h2 = store.store_tree(entries_b)
        assert h1 == h2


class TestReadTree:
    def test_read_tree_round_trip(self, store):
        # Input can be 2-tuples or 3-tuples
        entries = {"file.txt": ("blob", "abc"), "dir": ("tree", "def")}
        h = store.store_tree(entries)
        result = store.read_tree(h)
        # Output is always 3-tuples with default modes (0o644 for blobs, 0o755 for trees)
        expected = {"file.txt": ("blob", "abc", 0o644), "dir": ("tree", "def", 0o755)}
        assert result == expected

    def test_read_tree_preserves_explicit_modes(self, store):
        # Test that explicit modes are preserved
        entries = {"script.sh": ("blob", "abc", 0o755), "data": ("tree", "def", 0o700)}
        h = store.store_tree(entries)
        result = store.read_tree(h)
        assert result == entries

    def test_read_tree_nonexistent_hash_raises(self, store):
        with pytest.raises(ValueError, match="Not a tree"):
            store.read_tree("nonexistent_hash_value")


class TestExists:
    def test_exists_stored(self, store):
        h = store.store_blob(b"exists test")
        assert store.exists(h) is True

    def test_exists_missing(self, store):
        assert store.exists("not_a_real_hash") is False


class TestStats:
    def test_empty_store(self, store):
        stats = store.stats()
        assert stats["total_objects"] == 0
        assert stats["total_bytes"] == 0
        assert stats["by_type"] == {}

    def test_stats_counts_and_bytes(self, store):
        blob_data = b"some blob"
        store.store_blob(blob_data)
        tree_entries = {"f.txt": ("blob", "h")}
        store.store_tree(tree_entries)

        stats = store.stats()
        assert stats["total_objects"] == 2
        assert stats["by_type"]["blob"]["count"] == 1
        assert stats["by_type"]["blob"]["bytes"] == len(blob_data)
        assert stats["by_type"]["tree"]["count"] == 1


class TestHashContentTypePrefix:
    def test_same_bytes_different_type_different_hash(self, store):
        data = b"same bytes"
        h_blob = store.hash_content(data, ObjectType.BLOB)
        h_tree = store.hash_content(data, ObjectType.TREE)
        h_state = store.hash_content(data, ObjectType.STATE)
        assert h_blob != h_tree
        assert h_blob != h_state
        assert h_tree != h_state


class TestClose:
    def test_close_makes_connection_unusable(self, tmp_path):
        s = ContentStore(tmp_path / "close_test.db")
        s.store_blob(b"data")
        s.close()
        with pytest.raises(Exception):
            s.store_blob(b"after close")
