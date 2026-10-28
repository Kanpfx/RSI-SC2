"""Run sequential RSI iterations: python -B run_iteration.py --help."""
import argparse
import json
from pathlib import Path

from .experiment.config import load_config
from .experiment.run import run_experiment


def main():
    """Expose only the initial source, round count and experiment configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--parent", type=Path, help="Source directory containing bot/; defaults to this repository")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    try:
        report = run_experiment(Path(__file__).resolve().parents[1], load_config(args.config),
                                parent=args.parent, rounds=args.rounds)
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps(dict(status="configuration_error", reason=str(exc)), ensure_ascii=False))
        return 2
    return 130 if report["status"] == "interrupted" else 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
