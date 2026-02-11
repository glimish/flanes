"""
Content-Addressed Store (CAS)

The foundational storage layer. Every piece of content is stored
exactly once, addressed by its SHA-256 hash. This gives us:

- Automatic deduplication (agents rewriting unchanged files cost nothing)
- Integrity verification (tamper-evident history)
- Cheap snapshots (world states share unchanged blobs)

Similar to git's object store, but designed for whole-tree snapshots
rather than individual file tracking.
"""

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ObjectType(Enum):
    BLOB = "blob"  # Raw file content
    TREE = "tree"  # Directory listing: name -> (type, hash)
    STATE = "state"  # World state: root tree + metadata


@dataclass(frozen=True)
class CASObject:
    """An immutable content-addressed object."""

    hash: str
    type: ObjectType
    data: bytes
    size: int


class ContentStoreLimitError(ValueError):
    """Raised when a store operation exceeds configured limits."""


class ContentStore:
    """
    SQLite-backed content-addressed store.

    Uses SQLite for simplicity and portability. In production you'd
    want a proper blob store, but SQLite handles surprisingly large
    workloads and keeps the system self-contained.

    Thread Safety:
        This class is NOT safe for concurrent use from multiple threads.
        The batch() context manager uses an unsynchronized flag that can
        race under concurrent access.  Create one Repository (and therefore
        one ContentStore) per thread.  Multiple Repository instances safely
        share the same database file via SQLite WAL mode + busy_timeout.
    """

    # Default: 100 MB max blob size
    # Note: 0 or missing value uses DEFAULT_MAX_BLOB_SIZE
    #       For effectively unlimited, set to very large value (e.g., 10**12)
    DEFAULT_MAX_BLOB_SIZE = 100 * 1024 * 1024

    def __init__(self, db_path: Path, blob_threshold: int = 0, max_blob_size: int = 0):
        self.db_path = db_path
        self.blob_threshold = blob_threshold
        # If max_blob_size is 0 or not provided, use default
        self.max_blob_size = max_blob_size if max_blob_size > 0 else self.DEFAULT_MAX_BLOB_SIZE
        self._blobs_dir = db_path.parent / "blobs" if blob_threshold > 0 else None
        # check_same_thread=False: allows Repository created on one thread
        # to be used on another.  Does NOT make ContentStore thread-safe
        # for concurrent multi-thread use.
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
        # 30s timeout for multi-threaded scenarios on slow CI runners
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # WARNING: not thread-safe — one ContentStore per thread
        self._in_batch = False
        self._closed = False
        self._init_tables()
        self._ensure_location_column()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                hash TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                data BLOB NOT NULL,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_objects_type
                ON objects(type);

            CREATE TABLE IF NOT EXISTS stat_cache (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                blob_hash TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def _ensure_location_column(self):
        """Add location column if missing (needed for filesystem blob storage).

        This is also tracked by the schema migration framework in
        WorldStateManager, but ContentStore must be self-contained so it
        works when used standalone (e.g. in unit tests without a WSM).
        """
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(objects)")}
        if "location" not in cols:
            self.conn.execute("ALTER TABLE objects ADD COLUMN location TEXT DEFAULT NULL")
            self.conn.commit()

    # ── Batch Transactions ────────────────────────────────────────

    @contextmanager
    def batch(self):
        """Context manager for batched writes — single commit at the end."""
        if self._in_batch:
            yield  # nested — pass through
            return
        self._in_batch = True
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._in_batch = False

    # ── Core Operations ───────────────────────────────────────────

    def hash_content(self, content: bytes, obj_type: ObjectType) -> str:
        """
        Hash content with type prefix (like git does) to prevent
        collisions between different object types with same content.
        """
        header = f"{obj_type.value}:{len(content)}:".encode()
        return hashlib.sha256(header + content).hexdigest()

    def store(self, content: bytes, obj_type: ObjectType) -> str:
        """
        Store content and return its hash. Idempotent — storing
        the same content twice is a no-op that returns the same hash.

        Size limit is checked AFTER deduplication to allow re-storing
        existing large blobs (e.g., if limits are lowered on existing repos).
        """
        content_hash = self.hash_content(content, obj_type)

        # Check if already exists (dedup) - do this FIRST
        existing = self.conn.execute(
            "SELECT hash FROM objects WHERE hash = ?", (content_hash,)
        ).fetchone()

        if existing is not None:
            # Blob already exists - return hash without checking size
            # This allows re-storing large blobs if limits are lowered
            return content_hash

        # New blob - check size limit before storing
        if obj_type == ObjectType.BLOB and len(content) > self.max_blob_size:
            raise ContentStoreLimitError(
                f"Blob size {len(content)} bytes exceeds limit of {self.max_blob_size} bytes"
            )

        # Check if this blob should go to filesystem
        if (
            self.blob_threshold > 0
            and obj_type == ObjectType.BLOB
            and len(content) > self.blob_threshold
        ):
            # Write fs blob first, then record in DB.
            # If fs write fails, no DB entry is created.
            # If DB insert fails, clean up the fs blob.
            self._write_fs_blob(content_hash, content)
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO objects
                       (hash, type, data, size, created_at, location)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (content_hash, obj_type.value, b"", len(content), time.time(), "fs"),
                )
            except Exception:
                # DB insert failed — remove orphaned fs blob
                self.delete_fs_blob(content_hash)
                raise
        else:
            self.conn.execute(
                """INSERT OR IGNORE INTO objects
                   (hash, type, data, size, created_at) VALUES (?, ?, ?, ?, ?)""",
                (content_hash, obj_type.value, content, len(content), time.time()),
            )
        if not self._in_batch:
            self.conn.commit()

        return content_hash

    def retrieve(self, content_hash: str) -> CASObject | None:
        """Retrieve an object by its hash."""
        row = self.conn.execute(
            "SELECT hash, type, data, size, location FROM objects WHERE hash = ?", (content_hash,)
        ).fetchone()

        if row is None:
            return None

        data = row[2]
        location = row[4]
        if location == "fs":
            fs_path = self._blob_fs_path(content_hash)
            if not fs_path.exists():
                raise FileNotFoundError(
                    f"Filesystem blob missing for hash {content_hash}: {fs_path}"
                )
            data = fs_path.read_bytes()

        return CASObject(
            hash=row[0],
            type=ObjectType(row[1]),
            data=data,
            size=row[3],
        )

    def exists(self, content_hash: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM objects WHERE hash = ?", (content_hash,)).fetchone()
        return row is not None

    def store_blob(self, content: bytes) -> str:
        """Store raw file content."""
        return self.store(content, ObjectType.BLOB)

    def store_tree(self, entries: dict) -> str:
        """
        Store a directory tree.

        entries: {name: (type, hash, mode)} where type is 'blob' or 'tree'
                 mode is optional (defaults to 0o644 for blobs, 0o755 for trees)

        Entries are sorted for deterministic hashing — the same set of
        files always produces the same tree hash regardless of insertion order.

        Fix #2 from audit: Tree entries now include file mode as third element.
        """
        # Normalize entries to always have 3 elements (type, hash, mode)
        normalized = {}
        for name, entry in entries.items():
            if len(entry) == 2:
                typ, h = entry
                # Default mode: 0o644 for files, 0o755 for directories
                mode = 0o755 if typ == "tree" else 0o644
                normalized[name] = (typ, h, mode)
            else:
                normalized[name] = tuple(entry)

        # Sort for deterministic hashing
        sorted_entries = sorted(normalized.items())
        data = json.dumps(sorted_entries).encode()
        return self.store(data, ObjectType.TREE)

    def read_tree(self, tree_hash: str) -> dict[str, tuple]:
        """Read a tree back into its entries.

        Returns {name: (type, hash, mode)} where mode defaults to 0o644/0o755
        for backward compatibility with trees that don't have mode stored.
        """
        obj = self.retrieve(tree_hash)
        if obj is None or obj.type != ObjectType.TREE:
            raise ValueError(f"Not a tree: {tree_hash}")
        entries_list = json.loads(obj.data.decode())

        result = {}
        for name, entry in entries_list:
            if len(entry) == 2:
                # Old format without mode — use defaults
                typ, h = entry
                mode = 0o755 if typ == "tree" else 0o644
                result[name] = (typ, h, mode)
            else:
                typ, h, mode = entry
                result[name] = (typ, h, mode)

        return result

    # ── Stat Cache ────────────────────────────────────────────────

    def check_stat_cache(self, path: str, mtime_ns: int, size: int) -> str | None:
        """Returns cached blob hash if stat matches, else None."""
        row = self.conn.execute(
            "SELECT blob_hash FROM stat_cache WHERE path = ? AND mtime_ns = ? AND size = ?",
            (path, mtime_ns, size),
        ).fetchone()
        return row[0] if row else None

    def update_stat_cache(self, path: str, mtime_ns: int, size: int, blob_hash: str):
        """Upsert a stat cache entry."""
        self.conn.execute(
            """INSERT OR REPLACE INTO stat_cache
               (path, mtime_ns, size, blob_hash) VALUES (?, ?, ?, ?)""",
            (path, mtime_ns, size, blob_hash),
        )
        if not self._in_batch:
            self.conn.commit()

    # ── Filesystem Blob Storage ───────────────────────────────────

    def _blob_fs_path(self, content_hash: str) -> Path:
        """2-level fanout path for filesystem blobs."""
        blobs_dir = self._blobs_dir or (self.db_path.parent / "blobs")
        return blobs_dir / content_hash[:2] / content_hash[2:4] / content_hash

    def _write_fs_blob(self, content_hash: str, content: bytes):
        """Write a blob to the filesystem atomically via temp file + rename."""
        fs_path = self._blob_fs_path(content_hash)
        if fs_path.exists():
            return  # Already written (idempotent)
        fs_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(fs_path.parent), prefix=".blob.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(fs_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def delete_fs_blob(self, content_hash: str):
        """Delete a filesystem blob if it exists."""
        if self._blobs_dir is None:
            # Even with threshold=0, blobs may exist from a previous config
            blobs_dir = self.db_path.parent / "blobs"
            fs_path = blobs_dir / content_hash[:2] / content_hash[2:4] / content_hash
        else:
            fs_path = self._blob_fs_path(content_hash)
        if fs_path.exists():
            fs_path.unlink()

    # ── Statistics ────────────────────────────────────────────────

    def stats(self) -> dict:
        """Storage statistics."""
        row = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM objects").fetchone()
        by_type = {}
        for row2 in self.conn.execute(
            "SELECT type, COUNT(*), COALESCE(SUM(size), 0) FROM objects GROUP BY type"
        ):
            by_type[row2[0]] = {"count": row2[1], "bytes": row2[2]}

        return {
            "total_objects": row[0],
            "total_bytes": row[1],
            "by_type": by_type,
        }

    def close(self):
        """Close the SQLite connection. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
        except Exception:
            pass

    def __del__(self):
        """Safety net: close if the user forgot to call close()."""
        try:
            if not self._closed:
                self.close()
        except Exception:
            pass
