from __future__ import annotations

import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import ContextTypes
from loguru import logger

from app.cache import RuleDTO
from app.crud import ensure_group, list_enabled_rules
from app.matching import match_rule


async def _delete_later(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay: int,
) -> None:
    """延时删除某条机器人消息。"""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # 可能已被手动删除或没权限，忽略即可
        pass


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not user or user.is_bot:
        return
    if not msg.text:
        return
    if msg.text.startswith("/"):
        return

    group_id = chat.id

    # load group enabled & rules (cached)
    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]
    throttle = context.application.bot_data["throttle"]

    cached = cache.get_if_fresh(group_id)
    if cached is None:
        async with db.session() as session:
            # ensure group row exists
            await ensure_group(session, group_id=group_id, title=chat.title)
            rules = await list_enabled_rules(session, group_id=group_id)
            await session.commit()

        dtos: list[RuleDTO] = [
            RuleDTO(
                id=r.id,
                match_type=r.match_type,
                pattern=r.pattern,
                reply=r.reply,
                priority=r.priority,
                enabled=r.enabled,
                delete_after=r.delete_after,
            )
            for r in rules
        ]
        cache.set(group_id, dtos)
        cached = dtos

    text = msg.text
    for r in cached:
        try:
            if match_rule(text, r):
                if throttle.allow(group_id, r.id):
                    # 带“好的”按钮的回复
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "✅ 好的",
                                    callback_data=f"rule_reply_ok:{user.id}",
                                )
                            ]
                        ]
                    )
                    sent = await msg.reply_text(r.reply, reply_markup=keyboard)

                    # 按规则自动删除机器人回复
                    if r.delete_after and r.delete_after > 0:
                        try:
                            context.application.create_task(
                                _delete_later(
                                    context,
                                    chat.id,
                                    sent.message_id,
                                    r.delete_after,
                                )
                            )
                        except Exception as e:
                            logger.warning(f"schedule auto delete failed: {e}")
                return
        except Exception as e:
            logger.warning(f"Match loop error rule={r.id}: {e}")
            continue
