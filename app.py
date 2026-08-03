#!/usr/bin/env python3
"""Local editorial workbench for a single WeChat public account.

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
USER_AGENT = "EditorialWorkbench/1.0 (+local)"
AUTO_IMAGE_JOBS: dict[str, dict] = {}
AUTO_IMAGE_JOBS_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def redact_sensitive(value: object) -> str:
    message = str(value)
    for env_name in ("OPENAI_API_KEY", "RIGHTCODE_API_KEY", "RIGHT_CODE_API_KEY", "WECHAT_APP_SECRET", "WECHAT_APP_ID"):
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
        for column, definition in (("title_candidates", "TEXT DEFAULT '[]'"), ("claims", "TEXT DEFAULT '[]'"), ("style_profile_id", "INTEGER")):
            if column not in draft_columns:
                conn.execute(f"ALTER TABLE draft ADD COLUMN {column} {definition}")


def table_rows(table: str, limit: int = 100) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


BACKUP_TABLES = ("source_item", "topic", "draft", "asset", "publish_job", "metric_record", "style_profile")


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
        return 0, {"error": redact_sensitive(exc)}, {}


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


def read_text_model(prompt: str, system: str = "") -> tuple[bool, str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return False, "", "未配置 OPENAI_API_KEY，已使用本地协作模板"
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "temperature": 0.7}
    if "deepseek.com" in base:
        payload["response_format"] = {"type": "json_object"}
    status, data, _ = http_json(base + "/chat/completions", method="POST", payload=payload, headers={"Authorization": "Bearer " + api_key}, timeout=90)
    if status and data.get("choices"):
        content = data["choices"][0].get("message", {}).get("content", "")
        return True, content, ""
    return False, "", data.get("error", {}).get("message", "模型请求失败") if isinstance(data.get("error"), dict) else str(data.get("error", "模型请求失败"))


def local_draft(topic: dict, source: dict | None) -> dict:
    title = topic.get("title") or (source or {}).get("title") or "还没想好标题的文章"
    angle = topic.get("core_angle") or "先把这件具体的事讲清楚，再看看它为什么值得我们多想一会儿。"
    observation = topic.get("personal_observation") or "我还没有把自己的观察写进去，这一栏需要在定稿前补上。"
    lived = topic.get("lived_experience") or "这里应该放真实经历，暂时不替作者编造。"
    source_title = (source or {}).get("title") or "已选热点"
    source_summary = (source or {}).get("summary") or "暂无摘要，发布前需要回到原文核验。"
    body = "\n\n".join([
        "## 先把发生了什么说清楚",
        f"这两天，我一直在看「{source_title}」。真正让我停下来，不是它看起来有多热闹，而是它刚好碰到了一个大家都在经历的问题。先把目前能确认的部分放在这里。{source_summary}",
        "热点最容易让人误判的地方，是它会把很多不同的东西压缩成一个标题。标题看上去像结论，点进去以后才发现，里面其实混着事实、猜测、情绪，还有每个人自己投射进去的期待。",
        "## 真正让我停下来的，不是热闹",
        f"我自己的判断是，{angle}这句话现在还不一定完整，但它至少解释了我为什么愿意花时间把这件事写下来。对我来说，值得写的从来不只是发生了什么，而是它为什么会让一个普通人产生反应。",
        f"目前我能确认的个人观察是，{observation}。如果这篇文章最后要变得更具体，这里还需要补进一个真实场景，哪怕只是一次操作、一次犹豫，或者一个当时没有想明白的细节。",
        f"亲自经历的部分先不替作者补造。现在能留下的只有这一句，{lived}。等真正发布之前，这里应该换成自己的现场，而不是一段看起来很顺、其实没有发生过的故事。",
        "## 把它放回普通人的日常里",
        "很多技术和产品刚出现的时候，讨论都会先围绕能力展开。它能做什么，它比过去快多少，它是不是又把某个行业往前推了一步。但真正决定一件事能不能留下来的，往往是它进入日常以后，普通人会不会因此少做一件麻烦事，或者多承担一种新的麻烦。",
        "如果一个工具只在演示里显得厉害，离开演示就需要很多额外解释，它带来的可能只是短暂的新鲜感。相反，那些真正改变习惯的东西，通常不会一直提醒你自己有多先进，它只是悄悄缩短了一个步骤，或者让一个原本不愿意做的人开始愿意试一次。",
        "我们也可以换一个更实际的角度来观察它。不要先问它能不能替代谁，而是问它有没有让一个人更容易完成原本想做、却总是拖着没做的事。如果答案只是让结果看起来更快，却没有减少判断、返工和确认的成本，那它的价值可能还停留在展示阶段。",
        "真正进入生活的工具，往往会留下非常具体的痕迹。有人开始改变自己的工作顺序，有人把原来需要反复沟通的事情提前说清楚，也有人发现自己不再需要记住那么多零散规则。这些变化不一定会登上热搜，却比一场漂亮的演示更能说明问题。",
        "这也是我觉得这件事值得继续观察的原因。现在还不能急着把它归类成成功或失败，更适合先看它会不会被真实的人留下来，尤其是那些没有时间研究规则、也不想承担太多学习成本的人。",
        "## 现在还不能急着下结论",
        "目前比较明确的是来源材料里写到的事实，其他部分都应该标成判断或推断。事实需要回到原文核验，判断属于作者自己的角度，推断则只是根据现有信息往前走了一步。把这三件事分开，文章反而会更可信。",
        "我不太喜欢把一个刚发生的热点直接写成趋势，也不想为了让文章显得有力量，就替读者把答案提前说完。很多事情的真实影响，需要经过一段时间才会显形。今天看起来很重要的东西，可能只是一个短暂的噪音；今天看起来不起眼的变化，反而可能慢慢改掉我们的习惯。",
        "## 我更愿意保留的判断",
        f"所以回到最开始的那句话，{angle}我愿意暂时保留这个判断，但不把它包装成最终答案。它更像一个观察的起点，提醒我继续看三件事。第一，谁会最早真正使用它。第二，使用过程中最麻烦的地方在哪里。第三，它有没有让原本不在场的人也获得一点好处。",
        "写到这里，文章其实还没有结束。一个好的选题不是把所有问题都解决，而是让读者离开的时候，手里多了一个可以继续验证的问题。这个问题不需要很宏大，最好和自己的工作、学习或生活直接相关。",
        "对读者来说，最有用的也许不是记住一个新名词，而是知道下一次遇到类似消息时该怎么做。先找到原始来源，再把已经确认的部分和自己的感受分开，最后只对自己真正看见的东西下判断。这个动作看起来慢一点，却能避免被热点牵着走。",
        "写公众号也一样。文章不需要假装自己已经知道所有答案，但需要把为什么这样想交代清楚。只要读者能顺着你的证据和判断走完一遍，即使最后不同意你的结论，也会知道分歧究竟发生在哪里。",
        "## 留一个问题",
        "下一次我们再看到类似热点时，也许可以先别急着转发结论。多问一句，它到底改变了谁的日常，又把什么新的成本交给了谁。等这个问题有了更具体的答案，这篇文章才算真正写完。",
        "以上内容里，来源事实、作者判断和待补经历已经分开标记。发布前请回到原文核验事实，并把真实经历补回文章。"
    ])
    outline = ["把发生了什么说清楚", "热点为什么值得停下来", "放回普通人的日常", "事实、判断与推断", "作者暂时保留的判断", "留下一个可验证的问题"]
    titles = [title, f"{title}，我更在意它背后的那件事", f"看到这个热点后，我想先聊聊普通人的感受"]
    source_url = (source or {}).get("source_url") or (source or {}).get("aihot_url") or ""
    return {"title": titles[0], "title_candidates": titles, "digest": angle[:120], "body": body, "outline": outline,
            "evidence": [{"type": "source", "label": source_title, "url": source_url, "note": source_summary}],
            "claims": [{"kind": "fact", "text": source_summary, "source": source_url},
                       {"kind": "judgement", "text": angle, "source": "作者核心判断"},
                       {"kind": "inference", "text": "这件事可能会改变普通人的日常工作方式，发布前需人工核验。", "source": "作者推断"}],
            "mode": "local_template"}


def local_readability_markup(markdown: str) -> str:
    blocks = [block for block in re.split(r"(\n\s*\n)", markdown or "")]
    for index, block in enumerate(blocks):
        clean = block.strip()
        if not clean or clean.startswith(("#", ">", "!", "- ", "* ")) or re.match(r"^\d+[.)] ", clean):
            continue
        if "**" in clean or "==" in clean or "__" in clean or "^^" in clean:
            continue
        marked = re.sub(r"([「“][^」”]{2,24}[」”])", r"**\1**", clean, count=1)
        if marked == clean:
            marked = re.sub(r"(真正重要的是|关键在于|我更在意的是|所以，)([^。！？]{4,22})", r"\1**\2**", clean, count=1)
        if marked != clean:
            blocks[index] = block.replace(clean, marked, 1)
    return "".join(blocks)


def readability_markup(markdown: str, title: str = "") -> dict:
    prompt = f"""你是公众号排版编辑。只给下面这篇 Markdown 正文增加少量可读性标记，不得改写、删减、补造或调换任何文字。
