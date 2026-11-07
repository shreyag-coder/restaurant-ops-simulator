# Restaurant Operations Simulator — File-Based Core-First Plan

Updated: November 7, 2026. This is a plan for future work, not a claim that the application is implemented.

## 1. Goal and working approach

Get the core restaurant simulation working first, using **plain markdown/text files as the state and communication layer** and **one orchestrator script** as the only writer of shared state. There are no internal APIs and no server. Both contributors guide AI coding tools through prompts, review the results, and verify behavior.

The original product requirements remain in [BRIEF.md](BRIEF.md). The brief's "single JSON document" and "backend" are realized here as `world.md` (structured, machine-readable blocks inside markdown) and the orchestrator script.

The essential loop is:

**Orchestrator gives agent the files → agent writes a proposal → orchestrator validates → orchestrator updates files → continue.**

Agents never edit `world.md` directly. They propose; the orchestrator decides. This keeps validation, simulated time, resource reservation, and order correctness authoritative, and it removes write races between agents.

### First-draft target and confidence

| Deliverable | Target | Confidence |
|---|---|---|
| Working core draft with real AI decisions and file-based inspection | 3 elapsed hours with two contributors; 6 person-hours | 60/100 |
| Working draft with contingency | Up to 5 elapsed hours; 10 person-hours | 85/100 |
| Polished demo (viewer, replay, docs) | Provisionally 10–14 additional person-hours after the draft | 70/100 |

These are engineering estimates, not measured probabilities. They assume familiarity with AI coding tools and ready OpenRouter credits. Dropping the API layer and server removes work, but the viewer and hosting are now optional extras. Re-estimate after the first complete run.

## 2. File layout

```
sim/
  target.md          # shared goal, deadline, scoring rules (read-only to agents)
  world.md           # authoritative state: clock, version, stations, ingredients, recipes, orders, tasks
  agents/
    <name>.md        # one per worker: profile, structured skills, current task, task history, last result
  proposals/
    <name>.md        # the agent's single proposal for the current turn (overwritten each turn)
  log/
    events.md        # append-only ordered event log (decisions, accept/reject, start/complete, blockers)
    snapshots/       # world.md copied per tick, e.g. t0000.md, t0005.md
orchestrator.py      # the only process that writes world.md, agent files, and log/
```

### File format rules

- Markdown for readability. **Facts live in fenced `json` blocks or YAML frontmatter** (clock, station occupancy, inventory, task status, skills). **Prose sections** hold background, explanations, and history.
- Code parses only the structured blocks. Models read both.
- `world.md` stays small: current snapshot only. History belongs in agent files and `log/`.
- Each agent file has fixed sections: Profile (prose), Capabilities (structured), Current state (structured), Last result (written by orchestrator), Task history (appended by orchestrator).
- A proposal file contains one structured action plus a short explanation. Allowed actions: `start_task(task_id, station_id)`, `request_help(task_id, reason)`, `report_blocker(task_id, reason)`, `wait(reason)`.

## 3. Orchestrator responsibilities

- Run the turn loop: for each idle agent, provide `target.md`, `world.md`, and that agent's file to the model (OpenRouter, strict JSON-schema output), and save the response to `proposals/<name>.md`.
- Validate proposals **one at a time, in a deterministic order**: worker available and qualified, prerequisites complete, task not already claimed, ingredients available, station capacity free.
- Apply valid proposals atomically (reserve worker, station capacity, and ingredients together). Reject invalid ones with a reason written to the agent's "Last result"; rejected actions leave resources unchanged.
- Advance the discrete simulation clock to the next task completion. On completion, consume reserved ingredients, release resources exactly once, update task and order status, and update the agent's history.
- Verify orders (quantities, modifiers, recipe steps, packing, final verification). Track correctness and timeliness separately.
- Apply the staffing disruption: mark a worker unavailable at a scheduled time, return unfinished tasks to pending, release reservations, and let qualified workers claim them with the duration restarting. Completed prerequisites stay complete.
- Report an explicit blocker when no qualified worker exists; never invent a skill.
- Append every decision and result to `log/events.md` and write a snapshot per tick.
- End the run when the target is reached or the deadline expires.
- Support deterministic mode (explicitly selected, used for tests) and replay mode (reads snapshots/log, makes no model calls). Never silently use deterministic mode during an AI run. Keep the API key in an environment variable and out of the repository.
- Handle model timeouts and invalid responses with bounded retries, then record a visible error and stop or pause rather than guessing.

## 4. What must work before we expand

Start with the smallest scenario: one order, two workers, explicit capabilities, ingredients, stations, and timed recipe steps. Use the deterministic policy to verify the engine, then connect real OpenRouter decisions before calling the AI core complete.

The working core must demonstrate:

- `world.md` as the single authoritative state with clock, version, tasks, workers, ingredients, and stations.
- Workers choosing tasks from their own files and the current state, according to explicit capabilities.
- Atomic validation and reservation; rejected actions leave state unchanged.
- Tasks taking configured simulated time and releasing resources exactly once.
- Overlapping tasks by different workers without duplicate claims.
- Order completion based on produced quantities, modifiers, recipe completion, packing, and verification.
- Clear explanations, rejection reasons, blockers, and outcomes in the log and agent files.

AI mistakes should be visible outcomes, not hidden by changing state or substituting scripted decisions.

Technology: Python 3.12 for the orchestrator, HTTPX (or the OpenRouter-compatible client) for model calls, pytest for tests. A web dashboard and hosting are optional later work (see §6).

