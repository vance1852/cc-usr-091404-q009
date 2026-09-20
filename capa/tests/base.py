"""测试公共基类与夹具。"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.utils import timezone
from rest_framework.test import APITestCase

from capa import services
from capa.models import (
    CAPAAction,
    Deviation,
    EffectivenessPlan,
    FailureCondition,
    ObservationMetric,
)


class CAPABaseTestCase(APITestCase):
    """提供用户角色、偏差、措施与计划的标准夹具。"""

    @classmethod
    def setUpTestData(cls):
        cls.quality_group = Group.objects.create(name="Quality")
        cls.owner = User.objects.create_user("owner", password="pw")
        cls.qa = User.objects.create_user("qa", password="pw")
        cls.qa.groups.add(cls.quality_group)
        cls.qa2 = User.objects.create_user("qa2", password="pw")
        cls.qa2.groups.add(cls.quality_group)
        cls.outsider = User.objects.create_user("outsider", password="pw")

    def setUp(self):
        self.today = timezone.localdate()
        self.deviation = Deviation.objects.create(
            code="DEV-2026-001", title="灌装量偏差",
            description="灌装线A装量漂移", severity="major", created_by=self.qa,
        )
        self.action = CAPAAction.objects.create(
            code="CAPA-2026-001", deviation=self.deviation,
            title="校准灌装机并更换密封件", owner=self.owner,
        )

    # ---------------------------------------------------------------- 夹具辅助

    def make_plan(self, start_offset=-10, end_offset=20, min_samples=3,
                  target="1.0", threshold="2.0"):
        """为 self.action 创建判定计划 + 一个指标 + 一条失败条件。"""
        plan = EffectivenessPlan.objects.create(
            action=self.action,
            observation_start=self.today + timedelta(days=start_offset),
            observation_end=self.today + timedelta(days=end_offset),
            min_sample_size=min_samples,
        )
        metric = ObservationMetric.objects.create(
            plan=plan, name="灌装量偏差率", unit="%",
            baseline=Decimal("3.5"), target=Decimal(target), direction="decrease",
        )
        fc = FailureCondition.objects.create(
            plan=plan, metric=metric, operator="gt",
            threshold=Decimal(threshold), description="单批偏差率不得超过阈值",
        )
        return plan, metric, fc

    def approve_and_complete(self):
        """把措施推进到观察中(monitoring)状态。"""
        services.submit_plan(self.action, self.owner)
        services.approve_action(self.action, self.qa)
        evidence = services.submit_evidence(self.action, self.owner, title="维修与校准记录")
        services.review_evidence(evidence, self.qa, approve=True)
        services.complete_action(self.action, self.owner)
        self.action.refresh_from_db()
        return evidence

    def add_readings(self, metric, values, start_offset=-5, step=2):
        """在窗口内依次录入读数。"""
        for i, value in enumerate(values):
            services.record_reading(
                metric, self.owner, batch_no=f"B{i + 1:03d}",
                value=Decimal(str(value)),
                recorded_at=timezone.now() + timedelta(days=start_offset + i * step),
            )

    def drive_to_closed(self):
        """推进到"已关闭·有效", 返回 signoff。"""
        plan, metric, _ = self.make_plan()
        self.approve_and_complete()
        self.add_readings(metric, [0.8, 0.7, 0.9])
        services.evaluate_action(self.action, actor=self.qa)
        services.submit_for_review(self.action, self.owner)
        signoff = services.close_action(self.action, self.qa, decision="close_effective")
        self.action.refresh_from_db()
        return signoff
