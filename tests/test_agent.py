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
from agent.harness.context import ContextBuilder, available_tactics
from agent.harness.action_reference import _section
from agent.game.automation import AutomationController
from agent.game.observation.builder import Observation
from agent.game.actions.parser import parse_model_payload
from agent.game.actions.errors import OutputFormatError
from agent.game.actions.resolution.resolver import EntityContext
from agent.harness.controller import LLMGameController


class ContextAndParserTests(unittest.TestCase):
    def test_feedback_is_one_line_with_original_action_and_severity(self):
        from agent.game.actions.formatting import format_feedback
        text = format_feedback([
            {"action_index": 2, "submitted_action": "BuildWorkers(to_count=-1)",
             "error": "to_count must be nonnegative"},
            {"kind": "action_notice", "action_index": 3, "submitted_action": "TechUp(desired_tech=MARINE)",
             "error": "no new work started"},
        ])
        self.assertEqual(text.splitlines(), [
            "Error — Action 2 — BuildWorkers(to_count=-1): to_count must be nonnegative",
            "Notice — Action 3 — TechUp(desired_tech=MARINE): no new work started",
        ])

    def test_runtime_exception_and_union_error_are_identifiable(self):
        from agent.game.actions.execution.adapter import TrackedBehavior
        from agent.game.actions.resolution.loader import ActionCatalog
        failure = Mock()
        TrackedBehavior(Mock(execute=Mock(side_effect=AssertionError())),
                        on_failure=failure, on_result=Mock()).execute(None, {}, None)
        failure.assert_called_once_with("execution_error: AssertionError")
        with self.assertRaisesRegex(ValueError, r"target.*valid Point2 \| Unit"):
            ActionCatalog.load().type_resolver.resolve([], "Point2 | Unit", EntityContext(), "target")
        self.assertEqual(EntityContext(own_entities={"851": SimpleNamespace(tag=999999)}).observation_id(999999), "851")

    def test_migrated_catalog_constructs_real_ares_behaviors(self):
        from agent.game.actions.resolution.loader import ActionCatalog
        from agent.game.actions.execution.adapter import AresActionAdapter
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
        for text in ("BuildWorkers(to_count=22)", "# actions\n# actions",
                     "# actions\\nBuildWorkers(to_count=20)"):
            with self.subTest(text=text), self.assertRaises(OutputFormatError):
                parse_model_payload(text)

    def test_markdown_context_and_match_local_memory(self):
        for tactic in available_tactics():
            context = ContextBuilder(tactic)
            messages = context.build_planner_messages("OBSERVATION", [])
            self.assertIn("OBSERVATION", messages[1]["content"])
            self.assertIn(_section("tactical_guidance", context.sources[f"knowledge/tactics/{tactic}.md"]),
                          messages[1]["content"])
        context.update_working("Current priority")
        context.update_working(None)
        self.assertEqual(context.working, "Current priority")
        self.assertEqual(ContextBuilder().working, "")
        context.update_working("")
        self.assertEqual(context.working, "")

    def test_planner_capabilities_and_two_timestamped_decisions(self):
        context = ContextBuilder()
        for time, phase in (("00:10", "old"), ("00:20", "opening"), ("00:30", "growth")):
            context.update_working(f"## Current phase\n{phase}\n## Guidance\n1. Build army.\n2. Expand.", time)
        entries = [{"name": "BuildWorkers", "description": "Set worker target."}]
        content = context.build_planner_messages("LATEST", entries)[1]["content"]
        self.assertNotIn("time=00:10", content)
        self.assertIn("time=00:20 phase=opening guidance=1. Build army. | 2. Expand.", content)
        self.assertIn("time=00:30 phase=growth", content)
        self.assertLess(content.index("time=00:20"), content.index("time=00:30"))
        self.assertIn("BuildWorkers: [Persistent] Set worker target.", content)
        self.assertNotIn("argument_definitions", content)
        self.assertNotIn("BuildWorkers(", content)
        self.assertIn(_section("static_knowledge", context.sources["knowledge/terran.md"]), content)


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
        builder.build = Mock(side_effect=lambda _bot, iteration: Observation(iteration, {}, f"OBS frame={iteration}\n" + builder.action_history.render(), self.context))
        self.controller.action_exposure.build = Mock(return_value=SimpleNamespace(entries=[], validate=Mock()))
        self.gate = asyncio.Event()
        self.working = "## Current phase\nopening\n## Guidance\nPrepare expansion."
        self.reply = "# actions\nBuildWorkers(to_count=22)"

        async def complete(messages, *, phase, trace, iteration):
            trace.model_conversation(stage="request", iteration=iteration, request=messages)
            await self.gate.wait()
            trace.model_conversation(stage="response", iteration=iteration, reply=self.reply)
            return self.reply if ("<current_decision>\n" in messages[1]["content"]) else self.working

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
        await asyncio.sleep(0)
        await self.frame(2, 1)
        await self.controller._pending
        await self.frame(3, 2)

    async def test_single_request_apply_and_next_context(self):
        self.controller.executor.record_failure(self.bot, 0, "BuildWorkers()", "prior action failed")
        await self.frame(1, 0)
        await self.frame(2, 1)
        self.assertEqual(self.controller.client.complete.await_count, 1)
        self.assertIsNone(self.controller.automation.worker_target)
        self.assertEqual([type(call.args[0]).__name__ for call in self.bot.register_behavior.call_args_list],
                         ["Mining", "AutoSupply", "Mining", "AutoSupply"])
        await self.deliver()
        self.assertEqual(self.controller.automation.worker_target, 22)
        self.assertEqual(self.controller.context_builder.working, self.working)
        self.assertFalse((self.directory / "working.md").exists())
        messages = self.controller.client.complete.call_args.args[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[0]["content"], self.controller.context_builder.sources["prompts/excutor/system.md"])
        first_messages = self.controller.client.complete.call_args_list[0].args[0]
        first_content = first_messages[1]["content"]
        action_content = messages[1]["content"]
        self.assertIn(_section("previous_decision", "No previous decision."), first_content)
        self.assertIn("OBS frame=1", first_content)
        self.assertIn("OBS frame=2", action_content)
        self.assertNotIn("OBS frame=1", action_content)
        self.assertIn(_section("general_guidance", self.controller.context_builder.sources["prompts/planner/rules.md"]),
                      first_messages[1]["content"])
        self.assertIn("prior action failed", first_content)
        for content, tags in (
            (first_content, ("general_guidance", "static_knowledge", "tactical_guidance",
                             "action_capabilities", "previous_decision", "observation", "output_requirements")),
            (action_content, ("general_guidance", "static_knowledge", "actions_reference",
                              "current_decision", "observation", "output_requirements")),
        ):
            positions = [content.index(f"<{tag}>\n") for tag in tags]
            self.assertEqual(positions, sorted(positions))
        self.assertEqual(first_messages[0]["content"], self.controller.context_builder.sources["prompts/planner/system.md"])
        self.assertNotEqual(messages[0]["content"], first_messages[0]["content"])
        self.assertIn(_section("current_decision", self.working), messages[1]["content"])
        self.assertIn(_section("static_knowledge", self.controller.context_builder.sources["knowledge/terran.md"]),
                      messages[1]["content"])
        self.assertIn("<general_guidance>", messages[1]["content"])
        for line in self.controller.context_builder.sources["prompts/excutor/rules.md"].splitlines():
            self.assertIn(line.strip(), messages[1]["content"])
        self.assertIn("<tactical_guidance>", first_messages[1]["content"])
        self.assertNotIn("<tactical_guidance>", messages[1]["content"])
        for request in (first_messages, messages):
            self.assertIn("# observation_guide\n", request[1]["content"])
            self.assertNotIn("<observation_guide>", request[1]["content"])
            action_tag = "action_capabilities" if request is first_messages else "actions_reference"
            self.assertLess(request[1]["content"].index(f"<{action_tag}>"),
                            request[1]["content"].index("<output_requirements>"))
            for tag in ("general_guidance", "observation", action_tag, "output_requirements"):
                self.assertIn(f"<{tag}>", request[1]["content"])
                self.assertIn(f"<{tag}>", request[0]["content"])
        self.assertNotIn(self.controller.context_builder.sources["knowledge/tactics/BattleCruiserRush.md"], messages[1]["content"])
        self.assertIn("OBS", messages[1]["content"])
        self.assertNotIn("<execution_feedback>", messages[1]["content"])
        self.assertIn("actions_reference", messages[1]["content"])
        await self.frame(4, 5)
        messages = self.controller.client.complete.call_args_list[2].args[0]
        self.assertIn("time=00:00 phase=opening guidance=Prepare expansion.", messages[1]["content"])
        self.assertEqual(self.controller.client.complete.await_count, 3)
        settings = (self.directory / "settings.json").read_text()
        self.assertNotIn("test-secret", settings)
        self.assertFalse((self.directory / "context").exists())
        events = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["event"] == "working_memory_updated" for event in events))

    async def test_second_round_failure_keeps_saved_working(self):
        async def complete(messages, **kwargs):
            if not ("<current_decision>\n" in messages[1]["content"]):
                return self.working
            self.assertFalse((self.directory / "working.md").exists())
            raise RuntimeError("execution request failed")

        self.controller.client.complete = AsyncMock(side_effect=complete)
        await self.frame(1, 0)
        await self.frame(2, 1)
        await self.frame(3, 2)
        self.assertEqual(self.controller.context_builder.working, self.working)
        self.assertIsNone(self.controller.automation.worker_target)
        self.assertTrue(self.controller._feedback)

    async def test_action_reply_ignores_extra_working_section(self):
        self.reply += "\n# working\nUnwanted replacement"
        await self.frame(1, 0)
        await self.deliver()
        self.assertEqual(self.controller.context_builder.working, self.working)
        self.assertEqual(self.controller.automation.worker_target, 22)
        self.assertFalse(self.controller._feedback)

    async def test_feedback_retains_source_after_parse_failure_and_execution(self):
        self.reply = "# actions\nBad(unit=lookup(1))\nBuildWorkers()\nBuildWorkers(to_count=22)"
        await self.frame(1, 0)
        await self.deliver()
        feedback = self.controller._feedback
        self.assertEqual([item["action_index"] for item in feedback], [1, 2])
        self.assertEqual(feedback[1]["submitted_action"], "BuildWorkers()")
        history = self.controller.observation_builder.action_history.render()
        self.assertIn("Bad(unit=lookup(1)):", history)
        self.assertIn("BuildWorkers(): format_error: 'to_count' is required", history)
        action = self.controller.automation.worker_action
        self.controller.executor.record_failure(self.bot, 4, action, "later failure")
        self.assertEqual(feedback[-1]["action_index"], 3)
        self.assertEqual(feedback[-1]["submitted_action"], "BuildWorkers(to_count=22)")
        self.controller.executor.record_feedback(self.bot, {
            "kind": "action_notice", "action": action, "error": "no new work started",
        })
        await self.frame(5, 5)
        content = self.controller.client.complete.call_args_list[2].args[0][1]["content"]
        self.assertIn("failed:", content)
        self.assertNotIn("notice:", content)
        self.assertIn("BuildWorkers(to_count=22): later failure", content)
        self.assertNotIn("BuildWorkers(to_count=22): no new work started", content)
        self.assertNotIn("<execution_feedback>", content)

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