允许的标记只有：**重点加粗**、==重点高亮==、__重点下划线__、^^朱砂强调色^^。
每 300 到 500 字最多标记 1 到 2 处，优先标记结论、转折、关键判断或读者需要记住的短句，不要整段加粗，不要给标题加标记。
输出 JSON，只有一个字段 body。

文章标题：{title}
正文：
{markdown}
"""
    ok, text, error = read_text_model(prompt, "你是一名克制的公众号排版编辑，只做信息层级，不改变作者原文。")
    if ok:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                result = json.loads(match.group(0))
                body = str(result.get("body", "")).strip()
                if body and len(markdown_text_only(body)) == len(markdown_text_only(markdown)):
                    return {"body": body, "mode": "model", "message": "已整理重点层级，原文内容未改动"}
            except json.JSONDecodeError:
                pass
    return {"body": local_readability_markup(markdown), "mode": "local_fallback", "message": error or "已用本地规则整理少量重点"}


def generate_draft(draft_id: int) -> dict:
    with db_conn() as conn:
        draft_row = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if not draft_row:
            raise ValueError("草稿不存在")
        topic_row = conn.execute("SELECT * FROM topic WHERE id=?", (draft_row["topic_id"],)).fetchone() if draft_row["topic_id"] else None
        source_row = conn.execute("SELECT * FROM source_item WHERE id=?", (topic_row["source_id"],)).fetchone() if topic_row and topic_row["source_id"] else None
    topic = row_to_json(topic_row) or {}
    source = row_to_json(source_row)
    prompt = f"""你是一个公众号编辑协作者。请基于以下资料生成一篇可编辑的中文公众号长文草稿。
