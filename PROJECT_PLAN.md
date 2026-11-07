# Restaurant Operations Simulator — Simple JSON + Text Plan

Updated: November 7, 2026. This is a plan for future work, not a claim that anything is implemented. Original requirements: [BRIEF.md](BRIEF.md).

## 1. Goal and loop

Build a small, working multi-agent kitchen simulation in about 3 hours with two people using AI coding tools.

- **JSON** for everything a program reads or writes: config, state, logs.
- **Plain text** for what people and models read: worker personas and the generated report.
- **One process, `orchestrator.py`, is the only writer of state.** Workers (AI or deterministic) only return proposals; the orchestrator validates and applies them.

Each tick (one simulated minute):

**apply disruption → admit arriving orders → complete finished tasks → detect blockers → ask idle workers → validate proposals in fixed order → apply → save state, events, snapshot, report → advance clock**

## 2. File layout

```
sim/
  config/                  # hand-written, never changed during a run
    menu.json              # ingredients + stock, stations + capacity, recipes, modifiers, pack/verify steps
    workers.json           # id, name, role, skills {skill: duration multiplier}
    scenario.json          # target, deadline (30), 10 orders, variants: baseline | disruption | blocker
  personas/<id>.txt        # background prose given to the model
  runs/<run_id>/           # one folder per run (git-ignored)
    state.json             # authoritative current state
    events.jsonl           # append-only, one JSON event per line (decisions, accepts, rejects, starts, completions, blockers)
    snapshots/tNN.json     # state copy per tick, used by replay
    report.txt             # human-readable view, regenerated every tick
orchestrator.py            # engine, policies, CLI
llm.py                     # OpenRouter call with strict JSON schema
tests/test_engine.py       # pytest
```

Python 3.9+ standard library only (`urllib` for HTTP). pytest for tests. The API key comes from `OPENROUTER_API_KEY`; the model from `OPENROUTER_MODEL`.

## 3. Engine rules

