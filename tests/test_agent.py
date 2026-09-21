"""Focused checks for the independent agent's changed contracts."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

# Use the same local Ares bootstrap as the supported entry point.
import run
from agent.config import GameConfig, LLMConfig
from agent.context.builder import ContextBuilder, available_tactics
from agent.runtime.automation import AutomationController
from agent.runtime.observation.builder import Observation
from agent.runtime.parser import parse_model_payload
from agent.runtime.actions.errors import OutputFormatError
from agent.runtime.actions.resolution.resolver import EntityContext
from agent.runtime.controller import LLMGameController


class ContextAndParserTests(unittest.TestCase):
    def test_migrated_catalog_constructs_real_ares_behaviors(self):
        from agent.runtime.actions.resolution.loader import ActionCatalog
        from agent.runtime.actions.execution.adapter import AresActionAdapter
        from sc2.ids.unit_typeid import UnitTypeId

        actions = parse_model_payload(
            "# actions\nGasBuildingController(to_count=2)\nTechUp(desired_tech=BATTLECRUISER,base_location={x:10,y:10})"
        )["actions"]
        behaviors = AresActionAdapter(ActionCatalog.load()).compile(actions, EntityContext())
        self.assertEqual([type(item).__name__ for item in behaviors], ["GasBuildingController", "TechUp"])
        self.assertEqual(behaviors[0].to_count, 2)
        self.assertEqual(behaviors[1].desired_tech, UnitTypeId.BATTLECRUISER)

    def test_actions_only(self):
        result = parse_model_payload("# actions\nBuildWorkers(to_count=22)")
        self.assertEqual(result["actions"], [{"id": "BuildWorkers", "args": {"to_count": 22}}])
        self.assertNotIn("working", result)
        self.assertEqual(parse_model_payload("# actions")["actions"], [])

    def test_invalid_action_keeps_valid_sibling(self):
        result = parse_model_payload("# actions\nBad(unit=lookup(1))\nBuildWorkers(to_count=22)")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(len(result["errors"]), 1)

    def test_invalid_sections_are_rejected(self):
        for text in ("# phase\nopening\n# actions", "# working\nnote\n# actions",
                     "# actions\nBuildWorkers(to_count=22)\n# working\nnote",
                     "# actions\n# actions", "# actions\n# working\nx\n# working\ny",
                     "# actions\\nBuildWorkers(to_count=20)"):
            with self.subTest(text=text), self.assertRaises(OutputFormatError):
                parse_model_payload(text)

    def test_markdown_context_and_match_local_memory(self):
        for tactic in available_tactics():
            context = ContextBuilder(tactic)
            messages = context.build("OBSERVATION", [])
            self.assertIn("OBSERVATION", messages[1]["content"])
            self.assertIn(f"# {tactic}", messages[1]["content"])
        context.update_working("Current priority")
        context.update_working(None)
        self.assertEqual(context.working, "Current priority")
        self.assertEqual(ContextBuilder().working, "")
        context.update_working("")
        self.assertEqual(context.working, "")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.controller = LLMGameController(
            GameConfig(model_interval_seconds=5),
            LLMConfig(model="test", base_url="https://example.invalid", api_key="test-secret"),
            log_directory=self.directory,
        )
        self.context = EntityContext()
        self.bot = SimpleNamespace(
            time=0.0, time_formatted="00:00", supply_workers=12,
            minerals=100, vespene=0, start_location=object(),
            calculate_cost=Mock(return_value=SimpleNamespace(minerals=50, vespene=0)),
            tech_requirement_progress=Mock(return_value=1.0),
            register_behavior=Mock(), chat_send=AsyncMock(), can_afford=Mock(return_value=True),
        )
        builder = self.controller.observation_builder
        builder.collect_frame = Mock()
        builder.execution_context = Mock(side_effect=lambda _bot: self.context)
        builder.build = Mock(side_effect=lambda _bot, iteration: Observation(iteration, {}, "OBS", self.context))
        self.controller.action_exposure.build = Mock(return_value=SimpleNamespace(entries=[], validate=Mock()))
        self.gate = asyncio.Event()
        self.working = "## Current phase\nopening\n## Guidance\nPrepare expansion."
        self.reply = "# actions\nBuildWorkers(to_count=22)"

        async def complete(messages, *, trace, iteration):
            trace.model_conversation(stage="request", iteration=iteration, request=messages)
            await self.gate.wait()
            trace.model_conversation(stage="response", iteration=iteration, reply=self.reply)
            return self.reply if messages[1]["content"].startswith("<core_missions>\n") else self.working

        self.controller.client.complete = AsyncMock(side_effect=complete)

    async def asyncTearDown(self):
        await self.controller.close()
        self.temp.cleanup()

    async def frame(self, iteration, time):
        self.bot.time = time
        await self.controller.run_iteration(self.bot, iteration)
        await asyncio.sleep(0)

    async def deliver(self):
        self.gate.set()
        await self.controller._pending
        await self.frame(3, 2)

    async def test_single_request_apply_and_next_context(self):
        await self.frame(1, 0)
        await self.frame(2, 1)
        self.assertEqual(self.controller.client.complete.await_count, 1)
        self.assertIsNone(self.controller.automation.worker_target)
        self.assertEqual([type(call.args[0]).__name__ for call in self.bot.register_behavior.call_args_list],
                         ["Mining", "AutoSupply", "Mining", "AutoSupply"])
        await self.deliver()
        self.assertEqual(self.controller.automation.worker_target, 22)
        self.assertEqual(self.controller.context_builder.working, self.working)
        self.assertFalse((self.directory / "system/working.md").exists())
        messages = self.controller.client.complete.call_args.args[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[0]["content"], self.controller.context_builder.sources["prompts/system_actions.md"])
        first_messages = self.controller.client.complete.call_args_list[0].args[0]
        self.assertEqual(first_messages[0]["content"], self.controller.context_builder.sources["prompts/system_working.md"])
        self.assertNotEqual(messages[0]["content"], first_messages[0]["content"])
        self.assertIn("<core_missions>\n" + self.working + "\n</core_missions>", messages[1]["content"])
        self.assertIn("<general_guidance>", messages[1]["content"])
        for line in self.controller.context_builder.sources["memory/general.md"].splitlines():
            self.assertIn(line.strip(), messages[1]["content"])
        self.assertIn("<tactical_guidance>", first_messages[1]["content"])
        self.assertNotIn("<tactical_guidance>", messages[1]["content"])
        for request in (first_messages, messages):
            self.assertIn("# observation_guide\n", request[1]["content"])
            self.assertNotIn("<observation_guide>", request[1]["content"])
            self.assertLess(request[1]["content"].index("<actions_reference>"),
                            request[1]["content"].index("<output_requirements>"))
            for tag in ("general_guidance", "observation", "actions_reference", "output_requirements"):
                self.assertIn(f"<{tag}>", request[1]["content"])
                self.assertIn(f"<{tag}>", request[0]["content"])
        self.assertNotIn(self.controller.context_builder.sources["memory/tactics/BattleCruiserRush.md"], messages[1]["content"])
        self.assertIn("OBS", messages[1]["content"])
        self.assertIn("execution_feedback", messages[1]["content"])
        self.assertIn("actions_reference", messages[1]["content"])
        await self.frame(4, 5)
        messages = self.controller.client.complete.call_args_list[2].args[0]
        self.assertNotIn("Prepare expansion.", messages[1]["content"])
        self.assertEqual(self.controller.client.complete.await_count, 4)
        settings = (self.directory / "system/settings.json").read_text()
        self.assertNotIn("test-secret", settings)
        self.assertTrue((self.directory / "context/BattleCruiserRush.md").exists())
        events = [json.loads(line) for line in (self.directory / "system/events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["event"] == "working_memory_updated" for event in events))

    async def test_second_round_failure_keeps_saved_working(self):
        async def complete(messages, **kwargs):
            if not messages[1]["content"].startswith("<core_missions>\n"):
                return self.working
            self.assertFalse((self.directory / "system/working.md").exists())
            raise RuntimeError("execution request failed")

        self.controller.client.complete = AsyncMock(side_effect=complete)
        await self.frame(1, 0)
        await self.frame(2, 1)
        self.assertEqual(self.controller.context_builder.working, self.working)
        self.assertIsNone(self.controller.automation.worker_target)
        self.assertTrue(self.controller._feedback)

    async def test_action_reply_with_legacy_working_is_rejected(self):
        self.reply += "\n# working\nUnwanted replacement"
        await self.frame(1, 0)
        await self.deliver()
        self.assertEqual(self.controller.context_builder.working, self.working)
        self.assertIsNone(self.controller.automation.worker_target)
        self.assertTrue(self.controller._feedback)

    async def test_request_failure_keeps_existing_control_and_memory(self):
        self.controller.automation.replace_worker_override({"id": "BuildWorkers", "args": {"to_count": 23}})
        self.controller.context_builder.update_working("keep")
        self.controller.client.complete = AsyncMock(side_effect=RuntimeError("transport failed"))
        await self.frame(1, 0)
        await self.frame(2, 1)
        self.assertEqual(self.controller.automation.worker_target, 23)
        self.assertEqual(self.controller.context_builder.working, "keep")
        self.assertTrue(self.controller._feedback)

    async def test_close_discards_inflight_reply(self):
        await self.frame(1, 0)
        await self.controller.close()
        self.gate.set()
        await self.frame(2, 1)
        self.assertIsNone(self.controller.automation.worker_target)
        self.assertEqual(self.controller.context_builder.working, "")

    async def test_minimal_automation_lowers_depot_without_strategic_callbacks(self):
        from sc2.ids.unit_typeid import UnitTypeId
        from sc2.ids.ability_id import AbilityId
        depot = Mock(is_ready=True, type_id=UnitTypeId.SUPPLYDEPOT)
        self.bot.mediator = SimpleNamespace(get_own_structures_dict={UnitTypeId.SUPPLYDEPOT: [depot]})
        automation = AutomationController()
        await automation.run(self.bot, 16)
        automation.register_worker_production(self.bot)
        depot.assert_called_once_with(AbilityId.MORPH_SUPPLYDEPOT_LOWER)
        self.assertEqual(self.bot.register_behavior.call_count, 2)


if __name__ == "__main__":
    unittest.main()
