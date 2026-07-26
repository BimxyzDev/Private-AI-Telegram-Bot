"""
Private AI Telegram Bot - AI Engine
======================================
Modul ini berisi semua logika inti untuk berkomunikasi dengan Ollama serta
memproses file upload (dokumen, PDF, DOCX, gambar, video, ZIP). Logika ini
diadaptasi dari backend FastAPI versi sebelumnya, dilepas dari layer HTTP
agar bisa dipakai langsung oleh bot Telegram (python-telegram-bot).

Model yang dipakai:
  - Sistem 3 Tier (chat/coding, text-only), dipilih user lewat /model:
        🟢 Light  -> qwen2.5-coder:1.5b  (multiplier kuota token 1x)
        🟡 Medium -> qwen2.5-coder:7b    (multiplier kuota token 2x, default)
        🔴 Heavy  -> qwen2.5-coder:14b   (multiplier kuota token 3x)
  - qwen2.5vl -> analisis gambar & video (vision, di luar sistem tier/kuota)

Stabilitas & Timeout:
  - Timeout HTTP ke Ollama diset 600 detik (10 menit) agar request context
    panjang / model besar tidak terputus prematur (fix HTTP 500 lama).
  - Semua payload chat menyertakan options={"num_ctx": 2048} agar prompt
    processing tidak melambat akibat context window default yang terlalu besar.
  - Semua error (termasuk timeout) ditangani sebagai AIEngineError dengan
    pesan ramah-user, tidak pernah membuat proses Python crash.

Personalitas:
  - Setiap request chat ke Ollama otomatis menyertakan system prompt
    (DEFAULT_SYSTEM_PROMPT) yang membuat bot bergaya santai/gaul dan anti
    baper, bisa dikustomisasi lewat env var BOT_SYSTEM_PROMPT.
"""

import os
import io
import base64
import zipfile
import subprocess
import tempfile
import shutil
import logging
from typing import Optional, List, Dict, Any

import requests

logger = logging.getLogger("ai-bot.engine")


class AIEngineError(Exception):
    """Error yang bisa ditangkap oleh bot dan dikirim sebagai pesan ke user."""

    def __init__(self, message: str, user_message: Optional[str] = None):
        super().__init__(message)
        self.user_message = user_message or message


# =========================================================================
# KONFIGURASI (diisi lewat env vars, lihat bot.py / install.sh)
# =========================================================================

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

# --- Sistem 3 Tier Model ---
MODEL_TIERS: Dict[str, str] = {
    "light": os.environ.get("OLLAMA_MODEL_LIGHT", "qwen2.5-coder:1.5b"),
    "medium": os.environ.get("OLLAMA_MODEL_MEDIUM", "qwen2.5-coder:7b"),
    "heavy": os.environ.get("OLLAMA_MODEL_HEAVY", "qwen2.5-coder:14b"),
}

# Multiplier kuota token berdasarkan beban CPU tiap tier.
# Contoh: 1.000 token asli hasil Ollama pada tier medium (2x) memotong 2.000 token kuota user.
TOKEN_MULTIPLIER: Dict[str, int] = {
    "light": 1,
    "medium": 2,
    "heavy": 3,
}

DEFAULT_MODEL_TIER = "medium"
OLLAMA_MODEL = MODEL_TIERS[DEFAULT_MODEL_TIER]  # dipakai untuk logging start-up saja

OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5vl")

MAX_HISTORY_MESSAGES = 20

# Timeout HTTP client ke Ollama: 600 detik (10 menit) — lihat catatan modul di atas.
REQUEST_TIMEOUT_SECONDS = 600.0
VISION_TIMEOUT_SECONDS = 600.0

# Context window default Ollama. Nilai kecil (2048) menjaga prompt processing tetap cepat;
# bisa dinaikkan lewat env var OLLAMA_NUM_CTX jika benar-benar butuh context lebih panjang
# (dengan konsekuensi prompt processing lebih lambat).
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "2048"))

MAX_FILES_IN_ZIP = 20
MAX_ZIP_EXTRACTED_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_EXTRACTED_CHARS = 200_000

