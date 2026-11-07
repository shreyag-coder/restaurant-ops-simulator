"""Read-only historical view of a recorded run.

``replay_run(run_dir, at=None)`` renders a committed run, or the state as of
tick ``at``, from ``state.json``, ``snapshots/`` and ``events.jsonl``.  It
only opens files for reading, never imports the engine, policies, or model
client, and needs no credentials or network.

Tick semantics: ``snapshots/tNNN.json`` is the last committed state saved
during tick NNN.  For the current committed tick, ``state.json`` itself is
used (a crash mid-save can leave that tick's snapshot ahead of the commit).
Events shown are those committed up to the selected state's
``last_event_seq``.
"""

from pathlib import Path

from recording import (CheckpointError, list_snapshot_ticks, load_checkpoint,
                       load_events, load_snapshot)
from reporting import format_event, render_report

REPORT_EVENT_WINDOW = 40

__all__ = ["ReplayError", "replay_run", "replay_state", "list_runs", "latest_run",
           "resolve_run"]


class ReplayError(Exception):
    """A run or tick cannot be replayed; the message says why."""


def list_runs(runs_root):
    """Run directories under ``runs_root`` that have a ``state.json``,
    oldest first (by ``state.json`` modification time, then name)."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    runs = [p for p in root.iterdir() if p.is_dir() and (p / "state.json").is_file()]
    return sorted(runs, key=lambda p: ((p / "state.json").stat().st_mtime, p.name))


def latest_run(runs_root):
    runs = list_runs(runs_root)
    if not runs:
        raise ReplayError("no recorded runs in %s" % runs_root)
    return runs[-1]


def resolve_run(runs_root, run_id=None):
    """Directory for ``run_id`` under ``runs_root``, or the latest run."""
    if run_id is None:
        return latest_run(runs_root)
    run_dir = Path(runs_root) / run_id
    if not run_dir.is_dir():
        known = [p.name for p in list_runs(runs_root)]
        hint = ("available: %s" % ", ".join(known[-10:])) if known else "no recorded runs"
        raise ReplayError("no run %r in %s (%s)" % (run_id, runs_root, hint))
    return run_dir


def _tick_of(state):
    if not isinstance(state, dict):
        return None
    clock = state.get("clock")
    return clock.get("tick") if isinstance(clock, dict) else state.get("time")


def _ranges(ticks):
    """Compact ``[0,1,2,5,6]`` -> ``"0-2, 5-6"``."""
    if not ticks:
        return "none"
    parts, start, prev = [], ticks[0], ticks[0]
    for t in ticks[1:]:
        if t == prev + 1:
            prev = t
            continue
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = t
    parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ", ".join(parts)


def replay_state(run_dir, at=None):
    """Return ``(state, committed_events)`` as of tick ``at`` (read-only)."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise ReplayError("no run found at %s" % run_dir)
    try:
        committed = load_checkpoint(run_dir)
        events = load_events(run_dir)
    except CheckpointError as exc:
        raise ReplayError("cannot replay %s: %s" % (run_dir.name, exc))
    last_tick = _tick_of(committed)
    if at is None or at == last_tick:
        return committed, events
    if isinstance(at, bool) or not isinstance(at, int) or at < 0:
        raise ReplayError("tick must be a non-negative integer, got %r" % (at,))
    available = [t for t in list_snapshot_ticks(run_dir) if t <= last_tick]
    if at > last_tick:
        raise ReplayError("tick %d is beyond the last committed tick %d of run %s; "
                          "recorded ticks: %s" % (at, last_tick, run_dir.name, _ranges(available)))
    if at not in available:
        raise ReplayError("tick %d was not recorded for run %s; recorded ticks: %s"
                          % (at, run_dir.name, _ranges(available)))
    try:
        snapshot = load_snapshot(run_dir, at)
    except CheckpointError as exc:
        raise ReplayError("cannot replay tick %d of %s: %s" % (at, run_dir.name, exc))
    bound = snapshot.get("last_event_seq")
    if not isinstance(bound, int) or bound > committed["last_event_seq"]:
        raise ReplayError("snapshot for tick %d is inconsistent with the committed log" % at)
    return snapshot, events[:bound]


def replay_run(run_dir, at=None):
    """Render a committed run (or tick ``at``) as text.  Read-only."""
    state, events = replay_state(run_dir, at)
    tick = _tick_of(state)
    lines = ["REPLAY of run %s at tick %s (read-only; %d committed events, no model calls)"
             % (state.get("run_id"), tick, len(events)), ""]
    report = render_report(state, events[-REPORT_EVENT_WINDOW:])
    lines.append(report.rstrip("\n"))
    lines += ["", "== Event timeline (through tick %s) ==" % tick]
    if events:
        lines += ["  " + format_event(e) for e in events]
    else:
        lines.append("  (no events)")
    return "\n".join(lines) + "\n"
