"""端到端 API 测试：完整 CAPA 有效性流程与权限。"""
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from quality.models import (
    CompletionEvidence,
    CorrectiveAction,
    EffectivenessEvaluation as Eval,
    SignOff,
)
from quality.tests.factories import make_user


class CAPAFlowAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "admin", "admin@example.com", "admin1234")
        self.qa = make_user("qa", "quality")
        self.qa2 = make_user("qa2", "quality")
        self.owner = make_user("owner", "owner")
        self.client.force_authenticate(self.owner)

    def _create_deviation_action(self):
        dev = self.client.post("/api/deviations/", {
            "code": "DEV-100",
            "title": "灌装量反复偏低",
            "batch_no": "B-1",
            "severity": "major",
        }, format="json").json()
        act = self.client.post("/api/actions/", {
            "code": "CA-100",
            "deviation": dev["id"],
            "title": "校准灌装阀+修订SOP",
            "responsible_user": self.owner.id,
        }, format="json").json()
        return dev, act

    def test_user_creation_requires_admin(self):
        resp = self.client.post("/api/users/", {
            "username": "x", "password": "pass1234", "role": "quality"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.admin)
        resp = self.client.post("/api/users/", {
            "username": "newqa", "password": "pass1234", "role": "quality"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_full_effectiveness_flow(self):
        _, act = self._create_deviation_action()

        # 责任人不能批准自己的基线 → 403
        resp = self.client.post(
            f"/api/actions/{act['id']}/freeze_baseline/",
            {"window_days": 30, "min_sample_size": 5,
             "metrics": [{
                 "name": "灌装量偏差", "direction": "decrease",
                 "baseline_value": 8.0, "target_value": 2.0,
                 "fail_condition": "aggregate_threshold",
                 "fail_threshold": 3.0}]},
            format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # 质量角色固化基线
        self.client.force_authenticate(self.qa)
        resp = self.client.post(
            f"/api/actions/{act['id']}/freeze_baseline/",
            {"window_days": 30, "min_sample_size": 5,
             "metrics": [{
                 "name": "灌装量偏差", "direction": "decrease",
                 "baseline_value": 8.0, "target_value": 2.0,
                 "fail_condition": "aggregate_threshold",
                 "fail_threshold": 3.0}]},
            format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        metric_id = resp.json()["metrics"][0]["id"]
        self.assertTrue(resp.json()["metrics"][0]["is_locked"])

        # 固化后指标不可修改
        resp = self.client.patch(f"/api/metrics/{metric_id}/",
                                 {"target_value": 0.1})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        # 责任人提交完成证据
        self.client.force_authenticate(self.owner)
        resp = self.client.post("/api/evidences/", {
            "action": act["id"], "title": "校准记录"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        evidence_id = resp.json()["id"]

        # 责任人自批 → 403
        resp = self.client.post(f"/api/evidences/{evidence_id}/review/",
                                {"approved": True})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # 质量角色批准证据
        self.client.force_authenticate(self.qa)
        resp = self.client.post(f"/api/evidences/{evidence_id}/review/",
                                {"approved": True, "review_note": "完整"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 责任人不能自行确认完成
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/actions/{act['id']}/complete/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # 质量角色确认完成 → 窗口启动
        self.client.force_authenticate(self.qa)
        resp = self.client.post(f"/api/actions/{act['id']}/complete/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 录入达标且呈改善趋势的观察数据
        for i, v in enumerate([1.8, 1.6, 1.2, 1.0, 0.9]):
            resp = self.client.post("/api/observations/", {
                "metric": metric_id, "value": v,
                "sample_count": 1, "batch_no": f"B-{i}"},
                format="json")
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # 看板：剩余观察期 + 证据覆盖 + 建议
        resp = self.client.get(f"/api/actions/{act['id']}/assessment/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["engine"]["status"], "ready")
        self.assertGreaterEqual(data["engine"]["remaining_window_days"], 29)
        self.assertEqual(data["evidence_coverage"]["approved"], 1)
        self.assertEqual(
            data["engine"]["metrics"][0]["trend_label"], "改善")

        # 提交评审（责任人可提交）
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/actions/{act['id']}/submit-review/", {"note": "达标"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 冲突意见
        resp = self.client.post(f"/api/actions/{act['id']}/opinions/", {
            "stance": "challenges", "content": "建议再观察两批"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # 非质量角色不能签署
        resp = self.client.post(f"/api/actions/{act['id']}/sign/", {
            "decision": "approve_close", "reason": "ok"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # 质量签署关闭
        self.client.force_authenticate(self.qa)
        resp = self.client.post(f"/api/actions/{act['id']}/sign/", {
            "decision": "approve_close", "reason": "趋势与目标均达标"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.json()["engine"]["status"], "effective")

        # 撤回签署必须带理由
        resp = self.client.post(
            f"/api/actions/{act['id']}/withdraw-signoff/", {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.client.post(
            f"/api/actions/{act['id']}/withdraw-signoff/",
            {"reason": "新批次出现回弹"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        signoffs = resp.json()["signoffs"]
        self.assertEqual(len(signoffs), 2)  # 历史保留
        self.assertEqual(signoffs[0]["decision"], "withdrawn")

        action = CorrectiveAction.objects.get(pk=act["id"])
        self.assertEqual(
            action.current_evaluation.status, Eval.Status.IN_REVIEW)

    def test_related_deviation_reopens_evaluation(self):
        _, act = self._create_deviation_action()
        self.client.force_authenticate(self.qa)
        self.client.post(
            f"/api/actions/{act['id']}/freeze_baseline/",
            {"window_days": 30, "min_sample_size": 3,
             "metrics": [{
                 "name": "灌装量偏差", "direction": "decrease",
                 "baseline_value": 8.0, "target_value": 2.0,
                 "fail_threshold": 3.0}]},
            format="json")
        metric_id = CorrectiveAction.objects.get(pk=act["id"]) \
            .metrics.first().id
        ev_obj = CompletionEvidence.objects.create(
            action_id=act["id"], title="ev", submitted_by=self.owner)
        self.client.post(f"/api/evidences/{ev_obj.id}/review/",
                         {"approved": True})
        self.client.post(f"/api/actions/{act['id']}/complete/")
        for v in [1.0, 1.2, 1.1]:
            self.client.post("/api/observations/",
                             {"metric": metric_id, "value": v}, format="json")
        self.client.post(f"/api/actions/{act['id']}/submit-review/", {})
        self.client.post(f"/api/actions/{act['id']}/sign/",
                         {"decision": "approve_close", "reason": "ok"})

        # 新偏差（复发）
        recur = self.client.post("/api/deviations/", {
            "code": "DEV-101", "title": "同型号灌装阀复发",
            "severity": "major"})
        # 由质量角色关联并自动重开
        resp = self.client.post(
            f"/api/actions/{act['id']}/link-related/",
            {"deviation_id": recur.json()["id"],
             "relationship_note": "同类问题再次发生"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = resp.json()
        self.assertEqual(body["evaluation_version"], 2)
        self.assertEqual(body["engine"]["status"], "observing")
        # 历史版本仍可查
        versions = body["evaluation_history"]
        self.assertEqual(len(versions), 2)
        v1 = next(v for v in versions if v["version"] == 1)
        self.assertEqual(v1["status"], "reopened")
        self.assertEqual(v1["result"], "effective")

    def test_audit_log_quality_only(self):
        # 在本用例内产生一条审计记录
        dev = self.client.post("/api/deviations/", {
            "code": "DEV-200", "title": "灌装量偏差",
            "severity": "major"}, format="json")
        act = self.client.post("/api/actions/", {
            "code": "CA-200", "deviation": dev.json()["id"],
            "title": "措施", "responsible_user": self.owner.id},
            format="json")
        self.client.force_authenticate(self.qa)
        self.client.post(
            f"/api/actions/{act.json()['id']}/freeze_baseline/",
            {"window_days": 30, "min_sample_size": 5,
             "metrics": [{
                 "name": "灌装量偏差", "baseline_value": 8.0,
                 "target_value": 2.0, "fail_threshold": 3.0}]},
            format="json")

        self.client.force_authenticate(self.owner)
        resp = self.client.get("/api/audit-logs/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.qa)
        resp = self.client.get("/api/audit-logs/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        actions = {row["action"] for row in results}
        self.assertIn("baseline.freeze", actions)

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(None)
        resp = self.client.get("/api/actions/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
