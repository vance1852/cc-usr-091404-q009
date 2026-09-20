"""DRF 序列化器。"""
from django.contrib.auth.models import User
from rest_framework import serializers

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
    RootCauseHypothesis,
    SignOff,
    UserProfile,
)
from .roles import Role, is_quality


class UserProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = UserProfile
        fields = ["id", "username", "role", "department"]


class UserSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=Role.CHOICES, write_only=True)
    department = serializers.CharField(write_only=True, required=False,
                                       allow_blank=True)
    role_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "password", "role", "department",
                  "role_display", "is_superuser"]
        extra_kwargs = {"password": {"write_only": True, "required": True,
                                     "min_length": 6}}

    def get_role_display(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.get_role_display() if profile else None

    def create(self, validated_data):
        role = validated_data.pop("role")
        department = validated_data.pop("department", "")
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        UserProfile.objects.create(user=user, role=role, department=department)
        return user


class RootCauseHypothesisSerializer(serializers.ModelSerializer):
    raised_by_name = serializers.CharField(source="raised_by.username",
                                           read_only=True)

    class Meta:
        model = RootCauseHypothesis
        fields = ["id", "deviation", "category", "description",
                  "supporting_evidence", "is_confirmed", "raised_by",
                  "raised_by_name", "created_at"]
        read_only_fields = ["raised_by"]


class DeviationSerializer(serializers.ModelSerializer):
    hypotheses = RootCauseHypothesisSerializer(many=True, read_only=True)

    class Meta:
        model = Deviation
        fields = ["id", "code", "title", "description", "batch_no",
                  "severity", "status", "discovered_at", "closed_at",
                  "created_by", "created_at", "updated_at", "hypotheses"]
        read_only_fields = ["status", "closed_at", "created_by"]


class BaselineSerializer(serializers.ModelSerializer):
    approved_by_name = serializers.CharField(source="approved_by.username",
                                             read_only=True)

    class Meta:
        model = EffectivenessBaseline
        fields = ["id", "window_days", "min_sample_size", "max_extensions",
                  "approved_by", "approved_by_name", "approved_at", "note"]
        read_only_fields = ["approved_by", "approved_at"]


class MetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = Metric
        fields = ["id", "action", "name", "metric_type", "unit", "direction",
                  "baseline_value", "target_value", "fail_condition",
                  "fail_threshold", "allowed_breaches", "min_sample_size",
                  "is_locked"]
        read_only_fields = ["is_locked"]

    def validate(self, attrs):
        action = attrs.get("action") or getattr(self.instance, "action", None)
        if action and hasattr(action, "baseline"):
            raise serializers.ValidationError("基线已固化，指标定义不可变更")
        return attrs


class MetricObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MetricObservation
        fields = ["id", "metric", "batch_no", "observed_at", "value",
                  "sample_count", "note", "recorded_by", "created_at"]
        read_only_fields = ["recorded_by"]
        extra_kwargs = {"observed_at": {"required": False}}


class CompletionEvidenceSerializer(serializers.ModelSerializer):
    submitted_by_name = serializers.CharField(source="submitted_by.username",
                                              read_only=True)
    approved_by_name = serializers.CharField(source="approved_by.username",
                                             read_only=True)

    class Meta:
        model = CompletionEvidence
        fields = ["id", "action", "title", "description", "attachment_ref",
                  "submitted_by", "submitted_by_name", "submitted_at",
                  "approval_status", "approved_by", "approved_by_name",
                  "approved_at", "review_note"]
        read_only_fields = ["submitted_by", "approval_status", "approved_by",
                            "approved_at", "review_note"]


class SignOffSerializer(serializers.ModelSerializer):
    signer_name = serializers.CharField(source="signer.username", read_only=True)

    class Meta:
        model = SignOff
        fields = ["id", "signer", "signer_name", "decision", "reason",
                  "signed_at", "is_active", "signoff_version", "supersedes"]
        read_only_fields = ["signer", "signed_at", "is_active",
                            "signoff_version", "supersedes"]


class ConflictOpinionSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.username", read_only=True)

    class Meta:
        model = ConflictOpinion
        fields = ["id", "evaluation", "author", "author_name", "stance",
                  "content", "created_at"]
        read_only_fields = ["author"]


class EvaluationListSerializer(serializers.ModelSerializer):
    action_code = serializers.CharField(source="action.code", read_only=True)

    class Meta:
        model = EffectivenessEvaluation
        fields = ["id", "action", "action_code", "version", "status",
                  "result", "window_start", "window_end", "extension_count",
                  "is_current", "finalized_at", "last_evaluated_at",
                  "created_at"]


class RelatedDeviationSerializer(serializers.ModelSerializer):
    linked_by_name = serializers.CharField(source="linked_by.username",
                                           read_only=True)
    deviation_code = serializers.CharField(source="deviation.code",
                                           read_only=True)

    class Meta:
        model = RelatedDeviation
        fields = ["id", "action", "deviation", "deviation_code",
                  "relationship_note", "linked_by", "linked_by_name",
                  "linked_at", "reopened_evaluation"]
        read_only_fields = ["linked_by", "reopened_evaluation"]


class CorrectiveActionSerializer(serializers.ModelSerializer):
    responsible_name = serializers.CharField(source="responsible_user.username",
                                             read_only=True)
    baseline = BaselineSerializer(read_only=True)
    metrics = MetricSerializer(many=True, read_only=True)
    current_evaluation = serializers.SerializerMethodField()
    evidence_coverage = serializers.SerializerMethodField()

    class Meta:
        model = CorrectiveAction
        fields = ["id", "code", "deviation", "title", "description", "kind",
                  "responsible_user", "responsible_name", "due_date", "status",
                  "completed_at", "created_by", "created_at", "updated_at",
                  "baseline", "metrics", "current_evaluation",
                  "evidence_coverage"]
        read_only_fields = ["status", "completed_at", "created_by"]

    def get_current_evaluation(self, obj):
        ev = obj.current_evaluation
        return EvaluationListSerializer(ev).data if ev else None

    def get_evidence_coverage(self, obj):
        total = obj.evidences.count()
        approved = obj.evidences.filter(
            approval_status=CompletionEvidence.ApprovalStatus.APPROVED).count()
        rejected = obj.evidences.filter(
            approval_status=CompletionEvidence.ApprovalStatus.REJECTED).count()
        return {"total": total, "approved": approved, "rejected": rejected,
                "pending": total - approved - rejected}


class MetricSpecSerializer(serializers.Serializer):
    """基线固化时嵌套的指标定义入参（action 由外层注入）。"""

    name = serializers.CharField(max_length=120)
    metric_type = serializers.ChoiceField(
        choices=Metric.MetricType.choices, default=Metric.MetricType.MEAN)
    unit = serializers.CharField(required=False, allow_blank=True, default="")
    direction = serializers.ChoiceField(
        choices=Metric.Direction.choices, default=Metric.Direction.DECREASE)
    baseline_value = serializers.FloatField()
    target_value = serializers.FloatField()
    fail_condition = serializers.ChoiceField(
        choices=Metric.FailCondition.choices,
        default=Metric.FailCondition.AGGREGATE_THRESHOLD)
    fail_threshold = serializers.FloatField(required=False, allow_null=True,
                                            default=None)
    allowed_breaches = serializers.IntegerField(min_value=0, default=0)
    min_sample_size = serializers.IntegerField(
        min_value=1, required=False, allow_null=True, default=None)


class BaselineFreezeSerializer(serializers.Serializer):
    """措施批准时固化基线与指标的入参。"""

    window_days = serializers.IntegerField(min_value=1)
    min_sample_size = serializers.IntegerField(min_value=1)
    max_extensions = serializers.IntegerField(min_value=0, default=3)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    metrics = MetricSpecSerializer(many=True, required=False)

    def validate_metrics(self, value):
        names = [m["name"] for m in value]
        if len(names) != len(set(names)):
            raise serializers.ValidationError("指标名称不能重复")
        return value


class EvaluationActionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class SignOffCreateSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=SignOff.Decision.choices)
    reason = serializers.CharField(required=False, allow_blank=True,
                                   default="")


class WithdrawSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False)


class OpinionCreateSerializer(serializers.Serializer):
    stance = serializers.ChoiceField(choices=ConflictOpinion.Stance.choices)
    content = serializers.CharField(required=True, allow_blank=False)


class EvidenceReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_note = serializers.CharField(required=False, allow_blank=True,
                                        default="")


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ["id", "actor", "actor_name", "action", "entity_type",
                  "entity_id", "summary", "changes", "created_at"]


class RelatedDeviationCreateSerializer(serializers.Serializer):
    deviation_id = serializers.IntegerField()
    relationship_note = serializers.CharField(required=False, allow_blank=True,
                                              default="")

    def validate_deviation_id(self, value):
        if not Deviation.objects.filter(pk=value).exists():
            raise serializers.ValidationError("偏差不存在")
        return value


class IsQualityMixin:
    def check_quality(self, user):
        if not is_quality(user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("该操作需要质量角色")
