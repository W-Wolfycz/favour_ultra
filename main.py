# main.py
# Modified from original by wolfycz - Removed relationship feature
# Original work: https://github.com/nuomicici/astrbot_plugin_Favour_Ultra/
# Licensed under the Apache License, Version 2.0
import re
import json
import traceback
import hashlib
from pathlib import Path
from typing import List
from datetime import datetime
import asyncio

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.api.event import filter
from astrbot.core.utils.session_waiter import session_waiter, SessionController

from .utils import is_valid_userid, get_target_uid, escape_markdown, get_user_display_name
from .permissions import PermLevel, PermissionManager
from .storage import FavourDBManager, FavourRecord

class FavourManagerTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 基础配置
        self.favour_mode = self.config.get("favour_mode", "galgame")
        self.group_sort_by = self.config.get("group_sort_by", "default")
        self.min_favour_value = self.config.get("min_favour_value", -100)
        self.max_favour_value = self.config.get("max_favour_value", 100)
        self.default_favour = self.config.get("default_favour", 0)

        # 关系映射配置
        rel_conf = self.config.get("relationship_config", {})
        self.relationship_mode = rel_conf.get("mode", "simple")
        self.relationship_simple_list = rel_conf.get("simple_list", ["极度厌恶", "厌恶", "反感", "普通", "喜欢", "亲密", "挚爱"])
        self.relationship_advance_raw = rel_conf.get("advance_config", "")

        # 高级配置
        adv_conf = self.config.get("advanced_config", {})
        self.admin_default_favour = adv_conf.get("admin_default_favour", 50)
        self.admin_default_relationship = adv_conf.get("admin_default_relationship", "")
        self.favour_envoys = adv_conf.get("favour_envoys", [])
        self.favour_increase_min = adv_conf.get("favour_increase_min", 1)
        self.favour_increase_max = adv_conf.get("favour_increase_max", 3)
        self.favour_decrease_min = adv_conf.get("favour_decrease_min", 1)
        self.favour_decrease_max = adv_conf.get("favour_decrease_max", 5)
        self.perm_level_threshold = adv_conf.get("level_threshold", 50)
        self.allow_self_decrease = adv_conf.get("allow_self_decrease", False)

        # 命令权限配置
        self.member_commands = adv_conf.get("member_commands", ["查询好感度", "好感度帮助", "好感度指令帮助"])
        self.high_commands = adv_conf.get("high_commands", [])
        self.admin_commands = adv_conf.get("admin_commands", ["修改好感度"])
        self.owner_commands = adv_conf.get("owner_commands", ["清空好感度"])
        self.superuser_commands = adv_conf.get("superuser_commands", ["查询全局好感度", "清空全局好感度"])
        self.query_others_favour_level = adv_conf.get("query_others_favour_level", "群管理员")

        self._validate_config()

        # 权限管理初始化
        self.admins_id = context.get_config().get("admins_id", [])
        self.perm_mgr = PermissionManager(
            superusers=self.admins_id,
            level_threshold=self.perm_level_threshold
        )

        # 裁判模型配置
        self.judge_provider = self.config.get("judge_provider", "")

        # 特权用户集合（管理员+特使）
        self._privileged_user_ids = set(self.admins_id) | set(str(e) for e in self.favour_envoys)

        # 人设管理器
        self.persona_mgr = self.context.persona_manager

        # 数据库初始化
        self.data_dir = Path(context.get_config().get("plugin.data_dir", "./data")) / "plugin_data" / "astrbot_plugin_favour_w"
        self.db_manager = FavourDBManager(self.data_dir, self.min_favour_value, self.max_favour_value)

        # 异步初始化数据库
        asyncio.create_task(self._init_storage())

        # Playwright T2I
        self._pw_instance = None
        self._pw_browser = None
        self._t2i_output_dir = self.data_dir / "t2i_output"
        self._t2i_output_dir.mkdir(parents=True, exist_ok=True)
        self._html_template = (Path(__file__).parent / "自定义t2i模板.html").read_text(encoding="utf-8")
        _ver_match = re.search(r'version:\s*["\']?(.+?)["\']?\s*$', (Path(__file__).parent / "metadata.yaml").read_text(), re.MULTILINE)
        self._plugin_version = _ver_match.group(1) if _ver_match else ""

    async def _init_storage(self):
        try:
            await self.db_manager.init_db()
        except Exception as e:
            logger.error(f"数据库初始化失败: {str(e)}\n{traceback.format_exc()}")

        try:
            await self._ensure_browser()
            logger.info("Playwright 浏览器预热完成")
        except Exception as e:
            logger.warning(f"Playwright 浏览器预热失败: {e}")

    def _validate_config(self) -> None:
        if self.min_favour_value >= self.max_favour_value:
             self.min_favour_value = -100
             self.max_favour_value = 100

        self.default_favour = max(self.min_favour_value, min(self.max_favour_value, self.default_favour))
        self.admin_default_favour = max(self.min_favour_value, min(self.max_favour_value, self.admin_default_favour))

    async def _get_persona_id(self, event: AstrMessageEvent) -> str:
        umo = event.unified_msg_origin
        persona = await self.persona_mgr.get_default_persona_v3(umo)
        p_name = persona["name"] if persona else None
        logger.debug(f"[Bot:{event.get_platform_id()}] _get_persona_id: umo={umo}, persona_id={p_name}")
        if p_name:
            return p_name
        return "default"

    async def _check_command_permission(self, event: AstrMessageEvent, command_name: str) -> bool:
        user_id = event.get_sender_id()

        if user_id in self.admins_id:
            role = PermLevel.SUPERUSER
        elif isinstance(event, AiocqhttpMessageEvent):
            role = await self.perm_mgr.get_perm_level(event, user_id)
        else:
            role = PermLevel.MEMBER

        allowed_commands = set()
        if role >= PermLevel.MEMBER:
            allowed_commands.update(self.member_commands)
        if role >= PermLevel.HIGH:
            allowed_commands.update(self.high_commands)
        if role >= PermLevel.ADMIN:
            allowed_commands.update(self.admin_commands)
        if role >= PermLevel.OWNER:
            allowed_commands.update(self.owner_commands)
        if role >= PermLevel.SUPERUSER:
            allowed_commands.update(self.superuser_commands)

        return command_name in allowed_commands

    _LEVEL_NAME_MAP = {
        "普通成员": PermLevel.MEMBER,
        "高等级群员": PermLevel.HIGH,
        "群管理员": PermLevel.ADMIN,
        "群主": PermLevel.OWNER,
        "Bot管理员": PermLevel.SUPERUSER,
    }

    async def _get_user_perm_level(self, event: AstrMessageEvent) -> int:
        user_id = event.get_sender_id()
        if user_id in self.admins_id:
            return PermLevel.SUPERUSER
        if isinstance(event, AiocqhttpMessageEvent):
            return await self.perm_mgr.get_perm_level(event, user_id)
        return PermLevel.MEMBER

    def _get_relationship(self, favour: int, user_id: str = "") -> str:
        if self.admin_default_relationship and user_id and user_id in self._privileged_user_ids:
            return self.admin_default_relationship

        favour = max(self.min_favour_value, min(self.max_favour_value, favour))

        if self.relationship_mode == "simple":
            items = self.relationship_simple_list
            if not items:
                return "未知"
            n = len(items)
            total = self.max_favour_value - self.min_favour_value
            if total <= 0:
                return items[0]
            idx = int((favour - self.min_favour_value) * n / total)
            return items[max(0, min(idx, n - 1))]
        else:
            try:
                items = json.loads(self.relationship_advance_raw) if isinstance(self.relationship_advance_raw, str) else self.relationship_advance_raw
                for item in items:
                    if item.get("min_value", self.min_favour_value) <= favour <= item.get("max_value", self.max_favour_value):
                        return item.get("describe", "未知")
            except (json.JSONDecodeError, TypeError):
                pass
            return "未知"

    async def _get_initial_favour(self, event: AstrMessageEvent) -> int:
        user_id = event.get_sender_id()

        is_envoy = str(user_id) in [str(e) for e in self.favour_envoys]
        is_admin = await self.perm_mgr.check_permission(event, PermLevel.OWNER)

        base = self.admin_default_favour if (is_envoy or is_admin) else self.default_favour
        return max(self.min_favour_value, min(self.max_favour_value, base))

    async def _sort_records(self, event: AstrMessageEvent, records: List[FavourRecord]) -> List[FavourRecord]:
        if not records:
            return []

        if self.group_sort_by == "favour":
            return sorted(records, key=lambda x: x.favour, reverse=True)
        elif self.group_sort_by == "userid":
            return sorted(records, key=lambda x: x.user_id)
        elif self.group_sort_by == "nickname":
            enriched = []
            for r in records:
                name = await get_user_display_name(event, r.user_id)
                enriched.append((name, r))
            enriched.sort(key=lambda x: x[0].lower())
            return [x[1] for x in enriched]
        else:
            return sorted(records, key=lambda x: x.created_at if x.created_at else datetime.min)

    async def _ensure_browser(self):
        if self._pw_browser is None or not self._pw_browser.is_connected():
            from playwright.async_api import async_playwright
            if self._pw_instance:
                await self._pw_instance.stop()
            self._pw_instance = await async_playwright().start()
            self._pw_browser = await self._pw_instance.chromium.launch()
        return self._pw_browser

    async def _render_t2i(self, md_text: str) -> str:
        browser = await self._ensure_browser()
        page = await browser.new_page(viewport={"width": 800, "height": 600})

        html = self._html_template.replace("{{ version }}", self._plugin_version)
        safe_text = md_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
        html = html.replace("{{ text | safe }}", safe_text)

        await page.set_content(html, wait_until="networkidle")

        filename = hashlib.md5(md_text.encode()).hexdigest()[:12] + ".png"
        output_path = self._t2i_output_dir / filename
        await page.screenshot(path=str(output_path), full_page=True)
        await page.close()

        return str(output_path)

    async def _send_chunked_t2i(self, event: AstrMessageEvent, title: str, headers: List[str], rows: List[str], chunk_size: int = 200):
        total = len(rows)
        if total == 0:
            await event.send(event.plain_result(f"{title}\n暂无数据"))
            return

        persona_id = await self._get_persona_id(event)

        for i in range(0, total, chunk_size):
            chunk = rows[i:i+chunk_size]
            page_info = f"({i+1}-{min(i+chunk_size, total)}/{total})" if total > chunk_size else ""

            md_lines = [f"# {title} {page_info}", ""]
            md_lines.extend(headers)
            md_lines.extend(chunk)

            md_text = "\n".join(md_lines)
            try:
                img_path = await self._render_t2i(md_text)
                await event.send(event.image_result(img_path))
            except Exception as e:
                logger.error(f"[Bot:{event.get_platform_id()}] 生成图片失败 (Page {page_info}): {e}")
                await event.send(event.plain_result(f"生成图片失败，请检查日志。"))

    # ================= 事件处理 =================

    @filter.on_llm_request()
    async def inject_favour_prompt(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            user_id = event.get_sender_id()
            persona_id = await self._get_persona_id(event)

            record = await self.db_manager.get_favour(persona_id, user_id)
            if record:
                current_favour = record.favour
            else:
                current_favour = await self._get_initial_favour(event)

            relationship = self._get_relationship(current_favour, user_id)

            prompt_final = (
                f'<好感度系统 好感度="{current_favour}" 关系="{relationship}">\n'
                f'    <规则>如人格设定未针对"{relationship}"关系提供指引，请根据该关系做出符合人设的回答。仅作为提示，禁止将具体关系数值输出！</规则>\n'
                f'</好感度系统>'
            )

            req.extra_user_content_parts.append(
                TextPart(text=prompt_final).mark_as_temp()
            )

            logger.debug(f"[Bot:{event.get_platform_id()}] 注入的好感度上下文:\n{prompt_final}")
        except Exception as e:
            logger.error(f"[Bot:{event.get_platform_id()}] 注入好感度上下文失败: {str(e)}\n{traceback.format_exc()}")

    @filter.on_decorating_result(priority=10)
    async def evaluate_favour(self, event: AstrMessageEvent):
        res = event.get_result()
        if not res.is_llm_result():
            return

        user_text = event.message_str
        bot_reply = "".join([comp.text for comp in res.chain if isinstance(comp, Plain)])

        if not bot_reply.strip() or not user_text.strip():
            return

        asyncio.create_task(self._calculate_favour_bg(event, user_text, bot_reply))

    async def _calculate_favour_bg(self, event: AstrMessageEvent, user_text: str, bot_reply: str):
        try:
            user_id = event.get_sender_id()
            umo = event.unified_msg_origin
            persona_id = await self._get_persona_id(event)

            record = await self.db_manager.get_favour(persona_id, user_id)
            current_favour = record.favour if record else await self._get_initial_favour(event)

            # 获取人设 prompt
            persona_prompt = ""
            persona = await self.persona_mgr.get_default_persona_v3(umo)
            if persona and "prompt" in persona:
                persona_prompt = persona["prompt"] or ""

            if self.favour_mode == "galgame":
                mode_rule = (
                    "当前为 GALGAME 模式：偏向正向判定。"
                    "善意互动更易触发上升，轻微失礼宽容处理，好感度较高时上升阈值降低。"
                )
            else:
                mode_rule = (
                    "当前为 REALISTIC 模式：偏向严格判定。"
                    "好感度获取需长期积累，越界和无礼行为加重惩罚，上升阈值较高。"
                )

            persona_section = f"【角色人设】\n{persona_prompt}\n\n" if persona_prompt.strip() else ""

            # 关系规则（advance 模式下提供每段关系的行为规则）
            relationship_rules = ""
            if self.relationship_mode == "advance" and self.relationship_advance_raw:
                try:
                    items = json.loads(self.relationship_advance_raw) if isinstance(self.relationship_advance_raw, str) else self.relationship_advance_raw
                    rules = [f"- {item['describe']} ({item.get('min_value', '')}~{item.get('max_value', '')}): {item.get('rule', '')}" for item in items if item.get('rule')]
                    if rules:
                        relationship_rules = "【关系规则】\n" + "\n".join(rules) + "\n\n"
                except (json.JSONDecodeError, TypeError):
                    pass

            eval_prompt = (
                "你是一个运行在后台的好感度结算处理器。"
                "请根据【角色人设】、【用户发言】和【角色回复】，评估本次互动好感度的增减值。\n\n"
                f"【当前数据】\n当前好感度: {current_favour}\n\n"
                f"{persona_section}"
                f"{relationship_rules}"
                f"【用户发言】\n{user_text}\n\n"
                f"【角色回复】\n{bot_reply}\n\n"
                "【结算规则】\n"
                f"1. 若用户表达善意、赞美、送礼或有重要进展，且角色予以正面反馈，判定为上升（+{self.favour_increase_min} 到 +{self.favour_increase_max}）。\n"
                f"2. 若用户冒犯、恶俗、无理取闹，导致角色生气、冷漠或反感，判定为下降（-{self.favour_decrease_min} 到 -{self.favour_decrease_max}）。\n"
                "3. 若仅为普通交流，或当前好感度已经很高且用户没有特别举动，判定为 0。\n"
                f"4. {mode_rule}\n\n"
                "【格式要求】\n"
                "强制约束：你只能且必须输出一段合法的 JSON，不能包含任何 Markdown 符号或额外文字！\n"
                "范例：{\"change\": 0}\n\n"
                "请直接输出 JSON 结果："
            )

            # 选择裁判模型
            if self.judge_provider:
                provider_id = self.judge_provider.strip()
            else:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)

            if not provider_id:
                logger.warning(f"[Bot:{event.get_platform_id()}] 未找到可用的 LLM Provider 进行好感度结算")
                return

            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=eval_prompt,
            )
            result_text = resp.completion_text

            delta = 0
            json_match = re.search(r'\{.*?\}', result_text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    delta = int(data.get("change", 0))
                except (json.JSONDecodeError, ValueError):
                    logger.warning(f"[Bot:{event.get_platform_id()}] 好感度结算 JSON 解析失败: {result_text}")
            else:
                logger.warning(f"[Bot:{event.get_platform_id()}] 好感度结算未在模型回复中找到 JSON: {result_text}")
                return

            if delta == 0:
                return

            record = await self.db_manager.get_favour(persona_id, user_id)
            old_fav = record.favour if record else await self._get_initial_favour(event)
            new_fav = max(self.min_favour_value, min(self.max_favour_value, old_fav + delta))

            await self.db_manager.update_favour(persona_id, user_id, new_fav)
            logger.info(f"[Bot:{event.get_platform_id()}] 用户 {user_id} 好感度结算: {old_fav}->{new_fav} (Δ{delta})")
        except Exception as e:
            logger.error(f"[Bot:{event.get_platform_id()}] 好感度后台结算出错: {str(e)}\n{traceback.format_exc()}")

    # ================= 1. 查询类型 =================

    @filter.command("查询好感度", alias={'查好感度', '好感度查询', '查看好感度', '好感度'})
    async def query_favour(self, event: AstrMessageEvent, target: str = ""):
        """查询自己或他人的好感度"""
        if not await self._check_command_permission(event, "查询好感度"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        sender_id = event.get_sender_id()
        target_uid = get_target_uid(event, target) or sender_id

        # 查询他人好感度需要满足配置的最低等级
        if target_uid != sender_id:
            required = self._LEVEL_NAME_MAP.get(self.query_others_favour_level, PermLevel.ADMIN)
            user_level = await self._get_user_perm_level(event)
            if user_level < required:
                yield event.plain_result("权限不足！你只能查看自己的好感度。")
                return

        persona_id = await self._get_persona_id(event)
        record = await self.db_manager.get_favour(persona_id, target_uid)
        fav = record.favour if record else (await self._get_initial_favour(event) if target_uid == sender_id else 0)

        name = await get_user_display_name(event, target_uid)

        msg = f"🔍 用户：{name}\n🆔 ID：{target_uid}\n❤ 好感度：{fav}\n🔗 关系：{self._get_relationship(fav, target_uid)}"
        yield event.plain_result(msg)

    @filter.command("查询全局好感度", alias={'全局好感度', '查全局好感度', '查看全局好感度', '全局好感度查询', '查询全部好感度', '查全部好感度', '查看全部好感度', '全部好感度'})
    async def query_global_favour(self, event: AstrMessageEvent, page: int = 1):
        """查询全局好感度 (支持分页)"""
        if not await self._check_command_permission(event, "查询全局好感度"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        persona_id = await self._get_persona_id(event)
        records = await self.db_manager.get_global_records(persona_id)
        if not records:
            yield event.plain_result("暂无好感度记录。")
            return

        records = await self._sort_records(event, records)

        page_size = 20
        total_records = len(records)
        total_pages = (total_records + page_size - 1) // page_size
        if page < 1: page = 1
        if page > total_pages and total_pages > 0: page = total_pages

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_records = records[start_idx:end_idx]

        headers = [
            "| 用户ID | 好感度 | 关系 |",
            "| :--- | :---: | :---: |"
        ]
        rows = []
        for r in page_records:
            rel = escape_markdown(self._get_relationship(r.favour))
            rows.append(f"| {r.user_id} | {r.favour} | {rel} |")

        title = f"📊 好感度记录 - 第 {page}/{total_pages} 页"
        await self._send_chunked_t2i(event, title, headers, rows)

    # ================= 2. 修改类型 =================

    @filter.command("修改好感度")
    async def modify_favour(self, event: AstrMessageEvent, target: str, value: int):
        """修改好感度: /修改好感度 @用户 50"""
        if not await self._check_command_permission(event, "修改好感度"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        uid = get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return

        persona_id = await self._get_persona_id(event)
        try:
            await self.db_manager.update_favour(persona_id, uid, favour=value)
            yield event.plain_result(f"已将用户 {uid} 的好感度修改为 {value}。")
            logger.info(f"[Bot:{event.get_platform_id()}] 管理员 {event.get_sender_id()} 修改用户 {uid} 好感度为 {value}")
        except Exception as e:
            logger.error(f"[Bot:{event.get_platform_id()}] 修改好感度失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    @filter.command("降低好感度", alias={'降低我的好感度'})
    async def decrease_own_favour(self, event: AstrMessageEvent, value: int):
        """降低自己的好感度: /降低好感度 10"""
        if not self.allow_self_decrease:
            yield event.plain_result("该功能未启用。")
            return

        # 有"修改好感度"权限的用户使用 /修改好感度 即可
        if await self._check_command_permission(event, "修改好感度"):
            yield event.plain_result("你拥有修改好感度的权限，请直接使用 /修改好感度。")
            return

        if value <= 0:
            yield event.plain_result("请输入正整数。")
            return

        user_id = event.get_sender_id()
        persona_id = await self._get_persona_id(event)

        try:
            record = await self.db_manager.get_favour(persona_id, user_id)
            old_fav = record.favour if record else await self._get_initial_favour(event)
            new_fav = max(self.min_favour_value, old_fav - value)

            await self.db_manager.update_favour(persona_id, user_id, new_fav)
            yield event.plain_result(f"你的好感度已降低 {value}，当前：{new_fav}。")
            logger.info(f"[Bot:{event.get_platform_id()}] 用户 {user_id} 主动降低好感度: {old_fav}->{new_fav} (Δ{-value})")
        except Exception as e:
            logger.error(f"[Bot:{event.get_platform_id()}] 降低好感度失败: {e}")
            yield event.plain_result("操作失败，请检查日志。")

    # ================= 3. 清空类型 =================

    @filter.command("清空好感度")
    async def clear_user_favour(self, event: AstrMessageEvent, target: str):
        """清空指定用户好感度"""
        if not await self._check_command_permission(event, "清空好感度"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        uid = get_target_uid(event, target)
        persona_id = await self._get_persona_id(event)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return

        yield event.plain_result(f"⚠️ 警告：即将清空用户 {uid} 的好感度数据。\n请在 30 秒内回复「确认清空」以继续，回复其他内容取消。")

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "确认清空":
                record = await self.db_manager.get_favour(persona_id, uid)
                if record:
                    backup_file = await self.db_manager.backup_data([record], f"backup_user_{uid}")
                    await self.db_manager.delete_favour(persona_id, uid)
                    await evt.send(evt.plain_result(f"✅ 已清空用户 {uid} 的好感度数据。"))
                    logger.info(f"[Bot:{event.get_platform_id()}] 管理员 {evt.get_sender_id()} 清空了用户 {uid} 的好感度\n备份文件已保存至: {backup_file}")
                else:
                    await evt.send(evt.plain_result("该用户无好感度记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    @filter.command("清空全局好感度")
    async def clear_all_favour(self, event: AstrMessageEvent):
        """清空所有好感度"""
        if not await self._check_command_permission(event, "清空全局好感度"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        persona_id = await self._get_persona_id(event)

        yield event.plain_result(f"🚨 极度危险：即将清空数据库中【所有】好感度数据！\n请在 30 秒内回复「确认清空所有数据」以继续，回复其他内容取消。")

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "确认清空所有数据":
                records = await self.db_manager.get_global_records(persona_id)
                if records:
                    backup_file = await self.db_manager.backup_data(records, "backup_all_database")
                    await self.db_manager.clear_all(persona_id)
                    await evt.send(evt.plain_result(f"✅ 已清空所有好感度数据。"))
                    logger.warning(f"[Bot:{event.get_platform_id()}] Bot管理员 {evt.get_sender_id()} 清空了所有好感度数据！\n备份文件已保存至: {backup_file}")
                else:
                    await evt.send(evt.plain_result("数据库中无好感度记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    # ================= 4. 帮助类型 =================

    @filter.command("好感度帮助", alias={'查看好感度帮助'})
    async def help_menu(self, event: AstrMessageEvent):
        """显示可用命令菜单"""
        msg = ["⭐ 好感度插件命令菜单 ⭐"]

        # 查询类
        query_cmds = []
        if await self._check_command_permission(event, "查询好感度"):
            required = self._LEVEL_NAME_MAP.get(self.query_others_favour_level, PermLevel.ADMIN)
            user_level = await self._get_user_perm_level(event)
            if user_level >= required:
                query_cmds.append("- 查询好感度 [@用户]")
            else:
                query_cmds.append("- 查询好感度")
        if await self._check_command_permission(event, "查询全局好感度"):
            query_cmds.append("- 查询全局好感度 [页码]")
        if query_cmds:
            msg.append("\n[查询命令]")
            msg.extend(query_cmds)

        # 修改类
        modify_cmds = []
        if await self._check_command_permission(event, "修改好感度"):
            modify_cmds.append("- 修改好感度 @用户 <数值>")
        if self.allow_self_decrease and not await self._check_command_permission(event, "修改好感度"):
            modify_cmds.append("- 降低好感度 <数值>")
        if modify_cmds:
            msg.append("\n[修改命令]")
            msg.extend(modify_cmds)

        # 清空类
        clear_cmds = []
        if await self._check_command_permission(event, "清空好感度"):
            clear_cmds.append("- 清空好感度 @用户")
        if await self._check_command_permission(event, "清空全局好感度"):
            clear_cmds.append("- 清空全局好感度")
        if clear_cmds:
            msg.append("\n[清空命令]")
            msg.extend(clear_cmds)

        if await self._check_command_permission(event, "好感度指令帮助"):
            msg.append("\n- 好感度指令帮助")

        yield event.plain_result("\n".join(msg))

    @filter.command("好感度指令帮助")
    async def help_usage(self, event: AstrMessageEvent):
        """显示详细指令用法"""
        msg_parts = ["⭐ 好感度指令用法示例 ⭐"]
        section = 0

        # 查询类
        has_query = await self._check_command_permission(event, "查询好感度")
        has_query = has_query or await self._check_command_permission(event, "查询全局好感度")
        if has_query:
            section += 1
            msg_parts.append(f"\n{section}. 查询好感度")
            if await self._check_command_permission(event, "查询好感度"):
                required = self._LEVEL_NAME_MAP.get(self.query_others_favour_level, PermLevel.ADMIN)
                user_level = await self._get_user_perm_level(event)
                if user_level >= required:
                    msg_parts.append("   用法: /查询好感度 [@用户]")
                    msg_parts.append("   示例: /查询好感度 @Wolfycz")
                else:
                    msg_parts.append("   用法: /查询好感度")
                    msg_parts.append("   说明: 查看自己的好感度。")
            if await self._check_command_permission(event, "查询全局好感度"):
                msg_parts.append("   用法: /查询全局好感度 [页码]")
                msg_parts.append("   示例: /查询全局好感度 2")

        # 修改好感度
        has_modify = await self._check_command_permission(event, "修改好感度")
        if has_modify:
            section += 1
            msg_parts.append(f"\n{section}. 修改好感度")
            msg_parts.append("   用法: /修改好感度 @用户 <数值>")
            msg_parts.append("   示例: /修改好感度 @Wolfycz 60")

        # 降低好感度
        if self.allow_self_decrease and not has_modify:
            section += 1
            msg_parts.append(f"\n{section}. 降低好感度")
            msg_parts.append("   用法: /降低好感度 <数值>")
            msg_parts.append("   示例: /降低好感度 10")
            msg_parts.append("   说明: 降低自己的好感度，只能输入正整数。")

        # 清空操作
        clear_cmds = []
        if await self._check_command_permission(event, "清空好感度"):
            clear_cmds.append("   用法: /清空好感度 @用户")
        if await self._check_command_permission(event, "清空全局好感度"):
            clear_cmds.append("   用法: /清空全局好感度")
        if clear_cmds:
            section += 1
            msg_parts.append(f"\n{section}. 清空操作")
            msg_parts.extend(clear_cmds)
            msg_parts.append("   说明: 清空操作需要二次确认，并会自动备份数据。")

        yield event.plain_result("\n".join(msg_parts))
