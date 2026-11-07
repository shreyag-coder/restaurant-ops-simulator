"""Checks for the burrito-line crew (sim/config_chipotle, sim/personas_chipotle).

The static checks prove the ten worker profiles, the menu, and the personas
line up. The det runs pin the three-way demo contrast, which only the engine
can show; re-tune the orders if a duration or worker skill changes.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402
from scenario import load_config, validate_config  # noqa: E402

CONFIG = ROOT / "sim" / "config_chipotle"
PERSONAS = ROOT / "sim" / "personas_chipotle"
WORKER_IDS = ["w_km", "w_grill", "w_prep", "w_tortilla", "w_line",
              "w_dml", "w_expo", "w_cash", "w_svc", "w_trainee"]


@pytest.fixture
def baseline():
    return load_config(CONFIG, "baseline")


@pytest.mark.parametrize("variant", ["baseline", "disruption", "blocker"])
def test_every_variant_loads(variant):
    config = load_config(CONFIG, variant)
    assert [w["id"] for w in config["workers"]] == WORKER_IDS
    assert len(config["orders"]) == config["target"]["orders"] == 16


def test_every_worker_has_a_detailed_profile_and_persona(baseline):
    stations = baseline["menu"]["stations"]
    assert {p.stem for p in PERSONAS.glob("*.txt")} == set(WORKER_IDS)
    for worker in baseline["workers"]:
        profile = worker["profile"]
        assert profile["home_station"] in stations
        assert profile["background"].strip() and profile["strengths"] and profile["watch_outs"]
        persona = (PERSONAS / f"{worker['id']}.txt").read_text(encoding="utf-8")
        assert worker["name"] in persona


def test_kitchen_manager_is_the_only_grill_backup(baseline):
    grill = sorted(w["id"] for w in baseline["workers"] if "grill" in w["skills"])
    assert grill == ["w_grill", "w_km"]
    blocker = load_config(CONFIG, "blocker")
    assert "grill" not in next(w for w in blocker["workers"] if w["id"] == "w_km")["skills"]


@pytest.mark.parametrize("change, match", [
    (lambda p: p.update(home_station="walk_in"), "unknown home_station"),
    (lambda p: p.update(experience_years=-1), "experience_years"),
    (lambda p: p.update(experience_years=2.5), "experience_years"),
    (lambda p: p.pop("background"), "missing field"),
    (lambda p: p.update(strengths=["ok", ""]), "strengths"),
    (lambda p: p.update(skills={"grill": 1.0}), "unknown field"),
])
def test_bad_profiles_are_rejected(baseline, change, match):
    config = copy.deepcopy(baseline)
    change(config["workers"][0]["profile"])
    with pytest.raises(ValueError, match=match):
        validate_config(config)


def test_profile_stays_optional(baseline):
    config = copy.deepcopy(baseline)
    for worker in config["workers"]:
        del worker["profile"]
    validate_config(config)


@pytest.mark.parametrize("variant, status, correct, blocked", [
    ("baseline", "completed", 16, False),
    ("disruption", "completed", 16, False),
    ("blocker", "timed_out", 7, True),
])
def test_det_runs_show_the_three_way_contrast(tmp_path, variant, status, correct, blocked):
    assert orchestrator.main(["--runs-dir", str(tmp_path), "run", "--scenario", variant,
                              "--mode", "det", "--config-dir", str(CONFIG)]) == 0
    (run_dir,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == status
    assert sum(o["correct"] is True for o in state["orders"].values()) == correct
    assert bool(state["blockers"]) == blocked
    # Profiles are carried into the authoritative state for inspection.
    assert state["workers"]["w_km"]["profile"]["home_station"] == "prep"


# ------------------------------------------------- live console and story text

@pytest.fixture
def live_state(baseline):
    return orchestrator.create_state(baseline, "det")


@pytest.mark.parametrize("line, expected", [
    ("the tortilla press is broken", {"type": "station", "station_id": "tortilla", "available": False}),
    ("the grill cook is out", {"type": "worker", "worker_id": "w_grill", "available": False}),
    ("marisol is back", {"type": "worker", "worker_id": "w_km", "available": True}),
    ("the kitchen manager is out", {"type": "worker", "worker_id": "w_km", "available": False}),
    ("out of avocados", {"type": "stock", "ingredient_id": "avocado", "mode": "set", "amount": 0}),
    ("restock steak 4", {"type": "stock", "ingredient_id": "steak", "mode": "add", "amount": 4}),
])
def test_console_understands_the_burrito_line(live_state, line, expected):
    import live
    assert live.parse(line, live_state) == ("command", expected)


def test_gas_leak_shuts_the_grill_and_chip_fryer(live_state):
    import live
    _, command = live.parse("gas leak", live_state)
    assert sorted(command["station_ids"]) == ["fryer", "grill"]


def test_story_uses_burrito_line_verbs(tmp_path):
    assert orchestrator.main(["--runs-dir", str(tmp_path), "run", "--scenario", "baseline",
                              "--mode", "det", "--config-dir", str(CONFIG)]) == 0
    (run_dir,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    story = (run_dir / "story.txt").read_text()
    assert "work on for" not in story
    for phrase in ("grilling the protein for", "warming the tortilla for", "rolling O",
                   "1 chips and guac", "mashing the guac for"):
        assert phrase in story, phrase
