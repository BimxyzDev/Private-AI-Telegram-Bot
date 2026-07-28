"""
Enterprise Worker Node Agent v2.0
====================================
Aplikasi FastAPI yang berjalan di SETIAP Worker Node (default port 3716).
Ini adalah lapisan "otak" di setiap worker, bukan sekedar proxy.

FITUR ENTERPRISE (v2):
  1. Smart GPU Detection  : Deteksi vendor/nama GPU, CUDA, VRAM total/free/used via
                            nvidia-smi → fallback torch.cuda. Hasil di-cache.
  2. Dynamic Model Lock   : Model dikunci (Locked) / dibuka (Ready) berdasarkan VRAM.
                            CPU-Only → max 8B, GPU 8GB → +14B, GPU 24GB → +32B, dst.
  3. Auto Pull & Cache    : /pull endpoint otomatis jalankan `ollama pull` dengan
                            progress streaming, lalu refresh cache model lokal.
  4. Auto Unload & Safe   : Background thread unload model idle >IDLE_UNLOAD_MINUTES.
                            Jika RAM/VRAM nyaris habis, masuk SAFE_MODE → kunci model besar.
  5. Smart Queue Limiter  : Batasi concurrent per ukuran model (70B:1, 32B:2, 14B:4, 8B:∞)
                            untuk mencegah OOM / Kernel Panic.
  6. VRAM-Aware Routing   : /health melaporkan vram_free, model_status (locked/ready)
                            sehingga Master bisa routing berdasarkan VRAM aktual.

Env vars:
  WORKER_API_KEY                   (wajib) kunci rahasia, sama dengan yang di Master
  OLLAMA_HOST                      (default http://127.0.0.1:11434)
  WORKER_PORT                      (default 3716)
  WORKER_REQUEST_TIMEOUT_SECONDS   (default 600)
  IDLE_UNLOAD_MINUTES              (default 20) menit idle sebelum model di-unload
  SAFE_MODE_RAM_THRESHOLD          (default 90) % RAM sebelum masuk safe mode
  SAFE_MODE_VRAM_THRESHOLD         (default 85) % VRAM sebelum masuk safe mode
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
import threading
import subprocess
from typing import Optional, List, Dict, Any, Tuple

import requests
import psutil
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ============================================================================
# KONFIGURASI LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai-worker")

# ============================================================================
# ENVIRONMENT VARS
# ============================================================================
WORKER_API_KEY: str = os.environ.get("WORKER_API_KEY", "").strip()
OLLAMA_HOST: str = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
REQUEST_TIMEOUT_SECONDS: float = float(os.environ.get("WORKER_REQUEST_TIMEOUT_SECONDS", "600"))
DEFAULT_NUM_CTX: int = int(os.environ.get("OLLAMA_NUM_CTX", "4096"))
IDLE_UNLOAD_MINUTES: int = int(os.environ.get("IDLE_UNLOAD_MINUTES", "20"))
SAFE_MODE_RAM_THRESHOLD: float = float(os.environ.get("SAFE_MODE_RAM_THRESHOLD", "90"))
SAFE_MODE_VRAM_THRESHOLD: float = float(os.environ.get("SAFE_MODE_VRAM_THRESHOLD", "85"))

if not WORKER_API_KEY:
    raise RuntimeError(
        "WORKER_API_KEY tidak diset. Worker Agent menolak start tanpa API key. "
        "Set WORKER_API_KEY di /opt/ai-worker/.env sebelum menjalankan service."
    )

# ============================================================================
# DEFINISI TIER MODEL & BATAS HARDWARE
# ============================================================================
# Mapping: nama model (substring) → perkiraan ukuran parameter (Miliar)
MODEL_SIZE_MAP: Dict[str, float] = {
    "0.5b": 0.5, "1.1b": 1.1, "1.5b": 1.5, "2b": 2.0, "3b": 3.0, "3.8b": 3.8,
    "4b": 4.0, "7b": 7.0, "8b": 8.0, "8x7b": 47.0,  # mixtral MoE
    "9b": 9.0, "13b": 13.0, "14b": 14.0, "27b": 27.0, "32b": 32.0,
    "34b": 34.0, "70b": 70.0, "72b": 72.0, "405b": 405.0,
}

# VRAM minimum (GB) untuk tiap kelompok ukuran model agar bisa berjalan di GPU
VRAM_REQUIREMENTS: List[Tuple[float, float]] = [
    (8.0, 4.0),    # model ≤8B butuh minimal 4GB VRAM
    (14.0, 8.0),   # model ≤14B butuh minimal 8GB VRAM
    (32.0, 16.0),  # model ≤32B butuh minimal 16GB VRAM
    (70.0, 40.0),  # model ≤70B butuh minimal 40GB VRAM
    (999.0, 80.0), # model >70B butuh minimal 80GB VRAM
]

# Batas concurrent request per ukuran model (Zero OOM protection)
QUEUE_LIMITS: List[Tuple[float, int]] = [
    (8.0, 999),   # ≤8B: unlimited (pakai nilai besar)
    (14.0, 4),    # ≤14B: max 4 concurrent
    (32.0, 2),    # ≤32B: max 2 concurrent
    (70.0, 1),    # ≤70B: max 1 concurrent
    (999.0, 1),   # >70B: max 1 concurrent (safety)
]

# ============================================================================
# STATE GLOBAL — THREAD-SAFE
# ============================================================================
_state_lock = threading.Lock()
_active_tasks: int = 0

# active_per_model: { model_name: jumlah_concurrent_aktif }
_active_per_model: Dict[str, int] = {}

# Waktu terakhir model dipakai (untuk auto-unload)
# { model_name: timestamp_unix }
_model_last_used: Dict[str, float] = {}

# Cache hardware info (diisi saat startup, di-refresh berkala)
_hw_cache: Dict[str, Any] = {}
_hw_cache_lock = threading.Lock()
_hw_cache_time: float = 0.0
HW_CACHE_TTL_SECONDS: float = 30.0

# Cache daftar model Ollama lokal
_model_cache: List[Dict[str, Any]] = []
_model_cache_lock = threading.Lock()
_model_cache_time: float = 0.0
MODEL_CACHE_TTL_SECONDS: float = 15.0

# Apakah sistem sedang dalam Safe Mode?
_safe_mode: bool = False
_safe_mode_lock = threading.Lock()


# ============================================================================
# FASTAPI APP
# ============================================================================
app = FastAPI(
    title="Enterprise AI Worker Node Agent",
    version="2.0.0",
    description="Smart GPU/Hardware-Aware Worker Agent untuk Distributed AI Cluster",
)


# ============================================================================
# HELPER: API KEY AUTH
# ============================================================================
def _require_api_key(x_api_key: Optional[str]) -> None:
    """Validasi X-API-KEY header. Lempar 401 jika tidak valid."""
    if not x_api_key or x_api_key != WORKER_API_KEY:
        logger.warning("Akses ditolak: API key tidak valid atau kosong.")
        raise HTTPException(status_code=401, detail="X-API-KEY tidak valid atau tidak disertakan.")


# ============================================================================
# HARDWARE DETECTION
# ============================================================================

def _parse_nvidia_smi() -> Optional[Dict[str, Any]]:
    """
    Jalankan nvidia-smi untuk mendapatkan info GPU detail.
    Return dict info GPU atau None jika tidak ada GPU NVIDIA.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.free,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None

        lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        if not lines:
            return None

        # Ambil GPU pertama (index 0), bisa diextend multi-GPU nanti
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) < 6:
            return None

        name, driver, total_mb, free_mb, used_mb, util = parts
        total_gb = round(float(total_mb) / 1024, 2)
        free_gb = round(float(free_mb) / 1024, 2)
        used_gb = round(float(used_mb) / 1024, 2)

        # Deteksi CUDA version dari nvidia-smi output
        cuda_result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        # Ambil CUDA versi dari baris pertama `nvidia-smi` tanpa flag khusus
        cuda_version = _get_cuda_version()

        gpu_count = len(lines)

        return {
            "vendor": "NVIDIA",
            "name": name,
            "driver_version": driver,
            "cuda_version": cuda_version,
            "vram_total_gb": total_gb,
            "vram_free_gb": free_gb,
            "vram_used_gb": used_gb,
            "vram_util_pct": round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0.0,
            "gpu_util_pct": float(util) if util.replace(".", "").isdigit() else 0.0,
            "gpu_count": gpu_count,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as exc:
        logger.debug("nvidia-smi parse gagal: %s", exc)
        return None


def _get_cuda_version() -> str:
    """Ambil versi CUDA dari nvidia-smi versi pendek."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--version"], capture_output=True, text=True, timeout=5
        )
        # Format: "CUDA Version: 12.3"
        match = re.search(r"CUDA Version\s*:\s*([\d.]+)", result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    try:
        import torch  # type: ignore
        return torch.version.cuda or "unknown"
    except ImportError:
        return "unknown"


def _detect_gpu_torch() -> Optional[Dict[str, Any]]:
    """Fallback: gunakan PyTorch jika nvidia-smi tidak tersedia."""
    try:
        import torch  # type: ignore
        if not torch.cuda.is_available():
            return None
        device = torch.cuda.current_device()
        props = torch.cuda.get_device_properties(device)
        total_gb = round(props.total_memory / 1024**3, 2)
        free_bytes, total_bytes = torch.cuda.mem_get_info(device)
        free_gb = round(free_bytes / 1024**3, 2)
        used_gb = round((total_bytes - free_bytes) / 1024**3, 2)
        return {
            "vendor": "NVIDIA",  # torch.cuda hanya mendukung NVIDIA
            "name": props.name,
            "driver_version": "via-torch",
            "cuda_version": torch.version.cuda or "unknown",
            "vram_total_gb": total_gb,
            "vram_free_gb": free_gb,
            "vram_used_gb": used_gb,
            "vram_util_pct": round((used_gb / total_gb) * 100, 1) if total_gb > 0 else 0.0,
            "gpu_util_pct": 0.0,
            "gpu_count": torch.cuda.device_count(),
        }
    except Exception as exc:
        logger.debug("torch.cuda detection gagal: %s", exc)
        return None


def get_hardware_info(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Kumpulkan info hardware lengkap: GPU + CPU + RAM.
    Hasil di-cache TTL=30s untuk efisiensi.
    """
    global _hw_cache, _hw_cache_time

    with _hw_cache_lock:
        now = time.monotonic()
        if not force_refresh and _hw_cache and (now - _hw_cache_time) < HW_CACHE_TTL_SECONDS:
            return dict(_hw_cache)

        # CPU info
        cpu_count = psutil.cpu_count(logical=True)
        cpu_usage = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        ram_total_gb = round(ram.total / 1024**3, 2)
        ram_free_gb = round(ram.available / 1024**3, 2)
        ram_used_pct = round(ram.percent, 1)

        # GPU info
        gpu_info = _parse_nvidia_smi()
        if gpu_info is None:
            gpu_info = _detect_gpu_torch()

        has_gpu = gpu_info is not None

        hw = {
            "has_gpu": has_gpu,
            "gpu": gpu_info or {},
            "cpu_count": cpu_count,
            "cpu_usage_pct": cpu_usage,
            "ram_total_gb": ram_total_gb,
            "ram_free_gb": ram_free_gb,
            "ram_used_pct": ram_used_pct,
        }
        _hw_cache = hw
        _hw_cache_time = now
        return dict(hw)


# ============================================================================
# MODEL AVAILABILITY & LOCK LOGIC
# ============================================================================

def _estimate_model_size(model_name: str) -> float:
    """
    Estimasi ukuran model dalam miliar parameter dari nama model string.
    Contoh: 'qwen2.5:32b' → 32.0, 'llama3.1:70b' → 70.0
    """
    name_lower = model_name.lower()
    # Urutkan dari spesifik ke umum (8x7b sebelum 7b)
    for size_key in sorted(MODEL_SIZE_MAP.keys(), key=lambda k: -len(k)):
        if size_key in name_lower:
            return MODEL_SIZE_MAP[size_key]
    # Fallback: coba parse angka sebelum 'b' dengan regex
    match = re.search(r"(\d+(?:\.\d+)?)b", name_lower)
    if match:
        return float(match.group(1))
    return 7.0  # default jika tidak terdeteksi


def _get_vram_free_gb() -> float:
    """Ambil VRAM bebas saat ini (GB). Return 0 jika CPU-only."""
    hw = get_hardware_info()
    return hw.get("gpu", {}).get("vram_free_gb", 0.0)


def _get_vram_required(size_b: float) -> float:
    """Perkiraan VRAM minimum (GB) yang dibutuhkan untuk model berukuran size_b miliar."""
    for threshold, vram_req in VRAM_REQUIREMENTS:
        if size_b <= threshold:
            return vram_req
    return 80.0


def _get_queue_limit(size_b: float) -> int:
    """Maksimum concurrent request yang diizinkan untuk model berukuran size_b."""
    for threshold, limit in QUEUE_LIMITS:
        if size_b <= threshold:
            return limit
    return 1


def get_model_status(model_name: str, vram_free_gb: Optional[float] = None) -> str:
    """
    Tentukan apakah model 'Ready' atau 'Locked'.
    - CPU-Only (vram=0): hanya ≤8B yang Ready
    - GPU tersedia: hitung berdasarkan VRAM free saat ini
    - Safe Mode aktif: kunci semua model >8B
    """
    size_b = _estimate_model_size(model_name)

    with _safe_mode_lock:
        is_safe_mode = _safe_mode

    if is_safe_mode and size_b > 8.0:
        return "locked_safe_mode"

    if vram_free_gb is None:
        vram_free_gb = _get_vram_free_gb()

    if vram_free_gb < 0.5:  # CPU-only
        return "ready" if size_b <= 8.0 else "locked_no_gpu"

    required = _get_vram_required(size_b)
    if vram_free_gb >= required:
        return "ready"
    return "locked_insufficient_vram"


def list_ollama_models_with_status() -> List[Dict[str, Any]]:
    """
    Ambil daftar model dari Ollama API, tambahkan status hardware (ready/locked).
    Di-cache TTL=15s.
    """
    global _model_cache, _model_cache_time

    with _model_cache_lock:
        now = time.monotonic()
        if _model_cache and (now - _model_cache_time) < MODEL_CACHE_TTL_SECONDS:
            return list(_model_cache)

        raw_models = _fetch_ollama_models()
        hw = get_hardware_info()
        vram_free = hw.get("gpu", {}).get("vram_free_gb", 0.0)

        enriched = []
        for m in raw_models:
            name = m.get("name", "")
            size_b = _estimate_model_size(name)
            status = get_model_status(name, vram_free_gb=vram_free)
            enriched.append({
                "name": name,
                "size_b": size_b,
                "size_str": f"{size_b}B",
                "status": status,
                "vram_required_gb": _get_vram_required(size_b),
                "queue_limit": _get_queue_limit(size_b),
                "modified_at": m.get("modified_at", ""),
                "digest": m.get("digest", "")[:12] if m.get("digest") else "",
            })

        _model_cache = enriched
        _model_cache_time = now
        return list(enriched)


def _fetch_ollama_models() -> List[Dict[str, Any]]:
    """HTTP call ke Ollama /api/tags untuk daftar model lokal."""
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        resp.raise_for_status()
        return resp.json().get("models", [])
    except Exception as exc:
        logger.warning("Gagal fetch model list dari Ollama: %s", exc)
        return []


def refresh_model_cache() -> None:
    """Paksa refresh cache model (dipanggil setelah pull selesai)."""
    global _model_cache_time
    with _model_cache_lock:
        _model_cache_time = 0.0  # Expire cache


# ============================================================================
# SAFE MODE MONITOR (Background Thread)
# ============================================================================

def _safe_mode_monitor_loop() -> None:
    """
    Background thread: cek RAM/VRAM setiap 15 detik.
    Jika melewati threshold → aktifkan Safe Mode (kunci model besar).
    Jika kondisi membaik → nonaktifkan Safe Mode.
    """
    global _safe_mode
    while True:
        try:
            hw = get_hardware_info(force_refresh=True)
            ram_pct = hw["ram_used_pct"]
            vram_pct = hw.get("gpu", {}).get("vram_util_pct", 0.0)

            should_safe = ram_pct >= SAFE_MODE_RAM_THRESHOLD or (
                hw["has_gpu"] and vram_pct >= SAFE_MODE_VRAM_THRESHOLD
            )

            with _safe_mode_lock:
                was_safe = _safe_mode
                _safe_mode = should_safe

            if should_safe and not was_safe:
                logger.warning(
                    "⚠️  SAFE MODE AKTIF! RAM=%.1f%% VRAM=%.1f%%. "
                    "Model >8B dikunci sementara.",
                    ram_pct, vram_pct,
                )
            elif not should_safe and was_safe:
                logger.info(
                    "✅ Safe Mode NONAKTIF. RAM=%.1f%% VRAM=%.1f%% kembali normal.",
                    ram_pct, vram_pct,
                )
        except Exception as exc:
            logger.exception("Error di safe mode monitor: %s", exc)
        time.sleep(15)


# ============================================================================
# AUTO-UNLOAD IDLE MODEL (Background Thread)
# ============================================================================

def _auto_unload_loop() -> None:
    """
    Background thread: cek model yang idle > IDLE_UNLOAD_MINUTES.
    Unload dari VRAM via Ollama API untuk membebaskan resource.
    """
    idle_threshold = IDLE_UNLOAD_MINUTES * 60

    while True:
        try:
            now = time.time()
            with _state_lock:
                to_unload = [
                    model for model, last_used in _model_last_used.items()
                    if (now - last_used) > idle_threshold
                    and _active_per_model.get(model, 0) == 0
                ]

            for model in to_unload:
                _unload_model_from_vram(model)

        except Exception as exc:
            logger.exception("Error di auto-unload loop: %s", exc)
        time.sleep(60)  # Cek setiap 1 menit


def _unload_model_from_vram(model_name: str) -> bool:
    """
    Unload model dari VRAM dengan mengirim request generate dengan keep_alive=0.
    Ini meminta Ollama untuk segera melepas model dari memori.
    Return True jika berhasil.
    """
    try:
        payload = {
            "model": model_name,
            "keep_alive": 0,
            "prompt": "",
        }
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 404):
            with _state_lock:
                _model_last_used.pop(model_name, None)
            logger.info("🔄 Model '%s' berhasil di-unload dari VRAM (idle timeout).", model_name)
            refresh_model_cache()
            return True
        logger.warning("Gagal unload model '%s': HTTP %s", model_name, resp.status_code)
        return False
    except Exception as exc:
        logger.warning("Error saat unload model '%s': %s", model_name, exc)
        return False