- **Clock:** integer ticks; 1 tick = 1 simulated minute. A task started at `t` with duration `d` completes at `t + d`, never earlier.
- **Tasks are generated, not reasoned about.** When an order arrives, the orchestrator expands it from the recipes, one set per unit:
  - Burger: `patty` (grill) → `toppings` (prep) → `assemble` (assembly). Fries: `fry` (fryer).
  - Per order: `pack` (needs every item's final step) → `verify` (needs pack).
  - Ids are readable: `O2.b1.patty`, `O2.f1.fry`, `O2.pack`, `O2.verify`.
- **Modifiers** change the task graph or ingredient amounts: `no_lettuce` skips `toppings`, `no_cheese` / `extra_cheese` scale cheese.
- **Durations:** `ceil(step minutes × worker's skill multiplier)` (e.g. a backup grill cook at 1.5×).
- **Reservation (atomic):** starting a task takes the worker, one station slot, and its ingredients (`available → reserved`) together, or nothing.
- **Completion:** consumes reserved ingredients, frees the station and worker, appends to the worker's history.
- **Ingredient record:** `{"available": n, "reserved": n}` per ingredient.

## 4. Actions

| Action | Effect |
|---|---|
| `start_task(task_id)` | Validated and applied; the station comes from the task |
| `wait(reason)` | Logged |
| `report_blocker(task_id, reason)` | Logged |
| `request_help(task_id, reason)` | Logged |

Every proposal includes a short `why`. Validation, in order: worker available and idle → task exists and is pending → worker has the skill → prerequisites done → ingredients available → station has room. A rejection records the reason in the worker's `last_result` and changes nothing else.

The orchestrator also **detects blockers itself**: a ready task with no available qualified worker is logged once as `blocker` and shown in the report. Nobody gains a skill they don't have.

## 5. Decisions

- Only **idle, available workers with at least one ready task they are qualified for** are asked. Everyone else is skipped, which keeps model calls low.
- Each worker gets a small JSON **view**, not the whole state:
  - time, deadline, its skills, `last_result`
  - `ready_tasks`: id, order deadline, `my_minutes`, `best_minutes` (fastest available qualified worker), `station_has_room`, `ingredients_ok`
  - what the other workers are doing, and the current stock
- All asked workers propose against the same state; proposals are applied **one at a time in fixed worker order**, so conflicting claims are rejected. Rejected workers get **one re-ask round** in the same tick.
- **Modes**, always chosen explicitly and recorded in the state:
  - `det`: a fixed rule (earliest deadline first; prefer tasks where I'm the fastest; otherwise any feasible task). Used for tests and the baseline.
  - `ai`: persona text as the system prompt, the view as the user message, a strict JSON schema `{action, task_id, why}`, and one retry. If it still fails, the run is set to `paused` with the error shown; it never silently switches to `det`.
- Every decision event records the worker, mode, model, proposal, latency, and result.

## 6. Disruption and order verification

- **Disruption variant:** the grill cook becomes unavailable at minute 5. Their running task goes back to `pending`, the station slot and ingredients are released, and the duration restarts for whoever claims it next. Completed prerequisites stay done.
- **Blocker variant:** the same disruption, plus the backup's grill skill is removed. Grill tasks are reported as blocked and the run times out, which is the expected result.
- **Verification** runs when `verify` completes: the burgers and fries produced (with their modifiers) must match the order, every step must be done, and the order must be packed.
- **Metrics:** each order gets `correct` and `on_time` separately. The run ends `completed` when all orders are done, or `timed_out` at the deadline.

## 7. CLI

```
python orchestrator.py run --scenario baseline|disruption|blocker --mode det|ai [--max-ticks N]
python orchestrator.py resume [--run ID] [--max-ticks N]     # continue a paused or stepped run
python orchestrator.py replay [--run ID] [--at T]            # print events / snapshot T; no model calls
```

- **Step / pause:** use `--max-ticks 1`, then `resume`.
- **Reset:** start a new `run`; old runs stay in `sim/runs/`.
- **View:** `report.txt` shows target, time, orders and task status, stations, stock, workers, the latest decisions, and blockers. The full state is in `state.json`.

## 8. Two-person 3-hour schedule

- **Person A — engine:** `orchestrator.py` engine, config files, validation, clock, disruption, verification, snapshots, tests.
- **Person B — agents and visibility:** personas, `llm.py`, AI policy, decision logging, report, replay, docs.

| Minutes | Person A | Person B | Checkpoint |
|---|---|---|---|
| 0–20 | `menu.json`, `scenario.json`, state shape | `workers.json`, personas, view and proposal shape | Formats agreed from one real example of each |
| 20–60 | Task generation, validation, reservations, clock, completion (`det` mode) | `llm.py` with strict schema, one smoke call | **60:** one order completes in `det`; one valid AI proposal parsed |
| 60–120 | 10 orders, verification, disruption, blockers, events + snapshots | Plug the AI policy into the loop, re-ask round, paused-on-error | **120:** full baseline in `det` and `ai`; disruption run works |
| 120–160 | Tests from §9, fixes | `report.txt`, `replay`, `resume` | Runs are inspectable and replayable |
| 160–180 | Fresh-clone run of all three variants | README setup and demo steps | Tag `v0.1-draft` |

**If behind at minute 120,** cut in this order: `replay --at` (keep the event printout), `request_help`, report polish. Never cut validation, simulated time, or honest AI logging.

### Prompts for the coding tool

1. *(A)* "Read BRIEF.md and PROJECT_PLAN.md §2–§4. Implement the engine in `orchestrator.py` with the `det` policy, config in `sim/config/`, standard library only, Python 3.9. Run one order to completion and show events.jsonl."
2. *(B)* "Implement `llm.py` per PROJECT_PLAN.md §5: OpenRouter, strict JSON schema, timeout, one retry, error raised. Add the `ai` policy using personas and per-worker views. Run the baseline in `ai` mode."
3. *(A)* "Add the 10-order scenario, the disruption and blocker variants, verification, and the metrics from §6. Add the pytest checks from §9."
4. *(B)* "Add `report.txt`, `replay`, and `resume` per §7. Update the README with setup and demo steps."

## 9. Verification (pytest + manual)

1. Two workers claim the same task: one is accepted, the other is rejected with a reason.
2. Missing skill, insufficient stock, unfinished prerequisite, or a full station: the start is rejected and the state is unchanged.
3. A task completes exactly at `start + duration`, and its resources are released once.
4. Disruption: the interrupted task returns to pending, and its ingredients go from reserved back to available.
5. Blocker variant: a blocker is logged and nobody grills.
6. Modifiers: `no_lettuce` skips toppings; `no_cheese` uses no cheese.
7. The `det` baseline is repeatable: two runs produce identical event sequences, ignoring timestamps.
8. Replay reads snapshots and events with no model calls.

A tool saying "done" is not evidence. Check tests, `events.jsonl`, and `report.txt`.

## 10. Deferred

Web dashboard (it can read the same JSON files), hosting, per-task proficiency beyond one multiplier per skill, ingredient restocking, multiple restaurants, recipe editing.

## Estimates

| Deliverable | Estimate | Confidence |
|---|---|---|
| Draft (§8 complete) | 3 elapsed hours; 6 person-hours | 70/100 |
| Draft with contingency | 4.5 elapsed hours | 90/100 |
| Polished demo (web dashboard, prompt tuning, recordings) | +6–10 person-hours | 70/100 |

These are engineering judgments, not measurements. Re-estimate at the 60-minute checkpoint.
