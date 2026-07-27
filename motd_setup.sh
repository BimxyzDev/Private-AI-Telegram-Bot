#!/usr/bin/env bash

# motd_setup.sh - Setup SSH Welcome Banner untuk Private AI Telegram Bot
# =========================================================================
# Memasang script banner ke /etc/profile.d/ (BUKAN ke .bashrc pengguna
# tertentu) sehingga tampil otomatis untuk SEMUA user yang login lewat SSH
# ke server ini (root maupun user biasa), tanpa perlu mengedit .bashrc
# masing-masing satu-per-satu. Banner menampilkan:
#   - Status service Bot (ai-bot), Dashboard (ai-bot-dashboard), Worker
#     Agent (ai-worker-agent), dan Ollama -- HANYA yang benar-benar
#     ter-install/terdaftar sebagai service systemd di server ini.
#   - Load CPU (1/5/15 menit) & jumlah core.
#   - Pemakaian RAM (terpakai/total + persentase).
#   - Pemakaian disk di /.
#
# CARA PASANG:
#   sudo bash motd_setup.sh
#
# CARA LEPAS (kembalikan SSH banner ke default Ubuntu):
#   sudo rm /etc/profile.d/99-ai-bot-banner.sh
#   sudo rm -f /etc/update-motd.d/99-ai-bot-status   # jika dipakai varian dynamic-motd
#
# Aman dijalankan berulang kali (idempotent) -- menimpa file lama dengan versi baru.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Script ini harus dijalankan sebagai root atau menggunakan sudo."
  echo "Coba jalankan ulang dengan: sudo bash motd_setup.sh"
  exit 1
fi

BANNER_SCRIPT="/etc/profile.d/99-ai-bot-banner.sh"

echo "Memasang SSH Welcome Banner ke ${BANNER_SCRIPT}..."

cat > "${BANNER_SCRIPT}" <<'BANNER_EOF'
#!/usr/bin/env bash
# Auto-generated oleh motd_setup.sh (Private AI Telegram Bot).
# Tampil di setiap login SSH baru (interactive shell) untuk semua user.
# Jangan edit manual -- jalankan ulang motd_setup.sh jika ingin mengubah isi.

# Hanya tampilkan di sesi interaktif (hindari mengganggu script non-interaktif
# seperti scp, rsync, atau cron yang kebetulan memuat /etc/profile.d/*).
case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

_AIBOT_GREEN='\033[0;32m'
_AIBOT_RED='\033[0;31m'
_AIBOT_YELLOW='\033[1;33m'
_AIBOT_BLUE='\033[0;34m'
_AIBOT_BOLD='\033[1m'
_AIBOT_NC='\033[0m'

_aibot_service_status() {
  # Cetak label berwarna untuk satu service systemd, HANYA jika unit file-nya
  # benar-benar ada (server single-server/worker tidak punya semua service).
  local name="$1" label="$2"
  if systemctl list-unit-files 2>/dev/null | grep -q "^${name}.service"; then
    if systemctl is-active --quiet "${name}" 2>/dev/null; then
      echo -e "  ${label} : ${_AIBOT_GREEN}● Aktif${_AIBOT_NC}"
    else
      echo -e "  ${label} : ${_AIBOT_RED}● Berhenti${_AIBOT_NC} (cek: journalctl -u ${name} -n 30)"
    fi
  fi
}

echo ""
echo -e "${_AIBOT_BOLD}=================================================================${_AIBOT_NC}"
echo -e "${_AIBOT_BOLD}   🤖 Private AI Telegram Bot — Status Server${_AIBOT_NC}"
echo -e "${_AIBOT_BOLD}=================================================================${_AIBOT_NC}"

_aibot_service_status "ai-bot" "Telegram Bot     "
_aibot_service_status "ai-bot-dashboard" "Web Dashboard    "
_aibot_service_status "ai-worker-agent" "Worker Agent     "
_aibot_service_status "ollama" "Ollama           "

echo -e "${_AIBOT_BOLD}-----------------------------------------------------------------${_AIBOT_NC}"

# --- CPU Load ---
if [[ -f /proc/loadavg ]]; then
  _aibot_load="$(cut -d' ' -f1-3 /proc/loadavg)"
  _aibot_cores="$(nproc 2>/dev/null || echo '?')"
  echo -e "  ${_AIBOT_BLUE}CPU Load${_AIBOT_NC} (1/5/15 menit) : ${_aibot_load}  (${_aibot_cores} core)"
fi

# --- RAM ---
if command -v free &>/dev/null; then
  read -r _aibot_mem_used _aibot_mem_total <<< "$(free -m | awk '/^Mem:/ {print $3, $2}')"
  if [[ -n "${_aibot_mem_total:-}" && "${_aibot_mem_total}" -gt 0 ]]; then
    _aibot_mem_pct=$(( _aibot_mem_used * 100 / _aibot_mem_total ))
    _aibot_mem_color="${_AIBOT_GREEN}"
    [[ "${_aibot_mem_pct}" -ge 70 ]] && _aibot_mem_color="${_AIBOT_YELLOW}"
    [[ "${_aibot_mem_pct}" -ge 90 ]] && _aibot_mem_color="${_AIBOT_RED}"
    echo -e "  ${_AIBOT_BLUE}RAM${_AIBOT_NC}                     : ${_aibot_mem_color}${_aibot_mem_used}MB / ${_aibot_mem_total}MB (${_aibot_mem_pct}%)${_AIBOT_NC}"
  fi
fi

# --- Disk / ---
if command -v df &>/dev/null; then
  _aibot_disk_pct="$(df -h / | awk 'NR==2 {print $5}')"
  _aibot_disk_used="$(df -h / | awk 'NR==2 {print $3}')"
  _aibot_disk_total="$(df -h / | awk 'NR==2 {print $2}')"
  echo -e "  ${_AIBOT_BLUE}Disk (/)${_AIBOT_NC}                : ${_aibot_disk_used} / ${_aibot_disk_total} (${_aibot_disk_pct})"
fi

echo -e "${_AIBOT_BOLD}=================================================================${_AIBOT_NC}"
echo -e "  Log Bot        : ${_AIBOT_YELLOW}journalctl -u ai-bot -f${_AIBOT_NC}"
echo -e "  Log Dashboard  : ${_AIBOT_YELLOW}journalctl -u ai-bot-dashboard -f${_AIBOT_NC}"
echo -e "  Restart Bot    : ${_AIBOT_YELLOW}systemctl restart ai-bot${_AIBOT_NC}"
echo -e "${_AIBOT_BOLD}=================================================================${_AIBOT_NC}"
echo ""

unset -f _aibot_service_status
unset _AIBOT_GREEN _AIBOT_RED _AIBOT_YELLOW _AIBOT_BLUE _AIBOT_BOLD _AIBOT_NC
unset _aibot_load _aibot_cores _aibot_mem_used _aibot_mem_total _aibot_mem_pct _aibot_mem_color
unset _aibot_disk_pct _aibot_disk_used _aibot_disk_total
BANNER_EOF

chmod +x "${BANNER_SCRIPT}"

echo "Selesai. Banner akan tampil pada sesi SSH interaktif berikutnya (login ulang untuk melihat)."
echo "Untuk melihat sekarang tanpa logout: source ${BANNER_SCRIPT}"
