# Multi-Machine Collaboration with Fla

This tutorial walks through setting up two machines to collaborate on a Fla
project using S3 remote storage.

## Prerequisites

- Fla installed on both machines (`pip install fla[s3]`)
- An S3 bucket accessible from both machines
- AWS credentials configured (`~/.aws/credentials` or environment variables)

## Step 1: Initialize on Machine A

```bash
mkdir my-project && cd my-project
fla init --lane main

# Configure remote storage
cat > .fla/config.json << 'EOF'
{
  "version": "0.3.0",
  "default_lane": "main",
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-team-fla",
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
fla snapshot -m "Initial project files"
fla commit -m "Project setup"

# Push to remote
fla remote push
# → Pushed 5 objects (3 blobs, 1 tree, 1 state)
```

## Step 3: Initialize on Machine B

```bash
mkdir my-project && cd my-project
fla init --lane main

# Use the same remote config
cat > .fla/config.json << 'EOF'
{
  "version": "0.3.0",
  "default_lane": "main",
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-team-fla",
    "prefix": "my-project/",
    "region": "us-east-1"
  }
}
EOF

# Pull objects from remote
fla remote pull
# → Pulled 5 objects
```

## Step 4: Work in Parallel on Separate Lanes

```bash
# Machine A: work on authentication
fla lane create feature-auth
echo "def login(): pass" > auth.py
fla snapshot -m "Auth module scaffold"
fla commit -m "Add auth module"
fla remote push

# Machine B: work on API endpoints
fla lane create feature-api
echo "def get_users(): pass" > api.py
fla snapshot -m "API module scaffold"
fla commit -m "Add API module"
fla remote push
```

## Step 5: Sync and Review

```bash
# Either machine: pull everything
fla remote pull

# Check remote status
fla remote status
# → local_only: 0, remote_only: 0, synced: 15

# Review work from both lanes
fla history --lane feature-auth
fla history --lane feature-api

# Promote approved work to main
fla promote feature-auth --to main
fla promote feature-api --to main
fla remote push
```

## Tips

- **Use separate lanes per machine/agent** to avoid conflicts. Fla does not
  merge divergent histories on the same lane.
- **Push frequently** so other machines can pull the latest objects.
- **Use `fla remote status`** to check what needs syncing before starting work.
- **CAS deduplication** means identical file content is only stored and
  transferred once, even across lanes and machines.

## Alternative: GCS Backend

Replace the S3 config with GCS:

```json
{
  "remote_storage": {
    "backend": "gcs",
    "bucket": "my-team-fla",
    "prefix": "my-project/"
  }
}
```

Install with `pip install fla[gcs]` and configure Application Default
Credentials.
