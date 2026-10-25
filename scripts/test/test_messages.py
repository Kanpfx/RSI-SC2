"""Check conditional instructions and optional message fields without external services."""
from contextlib import redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
import unittest

from rsi.experiments import diagnosis_prompt
from rsi.tools.console import _action_progress, _observation_progress


class MessageTests(unittest.TestCase):
    def test_diagnosis_options_work_with_arbitrary_base_wording(self):
        base = "A short replacement role description."
        for competing in (True, False):
            for policy in ("active", "fixed"):
                with self.subTest(competing=competing, policy=policy):
                    prompt = diagnosis_prompt(base, dict(competing_hypotheses=competing, query_policy=policy))
                    self.assertTrue(prompt.startswith(base + "\n\n"))
                    self.assertIn("at least two competing hypotheses" if competing else "one hypothesis", prompt)
                    self.assertNotIn("one hypothesis" if competing else "at least two competing hypotheses", prompt)
                    self.assertIn("keep the problem fixed", prompt)
                    if policy == "fixed":
                        self.assertIn("do not call query_logs, read_reference or query_memory", prompt)
                        self.assertNotIn("Query evidence", prompt)
                    else:
                        self.assertIn("Query evidence", prompt)
                        self.assertIn("max_queries", prompt)
                        self.assertNotIn("predeclared evidence pack", prompt)

    def test_query_without_optional_target_has_no_empty_separator(self):
        output = StringIO()
        with redirect_stdout(output):
            _action_progress("diagnosis", "query_logs", dict(kind="overview", purpose="Check coverage"), None)
        self.assertIn("Query overview; purpose: Check coverage", output.getvalue())

    def test_query_result_keeps_pagination_truncation_and_reason(self):
        output = StringIO()
        value = dict(items=[dict(name="workers")], next_offset=1, truncated=True, reason="Narrow time")
        with redirect_stdout(output):
            _observation_progress("diagnosis", "query_logs", SimpleNamespace(text=json.dumps(value)))
        self.assertIn("Results: 1; next_offset=1; truncated; Narrow time", output.getvalue())
        output = StringIO()
        with redirect_stdout(output):
            _observation_progress("diagnosis", "query_logs", SimpleNamespace(text='{"items":[]}'))
        self.assertIn("Results: 0\n", output.getvalue())
        self.assertNotIn(";", output.getvalue())
