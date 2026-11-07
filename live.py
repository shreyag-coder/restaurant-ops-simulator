"""Interactive control of a running simulation (kitchen-manager console).

The orchestrator stays the only writer: this module only *queues* operator
commands on the engine (``engine.queue_command``) and advances it one minute
at a time with ``engine.run(1, checkpoint)``.  Queued commands take effect at
the start of the next simulated minute and are recorded as events, so they
are saved in checkpoints, shown in the story, and replayable.

Type plain English while the clock runs, for example::

    the fryer is broken        fryer fixed
    dana is out                dana is back
    out of patty               restock patty 5        stock cheese 3
    say rush on O7, please     status    pause    step    go    quit
    there is a fire alarm      all clear
    power outage               power is back
    gas leak                   gas leak fixed

Anything else goes to the scenario agent (scenario_agent.py), which proposes
kitchen changes for the engine to validate; without credentials it becomes a
message to the workers.
"""

import queue
import re
import sys
import threading
import time

TERMINAL = {"completed", "timed_out"}

HELP = """\
Kitchen manager commands (type any time, press Enter):
  <station> broken | fix <station>    e.g. "the fryer is broken", "fryer fixed"
  <worker> is out  | <worker> is back e.g. "dana is out", "dana is back"
  out of <ingredient>                 e.g. "out of patty"
  stock <ingredient> <n>              set stock, e.g. "stock cheese 3"
  restock <ingredient> <n>            add stock, e.g. "restock patty 5"
  say <message>                       tell every worker, e.g. "say rush on O7"
  fire alarm | all clear              every station stops until the all clear
  power outage | power is back        every station stops until power returns
  gas leak | gas leak fixed           grill and fryer stop until it's fixed
  anything else                       the scenario agent (AI) turns it into kitchen changes,
                                      e.g. "Dana burned her hand", "the fryer oil is old"
  status   pause   go   step   speed <seconds>   help   quit
Changes take effect at the start of the next simulated minute."""

_DOWN = {"break", "broke", "broken", "down", "dead", "failed", "fail", "faulty",
         "offline", "disable", "disabled", "stop", "stopped", "out", "kaput"}
_UP = {"fix", "fixed", "repair", "repaired", "working", "works", "up", "online",
       "back", "enable", "enabled", "restore", "restored", "ok", "okay", "fine"}
_AWAY = {"out", "away", "sick", "left", "leave", "leaves", "gone", "off", "absent",
         "unavailable", "break", "home", "remove"}
_BACK = {"back", "return", "returns", "returned", "available", "in", "here", "arrives",
         "arrived"}
# Kitchen-wide incidents: phrases that start/end them and the stations they stop.
# "all" means every station.  End phrases are checked first ("fire alarm off").
INCIDENTS = {
    "fire_alarm": {
        "start": ["fire alarm", "evacuate", "evacuation"],
        "end": ["all clear", "alarm off", "alarm is off", "alarm over", "alarm is over",
                "alarm stopped", "fire is out", "fire out", "fire over", "fire is over",
                "no more fire", "false alarm", "alarm fixed", "alarm is fixed", "alarm is fixex",
                "alarm cleared", "alarm is cleared", "alarm ended", "back to work"],
        "stations": "all",
        "start_text": "Fire alarm! Stop work, leave your station and evacuate until the all clear.",
        "end_text": "All clear, the fire alarm is over. Back to your stations.",
    },
    "power_outage": {
        "start": ["power outage", "power cut", "blackout", "no power", "power is out",
                  "power out", "power failure"],
        "end": ["power is back", "power back", "power restored", "power on",
                "power is on", "electricity is back"],
        "stations": "all",
        "start_text": "Power outage! No equipment works until power is back.",
        "end_text": "Power is back. Equipment is working again.",
    },
    "gas_leak": {
        "start": ["gas leak", "smell gas"],
        "end": ["gas leak fixed", "gas fixed", "leak fixed", "gas is fixed", "gas is back",
                "gas back", "gas safe", "gas is safe", "gas leak over"],
        "stations": ["grill", "fryer"],
        "start_text": "Gas leak! Grill and fryer are shut off until it's fixed.",
        "end_text": "Gas leak fixed. Grill and fryer are back on.",
    },
}

