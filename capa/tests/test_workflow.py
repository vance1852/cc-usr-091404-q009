"""工作流规则测试: 状态机、责任分离、计划固化、签署与撤回。"""
from capa import services
from capa.models import CAPAAction, Evidence, SignOff
from capa.tests.base import CAPABaseTestCase


class WorkflowTests(CAPABaseTestCase):
    def test_full_lifecycle_to_effective_close(self):
        plan, metric, _ = self.make_plan()
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)
        plan.refresh_from_db()
        self.assertTrue(plan.is_frozen)
        self.assertEqual(plan.approved_by, self.qa)

        evidence = services.submit_evidence(self.action, self.owner, title="维修与校准记录")
        services.review_evidence(evidence, self.qa, approve=True)
        services.complete_action(self.action, self.owner)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.MONITORING)

        self.add_readings(metric, [0.8, 0.7, 0.9])
        evaluation = services.evaluate_action(self.action, actor=self.qa)
        self.assertEqual(evaluation.result, "effective")
        self.assertTrue(evaluation.eligible_for_review)

        services.submit_for_review(self.action, self.owner)
        signoff = services.close_action(self.action, self.qa, decision="close_effective")
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.CLOSED_EFFECTIVE)
        self.assertEqual(signoff.version, 1)
        self.assertEqual(signoff.state, SignOff.State.ACTIVE)

    def test_completion_does_not_equal_effectiveness(self):
        """措施完成只进入观察期, 不能直接关闭。"""
        self.make_plan()
        self.approve_and_complete()
        self.assertEqual(self.action.status, CAPAAction.Status.MONITORING)
        self.assertNotIn(self.action.status, (
            CAPAAction.Status.CLOSED_EFFECTIVE, CAPAAction.Status.CLOSED_INEFFECTIVE))
        with self.assertRaises(services.BusinessRuleViolation):
            services.close_action(self.action, self.qa, decision="close_effective")

    def test_approve_requires_quality_role(self):
        self.make_plan()
        services.submit_plan(self.action, self.owner)
        with self.assertRaises(services.PermissionDenied):
            services.approve_action(self.action, self.outsider)

    def test_approve_separation_of_duties(self):
        """责任人即使拥有质量角色也不能批准自己的措施计划。"""
        self.make_plan()
        self.owner.groups.add(self.quality_group)
        services.submit_plan(self.action, self.owner)
        with self.assertRaises(services.PermissionDenied):
            services.approve_action(self.action, self.owner)

    def test_plan_frozen_after_approval(self):
        """批准后计划固化, API 修改被拒绝。"""
        plan, metric, _ = self.make_plan()
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)

        self.client.force_authenticate(self.qa)
        resp = self.client.patch(f"/api/plans/{plan.id}/", {"min_sample_size": 99})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.patch(f"/api/metrics/{metric.id}/", {"target": "9.9"})
        self.assertEqual(resp.status_code, 400)
        plan.refresh_from_db()
        self.assertEqual(plan.min_sample_size, 3)

    def test_evidence_self_approval_forbidden(self):
        """责任人不能批准自己提交的证据(即使拥有质量角色)。"""
        self.make_plan()
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)
        evidence = services.submit_evidence(self.action, self.owner, title="校准报告")
        self.owner.groups.add(self.quality_group)
        with self.assertRaises(services.PermissionDenied):
            services.review_evidence(evidence, self.owner, approve=True)

    def test_evidence_review_requires_quality(self):
        self.make_plan()
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)
        evidence = services.submit_evidence(self.action, self.owner, title="校准报告")
        with self.assertRaises(services.PermissionDenied):
            services.review_evidence(evidence, self.outsider, approve=True)

    def test_complete_requires_approved_evidence(self):
        self.make_plan()
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)
        with self.assertRaises(services.BusinessRuleViolation):
            services.complete_action(self.action, self.owner)
        services.submit_evidence(self.action, self.owner, title="校准报告")
        with self.assertRaises(services.BusinessRuleViolation):
            services.complete_action(self.action, self.owner)

    def test_close_requires_quality_role(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        services.evaluate_action(self.action, actor=self.qa)
        services.submit_for_review(self.action, self.owner)
        with self.assertRaises(services.PermissionDenied):
            services.close_action(self.action, self.owner, decision="close_effective")

    def test_close_override_system_requires_comment(self):
        """签署与系统判定不一致时必须填写说明。"""
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        services.evaluate_action(self.action, actor=self.qa)
        services.submit_for_review(self.action, self.owner)
        with self.assertRaises(services.BusinessRuleViolation):
            services.close_action(self.action, self.qa, decision="close_ineffective")
        signoff = services.close_action(
            self.action, self.qa, decision="close_ineffective",
            comment="现场复核发现未记录的干预, 按无效关闭")
        self.assertEqual(signoff.decision, "close_ineffective")

    def test_withdraw_signoff_creates_version_with_reason(self):
        signoff = self.drive_to_closed()
        self.assertEqual(self.action.status, CAPAAction.Status.CLOSED_EFFECTIVE)

        # 无理由撤回被拒绝
        with self.assertRaises(services.BusinessRuleViolation):
            services.withdraw_signoff(signoff, self.qa, reason="")
        # 非签署人撤回被拒绝
        with self.assertRaises(services.PermissionDenied):
            services.withdraw_signoff(signoff, self.qa2, reason="非本人撤回")

        new_version = services.withdraw_signoff(signoff, self.qa, reason="发现新风险, 撤回重审")
        signoff.refresh_from_db()
        self.assertEqual(signoff.state, SignOff.State.WITHDRAWN)
        self.assertEqual(signoff.withdrawn_reason, "发现新风险, 撤回重审")
        self.assertEqual(new_version.version, signoff.version + 1)
        self.assertEqual(new_version.decision, SignOff.Decision.WITHDRAW)
        self.assertEqual(new_version.supersedes, signoff)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.PENDING_REVIEW)
        # 历史版本保留
        self.assertEqual(SignOff.objects.filter(action=self.action).count(), 2)

    def test_submit_for_review_blocked_when_data_insufficient(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8])  # 只有 1 个样本, 要求 3 个
        evaluation = services.evaluate_action(self.action, actor=self.qa)
        self.assertEqual(evaluation.result, "inconclusive")
        self.assertFalse(evaluation.eligible_for_review)
        with self.assertRaises(services.BusinessRuleViolation):
            services.submit_for_review(self.action, self.owner)

    def test_reading_rejected_outside_observation_phase(self):
        """措施未进入观察期时不能录入观察数据。"""
        plan, metric, _ = self.make_plan()
        with self.assertRaises(services.BusinessRuleViolation):
            services.record_reading(metric, self.owner, batch_no="B001", value="0.8")
