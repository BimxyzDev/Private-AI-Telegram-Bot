#!/usr/bin/env bash

# update.sh - Smart Updater untuk Enterprise Private AI Telegram Bot (Single-Server, Master, & Worker Node)
# =================================================================================================
# Berbeda dari `install.sh` (yang punya mode [2] Update tapi HARUS dijalankan lewat menu
# interaktif dan mengasumsikan APP_DIR="/opt/ai-bot"), script ini adalah updater MANDIRI
# yang bisa dijalankan kapan saja tanpa menu, otomatis MENDETEKSI jenis instalasi yang
# ada di server (Single-Server / Master Node / Worker Node) lewat cek direktori, lalu
# melakukan `git pull` HANYA pada source code (termasuk folder web/ untuk Dashboard),
# sambil menjamin file-file berikut TIDAK PERNAH tertimpa atau terhapus:
#   - .env                     (token bot, ID owner, kredensial dashboard, API key worker, dll)
#   - bot_data.db*              (database SQLite + WAL/SHM, riwayat chat, user, worker registry)
#   - *.pem, *.crt, *.key, ssl/  (sertifikat SSL jika ada, mis. untuk webhook custom)
#
# Setelah source code diperbarui, updater ini JUGA otomatis:
#   - Menjalankan migrasi skema database (kolom hardware/queue baru -- lihat database.py)
#   - Menarik model Ollama baru yang mungkin ditambahkan di rilis ini (Auto Model Pull)
#   - Merestart HANYA service yang benar-benar ada di server ini
#
# CARA PAKAI:
#   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/update.sh | sudo bash
#   -- atau, jika sudah ada salinan lokal --
#   sudo bash update.sh
#
# Aman dijalankan berulang kali (idempotent). User TIDAK perlu mengisi ulang konfigurasi
# apa pun yang sudah ada -- script ini murni menyegarkan source code + dependency +
# restart service, konfigurasi (.env) dan data (database) sama sekali tidak disentuh.

set -euo pipefail

REPO_GIT_URL="https://github.com/BimxyzDev/Private-AI-Telegram-Bot.git"   # <-- HARUS SAMA dengan install.sh
REPO_BRANCH="main"

MASTER_APP_DIR="/opt/ai-bot"
WORKER_APP_DIR="/opt/ai-worker"

MASTER_SERVICE_NAME="ai-bot"
DASHBOARD_SERVICE_NAME="ai-bot-dashboard"
WORKER_SERVICE_NAME="ai-worker-agent"

# Pola file/folder yang WAJIB dipertahankan (backup sebelum pull, restore sesudahnya).
# Dicocokkan relatif terhadap root APP_DIR yang sedang diupdate.
PRESERVE_PATTERNS=(
  ".env"
  "bot_data.db"
  "bot_data.db-wal"
  "bot_data.db-shm"
  "bot_data.db.gz"
  "*.pem"
  "*.crt"
  "*.key"
  "ssl"
  "certs"
)

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

if [[ "${EUID}" -ne 0 ]]; then
  log_error "Script ini harus dijalankan sebagai root atau menggunakan sudo."
  echo "Coba jalankan ulang dengan: curl -sL <url> | sudo bash"
  exit 1
fi

echo -e "${BOLD}=============================================${NC}"
echo -e "${BOLD}  Private AI Telegram Bot - Smart Updater${NC}"
echo -e "${BOLD}=============================================${NC}"
echo ""

# -----------------------------------------------------------------------------
# BACKUP FILE PENTING SEBELUM GIT PULL/RESET
# -----------------------------------------------------------------------------
# Alasan dibackup ke direktori sementara TERPISAH (bukan hanya mengandalkan
# .gitignore/.git/info/exclude): `git reset --hard` bisa menghapus file
# untracked yang KEBETULAN memenuhi pola .gitignore repo upstream jika repo
# upstream tidak konsisten menjaga pola itu di semua rilis. Backup eksplisit
# ke luar APP_DIR adalah lapisan pengaman kedua yang tidak bergantung sama
# sekali pada isi .gitignore milik repo manapun.
backup_preserved_files() {
  local app_dir="$1"
  local backup_dir="$2"

  mkdir -p "${backup_dir}"
  local found_any=0
  for pattern in "${PRESERVE_PATTERNS[@]}"; do
    # shellcheck disable=SC2086 -- pattern sengaja tidak dikutip agar glob bekerja
    for match in "${app_dir}"/${pattern}; do
      if [[ -e "${match}" ]]; then
        cp -a "${match}" "${backup_dir}/" 2>/dev/null || true
        found_any=1
      fi
    done
  done
  if [[ "${found_any}" -eq 1 ]]; then
    log_info "Backup sementara file penting (${app_dir}) -> ${backup_dir}"
  fi
}

