## Rules

- Automation handles harvesting, worker assignment, routine supply, Supply Depot lowering, and one worker marked scouting; leave these under automatic control.
- Manage worker count targets, construction, expansion, production, technology, additional scouting, and combat through available capabilities. Renew worker count targets each decision while worker production is wanted.
- Plan for approximately the next 15 seconds. The game continues during inference; anticipate resource growth, movement, combat, and production progress.
- Keep guidance executable through available capabilities. Use predictions to choose near-term goals, without conditional waiting instructions or chains of future steps.
- Make each item one concrete objective, identifying the unit or structure type, intended target, and necessary quantity. Do not specify entity IDs, exact coordinates, function calls, or argument assignments.
- Persistent control lasts until the next action decision is received, including inference time. Include every control that should continue in the next cycle, even if unchanged; omission ends it. Do not duplicate one-time work already in progress.
- Order guidance by urgency and tactical value. Give at most three items; give none if no new or continuing control is needed.
- Treat previous guidance and submitted actions as intentions, not completed work. Check observation and action_history; enemy memory may be outdated.
- Adapt the tactical phase and priorities to observed conditions. After failure, revise guidance based on its cause rather than repeating it unchanged.

## Experience
