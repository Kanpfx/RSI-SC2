# SC2 knowledge library

English reference entries for standard Legacy of the Void multiplayer. Content is grouped by subject and stored as JSON. This README contains the directory guide, field conventions, baseline, sources, and maintenance notes.

## File tree

```text
knowledge/
├── README.md                     # Directory guide and reference conventions
├── mechanics/
│   ├── economy.json              # Resources, workers, supply, costs, production capacity
│   ├── construction.json         # Building lifecycle, add-ons, power, creep, Larva, morphs
│   ├── combat.json               # Target categories, armor, shields, splash, movement, energy
│   ├── information.json          # Vision, detection, burrow, terrain, pathing, transport
│   └── time.json                 # Game loops, API units, static and runtime availability
├── units/
│   ├── terran.json               # Terran units, buildings, add-ons, modes, temporary units
│   ├── protoss.json              # Protoss units, buildings, support units, temporary effects
│   └── zerg.json                 # Zerg units, buildings, morphs, cocoons, temporary attackers
├── abilities/
│   ├── terran.json               # Terran abilities: actor, effect, conditions
│   ├── protoss.json              # Protoss abilities: actor, effect, conditions
│   └── zerg.json                 # Zerg abilities: actor, effect, conditions
├── upgrades/
│   ├── terran.json               # Terran research: location, effect, prerequisites
│   ├── protoss.json              # Protoss research: location, effect, prerequisites
│   └── zerg.json                 # Zerg research: location, effect, prerequisites
└── tech_tree/
    ├── terran.json               # Terran production routes, attachments, upgrades
    ├── protoss.json              # Protoss production routes, conversions, merges
    └── zerg.json                 # Zerg production routes, morphs, tier requirements
```

The library contains 29 mechanics entries, 134 unit/building/form entries (123 core records plus 11 supplementary entries), 80 ability entries, 59 upgrade entries, and 123 technology nodes. Diagnosis, project conventions, and tactical reminder documents are removed.

## Entry conventions

Each JSON file is an object keyed by entry identifier. Unit identifiers use API names such as BATTLECRUISER or LURKERMP. General concepts use descriptive uppercase identifiers such as WORKER_SATURATION. Some supplementary entries group related states, for example EGGS_AND_COCOONS.

Files in `units/` and `tech_tree/` keep each entry on one line within standard JSON objects; file extensions and object structure are unchanged.

- **Mechanics:** `title` and `content` hold the subsection heading and its explanation.
- **Units:** `name`, `category`, `acquisition`, and `description` hold the encyclopedia entry. `order_cost` records an available incremental mineral/gas price. `data` retains the original exported API fields without changing their values. Supplementary entries may lack numeric data.
- **Abilities:** `name`, `actor`, and `description` identify the ability and explain its effect and conditions.
- **Upgrades:** `name`, `research_location`, and `description` identify the research, effect, and requirements. `api_name` is included where the original entry explicitly recorded it.
- **Technology trees:** `nodes` maps API identifiers to production routes; `upgrade_dependencies` records research-family gates; `rules` records race-specific exceptions and important paths. A route's `producer` is the producing/source actor, `requires` lists additional completed global tech, and `attached_addon` applies to that particular producer. Other fields specify transformations, merging, power, placement, or energy gates. Requirements inherit their own upstream dependencies. Transform routes reuse an asset and can legitimately form cycles.

Read the relevant file and entry. Content is not intended to be concatenated into a default prompt. Roles and interactions are conditional reference knowledge, not fixed counter rankings or evidence of a particular match state.

## Baseline and numeric fields

