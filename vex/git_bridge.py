"""
Git Bridge — Export/import between Vex and Git repositories.

All git operations use subprocess calls to the git CLI.
No gitpython dependency required.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .repo import Repository
from .state import AgentIdentity, EvaluationResult, Intent

GIT_TIMEOUT_SECONDS = 60


def _git(
    args: list, cwd: Path, env: dict | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess:
    """Run git command, raise RuntimeError on failure or timeout."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    if timeout is None:
        timeout = GIT_TIMEOUT_SECONDS
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            env=full_env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git {' '.join(args)} timed out after {timeout}s. "
            "This may indicate a hung git hook, network issue, or filesystem problem."
        )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return result


def _build_tree_from_flat(store, files: dict) -> str:
    """
    Build nested CAS trees from a flat {path: content_bytes} mapping.

    Groups files by top-level directory, recurses to build CAS trees
    bottom-up. Returns root tree hash.
    """
    # Separate files at this level vs subdirectories
    direct = {}      # name -> content bytes
    subdirs = {}     # dirname -> {subpath: content}

    for path, content in files.items():
        parts = path.split("/", 1)
        if len(parts) == 1:
            direct[parts[0]] = content
        else:
            dirname, subpath = parts
            if dirname not in subdirs:
                subdirs[dirname] = {}
            subdirs[dirname][subpath] = content

    entries = {}

    # Store direct blobs
    for name, content in direct.items():
        blob_hash = store.store_blob(content)
        entries[name] = ("blob", blob_hash)

    # Recurse into subdirectories
    for dirname, subfiles in subdirs.items():
        subtree_hash = _build_tree_from_flat(store, subfiles)
        entries[dirname] = ("tree", subtree_hash)

    return store.store_tree(entries)


