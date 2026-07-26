#!/usr/bin/env bash

# Private AI Telegram Bot - Installer

# Instalasi via:
#   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/install.sh | sudo bash
#
# Sistem target: Ubuntu 20.04/22.04/24.04, RAM 16GB, CPU 8 Core
# Stack: Ollama (qwen2.5-coder:14b + qwen2.5vl) + python-telegram-bot (polling) + Systemd
#
# Bot berjalan mode POLLING (bukan webhook) sehingga TIDAK butuh domain,
# TIDAK butuh SSL/certbot, dan TIDAK butuh Nginx. Bot cukup terhubung ke
# internet untuk polling API Telegram.


set -euo pipefail

# -----------------------------------------------------------------------------
# KONFIGURASI - SESUAIKAN DENGAN REPO GITHUB ANDA
# -----------------------------------------------------------------------------
REPO_RAW_BASE="https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main"   # <-- GANTI placeholder ini
BOT_PY_URL="${REPO_RAW_BASE}/bot.py"
AI_ENGINE_PY_URL="${REPO_RAW_BASE}/ai_engine.py"
DATABASE_PY_URL="${REPO_RAW_BASE}/database.py"

APP_DIR="/opt/ai-bot"
VENV_DIR="${APP_DIR}/venv"
SERVICE_NAME="ai-bot"
SERVICE_USER="aibot"
OLLAMA_MODEL="qwen2.5-coder:14b"
OLLAMA_VISION_MODEL="qwen2.5vl"
ENV_FILE="${APP_DIR}/.env"

# Rentang versi python-telegram-bot yang sudah diuji cocok dengan kode bot ini
# (API modul telegram.ext & telegram.constants sejak v20 relatif stabil hingga v21.x).
PTB_VERSION_SPEC="python-telegram-bot[all]>=21.0,<22.0"

# -----------------------------------------------------------------------------
# WARNA UNTUK OUTPUT TERMINAL
# -----------------------------------------------------------------------------
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# -----------------------------------------------------------------------------
# CEK ROOT / SUDO
# -----------------------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
  log_error "Script ini harus dijalankan sebagai root atau menggunakan sudo."
  echo "Coba jalankan ulang dengan: curl -sL <url> | sudo bash"
  exit 1
fi

echo -e "${BOLD}=============================================${NC}"
echo -e "${BOLD}  Private AI Telegram Bot - Instalasi Otomatis${NC}"
echo -e "${BOLD}=============================================${NC}"
echo ""


# a. UPDATE OS DAN INSTALL DEPENDENCIES

log_info "Memperbarui daftar paket sistem..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y

log_info "Meng-upgrade paket sistem (ini mungkin memakan waktu)..."
apt-get upgrade -y

log_info "Menginstall dependencies: python3, sqlite3, ffmpeg, curl, git..."
apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  sqlite3 \
  ffmpeg \
  curl \
  git

log_success "Dependencies sistem terinstall."
echo ""


# b. INSTALL OLLAMA

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


# c. PULL MODEL AI (CHAT/CODING + VISION)

log_info "Menarik model AI (chat/coding): ${OLLAMA_MODEL} (ukuran besar, mohon tunggu)..."
ollama pull "${OLLAMA_MODEL}"
log_success "Model ${OLLAMA_MODEL} siap digunakan."

log_info "Menarik model AI (vision, untuk gambar & video): ${OLLAMA_VISION_MODEL}..."
ollama pull "${OLLAMA_VISION_MODEL}"
log_success "Model ${OLLAMA_VISION_MODEL} siap digunakan."
echo ""


# SETUP USER SISTEM UNTUK MENJALANKAN SERVICE (least privilege)

if id "${SERVICE_USER}" &>/dev/null; then
  log_warn "User sistem '${SERVICE_USER}' sudah ada."
else
  log_info "Membuat user sistem '${SERVICE_USER}' untuk menjalankan service..."
  useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi


# d. DOWNLOAD bot.py, ai_engine.py, database.py DARI REPOSITORY

log_info "Menyiapkan direktori aplikasi di ${APP_DIR}..."
mkdir -p "${APP_DIR}"

log_info "Mengunduh bot.py dari ${BOT_PY_URL}..."
curl -fsSL "${BOT_PY_URL}" -o "${APP_DIR}/bot.py"

