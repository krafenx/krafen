#!/usr/bin/env python3
"""
Сервер сайта Krafen и архива стримов danonsha (FastAPI + SQLite).

Полностью заменяет Cloudflare Workers и Workers KV:
  - раздаёт HTML/CSS/JS и медиа-ассеты с чистыми URL (/watchlist, /setup и т.д.);
  - хранит коллекции (watchlist, gamelist, setup, wishlist) и OAuth-токены в SQLite (data/krafen.db)
    с автоматической миграцией существующих JSON-файлов;
  - реализует защищённую админ-сессию (HMAC SHA-256 cookie) и версионирование списков (revision);
  - проксирует и кэширует Twitch Helix, Discord Invite, Spotify, Last.fm, TMDB, Shikimori и IGDB;
  - раздаёт VOD-архив с поддержкой HTTP Range и чата записи;
  - при RECORDER=1 запускает фоновый процесс автозаписи recorder/recorder.py.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import html
import hmac
import io
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)

try:
    from cryptography.hazmat.primitives import serialization as _crypto_ser
    from py_vapid import Vapid as _VapidCls
    from pywebpush import WebPushException as _WebPushError
    from pywebpush import webpush as _send_webpush

    _WEBPUSH_AVAILABLE = True
except Exception:
    _crypto_ser = None
    _VapidCls = None
    _WebPushError = Exception
    _send_webpush = None
    _WEBPUSH_AVAILABLE = False

try:
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILDraw
    from PIL import ImageFilter as _PILFilter
    from PIL import ImageFont as _PILFont
    from PIL import ImageOps as _PILOps

    _PIL_AVAILABLE = True
except Exception:
    _PILImage = _PILDraw = _PILFilter = _PILFont = _PILOps = None
    _PIL_AVAILABLE = False

ROOT = Path(__file__).resolve().parent
_DOTENV_VALUES: dict[str, str] = {}


def load_dotenv(env_file: Path) -> None:
    """Читает локальный .env без внешних зависимостей, не перезаписывая уже заданные переменные."""
    if not env_file.is_file():
        return
    try:
        raw = env_file.read_bytes()
    except OSError:
        return
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text_content = raw.decode("utf-16", errors="ignore")
    else:
        text_content = raw.decode("utf-8-sig", errors="ignore")
        if "\x00" in text_content:
            text_content = text_content.replace("\x00", "")
    for raw_line in text_content.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        _DOTENV_VALUES[key] = val
        os.environ.setdefault(key, val)


ENV_FILE_PATH = ROOT / ".env"
load_dotenv(ENV_FILE_PATH)


def sync_env_file() -> bool:
    """Дописывает в .env параметры, появившиеся в .env.example после обновления.

    Существующие строки не меняются, поэтому правки пользователя в безопасности.
    Отключается переменной ENV_SYNC=0. Возвращает True, если файл был изменён.
    """
    try:
        import env_sync
    except Exception:
        return False
    if not env_sync.sync_enabled():
        return False
    try:
        result = env_sync.sync_env(ROOT)
    except Exception as exc:  # синхронизация не должна ронять сервер
        print(f"[env] синхронизация .env не удалась: {exc}", flush=True)
        return False
    if not result.ok:
        print(f"[env] не удалось обновить .env: {result.skipped_reason}", flush=True)
    elif result.changed:
        keys = ", ".join(result.added)
        print(f"[env] добавлено новых параметров: {len(result.added)} ({keys})", flush=True)
    return result.changed


if sync_env_file():
    # Дозагружаем .env, чтобы новые параметры действовали уже в этом запуске.
    # load_dotenv не перезаписывает ранее заданные переменные.
    load_dotenv(ENV_FILE_PATH)


def _resolve_root_path(raw_value: str | Path) -> Path:
    p = Path(raw_value)
    return p if p.is_absolute() else (ROOT / p).resolve()


DATA_DIR = _resolve_root_path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "krafen.db"
AUTH_DB_PATH = DATA_DIR / "auth.db"
ANALYTICS_DB_PATH = DATA_DIR / "analytics.db"
HEADLESS_LOG_PATH = DATA_DIR / "server.log"
SEED_SQL_PATH = DATA_DIR / "seed.sql"
_SERVER_STARTED_AT = time.time()
THUMB_CACHE_DIR = DATA_DIR / "vod_thumbs"
CHAT_CACHE_DIR = DATA_DIR / "vod_chats"
IMG_CACHE_DIR = DATA_DIR / "img_cache"
RECORD_DIR = _resolve_root_path(os.environ.get("RECORD_DIR", ROOT / "vods"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").strip().rstrip("/")

_RAW_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_WEAK_PLACEHOLDER_PASSWORDS = {"change-me", "changeme"}
ADMIN_PASSWORD = "" if _RAW_ADMIN_PASSWORD.strip().lower() in _WEAK_PLACEHOLDER_PASSWORDS else _RAW_ADMIN_PASSWORD
_ADMIN_PASSWORD_IS_PLACEHOLDER = bool(_RAW_ADMIN_PASSWORD and not ADMIN_PASSWORD)
SECURE_COOKIES_MODE = os.environ.get("SECURE_COOKIES", "auto").strip().lower()
TRUST_PROXY_MODE = os.environ.get("TRUST_PROXY_HEADERS", "auto").strip().lower()

TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "krafen")
RECORDER_CHANNEL = (os.environ.get("RECORDER_CHANNEL") or "danonsha").strip() or "danonsha"
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@krafen.me").strip() or "mailto:admin@krafen.me"
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
IGDB_CLIENT_ID = os.environ.get("IGDB_CLIENT_ID", "") or TWITCH_CLIENT_ID
IGDB_CLIENT_SECRET = os.environ.get("IGDB_CLIENT_SECRET", "") or TWITCH_CLIENT_SECRET
IGDB_ACCESS_TOKEN = os.environ.get("IGDB_ACCESS_TOKEN", "")

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LASTFM_USER = os.environ.get("LASTFM_USER", "krafen")
TMDB_ACCESS_TOKEN = os.environ.get("TMDB_ACCESS_TOKEN", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
DISCORD_INVITE_CODE = os.environ.get("DISCORD_INVITE_CODE", "kGdxDzxtmK")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "1e417_q9s6SjcZHyqMYzbVs8OHqfY5Ifk").strip()
GDRIVE_API_KEY = os.environ.get("GDRIVE_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
try:
    ALERT_MIN_FREE_GB = max(0.2, float(os.environ.get("ALERT_MIN_FREE_GB", "2.0") or "2.0"))
except ValueError:
    ALERT_MIN_FREE_GB = 2.0
RECORDER_TOKEN = os.environ.get("RECORDER_TOKEN", "").strip()
MSK_TZ = timezone(timedelta(hours=3))
TG_BACKUP_DAILY = os.environ.get("TG_BACKUP_DAILY", "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_E2B_PREVIEW = os.environ.get("ALLOW_E2B_PREVIEW", "0").strip().lower() in {"1", "true", "yes", "on"}

AUTO_UPDATE_MODE = os.environ.get("AUTO_UPDATE", "auto").strip().lower()
AUTO_UPDATE_REPO = os.environ.get("AUTO_UPDATE_REPO", "krafenx/krafen").strip().strip("/")
AUTO_UPDATE_BRANCH = os.environ.get("AUTO_UPDATE_BRANCH", "main").strip() or "main"
try:
    AUTO_UPDATE_INTERVAL = max(0, int(os.environ.get("AUTO_UPDATE_INTERVAL", "0") or "0"))
except ValueError:
    AUTO_UPDATE_INTERVAL = 0
UPDATE_SHA_PATH = DATA_DIR / ".update_sha"
_UPDATE_LOCK = threading.Lock()

SESSION_COOKIE = "kf_admin_session"
SPOTIFY_STATE_COOKIE = "kf_spotify_oauth"
# Сессии живут как в соцсетях: 30 дней по умолчанию, срок продлевается при активности.
try:
    _SESSION_TTL_DAYS = max(1, min(365, int(os.environ.get("SESSION_TTL_DAYS", "30") or "30")))
except ValueError:
    _SESSION_TTL_DAYS = 30
SESSION_TTL_SECONDS = _SESSION_TTL_DAYS * 24 * 60 * 60
# Продлеваем не чаще раза в сутки, чтобы не переписывать cookie на каждый запрос.
SESSION_RENEW_AFTER_SECONDS = 24 * 60 * 60

# ----------------------------- профили -----------------------------
# Профиль = владелец набора коллекций (watchlist/gamelist/setup/wishlist).
# "krafen" — основной, существующие данные принадлежат ему; "danonsha" — личный
# профиль в /danonsha/ со своим входом и своим оформлением.
DEFAULT_PROFILE = "krafen"

PROFILES: dict[str, dict[str, Any]] = {
    "krafen": {
        "display_name": "krafen",
        "base_path": "",
        "password_env": "ADMIN_PASSWORD",
        "cookie": "kf_admin_session",
        "store_scope": True,
    },
    "danonsha": {
        "display_name": "danonsha",
        "base_path": "/danonsha",
        "password_env": "ADMIN_PASSWORD_DANONSHA",
        "cookie": "kf_danonsha_session",
        "store_scope": False,
    },
}

# Пароли профилей берутся только из окружения (.env / переменные процесса).
def _load_profile_password(spec: dict[str, Any]) -> str:
    raw = os.environ.get(spec["password_env"], "")
    if not raw:
        return ""
    if raw.strip().lower() in _WEAK_PLACEHOLDER_PASSWORDS:
        return ""
    return raw


PROFILE_PASSWORDS: dict[str, str] = {name: _load_profile_password(spec) for name, spec in PROFILES.items()}


def profile_spec(profile: str | None) -> dict[str, Any]:
    return PROFILES.get(profile or DEFAULT_PROFILE, PROFILES[DEFAULT_PROFILE])


def profile_enabled(profile: str) -> bool:
    return bool(PROFILE_PASSWORDS.get(profile))


def profile_scope(profile: str) -> str:
    """Пространство имён для кэша; основной профиль сохраняет прежние ключи."""
    spec = profile_spec(profile)
    return profile if (not spec["store_scope"] or profile != DEFAULT_PROFILE) else ""


def collection_key(profile: str, name: str) -> str:
    scope = profile_scope(profile)
    return f"{scope}:{name}" if scope else name


TMDB_BASE = "https://api.themoviedb.org"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
SHIKIMORI_BASE = "https://shikimori.one"
IGDB_BASE = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
DISCORD_INVITE_BASE = "https://discord.com/api/invites"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)

_UPDATE_SKIP_TOP_DIRS = {".git", ".github", ".venv", "venv", "__pycache__", "vods"}
_MAX_UPDATE_ZIP_BYTES = 64 * 1024 * 1024


def _is_pterodactyl_env() -> bool:
    return bool(
        os.environ.get("P_SERVER_UUID")
        or os.environ.get("P_SERVER_LOCATION")
        or str(ROOT).startswith("/home/container")
    )


def is_auto_update_enabled() -> bool:
    # 1. Приоритет у собственной переменной KRAFEN_AUTO_UPDATE (из .env или окружения),
    #    которая не конфликтует с системной AUTO_UPDATE=0 панели Pterodactyl.
    explicit_kf = (_DOTENV_VALUES.get("KRAFEN_AUTO_UPDATE") or os.environ.get("KRAFEN_AUTO_UPDATE") or "").strip().lower()
    if explicit_kf in {"0", "false", "no", "off"}:
        return False
    if explicit_kf in {"1", "true", "yes", "on"}:
        return True

    # 2. Если пользователь явно прописал AUTO_UPDATE именно в своём локальном .env — учитываем его.
    dotenv_au = (_DOTENV_VALUES.get("AUTO_UPDATE") or "").strip().lower()
    if dotenv_au in {"0", "false", "no", "off"}:
        return False
    if dotenv_au in {"1", "true", "yes", "on"}:
        return True

    # 3. В контейнере Pterodactyl (H1Cloud и др.) сама панель выставляет системный AUTO_UPDATE=0 —
    #    игнорируем его и включаем встроенное автообновление по умолчанию (даже если остался .git).
    if _is_pterodactyl_env():
        return True

    # 4. Вне Pterodactyl: уважаем системную AUTO_UPDATE, а в режиме auto включаем, если нет .git.
    if AUTO_UPDATE_MODE in {"0", "false", "no", "off"}:
        return False
    if AUTO_UPDATE_MODE in {"1", "true", "yes", "on"}:
        return True
    return not (ROOT / ".git").exists()


def _is_protected_update_path(rel_posix: str, target: Path) -> bool:
    parts = [p for p in rel_posix.split("/") if p]
    if not parts or ".." in parts:
        return True
    top = parts[0]
    if top in _UPDATE_SKIP_TOP_DIRS:
        return True
    if len(parts) == 1 and (top == ".env" or (top.startswith(".env.") and top != ".env.example")):
        return True
    if top == "data":
        if len(parts) >= 2 and parts[1] in {".update_sha", "vod_thumbs", "vod_chats", "img_cache"}:
            return True
        name_lower = parts[-1].lower()
        is_user_db_or_json = (
            name_lower.endswith(".json")
            or name_lower.endswith(".db")
            or ".db-" in name_lower
            or ".sqlite" in name_lower
        )
        # Сохраняем живую БД и пользовательские JSON, если они уже созданы на диске
        if is_user_db_or_json and target.exists():
            return True
    return False


def _resolve_github_token() -> str:
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "ACCESS_TOKEN", "GIT_ACCESS_TOKEN"):
        val = (_DOTENV_VALUES.get(key) or os.environ.get(key) or "").strip()
        if val:
            return val
    return ""


def _fetch_remote_branch_sha() -> tuple[str, str]:
    """Возвращает (sha, error_code). При успехе error_code == ''."""
    if not AUTO_UPDATE_REPO or "/" not in AUTO_UPDATE_REPO:
        return "", "invalid_repo"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "krafen-auto-updater/1.0",
    }
    gh_token = _resolve_github_token()
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    api_url = f"https://api.github.com/repos/{AUTO_UPDATE_REPO}/commits/{AUTO_UPDATE_BRANCH}"
    try:
        resp = requests.get(api_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            sha = str(data.get("sha") or "").strip()
            if sha:
                return sha, ""
        return "", f"http_{resp.status_code}"
    except Exception as exc:
        return "", type(exc).__name__


_SAFE_GH_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_GH_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,120}$")
_ALLOWED_GH_ZIP_HOSTS = {"api.github.com", "github.com", "codeload.github.com"}


def _is_allowed_github_download_host(hostname: str) -> bool:
    h = (hostname or "").lower().rstrip(".")
    return h in _ALLOWED_GH_ZIP_HOSTS or h.endswith(".githubusercontent.com")


def _download_github_zip() -> bytes:
    if not _SAFE_GH_REPO_RE.fullmatch(AUTO_UPDATE_REPO) or not _SAFE_GH_REF_RE.fullmatch(AUTO_UPDATE_BRANCH):
        raise ValueError("invalid_auto_update_config")
    gh_token = _resolve_github_token()
    candidates: list[tuple[str, dict[str, str]]] = []
    api_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "krafen-auto-updater/1.0",
    }
    if gh_token:
        api_headers["Authorization"] = f"Bearer {gh_token}"
    # Для приватных репозиториев GitHub отдаёт архив именно через REST API /zipball/{ref}
    candidates.append(
        (f"https://api.github.com/repos/{AUTO_UPDATE_REPO}/zipball/{AUTO_UPDATE_BRANCH}", api_headers)
    )

    pub_headers = {"User-Agent": "krafen-auto-updater/1.0"}
    if gh_token:
        pub_headers["Authorization"] = f"Bearer {gh_token}"
    candidates.append(
        (f"https://codeload.github.com/{AUTO_UPDATE_REPO}/zip/refs/heads/{AUTO_UPDATE_BRANCH}", pub_headers)
    )
    candidates.append(
        (f"https://github.com/{AUTO_UPDATE_REPO}/archive/refs/heads/{AUTO_UPDATE_BRANCH}.zip", pub_headers)
    )

    last_err: Exception | None = None
    saw_404 = False
    for start_url, base_headers in candidates:
        try:
            cur_url = start_url
            cur_headers = dict(base_headers)
            for _hop in range(5):
                parsed = urlsplit(cur_url)
                if parsed.scheme != "https" or not _is_allowed_github_download_host(parsed.hostname or ""):
                    raise ValueError("disallowed_redirect_host")
                # Не пересылаем токен на CDN/S3 presigned URLs (*.githubusercontent.com)
                if (parsed.hostname or "").lower().endswith(".githubusercontent.com"):
                    cur_headers.pop("Authorization", None)
                with requests.get(cur_url, headers=cur_headers, stream=True, timeout=25, allow_redirects=False) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = (resp.headers.get("location") or "").strip()
                        if not loc:
                            raise RuntimeError("empty_redirect_location")
                        cur_url = loc
                        continue
                    if resp.status_code == 404:
                        saw_404 = True
                    resp.raise_for_status()
                    buf = io.BytesIO()
                    total = 0
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_UPDATE_ZIP_BYTES:
                            raise ValueError("update_archive_too_large")
                        buf.write(chunk)
                    return buf.getvalue()
            raise RuntimeError("too_many_redirects")
        except Exception as exc:
            last_err = exc
    if saw_404 and not gh_token:
        raise RuntimeError(
            f"репозиторий {AUTO_UPDATE_REPO} приватный (404) — задайте GITHUB_TOKEN в .env (Contents: Read-only) или сделайте репозиторий публичным"
        )
    raise RuntimeError(f"не удалось скачать архив с GitHub ({last_err})")


def perform_github_update(*, force: bool = False, allow_zip_fallback_on_api_error: bool = True) -> dict[str, Any]:
    with _UPDATE_LOCK:
        saved_sha = ""
        try:
            if UPDATE_SHA_PATH.is_file():
                saved_sha = UPDATE_SHA_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            saved_sha = ""

        remote_sha, api_err = _fetch_remote_branch_sha()
        if not force and remote_sha and saved_sha and remote_sha == saved_sha:
            print(f"автообновление: актуальная версия ({remote_sha[:7]})", flush=True)
            return {
                "ok": True,
                "up_to_date": True,
                "sha": remote_sha,
                "updated": [],
                "server_changed": False,
            }
        if not remote_sha and not allow_zip_fallback_on_api_error:
            return {"ok": False, "error": f"github_api_{api_err}", "updated": [], "server_changed": False}

        try:
            raw_zip = _download_github_zip()
            updated_files: list[str] = []
            server_changed = False
            total_unpacked = 0

            with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        continue
                    if info.file_size > _MAX_UPDATE_ZIP_BYTES:
                        raise ValueError("zip_entry_too_large")
                    total_unpacked += max(0, int(info.file_size))
                    if total_unpacked > _MAX_UPDATE_ZIP_BYTES * 4:
                        raise ValueError("zip_unpacked_too_large")
                    raw_name = info.filename.replace("\\", "/").lstrip("/")
                    if "/" not in raw_name:
                        continue
                    _, _, rel_posix = raw_name.partition("/")
                    rel_posix = rel_posix.strip("/")
                    if not rel_posix:
                        continue

                    target = (ROOT / rel_posix).resolve()
                    try:
                        target.relative_to(ROOT)
                    except ValueError:
                        continue

                    if _is_protected_update_path(rel_posix, target):
                        continue

                    new_bytes = zf.read(info)
                    if target.is_file():
                        try:
                            old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                            new_hash = hashlib.sha256(new_bytes).hexdigest()
                            if old_hash == new_hash:
                                continue
                        except OSError:
                            pass

                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = target.with_name(f".{target.name}.upd.{os.getpid()}")
                    try:
                        tmp_path.write_bytes(new_bytes)
                        os.replace(tmp_path, target)
                    finally:
                        if tmp_path.exists():
                            with contextlib.suppress(OSError):
                                tmp_path.unlink()

                    updated_files.append(rel_posix)
                    if rel_posix == "server.py":
                        server_changed = True

            if remote_sha:
                with contextlib.suppress(OSError):
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    UPDATE_SHA_PATH.write_text(remote_sha + "\n", encoding="utf-8")

            sha_label = f" [{AUTO_UPDATE_BRANCH} @ {remote_sha[:7]}]" if remote_sha else f" [{AUTO_UPDATE_BRANCH}]"
            if updated_files:
                preview = ", ".join(updated_files[:8])
                more = f" и ещё {len(updated_files) - 8}" if len(updated_files) > 8 else ""
                print(
                    f"автообновление: обновлено файлов: {len(updated_files)} ({preview}{more}){sha_label}",
                    flush=True,
                )
            else:
                print(f"автообновление: все файлы актуальны{sha_label}", flush=True)

            return {
                "ok": True,
                "up_to_date": len(updated_files) == 0,
                "sha": remote_sha or saved_sha,
                "updated": updated_files,
                "server_changed": server_changed,
            }
        except Exception as exc:
            print(f"автообновление: ошибка обновления ({exc}) — продолжаю с локальными файлами", flush=True)
            return {"ok": False, "error": "update_failed", "updated": [], "server_changed": False}


_HEADLESS_LOG_FH: Any = None
_CONSOLE_TEE_LOG_FH: Any = None
_ACTIVE_REC_PROC: subprocess.Popen | None = None
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_REG_NAME = "KrafenServer"
PID_FILE_PATH = DATA_DIR / "server.pid"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_CLI_MGMT_FLAGS = {
    "--install-autostart": "install",
    "--uninstall-autostart": "uninstall",
    "--restart": "restart",
    "--stop": "stop",
    "--status": "status",
}


class _TeeLogStream:
    """Дублирует вывод обычной консоли в data/server.log без ANSI-кодов."""

    def __init__(self, primary: Any, log_fh: Any) -> None:
        self._primary = primary
        self._log_fh = log_fh
        self.encoding = getattr(primary, "encoding", "utf-8") or "utf-8"

    def write(self, s: str) -> int:
        n = self._primary.write(s)
        if s:
            with contextlib.suppress(Exception):
                self._log_fh.write(_ANSI_ESCAPE_RE.sub("", s))
                self._log_fh.flush()
        return n

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self._primary.flush()
        with contextlib.suppress(Exception):
            self._log_fh.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._primary.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


def _has_cli_mgmt_flag() -> bool:
    return any(flag in sys.argv[1:] for flag in _CLI_MGMT_FLAGS)


def _is_headless_process() -> bool:
    if _has_cli_mgmt_flag():
        return False
    if "--background" in sys.argv[1:] or os.environ.get("KRAFEN_HEADLESS") == "1":
        return True
    if sys.stdout is None or sys.stderr is None:
        return True
    exe_stem = Path(sys.executable).stem.lower()
    if exe_stem.startswith(("pythonw", "pyw")):
        return True
    for stream in (sys.stdout, sys.stderr):
        name = str(getattr(stream, "name", "") or "").lower()
        if name in {"nul", "/dev/null"} or name.endswith(("\\nul", "/nul")):
            return True
    return False


def _open_rotating_server_log() -> Any:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "server.log"
    if log_path.is_file() and log_path.stat().st_size > 10 * 1024 * 1024:
        bak = DATA_DIR / "server.log.old"
        with contextlib.suppress(OSError):
            bak.unlink(missing_ok=True)
            log_path.replace(bak)
    return open(log_path, "a", encoding="utf-8", buffering=1, errors="replace")


def _reexec_current_process() -> None:
    os.environ["_KRAFEN_REEXEC"] = "1"
    script_path = str((ROOT / "server.py").resolve())
    print("автообновление: server.py изменился — перезапускаю процесс...", flush=True)
    if _ACTIVE_REC_PROC is not None and _ACTIVE_REC_PROC.poll() is None:
        with contextlib.suppress(Exception):
            _ACTIVE_REC_PROC.terminate()
            _ACTIVE_REC_PROC.wait(timeout=3)
    if _read_pid_file() == os.getpid():
        with contextlib.suppress(OSError):
            PID_FILE_PATH.unlink(missing_ok=True)
    if os.name != "nt":
        os.execv(sys.executable, [sys.executable, script_path, *sys.argv[1:]])
    elif _HEADLESS_LOG_FH is not None or threading.current_thread() is not threading.main_thread():
        was_headless = _HEADLESS_LOG_FH is not None
        for fh in (_HEADLESS_LOG_FH, _CONSOLE_TEE_LOG_FH):
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.flush()
                    fh.close()
        exe_dir = Path(sys.executable).resolve().parent
        py_console = exe_dir / "python.exe"
        helper_py = str(py_console if py_console.is_file() else Path(sys.executable).resolve())
        target_args = [sys.executable, script_path, *sys.argv[1:]]
        if was_headless and "--background" not in target_args:
            target_args.append("--background")
        flags = _WIN_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
        helper_code = (
            "import subprocess, time; "
            "time.sleep(1.2); "
            f"subprocess.Popen({target_args!r}, cwd={str(ROOT)!r}, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
            f"creationflags={flags})"
        )
        env = os.environ.copy()
        if was_headless:
            env["KRAFEN_HEADLESS"] = "1"
        subprocess.Popen(
            [helper_py, "-c", helper_code],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        os._exit(0)
    else:
        code = subprocess.call([sys.executable, script_path, *sys.argv[1:]])
        raise SystemExit(code)


def _configure_stdio_utf8(*, mirror_console_to_log: bool = False) -> None:
    global _HEADLESS_LOG_FH, _CONSOLE_TEE_LOG_FH
    if _is_headless_process():
        if _HEADLESS_LOG_FH is None:
            try:
                _HEADLESS_LOG_FH = _open_rotating_server_log()
                sys.stdout = _HEADLESS_LOG_FH
                sys.stderr = _HEADLESS_LOG_FH
            except OSError:
                pass
        return

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            with contextlib.suppress(Exception):
                _stream.reconfigure(encoding="utf-8", errors="replace")

    if mirror_console_to_log and _CONSOLE_TEE_LOG_FH is None and not _has_cli_mgmt_flag():
        try:
            _CONSOLE_TEE_LOG_FH = _open_rotating_server_log()
            if sys.stdout is not None and not isinstance(sys.stdout, _TeeLogStream):
                sys.stdout = _TeeLogStream(sys.stdout, _CONSOLE_TEE_LOG_FH)
            if sys.stderr is not None and not isinstance(sys.stderr, _TeeLogStream):
                sys.stderr = _TeeLogStream(sys.stderr, _CONSOLE_TEE_LOG_FH)
        except OSError:
            pass


def _is_local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _read_pid_file() -> int | None:
    try:
        if PID_FILE_PATH.is_file():
            val = int(PID_FILE_PATH.read_text(encoding="utf-8").strip())
            return val if val > 0 else None
    except Exception:
        pass
    return None


def _stop_running_server() -> bool:
    pid = _read_pid_file()
    if not pid or pid == os.getpid():
        return False
    stopped = False
    if os.name == "nt":
        # Безопасно проверяем, что этот PID действительно python/pythonw
        chk = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_WIN_NO_WINDOW,
            check=False,
        )
        out_lower = (chk.stdout or "").lower()
        if "python" in out_lower or "py.exe" in out_lower or "pyw.exe" in out_lower:
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_WIN_NO_WINDOW,
                check=False,
            )
            stopped = res.returncode == 0
    else:
        try:
            os.kill(pid, 15)
            stopped = True
        except OSError:
            pass
    if stopped:
        with contextlib.suppress(OSError):
            PID_FILE_PATH.unlink(missing_ok=True)
        for _ in range(20):
            if not _is_local_port_open(PORT):
                break
            time.sleep(0.15)
    return stopped


def _spawn_background_server() -> Path:
    exe_dir = Path(sys.executable).resolve().parent
    pythonw = exe_dir / "pythonw.exe"
    launcher_exe = pythonw if pythonw.is_file() else Path(sys.executable).resolve()
    script_path = (ROOT / "server.py").resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "server.log"
    if not log_path.exists():
        with contextlib.suppress(OSError):
            log_path.touch()
    env = os.environ.copy()
    env["KRAFEN_HEADLESS"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = _WIN_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [str(launcher_exe), str(script_path), "--background"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    for _ in range(30):
        if _is_local_port_open(PORT):
            break
        time.sleep(0.1)
    return launcher_exe


def _manage_autostart_cli(action: str) -> int:
    if action == "stop":
        if _stop_running_server():
            print("Фоновый сервер (и автозапись) остановлены.")
        elif _is_local_port_open(PORT):
            print(f"Порт {PORT} занят другим окном/процессом (PID-файл не найден) — закройте открытое окно терминала.")
        else:
            print("Сервер сейчас не запущен.")
        return 0

    if action == "status":
        running = _is_local_port_open(PORT)
        pid = _read_pid_file()
        reg_val = None
        if os.name == "nt":
            import winreg

            with contextlib.suppress(FileNotFoundError, OSError):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as key:
                    reg_val, _ = winreg.QueryValueEx(key, _AUTOSTART_REG_NAME)
        print(f"Автозапуск Windows: {'ВКЛЮЧЁН (' + str(reg_val) + ')' if reg_val else 'выключен'}")
        print(f"Статус сервера (порт {PORT}): {'РАБОТАЕТ' + (f' (PID {pid})' if pid else '') if running else 'не запущен'}")
        log_path = DATA_DIR / "server.log"
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            if lines:
                print(f"\nПоследние строки {log_path}:")
                for ln in lines:
                    print(f"  {ln}")
        return 0

    if os.name != "nt":
        print("На Linux используйте systemd-юнит krafen.service: sudo systemctl enable --now krafen")
        return 0

    import winreg  # стандартная библиотека Windows Python

    exe_dir = Path(sys.executable).resolve().parent
    pythonw = exe_dir / "pythonw.exe"
    launcher_exe = pythonw if pythonw.is_file() else Path(sys.executable).resolve()
    script_path = (ROOT / "server.py").resolve()
    cmd_str = f'"{launcher_exe}" "{script_path}" --background'

    if action == "restart":
        # Если запись в автозагрузке уже есть — обновим команду до актуальной (--background)
        with contextlib.suppress(FileNotFoundError, OSError):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as rkey:
                winreg.QueryValueEx(rkey, _AUTOSTART_REG_NAME)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as wkey:
                winreg.SetValueEx(wkey, _AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd_str)
        was_stopped = _stop_running_server()
        if not _is_local_port_open(PORT):
            _spawn_background_server()
            state_word = "перезапущен" if was_stopped else "запущен"
            print(f"Фоновый сервер {state_word} на порту {PORT}.")
            print(f"Логи пишутся в: {DATA_DIR / 'server.log'}")
        else:
            print(f"Порт {PORT} занят открытым окном терминала — закройте его и повторите команду.")
        return 0

    if action == "install":
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd_str)
        print(f"Автозапуск включён в Windows (HKCU\\...\\Run -> {_AUTOSTART_REG_NAME}):")
        print(f"  {cmd_str}")
        print(f"Логи фонового режима пишутся в: {DATA_DIR / 'server.log'}")
        was_stopped = _stop_running_server()
        if not _is_local_port_open(PORT):
            _spawn_background_server()
            if was_stopped:
                print(f"Фоновый сервер перезапущен на порту {PORT}.")
            else:
                print(f"Сервер сразу запущен в фоне (без окна) на порту {PORT}.")
        else:
            print(
                f"Сервер сейчас уже запущен на порту {PORT} в открытом окне консоли. "
                "Закройте то окно и выполните py server.py --restart, чтобы перевести работу в тихий фоновый режим."
            )
        print("Управление: py server.py --status | py server.py --restart | py server.py --stop | py server.py --uninstall-autostart")
        return 0

    if action == "uninstall":
        removed = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _AUTOSTART_REG_NAME)
                removed = True
        except FileNotFoundError:
            pass
        stopped = _stop_running_server()
        print("Автозапуск отключён." if removed else "Запись автозапуска не найдена (уже отключена).")
        if stopped:
            print("Фоновый сервер остановлен.")
        return 0

    return 1


_startup_auto_update_done = False


def _maybe_run_startup_auto_update() -> None:
    global _startup_auto_update_done
    if _startup_auto_update_done:
        return
    _startup_auto_update_done = True
    if not is_auto_update_enabled():
        return
    if os.environ.get("_KRAFEN_REEXEC") == "1":
        os.environ.pop("_KRAFEN_REEXEC", None)
        return
    upd = perform_github_update(force=False, allow_zip_fallback_on_api_error=True)
    if upd.get("ok") and upd.get("server_changed"):
        _reexec_current_process()


def _cf_api_call(method: str, url: str, auth_headers: dict[str, str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "krafen-cf-helper/1.0",
        **auth_headers,
    }
    raw_body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"success": False, "errors": [{"code": exc.code, "message": body[:200]}]}
    except Exception as exc:
        return {"success": False, "errors": [{"message": str(exc)}]}


def _disable_cloudflare_ech_cli(extra_args: list[str]) -> int:
    """
    Отключает TLS ECH (Encrypted Client Hello) и включает HTTP/3 (QUIC) + 0-RTT
    в бесплатной зоне Cloudflare через официальный API v4 (для обхода блокировки ECH на ТСПУ в РФ).
    """
    print("=== Настройка зоны Cloudflare для работы из РФ (отключение TLS ECH + включение HTTP/3) ===")
    domain = (
        os.environ.get("CF_DOMAIN")
        or (urlsplit(PUBLIC_ORIGIN).hostname if PUBLIC_ORIGIN else "")
        or "krafen.me"
    ).strip().lower()
    zone_id = (os.environ.get("CF_ZONE_ID") or "").strip()
    api_token = (os.environ.get("CF_API_TOKEN") or "").strip()
    cf_email = (os.environ.get("CF_EMAIL") or "").strip()
    cf_global_key = (os.environ.get("CF_GLOBAL_KEY") or "").strip()

    pos = [a.strip() for a in extra_args if a.strip() and not a.startswith("-")]
    if len(pos) == 1:
        api_token = pos[0]
    elif len(pos) >= 2:
        if "@" in pos[0]:
            cf_email, cf_global_key = pos[0], pos[1]
            if len(pos) >= 3:
                domain = pos[2].lower()
        else:
            zone_id, api_token = pos[0], pos[1]

    if not api_token and not (cf_email and cf_global_key):
        print(f"Домен: {domain}")
        print("В бесплатном тарифе Cloudflare переключатель ECH в браузере закрыт, но через API он отключается бесплатно.")
        print("Где взять ключ (любой из двух вариантов за 15 секунд):")
        print("  Вариант А (проще всего): Cloudflare → My Profile → API Tokens → Global API Key → View")
        print("  Вариант Б: API Token с правами Zone → Zone Settings → Edit и Zone → SSL and Certificates → Edit\n")
        try:
            entered = input("Введите API Token ИЛИ ваш Email от Cloudflare: ").strip()
        except EOFError:
            entered = ""
        if not entered:
            print("Отменено: ключ не указан.")
            return 1
        if "@" in entered:
            cf_email = entered
            try:
                cf_global_key = input("Введите Global API Key: ").strip()
            except EOFError:
                cf_global_key = ""
        else:
            api_token = entered

    if cf_email and cf_global_key:
        auth_headers = {"X-Auth-Email": cf_email, "X-Auth-Key": cf_global_key}
    elif api_token:
        auth_headers = {"Authorization": f"Bearer {api_token}"}
    else:
        print("Ошибка: не указан API Token или пара Email + Global API Key.")
        return 1

    if not zone_id:
        z_resp = _cf_api_call("GET", f"https://api.cloudflare.com/client/v4/zones?name={quote(domain)}", auth_headers)
        zones = z_resp.get("result") if isinstance(z_resp.get("result"), list) else []
        if not zones:
            z_all = _cf_api_call("GET", "https://api.cloudflare.com/client/v4/zones?per_page=50", auth_headers)
            all_list = z_all.get("result") if isinstance(z_all.get("result"), list) else []
            for z in all_list:
                if isinstance(z, dict) and str(z.get("name") or "").lower() == domain:
                    zones = [z]
                    break
            if not zones and len(all_list) == 1:
                zones = all_list
        if not zones:
            errs = z_resp.get("errors") or []
            print(f"Не удалось найти зону {domain} в Cloudflare: {errs or 'проверьте права ключа'}")
            return 1
        zone_id = str(zones[0].get("id") or "").strip()
        domain = str(zones[0].get("name") or domain)

    print(f"Зона найдена: {domain} ({zone_id})")
    settings_to_apply = [
        ("ech", "off", "TLS ECH (Encrypted Client Hello)"),
        ("rocket_loader", "off", "Rocket Loader (совместимость со строгим CSP nonce)"),
        ("email_obfuscation", "off", "Email Obfuscation (защита целостности скриптов)"),
        ("http3", "on", "HTTP/3 (QUIC поверх UDP)"),
        ("0rtt", "on", "0-RTT Connection Resumption"),
    ]
    ech_ok = False
    for setting_id, target_val, label in settings_to_apply:
        res = _cf_api_call(
            "PATCH",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/settings/{setting_id}",
            auth_headers,
            {"id": setting_id, "value": target_val},
        )
        if res.get("success"):
            actual = (res.get("result") or {}).get("value", target_val)
            print(f"  [OK] {label}: {actual}")
            if setting_id == "ech" and actual == "off":
                ech_ok = True
        else:
            errs = res.get("errors") or []
            print(f"  [!] {label}: не удалось изменить ({errs})")

    if ech_ok:
        print("\nГотово! ECH отключён. Запись HTTPS в DNS обновится за 1–3 минуты.")
        print("Чтобы проверить сразу без ожидания кэша браузера — откройте сайт в режиме Инкогнито")
        print("или очистите DNS-кэш браузера (chrome://net-internals/#dns → Clear host cache).")
        return 0
    print("\nВнимание: параметр ECH не изменился. Попробуйте использовать Global API Key (Email + Global API Key).")
    return 1


if __name__ == "__main__":
    with contextlib.suppress(OSError):
        os.chdir(ROOT)
    _configure_stdio_utf8()
    if "--disable-cf-ech" in sys.argv[1:]:
        _idx = sys.argv.index("--disable-cf-ech")
        raise SystemExit(_disable_cloudflare_ech_cli(sys.argv[_idx + 1 :]))
    for _fast_flag in ("--stop", "--status", "--uninstall-autostart"):
        if _fast_flag in sys.argv[1:]:
            raise SystemExit(_manage_autostart_cli(_CLI_MGMT_FLAGS[_fast_flag]))
    _maybe_run_startup_auto_update()
    for _upd_flag in ("--install-autostart", "--restart"):
        if _upd_flag in sys.argv[1:]:
            raise SystemExit(_manage_autostart_cli(_CLI_MGMT_FLAGS[_upd_flag]))


@contextlib.asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    await asyncio.to_thread(_maybe_run_startup_auto_update)
    await asyncio.to_thread(_ipinfo_get_reader)
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        prev_handler = loop.get_exception_handler()

        def _win32_loop_exception_handler(lp: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
            exc = ctx.get("exception")
            if isinstance(exc, ConnectionResetError) or (
                isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054)
            ):
                return
            if prev_handler is not None:
                prev_handler(lp, ctx)
            else:
                lp.default_exception_handler(ctx)

        loop.set_exception_handler(_win32_loop_exception_handler)
    _start_analytics_flusher()
    _geo_startup_warnings()
    _start_ipinfo_updater()
    _start_drive_vods_poller()
    _start_twitch_push_poller()
    _start_health_alert_poller()
    _start_daily_tg_backup_scheduler()
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            flush_analytics_buffer()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_app_lifespan)

VALID_TYPES = {"anime", "movie", "series"}
VALID_STATUSES = {"watched", "planned", "dropped"}
VALID_GAME_STATUSES = {"completed", "playing", "dropped", "wishlist", "paused"}
VALID_SETUP_SECTIONS = {"pc", "gear"}
VALID_SETUP_ICONS = {
    "cpu", "gpu", "ram", "board", "ssd", "hdd", "cooler", "psu", "case",
    "audio", "headphones", "monitor", "keyboard", "mouse", "mic", "chair", "other",
}
VALID_WISH_CATEGORIES = {"hardware", "peripheral", "other"}
VALID_WISH_STATUSES = {"want", "saving", "bought"}
VALID_CURRENCIES = {"RUB", "USD", "EUR"}
VALID_WISH_PRIORITIES = {1, 2, 3}
ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,120}$")

# Расширения коллекций: теги, избранное, оценки и рецензии watchlist
MAX_TAGS = 12
MAX_TAG_LEN = 28
MAX_WATCHLIST_RATING = 10

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

LIST_SPECS = {
    "watchlist": {
        "legacy_file": "watchlist.json",
        "normalizer": "watchlist",
        "max_items": 500,
        "cache_ttl": 300,
    },
    "gamelist": {
        "legacy_file": "gamelist.json",
        "normalizer": "gamelist",
        "max_items": 500,
        "cache_ttl": 300,
    },
    "setup": {
        "legacy_file": "setup.json",
        "normalizer": "setup",
        "max_items": 120,
        "cache_ttl": 300,
    },
    "wishlist": {
        "legacy_file": "wishlist.json",
        "normalizer": "wishlist",
        "max_items": 500,
        "cache_ttl": 300,
    },
}

_db_lock = threading.Lock()
_list_cache_lock = threading.Lock()
_list_cache: dict[str, dict[str, Any]] = {}
_api_cache_lock = threading.Lock()
_api_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}
_twitch_token: dict[str, Any] = {}
_igdb_token: dict[str, Any] = {}


# ----------------------------- SQLite хранилище -----------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS collection_meta (
    owner TEXT NOT NULL DEFAULT 'krafen',
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (owner, name)
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    title_ru TEXT,
    year TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    poster TEXT,
    rating INTEGER NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS gamelist_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    igdb_id INTEGER,
    title TEXT NOT NULL,
    year TEXT NOT NULL,
    status TEXT NOT NULL,
    rating REAL NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT '',
    cover TEXT,
    genres_json TEXT NOT NULL DEFAULT '[]',
    platforms_json TEXT NOT NULL DEFAULT '[]',
    category INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS setup_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    item_group TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS wishlist_items (
    owner TEXT NOT NULL DEFAULT 'krafen',
    pos INTEGER NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 2,
    price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'RUB',
    note TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    favorite INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
,
    PRIMARY KEY (owner, id)
);

CREATE TABLE IF NOT EXISTS drive_vods (
    id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    size_label TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    sort_key TEXT NOT NULL,
    folder_drive_id TEXT NOT NULL DEFAULT '',
    chat_drive_id TEXT NOT NULL DEFAULT '',
    thumb_drive_id TEXT NOT NULL DEFAULT '',
    meta_drive_id TEXT NOT NULL DEFAULT '',
    transcript_drive_id TEXT NOT NULL DEFAULT '',
    game TEXT NOT NULL DEFAULT '',
    duration_s INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ext_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    title_ru TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '-',
    media_type TEXT NOT NULL,
    image TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
"""