不要编造作者经历、数字、引语或事实。所有个人经历必须保留为待补位置。
风格参考是「有见识的普通人在认真聊一件打动他的事」，短段落，口语化，具体切入，避免模板化总结。
正文目标为 1800 到 2600 个中文字符，至少 10 个自然段，并使用 4 到 6 个 Markdown 二级标题组织阅读节奏。不要用项目符号把正文堆成提纲，每个段落都要有完整意思。资料不足时写清楚待核验或待补位置，不要用泛泛的励志话填充。为了提高可读性，可以少量使用 **重点加粗**、==重点高亮==、__重点下划线__ 或 ^^朱砂强调色^^，每 300 到 500 字最多标记 1 到 2 处，不要整段加粗。
输出 JSON，字段为 title_candidates（3个标题）、title、digest、outline（数组）、body、evidence（数组）、claims（数组）。claims 中明确区分 kind= fact / judgement / inference，并为 fact 填写 source。

选题：{json.dumps(topic, ensure_ascii=False)}
热点资料：{json.dumps(source or {}, ensure_ascii=False)}
作者观察：{topic.get('personal_observation','')}
真实经历：{topic.get('lived_experience','')}
情绪节点：{topic.get('emotional_note','')}
写作规范摘要：{STYLE_CONTEXT.get('skill_excerpt','')[:7000]}
作者个人补充偏好：{style_preferences()}
"""
    ok, text, error = read_text_model(prompt, "你是一名编辑部里的写作协作者，不是自动发稿机器人。")
    result = None
    if ok:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError:
                result = None
    if not result:
        result = local_draft(topic, source)
        result["model_note"] = error or "已使用本地协作模板"
    candidates = result.get("title_candidates")
    if isinstance(candidates, str):
        candidates = [candidates]
    result["title_candidates"] = candidates if isinstance(candidates, list) and candidates else [result.get("title", "")]
    if not isinstance(result.get("claims"), list) or not result.get("claims"):
        result["claims"] = local_draft(topic, source).get("claims", [])
    with db_conn() as conn:
        conn.execute("UPDATE draft SET title=?,digest=?,body=?,outline=?,evidence=?,title_candidates=?,claims=?,style_profile_id=?,status=?,updated_at=? WHERE id=?",
                     (result.get("title", ""), result.get("digest", ""), result.get("body", ""), json.dumps(result.get("outline", []), ensure_ascii=False),
                      json.dumps(result.get("evidence", []), ensure_ascii=False), json.dumps(result.get("title_candidates", []), ensure_ascii=False),
                      json.dumps(result.get("claims", []), ensure_ascii=False), current_style_profile_id(), "待审稿", now_iso(), draft_id))
        row = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
    result["draft"] = row_to_json(row)
    return result


FORBIDDEN_WORDS = ["说白了", "意味着什么", "这意味着", "本质上", "换句话说", "不可否认", "综上所述", "总的来说", "值得注意的是", "不难发现", "让我们来看看", "接下来让我们"]


def quality_check(body: str, evidence: list | None = None) -> dict:
    evidence = evidence or []
    hits = {word: body.count(word) for word in FORBIDDEN_WORDS if word in body}
    punctuation = {mark: body.count(mark) for mark in ["：", "——", '"', "“", "”"] if mark in body}
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    spoken = [x for x in ["我觉得", "我自己", "说真的", "其实吧", "你想想看", "这块需要注意一下", "我还在摸索", "太离谱了", "？？？"] if x in body]
    has_personal = any(x in body for x in ["我自己", "我当时", "我的经历", "我还没有", "待补"])
    placeholders = [x for x in ["待补", "这里应该放", "暂无摘要", "还没有把自己的观察写进去"] if x in body]
    has_specific_detail = bool(re.search(r"\d|「[^」]+」|“[^”]+”|当时|昨天|今天|这两天|具体", body))
    has_evidence = bool(evidence)
    checks = {
        "硬性规则": not hits and not punctuation,
        "开头具体": bool(paragraphs) and len(paragraphs[0]) < 160,
        "风格一致性": len(spoken) >= 2 and all(len(p) <= 420 for p in paragraphs),
        "人工输入": has_personal and not placeholders,
        "内容支撑": len(paragraphs) >= 4 and (has_specific_detail or has_evidence),
    }
    passed = sum(bool(v) for v in checks.values())
    passed_ok = passed >= 4 and checks["硬性规则"] and checks["内容支撑"] and checks["人工输入"]
    next_actions = []
    if not has_personal or placeholders:
        next_actions.append("补充真实经历或现场细节，并移除待补占位")
    if hits or punctuation:
        next_actions.append("把命中的套话或禁用标点改成具体表达")
    if not has_evidence:
        next_actions.append("补充来源链接或在文中明确标记待核验事实")
    return {"passed": passed_ok, "score": f"{passed}/5", "forbidden_words": hits, "punctuation": punctuation,
            "spoken_markers": spoken, "placeholders": placeholders, "specific_detail": has_specific_detail,
            "evidence_count": len(evidence), "checks": checks, "layers": {
                "硬性禁用词": not hits and not punctuation, "风格一致性": checks["风格一致性"],
                "内容支撑": checks["内容支撑"], "活人感终审": checks["人工输入"] and checks["开头具体"]},
            "next_actions": next_actions}


class ImageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.images: list[str] = []
        self.og_image = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta" and attrs_dict.get("property", "").lower() in {"og:image", "twitter:image"}:
            self.og_image = urljoin(self.base_url, attrs_dict.get("content", ""))
        if tag.lower() == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or attrs_dict.get("data-original")
            if src:
                self.images.append(urljoin(self.base_url, src))


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
    if content_type.startswith("image/"):
        candidates = [page_url]
    else:
        text = page_bytes.decode("utf-8", errors="ignore")
        parser = ImageParser(page_url)
        parser.feed(text)
        candidates = ([parser.og_image] if parser.og_image else []) + parser.images
    unique = []
    for candidate in candidates:
        if candidate and candidate not in unique and urlparse(candidate).scheme in {"http", "https"}:
            unique.append(candidate)
    imported = []
    for image_url in unique[:max(1, min(limit, 8))]:
        try:
            data, mime = download_url(image_url)
            if not mime.startswith("image/"):
                continue
            ext = mimetypes.guess_extension(mime) or Path(urlparse(image_url).path).suffix or ".bin"
            if ext == ".jpe":
                ext = ".jpg"
            filename = f"{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}"
            output = ASSET_DIR / filename
            output.write_bytes(data)
            with db_conn() as conn:
                cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                     (filename, str(output.relative_to(ROOT)), "image", image_url, "来源已记录，版权待确认", "", "来源图", now_iso()))
                imported.append({"id": cursor.lastrowid, "name": filename, "path": str(output.relative_to(ROOT)), "source_url": image_url, "rights_note": "来源已记录，版权待确认"})
        except Exception:
            continue
    return {"page_url": page_url, "found": len(unique), "imported": imported, "message": f"识别到 {len(unique)} 张图片，导入 {len(imported)} 张"}


def markdown_text_only(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"^#{1,6}\s+", "", value.strip())
    value = re.sub(r"[*_`=~^]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def image_count_in_markdown(body: str) -> int:
    return len(re.findall(r"!\[[^\]]*\]\([^)]*\)", body or ""))


def split_long_image_block(block: str, max_chars: int = 560) -> list[str]:
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
                if not block.startswith("!") and not block.startswith("#") and len(markdown_text_only(block)) >= 55]
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
    blocks, positions, target_count = choose_image_blocks(body)
    existing_count = image_count_in_markdown(body)
    if not positions:
        return {"ok": True, "draft": draft, "target_count": target_count, "inserted": [], "source_imported": [],
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
            existing_source_assets = conn.execute("SELECT * FROM asset WHERE kind='image' AND usage='来源图' ORDER BY id DESC LIMIT 120").fetchall()
        for row in existing_source_assets:
            asset = row_to_json(row) or {}
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
            prompt_result = image_prompt_from_selection(selected, title, context)
            generated_asset = generate_image_asset(prompt_result["prompt"], "正文插图")
            generated_asset["image_prompt"] = prompt_result["prompt"]
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
    message = f"配图规划 {target_count} 张：本次来源图 {source_used} 张，AI 补图 {len(generated)} 张，正文现有 {existing_count} 张"
    if remaining_count:
        message += f"，仍缺 {remaining_count} 张"
    if failures:
        detail = "；".join(str(item) for item in failures[:2])
        message += f"。失败 {len(failures)} 项：{detail}"
    return {"ok": True, "draft": row_to_json(updated), "target_count": target_count, "planned": positions,
            "inserted": inserted, "source_imported": unique_assets, "generated": generated, "failed": failures,
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
    prompt = f"""你是公众号编辑部的视觉编辑。请把下面选中的文章段落转换成一条可直接用于图片生成模型的中文提示词。
