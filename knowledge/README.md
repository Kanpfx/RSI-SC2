# Knowledge

Static SC2 references for mechanics, entities and dependencies.

```text
knowledge/
|-- mechanics/          # Economy, construction, combat, information, time
|-- units/              # Units/buildings, costs, raw API data
|-- abilities/          # Actors, effects, conditions
|-- upgrades/           # Research locations and prerequisites
`-- tech_tree/          # Production routes and dependencies
```

- **Organization:** outside `mechanics/`, files are `terran.json`, `protoss.json`, and `zerg.json`. Entries use API identifiers. Tech-tree sections are `nodes`, `upgrade_dependencies`, and `rules`.
- **Lookup:** with `read_reference`, omit `key` to list keys; use dotted keys to read an entry or field.
- **Costs and units:** `order_cost` is incremental; `data.api_cost` may include prior investment. Raw `*_api` values retain API units; see [mechanics/time.json](mechanics/time.json).
- **Interpretation:** missing/null means unknown, not zero. Static references provide background; match claims require match evidence, and action legality requires runtime checks.

```python
read_reference(path="knowledge/tech_tree/terran.json", key="nodes.BATTLECRUISER")
```

Data baseline: cached SC2 client 5.0.15.98370; burnysc2 7.3.0.
