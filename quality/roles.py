"""角色与状态常量。"""


class Role:
    QUALITY = "quality"        # 质量角色（QA / 质量体系负责人）
    OWNER = "owner"            # 措施责任人
    INVESTIGATOR = "investigator"  # 偏差调查人员

    CHOICES = [
        (QUALITY, "质量角色"),
        (OWNER, "措施责任人"),
        (INVESTIGATOR, "调查人员"),
    ]


def is_quality(user) -> bool:
    """超级用户或具有 quality 角色的用户视为质量角色。"""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == Role.QUALITY)
