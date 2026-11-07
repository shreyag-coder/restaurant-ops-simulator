"""AI-mode rounds ask their workers in parallel without changing outcomes."""

import threading

from orchestrator import SimulationEngine, create_state
from tests.test_engine import example_policy, example_view, expand_example, tiny_config

MODEL = "test/model"


def ai(policy):
    def decide(view, worker_id):
        envelope = policy(view, worker_id)
        return dict(envelope, mode="ai", model=MODEL)
    return decide


def ai_engine(policy, config=None, state=None):
    state = state or create_state(config or tiny_config(order_count=3, event_at=5), "ai", run_id="r", model=MODEL)
    return SimulationEngine(state, expand_example, example_view, ai(policy))


def test_a_round_asks_its_workers_at_the_same_time():
    barrier = threading.Barrier(2, timeout=5)  # fails if the two calls are made one after another
    first = {"A": True, "B": True}

    def policy(view, worker_id):
        if view["time"] == 0 and first[worker_id]:
            first[worker_id] = False
            barrier.wait()
        return example_policy(view, worker_id)

    result = ai_engine(policy).run()
    assert result["status"] == "completed", result.get("error")


def test_parallel_and_sequential_runs_are_identical(monkeypatch):
    parallel = ai_engine(example_policy)
    parallel_state = parallel.run()
    monkeypatch.setattr(SimulationEngine, "_prefetch_decisions", lambda self, c: {})
    sequential = ai_engine(example_policy)
    assert sequential.run() == parallel_state
    assert sequential.pending_events == parallel.pending_events


def test_a_failed_call_keeps_the_other_answers_and_resume_does_not_ask_twice():
    calls = []
    fail = {"A": True}

    def policy(view, worker_id):
        calls.append((view["time"], worker_id))
        if worker_id == "A" and fail["A"]:
            fail["A"] = False
            raise TimeoutError("provider timeout")
        return example_policy(view, worker_id)

    paused = ai_engine(policy, tiny_config()).run()
    assert paused["status"] == "paused" and paused["pause_reason"] == "decision_error"
    assert set(paused["continuation"]["decisions"]) == {"B"}  # B answered in parallel; kept
    result = ai_engine(policy, state=paused).run()
    assert result["status"] == "completed"
    assert sorted(calls[:2]) == [(0, "A"), (0, "B")]  # the paused round asked both at once
    assert calls[2] == (0, "A")  # resume retries only A; B's saved answer is reused
