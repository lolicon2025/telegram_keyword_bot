from __future__ import annotations

import asyncio
from typing import List, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import TimedOut, BadRequest
from loguru import logger

from app.crud import (
    ensure_group,
    list_rules,
    create_rule,
    delete_rule_by_id,
    add_audit,
    get_rule,
)

# 会话状态
CHOOSE_MATCH, INPUT_PATTERN, INPUT_REPLY, CONFIRM, EDIT_PATTERN, EDIT_REPLY = range(6)


MATCH_BUTTONS = [
    [
        InlineKeyboardButton("精确 exact", callback_data="add_match_exact"),
        InlineKeyboardButton("包含 contains", callback_data="add_match_contains"),
    ],
    [
        InlineKeyboardButton("正则 regex", callback_data="add_match_regex"),
        InlineKeyboardButton("模糊 fuzzy", callback_data="add_match_fuzzy"),
    ],
    [
        InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_back"),
    ],
]


def _menu_kb(context: ContextTypes.DEFAULT_TYPE | None = None) -> InlineKeyboardMarkup:
    gid = None
    title = None
    if context is not None:
        gid = context.user_data.get("manage_group_id")
        title = context.user_data.get("manage_group_title")

    if gid and title:
        label = f"📌 当前群: {title} ({gid})"
    elif gid:
        label = f"📌 当前群: {gid}"
    else:
        label = "📌 未选择群"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ 新增规则", callback_data="menu_add")],
            [InlineKeyboardButton("📄 查看规则", callback_data="menu_list")],
            [InlineKeyboardButton("🔄 切换群", callback_data="menu_switch")],
            [InlineKeyboardButton(label, callback_data="menu_noop")],
        ]
    )


def _truncate_one_line(text: str, max_len: int = 50) -> str:
    s = (text or "").replace("\r", "").replace("\n", " ⏎ ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _remember_group(
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    title: str | None = None,
    max_keep: int = 10,
) -> None:
    """记录该用户最近管理过的群，便于“切换群”菜单使用。"""
    recent: List[int] = context.user_data.get("recent_group_ids", [])
    recent = [gid for gid in recent if gid != group_id]
    recent.insert(0, group_id)
    context.user_data["recent_group_ids"] = recent[:max_keep]

    if title:
        titles: Dict[int, str] = context.user_data.get("group_titles", {})
        titles[group_id] = title
        context.user_data["group_titles"] = titles


async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """管理员检查：加重试，降低 Telegram Timed out 的影响。"""
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


# ======================= 群内入口 & “好的”按钮 =======================


async def rule_entry_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """在群里：管理员发送 /rule，给出私聊管理入口。"""
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not user or not message:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("请在群组里使用 /rule，然后点击按钮去私聊配置。")
        return

    chat_id = chat.id
    user_id = user.id

    if not await _is_admin(context, chat_id, user_id):
        await message.reply_text("只有群管理员可以配置关键词回复。")
        return

    # 记录群信息
    db = context.application.bot_data.get("db")
    if db is not None:
        try:
            async with db.session() as session:
                await ensure_group(session, group_id=chat_id, title=chat.title)
                await session.commit()
        except Exception as e:
            logger.warning(f"ensure_group failed in /rule: {e}")

    me = await context.bot.get_me()
    if not me.username:
        await message.reply_text("Bot 没有用户名，无法生成私聊管理链接（请在 BotFather 设置 username）。")
        return

    deep_link = f"https://t.me/{me.username}?start=manage_{chat_id}"

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔧 去私聊管理本群规则", url=deep_link)],
            [InlineKeyboardButton("✅ 好的（删除这条提示）", callback_data="rule_ok")],
        ]
    )

    await message.reply_text(
        "点击按钮在私聊里管理本群的关键词规则（不会在群里刷屏）。",
        reply_markup=kb,
    )


