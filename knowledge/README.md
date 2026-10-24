# Knowledge

Static SC2 references; use as background, not evidence of a particular match.

- `mechanics/`: economy, construction, combat, information and time.
- `units/`: units/buildings, costs and raw API data.
- `abilities/`: actors, effects and conditions.
- `upgrades/`: research locations and prerequisites.
- `tech_tree/`: production routes and dependencies under `nodes`, `upgrade_dependencies`, `rules`.

Outside `mechanics/`, files are `terran.json`, `protoss.json`, `zerg.json`. JSON entries use API identifiers.
With `read_reference`, omit `key` to list keys; use dotted keys for entries/fields.
Example: `read_reference(path="knowledge/tech_tree/terran.json", key="nodes.BATTLECRUISER")`.

`order_cost` is incremental; raw `data.api_cost` may include prior investment.
Missing/null means unknown, not zero. Raw `*_api` values retain API units; see `mechanics/time.json`.
Baseline: cached client 5.0.15.98370, burnysc2 7.3.0. Static data does not establish runtime action legality.
