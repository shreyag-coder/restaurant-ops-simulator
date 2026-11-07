"""Tests for replay.py: read-only, offline, exact-tick (Agent 4)."""

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import recording  # noqa: E402
from recording import save_checkpoint  # noqa: E402
from replay import (ReplayError, latest_run, list_runs, replay_run,  # noqa: E402
                    replay_state, resolve_run)

RUN_ID = "r-replay"


def make_state(tick, last_seq, run_id=RUN_ID):
    return {"schema_version": 1, "run_id": run_id, "state_version": tick,
            "status": "running", "mode": "det", "model": None,
            "target": {"text": "10 orders in 30 minutes", "deadline": 30},
            "clock": {"tick": tick, "continuation": {"phase": "tick_start"}},
            "orders": {"O1": {"id": "O1", "status": "in_progress", "deadline": 12,
                              "items": [{"item_id": "fries", "qty": 1}]}},
            "tasks": {"O1.f1.fry": {"id": "O1.f1.fry", "order_id": "O1",
                                    "station_id": "fryer", "status":
                                    "done" if tick >= 3 else "in_progress"}},
            "workers": {}, "stations": {}, "ingredients": {
                "potato": {"available": 10 - min(tick, 3), "reserved": 0}},
            "last_event_seq": last_seq}


def record_run(run_dir, ticks=(0, 1, 2, 3, 4), run_id=RUN_ID):
    """One event per tick; returns the run dir."""
    for seq, tick in enumerate(ticks, start=1):
        event = {"seq": seq, "run_id": run_id, "tick": tick, "type": "tick_marker",
                 "payload": {"message": "tick %d" % tick}}
        save_checkpoint(run_dir, make_state(tick, seq, run_id), [event])
    return run_dir


def hashes(folder):
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                         p.stat().st_mtime_ns)
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


@pytest.fixture
def run_dir(tmp_path):
    return record_run(tmp_path / "runs" / RUN_ID)


@pytest.fixture
def offline(monkeypatch):
    """Forbid any network use during replay."""
    def no_network(*args, **kwargs):
        raise AssertionError("replay attempted network access")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_exact_tick_selection(run_dir, offline):
    state, events = replay_state(run_dir, at=2)
    assert state["clock"]["tick"] == 2 and state["last_event_seq"] == 3
    assert [e["tick"] for e in events] == [0, 1, 2]
    text = replay_run(run_dir, at=2)
    assert "REPLAY of run r-replay at tick 2" in text
    assert "minute 2 of 30" in text
    assert "tick 2" in text and "tick 3" not in text


def test_default_is_latest_committed(run_dir, offline):
    state, events = replay_state(run_dir)
    assert state["clock"]["tick"] == 4 and len(events) == 5
    assert "at tick 4" in replay_run(run_dir)


def test_replay_is_read_only(run_dir, offline):
    before = hashes(run_dir)
    for at in (None, 0, 3, 4):
        replay_run(run_dir, at=at)
    with pytest.raises(ReplayError):
        replay_run(run_dir, at=99)
    assert hashes(run_dir) == before


def test_replay_does_not_load_engine_policies_or_model():
    code = ("import sys, replay; "
            "bad = [m for m in ('policies', 'llm', 'orchestrator', 'urllib.request') "
            "if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "[]"


def test_tick_errors_are_specific(tmp_path, offline):
    run = record_run(tmp_path / RUN_ID, ticks=(0, 1, 2, 5, 6))
    with pytest.raises(ReplayError, match=r"beyond the last committed tick 6.*0-2, 5-6"):
        replay_run(run, at=7)
    with pytest.raises(ReplayError, match=r"tick 3 was not recorded.*0-2, 5-6"):
        replay_run(run, at=3)
    with pytest.raises(ReplayError, match="non-negative integer"):
        replay_run(run, at=-1)


def test_missing_run_errors(tmp_path, offline):
    with pytest.raises(ReplayError, match="no run found"):
        replay_run(tmp_path / "ghost")
    with pytest.raises(ReplayError, match="no recorded runs"):
        latest_run(tmp_path)
    record_run(tmp_path / "a", run_id="a")
    with pytest.raises(ReplayError, match=r"no run 'zz'.*available: a"):
        resolve_run(tmp_path, "zz")


def test_incomplete_run_is_reported(run_dir, offline):
    (run_dir / "events.jsonl").write_text("")
    with pytest.raises(ReplayError, match="cannot replay r-replay"):
        replay_run(run_dir)


def test_uncommitted_events_and_snapshots_are_not_replayed(run_dir, offline):
    # Crash during tick 5's save: event appended and snapshot written, no commit.
    with open(run_dir / "events.jsonl", "a") as handle:
        handle.write(json.dumps({"seq": 6, "run_id": RUN_ID, "tick": 5,
                                 "type": "tick_marker", "payload": {"message": "ghost"}}) + "\n")
    recording.atomic_write_json(recording.snapshot_path(run_dir, 5), make_state(5, 6))
    text = replay_run(run_dir)
    assert "at tick 4" in text and "ghost" not in text
    with pytest.raises(ReplayError, match="beyond the last committed tick 4"):
        replay_run(run_dir, at=5)


def test_current_tick_uses_committed_state_not_newer_snapshot(run_dir, offline):
    # Crash mid same-tick save: t004 snapshot is ahead of state.json.
    recording.atomic_write_json(recording.snapshot_path(run_dir, 4), make_state(4, 9))
    state, events = replay_state(run_dir, at=4)
    assert state["last_event_seq"] == 5 and len(events) == 5


def test_list_and_resolve_runs(tmp_path):
    record_run(tmp_path / "first", run_id="first")
    record_run(tmp_path / "second", run_id="second")
    (tmp_path / "not-a-run").mkdir()
    names = [p.name for p in list_runs(tmp_path)]
    assert set(names) == {"first", "second"}
    assert resolve_run(tmp_path, "first").name == "first"
    assert resolve_run(tmp_path).name == names[-1]
    assert list_runs(tmp_path / "missing") == []


def test_story_run_is_read_only_and_tick_bounded(run_dir, offline):
    from replay import story_run
    before = hashes(run_dir)
    assert "KITCHEN STORY: run r-replay" in story_run(run_dir, at=2)
    with pytest.raises(ReplayError):
        story_run(run_dir, at=99)
    assert hashes(run_dir) == before
