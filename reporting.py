"""Pure text rendering of a simulation state.

``render_report(state, recent_events)`` turns a JSON-serializable state
dict (as written to ``sim/runs/<run_id>/state.json``) into the plain-text
``report.txt`` view.  It performs no I/O, reads no clock, never mutates its
arguments, and returns identical text for identical input.

The renderer is deliberately tolerant: a missing optional field is shown as
"-" instead of raising, so the report remains usable while the engine's
schema evolves.  Collections may be given either as ``{id: record}`` dicts
or as lists of records carrying an ``id`` field.
"""

import re

DASH = "-"
RECENT_DECISIONS = 12
RECENT_EVENTS = 12

__all__ = ["render_report", "format_event", "natural_key"]


# ---------------------------------------------------------------- helpers


def natural_key(text):
    """Sort key so that ``O2`` sorts before ``O10``."""
    parts = re.split(r"(\d+)", str(text))
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts]


def _records(collection):
    """Return ``[(id, record), ...]`` sorted naturally by id."""
    if not collection:
        return []
    if isinstance(collection, dict):
        items = [(str(k), v if isinstance(v, dict) else {"value": v})
                 for k, v in collection.items()]
    else:
        items = []
        for index, record in enumerate(collection):
            if isinstance(record, dict):
                items.append((str(record.get("id", index)), record))
    return sorted(items, key=lambda kv: natural_key(kv[0]))


def _fmt(value):
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value) if value else DASH
    return str(value)


def _first(record, *keys):
    """First present, non-None value among ``keys``."""
    for key in keys:
        if isinstance(record, dict) and record.get(key) is not None:
            return record[key]
    return None


def _yes_no_pending(value):
    if value is True:
        return "yes"
    if value is False:
        return "NO"
    return "pending"


