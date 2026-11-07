# Restaurant Operations Simulator — Core-First Prompting Plan

Updated: November 7, 2026. This is a plan for future work, not a claim that the application is implemented.

## 1. Goal and working approach

Get the core restaurant simulation working first. Both contributors will guide AI coding tools through prompts, review the results, and verify behavior. We do not need to design API contracts, agree endpoint shapes, or follow a minute-by-minute implementation schedule in advance.

The original product requirements remain in [BRIEF.md](BRIEF.md). This plan changes the order of work: prove the simulation loop, add real AI decisions, demonstrate adaptation, then improve the dashboard and host the demo.

The essential loop is:

**Read state → worker chooses an action → backend validates it → state changes → continue.**

Prompt-driven development does not replace runtime validation. The backend still owns the restaurant state, simulation clock, resources, and order correctness. Worker agents propose actions and cannot rewrite arbitrary state or invent qualifications.

### First-draft target and confidence

| Deliverable | Target | Confidence |
|---|---|---|
| Working core draft with real AI decisions and basic inspection | 3 elapsed hours with two contributors; 6 person-hours | 60/100 |
| Working draft with contingency | Up to 5 elapsed hours; 10 person-hours | 85/100 |
| Polished hosted hackathon demo | Provisionally 12–16 additional person-hours after the draft | 85/100 |

These are engineering estimates, not measured probabilities. They assume familiarity with AI coding tools and ready accounts/API credits. Re-estimate after the first complete run. Hosting remains a final deliverable, but it must not delay proving the core behavior.

## 2. What must work before we expand

Start with the smallest scenario that can prove the loop: one order, two workers, explicit capabilities, ingredients, stations, and timed recipe steps. Use a deterministic test policy to verify the engine, then connect real OpenRouter decisions before calling the AI core complete.

The working core must demonstrate:

- One authoritative JSON state with a simulation clock, version, tasks, workers, ingredients, stations, and an inspectable activity history.
- Workers choosing tasks according to their explicit capabilities and the current state.
- Validation of availability, qualifications, dependencies, task ownership, ingredients, and station capacity.
- Atomic reservation of the worker, station capacity, and ingredients; rejected actions leave resources unchanged.
- Tasks taking configured simulated time, with completion consuming reserved ingredients and releasing resources.
- Different workers performing overlapping tasks without duplicate claims.
- Order completion based on produced item quantities, modifiers, recipe completion, packing, and final verification.
- Clear action explanations, rejection reasons, blockers, and outcomes.

Keep correctness and timeliness separate. The engine ends a run when the shared target is reached or the simulation deadline expires. AI mistakes should be visible outcomes, not hidden by changing the state or substituting scripted decisions.

Use the agreed technology direction: a Python backend, OpenRouter worker decisions, and a React dashboard when the core is ready. FastAPI, Pydantic, and Vite remain sensible starting choices, but detailed wiring should follow the working implementation rather than be a planning prerequisite.

## 3. Two-person prompting workflow

### Ownership

- **Person A — simulation correctness:** prompt and review state handling, validation, resources, simulated time, task completion, order verification, and focused tests.
- **Person B — worker decisions and visibility:** prompt and review worker profiles, OpenRouter integration, decision logs, then the basic dashboard and replay view.
- Person A owns the core simulation files; Person B owns agent integration and presentation files. Avoid prompting separate tools to edit the same files simultaneously.
- Coordinate using a real state snapshot and a short working run. Discuss integration details when needed; do not block on a formal API contract.

Use the prompts below in order of dependency. Person B can prepare profiles and the OpenRouter adapter while Person A proves the engine. Both should inspect the first end-to-end run before adding more features.

### Prompt 1 — prove the engine (Person A)

> Read BRIEF.md and PROJECT_PLAN.md. Build the smallest working Python restaurant simulation with one authoritative JSON state, one order, two workers, explicit capabilities, recipe dependencies, ingredients, and station capacity. Use deterministic worker choices initially to test the engine. Validate proposals atomically and reserve all required resources together. Advance a simulation clock so tasks cannot finish early. Print or record decisions, rejections, task starts/completions, and the final state. Add focused checks for conflicting claims, missing skills, and inventory accounting. Show one complete run before expanding the scenario. Defer the dashboard and deployment.

**Checkpoint:** one order completes through the real engine; an invalid action is rejected without corrupting resources.

