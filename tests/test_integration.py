"""End-to-end check with the real modules of all four agents (no fakes).

The per-module tests use stand-ins for each other's code, so they cannot
catch contract mismatches between modules (for example the state's tick
field or the run-lock file format). This file runs the real CLI.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402

CONFIG = ROOT / "sim" / "config"


def cli(runs, *args):
    return orchestrator.main(["--runs-dir", str(runs), *args])


def only_run(runs):
    (run_dir,) = [p for p in runs.iterdir() if p.is_dir()]
    return run_dir


@pytest.mark.parametrize("variant, status, correct", [
    ("baseline", "completed", 10),
    ("disruption", "completed", 10),
    ("blocker", "timed_out", 6),
])
def test_det_run_records_state_events_snapshots_and_report(tmp_path, variant, status, correct):
    assert cli(tmp_path, "run", "--scenario", variant, "--mode", "det",
               "--config-dir", str(CONFIG)) == 0
    run_dir = only_run(tmp_path)
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == status
    assert sum(o["correct"] is True for o in state["orders"].values()) == correct
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in events] == list(range(1, state["last_event_seq"] + 1))
    assert state["last_committed_seq"] == state["last_event_seq"]
    assert (run_dir / "snapshots").is_dir() and any((run_dir / "snapshots").iterdir())
    report = (run_dir / "report.txt").read_text()
    assert "Complete all 10 orders correctly" in report and "x ?" not in report
    kinds = {e["type"] for e in events}
    if variant != "baseline":
        assert "task_interrupted" in kinds
    assert ("blocker" in kinds) == (variant == "blocker")


def test_step_resume_matches_uninterrupted_run_and_replay_is_offline(tmp_path):
    whole, stepped = tmp_path / "whole", tmp_path / "stepped"
    assert cli(whole, "run", "--scenario", "disruption", "--mode", "det", "--config-dir", str(CONFIG)) == 0
    assert cli(stepped, "run", "--scenario", "disruption", "--mode", "det", "--config-dir", str(CONFIG),
               "--max-ticks", "6") == 0
    assert cli(stepped, "resume") == 0
    assert cli(stepped, "resume") == 2  # terminal runs cannot resume

    def sequence(runs):
        lines = (only_run(runs) / "events.jsonl").read_text().splitlines()
        return [(e["tick"], e["type"], e.get("worker_id"), e.get("task_id"))
                for e in map(json.loads, lines)]

    assert sequence(stepped) == sequence(whole)

    run_dir = only_run(stepped)
    before = {p: p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    assert cli(stepped, "replay", "--at", "5") == 0
    assert {p: p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before