def export_to_git(repo: Repository, target_dir: Path, lane: str = "main") -> dict:
    """
    Export Vex history to a git repository.

    Creates a git repo in target_dir with one commit per accepted transition.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    _git(["init"], cwd=target_dir)

    # Get accepted transitions and reverse to chronological order
    transitions = repo.history(lane=lane, limit=10000, status="accepted")
    transitions.reverse()

    commit_count = 0

    for t in transitions:
        to_state = repo.wsm.get_state(t["to_state"])
        if to_state is None:
            continue

        flat_files = repo.wsm._flatten_tree(to_state["root_tree"])

        # Clear target_dir except .git/
        for item in target_dir.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(str(item))
            else:
                item.unlink()

        # Write all files from CAS blobs
        for file_path, blob_hash in flat_files.items():
            obj = repo.store.retrieve(blob_hash)
            if obj is None:
                continue
            full_path = target_dir / file_path
            # Validate path stays within target_dir
            try:
                full_path.resolve().relative_to(target_dir.resolve())
            except ValueError:
                continue  # Skip paths that escape target_dir
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_bytes(obj.data)

        # Stage all changes
        _git(["add", "-A"], cwd=target_dir)

        # Build commit message
        agent = t.get("agent", {})
        agent_id = agent.get("agent_id", "unknown")
        agent_type = agent.get("agent_type", "unknown")
        prompt = t.get("intent_prompt", "No message")
        state_id = t["to_state"]

        message = f"{prompt}\n\nAgent: {agent_id} ({agent_type})\nVex-State: {state_id}"

        # Set dates from created_at
        created_at = t.get("created_at", 0)
        date_str = str(int(created_at))

        # Sanitize agent_id for use in git author/committer fields
        safe_agent_id = (agent_id.replace("<", "").replace(">", "")
                         .replace("\n", "").replace("\r", ""))

        # Set both author and committer info (needed for CI environments without global git config)
        env = {
            "GIT_AUTHOR_DATE": f"@{date_str}",
            "GIT_COMMITTER_DATE": f"@{date_str}",
            "GIT_COMMITTER_NAME": safe_agent_id,
            "GIT_COMMITTER_EMAIL": f"{safe_agent_id}@vex",
        }
        author = f"{safe_agent_id} <{safe_agent_id}@vex>"

        try:
            _git(
                ["commit", f"--author={author}", "-m", message, "--allow-empty"],
                cwd=target_dir,
                env=env,
            )
            commit_count += 1
        except RuntimeError as e:
            # Skip "nothing to commit" errors, re-raise others
            if "nothing to commit" in str(e).lower():
                pass
            else:
                raise

    return {"commits": commit_count, "target": str(target_dir)}


def import_from_git(source_dir: Path, repo: Repository, lane: str = "main") -> dict:
    """
    Import git history into a Vex repository.

    Each git commit becomes a world state + accepted transition.
    """
    source_dir = Path(source_dir)
    git_dir = source_dir / ".git"
    if not git_dir.exists():
        raise ValueError(f"Not a git repository: {source_dir}")

    # Get commit hashes in chronological order
    result = _git(["log", "--reverse", "--format=%H"], cwd=source_dir)
    commit_hashes = result.stdout.decode().strip().split("\n")
    commit_hashes = [h for h in commit_hashes if h.strip()]

    if not commit_hashes:
        return {"commits_imported": 0, "lane": lane}

    # Ensure lane exists
    existing_lanes = {ln["name"] for ln in repo.wsm.list_lanes()}
    if lane not in existing_lanes:
        repo.wsm.create_lane(lane, repo.head())

    prev_state = repo.wsm.get_lane_head(lane)
    commits_imported = 0

    for commit_hash in commit_hashes:
        # Get commit metadata using NUL separator to avoid multi-line body issues
        meta_result = _git(
            ["log", "-1", "--format=%s%x00%aN%x00%aE%x00%at", commit_hash],
            cwd=source_dir,
        )
        meta_parts = meta_result.stdout.decode("utf-8", errors="replace").strip().split("\0")

        subject = meta_parts[0] if len(meta_parts) > 0 else "Imported commit"
        author_name = meta_parts[1] if len(meta_parts) > 1 else "unknown"
        _author_email = meta_parts[2] if len(meta_parts) > 2 else "unknown@git"  # noqa: F841
        timestamp_str = meta_parts[3] if len(meta_parts) > 3 else "0"
        try:
            timestamp = float(timestamp_str)
        except (ValueError, TypeError):
            timestamp = 0.0

        # Get file list for this commit
        try:
            ls_result = _git(
                ["ls-tree", "-r", "--name-only", commit_hash],
                cwd=source_dir,
            )
            file_paths = ls_result.stdout.decode("utf-8", errors="replace").strip().split("\n")
            file_paths = [f for f in file_paths if f.strip()]
        except RuntimeError:
            file_paths = []

        # Read each file's content
        files = {}
        for file_path in file_paths:
            try:
                show_result = _git(
                    ["show", f"{commit_hash}:{file_path}"],
                    cwd=source_dir,
                )
                files[file_path] = show_result.stdout
            except RuntimeError:
                continue

        if not files:
            continue

        # Build CAS tree from files
        with repo.store.batch():
            root_tree = _build_tree_from_flat(repo.store, files)

        # Create world state
        state_id = repo.wsm._create_world_state(root_tree, parent_id=prev_state)

        # Create intent and propose transition
        agent = AgentIdentity(agent_id=author_name, agent_type="git-import")
        intent = Intent(
            id=str(uuid.uuid4()),
            prompt=subject,
            agent=agent,
            tags=["git-import"],
            created_at=timestamp,
        )

        tid = repo.wsm.propose(
            from_state=prev_state,
            to_state=state_id,
            intent=intent,
            lane=lane,
        )

        # Auto-accept
        repo.wsm.evaluate(tid, EvaluationResult(
            passed=True,
            evaluator="git-import",
            summary=f"Imported from git commit {commit_hash[:12]}",
        ))

        prev_state = state_id
        commits_imported += 1

    return {"commits_imported": commits_imported, "lane": lane}