不要复述文章，不要生成图片中的文字，不要编造真实人物、品牌标志或新闻现场。画面必须服务于段落中明确出现的事实、对象、动作或环境；如果段落只有抽象判断，就用一个克制、可理解的日常物件或真实空间承载它，不要制造无意义的“科技感”。
禁止机器人、发光网络、漂浮图标、随机仪表盘、通用蓝紫渐变、无关人物、无关城市天际线和装饰性 3D 图标。
输出 JSON，字段为 prompt、visual_intent、avoid。prompt 只写最终生图提示词，包含主体、场景、构图、镜头或光线、色彩和编辑视觉风格，并明确“画面内不要出现文字、水印、Logo”。

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
                generated = str(result.get("prompt", "")).strip()
                if generated:
                    return {"prompt": generated, "visual_intent": str(result.get("visual_intent", "")),
                            "avoid": str(result.get("avoid", "")), "mode": "model"}
            except json.JSONDecodeError:
                pass
    fallback = (f"为公众号正文制作一张克制的纪实编辑配图，准确围绕段落中的具体内容“{selected_text[:180]}”取一个可视化瞬间；"
                "优先使用真实日常物件、工作台、纸张、屏幕、手部动作或室内环境，不添加段落没有提到的人物和符号；"
                "自然光，低饱和米白、墨黑、少量朱砂红，平面摄影或杂志纪实摄影，构图清楚，留白适中，横向 4:3。"
                "画面内不要出现文字、水印、Logo、机器人、发光网络、漂浮图标、蓝紫渐变和通用科技背景。")
    return {"prompt": fallback, "visual_intent": "把段落中的具体对象或动作转成纪实配图", "avoid": "文字、水印、Logo、机器人、发光网络、漂浮图标、通用科技背景", "mode": "local_fallback", "model_note": error or "未配置文本模型，使用本地提示词转换"}


