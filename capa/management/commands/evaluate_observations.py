"""
定时评估命令: 扫描所有处于观察期/待评审/已重开的措施, 执行有效性判定。

用法(建议由 cron / celery beat 定时触发, 例如每小时):
    python manage.py evaluate_observations

规则:
- 结论与当前版本一致时不重复建版;
- 数据不足且窗口已过 → 自动延长观察期(绝不判为成功);
- 触发失败条件 → 判定建议无效并允许提交评审。
"""
from django.core.management.base import BaseCommand

from capa.models import CAPAAction
from capa.services import evaluate_action


class Command(BaseCommand):
    help = "对观察期内的全部 CAPA 措施执行定时有效性评估"

    def handle(self, *args, **options):
        qs = CAPAAction.objects.filter(status__in=[
            CAPAAction.Status.MONITORING,
            CAPAAction.Status.PENDING_REVIEW,
            CAPAAction.Status.REOPENED,
        ])
        created, unchanged, failed = 0, 0, 0
        for action in qs:
            before = action.evaluations.count()
            try:
                evaluation = evaluate_action(action, actor=None, force=False)
            except Exception as exc:  # 单条失败不中断整体扫描
                failed += 1
                self.stderr.write(f"[失败] {action.code}: {exc}")
                continue
            if action.evaluations.count() > before:
                created += 1
                self.stdout.write(
                    f"[新判定] {action.code} -> v{evaluation.version} "
                    f"{evaluation.get_result_display()}"
                )
            else:
                unchanged += 1
                self.stdout.write(f"[无变化] {action.code} 维持 {evaluation.get_result_display()}")
        self.stdout.write(self.style.SUCCESS(
            f"评估完成: 新建版 {created}, 无变化 {unchanged}, 失败 {failed}"
        ))