# ============================================================================
# CONCURRENT TASK COUNTER
# ============================================================================

def _inc_task(model_name: str) -> None:
    global _active_tasks
    with _state_lock:
        _active_tasks += 1
        _active_per_model[model_name] = _active_per_model.get(model_name, 0) + 1
        _model_last_used[model_name] = time.time()


def _dec_task(model_name: str) -> None:
    global _active_tasks
    with _state_lock:
        _active_tasks = max(0, _active_tasks - 1)
        current = _active_per_model.get(model_name, 0)
        _active_per_model[model_name] = max(0, current - 1)
        _model_last_used[model_name] = time.time()


def _get_active_tasks() -> int:
    with _state_lock:
        return _active_tasks


def _check_queue_capacity(model_name: str) -> Tuple[bool, str]:
    """
    Cek apakah masih ada slot antrian untuk model ini.
    Return (True, "") jika boleh lanjut, (False, alasan) jika full.
    """
    size_b = _estimate_model_size(model_name)
    limit = _get_queue_limit(size_b)
    with _state_lock:
        current = _active_per_model.get(model_name, 0)
    if current >= limit:
        return False, (
            f"Antrian model '{model_name}' penuh ({current}/{limit} slot). "
            f"Model ~{size_b}B memiliki batas {limit} request concurrent untuk mencegah OOM. "
            "Silakan coba lagi sebentar."
        )
    return True, ""


