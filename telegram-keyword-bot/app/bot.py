from __future__ import annotations

from loguru import logger
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from app.config import get_settings
from app.db import Database
from app.cache import RuleCache
from app.matching import Throttle
from app.handlers.admin import (
    rule_entry_in_group,
    rule_ok,
    rule_reply_ok,
    start_private,
    switch_manage_group,
    menu_router,
    show_rules,
    add_start,
    choose_match,
    input_pattern,
    input_reply,
    confirm_set_delete,
    confirm,
    start_edit_pattern,
    save_edited_pattern,
    start_edit_reply,
    save_edited_reply,
    edit_rule_delete_menu,
    set_rule_delete_after,
    delete_rule,
    menu_back,
    CHOOSE_MATCH,
    INPUT_PATTERN,
    INPUT_REPLY,
    CONFIRM,
    EDIT_PATTERN,
    EDIT_REPLY,
)
from app.handlers.messages import on_group_message


def run() -> None:
    settings = get_settings()

    db = Database.from_url(settings.database_url)
    rule_cache = RuleCache(ttl_seconds=settings.rule_cache_ttl_seconds)
    throttle = Throttle(cooldown_seconds=settings.rule_cooldown_seconds)

    # 可选：减小 Timed out 概率
    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(5)
        .build()
    )

    app.bot_data["db"] = db
    app.bot_data["rule_cache"] = rule_cache
    app.bot_data["throttle"] = throttle

    # /rule only in groups
    app.add_handler(CommandHandler("rule", rule_entry_in_group, filters=filters.ChatType.GROUPS))

    # 群里“好的（删除 /rule 提示）”
    app.add_handler(CallbackQueryHandler(rule_ok, pattern=r"^rule_ok$"))

    # 关键词回复里的“好的”按钮
    app.add_handler(CallbackQueryHandler(rule_reply_ok, pattern=r"^rule_reply_ok:"))

    # 私聊 /start manage_<group_id>
    app.add_handler(CommandHandler("start", start_private, filters=filters.ChatType.PRIVATE))

    # 私聊：是否切换群确认
    app.add_handler(CallbackQueryHandler(switch_manage_group, pattern=r"^switch_(yes|no)$"))

    # 新增规则会话
    add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
        ],
        states={
            CHOOSE_MATCH: [
                CallbackQueryHandler(choose_match, pattern=r"^add_match_"),
                CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            ],
            INPUT_PATTERN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_pattern),
                CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            ],
            INPUT_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_reply),
                CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_set_delete, pattern=r"^add_del_\d+"),
                CallbackQueryHandler(confirm, pattern=r"^add_confirm_"),
                CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            CallbackQueryHandler(menu_back, pattern=r"^menu_back$"),
        ],
        name="add_rule_conv",
        persistent=False,
        per_chat=True,
        per_message=False,  # 必须 False，否则 MessageHandler 步骤收不到
    )
    app.add_handler(add_conv)

    # 编辑规则会话（编辑关键词 / 回复）
    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_edit_pattern, pattern=r"^editp_\d+$"),
            CallbackQueryHandler(start_edit_reply, pattern=r"^editr_\d+$"),
        ],
        states={
            EDIT_PATTERN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_pattern),
                CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            ],
            EDIT_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_reply),
                CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(menu_router, pattern=r"^(menu_|switch_to_)"),
            CallbackQueryHandler(menu_back, pattern=r"^menu_back$"),
        ],
        name="edit_rule_conv",
        persistent=False,
        per_chat=True,
        per_message=False,
    )
    app.add_handler(edit_conv)

    # 返回主菜单按钮（全局）
    app.add_handler(CallbackQueryHandler(menu_back, pattern=r"^menu_back$"))

    # 删除规则按钮（独立处理）
    app.add_handler(CallbackQueryHandler(delete_rule, pattern=r"^del_\d+$"))

    # 自动删除设置菜单
    app.add_handler(CallbackQueryHandler(edit_rule_delete_menu, pattern=r"^edel_\d+$"))
    app.add_handler(CallbackQueryHandler(set_rule_delete_after, pattern=r"^edelset_\d+_\d+$"))

    # 群消息自动回复
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, on_group_message)
    )

    logger.info("Bot is starting polling...")
    try:
        app.run_polling(allowed_updates=None)
    finally:
        import asyncio

        try:
            asyncio.run(db.dispose())
        except Exception:
            pass
