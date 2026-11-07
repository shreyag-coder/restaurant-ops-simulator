"""Exercise the local UI bridge against real orchestrator subprocesses."""

import copy
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dashboard import Dashboard, make_server
_spec = importlib.util.spec_from_file_location("dashboard_export", Path(__file__).resolve().parents[1] / "dashboard" / "export_replay.py")
_export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_export)
export_site = _export.export_site


def wait_until_idle(app):
    deadline = time.monotonic() + 15
    while app.control_status()["active"]:
        assert time.monotonic() < deadline, "Dashboard runner did not finish"
        time.sleep(0.025)
    assert not app.control_status()["error"]


def test_step_resume_and_export_use_real_committed_state(tmp_path):
    app = Dashboard(tmp_path / "runs", interval=0)
    app.control({"action": "new", "scenario": "disruption", "mode": "det"})
    wait_until_idle(app)
    run_id = app.run_id
    first = app.detail(run_id)
    assert first["state"]["time"] == 0 and first["state"]["status"] == "paused"
    assert len(app.runs()) == 1
    app.control({"action": "step", "run_id": run_id})
    wait_until_idle(app)
    assert app.detail(run_id)["state"]["time"] == 1
    app.control({"action": "start", "run_id": run_id})
    wait_until_idle(app)
    finished = app.detail(run_id)
    assert finished["state"]["status"] == "completed"
    assert sum(o["correct"] is True for o in finished["state"]["orders"].values()) == 10
    assert app.detail(run_id, 5)["state"]["time"] == 5
    seqs = [e["seq"] for e in finished["events"]]
    assert seqs == list(range(1, len(seqs) + 1))
    export_site(tmp_path / "site", app.runs_dir)
    manifest = json.loads((tmp_path / "site" / "dist" / "data" / "manifest.json").read_text())
    assert manifest["runs"][0]["variant"] == "disruption"
    bundle = json.loads((tmp_path / "site" / "dist" / "data" / "runs" / (run_id + ".json")).read_text())
    assert "5" in bundle["snapshots"]
    assert "personas" not in bundle["state"] and "source" not in bundle["state"]["config"]
    assert bundle["state"]["orders"] == finished["state"]["orders"]


def test_pause_stops_between_ticks_and_prevents_overlapping_controls(tmp_path):
    app = Dashboard(tmp_path / "runs", interval=2)
    app.control({"action": "start", "scenario": "baseline", "mode": "det"})
    with pytest.raises(ValueError, match="already progressing"):
        app.control({"action": "new"})
    while app.run_id is None:
        time.sleep(0.01)
    app.control({"action": "pause"})
    wait_until_idle(app)
    before = copy.deepcopy(app.detail(app.run_id)["state"])
    time.sleep(0.1)
    assert app.detail(app.run_id)["state"] == before
    assert before["status"] == "paused"


def test_invalid_controls_and_missing_ai_configuration_rejected(tmp_path, monkeypatch):
    app = Dashboard(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    for data in ({"action": "unknown"}, {"action": "start", "scenario": "invalid"},
                 {"action": "start", "mode": "ai"}, {"action": "step", "run_id": "../outside"}):
        with pytest.raises(ValueError):
            app.control(data)
    assert not app.control_status()["active"]


def test_ten_worker_kitchen_preset_uses_its_actual_config(tmp_path):
    app = Dashboard(tmp_path / "runs")
    app.control({"action": "new", "kitchen": "chipotle", "scenario": "baseline", "mode": "det"})
    wait_until_idle(app)
    state = app.detail(app.run_id)["state"]
    assert len(state["workers"]) == 10 and len(state["orders"]) == 16
    assert "w_trainee" in state["workers"] and "w_km" in state["workers"]
    assert app.runs()[0]["kitchen"] == "chipotle"


def test_http_serves_ui_and_blocks_cross_origin_controls(tmp_path):
    server = make_server(0, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}".format(server.server_port)
    try:
        with urllib.request.urlopen(url) as response:
            assert b'id="conversation-messages"' in response.read()
        with urllib.request.urlopen(url + "/experience.js") as response:
            assert b"conversationMessages" in response.read()
        with urllib.request.urlopen(url + "/api/meta") as response:
            meta = json.load(response)
            assert "ai_available" in meta and "OPENROUTER_API_KEY" not in str(meta)
        request = urllib.request.Request(url + "/api/control", data=b'{"action":"new"}',
                                         headers={"Content-Type": "application/json", "Origin": "http://another-site.example"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url + "/.env")
    finally:
        server.shutdown()
        server.server_close()
