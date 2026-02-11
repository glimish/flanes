# Reliability & Crash Consistency

Flanes is designed so that crashes, power failures, or interrupted operations
never leave the repository in an unrecoverable state.

## Guarantees

### CAS (Content-Addressed Store)

- **Immutability**: Once stored, objects (blobs, trees, states) are never
  modified in place. New content creates new objects.
- **Atomic writes**: SQLite WAL mode ensures that writes are all-or-nothing.
  A crash mid-transaction rolls back cleanly.
- **Deduplication is safe**: Two agents storing the same content independently
  will produce the same hash. The second write is a no-op (INSERT OR IGNORE).

### Workspace Materialization

- **Dirty markers**: Before materializing files into a workspace, Flanes writes a
  `.flanes_materializing` marker file containing the target state ID and timestamp.
  The marker is removed only after successful completion.
- **Detection**: `WorkspaceManager.is_dirty(name)` checks for this marker.
  A dirty workspace should be re-materialized from its `base_state` (or the
  target state in the marker).
- **Recovery**: Remove the workspace (`flanes workspace remove NAME --force`)
  and recreate it. The CAS still has all the data.

### Metadata (JSON files)

- **Atomic writes via temp+rename**: All workspace metadata writes use
  `_atomic_write()` which writes to a temp file, calls `fsync()`, then
  renames over the target. On POSIX, rename is atomic. On Windows, Flanes
  retries on `PermissionError` (antivirus/indexer interference).
- **Crash safety**: If the process dies mid-write, the temp file is orphaned
  but the original metadata is untouched.

### Garbage Collection

- **Explicit only**: GC never runs automatically. You must call `flanes gc`.
- **Dry-run default**: `flanes gc` defaults to `--dry-run`, showing what would
  be deleted without actually deleting anything.
- **Consistent mark phase**: GC uses a SQLite deferred transaction during the
  mark phase so that concurrent accepts can't cause the scanner to miss
  reachable objects.
- **Sweep order**: DB deletes happen inside a transaction; filesystem blob
  deletes happen after the DB transaction commits. If the process crashes
  between DB commit and filesystem cleanup, you get orphan files on disk
  (wasted space, not corruption) that the next GC will clean up.

### Locking

- **Advisory locks via `mkdir`**: Cross-platform, no `fcntl`/`msvcrt` needed.
  `mkdir` is atomic on every major OS.
- **Stale lock detection**: Locks record PID and hostname. On the same host,
  Flanes checks if the PID is still alive. Locks older than 4 hours are
  considered stale regardless.
- **Lock reclamation**: Stale locks are removed and re-acquired atomically.
  If two processes race to reclaim, exactly one succeeds (the second
  `mkdir` fails with `FileExistsError`).

## What Is NOT Guaranteed

- **No WAL for workspace files**: The working directory is regular files on
  disk. If the OS crashes mid-`write_bytes()`, a file in the workspace may be
  partially written. This is expected -- the workspace is a working copy, not
  the source of truth. The CAS is the source of truth. Re-materialize to recover.
- **No cross-machine lock safety**: Advisory locks check PID only on the same
  hostname. On shared filesystems (NFS, SMB), stale lock detection falls back
  to the 4-hour age timeout.
- **SQLite limitations**: Flanes inherits SQLite's concurrency model. Multiple
  readers are fine (WAL mode). Multiple writers serialize through SQLite's
  internal locking. For high-throughput multi-agent scenarios, consider one
  Repository instance per thread.

## Testing

See `tests/test_crash_consistency.py` for tests that verify:

- Dirty marker survives materialize failure
- Dirty marker is cleaned on success
- Existing CAS data survives snapshot failure
- GC preserves reachable objects
- Atomic metadata writes survive partial failure
- Dirty workspaces can be detected and recovered
