"""
Private AI Telegram Bot
==========================
Bot Telegram (mode polling) yang menghubungkan user ke:
  - Sistem 3 Tier model chat/coding, dipilih user lewat /model:
        🟢 Light  -> qwen2.5-coder:1.5b  (kuota token x1)
        🟡 Medium -> qwen2.5-coder:7b    (kuota token x2, default)
        🔴 Heavy  -> qwen2.5-coder:14b   (kuota token x3)
  - qwen2.5vl  -> analisis gambar & video (vision)

Fitur:
  - Setiap user mendapat kuota 50.000 TOKEN/hari (reset otomatis tiap 24 jam),
    dipotong sesuai jumlah token asli dari Ollama dikali multiplier tier model.
  - Owner bisa generate kode redeem (/gencode) untuk menaikkan kuota token user
    lain, termasuk ke "unlimited", dengan masa berlaku dalam hari. Setelah
    masa berlaku habis, otomatis kembali ke kuota default (50.000/hari).
  - Upload dokumen/PDF/DOCX/gambar/video/ZIP tetap didukung penuh.
  - Panggilan ke Ollama (bisa memakan waktu s/d 10 menit) dijalankan di thread
    terpisah (asyncio.to_thread) dan Application berjalan dengan
    concurrent_updates aktif, sehingga bot TIDAK hang/freeze untuk user lain
    saat ada request yang sedang diproses lama.

Jalankan dengan:
    python3 bot.py
(env vars wajib: TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID — lihat install.sh)
"""

import os
import io
import re
import asyncio
import hashlib
import logging
import html
import subprocess
import tempfile
import traceback
from typing import Optional

