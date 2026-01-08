from __future__ import annotations

import asyncio
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import TimedOut, BadRequest
from loguru import logger

from app.crud import ensure_group, list_rules, create_rule, delete_rule_by_id, add_audit


# Conversation states
CHOOSE_MATCH, INPUT_PATTERN, INPUT_REPLY, CONFIRM = range(4)


MATCH_BUTTONS = [
    [
        InlineKeyboardButton("精确 exact", callback_data="add_match_exact"),
        InlineKeyboardButton("包含 contains", callback_data="add_match_contains"),
    ],
    [
        InlineKeyboardButton("正则 regex", callback_data="add_match_regex"),
        InlineKeyboardButton("模糊 fuzzy", callback_data="add_match_fuzzy"),
    ],
]


def _menu_kb(context: ContextTypes.DEFAULT_TYPE | None = None) -> InlineKeyboardMarkup:
    gid = None
    if context is not None:
        gid = context.user_data.get("manage_group_id")
    title = f"📌 当前群: {gid}" if gid else "📌 未选择群"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ 新增规则", callback_data="menu_add")],
            [InlineKeyboardButton("📄 查看规则", callback_data="menu_list")],
            [InlineKeyboardButton("🔄 切换群", callback_data="menu_switch")],
            [InlineKeyboardButton(title, callback_data="menu_noop")],
        ]
    )


