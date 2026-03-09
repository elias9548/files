import asyncio
import logging
import re
import sqlite3
from datetime import datetime

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = "8357213873:AAE0QrL6DeRJlq9H0ouQF-3x9QaLHm9-ysM"

FIREBASE_KEY = "AIzaSyACgNtcW49YjVoSZ_CfbvNFs0t-Y_SMsYU"
PROJECT_ID   = "septima-f756f"
FS_BASE      = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"

POLL_INTERVAL = 15   # seconds between read-status checks
POLL_TIMEOUT  = 3600 # stop polling after 1 hour

MISTRAL_TOKEN = "dDAcPHX7mxaTD2PUxZyClgn2RCr6rMJ2"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# ── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── DATABASE (local SQLite for tracking pending messages) ─────────────────────
db = sqlite3.connect("septima_bot.db", check_same_thread=False)
db.execute("""
    CREATE TABLE IF NOT EXISTS pending (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id   INTEGER NOT NULL,
        status_msg  INTEGER NOT NULL,
        doc_name    TEXT,
        content_label TEXT,
        created_at  REAL DEFAULT (strftime('%s','now'))
    )
""")
db.commit()

# ── IN-MEMORY STATE ──────────────────────────────────────────────────────────
# user_states[chat_id] = { "step": "...", ... }
user_states: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# FIREBASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _query_by_field(field: str, value: str) -> list[dict]:
    """Run a Firestore structuredQuery and return list of docs as dicts."""
    url = f"{FS_BASE}:runQuery?key={FIREBASE_KEY}"
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "messages"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": field},
                    "op": "EQUAL",
                    "value": {"stringValue": value},
                }
            },
        }
    }
    try:
        r = requests.post(url, json=body, timeout=10)
        results = []
        for item in r.json():
            if "document" in item:
                doc = item["document"]
                f = doc.get("fields", {})
                results.append({
                    "doc_name": doc["name"],
                    "code":      f.get("code",     {}).get("stringValue", ""),
                    "deviceId":  f.get("deviceId", {}).get("stringValue", ""),
                    "text":      f.get("text",     {}).get("stringValue", ""),
                    "fileUrl":   f.get("fileUrl",  {}).get("stringValue", ""),
                    "fileName":  f.get("fileName", {}).get("stringValue", ""),
                    "fileType":  f.get("fileType", {}).get("stringValue", ""),
                    "status":    f.get("status",   {}).get("stringValue", "pending"),
                    "readTime":  f.get("readTime", {}).get("stringValue", ""),
                })
        return results
    except Exception as e:
        logger.error(f"Firebase query error: {e}")
        return []


def fb_code_exists(code: str) -> bool:
    return bool(_query_by_field("code", code.lower()))


def fb_save_by_code(code: str, text: str, file_url="", file_name="", file_type="") -> str:
    """Save a message by code. Returns Firestore document name."""
    url = f"{FS_BASE}/messages?key={FIREBASE_KEY}"
    fields: dict = {
        "code":     {"stringValue": code.lower()},
        "text":     {"stringValue": text},
        "status":   {"stringValue": "pending"},
        "ts":       {"timestampValue": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
    }
    if file_url:
        fields["fileUrl"]  = {"stringValue": file_url}
        fields["fileName"] = {"stringValue": file_name}
        fields["fileType"] = {"stringValue": file_type}
    try:
        r = requests.post(url, json={"fields": fields}, timeout=10)
        return r.json().get("name", "")
    except Exception as e:
        logger.error(f"Firebase save error: {e}")
        return ""


def fb_save_by_id(device_id: str, text: str, file_url="", file_name="", file_type="") -> str:
    """Save a message by deviceId. Returns Firestore document name."""
    url = f"{FS_BASE}/messages?key={FIREBASE_KEY}"
    fields: dict = {
        "deviceId": {"stringValue": device_id},
        "text":     {"stringValue": text},
        "status":   {"stringValue": "pending"},
        "ts":       {"timestampValue": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
    }
    if file_url:
        fields["fileUrl"]  = {"stringValue": file_url}
        fields["fileName"] = {"stringValue": file_name}
        fields["fileType"] = {"stringValue": file_type}
    try:
        r = requests.post(url, json={"fields": fields}, timeout=10)
        return r.json().get("name", "")
    except Exception as e:
        logger.error(f"Firebase save error: {e}")
        return ""


def fb_get_by_code(code: str) -> list[dict]:
    return _query_by_field("code", code.lower())


def fb_check_status(doc_name: str) -> tuple[str, str]:
    """Return (status, readTime) for a given document."""
    url = f"https://firestore.googleapis.com/v1/{doc_name}?key={FIREBASE_KEY}"
    try:
        r = requests.get(url, timeout=10)
        f = r.json().get("fields", {})
        return (
            f.get("status",   {}).get("stringValue", "pending"),
            f.get("readTime", {}).get("stringValue", ""),
        )
    except Exception:
        return "pending", ""


def fb_mark_read(doc_name: str, read_time: str) -> None:
    """Mark a document as read."""
    url = (
        f"https://firestore.googleapis.com/v1/{doc_name}"
        f"?updateMask.fieldPaths=status&updateMask.fieldPaths=readTime"
        f"&key={FIREBASE_KEY}"
    )
    body = {
        "fields": {
            "status":   {"stringValue": "read"},
            "readTime": {"stringValue": read_time},
        }
    }
    try:
        requests.patch(url, json=body, timeout=10)
    except Exception as e:
        logger.error(f"Firebase mark-read error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_valid_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{4}", code))


def ask_mistral(prompt: str) -> str:
    """Send a prompt to Mistral AI and return the reply text."""
    headers = {
        "Authorization": f"Bearer {MISTRAL_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "mistral-small-latest",
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(MISTRAL_API_URL, json=body, headers=headers, timeout=30)
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Mistral API error: {e}")
        return "⚠️ Ошибка при обращении к ИИ. Попробуйте позже."


def now_time() -> str:
    return datetime.now().strftime("%H:%M")


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Send by ID",   callback_data="menu:send_id"),
            InlineKeyboardButton("🔐 Send by Code", callback_data="menu:send_code"),
        ],
        [
            InlineKeyboardButton("📥 Get by Code",  callback_data="menu:get_code"),
            InlineKeyboardButton("🪪 Get my ID",    callback_data="menu:get_id"),
        ],
        [
            InlineKeyboardButton("🧠 Neural Network", callback_data="menu:neural_network"),
        ],
    ])


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND POLLING
# ══════════════════════════════════════════════════════════════════════════════

