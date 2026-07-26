#!/usr/bin/env bash

# Private AI Telegram Bot - Installer / Updater (v2 - Sistem Token + 3 Tier Model)

# Instalasi via:
#   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/install.sh | sudo bash
#
# Sistem target: Ubuntu 20.04/22.04/24.04, RAM 16GB+, CPU 8 Core+
# Stack: Ollama (3 tier qwen2.5-coder + qwen2.5vl) + python-telegram-bot (polling) + Systemd
#
# Bot berjalan mode POLLING (bukan webhook) sehingga TIDAK butuh domain,
# TIDAK butuh SSL/certbot, dan TIDAK butuh Nginx. Bot cukup terhubung ke
# internet untuk polling API Telegram.
#
# Script ini menyediakan MENU INTERAKTIF:
#   [1] Install Baru (Fresh Installation)
#   [2] Update Code & Pull Models (Tanpa Menghapus Database)


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

# --- Sistem 3 Tier Model ---
OLLAMA_MODEL_LIGHT="qwen2.5-coder:1.5b"
OLLAMA_MODEL_MEDIUM="qwen2.5-coder:7b"
OLLAMA_MODEL_HEAVY="qwen2.5-coder:14b"
OLLAMA_VISION_MODEL="qwen2.5vl"
ALL_MODELS=("${OLLAMA_MODEL_LIGHT}" "${OLLAMA_MODEL_MEDIUM}" "${OLLAMA_MODEL_HEAVY}" "${OLLAMA_VISION_MODEL}")

# Rentang versi python-telegram-bot yang sudah diuji cocok dengan kode bot ini
# (API modul telegram.ext & telegram.constants sejak v20 relatif stabil hingga v21.x).
PTB_VERSION_SPEC="python-telegram-bot[all]>=21.0,<22.0"
PIP_PACKAGES=("${PTB_VERSION_SPEC}" "requests" "pypdf" "python-docx")

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

# Jika dijalankan lewat 'curl | sudo bash', stdin adalah script itu sendiri,
# bukan terminal. Maka input interaktif diarahkan eksplisit ke /dev/tty agar
# prompt tetap berfungsi normal dalam mode instalasi one-liner.
if [[ -t 0 ]]; then
  TTY_IN=/dev/stdin
else
  TTY_IN=/dev/tty
fi

echo -e "${BOLD}=============================================${NC}"
echo -e "${BOLD}  Private AI Telegram Bot - Installer${NC}"
echo -e "${BOLD}=============================================${NC}"
echo ""

# =============================================================================
# MENU INTERAKTIF
# =============================================================================
echo -e "Pilih mode instalasi:"
echo -e "  ${GREEN}[1]${NC} Install Baru (Fresh Installation)"
echo -e "  ${GREEN}[2]${NC} Update Code & Pull Models (Tanpa Menghapus Database)"
echo ""
read -r -p "Masukkan pilihan [1/2]: " INSTALL_MODE < "${TTY_IN}"
while [[ "${INSTALL_MODE}" != "1" && "${INSTALL_MODE}" != "2" ]]; do
  log_warn "Pilihan tidak valid. Ketik 1 atau 2."
  read -r -p "Masukkan pilihan [1/2]: " INSTALL_MODE < "${TTY_IN}"
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
    git

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
  log_info "Menjalankan migrasi database (menambahkan kolom baru jika belum ada)..."
  if "${VENV_DIR}/bin/python3" -c "
import sys
sys.path.insert(0, '${APP_DIR}')
import database as db
db.set_db_path('${APP_DIR}/bot_data.db')
db.init_db()
print('Migrasi database selesai (kuota token per-user, model_tier, redeem token_value siap).')
"; then
    log_success "Migrasi database berhasil. Database & riwayat chat lama tetap aman/tidak terhapus."
  else
    log_error "Migrasi database GAGAL. Cek pesan error di atas."
    exit 1
  fi
  echo ""
}

