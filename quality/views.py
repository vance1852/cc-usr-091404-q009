"""DRF 视图：领域资源与业务动作。"""
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from . import engine, services
from .models import (
    AuditLog,
    CompletionEvidence,
    ConflictOpinion,
    CorrectiveAction,
    Deviation,
    EffectivenessEvaluation,
    Metric,
    MetricObservation,
    RelatedDeviation,
    RootCauseHypothesis,
)
from .roles import is_quality
from .serializers import (
    AuditLogSerializer,
    BaselineFreezeSerializer,
    CompletionEvidenceSerializer,
    ConflictOpinionSerializer,
    CorrectiveActionSerializer,
    DeviationSerializer,
    EvaluationActionSerializer,
    EvaluationListSerializer,
    EvidenceReviewSerializer,
    MetricObservationSerializer,
    MetricSerializer,
    OpinionCreateSerializer,
    RelatedDeviationCreateSerializer,
    RelatedDeviationSerializer,
    RootCauseHypothesisSerializer,
    SignOffCreateSerializer,
    SignOffSerializer,
    UserSerializer,
    WithdrawSerializer,
)


def _quality_required(user):
    if not is_quality(user):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("该操作需要质量角色")


def _service_error(exc):
    """把服务层异常映射为合适的 HTTP 状态：权限 403，校验 400。"""
    from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
    code = (status.HTTP_403_FORBIDDEN
            if isinstance(exc, DjangoPermissionDenied)
            else status.HTTP_400_BAD_REQUEST)
    return Response({"detail": str(exc)}, status=code)


class UserViewSet(viewsets.ModelViewSet):
    """用户与角色维护（仅管理员）。"""

    queryset = User.objects.all().select_related("profile")
    serializer_class = UserSerializer
    permission_classes = [IsAdminUser]
    http_method_names = ["get", "post", "head", "options"]


class DeviationViewSet(viewsets.ModelViewSet):
    queryset = Deviation.objects.all().select_related("created_by")
    serializer_class = DeviationSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """质量角色在所有措施有效后关闭偏差。"""
        _quality_required(request.user)
        deviation = self.get_object()
        try:
            services.close_deviation(deviation=deviation, user=request.user)
        except Exception as exc:  # ValidationError / PermissionDenied
            return _service_error(exc)
        return Response(DeviationSerializer(deviation).data)


class HypothesisViewSet(viewsets.ModelViewSet):
    serializer_class = RootCauseHypothesisSerializer

    def get_queryset(self):
        qs = RootCauseHypothesis.objects.all().select_related("raised_by")
        deviation_id = self.request.query_params.get("deviation")
        if deviation_id:
            qs = qs.filter(deviation_id=deviation_id)
        return qs

    def perform_create(self, serializer):
        serializer.save(raised_by=self.request.user)


class MetricViewSet(viewsets.ModelViewSet):
    """指标定义：基线固化前可维护，固化后只读。"""

    serializer_class = MetricSerializer

    def get_queryset(self):
        qs = Metric.objects.all().select_related("action")
        action_id = self.request.query_params.get("action")
        if action_id:
            qs = qs.filter(action_id=action_id)
        return qs

    def _ensure_unfrozen(self, action):
        if hasattr(action, "baseline"):
            from rest_framework.exceptions import ValidationError
            raise ValidationError(
                {"detail": "基线已在措施批准时固化，指标不可修改"})

    def perform_create(self, serializer):
        self._ensure_unfrozen(serializer.validated_data["action"])
        serializer.save()

    def perform_update(self, serializer):
        self._ensure_unfrozen(serializer.instance.action)
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        self._ensure_unfrozen(self.get_object().action)
        return super().destroy(request, *args, **kwargs)


class ObservationViewSet(viewsets.ModelViewSet):
    serializer_class = MetricObservationSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = MetricObservation.objects.all().select_related("metric", "recorded_by")
        metric_id = self.request.query_params.get("metric")
        action_id = self.request.query_params.get("action")
        if metric_id:
            qs = qs.filter(metric_id=metric_id)
        if action_id:
            qs = qs.filter(metric__action_id=action_id)
        return qs

    def perform_create(self, serializer):
        services.add_observation(
            metric=serializer.validated_data["metric"],
            user=self.request.user,
            value=serializer.validated_data["value"],
            observed_at=serializer.validated_data.get("observed_at"),
            batch_no=serializer.validated_data.get("batch_no", ""),
            sample_count=serializer.validated_data.get("sample_count", 1),
            note=serializer.validated_data.get("note", ""),
        )


