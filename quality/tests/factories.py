"""测试公共工厂。"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from quality.models import (
    CorrectiveAction,
    Deviation,
    UserProfile,
)
from quality.roles import Role
from quality import services


def make_user(username, role, superuser=False):
    user = User.objects.create_user(
        username=username, password="pass1234", is_superuser=superuser
    )
    if not superuser:
        UserProfile.objects.create(user=user, role=role)
    return user


def make_deviation(code="DEV-001", batch_no="B20260901",
                   created_by=None, **kwargs):
    return Deviation.objects.create(
        code=code,
        title=f"灌装量偏差 {code}",
        description="连续批次灌装量偏离标准区间",
        batch_no=batch_no,
        created_by=created_by,
        **kwargs,
    )


def make_action(deviation, responsible, code="CA-001",
                created_by=None, **kwargs):
    return CorrectiveAction.objects.create(
        code=code,
        deviation=deviation,
        title="校准灌装阀并修订称重复核 SOP",
        responsible_user=responsible,
        created_by=created_by or responsible,
        **kwargs,
    )


# 典型的“灌装量越低越好”指标
FILL_METRIC = {
    "name": "平均灌装量偏差(mL)",
    "metric_type": "mean",
    "unit": "mL",
    "direction": "decrease",
    "baseline_value": 8.0,
    "target_value": 2.0,
    "fail_condition": "aggregate_threshold",
    "fail_threshold": 3.0,
}


def setup_action(*, qa, owner, window_days=30, min_sample_size=5,
                 max_extensions=3, metrics=None, freeze=True):
    """创建 偏差+措施，默认由质量角色固化基线。"""
    deviation = make_deviation(created_by=owner)
    action = make_action(deviation, owner, created_by=owner)
    if freeze:
        services.freeze_baseline(
            action=action, user=qa,
            window_days=window_days,
            min_sample_size=min_sample_size,
            max_extensions=max_extensions,
            metrics=metrics or [dict(FILL_METRIC)],
        )
    return deviation, action


def approve_completion(action, *, owner, qa, title="灌装阀校准记录"):
    """责任人提交证据，质量角色批准。"""
    from quality.models import CompletionEvidence
    evidence = CompletionEvidence.objects.create(
        action=action, title=title, submitted_by=owner,
    )
    return services.approve_evidence(
        evidence=evidence, user=qa, approved=True,
        review_note="记录完整")


def complete_action(action, qa, backdate_hours=20):
    """完成登记并默认把窗口整体前移，便于在窗口内排布历史观察点。"""
    ev = services.mark_action_completed(action=action, user=qa)
    if backdate_hours:
        new_start = timezone.now() - timedelta(hours=backdate_hours)
        action.completed_at = new_start
        action.save(update_fields=["completed_at"])
        ev.window_start = new_start
        ev.window_end = new_start + timedelta(days=action.baseline.window_days)
        ev.save(update_fields=["window_start", "window_end"])
    return ev


def add_values(action, values, *, user, start=None, gap_minutes=1,
               sample_count=1, metric_index=0):
    """按时间顺序向指标录入观察值，返回 (metric, observations)。"""
    metric = action.metrics.all()[metric_index]
    # 默认从观察窗口起点（完成时刻）之后按分钟排布
    if start is None:
        if action.completed_at:
            start = action.completed_at + timedelta(minutes=1)
        else:
            start = (
                timezone.now() - timedelta(minutes=len(values))
                - timedelta(seconds=5)
            )
    obs = []
    for i, v in enumerate(values):
        obs.append(services.add_observation(
            metric=metric, user=user, value=v,
            observed_at=start + timedelta(minutes=gap_minutes * i),
            sample_count=sample_count,
        ))
    return metric, obs