log_info "Mengunduh ai_engine.py dari ${AI_ENGINE_PY_URL}..."
curl -fsSL "${AI_ENGINE_PY_URL}" -o "${APP_DIR}/ai_engine.py"

log_info "Mengunduh database.py dari ${DATABASE_PY_URL}..."
curl -fsSL "${DATABASE_PY_URL}" -o "${APP_DIR}/database.py"

log_success "File aplikasi berhasil diunduh."
echo ""


# SETUP PYTHON VIRTUAL ENVIRONMENT + DEPENDENCIES BOT

log_info "Membuat virtual environment Python..."
python3 -m venv "${VENV_DIR}"

log_info "Menginstall dependencies Python (python-telegram-bot, requests, pypdf, python-docx)..."
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -q \
  "${PTB_VERSION_SPEC}" \
  requests \
  pypdf \
  python-docx

log_success "Dependencies Python terinstall."
echo ""


# e. INPUT TOKEN BOT TELEGRAM DAN ID OWNER

echo -e "${BOLD}=================================================================${NC}"
echo -e "${BOLD}   KONFIGURASI BOT TELEGRAM${NC}"
echo -e "${BOLD}=================================================================${NC}"
echo ""
echo -e "Sebelum lanjut, siapkan dua hal berikut:"
echo -e "  1. ${YELLOW}Token Bot${NC} — dapatkan dari @BotFather di Telegram (perintah /newbot)."
echo -e "  2. ${YELLOW}ID Telegram Owner${NC} — dapatkan dari @userinfobot (kirim pesan apa saja"
echo -e "     ke bot itu, ID kamu akan ditampilkan sebagai angka)."
echo ""

# Jika dijalankan lewat 'curl | sudo bash', stdin adalah script itu sendiri,
# bukan terminal. Maka input interaktif diarahkan eksplisit ke /dev/tty agar
# prompt tetap berfungsi normal dalam mode instalasi one-liner.
if [[ -t 0 ]]; then
  TTY_IN=/dev/stdin
else
  TTY_IN=/dev/tty
fi

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

# bot.py TIDAK diubah/disisipi apa pun oleh installer ini. Ia hanya membaca
# TELEGRAM_BOT_TOKEN dan OWNER_TELEGRAM_ID dari environment variable.
# Nilai rahasia hanya pernah ditulis ke ${ENV_FILE}, file yang tidak pernah
# diunggah ke GitHub dan hanya bisa dibaca oleh root dan service_user (chmod 600).
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
EOF
chmod 600 "${ENV_FILE}"
log_success "Konfigurasi bot berhasil disimpan secara aman (chmod 600, tidak ada di source code)."

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
chmod 600 "${ENV_FILE}"
echo ""


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


# f. SETUP SYSTEMD SERVICE UNTUK BOT

log_info "Membuat systemd service '${SERVICE_NAME}'..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Private AI Telegram Bot
After=network-online.target ollama.service
Wants=network-online.target ollama.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment="OLLAMA_HOST=http://127.0.0.1:11434"
Environment="OLLAMA_MODEL=${OLLAMA_MODEL}"
Environment="OLLAMA_VISION_MODEL=${OLLAMA_VISION_MODEL}"
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


# g. OUTPUT INSTRUKSI ADMIN PASCA-INSTALL

