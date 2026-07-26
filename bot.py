"""
Private AI Telegram Bot
==========================
Bot Telegram (mode polling) yang menghubungkan user ke:
  - qwen2.5-coder:14b  -> chat/coding (text-only)
  - qwen2.5vl          -> analisis gambar & video (vision)

Fitur:
  - Setiap user mendapat limit 20 chat/hari (reset otomatis tiap hari)
  - Owner bisa generate kode redeem (/gencode) untuk menaikkan limit user
    lain, termasuk ke "unlimited", dengan masa berlaku dalam hari. Setelah
    masa berlaku habis, otomatis kembali ke limit default (20/hari).
  - Upload dokumen/PDF/DOCX/gambar/video/ZIP tetap didukung penuh.

Jalankan dengan:
    python3 bot.py
(env vars wajib: TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID — lihat install.sh)
"""

import os
import io
import logging
import html
import traceback
from typing import Optional

from telegram import Update, constants
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

import database as db
import ai_engine as engine

# =========================================================================
# KONFIGURASI
# =========================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN tidak diset. Bot menolak untuk start tanpa token bot Telegram. "
        "Set environment variable TELEGRAM_BOT_TOKEN sebelum menjalankan bot."
    )

_owner_raw = os.environ.get("OWNER_TELEGRAM_ID")
if not _owner_raw:
    raise RuntimeError(
        "OWNER_TELEGRAM_ID tidak diset. Bot menolak untuk start tanpa ID owner "
        "(dipakai untuk otorisasi command admin seperti /gencode). "
        "Set environment variable OWNER_TELEGRAM_ID sebelum menjalankan bot."
    )
try:
    OWNER_ID = int(_owner_raw)
except ValueError:
    raise RuntimeError("OWNER_TELEGRAM_ID harus berupa angka (Telegram user ID).")

DB_PATH = os.environ.get("AI_BOT_DB_PATH", "bot_data.db")
db.set_db_path(DB_PATH)

MAX_TELEGRAM_FILE_MB = 50  # batas file yang bisa diunduh bot lewat Bot API (server lokal bisa lebih besar)
MAX_TELEGRAM_MSG_LEN = 4000  # sedikit di bawah batas Telegram 4096 agar ada ruang untuk formatting

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # kurangi noise log dari library HTTP internal


# =========================================================================
# HELPER
# =========================================================================

def is_owner(telegram_id: int) -> bool:
    return telegram_id == OWNER_ID


def user_token_for(telegram_id: int) -> str:
    """Token unik dipakai sebagai key riwayat chat di database (isolasi per-user)."""
    return f"tg-{telegram_id}"


async def send_long_message(update: Update, text: str) -> None:
    """Telegram membatasi 4096 karakter per pesan; pesan panjang dipecah otomatis."""
    if not text:
        text = "[Jawaban kosong]"
    for i in range(0, len(text), MAX_TELEGRAM_MSG_LEN):
        chunk = text[i : i + MAX_TELEGRAM_MSG_LEN]
        await update.effective_message.reply_text(chunk)


def format_time_remaining(expires_at: Optional[float]) -> str:
    if expires_at is None:
        return "-"
    import time
    remaining = expires_at - time.time()
    if remaining <= 0:
        return "kedaluwarsa"
    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    if days > 0:
        return f"{days} hari {hours} jam lagi"
    return f"{hours} jam lagi"


def escape_markdown_v1(text: str) -> str:
    """
    Meloloskan (escape) karakter spesial Markdown legacy Telegram (_ * ` [) dari
    teks yang berasal dari user (misalnya nama tampilan Telegram), agar aman
    disisipkan ke pesan yang dikirim dengan parse_mode Markdown tanpa membuat
    Telegram menolak pesan karena entity yang tidak seimbang/rusak.
    """
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# =========================================================================
# COMMAND: /start, /help
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.get_or_create_user(user.id, user.username)
    safe_name = escape_markdown_v1(user.first_name or "there")
    text = (
        f"👋 Halo {safe_name}!\n\n"
        "Saya adalah *Private AI Assistant*. Kirim pesan apa saja untuk mulai chat/coding, "
        "atau kirim file (dokumen, PDF, DOCX, gambar, video, ZIP) untuk saya analisis.\n\n"
        "Perintah yang tersedia:\n"
        "/status - lihat sisa limit chat kamu hari ini\n"
        "/redeem <kode> - tukar kode redeem untuk menaikkan limit\n"
        "/reset - hapus riwayat chat kamu (mulai percakapan baru)\n"
        "/help - tampilkan bantuan ini"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


# =========================================================================
# COMMAND: /status
# =========================================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    u = db.get_or_create_user(user.id, user.username)

    if u["is_unlimited"]:
        limit_line = "♾️ *Unlimited*"
        sisa_line = f"Berlaku sampai: {format_time_remaining(u['plan_expires_at'])}"
    else:
        sisa = max(u["daily_limit"] - u["chats_used_today"], 0)
        limit_line = f"{u['chats_used_today']}/{u['daily_limit']} chat terpakai hari ini"
        sisa_line = f"Sisa: *{sisa}* chat"
        if u["plan_expires_at"]:
            sisa_line += f"\nPlan aktif sampai: {format_time_remaining(u['plan_expires_at'])}"

    text = f"📊 *Status Kamu*\n\n{limit_line}\n{sisa_line}"
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)


