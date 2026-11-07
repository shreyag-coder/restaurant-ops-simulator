import json
import os
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm  # noqa: E402
import policies  # noqa: E402
from llm import DecisionError, build_payload, request_decision  # noqa: E402

KEY = "sk-or-test-SECRET-123"
MODEL = "test/model-1"
PERSONA = "You are Dana, a lead line cook."
VIEW = {
    "time": 3, "deadline": 30, "worker_id": "w_grill", "skills": {"grill": 1.0},
    "last_result": None,
    "ready_tasks": [{"task_id": "O2.b1.patty", "order_id": "O2", "step_id": "patty",
                     "required_skill": "grill", "station_id": "grill", "order_deadline": 25,
                     "my_minutes": 4, "best_minutes": 4, "station_has_room": True,
                     "ingredients_ok": True}],
    "orders": [], "other_workers": [], "stock": {},
}
GOOD = {"action": "start_task", "task_id": "O2.b1.patty", "why": "Earliest deadline."}


def completion(content=None, refusal=None):
    message = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return 200, json.dumps({"model": MODEL, "choices": [{"message": message}]})


def ok(obj=GOOD):
    return completion(json.dumps(obj))


class FakeTransport:
    """Replays scripted responses; an Exception instance is raised instead."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "payload": json.loads(body), "headers": headers,
                           "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    def __init__(self, step=0.05):
        self.t = 100.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    if "smoke" in request.node.name:
        return

    def guard(*a, **k):
        raise AssertionError("network access attempted in offline test")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


def decide(transport, **kw):
    kw.setdefault("model", MODEL)
    kw.setdefault("api_key", KEY)
    return request_decision(VIEW, PERSONA, transport=transport, clock=FakeClock(), **kw)


def assert_no_secret(err):
    blob = json.dumps(err.to_dict()) + str(err) + repr(err.attempts)
    assert KEY not in blob


# ---------------------------------------------------------------- payload

def test_payload_has_strict_schema_persona_and_view():
    p = build_payload(VIEW, PERSONA, MODEL)
    assert p["model"] == MODEL
    rf = p["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"action", "task_id", "why"}
    assert set(schema["properties"]["action"]["enum"]) == set(policies.ACTIONS)
    assert p["messages"][0]["role"] == "system"
    assert p["messages"][0]["content"].startswith(PERSONA)
    assert json.loads(p["messages"][1]["content"]) == VIEW
    assert p["provider"] == {"require_parameters": True}


def test_request_headers_and_url():
    t = FakeTransport(ok())
    decide(t, timeout=12.5)
    call = t.calls[0]
    assert call["url"] == llm.OPENROUTER_URL
    assert call["headers"]["Authorization"] == "Bearer " + KEY
    assert call["timeout"] == 12.5
    assert KEY not in json.dumps(call["payload"])


# ---------------------------------------------------------------- success paths

def test_valid_response_returns_envelope():
    env = decide(FakeTransport(ok()))
    assert env["proposal"] == GOOD
    assert env["mode"] == "ai" and env["model"] == MODEL and env["attempts"] == 1
    assert isinstance(env["latency_ms"], int) and env["latency_ms"] > 0
    assert set(env) == {"proposal", "mode", "model", "latency_ms", "attempts"}


def test_wait_response():
    env = decide(FakeTransport(ok({"action": "wait", "task_id": None, "why": "Nothing for me."})))
    assert env["proposal"]["action"] == "wait"


def test_invalid_json_then_valid_uses_retry():
    t = FakeTransport(completion("not json {"), ok())
    env = decide(t)
    assert env["attempts"] == 2 and len(t.calls) == 2


def test_server_error_then_valid():
    env = decide(FakeTransport((503, "upstream overloaded"), ok()))
    assert env["attempts"] == 2


def test_timeout_then_valid():
    env = decide(FakeTransport(socket.timeout("timed out"), ok()))
    assert env["attempts"] == 2


# ---------------------------------------------------------------- failures

def test_two_invalid_raises_decision_error_with_metadata():
    t = FakeTransport(completion("nope"), ok({"action": "start_task", "task_id": "O9.x",
                                              "why": "hallucinated"}))
    with pytest.raises(DecisionError) as exc:
        decide(t)
    err = exc.value
    assert err.model == MODEL
    assert [a["attempt"] for a in err.attempts] == [1, 2]
    assert [a["error_type"] for a in err.attempts] == ["invalid_json", "invalid_proposal"]
    assert all(isinstance(a["latency_ms"], int) for a in err.attempts)
    assert len(t.calls) == 2
    assert_no_secret(err)


def test_attempts_are_bounded_to_two():
    t = FakeTransport(socket.timeout("t1"), socket.timeout("t2"), ok())
    with pytest.raises(DecisionError) as exc:
        decide(t)
    assert len(t.calls) == 2 and len(t.responses) == 1
    assert [a["error_type"] for a in exc.value.attempts] == ["timeout", "timeout"]


def test_urlerror_timeout_and_network():
    t = FakeTransport(urllib.error.URLError(socket.timeout("slow")),
                      urllib.error.URLError("connection refused"))
    with pytest.raises(DecisionError) as exc:
        decide(t)
    assert [a["error_type"] for a in exc.value.attempts] == ["timeout", "network"]


def test_auth_error_is_not_retried():
    t = FakeTransport((401, '{"error": {"message": "bad key %s"}}' % KEY), ok())
    with pytest.raises(DecisionError) as exc:
        decide(t)
    assert len(t.calls) == 1
    assert exc.value.attempts[0]["http_status"] == 401
    assert "[redacted]" in exc.value.attempts[0]["detail"]
    assert_no_secret(exc.value)


def test_refusal():
    t = FakeTransport(completion(None, refusal="I can't help"), completion(None, refusal="no"))
    with pytest.raises(DecisionError) as exc:
        decide(t)
    assert {a["error_type"] for a in exc.value.attempts} == {"refusal"}


@pytest.mark.parametrize("content", [
    json.dumps({**GOOD, "station_id": "grill"}),           # extra property
    json.dumps({"action": "cook", "task_id": None, "why": "x"}),  # unknown action
    json.dumps({"action": "wait", "task_id": None, "why": ""}),   # empty why
    json.dumps({"action": "wait", "task_id": 3, "why": "x"}),     # wrong type
    "```json\n%s\n```" % json.dumps(GOOD),                   # fenced, not strict JSON
])
def test_invalid_proposals_are_rejected(content):
    with pytest.raises(DecisionError) as exc:
        decide(FakeTransport(completion(content), completion(content)))
    assert len(exc.value.attempts) == 2


def test_provider_error_body_and_missing_choices():
    t = FakeTransport((200, json.dumps({"error": {"message": "rate limited"}})),
                      (200, json.dumps({"choices": []})))
    with pytest.raises(DecisionError) as exc:
        decide(t)
    assert [a["error_type"] for a in exc.value.attempts] == ["provider_error", "bad_response"]


def test_details_are_truncated():
    t = FakeTransport((500, "x" * 5000), (500, "y" * 5000))
    with pytest.raises(DecisionError) as exc:
        decide(t)
    assert all(len(a["detail"]) <= llm.DETAIL_CHARS + 3 for a in exc.value.attempts)


def test_missing_config_raises_without_calling(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    t = FakeTransport(ok())
    with pytest.raises(DecisionError, match="OPENROUTER_MODEL"):
        request_decision(VIEW, PERSONA, transport=t)
    with pytest.raises(DecisionError, match="OPENROUTER_API_KEY"):
        request_decision(VIEW, PERSONA, model=MODEL, transport=t)
    assert t.calls == []


def test_env_config_is_used(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", "env/model")
    t = FakeTransport(ok())
    env = request_decision(VIEW, PERSONA, transport=t)
    assert env["model"] == "env/model"
    assert t.calls[0]["payload"]["model"] == "env/model"


def test_choose_ai_delegates_and_does_not_mutate_view():
    import copy
    before = copy.deepcopy(VIEW)
    env = policies.choose_ai(VIEW, PERSONA, model=MODEL, api_key=KEY, transport=FakeTransport(ok()))
    assert env["mode"] == "ai" and VIEW == before


def test_no_silent_fallback():
    t = FakeTransport((500, "a"), (500, "b"))
    with pytest.raises(DecisionError):
        policies.choose_ai(VIEW, PERSONA, model=MODEL, api_key=KEY, transport=t)


# ---------------------------------------------------------------- opt-in real provider

@pytest.mark.skipif(
    not (os.environ.get("RUN_OPENROUTER_SMOKE") == "1"
         and os.environ.get("OPENROUTER_API_KEY") and os.environ.get("OPENROUTER_MODEL")),
    reason="set RUN_OPENROUTER_SMOKE=1, OPENROUTER_API_KEY and OPENROUTER_MODEL",
)
def test_real_openrouter_smoke():
    persona = policies.load_persona(ROOT / "sim" / "personas", "w_grill")
    env = request_decision(VIEW, persona, timeout=60)
    print("\nSMOKE envelope:", json.dumps(env))
    assert env["mode"] == "ai" and env["attempts"] in (1, 2)
    assert env["proposal"]["action"] in policies.ACTIONS
