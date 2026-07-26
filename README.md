# Private AI Telegram Bot

Bot Telegram AI privat (mode polling): chat/coding (qwen2.5-coder:14b) + analisis
gambar & video (qwen2.5vl). Setiap user mendapat limit chat harian, dengan sistem
kode redeem untuk menaikkan limit (termasuk unlimited) hingga masa berlaku tertentu.

## Isi Paket
- `bot.py`         — Bot Telegram utama (polling, python-telegram-bot)
- `ai_engine.py`   — Logika inti: ekstraksi file, panggilan ke Ollama (chat & vision)
- `database.py`    — Layer SQLite: riwayat chat, data user, limit, kode redeem
- `install.sh`     — Installer otomatis (curl | sudo bash)

## Cara Deploy
1. Upload keempat file di atas ke repo GitHub Anda.
2. Edit `install.sh`, ganti `REPO_RAW_BASE` dengan URL raw repo Anda.
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

## Limit Chat & Kode Redeem

- Setiap user baru otomatis mendapat **20 chat/hari**, reset otomatis setiap hari.
- Owner bisa membuat kode redeem lewat command di Telegram:
  ```
  /gencode <limit> <hari>        contoh: /gencode 50 30
  /gencode unlimited <hari>      contoh: /gencode unlimited 365
  ```
- User menukar kode dengan:
  ```
  /redeem <kode>
  ```
- Setiap kode hanya bisa dipakai **satu kali** oleh satu user.
- Setelah masa berlaku (hari) habis, limit user otomatis kembali ke default 20/hari —
  ini dicek otomatis setiap kali user mengirim pesan, tidak perlu campur tangan owner.

## Perintah Bot

**Semua user:**
| Command | Fungsi |
|---|---|
| `/start`, `/help` | Info & daftar perintah |
| `/status` | Lihat sisa limit chat hari ini / status plan |
| `/redeem <kode>` | Tukar kode redeem untuk menaikkan limit |
| `/reset` | Hapus riwayat chat (mulai percakapan baru) |

**Khusus Owner** (berdasarkan `OWNER_TELEGRAM_ID`):
| Command | Fungsi |
|---|---|
| `/gencode <limit\|unlimited> <hari>` | Buat kode redeem baru |
| `/codes` | Lihat kode redeem yang belum dipakai |
| `/users` | Lihat daftar user & status limit mereka |
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
satu penggunaan limit chat harian.

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
