"""
Backup & Restore Database ke GitHub — via REST Contents API
=============================================================
INI ADALAH DESAIN BARU yang menggantikan pendekatan lama (git commit + git
push langsung di working directory aplikasi). Alasan penggantian:

Masalah pendekatan lama:
  1. `git init`/`remote add db-backup` dijalankan DI DALAM git repo yang sama
     dengan source code bot (remote `origin`). Setiap 60 detik bot melakukan
     `git add bot_data.db` + commit + `push db-backup HEAD:main` — ini
     mem-push SELURUH HEAD (bot.py, ai_engine.py, dst, BUKAN cuma database)
     ke repo backup, dan riwayatnya menumpuk terus tanpa batas.
  2. Saat menu "Update" jalan (`git fetch origin` + `git reset --hard
     origin/main`), commit lokal hasil auto-backup ikut ter-reset. Karena
     `bot_data.db` sempat masuk ke index lokal tapi tidak ada di tree
     `origin/main`, `reset --hard` MENGHAPUS file database dari working
     directory — bot bisa kehilangan seluruh riwayat chat & user saat update.
  3. PAT GitHub disisipkan langsung di remote URL (`https://<PAT>@github...`)
     yang tersimpan permanen di `.git/config` dalam bentuk plaintext.
  4. Setup awal harus list & filter semua repo user lewat GitHub API hanya
     untuk menebak repo mana yang dipakai backup — rumit dan rapuh.

Desain baru jauh lebih sederhana:
  - HANYA memakai HTTP request ke GitHub REST API (`requests`), sama sekali
    tidak menyentuh binary `git` maupun repo `origin` milik source code bot.
  - Database di-gzip dulu baru di-upload sebagai SATU file lewat endpoint
    "Create or update file contents" (limit resmi GitHub: 100 MB per file —
    jauh lebih dari cukup untuk database SQLite bot ini).
  - Restore = kebalikannya: GET file, base64-decode, gunzip, tulis ke disk.
  - User cukup memasukkan PAT + "owner/repo" tujuan secara eksplisit saat
    instalasi — tidak perlu auto-deteksi/list repo yang rawan salah pilih.

Env/config yang dipakai (lihat bot.py & install.sh):
  GH_BACKUP_ENABLED, GH_BACKUP_PAT, GH_BACKUP_REPO ("owner/repo"),
  GH_BACKUP_BRANCH (default "main"), GH_BACKUP_PATH (default
  "bot_data.db.gz").
"""

import base64
import gzip
import logging
from typing import Optional

import requests

logger = logging.getLogger("ai-bot.github_backup")

API_BASE = "https://api.github.com"
TIMEOUT = 30


def _headers(pat: str) -> dict:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _contents_url(repo: str, path: str) -> str:
    return f"{API_BASE}/repos/{repo}/contents/{path}"


def check_repo_access(pat: str, repo: str) -> bool:
    """Cek apakah PAT valid & punya akses baca ke repo ('owner/repo')."""
    try:
        resp = requests.get(f"{API_BASE}/repos/{repo}", headers=_headers(pat), timeout=TIMEOUT)
        return resp.status_code == 200
    except requests.RequestException as exc:
        logger.warning("GH backup: gagal menghubungi GitHub API: %s", exc)
        return False


def pull_backup(pat: str, repo: str, path: str, branch: str, dest_file: str) -> bool:
    """
    Ambil backup database dari GitHub (kalau ada) dan tulis ke `dest_file`.
    Return True kalau berhasil restore, False kalau memang belum ada backup
    atau terjadi kegagalan (bukan exception — supaya pemanggil bisa lanjut
    dengan aman/membuat database baru jika restore tidak tersedia).
    """
    try:
        resp = requests.get(
            _contents_url(repo, path),
            headers=_headers(pat),
            params={"ref": branch},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("GH restore: gagal menghubungi GitHub API: %s", exc)
        return False

    if resp.status_code == 404:
        logger.info("GH restore: belum ada backup di %s (%s), lewati restore.", repo, path)
        return False
    if resp.status_code != 200:
        logger.warning(
            "GH restore: gagal mengambil backup (HTTP %s): %s", resp.status_code, resp.text[:300]
        )
        return False

    data = resp.json()
    try:
        raw = base64.b64decode(data["content"])
        db_bytes = gzip.decompress(raw)
    except Exception as exc:
        logger.warning("GH restore: gagal decode/decompress backup: %s", exc)
        return False

    with open(dest_file, "wb") as f:
        f.write(db_bytes)
    logger.info("GH restore: database berhasil di-restore dari %s (%s).", repo, path)
    return True


def push_backup(pat: str, repo: str, path: str, branch: str, src_file: str) -> bool:
    """Kompres `src_file` (gzip) lalu upload/update ke GitHub lewat Contents API."""
    try:
        with open(src_file, "rb") as f:
            raw = f.read()
    except OSError as exc:
        logger.warning("GH backup: gagal membaca %s: %s", src_file, exc)
        return False

    compressed = gzip.compress(raw)
    content_b64 = base64.b64encode(compressed).decode("ascii")

    # Ambil sha file lama dulu (kalau ada). GitHub WAJIB diberi tahu sha file
    # lama saat meng-update file yang sudah ada, kalau tidak request ditolak.
    sha: Optional[str] = None
    try:
        get_resp = requests.get(
            _contents_url(repo, path),
            headers=_headers(pat),
            params={"ref": branch},
            timeout=TIMEOUT,
        )
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha")
    except requests.RequestException as exc:
        logger.warning("GH backup: gagal cek sha file lama (lanjut coba sebagai file baru): %s", exc)

    body = {
        "message": "Auto-backup database bot",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    try:
        put_resp = requests.put(
            _contents_url(repo, path), headers=_headers(pat), json=body, timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        logger.warning("GH backup: gagal upload ke GitHub: %s", exc)
        return False

    if put_resp.status_code in (200, 201):
        logger.info("GH backup: database berhasil di-upload ke %s (%s).", repo, path)
        return True

    logger.warning(
        "GH backup: upload gagal (HTTP %s): %s", put_resp.status_code, put_resp.text[:300]
    )
    return False
