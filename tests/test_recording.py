"""Tests for recording.py checkpoint helpers (Agent 4)."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import recording  # noqa: E402
from recording import (CheckpointError, RunLockedError, acquire_run_lock,  # noqa: E402
                       inspect_run, list_snapshot_ticks, load_checkpoint, load_events,
                       release_run_lock, save_checkpoint)

RUN_ID = "r-test"


def make_state(tick, last_seq, **extra):
    state = {"schema_version": 1, "run_id": RUN_ID, "state_version": tick,
             "status": "running", "mode": "det", "model": None,
             "clock": {"tick": tick, "continuation": {"phase": "tick_start"}},
             "inputs": {"config": {"scenario": {"deadline": 30}}, "personas": {}},
             "orders": {}, "tasks": {}, "workers": {}, "stations": {}, "ingredients": {},
             "last_event_seq": last_seq}
    state.update(extra)
    return state


def make_events(first_seq, count, tick):
    return [{"seq": first_seq + i, "run_id": RUN_ID, "tick": tick, "type": "decision",
             "payload": {"i": first_seq + i}} for i in range(count)]


def run_steps(run_dir, steps):
    """steps: list of (tick, n_events); returns all events written."""
    seq, written = 0, []
    for tick, count in steps:
        events = make_events(seq + 1, count, tick)
        seq += count
        save_checkpoint(run_dir, make_state(tick, seq), events)
        written += events
    return written


def file_hashes(folder):
    import hashlib
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


# ------------------------------------------------------------ round trip


def test_round_trip_writes_all_artifacts(tmp_path):
    run = tmp_path / RUN_ID
    events = run_steps(run, [(0, 2), (1, 3)])
    state = load_checkpoint(run)
    assert state == make_state(1, 5)
    assert load_events(run) == events
    assert list_snapshot_ticks(run) == [0, 1]
    assert json.loads((run / "snapshots" / "t000.json").read_text()) == make_state(0, 2)
    report = (run / "report.txt").read_text()
    assert "Run:     r-test" in report and "last committed event #5" in report
    lines = (run / "events.jsonl").read_text().splitlines()
    assert [json.loads(l)["seq"] for l in lines] == [1, 2, 3, 4, 5]


def test_load_events_filters_by_tick(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1), (1, 2), (2, 1)])
    assert [e["seq"] for e in load_events(run, at=1)] == [1, 2, 3]
    assert [e["seq"] for e in load_events(run, at=0)] == [1]


def test_save_does_not_mutate_arguments(tmp_path):
    state, events = make_state(0, 2), make_events(1, 2, 0)
    before = (copy.deepcopy(state), copy.deepcopy(events))
    save_checkpoint(tmp_path / RUN_ID, state, events)
    assert (state, events) == before


def test_mid_tick_save_overwrites_same_tick_snapshot(tmp_path):
    run = tmp_path / RUN_ID
    save_checkpoint(run, make_state(3, 1, status="paused"), make_events(1, 1, 3))
    save_checkpoint(run, make_state(3, 2, status="running"), make_events(2, 1, 3))
    assert list_snapshot_ticks(run) == [3]
    assert json.loads((run / "snapshots" / "t003.json").read_text())["last_event_seq"] == 2


def test_save_with_no_new_events(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1)])
    save_checkpoint(run, make_state(1, 1), [])
    assert load_checkpoint(run)["clock"]["tick"] == 1
    assert len(load_events(run)) == 1


# ------------------------------------------------------------ contract violations


@pytest.mark.parametrize("events, state_seq, message", [
    (make_events(3, 1, 1), 2, "out of order"),               # gap (skips seq 2)
    (make_events(1, 1, 1), 2, "out of order"),               # duplicate seq 1
    (make_events(2, 1, 1), 3, "last_event_seq"),             # caller miscounted
    ([dict(make_events(2, 1, 1)[0], run_id="other")], 2, "run_id"),
    ([dict(make_events(2, 1, 1)[0], tick=9)], 2, "tick"),    # event after state tick
    ([{"seq": 2, "run_id": RUN_ID, "tick": 1}], 2, "missing"),
])
def test_rejects_bad_events_without_touching_disk(tmp_path, events, state_seq, message):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1)])
    before = file_hashes(run)
    with pytest.raises(ValueError, match=message):
        save_checkpoint(run, make_state(1, state_seq), events)
    assert file_hashes(run) == before


def test_rejects_backwards_tick_and_foreign_run(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1), (2, 1)])
    with pytest.raises(ValueError, match="earlier"):
        save_checkpoint(run, make_state(1, 2), [])
    other = make_state(2, 2)
    other["run_id"] = "someone-else"
    with pytest.raises(ValueError, match="belongs to run"):
        save_checkpoint(run, other, [])


def test_unserializable_state_leaves_disk_untouched(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1)])
    before = file_hashes(run)
    with pytest.raises((TypeError, ValueError)):
        save_checkpoint(run, make_state(1, 1, bad=object()), [])
    assert file_hashes(run) == before


def test_rejects_bad_state_shape(tmp_path):
    bad = make_state(0, 0)
    del bad["clock"]
    with pytest.raises(ValueError, match="clock"):
        save_checkpoint(tmp_path / RUN_ID, bad, [])
    with pytest.raises(ValueError, match="schema_version"):
        save_checkpoint(tmp_path / RUN_ID, make_state(0, 0, schema_version=99), [])


# ------------------------------------------------------------ crash boundary


def test_uncommitted_tail_is_ignored_then_truncated(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 2)])
    # Simulate a crash after step 3 of the next save: events appended, state not replaced.
    with open(run / "events.jsonl", "a") as handle:
        for event in make_events(3, 2, 1):
            handle.write(json.dumps(event) + "\n")
        handle.write('{"seq": 5, "run_id": "r-te')  # torn final line
    assert load_checkpoint(run)["last_event_seq"] == 2
    assert [e["seq"] for e in load_events(run)] == [1, 2]
    info = inspect_run(run)
    assert info["uncommitted_lines"] == 2 and info["torn_tail"] and info["error"] is None

    # The resumed orchestrator re-derives events 3.. and saves; the stale tail is replaced.
    replacement = [dict(e, payload={"redo": True}) for e in make_events(3, 1, 1)]
    save_checkpoint(run, make_state(1, 3), replacement)
    assert [e["seq"] for e in load_events(run)] == [1, 2, 3]
    assert load_events(run)[-1]["payload"] == {"redo": True}
    assert len((run / "events.jsonl").read_text().splitlines()) == 3


def test_crash_after_snapshot_keeps_previous_commit(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1)])
    # Crash after step 4 of tick 1: snapshot t001 exists but state.json is still tick 0.
    recording.atomic_write_json(recording.snapshot_path(run, 1), make_state(1, 2))
    state = load_checkpoint(run)
    assert state["clock"]["tick"] == 0 and state["last_event_seq"] == 1


def test_leftover_temp_file_is_harmless(tmp_path):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1)])
    (run / ".state.json.tmp-99999").write_text("{partial")
    assert load_checkpoint(run)["last_event_seq"] == 1
    run_steps_more = make_events(2, 1, 1)
    save_checkpoint(run, make_state(1, 2), run_steps_more)
    assert load_checkpoint(run)["last_event_seq"] == 2


@pytest.mark.parametrize("damage, message", [
    (lambda run: (run / "state.json").unlink(), "missing"),
    (lambda run: (run / "state.json").write_text("{not json"), "not valid JSON"),
    (lambda run: (run / "events.jsonl").write_text(""), "only 0 are recorded"),
    (lambda run: (run / "snapshots" / "t001.json").unlink(), "no snapshot"),
    (lambda run: (run / "events.jsonl").write_text(
        "\n".join(json.dumps(dict(e, seq=7)) for e in make_events(1, 3, 0)) + "\n"),
     "expected 1"),
])
def test_detects_incomplete_checkpoint(tmp_path, damage, message):
    run = tmp_path / RUN_ID
    run_steps(run, [(0, 1), (1, 2)])
    damage(run)
    with pytest.raises(CheckpointError, match=message):
        load_checkpoint(run)
    assert inspect_run(run)["error"]


def test_missing_run_dir(tmp_path):
    with pytest.raises(CheckpointError, match="no run directory"):
        load_checkpoint(tmp_path / "nope")
    assert inspect_run(tmp_path / "nope")["exists"] is False


def test_atomic_write_never_exposes_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    recording.atomic_write_json(target, {"v": 1})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(recording.os, "replace", boom)
    with pytest.raises(OSError):
        recording.atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# ------------------------------------------------------------ writer lock


def test_lock_is_exclusive_and_blocks_foreign_saves(tmp_path):
    run = tmp_path / RUN_ID
    acquire_run_lock(run)
    assert acquire_run_lock(run)  # re-entrant for the owning process
    save_checkpoint(run, make_state(0, 1), make_events(1, 1, 0))  # owner may write

    # A live foreign process holds the lock.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (run / recording.LOCK_FILE).write_text(json.dumps(
            {"pid": child.pid, "host": recording.socket.gethostname()}))
        with pytest.raises(RunLockedError):
            acquire_run_lock(run)
        with pytest.raises(RunLockedError):
            save_checkpoint(run, make_state(1, 1), [])
        assert release_run_lock(run) is False  # not ours to remove
    finally:
        child.kill()
        child.wait()
    # The holder died: its lock is stale and can be reclaimed.
    assert acquire_run_lock(run)
    assert release_run_lock(run) is True
    assert not (run / recording.LOCK_FILE).exists()


def test_run_lock_context_manager(tmp_path):
    run = tmp_path / RUN_ID
    with recording.run_lock(run):
        assert (run / recording.LOCK_FILE).exists()
        assert inspect_run(run)["locked_by"]["pid"] == os.getpid()
    assert not (run / recording.LOCK_FILE).exists()
