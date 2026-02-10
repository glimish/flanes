# Flanes Examples

## agent_workflow.py

Self-contained Python demo using the Flanes Agent SDK. Shows:

- Repository initialization
- Agent making changes via `AgentSession.work()`
- Feature lane creation with isolated workspace
- Promoting feature work back to main
- Querying transition history

```bash
# Run (temp dir, cleaned up automatically)
python examples/agent_workflow.py

# Run and keep the repo for inspection
python examples/agent_workflow.py --keep
```

## cli_workflow.sh

Same workflow using only CLI commands. Shows how agents or CI scripts
interact with Flanes through the command line.

```bash
bash examples/cli_workflow.sh
```

Requires `flanes` to be installed: `pip install -e .`

# Real-World Usage
[Laneswarm](https://github.com/glimish/laneswarm) is a multi-agent autonomous coding orchestrator that uses Flanes as its version control backend. It decomposes a project brief into a dependency-aware task graph, then dispatches parallel coder/reviewer/integrator agents that each work in isolated Flanes lanes. Every agent iteration is tracked as a Flanes transition with full cost accounting, and code is promoted to main only after passing verification gates.