async def rule_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """群里 /rule 提示消息：管理员点“好的”就删掉这条 bot 消息。"""
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
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def rule_reply_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    群里关键词回复上的“好的”按钮：
    data 形如 rule_reply_ok:<trigger_user_id>
    只有触发该回复的用户或群管理员可以删除那条回复消息。
    """
    q = update.callback_query
    if not q or not q.message or not q.from_user:
        return
    await q.answer()

    chat = q.message.chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    data = q.data or ""
    trigger_user_id: int | None = None
    if ":" in data:
        _, maybe_id = data.split(":", 1)
        try:
            trigger_user_id = int(maybe_id)
        except ValueError:
            trigger_user_id = None

    user_id = q.from_user.id
    is_admin = await _is_admin(context, chat.id, user_id)
    if not is_admin and (trigger_user_id is None or trigger_user_id != user_id):
        await q.answer("只有触发该回复的成员或管理员可以删除。", show_alert=True)
        return

    try:
        await q.message.delete()
    except BadRequest:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


# ======================= 私聊 /start & 切换群 =======================


async def start_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊 /start，支持 /start manage_<group_id>。"""
    chat = update.effective_chat
    message = update.message
    if not chat or chat.type != ChatType.PRIVATE or not message:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "你好！\n\n"
            "请先在目标群组里使用 /rule（管理员），然后点击按钮进入该群的管理界面。"
        )
        return

    token = args[0].strip()
    if not token.startswith("manage_"):
        await message.reply_text("无法识别的 /start 参数。请回到群里用 /rule 进入。")
        return

    try:
        group_id = int(token.split("_", 1)[1])
    except Exception:
        await message.reply_text("参数格式不正确，请回到群里重新点一次按钮。")
        return

    user = update.effective_user
    user_id = user.id if user else 0
    if not await _is_admin(context, group_id, user_id):
        await message.reply_text("你不是该群管理员，无法管理该群规则。")
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
        await message.reply_text(
            f"你当前正在管理群 {current_gid}。\n是否切换到群 {group_id}？",
            reply_markup=kb,
        )
        return

    # 设置当前管理群
    try:
        chat_obj = await context.bot.get_chat(group_id)
        gtitle = chat_obj.title or str(group_id)
    except Exception:
        gtitle = str(group_id)

    context.user_data["manage_group_id"] = group_id
    context.user_data["manage_group_title"] = gtitle
    _remember_group(context, group_id, title=gtitle)

    await message.reply_text(
        f"已进入群 {gtitle} ({group_id}) 的规则管理：",
        reply_markup=_menu_kb(context),
    )


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

        try:
            chat_obj = await context.bot.get_chat(pending_gid)
            gtitle = chat_obj.title or str(pending_gid)
        except Exception:
            gtitle = str(pending_gid)

        context.user_data["manage_group_id"] = pending_gid
        context.user_data["manage_group_title"] = gtitle
        context.user_data.pop("pending_manage_group_id", None)
        _remember_group(context, pending_gid, title=gtitle)

        await q.edit_message_text(
            f"已切换到群 {gtitle} ({pending_gid}) 的规则管理：", reply_markup=_menu_kb(context)
        )
        return


