# Two-Person Delivery Tasks

This is the implementation backlog, not a record of completed work. All tasks start unchecked. See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the file layout, orchestrator behavior, prompts, and tests.

## Ownership and coordination

- **Person A:** `orchestrator.py`, `world.md`/`target.md` structure, validation, reservations, simulated time, disruption, order verification, snapshots, and engine tests.
- **Person B:** agent files and profiles, prompts, OpenRouter module, proposal parsing, decision recording, then viewer and replay.
- Only the orchestrator writes `world.md`, agent files, and `log/`. Agents write only `proposals/<name>.md`.
- Agree the file templates by reviewing one real example of `world.md`, one agent file, and one proposal in the first 15 minutes. Person A owns later format changes; Person B reviews them.
- Use short-lived branches (`feature/engine`, `feature/agents`). Integrate at the checkpoints below.
- Keep the API key in an environment variable and out of commits. No internal APIs or servers.

## Draft schedule (about 3 elapsed hours)

The clock starts once the repo, OpenRouter credits, and Python environment are ready.

| Elapsed time | Person A | Person B | Checkpoint |
|---|---|---|---|
| 0–15 min | Draft `world.md` and `target.md` templates | Draft agent file and proposal templates | Agree the file formats |
| 15–60 min | Orchestrator loop, clock, validation, reservations (deterministic policy) | Four agent profiles, OpenRouter call with strict schema | One order completes; one AI proposal parsed |
| 60–100 min | Completion, order verification, event log, snapshots | Connect model proposals to the loop, record decisions | Real AI starts and completes tasks |
| 100–140 min | Ten-order scenario, disruption and interrupted-task recovery | Prompt tuning, wait/blocker handling, error handling | Baseline and disruption runs |
| 140–170 min | Critical pytest scenarios, fixes | Replay mode and simple viewer | Inspectable, replayable run |
| 170–180 min | Fresh-run and reset checks | Documentation | Verified `v0.1-draft` |

If behind at minute 100, stop viewer and polish work. Do not weaken validation, hand-edit state to fake progress, silently replace AI decisions, or skip simulation timing. Record any remaining acceptance gaps.

## Person A — draft checklist

- [ ] Define `world.md` and `target.md` structure (structured JSON blocks plus prose) and a representative fixture.
- [ ] Seed two recipes, ten orders, ingredients, stations, and four workers.
- [ ] Build item-level dependency graphs plus order packing and verification.
- [ ] Parse and strictly validate `world.md` and proposal files; reject malformed input.
- [ ] Implement one-at-a-time validation, atomic reservations, versioning, and rejection with reasons.
- [ ] Implement the discrete-event clock, proficiency durations, completion, and resource release.
- [ ] Update agent files (current state, last result, history) from the orchestrator only.
- [ ] Add start/pause/step/reset CLI commands and stale-proposal protection.
- [ ] Add baseline and disruption scenarios with interruption rollback.
- [ ] Add blockers, correctness/timeliness metrics, and terminal outcomes.
- [ ] Append `log/events.md` and write per-tick snapshots.
- [ ] Add deterministic test policy (explicitly selected) and critical pytest scenarios from the plan.

## Person B — draft checklist

- [ ] Write four agent files with distinct backgrounds and explicit structured capabilities.
- [ ] Write worker prompts that include the agent's background and capabilities.
- [ ] Implement strict-schema OpenRouter calls with timeout, bounded retry, and explicit errors.
- [ ] Verify a schema-response smoke test before integration.
- [ ] Parse proposals into `proposals/<name>.md` and hand them to the orchestrator.
- [ ] Record model identity, action, explanation, latency, and usage metadata.
- [ ] Show AI/deterministic/replay mode and provider failures clearly.
- [ ] Build replay over `log/snapshots/` and `log/events.md` with no model calls.
- [ ] Build a simple viewer (terminal summary or static page reading the files).
- [ ] Verify baseline and disruption demonstrations.
- [ ] Update documentation with actual setup and demo steps.

## Shared draft release gate

- [ ] A fresh run produces `world.md`, agent files, a log, and snapshots from the seed files.
- [ ] Four distinct worker profiles independently choose recorded AI actions.
- [ ] Concurrent tasks respect qualifications, dependencies, inventory, and station capacity.
- [ ] Task durations and order correctness are enforced by the orchestrator.
- [ ] Worker unavailability releases resources and supports qualified reassignment.
- [ ] Pause, step, reset, and stale-proposal handling pass verification.
- [ ] A recording can be replayed without model calls.
- [ ] Critical tests pass; any omissions are explicitly documented.
- [ ] Tag `v0.1-draft` after verification.

## Finalization after the draft

| Work | Owner | Estimate |
|---|---|---|
| Expand engine tests and fix failures | Person A | 3–4 hours |
| Prompt quality, repeated-wait handling, decision diagnostics | Person B | 2–3 hours |
| Scenario selection, replay filtering, viewer polish | Person B | 2–3 hours |
| Fresh-setup verification, final demo, handoff docs | Both | 3–4 total person-hours |

- [ ] Verify all original brief requirements against implemented behavior.
- [ ] Preserve baseline and disruption recordings.
- [ ] Document file formats, configuration, and failure behavior accurately.
- [ ] Test the demo from a fresh clone.
- [ ] Publish repository handoff and a three-minute demonstration.
- [ ] Tag `v1.0-demo` after final acceptance.
- [ ] Optional: web dashboard and hosting reading the same files.

## Estimates and confidence

| Deliverable | Completion estimate | Confidence |
|---|---|---|
| First draft | 3 elapsed hours; 6 person-hours | 60/100 |
| Draft with contingency | 5 elapsed hours; 10 person-hours | 85/100 |
| Finalized demo | Additional 10–14 person-hours | 70/100 |

Confidence scores are engineering judgments, conditional on experience, AI coding assistance, ready API credits, and model availability. They are not measured probabilities or a claim that implementation is complete.