github_auto_setup() {
  # Input hanya PAT. Username & repo di-auto-detect via GitHub API (TIDAK membuat repo baru,
  # supaya kompatibel dengan Fine-grained PAT yang dikunci ke 1 repo spesifik).
  echo -e "${BOLD}=================================================================${NC}"
  echo -e "${BOLD}   GITHUB BACKUP & RESTORE DATABASE (OTOMATIS)${NC}"
  echo -e "${BOLD}=================================================================${NC}"
  echo ""
  echo -e "${YELLOW}⚠️ MASUKKAN GITHUB PERSONAL ACCESS TOKEN (PAT) ⚠️${NC}"
  echo -e "Rekomendasi: Gunakan Fine-grained PAT yang di-scope ke 1 repo backup"
  echo -e "khusus, dengan akses \"Repository permissions\" -> \"Contents\" -> \"Read and write\"."
  echo -e "--------------------------------------------------"
  read -r -p "GitHub PAT (kosongkan untuk skip fitur backup): " GH_PAT_INPUT < "${TTY_IN}"
  echo ""

  if [[ -z "${GH_PAT_INPUT// }" ]]; then
    log_warn "PAT kosong, fitur GitHub backup/restore dilewati (bisa disetel lagi lewat menu Update)."
    return 0
  fi

  log_info "Memverifikasi PAT & mendeteksi username GitHub..."
  GH_USER_JSON="$(curl -sf -H "Authorization: Bearer ${GH_PAT_INPUT}" -H "Accept: application/vnd.github+json" https://api.github.com/user || true)"
  GH_USERNAME="$(echo "${GH_USER_JSON}" | grep -o '"login"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/')"

  if [[ -z "${GH_USERNAME}" ]]; then
    log_error "PAT tidak valid / gagal menghubungi GitHub API. Fitur backup/restore dilewati."
    return 0
  fi
  log_success "Terautentikasi sebagai GitHub user: ${GH_USERNAME}"

  log_info "Mendeteksi repo yang diizinkan oleh PAT ini..."
  REPOS_RESPONSE="$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer ${GH_PAT_INPUT}" -H "Accept: application/vnd.github+json" "https://api.github.com/user/repos?per_page=100&sort=updated")"
  REPOS_STATUS="$(echo "${REPOS_RESPONSE}" | tail -n1)"
  REPOS_BODY="$(echo "${REPOS_RESPONSE}" | sed '$d')"

  if [[ "${REPOS_STATUS}" != "200" ]]; then
    log_error "Gagal mengambil daftar repo (HTTP ${REPOS_STATUS}). Fitur backup/restore dilewati."
    return 0
  fi

  # Nama repo source code bot ini sendiri (dari REPO_GIT_URL) -> JANGAN pernah dipakai
  # sebagai remote backup, walaupun PAT punya akses ke situ juga.
  SELF_REPO_NAME="$(basename "${REPO_GIT_URL}" .git)"

  # Ambil SEMUA nama repo (field "name" milik object repo) dari respons JSON, lalu buang duplikat.
  mapfile -t ALL_REPOS < <(echo "${REPOS_BODY}" | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"([^"]+)"$/\1/' | awk '!seen[$0]++')

  # Buang repo source code bot dari daftar kandidat backup.
  CANDIDATE_REPOS=()
  for r in "${ALL_REPOS[@]}"; do
    [[ "${r}" != "${SELF_REPO_NAME}" ]] && CANDIDATE_REPOS+=("${r}")
  done

  if [[ "${#CANDIDATE_REPOS[@]}" -eq 0 ]]; then
    log_error "PAT ini hanya punya akses ke repo source code bot (${SELF_REPO_NAME}) atau tidak ada repo sama sekali."
    log_error "Buat/scope PAT ke repo backup KHUSUS (terpisah dari repo source code bot)."
    log_error "Fitur backup/restore dilewati."
    return 0
  elif [[ "${#CANDIDATE_REPOS[@]}" -eq 1 ]]; then
    GH_DETECTED_REPO="${CANDIDATE_REPOS[0]}"
    log_success "Repo terdeteksi: ${GH_USERNAME}/${GH_DETECTED_REPO} (akan dipakai sebagai remote backup)."
  else
    log_warn "PAT ini punya akses ke lebih dari 1 repo, pilih mana yang dipakai untuk backup database:"
    for i in "${!CANDIDATE_REPOS[@]}"; do
      echo -e "  ${GREEN}[$((i+1))]${NC} ${CANDIDATE_REPOS[$i]}"
    done
    read -r -p "Masukkan nomor repo [1-${#CANDIDATE_REPOS[@]}]: " REPO_CHOICE < "${TTY_IN}"
    while ! [[ "${REPO_CHOICE}" =~ ^[0-9]+$ ]] || (( REPO_CHOICE < 1 || REPO_CHOICE > ${#CANDIDATE_REPOS[@]} )); do
      log_warn "Pilihan tidak valid."
      read -r -p "Masukkan nomor repo [1-${#CANDIDATE_REPOS[@]}]: " REPO_CHOICE < "${TTY_IN}"
    done
    GH_DETECTED_REPO="${CANDIDATE_REPOS[$((REPO_CHOICE-1))]}"
    log_success "Repo dipilih: ${GH_USERNAME}/${GH_DETECTED_REPO}"
  fi

  GH_REMOTE_URL="https://${GH_PAT_INPUT}@github.com/${GH_USERNAME}/${GH_DETECTED_REPO}.git"

  log_info "Menyiapkan git remote 'db-backup' di ${APP_DIR}..."
  git config --global --add safe.directory "${APP_DIR}" || true
  if [[ ! -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" init -q
  fi
  if git -C "${APP_DIR}" remote get-url db-backup &>/dev/null; then
    git -C "${APP_DIR}" remote set-url db-backup "${GH_REMOTE_URL}"
  else
    git -C "${APP_DIR}" remote add db-backup "${GH_REMOTE_URL}"
  fi

  log_info "Mengecek apakah ada ${DB_FILE_NAME} lama di remote backup untuk di-restore..."
  TMP_RESTORE="$(mktemp -d)"
  if git clone -q --depth 1 "${GH_REMOTE_URL}" "${TMP_RESTORE}" 2>/dev/null; then
    if [[ -f "${TMP_RESTORE}/${DB_FILE_NAME}" ]]; then
      cp -f "${TMP_RESTORE}/${DB_FILE_NAME}" "${APP_DIR}/${DB_FILE_NAME}"
      log_success "Database lama ditemukan di GitHub, berhasil di-restore ke ${APP_DIR}/${DB_FILE_NAME}."
    else
      log_warn "Repo backup ada tapi belum ada ${DB_FILE_NAME} di dalamnya (kemungkinan install pertama kali)."
    fi
  else
    log_warn "Belum bisa clone repo backup (repo kosong/baru). Backup pertama akan dibuat otomatis oleh bot."
  fi
  rm -rf "${TMP_RESTORE}"

  GH_BACKUP_ENABLED="true"

  # Tulis/perbarui baris GH_BACKUP_* di .env jika file sudah ada (mode Update);
  # untuk fresh install, baris ini juga ditulis ulang oleh blok cat > .env utama.
  if [[ -f "${ENV_FILE}" ]]; then
    umask 077
    sed -i '/^GH_BACKUP_ENABLED=/d;/^GH_BACKUP_PAT=/d;/^GH_BACKUP_REPO=/d' "${ENV_FILE}"
    {
      echo "GH_BACKUP_ENABLED=true"
      echo "GH_BACKUP_PAT=${GH_PAT_INPUT}"
      echo "GH_BACKUP_REPO=${GH_USERNAME}/${GH_DETECTED_REPO}"
    } >> "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  fi
  echo ""
}

setup_systemd_service() {
  log_info "Membuat/memperbarui systemd service '${SERVICE_NAME}'..."

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
Environment="OLLAMA_MODEL_LIGHT=${OLLAMA_MODEL_LIGHT}"
Environment="OLLAMA_MODEL_MEDIUM=${OLLAMA_MODEL_MEDIUM}"
Environment="OLLAMA_MODEL_HEAVY=${OLLAMA_MODEL_HEAVY}"
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
  echo -e "${GREEN}${BOLD}   SELESAI - PRIVATE AI TELEGRAM BOT SIAP DIGUNAKAN${NC}"
  echo -e "${GREEN}${BOLD}=================================================================${NC}"
  echo ""
  echo -e "${BLUE}${BOLD}1. CARA MENGAKSES BOT${NC}"
  echo -e "   Buka Telegram, cari bot kamu, lalu kirim ${GREEN}/start${NC}."
  echo -e "   Bot berjalan mode POLLING — tidak butuh domain atau SSL."
  echo ""
  echo -e "${BLUE}${BOLD}   SISTEM 3 TIER MODEL (/model)${NC}"
  echo -e "   🟢 Light  : ${GREEN}${OLLAMA_MODEL_LIGHT}${NC}  (kuota token x1, super irit CPU)"
  echo -e "   🟡 Medium : ${GREEN}${OLLAMA_MODEL_MEDIUM}${NC}    (kuota token x2, default)"
  echo -e "   🔴 Heavy  : ${GREEN}${OLLAMA_MODEL_HEAVY}${NC}   (kuota token x3, reasoning maksimal)"
  echo -e "   Vision    : ${GREEN}${OLLAMA_VISION_MODEL}${NC} (gambar & video, di luar sistem tier)"
  echo -e "   User memilih tier lewat perintah ${YELLOW}/model${NC} di Telegram."
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
    echo -e "${BLUE}${BOLD}   BACKUP OTOMATIS DATABASE KE GITHUB${NC}"
    echo -e "   Status   : ${GREEN}AKTIF${NC} (auto-push tiap 60 detik jika ada perubahan)"
    echo -e "   Repo     : ${GREEN}https://github.com/${GH_REPO_DISPLAY}${NC}"
    echo ""
  fi
  echo -e "${BLUE}${BOLD}8. CARA UPDATE DI MASA DEPAN${NC}"
  echo -e "   Jalankan ulang installer ini dan pilih menu ${GREEN}[2] Update${NC}."
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
    # Pastikan remote 'origin' (source code bot) selalu ada & benar, terlepas dari
    # remote 'db-backup' yang mungkin sudah ditambahkan oleh github_auto_setup sebelumnya.
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
  install_ollama_if_needed
  pull_all_models

  # SETUP USER SISTEM UNTUK MENJALANKAN SERVICE (least privilege)
  if id "${SERVICE_USER}" &>/dev/null; then
    log_warn "User sistem '${SERVICE_USER}' sudah ada."
  else
    log_info "Membuat user sistem '${SERVICE_USER}' untuk menjalankan service..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi

  # CLONE REPOSITORY (bot.py, ai_engine.py, database.py)
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
GH_BACKUP_REPO=${GH_USERNAME:-}/${GH_DETECTED_REPO:-}
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
# EKSEKUSI SESUAI PILIHAN MENU
# =============================================================================
if [[ "${INSTALL_MODE}" == "1" ]]; then
  run_fresh_install_flow
else
  run_update_flow
fi
