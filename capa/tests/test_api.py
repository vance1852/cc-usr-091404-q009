"""API 层测试: 偏差关联自动重开、审计接口、负责人看板、权限。"""
from capa import services
from capa.models import CAPAAction, EffectivenessEvaluation, SignOff
from capa.tests.base import CAPABaseTestCase


class ReopenTests(CAPABaseTestCase):
    def test_new_linked_deviation_reopens_closed_action_and_preserves_history(self):
        """已关闭措施关联新偏差 → 自动重开, 历史判定与签署全部保留。"""
        signoff = self.drive_to_closed()
        self.assertEqual(self.action.status, CAPAAction.Status.CLOSED_EFFECTIVE)
        evaluations_before = list(
            EffectivenessEvaluation.objects.filter(action=self.action)
            .values_list("version", "result"))

        self.client.force_authenticate(self.qa)
        resp = self.client.post("/api/deviations/", {
            "code": "DEV-2026-002", "title": "灌装量偏差复发",
            "severity": "major", "linked_action": self.action.id,
        }, format="json")
        self.assertEqual(resp.status_code, 201)

        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.REOPENED)

        # 历史判定保留, 新增 REOPENED 当前版本
        evaluations = EffectivenessEvaluation.objects.filter(action=self.action)
        history = list(evaluations.filter(is_current=False).values_list("version", "result"))
        self.assertEqual(history, evaluations_before)
        current = evaluations.get(is_current=True)
        self.assertEqual(current.result, "reopened")
        self.assertIn("DEV-2026-002", " ".join(current.rationale))

        # 历史签署保留
        signoff.refresh_from_db()
        self.assertEqual(signoff.state, SignOff.State.ACTIVE)

    def test_linked_deviation_reopens_monitoring_action(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        services.evaluate_action(self.action, actor=self.qa)

        self.client.force_authenticate(self.qa)
        resp = self.client.post(f"/api/deviations/{self._new_deviation().id}/link-action/",
                                {"action": self.action.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.REOPENED)

    def _new_deviation(self):
        from capa.models import Deviation
        return Deviation.objects.create(
            code="DEV-2026-003", title="同类偏差", created_by=self.qa)


class AuditApiTests(CAPABaseTestCase):
    def test_audit_trail_written_and_filterable(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()

        self.client.force_authenticate(self.qa)
        resp = self.client.get("/api/audit/", {
            "entity_type": "CAPAAction", "entity_id": str(self.action.id)})
        self.assertEqual(resp.status_code, 200)
        actions = [row["action"] for row in resp.data["results"]]
        self.assertIn("capa.status_change", actions)
        # 每条审计都有操作人与时间
        for row in resp.data["results"]:
            self.assertTrue(row["timestamp"])
            self.assertTrue(row["actor"])

    def test_audit_requires_authentication(self):
        resp = self.client.get("/api/audit/")
        self.assertIn(resp.status_code, (401, 403))


class DashboardApiTests(CAPABaseTestCase):
    def test_dashboard_exposes_full_picture(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.9, 0.8, 0.7])
        services.evaluate_action(self.action, actor=self.qa)

        self.client.force_authenticate(self.qa)
        resp = self.client.get(f"/api/actions/{self.action.id}/dashboard/")
        self.assertEqual(resp.status_code, 200)
        data = resp.data

        # 证据覆盖
        self.assertEqual(data["evidence"]["total"], 1)
        self.assertEqual(data["evidence"]["approved"], 1)
        self.assertEqual(data["evidence"]["coverage"], 1.0)
        # 指标趋势摘要
        m = data["metrics"][0]
        self.assertEqual(m["name"], "灌装量偏差率")
        self.assertEqual(m["trend"], "improving")
        self.assertTrue(m["meets_target"])
        self.assertEqual(m["count"], 3)
        # 剩余观察期
        self.assertGreater(data["observation"]["days_remaining"], 0)
        self.assertEqual(data["observation"]["current_sample_size"], 3)
        # 可解释建议
        self.assertEqual(data["recommendation"]["code"], "propose_effective")
        self.assertTrue(data["recommendation"]["reasons"])
        # 当前判定
        self.assertEqual(data["current_evaluation"]["result"], "effective")

    def test_conflicting_opinions_visible_on_dashboard(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        evaluation = services.evaluate_action(self.action, actor=self.qa)

        self.client.force_authenticate(self.qa)
        self.client.post(f"/api/evaluations/{evaluation.id}/opinions/",
                         {"stance": "agree", "comment": "数据支持"}, format="json")
        self.client.force_authenticate(self.qa2)
        self.client.post(f"/api/evaluations/{evaluation.id}/opinions/",
                         {"stance": "disagree", "comment": "样本批次覆盖不足"}, format="json")

        self.client.force_authenticate(self.qa)
        resp = self.client.get(f"/api/actions/{self.action.id}/dashboard/")
        self.assertEqual(resp.data["opinions"]["agree"], 1)
        self.assertEqual(resp.data["opinions"]["disagree"], 1)
        self.assertEqual(resp.data["opinions"]["conflicts"][0]["comment"], "样本批次覆盖不足")

    def test_overview_lists_all_actions(self):
        self.make_plan()
        self.approve_and_complete()
        self.client.force_authenticate(self.qa)
        resp = self.client.get("/api/actions/overview/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["code"], self.action.code)


class ApiPermissionTests(CAPABaseTestCase):
    def test_unauthenticated_denied(self):
        self.assertIn(self.client.get("/api/actions/").status_code, (401, 403))

    def test_owner_cannot_approve_own_plan_via_api(self):
        self.make_plan()
        self.owner.groups.add(self.quality_group)  # 即使有质量角色也不行
        services.submit_plan(self.action, self.owner)
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/actions/{self.action.id}/approve/")
        self.assertEqual(resp.status_code, 403)

    def test_owner_cannot_approve_own_evidence_via_api(self):
        self.make_plan()
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)
        self.client.force_authenticate(self.owner)
        resp = self.client.post("/api/evidence/", {
            "action": self.action.id, "title": "校准记录"}, format="json")
        self.assertEqual(resp.status_code, 201)
        evidence_id = resp.data["id"]

        self.owner.groups.add(self.quality_group)
        resp = self.client.post(f"/api/evidence/{evidence_id}/approve/")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("责任分离", resp.data["detail"])

    def test_close_via_api_requires_quality(self):
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        services.evaluate_action(self.action, actor=self.qa)
        services.submit_for_review(self.action, self.owner)

        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/actions/{self.action.id}/close/",
                                {"decision": "close_effective"}, format="json")
        self.assertEqual(resp.status_code, 403)

        self.client.force_authenticate(self.qa)
        resp = self.client.post(f"/api/actions/{self.action.id}/close/",
                                {"decision": "close_effective"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.CLOSED_EFFECTIVE)

    def test_withdraw_signoff_via_api(self):
        signoff = self.drive_to_closed()
        self.client.force_authenticate(self.qa)
        # 无理由 → 400
        resp = self.client.post(f"/api/signoffs/{signoff.id}/withdraw/",
                                {"reason": ""}, format="json")
        self.assertEqual(resp.status_code, 400)
        # 带理由 → 新版本
        resp = self.client.post(f"/api/signoffs/{signoff.id}/withdraw/",
                                {"reason": "发现新风险"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["decision"], "withdraw")
        self.assertEqual(resp.data["version"], signoff.version + 1)
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, CAPAAction.Status.PENDING_REVIEW)
