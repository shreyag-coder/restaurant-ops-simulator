"""Checkpoint helpers for a run directory.

Only the orchestrator process calls the *writer* functions here
(``save_checkpoint``, ``acquire_run_lock``, ``release_run_lock``); this
module never starts a writer of its own.  Readers (``load_checkpoint``,
``load_events``, ``inspect_run``) never modify files.

Run directory layout::

    sim/runs/<run_id>/
      state.json          authoritative state; the COMMIT MARKER
      events.jsonl        one JSON event per line, seq = 1, 2, 3, ...
      snapshots/tNNN.json state copy at the latest save of tick NNN
      report.txt          rendered by reporting.render_report
      .writer.lock        present while an orchestrator owns the run

Committed-event boundary
------------------------
``state["last_event_seq"]`` in ``state.json`` is the boundary.  An event is
committed iff its ``seq <= last_event_seq`` of the current ``state.json``.

``save_checkpoint`` writes in this order:

1. serialize everything in memory (nothing touches disk if this fails);
2. drop any uncommitted tail of ``events.jsonl`` left by an earlier crash;
3. append the new events, flush + fsync;
4. atomically replace ``snapshots/tNNN.json``;
5. atomically replace ``report.txt``;
6. atomically replace ``state.json``  <- commit point.

A crash before step 6 leaves the previous checkpoint fully valid: readers
ignore event lines past the boundary, and the next save removes them.
"Atomic replace" is write-to-temp-in-same-dir, fsync, ``os.replace``.
"""

import contextlib
import json
import os
import socket
import time
from pathlib import Path

from reporting import render_report

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
REPORT_FILE = "report.txt"
SNAPSHOT_DIR = "snapshots"
LOCK_FILE = ".writer.lock"
REPORT_EVENT_WINDOW = 40

REQUIRED_STATE_KEYS = ("schema_version", "run_id", "clock", "last_event_seq")
REQUIRED_EVENT_KEYS = ("seq", "run_id", "tick", "type")

__all__ = [
    "SCHEMA_VERSION", "CheckpointError", "RunLockedError",
    "save_checkpoint", "load_checkpoint", "load_events", "inspect_run",
    "snapshot_path", "list_snapshot_ticks", "load_snapshot",
    "acquire_run_lock", "release_run_lock", "run_lock",
    "atomic_write_text", "atomic_write_json", "dumps_json",
]


class CheckpointError(Exception):
    """The run directory is missing, incomplete, or inconsistent."""


class RunLockedError(CheckpointError):
    """Another live process owns the run's writer lock."""


# ------------------------------------------------------------ primitives


