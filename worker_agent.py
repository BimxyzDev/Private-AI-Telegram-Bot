"""
Worker Node Agent — Distributed Cluster Architecture
========================================================
Aplikasi FastAPI ringan yang berjalan di SETIAP Worker Node (port 3716).
Worker Node adalah VPS yang menjalankan Ollama secara lokal; agent ini adalah
"jembatan" antara Master Node (Load Balancer) dan Ollama di server tsb.

Tanggung jawab agent ini HANYA:
  1. Melaporkan kesehatan node (CPU, RAM, jumlah task aktif, model yang
     tersedia) lewat GET /health -- dipanggil Master setiap beberapa detik.
  2. Meneruskan request generate/chat ke Ollama lokal lewat POST /generate,
     sambil menghitung active_tasks (dipakai Master untuk load balancing
     "least loaded").

Worker Node TIDAK menyimpan database, TIDAK tahu apa-apa soal user Telegram,
kuota token, atau node lain di cluster -- sengaja dibuat stateless & minim
dependency (fastapi, uvicorn, psutil, requests) supaya mudah di-scale
(tinggal jalankan install.sh mode [2] di VPS baru, lalu daftarkan IP-nya
lewat Admin Dashboard di Master Node).

Autentikasi:
  Semua endpoint (termasuk /health) WAJIB menyertakan header `X-API-KEY`
  yang cocok dengan env var WORKER_API_KEY. Ini mencegah pihak luar
  memakai Ollama VPS Anda sebagai relay gratis maupun membaca metrik node.

Env vars:
  WORKER_API_KEY   (wajib)  -- kunci rahasia, sama dengan yang didaftarkan di
                               Admin Dashboard Master Node untuk node ini.
  OLLAMA_HOST      (opsional, default http://127.0.0.1:11434)
  WORKER_PORT      (opsional, default 3716)
  WORKER_REQUEST_TIMEOUT_SECONDS (opsional, default 600 -- selaras ai_engine.py)

Menjalankan manual (systemd template ada di install.sh):
  uvicorn worker_agent:app --host 0.0.0.0 --port 3716
"""

import os
import time
import logging
import threading
from typing import Optional, List, Dict, Any

import requests
import psutil
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("ai-bot.worker_agent")
logging.basicConfig(level=logging.INFO)

WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "").strip()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("WORKER_REQUEST_TIMEOUT_SECONDS", "600"))
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "2048"))

if not WORKER_API_KEY:
    raise RuntimeError(
        "WORKER_API_KEY tidak diset. Worker Agent menolak untuk start tanpa API key "
        "(lihat /opt/ai-worker/.env). Ini mencegah node dipakai tanpa otorisasi Master."
    )

app = FastAPI(title="AI Bot - Worker Node Agent", version="1.0.0")

# Penghitung task aktif (thread-safe) -- dipakai Master untuk strategi
# load-balancing "least loaded". Bertambah saat /generate mulai memproses,
# berkurang saat selesai (baik sukses maupun gagal).
_active_tasks_lock = threading.Lock()
_active_tasks = 0


def _inc_active_tasks() -> None:
    global _active_tasks
    with _active_tasks_lock:
        _active_tasks += 1


def _dec_active_tasks() -> None:
    global _active_tasks
    with _active_tasks_lock:
        _active_tasks = max(0, _active_tasks - 1)


def _get_active_tasks() -> int:
    with _active_tasks_lock:
        return _active_tasks


def _require_api_key(x_api_key: Optional[str]) -> None:
    if not x_api_key or x_api_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="X-API-KEY tidak valid atau tidak disertakan.")


def _list_ollama_models() -> List[str]:
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as exc:
        logger.warning("Gagal mengambil daftar model Ollama: %s", exc)
        return []


# =========================================================================
# GET /health
# =========================================================================

@app.get("/health")
def health(x_api_key: Optional[str] = Header(None)):
    _require_api_key(x_api_key)
    try:
        cpu_usage = psutil.cpu_percent(interval=0.3)
        ram_usage = psutil.virtual_memory().percent
    except Exception as exc:
        logger.exception("Gagal membaca metrik sistem")
        raise HTTPException(status_code=500, detail=f"Gagal membaca metrik sistem: {exc}")

    return {
        "status": "online",
        "cpu_usage": cpu_usage,
        "ram_usage": ram_usage,
        "active_tasks": _get_active_tasks(),
        "models_available": _list_ollama_models(),
    }


# =========================================================================
# POST /generate
# =========================================================================
# Menerima payload generik yang bisa dipakai untuk 2 mode Ollama:
#   - mode="chat"     -> forward ke Ollama /api/chat  (dipakai untuk teks/coding)
#   - mode="generate" -> forward ke Ollama /api/generate (dipakai untuk vision,
#                        prompt + images base64)
# Respons SELALU dinormalisasi ke bentuk yang sama supaya Master (node_manager.py)
# tidak perlu tahu detail parsing Ollama untuk tiap mode:
#   {"content": str, "prompt_tokens": int, "completion_tokens": int, "total_tokens": int}

class GenerateRequest(BaseModel):
    model: str
    mode: str = "chat"  # "chat" atau "generate"
    messages: Optional[List[Dict[str, Any]]] = None   # dipakai jika mode="chat"
    prompt: Optional[str] = None                        # dipakai jika mode="generate"
    images: Optional[List[str]] = None                   # base64 images, jika mode="generate"
    options: Optional[Dict[str, Any]] = None


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


@app.post("/generate")
def generate(payload: GenerateRequest, x_api_key: Optional[str] = Header(None)):
    _require_api_key(x_api_key)

    options = dict(payload.options or {})
    options.setdefault("num_ctx", DEFAULT_NUM_CTX)

    _inc_active_tasks()
    try:
        if payload.mode == "generate":
            if not payload.prompt:
                raise HTTPException(status_code=400, detail="Field 'prompt' wajib diisi untuk mode='generate'.")
            ollama_payload = {
                "model": payload.model,
                "prompt": payload.prompt,
                "stream": False,
                "options": options,
            }
            if payload.images:
                ollama_payload["images"] = payload.images
            url = f"{OLLAMA_HOST}/api/generate"
            normalize = _normalize_generate_response
        else:
            if not payload.messages:
                raise HTTPException(status_code=400, detail="Field 'messages' wajib diisi untuk mode='chat'.")
            ollama_payload = {
                "model": payload.model,
                "messages": payload.messages,
                "stream": False,
                "options": options,
            }
            url = f"{OLLAMA_HOST}/api/chat"
            normalize = _normalize_chat_response

        try:
            resp = requests.post(url, json=ollama_payload, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Worker tidak dapat terhubung ke Ollama lokal di {OLLAMA_HOST}: {exc}",
            )
        except requests.exceptions.Timeout:
            raise HTTPException(
                status_code=504,
                detail=f"Request ke Ollama lokal timeout setelah {REQUEST_TIMEOUT_SECONDS}s.",
            )
        except requests.exceptions.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Ollama lokal mengembalikan error: {exc}. Pastikan model '{payload.model}' sudah di-pull.",
            )

        return normalize(resp.json())
    finally:
        _dec_active_tasks()


@app.get("/")
def root():
    return {"service": "ai-bot-worker-agent", "status": "running"}
