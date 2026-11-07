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

__all__ = ["render_report", "format_event", "natural_key", "render_story", "story_header", "narrate_events", "render_status"]


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



# ---------------------------------------------------------------- story
#
# A conversational, first-person narration of the event log, e.g.
#
#   --- Minute 2 ---
#   Kitchen: New order O4 came in: 2 fries. Due by minute 15.
#   Dana (Grill cook): I'm free, so I'm grilling the patty for O3's burger. Ready at minute 4.
#       thinking: "It's my only ready task, it's on my grill ..."
#
# Built only from committed events plus names/recipes in the state, so it is
# pure and works identically live (per checkpoint) and in replay.

_STEP_WORDS = {
    # step_id: (infinitive, -ing, past)
    "patty": ("grill the patty", "grilling the patty", "grilled the patty"),
    "toppings": ("prep the toppings", "prepping the toppings", "prepped the toppings"),
    "assemble": ("assemble", "assembling", "assembled"),
    "fry": ("fry", "frying", "fried"),
    "pack": ("pack", "packing", "packed"),
    "verify": ("check", "checking", "checked"),
}

_REASON_WORDS = {
    "Station is unavailable or full": "the {station} is full right now",
    "Ingredients are unavailable": "we're out of the ingredients for it",
    "Prerequisites are incomplete": "the earlier steps aren't finished yet",
    "Worker lacks required skill": "I don't have the skill for it",
}


def _speaker(state, worker_id):
    worker = (state.get("workers") or {}).get(worker_id) if isinstance(state.get("workers"), dict) else None
    if not isinstance(worker, dict):
        return str(worker_id or "Someone")
    name = worker.get("name") or worker_id
    return "%s (%s)" % (name, worker["role"]) if worker.get("role") else str(name)


def _name(state, worker_id):
    workers = state.get("workers") if isinstance(state.get("workers"), dict) else {}
    worker = workers.get(worker_id)
    return (worker.get("name") if isinstance(worker, dict) else None) or str(worker_id)


def _plural(recipe, qty):
    if qty == 1:
        return recipe
    return recipe if recipe.endswith("s") else recipe + "s"


def _order_items_phrase(order):
    parts = []
    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        recipe = str(_first(item, "recipe", "recipe_id", "item_id", "dish", "name") or "item")
        qty = _first(item, "quantity", "qty") or 1
        text = "%s %s" % (qty, _plural(recipe, qty))
        mods = [str(m).replace("_", " ") for m in item.get("modifiers") or []]
        if mods:
            text += " (%s)" % ", ".join(mods)
        parts.append(text)
    if not parts:
        return "an order"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _item_phrase(state, order_id, item_id):
    """"O2's burger #1" / "O3's fries" for an item id like ``O2.b1``."""
    order = (state.get("orders") or {}).get(order_id) or {}
    expected = {i.get("id"): i for i in order.get("expected_items") or [] if isinstance(i, dict)}
    item = expected.get(item_id) or {}
    recipe = item.get("recipe_id") or item.get("recipe") or "item"
    same = [i for i in expected.values() if (i.get("recipe_id") or i.get("recipe")) == recipe]
    label = "%s's %s" % (order_id, recipe)
    if len(same) > 1:
        suffix = re.sub(r"^\D*", "", str(item_id).rsplit(".", 1)[-1])
        if suffix:
            label += " #%s" % suffix
    return label


def _task_phrase(state, task_id, form):
    """("grill the patty", "for O2's burger #1") style pieces for a task."""
    task = (state.get("tasks") or {}).get(task_id) or {}
    step = task.get("step_id") or str(task_id).rsplit(".", 1)[-1]
    words = _STEP_WORDS.get(step)
    order_id = task.get("order_id") or str(task_id).split(".", 1)[0]
    if step in ("pack", "verify"):
        verb = words[form] if words else step
        return "%s order %s" % (verb, order_id)
    verb = words[form] if words else "work on"
    target = _item_phrase(state, order_id, task.get("item_id")) if task.get("item_id") else task_id
    return "%s %s" % (verb, ("for " + target) if step not in ("fry", "assemble") else target)


def _at_station(state, task_id):
    """" at the packing counter" unless the verb already names the station."""
    task = (state.get("tasks") or {}).get(task_id) or {}
    if task.get("step_id") in ("patty", "fry"):
        return ""
    return " at the " + _station_name(state, task_id)


def _station_label(state, station_id):
    station = (state.get("stations") or {}).get(station_id) or {}
    return str(station.get("name") or station_id or "station").lower()


def _station_name(state, task_id):
    task = (state.get("tasks") or {}).get(task_id) or {}
    station = (state.get("stations") or {}).get(task.get("station_id")) or {}
    return (station.get("name") or task.get("station_id") or "station").lower()


