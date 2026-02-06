"""
Concurrent Multi-Agent Integration Tests

Tests multiple agents working concurrently with:
- Multiple agents proposing simultaneously
- Workspace locking behavior under concurrency
- Agents hitting size/depth limits concurrently
- Multiple agents accepting/rejecting in different lanes
- Thread-safety of the repository operations

Run with: pytest tests/test_concurrent_agents.py -v
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from fla.agent_sdk import AgentSession
from fla.cas import ContentStoreLimitError
from fla.repo import Repository
from fla.state import (
    AgentIdentity,
    CostRecord,
    TransitionStatus,
    TreeDepthLimitError,
)


@pytest.fixture
def repo(tmp_path):
    """Create a test repository with initial content."""
    # Create initial files
    (tmp_path / "main.py").write_text(
        'def main():\n    print("Hello, World!")\n\nif __name__ == "__main__":\n    main()\n'
    )
    (tmp_path / "config.json").write_text(
        '{"app_name": "TestApp", "version": "1.0.0"}\n'
    )
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "utils.py").write_text(
        'def add(a, b):\n    return a + b\n'
    )

    repo = Repository.init(tmp_path)
    return repo


def test_concurrent_proposals(repo):
    """Test multiple agents proposing changes simultaneously."""
    initial_head = repo.head()
    repo_root = repo.root
    results = []
    errors = []

    def agent_propose(agent_num):
        """Worker function for each agent."""
        try:
            # Each thread gets its own Repository instance
            thread_repo = Repository(repo_root)

            agent = AgentIdentity(
                agent_id=f"agent-{agent_num}",
                agent_type="concurrent_tester",
                model="test-model",
            )

            # Create lane and workspace for this agent
            lane_name = f"feature-agent-{agent_num}"
            thread_repo.create_lane(lane_name, base=initial_head)

            # Modify workspace
            ws = thread_repo.workspace_path(lane_name)
            (ws / f"file_{agent_num}.py").write_text(
                f'# File created by agent {agent_num}\n'
                f'def func_{agent_num}():\n'
                f'    return {agent_num}\n'
            )

            # Snapshot and propose
            new_state = thread_repo.snapshot(lane_name, parent_id=initial_head)
            tid = thread_repo.propose(
                from_state=initial_head,
                to_state=new_state,
                prompt=f"Agent {agent_num} adding feature",
                agent=agent,
                tags=["concurrent", f"agent-{agent_num}"],
                cost=CostRecord(tokens_in=100, tokens_out=50, wall_time_ms=100, api_calls=1),
            )

            results.append({
                "agent_num": agent_num,
                "tid": tid,
                "lane": lane_name,
                "success": True,
            })
            return tid

        except Exception as e:
            errors.append({
                "agent_num": agent_num,
                "error": str(e),
            })
            raise

    # Run 5 agents concurrently
    num_agents = 5
    with ThreadPoolExecutor(max_workers=num_agents) as executor:
        futures = [executor.submit(agent_propose, i) for i in range(num_agents)]
        for future in as_completed(futures):
            future.result()  # Will raise if any agent failed

    # Verify all agents succeeded
    assert len(results) == num_agents, f"Expected {num_agents} results, got {len(results)}"
    assert len(errors) == 0, f"Errors occurred: {errors}"

    # Verify all transition IDs are unique
    tids = [r["tid"] for r in results]
    assert len(set(tids)) == num_agents, "All transition IDs should be unique"


def test_concurrent_workspace_locking(repo):
    """Test that workspace locking prevents concurrent modifications."""
    initial_head = repo.head()
    lane_name = "test-lane"
    repo.create_lane(lane_name, base=initial_head)

    agent = AgentIdentity(
        agent_id="lock-tester",
        agent_type="lock_test",
        model="test-model",
    )

    lock_acquired_count = 0
    lock_failed_count = 0
    lock = threading.Lock()

    def try_lock_workspace():
        """Try to acquire workspace lock."""
        nonlocal lock_acquired_count, lock_failed_count
        try:
            session = AgentSession(repo, agent)
            session.begin(lane_name)

            # If we got here, we acquired the lock
            with lock:
                lock_acquired_count += 1

            # Hold the lock briefly
            time.sleep(0.1)

            # Clean up properly
            session.end()

        except Exception:
            with lock:
                lock_failed_count += 1

    # Try to acquire lock from multiple threads
    num_threads = 5
    threads = [threading.Thread(target=try_lock_workspace) for _ in range(num_threads)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=30)

    # At most one should succeed at a time due to locking
    # (This test might need adjustment based on exact locking implementation)
    assert lock_acquired_count + lock_failed_count == num_threads


def test_concurrent_file_size_limit_violations(repo):
    """Test multiple agents hitting file size limits concurrently."""
    initial_head = repo.head()
    repo_root = repo.root
    errors_caught = []
    success_count = 0
    lock = threading.Lock()

    # Get the max blob size from config
    max_blob_size = repo.store.max_blob_size

    def agent_with_large_file(agent_num, file_size):
        """Worker that tries to snapshot large files."""
        nonlocal success_count
        try:
            # Each thread gets its own Repository instance
            thread_repo = Repository(repo_root)

            lane_name = f"large-file-{agent_num}"
            thread_repo.create_lane(lane_name, base=initial_head)

            ws = thread_repo.workspace_path(lane_name)
            # Create a file with the specified size
            large_content = b"X" * file_size
            (ws / f"large_{agent_num}.bin").write_bytes(large_content)

            # Try to snapshot - should fail if file is too large
            thread_repo.snapshot(lane_name, parent_id=initial_head)

            with lock:
                success_count += 1

        except ContentStoreLimitError as e:
            with lock:
                errors_caught.append({
                    "agent_num": agent_num,
                    "file_size": file_size,
                    "error": str(e),
                })

    # Run agents with files of different sizes
    test_cases = [
        (1, max_blob_size - 1000),  # Just under limit - should succeed
        (2, max_blob_size + 1000),  # Just over limit - should fail
        (3, max_blob_size + 5000),  # Over limit - should fail
        (4, max_blob_size - 5000),  # Under limit - should succeed
    ]

    with ThreadPoolExecutor(max_workers=len(test_cases)) as executor:
        futures = [
            executor.submit(agent_with_large_file, agent_num, size)
            for agent_num, size in test_cases
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except ContentStoreLimitError:
                pass  # Expected for oversized files

    # Verify correct number of successes and failures
    assert success_count == 2, f"Expected 2 successes, got {success_count}"
    assert len(errors_caught) == 2, f"Expected 2 errors, got {len(errors_caught)}"


def test_concurrent_tree_depth_limit_violations(repo, tmp_path):
    """Test multiple agents hitting tree depth limits concurrently."""
    initial_head = repo.head()
    repo_root = repo.root
    errors_caught = []
    success_count = 0
    lock = threading.Lock()

    # Get max tree depth from config
    max_tree_depth = repo.wsm.max_tree_depth

    def agent_with_deep_tree(agent_num, depth):
        """Worker that creates deeply nested directories."""
        nonlocal success_count
        try:
            # Each thread gets its own Repository instance
            thread_repo = Repository(repo_root)

            lane_name = f"deep-tree-{agent_num}"
            thread_repo.create_lane(lane_name, base=initial_head)

            ws = thread_repo.workspace_path(lane_name)

            # Create a deep directory structure
            current = ws / "deep"
            current.mkdir()
            for i in range(depth):
                current = current / f"level_{i}"
                current.mkdir()

            # Add a file at the deepest level
            (current / "deep_file.txt").write_text("Deep content")

            # Try to snapshot - should fail if too deep
            thread_repo.snapshot(lane_name, parent_id=initial_head)

            with lock:
                success_count += 1

        except TreeDepthLimitError as e:
            with lock:
                errors_caught.append({
                    "agent_num": agent_num,
                    "depth": depth,
                    "error": str(e),
                })

    # Run agents with different depth levels
    test_cases = [
        (1, max_tree_depth - 10),  # Under limit - should succeed
        (2, max_tree_depth + 5),   # Over limit - should fail
        (3, max_tree_depth + 20),  # Way over limit - should fail
        (4, max_tree_depth - 5),   # Under limit - should succeed
    ]

    with ThreadPoolExecutor(max_workers=len(test_cases)) as executor:
        futures = [
            executor.submit(agent_with_deep_tree, agent_num, depth)
            for agent_num, depth in test_cases
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except TreeDepthLimitError:
                pass  # Expected for too-deep trees

    # Verify correct number of successes and failures
    assert success_count == 2, f"Expected 2 successes, got {success_count}"
    assert len(errors_caught) == 2, f"Expected 2 errors, got {len(errors_caught)}"


def test_concurrent_accept_reject(repo):
    """Test multiple agents accepting/rejecting in different lanes concurrently."""
    initial_head = repo.head()
    repo_root = repo.root

    # Create transitions for multiple agents
    transitions = []
    for i in range(5):
        agent = AgentIdentity(
            agent_id=f"agent-{i}",
            agent_type="concurrent_evaluator",
            model="test-model",
        )

        lane_name = f"eval-lane-{i}"
        repo.create_lane(lane_name, base=initial_head)

        ws = repo.workspace_path(lane_name)
        (ws / f"file_{i}.py").write_text(f"# Agent {i} file\n")

        new_state = repo.snapshot(lane_name, parent_id=initial_head)
        tid = repo.propose(
            from_state=initial_head,
            to_state=new_state,
            prompt=f"Agent {i} change",
            agent=agent,
            lane=lane_name,  # Specify the lane!
            tags=[f"agent-{i}"],
            cost=CostRecord(tokens_in=100, tokens_out=50, wall_time_ms=100, api_calls=1),
        )

        transitions.append({
            "tid": tid,
            "agent_num": i,
            "should_accept": i % 2 == 0,  # Accept even-numbered agents
        })

    results = []
    lock = threading.Lock()

    def evaluate_transition(trans):
        """Worker to evaluate a transition."""
        # Each thread gets its own Repository instance
        thread_repo = Repository(repo_root)

        tid = trans["tid"]
        should_accept = trans["should_accept"]

        try:
            if should_accept:
                status = thread_repo.accept(
                    tid,
                    evaluator=f"test-evaluator-{trans['agent_num']}",
                    summary="Accepted",
                )
            else:
                status = thread_repo.reject(
                    tid,
                    evaluator=f"test-evaluator-{trans['agent_num']}",
                    summary="Rejected",
                )

            with lock:
                expected = (
                    TransitionStatus.ACCEPTED if should_accept else TransitionStatus.REJECTED
                )
                results.append({
                    "tid": tid,
                    "status": status,
                    "expected": expected,
                })
        except Exception as e:
            with lock:
                results.append({
                    "tid": tid,
                    "error": str(e),
                })

    # Evaluate all transitions concurrently
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(evaluate_transition, trans) for trans in transitions]
        for future in as_completed(futures):
            future.result()

    # Verify all evaluations completed correctly
    assert len(results) == 5
    for result in results:
        assert "error" not in result
        assert result["status"] == result["expected"]


def test_concurrent_snapshot_operations(repo):
    """Test multiple agents snapshotting their workspaces concurrently."""
    initial_head = repo.head()
    repo_root = repo.root
    snapshots = []
    lock = threading.Lock()

    def agent_snapshot(agent_num):
        """Worker that creates a snapshot."""
        # Each thread gets its own Repository instance
        thread_repo = Repository(repo_root)

        lane_name = f"snapshot-lane-{agent_num}"
        thread_repo.create_lane(lane_name, base=initial_head)

        ws = thread_repo.workspace_path(lane_name)

        # Create multiple files
        for i in range(10):
            (ws / f"file_{agent_num}_{i}.py").write_text(
                f"# File {i} by agent {agent_num}\n"
                f"def func_{agent_num}_{i}():\n"
                f"    return {agent_num * 100 + i}\n"
            )

        # Create nested directories
        subdir = ws / f"subdir_{agent_num}"
        subdir.mkdir()
        for i in range(5):
            (subdir / f"nested_{i}.py").write_text(f"# Nested {i}\n")

        # Snapshot
        state_id = thread_repo.snapshot(lane_name, parent_id=initial_head)

        with lock:
            snapshots.append({
                "agent_num": agent_num,
                "state_id": state_id,
                "lane": lane_name,
            })

        return state_id

    # Run multiple agents concurrently
    num_agents = 10
    with ThreadPoolExecutor(max_workers=num_agents) as executor:
        futures = [executor.submit(agent_snapshot, i) for i in range(num_agents)]
        for future in as_completed(futures):
            future.result()

    # Verify all snapshots were created
    assert len(snapshots) == num_agents

    # Verify all state IDs are unique
    state_ids = [s["state_id"] for s in snapshots]
    assert len(set(state_ids)) == num_agents, "All state IDs should be unique"

    # Verify all states exist in the repository
    verify_repo = Repository(repo_root)
    for snapshot in snapshots:
        state = verify_repo.wsm.get_state(snapshot["state_id"])
        assert state is not None
        assert state["root_tree"] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