@contextlib.contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def auth_db_connect():
    is_new = not AUTH_DB_PATH.exists()
    conn = sqlite3.connect(AUTH_DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if is_new:
        try:
            os.chmod(AUTH_DB_PATH, 0o600)
        except OSError:
            pass
    try:
        yield conn
    finally:
        conn.close()


def _normalize_legacy_list(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"revision": 0, "items": value}
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        rev = value.get("revision")
        if not isinstance(rev, int) or rev < 0:
            rev = 0
        return {"revision": rev, "items": value["items"]}
    return {"revision": 0, "items": []}


def _load_tags(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(t) for t in value if isinstance(t, str) and t.strip()][:MAX_TAGS]


def _select_collection_items(conn: sqlite3.Connection, name: str, owner: str = DEFAULT_PROFILE) -> list[dict[str, Any]]:
    if name == "watchlist":
        rows = conn.execute("SELECT * FROM watchlist_items WHERE owner = ? ORDER BY pos ASC", (owner,)).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "titleRu": r["title_ru"] or None,
                "year": r["year"],
                "type": r["type"],
                "status": r["status"],
                "poster": r["poster"] or None,
                "rating": int(r["rating"] or 0),
                "review": r["review"] or "",
                "tags": _load_tags(r["tags_json"]),
                "favorite": bool(r["favorite"]),
                "addedAt": int(r["added_at"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in rows
        ]
    if name == "gamelist":
        rows = conn.execute("SELECT * FROM gamelist_items WHERE owner = ? ORDER BY pos ASC", (owner,)).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            try:
                genres = json.loads(r["genres_json"])
            except Exception:
                genres = []
            try:
                platforms = json.loads(r["platforms_json"])
            except Exception:
                platforms = []
            items.append(
                {
                    "id": r["id"],
                    "igdbId": r["igdb_id"],
                    "title": r["title"],
                    "year": r["year"],
                    "status": r["status"],
                    "rating": float(r["rating"]),
                    "review": r["review"] or "",
                    "cover": r["cover"] or None,
                    "genres": genres if isinstance(genres, list) else [],
                    "platforms": platforms if isinstance(platforms, list) else [],
                    "category": int(r["category"] or 0),
                    "tags": _load_tags(r["tags_json"]),
                    "favorite": bool(r["favorite"]),
                    "addedAt": int(r["added_at"]),
                    "updatedAt": int(r["updated_at"]),
                }
            )
        return items
    if name == "setup":
        rows = conn.execute("SELECT * FROM setup_items WHERE owner = ? ORDER BY pos ASC", (owner,)).fetchall()
        return [
            {
                "id": r["id"],
                "group": r["item_group"],
                "category": r["category"],
                "name": r["name"],
                "icon": r["icon"],
                "note": r["note"] or "",
                "link": r["link"] or "",
                "sort": int(r["sort_order"]),
                "addedAt": int(r["added_at"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in rows
        ]
    if name == "wishlist":
        rows = conn.execute("SELECT * FROM wishlist_items WHERE owner = ? ORDER BY pos ASC", (owner,)).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "category": r["category"],
                "status": r["status"],
                "priority": int(r["priority"]),
                "price": float(r["price"]) if float(r["price"] or 0) > 0 else 0,
                "currency": r["currency"],
                "note": r["note"] or "",
                "link": r["link"] or "",
                "image": r["image"] or "",
                "tags": _load_tags(r["tags_json"]),
                "favorite": bool(r["favorite"]),
                "addedAt": int(r["added_at"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in rows
        ]
    return []


def _write_collection_items(conn: sqlite3.Connection, name: str, items: list[dict[str, Any]], owner: str = DEFAULT_PROFILE) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in items:
        iid = str(it.get("id") or "")
        if not iid or iid in seen_ids:
            continue
        seen_ids.add(iid)
        deduped.append(it)

    if name == "watchlist":
        conn.execute("DELETE FROM watchlist_items WHERE owner = ?", (owner,))
        conn.executemany(
            """
            INSERT INTO watchlist_items (
                owner, pos, id, title, title_ru, year, type, status, poster,
                rating, review, tags_json, favorite, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    owner,
                    pos,
                    it["id"],
                    it["title"],
                    it.get("titleRu"),
                    it.get("year") or "-",
                    it["type"],
                    it["status"],
                    it.get("poster"),
                    max(0, min(10, int(it.get("rating") or 0))),
                    it.get("review") or "",
                    json.dumps(it.get("tags") or [], ensure_ascii=False, separators=(",", ":")),
                    1 if it.get("favorite") else 0,
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    elif name == "gamelist":
        conn.execute("DELETE FROM gamelist_items WHERE owner = ?", (owner,))
        conn.executemany(
            """
            INSERT INTO gamelist_items (
                owner, pos, id, igdb_id, title, year, status, rating, review, cover,
                genres_json, platforms_json, category, tags_json, favorite, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    owner,
                    pos,
                    it["id"],
                    it.get("igdbId"),
                    it["title"],
                    it.get("year") or "-",
                    it["status"],
                    float(it.get("rating") or 0),
                    it.get("review") or "",
                    it.get("cover"),
                    json.dumps(it.get("genres") or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(it.get("platforms") or [], ensure_ascii=False, separators=(",", ":")),
                    int(it.get("category") or 0),
                    json.dumps(it.get("tags") or [], ensure_ascii=False, separators=(",", ":")),
                    1 if it.get("favorite") else 0,
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    elif name == "setup":
        conn.execute("DELETE FROM setup_items WHERE owner = ?", (owner,))
        conn.executemany(
            """
            INSERT INTO setup_items (owner, pos, id, item_group, category, name, icon, note, link, sort_order, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    owner,
                    pos,
                    it["id"],
                    it.get("group") or "pc",
                    it["category"],
                    it["name"],
                    it.get("icon") or "other",
                    it.get("note") or "",
                    it.get("link") or "",
                    int(it.get("sort") or 0),
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    elif name == "wishlist":
        conn.execute("DELETE FROM wishlist_items WHERE owner = ?", (owner,))
        conn.executemany(
            """
            INSERT INTO wishlist_items (
                owner, pos, id, title, category, status, priority, price, currency, note, link, image,
                tags_json, favorite, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    owner,
                    pos,
                    it["id"],
                    it["title"],
                    it["category"],
                    it["status"],
                    int(it.get("priority") or 2),
                    float(it.get("price") or 0),
                    it.get("currency") or "RUB",
                    it.get("note") or "",
                    it.get("link") or "",
                    it.get("image") or "",
                    json.dumps(it.get("tags") or [], ensure_ascii=False, separators=(",", ":")),
                    1 if it.get("favorite") else 0,
                    int(it.get("addedAt") or 0),
                    int(it.get("updatedAt") or 0),
                )
                for pos, it in enumerate(deduped)
            ],
        )
    return deduped


def kv_get(key: str, default: Any = None) -> Any:
    with _db_lock, auth_db_connect() as conn:
        row = conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default


def kv_set(key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    now = int(time.time())
    with _db_lock, auth_db_connect() as conn:
        conn.execute(
            """
            INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (key, payload, now),
        )


def init_auth_db() -> None:
    now = int(time.time())
    with _db_lock, auth_db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                channels_json TEXT NOT NULL DEFAULT '["krafen"]',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS revoked_sessions (
                nonce TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (now,))
        legacy_tokens = DATA_DIR / "tokens.json"
        if legacy_tokens.is_file():
            existing = conn.execute("SELECT 1 FROM kv WHERE key = 'tokens'").fetchone()
            if not existing:
                try:
                    parsed = json.loads(legacy_tokens.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        conn.execute(
                            "INSERT INTO kv (key, value_json, updated_at) VALUES ('tokens', ?, ?)",
                            (json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), now),
                        )
                except Exception:
                    pass


init_auth_db()


def resolve_secret_key() -> str:
    env_secret = os.environ.get("SECRET_KEY", "").strip()
    if env_secret:
        return env_secret
    stored = kv_get("session_secret")
    if isinstance(stored, str) and len(stored) >= 32:
        return stored
    generated = secrets.token_hex(32)
    kv_set("session_secret", generated)
    return generated


SECRET_KEY = resolve_secret_key()


def read_list(name: str, use_cache: bool = True, owner: str = DEFAULT_PROFILE) -> dict[str, Any]:
    cache_key = collection_key(owner, name)
    if use_cache:
        with _list_cache_lock:
            cached = _list_cache.get(cache_key)
            if cached and cached["ts"] > time.time() - LIST_SPECS[name]["cache_ttl"]:
                return {"revision": cached["revision"], "items": list(cached["items"])}
    with _db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT revision FROM collection_meta WHERE owner = ? AND name = ?", (owner, name)
        ).fetchone()
        rev = max(0, int(row["revision"] or 0)) if row else 0
        items = _select_collection_items(conn, name, owner)
        state = {"revision": rev, "items": items}
        with _list_cache_lock:
            _list_cache[cache_key] = {"ts": time.time(), "revision": rev, "items": list(items)}
        return state


def update_list_atomic(
    name: str, expected_revision: int, items: list[dict[str, Any]], owner: str = DEFAULT_PROFILE
) -> tuple[bool, dict[str, Any]]:
    now = int(time.time())
    cache_key = collection_key(owner, name)
    with _db_lock, db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT revision FROM collection_meta WHERE owner = ? AND name = ?", (owner, name)
            ).fetchone()
            current_rev = max(0, int(row["revision"] or 0)) if row else 0
            if expected_revision != current_rev:
                current_items = _select_collection_items(conn, name, owner)
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                state = {"revision": current_rev, "items": current_items}
                with _list_cache_lock:
                    _list_cache[cache_key] = {"ts": time.time(), "revision": current_rev, "items": list(current_items)}
                return False, state

            next_rev = current_rev + 1
            stored_items = _write_collection_items(conn, name, items, owner)
            conn.execute(
                """
                INSERT INTO collection_meta (owner, name, revision, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner, name) DO UPDATE SET
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (owner, name, next_rev, now),
            )
            conn.execute("COMMIT")
            state = {"revision": next_rev, "items": stored_items}
            with _list_cache_lock:
                _list_cache[cache_key] = {"ts": time.time(), "revision": next_rev, "items": list(stored_items)}
            return True, state
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise


def read_tokens() -> dict[str, Any]:
    val = kv_get("tokens", {})
    return val if isinstance(val, dict) else {}


def write_tokens(value: dict[str, Any]) -> None:
    kv_set("tokens", value)


# ----------------------------- утилиты и безопасность -----------------------------


def text(value: Any, max_len: int = 200) -> str:
    return str(value or "").strip()[:max_len]


def clean_url(value: Any, max_len: int = 600) -> str | None:
    url = text(value, max_len)
    if not url or not url.startswith("https://"):
        return None
    if any(ch in url for ch in ("\r", "\n", "\t", " ", "<", ">", '"', "'")):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return url


def year_from(value: Any) -> str:
    match = re.search(r"\d{4}", str(value or ""))
    return match.group(0) if match else "-"


def year_from_unix(value: Any) -> str:
    try:
        ts = int(value)
        if ts <= 0:
            return "-"
        return str(time.gmtime(ts).tm_year)
    except Exception:
        return "-"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_hmac_key(profile: str = DEFAULT_PROFILE) -> bytes:
    password = PROFILE_PASSWORDS.get(profile, ADMIN_PASSWORD)
    pwd_fp = hashlib.sha256(password.encode("utf-8")).hexdigest() if password else ""
    # Для основного профиля ключ остаётся прежним, чтобы уже выданные cookie не слетели.
    if profile == DEFAULT_PROFILE:
        return f"{SECRET_KEY}:{pwd_fp}".encode("utf-8")
    return f"{SECRET_KEY}:{profile}:{pwd_fp}".encode("utf-8")


def sign(value: str, profile: str = DEFAULT_PROFILE) -> str:
    return b64url(hmac.new(_session_hmac_key(profile), value.encode(), hashlib.sha256).digest())


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


_revoked_sessions_lock = threading.Lock()
_revoked_sessions: dict[str, int] = {}
_revoked_sessions_loaded = False


def _ensure_revoked_sessions_loaded() -> None:
    global _revoked_sessions_loaded
    if _revoked_sessions_loaded:
        return
    now = int(time.time())
    with _revoked_sessions_lock:
        if _revoked_sessions_loaded:
            return
        with contextlib.suppress(Exception):
            with _db_lock, auth_db_connect() as conn:
                rows = conn.execute(
                    "SELECT nonce, expires_at FROM revoked_sessions WHERE expires_at > ?", (now,)
                ).fetchall()
                for r in rows:
                    _revoked_sessions[str(r["nonce"])] = int(r["expires_at"])
        _revoked_sessions_loaded = True


def create_session(profile: str = DEFAULT_PROFILE) -> str:
    payload = b64url(
        json.dumps(
            {
                "exp": int(time.time()) + SESSION_TTL_SECONDS,
                "nonce": secrets.token_hex(16),
                "p": profile,
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{payload}.{sign(payload, profile)}"


def verify_session(token: str | None, profile: str = DEFAULT_PROFILE) -> bool:
    password = PROFILE_PASSWORDS.get(profile, "")
    if not token or not password or len(token) > 512:
        return False
    _ensure_revoked_sessions_loaded()
    try:
        payload, signature = token.split(".", 1)
        if not constant_time_equal(signature, sign(payload, profile)):
            return False
        data = json.loads(b64url_decode(payload))
        now = int(time.time())
        exp = int(data.get("exp", 0))
        nonce = str(data.get("nonce") or "")
        if exp <= now or not nonce:
            return False
        # Cookie другого профиля не должна давать доступ к этому.
        if str(data.get("p") or DEFAULT_PROFILE) != profile:
            return False
        with _revoked_sessions_lock:
            if nonce in _revoked_sessions:
                return False
        return True
    except Exception:
        return False


def session_needs_renewal(token: str | None, profile: str = DEFAULT_PROFILE) -> bool:
    """True, если сессия валидна, но выдана достаточно давно для продления."""
    if not token or len(token) > 512 or "." not in token:
        return False
    try:
        payload, signature = token.split(".", 1)
        if not constant_time_equal(signature, sign(payload, profile)):
            return False
        data = json.loads(b64url_decode(payload))
        exp = int(data.get("exp", 0))
        issued = exp - SESSION_TTL_SECONDS
        return (time.time() - issued) > SESSION_RENEW_AFTER_SECONDS
    except Exception:
        return False


def revoke_session(token: str | None, profile: str = DEFAULT_PROFILE) -> None:
    if not token or len(token) > 512 or "." not in token:
        return
    _ensure_revoked_sessions_loaded()
    try:
        payload, signature = token.split(".", 1)
        if not constant_time_equal(signature, sign(payload, profile)):
            return
        data = json.loads(b64url_decode(payload))
        nonce = str(data.get("nonce") or "")
        exp = int(data.get("exp", 0))
        now = int(time.time())
        if not nonce or exp <= now:
            return
        with _revoked_sessions_lock:
            expired = [k for k, v in _revoked_sessions.items() if v <= now]
            for k in expired:
                _revoked_sessions.pop(k, None)
            if len(_revoked_sessions) >= 2048:
                _revoked_sessions.pop(next(iter(_revoked_sessions)))
            _revoked_sessions[nonce] = exp
        with contextlib.suppress(Exception):
            with _db_lock, auth_db_connect() as conn:
                conn.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (now,))
                conn.execute(
                    "INSERT OR REPLACE INTO revoked_sessions (nonce, expires_at) VALUES (?, ?)",
                    (nonce, exp),
                )
    except Exception:
        return


def request_profile(request: Request) -> str:
    """Профиль, к которому относится запрос.

    Определяется по заголовку-подсказке от страницы (X-Krafen-Profile) и проверяется
    по cookie сессии этого профиля; без валидной сессии возвращается основной профиль.
    """
    hinted = str(request.headers.get("x-krafen-profile") or "").strip().lower()
    return hinted if hinted in PROFILES else DEFAULT_PROFILE


def session_cookie_name(request: Request) -> str:
    return profile_spec(request_profile(request))["cookie"]


def require_admin(request: Request, profile: str | None = None) -> None:
    profile = profile or request_profile(request)
    cookie_name = profile_spec(profile)["cookie"]
    if not verify_session(request.cookies.get(cookie_name), profile):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


_CLOUDFLARE_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _parse_extra_trusted_nets(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not raw or raw in {"0", "1", "true", "false", "yes", "no", "on", "off", "auto"}:
        return ()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        with contextlib.suppress(ValueError):
            nets.append(ipaddress.ip_network(item, strict=False))
    return tuple(nets)


_EXTRA_TRUSTED_PROXY_NETS = _parse_extra_trusted_nets(TRUST_PROXY_MODE)


def _is_trusted_proxy_peer(request: Request) -> bool:
    if TRUST_PROXY_MODE in {"0", "false", "no", "off"}:
        return False
    if TRUST_PROXY_MODE in {"1", "true", "yes", "on"}:
        return True
    peer = request.client.host if request.client else ""
    if not peer:
        return False
    if peer in {"testclient", "localhost"}:
        return True
    try:
        ip = ipaddress.ip_address(peer)
        if _EXTRA_TRUSTED_PROXY_NETS:
            return any(ip in net for net in _EXTRA_TRUSTED_PROXY_NETS) or ip.is_loopback
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
        return any(ip in net for net in _CLOUDFLARE_NETS)
    except ValueError:
        return False


_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}(?::\d{1,5})?$")


def _effective_request_host(request: Request) -> str:
    candidates: list[str] = []
    if _is_trusted_proxy_peer(request):
        xfh = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        if xfh:
            candidates.append(xfh)
    raw_host = (request.headers.get("host") or request.url.netloc or "").split(",")[0].strip()
    if raw_host:
        candidates.append(raw_host)
    for h in candidates:
        if _SAFE_HOST_RE.fullmatch(h):
            return h.lower()
    return f"localhost:{PORT}"


def _is_e2b_preview_request(request: Request) -> bool:
    if not ALLOW_E2B_PREVIEW:
        return False
    host_only = _effective_request_host(request).split(":", 1)[0]
    return host_only.endswith(".e2b.app")


def _is_cloudflare_peer(request: Request) -> bool:
    if TRUST_PROXY_MODE in {"0", "false", "no", "off"}:
        return False
    peer = request.client.host if request.client else ""
    if not peer:
        return False
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(peer)
        return any(ip in net for net in _CLOUDFLARE_NETS)
    return False


def is_https_request(request: Request) -> bool:
    if _is_trusted_proxy_peer(request):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"


def require_same_origin(request: Request) -> None:
    """Защита от CSRF для state-changing запросов."""
    allow_e2b_iframe = _is_e2b_preview_request(request)
    sec_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_site and sec_site not in ("same-origin", "none") and not allow_e2b_iframe:
        raise HTTPException(status_code=403, detail={"error": "csrf_blocked"})

    origin = (request.headers.get("origin") or "").strip()
    if origin == "null":
        raise HTTPException(status_code=403, detail={"error": "csrf_blocked"})
    if origin:
        parsed_origin = urlsplit(origin)
        origin_host = (parsed_origin.netloc or "").strip().lower()
        allowed_hosts = {_effective_request_host(request)}
        if PUBLIC_ORIGIN:
            pub_host = (urlsplit(PUBLIC_ORIGIN).netloc or "").strip().lower()
            if pub_host:
                allowed_hosts.add(pub_host)
        if not origin_host or (origin_host not in allowed_hosts and not allow_e2b_iframe):
            raise HTTPException(status_code=403, detail={"error": "csrf_blocked"})
    elif sec_site != "same-origin" and not allow_e2b_iframe:
        raise HTTPException(status_code=403, detail={"error": "csrf_blocked"})


def cookie_header(request: Request, name: str, value: str, max_age: int, path: str = "/") -> str:
    https = is_https_request(request)
    if SECURE_COOKIES_MODE == "1":
        secure = True
    elif SECURE_COOKIES_MODE == "0":
        secure = False
    else:
        secure = https

    in_iframe = request.headers.get("sec-fetch-dest", "").lower() == "iframe"
    same_site = "None" if (in_iframe and secure and _is_e2b_preview_request(request)) else "Lax"

    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "HttpOnly", f"SameSite={same_site}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def client_ip(request: Request) -> str:
    if _is_trusted_proxy_peer(request):
        forwarded = ""
        if _is_cloudflare_peer(request):
            forwarded = request.headers.get("cf-connecting-ip") or ""
        if not forwarded:
            forwarded = (
                request.headers.get("x-real-ip")
                or request.headers.get("x-forwarded-for")
                or ""
            )
        if forwarded:
            cand = forwarded.split(",")[0].strip()[:80]
            with contextlib.suppress(ValueError):
                return str(ipaddress.ip_address(cand))
    return request.client.host if request.client else "unknown"


_MAX_RATE_BUCKETS = 4096


def _prune_rate_buckets_locked(now: float, max_age: float = 300.0) -> None:
    if len(_rate_buckets) > 2048:
        cutoff = now - max_age
        stale = [k for k, v in _rate_buckets.items() if not v or v[-1] <= cutoff]
        for k in stale:
            _rate_buckets.pop(k, None)
    if len(_rate_buckets) >= _MAX_RATE_BUCKETS:
        # Защита памяти при массовом переборе ключей: вытесняем самые старые корзины
        by_age = sorted(_rate_buckets.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
        for k, _ in by_age[:1024]:
            _rate_buckets.pop(k, None)


def rate_limit(request: Request, name: str, limit: int = 60, period: int = 60) -> None:
    key = f"{name}:{client_ip(request)}"
    now = time.time()
    with _rate_lock:
        _prune_rate_buckets_locked(now, max(period, 300))
        bucket = [ts for ts in _rate_buckets.get(key, []) if ts > now - period]
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail={"error": "rate_limited"})
        bucket.append(now)
        _rate_buckets[key] = bucket


def _check_admin_failed_login_guard(request: Request, profile: str = DEFAULT_PROFILE) -> None:
    # Ограничение только по IP и профилю: злоумышленник с разных IP не блокирует вход владельцу.
    ip_key = f"admin_fail_ip:{profile}:{client_ip(request)}"
    now = time.time()
    with _rate_lock:
        ip_fails = [ts for ts in _rate_buckets.get(ip_key, []) if ts > now - 300]
        if len(ip_fails) >= 8:
            raise HTTPException(status_code=429, detail={"error": "rate_limited"})


def _record_admin_failed_login(request: Request, profile: str = DEFAULT_PROFILE) -> None:
    ip_key = f"admin_fail_ip:{profile}:{client_ip(request)}"
    now = time.time()
    with _rate_lock:
        _prune_rate_buckets_locked(now, 300)
        bucket = [ts for ts in _rate_buckets.get(ip_key, []) if ts > now - 300]
        bucket.append(now)
        _rate_buckets[ip_key] = bucket


def j(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


def cached_api_get(key: str) -> tuple[int, dict[str, Any]] | None:
    now = time.time()
    with _api_cache_lock:
        entry = _api_cache.get(key)
        if entry and entry[0] > now:
            return entry[1], entry[2]
    return None


def cached_api_set(key: str, ttl: float, status: int, payload: dict[str, Any]) -> None:
    now = time.time()
    with _api_cache_lock:
        if len(_api_cache) > 512:
            expired = [k for k, v in _api_cache.items() if v[0] <= now]
            for k in expired:
                _api_cache.pop(k, None)
        _api_cache[key] = (now + ttl, status, payload)


# ----------------------------- нормализация списков -----------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clean_string_array(value: Any, max_items: int, max_text: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text(item, max_text) for item in value if text(item, max_text)][:max_items]


def clean_tags(value: Any, max_items: int = MAX_TAGS, max_len: int = MAX_TAG_LEN) -> list[str]:
    """Теги: обрезаем, схлопываем пробелы, убираем дубли без учёта регистра.

    Принимаем только строки: числа, вложенные массивы и объекты из JSON отбрасываются,
    чтобы в коллекцию не попадали артефакты вроде "['x']".
    """
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = " ".join(raw.split())[:max_len].strip()
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= max_items:
            break
    return result


def normalize_watchlist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    item_type = text(item.get("type"), 20)
    status = text(item.get("status"), 20)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or item_type not in VALID_TYPES or status not in VALID_STATUSES:
        return None
    review = text(item.get("review"), 5000)
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "title": title,
        "titleRu": text(item.get("titleRu"), 180) or None,
        "year": text(item.get("year"), 24) or "-",
        "type": item_type,
        "status": status,
        "poster": clean_url(item.get("poster")),
        "rating": max(0, min(MAX_WATCHLIST_RATING, _safe_int(item.get("rating"), 0))),
        "review": review,
        "tags": clean_tags(item.get("tags")),
        "favorite": bool(item.get("favorite")),
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


def normalize_gamelist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    status = text(item.get("status"), 20)
    try:
        rating = max(0.0, min(10.0, float(item.get("rating") or 0)))
    except Exception:
        rating = 0.0
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or status not in VALID_GAME_STATUSES:
        return None
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "igdbId": int(item.get("igdbId")) if str(item.get("igdbId") or "").isdigit() else None,
        "title": title,
        "year": text(item.get("year"), 24) or "-",
        "status": status,
        "rating": round(rating * 10) / 10,
        "review": text(item.get("review"), 5000),
        "cover": clean_url(item.get("cover")),
        "genres": clean_string_array(item.get("genres"), 5, 32),
        "platforms": clean_string_array(item.get("platforms"), 8, 32),
        "category": max(0, _safe_int(item.get("category"), 0)),
        "tags": clean_tags(item.get("tags")),
        "favorite": bool(item.get("favorite")),
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


def normalize_setup_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    group = text(item.get("group") or item.get("section"), 20)
    category = text(item.get("category") or item.get("slot"), 80)
    name = text(item.get("name") or item.get("title"), 180)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or group not in VALID_SETUP_SECTIONS or not category or not name:
        return None
    icon = text(item.get("icon"), 20) or "other"
    raw_sort = item.get("sort") if item.get("sort") is not None else item.get("order")
    try:
        sort_value = max(0, min(999, round(float(raw_sort))))
    except Exception:
        sort_value = 0
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "group": group,
        "category": category,
        "name": name,
        "icon": icon if icon in VALID_SETUP_ICONS else "other",
        "note": text(item.get("note"), 300) or "",
        "link": clean_url(item.get("link")) or "",
        "sort": sort_value,
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


def normalize_wishlist_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = text(item.get("id"), 120)
    title = text(item.get("title"), 180)
    category = text(item.get("category"), 20)
    status = text(item.get("status"), 20)
    if not ITEM_ID_PATTERN.fullmatch(item_id) or not title or category not in VALID_WISH_CATEGORIES or status not in VALID_WISH_STATUSES:
        return None
    currency = text(item.get("currency"), 8).upper() or "RUB"
    try:
        price = float(item.get("price"))
        price = min(1_000_000_000, round(price * 100) / 100) if price > 0 else 0
    except Exception:
        price = 0
    try:
        priority = int(item.get("priority"))
    except Exception:
        priority = 2
    now = int(time.time() * 1000)
    return {
        "id": item_id,
        "title": title,
        "category": category,
        "status": status,
        "priority": priority if priority in VALID_WISH_PRIORITIES else 2,
        "price": price,
        "currency": currency if currency in VALID_CURRENCIES else "RUB",
        "note": text(item.get("note"), 2000) or "",
        "link": clean_url(item.get("link")) or "",
        "image": clean_url(item.get("image")) or "",
        "tags": clean_tags(item.get("tags")),
        "favorite": bool(item.get("favorite")),
        "addedAt": _safe_int(item.get("addedAt"), now) or now,
        "updatedAt": _safe_int(item.get("updatedAt"), now) or now,
    }


NORMALIZERS = {
    "watchlist": normalize_watchlist_item,
    "gamelist": normalize_gamelist_item,
    "setup": normalize_setup_item,
    "wishlist": normalize_wishlist_item,
}


DEFAULT_DRIVE_VODS = [
    ("2026-09-03_17-56", "1_SRlYEQ9E0hF1dWmoS3segy5qSAV7rG3", "мама в 16 (я) ｜ !конкурс ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "7.21 ГБ", "", "2026-09-03_17-56"),
    ("2026-09-02_12-15", "1td-vOnVhyvsT6VjzROhhdIV7asxvJImR", "ну допустим мяу ｜ !конкурс ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "12.32 ГБ", "", "2026-09-02_12-15"),
    ("2026-08-31_13-44", "1u8ZrnJPIASjUEI5EeYS1v_X7C63YnDeJ", "ну допустим мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "7.48 ГБ", "", "2026-08-31_13-44"),
    ("2026-08-30_13-04", "1igadvmUfhI_XOjcmGsE_2rMaxYB7_4Be", "мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "1.84 ГБ", "", "2026-08-30_13-04"),
    ("2026-08-30_01-01", "1mLVCiVyC6yBVkX-E3YJafLY7EJhGeu-k", "мяу ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "3.65 ГБ", "", "2026-08-30_01-01"),
    ("2026-08-27_17-23", "1KyokELlAV_-f703pnSjEtRpaKyWDH2mk", "щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "7.5 ГБ", "", "2026-08-27_17-23"),
    ("2026-08-25_14-52", "13maPLTBIWghOtGDDhdtG5iY9MUFdeXeE", "щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "13.25 ГБ", "эта запись лагающая — не исправить", "2026-08-25_14-52"),
    ("2026-08-23_11-50", "1QPUp9NYaHEdvS8P-cndGpks1LivR-YkP", "щавелевый персик ｜ 2К ДОНАТ И ОФФАЮ КОМПЬЮТЕР ✌ (БЕЗ ШУТОК) ｜ биб боб", "17.27 ГБ", "", "2026-08-23_11-50"),
]


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    """Мягкая миграция: добавляет недостающие колонки в существующую таблицу."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_ddl in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")


# Колонки, появившиеся позже базовой схемы (оценки, рецензии, теги, избранное)
_COLLECTION_EXTRA_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "watchlist_items": (
        ("rating", "INTEGER NOT NULL DEFAULT 0"),
        ("review", "TEXT NOT NULL DEFAULT ''"),
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "gamelist_items": (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "wishlist_items": (
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ),
}


_COLLECTION_TABLES = ("watchlist_items", "gamelist_items", "setup_items", "wishlist_items")


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _migrate_collections_to_owners(conn: sqlite3.Connection) -> None:
    """Переводит коллекции на владельца (owner) с составным первичным ключом.

    Старые таблицы имели id как PRIMARY KEY, из-за чего одинаковый id не мог
    существовать у двух профилей. Здесь таблица пересобирается: данные остаются
    за профилем krafen, id/порядок/все поля сохраняются как есть.
    """
    changed = False

    meta_cols = {r["name"] for r in _table_columns(conn, "collection_meta")}
    if "owner" not in meta_cols:
        conn.execute("ALTER TABLE collection_meta RENAME TO collection_meta_old")
        conn.execute(
            """
            CREATE TABLE collection_meta (
                owner TEXT NOT NULL DEFAULT 'krafen',
                name TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (owner, name)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO collection_meta (owner, name, revision, updated_at)
            SELECT 'krafen', name, revision, updated_at FROM collection_meta_old
            """
        )
        conn.execute("DROP TABLE collection_meta_old")
        changed = True

    for table in _COLLECTION_TABLES:
        cols = _table_columns(conn, table)
        names = [r["name"] for r in cols]
        if not names or "owner" in names:
            continue

        # Восстанавливаем DDL из схемы и убираем owner/PK, затем копируем данные.
        schema_sql = SCHEMA_SQL
        start = schema_sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        end = schema_sql.index(");", start) + 2
        new_ddl = schema_sql[start:end].replace("IF NOT EXISTS ", "", 1).replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}_new", 1)

        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(new_ddl)
        old_cols = [n for n in names]
        col_list = ", ".join(f'"{c}"' for c in old_cols)
        conn.execute(
            f"INSERT INTO {table}_new (owner, {col_list}) SELECT 'krafen', {col_list} FROM {table}_old"
        )
        conn.execute(f"DROP TABLE {table}_old")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        changed = True

    return changed


def init_db() -> None:
    now = int(time.time())
    with _db_lock, db_connect() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_collections_to_owners(conn)
        for table, columns in _COLLECTION_EXTRA_COLUMNS.items():
            _ensure_columns(conn, table, columns)
        _ensure_columns(conn, "suggestions", [("link", "TEXT NOT NULL DEFAULT ''")])

        existing_vod_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(drive_vods)").fetchall()
        }
        for col_name, col_ddl in (
            ("folder_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("chat_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("thumb_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("meta_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("transcript_drive_id", "TEXT NOT NULL DEFAULT ''"),
            ("game", "TEXT NOT NULL DEFAULT ''"),
            ("duration_s", "INTEGER NOT NULL DEFAULT 0"),
            ("started_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col_name not in existing_vod_cols:
                conn.execute(f"ALTER TABLE drive_vods ADD COLUMN {col_name} {col_ddl}")

        meta_count = conn.execute("SELECT COUNT(*) AS cnt FROM collection_meta").fetchone()["cnt"]
        has_legacy_json = any((DATA_DIR / spec["legacy_file"]).is_file() for spec in LIST_SPECS.values())

        if meta_count == 0 and not has_legacy_json and SEED_SQL_PATH.is_file():
            conn.executescript(SEED_SQL_PATH.read_text(encoding="utf-8"))

        drive_count = conn.execute("SELECT COUNT(*) AS cnt FROM drive_vods").fetchone()["cnt"]
        if drive_count == 0 and GDRIVE_FOLDER_ID == "1e417_q9s6SjcZHyqMYzbVs8OHqfY5Ifk":
            conn.executemany(
                "INSERT OR IGNORE INTO drive_vods (id, drive_id, title, size_label, note, sort_key) VALUES (?, ?, ?, ?, ?, ?)",
                DEFAULT_DRIVE_VODS,
            )

        for name, spec in LIST_SPECS.items():
            row = conn.execute(
                "SELECT 1 FROM collection_meta WHERE owner = ? AND name = ?", (DEFAULT_PROFILE, name)
            ).fetchone()
            if row:
                continue
            legacy_path = DATA_DIR / spec["legacy_file"]
            initial_rev = 0
            normalized_items: list[dict[str, Any]] = []
            if legacy_path.is_file():
                try:
                    raw = _normalize_legacy_list(json.loads(legacy_path.read_text(encoding="utf-8")))
                    initial_rev = raw["revision"]
                    norm_fn = NORMALIZERS[spec["normalizer"]]
                    for raw_item in raw["items"]:
                        clean = norm_fn(raw_item)
                        if clean is not None:
                            normalized_items.append(clean)
                except Exception:
                    initial_rev = 0
                    normalized_items = []
            conn.execute("BEGIN IMMEDIATE")
            try:
                _write_collection_items(conn, name, normalized_items[: spec["max_items"]], DEFAULT_PROFILE)
                conn.execute(
                    "INSERT INTO collection_meta (owner, name, revision, updated_at) VALUES (?, ?, ?, ?)",
                    (DEFAULT_PROFILE, name, initial_rev, now),
                )
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise

        # У каждого профиля свой набор коллекций, начиная с пустых.
        for owner in PROFILES:
            for name in LIST_SPECS:
                exists = conn.execute(
                    "SELECT 1 FROM collection_meta WHERE owner = ? AND name = ?", (owner, name)
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO collection_meta (owner, name, revision, updated_at) VALUES (?, ?, 0, ?)",
                    (owner, name, now),
                )


init_db()


# ----------------------------- Телеметрия и статистика (analytics.db) -----------------------------

ANALYTICS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_traffic (
    day TEXT PRIMARY KEY,
    views INTEGER NOT NULL DEFAULT 0,
    api_hits INTEGER NOT NULL DEFAULT 0,
    bot_hits INTEGER NOT NULL DEFAULT 0,
    vod_plays INTEGER NOT NULL DEFAULT 0,
    vod_downloads INTEGER NOT NULL DEFAULT 0,
    bytes_sent INTEGER NOT NULL DEFAULT 0,
    status_2xx INTEGER NOT NULL DEFAULT 0,
    status_3xx INTEGER NOT NULL DEFAULT 0,
    status_4xx INTEGER NOT NULL DEFAULT 0,
    status_5xx INTEGER NOT NULL DEFAULT 0,
    blocked_hits INTEGER NOT NULL DEFAULT 0,
    latency_sum_ms REAL NOT NULL DEFAULT 0,
    req_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hourly_traffic (
    hour_bucket INTEGER PRIMARY KEY,
    views INTEGER NOT NULL DEFAULT 0,
    api_hits INTEGER NOT NULL DEFAULT 0,
    vod_plays INTEGER NOT NULL DEFAULT 0,
    bytes_sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_visitors (
    day TEXT NOT NULL,
    vhash TEXT NOT NULL,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, vhash)
);

CREATE TABLE IF NOT EXISTS hourly_visitors (
    hour_bucket INTEGER NOT NULL,
    vhash TEXT NOT NULL,
    PRIMARY KEY (hour_bucket, vhash)
);

CREATE TABLE IF NOT EXISTS page_stats (
    day TEXT NOT NULL,
    page TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, page)
);

CREATE TABLE IF NOT EXISTS referrer_stats (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);

CREATE TABLE IF NOT EXISTS device_stats (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind, name)
);

CREATE TABLE IF NOT EXISTS vod_analytics (
    vod_id TEXT PRIMARY KEY,
    plays INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    chat_loads INTEGER NOT NULL DEFAULT 0,
    bytes_streamed INTEGER NOT NULL DEFAULT 0,
    last_played_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    ip TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""

_analytics_db_lock = threading.Lock()
_analytics_buf_lock = threading.Lock()
_analytics_flusher_started = False
_analytics_last_prune_ts = 0.0

# Страницы, которые не должны попадать в поиск: панель, служебные разделы.
# Отдаются с X-Robots-Tag: noindex; сканирование /tasks разрешено в robots.txt,
# чтобы поисковые роботы могли увидеть этот заголовок.
_NOINDEX_PAGES = {"admin.html", "tasks.html", "404.html"}

_TRACKED_PAGES: dict[str, tuple[str, str]] = {
    "/": ("/", "Главная"),
    "/index.html": ("/", "Главная"),
    "/watchlist": ("/watchlist", "Watchlist"),
    "/watchlist.html": ("/watchlist", "Watchlist"),
    "/gamelist": ("/gamelist", "Gamelist"),
    "/gamelist.html": ("/gamelist", "Gamelist"),
    "/setup": ("/setup", "Setup"),
    "/setup.html": ("/setup", "Setup"),
    "/wishlist": ("/wishlist", "Wishlist"),
    "/wishlist.html": ("/wishlist", "Wishlist"),
    "/wrapped": ("/wrapped", "Wrapped"),
    "/wrapped.html": ("/wrapped", "Wrapped"),
    "/tasks": ("/tasks", "Tasks"),
    "/tasks.html": ("/tasks", "Tasks"),
    "/danonsha": ("/danonsha", "danonsha"),
    "/danonsha.html": ("/danonsha", "danonsha"),
}

_BOT_UA_RE = re.compile(
    r"(bot|spider|crawl|slurp|mediapartners|curl/|wget/|python-requests|httpx|aiohttp|"
    r"headlesschrome|lighthouse|uptimerobot|pingdom|statuscake|go-http-client|facebookexternalhit|"
    r"discordbot|telegrambot|twitterbot|whatsapp|yandexbot|googlebot|bingbot|applebot|semrush|ahrefs)",
    flags=re.IGNORECASE,
)

_COUNTRY_LABELS = {
    "RU": "Россия",
    "BY": "Беларусь",
    "KZ": "Казахстан",
    "UA": "Украина",
    "DE": "Германия",
    "NL": "Нидерланды",
    "FI": "Финляндия",
    "US": "США",
    "GB": "Великобритания",
    "PL": "Польша",
    "FR": "Франция",
    "TR": "Турция",
    "AM": "Армения",
    "GE": "Грузия",
    "UZ": "Узбекистан",
    "EE": "Эстония",
    "LV": "Латвия",
    "LT": "Литва",
    "SE": "Швеция",
}

# In-memory агрегационные буферы до сброса в SQLite
_buf_daily: dict[str, list[float]] = {}
_buf_hourly: dict[int, list[int]] = {}
_buf_visitors: dict[tuple[str, str], list[int]] = {}
_buf_hourly_visitors: set[tuple[int, str]] = set()
_buf_pages: dict[tuple[str, str], int] = {}
_buf_referrers: dict[tuple[str, str], int] = {}
_buf_devices: dict[tuple[str, str, str], int] = {}
_buf_vods: dict[str, list[int]] = {}
_active_visitors: dict[str, tuple[float, str]] = {}
_recent_vod_events: dict[tuple[str, str, str], float] = {}


@contextlib.contextmanager
def analytics_db_connect():
    is_new = not ANALYTICS_DB_PATH.exists()
    conn = sqlite3.connect(ANALYTICS_DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if is_new:
        with contextlib.suppress(OSError):
            os.chmod(ANALYTICS_DB_PATH, 0o600)
    try:
        yield conn
    finally:
        conn.close()


def init_analytics_db() -> None:
    with _analytics_db_lock, analytics_db_connect() as conn:
        conn.executescript(ANALYTICS_SCHEMA_SQL)


with contextlib.suppress(Exception):
    init_analytics_db()


def read_vod_overrides() -> dict[str, dict[str, Any]]:
    val = kv_get("vod_overrides", {})
    return val if isinstance(val, dict) else {}


def write_vod_overrides(overrides: dict[str, dict[str, Any]]) -> None:
    kv_set("vod_overrides", overrides)


def _visitor_daily_hash(day: str, ip: str, ua: str) -> str:
    salt = SECRET_KEY.encode("utf-8")
    msg = f"{day}|{ip}|{ua[:160]}".encode("utf-8", errors="replace")
    return hmac.new(salt, msg, hashlib.sha256).hexdigest()[:20]


def _classify_user_agent(ua: str) -> tuple[str, str, str]:
    u = ua.lower()
    if any(k in u for k in ("ipad", "tablet", "playbook", "silk")):
        device = "Планшет"
    elif any(k in u for k in ("mobi", "iphone", "ipod", "android")):
        device = "Смартфон"
    else:
        device = "ПК"

    if "telegram" in u:
        browser = "Telegram"
    elif "yabrowser" in u or "yandex" in u:
        browser = "Яндекс"
    elif "edg/" in u or "edga/" in u or "edgios/" in u:
        browser = "Edge"
    elif "opr/" in u or "opera" in u:
        browser = "Opera"
    elif "firefox/" in u or "fxios/" in u:
        browser = "Firefox"
    elif "chrome/" in u or "crios/" in u:
        browser = "Chrome"
    elif "safari/" in u:
        browser = "Safari"
    else:
        browser = "Другой"

    if "windows" in u:
        os_name = "Windows"
    elif "android" in u:
        os_name = "Android"
    elif "iphone" in u or "ipad" in u or "cpu os" in u:
        os_name = "iOS"
    elif "mac os x" in u or "macintosh" in u:
        os_name = "macOS"
    elif "linux" in u:
        os_name = "Linux"
    else:
        os_name = "Другая"
    return device, browser, os_name


def _classify_referrer(ref: str, self_host: str) -> str:
    raw = (ref or "").strip()
    if not raw:
        return "Прямой заход"
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return "Прямой заход"
    if not host:
        return "Прямой заход"
    clean_self = (self_host or "").split(":", 1)[0].lower()
    if host == clean_self or (PUBLIC_ORIGIN and host == (urlsplit(PUBLIC_ORIGIN).hostname or "").lower()):
        return ""
    if host.startswith("www."):
        host = host[4:]
    if host in ("t.me", "telegram.me", "telegram.org") or host.endswith(".t.me"):
        return "Telegram"
    if "twitch.tv" in host:
        return "Twitch"
    if "discord.com" in host or "discord.gg" in host:
        return "Discord"
    if "youtube.com" in host or host == "youtu.be":
        return "YouTube"
    if "yandex." in host or host == "ya.ru":
        return "Яндекс"
    if "google." in host:
        return "Google"
    if host in ("vk.com", "vk.ru"):
        return "VK"
    if "github.com" in host:
        return "GitHub"
    if "boosty.to" in host:
        return "Boosty"
    return host[:60]


# --- География посетителей без Cloudflare ----------------------------------
# Основной источник: IPinfo Lite (MMDB, обновляется ежедневно), нужен IPINFO_TOKEN.
# Резерв (и работа без токена): встроенная база geoip2fast.
# Лицензия IPinfo Lite: CC BY-SA 4.0, см. README.
try:  # необязательная зависимость: без неё используется только резерв
    import maxminddb as _maxminddb
except ImportError:  # pragma: no cover
    _maxminddb = None
try:  # необязательная зависимость: без неё страна определяется только через Cloudflare
    from geoip2fast import GeoIP2Fast as _GeoIP2Fast
except ImportError:  # pragma: no cover
    _GeoIP2Fast = None

IPINFO_TOKEN = (os.environ.get("IPINFO_TOKEN") or "").strip()
IPINFO_DB_PATH = DATA_DIR / "ipinfo_lite.mmdb"
IPINFO_URL_TEMPLATE = "https://ipinfo.io/data/ipinfo_lite.mmdb?token={token}"
IPINFO_MAX_AGE_SEC = 24 * 3600
IPINFO_CHECK_SEC = 3600
_ipinfo_reader: Any = None
_ipinfo_reader_mtime = 0.0
_ipinfo_lock = threading.Lock()
_ipinfo_updater_started = False
_geoip_db: Any = None
_geoip_lock = threading.Lock()


_geo_stats_lock = threading.Lock()
_geo_source_counts: dict[str, int] = {}
_ipinfo_last: dict[str, Any] = {"attempt_ts": 0.0, "ok": None, "error": ""}
# Счётчики для диагностики: откуда пришёл адрес посетителя (без самих адресов)
_geo_conn_counts: dict[str, int] = {
    "total": 0, "trusted_peer": 0, "public_untrusted_peer": 0, "private_untrusted_peer": 0,
    "hdr_x_real_ip": 0, "hdr_x_forwarded_for": 0, "hdr_cf_connecting_ip": 0, "hdr_cf_ipcountry": 0,
}


def _geo_mask(message: str) -> str:
    if IPINFO_TOKEN:
        message = message.replace(IPINFO_TOKEN, "***")
    return message


def _geo_log(message: str) -> None:
    print(f"[geo] {_geo_mask(message)}", flush=True)


def _geo_count(source: str) -> None:
    with _geo_stats_lock:
        _geo_source_counts[source] = _geo_source_counts.get(source, 0) + 1


def _geo_note_connection(request: Request) -> None:
    trusted = _is_trusted_proxy_peer(request)
    peer = request.client.host if request.client else ""
    try:
        peer_public = not ipaddress.ip_address(peer).is_private and not ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_public = False
    with _geo_stats_lock:
        c = _geo_conn_counts
        c["total"] += 1
        if trusted:
            c["trusted_peer"] += 1
        elif peer_public:
            c["public_untrusted_peer"] += 1
        else:
            c["private_untrusted_peer"] += 1
        for key, header in (
            ("hdr_x_real_ip", "x-real-ip"),
            ("hdr_x_forwarded_for", "x-forwarded-for"),
            ("hdr_cf_connecting_ip", "cf-connecting-ip"),
            ("hdr_cf_ipcountry", "cf-ipcountry"),
        ):
            if request.headers.get(header):
                c[key] += 1


def geo_status() -> dict[str, Any]:
    """Состояние географии для админки. Токен не возвращается, только его наличие."""
    try:
        st = IPINFO_DB_PATH.stat()
        db_exists, db_bytes, db_mtime = True, st.st_size, int(st.st_mtime)
    except OSError:
        db_exists, db_bytes, db_mtime = False, 0, 0
    with _geo_stats_lock:
        counts = dict(_geo_source_counts)
        conn = dict(_geo_conn_counts)
    return {
        "maxminddb_installed": _maxminddb is not None,
        "geoip2fast_installed": _GeoIP2Fast is not None,
        "ipinfo_token_set": bool(IPINFO_TOKEN),
        "ipinfo_db_exists": db_exists,
        "ipinfo_db_bytes": db_bytes,
        "ipinfo_db_updated_ts": db_mtime,
        "ipinfo_last_attempt_ts": int(_ipinfo_last["attempt_ts"] or 0),
        "ipinfo_last_ok": _ipinfo_last["ok"],
        "ipinfo_last_error": _ipinfo_last["error"],
        "sources": counts,
        "connections": conn,
    }


def _geo_startup_warnings() -> None:
    if _maxminddb is None:
        _geo_log("не установлен maxminddb: выполните pip install -r requirements.txt")
    if _GeoIP2Fast is None:
        _geo_log("не установлен geoip2fast: выполните pip install -r requirements.txt")
    if not IPINFO_TOKEN:
        _geo_log("IPINFO_TOKEN не задан: база IPinfo Lite не загружается, используется резерв")


def _ipinfo_get_reader() -> Any:
    global _ipinfo_reader, _ipinfo_reader_mtime
    if _maxminddb is None:
        return None
    try:
        mtime = IPINFO_DB_PATH.stat().st_mtime
    except OSError:
        return None
    if _ipinfo_reader is not None and mtime == _ipinfo_reader_mtime:
        return _ipinfo_reader
    with _ipinfo_lock:
        if _ipinfo_reader is not None and mtime == _ipinfo_reader_mtime:
            return _ipinfo_reader
        old_reader = _ipinfo_reader
        try:
            new_reader = _maxminddb.open_database(str(IPINFO_DB_PATH))
            _ipinfo_reader = new_reader
            _ipinfo_reader_mtime = mtime
            if old_reader is not None:
                with contextlib.suppress(Exception):
                    old_reader.close()
        except Exception as exc:
            _geo_log(f"не удалось открыть {IPINFO_DB_PATH.name}: {type(exc).__name__}")
    return _ipinfo_reader


def _ipinfo_fail(message: str) -> None:
    _ipinfo_last["ok"] = False
    _ipinfo_last["error"] = _geo_mask(message)[:200]
    _geo_log(f"IPinfo Lite: ошибка загрузки ({_geo_mask(message)})")


def _ipinfo_download_once() -> bool:
    """Скачивает ipinfo_lite.mmdb по токену (302 → CDN) и атомарно заменяет файл."""
    if not IPINFO_TOKEN or _maxminddb is None:
        return False
    tmp = IPINFO_DB_PATH.with_name(IPINFO_DB_PATH.name + ".part")
    url = IPINFO_URL_TEMPLATE.format(token=quote(IPINFO_TOKEN, safe=""))
    _ipinfo_last["attempt_ts"] = time.time()
    try:
        # requests следует за редиректом и не передаёт токен на CDN
        with requests.get(url, stream=True, timeout=(10, 180), headers={"User-Agent": "krafen-geo/1.0"}) as resp:
            if resp.status_code != 200:
                _ipinfo_fail(f"HTTP {resp.status_code} (проверьте IPINFO_TOKEN)")
                return False
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        check = _maxminddb.open_database(str(tmp))
        try:
            if not check.get("8.8.8.8"):
                raise ValueError("база пустая или повреждена")
        finally:
            check.close()
        os.replace(tmp, IPINFO_DB_PATH)
        _ipinfo_last["ok"] = True
        _ipinfo_last["error"] = ""
        _geo_log("IPinfo Lite: база обновлена")
        return True
    except Exception as exc:
        _ipinfo_fail(f"{type(exc).__name__}: {str(exc)[:160]}")
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False


def _ipinfo_needs_update() -> bool:
    try:
        return time.time() - IPINFO_DB_PATH.stat().st_mtime >= IPINFO_MAX_AGE_SEC
    except OSError:
        return True


def _start_ipinfo_updater() -> None:
    global _ipinfo_updater_started
    if _ipinfo_updater_started or not IPINFO_TOKEN:
        return
    _ipinfo_updater_started = True

    def _worker() -> None:
        while True:
            if _ipinfo_needs_update():
                _ipinfo_download_once()
            time.sleep(IPINFO_CHECK_SEC)

    threading.Thread(target=_worker, name="krafen-ipinfo-updater", daemon=True).start()


def _country_from_ip_db(ip: str) -> str:
    """Страна по IP локально (IPinfo Lite, затем geoip2fast). Внешних запросов нет."""
    global _geoip_db
    valid = lambda cc: len(cc) == 2 and cc.isalpha() and cc != "XX"  # noqa: E731
    reader = _ipinfo_get_reader()
    if reader is not None:
        try:
            rec = reader.get(ip)
            cc = str(rec.get("country_code") or "").strip().upper() if isinstance(rec, dict) else ""
        except Exception:
            cc = ""
        if valid(cc):
            _geo_count("ipinfo")
            return cc
    if _GeoIP2Fast is None:
        return ""
    if _geoip_db is None:
        with _geoip_lock:
            if _geoip_db is None:
                try:
                    _geoip_db = _GeoIP2Fast()
                except Exception:
                    _geoip_db = False
    if not _geoip_db:
        return ""
    try:
        cc = str(getattr(_geoip_db.lookup(ip), "country_code", "") or "").strip().upper()
    except Exception:
        return ""
    if valid(cc):
        _geo_count("geoip2fast")
        return cc
    return ""


def _classify_country(request: Request, ip: str) -> str:
    _geo_note_connection(request)
    if _is_trusted_proxy_peer(request):
        cc = (request.headers.get("cf-ipcountry") or "").strip().upper()
        if len(cc) == 2 and cc.isalpha() and cc != "XX":
            _geo_count("cloudflare")
            return cc
    with contextlib.suppress(ValueError):
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_private:
            _geo_count("local")
            return "LOCAL"
    cc = _country_from_ip_db(ip)
    if not cc:
        _geo_count("unresolved")
    return cc or "DIRECT"


def record_vod_stream_bytes(vod_id: str, byte_count: int) -> None:
    if byte_count <= 0 or not vod_id:
        return
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    hbucket = int(now // 3600)
    with _analytics_buf_lock:
        d = _buf_daily.setdefault(day, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0])
        d[5] += byte_count
        hb = _buf_hourly.setdefault(hbucket, [0, 0, 0, 0])
        hb[3] += byte_count
        if len(_buf_vods) < 1024 or vod_id in _buf_vods:
            vb = _buf_vods.setdefault(vod_id, [0, 0, 0, 0, 0])
            vb[3] += byte_count


def record_request_telemetry(
    request: Request,
    status_code: int,
    latency_ms: float,
    resp_bytes: int,
) -> None:
    path = request.url.path or "/"
    if path.startswith("/api/admin"):
        return
    if status_code >= 500:
        _record_5xx_for_alerts(status_code, path)

    now = time.time()
    now_i = int(now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    hbucket = int(now // 3600)
    ua = (request.headers.get("user-agent") or "")[:300]
    ip = client_ip(request)
    is_bot = bool(_BOT_UA_RE.search(ua)) if ua else False
    purpose = (
        request.headers.get("sec-purpose")
        or request.headers.get("purpose")
        or request.headers.get("x-moz")
        or ""
    ).lower()
    is_prefetch = "prefetch" in purpose or "prerender" in purpose

    is_api = path.startswith("/api/")
    canonical_page = ""
    if request.method == "GET" and status_code == 200 and not is_api:
        norm_p = path.rstrip("/") or "/"
        tracked = _TRACKED_PAGES.get(norm_p)
        if tracked:
            canonical_page = tracked[0]

    # Определяем события VOD
    vod_event_id = ""
    vod_event_kind = ""  # "play" | "download" | "chat"
    if path.startswith("/api/vods/") and status_code in (200, 206):
        rest = path[len("/api/vods/") :]
        if rest.endswith("/stream") and request.method == "GET":
            raw_vid = rest[: -len("/stream")].strip("/")
            if safe_name(raw_vid):
                vod_event_id = raw_vid
                want_dl = (request.query_params.get("download") or "").strip() == "1"
                rng = (request.headers.get("range") or "").strip().lower()
                is_initial_range = (not rng) or rng.startswith("bytes=0-")
                if want_dl and is_initial_range:
                    vod_event_kind = "download"
                elif not want_dl:
                    # Исключаем короткий 192-байтный контейнерный probe (bytes=0-191)
                    if rng != "bytes=0-191":
                        vod_event_kind = "play"
        elif rest.endswith("/chat") and request.method == "GET" and status_code == 200:
            raw_vid = rest[: -len("/chat")].strip("/")
            if safe_name(raw_vid):
                vod_event_id = raw_vid
                vod_event_kind = "chat"
    elif path.startswith("/vods/") and request.method == "GET" and status_code in (200, 206):
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[2].lower().endswith((".mp4", ".webm", ".mkv", ".mov")) and safe_name(parts[1]):
            rng = (request.headers.get("range") or "").strip().lower()
            if rng != "bytes=0-191":
                vod_event_id = parts[1]
                vod_event_kind = "play"

    vhash = _visitor_daily_hash(day, ip, ua) if not is_bot else ""

    with _analytics_buf_lock:
        # [views, api_hits, bot_hits, vod_plays, vod_downloads, bytes_sent,
        #  s2xx, s3xx, s4xx, s5xx, blocked_hits, latency_sum_ms, req_count]
        d = _buf_daily.setdefault(day, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0])
        hb = _buf_hourly.setdefault(hbucket, [0, 0, 0, 0])  # [views, api_hits, vod_plays, bytes_sent]

        d[5] += max(0, resp_bytes)
        hb[3] += max(0, resp_bytes)
        d[11] += max(0.0, latency_ms)
        d[12] += 1

        if 200 <= status_code < 300:
            d[6] += 1
        elif 300 <= status_code < 400:
            d[7] += 1
        elif 400 <= status_code < 500:
            d[8] += 1
            if status_code in (401, 403, 413, 429):
                d[10] += 1
        elif status_code >= 500:
            d[9] += 1

        if is_bot:
            d[2] += 1
            return

        if is_api:
            d[1] += 1
            hb[1] += 1

        if canonical_page and not is_prefetch:
            d[0] += 1
            hb[0] += 1
            if len(_buf_pages) < 512:
                _buf_pages[(day, canonical_page)] = _buf_pages.get((day, canonical_page), 0) + 1

            if vhash:
                if len(_buf_visitors) < 8192:
                    vis = _buf_visitors.get((day, vhash))
                    if vis is None:
                        _buf_visitors[(day, vhash)] = [now_i, now_i, 1]
                    else:
                        vis[1] = now_i
                        vis[2] += 1
                if len(_buf_hourly_visitors) < 8192:
                    _buf_hourly_visitors.add((hbucket, vhash))

                # Живой онлайн за последние 5 минут
                if len(_active_visitors) > 2048:
                    cutoff_act = now - 300.0
                    stale_v = [k for k, v in _active_visitors.items() if v[0] < cutoff_act]
                    for k in stale_v:
                        _active_visitors.pop(k, None)
                if len(_active_visitors) < 2048:
                    _active_visitors[vhash] = (now, canonical_page)

            ref_src = _classify_referrer(request.headers.get("referer") or "", _effective_request_host(request))
            if ref_src and len(_buf_referrers) < 1024:
                _buf_referrers[(day, ref_src)] = _buf_referrers.get((day, ref_src), 0) + 1

            dev, brw, os_name = _classify_user_agent(ua)
            cc = _classify_country(request, ip)
            if len(_buf_devices) < 1024:
                for kind, val in (("device", dev), ("browser", brw), ("os", os_name), ("country", cc)):
                    _buf_devices[(day, kind, val)] = _buf_devices.get((day, kind, val), 0) + 1

        if vod_event_id and vod_event_kind and vhash:
            if len(_recent_vod_events) > 4096:
                cutoff_vod = now - 900.0
                stale_vod = [k for k, ts_v in _recent_vod_events.items() if ts_v < cutoff_vod]
                for k in stale_vod:
                    _recent_vod_events.pop(k, None)
            dedup_key = (vhash, vod_event_id, vod_event_kind)
            prev_ts = _recent_vod_events.get(dedup_key, 0.0)
            if now - prev_ts >= 900.0:
                _recent_vod_events[dedup_key] = now
                if len(_buf_vods) < 1024 or vod_event_id in _buf_vods:
                    # [plays, downloads, chat_loads, bytes_streamed, last_played_at]
                    vb = _buf_vods.setdefault(vod_event_id, [0, 0, 0, 0, 0])
                    if vod_event_kind == "play":
                        d[3] += 1
                        hb[2] += 1
                        vb[0] += 1
                        vb[4] = now_i
                    elif vod_event_kind == "download":
                        d[4] += 1
                        vb[1] += 1
                        vb[4] = now_i
                    elif vod_event_kind == "chat":
                        vb[2] += 1


def log_admin_event(request: Request | None, action: str, status: str = "ok", detail: str = "") -> None:
    ip = client_ip(request) if request is not None else "system"
    now_i = int(time.time())
    clean_act = text(action, 60)
    clean_st = text(status, 24)
    clean_det = text(detail, 260)
    try:
        with _analytics_db_lock, analytics_db_connect() as conn:
            conn.execute(
                "INSERT INTO admin_audit_log (ts, action, status, ip, detail) VALUES (?, ?, ?, ?, ?)",
                (now_i, clean_act, clean_st, ip[:80], clean_det),
            )
            conn.execute(
                "DELETE FROM admin_audit_log WHERE id NOT IN (SELECT id FROM admin_audit_log ORDER BY id DESC LIMIT 500)"
            )
    except Exception:
        pass


def flush_analytics_buffer() -> None:
    global _analytics_last_prune_ts
    with _analytics_buf_lock:
        if (
            not _buf_daily
            and not _buf_hourly
            and not _buf_visitors
            and not _buf_hourly_visitors
            and not _buf_pages
            and not _buf_referrers
            and not _buf_devices
            and not _buf_vods
        ):
            return
        snap_daily = dict(_buf_daily)
        _buf_daily.clear()
        snap_hourly = dict(_buf_hourly)
        _buf_hourly.clear()
        snap_visitors = dict(_buf_visitors)
        _buf_visitors.clear()
        snap_hvisitors = list(_buf_hourly_visitors)
        _buf_hourly_visitors.clear()
        snap_pages = dict(_buf_pages)
        _buf_pages.clear()
        snap_refs = dict(_buf_referrers)
        _buf_referrers.clear()
        snap_devs = dict(_buf_devices)
        _buf_devices.clear()
        snap_vods = dict(_buf_vods)
        _buf_vods.clear()

    now = time.time()
    with _analytics_db_lock, analytics_db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for day, vals in snap_daily.items():
                conn.execute(
                    """
                    INSERT INTO daily_traffic (
                        day, views, api_hits, bot_hits, vod_plays, vod_downloads, bytes_sent,
                        status_2xx, status_3xx, status_4xx, status_5xx, blocked_hits, latency_sum_ms, req_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET
                        views = views + excluded.views,
                        api_hits = api_hits + excluded.api_hits,
                        bot_hits = bot_hits + excluded.bot_hits,
                        vod_plays = vod_plays + excluded.vod_plays,
                        vod_downloads = vod_downloads + excluded.vod_downloads,
                        bytes_sent = bytes_sent + excluded.bytes_sent,
                        status_2xx = status_2xx + excluded.status_2xx,
                        status_3xx = status_3xx + excluded.status_3xx,
                        status_4xx = status_4xx + excluded.status_4xx,
                        status_5xx = status_5xx + excluded.status_5xx,
                        blocked_hits = blocked_hits + excluded.blocked_hits,
                        latency_sum_ms = latency_sum_ms + excluded.latency_sum_ms,
                        req_count = req_count + excluded.req_count
                    """,
                    (day, *vals),
                )

            for hbucket, hvals in snap_hourly.items():
                conn.execute(
                    """
                    INSERT INTO hourly_traffic (hour_bucket, views, api_hits, vod_plays, bytes_sent)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(hour_bucket) DO UPDATE SET
                        views = views + excluded.views,
                        api_hits = api_hits + excluded.api_hits,
                        vod_plays = vod_plays + excluded.vod_plays,
                        bytes_sent = bytes_sent + excluded.bytes_sent
                    """,
                    (hbucket, *hvals),
                )

            for (day, vhash), vvals in snap_visitors.items():
                conn.execute(
                    """
                    INSERT INTO daily_visitors (day, vhash, first_ts, last_ts, hits)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day, vhash) DO UPDATE SET
                        last_ts = MAX(last_ts, excluded.last_ts),
                        hits = hits + excluded.hits
                    """,
                    (day, vhash, vvals[0], vvals[1], vvals[2]),
                )

            if snap_hvisitors:
                conn.executemany(
                    "INSERT OR IGNORE INTO hourly_visitors (hour_bucket, vhash) VALUES (?, ?)",
                    snap_hvisitors,
                )

            for (day, page), cnt in snap_pages.items():
                conn.execute(
                    """
                    INSERT INTO page_stats (day, page, views) VALUES (?, ?, ?)
                    ON CONFLICT(day, page) DO UPDATE SET views = views + excluded.views
                    """,
                    (day, page, cnt),
                )

            for (day, src), cnt in snap_refs.items():
                conn.execute(
                    """
                    INSERT INTO referrer_stats (day, source, views) VALUES (?, ?, ?)
                    ON CONFLICT(day, source) DO UPDATE SET views = views + excluded.views
                    """,
                    (day, src, cnt),
                )

            for (day, kind, name), cnt in snap_devs.items():
                conn.execute(
                    """
                    INSERT INTO device_stats (day, kind, name, views) VALUES (?, ?, ?, ?)
                    ON CONFLICT(day, kind, name) DO UPDATE SET views = views + excluded.views
                    """,
                    (day, kind, name, cnt),
                )

            for vod_id, vvals in snap_vods.items():
                conn.execute(
                    """
                    INSERT INTO vod_analytics (vod_id, plays, downloads, chat_loads, bytes_streamed, last_played_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vod_id) DO UPDATE SET
                        plays = plays + excluded.plays,
                        downloads = downloads + excluded.downloads,
                        chat_loads = chat_loads + excluded.chat_loads,
                        bytes_streamed = bytes_streamed + excluded.bytes_streamed,
                        last_played_at = MAX(last_played_at, excluded.last_played_at)
                    """,
                    (vod_id, vvals[0], vvals[1], vvals[2], vvals[3], vvals[4]),
                )

            if now - _analytics_last_prune_ts > 3600.0:
                _analytics_last_prune_ts = now
                cur_hb = int(now // 3600)
                cutoff_90d = time.strftime("%Y-%m-%d", time.localtime(now - 90 * 86400))
                conn.execute("DELETE FROM hourly_traffic WHERE hour_bucket < ?", (cur_hb - 24 * 14,))
                conn.execute("DELETE FROM hourly_visitors WHERE hour_bucket < ?", (cur_hb - 72,))
                conn.execute("DELETE FROM daily_visitors WHERE day < ?", (cutoff_90d,))

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")


def _start_analytics_flusher() -> None:
    global _analytics_flusher_started
    if _analytics_flusher_started:
        return
    _analytics_flusher_started = True

    def _worker() -> None:
        while True:
            time.sleep(4.0)
            with contextlib.suppress(Exception):
                flush_analytics_buffer()

    threading.Thread(target=_worker, name="krafen-analytics-flusher", daemon=True).start()


_MAX_BODY_DEFAULT = 1_048_576  # 1 МБ для прочих state-changing запросов
_MAX_BODY_COLLECTION = 8_388_608  # 8 МБ: 500 записей с рецензиями до 5000 символов
_MAX_BODY_ADMIN = 8_192        # 8 КБ для /api/admin*
_MAX_BODY_RESTORE = 16_777_216  # 16 МБ для импорта JSON-бэкапа всех коллекций
_MAX_BODY_THUMB = 524_288      # 512 КБ для JPEG-превью


class _PayloadTooLargeError(Exception):
    pass


_COLLECTION_PUT_PATHS = frozenset(f"/api/{_name}" for _name in LIST_SPECS)


def _max_body_bytes_for_path(path: str) -> int:
    if path in ("/api/admin/restore", "/api/admin/import-list"):
        return _MAX_BODY_RESTORE
    if path.startswith("/api/admin"):
        return _MAX_BODY_ADMIN
    if path in _COLLECTION_PUT_PATHS:
        return _MAX_BODY_COLLECTION
    if path.startswith("/api/vods/") and path.endswith("/thumb"):
        return _MAX_BODY_THUMB
    return _MAX_BODY_DEFAULT


def security_headers(
    nonce: str | None = None,
    *,
    is_https: bool = False,
    allow_frame: bool = False,
) -> dict[str, str]:
    script_src = "script-src 'self'"
    if nonce:
        script_src += f" 'nonce-{nonce}'"
    style_src = "'self' 'unsafe-inline' https://fonts.googleapis.com"
    csp_parts = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "form-action 'self'",
        script_src,
        f"style-src {style_src}",
        f"style-src-elem {style_src}",
        "style-src-attr 'unsafe-inline'",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: https:",
        "media-src 'self' blob: https:",
        "worker-src 'self' blob:",
        "frame-src 'self' https://drive.google.com https://*.google.com https://*.googleusercontent.com",
        "connect-src 'self'",
    ]
    if not allow_frame:
        csp_parts.append("frame-ancestors 'self'")
    out = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Content-Security-Policy": "; ".join(csp_parts),
    }
    if not allow_frame:
        out["X-Frame-Options"] = "SAMEORIGIN"
    if is_https:
        out["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return out


def _accepts_gzip(request: Request) -> bool:
    enc = (request.headers.get("accept-encoding") or "").lower()
    return "gzip" in enc


_GZIPPABLE_CONTENT_TYPES = (
    "application/json",
    "application/x-ndjson",
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "image/svg+xml",
    "application/xml",
    "text/plain",
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    is_https = is_https_request(request)
    allow_frame = _is_e2b_preview_request(request)
    hdrs = security_headers(is_https=is_https, allow_frame=allow_frame)

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        max_bytes = _max_body_bytes_for_path(request.url.path)
        cl_raw = (request.headers.get("content-length") or "").strip()
        if cl_raw:
            try:
                if int(cl_raw) > max_bytes:
                    resp = j({"error": "payload_too_large"}, 413)
                    for k, v in hdrs.items():
                        resp.headers.setdefault(k, v)
                    return resp
            except ValueError:
                resp = j({"error": "invalid_content_length"}, 400)
                for k, v in hdrs.items():
                    resp.headers.setdefault(k, v)
                return resp

        orig_receive = request._receive
        seen_bytes = 0

        async def _limited_receive():
            nonlocal seen_bytes
            msg = await orig_receive()
            if msg.get("type") == "http.request":
                seen_bytes += len(msg.get("body") or b"")
                if seen_bytes > max_bytes:
                    raise _PayloadTooLargeError()
            return msg

        request._receive = _limited_receive

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except _PayloadTooLargeError:
        response = j({"error": "payload_too_large"}, 413)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    # Скользящая сессия: активный пользователь не разлогинивается. Cookie обновляем
    # не чаще раза в сутки и только для успешных ответов без собственного Set-Cookie.
    if (
        getattr(response, "status_code", 0) == 200
        and request.method in ("GET", "HEAD")
        and "set-cookie" not in response.headers
    ):
        for _profile, _spec in PROFILES.items():
            _token = request.cookies.get(_spec["cookie"])
            if not verify_session(_token, _profile):
                continue
            if not session_needs_renewal(_token, _profile):
                break
            response.headers["Set-Cookie"] = cookie_header(
                request, _spec["cookie"], create_session(_profile), SESSION_TTL_SECONDS
            )
            break

    for key, value in hdrs.items():
        response.headers.setdefault(key, value)

    with contextlib.suppress(Exception):
        cl_hdr = response.headers.get("content-length")
        resp_len = int(cl_hdr) if (cl_hdr and cl_hdr.isdigit()) else len(getattr(response, "body", b"") or b"")
        # Для StreamingResponse видео байты считаются отдельно в генераторе чанков
        if isinstance(response, StreamingResponse):
            resp_len = 0
        record_request_telemetry(request, int(getattr(response, "status_code", 200) or 200), latency_ms, resp_len)

    return response


# ----------------------------- API коллекций и админки -----------------------------


def make_get_list(name: str):
    def endpoint(request: Request) -> JSONResponse:
        try:
            profile = request_profile(request)
            state = read_list(name, owner=profile)
            response = j({"items": state["items"], "revision": state["revision"], "profile": profile})
        except Exception:
            response = j({"error": "storage_error", "items": [], "revision": 0}, 500)
        # JSON нужен для рендеринга страниц, но не является поисковой страницей.
        response.headers["X-Robots-Tag"] = "noindex"
        return response

    endpoint.__name__ = f"get_{name}"
    return endpoint


def make_put_list(name: str):
    def endpoint(request: Request, body: dict[str, Any]) -> JSONResponse:
        require_same_origin(request)
        profile = request_profile(request)
        require_admin(request, profile)
        rate_limit(request, f"write_{name}", 60, 60)
        try:
            revision = int(body.get("revision"))
        except Exception:
            revision = -1
        if revision < 0:
            state = read_list(name, use_cache=False, owner=profile)
            return j({"error": "revision_required", "items": state["items"], "revision": state["revision"]}, 409)

        normalizer = NORMALIZERS[name]
        raw_items = body.get("items")
        # Некорректный items не должен молча затирать коллекцию: пустой список — это
        # осознанное удаление всех записей, а строка/объект/null — ошибка клиента.
        if not isinstance(raw_items, list):
            state = read_list(name, use_cache=False, owner=profile)
            return j({"error": "items_required", "items": state["items"], "revision": state["revision"]}, 400)
        items = [normalizer(item) for item in raw_items]
        items = [item for item in items if item][: LIST_SPECS[name]["max_items"]]

        ok, next_state = update_list_atomic(name, revision, items, profile)
        if not ok:
            return j({"error": "revision_conflict", "items": next_state["items"], "revision": next_state["revision"]}, 409)
        log_admin_event(request, f"save_{name}", "ok", f"items={len(next_state['items'])}, rev={next_state['revision']}, {profile}")
        return j({"ok": True, "items": next_state["items"], "revision": next_state["revision"]})

    endpoint.__name__ = f"put_{name}"
    return endpoint


for _name in LIST_SPECS:
    app.add_api_route(f"/api/{_name}", make_get_list(_name), methods=["GET"])
    app.add_api_route(f"/api/{_name}", make_put_list(_name), methods=["PUT"])


def get_wrapped_stats(owner: str = "krafen", target_year: str = "all") -> dict[str, Any]:
    with _db_lock, db_connect() as conn:
        w_rows = conn.execute(
            "SELECT title, title_ru, year, type, status, poster, rating, review, tags_json, favorite, added_at "
            "FROM watchlist_items WHERE owner = ?",
            (owner,),
        ).fetchall()
        g_rows = conn.execute(
            "SELECT title, year, status, rating, review, cover, genres_json, platforms_json, favorite, added_at "
            "FROM gamelist_items WHERE owner = ?",
            (owner,),
        ).fetchall()
        vod_rows = conn.execute(
            "SELECT id, title, game, duration_s, started_at FROM drive_vods"
        ).fetchall()

    all_years: set[str] = set()

    def _extract_year(ts: Any, fallback_year: Any) -> str | None:
        if ts:
            try:
                num = float(ts)
                sec = num / 1000.0 if num > 2e10 else num
                y = datetime.fromtimestamp(sec).year
                if 2000 <= y <= 2100:
                    sy = str(y)
                    all_years.add(sy)
                    return sy
            except Exception:
                pass
        fy = str(fallback_year or "").strip()
        if len(fy) >= 4 and fy[:4].isdigit():
            y_str = fy[:4]
            if 2000 <= int(y_str) <= 2100:
                all_years.add(y_str)
                return y_str
        return None

    w_parsed = []
    for r in w_rows:
        d = dict(r)
        d["act_year"] = _extract_year(d.get("added_at"), d.get("year"))
        w_parsed.append(d)

    g_parsed = []
    for r in g_rows:
        d = dict(r)
        d["act_year"] = _extract_year(d.get("added_at"), d.get("year"))
        try:
            d["platforms"] = json.loads(d.get("platforms_json") or "[]")
        except Exception:
            d["platforms"] = []
        try:
            d["genres"] = json.loads(d.get("genres_json") or "[]")
        except Exception:
            d["genres"] = []
        g_parsed.append(d)

    vod_parsed = []
    for r in vod_rows:
        d = dict(r)
        st = str(d.get("started_at") or "")
        if len(st) >= 4 and st[:4].isdigit():
            y_str = st[:4]
            all_years.add(y_str)
            d["act_year"] = y_str
        else:
            d["act_year"] = None
        vod_parsed.append(d)

    years_list = sorted(list(all_years), reverse=True)
    if "all" not in years_list:
        years_list.append("all")

    if not target_year or target_year not in years_list:
        target_year = years_list[0] if years_list else "all"

    def _match(it: dict[str, Any]) -> bool:
        return True if target_year == "all" else it.get("act_year") == target_year

    filtered_w = [w for w in w_parsed if _match(w)]
    filtered_g = [g for g in g_parsed if _match(g)]
    filtered_v = [v for v in vod_parsed if _match(v)]

    movies_count = sum(1 for w in filtered_w if w.get("type") == "movie")
    series_count = sum(1 for w in filtered_w if w.get("type") in ("tv", "series", "serial"))
    anime_count = sum(1 for w in filtered_w if w.get("type") == "anime")

    games_completed = sum(1 for g in filtered_g if g.get("status") in ("completed", "passed", "finished"))
    total_vod_s = sum(int(v.get("duration_s") or 0) for v in filtered_v)

    w_ratings = [float(w["rating"]) for w in filtered_w if float(w.get("rating") or 0) > 0]
    avg_w = round(sum(w_ratings) / len(w_ratings), 1) if w_ratings else 0.0

    g_ratings = [float(g["rating"]) for g in filtered_g if float(g.get("rating") or 0) > 0]
    avg_g = round(sum(g_ratings) / len(g_ratings), 1) if g_ratings else 0.0

    p_counter = Counter()
    for g in filtered_g:
        for p in g.get("platforms", []):
            p_counter[p] += 1

    gen_counter = Counter()
    for g in filtered_g:
        for gen in g.get("genres", []):
            gen_counter[gen] += 1

    top_m = sorted(
        [w for w in filtered_w if w.get("poster")],
        key=lambda x: (x.get("favorite") or 0, float(x.get("rating") or 0)),
        reverse=True,
    )[:6]

    top_g = sorted(
        [g for g in filtered_g if g.get("cover")],
        key=lambda x: (x.get("favorite") or 0, float(x.get("rating") or 0)),
        reverse=True,
    )[:6]

    return {
        "available_years": years_list,
        "selected_year": target_year,
        "summary": {
            "total_watched": len(filtered_w),
            "movies_count": movies_count,
            "series_count": series_count,
            "anime_count": anime_count,
            "games_total": len(filtered_g),
            "games_completed": games_completed,
            "vods_count": len(filtered_v),
            "total_vod_hours": round(total_vod_s / 3600.0, 1),
            "avg_w_rating": avg_w,
            "avg_g_rating": avg_g,
        },
        "top_platforms": [{"name": p, "count": c} for p, c in p_counter.most_common(6)],
        "top_genres": [{"name": gn, "count": c} for gn, c in gen_counter.most_common(8)],
        "top_movies": [
            {
                "title": m.get("title_ru") or m.get("title"),
                "year": m.get("year"),
                "rating": m.get("rating"),
                "poster": m.get("poster"),
                "favorite": bool(m.get("favorite")),
                "type": m.get("type"),
            }
            for m in top_m
        ],
        "top_games": [
            {
                "title": g.get("title"),
                "year": g.get("year"),
                "rating": g.get("rating"),
                "cover": g.get("cover"),
                "favorite": bool(g.get("favorite")),
                "status": g.get("status"),
            }
            for g in top_g
        ],
    }


@app.get("/api/wrapped")
def api_wrapped(request: Request, year: str = "") -> JSONResponse:
    profile = request_profile(request)
    data = get_wrapped_stats(owner=profile, target_year=year)
    return j(data)


@app.get("/api/health")
def health() -> JSONResponse:
    return j({"ok": True})


@app.get("/api/admin")
def admin_check(request: Request) -> JSONResponse:
    tokens = read_tokens()
    profile = request_profile(request)
    cookie_name = profile_spec(profile)["cookie"]
    return j(
        {
            "ok": verify_session(request.cookies.get(cookie_name), profile),
            "profile": profile,
            "configured": bool(PROFILE_PASSWORDS.get(profile)),
            "spotifyConnected": bool(tokens.get("spotify_refresh_token")),
        }
    )


@app.delete("/api/admin")
def admin_logout(request: Request) -> JSONResponse:
    require_same_origin(request)
    profile = request_profile(request)
    cookie_name = profile_spec(profile)["cookie"]
    token = request.cookies.get(cookie_name)
    was_admin = verify_session(token, profile)
    revoke_session(token, profile)
    if was_admin:
        log_admin_event(request, "logout", "ok", f"Выход из админ-сессии ({profile})")
    response = j({"ok": True})
    response.headers["Set-Cookie"] = cookie_header(request, cookie_name, "", 0)
    return response


@app.post("/api/admin")
def admin_login(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    # Профиль выбирается полем login (значение по умолчанию — основной профиль).
    login_value = str(body.get("login") or "").strip().lower()
    profile = login_value if login_value in PROFILES else DEFAULT_PROFILE
    password = PROFILE_PASSWORDS.get(profile, "")
    cookie_name = profile_spec(profile)["cookie"]
    rate_limit(request, f"admin_login_{profile}", 10, 60)
    _check_admin_failed_login_guard(request, profile)
    if not password:
        return j({"error": "admin_password_missing", "profile": profile}, 503)
    if not constant_time_equal(text(body.get("password"), 512), password):
        _record_admin_failed_login(request, profile)
        log_admin_event(request, "login", "denied", f"Неверный пароль ({profile})")
        return j({"error": "invalid_password", "profile": profile}, 401)
    log_admin_event(request, "login", "ok", f"Успешный вход в админ-панель ({profile})")
    response = j({"ok": True, "profile": profile})
    response.headers["Set-Cookie"] = cookie_header(
        request, cookie_name, create_session(profile), SESSION_TTL_SECONDS
    )
    return response


@app.post("/api/admin/update")
def admin_trigger_update(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_update", 6, 60)
    result = perform_github_update(force=True, allow_zip_fallback_on_api_error=True)
    status = 200 if result.get("ok") else 502
    log_admin_event(
        request,
        "github_update",
        "ok" if result.get("ok") else "error",
        f"updated={result.get('updated_files', 0)}, sha={str(result.get('sha') or '')[:8]}",
    )
    if result.get("ok") and result.get("server_changed"):
        threading.Timer(1.0, _reexec_current_process).start()
    return j(result, status)


def _dir_usage_stats(directory: Path, pattern: str = "*") -> dict[str, int]:
    count = 0
    size_bytes = 0
    if not directory.is_dir():
        return {"count": 0, "bytes": 0}
    with contextlib.suppress(OSError):
        for p in directory.glob(pattern):
            if p.is_file() and not p.name.startswith(".tmp_"):
                count += 1
                with contextlib.suppress(OSError):
                    size_bytes += p.stat().st_size
    return {"count": count, "bytes": size_bytes}


def _file_size_safe(path: Path) -> int:
    try:
        total = path.stat().st_size if path.is_file() else 0
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.is_file():
                total += side.stat().st_size
        return total
    except OSError:
        return 0


def _process_rss_bytes() -> int:
    # Linux /proc/self/status
    status_file = Path("/proc/self/status")
    if status_file.is_file():
        with contextlib.suppress(OSError, ValueError):
            for line in status_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    # Windows GetProcessMemoryInfo
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes
            from ctypes import wintypes

            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            hproc = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(hproc, ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
    # POSIX resource fallback
    with contextlib.suppress(Exception):
        import resource

        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return rss if sys.platform == "darwin" else rss * 1024
    return 0


_RU_SHORT_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _fmt_short_day_ru(ymd: str) -> str:
    parts = ymd.split("-")
    if len(parts) != 3:
        return ymd
    with contextlib.suppress(ValueError):
        m = max(1, min(12, int(parts[1])))
        d = int(parts[2])
        return f"{d} {_RU_SHORT_MONTHS[m - 1]}"
    return ymd


@app.get("/api/admin/overview")
def admin_overview(request: Request, days: int = 14) -> JSONResponse:
    overview_profile = DEFAULT_PROFILE
    require_admin(request, overview_profile)
    rate_limit(request, "admin_overview", 120, 60)
    span_days = max(7, min(90, int(days or 14)))

    with contextlib.suppress(Exception):
        flush_analytics_buffer()

    now = time.time()
    today_ymd = time.strftime("%Y-%m-%d", time.localtime(now))
    start_ymd = time.strftime("%Y-%m-%d", time.localtime(now - (span_days - 1) * 86400))
    cur_hbucket = int(now // 3600)
    start_hbucket = cur_hbucket - 23

    # Живой онлайн (за последние 5 минут)
    active_cutoff = now - 300.0
    active_by_page: dict[str, int] = {}
    online_now = 0
    with _analytics_buf_lock:
        stale_keys = [k for k, v in _active_visitors.items() if v[0] < active_cutoff]
        for k in stale_keys:
            _active_visitors.pop(k, None)
        online_now = len(_active_visitors)
        for _, pg in _active_visitors.values():
            active_by_page[pg] = active_by_page.get(pg, 0) + 1

    # Чтение агрегатов из analytics.db
    with _analytics_db_lock, analytics_db_connect() as aconn:
        today_row = aconn.execute("SELECT * FROM daily_traffic WHERE day = ?", (today_ymd,)).fetchone()
        today_uniques = int(
            aconn.execute("SELECT COUNT(*) AS c FROM daily_visitors WHERE day = ?", (today_ymd,)).fetchone()["c"]
        )
        period_uniques = int(
            aconn.execute(
                "SELECT COUNT(DISTINCT vhash) AS c FROM daily_visitors WHERE day >= ?",
                (start_ymd,),
            ).fetchone()["c"]
        )
        all_uniques = int(aconn.execute("SELECT COUNT(DISTINCT vhash) AS c FROM daily_visitors").fetchone()["c"])

        period_sum = aconn.execute(
            """
            SELECT
                COALESCE(SUM(views), 0) AS views,
                COALESCE(SUM(api_hits), 0) AS api_hits,
                COALESCE(SUM(bot_hits), 0) AS bot_hits,
                COALESCE(SUM(vod_plays), 0) AS vod_plays,
                COALESCE(SUM(vod_downloads), 0) AS vod_downloads,
                COALESCE(SUM(bytes_sent), 0) AS bytes_sent,
                COALESCE(SUM(status_2xx), 0) AS s2xx,
                COALESCE(SUM(status_3xx), 0) AS s3xx,
                COALESCE(SUM(status_4xx), 0) AS s4xx,
                COALESCE(SUM(status_5xx), 0) AS s5xx,
                COALESCE(SUM(blocked_hits), 0) AS blocked_hits,
                COALESCE(SUM(latency_sum_ms), 0.0) AS lat_sum,
                COALESCE(SUM(req_count), 0) AS req_cnt
            FROM daily_traffic WHERE day >= ?
            """,
            (start_ymd,),
        ).fetchone()

        all_sum = aconn.execute(
            """
            SELECT
                COALESCE(SUM(views), 0) AS views,
                COALESCE(SUM(api_hits), 0) AS api_hits,
                COALESCE(SUM(bot_hits), 0) AS bot_hits,
                COALESCE(SUM(vod_plays), 0) AS vod_plays,
                COALESCE(SUM(vod_downloads), 0) AS vod_downloads,
                COALESCE(SUM(bytes_sent), 0) AS bytes_sent,
                COALESCE(SUM(blocked_hits), 0) AS blocked_hits
            FROM daily_traffic
            """
        ).fetchone()

        daily_map = {
            r["day"]: dict(r)
            for r in aconn.execute("SELECT * FROM daily_traffic WHERE day >= ?", (start_ymd,)).fetchall()
        }
        daily_uniq_map = {
            r["day"]: int(r["c"])
            for r in aconn.execute(
                "SELECT day, COUNT(*) AS c FROM daily_visitors WHERE day >= ? GROUP BY day",
                (start_ymd,),
            ).fetchall()
        }

        hourly_map = {
            int(r["hour_bucket"]): dict(r)
            for r in aconn.execute(
                "SELECT * FROM hourly_traffic WHERE hour_bucket >= ?",
                (start_hbucket,),
            ).fetchall()
        }
        hourly_uniq_map = {
            int(r["hour_bucket"]): int(r["c"])
            for r in aconn.execute(
                "SELECT hour_bucket, COUNT(*) AS c FROM hourly_visitors WHERE hour_bucket >= ? GROUP BY hour_bucket",
                (start_hbucket,),
            ).fetchall()
        }

        page_rows = aconn.execute(
            "SELECT page, SUM(views) AS v FROM page_stats WHERE day >= ? GROUP BY page ORDER BY v DESC",
            (start_ymd,),
        ).fetchall()
        ref_rows = aconn.execute(
            "SELECT source, SUM(views) AS v FROM referrer_stats WHERE day >= ? GROUP BY source ORDER BY v DESC LIMIT 10",
            (start_ymd,),
        ).fetchall()
        dev_rows = aconn.execute(
            "SELECT kind, name, SUM(views) AS v FROM device_stats WHERE day >= ? GROUP BY kind, name ORDER BY v DESC",
            (start_ymd,),
        ).fetchall()
        vod_stat_map = {
            r["vod_id"]: dict(r)
            for r in aconn.execute("SELECT * FROM vod_analytics").fetchall()
        }
        audit_rows = [
            dict(r)
            for r in aconn.execute("SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT 60").fetchall()
        ]

    # Почасовой ряд за последние 24ч
    hourly_24h: list[dict[str, Any]] = []
    for hb in range(start_hbucket, cur_hbucket + 1):
        hr = hourly_map.get(hb, {})
        ts_h = hb * 3600
        hourly_24h.append(
            {
                "ts": ts_h,
                "label": time.strftime("%H:00", time.localtime(ts_h)),
                "views": int(hr.get("views") or 0),
                "uniques": int(hourly_uniq_map.get(hb, 0)),
                "vod_plays": int(hr.get("vod_plays") or 0),
                "api_hits": int(hr.get("api_hits") or 0),
            }
        )

    # Посуточный ряд за выбранный период
    daily_series: list[dict[str, Any]] = []
    for offset in range(span_days - 1, -1, -1):
        d_ts = now - offset * 86400
        ymd = time.strftime("%Y-%m-%d", time.localtime(d_ts))
        dr = daily_map.get(ymd, {})
        daily_series.append(
            {
                "date": ymd,
                "label": _fmt_short_day_ru(ymd),
                "views": int(dr.get("views") or 0),
                "uniques": int(daily_uniq_map.get(ymd, 0)),
                "vod_plays": int(dr.get("vod_plays") or 0),
                "bytes_sent": int(dr.get("bytes_sent") or 0),
            }
        )

    page_labels = {v[0]: v[1] for v in _TRACKED_PAGES.values()}
    total_pg_views = sum(int(r["v"] or 0) for r in page_rows) or 1
    pages_breakdown = [
        {
            "path": r["page"],
            "label": page_labels.get(r["page"], r["page"]),
            "views": int(r["v"] or 0),
            "pct": round(int(r["v"] or 0) * 100.0 / total_pg_views, 1),
        }
        for r in page_rows
    ]

    total_refs = sum(int(r["v"] or 0) for r in ref_rows) or 1
    referrers_breakdown = [
        {
            "source": r["source"],
            "views": int(r["v"] or 0),
            "pct": round(int(r["v"] or 0) * 100.0 / total_refs, 1),
        }
        for r in ref_rows
    ]

    grouped_devs: dict[str, list[dict[str, Any]]] = {"device": [], "browser": [], "os": [], "country": []}
    kind_totals: dict[str, int] = {"device": 0, "browser": 0, "os": 0, "country": 0}
    for r in dev_rows:
        k = r["kind"]
        if k in kind_totals:
            kind_totals[k] += int(r["v"] or 0)
    for r in dev_rows:
        k = r["kind"]
        if k not in grouped_devs or len(grouped_devs[k]) >= 8:
            continue
        v = int(r["v"] or 0)
        raw_name = str(r["name"] or "")
        label = (
            ("Локальная сеть" if raw_name == "LOCAL" else ("Прямое подключение" if raw_name == "DIRECT" else _COUNTRY_LABELS.get(raw_name, raw_name)))
            if k == "country"
            else raw_name
        )
        grouped_devs[k].append(
            {
                "code": raw_name,
                "label": label,
                "views": v,
                "pct": round(v * 100.0 / max(1, kind_totals[k]), 1),
            }
        )

    # Статистика коллекций
    with _db_lock, db_connect() as conn:
        meta_rows = {
            r["name"]: {"revision": int(r["revision"] or 0), "updated_at": int(r["updated_at"] or 0)}
            for r in conn.execute(
                "SELECT name, revision, updated_at FROM collection_meta WHERE owner = ?", (overview_profile,)
            ).fetchall()
        }

    wl_state = read_list("watchlist", owner=overview_profile)
    gl_state = read_list("gamelist", owner=overview_profile)
    st_state = read_list("setup", owner=overview_profile)
    ws_state = read_list("wishlist", owner=overview_profile)

    wl_items = wl_state["items"]
    gl_items = gl_state["items"]
    st_items = st_state["items"]
    ws_items = ws_state["items"]

    rated_games = [float(g.get("rating") or 0) for g in gl_items if float(g.get("rating") or 0) > 0]
    avg_game_rating = round(sum(rated_games) / len(rated_games), 2) if rated_games else 0.0
    rated_titles = [float(i.get("rating") or 0) for i in wl_items if float(i.get("rating") or 0) > 0]
    avg_title_rating = round(sum(rated_titles) / len(rated_titles), 2) if rated_titles else 0.0
    wl_tags: dict[str, int] = {}
    for item in wl_items:
        for tag in item.get("tags") or []:
            wl_tags[tag] = wl_tags.get(tag, 0) + 1
    gl_tags: dict[str, int] = {}
    for item in gl_items:
        for tag in item.get("tags") or []:
            gl_tags[tag] = gl_tags.get(tag, 0) + 1
    ws_tags: dict[str, int] = {}
    for item in ws_items:
        for tag in item.get("tags") or []:
            ws_tags[tag] = ws_tags.get(tag, 0) + 1

    def _top_tags(counter: dict[str, int], limit: int = 6) -> list[dict[str, Any]]:
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        ]
    wish_budget: dict[str, float] = {"RUB": 0.0, "USD": 0.0, "EUR": 0.0}
    for w in ws_items:
        if w.get("status") != "bought":
            cur = w.get("currency") if w.get("currency") in wish_budget else "RUB"
            price_val = float(w.get("price") or 0)
            if price_val > 0:
                wish_budget[cur] += price_val

    collections_summary = {
        "total_items": len(wl_items) + len(gl_items) + len(st_items) + len(ws_items),
        "watchlist": {
            "total": len(wl_items),
            "revision": wl_state["revision"],
            "updated_at": meta_rows.get("watchlist", {}).get("updated_at", 0),
            "by_type": {
                "anime": sum(1 for i in wl_items if i.get("type") == "anime"),
                "movie": sum(1 for i in wl_items if i.get("type") == "movie"),
                "series": sum(1 for i in wl_items if i.get("type") == "series"),
            },
            "by_status": {
                "watched": sum(1 for i in wl_items if i.get("status") == "watched"),
                "planned": sum(1 for i in wl_items if i.get("status") == "planned"),
                "dropped": sum(1 for i in wl_items if i.get("status") == "dropped"),
            },
            "avg_rating": avg_title_rating,
            "rated_count": len(rated_titles),
            "reviews_count": sum(1 for i in wl_items if str(i.get("review") or "").strip()),
            "favorites": sum(1 for i in wl_items if i.get("favorite")),
            "top_tags": _top_tags(wl_tags),
        },
        "gamelist": {
            "total": len(gl_items),
            "revision": gl_state["revision"],
            "updated_at": meta_rows.get("gamelist", {}).get("updated_at", 0),
            "avg_rating": avg_game_rating,
            "reviews_count": sum(1 for i in gl_items if str(i.get("review") or "").strip()),
            "rated_count": len(rated_games),
            "favorites": sum(1 for i in gl_items if i.get("favorite")),
            "top_tags": _top_tags(gl_tags),
            "by_status": {
                "completed": sum(1 for i in gl_items if i.get("status") == "completed"),
                "playing": sum(1 for i in gl_items if i.get("status") == "playing"),
                "paused": sum(1 for i in gl_items if i.get("status") == "paused"),
                "wishlist": sum(1 for i in gl_items if i.get("status") == "wishlist"),
                "dropped": sum(1 for i in gl_items if i.get("status") == "dropped"),
            },
        },
        "setup": {
            "total": len(st_items),
            "revision": st_state["revision"],
            "updated_at": meta_rows.get("setup", {}).get("updated_at", 0),
            "pc_count": sum(1 for i in st_items if i.get("group") == "pc"),
            "gear_count": sum(1 for i in st_items if i.get("group") == "gear"),
        },
        "wishlist": {
            "total": len(ws_items),
            "revision": ws_state["revision"],
            "updated_at": meta_rows.get("wishlist", {}).get("updated_at", 0),
            "by_status": {
                "want": sum(1 for i in ws_items if i.get("status") == "want"),
                "saving": sum(1 for i in ws_items if i.get("status") == "saving"),
                "bought": sum(1 for i in ws_items if i.get("status") == "bought"),
            },
            "budget": {k: round(v, 2) for k, v in wish_budget.items()},
            "favorites": sum(1 for i in ws_items if i.get("favorite")),
            "tagged": sum(1 for i in ws_items if i.get("tags")),
            "top_tags": _top_tags(ws_tags),
        },
    }

    # Статистика VOD-архива
    all_vods = list_vods(force_sync=False, include_hidden=True)
    enriched_vods: list[dict[str, Any]] = []
    tot_vod_plays = 0
    tot_vod_dls = 0
    tot_vod_bytes = 0
    for v in all_vods:
        vid = str(v.get("id") or "")
        vst = vod_stat_map.get(vid, {})
        plays = int(vst.get("plays") or 0)
        dls = int(vst.get("downloads") or 0)
        chats = int(vst.get("chat_loads") or 0)
        b_streamed = int(vst.get("bytes_streamed") or 0)
        tot_vod_plays += plays
        tot_vod_dls += dls
        tot_vod_bytes += b_streamed
        stem = _vod_cache_stem(vid)
        has_cached_thumb = (THUMB_CACHE_DIR / f"thumb_{stem}.jpg").is_file()
        enriched_vods.append(
            {
                "id": vid,
                "source": v.get("source"),
                "title": v.get("title"),
                "orig_title": v.get("orig_title") or v.get("title"),
                "custom_title": v.get("custom_title") or "",
                "note": v.get("note") or "",
                "orig_note": v.get("orig_note") or "",
                "custom_note": v.get("custom_note") or "",
                "hidden": bool(v.get("hidden")),
                "game": v.get("game") or "",
                "size_label": v.get("size_label") or "",
                "duration_s": int(v.get("duration_s") or 0),
                "has_chat": bool(v.get("has_chat")),
                "has_cached_thumb": has_cached_thumb,
                "thumb_url": v.get("thumb_url"),
                "view_url": v.get("view_url") or "",
                "plays": plays,
                "downloads": dls,
                "chat_loads": chats,
                "bytes_streamed": b_streamed,
                "last_played_at": int(vst.get("last_played_at") or 0),
            }
        )

    # Кэши и файловая система
    thumb_stats = _dir_usage_stats(THUMB_CACHE_DIR, "thumb_*.jpg")
    chat_stats = _dir_usage_stats(CHAT_CACHE_DIR, "chat_*.jsonl")
    img_stats = _dir_usage_stats(IMG_CACHE_DIR, "img_*.*")
    with _api_cache_lock:
        api_cache_count = sum(1 for exp, _, _ in _api_cache.values() if exp > now)
    with _rate_lock:
        rate_buckets_count = len(_rate_buckets)

    disk_total, disk_used, disk_free = 0, 0, 0
    with contextlib.suppress(OSError):
        du = shutil.disk_usage(DATA_DIR)
        disk_total, disk_used, disk_free = int(du.total), int(du.used), int(du.free)

    load_1m = None
    if hasattr(os, "getloadavg"):
        with contextlib.suppress(OSError):
            load_1m = round(os.getloadavg()[0], 2)

    cur_sha = ""
    with contextlib.suppress(OSError):
        if UPDATE_SHA_PATH.is_file():
            cur_sha = UPDATE_SHA_PATH.read_text(encoding="utf-8").strip()[:12]

    tokens = read_tokens()
    rec_enabled = os.environ.get("RECORDER") == "1"
    rec_alive = bool(_ACTIVE_REC_PROC is not None and _ACTIVE_REC_PROC.poll() is None)
    rec_hb = kv_get("recorder_heartbeat")
    rec_hb_age = (now - float(rec_hb.get("ts") or 0)) if isinstance(rec_hb, dict) else None
    rec_hb_fresh = isinstance(rec_hb_age, (int, float)) and rec_hb_age < 900.0
    if rec_alive:
        rec_status = "ok"
        rec_detail = f"Активен (@{os.environ.get('RECORDER_CHANNEL', 'danonsha')})"
    elif rec_hb_fresh and isinstance(rec_hb, dict):
        rec_status = "ok"
        free_s = f"{float(rec_hb.get('free_gb') or 0):.1f} ГБ" if rec_hb.get("free_gb") else "—"
        rec_detail = (
            f"Домашний ПК · {rec_hb.get('state') or 'онлайн'} "
            f"({int(rec_hb_age // 60)}м назад) · диск {free_s}"
        )
    elif isinstance(rec_hb, dict) and rec_hb.get("ts"):
        rec_status = "warn"
        rec_detail = f"Домашний ПК офлайн > {max(15, int((rec_hb_age or 0) // 60))} мин"
    elif rec_enabled:
        rec_status = "warn"
        rec_detail = "Включён в .env, но процесс остановлен"
    else:
        rec_status = "muted"
        rec_detail = "Домашний ПК (настройте RECORDER_VPS_URL + RECORDER_TOKEN для статуса)"

    with _db_lock, db_connect() as sconn:
        sug_rows = [
            dict(sr)
            for sr in sconn.execute(
                "SELECT * FROM suggestions WHERE status = 'pending' ORDER BY id DESC LIMIT 60"
            ).fetchall()
        ]

    req_cnt_period = int(period_sum["req_cnt"] or 0)
    avg_lat_ms = round(float(period_sum["lat_sum"] or 0.0) / req_cnt_period, 1) if req_cnt_period > 0 else 0.0
    today_req_cnt = int(today_row["req_count"] or 0) if today_row else 0
    today_lat_ms = (
        round(float(today_row["latency_sum_ms"] or 0.0) / today_req_cnt, 1)
        if (today_row and today_req_cnt > 0)
        else 0.0
    )

    return j(
        {
            "ok": True,
            "span_days": span_days,
            "realtime": {
                "online_now": online_now,
                "active_pages": active_by_page,
                "server_time": int(now),
                "uptime_s": max(0, int(now - _SERVER_STARTED_AT)),
            },
            "kpis": {
                "today": {
                    "views": int(today_row["views"] or 0) if today_row else 0,
                    "uniques": today_uniques,
                    "vod_plays": int(today_row["vod_plays"] or 0) if today_row else 0,
                    "vod_downloads": int(today_row["vod_downloads"] or 0) if today_row else 0,
                    "api_hits": int(today_row["api_hits"] or 0) if today_row else 0,
                    "bot_hits": int(today_row["bot_hits"] or 0) if today_row else 0,
                    "blocked_hits": int(today_row["blocked_hits"] or 0) if today_row else 0,
                    "bytes_sent": int(today_row["bytes_sent"] or 0) if today_row else 0,
                    "avg_latency_ms": today_lat_ms,
                },
                "period": {
                    "views": int(period_sum["views"] or 0),
                    "uniques": period_uniques,
                    "vod_plays": int(period_sum["vod_plays"] or 0),
                    "vod_downloads": int(period_sum["vod_downloads"] or 0),
                    "api_hits": int(period_sum["api_hits"] or 0),
                    "bot_hits": int(period_sum["bot_hits"] or 0),
                    "blocked_hits": int(period_sum["blocked_hits"] or 0),
                    "bytes_sent": int(period_sum["bytes_sent"] or 0),
                    "avg_latency_ms": avg_lat_ms,
                    "status_2xx": int(period_sum["s2xx"] or 0),
                    "status_3xx": int(period_sum["s3xx"] or 0),
                    "status_4xx": int(period_sum["s4xx"] or 0),
                    "status_5xx": int(period_sum["s5xx"] or 0),
                },
                "all_time": {
                    "views": int(all_sum["views"] or 0),
                    "uniques": all_uniques,
                    "vod_plays": int(all_sum["vod_plays"] or 0),
                    "vod_downloads": int(all_sum["vod_downloads"] or 0),
                    "api_hits": int(all_sum["api_hits"] or 0),
                    "bot_hits": int(all_sum["bot_hits"] or 0),
                    "blocked_hits": int(all_sum["blocked_hits"] or 0),
                    "bytes_sent": int(all_sum["bytes_sent"] or 0),
                },
            },
            "charts": {
                "hourly_24h": hourly_24h,
                "daily": daily_series,
            },
            "breakdowns": {
                "pages": pages_breakdown,
                "referrers": referrers_breakdown,
                "devices": grouped_devs["device"],
                "browsers": grouped_devs["browser"],
                "os": grouped_devs["os"],
                "countries": grouped_devs["country"],
            },
            "collections": collections_summary,
            "vods": {
                "total": len(enriched_vods),
                "visible_count": sum(1 for v in enriched_vods if not v["hidden"]),
                "hidden_count": sum(1 for v in enriched_vods if v["hidden"]),
                "gdrive_count": sum(1 for v in enriched_vods if v["source"] == "gdrive"),
                "local_count": sum(1 for v in enriched_vods if v["source"] == "local"),
                "total_plays": tot_vod_plays,
                "total_downloads": tot_vod_dls,
                "total_bytes_streamed": tot_vod_bytes,
                "last_sync_ts": int(_drive_sync_ts or 0),
                "folder_id": GDRIVE_FOLDER_ID,
                "folder_url": f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}" if GDRIVE_FOLDER_ID else "",
                "items": enriched_vods,
            },
            "caches": {
                "vod_thumbs": thumb_stats,
                "vod_chats": chat_stats,
                "img_proxy": img_stats,
                "api_mem_entries": api_cache_count,
                "rate_buckets": rate_buckets_count,
            },
            "geo": geo_status(),
            "system": {
                "python": platform.python_version(),
                "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
                "pid": os.getpid(),
                "cpu_count": os.cpu_count() or 1,
                "load_1m": load_1m,
                "rss_bytes": _process_rss_bytes(),
                "disk_total_bytes": disk_total,
                "disk_used_bytes": disk_used,
                "disk_free_bytes": disk_free,
                "disk_used_pct": round(disk_used * 100.0 / disk_total, 1) if disk_total > 0 else 0.0,
                "db_sizes": {
                    "krafen_db": _file_size_safe(DB_PATH),
                    "auth_db": _file_size_safe(AUTH_DB_PATH),
                    "analytics_db": _file_size_safe(ANALYTICS_DB_PATH),
                    "server_log": _file_size_safe(HEADLESS_LOG_PATH),
                },
                "is_pterodactyl": _is_pterodactyl_env(),
                "auto_update_enabled": is_auto_update_enabled(),
                "auto_update_repo": f"{AUTO_UPDATE_REPO}@{AUTO_UPDATE_BRANCH}",
                "current_sha": cur_sha,
            },
            "integrations": [
                {
                    "id": "admin_auth",
                    "name": "Админ-защита и сессии",
                    "status": "ok" if ADMIN_PASSWORD else "error",
                    "detail": "Пароль задан, HMAC + CSRF активны" if ADMIN_PASSWORD else "Пароль не задан или равен change-me",
                },
                {
                    "id": "public_origin",
                    "name": "PUBLIC_ORIGIN",
                    "status": "ok" if PUBLIC_ORIGIN else "warn",
                    "detail": PUBLIC_ORIGIN or "Автоопределение по заголовку Host",
                },
                {
                    "id": "spotify",
                    "name": "Spotify Web API",
                    "status": (
                        "ok"
                        if (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and tokens.get("spotify_refresh_token"))
                        else ("warn" if (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET) else "muted")
                    ),
                    "detail": (
                        "Аккаунт привязан (живой трек активен)"
                        if tokens.get("spotify_refresh_token")
                        else ("Ключи есть, требуется привязка OAuth" if SPOTIFY_CLIENT_ID else "Не настроено (работает Last.fm)")
                    ),
                    "connected": bool(tokens.get("spotify_refresh_token")),
                },
                {
                    "id": "lastfm",
                    "name": "Last.fm Scrobbler",
                    "status": "ok" if LASTFM_API_KEY else "muted",
                    "detail": f"Профиль @{LASTFM_USER}" if LASTFM_API_KEY else "Ключ LASTFM_API_KEY не указан",
                },
                {
                    "id": "twitch",
                    "name": "Twitch Helix & IGDB",
                    "status": "ok" if (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET) else "warn",
                    "detail": f"Канал @{TWITCH_CHANNEL}" if TWITCH_CLIENT_ID else "Ключи TWITCH_CLIENT_ID/SECRET не заданы",
                },
                {
                    "id": "tmdb",
                    "name": "TMDB + Shikimori",
                    "status": "ok" if (TMDB_ACCESS_TOKEN or TMDB_API_KEY) else "warn",
                    "detail": "Поиск фильмов, сериалов и аниме готов" if (TMDB_ACCESS_TOKEN or TMDB_API_KEY) else "Shikimori работает, TMDB без ключа",
                },
                {
                    "id": "discord",
                    "name": "Discord Виджет",
                    "status": "ok" if DISCORD_INVITE_CODE else "muted",
                    "detail": f"Инвайт discord.gg/{DISCORD_INVITE_CODE}" if DISCORD_INVITE_CODE else "Не задан код приглашения",
                },
                {
                    "id": "gdrive",
                    "name": "Google Drive VOD-архив",
                    "status": "ok" if GDRIVE_FOLDER_ID else "muted",
                    "detail": (
                        f"Папка подключена ({'API v3' if GDRIVE_API_KEY else 'публичный режим'})"
                        if GDRIVE_FOLDER_ID
                        else "Папка не задана"
                    ),
                },
                {
                    "id": "github_update",
                    "name": "Автообновление GitHub",
                    "status": "ok" if is_auto_update_enabled() else "muted",
                    "detail": (
                        f"Включено ({'токен задан' if _resolve_github_token() else 'публичный доступ'})"
                        if is_auto_update_enabled()
                        else "Отключено (локальная git-рабочая копия)"
                    ),
                },
                {
                    "id": "recorder",
                    "name": "Рекордер стримов",
                    "status": rec_status,
                    "detail": rec_detail,
                },
                {
                    "id": "tg_alerts",
                    "name": "Telegram-алерты VPS",
                    "status": "ok" if (TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID) else "muted",
                    "detail": (
                        f"Активны (порог диска < {ALERT_MIN_FREE_GB:.1f} ГБ, 5xx и Drive)"
                        if (TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID)
                        else "Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_ADMIN_CHAT_ID"
                    ),
                    "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID),
                },
            ],
            "backup": {
                "last_tg_backup_ts": int(float(kv_get("last_tg_backup_ts") or 0)),
                "last_tg_backup_msk_day": str(kv_get("last_tg_backup_msk_day") or ""),
                "daily_enabled": bool(TG_BACKUP_DAILY),
                "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID),
            },
            "suggestions": sug_rows,
            "audit_log": audit_rows,
        }
    )


@app.post("/api/admin/vods/sync")
def admin_sync_vods(request: Request) -> JSONResponse:
    global _drive_sync_ts
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_vod_sync", 12, 60)
    if not GDRIVE_FOLDER_ID:
        return j({"error": "gdrive_not_configured"}, 400)
    with _drive_sync_lock:
        _drive_sync_ts = time.time()
    _sync_drive_vods_worker(GDRIVE_FOLDER_ID, force=True)
    vods = list_vods(force_sync=False, include_hidden=True)
    log_admin_event(request, "vod_sync", "ok", f"Синхронизировано записей: {len(vods)}")
    return j({"ok": True, "count": len(vods)})


@app.post("/api/admin/vods/override")
def admin_vod_override(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_vod_override", 60, 60)
    vod_id = text(body.get("vod_id"), 160)
    if not safe_name(vod_id):
        return j({"error": "invalid_vod_id"}, 400)

    overrides = read_vod_overrides()
    if body.get("reset"):
        overrides.pop(vod_id, None)
        write_vod_overrides(overrides)
        log_admin_event(request, "vod_override", "ok", f"Сброшены правки VOD {vod_id}")
        return j({"ok": True, "vod_id": vod_id, "override": None})

    custom_title = text(body.get("custom_title"), 260)
    custom_note = text(body.get("custom_note"), 180)
    hidden = bool(body.get("hidden"))
    if not custom_title and not custom_note and not hidden:
        overrides.pop(vod_id, None)
    else:
        overrides[vod_id] = {
            "custom_title": custom_title,
            "custom_note": custom_note,
            "hidden": hidden,
            "updated_at": int(time.time()),
        }
    write_vod_overrides(overrides)
    log_admin_event(
        request,
        "vod_override",
        "ok",
        f"VOD {vod_id}: hidden={hidden}, note={bool(custom_note)}, title={bool(custom_title)}",
    )
    return j({"ok": True, "vod_id": vod_id, "override": overrides.get(vod_id)})


@app.post("/api/admin/vods/reset-thumb")
def admin_vod_reset_thumb(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_reset_thumb", 30, 60)
    target = text(body.get("vod_id"), 160)
    removed = 0
    if target == "all":
        if THUMB_CACHE_DIR.is_dir():
            for p in THUMB_CACHE_DIR.glob("thumb_*.jpg"):
                with contextlib.suppress(OSError):
                    p.unlink()
                    removed += 1
        log_admin_event(request, "reset_thumb", "ok", f"Сброшены все превью ({removed} шт.)")
        return j({"ok": True, "removed": removed})

    if not safe_name(target):
        return j({"error": "invalid_vod_id"}, 400)
    stem = _vod_cache_stem(target)
    tpath = THUMB_CACHE_DIR / f"thumb_{stem}.jpg"
    if tpath.is_file():
        with contextlib.suppress(OSError):
            tpath.unlink()
            removed = 1
    log_admin_event(request, "reset_thumb", "ok", f"Сброшено превью {target}")
    return j({"ok": True, "removed": removed})


@app.post("/api/admin/cache/clear")
def admin_clear_cache(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_cache_clear", 20, 60)
    target = text(body.get("target"), 40) or "api"
    freed_bytes = 0
    removed_files = 0

    def _purge_dir(directory: Path, pattern: str) -> None:
        nonlocal freed_bytes, removed_files
        if not directory.is_dir():
            return
        for p in directory.glob(pattern):
            if p.is_file():
                with contextlib.suppress(OSError):
                    freed_bytes += p.stat().st_size
                    p.unlink()
                    removed_files += 1

    if target in ("api", "all_caches"):
        with _api_cache_lock:
            removed_files += len(_api_cache)
            _api_cache.clear()
        with _list_cache_lock:
            _list_cache.clear()
    if target in ("img", "all_caches"):
        _purge_dir(IMG_CACHE_DIR, "img_*.*")
    if target in ("thumbs", "all_caches"):
        _purge_dir(THUMB_CACHE_DIR, "thumb_*.jpg")
    if target in ("chats", "all_caches"):
        _purge_dir(CHAT_CACHE_DIR, "chat_*.jsonl")
    if target == "analytics":
        with _analytics_buf_lock:
            _buf_daily.clear()
            _buf_hourly.clear()
            _buf_visitors.clear()
            _buf_hourly_visitors.clear()
            _buf_pages.clear()
            _buf_referrers.clear()
            _buf_devices.clear()
            _buf_vods.clear()
            _active_visitors.clear()
            _recent_vod_events.clear()
        with _analytics_db_lock, analytics_db_connect() as aconn:
            for tbl in (
                "daily_traffic",
                "hourly_traffic",
                "daily_visitors",
                "hourly_visitors",
                "page_stats",
                "referrer_stats",
                "device_stats",
                "vod_analytics",
            ):
                aconn.execute(f"DELETE FROM {tbl}")

    log_admin_event(request, "cache_clear", "ok", f"target={target}, removed={removed_files}, bytes={freed_bytes}")
    return j({"ok": True, "target": target, "removed": removed_files, "freed_bytes": freed_bytes})


_alert_lock = threading.Lock()
_alert_cooldowns: dict[str, float] = {}
_http_5xx_timestamps: deque[float] = deque(maxlen=64)
_drive_sync_failures = 0
_health_alert_poller_started = False


def _send_telegram_admin_message(text_msg: str) -> tuple[bool, str]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return False, "telegram_not_configured"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(
            api_url,
            json={
                "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                "text": str(text_msg)[:3800],
                "disable_web_page_preview": True,
            },
            timeout=6,
        )
        if res.ok:
            return True, "ok"
        return False, f"http_{res.status_code}"
    except Exception:
        return False, "request_failed"


def _maybe_send_admin_tg_alert(kind: str, text_msg: str, cooldown_s: float = 3600.0) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return False
    now = time.time()
    with _alert_lock:
        last_ts = _alert_cooldowns.get(kind, 0.0)
        if now - last_ts < cooldown_s:
            return False
        _alert_cooldowns[kind] = now
    threading.Thread(
        target=_send_telegram_admin_message,
        args=(text_msg,),
        name=f"tg-alert-{kind}",
        daemon=True,
    ).start()
    return True


def _record_5xx_for_alerts(status_code: int, path: str) -> None:
    now = time.time()
    cutoff = now - 300.0
    burst_count = 0
    with _alert_lock:
        _http_5xx_timestamps.append(now)
        while _http_5xx_timestamps and _http_5xx_timestamps[0] < cutoff:
            _http_5xx_timestamps.popleft()
        burst_count = len(_http_5xx_timestamps)
    if burst_count >= 5:
        _maybe_send_admin_tg_alert(
            "http_5xx",
            f"[krafen.me] Зафиксирован всплеск 5xx на VPS: {burst_count} ошибок за 5 мин (последняя: {status_code} {path[:120]})",
            cooldown_s=1800.0,
        )


def _check_vps_disk_alert() -> None:
    try:
        du = shutil.disk_usage(DATA_DIR)
        free_gb = du.free / (1024.0 ** 3)
        if free_gb < ALERT_MIN_FREE_GB:
            _maybe_send_admin_tg_alert(
                "low_disk",
                f"[krafen.me] Мало места на диске VPS: свободно {free_gb:.2f} ГБ (порог {ALERT_MIN_FREE_GB:.1f} ГБ)",
                cooldown_s=21600.0,
            )
    except OSError:
        pass

    # Проверка протухшего heartbeat домашнего recorder.py
    hb = kv_get("recorder_heartbeat")
    if isinstance(hb, dict) and hb.get("ts"):
        age_s = time.time() - float(hb.get("ts") or 0)
        if age_s > 1800.0:
            _maybe_send_admin_tg_alert(
                "home_recorder_stale",
                f"[krafen.me] Домашний рекордер (@{hb.get('channel') or RECORDER_CHANNEL}) не отвечает более {int(age_s // 60)} мин.",
                cooldown_s=21600.0,
            )


def _next_msk_midnight(now_msk: datetime | None = None) -> datetime:
    cur = now_msk or datetime.now(MSK_TZ)
    return (cur + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)


_daily_tg_backup_started = False


def _start_daily_tg_backup_scheduler() -> None:
    global _daily_tg_backup_started
    if _daily_tg_backup_started:
        return
    _daily_tg_backup_started = True

    def _worker() -> None:
        while True:
            try:
                now_msk = datetime.now(MSK_TZ)
                target_msk = _next_msk_midnight(now_msk)
                while True:
                    rem = (target_msk - datetime.now(MSK_TZ)).total_seconds()
                    if rem <= 0:
                        break
                    time.sleep(min(30.0, rem))
                if TG_BACKUP_DAILY and TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID:
                    today_key = target_msk.strftime("%Y-%m-%d")
                    if kv_get("last_tg_backup_msk_day") != today_key:
                        ok, detail = _send_telegram_backup_zip()
                        if ok:
                            kv_set("last_tg_backup_msk_day", today_key)
                        log_admin_event(None, "tg_backup", "ok" if ok else "error", f"Плановый (00:00 МСК): {detail}")
            except Exception as exc:
                print(f"[backup] ошибка планировщика: {type(exc).__name__}", flush=True)
                time.sleep(60)

    threading.Thread(target=_worker, name="daily-msk-tg-backup", daemon=True).start()


def _build_full_server_backup_zip() -> bytes:
    """Формирует ZIP-бэкап сервера без секретов (код, БД коллекций и аналитики, ассеты, кэши VOD/чатов)."""
    import tempfile

    buf = io.BytesIO()
    max_zip_raw = 42 * 1024 * 1024  # запас под лимит Telegram sendDocument 50 МБ
    added_raw = 0
    skip_dirs = {
        ".git", ".venv", "venv", "__pycache__", "node_modules",
        ".pytest_cache", ".ruff_cache", ".cache", ".local", "etc",
        "vods", "brain", "scratch",
    }
    skip_names = {"ipinfo_lite.mmdb", "server.pid", "recorder.pid", "server.log", "server.log.old", "recorder.log", "rclone.conf"}
    code_prefixes = ("danonsha/", "recorder/", "tests/", "images/")

    def _is_secret_file(rel: str, fname: str) -> bool:
        low = fname.lower()
        if low == ".env" or (low.startswith(".env.") and low != ".env.example"):
            return True
        if low.startswith(".tg_session") or ".tg_session" in rel.lower():
            return True
        if low.endswith((".key", ".pem", ".pyc", ".pyo", ".exe")):
            return True
        return False

    def _add_path(zf: zipfile.ZipFile, src: Path, arc: str, limit: int = max_zip_raw) -> None:
        nonlocal added_raw
        if not src.is_file():
            return
        with contextlib.suppress(OSError):
            sz = src.stat().st_size
            if sz > 30 * 1024 * 1024 or added_raw + sz > limit:
                return
            zf.write(src, arcname=arc)
            added_raw += sz

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            seen_arcs: set[str] = set()
            # 1. Сначала код сайта, тесты, конфиги-примеры и ассеты (чтобы кэши в data/ не вытесняли код)
            root_and_code_files: list[tuple[str, Path]] = []
            vod_cache_files: list[tuple[str, Path]] = []
            for fpath in sorted(ROOT.rglob("*")):
                if not fpath.is_file():
                    continue
                try:
                    rel = fpath.relative_to(ROOT).as_posix()
                except ValueError:
                    continue
                parts = rel.split("/")
                if any(p in skip_dirs for p in parts):
                    continue
                fname = parts[-1]
                if fname in skip_names or _is_secret_file(rel, fname):
                    continue
                if rel.startswith("data/"):
                    if rel.startswith(("data/krafen.db", "data/auth.db", "data/analytics.db", "data/img_cache/")) or ".db-" in fname:
                        continue
                    if rel == "data/seed.sql":
                        root_and_code_files.append((rel, fpath))
                    elif rel.startswith(("data/vod_chats/", "data/vod_thumbs/")):
                        vod_cache_files.append((rel, fpath))
                elif "/" not in rel or rel.startswith(code_prefixes):
                    root_and_code_files.append((rel, fpath))

            for rel, fpath in root_and_code_files:
                if rel not in seen_arcs:
                    _add_path(zf, fpath, rel, max_zip_raw)
                    seen_arcs.add(rel)

            # 2. Консистентные снимки публичных SQLite БД (без auth.db)
            for src_db in (DB_PATH, ANALYTICS_DB_PATH):
                if not src_db.is_file():
                    continue
                dst_db = tmp_root / src_db.name
                try:
                    with (
                        contextlib.closing(sqlite3.connect(src_db, timeout=15)) as sconn,
                        contextlib.closing(sqlite3.connect(dst_db)) as dconn,
                    ):
                        sconn.backup(dconn)
                    arc = f"data/{src_db.name}"
                    if arc not in seen_arcs:
                        _add_path(zf, dst_db, arc, max_zip_raw)
                        seen_arcs.add(arc)
                except Exception:
                    continue

            # 3. В оставшийся бюджет докладываем кэши чатов/превью VOD и прокси-обложек
            for rel, fpath in vod_cache_files:
                if rel not in seen_arcs:
                    _add_path(zf, fpath, rel, max_zip_raw)
                    seen_arcs.add(rel)
            if IMG_CACHE_DIR.is_dir():
                for img_f in sorted(IMG_CACHE_DIR.iterdir()):
                    arc = f"data/img_cache/{img_f.name}"
                    if arc not in seen_arcs:
                        _add_path(zf, img_f, arc, max_zip_raw)
                        seen_arcs.add(arc)
    return buf.getvalue()


_build_sqlite_backup_zip = _build_full_server_backup_zip


def _send_telegram_backup_zip() -> tuple[bool, str]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return False, "telegram_not_configured"
    zip_bytes = _build_full_server_backup_zip()
    if not zip_bytes:
        return False, "empty_backup"
    if len(zip_bytes) > 49 * 1024 * 1024:
        print(f"[backup] размер ZIP ({len(zip_bytes)} байт) превышает лимит Telegram 50 МБ", flush=True)
        return False, "backup_too_large"
    now_msk = datetime.now(MSK_TZ)
    stamp = now_msk.strftime("%Y-%m-%d_%H-%M")
    filename = f"krafen-server-backup-{stamp}-msk.zip"
    size_kb = max(1, len(zip_bytes) // 1024)
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data={
                "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                "caption": f"[krafen.me] Полный бэкап сервера ({stamp} МСК · {size_kb} КБ)",
            },
            files={"document": (filename, zip_bytes, "application/zip")},
            timeout=30,
        )
        if res.ok:
            kv_set("last_tg_backup_ts", time.time())
            return True, filename
        return False, f"http_{res.status_code}"
    except Exception:
        return False, "request_failed"


def _start_health_alert_poller() -> None:
    global _health_alert_poller_started
    if _health_alert_poller_started:
        return
    _health_alert_poller_started = True

    def _poller() -> None:
        time.sleep(30)
        while True:
            _check_vps_disk_alert()
            time.sleep(600)

    threading.Thread(target=_poller, name="vps-health-alerts", daemon=True).start()


def _is_safe_public_http_url(raw_url: Any) -> str | None:
    cleaned = clean_url(raw_url, 2000)
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ip = ipaddress.ip_address(host)
        return cleaned if ip.is_global else None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    for info in infos:
        addr = info[4][0]
        try:
            resolved_ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if not resolved_ip.is_global:
            return None
    return cleaned


def _unwrap_checkable_url(raw_url: Any) -> str | None:
    s = str(raw_url or "").strip()
    if not s:
        return None
    if s.startswith("/api/img?"):
        qs = parse_qs(urlsplit(s).query)
        inner = qs.get("u", [""])[0]
        return inner or None
    if s.startswith(("http://", "https://")):
        return s
    return None


def _check_single_external_url(raw_url: str) -> dict[str, Any]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KrafenLinkCheck/1.0)"}
    cur = _is_safe_public_http_url(raw_url)
    if not cur:
        return {"ok": False, "status": 0, "reason": "Небезопасный или неразрешённый URL"}

    session = requests.Session()
    try:
        for _hop in range(4):
            try:
                res = session.head(cur, headers=headers, timeout=5, allow_redirects=False)
                if res.status_code in (403, 405, 501, 999):
                    res.close()
                    res = session.get(cur, headers=headers, timeout=5, allow_redirects=False, stream=True)
            except requests.Timeout:
                return {"ok": False, "status": 0, "reason": "Таймаут (5с)"}
            except requests.RequestException as exc:
                return {"ok": False, "status": 0, "reason": text(type(exc).__name__, 80)}

            try:
                code = int(res.status_code)
                if code in (301, 302, 303, 307, 308):
                    loc = res.headers.get("location") or ""
                    if not loc:
                        return {"ok": False, "status": code, "reason": f"HTTP {code} без Location"}
                    next_abs = loc if loc.startswith(("http://", "https://")) else urlsplit(cur)._replace(path=loc if loc.startswith("/") else "/" + loc, query="", fragment="").geturl()
                    safe_next = _is_safe_public_http_url(next_abs)
                    if not safe_next:
                        return {"ok": False, "status": code, "reason": "Редирект на приватный/недопустимый узел"}
                    cur = safe_next
                    continue
                if code < 400:
                    return {"ok": True, "status": code, "reason": "ok"}
                return {"ok": False, "status": code, "reason": f"HTTP {code}"}
            finally:
                res.close()
        return {"ok": False, "status": 310, "reason": "Слишком много редиректов"}
    finally:
        session.close()


@app.post("/api/admin/alerts/test")
def admin_test_tg_alert(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_alert_test", 10, 60)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return j({"ok": False, "error": "telegram_not_configured"}, 400)
    ok, detail = _send_telegram_admin_message(
        "[krafen.me] Тестовый алерт из /admin: Telegram-уведомления VPS работают."
    )
    log_admin_event(request, "tg_alert_test", "ok" if ok else "error", f"Результат: {detail}")
    if not ok:
        return j({"ok": False, "error": detail}, 502)
    return j({"ok": True})


@app.post("/api/admin/links/check")
def admin_check_links(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_links_check", 6, 60)

    field_map = {
        "watchlist": ("poster",),
        "gamelist": ("cover",),
        "setup": ("link",),
        "wishlist": ("link", "image"),
    }
    refs_by_url: dict[str, list[dict[str, str]]] = {}
    total_items = 0

    for owner in ("krafen", "danonsha"):
        for coll, fields in field_map.items():
            state = read_list(coll, owner=owner)
            items = state.get("items") or []
            total_items += len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_title = text(item.get("title") or item.get("name"), 140) or "Без названия"
                for fld in fields:
                    target_url = _unwrap_checkable_url(item.get(fld))
                    if not target_url:
                        continue
                    refs_by_url.setdefault(target_url, []).append(
                        {
                            "collection": coll,
                            "owner": owner,
                            "title": item_title,
                            "field": fld,
                        }
                    )

    unique_urls = list(refs_by_url.keys())[:250]
    results: dict[str, dict[str, Any]] = {}
    if unique_urls:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for url_str, res in zip(unique_urls, pool.map(_check_single_external_url, unique_urls)):
                results[url_str] = res

    broken: list[dict[str, Any]] = []
    for url_str in unique_urls:
        res = results.get(url_str) or {}
        if res.get("ok"):
            continue
        for ref in refs_by_url.get(url_str, []):
            broken.append(
                {
                    **ref,
                    "url": url_str[:300],
                    "status": int(res.get("status") or 0),
                    "reason": str(res.get("reason") or "Ошибка"),
                }
            )
            if len(broken) >= 200:
                break
        if len(broken) >= 200:
            break

    log_admin_event(
        request,
        "links_check",
        "ok",
        f"Проверено URL: {len(unique_urls)}, проблемных: {len(broken)}",
    )
    return j(
        {
            "ok": True,
            "total_items": total_items,
            "unique_urls": len(unique_urls),
            "broken_count": len(broken),
            "broken": broken,
        }
    )


def _require_recorder_auth(request: Request) -> None:
    if not RECORDER_TOKEN:
        raise HTTPException(status_code=503, detail="recorder_token_not_configured")
    raw = (request.headers.get("x-recorder-token") or "").strip()
    if not raw:
        auth_h = (request.headers.get("authorization") or "").strip()
        if auth_h.lower().startswith("bearer "):
            raw = auth_h[7:].strip()
    if not raw or not hmac.compare_digest(raw, RECORDER_TOKEN):
        raise HTTPException(status_code=401, detail="invalid_recorder_token")


@app.post("/api/recorder/heartbeat")
def recorder_heartbeat(request: Request, body: dict[str, Any]) -> JSONResponse:
    rate_limit(request, "rec_hb", 30, 60)
    _require_recorder_auth(request)
    channel = _normalize_push_channel(body.get("channel") or RECORDER_CHANNEL)
    try:
        free_gb = round(max(0.0, float(body.get("free_gb") or 0.0)), 2)
    except (TypeError, ValueError):
        free_gb = 0.0
    payload = {
        "channel": channel,
        "hostname": text(body.get("hostname"), 80),
        "state": text(body.get("state"), 32) or "idle",
        "detail": text(body.get("detail"), 180),
        "active_vod_id": text(body.get("active_vod_id"), 160),
        "free_gb": free_gb,
        "ts": time.time(),
    }
    kv_set("recorder_heartbeat", payload)
    if 0.0 < free_gb < ALERT_MIN_FREE_GB:
        _maybe_send_admin_tg_alert(
            "home_low_disk",
            f"[krafen.me] На домашнем ПК рекордера (@{channel}) осталось {free_gb:.1f} ГБ",
            cooldown_s=21600.0,
        )
    return j({"ok": True})


@app.post("/api/recorder/vod-ready")
def recorder_vod_ready(request: Request, body: dict[str, Any]) -> JSONResponse:
    rate_limit(request, "rec_vod_ready", 12, 60)
    _require_recorder_auth(request)
    vod_id = text(body.get("vod_id"), 160)
    if not safe_name(vod_id):
        return j({"error": "invalid_vod_id"}, 400)
    channel = _normalize_push_channel(body.get("channel") or RECORDER_CHANNEL)
    title = text(body.get("title"), 240) or vod_id
    if GDRIVE_FOLDER_ID:
        threading.Thread(
            target=lambda: maybe_sync_drive_vods(force=True),
            name="vod-ready-sync",
            daemon=True,
        ).start()
    last_notified = str(kv_get("last_vod_ready_push_id") or "")
    push_res = {"sent": 0, "failed": 0, "removed": 0}
    if last_notified != vod_id:
        kv_set("last_vod_ready_push_id", vod_id)
        push_res = send_stream_push_notifications(
            channel=channel,
            title=f"{channel}: запись стрима готова",
            body_text=title[:140],
            url=f"/danonsha?vod={quote(vod_id, safe='')}",
        )
    return j({"ok": True, "vod_id": vod_id, "push": push_res})


@app.post("/api/admin/backup/telegram")
def admin_backup_telegram(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_tg_backup", 6, 60)
    ok, detail = _send_telegram_backup_zip()
    log_admin_event(request, "tg_backup", "ok" if ok else "error", f"Принудительно: {detail}")
    if not ok:
        status = 400 if detail == "telegram_not_configured" else 502
        return j({"ok": False, "error": detail}, status)
    return j({"ok": True, "filename": detail})


@app.get("/api/admin/backup/zip")
def admin_download_backup_zip(request: Request) -> Response:
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_backup_zip", 10, 60)
    zip_bytes = _build_full_server_backup_zip()
    stamp = datetime.now(MSK_TZ).strftime("%Y-%m-%d_%H-%M")
    fname = f"krafen-server-backup-{stamp}-msk.zip"
    log_admin_event(request, "backup_export_zip", "ok", f"ZIP {fname} ({len(zip_bytes)} байт)")
    return Response(
        content=zip_bytes,
        status_code=200,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(len(zip_bytes)),
            "Content-Disposition": f'attachment; filename="{fname}"',
        },
    )


@app.post("/api/suggestions")
def public_create_suggestion(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    rate_limit(request, "public_suggest", 5, 600)
    raw_kind = text(body.get("kind"), 24).lower()
    if raw_kind and raw_kind not in {"movie", "game", "other"}:
        return j({"error": "invalid_kind"}, 400)
    kind = raw_kind or "other"
    title = text(body.get("title"), 180)
    if len(title) < 2:
        return j({"error": "title_too_short"}, 400)
    raw_mt = re.sub(r"[^a-zа-яё0-9 _/-]", "", text(body.get("media_type"), 32).lower()).strip()
    if kind == "game":
        media_type = "game"
    elif kind == "movie":
        media_type = raw_mt if raw_mt in {"movie", "series", "anime"} else "movie"
    else:
        media_type = raw_mt or "other"
    title_ru = text(body.get("title_ru"), 180)
    year = text(body.get("year"), 16) or "-"
    ext_id = text(body.get("ext_id"), 64)
    image = clean_url(body.get("image")) or ""
    raw_link = text(body.get("link"), 500)
    link = ""
    if raw_link:
        parsed_link = urlsplit(raw_link)
        if (
            parsed_link.scheme not in {"http", "https"}
            or not parsed_link.netloc
            or any(ch in raw_link for ch in ("\r", "\n", "\t", " ", "<", ">", '"', "'"))
        ):
            return j({"error": "invalid_link"}, 400)
        link = raw_link
    note = text(body.get("note"), 260)
    author = text(body.get("author"), 48) or "аноним"
    now_i = int(time.time())

    with _db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO suggestions (kind, ext_id, title, title_ru, year, media_type, image, note, author, link, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (kind, ext_id, title, title_ru, year, media_type, image, note, author, link, now_i),
        )
    _maybe_send_admin_tg_alert(
        "new_suggestion",
        f"[krafen.me] Новая предложка ({media_type}): {title}"
        + (f" ({year})" if year and year != "-" else "")
        + f" от {author}"
        + (f"\nСсылка: {link}" if link else "")
        + (f"\nКоммент: {note}" if note else ""),
        cooldown_s=60.0,
    )
    return j({"ok": True})


@app.post("/api/admin/suggestions/resolve")
def admin_resolve_suggestion(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_sug_resolve", 30, 60)
    sug_id = _safe_int(body.get("id"), 0)
    action = text(body.get("action"), 16).lower()
    if sug_id <= 0 or action not in {"accept", "reject"}:
        return j({"error": "invalid_request"}, 400)

    owner = request_profile(request)
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sug_id,)).fetchone()
        if not row:
            return j({"error": "not_found"}, 404)
        sug = dict(row)
        conn.execute("UPDATE suggestions SET status = ? WHERE id = ?", ("accepted" if action == "accept" else "rejected", sug_id))

    if action == "accept" and sug["kind"] in {"game", "movie"}:
        target_col = "gamelist" if sug["kind"] == "game" else "watchlist"
        state = read_list(target_col, use_cache=False, owner=owner)
        items = list(state.get("items") or [])
        norm_title = re.sub(r"\s+", " ", str(sug["title"] or "").strip().lower())
        exists = any(re.sub(r"\s+", " ", str(x.get("title") or "").strip().lower()) == norm_title for x in items)
        if not exists:
            now_ms = int(time.time() * 1000)
            if target_col == "gamelist":
                new_item = normalize_gamelist_item(
                    {
                        "id": sug.get("ext_id") or f"sug_{sug_id}_{now_ms}",
                        "title": sug["title"],
                        "year": sug.get("year") or "-",
                        "status": "wishlist",
                        "rating": 0,
                        "review": f"Предложено: {sug.get('author') or 'зритель'}. {sug.get('note') or ''}".strip(),
                        "cover": sug.get("image") or None,
                        "genres": [],
                        "platforms": [],
                        "addedAt": now_ms,
                        "updatedAt": now_ms,
                    }
                )
            else:
                new_item = normalize_watchlist_item(
                    {
                        "id": sug.get("ext_id") or f"sug_{sug_id}_{now_ms}",
                        "title": sug["title"],
                        "titleRu": sug.get("title_ru") or None,
                        "year": sug.get("year") or "-",
                        "type": sug.get("media_type") if sug.get("media_type") in VALID_TYPES else "movie",
                        "status": "planned",
                        "poster": sug.get("image") or None,
                        "rating": 0,
                        "review": f"Предложено: {sug.get('author') or 'зритель'}. {sug.get('note') or ''}".strip(),
                        "addedAt": now_ms,
                        "updatedAt": now_ms,
                    }
                )
            if new_item:
                items.insert(0, new_item)
                update_list_atomic(target_col, int(state["revision"]), items, owner=owner)

    log_admin_event(request, "suggestion_resolve", "ok", f"#{sug_id} {action}: {sug.get('title')}")
    return j({"ok": True})


def _parse_import_rows(target: str, raw_text: str) -> list[dict[str, Any]]:
    raw = (raw_text or "").strip()
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    status_wl_map = {
        "completed": "watched", "watched": "watched", "просмотрено": "watched", "2": "watched",
        "planned": "planned", "plan_to_watch": "planned", "watching": "planned", "в планах": "planned", "1": "planned", "0": "planned",
        "dropped": "dropped", "брошено": "dropped", "4": "dropped",
    }
    status_gl_map = {
        "completed": "completed", "played": "completed", "пройдено": "completed",
        "playing": "playing", "играю": "playing",
        "paused": "paused", "on_hold": "paused",
        "dropped": "dropped", "брошено": "dropped",
        "wishlist": "wishlist", "planned": "wishlist", "в планах": "wishlist",
    }

    # 1) JSON (Shikimori user_rates export, Steam GetOwnedGames, generic list)
    if raw.startswith(("[", "{")):
        try:
            parsed = json.loads(raw)
            rows = parsed if isinstance(parsed, list) else (
                parsed.get("items") or parsed.get("response", {}).get("games") or parsed.get("games") or []
            )
            if isinstance(rows, list):
                for row in rows[:250]:
                    if not isinstance(row, dict):
                        continue
                    anime_obj = row.get("anime") if isinstance(row.get("anime"), dict) else {}
                    title = text(
                        row.get("title") or row.get("name") or anime_obj.get("name") or row.get("target_title"),
                        180,
                    )
                    if not title:
                        continue
                    title_ru = text(row.get("titleRu") or anime_obj.get("russian"), 180)
                    raw_st = str(row.get("status") or "").strip().lower()
                    if target == "gamelist":
                        playtime = int(row.get("playtime_forever") or 0)
                        st = status_gl_map.get(raw_st) or ("completed" if playtime >= 120 else "wishlist")
                    else:
                        st = status_wl_map.get(raw_st) or "planned"
                    raw_type = str(row.get("type") or ("anime" if anime_obj else ("game" if target == "gamelist" else "movie"))).lower()
                    out.append(
                        {
                            "title": title,
                            "titleRu": title_ru or None,
                            "year": text(row.get("year"), 10) or year_from(anime_obj.get("aired_on")) or "-",
                            "type": raw_type,
                            "status": st,
                            "rating": float(row.get("score") or row.get("rating") or 0),
                            "poster": clean_url(row.get("poster") or row.get("cover") or row.get("image")),
                        }
                    )
                return out
        except Exception:
            pass

    # 2) XML (MyAnimeList / Shikimori XML export <anime>...</anime>)
    if "<anime>" in raw.lower():
        try:
            import xml.etree.ElementTree as ET
            root_xml = ET.fromstring(raw)
            for node in root_xml.findall(".//anime")[:250]:
                t_el = node.find("series_title") or node.find("title")
                s_el = node.find("my_status")
                sc_el = node.find("my_score")
                title = text(t_el.text if t_el is not None else "", 180)
                if not title:
                    continue
                st_raw = str((s_el.text if s_el is not None else "") or "").strip().lower()
                st = status_wl_map.get(st_raw, "planned")
                rating = _safe_int(sc_el.text if sc_el is not None else 0, 0)
                out.append({"title": title, "year": "-", "type": "anime", "status": st, "rating": rating})
            if out:
                return out
        except Exception:
            pass

    # 3) CSV / plain lines ("title,type,status,rating,year" or plain titles one per line)
    import csv
    lines = [ln for ln in raw.splitlines() if ln.strip()][:250]
    if lines and ("," in lines[0] or ";" in lines[0]):
        delim = ";" if lines[0].count(";") > lines[0].count(",") else ","
        reader = csv.reader(lines, delimiter=delim)
        for row_idx, cols in enumerate(reader):
            if not cols or not cols[0].strip():
                continue
            first_low = cols[0].strip().lower()
            if row_idx == 0 and first_low in {"title", "name", "название"}:
                continue
            title = text(cols[0], 180)
            col1 = text(cols[1], 24).lower() if len(cols) > 1 else ""
            col2 = text(cols[2], 24).lower() if len(cols) > 2 else ""
            col3 = cols[3].strip() if len(cols) > 3 else "0"
            col4 = text(cols[4], 10) if len(cols) > 4 else "-"
            if target == "gamelist":
                st = status_gl_map.get(col1) or status_gl_map.get(col2) or "completed"
                out.append({"title": title, "type": "game", "status": st, "rating": col3, "year": col4 or "-"})
            else:
                tp = col1 if col1 in VALID_TYPES else "movie"
                st = status_wl_map.get(col2) or status_wl_map.get(col1) or "watched"
                out.append({"title": title, "type": tp, "status": st, "rating": col3, "year": col4 or "-"})
        return out

    for ln in lines:
        out.append({
            "title": text(ln, 180),
            "type": "game" if target == "gamelist" else "movie",
            "status": "wishlist" if target == "gamelist" else "planned",
            "rating": 0,
            "year": "-",
        })
    return out


@app.post("/api/admin/import-list")
def admin_import_list(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    require_admin(request)
    rate_limit(request, "admin_import_list", 10, 60)
    target = text(body.get("target"), 20).lower()
    if target not in {"watchlist", "gamelist"}:
        return j({"error": "invalid_target"}, 400)
    raw = str(body.get("raw") or "")
    enrich = bool(body.get("enrich", True))
    parsed = _parse_import_rows(target, raw)
    if not parsed:
        return j({"error": "empty_or_unrecognized"}, 400)

    owner = request_profile(request)
    state = read_list(target, use_cache=False, owner=owner)
    existing_items = list(state.get("items") or [])
    seen_titles = {re.sub(r"\s+", " ", str(i.get("title") or "").strip().lower()) for i in existing_items}

    added_items: list[dict[str, Any]] = []
    skipped = 0
    enriched_cnt = 0
    now_ms = int(time.time() * 1000)

    for idx, row in enumerate(parsed):
        t_clean = text(row.get("title"), 180)
        t_norm = re.sub(r"\s+", " ", t_clean.lower())
        if not t_clean or t_norm in seen_titles:
            skipped += 1
            continue
        seen_titles.add(t_norm)
        poster = row.get("poster")
        year_v = row.get("year") or "-"
        title_ru = row.get("titleRu")
        ext_id = f"imp_{target}_{now_ms}_{idx}"

        if enrich and not poster and enriched_cnt < 15:
            try:
                if target == "gamelist":
                    resp = search_igdb(t_clean)
                elif row.get("type") == "anime":
                    resp = search_shikimori(t_clean)
                else:
                    tp = row.get("type") if row.get("type") in {"movie", "series"} else "movie"
                    resp = search_tmdb(t_clean, tp)
                if getattr(resp, "status_code", 500) == 200:
                    res_data = json.loads(resp.body.decode("utf-8"))
                    first = (res_data.get("results") or [None])[0]
                    if isinstance(first, dict):
                        ext_id = first.get("id") or ext_id
                        poster = first.get("cover") or first.get("poster") or poster
                        year_v = first.get("year") or year_v
                        title_ru = first.get("titleRu") or title_ru
                        enriched_cnt += 1
            except Exception:
                pass

        if target == "gamelist":
            norm_item = normalize_gamelist_item(
                {
                    "id": ext_id,
                    "title": t_clean,
                    "year": year_v,
                    "status": row.get("status") or "wishlist",
                    "rating": row.get("rating") or 0,
                    "review": "",
                    "cover": poster,
                    "addedAt": now_ms + idx,
                    "updatedAt": now_ms + idx,
                }
            )
        else:
            norm_item = normalize_watchlist_item(
                {
                    "id": ext_id,
                    "title": t_clean,
                    "titleRu": title_ru,
                    "year": year_v,
                    "type": row.get("type") if row.get("type") in VALID_TYPES else "movie",
                    "status": row.get("status") or "planned",
                    "poster": poster,
                    "rating": row.get("rating") or 0,
                    "review": "",
                    "addedAt": now_ms + idx,
                    "updatedAt": now_ms + idx,
                }
            )
        if norm_item:
            added_items.append(norm_item)

    if added_items:
        merged = (added_items + existing_items)[: LIST_SPECS[target]["max_items"]]
        update_list_atomic(target, int(state["revision"]), merged, owner=owner)
    log_admin_event(request, "list_import", "ok", f"{owner}/{target}: +{len(added_items)}, skip {skipped}")
    return j({"ok": True, "added": len(added_items), "skipped": skipped, "enriched": enriched_cnt})


@app.post("/api/admin/spotify/disconnect")
def admin_spotify_disconnect(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_spotify_disc", 10, 60)
    tokens = read_tokens()
    tokens.pop("spotify_refresh_token", None)
    write_tokens(tokens)
    with _api_cache_lock:
        for key in ("spotify", "spotify_now", "spotify_recent", "lastfm"):
            _api_cache.pop(key, None)
    log_admin_event(request, "spotify_disconnect", "ok", "Отключён токен Spotify")
    return j({"ok": True})


@app.get("/api/admin/backup")
def admin_export_backup(request: Request) -> Response:
    profile = request_profile(request)
    require_admin(request, profile)
    rate_limit(request, "admin_backup", 15, 60)
    snapshot: dict[str, Any] = {
        "schema": "krafen-backup-v1",
        "exported_at": int(time.time()),
        "exported_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collections": {
            name: read_list(name, use_cache=False, owner=profile) for name in LIST_SPECS
        },
    }
    if profile == DEFAULT_PROFILE:
        snapshot["vod_overrides"] = read_vod_overrides()
    raw = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"krafen-backup-{time.strftime('%Y-%m-%d')}.json"
    log_admin_event(request, "backup_export", "ok", f"Экспорт {fname} ({len(raw)} байт)")
    return Response(
        content=raw,
        status_code=200,
        media_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{fname}"',
        },
    )


@app.post("/api/admin/restore")
def admin_import_backup(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    profile = request_profile(request)
    require_admin(request, profile)
    rate_limit(request, "admin_restore", 6, 60)
    cols = body.get("collections")
    if not isinstance(cols, dict):
        return j({"error": "invalid_backup_format"}, 400)

    restored_counts: dict[str, int] = {}
    pending_cache: list[tuple[str, int, list[dict[str, Any]]]] = []
    now = int(time.time())
    with _db_lock, db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, spec in LIST_SPECS.items():
                if name not in cols:
                    continue
                entry = cols[name]
                raw_items = entry.get("items") if isinstance(entry, dict) else (entry if isinstance(entry, list) else None)
                if not isinstance(raw_items, list):
                    continue
                norm_fn = NORMALIZERS[spec["normalizer"]]
                cleaned = [x for x in (norm_fn(it) for it in raw_items) if x is not None][: spec["max_items"]]
                # Непустой бэкап, из которого не выжила ни одна запись, — это повреждённый
                # файл, а не намеренная очистка: иначе импорт молча стирает коллекцию.
                if raw_items and not cleaned:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    return j({"error": "invalid_items", "collection": name}, 400)
                row = conn.execute(
                    "SELECT revision FROM collection_meta WHERE owner = ? AND name = ?", (profile, name)
                ).fetchone()
                next_rev = (max(0, int(row["revision"] or 0)) if row else 0) + 1
                stored = _write_collection_items(conn, name, cleaned, profile)
                conn.execute(
                    """
                    INSERT INTO collection_meta (owner, name, revision, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(owner, name) DO UPDATE SET revision = excluded.revision, updated_at = excluded.updated_at
                    """,
                    (profile, name, next_rev, now),
                )
                pending_cache.append((name, next_rev, list(stored)))
                restored_counts[name] = len(stored)
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            return j({"error": "restore_failed"}, 500)

    with _list_cache_lock:
        for name, next_rev, stored in pending_cache:
            _list_cache[collection_key(profile, name)] = {"ts": time.time(), "revision": next_rev, "items": stored}

    raw_overrides = body.get("vod_overrides")
    if profile == DEFAULT_PROFILE and isinstance(raw_overrides, dict):
        safe_ov: dict[str, dict[str, Any]] = {}
        for k, v in list(raw_overrides.items())[:500]:
            vid = text(k, 160)
            if safe_name(vid) and isinstance(v, dict):
                safe_ov[vid] = {
                    "custom_title": text(v.get("custom_title"), 260),
                    "custom_note": text(v.get("custom_note"), 180),
                    "hidden": bool(v.get("hidden")),
                    "updated_at": now,
                }
        write_vod_overrides(safe_ov)

    summary_str = ", ".join(f"{k}:{v}" for k, v in restored_counts.items()) or "пусто"
    log_admin_event(request, "backup_restore", "ok", f"Восстановлено ({summary_str})")
    return j({"ok": True, "restored": restored_counts})


_LOG_REDACT_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{8,}|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)",
    flags=re.IGNORECASE,
)


@app.get("/api/admin/logs")
def admin_read_logs(request: Request, lines: int = 150) -> JSONResponse:
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_logs", 60, 60)
    max_lines = max(20, min(600, int(lines or 150)))
    if not HEADLESS_LOG_PATH.is_file():
        return j({"ok": True, "exists": False, "size_bytes": 0, "lines": []})
    try:
        st_size = HEADLESS_LOG_PATH.stat().st_size
        with open(HEADLESS_LOG_PATH, "rb") as fh:
            if st_size > 196_608:
                fh.seek(-196_608, os.SEEK_END)
            raw_tail = fh.read().decode("utf-8", errors="replace")
        tail_lines = [
            _LOG_REDACT_RE.sub("[REDACTED]", ln)
            for ln in raw_tail.splitlines()[-max_lines:]
        ]
        return j({"ok": True, "exists": True, "size_bytes": st_size, "lines": tail_lines})
    except OSError:
        return j({"ok": True, "exists": False, "size_bytes": 0, "lines": []})


# ----------------------------- Поиск (TMDB / Shikimori / IGDB) -----------------------------


def tmdb_poster(path: Any) -> str | None:
    value = text(path, 120)
    return f"{TMDB_IMAGE_BASE}{value}" if re.fullmatch(r"/[A-Za-z0-9_.-]+", value) else None


def igdb_cover(image_id: Any) -> str | None:
    value = text(image_id, 80)
    return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{value}.jpg" if re.fullmatch(r"[A-Za-z0-9_-]+", value) else None


def search_tmdb(query: str, item_type: str) -> JSONResponse:
    if not TMDB_ACCESS_TOKEN and not TMDB_API_KEY:
        return j({"error": "tmdb_key_missing"}, 503)
    endpoint = "/3/search/tv" if item_type == "series" else "/3/search/movie"
    params: dict[str, Any] = {"query": query, "language": "ru-RU", "include_adult": "false", "page": 1}
    headers = {"Accept": "application/json"}
    if TMDB_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_ACCESS_TOKEN.removeprefix('Bearer ')}"
    else:
        params["api_key"] = TMDB_API_KEY
    try:
        res = requests.get(f"{TMDB_BASE}{endpoint}", params=params, headers=headers, timeout=15)
        data = res.json() if res.content else {}
        if not res.ok:
            return j({"error": "tmdb_unavailable"}, 502)
        results = []
        for item in (data.get("results") or [])[:10]:
            localized = text(item.get("name") if item_type == "series" else item.get("title"))
            original = text(item.get("original_name") if item_type == "series" else item.get("original_title")) or localized
            if not original:
                continue
            results.append(
                {
                    "id": f"tmdb_{'tv' if item_type == 'series' else 'movie'}_{text(item.get('id'), 40)}",
                    "title": original,
                    "titleRu": localized if localized and localized != original else None,
                    "year": year_from(item.get("first_air_date") if item_type == "series" else item.get("release_date")),
                    "type": item_type,
                    "poster": clean_url(tmdb_poster(item.get("poster_path"))),
                }
            )
        return j({"results": results})
    except Exception:
        return j({"error": "tmdb_unavailable"}, 502)


def search_shikimori(query: str) -> JSONResponse:
    try:
        res = requests.get(
            f"{SHIKIMORI_BASE}/api/animes",
            params={"search": query, "limit": 10, "order": "popularity"},
            headers={"Accept": "application/json", "User-Agent": "krafen-site/2.0"},
            timeout=15,
        )
        data = res.json() if res.content else []
        if not res.ok or not isinstance(data, list):
            return j({"error": "shikimori_unavailable"}, 502)
        results = []
        for item in data:
            item_id = text(item.get("id"), 40)
            title = text(item.get("name"))
            if not item_id or not title:
                continue
            image = item.get("image") if isinstance(item.get("image"), dict) else {}
            original = text(image.get("original"), 300)
            poster_url = None
            if original:
                poster_url = clean_url(original if original.startswith("https://") else f"{SHIKIMORI_BASE}{original}")
            results.append(
                {
                    "id": f"sh_{item_id}",
                    "title": title,
                    "titleRu": text(item.get("russian")) or None,
                    "year": year_from(item.get("aired_on")),
                    "type": "anime",
                    "poster": poster_url,
                }
            )
        return j({"results": results})
    except Exception:
        return j({"error": "shikimori_unavailable"}, 502)


@app.get("/api/search")
def search(request: Request, q: str = "", type: str = "") -> JSONResponse:
    rate_limit(request, "search", 60, 60)
    query = text(q, 80)
    item_type = text(type, 20)
    if len(query) < 2:
        return j({"results": []})
    if item_type == "anime":
        return search_shikimori(query)
    if item_type in {"movie", "series"}:
        return search_tmdb(query, item_type)
    return j({"error": "invalid_type"}, 400)


def get_igdb_token(force_refresh: bool = False) -> str:
    global _igdb_token
    if IGDB_ACCESS_TOKEN:
        return IGDB_ACCESS_TOKEN.removeprefix("Bearer ")
    if not force_refresh and _igdb_token.get("access_token") and _igdb_token.get("expires_at", 0) > time.time() + 60:
        return _igdb_token["access_token"]
    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        return ""
    res = requests.post(
        TWITCH_TOKEN_URL,
        data={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        return ""
    expires_in = int(data.get("expires_in") or 3600)
    _igdb_token = {"access_token": data["access_token"], "expires_at": time.time() + expires_in - 300}
    return data["access_token"]


def search_igdb(query: str) -> JSONResponse:
    client_id = IGDB_CLIENT_ID
    if not client_id or (not IGDB_ACCESS_TOKEN and not IGDB_CLIENT_SECRET):
        return j({"error": "igdb_key_missing"}, 503)
    try:
        token = get_igdb_token()
        if not token:
            return j({"error": "igdb_unavailable"}, 502)
        safe_query = text(query, 80).replace("\\", "\\\\").replace('"', '\\"')
        body = (
            "fields name,cover.image_id,genres.name,platforms.abbreviation,platforms.name,first_release_date,category; "
            f'search "{safe_query}"; where version_parent = null; limit 10;'
        )
        res = requests.post(
            f"{IGDB_BASE}/games",
            data=body,
            headers={"Accept": "application/json", "Client-ID": client_id, "Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if res.status_code == 401 and not IGDB_ACCESS_TOKEN:
            token = get_igdb_token(force_refresh=True)
            if token:
                res = requests.post(
                    f"{IGDB_BASE}/games",
                    data=body,
                    headers={"Accept": "application/json", "Client-ID": client_id, "Authorization": f"Bearer {token}"},
                    timeout=15,
                )
        data = res.json() if res.content else []
        if not res.ok or not isinstance(data, list):
            return j({"error": "igdb_unavailable"}, 502)
        results = []
        for item in data:
            platforms = [
                text((p or {}).get("abbreviation") or (p or {}).get("name"), 24)
                for p in (item.get("platforms") or [])
                if isinstance(p, dict)
            ]
            genres = [text((g or {}).get("name"), 28) for g in (item.get("genres") or []) if isinstance(g, dict)]
            cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
            raw_id = text(item.get("id"), 40)
            title = text(item.get("name"), 180)
            if not raw_id or not title:
                continue
            results.append(
                {
                    "id": f"igdb_{raw_id}",
                    "igdbId": int(raw_id) if raw_id.isdigit() else None,
                    "title": title,
                    "year": year_from_unix(item.get("first_release_date")),
                    "type": "game",
                    "cover": igdb_cover(cover.get("image_id")),
                    "genres": [value for value in genres if value][:3],
                    "platforms": [value for value in platforms if value][:4],
                    "category": int(item.get("category") or 0),
                }
            )
        return j({"results": results})
    except Exception:
        return j({"error": "igdb_unavailable"}, 502)


@app.get("/api/game-search")
def game_search(request: Request, q: str = "") -> JSONResponse:
    rate_limit(request, "game_search", 60, 60)
    query = text(q, 80)
    if len(query) < 2:
        return j({"results": []})
    return search_igdb(query)


# ----------------------------- Музыка (Spotify + Last.fm) -----------------------------


def _parse_lastfm_track_item(raw_track: Any) -> dict[str, Any] | None:
    if not isinstance(raw_track, dict):
        return None
    artist = raw_track.get("artist") if isinstance(raw_track.get("artist"), dict) else {}
    album = raw_track.get("album") if isinstance(raw_track.get("album"), dict) else {}
    images = raw_track.get("image") if isinstance(raw_track.get("image"), list) else []
    image = None
    for image_item in reversed(images):
        if isinstance(image_item, dict) and image_item.get("#text"):
            image = clean_url(image_item.get("#text"))
            break
    attrs = raw_track.get("@attr") if isinstance(raw_track.get("@attr"), dict) else {}
    date_obj = raw_track.get("date") if isinstance(raw_track.get("date"), dict) else {}
    return {
        "name": text(raw_track.get("name"), 200),
        "artist": text(artist.get("#text"), 200),
        "album": text(album.get("#text"), 200),
        "image": image,
        "url": clean_url(raw_track.get("url")),
        "nowPlaying": attrs.get("nowplaying") == "true",
        "playedAt": _safe_int(date_obj.get("uts"), 0),
    }


@app.get("/api/lastfm")
def lastfm(request: Request) -> JSONResponse:
    rate_limit(request, "lastfm", 60, 60)
    if not LASTFM_API_KEY or not LASTFM_USER:
        return j({"error": "lastfm_key_missing"}, 503)

    hit = cached_api_get("lastfm")
    if hit:
        return j(hit[1], hit[0])

    try:
        res = requests.get(
            LASTFM_API_BASE,
            params={
                "method": "user.getrecenttracks",
                "user": LASTFM_USER,
                "api_key": LASTFM_API_KEY,
                "format": "json",
                "limit": 8,
            },
            timeout=15,
        )
        data = res.json() if res.content else {}
        if not res.ok or data.get("error"):
            return j({"error": "lastfm_unavailable"}, 502)
        recent = data.get("recenttracks") if isinstance(data.get("recenttracks"), dict) else {}
        raw_list = recent.get("track")
        items_raw = raw_list if isinstance(raw_list, list) else ([raw_list] if isinstance(raw_list, dict) else [])
        parsed_tracks = [t for t in (_parse_lastfm_track_item(x) for x in items_raw[:8]) if t and t.get("name")]
        track = parsed_tracks[0] if parsed_tracks else None
        history = parsed_tracks[1:7] if len(parsed_tracks) > 1 else []
        payload = {"track": track, "recent": history}
        cached_api_set("lastfm", 5.0, 200, payload)
        return j(payload)
    except Exception:
        return j({"error": "lastfm_unavailable"}, 502)


def spotify_basic_auth() -> str:
    raw = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def request_origin(request: Request) -> str:
    if PUBLIC_ORIGIN:
        return PUBLIC_ORIGIN
    proto = "https" if is_https_request(request) else "http"
    host = _effective_request_host(request)
    return f"{proto}://{host}"


def spotify_callback_url(request: Request) -> str:
    return f"{request_origin(request)}/api/spotify/callback"


@app.get("/api/spotify/auth")
def spotify_auth(request: Request):
    require_admin(request, DEFAULT_PROFILE)
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return j({"error": "spotify_credentials_missing"}, 503)
    state = b64url(secrets.token_bytes(18))
    callback = spotify_callback_url(request)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": SPOTIFY_CLIENT_ID,
            "redirect_uri": callback,
            "scope": "user-read-playback-state user-read-recently-played",
            "state": state,
        }
    )
    response = RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}", status_code=302)
    response.headers["Set-Cookie"] = cookie_header(request, SPOTIFY_STATE_COOKIE, state, 600, "/api/spotify/callback")
    return response


@app.get("/api/spotify/callback")
def spotify_callback(request: Request, state: str = "", code: str = "", error: str = "") -> Response:
    cookie_state = request.cookies.get(SPOTIFY_STATE_COOKIE, "")
    clear_cookie = cookie_header(request, SPOTIFY_STATE_COOKIE, "", 0, "/api/spotify/callback")
    if not cookie_state or not state or not constant_time_equal(cookie_state, state):
        return PlainTextResponse(
            "Проверка состояния авторизации Spotify не прошла. Вернитесь на сайт и попробуйте снова.",
            status_code=400,
            headers={"Set-Cookie": clear_cookie},
        )
    if error:
        response = RedirectResponse("/?spotify=error", status_code=303)
        response.headers["Set-Cookie"] = clear_cookie
        return response
    if not code or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return PlainTextResponse("Не заданы SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET.", status_code=503, headers={"Set-Cookie": clear_cookie})
    try:
        res = requests.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": spotify_callback_url(request),
            },
            headers={"Authorization": spotify_basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        data = res.json() if res.content else {}
        if not res.ok or not data.get("refresh_token"):
            return PlainTextResponse(
                "Не удалось подключить Spotify. Проверьте Redirect URI и секреты в .env.",
                status_code=502,
                headers={"Set-Cookie": clear_cookie},
            )
        tokens = read_tokens()
        tokens["spotify_refresh_token"] = data["refresh_token"]
        tokens["spotify_access_token"] = {
            "access_token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in") or 3600),
        }
        write_tokens(tokens)
        response = RedirectResponse("/?spotify=connected", status_code=303)
        response.headers["Set-Cookie"] = clear_cookie
        return response
    except Exception:
        return PlainTextResponse(
            "Ошибка соединения при подключении Spotify.",
            status_code=502,
            headers={"Set-Cookie": clear_cookie},
        )


def get_spotify_access_token(force_refresh: bool = False) -> str:
    tokens = read_tokens()
    cached = tokens.get("spotify_access_token") if isinstance(tokens.get("spotify_access_token"), dict) else None
    if not force_refresh and cached and cached.get("access_token") and int(cached.get("expires_at") or 0) > time.time() + 60:
        return cached["access_token"]
    refresh_token = text(tokens.get("spotify_refresh_token"), 4000)
    if not refresh_token or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return ""
    res = requests.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={
            "Authorization": spotify_basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        return ""
    if data.get("refresh_token"):
        tokens["spotify_refresh_token"] = data["refresh_token"]
    tokens["spotify_access_token"] = {
        "access_token": data["access_token"],
        "expires_at": time.time() + int(data.get("expires_in") or 3600),
    }
    write_tokens(tokens)
    return data["access_token"]


def _parse_spotify_track_item(
    item: Any,
    *,
    now_playing: bool = False,
    current_playback: bool = False,
    progress_ms: Any = None,
    played_at_iso: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    artists = item.get("artists") if isinstance(item.get("artists"), list) else []
    images = album.get("images") if isinstance(album.get("images"), list) else []
    ext_urls = item.get("external_urls") if isinstance(item.get("external_urls"), dict) else {}
    played_ts = 0
    if isinstance(played_at_iso, str) and played_at_iso:
        with contextlib.suppress(ValueError):
            played_ts = int(datetime.fromisoformat(played_at_iso.replace("Z", "+00:00")).timestamp())
    return {
        "name": text(item.get("name"), 200),
        "artist": text(", ".join(a.get("name") for a in artists if isinstance(a, dict) and a.get("name")), 200),
        "album": text(album.get("name"), 200),
        "image": clean_url(images[0].get("url")) if images and isinstance(images[0], dict) else None,
        "url": clean_url(ext_urls.get("spotify")),
        "nowPlaying": now_playing,
        "currentPlayback": current_playback,
        "progressMs": progress_ms,
        "durationMs": item.get("duration_ms"),
        "playedAt": played_ts,
    }


def _get_spotify_recent_tracks(token: str) -> list[dict[str, Any]]:
    hit = cached_api_get("spotify_recent")
    if hit and isinstance(hit[1], list):
        return hit[1]
    if not token:
        return []
    try:
        recent_res = requests.get(
            f"{SPOTIFY_API_BASE}/me/player/recently-played?limit=7",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if not recent_res.ok:
            return []
        recent = recent_res.json() if recent_res.content else {}
        items = recent.get("items") if isinstance(recent.get("items"), list) else []
        parsed: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            tr = _parse_spotify_track_item(
                entry.get("track"),
                now_playing=False,
                current_playback=False,
                played_at_iso=entry.get("played_at"),
            )
            if tr and tr.get("name"):
                parsed.append(tr)
        cached_api_set("spotify_recent", 60.0, 200, parsed)
        return parsed
    except Exception:
        return []


@app.get("/api/spotify")
def spotify(request: Request) -> JSONResponse:
    rate_limit(request, "spotify", 60, 60)

    hit = cached_api_get("spotify")
    if hit:
        return j(hit[1], hit[0])

    try:
        token = get_spotify_access_token()
    except Exception:
        token = ""
    if not token:
        return j({"error": "spotify_not_connected"}, 503)

    try:
        res = requests.get(f"{SPOTIFY_API_BASE}/me/player", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if res.status_code == 401:
            token = get_spotify_access_token(force_refresh=True)
            if token:
                res = requests.get(f"{SPOTIFY_API_BASE}/me/player", headers={"Authorization": f"Bearer {token}"}, timeout=15)

        item = None
        now_playing = False
        current_playback = False
        progress_ms = None
        recent_tracks: list[dict[str, Any]] = []
        if res.status_code == 200:
            data = res.json() if res.content else {}
            item = data.get("item") if isinstance(data.get("item"), dict) else None
            now_playing = bool(data.get("is_playing"))
            current_playback = bool(item)
            progress_ms = data.get("progress_ms")
            recent_tracks = _get_spotify_recent_tracks(token)
        elif res.status_code == 204:
            recent_tracks = _get_spotify_recent_tracks(token)
            if recent_tracks:
                track_top = recent_tracks[0]
                history = recent_tracks[1:7]
                payload = {"track": track_top, "recent": history}
                cached_api_set("spotify", 3.0, 200, payload)
                return j(payload)
            recent_res = requests.get(
                f"{SPOTIFY_API_BASE}/me/player/recently-played?limit=7",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            recent = recent_res.json() if recent_res.content else {}
            if not recent_res.ok:
                return j({"error": "spotify_recently_played_http_error", "status": recent_res.status_code}, 502)
            items = recent.get("items") if isinstance(recent.get("items"), list) else []
            item = items[0].get("track") if items and isinstance(items[0], dict) and isinstance(items[0].get("track"), dict) else None
        else:
            err = "spotify_reauthorization_required" if res.status_code == 401 else "spotify_playback_http_error"
            return j({"error": err, "status": res.status_code}, 502)

        track = _parse_spotify_track_item(
            item,
            now_playing=now_playing,
            current_playback=current_playback,
            progress_ms=progress_ms,
        )
        history = [
            t
            for t in recent_tracks
            if not track or (t.get("name"), t.get("artist")) != (track.get("name"), track.get("artist"))
        ][:6]
        payload = {"track": track, "recent": history}
        cached_api_set("spotify", 3.0, 200, payload)
        return j(payload)
    except Exception:
        return j({"error": "spotify_failed"}, 502)


# ----------------------------- Twitch & Discord -----------------------------


def twitch_app_token(force_refresh: bool = False) -> str:
    global _twitch_token
    if not force_refresh and _twitch_token.get("token") and _twitch_token.get("expires_at", 0) > time.time() + 60:
        return _twitch_token["token"]
    res = requests.post(
        TWITCH_TOKEN_URL,
        data={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    data = res.json() if res.content else {}
    if not res.ok or not data.get("access_token"):
        raise RuntimeError("twitch_token_failed")
    _twitch_token = {"token": data["access_token"], "expires_at": time.time() + int(data.get("expires_in") or 3600)}
    return data["access_token"]


def helix(path: str, **params: Any) -> list[dict[str, Any]]:
    token = twitch_app_token()
    res = requests.get(
        f"https://api.twitch.tv/helix{path}",
        params=params,
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if res.status_code == 401:
        token = twitch_app_token(force_refresh=True)
        res = requests.get(
            f"https://api.twitch.tv/helix{path}",
            params=params,
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
            timeout=15,
        )
    if not res.ok:
        raise RuntimeError(f"twitch_http_{res.status_code}")
    data = res.json() if res.content else {}
    return data.get("data") if isinstance(data.get("data"), list) else []


def twitch_status(login: str) -> tuple[int, dict[str, Any]]:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return 503, {"error": "twitch_key_missing"}
    cache_key = f"twitch:{login.lower()}"
    hit = cached_api_get(cache_key)
    if hit:
        return hit
    try:
        users = helix("/users", login=login)
        streams = helix("/streams", user_login=login)
        user = users[0] if users else {}
        stream = streams[0] if streams else None
        payload = {
            "live": bool(stream),
            "login": user.get("login", login),
            "displayName": user.get("display_name", login),
            "description": user.get("description", ""),
            "profileImage": clean_url(user.get("profile_image_url")) or "",
            "viewCount": user.get("view_count", 0),
            "url": f"https://www.twitch.tv/{login}",
            "stream": (
                {
                    "title": stream.get("title"),
                    "gameName": stream.get("game_name"),
                    "viewerCount": stream.get("viewer_count", 0),
                    "startedAt": stream.get("started_at"),
                }
                if stream
                else None
            ),
        }
        cached_api_set(cache_key, 60.0, 200, payload)
        return 200, payload
    except Exception:
        return 502, {"error": "twitch_failed"}


@app.get("/api/twitch")
def twitch(request: Request, login: str = TWITCH_CHANNEL) -> JSONResponse:
    rate_limit(request, "twitch", 60, 60)
    clean_login = re.sub(r"[^A-Za-z0-9_]", "", text(login, 40)) or TWITCH_CHANNEL
    status_code, payload = twitch_status(clean_login)
    return j(payload, status_code)


# ----------------------------- Web Push (уведомления о стриме) -----------------------------

_push_vapid_lock = threading.Lock()
_cached_vapid_pair: tuple[Any, str] | None = None
_twitch_push_poller_started = False


def resolve_vapid_keys() -> tuple[Any, str]:
    global _cached_vapid_pair
    if not _WEBPUSH_AVAILABLE or _VapidCls is None or _crypto_ser is None:
        return None, ""
    with _push_vapid_lock:
        if _cached_vapid_pair is not None:
            return _cached_vapid_pair
        env_priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
        env_pub = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
        if env_priv and env_pub:
            try:
                v = (
                    _VapidCls.from_pem(env_priv.encode("utf-8"))
                    if "-----BEGIN" in env_priv
                    else _VapidCls.from_string(env_priv)
                )
                _cached_vapid_pair = (v, env_pub)
                return _cached_vapid_pair
            except Exception:
                pass
        stored = kv_get("vapid_keys")
        if isinstance(stored, dict) and stored.get("private_pem") and stored.get("public_key"):
            try:
                v = _VapidCls.from_pem(str(stored["private_pem"]).encode("utf-8"))
                _cached_vapid_pair = (v, str(stored["public_key"]))
                return _cached_vapid_pair
            except Exception:
                pass
        v = _VapidCls()
        v.generate_keys()
        priv_pem = v.private_pem().decode("utf-8")
        raw_pub = v.public_key.public_bytes(
            encoding=_crypto_ser.Encoding.X962,
            format=_crypto_ser.PublicFormat.UncompressedPoint,
        )
        pub_b64 = b64url(raw_pub)
        kv_set("vapid_keys", {"private_pem": priv_pem, "public_key": pub_b64})
        _cached_vapid_pair = (v, pub_b64)
        return _cached_vapid_pair


_ALLOWED_PUSH_HOSTS = {
    "fcm.googleapis.com",
    "android.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
    "updates-push.services.mozilla.com",
}
_ALLOWED_PUSH_SUFFIXES = (
    ".notify.windows.com",
    ".push.apple.com",
    ".push.samsungosp.com",
    ".push.opera.com",
)


def _is_safe_push_endpoint(raw_url: Any) -> str | None:
    cleaned = clean_url(raw_url, 2000)
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    if parsed.port is not None and parsed.port != 443:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return None
    if host not in _ALLOWED_PUSH_HOSTS and not any(host.endswith(sfx) for sfx in _ALLOWED_PUSH_SUFFIXES):
        return None
    try:
        ip = ipaddress.ip_address(host)
        return cleaned if ip.is_global else None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    for info in infos:
        addr = info[4][0]
        try:
            resolved_ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if not resolved_ip.is_global:
            return None
    return cleaned


def _normalize_push_channel(raw: Any) -> str:
    allowed = {TWITCH_CHANNEL.lower(), RECORDER_CHANNEL.lower(), "krafen", "danonsha"}
    ch = re.sub(r"[^a-z0-9_]", "", text(raw, 40).lower())
    return ch if ch in allowed else TWITCH_CHANNEL.lower()


def send_stream_push_notifications(channel: str, title: str, body_text: str, url: str) -> dict[str, int]:
    vapid_inst, _ = resolve_vapid_keys()
    if vapid_inst is None or _send_webpush is None:
        return {"sent": 0, "failed": 0, "removed": 0}
    with _db_lock, auth_db_connect() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth, channels_json FROM push_subscriptions").fetchall()
    if not rows:
        return {"sent": 0, "failed": 0, "removed": 0}

    payload = json.dumps(
        {
            "title": title,
            "body": body_text,
            "url": url,
            "icon": "/icon-192.png",
            "badge": "/favicon.png",
            "tag": f"twitch-live-{channel}",
        },
        ensure_ascii=False,
    )
    sent = 0
    failed = 0
    dead_endpoints: list[str] = []
    push_session = requests.Session()
    push_session.max_redirects = 0
    try:
        for r in rows:
            try:
                channels = json.loads(r["channels_json"] or "[]")
            except Exception:
                channels = []
            if channel != "*" and channel not in channels and "all" not in channels:
                continue
            safe_ep = _is_safe_push_endpoint(r["endpoint"])
            if not safe_ep:
                dead_endpoints.append(r["endpoint"])
                continue
            sub_info = {
                "endpoint": safe_ep,
                "keys": {"p256dh": r["p256dh"], "auth": r["auth"]},
            }
            try:
                _send_webpush(
                    subscription_info=sub_info,
                    data=payload,
                    vapid_private_key=vapid_inst,
                    vapid_claims={"sub": VAPID_SUBJECT},
                    timeout=10,
                    requests_session=push_session,
                )
                sent += 1
            except _WebPushError as exc:
                status = getattr(exc, "status_code", None)
                if status in (404, 410):
                    dead_endpoints.append(r["endpoint"])
                else:
                    failed += 1
            except Exception:
                failed += 1
    finally:
        push_session.close()

    if dead_endpoints:
        with _db_lock, auth_db_connect() as conn:
            conn.executemany("DELETE FROM push_subscriptions WHERE endpoint = ?", [(ep,) for ep in dead_endpoints])
    return {"sent": sent, "failed": failed, "removed": len(dead_endpoints)}


def _start_twitch_push_poller() -> None:
    global _twitch_push_poller_started
    if _twitch_push_poller_started:
        return
    if not _WEBPUSH_AVAILABLE or not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return
    if os.environ.get("TWITCH_PUSH_POLL", "1").strip() == "0":
        return
    _twitch_push_poller_started = True

    def _poller() -> None:
        channels = list(dict.fromkeys([TWITCH_CHANNEL.lower(), RECORDER_CHANNEL.lower()]))
        while True:
            time.sleep(90)
            try:
                with _db_lock, auth_db_connect() as conn:
                    cnt = int(conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] or 0)
                if cnt <= 0:
                    continue
                last_notified = kv_get("push_last_notified", {})
                if not isinstance(last_notified, dict):
                    last_notified = {}
                changed = False
                for ch in channels:
                    code, st = twitch_status(ch)
                    if code != 200 or not st.get("live"):
                        continue
                    stream = st.get("stream") if isinstance(st.get("stream"), dict) else {}
                    started_at = text(stream.get("startedAt"), 64)
                    if not started_at or last_notified.get(ch) == started_at:
                        continue
                    # Не отправляем старые стримы (старше 2 часов), если это первый запуск с пустой историей
                    try:
                        st_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - st_dt).total_seconds() > 7200:
                            last_notified[ch] = started_at
                            changed = True
                            continue
                    except Exception:
                        pass
                    disp = text(st.get("displayName"), 80) or ch
                    s_title = text(stream.get("title"), 160) or "Прямой эфир"
                    s_game = text(stream.get("gameName"), 80)
                    body_txt = f"{s_title} · {s_game}" if s_game else s_title
                    dest_url = "/danonsha" if ch == RECORDER_CHANNEL.lower() else "/"
                    res = send_stream_push_notifications(ch, f"{disp} в эфире!", body_txt, dest_url)
                    last_notified[ch] = started_at
                    changed = True
                    print(f"[push] {ch} live -> sent={res['sent']}, failed={res['failed']}", flush=True)
                if changed:
                    kv_set("push_last_notified", last_notified)
            except Exception:
                pass

    threading.Thread(target=_poller, name="twitch-push-poller", daemon=True).start()


@app.get("/api/push/vapid-key")
def api_push_vapid_key() -> JSONResponse:
    _, pub = resolve_vapid_keys()
    return j({"configured": bool(pub), "publicKey": pub}, headers={"Cache-Control": "no-store"})


@app.post("/api/push/status")
def api_push_status(request: Request, body: dict[str, Any]) -> JSONResponse:
    rate_limit(request, "push_status", 60, 60)
    endpoint = _is_safe_push_endpoint(body.get("endpoint"))
    channel = _normalize_push_channel(body.get("channel"))
    if not endpoint:
        return j({"subscribed": False})
    with _db_lock, auth_db_connect() as conn:
        row = conn.execute("SELECT channels_json FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
    if not row:
        return j({"subscribed": False})
    try:
        channels = json.loads(row["channels_json"] or "[]")
    except Exception:
        channels = []
    return j({"subscribed": channel in channels or "all" in channels, "channels": channels})


@app.post("/api/push/subscribe")
def api_push_subscribe(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    rate_limit(request, "push_sub", 20, 60)
    if not _WEBPUSH_AVAILABLE:
        return j({"error": "webpush_unavailable"}, 503)
    sub = body.get("subscription") if isinstance(body.get("subscription"), dict) else {}
    endpoint = _is_safe_push_endpoint(sub.get("endpoint"))
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    p256dh = text(keys.get("p256dh"), 300)
    auth_key = text(keys.get("auth"), 200)
    if not endpoint or not p256dh or not auth_key:
        return j({"error": "invalid_subscription"}, 400)
    channel = _normalize_push_channel(body.get("channel"))
    now = int(time.time())
    with _db_lock, auth_db_connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] or 0)
        existing = conn.execute("SELECT channels_json FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        if not existing and total >= 5000:
            return j({"error": "subscriptions_full"}, 503)
        channels: list[str] = []
        if existing:
            try:
                parsed_c = json.loads(existing["channels_json"] or "[]")
                if isinstance(parsed_c, list):
                    channels = [str(x) for x in parsed_c if isinstance(x, str)]
            except Exception:
                pass
        if channel not in channels:
            channels.append(channel)
        c_json = json.dumps(channels, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, channels_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                channels_json = excluded.channels_json,
                updated_at = excluded.updated_at
            """,
            (endpoint, p256dh, auth_key, c_json, now, now),
        )
    return j({"ok": True, "channels": channels})


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe(request: Request, body: dict[str, Any]) -> JSONResponse:
    require_same_origin(request)
    rate_limit(request, "push_sub", 20, 60)
    endpoint = _is_safe_push_endpoint(body.get("endpoint"))
    if not endpoint:
        return j({"ok": True})
    raw_ch = text(body.get("channel"), 40)
    with _db_lock, auth_db_connect() as conn:
        if not raw_ch:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        else:
            channel = _normalize_push_channel(raw_ch)
            row = conn.execute("SELECT channels_json FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
            if row:
                try:
                    channels = [c for c in json.loads(row["channels_json"] or "[]") if c != channel]
                except Exception:
                    channels = []
                if channels:
                    conn.execute(
                        "UPDATE push_subscriptions SET channels_json = ?, updated_at = ? WHERE endpoint = ?",
                        (json.dumps(channels, separators=(",", ":")), int(time.time()), endpoint),
                    )
                else:
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    return j({"ok": True})


@app.post("/api/admin/push/test")
def api_admin_push_test(request: Request) -> JSONResponse:
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "admin_push_test", 10, 60)
    res = send_stream_push_notifications(
        "*",
        "krafen.me — тестовое уведомление",
        "Web Push уведомления о стримах работают исправно.",
        "/",
    )
    return j({"ok": True, **res})


# ----------------------------- Динамические OG-превью (Pillow) -----------------------------

_og_cache_lock = threading.Lock()
_og_image_cache: dict[str, tuple[float, bytes, str]] = {}
_og_font_cache: dict[tuple[int, bool, bool], Any] = {}


def _og_font(size: int, *, bold: bool = False, mono: bool = False) -> Any:
    key = (size, bold, mono)
    cached = _og_font_cache.get(key)
    if cached is not None:
        return cached
    win_fonts = Path(os.environ.get("WINDIR", "")) / "Fonts" if os.name == "nt" and os.environ.get("WINDIR") else None
    if mono:
        candidates = (
            *((str(win_fonts / ("consolab.ttf" if bold else "consola.ttf")),) if win_fonts else ()),
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        )
    elif bold:
        candidates = (
            *((str(win_fonts / "segoeuib.ttf"), str(win_fonts / "arialbd.ttf")) if win_fonts else ()),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        )
    else:
        candidates = (
            *((str(win_fonts / "segoeui.ttf"), str(win_fonts / "arial.ttf")) if win_fonts else ()),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        )
    font = None
    for path_str in candidates:
        if Path(path_str).is_file():
            try:
                font = _PILFont.truetype(path_str, size)
                break
            except Exception:
                continue
    if font is None:
        try:
            font = _PILFont.load_default(size=size)
        except TypeError:
            font = _PILFont.load_default()
    _og_font_cache[key] = font
    return font


def _og_fit_text(draw: Any, raw: str, font: Any, max_width: int) -> str:
    val = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not val:
        return ""
    if draw.textlength(val, font=font) <= max_width:
        return val
    while len(val) > 1 and draw.textlength(val + "…", font=font) > max_width:
        val = val[:-1]
    return val.rstrip() + "…"


def _og_rounded_thumb(img: Any, size: tuple[int, int], radius: int) -> Any:
    fit = _PILOps.fit(img.convert("RGBA"), size, method=_PILImage.Resampling.LANCZOS)
    mask = _PILImage.new("L", size, 0)
    md = _PILDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    fit.putalpha(mask)
    return fit


def _og_load_img(url: str | None, fallback_path: Path | None) -> Any:
    if url:
        cleaned = clean_url(url)
        if cleaned and _is_allowed_proxy_img_url(cleaned):
            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:32]
            for ext in (".jpg", ".webp", ".png", ".gif", ".avif"):
                p = IMG_CACHE_DIR / f"img_{digest}{ext}"
                if p.is_file():
                    try:
                        return _PILImage.open(p).convert("RGBA")
                    except Exception:
                        pass
            if os.environ.get("OG_FETCH_REMOTE", "1").strip() != "0":
                try:
                    res = requests.get(cleaned, timeout=2.0, headers={"User-Agent": "KrafenOG/1.0"})
                    if res.ok and 0 < len(res.content) <= 2_097_152:
                        img = _PILImage.open(io.BytesIO(res.content)).convert("RGBA")
                        ctype = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
                        ext = _SAFE_IMG_MIMES.get(ctype)
                        if ext:
                            with contextlib.suppress(Exception):
                                IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                                (IMG_CACHE_DIR / f"img_{digest}{ext}").write_bytes(res.content)
                        return img
                except Exception:
                    pass
    if fallback_path and fallback_path.is_file():
        try:
            return _PILImage.open(fallback_path).convert("RGBA")
        except Exception:
            pass
    return None


def _og_fetch_live_track() -> dict[str, Any] | None:
    hit = cached_api_get("spotify")
    if hit and hit[0] == 200 and isinstance(hit[1], dict) and hit[1].get("track"):
        return hit[1]["track"]
    if os.environ.get("OG_FETCH_REMOTE", "1").strip() == "0":
        return None
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            token = get_spotify_access_token()
            if token:
                res = requests.get(
                    f"{SPOTIFY_API_BASE}/me/player",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=2.5,
                )
                item = None
                now_playing = False
                if res.status_code == 200 and res.content:
                    data = res.json()
                    item = data.get("item") if isinstance(data.get("item"), dict) else None
                    now_playing = bool(data.get("is_playing"))
                elif res.status_code == 204:
                    rec = requests.get(
                        f"{SPOTIFY_API_BASE}/me/player/recently-played?limit=1",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=2.5,
                    )
                    if rec.ok and rec.content:
                        items = rec.json().get("items") or []
                        if items and isinstance(items[0], dict):
                            item = items[0].get("track")
                if isinstance(item, dict):
                    album = item.get("album") if isinstance(item.get("album"), dict) else {}
                    images = album.get("images") if isinstance(album.get("images"), list) else []
                    artists = item.get("artists") if isinstance(item.get("artists"), list) else []
                    return {
                        "name": text(item.get("name"), 140),
                        "artist": text(", ".join(a.get("name") for a in artists if isinstance(a, dict) and a.get("name")), 140),
                        "image": clean_url(images[0].get("url")) if images and isinstance(images[0], dict) else None,
                        "nowPlaying": now_playing,
                    }
        except Exception:
            pass
    hit_lfm = cached_api_get("lastfm")
    if hit_lfm and hit_lfm[0] == 200 and isinstance(hit_lfm[1], dict) and hit_lfm[1].get("track"):
        return hit_lfm[1]["track"]
    return None


def _og_collect_context(page: str) -> dict[str, Any]:
    owner = "danonsha" if page == "danonsha" else "krafen"
    counts = {}
    for col in ("watchlist", "gamelist", "setup", "wishlist"):
        try:
            counts[col] = len(read_list(col, use_cache=True, owner=owner).get("items") or [])
        except Exception:
            counts[col] = 0

    twitch_data: dict[str, Any] = {}
    ch = RECORDER_CHANNEL.lower() if page == "danonsha" else TWITCH_CHANNEL.lower()
    if page in {"index", "danonsha"} and TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        hit_tw = cached_api_get(f"twitch:{ch}")
        if hit_tw and hit_tw[0] == 200 and isinstance(hit_tw[1], dict):
            twitch_data = hit_tw[1]
        elif os.environ.get("OG_FETCH_REMOTE", "1").strip() != "0":
            code, st = twitch_status(ch)
            if code == 200 and isinstance(st, dict):
                twitch_data = st

    track = _og_fetch_live_track() if page == "index" else None
    wrapped = get_wrapped_stats(owner=owner, target_year="all") if page in {"wrapped", "danonsha"} else None
    return {
        "page": page,
        "owner": owner,
        "counts": counts,
        "twitch": twitch_data,
        "track": track,
        "wrapped": wrapped,
    }


def generate_og_png(page: str = "index") -> bytes:
    if not _PIL_AVAILABLE:
        fav = ROOT / "favicon.png"
        return fav.read_bytes() if fav.is_file() else b""

    clean_page = page if page in {"index", "watchlist", "gamelist", "setup", "wishlist", "danonsha", "wrapped"} else "index"
    now = time.time()
    with _og_cache_lock:
        hit = _og_image_cache.get(clean_page)
        if hit and hit[0] > now:
            return hit[1]

    ctx = _og_collect_context(clean_page)
    W, H = 1200, 630

    # Фон: глубокий обсидиан с багровым свечением
    canvas = _PILImage.new("RGBA", (W, H), (5, 5, 7, 255))
    glow = _PILImage.new("RGBA", (300, 158), (0, 0, 0, 0))
    gd = _PILDraw.Draw(glow)
    gd.ellipse((-40, -35, 165, 130), fill=(155, 18, 18, 115))
    gd.ellipse((150, 45, 340, 200), fill=(110, 10, 14, 90))
    glow = glow.filter(_PILFilter.GaussianBlur(28)).resize((W, H), _PILImage.Resampling.BILINEAR)
    canvas = _PILImage.alpha_composite(canvas, glow)

    # Полупрозрачные слои рисуем на отдельном overlay и композитим через alpha_composite
    overlay = _PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    od = _PILDraw.Draw(overlay)
    for gx in range(80, W, 80):
        od.line([(gx, 0), (gx, H)], fill=(255, 255, 255, 9), width=1)
    for gy in range(80, H, 80):
        od.line([(0, gy), (W, gy)], fill=(255, 255, 255, 9), width=1)

    card_box = (44, 38, W - 44, H - 38)
    od.rounded_rectangle(card_box, radius=28, fill=(11, 11, 16, 224), outline=(160, 160, 175, 48), width=1)
    od.line([(120, 39), (W - 120, 39)], fill=(163, 19, 19, 185), width=2)
    canvas = _PILImage.alpha_composite(canvas, overlay)

    ui_layer = _PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    ud = _PILDraw.Draw(ui_layer)

    f_mono_sm = _og_font(17, bold=True, mono=True)
    f_mono_xs = _og_font(15, bold=False, mono=True)
    f_title = _og_font(62, bold=True)
    f_sub = _og_font(24, bold=False)
    f_card_title = _og_font(28, bold=True)
    f_card_sub = _og_font(21, bold=False)

    titles = {
        "index": ("krafen", "Личный сайт · музыка · коллекции · эфиры"),
        "danonsha": ("danonsha", "Прямые эфиры Twitch · архив VOD и коллекции"),
        "watchlist": ("Watchlist", "Аниме, фильмы и сериалы с оценками и рецензиями"),
        "gamelist": ("Gamelist", "Библиотека пройденных и активных игр"),
        "setup": ("Setup", "Конфигурация системного блока и периферия"),
        "wishlist": ("Wishlist", "Список желаемых покупок и приоритеты"),
        "wrapped": ("Wrapped", "Хроника и статистика коллекций по годам"),
    }
    main_title, main_sub = titles[clean_page]
    eyebrow = f"KRAFEN.ME // {clean_page.upper()}" if clean_page != "index" else "KRAFEN.ME // PERSONAL SITE"

    # Верхняя строка: eyebrow слева, статус-пилюля справа
    ud.ellipse((88, 79, 98, 89), fill=(163, 19, 19, 255))
    ud.text((112, 74), eyebrow, font=f_mono_sm, fill=(165, 165, 175, 255))

    tw = ctx.get("twitch") or {}
    is_live = bool(tw.get("live"))
    badge_txt = "LIVE ON TWITCH" if is_live else ("TWITCH OFFLINE" if tw else "KRAFEN.ME")
    badge_w = int(ud.textlength(badge_txt, font=f_mono_xs)) + 52
    bx2 = W - 84
    bx1 = bx2 - badge_w
    if is_live:
        ud.rounded_rectangle((bx1, 66, bx2, 102), radius=18, fill=(22, 163, 74, 42), outline=(34, 197, 94, 160), width=1)
        ud.ellipse((bx1 + 16, 79, bx1 + 26, 89), fill=(34, 197, 94, 255))
        ud.text((bx1 + 36, 74), badge_txt, font=f_mono_xs, fill=(213, 255, 236, 255))
    else:
        ud.rounded_rectangle((bx1, 66, bx2, 102), radius=18, fill=(255, 255, 255, 14), outline=(160, 160, 170, 60), width=1)
        ud.ellipse((bx1 + 16, 79, bx1 + 26, 89), fill=(163, 19, 19, 255))
        ud.text((bx1 + 36, 74), badge_txt, font=f_mono_xs, fill=(220, 220, 228, 255))

    # Аватар профиля
    fallback_av = (ROOT / "images" / "danonsha-portrait.png") if clean_page == "danonsha" else (ROOT / "favicon.png")
    if not fallback_av.is_file():
        fallback_av = ROOT / "favicon.png"
    av_img = _og_load_img(tw.get("profileImage"), fallback_av)
    ud.rounded_rectangle((84, 134, 240, 290), radius=30, outline=(163, 19, 19, 185), width=2)

    # Заголовок и подзаголовок
    ud.text((272, 138), main_title, font=f_title, fill=(255, 255, 255, 255))
    ud.text((274, 218), _og_fit_text(ud, main_sub, f_sub, 820), font=f_sub, fill=(175, 175, 185, 255))

    # Плашки статистики коллекций
    cnt = ctx["counts"]
    chips = [
        f"Watchlist · {cnt.get('watchlist', 0)}",
        f"Gamelist · {cnt.get('gamelist', 0)}",
        f"Setup · {cnt.get('setup', 0)}",
        f"Wishlist · {cnt.get('wishlist', 0)}",
    ]
    cx = 274
    cy = 266
    for ch_txt in chips:
        cw = int(ud.textlength(ch_txt, font=f_mono_xs)) + 26
        if cx + cw > W - 84:
            break
        ud.rounded_rectangle((cx, cy, cx + cw, cy + 34), radius=17, fill=(255, 255, 255, 12), outline=(160, 160, 175, 48), width=1)
        ud.text((cx + 13, cy + 8), ch_txt, font=f_mono_xs, fill=(224, 224, 232, 255))
        cx += cw + 10

    # Нижняя динамическая витрина (Live Track / Live Stream / Archive Summary)
    box_y1, box_y2 = 356, 542
    ud.rounded_rectangle((84, box_y1, W - 84, box_y2), radius=22, fill=(255, 255, 255, 10), outline=(255, 255, 255, 34), width=1)

    track = ctx.get("track")
    wr = ctx.get("wrapped") or {}
    wr_sum = wr.get("summary") or {}

    art_to_paste = None
    if clean_page == "index" and isinstance(track, dict) and track.get("name"):
        art_to_paste = _og_load_img(track.get("image"), ROOT / "favicon.png")
        tx = 266
        now_pl = bool(track.get("nowPlaying"))
        eye = "СЕЙЧАС ИГРАЕТ В SPOTIFY" if now_pl else "НЕДАВНО ПРОСЛУШАНО"
        ud.ellipse((tx, box_y1 + 36, tx + 10, box_y1 + 46), fill=(34, 197, 94, 255) if now_pl else (163, 19, 19, 255))
        ud.text((tx + 20, box_y1 + 31), eye, font=f_mono_xs, fill=(210, 210, 220, 255))
        ud.text((tx, box_y1 + 66), _og_fit_text(ud, track.get("name"), f_card_title, 780), font=f_card_title, fill=(255, 255, 255, 255))
        ud.text((tx, box_y1 + 110), _og_fit_text(ud, track.get("artist") or "Spotify / Last.fm", f_card_sub, 780), font=f_card_sub, fill=(165, 165, 175, 255))
    elif is_live:
        stream = tw.get("stream") if isinstance(tw.get("stream"), dict) else {}
        s_title = text(stream.get("title"), 120) or "Прямой эфир"
        s_game = text(stream.get("gameName"), 60)
        s_view = stream.get("viewerCount", 0)
        ud.ellipse((114, box_y1 + 36, 124, box_y1 + 46), fill=(34, 197, 94, 255))
        ud.text((134, box_y1 + 31), f"TWITCH LIVE · @{tw.get('login', clean_page)}", font=f_mono_xs, fill=(213, 255, 236, 255))
        ud.text((114, box_y1 + 66), _og_fit_text(ud, s_title, f_card_title, 940), font=f_card_title, fill=(255, 255, 255, 255))
        meta_line = f"{s_view} зрителей" + (f" · {s_game}" if s_game else "")
        ud.text((114, box_y1 + 112), _og_fit_text(ud, meta_line, f_card_sub, 940), font=f_card_sub, fill=(175, 185, 180, 255))
    else:
        if clean_page == "danonsha":
            eye_lbl = "АРХИВ ЭФИРОВ · GOOGLE DRIVE"
            line1 = f"Записей в архиве: {wr_sum.get('vods_count', 0)} · {wr_sum.get('total_vod_hours', 0)} часов"
            line2 = text(tw.get("description"), 120) or "Трансляции, синхронизированный чат и личные коллекции"
        elif clean_page == "wrapped":
            eye_lbl = "ИТОГИ И СТАТИСТИКА КОЛЛЕКЦИЙ"
            line1 = f"Тайтлов: {wr_sum.get('total_watched', 0)} · Игр: {wr_sum.get('games_total', 0)} · Стримов: {wr_sum.get('vods_count', 0)}"
            line2 = f"Средняя оценка кино: {wr_sum.get('avg_w_rating') or '—'} · Длительность VOD: {wr_sum.get('total_vod_hours', 0)} ч."
        elif clean_page in {"watchlist", "gamelist", "setup", "wishlist"}:
            eye_lbl = f"КОЛЛЕКЦИЯ {clean_page.upper()}"
            line1 = f"Элементов в базе: {cnt.get(clean_page, 0)}"
            line2 = "Оценки, отзывы, фильтры, теги и быстрый поиск · krafen.me"
        else:
            eye_lbl = "TWITCH & ПРОФИЛЬ"
            line1 = f"@{tw.get('login', TWITCH_CHANNEL)} · {tw.get('viewCount', 0)} просмотров канала" if tw else "Коллекции, музыка, статус Twitch и ссылки в одном месте"
            line2 = "Нажмите, чтобы открыть профиль, списки и live-статусы"
        ud.ellipse((114, box_y1 + 36, 124, box_y1 + 46), fill=(163, 19, 19, 255))
        ud.text((134, box_y1 + 31), eye_lbl, font=f_mono_xs, fill=(195, 195, 205, 255))
        ud.text((114, box_y1 + 68), _og_fit_text(ud, line1, f_card_title, 940), font=f_card_title, fill=(255, 255, 255, 255))
        ud.text((114, box_y1 + 112), _og_fit_text(ud, line2, f_card_sub, 940), font=f_card_sub, fill=(160, 160, 170, 255))

    canvas = _PILImage.alpha_composite(canvas, ui_layer)
    if av_img is not None:
        r_av = _og_rounded_thumb(av_img, (148, 148), 26)
        canvas.paste(r_av, (88, 138), r_av)
    if art_to_paste is not None:
        r_art = _og_rounded_thumb(art_to_paste, (132, 132), 18)
        canvas.paste(r_art, (110, box_y1 + 27), r_art)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", compress_level=6)
    png_bytes = out.getvalue()
    etag = f'"{hashlib.sha256(png_bytes).hexdigest()[:24]}"'
    with _og_cache_lock:
        _og_image_cache[clean_page] = (now + 60.0, png_bytes, etag)
    return png_bytes


@app.api_route("/og/{page}.png", methods=["GET", "HEAD"])
def og_page_image(page: str, request: Request) -> Response:
    rate_limit(request, "og_img", 120, 60)
    png_bytes = generate_og_png(page)
    etag = f'"{hashlib.sha256(png_bytes).hexdigest()[:24]}"'
    cache_hdr = "public, max-age=60, stale-while-revalidate=300"
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_hdr})
    headers = {
        "Cache-Control": cache_hdr,
        "ETag": etag,
        "Content-Length": str(len(png_bytes)),
        "X-Content-Type-Options": "nosniff",
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type="image/png", headers=headers)
    return Response(content=png_bytes, status_code=200, media_type="image/png", headers=headers)


@app.api_route("/api/og", methods=["GET", "HEAD"])
def api_og_image(request: Request, page: str = "index") -> Response:
    return og_page_image(page, request)


def generate_showcase_card_png(kind: str, owner: str = DEFAULT_PROFILE) -> bytes:
    """Генерирует PNG-карточку сборки ПК (/setup) или вишлиста (/wishlist) на Pillow."""
    clean_kind = kind if kind in {"setup", "wishlist"} else "setup"
    if not _PIL_AVAILABLE:
        fav = ROOT / "favicon.png"
        return fav.read_bytes() if fav.is_file() else b""
    items = read_list(clean_kind, use_cache=True, owner=owner).get("items") or []
    rows_text: list[tuple[str, str]] = []
    if clean_kind == "setup":
        for it in items[:10]:
            cat = text(it.get("category"), 28) or text(it.get("group"), 16).upper()
            nm = text(it.get("name"), 72)
            note = text(it.get("note"), 44)
            rows_text.append((cat, f"{nm}" + (f" · {note}" if note else "")))
    else:
        wanted = [i for i in items if i.get("status") != "bought"] or items
        for it in wanted[:10]:
            cat = text(it.get("category"), 24)
            price_val = float(it.get("price") or 0)
            cur = text(it.get("currency"), 8) or "RUB"
            p_str = f"{int(round(price_val))} {cur}" if price_val > 0 else "подарок"
            rows_text.append((cat, f"{text(it.get('title'), 66)} — {p_str}"))

    W = 1080
    H = max(540, 220 + max(1, len(rows_text)) * 54 + 60)
    canvas = _PILImage.new("RGBA", (W, H), (6, 6, 9, 255))
    ov = _PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    od = _PILDraw.Draw(ov)
    od.rounded_rectangle((32, 28, W - 32, H - 28), radius=24, fill=(13, 13, 18, 238), outline=(163, 19, 19, 130), width=1)
    od.line([(96, 29), (W - 96, 29)], fill=(163, 19, 19, 210), width=2)
    canvas = _PILImage.alpha_composite(canvas, ov)

    ud = _PILDraw.Draw(canvas)
    f_eyebrow = _og_font(15, bold=True, mono=True)
    f_title = _og_font(42, bold=True)
    f_tag = _og_font(15, bold=True, mono=True)
    f_row = _og_font(20, bold=False)

    heading = "PC & GEAR SETUP" if clean_kind == "setup" else "WISHLIST & PLANS"
    ud.text((68, 62), f"KRAFEN.ME // {owner.upper()}", font=f_eyebrow, fill=(163, 19, 19, 255))
    ud.text((68, 92), heading, font=f_title, fill=(245, 245, 250, 255))
    ud.line([(68, 160), (W - 68, 160)], fill=(255, 255, 255, 28), width=1)

    if not rows_text:
        ud.text((68, 210), "Список пока пуст", font=f_row, fill=(160, 160, 172, 255))
    else:
        y = 186
        for left_lbl, right_val in rows_text:
            ud.rounded_rectangle((68, y, W - 68, y + 44), radius=10, fill=(255, 255, 255, 9), outline=(255, 255, 255, 22), width=1)
            ud.text((86, y + 13), _og_fit_text(ud, left_lbl.upper(), f_tag, 175), font=f_tag, fill=(255, 122, 122, 255))
            ud.text((280, y + 10), _og_fit_text(ud, right_val, f_row, W - 368), font=f_row, fill=(236, 236, 242, 255))
            y += 54

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


@app.api_route("/api/card/{kind}.png", methods=["GET", "HEAD"])
def api_showcase_card(kind: str, request: Request) -> Response:
    rate_limit(request, "showcase_card", 60, 60)
    if kind not in {"setup", "wishlist"}:
        return j({"error": "invalid_card_kind"}, 404)
    owner = request_profile(request)
    png_bytes = generate_showcase_card_png(kind, owner=owner)
    headers = {
        "Cache-Control": "public, max-age=120, stale-while-revalidate=300",
        "Content-Length": str(len(png_bytes)),
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="krafen-{kind}.png"',
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type="image/png", headers=headers)
    return Response(content=png_bytes, status_code=200, media_type="image/png", headers=headers)


@app.get("/api/discord")
def discord_invite(request: Request, code: str = DISCORD_INVITE_CODE) -> JSONResponse:
    rate_limit(request, "discord", 60, 60)
    clean_code = re.sub(r"[^A-Za-z0-9_-]", "", text(code, 40)) or DISCORD_INVITE_CODE
    if not clean_code:
        return j({"error": "discord_code_missing"}, 503)
    cache_key = f"discord:{clean_code}"
    hit = cached_api_get(cache_key)
    if hit:
        return j(hit[1], hit[0])
    try:
        res = requests.get(
            f"{DISCORD_INVITE_BASE}/{clean_code}",
            params={"with_counts": "true"},
            headers={"Accept": "application/json", "User-Agent": "krafen-site/2.0"},
            timeout=12,
        )
        data = res.json() if res.content else {}
        if not res.ok:
            return j({"error": "discord_unavailable"}, 502)
        guild = data.get("guild") if isinstance(data.get("guild"), dict) else {}
        icon_hash = text(guild.get("icon"), 80)
        guild_id = text(guild.get("id"), 40)
        valid_icon = re.fullmatch(r"[A-Za-z0-9_]+", icon_hash) and re.fullmatch(r"\d+", guild_id)
        raw_icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=128" if valid_icon else None
        icon_url = f"/api/img?u={quote(raw_icon_url, safe='')}" if raw_icon_url else None
        payload = {
            "code": clean_code,
            "name": text(guild.get("name"), 120) or "Discord Server",
            "memberCount": int(data.get("approximate_member_count") or 0),
            "onlineCount": int(data.get("approximate_presence_count") or 0),
            "icon": icon_url,
            "url": f"https://discord.gg/{clean_code}",
        }
        cached_api_set(cache_key, 180.0, 200, payload)
        return j(payload)
    except Exception:
        return j({"error": "discord_unavailable"}, 502)


# ----------------------------- Прокси обложек для РФ (/api/img) -----------------------------

_PROXY_IMG_HOSTS = frozenset({
    "cdn.discordapp.com",
    "media.discordapp.net",
    "shikimori.one",
    "nyaa.shikimori.one",
    "desu.shikimori.one",
    "dere.shikimori.one",
    "kawai.shikimori.one",
    "image.tmdb.org",
    "images.igdb.com",
    "i.scdn.co",
    "lastfm.freetls.fastly.net",
    "lastfm-img2.akamaized.net",
    "static-cdn.jtvnw.net",
    "drive.google.com",
    "lh3.googleusercontent.com",
    "images.weserv.nl",
    "m.media-amazon.com",
})
_SAFE_IMG_MIMES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
_MAX_PROXY_IMG_BYTES = 4 * 1024 * 1024
_IMG_CACHE_PRUNE_LOCK = threading.Lock()


def _is_allowed_proxy_img_url(raw_url: str) -> bool:
    cleaned = clean_url(raw_url)
    if not cleaned:
        return False
    parsed = urlsplit(cleaned)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and host in _PROXY_IMG_HOSTS


def _prune_img_cache() -> None:
    if not _IMG_CACHE_PRUNE_LOCK.acquire(blocking=False):
        return
    try:
        files = [p for p in IMG_CACHE_DIR.glob("img_*.*") if p.is_file()]
        if len(files) <= 600:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for old in files[:150]:
            with contextlib.suppress(OSError):
                old.unlink()
    except OSError:
        pass
    finally:
        _IMG_CACHE_PRUNE_LOCK.release()


@app.api_route("/api/img", methods=["GET", "HEAD"])
def proxy_external_image(request: Request, u: str = "") -> Response:
    rate_limit(request, "img_proxy", 240, 60)
    target = clean_url(u)
    if not target or not _is_allowed_proxy_img_url(target):
        return j({"error": "invalid_image_url"}, 400)

    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:32]
    etag = f'"{digest}"'
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "public, max-age=604800, immutable"},
        )

    IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for ext, mime in (
        (".jpg", "image/jpeg"),
        (".webp", "image/webp"),
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".avif", "image/avif"),
    ):
        cached_path = IMG_CACHE_DIR / f"img_{digest}{ext}"
        if cached_path.is_file():
            headers = {
                "Cache-Control": "public, max-age=604800, immutable",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
            }
            if request.method == "HEAD":
                return Response(status_code=200, media_type=mime, headers=headers)
            return FileResponse(cached_path, media_type=mime, headers=headers)

    cur_url = target
    try:
        for _hop in range(4):
            if not _is_allowed_proxy_img_url(cur_url):
                return j({"error": "disallowed_redirect"}, 400)
            with requests.get(
                cur_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KrafenImgProxy/1.0)",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
                stream=True,
                timeout=(3.5, 10),
                allow_redirects=False,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = (resp.headers.get("location") or "").strip()
                    if not loc:
                        return j({"error": "upstream_bad_redirect"}, 502)
                    if loc.startswith("/"):
                        p = urlsplit(cur_url)
                        loc = f"{p.scheme}://{p.netloc}{loc}"
                    cur_url = loc
                    continue
                if resp.status_code != 200:
                    return j({"error": "upstream_not_found"}, 404 if resp.status_code == 404 else 502)
                raw_ct = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                ext = _SAFE_IMG_MIMES.get(raw_ct)
                if not ext:
                    return j({"error": "unsupported_media_type"}, 502)
                buf = io.BytesIO()
                total = 0
                for chunk in resp.iter_content(chunk_size=32 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_PROXY_IMG_BYTES:
                        return j({"error": "image_too_large"}, 502)
                    buf.write(chunk)
                img_bytes = buf.getvalue()
                if not img_bytes:
                    return j({"error": "empty_image"}, 502)

                out_mime = "image/jpeg" if raw_ct == "image/jpg" else raw_ct
                final_path = IMG_CACHE_DIR / f"img_{digest}{ext}"
                tmp_path = IMG_CACHE_DIR / f".tmp_{digest}_{secrets.token_hex(4)}{ext}"
                with contextlib.suppress(OSError):
                    tmp_path.write_bytes(img_bytes)
                    os.replace(tmp_path, final_path)
                    _prune_img_cache()
                headers = {
                    "Cache-Control": "public, max-age=604800, immutable",
                    "ETag": etag,
                    "X-Content-Type-Options": "nosniff",
                    "Content-Length": str(len(img_bytes)),
                }
                if request.method == "HEAD":
                    return Response(status_code=200, media_type=out_mime, headers=headers)
                return Response(content=img_bytes, status_code=200, media_type=out_mime, headers=headers)
        return j({"error": "too_many_redirects"}, 502)
    except (requests.RequestException, OSError):
        return j({"error": "upstream_unreachable"}, 502)


# ----------------------------- VOD архив -----------------------------


def safe_name(value: str) -> bool:
    if not value or len(value) > 200 or "\x00" in value:
        return False
    if (
        value in {".", ".."}
        or ".." in value
        or "/" in value
        or "\\" in value
        or value.startswith((".", " "))
        or value.endswith((".", " "))
    ):
        return False
    return re.fullmatch(r"[\w. -]+", value, flags=re.UNICODE) is not None


def _vod_cache_stem(vod_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", vod_id) and ".." not in vod_id:
        return vod_id
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", vod_id)[:48].strip("_") or "vod"
    digest = hashlib.sha1(vod_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _format_size_bytes(num_bytes: int | float) -> str:
    n = float(num_bytes or 0)
    if n <= 0:
        return ""
    gb = n / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = n / (1024 ** 2)
    if mb >= 1.0:
        return f"{mb:.1f} MB"
    kb = max(1, round(n / 1024))
    return f"{kb} KB"


_drive_sync_lock = threading.Lock()
_drive_sync_worker_lock = threading.Lock()
_drive_sync_ts = 0.0
_FLIP_TITLE_RE = re.compile(r'<div[^>]*class="flip-entry-title"[^>]*>([\s\S]*?)</div>', re.IGNORECASE)
_FLIP_FOLDER_HREF_RE = re.compile(
    r'href="https://drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|folderview\?id=)([A-Za-z0-9_-]{10,})',
    re.IGNORECASE,
)
_FLIP_FILE_HREF_RE = re.compile(
    r'href="https://(?:drive|docs)\.google\.com/(?:file/d/|open\?id=)([A-Za-z0-9_-]{10,})',
    re.IGNORECASE,
)


def parse_drive_filename(raw_name: str, fallback_key: str) -> dict[str, str]:
    clean = re.sub(r"\.(mp4|mkv|webm|mov)$", "", html.unescape(raw_name).strip(), flags=re.IGNORECASE).strip()
    date_match = re.search(r"\[(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})\]", clean)
    note = ""
    title = clean
    if date_match:
        vod_id = date_match.group(1)
        prefix = clean[: date_match.start()].strip(" -—·")
        suffix = clean[date_match.end() :].strip()
        suffix = re.sub(r"^danonsha\s*-\s*", "", suffix, flags=re.IGNORECASE).strip()
        title = suffix or clean
        note = prefix
    else:
        stamp_match = re.match(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})(?:[_-]+(.*))?$", clean)
        if stamp_match:
            sanitized = re.sub(r"\.{2,}", ".", re.sub(r"[^\w.-]+", "_", clean, flags=re.UNICODE)).strip("._ ")
            vod_id = sanitized or stamp_match.group(1)
            slug_tail = (stamp_match.group(2) or "").replace("-", " ").replace("_", " ").strip()
            title = slug_tail or "Запись стрима"
        else:
            vod_id = f"gdrive_{fallback_key}"
    return {
        "id": vod_id[:160],
        "title": title[:260] or "Запись стрима",
        "note": note[:180],
        "sort_key": vod_id[:160],
    }


def parse_drive_folder_name(raw_folder_name: str, fallback_key: str) -> dict[str, str]:
    clean = html.unescape(raw_folder_name or "").strip()
    sanitized_id = re.sub(r"\.{2,}", ".", re.sub(r"[^\w.-]+", "_", clean, flags=re.UNICODE)).strip("._ ")[:160]
    stamp_m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})", clean)
    if stamp_m:
        stamp = stamp_m.group(1)
        vod_id = sanitized_id if sanitized_id else stamp
        tail = clean[stamp_m.end() :].lstrip("_- —·").strip()
        fallback_title = tail.replace("-", " ").replace("_", " ").strip() or "Запись стрима"
        sort_key = vod_id if vod_id.startswith(stamp) else f"{stamp}_{vod_id}"
    else:
        vod_id = sanitized_id or f"gdrive_{fallback_key}"
        fallback_title = clean or "Запись стрима"
        sort_key = vod_id
    return {
        "id": vod_id[:160],
        "fallback_title": fallback_title[:260],
        "sort_key": sort_key[:160],
    }


def _parse_embedded_folder_html(html_text: str) -> list[dict[str, Any]]:
    """Парсит файлы и подпапки из https://drive.google.com/embeddedfolderview?id=...#list."""
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    chunks = re.split(r"""<div[^>]+class=["'](?:[^"']*\s)?flip-entry(?:\s[^"']*)?["']""", html_text, flags=re.IGNORECASE)
    for chunk in chunks[1:]:
        title_m = _FLIP_TITLE_RE.search(chunk)
        if not title_m:
            continue
        plain_name = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
        if not plain_name:
            continue
        folder_m = _FLIP_FOLDER_HREF_RE.search(chunk)
        if folder_m:
            did = folder_m.group(1)
            if did not in seen_ids:
                seen_ids.add(did)
                entries.append({"id": did, "name": plain_name, "kind": "folder", "size": 0})
            continue
        file_m = _FLIP_FILE_HREF_RE.search(chunk)
        if file_m:
            did = file_m.group(1)
            if did not in seen_ids:
                seen_ids.add(did)
                entries.append({"id": did, "name": plain_name, "kind": "file", "size": 0})
    return entries


def _list_gdrive_folder(folder_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """Возвращает (ok, entries), где каждый элемент: {id, name, kind: 'file'|'folder', size: int}."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", folder_id):
        return False, []

    if GDRIVE_API_KEY:
        try:
            items: list[dict[str, Any]] = []
            page_token = ""
            for _ in range(10):
                params: dict[str, Any] = {
                    "q": f"'{folder_id}' in parents and trashed = false",
                    "fields": "nextPageToken,files(id,name,mimeType,size)",
                    "pageSize": 200,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "key": GDRIVE_API_KEY,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = requests.get(
                    "https://www.googleapis.com/drive/v3/files",
                    params=params,
                    headers={"User-Agent": "KrafenSite/1.0"},
                    timeout=10,
                )
                if not resp.ok:
                    return False, []
                payload = resp.json()
                for f in payload.get("files") or []:
                    fid = str(f.get("id") or "").strip()
                    fname = str(f.get("name") or "").strip()
                    if not fid or not fname:
                        continue
                    mime = str(f.get("mimeType") or "")
                    kind = "folder" if mime == "application/vnd.google-apps.folder" else "file"
                    try:
                        fsize = int(f.get("size") or 0)
                    except (TypeError, ValueError):
                        fsize = 0
                    items.append({"id": fid, "name": fname, "kind": kind, "size": fsize})
                page_token = str(payload.get("nextPageToken") or "").strip()
                if not page_token:
                    return True, items
            return False, []
        except Exception:
            return False, []

    try:
        res = requests.get(
            f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
            headers={"User-Agent": "Mozilla/5.0 (compatible; KrafenSite/1.0)"},
            timeout=10,
        )
        if not res.ok:
            return False, []
        body = res.text
        if "flip-entries" not in body and "flip-entry" not in body:
            return False, []
        return True, _parse_embedded_folder_html(body)
    except Exception:
        return False, []


def _read_response_prefix(resp: requests.Response, max_bytes: int = 131072) -> str:
    """Читает префикс тела ответа через iter_content (без необработанных urllib3-исключений)."""
    buf = bytearray()
    try:
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) >= max_bytes:
                break
    except (requests.RequestException, OSError):
        pass
    return bytes(buf[:max_bytes]).decode("utf-8", errors="ignore")


_SAFE_GDRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,80}$")
_SAFE_GDRIVE_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_STREAM_MIMES = {
    "video/mp4",
    "video/webm",
    "video/x-matroska",
    "video/quicktime",
    "video/mp2t",
    "application/octet-stream",
}


def _extract_safe_gdrive_uuid(sample: str) -> str:
    uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', sample)
    if not uuid_m:
        return ""
    cand = uuid_m.group(1).strip()
    return cand if _SAFE_GDRIVE_UUID_RE.fullmatch(cand) else ""


def _probe_gdrive_file_size(drive_id: str) -> int:
    """Определяет точный размер файла на Google Drive по 1-байтовому Range-запросу."""
    if not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return 0
    base_url = "https://drive.usercontent.google.com/download"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
        "Range": "bytes=0-0",
    }
    params: dict[str, str] = {"id": drive_id, "export": "download", "confirm": "t"}
    session = requests.Session()
    try:
        resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(3, 8))
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            sample = _read_response_prefix(resp)
            resp.close()
            found_uuid = _extract_safe_gdrive_uuid(sample)
            if not found_uuid:
                return 0
            params["uuid"] = found_uuid
            resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(3, 8))
        cr = resp.headers.get("Content-Range") or ""
        cl = resp.headers.get("Content-Length") or ""
        resp.close()
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if resp.status_code == 200 and cl.isdigit():
            return int(cl)
    except Exception:
        return 0
    finally:
        session.close()
    return 0


def _download_gdrive_small_bytes(drive_id: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
    """Скачивает небольшой публичный файл с Google Drive (meta.json, chat.jsonl, thumb.jpg)."""
    if not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return None
    base_url = "https://drive.usercontent.google.com/download"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }
    params: dict[str, str] = {"id": drive_id, "export": "download", "confirm": "t"}
    session = requests.Session()
    try:
        resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(4, 15))
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            sample = _read_response_prefix(resp)
            resp.close()
            found_uuid = _extract_safe_gdrive_uuid(sample)
            if not found_uuid:
                return None
            params["uuid"] = found_uuid
            resp = session.get(base_url, params=params, headers=headers, stream=True, timeout=(4, 15))
            ctype = (resp.headers.get("content-type") or "").lower()

        if resp.status_code >= 400 or "text/html" in ctype:
            resp.close()
            return None

        buf = io.BytesIO()
        total = 0
        for chunk in resp.iter_content(chunk_size=32 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                resp.close()
                return None
            buf.write(chunk)
        resp.close()
        return buf.getvalue()
    except (requests.RequestException, OSError):
        return None
    finally:
        session.close()


def _upsert_drive_vod(conn: sqlite3.Connection, row_data: dict[str, Any]) -> None:
    # Удаляем конфликтующие дубли с тем же drive_id под другим id, если такие были
    conn.execute(
        "DELETE FROM drive_vods WHERE drive_id = ? AND id != ?",
        (row_data["drive_id"], row_data["id"]),
    )
    conn.execute(
        """
        INSERT INTO drive_vods (
            id, drive_id, title, size_label, note, sort_key,
            folder_drive_id, chat_drive_id, thumb_drive_id, meta_drive_id, transcript_drive_id,
            game, duration_s, started_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            drive_id = excluded.drive_id,
            title = excluded.title,
            size_label = CASE WHEN excluded.size_label != '' THEN excluded.size_label ELSE drive_vods.size_label END,
            note = CASE WHEN drive_vods.note != '' AND excluded.note = '' THEN drive_vods.note ELSE excluded.note END,
            sort_key = excluded.sort_key,
            folder_drive_id = excluded.folder_drive_id,
            chat_drive_id = excluded.chat_drive_id,
            thumb_drive_id = excluded.thumb_drive_id,
            meta_drive_id = excluded.meta_drive_id,
            transcript_drive_id = excluded.transcript_drive_id,
            game = excluded.game,
            duration_s = CASE WHEN excluded.duration_s > 0 THEN excluded.duration_s ELSE drive_vods.duration_s END,
            started_at = CASE WHEN excluded.started_at != '' THEN excluded.started_at ELSE drive_vods.started_at END
        """,
        (
            row_data["id"],
            row_data["drive_id"],
            row_data["title"],
            row_data.get("size_label", ""),
            row_data.get("note", ""),
            row_data["sort_key"],
            row_data.get("folder_drive_id", ""),
            row_data.get("chat_drive_id", ""),
            row_data.get("thumb_drive_id", ""),
            row_data.get("meta_drive_id", ""),
            row_data.get("transcript_drive_id", ""),
            row_data.get("game", ""),
            int(row_data.get("duration_s") or 0),
            row_data.get("started_at", ""),
        ),
    )


def _inspect_drive_vod_subfolder(
    folder_drive_id: str,
    folder_name: str,
    existing: dict[str, Any] | None,
    force: bool = False,
) -> dict[str, Any] | None:
    """
    Читает структуру подпапки записи на Google Drive формата:
      <vod_folder>/
        ├── video.mp4
        ├── chat.jsonl
        ├── meta.json
        └── thumb.jpg
    """
    parsed_folder = parse_drive_folder_name(folder_name, folder_drive_id[:12])
    if (
        not force
        and existing
        and existing.get("drive_id")
        and existing.get("size_label")
        and (existing.get("meta_drive_id") or existing.get("thumb_drive_id"))
    ):
        return None

    ok, sub_entries = _list_gdrive_folder(folder_drive_id)
    if not ok or not sub_entries:
        return None

    files_by_lower: dict[str, dict[str, Any]] = {}
    all_files: list[dict[str, Any]] = []
    for item in sub_entries:
        if item["kind"] != "file":
            continue
        all_files.append(item)
        files_by_lower[item["name"].lower()] = item

    meta_entry = files_by_lower.get("meta.json")
    meta_obj: dict[str, Any] = {}
    if meta_entry:
        raw_meta = _download_gdrive_small_bytes(meta_entry["id"], max_bytes=256 * 1024)
        if raw_meta:
            try:
                loaded = json.loads(raw_meta.decode("utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    meta_obj = loaded
            except Exception:
                meta_obj = {}

    preferred_video = str(meta_obj.get("video_file") or "video.mp4").strip().lower()
    preferred_chat = str(meta_obj.get("chat_file") or "chat.jsonl").strip().lower()
    preferred_thumb = str(meta_obj.get("thumb_file") or "thumb.jpg").strip().lower()
    preferred_transcript = str(meta_obj.get("transcript_file") or "transcript.vtt").strip().lower()

    video_entry = files_by_lower.get(preferred_video) or files_by_lower.get("video.mp4")
    if not video_entry:
        for f in all_files:
            low = f["name"].lower()
            if low.endswith((".mp4", ".mkv", ".webm", ".mov")) and not re.search(r"\.part\d+\.mp4$", low):
                video_entry = f
                break
    if not video_entry:
        return None

    chat_entry = files_by_lower.get(preferred_chat) or files_by_lower.get("chat.jsonl")
    if not chat_entry:
        for f in all_files:
            if f["name"].lower().endswith(".jsonl"):
                chat_entry = f
                break

    thumb_entry = files_by_lower.get(preferred_thumb) or files_by_lower.get("thumb.jpg")
    if not thumb_entry:
        for f in all_files:
            low = f["name"].lower()
            if low.startswith("thumb.") and low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                thumb_entry = f
                break
        if not thumb_entry:
            for f in all_files:
                if f["name"].lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    thumb_entry = f
                    break

    raw_title = str(meta_obj.get("title") or "").strip()
    title = (raw_title or parsed_folder["fallback_title"] or "Запись стрима")[:260]
    game = str(meta_obj.get("game") or "").strip()[:120]
    note = str(meta_obj.get("note") or "").strip()[:180]
    started_at = str(meta_obj.get("started_at") or meta_obj.get("record_started_at") or "").strip()[:64]
    try:
        duration_s = max(0, int(float(meta_obj.get("duration_s") or 0)))
    except (TypeError, ValueError):
        duration_s = 0

    size_label = str(meta_obj.get("size_label") or "").strip()[:40]
    if not size_label:
        size_bytes = video_entry.get("size") or meta_obj.get("size_bytes") or 0
        try:
            size_bytes = int(size_bytes)
        except (TypeError, ValueError):
            size_bytes = 0
        if size_bytes <= 0:
            size_bytes = _probe_gdrive_file_size(video_entry["id"])
        size_label = _format_size_bytes(size_bytes)

    transcript_entry = (
        files_by_lower.get(preferred_transcript)
        or files_by_lower.get("transcript.vtt")
        or files_by_lower.get("subtitles.vtt")
    )
    if not transcript_entry:
        for f in all_files:
            if f["name"].lower().endswith(".vtt"):
                transcript_entry = f
                break

    return {
        "id": parsed_folder["id"],
        "drive_id": video_entry["id"],
        "title": title,
        "size_label": size_label,
        "note": note,
        "sort_key": parsed_folder["sort_key"],
        "folder_drive_id": folder_drive_id,
        "chat_drive_id": chat_entry["id"] if chat_entry else "",
        "thumb_drive_id": thumb_entry["id"] if thumb_entry else "",
        "meta_drive_id": meta_entry["id"] if meta_entry else "",
        "transcript_drive_id": transcript_entry["id"] if transcript_entry else "",
        "game": game,
        "duration_s": duration_s,
        "started_at": started_at,
    }


def _sync_drive_vods_worker(folder_id: str, force: bool = False) -> None:
    global _drive_sync_failures
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,80}", folder_id):
        return
    if not _drive_sync_worker_lock.acquire(blocking=False):
        return
    try:
        ok, top_entries = _list_gdrive_folder(folder_id)
        if not ok:
            _drive_sync_failures += 1
            if _drive_sync_failures >= 3:
                _maybe_send_admin_tg_alert(
                    "gdrive_sync",
                    f"[krafen.me] Google Drive VOD sync не отвечает ({_drive_sync_failures} попытки подряд)",
                    cooldown_s=7200.0,
                )
            return
        _drive_sync_failures = 0

        with _db_lock, db_connect() as conn:
            existing_rows = {
                str(r["folder_drive_id"]): dict(r)
                for r in conn.execute("SELECT * FROM drive_vods WHERE folder_drive_id != ''").fetchall()
            }

        prepared_rows: list[dict[str, Any]] = []
        seen_folder_ids: set[str] = set()
        seen_flat_drive_ids: set[str] = set()

        for entry in top_entries:
            if entry["kind"] == "folder":
                f_id = entry["id"]
                seen_folder_ids.add(f_id)
                sub_row = _inspect_drive_vod_subfolder(
                    f_id,
                    entry["name"],
                    existing_rows.get(f_id),
                    force=force,
                )
                if sub_row:
                    prepared_rows.append(sub_row)
            elif entry["kind"] == "file":
                plain_title = entry["name"]
                if not plain_title.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
                    continue
                seen_flat_drive_ids.add(entry["id"])
                parsed = parse_drive_filename(plain_title, entry["id"][:12])
                prepared_rows.append(
                    {
                        "id": parsed["id"],
                        "drive_id": entry["id"],
                        "title": parsed["title"],
                        "size_label": _format_size_bytes(entry.get("size") or 0),
                        "note": parsed["note"],
                        "sort_key": parsed["sort_key"],
                        "folder_drive_id": "",
                        "chat_drive_id": "",
                        "thumb_drive_id": "",
                        "meta_drive_id": "",
                        "transcript_drive_id": "",
                        "game": "",
                        "duration_s": 0,
                        "started_at": "",
                    }
                )

        if not top_entries and not GDRIVE_API_KEY:
            return

        with _db_lock, db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row_data in prepared_rows:
                    _upsert_drive_vod(conn, row_data)
                if top_entries:
                    # Удаляем записи подпапок, которые были удалены из Google Drive
                    for old_folder_id, old_row in existing_rows.items():
                        if old_folder_id not in seen_folder_ids:
                            conn.execute("DELETE FROM drive_vods WHERE id = ?", (old_row["id"],))
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
    except Exception:
        pass
    finally:
        _drive_sync_worker_lock.release()


_drive_poller_started = False


def _start_drive_vods_poller() -> None:
    global _drive_poller_started
    if _drive_poller_started or not GDRIVE_FOLDER_ID:
        return
    _drive_poller_started = True

    def _poller() -> None:
        maybe_sync_drive_vods(force=False)
        while True:
            time.sleep(90)
            maybe_sync_drive_vods(force=False)

    threading.Thread(target=_poller, name="gdrive-vods-poller", daemon=True).start()


def maybe_sync_drive_vods(force: bool = False) -> None:
    global _drive_sync_ts
    if not GDRIVE_FOLDER_ID:
        return
    now = time.time()
    min_interval = 12.0 if force else 60.0
    with _drive_sync_lock:
        if now - _drive_sync_ts < min_interval:
            return
        _drive_sync_ts = now
    if force:
        _sync_drive_vods_worker(GDRIVE_FOLDER_ID, force=True)
    else:
        threading.Thread(target=_sync_drive_vods_worker, args=(GDRIVE_FOLDER_ID, False), daemon=True).start()


def _parse_vod_chapters(raw_chapters: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_chapters, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw_chapters[:40]:
        if isinstance(entry, dict):
            sec = max(0, _safe_int(entry.get("t") if "t" in entry else entry.get("time"), 0))
            title = text(entry.get("title") or entry.get("label") or entry.get("name"), 120)
            if title:
                out.append({"t": sec, "title": title})
    return sorted(out, key=lambda x: x["t"])


def list_vods(force_sync: bool = False, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    if force_sync:
        maybe_sync_drive_vods(force=True)

    vods_by_id: dict[str, dict[str, Any]] = {}

    if RECORD_DIR.exists():
        for folder in sorted(RECORD_DIR.iterdir(), reverse=True):
            if not folder.is_dir() or not safe_name(folder.name):
                continue
            meta_file = folder / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                raw_meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw_meta, dict):
                continue
            raw_vfile = text(raw_meta.get("video_file"), 120) or "video.mp4"
            video_file = raw_vfile if safe_name(raw_vfile) else "video.mp4"
            raw_tfile = text(raw_meta.get("thumb_file"), 120) or "thumb.jpg"
            thumb_file = raw_tfile if safe_name(raw_tfile) else "thumb.jpg"
            q_id = quote(folder.name, safe="")
            vods_by_id[folder.name] = {
                "id": folder.name,
                "source": "local",
                "title": text(raw_meta.get("title"), 260) or folder.name,
                "game": text(raw_meta.get("game"), 120),
                "duration_s": max(0, _safe_int(raw_meta.get("duration_s"), 0)),
                "started_at": text(raw_meta.get("started_at") or raw_meta.get("record_started_at"), 64),
                "size_label": text(raw_meta.get("size_label"), 40),
                "note": text(raw_meta.get("note"), 180),
                "video_file": video_file,
                "has_chat": (folder / "chat.jsonl").is_file(),
                "has_transcript": any((folder / fn).is_file() for fn in ("transcript.vtt", "subtitles.vtt")),
                "has_video": (folder / video_file).is_file() or (folder / "video.mp4").is_file(),
                "has_thumb": (folder / thumb_file).is_file() or (folder / "thumb.jpg").is_file(),
                "thumb_url": f"/api/vods/{q_id}/thumb",
                "thumb_fallbacks": [],
                "chapters": _parse_vod_chapters(raw_meta.get("chapters")),
                "sort_key": folder.name,
            }

    with _db_lock, db_connect() as conn:
        rows = conn.execute("SELECT * FROM drive_vods ORDER BY sort_key DESC").fetchall()
        for r in rows:
            vid = r["id"]
            if vid in vods_by_id:
                continue
            q_vid = quote(vid, safe="")
            drive_id = r["drive_id"]
            thumb_drive_id = r["thumb_drive_id"] or ""
            chat_drive_id = r["chat_drive_id"] or ""
            folder_drive_id = r["folder_drive_id"] or ""

            raw_fallbacks: list[str] = []
            if thumb_drive_id:
                raw_fallbacks.append(f"https://drive.google.com/thumbnail?id={thumb_drive_id}&sz=w960")
                raw_fallbacks.append(f"https://lh3.googleusercontent.com/d/{thumb_drive_id}=w960")
            raw_fallbacks.append(f"https://drive.google.com/thumbnail?id={drive_id}&sz=w960")
            raw_fallbacks.append(f"https://lh3.googleusercontent.com/d/{drive_id}=w960")
            fallbacks = [f"/api/img?u={quote(u, safe='')}" for u in raw_fallbacks]

            vods_by_id[vid] = {
                "id": vid,
                "source": "gdrive",
                "drive_id": drive_id,
                "folder_drive_id": folder_drive_id,
                "title": r["title"],
                "game": r["game"] or "",
                "duration_s": int(r["duration_s"] or 0),
                "started_at": r["started_at"] or "",
                "size_label": r["size_label"] or "",
                "note": r["note"] or "",
                "stream_url": f"/api/vods/{q_vid}/stream",
                "download_url": f"/api/vods/{q_vid}/stream?download=1",
                "embed_url": f"https://drive.google.com/file/d/{drive_id}/preview",
                "view_url": (
                    f"https://drive.google.com/drive/folders/{folder_drive_id}"
                    if folder_drive_id
                    else f"https://drive.google.com/file/d/{drive_id}/view"
                ),
                "thumb_url": f"/api/vods/{q_vid}/thumb",
                "thumb_fallbacks": fallbacks,
                "has_chat": bool(chat_drive_id),
                "has_transcript": bool((r["transcript_drive_id"] or "").strip()) if "transcript_drive_id" in r.keys() else False,
                "has_video": True,
                "sort_key": r["sort_key"],
            }

    overrides = read_vod_overrides()
    final_vods: list[dict[str, Any]] = []
    for vid, entry in vods_by_id.items():
        ov = overrides.get(vid) if isinstance(overrides.get(vid), dict) else None
        is_hidden = bool(ov and ov.get("hidden"))
        if is_hidden and not include_hidden:
            continue
        orig_title = str(entry.get("title") or "")
        orig_note = str(entry.get("note") or "")
        custom_title = text(ov.get("custom_title"), 260) if ov else ""
        custom_note = text(ov.get("custom_note"), 180) if ov else ""
        if custom_title:
            entry["title"] = custom_title
        if custom_note:
            entry["note"] = custom_note
        if include_hidden:
            entry["orig_title"] = orig_title
            entry["orig_note"] = orig_note
            entry["custom_title"] = custom_title
            entry["custom_note"] = custom_note
            entry["hidden"] = is_hidden
        final_vods.append(entry)

    if not force_sync:
        maybe_sync_drive_vods(force=False)
    return sorted(final_vods, key=lambda x: str(x.get("sort_key") or x.get("id") or ""), reverse=True)


def find_vod(vod_id: str) -> Path | None:
    if not safe_name(vod_id):
        return None
    folder = RECORD_DIR / vod_id
    return folder if folder.is_dir() else None


def _get_drive_vod_row(vod_id: str) -> dict[str, Any] | None:
    if not safe_name(vod_id):
        return None
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM drive_vods WHERE id = ?", (vod_id,)).fetchone()
        return dict(row) if row else None


@app.get("/api/vods")
def api_vods(request: Request, refresh: str = "") -> JSONResponse:
    force = refresh in {"1", "true", "yes"}
    if force:
        rate_limit(request, "vods_refresh", 6, 60)
    folder_url = f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}" if GDRIVE_FOLDER_ID else ""
    return j(
        {"vods": list_vods(force_sync=force), "folder_url": folder_url},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/vods/{vod_id}/chat")
def vod_chat(vod_id: str, request: Request):
    rate_limit(request, "vod_chat", 60, 60)
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)
    chat_headers = {"X-Content-Type-Options": "nosniff"}
    folder = find_vod(vod_id)
    chat = folder / "chat.jsonl" if folder else None
    if chat and chat.is_file():
        return FileResponse(chat, media_type=MIME[".jsonl"], headers=chat_headers)

    row = _get_drive_vod_row(vod_id)
    chat_drive_id = str(row.get("chat_drive_id") or "").strip() if row else ""
    if not chat_drive_id:
        return j({"error": "no chat"}, 404)

    cached_chat = CHAT_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jsonl"
    pub_chat_headers = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if cached_chat.is_file() and cached_chat.stat().st_size > 0:
        return FileResponse(cached_chat, media_type=MIME[".jsonl"], headers=pub_chat_headers)

    raw_chat = _download_gdrive_small_bytes(chat_drive_id, max_bytes=20 * 1024 * 1024)
    if not raw_chat:
        return j({"error": "chat_unavailable"}, 502)

    CHAT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_chat = cached_chat.with_name(f".{cached_chat.stem}.{secrets.token_hex(4)}.jsonl")
    try:
        tmp_chat.write_bytes(raw_chat)
        tmp_chat.replace(cached_chat)
    except OSError:
        return Response(content=raw_chat, media_type=MIME[".jsonl"], headers=chat_headers)
    finally:
        if tmp_chat.exists():
            tmp_chat.unlink(missing_ok=True)

    return FileResponse(cached_chat, media_type=MIME[".jsonl"], headers=pub_chat_headers)


def _parse_vtt_seconds(raw_ts: str) -> float:
    parts = raw_ts.strip().replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def _parse_webvtt_cues(vtt_text: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    blocks = re.split(r"\r?\n\s*\r?\n", vtt_text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        ts_idx = 0 if "-->" in lines[0] else (1 if len(lines) > 1 and "-->" in lines[1] else -1)
        if ts_idx < 0:
            continue
        start_s, _, end_rest = lines[ts_idx].partition("-->")
        end_s = end_rest.strip().split()[0] if end_rest.strip() else "0"
        cue_txt = " ".join(lines[ts_idx + 1 :]).strip()
        if not cue_txt:
            continue
        cues.append(
            {
                "start": round(_parse_vtt_seconds(start_s), 2),
                "end": round(_parse_vtt_seconds(end_s), 2),
                "text": cue_txt[:300],
            }
        )
    return cues[:6000]


@app.get("/api/vods/{vod_id}/transcript")
def vod_transcript(vod_id: str, request: Request, format: str = "json") -> Response:
    rate_limit(request, "vod_transcript", 60, 60)
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)
    raw_vtt: bytes | None = None
    folder = find_vod(vod_id)
    if folder:
        for fn in ("transcript.vtt", "subtitles.vtt"):
            cand = folder / fn
            if cand.is_file():
                raw_vtt = cand.read_bytes()
                break
    if raw_vtt is None:
        row = _get_drive_vod_row(vod_id)
        tr_drive_id = str(row.get("transcript_drive_id") or "").strip() if row else ""
        if not tr_drive_id:
            return j({"cues": []})
        cached_vtt = CHAT_CACHE_DIR / f"sub_{_vod_cache_stem(vod_id)}.vtt"
        if cached_vtt.is_file() and cached_vtt.stat().st_size > 0:
            raw_vtt = cached_vtt.read_bytes()
        else:
            raw_vtt = _download_gdrive_small_bytes(tr_drive_id, max_bytes=10 * 1024 * 1024)
            if raw_vtt:
                CHAT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    cached_vtt.write_bytes(raw_vtt)
    if not raw_vtt:
        return j({"cues": []})
    vtt_str = raw_vtt.decode("utf-8", errors="replace")
    if format == "vtt":
        return Response(content=vtt_str.encode("utf-8"), media_type="text/vtt; charset=utf-8")
    return j({"cues": _parse_webvtt_cues(vtt_str)})


def video_file_for(folder: Path, filename: str) -> Path | None:
    if not safe_name(filename):
        return None
    direct = folder / filename
    if direct.is_file():
        return direct
    fallback = folder / "video.mp4"
    return fallback if fallback.is_file() else None


@app.api_route("/vods/{vod_id}/{filename}", methods=["GET", "HEAD"])
def vod_video(vod_id: str, filename: str, request: Request) -> Response:
    folder = find_vod(vod_id)
    if not folder:
        return j({"error": "not found"}, 404)
    video = video_file_for(folder, filename or "video.mp4")
    if not video:
        return j({"error": "not found"}, 404)
    size = video.stat().st_size
    media_type = MIME.get(video.suffix.lower(), "video/mp4")
    start, end = 0, max(0, size - 1)
    range_header = (request.headers.get("range") or "").strip()
    status_code = 200
    if range_header and range_header.lower().startswith("bytes="):
        if size <= 0:
            return j({"error": "range not satisfiable"}, 416, {"Content-Range": "bytes */0"})
        raw_range = range_header[6:].strip()
        if "," in raw_range or "-" not in raw_range:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        start_s, _, end_s = raw_range.partition("-")
        start_s, end_s = start_s.strip(), end_s.strip()
        if not start_s and not end_s:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        try:
            if not start_s:
                suffix_len = int(end_s)
                if suffix_len <= 0:
                    return j({"error": "range not satisfiable"}, 416, {"Content-Range": f"bytes */{size}"})
                start = max(0, size - suffix_len)
                end = size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
                if start < 0 or start >= size or end < start:
                    return j({"error": "range not satisfiable"}, 416, {"Content-Range": f"bytes */{size}"})
                end = min(end, size - 1)
        except ValueError:
            return j({"error": "bad range"}, 416, {"Content-Range": f"bytes */{size}"})
        status_code = 206
    length = (end - start + 1) if size > 0 else 0

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        return Response(status_code=status_code, media_type=media_type, headers=headers)

    def iterator():
        with video.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iterator(), status_code=status_code, media_type=media_type, headers=headers)


_gdrive_uuid_cache: dict[str, tuple[float, str]] = {}
_gdrive_uuid_lock = threading.Lock()
_gdrive_unreachable_until: float = 0.0
_thumb_locks_guard = threading.Lock()
_thumb_locks: dict[str, threading.Lock] = {}
_thumb_sem = threading.BoundedSemaphore(2)


def _get_thumb_lock(vod_id: str) -> threading.Lock:
    with _thumb_locks_guard:
        if len(_thumb_locks) > 512:
            for k in list(_thumb_locks.keys())[:256]:
                if not _thumb_locks[k].locked():
                    _thumb_locks.pop(k, None)
        lk = _thumb_locks.get(vod_id)
        if lk is None:
            lk = threading.Lock()
            _thumb_locks[vod_id] = lk
        return lk


def _atomic_write_bytes(dest: Path, payload: bytes) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{secrets.token_hex(4)}.jpg")
    try:
        tmp.write_bytes(payload)
        tmp.replace(dest)
        return True
    except OSError:
        return dest.is_file() and dest.stat().st_size > 1024
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _find_ffmpeg_bin() -> str | None:
    for candidate in (ROOT / "ffmpeg.exe", ROOT / "ffmpeg", ROOT / "recorder" / "ffmpeg.exe"):
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which("ffmpeg")


def _extract_ffmpeg_frame(input_target: str, dest: Path, seek_seconds: list[int]) -> bool:
    ffmpeg_exe = _find_ffmpeg_bin()
    if not ffmpeg_exe:
        return False
    is_remote = input_target.startswith("https://")
    proto_whitelist = "http,https,tcp,tls,crypto" if is_remote else "file,crypto,data"
    safe_target = input_target if is_remote else str(Path(input_target).resolve())
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{secrets.token_hex(4)}.jpg")
    for ss in seek_seconds:
        try:
            subprocess.run(
                [
                    ffmpeg_exe, "-nostdin", "-y",
                    "-protocol_whitelist", proto_whitelist,
                    "-ss", str(int(ss)),
                    "-i", safe_target,
                    "-frames:v", "1",
                    "-vf", "scale=960:-2",
                    "-q:v", "3",
                    str(tmp),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_WIN_NO_WINDOW,
                timeout=22,
                check=False,
            )
            if tmp.is_file() and tmp.stat().st_size > 1024:
                try:
                    tmp.replace(dest)
                except OSError:
                    pass
                return dest.is_file() and dest.stat().st_size > 1024
        except Exception:
            pass
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    return False


def _cache_gdrive_uuid(drive_id: str, found_uuid: str) -> None:
    now = time.time()
    with _gdrive_uuid_lock:
        if len(_gdrive_uuid_cache) > 512:
            expired = [k for k, v in _gdrive_uuid_cache.items() if v[0] <= now]
            for k in expired:
                _gdrive_uuid_cache.pop(k, None)
            if len(_gdrive_uuid_cache) > 512:
                _gdrive_uuid_cache.pop(next(iter(_gdrive_uuid_cache)))
        _gdrive_uuid_cache[drive_id] = (now + 1200, found_uuid)


def _resolve_gdrive_direct_url(drive_id: str) -> str | None:
    global _gdrive_unreachable_until
    if not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return None
    now = time.time()
    if now < _gdrive_unreachable_until:
        return None
    with _gdrive_uuid_lock:
        entry = _gdrive_uuid_cache.get(drive_id)
        if entry and entry[0] > now and _SAFE_GDRIVE_UUID_RE.fullmatch(entry[1]):
            return f"https://drive.usercontent.google.com/download?id={drive_id}&export=download&confirm=t&uuid={entry[1]}"

    base_url = "https://drive.usercontent.google.com/download"
    params = {"id": drive_id, "export": "download", "confirm": "t"}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Range": "bytes=0-1023",
    }
    try:
        with requests.get(base_url, params=params, headers=headers, stream=True, timeout=(3, 10)) as r:
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/html" in ctype:
                sample = _read_response_prefix(r)
                found_uuid = _extract_safe_gdrive_uuid(sample)
                if found_uuid:
                    _cache_gdrive_uuid(drive_id, found_uuid)
                    return f"{base_url}?id={drive_id}&export=download&confirm=t&uuid={found_uuid}"
                return None
            if r.status_code in (200, 206):
                return f"{base_url}?id={drive_id}&export=download&confirm=t"
    except (requests.RequestException, OSError):
        _gdrive_unreachable_until = time.time() + 25
    return None


@app.get("/api/vods/{vod_id}/thumb")
def vod_thumb(vod_id: str) -> Response:
    global _gdrive_unreachable_until
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)

    cache_headers = {"Cache-Control": "public, max-age=86400"}
    folder = find_vod(vod_id)
    if folder:
        thumb_path = folder / "thumb.jpg"
        if thumb_path.is_file() and thumb_path.stat().st_size > 512:
            return FileResponse(thumb_path, media_type="image/jpeg", headers=cache_headers)
        meta_file = folder / "meta.json"
        video_name = "video.mp4"
        duration_s = 0
        if meta_file.is_file():
            try:
                m = json.loads(meta_file.read_text(encoding="utf-8"))
                video_name = text(m.get("video_file"), 120) or "video.mp4"
                duration_s = int(m.get("duration_s") or 0)
            except Exception:
                pass
        video = video_file_for(folder, video_name)
        if video:
            with _get_thumb_lock(vod_id):
                if not (thumb_path.is_file() and thumb_path.stat().st_size > 512):
                    with _thumb_sem:
                        seeks = [min(180, max(5, duration_s // 5)) if duration_s > 10 else 5, 1]
                        _extract_ffmpeg_frame(str(video), thumb_path, seeks)
            if thumb_path.is_file() and thumb_path.stat().st_size > 512:
                return FileResponse(thumb_path, media_type="image/jpeg", headers=cache_headers)
        return j({"error": "no thumb"}, 404)

    row = _get_drive_vod_row(vod_id)
    drive_id = str(row.get("drive_id") or "").strip() if row else ""
    thumb_drive_id = str(row.get("thumb_drive_id") or "").strip() if row else ""
    if not drive_id:
        return j({"error": "not found"}, 404)

    cached_path = THUMB_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jpg"
    if cached_path.is_file() and cached_path.stat().st_size > 512:
        return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)

    if time.time() < _gdrive_unreachable_until:
        return j({"error": "thumb_unavailable"}, 404)

    with _get_thumb_lock(vod_id):
        if cached_path.is_file() and cached_path.stat().st_size > 512:
            return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)
        if time.time() < _gdrive_unreachable_until:
            return j({"error": "thumb_unavailable"}, 404)

        with _thumb_sem:
            # 1) Если в подпапке записи на Google Drive лежит готовый thumb.jpg — забираем его напрямую
            if thumb_drive_id:
                raw_thumb = _download_gdrive_small_bytes(thumb_drive_id, max_bytes=4 * 1024 * 1024)
                if raw_thumb and len(raw_thumb) > 512:
                    if _atomic_write_bytes(cached_path, raw_thumb):
                        return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)

            # 2) Локальный ffmpeg по прямому потоку видео
            if _find_ffmpeg_bin():
                direct_url = _resolve_gdrive_direct_url(drive_id)
                if direct_url and _extract_ffmpeg_frame(direct_url, cached_path, [180, 30, 3]):
                    return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)

            # 3) Превью от Google Drive (для thumb.jpg или для video.mp4)
            if time.time() >= _gdrive_unreachable_until:
                ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
                candidate_urls: list[str] = []
                if thumb_drive_id:
                    candidate_urls.append(f"https://drive.google.com/thumbnail?id={thumb_drive_id}&sz=w960")
                    candidate_urls.append(f"https://lh3.googleusercontent.com/d/{thumb_drive_id}=w960")
                candidate_urls.append(f"https://drive.google.com/thumbnail?id={drive_id}&sz=w960")
                candidate_urls.append(f"https://lh3.googleusercontent.com/d/{drive_id}=w960")
                for candidate_url in candidate_urls:
                    try:
                        r = requests.get(candidate_url, headers=ua, timeout=(3, 10))
                        ctype = (r.headers.get("content-type") or "").lower()
                        if r.status_code == 200 and ctype.startswith("image/") and len(r.content) > 512:
                            if _atomic_write_bytes(cached_path, r.content):
                                return FileResponse(cached_path, media_type="image/jpeg", headers=cache_headers)
                    except (requests.RequestException, OSError):
                        continue

    return j({"error": "thumb_unavailable"}, 404)


@app.post("/api/vods/{vod_id}/thumb")
async def cache_client_vod_thumb(vod_id: str, request: Request) -> JSONResponse:
    if not safe_name(vod_id):
        return j({"error": "not found"}, 404)
    require_same_origin(request)
    require_admin(request, DEFAULT_PROFILE)
    rate_limit(request, "vod_thumb_cache", 24, 60)

    folder = find_vod(vod_id)
    if folder:
        dest = folder / "thumb.jpg"
    elif _resolve_drive_id(vod_id):
        dest = THUMB_CACHE_DIR / f"{_vod_cache_stem(vod_id)}.jpg"
    else:
        return j({"error": "not found"}, 404)

    if dest.is_file() and dest.stat().st_size > 1024:
        return j({"cached": True, "already": True})

    raw = await request.body()
    if len(raw) < 1024 or len(raw) > 524288:
        return j({"error": "invalid_size"}, 400)
    if raw[:3] != b"\xff\xd8\xff" or raw[-2:] != b"\xff\xd9":
        return j({"error": "invalid_jpeg"}, 400)

    def _save_thumb_sync() -> None:
        with _get_thumb_lock(vod_id):
            if not (dest.is_file() and dest.stat().st_size > 1024):
                _atomic_write_bytes(dest, raw)

    await asyncio.to_thread(_save_thumb_sync)
    return j({"cached": True})


def _resolve_drive_id(vod_id: str) -> str | None:
    if not safe_name(vod_id):
        return None
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT drive_id FROM drive_vods WHERE id = ?", (vod_id,)).fetchone()
        return str(row["drive_id"]) if row and row["drive_id"] else None


@app.api_route("/api/vods/{vod_id}/stream", methods=["GET", "HEAD"])
def vod_stream(vod_id: str, request: Request) -> Response:
    folder = find_vod(vod_id)
    if folder:
        meta_path = folder / "meta.json"
        video_name = "video.mp4"
        if meta_path.is_file():
            try:
                meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
                video_name = str(meta_obj.get("video_file") or "video.mp4")
            except Exception:
                pass
        return vod_video(vod_id, video_name, request)

    drive_id = _resolve_drive_id(vod_id)
    if not drive_id or not _SAFE_GDRIVE_ID_RE.fullmatch(drive_id):
        return j({"error": "not found"}, 404)

    now = time.time()
    cached_uuid = ""
    with _gdrive_uuid_lock:
        entry = _gdrive_uuid_cache.get(drive_id)
        if entry and entry[0] > now and _SAFE_GDRIVE_UUID_RE.fullmatch(entry[1]):
            cached_uuid = entry[1]

    req_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }
    range_header = (request.headers.get("range") or "").strip()
    if range_header:
        if not re.fullmatch(r"bytes=\d{0,16}-\d{0,16}", range_header, flags=re.IGNORECASE):
            return j({"error": "bad range"}, 416)
        req_headers["Range"] = range_header

    base_url = "https://drive.usercontent.google.com/download"
    params: dict[str, str] = {"id": drive_id, "export": "download", "confirm": "t"}
    if cached_uuid:
        params["uuid"] = cached_uuid

    session = requests.Session()
    try:
        upstream = session.request(
            "GET" if request.method == "HEAD" and not cached_uuid else request.method,
            base_url,
            params=params,
            headers=req_headers,
            stream=True,
            timeout=(4, 30),
        )
        ctype = (upstream.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            if cached_uuid:
                with _gdrive_uuid_lock:
                    _gdrive_uuid_cache.pop(drive_id, None)
            html_sample = _read_response_prefix(upstream)
            upstream.close()
            found_uuid = _extract_safe_gdrive_uuid(html_sample)
            if not found_uuid and request.method == "HEAD" and cached_uuid:
                probe_params = {"id": drive_id, "export": "download", "confirm": "t"}
                probe_headers = dict(req_headers)
                probe_headers.setdefault("Range", "bytes=0-1023")
                with session.get(base_url, params=probe_params, headers=probe_headers, stream=True, timeout=(4, 15)) as probe:
                    if "text/html" in (probe.headers.get("content-type") or "").lower():
                        found_uuid = _extract_safe_gdrive_uuid(_read_response_prefix(probe))
            if found_uuid:
                _cache_gdrive_uuid(drive_id, found_uuid)
                params["uuid"] = found_uuid
                upstream = session.request(
                    request.method,
                    base_url,
                    params=params,
                    headers=req_headers,
                    stream=True,
                    timeout=(4, 30),
                )
                ctype = (upstream.headers.get("content-type") or "").lower()

        if upstream.status_code >= 400 or "text/html" in ctype:
            upstream.close()
            session.close()
            return j({"error": "upstream_unavailable"}, 502)
    except (requests.RequestException, OSError):
        session.close()
        return j({"error": "upstream_unreachable"}, 502)

    want_download = (request.query_params.get("download") or "").strip() == "1"
    dl_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", vod_id)[:80].strip("_.") or "stream"
    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{dl_name}.mp4"' if want_download else "inline",
    }
    for h_name in ("Content-Length", "Content-Range"):
        val = upstream.headers.get(h_name)
        if val:
            out_headers[h_name] = val
    raw_mime = (upstream.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    media_type = raw_mime if raw_mime in _SAFE_STREAM_MIMES else "video/mp4"
    status_code = upstream.status_code if upstream.status_code in (200, 206) else 200

    if request.method == "HEAD":
        upstream.close()
        session.close()
        return Response(status_code=status_code, media_type=media_type, headers=out_headers)

    def stream_chunks():
        sent = 0
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    sent += len(chunk)
                    yield chunk
        except (requests.RequestException, OSError):
            return
        finally:
            if sent > 0:
                with contextlib.suppress(Exception):
                    record_vod_stream_bytes(vod_id, sent)
            upstream.close()
            session.close()

    return StreamingResponse(stream_chunks(), status_code=status_code, media_type=media_type, headers=out_headers)


# ----------------------------- Статика -----------------------------

STATIC_ALLOWED = {
    ".html", ".css", ".js", ".xml", ".png", ".jpg", ".jpeg",
    ".webp", ".svg", ".ico", ".mp4", ".webm", ".mkv", ".mov",
    ".webmanifest",
}
STATIC_EXACT_NAMES = {"CNAME", "robots.txt", "manifest.json"}
STATIC_BLOCKED_ROOTS = {
    "data", "recorder", "vods", ".git", ".github", ".venv", "venv", "__pycache__",
    "etc", "tests", ".local", ".cache", "brain", "scratch",
}

# Явный список не затрагивает API, файлы и несуществующие адреса.
_STATIC_PAGE_REDIRECTS = {"/index.html": "/", "/favicon.ico": "/favicon.png"}
for _page in ("watchlist", "gamelist", "setup", "wishlist", "danonsha", "wrapped", "suggest"):
    _STATIC_PAGE_REDIRECTS[f"/{_page}.html"] = f"/{_page}"
    _STATIC_PAGE_REDIRECTS[f"/{_page}/"] = f"/{_page}"
_STATIC_PAGE_REDIRECTS["/404.html"] = "/404"
_STATIC_PAGE_REDIRECTS["/404/"] = "/404"
_STATIC_PAGE_REDIRECTS["/recap"] = "/wrapped"
_STATIC_PAGE_REDIRECTS["/recap/"] = "/wrapped"
_STATIC_PAGE_REDIRECTS["/recap.html"] = "/wrapped"

_REDIRECTS_FILE = ROOT / "_redirects"
if _REDIRECTS_FILE.is_file():
    try:
        for _r_line in _REDIRECTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            _r_line = _r_line.strip()
            if not _r_line or _r_line.startswith("#"):
                continue
            _r_parts = _r_line.split()
            if len(_r_parts) >= 2:
                _r_src, _r_dst = _r_parts[0].strip(), _r_parts[1].strip()
                if _r_src.startswith("/"):
                    _STATIC_PAGE_REDIRECTS[_r_src] = _r_dst
                    _clean_src = _r_src.rstrip("/")
                    if _clean_src:
                        _STATIC_PAGE_REDIRECTS[_clean_src] = _r_dst
                        _STATIC_PAGE_REDIRECTS[_clean_src + "/"] = _r_dst
    except OSError:
        pass


def resolve_static(full_path: str) -> Path | None:
    rel = (full_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        rel = "index.html"
    elif rel.endswith("/"):
        trimmed = rel.rstrip("/")
        if trimmed and "." not in trimmed.split("/")[-1]:
            candidate_html = (ROOT / (trimmed + ".html")).resolve()
            try:
                candidate_html.relative_to(ROOT)
                if candidate_html.is_file():
                    rel = trimmed + ".html"
                else:
                    rel += "index.html"
            except Exception:
                return None
        else:
            rel += "index.html"
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return None
    if parts[0].lower() in STATIC_BLOCKED_ROOTS:
        return None
    path = (ROOT / rel).resolve()
    try:
        path.relative_to(ROOT)
    except Exception:
        return None
    # Страница рядом с одноимённым каталогом важнее каталога:
    # /danonsha -> danonsha.html, а не danonsha/index.html.
    if not path.suffix and path.name not in STATIC_EXACT_NAMES:
        sibling = path.with_name(path.name + ".html")
        if sibling.is_file():
            path = sibling
    if path.is_dir():
        path = path / "index.html"
    if not path.suffix and path.name not in STATIC_EXACT_NAMES and path.with_name(path.name + ".html").is_file():
        path = path.with_name(path.name + ".html")
    if path.name not in STATIC_EXACT_NAMES and path.suffix.lower() not in STATIC_ALLOWED:
        return None
    if not path.is_file():
        return None
    return path


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
def static_file(full_path: str, request: Request) -> Response:
    target = _STATIC_PAGE_REDIRECTS.get("/" + full_path)
    if target is not None:
        query = request.url.query
        is_external = target.startswith(("http://", "https://"))
        dest = target
        if query:
            sep = "&" if ("?" in dest) else "?"
            dest += f"{sep}{query}"
        return RedirectResponse(dest, status_code=302 if is_external else 301)

    path = resolve_static(full_path)
    if not path:
        accept = request.headers.get("accept", "")
        has_static_ext = bool(Path(full_path).suffix)
        is_json_req = full_path.startswith("api/") or ("application/json" in accept and "text/html" not in accept)
        if is_json_req:
            return j({"error": "not found"}, 404)
        if has_static_ext and Path(full_path).suffix.lower() not in {".html", ".htm"}:
            return Response(b"Not Found", status_code=404, media_type="text/plain; charset=utf-8")
        err_page = ROOT / "404.html"
        if err_page.is_file():
            path = err_page
        else:
            return j({"error": "not found"}, 404)

    ext = path.suffix.lower()
    if ext == ".html":
        status = 404 if path.name.lower() == "404.html" else 200
        nonce = b64url(secrets.token_bytes(16))
        body = path.read_text(encoding="utf-8", errors="replace").replace("__CSP_NONCE__", nonce)
        encoded = body.encode("utf-8")
        headers = {
            "Cache-Control": "private, no-cache, no-store, must-revalidate" if status == 404 else "private, no-cache, no-transform",
            "Content-Length": str(len(encoded)),
        }
        # Панель управления, личный профиль и служебные страницы не индексируются.
        relative = path.relative_to(ROOT).as_posix()
        if path.name.lower() in _NOINDEX_PAGES or relative.startswith("danonsha/"):
            headers["X-Robots-Tag"] = "noindex, nofollow"
        headers.update(
            security_headers(
                nonce,
                is_https=is_https_request(request),
                allow_frame=_is_e2b_preview_request(request),
            )
        )
        if request.method == "HEAD":
            return Response(status_code=status, media_type=MIME[".html"], headers=headers)
        return Response(content=encoded, status_code=status, media_type=MIME[".html"], headers=headers)

    media_type = MIME.get(ext, "text/plain; charset=utf-8")
    try:
        st = path.stat()
        etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
    except OSError:
        etag = ""

    if path.name == "sw.js":
        cache_ctrl = "no-cache, no-store, must-revalidate"
    elif ext in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".mp4", ".webm", ".mkv", ".mov"}:
        cache_ctrl = "public, max-age=2592000, stale-while-revalidate=86400"
    elif ext in {".webmanifest"} or path.name == "manifest.json":
        cache_ctrl = "public, max-age=86400, stale-while-revalidate=604800"
    elif ext in {".css", ".js"}:
        cache_ctrl = "public, max-age=86400, stale-while-revalidate=604800"
    else:
        cache_ctrl = "public, max-age=86400"

    if etag and (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_ctrl})

    if ext in {".css", ".js", ".svg", ".xml", ".txt", ".webmanifest"} and st.st_size <= 2 * 1024 * 1024:
        raw_bytes = path.read_bytes()
        headers = {"Cache-Control": cache_ctrl, "Content-Length": str(len(raw_bytes))}
        if path.name == "sw.js":
            headers["Service-Worker-Allowed"] = "/"
        if etag:
            headers["ETag"] = etag
        if request.method == "HEAD":
            return Response(status_code=200, media_type=media_type, headers=headers)
        return Response(content=raw_bytes, status_code=200, media_type=media_type, headers=headers)

    extra_headers: dict[str, str] = {"Cache-Control": cache_ctrl}
    if etag:
        extra_headers["ETag"] = etag
    return FileResponse(path, media_type=media_type, headers=extra_headers)


def _start_auto_update_watcher() -> None:
    interval = max(60, AUTO_UPDATE_INTERVAL)

    def _loop() -> None:
        while True:
            time.sleep(interval)
            res = perform_github_update(force=False, allow_zip_fallback_on_api_error=False)
            if res.get("ok") and res.get("server_changed"):
                _reexec_current_process()

    t = threading.Thread(target=_loop, name="krafen-auto-updater", daemon=True)
    t.start()
    print(f"автообновление: фоновая проверка каждые {interval} сек.", flush=True)


def start_recorder() -> subprocess.Popen | None:
    script = ROOT / "recorder" / "recorder.py"
    if not script.is_file():
        print("recorder/recorder.py не найден — автозапись выключена", flush=True)
        return None
    env = os.environ.copy()
    env.setdefault("RECORD_DIR", str(RECORD_DIR))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    print("автозапись: запускаю recorder/recorder.py", flush=True)
    kwargs: dict[str, Any] = {
        "env": env,
        "cwd": str(ROOT),
    }
    if _HEADLESS_LOG_FH is not None:
        exe = Path(sys.executable).resolve()
        py_console = exe.with_name("python.exe")
        runner = str(py_console if py_console.is_file() else exe)
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = _HEADLESS_LOG_FH
        kwargs["stderr"] = _HEADLESS_LOG_FH
        kwargs["creationflags"] = _WIN_NO_WINDOW
    else:
        runner = sys.executable
    return subprocess.Popen([runner, str(script)], **kwargs)


def main() -> None:
    global _ACTIVE_REC_PROC
    _configure_stdio_utf8(mirror_console_to_log=True)
    mode_label = "фоновый режим" if _HEADLESS_LOG_FH is not None else "консоль"
    print(f"\n=== [{time.strftime('%Y-%m-%d %H:%M:%S')}] запуск krafen ({mode_label}, PID {os.getpid()}) ===", flush=True)
    with contextlib.suppress(OSError):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if is_auto_update_enabled() and AUTO_UPDATE_INTERVAL > 0:
        _start_auto_update_watcher()
    if _ADMIN_PASSWORD_IS_PLACEHOLDER:
        print("ВНИМАНИЕ: ADMIN_PASSWORD оставлен как 'change-me' из примера — вход в админ-режим отключён до смены пароля", flush=True)
    elif not ADMIN_PASSWORD:
        print("ВНИМАНИЕ: ADMIN_PASSWORD не задан, вход в админ-режим отключён", flush=True)
    rec_proc = start_recorder() if os.environ.get("RECORDER") == "1" else None
    _ACTIVE_REC_PROC = rec_proc
    ssl_certfile = (os.environ.get("SSL_CERTFILE") or "").strip() or None
    ssl_keyfile = (os.environ.get("SSL_KEYFILE") or "").strip() or None
    scheme_lbl = "https" if (ssl_certfile and ssl_keyfile) else "http"
    print(f"krafen server → {scheme_lbl}://{HOST}:{PORT}", flush=True)
    print(f"статика: {ROOT}", flush=True)
    print(f"база данных: {DB_PATH}", flush=True)
    print(f"папка записей: {RECORD_DIR}", flush=True)
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            log_level="info",
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
    finally:
        _ACTIVE_REC_PROC = None
        if _read_pid_file() == os.getpid():
            with contextlib.suppress(OSError):
                PID_FILE_PATH.unlink(missing_ok=True)
        if rec_proc and rec_proc.poll() is None:
            rec_proc.terminate()
            try:
                rec_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rec_proc.kill()
            print("рекордер остановлен", flush=True)


if __name__ == "__main__":
    main()
