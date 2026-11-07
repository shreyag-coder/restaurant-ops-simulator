"""Live operator commands: parser, engine effects, resume safety, console loop."""

import copy
import queue
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402
import policies  # noqa: E402
import recording  # noqa: E402
import scenario  # noqa: E402
from live import interactive, parse  # noqa: E402
from reporting import narrate_events, render_status  # noqa: E402


def new_engine(variant="baseline"):
    config = scenario.load_config(ROOT / "sim" / "config", variant)
    state = orchestrator.create_state(config, "det", run_id="r-live")
    return orchestrator._engine_for(state, scenario, policies)


def events_of(engine, etype):
    return [e for e in engine.pending_events if e["type"] == etype]


# ---------------------------------------------------------------- parser


@pytest.mark.parametrize("text, expected", [
    ("the fryer is broken", {"type": "station", "station_id": "fryer", "available": False}),
    ("Fryer is working again", {"type": "station", "station_id": "fryer", "available": True}),
    ("grill not working", {"type": "station", "station_id": "grill", "available": False}),
    ("fix the prep counter", {"type": "station", "station_id": "prep", "available": True}),
    ("dana is out", {"type": "worker", "worker_id": "w_grill", "available": False}),
    ("Dana is back", {"type": "worker", "worker_id": "w_grill", "available": True}),
    ("grill cook is sick", {"type": "worker", "worker_id": "w_grill", "available": False}),
    ("out of patty", {"type": "stock", "ingredient_id": "patty", "mode": "set", "amount": 0}),
    ("restock patties 5", {"type": "stock", "ingredient_id": "patty", "mode": "add", "amount": 5}),
    ("stock cheese 3", {"type": "stock", "ingredient_id": "cheese", "mode": "set", "amount": 3}),
    ("say rush on O7", {"type": "announce", "text": "rush on O7"}),
])
def test_parse_commands(text, expected):
    assert parse(text, new_engine().state) == ("command", expected)


@pytest.mark.parametrize("text, name", [("status", "status"), ("pause", "pause"), ("go", "go"),
                                        ("step", "step"), ("quit", "quit"), ("stop", "pause")])
def test_parse_controls(text, name):
    assert parse(text, new_engine().state)[:2] == ("control", name)


@pytest.mark.parametrize("text", ["fryer", "dana", "say", ""])
def test_parse_asks_for_clarification(text):
    with pytest.raises(ValueError):
        parse(text, new_engine().state)


def test_unknown_text_becomes_a_message_to_workers():
    kind, command = parse("A customer is complaining loudly", new_engine().state)
    assert kind == "command" and command["type"] == "announce" and command["free_text"]
    assert command["text"] == "A customer is complaining loudly"


@pytest.mark.parametrize("text, name, active", [
    ("There is a fire alarm", "fire_alarm", True), ("all clear", "fire_alarm", False),
    ("the fire alarm is off", "fire_alarm", False), ("false alarm", "fire_alarm", False),
    ("power outage", "power_outage", True), ("power is back", "power_outage", False),
    ("I smell gas", "gas_leak", True), ("gas leak fixed", "gas_leak", False),
])
def test_parse_incidents(text, name, active):
    kind, command = parse(text, new_engine().state)
    assert (kind, command["type"], command["name"], command["active"]) == ("command", "incident", name, active)


