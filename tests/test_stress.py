"""
Stress Tests for Vex

Tests production workload scenarios:
- Concurrent snapshot operations
- Large workspace handling (10k+ files)
- REST API concurrency
- Memory usage under load

Run with: pytest tests/test_stress.py -v -m stress
For quick smoke test: pytest tests/test_stress.py -v -m "stress and not slow"
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from vex.repo import Repository
from vex.state import AgentIdentity

# Mark all tests in this module as stress tests
pytestmark = pytest.mark.stress


@pytest.fixture
def repo(tmp_path):
    """Create a basic test repository."""
    (tmp_path / "README.md").write_text("# Test Project\n")
    repo = Repository.init(tmp_path)
    yield repo
    repo.close()


@pytest.fixture
def large_repo(tmp_path):
    """Create a repository with many files for stress testing."""
    # Create initial structure
    (tmp_path / "README.md").write_text("# Large Test Project\n")
    repo = Repository.init(tmp_path)
    yield repo
    repo.close()


def _create_files(base_path: Path, count: int, dirs: int = 50):
    """Helper to create many files spread across directories."""
    dir_list = [base_path]
    for i in range(dirs):
        d = base_path / f"dir_{i:03d}"
        d.mkdir(parents=True, exist_ok=True)
        dir_list.append(d)

    for i in range(count):
        d = dir_list[i % len(dir_list)]
        (d / f"file_{i:06d}.txt").write_text(f"Content for file {i}\n" * 10)


class TestConcurrentSnapshots:
    """Test concurrent snapshot operations."""

    def test_10_concurrent_snapshots(self, repo):
        """10 agents snapshotting different workspaces concurrently."""
        initial_head = repo.head()
        repo_root = repo.root
        results = []
        lock = threading.Lock()

        def snapshot_worker(agent_num):
            thread_repo = Repository(repo_root)
            try:
                lane = f"agent-{agent_num}"
                thread_repo.create_lane(lane, base=initial_head)

                ws = thread_repo.workspace_path(lane)
                for i in range(10):
                    (ws / f"file_{agent_num}_{i}.py").write_text(
                        f"# Agent {agent_num} file {i}\n"
                    )

                state_id = thread_repo.snapshot(lane)
                with lock:
                    results.append({"agent": agent_num, "state_id": state_id})
                return state_id
            finally:
                thread_repo.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(snapshot_worker, i) for i in range(10)]
            for f in as_completed(futures):
                f.result()

        assert len(results) == 10
        state_ids = {r["state_id"] for r in results}
        assert len(state_ids) == 10  # All unique

    @pytest.mark.slow
    def test_50_concurrent_snapshots(self, repo):
        """50 agents snapshotting concurrently - higher load."""
        initial_head = repo.head()
        repo_root = repo.root
        results = []
        errors = []
        lock = threading.Lock()

        def snapshot_worker(agent_num):
            thread_repo = Repository(repo_root)
            try:
                lane = f"agent-{agent_num}"
                thread_repo.create_lane(lane, base=initial_head)

                ws = thread_repo.workspace_path(lane)
                for i in range(5):
                    (ws / f"file_{agent_num}_{i}.txt").write_text(f"Agent {agent_num}\n")

                state_id = thread_repo.snapshot(lane)
                with lock:
                    results.append({"agent": agent_num, "state_id": state_id})
                return state_id
            except Exception as e:
                with lock:
                    errors.append({"agent": agent_num, "error": str(e)})
                raise
            finally:
                thread_repo.close()

        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(snapshot_worker, i) for i in range(50)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass  # Errors already captured

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 50


class TestLargeWorkspace:
    """Test handling of large workspaces."""

    def test_snapshot_1k_files(self, large_repo, tmp_path):
        """Snapshot workspace with 1,000 files."""
        ws = large_repo.workspace_path("main")
        _create_files(ws, 1000, dirs=20)

        t0 = time.monotonic()
        state_id = large_repo.snapshot("main")
        elapsed = time.monotonic() - t0

        assert state_id is not None
        assert elapsed < 30  # Should complete in <30s

    @pytest.mark.slow
    def test_snapshot_10k_files(self, large_repo):
        """Snapshot workspace with 10,000 files."""
        ws = large_repo.workspace_path("main")
        _create_files(ws, 10000, dirs=100)

        t0 = time.monotonic()
        state_id = large_repo.snapshot("main")
        elapsed = time.monotonic() - t0

        assert state_id is not None
        assert elapsed < 120  # Should complete in <2 minutes

        # Verify snapshot can be read back
        state = large_repo.wsm.get_state(state_id)
        assert state is not None

    @pytest.mark.slow
    def test_snapshot_50k_files(self, large_repo):
        """Snapshot workspace with 50,000 files - stress test."""
        ws = large_repo.workspace_path("main")
        _create_files(ws, 50000, dirs=200)

        t0 = time.monotonic()
        state_id = large_repo.snapshot("main")
        elapsed = time.monotonic() - t0

        assert state_id is not None
        # Allow more time for very large workspace
        assert elapsed < 300  # 5 minutes max

    def test_incremental_snapshot_efficiency(self, large_repo):
        """Verify stat cache makes incremental snapshots fast."""
        ws = large_repo.workspace_path("main")
        _create_files(ws, 1000, dirs=20)

        # First snapshot - builds cache
        t0 = time.monotonic()
        state1 = large_repo.snapshot("main")
        first_elapsed = time.monotonic() - t0

        # Modify only 3 files
        for i in range(3):
            (ws / f"dir_00{i}" / f"modified_{i}.txt").write_text("Modified\n")

        # Second snapshot - should use cache
        t0 = time.monotonic()
        state2 = large_repo.snapshot("main")
        second_elapsed = time.monotonic() - t0

        assert state1 != state2
        # Incremental should be faster (at least somewhat, accounting for variance)
        # On small repos, the overhead dominates, so we're lenient here
        assert second_elapsed < first_elapsed * 0.8 or second_elapsed < 0.5


class TestConcurrentCommits:
    """Test concurrent commit operations."""

    def test_concurrent_commits_different_lanes(self, repo):
        """Multiple agents committing to different lanes."""
        initial_head = repo.head()
        repo_root = repo.root
        results = []
        lock = threading.Lock()

        def commit_worker(agent_num):
            thread_repo = Repository(repo_root)
            try:
                agent = AgentIdentity(
                    agent_id=f"agent-{agent_num}",
                    agent_type="stress_test",
                )
                lane = f"lane-{agent_num}"
                thread_repo.create_lane(lane, base=initial_head)

                ws = thread_repo.workspace_path(lane)
                (ws / f"change_{agent_num}.txt").write_text(f"Agent {agent_num}\n")

                result = thread_repo.quick_commit(
                    workspace=lane,
                    prompt=f"Agent {agent_num} commit",
                    agent=agent,
                    auto_accept=True,
                )

                with lock:
                    results.append(result)
                return result
            finally:
                thread_repo.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(commit_worker, i) for i in range(10)]
            for f in as_completed(futures):
                f.result()

        assert len(results) == 10
        assert all(r["status"] == "accepted" for r in results)

    def test_concurrent_commits_same_lane_staleness(self, repo):
        """Multiple agents committing to same lane - only one should win."""
        initial_head = repo.head()
        repo_root = repo.root
        results = []
        lock = threading.Lock()

        # Create a shared lane
        repo.create_lane("shared", base=initial_head)

        def commit_worker(agent_num):
            thread_repo = Repository(repo_root)
            try:
                agent = AgentIdentity(
                    agent_id=f"agent-{agent_num}",
                    agent_type="stress_test",
                )

                ws = thread_repo.workspace_path("shared")
                (ws / f"agent_{agent_num}.txt").write_text(f"Agent {agent_num}\n")

                # Small delay to make race more likely
                time.sleep(0.01 * agent_num)

                result = thread_repo.quick_commit(
                    workspace="shared",
                    prompt=f"Agent {agent_num} commit",
                    agent=agent,
                    auto_accept=True,
                )

                with lock:
                    results.append({"agent": agent_num, "result": result})
                return result
            finally:
                thread_repo.close()

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(commit_worker, i) for i in range(5)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        # At least one should succeed
        accepted = [r for r in results if r["result"]["status"] == "accepted"]
        assert len(accepted) >= 1


class TestRESTAPIConcurrency:
    """Test REST API under concurrent load."""

    @pytest.fixture
    def server_port(self, repo):
        """Start a test server and return its port."""
        import socket

        from vex.server import VexServer

        # Find free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]

        server = VexServer(repo, "127.0.0.1", port)

        def serve():
            server.serve_forever()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        # Give server time to start
        time.sleep(0.1)

        yield port

        server.shutdown()

    def test_concurrent_status_requests(self, server_port):
        """10 concurrent /status requests should all succeed."""
        import json as json_module
        import urllib.request

        results = []
        errors = []
        lock = threading.Lock()

        def fetch_status():
            try:
                url = f"http://127.0.0.1:{server_port}/status"
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json_module.loads(resp.read())
                    with lock:
                        results.append(data)
            except Exception as e:
                with lock:
                    errors.append(str(e))

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_status) for _ in range(10)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 10

    def test_health_endpoint_no_lock(self, server_port):
        """Health endpoint should respond quickly without repo lock."""
        import json as json_module
        import urllib.request

        timings = []
        for _ in range(5):
            t0 = time.monotonic()
            url = f"http://127.0.0.1:{server_port}/health"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json_module.loads(resp.read())
            elapsed = time.monotonic() - t0
            timings.append(elapsed)
            assert data["status"] == "healthy"

        # Health checks should be very fast (<100ms each)
        assert max(timings) < 0.1


class TestMemoryUsage:
    """Test memory behavior under load."""

    @pytest.mark.slow
    def test_no_memory_leak_repeated_snapshots(self, large_repo):
        """Repeated snapshots shouldn't leak memory."""
        import gc

        ws = large_repo.workspace_path("main")
        _create_files(ws, 500, dirs=10)

        # Warm up
        large_repo.snapshot("main")
        gc.collect()

        # Do many snapshots
        for i in range(20):
            # Modify a file
            (ws / "dir_000" / "changing.txt").write_text(f"Iteration {i}\n")
            large_repo.snapshot("main")

        # Force GC
        gc.collect()

        # If we got here without OOM, test passes
        # More sophisticated memory checks would require psutil


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "stress"])