echo ""
echo -e "${GREEN}${BOLD}=================================================================${NC}"
echo -e "${GREEN}${BOLD}   INSTALASI SELESAI - PRIVATE AI TELEGRAM BOT SIAP DIGUNAKAN${NC}"
echo -e "${GREEN}${BOLD}=================================================================${NC}"
echo ""
echo -e "${BLUE}${BOLD}1. CARA MENGAKSES BOT${NC}"
echo -e "   Buka Telegram, cari bot kamu berdasarkan username yang didaftarkan"
echo -e "   ke @BotFather, lalu kirim ${GREEN}/start${NC}."
echo -e "   Bot berjalan mode POLLING — tidak butuh domain atau SSL."
echo ""
echo -e "${BLUE}${BOLD}   MODEL AI YANG AKTIF${NC}"
echo -e "   Chat/Coding : ${GREEN}${OLLAMA_MODEL}${NC}"
echo -e "   Vision (gambar & video) : ${GREEN}${OLLAMA_VISION_MODEL}${NC}"
echo -e "   Upload mendukung: dokumen teks/code/PDF/DOCX, gambar, video, dan ZIP"
echo -e "   (isi ZIP diekstrak & tiap file diproses otomatis sesuai jenisnya)."
echo ""
echo -e "${BLUE}${BOLD}2. LIMIT CHAT USER${NC}"
echo -e "   Setiap user baru otomatis mendapat ${GREEN}20 chat/hari${NC} (reset otomatis tiap hari)."
echo -e "   Sebagai owner, kamu bisa menaikkan limit user lain dengan kode redeem:"
echo -e "     ${YELLOW}/gencode <limit> <hari>${NC}        contoh: /gencode 50 30"
echo -e "     ${YELLOW}/gencode unlimited <hari>${NC}       contoh: /gencode unlimited 365"
echo -e "   Kode yang dihasilkan lalu dibagikan ke user, mereka tinggal kirim:"
echo -e "     ${YELLOW}/redeem <kode>${NC}"
echo -e "   Setelah masa berlaku (hari) habis, limit user otomatis kembali ke 20/hari."
echo ""
echo -e "${BLUE}${BOLD}3. PERINTAH OWNER LAINNYA${NC}"
echo -e "   ${YELLOW}/codes${NC}              — lihat kode redeem yang belum dipakai"
echo -e "   ${YELLOW}/users${NC}              — lihat daftar user terdaftar & status limit mereka"
echo -e "   ${YELLOW}/ban <id>${NC}           — nonaktifkan akses user tertentu"
echo -e "   ${YELLOW}/unban <id>${NC}         — aktifkan kembali akses user"
echo -e "   ${YELLOW}/broadcast <pesan>${NC}  — kirim pesan ke semua user terdaftar"
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
echo -e "   Log bot (real-time):"
echo -e "      ${GREEN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
echo -e "   Log 100 baris terakhir:"
echo -e "      ${GREEN}sudo journalctl -u ${SERVICE_NAME} -n 100${NC}"
echo ""
echo -e "${BLUE}${BOLD}6. CARA MERESTART SERVICE${NC}"
echo -e "   Restart bot:"
echo -e "      ${GREEN}sudo systemctl restart ${SERVICE_NAME}${NC}"
echo -e "   Cek status bot:"
echo -e "      ${GREEN}sudo systemctl status ${SERVICE_NAME}${NC}"
echo -e "   Restart Ollama (jika model tidak merespons):"
echo -e "      ${GREEN}sudo systemctl restart ollama${NC}"
echo ""
echo -e "${BLUE}${BOLD}7. LOKASI FILE PENTING${NC}"
echo -e "   Bot            : ${GREEN}${APP_DIR}/bot.py${NC}"
echo -e "   AI Engine      : ${GREEN}${APP_DIR}/ai_engine.py${NC}"
echo -e "   Database layer : ${GREEN}${APP_DIR}/database.py${NC}"
echo -e "   Database chat  : ${GREEN}${APP_DIR}/bot_data.db${NC}"
echo -e "   Config rahasia : ${GREEN}${ENV_FILE}${NC}"
echo -e "   Service systemd: ${GREEN}/etc/systemd/system/${SERVICE_NAME}.service${NC}"
echo ""
echo -e "${YELLOW}${BOLD}PENTING - KEAMANAN:${NC}"
echo -e "  - Token bot & ID owner TIDAK ADA di ${APP_DIR}/bot.py maupun di repo GitHub Anda."
echo -e "  - Rahasia hanya ada di ${GREEN}${ENV_FILE}${NC} di server ini (chmod 600)."
echo -e "  - JANGAN PERNAH commit file .env ke Git. Tambahkan baris berikut ke"
echo -e "    .gitignore repo Anda jika belum ada: ${YELLOW}*.env${NC}"
echo -e "  - Hanya OWNER_TELEGRAM_ID yang bisa memakai /gencode, /users, /ban, dll."
echo -e "    Jaga ID ini tetap rahasia dari user biasa."
echo ""
echo -e "${GREEN}${BOLD}=================================================================${NC}"
