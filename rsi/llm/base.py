from typing import Protocol


class LLM(Protocol):
    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Return an assistant message with content and optional tool_calls."""
        ...
