"""
定时评估入口：由外部调度器（cron / systemd / APScheduler）周期性执行，
例如每小时一次：

    python manage.py run_effectiveness_checks

也可用 --as-of 指定评估时刻（测试/复盘用）。
"""
from datetime import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from quality.services import run_due_evaluations


class Command(BaseCommand):
    help = "执行 CAPA 有效性定时评估（延长窗口/判定达标/失败）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-of", dest="as_of", default=None,
            help="ISO 8601 时刻；默认当前时间",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        if options["as_of"]:
            parsed = parse_datetime(options["as_of"])
            if parsed is None:
                raise SystemExit("--as-of 必须是合法 ISO 8601 时间")
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            now = parsed
        touched = run_due_evaluations(now=now)
        ready = sum(
            1 for e in touched
            if e.status == "ready")
        extended = sum(1 for e in touched if e.status == "extended")
        ineffective = sum(1 for e in touched if e.status == "ineffective")
        observing = sum(1 for e in touched if e.status in ("observing",))
        self.stdout.write(
            f"评估 {len(touched)} 项措施：观察中 {observing}，可评审 {ready}，"
            f"延长 {extended}，无效 {ineffective}"
        )
