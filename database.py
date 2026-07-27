"""
Private AI Telegram Bot - Database Layer (SQLite)
====================================================
Menyimpan tiga hal:
  - chats          : riwayat percakapan per user (untuk memori AI)
  - users          : status kuota TOKEN harian tiap user, model tier aktif,
                      plan aktif & masa berlaku
  - redeem_codes   : kode redeem yang dibuat owner untuk menaikkan kuota token user

=====================================================================
CATATAN MIGRASI (v2 - Sistem Token + 3 Tier Model)
=====================================================================
Versi sebelumnya memakai kuota "N chat/hari" (kolom daily_limit,
chats_used_today). Versi ini beralih ke kuota berbasis TOKEN
(kolom token_limit, tokens_used) + kolom model_tier per-user
(light/medium/heavy). init_db() otomatis menambahkan kolom baru ke
database lama lewat ALTER TABLE bila belum ada, sehingga proses
`update` (lihat install.sh) aman dijalankan tanpa menghapus database
maupun riwayat chat yang sudah ada.
"""

import sqlite3
import time
import secrets
import string
import logging
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

logger = logging.getLogger("ai-bot.database")

DB_PATH = "bot_data.db"

# Kuota token harian default untuk user baru (di-reset tiap 24 jam / ganti hari UTC)
DEFAULT_DAILY_TOKEN_LIMIT = 50_000

# Role + tier default untuk user baru. Role menentukan "kategori" (general chat vs
# coder/IT), tier menentukan ukuran model di dalam role tsb. Kombinasi ini HARUS
# selalu valid terhadap ai_engine.ROLE_TIERS (general/medium -> llama3.1:8b).
DEFAULT_MODEL_ROLE = "general"
DEFAULT_MODEL_TIER = "medium"
VALID_MODEL_ROLES = ("general", "coder", "extended")
# Catatan: validasi apakah kombinasi role+tier benar-benar ada dilakukan di
# ai_engine.resolve_model (fallback otomatis), bukan di sini -- supaya database
# tidak perlu tahu daftar tier yang valid untuk tiap role (single source of truth
# ada di ai_engine.ROLE_TIERS). Daftar tier di bawah mencakup SEMUA tier lintas
# role (general/coder/extended) -- termasuk 20+ model role "extended" dari
# ultra ringan (CPU-only) sampai ultra heavy (wajib GPU besar/multi-GPU).
VALID_MODEL_TIERS = (
    "super_ringan", "light", "medium", "heavy",
    "ultra_ringan", "tinyllama", "gemma_2b", "phi3_mini", "llama32_3b", "qwen_4b",
    "mistral_7b", "llama31_8b", "gemma2_9b", "qwen_14b", "deepseek_r1_8b",
    "phi3_medium", "codellama_13b",
    "qwen_32b", "deepseek_r1_32b", "mixtral_8x7b", "gemma2_27b", "codellama_34b", "yi_34b",
    "llama31_70b", "qwen_72b", "deepseek_r1_70b", "llama3_405b",
)


def set_db_path(path: str) -> None:
    global DB_PATH
    DB_PATH = path


