#!/usr/bin/env bash

# Enterprise Private AI Telegram Bot - Installer / Updater (v4 - Smart Hardware
# Cluster: GPU Detection, Auto Pull/Unload, Smart Queue, Model Fallback)

# Instalasi via:
#   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/install.sh | sudo bash
#
# Sistem target: Ubuntu 20.04/22.04/24.04, RAM 16GB+, CPU 8 Core+ (Worker Node
# dengan GPU disarankan untuk model >=14B, lihat worker_agent.py Smart GPU Detection)
# Stack: Ollama + python-telegram-bot (polling) + FastAPI (Worker Agent & Dashboard) + Systemd
#
# Bot berjalan mode POLLING (bukan webhook) sehingga TIDAK butuh domain,
# TIDAK butuh SSL/certbot, dan TIDAK butuh Nginx. Bot cukup terhubung ke
# internet untuk polling API Telegram. Dashboard Web tetap butuh port terbuka
# (default 8080) jika ingin diakses dari luar VPN/firewall internal.
#
# Script ini menyediakan MENU INTERAKTIF:
#   [1] Install Baru - Single Server (Ollama + Bot dalam 1 VPS, mode lama)
#   [2] Update Code & Pull Models - Single Server (Tanpa Menghapus Database)
#   [3] Install MASTER NODE (Cluster: Bot + Load Balancer + Web Dashboard)
#   [4] Install WORKER NODE (Cluster: Ollama + Worker Agent + Smart GPU Detection)


set -euo pipefail

# -----------------------------------------------------------------------------
# KONFIGURASI - SESUAIKAN DENGAN REPO GITHUB ANDA
# -----------------------------------------------------------------------------
REPO_GIT_URL="https://github.com/BimxyzDev/Private-AI-Telegram-Bot.git"   # <-- GANTI placeholder ini
REPO_BRANCH="main"

APP_DIR="/opt/ai-bot"
VENV_DIR="${APP_DIR}/venv"
SERVICE_NAME="ai-bot"
SERVICE_USER="aibot"
ENV_FILE="${APP_DIR}/.env"

# --- GitHub Backup & Restore (otomatis, hanya butuh PAT) ---
DB_FILE_NAME="bot_data.db"

# --- Sistem Role + Tier Model (General Chat & Coder/IT) ---
OLLAMA_MODEL_GENERAL_SUPER_RINGAN="qwen2.5:1.5b"
OLLAMA_MODEL_GENERAL_LIGHT="llama3.2:3b"
OLLAMA_MODEL_GENERAL_MEDIUM="llama3.1:8b"
OLLAMA_MODEL_GENERAL_HEAVY="gemma2:9b"
OLLAMA_MODEL_CODER_LIGHT="qwen2.5-coder:1.5b"
OLLAMA_MODEL_CODER_MEDIUM="qwen2.5-coder:7b"
OLLAMA_MODEL_CODER_HEAVY="qwen2.5-coder:14b"
OLLAMA_VISION_MODEL="qwen2.5vl"
ALL_MODELS=(
  "${OLLAMA_MODEL_GENERAL_SUPER_RINGAN}"
  "${OLLAMA_MODEL_GENERAL_LIGHT}"
  "${OLLAMA_MODEL_GENERAL_MEDIUM}"
  "${OLLAMA_MODEL_GENERAL_HEAVY}"
  "${OLLAMA_MODEL_CODER_LIGHT}"
  "${OLLAMA_MODEL_CODER_MEDIUM}"
  "${OLLAMA_MODEL_CODER_HEAVY}"
  "${OLLAMA_VISION_MODEL}"
)

# --- Role "extended": katalog 20+ model (CPU-only ringan s/d GPU besar) ---
# TIDAK di-pull otomatis secara default (banyak model besar & butuh GPU kuat,
# supaya instalasi standar tetap cepat/ringan). Set PULL_EXTENDED_MODELS=true
# sebelum menjalankan install.sh, atau gunakan /pullmodel dari Telegram setelah
# instalasi selesai (Auto Model Pull, lihat worker_agent.py) untuk pull model
# tertentu saja sesuai kebutuhan & kapasitas VRAM node.
PULL_EXTENDED_MODELS="${PULL_EXTENDED_MODELS:-false}"
EXTENDED_MODELS_NO_GPU=(
  "qwen2.5:0.5b" "qwen2.5:1.5b" "tinyllama:1.1b" "gemma2:2b"
  "phi3:3.8b" "llama3.2:3b" "qwen2.5:4b"
)
EXTENDED_MODELS_GPU_KECIL_MENENGAH=(
  "mistral:7b" "llama3.1:8b" "gemma2:9b" "qwen2.5:14b"
  "deepseek-r1:8b" "phi3:14b" "codellama:13b"
)
EXTENDED_MODELS_GPU_BESAR=(
  "qwen2.5:32b" "deepseek-r1:32b" "mixtral:8x7b" "gemma2:27b" "codellama:34b" "yi:34b"
)
EXTENDED_MODELS_GPU_SANGAT_BESAR=(
  "llama3.1:70b" "qwen2.5:72b" "deepseek-r1:70b" "llama3.1:405b"
)
EXTENDED_MODELS_ALL=(
  "${EXTENDED_MODELS_NO_GPU[@]}"
  "${EXTENDED_MODELS_GPU_KECIL_MENENGAH[@]}"
  "${EXTENDED_MODELS_GPU_BESAR[@]}"
  "${EXTENDED_MODELS_GPU_SANGAT_BESAR[@]}"
)
if [ "${PULL_EXTENDED_MODELS}" = "true" ]; then
  ALL_MODELS+=("${EXTENDED_MODELS_ALL[@]}")
fi

# Rentang versi python-telegram-bot yang sudah diuji cocok dengan kode bot ini
# (API modul telegram.ext & telegram.constants sejak v20 relatif stabil hingga v21.x).
PTB_VERSION_SPEC="python-telegram-bot[all]>=21.0,<22.0"
PIP_PACKAGES=("${PTB_VERSION_SPEC}" "requests" "pypdf" "python-docx")

# -----------------------------------------------------------------------------
# DISTRIBUTED CLUSTER ARCHITECTURE (Master-Worker) - KONFIGURASI ENTERPRISE
# -----------------------------------------------------------------------------
# Master Node : Telegram Bot + Smart Load Balancer (node_manager.py) + Web
#               Dashboard (web_app.py, FastAPI + HTML/JS/CSS terpisah di web/)
#               + SQLite DB. TIDAK menjalankan Ollama (kecuali Fallback opsional).
# Worker Node : Ollama + Worker Agent Enterprise (worker_agent.py) dengan Smart
#               GPU Detection, Auto Pull, Auto Unload, dan Safe Mode -- port 3716.
#               Bisa berjumlah banyak, didaftarkan ke Master lewat Admin Dashboard.
WORKER_APP_DIR="/opt/ai-worker"
WORKER_VENV_DIR="${WORKER_APP_DIR}/venv"
WORKER_SERVICE_NAME="ai-worker-agent"
WORKER_SERVICE_USER="aiworker"
WORKER_ENV_FILE="${WORKER_APP_DIR}/.env"
DEFAULT_WORKER_PORT=3716

DASHBOARD_SERVICE_NAME="ai-bot-dashboard"
DEFAULT_DASHBOARD_PORT=8080

FASTAPI_SPEC="fastapi>=0.110,<1.0"
UVICORN_SPEC="uvicorn[standard]>=0.27,<1.0"
PIP_PACKAGES_MASTER_EXTRA=("${FASTAPI_SPEC}" "${UVICORN_SPEC}")
# psutil wajib di Worker Node untuk Smart Hardware Detection (CPU/RAM); GPU
# terdeteksi lewat nvidia-smi (subprocess, tanpa dependency Python) dengan
# fallback opsional ke `torch.cuda` jika tersedia (TIDAK di-install otomatis
# oleh installer ini -- PyTorch besar, worker_agent.py sudah aman tanpa itu).
PIP_PACKAGES_WORKER=("${FASTAPI_SPEC}" "${UVICORN_SPEC}" "psutil>=5.9,<6.0" "requests")

# Default tuning Worker Agent Enterprise (bisa diedit lewat WORKER_ENV_FILE
# kapan saja setelah instalasi, tanpa perlu re-run installer):
DEFAULT_IDLE_UNLOAD_MINUTES=20
DEFAULT_SAFE_MODE_RAM_THRESHOLD=90
DEFAULT_SAFE_MODE_VRAM_THRESHOLD=85

# -----------------------------------------------------------------------------
# WARNA UNTUK OUTPUT TERMINAL
# -----------------------------------------------------------------------------
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m' # No Color

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_gpu()     { echo -e "${MAGENTA}[GPU]${NC} $1"; }