restore_preserved_files() {
  local app_dir="$1"
  local backup_dir="$2"

  if [[ ! -d "${backup_dir}" ]]; then
    return
  fi
  # Salin balik SEMUA yang dibackup, menimpa apa pun yang mungkin ikut ter-pull
  # dari repo (mis. repo upstream menyertakan .env.example dengan nama sama --
  # skenario ini seharusnya tidak terjadi, tapi restore selalu jadi prioritas
  # terakhir demi keamanan data pengguna).
  shopt -s dotglob nullglob
  for item in "${backup_dir}"/*; do
    cp -a "${item}" "${app_dir}/" 2>/dev/null || true
  done
  shopt -u dotglob nullglob
  log_success "File penting (.env, database, sertifikat) dipulihkan setelah update."
}

# -----------------------------------------------------------------------------
# UPDATE SATU DIREKTORI APLIKASI (dipakai untuk Master maupun Single-Server)
# -----------------------------------------------------------------------------
update_app_dir() {
  local app_dir="$1"
  local venv_dir="${app_dir}/venv"

  if [[ ! -d "${app_dir}" ]]; then
    return 1
  fi

  log_info "Mengupdate source code di ${app_dir}..."
  local backup_dir
  backup_dir="$(mktemp -d)"
  backup_preserved_files "${app_dir}" "${backup_dir}"

  if [[ -d "${app_dir}/.git" ]]; then
    git config --global --add safe.directory "${app_dir}" || true
    if git -C "${app_dir}" remote get-url origin &>/dev/null; then
      git -C "${app_dir}" remote set-url origin "${REPO_GIT_URL}"
    else
      git -C "${app_dir}" remote add origin "${REPO_GIT_URL}"
    fi
    git -C "${app_dir}" fetch origin
    git -C "${app_dir}" reset --hard "origin/${REPO_BRANCH}"
    git -C "${app_dir}" clean -fd -e ".env" -e "bot_data.db*" -e "*.pem" -e "*.crt" -e "*.key" -e "ssl" -e "certs" -e "venv"
    log_success "git pull berhasil di ${app_dir}."
  else
    log_warn "${app_dir} bukan git repository, meng-clone ulang lalu menimpa source code saja..."
    local tmp_clone
    tmp_clone="$(mktemp -d)"
    git clone --branch "${REPO_BRANCH}" --depth 1 "${REPO_GIT_URL}" "${tmp_clone}"
    find "${tmp_clone}" -maxdepth 1 -type f -name "*.py" -exec cp -f {} "${app_dir}/" \;
    find "${tmp_clone}" -maxdepth 1 -type f -name "*.conf" -exec cp -f {} "${app_dir}/" \;
    # Folder web/ (index.html, admin.html, style.css, dashboard.js, admin.js) untuk
    # web_app.py (Master Dashboard) -- hanya relevan di Master Node, tapi aman
    # disalin di semua jenis instalasi (tidak dipakai jika web_app.py tidak dijalankan).
    [[ -d "${tmp_clone}/web" ]] && cp -rf "${tmp_clone}/web" "${app_dir}/"
    rm -rf "${tmp_clone}"
    log_success "Source code berhasil disalin ke ${app_dir} (termasuk node_manager.py, web_app.py, worker_agent.py, folder web/ jika relevan)."
  fi

  restore_preserved_files "${app_dir}" "${backup_dir}"
  rm -rf "${backup_dir}"

  if [[ -x "${venv_dir}/bin/pip" ]]; then
    log_info "Memperbarui dependency Python di ${app_dir} (jika ada perubahan requirements)..."
    if [[ -f "${app_dir}/requirements.txt" ]]; then
      "${venv_dir}/bin/pip" install -q --upgrade -r "${app_dir}/requirements.txt" || \
        log_warn "Gagal memperbarui sebagian dependency, cek manual jika ada error import setelah restart."
    fi
  fi

  return 0
}

# -----------------------------------------------------------------------------
# MIGRASI DATABASE (aman, idempotent -- lihat database._migrate_schema)
# -----------------------------------------------------------------------------
migrate_database_in() {
  local app_dir="$1"
  local venv_dir="${app_dir}/venv"
  if [[ -x "${venv_dir}/bin/python3" && -f "${app_dir}/database.py" ]]; then
    log_info "Menjalankan migrasi skema database (idempotent, aman diulang) di ${app_dir}..."
    if "${venv_dir}/bin/python3" -c "
import sys
sys.path.insert(0, '${app_dir}')
import database as db
db.init_db()
print('OK - skema database sudah paling baru.')
"; then
      log_success "Migrasi database selesai."
    else
      log_error "Migrasi database GAGAL di ${app_dir}. Database TIDAK diubah/dihapus, cek error di atas."
      return 1
    fi
  fi
}

# -----------------------------------------------------------------------------
# RESTART SERVICE (hanya yang benar-benar ada di server ini)
# -----------------------------------------------------------------------------
restart_if_exists() {
  local service_name="$1"
  if systemctl list-unit-files | grep -q "^${service_name}.service"; then
    log_info "Merestart service ${service_name}..."
    systemctl daemon-reload
    systemctl restart "${service_name}"
    sleep 2
    if systemctl is-active --quiet "${service_name}"; then
      log_success "Service ${service_name} berjalan normal setelah update."
    else
      log_error "Service ${service_name} GAGAL berjalan setelah update. Cek: journalctl -u ${service_name} -n 50"
    fi
  fi
}

# -----------------------------------------------------------------------------
# PULL MODEL OLLAMA BARU (jika ada model baru ditambahkan di rilis ini)
# -----------------------------------------------------------------------------
# Dibaca dari .env app_dir (bukan hardcode) supaya override model milik owner
# (lewat OLLAMA_MODEL_*) tetap dihormati -- persis sama seperti cara install.sh
# menentukan model yang di-pull. Kalau .env belum punya var model baru (instalasi
# lama sebelum rilis ini), dipakai default bawaan sesuai ai_engine.py.
pull_new_models() {
  local app_dir="$1"
  local env_file="${app_dir}/.env"

  if ! command -v ollama &>/dev/null; then
    return
  fi

  # shellcheck disable=SC1090
  local super_ringan light medium heavy
  super_ringan="$(grep -oP '^OLLAMA_MODEL_GENERAL_SUPER_RINGAN=\K.*' "${env_file}" 2>/dev/null || echo "qwen2.5:1.5b")"
  heavy="$(grep -oP '^OLLAMA_MODEL_GENERAL_HEAVY=\K.*' "${env_file}" 2>/dev/null || echo "gemma2:9b")"

  local new_models=("${super_ringan}" "${heavy}")
  for model in "${new_models[@]}"; do
    log_info "Menarik model AI baru '${model}' (jika belum ada, cepat jika sudah)..."
    if ollama pull "${model}"; then
      log_success "Model '${model}' siap dipakai."
    else
      log_warn "Gagal menarik model '${model}'. Cek koneksi/disk, lalu jalankan manual: ollama pull ${model}"
    fi
  done
}

# -----------------------------------------------------------------------------
# DETEKSI JENIS INSTALASI & EKSEKUSI
# -----------------------------------------------------------------------------
UPDATED_ANY=0

if [[ -d "${MASTER_APP_DIR}" ]]; then
  echo -e "${BOLD}--- Terdeteksi instalasi di ${MASTER_APP_DIR} (Single-Server / Master Node) ---${NC}"
  update_app_dir "${MASTER_APP_DIR}"
  migrate_database_in "${MASTER_APP_DIR}" || true
  pull_new_models "${MASTER_APP_DIR}"
  chown -R "$(stat -c '%U:%G' "${MASTER_APP_DIR}")" "${MASTER_APP_DIR}" 2>/dev/null || true
  chmod 600 "${MASTER_APP_DIR}/.env" 2>/dev/null || true
  restart_if_exists "${MASTER_SERVICE_NAME}"
  restart_if_exists "${DASHBOARD_SERVICE_NAME}"
  UPDATED_ANY=1
  echo ""
fi

if [[ -d "${WORKER_APP_DIR}" ]]; then
  echo -e "${BOLD}--- Terdeteksi instalasi di ${WORKER_APP_DIR} (Worker Node) ---${NC}"
  update_app_dir "${WORKER_APP_DIR}"
  pull_new_models "${WORKER_APP_DIR}"
  chown -R "$(stat -c '%U:%G' "${WORKER_APP_DIR}")" "${WORKER_APP_DIR}" 2>/dev/null || true
  chmod 600 "${WORKER_APP_DIR}/.env" 2>/dev/null || true
  restart_if_exists "${WORKER_SERVICE_NAME}"
  UPDATED_ANY=1
  echo ""
fi

if [[ "${UPDATED_ANY}" -eq 0 ]]; then
  log_error "Tidak ditemukan instalasi di ${MASTER_APP_DIR} maupun ${WORKER_APP_DIR}."
  log_error "Jalankan install.sh terlebih dahulu sebelum memakai updater ini."
  exit 1
fi

echo -e "${GREEN}${BOLD}=================================================================${NC}"
echo -e "${GREEN}${BOLD}   UPDATE SELESAI${NC}"
echo -e "${GREEN}${BOLD}=================================================================${NC}"
echo -e "Konfigurasi (.env), database (termasuk registry Worker Node & histori antrian),"
echo -e "dan sertifikat SSL (jika ada) TIDAK diubah."
echo -e "Cek log service jika ada yang mencurigakan:"
[[ -d "${MASTER_APP_DIR}" ]] && echo -e "  ${GREEN}sudo journalctl -u ${MASTER_SERVICE_NAME} -f${NC}          (Telegram Bot)"
[[ -d "${MASTER_APP_DIR}" ]] && echo -e "  ${GREEN}sudo journalctl -u ${DASHBOARD_SERVICE_NAME} -f${NC}   (Web Dashboard, web_app.py)"
[[ -d "${WORKER_APP_DIR}" ]] && echo -e "  ${GREEN}sudo journalctl -u ${WORKER_SERVICE_NAME} -f${NC}   (Worker Agent, GPU/VRAM/Safe Mode)"
echo ""
