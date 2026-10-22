"""Run the modular Bot from the project command-line entry."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.run import main


if __name__ == "__main__":
    raise SystemExit(main())
