from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AuditLogViewSet,
    CAPAActionViewSet,
    DeviationViewSet,
    EffectivenessEvaluationViewSet,
    EffectivenessPlanViewSet,
    EvidenceViewSet,
    FailureConditionViewSet,
    MetricReadingViewSet,
    ObservationMetricViewSet,
    RootCauseHypothesisViewSet,
    SignOffViewSet,
)

router = DefaultRouter()
router.register("deviations", DeviationViewSet, basename="deviation")
router.register("hypotheses", RootCauseHypothesisViewSet, basename="hypothesis")
router.register("actions", CAPAActionViewSet, basename="capaaction")
router.register("plans", EffectivenessPlanViewSet, basename="plan")
router.register("metrics", ObservationMetricViewSet, basename="metric")
router.register("failure-conditions", FailureConditionViewSet, basename="failurecondition")
router.register("evidence", EvidenceViewSet, basename="evidence")
router.register("readings", MetricReadingViewSet, basename="reading")
router.register("evaluations", EffectivenessEvaluationViewSet, basename="evaluation")
router.register("signoffs", SignOffViewSet, basename="signoff")
router.register("audit", AuditLogViewSet, basename="audit")

urlpatterns = [
    path("", include(router.urls)),
]