async def show_switch_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """菜单：列出最近管理过的群，点击即可切换"""
    q = update.callback_query
    if q:
        await q.answer()

    recent: List[int] = context.user_data.get("recent_group_ids", [])
    current = context.user_data.get("manage_group_id")
    titles: Dict[int, str] = context.user_data.get("group_titles", {})

    if not recent:
        text = "你还没有管理过任何群。\n\n请先在群里输入 /rule，再从私聊进入。"
        if q:
            await q.edit_message_text(text, reply_markup=_menu_kb(context))
        else:
            await update.effective_message.reply_text(text, reply_markup=_menu_kb(context))
        return

    buttons: List[List[InlineKeyboardButton]] = []
    for gid in recent[:10]:
        prefix = "✅ " if gid == current else ""
        gname = titles.get(gid)
        if gname:
            label = f"{prefix}{gname} ({gid})"
        else:
            label = f"{prefix}{gid}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"switch_to_{gid}")])

    buttons.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu_back")])
    kb = InlineKeyboardMarkup(buttons)

    text = "最近管理过的群："
    if q:
        await q.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """返回主菜单（给 ConversationHandler 当作 fallback 用）。"""
    q = update.callback_query
    if q:
        await q.answer()
        await q.edit_message_text("主菜单：", reply_markup=_menu_kb(context))
    else:
        await update.effective_message.reply_text("主菜单：", reply_markup=_menu_kb(context))
    return ConversationHandler.END


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """主菜单 callback 分发。"""
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

    # 切换群：switch_to_<group_id>
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

        try:
            chat_obj = await context.bot.get_chat(gid)
            gtitle = chat_obj.title or str(gid)
        except Exception:
            gtitle = str(gid)

        context.user_data["manage_group_id"] = gid
        context.user_data["manage_group_title"] = gtitle
        _remember_group(context, gid, title=gtitle)
        await q.edit_message_text(f"已切换到群 {gtitle} ({gid})：", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    await q.edit_message_text("未知操作。", reply_markup=_menu_kb(context))
    return ConversationHandler.END


# ======================= 新增规则流程 =======================


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """开始新增规则：选择匹配模式。"""
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("管理群信息丢失，请回到群里重新 /rule 进入。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法新增规则。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    # 清理旧的临时数据
    for k in ("add_match_type", "add_pattern", "add_reply", "add_delete_after"):
        context.user_data.pop(k, None)

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


def _format_delete_after(sec: int | None) -> str:
    if not sec:
        return "不自动删除"
    return f"{sec} 秒后自动删除"


def _build_add_confirm_kb(delete_after: int | None) -> InlineKeyboardMarkup:
    da = delete_after or 0

    def label(sec: int) -> str:
        base = "不自动删除" if sec == 0 else f"{sec}s"
        return f"✅ {base}" if da == sec else base

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label(0), callback_data="add_del_0"),
                InlineKeyboardButton(label(3), callback_data="add_del_3"),
                InlineKeyboardButton(label(5), callback_data="add_del_5"),
            ],
            [
                InlineKeyboardButton(label(10), callback_data="add_del_10"),
                InlineKeyboardButton(label(15), callback_data="add_del_15"),
                InlineKeyboardButton(label(30), callback_data="add_del_30"),
            ],
            [
                InlineKeyboardButton("✅ 保存", callback_data="add_confirm_save"),
                InlineKeyboardButton("❌ 取消", callback_data="add_confirm_cancel"),
            ],
        ]
    )


async def input_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or update.message.text is None:
        await update.effective_message.reply_text("请发送文本作为回复内容。")
        return INPUT_REPLY

    reply = update.message.text
    if len(reply) > 8000:
        await update.message.reply_text("回复内容太长了（>8000）。请缩短后再发。")
        return INPUT_REPLY

    context.user_data["add_reply"] = reply
    # 默认不自动删除
    context.user_data["add_delete_after"] = 0

    match_type = context.user_data.get("add_match_type")
    pattern = context.user_data.get("add_pattern")
    delete_after = context.user_data.get("add_delete_after", 0)

    preview = (
        f"将创建规则：\n"
        f"- 模式: {match_type}\n"
        f"- 关键词/规则: {pattern}\n"
        f"- 回复: \n{reply}\n"
        f"- 自动删除: {_format_delete_after(delete_after)}\n\n"
        f"你可以先在下面选择自动删除时间，再点“保存”。"
    )
    kb = _build_add_confirm_kb(delete_after)
    await update.message.reply_text(preview, reply_markup=kb)
    return CONFIRM