_ADD = {"restock", "add", "deliver", "delivery", "delivered", "more", "plus"}
_CONTROL = {
    "help": "help", "?": "help", "h": "help",
    "status": "status", "s": "status", "state": "status",
    "pause": "pause", "p": "pause", "hold": "pause",
    "go": "go", "resume": "go", "continue": "go", "play": "go", "c": "go", "run": "go",
    "step": "step", "n": "step", "next": "step",
    "quit": "quit", "exit": "quit", "q": "quit", "stop": "pause",
}


def _words(text):
    return re.findall(r"[a-z_]+|[+-]?\d+", text.lower())


def _aliases(records, extra_fields=()):
    """Map lowercase phrases -> id, longest phrases first."""
    table = {}
    for rid, rec in (records or {}).items():
        table[rid.lower()] = rid
        for field in ("name",) + tuple(extra_fields):
            value = rec.get(field) if isinstance(rec, dict) else None
            if isinstance(value, str) and value.strip():
                table.setdefault(value.lower(), rid)
    return sorted(table.items(), key=lambda kv: -len(kv[0]))


def _find(text, aliases, words):
    """First alias whose phrase appears in text (word-bounded); also plurals."""
    for phrase, rid in aliases:
        if " " in phrase or "_" in phrase:
            if re.search(r"\b%s\b" % re.escape(phrase), text):
                return rid
        elif phrase in words or phrase + "s" in words or phrase + "es" in words:
            return rid
    return None


def _station_aliases(state):
    table = dict(_aliases(state.get("stations")))
    for phrase, rid in list(table.items()):
        first = phrase.split()[0]
        if first not in ("the",):
            table.setdefault(first, rid)  # "prep counter" -> "prep"
    if "fryer" in (state.get("stations") or {}):
        table.setdefault("frier", "fryer")
    return sorted(table.items(), key=lambda kv: -len(kv[0]))


def _ingredient_aliases(state):
    table = dict(_aliases(state.get("ingredients")))
    for phrase, rid in list(table.items()):
        last = phrase.split()[-1]
        table.setdefault(last, rid)  # "beef patty" -> "patty"
    known = state.get("ingredients") or {}
    for plural, rid in (("patties", "patty"), ("potatoes", "potato"), ("fries", "potato")):
        if rid in known:
            table.setdefault(plural, rid)
    return sorted(table.items(), key=lambda kv: -len(kv[0]))


def parse(text, state):
    """Parse one line.  Returns ("control", name, arg) or ("command", dict).

    Raises ValueError with a helpful message when the line is not understood.
    """
    raw = text.strip()
    low = raw.lower()
    words = _words(raw)
    if not words:
        raise ValueError("empty command")

    if words[0] in ("say", "announce", "tell", "note", "msg", "message"):
        message = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        if not message.strip():
            raise ValueError("say what? e.g. 'say rush on O7'")
        return ("command", {"type": "announce", "text": message.strip()})

    if words[0] == "speed" or words[0] == "pace":
        match = re.search(r"(\d+(?:\.\d+)?)", raw)
        if not match:
            raise ValueError("speed needs seconds per minute, e.g. 'speed 1'")
        return ("control", "speed", float(match.group(1)))

    if len(words) == 1 and words[0] in _CONTROL:
        return ("control", _CONTROL[words[0]], None)

    incident = _match_incident(low, state)
    if incident:
        return ("command", incident)

    numbers = [int(w) for w in words if re.match(r"[+-]?\d+$", w)]
    ingredient = _find(low, _ingredient_aliases(state), words)
    worker = _find(low, _aliases(state.get("workers"), ("role",)), words)
    station = _find(low, _station_aliases(state), words)
    wordset = set(words)

    # Stock: "out of patty", "no cheese", "restock patty 5", "stock bun 3", "patty +4".
    if ingredient and (numbers or "out" in wordset and "of" in wordset
                       or words[0] in ("no", "stock", "restock", "set")
                       or "ran" in wordset or "empty" in wordset):
        if not numbers:
            return ("command", {"type": "stock", "ingredient_id": ingredient, "mode": "set", "amount": 0})
        amount = numbers[-1]
        signed = re.search(r"[+-]\d+", raw) is not None
        if wordset & _ADD or signed:
            return ("command", {"type": "stock", "ingredient_id": ingredient, "mode": "add", "amount": amount})
        return ("command", {"type": "stock", "ingredient_id": ingredient, "mode": "set", "amount": amount})

    # A worker phrase ("grill cook", "dana") wins over a station word inside it.
    if worker:
        if wordset & _BACK:
            return ("command", {"type": "worker", "worker_id": worker, "available": True})
        if wordset & _AWAY:
            return ("command", {"type": "worker", "worker_id": worker, "available": False})
        if len(words) == 1:
            raise ValueError("is %s out or back? e.g. '%s is out'" % (worker, worker))
        return ("command", {"type": "announce", "text": raw, "free_text": True})

    if station:
        if wordset & _UP and not ({"not"} & wordset):
            return ("command", {"type": "station", "station_id": station, "available": True})
        if wordset & _DOWN or "not" in wordset:
            return ("command", {"type": "station", "station_id": station, "available": False})
        if len(words) == 1:
            raise ValueError("is the %s broken or fixed? e.g. '%s broken'" % (station, station))
        return ("command", {"type": "announce", "text": raw, "free_text": True})

    # Not a change the engine knows: pass it on to the workers as a message.
    return ("command", {"type": "announce", "text": raw, "free_text": True})


