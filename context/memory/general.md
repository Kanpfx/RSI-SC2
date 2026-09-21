## Rules

- Automation handles harvesting, worker assignment, routine supply, and Supply Depot lowering; these run on their own and should not be controlled or adjusted.
- Automation sends one worker marked scouting around enemy starts and expansion locations; leave this worker under automatic control.
- You control worker growth, production, technology, scouting, and combat through the available actions; other tasks have no automatic fallback.
- Use only explicitly provided ability names and other named parameter values; do not guess or try unlisted alternatives.
- When several units should perform the same behaviour, prefer one group action over repeated single-unit actions.
- Assign each unit to at most one control action per decision, including membership in group actions; do not combine a group order and an individual order for the same unit.
- Worker production starts only after BuildWorkers; its target persists until replaced.
- Continuous instructions remain active until replaced or their units disappear; omitting them does not cancel them. They are marked [Persistent action] in the action list and appear under active in the history.
- Do not resend a continuous action that is already in effect; either send a new version with different arguments or leave it as is.
- For persistent macro controllers of the same action type, a new configuration fully replaces the previous one; configurations are not merged.
- Submitted actions are not necessarily completed. Resource-limited construction may queue temporarily.
- The game continues while you decide; actions are checked against the latest state.

## Experience

- Adapt tactical guidance to the current situation rather than following phases rigidly.
- Distinguish observed facts from hypotheses, especially about unseen enemies.
- Check feedback and pending work before retrying; avoid duplicate orders.
