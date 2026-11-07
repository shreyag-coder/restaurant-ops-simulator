# Restaurant Operations Simulator

A planned multi-agent restaurant simulator. Four AI workers independently choose kitchen tasks through OpenRouter. One Python orchestrator validates their proposals, advances simulated time, and is the only writer of the restaurant state.

**Current status: planning only. No application code has been written yet.**

## Project documents

- [Original requirements](BRIEF.md)
- [Architecture, rules, schedule, and verification](PROJECT_PLAN.md)
- [Four-agent assignments, handoffs, and implementation prompts](TASKS.md)

## Design in brief

- **JSON for machine data, plain text for people and models.** There are no servers and no internal APIs.
- `sim/config/*.json`: menu, workers, and scenario (10 orders; baseline, disruption, and blocker variants).
- `sim/personas/<id>.txt`: each worker's background, given to the model.
- `sim/runs/<run_id>/state.json`: the authoritative state. `events.jsonl` is the event log, `snapshots/` holds a copy per tick, and `report.txt` is the human-readable view.
- Each simulated minute, only idle workers with ready tasks are asked to choose. The orchestrator validates proposals one at a time, reserves the worker, station, and ingredients atomically, and completes tasks when their time is up.

## Technology

| Component | Choice |
|---|---|
| Orchestrator | Python 3.9+, standard library only |
| AI decisions | OpenRouter, strict JSON-schema output |
| State, config, logs | JSON / JSONL |
| Personas, report | Plain text |
| Tests | pytest with an explicitly selected deterministic policy |
| Replay | Snapshots and events, no model calls |

## Planned usage

```
export OPENROUTER_API_KEY=...        # never commit
python orchestrator.py run --scenario baseline --mode det
python orchestrator.py run --scenario disruption --mode ai
python orchestrator.py replay
pytest
```

## Estimates

| Milestone | Estimate | Confidence |
|---|---|---|
| First draft | 3 elapsed hours with two people (6 person-hours) | 70/100 |
| Draft with contingency | 4.5 elapsed hours | 90/100 |
| Polished demo with a web dashboard | +6–10 person-hours | 70/100 |

Estimates are engineering judgments, not measurements.

## Ownership

- **Agent 1:** engine, authoritative state, CLI, and integration.
- **Agent 2:** scenario config, validation, and recipe task generation.
- **Agent 3:** worker personas, decision policies, and OpenRouter.
- **Agent 4:** recording helpers, reports, replay, and setup documentation.

These are four coding assignments, separate from the four simulated workers. Only Agent 1 edits `orchestrator.py`; helpers use ordinary imports and the orchestrator remains the only state writer. Each agent owns separate files and tests; see TASKS.md. The estimates above retain their original two-contributor assumptions and should be revisited after the first integrated order.

## Burrito-line crew (Chipotle-style)

A second, larger kitchen modeled on a fast-casual burrito line. It lives in `sim/config_chipotle/` and `sim/personas_chipotle/`, and the default burger kitchen is unchanged.

- **Menu:** burritos, bowls, three soft tacos, and chips & guac. Modifiers include steak, sofritas, veggie, no beans, no cheese, no rice, and salsa instead of guac.
- **Stations:** grill, tortilla press, back-of-house prep, chip fryer, serving line, expo/bagging, and pickup shelf.
- **Crew of 10:** kitchen manager, grill cook, prep cook, tortilla station, line server, digital make line, expo, cashier, service manager, and a second-week trainee.
- **Worker profiles:** each worker in `workers.json` can carry an optional `profile` (experience, background, home station, shift, certifications, strengths, watch-outs). The profile is descriptive only: `skills` alone decide which tasks a worker may start.
- **16 orders in 30 minutes.** In det mode, baseline finishes at minute 25 and disruption (grill cook out at minute 5, kitchen manager covers) finishes at minute 29. In the blocker variant the kitchen manager has no grill skill, so only 7 of 16 orders finish, with grill blockers.

```
python orchestrator.py run --scenario disruption --mode det \
    --config-dir sim/config_chipotle --personas-dir sim/personas_chipotle
```

In AI mode, up to ten idle workers can be asked each minute, so expect noticeably more model calls than with the four-worker kitchen.

## Planned demonstration

Run ten orders against a 30-minute target and read the decisions and reservations in `report.txt` and `events.jsonl`. Make the grill cook unavailable at minute 5 and watch the backup take over. Remove the backup's skill to see an explicit blocker. Finally, replay the run without any model calls.
