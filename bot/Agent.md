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

- `main.py`: import-safe `SeedBot`; records telemetry, manages the economy,
  then controls combat each step, and saves logs at game end.
- `strategy.py`: shared parameters and goal selection; currently selects an
  attack destination once 12 Marines exist. No persistent strategy state yet.
- `economy.py`: worker allocation, construction, technology and production;
  the connected technology hook is currently empty.
- `combat.py`: executes the selected goal with idle Marine attack orders.
- `telemetry.py`: owns state snapshots and logging without making decisions.
  Records iteration/minerals/vespene every 10 iterations, flushing every 500
  samples and at game end. `event(time, kind, **fields)` accepts optional
  decision events; callers supply the reason. Richer snapshot fields are not
  yet persisted. Decision modules read current BotAI data directly.

## Latest change and open questions

Flattened modules, merged observation into telemetry, and separated attack goal
selection from unit orders. Optional events allow future decision explanations.
The opening and default resource log format are unchanged. This is a starting point,
not evidence of an effective strategy. Production capacity, army composition and
combat decisions should be assessed using the parent version's actual results.
Future updates should summarize the main problem, implemented change, rationale,
expected effect and remaining uncertainty. Candidate game results are not yet
available when this document is updated before submission.