# =========================================================================
# COMMAND: /reset
# =========================================================================

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.clear_history(user_token_for(user.id))
    await update.message.reply_text("✅ Riwayat chat kamu sudah dihapus. Mulai percakapan baru!")


# =========================================================================
# COMMAND: /redeem <kode>
# =========================================================================

async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.get_or_create_user(user.id, user.username)

    if not context.args:
        await update.message.reply_text("Format: `/redeem KODE`", parse_mode=constants.ParseMode.MARKDOWN)
        return

    code = context.args[0].strip()
    record = db.get_redeem_code(code)

    if record is None:
        await update.message.reply_text("❌ Kode redeem tidak ditemukan.")
        return

    if record["redeemed_by"] is not None:
        await update.message.reply_text("❌ Kode ini sudah pernah digunakan.")
        return

    db.apply_plan(
        telegram_id=user.id,
        limit_value=record["limit_value"],
        is_unlimited=bool(record["is_unlimited"]),
        duration_days=record["duration_days"],
    )
    db.mark_code_redeemed(record["code"], user.id)

    if record["is_unlimited"]:
        plan_desc = "♾️ *Unlimited chat*"
    else:
        plan_desc = f"*{record['limit_value']} chat/hari*"

    await update.message.reply_text(
        f"✅ Kode berhasil ditukar!\n\nPlan kamu sekarang: {plan_desc}\n"
        f"Berlaku selama: *{record['duration_days']} hari*",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


# =========================================================================
# COMMAND ADMIN (OWNER ONLY): /gencode, /codes, /users, /ban, /unban, /broadcast
# =========================================================================

async def cmd_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Format:
      /gencode <limit> <hari>       -> misal /gencode 50 30
      /gencode unlimited <hari>     -> misal /gencode unlimited 365
    """
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Format:\n"
            "`/gencode <limit> <hari>` — contoh: `/gencode 50 30`\n"
            "`/gencode unlimited <hari>` — contoh: `/gencode unlimited 365`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    limit_arg, days_arg = context.args

    try:
        duration_days = int(days_arg)
        if duration_days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Jumlah hari harus berupa angka bulat positif.")
        return

    if limit_arg.lower() == "unlimited":
        code = db.create_redeem_code(
            limit_value=0, is_unlimited=True, duration_days=duration_days, created_by=user.id
        )
        plan_desc = "Unlimited chat"
    else:
        try:
            limit_value = int(limit_arg)
            if limit_value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Limit harus berupa angka bulat positif, atau kata kunci `unlimited`.",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            return
        code = db.create_redeem_code(
            limit_value=limit_value, is_unlimited=False, duration_days=duration_days, created_by=user.id
        )
        plan_desc = f"{limit_value} chat/hari"

    await update.message.reply_text(
        f"✅ Kode redeem berhasil dibuat:\n\n"
        f"`{code}`\n\n"
        f"Plan: *{plan_desc}*\n"
        f"Masa berlaku setelah redeem: *{duration_days} hari*\n\n"
        f"Bagikan kode ini ke user, lalu mereka tinggal kirim `/redeem {code}`",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan kode redeem yang belum dipakai (owner only)."""
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return

    codes = db.list_codes(limit=30, only_unused=True)
    if not codes:
        await update.message.reply_text("Tidak ada kode redeem yang belum dipakai.")
        return

    lines = ["🎟️ *Kode Redeem Aktif (belum dipakai)*\n"]
    for c in codes:
        plan = "Unlimited" if c["is_unlimited"] else f"{c['limit_value']} chat/hari"
        lines.append(f"`{c['code']}` — {plan}, {c['duration_days']} hari")

    await send_long_message(update, "\n".join(lines))


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan daftar user terbaru (owner only)."""
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return

    total = db.count_users()
    users = db.list_users(limit=30)

    lines = [f"👥 *Total user: {total}*\n"]
    for u in users:
        uname = f"@{u['username']}" if u["username"] else "(no username)"
        plan = "Unlimited" if u["is_unlimited"] else f"{u['chats_used_today']}/{u['daily_limit']}"
        ban_flag = " 🚫BANNED" if u["is_banned"] else ""
        lines.append(f"`{u['telegram_id']}` {uname} — {plan}{ban_flag}")

    await send_long_message(update, "\n".join(lines))


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return
    if not context.args:
        await update.message.reply_text("Format: `/ban <telegram_id>`", parse_mode=constants.ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID Telegram harus berupa angka.")
        return
    db.set_banned(target_id, True)
    await update.message.reply_text(f"🚫 User `{target_id}` telah diban.", parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return
    if not context.args:
        await update.message.reply_text("Format: `/unban <telegram_id>`", parse_mode=constants.ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID Telegram harus berupa angka.")
        return
    db.set_banned(target_id, False)
    await update.message.reply_text(f"✅ User `{target_id}` telah di-unban.", parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengirim pesan ke semua user terdaftar (owner only)."""
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return
    if not context.args:
        await update.message.reply_text("Format: `/broadcast <pesan>`", parse_mode=constants.ParseMode.MARKDOWN)
        return

    message_text = " ".join(context.args)
    users = db.list_users(limit=100000)

    sent, failed = 0, 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["telegram_id"], text=f"📢 {message_text}")
            sent += 1
        except TelegramError:
            failed += 1

    await update.message.reply_text(f"✅ Broadcast selesai. Terkirim: {sent}, gagal: {failed}.")


