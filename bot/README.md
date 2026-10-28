# Bot

Rule-based Terran Battlecruiser Bot. Paths below are relative to `bot/`.

```text
bot/
|-- entry.py             # Lifecycle and per-step execution
|-- components/
|   |-- tactics/         # Strategy goals and phases
|   |-- tasks/           # Compose behaviors into tasks
|   `-- behaviors/      # Propose basic action requests
|-- core/                # Module calls, unit ownership, action arbitration
|-- spatial/             # Map, pathfinding, placement
`-- telemetry/           # Native observations and execution logs
```

- **Entry:** `entry.py` captures observations, observes previous actions, updates unit ownership, runs the strategy, resolves requests, and writes execution logs. Strategy entry: `components/tactics/bc_rush.py`.
- **Module contract:** implement `async execute(frame, ...) -> bool` and invoke through `frame.run()`. `True` means control is handled; it does not prove command success.
- **Observations and services:** read `frame.game`; use `frame.units` for ownership and `frame.spatial` for map/path/placement queries. Propose requests through `frame.actions.propose()`; core arbitrates and submits commands.
- **Action evidence:** `proposed` is intent, `submitted` is locally queued, `observed` has native execution evidence, and `completed` has completion evidence. `unknown` means confirmation is unavailable.
- **Editing scope:** RSI edits only Python files in `components/{tactics,tasks,behaviors}/`; preserve the framework contracts.

```python
await frame.run(self.economy, priority=50, worker_target=28)
```

Logs use native `game_loop`, named `frame`; one observation is captured at the start of each Bot iteration. Events use `{frame,kind,data}`; interval events add an exclusive `end_frame`.

- `manifest.json` / `result.json`: match conditions, code/environment, and execution outcome.
- `state.jsonl`: native observation columns. The header declares `frame_step` (currently 2); each block stores only `start_frame` and value arrays. Index `i` maps to `start_frame + i * frame_step`; gaps start a new block. Enemy observations include visible enemies only.
- `events.jsonl`: native unit/building callbacks.
- `trace.jsonl`: every module call, request/status record, and exception. Module definitions and request parameters are written once; IDs link later records. JSONL headers declare the version and compact row columns.
- `analysis.json`: derived arrays sharing the state frame axis, derived events, configuration, and quality. The `view` field caches scalar observations, frame coordinates, counts, native events and execution summaries for queries; unit snapshots and call histories remain in the source logs. Missing values are null; check quality before drawing conclusions.
