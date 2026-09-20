"""判定引擎纯逻辑测试：样本量、失败条件、延长与趋势。"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from quality import engine
from quality.models import EffectivenessEvaluation as Eval
from quality.tests.factories import (
    FILL_METRIC,
    add_values,
    approve_completion,
    complete_action,
    make_user,
    setup_action,
)


class EngineTestMixin:
    def setUp(self):
        self.qa = make_user("qa", "quality")
        self.owner = make_user("owner", "owner")
        self.other_qa = make_user("qa2", "quality")


class PendingTests(EngineTestMixin, TestCase):
    def test_not_completed_is_pending(self):
        _, action = setup_action(qa=self.qa, owner=self.owner)
        result = engine.evaluate(action.current_evaluation)
        self.assertEqual(result.status, Eval.Status.PENDING)
        self.assertEqual(result.recommendation_code, "NOT_COMPLETED")


class ObservingTests(EngineTestMixin, TestCase):
    def _ready_action(self, min_sample=5):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=min_sample)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        return action

    def test_insufficient_sample_keeps_observing_and_never_success(self):
        action = self._ready_action(min_sample=5)
        add_values(action, [1.5, 1.6], user=self.owner)
        result = engine.evaluate(action.current_evaluation)
        self.assertEqual(result.status, Eval.Status.OBSERVING)
        self.assertFalse(result.metric_results[0].sample_met)
        self.assertNotIn(result.status,
                         (Eval.Status.EFFECTIVE, Eval.Status.READY))

    def test_sample_met_before_window_end_is_ready_early(self):
        action = self._ready_action(min_sample=5)
        add_values(action, [1.2, 1.4, 1.1, 1.5, 1.3], user=self.owner)
        result = engine.evaluate(action.current_evaluation)
        self.assertEqual(result.status, Eval.Status.READY)
        self.assertEqual(result.recommendation_code, "READY_EARLY")
        self.assertAlmostEqual(result.metric_results[0].aggregate, 1.3)
        self.assertTrue(result.metric_results[0].meets_target)

    def test_weighted_mean_uses_sample_count(self):
        action = self._ready_action(min_sample=10)
        add_values(action, [1.0, 3.0], user=self.owner,
                   sample_count=5)  # 10 samples
        result = engine.evaluate(action.current_evaluation)
        self.assertEqual(result.metric_results[0].n_samples, 10)
        self.assertAlmostEqual(result.metric_results[0].aggregate, 2.0)

    def test_window_end_low_sample_extends(self):
        action = self._ready_action(min_sample=5)
        ev = action.current_evaluation
        add_values(action, [1.2], user=self.owner)
        after_window = ev.window_end + timedelta(days=1)
        result = engine.evaluate(ev, now=after_window)
        self.assertEqual(result.status, Eval.Status.EXTENDED)
        self.assertTrue(result.extended)
        self.assertEqual(result.extension_count, 1)
        self.assertGreater(result.window_end, ev.window_end)

    def test_repeated_extension_then_insufficient_at_limit(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, window_days=10,
            min_sample_size=5, max_extensions=2)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [1.2], user=self.owner)
        ev = action.current_evaluation

        def simulated(window_start, window_end, ext_count):
            # 用未落库的判定对象模拟定时任务逐次延长后的状态
            return Eval(
                action=action, status=Eval.Status.EXTENDED,
                window_start=window_start, window_end=window_end,
                extension_count=ext_count)

        r1 = engine.evaluate(ev, now=ev.window_end + timedelta(days=1))
        self.assertEqual(r1.status, Eval.Status.EXTENDED)

        sim1 = simulated(r1.window_start, r1.window_end, 1)
        r2 = engine.evaluate(sim1, now=r1.window_end + timedelta(days=1))
        self.assertEqual(r2.status, Eval.Status.EXTENDED)
        self.assertEqual(r2.extension_count, 2)

        sim2 = simulated(r2.window_start, r2.window_end, 2)
        r3 = engine.evaluate(sim2, now=r2.window_end + timedelta(days=1))
        self.assertEqual(r3.status, Eval.Status.DATA_INSUFFICIENT)
        self.assertEqual(r3.recommendation_code, "INSUFFICIENT_AT_LIMIT")

    def test_aggregate_failure_blocks_review_even_with_enough_samples(self):
        action = self._ready_action(min_sample=5)
        add_values(action, [4.0, 4.2, 4.1, 4.0, 4.3], user=self.owner)
        result = engine.evaluate(action.current_evaluation)
        self.assertEqual(result.status, Eval.Status.INEFFECTIVE)
        self.assertEqual(result.recommendation_code, "FAILURE_TRIGGERED")
        self.assertTrue(result.metric_results[0].fail_triggered)

    def test_individual_breach_respects_allowed_count(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=5,
            metrics=[{
                **FILL_METRIC,
                "fail_condition": "individual_breach",
                "fail_threshold": 3.0,
                "allowed_breaches": 1,
            }])
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        # 1 次越限在允许范围内 → 达标
        add_values(action, [1.0, 1.2, 3.5, 1.1, 1.3], user=self.owner)
        ok = engine.evaluate(action.current_evaluation)
        self.assertEqual(ok.status, Eval.Status.READY)
        # 再出现一次越限 → 失败
        add_values(action, [4.0], user=self.owner)
        bad = engine.evaluate(action.current_evaluation)
        self.assertEqual(bad.status, Eval.Status.INEFFECTIVE)
        self.assertEqual(bad.metric_results[0].breach_count, 2)

    def test_increase_direction_threshold(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=3,
            metrics=[{
                "name": "合格率", "metric_type": "rate", "unit": "%",
                "direction": "increase", "baseline_value": 90.0,
                "target_value": 98.0,
                "fail_condition": "aggregate_threshold",
                "fail_threshold": 97.0,
            }])
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [96.0, 96.5, 96.0], user=self.owner)
        result = engine.evaluate(action.current_evaluation)
        self.assertEqual(result.status, Eval.Status.INEFFECTIVE)
        self.assertIn("低于", result.reason_lines[0])

    def test_trend_summary_improving(self):
        action = self._ready_action(min_sample=6)
        add_values(action, [7.0, 6.0, 4.0, 3.0, 2.0, 1.5], user=self.owner)
        m = engine.evaluate(action.current_evaluation).metric_results[0]
        self.assertEqual(m.trend_label, "改善")
        self.assertLess(m.trend_slope, 0)
        self.assertGreater(m.first_half_mean, m.second_half_mean)
