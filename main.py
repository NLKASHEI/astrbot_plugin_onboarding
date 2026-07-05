# -*- coding: utf-8 -*-
"""
astrbot_plugin_onboarding - 棱镜娘新人引导系统 v1.0

对齐类脑娘 GuidanceCog 实现：
- 通过 pycord 原生 client.add_listener() 注册 on_member_update 事件
- 当成员获得指定身份组（如「镜花浮梦」）时，自动发送可爱俏皮的欢迎私信
- 引导新成员前往索引频道
- 完全不需要修改 AstrBot 适配器代码

依赖：AstrBot 已配置 Discord 适配器（pycord），intents.members 已启用。
"""

import asyncio
import logging

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig

# ---------- 默认配置 ----------

DEFAULT_WELCOME = (
    "嗨嗨～欢迎来到 Prism 棱镜社区！✨\n\n"
    "我是棱镜娘，大家都叫我宝宝～以后就是一家人啦！\n\n"
    "社区里有好多有意思的东西，角色卡、AI 绘图、各种好玩的预设……"
    "怕你第一次来转晕了，我帮你指个路：\n\n"
    "👉 [点我去索引频道！]({index_url})\n\n"
    "那里有社区的完整导航，想找什么都能找到～有什么不懂的随时问我嗷！"
)

DEFAULT_INDEX_URL = (
    "https://discord.com/channels/1461731450058575986/1522145315115892827"
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
        self._hooked = False
        self._discord_client = None
        self._sent = set()  # 已发送过欢迎 DM 的 user_id，防止重复

        if self.target_role_id == 0:
            logger.warning(
                "[Onboarding] target_role_id 未配置，新人引导不会触发。"
                "请在 WebUI 插件配置中填写 Discord 身份组 ID。"
            )
            return

        # 延迟注册 Discord 原生事件监听
        asyncio.create_task(self._hook_discord())

    async def _hook_discord(self):
        """轮询重试：每 5 秒尝试拿到 DiscordBotClient 并注册 on_member_update。"""
        while not self._hooked:
            await asyncio.sleep(5)
            try:
                platforms = self.context.platform_manager.get_insts()
                for platform in platforms:
                    client = getattr(platform, "client", None)
                    if client is None:
                        continue
                    if not hasattr(client, "add_listener"):
                        continue

                    client.add_listener(self._on_member_update, "member_update")
                    self._hooked = True
                    self._discord_client = client
                    logger.info(
                        "[Onboarding] 已注册 Discord on_member_update 监听，"
                        f"监听身份组 ID={self.target_role_id}"
                    )
                    return
            except Exception as e:
                logger.warning(f"[Onboarding] 注册 Discord 事件失败，5秒后重试: {e}")

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
                        f"[Onboarding] 无法发送私信给 {after.id}（用户关闭了私信）"
                    )
                else:
                    logger.error(
                        f"[Onboarding] 发送欢迎私信失败 ({after.id}): {send_err}"
                    )

        except Exception as e:
            logger.error(f"[Onboarding] 处理成员更新事件失败: {e}", exc_info=True)

    @filter.command("onboarding")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看新人引导系统状态"""
        content = (
            f"## 🧭 {self.bot_name} 新人引导系统\n\n"
            f"**监听身份组 ID**: `{self.target_role_id}`\n"
            f"**索引频道**: {self.index_channel_url}\n"
            f"**Discord 回调已注册**: {'✅ 是' if self._hooked else '❌ 否（可能 Discord 未连接）'}\n\n"
            "当成员获得指定身份组时，会自动发送欢迎私信～"
        )
        if getattr(event, 'interaction_followup_webhook', None):
            await event.interaction_followup_webhook.send(content, ephemeral=True)
            return
        yield event.plain_result(content)
