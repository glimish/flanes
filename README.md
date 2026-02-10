![flanes](https://github.com/user-attachments/assets/54767074-159b-44b2-825c-bc482cef5e23)

**Version Control for Agentic AI Systems**

[![Tests](https://github.com/glimish/flanes/actions/workflows/test.yml/badge.svg)](https://github.com/glimish/flanes/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Version control designed from the ground up for AI agents. Replaces git's line-diff model with intent-based snapshots, physically isolated workspaces, and evaluation gating.

## Why Flanes?

Git assumes a single human making small, curated edits. AI agents break every part of that model.

| | Git | Flanes |
|---|---|---|
| **Unit of work** | Line diffs | Full world-state snapshots |
| **Change metadata** | Free-text commit message | Structured intent + agent identity |
| **Quality gate** | CI runs after merge | Evaluation gating before accept |
| **Parallel agents** | Branch conflicts | Physically isolated workspaces |
| **Cost tracking** | None | Per-transition token/API accounting |

## Quick Demo

```bash
cd my-project
flanes init
# Writes files, then commits in one step:
flanes commit --prompt "Add auth module" \
  --agent-id coder-1 --agent-type feature_dev --auto-accept

# Create isolated feature lane
flanes lane create feature-auth

# Work in isolation, promote back to main
flanes promote --workspace feature-auth --target main --auto-accept

# Query history
flanes history --lane main
```

Or use the Python SDK:

```python
from flanes.agent_sdk import AgentSession

session = AgentSession(
    repo_path="./my-project",
    agent_id="coder-alpha",
    agent_type="feature_developer",
)

with session.work("Add authentication module", tags=["auth"], auto_accept=True) as w:
    (w.path / "auth.py").write_text("def authenticate(): ...")
    w.record_tokens(tokens_in=2000, tokens_out=1200)
# On exit: snapshots -> proposes -> accepts (or rejects on exception)
```

See [`examples/`](examples/) for runnable demos.

## Installation

```bash
pip install flanes

# Optional: remote storage backends
pip install flanes[s3]    # Amazon S3 (boto3)
pip install flanes[gcs]   # Google Cloud Storage
```

## Core Concepts

**World States:** Immutable snapshots of the entire project. Agents propose new world states, not diffs.

**Intents:** Structured metadata for every change: the instruction, who issued it, cost tracking, and semantic tags.

**Transitions:** Proposals to move from one state to another. Must be *evaluated* before acceptance.

**Lanes:** Isolated workstreams. Work is *promoted* into a target lane through evaluation, not merged.

**Workspaces:** Main workspace is the repo root (git-style). Feature lanes get physically isolated directories under `.flanes/workspaces/`.

## Architecture

<img width="1536" height="1024" alt="fla_architecture" src="https://github.com/user-attachments/assets/a664e27a-8739-4b39-8297-94fef913c1cb" />

## Key Features

- **Git-style main:** repo root IS the main workspace. Files stay where you expect them.
- **Physical isolation:** feature workspaces are real directories. Parallel agents can't stomp on each other.
- **Smart incremental updates:** workspace sync writes only changed files, not the entire tree.
- **Cross-platform locking:** atomic `mkdir` locking works on Linux, macOS, and Windows.
- **Conflict detection:** promote finds path-level collisions without content merging.
- **Evaluators:** run pytest, ruff, or custom checks as gates before accepting transitions.
- **Cost tracking:** per-transition token usage, wall time, and API call counts.
- **Git bridge:** `flanes export-git` / `flanes import-git` for CI integration.
- **Remote storage:** S3/GCS-backed sync for team collaboration.
- **MCP server:** expose Flanes as tools for LLM integration via Model Context Protocol.
- **REST API:** `flanes serve` starts a multi-threaded HTTP API.
- **Garbage collection:** `flanes gc` removes rejected states and unreachable objects.

## Real-World Usage

**[Laneswarm](https://github.com/glimish/laneswarm)** — a multi-agent autonomous coding orchestrator — uses Flanes as its version control backend. It decomposes a project brief into a dependency-aware task graph, then dispatches parallel coder/reviewer/integrator agents that each work in isolated Flanes lanes. Every agent iteration is tracked as a Flanes transition with full cost accounting, and code is promoted to main only after passing verification gates.

## Documentation

- **[User Guide](docs/guide.md):** comprehensive reference for all features
- **[Examples](examples/):** runnable demo scripts
- **[Contributing](CONTRIBUTING.md):** development setup and guidelines

## Workspace Layout

```
my-project/
+-- .flanes/
|   +-- config.json
|   +-- store.db                        # SQLite database
|   +-- main.json                       # main workspace metadata
|   +-- workspaces/
|       +-- feature-auth/               # isolated feature workspace
|       +-- feature-auth.json
+-- app.py                              # YOUR FILES AT REPO ROOT
+-- lib/
```

## Running Tests

```bash
pip install -e ".[dev]"
python -X utf8 -m pytest tests/ -v
```

## License

[MIT](LICENSE)