## 5. Two-person prompting workflow

### Ownership

- **Person A — engine and files:** orchestrator loop, `world.md` structure, validation, reservations, simulated time, completion, order verification, disruption handling, snapshots, and pytest checks.
- **Person B — agents and visibility:** agent files and profiles, prompts, OpenRouter integration, proposal parsing, decision recording, then a viewer and replay.
- Person A owns `orchestrator.py` and `world.md`/`target.md` structure; Person B owns `agents/` content, prompts, and the model-call module. Avoid prompting separate tools to edit the same files at once.
- Agree on the file templates by looking at one real example of each, not a formal contract.

### Prompt 1 — prove the engine (Person A)

> Read BRIEF.md and PROJECT_PLAN.md. Build the smallest working file-based restaurant simulation. `sim/world.md` is the authoritative state (structured JSON blocks inside markdown), `sim/target.md` holds the goal, and each worker has `sim/agents/<name>.md`. A single `orchestrator.py` is the only writer of shared state; agents only write a proposal file. Start with one order, two workers, explicit capabilities, recipe dependencies, ingredients, and station capacity, using explicit deterministic worker choices. Validate proposals one at a time and reserve all resources together. Advance a simulated clock so tasks cannot finish early. Append decisions, rejections, starts, and completions to `sim/log/events.md` and write a snapshot per tick. Add focused checks for conflicting claims, missing skills, and inventory accounting. Show one complete run. Defer any dashboard and hosting.

**Checkpoint:** one order completes; an invalid proposal is rejected without corrupting resources.

### Prompt 2 — connect real worker decisions (Person B, integrated with Person A)

> Inspect the working simulation and connect independent worker decisions through OpenRouter. Give each worker an agent file with its own background and explicit capabilities. For each idle worker, pass target.md, world.md, and the worker's own file, and require a strict-schema proposal: start a task, request help, report a blocker, or wait, with a short explanation. Validate the response before it reaches the orchestrator. Keep the API key in an environment variable. Record the model, proposal, explanation, latency, and accepted/rejected result in the agent file and event log. Handle timeouts and invalid responses with bounded retries and a visible error. Keep deterministic mode explicitly selectable; never silently use it in an AI run. Verify one full AI-driven order.

**Checkpoint:** real model decisions drive the loop; only the orchestrator changes shared state.

### Prompt 3 — expand and test adaptation (Person A, Person B reviewing)

> Expand the proven loop to one restaurant, four workers, a small explicit menu, and ten sample orders. Use burgers and fries, quantity variations, and supported burger modifiers. Give workers distinct backgrounds and structured capabilities, including a qualified backup for the grill cook. Target completion within thirty simulated minutes. Add a baseline scenario and a scenario where the grill cook becomes unavailable at minute five. Interrupted unfinished tasks return to pending, release their reservations, and restart their duration when another qualified worker claims them; completed prerequisites remain complete. Verify that removing the backup qualification produces an explicit blocker instead of an invented skill. Record correctness and timeliness separately.

**Checkpoint:** the larger scenario stays consistent, disruption recovery works, and impossible work is explained.

### Prompt 4 — inspection and replay (Person B)

> Make runs easy to inspect. Add a replay mode that steps through `log/snapshots/` and `log/events.md` without calling the model, and a simple viewer (a terminal summary or a small static page that reads the files) showing target, time, progress, orders and tasks, stations, ingredients, worker activity, decisions, blockers, and action results, plus the full `world.md`. Support start, pause, step, and reset in the orchestrator CLI; reset archives the old run and discards pending proposals. Prioritize readable behavior over polish.

**Checkpoint:** both contributors can explain a run from the files or viewer and replay it.

### Prompt 5 — finalize (both)

> Review the implementation against BRIEF.md. Fix correctness gaps before polishing. Verify a fresh baseline run, a staffing disruption, invalid proposals, model failures, controls, and replay. Save baseline and disruption recordings, update setup instructions (including how to set the API key), and prepare a three-minute demonstration. Report remaining limitations accurately.

**Checkpoint:** a documented, repeatable demo from a fresh clone.

## 6. Optional later work

A web dashboard, a hosted deployment, and a real-time API are no longer part of the core. Add them only if time remains, reading the same files. Production persistence, multiple restaurants, and recipe editing remain out of scope.

## 7. Verification and scope control

Ask the coding tool to verify these scenarios as behavior is added:

1. Two workers propose the same task: only one succeeds.
2. Missing skill, unavailable ingredients, incomplete prerequisites, or a full station: reject without partial reservations.
3. A task stays active until its simulated finish time; completion releases resources exactly once.
4. Interrupting a task restores reservations consistently and allows qualified reassignment.
5. Wrong quantities or modifiers prevent order verification.
6. Missing replacement skills produce an explicit blocker.
7. A malformed or stale proposal file cannot change state.
8. Deterministic baseline results are repeatable; real AI results are recorded honestly even when they miss the target.
9. Replay reproduces recorded states without new model requests.
10. A hand-edited or malformed `world.md` is detected rather than silently accepted.

After each prompt, inspect the result and run a short scenario. A tool saying "done" is not sufficient evidence; rely on tests, logs, and snapshots. Keep an explicit list of unfinished requirements rather than calling an incomplete draft finalized.

Known tradeoffs of the file-based approach: markdown parsing is only as reliable as the structured blocks, so keep them strict; there is no live push to a UI; and model calls run per turn, so cost and latency grow with the number of workers and ticks.
