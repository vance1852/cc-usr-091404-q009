"""
有效性判定引擎：纯计算 + 状态判定，不直接写库，便于测试。

规则要点：
1. 措施完成 ≠ 有效；完成后进入观察窗口。
2. 窗口内每个指标达到最小样本量且未触发失败条件，才可提交评审。
3. 窗口结束仍数据不足 → 自动延长观察，绝不判为成功；
   达到延长上限仍不足 → DATA_INSUFFICIENT，留待质量角色裁决。
4. 失败条件（聚合越阈 / 单点越限）一旦触发即判定失败，样本不足也不掩盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from django.utils import timezone

from .models import (
    CorrectiveAction,
    EffectivenessEvaluation,
    Metric,
    MetricObservation,
)

# ---- 引擎输出的系统建议码 ----
RECOMMENDATION_CODES = {
    "NOT_COMPLETED": "措施尚未完成，等待执行与证据批准",
    "KEEP_OBSERVING": "继续观察：窗口未结束且样本量不足",
    "READY_EARLY": "窗口内已达最小样本量且无失败信号，可提前提交评审",
    "READY": "观察窗口结束，指标达标，建议提交评审",
    "EXTENDED_LOW_SAMPLE": "数据不足，已自动延长观察窗口（不得判成功）",
    "INSUFFICIENT_AT_LIMIT": "已达延长上限仍数据不足，需质量角色裁决",
    "FAILURE_TRIGGERED": "失败条件已触发，措施判定为无效，禁止提交评审",
    "ALREADY_SUBMITTED": "已提交评审，等待质量角色签署",
    "CLOSED_OUTCOME": "判定已有最终结论",
    "REOPENED": "判定已因相关偏差重开，进入新版本观察",
}


@dataclass
class MetricResult:
    metric_id: int
    name: str
    unit: str
    direction: str
    metric_type: str
    baseline_value: float
    target_value: float
    min_sample_required: int
    n_points: int = 0
    n_samples: int = 0
    aggregate: Optional[float] = None
    breach_count: int = 0
    breach_values: list = field(default_factory=list)
    fail_triggered: bool = False
    fail_reasons: list = field(default_factory=list)
    sample_met: bool = False
    meets_target: Optional[bool] = None
    trend_label: str = "无数据"
    trend_slope: Optional[float] = None
    first_half_mean: Optional[float] = None
    second_half_mean: Optional[float] = None
    values: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "unit": self.unit,
            "direction": self.direction,
            "metric_type": self.metric_type,
            "baseline_value": self.baseline_value,
            "target_value": self.target_value,
            "min_sample_required": self.min_sample_required,
            "n_points": self.n_points,
            "n_samples": self.n_samples,
            "aggregate": self.aggregate,
            "breach_count": self.breach_count,
            "fail_triggered": self.fail_triggered,
            "fail_reasons": self.fail_reasons,
            "sample_met": self.sample_met,
            "meets_target": self.meets_target,
            "trend_label": self.trend_label,
            "trend_slope": self.trend_slope,
            "first_half_mean": self.first_half_mean,
            "second_half_mean": self.second_half_mean,
            "values": self.values,
        }


@dataclass
class EngineResult:
    status: str
    recommendation_code: str
    recommendation_text: str
    reason_lines: list
    metric_results: list
    now: object
    window_start: Optional[object] = None
    window_end: Optional[object] = None
    remaining_seconds: Optional[float] = None
    extended: bool = False
    extension_count: int = 0

    @property
    def remaining_days(self):
        if self.remaining_seconds is None:
            return None
        return round(max(0.0, self.remaining_seconds) / 86400, 1)

    @property
    def status_display(self):
        return (EffectivenessEvaluation.Status(self.status).label
                if self.status in EffectivenessEvaluation.Status.values
                else self.status)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "status_display": EffectivenessEvaluation.Status(self.status).label
            if self.status in EffectivenessEvaluation.Status.values
            else self.status,
            "recommendation_code": self.recommendation_code,
            "recommendation_text": self.recommendation_text,
            "reasons": self.reason_lines,
            "remaining_window_days": self.remaining_days,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "extension_count": self.extension_count,
            "metrics": [m.as_dict() for m in self.metric_results],
        }


def _is_breach(direction: str, value: float, threshold: float) -> bool:
    """单向规格越限：越低越好的指标高于阈值、越高越好的指标低于阈值。"""
    if direction == Metric.Direction.INCREASE:
        return value < threshold
    return value > threshold


def _linear_slope(points: list[float]) -> Optional[float]:
    n = len(points)
    if n < 2:
        return None
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(points) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, points)) / denom


def evaluate_metric(metric: Metric, observations, now) -> MetricResult:
    res = MetricResult(
        metric_id=metric.id,
        name=metric.name,
        unit=metric.unit,
        direction=metric.direction,
        metric_type=metric.metric_type,
        baseline_value=metric.baseline_value,
        target_value=metric.target_value,
        min_sample_required=metric.effective_min_sample,
    )
    obs = sorted(observations, key=lambda o: o.observed_at)
    res.n_points = len(obs)
    res.n_samples = sum(o.sample_count for o in obs)
    res.values = [o.value for o in obs]
    res.sample_met = res.n_samples >= res.min_sample_required

    if obs:
        if metric.metric_type == Metric.MetricType.COUNT:
            res.aggregate = float(sum(o.value for o in obs))
        else:
            total_w = res.n_samples
            res.aggregate = (
                sum(o.value * o.sample_count for o in obs) / total_w
                if total_w
                else float("nan")
            )
        res.meets_target = not _is_breach(
            metric.direction, res.aggregate, metric.target_value
        )
        # 趋势（前半窗口 vs 后半窗口 + 最小二乘斜率）
        half = max(1, len(obs) // 2)
        first = [o.value for o in obs[:half]]
        second = [o.value for o in obs[half:]] if len(obs) > 1 else []
        res.first_half_mean = sum(first) / len(first)
        if second:
            res.second_half_mean = sum(second) / len(second)
        res.trend_slope = _linear_slope(res.values)
        res.trend_label = _trend_label(metric.direction, res)

        threshold = (
            metric.fail_threshold
            if metric.fail_threshold is not None
            else metric.target_value
        )
        # 单点越限
        res.breach_values = [
            o.value for o in obs if _is_breach(metric.direction, o.value, threshold)
        ]
        res.breach_count = len(res.breach_values)
        if (
            metric.fail_condition == Metric.FailCondition.INDIVIDUAL_BREACH
            and res.breach_count > metric.allowed_breaches
        ):
            res.fail_triggered = True
            res.fail_reasons.append(
                f"单点越限 {res.breach_count} 次，超过允许次数 "
                f"{metric.allowed_breaches}（阈值 {threshold:g}）"
            )
        # 聚合值越阈
        if (
            metric.fail_condition == Metric.FailCondition.AGGREGATE_THRESHOLD
            and _is_breach(metric.direction, res.aggregate, threshold)
        ):
            res.fail_triggered = True
            comparator = "高于" if metric.direction == Metric.Direction.DECREASE else "低于"
            res.fail_reasons.append(
                f"聚合值 {res.aggregate:.3g} {comparator}失败阈值 {threshold:g}"
            )
    return res


def _trend_label(direction: str, res: MetricResult) -> str:
    if res.second_half_mean is None or res.first_half_mean is None:
        return "数据点不足"
    delta = res.second_half_mean - res.first_half_mean
    if abs(delta) < 1e-9:
        return "持平"
    improving = (delta < 0) if direction == Metric.Direction.DECREASE else (delta > 0)
    return "改善" if improving else "恶化"


def evaluate(
    evaluation: EffectivenessEvaluation,
    now=None,
    extension_days: Optional[int] = None,
    force_recheck: bool = False,
) -> EngineResult:
    """
    计算当前判定版本的状态与可解释建议。不写库；
    extension_days 仅用于在建议文本中体现延长步长。
    force_recheck=True 时忽略“评审中”等状态短路，按最新数据重新判定
    （用于质量签署前的最终校验）。
    """
    now = now or timezone.now()
    action = evaluation.action
    reasons: list[str] = []

    if action.status != CorrectiveAction.Status.COMPLETED or not action.completed_at:
        return EngineResult(
            status=EffectivenessEvaluation.Status.PENDING,
            recommendation_code="NOT_COMPLETED",
            recommendation_text=RECOMMENDATION_CODES["NOT_COMPLETED"],
            reason_lines=["措施尚未标记完成，或缺少已批准的完成证据"],
            metric_results=[],
            now=now,
            extension_count=evaluation.extension_count,
        )

    window_start = evaluation.window_start or action.completed_at
    window_end = evaluation.window_end
    effective_end = min(now, window_end) if window_end else now
    metric_results = []
    for metric in action.metrics.all():
        obs = list(
            MetricObservation.objects.filter(
                metric=metric,
                observed_at__gte=window_start,
                observed_at__lte=effective_end,
            )
        )
        metric_results.append(evaluate_metric(metric, obs, now))

    # 终态短路：结论状态不变，但仍返回窗口内指标快照供看板展示
    if not force_recheck:
        if evaluation.status == EffectivenessEvaluation.Status.IN_REVIEW:
            return _static(evaluation, now, "ALREADY_SUBMITTED", metric_results)
        if evaluation.status in (
            EffectivenessEvaluation.Status.EFFECTIVE,
            EffectivenessEvaluation.Status.CLOSED,
        ):
            return _static(evaluation, now, "CLOSED_OUTCOME", metric_results)
        if evaluation.status == EffectivenessEvaluation.Status.REOPENED:
            return _static(evaluation, now, "REOPENED", metric_results)

    remaining = (window_end - now).total_seconds() if window_end else None
    window_open = window_end is not None and now < window_end
    any_failure = any(m.fail_triggered for m in metric_results)
    all_sample_met = bool(metric_results) and all(
        m.sample_met for m in metric_results
    )

    # 1) 失败条件优先：任何指标触发失败 → 无效，禁止提交
    if any_failure:
        for m in metric_results:
            for r in m.fail_reasons:
                reasons.append(f"[指标 {m.name}] {r}")
        return EngineResult(
            status=EffectivenessEvaluation.Status.INEFFECTIVE,
            recommendation_code="FAILURE_TRIGGERED",
            recommendation_text=RECOMMENDATION_CODES["FAILURE_TRIGGERED"],
            reason_lines=reasons,
            metric_results=metric_results,
            now=now,
            window_start=window_start,
            window_end=window_end,
            remaining_seconds=remaining,
            extension_count=evaluation.extension_count,
        )

    # 2) 样本齐全且无失败 → 可提交（窗口未结束也允许提前评审）
    if all_sample_met:
        code = "READY_EARLY" if window_open else "READY"
        for m in metric_results:
            reasons.append(
                f"[指标 {m.name}] 样本 {m.n_samples}/{m.min_sample_required}，"
                f"聚合 {_fmt(m.aggregate)}（目标 {_fmt(m.target_value)}），趋势 {m.trend_label}"
            )
        return EngineResult(
            status=EffectivenessEvaluation.Status.READY,
            recommendation_code=code,
            recommendation_text=RECOMMENDATION_CODES[code],
            reason_lines=reasons,
            metric_results=metric_results,
            now=now,
            window_start=window_start,
            window_end=window_end,
            remaining_seconds=remaining,
            extension_count=evaluation.extension_count,
        )

    # 3) 样本不足
    for m in metric_results:
        if not m.sample_met:
            reasons.append(
                f"[指标 {m.name}] 样本 {m.n_samples}/{m.min_sample_required}，"
                f"数据不足，不得据此判定成功"
            )

    if window_open:
        return EngineResult(
            status=EffectivenessEvaluation.Status.OBSERVING,
            recommendation_code="KEEP_OBSERVING",
            recommendation_text=RECOMMENDATION_CODES["KEEP_OBSERVING"],
            reason_lines=reasons,
            metric_results=metric_results,
            now=now,
            window_start=window_start,
            window_end=window_end,
            remaining_seconds=remaining,
            extension_count=evaluation.extension_count,
        )

    # 窗口结束：延长 or 上限
    max_ext = action.baseline.max_extensions
    if evaluation.extension_count < max_ext:
        step = extension_days or action.baseline.window_days
        reasons.append(
            f"观察窗口已结束且样本不足，自动延长 {step} 天"
            f"（第 {evaluation.extension_count + 1}/{max_ext} 次延长）"
        )
        return EngineResult(
            status=EffectivenessEvaluation.Status.EXTENDED,
            recommendation_code="EXTENDED_LOW_SAMPLE",
            recommendation_text=RECOMMENDATION_CODES["EXTENDED_LOW_SAMPLE"],
            reason_lines=reasons,
            metric_results=metric_results,
            now=now,
            window_start=window_start,
            window_end=window_end + timedelta(days=step),
            remaining_seconds=(window_end + timedelta(days=step) - now).total_seconds(),
            extended=True,
            extension_count=evaluation.extension_count + 1,
        )

    reasons.append(
        f"已延长 {max_ext} 次仍未达到最小样本量，系统不判定成功，"
        "须由质量角色决定继续取样或按风险处置"
    )
    return EngineResult(
        status=EffectivenessEvaluation.Status.DATA_INSUFFICIENT,
        recommendation_code="INSUFFICIENT_AT_LIMIT",
        recommendation_text=RECOMMENDATION_CODES["INSUFFICIENT_AT_LIMIT"],
        reason_lines=reasons,
        metric_results=metric_results,
        now=now,
        window_start=window_start,
        window_end=window_end,
        remaining_seconds=0,
        extension_count=evaluation.extension_count,
    )


def _static(evaluation, now, code, metric_results=None) -> EngineResult:
    return EngineResult(
        status=evaluation.status,
        recommendation_code=code,
        recommendation_text=RECOMMENDATION_CODES[code],
        reason_lines=[],
        metric_results=metric_results or [],
        now=now,
        window_start=evaluation.window_start,
        window_end=evaluation.window_end,
        remaining_seconds=(
            (evaluation.window_end - now).total_seconds()
            if evaluation.window_end
            else None
        ),
        extension_count=evaluation.extension_count,
    )


def _fmt(v):
    return "—" if v is None else f"{v:.3g}"
