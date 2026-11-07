"""CLI integration against explicit stand-ins for Agents 2, 3, and 4.

These stand-ins live in tests only. A passing test does not claim that the
production scenario/policy/recording modules or real AI provider exist.
"""

import ast
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator
from test_engine import expand_example, example_policy, example_view, tiny_config


def install_helpers(monkeypatch, tmp_path, variant_event=False):
    """Disk-backed test recording helper, not production persistence code."""
    def config_loader(config_dir, variant):
        return tiny_config(event_at=1 if variant_event else None, backup=variant != "blocker")

    def policy(view):
        return example_policy(view, view["worker_id"])

    def save_checkpoint(run_dir, state, events):
        run_dir = Path(run_dir)
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")
        snapshots = run_dir / "snapshots"
        snapshots.mkdir(exist_ok=True)
        (snapshots / ("t{:02d}.json".format(state["time"]))).write_text(json.dumps(state), encoding="utf-8")

    def replay_run(run_dir, at=None):
        path = Path(run_dir) / "state.json" if at is None else Path(run_dir) / "snapshots" / ("t{:02d}.json".format(at))
        state = json.loads(path.read_text(encoding="utf-8"))
        return "Replay {}: minute {}".format(state["run_id"], state["time"])

    scenario = SimpleNamespace(load_config=config_loader, validate_config=lambda config: None, expand_order=expand_example)
    policies = SimpleNamespace(build_worker_view=example_view, choose_deterministic=policy)
    recording = SimpleNamespace(save_checkpoint=save_checkpoint, load_checkpoint=lambda path: json.loads((Path(path) / "state.json").read_text(encoding="utf-8")))
    monkeypatch.setitem(sys.modules, "scenario", scenario)
    monkeypatch.setitem(sys.modules, "policies", policies)
    monkeypatch.setitem(sys.modules, "recording", recording)
    monkeypatch.setitem(sys.modules, "replay", SimpleNamespace(replay_run=replay_run))
    return scenario, policies, recording


def read_run(root):
    paths = list(root.iterdir())
    assert len(paths) == 1
    return paths[0], json.loads((paths[0] / "state.json").read_text(encoding="utf-8"))


def run_args(root, *extra):
    return ["--runs-dir", str(root), "run", "--scenario", "baseline", "--mode", "det", *extra]


def test_help_has_no_integration_dependency_or_side_effects(tmp_path):
    path = str(Path(orchestrator.__file__).resolve())
    result = subprocess.run([sys.executable, path, "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert "run" in result.stdout and "resume" in result.stdout and "replay" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_start_requires_explicit_mode_and_scenario():
    with pytest.raises(SystemExit) as exc:
        orchestrator.main(["run", "--scenario", "baseline"])
    assert exc.value.code == 2


def test_missing_dependency_is_clear_without_creating_run(monkeypatch, tmp_path, capsys):
    def missing(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(orchestrator.importlib, "import_module", missing)
    root = tmp_path / "runs"
    assert orchestrator.main(run_args(root)) == 2
    assert "Required integration module is not available: scenario" in capsys.readouterr().err
    assert not root.exists()


def test_real_cli_run_records_and_completes_one_order(monkeypatch, tmp_path):
    install_helpers(monkeypatch, tmp_path)
    root = tmp_path / "runs"
    assert orchestrator.main(run_args(root)) == 0
    run_dir, state = read_run(root)
    assert state["status"] == "completed" and state["orders"]["O1"]["correct"]
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert state["last_committed_seq"] == state["last_event_seq"] == events[-1]["seq"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


def test_cli_step_resume_uses_frozen_input_and_refuses_terminal(monkeypatch, tmp_path, capsys):
    scenario, _, _ = install_helpers(monkeypatch, tmp_path, variant_event=True)
    root = tmp_path / "runs"
    assert orchestrator.main(run_args(root, "--max-ticks", "1")) == 0
    run_dir, paused = read_run(root)
    assert paused["status"] == "paused" and paused["next_tick"] == 1

    def should_not_reload(*args):
        raise AssertionError("Resume must use frozen inputs")
    scenario.load_config = should_not_reload
    assert orchestrator.main(["--runs-dir", str(root), "resume", "--run", run_dir.name]) == 0
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "completed" and state["applied_events"] == [0]
    assert orchestrator.main(["--runs-dir", str(root), "resume", "--run", run_dir.name]) == 2
    assert "Only paused runs may resume" in capsys.readouterr().err
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["type"] == "order_admitted"]) == 1
    assert len([e for e in events if e["type"] == "task_interrupted"]) == 1
    assert len({e["seq"] for e in events}) == len(events)


def test_replay_does_not_load_policy_or_write(monkeypatch, tmp_path, capsys):
    install_helpers(monkeypatch, tmp_path)
    root = tmp_path / "runs"
    assert orchestrator.main(run_args(root)) == 0
    run_dir, _ = read_run(root)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in run_dir.rglob("*") if p.is_file()}
    real_import = orchestrator.importlib.import_module

    def offline_import(name):
        assert name == "replay", "Replay must not import scenario, policies, or recording"
        return real_import(name)
    monkeypatch.setattr(orchestrator.importlib, "import_module", offline_import)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert orchestrator.main(["--runs-dir", str(root), "replay", "--run", run_dir.name, "--at", "0"]) == 0
    assert "minute 0" in capsys.readouterr().out
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in run_dir.rglob("*") if p.is_file()}


def test_replay_missing_tick_is_an_error(monkeypatch, tmp_path, capsys):
    install_helpers(monkeypatch, tmp_path)
    root = tmp_path / "runs"
    orchestrator.main(run_args(root))
    assert orchestrator.main(["--runs-dir", str(root), "replay", "--at", "999"]) == 2
    assert "Error:" in capsys.readouterr().err


