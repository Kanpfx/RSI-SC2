# Bot

A rule-based Terran bot built on burnysc2. Tactics own strategy goals and state;
behaviors propose actions; core allocates units and arbitrates execution.
The default strategy is [BcRush](components/tactics/bc_rush.py), registered in `entry.py`.

## Layout

```text
entry.py                    # Game lifecycle, tactic registration and frame loop
components/tactics/         # Strategy goals, thresholds, priorities and persistent state
components/behaviors/       # Reusable economy, production, combat and movement actions
  helpers.py                # Group-aware unit selection and shared helpers
core/                       # Frame orchestration, unit ownership, action arbitration,
                            # resource reservations and construction tracking
spatial/                    # Map information, pathfinding and building placement
telemetry/logger.py         # Collector definitions and event hooks
telemetry/recorder.py       # Sampling, buffered binary tables and their index
run_local.py                # Local games against the built-in AI
run.py                      # Join games hosted by an external manager
```

## Execution and Contracts

Frame order: `logger.observe → frame.begin → request_units → units.resolve → execute → frame.resolve → frame.end → logger.consume`.

- `request_units()` only declares claims; `execute()` composes behaviors and returns
  `None`. Behaviors return `bool`: `True` means an intent is handled or maintained,
  not that a command was accepted or executed.
- Declare each unit group once per frame. Groups are exclusive; use
  `subjects(frame, tags)` to select units owned by the current group.
- Use stable group/behavior keys; the tactic key is prefixed automatically.
  Claim priority controls unit ownership; `frame.run(..., priority=...)` controls
  action arbitration. These are independent; higher values win.
- Compose behaviors through `frame.run`; submit requests through `frame.actions`.
  Core dispatches native commands. Use `maintained()` to avoid duplicate orders.
- Reuse `build_structure` and `addon`; core tracks construction and builder leases.
  `save_if_unaffordable=True` reserves resources for a higher-priority request.
  `submitted` means locally queued; later observations confirm execution.
- `frame.game` exposes native observations; `frame.spatial` provides map, pathing
  and placement services. Internal timing uses `game_loop` frames; `GAME_STEP = 2`.
  Keep strategy state in tactics and reusable actions in behaviors.

## Telemetry

In `Logger`, `_configure` registers datasets, `_event` handles native callbacks,
and `_execution` receives detached frame records. Preserve the logger lifecycle.
Defaults: `minerals` and `kill_value` series every 112 frames, plus construction events.

Use `Recorder.add_series` for periodic samples and `add_events` for event tables.
Fields require fixed scalar NumPy dtypes. A series callback returns a tuple in
field order and must not mutate game state or issue commands. For example, in
`_configure`, after `recorder = self.recorder`:

```python
recorder.add_series(
    "gas", fields=[("vespene", "<i8")],
    sample=lambda game: (game.vespene,), every_frames=112,
)
```

Samples run before decisions. Each table stores its own actual `frame` axis;
missing observations are not zeros. Recording ranges use `[start_frame, end_frame)`.
Datasets are NumPy structured arrays in `<name>.bin`; `overview.json` records
schemas, ranges, row counts, status and errors. Read a recorded table with:

```python
from bot.telemetry.recorder import read_dataset

gas = read_dataset("path/to/match", "gas")
frames, values = gas["frame"], gas["vespene"]
```

## Usage

Use Python 3.12 with project dependencies installed. From the project root:

```powershell
python -B -c "import bot.entry"
python -B -m bot.run_local --map AbyssalReefLE --race Zerg --difficulty Easy --seed 1 --time-limit 150
python -B -m bot.run_local --help
```

Local games require SC2 and the selected map; set `SC2PATH` for a custom installation.
`--time-limit` uses game seconds. Results, telemetry and replay default to a unique
directory under `runtime/bot/`; `--output` selects another directory.
The external `run.py` entry receives connection arguments from the game manager.