async def confirm_set_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """新增规则确认阶段：修改自动删除时间。"""
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    data = q.data or ""
    try:
        sec = int(data.split("_", 2)[2])
    except Exception:
        sec = 0

    context.user_data["add_delete_after"] = sec

    match_type = context.user_data.get("add_match_type")
    pattern = context.user_data.get("add_pattern")
    reply = context.user_data.get("add_reply")
    delete_after = context.user_data.get("add_delete_after", 0)

    if not match_type or not pattern or reply is None:
        await q.edit_message_text("上下文丢失，请重新开始新增规则。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    preview = (
        f"将创建规则：\n"
        f"- 模式: {match_type}\n"
        f"- 关键词/规则: {pattern}\n"
        f"- 回复: \n{reply}\n"
        f"- 自动删除: {_format_delete_after(delete_after)}\n\n"
        f"你可以先在下面选择自动删除时间，再点“保存”。"
    )
    kb = _build_add_confirm_kb(delete_after)
    await q.edit_message_text(preview, reply_markup=kb)
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
        context.user_data.pop("add_delete_after", None)
        await q.edit_message_text("已取消。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    if q.data != "add_confirm_save":
        return ConversationHandler.END

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("管理群信息丢失，请回到群里重新 /rule 进入。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法保存规则。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    match_type = context.user_data.get("add_match_type")
    pattern = context.user_data.get("add_pattern")
    reply = context.user_data.get("add_reply")
    delete_after = context.user_data.get("add_delete_after") or 0

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
                delete_after=delete_after or None,
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
                    "delete_after": rule.delete_after,
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
    context.user_data.pop("add_delete_after", None)

    await q.edit_message_text("✅ 已保存规则。", reply_markup=_menu_kb(context))
    return ConversationHandler.END


# ======================= 查看 / 编辑 / 删除规则 =======================


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

    lines: List[str] = []
    buttons: List[List[InlineKeyboardButton]] = []
    for r in rules:
        status = "✅" if r.enabled else "⛔"
        reply_preview = _truncate_one_line(r.reply, max_len=50)
        auto_del = _format_delete_after(r.delete_after)
        lines.append(
            f"{status} #{r.id} [{r.match_type}] p={r.priority} :: {r.pattern}\n"
            f"    ↳ 回复: {reply_preview}\n"
            f"    ↳ 自动删除: {auto_del}"
        )
        buttons.append(
            [
                InlineKeyboardButton(f"✏️ 编辑关键词 #{r.id}", callback_data=f"editp_{r.id}"),
                InlineKeyboardButton(f"✏️ 编辑回复 #{r.id}", callback_data=f"editr_{r.id}"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(f"⏱ 自动删除 #{r.id}", callback_data=f"edel_{r.id}"),
                InlineKeyboardButton(f"🗑 删除 #{r.id}", callback_data=f"del_{r.id}"),
            ]
        )

    text = "规则列表（前 20 条）：\n\n" + "\n\n".join(lines)
    buttons.append([InlineKeyboardButton("⬅️ 返回菜单", callback_data="menu_back")])
    kb = InlineKeyboardMarkup(buttons)
    if q:
        await q.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def edit_rule_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """显示某条规则的自动删除设置菜单。"""
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
        await q.edit_message_text("你不是该群管理员，无法编辑规则。", reply_markup=_menu_kb(context))
        return

    try:
        rule_id = int((q.data or "").split("_", 1)[1])
    except Exception:
        await q.edit_message_text("参数错误。", reply_markup=_menu_kb(context))
        return

    db = context.application.bot_data["db"]
    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)

    if not rule:
        await q.edit_message_text(f"未找到规则 #{rule_id}（可能已删除）。", reply_markup=_menu_kb(context))
        return

    current = rule.delete_after or 0
    text = (
        f"规则 #{rule_id} 的自动删除设置：\n"
        f"当前：{_format_delete_after(current)}\n\n"
        f"请选择新的自动删除时间："
    )

    def btn(sec: int) -> InlineKeyboardButton:
        label = "不自动删除" if sec == 0 else f"{sec}s"
        if current == sec:
            label = f"✅ {label}"
        return InlineKeyboardButton(label, callback_data=f"edelset_{rule_id}_{sec}")

    kb = InlineKeyboardMarkup(
        [
            [btn(0), btn(3), btn(5)],
            [btn(10), btn(15), btn(30)],
            [InlineKeyboardButton("⬅️ 返回规则列表", callback_data="menu_list")],
        ]
    )
    await q.edit_message_text(text, reply_markup=kb)


async def set_rule_delete_after(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """真正更新某条规则的自动删除时间。"""
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    try:
        _, rule_id_str, sec_str = data.split("_", 2)
        rule_id = int(rule_id_str)
        sec = int(sec_str)
    except Exception:
        await q.edit_message_text("参数错误。", reply_markup=_menu_kb(context))
        return

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("请先在群里 /rule 进入对应群管理。", reply_markup=_menu_kb(context))
        return

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法编辑规则。", reply_markup=_menu_kb(context))
        return

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)
        if not rule:
            await q.edit_message_text(f"未找到规则 #{rule_id}（可能已删除）。", reply_markup=_menu_kb(context))
            return

        before = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        rule.delete_after = sec or None

        after = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        await add_audit(
            session,
            group_id=group_id,
            actor_user_id=user_id,
            action="update_delete_after",
            before_json=before,
            after_json=after,
        )
        await session.commit()

    cache.invalidate(group_id)

    await q.edit_message_text(
        f"已更新规则 #{rule_id} 的自动删除设置为：{_format_delete_after(sec)}\n\n"
        f"你可以点击“查看规则”再次查看当前配置。",
        reply_markup=_menu_kb(context),
    )


async def start_edit_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """开始编辑某条规则的关键词。"""
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("请先在群里 /rule 进入对应群管理。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法编辑规则。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    try:
        rule_id = int((q.data or "").split("_", 1)[1])
    except Exception:
        await q.edit_message_text("参数错误。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    db = context.application.bot_data["db"]
    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)

    if not rule:
        await q.edit_message_text(f"未找到规则 #{rule_id}（可能已删除）。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    context.user_data["edit_rule_id"] = rule_id

    text = (
        f"正在编辑规则 #{rule_id} 的关键词。\n"
        f"当前匹配模式：{rule.match_type}\n"
        f"当前关键词/规则：\n{rule.pattern}\n\n"
        f"请发送新的关键词/规则内容："
    )
    await q.edit_message_text(text)
    return EDIT_PATTERN


async def save_edited_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """保存编辑后的关键词。"""
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("请发送文本作为新的关键词/规则。")
        return EDIT_PATTERN

    group_id = context.user_data.get("manage_group_id")
    rule_id = context.user_data.get("edit_rule_id")
    if not group_id or not rule_id:
        await update.effective_message.reply_text(
            "上下文丢失，请重新在“查看规则”里点击编辑。",
            reply_markup=_menu_kb(context),
        )
        return ConversationHandler.END

    new_pattern = update.message.text.strip()
    if len(new_pattern) > 2000:
        await update.message.reply_text("关键词/规则太长了（>2000）。请缩短后再发。")
        return EDIT_PATTERN

    user = update.effective_user
    user_id = user.id if user else 0

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)
        if not rule:
            await update.message.reply_text(
                f"未找到规则 #{rule_id}（可能已删除）。",
                reply_markup=_menu_kb(context),
            )
            return ConversationHandler.END

        before = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        rule.pattern = new_pattern

        after = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        await add_audit(
            session,
            group_id=group_id,
            actor_user_id=user_id,
            action="update_pattern",
            before_json=before,
            after_json=after,
        )
        await session.commit()

    cache.invalidate(group_id)
    context.user_data.pop("edit_rule_id", None)

    await update.message.reply_text(
        f"规则 #{rule_id} 的关键词已更新。",
        reply_markup=_menu_kb(context),
    )
    return ConversationHandler.END


async def start_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """开始编辑某条规则的回复内容。"""
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    await q.answer()

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("请先在群里 /rule 进入对应群管理。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法编辑规则。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    try:
        rule_id = int((q.data or "").split("_", 1)[1])
    except Exception:
        await q.edit_message_text("参数错误。", reply_markup=_menu_kb(context))
        return ConversationHandler.END

    db = context.application.bot_data["db"]
    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)

    if not rule:
        await q.edit_message_text(
            f"未找到规则 #{rule_id}（可能已删除）。",
            reply_markup=_menu_kb(context),
        )
        return ConversationHandler.END

    context.user_data["edit_rule_id"] = rule_id

    text = (
        f"正在编辑规则 #{rule_id} 的回复内容。\n"
        f"当前回复：\n{rule.reply}\n\n"
        f"请发送新的回复内容（可多行）："
    )
    await q.edit_message_text(text)
    return EDIT_REPLY


async def save_edited_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """保存编辑后的回复内容。"""
    if not update.message or update.message.text is None:
        await update.effective_message.reply_text("请发送文本作为新的回复内容。")
        return EDIT_REPLY

    group_id = context.user_data.get("manage_group_id")
    rule_id = context.user_data.get("edit_rule_id")
    if not group_id or not rule_id:
        await update.effective_message.reply_text(
            "上下文丢失，请重新在“查看规则”里点击编辑回复。",
            reply_markup=_menu_kb(context),
        )
        return ConversationHandler.END

    new_reply = update.message.text
    if len(new_reply) > 8000:
        await update.message.reply_text("回复内容太长了（>8000）。请缩短后再发。")
        return EDIT_REPLY

    user = update.effective_user
    user_id = user.id if user else 0

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)
        if not rule:
            await update.message.reply_text(
                f"未找到规则 #{rule_id}（可能已删除）。",
                reply_markup=_menu_kb(context),
            )
            return ConversationHandler.END

        before = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        rule.reply = new_reply

        after = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        await add_audit(
            session,
            group_id=group_id,
            actor_user_id=user_id,
            action="update_reply",
            before_json=before,
            after_json=after,
        )
        await session.commit()

    cache.invalidate(group_id)
    context.user_data.pop("edit_rule_id", None)

    await update.message.reply_text(
        f"规则 #{rule_id} 的回复内容已更新。",
        reply_markup=_menu_kb(context),
    )
    return ConversationHandler.END


async def delete_rule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """删除某条规则。"""
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    try:
        rule_id = int(data.split("_", 1)[1])
    except Exception:
        await q.edit_message_text("参数错误。", reply_markup=_menu_kb(context))
        return

    group_id = context.user_data.get("manage_group_id")
    if not group_id:
        await q.edit_message_text("请先在群里 /rule 进入对应群管理。", reply_markup=_menu_kb(context))
        return

    user_id = q.from_user.id
    if not await _is_admin(context, group_id, user_id):
        await q.edit_message_text("你不是该群管理员，无法删除规则。", reply_markup=_menu_kb(context))
        return

    db = context.application.bot_data["db"]
    cache = context.application.bot_data["rule_cache"]

    async with db.session() as session:
        rule = await get_rule(session, group_id=group_id, rule_id=rule_id)
        if not rule:
            await q.edit_message_text(
                f"未找到规则 #{rule_id}（可能已删除）。",
                reply_markup=_menu_kb(context),
            )
            return

        before = {
            "id": rule.id,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply": rule.reply,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "delete_after": rule.delete_after,
        }

        await delete_rule_by_id(session, group_id=group_id, rule_id=rule_id)

        await add_audit(
            session,
            group_id=group_id,
            actor_user_id=user_id,
            action="delete",
            before_json=before,
            after_json=None,
        )
        await session.commit()

    cache.invalidate(group_id)

    await q.edit_message_text(
        f"规则 #{rule_id} 已删除。",
        reply_markup=_menu_kb(context),
    )
