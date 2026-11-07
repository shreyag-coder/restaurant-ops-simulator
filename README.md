# Restaurant Operations Simulator

A planned multi-agent restaurant simulator: four AI workers independently choose tasks through OpenRouter. The workers communicate through plain markdown files, and one Python orchestrator script validates their proposals, advances simulated time, and is the only writer of the restaurant state.

**Current status: planning only. No application code, dependencies, or deployment have been created.**

## Project documents

- [Original requirements](BRIEF.md)
- [Architecture, delivery plan, and acceptance criteria](PROJECT_PLAN.md)
- [Two-person task assignments and checkpoints](TASKS.md)

## Design in brief

- No internal APIs and no server. State and communication live in text files.
- `sim/world.md` is the authoritative restaurant state (clock, stations, ingredients, orders, tasks).
- `sim/target.md` holds the shared goal and deadline.
- `sim/agents/<name>.md` holds each worker's profile, skills, current task, and task history.
- Each turn an agent reads the target, the world, and its own file, then writes a **proposal** to `sim/proposals/`. It never edits `world.md`.
- `orchestrator.py` validates proposals one at a time, applies valid ones atomically, advances the clock, and appends to `sim/log/`, including a snapshot per tick for replay.

## Proposed technology

| Component | Planned choice |
|---|---|
| Orchestrator | Python 3.12 |
| AI decisions | OpenRouter, strict JSON-schema responses |
| State and messages | Markdown files with structured JSON blocks |
| Tests | pytest and an explicitly selected deterministic worker policy |
| Inspection | Terminal summary or static page reading the files; optional dashboard later |
| Replay | Snapshots and event log, no further model calls |

## Delivery estimates

| Milestone | Estimate | Confidence |
|---|---|---|
| First draft | 3 elapsed hours with two people; 6 person-hours | 60/100 |
| Draft with contingency | 5 elapsed hours with two people; 10 person-hours | 85/100 |
| Finalized demo | Additional 10–14 person-hours | 70/100 |

Estimates assume OpenRouter credits are ready and both contributors use AI coding assistance. Confidence scores are engineering judgments, not measured probabilities.

## Ownership

- **Person A:** orchestrator, world state, validation, simulated time, tests.
- **Person B:** agent files and prompts, OpenRouter integration, replay and viewer.

Contributor names remain unassigned. The detailed split is in [TASKS.md](TASKS.md).

## Planned demonstration

Complete ten orders within thirty simulated minutes, inspect worker decisions and resource reservations in the files, make a worker unavailable, observe qualified reassignment, and replay the recorded run without further model calls.

The release target is a polished hackathon demo. A web dashboard, hosting, production persistence, multiple restaurants, and recipe editing are outside the core release.