from telegram import (
    Update, constants, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest

import database as db
import ai_engine as engine
import github_backup as ghbackup

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

# --- GitHub Backup & Restore (otomatis, dikonfigurasi lewat install.sh) ---
# Catatan desain: backup HANYA lewat GitHub REST API (lihat github_backup.py),
# TIDAK PERNAH memakai git di working directory bot (yang juga dipakai untuk
# `git pull` source code lewat install.sh) — supaya proses update kode tidak
# pernah bisa menimpa/menghapus database, dan supaya PAT tidak perlu
# tersimpan di remote URL git.
GH_BACKUP_ENABLED = os.environ.get("GH_BACKUP_ENABLED", "false").lower() == "true"
GH_BACKUP_PAT = os.environ.get("GH_BACKUP_PAT", "")
GH_BACKUP_REPO = os.environ.get("GH_BACKUP_REPO", "")  # format "owner/repo"
GH_BACKUP_BRANCH = os.environ.get("GH_BACKUP_BRANCH", "main")
GH_BACKUP_PATH = os.environ.get("GH_BACKUP_PATH", "bot_data.db.gz")
GH_BACKUP_INTERVAL_SEC = 60

PIN_TAG_RE = re.compile(r"\[PIN\]", re.IGNORECASE)

MAX_TELEGRAM_FILE_MB = 50  # batas file yang bisa diunduh bot lewat Bot API (server lokal bisa lebih besar)
MAX_TELEGRAM_MSG_LEN = 4000  # sedikit di bawah batas Telegram 4096 agar ada ruang untuk formatting

# --- Auto-Register Commands (muncul di menu "/" Telegram, kiri bawah kotak chat) ---
# Didaftarkan sekali saat startup lewat application.bot.set_my_commands (lihat
# _post_init_start_backup). Command admin didaftarkan TERPISAH dengan scope
# BotCommandScopeChat(OWNER_ID) supaya HANYA muncul di menu owner -- user biasa
# tidak melihat /gencode dkk di menu mereka sama sekali (tetap tertolak juga
# secara otorisasi di masing-masing handler, ini murni soal kerapian UX menu).
PUBLIC_COMMANDS = [
    BotCommand("start", "Mulai & lihat cara pakai bot"),
    BotCommand("help", "Bantuan & daftar perintah"),
    BotCommand("status", "Lihat sisa kuota token & model aktif (profil)"),
    BotCommand("model", "Pilih Role & Tier model AI"),
    BotCommand("redeem", "Tukar kode redeem"),
    BotCommand("reset", "Hapus riwayat chat, mulai baru"),
]
OWNER_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("gencode", "[Owner] Buat kode redeem baru"),
    BotCommand("codes", "[Owner] Lihat kode redeem belum dipakai"),
    BotCommand("users", "[Owner] Lihat daftar user"),
    BotCommand("ban", "[Owner] Nonaktifkan akses user"),
    BotCommand("unban", "[Owner] Aktifkan kembali akses user"),
    BotCommand("broadcast", "[Owner] Kirim pesan ke semua user"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # kurangi noise log dari library HTTP internal

def model_full_label(role: str, tier: str) -> str:
    """Label lengkap 'Role - Tier (nama_model)' dipakai di /status dan konfirmasi /model."""
    role_label = engine.ROLE_LABELS.get(role, role)
    tier_label = engine.TIER_SHORT_LABELS.get(tier, tier)
    model_name = engine.resolve_model(role, tier)
    lock_suffix = " 🔒" if _model_lock_reason(role, tier) else ""
    return f"{role_label} · {tier_label} ({model_name}){lock_suffix}"


def model_short_label(role: str, tier: str) -> str:
    """Label pendek 'Role · Tier' dipakai di /users (ringkasan admin)."""
    role_label = engine.ROLE_LABELS.get(role, role)
    tier_label = engine.TIER_SHORT_LABELS.get(tier, tier)
    lock_suffix = " 🔒" if _model_lock_reason(role, tier) else ""
    return f"{role_label} · {tier_label}{lock_suffix}"
GPU_DISABLED_IMAGE_MESSAGE = (
    "⚠️ Maaf, server saat ini berjalan tanpa GPU (CPU-Only). Fitur analisis gambar "
    "dinonaktifkan demi menjaga stabilitas server."
)


# =========================================================================
# DETEKSI GPU OTOMATIS (SAAT STARTUP)
# =========================================================================

def detect_gpu() -> bool:
    """
    Mendeteksi apakah server punya GPU yang bisa dipakai, dicek lewat 2 cara
    (mana saja yang berhasil duluan dianggap cukup):
      1. Command `nvidia-smi` tersedia & bisa dijalankan (GPU NVIDIA terpasang + driver aktif).
      2. `torch.cuda.is_available()` True (jika library torch terinstall).
    Dipanggil sekali saat startup dan hasilnya disimpan ke variabel global HAS_GPU,
    dipakai sebagai guardrail sebelum bot mengirim request analisis gambar ke Ollama.
    """
    # Cara 1: cek command nvidia-smi
    try:
        result = subprocess_run_nvidia_smi()
        if result:
            logger.info("Deteksi GPU: nvidia-smi tersedia dan berhasil dijalankan. GPU terdeteksi.")
            return True
    except Exception as e:
        logger.debug("Deteksi GPU via nvidia-smi gagal/tidak tersedia: %s", e)

    # Cara 2: cek torch.cuda.is_available() (opsional, hanya jika torch terinstall)
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            logger.info("Deteksi GPU: torch.cuda.is_available() = True. GPU terdeteksi.")
            return True
    except ImportError:
        logger.debug("Library torch tidak terinstall, melewati pengecekan torch.cuda.is_available().")
    except Exception as e:
        logger.debug("Deteksi GPU via torch gagal: %s", e)

    logger.warning(
        "Tidak ada GPU terdeteksi (nvidia-smi maupun torch.cuda tidak tersedia/aktif). "
        "Bot akan berjalan mode CPU-Only, fitur analisis gambar akan dinonaktifkan."
    )
    return False


def subprocess_run_nvidia_smi() -> bool:
    """Menjalankan `nvidia-smi` dan mengembalikan True jika command ada dan exit code 0."""
    import subprocess
    import shutil as _shutil

    if _shutil.which("nvidia-smi") is None:
        return False

    proc = subprocess.run(
        ["nvidia-smi"],
        capture_output=True,
        timeout=10,
    )
    return proc.returncode == 0


def detect_gpu_profile() -> dict:
    """Mendeteksi GPU + perkiraan VRAM agar tier berat bisa dikunci dengan aman."""
    profile = {
        "has_gpu": False,
        "backend": None,
        "gpu_count": 0,
        "max_vram_gb": 0.0,
    }

    try:
        if subprocess_run_nvidia_smi():
            mem_query = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if mem_query.returncode == 0:
                values = []
                for line in mem_query.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        values.append(float(line) / 1024.0)
                    except ValueError:
                        continue
                if values:
                    profile.update(
                        {
                            "has_gpu": True,
                            "backend": "nvidia-smi",
                            "gpu_count": len(values),
                            "max_vram_gb": max(values),
                        }
                    )
                    return profile
    except Exception as e:
        logger.debug("Deteksi GPU via nvidia-smi gagal/tidak tersedia: %s", e)

    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            values = []
            for idx in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(idx)
                values.append(props.total_memory / (1024 ** 3))
            if values:
                profile.update(
                    {
                        "has_gpu": True,
                        "backend": "torch",
                        "gpu_count": len(values),
                        "max_vram_gb": max(values),
                    }
                )
                return profile
    except ImportError:
        logger.debug("Library torch tidak terinstall, melewati pengecekan torch.cuda.is_available().")
    except Exception as e:
        logger.debug("Deteksi GPU via torch gagal: %s", e)

    logger.warning(
        "Tidak ada GPU terdeteksi (nvidia-smi maupun torch.cuda tidak tersedia/aktif). "
        "Bot akan berjalan mode CPU-Only, fitur analisis gambar akan dinonaktifkan."
    )
    return profile


# Dideteksi sekali saat modul di-load (startup bot). Dipakai sebagai guardrail
# di handle_photo agar server CPU-only tidak dipaksa memproses request gambar
# yang berat dan bisa mengganggu stabilitas (lihat GPU_DISABLED_IMAGE_MESSAGE).
GPU_PROFILE: dict = detect_gpu_profile()
HAS_GPU: bool = bool(GPU_PROFILE.get("has_gpu"))
GPU_MAX_VRAM_GB: float = float(GPU_PROFILE.get("max_vram_gb") or 0.0)


# =========================================================================
# HELPER
# Ambang VRAM konservatif untuk tier yang benar-benar berat.
TIER_MIN_VRAM_GB = {
    "heavy": 12.0,
    "qwen_14b": 12.0,
    "phi3_medium": 12.0,
    "codellama_13b": 12.0,
    "qwen_32b": 24.0,
    "deepseek_r1_32b": 24.0,
    "mixtral_8x7b": 24.0,
    "gemma2_27b": 24.0,
    "codellama_34b": 24.0,
    "yi_34b": 24.0,
    "llama31_70b": 48.0,
    "qwen_72b": 48.0,
    "deepseek_r1_70b": 48.0,
    "llama3_405b": 96.0,
}


def _required_vram_gb(tier: str) -> float:
    return float(TIER_MIN_VRAM_GB.get(tier, 0.0))


def _model_lock_reason(role: str, tier: str) -> Optional[str]:
    """Kembalikan alasan jika model harus dikunci karena hardware/cluster tidak cukup."""
    model_name = engine.ROLE_TIERS.get(role, {}).get(tier)
    if not model_name:
        return "Model tidak valid."

    if engine.CLUSTER_MODE == "master":
        try:
            import node_manager

            candidates = node_manager.get_candidate_nodes(model_name)
            if candidates:
                return None
            if not engine.MASTER_OLLAMA_FALLBACK:
                return f"Model '{model_name}' belum tersedia di worker cluster saat ini."
        except Exception as exc:
            logger.debug("Gagal mengecek kandidat worker untuk model %s: %s", model_name, exc)

    required_vram = _required_vram_gb(tier)
    if required_vram <= 0:
        return None

    if not HAS_GPU:
        return (
            f"Model '{model_name}' dikunci karena server ini CPU-only. "
            f"Butuh GPU minimal {required_vram:.0f} GB VRAM."
        )

    if GPU_MAX_VRAM_GB < required_vram:
        return (
            f"Model '{model_name}' dikunci karena GPU hanya {GPU_MAX_VRAM_GB:.0f} GB VRAM. "
            f"Butuh minimal {required_vram:.0f} GB VRAM."
        )

    return None

# =========================================================================

def is_owner(telegram_id: int) -> bool:
    return telegram_id == OWNER_ID


def user_token_for(telegram_id: int) -> str:
    """Token unik dipakai sebagai key riwayat chat di database (isolasi per-user)."""
    return f"tg-{telegram_id}"


def format_number_id(n: int) -> str:
    """Format angka dengan pemisah ribuan gaya Indonesia (titik), misal 50000 -> '50.000'."""
    return f"{n:,}".replace(",", ".")


async def _reply_text_safe(update: Update, text: str):
    """
    Kirim pesan dengan parse_mode Markdown agar code block/format AI tampil rapi di
    Telegram. Jika Markdown dari AI malformed (BadRequest), fallback kirim ulang
    sebagai plain text agar pesan tetap terkirim dan bot tidak crash.
    """
    try:
        return await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        logger.warning("Markdown parse gagal (%s), fallback ke plain text.", e)
        return await update.effective_message.reply_text(text)


async def send_long_message(update: Update, text: str) -> None:
    """Telegram membatasi 4096 karakter per pesan; pesan panjang dipecah otomatis."""
    if not text:
        text = "[Jawaban kosong]"
    for i in range(0, len(text), MAX_TELEGRAM_MSG_LEN):
        chunk = text[i : i + MAX_TELEGRAM_MSG_LEN]
        await _reply_text_safe(update, chunk)


AUTO_FILE_THRESHOLD = 3000

# Ekstensi bahasa fenced-code-block -> ekstensi file yang dikirim sebagai Document
CODE_LANG_EXT = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts",
    "ts": "ts", "jsx": "jsx", "tsx": "tsx", "html": "html", "css": "css", "json": "json",
    "bash": "sh", "sh": "sh", "shell": "sh", "yaml": "yaml", "yml": "yaml", "sql": "sql",
    "java": "java", "c": "c", "cpp": "cpp", "go": "go", "rust": "rs", "php": "php",
    "xml": "xml", "markdown": "md", "md": "md",
}
FIRST_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n")


def _guess_file_extension(text: str) -> str:
    m = FIRST_FENCE_RE.search(text)
    if m:
        lang = m.group(1).lower()
        if lang in CODE_LANG_EXT:
            return CODE_LANG_EXT[lang]
    return "txt"


async def _send_as_document(update: Update, text: str, caption: str) -> None:
    ext = _guess_file_extension(text)
    with tempfile.NamedTemporaryFile(mode="w", suffix=f".{ext}", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            await update.effective_message.reply_document(
                document=f, filename=f"jawaban.{ext}", caption=caption[:1000]
            )
    finally:
        os.remove(tmp_path)


# =========================================================================
# FEEDBACK VISUAL: EDIT PESAN BERKALA SAAT AI SEDANG MEMPROSES
# =========================================================================
# Untuk proses panjang (chat & ekstraksi file), user melihat SATU pesan yang
# isinya berganti secara berkala ("Membaca konteks..." -> "Menyusun jawaban...")
# alih-alih pesan "sedang mengetik" yang statis. Diimplementasikan sebagai
# task asyncio terpisah yang berjalan PARALEL dengan pemrosesan sebenarnya
# (asyncio.to_thread ke ai_engine), bukan menyisipkan callback ke ai_engine --
# ai_engine.py sengaja tetap sinkron & tidak perlu tahu apa pun soal Telegram.

PROGRESS_STAGES = [
    (0, "🧠 Membaca konteks percakapan..."),
    (4, "🔎 Menyusun jawaban..."),
    (8, "⏳ Model sedang berpikir lebih dalam (pertanyaan/berkas ini cukup kompleks)..."),
    (18, "⏳ Masih diproses, mohon tunggu (model besar bisa makan waktu beberapa menit)..."),
]


class ProgressReporter:
    """
    Context manager async: mengirim 1 pesan lalu mengeditnya secara berkala
    mengikuti PROGRESS_STAGES selama blok `async with` berjalan. Pesan
    progres otomatis dihapus begitu blok selesai (baik sukses maupun error)
    supaya tidak menyampah histori chat -- balasan asli AI dikirim terpisah
    lewat deliver_ai_reply seperti biasa.

    Semua error Telegram (rate limit, pesan dihapus manual oleh user, dll)
    ditelan diam-diam di sini -- progres visual adalah "nice to have", tidak
    boleh sampai membuat proses utama (chat/file) gagal gara-gara gagal edit.
    """

    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._chat_id = update.effective_chat.id
        self._bot = context.bot
        self._message = None
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "ProgressReporter":
        try:
            self._message = await self._bot.send_message(self._chat_id, PROGRESS_STAGES[0][1])
        except TelegramError as e:
            logger.debug("ProgressReporter: gagal kirim pesan progres awal: %s", e)
            self._message = None
        if self._message is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        try:
            for delay_sec, text in PROGRESS_STAGES[1:]:
                await asyncio.sleep(delay_sec)  # jeda relatif sejak tahap sebelumnya
                try:
                    await self._message.edit_text(text)
                except BadRequest:
                    pass  # isi identik/pesan sudah dihapus -- aman diabaikan
                except TelegramError as e:
                    logger.debug("ProgressReporter: gagal edit pesan progres: %s", e)
        except asyncio.CancelledError:
            pass

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._message is not None:
            try:
                await self._message.delete()
            except TelegramError:
                pass  # user mungkin sudah menghapusnya manual, atau sudah kedaluwarsa


async def deliver_ai_reply(update: Update, reply_text: str) -> None:
    """
    Menangani fleksibilitas respons AI:
      - Deteksi tag [PIN] -> hapus tag, kirim, lalu pin pesan tersebut.
      - Jika teks > 3000 karakter -> kirim sebagai file Document, bukan dipecah jadi banyak pesan.
      - Selain itu -> kirim sebagai pesan teks biasa (dipecah otomatis jika perlu).
    """
    should_pin = bool(PIN_TAG_RE.search(reply_text))
    clean_text = PIN_TAG_RE.sub("", reply_text).strip() or "[Jawaban kosong]"

    if len(clean_text) > AUTO_FILE_THRESHOLD:
        preview = clean_text[:200].strip()
        await _send_as_document(update, clean_text, caption=f"📄 Jawaban lengkap (teks panjang)\n{preview}...")
        sent_message = update.effective_message
    else:
        for i in range(0, len(clean_text), MAX_TELEGRAM_MSG_LEN):
            chunk = clean_text[i : i + MAX_TELEGRAM_MSG_LEN]
            sent_message = await _reply_text_safe(update, chunk)

    if should_pin:
        try:
            await sent_message.pin(disable_notification=True)
        except TelegramError as e:
            logger.warning("Gagal pin pesan: %s", e)


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
# BACKGROUND TASK: AUTO-BACKUP DATABASE KE GITHUB TIAP 60 DETIK
# (lihat github_backup.py — upload lewat REST API, tanpa git sama sekali)
# =========================================================================

def _file_hash(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


async def github_auto_backup_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Loop background: cek hash database tiap 60 detik, upload ke GitHub hanya jika berubah."""
    last_hash: Optional[str] = None
    while True:
        try:
            current_hash = _file_hash(DB_PATH)
            if current_hash is not None and current_hash != last_hash:
                ok = await asyncio.to_thread(
                    ghbackup.push_backup,
                    GH_BACKUP_PAT,
                    GH_BACKUP_REPO,
                    GH_BACKUP_PATH,
                    GH_BACKUP_BRANCH,
                    DB_PATH,
                )
                if ok:
                    last_hash = current_hash
        except Exception:
            logger.exception("GH backup: error tak terduga di loop auto-backup")
        await asyncio.sleep(GH_BACKUP_INTERVAL_SEC)


async def _post_init_start_backup(application: Application) -> None:
    # --- Auto-Register Commands ke menu Telegram ("/" di kiri bawah kotak chat) ---
    # Scope default (PUBLIC_COMMANDS) berlaku untuk semua user; scope khusus
    # OWNER_ID (OWNER_COMMANDS, superset yang menyertakan command admin) HANYA
    # berlaku di chat pribadi owner dengan bot. Kegagalan di sini (mis. Telegram
    # API sedang bermasalah) tidak boleh menghentikan bot start, jadi dibungkus
    # try/except dan hanya di-log sebagai warning.
    try:
        await application.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
        await application.bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(OWNER_ID))
        logger.info("Menu command Telegram berhasil didaftarkan (%d publik, %d untuk owner).",
                    len(PUBLIC_COMMANDS), len(OWNER_COMMANDS))
    except TelegramError as e:
        logger.warning("Gagal mendaftarkan menu command ke Telegram (bot tetap jalan normal): %s", e)

    if not (GH_BACKUP_ENABLED and GH_BACKUP_PAT and GH_BACKUP_REPO):
        logger.info(
            "GH backup: dinonaktifkan (GH_BACKUP_ENABLED != true, atau PAT/repo belum diset)."
        )
    else:
        application.create_task(github_auto_backup_loop(application))
        logger.info(
            "GH backup: auto-backup ke %s (%s) tiap %ss AKTIF.",
            GH_BACKUP_REPO, GH_BACKUP_PATH, GH_BACKUP_INTERVAL_SEC,
        )

    # --- Distributed Cluster Architecture: Master Node Load Balancer ---
    # Hanya relevan jika CLUSTER_MODE=master (diset oleh install.sh saat memilih
    # "Install Master Node"). Health-check loop berjalan di background thread
    # terpisah (lihat node_manager.py) supaya tidak mengganggu event loop bot.
    if engine.CLUSTER_MODE == "master":
        import node_manager
        node_manager.start_health_check_loop()
        logger.info(
            "Cluster Mode AKTIF (master): health-check Worker Node tiap %ss.",
            node_manager.NODE_HEALTH_CHECK_INTERVAL_SECONDS,
        )
    else:
        logger.info("Cluster Mode: standalone (memanggil Ollama lokal langsung, tanpa Worker Node).")


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
        "/status - lihat sisa kuota token & model aktif kamu hari ini\n"
        "/model - pilih tier model AI (Light/Medium/Heavy)\n"
        "/redeem <kode> - tukar kode redeem untuk menaikkan kuota token\n"
        "/reset - hapus riwayat chat kamu (mulai percakapan baru)\n"
        "/help - tampilkan bantuan ini"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


# =========================================================================
def _tier_button_locked(role: str, tier: str) -> bool:
    return _model_lock_reason(role, tier) is not None


# COMMAND: /model - pilih Role (General/Coder) lalu Tier, lewat Inline Keyboard
# =========================================================================
# Alur (state disimpan di Telegram sendiri lewat callback_data, BUKAN di memori bot,
# supaya aman dipakai bareng banyak user sekaligus tanpa risiko state nyasar/tabrakan
# antar user -- lihat catatan di callback_model_role/callback_model_tier):
#   1. /model           -> tampilkan 2 tombol role: General Chat / Coder-IT
#   2. tap role         -> callback_data "model_role:<role>", tampilkan tombol tier
#                          untuk role itu + tombol "⬅️ Kembali"
#   3. tap tier         -> callback_data "model_tier:<role>:<tier>", terapkan &
#                          konfirmasi, tombol tetap tampil (bisa ganti lagi langsung)
#   4. tap "⬅️ Kembali" -> callback_data "model_back", kembali ke langkah 1

def _role_keyboard(current_role: str) -> InlineKeyboardMarkup:
    buttons = []
    for role in engine.ROLE_ORDER:
        label = engine.ROLE_LABELS[role]
        if role == current_role:
            label += " ✅"
        buttons.append([InlineKeyboardButton(label, callback_data=f"model_role:{role}")])
    return InlineKeyboardMarkup(buttons)


def _tier_keyboard(role: str, current_role: str, current_tier: str) -> InlineKeyboardMarkup:
    buttons = []
    for tier in engine.ROLE_TIER_ORDER[role]:
        locked = _tier_button_locked(role, tier)
        label = f"{engine.TIER_SHORT_LABELS[tier]} ({engine.ROLE_TIERS[role][tier]})"
        if locked:
            label = f"🔒 {label}"
        if role == current_role and tier == current_tier:
            label += " ✅"
        callback_data = f"model_lock:{role}:{tier}" if locked else f"model_tier:{role}:{tier}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback_data)])
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="model_back")])
    return InlineKeyboardMarkup(buttons)


def _role_select_text(current_role: str, current_tier: str) -> str:
    return (
        "🤖 *Pilih Role AI*\n\n"
        "🗣️ *General Chat* — buat ngobrol santai, tanya-tanya umum, curhat, dll.\n"
        "💻 *Coder / IT* — buat ngoding, debugging, pertanyaan teknis IT.\n\n"
        f"Model aktif kamu saat ini: *{model_full_label(current_role, current_tier)}*"
    )


def _tier_select_text(role: str, current_role: str, current_tier: str) -> str:
    role_label = engine.ROLE_LABELS[role]
    lines = [f"🤖 *Pilih Tier untuk {role_label}*\n"]
    for tier in engine.ROLE_TIER_ORDER[role]:
        model_name = engine.ROLE_TIERS[role][tier]
        tier_label = engine.TIER_SHORT_LABELS[tier]
        desc = engine.TIER_DESCRIPTIONS[tier]
        multiplier = engine.TOKEN_MULTIPLIER[tier]
        locked = _tier_button_locked(role, tier)
        status = "🔒 Terkunci" if locked else f"Kuota token x{multiplier}."
        if locked:
            reason = _model_lock_reason(role, tier)
            if reason:
                status = f"🔒 Terkunci — {reason}"
        lines.append(f"{tier_label} — `{model_name}`\n   {desc} {status}\n")
    lines.append(f"Model aktif kamu saat ini: *{model_full_label(current_role, current_tier)}*")
    return "\n".join(lines)
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    u = db.get_or_create_user(user.id, user.username)
    current_role, current_tier = u["model_role"], u["model_tier"]

    await update.message.reply_text(
        _role_select_text(current_role, current_tier),
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=_role_keyboard(current_role),
    )


async def callback_model_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 1 -> 2: user memilih role, tampilkan pilihan tier untuk role tsb."""
    query = update.callback_query
    user = query.from_user
    selected_role = (query.data or "").split(":", 1)[-1]

    if selected_role not in db.VALID_MODEL_ROLES:
        await query.answer("❌ Role tidak valid.", show_alert=True)
        return

    u = db.get_or_create_user(user.id, user.username)
    current_role, current_tier = u["model_role"], u["model_tier"]

    await query.answer()
    try:
        await query.edit_message_text(
            _tier_select_text(selected_role, current_role, current_tier),
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_tier_keyboard(selected_role, current_role, current_tier),
        )
    except TelegramError:
        pass


async def callback_model_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback untuk tier yang terkunci. Hanya tampilkan alasan, jangan ubah model."""
    query = update.callback_query
    payload = (query.data or "").split(":")
    if len(payload) != 3:
        await query.answer("❌ Data tombol tidak valid.", show_alert=True)
        return
    _, role, tier = payload
    reason = _model_lock_reason(role, tier)
    if not reason:
        await query.answer("Model ini tidak terkunci.", show_alert=False)
        return
    await query.answer(f"🔒 {reason}", show_alert=True)


async def callback_model_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback untuk tier yang terkunci. Hanya tampilkan alasan, jangan ubah model."""
    query = update.callback_query
    payload = (query.data or "").split(":")
    if len(payload) != 3:
        await query.answer("❌ Data tombol tidak valid.", show_alert=True)
        return
    _, role, tier = payload
    reason = _model_lock_reason(role, tier)
    if not reason:
        await query.answer("Model ini tidak terkunci.", show_alert=False)
        return
    await query.answer(f"🔒 {reason}", show_alert=True)


async def callback_model_tier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 2: user memilih tier di dalam role yang sudah dipilih -> terapkan."""
    query = update.callback_query
    user = query.from_user
    payload = (query.data or "").split(":")  # ["model_tier", "<role>", "<tier>"]

    if len(payload) != 3:
        await query.answer("❌ Data tombol tidak valid.", show_alert=True)
        return
    _, role, tier = payload

    # Validasi ganda: role & tier masing-masing valid, DAN kombinasinya benar-benar
    # tersedia (mis. role 'general' tidak punya tier 'heavy') -- dicek langsung ke
    # ai_engine.ROLE_TIERS (single source of truth) supaya tombol yang sengaja
    # dipalsukan (callback_data custom dari luar bot) tidak bisa lolos.
    if role not in db.VALID_MODEL_ROLES or tier not in db.VALID_MODEL_TIERS:
        await query.answer("❌ Role/tier tidak valid.", show_alert=True)
        return
    if tier not in engine.ROLE_TIERS.get(role, {}):
        await query.answer(f"❌ Tier '{tier}' tidak tersedia untuk role ini.", show_alert=True)
        return

    reason = _model_lock_reason(role, tier)
    if reason:
        await query.answer(f"🔒 {reason}", show_alert=True)
        return

    model_name = engine.ROLE_TIERS[role][tier]
    needs_local_pull = engine.CLUSTER_MODE != "master"
    if engine.CLUSTER_MODE == "master":
        try:
            import node_manager

            needs_local_pull = not node_manager.get_candidate_nodes(model_name) and engine.MASTER_OLLAMA_FALLBACK
        except Exception as exc:
            logger.debug("Gagal cek kandidat worker saat memilih model %s: %s", model_name, exc)
            needs_local_pull = engine.MASTER_OLLAMA_FALLBACK

    if needs_local_pull:
        try:
            await asyncio.to_thread(engine.ensure_model_ready, model_name)
        except engine.AIEngineError as exc:
            await query.answer(f"❌ {exc.user_message}", show_alert=True)
            return

    db.get_or_create_user(user.id, user.username)
    db.set_model_role_tier(user.id, role, tier)

    label = model_short_label(role, tier)
    await query.answer(f"Model diganti ke {label}")
    try:
        await query.edit_message_text(
            f"✅ Model aktif kamu sekarang: *{model_full_label(role, tier)}*\n\n"
            "Kirim pesan untuk mulai chat dengan model ini.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_tier_keyboard(role, role, tier),
        )
    except TelegramError:
        pass
    except TelegramError:
        pass


async def callback_model_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol '⬅️ Kembali' di layar tier -> kembali ke layar pilih role."""
    query = update.callback_query
    user = query.from_user
    u = db.get_or_create_user(user.id, user.username)
    current_role, current_tier = u["model_role"], u["model_tier"]

    await query.answer()
    try:
        await query.edit_message_text(
            _role_select_text(current_role, current_tier),
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_role_keyboard(current_role),
        )
    except TelegramError:
        pass
async def callback_model_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol '⬅️ Kembali' di layar tier -> kembali ke layar pilih role."""
    query = update.callback_query
    user = query.from_user
    u = db.get_or_create_user(user.id, user.username)
    current_role, current_tier = u["model_role"], u["model_tier"]

    await query.answer()
    try:
        await query.edit_message_text(
            _role_select_text(current_role, current_tier),
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_role_keyboard(current_role),
        )
    except TelegramError:
        pass
async def callback_model_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol '⬅️ Kembali' di layar tier -> kembali ke layar pilih role."""
    query = update.callback_query
    user = query.from_user
    u = db.get_or_create_user(user.id, user.username)
    current_role, current_tier = u["model_role"], u["model_tier"]

    await query.answer()
    try:
        await query.edit_message_text(
            _role_select_text(current_role, current_tier),
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_role_keyboard(current_role),
        )
    except TelegramError:
        pass


# =========================================================================
# COMMAND: /status
# =========================================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    u = db.get_or_create_user(user.id, user.username)

    role, tier = u["model_role"], u["model_tier"]
    model_line = f"Model aktif: *{model_full_label(role, tier)}*"
    lock_reason = _model_lock_reason(role, tier)
    if lock_reason:
        model_line += f"\n⚠️ {lock_reason}"

    if u["is_unlimited"]:
        limit_line = "♾️ *Unlimited*"
        sisa_line = f"Berlaku sampai: {format_time_remaining(u['plan_expires_at'])}"
    else:
        sisa = max(u["token_limit"] - u["tokens_used"], 0)
        limit_line = (
            f"{format_number_id(u['tokens_used'])}/{format_number_id(u['token_limit'])} "
            "token terpakai hari ini"
        )
        sisa_line = f"Sisa: *{format_number_id(sisa)}* token"
        if u["plan_expires_at"]:
            sisa_line += f"\nPlan aktif sampai: {format_time_remaining(u['plan_expires_at'])}"

    text = f"📊 *Status Kamu*\n\n{model_line}\n{limit_line}\n{sisa_line}"
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
        token_value=record["token_value"],
        is_unlimited=bool(record["is_unlimited"]),
        duration_days=record["duration_days"],
    )
    db.mark_code_redeemed(record["code"], user.id)

    if record["is_unlimited"]:
        plan_desc = "♾️ *Unlimited token*"
    else:
        plan_desc = f"*{format_number_id(record['token_value'])} token/hari*"

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
      /gencode <jumlah_token> <hari>   -> misal /gencode 100000 30
      /gencode unlimited <hari>        -> misal /gencode unlimited 365 (khusus VIP/Owner)
    """
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⛔ Perintah ini khusus owner.")
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Format:\n"
            "`/gencode <jumlah_token> <hari>` — contoh: `/gencode 100000 30`\n"
            "`/gencode unlimited <hari>` — contoh: `/gencode unlimited 365`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    token_arg, days_arg = context.args

    try:
        duration_days = int(days_arg)
        if duration_days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Jumlah hari harus berupa angka bulat positif.")
        return

    if token_arg.lower() == "unlimited":
        code = db.create_redeem_code(
            token_value=-1, is_unlimited=True, duration_days=duration_days, created_by=user.id
        )
        plan_desc = "Unlimited token (VIP/Owner)"
    else:
        try:
            token_value = int(token_arg)
            if token_value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Jumlah token harus berupa angka bulat positif, atau kata kunci `unlimited`.",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            return
        code = db.create_redeem_code(
            token_value=token_value, is_unlimited=False, duration_days=duration_days, created_by=user.id
        )
        plan_desc = f"{format_number_id(token_value)} token/hari"

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
        plan = "Unlimited" if c["is_unlimited"] else f"{format_number_id(c['token_value'])} token/hari"
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
        tier_flag = model_short_label(u["model_role"], u["model_tier"])
        if u["is_unlimited"]:
            plan = "Unlimited"
        else:
            plan = f"{format_number_id(u['tokens_used'])}/{format_number_id(u['token_limit'])} tok"
        ban_flag = " 🚫BANNED" if u["is_banned"] else ""
        lines.append(f"`{u['telegram_id']}` {uname} {tier_flag} — {plan}{ban_flag}")

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
    Mengecek apakah user boleh chat. Jika tidak (kuota token habis / banned), kirim pesan
    penjelasan dan return None. Jika boleh, return data user.
    """
    u = db.get_or_create_user(telegram_id, username)

    if u["is_banned"]:
        await update.effective_message.reply_text("🚫 Akun kamu telah dinonaktifkan oleh owner.")
        return None

    if not db.can_use(u):
        sisa = max(u["token_limit"] - u["tokens_used"], 0)
        await update.effective_message.reply_text(
            f"⚠️ Kuota token harian kamu sudah habis "
            f"({format_number_id(u['tokens_used'])}/{format_number_id(u['token_limit'])} token, sisa {sisa}).\n\n"
            "Kuota akan reset otomatis besok, atau kamu bisa naikkan kuota dengan kode redeem:\n"
            "`/redeem <kode>`\n\n"
            "Tips: model tier *Light* memotong kuota lebih sedikit (x1) dibanding *Heavy* (x3). "
            "Ganti tier lewat /model.\n\n"
            "Hubungi owner bot untuk mendapatkan kode redeem.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return None

    return u


def _deduct_tokens_for_tier(telegram_id: int, tier: str, total_tokens: int) -> None:
    multiplier = engine.TOKEN_MULTIPLIER.get(tier, 1)
    deduction = total_tokens * multiplier
    db.add_token_usage(telegram_id, deduction)


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

    role, tier = u["model_role"], u["model_tier"]
    lock_reason = _model_lock_reason(role, tier)
    if lock_reason:
        await message.reply_text(f"🔒 {lock_reason}\nPilih model yang sesuai hardware lewat /model.")
        return

    model_name = engine.resolve_model(role, tier)
    token = user_token_for(telegram_user.id)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)

    try:
        previous_history = db.get_history(token, limit=engine.MAX_HISTORY_MESSAGES)
        db.save_message(token, "user", user_message)

        # Panggilan ke Ollama bisa memakan waktu sampai 10 menit (timeout 600s).
        # Dijalankan di thread terpisah agar event loop bot TIDAK terblokir/hang
        # dan user lain tetap bisa dilayani secara bersamaan. ProgressReporter
        # mengedit satu pesan secara berkala selama menunggu (feedback visual).
        async with ProgressReporter(update, context):
            result = await asyncio.to_thread(engine.chat, user_message, previous_history, model_name, role)

        reply_text = result["content"]
        db.save_message(token, "assistant", reply_text)
        _deduct_tokens_for_tier(telegram_user.id, tier, result["total_tokens"])

        await deliver_ai_reply(update, reply_text)
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

    role, tier = u["model_role"], u["model_tier"]
    lock_reason = _model_lock_reason(role, tier)
    if lock_reason:
        await message.reply_text(f"🔒 {lock_reason}\nPilih model yang sesuai hardware lewat /model.")
        return

    model_name = engine.resolve_model(role, tier)
    token = user_token_for(telegram_user.id)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)
    # Catatan: filename berasal dari user (nama file asli mereka) dan bisa berisi karakter
    # spesial Markdown (backtick, asterisk, underscore) yang bisa membuat parser Telegram
    # gagal dan melempar error jika dikirim dengan parse_mode Markdown. Kirim sebagai teks
    # polos agar aman untuk filename apa pun.
    await message.reply_text(f"⏳ Memproses '{filename}'...")

    try:
        # Ekstraksi/analisis file (termasuk panggilan vision) juga bisa memakan waktu lama
        # (video/ZIP besar), jalankan di thread terpisah agar bot tetap responsif.
        async with ProgressReporter(update, context):
            result = await asyncio.to_thread(engine.process_uploaded_file, filename, content)
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

        async with ProgressReporter(update, context):
            result_chat = await asyncio.to_thread(engine.chat, user_message, previous_history, model_name, role)
        reply_text = result_chat["content"]

        db.save_message(token, "assistant", reply_text)
        _deduct_tokens_for_tier(telegram_user.id, tier, result_chat["total_tokens"])

        if result["truncated"]:
            await message.reply_text("ℹ️ Catatan: hasil ekstraksi file terpotong karena terlalu panjang.")

        await deliver_ai_reply(update, reply_text)
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
    # Guardrail GPU: analisis gambar (vision) cukup berat untuk CPU-only server.
    # Jika server tidak punya GPU, tolak request di sini sebelum sempat mengunduh
    # file atau memanggil Ollama sama sekali, demi menjaga stabilitas server.
    if not HAS_GPU:
        await update.effective_message.reply_text(GPU_DISABLED_IMAGE_MESSAGE)
        return

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

    if HAS_GPU:
        logger.info("GPU terdeteksi. Fitur analisis gambar (vision) AKTIF.")
    else:
        logger.warning(
            "GPU TIDAK terdeteksi (mode CPU-Only). Fitur analisis gambar (vision) DINONAKTIFKAN "
            "untuk user demi menjaga stabilitas server."
        )

    # concurrent_updates diaktifkan agar panggilan Ollama yang lama (s/d 10 menit) untuk
    # satu user TIDAK memblokir/hang bot untuk user lain yang sedang chat bersamaan.
    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(8)
        .post_init(_post_init_start_backup)
        .build()
    )

    # Command handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("model", cmd_model))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("redeem", cmd_redeem))

    # Callback query (inline button) handler untuk /model (alur 2 langkah: role -> tier)
    application.add_handler(CallbackQueryHandler(callback_model_role, pattern=r"^model_role:"))
    application.add_handler(CallbackQueryHandler(callback_model_lock, pattern=r"^model_lock:"))
    application.add_handler(CallbackQueryHandler(callback_model_tier, pattern=r"^model_tier:"))
    application.add_handler(CallbackQueryHandler(callback_model_back, pattern=r"^model_back$"))

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
        "Private AI Telegram Bot siap. Role+Tier model: %s | Model vision: %s | Ollama host: %s | Owner ID: %s",
        engine.ROLE_TIERS, engine.OLLAMA_VISION_MODEL, engine.OLLAMA_HOST, OWNER_ID,
    )
    logger.info("Menjalankan bot dalam mode polling...")

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
