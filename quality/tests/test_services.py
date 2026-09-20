"""服务层工作流测试：责任分离、证据、评审签署、重开版本保留。"""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from quality import services
from quality.models import (
    CompletionEvidence,
    ConflictOpinion,
    CorrectiveAction,
    Deviation,
    EffectivenessEvaluation as Eval,
    Metric,
    SignOff,
)
from quality.tests.factories import (
    FILL_METRIC,
    add_values,
    approve_completion,
    complete_action,
    make_action,
    make_deviation,
    make_user,
    setup_action,
)


class WorkflowTest(TestCase):
    def setUp(self):
        self.qa = make_user("qa", "quality")
        self.owner = make_user("owner", "owner")
        self.investigator = make_user("inv", "investigator")
        self.qa2 = make_user("qa2", "quality")

    # -------------------------------------------------- 基线与责任分离
    def test_owner_cannot_freeze_own_baseline(self):
        deviation = make_deviation(created_by=self.owner)
        action = make_action(deviation, self.owner)
        with self.assertRaises(PermissionDenied):
            services.freeze_baseline(
                action=action, user=self.owner,
                window_days=30, min_sample_size=5)

    def test_baseline_frozen_once_and_metrics_locked(self):
        _, action = setup_action(qa=self.qa, owner=self.owner)
        with self.assertRaises(ValidationError):
            services.freeze_baseline(
                action=action, user=self.qa2,
                window_days=60, min_sample_size=10)
        metric = action.metrics.first()
        self.assertTrue(metric.is_locked)
        self.assertEqual(action.baseline.window_days, 30)
        self.assertEqual(action.evaluations.count(), 1)

    def test_baseline_requires_at_least_one_metric(self):
        deviation = make_deviation(created_by=self.owner)
        action = make_action(deviation, self.owner)
        with self.assertRaises(ValidationError):
            services.freeze_baseline(
                action=action, user=self.qa,
                window_days=30, min_sample_size=5, metrics=[])

    # -------------------------------------------------- 证据批准
    def test_owner_cannot_approve_own_evidence(self):
        _, action = setup_action(qa=self.qa, owner=self.owner)
        evidence = CompletionEvidence.objects.create(
            action=action, title="校准记录", submitted_by=self.owner)
        with self.assertRaises(PermissionDenied):
            services.approve_evidence(
                evidence=evidence, user=self.owner, approved=True)

    def test_evidence_double_review_rejected(self):
        _, action = setup_action(qa=self.qa, owner=self.owner)
        evidence = CompletionEvidence.objects.create(
            action=action, title="校准记录", submitted_by=self.owner)
        services.approve_evidence(evidence=evidence, user=self.qa,
                                  approved=True)
        with self.assertRaises(ValidationError):
            services.approve_evidence(evidence=evidence, user=self.qa2,
                                      approved=True)

    def test_complete_requires_approved_evidence(self):
        _, action = setup_action(qa=self.qa, owner=self.owner)
        with self.assertRaises(ValidationError):
            services.mark_action_completed(action=action, user=self.qa)

    def test_owner_cannot_self_confirm_completion(self):
        _, action = setup_action(qa=self.qa, owner=self.owner)
        approve_completion(action, owner=self.owner, qa=self.qa)
        with self.assertRaises(PermissionDenied):
            services.mark_action_completed(action=action, user=self.owner)

    # -------------------------------------------------- 提交评审
    def _fully_ready(self, min_sample=5):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=min_sample)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [1.2, 1.4, 1.1, 1.5, 1.3], user=self.owner)
        return action

    def test_submit_blocked_when_sample_insufficient(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=5)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [1.2], user=self.owner)
        with self.assertRaises(ValidationError):
            services.submit_for_review(action=action, user=self.owner)

    def test_submit_when_ready(self):
        action = self._fully_ready()
        ev = services.submit_for_review(action=action, user=self.owner,
                                        note="数据达标")
        self.assertEqual(ev.status, Eval.Status.IN_REVIEW)
        self.assertEqual(ev.submitted_by, self.owner)

    # -------------------------------------------------- 签署
    def test_non_quality_cannot_sign(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        with self.assertRaises(PermissionDenied):
            services.sign_off(
                evaluation=action.current_evaluation,
                user=self.investigator, decision="approve_close")

    def test_quality_approve_closes_effective(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        signoff = services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.APPROVE, reason="趋势达标")
        ev = action.current_evaluation
        self.assertEqual(ev.status, Eval.Status.EFFECTIVE)
        self.assertEqual(ev.result, Eval.Result.EFFECTIVE)
        self.assertTrue(signoff.is_active)
        self.assertEqual(signoff.signoff_version, 1)

    def test_approve_rejected_if_new_failure_data_appeared(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        # 提交评审后又出现越限数据
        add_values(action, [9.9, 9.8, 9.7, 9.6, 9.5], user=self.owner)
        with self.assertRaises(ValidationError):
            services.sign_off(
                evaluation=action.current_evaluation, user=self.qa,
                decision=SignOff.Decision.APPROVE)

    def test_quality_reject_marks_ineffective(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.REJECT, reason="抽样方案不可信")
        ev = action.current_evaluation
        self.assertEqual(ev.status, Eval.Status.INEFFECTIVE)
        self.assertEqual(ev.result, Eval.Result.INEFFECTIVE)

    # -------------------------------------------------- 撤回签署
    def test_withdraw_requires_reason_and_keeps_history(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.APPROVE, reason="ok")
        with self.assertRaises(ValidationError):
            services.withdraw_signoff(
                evaluation=action.current_evaluation, user=self.qa,
                reason="  ")
        record = services.withdraw_signoff(
            evaluation=action.current_evaluation, user=self.qa,
            reason="发现新批次灌装量回弹")
        ev = action.current_evaluation
        self.assertEqual(record.decision, SignOff.Decision.WITHDRAWN)
        self.assertEqual(record.signoff_version, 2)
        superseded = SignOff.objects.get(pk=record.supersedes_id)
        self.assertFalse(superseded.is_active)
        # 原签署记录仍然存在，未删除
        self.assertEqual(ev.signoffs.count(), 2)
        self.assertEqual(ev.status, Eval.Status.IN_REVIEW)
        self.assertIsNone(ev.result)

    def test_non_quality_cannot_withdraw(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.APPROVE)
        with self.assertRaises(PermissionDenied):
            services.withdraw_signoff(
                evaluation=action.current_evaluation, user=self.owner,
                reason="n/a")

    # -------------------------------------------------- 定时评估：延长
    def test_scheduled_run_extends_window_instead_of_success(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, window_days=10,
            min_sample_size=5)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [1.2], user=self.owner)
        services.run_evaluation(
            action=action,
            now=action.current_evaluation.window_end + timedelta(days=1))
        ev = action.current_evaluation
        self.assertEqual(ev.status, Eval.Status.EXTENDED)
        self.assertEqual(ev.extension_count, 1)
        self.assertGreater(
            ev.window_end,
            action.completed_at + timedelta(days=10))

    def test_scheduled_run_marks_ready(self):
        action = self._fully_ready()
        services.run_evaluation(action=action)
        self.assertEqual(action.current_evaluation.status, Eval.Status.READY)

    def test_scheduled_run_marks_failure(self):
        _, action = setup_action(
            qa=self.qa, owner=self.owner, min_sample_size=5)
        approve_completion(action, owner=self.owner, qa=self.qa)
        complete_action(action, self.qa)
        add_values(action, [4.0, 4.2, 4.1, 4.0, 4.3], user=self.owner)
        services.run_evaluation(action=action)
        ev = action.current_evaluation
        self.assertEqual(ev.status, Eval.Status.INEFFECTIVE)
        self.assertEqual(ev.result, Eval.Result.INEFFECTIVE)
        self.assertIn("聚合值", ev.conclusion_note)

    def test_run_due_evaluations_only_completed(self):
        # 未完成措施不应报错，也不会产生状态变化
        _, action = setup_action(qa=self.qa, owner=self.owner)
        touched = services.run_due_evaluations()
        self.assertEqual(touched, [])

    # -------------------------------------------------- 相关偏差重开
    def test_related_deviation_reopens_with_new_version_keeps_history(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.APPROVE, reason="ok")
        services.close_deviation(deviation=action.deviation, user=self.qa)
        old = action.current_evaluation
        self.assertEqual(old.version, 1)

        recur = make_deviation(code="DEV-002", batch_no="B20261015",
                               created_by=self.investigator)
        new_ev = services.link_related_deviation(
            action=action, deviation=recur, user=self.qa,
            relationship_note="同型号灌装阀再次出现灌装量偏低")
        self.assertEqual(new_ev.version, 2)
        self.assertTrue(new_ev.is_current)

        old.refresh_from_db()
        self.assertFalse(old.is_current)
        self.assertEqual(old.status, Eval.Status.REOPENED)
        # 先前结论不删除
        self.assertEqual(old.result, Eval.Result.EFFECTIVE)
        self.assertEqual(action.evaluations.count(), 2)
        self.assertEqual(new_ev.trigger_deviation, recur)
        # 原偏差在 CAPA 失效后也应重新打开
        action.deviation.refresh_from_db()
        self.assertEqual(action.deviation.status, Deviation.Status.REOPENED)
        # 原始偏差不能作为相关偏差
        with self.assertRaises(ValidationError):
            services.link_related_deviation(
                action=action, deviation=action.deviation, user=self.qa)
        # 不能重复关联
        with self.assertRaises(ValidationError):
            services.link_related_deviation(
                action=action, deviation=recur, user=self.qa)

    def test_reopened_version_must_observe_before_effective_again(self):
        action = self._fully_ready()
        services.submit_for_review(action=action, user=self.owner)
        services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.APPROVE)
        recur = make_deviation(code="DEV-003", created_by=self.investigator)
        services.link_related_deviation(
            action=action, deviation=recur, user=self.qa)
        ev = action.current_evaluation
        self.assertEqual(ev.status, Eval.Status.OBSERVING)
        # 新版本无数据，不能提交
        with self.assertRaises(ValidationError):
            services.submit_for_review(action=action, user=self.owner)

    # -------------------------------------------------- 偏差关闭
    def test_deviation_close_requires_effective_actions_and_quality(self):
        action = self._fully_ready()
        deviation = action.deviation
        # 责任人不能关闭
        with self.assertRaises(PermissionDenied):
            services.close_deviation(deviation=deviation, user=self.owner)
        # 措施未判有效，质量角色也不能关闭
        with self.assertRaises(ValidationError):
            services.close_deviation(deviation=deviation, user=self.qa)
        services.submit_for_review(action=action, user=self.owner)
        services.sign_off(
            evaluation=action.current_evaluation, user=self.qa,
            decision=SignOff.Decision.APPROVE)
        services.close_deviation(deviation=deviation, user=self.qa)
        deviation.refresh_from_db()
        self.assertEqual(deviation.status, Deviation.Status.CLOSED)
        self.assertIsNotNone(deviation.closed_at)

    # -------------------------------------------------- 冲突意见
    def test_conflict_opinion_recorded(self):
        action = self._fully_ready()
        ev = action.current_evaluation
        services.add_conflict_opinion(
            evaluation=ev, user=self.investigator,
            stance=ConflictOpinion.Stance.CHALLENGES,
            content="后段两个批次已有回弹迹象，建议延长观察")
        self.assertEqual(ev.opinions.count(), 1)
        with self.assertRaises(ValidationError):
            services.add_conflict_opinion(
                evaluation=ev, user=self.investigator,
                stance=ConflictOpinion.Stance.ABSTAIN, content="   ")
