# Private AI Telegram Bot

Bot Telegram AI privat (mode polling): chat/coding lewat 3 tier model
(qwen2.5-coder 1.5b/7b/14b) + analisis gambar & video (qwen2.5vl). Setiap user
mendapat kuota token harian, dengan sistem kode redeem untuk menaikkan kuota
(termasuk unlimited) hingga masa berlaku tertentu.

## Isi Paket
- `bot.py`            — Bot Telegram utama (polling, python-telegram-bot)
- `ai_engine.py`      — Logika inti: ekstraksi file, panggilan ke Ollama (chat & vision)
- `database.py`       — Layer SQLite: riwayat chat, data user, limit, kode redeem
- `github_backup.py`  — Backup & restore database ke GitHub lewat REST API (lihat bagian di bawah)
- `install.sh`        — Installer otomatis (curl | sudo bash)

## Cara Deploy
1. Upload kelima file di atas ke repo GitHub Anda (ini repo *source code*, terpisah
   dari repo backup database di bagian "Backup Database ke GitHub" di bawah).
2. Edit `install.sh`, ganti `REPO_GIT_URL` di bagian atas file dengan URL repo Anda.
3. Siapkan dua hal dari Telegram sebelum instalasi:
   - **Token Bot** dari [@BotFather](https://t.me/BotFather) (perintah `/newbot`)
   - **ID Telegram Owner** dari [@userinfobot](https://t.me/userinfobot) (kirim pesan apa
     saja, ID kamu akan ditampilkan)
4. Di VPS Ubuntu, jalankan:
   ```
   curl -sL https://raw.githubusercontent.com/BimxyzDev/Private-AI-Telegram-Bot/main/install.sh | sudo bash
   ```
5. Installer akan meminta Token Bot dan ID Owner di tengah proses instalasi, lalu
   otomatis menyimpannya ke `/opt/ai-bot/.env` (chmod 600) — TIDAK ADA di source code.
6. Setelah instalasi selesai, buka Telegram dan kirim `/start` ke bot Anda.

Bot berjalan mode **polling**, jadi tidak butuh domain, SSL/certbot, maupun Nginx —
cukup koneksi internet keluar dari VPS ke server Telegram.

## Kuota Token & Kode Redeem

- Setiap user baru otomatis mendapat **50.000 token/hari** (reset otomatis tiap
  ganti hari UTC). Token terpakai dihitung dari jumlah token asli Ollama
  (prompt + hasil jawaban) dikali multiplier tier model yang dipakai.
- User memilih tier model AI lewat `/model`:
  - 🟢 **Light** (`qwen2.5-coder:1.5b`) — kuota token x1, paling irit
  - 🟡 **Medium** (`qwen2.5-coder:7b`) — kuota token x2, default
  - 🔴 **Heavy** (`qwen2.5-coder:14b`) — kuota token x3, reasoning maksimal
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
| `/status` | Lihat sisa kuota token hari ini & tier model aktif |
| `/model` | Pilih tier model AI (Light/Medium/Heavy) |
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
  (`ProtectSystem=strict`, `NoNewPrivileges=true`, dll).
- Hanya `OWNER_TELEGRAM_ID` yang bisa memakai command admin (`/gencode`, `/users`,
  `/ban`, `/broadcast`, dll) — command ini ditolak otomatis untuk user lain.

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
