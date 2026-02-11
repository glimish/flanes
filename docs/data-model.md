# Flanes Data Model

## Core Concepts

### Content-Addressed Store (CAS)

Every piece of content is stored exactly once, addressed by its SHA-256 hash.

**Object types:**

| Type   | Contains                          | Addressed by            |
|--------|-----------------------------------|-------------------------|
| `blob` | Raw file bytes                    | `sha256(content)`       |
| `tree` | Directory listing: `[(name, (type, hash, mode)), ...]` | `sha256(json(entries))` |
| `state` | Root tree hash + parent ref + timestamp | `sha256(json(metadata))` |

**What gets hashed:**
- **Blobs**: Raw bytes only. No filename, no permissions, no timestamp.
  Two files with identical bytes produce the same blob hash.
- **Trees**: A sorted JSON array of `[name, [type, hash, mode]]` entries.
  File modes (e.g., `0o644`, `0o755`) are stored in the tree, not the blob.
  This means the same file content with different permissions creates the same
  blob but different trees.
- **States**: A JSON object with `root_tree`, `parent_id`, and `created_at`.

### World States

A WorldState is an immutable snapshot of the entire project. It points to a
root tree hash and optionally to a parent state (forming a DAG).

```
WorldState {
    id:         sha256(root_tree + parent_id + created_at)
    root_tree:  hash of root tree object
    parent_id:  hash of parent state (null for initial)
    created_at: unix timestamp
}
```

### Transitions

A Transition records a proposed change from one state to another, along with
who proposed it (agent), why (intent), and the evaluation outcome.

```
Transition {
    id:          uuid
    from_state:  state hash (null for initial)
    to_state:    state hash
    intent_id:   uuid -> Intent record
    lane:        lane name
    status:      proposed | evaluating | accepted | rejected | superseded
    created_at:  unix timestamp
}
```

### Lanes

A Lane is a named branch of history. Each lane has a `head_state` (the
latest accepted state) and a `fork_base` (the state it was forked from).

### Workspaces

A Workspace is a physical directory materialized from a CAS state. The
"main" workspace lives at the repo root (like git). Feature workspaces
live under `.flanes/workspaces/<name>/`.

## Ignore Rules

Flanes ignores certain paths during `snapshot_directory`:

**Always ignored** (hardcoded in `DEFAULT_IGNORE`):
- VCS directories: `.flanes`, `.git`, `.svn`, `.hg`
- Build artifacts: `__pycache__`, `node_modules`
- OS noise: `.DS_Store`, `Thumbs.db`
- IDE: `.idea`, `.vscode`
- Environment/secrets: `.env`, `.env.*`, `*.pem`, `*.key`, etc.

**User-configurable** via `.flanesignore` (like `.gitignore`):
- One pattern per line
- Lines starting with `#` are comments
- Patterns ending with `/` match directories only
- Patterns starting with `!` negate a previous match
- Patterns are matched against both basename and relative path

## Symlink Policy

Symlinks are **skipped** during snapshotting. This prevents:
- Reading files outside the workspace boundary
- Infinite loops from circular symlinks
- Non-deterministic snapshots (symlink targets may change)

Skipped symlinks are logged at DEBUG level.

## Filesystem Layout

```
my-project/
+-- .flanes/
|   +-- config.json           <- repo config (version, limits, etc.)
|   +-- store.db              <- SQLite database (CAS + metadata)
|   +-- main.json             <- main workspace metadata
|   +-- main.lockdir/         <- main workspace lock (existence = locked)
|   |   +-- owner.json        <- lock holder info (agent_id, pid, hostname)
|   +-- workspaces/
|       +-- feature-auth/     <- feature workspace files
|       +-- feature-auth.json <- feature workspace metadata
|       +-- feature-auth.lockdir/
+-- app.py                    <- main workspace files (repo root)
+-- .flanesignore             <- ignore rules
+-- models.py
```

## Workspace Name Constraints

Workspace names must match: `^[a-zA-Z0-9][a-zA-Z0-9._-]*$`

- Must start with a letter or digit
- May contain letters, digits, dots, hyphens, underscores
- No slashes, backslashes, spaces, or special characters
- This prevents nested directory creation and path ambiguity
