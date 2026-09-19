"""Compact action history preserves current status and execution feedback."""

import unittest

from agent.runtime.observation.action_history import ActionHistory


class ActionHistoryTests(unittest.TestCase):
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