def test_fire_alarm_stops_everything_and_all_clear_restores_only_its_stations():
    engine = new_engine()
    engine.run(1)
    assert any(t["status"] == "active" for t in engine.state["tasks"].values())
    engine.queue_command({"type": "station", "station_id": "fryer", "available": False})
    engine.run(1)  # fryer broken on its own, before the alarm
    _, alarm = parse("there is a fire alarm", engine.state)
    engine.queue_command(alarm)
    engine.run(1)
    s = engine.state
    assert all(not st["available"] for st in s["stations"].values())
    assert not any(t["status"] == "active" for t in s["tasks"].values())
    assert all(rec["reserved"] == 0 for rec in s["ingredients"].values())
    assert "fryer" not in s["incidents"]["fire_alarm"]["stations"]
    assert "Fire alarm" in s["announcements"][-1]["text"]
    view = policies.build_worker_view(engine.snapshot(), "w_expo")
    assert all(not st["working"] for st in view["stations"].values())
    text = "\n".join(narrate_events(engine.snapshot(), engine.pending_events))
    assert "Manager: FIRE ALARM!" in text and "because of the fire alarm" in text
    assert "is BROKEN" not in text.split("FIRE ALARM")[-1]  # no per-station noise

    _, clear = parse("all clear", engine.state)
    engine.queue_command(clear)
    engine.run(1)
    s = engine.state
    assert s["incidents"] == {}
    assert s["stations"]["fryer"]["available"] is False  # still broken: not the alarm's doing
    assert all(st["available"] for sid, st in s["stations"].items() if sid != "fryer")
    assert "The fire alarm is over" in "\n".join(narrate_events(engine.snapshot(), engine.pending_events))


def test_repeated_or_unmatched_incident_commands_are_harmless():
    engine = new_engine()
    _, alarm = parse("fire alarm", engine.state)
    _, clear = parse("all clear", engine.state)
    engine.queue_command(clear)  # nothing to clear
    engine.queue_command(alarm)
    engine.queue_command(alarm)  # duplicate
    engine.run(1)
    assert len(events_of(engine, "incident_started")) == 1
    assert not events_of(engine, "incident_cleared")


# ---------------------------------------------------------------- engine


def test_breaking_station_interrupts_and_releases_then_fix_restores():
    engine = new_engine()
    engine.run(1)  # minute 0: fryer work starts
    fry = next(t for t in engine.state["tasks"].values()
               if t["station_id"] == "fryer" and t["status"] == "active")
    potato_before = copy.deepcopy(engine.state["ingredients"]["potato"])
    engine.pending_events.clear()

    engine.queue_command({"type": "station", "station_id": "fryer", "available": False})
    assert engine.state["stations"]["fryer"]["available"] is True  # only applied next minute
    engine.run(1)
    s = engine.state
    assert s["stations"]["fryer"]["available"] is False
    assert s["stations"]["fryer"]["active_tasks"] == []
    assert s["tasks"][fry["id"]]["status"] == "pending"
    potato = s["ingredients"]["potato"]
    assert potato["reserved"] == 0
    assert potato["available"] == potato_before["available"] + potato_before["reserved"]
    assert any(e["payload"].get("reason") == "station_unavailable" for e in events_of(engine, "task_interrupted"))
    assert any("out of service" in r for r in s["blockers"].values())
    assert not any(t["station_id"] == "fryer" and t["status"] == "active" for t in s["tasks"].values())

    engine.queue_command({"type": "station", "station_id": "fryer", "available": True})
    engine.run(1)
    assert engine.state["stations"]["fryer"]["available"] is True
    assert not any("out of service" in r for r in engine.state["blockers"].values())


def test_workers_see_broken_station_and_announcements():
    engine = new_engine()
    engine.queue_command({"type": "station", "station_id": "fryer", "available": False})
    engine.queue_command({"type": "announce", "text": "fryer is down, focus on burgers"})
    engine.run(1)
    view = policies.build_worker_view(engine.snapshot(), "w_fry")
    assert view["stations"]["fryer"]["working"] is False
    assert view["announcements"][-1]["text"] == "fryer is down, focus on burgers"
    assert all(not t["station_has_room"] for t in view["ready_tasks"] if t["task_id"].endswith(".fry"))


