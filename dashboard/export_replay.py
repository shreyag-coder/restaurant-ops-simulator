"""Package recent real recordings for the private static replay dashboard.

Usage: python3 dashboard/export_replay.py
The generated Site is separate from the Python repo; it contains only the
viewer, its hosting manifest, and sanitized copies of selected recordings.
"""

import copy
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recording
import replay
from dashboard import summary


def viewer_state(state):
    result = copy.deepcopy(state)
    # No local paths, persona prompts, or provider continuation payloads are
    # necessary to inspect a recording. Runtime secrets are never in state.
    result.pop("personas", None)
    result.pop("continuation", None)
    result["config"].pop("source", None)
    return result


def export_site(destination=None, runs_root=None):
    project = Path(destination or ROOT / "dashboard" / "replay-site")
    dist = project / "dist"
    runs_root = Path(runs_root or ROOT / "sim" / "runs")
    web = ROOT / "dashboard" / "web"
    (dist / "data" / "runs").mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "styles.css", "app.js", "experience.css", "experience.js",
                 "workspace.css", "workspace.js"):
        shutil.copyfile(web / name, dist / name)
    (dist / "config.js").write_text("window.DASHBOARD_CONFIG = { static: true };\n", encoding="utf-8")
    selected = {}
    for folder in reversed(replay.list_runs(runs_root)):
        try:
            state = recording.load_checkpoint(folder)
            variant = state["config"].get("variant", "baseline")
            key = (summary(state)["kitchen"], state["mode"], variant)
            if key not in selected and state["status"] in {"completed", "timed_out"}:
                selected[key] = folder
        except (recording.CheckpointError, OSError, KeyError):
            continue
    rows = []
    for key, folder in sorted(selected.items()):
        state, events = replay.replay_state(folder)
        snapshots = {str(t): viewer_state(replay.replay_state(folder, at=t)[0])
                     for t in recording.list_snapshot_ticks(folder) if t <= state["time"]}
        payload = {"state": viewer_state(state), "events": events, "snapshots": snapshots}
        path = dist / "data" / "runs" / (state["run_id"] + ".json")
        path.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        rows.append(summary(state))
    if not rows:
        raise ValueError("No completed recordings available. Run a deterministic scenario first.")
    (dist / "data" / "manifest.json").write_text(json.dumps({"runs": rows}, indent=2), encoding="utf-8")
    referenced = {row["run_id"] + ".json" for row in rows}
    for old in (dist / "data" / "runs").glob("*.json"):
        if old.name not in referenced:
            old.unlink()
    print("Prepared private replay viewer with {} recorded runs in {}".format(len(rows), dist))
    return project


if __name__ == "__main__":
    export_site()
