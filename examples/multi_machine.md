# Multi-Machine Collaboration with Flanes

This tutorial walks through setting up two machines to collaborate on a Flanes
project using S3 remote storage.

## Prerequisites

- Flanes installed on both machines (`pip install flanes[s3]`)
- An S3 bucket accessible from both machines
- AWS credentials configured (`~/.aws/credentials` or environment variables)

## Step 1: Initialize on Machine A

```bash
mkdir my-project && cd my-project
flanes init --lane main

# Configure remote storage
cat > .flanes/config.json << 'EOF'
{
  "version": "0.3.0",
  "default_lane": "main",
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-team-flanes",
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
flanes snapshot -m "Initial project files"
flanes commit -m "Project setup"

# Push to remote (--metadata syncs lane history too)
flanes remote push --metadata
# → Pushed 5 objects (3 blobs, 1 tree, 1 state)
```

## Step 3: Initialize on Machine B

```bash
mkdir my-project && cd my-project
flanes init --lane main

# Use the same remote config
cat > .flanes/config.json << 'EOF'
{
  "version": "0.3.0",
  "default_lane": "main",
  "remote_storage": {
    "backend": "s3",
    "bucket": "my-team-flanes",
    "prefix": "my-project/",
    "region": "us-east-1"
  }
}
EOF

# Pull objects and lane metadata from remote
flanes remote pull --metadata
# → Pulled 5 objects
```

## Step 4: Work in Parallel on Separate Lanes

```bash
# Machine A: work on authentication
flanes lane create feature-auth
echo "def login(): pass" > auth.py
flanes snapshot -m "Auth module scaffold"
flanes commit -m "Add auth module"
flanes remote push --metadata

# Machine B: work on API endpoints
flanes lane create feature-api
echo "def get_users(): pass" > api.py
flanes snapshot -m "API module scaffold"
flanes commit -m "Add API module"
flanes remote push --metadata
```

## Step 5: Sync and Review

```bash
# Either machine: pull everything (including lane metadata)
flanes remote pull --metadata

# Check remote status
flanes remote status
# → local_only: 0, remote_only: 0, synced: 15

# Review work from both lanes
flanes history --lane feature-auth
flanes history --lane feature-api

# Promote approved work to main
flanes promote feature-auth --to main
flanes promote feature-api --to main
flanes remote push --metadata
```

## Tips

- **Use `--metadata`** to sync lane history, transitions, and intents alongside CAS objects.
- **Use separate lanes per machine/agent** for the cleanest workflow. Same-lane work across machines is supported with conflict detection on pull.
- **Push frequently** so other machines can pull the latest objects.
- **Use `flanes remote status`** to check what needs syncing before starting work.
- **CAS deduplication** means identical file content is only stored and
  transferred once, even across lanes and machines.
- **NFS/shared filesystems:** Flanes detects and blocks cross-machine concurrent access to the same `.flanes/` directory. Always use remote push/pull instead.

## Alternative: GCS Backend

Replace the S3 config with GCS:

```json
{
  "remote_storage": {
    "backend": "gcs",
    "bucket": "my-team-flanes",
    "prefix": "my-project/"
  }
}
```

Install with `pip install flanes[gcs]` and configure Application Default
Credentials.
