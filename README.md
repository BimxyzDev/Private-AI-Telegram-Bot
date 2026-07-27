# Private AI Telegram Bot

Bot Telegram AI privat (mode polling): obrolan umum & coding lewat sistem Role +
Tier model (🗣️ General Chat: llama3.2:3b/llama3.1:8b, 💻 Coder/IT: qwen2.5-coder
1.5b/7b/14b) + analisis gambar & video (qwen2.5vl). Setiap user mendapat kuota
token harian, dengan sistem kode redeem untuk menaikkan kuota (termasuk unlimited)
hingga masa berlaku tertentu. Dilengkapi proteksi anti-jailbreak bawaan untuk
menjaga model tetap pada persona & aturannya.

## Isi Paket

**Single-Server / Master Node:**
- `bot.py`            — Bot Telegram utama (polling, python-telegram-bot)
- `ai_engine.py`      — Logika inti: ekstraksi file, panggilan ke Ollama (chat & vision)
- `database.py`       — Layer SQLite: riwayat chat, data user, limit, kode redeem, registry cluster
- `github_backup.py`  — Backup & restore database ke GitHub lewat REST API (lihat bagian di bawah)
- `master_dashboard.py` — Web Dashboard (FastAPI): status publik cluster + Admin Panel CRUD Worker Node
- `node_manager.py`   — Load Balancer & health-check Worker Node (hanya aktif di Master Node)

**Worker Node** (Distributed Cluster Architecture, opsional):
- `worker_agent.py`   — Agent FastAPI ringan yang menjembatani Master ↔ Ollama lokal di tiap Worker

**Deployment:**
- `install.sh`        — Installer utama (menu interaktif: Single-Server / Update / Master / Worker)
- `update.sh`         — Smart Updater mandiri (git pull tanpa menimpa `.env`/database/SSL)
- `motd_setup.sh`      — Pasang SSH Welcome Banner (status Bot/Ollama/CPU/RAM saat login)
- `ollama-limit.conf` — Referensi systemd drop-in (cgroups, batas 70% CPU/RAM untuk Ollama)

## Cara Deploy
1. Upload semua file di atas ke repo GitHub Anda (ini repo *source code*, terpisah
   dari repo backup database di bagian "Backup Database ke GitHub" di bawah).
2. Edit `install.sh` **dan** `update.sh`, ganti `REPO_GIT_URL` di bagian atas
   masing-masing file dengan URL repo Anda (harus sama persis di keduanya).
