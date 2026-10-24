# Bot

Rule-based Terran Battlecruiser Bot. Paths below are relative to `bot/`.

- `entry.py`: lifecycle and game loop.
- `components/`: `tactics/` sets goals, `tasks/` composes behaviors, `behaviors/` proposes actions. Strategy entry: `tactics/bc_rush.py`.
- `core/`: calls, ownership and action arbitration; `spatial/`: map/path/placement; `telemetry/`: passive logs.

Components implement `async execute(frame, ...) -> bool`; call them through `frame.run`.
Read observations via `frame.game`, shared services via `frame.units` / `frame.spatial`,
and propose actions via `frame.actions.propose`. Core handles native commands and resource checks.
`True` means some control is handled, not command success: `proposed` is intent,
`submitted` is locally queued, `observed` is execution evidence.

RSI edits only `components/{tactics,tasks,behaviors}/` Python files; preserve framework contracts.
Example: `await frame.run(self.economy, priority=50, worker_target=28)`.