def test_stock_and_worker_commands():
    engine = new_engine()
    engine.queue_command({"type": "stock", "ingredient_id": "patty", "mode": "set", "amount": 0})
    engine.queue_command({"type": "worker", "worker_id": "w_grill", "available": False})
    engine.run(1)
    assert engine.state["ingredients"]["patty"]["available"] == 0
    assert engine.state["workers"]["w_grill"]["available"] is False
    assert not any(t["station_id"] == "grill" and t["status"] == "active"
                   for t in engine.state["tasks"].values())
    engine.queue_command({"type": "stock", "ingredient_id": "patty", "mode": "add", "amount": 4})
    engine.queue_command({"type": "worker", "worker_id": "w_grill", "available": True})
    engine.run(1)
    assert engine.state["workers"]["w_grill"]["available"] is True
    assert events_of(engine, "stock_changed")[-1]["payload"]["after"] <= 4


@pytest.mark.parametrize("command", [
    {"type": "station", "station_id": "oven", "available": False},
    {"type": "worker", "worker_id": "w_grill", "available": "no"},
    {"type": "stock", "ingredient_id": "patty", "mode": "set", "amount": -1},
    {"type": "announce", "text": "  "},
    {"type": "teleport"},
])
def test_invalid_commands_are_rejected_and_not_queued(command):
    engine = new_engine()
    with pytest.raises(ValueError):
        engine.queue_command(command)
    assert engine.state["operator_queue"] == []


def test_queued_command_survives_checkpoint_and_is_applied_once(tmp_path):
    run_dir = tmp_path / "r-live"
    engine = new_engine()
    save = lambda e: orchestrator._checkpoint(e, run_dir, recording)  # noqa: E731
    engine.run(1, save)
    engine.queue_command({"type": "station", "station_id": "fryer", "available": False})
    save(engine)  # e.g. the console saved, then the process stopped
    resumed = orchestrator._engine_for(recording.load_checkpoint(run_dir), scenario, policies)
    assert resumed.state["operator_queue"]
    resumed.run(1, lambda e: orchestrator._checkpoint(e, run_dir, recording))
    resumed.run(2, lambda e: orchestrator._checkpoint(e, run_dir, recording))
    changes = [e for e in recording.load_events(run_dir) if e["type"] == "station_availability"]
    assert len(changes) == 1 and changes[0]["tick"] == 1


def test_story_and_status_describe_operator_events():
    engine = new_engine()
    engine.run(1)
    engine.queue_command({"type": "station", "station_id": "fryer", "available": False})
    engine.queue_command({"type": "announce", "text": "hang tight"})
    engine.run(1)
    text = "\n".join(narrate_events(engine.snapshot(), engine.pending_events))
    assert "Manager: The fryer is BROKEN!" in text
    assert "because the fryer went down" in text
    assert "while the fryer is out of service" in text
    assert 'Manager: "hang tight"' in text
    assert "fryer BROKEN" in render_status(engine.snapshot(), "clock running")


# ---------------------------------------------------------------- console loop


def scripted(lines):
    inbox = queue.Queue()
    for line in lines:
        inbox.put(line)
    inbox.put(None)
    return inbox


def test_console_runs_to_the_end_and_applies_commands():
    engine, out = new_engine(), []
    result = interactive(engine, None, pace=0, inbox=scripted(["fryer broken", "banana", "fire alarm",
                                                                 "all clear"]),
                         out=out.append, render_status=render_status)
    assert result == "finished"
    assert engine.state["status"] in orchestrator.TERMINAL
    assert any(">> OK: fryer will be BROKEN" in line for line in out)
    assert any("telling the workers" in line and "banana" in line for line in out)
    assert any(">> OK: FIRE ALARM starts" in line for line in out)


def test_console_pause_step_quit():
    engine, out = new_engine(), []
    result = interactive(engine, None, pace=0, inbox=scripted(["pause", "step", "step", "status", "quit"]),
                         out=out.append, render_status=render_status)
    assert result == "quit"
    assert engine.state["status"] == "paused" and engine.state["status"] not in orchestrator.TERMINAL
    assert any("clock paused" in line for line in out)
