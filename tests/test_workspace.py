"""Kitchen updates from the dashboard chat and the team-profile editor.

Profile edits run against a temporary copy of sim/, never the shipped files.
"""

import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402
import scenario  # noqa: E402
from dashboard import Dashboard, make_server  # noqa: E402

CHIPOTLE = ROOT / "sim" / "config_chipotle"


def cli(runs, *args):
    return orchestrator.main(["--runs-dir", str(runs), *args])


def only_run(runs):
    (run_dir,) = [p for p in Path(runs).iterdir() if p.is_dir()]
    return run_dir


def events(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def wait_until_idle(app, limit=30):
    deadline = time.monotonic() + limit
    while app.control_status()["active"]:
        assert time.monotonic() < deadline, "Dashboard runner did not finish"
        time.sleep(0.02)
    assert not app.control_status()["error"]


@pytest.fixture(autouse=True)
def no_paid_calls(monkeypatch):
    """Kitchen updates use the AI interpreter when a key is set; never in tests."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
def sim_copy(tmp_path):
    target = tmp_path / "sim"
    for name in ("config", "personas", "config_chipotle", "personas_chipotle"):
        shutil.copytree(ROOT / "sim" / name, target / name)
    return target


# ------------------------------------------------------------- CLI command

def test_cli_command_applies_the_update_immediately(tmp_path, capsys):
    assert cli(tmp_path, "run", "--scenario", "baseline", "--mode", "det", "--quiet",
               "--config-dir", str(CHIPOTLE), "--max-ticks", "2") == 0
    run_dir = only_run(tmp_path)
    capsys.readouterr()
    assert cli(tmp_path, "command", "--text", "the chip fryer is broken") == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["command"] == {"type": "station", "station_id": "fryer", "available": False}
    assert result["applies_at"] == 1 and not result["free_text"]
    assert result["interpreted_by"] == "keywords" and result["description"] == "Chip fryer is out of service"
    state = json.loads((run_dir / "state.json").read_text())
    assert state["stations"]["fryer"]["available"] is False and state["operator_queue"] == []
    kinds = [e["type"] for e in events(run_dir)]
    said = len(kinds) - 1 - kinds[::-1].index("operator_command")
    assert "station_availability" in kinds[said:]  # the effect follows what the manager said
    assert cli(tmp_path, "resume", "--max-ticks", "1", "--quiet") == 0
    state = json.loads((run_dir / "state.json").read_text())
    assert state["stations"]["fryer"]["available"] is False


def test_cli_free_text_becomes_a_message_and_clock_words_are_refused(tmp_path, capsys):
    assert cli(tmp_path, "run", "--scenario", "baseline", "--mode", "det", "--quiet",
               "--config-dir", str(CHIPOTLE), "--max-ticks", "1") == 0
    capsys.readouterr()
    assert cli(tmp_path, "command", "--text", "rush on O2 please") == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["free_text"] and result["command"] == {"type": "announce", "text": "rush on O2 please"}
    assert cli(tmp_path, "command", "--text", "pause") == 2
    assert "clock control" in capsys.readouterr().err


def test_cli_command_refuses_finished_runs(tmp_path, capsys):
    assert cli(tmp_path, "run", "--scenario", "baseline", "--mode", "det", "--quiet",
               "--config-dir", str(CHIPOTLE)) == 0
    capsys.readouterr()
    assert cli(tmp_path, "command", "--text", "fryer is broken") == 2
    assert "shift has ended" in capsys.readouterr().err


# ------------------------------------------------------- dashboard command

def test_dashboard_update_is_queued_and_shows_in_the_event_log(tmp_path):
    app = Dashboard(tmp_path / "runs", interval=0)
    app.control({"action": "new", "kitchen": "chipotle", "scenario": "baseline", "mode": "det"})
    wait_until_idle(app)
    result = app.command({"run_id": app.run_id, "text": "Tomas is out"})
    assert result["command"] == {"type": "worker", "worker_id": "w_grill", "available": False}
    detail = app.detail(app.run_id)
    said = [e for e in detail["events"] if e["type"] == "operator_command"]
    assert said and said[-1]["payload"]["text"] == "Tomas is out"
    assert detail["state"]["workers"]["w_grill"]["available"] is False  # applied right away
    with pytest.raises(ValueError, match="Type a kitchen update"):
        app.command({"run_id": app.run_id, "text": "  "})
    with pytest.raises(ValueError, match="Invalid run ID"):
        app.command({"run_id": "../etc", "text": "fryer is broken"})


def test_dashboard_update_sent_while_running_waits_for_the_minute(tmp_path):
    app = Dashboard(tmp_path / "runs", interval=0.25)
    app.control({"action": "new", "kitchen": "chipotle", "scenario": "baseline", "mode": "det"})
    wait_until_idle(app)
    run_id = app.run_id
    app.control({"action": "start", "run_id": run_id})
    time.sleep(0.3)
    assert app.control_status()["active"], "run finished before the update could be sent"
    result = app.command({"run_id": run_id, "text": "out of avocado"})
    assert result["command"]["ingredient_id"] == "avocado"
    app.control({"action": "pause"})
    wait_until_idle(app)
    applied = [e for e in app.detail(run_id)["events"] if e["type"] == "stock_changed"]
    state = app.detail(run_id)["state"]
    assert applied or state["operator_queue"], "update was lost"


# ------------------------------------------------------------ team profiles

def edited(data, worker_id, **changes):
    worker = dict(next(w for w in data["workers"] if w["id"] == worker_id))
    worker.update(changes)
    worker.pop("id")
    return worker


def test_profiles_list_workers_personas_skills_and_stations(sim_copy):
    app = Dashboard(sim_root=sim_copy)
    data = app.profiles("chipotle")
    assert [w["id"] for w in data["workers"]][:2] == ["w_km", "w_grill"]
    assert "Marisol" in data["personas"]["w_km"]
    assert data["skills"] == ["fry", "grill", "line", "pack", "prep", "tortilla", "verify"]
    assert data["stations"]["tortilla"] == "Tortilla press"
    assert app.profiles("classic")["workers"][0]["id"] == "w_grill"
    with pytest.raises(ValueError, match="Unknown kitchen"):
        app.profiles("../config")


def test_saving_a_profile_validates_writes_and_reaches_new_runs(sim_copy, tmp_path):
    app = Dashboard(tmp_path / "runs", interval=0, sim_root=sim_copy)
    data = app.profiles("chipotle")
    profile = dict(data["workers"][-1]["profile"], strengths=["Fast learner", "Keeps the line stocked"])
    worker = edited(data, "w_trainee", role="Line trainee", skills={"line": 1.5, "prep": 2.0, "pack": 1.0},
                    profile=profile)
    saved = app.save_profile({"kitchen": "chipotle", "worker_id": "w_trainee", "worker": worker,
                              "persona": "You are Mia, now in your third week."})
    trainee = next(w for w in saved["workers"] if w["id"] == "w_trainee")
    assert trainee["role"] == "Line trainee" and trainee["skills"]["line"] == 1.5
    assert saved["personas"]["w_trainee"].startswith("You are Mia, now in your third week.")
    for variant in ("baseline", "disruption", "blocker"):
        scenario.load_config(sim_copy / "config_chipotle", variant)
    # The shipped files are untouched.
    shipped = json.loads((CHIPOTLE / "workers.json").read_text())["workers"]
    assert next(w for w in shipped if w["id"] == "w_trainee")["role"] == "New crew member"
    app.control({"action": "new", "kitchen": "chipotle", "scenario": "baseline", "mode": "det"})
    wait_until_idle(app)
    assert app.detail(app.run_id)["state"]["workers"]["w_trainee"]["role"] == "Line trainee"


@pytest.mark.parametrize("change, match", [
    # Without w_km grill there is no backup (disruption) and nothing to remove (blocker).
    (lambda d: ("w_km", edited(d, "w_km", skills={"prep": 1.0, "fry": 1.0})), "Not saved.*grill"),
    (lambda d: ("w_grill", edited(d, "w_grill", skills={"grill": 1.0, "juggling": 1.0})), "not required by any menu step"),
    (lambda d: ("w_grill", edited(d, "w_grill", skills={"grill": 0})), "multiplier"),
    (lambda d: ("w_grill", edited(d, "w_grill", name="")), "name"),
    (lambda d: ("w_grill", edited(d, "w_grill", profile={"background": "x"})), "missing field"),
    (lambda d: ("w_grill", dict(edited(d, "w_grill"), id="w_new")), "can't be edited"),
    (lambda d: ("w_nobody", edited(d, "w_grill")), "Unknown worker"),
])
def test_invalid_profile_edits_are_rejected_and_nothing_is_written(sim_copy, change, match):
    app = Dashboard(sim_root=sim_copy)
    before = (sim_copy / "config_chipotle" / "workers.json").read_text()
    persona_before = (sim_copy / "personas_chipotle" / "w_grill.txt").read_text()
    worker_id, worker = change(app.profiles("chipotle"))
    with pytest.raises(ValueError, match=match):
        app.save_profile({"kitchen": "chipotle", "worker_id": worker_id, "worker": worker, "persona": "Changed"})
    assert (sim_copy / "config_chipotle" / "workers.json").read_text() == before
    assert (sim_copy / "personas_chipotle" / "w_grill.txt").read_text() == persona_before


def test_empty_persona_is_rejected(sim_copy):
    app = Dashboard(sim_root=sim_copy)
    data = app.profiles("chipotle")
    with pytest.raises(ValueError, match="persona"):
        app.save_profile({"kitchen": "chipotle", "worker_id": "w_km", "worker": edited(data, "w_km"), "persona": " "})


def test_http_profiles_and_command_endpoints(sim_copy, tmp_path):
    server = make_server(0, tmp_path / "runs", sim_root=sim_copy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}".format(server.server_port)

    def post(path, body, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    try:
        with urllib.request.urlopen(url + "/workspace.js") as response:
            assert b"renderTasks" in response.read()
        with urllib.request.urlopen(url + "/api/meta") as response:
            version = json.load(response)["ui_version"]
        assert len(version) == 16  # the page reloads itself when this changes
        with urllib.request.urlopen(url + "/api/profiles?kitchen=chipotle") as response:
            data = json.load(response)
        assert len(data["workers"]) == 10
        worker = edited(data, "w_cash", shift="Close (3:00 to 10:00 p.m.)")
        worker["profile"] = dict(worker["profile"], shift="Close (3:00 to 10:00 p.m.)")
        worker.pop("shift")
        saved = post("/api/profiles", {"kitchen": "chipotle", "worker_id": "w_cash", "worker": worker,
                                       "persona": data["personas"]["w_cash"]})
        assert next(w for w in saved["workers"] if w["id"] == "w_cash")["profile"]["shift"].startswith("Close")
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/api/profiles", {"kitchen": "chipotle"}, origin="http://another-site.example")
        assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/api/command", {"run_id": "0" * 32, "text": "fryer is broken"})
        assert exc.value.code == 400 and "Run not found" in json.load(exc.value)["error"]
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------ AI-interpreted kitchen updates

def paused_run(tmp_path):
    assert cli(tmp_path, "run", "--scenario", "baseline", "--mode", "det", "--quiet",
               "--config-dir", str(CHIPOTLE), "--max-ticks", "3") == 0
    return only_run(tmp_path)


def fake_agent(changes, summary="ok"):
    seen = []

    def interpret(text, state):
        seen.append(text)
        return {"summary": summary, "model": "fake/model", "latency_ms": 5, "attempts": 1, "changes": changes}
    return interpret, seen


def update(run_dir, text, interpret):
    import policies
    import recording
    return orchestrator.queue_operator_text(run_dir, text, scenario, policies, recording, interpret=interpret)


def test_any_message_goes_through_the_ai_and_changes_the_state_now(tmp_path):
    run_dir = paused_run(tmp_path)
    interpret, seen = fake_agent([
        {"type": "worker", "worker_id": "w_svc", "available": False, "why": "burned hand"},
        {"type": "announce", "text": "Deshawn is off the line; cover front of house.", "why": "tell team"},
    ], summary="Deshawn can't work after burning his hand.")
    result = update(run_dir, "Deshawn burnt his hand and is not able to work", interpret)
    assert seen == ["Deshawn burnt his hand and is not able to work"]
    assert result["interpreted_by"] == "ai" and not result["free_text"]
    assert result["description"] == "Deshawn is off the floor"
    state = json.loads((run_dir / "state.json").read_text())
    assert state["workers"]["w_svc"]["available"] is False and state["operator_queue"] == []
    kinds = [e["type"] for e in events(run_dir)]
    assert kinds[-1] == "announcement" or "announcement" in kinds[kinds.index("operator_command"):]
    assert "worker_availability" in kinds[kinds.index("operator_command"):]


def test_ai_slowdown_uses_existing_skills_and_invented_skills_are_refused(tmp_path):
    run_dir = paused_run(tmp_path)
    interpret, _ = fake_agent([
        {"type": "worker_skill", "worker_id": "w_expo", "skill": "pack", "multiplier": 2, "why": "sore hand"},
        {"type": "worker_skill", "worker_id": "w_expo", "skill": "grill", "multiplier": 1, "why": "invented"},
    ])
    result = update(run_dir, "Priya burnt her hand but can work slowly", interpret)
    state = json.loads((run_dir / "state.json").read_text())
    assert state["workers"]["w_expo"]["skills"]["pack"] == 2
    assert "grill" not in state["workers"]["w_expo"]["skills"]
    assert len(result["rejected"]) == 1


def test_if_the_ai_fails_the_keyword_parser_is_used(tmp_path):
    run_dir = paused_run(tmp_path)

    def broken(text, state):
        raise TimeoutError("provider down")
    result = update(run_dir, "Priya went out for a break", broken)
    assert result["interpreted_by"] == "keywords" and result["ai_error"] == "TimeoutError"
    assert json.loads((run_dir / "state.json").read_text())["workers"]["w_expo"]["available"] is False
