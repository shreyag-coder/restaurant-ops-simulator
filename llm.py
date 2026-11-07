"""OpenRouter decision calls with a strict JSON schema.

Standard library only (urllib). One provider retry at most (two attempts in
total). On exhaustion, raise DecisionError with sanitized per-attempt
metadata; there is never a silent fallback to the deterministic policy.
This module performs no file writes and never logs request headers or keys.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_ATTEMPTS = 2
DETAIL_CHARS = 200
NON_RETRYABLE_STATUS = (400, 401, 402, 403, 404)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start_task", "wait", "report_blocker", "request_help"],
        },
        "task_id": {"type": ["string", "null"]},
        "why": {"type": "string"},
    },
    "required": ["action", "task_id", "why"],
    "additionalProperties": False,
}

SYSTEM_RULES = """\
You are one worker in a simulated restaurant kitchen. The user message is a
JSON view of the kitchen from your point of view.

Rules:
- Your "skills" in the view are authoritative. You cannot do anything else and
  must not claim any other qualification, whatever your background suggests.
- You may only reference task IDs listed in "ready_tasks".
- Actions: "start_task" (task_id required), "request_help" (task_id required),
  "report_blocker" (task_id required), or "wait" (task_id must be null).
- Consider order deadlines, my_minutes vs best_minutes, station room,
  ingredient availability, and what other workers are doing.
- "why" is one short sentence explaining the choice.
- Respond with only a JSON object: {"action": ..., "task_id": ..., "why": ...}.
"""


class DecisionError(Exception):
    """Raised when no valid AI proposal could be obtained.

    Attributes:
        model: configured model identity (may be None if unconfigured).
        attempts: list of sanitized dicts
            {attempt, error_type, http_status, latency_ms, detail}.
    """

    def __init__(self, message, model=None, attempts=None):
        super().__init__(message)
        self.model = model
        self.attempts = list(attempts or [])

    def to_dict(self):
        return {"message": str(self), "model": self.model, "attempts": list(self.attempts)}


class _AttemptFailure(Exception):
    def __init__(self, error_type, detail, http_status=None, retryable=True):
        super().__init__(detail)
        self.error_type = error_type
        self.detail = detail
        self.http_status = http_status
        self.retryable = retryable


def _sanitize(text, api_key):
    text = str(text)
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = " ".join(text.split())
    if len(text) > DETAIL_CHARS:
        text = text[:DETAIL_CHARS] + "..."
    return text


# --------------------------------------------------------------------------
# Request building and transport
# --------------------------------------------------------------------------

def build_payload(view, persona, model):
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "%s\n\n%s" % (persona.strip(), SYSTEM_RULES)},
            {"role": "user", "content": json.dumps(view, sort_keys=True)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "worker_decision", "strict": True, "schema": RESPONSE_SCHEMA},
        },
        "provider": {"require_parameters": True},
        "temperature": 0,
        "max_tokens": 300,
    }


def urllib_transport(url, body, headers, timeout):
    """POST body; return (status, text). Network errors propagate."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        try:
            text = err.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return err.code, text


def _post(payload, api_key, timeout, transport):
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "X-Title": "restaurant-ops-simulator",
    }
    body = json.dumps(payload).encode("utf-8")
    try:
        return transport(OPENROUTER_URL, body, headers, timeout)
    except (socket.timeout, TimeoutError) as err:
        raise _AttemptFailure("timeout", "request timed out: %s" % err)
    except urllib.error.URLError as err:
        if isinstance(err.reason, (socket.timeout, TimeoutError)):
            raise _AttemptFailure("timeout", "request timed out: %s" % err.reason)
        raise _AttemptFailure("network", "network error: %s" % err.reason)
    except OSError as err:
        raise _AttemptFailure("network", "network error: %s" % err)


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

def parse_completion(status, text, view):
    """Turn an HTTP response into a validated proposal or raise _AttemptFailure."""
    from policies import validate_proposal  # lazy: policies imports this module

    if status != 200:
        retryable = status not in NON_RETRYABLE_STATUS
        raise _AttemptFailure("http_error", "HTTP %s: %s" % (status, text),
                              http_status=status, retryable=retryable)
    try:
        body = json.loads(text)
    except ValueError:
        raise _AttemptFailure("bad_response", "response body is not JSON: %s" % text)

    if isinstance(body, dict) and body.get("error"):
        raise _AttemptFailure("provider_error", "provider error: %s" % json.dumps(body["error"]))
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise _AttemptFailure("bad_response", "missing choices[0].message")

    if message.get("refusal"):
        raise _AttemptFailure("refusal", "model refused: %s" % message["refusal"])
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _AttemptFailure("bad_response", "empty message content")
    try:
        obj = json.loads(content)
    except ValueError:
        raise _AttemptFailure("invalid_json", "content is not JSON: %s" % content)
    try:
        return validate_proposal(obj, view)
    except ValueError as err:
        raise _AttemptFailure("invalid_proposal", "%s; content: %s" % (err, content))


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def request_decision(view, persona, *, model=None, api_key=None, timeout=30.0,
                     transport=None, clock=time.monotonic):
    """Return an AI decision envelope or raise DecisionError.

    model/api_key default to OPENROUTER_MODEL / OPENROUTER_API_KEY.
    latency_ms is total wall time across attempts.
    """
    model = model or os.environ.get("OPENROUTER_MODEL")
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not model:
        raise DecisionError("OPENROUTER_MODEL is not set", model=None)
    if not api_key:
        raise DecisionError("OPENROUTER_API_KEY is not set", model=model)
    transport = transport or urllib_transport

    payload = build_payload(view, persona, model)
    attempts = []
    started = clock()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempt_start = clock()
        try:
            status, text = _post(payload, api_key, timeout, transport)
            proposal = parse_completion(status, text, view)
        except _AttemptFailure as failure:
            attempts.append({
                "attempt": attempt,
                "error_type": failure.error_type,
                "http_status": failure.http_status,
                "latency_ms": int(round((clock() - attempt_start) * 1000)),
                "detail": _sanitize(failure.detail, api_key),
            })
            if not failure.retryable:
                break
            continue
        return {
            "proposal": proposal,
            "mode": "ai",
            "model": model,
            "latency_ms": int(round((clock() - started) * 1000)),
            "attempts": attempt,
        }

    last = attempts[-1]
    raise DecisionError(
        "AI decision failed after %d attempt(s): %s" % (len(attempts), last["error_type"]),
        model=model,
        attempts=attempts,
    )
