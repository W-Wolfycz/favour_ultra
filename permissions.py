# permissions.py
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


class PermLevel:
    UNKNOWN = -1
    MEMBER = 0
    HIGH = 1
    ADMIN = 2
    OWNER = 3
    SUPERUSER = 4


class PermissionManager:
    def __init__(self, superusers: list[str] | None = None, level_threshold: int = 50):
        self.superusers = superusers or []
        self.level_threshold = level_threshold

    async def get_perm_level(self, event: AiocqhttpMessageEvent, user_id: str | int) -> int:
        try:
            if str(user_id) in self.superusers:
                return PermLevel.SUPERUSER

            group_id = event.get_group_id()
            if not group_id:
                return PermLevel.MEMBER

            if not user_id:
                return PermLevel.UNKNOWN

            try:
                group_id_int = int(str(group_id).strip())
                user_id_int = int(str(user_id).strip())
            except ValueError:
                return PermLevel.MEMBER

            try:
                info = await event.bot.get_group_member_info(
                    group_id=group_id_int,
                    user_id=user_id_int,
                    no_cache=True
                )
            except Exception:
                return PermLevel.UNKNOWN

            role = info.get("role", "unknown")
            level = int(info.get("level", 0))

            if role == "owner":
                return PermLevel.OWNER
            elif role == "admin":
                return PermLevel.ADMIN
            elif role == "member":
                return PermLevel.HIGH if level >= self.level_threshold else PermLevel.MEMBER
            else:
                return PermLevel.UNKNOWN

        except Exception as e:
            logger.error(f"权限检查过程中发生错误: {str(e)}")
            return PermLevel.UNKNOWN

    async def check_permission(self, event, required_level: int) -> bool:
        if str(event.get_sender_id()) in self.superusers:
            return True
        if not isinstance(event, AiocqhttpMessageEvent):
            return False
        level = await self.get_perm_level(event, event.get_sender_id())
        return level >= required_level
