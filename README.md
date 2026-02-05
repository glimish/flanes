# Vex — Version Control for Agentic AI Systems

Version control designed from the ground up for AI agents. Replaces git's line-diff model with intent-based snapshots, physically isolated workspaces, and evaluation gating.

## Why Not Git?

Git assumes a single human making small, curated edits. AI agents break every part of that model: they rewrite whole files (making diffs noise), run concurrently (causing conflicts), and produce torrents of automated changes with no structured record of *why*. Vex replaces these assumptions with primitives designed for agentic workflows.

## Installation

```bash
pip install vex

# Optional: remote storage backends
pip install vex[s3]    # Amazon S3 support (boto3)
pip install vex[gcs]   # Google Cloud Storage support (google-cloud-storage)
```

## Core Concepts

**World States** — The unit of versioning is a complete, immutable snapshot of the entire project. Agents don't "commit changes." They propose a new world state. No partial commits, no dirty working directories.

**Intents** — Every change carries a structured intent: the instruction that caused it, who issued it, cost tracking, and semantic tags for search. You can query "show me everything related to authentication" without grepping commit messages.

**Transitions** — A transition proposes moving from one world state to another. Transitions must be *evaluated* (by tests, linters, humans, or other agents) before they're accepted. This is the gate.

**Lanes** — Isolated workstreams. They don't merge — they produce candidates that get *promoted* into a target lane through evaluation. Lane names use dashes (e.g., `feature-auth`, `bugfix-parser`). Slashes are not allowed.

**Workspaces** — The main workspace IS the repo root (like git). Feature lanes get physically isolated directories under `.vex/workspaces/<name>/`. This gives you familiar git-style behavior for main while still enabling parallel agents on feature lanes to work without stomping on each other.

**Promote** — The mechanism for composing work from lanes into a target. Detects file-level conflicts (same path modified on both sides), rebases cleanly when possible, and stops with a conflict report when not. No three-way content merge ever happens.

## Architecture

```
┌──────────────────────────────────────────────────┐
│                 CLI / Agent SDK                   │
│       (vex commands / AgentSession API)           │
├──────────────────────────────────────────────────┤
│                  Repository                       │
│   (propose, accept, promote, restore, etc)        │
├────────────────────┬─────────────────────────────┤
│  WorkspaceManager  │     WorldStateManager        │
│  (isolation,       │  (states, transitions,       │
│   locking,         │   lanes, history, trace,     │
│   materialization) │   conflict detection)        │
├────────────────────┴─────────────────────────────┤
│             Content-Addressed Store               │
│    (SHA-256 blobs + trees, dedup, integrity)      │
├──────────────────────────────────────────────────┤
│                    SQLite                          │
│         (WAL mode, single-file database)          │
└──────────────────────────────────────────────────┘
```

## Quick Start

### Initialize

```bash
cd my-project
vex init
# ✓ Initialized Vex repository at /path/to/my-project
#   Initial snapshot: a7d53265...
#   Lane: main
```

Existing files stay in place — the repo root IS the main workspace (like git). Only `.vex/` metadata is added.

### Agent Workflow (Python SDK)

```python
from vex.agent_sdk import AgentSession

session = AgentSession(
    repo_path="./my-project",
    agent_id="coder-alpha",
    agent_type="feature_developer",
    model="claude-sonnet-4-20250514",
)

with session.work("Add authentication module", tags=["auth"], auto_accept=True) as w:
    # w.path points to the isolated workspace directory
    (w.path / "lib" / "auth.py").write_text("def authenticate(): ...")
    w.record_tokens(tokens_in=2000, tokens_out=1200)

# On exit: snapshots workspace → proposes transition → accepts
# On exception: proposes → rejects, recording the error
```

See [docs/guide.md](docs/guide.md) for full SDK documentation including `WorkContext`, error handling, and lane management.

### Agent Workflow (CLI)

```bash
# Snapshot a workspace (auto-detects from cwd, or use --workspace)
vex snapshot --workspace main

# Quick commit: snapshot + propose + accept in one step
vex commit \
  --prompt "Fix null pointer in parser" \
  --agent-id debugger-beta \
  --agent-type bugfix \
  --workspace main \
  --auto-accept

# All commands support --json for agent consumption
vex history --json
```

### Workspaces

```bash
# Create a lane with its workspace
vex lane create feature-auth

# List workspaces
vex workspace list

# The workspace is a real directory — agents modify files directly
ls .vex/workspaces/feature-auth/

# Update a workspace to latest main (incremental — only changed files)
vex workspace update feature-auth --state <main-head-id>
```

### Promote

```bash
# Promote feature work into main
vex promote --workspace feature-auth --target main --auto-accept

# If files conflict (same path modified on both sides):
# ✗ Conflicts detected — cannot promote 'feature-auth' into 'main'
#   Conflicting files:
#     README.md  (lane: modified, target: modified)
#
#   To resolve: update the workspace, fix conflicts, then re-promote.
```

Promote does NOT merge file contents. It detects path-level conflicts and stops. The orchestrator decides what to do: re-run the agent from updated main, manual fix, or LLM-assisted resolution. Vex is the mechanism, not the policy.

### Query Operations

```bash
vex history --lane main --limit 10    # transition history
vex trace                              # causal lineage (why is this state here?)
vex diff <state-a> <state-b>          # file-level diff between any two states
vex search "authentication"            # search intents by text/tags
vex semantic-search "auth logic"       # embedding-based semantic search
vex lanes                              # list all lanes
vex status                             # full repo status
vex info <state-id>                    # details about a world state
vex restore <state-id> --workspace main  # restore workspace to any state
```