### Prompt 2 — connect real worker decisions (Person B, integrated with Person A)

> Inspect the working simulation and connect independent worker decisions through OpenRouter. Give each worker its own background and explicit capabilities. Provide the current state and ask for a structured action with a short explanation: start a task, request help, report a blocker, or wait. Validate model responses before submitting them to the engine. Keep the API key server-side. Record the model, proposed action, explanation, and accepted/rejected result. Handle timeouts and invalid responses with bounded retries and a visible error or pause. Keep deterministic mode explicitly selectable for tests; never silently use it during an AI run. Reuse the current implementation and verify one full AI-driven order.

**Checkpoint:** real OpenRouter decisions drive the existing loop; the backend remains responsible for all state changes.

### Prompt 3 — expand and test adaptation (Person A, with Person B reviewing decisions)

> Expand the proven loop to one restaurant, four workers, a small explicit menu, and ten sample orders. Use burgers and fries, quantity variations, and supported burger modifiers. Give workers distinct backgrounds and structured capabilities, including a qualified backup for the grill cook. Target completion within thirty simulated minutes. Add a baseline run and a scenario where the grill cook becomes unavailable at minute five. Interrupted unfinished tasks return to pending, release their reservations, and restart their duration when another qualified worker claims them; completed prerequisites remain complete. Show the remaining workers adapting. Also verify that removing the backup qualification produces an explicit blocker instead of an invented skill. Record correctness and timeliness separately.

**Checkpoint:** the larger scenario remains consistent, disruption recovery works, and impossible work is explained.

### Prompt 4 — make the working core easy to inspect (Person B)

> Inspect the working backend and build a basic React dashboard around its actual state. Show target/time/progress, orders and tasks, stations, available and reserved ingredients, staff profiles and activity, decisions, blockers, and action results. Add an expandable full-state view and start, pause, step, and reset controls using the simplest integration that fits the existing implementation. Make pause freeze progression and reset discard stale outstanding decisions. Record ordered events and snapshots, support run export, and provide basic replay without calling the model again. Prioritize readable working behavior over visual polish.

**Checkpoint:** both contributors can explain a run from the dashboard and replay its recorded state changes.

### Prompt 5 — finalize and host (both)

> Review the implementation against BRIEF.md. Fix correctness gaps before polishing the interface. Verify a fresh baseline run, a staffing disruption, invalid actions, model failures, controls, and replay. Deploy the existing application as one Render service serving the backend and built dashboard. Keep one backend process and replica while state is in memory. Protect demo access and keep secrets server-side. Clearly document temporary state and export recordings before service restarts. Update setup instructions, save baseline/disruption recordings, and prepare a three-minute demonstration. Report remaining limitations accurately.

**Checkpoint:** a hosted URL demonstrates the verified behavior and the README explains how to run and present it.

## 4. Verification and scope control

Ask the coding tool to verify these meaningful scenarios as the corresponding behavior is added:

1. Two workers claim the same task: only one succeeds.
2. Missing skill, unavailable ingredients, incomplete prerequisites, or a full station: reject without partial reservations.
3. A task stays active until its simulated finish time; completion releases resources exactly once.
4. Interrupting a task restores reservations consistently and allows qualified reassignment.
5. Wrong quantities or modifiers prevent order verification.
6. Missing replacement skills produce an explicit blocker.
7. Pause and reset prevent old model responses from changing the stopped or new run.
8. Deterministic baseline results are repeatable; real AI results are recorded honestly even when they miss the target.
9. Replay reproduces recorded states without new model requests.

After each prompt, inspect the result and run a short scenario. If it fails, prompt for a focused fix before adding more features. Tests and logs support behavior checks; a tool saying “done” is not sufficient evidence.

For the three-hour draft, prioritize the complete AI-driven loop, resource correctness, timed tasks, disruption handling, and basic state inspection. A terminal or simple state viewer is sufficient while the core is being proven. Do not spend early time on dashboard styling, a prescribed folder layout, formal API contracts, or deployment configuration.

The finalized demo still needs the full dashboard, controls, ten-order scenario, replay, hosted URL, setup documentation, and a clear demonstration. Production persistence, multiple restaurants, and recipe editing remain outside this release. Keep an explicit list of unfinished requirements rather than calling an incomplete draft finalized.
