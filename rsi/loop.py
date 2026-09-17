import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from rsi.config import load_config
from rsi.console import elapsed, log
from rsi.evaluation.runner import preflight
from rsi.evolution.runner import Evolution
from rsi.evolution.state import redact
from rsi.llm.client import Client


def main():
    parser = argparse.ArgumentParser(description="SC2-RSI minimal evolution loop")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--preflight", action="store_true", help="Check setup without LLM calls or games")
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[1]
        load_dotenv(root / ".env", override=False, encoding="utf-8-sig")
        config = load_config(args.config)
        report = preflight(root, config)
        if args.preflight:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        llm = Client()
        evolution = Evolution(root, config, llm)
        evolution.record["environment"] = report
        summary = evolution.run()
        best = summary["best"]
        log("Completed", f"evaluated={summary['evaluated_nodes']} "
                         f"failed={summary['failed_attempts']} "
                         f"elapsed={elapsed(summary['elapsed'])}")
        if best:
            log("Best", f"{best['id']} {best['wins']}/{best['games']} commit={best['commit'][:12]}")
        if summary["usage"]:
            log("Usage", json.dumps(summary["usage"], ensure_ascii=False))
        log("Output", summary["output"])
        return 0
    except Exception as exc:
        log("Error", redact(str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
