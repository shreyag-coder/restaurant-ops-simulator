# Restaurant Operations Simulator

A planned multi-agent restaurant simulator. Four AI workers independently choose kitchen tasks through OpenRouter. One Python orchestrator validates their proposals, advances simulated time, and is the only writer of the restaurant state.

**Current status: planning only. No application code has been written yet.**

## Project documents

- [Original requirements](BRIEF.md)
- [Architecture, rules, schedule, and verification](PROJECT_PLAN.md)
- [Two-person checklist](TASKS.md)

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

- **Person A:** engine, config, validation, simulated time, disruption, tests.
- **Person B:** personas, OpenRouter integration, report, replay, docs.

## Planned demonstration

Run ten orders against a 30-minute target and read the decisions and reservations in `report.txt` and `events.jsonl`. Make the grill cook unavailable at minute 5 and watch the backup take over. Remove the backup's skill to see an explicit blocker. Finally, replay the run without any model calls.
