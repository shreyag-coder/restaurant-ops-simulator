# Restaurant Operations Simulator

A planned multi-agent restaurant simulator: four AI workers independently choose tasks through OpenRouter, while a Python backend validates actions, advances simulated time, and maintains one authoritative restaurant-state document.

**Current status: planning only. No application code, dependencies, or deployment have been created.**

## Project documents

- [Original requirements](BRIEF.md)
- [Architecture, delivery plan, and acceptance criteria](PROJECT_PLAN.md)
- [Two-person task assignments and checkpoints](TASKS.md)

## Proposed technology

| Component | Planned choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic, HTTPX |
| Dashboard | React, TypeScript, Vite |
| AI decisions | OpenRouter, strict JSON-schema responses |
| Tests | pytest and an explicitly selected deterministic worker policy |
| Hosting | One Render Docker service serving the dashboard and API |
| State and replay | One backend JSON state; exportable snapshots and decision/event history |

## Delivery estimates

| Milestone | Estimate | Confidence |
|---|---|---|
| First hosted draft | 3 elapsed hours with two people; 6 person-hours | 60/100 |
| Draft with contingency | 5 elapsed hours with two people; 10 person-hours | 85/100 |
| Finalized hackathon demo | Additional 12–16 person-hours; approximately 1–2 working days | 85/100 |

Estimates assume accounts and API credits are ready before implementation starts and both contributors are familiar with their assigned stack and use AI coding assistance. Confidence scores are engineering judgments, not measured probabilities.

## Ownership

- **Person A:** simulation engine, API, tests, and deployment.
- **Person B:** worker profiles, OpenRouter integration, dashboard, and replay interface.

Contributor names and GitHub collaborator invitations remain unassigned. The detailed work split is in [TASKS.md](TASKS.md).

## Planned demonstration

Complete ten orders within thirty simulated minutes, inspect worker decisions and resource reservations, make a worker unavailable, observe qualified reassignment, and replay the recorded run without further model calls.

The release target is a polished hackathon demo. Production persistence, multiple restaurants, and recipe editing are outside this release.
