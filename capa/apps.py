from django.apps import AppConfig


class CapaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "capa"
    verbose_name = "偏差与 CAPA 有效性判定"
