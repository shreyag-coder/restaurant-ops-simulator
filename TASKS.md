# Four-Agent Implementation Tasks

Based on pulled commit `f915ca4` ("Align plan to JSON-first architecture"), verified against remote `main` on November 7, 2026. Application implementation has not started. These four coding agents are distinct from the four simulated restaurant workers.

Follow [PROJECT_PLAN.md](PROJECT_PLAN.md) for behavior: Python 3.9+ standard library, JSON/JSONL, text personas, and CLI controls. pytest is a development dependency. Web dashboard and hosting remain deferred.

## Exclusive ownership

| Agent | Responsibility | Files |
|---|---|---|
| 1 | Engine, state, CLI, integration | `orchestrator.py`, `tests/test_engine.py`, `tests/test_cli.py`, `.gitignore`, `requirements-dev.txt` |
| 2 | Config, validation, task graphs | `sim/config/*.json`, `scenario.py`, `tests/test_scenario.py` |
| 3 | Policies, personas, OpenRouter | `policies.py`, `llm.py`, `sim/personas/*.txt`, `tests/test_policies.py`, `tests/test_llm.py` |
| 4 | Recording, report, replay, setup docs | `recording.py`, `reporting.py`, `replay.py`, `tests/test_recording.py`, `tests/test_reporting.py`, `tests/test_replay.py`, `README.md` |

Each agent edits only their files and sends cross-file fixes to the owner. Agent 1 owns integration and runtime state fields; Agent 2 owns seed formats. Use separate branches/worktrees for separate coding sessions; never switch a shared checkout while another agent edits it.

Only the orchestrator process writes run artifacts. Agent 4's recording helpers are called by that process, not an independent writer. Config and personas stay unchanged during a run. Ignore `sim/runs/`, `.env`, bytecode, and test caches; never commit keys.

## First handoff and shared boundaries

1. Agent 2 shares one modified multi-quantity order, its expanded tasks, menu shape, and four worker IDs/skills.
2. Agent 1 shares a JSON-serializable state example, event example, and resumable checkpoint boundary using those inputs.
3. Agent 3 shares a detached worker view/proposal; Agent 4 renders a sample report and demonstrates a recording round-trip.
4. Agent 1 connects the helpers through ordinary Python imports. No internal HTTP API or shared-file polling is needed.

Start in parallel: Agent 1 uses a tiny test scenario; Agent 2 prepares data/expansion; Agent 3 works on parsing and policies against the sample view; Agent 4 works on reporting/recording against the sample state. Complete examples before integration, without waiting for every module to be finished.

Proposed interfaces (not implemented yet):

```python
# Agent 2: fresh config and pure task expansion
load_config(config_dir, variant) -> dict
validate_config(config) -> None  # raises ValueError
expand_order(order, menu) -> dict  # {"tasks": [...], "expected_items": [...]}

# Agent 3: detached inputs; no state mutation or run-file writes
build_worker_view(state, worker_id) -> dict
choose_deterministic(view) -> dict  # decision envelope
choose_ai(view, persona) -> dict    # envelope or DecisionError

# Agent 4: writers called only by the orchestrator
render_report(state, recent_events) -> str
save_checkpoint(run_dir, state, new_events) -> None
load_checkpoint(run_dir) -> dict
load_events(run_dir, at=None) -> list
replay_run(run_dir, at=None) -> str  # read-only
```

Generated tasks contain `id`, `order_id`, `item_id`, `step_id`, `required_skill`, `station_id`, `base_minutes`, `ingredients`, `depends_on`, and `status="pending"`. The engine initializes ownership, reservations, start/finish times, and produced items. Each unit has its own task chain; pack/verify appear once per order.

Decision envelope:

```json
{
  "proposal": {"action": "start_task", "task_id": "O2.b1.patty", "why": "Ready work for the earliest deadline."},
  "mode": "det",
  "model": null,
  "latency_ms": 0,
  "attempts": 1
}
```

The orchestrator attaches worker/decision identity, observed state version, and accepted/rejected result. `task_id` is required for start/help/blocker actions and null for wait; `why` is nonempty. Reject extra properties, unknown actions, and invalid field types. AI output includes configured model identity and measured metadata; exhausted attempts raise `DecisionError` with sanitized attempt metadata.

