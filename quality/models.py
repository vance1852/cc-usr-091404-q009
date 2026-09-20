"""
领域模型：偏差、根因假设、CAPA 措施、固化基线、观察指标、
有效性判定（版本化）、完成证据、相关偏差、冲突意见、质量签署与审计日志。
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from .roles import Role


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    role = models.CharField(max_length=20, choices=Role.CHOICES)
    department = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name = "用户角色"

    def __str__(self):
        return f"{self.user.username}({self.get_role_display()})"


class Deviation(models.Model):
    """灌装量等生产偏差。"""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        OPEN = "open", "已发起"
        UNDER_INVESTIGATION = "investigating", "调查中"
        CLOSED = "closed", "已关闭"
        REOPENED = "reopened", "重新打开"

    class Severity(models.TextChoices):
        MINOR = "minor", "一般"
        MAJOR = "major", "主要"
        CRITICAL = "critical", "严重"

    code = models.CharField(max_length=40, unique=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    batch_no = models.CharField(max_length=60, blank=True)
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.MAJOR
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    discovered_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="raised_deviations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "偏差"
        ordering = ["-discovered_at"]

    def __str__(self):
        return f"{self.code} {self.title}"


class RootCauseHypothesis(models.Model):
    """根因假设（5M1E 分类），可标记是否经证据确认。"""

    class Category(models.TextChoices):
        MAN = "man", "人员"
        MACHINE = "machine", "设备"
        MATERIAL = "material", "物料"
        METHOD = "method", "方法"
        ENVIRONMENT = "environment", "环境"
        MEASUREMENT = "measurement", "测量"

    deviation = models.ForeignKey(
        Deviation, on_delete=models.CASCADE, related_name="hypotheses"
    )
    category = models.CharField(max_length=15, choices=Category.choices)
    description = models.TextField()
    supporting_evidence = models.TextField(blank=True)
    is_confirmed = models.BooleanField(default=False)
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="raised_hypotheses",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "根因假设"
        ordering = ["-is_confirmed", "created_at"]


class CorrectiveAction(models.Model):
    """纠正/预防措施（CAPA）。"""

    class Kind(models.TextChoices):
        CORRECTIVE = "corrective", "纠正措施"
        PREVENTIVE = "preventive", "预防措施"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        IN_PROGRESS = "in_progress", "执行中"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"

    code = models.CharField(max_length=40, unique=True)
    deviation = models.ForeignKey(
        Deviation, on_delete=models.CASCADE, related_name="actions"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    kind = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.CORRECTIVE
    )
    # 责任分离：责任人与批准人/签署人不能是同一人
    responsible_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="responsible_actions",
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.DRAFT
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_actions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "纠正预防措施"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.title}"

    @property
    def current_evaluation(self):
        return self.evaluations.filter(is_current=True).first()


class EffectivenessBaseline(models.Model):
    """措施批准时固化的量化判定基线：窗口、最小样本量与失败条件参数。"""

    action = models.OneToOneField(
        CorrectiveAction, on_delete=models.PROTECT, related_name="baseline"
    )
    window_days = models.PositiveIntegerField()
    min_sample_size = models.PositiveIntegerField()
    max_extensions = models.PositiveIntegerField(default=3)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_baselines",
    )
    approved_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "有效性判定基线"


class Metric(models.Model):
    """观察指标定义；基线固化后不可修改。"""

    class MetricType(models.TextChoices):
        MEAN = "mean", "均值"
        RATE = "rate", "比率"
        COUNT = "count", "计数"

    class Direction(models.TextChoices):
        DECREASE = "decrease", "越低越好"
        INCREASE = "increase", "越高越好"

    class FailCondition(models.TextChoices):
        NONE = "none", "无聚合失败条件"
        AGGREGATE_THRESHOLD = "aggregate_threshold", "聚合值越过阈值"
        INDIVIDUAL_BREACH = "individual_breach", "单点越限"

    action = models.ForeignKey(
        CorrectiveAction, on_delete=models.CASCADE, related_name="metrics"
    )
    name = models.CharField(max_length=120)
    metric_type = models.CharField(
        max_length=10, choices=MetricType.choices, default=MetricType.MEAN
    )
    unit = models.CharField(max_length=30, blank=True)
    direction = models.CharField(
        max_length=10, choices=Direction.choices, default=Direction.DECREASE
    )
    baseline_value = models.FloatField()
    target_value = models.FloatField()
    # 失败条件（批准时固化）
    fail_condition = models.CharField(
        max_length=20,
        choices=FailCondition.choices,
        default=FailCondition.AGGREGATE_THRESHOLD,
    )
    fail_threshold = models.FloatField(null=True, blank=True)
    allowed_breaches = models.PositiveIntegerField(default=0)
    min_sample_size = models.PositiveIntegerField(
        null=True, blank=True, help_text="为空时使用基线全局最小样本量"
    )
    is_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "观察指标"
        ordering = ["id"]

    @property
    def effective_min_sample(self):
        if self.min_sample_size is not None:
            return self.min_sample_size
        return self.action.baseline.min_sample_size


class MetricObservation(models.Model):
    """窗口内采集的指标数据点。"""

    metric = models.ForeignKey(
        Metric, on_delete=models.CASCADE, related_name="observations"
    )
    batch_no = models.CharField(max_length=60, blank=True)
    observed_at = models.DateTimeField()
    value = models.FloatField()
    sample_count = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=300, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_observations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "指标观察数据"
        ordering = ["observed_at"]


class CompletionEvidence(models.Model):
    """措施完成证据；责任人不能批准自己提交的证据。"""

    class ApprovalStatus(models.TextChoices):
        PENDING = "pending", "待批准"
        APPROVED = "approved", "已批准"
        REJECTED = "rejected", "已拒绝"

    action = models.ForeignKey(
        CorrectiveAction, on_delete=models.CASCADE, related_name="evidences"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    attachment_ref = models.CharField(max_length=300, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_evidences",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    approval_status = models.CharField(
        max_length=10, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_evidences",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "完成证据"
        ordering = ["-submitted_at"]


class EffectivenessEvaluation(models.Model):
    """
    CAPA 有效性判定版本。每次相关偏差重开都会新增版本，
    历史版本及其结论永久保留、不可删除或改写。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待观察（措施未完成）"
        OBSERVING = "observing", "观察中"
        EXTENDED = "extended", "观察已延长（数据不足）"
        DATA_INSUFFICIENT = "data_insufficient", "数据不足（达延长上限）"
        READY = "ready", "可提交评审"
        IN_REVIEW = "in_review", "评审中"
        EFFECTIVE = "effective", "判定有效"
        INEFFECTIVE = "ineffective", "判定无效"
        CLOSED = "closed", "已关闭"
        REOPENED = "reopened", "因相关偏差重开"

    class Result(models.TextChoices):
        EFFECTIVE = "effective", "有效"
        INEFFECTIVE = "ineffective", "无效"

    action = models.ForeignKey(
        CorrectiveAction, on_delete=models.CASCADE, related_name="evaluations"
    )
    version = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    result = models.CharField(
        max_length=12, choices=Result.choices, null=True, blank=True
    )
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    extension_count = models.PositiveIntegerField(default=0)
    is_current = models.BooleanField(default=True)
    conclusion_note = models.CharField(max_length=1000, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_evaluations",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    # 触发本版本重开的相关偏差
    trigger_deviation = models.ForeignKey(
        Deviation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="triggered_evaluations",
    )
    last_evaluated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "有效性判定版本"
        unique_together = ("action", "version")
        ordering = ["action", "-version"]

    def __str__(self):
        return f"{self.action.code} 判定v{self.version}({self.get_status_display()})"


class RelatedDeviation(models.Model):
    """关联到措施的新发生偏差（复发/同类问题）。"""

    action = models.ForeignKey(
        CorrectiveAction, on_delete=models.CASCADE, related_name="related_deviations"
    )
    deviation = models.ForeignKey(
        Deviation, on_delete=models.PROTECT, related_name="related_to_actions"
    )
    relationship_note = models.CharField(max_length=500, blank=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="linked_related_deviations",
    )
    linked_at = models.DateTimeField(default=timezone.now)
    reopened_evaluation = models.ForeignKey(
        EffectivenessEvaluation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reopen_links",
    )

    class Meta:
        verbose_name = "相关偏差关联"
        unique_together = ("action", "deviation")


class ConflictOpinion(models.Model):
    """评审中的冲突/不同意见。"""

    class Stance(models.TextChoices):
        SUPPORTS = "supports_effective", "支持有效"
        CHALLENGES = "challenges", "质疑有效性"
        ABSTAIN = "abstain", "中立/保留"

    evaluation = models.ForeignKey(
        EffectivenessEvaluation, on_delete=models.CASCADE, related_name="opinions"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="opinions"
    )
    stance = models.CharField(max_length=20, choices=Stance.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "冲突意见"
        ordering = ["-created_at"]


class SignOff(models.Model):
    """质量角色对有效性判定的签署；撤回不删除记录，而生成带理由的新版本。"""

    class Decision(models.TextChoices):
        APPROVE = "approve_close", "批准有效/关闭"
        REJECT = "reject", "退回（无效）"
        WITHDRAWN = "withdrawn", "撤回前序签署"

    evaluation = models.ForeignKey(
        EffectivenessEvaluation, on_delete=models.CASCADE, related_name="signoffs"
    )
    signer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="signoffs"
    )
    decision = models.CharField(max_length=15, choices=Decision.choices)
    reason = models.TextField(blank=True)
    signed_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    signoff_version = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="superseded_by",
    )

    class Meta:
        verbose_name = "质量签署"
        ordering = ["-signoff_version", "-signed_at"]


class AuditLog(models.Model):
    """不可变审计记录。"""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=60)
    entity_type = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=40)
    summary = models.CharField(max_length=500)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "审计日志"
        ordering = ["-created_at"]