def _match_incident(low, state):
    for which in ("end", "start"):
        for name, spec in INCIDENTS.items():
            for phrase in spec[which]:
                if re.search(r"\b%s\b" % re.escape(phrase), low):
                    stations = sorted(state.get("stations") or {}) if spec["stations"] == "all" \
                        else [sid for sid in spec["stations"] if sid in (state.get("stations") or {})]
                    return {"type": "incident", "name": name, "active": which == "start",
                            "station_ids": stations, "text": spec[which + "_text"]}
    return None


def describe(command, state):
    """Short confirmation text for a queued command."""
    kind = command["type"]
    when = "at minute %s" % state.get("next_tick", "?")
    if kind == "station":
        return "%s will be %s %s" % (command["station_id"],
                                     "FIXED" if command["available"] else "BROKEN", when)
    if kind == "worker":
        name = (state.get("workers", {}).get(command["worker_id"]) or {}).get("name") or command["worker_id"]
        return "%s will be %s %s" % (name, "BACK" if command["available"] else "OUT", when)
    if kind == "stock":
        verb = "set to" if command["mode"] == "set" else "changed by"
        return "%s stock will be %s %d %s" % (command["ingredient_id"], verb, command["amount"], when)
    if kind == "station_capacity":
        return "%s capacity set to %d %s" % (command["station_id"], command["capacity"], when)
    if kind == "station_speed":
        return "%s works at x%g time %s" % (command["station_id"], command["multiplier"], when)
    if kind == "worker_skill":
        name = (state.get("workers", {}).get(command["worker_id"]) or {}).get("name") or command["worker_id"]
        if command.get("remove"):
            return "%s can no longer do %s %s" % (name, command["skill"], when)
        return "%s's %s work takes x%g time %s" % (name, command["skill"], command["multiplier"], when)
    if kind == "add_order":
        items = ", ".join("%d %s%s" % (i["quantity"], i["recipe"],
                                       (" (%s)" % ", ".join(i["modifiers"])) if i["modifiers"] else "")
                          for i in command["items"])
        return "new order %s: %s, due by minute %d, arrives %s" % (
            command["order_id"], items, command["deadline_minutes"], when)
    if kind == "order_deadline":
        return "%s is now due by minute %d" % (command["order_id"], command["deadline_minutes"])
    if kind == "announce":
        return 'message to workers: "%s"' % command["text"]
    if kind == "incident":
        label = command["name"].replace("_", " ")
        if command["active"]:
            stations = ", ".join(command["station_ids"]) or "no stations"
            return "%s starts %s (out of service: %s)" % (label.upper(), when, stations)
        return "%s ends %s" % (label, when)
    return "announcement goes out %s" % when


def _stdin_reader(inbox, stream):
    try:
        for line in stream:
            inbox.put(line.rstrip("\n"))
    except (OSError, ValueError):
        pass
    inbox.put(None)  # end of input


