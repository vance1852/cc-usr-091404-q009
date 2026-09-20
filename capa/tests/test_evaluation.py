"""评估引擎测试: 样本量门槛、失败条件、目标达成、观察期延长、定时任务。"""
from django.core.management import call_command

from capa import services
from capa.models import AuditLog, EffectivenessEvaluation
from capa.tests.base import CAPABaseTestCase


class EvaluationTests(CAPABaseTestCase):
    def test_insufficient_data_is_inconclusive_not_success(self):
        """窗口内样本不足 → 数据不足继续观察, 绝不能判为有效。"""
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.9])  # 2 < 3
        evaluation = services.evaluate_action(self.action, actor=None)
        self.assertEqual(evaluation.result, "inconclusive")
        self.assertFalse(evaluation.eligible_for_review)
        self.assertIn("数据不足", " ".join(evaluation.rationale))

    def test_insufficient_data_past_window_extends_observation(self):
        """窗口已过且样本不足 → 自动延长观察期而非判为成功。"""
        plan, metric, _ = self.make_plan(start_offset=-40, end_offset=-10)
        self.approve_and_complete()
        self.add_readings(metric, [0.8], start_offset=-30)
        old_end = plan.observation_end

        evaluation = services.evaluate_action(self.action, actor=None)
        self.assertEqual(evaluation.result, "extended")
        self.assertFalse(evaluation.eligible_for_review)
        plan.refresh_from_db()
        self.assertGreater(plan.observation_end, old_end)
        self.assertEqual(plan.extension_count, 1)
        self.assertTrue(AuditLog.objects.filter(
            action="capa.observation_extended", entity_id=str(plan.pk)).exists())

    def test_failure_condition_triggers_ineffective(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 2.5, 0.9])  # 2.5 > 阈值 2.0
        evaluation = services.evaluate_action(self.action, actor=None)
        self.assertEqual(evaluation.result, "ineffective")
        self.assertTrue(evaluation.eligible_for_review)
        self.assertIn("失败条件触发", " ".join(evaluation.rationale))

    def test_targets_met_is_effective(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        evaluation = services.evaluate_action(self.action, actor=None)
        self.assertEqual(evaluation.result, "effective")
        self.assertEqual(evaluation.sample_size, 3)
        self.assertTrue(evaluation.metrics_summary["灌装量偏差率"]["meets_target"])

    def test_targets_missed_is_ineffective(self):
        """样本足够、未触发失败条件但未达目标 → 建议无效。"""
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [1.4, 1.5, 1.6])  # 均值 1.5 > 目标 1.0, 但未超阈值 2.0
        evaluation = services.evaluate_action(self.action, actor=None)
        self.assertEqual(evaluation.result, "ineffective")
        self.assertIn("未达目标", " ".join(evaluation.rationale))

    def test_readings_outside_window_ignored(self):
        plan, metric, _ = self.make_plan(start_offset=-5, end_offset=20)
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9], start_offset=-20)  # 全部在窗口开始前
        evaluation = services.evaluate_action(self.action, actor=None)
        self.assertEqual(evaluation.sample_size, 0)
        self.assertEqual(evaluation.result, "inconclusive")

    def test_scheduled_command_creates_version_only_on_change(self):
        """定时评估: 结论不变时不重复建版。"""
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])

        call_command("evaluate_observations")
        self.assertEqual(
            EffectivenessEvaluation.objects.filter(action=self.action).count(), 1)
        call_command("evaluate_observations")
        self.assertEqual(
            EffectivenessEvaluation.objects.filter(action=self.action).count(), 1)

        # 数据变化导致结论翻转 → 产生新版本
        self.add_readings(metric, [2.5], start_offset=1)
        call_command("evaluate_observations")
        evaluations = EffectivenessEvaluation.objects.filter(action=self.action)
        self.assertEqual(evaluations.count(), 2)
        current = evaluations.get(is_current=True)
        self.assertEqual(current.result, "ineffective")
        # 历史版本保留
        self.assertTrue(evaluations.filter(is_current=False, result="effective").exists())

    def test_scheduled_command_extends_past_due_observation(self):
        """定时任务对窗口已过且样本不足的措施自动延长观察。"""
        plan, metric, _ = self.make_plan(start_offset=-40, end_offset=-10)
        self.approve_and_complete()
        self.add_readings(metric, [0.8], start_offset=-30)
        call_command("evaluate_observations")
        plan.refresh_from_db()
        self.assertEqual(plan.extension_count, 1)
        current = self.action.evaluations.get(is_current=True)
        self.assertEqual(current.result, "extended")
