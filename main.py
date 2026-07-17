# -*- coding: utf-8 -*-
"""
astrbot_plugin_onboarding - 棱镜娘新人引导系统 v1.1.0

对齐类脑娘 GuidanceCog 实现：
- 通过 pycord 原生 client.add_listener() 注册 on_member_join 与 on_member_update 事件
- on_member_join：新成员加入服务器时，自动发送入服须知私信，引导确认后脱离缓冲组
- on_member_update：当成员获得指定身份组（如「镜花浮梦」）时，自动发送可爱俏皮的欢迎私信
- 引导新成员前往索引频道
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

        if self.enable_join_notice:
            logger.info(
                f"[Onboarding] 入服提醒已启用，notice_channel_url={self.notice_channel_url}"
                + (f", buffer_role_id={self.buffer_role_id}" if self.buffer_role_id else "")
            )
        else:
            logger.info("[Onboarding] 入服提醒已关闭")

        logger.info(f"[Onboarding] 开始注册 Discord 事件监听")

        # 延迟注册 Discord 原生事件监听
        asyncio.create_task(self._hook_discord())

    async def _hook_discord(self):
        """轮询重试：每 5 秒尝试拿到 DiscordBotClient 并注册事件监听。最多重试 3 次（约 15 秒）。"""
        logger.info(f"[Onboarding] 开始轮询 Discord 客户端，target_role_id={self.target_role_id}")
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

                    client.add_listener(self._on_member_update, "on_member_update")
                    client.add_listener(self._on_member_join, "on_member_join")
                    self._hooked = True
                    self._discord_client = client
                    logger.info(
                        f"[Onboarding] ✅ 已注册 Discord 事件监听："
                        f"on_member_update (target_role_id={self.target_role_id}), "
                        f"on_member_join (enable_join_notice={self.enable_join_notice}), "
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
        """Discord 成员加入服务器事件 —— 发送入服须知私信提醒。"""
        try:
            # 忽略机器人自身
            if member.bot:
                return

            if not self.enable_join_notice:
                return

            # 去重
            if member.id in self._join_sent:
                return

            display = getattr(member, "display_name", None) or getattr(
                member, "name", str(member.id)
            )
            logger.info(
                f"[Onboarding] 检测到新成员 {display} ({member.id}) 加入服务器，发送入服须知私信..."
            )

            message = self.join_notice_message.format(
                user_name=display,
                user_mention=member.mention,
                notice_url=self.notice_channel_url,
                buffer_role_id=self.buffer_role_id,
            )

            try:
                await member.send(message)
                self._join_sent.add(member.id)
                logger.info(
                    f"[Onboarding] 已发送入服须知私信给 {member.id} ({display})"
                )
            except Exception as send_err:
                err_name = type(send_err).__name__
                if "Forbidden" in err_name or "403" in str(send_err):
                    logger.warning(
                        f"<yellow>[Onboarding] 无法发送入服须知私信给 {member.id}（用户关闭了私信）</yellow>"
                    )
                else:
                    logger.error(
                        f"<yellow>[Onboarding] 发送入服须知私信失败 ({member.id}): {send_err}</yellow>"
                    )

        except Exception as e:
            logger.error(f"<yellow>[Onboarding] 处理成员加入事件失败: {e}</yellow>", exc_info=True)

    async def _on_member_update(self, before, after):
        """Discord 成员更新事件 —— 检测身份组变更。"""
        try:
            # 忽略机器人自身
            if before.bot or after.bot:
                return

            logger.debug(
                f"[Onboarding] on_member_update: user={after.id} "
                f"before_roles={[r.id for r in before.roles]} "
                f"after_roles={[r.id for r in after.roles]} "
                f"target={self.target_role_id}"
            )

            before_has = any(r.id == self.target_role_id for r in before.roles)
            after_has = any(r.id == self.target_role_id for r in after.roles)

            # 只在「新获得」角色时触发 + 去重
            if before_has or not after_has:
                return
            if after.id in self._sent:
                return  # 已发送过，跳过

            # 互斥保护：如果缓冲组 ID 与目标身份组相同，且用户刚加入服务器
            # （已通过 on_member_join 收到入服须知私信），说明这只是自动分配缓冲组，
            # 并非真正的"毕业"，跳过欢迎私信，避免短时间内连发两条消息
            if (self.buffer_role_id
                    and str(self.target_role_id) == self.buffer_role_id
                    and after.id in self._join_sent):
                logger.info(
                    f"[Onboarding] {display} 获得身份组但已在入服提醒中通知过"
                    "（缓冲组与目标身份组相同），跳过欢迎私信"
                )
                return

            display = getattr(after, "display_name", None) or getattr(
                after, "name", str(after.id)
            )
            logger.info(
                f"[Onboarding] {display} 获得身份组，发送欢迎私信..."
            )

            message = self.welcome_message.format(
                user_name=display,
                user_mention=after.mention,
                index_url=self.index_channel_url,
            )

            try:
                await after.send(message)
                self._sent.add(after.id)
                logger.info(f"[Onboarding] 已发送欢迎私信给 {after.id} ({display})")
            except Exception as send_err:
                err_name = type(send_err).__name__
                if "Forbidden" in err_name or "403" in str(send_err):
                    logger.warning(
                        f"<yellow>[Onboarding] 无法发送私信给 {after.id}（用户关闭了私信）</yellow>"
                    )
                else:
                    logger.error(
                        f"<yellow>[Onboarding] 发送欢迎私信失败 ({after.id}): {send_err}</yellow>"
                    )

        except Exception as e:
            logger.error(f"<yellow>[Onboarding] 处理成员更新事件失败: {e}</yellow>", exc_info=True)

    @filter.command("onboarding")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看新人引导系统状态"""
        content = (
            f"## 🧭 {self.bot_name} 新人引导系统\n\n"
            f"**监听身份组 ID**: `{self.target_role_id}`\n"
            f"**索引频道**: {self.index_channel_url}\n\n"
            f"**入服提醒**: {'✅ 已启用' if self.enable_join_notice else '❌ 已关闭'}\n"
            f"**入服须知频道**: {self.notice_channel_url}\n"
            f"**缓冲组 ID**: `{self.buffer_role_id or '（未配置）'}`\n\n"
            f"**Discord 回调已注册**: {'✅ 是' if self._hooked else '❌ 否（可能 Discord 未连接）'}\n\n"
            "当成员加入服务器时，会自动发送入服须知私信～\n"
            "当成员获得指定身份组时，会自动发送欢迎私信～"
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
