# Enterprise Private AI Telegram Bot

Bot Telegram AI privat (mode polling) berbasis Ollama dengan arsitektur
terdistribusi **Master-Worker Cluster**: obrolan umum & coding lewat sistem
Role + Tier model (🗣️ General Chat, 💻 Coder/IT, 🧩 Extended — 20+ model dari
CPU-only ringan sampai model raksasa wajib GPU) + analisis gambar & video
(qwen2.5vl). Dilengkapi **Smart Hardware Management** (deteksi GPU/VRAM
otomatis), **Smart Worker Selection**, **Model Fallback otomatis**, dan
**Zero-OOM Queue Protection** — dirancang agar sistem bisa berjalan tanpa
intervensi manual owner setelah instalasi selesai.

## Fitur Utama (Enterprise v2)

- **Smart GPU Detection** — tiap Worker Node mendeteksi GPU (vendor, nama,
  CUDA, VRAM total/free/used) via `nvidia-smi` saat startup, fallback ke
  `torch.cuda` jika tersedia. Hasil di-cache dan direfresh berkala.
- **Dynamic Model Availability** — model otomatis berstatus *Ready* atau
  *Locked* berdasarkan VRAM bebas saat itu (CPU-Only → maks 8B, GPU 8GB →
  hingga 14B, GPU 24GB → hingga 32B, dst).
- **Auto Model Pull & Cache** — model baru bisa ditarik langsung dari
  Telegram (`/pullmodel`) atau Admin Dashboard, dengan progress log real-time,
  cache model lokal otomatis di-refresh setelahnya.
- **Auto Unload & Safe Mode** — model yang idle >20 menit (dapat diatur)
  otomatis dilepas dari VRAM. Jika RAM/VRAM node nyaris habis, node masuk
  *Safe Mode* dan mengunci model besar sampai kondisi membaik.
- **Smart Worker Selection** — Master Node memilih Worker Node bukan cuma
  dari antrean, tapi dari kombinasi: ketersediaan model, sisa VRAM, load
  CPU, dan load RAM.
- **Model Fallback Otomatis & Transparan** — jika model yang diminta user
  penuh/locked di semua node, bot **otomatis** fallback ke model lebih kecil
  (mis. 70B → 32B → 14B → 8B) dan memberi tahu user model apa yang
  benar-benar dipakai — tidak pernah diam-diam mengganti tanpa penjelasan.
- **Smart Queue System** — antrean dibatasi per ukuran model (mis. 70B maks
  1 request, 32B maks 2, 14B maks 4, 8B tanpa batas) untuk mencegah
  Kernel Panic/OOM di Worker Node.
- **Web Dashboard bergaya Mission Control** — status cluster publik
  real-time (GPU, VRAM, CPU, RAM, task aktif, model ready/locked) + Admin
  Panel untuk CRUD Worker Node, pull/unload model, dan monitoring antrean.

## Isi Paket

**Master Node:**
- `bot.py` — Bot Telegram utama (polling, python-telegram-bot), termasuk
  command admin cluster (`/hardware`, `/worker`, `/queue`, `/pullmodel`,
  `/unload`, `/modelsync`)
- `ai_engine.py` — Logika inti: ekstraksi file, panggilan ke Ollama (chat &
  vision), Model Fallback Chain
- `database.py` — Layer SQLite: riwayat chat, user, kode redeem, registry
  cluster (hardware GPU/VRAM), log antrean
- `github_backup.py` — Backup & restore database ke GitHub lewat REST API
- `node_manager.py` — Smart Load Balancer: health-check, Smart Worker
  Selection, Model Fallback Chain (**hanya aktif di Master Node**)
- `web_app.py` — Web Dashboard API murni (FastAPI), menyajikan file statis
  dari folder `web/`

**Web Dashboard (folder `web/`, HTML/CSS/JS murni, tanpa build step):**
- `index.html`, `style.css`, `dashboard.js` — Dashboard publik (status
  cluster real-time, bertema *mission control*)
- `admin.html`, `admin.js` — Admin Panel (CRUD node, pull/unload model,
  monitoring antrean & user)

**Worker Node** (Distributed Cluster Architecture):
- `worker_agent.py` — Agent FastAPI Enterprise: Smart GPU Detection, Auto
  Pull/Unload, Safe Mode, Smart Queue Limiter

**Deployment:**
- `install.sh` — Installer utama (menu interaktif: Single-Server / Update /
  Master / Worker)
- `update.sh` — Smart Updater mandiri (git pull tanpa menimpa
  `.env`/database/SSL)
- `motd_setup.sh` — Pasang SSH Welcome Banner (status Bot/Ollama/CPU/RAM
  saat login)
- `ollama-limit.conf` — Referensi systemd drop-in (cgroups, batas 70%
  CPU/RAM untuk Ollama)

## Cara Deploy

