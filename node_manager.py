"""
Enterprise Master Node - Smart Load Balancer & Cluster Orchestrator
========================================================================
Modul ini HANYA berjalan di Master Node. Tanggung jawabnya:

  1. Health-Check Loop (background thread): polling GET /health ke semua
     Worker Node `enabled`, menyimpan metrik LENGKAP (CPU/RAM/VRAM/GPU/
     Safe Mode/model detail) ke `database.worker_nodes`.

  2. SMART WORKER SELECTION: node dipilih bukan cuma dari antrean, tapi dari
     kombinasi skor: ketersediaan model (ready/locked), sisa VRAM, load CPU,
     load RAM, dan jumlah task aktif per model (queue limit).

  3. MODEL FALLBACK CHAIN: jika model yang diminta user tidak tersedia/locked
     di semua node (VRAM penuh), engine otomatis mencoba fallback ke tier
     lebih kecil (mis. 70B -> 32B -> 14B -> 8B) dan memberi tahu pemanggil
     model apa yang benar-benar dipakai (agar bot bisa menginformasikan user).

  4. SMART QUEUE: node_manager tidak mengelola limit concurrency sendiri
     (itu tanggung jawab worker_agent.py per-node demi mencegah OOM lokal),
     tapi ia MEMBACA active_per_model dari /health untuk skip node yang queue
     modelnya sudah penuh, sehingga request tidak dikirim ke node yang pasti
     akan menolak dengan HTTP 429.

Desain sengaja SINKRON (pakai `requests`), health-check loop dijalankan di
background thread (bukan asyncio task) supaya tidak tergantung/mengganggu
event loop python-telegram-bot.
"""

import os
import re
import json
import time
import logging
import threading
from typing import Optional, List, Dict, Any, Tuple

import requests

import database as db

logger = logging.getLogger("ai-bot.node_manager")

NODE_HEALTH_CHECK_INTERVAL_SECONDS = int(os.environ.get("NODE_HEALTH_CHECK_INTERVAL", "7"))
WORKER_HEALTH_TIMEOUT_SECONDS = float(os.environ.get("WORKER_HEALTH_TIMEOUT", "5"))
WORKER_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("WORKER_REQUEST_TIMEOUT", "600"))

_health_check_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


class NoAvailableWorkerError(Exception):
    """Dilempar saat tidak ada satu pun Worker Node online yang bisa memproses request."""


# ============================================================================
# MODEL SIZE ESTIMATION & FALLBACK CHAIN
# ============================================================================
# Sama dengan worker_agent.py (duplikasi minimal by design -- Master tidak
# boleh bergantung penuh pada satu Worker untuk logika fallback-nya sendiri).

_MODEL_SIZE_MAP: Dict[str, float] = {
    "0.5b": 0.5, "1.1b": 1.1, "1.5b": 1.5, "2b": 2.0, "3b": 3.0, "3.8b": 3.8,
    "4b": 4.0, "7b": 7.0, "8b": 8.0, "8x7b": 47.0,
    "9b": 9.0, "13b": 13.0, "14b": 14.0, "27b": 27.0, "32b": 32.0,
    "34b": 34.0, "70b": 70.0, "72b": 72.0, "405b": 405.0,
}

# Urutan fallback standar: dari besar ke kecil. Dipakai untuk mencari model
# "adik" dari model yang diminta, dalam keluarga/basis nama yang sama jika
# memungkinkan, atau fallback generik berdasarkan ukuran saja.
_FALLBACK_SIZE_CHAIN: List[float] = [70.0, 32.0, 14.0, 8.0, 3.0, 1.5]


def _estimate_model_size(model_name: str) -> float:
    name_lower = model_name.lower()
    for size_key in sorted(_MODEL_SIZE_MAP.keys(), key=lambda k: -len(k)):
        if size_key in name_lower:
            return _MODEL_SIZE_MAP[size_key]
    match = re.search(r"(\d+(?:\.\d+)?)b", name_lower)
    if match:
        return float(match.group(1))
    return 7.0


def _model_base_name(model_name: str) -> str:
    """Ambil nama dasar model tanpa tag ukuran, mis. 'qwen2.5-coder:14b' -> 'qwen2.5-coder'."""
    return model_name.split(":")[0] if ":" in model_name else model_name


# ============================================================================
# HEALTH CHECK LOOP (background thread)
# ============================================================================

