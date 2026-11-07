# Two-Person Delivery Tasks

This is the implementation backlog, not a record of completed application work. All implementation tasks start unchecked. See [PROJECT_PLAN.md](PROJECT_PLAN.md) for contracts, behavior, tests, estimates, and release criteria.

## Ownership and coordination

- **Person A:** backend state, simulation engine, validation, API, engine tests, and deployment.
- **Person B:** worker profiles/prompts, OpenRouter adapter, dashboard, and replay interface.
- Use short-lived `feature/simulation-api` and `feature/agents-dashboard` branches. Integrate at the checkpoints below.
- Agree the state fixture, API shapes, and agent adapter during the first 15 minutes. Person A owns later contract changes; Person B reviews them before integration.
- Person B's agent adapter receives a state snapshot and worker profile and returns a validated proposal or explicit provider error. Person A wires it into the engine; the adapter never changes world state.
- Keep credentials out of commits. Contributor GitHub usernames and collaborator invitations are not assigned by this document.

## Three-hour draft schedule

The clock starts after repository setup, account access, and API-credit readiness. Two contributors work concurrently: three elapsed hours equals six person-hours.

| Elapsed time | Person A | Person B | Checkpoint |
|---|---|---|---|
| 0–15 min | FastAPI skeleton, state/API contracts | React skeleton, agent adapter contract | Agree state fixture and interfaces |
| 15–45 min | Dependencies, discrete clock, reservations | Dashboard fixture panels, OpenRouter adapter | One strict-schema AI action returned |
| 45–80 min | Validation, completion, controls; initial Render deploy | Connect dashboard and controls | Hosted deterministic loop |
| 80–115 min | Disruption, interrupted-task recovery, order verification | Integrate worker decisions and action results | Real AI starts and completes tasks |
| 115–145 min | Snapshot recording/export, critical tests | JSON inspector, activity feed, basic replay slider | Inspectable replayable run |
| 145–165 min | Merge, tests, engine/hosting fixes | Verify hosted panels and controls | Release candidate |
| 165–180 min | Fresh-run/reset and release checks | Disruption demo and documentation | Verified `v0.1-draft` |

If behind at minute 115, stop visual styling and convenience work. Do not weaken validation, fake backend state, silently replace AI decisions, or skip simulation timing to claim completion. Record any remaining acceptance gaps.

## Person A — draft checklist

- [ ] Establish state schema and representative shared fixture.
- [ ] Seed two recipes, ten orders, ingredients, stations, and four profiles.
- [ ] Build item-level dependency graphs plus order packing/verification.
- [ ] Implement atomic validation, reservations, versioning, and action rejection.
- [ ] Implement discrete-event time, proficiency durations, completion, and resource release.
- [ ] Add start/pause/step/reset, stale-run protection, and state API.
- [ ] Add baseline/disruption scenarios and worker-availability endpoint.
- [ ] Implement interruption rollback and backend order verification.
- [ ] Add blockers, correctness/timeliness metrics, and terminal outcomes.
- [ ] Wire Person B's adapter and an explicitly selected deterministic test policy into the engine.
- [ ] Capture snapshots and export recordings.
- [ ] Add critical pytest scenarios listed in the plan.
- [ ] Build/deploy one authenticated Render service; retain one process/replica.
- [ ] Verify a fresh hosted run after reset.

## Person B — draft checklist

- [ ] Implement worker-specific prompts reflecting background and explicit capabilities.
- [ ] Implement strict-schema OpenRouter calls, Pydantic-compatible action output, timeout/retry, and explicit errors.
- [ ] Verify a schema-response smoke test before integration.
- [ ] Record model identity, action, short explanation, latency, and available usage metadata.
- [ ] Build target/clock/progress, orders/tasks, stations, ingredients, and worker panels.
- [ ] Show decisions, blockers, help requests, and accepted/rejected results.
- [ ] Connect backend polling, start/pause/step/reset, and disruption controls.
- [ ] Show AI/deterministic/replay mode and provider failures clearly.
- [ ] Add full JSON inspector, run export, and recorded-snapshot replay slider.
- [ ] Verify hosted baseline and disruption demonstrations.
- [ ] Update documentation with actual setup and demonstration steps.

## Shared draft release gate

- [ ] Hosted URL renders actual backend state.
- [ ] Four distinct worker profiles independently choose recorded AI actions.
- [ ] Concurrent tasks respect qualifications, dependencies, inventory, and station capacity.
- [ ] Task durations and order correctness are engine-enforced.
- [ ] Worker unavailability releases resources and supports qualified reassignment.
- [ ] Controls and stale-response handling pass verification.
- [ ] Exported recording can be inspected/replayed without model calls.
- [ ] Critical tests pass; any omissions are explicitly documented.
- [ ] Tag `v0.1-draft` after verification.

## Finalization after the draft

| Work | Owner | Estimate |
|---|---|---|
| Expand engine/API tests and fix failures | Person A | 4–5 hours |
| Prompt quality, repeated-wait handling, decision diagnostics | Person B | 2–3 hours |
| Scenario selection, replay import/filtering, dashboard polish | Person B | 3–4 hours |
| Clean setup/deployment verification, final demo, and handoff docs | Both | 3–4 total person-hours |

- [ ] Verify all original brief requirements against implemented behavior.
- [ ] Validate read-only recording import and replay consistency.
- [ ] Preserve baseline and disruption recordings.
- [ ] Document ephemeral state, configuration, and failure behavior accurately.
- [ ] Test the hosted demonstration from fresh setup.
- [ ] Publish repository/URL handoff and a three-minute demonstration.
- [ ] Tag `v1.0-demo` after final acceptance.

## Estimates and confidence

| Deliverable | Completion estimate | Confidence |
|---|---|---|
| Hosted first draft | 3 elapsed hours; 6 person-hours | 60/100 |
| Hosted draft with contingency | 5 elapsed hours; 10 person-hours | 85/100 |
| Finalized hackathon demo | Additional 12–16 person-hours; approximately 1–2 working days | 85/100 |

Confidence scores are engineering judgments, conditional on experience, AI coding assistance, ready accounts/API credits, and hosting/model availability. They are not measured probabilities or a claim that implementation is complete.
