from django.contrib import admin

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


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "department")


class HypothesisInline(admin.TabularInline):
    model = RootCauseHypothesis
    extra = 0


@admin.register(Deviation)
class DeviationAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "severity", "status", "batch_no",
                    "discovered_at")
    list_filter = ("status", "severity")
    search_fields = ("code", "title", "batch_no")
    inlines = [HypothesisInline]


class MetricInline(admin.TabularInline):
    model = Metric
    extra = 0


class EvidenceInline(admin.TabularInline):
    model = CompletionEvidence
    extra = 0


@admin.register(CorrectiveAction)
class CorrectiveActionAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "deviation", "responsible_user",
                    "status", "due_date", "completed_at")
    list_filter = ("status", "kind")
    search_fields = ("code", "title")
    inlines = [MetricInline, EvidenceInline]


@admin.register(EffectivenessEvaluation)
class EffectivenessEvaluationAdmin(admin.ModelAdmin):
    list_display = ("action", "version", "status", "result", "is_current",
                    "window_start", "window_end", "extension_count")
    list_filter = ("status", "is_current")


@admin.register(SignOff)
class SignOffAdmin(admin.ModelAdmin):
    list_display = ("evaluation", "signoff_version", "signer", "decision",
                    "is_active", "signed_at")
    list_filter = ("decision", "is_active")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor_name", "action", "entity_type",
                    "entity_id", "summary")
    list_filter = ("action", "entity_type")
    search_fields = ("summary", "entity_id")


admin.site.register([EffectivenessBaseline, MetricObservation,
                     RelatedDeviation, ConflictOpinion])