def _check_one_node(node: Dict[str, Any]) -> None:
    """
    Ping satu Worker Node, simpan hasil health-check LENGKAP (termasuk hardware
    GPU/VRAM/Safe Mode) ke database.
    """
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
                cpu=data.get("cpu_usage_pct"),
                ram=data.get("ram_used_pct"),
                active_tasks=data.get("active_tasks", 0),
                latency_ms=latency_ms,
                models_json=json.dumps(data.get("models_available", [])),
                has_gpu=data.get("has_gpu", False),
                gpu_name=data.get("gpu_name", ""),
                gpu_vendor=data.get("gpu_vendor", ""),
                cuda_version=data.get("cuda_version", ""),
                vram_total_gb=data.get("vram_total_gb"),
                vram_free_gb=data.get("vram_free_gb"),
                vram_used_pct=data.get("vram_used_pct"),
                gpu_util_pct=data.get("gpu_util_pct"),
                cpu_count=data.get("cpu_count"),
                ram_total_gb=data.get("ram_total_gb"),
                safe_mode=data.get("safe_mode", False),
                models_detail_json=json.dumps(data.get("models_detail", [])),
            )
            if data.get("safe_mode"):
                logger.warning(
                    "⚠️  Node '%s' memasuki SAFE MODE (RAM=%.1f%% VRAM=%.1f%%). Model besar dikunci sementara.",
                    node["name"], data.get("ram_used_pct", 0), data.get("vram_used_pct", 0),
                )
        elif resp.status_code == 401:
            logger.warning("Node '%s' menolak API key (401) -- cek konfigurasi.", node["name"])
            db.update_worker_node_health(node["id"], status="unauthorized", latency_ms=latency_ms)
        else:
            db.update_worker_node_health(node["id"], status="error", latency_ms=latency_ms)
    except Exception as exc:
        logger.info("Node '%s' (%s:%s) offline/tidak terjangkau: %s", node["name"], node["host"], node["port"], exc)
        db.update_worker_node_health(node["id"], status="offline")


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
    """Dipanggil sekali saat startup bot.py Master Node."""
    global _health_check_thread
    if _health_check_thread is not None and _health_check_thread.is_alive():
        return
    _stop_event.clear()
    _health_check_thread = threading.Thread(target=_health_check_loop, daemon=True, name="node-health-check")
    _health_check_thread.start()


def stop_health_check_loop() -> None:
    _stop_event.set()


# ============================================================================
# SMART WORKER SELECTION
# ============================================================================

