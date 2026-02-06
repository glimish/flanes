"""WorldStateManager unit tests."""

import uuid

import pytest

from fla.cas import ContentStore
from fla.state import (
    AgentIdentity,
    EvaluationResult,
    Intent,
    TransitionStatus,
    WorldStateManager,
)


def _make_intent(prompt="test intent"):
    return Intent(
        id=str(uuid.uuid4()),
        prompt=prompt,
        agent=AgentIdentity(agent_id="test-agent", agent_type="test"),
        tags=["test"],
    )


@pytest.fixture
def env(tmp_path):
    """Provides (store, wsm) tuple."""
    db = tmp_path / "test.db"
    store = ContentStore(db)
    wsm = WorldStateManager(store, db)
    yield store, wsm
    store.close()


class TestCreateStateFromTree:
    def test_round_trip(self, env):
        store, wsm = env
        tree_hash = store.store_tree({"a.txt": ("blob", store.store_blob(b"hello"))})
        state_id = wsm.create_state_from_tree(tree_hash, parent_id=None)
        state = wsm.get_state(state_id)
        assert state is not None
        assert state["id"] == state_id
        assert state["root_tree"] == tree_hash
        assert state["parent_id"] is None


class TestRecordAndGetIntent:
    def test_round_trip(self, env):
        _, wsm = env
        intent = _make_intent("do the thing")
        wsm.record_intent(intent)
        got = wsm.get_intent(intent.id)
        assert got is not None
        assert got.id == intent.id
        assert got.prompt == "do the thing"
        assert got.agent.agent_id == "test-agent"

    def test_get_intent_missing_returns_none(self, env):
        _, wsm = env
        assert wsm.get_intent("nonexistent-id") is None


class TestEvaluate:
    def _setup_proposed(self, env):
        store, wsm = env
        tree = store.store_tree({"f.txt": ("blob", store.store_blob(b"v1"))})
        s1 = wsm.create_state_from_tree(tree)
        tree2 = store.store_tree({"f.txt": ("blob", store.store_blob(b"v2"))})
        s2 = wsm.create_state_from_tree(tree2, parent_id=s1)
        intent = _make_intent()
        wsm.create_lane("main", base_state=s1)
        tid = wsm.propose(s1, s2, intent, lane="main")
        return s1, s2, tid

    def test_double_evaluate_raises(self, env):
        _, _, tid = self._setup_proposed(env)
        _, wsm = env
        wsm.evaluate(tid, EvaluationResult(passed=True, evaluator="e"))
        with pytest.raises(ValueError, match="not proposed"):
            wsm.evaluate(tid, EvaluationResult(passed=True, evaluator="e"))

    def test_rejection_does_not_advance_head(self, env):
        s1, s2, tid = self._setup_proposed(env)
        _, wsm = env
        head_before = wsm.get_lane_head("main")
        status = wsm.evaluate(tid, EvaluationResult(passed=False, evaluator="e"))
        assert status == TransitionStatus.REJECTED
        assert wsm.get_lane_head("main") == head_before


class TestHistory:
    def test_status_filter(self, env):
        store, wsm = env
        tree = store.store_tree({"f.txt": ("blob", store.store_blob(b"v1"))})
        s1 = wsm.create_state_from_tree(tree)
        tree2 = store.store_tree({"f.txt": ("blob", store.store_blob(b"v2"))})
        s2 = wsm.create_state_from_tree(tree2, parent_id=s1)
        tree3 = store.store_tree({"f.txt": ("blob", store.store_blob(b"v3"))})
        s3 = wsm.create_state_from_tree(tree3, parent_id=s1)

        wsm.create_lane("main", base_state=s1)

        # One accepted
        i1 = _make_intent("accepted one")
        tid1 = wsm.propose(s1, s2, i1, lane="main")
        wsm.evaluate(tid1, EvaluationResult(passed=True, evaluator="e"))

        # One rejected
        i2 = _make_intent("rejected one")
        tid2 = wsm.propose(s2, s3, i2, lane="main")
        wsm.evaluate(tid2, EvaluationResult(passed=False, evaluator="e"))

        accepted = wsm.history("main", status_filter=TransitionStatus.ACCEPTED)
        rejected = wsm.history("main", status_filter=TransitionStatus.REJECTED)
        assert len(accepted) == 1
        assert accepted[0]["status"] == "accepted"
        assert len(rejected) == 1
        assert rejected[0]["status"] == "rejected"


class TestMaterialize:
    def test_materialize_raises_on_missing_state(self, env, tmp_path):
        _, wsm = env
        with pytest.raises(ValueError, match="State not found"):
            wsm.materialize("nonexistent-state-id", tmp_path / "out")