def _truncate_one_line(text: str, max_len: int = 50) -> str:
    s = (text or "").replace("\r", "").replace("\n", " ⏎ ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _remember_group(context: ContextTypes.DEFAULT_TYPE, group_id: int, max_keep: int = 10) -> None:
    """记录该用户最近管理过的群，便于“切换群”菜单使用。"""
    recent: List[int] = context.user_data.get("recent_group_ids", [])
    # 去重 + 把当前放到最前面
    recent = [gid for gid in recent if gid != group_id]
    recent.insert(0, group_id)
    context.user_data["recent_group_ids"] = recent[:max_keep]


async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """管理员检查：加重试，降低 Telegram 偶发 Timed out 的影响。"""
    for attempt in range(3):
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            status = str(member.status).lower()
            return status in ("administrator", "creator")
        except TimedOut as e:
            if attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            logger.warning(f"Admin check timed out: chat_id={chat_id}, user_id={user_id}, err={e}")
            return False
        except Exception as e:
            logger.warning(f"Admin check failed: chat_id={chat_id}, user_id={user_id}, err={e}")
            return False
    return False


async def rule_entry_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """In group: admin runs /rule. Bot replies with deep-link to private chat management."""
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("请在群组里使用 /rule，然后点击按钮去私聊配置。")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _is_admin(context, chat_id, user_id):
        await update.message.reply_text("只有群管理员可以配置关键词回复。")
        return

    me = await context.bot.get_me()
    if not me.username:
        await update.message.reply_text("Bot 没有用户名，无法生成私聊管理链接（请在 BotFather 设置 username）。")
        return

    deep_link = f"https://t.me/{me.username}?start=manage_{chat_id}"

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔧 去私聊管理本群规则", url=deep_link)],
            [InlineKeyboardButton("✅ 好的（删除这条提示）", callback_data="rule_ok")],
        ]
    )

    await update.message.reply_text(
        "点击按钮在私聊里管理本群的关键词规则（不会在群里刷屏）。",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def rule_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """群里 /rule 的提示消息：管理员点“好的”就删掉这条 bot 消息。"""
    q = update.callback_query
    if not q or not q.message or not q.from_user:
        return
    await q.answer()

    chat = q.message.chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await _is_admin(context, chat.id, q.from_user.id):
        await q.answer("仅群管理员可执行。", show_alert=True)
        return

    try:
        await q.message.delete()
    except BadRequest:
        # 删除失败就去掉按钮，避免重复点
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def start_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private /start handler. Supports /start manage_<group_id>."""
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE or not update.message:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "你好！\n\n"
            "请先在目标群组里使用 /rule（管理员），然后点击按钮进入该群的管理界面。"
        )
        return

    token = args[0].strip()
    if token.startswith("manage_"):
        try:
            group_id = int(token.split("_", 1)[1])
        except Exception:
            await update.message.reply_text("参数格式不正确，请回到群里重新点一次按钮。")
            return

        user_id = update.effective_user.id if update.effective_user else 0
        if not await _is_admin(context, group_id, user_id):
            await update.message.reply_text("你不是该群管理员，无法管理该群规则。")
            return

        current_gid = context.user_data.get("manage_group_id")
        if current_gid and current_gid != group_id:
            context.user_data["pending_manage_group_id"] = group_id
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ 切换", callback_data="switch_yes"),
                        InlineKeyboardButton("❌ 不切换", callback_data="switch_no"),
                    ]
                ]
            )
            await update.message.reply_text(
                f"你当前正在管理群 {current_gid}。\n是否切换到群 {group_id}？",
                reply_markup=kb,
            )
            return

        context.user_data["manage_group_id"] = group_id
        _remember_group(context, group_id)

        await update.message.reply_text(
            f"已进入群 {group_id} 的规则管理：",
            reply_markup=_menu_kb(context),
        )
        return

    await update.message.reply_text("无法识别的 /start 参数。请回到群里用 /rule 进入。")


async def switch_manage_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理：是否切换当前管理群（同一管理员点了另一个群的 manage 链接）"""
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    current_gid = context.user_data.get("manage_group_id")
    pending_gid = context.user_data.get("pending_manage_group_id")

    if data == "switch_no":
        context.user_data.pop("pending_manage_group_id", None)
        await q.edit_message_text(f"保持管理群 {current_gid}：", reply_markup=_menu_kb(context))
        return

    if data == "switch_yes":
        if not pending_gid:
            await q.edit_message_text("没有待切换的群。", reply_markup=_menu_kb(context))
            return

        user_id = q.from_user.id
        if not await _is_admin(context, pending_gid, user_id):
            context.user_data.pop("pending_manage_group_id", None)
            await q.edit_message_text("你不是该群管理员，无法切换。", reply_markup=_menu_kb(context))
            return

        context.user_data["manage_group_id"] = pending_gid
        context.user_data.pop("pending_manage_group_id", None)
        _remember_group(context, pending_gid)

        await q.edit_message_text(f"已切换到群 {pending_gid} 的规则管理：", reply_markup=_menu_kb(context))
        return


async def show_switch_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """菜单：列出最近管理过的群，点击即可切换"""
    q = update.callback_query
    if q:
        await q.answer()

    recent: List[int] = context.user_data.get("recent_group_ids", [])
    current = context.user_data.get("manage_group_id")

    if not recent:
        text = "你还没有管理过任何群。\n\n请先在群里输入 /rule，再从私聊进入。"
        if q:
            await q.edit_message_text(text, reply_markup=_menu_kb(context))
        else:
            await update.effective_message.reply_text(text, reply_markup=_menu_kb(context))
        return

    buttons = []
    for gid in recent[:10]:
        prefix = "✅ " if gid == current else ""
        buttons.append([InlineKeyboardButton(f"{prefix}切换到 {gid}", callback_data=f"switch_to_{gid}")])

    buttons.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu_back")])

    kb = InlineKeyboardMarkup(buttons)
    text = "选择要切换管理的群："
    if q:
        await q.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Routes menu callbacks."""
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    data = q.data or ""

    if data == "menu_noop":
        return ConversationHandler.END

    if data == "menu_add":
        return await add_start(update, context)

    if data == "menu_list":
        await show_rules(update, context)
        return ConversationHandler.END

    if data == "menu_switch":
        await show_switch_menu(update, context)
        return ConversationHandler.END

    if data == "menu_back":
        await menu_back(update, context)
        return ConversationHandler.END

    # ✅ 切换群：switch_to_<group_id>
    if data.startswith("switch_to_"):
        try:
            gid = int(data.split("_", 2)[2])
        except Exception:
            await q.edit_message_text("切换参数错误。", reply_markup=_menu_kb(context))
            return ConversationHandler.END

        user_id = q.from_user.id
        if not await _is_admin(context, gid, user_id):
            await q.edit_message_text("你不是该群管理员，无法切换到该群。", reply_markup=_menu_kb(context))
            return ConversationHandler.END

        context.user_data["manage_group_id"] = gid
        _remember_group(context, gid)
        await q.edit_message_text(f"已切换到群 {gid}：", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    await q.edit_message_text("未知操作。", reply_markup=_menu_kb(context))
    return ConversationHandler.END


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("请先在群里输入 /rule 并通过按钮进入对应群的管理界面。")
        return ConversationHandler.END

    await q.edit_message_text(
        "请选择匹配模式：",
        reply_markup=InlineKeyboardMarkup(MATCH_BUTTONS),
    )
    return CHOOSE_MATCH


async def choose_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    data = q.data or ""
    if not data.startswith("add_match_"):
        return ConversationHandler.END

    match_type = data.replace("add_match_", "", 1)
    context.user_data["add_match_type"] = match_type

    await q.edit_message_text(
        f"已选择模式：{match_type}\n\n"
        "请发送关键词/规则内容（下一条消息）："
    )
    return INPUT_PATTERN


async def input_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("请发送文本作为关键词/规则内容。")
        return INPUT_PATTERN

    pattern = update.message.text.strip()
    if len(pattern) > 2000:
        await update.message.reply_text("关键词/规则太长了（>2000）。请缩短后再发。")
        return INPUT_PATTERN

    context.user_data["add_pattern"] = pattern
    await update.message.reply_text("好的。请发送要回复的内容（可多行）：")
    return INPUT_REPLY


async def input_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or update.message.text is None:
        await update.effective_message.reply_text("请发送文本作为回复内容。")
        return INPUT_REPLY

    reply = update.message.text
    if len(reply) > 8000:
        await update.message.reply_text("回复内容太长了（>8000）。请缩短后再发。")
        return INPUT_REPLY

    context.user_data["add_reply"] = reply

    match_type = context.user_data.get("add_match_type")
    pattern = context.user_data.get("add_pattern")
    preview = (
        f"将创建规则：\n"
        f"- 模式: {match_type}\n"
        f"- 关键词/规则: {pattern}\n"
        f"- 回复: \n{reply}\n\n"
        f"确认保存吗？"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 保存", callback_data="add_confirm_save")],
            [InlineKeyboardButton("❌ 取消", callback_data="add_confirm_cancel")],
        ]
    )
    await update.message.reply_text(preview, reply_markup=kb)
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    if q.data == "add_confirm_cancel":
        context.user_data.pop("add_match_type", None)
        context.user_data.pop("add_pattern", None)
        context.user_data.pop("add_reply", None)
        await q.edit_message_text("已取消。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    if q.data != "add_confirm_save":
        return ConversationHandler.END

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("管理群信息丢失，请回到群里重新 /rule 进入。")
        return ConversationHandler.END

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法保存规则。")
        return ConversationHandler.END

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    match_type = context.user_data.get("add_match_type")
    pattern = context.user_data.get("add_pattern")
    reply = context.user_data.get("add_reply")

    try:
        async with db.session() as session:
            await ensure_group(session, group_id=group_id)
            rule = await create_rule(
                session,
                group_id=group_id,
                match_type=match_type,
                pattern=pattern,
                reply=reply,
                created_by=user_id,
                priority=100,
                enabled=True,
            )
            await add_audit(
                session,
                group_id=group_id,
                actor_user_id=user_id,
                action="create",
                before_json=None,
                after_json={
                    "id": rule.id,
                    "match_type": rule.match_type,
                    "pattern": rule.pattern,
                    "reply": rule.reply,
                    "priority": rule.priority,
                    "enabled": rule.enabled,
                },
            )
            await session.commit()
    except Exception as e:
        logger.exception(e)
        await q.edit_message_text(f"保存失败：{e}")
        return ConversationHandler.END

    cache.invalidate(group_id)

    context.user_data.pop("add_match_type", None)
    context.user_data.pop("add_pattern", None)
    context.user_data.pop("add_reply", None)

    await q.edit_message_text("✅ 已保存规则。", reply_markup=_menu_kb(context))
    return ConversationHandler.END


async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        msg = "请先在群里 /rule 进入对应群管理。"
        if q:
            await q.edit_message_text(msg, reply_markup=_menu_kb(context))
        else:
            await update.effective_message.reply_text(msg, reply_markup=_menu_kb(context))
        return

    db = context.application.bot_data["db"]
    async with db.session() as session:
        rules = await list_rules(session, group_id=group_id, limit=20, offset=0)

    if not rules:
        text = "当前群还没有任何规则。"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ 新增规则", callback_data="menu_add")]])
        if q:
            await q.edit_message_text(text, reply_markup=kb)
        else:
            await update.effective_message.reply_text(text, reply_markup=kb)
        return

    lines = []
    buttons = []
    for r in rules:
        status = "✅" if r.enabled else "⛔"
        reply_preview = _truncate_one_line(r.reply, max_len=50)
        lines.append(
            f"{status} #{r.id} [{r.match_type}] p={r.priority} :: {r.pattern}\n"
            f"    ↳ 回复: {reply_preview}"
        )
        buttons.append([InlineKeyboardButton(f"删除 #{r.id}", callback_data=f"del_{r.id}")])

    text = "规则列表（前 20 条）：\n\n" + "\n\n".join(lines)
    kb = InlineKeyboardMarkup(buttons + [[InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu_back")]])
    if q:
        await q.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def delete_rule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("请先在群里 /rule 进入对应群管理。", reply_markup=_menu_kb(context))
        return

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法删除规则。", reply_markup=_menu_kb(context))
        return

    try:
        rule_id = int((q.data or "").split("_", 1)[1])
    except Exception:
        await q.edit_message_text("删除参数错误。", reply_markup=_menu_kb(context))
        return

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    async with db.session() as session:
        ok = await delete_rule_by_id(session, group_id=group_id, rule_id=rule_id)
        await add_audit(
            session,
            group_id=group_id,
            actor_user_id=user_id,
            action="delete",
            before_json={"id": rule_id},
            after_json=None,
        )
        await session.commit()

    if ok:
        cache.invalidate(group_id)
        await q.edit_message_text(f"✅ 已删除规则 #{rule_id}。", reply_markup=_menu_kb(context))
    else:
        await q.edit_message_text(f"未找到规则 #{rule_id}（可能已删除）。", reply_markup=_menu_kb(context))


async def menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    await q.edit_message_text("规则管理菜单：", reply_markup=_menu_kb(context))
