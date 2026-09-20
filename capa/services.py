"""
业务服务层: 所有状态变更与判定逻辑集中在此, 视图层只做参数解析。

关键规则:
1. 措施批准时固化计划(基线/目标/窗口/失败条件), 固化后禁止直接修改。
2. 措施完成 ≠ 有效: 证据齐全仅使措施进入观察期。
3. 窗口内达到最小样本量且未触发失败条件, 才允许提交有效性评审。
4. 数据不足时延长观察, 绝不判为成功。
5. 观察期内出现关联新偏差 → 自动重开判定, 历史结论保留。
6. 责任人不可批准自己的证据; 关闭决定必须由质量角色签署。
7. 撤回签署产生带理由的新版本, 历史版本保留。
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from .models import (
    AuditLog,
    CAPAAction,
    Deviation,
    EffectivenessEvaluation,
    EffectivenessPlan,
    Evidence,
    MetricReading,
    SignOff,
)


class BusinessRuleViolation(Exception):
    """业务规则被违反时抛出, 视图层转换为 400。"""


class PermissionDenied(BusinessRuleViolation):
    """职责分离 / 角色校验失败, 视图层转换为 403。"""


# ---------------------------------------------------------------- 工具

def user_is_quality(user) -> bool:
    """质量角色: 属于 Quality 组或为 staff。"""
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    group_name = getattr(settings, "CAPA_QUALITY_GROUP", "Quality")
    return user.groups.filter(name=group_name).exists()


def ensure_quality_group_exists():
    Group.objects.get_or_create(name=getattr(settings, "CAPA_QUALITY_GROUP", "Quality"))


def log_audit(actor, action, entity, summary="", detail=None):
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        entity_type=entity.__class__.__name__,
        entity_id=str(entity.pk),
        summary=summary,
        detail=detail or {},
    )


def _set_status(action: CAPAAction, new_status: str, actor, summary, detail=None):
    old = action.status
    action.status = new_status
    action.save(update_fields=["status", "updated_at"])
    log_audit(actor, "capa.status_change", action, summary,
              {"from": old, "to": new_status, **(detail or {})})


# ---------------------------------------------------------------- 计划与批准

@transaction.atomic
def submit_plan(action: CAPAAction, user):
    """草稿 → 待批准。要求已定义计划且至少一个观察指标。"""
    if action.status != CAPAAction.Status.DRAFT:
        raise BusinessRuleViolation(f"当前状态 {action.get_status_display()} 不允许提交批准")
    plan = action.current_plan
    if plan is None:
        raise BusinessRuleViolation("尚未定义有效性判定计划")
    if not plan.metrics.exists():
        raise BusinessRuleViolation("计划至少需要一个观察指标")
    _set_status(action, CAPAAction.Status.PENDING_APPROVAL, user, "提交措施批准")
    return action


@transaction.atomic
def approve_action(action: CAPAAction, user):
    """
    待批准 → 已批准。批准时固化计划。

    责任分离: 批准人不能是措施责任人。
    """
    if action.status != CAPAAction.Status.PENDING_APPROVAL:
        raise BusinessRuleViolation(f"当前状态 {action.get_status_display()} 不允许批准")
    if not user_is_quality(user):
        raise PermissionDenied("只有质量角色可以批准措施")
    if user == action.owner:
        raise PermissionDenied("责任分离: 责任人不能批准自己的措施计划")
    plan = action.current_plan
    if plan is None or not plan.metrics.exists():
        raise BusinessRuleViolation("计划或观察指标缺失, 无法批准")

    plan.is_frozen = True
    plan.approved_by = user
    plan.approved_at = timezone.now()
    plan.save(update_fields=["is_frozen", "approved_by", "approved_at"])
    log_audit(user, "capa.plan_frozen", plan, "批准并固化有效性判定计划", {
        "observation_start": str(plan.observation_start),
        "observation_end": str(plan.observation_end),
        "min_sample_size": plan.min_sample_size,
        "metrics": [m.name for m in plan.metrics.all()],
        "failure_conditions": [fc.description for fc in plan.failure_conditions.all()],
    })
    _set_status(action, CAPAAction.Status.APPROVED, user, "措施批准, 计划已固化")
    return action


# ---------------------------------------------------------------- 证据

@transaction.atomic
def submit_evidence(action: CAPAAction, user, **fields) -> Evidence:
    if action.status != CAPAAction.Status.APPROVED:
        raise BusinessRuleViolation("措施处于执行阶段(已批准)时才能提交证据")
    evidence = Evidence.objects.create(action=action, submitted_by=user, **fields)
    log_audit(user, "capa.evidence_submit", evidence, f"提交证据: {evidence.title}")
    return evidence


@transaction.atomic
def review_evidence(evidence: Evidence, user, approve: bool, comment="") -> Evidence:
    """
    审核证据。

    责任分离: 提交人(责任人)不能批准/驳回自己的证据;
    审核人须具备质量角色。
    """
    if evidence.status != Evidence.Status.SUBMITTED:
        raise BusinessRuleViolation("该证据已审核, 不能重复操作")
    if user == evidence.submitted_by:
        raise PermissionDenied("责任分离: 不能审核自己提交的证据")
    if not user_is_quality(user):
        raise PermissionDenied("只有质量角色可以审核证据")
    evidence.status = Evidence.Status.APPROVED if approve else Evidence.Status.REJECTED
    evidence.reviewed_by = user
    evidence.reviewed_at = timezone.now()
    evidence.review_comment = comment
    evidence.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment"])
    log_audit(user, "capa.evidence_review", evidence,
              f"{'批准' if approve else '驳回'}证据: {evidence.title}",
              {"comment": comment})
    return evidence


@transaction.atomic
def complete_action(action: CAPAAction, user):
    """
    执行完成 → 进入观察期。

    完成 ≠ 有效: 仅表示措施执行完毕, 有效性由观察期数据判定。
    要求: 至少一条已批准证据, 且无待审核证据。
    """
    if action.status != CAPAAction.Status.APPROVED:
        raise BusinessRuleViolation(f"当前状态 {action.get_status_display()} 不能标记完成")
    if user != action.owner and not user_is_quality(user):
        raise PermissionDenied("只有责任人或质量角色可以标记措施完成")
    qs = action.evidence.all()
    if not qs.filter(status=Evidence.Status.APPROVED).exists():
        raise BusinessRuleViolation("至少需要一条已批准的完成证据")
    if qs.filter(status=Evidence.Status.SUBMITTED).exists():
        raise BusinessRuleViolation("存在待审核证据, 请先完成审核")
    _set_status(action, CAPAAction.Status.MONITORING, user,
                "措施执行完成, 进入观察期(完成不等于有效)")
    return action


# ---------------------------------------------------------------- 读数

@transaction.atomic
def record_reading(metric, user, batch_no, value, recorded_at=None) -> MetricReading:
    action = metric.plan.action
    if action.status not in (CAPAAction.Status.MONITORING,
                             CAPAAction.Status.PENDING_REVIEW,
                             CAPAAction.Status.REOPENED):
        raise BusinessRuleViolation("措施未处于观察期, 不能录入观察数据")
    reading = MetricReading.objects.create(
        metric=metric, batch_no=batch_no, value=value,
        recorded_at=recorded_at or timezone.now(), recorded_by=user,
    )
    log_audit(user, "capa.reading_record", reading,
              f"录入读数 {metric.name} {batch_no}={value}")
    return reading


# ---------------------------------------------------------------- 评估引擎

def _trend(values, direction):
    """基于前后半段均值比较判断趋势: improving / worsening / stable / insufficient_data。"""
    if len(values) < 2:
        return "insufficient_data"
    half = len(values) // 2
    first = sum(values[:half]) / half
    second = sum(values[half:]) / (len(values) - half)
    tolerance = abs(first) * 0.01 or 1e-9
    if abs(second - first) <= tolerance:
        return "stable"
    improving = second > first if direction == "increase" else second < first
    return "improving" if improving else "worsening"


def _meets_target(mean_value, metric) -> bool:
    if metric.direction == "increase":
        return mean_value >= metric.target
    return mean_value <= metric.target


def collect_window_data(plan: EffectivenessPlan):
    """
    汇总当前计划窗口内的观察数据。

    返回 (metrics_summary, sample_size, failures):
    - metrics_summary: {指标名: {count, mean, min, max, latest, trend, meets_target, ...}}
    - sample_size: 各指标窗口内样本量的最小值(瓶颈指标)
    - failures: 触发失败条件的样本描述列表
    """
    summary = {}
    failures = []
    counts = []
    for metric in plan.metrics.all():
        readings = list(metric.readings.filter(
            recorded_at__date__gte=plan.observation_start,
            recorded_at__date__lte=plan.observation_end,
        ).order_by("recorded_at"))
        values = [r.value for r in readings]
        count = len(values)
        counts.append(count)
        mean_value = (sum(values) / count) if count else None
        entry = {
            "unit": metric.unit,
            "baseline": float(metric.baseline),
            "target": float(metric.target),
            "direction": metric.direction,
            "count": count,
            "mean": float(mean_value) if mean_value is not None else None,
            "min": float(min(values)) if values else None,
            "max": float(max(values)) if values else None,
            "latest": float(values[-1]) if values else None,
            "trend": _trend([float(v) for v in values], metric.direction),
            "meets_target": (_meets_target(mean_value, metric)
                             if mean_value is not None else None),
        }
        summary[metric.name] = entry
        for fc in metric.failure_conditions.all():
            for r in readings:
                if fc.is_triggered_by(r.value):
                    failures.append(
                        f"失败条件触发: {fc.description} "
                        f"(批号 {r.batch_no} 读数 {r.value} @ {r.recorded_at:%Y-%m-%d})"
                    )
    sample_size = min(counts) if counts else 0
    return summary, sample_size, failures


@transaction.atomic
def evaluate_action(action: CAPAAction, actor=None, force=False) -> EffectivenessEvaluation:
    """
    对措施执行一次有效性判定, 产生新的判定版本(历史版本保留)。

    force=False 时, 若结论与当前版本相同则不重复建版(供定时任务使用)。
    """
    if action.status not in (CAPAAction.Status.MONITORING,
                             CAPAAction.Status.PENDING_REVIEW,
                             CAPAAction.Status.REOPENED):
        raise BusinessRuleViolation(f"当前状态 {action.get_status_display()} 不能执行有效性判定")
    plan = action.current_plan
    if plan is None or not plan.is_frozen:
        raise BusinessRuleViolation("缺少已固化的判定计划")

    today = timezone.localdate()
    summary, sample_size, failures = collect_window_data(plan)
    reasons = []
    extension_days = getattr(settings, "CAPA_EXTENSION_DAYS", 30)

    if failures:
        result = EffectivenessEvaluation.Result.INEFFECTIVE
        eligible = True
        reasons.extend(failures)
        reasons.append("观察窗口内触发失败条件, 建议判定为无效")
    elif sample_size < plan.min_sample_size:
        eligible = False
        if today > plan.observation_end:
            # 数据不足且窗口已过 → 受治理延长, 绝不判为成功
            old_end = plan.observation_end
            plan.observation_end = old_end + timedelta(days=extension_days)
            plan.extension_count += 1
            plan.save(update_fields=["observation_end", "extension_count"])
            result = EffectivenessEvaluation.Result.EXTENDED
            reasons.append(
                f"窗口内样本量 {sample_size}/{plan.min_sample_size}, 数据不足, "
                f"观察期由 {old_end} 延长至 {plan.observation_end}(第 {plan.extension_count} 次延长)"
            )
            log_audit(actor, "capa.observation_extended", plan,
                      "数据不足, 观察期延长",
                      {"old_end": str(old_end), "new_end": str(plan.observation_end),
                       "sample_size": sample_size,
                       "min_sample_size": plan.min_sample_size})
        else:
            result = EffectivenessEvaluation.Result.INCONCLUSIVE
            reasons.append(
                f"窗口内样本量 {sample_size}/{plan.min_sample_size}, "
                f"数据不足, 继续观察至 {plan.observation_end}"
            )
    else:
        unmet = [name for name, s in summary.items() if s["meets_target"] is False]
        reasons.append(f"窗口内样本量 {sample_size}/{plan.min_sample_size}, 未触发失败条件")
        if unmet:
            result = EffectivenessEvaluation.Result.INEFFECTIVE
            eligible = True
            reasons.append("以下指标未达目标: " + ", ".join(unmet))
        else:
            result = EffectivenessEvaluation.Result.EFFECTIVE
            eligible = True
            reasons.append("全部指标达到目标, 建议判定为有效")

    current = action.current_evaluation
    if not force and current and current.result == result:
        return current

    action.evaluations.filter(is_current=True).update(is_current=False)
    version = (action.evaluations.count() or 0) + 1
    evaluation = EffectivenessEvaluation.objects.create(
        action=action, version=version, result=result, rationale=reasons,
        metrics_summary=summary, sample_size=sample_size,
        eligible_for_review=eligible, created_by=actor, is_current=True,
    )
    log_audit(actor, "capa.evaluate", evaluation,
              f"有效性判定 v{version}: {evaluation.get_result_display()}",
              {"result": result, "sample_size": sample_size, "eligible": eligible})
    return evaluation


@transaction.atomic
def submit_for_review(action: CAPAAction, user):
    """观察中/已重开 → 待评审。仅当当前判定允许提交(样本量达标且未触发失败条件, 或已触发失败条件)。"""
    if action.status not in (CAPAAction.Status.MONITORING, CAPAAction.Status.REOPENED):
        raise BusinessRuleViolation(f"当前状态 {action.get_status_display()} 不能提交评审")
    evaluation = action.current_evaluation
    if evaluation is None or not evaluation.eligible_for_review:
        raise BusinessRuleViolation(
            "当前判定不允许提交评审: 需窗口内达到最小样本量且未触发失败条件"
            "(数据不足时应延长观察, 不能判为成功)"
        )
    _set_status(action, CAPAAction.Status.PENDING_REVIEW, user,
                "提交有效性评审", {"evaluation_version": evaluation.version})
    return action


# ---------------------------------------------------------------- 关闭与签署

@transaction.atomic
def close_action(action: CAPAAction, user, decision: str, comment="") -> SignOff:
    """
    质量角色签署关闭。

    若签署决定与系统判定结论不一致, 必须填写说明(冲突意见留痕)。
    """
    if not user_is_quality(user):
        raise PermissionDenied("关闭决定必须由质量角色签署")
    if action.status != CAPAAction.Status.PENDING_REVIEW:
        raise BusinessRuleViolation(f"当前状态 {action.get_status_display()} 不能签署关闭")
    if decision not in (SignOff.Decision.CLOSE_EFFECTIVE, SignOff.Decision.CLOSE_INEFFECTIVE):
        raise BusinessRuleViolation("非法的关闭决定")
    evaluation = action.current_evaluation
    if evaluation is None or not evaluation.eligible_for_review:
        raise BusinessRuleViolation("缺少可评审的判定结论")

    expected = (SignOff.Decision.CLOSE_EFFECTIVE
                if evaluation.result == EffectivenessEvaluation.Result.EFFECTIVE
                else SignOff.Decision.CLOSE_INEFFECTIVE)
    if decision != expected and not comment.strip():
        raise BusinessRuleViolation(
            "签署决定与系统判定不一致, 必须填写说明以记录冲突意见"
        )

    version = (action.signoffs.count() or 0) + 1
    signoff = SignOff.objects.create(
        action=action, evaluation=evaluation, version=version,
        decision=decision, comment=comment, user=user,
    )
    log_audit(user, "capa.signoff", signoff,
              f"签署关闭 v{version}: {signoff.get_decision_display()}",
              {"comment": comment, "overrode_system": decision != expected})
    new_status = (CAPAAction.Status.CLOSED_EFFECTIVE
                  if decision == SignOff.Decision.CLOSE_EFFECTIVE
                  else CAPAAction.Status.CLOSED_INEFFECTIVE)
    _set_status(action, new_status, user, "质量签署关闭",
                {"signoff_version": version})
    return signoff


@transaction.atomic
def withdraw_signoff(signoff: SignOff, user, reason: str) -> SignOff:
    """
    撤回签署: 原签署置为已撤回, 并生成一条带理由的新版本记录。
    仅签署人本人可撤回; 措施回到待评审状态。
    """
    if not reason or not reason.strip():
        raise BusinessRuleViolation("撤回签署必须填写理由")
    if signoff.user != user:
        raise PermissionDenied("只有签署人本人可以撤回该签署")
    if signoff.state != SignOff.State.ACTIVE:
        raise BusinessRuleViolation("该签署已撤回, 不能重复操作")
    if signoff.decision == SignOff.Decision.WITHDRAW:
        raise BusinessRuleViolation("撤回记录本身不能再被撤回")

    signoff.state = SignOff.State.WITHDRAWN
    signoff.withdrawn_reason = reason
    signoff.withdrawn_at = timezone.now()
    signoff.save(update_fields=["state", "withdrawn_reason", "withdrawn_at"])

    action = signoff.action
    new_version = SignOff.objects.create(
        action=action, evaluation=signoff.evaluation,
        version=signoff.version + 1, decision=SignOff.Decision.WITHDRAW,
        comment=reason, user=user, supersedes=signoff,
    )
    log_audit(user, "capa.signoff_withdraw", new_version,
              f"撤回签署 v{signoff.version}, 生成新版本 v{new_version.version}",
              {"reason": reason})
    if action.status in (CAPAAction.Status.CLOSED_EFFECTIVE,
                         CAPAAction.Status.CLOSED_INEFFECTIVE):
        _set_status(action, CAPAAction.Status.PENDING_REVIEW, user,
                    "签署被撤回, 措施回到待评审",
                    {"withdrawn_signoff": signoff.version})
    return new_version


# ---------------------------------------------------------------- 偏差关联与重开

@transaction.atomic
def link_deviation(deviation: Deviation, action: CAPAAction, user) -> Deviation:
    """
    将偏差关联到措施。若措施处于观察/评审/已关闭状态, 自动重开判定:
    状态回到"已重开", 生成 REOPENED 判定版本; 历史结论全部保留。
    """
    deviation.linked_action = action
    deviation.save(update_fields=["linked_action"])
    log_audit(user, "capa.deviation_linked", deviation,
              f"偏差 {deviation.code} 关联到措施 {action.code}")

    reopenable = (CAPAAction.Status.MONITORING, CAPAAction.Status.PENDING_REVIEW,
                  CAPAAction.Status.CLOSED_EFFECTIVE, CAPAAction.Status.CLOSED_INEFFECTIVE,
                  CAPAAction.Status.REOPENED)
    if action.status in reopenable:
        previous = action.status
        if action.status != CAPAAction.Status.REOPENED:
            _set_status(action, CAPAAction.Status.REOPENED, user,
                        f"关联新偏差 {deviation.code}, 判定自动重开",
                        {"previous_status": previous, "deviation": deviation.code})
        action.evaluations.filter(is_current=True).update(is_current=False)
        version = (action.evaluations.count() or 0) + 1
        EffectivenessEvaluation.objects.create(
            action=action, version=version,
            result=EffectivenessEvaluation.Result.REOPENED,
            rationale=[f"观察/关闭后出现关联偏差 {deviation.code}({deviation.title}), "
                       f"原结论保留于历史版本, 需重新收集数据并判定"],
            sample_size=0, eligible_for_review=False,
            created_by=user, is_current=True,
        )
    return deviation


@transaction.atomic
def extend_observation(action: CAPAAction, user, days: int, reason: str):
    """质量角色手动延长观察期(受治理的唯一计划变更途径)。"""
    if not user_is_quality(user):
        raise PermissionDenied("只有质量角色可以延长观察期")
    if not reason or not reason.strip():
        raise BusinessRuleViolation("延长观察期必须填写理由")
    if days <= 0:
        raise BusinessRuleViolation("延长天数必须为正数")
    plan = action.current_plan
    if plan is None or not plan.is_frozen:
        raise BusinessRuleViolation("缺少已固化的判定计划")
    old_end = plan.observation_end
    plan.observation_end = old_end + timedelta(days=days)
    plan.extension_count += 1
    plan.save(update_fields=["observation_end", "extension_count"])
    log_audit(user, "capa.observation_extended", plan, "手动延长观察期",
              {"old_end": str(old_end), "new_end": str(plan.observation_end),
               "reason": reason})
    return plan


# ---------------------------------------------------------------- 负责人看板

def build_action_dashboard(action: CAPAAction) -> dict:
    """为质量负责人汇总单项措施的全貌与系统建议。"""
    plan = action.current_plan
    evaluation = action.current_evaluation
    today = timezone.localdate()

    evidence_qs = action.evidence.all()
    evidence_total = evidence_qs.count()
    evidence_approved = evidence_qs.filter(status=Evidence.Status.APPROVED).count()

    if plan and plan.is_frozen:
        summary, sample_size, failures = collect_window_data(plan)
        days_remaining = (plan.observation_end - today).days
        observation = {
            "start": str(plan.observation_start),
            "end": str(plan.observation_end),
            "days_remaining": days_remaining,
            "min_sample_size": plan.min_sample_size,
            "current_sample_size": sample_size,
            "extension_count": plan.extension_count,
        }
        metrics = [
            {"name": name, **data} for name, data in summary.items()
        ]
    else:
        observation = None
        metrics = []
        failures = []

    opinions = {"agree": 0, "disagree": 0, "conflicts": []}
    if evaluation:
        for op in evaluation.opinions.select_related("user"):
            opinions[op.stance] += 1
            if op.stance == "disagree":
                opinions["conflicts"].append(
                    {"user": op.user.username, "comment": op.comment}
                )

    recommendation = _recommendation(action, evaluation, plan)

    return {
        "action_id": action.pk,
        "code": action.code,
        "title": action.title,
        "status": action.status,
        "status_display": action.get_status_display(),
        "owner": action.owner.username,
        "deviation": action.deviation.code,
        "evidence": {
            "total": evidence_total,
            "approved": evidence_approved,
            "pending": evidence_qs.filter(status=Evidence.Status.SUBMITTED).count(),
            "coverage": round(evidence_approved / evidence_total, 4) if evidence_total else 0.0,
        },
        "observation": observation,
        "metrics": metrics,
        "failure_conditions_triggered": failures,
        "current_evaluation": (
            {
                "version": evaluation.version,
                "result": evaluation.result,
                "result_display": evaluation.get_result_display(),
                "rationale": evaluation.rationale,
                "eligible_for_review": evaluation.eligible_for_review,
                "created_at": evaluation.created_at.isoformat(),
                "created_by": evaluation.created_by.username if evaluation.created_by else "system",
            }
            if evaluation else None
        ),
        "opinions": opinions,
        "recommendation": recommendation,
    }


def _recommendation(action, evaluation, plan) -> dict:
    """把当前判定翻译成给负责人的可解释建议。"""
    if action.status == CAPAAction.Status.DRAFT:
        return {"code": "define_plan", "reasons": ["措施尚在草稿, 请完善判定计划并提交批准"]}
    if action.status == CAPAAction.Status.PENDING_APPROVAL:
        return {"code": "await_approval", "reasons": ["等待质量角色批准并固化判定计划"]}
    if action.status == CAPAAction.Status.APPROVED:
        return {"code": "execute_and_evidence", "reasons": ["执行措施并提交完成证据"]}
    if evaluation is None:
        return {"code": "run_evaluation", "reasons": ["尚未产生判定, 请运行有效性评估"]}
    if evaluation.result == EffectivenessEvaluation.Result.EFFECTIVE:
        return {"code": "propose_effective",
                "reasons": evaluation.rationale + ["可提交评审并建议签署关闭(有效)"]}
    if evaluation.result == EffectivenessEvaluation.Result.INEFFECTIVE:
        return {"code": "propose_ineffective",
                "reasons": evaluation.rationale + ["建议评审后按无效关闭并启动新一轮 CAPA"]}
    if evaluation.result == EffectivenessEvaluation.Result.EXTENDED:
        return {"code": "extend_observation", "reasons": evaluation.rationale}
    if evaluation.result == EffectivenessEvaluation.Result.REOPENED:
        return {"code": "reopened_assess",
                "reasons": evaluation.rationale + ["历史结论已保留, 需重新积累窗口数据"]}
    return {"code": "continue_monitoring", "reasons": evaluation.rationale}