def dumps_json(data):
    """Canonical JSON text used for every state/snapshot file."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _dumps_event(event):
    return json.dumps(event, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _fsync_dir(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path, text):
    """Replace ``path`` with ``text`` so readers see old or new, never partial."""
    path = Path(path)
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    _fsync_dir(path.parent)


def atomic_write_json(path, data):
    atomic_write_text(path, dumps_json(data))


def snapshot_path(run_dir, tick):
    return Path(run_dir) / SNAPSHOT_DIR / ("t%03d.json" % tick)


def list_snapshot_ticks(run_dir):
    """Sorted ticks that have a snapshot file (read-only)."""
    folder = Path(run_dir) / SNAPSHOT_DIR
    ticks = []
    if folder.is_dir():
        for entry in folder.iterdir():
            name = entry.name
            if name.startswith("t") and name.endswith(".json") and name[1:-5].isdigit():
                ticks.append(int(name[1:-5]))
    return sorted(ticks)


# ------------------------------------------------------------ validation


def _tick_of(state):
    clock = state.get("clock")
    tick = clock.get("tick") if isinstance(clock, dict) else None
    if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
        raise ValueError("state['clock']['tick'] must be a non-negative integer")
    return tick


def _check_state(state):
    if not isinstance(state, dict):
        raise ValueError("state must be a dict")
    missing = [k for k in REQUIRED_STATE_KEYS if k not in state]
    if missing:
        raise ValueError("state is missing required keys: %s" % ", ".join(missing))
    if state["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("unsupported schema_version %r" % (state["schema_version"],))
    if not isinstance(state["run_id"], str) or not state["run_id"]:
        raise ValueError("state['run_id'] must be a non-empty string")
    seq = state["last_event_seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("state['last_event_seq'] must be a non-negative integer")
    return _tick_of(state)


def _check_events(events, run_id, first_seq, max_tick):
    expected = first_seq
    last_tick = None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("event must be a dict, got %s" % type(event).__name__)
        missing = [k for k in REQUIRED_EVENT_KEYS if k not in event]
        if missing:
            raise ValueError("event is missing required keys: %s" % ", ".join(missing))
        if event["seq"] != expected or isinstance(event["seq"], bool):
            raise ValueError("event seq %r out of order; expected %d" % (event["seq"], expected))
        if event["run_id"] != run_id:
            raise ValueError("event #%d has run_id %r, expected %r"
                             % (expected, event["run_id"], run_id))
        tick = event["tick"]
        if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0 or tick > max_tick:
            raise ValueError("event #%d tick %r must be an integer in 0..%d"
                             % (expected, tick, max_tick))
        if last_tick is not None and tick < last_tick:
            raise ValueError("event #%d tick %d goes backwards" % (expected, tick))
        last_tick = tick
        expected += 1


# ------------------------------------------------------------ event file


def _scan_events(path):
    """Parse ``events.jsonl``; return ``(events, byte_offsets, torn_tail)``.

    ``byte_offsets[i]`` is the end offset of line ``i``.  A final line with
    no newline or invalid JSON is reported as a torn tail, not an event.
    """
    events, offsets, torn = [], [], False
    path = Path(path)
    if not path.exists():
        return events, offsets, torn
    data = path.read_bytes()
    pos = 0
    while pos < len(data):
        end = data.find(b"\n", pos)
        if end == -1:
            torn = True  # partial final line from an interrupted append
            break
        line = data[pos:end]
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if end + 1 >= len(data):
                torn = True
                break
            raise CheckpointError("%s: unreadable line %d" % (path, len(events) + 1))
        events.append(event)
        offsets.append(end + 1)
        pos = end + 1
    return events, offsets, torn


def _committed(events, last_seq, run_id, where):
    if len(events) < last_seq:
        raise CheckpointError("%s: state commits %d events but only %d are recorded"
                              % (where, last_seq, len(events)))
    committed = events[:last_seq]
    for index, event in enumerate(committed, start=1):
        if not isinstance(event, dict) or event.get("seq") != index:
            raise CheckpointError("%s: line %d has seq %r, expected %d"
                                  % (where, index, event.get("seq") if isinstance(event, dict)
                                     else None, index))
        if event.get("run_id") != run_id:
            raise CheckpointError("%s: event #%d belongs to run %r"
                                  % (where, index, event.get("run_id")))
    return committed


def _read_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise CheckpointError("%s is missing: %s" % (what, path))
    except ValueError as exc:
        raise CheckpointError("%s is not valid JSON (%s): %s" % (what, exc, path))


# ------------------------------------------------------------ lock


def _lock_owner(lock_path):
    try:
        with open(lock_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_run_lock(run_dir):
    """Claim exclusive writer ownership of ``run_dir``.

    Uses ``O_CREAT | O_EXCL``.  A lock left by a dead process on this host
    is reclaimed; a lock held by a live process raises ``RunLockedError``.
    Returns the lock path.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / LOCK_FILE
    record = {"pid": os.getpid(), "host": socket.gethostname(), "acquired_at": time.time()}
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = _lock_owner(lock_path)
            if owner.get("pid") == os.getpid() and owner.get("host") == record["host"]:
                return lock_path  # already ours
            stale = owner.get("host") == record["host"] and not _pid_alive(owner.get("pid"))
            if stale:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            raise RunLockedError("run %s is already being written by pid %s on %s"
                                 % (run_dir.name, owner.get("pid", "?"), owner.get("host", "?")))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return lock_path
    raise RunLockedError("could not acquire writer lock for %s" % run_dir)


def release_run_lock(run_dir):
    """Remove the lock if this process owns it.  Returns True if removed."""
    lock_path = Path(run_dir) / LOCK_FILE
    owner = _lock_owner(lock_path)
    if owner.get("pid") == os.getpid() and owner.get("host") == socket.gethostname():
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()
        return True
    return False


@contextlib.contextmanager
def run_lock(run_dir):
    acquire_run_lock(run_dir)
    try:
        yield
    finally:
        release_run_lock(run_dir)


def _assert_not_foreign_locked(run_dir):
    lock_path = Path(run_dir) / LOCK_FILE
    if not lock_path.exists():
        return
    owner = _lock_owner(lock_path)
    if owner.get("pid") == os.getpid() and owner.get("host") == socket.gethostname():
        return
    raise RunLockedError("refusing to write %s: writer lock held by pid %s on %s"
                         % (Path(run_dir).name, owner.get("pid", "?"), owner.get("host", "?")))


# ------------------------------------------------------------ writer


