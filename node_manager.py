"""
Master Node - Load Balancer & Health Check untuk Worker Node Cluster
========================================================================
Modul ini HANYA berjalan di Master Node. Tanggung jawabnya:

  1. Health-Check Loop (background thread): setiap NODE_HEALTH_CHECK_INTERVAL
     detik, ping GET /health ke semua Worker Node yang `enabled` di registry
     (`database.worker_nodes`), lalu simpan hasilnya (status/cpu/ram/
     active_tasks/latency) kembali ke database lewat
     `database.update_worker_node_health`.

  2. Load Balancing ("Least Loaded"): saat ai_engine.py butuh memanggil AI,
     `generate()` di modul ini memilih Worker Node online dengan
     `active_tasks` paling sedikit (tie-break: cpu_usage lalu ram_usage lebih
     rendah), mengirim request ke situ, dan otomatis FAILOVER ke node
     berikutnya (urutan least-loaded) jika node yang dipilih gagal/timeout.

Desain sengaja SINKRON (pakai `requests`, bukan `httpx`/asyncio) supaya
konsisten dengan ai_engine.py yang sudah sinkron dan dipanggil lewat
`asyncio.to_thread(...)` dari bot.py -- tidak perlu event loop terpisah,
tidak perlu dependency baru (httpx) di Master.

Health-check loop sendiri dijalankan di background thread (bukan asyncio
task) supaya sama sekali tidak tergantung/mengganggu event loop
python-telegram-bot, dan tetap jalan meskipun dipanggil dari worker thread
mana pun.

Registry Worker Node disimpan di SQLite (tabel `worker_nodes`, lihat
database.py) supaya bisa ditambah/diedit/dihapus/toggle lewat Admin Web
Dashboard (master_dashboard.py) TANPA perlu restart Telegram Bot -- loop
health-check di modul ini selalu membaca ulang daftar node dari database
di setiap siklus.
"""

import os
import json
import time
import logging
import threading
from typing import Optional, List, Dict, Any

import requests

import database as db

logger = logging.getLogger("ai-bot.node_manager")

NODE_HEALTH_CHECK_INTERVAL_SECONDS = int(os.environ.get("NODE_HEALTH_CHECK_INTERVAL", "7"))
WORKER_HEALTH_TIMEOUT_SECONDS = float(os.environ.get("WORKER_HEALTH_TIMEOUT", "5"))
WORKER_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("WORKER_REQUEST_TIMEOUT", "600"))

_health_check_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


class NoAvailableWorkerError(Exception):
    """Dilempar saat tidak ada satu pun Worker Node online yang bisa dipakai."""


# =========================================================================
# HEALTH CHECK LOOP (background thread)
# =========================================================================

def _check_one_node(node: Dict[str, Any]) -> None:
    url = f"http://{node['host']}:{node['port']}/health"
    start = time.monotonic()
    try:
        resp = requests.get(url, headers={"X-API-KEY": node["api_key"]}, timeout=WORKER_HEALTH_TIMEOUT_SECONDS)
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code == 200:
            data = resp.json()
            db.update_worker_node_health(
                node["id"],
                status="online",
                cpu=data.get("cpu_usage"),
                ram=data.get("ram_usage"),
                active_tasks=data.get("active_tasks", 0),
                latency_ms=latency_ms,
                models_json=json.dumps(data.get("models_available", [])),
            )
        elif resp.status_code == 401:
            logger.warning("Node '%s' menolak API key (401) -- cek konfigurasi.", node["name"])
            db.update_worker_node_health(
                node["id"], status="unauthorized", cpu=None, ram=None,
                active_tasks=None, latency_ms=latency_ms, models_json=None,
            )
        else:
            db.update_worker_node_health(
                node["id"], status="error", cpu=None, ram=None,
                active_tasks=None, latency_ms=latency_ms, models_json=None,
            )
    except Exception as exc:
        logger.info("Node '%s' (%s:%s) offline/tidak terjangkau: %s", node["name"], node["host"], node["port"], exc)
        db.update_worker_node_health(
            node["id"], status="offline", cpu=None, ram=None,
            active_tasks=None, latency_ms=None, models_json=None,
        )


