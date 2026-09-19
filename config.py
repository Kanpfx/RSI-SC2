"""Runtime settings and project-local environment loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GameConfig:
    model_interval_seconds: float = 60 * 2 / 22.4
    max_actions_per_decision: int = 8
    max_action_units: int = 12
    max_action_targets: int = 8
    max_point_nudge_tiles: float = 2.0
    deferred_action_ttl_iterations: int = 180
    resource_queue_mineral_tolerance: int = 120
    resource_queue_vespene_tolerance: int = 60


@dataclass(frozen=True)
class LLMConfig:
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 2048
    timeout_s: float = 20.0
    transport_retries: int = 2

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(model=os.getenv("LLM_MODEL", ""),
                   base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
                   api_key=os.getenv("LLM_API_KEY", ""))

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url and self.api_key)


def load_environment() -> None:
    """Load the agent-local .env without replacing existing shell variables."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_environment(keys: list[str]) -> None:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        raise RuntimeError("Missing model configuration: " + ", ".join(missing))