def _why(text):
    text = (text or "").strip()
    return ['    thinking: "%s"' % text] if text else []


def narrate_events(state, events, owners=None):
    """Story lines for ``events`` (pure).  ``owners`` maps task_id -> worker
    who most recently started it; it is updated in place so callers can
    narrate a log incrementally."""
    owners = {} if owners is None else owners
    events = [e for e in events if isinstance(e, dict)]
    # Announce "X is no longer available" before "X had to stop ...".
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        if (a.get("type") == "task_interrupted" and b.get("type") == "worker_availability"
                and a.get("worker_id") == b.get("worker_id") and a.get("tick") == b.get("tick")):
            events[i], events[i + 1] = b, a
    started = {}
    produced = set()
    for e in events:
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if e.get("type") == "task_started":
            started[(e.get("tick"), p.get("task_id"))] = p
        elif e.get("type") == "item_produced":
            produced.add((e.get("seq"), p.get("item_id")))
    produced_items = {item for _, item in produced}

    lines, tick = [], object()
    deadline = _deadline(state)
    incident_of = {}  # station_id -> incident name, tracked through the events
    for e in events:
        etype = e.get("type")
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        out = []
        wid = e.get("worker_id") or p.get("worker_id")
        if etype == "order_admitted":
            order = (state.get("orders") or {}).get(p.get("order_id")) or {}
            due = _first(order, "deadline_minutes", "deadline")
            out.append("Kitchen: New order %s came in: %s.%s" % (
                p.get("order_id"), _order_items_phrase(order),
                (" Due by minute %s." % due) if due is not None else ""))
        elif etype == "task_started":
            owners[p.get("task_id")] = p.get("worker_id")
        elif etype == "decision":
            proposal = p.get("proposal") if isinstance(p.get("proposal"), dict) else {}
            result = p.get("result") if isinstance(p.get("result"), dict) else {}
            action, task_id, why = proposal.get("action"), proposal.get("task_id"), proposal.get("why")
            who = _speaker(state, wid)
            retry = "OK, instead " if p.get("round") else ""
            if action == "start_task" and result.get("accepted"):
                info = started.get((e.get("tick"), task_id)) or {}
                ready = info.get("finishes_at")
                opener = "OK, instead I'm" if retry else "I'm free, so I'm"
                out.append("%s: %s %s%s.%s" % (
                    who, opener, _task_phrase(state, task_id, 1), _at_station(state, task_id),
                    (" Ready at minute %s." % ready) if ready is not None else ""))
                out += _why(why)
            elif action == "start_task":
                reason = result.get("reason") or "it was refused"
                if reason == "Task is not pending" and owners.get(task_id) and owners[task_id] != wid:
                    because = "%s already took it" % _name(state, owners[task_id])
                else:
                    because = _REASON_WORDS.get(reason, reason[:1].lower() + reason[1:]).format(
                        station=_station_name(state, task_id))
                out.append("%s: I wanted to %s, but %s." % (who, _task_phrase(state, task_id, 0), because))
                out += _why(why)
            elif action == "wait":
                out.append("%s: I'll hold off for now." % who)
                out += _why(why)
            elif action == "report_blocker":
                out.append("%s: I'm stuck on %s and can't do it." % (who, task_id))
                out += _why(why)
            elif action == "request_help":
                out.append("%s: Can someone help with %s?" % (who, task_id))
                out += _why(why)
            else:
                out.append("%s: %s %s" % (who, action, task_id or ""))
                out += _why(why)
        elif etype == "task_completed":
            task_id = p.get("task_id")
            task = (state.get("tasks") or {}).get(task_id) or {}
            line = "%s: Done, I %s." % (_speaker(state, wid), _task_phrase(state, task_id, 2))
            item_id = task.get("item_id")
            if (task.get("step_id") not in ("pack", "verify") and item_id in produced_items
                    and task_id == next((i.get("final_task_id") for i in
                                         ((state.get("orders") or {}).get(task.get("order_id")) or {})
                                         .get("expected_items") or []
                                         if isinstance(i, dict) and i.get("id") == item_id), None)):
                label = _item_phrase(state, task.get("order_id"), item_id)
                line += " %s %s ready." % (label, "are" if label.split(" #")[0].endswith("s") else "is")
            out.append(line)
        elif etype == "order_verified":
            order = (state.get("orders") or {}).get(p.get("order_id")) or {}
            due = _first(order, "deadline_minutes", "deadline")
            if p.get("correct") and p.get("on_time"):
                verdict = "everything matches and it's on time (due %s). Order complete!" % _fmt(due)
            elif p.get("correct"):
                verdict = "it's correct, but LATE (due %s)." % _fmt(due)
            else:
                verdict = "something is WRONG with it; it fails verification."
            out.append("Kitchen: Order %s verified: %s" % (p.get("order_id"), verdict))
        elif etype == "worker_availability":
            if p.get("available"):
                out.append("Kitchen: %s is back and available." % _speaker(state, wid))
            else:
                out.append("Kitchen: %s is no longer available!" % _speaker(state, wid))
        elif etype == "task_interrupted":
            because = ""
            if p.get("reason") == "station_capacity":
                because = " because the %s has less room now" % _station_label(state, p.get("station_id"))
            elif p.get("reason") == "skill_removed":
                because = " because they can no longer do that work"
            if p.get("reason") == "station_unavailable":
                because = " because the %s went down" % _station_label(state, p.get("station_id"))
                cause = incident_of.get(p.get("station_id"))
                if cause:
                    because = " because of the %s" % cause.replace("_", " ")
            out.append("Kitchen: %s had to stop %s%s. It goes back on the board and "
                       "the ingredients are returned." % (_name(state, wid),
                                                          _task_phrase(state, p.get("task_id"), 1),
                                                          because))
        elif etype == "station_availability" and p.get("cause"):
            pass  # covered by the incident_started / incident_cleared line
        elif etype in ("incident_started", "incident_cleared"):
            for sid in p.get("station_ids") or []:
                if etype == "incident_started":
                    incident_of[sid] = p.get("name") or "incident"
                else:
                    incident_of.pop(sid, None)
            name = str(p.get("name") or "incident").replace("_", " ")
            stations = [_station_label(state, sid) for sid in p.get("station_ids") or []]
            if etype == "incident_started":
                line = "Manager: %s!" % name.upper()
                if p.get("text"):
                    line += ' "%s"' % p["text"]
                if stations:
                    line += " Out of service: %s." % ", ".join(stations)
            else:
                line = "Manager: The %s is over (started minute %s)." % (name, _fmt(p.get("since")))
                if p.get("text"):
                    line += ' "%s"' % p["text"]
                if stations:
                    line += " Back in service: %s." % ", ".join(stations)
            out.append(line)
        elif etype == "station_availability":
            label = _station_label(state, p.get("station_id"))
            if p.get("available"):
                out.append("Manager: The %s is fixed and working again." % label)
            else:
                out.append("Manager: The %s is BROKEN! Nobody can use it until it's fixed." % label)
        elif etype == "scenario_interpreted":
            line = 'Manager: "%s"' % p.get("text")
            if p.get("summary"):
                line += "  (scenario agent: %s)" % p["summary"]
            out.append(line)
            for item in p.get("rejected") or []:
                out.append("    (not applied: %s)" % item.get("error"))
        elif etype == "station_capacity_changed":
            after = p.get("after")
            out.append("Manager: The %s now handles %s %s at once (was %s)." % (
                _station_label(state, p.get("station_id")), _fmt(after), "task" if after == 1 else "tasks",
                _fmt(p.get("before"))))
        elif etype == "station_speed_changed":
            label = _station_label(state, p.get("station_id"))
            after = p.get("after")
            if after == 1:
                out.append("Manager: The %s is back to normal speed." % label)
            elif isinstance(after, (int, float)) and after > 1:
                out.append("Manager: The %s is running slow: new tasks there take %sx as long." % (label, _fmt(after)))
            else:
                out.append("Manager: The %s is running fast: new tasks there take %sx the time." % (label, _fmt(after)))
        elif etype == "worker_skill_changed":
            name, skill, after = _name(state, wid), p.get("skill"), p.get("after")
            if after is None:
                out.append("Manager: %s can no longer do %s work." % (name, skill))
            else:
                out.append("Manager: %s's %s work now takes %sx the standard time (was %sx)." % (
                    name, skill, _fmt(after), _fmt(p.get("before"))))
        elif etype == "order_added":
            order = dict((state.get("orders") or {}).get(p.get("order_id")) or {}, items=p.get("items"))
            out.append("Manager: Adding a new order %s: %s. Due by minute %s." % (
                p.get("order_id"), _order_items_phrase(order), _fmt(p.get("deadline_minutes"))))
        elif etype == "order_deadline_changed":
            out.append("Manager: Order %s is now due by minute %s (was %s)." % (
                p.get("order_id"), _fmt(p.get("after")), _fmt(p.get("before"))))
        elif etype == "stock_changed":
            out.append("Manager: %s stock changed from %s to %s." % (
                str(p.get("ingredient_id")).capitalize(), _fmt(p.get("before")), _fmt(p.get("after"))))
        elif etype == "announcement":
            out.append('Manager: "%s"' % p.get("text"))
        elif etype == "blocker":
            reason = p.get("reason") or "missing skill"
            if "out of service" in reason:
                task = (state.get("tasks") or {}).get(p.get("task_id")) or {}
                out.append("Kitchen: BLOCKED: can't %s while the %s is out of service." % (
                    _task_phrase(state, p.get("task_id"), 0), _station_label(state, task.get("station_id"))))
            else:
                out.append("Kitchen: BLOCKED: nobody available can %s (%s)." % (
                    _task_phrase(state, p.get("task_id"), 0), reason))
        elif etype == "decision_error":
            out.append("Kitchen: Couldn't get a decision from %s's AI (%s). Simulation paused." % (
                _name(state, wid), p.get("error_type") or "error"))
        elif etype == "run_finished":
            status = p.get("status")
            total = len(state.get("orders") or {}) or "?"
            if status == "completed":
                out.append("Kitchen: TARGET REACHED: %s/%s orders correct at minute %s of %s." % (
                    p.get("correct_orders"), total, e.get("tick"), _fmt(deadline)))
            else:
                out.append("Kitchen: TIME'S UP (%s): %s/%s orders correct by minute %s." % (
                    status, p.get("correct_orders"), total, e.get("tick")))
        if out:
            if e.get("tick") != tick:
                tick = e.get("tick")
                lines += ["", "--- Minute %s ---" % tick]
            lines += out
    return lines


