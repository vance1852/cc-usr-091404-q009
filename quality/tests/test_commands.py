"""定时评估管理命令测试。"""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from quality.models import EffectivenessEvaluation as Eval
from quality.tests.factories import (
    add_values,
    approve_completion,
    complete_action,
    make_user,
    setup_action,
)


class RunChecksCommandTests(TestCase):
    def setUp(self):
        self.qa = make_user("qa", "quality")
        self.owner = make_user("owner", "owner")

    def test_command_extends_and_reports(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, window_days=10,
            min_sample_size=5)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [1.2], user=self.owner)
        as_of = (
            action.current_evaluation.window_end + timedelta(days=1)
        ).isoformat()

        out = StringIO()
        call_command("run_effectiveness_checks", f"--as-of={as_of}",
                     stdout=out)
        self.assertIn("延长 1", out.getvalue())
        self.assertEqual(
            action.current_evaluation.status, Eval.Status.EXTENDED)
        self.assertEqual(action.current_evaluation.extension_count, 1)

    def test_command_reports_ready(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=3)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [1.0, 1.2, 1.1], user=self.owner)
        out = StringIO()
        call_command("run_effectiveness_checks", stdout=out)
        self.assertIn("可评审 1", out.getvalue())
