# Two-Person Task Checklist

Backlog only; nothing here is done yet. Schedule, rules, and prompts are in [PROJECT_PLAN.md](PROJECT_PLAN.md) §8.

## Coordination

- Person A owns `orchestrator.py` and `sim/config/`. Person B owns `llm.py`, `sim/personas/`, the report, and replay.
- Only the orchestrator writes `sim/runs/`. Workers only return proposals.
- Short-lived branches (`feature/engine`, `feature/agents`), merged at each checkpoint.
- Keep the API key in `OPENROUTER_API_KEY`, never in commits. Git-ignore `sim/runs/` and `.env`.

## Person A — engine

- [ ] `menu.json` (burger, fries, modifiers, pack/verify), `scenario.json` (10 orders and 3 variants), state shape
- [ ] Task generation from recipes and modifiers
- [ ] Validation and atomic reservation, with rejection reasons
- [ ] Tick clock, completion, ingredient consumption, worker history
- [ ] `det` policy
- [ ] Disruption (interrupt and release) and blocker detection
- [ ] Order verification and the `correct` / `on_time` metrics
- [ ] `events.jsonl`, per-tick snapshots
- [ ] pytest scenarios 1–7 from the plan §9

## Person B — agents and visibility

- [ ] `workers.json` (4 workers, backup grill skill) and 4 persona `.txt` files
- [ ] Per-worker view builder (shape agreed with A)
- [ ] `llm.py`: strict schema, timeout, one retry, a raised error, and a smoke test
- [ ] `ai` policy, the re-ask round, and pausing on error; decision events with model and latency
- [ ] `report.txt` generator
- [ ] `resume` and `replay [--at T]` (pytest scenario 8)
- [ ] README setup and demo steps

## Release gate (`v0.1-draft`)

- [ ] A fresh clone runs `baseline`, `disruption`, and `blocker` in `det` mode
- [ ] An `ai` baseline run is recorded honestly, whether or not it meets the target
- [ ] Tests pass; any gaps are listed in the README
