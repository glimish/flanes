# Multi-Machine Collaboration with Vex

This tutorial walks through setting up two machines to collaborate on a Vex
project using S3 remote storage.

## Prerequisites

- Vex installed on both machines (`pip install vex[s3]`)
- An S3 bucket accessible from both machines
- AWS credentials configured (`~/.aws/credentials` or environment variables)

## Step 1: Initialize on Machine A

```bash
mkdir my-project && cd my-project
vex init --lane main

# Configure remote storage
cat > .vex/config.json << 'EOF'
{
  "version": "0.3.0",
  "default_lane": "main",
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-team-vex",
    "prefix": "my-project/",
    "region": "us-east-1"
  }
}
EOF
```

## Step 2: Do Work on Machine A

```bash
# Create a workspace and do some work
echo "# My Project" > README.md
echo "print('hello')" > main.py
vex snapshot -m "Initial project files"
vex commit -m "Project setup"

# Push to remote
vex remote push
# → Pushed 5 objects (3 blobs, 1 tree, 1 state)
```

## Step 3: Initialize on Machine B

```bash
mkdir my-project && cd my-project
vex init --lane main

# Use the same remote config
cat > .vex/config.json << 'EOF'
{
  "version": "0.3.0",
  "default_lane": "main",
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-team-vex",
    "prefix": "my-project/",
    "region": "us-east-1"
  }
}
EOF

# Pull objects from remote
vex remote pull
# → Pulled 5 objects
```

## Step 4: Work in Parallel on Separate Lanes

```bash
# Machine A: work on authentication
vex lane create feature-auth
echo "def login(): pass" > auth.py
vex snapshot -m "Auth module scaffold"
vex commit -m "Add auth module"
vex remote push

# Machine B: work on API endpoints
vex lane create feature-api
echo "def get_users(): pass" > api.py
vex snapshot -m "API module scaffold"
vex commit -m "Add API module"
vex remote push
```

## Step 5: Sync and Review

```bash
# Either machine: pull everything
vex remote pull

# Check remote status
vex remote status
# → local_only: 0, remote_only: 0, synced: 15

# Review work from both lanes
vex history --lane feature-auth
vex history --lane feature-api

# Promote approved work to main
vex promote feature-auth --to main
vex promote feature-api --to main
vex remote push
```

## Tips

- **Use separate lanes per machine/agent** to avoid conflicts. Vex does not
  merge divergent histories on the same lane.
- **Push frequently** so other machines can pull the latest objects.
- **Use `vex remote status`** to check what needs syncing before starting work.
- **CAS deduplication** means identical file content is only stored and
  transferred once, even across lanes and machines.

## Alternative: GCS Backend

Replace the S3 config with GCS:

```json
{
  "remote_storage": {
    "backend": "gcs",
    "bucket": "my-team-vex",
    "prefix": "my-project/"
  }
}
```

Install with `pip install vex[gcs]` and configure Application Default
Credentials.
