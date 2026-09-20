"""API 路由。"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("deviations", views.DeviationViewSet, basename="deviation")
router.register("hypotheses", views.HypothesisViewSet, basename="hypothesis")
router.register("actions", views.CorrectiveActionViewSet,
                basename="correctiveaction")
router.register("metrics", views.MetricViewSet, basename="metric")
router.register("observations", views.ObservationViewSet,
                basename="observation")
router.register("evidences", views.EvidenceViewSet, basename="evidence")
router.register("evaluations", views.EvaluationViewSet,
                basename="evaluation")
router.register("audit-logs", views.AuditLogViewSet, basename="auditlog")

urlpatterns = [
    path("", include(router.urls)),
]