# ============================================================================
# NORMALIZE OLLAMA RESPONSE
# ============================================================================

def _normalize_chat_response(data: dict) -> Dict[str, Any]:
    content = data.get("message", {}).get("content", "").strip() or "[Model tidak mengembalikan jawaban]"
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    return {
        "content": content,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _normalize_generate_response(data: dict) -> Dict[str, Any]:
    content = data.get("response", "").strip() or "[Model tidak mengembalikan jawaban]"
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    return {
        "content": content,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class GenerateRequest(BaseModel):
    model: str
    mode: str = "chat"
    messages: Optional[List[Dict[str, Any]]] = None
    prompt: Optional[str] = None
    images: Optional[List[str]] = None
    options: Optional[Dict[str, Any]] = None


class PullModelRequest(BaseModel):
    model_name: str


class UnloadModelRequest(BaseModel):
    model_name: str


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/")
def root():
    return {
        "service": "enterprise-ai-worker-agent",
        "version": "2.0.0",
        "status": "running",
        "safe_mode": _safe_mode,
    }


@app.get("/health")
def health(x_api_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """
    Health check endpoint — dipanggil Master setiap beberapa detik.
    Melaporkan info hardware real-time termasuk VRAM, model status, safe mode.
    """
    _require_api_key(x_api_key)

    hw = get_hardware_info()
    models = list_ollama_models_with_status()
    vram_free = hw.get("gpu", {}).get("vram_free_gb", 0.0)
    vram_total = hw.get("gpu", {}).get("vram_total_gb", 0.0)
    vram_used_pct = hw.get("gpu", {}).get("vram_util_pct", 0.0)

    with _safe_mode_lock:
        is_safe = _safe_mode

    with _state_lock:
        per_model_snapshot = dict(_active_per_model)

    return {
        "status": "online",
        "safe_mode": is_safe,
        # Hardware
        "has_gpu": hw["has_gpu"],
        "gpu_name": hw.get("gpu", {}).get("name", "CPU-Only"),
        "gpu_vendor": hw.get("gpu", {}).get("vendor", ""),
        "cuda_version": hw.get("gpu", {}).get("cuda_version", ""),
        "vram_total_gb": vram_total,
        "vram_free_gb": vram_free,
        "vram_used_pct": vram_used_pct,
        "gpu_util_pct": hw.get("gpu", {}).get("gpu_util_pct", 0.0),
        "gpu_count": hw.get("gpu", {}).get("gpu_count", 0),
        # System
        "cpu_count": hw["cpu_count"],
        "cpu_usage_pct": hw["cpu_usage_pct"],
        "ram_total_gb": hw["ram_total_gb"],
        "ram_free_gb": hw["ram_free_gb"],
        "ram_used_pct": hw["ram_used_pct"],
        # Tasks
        "active_tasks": _get_active_tasks(),
        "active_per_model": per_model_snapshot,
        # Models
        "models_available": [m["name"] for m in models],
        "models_detail": models,
        # Timestamp
        "reported_at": time.time(),
    }


@app.get("/hardware")
def hardware_info(x_api_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Detail spesifikasi hardware node (cached, force refresh)."""
    _require_api_key(x_api_key)
    return get_hardware_info(force_refresh=True)


@app.get("/models")
def list_models(x_api_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Daftar model lokal dengan status hardware (ready/locked)."""
    _require_api_key(x_api_key)
    models = list_ollama_models_with_status()
    return {
        "models": models,
        "total": len(models),
        "ready": sum(1 for m in models if m["status"] == "ready"),
        "locked": sum(1 for m in models if "locked" in m["status"]),
        "safe_mode": _safe_mode,
    }


@app.post("/pull")
def pull_model(req: PullModelRequest, x_api_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """
    Trigger `ollama pull <model_name>` secara sinkron.
    Cocok untuk admin trigger dari bot atau dashboard.
    Progress di-log ke server log.
    """
    _require_api_key(x_api_key)

    model = req.model_name.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model_name tidak boleh kosong.")

    logger.info("🚀 Memulai pull model '%s'...", model)
    start_time = time.time()

    try:
        # Gunakan Ollama streaming pull API
        with requests.post(
            f"{OLLAMA_HOST}/api/pull",
            json={"name": model, "stream": True},
            timeout=3600,  # 1 jam timeout untuk model besar
            stream=True,
        ) as resp:
            resp.raise_for_status()
            last_status = ""
            for line in resp.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        status = data.get("status", "")
                        if status != last_status:
                            total = data.get("total", 0)
                            completed = data.get("completed", 0)
                            if total > 0:
                                pct = round(completed / total * 100, 1)
                                logger.info("Pull '%s': %s (%.1f%%)", model, status, pct)
                            else:
                                logger.info("Pull '%s': %s", model, status)
                            last_status = status
                        if data.get("error"):
                            logger.error("Error pull '%s': %s", model, data["error"])
                            raise HTTPException(status_code=500, detail=f"Ollama pull error: {data['error']}")
                    except json.JSONDecodeError:
                        pass  # skip baris non-JSON

        elapsed = round(time.time() - start_time, 1)
        logger.info("✅ Model '%s' berhasil di-pull dalam %.1fs.", model, elapsed)

        # Refresh cache model setelah pull
        refresh_model_cache()
        models_after = list_ollama_models_with_status()
        pulled = next((m for m in models_after if model in m["name"]), None)

        return {
            "success": True,
            "model": model,
            "elapsed_seconds": elapsed,
            "model_status": pulled.get("status", "unknown") if pulled else "unknown",
            "message": f"Model '{model}' berhasil di-pull dan siap digunakan.",
        }

    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail=f"Tidak bisa terhubung ke Ollama di {OLLAMA_HOST}.")
    except requests.exceptions.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Ollama pull error: {exc}")


@app.post("/unload")
def unload_model(req: UnloadModelRequest, x_api_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Manual unload model dari VRAM."""
    _require_api_key(x_api_key)
    model = req.model_name.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model_name tidak boleh kosong.")

    # Cek apakah ada task aktif untuk model ini
    with _state_lock:
        active = _active_per_model.get(model, 0)
    if active > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Model '{model}' masih memiliki {active} request aktif. Tunggu selesai sebelum unload.",
        )

    success = _unload_model_from_vram(model)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal unload model '{model}'. Lihat log server.")
    return {"success": True, "model": model, "message": f"Model '{model}' berhasil di-unload dari VRAM."}


@app.post("/generate")
def generate(
    payload: GenerateRequest,
    x_api_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    Endpoint utama: forward request AI ke Ollama lokal.
    Fitur:
    - Cek model status (locked/ready) berdasarkan VRAM
    - Cek queue limit untuk mencegah OOM
    - Update last-used timestamp untuk auto-unload
    """
    _require_api_key(x_api_key)

    model = payload.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Field 'model' wajib diisi.")

    # Cek apakah model berstatus locked
    model_status = get_model_status(model)
    if "locked" in model_status:
        reasons = {
            "locked_no_gpu": "Server ini CPU-Only. Model ini membutuhkan GPU.",
            "locked_insufficient_vram": "VRAM tidak cukup untuk model ini saat ini.",
            "locked_safe_mode": "Server dalam Safe Mode (resource hampir habis). Model besar dikunci sementara.",
        }
        reason = reasons.get(model_status, "Model dikunci oleh sistem.")
        raise HTTPException(
            status_code=503,
            detail=f"Model '{model}' tidak tersedia di node ini: {reason}",
            headers={"X-Model-Status": model_status},
        )

    # Cek queue capacity
    can_proceed, queue_msg = _check_queue_capacity(model)
    if not can_proceed:
        raise HTTPException(status_code=429, detail=queue_msg)

    options = dict(payload.options or {})
    options.setdefault("num_ctx", DEFAULT_NUM_CTX)

    _inc_task(model)
    try:
        if payload.mode == "generate":
            if not payload.prompt:
                raise HTTPException(status_code=400, detail="Field 'prompt' wajib diisi untuk mode='generate'.")
            ollama_payload: Dict[str, Any] = {
                "model": model,
                "prompt": payload.prompt,
                "stream": False,
                "options": options,
            }
            if payload.images:
                ollama_payload["images"] = payload.images
            url = f"{OLLAMA_HOST}/api/generate"
            normalize_fn = _normalize_generate_response
        else:
            if not payload.messages:
                raise HTTPException(status_code=400, detail="Field 'messages' wajib diisi untuk mode='chat'.")
            ollama_payload = {
                "model": model,
                "messages": payload.messages,
                "stream": False,
                "options": options,
            }
            url = f"{OLLAMA_HOST}/api/chat"
            normalize_fn = _normalize_chat_response

        try:
            resp = requests.post(url, json=ollama_payload, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            logger.error("Koneksi ke Ollama gagal: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"Worker tidak dapat terhubung ke Ollama di {OLLAMA_HOST}: {exc}",
            )
        except requests.exceptions.Timeout:
            logger.error("Request ke Ollama timeout setelah %ss untuk model '%s'.", REQUEST_TIMEOUT_SECONDS, model)
            raise HTTPException(
                status_code=504,
                detail=f"Request timeout setelah {REQUEST_TIMEOUT_SECONDS}s. Model '{model}' terlalu lambat.",
            )
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response else 502
            body = exc.response.text[:500] if exc.response else str(exc)
            # Deteksi OOM dari Ollama error message
            if "out of memory" in body.lower() or "oom" in body.lower():
                logger.critical(
                    "🔴 OOM ERROR! Model '%s' menyebabkan Out-of-Memory di node ini! Detail: %s",
                    model, body
                )
            raise HTTPException(
                status_code=502,
                detail=f"Ollama error (HTTP {status_code}): {body}",
            )

        result = normalize_fn(resp.json())
        logger.info(
            "✅ Generate selesai: model='%s' prompt_tokens=%d completion_tokens=%d",
            model, result["prompt_tokens"], result["completion_tokens"],
        )
        return result

    finally:
        _dec_task(model)


# ============================================================================
# STARTUP EVENTS
# ============================================================================

@app.on_event("startup")
def on_startup() -> None:
    """Jalankan background threads saat startup."""
    logger.info("🚀 Enterprise Worker Node Agent v2.0 startup...")

    # Deteksi hardware awal
    hw = get_hardware_info(force_refresh=True)
    if hw["has_gpu"]:
        gpu = hw.get("gpu", {})
        logger.info(
            "🖥️  GPU Terdeteksi: %s %s | CUDA: %s | VRAM: %.1fGB total, %.1fGB free",
            gpu.get("vendor", ""), gpu.get("name", ""),
            gpu.get("cuda_version", ""), gpu.get("vram_total_gb", 0),
            gpu.get("vram_free_gb", 0),
        )
    else:
        logger.warning("⚠️  Tidak ada GPU terdeteksi. Berjalan dalam mode CPU-Only (max model: 8B).")

    # Load model cache awal
    models = list_ollama_models_with_status()
    logger.info(
        "📦 Model Ollama lokal: %d model (%d ready, %d locked)",
        len(models),
        sum(1 for m in models if m["status"] == "ready"),
        sum(1 for m in models if "locked" in m["status"]),
    )

    # Jalankan Safe Mode Monitor
    sm_thread = threading.Thread(target=_safe_mode_monitor_loop, daemon=True, name="safe-mode-monitor")
    sm_thread.start()
    logger.info("🛡️  Safe Mode Monitor aktif (RAM threshold: %.0f%%, VRAM threshold: %.0f%%)",
                SAFE_MODE_RAM_THRESHOLD, SAFE_MODE_VRAM_THRESHOLD)

    # Jalankan Auto-Unload Thread
    unload_thread = threading.Thread(target=_auto_unload_loop, daemon=True, name="auto-unload")
    unload_thread.start()
    logger.info("🔄 Auto-Unload Monitor aktif (idle timeout: %d menit).", IDLE_UNLOAD_MINUTES)

    logger.info("✅ Worker Agent siap menerima request di port WORKER_PORT.")
