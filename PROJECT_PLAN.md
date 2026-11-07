# Restaurant Operations Simulator — Build Plan

Planning baseline: November 7, 2026. This document specifies future work; it does not claim that the application has been implemented.

## 1. Outcome, scope, and delivery confidence

Build a hosted simulator where four AI workers independently choose restaurant tasks through OpenRouter. The Python backend enforces rules and advances simulated time; the dashboard renders actual backend state.

The original requirements are preserved in [BRIEF.md](BRIEF.md). The implementation schedule and ownership are in [TASKS.md](TASKS.md).

| Milestone | Completion estimate | Confidence |
|---|---|---|
| Documentation-only repository setup | 15–25 minutes | 95/100 |
| First hosted draft | 3 elapsed hours with two contributors; 6 person-hours | 60/100 |
| Draft contingency | Up to 2 additional elapsed hours; up to 4 additional person-hours | 85/100 within 5 elapsed hours |
| Finalized hackathon demo | Additional 12–16 person-hours after the draft; approximately 1–2 working days | 85/100 |

These are engineering judgments, not measured probabilities. The three-hour implementation clock begins after repository setup and account readiness. Estimates assume both contributors know their assigned stack, use AI coding assistance, and have working GitHub, Render, and funded OpenRouter access. Hosting delays, model availability, and integration are the main schedule risks.

The draft implements the full simulation loop with a basic dashboard. Finalization improves verification, replay usability, prompts, tests, and presentation. The target is a hackathon demo, not a production service.

## 2. Architecture and interfaces

### Technology and hosting

- Backend: Python 3.12, FastAPI, Pydantic, HTTPX, pytest.
- Frontend: React, TypeScript, Vite, plain CSS.
- AI: direct OpenRouter HTTP calls; four worker-specific profiles using one configurable model.
- Default model: `google/gemini-2.5-flash-lite`, verified through a strict-schema smoke test before integration.
- Deploy one Render Docker service: a Node 22 build stage creates frontend assets; a Python runtime serves assets and API through FastAPI.
- Run one backend process, one Uvicorn worker, and one service replica so there is exactly one state owner.
- Keep the authoritative JSON state and current recordings in memory. Export recordings before restart; do not rely on ephemeral hosting files for durable storage.
- Protect the hosted demo with HTTP Basic authentication using username `demo` and a server-side `DEMO_PASSWORD`. Exempt the health endpoint for platform checks. Keep `OPENROUTER_API_KEY` server-side; configure the model with `OPENROUTER_MODEL`.
- Pin successfully tested dependencies and commit lock information during implementation.

