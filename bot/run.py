"""Run the modular bot independently: python -B -m bot.run."""
import argparse
import json
from pathlib import Path
from uuid import uuid4


def main():
    """Parse standalone options and delegate the match to the shared isolated runner."""
    from sc2.data import Difficulty

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default="AbyssalReefLE")
    parser.add_argument("--race", choices=("Terran", "Zerg", "Protoss"), default="Zerg")
    parser.add_argument("--difficulty", choices=tuple(Difficulty.__members__), default="Easy")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--time-limit", type=int, default=150)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument("--wall-timeout", type=int, default=600)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    from runner.matches import run_match
    output = args.output or root / "runtime/bot" / uuid4().hex
    conditions = dict(map=args.map, race=args.race, difficulty=args.difficulty, seed=args.seed,
                      game_time_limit=args.time_limit, wall_timeout=args.wall_timeout)
    result = run_match(root, root, conditions, output, log=not args.no_log)
    print(json.dumps(result, indent=2))
    return 0 if result["execution_status"] == "normal" else 1



if __name__ == "__main__":
    raise SystemExit(main())
