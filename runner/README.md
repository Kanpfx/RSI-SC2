# Shared SC2 execution

`runner` contains reusable execution utilities. It does not depend on RSI, its
diagnosis tools, or its acceptance policy.

- `matches.py`: `run_match`, `run_batch`, and the isolated game worker.
- `versions.py`: canonical file inventories, hashes, snapshots, and verification.
- `processes.py`: subprocess waiting, cancellation, and termination.
- `environment.py`: SC2 environment loading and game-time units.
- `artifacts.py`: UTF-8 JSON persistence and project-relative path conversion.
- `console.py`: optional progress output, independent of SDK event rendering.

## Match API

```python
from pathlib import Path
from runner.matches import run_match

project = Path.cwd()
result = run_match(
    project, project,
    dict(map="AbyssalReefLE", race="Zerg", difficulty="Easy", seed=1,
         game_time_limit=150, wall_timeout=600),
    project / "runtime/bot/example",
)
```

The second argument is either a source root containing `bot/`, or a verified
snapshot reference `{version, source_root}`. An ordinary source tree is hashed
without copying it; RSI uses immutable snapshots. Each match runs in a fresh
process so imports select that source rather than an already imported Bot.

`purpose` defaults to `standalone`; `batch_id` is optional. `environment` can be
passed explicitly or loaded from the project's `environment.json`. `log=False`
disables telemetry while retaining the manifest, result, and Replay.

The returned object is identical to `result.json`. Format v3 stores `schema_version`,
`execution_status`, `outcome`, `termination`, `last_frame`, `steps`, `phase`,
`mean_step_ms`, `max_step_ms`, and `error`; worker failures may add `exit_code`.
Unknown frame is null, not zero. Identity, version and directory are not copied
into the result; the replay's existence is checked directly. Errors contain a
stage/type/message and either a trace error reference or an infrastructure traceback.

The manifest stores conditions (`game_time_limit` and `wall_timeout` are seconds),
source identity, expected environment and recording settings. `actual` stores the
observed map hash, SC2 build/game version and Bot game step when available.
The worker derives its output directory from the manifest's parent directory.
Detailed formats are documented in [Bot telemetry](../bot/README.md).

`run_batch(project, version, conditions, directory, purpose, parallel=3,
wall_budget=None)` schedules these same single matches. The optional budget
caps worker timeouts after reserving the existing staggered startup interval.
`batch.json` indexes run directories and holds short result summaries; each
run's `result.json` remains authoritative. Interruption preserves completed
matches and records cancelled jobs without retrying them.

## Versions and paths

`Versions.freeze(source)` copies only `bot/` into a content-addressed snapshot.
`hash_files` is the sole hashing algorithm; it preserves the previous sorted
POSIX paths, byte contents, and null separators, including the `bot/` prefix.
Verification checks every retained snapshot file, including unexpected additions.
RSI's editable-file policy lives in `rsi.tools.validate`.

Path helpers accept `root=` explicitly; their default is this repository root.
Paths outside that root remain absolute. Existing absolute artifact references
remain readable.