# -----------------------------------------------------------------------------
# CEK ROOT / SUDO
# -----------------------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
  log_error "Script ini harus dijalankan sebagai root atau menggunakan sudo."
  echo "Coba jalankan ulang dengan: curl -sL <url> | sudo bash"
  exit 1
fi

# Jika dijalankan lewat 'curl | sudo bash', stdin adalah script itu sendiri,
# bukan terminal. Maka input interaktif diarahkan eksplisit ke /dev/tty agar
# prompt tetap berfungsi normal dalam mode instalasi one-liner.
if [[ -t 0 ]]; then
  TTY_IN=/dev/stdin
else
  TTY_IN=/dev/tty
fi

echo -e "${BOLD}=============================================${NC}"
echo -e "${BOLD}  Enterprise Private AI Telegram Bot - Installer${NC}"
echo -e "${BOLD}  Smart Hardware Cluster · Zero-OOM · Auto Fallback${NC}"
echo -e "${BOLD}=============================================${NC}"
echo ""

# =============================================================================
# MENU INTERAKTIF
# =============================================================================
echo -e "Pilih mode instalasi:"
echo -e "  ${GREEN}[1]${NC} Install Baru - Single Server (Ollama + Bot dalam 1 VPS, mode lama)"
echo -e "  ${GREEN}[2]${NC} Update Code & Pull Models - Single Server (Tanpa Menghapus Database)"
echo -e "  ${GREEN}[3]${NC} Install MASTER NODE (Cluster: Bot + Smart Load Balancer + Web Dashboard)"
echo -e "  ${GREEN}[4]${NC} Install WORKER NODE (Cluster: Ollama + Worker Agent + Smart GPU Detection)"
echo ""
echo -e "${YELLOW}Catatan:${NC} Pilih [3]/[4] jika ingin menyebar beban AI ke beberapa VPS"
echo -e "sekaligus (Distributed Cluster Architecture, dengan Smart Worker Selection,"
echo -e "Model Fallback otomatis, dan proteksi Zero-OOM). Pilih [1]/[2] untuk tetap"
echo -e "memakai 1 VPS saja (Ollama & Bot di server yang sama)."
echo ""
read -r -p "Masukkan pilihan [1/2/3/4]: " INSTALL_MODE < "${TTY_IN}"
while [[ "${INSTALL_MODE}" != "1" && "${INSTALL_MODE}" != "2" && "${INSTALL_MODE}" != "3" && "${INSTALL_MODE}" != "4" ]]; do
  log_warn "Pilihan tidak valid. Ketik 1, 2, 3, atau 4."
  read -r -p "Masukkan pilihan [1/2/3/4]: " INSTALL_MODE < "${TTY_IN}"
done
echo ""

if [[ "${INSTALL_MODE}" == "2" && ! -d "${APP_DIR}" ]]; then
  log_error "Mode Update dipilih tapi ${APP_DIR} belum ada. Jalankan mode [1] Install Baru terlebih dahulu."
  exit 1
fi

# =============================================================================
# FUNGSI BERSAMA
# =============================================================================

install_system_dependencies() {
  log_info "Memperbarui daftar paket sistem..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y

  log_info "Menginstall dependencies: python3, sqlite3, ffmpeg, curl, git..."
  apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    sqlite3 \
    ffmpeg \
    curl \
    git \
    pciutils

  log_success "Dependencies sistem terinstall."
  echo ""
}

install_ollama_if_needed() {
  if command -v ollama &>/dev/null; then
    log_warn "Ollama sudah terinstall, melewati instalasi ulang."
  else
    log_info "Menginstall Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    log_success "Ollama berhasil diinstall."
  fi

  log_info "Mengaktifkan dan menjalankan service Ollama..."
  systemctl enable ollama --now || true
  sleep 3
  echo ""
}

# ---------------------------------------------------------------------------
# SMART GPU DETECTION (Installer-level) - dipakai untuk menyapa user & memilih
# strategi resource limit yang tepat SEBELUM Worker Agent berjalan penuh.
# Deteksi runtime lengkap (VRAM real-time, CUDA version, dst) tetap dilakukan
# oleh worker_agent.py sendiri saat startup -- fungsi ini hanya level instalasi.
# ---------------------------------------------------------------------------
detect_gpu_at_install() {
  if command -v nvidia-smi &>/dev/null; then
    local gpu_name gpu_vram
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)"
    gpu_vram="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1)"
    if [[ -n "${gpu_name}" ]]; then
      log_gpu "GPU NVIDIA terdeteksi: ${gpu_name} (${gpu_vram} MB VRAM)."
      HAS_GPU_AT_INSTALL="true"
      return 0
    fi
  fi
  log_warn "Tidak ada GPU NVIDIA terdeteksi (nvidia-smi tidak ditemukan/gagal). Node ini akan berjalan CPU-Only."
  log_warn "Model yang tersedia otomatis dibatasi ke kelas ringan (≤8B) -- lihat Dynamic Model Availability di worker_agent.py."
  HAS_GPU_AT_INSTALL="false"
  return 1
}

# ---------------------------------------------------------------------------
# RESOURCE LIMIT OLLAMA (cgroups v2, 70% CPU + 70% RAM) - lihat ollama-limit.conf
# ---------------------------------------------------------------------------
# Dipanggil setelah Ollama diinstall di server yang JUGA menjalankan proses lain
# (Bot/Dashboard) -- yaitu mode Single-Server ([1]) dan mode Master Node dengan
# opsi "Ollama Fallback" diaktifkan ([3]). TIDAK dipanggil di Worker Node murni
# ([4]) karena Worker Node memang didedikasikan penuh untuk Ollama saja -- Worker
# Node justru punya proteksi SENDIRI yang lebih pintar (Safe Mode dinamis di
# worker_agent.py yang bereaksi terhadap RAM/VRAM real-time, bukan cgroups statis).
apply_ollama_resource_limit() {
  log_info "Menerapkan batas resource Ollama (maks 70% CPU & 70% RAM) agar Bot/Dashboard tidak crash/OOM..."

  local cpu_cores mem_total_kb mem_max_kb mem_high_kb cpu_quota_percent

  cpu_cores="$(nproc)"
  cpu_quota_percent=$(( cpu_cores * 70 ))

  mem_total_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  mem_max_kb=$(( mem_total_kb * 70 / 100 ))
  mem_high_kb=$(( mem_total_kb * 63 / 100 ))  # ~90% dari MemoryMax, sebagai soft-limit

  mkdir -p /etc/systemd/system/ollama.service.d
  cat > /etc/systemd/system/ollama.service.d/limit.conf <<EOF
# File ini di-generate otomatis oleh install.sh — lihat ollama-limit.conf di
# repo untuk penjelasan lengkap tiap opsi. Aman ditimpa ulang saat re-install.
[Service]
CPUQuota=${cpu_quota_percent}%
MemoryMax=${mem_max_kb}K
MemoryHigh=${mem_high_kb}K
OOMScoreAdjust=500
EOF

  systemctl daemon-reload
  systemctl restart ollama
  log_success "Batas resource Ollama aktif: CPUQuota=${cpu_quota_percent}% (${cpu_cores} core terdeteksi), MemoryMax=$(( mem_max_kb / 1024 ))MB."
  echo ""
}

pull_all_models() {
  # ollama pull idempotent: jika model sudah ada & up-to-date, hanya diverifikasi (cepat).
  for model in "${ALL_MODELS[@]}"; do
    log_info "Menarik model: ${model} (jika belum ada, mohon tunggu)..."
    ollama pull "${model}"
    log_success "Model ${model} siap digunakan."
  done
  echo ""
}

setup_python_venv() {
  if [[ ! -d "${VENV_DIR}" ]]; then
    log_info "Membuat virtual environment Python..."
    python3 -m venv "${VENV_DIR}"
  else
    log_info "Virtual environment sudah ada, dipakai ulang."
  fi

  log_info "Menginstall/memperbarui dependencies Python (python-telegram-bot, requests, pypdf, python-docx)..."
  "${VENV_DIR}/bin/pip" install --upgrade pip -q
  "${VENV_DIR}/bin/pip" install -q --upgrade "${PIP_PACKAGES[@]}"

  log_success "Dependencies Python terinstall/terbaru."
  echo ""
}

migrate_database() {
  log_info "Menjalankan migrasi database (menambahkan kolom baru jika belum ada, termasuk skema hardware Worker Node)..."
  if "${VENV_DIR}/bin/python3" -c "
import sys
sys.path.insert(0, '${APP_DIR}')
import database as db
db.set_db_path('${APP_DIR}/bot_data.db')
db.init_db()
print('Migrasi database selesai (kuota token per-user, model_role + model_tier, redeem token_value, worker_nodes hardware, queue_events siap).')
"; then
    log_success "Migrasi database berhasil. Database & riwayat chat lama tetap aman/tidak terhapus."
  else
    log_error "Migrasi database GAGAL. Cek pesan error di atas."
    exit 1
  fi
  echo ""
}

