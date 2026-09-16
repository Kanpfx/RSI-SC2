from pathlib import Path

import yaml


def load_config(path):
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    try:
        for group, key in (("project", "max_generations"), ("agent", "max_steps"),
                           ("search", "beam_width"), ("search", "branch_factor"),
                           ("evaluation", "game_timeout_sec"), ("tools", "bash_timeout_sec")):
            value = config[group][key]
            if type(value) is not int or value < 1:
                raise ValueError(f"{group}.{key} must be a positive integer")
        evaluation = config["evaluation"]
        if type(evaluation["games"]) is not int or evaluation["games"] != 3:
            raise ValueError("evaluation.games must be 3 in v1")
        if (evaluation["realtime"] is not False or evaluation["bot_race"] != "Terran"
                or evaluation["opponent"] != {
                    "race": "Terran", "difficulty": "CheatVision", "build": "RandomBuild"}):
            raise ValueError("v1 requires non-realtime Terran vs Terran / CheatVision / RandomBuild")
        if not isinstance(evaluation["map"], str) or not evaluation["map"].strip():
            raise ValueError("evaluation.map is required")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Invalid or missing configuration: {exc}") from exc
    return config
