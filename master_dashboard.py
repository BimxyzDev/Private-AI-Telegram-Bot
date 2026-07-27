"""
Master Node - Public Web Dashboard + Admin Panel
====================================================
Aplikasi FastAPI TERPISAH dari bot.py (proses & systemd service sendiri),
berjalan di Master Node, menampilkan status cluster Worker Node secara
publik dan menyediakan Admin Panel untuk CRUD registry node TANPA perlu
restart Telegram Bot.

Sengaja dipisah dari proses bot.py:
  - Bot Telegram (polling) dan Dashboard (HTTP server) punya siklus hidup
    berbeda -- restart/crash salah satu tidak mematikan yang lain.
  - node_manager.py (health-check loop) tetap jalan di dalam proses bot.py;
    dashboard ini HANYA membaca/menulis registry node lewat SQLite
    (`database.py`, tabel `worker_nodes`), tidak melakukan ping sendiri ke
    Worker Node -- menghindari duplikasi health-check.

Env vars:
  DB_PATH            path ke bot_data.db (HARUS sama dengan yang dipakai bot.py)
  DASHBOARD_PORT      (default 8080)
  ADMIN_USERNAME      (wajib untuk akses Admin Panel)
  ADMIN_PASSWORD      (wajib untuk akses Admin Panel)

Menjalankan manual (systemd template ada di install.sh):
  uvicorn master_dashboard:app --host 0.0.0.0 --port 8080
"""

import os
import secrets
import logging
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import database as db

logger = logging.getLogger("ai-bot.dashboard")
logging.basicConfig(level=logging.INFO)

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
db.set_db_path(DB_PATH)
db.init_db()

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

app = FastAPI(title="AI Bot Cluster - Dashboard", version="1.0.0")
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
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


def _public_node_view(node: dict) -> dict:
    """Sanitasi node untuk publik: JANGAN pernah expose api_key."""
    return {
        "id": node["id"],
        "name": node["name"],
        "host_masked": _mask_host(node["host"]),
        "enabled": bool(node["enabled"]),
        "status": node["status"],
        "cpu_usage": node["cpu_usage"],
        "ram_usage": node["ram_usage"],
        "active_tasks": node["active_tasks"],
        "latency_ms": node["latency_ms"],
        "last_checked": node["last_checked"],
    }


def _mask_host(host: str) -> str:
    """Sembunyikan sebagian IP/domain node di tampilan publik demi keamanan cluster."""
    if not host:
        return host
    parts = host.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2]) + ".xxx.xxx"
    return host[:3] + "***"


# =========================================================================
# PUBLIC: STATUS CLUSTER
# =========================================================================

@app.get("/api/public/nodes")
def public_nodes():
    nodes = db.list_worker_nodes(enabled_only=False)
    return {"nodes": [_public_node_view(n) for n in nodes]}


@app.get("/", response_class=HTMLResponse)
def public_dashboard():
    return _PUBLIC_HTML


# =========================================================================
# ADMIN: CRUD NODE REGISTRY (dinamis, tanpa restart Telegram Bot)
# =========================================================================

class NodeCreate(BaseModel):
    name: str
    host: str
    port: int = 3716
    api_key: str


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    api_key: Optional[str] = None


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(admin: str = Depends(require_admin)):
    return _ADMIN_HTML


@app.get("/api/admin/nodes")
def admin_list_nodes(admin: str = Depends(require_admin)):
    nodes = db.list_worker_nodes(enabled_only=False)
    # Mask api_key (tampilkan hanya 4 karakter terakhir) supaya tidak bocor
    # lewat network tab browser secara tidak sengaja saat di-screenshot dll.
    for n in nodes:
        key = n.get("api_key", "")
        n["api_key_masked"] = ("*" * max(len(key) - 4, 0)) + key[-4:] if key else ""
        n.pop("api_key", None)
    return {"nodes": nodes}


@app.post("/api/admin/nodes")
def admin_add_node(payload: NodeCreate, admin: str = Depends(require_admin)):
    node_id = db.add_worker_node(payload.name, payload.host, payload.port, payload.api_key)
    logger.info("Admin '%s' menambahkan Worker Node baru: %s (%s:%s)", admin, payload.name, payload.host, payload.port)
    return {"id": node_id, "status": "created"}


