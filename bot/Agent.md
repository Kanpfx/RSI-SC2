# Bot design notes

This document describes the current implementation, not a fixed strategy.
Keep it concise and consistent with the code; replace outdated descriptions
rather than accumulating a chronological log.

## Current approach

- Terran bot with a minimal single-base, Marine-only opening.
- Economy: distribute workers, train SCVs at ready idle bases when affordable,
  build Supply Depots below 4 free supply, and build one Barracks after a Depot
  is ready. Ready idle Barracks train Marines when affordable.
- Combat: once at least 12 Marines exist, order idle Marines to attack the
  opponent's first starting location.
- No explicit worker cap, gas collection, expansion, tech progression, upgrades,
  scouting, retreat or dedicated base-defense logic is implemented.

## Module responsibilities

- `main.py`: import-safe `SeedBot` entrypoint; each step observes, records,
  manages the economy, then controls combat.
- `modules/economy/`: separate worker allocation, construction, technology and
  production functions, called by `manage`. The technology hook is currently empty.
- `modules/combat/`: Marine attack orders.
- `modules/strategy/`: attack threshold and supply buffer, accessed through the
  `strategy` module without re-exporting individual constants.
- `modules/observation/`: in-memory resource, supply, worker, army and time snapshot.
- `modules/logger/`: resource samples every 10 iterations to `telemetry.json`,
  flushed every 500 samples and at game end. The snapshot is not logged.

## Latest change and open questions

Separated economy responsibilities and connected an empty technology hook to make
extension easier; observation and combat TODOs suggest optional directions.
The opening behavior and logging are unchanged. This is a minimal starting point,
not evidence of an effective strategy. Production capacity, army composition and
combat decisions should be assessed using the parent version's actual results.
Future updates should summarize the main problem, implemented change, rationale,
expected effect and remaining uncertainty. Candidate game results are not yet
available when this document is updated before submission.
