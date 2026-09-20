from django.contrib import admin

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


@admin.register(Deviation)
class DeviationAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "severity", "status", "occurred_at", "linked_action")
    list_filter = ("status", "severity")


@admin.register(CAPAAction)
class CAPAActionAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "owner", "status", "due_date")
    list_filter = ("status",)


@admin.register(EffectivenessPlan)
class EffectivenessPlanAdmin(admin.ModelAdmin):
    list_display = ("action", "version", "observation_start", "observation_end",
                    "min_sample_size", "is_frozen", "extension_count")
    list_filter = ("is_frozen",)


@admin.register(Evidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display = ("title", "action", "submitted_by", "status", "reviewed_by")
    list_filter = ("status",)


@admin.register(EffectivenessEvaluation)
class EffectivenessEvaluationAdmin(admin.ModelAdmin):
    list_display = ("action", "version", "result", "sample_size",
                    "eligible_for_review", "is_current", "created_at")
    list_filter = ("result", "is_current")


@admin.register(SignOff)
class SignOffAdmin(admin.ModelAdmin):
    list_display = ("action", "version", "decision", "user", "state", "signed_at")
    list_filter = ("decision", "state")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor", "action", "entity_type", "entity_id", "summary")
    list_filter = ("action", "entity_type")


admin.site.register([
    RootCauseHypothesis, ObservationMetric, FailureCondition,
    MetricReading, ReviewOpinion,
])
