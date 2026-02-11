# Flanes User Guide

Comprehensive guide for Flanes, a version control system for agentic AI.

## Table of Contents

1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [CLI Reference](#cli-reference)
5. [Agent SDK](#agent-sdk)
6. [Configuration](#configuration)
7. [Evaluators](#evaluators)
8. [Budgets](#budgets)
9. [Remote Storage](#remote-storage)
10. [Git Bridge](#git-bridge)
11. [Garbage Collection](#garbage-collection)
12. [Multi-repo Projects](#multi-repo-projects)
13. [Templates](#templates)
14. [MCP Server](#mcp-server)
15. [REST API](#rest-api)
16. [Repository Health](#repository-health)
17. [Limits & Safety](#limits--safety)
18. [Thread Safety](#thread-safety)

---

## Installation

### Basic Install

```bash
pip install flanes
```

### Optional Dependencies

```bash
# Amazon S3 remote storage
pip install flanes[s3]

# Google Cloud Storage remote storage
pip install flanes[gcs]
```

### Verify Installation

```bash
flanes --help
```

---

## Quick Start

### Initialize a Repository

```bash
cd my-project
flanes init
```

This creates a `.flanes/` directory and takes an initial snapshot. Your files stay in place: the repo root IS the main workspace (like git). Only metadata is added.

### Make Changes and Commit

```bash
# Edit files at repo root (main workspace IS the repo root)
echo "print('hello')" > app.py

# Quick commit: snapshot + propose + accept
flanes commit \
  --prompt "Add hello world app" \
  --agent-id dev-1 \
  --agent-type coder \
  --auto-accept
```

### Create a Feature Lane

```bash
# Create a new lane (automatically creates a workspace)
flanes lane create feature-auth

# Work in the feature workspace
echo "def login(): pass" > .flanes/workspaces/feature-auth/auth.py

# Commit on the feature lane
flanes commit \
  --prompt "Add auth module" \
  --agent-id dev-1 \
  --agent-type coder \
  --workspace feature-auth \
  --auto-accept
```

### Promote to Main

```bash
flanes promote --workspace feature-auth --target main --auto-accept
```

### View History

```bash
flanes history --lane main
flanes trace          # causal lineage
flanes status         # full repo overview
```

---

## Core Concepts

For a concise overview, see the [README](../README.md#core-concepts).

### World States

A world state is a complete, immutable snapshot of the entire project at a point in time. Every file, every directory, captured as a Merkle tree in the content-addressed store. World states are identified by the SHA-256 hash of their root tree.

There are no partial commits. When you snapshot a workspace, Flanes hashes every file and builds a complete tree. If nothing changed, the hash is the same and no new state is created.

### Intents

Every transition carries a structured intent:

- **prompt:** the instruction that caused the change
- **agent identity:** who made it (agent_id, agent_type, model)
- **tags:** semantic labels for search (e.g., `["auth", "security"]`)
- **cost:** token usage, API calls, wall time
- **context_refs:** references to related states or external resources
- **metadata:** arbitrary key-value pairs

Intents make the *why* behind changes queryable, not just the *what*.

### Transitions

A transition proposes moving from one world state (parent) to another (child). Transitions have a lifecycle:

1. **proposed:** created but not yet evaluated
2. **accepted:** passed evaluation, advances the lane head
3. **rejected:** failed evaluation, recorded for posterity

Accepting a transition advances the lane's head pointer to the new state.

### Lanes

Lanes are isolated workstreams, analogous to branches but with different semantics. Lanes don't merge. Instead, work is *promoted* from one lane to another.

Lane names must use dashes, not slashes:
- `feature-auth` (valid)
- `bugfix-parser-edge-case` (valid)
- `feature/auth` (invalid, rejected with error)

### Workspaces

The main workspace IS the repo root (like git). Feature lanes get physically isolated directories at `.flanes/workspaces/<name>/`. This gives you familiar git-style behavior for everyday work while still enabling parallel agents to work on feature lanes without interfering with each other.

Workspaces are materialized from the CAS when created and incrementally updated when the target state changes; only modified files are written.

### Promote

Promote copies accepted work from a source lane into a target lane. It:

1. Finds the common ancestor state
2. Diffs both sides against the ancestor
3. If no file paths collide, applies the source changes to the target
4. If paths collide, reports conflicts with detailed resolution guidance

Promote never merges file contents. When conflicts occur, it provides three resolution options:
- Update workspace to target and manually fix conflicts
- Re-run agent from updated base
- Use `--force` to overwrite target changes (for automation)

The `--force` flag is useful in CI/CD pipelines where the agent's changes should always win.

---

## CLI Reference

All commands support `--json` for machine-readable output.

### Repository

| Command | Description |
|---------|-------------|
| `flanes init [path]` | Initialize a new Flanes repository |
| `flanes status` | Show repository status (lanes, workspaces, head states) |
| `flanes doctor [--fix]` | Check repository health, optionally fix issues |

### Snapshots & Commits

| Command | Description |
|---------|-------------|
| `flanes snapshot [--workspace NAME]` | Snapshot workspace to create a new world state |
| `flanes propose --prompt "..." --agent-id ID --agent-type TYPE` | Propose a transition |
| `flanes accept TRANSITION_ID` | Accept a proposed transition |
| `flanes reject TRANSITION_ID` | Reject a proposed transition |
| `flanes commit --prompt "..." --agent-id ID --agent-type TYPE [--auto-accept]` | Quick commit (snapshot + propose + accept) |

#### Commit Options

```bash
flanes commit \
  --prompt "Description of changes" \
  --agent-id coder-1 \
  --agent-type feature_developer \
  --workspace feature-auth \
  --model claude-sonnet-4-20250514 \
  --tags auth,security \
  --tokens-in 2000 \
  --tokens-out 1200 \
  --auto-accept
```

### Lanes

| Command | Description |
|---------|-------------|
| `flanes lanes` | List all lanes with head states |
| `flanes lane create NAME [--base STATE_ID]` | Create a new lane (and workspace) |
| `flanes lane delete NAME [--force]` | Delete a lane and its workspace |

```bash
# Create a lane branching from current main head
flanes lane create feature-auth

# Create a lane from a specific state
flanes lane create experiment-v2 --base abc123

# Delete a lane and its workspace
flanes lane delete feature-auth

# Force delete even if workspace is locked
flanes lane delete feature-auth --force
```

### Workspaces

| Command | Description |
|---------|-------------|
| `flanes workspace list` | List all workspaces |
| `flanes workspace create NAME [--lane LANE] [--base STATE_ID]` | Create a workspace |
| `flanes workspace remove NAME [--force]` | Remove a workspace |
| `flanes workspace update NAME [--state STATE_ID]` | Update workspace to a state |
| `flanes restore STATE_ID [--workspace NAME]` | Restore workspace to any historical state |
| `flanes promote --workspace NAME --target LANE [--auto-accept] [--force]` | Promote workspace into target lane |

```bash
# Update feature workspace to latest main
flanes workspace update feature-auth --state $(flanes status --json | jq -r '.lanes.main.head')

# Restore to a previous state
flanes restore abc123def --workspace main
```

### Query & History

| Command | Description |
|---------|-------------|
| `flanes history [--lane LANE] [--limit N] [--status STATUS]` | Show transition history |
| `flanes log` | Alias for `history` |
| `flanes trace [STATE_ID]` | Show causal lineage of a state |
| `flanes diff STATE_A STATE_B [--content]` | File-level diff between two states |
| `flanes search QUERY` | Search intents by text and tags |
| `flanes semantic-search QUERY [--limit N]` | Embedding-based semantic search |
| `flanes info STATE_ID` | Show details about a world state |
| `flanes show STATE_ID PATH` | Show file content from a state |

```bash
# Show last 5 accepted transitions on main
flanes history --lane main --limit 5 --status accepted

# Diff two states with file content
flanes diff abc123 def456 --content

# Search for authentication-related changes
flanes search "authentication"
```

### Evaluators

| Command | Description |
|---------|-------------|
| `flanes evaluate [TRANSITION_ID] [--workspace NAME]` | Run evaluators against a transition |

### Budgets

| Command | Description |
|---------|-------------|
| `flanes budget show LANE` | Show budget status for a lane |
| `flanes budget set LANE [options]` | Set budget limits for a lane |

### Garbage Collection

| Command | Description |
|---------|-------------|
| `flanes gc [--confirm] [--older-than N]` | Run garbage collection |

### Git Bridge

| Command | Description |
|---------|-------------|
| `flanes export-git TARGET_DIR [--lane LANE]` | Export Flanes history to a git repository |
| `flanes import-git SOURCE_DIR [--lane LANE]` | Import git commits into Flanes |

### Remote Storage

| Command | Description |
|---------|-------------|
| `flanes remote push [--metadata]` | Push local objects to remote storage (with `--metadata`: also sync lane metadata) |
| `flanes remote pull [--metadata]` | Pull remote objects to local store (with `--metadata`: also sync lane metadata, detect conflicts) |
| `flanes remote status` | Show sync status |

### Templates

| Command | Description |
|---------|-------------|
| `flanes template list` | List available templates |
| `flanes template create NAME [--description DESC]` | Create a template from current workspace |
| `flanes template show NAME` | Show template details |

### Projects

| Command | Description |
|---------|-------------|
| `flanes project init [--name NAME]` | Initialize a multi-repo project |
| `flanes project add REPO_PATH MOUNT_POINT [--lane LANE]` | Add a repo to the project |
| `flanes project status` | Show status of all repos |
| `flanes project snapshot` | Coordinated snapshot across all repos |

### Server & Integration

| Command | Description |
|---------|-------------|
| `flanes serve [--port PORT] [--host HOST] [--token TOKEN] [--insecure] [--web]` | Start REST API server (default: 127.0.0.1:7654) |
| `flanes mcp` | Start MCP tool server (stdio) |
| `flanes completion SHELL` | Generate shell completion script (bash, zsh, fish) |

### Low-level

| Command | Description |
|---------|-------------|
| `flanes cat-file HASH [--type TYPE]` | Inspect raw CAS object |

---

## Agent SDK

The Python SDK provides `AgentSession` for programmatic access to Flanes from agent code.

### AgentSession

```python
from flanes.agent_sdk import AgentSession

session = AgentSession(
    repo_path="./my-project",
    agent_id="coder-1",
    agent_type="feature_developer",
    model="claude-sonnet-4-20250514",
    lane="main",                    # optional, defaults to repo default
    workspace="main",               # optional, defaults to lane name
    session_id="custom-session-id", # optional, auto-generated if omitted
)
```

### The work() Context Manager

The recommended way to use the SDK. Handles locking, snapshotting, proposing, and accepting/rejecting automatically:

```python
with session.work("Add authentication module", tags=["auth"], auto_accept=True) as w:
    # w.path is a pathlib.Path to the isolated workspace directory
    auth_file = w.path / "lib" / "auth.py"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("def authenticate(user, password): ...")

    # Track token usage
    w.record_tokens(tokens_in=2000, tokens_out=1200)

    # Add custom metadata
    w.add_metadata("complexity", "medium")

# On normal exit: snapshot → propose → accept (if auto_accept=True)
# On exception: snapshot → propose → reject (error recorded in metadata)
# Always: releases workspace lock
```

### WorkContext

The `w` object inside `work()` is a `WorkContext`:

- **`w.path`:** `pathlib.Path` to the workspace directory. Read and write files here.
- **`w.record_tokens(tokens_in, tokens_out)`:** track token usage for cost accounting.
- **`w.add_metadata(key, value)`:** attach arbitrary metadata to the transition.
- **`w.result`:** after `work()` exits, contains the checkpoint result dict.

### Manual Session Control

For more granular control:

```python
session = AgentSession(repo_path="./my-project", agent_id="coder-1", agent_type="coder")

# Acquire workspace lock
session.begin()

try:
    # Get workspace path
    ws_path = session.workspace_path()

    # ... modify files ...

    # Record token usage
    session.record_tokens(tokens_in=1500, tokens_out=800)

    # Snapshot and propose
    result = session.propose(
        prompt="Refactored parser module",
        tags=["refactor", "parser"],
    )

    # Or use checkpoint (snapshot + propose + optionally accept)
    result = session.checkpoint(
        prompt="Refactored parser module",
        auto_accept=True,
        tags=["refactor"],
    )
finally:
    session.end()  # Always release the lock
```

### Lane Management from SDK

```python
# Create a new lane and switch to it
session.create_lane("feature-auth")

# Switch to an existing lane
session.switch_lane("main")
```

### Error Handling

When an exception occurs inside `work()`:

1. The workspace is snapshotted (capturing the partial state)
2. A transition is proposed with the error recorded in metadata
3. The transition is rejected
4. The workspace lock is released
5. The original exception is re-raised

This ensures that even failed work is recorded for debugging and cost tracking.

---

## Configuration

Repository configuration is stored in `.flanes/config.json`. It is created automatically by `flanes init`.

### All Fields

```json
{
  "version": "0.3.0",
  "default_lane": "main",
  "created_at": 1706999999.0,
  "max_blob_size": 104857600,
  "max_tree_depth": 100,
  "blob_threshold": 0,
  "evaluators": [],
  "embedding_api_url": "https://api.openai.com/v1",
  "embedding_api_key": "sk-...",
  "embedding_model": "text-embedding-3-small",
  "embedding_dimensions": 1536,
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-flanes-bucket",
    "prefix": "project-name/",
    "region": "us-east-1"
  }
}
```

### Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | string | `"0.3.0"` | Flanes version that created this repo |
| `default_lane` | string | `"main"` | Default lane for operations |
| `created_at` | float | (auto) | Unix timestamp of repo creation |
| `max_blob_size` | int | `104857600` | Maximum file size in bytes (100 MB). Set to 0 for default. |
| `max_tree_depth` | int | `100` | Maximum directory nesting depth. Set to 0 for default. |
| `blob_threshold` | int | `0` | Size threshold for external blob storage |
| `evaluators` | array | `[]` | List of evaluator configurations (see [Evaluators](#evaluators)) |
| `embedding_api_url` | string | - | OpenAI-compatible embedding API URL |
| `embedding_api_key` | string | - | API key for embedding service |
| `embedding_model` | string | - | Embedding model name |
| `embedding_dimensions` | int | - | Embedding vector dimensions |
| `remote_storage` | object | - | Remote storage configuration (see [Remote Storage](#remote-storage)) |

---

## Evaluators

Evaluators run shell commands (tests, linters, type checkers) against a workspace to validate transitions before accepting them.

### Setup

Add evaluators to `.flanes/config.json`:

```json
{
  "evaluators": [
    {
      "name": "pytest",
      "command": "pytest tests/ -x",
      "working_directory": null,
      "required": true,
      "timeout_seconds": 300
    },
    {
      "name": "ruff",
      "command": "ruff check .",
      "working_directory": null,
      "required": true,
      "timeout_seconds": 60
    },
    {
      "name": "mypy",
      "command": "mypy src/",
      "working_directory": null,
      "required": false,
      "timeout_seconds": 120
    }
  ]
}
```

### Evaluator Config Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | (required) | Display name for the evaluator |
| `command` | string | - | Shell command to execute (OS-dependent parsing) |
| `args` | array | - | Explicit argument list (cross-platform, recommended) |
| `working_directory` | string | `null` | Working directory for the command. If `null`, uses the workspace directory. |
| `required` | bool | `true` | If `true`, failure blocks acceptance. If `false`, failure is recorded but doesn't block. |
| `timeout_seconds` | int | `300` | Maximum execution time before the evaluator is killed |

**Note:** Either `command` or `args` must be provided. If both are given, `args` takes precedence.

### Cross-Platform Evaluators

The `command` field uses OS-dependent parsing:
- **Windows:** Passed as a single string to CreateProcess
- **POSIX:** Split using `shlex.split()`

For consistent cross-platform behavior, use the `args` array instead:

```json
{
  "evaluators": [
    {
      "name": "pytest",
      "args": ["python", "-m", "pytest", "tests/", "-x"],
      "required": true
    },
    {
      "name": "ruff",
      "args": ["ruff", "check", "."],
      "required": true
    }
  ]
}
```

### Running Evaluators

```bash
# Run evaluators against a specific transition
flanes evaluate <transition-id> --workspace main

# Evaluators run automatically during commit if configured
flanes commit --prompt "Add feature" --agent-id dev-1 --agent-type coder --auto-accept
```

### Required vs Optional

- **Required** evaluators must pass for a transition to be accepted. If any required evaluator fails, the transition is rejected.
- **Optional** evaluators record their results but don't block acceptance. Useful for advisory checks (e.g., code coverage, style suggestions).

### Auto-Accept Behavior

When using `--auto-accept`, evaluators still run but failures produce warnings instead of blocking:

```bash
flanes commit --prompt "Add feature" --agent-id dev-1 --agent-type coder --auto-accept
# Note: --auto-accept will run evaluators but won't block on failures
# ✓ Committed: abc123def456
#   Eval: ✗ pytest failed: 2 tests failed
```

This ensures evaluation data is always captured, even for automated commits. The evaluation result is stored in the transition metadata.

---

## Budgets

Budgets enforce per-lane cost limits on token usage, API calls, and wall time.

### Setting Budgets

```bash
# Set token limits
flanes budget set feature-auth \
  --max-tokens-in 100000 \
  --max-tokens-out 50000

# Set API call limit
flanes budget set feature-auth \
  --max-api-calls 500

# Set wall time limit (milliseconds)
flanes budget set feature-auth \
  --max-wall-time 3600000

# Set alert threshold (percentage of budget consumed before warning)
flanes budget set feature-auth \
  --alert-threshold 80
```

### Checking Budget Status

```bash
flanes budget show feature-auth
```

Output shows current usage against limits:

```
Budget for lane 'feature-auth':
  Tokens in:  45,000 / 100,000 (45%)
  Tokens out: 22,000 / 50,000  (44%)
  API calls:  120 / 500        (24%)
  Wall time:  850,000 / 3,600,000 ms (24%)
```

### Budget Config Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_tokens_in` | int | `null` | Maximum input tokens. `null` = unlimited. |
| `max_tokens_out` | int | `null` | Maximum output tokens. `null` = unlimited. |
| `max_api_calls` | int | `null` | Maximum API calls. `null` = unlimited. |
| `max_wall_time_ms` | int | `null` | Maximum wall time in milliseconds. `null` = unlimited. |
| `alert_threshold_pct` | int | `80` | Percentage of budget consumed that triggers a warning |

### Enforcement

When a budget limit is exceeded, `check_budget()` raises a `BudgetError`. This is checked during `propose()` and `checkpoint()` operations. The agent receives the error and can decide how to proceed (e.g., stop work, switch lanes, request budget increase).

Budget data is stored in the lane's metadata column, so no schema migration is required.

---

## Remote Storage

Remote storage enables team collaboration by syncing the local content-addressed store with a remote backend (S3 or GCS).

### S3 Setup

Add to `.flanes/config.json`:

```json
{
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-flanes-bucket",
    "prefix": "project-name/",
    "region": "us-east-1"
  }
}
```

Requires `boto3`. Install with `pip install flanes[s3]`.

AWS credentials are resolved through the standard boto3 chain (environment variables, `~/.aws/credentials`, IAM role, etc.).

### GCS Setup

```json
{
  "remote_storage": {
    "backend": "gcs",
    "bucket": "my-flanes-bucket",
    "prefix": "project-name/"
  }
}
```

Requires `google-cloud-storage`. Install with `pip install flanes[gcs]`.

GCP credentials are resolved through Application Default Credentials.

### Push / Pull / Status

```bash
# Push local objects to remote
flanes remote push

# Pull remote objects to local store
flanes remote pull

# Push/pull with lane metadata (transitions, intents, lane heads)
flanes remote push --metadata
flanes remote pull --metadata

# Check sync status (what's local-only, remote-only, synced)
flanes remote status
```

### How It Works

Remote sync operates at the CAS object level. Each blob and tree is an independently addressable object identified by its SHA-256 hash. Push uploads objects that exist locally but not remotely. Pull downloads objects that exist remotely but not locally. The content-addressed design means objects are naturally deduplicated: the same file content is only stored once regardless of how many states reference it.

### Integrity Verification

When pulling objects from remote storage, Flanes verifies the SHA-256 hash of each downloaded payload matches the expected object key. This protects against:

- **Corrupted storage:** bit rot or transfer errors
- **Malicious backends:** tampered data on shared or untrusted storage

Objects that fail integrity verification are logged and skipped. The pull result includes an `integrity_failures` count:

```json
{
  "pulled": 42,
  "skipped": 100,
  "errors": 0,
  "integrity_failures": 1,
  "total": 143
}
```

---

## Multi-Machine Collaboration

Flanes supports collaboration across multiple machines using remote storage as the synchronization layer.

### Architecture

Each machine has its own local Flanes repository with a full CAS and SQLite database. Remote storage (S3/GCS) acts as a shared object pool. Machines push and pull CAS objects (blobs, trees, and state snapshots) to stay in sync.

```
Machine A                  Remote (S3/GCS)                Machine B
┌──────────┐              ┌──────────────┐              ┌──────────┐
│ .flanes/    │──push──→     │ blobs/       │     ←──pull──│ .flanes/    │
│  cas/    │              │ trees/       │              │  cas/    │
│  db      │←──pull──     │ states/      │     ──push──→│  db      │
└──────────┘              └──────────────┘              └──────────┘
```

### Setup

1. Initialize Flanes on each machine:
   ```bash
   flanes init --lane main
   ```

2. Configure the same remote backend on each machine (`.flanes/config.json`):
   ```json
   {
     "remote_storage": {
       "backend": "s3",
       "bucket": "team-flanes-bucket",
       "prefix": "my-project/"
     }
   }
   ```

3. Push from the first machine, pull on the second:
   ```bash
   # Machine A: push local work (use --metadata to include lane history)
   flanes remote push --metadata

   # Machine B: pull remote objects and lane metadata
   flanes remote pull --metadata
   ```

### Workflow: Parallel Agents on Separate Machines

A common pattern is running independent agents on different machines, each working on a separate lane:

```bash
# Machine A: agent works on feature-auth
flanes lane create feature-auth
# ... agent does work, snapshots, proposes ...
flanes remote push --metadata

# Machine B: agent works on feature-api
flanes lane create feature-api
# ... agent does work, snapshots, proposes ...
flanes remote push --metadata

# Either machine: pull all work, review, promote
flanes remote pull --metadata
flanes history --lane feature-auth
flanes history --lane feature-api
flanes promote feature-auth --to main
flanes promote feature-api --to main
```

### Alternative: Git Bridge as Middleware

For teams already using Git, the git bridge can serve as a synchronization layer:

1. Each machine exports its Flanes history to a git repo
2. Git handles the multi-machine sync (push/pull/merge)
3. Other machines import from the shared git repo

```bash
# Machine A: export and push via git
flanes export-git ./sync-repo --lane main
cd ./sync-repo && git push origin main

# Machine B: pull via git and import
cd ./sync-repo && git pull origin main
flanes import-git ./sync-repo --lane imported
```

This approach trades some fidelity (Flanes metadata like cost records and evaluations are not preserved in git) for compatibility with existing git workflows.

### Limitations

- **SQLite is local-only.** Lane metadata, workspace state, and transition history live in the local SQLite database. By default, only CAS objects (blobs, trees, state snapshots) are synced via remote push/pull. Use `--metadata` to also sync lane metadata, transitions, and intents.
- **Conflict detection, not auto-merge.** When two machines create transitions on the same lane, `flanes remote pull --metadata` detects divergent heads and reports conflicts. Clean merges (different lanes or non-overlapping changes) are handled automatically. Conflicting same-lane work requires manual resolution.
- **NFS safety fencing.** Running two Flanes instances against the same `.flanes/` directory on a network filesystem (NFS, SMB) is detected and blocked. Flanes uses an instance lock to prevent cross-machine concurrent access to the same repository. Use remote push/pull for multi-machine collaboration instead.

---

## Git Bridge

The git bridge allows importing from and exporting to standard git repositories. This is useful for CI integration, sharing work with git-based tools, or migrating projects.

### Export to Git

```bash
# Export main lane history to a new git repo
flanes export-git ./my-project-git --lane main

# Export a different lane
flanes export-git ./feature-export --lane feature-auth
```

This creates a git repository at the target path with one commit per accepted transition. Commit messages are derived from transition prompts. The full file tree is materialized for each commit.

### Import from Git

```bash
# Import git history into a Flanes lane
flanes import-git ./existing-git-repo --lane imported

# Import into main lane
flanes import-git ./existing-git-repo --lane main
```

This walks the git log and creates a Flanes transition for each commit. File trees are ingested into the CAS.

### Notes

- The git bridge uses `git` commands via subprocess, so git must be installed and on PATH.
- Export creates a fresh git repo; it does not append to existing repos.
- Import creates transitions with agent_type `git-import`.

---

## Git + Flanes Coexistence

Flanes is designed to work alongside Git, not replace it. A common pattern is using Git as the source of truth for human collaboration and CI, while Flanes manages agent experiments and quality-gated work.

### Why Use Both?

| Concern | Git | Flanes |
|---------|-----|-----|
| Human collaboration | Branches, PRs, code review | - |
| CI/CD integration | Native support everywhere | Export via git bridge |
| Agent experiments | No quality gates | Propose/accept/reject cycle |
| Parallel agent work | Branch conflicts | Independent lanes, no conflicts |
| Cost tracking | - | Per-lane token and API call budgets |
| Rollback granularity | Commits | Snapshots (sub-commit checkpoints) |

### Setup

When you run `flanes init` inside an existing Git repository, Flanes detects this and reminds you to add `.flanes/` to your `.gitignore`:

```bash
cd my-git-project
flanes init
# Note: Detected existing Git repository.
#   Add '.flanes/' to your .gitignore:  echo '.flanes/' >> .gitignore
```

Add `.flanes/` to `.gitignore` to prevent Git from tracking Flanes's internal state:

```bash
echo '.flanes/' >> .gitignore
git add .gitignore
git commit -m "Ignore flanes directory"
```

### Recommended Workflow

1. **Git is the source of truth.** The `main` branch in Git represents the canonical project state.
2. **Flanes manages agent work.** Agents use Flanes lanes for experimental work with quality gates.
3. **Export approved work back to Git.** Use `flanes export-git` to create git commits from accepted Flanes transitions.

```bash
# Agent does work in Flanes
flanes lane create agent-feature
# ... agent proposes, evaluator accepts ...

# Export the approved lane to a git branch
flanes export-git ./export-dir --lane agent-feature
cd ./export-dir
git remote add origin <your-repo-url>
git push origin main:agent-feature

# Create a PR from the agent's work
# Review and merge as normal
```

### What Gets Tracked Where

- **Git tracks:** Source code, configuration, documentation, `.gitignore`
- **Flanes tracks (in `.flanes/`):** Agent snapshots, transition history, evaluations, cost records, CAS objects
- **Neither tracks:** Build artifacts, node_modules, virtual environments (add to both `.gitignore` and `.flanesignore`)

---

## Garbage Collection

Garbage collection removes unreachable objects and expired transitions to reclaim storage space.

### How It Works

Flanes uses a mark-and-sweep algorithm:

1. **Mark phase:** starting from lane heads, fork bases, and non-rejected transitions, walk all reachable objects (states, trees, blobs) and mark them as live.
2. **Sweep phase:** delete all unmarked objects and transitions older than the specified age.

A deferred transaction is used during the mark phase to prevent concurrent `accept` operations from creating objects that would be incorrectly swept.

### Usage

```bash
# Dry run: shows what would be deleted without deleting anything
flanes gc

# Actually delete (requires --confirm)
flanes gc --confirm

# Only delete objects older than 60 days (default: 30)
flanes gc --confirm --older-than 60
```

### Output

```
Garbage collection results:
  Reachable objects:     1,234
  Deleted objects:       56
  Deleted bytes:         12,345,678
  Deleted states:        8
  Deleted transitions:   12
  Pruned cache entries:  23
  Elapsed:           150 ms
```

### What Gets Deleted

- Rejected transitions older than the age threshold
- World states not reachable from any lane head or non-rejected transition
- CAS objects (blobs, trees) not referenced by any reachable state
- Superseded states that are no longer part of any lane's history
- Stale stat cache entries referencing deleted blobs (prevents unbounded cache growth)

### What Is Always Preserved

- All lane head states and their complete ancestry
- All non-rejected transitions (proposed and accepted)
- All objects reachable from preserved states

---

## Multi-repo Projects

Projects coordinate multiple Flanes repositories under a single umbrella for microservices or monorepo-style workflows.

### Initialize a Project

```bash
# In the parent directory
flanes project init --name my-microservices
```

This creates a `.flanes-project.json` file in the current directory.

### Add Repositories

```bash
# Add Flanes repos to the project
flanes project add services/auth auth-service --lane main
flanes project add services/api api-service --lane main
flanes project add services/frontend frontend --lane main
```

Each repo is identified by its filesystem path and given a logical mount point name.

### Check Status

```bash
flanes project status
```

Output:

```
Project: my-microservices
Root:    /path/to/project

Repos:
  auth-service:  a7d53265 [ok]
  api-service:   ee49ee72 [ok]
  frontend:      3f8b1c94 [ok]
```

### Coordinated Snapshot

```bash
flanes project snapshot
```

Snapshots all repos in the project. This captures a consistent point-in-time across all repositories.

### Project File Format

The `.flanes-project.json` file:

```json
{
  "name": "my-microservices",
  "created_at": 1706999999.0,
  "repos": [
    {
      "repo_path": "services/auth",
      "mount_point": "auth-service",
      "lane": "main"
    },
    {
      "repo_path": "services/api",
      "mount_point": "api-service",
      "lane": "main"
    }
  ]
}
```

---

## Templates

Templates provide reusable workspace scaffolding: predefined files, directories, and ignore patterns that can be applied when creating new workspaces.

### Creating a Template

```bash
flanes template create python-service --description "Python microservice with tests"
```

### Listing Templates

```bash
flanes template list
```

### Viewing Template Details

```bash
flanes template show python-service
```

### Template Storage

Templates are stored as JSON files in `.flanes/templates/<name>.json`:

```json
{
  "name": "python-service",
  "description": "Python microservice with tests",
  "files": [
    {
      "path": "main.py",
      "content": "def main():\n    pass\n"
    },
    {
      "path": "README.md",
      "source_hash": "a7d5326..."
    }
  ],
  "directories": ["tests/", "src/", "docs/"],
  "flaignore_patterns": ["*.pyc", "__pycache__/", ".env"]
}
```

### Template Files

Each file in a template can specify content in two ways:

- **`content`:** inline text content, stored directly in the template JSON
- **`source_hash`:** reference to a blob in the CAS, for binary or large files

### Security

- Template names are validated: no `..`, `/`, `\`, or null bytes
- File paths in templates are validated against path traversal attacks
- Files are only written within the target workspace directory

---

## MCP Server

The MCP (Model Context Protocol) server exposes Flanes operations as tools that LLMs can call directly.

### Starting the Server

```bash
flanes mcp
```

The server runs over stdio using JSON-RPC 2.0 with Content-Length framing (LSP-style), per the MCP specification.

### Available Tools

| Tool | Description |
|------|-------------|
| `fla_status` | Get repository status |
| `fla_snapshot` | Snapshot a workspace |
| `fla_commit` | Quick commit (snapshot + propose + accept) |
| `fla_history` | Show transition history |
| `fla_diff` | Diff two states |
| `fla_show` | Show file content from a state |
| `fla_search` | Search intents |
| `fla_lanes` | List lanes |

### Integration

The MCP server is designed to be launched as a subprocess by an LLM orchestrator. The orchestrator sends JSON-RPC requests over stdin and reads responses from stdout. Each tool accepts parameters as a JSON object and returns results as JSON.

---

## REST API

The REST API provides HTTP access to Flanes operations for web-based tools and remote agents.

### Starting the Server

```bash
# Default: 127.0.0.1:7654
flanes serve

# Custom host and port
flanes serve --host 0.0.0.0 --port 8080 --token my-secret

# With web viewer
flanes serve --web

# Bind to all interfaces without auth (NOT recommended for production)
flanes serve --host 0.0.0.0 --insecure
```

The server uses `ThreadingHTTPServer` for concurrent request handling. A lock serializes SQLite access to ensure thread safety.

### Authentication

The REST API supports bearer token authentication via the `FLANES_API_TOKEN` environment variable or the `--token` CLI flag:

```bash
# Via environment variable
export FLANES_API_TOKEN=my-secret-token
flanes serve

# Via CLI flag
flanes serve --token my-secret-token

# Clients send the token in the Authorization header
curl -H "Authorization: Bearer my-secret-token" http://127.0.0.1:7654/status
```

When a token is set, all endpoints except `GET /health` require valid authentication. Unauthenticated requests receive a `401 Unauthorized` response.

**Non-loopback safety:** If binding to a non-loopback address (e.g., `0.0.0.0`), Flanes requires an auth token or the `--insecure` flag. This prevents accidentally exposing an unauthenticated API to the network.

### Web Viewer

The `--web` flag enables a built-in web viewer at `/web/`:

```bash
flanes serve --web
# Web viewer: http://127.0.0.1:7654/web/
```

Static files are served from the bundled `flanes/web/` directory. The web viewer does not require authentication.

### GET Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check (no repo lock, fast) |
| `GET /status` | Repository status |
| `GET /head?lane=LANE` | Head state for a lane |
| `GET /lanes` | List all lanes |
| `GET /history?lane=LANE&limit=N&status=STATUS` | Transition history |
| `GET /states/<id>` | State details |
| `GET /states/<id>/files` | List files in a state |
| `GET /states/<id>/files/<path>` | Get file content (base64-encoded) |
| `GET /diff?a=STATE_A&b=STATE_B` | Diff two states |
| `GET /search?q=QUERY` | Search intents |
| `GET /objects/<hash>` | Raw CAS object (base64-encoded) |
| `GET /trace?state=STATE_ID` | Causal lineage |
| `GET /workspaces` | List workspaces |

### POST Endpoints

| Endpoint | Body | Description |
|----------|------|-------------|
| `POST /snapshot` | `{"workspace": "main"}` | Snapshot a workspace |
| `POST /propose` | `{"from_state", "to_state", "prompt", "agent_id", "agent_type", ...}` | Propose a transition |
| `POST /accept/<id>` | `{"evaluator": "...", "summary": "..."}` | Accept a transition |
| `POST /reject/<id>` | `{"evaluator": "...", "summary": "..."}` | Reject a transition |
| `POST /commit` | `{"workspace", "prompt", "agent_id", "agent_type", "auto_accept", ...}` | Quick commit |
| `POST /lanes` | `{"name": "...", "base": "..."}` | Create a lane |
| `POST /workspaces` | `{"name": "...", "lane": "...", "state_id": "..."}` | Create a workspace |
| `POST /gc` | `{"dry_run": true, "max_age_days": 30}` | Run garbage collection |

### DELETE Endpoints

| Endpoint | Description |
|----------|-------------|
| `DELETE /workspaces/<name>` | Remove a workspace |

### Response Format

All endpoints return JSON. Errors return `{"error": "message"}` with appropriate HTTP status codes (400 for client errors, 404 for not found, 500 for server errors).

---

## Repository Health

The `flanes doctor` command checks for and optionally fixes common repository issues.

### Running Doctor

```bash
# Check for issues (dry run)
flanes doctor

# Fix all fixable issues
flanes doctor --fix

# JSON output
flanes doctor --json
```

### Checks Performed

| Issue | Fixable | Description |
|-------|---------|-------------|
| Dirty workspaces | Yes | Workspace has a `.flanes_materializing` marker from an interrupted operation |
| Stale locks | Yes | Workspace lock held by a process that no longer exists (checked by PID) |
| Orphaned directories | Yes | Workspace directory exists but has no metadata file (`.json`) |
| Missing directories | Yes | Metadata file exists but workspace directory is missing |
| Lane without workspace | Yes | Lane exists in database but workspace was never created or was deleted |
| Workspace without lane | Yes | Workspace exists on disk but the lane record was deleted from the database |
| Version mismatch | No | Repository version doesn't match installed Flanes version (informational) |

### Example Output

```
[!] Workspace 'feature-auth' has interrupted operation marker
[!] Workspace 'main' has a stale lock (pid: 12345)
[X] Directory 'bugfix-parser' has no metadata file

3 issue(s) can be fixed with 'flanes doctor --fix'.
```

After `--fix`:

```
[fixed] Workspace 'feature-auth' has interrupted operation marker
[fixed] Workspace 'main' has a stale lock (pid: 12345)
[fixed] Directory 'bugfix-parser' has no metadata file

Fixed 3 issue(s).
```

---

## Limits & Safety

### Blob Size Limit

Maximum file size that can be stored in the CAS.

- **Default:** 100 MB (104,857,600 bytes)
- **Config key:** `max_blob_size`
- **Error:** `ContentStoreLimitError` when exceeded

Deduplication is checked before the size limit. If an identical blob already exists in the store, it is accepted regardless of the current limit setting.

### Tree Depth Limit

Maximum directory nesting depth.

- **Default:** 100 levels
- **Config key:** `max_tree_depth`
- **Error:** `TreeDepthLimitError` when exceeded

Prevents accidentally ingesting deeply nested `node_modules`-style trees.

### Lane Name Validation

Lane names are validated to prevent path traversal and injection:

- Cannot be empty
- Cannot contain `/` or `\` (use `-` instead)
- Cannot contain `..`
- Cannot contain null bytes

Invalid names are rejected with a descriptive error message suggesting the correct format.

### Workspace Name Validation

Workspace names must match the pattern `^[a-zA-Z0-9][a-zA-Z0-9._-]*$`:

- Must start with a letter or digit
- May contain letters, digits, dots, hyphens, and underscores
- No slashes, backslashes, spaces, or special characters
- Additionally verified to not escape the workspaces directory (belt-and-suspenders path containment check)

This strict regex prevents path traversal, directory injection, and platform-specific issues with special characters in directory names.

### Template Path Validation

Template file paths are validated against path traversal:

- Cannot contain `..`
- Cannot contain `/` or `\` at the start
- Must resolve to a location within the target workspace

### Workspace Locking

Workspaces use advisory locking via atomic `mkdir`:

- Main lock: `.flanes/main.lockdir/`
- Feature lock: `.flanes/workspaces/<name>.lockdir/`
- Owner file: `owner.json` inside the lock directory (contains PID, hostname, timestamp)
- Stale lock detection: `flanes doctor` checks if the owning PID is still alive
- Cross-platform: Works on Linux, macOS, and Windows

### Dirty Markers

During workspace materialization and update operations:

- A `.flanes_materializing` marker file is written inside the workspace
- If the process dies mid-operation, the marker persists
- `flanes doctor` detects and cleans up dirty workspaces
- Prevents using a workspace that may be in an inconsistent state

### .flanesignore Patterns

Create a `.flanesignore` file in your workspace root to exclude files from snapshots:

```
# Exact filename matches
.env
credentials.json

# Glob patterns (basename only)
*.pyc
*.log
test_*

# Path patterns (matches relative path)
build/output/*
docs/generated/*
node_modules/

# Directory patterns (trailing slash)
__pycache__/
.pytest_cache/

# Negation (re-include a previously ignored file)
!important.log
```

**Pattern matching rules:**

| Pattern | Matches |
|---------|---------|
| `*.log` | Any file ending in `.log` (basename match) |
| `build/output/*` | Files directly in `build/output/` (path match) |
| `test_*` | Files starting with `test_` (basename match) |
| `!keep.log` | Re-includes `keep.log` even if `*.log` is ignored |
| `cache/` | Directories named `cache` (directory pattern) |

**Default ignores** (always excluded):
- Version control: `.flanes`, `.git`, `.svn`, `.hg`
- Build artifacts: `__pycache__`, `node_modules`
- OS noise: `.DS_Store`, `Thumbs.db`
- Environment files: `.env`, `.env.local`, `.env.development`, `.env.production`, `.env.test`, `.env.staging`
- Credentials: `*.pem`, `*.key`, `*.p12`, `*.pfx`, `credentials.json`, `service-account.json`
- IDE: `.idea`, `.vscode`

A `.flanesignore` template file is auto-created on `flanes init` with common patterns commented out for easy customization.

### Symlink Handling

Symlinks are **skipped** during snapshot to prevent:

- Reading files outside the workspace (security risk)
- Non-deterministic snapshots (symlink targets may change)
- Circular references causing infinite loops

If you need symlinked content, copy it into the workspace instead.

### File Permissions

File permissions (mode bits) are preserved during snapshot and restored on materialize:

- Executable scripts remain executable after restore
- Mode is stored as the third element in tree entries: `(type, hash, mode)`
- Default mode: `0o644` for files, `0o755` for directories
- Note: `chmod` may silently fail on some filesystems (FAT32, some network mounts)

### Thread Safety

Flanes is safe to use from multiple threads, enabling multi-threaded agent orchestrators:

```python
from concurrent.futures import ThreadPoolExecutor
from flanes.repo import Repository

# Option 1: Share one Repository across threads
repo = Repository.find("./my-project")
with ThreadPoolExecutor(max_workers=4) as executor:
    # Multiple threads can call repo methods concurrently
    futures = [executor.submit(repo.status) for _ in range(4)]

# Option 2: One Repository per thread (best performance)
def worker():
    repo = Repository.find("./my-project")  # Each thread gets its own
    # ... do work ...
    repo.close()
```

**Implementation details:**

- SQLite connection uses `check_same_thread=False`
- WAL mode enables concurrent reads
- 30-second busy timeout handles write contention
- Writes are serialized via SQLite's internal locking

For highest throughput, create one `Repository` instance per thread. They safely share the same database file.

---

## Writing Plugins

Flanes supports plugins via Python entry points. Third-party packages can register evaluators, storage backends, and lifecycle hooks.

### Plugin Groups

| Entry Point Group | Purpose | Signature |
|-------------------|---------|-----------|
| `flanes.evaluators` | Custom evaluators (Python callables) | `(workspace_path: Path) -> EvaluatorResult` |
| `flanes.storage` | Remote storage backends | `(config: dict) -> RemoteBackend` |
| `flanes.hooks` | Lifecycle hooks | `(event: str, context: dict) -> None` |

### Evaluator Plugins

An evaluator plugin is a Python callable that receives the workspace path and returns an `EvaluatorResult`. Plugin evaluators run alongside configured shell-command evaluators.

```python
# my_plugin/evaluator.py
from pathlib import Path
from flanes.evaluators import EvaluatorResult

def check_readme(workspace_path: Path) -> EvaluatorResult:
    """Evaluator that checks if README.md exists."""
    readme = workspace_path / "README.md"
    return EvaluatorResult(
        name="readme-check",
        passed=readme.exists(),
        returncode=0 if readme.exists() else 1,
        stdout="README.md found" if readme.exists() else "",
        stderr="" if readme.exists() else "README.md missing",
        duration_ms=0.0,
    )
```

Register in `pyproject.toml`:

```toml
[project.entry-points."flanes.evaluators"]
readme-check = "my_plugin.evaluator:check_readme"
```

### Storage Backend Plugins

A storage backend plugin is a factory callable that receives the `remote_storage` config dict and returns a `RemoteBackend` instance.

```python
# my_plugin/storage.py
from flanes.remote import RemoteBackend

class AzureBackend(RemoteBackend):
    def __init__(self, container, prefix=""):
        # ... setup Azure Blob Storage client ...
        pass

    def upload(self, key, data): ...
    def download(self, key): ...
    def exists(self, key): ...
    def list_keys(self, prefix=""): ...
    def delete(self, key): ...

def create_azure_backend(config):
    return AzureBackend(
        container=config["container"],
        prefix=config.get("prefix", ""),
    )
```

Register and configure:

```toml
# pyproject.toml
[project.entry-points."flanes.storage"]
azure = "my_plugin.storage:create_azure_backend"
```

```json
// .flanes/config.json
{
  "remote_storage": {
    "type": "azure",
    "container": "my-container",
    "prefix": "flanes/"
  }
}
```

### Hook Plugins

Hooks run before and after key lifecycle events. They are called with the event name and a context dict. Hook failures are logged but never block the operation.

Available events: `pre_propose`, `post_propose`, `pre_accept`, `post_accept`, `pre_reject`, `post_reject`.

```python
# my_plugin/hooks.py
import logging

logger = logging.getLogger(__name__)

def audit_hook(event, context):
    """Log all lifecycle events for auditing."""
    logger.info("Flanes event: %s context=%s", event, context)
```

Register:

```toml
[project.entry-points."flanes.hooks"]
audit = "my_plugin.hooks:audit_hook"
```

### Plugin Discovery

Plugins are discovered automatically at runtime via `importlib.metadata.entry_points()`. Install a plugin package (e.g., `pip install my-flanes-plugin`) and Flanes will find it on the next operation. No configuration changes needed beyond installing the package.