VIDEO_SAMPLE_FRAMES = 4
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")

# =========================================================================
# PERSONALITAS / SYSTEM PROMPT (ANTI BAPER)
# =========================================================================
# Disuntikkan sebagai pesan role "system" pertama di setiap request chat ke
# Ollama, supaya bot punya kepribadian konsisten: santai, gaul, tidak baperan,
# dan tidak membalas dengan gaya customer service formal ala Bahasa Inggris.
DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "BOT_SYSTEM_PROMPT",
    "Kamu adalah BIMXYZ AI BOT, asisten AI dan koding yang cerdas, santai, gaul, dan "
    "responsif. Selalu gunakan Bahasa Indonesia yang natural, santai, dan komunikatif. "
    "Jika user bercanda, menggunakan bahasa gaul, atau mengumpat (seperti 'asu', 'anjing', "
    "dll), tanggapi dengan santai, lucu, atau humoris. Dilarang keras meminta maaf secara "
    "formal dalam Bahasa Inggris ala Customer Service.",
)


def resolve_model(tier: Optional[str]) -> str:
    """Mengembalikan nama model Ollama untuk tier tertentu, fallback ke tier default jika tidak valid."""
    if tier not in MODEL_TIERS:
        tier = DEFAULT_MODEL_TIER
    return MODEL_TIERS[tier]


# =========================================================================
# DETEKSI JENIS FILE
# =========================================================================

TEXT_LIKE_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".css", ".xml", ".yaml", ".yml", ".log", ".sh", ".sql",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
ZIP_EXTENSIONS = {".zip"}