def save_generated_image(raw: bytes, prompt: str, usage: str, rights_note: str) -> dict:
    filename = f"generated-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
    output = ASSET_DIR / filename
    output.write_bytes(raw)
    with db_conn() as conn:
        cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             (filename, str(output.relative_to(ROOT)), "image", "", rights_note, prompt, usage, now_iso()))
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
    size = os.getenv("RIGHTCODE_IMAGE_SIZE", "16:9" if "封面" in usage else "4:3")
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
    rightcode_key = os.getenv("RIGHTCODE_API_KEY", "") or os.getenv("RIGHT_CODE_API_KEY", "")
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
    """Convert legacy local SVG cards to PNG because WeChat rejects SVG uploads."""
    if asset_path.suffix.lower() != ".svg":
        return asset_path
    output = asset_path.with_suffix(".png")
    if not output.exists():
        write_editorial_png(output)
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
        cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             (filename, str(output.relative_to(ROOT)), "image", "", "本地原创视觉，未调用外部模型", prompt, usage, now_iso()))
    return {"id": cursor.lastrowid, "name": filename, "path": str(output.relative_to(ROOT)), "prompt": prompt, "usage": usage,
            "mode": "local_editorial", "message": "本地原创配图已保存（未调用外部模型）"}


def md_to_html(markdown: str, image_map: dict[str, str] | None = None) -> str:
    image_map = image_map or {}
    lines = markdown.replace("\r\n", "\n").split("\n")
    output: list[str] = []
    list_tag = ""
    lead_paragraph = True

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
            alt, src = image_match.groups()
            src = image_map.get(src, src)
            output.append(f'<p style="margin:25px 0;text-align:center"><img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}" style="max-width:100%;height:auto;display:block;margin:0 auto;border-radius:8px"></p>')
            lead_paragraph = False
        elif line == "---":
            close_list()
            output.append('<hr style="border:0;border-top:1px solid #d8d0c3;margin:28px 0">')
        elif line.startswith("> "):
            close_list()
            output.append(f'<blockquote style="margin:22px 0;padding:13px 16px;border-left:4px solid #b44735;background:#f4eee4;color:#625b50;line-height:1.9">{inline_html(line[2:], image_map)}</blockquote>')
        elif line.startswith("### "):
            close_list()
            output.append(f'<h3 style="font-size:17px;line-height:1.5;margin:1.8em 0 .7em;color:#4f483e">{inline_html(line[4:], image_map)}</h3>')
            lead_paragraph = True
        elif line.startswith("## "):
            close_list()
            output.append(f'<h2 style="font-size:21px;line-height:1.45;margin:2.1em 0 .8em;padding-left:12px;border-left:4px solid #b44735;color:#1d1c19">{inline_html(line[3:], image_map)}</h2>')
            lead_paragraph = True
        elif line.startswith("# "):
            close_list()
            output.append(f'<h1 style="font-size:28px;line-height:1.35;margin:0 0 1.2em;color:#1d1c19">{inline_html(line[2:], image_map)}</h1>')
            lead_paragraph = True
        elif re.match(r"^[-*] ", line) or re.match(r"^\d+[.)] ", line):
            wanted = "ol" if re.match(r"^\d+[.)] ", line) else "ul"
            if list_tag != wanted:
                close_list()
                list_tag = wanted
                output.append(f'<{list_tag} style="margin:18px 0;padding-left:24px;line-height:1.9">')
            content = re.sub(r"^(?:[-*]|\d+[.)])\s+", "", line)
            output.append(f"<li>{inline_html(content, image_map)}</li>")
        else:
            close_list()
            if lead_paragraph:
                output.append(f'<p style="margin:0 0 1.35em;line-height:2;color:#37332d;font-size:17px;letter-spacing:.01em">{inline_html(line, image_map)}</p>')
                lead_paragraph = False
            else:
                output.append(f'<p style="margin:0 0 1.25em;line-height:1.95;color:#37332d;font-size:16px">{inline_html(line, image_map)}</p>')
    close_list()
    return "".join(output)