@contextmanager
def get_db():
    """Context manager untuk koneksi SQLite yang aman (auto close, auto commit/rollback)."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Menambahkan kolom baru ke tabel yang sudah ada (dari instalasi versi lama)
    tanpa menghapus data. Aman dipanggil berulang kali (idempotent) — setiap
    kolom hanya ditambahkan jika belum ada.
    """
    # --- users: kolom sistem token + model tier ---
    user_cols = _existing_columns(conn, "users")
    if "model_tier" not in user_cols:
        conn.execute(
            f"ALTER TABLE users ADD COLUMN model_tier TEXT NOT NULL DEFAULT '{DEFAULT_MODEL_TIER}'"
        )
        logger.info("Migrasi DB: kolom 'model_tier' ditambahkan ke tabel users.")
    if "token_limit" not in user_cols:
        conn.execute(
            f"ALTER TABLE users ADD COLUMN token_limit INTEGER NOT NULL DEFAULT {DEFAULT_DAILY_TOKEN_LIMIT}"
        )
        logger.info("Migrasi DB: kolom 'token_limit' ditambahkan ke tabel users.")
    if "tokens_used" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0")
        logger.info("Migrasi DB: kolom 'tokens_used' ditambahkan ke tabel users.")
    if "model_role" not in user_cols:
        # PENTING: kolom ini baru (v3 - sistem Role + Tier). Sebelum v3, SEMUA user
        # cuma punya model_tier yang selalu memetakan ke keluarga model qwen2.5-coder
        # (tidak ada role "general" sama sekali). Kalau kita backfill kolom baru ini
        # dengan DEFAULT_MODEL_ROLE ("general"), user LAMA yang sudah terbiasa pakai
        # model coder akan diam-diam pindah ke model general (llama) begitu mereka
        # chat lagi -- itu perubahan perilaku yang tidak diinginkan untuk user lama.
        # Solusinya: tambah kolom dengan DEFAULT SQL 'coder' dulu (berlaku untuk baris
        # yang sudah ada), baru user BARU (INSERT setelah migrasi ini) yang eksplisit
        # diberi DEFAULT_MODEL_ROLE ("general") lewat get_or_create_user().
        conn.execute("ALTER TABLE users ADD COLUMN model_role TEXT NOT NULL DEFAULT 'coder'")
        logger.info(
            "Migrasi DB: kolom 'model_role' ditambahkan ke tabel users, "
            "user lama di-backfill ke role 'coder' (mempertahankan perilaku sebelumnya)."
        )

    # --- redeem_codes: kolom token_value (pengganti limit_value / chat-based) ---
    code_cols = _existing_columns(conn, "redeem_codes")
    if "token_value" not in code_cols:
        conn.execute("ALTER TABLE redeem_codes ADD COLUMN token_value INTEGER NOT NULL DEFAULT 0")
        logger.info("Migrasi DB: kolom 'token_value' ditambahkan ke tabel redeem_codes.")


def init_db() -> None:
    """Membuat semua tabel jika belum ada, lalu menjalankan migrasi kolom baru jika perlu."""
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_token TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                message TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_token ON chats(user_token);")

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen REAL NOT NULL,
                model_role TEXT NOT NULL DEFAULT '{DEFAULT_MODEL_ROLE}',
                model_tier TEXT NOT NULL DEFAULT '{DEFAULT_MODEL_TIER}',
                token_limit INTEGER NOT NULL DEFAULT {DEFAULT_DAILY_TOKEN_LIMIT},
                tokens_used INTEGER NOT NULL DEFAULT 0,
                is_unlimited INTEGER NOT NULL DEFAULT 0,
                plan_expires_at REAL,
                usage_date TEXT NOT NULL,
                is_banned INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                token_value INTEGER NOT NULL DEFAULT 0,
                is_unlimited INTEGER NOT NULL DEFAULT 0,
                duration_days INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL,
                redeemed_by INTEGER,
                redeemed_at REAL
            );
            """
        )

        # --- worker_nodes: registry cluster (Master-Worker Architecture) ---
        # Menyimpan konfigurasi tiap Worker Node (host/port/API key) + cache metrik
        # kesehatan terakhir (diisi oleh node_manager.health_check_loop). Tabel ini
        # HANYA relevan/berisi data di Master Node -- Worker Node tidak menulis ke sini.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 3716,
                api_key TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown',
                cpu_usage REAL,
                ram_usage REAL,
                active_tasks INTEGER,
                latency_ms REAL,
                models_available TEXT,
                last_checked REAL,
                created_at REAL NOT NULL
            );
            """
        )

        # Migrasi kolom baru untuk database yang dibuat oleh versi sebelumnya
        _migrate_schema(conn)

    logger.info("Database siap: %s", DB_PATH)


# =========================================================================
# CHAT HISTORY
# =========================================================================

def save_message(user_token: str, role: str, message: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chats (user_token, role, message, timestamp) VALUES (?, ?, ?, ?)",
            (user_token, role, message, time.time()),
        )