async def poll_read_status(
    app: Application,
    row_id: int,
    sender_chat_id: int,
    status_msg_id: int,
    doc_name: str,
    content_label: str,
) -> None:
    """Poll Firestore every POLL_INTERVAL seconds; edit status message when read."""
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        status, read_time = fb_check_status(doc_name)
        if status == "read":
            try:
                await app.bot.edit_message_text(
                    chat_id=sender_chat_id,
                    message_id=status_msg_id,
                    text=f"{content_label} прочитан в {read_time or now_time()}",
                )
            except Exception as e:
                logger.warning(f"Could not edit status message: {e}")
            db.execute("DELETE FROM pending WHERE id=?", (row_id,))
            db.commit()
            return
    logger.info(f"Polling timed out for row {row_id}")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_states.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "Welcome to Septima!",
        reply_markup=main_keyboard(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK (button) HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    data = q.data

    if data == "menu:send_id":
        user_states[chat_id] = {"step": "send_id_content"}
        await q.message.reply_text(
            "Отправьте текст или файл для получения на устройство через ID:"
        )

    elif data == "menu:send_code":
        user_states[chat_id] = {"step": "send_code_content"}
        await q.message.reply_text(
            "Отправьте текст или файл для получения на устройство через Code:"
        )

    elif data == "menu:get_code":
        user_states[chat_id] = {"step": "get_code_enter_code"}
        await q.message.reply_text("Пришлите код для получения текста или файла")

    elif data == "menu:get_id":
        await q.message.reply_text(
            f"Ваш ID: <code>{chat_id}</code>\n\nДля получения уникального короткого ID напишите CEO.",
            parse_mode="HTML",
        )

    elif data == "menu:neural_network":
        await q.message.reply_text(
            "Для использования ИИ в начале вашего сообщения добавьте /ai или @ai.\n\n"
            "Пример:\n"
            "• /ai объясни что такое ИИ."
        )


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER (routes by state)
# ══════════════════════════════════════════════════════════════════════════════

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg     = update.message
    chat_id = msg.chat_id
    state   = user_states.get(chat_id, {})
    step    = state.get("step", "")

    # ── helpers to detect content ──────────────────────────────────────────
    is_file = bool(msg.document or msg.photo or msg.audio or msg.video or msg.voice)
    text_content = msg.text or msg.caption or ""
    content_label = "Файл" if is_file else "Текст"

    # ── AI via Mistral (/ai or @ai prefix) ───────────────────────────────
    ai_prefixes = ("/ai ", "@ai ")
    for prefix in ai_prefixes:
        if text_content.lower().startswith(prefix):
            prompt = text_content[len(prefix):].strip()
            if not prompt:
                await msg.reply_text("Введите вопрос после /ai. Пример: /ai объясни что такое ИИ.")
                return
            thinking_msg = await msg.reply_text("🧠 Думаю...")
            reply = await asyncio.get_event_loop().run_in_executor(None, ask_mistral, prompt)
            await thinking_msg.delete()
            await msg.reply_text(reply)
            return

    # ── GET BY ID ─────────────────────────────────────────────────────────────
    if step == "get_id_waiting":
        # User may have typed a custom device_id, or we use their own chat_id
        entered = text_content.strip()
        device_id = entered if entered else str(chat_id)

        docs = _query_by_field("deviceId", device_id)
        if not docs:
            await msg.reply_text(
                f"Нет сообщений для ID: <code>{device_id}</code>",
                parse_mode="HTML",
            )
            user_states.pop(chat_id, None)
            return

        # Deliver all pending messages for this device_id
        read_time = now_time()
        for doc in docs:
            if doc["status"] == "read":
                continue
            fb_mark_read(doc["doc_name"], read_time)
            if doc["fileUrl"]:
                file_ref = doc["fileUrl"]
                file_name = doc["fileName"] or "file"
                file_type = doc["fileType"] or ""
                is_tg_file_id = not file_ref.startswith("http")
                try:
                    if file_type.startswith("image/") and is_tg_file_id:
                        await msg.reply_photo(photo=file_ref, caption=doc["text"] or None)
                    elif file_type.startswith("video/") and is_tg_file_id:
                        await msg.reply_video(video=file_ref, caption=doc["text"] or None)
                    elif file_type.startswith("audio/") and is_tg_file_id:
                        if file_type == "audio/ogg":
                            await msg.reply_voice(voice=file_ref)
                        else:
                            await msg.reply_audio(audio=file_ref)
                    else:
                        await msg.reply_document(document=file_ref, filename=file_name)
                    if doc["text"] and not file_type.startswith("image/") and not file_type.startswith("video/"):
                        await msg.reply_text(doc["text"])
                except Exception as e:
                    logger.warning(f"Could not send file by ID: {e}")
                    await msg.reply_text("⚠️ Не удалось доставить файл.")
            else:
                await msg.reply_text(doc["text"] or "(пусто)")

        user_states.pop(chat_id, None)
        return

    # ── GET BY CODE ────────────────────────────────────────────────────────
    if step == "get_code_enter_code":
        code = text_content.strip()
        if not is_valid_code(code):
            await msg.reply_text("Данного кода не существует или он неверный")
            user_states.pop(chat_id, None)
            return

        docs = fb_get_by_code(code)
        if not docs:
            await msg.reply_text("Данного кода не существует или он неверный")
            user_states.pop(chat_id, None)
            return

        doc = docs[0]
        read_time = now_time()
        fb_mark_read(doc["doc_name"], read_time)

        # Deliver the content
        if doc["fileUrl"]:
            file_ref = doc["fileUrl"]
            file_name = doc["fileName"] or "file"
            file_type = doc["fileType"] or ""
            # Detect if it's a Telegram file_id (no http) or a real URL
            is_tg_file_id = not file_ref.startswith("http")
            try:
                if file_type.startswith("image/") and is_tg_file_id:
                    await msg.reply_photo(photo=file_ref)
                elif file_type.startswith("video/") and is_tg_file_id:
                    await msg.reply_video(video=file_ref)
                elif file_type.startswith("audio/") and is_tg_file_id:
                    if file_type == "audio/ogg":
                        await msg.reply_voice(voice=file_ref)
                    else:
                        await msg.reply_audio(audio=file_ref)
                else:
                    await msg.reply_document(
                        document=file_ref,
                        filename=file_name,
                    )
            except Exception as e:
                logger.warning(f"Could not send file: {e}")
                if doc["text"]:
                    await msg.reply_text(doc["text"])
                else:
                    await msg.reply_text("⚠️ Не удалось доставить файл.")
        else:
            await msg.reply_text(doc["text"] or "(пусто)")

        user_states.pop(chat_id, None)
        return

    # ── SEND BY CODE — step 1: receive content ────────────────────────────
    if step == "send_code_content":
        state["content_label"] = content_label

        if is_file:
            # Get file_id for later; we'll send file_id directly (no Cloudinary needed)
            if msg.document:
                state["file_id"]   = msg.document.file_id
                state["file_name"] = msg.document.file_name or "file"
                state["file_type"] = msg.document.mime_type or ""
            elif msg.photo:
                state["file_id"]   = msg.photo[-1].file_id
                state["file_name"] = "photo.jpg"
                state["file_type"] = "image/jpeg"
            elif msg.video:
                state["file_id"]   = msg.video.file_id
                state["file_name"] = msg.video.file_name or "video.mp4"
                state["file_type"] = msg.video.mime_type or "video/mp4"
            elif msg.audio:
                state["file_id"]   = msg.audio.file_id
                state["file_name"] = msg.audio.file_name or "audio"
                state["file_type"] = msg.audio.mime_type or "audio/*"
            elif msg.voice:
                state["file_id"]   = msg.voice.file_id
                state["file_name"] = "voice.ogg"
                state["file_type"] = "audio/ogg"
            state["text"] = text_content
        else:
            state["text"]    = text_content
            state["file_id"] = None

        state["step"] = "send_code_enter_code"
        user_states[chat_id] = state
        await msg.reply_text("Введите секретный код в формате 0000")
        return

    # ── SEND BY CODE — step 2: receive code ──────────────────────────────
    if step == "send_code_enter_code":
        code = text_content.strip()
        if not is_valid_code(code):
            await msg.reply_text("Код введен в неверном формате")
            return

        if fb_code_exists(code):
            await msg.reply_text("Данный секретный код занят. Введите другой")
            return

        # Save to Firebase (file_id as fileUrl — web won't use it, but bot can)
        file_id   = state.get("file_id", "")
        file_name = state.get("file_name", "")
        file_type = state.get("file_type", "")
        text      = state.get("text", "")
        label     = state.get("content_label", "Файл" if file_id else "Текст")

        doc_name = fb_save_by_code(
            code,
            text,
            file_url=file_id,
            file_name=file_name,
            file_type=file_type,
        )

        status_msg = await msg.reply_text(
            f"{label} отправлен по Code – {code}. Ожидает прочтения"
        )

        # Track in local DB for polling — store code in label for status message
        poll_label = f"{label} с Code {code.upper()}"
        cursor = db.execute(
            "INSERT INTO pending (sender_id, status_msg, doc_name, content_label) VALUES (?,?,?,?)",
            (chat_id, status_msg.message_id, doc_name, poll_label),
        )
        db.commit()
        row_id = cursor.lastrowid

        # Start background polling
        asyncio.create_task(
            poll_read_status(
                context.application,
                row_id,
                chat_id,
                status_msg.message_id,
                doc_name,
                poll_label,
            )
        )

        user_states.pop(chat_id, None)
        return

    # ── SEND BY ID — step 1: receive content ─────────────────────────────
    if step == "send_id_content":
        device_id = str(chat_id)

        if is_file:
            if msg.document:
                file_id   = msg.document.file_id
                file_name = msg.document.file_name or "file"
                file_type = msg.document.mime_type or ""
            elif msg.photo:
                file_id   = msg.photo[-1].file_id
                file_name = "photo.jpg"
                file_type = "image/jpeg"
            elif msg.video:
                file_id   = msg.video.file_id
                file_name = msg.video.file_name or "video.mp4"
                file_type = msg.video.mime_type or "video/mp4"
            elif msg.audio:
                file_id   = msg.audio.file_id
                file_name = msg.audio.file_name or "audio"
                file_type = msg.audio.mime_type or "audio/*"
            elif msg.voice:
                file_id   = msg.voice.file_id
                file_name = "voice.ogg"
                file_type = "audio/ogg"
            else:
                file_id, file_name, file_type = "", "", ""
        else:
            file_id, file_name, file_type = "", "", ""

        text  = text_content
        label = "Файл" if is_file else "Текст"

        doc_name = fb_save_by_id(
            device_id,
            text,
            file_url=file_id,
            file_name=file_name,
            file_type=file_type,
        )

        status_msg = await msg.reply_text(f"{label} отправлен. Ожидает прочтения.")

        cursor_row = db.execute(
            "INSERT INTO pending (sender_id, status_msg, doc_name, content_label) VALUES (?,?,?,?)",
            (chat_id, status_msg.message_id, doc_name, label),
        )
        db.commit()
        row_id = cursor_row.lastrowid

        asyncio.create_task(
            poll_read_status(
                context.application,
                row_id,
                chat_id,
                status_msg.message_id,
                doc_name,
                label,
            )
        )

        user_states.pop(chat_id, None)
        return

    # ── No active state — show menu ───────────────────────────────────────
    await msg.reply_text("Welcome to Septima!", reply_markup=main_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
# RESUME PENDING POLLS ON STARTUP
# ══════════════════════════════════════════════════════════════════════════════

async def resume_polls(app: Application) -> None:
    rows = db.execute("SELECT id, sender_id, status_msg, doc_name, content_label FROM pending").fetchall()
    for row_id, sender_id, status_msg, doc_name, label in rows:
        if not doc_name:
            continue
        asyncio.create_task(
            poll_read_status(app, row_id, sender_id, status_msg, doc_name, label)
        )
    logger.info(f"Resumed {len(rows)} pending poll(s)")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.Document.ALL | filters.PHOTO |
            filters.AUDIO | filters.VIDEO | filters.VOICE,
            message_handler,
        )
    )

    app.post_init = resume_polls

    logger.info("Septima bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()