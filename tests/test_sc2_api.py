from pathlib import Path

import pytest

from rsi.tools import sc2_api


@pytest.fixture
def installed_source(tmp_path, monkeypatch):
    root = tmp_path / "sc2"
    root.mkdir()
    (root / "bot_ai.py").write_text('''raise RuntimeError("Source must never execute")
class BotAI:
    async def build(self, unit: int, near=None) -> bool:
        """Build near the selected position."""
        return True
''', encoding="utf-8")
    (root / "unit.py").write_text("class Unit:\n    def build(self, unit):\n        return False\n", encoding="utf-8")

    class Distribution:
        version = "test-version"

        def locate_file(self, name):
            assert name == "sc2"
            return root

    monkeypatch.setattr(sc2_api.importlib.metadata, "distribution", lambda name: Distribution())
    return root


def test_lookup_definition_without_execution(installed_source):
    result = sc2_api.lookup_sc2_api("BotAI.build")
    assert result["status"] == "found" and result["version"] == "test-version"
    assert result["symbol"] == "sc2.bot_ai.BotAI.build" and result["async"]
    assert result["signature"] == "async def build(self, unit: int, near=None) -> bool:"
    assert result["line"] == 3 and Path(result["file"]).is_relative_to(installed_source)
    assert "Build near" in result["docstring"] and "return True" in result["source"]
    assert not result["source_truncated"]
    assert sc2_api.lookup_sc2_api("sc2.bot_ai.BotAI.build") == {
        **result, "query": "sc2.bot_ai.BotAI.build"}


def test_ambiguous_missing_invalid_and_truncated(installed_source, monkeypatch):
    result = sc2_api.lookup_sc2_api("build")
    assert result["status"] == "ambiguous"
    assert {item["symbol"] for item in result["matches"]} == {"sc2.bot_ai.BotAI.build", "sc2.unit.Unit.build"}
    assert sc2_api.lookup_sc2_api("missing")["status"] == "not_found"
    for query in ("../secret.py", "/etc/passwd", "BotAI.build()", "", None):
        assert sc2_api.lookup_sc2_api(query)["status"] == "invalid_query"
    monkeypatch.setattr(sc2_api, "MAX_SOURCE", 12)
    result = sc2_api.lookup_sc2_api("Unit.build")
    assert result["source_truncated"] and len(result["source"]) == 12
