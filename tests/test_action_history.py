"""Compact action history preserves current status and execution feedback."""

import unittest

from agent.game.observation.action_history import ActionHistory


class ActionHistoryTests(unittest.TestCase):
    def test_two_cycles_without_record_limit(self):
        history = ActionHistory()
        history.begin_decision()
        for index in range(15):
            history.record(f"Action{index}()", "00:10", "failed", "invalid")
        history.begin_decision()
        self.assertEqual(history.render().count("time@00:10"), 15)
        history.begin_decision()
        self.assertEqual(history.render(), "[None]")

    def test_two_cycle_window_preserves_each_cycle_and_sorts_times(self):
        history = ActionHistory()
        for cycle in range(1, 6):
            history.begin_decision()
            history.record('Build()', f'00:{cycle * 10:02}', 'failed', str(cycle))
        rendered = history.render()
        self.assertEqual(rendered.count('Build()'), 2)
        self.assertNotIn('time@00:30', rendered)
        self.assertLess(rendered.index('time@00:40'), rendered.index('time@00:50'))

    def test_live_actions_survive_history_window(self):
        history = ActionHistory()
        active = {"id": "BuildWorkers", "args": {"to_count": 20}}
        history.begin_decision()
        history.sync_active([active], "00:10")
        for _ in range(4):
            history.begin_decision()
        self.assertIn("active:", history.render())
        history.sync_active([], "00:50")
        self.assertEqual(history.render(), "[None]")

    def test_failure_uses_event_time_and_original_submission(self):
        history = ActionHistory()
        action = {"id": "BuildWorkers", "args": {"to_count": -1}}
        history.record(action, "02:50", "submitted")
        history.record(action, "02:57", "failed", "invalid value; 'to_count' = -1; expected a nonnegative integer",
                       submitted_action="BuildWorkers(to_count=-1)")
        self.assertEqual(history.render(), "failed:\n- time@02:57 BuildWorkers(to_count=-1): "
                         "invalid value; 'to_count' = -1; expected a nonnegative integer")

    def test_annotations_attach_to_active_or_submitted_actions(self):
        history = ActionHistory()
        action = {"id": "BuildWorkers", "args": {"to_count": 20}}
        history.sync_active([action], "02:50")
        history.annotate(action, "no new work started")
        self.assertIn("active:\n- time@02:50 BuildWorkers(to_count=20)", history.render())
        self.assertIn("BuildWorkers(to_count=20): no new work started", history.render())
        self.assertNotIn("notice:", history.render())
        history.sync_active([], "03:00")
        history.record(action, "03:00", "submitted")
        history.annotate(action, "order issued")
        self.assertIn("submitted:\n- time@03:00", history.render())
        self.assertIn("BuildWorkers(to_count=20): order issued", history.render())

    def test_unmatched_annotation_does_not_add_history(self):
        history = ActionHistory()
        history.annotate("Unknown()", "no new work started")
        self.assertEqual(history.render(), "[None]")

    def test_grouped_statuses_preserve_arguments_and_reasons(self):
        history = ActionHistory()
        workers = {"id": "BuildWorkers", "args": {"to_count": 45}}
        build = {"id": "Build", "args": {"structure": "COMMANDCENTER", "target": "natural"}}
        tech = {"id": "TechUp", "args": {"target": "BATTLECRUISER"}}
        history.sync_active([workers], "00:11")
        history.record(build, "00:12", "submitted")
        history.record(tech, "00:13", "failed", "missing prerequisite")
        self.assertEqual(history.render(), (
            "active:\n- time@00:11 BuildWorkers(to_count=45)\n"
            "submitted:\n- time@00:12 Build(structure=COMMANDCENTER, target=natural)\n"
            "failed:\n- time@00:13 TechUp(target=BATTLECRUISER): missing prerequisite"
        ))

    def test_retry_replaces_old_failure_and_preserves_submission(self):
        history = ActionHistory()
        self.assertEqual(history.render(), "[None]")
        action = {"id": "Build", "args": {"target": "natural"}}
        history.record(action, "00:10", "failed", "not ready")
        history.record(action, "00:20", "submitted")
        history.annotate(action, "submitted\nawaiting construction")
        self.assertEqual(history.render(),
                         "submitted:\n- time@00:20 Build(target=natural): submitted awaiting construction")


    def test_legacy_statuses_are_rejected(self):
        history = ActionHistory()
        for status in ("accepted", "queued", "expired"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                history.record("Action()", "00:10", status)
