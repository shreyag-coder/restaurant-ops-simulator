# Service dashboard

Start the local dashboard from the repository root:

```sh
python3 dashboard.py
```

Open **http://127.0.0.1:8765**. The dashboard uses Python's standard library and plain HTML/CSS/JavaScript; no npm installation is required. An internet connection loads the optional display fonts; system fonts are used when unavailable.

## Simple layout (current)

The dashboard shows only **Start new run** and **Pause**, a plain status next to them, colour-coded summary cards, the kitchen floor, the team chat, and the order board. The menu is Live kitchen, Task progress, Orders, and Team profiles. New runs use the ten-person **burrito line** on a **normal shift** (AI when the server has an OpenRouter key, otherwise rule-based); add problems from the chat box. The kitchen/scenario/policy pickers, Step, the recording picker, run ids, the replay timeline, engine details, the decision log, and the state inspector are hidden with CSS in `dashboard/web/workspace.css` (the "simple dashboard" block), not deleted, so they can be brought back by removing selectors there. The defaults are `SIMPLE_KITCHEN` and `SIMPLE_SCENARIO` in `workspace.js`. The hosted replay viewer keeps its recording picker. The sections below describe the full set of controls.

## Simulation controls

- Choose the **burger/fries kitchen** or the **ten-person burrito line**. Worker counts, skills, stations, and orders come from the selected configuration.
- **New run** creates a fresh run for the selected scenario/policy and processes its first tick. Existing recordings are kept.
- **Start / Continue** advances the real orchestrator one tick at a time, showing each committed checkpoint. A completed run starts a new recording.
- **Pause** stops automatic advancement after the current tick finishes. In AI mode, an outstanding model request can take time; accepted work is preserved at the checkpoint.
- **Step** advances a paused run by one tick, or creates a new single-tick run when the selected recording has ended.
- **Run timeline** selects saved snapshots. Its play button animates recorded history without progressing the simulator or calling a model. **Latest** returns to the current committed state.
- **Watch shift** plays the recorded kitchen and conversation together. Use the adjacent arrows to step through snapshots and the speed selector for 0.5×–4× playback. Station progress, working avatars, and queue counts reflect the selected snapshot.
- **Team conversation** shows original recorded decision explanations in chronological bubbles, accepted shared requests, and separately labeled engine updates. Click a teammate to filter their voice; filter by message type or search text. Task chips open the corresponding order. Follow mode keeps new messages in view; earlier history is expandable.
- Team/inventory/engine tables are under **Team, resources & engine details** so the main view stays focused on coordination.
- Select an order to inspect each task, prerequisites, ingredients, assigned worker, and start/finish times. The decision log supports event filters and text search. The state inspector displays the selected JSON snapshot, and the download icon exports it with its events.

The UI displays actual order correctness and individual deadline results separately. Deterministic decisions are labeled explicitly. AI mode becomes available when both `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` are set in the dashboard process's environment; keys are never sent to the browser. Configuring a model does not initiate calls: select AI mode and start a run to use it.

### Kitchen updates, task progress, and team profiles

- **Kitchen updates in the team chat.** Type into the box under **Team conversation**, for example "the fryer is broken", "Tomas is out", "out of avocados", "restock steak 4", "gas leak", or "say rush on O7". The local server passes the text to `orchestrator.py command`, which parses it like the live console, validates it, and queues it. The engine applies it at the start of the next simulated minute. An update sent while the clock is running waits for the current minute to finish. Text the parser doesn't recognize is passed to the workers as a message. The dashboard never calls the paid scenario agent. Your updates appear as dark **Kitchen manager** bubbles, and their effects appear as kitchen updates.
- **Task progress.** Every task in the selected minute has a percentage: active tasks show elapsed time against their duration, and orders show the average of their tasks. Rows are tinted green when completed, yellow while in progress, light red when delayed (blocked, past the order deadline, or due to finish after it), and grey while waiting. Click a status chip to filter.
- **Team profiles.** View and edit each worker's name, role, description, skills and speed multipliers, profile (experience, home station, shift, background, certifications, strengths, watch-outs), and persona text. Each save is checked against every scenario variant before it is written, so an edit that would break a scenario (for example, removing the only grill backup) is refused with the reason. Edits are written to `sim/<kitchen>/workers.json` and `sim/<personas>/<id>.txt` and apply to **new** runs. Recorded runs keep the team they started with. Changing skills changes the demo outcomes that `tests/test_chipotle_config.py` and `tests/test_integration.py` pin, so revert with git if needed. The hosted viewer shows profiles read-only and disables the update box.

### What counts as conversation

An AI decision's `why` is displayed as an **AI decision explanation**, with one display-only change: raw task and worker IDs (`O6.bu2.build`, `w_line`) are shown as plain words (“Build burrito” for O6, Bri). Hover the bubble to see the recorded wording; recordings are never altered. New AI decisions are asked to write in plain words in the first place. Task chips, station cards, and the order dialog also use task names instead of IDs; a deterministic policy's explanation is labeled **rule-based**. Existing recordings do not acquire invented messages or replies. Engine updates are identified as system messages.

For new runs, accepted `request_help` and `report_blocker` actions broadcast their exact explanation to a shared team channel. The engine records a `worker_message` event and retains recent messages in state; subsequent worker views include the latest twelve. Thus AI workers can see and respond to shared requests on later turns. Broadcasting does not reserve resources, grant qualifications, or guarantee another worker will answer. Old recordings remain replayable; a recording without peer messages says so explicitly.

The local server binds only to loopback and launches the existing `orchestrator.py` CLI as the state writer. It rejects overlapping dashboard controls and cross-origin control requests. It serves the dashboard assets and committed recordings, rather than exposing arbitrary project files.

Optional arguments:

```sh
python3 dashboard.py --port 8766 --runs-dir sim/runs
```

## Private hosted replay viewer

Published private viewer: https://service-kitchen-replay-shreya.atomic-shadow.chatgpt.site

The hosted companion uses the same interface in replay-only mode. It cannot execute the local Python process; simulation controls are disabled. It includes the most recent terminal recording for each available kitchen, policy, and scenario combination, with historical snapshots and events. AI recordings are labeled by model; playing them does not make new model calls.

Rebuild its static files after changing the viewer or recording new scenarios:

```sh
python3 dashboard/export_replay.py
```

Output is under `dashboard/replay-site/dist/`. The Sites manifest is under that project's `.openai/hosting.json`. This generated directory is excluded from the main repository and is published through its own private Site source repository. Export removes local config source paths, persona prompts, and continuation data; no runtime API credentials are exported.

Publishing is a separate step after export. The hosted viewer does not automatically synchronize new local runs.

## Verification

```sh
python3 -m pytest -q
node --check dashboard/web/app.js
python3 dashboard/export_replay.py
node tests/test_dashboard_ui.mjs
```

Dashboard checks exercise real CLI subprocesses, committed state, pause/step/resume, replay packaging, and the local HTTP boundary. Offline JavaScript checks render all three recordings and historical snapshots against a small DOM test double; they are not visual browser verification. Browser appearance and interactions require an available browser automation connection.
