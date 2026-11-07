# Restaurant Operations Simulator — Simple JSON + Text Plan

Updated: November 7, 2026. This is a plan for future work, not a claim that anything is implemented. Original requirements: [BRIEF.md](BRIEF.md).

## 1. Goal and loop

Build a small, working multi-agent kitchen simulation using four coding agents with separate file ownership. The earlier three-hour estimate assumed two contributors; re-estimate after the first integrated order. Assignments and implementation prompts are in [TASKS.md](TASKS.md).

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
orchestrator.py            # engine, authoritative state, CLI, integration
scenario.py                # config loading/validation and pure task generation
policies.py                # detached worker views and det/ai policies
llm.py                     # OpenRouter call with strict JSON schema
recording.py               # checkpoint helpers called only by orchestrator
reporting.py               # pure text report rendering
replay.py                  # read-only historical view
tests/test_*.py            # separate tests owned by each coding agent
```

Python 3.9+ standard library only (`urllib` for HTTP). pytest for tests. The API key comes from `OPENROUTER_API_KEY`; the model from `OPENROUTER_MODEL`. Helpers run inside the orchestrator process; they do not add another service or state writer.

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
- **Boundary ordering:** disruption precedes completion, as specified in the tick loop. A task belonging to the disrupted worker is interrupted even when its finish time is that tick. At the deadline tick, process due completions and verification before deciding completed/timed-out; start no new work at the deadline.

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
- The provider's one retry is separate from the one re-ask after an action conflict. The policy returns proposal/metadata; only the engine validates, records, and changes state. Deterministic ties use task ID after deadline and fastest-worker preference. Full function boundaries and field requirements are in TASKS.md.

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
- **Resume boundary:** save frozen run inputs, applied-event markers, committed event sequence, and an explicit continuation position. Resume must not reapply accepted actions, disruption, or completion, including after a mid-tick AI error. Detect incomplete checkpoints and prevent concurrent writers for one run. Replay reads committed snapshots/events without invoking policies or writing files.

## 8. Four-agent delivery workflow

These are coding agents implementing the application, separate from the four simulated workers. Detailed checklists, interfaces, and copyable prompts are in [TASKS.md](TASKS.md).

| Agent | Ownership | First deliverable |
|---|---|---|
| 1 — engine/integration | `orchestrator.py`, state, validation, time, disruption, CLI, engine/CLI tests | One order completes correctly through the real engine |
| 2 — scenarios/tasks | `sim/config/*.json`, `scenario.py`, scenario tests | Valid config and expanded tasks for one modified multi-quantity order |
| 3 — worker decisions | `policies.py`, `llm.py`, personas, policy/provider tests | Repeatable deterministic proposal and strictly parsed AI response |
| 4 — inspection/recording | `recording.py`, `reporting.py`, `replay.py`, their tests, README | Report and read-only replay from an agreed saved state |

Only Agent 1 edits the orchestrator. Agent 2 owns worker JSON; Agent 3 owns matching persona text. Each agent owns separate test files. Agent 4 supplies recording helpers, but only the orchestrator process invokes writers.

### Dependency checkpoints

1. Share one config/order/task example (Agent 2), state/event/checkpoint example (Agent 1), view/proposal example (Agent 3), and rendered report (Agent 4). Start independent work against these small examples.
2. Integrate config/task generation with the engine, then the deterministic policy; complete one order before expanding to ten.
3. Connect real AI decisions, disruption/blocker variants, and recording/reporting. Preserve explicit provider failures and resource validation.
4. Verify resume/replay and all §9 checks; document fresh setup and actual baseline/disruption/blocker outcomes. Record a real AI run separately from offline tests.

Re-estimate after checkpoint 2. If behind, reduce report polish, optional help actions, and exact-tick replay convenience before changing correctness or timing. Keep event inspection, honest AI logging, and explicit remaining gaps.

## 9. Verification (pytest + manual)

1. Two workers claim the same task: one is accepted, the other is rejected with a reason.
2. Missing skill, insufficient stock, unfinished prerequisite, or a full station: the start is rejected and the state is unchanged.
3. A task completes exactly at `start + duration`, and its resources are released once.
4. Disruption: the interrupted task returns to pending, and its ingredients go from reserved back to available.
5. Blocker variant: a blocker is logged and nobody grills.
6. Modifiers: `no_lettuce` skips toppings; `no_cheese` uses no cheese.
7. The `det` baseline is repeatable: two runs produce identical event sequences, ignoring timestamps.
8. Replay reads snapshots and events with no model calls.
9. Step/resume matches an uninterrupted deterministic run; no repeated disruption, task completion, or event sequence. Provider-error pause preserves continuation state.
10. `no_lettuce` rewires dependencies; `extra_cheese` uses the configured quantity; contradictory/unknown modifiers are rejected. Verify requested quantities/modifiers against produced items, not only task statuses.
11. Recordings use committed state/event boundaries; replay is read-only and resume rejects incomplete checkpoints or an already active writer.

A tool saying "done" is not evidence. Check tests, `events.jsonl`, and `report.txt`.

## 10. Deferred

Web dashboard (it can read the same JSON files), hosting, per-task proficiency beyond one multiplier per skill, ingredient restocking, multiple restaurants, recipe editing.

## Estimates

| Deliverable | Estimate | Confidence |
|---|---|---|
| Draft (§8 complete) | 3 elapsed hours; 6 person-hours | 70/100 |
| Draft with contingency | 4.5 elapsed hours | 90/100 |
| Polished demo (web dashboard, prompt tuning, recordings) | +6–10 person-hours | 70/100 |

These are historical two-contributor engineering estimates, not measurements or a new four-agent schedule. Re-estimate after the first integrated order.
