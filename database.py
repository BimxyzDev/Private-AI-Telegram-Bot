"""
Enterprise AI Telegram Bot - Database Layer (SQLite)
========================================================
Menyimpan:
  - chats          : riwayat percakapan per user (memori AI)
  - users          : kuota token harian, role/tier model aktif, plan & masa berlaku
  - redeem_codes   : kode redeem yang dibuat owner untuk menaikkan kuota token user
  - worker_nodes   : registry cluster Worker Node (Master-Worker Architecture) —
                     kini menyimpan metrik hardware lengkap (GPU, VRAM, Safe Mode)
  - queue_events   : log historis event antrian (untuk /queue & analitik dashboard)

=====================================================================
CATATAN MIGRASI (v3 - Enterprise Hardware-Aware Cluster)
=====================================================================
init_db() otomatis menambahkan kolom baru ke database lama lewat ALTER TABLE
bila belum ada, sehingga proses update (lihat update.sh) aman dijalankan
tanpa menghapus database maupun riwayat chat yang sudah ada.
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

# Role + tier default untuk user baru.
DEFAULT_MODEL_ROLE = "general"
DEFAULT_MODEL_TIER = "medium"
VALID_MODEL_ROLES = ("general", "coder", "extended")
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


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str, existing: set
) -> None:
    """Helper idempotent: tambah kolom hanya jika belum ada."""
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        logger.info("Migrasi DB: kolom '%s' ditambahkan ke tabel %s.", column, table)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Menambahkan kolom baru ke tabel yang sudah ada (dari instalasi versi lama)
    tanpa menghapus data. Aman dipanggil berulang kali (idempotent).
    """
    # --- users: kolom sistem token + model tier ---
    user_cols = _existing_columns(conn, "users")
    _add_column_if_missing(conn, "users", "model_tier", f"TEXT NOT NULL DEFAULT '{DEFAULT_MODEL_TIER}'", user_cols)
    _add_column_if_missing(conn, "users", "token_limit", f"INTEGER NOT NULL DEFAULT {DEFAULT_DAILY_TOKEN_LIMIT}", user_cols)
    _add_column_if_missing(conn, "users", "tokens_used", "INTEGER NOT NULL DEFAULT 0", user_cols)
    if "model_role" not in user_cols:
        # User lama di-backfill ke 'coder' untuk mempertahankan perilaku sebelumnya.
        conn.execute("ALTER TABLE users ADD COLUMN model_role TEXT NOT NULL DEFAULT 'coder'")
        logger.info(
            "Migrasi DB: kolom 'model_role' ditambahkan ke tabel users, "
            "user lama di-backfill ke role 'coder'."
        )

    # --- redeem_codes: kolom token_value ---
    code_cols = _existing_columns(conn, "redeem_codes")
    _add_column_if_missing(conn, "redeem_codes", "token_value", "INTEGER NOT NULL DEFAULT 0", code_cols)

    # --- worker_nodes: kolom hardware enterprise (GPU/VRAM/Safe Mode) ---
    node_cols = _existing_columns(conn, "worker_nodes")
    _add_column_if_missing(conn, "worker_nodes", "has_gpu", "INTEGER", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "gpu_name", "TEXT", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "gpu_vendor", "TEXT", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "cuda_version", "TEXT", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "vram_total_gb", "REAL", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "vram_free_gb", "REAL", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "vram_used_pct", "REAL", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "gpu_util_pct", "REAL", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "cpu_count", "INTEGER", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "ram_total_gb", "REAL", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "safe_mode", "INTEGER NOT NULL DEFAULT 0", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "models_detail_json", "TEXT", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "priority", "INTEGER NOT NULL DEFAULT 100", node_cols)
    _add_column_if_missing(conn, "worker_nodes", "tags", "TEXT", node_cols)


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
        # Menyimpan konfigurasi tiap Worker Node + cache metrik kesehatan HARDWARE
        # LENGKAP (GPU/VRAM/Safe Mode) yang di-refresh oleh node_manager.health_check_loop.
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
                created_at REAL NOT NULL,
                -- kolom hardware enterprise ditambahkan lewat _migrate_schema --
                has_gpu INTEGER,
                gpu_name TEXT,
                gpu_vendor TEXT,
                cuda_version TEXT,
                vram_total_gb REAL,
                vram_free_gb REAL,
                vram_used_pct REAL,
                gpu_util_pct REAL,
                cpu_count INTEGER,
                ram_total_gb REAL,
                safe_mode INTEGER NOT NULL DEFAULT 0,
                models_detail_json TEXT,
                priority INTEGER NOT NULL DEFAULT 100,
                tags TEXT
            );
            """
        )

        # --- queue_events: log historis untuk /queue & analitik dashboard ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                model_name TEXT NOT NULL,
                node_name TEXT,
                event_type TEXT NOT NULL,  -- 'queued' | 'started' | 'completed' | 'failed' | 'fallback'
                detail TEXT,
                created_at REAL NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_events_created ON queue_events(created_at);")

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

        if username and user["username"] != username:
            conn.execute(
                "UPDATE users SET username = ? WHERE telegram_id = ?", (username, telegram_id)
            )
            user["username"] = username

        if user["usage_date"] != today:
            conn.execute(
                "UPDATE users SET tokens_used = 0, usage_date = ? WHERE telegram_id = ?",
                (today, telegram_id),
            )
            user["tokens_used"] = 0
            user["usage_date"] = today

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
    Mengubah role DAN tier model user sekaligus dalam satu UPDATE (atomik).
    Validasi kombinasi role+tier yang benar-benar tersedia adalah tanggung
    jawab ai_engine.resolve_model (single source of truth).
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
        for _ in range(10):
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
# WORKER NODE REGISTRY (Master Node — Cluster Architecture Enterprise)
# =========================================================================
# Dipakai oleh node_manager.py (health-check + smart routing) dan
# web_app.py (CRUD lewat Admin Web UI). Worker Node itu sendiri
# TIDAK memakai fungsi-fungsi ini sama sekali (registry hanya ada di Master).

def add_worker_node(
    name: str, host: str, port: int, api_key: str, priority: int = 100, tags: str = ""
) -> int:
    """Mendaftarkan Worker Node baru ke cluster. Return id node yang baru dibuat."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO worker_nodes (name, host, port, api_key, enabled, status, created_at, priority, tags)
            VALUES (?, ?, ?, ?, 1, 'unknown', ?, ?, ?)
            """,
            (name.strip(), host.strip(), port, api_key.strip(), time.time(), priority, tags.strip()),
        )
        return cur.lastrowid


def list_worker_nodes(enabled_only: bool = False) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM worker_nodes WHERE enabled = 1 ORDER BY priority ASC, id ASC"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM worker_nodes ORDER BY priority ASC, id ASC").fetchall()
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
    priority: Optional[int] = None,
    tags: Optional[str] = None,
) -> None:
    """Update konfigurasi node (dipakai Admin Web UI). Field yang None tidak diubah."""
    current = get_worker_node(node_id)
    if not current:
        raise ValueError(f"Worker node id={node_id} tidak ditemukan.")
    with get_db() as conn:
        conn.execute(
            """
            UPDATE worker_nodes
            SET name = ?, host = ?, port = ?, api_key = ?, priority = ?, tags = ?
            WHERE id = ?
            """,
            (
                (name if name is not None else current["name"]).strip(),
                (host if host is not None else current["host"]).strip(),
                port if port is not None else current["port"],
                (api_key if api_key is not None else current["api_key"]).strip(),
                priority if priority is not None else current["priority"],
                (tags if tags is not None else (current["tags"] or "")).strip(),
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
    cpu: Optional[float] = None,
    ram: Optional[float] = None,
    active_tasks: Optional[int] = None,
    latency_ms: Optional[float] = None,
    models_json: Optional[str] = None,
    has_gpu: Optional[bool] = None,
    gpu_name: Optional[str] = None,
    gpu_vendor: Optional[str] = None,
    cuda_version: Optional[str] = None,
    vram_total_gb: Optional[float] = None,
    vram_free_gb: Optional[float] = None,
    vram_used_pct: Optional[float] = None,
    gpu_util_pct: Optional[float] = None,
    cpu_count: Optional[int] = None,
    ram_total_gb: Optional[float] = None,
    safe_mode: Optional[bool] = None,
    models_detail_json: Optional[str] = None,
) -> None:
    """
    Dipanggil oleh node_manager.health_check_loop setiap siklus health-check.
    Sekarang menyimpan metrik hardware lengkap (GPU/VRAM/Safe Mode) untuk
    mendukung Smart Worker Selection & dashboard real-time.
    """
    with get_db() as conn:
        conn.execute(
            """
            UPDATE worker_nodes
            SET status = ?, cpu_usage = ?, ram_usage = ?, active_tasks = ?,
                latency_ms = ?, models_available = ?, last_checked = ?,
                has_gpu = ?, gpu_name = ?, gpu_vendor = ?, cuda_version = ?,
                vram_total_gb = ?, vram_free_gb = ?, vram_used_pct = ?, gpu_util_pct = ?,
                cpu_count = ?, ram_total_gb = ?, safe_mode = ?, models_detail_json = ?
            WHERE id = ?
            """,
            (
                status, cpu, ram, active_tasks, latency_ms, models_json, time.time(),
                (1 if has_gpu else 0) if has_gpu is not None else None,
                gpu_name, gpu_vendor, cuda_version,
                vram_total_gb, vram_free_gb, vram_used_pct, gpu_util_pct,
                cpu_count, ram_total_gb,
                (1 if safe_mode else 0) if safe_mode is not None else 0,
                models_detail_json,
                node_id,
            ),
        )


# =========================================================================
# QUEUE EVENTS (Log historis untuk /queue & analitik dashboard)
# =========================================================================

def log_queue_event(
    telegram_id: Optional[int],
    model_name: str,
    node_name: Optional[str],
    event_type: str,
    detail: str = "",
) -> None:
    """Catat event antrian: queued/started/completed/failed/fallback."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO queue_events (telegram_id, model_name, node_name, event_type, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, model_name, node_name, event_type, detail, time.time()),
        )


def get_recent_queue_events(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def prune_old_queue_events(older_than_days: int = 7) -> int:
    """Housekeeping: hapus event lama agar tabel tidak membengkak. Return jumlah baris dihapus."""
    cutoff = time.time() - older_than_days * 86400
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM queue_events WHERE created_at < ?", (cutoff,))
        return cursor.rowcount
