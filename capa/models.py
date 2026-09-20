"""
偏差与 CAPA 有效性判定领域模型。

设计要点:
- EffectivenessPlan 在措施批准时固化基线 / 目标 / 观察窗口 / 失败条件,
  固化后禁止通过 API 修改; 数据不足时的观察期延长只能由受治理的服务函数执行
  并留下审计记录。
- EffectivenessEvaluation 以版本化方式保存每次判定结论, 历史结论永不删除,
  仅通过 is_current 标记当前有效版本。
- SignOff 同样版本化, 撤回签署会产生一条带理由的新版本记录。
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class Deviation(models.Model):
    """偏差记录。可通过 linked_action 关联到既有 CAPA 措施(后续/复发偏差)。"""

    class Status(models.TextChoices):
        OPEN = "open", "未处理"
        INVESTIGATING = "investigating", "调查中"
        CAPA_IN_PROGRESS = "capa_in_progress", "CAPA 执行中"
        MONITORING = "monitoring", "效果观察中"
        CLOSED = "closed", "已关闭"

    class Severity(models.TextChoices):
        MINOR = "minor", "轻微"
        MAJOR = "major", "主要"
        CRITICAL = "critical", "严重"

    code = models.CharField("偏差编号", max_length=32, unique=True)
    title = models.CharField("标题", max_length=200)
    description = models.TextField("描述", blank=True)
    severity = models.CharField(
        "严重程度", max_length=16, choices=Severity.choices, default=Severity.MINOR
    )
    status = models.CharField(
        "状态", max_length=32, choices=Status.choices, default=Status.OPEN
    )
    occurred_at = models.DateField("发生日期", default=timezone.localdate)
    linked_action = models.ForeignKey(
        "CAPAAction",
        verbose_name="关联措施",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="followup_deviations",
        help_text="若该偏差是某措施观察期内的复发/相关偏差, 关联后将自动重开判定",
    )
    created_by = models.ForeignKey(
        User, verbose_name="创建人", null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} {self.title}"


class RootCauseHypothesis(models.Model):
    """根因假设。"""

    class Status(models.TextChoices):
        PROPOSED = "proposed", "已提出"
        CONFIRMED = "confirmed", "已确认"
        REJECTED = "rejected", "已排除"

    deviation = models.ForeignKey(
        Deviation, verbose_name="偏差", on_delete=models.CASCADE, related_name="hypotheses"
    )
    statement = models.TextField("假设内容")
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PROPOSED
    )
    created_by = models.ForeignKey(
        User, verbose_name="提出人", null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"假设#{self.pk} ({self.get_status_display()})"


class CAPAAction(models.Model):
    """纠正预防措施。"""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_APPROVAL = "pending_approval", "待批准"
        APPROVED = "approved", "已批准(执行中)"
        MONITORING = "monitoring", "措施完成·观察中"
        PENDING_REVIEW = "pending_review", "待有效性评审"
        CLOSED_EFFECTIVE = "closed_effective", "已关闭·有效"
        CLOSED_INEFFECTIVE = "closed_ineffective", "已关闭·无效"
        REOPENED = "reopened", "已重开"

    code = models.CharField("措施编号", max_length=32, unique=True)
    deviation = models.ForeignKey(
        Deviation, verbose_name="来源偏差", on_delete=models.CASCADE, related_name="capa_actions"
    )
    hypothesis = models.ForeignKey(
        RootCauseHypothesis,
        verbose_name="针对的根因假设",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="actions",
    )
    title = models.CharField("标题", max_length=200)
    description = models.TextField("措施内容", blank=True)
    owner = models.ForeignKey(
        User, verbose_name="责任人", on_delete=models.PROTECT, related_name="owned_actions"
    )
    status = models.CharField(
        "状态", max_length=32, choices=Status.choices, default=Status.DRAFT
    )
    due_date = models.DateField("要求完成日期", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} {self.title}"

    @property
    def current_plan(self):
        return self.plans.filter(is_current=True).first()

    @property
    def current_evaluation(self):
        return self.evaluations.filter(is_current=True).first()


class EffectivenessPlan(models.Model):
    """
    有效性判定计划: 批准时固化基线、目标、观察窗口、最小样本量与失败条件。

    is_frozen=True 后, 业务层禁止任何直接修改; 唯一例外是
    services.extend_observation() 在数据不足时对 observation_end 的
    受治理延长(每次延长均产生审计与判定记录)。
    """

    action = models.ForeignKey(
        CAPAAction, verbose_name="措施", on_delete=models.CASCADE, related_name="plans"
    )
    version = models.PositiveIntegerField("版本", default=1)
    observation_start = models.DateField("观察开始日")
    observation_end = models.DateField("观察截止日")
    min_sample_size = models.PositiveIntegerField("最小样本量", default=3)
    extension_count = models.PositiveIntegerField("已延长次数", default=0)
    is_frozen = models.BooleanField("已固化", default=False)
    is_current = models.BooleanField("当前版本", default=True)
    change_reason = models.CharField("版本说明", max_length=255, blank=True)
    approved_by = models.ForeignKey(
        User, verbose_name="批准人", null=True, blank=True, on_delete=models.SET_NULL
    )
    approved_at = models.DateTimeField("批准时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["action", "version"], name="uniq_plan_version_per_action"
            )
        ]
        ordering = ["-version"]

    def __str__(self):
        return f"计划 v{self.version} ({self.action.code})"


class ObservationMetric(models.Model):
    """观察指标: 基线与目标值随计划一同固化。"""

    class Direction(models.TextChoices):
        INCREASE = "increase", "越高越好"
        DECREASE = "decrease", "越低越好"

    plan = models.ForeignKey(
        EffectivenessPlan, verbose_name="所属计划", on_delete=models.CASCADE, related_name="metrics"
    )
    name = models.CharField("指标名", max_length=100)
    unit = models.CharField("单位", max_length=32, blank=True)
    baseline = models.DecimalField("基线值", max_digits=14, decimal_places=4)
    target = models.DecimalField("目标值", max_digits=14, decimal_places=4)
    direction = models.CharField("改善方向", max_length=16, choices=Direction.choices)

    def __str__(self):
        return f"{self.name} (目标 {self.target}{self.unit})"


class FailureCondition(models.Model):
    """失败条件: 观察窗口内任一样本触发即判定措施未达效。"""

    class Operator(models.TextChoices):
        GT = "gt", ">"
        GTE = "gte", ">="
        LT = "lt", "<"
        LTE = "lte", "<="

    plan = models.ForeignKey(
        EffectivenessPlan,
        verbose_name="所属计划",
        on_delete=models.CASCADE,
        related_name="failure_conditions",
    )
    metric = models.ForeignKey(
        ObservationMetric,
        verbose_name="指标",
        on_delete=models.CASCADE,
        related_name="failure_conditions",
    )
    operator = models.CharField("比较符", max_length=8, choices=Operator.choices)
    threshold = models.DecimalField("阈值", max_digits=14, decimal_places=4)
    description = models.CharField("条件说明", max_length=255)

    def is_triggered_by(self, value) -> bool:
        """value 触发该失败条件时返回 True。"""
        ops = {
            "gt": lambda v: v > self.threshold,
            "gte": lambda v: v >= self.threshold,
            "lt": lambda v: v < self.threshold,
            "lte": lambda v: v <= self.threshold,
        }
        return ops[self.operator](value)

    def __str__(self):
        return self.description


class MetricReading(models.Model):
    """指标读数(按批次采集)。"""

    metric = models.ForeignKey(
        ObservationMetric, verbose_name="指标", on_delete=models.CASCADE, related_name="readings"
    )
    batch_no = models.CharField("批号", max_length=64)
    value = models.DecimalField("读数", max_digits=14, decimal_places=4)
    recorded_at = models.DateTimeField("采集时间", default=timezone.now)
    recorded_by = models.ForeignKey(
        User, verbose_name="记录人", null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["recorded_at"]

    def __str__(self):
        return f"{self.metric.name} {self.batch_no}={self.value}"


class Evidence(models.Model):
    """措施完成证据。提交人与审核人必须分离。"""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "待审核"
        APPROVED = "approved", "已批准"
        REJECTED = "rejected", "已驳回"

    action = models.ForeignKey(
        CAPAAction, verbose_name="措施", on_delete=models.CASCADE, related_name="evidence"
    )
    title = models.CharField("标题", max_length=200)
    description = models.TextField("说明", blank=True)
    reference = models.CharField("文件/记录引用", max_length=255, blank=True)
    submitted_by = models.ForeignKey(
        User, verbose_name="提交人", on_delete=models.PROTECT, related_name="submitted_evidence"
    )
    submitted_at = models.DateTimeField("提交时间", auto_now_add=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.SUBMITTED
    )
    reviewed_by = models.ForeignKey(
        User,
        verbose_name="审核人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_evidence",
    )
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    review_comment = models.TextField("审核意见", blank=True)

    def __str__(self):
        return f"证据#{self.pk} {self.title} ({self.get_status_display()})"


class EffectivenessEvaluation(models.Model):
    """
    有效性判定结论(版本化, 历史版本只读保留, 永不删除)。

    rationale 为可解释的理由列表, 供评审人理解决定依据。
    """

    class Result(models.TextChoices):
        EFFECTIVE = "effective", "有效"
        INEFFECTIVE = "ineffective", "无效"
        INCONCLUSIVE = "inconclusive", "数据不足·继续观察"
        EXTENDED = "extended", "数据不足·已延长观察"
        REOPENED = "reopened", "因新偏差重开"

    action = models.ForeignKey(
        CAPAAction, verbose_name="措施", on_delete=models.CASCADE, related_name="evaluations"
    )
    version = models.PositiveIntegerField("版本")
    result = models.CharField("结论", max_length=16, choices=Result.choices)
    rationale = models.JSONField("判定理由", default=list)
    metrics_summary = models.JSONField("指标摘要", default=dict)
    sample_size = models.PositiveIntegerField("窗口内样本量", default=0)
    eligible_for_review = models.BooleanField("可提交评审", default=False)
    created_by = models.ForeignKey(
        User,
        verbose_name="判定人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="为空表示系统定时评估产生",
    )
    created_at = models.DateTimeField("判定时间", auto_now_add=True)
    is_current = models.BooleanField("当前版本", default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["action", "version"], name="uniq_evaluation_version_per_action"
            )
        ]
        ordering = ["-version"]

    def __str__(self):
        return f"判定 v{self.version} {self.action.code}: {self.get_result_display()}"


class ReviewOpinion(models.Model):
    """评审意见(允许与系统建议冲突, 冲突意见会展示在负责人看板)。"""

    class Stance(models.TextChoices):
        AGREE = "agree", "同意"
        DISAGREE = "disagree", "反对"

    evaluation = models.ForeignKey(
        EffectivenessEvaluation,
        verbose_name="判定",
        on_delete=models.CASCADE,
        related_name="opinions",
    )
    user = models.ForeignKey(User, verbose_name="评审人", on_delete=models.CASCADE)
    stance = models.CharField("立场", max_length=16, choices=Stance.choices)
    comment = models.TextField("意见", blank=True)
    created_at = models.DateTimeField("发表时间", auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evaluation", "user"], name="uniq_opinion_per_user_per_evaluation"
            )
        ]


class SignOff(models.Model):
    """
    关闭签署(版本化)。

    撤回签署时: 原记录置为 withdrawn 并记录理由, 同时生成一条
    decision=withdraw 的新版本记录; 历史版本全部保留。
    """

    class Decision(models.TextChoices):
        CLOSE_EFFECTIVE = "close_effective", "签署关闭·有效"
        CLOSE_INEFFECTIVE = "close_ineffective", "签署关闭·无效"
        WITHDRAW = "withdraw", "撤回签署"

    class State(models.TextChoices):
        ACTIVE = "active", "生效中"
        WITHDRAWN = "withdrawn", "已撤回"

    action = models.ForeignKey(
        CAPAAction, verbose_name="措施", on_delete=models.CASCADE, related_name="signoffs"
    )
    evaluation = models.ForeignKey(
        EffectivenessEvaluation,
        verbose_name="依据的判定",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="signoffs",
    )
    version = models.PositiveIntegerField("版本", default=1)
    decision = models.CharField("决定", max_length=32, choices=Decision.choices)
    comment = models.TextField("签署意见", blank=True)
    user = models.ForeignKey(User, verbose_name="签署人", on_delete=models.PROTECT)
    signed_at = models.DateTimeField("签署时间", auto_now_add=True)
    state = models.CharField(
        "状态", max_length=16, choices=State.choices, default=State.ACTIVE
    )
    withdrawn_reason = models.TextField("撤回理由", blank=True)
    withdrawn_at = models.DateTimeField("撤回时间", null=True, blank=True)
    supersedes = models.ForeignKey(
        "self",
        verbose_name="取代的签署",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="successors",
    )

    class Meta:
        ordering = ["-version"]

    def __str__(self):
        return f"签署 v{self.version} {self.action.code}: {self.get_decision_display()}"


class AuditLog(models.Model):
    """审计日志: 所有关键状态变更均留痕。"""

    timestamp = models.DateTimeField("时间", auto_now_add=True)
    actor = models.ForeignKey(
        User,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="为空表示系统行为",
    )
    action = models.CharField("操作", max_length=64)
    entity_type = models.CharField("对象类型", max_length=64)
    entity_id = models.CharField("对象ID", max_length=64)
    summary = models.CharField("摘要", max_length=255, blank=True)
    detail = models.JSONField("明细", default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        who = self.actor_id or "system"
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {who} {self.action} {self.entity_type}#{self.entity_id}"