# =========================================================================
# LIMIT ENFORCEMENT (dipakai sebelum setiap chat/upload diproses)
# =========================================================================

async def enforce_limit_or_reply(update: Update, telegram_id: int, username: Optional[str]) -> Optional[dict]:
    """
    Mengecek apakah user boleh chat. Jika tidak (limit habis / banned), kirim pesan
    penjelasan dan return None. Jika boleh, return data user.
    """
    u = db.get_or_create_user(telegram_id, username)

    if u["is_banned"]:
        await update.effective_message.reply_text("🚫 Akun kamu telah dinonaktifkan oleh owner.")
        return None

    if not db.can_chat(u):
        await update.effective_message.reply_text(
            f"⚠️ Limit chat harian kamu ({u['daily_limit']}/hari) sudah habis.\n\n"
            "Limit akan reset otomatis besok, atau kamu bisa naikkan limit dengan kode redeem:\n"
            "`/redeem <kode>`\n\n"
            "Hubungi owner bot untuk mendapatkan kode redeem.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return None

    return u


# =========================================================================
# HANDLER: PESAN TEKS BIASA (CHAT/CODING)
# =========================================================================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    message = update.effective_message
    user_message = (message.text or "").strip()

    if not user_message:
        return

    u = await enforce_limit_or_reply(update, telegram_user.id, telegram_user.username)
    if u is None:
        return

    token = user_token_for(telegram_user.id)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)

    try:
        previous_history = db.get_history(token, limit=engine.MAX_HISTORY_MESSAGES)
        db.save_message(token, "user", user_message)

        reply_text = engine.chat(token, user_message, previous_history)

        db.save_message(token, "assistant", reply_text)
        db.increment_usage(telegram_user.id)

        await send_long_message(update, reply_text)
    except engine.AIEngineError as e:
        logger.error("AIEngineError saat chat: %s", e)
        await message.reply_text(f"❌ {e.user_message}")
    except Exception:
        logger.exception("Error tak terduga saat memproses chat")
        await message.reply_text("❌ Terjadi error tak terduga saat memproses pesan kamu. Coba lagi.")


# =========================================================================
# HANDLER: UPLOAD FILE (DOKUMEN, PDF, DOCX, GAMBAR, VIDEO, ZIP)
# =========================================================================

async def _download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    return buf.getvalue()