3. Siapkan dua hal dari Telegram sebelum instalasi:
   - **Token Bot** dari [@BotFather](https://t.me/BotFather) (perintah `/newbot`)
   - **ID Telegram Owner** dari [@userinfobot](https://t.me/userinfobot) (kirim pesan apa
     saja, ID kamu akan ditampilkan)
4. Di VPS Ubuntu, jalankan:
   ```
   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/install.sh | sudo bash
   ```
5. Installer menampilkan menu interaktif — pilih **[1] Install Baru (Single-Server)**
   untuk 1 VPS yang menjalankan Ollama & Bot sekaligus (paling sederhana), atau **[3]/[4]**
   jika ingin arsitektur cluster Master/Worker (lihat bagian "Arsitektur Cluster" di bawah).
6. Installer akan meminta Token Bot dan ID Owner di tengah proses instalasi, lalu
   otomatis menyimpannya ke `/opt/ai-bot/.env` (chmod 600) — TIDAK ADA di source code.
7. (Opsional) Pasang SSH Welcome Banner agar status Bot/Ollama/CPU/RAM tampil
   otomatis saat login SSH ke server:
   ```
   sudo bash motd_setup.sh
   ```
8. Setelah instalasi selesai, buka Telegram dan kirim `/start` ke bot Anda — daftar
   perintah juga otomatis muncul di menu "/" Telegram (kiri bawah kotak chat).

Bot berjalan mode **polling**, jadi tidak butuh domain, SSL/certbot, maupun Nginx —
cukup koneksi internet keluar dari VPS ke server Telegram.

## Kuota Token & Kode Redeem

- Setiap user baru otomatis mendapat **50.000 token/hari** (reset otomatis tiap
  ganti hari UTC). Token terpakai dihitung dari jumlah token asli Ollama
  (prompt + hasil jawaban) dikali multiplier tier model yang dipakai.
- User memilih **Role** lalu **Tier** model AI lewat `/model` (2 langkah, Inline
  Keyboard Telegram):
  - 🗣️ **General Chat** — untuk obrolan santai, tanya umum, curhat, dll:
    - 🟢 Light (`llama3.2:3b`) — kuota token x1, paling irit
    - 🟡 Medium (`llama3.1:8b`) — kuota token x2, **default user baru**
  - 💻 **Coder / IT** — untuk ngoding, debugging, pertanyaan teknis:
    - 🟢 Light (`qwen2.5-coder:1.5b`) — kuota token x1, paling irit
    - 🟡 Medium (`qwen2.5-coder:7b`) — kuota token x2
    - 🔴 Heavy (`qwen2.5-coder:14b`) — kuota token x3, reasoning maksimal
  - User bisa ganti role/tier kapan saja lewat `/model`, tanpa kehilangan riwayat
    chat maupun kuota token yang tersisa.
- Owner bisa membuat kode redeem lewat command di Telegram:
  ```
  /gencode <jumlah_token> <hari>   contoh: /gencode 100000 30
  /gencode unlimited <hari>        contoh: /gencode unlimited 365
  ```
- User menukar kode dengan:
  ```
  /redeem <kode>
  ```
- Setiap kode hanya bisa dipakai **satu kali** oleh satu user.
- Setelah masa berlaku (hari) habis, kuota user otomatis kembali ke default
  50.000 token/hari — ini dicek otomatis setiap kali user mengirim pesan,
  tidak perlu campur tangan owner.

## Perintah Bot

**Semua user:**
| Command | Fungsi |
|---|---|
| `/start`, `/help` | Info & daftar perintah |
| `/status` | Lihat sisa kuota token hari ini & role+tier model aktif |
| `/model` | Pilih Role (General Chat/Coder-IT) lalu Tier model AI |
| `/redeem <kode>` | Tukar kode redeem untuk menaikkan kuota token |
| `/reset` | Hapus riwayat chat (mulai percakapan baru) |

**Khusus Owner** (berdasarkan `OWNER_TELEGRAM_ID`):
| Command | Fungsi |
|---|---|
| `/gencode <jumlah_token\|unlimited> <hari>` | Buat kode redeem baru |
| `/codes` | Lihat kode redeem yang belum dipakai |
| `/users` | Lihat daftar user & status kuota mereka |
| `/ban <id>` | Nonaktifkan akses user |
| `/unban <id>` | Aktifkan kembali akses user |
| `/broadcast <pesan>` | Kirim pesan ke semua user terdaftar |

Semua perintah di atas otomatis terdaftar ke menu **"/"** Telegram (kiri bawah kotak
chat) saat bot startup — user cukup ketik `/` untuk melihat daftar lengkap tanpa
perlu mengetik manual. Menu owner (termasuk command admin) hanya muncul khusus di
chat pribadi `OWNER_TELEGRAM_ID` dengan bot, tidak terlihat oleh user lain.

**Feedback visual saat AI berpikir:** untuk chat maupun upload file, bot mengirim
satu pesan status yang diperbarui secara berkala ("🧠 Membaca konteks..." → "🔎
Menyusun jawaban..." → dst. mengikuti durasi proses), lalu otomatis terhapus begitu
jawaban asli terkirim — memberi kepastian visual bahwa bot masih bekerja, terutama
untuk model besar/berkas panjang yang bisa memakan waktu beberapa menit.

## Fitur Upload

Kirim file langsung ke bot (sebagai dokumen, foto, atau video Telegram):

| Jenis file      | Ekstensi                                   | Diproses dengan          |
|-----------------|---------------------------------------------|---------------------------|
| Dokumen/code    | .txt .md .csv .json .py .js .html .css dst | Ekstraksi teks langsung   |
| PDF             | .pdf                                        | pypdf                     |
| Word            | .docx                                       | python-docx               |
| Gambar          | .jpg .jpeg .png .webp .bmp .gif            | qwen2.5vl (vision)        |
| Video           | .mp4 .mov .mkv .avi .webm .m4v             | ffmpeg (sampling 4 frame) + qwen2.5vl |
| ZIP             | .zip                                        | Diekstrak, tiap file di dalam diproses otomatis sesuai jenisnya (maks 20 file, maks 200MB hasil ekstrak) |

Batas upload per file: **50 MB**. Setiap file yang diproses tetap dihitung sebagai
pemakaian kuota token harian (sesuai jumlah token hasil pemrosesan x multiplier tier model).

## Proteksi Anti-Jailbreak

Model kecil (1.5b–8b) yang dipakai di tier Light/Medium relatif lebih mudah
"dibujuk" keluar dari aturan/persona-nya dibanding model besar. Bot ini punya
2 lapis proteksi:

1. **Pengecekan pola lokal** (di kode Python, sebelum request dikirim ke Ollama) —
   mendeteksi pola umum jailbreak (misal "ignore all previous instructions",
   "abaikan instruksi sebelumnya", "developer mode activated", dll). Jika
   terdeteksi, bot langsung membalas penolakan tanpa memanggil model sama
   sekali — **tidak memotong kuota token user**.
2. **System prompt yang diperkuat** — setiap request ke Ollama selalu menyertakan
   instruksi keamanan tetap yang tidak bisa dioverride lewat env var maupun
   pesan dari user manapun (termasuk yang mengaku developer/owner/admin).

Kedua lapis ini berlaku untuk **kedua role** (General Chat maupun Coder/IT).

## Migrasi dari Versi Sebelumnya (3-Tier Coder-Only)

Kalau kamu sudah punya bot ini berjalan dari versi sebelum sistem Role (yang
cuma punya tier Light/Medium/Heavy berbasis qwen2.5-coder saja), migrasi
database berjalan **otomatis dan aman**:

- Kolom baru `model_role` ditambahkan lewat `ALTER TABLE`, database & riwayat
  chat lama **tidak dihapus**.
- User yang sudah ada di-backfill ke `model_role = 'coder'` secara otomatis,
  sehingga **model yang mereka pakai tetap sama persis** seperti sebelum update
  (tidak ada yang tiba-tiba pindah ke model General tanpa sepengetahuan mereka).
- Hanya user **baru** (belum pernah chat sebelumnya) yang mendapat default baru
  (General Chat, tier Medium → `llama3.1:8b`).
- Cukup jalankan ulang `install.sh` (mode Update) di server yang sudah ada,
  migrasi kolom akan otomatis dijalankan sebelum service di-restart.

## Arsitektur Cluster (Master-Worker, Opsional)

Selain mode **Single-Server** (1 VPS menjalankan semuanya, cara paling sederhana),
bot ini juga mendukung **Distributed Cluster Architecture** untuk menyebar beban
inferensi AI ke beberapa VPS sekaligus:

- **Master Node** — menjalankan Telegram Bot, Load Balancer (`node_manager.py`),
  Web Dashboard (`master_dashboard.py`), dan database SQLite. Secara default
  **tidak** menjalankan Ollama sendiri.
- **Worker Node** — VPS terpisah yang HANYA menjalankan Ollama + `worker_agent.py`
  (agent ringan di port `3716`). Bisa didaftarkan sebanyak apa pun ke Master lewat
  Admin Dashboard, tanpa perlu restart Bot Telegram.
- **Load Balancing "Least Loaded"** — tiap request AI dirutekan ke Worker Node
  dengan `active_tasks` paling sedikit (tie-break: CPU lalu RAM lebih rendah),
  dengan failover otomatis ke Worker Node berikutnya jika node yang dipilih gagal.
- **Ollama Fallback di Master Node (opsional)** — saat memilih **[3] Install
  Master Node** di `install.sh`, ada opsi tambahan untuk JUGA menjalankan Ollama
  lokal di Master sebagai cadangan terakhir, dipakai HANYA jika **semua** Worker
  Node di cluster sedang offline/tidak tersedia. Nonaktif secara default. Jika
  diaktifkan, Ollama otomatis dibatasi maksimal **70% CPU & 70% RAM** (lihat
  `ollama-limit.conf`) supaya Bot & Dashboard di server yang sama tetap stabil.

**Cara deploy cluster:**
1. Jalankan `install.sh` di VPS pertama, pilih **[3] Install MASTER NODE**.
2. Jalankan `install.sh` di tiap VPS Worker, pilih **[4] Install WORKER NODE**
   — installer akan menampilkan API Key unik untuk node tsb.
3. Buka `http://<ip-master>:8080/admin` (kredensial sesuai yang diisi saat
   instalasi Master), tambahkan tiap Worker Node (nama, IP, port, API Key).
4. Status cluster publik bisa dilihat di `http://<ip-master>:8080/` (tanpa login,
   IP Worker Node disamarkan demi keamanan).

## Update & Maintenance Server

**Update source code** (aman, tidak menyentuh `.env`/database/sertifikat SSL):
```
curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/update.sh | sudo bash
```
Script ini otomatis mendeteksi instalasi yang ada (Single-Server/Master di
`/opt/ai-bot`, Worker di `/opt/ai-worker`), menjalankan `git pull`, migrasi
database (idempotent), lalu merestart service yang relevan — tanpa perlu mengisi
ulang Token Bot, ID Owner, atau konfigurasi lain yang sudah ada.

**SSH Welcome Banner** — menampilkan status Bot/Dashboard/Worker Agent/Ollama
serta CPU/RAM/Disk setiap kali login SSH ke server:
```
sudo bash motd_setup.sh
```
Dipasang ke `/etc/profile.d/` (berlaku untuk semua user, bukan hanya satu akun).
Lepas dengan `sudo rm /etc/profile.d/99-ai-bot-banner.sh`.

**Batas resource Ollama** (`ollama-limit.conf`) — di mode Single-Server, batas
70% CPU & 70% RAM untuk Ollama diterapkan **otomatis** oleh `install.sh` (dihitung
sesuai jumlah core & RAM fisik server saat itu). Untuk mengecek batas yang aktif:
```
systemctl show ollama --property=CPUQuotaPerSecUSec,MemoryMax,MemoryHigh
```

## Backup Database ke GitHub (Opsional)

Database (`bot_data.db`) bisa di-backup otomatis ke repo GitHub terpisah, supaya
riwayat chat & user tetap aman walau server hilang/di-reset.

- Cara kerja: tiap 60 detik bot mengecek apakah database berubah, lalu meng-gzip
  dan meng-upload-nya lewat **GitHub REST API** (`github_backup.py`) — **bukan**
  lewat perintah `git`. Ini sengaja dipisah total dari repo source code bot,
  supaya proses update kode (`git pull`) tidak akan pernah menyentuh atau
  menghapus database.
- Setup: saat instalasi (atau lewat menu Update), masukkan **GitHub PAT** (fine-grained,
  di-scope ke satu repo backup, izin *Contents: Read and write*) dan **repo tujuan**
  (`owner/repo`, harus repo terpisah dari repo source code bot ini).
- Saat instalasi, kalau repo backup sudah punya database lama, otomatis di-restore
  lebih dulu sebelum bot dijalankan.
- Konfigurasi tersimpan di `.env`: `GH_BACKUP_ENABLED`, `GH_BACKUP_PAT`,
  `GH_BACKUP_REPO`, `GH_BACKUP_BRANCH` (default `main`), `GH_BACKUP_PATH`
  (default `bot_data.db.gz`).
- Kosongkan PAT saat instalasi kalau tidak ingin memakai fitur ini — bot tetap
  berjalan normal tanpa backup GitHub.

## Keamanan
- Token Bot & ID Owner wajib via environment variable (`TELEGRAM_BOT_TOKEN`,
  `OWNER_TELEGRAM_ID`) — bot menolak start jika tidak diset.
- Rahasia disimpan di `/opt/ai-bot/.env` (chmod 600), dimuat oleh systemd `EnvironmentFile=`.
- Bot jalan sebagai user sistem non-root (`aibot`) dengan systemd hardening
  (`ProtectSystem=strict`, `NoNewPrivileges=true`, dll). Worker Node (`aiworker`)
  memakai hardening yang sama di service `ai-worker-agent`.
- Hanya `OWNER_TELEGRAM_ID` yang bisa memakai command admin (`/gencode`, `/users`,
  `/ban`, `/broadcast`, dll) — command ini ditolak otomatis untuk user lain, dan
  menu commandnya pun hanya terdaftar di chat pribadi owner (lihat "Perintah Bot").
- Komunikasi Master ↔ Worker Node diautentikasi lewat header `X-API-KEY` unik
  per node; Admin Dashboard (`/admin`) dilindungi HTTP Basic Auth (`ADMIN_USERNAME`/
  `ADMIN_PASSWORD`), dan API Key Worker Node tidak pernah ditampilkan penuh di UI
  (hanya 4 karakter terakhir).

## Mengganti Token Bot / ID Owner
```
sudo nano /opt/ai-bot/.env
sudo systemctl restart ai-bot
```

## Melihat Log
```
sudo journalctl -u ai-bot -f
```

## Restart Service
```
sudo systemctl restart ai-bot
sudo systemctl restart ollama
```
