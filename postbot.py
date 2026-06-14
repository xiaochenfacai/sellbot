"""
PostBot - Telegram 发布机器人
私聊发图片/视频/文字 → 发布到已绑定的群或频道
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("postbot")

TOKEN = os.environ.get("POSTBOT_TOKEN", "8877964306:AAHm05ZMBbdI-5kffaiEvw1mqLPCFflWQO0")
MASTER_ID = int(os.environ.get("POSTBOT_MASTER", "8807178282"))
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("POSTBOT_DB", "postbot_data.db")

flask_app = Flask(__name__)
_pending: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS targets (
                chat_id   INTEGER PRIMARY KEY,
                title     TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                added_at  TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS prefs (
                user_id         INTEGER PRIMARY KEY,
                default_target  INTEGER
            )"""
        )


def db_add_target(chat_id: int, title: str, chat_type: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO targets VALUES (?, ?, ?, ?)",
            (chat_id, title, chat_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def db_remove_target(chat_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM targets WHERE chat_id = ?", (chat_id,))


def db_list_targets() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT chat_id, title, chat_type FROM targets ORDER BY added_at"
        ).fetchall()
    return [{"id": r[0], "title": r[1], "type": r[2]} for r in rows]


def db_set_default(user_id: int, chat_id: int | None):
    with sqlite3.connect(DB_PATH) as conn:
        if chat_id is None:
            conn.execute("DELETE FROM prefs WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO prefs VALUES (?, ?)",
                (user_id, chat_id),
            )


def db_get_default(user_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT default_target FROM prefs WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_master(user_id: int | None) -> bool:
    return user_id == MASTER_ID


async def reply(update: Update, text: str, **kwargs):
    msg = update.effective_message
    if msg:
        return await msg.reply_text(text, **kwargs)


def forward_chat(message):
    if message.forward_from_chat:
        return message.forward_from_chat
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "chat", None):
        return origin.chat
    return None


def target_label(t: dict) -> str:
    kind = "频道" if t["type"] == "channel" else "群组"
    return f"{t['title']} ({kind})"


def build_keyboard(targets: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(target_label(t), callback_data=f"{prefix}:{t['id']}")]
        for t in targets
    ]
    rows.append([InlineKeyboardButton("📢 全部发送", callback_data=f"{prefix}:all")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(rows)


async def verify_and_bind(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          chat_id: int, title: str, chat_type: str):
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)

        if member.status not in ("administrator", "creator"):
            await reply(update, "❌ 请先把机器人设为管理员。")
            return

        if chat_type == "channel":
            can_post = getattr(member, "can_post_messages", False)
            can_edit = getattr(member, "can_edit_messages", False)
            if not (can_post or can_edit):
                await reply(update, "❌ 频道里机器人需要「发消息」权限。")
                return
        elif chat_type in ("group", "supergroup"):
            # 管理员类型没有 can_send_messages，只有受限成员才有
            if getattr(member, "can_send_messages", True) is False:
                await reply(update, "❌ 群里机器人需要「发消息」权限。")
                return

        db_add_target(chat_id, title, chat_type)
        await reply(update, f"✅ 已绑定：{title}\n\n现在可以私聊我发作品了。")
        log.info("绑定成功 chat_id=%s title=%s", chat_id, title)

    except Exception as e:
        log.exception("绑定失败 chat_id=%s", chat_id)
        await reply(update, f"❌ 绑定失败：{e}")


async def publish_to(context: ContextTypes.DEFAULT_TYPE,
                     from_chat: int, msg_id: int, target_ids: list[int]) -> tuple[int, int]:
    ok, fail = 0, 0
    for tid in target_ids:
        try:
            await context.bot.copy_message(chat_id=tid, from_chat_id=from_chat, message_id=msg_id)
            ok += 1
        except Exception as e:
            fail += 1
            log.error("发布失败 target=%s err=%s", tid, e)
    return ok, fail


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
HELP_PRIVATE = (
    "📮 <b>发布机器人</b>\n\n"
    "<b>绑定群/频道：</b>\n"
    "① 群里发 /bind\n"
    "② 私聊转发频道/群消息给我\n"
    "③ 私聊发 /bind -100xxxxxxxxxx\n\n"
    "<b>发布作品：</b>\n"
    "私聊发图片、视频、文字 → 选择目标\n\n"
    "<b>命令：</b>\n"
    "/ping — 测试在线\n"
    "/id — 查看 ID\n"
    "/targets — 已绑定列表\n"
    "/default — 默认发布目标\n"
    "/unbind — 解除绑定（在群内发）"
)

HELP_GROUP = (
    "📮 发布机器人已就绪\n"
    "本群 ID：<code>{cid}</code>\n\n"
    "管理员发 /bind 绑定\n"
    "发 /ping 测试在线"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        if not is_master(user.id if user else None):
            await reply(update, "此机器人仅供管理员使用。")
            return
        await reply(update, HELP_PRIVATE, parse_mode="HTML")
    else:
        await reply(update, HELP_GROUP.format(cid=chat.id), parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await reply(update, f"✅ 机器人在线\n聊天 ID：<code>{chat.id}</code>", parse_mode="HTML")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    uid = user.id if user else "?"
    ok = is_master(user.id if user else None)
    await reply(
        update,
        f"你的 ID：<code>{uid}</code>\n"
        f"聊天 ID：<code>{chat.id}</code>\n"
        f"管理员 ID：<code>{MASTER_ID}</code>\n"
        f"匹配：{'✅ 是管理员' if ok else '❌ 不是管理员'}",
        parse_mode="HTML",
    )


async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    # 私聊绑定
    if chat.type == "private":
        if not is_master(user.id if user else None):
            await reply(update, "只有管理员可以绑定。")
            return

        if context.args:
            try:
                target_id = int(context.args[0])
            except ValueError:
                await reply(update, "格式：/bind -100xxxxxxxxxx")
                return
            try:
                t = await context.bot.get_chat(target_id)
            except Exception as e:
                await reply(update, f"找不到群/频道：{e}")
                return
            await verify_and_bind(update, context, t.id, t.title or str(t.id), t.type)
            return

        await reply(
            update,
            "绑定方法：\n\n"
            "① 转发群/频道消息到这里（推荐）\n"
            "② 在群里发 /bind\n"
            "③ /bind -100xxxxxxxxxx",
        )
        return

    # 群/频道内绑定
    if not is_master(user.id if user else None):
        await reply(update, "只有管理员可以绑定。")
        return

    await verify_and_bind(update, context, chat.id, chat.title or str(chat.id), chat.type)


async def cmd_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return
    if not is_master(user.id if user else None):
        await reply(update, "只有管理员可以操作。")
        return
    db_remove_target(chat.id)
    await reply(update, "✅ 已解除绑定。")


async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    targets = db_list_targets()
    if not targets:
        await reply(update, "暂无绑定的群/频道。")
        return
    default = db_get_default(update.effective_user.id)
    lines = ["📋 已绑定：\n"]
    for i, t in enumerate(targets, 1):
        mark = " ⭐" if default == t["id"] else ""
        kind = "频道" if t["type"] == "channel" else "群组"
        lines.append(f"{i}. {t['title']} ({kind}){mark}\n   ID: {t['id']}")
    await reply(update, "\n".join(lines))


async def cmd_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    targets = db_list_targets()
    if not targets:
        await reply(update, "请先绑定群/频道。")
        return
    await reply(update, "选择默认发布目标：", reply_markup=build_keyboard(targets, "def"))


# ---------------------------------------------------------------------------
# 私聊发布 & 转发绑定
# ---------------------------------------------------------------------------
MEDIA_FILTER = (
    filters.PHOTO | filters.VIDEO | filters.ANIMATION
    | filters.Document.ALL | filters.AUDIO | filters.VOICE
    | filters.VIDEO_NOTE | (filters.TEXT & ~filters.COMMAND)
)


async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_master(update.effective_user.id):
        return

    msg = update.message
    targets = db_list_targets()

    if not targets:
        await msg.reply_text(
            "还没有绑定任何群/频道。\n\n"
            "请先：\n"
            "① 把机器人拉进群/频道并设管理员\n"
            "② 群里发 /bind，或转发消息给我绑定"
        )
        return

    default = db_get_default(update.effective_user.id)
    if default and any(t["id"] == default for t in targets):
        ok, fail = await publish_to(context, msg.chat_id, msg.message_id, [default])
        text = "✅ 已发布到默认目标"
        if fail:
            text += f"\n⚠️ 发布失败 {fail} 次"
        await msg.reply_text(text)
        return

    _pending[update.effective_user.id] = {
        "from_chat": msg.chat_id,
        "msg_id": msg.message_id,
    }
    await msg.reply_text("选择发布目标：", reply_markup=build_keyboard(targets, "pub"))


async def on_forward_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_master(update.effective_user.id):
        return

    source = forward_chat(update.message)
    if not source or source.type not in ("channel", "group", "supergroup"):
        return

    await verify_and_bind(
        update, context,
        source.id,
        source.title or str(source.id),
        source.type,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_master(query.from_user.id):
        return

    action, _, value = query.data.partition(":")
    uid = query.from_user.id

    if action == "def":
        if value == "cancel":
            await query.edit_message_text("已取消。")
        elif value == "all":
            db_set_default(uid, None)
            await query.edit_message_text("已清除默认目标。")
        else:
            db_set_default(uid, int(value))
            targets = {t["id"]: t for t in db_list_targets()}
            name = targets.get(int(value), {}).get("title", value)
            await query.edit_message_text(f"⭐ 默认目标：{name}")
        return

    if action == "pub":
        pending = _pending.pop(uid, None)
        if not pending:
            await query.edit_message_text("内容已过期，请重新发送。")
            return
        if value == "cancel":
            await query.edit_message_text("已取消发布。")
            return

        targets = db_list_targets()
        ids = [t["id"] for t in targets] if value == "all" else [int(value)]
        ok, fail = await publish_to(context, pending["from_chat"], pending["msg_id"], ids)
        text = f"✅ 成功发布到 {ok} 个目标"
        if fail:
            text += f"\n⚠️ {fail} 个失败"
        await query.edit_message_text(text)


# ---------------------------------------------------------------------------
# 事件 & 日志
# ---------------------------------------------------------------------------
async def on_bot_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.my_chat_member
    if not m or m.new_chat_member.status not in ("administrator", "member"):
        return
    chat = m.chat
    if chat.type not in ("group", "supergroup", "channel"):
        return
    try:
        await context.bot.send_message(
            chat.id,
            HELP_GROUP.format(cid=chat.id),
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("进群消息发送失败: %s", e)


async def on_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    log.info(
        "update chat=%s type=%s user=%s text=%s",
        chat.id if chat else "?",
        chat.type if chat else "?",
        user.id if user else "?",
        (msg.text[:60] if msg and msg.text else ""),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("处理出错", exc_info=context.error)


# ---------------------------------------------------------------------------
# Flask + 启动
# ---------------------------------------------------------------------------
@flask_app.route("/")
def health():
    return f"PostBot OK | master={MASTER_ID}", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


def create_app() -> Application:
    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(on_error)

    # 日志（最低优先级）
    app.add_handler(MessageHandler(filters.ALL, on_log), group=-1)

    # 进群事件
    app.add_handler(ChatMemberHandler(on_bot_joined, ChatMemberHandler.MY_CHAT_MEMBER))

    # 普通消息命令
    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help),
        ("ping", cmd_ping), ("id", cmd_id),
        ("bind", cmd_bind), ("unbind", cmd_unbind),
        ("targets", cmd_targets), ("default", cmd_default),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
        app.add_handler(CommandHandler(cmd, handler, filters=filters.UpdateType.CHANNEL_POSTS))

    # 回调 & 私聊
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.FORWARDED, on_forward_bind))
    app.add_handler(MessageHandler(MEDIA_FILTER & filters.ChatType.PRIVATE, on_private_media))

    return app


def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("PostBot 启动 port=%s master=%s", PORT, MASTER_ID)
    create_app().run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