def story_header(state):
    """Title, goal, and team roster lines for a story (pure)."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dict, got %s" % type(state).__name__)
    lines = ["KITCHEN STORY: run %s (%s mode%s)" % (
        _fmt(state.get("run_id")), _fmt(state.get("mode")),
        (", model " + state["model"]) if state.get("model") else ""),
        "Goal: %s" % _fmt(_target_text(state)), "", "The team:"]
    for wid, worker in _records(state.get("workers")):
        skills = worker.get("skills")
        skills = ", ".join(sorted(skills)) if isinstance(skills, dict) else _fmt(skills)
        lines.append("  %s: %s [%s]" % (_speaker(state, wid), _fmt(worker.get("description")), skills))
    return lines


def render_story(state, events):
    """Full conversational story of a run (pure)."""
    lines = story_header(state)
    lines += narrate_events(state, list(events or []))
    status = state.get("status")
    if status not in ("completed", "timed_out"):
        lines += ["", "(story continues: run is %s at minute %s)" % (_fmt(status), _fmt(_tick(state)))]
    return "\n".join(lines) + "\n"



def render_status(state, status_label=None):
    """Compact live status for an interactive session (pure)."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dict, got %s" % type(state).__name__)
    orders = _records(state.get("orders"))
    done = sum(1 for _, o in orders if o.get("correct") is True)
    lines = ["", "=== STATUS at minute %s of %s: %d/%d orders done (%s) ===" % (
        _fmt(_tick(state)), _fmt(_deadline(state)), done, len(orders),
        status_label or _fmt(state.get("status")))]
    tasks = dict(_records(state.get("tasks")))
    parts = []
    for sid, st in _records(state.get("stations")):
        active = st.get("active_tasks") or []
        flag = "BROKEN" if st.get("available") is False else "%d/%s" % (len(active), _fmt(st.get("capacity")))
        if st.get("duration_multiplier", 1) != 1:
            flag += " slow x%s" % _fmt(st["duration_multiplier"])
        parts.append("%s %s" % (sid, flag))
    lines.append("Stations: " + ", ".join(parts))
    for wid, w in _records(state.get("workers")):
        current = w.get("current_task")
        if w.get("available") is False:
            doing = "AWAY"
        elif current:
            doing = "%s (done at %s)" % (current, _fmt((tasks.get(current) or {}).get("finishes_at")))
        else:
            doing = "idle"
        lines.append("  %-24s %s" % (_speaker(state, wid), doing))
    stock = ["%s %s%s" % (iid, _fmt(rec.get("available")),
                          ("(+%s held)" % _fmt(rec.get("reserved"))) if rec.get("reserved") else "")
             for iid, rec in _records(state.get("ingredients"))]
    lines.append("Stock: " + ", ".join(stock))
    open_orders = ["%s(%s, due %s)" % (oid, o.get("status"), _fmt(_first(o, "deadline_minutes", "deadline")))
                   for oid, o in orders if o.get("correct") is None and o.get("status") != "scheduled"]
    lines.append("Open orders: " + (", ".join(open_orders) or "none"))
    blockers = state.get("blockers") or {}
    if blockers:
        items = blockers.items() if isinstance(blockers, dict) else enumerate(blockers)
        lines.append("Blocked: " + "; ".join("%s: %s" % (k, v) for k, v in items))
    incidents = state.get("incidents") or {}
    if incidents:
        lines.append("Incidents: " + "; ".join(
            "%s since minute %s" % (n.replace("_", " "), _fmt(r.get("since"))) for n, r in incidents.items()))
    queued = state.get("operator_queue") or []
    if queued:
        lines.append("Queued for next minute: %d command(s)" % len(queued))
    return "\n".join(lines)
