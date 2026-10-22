# Modular BC Bot

Rule-based SC2 Bot. Business logic lives in `components/`.

## Run

From the project root, using the existing `sc2rsi` environment:

```powershell
python -B -m bot.run --time-limit 150
python -B -m unittest scripts.test.test_bot -v
```

Environment: `environment.json`. Output: `runtime/bot/<id>/`.
Use `--help` for options; `--time-limit` uses game seconds.

## Structure

| Path | Responsibility |
| --- | --- |
| `entry.py` | BotAI lifecycle and sole game loop |
| `components/tactics/` | Global goals, task allocation, priorities |
| `components/tasks/` | Local goals and behavior composition |
| `components/behaviors/` | Combat, economy, production, movement |
| `core/` | Call tracking, unit ownership, action arbitration |
| `spatial/` | Map, threat-aware paths, placement reservations |
| `telemetry/` | Passive execution logs and state samples |

## Module contract

```python
async def execute(self, frame, *, tags, target) -> bool:
    ...

await frame.run(module, tags=tags, target=target, priority=80)
```

- Read native observations through `frame.game`; use `frame.units` and `frame.spatial` for shared services.
- Call modules through `frame.run`; disabled calls require `skip_reason`.
- Propose actions through `frame.actions.propose(...)`. Never issue native Unit/BotAI commands in components.
- `True` means control is handled for at least one subject, including maintained orders. It does not prove all subjects acted or a command succeeded.
- Higher priorities arbitrate first; ties preserve proposal order, and explicit queues preserve source order.
- Propose eligible macro actions even when resources are insufficient; Actions owns affordability checks and optional saving budgets.
- Unsubmitted requests expire each frame. Submitted actions and construction reservations persist until resolved or timed out.
- `submitted` means locally queued; `observed` means execution evidence. Construction remains tracked until `completed` or `failed`.
- Logs link calls and actions through `call_id`, `parent_id`, `request_id`, and game frames.

## RSI boundary

RSI may edit Python files under `components/{behaviors,tasks,tactics}/`.
Keep Core, Spatial, telemetry and execution contracts fixed.
Run RSI with `python -B -m rsi.run --help`.