1. Upload semua file di atas (termasuk folder `web/` apa adanya) ke repo
   GitHub Anda.
2. Edit `install.sh` **dan** `update.sh`, ganti `REPO_GIT_URL` di bagian
   atas masing-masing file dengan URL repo Anda (harus sama persis).
3. Siapkan dua hal dari Telegram sebelum instalasi:
   - **Token Bot** dari [@BotFather](https://t.me/BotFather) (`/newbot`)
   - **ID Telegram Owner** dari [@userinfobot](https://t.me/userinfobot)
4. Di VPS Ubuntu, jalankan:
   ```
   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/install.sh | sudo bash
   ```
5. Installer menampilkan menu interaktif — pilih **[1] Install Baru
   (Single-Server)** untuk 1 VPS sederhana, atau **[3]/[4]** untuk arsitektur
   cluster Master/Worker (lihat "Arsitektur Cluster" di bawah — sangat
   direkomendasikan jika Anda punya VPS dengan GPU terpisah).
6. Installer meminta Token Bot dan ID Owner, lalu otomatis menyimpannya ke
   `/opt/ai-bot/.env` (chmod 600) — TIDAK ADA di source code.
7. (Opsional) Pasang SSH Welcome Banner: `sudo bash motd_setup.sh`
8. Setelah instalasi selesai, kirim `/start` ke bot Anda di Telegram.

Bot berjalan mode **polling** — tidak butuh domain, SSL/certbot, maupun
Nginx.

## Kuota Token & Kode Redeem

- Setiap user baru otomatis mendapat **50.000 token/hari** (reset otomatis
  tiap ganti hari UTC). Token terpakai = token asli Ollama × multiplier
  tier model.
- User memilih **Role** lalu **Tier** lewat `/model` (Inline Keyboard,
  2 langkah): 🗣️ General Chat, 💻 Coder/IT, atau 🧩 Extended (katalog 20+
  model termasuk kelas ultra-heavy yang wajib GPU besar).
- Owner membuat kode redeem: `/gencode <jumlah_token|unlimited> <hari>`.
  User menukar dengan `/redeem <kode>`. Setiap kode hanya bisa dipakai
  sekali; setelah masa berlaku habis, kuota kembali ke default otomatis.

## Perintah Bot

**Semua user:**
| Command | Fungsi |
|---|---|
| `/start`, `/help` | Info & daftar perintah |
| `/status` | Sisa kuota token & model aktif hari ini |
| `/model` | Pilih Role lalu Tier model AI |
| `/redeem <kode>` | Tukar kode redeem |
| `/reset` | Hapus riwayat chat |

**Khusus Owner — Umum:**
| Command | Fungsi |
|---|---|
| `/gencode <token\|unlimited> <hari>` | Buat kode redeem baru |
| `/codes` | Kode redeem yang belum dipakai |
| `/users` | Daftar user & status kuota |
| `/ban` / `/unban <id>` | Nonaktifkan/aktifkan akses user |
| `/broadcast <pesan>` | Kirim pesan ke semua user |

**Khusus Owner — Cluster & Hardware (hanya aktif jika `CLUSTER_MODE=master`):**
| Command | Fungsi |
|---|---|
| `/hardware` | Spek GPU/VRAM/CPU/RAM semua Worker Node |
| `/worker` | Ringkasan status cluster (online/offline, model ready/locked, safe mode) |
| `/queue` | Antrean aktif per node + histori event (queued/started/completed/failed/fallback) |
| `/pullmodel <model> [node_id]` | Pull model ke satu/semua Worker Node |
| `/unload <model> <node_id>` | Unload model dari VRAM node tertentu |
| `/modelsync [node_id]` | Paksa refresh cache model dari Worker Node |

## Fitur Upload

| Jenis file | Ekstensi | Diproses dengan |
|---|---|---|
| Dokumen/code | .txt .md .csv .json .py .js .html .css dst | Ekstraksi teks langsung |
| PDF | .pdf | pypdf |
| Word | .docx | python-docx |
| Gambar | .jpg .jpeg .png .webp .bmp .gif | qwen2.5vl (vision) |
| Video | .mp4 .mov .mkv .avi .webm .m4v | ffmpeg (4 frame) + qwen2.5vl |
| ZIP | .zip | Diekstrak, tiap file diproses sesuai jenisnya (maks 20 file, 200MB) |

Batas upload: **50 MB**. Vision TIDAK memakai Model Fallback Chain (fallback
ke model non-vision tidak relevan secara semantik).

## Proteksi Anti-Jailbreak

1. **Pengecekan pola lokal** — pola jailbreak umum terdeteksi sebelum
   request dikirim ke Ollama, bot langsung menolak tanpa memotong kuota.
2. **System prompt yang diperkuat** — instruksi keamanan tetap yang tidak
   bisa dioverride lewat env var maupun pesan user manapun.

## Arsitektur Cluster (Master-Worker)

- **Master Node** — Telegram Bot, Smart Load Balancer (`node_manager.py`),
  Web Dashboard (`web_app.py`), database SQLite. Secara default **tidak**
  menjalankan Ollama sendiri (opsional: Ollama Fallback, lihat di bawah).
- **Worker Node** — VPS terpisah menjalankan Ollama + `worker_agent.py`
  Enterprise (Smart GPU Detection, Auto Pull/Unload, Safe Mode) di port
  `3716`. Didaftarkan ke Master lewat Admin Dashboard, online otomatis
  dalam ~7 detik tanpa restart Bot.
- **Smart Worker Selection** — request dirutekan ke node dengan skor
  terbaik: model *ready* (bukan locked), slot antrean belum penuh, lalu
  diurutkan dari active_tasks terendah → CPU terendah → RAM terendah →
  VRAM bebas terbanyak.
- **Model Fallback Chain** — jika model yang diminta locked/penuh di
  seluruh cluster, sistem mencari model sefamili yang lebih kecil dulu
  (mis. `qwen2.5-coder:32b` → `qwen2.5-coder:14b`), lalu fallback
  lintas-keluarga jika perlu. User selalu diberi tahu jika terjadi
  fallback.
- **Failover antar-node** — jika node yang dipilih gagal/timeout di
  tengah proses, request otomatis dicoba ke node kandidat berikutnya.
- **Ollama Fallback di Master Node (opsional)** — saat instal Master Node,
  ada opsi menjalankan Ollama lokal di Master sebagai cadangan terakhir,
  dipakai HANYA jika semua Worker Node offline. Nonaktif secara default,
  otomatis dibatasi 70% CPU/RAM jika diaktifkan.

**Cara deploy cluster:**
1. `install.sh` di VPS pertama → **[3] Install MASTER NODE**.
2. `install.sh` di tiap VPS Worker (idealnya dengan GPU) → **[4] Install
   WORKER NODE** — installer menampilkan API Key unik & ringkasan status
   GPU/CPU-only node tsb.
3. Buka `http://<ip-master>:8080/admin` → tambahkan tiap Worker Node.
4. Status cluster publik: `http://<ip-master>:8080/` (tanpa login, IP
   Worker disamarkan).

## Web Dashboard

- **Publik** (`/`) — status real-time semua node: LED status
  (online/offline/error), bar VRAM/CPU/RAM bergaya hardware, jumlah model
  ready/locked, task aktif, latency. Refresh otomatis tiap 5 detik.
- **Admin** (`/admin`, HTTP Basic Auth) — tambah/hapus/toggle Worker Node,
  trigger pull/unload model langsung dari browser, monitoring antrean +
  histori event, daftar user bot.
- Dibangun murni HTML/CSS/JS (Tailwind-free, tanpa build step) agar mudah
  di-audit dan di-modifikasi.

## Update & Maintenance Server

```
curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/update.sh | sudo bash
```
Auto-detect instalasi (Single-Server/Master di `/opt/ai-bot`, Worker di
`/opt/ai-worker`), git pull (termasuk folder `web/`), migrasi database
(idempotent, termasuk kolom hardware/queue baru), pull model baru, restart
service — **tanpa** menyentuh `.env`/database/SSL.

## Backup Database ke GitHub (Opsional)

Database di-backup otomatis tiap 60 detik (jika berubah) lewat GitHub REST
API — bukan `git`, jadi update source code tidak akan pernah menyentuh
database. Setup: masukkan GitHub PAT (fine-grained, *Contents: Read and
write*) dan repo tujuan terpisah saat instalasi/update.

## Keamanan

- Token Bot & ID Owner wajib via env var, bot menolak start jika tidak diset.
- Rahasia di `.env` (chmod 600), dimuat lewat systemd `EnvironmentFile=`.
- Bot & Worker Agent berjalan sebagai user sistem non-root dengan systemd
  hardening (`ProtectSystem=strict`, `NoNewPrivileges=true`).
- Hanya `OWNER_TELEGRAM_ID` bisa memakai command admin.
- Master ↔ Worker Node diautentikasi via header `X-API-KEY` unik per node.
- Admin Dashboard dilindungi HTTP Basic Auth; API Key Worker Node tidak
  pernah ditampilkan penuh di UI (hanya 4 karakter terakhir).

## Mengganti Token Bot / ID Owner
```
sudo nano /opt/ai-bot/.env
sudo systemctl restart ai-bot
```

## Melihat Log
```
sudo journalctl -u ai-bot -f
sudo journalctl -u ai-bot-dashboard -f   # Master: Web Dashboard
sudo journalctl -u ai-worker-agent -f    # Worker: Hardware/GPU/Safe Mode
```

## Restart Service
```
sudo systemctl restart ai-bot
sudo systemctl restart ai-bot-dashboard
sudo systemctl restart ai-worker-agent
sudo systemctl restart ollama
```
