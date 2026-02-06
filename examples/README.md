# Fla Examples

## agent_workflow.py

Self-contained Python demo using the Fla Agent SDK. Shows:

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
interact with Fla through the command line.

```bash
bash examples/cli_workflow.sh
```

Requires `fla` to be installed: `pip install -e .`
