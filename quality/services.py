"""
服务层：封装全部带审计与权限规则的业务操作。

关键不变量：
- 基线（窗口/最小样本/失败条件）在措施批准时固化，之后只读。
- 责任人不可批准自己提交的证据。
- 只有 READY 状态可提交评审；数据不足延长而非成功。
- 相关偏差重开会新增判定版本，历史版本保留。
- 关闭需质量角色签署；撤回签署生成带理由的新版本记录。
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import engine
from .models import (
    AuditLog,
    CompletionEvidence,
    ConflictOpinion,
    CorrectiveAction,
    Deviation,
    EffectivenessBaseline,
    EffectivenessEvaluation,
    Metric,
    MetricObservation,
    RelatedDeviation,
    SignOff,
)
from .roles import is_quality


def _audit(actor, action, entity, entity_id, summary, changes=None):
    AuditLog.objects.create(
        actor=actor if actor and actor.is_authenticated else None,
        actor_name=actor.get_username() if actor and actor.is_authenticated else "system",
        action=action,
        entity_type=entity,
        entity_id=str(entity_id),
        summary=summary,
        changes=changes or {},
    )


# ---------------------------------------------------------------- 偏差

def close_deviation(*, deviation: Deviation, user) -> Deviation:
    """关闭偏差的前提：关联措施的当前判定均已获有效签署/关闭。"""
    if not is_quality(user):
        raise PermissionDenied("只有质量角色可以关闭偏差")
    active_actions = deviation.actions.exclude(
        status=CorrectiveAction.Status.CANCELLED
    )
    blockers = []
    for act in active_actions:
        ev = act.current_evaluation
        if ev is None or ev.status not in (
            EffectivenessEvaluation.Status.EFFECTIVE,
            EffectivenessEvaluation.Status.CLOSED,
        ):
            blockers.append(f"{act.code}（{ev.get_status_display() if ev else '无判定'}）")
    if blockers:
        raise ValidationError(
            f"以下措施尚未判定有效，偏差不能关闭：{'、'.join(blockers)}"
        )
    deviation.status = Deviation.Status.CLOSED
    deviation.closed_at = timezone.now()
    deviation.save(update_fields=["status", "closed_at", "updated_at"])
    _audit(user, "deviation.close", "Deviation", deviation.id,
           f"偏差 {deviation.code} 关闭")
    return deviation


# ---------------------------------------------------------------- 基线与指标

@transaction.atomic
def freeze_baseline(*, action: CorrectiveAction, user, window_days: int,
                    min_sample_size: int, max_extensions: int = 3,
                    note: str = "", metrics: list[dict] | None = None):
    """
    批准措施时固化判定基线与指标定义。
    批准人不得是措施责任人（责任分离）。基线只能固化一次。
    """
    if action.responsible_user_id == user.id:
        raise PermissionDenied("责任人不可批准自己负责的措施基线")
    if hasattr(action, "baseline"):
        raise ValidationError("基线已固化，不可修改；如需调整请走变更评审")
    if window_days <= 0 or min_sample_size <= 0:
        raise ValidationError("观察窗口天数与最小样本量必须为正整数")
    if not metrics:
        raise ValidationError("固化基线时必须至少定义一个观察指标")

    baseline = EffectivenessBaseline.objects.create(
        action=action,
        window_days=window_days,
        min_sample_size=min_sample_size,
        max_extensions=max_extensions,
        approved_by=user,
        note=note,
    )
    for spec in metrics or []:
        Metric.objects.create(
            action=action,
            name=spec["name"],
            metric_type=spec.get("metric_type", Metric.MetricType.MEAN),
            unit=spec.get("unit", ""),
            direction=spec.get("direction", Metric.Direction.DECREASE),
            baseline_value=float(spec["baseline_value"]),
            target_value=float(spec["target_value"]),
            fail_condition=spec.get(
                "fail_condition", Metric.FailCondition.AGGREGATE_THRESHOLD
            ),
            fail_threshold=(
                float(spec["fail_threshold"])
                if spec.get("fail_threshold") is not None
                else None
            ),
            allowed_breaches=int(spec.get("allowed_breaches", 0)),
            min_sample_size=(
                int(spec["min_sample_size"])
                if spec.get("min_sample_size") is not None
                else None
            ),
            is_locked=True,
        )

    if action.status == CorrectiveAction.Status.DRAFT:
        action.status = CorrectiveAction.Status.IN_PROGRESS
        action.save(update_fields=["status", "updated_at"])

    # 基线固化即创建 v1 判定（待观察，窗口起点取完成时间）
    EffectivenessEvaluation.objects.create(
        action=action, version=1,
        status=EffectivenessEvaluation.Status.PENDING,
    )
    _audit(user, "baseline.freeze", "EffectivenessBaseline", baseline.id,
           f"措施 {action.code} 基线固化：窗口 {window_days} 天，"
           f"最小样本 {min_sample_size}，指标 {len(metrics or [])} 项",
           {"window_days": window_days, "min_sample_size": min_sample_size,
            "max_extensions": max_extensions,
            "metrics": [m.get("name") for m in metrics or []]})
    return baseline


# ---------------------------------------------------------------- 证据

@transaction.atomic
def approve_evidence(*, evidence: CompletionEvidence, user, approved: bool,
                     review_note: str = "") -> CompletionEvidence:
    """批准/拒绝完成证据。责任人不可批准自己的证据；批准人须非提交人。"""
    if evidence.submitted_by_id == user.id:
        raise PermissionDenied("责任人不可批准自己提交的证据")
    if evidence.approval_status != CompletionEvidence.ApprovalStatus.PENDING:
        raise ValidationError("该证据已处理，不能重复批准")
    evidence.approval_status = (
        CompletionEvidence.ApprovalStatus.APPROVED if approved
        else CompletionEvidence.ApprovalStatus.REJECTED
    )
    evidence.approved_by = user
    evidence.approved_at = timezone.now()
    evidence.review_note = review_note
    evidence.save()
    _audit(
        user,
        "evidence.approve" if approved else "evidence.reject",
        "CompletionEvidence", evidence.id,
        f"证据《{evidence.title}》{'批准' if approved else '拒绝'}：{review_note}",
    )
    return evidence


@transaction.atomic
def mark_action_completed(*, action: CorrectiveAction, user) -> EffectivenessEvaluation:
    """
    措施完成登记：必须至少有一份已批准证据，且操作人不能是责任人自批。
    完成会打开 v1 观察窗口。完成 ≠ 有效。
    """
    if action.responsible_user_id == user.id:
        raise PermissionDenied("责任人不可自行确认措施完成")
    if not hasattr(action, "baseline"):
        raise ValidationError("措施尚未批准固化判定基线，不能标记完成")
    approved = action.evidences.filter(
        approval_status=CompletionEvidence.ApprovalStatus.APPROVED
    ).exists()
    if not approved:
        raise ValidationError("缺少已批准的完成证据，措施不能标记完成")

    action.status = CorrectiveAction.Status.COMPLETED
    action.completed_at = timezone.now()
    action.save(update_fields=["status", "completed_at", "updated_at"])

    evaluation = action.current_evaluation or EffectivenessEvaluation(
        action=action, version=1
    )
    evaluation.status = EffectivenessEvaluation.Status.OBSERVING
    evaluation.window_start = action.completed_at
    evaluation.window_end = action.completed_at + timedelta(
        days=action.baseline.window_days
    )
    evaluation.save()
    _audit(user, "action.complete", "CorrectiveAction", action.id,
           f"措施 {action.code} 完成，观察窗口 "
           f"{evaluation.window_start:%Y-%m-%d} ~ {evaluation.window_end:%Y-%m-%d}")
    return evaluation


# ---------------------------------------------------------------- 观察数据

def add_observation(*, metric: Metric, user, value: float, observed_at=None,
                    batch_no: str = "", sample_count: int = 1,
                    note: str = "") -> MetricObservation:
    if metric.is_locked is False:
        raise ValidationError("指标定义尚未随基线固化，不能录入观察数据")
    obs = MetricObservation.objects.create(
        metric=metric, value=float(value),
        observed_at=observed_at or timezone.now(),
        batch_no=batch_no, sample_count=sample_count, note=note,
        recorded_by=user,
    )
    _audit(user, "observation.add", "MetricObservation", obs.id,
           f"{metric.action.code}/{metric.name} 录入 {value:g}"
           f"（样本 {sample_count}，批次 {batch_no or '-'}）")
    return obs


# ---------------------------------------------------------------- 定时评估

@transaction.atomic
def run_evaluation(*, action: CorrectiveAction, now=None,
                   actor=None) -> EffectivenessEvaluation | None:
    """
    定时任务入口：对当前判定版本执行引擎评估，并把结果落库。
    - EXTENDED：自动把窗口延长一个步长（数据不足绝不判成功）。
    - INEFFECTIVE：失败条件触发，记录结论但仍需质量签署关闭。
    其它状态只刷新 last_evaluated_at。
    """
    evaluation = action.current_evaluation
    if evaluation is None:
        return None
    now = now or timezone.now()
    result = engine.evaluate(evaluation, now=now)
    evaluation.last_evaluated_at = now

    if result.status == EffectivenessEvaluation.Status.EXTENDED and result.extended:
        evaluation.window_end = result.window_end
        evaluation.extension_count = result.extension_count
        evaluation.status = EffectivenessEvaluation.Status.EXTENDED
        evaluation.save(update_fields=[
            "window_end", "extension_count", "status", "last_evaluated_at"])
        _audit(actor, "evaluation.extend", "EffectivenessEvaluation",
               evaluation.id,
               f"{action.code} v{evaluation.version} 数据不足，"
               f"窗口延长至 {result.window_end:%Y-%m-%d}",
               {"reasons": result.reason_lines})
    elif result.status == EffectivenessEvaluation.Status.INEFFECTIVE:
        if evaluation.status != EffectivenessEvaluation.Status.INEFFECTIVE:
            evaluation.status = EffectivenessEvaluation.Status.INEFFECTIVE
            evaluation.result = EffectivenessEvaluation.Result.INEFFECTIVE
            evaluation.finalized_at = now
            evaluation.conclusion_note = "；".join(result.reason_lines)
            evaluation.save(update_fields=[
                "status", "result", "finalized_at", "conclusion_note",
                "last_evaluated_at"])
            _audit(actor, "evaluation.fail", "EffectivenessEvaluation",
                   evaluation.id,
                   f"{action.code} v{evaluation.version} 触发失败条件，判定无效",
                   {"reasons": result.reason_lines})
        else:
            evaluation.save(update_fields=["last_evaluated_at"])
    elif result.status == EffectivenessEvaluation.Status.READY:
        if evaluation.status != EffectivenessEvaluation.Status.READY:
            evaluation.status = EffectivenessEvaluation.Status.READY
            evaluation.save(update_fields=["status", "last_evaluated_at"])
            _audit(actor, "evaluation.ready", "EffectivenessEvaluation",
                   evaluation.id,
                   f"{action.code} v{evaluation.version} 达标，可提交评审")
        else:
            evaluation.save(update_fields=["last_evaluated_at"])
    elif result.status == EffectivenessEvaluation.Status.DATA_INSUFFICIENT:
        if evaluation.status != EffectivenessEvaluation.Status.DATA_INSUFFICIENT:
            evaluation.status = EffectivenessEvaluation.Status.DATA_INSUFFICIENT
            evaluation.save(update_fields=["status", "last_evaluated_at"])
            _audit(actor, "evaluation.insufficient", "EffectivenessEvaluation",
                   evaluation.id,
                   f"{action.code} v{evaluation.version} 达延长上限仍数据不足，"
                   "待质量角色裁决",
                   {"reasons": result.reason_lines})
        else:
            evaluation.save(update_fields=["last_evaluated_at"])
    else:
        update = ["last_evaluated_at"]
        # 延长后的新窗口内继续观察，状态由 EXTENDED 回到 OBSERVING
        if (
            evaluation.status == EffectivenessEvaluation.Status.EXTENDED
            and result.status == EffectivenessEvaluation.Status.OBSERVING
        ):
            evaluation.status = EffectivenessEvaluation.Status.OBSERVING
            update.append("status")
        evaluation.save(update_fields=update)

    return evaluation


def run_due_evaluations(now=None):
    """供定时任务/管理命令调用：评估所有有当前版本的已完成措施。"""
    actions = CorrectiveAction.objects.filter(
        status=CorrectiveAction.Status.COMPLETED
    ).select_related("baseline")
    touched = []
    for action in actions:
        ev = run_evaluation(action=action, now=now)
        if ev:
            touched.append(ev)
    return touched


# ---------------------------------------------------------------- 评审与签署

@transaction.atomic
def submit_for_review(*, action: CorrectiveAction, user, note: str = ""):
    """提交评审：只有引擎判定 READY 才允许；责任人可以提交，质量角色签署。"""
    evaluation = action.current_evaluation
    if evaluation is None:
        raise ValidationError("尚无判定版本")
    result = engine.evaluate(evaluation)
    if result.status != EffectivenessEvaluation.Status.READY:
        raise ValidationError(
            f"当前状态为「{result.status_display}」，不满足提交评审条件："
            + "；".join(result.reason_lines)
        )
    evaluation.status = EffectivenessEvaluation.Status.IN_REVIEW
    evaluation.submitted_by = user
    evaluation.submitted_at = timezone.now()
    if note:
        evaluation.conclusion_note = note
    evaluation.save()
    _audit(user, "evaluation.submit", "EffectivenessEvaluation", evaluation.id,
           f"{action.code} v{evaluation.version} 提交有效性评审")
    return evaluation


def add_conflict_opinion(*, evaluation: EffectivenessEvaluation, user,
                         stance: str, content: str) -> ConflictOpinion:
    if not content.strip():
        raise ValidationError("意见内容不能为空")
    opinion = ConflictOpinion.objects.create(
        evaluation=evaluation, author=user, stance=stance, content=content
    )
    _audit(user, "opinion.add", "ConflictOpinion", opinion.id,
           f"{evaluation.action.code} v{evaluation.version} 新增意见：{stance}")
    return opinion


@transaction.atomic
def sign_off(*, evaluation: EffectivenessEvaluation, user, decision: str,
             reason: str = "") -> SignOff:
    """质量角色签署：批准关闭或退回。关闭决定必须由质量角色签署。"""
    if not is_quality(user):
        raise PermissionDenied("关闭决定必须由质量角色签署")
    if evaluation.status not in (
        EffectivenessEvaluation.Status.IN_REVIEW,
        EffectivenessEvaluation.Status.INEFFECTIVE,
        EffectivenessEvaluation.Status.DATA_INSUFFICIENT,
        EffectivenessEvaluation.Status.READY,
    ):
        raise ValidationError(
            f"判定当前状态（{evaluation.get_status_display()}）不允许签署"
        )

    prev = evaluation.signoffs.filter(is_active=True).order_by(
        "-signoff_version").first()
    signoff = SignOff.objects.create(
        evaluation=evaluation,
        signer=user,
        decision=decision,
        reason=reason,
        signoff_version=(prev.signoff_version + 1) if prev else 1,
        supersedes=prev,
    )
    if prev:
        SignOff.objects.filter(pk=prev.pk).update(is_active=False)

    if decision == SignOff.Decision.APPROVE:
        # 签署前必须按最新数据复核：提交后若出现失败数据或样本被纠正，
        # 即使状态仍为“评审中”也不允许批准为有效。
        result = engine.evaluate(evaluation, force_recheck=True)
        if result.status != EffectivenessEvaluation.Status.READY:
            raise ValidationError(
                f"最新数据不满足有效条件（{result.status_display}），"
                "不能批准关闭：" + "；".join(result.reason_lines)
            )
        evaluation.status = EffectivenessEvaluation.Status.EFFECTIVE
        evaluation.result = EffectivenessEvaluation.Result.EFFECTIVE
        evaluation.finalized_at = timezone.now()
        if reason:
            evaluation.conclusion_note = (
                evaluation.conclusion_note + f"｜签署理由：{reason}"
            ).strip("｜")
        evaluation.save()
    elif decision == SignOff.Decision.REJECT:
        evaluation.status = EffectivenessEvaluation.Status.INEFFECTIVE
        evaluation.result = EffectivenessEvaluation.Result.INEFFECTIVE
        evaluation.finalized_at = timezone.now()
        evaluation.conclusion_note = (
            f"质量退回：{reason}" or evaluation.conclusion_note
        )
        evaluation.save()
    _audit(user, "signoff.create", "SignOff", signoff.id,
           f"{evaluation.action.code} v{evaluation.version} 签署："
           f"{signoff.get_decision_display()}（{reason}）",
           {"decision": decision})
    return signoff


@transaction.atomic
def withdraw_signoff(*, evaluation: EffectivenessEvaluation, user,
                     reason: str) -> SignOff:
    """
    撤回签署：不删除任何历史记录，原签署标记失效，
    并生成一条带理由的 withdrawn 新版本签署；判定回到评审中。
    """
    if not is_quality(user):
        raise PermissionDenied("只有质量角色可以撤回签署")
    if not reason.strip():
        raise ValidationError("撤回签署必须填写理由")
    active = evaluation.signoffs.filter(is_active=True).first()
    if active is None:
        raise ValidationError("没有生效中的签署可撤回")

    SignOff.objects.filter(pk=active.pk).update(is_active=False)
    record = SignOff.objects.create(
        evaluation=evaluation,
        signer=user,
        decision=SignOff.Decision.WITHDRAWN,
        reason=reason,
        signoff_version=active.signoff_version + 1,
        supersedes=active,
        is_active=True,
    )
    evaluation.status = EffectivenessEvaluation.Status.IN_REVIEW
    evaluation.result = None
    evaluation.finalized_at = None
    evaluation.save(update_fields=["status", "result", "finalized_at"])
    _audit(user, "signoff.withdraw", "SignOff", record.id,
           f"{evaluation.action.code} v{evaluation.version} 撤回签署，理由：{reason}",
           {"supersedes": active.id})
    return record


# ---------------------------------------------------------------- 相关偏差 / 重开

@transaction.atomic
def link_related_deviation(*, action: CorrectiveAction, deviation: Deviation,
                           user, relationship_note: str = "") -> EffectivenessEvaluation:
    """
    关联新发生的相关偏差：自动重开有效性判定，生成新版本。
    先前版本与结论保留（状态置为 REOPENED 归档，is_current=False），不删除。
    """
    if RelatedDeviation.objects.filter(
        action=action, deviation=deviation
    ).exists():
        raise ValidationError("该偏差已关联到此措施")
    if deviation.id == action.deviation_id:
        raise ValidationError("不能关联原始偏差，请新建复发/同类偏差")

    current = action.current_evaluation
    link = RelatedDeviation.objects.create(
        action=action, deviation=deviation,
        relationship_note=relationship_note, linked_by=user,
    )

    if current is None:
        raise ValidationError("措施尚无判定版本，无法重开")

    # 归档旧版本：保留其结论内容，仅切换状态与当前标记
    EffectivenessEvaluation.objects.filter(pk=current.pk).update(
        is_current=False,
        status=EffectivenessEvaluation.Status.REOPENED,
    )
    new_version = EffectivenessEvaluation.objects.create(
        action=action,
        version=current.version + 1,
        status=EffectivenessEvaluation.Status.OBSERVING,
        trigger_deviation=deviation,
        window_start=timezone.now(),
        window_end=timezone.now()
        + timedelta(days=action.baseline.window_days),
        conclusion_note=f"因相关偏差 {deviation.code} 重开：{relationship_note}",
    )
    RelatedDeviation.objects.filter(pk=link.pk).update(
        reopened_evaluation=new_version
    )
    if deviation.status == Deviation.Status.CLOSED:
        deviation.status = Deviation.Status.REOPENED
        deviation.save(update_fields=["status"])
    # CAPA 被证明不再可靠时，其原始偏差也必须重新打开
    original = action.deviation
    if original.status == Deviation.Status.CLOSED:
        original.status = Deviation.Status.REOPENED
        original.save(update_fields=["status"])
    _audit(user, "evaluation.reopen", "EffectivenessEvaluation", new_version.id,
           f"{action.code} 因相关偏差 {deviation.code} 重开，"
           f"v{current.version} 结论保留，新建 v{new_version.version}",
           {"previous_version": current.version,
            "trigger_deviation": deviation.code})
    return new_version
