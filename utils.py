# utils.py
import string

from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent


def is_valid_userid(userid: str) -> bool:
    if not userid or len(userid.strip()) == 0:
        return False
    userid = userid.strip()
    if len(userid) > 64:
        return False
    allowed_chars = string.ascii_letters + string.digits + "_-:@."
    return all(c in allowed_chars for c in userid)

def get_target_uid(event: AstrMessageEvent, text_arg: str) -> str | None:
    bot_self_id = None
    if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'self_id'):
        bot_self_id = str(event.message_obj.self_id)

    if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message'):
        for component in event.message_obj.message:
            if isinstance(component, At):
                uid = str(component.qq)
                if bot_self_id and uid == bot_self_id:
                    continue
                return uid

    if text_arg:
        cleaned_arg = text_arg.strip()
        if is_valid_userid(cleaned_arg):
            return cleaned_arg

    return None

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    mapping = {
        "|": "&#124;",
        "`": "&#96;",
        "*": "&#42;",
        "~": "&#126;",
        "_": "&#95;",
        "[": "&#91;",
        "]": "&#93;",
        "\n": " "
    }
    for char, entity in mapping.items():
        text = text.replace(char, entity)
    return text

async def get_user_display_name(event: AstrMessageEvent, user_id: str) -> str:
    try:
        group_id = event.get_group_id()
        if group_id:
            info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id), no_cache=True)
            return info.get("card") or info.get("nickname") or user_id
        else:
            info = await event.bot.get_stranger_info(user_id=int(user_id))
            return info.get("nickname") or user_id
    except:
        return user_id
