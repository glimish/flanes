![flanes](https://github.com/user-attachments/assets/54767074-159b-44b2-825c-bc482cef5e23)

**Version Control for Agentic AI Systems**

[![Tests](https://github.com/glimish/flanes/actions/workflows/test.yml/badge.svg)](https://github.com/glimish/flanes/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A versioned coordination layer for multi-agent parallel work. Not a git replacement -- a layer that makes parallel agent execution safe, observable, and recoverable.

Flanes is a content-addressed snapshot store, a state/transition ledger with structured intent and cost tracking, a workspace and locking manager with real per-lane directories, and a conservative integration primitive that detects path-level collisions instead of guessing merges.

## Why Not Just Git Branches?

Git is great for humans. Multi-agent code generation has different failure modes.

**Agents need a lifecycle, not just commits.** Flanes tracks proposals, evaluations, and accept/reject decisions, storing structured intent and cost per attempt. In git, rejected attempts get squashed away or left as junk branches. In agent systems, rejected attempts are signal -- cost data, failure modes, prompts that didn't work, regressions caught.

**Auto-merge is risky for agent edits.** Git will happily auto-merge same-file edits that compile but are semantically wrong. Flanes is conservative: if both sides touched the same path, promote stops and asks the orchestrator to decide (rerun from new base, manual resolution, or force overwrite).

**Isolation should be enforced, not social.** Git worktrees give you directories, but Flanes adds workspace locks and metadata so orchestrators can safely dispatch parallel agents without accidental stomps.

**Cost and intent are first-class.** Token usage, API call counts, wall time, semantic tags, and the exact prompt that caused a change are stored per transition -- not in sidecar databases or PR comments.

**Git remains the delivery format.** Use `flanes export-git` for CI, code review, and deployment. Use Flanes internally to manage agent work.

### A Concrete Failure Case Flanes Prevents

Agent A and Agent B fork from the same base. B lands first on main, touching `auth.py`. A later tries to merge and also touched `auth.py` in a different region. Git auto-merges cleanly -- no textual conflict. CI might even pass. But the combined semantics are subtly wrong: duplicate logic, broken invariants, wrong ordering.

Flanes flags a conflict immediately because both sides touched the same path. It refuses to guess a merge and forces the orchestrator to make a policy decision: rerun A from the new base, resolve manually, or force overwrite.

## Quick Start

```bash
pip install flanes
cd my-project
flanes init
```

```bash
# Agent commits work with structured metadata
flanes commit --prompt "Add auth module" \
  --agent-id coder-1 --agent-type feature_dev --auto-accept

# Create an isolated feature lane
flanes lane create feature-auth

# Work in isolation, promote back to main
flanes promote --workspace feature-auth --target main --auto-accept

# Query history with full intent and cost data
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

| Concept | What it is |
|---|---|
| **World State** | Immutable snapshot of the entire project. Agents propose new world states, not diffs. |
| **Intent** | Structured metadata for every change: the instruction, who issued it, cost tracking, semantic tags. |
| **Transition** | A proposal to move from one state to another. Must be *evaluated* before acceptance. |
| **Lane** | An isolated workstream. Work is *promoted* into a target lane through evaluation, not merged. |
| **Workspace** | Main workspace is the repo root (git-style). Feature lanes get physically isolated directories under `.flanes/workspaces/`. |

## Architecture

<img width="1536" height="1024" alt="fla_architecture" src="https://github.com/user-attachments/assets/a664e27a-8739-4b39-8297-94fef913c1cb" />

## Key Features

- **Git-style main:** repo root IS the main workspace -- files stay where you expect them
- **Physical isolation:** feature workspaces are real directories; parallel agents can't stomp on each other
- **Evaluation gating:** run pytest, ruff, or custom checks as gates before accepting transitions
- **Conservative promotion:** path-level collision detection without content merging
- **Cost tracking:** per-transition token usage, wall time, and API call counts
- **Smart incremental updates:** workspace sync writes only changed files, not the entire tree
- **Cross-platform locking:** atomic `mkdir` locking works on Linux, macOS, and Windows
- **Crash consistency:** atomic metadata writes, dirty markers for recovery, consistent GC ([details](docs/reliability.md))
- **Git bridge:** `flanes export-git` / `flanes import-git` for CI integration
- **Remote storage:** S3/GCS-backed sync for team collaboration
- **MCP server:** expose Flanes as tools for LLM integration via Model Context Protocol
- **REST API:** `flanes serve` starts a multi-threaded HTTP API with optional token auth
- **Garbage collection:** `flanes gc` removes rejected states and unreachable objects

## Non-Goals

Flanes is intentionally scoped. These are things it does **not** try to do:

- **Replace git for human collaboration.** Git excels at code review, branching workflows, and ecosystem integration. Flanes complements git; use `flanes export-git` for the human-facing side.
- **Content-level merge resolution.** Flanes detects path-level collisions and stops. It never guesses how to combine two edits to the same file.
- **Distributed consensus.** Flanes uses SQLite, not a distributed database. For multi-machine workflows, use `flanes remote push/pull`.
- **Package management or deployment.** Flanes tracks agent work. CI/CD remains your existing toolchain.

## Real-World Usage

**[Laneswarm](https://github.com/glimish/laneswarm)** -- a multi-agent autonomous coding orchestrator -- uses Flanes as its version control backend. It decomposes a project brief into a dependency-aware task graph, then dispatches parallel coder/reviewer/integrator agents that each work in isolated Flanes lanes. Every agent iteration is tracked as a Flanes transition with full cost accounting, and code is promoted to main only after passing verification gates.

## Documentation

- **[User Guide](docs/guide.md):** comprehensive reference for all features
- **[Data Model](docs/data-model.md):** hashing, object types, ignore rules, filesystem layout
- **[Reliability](docs/reliability.md):** crash consistency, durability guarantees, recovery
- **[Examples](examples/):** runnable demo scripts
- **[Contributing](CONTRIBUTING.md):** development setup and guidelines

## Workspace Layout

```
my-project/
+-- .flanes/
|   +-- config.json
|   +-- store.db                        # SQLite CAS + metadata
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