def _health_check_loop() -> None:
    logger.info("Node health-check loop dimulai (interval %ss).", NODE_HEALTH_CHECK_INTERVAL_SECONDS)
    while not _stop_event.is_set():
        try:
            nodes = db.list_worker_nodes(enabled_only=True)
            threads = [threading.Thread(target=_check_one_node, args=(n,), daemon=True) for n in nodes]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=WORKER_HEALTH_TIMEOUT_SECONDS + 2)
        except Exception:
            logger.exception("Error tak terduga di health-check loop, lanjut ke siklus berikutnya.")
        _stop_event.wait(NODE_HEALTH_CHECK_INTERVAL_SECONDS)


def start_health_check_loop() -> None:
    """Dipanggil sekali saat startup bot.py Master Node (lihat _post_init_start_backup)."""
    global _health_check_thread
    if _health_check_thread is not None and _health_check_thread.is_alive():
        return
    _stop_event.clear()
    _health_check_thread = threading.Thread(target=_health_check_loop, daemon=True)
    _health_check_thread.start()


def stop_health_check_loop() -> None:
    _stop_event.set()


# =========================================================================
# LOAD BALANCING: PILIH NODE PALING SEDIKIT BEBAN ("Least Loaded")
# =========================================================================

def _score(node: Dict[str, Any]) -> tuple:
    """Skor lebih rendah = lebih diprioritaskan. Urutan: active_tasks, cpu, ram."""
    active = node["active_tasks"] if node["active_tasks"] is not None else 999_999
    cpu = node["cpu_usage"] if node["cpu_usage"] is not None else 999_999
    ram = node["ram_usage"] if node["ram_usage"] is not None else 999_999
    return (active, cpu, ram)


def get_candidate_nodes() -> List[Dict[str, Any]]:
    """Daftar node online (enabled + status='online'), diurutkan dari paling sedikit beban."""
    nodes = db.list_worker_nodes(enabled_only=True)
    online = [n for n in nodes if n.get("status") == "online"]
    online.sort(key=_score)
    return online


def get_cluster_snapshot() -> List[Dict[str, Any]]:
    """Semua node (termasuk yang disabled/offline) -- dipakai Public Dashboard."""
    nodes = db.list_worker_nodes(enabled_only=False)
    for n in nodes:
        if n.get("models_available"):
            try:
                n["models_available"] = json.loads(n["models_available"])
            except (TypeError, ValueError):
                n["models_available"] = []
        else:
            n["models_available"] = []
    return nodes


def generate(
    model_name: str,
    messages: Optional[List[dict]] = None,
    prompt: Optional[str] = None,
    images: Optional[List[str]] = None,
    options: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Rute request AI (chat ATAU vision/generate) ke Worker Node yang paling
    sedikit beban, dengan failover otomatis ke node berikutnya jika gagal.
    Salah satu dari `messages` (mode chat) atau `prompt` (mode generate,
    dipakai untuk vision) wajib diisi.

    Return dict senada dengan ai_engine.call_ollama_chat:
      {"content", "prompt_tokens", "completion_tokens", "total_tokens"}
    """
    candidates = get_candidate_nodes()
    if not candidates:
        raise NoAvailableWorkerError(
            "Tidak ada Worker Node yang online saat ini. Cek status cluster di Admin Dashboard."
        )

    mode = "generate" if prompt is not None else "chat"
    payload: Dict[str, Any] = {"model": model_name, "mode": mode, "options": options or {}}
    if mode == "generate":
        payload["prompt"] = prompt
        if images:
            payload["images"] = images
    else:
        payload["messages"] = messages

    last_error: Optional[Exception] = None
    for node in candidates:
        url = f"http://{node['host']}:{node['port']}/generate"
        try:
            resp = requests.post(
                url, json=payload, headers={"X-API-KEY": node["api_key"]},
                timeout=WORKER_REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Request AI diproses oleh node '%s' (model=%s).", node["name"], model_name)
            return data
        except Exception as exc:
            logger.warning(
                "Node '%s' gagal memproses request (%s), failover ke node berikutnya jika ada.",
                node["name"], exc,
            )
            last_error = exc
            # Tandai offline segera supaya siklus health-check berikutnya (dan
            # request lain yang datang di sela-sela ini) tidak memilih node yang
            # sama lagi sebelum dikonfirmasi ulang online.
            db.update_worker_node_health(
                node["id"], status="offline", cpu=None, ram=None,
                active_tasks=None, latency_ms=None, models_json=None,
            )
            continue

    raise NoAvailableWorkerError(
        f"Semua Worker Node gagal memproses request. Error terakhir: {last_error}"
    )
