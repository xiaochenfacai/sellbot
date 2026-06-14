"""
Telegram 发布机器人
私聊发送图片/视频/文字 → 自动转发到已绑定的群或频道（需机器人为管理员）
"""

import logging
import sqlite3
import json
import os
import threading
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from flask import Flask, request

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ========== 配置（部署前请修改） ==========
TOKEN = os.environ.get("POSTBOT_TOKEN", "8877964306:AAFozmpoGQWv9kARDd8v3rpdhYNvlGbiZbM")
MASTER_USER_ID = int(os.environ.get("POSTBOT_MASTER", "8807178282"))
WEB_URL = os.environ.get("WEB_URL", "")
PORT = int(os.environ.get("PORT", 8080))

flask_app = Flask(__name__)

# 待发布内容暂存：user_id -> {message_ids, chat_id, caption, media_group_id}
_pending: dict[int, dict] = {}


# ========== 数据库 ==========

def init_db():
    conn = sqlite3.connect("postbot_data.db")
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS targets (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT,
            chat_type TEXT,
            added_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS user_prefs (
            user_id INTEGER PRIMARY KEY,
            default_target INTEGER
        )"""
    )
    conn.commit()
    conn.close()


def add_target(chat_id: int, title: str, chat_type: str):
    conn = sqlite3.connect("postbot_data.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO targets (chat_id, chat_title, chat_type, added_at) VALUES (?, ?, ?, ?)",
        (chat_id, title, chat_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def remove_target(chat_id: int):
    conn = sqlite3.connect("postbot_data.db")
    c = conn.cursor()
    c.execute("DELETE FROM targets WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_targets():
    conn = sqlite3.connect("postbot_data.db")
    c = conn.cursor()
    c.execute("SELECT chat_id, chat_title, chat_type FROM targets ORDER BY added_at")
    rows = c.fetchall()
    conn.close()
    return [{"chat_id": r[0], "title": r[1], "type": r[2]} for r in rows]


def set_default_target(user_id: int, chat_id: int | None):
    conn = sqlite3.connect("postbot_data.db")
    c = conn.cursor()
    if chat_id is None:
        c.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))
    else:
        c.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, default_target) VALUES (?, ?)",
            (user_id, chat_id),
        )
    conn.commit()
    conn.close()


def get_default_target(user_id: int):
    conn = sqlite3.connect("postbot_data.db")
    c = conn.cursor()
    c.execute("SELECT default_target FROM user_prefs WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


# ========== 权限 ==========

def is_master(user_id: int) -> bool:
    return user_id == MASTER_USER_ID


# ========== 发布逻辑 ==========

async def copy_to_target(context: ContextTypes.DEFAULT_TYPE, from_chat_id: int, message_id: int, target_id: int):
    """复制单条消息到目标（不显示「转发自」）"""
    return await context.bot.copy_message(
        chat_id=target_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
    )


async def publish_message(context, from_chat_id: int, message_id: int, target_ids: list[int]) -> list[tuple[int, str]]:
    results = []
    for tid in target_ids:
        try:
            await copy_to_target(context, from_chat_id, message_id, tid)
            results.append((tid, "ok"))
        except Exception as e:
            results.append((tid, str(e)))
            logging.error("发布到 %s 失败: %s", tid, e)
    return results


def _target_label(t: dict) -> str:
    kind = "频道" if t["type"] == "channel" else "群组"
    return f"{t['title']} ({kind})"


def _build_target_keyboard(targets: list[dict], prefix: str = "pub") -> InlineKeyboardMarkup:
    buttons = []
    for t in targets:
        buttons.append([
            InlineKeyboardButton(
                _target_label(t),
                callback_data=f"{prefix}:{t['chat_id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("📢 全部发送", callback_data=f"{prefix}:all")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(buttons)


async def _ask_target_or_publish(update: Update, context: ContextTypes.DEFAULT_TYPE, message):
    """收到私聊内容后，选择发布目标或直接发布"""
    user_id = update.effective_user.id
    targets = get_targets()

    if not targets:
        await message.reply_text(
            "还没有绑定任何群/频道。\n\n"
            "请先把机器人拉进群或频道，并设为管理员（需有发消息权限），"
            "然后在群里发送 /bind 绑定。"
        )
        return

    default_id = get_default_target(user_id)
    if default_id and any(t["chat_id"] == default_id for t in targets):
        results = await publish_message(context, message.chat_id, message.message_id, [default_id])
        ok = [t for t, s in results if s == "ok"]
        fail = [(t, s) for t, s in results if s != "ok"]
        text = f"✅ 已发布到默认目标"
        if fail:
            text += f"\n⚠️ 失败: {fail[0][1]}"
        await message.reply_text(text)
        return

    _pending[user_id] = {
        "from_chat_id": message.chat_id,
        "message_id": message.message_id,
    }
    await message.reply_text(
        "选择要发布到哪里：",
        reply_markup=_build_target_keyboard(targets),
    )


# ========== 命令处理 ==========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_master(update.effective_user.id):
        await update.message.reply_text("此机器人仅供管理员使用。")
        return
    await update.message.reply_text(
        "📮 发布机器人\n\n"
        "用法：\n"
        "1. 把机器人拉进群/频道，设为管理员\n"
        "2. 在群/频道里发 /bind 绑定\n"
        "3. 私聊发图片、视频、文字 → 自动发布\n\n"
        "命令：\n"
        "/targets — 查看已绑定的群/频道\n"
        "/default — 设置默认发布目标\n"
        "/unbind — 在群内解除绑定\n"
        "/help — 帮助"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("请在群或频道内使用 /bind。")
        return

    if not is_master(user.id):
        await update.message.reply_text("只有管理员可以绑定。")
        return

    me = await context.bot.get_me()
    member = await context.bot.get_chat_member(chat.id, me.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("请先把机器人设为管理员。")
        return

    if chat.type == "channel":
        if not (member.can_post_messages or member.can_edit_messages):
            await update.message.reply_text("频道里机器人需要「发消息」权限。")
            return
    elif chat.type in ("group", "supergroup"):
        if member.can_send_messages is False:
            await update.message.reply_text("群里机器人需要「发消息」权限。")
            return

    title = chat.title or str(chat.id)
    add_target(chat.id, title, chat.type)
    await update.message.reply_text(f"✅ 已绑定：{title}\n\n私聊发内容即可发布到这里。")


async def cmd_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        return
    if not is_master(update.effective_user.id):
        return
    remove_target(chat.id)
    await update.message.reply_text("已解除绑定。")


async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    targets = get_targets()
    if not targets:
        await update.message.reply_text("暂无绑定的群/频道。")
        return
    lines = ["📋 已绑定的发布目标：\n"]
    for i, t in enumerate(targets, 1):
        kind = "频道" if t["type"] == "channel" else "群组"
        default = get_default_target(update.effective_user.id)
        mark = " ⭐默认" if default == t["chat_id"] else ""
        lines.append(f"{i}. {t['title']} ({kind}){mark}\n   ID: {t['chat_id']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    targets = get_targets()
    if not targets:
        await update.message.reply_text("请先绑定群/频道。")
        return
    await update.message.reply_text(
        "选择默认发布目标（私聊发内容时会直接发到这里，不用再选）：",
        reply_markup=_build_target_keyboard(targets, prefix="def"),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_master(query.from_user.id):
        return

    data = query.data
    if not data:
        return

    action, _, value = data.partition(":")
    user_id = query.from_user.id

    if action == "def":
        if value == "cancel":
            await query.edit_message_text("已取消。")
            return
        if value == "all":
            set_default_target(user_id, None)
            await query.edit_message_text("已清除默认目标，每次发布需手动选择。")
            return
        set_default_target(user_id, int(value))
        targets = {t["chat_id"]: t for t in get_targets()}
        t = targets.get(int(value), {})
        await query.edit_message_text(f"⭐ 默认目标已设为：{t.get('title', value)}")

    elif action == "pub":
        pending = _pending.pop(user_id, None)
        if not pending:
            await query.edit_message_text("内容已过期，请重新发送。")
            return

        if value == "cancel":
            await query.edit_message_text("已取消发布。")
            return

        targets = get_targets()
        if value == "all":
            target_ids = [t["chat_id"] for t in targets]
        else:
            target_ids = [int(value)]

        results = await publish_message(
            context,
            pending["from_chat_id"],
            pending["message_id"],
            target_ids,
        )
        ok_count = sum(1 for _, s in results if s == "ok")
        fail = [(t, s) for t, s in results if s != "ok"]
        msg = f"✅ 成功发布到 {ok_count} 个目标"
        if fail:
            msg += f"\n⚠️ {len(fail)} 个失败"
        await query.edit_message_text(msg)


# 支持私聊发布的消息类型
_PUBLISH_FILTER = (
    filters.PHOTO
    | filters.VIDEO
    | filters.ANIMATION
    | filters.Document.ALL
    | filters.AUDIO
    | filters.VOICE
    | filters.VIDEO_NOTE
    | filters.TEXT & ~filters.COMMAND
)


async def on_private_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_master(update.effective_user.id):
        return
    await _ask_target_or_publish(update, context, update.message)


# ========== Webhook（Render 部署用） ==========

@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "ok"


@flask_app.route("/")
def index():
    return "PostBot is running."


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ========== 启动 ==========

init_db()
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(CommandHandler("bind", cmd_bind))
application.add_handler(CommandHandler("unbind", cmd_unbind))
application.add_handler(CommandHandler("targets", cmd_targets))
application.add_handler(CommandHandler("default", cmd_default))
application.add_handler(CallbackQueryHandler(on_callback))
application.add_handler(MessageHandler(_PUBLISH_FILTER & filters.ChatType.PRIVATE, on_private_content))

if __name__ == "__main__":
    if WEB_URL:
        threading.Thread(target=run_flask, daemon=True).start()
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEB_URL}/{TOKEN}",
        )
    else:
        print("本地模式：polling 运行中...")
        application.run_polling()