def inline_html(value: str, image_map: dict[str, str]) -> str:
    escaped = html.escape(value)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r'<strong>\1</strong>', escaped)
    escaped = re.sub(r"==([^=]+)==", r'<mark style="background:#eff5b7;color:#343b20;padding:0 3px">\1</mark>', escaped)
    escaped = re.sub(r"__([^_]+)__", r'<u style="text-decoration-color:#b44735;text-underline-offset:3px">\1</u>', escaped)
    escaped = re.sub(r"\^\^([^\^]+)\^\^", r'<span style="color:#b44735;font-weight:600">\1</span>', escaped)
    escaped = re.sub(r"`([^`]+)`", r'<code style="background:#eee8dc;padding:2px 5px;border-radius:4px">\1</code>', escaped)
    return escaped


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
            raise RuntimeError(data.get("errmsg", "微信 access_token 获取失败"))
        self.token = data["access_token"]
        self.expires_at = time.time() + int(data.get("expires_in", 7200))
        return self.token

    def api_json(self, endpoint: str, payload: dict | None = None) -> dict:
        token = self.access_token()
        separator = "&" if "?" in endpoint else "?"
        status, data, _ = http_json("https://api.weixin.qq.com" + endpoint + separator + "access_token=" + quote(token), method="POST" if payload is not None else "GET", payload=payload)
        if status == 0 or data.get("errcode"):
            raise RuntimeError(data.get("errmsg", "微信接口请求失败"))
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
            raise RuntimeError(result.get("errmsg", "微信封面上传失败"))
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
            raise RuntimeError(result.get("errmsg", "微信正文图片上传失败"))
        return result["url"]

    def add_draft(self, title: str, digest: str, content: str, cover_media_id: str = "") -> dict:
        article = {"title": title[:64], "author": os.getenv("WECHAT_AUTHOR", ""), "digest": digest[:120], "content": content,
                   "content_source_url": "", "need_open_comment": 1, "only_fans_can_comment": 0, "show_cover_pic": 1}
        if cover_media_id:
            article["thumb_media_id"] = cover_media_id
        return self.api_json("/cgi-bin/draft/add", {"articles": [article]})

    def get_drafts(self) -> dict:
        return self.api_json("/cgi-bin/draft/batchget", {"offset": 0, "count": 20, "no_content": 1})

    def article_summary(self, begin: str, end: str) -> dict:
        return self.api_json("/datacube/getarticlesummary", {"begin_date": begin, "end_date": end})


WECHAT = WeChatClient()


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
    return md_to_html(markdown).replace("<p>", '<p style="margin:0 0 1.1em;line-height:1.9">').replace("<h1>", '<h1 style="font-family:Georgia,serif;font-size:28px">').replace("<h2>", '<h2 style="font-family:Georgia,serif;font-size:20px;margin-top:1.8em">').replace("<h3>", '<h3 style="font-family:Georgia,serif;font-size:16px;margin-top:1.5em">')