- Reviewed on **2026-10-21**. Cached client: **5.0.15.98370**; data build: **98370**; map: **AbyssalReefLE**; SDK: **burnysc2 7.3.0**.
- Cached data version: `84197ED58A0329FA79CC9426E07E5FB3`. The environment separately labels the installed game 5.0.17.98370; it does not change the cache's identity.
- `order_cost` uses the pinned SDK's incremental-cost corrections, excluding an already-owned morph source. Zergling cost buys a pair; `units_per_order` is 2, while `data.supply` describes one resulting unit. Costs for direct training and a free mode change are different concepts.
- `order_cost: null` means no reliable independent production price was recorded; it is not a zero-cost assertion. Transport Overlord and Greater Spire surcharges are intentionally unspecified because the cached totals/SDK calculation do not reliably establish them. A mode change does not purchase a second unit.
- `data.api_cost` is the original `[minerals, gas]` unit-data value, which may include prior investment and is not universally an order price. `data.producer` is an association from the official enum header and may be incomplete. `data.requires_api` is a partial API prerequisite, not the complete technology tree; `require_attached` applies to that API field.
- `build_time_api`, `speed_api`, and weapon `period_api` retain raw API units. Recorded match loops use the project's 22.4 loops/second convention; wall time and movement-speed units are separate.
- Raw `null`/missing fields mean unavailable, not zero or nonexistent. Missing weapons do not establish that a unit is unarmed: damage may use abilities, turrets, or sub-units. Health/shield maxima and unsupported spell values are not fabricated. Static availability is not runtime action legality.

## Sources

The original 122 exported records are preserved inside the race files' `data` fields. Cyclone is additionally read from the same cached response; the original compact export omitted it. Production/research relationships use installed SDK tables, with the Cyclone attachment requirement checked against cached client data. Numeric references belong to this baseline rather than an assumed latest ladder patch.

- [Blizzard data protocol](https://github.com/Blizzard/s2client-proto/blob/master/s2clientprotocol/data.proto): raw unit, weapon, ability, and upgrade fields.
- [Blizzard query protocol](https://github.com/Blizzard/s2client-proto/blob/master/s2clientprotocol/query.proto): runtime ability, placement, and path queries.
- [Blizzard API identifiers](https://github.com/Blizzard/s2client-api/blob/master/include/sc2api/sc2_typeenums.h): unit/ability associations.
- [python-sc2](https://github.com/BurnySc2/python-sc2): installed version 7.3.0; `unit_train_build_abilities.py`, `unit_trained_from.py`, `unit_research_abilities.py`, `unit_abilities.py`, and `game_data.py`.
- Historical Blizzard guides: [resources](https://news.blizzard.com/en-us/article/4488900/game-guide-resources), [economy](https://news.blizzard.com/en-us/article/4488313/game-guide-economy), [add-ons](https://news.blizzard.com/en-us/article/5740265/game-guide-terran-add-ons), [Zerg production](https://news.blizzard.com/en-us/article/5740264/game-guide-zerg-basics), [detection](https://news.blizzard.com/en-us/article/4546770/game-guide-detection), [Queen abilities](https://news.blizzard.com/en-us/article/5967865/game-guide-zerg-queen-tips), and [Protoss combat](https://news.blizzard.com/en-us/article/5963107/game-guide-protoss-attacks). These support stable mechanics; old numerical values are not imported.
- [Blizzard 5.0.15 balance notes](https://news.blizzard.com/en-us/article/24225313/starcraft-ii-5-0-15-patch-notes): context for version-sensitive research and abilities.

Cached official response SHA-256: `72c4a244601b22f7445346a0f07cb8d57b9480adfee0a413d1ab019912845813`.
Official enum-header SHA-256: `b2f9791efe8eaf4d44535a41d68c834ac37d197f3a8db76b82ac4eabf38265f2`.
The old raw response, header, and client-metadata cache were removed during cleanup. Static facts remain in this directory; the hashes above record their original provenance.

## Maintenance and integration

RSI (`rsi/`) indexes these directories and retrieves individual entries from a frozen per-iteration copy. The legacy export script still references the former layout. The existing export script would recreate `knowledge/sc2/units.json`, not update these JSON files. Review content and provenance together when changing the baseline.