### Evaluators

Run configured test suites, linters, and other checks against transitions before accepting them:

```bash
vex evaluate <transition-id> --workspace main
```

Evaluators are configured in `.vex/config.json`. See [docs/guide.md](docs/guide.md) for setup details.

### Budgets

Track and limit per-lane token usage, API calls, and wall time:

```bash
vex budget show main
vex budget set feature-auth --max-tokens-in 100000 --max-tokens-out 50000
```

### Additional Features

- **Garbage Collection** — `vex gc` removes rejected/superseded states and unreachable objects
- **Git Bridge** — `vex export-git` / `vex import-git` to sync with git repos for CI integration
- **Remote Storage** — `vex remote push` / `vex remote pull` for S3/GCS-backed team collaboration
- **MCP Server** — `vex mcp` exposes Vex operations as tools for LLM integration via Model Context Protocol
- **REST API** — `vex serve` starts an HTTP API server for programmatic access
- **Multi-repo Projects** — `vex project init` coordinates snapshots across multiple Vex repositories
- **Templates** — `vex template create` / `vex template list` for reusable workspace scaffolding
- **Repository Health** — `vex doctor` detects and fixes stale locks, dirty workspaces, orphaned metadata

See [docs/guide.md](docs/guide.md) for comprehensive documentation on all features.

## Workspace Layout

```
my-project/
├── .vex/
│   ├── config.json
│   ├── store.db                        # SQLite database (states, transitions, lanes, CAS)
│   ├── main.json                       # main workspace metadata
│   ├── main.lockdir/                   # main workspace lock (when active)
│   │   └── owner.json
│   └── workspaces/
│       ├── feature-auth/               # isolated feature lane workspace
│       │   ├── main.py
│       │   └── lib/
│       ├── feature-auth.json
│       └── feature-auth.lockdir/
├── main.py                             # YOUR FILES STAY AT REPO ROOT
└── lib/
```

The main workspace IS the repo root (git-style). Feature lanes get isolated directories under `.vex/workspaces/`. This means `ls` shows your files, IDEs work naturally, but parallel agents on feature lanes still can't stomp on each other.

## Key Design Properties

**Git-style main.** The main workspace is the repo root itself. Files stay where you expect them — `ls` shows your project, IDEs work naturally, and `vex init` doesn't move anything.

**Physical isolation for feature lanes.** Feature workspaces are real directories under `.vex/workspaces/`. Two agents in separate workspaces cannot interfere with each other at the filesystem level.

**Smart incremental updates.** When syncing a workspace to a new state, Vex diffs the old and new trees and writes only changed files. On a large repo where 3 files changed, it writes 3 files — not 10,000.

**Cross-platform locking.** Workspace locking uses atomic `mkdir` (works on Linux, macOS, and Windows). Lock owner metadata includes PID and hostname for stale lock detection.

**Atomic metadata writes.** All workspace metadata is written via temp-file + rename to prevent corruption on crashes.

**Dirty markers.** During materialization/update operations, a `.vex_materializing` marker file is written inside the workspace. If the process dies mid-operation, the marker survives for recovery detection.

**Conflict detection without merging.** Promote finds the common ancestor, diffs both sides, and reports which paths collide. No content-level merge is attempted — that's a policy decision for the orchestration layer.

**Cost tracking.** Every transition records token usage (in/out), wall time, and API calls. Essential for optimizing multi-agent workloads.

**File permissions preserved.** Executable bits and file modes are captured during snapshot and restored on materialize. Scripts stay executable.

**Symlinks skipped.** Symlinks are not followed during snapshot to prevent reading files outside the workspace — important for security in agent environments.

**.vexignore support.** Exclude files from snapshots with glob patterns (`*.pyc`), path patterns (`build/output/*`), and negation (`!important.log`).

## File Structure

```
vex/
├── __init__.py         # Package exports, version
├── cas.py              # Content-Addressed Store (SHA-256 blobs + Merkle trees)
├── state.py            # WorldState, Transition, Intent, Lane, conflict detection
├── workspace.py        # Workspace isolation, locking, materialization, dirty markers
├── repo.py             # Repository — high-level API (propose, accept, promote, etc)
├── agent_sdk.py        # AgentSession / WorkContext — Python SDK for agents
├── cli.py              # CLI interface (all commands, workspace auto-detection)
├── evaluators.py       # Shell-command evaluators (pytest, ruff, etc.)
├── budgets.py          # Per-lane cost tracking, alerts, enforcement
├── templates.py        # Workspace templates (create, apply, manage)
├── gc.py               # Mark-and-sweep garbage collection
├── git_bridge.py       # Export/import between Vex and git
├── remote.py           # Remote storage backends (S3, GCS) + sync
├── mcp_server.py       # MCP tool server (JSON-RPC 2.0 over stdio)
├── server.py           # REST API server (stdlib http.server)
├── project.py          # Multi-repo project coordination
├── embeddings.py       # Embedding client for semantic search
└── completions.py      # Shell completion scripts (bash, zsh, fish)
tests/
├── test_integration.py # Full integration test suite
├── test_cli.py         # CLI command tests
├── test_cas.py         # Content-addressed store tests
├── test_repository.py  # Repository API tests
└── ...                 # 20 test files total
```

## Running Tests

```bash
pytest tests/
```

## License

MIT
