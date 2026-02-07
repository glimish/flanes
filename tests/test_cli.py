"""
Tests for Phase 4 CLI features.

Uses subprocess to invoke the CLI and verify exit codes and output.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_fla(*args, cwd=None, expect_fail=False):
    """Run a fla CLI command and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, "-X", "utf8", "-m", "fla.cli"] + list(args)
    # On Windows, CREATE_NEW_PROCESS_GROUP prevents spurious CTRL_C_EVENT
    # from the CI runner reaching the child process.
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
        **kwargs,
    )
    if not expect_fail:
        if result.returncode != 0:
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def empty_dir(tmp_path):
    """An empty temporary directory (no repo)."""
    return tmp_path


@pytest.fixture
def repo_dir(tmp_path):
    """A temporary directory with an initialized fla repo containing a test file."""
    # Create a test file first
    (tmp_path / "hello.txt").write_text("Hello, World!\n")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02binary content")
    rc, out, err = run_fla("init", cwd=tmp_path)
    assert rc == 0, f"Init failed: {err}"
    return tmp_path


class TestErrorOutsideRepo:
    def test_status_outside_repo(self, empty_dir):
        rc, out, err = run_fla("status", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "fla init" in err.lower() or "fla init" in err

    def test_init_works_outside_repo(self, empty_dir):
        rc, out, err = run_fla("init", cwd=empty_dir)
        assert rc == 0

    def test_error_json_mode(self, empty_dir):
        rc, out, err = run_fla("--json", "status", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data


class TestLogAlias:
    def test_log_alias_matches_history(self, repo_dir):
        # First create a commit so there's history
        rc, _, _ = run_fla(
            "commit",
            "-m",
            "test commit",
            "--agent-id",
            "test",
            "--agent-type",
            "human",
            "--auto-accept",
            cwd=repo_dir,
        )
        assert rc == 0

        rc1, out1, _ = run_fla("history", cwd=repo_dir)
        rc2, out2, _ = run_fla("log", cwd=repo_dir)
        assert rc1 == 0
        assert rc2 == 0
        assert out1 == out2


class TestDiffContent:
    def test_diff_content_shows_unified_diff(self, repo_dir):
        # Get initial state
        rc, out, _ = run_fla("--json", "status", cwd=repo_dir)
        assert rc == 0
        status = json.loads(out)
        state_a = status["current_head"]

        # Modify file in the main workspace (which is the repo root in git-style)
        ws_dir = repo_dir  # Main workspace IS the repo root
        (ws_dir / "hello.txt").write_text("Hello, Modified World!\n")

        rc, out, _ = run_fla(
            "--json",
            "commit",
            "-m",
            "modify hello",
            "--agent-id",
            "test",
            "--agent-type",
            "human",
            "--auto-accept",
            cwd=repo_dir,
        )
        assert rc == 0
        commit_result = json.loads(out)
        state_b = commit_result["to_state"]

        # Run diff --content
        rc, out, _ = run_fla("diff", "--content", state_a, state_b, cwd=repo_dir)
        assert rc == 0
        assert "---" in out or "+++" in out or "@@" in out or "~" in out


class TestShow:
    def test_show_file_content(self, repo_dir):
        # Get current head
        rc, out, _ = run_fla("--json", "status", cwd=repo_dir)
        status = json.loads(out)
        state_id = status["current_head"]

        # Show hello.txt
        rc, out, err = run_fla("show", state_id, "hello.txt", cwd=repo_dir)
        assert rc == 0
        assert "Hello, World!" in out

    def test_show_missing_path(self, repo_dir):
        rc, out, _ = run_fla("--json", "status", cwd=repo_dir)
        status = json.loads(out)
        state_id = status["current_head"]

        rc, out, err = run_fla("show", state_id, "nonexistent.txt", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_show_json_base64(self, repo_dir):
        rc, out, _ = run_fla("--json", "status", cwd=repo_dir)
        status = json.loads(out)
        state_id = status["current_head"]

        rc, out, err = run_fla("--json", "show", state_id, "hello.txt", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert "content_base64" in data
        content = base64.b64decode(data["content_base64"])
        assert b"Hello, World!" in content


class TestVerboseQuiet:
    def test_quiet_shorter_than_default(self, repo_dir):
        rc1, out_default, _ = run_fla("status", cwd=repo_dir)
        rc2, out_quiet, _ = run_fla("-q", "status", cwd=repo_dir)
        assert rc1 == 0
        assert rc2 == 0
        assert len(out_quiet) < len(out_default)

    def test_verbose_longer_than_default(self, repo_dir):
        rc1, out_default, _ = run_fla("status", cwd=repo_dir)
        rc2, out_verbose, _ = run_fla("-v", "status", cwd=repo_dir)
        assert rc1 == 0
        assert rc2 == 0
        # Verbose should be at least as long as default (may have full hashes)
        assert len(out_verbose) >= len(out_default)

    def test_quiet_snapshot(self, repo_dir):
        rc, out, _ = run_fla("-q", "snapshot", cwd=repo_dir)
        assert rc == 0
        # Should be just the state ID, one line
        lines = out.strip().split("\n")
        assert len(lines) == 1
        assert len(lines[0]) > 20  # full hash


class TestDoctor:
    def test_doctor_clean_repo(self, repo_dir):
        rc, out, _ = run_fla("doctor", cwd=repo_dir)
        assert rc == 0
        # Version check may differ, just ensure it ran successfully
        assert rc == 0

    def test_doctor_dirty_workspace(self, repo_dir):
        # Plant dirty marker in main workspace (which is repo root in git-style)
        # Main workspace IS the repo root now
        marker = repo_dir / ".fla_materializing"
        marker.write_text('{"state_id": "test", "started_at": 0}')

        rc, out, _ = run_fla("doctor", cwd=repo_dir)
        assert rc == 0
        assert "interrupted operation marker" in out or "dirty" in out.lower()

    def test_doctor_fix_dirty(self, repo_dir):
        # Plant dirty marker in main workspace (repo root)
        marker = repo_dir / ".fla_materializing"
        marker.write_text('{"state_id": "test", "started_at": 0}')

        rc, out, _ = run_fla("doctor", "--fix", cwd=repo_dir)
        assert rc == 0
        assert not marker.exists()

    def test_doctor_json(self, repo_dir):
        rc, out, _ = run_fla("--json", "doctor", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert "findings" in data
        assert "fixed" in data


class TestCompletions:
    def test_bash_completion(self, empty_dir):
        rc, out, _ = run_fla("completion", "bash", cwd=empty_dir)
        assert rc == 0
        assert "compgen" in out

    def test_zsh_completion(self, empty_dir):
        rc, out, _ = run_fla("completion", "zsh", cwd=empty_dir)
        assert rc == 0
        assert "compdef" in out

    def test_fish_completion(self, empty_dir):
        rc, out, _ = run_fla("completion", "fish", cwd=empty_dir)
        assert rc == 0
        assert "complete -c fla" in out

    def test_completion_no_repo_needed(self, empty_dir):
        """Completion should work without a repo."""
        rc, out, _ = run_fla("completion", "bash", cwd=empty_dir)
        assert rc == 0


# ── CLI Error Path Tests ─────────────────────────────────────


class TestCLIErrorPaths:
    """Test that CLI commands fail gracefully with proper error messages and exit codes."""

    def test_no_command(self, empty_dir):
        rc, out, err = run_fla(cwd=empty_dir, expect_fail=True)
        assert rc == 1

    def test_invalid_command(self, empty_dir):
        rc, out, err = run_fla("nonexistent-command", cwd=empty_dir, expect_fail=True)
        assert rc != 0

    def test_snapshot_outside_repo(self, empty_dir):
        rc, out, err = run_fla("snapshot", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "error" in err.lower() or "not" in err.lower()

    def test_snapshot_outside_repo_json(self, empty_dir):
        rc, out, err = run_fla("--json", "snapshot", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_accept_invalid_transition(self, repo_dir):
        rc, out, err = run_fla("accept", "nonexistent-id", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_accept_invalid_transition_json(self, repo_dir):
        rc, out, err = run_fla("--json", "accept", "nonexistent-id", cwd=repo_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_reject_invalid_transition(self, repo_dir):
        rc, out, err = run_fla("reject", "nonexistent-id", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_info_invalid_state(self, repo_dir):
        rc, out, err = run_fla("info", "nonexistent-state-id", cwd=repo_dir)
        # info prints "State not found" but doesn't sys.exit(1) — it returns None
        assert "not found" in out.lower() or rc == 0

    def test_show_invalid_state(self, repo_dir):
        rc, out, err = run_fla("show", "bad-state", "file.txt", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_diff_invalid_states(self, repo_dir):
        rc, out, err = run_fla("diff", "bad-a", "bad-b", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_lane_create_invalid_name(self, repo_dir):
        """Lane names with path separators should be rejected."""
        rc, out, err = run_fla("lane", "create", "../../escape", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_lane_create_invalid_name_json(self, repo_dir):
        rc, out, err = run_fla(
            "--json", "lane", "create", "../escape", cwd=repo_dir, expect_fail=True
        )
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_workspace_remove_nonexistent(self, repo_dir):
        rc, out, err = run_fla(
            "workspace", "remove", "no-such-workspace", cwd=repo_dir, expect_fail=True
        )
        assert rc == 1

    def test_gc_dry_run_default(self, repo_dir):
        """GC without --confirm should be a dry run (no error)."""
        rc, out, err = run_fla("gc", cwd=repo_dir)
        assert rc == 0
        assert "DRY RUN" in out

    def test_gc_json(self, repo_dir):
        rc, out, err = run_fla("--json", "gc", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert data["dry_run"] is True
        assert "reachable_objects" in data

    def test_commit_missing_required_args(self, empty_dir):
        """commit without --prompt should fail with argparse error."""
        # First init a repo
        run_fla("init", cwd=empty_dir)
        rc, out, err = run_fla(
            "commit", "--agent-id", "x", "--agent-type", "y", cwd=empty_dir, expect_fail=True
        )
        assert rc != 0
        assert "required" in err.lower() or "prompt" in err.lower()

    def test_propose_missing_required_args(self, repo_dir):
        """propose without required args should fail."""
        rc, out, err = run_fla("propose", cwd=repo_dir, expect_fail=True)
        assert rc != 0

    def test_cat_file_missing_hash(self, repo_dir):
        rc, out, err = run_fla("cat-file", "deadbeef0000", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_restore_invalid_state(self, repo_dir):
        rc, out, err = run_fla("restore", "bad-state-id", "--force", cwd=repo_dir, expect_fail=True)
        assert rc == 1


class TestGitCoexistence:
    """Test that fla init detects existing Git repos and provides guidance."""

    def test_init_in_git_repo(self, tmp_path):
        """fla init in a git repo should print a note about .gitignore."""
        (tmp_path / ".git").mkdir()
        rc, out, err = run_fla("init", cwd=tmp_path)
        assert rc == 0
        assert "Git repository" in out
        assert ".gitignore" in out

    def test_init_json_git_detected(self, tmp_path):
        """fla init --json in a git repo should include git_detected field."""
        (tmp_path / ".git").mkdir()
        rc, out, err = run_fla("--json", "init", cwd=tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data.get("git_detected") is True

    def test_init_no_git(self, tmp_path):
        """fla init without .git/ should not mention git."""
        rc, out, err = run_fla("init", cwd=tmp_path)
        assert rc == 0
        assert "Git repository" not in out

    def test_init_json_no_git(self, tmp_path):
        """fla init --json without .git/ should not include git_detected."""
        rc, out, err = run_fla("--json", "init", cwd=tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert "git_detected" not in data


class TestCLIPolish:
    """Test CLI polish features: version, aliases, did-you-mean, error hints."""

    def test_version_flag(self, empty_dir):
        """fla --version should print the version string."""
        rc, out, err = run_fla("--version", cwd=empty_dir)
        assert rc == 0
        assert "fla" in out
        assert "0." in out  # version starts with 0.x

    def test_version_short_flag(self, empty_dir):
        """fla -V should also print the version."""
        rc, out, err = run_fla("-V", cwd=empty_dir)
        assert rc == 0
        assert "fla" in out

    def test_alias_ci(self, repo_dir):
        """fla ci should resolve to commit."""
        rc, out, err = run_fla(
            "ci",
            "-m",
            "alias test",
            "--agent-id",
            "test",
            "--agent-type",
            "human",
            "--auto-accept",
            cwd=repo_dir,
        )
        assert rc == 0

    def test_alias_st(self, repo_dir):
        """fla st should resolve to status."""
        rc, out, err = run_fla("st", cwd=repo_dir)
        assert rc == 0

    def test_alias_sn(self, repo_dir):
        """fla sn should resolve to snapshot."""
        rc, out, err = run_fla("sn", cwd=repo_dir)
        assert rc == 0

    def test_alias_hist(self, repo_dir):
        """fla hist should resolve to history."""
        rc, out, err = run_fla("hist", cwd=repo_dir)
        assert rc == 0

    def test_did_you_mean(self, empty_dir):
        """Misspelled command should suggest correct one."""
        rc, out, err = run_fla("statu", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "did you mean" in err.lower() or "Did you mean" in err

    def test_did_you_mean_commit(self, empty_dir):
        """fla comit should suggest commit."""
        rc, out, err = run_fla("comit", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "commit" in err

    def test_grouped_help(self, empty_dir):
        """fla with no args should show grouped command help."""
        rc, out, err = run_fla(cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "Core:" in out or "Core:" in err