Views contain time/deadline, skills, last result, qualified dependency-ready tasks, order deadlines, `my_minutes`, `best_minutes`, resource feasibility, other workers' activity, and stock. `best_minutes` compares available qualified workers, including busy workers, as a proficiency comparison; busy workers cannot claim additional tasks.

Checkpoint fields must cover run ID, schema/state version, mode/model, frozen config/personas, clock and continuation position, tasks/workers/orders, produced items, reservations, applied disruption markers, and last committed event sequence. Events carry increasing `seq`, run ID, tick, type, references, and payload. Agent 1 agrees exact field names with Agents 3 and 4 before wiring modules together.

## Agent 1 — engine and CLI

- [ ] Initialize authoritative state; admit orders and incorporate Agent 2's generated tasks.
- [ ] Validate actions and atomically reserve worker, station, and ingredients; record rejection reasons.
- [ ] Enforce integer ticks and `ceil(base_minutes * skill_multiplier)` completion; release/consume resources once.
- [ ] Track produced quantities/modifiers, packing, verification, correctness, and timeliness.
- [ ] Apply minute-five interruption and rollback; preserve completed prerequisites; detect/deduplicate missing-skill blockers.
- [ ] Integrate policies: same-state first-round views, fixed application order, one fresh-view re-ask for rejected workers.
- [ ] Pause on provider failure with visible metadata and no deterministic fallback.
- [ ] Implement run/resume/replay CLI, run selection, and max-tick controls using Agent 4's helpers.
- [ ] Make checkpoints resume without repeating accepted actions, disruptions, or completions; prevent concurrent run writers.
- [ ] Add engine/CLI tests, ignore/dependency files, and perform final integration.

Acceptance: competing claims accept once; invalid starts leave resources unchanged; timing and inventory hold; step/resume matches an uninterrupted deterministic run; terminal metrics are honest. Completed/timed-out runs cannot resume.

Implementation prompt:

> Read BRIEF.md, PROJECT_PLAN.md, and TASKS.md. Implement only Agent 1's files. Build the single-writer Python engine and CLI, importing the other agents' helpers. Share concrete state/event/checkpoint examples early. Prove one order through reservations, timing, packing, and verification, then integrate ten orders and all variants. Add focused engine/CLI tests for conflicts, interruption, AI-error pause, and step/resume equivalence. Report commands/results and remaining gaps. Do not add a server, dashboard, or deployment.

## Agent 2 — scenario inputs and task graphs

- [ ] Define `menu.json`: stock/units, station capacities, recipes, base durations, modifiers, packing/verification.
- [ ] Define `workers.json`: four workers, structured skills/multipliers, and grill backup; share IDs with Agent 3.
- [ ] Define `scenario.json`: ten orders, thirty-minute target, baseline/disruption/blocker variants.
- [ ] Implement fresh config loading, reference validation, and pure per-unit expansion.
- [ ] Implement `no_lettuce` dependency rewiring, explicit `no_cheese`/`extra_cheese` amounts, and rejection of unknown/contradictory modifiers.
- [ ] Validate IDs, acyclic dependencies, skill/station coverage, positive durations, and aggregate ingredient/packaging demand.
- [ ] Keep overrides local to copied run inputs; ensure unfinished grill work at minute five in disruption/blocker demonstrations.
- [ ] Add scenario tests and share expanded tasks for one modified multi-quantity order.

Acceptance: unique per-unit task chains, pack depends on every item's final step, modifiers affect all requested units correctly, and base config is unchanged by variant loading. Static stock/skill checks do not prove deadline completion.

Implementation prompt:

> Implement only Agent 2's config files, scenario.py, and scenario tests. Follow the latest integer-tick JSON-first plan. Supply pure load_config, validate_config, and expand_order functions; four workers; burgers/fries; ten orders; and three variants. Validate resources, modifiers, and dependency rewiring. Share worker IDs and expanded tasks early. Never edit the orchestrator, policies, personas, or run files. Port useful old fixture content to the current schema rather than restoring incompatible formats. Report checks and expansion results.

## Agent 3 — worker decisions and OpenRouter