def save_checkpoint(run_dir, state, new_events):
    """Commit ``new_events`` and ``state`` to ``run_dir``.

    Contract (raises ``ValueError`` before touching disk otherwise):

    * ``state["last_event_seq"]`` already equals the previous committed seq
      plus ``len(new_events)`` — the caller owns that field;
    * ``new_events`` continue the sequence contiguously, carry the state's
      ``run_id``, and have non-decreasing ticks no later than the state's;
    * ``state["clock"]["tick"]`` never goes backwards.

    Neither argument is modified.  If another live process holds the run's
    writer lock, ``RunLockedError`` is raised.
    """
    run_dir = Path(run_dir)
    new_events = list(new_events or [])
    tick = _check_state(state)
    run_id = state["run_id"]

    state_path = run_dir / STATE_FILE
    previous_seq, previous_tick = 0, 0
    if state_path.exists():
        previous = _read_json(state_path, "committed state")
        if not isinstance(previous, dict) or previous.get("run_id") != run_id:
            raise ValueError("run directory %s belongs to run %r, not %r"
                             % (run_dir, previous.get("run_id") if isinstance(previous, dict)
                                else None, run_id))
        previous_seq = previous.get("last_event_seq", 0)
        previous_tick = _tick_of(previous)
    if tick < previous_tick:
        raise ValueError("tick %d is earlier than committed tick %d" % (tick, previous_tick))
    _check_events(new_events, run_id, previous_seq + 1, tick)
    if state["last_event_seq"] != previous_seq + len(new_events):
        raise ValueError("state['last_event_seq'] is %d but committed %d + %d new events = %d"
                         % (state["last_event_seq"], previous_seq, len(new_events),
                            previous_seq + len(new_events)))

    # 1. Serialize everything first; a TypeError here leaves the disk untouched.
    event_text = "".join(_dumps_event(e) + "\n" for e in new_events).encode("utf-8")
    state_text = dumps_json(state)

    run_dir.mkdir(parents=True, exist_ok=True)
    _assert_not_foreign_locked(run_dir)
    (run_dir / SNAPSHOT_DIR).mkdir(exist_ok=True)

    # 2. Drop an uncommitted tail left by a crash between steps 3 and 6.
    events_path = run_dir / EVENTS_FILE
    existing, offsets, _torn = _scan_events(events_path)
    committed = _committed(existing, previous_seq, run_id, str(events_path))
    keep_bytes = offsets[previous_seq - 1] if previous_seq else 0
    if events_path.exists() and events_path.stat().st_size != keep_bytes:
        with open(events_path, "r+b") as handle:
            handle.truncate(keep_bytes)
            handle.flush()
            os.fsync(handle.fileno())

    # 3. Append new events durably.
    with open(events_path, "ab") as handle:
        handle.write(event_text)
        handle.flush()
        os.fsync(handle.fileno())

    # 4-5. Snapshot and report.
    atomic_write_text(snapshot_path(run_dir, tick), state_text)
    recent = (committed + new_events)[-REPORT_EVENT_WINDOW:]
    atomic_write_text(run_dir / REPORT_FILE, render_report(state, recent))

    # 6. Commit.
    atomic_write_text(state_path, state_text)


# ------------------------------------------------------------ readers


def load_checkpoint(run_dir):
    """Return the committed state of ``run_dir`` (read-only).

    Raises ``CheckpointError`` if the run is missing or incomplete: no or
    unreadable ``state.json``, unsupported schema, fewer committed events
    than ``last_event_seq``, broken sequence, or no snapshot for the
    committed tick.  Uncommitted trailing event lines are ignored.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise CheckpointError("no run directory at %s" % run_dir)
    state = _read_json(run_dir / STATE_FILE, "state.json")
    try:
        tick = _check_state(state)
    except ValueError as exc:
        raise CheckpointError("state.json is incomplete: %s" % exc)
    events, _offsets, _torn = _scan_events(run_dir / EVENTS_FILE)
    _committed(events, state["last_event_seq"], state["run_id"], str(run_dir / EVENTS_FILE))
    if not snapshot_path(run_dir, tick).exists():
        raise CheckpointError("incomplete checkpoint: no snapshot for committed tick %d in %s"
                              % (tick, run_dir))
    return state


def load_events(run_dir, at=None):
    """Committed events of ``run_dir`` in order, optionally only ``tick <= at``."""
    state = load_checkpoint(run_dir)
    events, _offsets, _torn = _scan_events(Path(run_dir) / EVENTS_FILE)
    committed = events[:state["last_event_seq"]]
    if at is not None:
        committed = [e for e in committed if e.get("tick", 0) <= at]
    return committed


def load_snapshot(run_dir, tick):
    """Return the snapshot dict for ``tick`` (read-only)."""
    snapshot = _read_json(snapshot_path(run_dir, tick), "snapshot for tick %d" % tick)
    if not isinstance(snapshot, dict):
        raise CheckpointError("snapshot for tick %d is not an object" % tick)
    return snapshot


def inspect_run(run_dir):
    """Diagnostics for a run directory; never raises for missing pieces."""
    run_dir = Path(run_dir)
    info = {"run_dir": str(run_dir), "exists": run_dir.is_dir(), "committed_seq": None,
            "tick": None, "status": None, "event_lines": 0, "uncommitted_lines": 0,
            "torn_tail": False, "snapshots": [], "locked_by": None, "error": None}
    if not info["exists"]:
        info["error"] = "no run directory"
        return info
    info["snapshots"] = list_snapshot_ticks(run_dir)
    lock_path = run_dir / LOCK_FILE
    if lock_path.exists():
        info["locked_by"] = _lock_owner(lock_path) or {"pid": None}
    try:
        events, _offsets, torn = _scan_events(run_dir / EVENTS_FILE)
        info["event_lines"], info["torn_tail"] = len(events), torn
        state = load_checkpoint(run_dir)
        info.update(committed_seq=state["last_event_seq"], tick=_tick_of(state),
                    status=state.get("status"),
                    uncommitted_lines=len(events) - state["last_event_seq"])
    except CheckpointError as exc:
        info["error"] = str(exc)
    return info
