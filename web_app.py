"""
Enterprise Master Node - Web Dashboard Backend (FastAPI, API-Only)
========================================================================
Aplikasi FastAPI TERPISAH dari bot.py (proses & systemd service sendiri),
berjalan di Master Node. Modul ini HANYA menyediakan REST API + menyajikan
file statis HTML/JS/CSS murni dari folder `web/` (TIDAK ADA HTML inline di
sini) -- lihat web/index.html, web/dashboard.js, web/style.css.

Kenapa dipisah dari bot.py:
  - Bot Telegram (polling) dan Dashboard (HTTP server) punya siklus hidup
    berbeda -- restart/crash salah satu tidak mematikan yang lain.
  - node_manager.py (health-check loop) tetap jalan di dalam proses bot.py;
    dashboard ini HANYA membaca registry node lewat SQLite (`database.py`),
    tidak melakukan ping sendiri ke Worker Node (hindari duplikasi health-check).

Endpoint utama:
  GET  /                          -> web/index.html (Public Dashboard)
  GET  /admin                     -> web/admin.html (Admin Panel, HTTP Basic Auth)
  GET  /api/public/nodes          -> status cluster (data disamarkan, tanpa API key)
  GET  /api/public/stats          -> ringkasan cluster (total node, model, dll)
  GET  /api/admin/nodes           -> CRUD registry node (admin only)
  POST /api/admin/nodes           -> tambah node baru
  PUT  /api/admin/nodes/{id}      -> update konfigurasi node
  POST /api/admin/nodes/{id}/toggle -> enable/disable node
  DELETE /api/admin/nodes/{id}    -> hapus node
  POST /api/admin/nodes/{id}/pull -> trigger pull model ke node tertentu
  POST /api/admin/nodes/{id}/unload -> trigger unload model dari node tertentu
  GET  /api/admin/queue           -> ringkasan antrian cluster
  GET  /api/admin/users           -> daftar user bot (ringkas, tanpa data sensitif)

Env vars:
  DB_PATH            path ke bot_data.db (HARUS sama dengan yang dipakai bot.py)
  DASHBOARD_PORT      (default 8080)
  ADMIN_USERNAME      (wajib untuk akses Admin Panel)
  ADMIN_PASSWORD      (wajib untuk akses Admin Panel)

Menjalankan manual (systemd template ada di install.sh):
  uvicorn web_app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import json
import secrets
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import database as db

logger = logging.getLogger("ai-bot.web_app")
logging.basicConfig(level=logging.INFO)

# ============================================================================
# KONFIGURASI
# ============================================================================
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
db.set_db_path(DB_PATH)
db.init_db()

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Enterprise AI Bot Cluster - Dashboard API", version="2.0.0")
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Autentikasi HTTP Basic untuk semua endpoint /api/admin/* dan /admin."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_USERNAME/ADMIN_PASSWORD belum diset di server (.env Dashboard).",
        )
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kredensial admin salah.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ============================================================================
# HELPER: SANITASI DATA UNTUK PUBLIC VIEW
# ============================================================================

def _mask_host(host: str) -> str:
    """Sembunyikan sebagian IP/domain node di tampilan publik demi keamanan cluster."""
    if not host:
        return host
    parts = host.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2]) + ".xxx.xxx"
    return host[:3] + "***"


def _models_detail_of(node: dict) -> list:
    raw = node.get("models_detail_json")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _public_node_view(node: dict) -> dict:
    """Sanitasi node untuk publik: JANGAN pernah expose api_key & host penuh."""
    models_detail = _models_detail_of(node)
    return {
        "id": node["id"],
        "name": node["name"],
        "host_masked": _mask_host(node["host"]),
        "enabled": bool(node["enabled"]),
        "status": node["status"],
        "has_gpu": bool(node.get("has_gpu")),
        "gpu_name": node.get("gpu_name") or "CPU-Only",
        "vram_total_gb": node.get("vram_total_gb"),
        "vram_free_gb": node.get("vram_free_gb"),
        "vram_used_pct": node.get("vram_used_pct"),
        "cpu_usage": node.get("cpu_usage"),
        "ram_usage": node.get("ram_usage"),
        "active_tasks": node.get("active_tasks"),
        "latency_ms": node.get("latency_ms"),
        "safe_mode": bool(node.get("safe_mode")),
        "models_ready_count": sum(1 for m in models_detail if m.get("status") == "ready"),
        "models_locked_count": sum(1 for m in models_detail if "locked" in m.get("status", "")),
        "last_checked": node.get("last_checked"),
    }


def _admin_node_view(node: dict) -> dict:
    """Sanitasi node untuk admin: API key disamarkan (4 karakter terakhir), sisanya lengkap."""
    node = dict(node)
    key = node.get("api_key", "")
    node["api_key_masked"] = ("*" * max(len(key) - 4, 0)) + key[-4:] if key else ""
    node.pop("api_key", None)
    node["models_detail"] = _models_detail_of(node)
    return node


# ============================================================================
# STATIC FILES (web/ folder -- HTML/CSS/JS murni, tidak ada inline HTML)
# ============================================================================

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def public_dashboard():
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="web/index.html tidak ditemukan di server.")
    return FileResponse(index_path)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(admin: str = Depends(require_admin)):
    admin_path = WEB_DIR / "admin.html"
    if not admin_path.exists():
        raise HTTPException(status_code=500, detail="web/admin.html tidak ditemukan di server.")
    return FileResponse(admin_path)


# ============================================================================
# PUBLIC API: STATUS CLUSTER (real-time, tanpa auth, data disamarkan)
# ============================================================================

@app.get("/api/public/nodes")
def public_nodes():
    nodes = db.list_worker_nodes(enabled_only=False)
    return {"nodes": [_public_node_view(n) for n in nodes]}


@app.get("/api/public/stats")
def public_stats():
    """Ringkasan cluster untuk hero section dashboard publik."""
    nodes = db.list_worker_nodes(enabled_only=False)
    online = [n for n in nodes if n["status"] == "online"]

    total_vram_free = sum(n.get("vram_free_gb") or 0 for n in online if n.get("has_gpu"))
    total_vram_total = sum(n.get("vram_total_gb") or 0 for n in online if n.get("has_gpu"))
    total_active_tasks = sum(n.get("active_tasks") or 0 for n in online)
    gpu_node_count = sum(1 for n in online if n.get("has_gpu"))

    all_models = set()
    for n in online:
        for m in _models_detail_of(n):
            if m.get("status") == "ready":
                all_models.add(m.get("name"))

    return {
        "total_nodes": len(nodes),
        "online_nodes": len(online),
        "gpu_nodes": gpu_node_count,
        "cpu_only_nodes": len(online) - gpu_node_count,
        "total_vram_free_gb": round(total_vram_free, 1),
        "total_vram_total_gb": round(total_vram_total, 1),
        "total_active_tasks": total_active_tasks,
        "unique_models_ready": len(all_models),
        "models_ready": sorted(all_models),
    }


# ============================================================================
# ADMIN: CRUD NODE REGISTRY (dinamis, tanpa restart Telegram Bot)
# ============================================================================

class NodeCreate(BaseModel):
    name: str
    host: str
    port: int = 3716
    api_key: str
    priority: int = 100
    tags: str = ""


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    api_key: Optional[str] = None
    priority: Optional[int] = None
    tags: Optional[str] = None


class PullRequest(BaseModel):
    model_name: str


class UnloadRequest(BaseModel):
    model_name: str


@app.get("/api/admin/nodes")
def admin_list_nodes(admin: str = Depends(require_admin)):
    nodes = db.list_worker_nodes(enabled_only=False)
    return {"nodes": [_admin_node_view(n) for n in nodes]}


@app.post("/api/admin/nodes")
def admin_add_node(payload: NodeCreate, admin: str = Depends(require_admin)):
    node_id = db.add_worker_node(
        payload.name, payload.host, payload.port, payload.api_key, payload.priority, payload.tags
    )
    logger.info("Admin '%s' menambahkan Worker Node baru: %s (%s:%s)", admin, payload.name, payload.host, payload.port)
    return {"id": node_id, "status": "created"}


@app.put("/api/admin/nodes/{node_id}")
def admin_update_node(node_id: int, payload: NodeUpdate, admin: str = Depends(require_admin)):
    if not db.get_worker_node(node_id):
        raise HTTPException(status_code=404, detail="Node tidak ditemukan.")
    db.update_worker_node_config(
        node_id, name=payload.name, host=payload.host, port=payload.port,
        api_key=payload.api_key, priority=payload.priority, tags=payload.tags,
    )
    logger.info("Admin '%s' memperbarui konfigurasi Worker Node id=%s", admin, node_id)
    return {"id": node_id, "status": "updated"}


@app.post("/api/admin/nodes/{node_id}/toggle")
def admin_toggle_node(node_id: int, admin: str = Depends(require_admin)):
    node = db.get_worker_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node tidak ditemukan.")
    new_state = not bool(node["enabled"])
    db.set_worker_node_enabled(node_id, new_state)
    logger.info("Admin '%s' meng-%s Worker Node id=%s", admin, "aktifkan" if new_state else "nonaktifkan", node_id)
    return {"id": node_id, "enabled": new_state}


@app.delete("/api/admin/nodes/{node_id}")
def admin_delete_node(node_id: int, admin: str = Depends(require_admin)):
    if not db.get_worker_node(node_id):
        raise HTTPException(status_code=404, detail="Node tidak ditemukan.")
    db.delete_worker_node(node_id)
    logger.info("Admin '%s' menghapus Worker Node id=%s", admin, node_id)
    return {"id": node_id, "status": "deleted"}


@app.post("/api/admin/nodes/{node_id}/pull")
def admin_pull_model(node_id: int, payload: PullRequest, admin: str = Depends(require_admin)):
    """Trigger `ollama pull` di node tertentu langsung dari Dashboard (sinkron, bisa lama)."""
    import node_manager
    if not db.get_worker_node(node_id):
        raise HTTPException(status_code=404, detail="Node tidak ditemukan.")
    try:
        result = node_manager.pull_model_on_node(node_id, payload.model_name)
        logger.info("Admin '%s' pull model '%s' ke node id=%s", admin, payload.model_name, node_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gagal pull model: {exc}")


@app.post("/api/admin/nodes/{node_id}/unload")
def admin_unload_model(node_id: int, payload: UnloadRequest, admin: str = Depends(require_admin)):
    """Trigger unload model dari VRAM node tertentu langsung dari Dashboard."""
    import node_manager
    if not db.get_worker_node(node_id):
        raise HTTPException(status_code=404, detail="Node tidak ditemukan.")
    try:
        result = node_manager.unload_model_on_node(node_id, payload.model_name)
        logger.info("Admin '%s' unload model '%s' dari node id=%s", admin, payload.model_name, node_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gagal unload model: {exc}")


@app.get("/api/admin/queue")
def admin_queue_summary(admin: str = Depends(require_admin)):
    """Ringkasan antrian cluster + histori event untuk panel monitoring Admin."""
    import node_manager
    return node_manager.get_queue_summary()


@app.get("/api/admin/users")
def admin_list_users(admin: str = Depends(require_admin), limit: int = 100):
    """Daftar user bot Telegram (ringkas) untuk monitoring dari dashboard."""
    users = db.list_users(limit=limit)
    total = db.count_users()
    # Hilangkan field yang tidak relevan untuk tampilan dashboard
    slim_users = [
        {
            "telegram_id": u["telegram_id"],
            "username": u["username"],
            "model_role": u["model_role"],
            "model_tier": u["model_tier"],
            "tokens_used": u["tokens_used"],
            "token_limit": u["token_limit"],
            "is_unlimited": bool(u["is_unlimited"]),
            "is_banned": bool(u["is_banned"]),
            "first_seen": u["first_seen"],
        }
        for u in users
    ]
    return {"total": total, "users": slim_users}