- [ ] Write four matching personas; read structured skills from Agent 2's config.
- [ ] Build detached views and deterministic feasible-task ranking: earliest deadline, prefer fastest-worker tasks, stable task-ID tie-break; otherwise wait.
- [ ] Implement urllib calls using configured model, strict response schema, timeout, and one provider retry (two attempts total).
- [ ] Validate action/task-ID/explanation fields locally; return proposal plus mode/model/latency/attempt metadata.
- [ ] Raise explicit sanitized DecisionError after exhausted attempts; keep provider retry distinct from engine conflict re-ask.
- [ ] Test purity, skill filtering, rounding, stable ranking, refusals, invalid responses, timeouts, and bounded attempts with mocked HTTP.
- [ ] Run an opt-in real-provider smoke check when credentials exist and report it separately.

Acceptance: no state mutations, run-file writes, secret logging, or silent fallback. Default tests run offline. A real proposal is required before claiming live AI integration works.

Implementation prompt:

> Implement only Agent 3's policies.py, llm.py, personas, and focused tests. Use Python 3.9+ standard library, urllib, and the agreed view/envelope. Return proposals and metadata; scheduling, rejection handling, logging, and pausing belong to the orchestrator. Use explicit det/ai modes, strict local checks, timeout, and at most one provider retry. Do not mutate state or invent skills. Coordinate worker IDs with Agent 2 and state fields with Agent 1. Mock default tests and report real-provider validation separately.

## Agent 4 — recording, reports, and replay

- [ ] Render target/time/mode/status, orders/tasks, station utilization, stock/reservations, workers, decisions, blockers, and separate correctness/timeliness results.
- [ ] Implement orchestrator-invoked checkpoint helpers for state, ordered events, snapshots, and generated report.
- [ ] Use atomic JSON replacement and an explicit committed-event boundary; detect incomplete checkpoints.
- [ ] Agree frozen inputs and continuation fields with Agent 1 so edited seed files cannot alter a resumed run.
- [ ] Implement read-only replay from snapshots/committed events, with exact-tick selection and useful missing-run/tick errors.
- [ ] Test report purity, checkpoint round-trips, commit/sequence boundaries, and offline replay with network calls forbidden.
- [ ] Update README setup/demo instructions after integration, documenting actual behavior and limitations.

Acceptance: reports reflect saved state; replay selects historical data without policies, credentials, network, or writes. Helpers never create a second writer. Agent 1's resume tests prove no repeated work/events.

Implementation prompt:

> Implement only Agent 4's recording.py, reporting.py, replay.py, tests, and README setup/demo documentation. Agree checkpoint/event shapes with Agent 1 first. Use standard-library file handling, atomic state replacement, and a documented committed-event boundary. Only the orchestrator invokes writers. Replay must be read-only and must not call policies/models. Supply helpers for Agent 1 to wire into the CLI. Validate known reports and exact-tick offline replay; report unresolved crash/resume limitations honestly. Do not add a dashboard.

## Integration order and release gate

1. **Inputs + engine:** Agents 1/2 complete one order using an explicit test decision provider.
2. **Policies:** connect Agent 3's deterministic policy, then AI decisions and visible error pausing.
3. **Inspection:** Agent 1 wires Agent 4's helpers into run/resume/replay.
4. **Acceptance:** full tests, fresh deterministic variants, and a separately recorded real AI baseline.

- [ ] Fresh setup runs baseline, disruption, and blocker in deterministic mode.
- [ ] Quantities, modifiers, timing, and reservations are engine-verified.
- [ ] Disruption demonstrates actual interruption, resource rollback, and qualified reassignment.
- [ ] Blocker variant has unfinished grill work, reports missing skill, and times out honestly.
- [ ] AI decisions/failures are recorded without hidden fallback.
- [ ] Pause/step/resume do not duplicate applied work/events.
- [ ] Replay works offline without changing saved files.
- [ ] Tests pass, real demo results are documented, and gaps are explicit.

Four agents do not imply half the earlier elapsed-time estimate. Re-estimate after the first one-order integration. Keep the earlier scenario stash intact: its paths, modifiers, inventory conventions, and fractional durations differ from the current plan.