def _table(headers, rows):
    """Render a left-aligned text table."""
    rows = [[_fmt(c) if not isinstance(c, str) else (c or DASH) for c in row]
            for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(),
             "  " + "  ".join("-" * w for w in widths)]
    for row in rows:
        lines.append("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return lines


def _section(title):
    return ["", "== %s ==" % title]


def _tick(state):
    clock = state.get("clock")
    if isinstance(clock, dict):
        return _first(clock, "tick", "time")
    return _first(state, "tick", "time", "clock")


def _deadline(state):
    target = state.get("target")
    if isinstance(target, dict) and target.get("deadline") is not None:
        return target["deadline"]
    return state.get("deadline")


def _target_text(state):
    target = state.get("target")
    if isinstance(target, dict):
        text = _first(target, "text", "description")
        if text is not None:
            return text
        # The orchestrator keeps the target's wording in the frozen config.
        config = state.get("config")
        frozen = config.get("target") if isinstance(config, dict) else None
        return _first(frozen, "text", "description") if isinstance(frozen, dict) else None
    return target


def _items_text(order):
    items = _first(order, "items", "requested")
    if not items:
        return DASH
    parts = []
    for item in items if isinstance(items, list) else [items]:
        if not isinstance(item, dict):
            parts.append(_fmt(item))
            continue
        name = _first(item, "item_id", "dish", "name", "recipe", "recipe_id") or "?"
        qty = _first(item, "qty", "quantity")
        mods = item.get("modifiers") or []
        text = ("%sx %s" % (_fmt(qty), name)) if qty is not None else str(name)
        if mods:
            text += " [%s]" % ", ".join(str(m) for m in mods)
        parts.append(text)
    return "; ".join(parts)


# ---------------------------------------------------------------- events


def format_event(event):
    """One-line, human-readable summary of an event (pure)."""
    if not isinstance(event, dict):
        return _fmt(event)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    head = "#%s t=%s %s" % (_fmt(event.get("seq")), _fmt(event.get("tick")),
                            _fmt(event.get("type")))
    refs = []
    for key in ("worker_id", "task_id", "order_id"):
        value = event.get(key) if event.get(key) is not None else payload.get(key)
        if value is not None:
            refs.append("%s=%s" % (key.replace("_id", ""), value))
    detail = []
    proposal = payload.get("proposal")
    if isinstance(proposal, dict):
        detail.append("%s %s" % (_fmt(proposal.get("action")), _fmt(proposal.get("task_id"))))
        if proposal.get("why"):
            detail.append('why="%s"' % proposal["why"])
    result = payload.get("result")
    if isinstance(result, dict):
        if result.get("accepted") is True:
            detail.append("ACCEPTED")
        elif result.get("accepted") is False:
            detail.append("REJECTED: %s" % _fmt(result.get("reason")))
    for key in ("reason", "message", "error"):
        if key in payload and not isinstance(payload[key], (dict, list)):
            detail.append("%s=%s" % (key, payload[key]))
    line = head
    if refs:
        line += " " + " ".join(refs)
    if detail:
        line += " | " + " ".join(detail)
    return line


# ---------------------------------------------------------------- sections


def _header(state):
    tick, deadline = _tick(state), _deadline(state)
    lines = ["RESTAURANT OPERATIONS REPORT",
             "Run:     %s" % _fmt(state.get("run_id")),
             "Target:  %s" % _fmt(_target_text(state)),
             "Time:    minute %s of %s" % (_fmt(tick), _fmt(deadline)),
             "Mode:    %s%s" % (_fmt(state.get("mode")),
                                (" (model: %s)" % state["model"]) if state.get("model") else ""),
             "Status:  %s" % _fmt(state.get("status")).upper(),
             "Version: state %s, last committed event #%s" % (
                 _fmt(state.get("state_version")), _fmt(state.get("last_event_seq")))]
    if state.get("scenario"):
        lines.insert(3, "Scenario: %s" % state["scenario"])
    pause = state.get("pause")
    if pause:
        if isinstance(pause, dict):
            lines.append("PAUSED:  %s" % _fmt(_first(pause, "reason", "message")))
            error = pause.get("error")
            if error:
                lines.append("  error: %s" % (
                    _fmt(_first(error, "message", "type")) if isinstance(error, dict) else _fmt(error)))
            for key in ("worker_id", "attempts", "model"):
                if pause.get(key) is not None:
                    lines.append("  %s: %s" % (key, _fmt(pause[key])))
        else:
            lines.append("PAUSED:  %s" % _fmt(pause))
    return lines


def _results(state):
    orders = _records(state.get("orders"))
    total = len(orders)
    done = sum(1 for _, o in orders if o.get("status") in ("completed", "verified", "done"))
    correct = sum(1 for _, o in orders if o.get("correct") is True)
    incorrect = sum(1 for _, o in orders if o.get("correct") is False)
    on_time = sum(1 for _, o in orders if o.get("on_time") is True)
    late = sum(1 for _, o in orders if o.get("on_time") is False)
    lines = _section("Results")
    lines.append("  Orders finished:  %d / %d" % (done, total))
    lines.append("  Correct:          %d   (incorrect: %d)" % (correct, incorrect))
    lines.append("  On time:          %d   (late: %d)" % (on_time, late))
    status = state.get("status")
    if status in ("completed", "timed_out"):
        met = total > 0 and correct == total and on_time == total
        lines.append("  Target met:       %s" % ("YES" if met else "NO"))
    else:
        lines.append("  Target met:       not decided yet (status: %s)" % _fmt(status))
    return lines


def _orders_and_tasks(state):
    orders = _records(state.get("orders"))
    tasks = _records(state.get("tasks"))
    lines = _section("Orders")
    if not orders:
        lines.append("  (no orders admitted)")
    else:
        rows = []
        for oid, order in orders:
            order_tasks = [t for _, t in tasks if t.get("order_id") == oid]
            finished = sum(1 for t in order_tasks if t.get("status") in ("done", "completed"))
            rows.append([oid, _items_text(order),
                         _fmt(_first(order, "arrival", "arrival_tick", "arrives_at")),
                         _fmt(order.get("deadline")), _fmt(order.get("status")),
                         "%d/%d" % (finished, len(order_tasks)) if order_tasks else DASH,
                         _yes_no_pending(order.get("correct")),
                         _yes_no_pending(order.get("on_time"))])
        lines += _table(["order", "items", "arrive", "due", "status", "tasks",
                         "correct", "on_time"], rows)

    lines += _section("Tasks")
    if not tasks:
        lines.append("  (no tasks)")
        return lines
    rows = []
    for tid, task in tasks:
        rows.append([tid, _fmt(task.get("status")),
                     _fmt(_first(task, "owner", "worker_id", "assigned_to")),
                     _fmt(task.get("station_id")),
                     _fmt(_first(task, "started_at", "start")),
                     _fmt(_first(task, "finishes_at", "finish_at", "ends_at")),
                     _fmt(_first(task, "finished_at", "completed_at"))])
    lines += _table(["task", "status", "worker", "station", "start", "due_done", "done_at"], rows)
    return lines


def _stations(state):
    stations = _records(state.get("stations"))
    tasks = dict(_records(state.get("tasks")))
    tick = _tick(state)
    lines = _section("Stations")
    if not stations:
        lines.append("  (no stations)")
        return lines
    rows = []
    for sid, station in stations:
        active = station.get("active_tasks")
        if active is None:
            active = station.get("current_tasks")
        if active is None:
            active = [tid for tid, t in tasks.items()
                      if t.get("station_id") == sid and t.get("status") in ("in_progress", "running")]
        active = list(active or [])
        workers = station.get("workers")
        if workers is None:
            workers = [_first(tasks.get(t, {}), "owner", "worker_id") for t in active]
        capacity = _first(station, "capacity", "slots")
        # Busy minutes from task timings: finished tasks plus elapsed time of running ones.
        busy = 0
        for t in tasks.values():
            if t.get("station_id") != sid:
                continue
            start = _first(t, "started_at", "start")
            end = _first(t, "finished_at", "completed_at")
            if end is None and t.get("status") in ("in_progress", "running"):
                end = tick
            if isinstance(start, int) and isinstance(end, int) and end >= start:
                busy += end - start
        util = DASH
        if isinstance(capacity, int) and capacity > 0 and isinstance(tick, int) and tick > 0:
            util = "%d%%" % round(100.0 * busy / (capacity * tick))
        rows.append([sid, "%d/%s" % (len(active), _fmt(capacity)), _fmt(active),
                     _fmt([w for w in workers if w]), str(busy), util])
    lines += _table(["station", "in_use", "active_tasks", "workers", "busy_min", "util"], rows)
    return lines


def _stock(state):
    ingredients = _records(state.get("ingredients"))
    lines = _section("Stock")
    if not ingredients:
        lines.append("  (no ingredients)")
        return lines
    rows = [[iid, _fmt(rec.get("available")), _fmt(rec.get("reserved")), _fmt(rec.get("unit"))]
            for iid, rec in ingredients]
    lines += _table(["ingredient", "available", "reserved", "unit"], rows)
    return lines


def _workers(state):
    workers = _records(state.get("workers"))
    lines = _section("Workers")
    if not workers:
        lines.append("  (no workers)")
        return lines
    rows = []
    for wid, worker in workers:
        skills = worker.get("skills")
        if isinstance(skills, dict):
            skill_text = ", ".join("%s x%s" % (k, _fmt(v)) for k, v in sorted(skills.items()))
        else:
            skill_text = _fmt(skills)
        last = worker.get("last_result")
        if isinstance(last, dict):
            if last.get("accepted") is True:
                last_text = "accepted %s" % _fmt(last.get("task_id"))
            elif last.get("accepted") is False:
                last_text = "rejected: %s" % _fmt(last.get("reason"))
            else:
                last_text = _fmt(_first(last, "message", "reason", "action"))
        else:
            last_text = _fmt(last)
        rows.append([wid, _fmt(worker.get("name")), _fmt(worker.get("available")),
                     _fmt(worker.get("status")), _fmt(worker.get("current_task")),
                     skill_text or DASH, last_text])
    lines += _table(["worker", "name", "available", "status", "current", "skills",
                     "last_result"], rows)
    return lines


def _decisions(recent_events):
    decisions = [e for e in recent_events if isinstance(e, dict) and e.get("type") == "decision"]
    lines = _section("Recent decisions")
    if not decisions:
        lines.append("  (none)")
        return lines
    for event in decisions[-RECENT_DECISIONS:]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        meta = []
        if payload.get("mode"):
            meta.append(str(payload["mode"]))
        if payload.get("model"):
            meta.append(str(payload["model"]))
        if payload.get("latency_ms") is not None:
            meta.append("%sms" % _fmt(payload["latency_ms"]))
        if payload.get("attempts") is not None:
            meta.append("attempts=%s" % _fmt(payload["attempts"]))
        line = "  " + format_event(event)
        if meta:
            line += " (%s)" % ", ".join(meta)
        lines.append(line)
    return lines


def _blockers(state):
    blockers = state.get("blockers") or []
    if isinstance(blockers, dict):
        blockers = [dict(v, id=k) if isinstance(v, dict) else {"id": k, "reason": v}
                    for k, v in sorted(blockers.items(), key=lambda kv: natural_key(kv[0]))]
    lines = _section("Blockers")
    if not blockers:
        lines.append("  (none)")
        return lines
    rows = []
    for b in blockers:
        if not isinstance(b, dict):
            rows.append([DASH, DASH, _fmt(b), DASH])
            continue
        rows.append([_fmt(_first(b, "task_id", "id")),
                     _fmt(_first(b, "missing_skill", "required_skill", "skill")),
                     _fmt(_first(b, "reason", "message")),
                     _fmt(_first(b, "tick", "first_seen", "since"))])
    lines += _table(["task", "missing_skill", "reason", "since"], rows)
    return lines


def _recent_events(recent_events):
    others = [e for e in recent_events if not (isinstance(e, dict) and e.get("type") == "decision")]
    lines = _section("Recent events")
    if not others:
        lines.append("  (none)")
        return lines
    lines += ["  " + format_event(e) for e in others[-RECENT_EVENTS:]]
    return lines


# ---------------------------------------------------------------- public


def render_report(state, recent_events=None):
    """Render ``state`` and the given recent events as plain text.

    Pure: no I/O, no clock, no mutation of the arguments.
    """
    if not isinstance(state, dict):
        raise TypeError("state must be a dict, got %s" % type(state).__name__)
    events = list(recent_events or [])
    lines = []
    lines += _header(state)
    lines += _results(state)
    lines += _orders_and_tasks(state)
    lines += _stations(state)
    lines += _stock(state)
    lines += _workers(state)
    lines += _decisions(events)
    lines += _blockers(state)
    lines += _recent_events(events)
    return "\n".join(lines) + "\n"
