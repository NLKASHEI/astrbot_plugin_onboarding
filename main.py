# -*- coding: utf-8 -*-
"""
astrbot_plugin_onboarding - 棱镜娘新人引导系统 v1.2.0

对齐类脑娘 GuidanceCog 实现：
- 通过 pycord 原生 client.add_listener() 注册 on_member_join 与 on_member_update 事件
- 阶段一（on_member_update）：成员获得缓冲组时，自动发送入服须知私信，引导确认后脱离缓冲组
- 阶段二（on_member_update）：成员获得正式身份组（如「镜花浮梦」）时，自动发送欢迎私信引导前往索引频道
- on_member_join 仅记录日志，不发送私信（避免加入和获得缓冲组之间的时间差导致重复）
- 完全不需要修改 AstrBot 适配器代码

依赖：AstrBot 已配置 Discord 适配器（pycord），intents.members 已启用。
"""

import asyncio

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig


def _parse_bool(value, default=False):
    """兼容 bool / str / None 类型的布尔值解析，避免 bool('false') 为 True 的陷阱。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return bool(value)

# ---------- 默认配置 ----------

DEFAULT_WELCOME = (
    "嗨嗨～欢迎来到 Prism 棱镜社区！✨\n\n"
    "我是棱镜娘，大家都叫我宝宝～以后就是一家人啦！\n\n"
    "社区里有好多有意思的东西，角色卡、AI 绘图、各种好玩的预设…… "
    "怕你第一次来转晕了，我帮你指个路：\n\n"
    "👉 [点我去索引频道！]({index_url})\n\n"
    "那里有社区的完整导航，想找什么都能找到～有什么不懂的随时问我嗷！"
)

DEFAULT_INDEX_URL = (
    "https://discord.com/channels/1461731450058575986/1522145315115892827"
)

DEFAULT_NOTICE_URL = (
    "https://discord.com/channels/1461731450058575986/1521138419131088916"
)

DEFAULT_JOIN_NOTICE = (
    "哈喽哈喽～欢迎新人宝宝来到 Prism 棱镜社区！✨\n\n"
    "我是棱镜娘，大家都叫我宝宝～先别急着到处跑嗷，"
    "有一件超重要的事情要你先做：\n\n"
    "👉 [点我去看看入服须知]({notice_url})\n\n"
    "认真读完入服须知并确认之后，就能脱离缓冲组，"
    "解锁社区的完整功能啦～角色卡、AI 绘图、各种好玩的预设都在等你嗷！\n\n"
    "有什么不懂的随时戳我，宝宝一直都在～"
)


class OnboardingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)

        cfg = config or {}
        try:
            self.target_role_id = int(cfg.get("target_role_id", "0") or "0")
        except (ValueError, TypeError):
            self.target_role_id = 0
        self.index_channel_url = str(
            cfg.get("index_channel_url", "") or DEFAULT_INDEX_URL
        )
        self.welcome_message = str(
            cfg.get("welcome_message", "") or DEFAULT_WELCOME
        )
        self.bot_name = str(cfg.get("bot_name", "") or "棱镜娘")
        self.notice_channel_url = str(
            cfg.get("notice_channel_url", "") or DEFAULT_NOTICE_URL
        )
        self.join_notice_message = str(
            cfg.get("join_notice_message", "") or DEFAULT_JOIN_NOTICE
        )
        self.enable_join_notice = _parse_bool(cfg.get("enable_join_notice", True), default=True)
        self.buffer_role_id = str(cfg.get("buffer_role_id", "") or "")
        try:
            self._buffer_role_id_int = int(self.buffer_role_id) if self.buffer_role_id else 0
        except (ValueError, TypeError):
            self._buffer_role_id_int = 0
        self._hooked = False
        self._discord_client = None
        self._sent = set()  # 已发送过欢迎 DM 的 user_id，防止重复
        self._join_sent = set()  # 已发送过入服提醒 DM 的 user_id，防止重复

        if self.target_role_id == 0:
            logger.warning(
                "<yellow>[Onboarding] target_role_id 未配置，身份组欢迎私信不会触发。"
                "请在 WebUI 插件配置中填写 Discord 身份组 ID。</yellow>"
            )
        else:
            logger.info(f"[Onboarding] 插件初始化完成，target_role_id={self.target_role_id}")

        if self.enable_join_notice and self._buffer_role_id_int:
            logger.info(
                f"[Onboarding] 入服提醒已启用 — 获得缓冲组 {self._buffer_role_id_int} 时发送【入服须知】"
                f" → {self.notice_channel_url}"
            )
        elif not self.enable_join_notice:
            logger.info("[Onboarding] 入服提醒已关闭")
        elif not self._buffer_role_id_int:
            logger.warning(
                "<yellow>[Onboarding] 入服提醒已启用但 buffer_role_id 未配置，"
                "阶段一（入服须知）不会触发。请在 WebUI 插件配置中填写缓冲组 ID。</yellow>"
            )

        logger.info(f"[Onboarding] 开始注册 Discord 事件监听")

        # 延迟注册 Discord 原生事件监听
        asyncio.create_task(self._hook_discord())

    async def _hook_discord(self):
        """轮询重试：每 5 秒尝试拿到 DiscordBotClient 并注册事件监听。最多重试 3 次（约 15 秒）。"""
        logger.info(f"[Onboarding] 开始轮询 Discord 客户端（阶段一 buffer={self._buffer_role_id_int} 阶段二 target={self.target_role_id}）")
        retry = 0
        max_retry = 3
        while not self._hooked and retry < max_retry:
            retry += 1
            await asyncio.sleep(5)
            try:
                platforms = self.context.platform_manager.get_insts()
                logger.info(f"[Onboarding] 第{retry}次尝试，找到 {len(platforms)} 个平台实例")
                for platform in platforms:
                    client = getattr(platform, "client", None)
                    if client is None:
                        continue
                    if not hasattr(client, "add_listener"):
                        continue

                    # 先移除旧监听器防止重载残留导致重复注册
                    try:
                        client.remove_listener(self._on_member_update)
                    except Exception:
                        pass
                    try:
                        client.remove_listener(self._on_member_join)
                    except Exception:
                        pass

                    client.add_listener(self._on_member_update, "on_member_update")
                    client.add_listener(self._on_member_join, "on_member_join")
                    self._hooked = True
                    self._discord_client = client
                    logger.info(
                        f"[Onboarding] ✅ 已注册 Discord 事件监听："
                        f"on_member_join（仅记录日志），"
                        f"on_member_update 阶段一 buffer={self._buffer_role_id_int} → 入服须知，"
                        f"阶段二 target={self.target_role_id} → 欢迎/索引，"
                        f"intents.members={getattr(client, 'intents', None) and client.intents.members}"
                    )
                    return
            except Exception as e:
                logger.warning(f"<yellow>[Onboarding] 第{retry}次注册失败: {e}</yellow>")
        if not self._hooked:
            logger.error(
                f"<yellow>[Onboarding] ❌ 已重试{max_retry}次仍无法注册 Discord 事件，"
                "新人引导功能不会触发。请检查 Discord 适配器是否正常连接。</yellow>"
            )

    async def _on_member_join(self, member):
        """Discord 成员加入服务器事件 —— 仅记录日志，入服须知由获得缓冲组时触发。"""
        try:
            if member.bot:
                return

            display = getattr(member, "display_name", None) or getattr(
                member, "name", str(member.id)
            )
            logger.info(
                f"[Onboarding] 新成员 {display} ({member.id}) 加入服务器，"
                f"等待获得缓冲组 (buffer_role_id={self._buffer_role_id_int}) 后发送入服须知"
            )

        except Exception as e:
            logger.error(f"<yellow>[Onboarding] 处理成员加入事件失败: {e}</yellow>", exc_info=True)

    async def _on_member_update(self, before, after):
        """Discord 成员更新事件 —— 两阶段引导：
        阶段一：获得缓冲组 → 发送入服须知
        阶段二：获得正式身份组 → 发送欢迎/索引
        """
        try:
            # 忽略机器人自身
            if before.bot or after.bot:
                return

            display = getattr(after, "display_name", None) or getattr(
                after, "name", str(after.id)
            )

            logger.debug(
                f"[Onboarding] on_member_update: user={after.id} "
                f"before_roles={[r.id for r in before.roles]} "
                f"after_roles={[r.id for r in after.roles]} "
                f"buffer={self._buffer_role_id_int} target={self.target_role_id}"
            )

            # ---- 阶段一：缓冲组 → 入服须知 ----
            if self.enable_join_notice and self._buffer_role_id_int:
                before_has_buffer = any(
                    r.id == self._buffer_role_id_int for r in before.roles
                )
                after_has_buffer = any(
                    r.id == self._buffer_role_id_int for r in after.roles
                )

                if not before_has_buffer and after_has_buffer:
                    # 新获得缓冲组
                    if after.id not in self._join_sent:
                        logger.info(
                            f"[Onboarding] {display} 获得缓冲组 {self._buffer_role_id_int}，"
                            f"准备发送【入服须知】私信 → {self.notice_channel_url}"
                        )
                        notice_msg = self.join_notice_message.format(
                            user_name=display,
                            user_mention=after.mention,
                            notice_url=self.notice_channel_url,
                            buffer_role_id=self.buffer_role_id,
                        )
                        try:
                            await after.send(notice_msg)
                            self._join_sent.add(after.id)
                            logger.info(
                                f"[Onboarding] ✅ 已发送【入服须知】私信给 {after.id} ({display})"
                            )
                        except Exception as send_err:
                            err_name = type(send_err).__name__
                            if "Forbidden" in err_name or "403" in str(send_err):
                                logger.warning(
                                    f"<yellow>[Onboarding] 无法发送【入服须知】私信给 {after.id}"
                                    "（用户关闭了私信）</yellow>"
                                )
                            else:
                                logger.error(
                                    f"<yellow>[Onboarding] 发送【入服须知】私信失败"
                                    f" ({after.id}): {send_err}</yellow>"
                                )

                    # 如果缓冲组与正式身份组相同，阶段一已处理，跳过阶段二
                    if self._buffer_role_id_int == self.target_role_id:
                        logger.info(
                            f"[Onboarding] ⏭️ {display} 缓冲组与正式身份组相同"
                            f" ({self._buffer_role_id_int})，已通过阶段一发送【入服须知】，"
                            "跳过阶段二【欢迎/索引】"
                        )
                        return

            # ---- 阶段二：正式身份组 → 欢迎/索引 ----
            if self.target_role_id:
                before_has_target = any(
                    r.id == self.target_role_id for r in before.roles
                )
                after_has_target = any(
                    r.id == self.target_role_id for r in after.roles
                )

                if not before_has_target and after_has_target:
                    if after.id in self._sent:
                        return  # 已发送过欢迎私信

                    logger.info(
                        f"[Onboarding] {display} 获得正式身份组 {self.target_role_id}，"
                        f"准备发送【欢迎/索引】私信 → {self.index_channel_url}"
                    )
                    welcome_msg = self.welcome_message.format(
                        user_name=display,
                        user_mention=after.mention,
                        index_url=self.index_channel_url,
                    )
                    try:
                        await after.send(welcome_msg)
                        self._sent.add(after.id)
                        logger.info(
                            f"[Onboarding] ✅ 已发送【欢迎/索引】私信给 {after.id} ({display})，"
                            f"引导前往 {self.index_channel_url}"
                        )
                    except Exception as send_err:
                        err_name = type(send_err).__name__
                        if "Forbidden" in err_name or "403" in str(send_err):
                            logger.warning(
                                f"<yellow>[Onboarding] 无法发送【欢迎/索引】私信给 {after.id}"
                                "（用户关闭了私信）</yellow>"
                            )
                        else:
                            logger.error(
                                f"<yellow>[Onboarding] 发送【欢迎/索引】私信失败"
                                f" ({after.id}): {send_err}</yellow>"
                            )

        except Exception as e:
            logger.error(f"<yellow>[Onboarding] 处理成员更新事件失败: {e}</yellow>", exc_info=True)

    @filter.command("onboarding")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看新人引导系统状态"""
        content = (
            f"## 🧭 {self.bot_name} 新人引导系统\n\n"
            f"### 阶段一：入服须知\n"
            f"**缓冲组 ID**: `{self._buffer_role_id_int or '（未配置）'}`\n"
            f"**入服须知频道**: {self.notice_channel_url}\n"
            f"**状态**: {'✅ 已启用' if self.enable_join_notice and self._buffer_role_id_int else '❌ 未就绪'}\n\n"
            f"### 阶段二：欢迎引导\n"
            f"**正式身份组 ID**: `{self.target_role_id}`\n"
            f"**索引频道**: {self.index_channel_url}\n"
            f"**状态**: {'✅ 已启用' if self.target_role_id else '❌ 未配置'}\n\n"
            f"**Discord 回调已注册**: {'✅ 是' if self._hooked else '❌ 否（可能 Discord 未连接）'}\n\n"
            "获得缓冲组 → 自动发送入服须知私信\n"
            "获得正式身份组 → 自动发送欢迎/索引私信"
        )
        if getattr(event, 'interaction_followup_webhook', None):
            await event.interaction_followup_webhook.send(content, ephemeral=True)
            return
        yield event.plain_result(content)

    async def terminate(self):
        """插件卸载/重载时清理 Discord 事件监听器，避免重载后重复触发。"""
        if self._discord_client and hasattr(self._discord_client, "remove_listener"):
            try:
                self._discord_client.remove_listener(self._on_member_update)
            except (ValueError, Exception):
                pass
            try:
                self._discord_client.remove_listener(self._on_member_join)
            except (ValueError, Exception):
                pass
            logger.info("[Onboarding] 已移除 Discord 事件监听器，插件已卸载")
        self._hooked = False
