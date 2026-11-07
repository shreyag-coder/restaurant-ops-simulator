Build a multi-agent restaurant operations simulator.

The first version should focus on the backend simulation, with a clear
dashboard for inspecting the restaurant state and agent activity.

## Core concept

Maintain one structured restaurant-state document containing everything
needed to understand the restaurant at a given moment.

Separate AI agents represent restaurant workers.

Each agent:
1. Reads the current restaurant state.
2. Understands its own background, skills, and responsibilities.
3. Chooses an action it is capable of performing.
4. Submits that action.
5. Receives the result after the simulation validates and applies it.
6. Continues working toward the shared target.

Agents should independently choose their actions.
The backend enforces the rules and maintains consistent state.

## Shared restaurant state

Use a single JSON document as the authoritative state.

Include:

### Simulation
- Current simulation time
- State version
- Running, paused, completed, or timed-out status
- Shared target and deadline

### Stations
Examples: preparation counter, grill, frying area, assembly, packing.

For each station, record:
- ID and type
- Equipment and capacity
- Availability
- Current tasks
- Assigned or active workers

### Ingredients
- Ingredient ID and name
- Quantity and unit
- Available quantity
- Quantity reserved for active tasks
- Storage location

### Recipes
- Dish name
- Required ingredients and quantities
- Preparation steps and dependencies
- Required station and worker capabilities for each step
- Simulation duration for each step

### Orders
- Order ID
- Requested dishes, quantities, and modifiers
- Arrival time and deadline
- Required tasks and their dependencies
- Task progress
- Overall order status
- Final verification result

### Staff
- Agent ID and description
- Background and experience
- Dishes and tasks the agent can perform
- Explicit skills and qualifications
- Task-specific proficiency and duration modifiers
- Availability
- Current task and station
- Current status

### Activity history
- Agent decisions
- Accepted and rejected actions
- Task start and completion events
- Ingredient usage
- Blockers and requests for help
- Order status changes

## Agent profiles

Give agents distinct descriptions that affect their decisions.

Examples:
- An experienced grill cook who can also prepare ingredients.
- A preparation specialist who can assemble dishes.
- A worker who can fry food and pack orders.
- An expediter who can assemble, verify, and prepare orders for pickup.

Represent actionable capabilities as structured fields alongside the
natural-language descriptions.

An agent’s background should inform its choices, while explicit
capabilities determine which actions are allowed.

Agents must not invent qualifications or assume they can perform
every task.

## Agent actions

Use structured actions, such as:
- start_task(task_id, station_id)
- request_help(task_id, reason)
- report_blocker(task_id, reason)
- wait(reason)

Each decision should include a short explanation.

Agents should consider:
- Which tasks are ready
- Their own capabilities
- Order deadlines
- Ingredient availability
- Station capacity
- Work already being performed by other agents

Agents may choose different tasks and work concurrently.

## State updates and consistency

Agents submit proposed actions; they do not directly rewrite arbitrary
parts of the state document.

Validate and apply actions atomically.

Before starting a task, check that:
- The worker is available and qualified.
- Prerequisite tasks are complete.
- The task has not already been claimed.
- Required ingredients are available.
- The station has capacity.

Starting a task must reserve the required worker, station capacity,
and ingredients together.

Reject conflicting or stale actions with an explanation.
Let the agent read the updated state and choose again.

Use a simulation clock.
Tasks take their configured duration; they must not finish immediately
because an agent claims they are complete.

When a task finishes, the simulation engine updates the state,
consumes reserved ingredients, and releases resources.

The backend validates and advances the world.
Staff agents decide which work to undertake.

## Shared target

Example:
“Complete all 10 orders correctly within 30 simulated minutes.”

An order is complete only when:
- Every requested item and quantity is present.
- Modifiers are satisfied.
- Required recipe steps are complete.
- Packing and final verification have passed.

Track correctness and timeliness separately.

End the run when the target is reached or the time limit expires.
Report blocked work explicitly when the target cannot be achieved.

## Staffing disruption

Allow a worker to become unavailable during a run.

Other agents should observe the changed state and adjust their choices.

A specialist’s workload can be divided among multiple workers when
those workers have the capabilities required for the individual tasks.

If a required capability is missing, agents should report the blocker.
They must not invent a replacement skill.

Define what happens to interrupted tasks and release their resources
consistently.

## Dashboard

Build a dashboard showing:
- Shared target, simulation time, and progress
- Orders and task statuses
- Stations and current activity
- Available and reserved ingredients
- Staff profiles and current actions
- Agent decisions, blockers, and action results
- An expandable view of the complete state document

Provide start, pause, step, and reset controls.

The dashboard must render the actual backend state.

## First-draft scope

Use:
- One restaurant
- Four staff agents
- A small menu with explicit recipes
- Ten sample orders
- One shared restaurant-state document
- One worker-unavailability scenario

Prioritize the complete loop:
Read state → Decide → Validate action → Update state → Continue.

Make the event sequence inspectable and record model decisions so a
completed run can be replayed.