@app.put("/api/admin/nodes/{node_id}")
def admin_update_node(node_id: int, payload: NodeUpdate, admin: str = Depends(require_admin)):
    if not db.get_worker_node(node_id):
        raise HTTPException(status_code=404, detail="Node tidak ditemukan.")
    db.update_worker_node_config(
        node_id, name=payload.name, host=payload.host, port=payload.port, api_key=payload.api_key
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


# =========================================================================
# HTML (Tailwind via CDN) -- disatukan dalam satu file untuk kesederhanaan deploy
# =========================================================================

_PUBLIC_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>AI Bot Cluster - Status</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/tailwindcss/2.2.19/tailwind.min.css"></script>
<style>body{background:#0f172a;font-family:ui-sans-serif,system-ui,sans-serif;}</style>
</head>
<body class="text-slate-100 min-h-screen p-6">
  <div class="max-w-5xl mx-auto">
    <h1 class="text-2xl font-bold mb-1">🖥️ AI Bot Cluster - Status Node</h1>
    <p class="text-slate-400 mb-6 text-sm">Status Worker Node diperbarui otomatis tiap beberapa detik.</p>
    <div id="cards" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></div>
  </div>
<script>
function badge(status) {
  const map = {online: "bg-green-600", offline: "bg-red-600", unauthorized: "bg-amber-600", error: "bg-amber-600", unknown: "bg-slate-500"};
  return `<span class="px-2 py-0.5 rounded text-xs font-semibold ${map[status] || 'bg-slate-500'}">${status}</span>`;
}
function bar(value, color) {
  const v = value == null ? 0 : Math.min(100, Math.max(0, value));
  return `<div class="w-full bg-slate-700 rounded h-2 mt-1"><div class="${color} h-2 rounded" style="width:${v}%"></div></div>`;
}
async function refresh() {
  try {
    const res = await fetch('/api/public/nodes');
    const data = await res.json();
    const cards = data.nodes.map(n => `
      <div class="bg-slate-800 rounded-xl p-4 shadow ${n.enabled ? '' : 'opacity-50'}">
        <div class="flex justify-between items-center mb-2">
          <span class="font-semibold">${n.name}</span>
          ${badge(n.status)}
        </div>
        <div class="text-xs text-slate-400 mb-3">${n.host_masked}${n.enabled ? '' : ' (disabled)'}</div>
        <div class="text-xs">CPU: ${n.cpu_usage != null ? n.cpu_usage.toFixed(1) + '%' : '-'}</div>
        ${bar(n.cpu_usage, 'bg-blue-500')}
        <div class="text-xs mt-2">RAM: ${n.ram_usage != null ? n.ram_usage.toFixed(1) + '%' : '-'}</div>
        ${bar(n.ram_usage, 'bg-purple-500')}
        <div class="flex justify-between text-xs mt-3 text-slate-300">
          <span>Task aktif: ${n.active_tasks != null ? n.active_tasks : '-'}</span>
          <span>Latency: ${n.latency_ms != null ? n.latency_ms.toFixed(0) + 'ms' : '-'}</span>
        </div>
      </div>`).join('');
    document.getElementById('cards').innerHTML = cards || '<p class="text-slate-400">Belum ada Worker Node terdaftar.</p>';
  } catch (e) {
    document.getElementById('cards').innerHTML = '<p class="text-red-400">Gagal memuat status cluster.</p>';
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

_ADMIN_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>AI Bot Cluster - Admin</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/tailwindcss/2.2.19/tailwind.min.css"></script>
<style>body{background:#0f172a;font-family:ui-sans-serif,system-ui,sans-serif;}</style>
</head>
<body class="text-slate-100 min-h-screen p-6">
  <div class="max-w-4xl mx-auto">
    <h1 class="text-2xl font-bold mb-6">⚙️ Admin - Kelola Worker Node</h1>

    <div class="bg-slate-800 rounded-xl p-4 mb-6">
      <h2 class="font-semibold mb-3">Tambah Worker Node Baru</h2>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-2">
        <input id="f_name" placeholder="Nama (mis. worker-1)" class="bg-slate-700 rounded px-3 py-2 text-sm">
        <input id="f_host" placeholder="IP / domain" class="bg-slate-700 rounded px-3 py-2 text-sm">
        <input id="f_port" placeholder="Port" value="3716" class="bg-slate-700 rounded px-3 py-2 text-sm">
        <input id="f_key" placeholder="API Key" class="bg-slate-700 rounded px-3 py-2 text-sm">
      </div>
      <button onclick="addNode()" class="mt-3 bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded text-sm font-semibold">+ Tambah Node</button>
    </div>

    <div id="node_list" class="space-y-3"></div>
  </div>
<script>
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) { alert('Error: ' + res.status + ' ' + (await res.text())); throw new Error('api error'); }
  return res.json();
}
async function loadNodes() {
  const data = await api('/api/admin/nodes');
  const list = data.nodes.map(n => `
    <div class="bg-slate-800 rounded-xl p-4 flex justify-between items-center ${n.enabled ? '' : 'opacity-50'}">
      <div>
        <div class="font-semibold">${n.name} <span class="text-xs text-slate-400">(${n.status})</span></div>
        <div class="text-xs text-slate-400">${n.host}:${n.port} — key ${n.api_key_masked}</div>
      </div>
      <div class="space-x-2">
        <button onclick="toggleNode(${n.id})" class="px-3 py-1 rounded text-xs font-semibold ${n.enabled ? 'bg-amber-600 hover:bg-amber-500' : 'bg-green-600 hover:bg-green-500'}">${n.enabled ? 'Nonaktifkan' : 'Aktifkan'}</button>
        <button onclick="deleteNode(${n.id})" class="px-3 py-1 rounded text-xs font-semibold bg-red-600 hover:bg-red-500">Hapus</button>
      </div>
    </div>`).join('');
  document.getElementById('node_list').innerHTML = list || '<p class="text-slate-400">Belum ada node terdaftar.</p>';
}
async function addNode() {
  const name = document.getElementById('f_name').value.trim();
  const host = document.getElementById('f_host').value.trim();
  const port = parseInt(document.getElementById('f_port').value.trim() || '3716', 10);
  const api_key = document.getElementById('f_key').value.trim();
  if (!name || !host || !api_key) { alert('Nama, host, dan API Key wajib diisi.'); return; }
  await api('/api/admin/nodes', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, host, port, api_key})});
  document.getElementById('f_name').value = '';
  document.getElementById('f_host').value = '';
  document.getElementById('f_key').value = '';
  loadNodes();
}
async function toggleNode(id) { await api(`/api/admin/nodes/${id}/toggle`, {method: 'POST'}); loadNodes(); }
async function deleteNode(id) { if (confirm('Hapus node ini dari cluster?')) { await api(`/api/admin/nodes/${id}`, {method: 'DELETE'}); loadNodes(); } }
loadNodes();
setInterval(loadNodes, 8000);
</script>
</body>
</html>
"""