def prepare_wechat_content(markdown: str) -> str:
    """Upload local markdown images before building the WeChat HTML body."""
    image_map: dict[str, str] = {}
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
    return md_to_html(markdown, image_map)


def dashboard() -> dict:
    with db_conn() as conn:
        hot_count = conn.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()["count"]
        topic_count = conn.execute("SELECT COUNT(*) AS count FROM topic").fetchone()["count"]
        writing_count = conn.execute("SELECT COUNT(*) AS count FROM draft WHERE status NOT IN ('草稿已创建','已归档')").fetchone()["count"]
        asset_count = conn.execute("SELECT COUNT(*) AS count FROM asset").fetchone()["count"]
        metric_count = conn.execute("SELECT COUNT(*) AS count FROM metric_record").fetchone()["count"]
    model_configured = bool(os.getenv("OPENAI_API_KEY") or os.getenv("RIGHTCODE_API_KEY") or os.getenv("RIGHT_CODE_API_KEY"))
    return {"hot_count": hot_count, "topic_count": topic_count, "writing_count": writing_count, "asset_count": asset_count, "metric_count": metric_count,
            "style": {"name": "数字生命卡兹克", "skill_loaded": bool(STYLE_CONTEXT.get("skill_path")), "sample_count": len(STYLE_CONTEXT.get("samples", []))},
            "integrations": {"aihot": True, "model": model_configured, "wechat": WECHAT.configured}}