def get_history(user_token: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if limit:
            rows = conn.execute(
                """
                SELECT id, user_token, role, message, timestamp
                FROM (
                    SELECT * FROM chats WHERE user_token = ?
                    ORDER BY id DESC LIMIT ?
                )
                ORDER BY id ASC
                """,
                (user_token, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_token, role, message, timestamp FROM chats WHERE user_token = ? ORDER BY id ASC",
                (user_token,),
            ).fetchall()
    return [dict(row) for row in rows]


def clear_history(user_token: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM chats WHERE user_token = ?", (user_token,))


# =========================================================================
# USERS & KUOTA TOKEN HARIAN
# =========================================================================

def _today_str() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def get_or_create_user(telegram_id: int, username: Optional[str]) -> Dict[str, Any]:
    """Mengambil data user, membuatnya jika belum ada. Otomatis reset counter jika hari berganti."""
    today = _today_str()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO users
                    (telegram_id, username, first_seen, model_role, model_tier, token_limit, tokens_used,
                     is_unlimited, plan_expires_at, usage_date, is_banned)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL, ?, 0)
                """,
                (
                    telegram_id, username, time.time(),
                    DEFAULT_MODEL_ROLE, DEFAULT_MODEL_TIER, DEFAULT_DAILY_TOKEN_LIMIT, today,
                ),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return dict(row)

        user = dict(row)

        # Update username jika berubah
        if username and user["username"] != username:
            conn.execute(
                "UPDATE users SET username = ? WHERE telegram_id = ?", (username, telegram_id)
            )
            user["username"] = username

        # Reset kuota token harian jika tanggal sudah berganti
        if user["usage_date"] != today:
            conn.execute(
                "UPDATE users SET tokens_used = 0, usage_date = ? WHERE telegram_id = ?",
                (today, telegram_id),
            )
            user["tokens_used"] = 0
            user["usage_date"] = today

        # Jika plan berbayar (kuota tinggi/unlimited) sudah kedaluwarsa, turunkan kembali ke default
        if user["plan_expires_at"] is not None and time.time() > user["plan_expires_at"]:
            conn.execute(
                """
                UPDATE users
                SET token_limit = ?, is_unlimited = 0, plan_expires_at = NULL
                WHERE telegram_id = ?
                """,
                (DEFAULT_DAILY_TOKEN_LIMIT, telegram_id),
            )
            user["token_limit"] = DEFAULT_DAILY_TOKEN_LIMIT
            user["is_unlimited"] = 0
            user["plan_expires_at"] = None

    return user


def can_use(user: Dict[str, Any]) -> bool:
    """Mengecek apakah user masih boleh memakai bot (belum diban & kuota token masih ada)."""
    if user["is_banned"]:
        return False
    if user["is_unlimited"]:
        return True
    return user["tokens_used"] < user["token_limit"]


def add_token_usage(telegram_id: int, tokens_to_deduct: int) -> None:
    """Menambah pemakaian token user (sudah dikalikan multiplier tier model oleh pemanggil)."""
    if tokens_to_deduct <= 0:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET tokens_used = tokens_used + ? WHERE telegram_id = ?",
            (tokens_to_deduct, telegram_id),
        )


def set_model_role_tier(telegram_id: int, role: str, tier: str) -> None:
    """
    Mengubah role DAN tier model user sekaligus dalam satu UPDATE (atomik), dipanggil
    setelah user menyelesaikan alur 2 langkah /model (pilih role -> pilih tier).
    Validasi kombinasi role+tier yang benar-benar tersedia (mis. role 'general' tidak
    punya tier 'heavy') sengaja TIDAK dilakukan di sini -- itu tanggung jawab
    ai_engine.resolve_model tempat mapping role->tier->model_name didefinisikan
    (single source of truth), supaya database tidak perlu duplikasi daftar tier
    yang valid untuk tiap role.
    """
    if role not in VALID_MODEL_ROLES:
        raise ValueError(f"Role model tidak valid: {role}")
    if tier not in VALID_MODEL_TIERS:
        raise ValueError(f"Tier model tidak valid: {tier}")
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET model_role = ?, model_tier = ? WHERE telegram_id = ?",
            (role, tier, telegram_id),
        )


def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return dict(row) if row else None


def apply_plan(telegram_id: int, token_value: int, is_unlimited: bool, duration_days: int) -> None:
    """
    Menerapkan hasil redeem code ke akun user: kuota token baru + masa berlaku baru
    (hari ini + duration_days), dan reset pemakaian token hari ini ke 0.
    Mengasumsikan user sudah ada (panggil get_or_create_user terlebih dahulu) — jika tidak,
    raise error secara eksplisit alih-alih diam-diam tidak melakukan apa pun.
    """
    expires_at = time.time() + duration_days * 86400
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE users
            SET token_limit = ?, is_unlimited = ?, plan_expires_at = ?, tokens_used = 0
            WHERE telegram_id = ?
            """,
            (token_value, 1 if is_unlimited else 0, expires_at, telegram_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(
                f"apply_plan dipanggil untuk telegram_id={telegram_id} yang belum terdaftar. "
                "Panggil get_or_create_user() terlebih dahulu."
            )


def set_banned(telegram_id: int, banned: bool) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_banned = ? WHERE telegram_id = ?",
            (1 if banned else 0, telegram_id),
        )


def list_users(limit: int = 50) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY first_seen DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_users() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return row["c"]


# =========================================================================
# REDEEM CODES (BERBASIS TOKEN)
# =========================================================================

def _generate_code(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_redeem_code(
    token_value: int, is_unlimited: bool, duration_days: int, created_by: int
) -> str:
    """Membuat kode redeem baru (kuota token) yang unik dan menyimpannya ke database."""
    with get_db() as conn:
        for _ in range(10):  # coba beberapa kali jika terjadi tabrakan kode (sangat jarang)
            code = _generate_code()
            existing = conn.execute(
                "SELECT 1 FROM redeem_codes WHERE code = ?", (code,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO redeem_codes
                    (code, token_value, is_unlimited, duration_days, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (code, token_value, 1 if is_unlimited else 0, duration_days, created_by, time.time()),
            )
            return code
    raise RuntimeError("Gagal membuat kode redeem unik setelah beberapa percobaan.")


def get_redeem_code(code: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM redeem_codes WHERE code = ?", (code.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def mark_code_redeemed(code: str, telegram_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE redeem_codes SET redeemed_by = ?, redeemed_at = ? WHERE code = ?",
            (telegram_id, time.time(), code.strip().upper()),
        )


def list_codes(limit: int = 50, only_unused: bool = False) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if only_unused:
            rows = conn.execute(
                "SELECT * FROM redeem_codes WHERE redeemed_by IS NULL ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM redeem_codes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# =========================================================================
# WORKER NODE REGISTRY (Master Node — Cluster Architecture)
# =========================================================================
# Dipakai oleh node_manager.py (health-check + load balancing) dan
# master_dashboard.py (CRUD lewat Admin Web UI). Worker Node itu sendiri
# TIDAK memakai fungsi-fungsi ini sama sekali (registry hanya ada di Master).

def add_worker_node(name: str, host: str, port: int, api_key: str) -> int:
    """Mendaftarkan Worker Node baru ke cluster. Return id node yang baru dibuat."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO worker_nodes (name, host, port, api_key, enabled, status, created_at)
            VALUES (?, ?, ?, ?, 1, 'unknown', ?)
            """,
            (name.strip(), host.strip(), port, api_key.strip(), time.time()),
        )
        return cur.lastrowid


def list_worker_nodes(enabled_only: bool = False) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM worker_nodes WHERE enabled = 1 ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM worker_nodes ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def get_worker_node(node_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM worker_nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def update_worker_node_config(
    node_id: int,
    name: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    api_key: Optional[str] = None,
) -> None:
    """Update konfigurasi node (dipakai Admin Web UI). Field yang None tidak diubah."""
    current = get_worker_node(node_id)
    if not current:
        raise ValueError(f"Worker node id={node_id} tidak ditemukan.")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE worker_nodes
            SET name = ?, host = ?, port = ?, api_key = ?
            WHERE id = ?
            """,
            (
                (name if name is not None else current["name"]).strip(),
                (host if host is not None else current["host"]).strip(),
                port if port is not None else current["port"],
                (api_key if api_key is not None else current["api_key"]).strip(),
                node_id,
            ),
        )


def set_worker_node_enabled(node_id: int, enabled: bool) -> None:
    """Toggle enable/disable node tanpa perlu restart Telegram Bot maupun Dashboard."""
    with get_db() as conn:
        conn.execute(
            "UPDATE worker_nodes SET enabled = ?, status = 'unknown' WHERE id = ?",
            (1 if enabled else 0, node_id),
        )


def delete_worker_node(node_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM worker_nodes WHERE id = ?", (node_id,))


def update_worker_node_health(
    node_id: int,
    status: str,
    cpu: Optional[float],
    ram: Optional[float],
    active_tasks: Optional[int],
    latency_ms: Optional[float],
    models_json: Optional[str],
) -> None:
    """Dipanggil oleh node_manager.health_check_loop setiap siklus health-check."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE worker_nodes
            SET status = ?, cpu_usage = ?, ram_usage = ?, active_tasks = ?,
                latency_ms = ?, models_available = ?, last_checked = ?
            WHERE id = ?
            """,
            (status, cpu, ram, active_tasks, latency_ms, models_json, time.time(), node_id),
        )