Official references: [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), [model page](https://openrouter.ai/google/gemini-2.5-flash-lite), [Render Docker deployment](https://render.com/docs/docker), [Render free-service limitations](https://render.com/docs/free).

### State and shared contract

Maintain one authoritative JSON document containing:

- Simulation ID, clock, version, running/paused/completed/timed-out status, shared target, and deadline.
- Stations with equipment, capacity, availability, active tasks, and workers.
- Ingredients with quantity, unit, available/reserved quantities, and storage location.
- Recipes with ingredient quantities, task dependencies, required stations/capabilities, and step durations.
- Orders with arrivals, deadlines, quantities, modifiers, task progress, produced items, status, and verification result.
- Staff with descriptions, experience, explicit skills, task proficiency/duration modifiers, availability, and current work.
- Activity history containing proposed actions, explanations, acceptance/rejection results, task events, inventory changes, requests for help, blockers, and order changes.

Use Pydantic schemas as the backend contract, plus a representative JSON fixture shared with the frontend. Agree the fixture and TypeScript-facing shapes during the first 15 minutes. Person A owns subsequent contract changes; Person B reviews them before integration.

| Endpoint | Behavior |
|---|---|
| `GET /api/state` | Return the complete authoritative state |
| `POST /api/control` | Start, pause, step, or reset; reset selects scenario and decision mode |
| `POST /api/staff/{id}/availability` | Change worker availability through backend validation |
| `GET /api/run/export` | Download recorded snapshots and decision/event history |
| `GET /health` | Public deployment health check |

Mutating requests carry `runId` and `expectedVersion`; reject conflicts with a clear `409` result. Poll state once per second. Step performs one worker-decision round and advances to the next scheduled simulation event. Pause freezes progression, and reset invalidates outstanding model responses. Responses arriving after pause are discarded and requested again on resume.

### Dashboard

Display shared target, simulation time, progress, orders/tasks, station activity, available/reserved ingredients, staff profiles/actions, decisions/results/blockers, and an expandable complete JSON view. Include start, pause, step, reset, worker-unavailability controls, export, and a basic recorded-snapshot replay slider. Clearly identify AI, deterministic, and replay modes.

## 3. Simulation and independent worker behavior

### Fixed restaurant scenario

- One restaurant, four workers, ten seeded orders, and a thirty-minute simulation deadline.
- Menu: burger and fries. Burger modifiers: no onion and extra cheese.
- Stations: preparation, grill, fryer, assembly, packing. Preparation capacity is two; other station capacities are one.
- Orders arrive at simulation start. Seed four burger-and-fries orders, three burger-only orders, and three fries-only orders. One burger-only order requests two burgers; one fries-only order requests two fries. Include both supported modifiers in the fixture.
- Allocate recipe ingredients to exactly one consuming step to avoid duplicate charging. Initial inventory covers all seeded requirements plus a small buffer.
- Initial base durations: preparation 60 seconds, grilling 120 seconds, frying 90 seconds, assembly 45 seconds, packing 30 seconds per order, verification 15 seconds per order. Tune the fixture only if deterministic acceptance testing shows the seeded target is infeasible.

| Worker | Explicit capabilities | Initial duration modifier |
|---|---|---|
| Grill cook | Grill, preparation | Grill 0.8; other work 1.0 |
| Preparation specialist | Preparation, assembly, backup grill | Grill 1.25; other work 1.0 |
| Fry cook | Frying, preparation, packing | 1.0 |
| Expediter | Assembly, packing, verification | 1.0 |

At simulated minute five, the grill cook becomes unavailable for the rest of the run. Qualified remaining workers divide the remaining work. Also provide a baseline scenario without disruption. A separate test fixture removes the backup qualification to verify missing-capability blockers.

### Engine invariants

- Build item-level task dependency graphs from explicit recipes; add per-order packing and verification.
- Agents propose actions; only the engine mutates authoritative state.
- Validate worker availability/qualification, prerequisites, task ownership, inventory, station type/capacity, run identity, and state version.
- Under one backend lock, reserve worker, station capacity, and ingredients together. No network await occurs while holding the lock.
- Increment state version for each recorded state transition. Never partially apply a rejected action.
- Record task finish time using base duration and worker proficiency. Completion consumes reservations, commits outputs, and releases worker/station capacity.
- Interrupted unfinished tasks return to pending, release all reservations, and restart their duration when reclaimed. Completed prerequisite outputs remain complete.
- Use a discrete-event clock; model response latency does not consume simulated time. Advance to task completion, disruption, or the deadline.
- At an equal timestamp, apply task completions before disruption and deadline evaluation.
- Verify requested item counts, modifiers, completed recipe steps, packing, and final verification against backend records. A worker cannot declare an order complete by assertion.
- Track correctness and timeliness separately. Finish when all orders pass within the target or the deadline expires.
- Record permanent blockers explicitly. If no future event can resolve them, advance to the deadline and finish timed out with the reason.

### Agent integration

Each available worker reads the latest state, its own profile, explicit capabilities, ready tasks, deadlines, inventory, station availability, and current coworkers' work. It independently chooses one action with a short explanation:

- `start_task(task_id, station_id)`
- `request_help(task_id, reason)`
- `report_blocker(task_id, reason)`
- `wait(reason)`

Natural-language background informs decisions; structured capabilities determine permission. Help requests are visible signals, not grants of new skills.

For the draft, request decisions sequentially against fresh state, rotating the first worker each round. Started tasks execute concurrently in simulated time. This keeps workers independent while reducing stale-snapshot conflicts.

Use OpenRouter chat completions with strict JSON-schema output and `provider.require_parameters: true`; validate the response again through Pydantic. Run an initial smoke test and stop integration with a clear error if the configured model/provider cannot honor the schema.

Use a 20-second request timeout, one retry, and a 200-call per-run guard. On repeated provider/schema failures or call-budget exhaustion, pause visibly. If all workers repeatedly wait while executable tasks remain and no future event exists, pause with a stalled-decision explanation after three unsuccessful rounds. Do not label poor AI choices as impossible restaurant work.

Provide an explicitly selected deterministic policy for repeatable tests. Never silently substitute it during an AI run. Record model ID, proposed action, short explanation, result, latency, and available token usage. A deterministic baseline must meet the target; an AI run may fail and must report that outcome accurately.

### Replay

Capture versioned snapshots after transitions and retain ordered decision/event records. Export a versioned JSON recording with scenario identity, snapshots, and final outcome. Replay selects recorded snapshots without running the engine or calling OpenRouter. Finalization adds validated recording import; imported recordings are read-only and never replace a live simulation.

## 4. Validation and release gates

Draft acceptance requires a hosted URL, four recorded AI worker profiles, concurrent work, resource consistency, simulated durations, correct order verification, disruption recovery, functioning controls, all dashboard panels, and exportable/replayable recordings.

Prioritize these pytest scenarios:

1. Conflicting task claims: only one succeeds.
2. Missing qualifications, dependencies, inventory, or station capacity: reject without partial reservations.
3. Task completion and interruption preserve ingredient and capacity accounting.
4. Tasks do not finish early; proficiency changes configured duration.
5. Missing quantities or incorrect modifiers prevent verification.
6. Reset and pause prevent outdated model responses from changing state.
7. The deterministic baseline meets the deadline; missing-capability work records explicit blockers.
8. Pause, step, reset, repeated start, and terminal-state controls behave consistently.
9. Replay's final snapshot matches the recorded final state and makes no model requests.

Run one hosted AI baseline and one hosted disruption scenario manually. Verify a fresh run after reset, export/replay, visible provider failures, and protection of server credentials. Do not present local-only tests as hosted verification.

Tag `v0.1-draft` only when the implemented acceptance items are verified; explicitly list anything incomplete. If behind at minute 115, stop styling and convenience features while preserving correctness, real AI decisions, simulation timing, disruption handling, and hosting.

## 5. Finalized hackathon demo

After the draft, budget an additional 12–16 person-hours:

| Work | Owner | Estimate |
|---|---|---|
| Expand engine/API invariants, interruption and deadline tests; fix failures | Person A | 4–5 hours |
| Improve prompts, repeated-wait behavior, decision diagnostics | Person B | 2–3 hours |
| Scenario selection, replay import/filtering, clearer task dependencies and dashboard polish | Person B | 3–4 hours |
| Clean deployment/setup verification, hosted failure checks, README and three-minute demonstration | Both | 3–4 total person-hours |

Release `v1.0-demo` with repository, hosted URL, passing checks, baseline/disruption recordings, setup/environment instructions, architecture explanation, known demo-storage limitations, and a three-minute demonstration of normal operation, worker loss, adaptation, and replay.

Defaults: private repository; Person A/B until contributor identities are supplied; one restaurant and one active shared run; demo authentication; no durable production storage, multiple restaurants, recipe editor, or production operations program in this release.
