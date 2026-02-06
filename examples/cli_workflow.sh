#!/usr/bin/env bash
#
# Fla CLI Workflow Example
#
# Demonstrates the core Fla workflow using only CLI commands.
# Requires: fla installed (pip install -e .)
#
# Usage:
#   bash examples/cli_workflow.sh
#

set -euo pipefail

DEMO_DIR=$(mktemp -d -t fla-cli-demo-XXXXXX)
echo "=== Fla CLI Workflow Demo ==="
echo "Working directory: $DEMO_DIR"
echo

cleanup() {
    rm -rf "$DEMO_DIR"
    echo "Cleaned up $DEMO_DIR"
}
trap cleanup EXIT

cd "$DEMO_DIR"

# ── Step 1: Initialize ──────────────────────────────────────────
echo "--- Step 1: Initialize repository ---"
echo 'def main(): print("Hello")' > app.py
echo "# My Project" > README.md
fla init
echo

# ── Step 2: Make changes and commit ─────────────────────────────
echo "--- Step 2: Agent commits changes ---"
echo 'def greet(name): return f"Hello, {name}!"' > utils.py
fla commit \
  --prompt "Add utility module" \
  --agent-id coder-alpha \
  --agent-type feature_developer \
  --auto-accept
echo

# ── Step 3: Create feature lane ─────────────────────────────────
echo "--- Step 3: Create feature lane ---"
fla lane create feature-auth
echo "Workspaces:"
fla workspace list
echo

# ── Step 4: Work on feature lane ────────────────────────────────
echo "--- Step 4: Work on feature lane ---"
WS_DIR=".fla/workspaces/feature-auth"
mkdir -p "$WS_DIR/auth"
echo 'def login(): pass' > "$WS_DIR/auth/login.py"
fla commit \
  --prompt "Add auth module" \
  --agent-id auth-agent \
  --agent-type security \
  --workspace feature-auth \
  --auto-accept
echo

# ── Step 5: Promote to main ─────────────────────────────────────
echo "--- Step 5: Promote feature-auth to main ---"
fla promote \
  --workspace feature-auth \
  --target main \
  --auto-accept
echo

# ── Step 6: View history ────────────────────────────────────────
echo "--- Step 6: View history ---"
fla history --lane main
echo

echo "--- Step 7: View status ---"
fla status
echo

echo "=== Demo complete ==="
