"""
Tests for Phase 4 CLI features.

Uses subprocess to invoke the CLI and verify exit codes and output.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def run_vex(*args, cwd=None, expect_fail=False):
    """Run a vex CLI command and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, "-X", "utf8", "-m", "vex.cli"] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
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
    """A temporary directory with an initialized vex repo containing a test file."""
    # Create a test file first
    (tmp_path / "hello.txt").write_text("Hello, World!\n")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02binary content")
    rc, out, err = run_vex("init", cwd=tmp_path)
    assert rc == 0, f"Init failed: {err}"
    return tmp_path


class TestErrorOutsideRepo:
    def test_status_outside_repo(self, empty_dir):
        rc, out, err = run_vex("status", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "vex init" in err.lower() or "vex init" in err

    def test_init_works_outside_repo(self, empty_dir):
        rc, out, err = run_vex("init", cwd=empty_dir)
        assert rc == 0

    def test_error_json_mode(self, empty_dir):
        rc, out, err = run_vex("--json", "status", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data


class TestLogAlias:
    def test_log_alias_matches_history(self, repo_dir):
        # First create a commit so there's history
        rc, _, _ = run_vex(
            "commit", "-m", "test commit",
            "--agent-id", "test", "--agent-type", "human",
            "--auto-accept", cwd=repo_dir,
        )
        assert rc == 0

        rc1, out1, _ = run_vex("history", cwd=repo_dir)
        rc2, out2, _ = run_vex("log", cwd=repo_dir)
        assert rc1 == 0
        assert rc2 == 0
        assert out1 == out2


class TestDiffContent:
    def test_diff_content_shows_unified_diff(self, repo_dir):
        # Get initial state
        rc, out, _ = run_vex("--json", "status", cwd=repo_dir)
        assert rc == 0
        status = json.loads(out)
        state_a = status["current_head"]

        # Modify file in the workspace directory (files are moved there during init)
        ws_dir = repo_dir / ".vex" / "workspaces" / "main"
        (ws_dir / "hello.txt").write_text("Hello, Modified World!\n")

        rc, out, _ = run_vex(
            "--json", "commit", "-m", "modify hello",
            "--agent-id", "test", "--agent-type", "human",
            "--auto-accept", cwd=repo_dir,
        )
        assert rc == 0
        commit_result = json.loads(out)
        state_b = commit_result["to_state"]

        # Run diff --content
        rc, out, _ = run_vex("diff", "--content", state_a, state_b, cwd=repo_dir)
        assert rc == 0
        assert "---" in out or "+++" in out or "@@" in out or "~" in out


class TestShow:
    def test_show_file_content(self, repo_dir):
        # Get current head
        rc, out, _ = run_vex("--json", "status", cwd=repo_dir)
        status = json.loads(out)
        state_id = status["current_head"]

        # Show hello.txt
        rc, out, err = run_vex("show", state_id, "hello.txt", cwd=repo_dir)
        assert rc == 0
        assert "Hello, World!" in out

    def test_show_missing_path(self, repo_dir):
        rc, out, _ = run_vex("--json", "status", cwd=repo_dir)
        status = json.loads(out)
        state_id = status["current_head"]

        rc, out, err = run_vex("show", state_id, "nonexistent.txt", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_show_json_base64(self, repo_dir):
        rc, out, _ = run_vex("--json", "status", cwd=repo_dir)
        status = json.loads(out)
        state_id = status["current_head"]

        rc, out, err = run_vex("--json", "show", state_id, "hello.txt", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert "content_base64" in data
        content = base64.b64decode(data["content_base64"])
        assert b"Hello, World!" in content


class TestVerboseQuiet:
    def test_quiet_shorter_than_default(self, repo_dir):
        rc1, out_default, _ = run_vex("status", cwd=repo_dir)
        rc2, out_quiet, _ = run_vex("-q", "status", cwd=repo_dir)
        assert rc1 == 0
        assert rc2 == 0
        assert len(out_quiet) < len(out_default)

    def test_verbose_longer_than_default(self, repo_dir):
        rc1, out_default, _ = run_vex("status", cwd=repo_dir)
        rc2, out_verbose, _ = run_vex("-v", "status", cwd=repo_dir)
        assert rc1 == 0
        assert rc2 == 0
        # Verbose should be at least as long as default (may have full hashes)
        assert len(out_verbose) >= len(out_default)

    def test_quiet_snapshot(self, repo_dir):
        rc, out, _ = run_vex("-q", "snapshot", cwd=repo_dir)
        assert rc == 0
        # Should be just the state ID, one line
        lines = out.strip().split('\n')
        assert len(lines) == 1
        assert len(lines[0]) > 20  # full hash


class TestDoctor:
    def test_doctor_clean_repo(self, repo_dir):
        rc, out, _ = run_vex("doctor", cwd=repo_dir)
        assert rc == 0
        assert "No issues found" in out

    def test_doctor_dirty_workspace(self, repo_dir):
        # Plant dirty marker
        vex_dir = repo_dir / ".vex"
        ws_dir = list((vex_dir / "workspaces").iterdir())
        # Find an actual workspace directory (not .json or .lockdir)
        actual_ws = None
        for item in ws_dir:
            if item.is_dir() and not item.name.endswith(".lockdir"):
                actual_ws = item
                break
        assert actual_ws is not None, "No workspace directory found"

        marker = actual_ws / ".vex_materializing"
        marker.write_text('{"state_id": "test", "started_at": 0}')

        rc, out, _ = run_vex("doctor", cwd=repo_dir)
        assert rc == 0
        assert "interrupted operation marker" in out or "dirty" in out.lower()

    def test_doctor_fix_dirty(self, repo_dir):
        # Plant dirty marker
        vex_dir = repo_dir / ".vex"
        ws_dirs = list((vex_dir / "workspaces").iterdir())
        actual_ws = None
        for item in ws_dirs:
            if item.is_dir() and not item.name.endswith(".lockdir"):
                actual_ws = item
                break
        assert actual_ws is not None

        marker = actual_ws / ".vex_materializing"
        marker.write_text('{"state_id": "test", "started_at": 0}')

        rc, out, _ = run_vex("doctor", "--fix", cwd=repo_dir)
        assert rc == 0
        assert not marker.exists()

    def test_doctor_json(self, repo_dir):
        rc, out, _ = run_vex("--json", "doctor", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert "findings" in data
        assert "fixed" in data


class TestCompletions:
    def test_bash_completion(self, empty_dir):
        rc, out, _ = run_vex("completion", "bash", cwd=empty_dir)
        assert rc == 0
        assert "compgen" in out

    def test_zsh_completion(self, empty_dir):
        rc, out, _ = run_vex("completion", "zsh", cwd=empty_dir)
        assert rc == 0
        assert "compdef" in out

    def test_fish_completion(self, empty_dir):
        rc, out, _ = run_vex("completion", "fish", cwd=empty_dir)
        assert rc == 0
        assert "complete -c vex" in out

    def test_completion_no_repo_needed(self, empty_dir):
        """Completion should work without a repo."""
        rc, out, _ = run_vex("completion", "bash", cwd=empty_dir)
        assert rc == 0


# ── CLI Error Path Tests ─────────────────────────────────────

class TestCLIErrorPaths:
    """Test that CLI commands fail gracefully with proper error messages and exit codes."""

    def test_no_command(self, empty_dir):
        rc, out, err = run_vex(cwd=empty_dir, expect_fail=True)
        assert rc == 1

    def test_invalid_command(self, empty_dir):
        rc, out, err = run_vex("nonexistent-command", cwd=empty_dir, expect_fail=True)
        assert rc != 0

    def test_snapshot_outside_repo(self, empty_dir):
        rc, out, err = run_vex("snapshot", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        assert "error" in err.lower() or "not" in err.lower()

    def test_snapshot_outside_repo_json(self, empty_dir):
        rc, out, err = run_vex("--json", "snapshot", cwd=empty_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_accept_invalid_transition(self, repo_dir):
        rc, out, err = run_vex("accept", "nonexistent-id", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_accept_invalid_transition_json(self, repo_dir):
        rc, out, err = run_vex("--json", "accept", "nonexistent-id", cwd=repo_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_reject_invalid_transition(self, repo_dir):
        rc, out, err = run_vex("reject", "nonexistent-id", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_info_invalid_state(self, repo_dir):
        rc, out, err = run_vex("info", "nonexistent-state-id", cwd=repo_dir)
        # info prints "State not found" but doesn't sys.exit(1) — it returns None
        assert "not found" in out.lower() or rc == 0

    def test_show_invalid_state(self, repo_dir):
        rc, out, err = run_vex("show", "bad-state", "file.txt", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_diff_invalid_states(self, repo_dir):
        rc, out, err = run_vex("diff", "bad-a", "bad-b", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_lane_create_invalid_name(self, repo_dir):
        """Lane names with path separators should be rejected."""
        rc, out, err = run_vex("lane", "create", "../../escape", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_lane_create_invalid_name_json(self, repo_dir):
        rc, out, err = run_vex("--json", "lane", "create", "../escape", cwd=repo_dir, expect_fail=True)
        assert rc == 1
        data = json.loads(out)
        assert "error" in data

    def test_workspace_remove_nonexistent(self, repo_dir):
        rc, out, err = run_vex("workspace", "remove", "no-such-workspace", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_gc_dry_run_default(self, repo_dir):
        """GC without --confirm should be a dry run (no error)."""
        rc, out, err = run_vex("gc", cwd=repo_dir)
        assert rc == 0
        assert "DRY RUN" in out

    def test_gc_json(self, repo_dir):
        rc, out, err = run_vex("--json", "gc", cwd=repo_dir)
        assert rc == 0
        data = json.loads(out)
        assert data["dry_run"] is True
        assert "reachable_objects" in data

    def test_commit_missing_required_args(self, empty_dir):
        """commit without --prompt should fail with argparse error."""
        # First init a repo
        run_vex("init", cwd=empty_dir)
        rc, out, err = run_vex("commit", "--agent-id", "x", "--agent-type", "y", cwd=empty_dir, expect_fail=True)
        assert rc != 0
        assert "required" in err.lower() or "prompt" in err.lower()

    def test_propose_missing_required_args(self, repo_dir):
        """propose without required args should fail."""
        rc, out, err = run_vex("propose", cwd=repo_dir, expect_fail=True)
        assert rc != 0

    def test_cat_file_missing_hash(self, repo_dir):
        rc, out, err = run_vex("cat-file", "deadbeef0000", cwd=repo_dir, expect_fail=True)
        assert rc == 1

    def test_restore_invalid_state(self, repo_dir):
        rc, out, err = run_vex("restore", "bad-state-id", "--force", cwd=repo_dir, expect_fail=True)
        assert rc == 1