github_auto_setup() {
  # -----------------------------------------------------------------------
  # Backup/restore database TIDAK memakai `git` sama sekali, murni lewat
  # GitHub REST API (curl/python), dan disimpan terpisah total dari git repo
  # source code bot. Auto-backup rutin tiap 60 detik dilakukan oleh bot.py
  # lewat modul github_backup.py (Python), bukan oleh script ini.
  # -----------------------------------------------------------------------
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   GITHUB BACKUP & RESTORE DATABASE (OTOMATIS)${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""
  echo -e "${YELLOW}⚠️ MASUKKAN GITHUB PERSONAL ACCESS TOKEN (PAT) ⚠️${NC}"
  echo -e "Rekomendasi: Gunakan Fine-grained PAT yang di-scope ke 1 repo backup"
  echo -e "khusus (TERPISAH dari repo source code bot), dengan akses"
  echo -e "\"Repository permissions\" -> \"Contents\" -> \"Read and write\"."
  echo -e "--------------------------------------------------"
  read -r -p "GitHub PAT (kosongkan untuk skip fitur backup): " GH_PAT_INPUT < "${TTY_IN}"
  echo ""

  if [[ -z "${GH_PAT_INPUT// }" ]]; then
    log_warn "PAT kosong, fitur GitHub backup/restore dilewati (bisa disetel lagi lewat menu Update)."
    return 0
  fi

  read -r -p "Repo tujuan backup, format owner/repo (contoh: budi/bot-backup): " GH_REPO_INPUT < "${TTY_IN}"
  while [[ -z "${GH_REPO_INPUT// }" || "${GH_REPO_INPUT}" != */* ]]; do
    log_warn "Format harus owner/repo (contoh: budi/bot-backup)."
    read -r -p "Repo tujuan backup, format owner/repo: " GH_REPO_INPUT < "${TTY_IN}"
  done

  read -r -p "Branch repo backup [default: main]: " GH_BRANCH_INPUT < "${TTY_IN}"
  GH_BRANCH_INPUT="${GH_BRANCH_INPUT:-main}"

  # Repo backup TIDAK BOLEH sama dengan repo source code bot ini sendiri —
  # kalau sama, backup database akan tertimpa/bercampur dengan source code.
  SELF_REPO_NAME="$(basename "${REPO_GIT_URL}" .git)"
  if [[ "${GH_REPO_INPUT##*/}" == "${SELF_REPO_NAME}" ]]; then
    log_error "Repo backup tidak boleh sama dengan repo source code bot (${SELF_REPO_NAME})."
    log_error "Gunakan repo terpisah khusus untuk backup database. Fitur backup dilewati."
    return 0
  fi

  log_info "Memverifikasi akses PAT ke ${GH_REPO_INPUT}..."
  GH_CHECK_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${GH_PAT_INPUT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GH_REPO_INPUT}")"

  if [[ "${GH_CHECK_STATUS}" != "200" ]]; then
    log_error "Tidak bisa mengakses ${GH_REPO_INPUT} dengan PAT ini (HTTP ${GH_CHECK_STATUS})."
    log_error "Pastikan repo sudah ada dan PAT punya akses Contents: Read and write ke repo tsb."
    log_error "Fitur backup/restore dilewati (bisa disetel lagi lewat menu Update)."
    return 0
  fi
  log_success "Akses ke ${GH_REPO_INPUT} terverifikasi."

  GH_BACKUP_REPO="${GH_REPO_INPUT}"
  GH_BACKUP_BRANCH="${GH_BRANCH_INPUT}"
  GH_BACKUP_PATH="${DB_FILE_NAME}.gz"

  log_info "Mengecek apakah ada backup database lama di ${GH_BACKUP_REPO}..."
  GH_RESTORE_JSON="$(mktemp)"
  GH_RESTORE_STATUS="$(curl -s -o "${GH_RESTORE_JSON}" -w '%{http_code}' \
    -H "Authorization: Bearer ${GH_PAT_INPUT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GH_BACKUP_REPO}/contents/${GH_BACKUP_PATH}?ref=${GH_BACKUP_BRANCH}")"

  if [[ "${GH_RESTORE_STATUS}" == "200" ]]; then
    if python3 - "${GH_RESTORE_JSON}" "${APP_DIR}/${DB_FILE_NAME}" <<'PYEOF'
import base64
import gzip
import json
import sys

json_path, dest_path = sys.argv[1], sys.argv[2]
with open(json_path) as f:
    data = json.load(f)
raw = base64.b64decode(data["content"])
db_bytes = gzip.decompress(raw)
with open(dest_path, "wb") as out:
    out.write(db_bytes)
PYEOF
    then
      log_success "Database lama ditemukan di GitHub, berhasil di-restore ke ${APP_DIR}/${DB_FILE_NAME}."
    else
      log_warn "Ada file di ${GH_BACKUP_PATH} tapi gagal di-decode, database lokal tidak diubah."
    fi
  else
    log_warn "Belum ada backup di ${GH_BACKUP_REPO} (kemungkinan install pertama kali). Backup pertama akan dibuat otomatis oleh bot."
  fi
  rm -f "${GH_RESTORE_JSON}"

  GH_BACKUP_ENABLED="true"

  if [[ -f "${ENV_FILE}" ]]; then
    umask 077
    sed -i '/^GH_BACKUP_ENABLED=/d;/^GH_BACKUP_PAT=/d;/^GH_BACKUP_REPO=/d;/^GH_BACKUP_BRANCH=/d;/^GH_BACKUP_PATH=/d' "${ENV_FILE}"
    {
      echo "GH_BACKUP_ENABLED=true"
      echo "GH_BACKUP_PAT=${GH_PAT_INPUT}"
      echo "GH_BACKUP_REPO=${GH_BACKUP_REPO}"
      echo "GH_BACKUP_BRANCH=${GH_BACKUP_BRANCH}"
      echo "GH_BACKUP_PATH=${GH_BACKUP_PATH}"
    } >> "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  fi
  echo ""
}

setup_systemd_service() {
  log_info "Membuat/memperbarui systemd service '${SERVICE_NAME}'..."

  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Enterprise Private AI Telegram Bot
After=network-online.target ollama.service
Wants=network-online.target ollama.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment="OLLAMA_HOST=http://127.0.0.1:11434"
Environment="OLLAMA_MODEL_GENERAL_SUPER_RINGAN=${OLLAMA_MODEL_GENERAL_SUPER_RINGAN}"
Environment="OLLAMA_MODEL_GENERAL_LIGHT=${OLLAMA_MODEL_GENERAL_LIGHT}"
Environment="OLLAMA_MODEL_GENERAL_MEDIUM=${OLLAMA_MODEL_GENERAL_MEDIUM}"
Environment="OLLAMA_MODEL_GENERAL_HEAVY=${OLLAMA_MODEL_GENERAL_HEAVY}"
Environment="OLLAMA_MODEL_CODER_LIGHT=${OLLAMA_MODEL_CODER_LIGHT}"
Environment="OLLAMA_MODEL_CODER_MEDIUM=${OLLAMA_MODEL_CODER_MEDIUM}"
Environment="OLLAMA_MODEL_CODER_HEAVY=${OLLAMA_MODEL_CODER_HEAVY}"
Environment="OLLAMA_VISION_MODEL=${OLLAMA_VISION_MODEL}"
Environment="OLLAMA_NUM_CTX=2048"
Environment="AI_BOT_DB_PATH=${APP_DIR}/bot_data.db"
# Rahasia (TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID) dimuat dari file terpisah,
# tidak pernah ditulis langsung ke unit file ini maupun ke source code aplikasi.
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Hardening dasar
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
EOF

  log_info "Reload systemd daemon dan mengaktifkan service..."
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"

  sleep 4

  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    log_success "Service ${SERVICE_NAME} berhasil berjalan."
  else
    log_error "Service ${SERVICE_NAME} GAGAL berjalan. Cek log dengan: journalctl -u ${SERVICE_NAME} -n 50"
  fi
  echo ""
}

print_footer() {
  echo ""
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo -e "${GREEN}${BOLD}   SELESAI - ENTERPRISE PRIVATE AI TELEGRAM BOT SIAP DIGUNAKAN${NC}"
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}1. CARA MENGAKSES BOT${NC}"
  echo -e "   Buka Telegram, cari bot kamu, lalu kirim ${GREEN}/start${NC}."
  echo -e "   Bot berjalan mode POLLING — tidak butuh domain atau SSL."
  echo ""
  echo -e "${BLUE}${BOLD}   SISTEM ROLE + TIER MODEL (/model)${NC}"
  echo -e "   🗣️  General Chat:"
  echo -e "      ⚪ Super Ringan : ${GREEN}${OLLAMA_MODEL_GENERAL_SUPER_RINGAN}${NC}  (kuota token x1, tercepat)"
  echo -e "      🟢 Light        : ${GREEN}${OLLAMA_MODEL_GENERAL_LIGHT}${NC}  (kuota token x1, super irit CPU)"
  echo -e "      🟡 Medium       : ${GREEN}${OLLAMA_MODEL_GENERAL_MEDIUM}${NC}  (kuota token x2, default user baru)"
  echo -e "      🔴 Heavy        : ${GREEN}${OLLAMA_MODEL_GENERAL_HEAVY}${NC}  (kuota token x3, reasoning terbaik)"
  echo -e "   💻 Coder / IT:"
  echo -e "      🟢 Light  : ${GREEN}${OLLAMA_MODEL_CODER_LIGHT}${NC}  (kuota token x1, super irit CPU)"
  echo -e "      🟡 Medium : ${GREEN}${OLLAMA_MODEL_CODER_MEDIUM}${NC}    (kuota token x2)"
  echo -e "      🔴 Heavy  : ${GREEN}${OLLAMA_MODEL_CODER_HEAVY}${NC}   (kuota token x3, reasoning maksimal)"
  echo -e "   Vision    : ${GREEN}${OLLAMA_VISION_MODEL}${NC} (gambar & video, di luar sistem role/tier)"
  echo -e "   User memilih role lalu tier lewat perintah ${YELLOW}/model${NC} di Telegram (2 langkah)."
  echo ""
  echo -e "${BLUE}${BOLD}2. KUOTA TOKEN HARIAN${NC}"
  echo -e "   Setiap user baru otomatis mendapat ${GREEN}50.000 token/hari${NC} (reset tiap 24 jam)."
  echo -e "   Token terpakai = (prompt_eval_count + eval_count dari Ollama) x multiplier tier."
  echo -e "   Sebagai owner, kamu bisa menaikkan kuota user lain dengan kode redeem:"
  echo -e "     ${YELLOW}/gencode <jumlah_token> <hari>${NC}   contoh: /gencode 100000 30"
  echo -e "     ${YELLOW}/gencode unlimited <hari>${NC}        contoh: /gencode unlimited 365"
  echo -e "   Kode yang dihasilkan lalu dibagikan ke user, mereka tinggal kirim:"
  echo -e "     ${YELLOW}/redeem <kode>${NC}"
  echo -e "   Setelah masa berlaku (hari) habis, kuota user otomatis kembali ke 50.000/hari."
  echo ""
  echo -e "${BLUE}${BOLD}3. PERINTAH OWNER LAINNYA${NC}"
  echo -e "   ${YELLOW}/codes${NC}              — lihat kode redeem yang belum dipakai"
  echo -e "   ${YELLOW}/users${NC}              — lihat daftar user terdaftar & status kuota mereka"
  echo -e "   ${YELLOW}/ban <id>${NC}           — nonaktifkan akses user tertentu"
  echo -e "   ${YELLOW}/unban <id>${NC}         — aktifkan kembali akses user"
  echo -e "   ${YELLOW}/broadcast <pesan>${NC}  — kirim pesan ke semua user terdaftar"
  echo -e "   ${YELLOW}(Catatan: /hardware, /worker, /queue, /pullmodel, /unload, /modelsync${NC}"
  echo -e "   ${YELLOW}hanya aktif di mode Cluster (Master Node) — lihat opsi [3]/[4] di installer ini)${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}4. KONFIGURASI BOT (TOKEN & OWNER ID)${NC}"
  echo -e "   a. Edit file rahasia (BUKAN bot.py):"
  echo -e "      ${GREEN}sudo nano ${ENV_FILE}${NC}"
  echo -e "   b. Ubah nilai TELEGRAM_BOT_TOKEN atau OWNER_TELEGRAM_ID sesuai kebutuhan."
  echo -e "   c. Simpan file, lalu restart service:"
  echo -e "      ${GREEN}sudo systemctl restart ${SERVICE_NAME}${NC}"
  echo -e "   File ${ENV_FILE} tidak pernah disentuh oleh update source code,"
  echo -e "   jadi aman untuk mengganti token/owner kapan saja tanpa risiko tertimpa."
  echo ""
  echo -e "${BLUE}${BOLD}5. CARA MELIHAT LOG BOT${NC}"
  echo -e "   Log bot (real-time):    ${GREEN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
  echo -e "   Log 100 baris terakhir: ${GREEN}sudo journalctl -u ${SERVICE_NAME} -n 100${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}6. CARA MERESTART SERVICE${NC}"
  echo -e "   Restart bot:            ${GREEN}sudo systemctl restart ${SERVICE_NAME}${NC}"
  echo -e "   Cek status bot:         ${GREEN}sudo systemctl status ${SERVICE_NAME}${NC}"
  echo -e "   Restart Ollama:         ${GREEN}sudo systemctl restart ollama${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}7. LOKASI FILE PENTING${NC}"
  echo -e "   Aplikasi (git repo) : ${GREEN}${APP_DIR}${NC}"
  echo -e "   Database chat/user  : ${GREEN}${APP_DIR}/bot_data.db${NC}"
  echo -e "   Config rahasia      : ${GREEN}${ENV_FILE}${NC}"
  echo -e "   Service systemd     : ${GREEN}/etc/systemd/system/${SERVICE_NAME}.service${NC}"
  echo ""
  if grep -q '^GH_BACKUP_ENABLED=true' "${ENV_FILE}" 2>/dev/null; then
    GH_REPO_DISPLAY="$(grep '^GH_BACKUP_REPO=' "${ENV_FILE}" | cut -d= -f2)"
    GH_BRANCH_DISPLAY="$(grep '^GH_BACKUP_BRANCH=' "${ENV_FILE}" | cut -d= -f2)"
    GH_PATH_DISPLAY="$(grep '^GH_BACKUP_PATH=' "${ENV_FILE}" | cut -d= -f2)"
    echo -e "${BLUE}${BOLD}   BACKUP OTOMATIS DATABASE KE GITHUB${NC}"
    echo -e "   Status   : ${GREEN}AKTIF${NC} (upload lewat GitHub API tiap 60 detik jika ada perubahan)"
    echo -e "   Repo     : ${GREEN}https://github.com/${GH_REPO_DISPLAY}/blob/${GH_BRANCH_DISPLAY:-main}/${GH_PATH_DISPLAY:-${DB_FILE_NAME}.gz}${NC}"
    echo -e "   Metode   : REST API murni (bukan git) — repo source code bot tidak pernah tersentuh."
    echo ""
  fi
  echo -e "${BLUE}${BOLD}8. CARA UPDATE DI MASA DEPAN${NC}"
  echo -e "   Jalankan ulang installer ini dan pilih menu ${GREEN}[2] Update${NC}, atau pakai"
  echo -e "   updater mandiri: ${GREEN}sudo bash update.sh${NC} (auto-detect Single/Master/Worker)."
  echo -e "   Proses ini akan: git pull, update dependency Python, migrasi kolom DB baru"
  echo -e "   (tanpa menghapus data), pull model baru jika belum ada, lalu restart service."
  echo ""
  echo -e "${YELLOW}${BOLD}PENTING - KEAMANAN:${NC}"
  echo -e "  - Token bot & ID owner TIDAK ADA di source code maupun di repo GitHub Anda."
  echo -e "  - Rahasia hanya ada di ${GREEN}${ENV_FILE}${NC} di server ini (chmod 600)."
  echo -e "  - JANGAN PERNAH commit file .env ke Git (tambahkan ${YELLOW}*.env${NC} ke .gitignore)."
  echo -e "  - Hanya OWNER_TELEGRAM_ID yang bisa memakai /gencode, /users, /ban, dll."
  echo ""
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
}

# =============================================================================
# MODE [2] UPDATE: Update Code & Pull Models (Tanpa Menghapus Database)
# =============================================================================
run_update_flow() {
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   MODE UPDATE - Update Code & Pull Models (Database Dipertahankan)${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""

  if [[ ! -f "${ENV_FILE}" ]]; then
    log_error "File konfigurasi ${ENV_FILE} tidak ditemukan. Instalasi sebelumnya sepertinya rusak/tidak lengkap."
    log_error "Jalankan ulang installer ini dan pilih [1] Install Baru."
    exit 1
  fi

  install_system_dependencies
  install_ollama_if_needed

  if [[ -d "${APP_DIR}/.git" ]]; then
    log_info "Menjalankan git pull di ${APP_DIR}..."
    git config --global --add safe.directory "${APP_DIR}" || true
    if git -C "${APP_DIR}" remote get-url origin &>/dev/null; then
      git -C "${APP_DIR}" remote set-url origin "${REPO_GIT_URL}"
    else
      git -C "${APP_DIR}" remote add origin "${REPO_GIT_URL}"
    fi
    git -C "${APP_DIR}" fetch origin
    git -C "${APP_DIR}" reset --hard "origin/${REPO_BRANCH}"
    log_success "Code berhasil diperbarui via git pull."
  else
    log_warn "${APP_DIR} bukan git repository (instalasi lama berbasis curl per-file)."
    log_info "Meng-clone ulang repo terbaru ke direktori sementara lalu menyalin source code..."
    TMP_CLONE="$(mktemp -d)"
    git clone --branch "${REPO_BRANCH}" --depth 1 "${REPO_GIT_URL}" "${TMP_CLONE}"
    cp -f "${TMP_CLONE}/bot.py" "${APP_DIR}/bot.py"
    cp -f "${TMP_CLONE}/ai_engine.py" "${APP_DIR}/ai_engine.py"
    cp -f "${TMP_CLONE}/database.py" "${APP_DIR}/database.py"
    cp -f "${TMP_CLONE}/github_backup.py" "${APP_DIR}/github_backup.py" 2>/dev/null || true
    rm -rf "${TMP_CLONE}"
    log_success "Source code berhasil diperbarui."
  fi
  echo ""

  setup_python_venv

  if ! grep -q '^GH_BACKUP_ENABLED=true' "${ENV_FILE}" 2>/dev/null; then
    log_info "Fitur GitHub backup/restore belum dikonfigurasi. Setup sekarang (opsional)..."
    github_auto_setup
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    chmod 600 "${ENV_FILE}"
  fi

  migrate_database
  pull_all_models
  apply_ollama_resource_limit

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
  chmod 600 "${ENV_FILE}"

  setup_systemd_service
  print_footer
}

# =============================================================================
# MODE [1] FRESH INSTALL
# =============================================================================
run_fresh_install_flow() {
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   MODE INSTALL BARU (FRESH INSTALLATION)${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""

  install_system_dependencies
  detect_gpu_at_install || true
  install_ollama_if_needed
  pull_all_models
  apply_ollama_resource_limit
  if id "${SERVICE_USER}" &>/dev/null; then
    log_warn "User sistem '${SERVICE_USER}' sudah ada."
  else
    log_info "Membuat user sistem '${SERVICE_USER}' untuk menjalankan service..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi

  # CLONE REPOSITORY (bot.py, ai_engine.py, database.py, dst)
  log_info "Menyiapkan direktori aplikasi di ${APP_DIR}..."
  if [[ -d "${APP_DIR}" ]]; then
    log_warn "${APP_DIR} sudah ada. File .env (jika ada) akan tetap dipertahankan."
  fi
  mkdir -p "${APP_DIR}"

  if [[ -d "${APP_DIR}/.git" ]]; then
    log_warn "Repo git sudah ada di ${APP_DIR}, menjalankan git pull alih-alih clone ulang..."
    git config --global --add safe.directory "${APP_DIR}" || true
    if git -C "${APP_DIR}" remote get-url origin &>/dev/null; then
      git -C "${APP_DIR}" remote set-url origin "${REPO_GIT_URL}"
    else
      git -C "${APP_DIR}" remote add origin "${REPO_GIT_URL}"
    fi
    git -C "${APP_DIR}" fetch origin
    git -C "${APP_DIR}" reset --hard "origin/${REPO_BRANCH}"
  else
    log_info "Meng-clone repository dari ${REPO_GIT_URL}..."
    TMP_CLONE="$(mktemp -d)"
    git clone --branch "${REPO_BRANCH}" --depth 1 "${REPO_GIT_URL}" "${TMP_CLONE}"
    # Salin isi repo ke APP_DIR tanpa menimpa .env yang mungkin sudah ada dari percobaan sebelumnya
    cp -rf "${TMP_CLONE}/." "${APP_DIR}/"
    rm -rf "${TMP_CLONE}"
  fi
  log_success "File aplikasi berhasil disiapkan."

  # Proteksi tambahan (local-only, tidak ikut ke-reset oleh git pull upstream):
  # pastikan .env & file database TIDAK PERNAH bisa ikut ter-track oleh repo
  # source code bot ini, seandainya ada yang menjalankan `git add .` manual.
  if [[ -d "${APP_DIR}/.git" ]]; then
    {
      echo ".env"
      echo "${DB_FILE_NAME}"
      echo "${DB_FILE_NAME}.gz"
      echo "venv/"
    } > "${APP_DIR}/.git/info/exclude"
  fi
  echo ""

  setup_python_venv

  # INPUT TOKEN BOT TELEGRAM DAN ID OWNER
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   KONFIGURASI BOT TELEGRAM${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""
  echo -e "Sebelum lanjut, siapkan dua hal berikut:"
  echo -e "  1. ${YELLOW}Token Bot${NC} — dapatkan dari @BotFather di Telegram (perintah /newbot)."
  echo -e "  2. ${YELLOW}ID Telegram Owner${NC} — dapatkan dari @userinfobot (kirim pesan apa saja"
  echo -e "     ke bot itu, ID kamu akan ditampilkan sebagai angka)."
  echo ""

  read -r -p "Masukkan Token Bot Telegram: " BOT_TOKEN_INPUT < "${TTY_IN}"
  while [[ -z "${BOT_TOKEN_INPUT// }" ]]; do
    log_warn "Token bot tidak boleh kosong."
    read -r -p "Masukkan Token Bot Telegram: " BOT_TOKEN_INPUT < "${TTY_IN}"
  done

  read -r -p "Masukkan ID Telegram Owner (angka): " OWNER_ID_INPUT < "${TTY_IN}"
  while ! [[ "${OWNER_ID_INPUT// }" =~ ^-?[0-9]+$ ]]; do
    log_warn "ID Owner harus berupa angka (contoh: 123456789)."
    read -r -p "Masukkan ID Telegram Owner (angka): " OWNER_ID_INPUT < "${TTY_IN}"
  done

  echo ""
  log_success "Konfigurasi bot diterima."
  echo ""

  # SECRET MANAGEMENT - TOKEN & ID OWNER DISIMPAN DI FILE .env, BUKAN DI SOURCE CODE
  if [[ -f "${ENV_FILE}" ]]; then
    log_warn "File .env sudah ada di ${ENV_FILE} — akan ditimpa dengan konfigurasi baru yang baru saja diinput."
  fi

  log_info "Menyimpan konfigurasi ke ${ENV_FILE}..."
  umask 077
  cat > "${ENV_FILE}" <<EOF
# File ini berisi rahasia (secrets). JANGAN commit/upload ke Git atau tempat publik manapun.
# Tambahkan "*.env" ke .gitignore repo Anda.
TELEGRAM_BOT_TOKEN=${BOT_TOKEN_INPUT}
OWNER_TELEGRAM_ID=${OWNER_ID_INPUT}
GH_BACKUP_ENABLED=${GH_BACKUP_ENABLED:-false}
GH_BACKUP_PAT=${GH_PAT_INPUT:-}
GH_BACKUP_REPO=${GH_BACKUP_REPO:-}
GH_BACKUP_BRANCH=${GH_BACKUP_BRANCH:-main}
GH_BACKUP_PATH=${GH_BACKUP_PATH:-${DB_FILE_NAME}.gz}
EOF
  chmod 600 "${ENV_FILE}"
  log_success "Konfigurasi bot berhasil disimpan secara aman (chmod 600, tidak ada di source code)."

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
  chmod 600 "${ENV_FILE}"
  echo ""

  github_auto_setup
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

  migrate_database

  # VALIDASI CEPAT: PASTIKAN SEMUA MODUL BISA DI-IMPORT SEBELUM SERVICE DIAKTIFKAN
  log_info "Memvalidasi instalasi (import check)..."
  if "${VENV_DIR}/bin/python3" -c "
import sys
sys.path.insert(0, '${APP_DIR}')
import telegram
import database
import ai_engine
print('OK - python-telegram-bot versi', telegram.__version__)
" ; then
    log_success "Validasi modul berhasil."
  else
    log_error "Validasi modul GAGAL. Cek pesan error di atas sebelum melanjutkan."
    log_error "Service TIDAK akan diaktifkan sampai masalah ini diperbaiki."
    exit 1
  fi
  echo ""

  setup_systemd_service
  print_footer
}

# =============================================================================
# CLUSTER: MASTER NODE (Bot + Smart Load Balancer + Web Dashboard, TANPA Ollama lokal)
# =============================================================================
setup_master_bot_systemd() {
  log_info "Membuat/memperbarui systemd service '${SERVICE_NAME}' (Master - Telegram Bot)..."
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Enterprise AI Telegram Bot - Master Node (Smart Load Balancer)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment="CLUSTER_MODE=master"
Environment="OLLAMA_NUM_CTX=2048"
Environment="AI_BOT_DB_PATH=${APP_DIR}/bot_data.db"
Environment="NODE_HEALTH_CHECK_INTERVAL=7"
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  sleep 4

  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    log_success "Service ${SERVICE_NAME} (Master Bot) berhasil berjalan."
  else
    log_error "Service ${SERVICE_NAME} GAGAL berjalan. Cek log: journalctl -u ${SERVICE_NAME} -n 50"
  fi
  echo ""
}

setup_master_dashboard_systemd() {
  log_info "Membuat/memperbarui systemd service '${DASHBOARD_SERVICE_NAME}' (Web Dashboard, FastAPI + web/ statis)..."
  cat > "/etc/systemd/system/${DASHBOARD_SERVICE_NAME}.service" <<EOF
[Unit]
Description=Enterprise AI Telegram Bot - Cluster Dashboard (Master Node)
After=network-online.target ${SERVICE_NAME}.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment="DB_PATH=${APP_DIR}/bot_data.db"
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn web_app:app --host 0.0.0.0 --port ${DASHBOARD_PORT_INPUT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${DASHBOARD_SERVICE_NAME}"
  systemctl restart "${DASHBOARD_SERVICE_NAME}"
  sleep 3

  if systemctl is-active --quiet "${DASHBOARD_SERVICE_NAME}"; then
    log_success "Service ${DASHBOARD_SERVICE_NAME} (Dashboard) berhasil berjalan di port ${DASHBOARD_PORT_INPUT}."
  else
    log_error "Service ${DASHBOARD_SERVICE_NAME} GAGAL berjalan. Cek log: journalctl -u ${DASHBOARD_SERVICE_NAME} -n 50"
  fi
  echo ""
}

print_master_footer() {
  SERVER_IP_HINT="$(curl -s --max-time 3 https://api.ipify.org || echo '<IP-VPS-Anda>')"
  echo ""
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo -e "${GREEN}${BOLD}   MASTER NODE SIAP - ENTERPRISE CLUSTER ARCHITECTURE${NC}"
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}1. TELEGRAM BOT${NC}"
  echo -e "   Kirim ${GREEN}/start${NC} ke bot Anda di Telegram. Bot akan menjawab TAPI"
  echo -e "   ${YELLOW}TIDAK ADA Worker Node terdaftar sampai Anda menambahkannya${NC} (langkah 3)."
  echo ""
  echo -e "${BLUE}${BOLD}2. WEB DASHBOARD (Status Publik + Admin)${NC}"
  echo -e "   Status publik cluster (mission-control view) : ${GREEN}http://${SERVER_IP_HINT}:${DASHBOARD_PORT_INPUT}/${NC}"
  echo -e "   Admin panel (kelola node, pull/unload model)  : ${GREEN}http://${SERVER_IP_HINT}:${DASHBOARD_PORT_INPUT}/admin${NC}"
  echo -e "   Login admin : ${GREEN}${ADMIN_USERNAME_INPUT}${NC} / (password sesuai input Anda tadi)"
  echo -e "   ${YELLOW}Buka port ${DASHBOARD_PORT_INPUT}/tcp di firewall/security-group VPS ini.${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}3. MENAMBAHKAN WORKER NODE${NC}"
  echo -e "   a. Jalankan installer ini di VPS lain (idealnya dengan GPU), pilih ${GREEN}[4] Install Worker Node${NC}."
  echo -e "   b. Catat IP VPS worker, port (default ${DEFAULT_WORKER_PORT}), dan API Key yang"
  echo -e "      ditampilkan di akhir instalasi worker tsb."
  echo -e "   c. Buka Admin Dashboard di atas -> isi form 'Tambah Worker Node' -> Submit."
  echo -e "   d. Node akan online otomatis dalam ${GREEN}~7 detik${NC} (health-check berkala),"
  echo -e "      TANPA perlu restart Telegram Bot maupun Dashboard."
  echo ""
  echo -e "${BLUE}${BOLD}4. SMART WORKER SELECTION & MODEL FALLBACK${NC}"
  echo -e "   Setiap chat/analisis gambar-video dari user dirutekan otomatis ke Worker Node"
  echo -e "   terbaik berdasarkan: ketersediaan model (ready/locked), sisa VRAM, load CPU/RAM,"
  echo -e "   dan slot antrian model tsb. Jika model yang diminta penuh di semua node, sistem"
  echo -e "   otomatis FALLBACK ke model lebih kecil dan memberi tahu user secara transparan."
  echo -e "   Jika node yang dipilih gagal di tengah jalan, request otomatis failover ke node lain."
  echo ""
  echo -e "${BLUE}${BOLD}5. PERINTAH ADMIN CLUSTER (khusus owner, di Telegram)${NC}"
  echo -e "   ${YELLOW}/hardware${NC}   — spek GPU/VRAM/CPU/RAM semua Worker Node"
  echo -e "   ${YELLOW}/worker${NC}     — ringkasan status cluster (online/offline, model ready/locked)"
  echo -e "   ${YELLOW}/queue${NC}      — antrian aktif + histori event (queued/fallback/failed)"
  echo -e "   ${YELLOW}/pullmodel${NC}  — pull model baru ke satu/semua Worker Node"
  echo -e "   ${YELLOW}/unload${NC}     — unload model tertentu dari VRAM node tertentu"
  echo -e "   ${YELLOW}/modelsync${NC}  — paksa refresh cache model dari Worker Node"
  echo ""
  echo -e "${BLUE}${BOLD}6. LOG & SERVICE${NC}"
  echo -e "   Log bot        : ${GREEN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
  echo -e "   Log dashboard  : ${GREEN}sudo journalctl -u ${DASHBOARD_SERVICE_NAME} -f${NC}"
  echo -e "   Restart bot    : ${GREEN}sudo systemctl restart ${SERVICE_NAME}${NC}"
  echo -e "   Restart dash.  : ${GREEN}sudo systemctl restart ${DASHBOARD_SERVICE_NAME}${NC}"
  echo -e "   Config rahasia : ${GREEN}${ENV_FILE}${NC}"
  echo ""
  echo -e "${YELLOW}${BOLD}PENTING - KEAMANAN:${NC}"
  echo -e "  - Ganti ADMIN_USERNAME/ADMIN_PASSWORD di ${ENV_FILE} kapan saja lalu restart"
  echo -e "    ${DASHBOARD_SERVICE_NAME} untuk menerapkannya."
  echo -e "  - API Key tiap Worker Node HARUS unik dan dijaga kerahasiaannya (dipakai di header"
  echo -e "    X-API-KEY saat Master memanggil Worker)."
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
}

install_master_node_flow() {
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   INSTALL MASTER NODE (ENTERPRISE CLUSTER ARCHITECTURE)${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""
  echo -e "Master Node menjalankan: Telegram Bot, Smart Load Balancer (node_manager.py),"
  echo -e "Web Dashboard (web_app.py, FastAPI + HTML/JS/CSS statis di web/), dan database SQLite."
  echo -e "${YELLOW}Secara default, Ollama TIDAK diinstall di sini${NC} -- AI generation"
  echo -e "dijalankan oleh Worker Node yang Anda daftarkan lewat Admin Dashboard nanti."
  echo ""
  echo -e "${BOLD}--- Opsi: Ollama Fallback di Master Node (opsional) ---${NC}"
  echo -e "Jika diaktifkan, Master Node JUGA menjalankan Ollama secara lokal sebagai"
  echo -e "cadangan -- ai_engine.py akan otomatis memakainya HANYA jika semua Worker"
  echo -e "Node di cluster sedang offline/tidak tersedia (lihat node_manager.generate)."
  echo -e "${YELLOW}Resource Ollama otomatis dibatasi maksimal 70% CPU & 70% RAM${NC}"
  echo -e "(lihat ollama-limit.conf) agar Bot/Dashboard tetap stabil dan tidak OOM."
  echo ""
  read -r -p "Aktifkan Ollama Fallback di Master Node ini? [y/N]: " OLLAMA_FALLBACK_INPUT < "${TTY_IN}"
  OLLAMA_FALLBACK_INPUT="${OLLAMA_FALLBACK_INPUT:-n}"
  if [[ "${OLLAMA_FALLBACK_INPUT,,}" == "y" || "${OLLAMA_FALLBACK_INPUT,,}" == "yes" ]]; then
    MASTER_OLLAMA_FALLBACK="true"
    log_info "Ollama Fallback AKAN diinstall & dibatasi 70% CPU/RAM di Master Node ini."
  else
    MASTER_OLLAMA_FALLBACK="false"
    log_info "Ollama Fallback dilewati -- Master Node murni Bot+DB+Dashboard (default)."
  fi
  echo ""

  install_system_dependencies

  if id "${SERVICE_USER}" &>/dev/null; then
    log_warn "User sistem '${SERVICE_USER}' sudah ada."
  else
    log_info "Membuat user sistem '${SERVICE_USER}'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi

  log_info "Menyiapkan direktori aplikasi di ${APP_DIR}..."
  mkdir -p "${APP_DIR}"
  if [[ -d "${APP_DIR}/.git" ]]; then
    git config --global --add safe.directory "${APP_DIR}" || true
    if git -C "${APP_DIR}" remote get-url origin &>/dev/null; then
      git -C "${APP_DIR}" remote set-url origin "${REPO_GIT_URL}"
    else
      git -C "${APP_DIR}" remote add origin "${REPO_GIT_URL}"
    fi
    git -C "${APP_DIR}" fetch origin
    git -C "${APP_DIR}" reset --hard "origin/${REPO_BRANCH}"
  else
    log_info "Meng-clone repository dari ${REPO_GIT_URL}..."
    TMP_CLONE="$(mktemp -d)"
    git clone --branch "${REPO_BRANCH}" --depth 1 "${REPO_GIT_URL}" "${TMP_CLONE}"
    cp -rf "${TMP_CLONE}/." "${APP_DIR}/"
    rm -rf "${TMP_CLONE}"
  fi
  if [[ -d "${APP_DIR}/.git" ]]; then
    { echo ".env"; echo "${DB_FILE_NAME}"; echo "${DB_FILE_NAME}.gz"; echo "venv/"; } > "${APP_DIR}/.git/info/exclude"
  fi
  if [[ ! -d "${APP_DIR}/web" ]]; then
    log_error "Folder web/ (index.html, admin.html, style.css, dashboard.js, admin.js) tidak ditemukan di repo."
    log_error "Web Dashboard TIDAK akan berfungsi tanpa folder ini. Periksa isi repo Anda."
  fi
  log_success "File aplikasi Master Node siap."
  echo ""

  setup_python_venv
  log_info "Menginstall dependency tambahan Master (fastapi, uvicorn) untuk Web Dashboard..."
  "${VENV_DIR}/bin/pip" install -q --upgrade "${PIP_PACKAGES_MASTER_EXTRA[@]}"
  log_success "Dependency Master lengkap."
  echo ""

  echo -e "${BOLD}--- Konfigurasi Bot Telegram ---${NC}"
  read -r -p "Masukkan Token Bot Telegram: " BOT_TOKEN_INPUT < "${TTY_IN}"
  while [[ -z "${BOT_TOKEN_INPUT// }" ]]; do
    log_warn "Token bot tidak boleh kosong."
    read -r -p "Masukkan Token Bot Telegram: " BOT_TOKEN_INPUT < "${TTY_IN}"
  done
  read -r -p "Masukkan ID Telegram Owner (angka): " OWNER_ID_INPUT < "${TTY_IN}"
  while ! [[ "${OWNER_ID_INPUT// }" =~ ^-?[0-9]+$ ]]; do
    log_warn "ID Owner harus berupa angka."
    read -r -p "Masukkan ID Telegram Owner (angka): " OWNER_ID_INPUT < "${TTY_IN}"
  done
  echo ""

  echo -e "${BOLD}--- Konfigurasi Web Dashboard (Admin Panel) ---${NC}"
  read -r -p "Username admin dashboard: " ADMIN_USERNAME_INPUT < "${TTY_IN}"
  while [[ -z "${ADMIN_USERNAME_INPUT// }" ]]; do
    log_warn "Username tidak boleh kosong."
    read -r -p "Username admin dashboard: " ADMIN_USERNAME_INPUT < "${TTY_IN}"
  done
  read -r -s -p "Password admin dashboard: " ADMIN_PASSWORD_INPUT < "${TTY_IN}"
  echo ""
  while [[ -z "${ADMIN_PASSWORD_INPUT// }" ]]; do
    log_warn "Password tidak boleh kosong."
    read -r -s -p "Password admin dashboard: " ADMIN_PASSWORD_INPUT < "${TTY_IN}"
    echo ""
  done
  read -r -p "Port Web Dashboard [default: ${DEFAULT_DASHBOARD_PORT}]: " DASHBOARD_PORT_INPUT < "${TTY_IN}"
  DASHBOARD_PORT_INPUT="${DASHBOARD_PORT_INPUT:-${DEFAULT_DASHBOARD_PORT}}"
  echo ""

  log_info "Menyimpan konfigurasi ke ${ENV_FILE}..."
  umask 077
  cat > "${ENV_FILE}" <<EOF
# File ini berisi rahasia (secrets). JANGAN commit/upload ke Git atau tempat publik manapun.
TELEGRAM_BOT_TOKEN=${BOT_TOKEN_INPUT}
OWNER_TELEGRAM_ID=${OWNER_ID_INPUT}
CLUSTER_MODE=master
MASTER_OLLAMA_FALLBACK=${MASTER_OLLAMA_FALLBACK}
ADMIN_USERNAME=${ADMIN_USERNAME_INPUT}
ADMIN_PASSWORD=${ADMIN_PASSWORD_INPUT}
DASHBOARD_PORT=${DASHBOARD_PORT_INPUT}
GH_BACKUP_ENABLED=${GH_BACKUP_ENABLED:-false}
GH_BACKUP_PAT=${GH_PAT_INPUT:-}
GH_BACKUP_REPO=${GH_BACKUP_REPO:-}
GH_BACKUP_BRANCH=${GH_BACKUP_BRANCH:-main}
GH_BACKUP_PATH=${GH_BACKUP_PATH:-${DB_FILE_NAME}.gz}
EOF
  chmod 600 "${ENV_FILE}"
  log_success "Konfigurasi Master Node disimpan (chmod 600)."
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
  echo ""

  github_auto_setup
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

  migrate_database

  if [[ "${MASTER_OLLAMA_FALLBACK}" == "true" ]]; then
    log_info "Menginstall Ollama sebagai Fallback di Master Node (opsi diaktifkan)..."
    install_ollama_if_needed
    pull_all_models
    apply_ollama_resource_limit
  fi

  log_info "Memvalidasi instalasi Master (import check)..."
  if "${VENV_DIR}/bin/python3" -c "
import sys
sys.path.insert(0, '${APP_DIR}')
import telegram, database, ai_engine, node_manager, web_app
print('OK - modul Master Node lengkap (bot + node_manager + web_app dashboard).')
"; then
    log_success "Validasi modul Master berhasil."
  else
    log_error "Validasi modul GAGAL. Service TIDAK diaktifkan sampai masalah diperbaiki."
    exit 1
  fi
  echo ""

  setup_master_bot_systemd
  setup_master_dashboard_systemd
  print_master_footer
}

# =============================================================================
# CLUSTER: WORKER NODE (Ollama + Worker Agent Enterprise, port 3716)
# =============================================================================
setup_worker_systemd() {
  log_info "Membuat/memperbarui systemd service '${WORKER_SERVICE_NAME}'..."
  cat > "/etc/systemd/system/${WORKER_SERVICE_NAME}.service" <<EOF
[Unit]
Description=Enterprise AI Bot Cluster - Worker Node Agent (Smart GPU/Hardware Management)
After=network-online.target ollama.service
Wants=network-online.target ollama.service

[Service]
Type=simple
User=${WORKER_SERVICE_USER}
Group=${WORKER_SERVICE_USER}
WorkingDirectory=${WORKER_APP_DIR}
EnvironmentFile=${WORKER_ENV_FILE}
ExecStart=${WORKER_VENV_DIR}/bin/uvicorn worker_agent:app --host 0.0.0.0 --port ${WORKER_PORT_INPUT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${WORKER_APP_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${WORKER_SERVICE_NAME}"
  systemctl restart "${WORKER_SERVICE_NAME}"
  sleep 3

  if systemctl is-active --quiet "${WORKER_SERVICE_NAME}"; then
    log_success "Service ${WORKER_SERVICE_NAME} berhasil berjalan di port ${WORKER_PORT_INPUT}."
  else
    log_error "Service ${WORKER_SERVICE_NAME} GAGAL berjalan. Cek log: journalctl -u ${WORKER_SERVICE_NAME} -n 50"
  fi
  echo ""
}

print_worker_footer() {
  SERVER_IP_HINT="$(curl -s --max-time 3 https://api.ipify.org || echo '<IP-VPS-Anda>')"
  echo ""
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo -e "${GREEN}${BOLD}   WORKER NODE SIAP (SMART HARDWARE MANAGEMENT AKTIF)${NC}"
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo ""
  if [[ "${HAS_GPU_AT_INSTALL:-false}" == "true" ]]; then
    echo -e "${MAGENTA}${BOLD}GPU TERDETEKSI${NC} — Dynamic Model Availability aktif: model hingga kelas"
    echo -e "sesuai VRAM node ini akan berstatus 'Ready', sisanya 'Locked' otomatis."
  else
    echo -e "${YELLOW}${BOLD}CPU-ONLY${NC} — hanya model ≤8B yang berstatus 'Ready' di node ini."
    echo -e "Pasang GPU NVIDIA + driver, lalu restart service ini untuk membuka model lebih besar."
  fi
  echo ""
  echo -e "Daftarkan node ini di Admin Dashboard Master Node dengan data berikut:"
  echo ""
  echo -e "   Host / IP  : ${GREEN}${SERVER_IP_HINT}${NC}"
  echo -e "   Port       : ${GREEN}${WORKER_PORT_INPUT}${NC}"
  echo -e "   API Key    : ${GREEN}${WORKER_API_KEY_INPUT}${NC}"
  echo ""
  echo -e "${YELLOW}Simpan API Key di atas -- tidak ditampilkan ulang secara otomatis.${NC}"
  echo -e "${YELLOW}Buka port ${WORKER_PORT_INPUT}/tcp di firewall/security-group VPS ini${NC}"
  echo -e "${YELLOW}(hanya perlu bisa diakses dari IP Master Node, idealnya dibatasi lewat firewall).${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}FITUR ENTERPRISE AKTIF DI NODE INI${NC}"
  echo -e "   ✓ Smart GPU Detection    — nvidia-smi + fallback torch.cuda, VRAM real-time"
  echo -e "   ✓ Dynamic Model Lock     — model dikunci/dibuka otomatis sesuai VRAM tersedia"
  echo -e "   ✓ Auto Pull & Cache      — model baru bisa ditarik lewat /pullmodel dari Telegram"
  echo -e "   ✓ Auto Unload Idle       — model idle > ${DEFAULT_IDLE_UNLOAD_MINUTES} menit otomatis dilepas dari VRAM"
  echo -e "   ✓ Safe Mode              — RAM≥${DEFAULT_SAFE_MODE_RAM_THRESHOLD}%% / VRAM≥${DEFAULT_SAFE_MODE_VRAM_THRESHOLD}%% otomatis kunci model besar"
  echo -e "   ✓ Smart Queue Limiter    — batas concurrent per ukuran model (cegah OOM/Kernel Panic)"
  echo -e "   (semua threshold di atas bisa diedit di ${WORKER_ENV_FILE})"
  echo ""
  echo -e "${BLUE}${BOLD}Model yang sudah di-pull di node ini:${NC}"
  for model in "${ALL_MODELS[@]}"; do
    echo -e "   - ${GREEN}${model}${NC}"
  done
  echo ""
  echo -e "${BLUE}${BOLD}LOG & SERVICE${NC}"
  echo -e "   Log worker agent : ${GREEN}sudo journalctl -u ${WORKER_SERVICE_NAME} -f${NC}"
  echo -e "   Restart worker   : ${GREEN}sudo systemctl restart ${WORKER_SERVICE_NAME}${NC}"
  echo -e "   Restart Ollama   : ${GREEN}sudo systemctl restart ollama${NC}"
  echo -e "   Config           : ${GREEN}${WORKER_ENV_FILE}${NC}"
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
}

install_worker_node_flow() {
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   INSTALL WORKER NODE (ENTERPRISE CLUSTER, SMART HARDWARE MGMT)${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""
  echo -e "Worker Node menjalankan: Ollama + Worker Agent Enterprise (FastAPI, port ${DEFAULT_WORKER_PORT})"
  echo -e "dengan Smart GPU Detection, Auto Pull/Unload, dan Safe Mode bawaan."
  echo -e "Setelah instalasi selesai, daftarkan node ini ke Master Node lewat Admin Dashboard."
  echo ""

  install_system_dependencies
  detect_gpu_at_install || true
  install_ollama_if_needed
  pull_all_models

  if id "${WORKER_SERVICE_USER}" &>/dev/null; then
    log_warn "User sistem '${WORKER_SERVICE_USER}' sudah ada."
  else
    log_info "Membuat user sistem '${WORKER_SERVICE_USER}'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${WORKER_SERVICE_USER}"
  fi

  log_info "Menyiapkan direktori Worker Agent di ${WORKER_APP_DIR}..."
  mkdir -p "${WORKER_APP_DIR}"
  TMP_CLONE="$(mktemp -d)"
  git clone --branch "${REPO_BRANCH}" --depth 1 "${REPO_GIT_URL}" "${TMP_CLONE}"
  cp -f "${TMP_CLONE}/worker_agent.py" "${WORKER_APP_DIR}/worker_agent.py"
  rm -rf "${TMP_CLONE}"
  log_success "worker_agent.py siap di ${WORKER_APP_DIR}."
  echo ""

  log_info "Membuat virtual environment Python untuk Worker Agent..."
  if [[ ! -d "${WORKER_VENV_DIR}" ]]; then
    python3 -m venv "${WORKER_VENV_DIR}"
  fi
  "${WORKER_VENV_DIR}/bin/pip" install --upgrade pip -q
  "${WORKER_VENV_DIR}/bin/pip" install -q --upgrade "${PIP_PACKAGES_WORKER[@]}"
  log_success "Dependency Worker Agent terinstall."
  echo ""

  echo -e "${BOLD}--- Konfigurasi Worker Agent ---${NC}"
  DEFAULT_GENERATED_KEY="$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 48)"
  read -r -p "API Key untuk node ini [default: acak aman, tekan Enter untuk pakai]: " WORKER_API_KEY_INPUT < "${TTY_IN}"
  WORKER_API_KEY_INPUT="${WORKER_API_KEY_INPUT:-${DEFAULT_GENERATED_KEY}}"

  read -r -p "Port Worker Agent [default: ${DEFAULT_WORKER_PORT}]: " WORKER_PORT_INPUT < "${TTY_IN}"
  WORKER_PORT_INPUT="${WORKER_PORT_INPUT:-${DEFAULT_WORKER_PORT}}"

  echo ""
  echo -e "${BOLD}--- Tuning Enterprise (opsional, tekan Enter untuk pakai default) ---${NC}"
  read -r -p "Auto-Unload idle timeout dalam menit [default: ${DEFAULT_IDLE_UNLOAD_MINUTES}]: " IDLE_UNLOAD_INPUT < "${TTY_IN}"
  IDLE_UNLOAD_INPUT="${IDLE_UNLOAD_INPUT:-${DEFAULT_IDLE_UNLOAD_MINUTES}}"
  read -r -p "Safe Mode RAM threshold %% [default: ${DEFAULT_SAFE_MODE_RAM_THRESHOLD}]: " SAFE_RAM_INPUT < "${TTY_IN}"
  SAFE_RAM_INPUT="${SAFE_RAM_INPUT:-${DEFAULT_SAFE_MODE_RAM_THRESHOLD}}"
  read -r -p "Safe Mode VRAM threshold %% [default: ${DEFAULT_SAFE_MODE_VRAM_THRESHOLD}]: " SAFE_VRAM_INPUT < "${TTY_IN}"
  SAFE_VRAM_INPUT="${SAFE_VRAM_INPUT:-${DEFAULT_SAFE_MODE_VRAM_THRESHOLD}}"
  echo ""

  log_info "Menyimpan konfigurasi ke ${WORKER_ENV_FILE}..."
  umask 077
  cat > "${WORKER_ENV_FILE}" <<EOF
# File ini berisi rahasia (API key). JANGAN bagikan ke pihak tidak tepercaya.
WORKER_API_KEY=${WORKER_API_KEY_INPUT}
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_NUM_CTX=2048
WORKER_REQUEST_TIMEOUT_SECONDS=600
# --- Enterprise tuning: Auto-Unload & Safe Mode (lihat worker_agent.py) ---
IDLE_UNLOAD_MINUTES=${IDLE_UNLOAD_INPUT}
SAFE_MODE_RAM_THRESHOLD=${SAFE_RAM_INPUT}
SAFE_MODE_VRAM_THRESHOLD=${SAFE_VRAM_INPUT}
EOF
  chmod 600 "${WORKER_ENV_FILE}"
  chown -R "${WORKER_SERVICE_USER}:${WORKER_SERVICE_USER}" "${WORKER_APP_DIR}"
  log_success "Konfigurasi Worker Node disimpan (chmod 600)."
  echo ""

  log_info "Memvalidasi instalasi Worker (import check)..."
  if "${WORKER_VENV_DIR}/bin/python3" -c "
import fastapi, uvicorn, psutil, requests
print('OK - modul Worker Agent lengkap.')
"; then
    log_success "Validasi modul Worker berhasil."
  else
    log_error "Validasi modul GAGAL. Service TIDAK diaktifkan sampai masalah diperbaiki."
    exit 1
  fi
  echo ""

  setup_worker_systemd
  print_worker_footer
}

# =============================================================================
# EKSEKUSI SESUAI PILIHAN MENU
# =============================================================================
case "${INSTALL_MODE}" in
  1) run_fresh_install_flow ;;
  2) run_update_flow ;;
  3) install_master_node_flow ;;
  4) install_worker_node_flow ;;
esac