def test_checkpoint_failure_does_not_acknowledge_buffered_events(tmp_path):
    engine = orchestrator.SimulationEngine(orchestrator.create_state(tiny_config(), "det"), expand_example, example_view, example_policy)
    engine.run(max_ticks=1)
    before = copy.deepcopy(engine.pending_events)
    original_commit = engine.state["last_committed_seq"]

    def fail(*args):
        raise OSError("Disk failure")
    with pytest.raises(OSError):
        orchestrator._checkpoint(engine, tmp_path, SimpleNamespace(save_checkpoint=fail))
    assert engine.pending_events == before
    assert engine.state["last_committed_seq"] == original_commit


def test_checkpoint_writer_cannot_mutate_engine(tmp_path):
    engine = orchestrator.SimulationEngine(orchestrator.create_state(tiny_config(), "det"), expand_example, example_view, example_policy)
    engine.run(max_ticks=1)

    def corrupt(path, state, events):
        state["ingredients"]["patty"]["available"] = -1
        state["last_committed_seq"] = -1
        events.clear()
    original_seq = engine.state["last_event_seq"]
    orchestrator._checkpoint(engine, tmp_path, SimpleNamespace(save_checkpoint=corrupt))
    assert engine.state["ingredients"]["patty"]["available"] >= 0
    assert engine.state["last_committed_seq"] == original_seq


def test_second_process_cannot_write_same_run(tmp_path):
    with orchestrator.run_lock(tmp_path):
        script = "from pathlib import Path; from orchestrator import run_lock\nwith run_lock(Path(__import__('sys').argv[1])): pass\n"
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
        assert result.returncode != 0 and "Another orchestrator" in result.stderr
    # OS releases the first lock when the handle closes; no stale-file blocker.
    with orchestrator.run_lock(tmp_path):
        pass


@pytest.mark.parametrize("identifier", ["..", ".", "../other", "/tmp", "missing"])
def test_run_selection_rejects_traversal_and_unknown_ids(tmp_path, identifier):
    with pytest.raises(ValueError):
        orchestrator._select_run(tmp_path, identifier)


def test_run_selection_latest_and_symlink_rejected(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "state.json").write_text("{}")
    (second / "state.json").write_text("{}")
    os.utime(first / "state.json", ns=(1, 1))
    os.utime(second / "state.json", ns=(2, 2))
    assert orchestrator._select_run(tmp_path, None) == second
    (tmp_path / "alias").symlink_to(second, target_is_directory=True)
    with pytest.raises(ValueError):
        orchestrator._select_run(tmp_path, "alias")


def test_ai_requires_credentials_before_run_creation(monkeypatch, tmp_path, capsys):
    install_helpers(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    root = tmp_path / "runs"
    args = run_args(root)
    args[-1] = "ai"
    assert orchestrator.main(args) == 2
    assert "requires OPENROUTER_MODEL and OPENROUTER_API_KEY" in capsys.readouterr().err
    assert not root.exists()


def test_ai_error_is_saved_and_resume_does_not_reload_personas(monkeypatch, tmp_path, capsys):
    _, policies, _ = install_helpers(monkeypatch, tmp_path)
    personas = tmp_path / "personas"
    personas.mkdir()
    for worker in ("A", "B"):
        (personas / (worker + ".txt")).write_text("Frozen " + worker)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-never-send")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    calls = []
    fail = True

    def ai_policy(view, persona):
        nonlocal fail
        calls.append((view["time"], view["worker_id"], persona))
        if view["worker_id"] == "B" and fail:
            fail = False
            raise TimeoutError("secret fake-key-never-send")
        envelope = example_policy(view, view["worker_id"])
        envelope.update(mode="ai", model="test/model")
        return envelope
    policies.choose_ai = ai_policy
    root = tmp_path / "runs"
    assert orchestrator.main(["--runs-dir", str(root), "run", "--scenario", "baseline", "--mode", "ai", "--personas-dir", str(personas)]) == 2
    run_dir, state = read_run(root)
    assert state["status"] == "paused" and state["pause_reason"] == "decision_error"
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert "fake-key-never-send" not in path.read_text()
    (personas / "A.txt").write_text("Changed after run")
    monkeypatch.setenv("OPENROUTER_MODEL", "different/model")
    assert orchestrator.main(["--runs-dir", str(root), "resume"]) == 0
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "completed" and state["model"] == "test/model"
    assert all(persona == "Frozen " + worker for _, worker, persona in calls)
    assert calls.count((0, "A", "Frozen A")) == 1
    assert "fake-key-never-send" not in capsys.readouterr().err


def test_incomplete_checkpoint_rejected(monkeypatch, tmp_path, capsys):
    install_helpers(monkeypatch, tmp_path)
    root = tmp_path / "runs"
    orchestrator.main(run_args(root, "--max-ticks", "1"))
    run_dir, state = read_run(root)
    state["last_committed_seq"] -= 1
    (run_dir / "state.json").write_text(json.dumps(state))
    assert orchestrator.main(["--runs-dir", str(root), "resume"]) == 2
    assert "Incomplete checkpoint" in capsys.readouterr().err


def test_starts_new_run_without_overwriting_old(monkeypatch, tmp_path):
    install_helpers(monkeypatch, tmp_path)
    root = tmp_path / "runs"
    assert orchestrator.main(run_args(root)) == 0
    run_dir, _ = read_run(root)
    original = (run_dir / "state.json").read_bytes()
    assert orchestrator.main(run_args(root)) == 0
    assert len(list(root.iterdir())) == 2
    assert (run_dir / "state.json").read_bytes() == original


def test_source_uses_python39_compatible_syntax():
    ast.parse(Path(orchestrator.__file__).read_text(), feature_version=(3, 9))
