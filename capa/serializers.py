from rest_framework import serializers

from .models import (
    AuditLog,
    CAPAAction,
    Deviation,
    EffectivenessEvaluation,
    EffectivenessPlan,
    Evidence,
    FailureCondition,
    MetricReading,
    ObservationMetric,
    ReviewOpinion,
    RootCauseHypothesis,
    SignOff,
)


class DeviationSerializer(serializers.ModelSerializer):
    created_by = serializers.ReadOnlyField(source="created_by.username")

    class Meta:
        model = Deviation
        fields = [
            "id", "code", "title", "description", "severity", "status",
            "occurred_at", "linked_action", "created_by", "created_at",
        ]
        read_only_fields = ["status", "created_at"]


class RootCauseHypothesisSerializer(serializers.ModelSerializer):
    created_by = serializers.ReadOnlyField(source="created_by.username")

    class Meta:
        model = RootCauseHypothesis
        fields = ["id", "deviation", "statement", "status", "created_by", "created_at"]
        read_only_fields = ["created_at"]


class FailureConditionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FailureCondition
        fields = ["id", "metric", "operator", "threshold", "description"]


class ObservationMetricSerializer(serializers.ModelSerializer):
    failure_conditions = FailureConditionSerializer(many=True, read_only=True)

    class Meta:
        model = ObservationMetric
        fields = [
            "id", "plan", "name", "unit", "baseline", "target",
            "direction", "failure_conditions",
        ]


class EffectivenessPlanSerializer(serializers.ModelSerializer):
    metrics = ObservationMetricSerializer(many=True, read_only=True)
    failure_conditions = FailureConditionSerializer(many=True, read_only=True)
    approved_by = serializers.ReadOnlyField(source="approved_by.username")

    class Meta:
        model = EffectivenessPlan
        fields = [
            "id", "action", "version", "observation_start", "observation_end",
            "min_sample_size", "extension_count", "is_frozen", "is_current",
            "change_reason", "approved_by", "approved_at", "metrics",
            "failure_conditions",
        ]
        read_only_fields = ["is_frozen", "is_current", "extension_count",
                            "approved_by", "approved_at", "version"]

    def validate(self, attrs):
        start = attrs.get("observation_start", getattr(self.instance, "observation_start", None))
        end = attrs.get("observation_end", getattr(self.instance, "observation_end", None))
        if start and end and end <= start:
            raise serializers.ValidationError("观察截止日必须晚于观察开始日")
        return attrs


class CAPAActionSerializer(serializers.ModelSerializer):
    owner = serializers.ReadOnlyField(source="owner.username")
    current_plan = EffectivenessPlanSerializer(read_only=True)
    status_display = serializers.ReadOnlyField(source="get_status_display")

    class Meta:
        model = CAPAAction
        fields = [
            "id", "code", "deviation", "hypothesis", "title", "description",
            "owner", "status", "status_display", "due_date",
            "current_plan", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class EvidenceSerializer(serializers.ModelSerializer):
    submitted_by = serializers.ReadOnlyField(source="submitted_by.username")
    reviewed_by = serializers.ReadOnlyField(source="reviewed_by.username")

    class Meta:
        model = Evidence
        fields = [
            "id", "action", "title", "description", "reference",
            "submitted_by", "submitted_at", "status",
            "reviewed_by", "reviewed_at", "review_comment",
        ]
        read_only_fields = ["status", "submitted_at", "reviewed_by",
                            "reviewed_at", "review_comment"]


class MetricReadingSerializer(serializers.ModelSerializer):
    recorded_by = serializers.ReadOnlyField(source="recorded_by.username")

    class Meta:
        model = MetricReading
        fields = ["id", "metric", "batch_no", "value", "recorded_at", "recorded_by"]
        read_only_fields = ["recorded_at"]


class ReviewOpinionSerializer(serializers.ModelSerializer):
    user = serializers.ReadOnlyField(source="user.username")

    class Meta:
        model = ReviewOpinion
        fields = ["id", "evaluation", "user", "stance", "comment", "created_at"]
        read_only_fields = ["created_at"]


class EffectivenessEvaluationSerializer(serializers.ModelSerializer):
    created_by = serializers.ReadOnlyField(source="created_by.username")
    result_display = serializers.ReadOnlyField(source="get_result_display")
    opinions = ReviewOpinionSerializer(many=True, read_only=True)

    class Meta:
        model = EffectivenessEvaluation
        fields = [
            "id", "action", "version", "result", "result_display", "rationale",
            "metrics_summary", "sample_size", "eligible_for_review",
            "created_by", "created_at", "is_current", "opinions",
        ]


class SignOffSerializer(serializers.ModelSerializer):
    user = serializers.ReadOnlyField(source="user.username")
    decision_display = serializers.ReadOnlyField(source="get_decision_display")

    class Meta:
        model = SignOff
        fields = [
            "id", "action", "evaluation", "version", "decision",
            "decision_display", "comment", "user", "signed_at", "state",
            "withdrawn_reason", "withdrawn_at", "supersedes",
        ]
        read_only_fields = ["version", "state", "withdrawn_reason",
                            "withdrawn_at", "supersedes", "signed_at"]


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "timestamp", "actor", "action", "entity_type",
                  "entity_id", "summary", "detail"]

    def get_actor(self, obj):
        return obj.actor.username if obj.actor else "system"
