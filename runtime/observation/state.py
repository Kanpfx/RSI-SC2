"""Cross-frame state that has no dependency on a particular LLM provider."""

from __future__ import annotations


class TagIdMapper:
    """Compact IDs that are never reused after a unit dies."""

    def __init__(self) -> None:
        self._tag_to_id: dict[int, int] = {}
        self._next_id = 0

    def alias(self, tag: int) -> str:
        if tag not in self._tag_to_id:
            self._tag_to_id[tag] = self._next_id
            self._next_id += 1
        return str(self._tag_to_id[tag])