async def _process_and_reply_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE, filename: str, content: bytes
) -> None:
    telegram_user = update.effective_user
    message = update.effective_message

    u = await enforce_limit_or_reply(update, telegram_user.id, telegram_user.username)
    if u is None:
        return

    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_TELEGRAM_FILE_MB:
        await message.reply_text(
            f"❌ File terlalu besar ({size_mb:.1f} MB). Batas maksimal {MAX_TELEGRAM_FILE_MB} MB."
        )
        return

    token = user_token_for(telegram_user.id)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)
    # Catatan: filename berasal dari user (nama file asli mereka) dan bisa berisi karakter
    # spesial Markdown (backtick, asterisk, underscore) yang bisa membuat parser Telegram
    # gagal dan melempar error jika dikirim dengan parse_mode Markdown. Kirim sebagai teks
    # polos agar aman untuk filename apa pun.
    await message.reply_text(f"⏳ Memproses '{filename}'...")

    try:
        result = engine.process_uploaded_file(filename, content)
        extracted_text = result["extracted_text"]

        caption = (message.caption or "").strip()
        if caption:
            user_message = (
                f"[User mengirim file '{filename}' ({result['file_kind']}) dengan pesan: \"{caption}\"]\n\n"
                f"Berikut hasil ekstraksi/analisis file tersebut:\n\n{extracted_text}"
            )
        else:
            user_message = (
                f"[User mengirim file '{filename}' ({result['file_kind']}) tanpa pesan tambahan.]\n\n"
                f"Berikut hasil ekstraksi/analisis file tersebut:\n\n{extracted_text}"
            )

        previous_history = db.get_history(token, limit=engine.MAX_HISTORY_MESSAGES)
        db.save_message(token, "user", user_message)

        reply_text = engine.chat(token, user_message, previous_history)

        db.save_message(token, "assistant", reply_text)
        db.increment_usage(telegram_user.id)

        if result["truncated"]:
            await message.reply_text("ℹ️ Catatan: hasil ekstraksi file terpotong karena terlalu panjang.")

        await send_long_message(update, reply_text)
    except engine.AIEngineError as e:
        logger.error("AIEngineError saat memproses file: %s", e)
        await message.reply_text(f"❌ {e.user_message}")
    except Exception:
        logger.exception("Error tak terduga saat memproses file")
        await message.reply_text("❌ Terjadi error tak terduga saat memproses file kamu. Coba lagi.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.effective_message.document
    filename = doc.file_name or f"file_{doc.file_unique_id}"
    content = await _download_telegram_file(context, doc.file_id)
    await _process_and_reply_file(update, context, filename, content)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telegram mengirim beberapa resolusi; ambil yang terbesar (kualitas terbaik)
    photo = update.effective_message.photo[-1]
    filename = f"photo_{photo.file_unique_id}.jpg"
    content = await _download_telegram_file(context, photo.file_id)
    await _process_and_reply_file(update, context, filename, content)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    video = update.effective_message.video
    filename = video.file_name or f"video_{video.file_unique_id}.mp4"
    content = await _download_telegram_file(context, video.file_id)
    await _process_and_reply_file(update, context, filename, content)


# =========================================================================
# ERROR HANDLER GLOBAL
# =========================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception saat menangani update:", exc_info=context.error)
    tb_string = "".join(
        traceback.format_exception(None, context.error, context.error.__traceback__)
    )
    logger.error(tb_string)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Terjadi error internal. Owner bot sudah diberi tahu."
            )
        except TelegramError:
            pass

    # Beri tahu owner untuk error yang tidak tertangani, agar bisa segera dicek
    try:
        error_summary = html.escape(str(context.error))[:500]
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ Bot error:\n<code>{error_summary}</code>",
            parse_mode=constants.ParseMode.HTML,
        )
    except TelegramError:
        pass


# =========================================================================
# ENTRY POINT
# =========================================================================

def main() -> None:
    db.init_db()

    if not engine.ffmpeg_available():
        logger.warning(
            "ffmpeg/ffprobe tidak ditemukan di PATH. Analisis video tidak akan berfungsi "
            "sampai ffmpeg diinstall (sudo apt-get install ffmpeg)."
        )

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("redeem", cmd_redeem))

    # Owner-only admin commands
    application.add_handler(CommandHandler("gencode", cmd_gencode))
    application.add_handler(CommandHandler("codes", cmd_codes))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # File handlers
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))

    # Pesan teks biasa (chat/coding) — harus didaftarkan setelah handler command & file
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    application.add_error_handler(error_handler)

    logger.info(
        "Private AI Telegram Bot siap. Model chat: %s | Model vision: %s | Ollama host: %s | Owner ID: %s",
        engine.OLLAMA_MODEL, engine.OLLAMA_VISION_MODEL, engine.OLLAMA_HOST, OWNER_ID,
    )
    logger.info("Menjalankan bot dalam mode polling...")

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
