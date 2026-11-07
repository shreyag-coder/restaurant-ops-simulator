"""Tests for reporting.render_report (Agent 4).

The sample state below is Agent 4's proposed shape, pending Agent 1's
confirmed field names.  It models one modified multi-quantity order mid-run.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reporting import format_event, natural_key, render_report  # noqa: E402


def sample_state():
    return {
        "schema_version": 1,
        "run_id": "r-sample",
        "state_version": 14,
        "status": "running",
        "mode": "det",
        "model": None,
        "scenario": "disruption",
        "target": {"text": "Complete all 10 orders correctly within 30 simulated minutes.",
                   "deadline": 30},
        "clock": {"tick": 6, "continuation": {"phase": "tick_start"}},
        "last_event_seq": 9,
        "orders": {
            "O2": {"id": "O2", "arrival": 0, "deadline": 15, "status": "in_progress",
                   "items": [{"item_id": "burger", "qty": 2, "modifiers": ["no_lettuce"]},
                             {"item_id": "fries", "qty": 1, "modifiers": []}],
                   "correct": None, "on_time": None},
            "O10": {"id": "O10", "arrival": 20, "deadline": 30, "status": "waiting",
                    "items": [{"item_id": "fries", "qty": 1}], "correct": None,
                    "on_time": None},
            "O1": {"id": "O1", "arrival": 0, "deadline": 12, "status": "completed",
                   "items": [{"item_id": "fries", "qty": 1}], "correct": True,
                   "on_time": True},
        },
        "tasks": {
            "O2.b1.patty": {"id": "O2.b1.patty", "order_id": "O2", "station_id": "grill",
                            "status": "pending", "owner": None},
            "O2.b2.patty": {"id": "O2.b2.patty", "order_id": "O2", "station_id": "grill",
                            "status": "in_progress", "owner": "w_backup",
                            "started_at": 5, "finishes_at": 11},
            "O2.f1.fry": {"id": "O2.f1.fry", "order_id": "O2", "station_id": "fryer",
                          "status": "done", "owner": "w_fry", "started_at": 3,
                          "finishes_at": 6, "finished_at": 6},
            "O1.f1.fry": {"id": "O1.f1.fry", "order_id": "O1", "station_id": "fryer",
                          "status": "done", "owner": "w_fry", "started_at": 0,
                          "finishes_at": 3, "finished_at": 3},
        },
        "stations": {
            "grill": {"id": "grill", "capacity": 2},
            "fryer": {"id": "fryer", "capacity": 1},
        },
        "ingredients": {
            "patty": {"available": 17, "reserved": 1, "unit": "pc"},
            "potato": {"available": 8, "reserved": 0, "unit": "portion"},
        },
        "workers": {
            "w_grill": {"id": "w_grill", "name": "Marco", "available": False,
                        "status": "unavailable", "current_task": None,
                        "skills": {"grill": 1.0, "prep": 1.0},
                        "last_result": {"accepted": True, "task_id": "O2.b1.patty"}},
            "w_backup": {"id": "w_backup", "name": "Priya", "available": True,
                         "status": "busy", "current_task": "O2.b2.patty",
                         "skills": {"grill": 1.5, "assembly": 1.0},
                         "last_result": {"accepted": False, "reason": "task already claimed"}},
            "w_fry": {"id": "w_fry", "name": "Sam", "available": True, "status": "idle",
                      "current_task": None, "skills": {"fry": 1.0, "pack": 1.0},
                      "last_result": None},
        },
        "blockers": [{"task_id": "O2.b1.patty", "missing_skill": "grill",
                      "reason": "no available qualified worker", "tick": 5}],
        "pause": None,
    }


def sample_events():
    return [
        {"seq": 8, "run_id": "r-sample", "tick": 5, "type": "decision", "worker_id": "w_backup",
         "payload": {"proposal": {"action": "start_task", "task_id": "O2.b2.patty",
                                  "why": "Grill cook is out; I am next fastest."},
                     "mode": "det", "model": None, "latency_ms": 0, "attempts": 1,
                     "result": {"accepted": True}}},
        {"seq": 9, "run_id": "r-sample", "tick": 5, "type": "interrupted",
         "worker_id": "w_grill", "task_id": "O2.b1.patty",
         "payload": {"reason": "worker unavailable"}},
    ]


def test_renders_every_section():
    text = render_report(sample_state(), sample_events())
    for heading in ("RESTAURANT OPERATIONS REPORT", "== Results ==", "== Orders ==",
                    "== Tasks ==", "== Stations ==", "== Stock ==", "== Workers ==",
                    "== Recent decisions ==", "== Blockers ==", "== Recent events =="):
        assert heading in text
    assert "minute 6 of 30" in text
    assert "Complete all 10 orders" in text
    assert "Scenario: disruption" in text
    assert "2x burger [no_lettuce]; 1x fries" in text
    assert "REJECTED" not in text  # the only decision was accepted
    assert "ACCEPTED" in text and "Grill cook is out" in text
    assert "rejected: task already claimed" in text
    assert "missing_skill" in text and "no available qualified worker" in text
    assert "interrupted" in text


def test_report_is_pure_and_deterministic():
    state, events = sample_state(), sample_events()
    before = (copy.deepcopy(state), copy.deepcopy(events))
    first = render_report(state, events)
    second = render_report(state, events)
    assert first == second
    assert (state, events) == before


def test_report_reflects_json_round_trip():
    state = sample_state()
    restored = json.loads(json.dumps(state))
    assert render_report(restored, sample_events()) == render_report(state, sample_events())


def test_orders_sorted_naturally_and_stock_shown():
    text = render_report(sample_state(), [])
    assert text.index("  O1 ") < text.index("  O2 ") < text.index("  O10 ")
    stock_line = next(l for l in text.splitlines() if l.strip().startswith("patty "))
    assert stock_line.split()[1:4] == ["17", "1", "pc"]


def test_station_occupancy_and_utilization():
    text = render_report(sample_state(), [])
    grill = next(l for l in text.splitlines() if l.strip().startswith("grill "))
    fryer = next(l for l in text.splitlines() if l.strip().startswith("fryer "))
    assert "1/2" in grill and "O2.b2.patty" in grill and "w_backup" in grill
    # grill: 1 busy minute (5..6) over 2 slots x 6 ticks = 8%
    assert grill.split()[-2:] == ["1", "8%"]
    # fryer: two 3-minute tasks over 1 slot x 6 ticks = 100%
    assert "0/1" in fryer and fryer.split()[-2:] == ["6", "100%"]


def test_correctness_and_timeliness_separate():
    state = sample_state()
    state["status"] = "timed_out"
    state["orders"]["O2"].update(status="completed", correct=True, on_time=False)
    state["orders"]["O10"].update(status="completed", correct=False, on_time=True)
    text = render_report(state, [])
    assert "Correct:          2   (incorrect: 1)" in text
    assert "On time:          2   (late: 1)" in text
    assert "Target met:       NO" in text


def test_target_met_only_when_all_correct_and_on_time():
    state = sample_state()
    state["status"] = "completed"
    for order in state["orders"].values():
        order.update(status="completed", correct=True, on_time=True)
    assert "Target met:       YES" in render_report(state, [])
    state["status"] = "running"
    assert "not decided" in render_report(state, [])


def test_paused_state_shows_error():
    state = sample_state()
    state["status"] = "paused"
    state["mode"], state["model"] = "ai", "provider/model-x"
    state["pause"] = {"reason": "AI decision failed", "worker_id": "w_fry", "attempts": 2,
                      "error": {"type": "timeout", "message": "provider timed out"}}
    text = render_report(state, [])
    assert "Status:  PAUSED" in text
    assert "(model: provider/model-x)" in text
    assert "PAUSED:  AI decision failed" in text
    assert "provider timed out" in text and "attempts: 2" in text


def test_tolerates_minimal_and_list_shaped_state():
    text = render_report({"run_id": "x"}, None)
    assert "(no orders admitted)" in text and "(no workers)" in text
    listed = sample_state()
    listed["workers"] = list(listed["workers"].values())
    listed["stations"] = list(listed["stations"].values())
    assert "w_backup" in render_report(listed, [])


def test_rejects_non_dict_state():
    with pytest.raises(TypeError):
        render_report([], [])


def test_format_event_rejection_and_refs():
    line = format_event({"seq": 3, "tick": 2, "type": "decision", "worker_id": "w_fry",
                         "payload": {"proposal": {"action": "start_task", "task_id": "O1.pack",
                                                  "why": "x"},
                                     "result": {"accepted": False, "reason": "station full"}}})
    assert line.startswith("#3 t=2 decision worker=w_fry")
    assert "start_task O1.pack" in line and "REJECTED: station full" in line


def test_natural_key():
    assert sorted(["O10", "O2", "O1"], key=natural_key) == ["O1", "O2", "O10"]


# ---------------------------------------------------------------- story

import subprocess  # noqa: E402

from reporting import narrate_events, render_story, story_header  # noqa: E402


def story_state():
    return {
        "run_id": "r-story", "mode": "ai", "model": "m/x", "status": "running", "time": 5,
        "target": {"completed_orders": 1},
        "config": {"target": {"description": "Finish order O1."}}, "deadline": 30,
        "workers": {
            "w_grill": {"name": "Dana", "role": "Grill cook", "skills": {"grill": 1.0},
                        "description": "Grills."},
            "w_expo": {"name": "Jordan", "role": "Expediter", "skills": {"grill": 1.0, "pack": 1.0},
                       "description": "Packs."},
        },
        "stations": {"grill": {"name": "Grill"}, "packing": {"name": "Packing counter"}},
        "orders": {"O1": {"items": [{"recipe": "burger", "quantity": 2, "modifiers": ["no_lettuce"]},
                                    {"recipe": "fries", "quantity": 1, "modifiers": []}],
                          "deadline_minutes": 12,
                          "expected_items": [
                              {"id": "O1.b1", "recipe_id": "burger", "final_task_id": "O1.b1.patty"},
                              {"id": "O1.b2", "recipe_id": "burger", "final_task_id": "O1.b2.patty"},
                              {"id": "O1.f1", "recipe_id": "fries", "final_task_id": "O1.f1.fry"}]}},
        "tasks": {
            "O1.b1.patty": {"order_id": "O1", "item_id": "O1.b1", "step_id": "patty", "station_id": "grill"},
            "O1.b2.patty": {"order_id": "O1", "item_id": "O1.b2", "step_id": "patty", "station_id": "grill"},
            "O1.pack": {"order_id": "O1", "item_id": None, "step_id": "pack", "station_id": "packing"},
        },
    }


def ev(seq, tick, etype, worker=None, **payload):
    e = {"seq": seq, "run_id": "r-story", "tick": tick, "type": etype, "payload": payload}
    if worker:
        e["worker_id"] = worker
        payload["worker_id"] = worker
    return e


def decision(seq, tick, worker, action, task, why, accepted, reason="Accepted", round_=0):
    return ev(seq, tick, "decision", worker, round=round_,
              proposal={"action": action, "task_id": task, "why": why},
              result={"accepted": accepted, "reason": reason})


def story_events():
    return [
        ev(1, 0, "order_admitted", order_id="O1", task_ids=[]),
        ev(2, 0, "task_started", "w_expo", task_id="O1.b1.patty", finishes_at=2),
        decision(3, 0, "w_expo", "start_task", "O1.b1.patty", "Earliest deadline.", True),
        decision(4, 0, "w_grill", "start_task", "O1.b1.patty", "I grill.", False, "Task is not pending"),
        ev(5, 0, "task_started", "w_grill", task_id="O1.b2.patty", finishes_at=2),
        decision(6, 0, "w_grill", "start_task", "O1.b2.patty", "Next patty.", True, round_=1),
        ev(7, 2, "task_completed", "w_expo", task_id="O1.b1.patty"),
        ev(8, 2, "item_produced", order_id="O1", item_id="O1.b1"),
        decision(9, 2, "w_grill", "wait", None, "Nothing for me.", True),
        decision(10, 2, "w_expo", "start_task", "O1.pack", "Pack it.", False,
                 "Station is unavailable or full"),
        ev(11, 3, "task_interrupted", "w_grill", task_id="O1.b2.patty"),
        ev(12, 3, "worker_availability", "w_grill", available=False),
        ev(13, 3, "blocker", task_id="O1.b2.patty", reason="No available worker has skill grill"),
        ev(14, 4, "order_verified", order_id="O1", correct=True, on_time=False),
        ev(15, 4, "decision_error", "w_expo", error_type="DecisionError"),
        ev(16, 5, "run_finished", status="timed_out", correct_orders=0),
    ]


def test_story_reads_as_conversation():
    text = render_story(story_state(), story_events())
    expected = [
        "KITCHEN STORY: run r-story (ai mode, model m/x)",
        "Goal: Finish order O1.",
        "Dana (Grill cook): Grills. [grill]",
        "--- Minute 0 ---",
        "Kitchen: New order O1 came in: 2 burgers (no lettuce) and 1 fries. Due by minute 12.",
        "Jordan (Expediter): I'm free, so I'm grilling the patty for O1's burger #1. Ready at minute 2.",
        '    thinking: "Earliest deadline."',
        "Dana (Grill cook): I wanted to grill the patty for O1's burger #1, but Jordan already took it.",
        "Dana (Grill cook): OK, instead I'm grilling the patty for O1's burger #2. Ready at minute 2.",
        "Jordan (Expediter): Done, I grilled the patty for O1's burger #1. O1's burger #1 is ready.",
        "Dana (Grill cook): I'll hold off for now.",
        "Jordan (Expediter): I wanted to pack order O1, but the packing counter is full right now.",
        "Kitchen: Dana (Grill cook) is no longer available!",
        "Kitchen: Dana had to stop grilling the patty for O1's burger #2.",
        "Kitchen: BLOCKED: nobody available can grill the patty for O1's burger #2",
        "Kitchen: Order O1 verified: it's correct, but LATE (due 12).",
        "Kitchen: Couldn't get a decision from Jordan's AI (DecisionError). Simulation paused.",
        "Kitchen: TIME'S UP (timed_out): 0/1 orders correct by minute 5.",
    ]
    for line in expected:
        assert line in text, line
    assert text.index("is no longer available") < text.index("had to stop")
    assert "story continues" in text  # state status is still running


def test_story_is_pure_and_incremental_matches_full():
    state, events = story_state(), story_events()
    before = (copy.deepcopy(state), copy.deepcopy(events))
    full = narrate_events(state, events)
    owners, pieces = {}, []
    for tick in sorted({e["tick"] for e in events}):
        pieces += narrate_events(state, [e for e in events if e["tick"] == tick], owners)
    assert pieces == full
    assert (state, events) == before
    assert story_header(state)[0].startswith("KITCHEN STORY")


def test_cli_prints_live_story_and_quiet_suppresses_it(tmp_path):
    base = [sys.executable, "orchestrator.py", "--runs-dir", str(tmp_path), "run",
            "--scenario", "baseline", "--mode", "det", "--max-ticks", "3"]
    out = subprocess.run(base, cwd=str(ROOT), capture_output=True, text=True, check=True).stdout
    assert "KITCHEN STORY" in out and "--- Minute 0 ---" in out
    assert "I'm free, so I'm" in out and 'thinking: "' in out
    quiet = subprocess.run(base + ["--quiet"], cwd=str(ROOT), capture_output=True, text=True,
                           check=True).stdout
    assert "--- Minute" not in quiet and "Run " in quiet
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    story = subprocess.run([sys.executable, "orchestrator.py", "--runs-dir", str(tmp_path),
                            "replay", "--run", run_dir.name, "--story", "--at", "0"],
                           cwd=str(ROOT), capture_output=True, text=True, check=True).stdout
    assert "--- Minute 0 ---" in story and "--- Minute 1 ---" not in story
