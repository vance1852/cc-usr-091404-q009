from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
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
from .serializers import (
    AuditLogSerializer,
    CAPAActionSerializer,
    DeviationSerializer,
    EffectivenessEvaluationSerializer,
    EffectivenessPlanSerializer,
    EvidenceSerializer,
    FailureConditionSerializer,
    MetricReadingSerializer,
    ObservationMetricSerializer,
    ReviewOpinionSerializer,
    RootCauseHypothesisSerializer,
    SignOffSerializer,
)


class ServiceErrorMixin:
    """把业务异常映射为 HTTP 响应。"""

    def handle_service_call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except services.PermissionDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except services.BusinessRuleViolation as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class DeviationViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    queryset = Deviation.objects.all()
    serializer_class = DeviationSerializer

    def perform_create(self, serializer):
        deviation = serializer.save(created_by=self.request.user)
        # 创建时即关联措施 → 触发自动重开
        if deviation.linked_action_id:
            services.link_deviation(deviation, deviation.linked_action, self.request.user)

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("linked_action"):
            qs = qs.filter(linked_action_id=params["linked_action"])
        return qs

    @action(detail=True, methods=["post"], url_path="link-action")
    def link_action(self, request, pk=None):
        """把既有偏差关联到措施(后续/复发偏差), 必要时时自动重开判定。"""
        deviation = self.get_object()
        action_id = request.data.get("action")
        try:
            capa_action = CAPAAction.objects.get(pk=action_id)
        except CAPAAction.DoesNotExist:
            return Response({"detail": "措施不存在"}, status=status.HTTP_404_NOT_FOUND)
        result = self.handle_service_call(
            services.link_deviation, deviation, capa_action, request.user)
        if isinstance(result, Response):
            return result
        return Response(DeviationSerializer(result).data)


class RootCauseHypothesisViewSet(viewsets.ModelViewSet):
    queryset = RootCauseHypothesis.objects.all()
    serializer_class = RootCauseHypothesisSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("deviation"):
            qs = qs.filter(deviation_id=self.request.query_params["deviation"])
        return qs


class CAPAActionViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    queryset = CAPAAction.objects.select_related("owner", "deviation")
    serializer_class = CAPAActionSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("owner"):
            qs = qs.filter(owner__username=params["owner"])
        return qs

    @action(detail=True, methods=["post"], url_path="submit-plan")
    def submit_plan(self, request, pk=None):
        result = self.handle_service_call(services.submit_plan, self.get_object(), request.user)
        if isinstance(result, Response):
            return result
        return Response(CAPAActionSerializer(result).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        result = self.handle_service_call(services.approve_action, self.get_object(), request.user)
        if isinstance(result, Response):
            return result
        return Response(CAPAActionSerializer(result).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        result = self.handle_service_call(services.complete_action, self.get_object(), request.user)
        if isinstance(result, Response):
            return result
        return Response(CAPAActionSerializer(result).data)

    @action(detail=True, methods=["post"])
    def evaluate(self, request, pk=None):
        """手动执行一次有效性判定(强制产生新版本)。"""
        result = self.handle_service_call(
            services.evaluate_action, self.get_object(), actor=request.user, force=True)
        if isinstance(result, Response):
            return result
        return Response(EffectivenessEvaluationSerializer(result).data)

    @action(detail=True, methods=["post"], url_path="submit-for-review")
    def submit_for_review(self, request, pk=None):
        result = self.handle_service_call(
            services.submit_for_review, self.get_object(), request.user)
        if isinstance(result, Response):
            return result
        return Response(CAPAActionSerializer(result).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        result = self.handle_service_call(
            services.close_action, self.get_object(), request.user,
            decision=request.data.get("decision", ""),
            comment=request.data.get("comment", ""))
        if isinstance(result, Response):
            return result
        return Response(SignOffSerializer(result).data)

    @action(detail=True, methods=["post"], url_path="extend-observation")
    def extend_observation(self, request, pk=None):
        result = self.handle_service_call(
            services.extend_observation, self.get_object(), request.user,
            days=int(request.data.get("days", 0)),
            reason=request.data.get("reason", ""))
        if isinstance(result, Response):
            return result
        return Response(EffectivenessPlanSerializer(result).data)

    @action(detail=True, methods=["get"])
    def dashboard(self, request, pk=None):
        """单项措施看板: 证据覆盖、指标趋势、剩余观察期、冲突意见与系统建议。"""
        return Response(services.build_action_dashboard(self.get_object()))

    @action(detail=False, methods=["get"])
    def overview(self, request):
        """负责人总览: 全部措施看板(可按状态过滤)。"""
        qs = self.get_queryset()
        return Response([services.build_action_dashboard(a) for a in qs])


class EffectivenessPlanViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    queryset = EffectivenessPlan.objects.prefetch_related("metrics", "failure_conditions")
    serializer_class = EffectivenessPlanSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("action"):
            qs = qs.filter(action_id=self.request.query_params["action"])
        return qs

    def _reject_if_frozen(self, instance):
        if instance.is_frozen:
            return Response(
                {"detail": "计划已在批准时固化, 禁止修改; 数据不足请走观察期延长流程"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def update(self, request, *args, **kwargs):
        blocked = self._reject_if_frozen(self.get_object())
        return blocked or super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        blocked = self._reject_if_frozen(self.get_object())
        return blocked or super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        blocked = self._reject_if_frozen(self.get_object())
        return blocked or super().destroy(request, *args, **kwargs)


class ObservationMetricViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    queryset = ObservationMetric.objects.all()
    serializer_class = ObservationMetricSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("plan"):
            qs = qs.filter(plan_id=self.request.query_params["plan"])
        return qs

    def _reject_if_frozen(self, instance):
        if instance.plan.is_frozen:
            return Response(
                {"detail": "计划已固化, 指标不可变更"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def update(self, request, *args, **kwargs):
        blocked = self._reject_if_frozen(self.get_object())
        return blocked or super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        blocked = self._reject_if_frozen(self.get_object())
        return blocked or super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        blocked = self._reject_if_frozen(self.get_object())
        return blocked or super().destroy(request, *args, **kwargs)


class FailureConditionViewSet(viewsets.ModelViewSet):
    queryset = FailureCondition.objects.all()
    serializer_class = FailureConditionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("plan"):
            qs = qs.filter(plan_id=self.request.query_params["plan"])
        return qs


class EvidenceViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    queryset = Evidence.objects.all()
    serializer_class = EvidenceSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("action"):
            qs = qs.filter(action_id=self.request.query_params["action"])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            evidence = services.submit_evidence(
                CAPAAction.objects.get(pk=serializer.validated_data["action"].pk),
                request.user,
                title=serializer.validated_data["title"],
                description=serializer.validated_data.get("description", ""),
                reference=serializer.validated_data.get("reference", ""),
            )
        except CAPAAction.DoesNotExist:
            return Response({"detail": "措施不存在"}, status=status.HTTP_404_NOT_FOUND)
        except services.PermissionDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except services.BusinessRuleViolation as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(EvidenceSerializer(evidence).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        result = self.handle_service_call(
            services.review_evidence, self.get_object(), request.user,
            approve=True, comment=request.data.get("comment", ""))
        if isinstance(result, Response):
            return result
        return Response(EvidenceSerializer(result).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        result = self.handle_service_call(
            services.review_evidence, self.get_object(), request.user,
            approve=False, comment=request.data.get("comment", ""))
        if isinstance(result, Response):
            return result
        return Response(EvidenceSerializer(result).data)


class MetricReadingViewSet(ServiceErrorMixin, viewsets.ModelViewSet):
    queryset = MetricReading.objects.all()
    serializer_class = MetricReadingSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("metric"):
            qs = qs.filter(metric_id=params["metric"])
        if params.get("batch_no"):
            qs = qs.filter(batch_no=params["batch_no"])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reading = services.record_reading(
                serializer.validated_data["metric"],
                request.user,
                batch_no=serializer.validated_data["batch_no"],
                value=serializer.validated_data["value"],
            )
        except services.PermissionDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except services.BusinessRuleViolation as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MetricReadingSerializer(reading).data, status=status.HTTP_201_CREATED)


class EffectivenessEvaluationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EffectivenessEvaluation.objects.prefetch_related("opinions")
    serializer_class = EffectivenessEvaluationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("action"):
            qs = qs.filter(action_id=params["action"])
        if params.get("is_current") in ("true", "1"):
            qs = qs.filter(is_current=True)
        return qs

    @action(detail=True, methods=["post"], url_path="opinions")
    def add_opinion(self, request, pk=None):
        """对某版判定发表意见(同意/反对), 反对意见构成冲突意见展示给负责人。"""
        evaluation = self.get_object()
        serializer = ReviewOpinionSerializer(data={
            "evaluation": evaluation.pk,
            "stance": request.data.get("stance"),
            "comment": request.data.get("comment", ""),
        })
        serializer.is_valid(raise_exception=True)
        opinion, _ = ReviewOpinion.objects.update_or_create(
            evaluation=evaluation, user=request.user,
            defaults={"stance": serializer.validated_data["stance"],
                      "comment": serializer.validated_data.get("comment", "")},
        )
        services.log_audit(request.user, "capa.review_opinion", evaluation,
                           f"评审意见: {opinion.get_stance_display()}")
        return Response(ReviewOpinionSerializer(opinion).data,
                        status=status.HTTP_201_CREATED)


class SignOffViewSet(ServiceErrorMixin, viewsets.ReadOnlyModelViewSet):
    queryset = SignOff.objects.all()
    serializer_class = SignOffSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("action"):
            qs = qs.filter(action_id=self.request.query_params["action"])
        return qs

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        result = self.handle_service_call(
            services.withdraw_signoff, self.get_object(), request.user,
            reason=request.data.get("reason", ""))
        if isinstance(result, Response):
            return result
        return Response(SignOffSerializer(result).data)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """审计接口: 按对象类型/对象ID/操作人/操作类型过滤。"""

    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("entity_type"):
            qs = qs.filter(entity_type=params["entity_type"])
        if params.get("entity_id"):
            qs = qs.filter(entity_id=params["entity_id"])
        if params.get("actor"):
            qs = qs.filter(actor__username=params["actor"])
        if params.get("action"):
            qs = qs.filter(action=params["action"])
        return qs
