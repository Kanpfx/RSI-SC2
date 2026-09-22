"""Compact action history preserves current status and execution feedback."""

import unittest

from agent.runtime.observation.action_history import ActionHistory


class ActionHistoryTests(unittest.TestCase):
    def test_previous_three_cycles_without_record_limit(self):
        history = ActionHistory()
        history.begin_decision()
        for index in range(15):
            history.record(f"Action{index}()", "00:10", "failed", "invalid")
            history.record(f"Action{index}()", "00:10", "notice", "info")
        for _ in range(3):
            history.begin_decision()
        self.assertEqual(history.render().count("time@00:10"), 30)
        history.begin_decision()
        self.assertEqual(history.render(), "[None]")

    def test_live_actions_survive_history_window(self):
        history = ActionHistory()
        active = {"id": "BuildWorkers", "args": {"to_count": 20}}
        queued = {"id": "Build", "args": {"target": "natural"}}
        history.begin_decision()
        history.sync_active([active], "00:10")
        history.record(queued, "00:10", "queued")
        for _ in range(4):
            history.begin_decision()
        self.assertIn("active:", history.render())
        self.assertIn("queued:", history.render())
        history.record(queued, "00:50", "failed", "wait ended")
        history.begin_decision()
        self.assertIn("time@00:50 Build(target=natural): wait ended", history.render())

    def test_failure_uses_event_time_and_original_submission(self):
        history = ActionHistory()
        action = {"id": "BuildWorkers", "args": {"to_count": -1}}
        history.record(action, "02:50", "accepted")
        history.record(action, "02:57", "failed", "invalid value; 'to_count' = -1; expected a nonnegative integer",
                       submitted_action="BuildWorkers(to_count=-1)")
        self.assertEqual(history.render(), "failed:\n- time@02:57 BuildWorkers(to_count=-1): "
                         "invalid value; 'to_count' = -1; expected a nonnegative integer")

    def test_notice_does_not_remove_active_or_queued_actions(self):
        history = ActionHistory()
        action = {"id": "BuildWorkers", "args": {"to_count": 20}}
        history.sync_active([action], "02:50")
        history.record(action, "02:57", "notice", "unchanged target")
        self.assertIn("active:\n- time@02:50 BuildWorkers(to_count=20)", history.render())
        self.assertIn("notice:\n- time@02:57 BuildWorkers(to_count=20): unchanged target", history.render())
        history.record(action, "03:00", "queued")
        history.record(action, "03:01", "notice", "still waiting")
        self.assertIn("queued:\n- time@03:00", history.render())

    def test_grouped_statuses_preserve_arguments_and_reasons(self):
        history = ActionHistory()
        workers = {"id": "BuildWorkers", "args": {"to_count": 45}}
        build = {"id": "Build", "args": {"structure": "COMMANDCENTER", "target": "natural"}}
        tech = {"id": "TechUp", "args": {"target": "BATTLECRUISER"}}
        history.record(workers, "00:10", "accepted")
        history.sync_active([workers], "00:11")
        history.record(build, "00:12", "queued")
        history.record(tech, "00:13", "failed", "missing prerequisite")
        self.assertEqual(history.render(), (
            "active:\n- time@00:11 BuildWorkers(to_count=45)\n"
            "queued:\n- time@00:12 Build(structure=COMMANDCENTER, target=natural)\n"
            "failed:\n- time@00:13 TechUp(target=BATTLECRUISER): missing prerequisite"
        ))

    def test_retry_replaces_old_failure_and_preserves_one_time_acceptance(self):
        history = ActionHistory()
        self.assertEqual(history.render(), "[None]")
        action = {"id": "Build", "args": {"target": "natural"}}
        history.record(action, "00:10", "failed", "not ready")
        history.record(action, "00:15", "queued")
        history.record(action, "00:20", "accepted")
        history.annotate(action, "submitted\nawaiting construction")
        self.assertEqual(history.render(),
                         "accepted:\n- time@00:20 Build(target=natural): submitted awaiting construction")
