#!/usr/bin/env python3
"""Local WeChat content production workbench for a single public account.

Run with ``python app.py`` and open http://127.0.0.1:8765.
The server deliberately binds to loopback only. Secrets are read from env vars.
"""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import io
import json
import mimetypes
import os
import re
import socket
import ssl
import struct
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    import certifi  # type: ignore
    TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    TLS_CONTEXT = ssl.create_default_context()

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except Exception:
    Image = ImageDraw = ImageFont = None


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".workbench"
ASSET_DIR = DATA_DIR / "assets"
DB_PATH = DATA_DIR / "workbench.sqlite3"
HOST = "127.0.0.1"
PORT = int(os.getenv("WORKBENCH_PORT", "8765"))
AIHOT_BASE = "https://aihot.virxact.com/api/v1"
USER_AGENT = "WeChatContentWorkbench/1.0 (+local)"
AUTO_IMAGE_JOBS: dict[str, dict] = {}
AUTO_IMAGE_JOBS_LOCK = threading.Lock()
LENGTH_PRESETS = {
    "compact": {"label": "精简 · 1200–1800 字", "minimum": 1200, "maximum": 1800},
    "standard": {"label": "标准 · 2200–3200 字", "minimum": 2200, "maximum": 3200},
    "deep": {"label": "深度 · 3500–5000 字", "minimum": 3500, "maximum": 5000},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def redact_sensitive(value: object) -> str:
    message = str(value)
    for env_name in ("OPENAI_API_KEY", "OPENAI_IMAGE_API_KEY", "DEEPSEEK_API_KEY", "YUZAPI_API_KEY", "YUZ_API_KEY", "RIGHTCODE_API_KEY", "RIGHT_CODE_API_KEY", "RIGHTCODE_IMAGE_API_KEY", "RIGHT_CODE_IMAGE_API_KEY", "WECHAT_APP_SECRET", "WECHAT_APP_ID"):
        secret = os.getenv(env_name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def safe_json_load(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def db_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    ASSET_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_item (
              id INTEGER PRIMARY KEY,
              external_id TEXT UNIQUE,
              title TEXT NOT NULL,
              summary TEXT DEFAULT '',
              source_name TEXT DEFAULT '',
              source_url TEXT DEFAULT '',
              aihot_url TEXT DEFAULT '',
              category TEXT DEFAULT '',
              published_at TEXT DEFAULT '',
              fetched_at TEXT NOT NULL,
              raw_json TEXT DEFAULT '{}',
              is_selected INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS topic (
              id INTEGER PRIMARY KEY,
              source_id INTEGER,
              title TEXT NOT NULL,
              core_angle TEXT DEFAULT '',
              audience TEXT DEFAULT '',
              personal_observation TEXT DEFAULT '',
              lived_experience TEXT DEFAULT '',
              emotional_note TEXT DEFAULT '',
              h_score INTEGER DEFAULT 0,
              k_score INTEGER DEFAULT 0,
              r_score INTEGER DEFAULT 0,
              window TEXT DEFAULT '7d',
              status TEXT DEFAULT '待判断',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES source_item(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS draft (
              id INTEGER PRIMARY KEY,
              topic_id INTEGER,
              title TEXT DEFAULT '',
              digest TEXT DEFAULT '',
              body TEXT DEFAULT '',
              outline TEXT DEFAULT '[]',
              evidence TEXT DEFAULT '[]',
              title_candidates TEXT DEFAULT '[]',
              claims TEXT DEFAULT '[]',
              length_preset TEXT DEFAULT 'standard',
              style_profile_id INTEGER,
              quality_report TEXT DEFAULT '{}',
              status TEXT DEFAULT '写作中',
              cover_asset_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(topic_id) REFERENCES topic(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS asset (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              path TEXT NOT NULL,
              kind TEXT DEFAULT 'image',
              source_url TEXT DEFAULT '',
              source_page_url TEXT DEFAULT '',
              source_kind TEXT DEFAULT 'unknown',
              rights_note TEXT DEFAULT '待人工确认',
              prompt TEXT DEFAULT '',
              usage TEXT DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publish_job (
              id INTEGER PRIMARY KEY,
              draft_id INTEGER NOT NULL,
              media_id TEXT DEFAULT '',
              status TEXT DEFAULT '待授权',
              message TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(draft_id) REFERENCES draft(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS metric_record (
              id INTEGER PRIMARY KEY,
              article_key TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              metric_type TEXT NOT NULL,
              value REAL,
              raw_json TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS published_article (
              id INTEGER PRIMARY KEY,
              draft_id INTEGER,
              wechat_media_id TEXT DEFAULT '',
              wechat_msgid TEXT DEFAULT '',
              title TEXT NOT NULL,
              article_url TEXT DEFAULT '',
              published_at TEXT NOT NULL,
              match_status TEXT DEFAULT 'manual_confirmed',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(draft_id) REFERENCES draft(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS metric_daily (
              id INTEGER PRIMARY KEY,
              article_key TEXT NOT NULL,
              metric_date TEXT NOT NULL,
              metric_type TEXT NOT NULL,
              value REAL NOT NULL DEFAULT 0,
              source TEXT NOT NULL DEFAULT 'wechat_api',
              raw_json TEXT DEFAULT '{}',
              synced_at TEXT NOT NULL,
              UNIQUE(article_key, metric_date, metric_type, source)
            );
            CREATE TABLE IF NOT EXISTS metric_sync_run (
              id INTEGER PRIMARY KEY,
              date_from TEXT NOT NULL,
              date_to TEXT NOT NULL,
              status TEXT NOT NULL,
              requested_days INTEGER NOT NULL DEFAULT 0,
              succeeded_days INTEGER NOT NULL DEFAULT 0,
              article_count INTEGER NOT NULL DEFAULT 0,
              metric_count INTEGER NOT NULL DEFAULT 0,
              error_message TEXT DEFAULT '',
              started_at TEXT NOT NULL,
              finished_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS setting (
              key TEXT PRIMARY KEY,
              value TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS style_profile (
              id INTEGER PRIMARY KEY,
              name TEXT UNIQUE NOT NULL,
              rules TEXT DEFAULT '',
              preferences TEXT DEFAULT '',
              sample_names TEXT DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(draft)").fetchall()}
        for column, definition in (("title_candidates", "TEXT DEFAULT '[]'"), ("claims", "TEXT DEFAULT '[]'"), ("length_preset", "TEXT DEFAULT 'standard'"), ("style_profile_id", "INTEGER"),
                                   ("model_provider", "TEXT DEFAULT ''"), ("model_name", "TEXT DEFAULT ''"), ("model_fallback", "INTEGER DEFAULT 0"), ("model_note", "TEXT DEFAULT ''")):
            if column not in draft_columns:
                conn.execute(f"ALTER TABLE draft ADD COLUMN {column} {definition}")
        asset_columns = {row[1] for row in conn.execute("PRAGMA table_info(asset)").fetchall()}
        for column, definition in (("source_page_url", "TEXT DEFAULT ''"), ("source_kind", "TEXT DEFAULT 'unknown'"), ("source_scope", "TEXT DEFAULT ''")):
            if column not in asset_columns:
                conn.execute(f"ALTER TABLE asset ADD COLUMN {column} {definition}")


def table_rows(table: str, limit: int = 100) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


BACKUP_TABLES = ("source_item", "topic", "draft", "asset", "publish_job", "metric_record",
                 "published_article", "metric_daily", "metric_sync_run", "style_profile")


def backup_payload() -> dict:
    backup = {
        "version": 2,
        "exported_at": now_iso(),
        "style": {"name": "数字生命卡兹克", "skill_path": STYLE_CONTEXT.get("skill_path", "")},
        "asset_files": {},
    }
    with db_conn() as conn:
        for table in BACKUP_TABLES:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            backup[table] = [dict(row) for row in rows]
    return backup


def backup_package() -> bytes:
    backup = backup_payload()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for asset in backup.get("asset", []):
            try:
                path = safe_relative_path(str(asset.get("path", "")))
            except ValueError:
                continue
            if not path.exists() or not path.is_file():
                continue
            archive_name = f"assets/{asset['id']}-{re.sub(r'[^A-Za-z0-9._-]+', '-', path.name)}"
            backup["asset_files"][str(asset.get("path", ""))] = archive_name
            bundle.write(path, archive_name)
        bundle.writestr("backup.json", json.dumps(backup, ensure_ascii=False, indent=2))
    return archive.getvalue()


def restore_backup(backup: dict, archived_files: dict[str, bytes] | None = None) -> dict:
    if not isinstance(backup, dict) or int(backup.get("version", 0) or 0) < 1:
        raise ValueError("不是可识别的工作台备份")
    archived_files = archived_files or {}
    source_asset_paths = backup.get("asset_files") or {}
    restored_assets = 0
    assets = []
    for raw_asset in backup.get("asset", []):
        asset = dict(raw_asset)
        original_path = str(asset.get("path", ""))
        archive_name = source_asset_paths.get(original_path, "")
        if archive_name in archived_files:
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(original_path).name or "asset")
            restored_name = f"restored-{asset.get('id', uuid.uuid4().hex[:8])}-{safe_name}"
            output = ASSET_DIR / restored_name
            output.write_bytes(archived_files[archive_name])
            asset["path"] = str(output.relative_to(ROOT))
            restored_assets += 1
        assets.append(asset)
    backup["asset"] = assets

    with db_conn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in reversed(BACKUP_TABLES):
            conn.execute(f"DELETE FROM {table}")
        for table in BACKUP_TABLES:
            rows = backup.get(table) or []
            if not rows:
                continue
            table_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for raw_row in rows:
                row = {key: value for key, value in dict(raw_row).items() if key in table_columns}
                if not row:
                    continue
                columns = list(row)
                placeholders = ",".join("?" for _ in columns)
                conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", [row[column] for column in columns])
        conn.execute("PRAGMA foreign_keys = ON")
    return {"ok": True, "message": "工作台备份已恢复", "restored_assets": restored_assets,
            "counts": {table: len(backup.get(table) or []) for table in BACKUP_TABLES}}


def locate_skill() -> Path | None:
    candidates = [
        os.getenv("KHAZIX_SKILL_PATH", ""),
        str(Path.home() / ".codex/skills/khazix-writer/SKILL.md"),
        str(Path.home() / ".agents/skills/khazix-writer/SKILL.md"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def load_style_context() -> dict:
    skill_path = locate_skill()
    skill_text = skill_path.read_text(encoding="utf-8", errors="ignore") if skill_path else ""
    samples: list[dict[str, str]] = []
    for path in sorted(ROOT.glob("*.md")):
        if any(marker in path.name for marker in ("公众号", "来源卡", "质检报告", "初稿", "素材清单")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            samples.append({"name": path.name, "excerpt": text[:2400]})
    return {"skill_path": str(skill_path) if skill_path else "", "skill_excerpt": skill_text[:16000], "samples": samples[:18]}


STYLE_CONTEXT = load_style_context()


def seed_style_profile() -> None:
    timestamp = now_iso()
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO style_profile(name,rules,preferences,sample_names,created_at,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET rules=excluded.rules,
            sample_names=excluded.sample_names, updated_at=excluded.updated_at""",
            ("数字生命卡兹克", STYLE_CONTEXT.get("skill_excerpt", ""), "",
             json.dumps([sample.get("name", "") for sample in STYLE_CONTEXT.get("samples", [])], ensure_ascii=False), timestamp, timestamp),
        )


def style_preferences() -> str:
    with db_conn() as conn:
        row = conn.execute("SELECT preferences FROM style_profile WHERE name=?", ("数字生命卡兹克",)).fetchone()
    return str(row["preferences"] or "") if row else ""


def current_style_profile_id() -> int | None:
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM style_profile WHERE name=?", ("数字生命卡兹克",)).fetchone()
    return int(row["id"]) if row else None


def normalize_source(item: dict, category: str = "") -> dict:
    source = item.get("source") or {}
    links = item.get("links") or {}
    published = item.get("publishedAt") or item.get("published_at") or item.get("published") or ""
    title = str(item.get("title") or item.get("name") or "未命名热点").strip()
    external_id = str(item.get("id") or item.get("publicId") or hashlib.sha1((title + str(links)).encode()).hexdigest()[:16])
    return {
        "external_id": external_id,
        "title": title,
        "summary": str(item.get("summary") or item.get("description") or item.get("digest") or "").strip(),
        "source_name": str(source.get("name") if isinstance(source, dict) else source or item.get("sourceName") or "").strip(),
        "source_url": str(links.get("original") or item.get("url") or item.get("sourceUrl") or "").strip(),
        "aihot_url": str(links.get("aihot") or item.get("permalink") or "").strip(),
        "category": category or str(item.get("category") or "").strip(),
        "published_at": str(published),
        "raw_json": item,
        "is_selected": 1 if item.get("selected", True) else 0,
    }


def http_json(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None, timeout: int = 12) -> tuple[int, dict, dict]:
    body = None
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json_bytes(payload)
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
            raw = response.read(8 * 1024 * 1024)
            return response.status, json.loads(raw.decode("utf-8", errors="replace")), dict(response.headers.items())
    except HTTPError as exc:
        if exc.code == 304:
            return 304, {}, dict(exc.headers.items())
        try:
            raw = exc.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {"error": {"message": raw}}
        except Exception:
            data = {"error": {"message": redact_sensitive(str(exc))}}
        try:
            data = json.loads(redact_sensitive(json.dumps(data, ensure_ascii=False)))
        except Exception:
            data = {"error": {"message": redact_sensitive(str(data))}}
        return exc.code, data, dict(exc.headers.items())
    except Exception as exc:
        # Keep the exception class in the diagnostic.  A bare socket timeout
        # often has an empty string on macOS, which used to surface only as
        # “模型请求失败” and made a provider outage look like bad JSON.
        detail = str(exc).strip()
        detail = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return 0, {"error": redact_sensitive(detail)}, {}


def sync_aihot(window: str = "24h", category: str = "") -> dict:
    with db_conn() as conn:
        last_sync = conn.execute("SELECT value FROM setting WHERE key=?", ("aihot_last_sync_at",)).fetchone()
    if last_sync:
        try:
            elapsed = time.time() - float(last_sync["value"])
            if elapsed < 8:
                with db_conn() as conn:
                    count = conn.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()["count"]
                return {"ok": True, "cached": True, "count": 0, "inserted": 0, "message": f"为避免频繁请求，沿用本地热点（{count} 条）"}
        except (TypeError, ValueError):
            pass
    with db_conn() as conn:
        conn.execute("INSERT INTO setting(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("aihot_last_sync_at", str(time.time())))
    params = {"mode": "selected", "window": window if window in {"24h", "7d"} else "24h", "limit": "30"}
    if category:
        params["category"] = category
    query = "&".join(f"{quote(k)}={quote(str(v))}" for k, v in params.items())
    url = f"{AIHOT_BASE}/items?{query}"
    headers = {}
    with db_conn() as conn:
        etag = conn.execute("SELECT value FROM setting WHERE key=?", ("aihot_etag:" + query,)).fetchone()
        if etag:
            headers["If-None-Match"] = etag["value"]
    status, data, response_headers = http_json(url, headers=headers)
    if status == 304:
        return {"ok": True, "cached": True, "count": 0, "message": "AI HOT 没有新变化"}
    if status == 0 or "error" in data:
        with db_conn() as conn:
            count = conn.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()["count"]
        return {"ok": False, "cached": count > 0, "count": 0, "message": f"AI HOT 暂时不可用，已保留本地热点（{count} 条）", "error": data.get("error", "request failed")}
    page = data.get("page") or {}
    items = data.get("items") or data.get("data") or data.get("results") or []
    inserted = 0
    with db_conn() as conn:
        for raw in items:
            item = normalize_source(raw, category)
            existed = conn.execute("SELECT 1 FROM source_item WHERE external_id=?", (item["external_id"],)).fetchone() is not None
            conn.execute(
                """INSERT INTO source_item (external_id,title,summary,source_name,source_url,aihot_url,category,published_at,fetched_at,raw_json,is_selected)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET title=excluded.title, summary=excluded.summary,
                source_name=excluded.source_name, source_url=excluded.source_url, aihot_url=excluded.aihot_url,
                category=excluded.category, published_at=excluded.published_at, fetched_at=excluded.fetched_at, raw_json=excluded.raw_json,
                is_selected=excluded.is_selected""",
                (item["external_id"], item["title"], item["summary"], item["source_name"], item["source_url"], item["aihot_url"],
                 item["category"], item["published_at"], now_iso(), json.dumps(item["raw_json"], ensure_ascii=False), item["is_selected"]),
            )
            if not existed:
                inserted += 1
        if response_headers.get("ETag"):
            conn.execute("INSERT INTO setting(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("aihot_etag:" + query, response_headers["ETag"]))
    return {"ok": True, "cached": False, "count": len(items), "inserted": inserted, "has_more": page.get("hasMore", False), "message": f"同步了 {len(items)} 条热点"}


def seed_local_sources() -> None:
    with db_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()["count"]
        if count:
            return
        samples = [
            ("local-ai-workbench", "AI 最有用的时候，不是你不会，是你脑子乱了", "从工具使用转向思考整理，真正的生产力来自把混乱变成可行动的结构。", "本地内容库", "", "", "ai-products"),
            ("local-codex-site", "我用 Codex 做了一个会说话的个人网站", "从一个真实项目回看代理式开发、数据与发布流程。", "本地内容库", "", "", "tip"),
            ("local-ai-company", "AI 开始组建公司了", "当多个角色开始分工、审查和回收错误，AI 产品的重点从回答转向协作。", "本地内容库", "", "", "industry"),
        ]
        for external_id, title, summary, source_name, source_url, aihot_url, category in samples:
            conn.execute("INSERT OR IGNORE INTO source_item(external_id,title,summary,source_name,source_url,aihot_url,category,published_at,fetched_at,raw_json,is_selected) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (external_id, title, summary, source_name, source_url, aihot_url, category, "", now_iso(), "{}", 1))


def seed_assets() -> None:
    with db_conn() as conn:
        if conn.execute("SELECT COUNT(*) AS count FROM asset").fetchone()["count"]:
            return
        paths = []
        for pattern in ("**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif"):
            paths.extend(ROOT.glob(pattern))
        for path in sorted(set(paths))[:80]:
            if ".workbench" in path.parts:
                continue
            rel = str(path.relative_to(ROOT))
            conn.execute("INSERT INTO asset(name,path,kind,source_url,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (path.name, rel, "image", "", "工作区已有素材，发布前人工确认", "", "历史素材", now_iso()))


def row_to_json(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("raw_json", "outline", "evidence", "title_candidates", "claims", "quality_report"):
        if key in result:
            result[key] = safe_json_load(result[key], {} if key == "quality_report" else [])
    for key in ("outline", "evidence", "title_candidates", "claims"):
        if key in result and not isinstance(result[key], list):
            result[key] = [result[key]] if result[key] else []
    return result


def get_topics(limit: int = 50) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute("""SELECT t.*, s.title AS source_title, s.source_url, s.aihot_url, s.source_name
          FROM topic t LEFT JOIN source_item s ON s.id=t.source_id ORDER BY t.updated_at DESC LIMIT ?""", (limit,)).fetchall()
        return [row_to_json(row) for row in rows]


def get_drafts(limit: int = 50) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute("""SELECT d.*, t.title AS topic_title,
          p.media_id AS publish_media_id, p.status AS publish_status, p.message AS publish_message,
          p.updated_at AS publish_updated_at
          FROM draft d LEFT JOIN topic t ON t.id=d.topic_id
          LEFT JOIN publish_job p ON p.id=(SELECT MAX(p2.id) FROM publish_job p2 WHERE p2.draft_id=d.id)
          ORDER BY d.updated_at DESC LIMIT ?""", (limit,)).fetchall()
        return [row_to_json(row) for row in rows]


WECHAT_METRIC_LABELS = {
    "int_page_read_user": "阅读人数",
    "int_page_read_count": "阅读次数",
    "ori_page_read_user": "原文页阅读人数",
    "ori_page_read_count": "原文页阅读次数",
    "share_user": "分享人数",
    "share_count": "分享次数",
    "add_to_fav_user": "收藏人数",
    "add_to_fav_count": "收藏次数",
    "int_page_from_session_read_user": "会话阅读人数",
    "int_page_from_session_read_count": "会话阅读次数",
}


def normalize_article_title(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").strip().lower(), flags=re.UNICODE)


def published_articles() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute("""SELECT p.*, d.title AS draft_title
          FROM published_article p LEFT JOIN draft d ON d.id=p.draft_id
          ORDER BY p.published_at DESC, p.id DESC""").fetchall()
    return [row_to_json(row) for row in rows]


def register_published_article(payload: dict) -> dict:
    draft_id = int(payload.get("draft_id") or 0) or None
    title = str(payload.get("title") or "").strip()
    media_id = ""
    if draft_id:
        with db_conn() as conn:
            draft = conn.execute("SELECT title FROM draft WHERE id=?", (draft_id,)).fetchone()
            latest_publish = conn.execute("SELECT media_id FROM publish_job WHERE draft_id=? AND status='草稿已创建' ORDER BY id DESC LIMIT 1", (draft_id,)).fetchone()
        if not draft:
            raise ValueError("选择的本地文章不存在")
        title = title or str(draft["title"] or "").strip()
        media_id = str(latest_publish["media_id"] if latest_publish else "")
    if not title:
        raise ValueError("请填写已发布文章标题")
    published_at = str(payload.get("published_at") or "").strip()[:10]
    try:
        datetime.fromisoformat(published_at)
    except ValueError as exc:
        raise ValueError("发布日期应为 YYYY-MM-DD") from exc
    if published_at > datetime.now().date().isoformat():
        raise ValueError("发布日期不能晚于今天")
    article_url = str(payload.get("article_url") or "").strip()
    if article_url:
        parsed = urlparse(article_url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("weixin.qq.com"):
            raise ValueError("文章链接应为 https://mp.weixin.qq.com/ 开头的公众号文章地址")
    timestamp = now_iso()
    with db_conn() as conn:
        existing = None
        if article_url:
            existing = conn.execute("SELECT id FROM published_article WHERE article_url=? LIMIT 1", (article_url,)).fetchone()
        if not existing and draft_id:
            existing = conn.execute("SELECT id FROM published_article WHERE draft_id=? AND published_at=? LIMIT 1", (draft_id, published_at)).fetchone()
        if existing:
            article_id = existing["id"]
            conn.execute("""UPDATE published_article
              SET draft_id=?,wechat_media_id=?,title=?,article_url=?,published_at=?,match_status='manual_confirmed',updated_at=?
              WHERE id=?""", (draft_id, media_id, title, article_url, published_at, timestamp, article_id))
        else:
            cursor = conn.execute("""INSERT INTO published_article
              (draft_id,wechat_media_id,title,article_url,published_at,match_status,created_at,updated_at)
              VALUES(?,?,?,?,?,'manual_confirmed',?,?)""", (draft_id, media_id, title, article_url, published_at, timestamp, timestamp))
            article_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM published_article WHERE id=?", (article_id,)).fetchone()
    return {"ok": True, "article": row_to_json(row), "message": "已登记发布文章；同步后会用标题和发布日期确认微信数据"}


def metric_article_key(item: dict, metric_date: str) -> str:
    msgid = str(item.get("msgid") or item.get("msg_id") or "").strip()
    if msgid:
        return "wechat:" + msgid
    title_key = normalize_article_title(item.get("title"))
    stable = title_key or hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"wechat:{metric_date}:{stable}"


def match_published_article(conn: sqlite3.Connection, item: dict, metric_date: str) -> int | None:
    title_key = normalize_article_title(item.get("title"))
    if not title_key:
        return None
    candidates = conn.execute("SELECT * FROM published_article WHERE published_at=?", (metric_date,)).fetchall()
    match = next((row for row in candidates if normalize_article_title(row["title"]) == title_key), None)
    if not match:
        return None
    msgid = str(item.get("msgid") or item.get("msg_id") or "").strip()
    conn.execute("UPDATE published_article SET wechat_msgid=?,match_status='api_matched',updated_at=? WHERE id=?",
                 (msgid, now_iso(), match["id"]))
    return int(match["id"])


def metric_summary(days: int = 7) -> dict:
    days = max(1, min(int(days or 7), 30))
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)
    with db_conn() as conn:
        raw_rows = conn.execute("""SELECT * FROM metric_daily
          WHERE metric_date BETWEEN ? AND ?
          ORDER BY CASE source WHEN 'wechat_api' THEN 0 ELSE 1 END, synced_at DESC, id DESC""",
                                (start_date.isoformat(), end_date.isoformat())).fetchall()
        latest_sync = conn.execute("SELECT * FROM metric_sync_run ORDER BY id DESC LIMIT 1").fetchone()
        legacy_count = conn.execute("SELECT COUNT(*) AS count FROM metric_record").fetchone()["count"]
    selected_rows: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in raw_rows:
        key = (str(row["article_key"]), str(row["metric_date"]), str(row["metric_type"]))
        selected_rows.setdefault(key, row)
    rows = list(selected_rows.values())
    totals = {key: 0.0 for key in WECHAT_METRIC_LABELS}
    daily_map: dict[str, dict] = {}
    article_map: dict[str, dict] = {}
    sources: set[str] = set()
    for row in rows:
        item = row_to_json(row) or {}
        sources.add(str(item.get("source") or "unknown"))
        metric_type = str(item.get("metric_type") or "")
        value = float(item.get("value") or 0)
        if metric_type in totals:
            totals[metric_type] += value
        metric_date = str(item.get("metric_date") or "")
        daily = daily_map.setdefault(metric_date, {"date": metric_date})
        daily[metric_type] = float(daily.get(metric_type, 0)) + value
        raw = item.get("raw_json") if isinstance(item.get("raw_json"), dict) else {}
        article_key = str(item.get("article_key") or "")
        article = article_map.setdefault(article_key, {
            "article_key": article_key,
            "title": str(raw.get("title") or "未返回标题"),
            "metric_date": metric_date,
            "published_article_id": raw.get("_published_article_id"),
        })
        article[metric_type] = float(article.get(metric_type, 0)) + value
    articles = sorted(article_map.values(), key=lambda item: (-float(item.get("int_page_read_count", 0)), item.get("title", "")))
    latest = row_to_json(latest_sync) if latest_sync else None
    has_data = bool(rows)
    if has_data and sources == {"manual_import"}:
        message = "已读取从公众号后台手工登记的真实数据"
    elif has_data:
        message = "已读取公众号 API 返回的真实数据"
    else:
        message = "尚未收到公众号图文统计；新发布文章的数据可能仍在生成"
    return {
        "ok": True,
        "range": {"date_from": start_date.isoformat(), "date_to": end_date.isoformat(), "days": days},
        "has_data": has_data,
        "totals": {key: int(value) if value.is_integer() else value for key, value in totals.items()},
        "daily": list(daily_map.values()),
        "articles": articles,
        "published_articles": published_articles(),
        "latest_sync": latest,
        "legacy_ignored": int(legacy_count),
        "metric_labels": WECHAT_METRIC_LABELS,
        "sources": sorted(sources),
        "message": message,
    }


def save_manual_metrics(payload: dict) -> dict:
    article_id = int(payload.get("published_article_id") or 0)
    if not article_id:
        raise ValueError("请先选择一篇已登记的发布文章")
    with db_conn() as conn:
        article = conn.execute("SELECT * FROM published_article WHERE id=?", (article_id,)).fetchone()
    if not article:
        raise ValueError("已发布文章记录不存在")
    metric_date = str(payload.get("metric_date") or "").strip()[:10]
    try:
        datetime.fromisoformat(metric_date)
    except ValueError as exc:
        raise ValueError("数据日期应为 YYYY-MM-DD") from exc
    if metric_date > datetime.now().date().isoformat():
        raise ValueError("数据日期不能晚于今天")
    values: dict[str, float] = {}
    for metric_type in WECHAT_METRIC_LABELS:
        raw = payload.get(metric_type)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{WECHAT_METRIC_LABELS[metric_type]}必须是数字") from exc
        if value < 0:
            raise ValueError(f"{WECHAT_METRIC_LABELS[metric_type]}不能为负数")
        values[metric_type] = value
    if not values:
        raise ValueError("至少填写一个真实指标")
    article_key = f"published:{article_id}"
    raw_item = {
        "_source": "manual_import",
        "_published_article_id": article_id,
        "title": article["title"],
        "ref_date": metric_date,
        "note": str(payload.get("note") or "公众号后台手工登记")[:300],
    }
    raw_json = json.dumps(raw_item, ensure_ascii=False, sort_keys=True)
    timestamp = now_iso()
    with db_conn() as conn:
        for metric_type, value in values.items():
            conn.execute("""INSERT INTO metric_daily
              (article_key,metric_date,metric_type,value,source,raw_json,synced_at)
              VALUES(?,?,?,?,'manual_import',?,?)
              ON CONFLICT(article_key,metric_date,metric_type,source)
              DO UPDATE SET value=excluded.value,raw_json=excluded.raw_json,synced_at=excluded.synced_at""",
                         (article_key, metric_date, metric_type, value, raw_json, timestamp))
    return {"ok": True, "message": "真实后台数据已登记，并明确标记为手工来源", "summary": metric_summary(7)}


def normalize_length_preset(value: object) -> str:
    candidate = str(value or "standard").strip().lower()
    return candidate if candidate in LENGTH_PRESETS else "standard"


def length_config(value: object) -> dict:
    return LENGTH_PRESETS[normalize_length_preset(value)]


def normalize_markdown_blocks(body: str) -> str:
    """Make paragraph boundaries explicit so preview and WeChat HTML do not stack prose."""
    blocks: list[str] = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        clean = line.strip()
        if not clean:
            continue
        blocks.append(clean)
    return "\n\n".join(blocks)


def trim_body_to_budget(body: str, length_preset: str = "standard") -> str:
    config = length_config(length_preset)
    if len(markdown_text_only(body)) <= config["maximum"]:
        return body
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body or "") if block.strip()]
    if not blocks:
        return body
    kept: list[str] = []
    used = 0
    for block in blocks:
        size = len(markdown_text_only(block))
        if kept and used + size > config["maximum"] - 70:
            break
        kept.append(block)
        used += size
    closing = "我先把问题留在这里，后面用真实使用继续验证。"
    if closing not in kept and used + len(closing) <= config["maximum"]:
        kept.append(closing)
    return "\n\n".join(kept)


def text_model_configured() -> bool:
    return bool(os.getenv("RIGHTCODE_API_KEY") or os.getenv("RIGHT_CODE_API_KEY") or
                os.getenv("YUZAPI_API_KEY") or os.getenv("YUZ_API_KEY") or
                os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))


def active_text_model() -> tuple[str, str]:
    if os.getenv("RIGHTCODE_API_KEY", "") or os.getenv("RIGHT_CODE_API_KEY", ""):
        return "Right Code", os.getenv("RIGHTCODE_TEXT_MODEL", os.getenv("RIGHTCODE_MODEL", "gpt-5.6-sol")).strip() or "gpt-5.6-sol"
    if os.getenv("YUZAPI_API_KEY", "") or os.getenv("YUZ_API_KEY", ""):
        return "YuzAPI", os.getenv("YUZAPI_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol"
    if os.getenv("OPENAI_API_KEY", ""):
        return "OpenAI", os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    if os.getenv("DEEPSEEK_API_KEY", ""):
        return "DeepSeek", os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    return "未配置", ""


TEXT_MODEL_STATE = threading.local()
TRANSIENT_MODEL_MARKERS = ("temporarily unavailable", "service unavailable", "ssl", "bad record mac", "decryption_failed",
                           "timed out", "timeout", "connection reset", "broken pipe", "remote end closed")


def text_model_state() -> dict:
    return getattr(TEXT_MODEL_STATE, "value", {"provider": "", "model": "", "fallback": False, "primary_error": ""})


def set_text_model_state(provider: str, model: str, fallback: bool = False, primary_error: str = "") -> None:
    TEXT_MODEL_STATE.value = {"provider": provider, "model": model, "fallback": bool(fallback), "primary_error": primary_error}


def model_candidates() -> list[dict]:
    candidates: list[dict] = []
    rightcode_key = os.getenv("RIGHTCODE_API_KEY", "") or os.getenv("RIGHT_CODE_API_KEY", "")
    if rightcode_key:
        candidates.append({"provider": "Right Code", "model": os.getenv("RIGHTCODE_TEXT_MODEL", os.getenv("RIGHTCODE_MODEL", "gpt-5.6-sol")).strip() or "gpt-5.6-sol",
                           "base": os.getenv("RIGHTCODE_TEXT_BASE_URL", "https://www.rightapi.ai/codex/v1").rstrip("/"),
                           "key": rightcode_key, "primary": True})
    yuzapi_key = os.getenv("YUZAPI_API_KEY", "") or os.getenv("YUZ_API_KEY", "")
    if yuzapi_key:
        candidates.append({"provider": "YuzAPI", "model": os.getenv("YUZAPI_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol",
                           "base": os.getenv("YUZAPI_BASE_URL", "https://yuzapi.fun/v1").rstrip("/"), "key": yuzapi_key,
                           "primary": True})
    if os.getenv("OPENAI_API_KEY", ""):
        candidates.append({"provider": "OpenAI 兼容", "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
                           "base": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
                           "key": os.getenv("OPENAI_API_KEY", ""), "primary": not bool(yuzapi_key)})
    if os.getenv("DEEPSEEK_API_KEY", ""):
        candidates.append({"provider": "DeepSeek", "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
                           "base": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
                           "key": os.getenv("DEEPSEEK_API_KEY", ""), "primary": not bool(yuzapi_key) and not bool(os.getenv("OPENAI_API_KEY", ""))})
    unique: list[dict] = []
    seen = set()
    for candidate in candidates:
        marker = (candidate["base"], candidate["model"], candidate["key"])
        if marker not in seen:
            unique.append(candidate)
            seen.add(marker)
    return unique


def request_text_candidate(candidate: dict, prompt: str, system: str, retries: int, timeout_seconds: int | None = None) -> tuple[bool, str, str, int]:
    provider = candidate["provider"]
    base = candidate["base"]
    model = candidate["model"]
    if provider == "Right Code":
        # Right Code's chat compatibility layer replaces system instructions;
        # put the editorial rules in the user message so they remain effective.
        user_content = f"内部编辑要求：\n{system}\n\n具体任务：\n{prompt}"
        messages = [{"role": "user", "content": user_content}]
    else:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages, "temperature": 0.7}
    if "deepseek.com" in base or (provider == "YuzAPI" and os.getenv("YUZAPI_JSON_MODE", "").strip().lower() in {"1", "true", "yes", "on"}) or (provider == "Right Code" and os.getenv("RIGHTCODE_JSON_MODE", "").strip().lower() in {"1", "true", "yes", "on"}):
        payload["response_format"] = {"type": "json_object"}
    if provider == "YuzAPI" and os.getenv("YUZAPI_OMIT_TEMPERATURE", "").strip().lower() in {"1", "true", "yes", "on"}:
        payload.pop("temperature", None)
    request_headers = {"Authorization": "Bearer " + candidate["key"], "Connection": "close"}
    if timeout_seconds is None:
        # gpt-5.6-sol can spend longer on a grounded long-form JSON response.
        # Keep the browser request window below 165s while giving the primary
        # provider enough time to finish once; timeout failures are not sent
        # again with the same large prompt.
        default_timeout = "90" if provider == "Right Code" else "45"
        timeout_value = os.getenv("RIGHTCODE_TEXT_TIMEOUT", default_timeout) if provider == "Right Code" else os.getenv("TEXT_MODEL_TIMEOUT", default_timeout)
    else:
        timeout_value = str(timeout_seconds)
    timeout = max(20, min(120, int(timeout_value or 45)))
    status, data, error = 0, {}, "模型请求失败"
    for attempt in range(retries):
        status, data, _ = http_json(base + "/chat/completions", method="POST", payload=payload, headers=request_headers, timeout=timeout)
        if status and data.get("choices"):
            content = data["choices"][0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
            return True, str(content), "", status
        error = data.get("error", {}).get("message", "模型请求失败") if isinstance(data.get("error"), dict) else str(data.get("error", "模型请求失败"))
        retryable = status == 0 or 500 <= status < 600
        # A long model response timing out is not made more likely to finish by
        # sending the same large article prompt again. Keep retries for HTTP
        # 5xx and connection failures, but let the caller try a backup once.
        timed_out = "timed out" in error.lower() or "timeout" in error.lower()
        if attempt + 1 < retries and retryable and not timed_out and (status != 0 or any(marker in error.lower() for marker in TRANSIENT_MODEL_MARKERS)):
            time.sleep(1.2)
            continue
        break
    return False, "", f"{provider} {model} 请求失败：{error}", status


def read_text_model(prompt: str, system: str = "") -> tuple[bool, str, str]:
    candidates = model_candidates()
    set_text_model_state("", "", False, "")
    if not candidates:
        return False, "", "未配置 YUZAPI_API_KEY、OPENAI_API_KEY 或 DEEPSEEK_API_KEY"
    fallback_enabled = os.getenv("YUZAPI_FALLBACK_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    primary = candidates[0]
    ok, content, error, status = request_text_candidate(primary, prompt, system, retries=2)
    if ok:
        set_text_model_state(primary["provider"], primary["model"])
        return True, content, ""
    transient = status == 0 or 500 <= status < 600 or any(marker in error.lower() for marker in TRANSIENT_MODEL_MARKERS)
    if fallback_enabled and transient and len(candidates) > 1:
        # Use one configured backup within the browser's 165s request window.
        for backup in candidates[1:2]:
            fallback_timeout = max(20, min(45, int(os.getenv("TEXT_MODEL_FALLBACK_TIMEOUT", "45") or 45)))
            backup_ok, backup_content, backup_error, _ = request_text_candidate(backup, prompt, system, retries=1, timeout_seconds=fallback_timeout)
            if backup_ok:
                set_text_model_state(backup["provider"], backup["model"], True, error)
                return True, backup_content, ""
            error = f"{error}；备用模型 {backup_error}"
    set_text_model_state(primary["provider"], primary["model"], False, error)
    return False, "", error


def extract_json_object(value: str) -> dict | None:
    """Extract the first valid JSON object, including fenced model responses."""
    decoder = json.JSONDecoder()
    text = str(value or "").strip()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def writing_style_context(provider: str) -> tuple[str, str]:
    """Return enough style evidence without making long requests fragile.

    Right Code is the preferred long-form provider, but its compatibility
    endpoint is more sensitive to oversized prompts. The explicit rules in
    generate_draft carry the non-negotiable constraints, so the external skill
    excerpt and historical samples can be smaller without losing the voice.
    """
    if provider == "Right Code":
        skill_limit, sample_count, sample_limit = 4200, 3, 650
    else:
        skill_limit, sample_count, sample_limit = 7000, 5, 900
    skill_excerpt = str(STYLE_CONTEXT.get("skill_excerpt", ""))[:skill_limit]
    samples = "\n\n".join(
        f"样本 {sample.get('name', '')}\n{sample.get('excerpt', '')[:sample_limit]}"
        for sample in STYLE_CONTEXT.get("samples", [])[:sample_count]
    )
    return skill_excerpt, samples


def local_draft(topic: dict, source: dict | None, length_preset: str = "standard") -> dict:
    title = topic.get("title") or (source or {}).get("title") or "还没想好标题的文章"
    angle = topic.get("core_angle") or "先把这件具体的事讲清楚，再看看它为什么值得我们多想一会儿。"
    observation = topic.get("personal_observation") or "我在整理这类信息时，最容易卡住的不是看不懂，而是很快就被一个漂亮的结论带着走。"
    lived = topic.get("lived_experience") or "我没有把一个并未发生过的现场塞进文章里，而是把自己已经反复做过的动作留下来，先找原文，再拆开事实和判断，最后才决定这件事值不值得写。"
    emotion = topic.get("emotional_note") or "我真正有感觉的地方，往往不是消息有多大，而是它突然碰到了日常里一个很小、但躲不开的问题。"
    source_title = (source or {}).get("title") or "已选热点"
    source_summary = (source or {}).get("summary") or "暂无摘要，发布前需要回到原文核验。"
    body = "\n\n".join([
        "## 先把发生了什么说清楚",
        f"这两天，我一直在看「{source_title}」。真正让我停下来，不是它看起来有多热闹，而是它刚好碰到了一个大家都在经历的问题。先把目前能确认的部分放在这里。{source_summary}",
        "热点最容易让人误判的地方，是它会把很多不同的东西压缩成一个标题。标题看上去像结论，点进去以后才发现，里面其实混着事实、猜测、情绪，还有每个人自己投射进去的期待。",
        "## 真正让我停下来的，不是热闹",
        f"我自己的判断是，{angle}这句话现在还不一定完整，但它至少解释了我为什么愿意花时间把这件事写下来。对我来说，值得写的从来不只是发生了什么，而是它为什么会让一个普通人产生反应。",
        f"我现在能确认的个人观察是，{observation}我会故意慢半拍，先把原文、时间和具体对象记下来，再去看别人怎么解释。",
        f"我没有把一个并未发生过的现场塞进文章里，{lived}这听起来没有那么戏剧化，但至少不会把读者带进一个虚构的故事。",
        "## 把它放回普通人的日常里",
        "很多技术和产品刚出现的时候，讨论都会先围绕能力展开。它能做什么，它比过去快多少，它是不是又把某个行业往前推了一步。但真正决定一件事能不能留下来的，往往是它进入日常以后，普通人会不会因此少做一件麻烦事，或者多承担一种新的麻烦。",
        "如果一个工具只在演示里显得厉害，离开演示就需要很多额外解释，它带来的可能只是短暂的新鲜感。相反，那些真正改变习惯的东西，通常不会一直提醒你自己有多先进，它只是悄悄缩短了一个步骤，或者让一个原本不愿意做的人开始愿意试一次。",
        "我们也可以换一个更实际的角度来观察它。不要先问它能不能替代谁，而是问它有没有让一个人更容易完成原本想做、却总是拖着没做的事。如果答案只是让结果看起来更快，却没有减少判断、返工和确认的成本，那它的价值可能还停留在展示阶段。",
        "真正进入生活的工具，往往会留下非常具体的痕迹。有人开始改变自己的工作顺序，有人把原来需要反复沟通的事情提前说清楚，也有人发现自己不再需要记住那么多零散规则。这些变化不一定会登上热搜，却比一场漂亮的演示更能说明问题。",
        "这也是我觉得这件事值得继续观察的原因。现在还不能急着把它归类成成功或失败，更适合先看它会不会被真实的人留下来，尤其是那些没有时间研究规则、也不想承担太多学习成本的人。",
        "## 现在还不能急着下结论",
        "目前比较明确的是来源材料里写到的事实，其他部分都应该标成判断或推断。事实需要回到原文核验，判断属于作者自己的角度，推断则只是根据现有信息往前走了一步。把这三件事分开，文章反而会更可信。",
        "我不太喜欢把一个刚发生的热点直接写成趋势，也不想为了让文章显得有力量，就替读者把答案提前说完。很多事情的真实影响，需要经过一段时间才会显形。今天看起来很重要的东西，可能只是一个短暂的噪音；今天看起来不起眼的变化，反而可能慢慢改掉我们的习惯。",
        f"说真的，{emotion}这种感觉不会自动变成一个结论，它更像一个小钩子，提醒我不要只写技术名词，也要把这件事放回人的生活里。",
        "## 我更愿意保留的判断",
        f"所以回到最开始的那句话，{angle}我愿意暂时保留这个判断，但不把它包装成最终答案。它更像一个观察的起点，提醒我继续看三件事。第一，谁会最早真正使用它。第二，使用过程中最麻烦的地方在哪里。第三，它有没有让原本不在场的人也获得一点好处。",
        "写到这里，文章其实还没有结束。一个好的选题不是把所有问题都解决，而是让读者离开的时候，手里多了一个可以继续验证的问题。这个问题不需要很宏大，最好和自己的工作、学习或生活直接相关。",
        "对读者来说，最有用的也许不是记住一个新名词，而是知道下一次遇到类似消息时该怎么做。先找到原始来源，再把已经确认的部分和自己的感受分开，最后只对自己真正看见的东西下判断。这个动作看起来慢一点，却能避免被热点牵着走。",
        "写公众号也一样。文章不需要假装自己已经知道所有答案，但需要把为什么这样想交代清楚。只要读者能顺着你的证据和判断走完一遍，即使最后不同意你的结论，也会知道分歧究竟发生在哪里。",
        "## 留一个问题",
        "下一次我们再看到类似热点时，也许可以先别急着转发结论。多问一句，它到底改变了谁的日常，又把什么新的成本交给了谁。等这个问题有了更具体的答案，这篇文章才算真正写完。",
        "以上内容里的来源事实，发布前需要回到原文核验。作者判断和推断已经分开写清楚，读者可以不同意，但至少能看见这份判断是怎么走出来的。"
    ])
    body += "\n\n" + "\n\n".join([
        "我越来越觉得，写这类东西最难的不是把资料找齐，而是知道哪些地方应该停下来。资料可以一直增加，观点也可以一直变多，可读者真正需要的，往往只是一个清楚的判断和一条能自己走回原文的路。",
        "这条路不能靠漂亮的排版替代，也不能靠几个听起来很大的词替代。文章写得再顺，如果每一个判断都没有落脚处，读者读完也只会觉得好像懂了，但说不出自己到底懂了什么。",
        "所以我现在更愿意把事实写得具体一点，把判断写得诚实一点，把推断写得保守一点。这样文章可能没有那么像一份结论，却更像一个人真正走过一段思考之后留下来的东西。",
        "你也可以用这个方法看别的热点。看到一个新名词，先问它具体解决了谁的问题，再问这个解决方案把什么成本藏在了后面。很多时候，第二个问题比第一个更接近真实生活。",
        "如果答案暂时还不清楚，也没有关系。保留一个没有急着盖章的问题，本身就是一种判断。至少，它比为了让文章结束而强行升华，要诚实得多。",
        "回到最开始，我愿意继续观察这件事，不是因为我已经看懂了全部，而是因为它让我看见了一个还没有被说透的变化。这个变化最后会不会成立，需要时间，也需要更多具体的人真的用起来。",
        "大概这就是我愿意把它写下来的原因。不是为了替读者宣布答案，而是把我已经看见的部分摆在桌上，剩下的那一步，我们各自去验证。",
        "还有一个很容易被忽略的地方，真正的使用者通常没有那么多时间去研究一套复杂规则。他们只是遇到了一个麻烦，想看看有没有更省力的办法。产品如果只能对熟悉术语的人友好，就还没有真正走进日常。",
        "我也不喜欢把普通人写成一个抽象的读者群。屏幕前可能是一个刚下班的人，也可能是一个正在学新东西的人，他们没有义务理解所有技术细节，却完全有资格知道这件事会不会影响自己。",
        "从这个角度看，很多发布会里最重要的不是参数，而是它有没有让一个原本不敢尝试的人少一点门槛。门槛降下来以后，才会有更多真实反馈回来，产品也才知道自己到底哪里有用，哪里只是看起来厉害。",
        "这件事如果最后真的成立，改变可能不会以一条热搜的形式出现。它更可能变成某个软件里一个不起眼的按钮，或者一次原本需要反复沟通、后来只用几句话就能完成的工作。",
        "所以我不急着把它写成行业拐点，也不急着把它说成普通人的机会。先观察它有没有穿过演示和宣传，落到那些不关心技术名词、只关心事情能不能顺利做完的人手里。",
        "如果你正在考虑要不要试一下，也不用立刻做一个很大的决定。先拿一个真实的小任务跑一遍，看看它到底替你省了哪一步，又增加了哪一步。很多判断，只有放进自己的工作里才会变得准确。",
        "这也是我写这类文章时会反复提醒自己的地方，别把别人演示出来的结果直接当成自己的经验。你可以借鉴方法，但还得回到自己的场景里跑一次，哪怕结果没有那么漂亮，也比想象更有价值。",
        "真的有这么简单吗？没有。工具只是把一部分动作变快了，选择、判断和确认仍然要由人自己负责，这也是我不愿意把任何新产品直接写成万能答案的原因。",
        "如果把一件事拆开看，最先发生变化的通常不是结果，而是过程。原来需要来回找资料、确认格式、重复修改的工作，可能会少掉其中一两步。别小看这两步，很多人就是在这里被消耗掉的。",
        "但过程变短以后，人的责任不会自动变少。你还是要知道自己为什么选这个方向，也要看一眼结果有没有跑偏。越是看起来省事的地方，越不能完全闭着眼睛交出去。",
        "我自己更愿意把这种变化叫作还给人一点注意力，而不是替人完成一切。注意力回来以后，你才有机会想更重要的问题，事情到底该不该做，做成什么样，谁会真正从里面得到好处。",
        "这也解释了为什么同一个工具，有人用起来觉得很爽，有人用两次就放弃。差别不一定在工具本身，而在于它有没有接上这个人的真实工作顺序。没有接上，再强的能力也只是一段演示。",
        "所以看任何新产品，我都会留意一个很小的信号，用完以后我是不是愿意下次还打开它。如果答案是愿意，哪怕它现在还有不少毛病，也值得继续观察。如果答案是不愿意，参数再漂亮也很难进入生活。",
        "这件事最后会走到哪里，还没有答案。可没有答案不等于不能开始观察，先把发生过的部分记下来，把自己的感受说清楚，再等时间把剩下的部分慢慢筛出来。",
        "有时候，文章写到最后，最值得留下的不是一句特别响亮的话，而是一个更准确的问题。这个问题能够带着你去看下一次更新，也能够提醒你别被第一次兴奋的感觉牵着走。",
        "我也不敢保证今天的判断一定正确。内容创作者最容易犯的错，就是写得越顺，越觉得自己已经把事情看透了。可现实往往会在下一次使用、下一条数据或者一个普通人的反馈里，把结论重新推一遍。",
        "所以这篇文章更像一次阶段性的记录。它把目前能确认的材料、已经发生的观察和我暂时形成的判断放在一起，后面如果事实变了，文章也应该跟着变，而不是为了维护一个漂亮的结论硬撑下去。",
        "这份好奇心暂时就留在这里。等更多真实使用发生以后，再回来看看今天的判断有没有被现实推翻，也许比现在急着给出一个漂亮结论更有意思。至少现在，我愿意先把问题留在桌面上。"
    ])
    body = re.sub(r"^##\s+", "", body, flags=re.M)
    body = normalize_markdown_blocks(body)
    body = trim_body_to_budget(body, length_preset)
    outline = ["具体事件与核心判断", "从能力回到使用成本", "事实、判断与推断", "普通人的真实问题", "留下一个可验证的问题"]
    titles = [title, f"{title}，我更在意它背后的那件事", f"看到这个热点后，我想先聊聊普通人的感受"]
    source_url = (source or {}).get("source_url") or (source or {}).get("aihot_url") or ""
    return {"title": titles[0], "title_candidates": titles, "digest": angle[:120], "body": body, "outline": outline,
            "evidence": [{"type": "source", "label": source_title, "url": source_url, "note": source_summary}],
            "claims": [{"kind": "fact", "text": source_summary, "source": source_url},
                       {"kind": "judgement", "text": angle, "source": "作者核心判断"},
                       {"kind": "inference", "text": "这件事可能会改变普通人的日常工作方式，发布前需人工核验。", "source": "作者推断"}],
            "mode": "local_template", "length_preset": normalize_length_preset(length_preset)}


def split_readability_paragraph(block: str, target: int = 92, hard_limit: int = 118) -> list[str]:
    """Split a dense paragraph only at sentence boundaries.

    The characters are left untouched; only blank-line boundaries are added.
    This gives WeChat's narrow reading column some air without turning every
    sentence into a separate social-media caption.
    """
    plain = markdown_text_only(block)
    if (len(plain) <= hard_limit or "http://" in block or "https://" in block
            or block.startswith(("!", "#", ">", "- ", "* ")) or re.match(r"^\d+[.)]\s+", block)):
        return [block]
    sentences = [item for item in re.split(r"(?<=[。！？!?；;])", block) if item.strip()]
    if len(sentences) < 2:
        return [block]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        candidate = current + sentence
        if current and len(markdown_text_only(candidate)) > target:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > 1 and len(markdown_text_only(chunks[-1])) < 24:
        combined = chunks[-2] + chunks[-1]
        if len(markdown_text_only(combined)) <= hard_limit:
            chunks[-2:] = [combined]
    if any(chunk.count(marker) % 2 for chunk in chunks for marker in ("**", "==", "__", "^^", "`")):
        return [block]
    return chunks if len(chunks) > 1 else [block]


def local_readability_markup(markdown: str) -> str:
    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n", markdown or "") if block.strip()]
    if not raw_blocks:
        return markdown

    # Rebuild the editorial layer on every pass. This avoids accumulating old
    # headings or emphasis when the user clicks “整理重点” more than once.
    blocks: list[str] = []
    heading_hints: dict[int, int] = {}
    for raw in raw_blocks:
        block = raw.strip()
        if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)", block) or block == "---":
            blocks.append(block)
            continue
        heading_match = re.match(r"^(#{2,3})\s+", block)
        heading_level = 0
        if heading_match:
            heading_level = len(heading_match.group(1))
            block = block[heading_match.end():].strip()
        was_callout = bool(re.match(r"^>\s*", block))
        block = re.sub(r"^#\s+", "", block)
        block = re.sub(r"^>\s*(?:\[(?:引言|方法|技巧|风险|金句)\]\s*)?", "", block)
        block = re.sub(r"\*\*([^*]+)\*\*", r"\1", block)
        block = re.sub(r"==([^=]+)==", r"\1", block)
        block = re.sub(r"__([^_]+)__", r"\1", block)
        block = re.sub(r"\^\^([^\^]+)\^\^", r"\1", block)
        pieces = [block.strip()] if was_callout or heading_level else split_readability_paragraph(block.strip())
        if heading_level:
            heading_hints[len(blocks)] = heading_level
        blocks.extend(pieces)

    def is_special(index: int) -> bool:
        block = blocks[index]
        return bool(not block or block == "---" or block.startswith(("!", "- ", "* "))
                    or re.match(r"^\d+[.)]\s+", block))

    text_indices = [index for index in range(len(blocks)) if not is_special(index)]
    if not text_indices:
        return markdown
    lead_index = text_indices[0]
    lengths = [len(markdown_text_only(block)) for block in blocks]
    total_length = max(1, sum(lengths))
    positions: list[int] = []
    running = 0
    for length in lengths:
        positions.append(running)
        running += length

    # Candidate headings reuse a short standalone paragraph or the first
    # sentence of a longer paragraph. They are selected around article thirds,
    # instead of greedily turning the first four short sentences into headings.
    heading_candidates: list[dict] = []
    for index in text_indices:
        if index == lead_index:
            continue
        block = blocks[index]
        plain = markdown_text_only(block)
        if "http://" in block or "https://" in block:
            continue
        sentences = [item.strip() for item in re.split(r"(?<=[。！？!?])", block) if item.strip()]
        heading = ""
        remainder = ""
        if 9 <= len(plain) <= 58:
            heading = block
        elif len(sentences) >= 2 and 10 <= len(markdown_text_only(sentences[0])) <= 42:
            heading = sentences[0]
            remainder = block[len(sentences[0]):].strip()
        if not heading:
            continue
        signal = 1
        if re.search(r"(?:真正|关键|问题|不是|而是|为什么|普通人|更重要|变化|难的是|值得)", heading):
            signal += 2
        if index in heading_hints:
            signal += 1
        heading_candidates.append({"index": index, "heading": heading, "remainder": remainder,
                                   "position": positions[index], "signal": signal})

    if total_length < 1500:
        h2_targets = [0.18, 0.62]
    elif total_length < 3200:
        h2_targets = [0.12, 0.43, 0.74]
    else:
        h2_targets = [0.10, 0.34, 0.60, 0.83]
    h2_choices: list[dict] = []
    used_heading_indices: set[int] = set()
    minimum_gap = max(180, int(total_length * 0.16))
    for fraction in h2_targets:
        target = total_length * fraction
        available = [candidate for candidate in heading_candidates
                     if candidate["index"] not in used_heading_indices
                     and all(abs(candidate["position"] - selected["position"]) >= minimum_gap for selected in h2_choices)]
        if not available:
            continue
        selected = min(available, key=lambda candidate: abs(candidate["position"] - target) - candidate["signal"] * 24)
        h2_choices.append(selected)
        used_heading_indices.add(selected["index"])

    # One or two quieter subheads create hierarchy inside the main sections.
    h3_choices: list[dict] = []
    for fraction in ([0.28, 0.58] if total_length >= 1800 else [0.46]):
        target = total_length * fraction
        available = [candidate for candidate in heading_candidates
                     if candidate["index"] not in used_heading_indices
                     and all(abs(candidate["position"] - selected["position"]) >= max(120, minimum_gap // 2)
                             for selected in h2_choices + h3_choices)]
        if not available:
            continue
        selected = min(available, key=lambda candidate: abs(candidate["position"] - target) - candidate["signal"] * 16)
        h3_choices.append(selected)
        used_heading_indices.add(selected["index"])

    h2_map = {choice["index"]: choice for choice in h2_choices}
    h3_map = {choice["index"]: choice for choice in h3_choices}

    # Long articles need more than an opening frame. Pick two or three existing
    # paragraphs as distributed visual anchors, preferring real methods, risks
    # and concise judgments. No copy is invented for decoration.
    if total_length < 1500:
        callout_targets = [0.58]
    elif total_length < 2800:
        callout_targets = [0.34, 0.69]
    else:
        callout_targets = [0.27, 0.55, 0.80]
    callout_candidates: list[dict] = []
    for index in text_indices:
        if index == lead_index or index in used_heading_indices or positions[index] < total_length * 0.16:
            continue
        plain = markdown_text_only(blocks[index])
        if not 14 <= len(plain) <= 145 or "http" in plain:
            continue
        label, signal = "", 0
        if re.search(r"(?:不要|(?<!能)不能|别急|风险|小心|边界|还不能|需要注意)", plain):
            label, signal = "风险", 5
        elif re.search(r"(?:最稳妥|可以先|具体做法|建议|先.{1,18}再|最简单的办法|只需要)", plain):
            label, signal = "方法", 5
        elif len(plain) <= 82 and re.search(r"(?:真正|关键|问题不在|不是.{1,30}而是|我更在意|更重要|难的是|才是|值得)", plain):
            label, signal = "金句", 4
        elif len(plain) <= 58:
            label, signal = "金句", 1
        if label:
            callout_candidates.append({"index": index, "label": label, "position": positions[index], "signal": signal})

    callout_choices: list[dict] = []
    used_callout_indices: set[int] = set()
    callout_gap = max(170, int(total_length * .16))
    heading_positions_selected = [choice["position"] for choice in h2_choices + h3_choices]
    for fraction in callout_targets:
        target = total_length * fraction
        available = [candidate for candidate in callout_candidates
                     if candidate["index"] not in used_callout_indices
                     and all(abs(candidate["position"] - selected["position"]) >= callout_gap for selected in callout_choices)
                     and all(abs(candidate["position"] - heading_position) >= 80 for heading_position in heading_positions_selected)]
        if not available:
            available = [candidate for candidate in callout_candidates
                         if candidate["index"] not in used_callout_indices
                         and all(abs(candidate["position"] - selected["position"]) >= callout_gap for selected in callout_choices)]
        if not available:
            continue
        selected = min(available, key=lambda candidate: abs(candidate["position"] - target) - candidate["signal"] * 38)
        callout_choices.append(selected)
        used_callout_indices.add(selected["index"])
    callout_map = {choice["index"]: choice["label"] for choice in callout_choices}

    reserved = {lead_index, *used_heading_indices}
    reserved.update(used_callout_indices)

    # Spread emphasis over the whole article. Method, risk and judgment use
    # different markers so bold/highlight/underline/color have real meaning.
    emphasis_candidates: list[dict] = []
    for index in text_indices:
        if index in reserved:
            continue
        for sentence in [item.strip() for item in re.split(r"(?<=[。！？!?])", blocks[index]) if item.strip()]:
            plain = markdown_text_only(sentence)
            if not 10 <= len(plain) <= 58 or "http" in sentence:
                continue
            kind, score = "judgement", 1
            if re.search(r"(?:最稳妥|可以先|具体做法|建议|只需要|先.{1,18}再|最简单)", sentence):
                kind, score = "method", 5
            elif re.search(r"(?:不要|(?<!能)不能|别急|风险|小心|边界|还不能|需要注意)", sentence):
                kind, score = "risk", 5
            elif re.search(r"(?:真正|关键|问题不在|不是.{1,30}而是|我更在意|更重要|决定.{1,20}不是)", sentence):
                kind, score = "judgement", 4
            elif re.search(r"(?:但|却|普通人|难的是|值得)", sentence):
                score = 2
            emphasis_candidates.append({"index": index, "sentence": sentence, "position": positions[index],
                                        "kind": kind, "score": score})

    emphasis_budget = max(4, min(7, total_length // 500 + 2))
    emphasis_targets = [(slot + 1) / (emphasis_budget + 1) for slot in range(emphasis_budget)]
    selected_emphasis: list[dict] = []
    used_emphasis_blocks: set[int] = set()
    for fraction in emphasis_targets:
        target = total_length * fraction
        available = [candidate for candidate in emphasis_candidates if candidate["index"] not in used_emphasis_blocks]
        if not available:
            break
        selected = min(available, key=lambda candidate: abs(candidate["position"] - target) - candidate["score"] * 34)
        selected_emphasis.append(selected)
        used_emphasis_blocks.add(selected["index"])

    judgement_index = 0
    for selected in selected_emphasis:
        marker = "__" if selected["kind"] == "method" else "^^" if selected["kind"] == "risk" else ("**" if judgement_index % 2 == 0 else "==")
        if selected["kind"] == "judgement":
            judgement_index += 1
        sentence = selected["sentence"]
        blocks[selected["index"]] = blocks[selected["index"]].replace(sentence, f"{marker}{sentence}{marker}", 1)

    structured: list[str] = []
    for index, block in enumerate(blocks):
        if index == lead_index:
            structured.append(f"> [引言] {block}")
            continue
        choice = h2_map.get(index) or h3_map.get(index)
        if choice:
            prefix = "##" if index in h2_map else "###"
            structured.append(f"{prefix} {choice['heading']}")
            if choice["remainder"]:
                structured.append(choice["remainder"])
            continue
        if index in callout_map:
            structured.append(f"> [{callout_map[index]}] {block}")
            continue
        structured.append(block)
    rendered = "\n\n".join(structured)
    return rendered if layout_plain_text(rendered) == layout_plain_text(markdown) else markdown


def grounded_personal_passage(topic: dict) -> str:
    observation = str(topic.get("personal_observation") or "").strip()
    emotion = str(topic.get("emotional_note") or "").strip()
    if observation:
        return f"我现在能确认的观察是，{observation}这不是一个虚构的现场，而是我在整理这件事时真正反复想到的地方。"
    if emotion:
        return f"我自己的感觉是，{emotion}这份感觉还不足以替代事实，但足够提醒我继续把问题看具体。"
    return "我没有把一个并未发生过的现场塞进文章里。现在能确认的是，我在整理这类材料时会先回到原文，再把事实、判断和推断分开，最后才决定这件事值不值得继续写。"


def normalize_body_placeholders(body: str, topic: dict) -> str:
    if not body:
        return body
    fallback = grounded_personal_passage(topic)
    patterns = [
        r"[^\n。！？]*待补[^\n。！？]*[。！？]?",
        r"[^\n。！？]*这里应该放[^\n。！？]*[。！？]?",
        r"[^\n。！？]*请作者补[^\n。！？]*[。！？]?",
        r"[^\n。！？]*等作者[^\n。！？]*[。！？]?",
    ]
    normalized = body
    for pattern in patterns:
        normalized = re.sub(pattern, fallback, normalized)
    # Keep h2/h3 markers for the later layout pass. A leading h1 would repeat
    # the separate WeChat article title, so only flatten that level here.
    normalized = re.sub(r"^#\s+", "", normalized, flags=re.M)
    return normalized.replace("发布前请回到原文核验事实，并把真实经历补回文章。", "发布前请回到原文核验事实，作者判断和推断已经分开写清楚。")


def layout_plain_text(value: str) -> str:
    """Compare layout output by content, ignoring Markdown control syntax."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value or "")
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*>\s*", "", text)
    text = re.sub(r"(?m)^\s*(?:\[[^\]]+\]|引言|方法|技巧|风险|金句)\s*(?:[|｜:：]\s*)?", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*]|\d+[.)])\s+", "", text)
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", r"\1", text)
    text = re.sub(r"[*_`=~^]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Promoting the first sentence of a paragraph into a heading introduces a
    # block boundary after Chinese punctuation. Treat that whitespace as
    # layout-only so the no-rewrite guard still verifies the actual wording.
    return re.sub(r"(?<=[。！？!?])\s+(?=[\u3400-\u9fffA-Za-z0-9])", "", text)


def readability_markup(markdown: str, title: str = "") -> dict:
    plain_length = len(markdown_text_only(markdown))
    mark_budget = max(4, min(10, plain_length // 400 + 1))
    support_box_count = 1 if plain_length < 1500 else 2 if plain_length < 2800 else 3
    prompt = f"""你是公众号排版总编，负责把一篇已经写完的 Markdown 正文校准成可读版式。
只允许给原文已有的句子增加 Markdown 结构标记，不得改写、删减、补造、拆散或调换任何文字。
允许的块级标记只有：
- `## 原文已有的一整句`，表示一级正文小节标题
- `### 原文已有的一整句`，表示二级正文小节标题
- `> [引言] 原文已有的一整句`，表示开头引言
- `> [方法] 原文已有的一整句` 或 `> [技巧] 原文已有的一整句`，表示可执行方法
- `> [风险] 原文已有的一整句`，表示风险提醒
- `> [金句] 原文已有的一整句`，表示值得停顿的判断
控制词 `[引言]`、`[方法]`、`[技巧]`、`[风险]`、`[金句]` 不属于正文，不要改变后面的原句。
允许的行内标记只有：**重点加粗**、==重点高亮==、__重点下划线__、^^朱砂强调色^^。
全文重点标记总数控制在 {max(4, min(10, mark_budget))} 处，不要整段标记。方法、技巧优先下划线，金句优先加粗或高亮，风险优先朱砂强调。
正文超过 1200 字时使用 2 到 4 个 `##`，并分布在前段、中段和后段；`###` 只用于一级小节内部，最多 2 个，不要把前几段连续做成标题。不要输出 `#` 一级标题，文章标题由公众号标题栏负责。
第一段使用一个 `[引言]`，全文再选 {support_box_count} 个方法、风险或金句框，分散放在长段文字之间作为阅读锚点。优先保留语义差异，不要连续使用装饰块，不要把每个段落都做成框。
普通正文每段控制在 1 到 3 句、约 45 到 100 个中文字符，最长不超过 120 字。只允许在原文句号、问号、感叹号或分号后增加空行，不得删字、换字或把每句话都拆成独立段落。
输出 JSON，只有一个字段 body，body 必须是完整正文。

文章标题：{title}
正文：
{markdown}
"""
    ok, text, error = read_text_model(prompt, "你是一名克制的公众号排版总编，只做结构和重点标记，不改变作者原文。")
    if ok:
        result = extract_json_object(text)
        if result:
            body = str(result.get("body", "")).strip()
            mark_count = len(re.findall(r"\*\*[^*]+\*\*|==[^=]+==|__[^_]+__|\^\^[^\^]+\^\^", body))
            minimum_marks = max(4, min(6, plain_length // 700 + 2))
            h1_count = len(re.findall(r"(?m)^#\s+", body))
            h2_count = len(re.findall(r"(?m)^##\s+", body))
            h3_count = len(re.findall(r"(?m)^###\s+", body))
            control_count = len(re.findall(r"(?m)^>\s+\[(?:引言|方法|技巧|风险|金句)\]", body))
            normal_blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()
                             and not block.lstrip().startswith(("!", "#", ">", "- ", "* "))
                             and not re.match(r"^\d+[.)]\s+", block.lstrip())]
            dense_paragraphs = [block for block in normal_blocks if len(markdown_text_only(block)) > 120]
            heading_positions = [match.start() for match in re.finditer(r"(?m)^#{2,3}\s+", body)]
            heading_spread = (len(heading_positions) >= 2 and heading_positions[-1] - heading_positions[0] >= len(body) * .38)
            minimum_h2 = 2 if plain_length >= 1200 else 1
            expected_callouts = 1 + support_box_count
            if (body and layout_plain_text(body) == layout_plain_text(markdown)
                    and h1_count == 0 and minimum_h2 <= h2_count <= 4 and h3_count <= 2
                    and control_count == expected_callouts and heading_spread
                    and not dense_paragraphs
                    and minimum_marks <= mark_count <= mark_budget):
                layout_model = text_model_state()
                model_label = " ".join(part for part in (layout_model.get("provider", ""), layout_model.get("model", "")) if part).strip() or "文本模型"
                return {"body": body, "mode": "model", "message": f"{model_label} 已校准章节框、语义框与重点层级，正文内容未改动",
                        "layout": {"h2": h2_count, "h3": h3_count, "callouts": control_count, "emphasis": mark_count}}
    return {"body": local_readability_markup(markdown), "mode": "local_fallback", "message": error or "模型排版未通过校验，已用本地规则补齐章节框、语义框与重点层级"}


def generate_draft(draft_id: int, length_preset: str | None = None) -> dict:
    with db_conn() as conn:
        draft_row = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if not draft_row:
            raise ValueError("草稿不存在")
        topic_row = conn.execute("SELECT * FROM topic WHERE id=?", (draft_row["topic_id"],)).fetchone() if draft_row["topic_id"] else None
        source_row = conn.execute("SELECT * FROM source_item WHERE id=?", (topic_row["source_id"],)).fetchone() if topic_row and topic_row["source_id"] else None
    topic = row_to_json(topic_row) or {}
    source = row_to_json(source_row)
    length_preset = normalize_length_preset(length_preset or draft_row["length_preset"] or "standard")
    length = length_config(length_preset)
    prompt_provider, _ = active_text_model()
    skill_excerpt, style_samples = writing_style_context(prompt_provider)
    prompt = f"""你是数字生命卡兹克的公众号写作协作者。请基于以下资料生成一篇可以直接进入审稿的中文长文。
严格遵守 Khazix 写作规则，文章要像一个有见识的普通人在认真聊一件打动他的事，不像报告或营销稿。正文目标为 {length['minimum']} 到 {length['maximum']} 个中文字符，至少 10 个自然段。开头从具体事件或当下场景切入，不使用教科书式开场。
必须遵守 3s 原则，标题和开头首屏的前 3 到 5 句，要让读者在约 3 秒内知道发生了什么、矛盾或看点是什么、继续读能得到什么判断或具体帮助。不要用背景铺垫、宏大定义或空泛感受消耗首屏，第一段必须出现具体对象、动作、变化、数字、时间或可验证事实中的至少一种。
不要编造作者没有提供或历史样本中没有出现的具体经历、数字、引语、人物、时间和测试结果。缺失第一手经历时，不要留下待补、这里应该放、等作者补充等占位符；改用已有作者观察、历史文章中已经发生的工具使用和当前工作流形成谨慎的个人判断，不把推测写成亲历事实。文章必须完整结束。
除非文章本身是方法论分条，不使用 Markdown 二级标题，不用项目符号堆成提纲。靠口语化转场、短段落、疑问句和少量断裂句推进。不要使用首先、其次、最后、综上所述、值得注意的是、说白了、意味着什么、本质上、换句话说、不可否认等套话，不使用冒号、破折号和双引号。
知识要自然地聊出来，每个核心观点都要有具体事实、场景、工具名称、数据或来源支撑，并加入对读者处境的理解、一个自然的文化或历史参照，以及结尾回扣。正文先输出普通 Markdown，不要添加加粗、高亮或下划线，重点标记由编辑部后处理。
输出 JSON，字段为 title_candidates（3个标题）、title、digest、outline（数组）、body、evidence（数组）、claims（数组）。claims 中明确区分 kind= fact / judgement / inference，并为 fact 填写 source。

选题：{json.dumps(topic, ensure_ascii=False)}
热点资料：{json.dumps(source or {}, ensure_ascii=False)}
作者观察：{topic.get('personal_observation','')}
真实经历：{topic.get('lived_experience','')}
情绪节点：{topic.get('emotional_note','')}
写作规范摘要：{skill_excerpt}
历史文章样本摘要：{style_samples}
作者个人补充偏好：{style_preferences()}
"""
    ok, text, error = read_text_model(prompt, "你是一名编辑部里的写作协作者，不是自动发稿机器人。")
    writing_model = dict(text_model_state())
    result = None
    if ok:
        result = extract_json_object(text)
    if not result:
        if text_model_configured():
            failure = error or "模型返回不是有效 JSON，未写入本地模板"
            raise RuntimeError(f"{active_text_model()[0]} {active_text_model()[1]} 写作失败：{failure}")
        result = local_draft(topic, source, length_preset)
        result["model_note"] = error or "已使用本地协作模板"
    result["body"] = normalize_markdown_blocks(normalize_body_placeholders(str(result.get("body", "")), topic))
    result["body"] = trim_body_to_budget(result["body"], length_preset)
    layout = readability_markup(result["body"], str(result.get("title", "")))
    result["body"] = layout["body"]
    result["readability_mode"] = layout.get("mode", "local_fallback")
    result["digest"] = markdown_text_only(str(result.get("digest", "")))[:120] or markdown_text_only(result["body"])[:120]
    result["model_provider"] = writing_model.get("provider", "")
    result["model_name"] = writing_model.get("model", "")
    result["model_fallback"] = bool(writing_model.get("fallback"))
    if result["model_fallback"]:
        result["model_note"] = f"首选模型暂时不可用，已使用备用模型 {writing_model.get('provider')} {writing_model.get('model')}"
    elif result["model_provider"]:
        result["model_note"] = f"已使用 {result['model_provider']} {result['model_name']} 生成"
    candidates = result.get("title_candidates")
    if isinstance(candidates, str):
        candidates = [candidates]
    result["title_candidates"] = candidates if isinstance(candidates, list) and candidates else [result.get("title", "")]
    if not isinstance(result.get("claims"), list) or not result.get("claims"):
        result["claims"] = local_draft(topic, source, length_preset).get("claims", [])
    result["length_preset"] = length_preset
    result["quality_report"] = quality_check(result.get("body", ""), result.get("evidence", []) if isinstance(result.get("evidence"), list) else [], length_preset)
    with db_conn() as conn:
        conn.execute("UPDATE draft SET title=?,digest=?,body=?,outline=?,evidence=?,title_candidates=?,claims=?,length_preset=?,quality_report=?,style_profile_id=?,model_provider=?,model_name=?,model_fallback=?,model_note=?,status=?,updated_at=? WHERE id=?",
                     (result.get("title", ""), result.get("digest", ""), result.get("body", ""), json.dumps(result.get("outline", []), ensure_ascii=False),
                      json.dumps(result.get("evidence", []), ensure_ascii=False), json.dumps(result.get("title_candidates", []), ensure_ascii=False),
                      json.dumps(result.get("claims", []), ensure_ascii=False), length_preset, json.dumps(result["quality_report"], ensure_ascii=False), current_style_profile_id(),
                      result.get("model_provider", ""), result.get("model_name", ""), 1 if result.get("model_fallback") else 0, result.get("model_note", ""), "待审稿", now_iso(), draft_id))
        row = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
    result["draft"] = row_to_json(row)
    return result


FORBIDDEN_WORDS = ["说白了", "意味着什么", "这意味着", "本质上", "换句话说", "不可否认", "综上所述", "总的来说", "值得注意的是", "不难发现", "让我们来看看", "接下来让我们"]


def three_second_content_check(body: str) -> dict:
    """Check whether the first screen gives a reader a reason to continue."""
    paragraphs = [markdown_text_only(p) for p in re.split(r"\n\s*\n", body or "") if markdown_text_only(p)]
    opening = " ".join(paragraphs[:2])[:420]
    concrete = bool(re.search(r"\d|https?://|「[^」]+」|最近|这两天|今天|昨天|刚刚|刚才|发布|上线|宣布|刷到|看到|测试|用过", opening))
    tension = bool(re.search(r"为什么|但|却|还不能|问题|卡住|停下来|冲突|区别|真正|到底|[？?]", opening))
    promise = bool(re.search(r"我想|先说|聊聊|看看|判断|方法|具体|你会|读完|值得|关键|答案|怎么", opening))
    generic_start = bool(re.match(r"^(在当今|随着|近年来|伴随着|众所周知|在这个时代)", opening))
    passed = bool(opening) and len(opening) <= 420 and concrete and tension and promise and not generic_start
    reasons = []
    if not opening:
        reasons.append("缺少正文首屏")
    if not concrete:
        reasons.append("首屏没有具体事件、对象或可验证事实")
    if not tension:
        reasons.append("首屏没有冲突、疑问或明确看点")
    if not promise:
        reasons.append("首屏没有告诉读者继续阅读能得到什么")
    if generic_start:
        reasons.append("首屏从宏大背景或套话开始")
    return {"passed": passed, "opening_chars": len(opening), "reasons": reasons,
            "rule": "3 秒内看懂发生了什么、为什么值得继续看、能得到什么"}


def quality_check(body: str, evidence: list | None = None, length_preset: str = "standard") -> dict:
    evidence = evidence or []
    length_preset = normalize_length_preset(length_preset)
    length = length_config(length_preset)
    plain = markdown_text_only(body or "")
    hits = {word: body.count(word) for word in FORBIDDEN_WORDS if word in body}
    punctuation = {mark: body.count(mark) for mark in ["：", "——", '"', "“", "”"] if mark in body}
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    ordinary_paragraphs = [paragraph for paragraph in paragraphs if not paragraph.lstrip().startswith(("!", "#", ">", "- ", "* "))
                           and not re.match(r"^\d+[.)]\s+", paragraph.lstrip())]
    ordinary_lengths = [len(markdown_text_only(paragraph)) for paragraph in ordinary_paragraphs]
    dense_paragraphs = [length for length in ordinary_lengths if length > 120]
    spoken_candidates = ["我觉得", "我自己", "说真的", "其实吧", "你想想看", "我发现", "我更在意", "我还在摸索", "太离谱了", "？？？", "= =", "。。。"]
    spoken = [x for x in spoken_candidates if x in body]
    placeholders = [x for x in ["待补", "这里应该放", "等作者补", "请作者补", "暂无摘要", "还没有把自己的观察写进去", "发布前再补"] if x in body]
    has_personal = bool(re.search(r"我(觉得|自己|发现|更在意|会|一直|始终|不太|在整理|愿意|现在)", body))
    has_specific_detail = bool(re.search(r"\d|https?://|「[^」]+」|当时|昨天|今天|这两天|具体|原文", body))
    question_count = len(re.findall(r"[？?]", body))
    short_breaks = sum(1 for paragraph in paragraphs if len(markdown_text_only(paragraph)) <= 18)
    heading_count = len(re.findall(r"^#{1,6}\s+", body, re.M))
    h2_count = len(re.findall(r"^##\s+", body, re.M))
    h3_count = len(re.findall(r"^###\s+", body, re.M))
    list_count = len(re.findall(r"^(?:[-*]\s+|\d+[.)]\s+)", body, re.M))
    emphasis_count = len(re.findall(r"\*\*[^*]+\*\*|==[^=]+==|__[^_]+__|\^\^[^\^]+\^\^", body))
    emphasis_budget = max(4, min(10, len(plain) // 400 + 1))
    emphasis_minimum = max(3, min(6, len(plain) // 700 + 2)) if plain else 0
    paragraph_keys = [re.sub(r"\W+", "", markdown_text_only(paragraph))[:90] for paragraph in paragraphs]
    repeated_paragraphs = len(paragraph_keys) - len(set(paragraph_keys))
    empty_expression_hits = sum(body.count(term) for term in ["很重要", "非常关键", "值得关注", "有很多可能", "带来新的机遇"])
    three_second = three_second_content_check(body)
    length_ok = length["minimum"] <= len(plain) <= length["maximum"]
    density_ok = len(paragraphs) >= 10 and repeated_paragraphs <= max(1, len(paragraphs) // 8) and empty_expression_hits <= 3
    checks = {
        "硬性规则": not hits and not punctuation and not placeholders,
        "3秒首屏": three_second["passed"],
        "长文长度": length_ok,
        "开头具体": bool(paragraphs) and len(markdown_text_only(paragraphs[0])) <= 180,
        "节奏层次": short_breaks >= 2 and question_count >= 1,
        "段落呼吸": not dense_paragraphs,
        "口语与个人判断": len(spoken) >= 2 and has_personal,
        "内容支撑": len(paragraphs) >= 10 and (has_specific_detail or bool(evidence)),
        "证据链": bool(evidence),
        "信息密度": density_ok,
        "结构克制": h2_count <= 4 and h3_count <= 6 and heading_count <= 10 and list_count <= 3,
        "重点存在": emphasis_minimum <= emphasis_count <= emphasis_budget,
    }
    passed = sum(bool(value) for value in checks.values())
    passed_ok = all(checks[key] for key in ("硬性规则", "3秒首屏", "长文长度", "段落呼吸", "内容支撑", "证据链", "信息密度", "结构克制", "重点存在")) and passed >= 11
    next_actions = []
    if not length_ok:
        next_actions.append(f"正文当前 {len(plain)} 字，当前档位建议控制在 {length['minimum']} 到 {length['maximum']} 字")
    if placeholders:
        next_actions.append("移除待补、这里应该放等占位表达，改用已有观察或谨慎判断完整收束")
    if hits or punctuation:
        next_actions.append("把命中的套话或禁用标点改成具体、口语化表达")
    if not checks["节奏层次"]:
        next_actions.append("增加短句断裂和自然疑问，拆短过长段落")
    if dense_paragraphs:
        next_actions.append(f"有 {len(dense_paragraphs)} 个普通段落超过 120 字，请只在原句结束处拆段，避免手机端形成文字墙")
    if not checks["3秒首屏"]:
        next_actions.append("按 3s 原则重写首屏，先交代具体事件，再亮出冲突和读者继续阅读能得到的判断")
    if not checks["证据链"]:
        next_actions.append("至少绑定一条可回溯来源，并区分事实、判断和推断")
    if not checks["结构克制"]:
            next_actions.append("控制在 4 个一级小节、6 个二级小节以内，避免每段都做成标题")
    if not checks["信息密度"]:
        next_actions.append("删除重复观点和空泛表达，让每个段落都推进事实、判断或行动")
    if not checks["重点存在"]:
        next_actions.append(f"补充 {emphasis_minimum} 到 {emphasis_budget} 处重点，只标记金句、方法、技巧和风险")
    return {"passed": passed_ok, "score": f"{passed}/{len(checks)}", "forbidden_words": hits, "punctuation": punctuation,
            "spoken_markers": spoken, "placeholders": placeholders, "specific_detail": has_specific_detail,
            "plain_length": len(plain), "length_preset": length_preset, "target_length": length,
            "heading_count": heading_count, "h2_count": h2_count, "h3_count": h3_count,
            "list_count": list_count, "emphasis_count": emphasis_count,
            "dense_paragraph_count": len(dense_paragraphs), "max_paragraph_length": max(ordinary_lengths or [0]),
            "repeated_paragraphs": repeated_paragraphs, "empty_expression_hits": empty_expression_hits,
            "evidence_count": len(evidence), "three_second": three_second, "checks": checks, "layers": {
                "硬性禁用词": checks["硬性规则"], "3秒首屏": checks["3秒首屏"],
                "风格一致性": checks["开头具体"] and checks["节奏层次"] and checks["口语与个人判断"],
                "内容支撑": checks["内容支撑"] and checks["证据链"] and checks["信息密度"], "活人感终审": checks["口语与个人判断"] and not placeholders},
            "next_actions": next_actions}


class ImageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self._scoped_images: list[str] = []
        self._unscoped_images: list[str] = []
        self._tag_stack: list[tuple[str, bool, bool]] = []
        self._excluded_depth = 0
        self._content_depth = 0
        self.excluded_count = 0
        self.og_image = ""

    @property
    def images(self) -> list[str]:
        # Prefer recognized article content once a page exposes it. This keeps
        # header, recommendation and footer images out of the source pool.
        return self._scoped_images if self._scoped_images else self._unscoped_images

    @staticmethod
    def _class_text(attrs_dict: dict[str, str]) -> str:
        return " ".join(attrs_dict.get(key, "") for key in ("id", "class", "role", "aria-label", "data-testid")).lower()

    @staticmethod
    def _is_excluded_container(tag: str, marker: str) -> bool:
        if tag.lower() in {"header", "footer", "nav", "aside"}:
            return True
        return any(token in marker for token in EXCLUDED_IMAGE_CONTAINER_MARKERS)

    @staticmethod
    def _is_content_container(tag: str, marker: str) -> bool:
        if tag.lower() in {"article", "main"}:
            return True
        return any(token in marker for token in CONTENT_IMAGE_CONTAINER_MARKERS)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta" and attrs_dict.get("property", "").lower() in {"og:image", "twitter:image"}:
            candidate = urljoin(self.base_url, attrs_dict.get("content", ""))
            if not any(token in candidate.lower() for token in NON_ARTICLE_IMAGE_MARKERS):
                self.og_image = candidate
        marker = self._class_text(attrs_dict)
        excluded = self._excluded_depth > 0 or self._is_excluded_container(tag, marker)
        content = not excluded and self._is_content_container(tag, marker)
        self._tag_stack.append((tag.lower(), excluded, content))
        if excluded:
            self._excluded_depth += 1
        if content:
            self._content_depth += 1
        if tag.lower() == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or attrs_dict.get("data-original")
            if src:
                candidate = urljoin(self.base_url, src)
                if excluded or any(token in candidate.lower() for token in NON_ARTICLE_IMAGE_MARKERS):
                    self.excluded_count += 1
                elif self._content_depth:
                    self._scoped_images.append(candidate)
                else:
                    self._unscoped_images.append(candidate)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.lower()
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index][0] != wanted:
                continue
            closing = self._tag_stack[index:]
            del self._tag_stack[index:]
            for _, excluded, content in reversed(closing):
                if excluded:
                    self._excluded_depth = max(0, self._excluded_depth - 1)
                if content:
                    self._content_depth = max(0, self._content_depth - 1)
            break


DECORATIVE_IMAGE_MARKERS = (
    "avatar", "favicon", "icon", "logo", "emoji", "sticker", "badge", "sprite",
    "qrcode", "qr-code", "profile", "author", "head", "user", "default", "80-80",
    "64x64", "48x48", "32x32", "16x16"
)

CONTENT_IMAGE_CONTAINER_MARKERS = (
    "article-content", "article-body", "post-content", "post-body", "entry-content", "story-body",
    "rich_media_content", "rich_media_area_primary", "main-content", "正文", "文章正文",
)
EXCLUDED_IMAGE_CONTAINER_MARKERS = (
    "comment", "comments", "comment-list", "comment-area", "reply", "replies", "discussion", "discuss",
    "留言", "评论", "回复", "讨论", "recommend", "recommended", "related", "相关推荐", "sidebar",
    "side-bar", "advert", "ad-container", "share", "social", "header", "footer", "navigation",
)
NON_ARTICLE_IMAGE_MARKERS = (
    "comment", "comments", "reply", "replies", "discussion", "recommend", "recommended", "related",
    "avatar", "favicon", "icon", "logo", "emoji", "sticker", "badge", "sprite", "qrcode", "qr-code",
    "profile", "author", "head", "user", "default", "80-80", "64x64", "48x48", "32x32", "16x16",
)


def image_candidate_reason(url: str, data: bytes, mime: str) -> str:
    lowered = (urlparse(url).path + "?" + urlparse(url).query).lower()
    for marker in NON_ARTICLE_IMAGE_MARKERS:
        if marker in lowered:
            return f"非正文图片特征：{marker}"
    for marker in DECORATIVE_IMAGE_MARKERS:
        if marker in lowered:
            return f"装饰性图片特征：{marker}"
    if len(data) < 12 * 1024:
        return "文件过小，疑似图标或头像"
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
            if min(width, height) < 240:
                return f"尺寸过小：{width}×{height}"
            if width * height < 120000:
                return f"像素过少：{width}×{height}"
        except Exception:
            return "图片无法解析"
    if not mime.startswith("image/"):
        return "不是图片文件"
    return ""


def registered_image_candidate_reason(asset: dict) -> str:
    try:
        path = safe_relative_path(str(asset.get("path", "")))
        if not path.exists() or not path.is_file():
            return "本地文件不存在"
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or ""
        return image_candidate_reason(str(asset.get("source_url", "")) or path.name, data, mime)
    except Exception as exc:
        return redact_sensitive(exc)


def assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只支持 http/https 网页地址")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
        for info in addresses:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("为安全起见，不读取本机或内网地址")
    except socket.gaierror as exc:
        raise ValueError("网址无法解析") from exc


def download_url(url: str, max_bytes: int = 6 * 1024 * 1024) -> tuple[bytes, str]:
    assert_public_url(url)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,image/*,*/*;q=0.8"})
    with urlopen(request, timeout=15, context=TLS_CONTEXT) as response:
        content_type = response.headers.get_content_type()
        content_length = int(response.headers.get("Content-Length", "0") or 0)
        if content_length > max_bytes:
            raise ValueError("文件过大")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("文件过大")
        return data, content_type


def import_images_from_url(page_url: str, limit: int = 8) -> dict:
    page_bytes, content_type = download_url(page_url, max_bytes=4 * 1024 * 1024)
    candidates = []
    excluded_count = 0
    if content_type.startswith("image/"):
        candidates = [page_url]
        source_scope = "direct_image"
    else:
        text = page_bytes.decode("utf-8", errors="ignore")
        parser = ImageParser(page_url)
        parser.feed(text)
        candidates = ([parser.og_image] if parser.og_image else []) + parser.images
        excluded_count = parser.excluded_count
        source_scope = "article_body"
    unique = []
    for candidate in candidates:
        if candidate and candidate not in unique and urlparse(candidate).scheme in {"http", "https"}:
            unique.append(candidate)
    imported = []
    skipped: list[dict[str, str]] = []
    candidate_limit = max(max(1, min(limit, 8)) * 4, 16)
    for image_url in unique[:candidate_limit]:
        try:
            data, mime = download_url(image_url)
            if not mime.startswith("image/"):
                skipped.append({"url": image_url, "reason": "不是图片文件"})
                continue
            reason = image_candidate_reason(image_url, data, mime)
            if reason:
                skipped.append({"url": image_url, "reason": reason})
                continue
            ext = mimetypes.guess_extension(mime) or Path(urlparse(image_url).path).suffix or ".bin"
            if ext == ".jpe":
                ext = ".jpg"
            filename = f"{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}"
            output = ASSET_DIR / filename
            output.write_bytes(data)
            with db_conn() as conn:
                cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,source_page_url,source_kind,source_scope,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                     (filename, str(output.relative_to(ROOT)), "image", image_url, page_url, "source", source_scope, "来源页面正文区域已记录，版权待确认", "", "来源图", now_iso()))
                imported.append({"id": cursor.lastrowid, "name": filename, "path": str(output.relative_to(ROOT)), "source_url": image_url,
                                 "source_page_url": page_url, "source_kind": "source", "source_scope": source_scope,
                                 "rights_note": "来源页面正文区域已记录，版权待确认"})
        except Exception:
            continue
        if len(imported) >= max(1, min(limit, 8)):
            break
    message = f"识别到 {len(unique)} 张正文候选图，导入 {len(imported)} 张正文候选图"
    if skipped:
        message += f"，过滤 {len(skipped)} 张头像、图标、评论区或无效图片"
    if excluded_count:
        message += f"，排除 {excluded_count} 张非正文区域图片"
    return {"page_url": page_url, "found": len(unique), "imported": imported, "skipped": skipped, "message": message}


def markdown_text_only(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value or "")
    value = re.sub(r"(?m)^\s*#{1,6}\s+", "", value)
    value = re.sub(r"(?m)^\s*>\s*", "", value)
    value = re.sub(r"(?m)^\s*(?:\[[^\]]+\]|引言|方法|技巧|风险|金句)\s*(?:[|｜:：]\s*)?", "", value)
    value = re.sub(r"(?m)^\s*(?:[-*]|\d+[.)])\s+", "", value)
    value = re.sub(r"[*_`=~^]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def image_count_in_markdown(body: str) -> int:
    return len(re.findall(r"!\[[^\]]*\]\([^)]*\)", body or ""))


def is_local_fallback_image_ref(source: str) -> bool:
    name = Path(urlparse(str(source or "")).path).name.lower()
    return name.startswith(("info-card-", "local-editorial-"))


def remove_local_fallback_images(body: str) -> str:
    """Remove old offline placeholders so a later run can replace them with real images."""
    pattern = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    return pattern.sub(lambda match: "" if is_local_fallback_image_ref(match.group(1).strip()) else match.group(0), body or "")


def remove_unscoped_source_images(body: str) -> str:
    """Drop legacy source-image references that were not verified as article-body images."""
    references = {match.group(1).strip() for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", body or "")}
    if not references:
        return body
    with db_conn() as conn:
        rows = conn.execute("SELECT path FROM asset WHERE usage='来源图' AND source_kind='source' AND COALESCE(source_scope,'')!='article_body'").fetchall()
    blocked = {str(row["path"]) for row in rows if str(row["path"]) in references}
    if not blocked:
        return body
    return re.sub(r"!\[[^\]]*\]\(([^)]+)\)", lambda match: "" if match.group(1).strip() in blocked else match.group(0), body or "")


def split_long_image_block(block: str, max_chars: int = 500) -> list[str]:
    """Split a long prose block at sentence boundaries so image slots can be placed naturally."""
    clean = block.strip()
    if len(markdown_text_only(clean)) <= max_chars or clean.startswith(("#", "!")):
        return [clean]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?；;])\s*", clean) if item.strip()]
    if len(sentences) <= 1:
        return [clean[index:index + max_chars].strip() for index in range(0, len(clean), max_chars)]
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for sentence in sentences:
        sentence_length = len(markdown_text_only(sentence))
        if current and current_length + sentence_length > max_chars:
            chunks.append("".join(current).strip())
            current = []
            current_length = 0
        current.append(sentence)
        current_length += sentence_length
    if current:
        chunks.append("".join(current).strip())
    return chunks or [clean]


def choose_image_blocks(body: str) -> tuple[list[str], list[int], int]:
    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n", body or "") if block.strip()]
    blocks: list[str] = []
    for block in raw_blocks:
        blocks.extend(split_long_image_block(block))
    text_length = len(markdown_text_only(body or ""))
    target_count = (text_length + 499) // 500 if text_length else 0
    existing_count = image_count_in_markdown(body or "")
    needed = max(0, target_count - existing_count)
    eligible = [i for i, block in enumerate(blocks)
                if not block.startswith("!") and not block.startswith("#") and len(markdown_text_only(block)) >= 25]
    if not eligible or not needed:
        return blocks, [], target_count
    cumulative = 0
    positions: list[int] = []
    thresholds = [500 * (i + 1) for i in range(needed)]
    for index, block in enumerate(blocks):
        cumulative += len(markdown_text_only(block))
        if index not in eligible:
            continue
        if thresholds and cumulative >= thresholds[0]:
            positions.append(index)
            thresholds.pop(0)
    for index in reversed(eligible):
        if len(positions) >= needed:
            break
        if index not in positions:
            positions.append(index)
    return blocks, sorted(positions[:needed]), target_count


ABSTRACT_VISUAL_TERMS = (
    "判断", "趋势", "价值", "意义", "未来", "普通人", "成本", "选择", "方法", "风险",
    "机会", "门槛", "能力", "影响", "变化", "习惯", "效率", "注意力", "观点", "结论",
)
CONCRETE_VISUAL_TERMS = (
    "屏幕", "页面", "按钮", "图片", "文件", "工作台", "手", "房间", "人物", "桌面", "手机",
    "电脑", "键盘", "纸张", "镜头", "窗口", "产品", "办公室", "街道", "商店", "实验室", "操作",
    "金额", "数字", "时间", "聊天", "代码", "文章", "原文", "界面", "会议", "模型",
)

# The reference workflow uses a restrained editorial poster language rather than
# a generic "AI illustration" preset. Keep the variation deterministic so the
# same paragraph does not produce the same composition on every retry.
ZINE_LAYOUT_RECIPES = (
    ("lower-left-float", "lower-left quadrant", "torn-paper clipping", "short phrase pressed against the clipping edge", "xerox softness", "quiet archival"),
    ("upper-right-block", "upper-right quadrant", "solid color paper block", "small typewriter caption below the block", "risograph grain", "distant diary-like"),
    ("dual-panel", "center-left", "two small overlapping paper fragments", "fragmented floating letters between the fragments", "halftone degradation", "slightly surreal"),
    ("single-specimen", "lower-middle", "one isolated object specimen", "almost textless with one tiny caption", "scan noise and paper fibers", "calm and observational"),
    ("irregular-cutout", "upper-middle", "irregular organic paper cutout", "low-contrast archive microtext near the cutout", "letterpress ink bleed", "memory-like"),
)
ZINE_ACCENTS = ("tomato red", "cobalt blue", "pear green", "ultramarine", "lemon yellow")


def zine_recipe_for(text: str, title: str = "") -> dict:
    """Pick a stable, varied recipe for the adapted landscape zine mode."""
    seed = hashlib.sha256(f"{title}\n{text}".encode("utf-8", "ignore")).digest()
    layout = ZINE_LAYOUT_RECIPES[seed[0] % len(ZINE_LAYOUT_RECIPES)]
    return {
        "layout": layout[0],
        "position": layout[1],
        "anchor": layout[2],
        "typography": layout[3],
        "texture": layout[4],
        "mood": layout[5],
        "accent": ZINE_ACCENTS[seed[1] % len(ZINE_ACCENTS)],
    }


def compile_zine_image_prompt(subject: str, visual_intent: str, title: str, source_text: str) -> tuple[str, dict]:
    """Compile model-extracted meaning into the reference skill's visual grammar."""
    recipe = zine_recipe_for(source_text, title)
    subject = re.sub(r"\s+", " ", subject or "").strip(" 。；;，,")[:220]
    intent = re.sub(r"\s+", " ", visual_intent or "").strip(" 。；;，,")[:160]
    if not subject:
        subject = "段落中的一个具体物件、动作或关系"
    if not intent:
        intent = "把段落的核心判断压缩成一个可被看见的瞬间"
    prompt = (
        f"横版 4:3 公众号正文编辑配图，整幅画面是一张铺满画布的旧纸张，没有边框、没有设备样机。"
        f"保留约 62%-80% 的安静留白，只让一个中小型视觉集群占据约 18%-35% 画面，放在{recipe['position']}，"
        "主体比极简海报中的微小锚点稍稍放大，主体本身占视觉集群约 25%-40%，必须完整可辨，不得缩成小点或细线；"
        "画面保持平视扫描稿关系，不做满幅场景。\n\n"
        f"把“{subject}”作为唯一主视觉，用{recipe['anchor']}表现；它要承担“{intent}”这一视觉隐喻，"
        "只保留段落真正需要的对象、动作和关系，不添加随机人物、城市天际线或通用科技符号。"
        "主体使用灰阶照片、旧印刷插图、纸张碎片或低对比剪影的质感，边缘可以有撕纸、复印、网点和轻微错位。\n\n"
        f"采用{recipe['typography']}，只允许极短、稀疏、可有可无的打字机或衬线微型字，不出现长句。"
        f"全图只使用一个明显的高饱和{recipe['accent']}色锚点，呈不透明平面色块、剪纸或主体局部，"
        "约占整幅 0.8%-2.5%，缩略图也能看见；纸张、照片和辅助标记保持灰黑与米白。"
        f"整体采用{recipe['texture']}、纸纤维、旧印刷磨损和轻微油墨渗出。\n\n"
        f"情绪是{recipe['mood']}，克制、安静、像日本或韩国独立 zine 的编辑海报；哑光吸墨纸，漫射光，低到中等对比。"
        "执行图片 3s 原则，缩略图或首屏停留约 3 秒时，读者必须先看懂一个主体、一个动作或冲突、一个视觉重点；"
        "如果画面不能用一句短话说清楚，就删掉装饰并强化主体关系。"
        " "
        "没有硬阴影、景深或三维纵深。不要出现完整场景铺满、商业广告、品牌 Logo、CTA、水印、干净 UI、"
        "蓝紫渐变、发光网络、漂浮图标、随机仪表盘、机器人、霓虹、赛博朋克、可爱卡通、时尚大片、"
        "密集拼贴、过多物件、过多颜色、长段可读文字或无意义的科技装饰。"
    )
    return prompt, recipe


def paragraph_visual_kind(text: str) -> str:
    """Classify a paragraph for diagnostics; all missing slots use the zine compiler."""
    plain = markdown_text_only(text)
    abstract_score = sum(1 for term in ABSTRACT_VISUAL_TERMS if term in plain)
    concrete_score = sum(1 for term in CONCRETE_VISUAL_TERMS if term in plain)
    has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|元|万|亿|岁|天|次|张|字)?", plain))
    if concrete_score >= 2 or (concrete_score and has_number and abstract_score <= concrete_score + 1):
        return "ai_scene"
    return "info_card"


def external_image_model_configured() -> bool:
    """Return whether auto layout can actually call an image endpoint."""
    if (os.getenv("RIGHTCODE_IMAGE_API_KEY", "") or os.getenv("RIGHT_CODE_IMAGE_API_KEY", "") or
            os.getenv("RIGHTCODE_API_KEY", "") or os.getenv("RIGHT_CODE_API_KEY", "")):
        return True
    if os.getenv("OPENAI_IMAGE_API_KEY", ""):
        return True
    generic_key = os.getenv("OPENAI_API_KEY", "")
    base = os.getenv("OPENAI_IMAGE_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    base = base.lower()
    return bool(generic_key and "deepseek.com" not in base and "yuzapi.fun" not in base)


def source_urls_for_draft(topic: dict, evidence: list) -> list[str]:
    urls: list[str] = []
    source_url = str(topic.get("source_url") or "").strip()
    if source_url:
        urls.append(source_url)
    for item in evidence if isinstance(evidence, list) else []:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("source_url") or "").strip()
            if url:
                urls.append(url)
    return list(dict.fromkeys(url for url in urls if urlparse(url).scheme in {"http", "https"}))


def insert_image_blocks(blocks: list[str], placements: dict[int, dict]) -> str:
    output: list[str] = []
    for index, block in enumerate(blocks):
        output.append(block)
        asset = placements.get(index)
        if asset:
            alt = re.sub(r"\s+", " ", str(asset.get("name", "正文配图"))).strip() or "正文配图"
            output.append(f"![{alt}]({asset['path']})")
    return "\n\n".join(output)


def auto_layout_draft_images(draft_id: int, body_override: str = "", title_override: str = "") -> dict:
    with db_conn() as conn:
        draft_row = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        topic_row = conn.execute("SELECT t.*, s.source_url, s.aihot_url, s.title AS source_title FROM topic t LEFT JOIN source_item s ON s.id=t.source_id WHERE t.id=?", (draft_row["topic_id"],)).fetchone() if draft_row and draft_row["topic_id"] else None
    if not draft_row:
        raise ValueError("草稿不存在")
    draft = row_to_json(draft_row) or {}
    topic = row_to_json(topic_row) or {}
    body = str(body_override or draft.get("body") or "").strip()
    title = str(title_override or draft.get("title") or draft.get("topic_title") or "未命名文章").strip()
    if not body:
        raise ValueError("正文为空，先生成或写入文章正文")
    original_body = body
    body = remove_unscoped_source_images(body)
    if external_image_model_configured():
        body = remove_local_fallback_images(body)
    blocks, positions, target_count = choose_image_blocks(body)
    existing_count = image_count_in_markdown(body)
    if not positions:
        updated_draft = draft
        if body != original_body:
            with db_conn() as conn:
                conn.execute("UPDATE draft SET body=?,digest=?,updated_at=? WHERE id=?", (body, markdown_text_only(body)[:120], now_iso(), draft_id))
                updated_draft = row_to_json(conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()) or draft
        return {"ok": True, "draft": updated_draft, "target_count": target_count, "inserted": [], "source_imported": [],
                "generated": [], "failed": [], "message": f"正文已有足够配图（目标 {target_count} 张），暂不重复添加"}

    evidence = draft.get("evidence") if isinstance(draft.get("evidence"), list) else []
    page_urls = source_urls_for_draft(topic, evidence)[:3]
    imported: list[dict] = []
    import_failures: list[str] = []
    for page_url in page_urls:
        try:
            result = import_images_from_url(page_url, limit=4)
            imported.extend(result.get("imported", []))
        except Exception as exc:
            import_failures.append(f"{page_url}：{redact_sensitive(exc)}")

    source_hosts = {urlparse(url).hostname for url in page_urls if urlparse(url).hostname}
    if source_hosts:
        with db_conn() as conn:
            existing_source_assets = conn.execute("SELECT * FROM asset WHERE kind='image' AND usage='来源图' AND source_scope='article_body' ORDER BY id DESC LIMIT 120").fetchall()
        for row in existing_source_assets:
            asset = row_to_json(row) or {}
            if registered_image_candidate_reason(asset):
                continue
            if urlparse(str(asset.get("source_url") or "")).hostname in source_hosts:
                imported.append(asset)

    unique_assets: list[dict] = []
    seen_asset_ids: set[int] = set()
    for asset in imported:
        asset_id = int(asset.get("id", 0) or 0)
        if asset_id and asset_id not in seen_asset_ids:
            unique_assets.append(asset)
            seen_asset_ids.add(asset_id)

    placements: dict[int, dict] = {}
    source_used = 0
    generated: list[dict] = []
    info_cards: list[dict] = []
    failures = list(import_failures)
    for slot_index, block_index in enumerate(positions):
        if source_used < len(unique_assets):
            asset = unique_assets[source_used]
            source_used += 1
            placements[block_index] = asset
            continue
        selected = markdown_text_only(blocks[block_index])[:1000]
        context = "\n".join(markdown_text_only(blocks[i])[:260] for i in range(max(0, block_index - 1), min(len(blocks), block_index + 2)))
        try:
            visual_kind = paragraph_visual_kind(selected)
            if not external_image_model_configured():
                card = generate_local_info_card(selected, "正文信息卡")
                card["visual_kind"] = visual_kind
                card["strategy"] = "offline_fallback"
                placements[block_index] = card
                info_cards.append(card)
            else:
                prompt_result = image_prompt_from_selection(selected, title, context)
                generated_asset = generate_image_asset(prompt_result["prompt"], "正文插图")
                generated_asset["image_prompt"] = prompt_result["prompt"]
                generated_asset["visual_kind"] = visual_kind
                generated_asset["strategy"] = "zine_editorial"
                generated_asset["visual_recipe"] = prompt_result.get("recipe", {})
                placements[block_index] = generated_asset
                generated.append(generated_asset)
        except Exception as exc:
            failures.append(f"第 {slot_index + 1} 张配图：{redact_sensitive(exc)}")

    new_body = insert_image_blocks(blocks, placements)
    inserted = list(placements.values())
    with db_conn() as conn:
        conn.execute("UPDATE draft SET body=?,digest=?,status=?,updated_at=? WHERE id=?", (new_body, markdown_text_only(new_body)[:120], "待排版", now_iso(), draft_id))
        updated = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
    completed_count = existing_count + len(inserted)
    remaining_count = max(0, target_count - completed_count)
    replacement_note = "，已替换旧离线卡" if body != original_body else ""
    message = f"配图规划 {target_count} 张：来源图 {source_used} 张，编辑视觉图 {len(generated)} 张，离线信息卡 {len(info_cards)} 张，正文现有 {existing_count} 张{replacement_note}"
    if remaining_count:
        message += f"，仍缺 {remaining_count} 张"
    if failures:
        detail = "；".join(str(item) for item in failures[:2])
        message += f"。失败 {len(failures)} 项：{detail}"
    return {"ok": True, "draft": row_to_json(updated), "target_count": target_count, "planned": positions,
            "inserted": inserted, "source_imported": unique_assets, "generated": generated, "info_cards": info_cards, "failed": failures,
            "remaining": remaining_count, "message": message}


def run_auto_image_job(job_id: str, draft_id: int, body: str, title: str) -> None:
    try:
        with AUTO_IMAGE_JOBS_LOCK:
            AUTO_IMAGE_JOBS[job_id]["message"] = "正在读取原文图片；来源图不足时再生成…"
        result = auto_layout_draft_images(draft_id, body, title)
        with AUTO_IMAGE_JOBS_LOCK:
            AUTO_IMAGE_JOBS[job_id].update({"status": "done", "message": result.get("message", "自动配图完成"), "result": result})
    except Exception as exc:
        with AUTO_IMAGE_JOBS_LOCK:
            AUTO_IMAGE_JOBS[job_id].update({"status": "failed", "message": redact_sensitive(exc), "error": redact_sensitive(exc)})
    finally:
        with AUTO_IMAGE_JOBS_LOCK:
            if len(AUTO_IMAGE_JOBS) > 48:
                finished = [key for key, value in AUTO_IMAGE_JOBS.items() if value.get("status") in {"done", "failed"}]
                for key in finished[:max(0, len(AUTO_IMAGE_JOBS) - 32)]:
                    AUTO_IMAGE_JOBS.pop(key, None)


def image_prompt_from_selection(selected_text: str, title: str = "", context: str = "") -> dict:
    selected_text = selected_text.strip()
    if not selected_text:
        raise ValueError("请先在正文中选中一段文字")
    selected_text = selected_text[:2400]
    context = context.strip()[:1200]
    visual_mode = paragraph_visual_kind(selected_text)
    prompt = f"""你是公众号编辑部的视觉策划，不负责自由写一条泛化的 AI 绘画提示词。请先从段落中提炼一个可以被看见的主体、动作或关系，再交给工作台的 Minimal Zine 编辑视觉编译器。
不要复述文章，不要补造新闻现场、真实人物、品牌标志或事实。不要把抽象观点翻译成“科技感”，要找一个段落中已有的物件、动作、空间或清晰的视觉隐喻。
执行图片 3s 原则，先问自己，读者看缩略图 3 秒后能不能说出主体、动作或冲突；如果不能，继续收敛为一个更具体的视觉锚点，不要用抽象符号凑图。
输出严格 JSON，字段为 subject、visual_intent、action、setting、metaphor、caption、avoid。每个字段都是简短中文短语；caption 最多 12 个字，没有必要时留空。不要输出 prompt 字段，不要输出 Markdown。

文章标题：{title}
上下文：{context}
选中段落：{selected_text}
"""
    ok, text, error = read_text_model(prompt, "你是一名克制的公众号视觉编辑，先理解段落，再把它翻译成画面语言。")
    if ok:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                result = json.loads(match.group(0))
                subject = str(result.get("subject", "")).strip()
                visual_intent = str(result.get("visual_intent", "")).strip()
                if subject or visual_intent:
                    action = str(result.get("action", "")).strip()
                    setting = str(result.get("setting", "")).strip()
                    metaphor = str(result.get("metaphor", "")).strip()
                    subject_detail = "；".join(item for item in (
                        subject,
                        f"动作：{action}" if action else "",
                        f"场景：{setting}" if setting else "",
                    ) if item)[:260]
                    intent_detail = "；".join(item for item in (
                        visual_intent,
                        f"隐喻：{metaphor}" if metaphor else "",
                    ) if item)[:200]
                    generated, recipe = compile_zine_image_prompt(subject_detail, intent_detail, title, selected_text)
                    return {"prompt": generated, "visual_intent": visual_intent or "已提炼为一个可见的编辑视觉锚点",
                            "avoid": str(result.get("avoid", "")).strip(), "mode": "model",
                            "visual_mode": "zine_editorial", "paragraph_kind": visual_mode,
                            "render_mode": "zine_editorial", "recipe": recipe}
            except json.JSONDecodeError:
                pass
    fallback, recipe = compile_zine_image_prompt(
        "段落中的具体对象、动作或关系",
        f"围绕“{selected_text[:120]}”取一个可视化瞬间",
        title,
        selected_text,
    )
    return {"prompt": fallback, "visual_intent": "将段落压缩为一个具体视觉锚点",
            "avoid": "文字、水印、Logo、机器人、发光网络、漂浮图标、蓝紫渐变、通用科技背景",
            "mode": "local_fallback", "visual_mode": "zine_editorial", "paragraph_kind": visual_mode,
            "render_mode": "zine_editorial",
            "recipe": recipe, "model_note": error or "未配置文本模型，使用本地视觉编译器"}


def save_generated_image(raw: bytes, prompt: str, usage: str, rights_note: str) -> dict:
    filename = f"generated-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
    output = ASSET_DIR / filename
    output.write_bytes(raw)
    with db_conn() as conn:
        cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,source_page_url,source_kind,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (filename, str(output.relative_to(ROOT)), "image", "", "", "ai", rights_note, prompt, usage, now_iso()))
    return {"id": cursor.lastrowid, "name": filename, "path": str(output.relative_to(ROOT)), "prompt": prompt, "usage": usage}


def decode_image_result(data: dict) -> bytes:
    items = data.get("data") if isinstance(data.get("data"), list) else []
    item = items[0] if items else {}
    image_data = item.get("b64_json") or item.get("base64")
    if image_data:
        if image_data.startswith("data:") and "," in image_data:
            image_data = image_data.split(",", 1)[1]
        return base64.b64decode(image_data)
    url = item.get("url", "")
    if url:
        raw, _ = download_url(url, max_bytes=12 * 1024 * 1024)
        return raw
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    for candidate in candidates:
        parts = ((candidate.get("content") or {}).get("parts") or []) if isinstance(candidate, dict) else []
        for part in parts:
            text = part.get("text", "") if isinstance(part, dict) else ""
            if text.startswith("data:") and "," in text:
                return base64.b64decode(text.split(",", 1)[1])
            if text.startswith("http://") or text.startswith("https://"):
                raw, _ = download_url(text, max_bytes=12 * 1024 * 1024)
                return raw
    raise RuntimeError("图片接口未返回可保存的图片")


def rightcode_error_message(data: dict, fallback: str) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error.get("code")
        if message:
            return str(message)
    elif error:
        return str(error)
    for key in ("message", "detail", "error_message"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
    return fallback


def generate_rightcode_image(prompt: str, usage: str, api_key: str) -> dict:
    draw_base = os.getenv("RIGHTCODE_IMAGE_BASE_URL", "https://www.rightapi.ai/draw").rstrip("/")
    task_base = os.getenv("RIGHTCODE_TASK_BASE_URL", "https://www.rightapi.ai").rstrip("/")
    requested_model = os.getenv("RIGHTCODE_IMAGE_MODEL", os.getenv("RIGHT_CODE_IMAGE_MODEL", "gpt-image-2")).strip() or "gpt-image-2"
    model_aliases = {"image2": "gpt-image-2", "image2-vip": "gpt-image-2-vip"}
    model = model_aliases.get(requested_model.lower(), requested_model)
    requested_size = os.getenv("RIGHTCODE_IMAGE_SIZE", "16:9" if "封面" in usage else "4:3").strip() or ("16:9" if "封面" in usage else "4:3")
    # Right Code documents 1:1, 16:9, 9:16, 4:3 and pixel sizes; 3:2 was
    # previously used by the local prompt compiler but is rejected by this API.
    size = {"3:2": "4:3", "2:3": "4:3"}.get(requested_size, requested_size)
    image_size = os.getenv("RIGHTCODE_IMAGE_RESOLUTION", "1K" if model.startswith(("gpt-image-2", "nano-banana")) else "").strip()
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size, "async": True}
    if image_size:
        payload["imageSize"] = image_size
    headers = {"Authorization": "Bearer " + api_key}
    submit_url = draw_base + "/v1/images/generations"
    max_attempts = max(1, int(os.getenv("RIGHTCODE_SUBMIT_RETRIES", "3")))
    retry_delay = max(1.0, float(os.getenv("RIGHTCODE_RETRY_DELAY", "2")))
    status, data = 0, {}
    task_id = None
    for attempt in range(max_attempts):
        status, data, _ = http_json(submit_url, method="POST", payload=payload, headers=headers, timeout=30)
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        task_id = data.get("task_id") or task_data.get("task_id")
        if task_id or status not in {429, 500, 502, 503, 504} or attempt == max_attempts - 1:
            break
        time.sleep(retry_delay * (attempt + 1))
    if status == 0 or status >= 400 or not task_id:
        message = rightcode_error_message(data, "接口没有返回 task_id")
        status_note = f"HTTP {status}" if status else "网络请求失败"
        if status == 503 and "pricing" in message.lower():
            message = f"Right Code 暂时无法读取该模型的计费配置，请稍后重试，并确认后台模型列表中确实存在 {model}"
        raise RuntimeError(f"Right Code 图片任务提交失败（{status_note}，模型 {model}）：{message}")
    task_id = str(task_id)
    deadline = time.time() + float(os.getenv("RIGHTCODE_IMAGE_TIMEOUT", "150"))
    last_status = "processing"
    while time.time() < deadline:
        time.sleep(2)
        poll_status, result, _ = http_json(task_base + "/v1/tasks/" + quote(task_id, safe=""), headers=headers, timeout=30)
        if poll_status >= 400:
            raise RuntimeError(f"Right Code 任务查询失败（HTTP {poll_status}，task_id={task_id}）：{rightcode_error_message(result, '请检查 API Key 与任务归属')}")
        if poll_status == 0:
            continue
        last_status = str(result.get("status", last_status))
        if last_status == "failed":
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            raise RuntimeError(error.get("message", "Right Code 图片任务失败"))
        if last_status in {"completed", "success"} or result.get("data") or result.get("candidates"):
            raw = decode_image_result(result)
            saved = save_generated_image(raw, prompt, usage, f"Right Code {model} 中转生成，任务 {task_id}")
            saved.update({"mode": "rightcode_image2", "task_id": task_id, "message": f"Right Code {model} 配图已保存到本地素材库"})
            return saved
    raise TimeoutError(f"Right Code 图片任务超时，task_id={task_id}，最后状态={last_status}")


def generate_image_asset(prompt: str, usage: str = "原创配图") -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("请输入画面描述")
    rightcode_key = (os.getenv("RIGHTCODE_IMAGE_API_KEY", "") or os.getenv("RIGHT_CODE_IMAGE_API_KEY", "") or
                     os.getenv("RIGHTCODE_API_KEY", "") or os.getenv("RIGHT_CODE_API_KEY", ""))
    if rightcode_key:
        return generate_rightcode_image(prompt, usage, rightcode_key)
    api_key = os.getenv("OPENAI_IMAGE_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    base = os.getenv("OPENAI_IMAGE_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if "deepseek.com" in base and not os.getenv("OPENAI_IMAGE_BASE_URL"):
        return generate_local_editorial_asset(prompt, usage)
    if not api_key:
        return generate_local_editorial_asset(prompt, usage)
    model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    status, data, _ = http_json(base + "/images/generations", method="POST", payload={"model": model, "prompt": prompt, "size": "1024x1024"}, headers={"Authorization": "Bearer " + api_key}, timeout=120)
    if status == 0 or not data.get("data"):
        message = data.get("error", {}).get("message", "图片生成失败") if isinstance(data.get("error"), dict) else str(data.get("error", "图片生成失败"))
        raise RuntimeError(message)
    item = data["data"][0]
    image_data = item.get("b64_json")
    if image_data:
        raw = base64.b64decode(image_data)
    elif item.get("url"):
        raw, _ = download_url(item["url"], max_bytes=12 * 1024 * 1024)
    else:
        raise RuntimeError("图片接口未返回可保存的图片")
    saved = save_generated_image(raw, prompt, usage, "AI 原创，生成提示词已记录")
    saved["message"] = "原创配图已保存到本地素材库"
    return saved


def write_editorial_png(output: Path) -> None:
    """Write a small dependency-free PNG card for platforms that reject SVG."""
    width, height = 1200, 900
    background = (245, 241, 232)
    ink = (29, 28, 25)
    red = (180, 71, 53)
    green = (212, 239, 56)
    rows = bytearray()
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            color = background
            if 64 <= x < width - 64 and 64 <= y < height - 64:
                color = ink
            if (x - 1000) ** 2 + (y - 220) ** 2 < 140 ** 2:
                color = red
            if 116 <= x < 700 and 690 <= y < 698:
                color = green
            row.extend(color)
        rows.extend(row)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(bytes(rows), 9)) +
           chunk(b"IEND", b""))
    output.write_bytes(png)


def wechat_compatible_image_path(asset_path: Path) -> Path:
    """Normalize uploads to JPEG when the source format is not reliably accepted."""
    suffix = asset_path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png"}:
        return asset_path
    if suffix == ".svg":
        output = asset_path.with_suffix(".png")
        if not output.exists():
            write_editorial_png(output)
        return output
    if Image is None:
        if sys.platform == "darwin" and suffix in {".webp", ".avif", ".gif"}:
            output = asset_path.with_name(asset_path.stem + "-wechat.jpg")
            try:
                subprocess.run(["sips", "-s", "format", "jpeg", str(asset_path), "--out", str(output)],
                               check=True, capture_output=True, text=True, timeout=30)
                if output.exists() and output.stat().st_size:
                    return output
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"Mac 图片转换失败：{asset_path.name}，{exc}") from exc
        raise RuntimeError(f"微信不支持 {suffix or '该'} 图片格式，当前环境缺少 Pillow，无法转换为 JPEG")
    output = asset_path.with_name(asset_path.stem + "-wechat.jpg")
    try:
        with Image.open(asset_path) as source:
            # Animated GIFs use their first frame; WeChat articles need a
            # single raster image, and WebP/AVIF are normalized to JPEG.
            try:
                source.seek(0)
            except Exception:
                pass
            image = source.convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            background.save(output, format="JPEG", quality=88, optimize=True)
    except Exception as exc:
        raise RuntimeError(f"图片无法转换为微信公众号兼容格式：{asset_path.name}，{exc}") from exc
    return output


def generate_local_editorial_asset(prompt: str, usage: str) -> dict:
    """Create a usable local visual when no external image model is configured.

    This is deliberately labelled as a local editorial card rather than pretending
    that an AI image model was called. It keeps the asset workflow usable offline.
    """
    filename = f"local-editorial-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
    output = ASSET_DIR / filename
    if Image is None:
        write_editorial_png(output)
    else:
        width, height = 1200, 900
        image = Image.new("RGB", (width, height), "#f5f1e8")
        draw = ImageDraw.Draw(image)
        draw.rectangle((64, 64, width - 64, height - 64), fill="#1d1c19")
        draw.rectangle((64, 64, 390, height - 64), fill="#b44735")
        draw.ellipse((875, 95, 1125, 345), fill="#758d55")
        draw.polygon([(790, 835), (1120, 835), (1120, 625)], fill="#d4ef38")
        font_paths = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        def font(size: int):
            for candidate in font_paths:
                if Path(candidate).exists():
                    try:
                        return ImageFont.truetype(candidate, size)
                    except OSError:
                        pass
            return ImageFont.load_default()
        small, large, foot = font(25), font(48), font(22)
        draw.text((116, 112), "EDITORIAL NOTE / LOCAL MODE", fill="#f5f1e8", font=small)
        label = prompt.replace("\n", " ").strip()
        if len(label) > 38:
            label = label[:38] + "…"
        draw.text((116, 285), label, fill="#f5f1e8", font=large)
        draw.text((116, 733), f"{usage} · 本地原创视觉 · 未调用外部模型", fill="#f5f1e8", font=foot)
        draw.line((116, 690, 700, 690), fill="#d4ef38", width=8)
        image.save(output, format="PNG")
    with db_conn() as conn:
        cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,source_page_url,source_kind,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (filename, str(output.relative_to(ROOT)), "image", "", "", "local", "本地原创视觉，未调用外部模型", prompt, usage, now_iso()))
    return {"id": cursor.lastrowid, "name": filename, "path": str(output.relative_to(ROOT)), "prompt": prompt, "usage": usage,
            "mode": "local_editorial", "message": "本地原创配图已保存（未调用外部模型）"}


def generate_local_info_card(text: str, usage: str = "正文信息卡") -> dict:
    """Make a restrained editorial card for abstract paragraphs without calling an image model."""
    plain = markdown_text_only(text)
    if not plain:
        raise ValueError("信息卡内容为空")
    filename = f"info-card-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
    output = ASSET_DIR / filename
    label = "一句判断"
    if re.search(r"(?:可以|建议|先.*再|方法|步骤|做法)", plain):
        label = "可执行方法"
    elif re.search(r"(?:风险|不要|别急|不能|成本)", plain):
        label = "风险提醒"
    if Image is None:
        write_editorial_png(output)
    else:
        width, height = 1200, 760
        image = Image.new("RGB", (width, height), "#f5f1e8")
        draw = ImageDraw.Draw(image)
        font_paths = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

        def font(size: int):
            for candidate in font_paths:
                if Path(candidate).exists():
                    try:
                        return ImageFont.truetype(candidate, size)
                    except OSError:
                        pass
            return ImageFont.load_default()

        small, label_font, body_font, foot = font(22), font(28), font(48), font(20)
        draw.rectangle((64, 64, width - 64, height - 64), outline="#1d1c19", width=3)
        draw.rectangle((64, 64, 84, height - 64), fill="#b44735")
        draw.text((112, 104), "EDITORIAL DESK / INFO CARD", fill="#6b655b", font=small)
        draw.text((112, 172), label, fill="#b44735", font=label_font)
        draw.line((112, 230, 1088, 230), fill="#d8d0c3", width=2)
        card_text = plain[:92] + ("…" if len(plain) > 92 else "")
        wrapped = "\n".join(card_text[index:index + 18] for index in range(0, len(card_text), 18))
        draw.multiline_text((112, 286), wrapped, fill="#1d1c19", font=body_font, spacing=12)
        draw.rectangle((1000, 590, 1060, 650), fill="#d4ef38")
        draw.text((112, 660), "正文配图 · 信息卡补位 · 不调用外部模型", fill="#6b655b", font=foot)
        image.save(output, format="PNG")
    card_prompt = f"{label}：{plain[:240]}"
    with db_conn() as conn:
        cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,source_page_url,source_kind,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (filename, str(output.relative_to(ROOT)), "image", "", "", "local", "本地编辑信息卡，非外部图片，适合抽象段落", card_prompt, usage, now_iso()))
    return {"id": cursor.lastrowid, "name": filename, "path": str(output.relative_to(ROOT)), "prompt": card_prompt,
            "usage": usage, "mode": "local_info_card", "message": "已用本地信息卡补足抽象段落"}


def md_to_html(markdown: str, image_map: dict[str, str] | None = None) -> str:
    image_map = image_map or {}
    render_blocks: list[str] = []
    for block in [item.strip() for item in re.split(r"\n\s*\n", markdown.replace("\r\n", "\n")) if item.strip()]:
        render_blocks.extend(split_readability_paragraph(block))
    lines = "\n\n".join(render_blocks).split("\n")
    output: list[str] = []
    list_tag = ""
    h2_number = 0
    paragraph_streak = 0

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            output.append(f"</{list_tag}>")
            list_tag = ""

    for raw in lines:
        line = raw.strip()
        if not line:
            close_list()
            continue
        image_match = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if image_match:
            close_list()
            paragraph_streak = 0
            alt, src = image_match.groups()
            src = image_map.get(src, src)
            output.append(
                f'<p class="wechat-image" style="margin:32px 0;text-align:center;line-height:0">'
                f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}" '
                'style="width:100%;max-width:100%;height:auto;display:block;margin:0 auto;padding:4px;box-sizing:border-box;'
                'border:1px solid #e0d8cc;border-radius:3px;background:#ffffff;box-shadow:0 8px 20px rgba(50,42,32,.08)"></p>'
            )
        elif line == "---":
            close_list()
            paragraph_streak = 0
            output.append('<hr style="width:56px;border:0;border-top:2px solid #b44735;margin:38px auto">')
        elif line.startswith("> "):
            close_list()
            paragraph_streak = 0
            quote = line[2:].strip()
            label_match = re.match(r"^\[(引言|方法|技巧|风险|金句)\]\s*(.*)$", quote)
            if label_match:
                label, content = label_match.groups()
                styles = {
                    "引言": ("wechat-lead", "#b44735", "#fbf7ef", "#5d574f", "引言", "border-top:1px solid #b44735;border-bottom:1px solid #b44735"),
                    "方法": ("wechat-method", "#758d55", "#f1f4e8", "#3f5130", "方法", "border:1px solid #bdc9a8;border-left:5px solid #758d55"),
                    "技巧": ("wechat-method", "#758d55", "#f1f4e8", "#3f5130", "技巧", "border:1px solid #bdc9a8;border-left:5px solid #758d55"),
                    "风险": ("wechat-risk", "#b44735", "#f8ece8", "#713329", "风险", "border:1px solid #dfb7ae;border-left:5px solid #b44735"),
                    "金句": ("wechat-punchline", "#1d1c19", "#f3efe7", "#1d1c19", "金句", "border:1px solid #aaa196"),
                }[label]
                class_name, border, background, color, display_label, frame = styles
                output.append(
                    f'<div class="wechat-callout {class_name}" style="margin:28px 0;padding:17px 19px;{frame};background:{background};color:{color};line-height:1.95;border-radius:2px">'
                    f'<div style="margin-bottom:8px"><span style="display:inline-block;padding:2px 7px;border:1px solid {border};'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;font-size:10px;line-height:1.4;letter-spacing:.12em;color:{border}">{display_label}</span></div>'
                    f'<div style="font-size:16px;letter-spacing:.02em">{inline_html(content, image_map)}</div></div>'
                )
            else:
                output.append(f'<blockquote style="margin:28px 0;padding:16px 19px;border-left:4px solid #b44735;background:#fbf7ef;color:#625b50;line-height:1.95">{inline_html(quote, image_map)}</blockquote>')
        elif line.startswith("### "):
            close_list()
            paragraph_streak = 0
            output.append(f'<h3 class="wechat-h3" style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;font-size:17px;line-height:1.6;margin:34px 0 15px;padding-left:12px;border-left:3px solid #758d55;color:#403c35;font-weight:700;letter-spacing:.02em">{inline_html(line[4:], image_map)}</h3>')
        elif line.startswith("## "):
            close_list()
            paragraph_streak = 0
            h2_number += 1
            output.append(
                f'<h2 class="wechat-h2" style="font-family:Georgia,\'Songti SC\',\'STSong\',serif;font-size:20px;line-height:1.55;margin:48px 0 24px;padding:15px 16px 14px;'
                'border:1px solid #d8d0c3;border-left:5px solid #b44735;background:#fbf8f1;box-shadow:4px 4px 0 #eee7db;'
                'color:#1d1c19;font-weight:700;letter-spacing:.01em">'
                f'<span style="display:inline-block;vertical-align:middle;margin-right:10px;padding:2px 7px;border:1px solid #b44735;'
                f'background:#b44735;color:#fffdf8;font-size:11px;line-height:1.4;font-weight:600;letter-spacing:.08em">{h2_number:02d}</span>'
                f'<span style="vertical-align:middle">{inline_html(line[3:], image_map)}</span></h2>'
            )
        elif line.startswith("# "):
            close_list()
            paragraph_streak = 0
            output.append(f'<h1 style="font-family:Georgia,\'Songti SC\',\'STSong\',serif;font-size:28px;line-height:1.35;margin:0 0 1.2em;color:#1d1c19;font-weight:500;letter-spacing:-.02em">{inline_html(line[2:], image_map)}</h1>')
        elif re.match(r"^[-*] ", line) or re.match(r"^\d+[.)] ", line):
            paragraph_streak = 0
            wanted = "ol" if re.match(r"^\d+[.)] ", line) else "ul"
            if list_tag != wanted:
                close_list()
                list_tag = wanted
                output.append(f'<{list_tag} style="margin:18px 0;padding-left:24px;line-height:1.9">')
            content = re.sub(r"^(?:[-*]|\d+[.)])\s+", "", line)
            output.append(f"<li>{inline_html(content, image_map)}</li>")
        else:
            close_list()
            paragraph_streak += 1
            plain_length = len(markdown_text_only(line))
            bottom_space = 30 if paragraph_streak % 3 == 0 or plain_length <= 24 else 18
            rhythm_class = " wechat-breath" if bottom_space == 30 else ""
            output.append(f'<p class="wechat-paragraph{rhythm_class}" style="margin:0;padding:0 0 {bottom_space}px;line-height:1.95;color:#3f3a34;font-size:17px;letter-spacing:.02em;text-align:justify;word-break:break-word">{inline_html(line, image_map)}</p>')
    close_list()
    return "".join(output)


def inline_html(value: str, image_map: dict[str, str]) -> str:
    escaped = html.escape(value)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" style="color:#657c47;text-decoration:underline;text-underline-offset:3px">\1</a>', escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r'<strong style="color:#1d1c19;font-weight:700">\1</strong>', escaped)
    escaped = re.sub(r"==([^=]+)==", r'<mark style="background:#eef2af;color:#30351f;padding:1px 3px">\1</mark>', escaped)
    escaped = re.sub(r"__([^_]+)__", r'<u style="text-decoration-color:#758d55;text-decoration-thickness:2px;text-underline-offset:4px;font-weight:600">\1</u>', escaped)
    escaped = re.sub(r"\^\^([^\^]+)\^\^", r'<span style="color:#b44735;font-weight:600">\1</span>', escaped)
    escaped = re.sub(r"`([^`]+)`", r'<code style="background:#eee8dc;padding:2px 5px;border-radius:4px">\1</code>', escaped)
    return escaped


def wrap_wechat_document(content: str) -> str:
    """Use one self-contained article shell for local preview and WeChat drafts."""
    return (
        '<section class="wechat-canvas" style="display:block;width:100%;max-width:620px;margin:0 auto;'
        'padding:38px 32px 56px;box-sizing:border-box;border:0;background:#fffdf8;color:#37332d;'
        'font-family:\'Songti SC\',\'STSong\',serif;font-size:16px;line-height:1.95;white-space:normal">'
        f'{content}</section>'
    )


def wechat_error_message(data: dict, fallback: str = "微信接口请求失败") -> str:
    message = str(data.get("errmsg", "") or fallback) if isinstance(data, dict) else fallback
    lowered = message.lower()
    if "not in whitelist" in lowered or "invalid ip" in lowered or data.get("errcode") == 40164:
        match = re.search(r"invalid ip\s+((?:\d{1,3}\.){3}\d{1,3})", message, re.I)
        ip = match.group(1) if match else "当前出口 IP"
        return f"公众号 IP 白名单未包含 {ip}。请到公众号后台的 设置与开发 → 基本配置 → IP 白名单，添加 {ip}（只填 IPv4，不要填写 ::ffff: 前缀），保存后再重试。"
    if "unsupported file type" in lowered or "invalid file type" in lowered:
        return "微信拒绝了上传的图片格式。工作台会在重试时把 WebP、AVIF、GIF 等图片转换为 JPEG；请重启工作台后再试。"
    if data.get("errcode") == 48001:
        return "当前公众号没有图文数据分析接口权限（48001）。草稿和发布功能不受影响；数据复盘需以公众号后台实际开放权限为准。"
    return message


class WeChatClient:
    def __init__(self) -> None:
        self.app_id = os.getenv("WECHAT_APP_ID", "")
        self.app_secret = os.getenv("WECHAT_APP_SECRET", "")
        self.token = ""
        self.expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)
    def access_token(self) -> str:
        if self.token and time.time() < self.expires_at - 120:
            return self.token
        if not self.configured:
            raise ValueError("未配置 WECHAT_APP_ID / WECHAT_APP_SECRET")
        query = f"grant_type=client_credential&appid={quote(self.app_id)}&secret={quote(self.app_secret)}"
        status, data, _ = http_json("https://api.weixin.qq.com/cgi-bin/token?" + query)
        if status == 0 or data.get("errcode"):
            raise RuntimeError(wechat_error_message(data, "微信 access_token 获取失败"))
        self.token = data["access_token"]
        self.expires_at = time.time() + int(data.get("expires_in", 7200))
        return self.token

    def api_json(self, endpoint: str, payload: dict | None = None) -> dict:
        token = self.access_token()
        separator = "&" if "?" in endpoint else "?"
        status, data, _ = http_json("https://api.weixin.qq.com" + endpoint + separator + "access_token=" + quote(token), method="POST" if payload is not None else "GET", payload=payload)
        if status == 0 or data.get("errcode"):
            raise RuntimeError(wechat_error_message(data))
        return data

    def test(self) -> dict:
        self.access_token()
        return {"ok": True, "configured": True, "message": "公众号接口连接成功"}

    def upload_cover(self, asset_path: Path) -> str:
        asset_path = wechat_compatible_image_path(asset_path)
        token = self.access_token()
        boundary = "----Workbench" + uuid.uuid4().hex
        data = asset_path.read_bytes()
        filename = asset_path.name.replace('"', '') or "cover.jpg"
        content_type = mimetypes.guess_type(asset_path.name)[0] or "image/jpeg"
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="media"; filename="{filename}"\r\n'
                  f"Content-Type: {content_type}\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        body = header + data + f"\r\n--{boundary}--\r\n".encode()
        req = Request("https://api.weixin.qq.com/cgi-bin/material/add_material?type=image&access_token=" + quote(token), data=body, method="POST", headers={"User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(req, timeout=30, context=TLS_CONTEXT) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("errcode"):
            raise RuntimeError(wechat_error_message(result, "微信封面上传失败"))
        return result["media_id"]

    def upload_inline(self, asset_path: Path) -> str:
        asset_path = wechat_compatible_image_path(asset_path)
        token = self.access_token()
        boundary = "----Workbench" + uuid.uuid4().hex
        data = asset_path.read_bytes()
        filename = asset_path.name.replace('"', '') or "inline.jpg"
        content_type = mimetypes.guess_type(asset_path.name)[0] or "image/jpeg"
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="media"; filename="{filename}"\r\n'
                  f"Content-Type: {content_type}\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        body = header + data + f"\r\n--{boundary}--\r\n".encode()
        req = Request("https://api.weixin.qq.com/cgi-bin/media/uploadimg?access_token=" + quote(token), data=body, method="POST", headers={"User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(req, timeout=30, context=TLS_CONTEXT) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("errcode"):
            raise RuntimeError(wechat_error_message(result, "微信正文图片上传失败"))
        return result["url"]

    def add_draft(self, title: str, digest: str, content: str, cover_media_id: str = "") -> dict:
        clean_digest = markdown_text_only(digest)[:120]
        article = {"title": title[:64], "author": os.getenv("WECHAT_AUTHOR", ""), "digest": clean_digest, "content": content,
                   "content_source_url": "", "need_open_comment": 1, "only_fans_can_comment": 0, "show_cover_pic": 1}
        if cover_media_id:
            article["thumb_media_id"] = cover_media_id
        return self.api_json("/cgi-bin/draft/add", {"articles": [article]})

    def get_drafts(self) -> dict:
        return self.api_json("/cgi-bin/draft/batchget", {"offset": 0, "count": 20, "no_content": 1})

    def article_summary(self, begin: str, end: str) -> dict:
        return self.api_json("/datacube/getarticlesummary", {"begin_date": begin, "end_date": end})


WECHAT = WeChatClient()


def sync_wechat_metrics(days: int = 7) -> dict:
    days = max(1, min(int(days or 7), 7))
    end_date = datetime.now().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    started_at = now_iso()
    with db_conn() as conn:
        cursor = conn.execute("""INSERT INTO metric_sync_run
          (date_from,date_to,status,requested_days,started_at)
          VALUES(?,?,'running',?,?)""", (start_date.isoformat(), end_date.isoformat(), days, started_at))
        run_id = cursor.lastrowid
    succeeded_days = 0
    article_count = 0
    metric_count = 0
    errors: list[str] = []
    for offset in range(days):
        metric_date = start_date + timedelta(days=offset)
        date_text = metric_date.isoformat()
        try:
            result = WECHAT.article_summary(date_text, date_text)
            items = result.get("list") if isinstance(result.get("list"), list) else []
            succeeded_days += 1
            with db_conn() as conn:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_date = str(item.get("ref_date") or date_text)[:10]
                    article_key = metric_article_key(item, item_date)
                    published_article_id = match_published_article(conn, item, item_date)
                    if published_article_id:
                        article_key = f"published:{published_article_id}"
                    raw_item = dict(item)
                    raw_item["_source"] = "wechat_api"
                    raw_item["_published_article_id"] = published_article_id
                    raw_item["_missing_fields"] = [key for key in WECHAT_METRIC_LABELS if key not in item]
                    raw_json = json.dumps(raw_item, ensure_ascii=False, sort_keys=True)
                    article_count += 1
                    for metric_type in WECHAT_METRIC_LABELS:
                        if metric_type not in item:
                            continue
                        conn.execute("""INSERT INTO metric_daily
                          (article_key,metric_date,metric_type,value,source,raw_json,synced_at)
                          VALUES(?,?,?,?,'wechat_api',?,?)
                          ON CONFLICT(article_key,metric_date,metric_type,source)
                          DO UPDATE SET value=excluded.value,raw_json=excluded.raw_json,synced_at=excluded.synced_at""",
                                     (article_key, item_date, metric_type, float(item.get(metric_type) or 0), raw_json, now_iso()))
                        metric_count += 1
        except Exception as exc:
            error_text = redact_sensitive(exc)
            errors.append(f"{date_text}：{error_text}")
            if "48001" in error_text or "没有图文数据分析接口权限" in error_text:
                break
    status = "completed" if succeeded_days == days else ("partial" if succeeded_days else "failed")
    error_message = "；".join(errors)[:4000]
    with db_conn() as conn:
        conn.execute("""UPDATE metric_sync_run
          SET status=?,succeeded_days=?,article_count=?,metric_count=?,error_message=?,finished_at=? WHERE id=?""",
                     (status, succeeded_days, article_count, metric_count, error_message, now_iso(), run_id))
        run = conn.execute("SELECT * FROM metric_sync_run WHERE id=?", (run_id,)).fetchone()
    if status == "failed":
        return {"ok": False, "error": error_message or "公众号数据同步失败", "sync": row_to_json(run)}
    summary = metric_summary(days)
    if article_count:
        message = f"已按天同步 {succeeded_days}/{days} 天，收到 {article_count} 条图文统计"
    elif errors:
        message = f"已同步 {succeeded_days}/{days} 天，但部分日期失败；其余日期暂未返回图文统计"
    else:
        message = f"数据接口连接成功，近 {days} 天暂未返回图文统计；新发布文章的数据可能仍在生成"
    return {"ok": True, "message": message, "sync": row_to_json(run), "summary": summary}


def safe_relative_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    if ROOT not in path.parents and path != ROOT:
        raise ValueError("非法文件路径")
    return path


def registered_asset_path(value: str) -> Path:
    path = safe_relative_path(value)
    with db_conn() as conn:
        registered = conn.execute("SELECT 1 FROM asset WHERE path=? LIMIT 1", (value,)).fetchone()
    if not registered:
        raise ValueError("图片必须先登记到素材库，不能直接读取工作区文件")
    return path


def markdown_preview(markdown: str) -> str:
    return md_to_html(markdown)


def prepare_wechat_content(markdown: str, cover_asset_path: Path | None = None) -> str:
    """Upload local images and put the selected cover at the top of the body."""
    image_map: dict[str, str] = {}
    cover_html = ""
    if cover_asset_path and cover_asset_path.exists() and cover_asset_path.is_file():
        cover_url = WECHAT.upload_inline(cover_asset_path)
        cover_html = (
            '<p class="wechat-cover" style="margin:0 0 30px;text-align:center;line-height:0;">'
            f'<img src="{html.escape(cover_url, quote=True)}" alt="文章封面" '
            'style="display:block;width:100%;max-width:900px;height:auto;margin:0 auto;border-radius:8px;" />'
            '</p>'
        )
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        source = match.group(1).strip()
        if source in image_map or urlparse(source).scheme in {"http", "https"}:
            continue
        try:
            asset_path = registered_asset_path(source)
            if asset_path.exists() and asset_path.is_file():
                image_map[source] = WECHAT.upload_inline(asset_path)
        except Exception as exc:
            raise RuntimeError(f"正文图片上传失败：{source}，{exc}") from exc
    return wrap_wechat_document(cover_html + md_to_html(markdown, image_map))


def preview_wechat_content(markdown: str, cover_asset_id: object = None) -> str:
    """Render the exact production layout locally without uploading any assets."""
    image_map: dict[str, str] = {}
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        source = match.group(1).strip()
        if source in image_map or urlparse(source).scheme in {"http", "https"}:
            continue
        try:
            registered_asset_path(source)
        except Exception:
            continue
        image_map[source] = "/media?path=" + quote(source, safe="")

    cover_html = ""
    if cover_asset_id:
        with db_conn() as conn:
            asset = conn.execute("SELECT path FROM asset WHERE id=?", (int(cover_asset_id),)).fetchone()
        if asset:
            asset_path = safe_relative_path(asset["path"])
            if asset_path.exists() and asset_path.is_file():
                cover_url = "/media?path=" + quote(asset["path"], safe="")
                cover_html = (
                    '<p class="wechat-cover" style="margin:0 0 30px;text-align:center;line-height:0;">'
                    f'<img src="{html.escape(cover_url, quote=True)}" alt="cover" '
                    'style="display:block;width:100%;max-width:900px;height:auto;margin:0 auto;border-radius:8px;" />'
                    '</p>'
                )
    return wrap_wechat_document(cover_html + md_to_html(markdown, image_map))


def dashboard() -> dict:
    with db_conn() as conn:
        hot_count = conn.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()["count"]
        topic_count = conn.execute("SELECT COUNT(*) AS count FROM topic").fetchone()["count"]
        writing_count = conn.execute("SELECT COUNT(*) AS count FROM draft WHERE status NOT IN ('草稿已创建','已归档')").fetchone()["count"]
        asset_count = conn.execute("SELECT COUNT(*) AS count FROM asset").fetchone()["count"]
        metric_count = conn.execute("SELECT COUNT(*) AS count FROM metric_daily WHERE source='wechat_api'").fetchone()["count"]
    model_configured = bool(os.getenv("YUZAPI_API_KEY") or os.getenv("YUZ_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("RIGHTCODE_API_KEY") or os.getenv("RIGHT_CODE_API_KEY"))
    return {"hot_count": hot_count, "topic_count": topic_count, "writing_count": writing_count, "asset_count": asset_count, "metric_count": metric_count,
            "style": {"name": "数字生命卡兹克", "skill_loaded": bool(STYLE_CONTEXT.get("skill_path")), "sample_count": len(STYLE_CONTEXT.get("samples", []))},
            "integrations": {"aihot": True, "model": model_configured, "wechat": WECHAT.configured}}


def integration_status() -> dict:
    provider, model = active_text_model()
    image_configured = bool(os.getenv("RIGHTCODE_IMAGE_API_KEY") or os.getenv("RIGHT_CODE_IMAGE_API_KEY") or
                            os.getenv("RIGHTCODE_API_KEY") or os.getenv("RIGHT_CODE_API_KEY") or
                            os.getenv("OPENAI_IMAGE_API_KEY"))
    model_configured = text_model_configured() or image_configured
    return {
        "local": {"ok": True, "host": HOST, "message": "仅绑定本机，凭据不写入内容库"},
        "aihot": {"ok": True, "configured": True, "name": "AI HOT", "message": "公开只读热点接口，可同步并缓存"},
        "model": {"ok": model_configured, "configured": model_configured,
                  "name": "写作 / 图片模型", "provider": provider, "text_model": model,
                  "text_configured": text_model_configured(), "image_configured": image_configured,
                  "message": f"当前写作模型：{provider} {model}；临时故障会明确标记备用模型，不伪装成首选模型",
                  "env": ["RIGHTCODE_API_KEY", "RIGHTCODE_TEXT_BASE_URL", "RIGHTCODE_TEXT_MODEL", "RIGHTCODE_TEXT_TIMEOUT", "RIGHTCODE_JSON_MODE", "YUZAPI_API_KEY", "YUZAPI_BASE_URL", "YUZAPI_MODEL", "YUZAPI_JSON_MODE", "YUZAPI_OMIT_TEMPERATURE", "YUZAPI_FALLBACK_ENABLED", "TEXT_MODEL_TIMEOUT", "TEXT_MODEL_FALLBACK_TIMEOUT", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "OPENAI_IMAGE_API_KEY", "OPENAI_IMAGE_BASE_URL", "OPENAI_IMAGE_MODEL", "RIGHTCODE_IMAGE_API_KEY", "RIGHTCODE_IMAGE_BASE_URL", "RIGHTCODE_TASK_BASE_URL", "RIGHTCODE_IMAGE_MODEL", "RIGHTCODE_IMAGE_SIZE"]},
        "wechat": {"ok": WECHAT.configured, "configured": WECHAT.configured, "name": "微信公众号",
                   "message": "只创建草稿，不执行群发" if WECHAT.configured else "本地创作可用；配置后才能写入公众号草稿箱",
                   "env": ["WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_AUTHOR"],
                   "note": "公众号后台还需要把本机出口 IP 加入白名单"},
        "style": {"ok": bool(STYLE_CONTEXT.get("skill_path")), "name": "数字生命卡兹克",
                  "samples": len(STYLE_CONTEXT.get("samples", [])), "preferences_configured": bool(style_preferences())},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatContentWorkbench/1.0"

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("[workbench] " + format % args + "\n")

    def send_json(self, data: object, status: int = 200) -> None:
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, data: bytes, filename: str, content_type: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 10 * 1024 * 1024:
            raise ValueError("请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == "/":
                return self.send_file(ROOT / "index.html", "text/html; charset=utf-8")
            if path == "/api/dashboard":
                return self.send_json(dashboard())
            if path == "/api/hot":
                with db_conn() as conn:
                    rows = conn.execute("SELECT * FROM source_item ORDER BY published_at DESC, id DESC LIMIT 80").fetchall()
                return self.send_json([row_to_json(row) for row in rows])
            if path == "/api/topics":
                return self.send_json(get_topics())
            auto_image_job_match = re.match(r"^/api/drafts/(\d+)/auto-images/([0-9a-f-]+)$", path)
            if auto_image_job_match:
                draft_id, job_id = int(auto_image_job_match.group(1)), auto_image_job_match.group(2)
                with AUTO_IMAGE_JOBS_LOCK:
                    job = dict(AUTO_IMAGE_JOBS.get(job_id, {}))
                if not job or int(job.get("draft_id", 0)) != draft_id:
                    return self.send_json({"error": "自动配图任务不存在或已过期"}, 404)
                return self.send_json({"ok": True, **job})
            if path == "/api/drafts":
                return self.send_json(get_drafts())
            if path == "/api/assets":
                return self.send_json(table_rows("asset", 120))
            if path == "/api/metrics":
                with db_conn() as conn:
                    rows = conn.execute("SELECT * FROM metric_daily WHERE source='wechat_api' ORDER BY metric_date DESC, id DESC LIMIT 500").fetchall()
                return self.send_json([row_to_json(row) for row in rows])
            if path == "/api/metrics/summary":
                days = int((params.get("days") or ["7"])[0])
                return self.send_json(metric_summary(days))
            if path == "/api/published-articles":
                return self.send_json(published_articles())
            if path == "/api/style":
                with db_conn() as conn:
                    profile = conn.execute("SELECT * FROM style_profile WHERE name=?", ("数字生命卡兹克",)).fetchone()
                result = dict(profile) if profile else {}
                result.update({"name": "数字生命卡兹克", "skill_path": STYLE_CONTEXT.get("skill_path", ""),
                               "sample_count": len(STYLE_CONTEXT.get("samples", [])), "rules_loaded": bool(STYLE_CONTEXT.get("skill_excerpt"))})
                result["sample_names"] = safe_json_load(result.get("sample_names"), [])
                return self.send_json(result)
            if path == "/api/status":
                return self.send_json(integration_status())
            if path == "/api/wechat/drafts":
                return self.send_json(WECHAT.get_drafts())
            draft_markdown = re.match(r"^/api/drafts/(\d+)/markdown$", path)
            if draft_markdown:
                draft_id = int(draft_markdown.group(1))
                with db_conn() as conn:
                    draft = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
                if not draft:
                    return self.send_json({"error": "草稿不存在"}, 404)
                title = draft["title"] or "未命名文章"
                body = draft["body"] or ""
                data = f"# {title}\n\n{body}\n".encode("utf-8")
                return self.send_download(data, f"draft-{draft_id}.md", "text/markdown")
            if path == "/api/export":
                return self.send_download(json_bytes(backup_payload()), f"wechat-content-backup-{datetime.now().date().isoformat()}.json")
            if path == "/api/export/package":
                return self.send_download(backup_package(), f"wechat-content-workbench-{datetime.now().date().isoformat()}.zip", "application/zip")
            if path == "/media":
                value = (params.get("path") or [""])[0]
                return self.send_file(registered_asset_path(value))
            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            return self.send_json({"error": redact_sensitive(exc)}, 400)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/import":
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0 or length > 150 * 1024 * 1024:
                    raise ValueError("备份文件为空或超过 150 MB")
                raw = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                archived_files: dict[str, bytes] = {}
                if "zip" in content_type or self.headers.get("X-Backup-Filename", "").lower().endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
                        if "backup.json" not in bundle.namelist():
                            raise ValueError("压缩包缺少 backup.json")
                        backup = json.loads(bundle.read("backup.json").decode("utf-8"))
                        for name in bundle.namelist():
                            if name.startswith("assets/") and not name.endswith("/"):
                                archived_files[name] = bundle.read(name)
                else:
                    backup = json.loads(raw.decode("utf-8"))
                return self.send_json(restore_backup(backup, archived_files))
            body = self.read_body()
            if path == "/api/hot/sync":
                return self.send_json(sync_aihot(body.get("window", "24h"), body.get("category", "")))
            if path == "/api/style":
                preferences = str(body.get("preferences", "")).strip()
                if len(preferences) > 6000:
                    raise ValueError("个人风格偏好不能超过 6000 字")
                with db_conn() as conn:
                    conn.execute("UPDATE style_profile SET preferences=?,updated_at=? WHERE name=?", (preferences, now_iso(), "数字生命卡兹克"))
                return self.send_json({"ok": True, "message": "个人风格偏好已保存", "preferences": preferences})
            if path == "/api/topics":
                title = str(body.get("title", "")).strip()
                if not title:
                    raise ValueError("选题标题不能为空")
                core_angle = str(body.get("core_angle", "")).strip()
                if len(core_angle) < 8:
                    raise ValueError("必须先写下至少 8 个字的人工核心判断，不能只保存热点标题")
                source_id = body.get("source_id")
                if source_id:
                    with db_conn() as conn:
                        if not conn.execute("SELECT 1 FROM source_item WHERE id=?", (int(source_id),)).fetchone():
                            raise ValueError("关联的热点不存在，请重新选择")
                timestamp = now_iso()
                with db_conn() as conn:
                    cursor = conn.execute("INSERT INTO topic(source_id,title,core_angle,audience,personal_observation,lived_experience,emotional_note,h_score,k_score,r_score,window,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                         (source_id, title, core_angle, body.get("audience", ""), body.get("personal_observation", ""), body.get("lived_experience", ""), body.get("emotional_note", ""),
                                          int(body.get("h_score", 0) or 0), int(body.get("k_score", 0) or 0), int(body.get("r_score", 0) or 0), body.get("window", "7d"), body.get("status", "待判断"), timestamp, timestamp))
                    topic_id = cursor.lastrowid
                    draft_cursor = conn.execute("INSERT INTO draft(topic_id,length_preset,status,created_at,updated_at) VALUES(?,?,?,?,?)", (topic_id, "standard", "写作中", timestamp, timestamp))
                return self.send_json({"ok": True, "topic_id": topic_id, "draft_id": draft_cursor.lastrowid})
            if path == "/api/drafts/blank":
                timestamp = now_iso()
                with db_conn() as conn:
                    cursor = conn.execute("INSERT INTO draft(topic_id,title,digest,body,length_preset,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                         (None, "", "", "", "standard", "写作中", timestamp, timestamp))
                    row = conn.execute("SELECT * FROM draft WHERE id=?", (cursor.lastrowid,)).fetchone()
                return self.send_json({"ok": True, "draft_id": cursor.lastrowid, "draft": row_to_json(row)})
            topic_match = re.match(r"^/api/topics/(\d+)/draft$", path)
            if topic_match:
                topic_id = int(topic_match.group(1))
                with db_conn() as conn:
                    cursor = conn.execute("INSERT INTO draft(topic_id,length_preset,status,created_at,updated_at) VALUES(?,?,?,?,?)", (topic_id, "standard", "写作中", now_iso(), now_iso()))
                return self.send_json({"ok": True, "draft_id": cursor.lastrowid})
            auto_images_match = re.match(r"^/api/drafts/(\d+)/auto-images$", path)
            if auto_images_match:
                draft_id = int(auto_images_match.group(1))
                with db_conn() as conn:
                    if not conn.execute("SELECT 1 FROM draft WHERE id=?", (draft_id,)).fetchone():
                        raise ValueError("草稿不存在")
                with AUTO_IMAGE_JOBS_LOCK:
                    active = next((dict(value, job_id=key) for key, value in AUTO_IMAGE_JOBS.items()
                                   if int(value.get("draft_id", 0)) == draft_id and value.get("status") == "running"), None)
                    if active:
                        return self.send_json({"ok": True, **active}, 202)
                    job_id = uuid.uuid4().hex
                    AUTO_IMAGE_JOBS[job_id] = {"draft_id": draft_id, "status": "running", "message": "任务已开始，正在准备配图…"}
                threading.Thread(target=run_auto_image_job, args=(job_id, draft_id, str(body.get("body", "")), str(body.get("title", ""))), daemon=True).start()
                return self.send_json({"ok": True, "job_id": job_id, "draft_id": draft_id, "status": "running", "message": "任务已开始，正在读取原文图片…"}, 202)
            draft_match = re.match(r"^/api/drafts/(\d+)/(generate|quality|save|readability)$", path)
            if draft_match:
                draft_id, action = int(draft_match.group(1)), draft_match.group(2)
                if action == "generate":
                    return self.send_json(generate_draft(draft_id, body.get("length_preset")))
                if action == "readability":
                    with db_conn() as conn:
                        draft_for_layout = conn.execute("SELECT title,body,digest FROM draft WHERE id=?", (draft_id,)).fetchone()
                    if not draft_for_layout:
                        raise ValueError("草稿不存在")
                    layout = readability_markup(str(body.get("body", "")) or draft_for_layout["body"] or "", str(body.get("title", "")) or draft_for_layout["title"] or "")
                    with db_conn() as conn:
                        digest = markdown_text_only(draft_for_layout["digest"] or "")[:120] or markdown_text_only(layout["body"])[:120]
                        conn.execute("UPDATE draft SET body=?,digest=?,status=?,updated_at=? WHERE id=?", (layout["body"], digest, "待排版", now_iso(), draft_id))
                        updated = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
                    layout["draft"] = row_to_json(updated)
                    return self.send_json(layout)
                if action == "quality":
                    with db_conn() as conn:
                        draft_for_quality = conn.execute("SELECT evidence,length_preset FROM draft WHERE id=?", (draft_id,)).fetchone()
                    evidence = safe_json_load(draft_for_quality["evidence"] if draft_for_quality else "[]", [])
                    preset = normalize_length_preset(body.get("length_preset") or (draft_for_quality["length_preset"] if draft_for_quality else "standard"))
                    result = quality_check(body.get("body", ""), evidence if isinstance(evidence, list) else [], preset)
                    with db_conn() as conn:
                        conn.execute("UPDATE draft SET quality_report=?,status=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), "待排版" if result["passed"] else "待审稿", now_iso(), draft_id))
                    return self.send_json(result)
                with db_conn() as conn:
                    preset = normalize_length_preset(body.get("length_preset", "standard"))
                    clean_digest = markdown_text_only(str(body.get("digest", "")))[:120] or markdown_text_only(str(body.get("body", "")))[:120]
                    conn.execute("UPDATE draft SET title=?,digest=?,body=?,length_preset=?,status=?,cover_asset_id=?,updated_at=? WHERE id=?", (body.get("title", ""), clean_digest, body.get("body", ""), preset, body.get("status", "写作中"), body.get("cover_asset_id") or None, now_iso(), draft_id))
                return self.send_json({"ok": True})
            if path == "/api/assets/import-url":
                return self.send_json(import_images_from_url(str(body.get("url", "")).strip()))
            if path == "/api/assets/prompt-from-selection":
                return self.send_json(image_prompt_from_selection(str(body.get("selected_text", "")), str(body.get("title", "")), str(body.get("context", ""))))
            if path == "/api/assets/generate-from-prompt":
                prompt = str(body.get("prompt", "")).strip()
                if not prompt:
                    raise ValueError("图片提示词为空")
                if str(body.get("visual_mode", "")).strip() == "info_card":
                    selected_text = str(body.get("selected_text", "")).strip()
                    if selected_text:
                        result = generate_local_info_card(selected_text, str(body.get("usage", "正文信息卡")))
                        result["image_prompt"] = prompt
                        return self.send_json(result)
                result = generate_image_asset(prompt, str(body.get("usage", "正文插图")))
                result["image_prompt"] = prompt
                return self.send_json(result)
            if path == "/api/assets/generate-image":
                return self.send_json(generate_image_asset(str(body.get("prompt", "")), str(body.get("usage", "原创配图"))))
            if path == "/api/assets":
                rel_path = str(body.get("path", "")).strip()
                asset_path = safe_relative_path(rel_path)
                if not asset_path.exists() or not asset_path.is_file():
                    raise ValueError("素材文件不存在")
                guessed_type = mimetypes.guess_type(asset_path.name)[0] or ""
                if body.get("kind", "image") == "image" and not guessed_type.startswith("image/"):
                    raise ValueError("只允许登记图片素材")
                with db_conn() as conn:
                    source_url = str(body.get("source_url", "")).strip()
                    cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,source_page_url,source_kind,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                         (Path(rel_path).name, rel_path, body.get("kind", "image"), source_url, str(body.get("source_page_url", "")).strip(),
                                          "source" if source_url else "local", body.get("rights_note", "待人工确认"), body.get("prompt", ""), body.get("usage", ""), now_iso()))
                return self.send_json({"ok": True, "id": cursor.lastrowid})
            if path == "/api/wechat/preview":
                return self.send_json({"ok": True, "html": preview_wechat_content(
                    str(body.get("body", "")), body.get("cover_asset_id"))})
            if path == "/api/wechat/test":
                return self.send_json(WECHAT.test())
            if path == "/api/published-articles":
                return self.send_json(register_published_article(body))
            if path == "/api/metrics/manual":
                return self.send_json(save_manual_metrics(body))
            publish_match = re.match(r"^/api/drafts/(\d+)/publish$", path)
            if publish_match:
                draft_id = int(publish_match.group(1))
                if not body.get("authorized"):
                    raise ValueError("需要明确授权创建公众号草稿")
                if not WECHAT.configured:
                    raise ValueError("未配置 WECHAT_APP_ID / WECHAT_APP_SECRET；当前只可保存本地草稿，不能假装已写入公众号")
                with db_conn() as conn:
                    draft = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
                    if not draft:
                        raise ValueError("草稿不存在")
                    existing = conn.execute("SELECT * FROM publish_job WHERE draft_id=? AND status='草稿已创建' ORDER BY id DESC LIMIT 1", (draft_id,)).fetchone()
                    if existing:
                        return self.send_json({"ok": True, "idempotent": True, "job": row_to_json(existing)})
                    failed = conn.execute("SELECT * FROM publish_job WHERE draft_id=? AND status IN ('处理中','失败') ORDER BY id DESC LIMIT 1", (draft_id,)).fetchone()
                    if failed and not body.get("retry"):
                        return self.send_json({"ok": False, "idempotent": True, "error": "该草稿已有未完成或失败的公众号任务，未重复创建；如需重试请明确传入 retry", "job": row_to_json(failed)}, 409)
                    cursor = conn.execute("INSERT INTO publish_job(draft_id,status,message,created_at,updated_at) VALUES(?,?,?,?,?)", (draft_id, "处理中", "", now_iso(), now_iso()))
                    job_id = cursor.lastrowid
                try:
                    cover_media = ""
                    cover_asset_path = None
                    with db_conn() as conn:
                        asset = conn.execute("SELECT * FROM asset WHERE id=?", (draft["cover_asset_id"],)).fetchone() if draft["cover_asset_id"] else conn.execute("SELECT * FROM asset WHERE kind='image' ORDER BY CASE WHEN name LIKE '%cover%' OR name LIKE '%封面%' OR name LIKE '01%' THEN 0 ELSE 1 END, id LIMIT 1").fetchone()
                    if asset:
                        cover_asset_path = safe_relative_path(asset["path"])
                        cover_media = WECHAT.upload_cover(cover_asset_path)
                    content = prepare_wechat_content(draft["body"], cover_asset_path)
                    result = WECHAT.add_draft(draft["title"], draft["digest"], content, cover_media)
                    with db_conn() as conn:
                        conn.execute("UPDATE publish_job SET media_id=?,status=?,message=?,updated_at=? WHERE id=?", (result.get("media_id", ""), "草稿已创建", "已写入公众号草稿箱", now_iso(), job_id))
                        conn.execute("UPDATE draft SET status=?,updated_at=? WHERE id=?", ("草稿已创建", now_iso(), draft_id))
                        job = conn.execute("SELECT * FROM publish_job WHERE id=?", (job_id,)).fetchone()
                    return self.send_json({"ok": True, "job": row_to_json(job)})
                except Exception as exc:
                    with db_conn() as conn:
                        conn.execute("UPDATE publish_job SET status=?,message=?,updated_at=? WHERE id=?", ("失败", redact_sensitive(exc), now_iso(), job_id))
                    raise
            if path == "/api/metrics/sync":
                return self.send_json(sync_wechat_metrics(body.get("days", 7)))
            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            return self.send_json({"ok": False, "error": redact_sensitive(exc)}, 400)


def main() -> None:
    init_db()
    seed_style_profile()
    seed_local_sources()
    seed_assets()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"WeChat Content Production Workbench running at http://{HOST}:{PORT}")
    print(f"Local data: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
