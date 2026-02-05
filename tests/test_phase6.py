"""
Tests for Phase 6: Interoperability.

Covers cat-file, git bridge (export/import), REST server, and MCP server.
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Check if git is available
try:
    subprocess.run(["git", "--version"], capture_output=True, check=True)
    HAS_GIT = True
except (FileNotFoundError, subprocess.CalledProcessError):
    HAS_GIT = False


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
def repo_dir(tmp_path):
    """A temporary directory with an initialized vex repo containing test files."""
    (tmp_path / "hello.txt").write_text("Hello, World!\n")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02binary content")
    rc, out, err = run_vex("init", cwd=tmp_path)
    assert rc == 0, f"Init failed: {err}"
    return tmp_path


@pytest.fixture
def repo_with_commit(repo_dir):
    """A repo with at least one commit."""
    rc, _, _ = run_vex(
        "commit", "-m", "initial commit",
        "--agent-id", "test", "--agent-type", "human",
        "--auto-accept", cwd=repo_dir,
    )
    assert rc == 0
    return repo_dir


@pytest.fixture
def repo(tmp_path):
    """A Repository object for direct API testing."""
    from vex.repo import Repository
    (tmp_path / "hello.txt").write_text("Hello, World!\n")
    repo = Repository.init(tmp_path)
    return repo


# ── cat-file tests ──────────────────────────────────────────────


class TestCatFileBlob:
    def test_cat_file_blob(self, repo_with_commit):
        # Get head state, find a blob hash
        rc, out, _ = run_vex("--json", "status", cwd=repo_with_commit)
        assert rc == 0
        status = json.loads(out)
        head = status["current_head"]

        rc, out, _ = run_vex("--json", "show", head, "hello.txt", cwd=repo_with_commit)
        assert rc == 0
        show_data = json.loads(out)
        blob_hash = show_data["blob_hash"]

        # Now cat-file the blob
        rc, out, _ = run_vex("--json", "cat-file", blob_hash, cwd=repo_with_commit)
        assert rc == 0
        data = json.loads(out)
        assert data["type"] == "blob"
        assert data["hash"] == blob_hash
        content = base64.b64decode(data["content_base64"])
        assert b"Hello, World!" in content


class TestCatFileTree:
    def test_cat_file_tree(self, repo_with_commit):
        rc, out, _ = run_vex("--json", "status", cwd=repo_with_commit)
        assert rc == 0
        head = json.loads(out)["current_head"]

        # Get root tree from state info
        rc, out, _ = run_vex("--json", "info", head, cwd=repo_with_commit)
        assert rc == 0
        info = json.loads(out)
        root_tree = info["root_tree"]

        rc, out, _ = run_vex("--json", "cat-file", root_tree, cwd=repo_with_commit)
        assert rc == 0
        data = json.loads(out)
        assert data["type"] == "tree"
        assert "entries" in data
        names = [e["name"] for e in data["entries"]]
        assert "hello.txt" in names


class TestCatFileState:
    def test_cat_file_state(self, repo_with_commit):
        rc, out, _ = run_vex("--json", "status", cwd=repo_with_commit)
        assert rc == 0
        head = json.loads(out)["current_head"]

        # cat-file on a state ID (world_states fallback)
        rc, out, _ = run_vex("--json", "cat-file", head, cwd=repo_with_commit)
        assert rc == 0
        data = json.loads(out)
        assert data["type"] == "state"
        assert "root_tree" in data


class TestCatFileNotFound:
    def test_cat_file_not_found(self, repo_with_commit):
        rc, out, err = run_vex(
            "cat-file", "deadbeef" * 8,
            cwd=repo_with_commit, expect_fail=True,
        )
        assert rc == 1


class TestCatFileTypeMismatch:
    def test_cat_file_type_mismatch(self, repo_with_commit):
        rc, out, _ = run_vex("--json", "status", cwd=repo_with_commit)
        head = json.loads(out)["current_head"]

        # Get a tree hash
        rc, out, _ = run_vex("--json", "info", head, cwd=repo_with_commit)
        root_tree = json.loads(out)["root_tree"]

        # Try to cat-file with --type blob on a tree → should fail
        rc, out, err = run_vex(
            "cat-file", root_tree, "--type", "blob",
            cwd=repo_with_commit, expect_fail=True,
        )
        assert rc == 1


class TestCatFileJson:
    def test_cat_file_json_blob(self, repo_with_commit):
        rc, out, _ = run_vex("--json", "status", cwd=repo_with_commit)
        head = json.loads(out)["current_head"]

        rc, out, _ = run_vex("--json", "show", head, "hello.txt", cwd=repo_with_commit)
        blob_hash = json.loads(out)["blob_hash"]

        rc, out, _ = run_vex("--json", "cat-file", blob_hash, cwd=repo_with_commit)
        assert rc == 0
        data = json.loads(out)
        assert "content_base64" in data
        assert data["type"] == "blob"


# ── Git export tests ────────────────────────────────────────────


@pytest.mark.skipif(not HAS_GIT, reason="git not found on PATH")
class TestGitExport:
    def test_export_creates_git_repo(self, repo_with_commit, tmp_path):
        target = tmp_path / "git-export"
        rc, out, err = run_vex(
            "export-git", str(target),
            cwd=repo_with_commit,
        )
        assert rc == 0, f"Export failed: {err}"
        assert (target / ".git").exists()

    def test_export_preserves_files(self, repo_with_commit, tmp_path):
        target = tmp_path / "git-export"
        run_vex("export-git", str(target), cwd=repo_with_commit)

        assert (target / "hello.txt").exists()
        assert (target / "hello.txt").read_text() == "Hello, World!\n"

    def test_export_preserves_messages(self, repo_with_commit, tmp_path):
        target = tmp_path / "git-export"
        run_vex("export-git", str(target), cwd=repo_with_commit)

        result = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=str(target), capture_output=True, text=True,
        )
        messages = result.stdout.strip().split("\n")
        # Should contain at least the initial commit message
        assert any("initial commit" in m.lower() or "initial snapshot" in m.lower()
                    for m in messages)

    def test_export_multiple_commits(self, repo_with_commit, tmp_path):
        # Make a second commit
        # Main workspace IS the repo root in git-style
        ws_path = repo_with_commit
        (ws_path / "second.txt").write_text("Second file\n")
        run_vex(
            "commit", "-m", "add second file",
            "--agent-id", "test", "--agent-type", "human",
            "--auto-accept", cwd=repo_with_commit,
        )

        target = tmp_path / "git-export"
        rc, _, err = run_vex("export-git", str(target), cwd=repo_with_commit)
        assert rc == 0, f"Export failed: {err}"

        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(target), capture_output=True, text=True,
        )
        commits = [line for line in result.stdout.strip().split("\n") if line]
        assert len(commits) >= 2


# ── Git import tests ────────────────────────────────────────────


@pytest.mark.skipif(not HAS_GIT, reason="git not found on PATH")
class TestGitImport:
    def _create_git_repo(self, path):
        """Create a git repo with some commits."""
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=str(path), capture_output=True, check=True)

        (path / "readme.txt").write_text("Hello from git\n")
        subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "first commit"],
                       cwd=str(path), capture_output=True, check=True)

        (path / "extra.txt").write_text("Extra file\n")
        subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add extra file"],
                       cwd=str(path), capture_output=True, check=True)

    def test_import_creates_states(self, repo_with_commit, tmp_path):
        git_repo = tmp_path / "git-source"
        self._create_git_repo(git_repo)

        rc, out, err = run_vex(
            "import-git", str(git_repo), "--lane", "imported",
            cwd=repo_with_commit,
        )
        assert rc == 0, f"Import failed: {err}"

        # Check that states exist on the imported lane
        rc, out, _ = run_vex("--json", "history", "--lane", "imported",
                             cwd=repo_with_commit)
        assert rc == 0
        history = json.loads(out)
        assert len(history) >= 2

    def test_import_preserves_files(self, repo_with_commit, tmp_path):
        git_repo = tmp_path / "git-source"
        self._create_git_repo(git_repo)

        run_vex("import-git", str(git_repo), "--lane", "imported",
                cwd=repo_with_commit)

        # Get head of imported lane
        rc, out, _ = run_vex("--json", "history", "--lane", "imported",
                             "--status", "accepted", cwd=repo_with_commit)
        history = json.loads(out)
        latest_state = history[0]["to_state"]

        # Check file content
        rc, out, _ = run_vex("--json", "show", latest_state, "readme.txt",
                             cwd=repo_with_commit)
        assert rc == 0
        data = json.loads(out)
        content = base64.b64decode(data["content_base64"])
        assert content == b"Hello from git\n"

    def test_import_auto_accepts(self, repo_with_commit, tmp_path):
        git_repo = tmp_path / "git-source"
        self._create_git_repo(git_repo)

        run_vex("import-git", str(git_repo), "--lane", "imported",
                cwd=repo_with_commit)

        rc, out, _ = run_vex("--json", "history", "--lane", "imported",
                             cwd=repo_with_commit)
        history = json.loads(out)
        for t in history:
            assert t["status"] == "accepted"


# ── Git roundtrip test ──────────────────────────────────────────


@pytest.mark.skipif(not HAS_GIT, reason="git not found on PATH")
class TestGitRoundtrip:
    def test_export_import_roundtrip(self, repo_with_commit, tmp_path):
        # Export to git
        git_dir = tmp_path / "git-export"
        rc, _, err = run_vex("export-git", str(git_dir), cwd=repo_with_commit)
        assert rc == 0, f"Export failed: {err}"

        # Create a fresh vex repo for import
        import_dir = tmp_path / "vex-import"
        import_dir.mkdir()
        rc, _, err = run_vex("init", cwd=import_dir)
        assert rc == 0

        # Import from the git export
        rc, _, err = run_vex(
            "import-git", str(git_dir), "--lane", "roundtrip",
            cwd=import_dir,
        )
        assert rc == 0, f"Import failed: {err}"

        # Verify file content survived the roundtrip
        rc, out, _ = run_vex("--json", "history", "--lane", "roundtrip",
                             "--status", "accepted", cwd=import_dir)
        history = json.loads(out)
        assert len(history) > 0

        latest = history[0]["to_state"]
        rc, out, _ = run_vex("--json", "show", latest, "hello.txt", cwd=import_dir)
        assert rc == 0
        data = json.loads(out)
        content = base64.b64decode(data["content_base64"])
        assert b"Hello, World!" in content


# ── REST server tests ───────────────────────────────────────────


class TestRESTServer:
    @pytest.fixture(autouse=True)
    def setup_server(self, tmp_path):
        """Start server in daemon thread with port=0 for OS-assigned port."""
        from vex.repo import Repository
        from vex.server import VexServer

        (tmp_path / "hello.txt").write_text("Hello, World!\n")
        repo = Repository.init(tmp_path)

        # Make a commit
        from vex.state import AgentIdentity
        repo.quick_commit(
            workspace="main",
            prompt="test commit",
            agent=AgentIdentity(agent_id="test", agent_type="human"),
            auto_accept=True,
        )
        repo.close()

        # Pass path so repo is opened in the server thread
        self.server = VexServer(str(tmp_path), host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Give server a moment to start
        time.sleep(0.1)

        yield

        self.server.shutdown()
        # Repo was opened in server thread; just let it go since
        # the server thread is a daemon and Python will clean up.

    def _get(self, path):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())

    def _post(self, path, data=None):
        url = f"{self.base_url}{path}"
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())

    def test_status_endpoint(self):
        data = self._get("/status")
        assert "current_head" in data
        assert "lanes" in data

    def test_lanes_endpoint(self):
        data = self._get("/lanes")
        assert isinstance(data, list)
        names = [lane["name"] for lane in data]
        assert "main" in names

    def test_history_endpoint(self):
        data = self._get("/history")
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_commit_endpoint(self):
        data = self._post("/commit", {
            "workspace": "main",
            "prompt": "api commit",
            "agent_id": "api-test",
            "agent_type": "test",
            "auto_accept": True,
        })
        assert "transition_id" in data

    def test_workspaces_endpoint(self):
        data = self._get("/workspaces")
        assert isinstance(data, list)
        names = [w["name"] for w in data]
        assert "main" in names

    def test_404_unknown_path(self):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._get("/nonexistent")
        assert exc_info.value.code == 404


# ── MCP server tests ────────────────────────────────────────────


class TestMCPServer:
    @pytest.fixture(autouse=True)
    def setup_mcp(self, tmp_path):
        from vex.mcp_server import MCPServer
        from vex.repo import Repository

        (tmp_path / "hello.txt").write_text("Hello, World!\n")
        self.repo = Repository.init(tmp_path)

        # Make a commit
        from vex.state import AgentIdentity
        self.repo.quick_commit(
            workspace="main",
            prompt="test commit",
            agent=AgentIdentity(agent_id="test", agent_type="human"),
            auto_accept=True,
        )

        self.mcp = MCPServer.__new__(MCPServer)
        self.mcp.repo = self.repo

        yield

        self.repo.close()

    def test_initialize(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        })
        assert resp["id"] == 1
        assert "protocolVersion" in resp["result"]
        assert "capabilities" in resp["result"]
        assert "serverInfo" in resp["result"]

    def test_tools_list(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        })
        tools = resp["result"]["tools"]
        assert len(tools) == 12
        names = {t["name"] for t in tools}
        assert "vex_status" in names
        assert "vex_commit" in names

    def test_tool_status(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "vex_status", "arguments": {}},
        })
        content = resp["result"]["content"]
        assert len(content) == 1
        data = json.loads(content[0]["text"])
        assert "current_head" in data

    def test_tool_commit(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "vex_commit",
                "arguments": {
                    "prompt": "mcp commit",
                    "agent_id": "mcp-test",
                    "agent_type": "test",
                },
            },
        })
        content = resp["result"]["content"]
        data = json.loads(content[0]["text"])
        assert "transition_id" in data

    def test_tool_lanes(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "vex_lanes", "arguments": {}},
        })
        content = resp["result"]["content"]
        data = json.loads(content[0]["text"])
        assert isinstance(data, list)
        names = [lane["name"] for lane in data]
        assert "main" in names

    def test_unknown_tool(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        })
        assert resp["result"].get("isError") is True

    def test_unknown_method(self):
        resp = self.mcp.handle_request({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "nonexistent/method",
            "params": {},
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32601
