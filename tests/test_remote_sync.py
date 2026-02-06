"""
Tests for remote storage push/pull with type-preservation,
and the stale-accept lane conflict detection.
"""

import json

import pytest


@pytest.fixture
def repo_pair(tmp_path):
    """Two Fla repos sharing a remote backend — simulates push from one, pull into another."""
    from fla.remote import InMemoryBackend, RemoteSyncManager
    from fla.repo import Repository
    from fla.state import AgentIdentity

    # Repo A: has content
    dir_a = tmp_path / "repo_a"
    dir_a.mkdir()
    (dir_a / "main.py").write_text("print('a')\n")
    repo_a = Repository.init(dir_a)

    # Make a second commit to have more history
    ws = repo_a.workspace_path("main")
    (ws / "lib.py").write_text("def f(): pass\n")
    agent = AgentIdentity(agent_id="agent-a", agent_type="test")
    repo_a.quick_commit(workspace="main", prompt="Add lib", agent=agent, auto_accept=True)

    # Repo B: empty
    dir_b = tmp_path / "repo_b"
    dir_b.mkdir()
    repo_b = Repository.init(dir_b)

    # Shared backend
    backend = InMemoryBackend()

    sync_a = RemoteSyncManager(repo_a.store, backend, tmp_path / "cache_a")
    sync_b = RemoteSyncManager(repo_b.store, backend, tmp_path / "cache_b")

    yield repo_a, repo_b, sync_a, sync_b, backend

    repo_a.close()
    repo_b.close()


class TestRemotePushPull:

    def test_push_then_pull_round_trip(self, repo_pair):
        """Objects pushed from repo A can be pulled into repo B with correct types."""
        repo_a, repo_b, sync_a, sync_b, backend = repo_pair

        # Push everything from A
        push_result = sync_a.push()
        assert push_result["pushed"] > 0

        # Pull everything into B
        pull_result = sync_b.pull()
        assert pull_result["pulled"] > 0
        assert pull_result["errors"] == 0

        # All objects from A should now exist in B
        from fla.cas import ObjectType
        rows_a = repo_a.store.conn.execute("SELECT hash, type FROM objects").fetchall()
        for h, t in rows_a:
            obj_b = repo_b.store.retrieve(h)
            assert obj_b is not None, f"Missing object {h} (type={t}) after pull"
            assert obj_b.type == ObjectType(t), (
                f"Type mismatch for {h}: expected {t}, "
                f"got {obj_b.type.value}"
            )

    def test_push_preserves_all_object_types(self, repo_pair):
        """Push uploads blobs, trees, and states with type prefixes."""
        repo_a, _, sync_a, _, backend = repo_pair

        sync_a.push()

        # Verify backend has type-prefixed payloads
        for key in backend.list_keys():
            payload = backend.download(key)
            assert payload is not None
            newline_idx = payload.find(b"\n")
            assert newline_idx > 0, f"Object {key} missing type prefix"
            type_str = payload[:newline_idx].decode("utf-8")
            assert type_str in ("blob", "tree", "state"), f"Unexpected type: {type_str}"

    def test_pull_idempotent(self, repo_pair):
        """Pulling twice doesn't duplicate objects."""
        _, repo_b, sync_a, sync_b, _ = repo_pair

        sync_a.push()
        result1 = sync_b.pull()
        result2 = sync_b.pull()

        assert result1["pulled"] > 0
        assert result2["pulled"] == 0
        assert result2["skipped"] == result1["pulled"] + result1["skipped"]

    def test_push_idempotent(self, repo_pair):
        """Pushing twice doesn't re-upload."""
        _, _, sync_a, _, _ = repo_pair

        r1 = sync_a.push()
        r2 = sync_a.push()

        assert r1["pushed"] > 0
        assert r2["pushed"] == 0
        assert r2["skipped"] == r1["total"]

    def test_status_reflects_sync_state(self, repo_pair):
        """Status correctly reports local-only, remote-only, synced."""
        _, _, sync_a, sync_b, _ = repo_pair

        # Before push: all local-only
        status = sync_a.status()
        assert len(status["local_only"]) > 0
        assert len(status["synced"]) == 0

        # After push: all synced
        sync_a.push()
        status = sync_a.status()
        assert len(status["local_only"]) == 0
        assert len(status["synced"]) > 0

        # From B's perspective: all remote-only until pulled
        status_b = sync_b.status()
        assert len(status_b["remote_only"]) > 0


class TestStaleAccept:

    @pytest.fixture
    def repo_with_two_proposals(self, tmp_path):
        """A repo with two proposed transitions from the same from_state."""
        from fla.repo import Repository
        from fla.state import AgentIdentity

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("original\n")
        repo = Repository.init(project_dir)

        ws_path = repo.workspace_path("main")
        head = repo.head("main")
        agent = AgentIdentity(agent_id="agent1", agent_type="test")

        # Proposal 1: agent changes file.txt
        (ws_path / "file.txt").write_text("change from agent 1\n")
        state1 = repo.snapshot("main", parent_id=head)
        tid1 = repo.propose(
            from_state=head, to_state=state1,
            prompt="Agent 1 change", agent=agent, lane="main",
        )

        # Proposal 2: different agent changes file.txt differently
        (ws_path / "file.txt").write_text("change from agent 2\n")
        state2 = repo.snapshot("main", parent_id=head)
        agent2 = AgentIdentity(agent_id="agent2", agent_type="test")
        tid2 = repo.propose(
            from_state=head, to_state=state2,
            prompt="Agent 2 change", agent=agent2, lane="main",
        )

        yield repo, tid1, tid2, head

        repo.close()

    def test_second_accept_is_rejected_as_stale(self, repo_with_two_proposals):
        """When two transitions share from_state, accepting first makes second stale."""
        repo, tid1, tid2, original_head = repo_with_two_proposals
        from fla.state import TransitionStatus

        # Accept first
        status1 = repo.accept(tid1, evaluator="test", summary="Accept first")
        assert status1 == TransitionStatus.ACCEPTED

        # Head should have moved
        new_head = repo.head("main")
        assert new_head != original_head

        # Accept second — should be rejected as stale
        status2 = repo.accept(tid2, evaluator="test", summary="Accept second")
        assert status2 == TransitionStatus.REJECTED

        # Head should not have moved again
        assert repo.head("main") == new_head

    def test_stale_reject_includes_explanation(self, repo_with_two_proposals):
        """Stale rejection includes a useful message about the conflict."""
        repo, tid1, tid2, _ = repo_with_two_proposals

        repo.accept(tid1)

        # Accept second — rejected
        repo.accept(tid2)

        # Check the evaluation summary
        row = repo.wsm.conn.execute(
            "SELECT evaluation_json FROM transitions WHERE id = ?",
            (tid2,)
        ).fetchone()
        evaluation = json.loads(row[0])
        assert "Stale" in evaluation["summary"]
        assert "Re-propose" in evaluation["summary"]