def integration_status() -> dict:
    model_configured = bool(os.getenv("OPENAI_API_KEY") or os.getenv("RIGHTCODE_API_KEY") or os.getenv("RIGHT_CODE_API_KEY"))
    return {
        "local": {"ok": True, "host": HOST, "message": "仅绑定本机，凭据不写入内容库"},
        "aihot": {"ok": True, "configured": True, "name": "AI HOT", "message": "公开只读热点接口，可同步并缓存"},
        "model": {"ok": model_configured, "configured": model_configured,
                  "name": "写作 / 图片模型", "message": "支持 DeepSeek/OpenAI 写作与 Right Code 图片模型中转配图；未配置时使用本地模板",
                  "env": ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_IMAGE_API_KEY", "OPENAI_IMAGE_BASE_URL", "OPENAI_IMAGE_MODEL", "RIGHTCODE_API_KEY", "RIGHTCODE_IMAGE_MODEL", "RIGHTCODE_IMAGE_SIZE"]},
        "wechat": {"ok": WECHAT.configured, "configured": WECHAT.configured, "name": "微信公众号",
                   "message": "只创建草稿，不执行群发" if WECHAT.configured else "本地创作可用；配置后才能写入公众号草稿箱",
                   "env": ["WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_AUTHOR"],
                   "note": "公众号后台还需要把本机出口 IP 加入白名单"},
        "style": {"ok": bool(STYLE_CONTEXT.get("skill_path")), "name": "数字生命卡兹克",
                  "samples": len(STYLE_CONTEXT.get("samples", [])), "preferences_configured": bool(style_preferences())},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "EditorialWorkbench/1.0"

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
                    rows = conn.execute("SELECT * FROM metric_record ORDER BY observed_at DESC, id DESC LIMIT 200").fetchall()
                return self.send_json([row_to_json(row) for row in rows])
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
                return self.send_download(json_bytes(backup_payload()), f"editorial-backup-{datetime.now().date().isoformat()}.json")
            if path == "/api/export/package":
                return self.send_download(backup_package(), f"editorial-workbench-{datetime.now().date().isoformat()}.zip", "application/zip")
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
                    draft_cursor = conn.execute("INSERT INTO draft(topic_id,status,created_at,updated_at) VALUES(?,?,?,?)", (topic_id, "写作中", timestamp, timestamp))
                return self.send_json({"ok": True, "topic_id": topic_id, "draft_id": draft_cursor.lastrowid})
            if path == "/api/drafts/blank":
                timestamp = now_iso()
                with db_conn() as conn:
                    cursor = conn.execute("INSERT INTO draft(topic_id,title,digest,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                         (None, "", "", "", "写作中", timestamp, timestamp))
                    row = conn.execute("SELECT * FROM draft WHERE id=?", (cursor.lastrowid,)).fetchone()
                return self.send_json({"ok": True, "draft_id": cursor.lastrowid, "draft": row_to_json(row)})
            topic_match = re.match(r"^/api/topics/(\d+)/draft$", path)
            if topic_match:
                topic_id = int(topic_match.group(1))
                with db_conn() as conn:
                    cursor = conn.execute("INSERT INTO draft(topic_id,status,created_at,updated_at) VALUES(?,?,?,?)", (topic_id, "写作中", now_iso(), now_iso()))
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
                    return self.send_json(generate_draft(draft_id))
                if action == "readability":
                    with db_conn() as conn:
                        draft_for_layout = conn.execute("SELECT title,body FROM draft WHERE id=?", (draft_id,)).fetchone()
                    if not draft_for_layout:
                        raise ValueError("草稿不存在")
                    layout = readability_markup(str(body.get("body", "")) or draft_for_layout["body"] or "", str(body.get("title", "")) or draft_for_layout["title"] or "")
                    with db_conn() as conn:
                        conn.execute("UPDATE draft SET body=?,digest=?,status=?,updated_at=? WHERE id=?", (layout["body"], markdown_text_only(layout["body"])[:120], "待排版", now_iso(), draft_id))
                        updated = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
                    layout["draft"] = row_to_json(updated)
                    return self.send_json(layout)
                if action == "quality":
                    with db_conn() as conn:
                        draft_for_quality = conn.execute("SELECT evidence FROM draft WHERE id=?", (draft_id,)).fetchone()
                    evidence = safe_json_load(draft_for_quality["evidence"] if draft_for_quality else "[]", [])
                    result = quality_check(body.get("body", ""), evidence if isinstance(evidence, list) else [])
                    with db_conn() as conn:
                        conn.execute("UPDATE draft SET quality_report=?,status=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), "待排版" if result["passed"] else "待审稿", now_iso(), draft_id))
                    return self.send_json(result)
                with db_conn() as conn:
                    conn.execute("UPDATE draft SET title=?,digest=?,body=?,status=?,cover_asset_id=?,updated_at=? WHERE id=?", (body.get("title", ""), body.get("digest", ""), body.get("body", ""), body.get("status", "写作中"), body.get("cover_asset_id") or None, now_iso(), draft_id))
                return self.send_json({"ok": True})
            if path == "/api/assets/import-url":
                return self.send_json(import_images_from_url(str(body.get("url", "")).strip()))
            if path == "/api/assets/prompt-from-selection":
                return self.send_json(image_prompt_from_selection(str(body.get("selected_text", "")), str(body.get("title", "")), str(body.get("context", ""))))
            if path == "/api/assets/generate-from-prompt":
                prompt = str(body.get("prompt", "")).strip()
                if not prompt:
                    raise ValueError("图片提示词为空")
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
                    cursor = conn.execute("INSERT INTO asset(name,path,kind,source_url,rights_note,prompt,usage,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                         (Path(rel_path).name, rel_path, body.get("kind", "image"), body.get("source_url", ""), body.get("rights_note", "待人工确认"), body.get("prompt", ""), body.get("usage", ""), now_iso()))
                return self.send_json({"ok": True, "id": cursor.lastrowid})
            if path == "/api/wechat/test":
                return self.send_json(WECHAT.test())
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
                    with db_conn() as conn:
                        asset = conn.execute("SELECT * FROM asset WHERE id=?", (draft["cover_asset_id"],)).fetchone() if draft["cover_asset_id"] else conn.execute("SELECT * FROM asset WHERE kind='image' ORDER BY CASE WHEN name LIKE '%cover%' OR name LIKE '%封面%' OR name LIKE '01%' THEN 0 ELSE 1 END, id LIMIT 1").fetchone()
                    if asset:
                        cover_media = WECHAT.upload_cover(safe_relative_path(asset["path"]))
                    content = prepare_wechat_content(draft["body"])
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
                end = datetime.now().date()
                begin = end - timedelta(days=7)
                result = WECHAT.article_summary(begin.isoformat(), end.isoformat())
                articles = result.get("list") or []
                expected_metrics = ("int_page_read_count", "share_count", "add_to_fav_count", "int_page_from_session_read_count")
                with db_conn() as conn:
                    for item in articles:
                        key = str(item.get("ref_date") or item.get("title") or uuid.uuid4().hex)
                        raw_item = dict(item)
                        raw_item["_missing_fields"] = [field for field in expected_metrics if field not in item]
                        raw_json = json.dumps(raw_item, ensure_ascii=False, sort_keys=True)
                        for metric_type in expected_metrics:
                            if metric_type in item:
                                exists = conn.execute("SELECT 1 FROM metric_record WHERE article_key=? AND metric_type=? AND raw_json=? LIMIT 1", (key, metric_type, raw_json)).fetchone()
                                if not exists:
                                    conn.execute("INSERT INTO metric_record(article_key,observed_at,metric_type,value,raw_json) VALUES(?,?,?,?,?)", (key, now_iso(), metric_type, float(item.get(metric_type) or 0), raw_json))
                return self.send_json({"ok": True, "count": len(articles), "message": f"回收 {len(articles)} 条公众号数据"})
            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            return self.send_json({"ok": False, "error": redact_sensitive(exc)}, 400)


def main() -> None:
    init_db()
    seed_style_profile()
    seed_local_sources()
    seed_assets()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Editorial Workbench running at http://{HOST}:{PORT}")
    print(f"Local data: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