class EvidenceViewSet(viewsets.ModelViewSet):
    serializer_class = CompletionEvidenceSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = CompletionEvidence.objects.all().select_related(
            "submitted_by", "approved_by", "action")
        action_id = self.request.query_params.get("action")
        if action_id:
            qs = qs.filter(action_id=action_id)
        return qs

    def perform_create(self, serializer):
        serializer.save(submitted_by=self.request.user)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """批准/拒绝证据；责任人不能批准自己的证据。"""
        evidence = self.get_object()
        serializer = EvidenceReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.approve_evidence(
                evidence=evidence, user=request.user,
                approved=serializer.validated_data["approved"],
                review_note=serializer.validated_data["review_note"],
            )
        except Exception as exc:
            return _service_error(exc)
        return Response(CompletionEvidenceSerializer(evidence).data)


class CorrectiveActionViewSet(viewsets.ModelViewSet):
    queryset = CorrectiveAction.objects.all().select_related(
        "responsible_user", "created_by")
    serializer_class = CorrectiveActionSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    # -------------------------------------------------- 基线固化
    @action(detail=True, methods=["post"])
    def freeze_baseline(self, request, pk=None):
        """措施批准时固化量化基线、目标、观察窗口与失败条件。"""
        action = self.get_object()
        payload = BaselineFreezeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            services.freeze_baseline(
                action=action, user=request.user,
                window_days=data["window_days"],
                min_sample_size=data["min_sample_size"],
                max_extensions=data["max_extensions"],
                note=data.get("note", ""),
                metrics=[
                    {**m, "action": action} for m in data.get("metrics", [])
                ],
            )
        except Exception as exc:
            return _service_error(exc)
        action.refresh_from_db()
        return Response(CorrectiveActionSerializer(action).data,
                        status=status.HTTP_201_CREATED)

    # -------------------------------------------------- 完成登记
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        action = self.get_object()
        try:
            services.mark_action_completed(action=action, user=request.user)
        except Exception as exc:
            return _service_error(exc)
        return Response(CorrectiveActionSerializer(action).data)

    # -------------------------------------------------- 手动触发评估
    @action(detail=True, methods=["post"])
    def evaluate_now(self, request, pk=None):
        action = self.get_object()
        services.run_evaluation(action=action, actor=request.user)
        return Response(self._assessment(action))

    # -------------------------------------------------- 有效性看板
    @action(detail=True, methods=["get"])
    def assessment(self, request, pk=None):
        """证据覆盖、指标趋势摘要、剩余观察期、冲突意见与系统建议。"""
        return Response(self._assessment(self.get_object()))

    def _assessment(self, action):
        evaluation = action.current_evaluation
        engine_result = None
        if evaluation:
            # 评审中也按最新数据复核，供签署人看到实时指标；
            # 已判定有效的版本保持结论短路，不让新数据改写展示状态。
            force = evaluation.status in (
                EffectivenessEvaluation.Status.OBSERVING,
                EffectivenessEvaluation.Status.EXTENDED,
                EffectivenessEvaluation.Status.READY,
                EffectivenessEvaluation.Status.IN_REVIEW,
                EffectivenessEvaluation.Status.DATA_INSUFFICIENT,
            )
            engine_result = engine.evaluate(
                evaluation, force_recheck=force).as_dict()
        evidences = CompletionEvidenceSerializer(
            action.evidences.all(), many=True).data
        opinions = []
        signoffs = []
        if evaluation:
            opinions = ConflictOpinionSerializer(
                evaluation.opinions.select_related("author"), many=True).data
            signoffs = SignOffSerializer(
                evaluation.signoffs.select_related("signer"), many=True).data
        related = RelatedDeviationSerializer(
            action.related_deviations.select_related("deviation", "linked_by"),
            many=True).data
        history = EvaluationListSerializer(
            action.evaluations.all(), many=True).data
        return {
            "action": CorrectiveActionSerializer(action).data,
            "evaluation_version": evaluation.version if evaluation else None,
            "engine": engine_result,
            "evidence_coverage": {
                "total": len(evidences),
                "approved": sum(1 for e in evidences
                                if e["approval_status"] == "approved"),
                "rejected": sum(1 for e in evidences
                                if e["approval_status"] == "rejected"),
                "pending": sum(1 for e in evidences
                               if e["approval_status"] == "pending"),
                "items": evidences,
            },
            "conflict_opinions": opinions,
            "signoffs": signoffs,
            "related_deviations": related,
            "evaluation_history": history,
        }

    # -------------------------------------------------- 提交评审
    @action(detail=True, methods=["post"], url_path="submit-review")
    def submit_review(self, request, pk=None):
        action = self.get_object()
        serializer = EvaluationActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.submit_for_review(
                action=action, user=request.user,
                note=serializer.validated_data.get("note", ""))
        except Exception as exc:
            return _service_error(exc)
        return Response(self._assessment(action))

    # -------------------------------------------------- 质量签署
    @action(detail=True, methods=["post"])
    def sign(self, request, pk=None):
        action = self.get_object()
        evaluation = action.current_evaluation
        if evaluation is None:
            return Response({"detail": "尚无判定版本"},
                            status=status.HTTP_400_BAD_REQUEST)
        serializer = SignOffCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.sign_off(
                evaluation=evaluation, user=request.user,
                decision=serializer.validated_data["decision"],
                reason=serializer.validated_data.get("reason", ""))
        except Exception as exc:
            return _service_error(exc)
        return Response(self._assessment(action))

    # -------------------------------------------------- 撤回签署
    @action(detail=True, methods=["post"], url_path="withdraw-signoff")
    def withdraw_signoff(self, request, pk=None):
        action = self.get_object()
        evaluation = action.current_evaluation
        if evaluation is None:
            return Response({"detail": "尚无判定版本"},
                            status=status.HTTP_400_BAD_REQUEST)
        serializer = WithdrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.withdraw_signoff(
                evaluation=evaluation, user=request.user,
                reason=serializer.validated_data["reason"])
        except Exception as exc:
            return _service_error(exc)
        return Response(self._assessment(action))

    # -------------------------------------------------- 冲突意见
    @action(detail=True, methods=["get", "post"])
    def opinions(self, request, pk=None):
        action = self.get_object()
        evaluation = action.current_evaluation
        if evaluation is None:
            return Response({"detail": "尚无判定版本"},
                            status=status.HTTP_400_BAD_REQUEST)
        if request.method == "GET":
            return Response(ConflictOpinionSerializer(
                evaluation.opinions.all(), many=True).data)
        serializer = OpinionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        opinion = services.add_conflict_opinion(
            evaluation=evaluation, user=request.user,
            stance=serializer.validated_data["stance"],
            content=serializer.validated_data["content"])
        return Response(ConflictOpinionSerializer(opinion).data,
                        status=status.HTTP_201_CREATED)

    # -------------------------------------------------- 相关偏差/重开
    @action(detail=True, methods=["post"], url_path="link-related")
    def link_related(self, request, pk=None):
        action = self.get_object()
        serializer = RelatedDeviationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        deviation = get_object_or_404(
            Deviation, pk=serializer.validated_data["deviation_id"])
        try:
            services.link_related_deviation(
                action=action, deviation=deviation, user=request.user,
                relationship_note=serializer.validated_data.get(
                    "relationship_note", ""))
        except Exception as exc:
            return _service_error(exc)
        return Response(self._assessment(action),
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def related(self, request, pk=None):
        action = self.get_object()
        return Response(RelatedDeviationSerializer(
            action.related_deviations.all(), many=True).data)


class EvaluationViewSet(ReadOnlyModelViewSet):
    """判定版本（含历史版本、意见与签署链）。"""

    queryset = EffectivenessEvaluation.objects.all().select_related(
        "action", "trigger_deviation")

    def get_serializer_class(self):
        return EvaluationListSerializer

    @action(detail=True, methods=["get"])
    def detail(self, request, pk=None):
        evaluation = self.get_object()
        engine_result = engine.evaluate(
            evaluation, force_recheck=True).as_dict()
        return Response({
            "evaluation": EvaluationListSerializer(evaluation).data,
            "engine_snapshot": engine_result,
            "opinions": ConflictOpinionSerializer(
                evaluation.opinions.all(), many=True).data,
            "signoffs": SignOffSerializer(
                evaluation.signoffs.all(), many=True).data,
        })


class AuditLogViewSet(ReadOnlyModelViewSet):
    """审计接口：仅质量角色可查。"""

    queryset = AuditLog.objects.all().select_related("actor")
    serializer_class = AuditLogSerializer

    def list(self, request, *args, **kwargs):
        _quality_required(request.user)
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        _quality_required(request.user)
        return super().retrieve(request, *args, **kwargs)