class TestMetadataSync:
    """Tests for metadata push/pull functionality."""

    def test_push_pull_metadata_round_trip(self, repo_pair):
        """Push metadata from repo A, pull into repo B, verify it arrives."""
        repo_a, repo_b, sync_a, sync_b, _ = repo_pair

        # Push CAS objects + metadata from repo A
        sync_a.push()
        meta_result = sync_a.push_metadata(repo_a.wsm)
        assert meta_result["pushed_lanes"] >= 1

        # Pull CAS objects + metadata into repo B
        sync_b.pull()
        pull_result = sync_b.pull_metadata(repo_b.wsm)
        assert pull_result["lanes_pulled"] >= 1
        assert pull_result["transitions_imported"] >= 1
        assert pull_result["intents_imported"] >= 1

        # Verify repo B now has the transitions
        history_b = repo_b.wsm.history("main", limit=10)
        assert len(history_b) >= 1

    def test_metadata_pull_detects_conflict(self, repo_pair):
        """Divergent same-lane work produces a conflict report."""
        from fla.state import AgentIdentity

        repo_a, repo_b, sync_a, sync_b, _ = repo_pair

        # Sync everything first so both repos have same base
        sync_a.push()
        sync_a.push_metadata(repo_a.wsm)
        sync_b.pull()
        sync_b.pull_metadata(repo_b.wsm)

        # Now do divergent work on the same lane
        # Repo A: make a commit
        ws_a = repo_a.workspace_path("main")
        (ws_a / "file_a.py").write_text("from_a\n")
        agent_a = AgentIdentity(agent_id="agent-a", agent_type="test")
        repo_a.quick_commit("main", "commit from A", agent_a, auto_accept=True)
        sync_a.push()
        sync_a.push_metadata(repo_a.wsm)

        # Repo B: make a different commit on the same lane
        ws_b = repo_b.workspace_path("main")
        (ws_b / "file_b.py").write_text("from_b\n")
        agent_b = AgentIdentity(agent_id="agent-b", agent_type="test")
        repo_b.quick_commit("main", "commit from B", agent_b, auto_accept=True)

        # Pull metadata — should detect conflict
        sync_b.pull()
        result = sync_b.pull_metadata(repo_b.wsm)
        assert len(result["conflicts"]) >= 1
        assert result["conflicts"][0]["lane"] == "main"

    def test_metadata_pull_no_conflict_different_lanes(self, repo_pair):
        """Metadata from a new lane merges cleanly (no conflict on that lane)."""
        from fla.state import AgentIdentity

        repo_a, repo_b, sync_a, sync_b, _ = repo_pair

        # Sync initial state
        sync_a.push()
        sync_a.push_metadata(repo_a.wsm)
        sync_b.pull()
        sync_b.pull_metadata(repo_b.wsm)

        # Repo A: work on a new lane (create_lane also creates workspace)
        base = repo_a.head("main")
        repo_a.create_lane("feature-a", base)
        ws_a = repo_a.workspace_path("feature-a")
        (ws_a / "feature_a.py").write_text("feature_a\n")
        agent_a = AgentIdentity(agent_id="agent-a", agent_type="test")
        repo_a.quick_commit("feature-a", "feature A", agent_a, auto_accept=True)
        sync_a.push()
        sync_a.push_metadata(repo_a.wsm)

        # Pull into repo B — feature-a lane should merge cleanly (no conflict on it)
        sync_b.pull()
        result = sync_b.pull_metadata(repo_b.wsm)
        assert result["lanes_pulled"] >= 1
        # Only check for conflicts on the feature-a lane specifically
        feature_conflicts = [c for c in result["conflicts"] if c["lane"] == "feature-a"]
        assert len(feature_conflicts) == 0

        # Repo B should now know about feature-a lane
        lanes_b = {row["name"] for row in repo_b.wsm.list_lanes()}
        assert "feature-a" in lanes_b

    def test_metadata_idempotent(self, repo_pair):
        """Pushing and pulling metadata twice doesn't duplicate records."""
        repo_a, repo_b, sync_a, sync_b, _ = repo_pair

        sync_a.push()
        sync_a.push_metadata(repo_a.wsm)
        sync_b.pull()
        _ = sync_b.pull_metadata(repo_b.wsm)

        # Pull again — should import nothing new
        result2 = sync_b.pull_metadata(repo_b.wsm)
        assert result2["transitions_imported"] == 0
        assert result2["intents_imported"] == 0