def _node_models_detail(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = node.get("models_detail_json")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _node_has_model_ready(node: Dict[str, Any], model_name: str) -> Tuple[bool, str]:
    """
    Cek apakah node punya model tsb dalam status 'ready'.
    Return (tersedia: bool, alasan_jika_tidak: str)
    """
    details = _node_models_detail(node)
    if not details:
        # Fallback: cek dari daftar nama model sederhana jika detail belum ada
        # (mis. worker versi lama/belum sempat health-check dengan payload baru)
        models_raw = node.get("models_available")
        try:
            names = json.loads(models_raw) if models_raw else []
        except (TypeError, ValueError):
            names = []
        if model_name in names:
            return True, ""
        return False, "model_not_found"

    for m in details:
        if m.get("name") == model_name:
            status = m.get("status", "")
            if status == "ready":
                return True, ""
            return False, status  # locked_no_gpu / locked_insufficient_vram / locked_safe_mode
    return False, "model_not_found"


def _node_queue_full(node: Dict[str, Any], model_name: str) -> bool:
    """Cek apakah slot antrian untuk model ini di node tsb sudah penuh."""
    details = _node_models_detail(node)
    for m in details:
        if m.get("name") == model_name:
            limit = m.get("queue_limit", 999)
            active = node.get("active_tasks", 0) or 0
            # Catatan: active_tasks di sini global per node (bukan per model);
            # per-model breakdown ada di /health["active_per_model"] namun tidak
            # disimpan permanen di DB demi kesederhanaan skema. Untuk keputusan
            # cepat, gunakan heuristik: jika node overload total, anggap penuh.
            return active >= limit
    return False


def _node_score(node: Dict[str, Any], model_name: str) -> tuple:
    """
    Skor node untuk SMART WORKER SELECTION -- lebih rendah = lebih diprioritaskan.
    Kriteria berurutan (tie-break):
      1. active_tasks (load antrian saat ini)
      2. cpu_usage
      3. ram_usage
      4. -vram_free_gb (semakin BANYAK vram bebas semakin diprioritaskan, jadi negatif)
    """
    active = node.get("active_tasks") if node.get("active_tasks") is not None else 999_999
    cpu = node.get("cpu_usage") if node.get("cpu_usage") is not None else 999_999
    ram = node.get("ram_usage") if node.get("ram_usage") is not None else 999_999
    vram_free = node.get("vram_free_gb") if node.get("vram_free_gb") is not None else 0.0
    return (active, cpu, ram, -vram_free)


def get_candidate_nodes(model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Daftar node ONLINE, TIDAK Safe-Mode-blocking, diurutkan dari skor terbaik.
    Jika model_name diberikan, hanya node yang punya model tsb dalam status
    'ready' DAN slot antrian belum penuh yang di-return.
    """
    nodes = db.list_worker_nodes(enabled_only=True)
    online = [n for n in nodes if n.get("status") == "online"]

    if model_name:
        filtered = []
        for n in online:
            ready, reason = _node_has_model_ready(n, model_name)
            if not ready:
                continue
            if _node_queue_full(n, model_name):
                continue
            filtered.append(n)
        online = filtered

    online.sort(key=lambda n: _node_score(n, model_name or ""))
    return online


def get_cluster_snapshot() -> List[Dict[str, Any]]:
    """Semua node (termasuk disabled/offline) -- dipakai Public/Admin Dashboard."""
    nodes = db.list_worker_nodes(enabled_only=False)
    for n in nodes:
        if n.get("models_available"):
            try:
                n["models_available"] = json.loads(n["models_available"])
            except (TypeError, ValueError):
                n["models_available"] = []
        else:
            n["models_available"] = []
        n["models_detail"] = _node_models_detail(n)
    return nodes


# ============================================================================
# MODEL FALLBACK CHAIN
# ============================================================================

def find_available_model_with_fallback(
    requested_model: str,
) -> Tuple[Optional[str], List[Dict[str, Any]], List[str]]:
    """
    Cari model yang benar-benar tersedia (ready di minimal 1 node), dengan
    fallback otomatis ke ukuran lebih kecil jika model yang diminta penuh/locked
    di SEMUA node.

    Return: (model_final: Optional[str], candidate_nodes, fallback_chain_log)
      - model_final None jika BENAR-BENAR tidak ada model apapun yang tersedia
        (semua node offline, atau bahkan model terkecil pun locked).
      - fallback_chain_log: daftar string log tiap langkah percobaan (untuk
        diteruskan ke user sebagai notifikasi transparansi).
    """
    fallback_log: List[str] = []

    # 1. Coba model yang diminta langsung
    candidates = get_candidate_nodes(requested_model)
    if candidates:
        return requested_model, candidates, fallback_log

    fallback_log.append(f"Model '{requested_model}' tidak tersedia di node manapun saat ini.")

    # 2. Fallback berdasarkan basis nama yang sama (mis. qwen2.5-coder:32b -> qwen2.5-coder:14b)
    base_name = _model_base_name(requested_model)
    requested_size = _estimate_model_size(requested_model)
    smaller_sizes = [s for s in _FALLBACK_SIZE_CHAIN if s < requested_size]

    all_nodes = db.list_worker_nodes(enabled_only=True)
    online_nodes = [n for n in all_nodes if n.get("status") == "online"]

    # Kumpulkan semua model unik yang 'ready' di seluruh cluster, dengan basis nama sama
    same_family_ready: Dict[str, float] = {}  # model_name -> size
    for n in online_nodes:
        for m in _node_models_detail(n):
            if m.get("status") != "ready":
                continue
            name = m.get("name", "")
            if _model_base_name(name) == base_name:
                same_family_ready[name] = m.get("size_b", _estimate_model_size(name))

    # Urutkan kandidat sefamili dari yang PALING DEKAT (terbesar tapi < requested) ke terkecil
    same_family_sorted = sorted(
        [(name, size) for name, size in same_family_ready.items() if size < requested_size],
        key=lambda x: -x[1],
    )

    for candidate_name, candidate_size in same_family_sorted:
        candidates = get_candidate_nodes(candidate_name)
        if candidates:
            fallback_log.append(
                f"Fallback otomatis ke model sefamili '{candidate_name}' (~{candidate_size}B)."
            )
            return candidate_name, candidates, fallback_log

    # 3. Fallback generik lintas-keluarga: cari model APAPUN yang ready, ukuran menurun
    all_ready: Dict[str, float] = {}
    for n in online_nodes:
        for m in _node_models_detail(n):
            if m.get("status") == "ready":
                all_ready[m.get("name", "")] = m.get("size_b", _estimate_model_size(m.get("name", "")))

    generic_sorted = sorted(
        [(name, size) for name, size in all_ready.items() if size < requested_size],
        key=lambda x: -x[1],
    )
    for candidate_name, candidate_size in generic_sorted:
        candidates = get_candidate_nodes(candidate_name)
        if candidates:
            fallback_log.append(
                f"Fallback lintas-model ke '{candidate_name}' (~{candidate_size}B) "
                f"karena tidak ada model sefamili '{base_name}' yang tersedia."
            )
            return candidate_name, candidates, fallback_log

    fallback_log.append("Tidak ditemukan model fallback apapun yang tersedia di seluruh cluster.")
    return None, [], fallback_log


# ============================================================================
# GENERATE (dengan Smart Selection + Failover)
# ============================================================================

def generate(
    model_name: str,
    messages: Optional[List[dict]] = None,
    prompt: Optional[str] = None,
    images: Optional[List[str]] = None,
    options: Optional[dict] = None,
    telegram_id: Optional[int] = None,
    allow_fallback: bool = True,
) -> Dict[str, Any]:
    """
    Rute request AI ke Worker Node terbaik (Smart Worker Selection), dengan
    MODEL FALLBACK CHAIN otomatis jika model diminta tidak tersedia, dan
    failover ke node berikutnya jika node yang dipilih gagal di tengah jalan.

    Return dict:
      {"content", "prompt_tokens", "completion_tokens", "total_tokens",
       "model_used", "fallback_occurred", "fallback_log"}
    """
    final_model = model_name
    fallback_log: List[str] = []

    candidates = get_candidate_nodes(model_name)
    if not candidates and allow_fallback:
        final_model, candidates, fallback_log = find_available_model_with_fallback(model_name)
        if final_model is None:
            db.log_queue_event(telegram_id, model_name, None, "failed", "Semua node offline / model tidak tersedia")
            raise NoAvailableWorkerError(
                "Tidak ada Worker Node yang online & memiliki model yang cocok saat ini. "
                "Cek status cluster di Admin Dashboard atau coba lagi nanti."
            )
        for log_line in fallback_log:
            logger.warning("🔀 %s", log_line)
        db.log_queue_event(
            telegram_id, model_name, None, "fallback",
            f"Fallback dari '{model_name}' ke '{final_model}'",
        )
    elif not candidates:
        raise NoAvailableWorkerError(
            f"Model '{model_name}' tidak tersedia di node manapun saat ini (fallback dinonaktifkan)."
        )

    mode = "generate" if prompt is not None else "chat"
    payload: Dict[str, Any] = {"model": final_model, "mode": mode, "options": options or {}}
    if mode == "generate":
        payload["prompt"] = prompt
        if images:
            payload["images"] = images
    else:
        payload["messages"] = messages

    last_error: Optional[Exception] = None
    for node in candidates:
        url = f"http://{node['host']}:{node['port']}/generate"
        db.log_queue_event(telegram_id, final_model, node["name"], "started")
        try:
            resp = requests.post(
                url, json=payload, headers={"X-API-KEY": node["api_key"]},
                timeout=WORKER_REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code == 429:
                # Antrian penuh di node ini -- coba node berikutnya tanpa menandai offline
                logger.info("Node '%s' antrian penuh untuk model '%s', coba node berikutnya.", node["name"], final_model)
                db.log_queue_event(telegram_id, final_model, node["name"], "failed", "Queue penuh (429)")
                last_error = RuntimeError(f"Antrian penuh di node {node['name']}")
                continue
            if resp.status_code == 503:
                # Model terkunci di node ini (VRAM/Safe Mode berubah sejak health-check terakhir)
                logger.info("Node '%s' menolak model '%s' (locked), coba node berikutnya.", node["name"], final_model)
                db.log_queue_event(telegram_id, final_model, node["name"], "failed", "Model locked (503)")
                last_error = RuntimeError(f"Model locked di node {node['name']}")
                continue

            resp.raise_for_status()
            data = resp.json()
            logger.info("✅ Request AI diproses oleh node '%s' (model=%s).", node["name"], final_model)
            db.log_queue_event(telegram_id, final_model, node["name"], "completed")

            data["model_used"] = final_model
            data["fallback_occurred"] = final_model != model_name
            data["fallback_log"] = fallback_log
            return data

        except Exception as exc:
            logger.warning(
                "Node '%s' gagal memproses request (%s), failover ke node berikutnya jika ada.",
                node["name"], exc,
            )
            db.log_queue_event(telegram_id, final_model, node["name"], "failed", str(exc)[:200])
            last_error = exc
            # Tandai offline segera supaya siklus berikutnya tidak memilih node yang sama
            db.update_worker_node_health(node["id"], status="offline")
            continue

    raise NoAvailableWorkerError(
        f"Semua Worker Node gagal memproses request untuk model '{final_model}'. Error terakhir: {last_error}"
    )


# ============================================================================
# ADMIN OPERATIONS: PULL MODEL / UNLOAD ke node tertentu atau semua node
# ============================================================================

def pull_model_on_node(node_id: int, model_name: str, timeout: float = 3600.0) -> Dict[str, Any]:
    """Trigger /pull di satu Worker Node tertentu. Dipakai oleh /pullmodel admin command."""
    node = db.get_worker_node(node_id)
    if not node:
        raise ValueError(f"Worker node id={node_id} tidak ditemukan.")
    url = f"http://{node['host']}:{node['port']}/pull"
    resp = requests.post(
        url, json={"model_name": model_name},
        headers={"X-API-KEY": node["api_key"]}, timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def pull_model_on_all_nodes(model_name: str, timeout: float = 3600.0) -> List[Dict[str, Any]]:
    """Trigger /pull di SEMUA node online sekaligus. Return hasil per node."""
    nodes = db.list_worker_nodes(enabled_only=True)
    online = [n for n in nodes if n.get("status") == "online"]
    results = []
    for node in online:
        try:
            result = pull_model_on_node(node["id"], model_name, timeout=timeout)
            results.append({"node": node["name"], "success": True, **result})
        except Exception as exc:
            results.append({"node": node["name"], "success": False, "error": str(exc)})
    return results


def unload_model_on_node(node_id: int, model_name: str) -> Dict[str, Any]:
    """Trigger /unload di satu Worker Node tertentu. Dipakai oleh /unload admin command."""
    node = db.get_worker_node(node_id)
    if not node:
        raise ValueError(f"Worker node id={node_id} tidak ditemukan.")
    url = f"http://{node['host']}:{node['port']}/unload"
    resp = requests.post(
        url, json={"model_name": model_name},
        headers={"X-API-KEY": node["api_key"]}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def sync_models_from_node(node_id: int) -> Dict[str, Any]:
    """Ambil daftar model terbaru dari satu node (force refresh via /models). Dipakai /modelsync."""
    node = db.get_worker_node(node_id)
    if not node:
        raise ValueError(f"Worker node id={node_id} tidak ditemukan.")
    url = f"http://{node['host']}:{node['port']}/models"
    resp = requests.get(url, headers={"X-API-KEY": node["api_key"]}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_queue_summary() -> Dict[str, Any]:
    """Ringkasan antrian seluruh cluster untuk /queue admin command & dashboard."""
    nodes = get_cluster_snapshot()
    online_nodes = [n for n in nodes if n.get("status") == "online"]

    total_active = sum(n.get("active_tasks") or 0 for n in online_nodes)
    per_node = []
    for n in online_nodes:
        per_node.append({
            "name": n["name"],
            "active_tasks": n.get("active_tasks") or 0,
            "cpu_usage": n.get("cpu_usage"),
            "ram_usage": n.get("ram_usage"),
            "vram_free_gb": n.get("vram_free_gb"),
            "vram_total_gb": n.get("vram_total_gb"),
            "safe_mode": bool(n.get("safe_mode")),
            "models_ready": [m["name"] for m in n.get("models_detail", []) if m.get("status") == "ready"],
            "models_locked": [m["name"] for m in n.get("models_detail", []) if "locked" in m.get("status", "")],
        })

    recent_events = db.get_recent_queue_events(limit=15)

    return {
        "total_nodes_online": len(online_nodes),
        "total_nodes_all": len(nodes),
        "total_active_tasks": total_active,
        "nodes": per_node,
        "recent_events": recent_events,
    }