def detect_file_kind(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in ZIP_EXTENSIONS:
        return "zip"
    return "text"  # dokumen, code, pdf, docx, dan fallback lain-lain


# =========================================================================
# EKSTRAKSI TEKS DARI FILE DOKUMEN (non-media)
# =========================================================================

def extract_text_from_file(filename: str, content: bytes) -> str:
    """
    Ekstraksi teks sederhana berdasarkan ekstensi file.
    Mendukung: .txt, .md, .csv, .json, .py, .js, .html, .css, dan file teks lain (decode utf-8).
    Untuk .pdf dan .docx, mencoba library opsional jika tersedia; jika tidak, memberi pesan informatif.
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext in TEXT_LIKE_EXTENSIONS or ext == "":
        try:
            return content.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("Gagal decode file teks %s: %s", filename, e)
            return f"[Gagal membaca file sebagai teks: {e}]"

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages_text = []
            for page in reader.pages:
                pages_text.append(page.extract_text() or "")
            return "\n".join(pages_text)
        except ImportError:
            return "[Ekstraksi PDF membutuhkan library 'pypdf'. Install dengan: pip install pypdf]"
        except Exception as e:
            return f"[Gagal mengekstrak teks dari PDF: {e}]"

    if ext == ".docx":
        try:
            import docx
            doc = docx.Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return "[Ekstraksi DOCX membutuhkan library 'python-docx'. Install dengan: pip install python-docx]"
        except Exception as e:
            return f"[Gagal mengekstrak teks dari DOCX: {e}]"

    # Fallback: coba decode sebagai teks, jika gagal beri tahu format tidak didukung
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return f"[Format file '{ext}' tidak didukung untuk ekstraksi teks otomatis.]"


# =========================================================================
# VISION: ANALISIS GAMBAR VIA OLLAMA (qwen2.5vl)
# =========================================================================

def call_ollama_vision(image_bytes: bytes, prompt: str) -> str:
    """Mengirim satu gambar (raw bytes) + prompt teks ke model vision Ollama."""
    url = f"{OLLAMA_HOST}/api/generate"
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "model": OLLAMA_VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"num_ctx": DEFAULT_NUM_CTX},
    }
    try:
        resp = requests.post(url, json=payload, timeout=VISION_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "").strip() or "[Model vision tidak mengembalikan jawaban]"
    except requests.exceptions.ConnectionError as e:
        logger.error("Tidak bisa terhubung ke Ollama (vision) di %s: %s", OLLAMA_HOST, e)
        raise AIEngineError(
            str(e),
            f"Tidak dapat terhubung ke Ollama di {OLLAMA_HOST}. Pastikan service Ollama berjalan.",
        )
    except requests.exceptions.Timeout:
        logger.error("Request vision ke Ollama timeout setelah %s detik", VISION_TIMEOUT_SECONDS)
        raise AIEngineError(
            "timeout",
            "⏳ Analisis gambar/video melebihi waktu tunggu (10 menit). Server sedang sibuk "
            "atau file terlalu kompleks. Coba lagi dalam beberapa saat.",
        )
    except requests.exceptions.HTTPError as e:
        logger.error("Ollama (vision) mengembalikan error HTTP: %s", e)
        raise AIEngineError(
            str(e),
            f"Ollama mengembalikan error saat analisis gambar: {e}. "
            f"Pastikan model '{OLLAMA_VISION_MODEL}' sudah di-pull (ollama pull {OLLAMA_VISION_MODEL}).",
        )
    except Exception as e:
        logger.exception("Error tak terduga saat memanggil Ollama vision")
        raise AIEngineError(str(e), f"Error tak terduga saat analisis gambar: {e}")


def analyze_image(filename: str, content: bytes) -> str:
    prompt = (
        f"Deskripsikan gambar berikut ('{filename}') secara detail dan jelas dalam Bahasa Indonesia. "
        "Sebutkan objek utama, konteks, teks yang terlihat (jika ada), dan hal penting lainnya."
    )
    return call_ollama_vision(content, prompt)


# =========================================================================
# VIDEO: EKSTRAKSI FRAME KUNCI VIA FFMPEG, LALU DIANALISIS SEBAGAI GAMBAR
# =========================================================================

def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG_BIN) is not None and shutil.which(FFPROBE_BIN) is not None


def get_video_duration_seconds(video_path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(result.stdout.strip())
    except Exception as e:
        logger.warning("Gagal membaca durasi video: %s", e)
        return None


def extract_video_frames(video_path: str, out_dir: str, num_frames: int) -> List[str]:
    """Mengekstrak beberapa frame kunci yang tersebar merata sepanjang durasi video."""
    duration = get_video_duration_seconds(video_path)
    frame_paths = []

    if not duration or duration <= 0:
        timestamps = [0.5]
    else:
        margin = duration * 0.05
        usable_duration = max(duration - 2 * margin, 0.1)
        if num_frames == 1:
            timestamps = [duration / 2]
        else:
            timestamps = [
                margin + (usable_duration * i / (num_frames - 1))
                for i in range(num_frames)
            ]

    for idx, ts in enumerate(timestamps):
        out_path = os.path.join(out_dir, f"frame_{idx}.jpg")
        try:
            subprocess.run(
                [
                    FFMPEG_BIN, "-y",
                    "-ss", str(max(ts, 0)),
                    "-i", video_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    out_path,
                ],
                capture_output=True, timeout=60, check=True,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                frame_paths.append(out_path)
        except Exception as e:
            logger.warning("Gagal ekstrak frame pada detik %.2f: %s", ts, e)
            continue

    return frame_paths


def analyze_video(filename: str, content: bytes) -> str:
    if not ffmpeg_available():
        return (
            f"[Tidak dapat menganalisis video '{filename}': ffmpeg/ffprobe tidak ditemukan di server. "
            "Install dengan: sudo apt-get install ffmpeg]"
        )

    with tempfile.TemporaryDirectory(prefix="ai_video_") as tmp_dir:
        video_path = os.path.join(tmp_dir, filename or "input_video")
        with open(video_path, "wb") as f:
            f.write(content)

        frame_paths = extract_video_frames(video_path, tmp_dir, VIDEO_SAMPLE_FRAMES)

        if not frame_paths:
            return f"[Gagal mengekstrak frame dari video '{filename}'. Pastikan format video valid.]"

        descriptions = []
        for i, frame_path in enumerate(frame_paths):
            try:
                with open(frame_path, "rb") as f:
                    frame_bytes = f.read()
                prompt = (
                    f"Ini adalah cuplikan frame ke-{i + 1} dari {len(frame_paths)} frame yang diambil "
                    f"dari video '{filename}'. Deskripsikan secara singkat apa yang terlihat di frame ini "
                    "dalam Bahasa Indonesia."
                )
                desc = call_ollama_vision(frame_bytes, prompt)
                descriptions.append(f"Frame {i + 1}: {desc}")
            except AIEngineError as e:
                descriptions.append(f"Frame {i + 1}: [Gagal dianalisis: {e.user_message}]")
            except Exception as e:
                descriptions.append(f"Frame {i + 1}: [Gagal dianalisis: {e}]")

        summary = (
            f"Analisis video '{filename}' berdasarkan {len(frame_paths)} frame sampel "
            f"(bukan keseluruhan video):\n\n" + "\n\n".join(descriptions)
        )
        return summary


# =========================================================================
# ZIP: EKSTRAKSI ISI DAN PEMROSESAN TIAP FILE SESUAI JENISNYA
# =========================================================================

def analyze_zip(filename: str, content: bytes) -> str:
    """
    Mengekstrak isi ZIP dan memproses tiap file di dalamnya sesuai jenisnya:
    - Teks/code/pdf/docx -> ekstraksi teks
    - Gambar             -> analisis vision
    - Video              -> ekstraksi frame + analisis vision
    - ZIP bersarang       -> dilewati (tidak diekstrak rekursif, untuk mencegah zip bomb)
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return f"[File '{filename}' bukan ZIP yang valid atau rusak.]"

    infolist = zf.infolist()

    total_uncompressed = sum(info.file_size for info in infolist)
    if total_uncompressed > MAX_ZIP_EXTRACTED_TOTAL_BYTES:
        return (
            f"[ZIP '{filename}' ditolak: ukuran hasil ekstraksi ({total_uncompressed // (1024*1024)} MB) "
            f"melebihi batas aman {MAX_ZIP_EXTRACTED_TOTAL_BYTES // (1024*1024)} MB.]"
        )

    file_entries = [info for info in infolist if not info.is_dir()]
    if len(file_entries) > MAX_FILES_IN_ZIP:
        note = (
            f"[Catatan: ZIP berisi {len(file_entries)} file, hanya {MAX_FILES_IN_ZIP} file pertama "
            "yang diproses.]\n\n"
        )
        file_entries = file_entries[:MAX_FILES_IN_ZIP]
    else:
        note = ""

    sections = [f"Isi ZIP '{filename}' ({len(file_entries)} file diproses):\n"]

    for info in file_entries:
        entry_name = info.filename
        ext = os.path.splitext(entry_name)[1].lower()

        try:
            entry_bytes = zf.read(info)
        except Exception as e:
            sections.append(f"\n--- {entry_name} ---\n[Gagal membaca file dari ZIP: {e}]")
            continue

        if ext in ZIP_EXTENSIONS:
            sections.append(f"\n--- {entry_name} ---\n[ZIP bersarang dilewati, tidak diekstrak rekursif.]")
            continue

        if ext in IMAGE_EXTENSIONS:
            try:
                desc = analyze_image(entry_name, entry_bytes)
                sections.append(f"\n--- {entry_name} (gambar) ---\n{desc}")
            except AIEngineError as e:
                sections.append(f"\n--- {entry_name} (gambar) ---\n[Gagal dianalisis: {e.user_message}]")
            continue

        if ext in VIDEO_EXTENSIONS:
            try:
                desc = analyze_video(entry_name, entry_bytes)
                sections.append(f"\n--- {entry_name} (video) ---\n{desc}")
            except AIEngineError as e:
                sections.append(f"\n--- {entry_name} (video) ---\n[Gagal dianalisis: {e.user_message}]")
            continue

        text = extract_text_from_file(entry_name, entry_bytes)
        sections.append(f"\n--- {entry_name} ---\n{text}")

    return note + "\n".join(sections)


def process_uploaded_file(filename: str, content: bytes) -> Dict[str, object]:
    """
    Entry point tunggal untuk memproses file apa pun yang diupload lewat Telegram.
    Mengembalikan dict berisi file_kind dan extracted_text (siap digabung ke prompt).
    """
    file_kind = detect_file_kind(filename)

    if file_kind == "image":
        extracted = analyze_image(filename, content)
    elif file_kind == "video":
        extracted = analyze_video(filename, content)
    elif file_kind == "zip":
        extracted = analyze_zip(filename, content)
    else:
        extracted = extract_text_from_file(filename, content)

    truncated = False
    if len(extracted) > MAX_EXTRACTED_CHARS:
        extracted = extracted[:MAX_EXTRACTED_CHARS]
        truncated = True

    return {
        "filename": filename,
        "file_kind": file_kind,
        "extracted_text": extracted,
        "truncated": truncated,
    }


# =========================================================================
# INTEGRASI OLLAMA (CHAT/CODING) - SISTEM 3 TIER + TOKEN ACCOUNTING
# =========================================================================

def build_prompt_context(history: List[dict], new_message: str) -> List[dict]:
    """
    Membangun list pesan format chat (role/content) untuk dikirim ke Ollama.
    Pesan "system" berisi personality prompt (anti baper) selalu disisipkan
    sebagai pesan pertama, sebelum riwayat chat.
    """
    messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
    for item in history:
        role = item["role"]
        if role not in ("user", "assistant", "system"):
            role = "user"
        messages.append({"role": role, "content": item["message"]})
    messages.append({"role": "user", "content": new_message})
    return messages


def call_ollama_chat(messages: List[dict], model_name: str) -> Dict[str, Any]:
    """
    Mengirim request ke Ollama /api/chat dan mengembalikan dict:
      {
        "content": str,            # jawaban model
        "prompt_tokens": int,      # dari field 'prompt_eval_count' Ollama
        "completion_tokens": int,  # dari field 'eval_count' Ollama
        "total_tokens": int,       # prompt_tokens + completion_tokens
      }
    Timeout diset 600 detik (10 menit) dan context window diset num_ctx=2048
    secara default agar prompt processing tidak lambat.
    """
    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {"num_ctx": DEFAULT_NUM_CTX},
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("message", {}).get("content", "").strip() or "[Model tidak mengembalikan jawaban]"
        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        completion_tokens = int(data.get("eval_count") or 0)

        return {
            "content": content,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    except requests.exceptions.ConnectionError as e:
        logger.error("Tidak bisa terhubung ke Ollama di %s: %s", OLLAMA_HOST, e)
        raise AIEngineError(
            str(e),
            f"Tidak dapat terhubung ke Ollama di {OLLAMA_HOST}. Pastikan service Ollama berjalan.",
        )
    except requests.exceptions.Timeout:
        logger.error(
            "Request ke Ollama (model=%s) timeout setelah %s detik", model_name, REQUEST_TIMEOUT_SECONDS
        )
        raise AIEngineError(
            "timeout",
            "⏳ AI sedang memproses context yang panjang dan melebihi waktu tunggu (10 menit). "
            "Coba kirim pesan yang lebih singkat, gunakan /reset untuk memulai percakapan baru, "
            "atau coba lagi dalam beberapa saat.",
        )
    except requests.exceptions.HTTPError as e:
        logger.error("Ollama mengembalikan error HTTP: %s", e)
        raise AIEngineError(
            str(e),
            f"Ollama mengembalikan error: {e}. Pastikan model '{model_name}' sudah di-pull "
            f"(ollama pull {model_name}).",
        )
    except Exception as e:
        logger.exception("Error tak terduga saat memanggil Ollama")
        raise AIEngineError(str(e), f"Error tak terduga saat memanggil model AI: {e}")


def chat(user_message: str, history: List[dict], model_name: str) -> Dict[str, Any]:
    """
    High-level helper: bangun konteks dari history + pesan baru, panggil Ollama
    dengan model tier yang dipilih user, kembalikan dict (lihat call_ollama_chat).
    """
    messages = build_prompt_context(history, user_message)
    return call_ollama_chat(messages, model_name)