def _use_scenario_agent(engine, text, interpret, out):
    """Ask the scenario agent to turn ``text`` into kitchen changes and queue them."""
    out(">> Scenario agent is reading that...")
    try:
        result = interpret(text, engine.state)
    except Exception as exc:  # ScenarioError or anything unexpected: say so, change nothing
        out(">> Scenario agent failed (%s). Nothing was changed. Try rephrasing, or use a "
            "built-in command (type 'help')." % str(exc)[:200])
        return
    note = {"text": text, "summary": result.get("summary"), "model": result.get("model"),
            "latency_ms": result.get("latency_ms")}
    accepted, rejected = engine.queue_batch(result.get("changes") or [], note=note)
    out(">> Scenario agent (%s, %.1fs): %s" % (result.get("model"), (result.get("latency_ms") or 0) / 1000.0,
                                               result.get("summary") or "(no summary)"))
    for command in accepted:
        why = command.get("why")
        out("   + %s%s" % (describe(command, engine.state), ("  [%s]" % why) if why else ""))
    for item in rejected:
        cmd = item["command"]
        out("   x rejected %s: %s" % (cmd.get("type"), item["error"]))
    if not accepted:
        out("   (no kitchen changes)")


def interactive(engine, checkpoint, pace=2.0, inbox=None, out=print, render_status=None,
                interpret=None):
    """Advance ``engine`` one minute every ``pace`` seconds while reading
    manager commands from ``inbox`` (a queue of lines; stdin by default).

    ``interpret(text, state)`` is the optional scenario agent: free text that
    is not a built-in command is sent to it, and its proposed changes are
    validated by the engine.  Without it, free text becomes a message.

    Returns "finished", "quit", or "detached" (input ended while paused).
    """
    if inbox is None:
        inbox = queue.Queue()
        threading.Thread(target=_stdin_reader, args=(inbox, sys.stdin), daemon=True).start()
    out(HELP)
    out("Clock: 1 simulated minute every %gs. Type 'pause' to stop the clock.\n" % pace)
    paused, steps, eof = False, 0, False

    def handle(line):
        nonlocal paused, steps, pace
        if not line.strip():
            if paused:
                steps += 1
                return "step"
            return None
        try:
            parsed = parse(line, engine.state)
        except ValueError as exc:
            out(">> " + str(exc))
            return None
        if parsed[0] == "control":
            name, arg = parsed[1], parsed[2]
            if name == "help":
                out(HELP)
            elif name == "status":
                label = "clock paused" if paused else "clock running"
                out(render_status(engine.state, label) if render_status else str(engine.state.get("time")))
            elif name == "pause":
                paused = True
                out(">> Paused at minute %s. 'step' (or Enter) = one minute, 'go' = continue."
                    % engine.state.get("time"))
            elif name == "go":
                paused = False
                out(">> Clock running.")
                return "go"
            elif name == "step":
                paused = True
                steps += 1
                return "step"
            elif name == "speed":
                pace = max(0.0, arg)
                out(">> Clock: 1 simulated minute every %gs." % pace)
            elif name == "quit":
                return "quit"
            return None
        if parsed[1].get("free_text") and interpret is not None:
            _use_scenario_agent(engine, parsed[1]["text"], interpret, out)
            return None
        try:
            queued = engine.queue_command(parsed[1])
        except ValueError as exc:
            out(">> Can't do that: %s" % exc)
            return None
        if parsed[1].get("free_text"):
            out(">> Scenario agent is off (needs OPENROUTER_API_KEY and OPENROUTER_MODEL), so I'm only "
                "telling the workers: \"%s\". It doesn't change equipment, staff or stock." % queued["text"])
        else:
            out(">> OK: " + describe(queued, engine.state))
        return None

    try:
        while engine.state["status"] not in TERMINAL:
            wait_until = None
            if not paused or steps > 0:
                steps = max(0, steps - 1)
                started = time.monotonic()
                engine.run(1, checkpoint)
                if engine.state["status"] in TERMINAL:
                    break
                if engine.state.get("pause_reason") == "decision_error":
                    out(">> AI decision failed; clock paused. Type 'go' to retry or 'quit'.")
                    paused = True
                wait_until = started + pace
            while True:
                if paused and steps == 0:
                    if eof:
                        return "detached"
                    timeout = None
                else:
                    timeout = max(0.0, (wait_until or 0) - time.monotonic())
                try:
                    line = inbox.get(timeout=timeout)
                except queue.Empty:
                    break
                if line is None:
                    eof = True
                    continue
                action = handle(line)
                if action == "quit":
                    if engine.state.get("operator_queue"):
                        out(">> Note: %d queued command(s) were not applied yet."
                            % len(engine.state["operator_queue"]))
                    return "quit"
                if action in ("step", "go"):
                    break
        return "finished"
    except KeyboardInterrupt:
        out("\n>> Stopped (Ctrl-C). Progress up to the last finished minute is saved.")
        return "quit"
