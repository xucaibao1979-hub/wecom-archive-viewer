"""Read-only local browser for the existing WeCom PostgreSQL archive."""
import json
import os
import csv
import io
import mimetypes
import sqlite3
import subprocess
import tempfile
import unicodedata
import secrets
import hmac
import time
import re
import logging
import hashlib
import base64
import zipfile
from collections import OrderedDict
from threading import Lock
from urllib.request import urlopen, Request, build_opener, HTTPRedirectHandler
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, render_template, request, session, Response, send_file, abort, redirect
from dotenv import load_dotenv
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash
from imageio_ffmpeg import get_ffmpeg_exe

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VIEWER_DATA_DIR", "/opt/archive-data" if os.name != "nt" else str(APP_DIR))).expanduser().resolve()
load_dotenv(DATA_DIR / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("VIEWER_SECRET_KEY", "")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("VIEWER_COOKIE_SECURE", "false").lower() == "true")
USER_DB = Path(os.getenv("VIEWER_USER_DB", str(DATA_DIR / "viewer_users.db"))).expanduser().resolve()
SSO_TTL_SECONDS = 60
READ_CACHE_TTL = min(300, max(0, int(os.getenv("VIEWER_CACHE_TTL_SECONDS", "45"))))
READ_CACHE = OrderedDict()
READ_CACHE_LOCK = Lock()
MENU_KEYS = ("employees", "group", "export", "files", "customers", "groups",
             "customerStats", "conversationStats", "officialActivity", "serviceMessages")
DEFAULT_MENUS = json.dumps(MENU_KEYS)
ENDPOINT_MENUS = {
    "archive_employees": {"employees", "customerStats", "conversationStats"},
    "employees": {"employees", "customerStats", "conversationStats"},
    "conversations": {"employees", "group", "export", "files"},
    "messages": {"employees", "group"},
    "customers": {"customers"}, "groups": {"groups", "files"},
    "official_group_activity": {"officialActivity"},
    "service_message_stats": {"serviceMessages"},
    "customer_stats": {"customerStats"}, "conversation_stats": {"conversationStats"},
    "home_dashboard": set(), "files": {"files"}, "file_groups": {"files"}, "batch_download_files": {"files"},
    "media": {"employees", "group", "files"}, "play_audio": {"employees", "group"},
    "export_conversation": {"employees", "group", "export"},
}


class RedactSsoTicket(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "/sso/login?ticket=" in message or "/sso/consume?ticket=" in message or "/?sso_token=" in message:
            record.msg = re.sub(r"(/sso/(?:login|consume)\?ticket=|/\?sso_token=)[^\s\"&]+", r"\1[redacted]", message)
            record.args = ()
        return True


logging.getLogger("werkzeug").addFilter(RedactSsoTicket())


@contextmanager
def user_db():
    if not USER_DB.parent.is_dir():
        raise RuntimeError(f"用户数据目录不存在：{USER_DB.parent}；请先创建目录并检查 VIEWER_USER_DB")
    conn = sqlite3.connect(USER_DB, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','viewer')), enabled INTEGER NOT NULL DEFAULT 1)")
        if not any(row[1] == "display_name" for row in conn.execute("PRAGMA table_info(users)")):
            conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "wecom_userid" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN wecom_userid TEXT")
        if "sso_key_hash" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN sso_key_hash TEXT")
        if "sso_key_version" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN sso_key_version TEXT")
        if "sso_created_at" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN sso_created_at TEXT")
        if "sso_external_user_id" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN sso_external_user_id TEXT")
        conn.execute("UPDATE users SET sso_key_hash=NULL,sso_key_version=NULL WHERE sso_key_hash IS NOT NULL AND length(sso_key_hash)<>64")
        if "menu_permissions" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN menu_permissions TEXT NOT NULL DEFAULT '[]'")
            conn.execute("UPDATE users SET menu_permissions=?", (DEFAULT_MENUS,))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_wecom_userid_unique ON users(wecom_userid) WHERE wecom_userid IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_sso_external_user_id_unique ON users(sso_external_user_id) WHERE sso_external_user_id IS NOT NULL")
        conn.execute("CREATE TABLE IF NOT EXISTS viewer_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS sso_tickets (ticket_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, auth_version TEXT NOT NULL, expires_at INTEGER NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS sso_tickets_expires ON sso_tickets(expires_at)")
        conn.execute("CREATE TABLE IF NOT EXISTS sso_rate_limits (bucket TEXT PRIMARY KEY, window_start INTEGER NOT NULL, hits INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')")
        if not any(row[1] == "detail" for row in conn.execute("PRAGMA table_info(audit_events)")):
            conn.execute("ALTER TABLE audit_events ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(action, detail="", actor=None):
    actor = actor or session.get("username") or "system"
    with user_db() as conn:
        conn.execute("INSERT INTO audit_events(actor,action,detail) VALUES(?,?,?)", (actor, action, detail[:500]))


def service_staff_ids():
    with user_db() as conn:
        row = conn.execute("SELECT value FROM viewer_settings WHERE key='service_staff_ids'").fetchone()
    if row:
        return json.loads(row["value"]), "custom"
    department_ids = [x.strip() for x in os.getenv("VIEWER_SERVICE_DEPARTMENT_IDS", "194").split(",") if x.strip()]
    if not department_ids:
        return [], "department"
    users = query("""SELECT userid FROM wecom_users
                     WHERE string_to_array(replace(department,' ',''),',') && %s::text[]""", (department_ids,))
    return [u["userid"] for u in users], "department"


def bootstrap_admin():
    username = os.getenv("VIEWER_ADMIN_USER", "").strip()
    password = os.getenv("VIEWER_ADMIN_PASSWORD", "")
    with user_db() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 and username and password:
            conn.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (username, generate_password_hash(password), "admin"))


def require_login(admin=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return jsonify({"error": "请先登录"}), 401
            with user_db() as conn:
                account = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
            if not account or not account["enabled"]:
                session.clear()
                return jsonify({"error": "账号已停用，请重新登录"}), 401
            if session.get("sso_key_version"):
                if time.time() - float(session.get("sso_login_at", 0)) > 8 * 3600:
                    session.clear()
                    return jsonify({"error": "免登会话已过期，请重新进入"}), 401
                if not account["sso_key_hash"] or account["sso_key_version"] != session["sso_key_version"]:
                    session.clear()
                    return jsonify({"error": "免登授权已失效，请重新进入"}), 401
            session["username"], session["role"] = account["username"], account["role"]
            session["menu_permissions"] = list(MENU_KEYS) if account["role"] == "admin" else json.loads(account["menu_permissions"] or "[]")
            session.setdefault("csrf_token", secrets.token_urlsafe(24))
            if admin and session["role"] != "admin":
                return jsonify({"error": "需要管理员权限"}), 403
            required = ENDPOINT_MENUS.get(request.endpoint)
            if account["role"] != "admin" and required is not None and required and not required.intersection(session["menu_permissions"]):
                return jsonify({"error": "无此菜单权限"}), 403
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def cached_read(fn):
    """Cache common archive GET responses briefly, after authorization is checked."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not READ_CACHE_TTL:
            return fn(*args, **kwargs)
        key = (request.endpoint, tuple(sorted(request.args.items(multi=True))),
               datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"))
        now = time.monotonic()
        with READ_CACHE_LOCK:
            hit = READ_CACHE.get(key)
            if hit and hit[0] > now:
                READ_CACHE.move_to_end(key)
                return Response(hit[1], mimetype="application/json")
        result = fn(*args, **kwargs)
        if isinstance(result, Response) and result.status_code == 200 and result.mimetype == "application/json":
            body = result.get_data()
            with READ_CACHE_LOCK:
                READ_CACHE[key] = (time.monotonic() + READ_CACHE_TTL, body)
                READ_CACHE.move_to_end(key)
                while len(READ_CACHE) > 128:
                    READ_CACHE.popitem(last=False)
        return result
    return wrapped


def check_origin():
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        abort(403)


@app.before_request
def protect_browser_mutations():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE") or request.endpoint in ("login", "issue_sso_ticket"):
        return None
    if session.get("user_id"):
        check_origin()
        token = session.get("csrf_token", "")
        if not token or not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), token):
            return jsonify({"error": "CSRF 校验失败，请刷新页面"}), 403
    return None


def rate_allowed(kind, identity, limit, window=60):
    now = int(time.time())
    bucket = kind + ":" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    with user_db() as conn:
        conn.execute("""INSERT INTO sso_rate_limits(bucket,window_start,hits) VALUES(?,?,1)
                        ON CONFLICT(bucket) DO UPDATE SET
                        window_start=CASE WHEN window_start<=? THEN excluded.window_start ELSE window_start END,
                        hits=CASE WHEN window_start<=? THEN 1 ELSE hits+1 END""",
                     (bucket, now, now-window, now-window))
        hits = conn.execute("SELECT hits FROM sso_rate_limits WHERE bucket=?", (bucket,)).fetchone()[0]
    return hits <= limit


def official_group_rule(alias="r"):
    """Keep both official-group reports on the same configurable definition."""
    keywords = [value.strip() for value in os.getenv("VIEWER_OFFICIAL_GROUP_KEYWORDS", "官群").split(",") if value.strip()]
    room_ids = [value.strip() for value in os.getenv("VIEWER_OFFICIAL_GROUP_IDS", "").split(",") if value.strip()]
    patterns = ["%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for value in keywords]
    return f"({alias}.room_name ILIKE ANY(%s::text[]) OR {alias}.roomid = ANY(%s::text[]))", (patterns, room_ids)


def sso_origin():
    origin = os.getenv("VIEWER_PUBLIC_ORIGIN", "").rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or not app.config["SESSION_COOKIE_SECURE"]:
        return None
    return origin


@app.after_request
def sso_response_headers(response):
    if request.path in ("/api/sso/ticket", "/sso/login") or request.path.endswith("/sso-key") or (request.path == "/" and "sso_token" in request.args):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post("/api/sso/ticket")
def issue_sso_ticket():
    origin = sso_origin()
    if not origin:
        audit("免登换票失败", "HTTPS 或 Secure Cookie 未配置", actor="sso")
        return jsonify({"error": "免登服务未完成 HTTPS 或 Secure Cookie 配置"}), 503
    aura_key = request.headers.get("X-Aura-SSO-Key", "")
    standard_key = request.headers.get("X-SSO-Key", "")
    key = aura_key or standard_key
    address = request.remote_addr or "unknown"
    if not rate_allowed("ticket-ip", address, 30) or not rate_allowed("ticket-key", key, 12):
        audit("免登换票失败", "请求过于频繁", actor="sso")
        return jsonify({"error": "请求过于频繁"}), 429
    if aura_key and standard_key and not hmac.compare_digest(aura_key, standard_key):
        audit("免登换票失败", "两个密钥请求头不一致", actor="sso")
        return jsonify({"error": "密钥请求头冲突"}), 400
    if not key:
        audit("免登换票失败", "缺少免登密钥请求头", actor="sso")
        return jsonify({"error": "缺少 X-Aura-SSO-Key 或 X-SSO-Key 请求头"}), 400
    external_id = None
    if aura_key:
        payload = request.get_json(silent=True)
        external_id = payload.get("userId") if isinstance(payload, dict) else None
        if not isinstance(external_id, str) or not external_id or len(external_id) > 80:
            audit("免登换票失败", "IT小爱 userId 缺失或格式无效", actor="sso")
            return jsonify({"error": "userId 格式无效"}), 400
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with user_db() as conn:
        account = conn.execute("SELECT id,enabled,sso_key_version,sso_external_user_id FROM users WHERE sso_key_hash=?", (digest,)).fetchone()
    if not account:
        audit("免登换票失败", "未匹配到用户授权", actor="sso")
        return jsonify({"error": "免登密钥无效或授权已失效"}), 403
    if not account["enabled"] or not account["sso_key_version"]:
        audit("免登换票失败", f"用户ID {account['id']} {'账号已停用' if not account['enabled'] else '授权已失效'}", actor="sso")
        return jsonify({"error": "免登密钥无效或授权已失效"}), 403
    if aura_key and not hmac.compare_digest(account["sso_external_user_id"] or "", external_id):
        audit("免登换票失败", f"用户ID {account['id']} IT小爱 userId 与授权绑定不符", actor="sso")
        return jsonify({"error": "免登密钥无效或授权已失效"}), 403
    now = int(time.time())
    ticket = secrets.token_urlsafe(32)
    ticket_hash = hashlib.sha256(ticket.encode("ascii")).hexdigest()
    saturated = False
    with user_db() as conn:
        conn.execute("DELETE FROM sso_tickets WHERE expires_at<=?", (now,))
        saturated = conn.execute("SELECT COUNT(*) FROM sso_tickets").fetchone()[0] >= 1000
        if not saturated:
            conn.execute("INSERT INTO sso_tickets(ticket_hash,user_id,auth_version,expires_at) VALUES(?,?,?,?)",
                         (ticket_hash, account["id"], account["sso_key_version"], now + SSO_TTL_SECONDS))
    if saturated:
        audit("免登换票失败", "未消费票据过多", actor="sso")
        return jsonify({"error": "未消费票据过多，请稍后重试"}), 503
    audit("免登换票成功", f"用户ID {account['id']}", actor="sso")
    login_url = f"{origin}/?sso_token={ticket}" if aura_key else f"{origin}/sso/login?ticket={ticket}"
    response = jsonify({"loginUrl": login_url, "expiresIn": SSO_TTL_SECONDS})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/sso/login")
def consume_sso_ticket():
    if not sso_origin():
        return jsonify({"error": "免登服务未配置"}), 503
    ticket = request.args.get("ticket", "") or request.args.get("sso_token", "")
    if not rate_allowed("consume-ip", request.remote_addr or "unknown", 60):
        audit("免登登录失败", "请求过于频繁", actor="sso")
        return jsonify({"error": "请求过于频繁"}), 429
    ticket_hash = hashlib.sha256(ticket.encode("utf-8")).hexdigest() if ticket else ""
    with user_db() as conn:
        record = conn.execute("DELETE FROM sso_tickets WHERE ticket_hash=? RETURNING user_id,auth_version,expires_at", (ticket_hash,)).fetchone()
        account = conn.execute("SELECT id,enabled,sso_key_hash,sso_key_version FROM users WHERE id=?", (record["user_id"],)).fetchone() if record else None
    if not record or record["expires_at"] <= int(time.time()):
        audit("免登登录失败", "票据过期或重复使用", actor="sso")
        return jsonify({"error": "票据已过期或已使用"}), 400
    if not account or not account["enabled"] or not account["sso_key_hash"] or account["sso_key_version"] != record["auth_version"]:
        audit("免登登录失败", f"用户ID {record['user_id']} 授权已失效", actor="sso")
        return jsonify({"error": "用户免登授权已失效"}), 403
    session.clear()
    session.update(user_id=record["user_id"], sso_key_version=record["auth_version"], sso_login_at=time.time(), csrf_token=secrets.token_urlsafe(24))
    audit("免登登录成功", f"用户ID {record['user_id']}")
    response = redirect("/", code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/settings/service-staff")
@require_login(admin=True)
def get_service_staff():
    selected, source = service_staff_ids()
    users = query("SELECT userid,name,department FROM wecom_users ORDER BY name,userid LIMIT 5000")
    return jsonify({"users": users, "selected": selected, "source": source})


@app.put("/api/settings/service-staff")
@require_login(admin=True)
def save_service_staff():
    check_origin()
    data = request.get_json(silent=True) or {}
    selected = data.get("selected")
    if not isinstance(selected, list) or len(selected) > 5000 or any(not isinstance(x, str) or len(x) > 128 for x in selected):
        return jsonify({"error": "人员列表无效"}), 400
    selected = list(dict.fromkeys(selected))
    if selected:
        known = query("SELECT userid FROM wecom_users WHERE userid = ANY(%s::text[])", (selected,))
        if len(known) != len(selected):
            return jsonify({"error": "人员列表包含未知员工"}), 400
    with user_db() as conn:
        conn.execute("INSERT INTO viewer_settings(key,value) VALUES('service_staff_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(selected),))
    audit("设置客服统计人员", f"{len(selected)} 人")
    return jsonify({"ok": True, "selected": selected})


@app.delete("/api/settings/service-staff")
@require_login(admin=True)
def reset_service_staff():
    check_origin()
    with user_db() as conn:
        conn.execute("DELETE FROM viewer_settings WHERE key='service_staff_ids'")
    audit("恢复客服统计人员默认配置")
    return jsonify({"ok": True})


@app.get("/api/settings/sso-info")
@require_login(admin=True)
def get_sso_info():
    return jsonify({"ready": bool(sso_origin()),
                    "origin": os.getenv("VIEWER_PUBLIC_ORIGIN", "")})


@app.get("/api/settings/audit")
@require_login(admin=True)
def get_audit_events():
    with user_db() as conn:
        rows = conn.execute("SELECT created_at,actor,action,detail FROM audit_events ORDER BY id DESC LIMIT 500").fetchall()
    return jsonify([dict(row) for row in rows])


@app.errorhandler(psycopg.Error)
def database_error(error):
    app.logger.warning("数据库读取失败: %s", error.__class__.__name__)
    return jsonify({"error": "数据库连接或查询失败，请检查 PostgreSQL 地址、网络和只读权限"}), 503


def db_config():
    cfg = {}
    for env, key in (("WECOM_DB_HOST", "host"), ("WECOM_DB_PORT", "port"),
                     ("WECOM_DB_NAME", "dbname"), ("WECOM_DB_USER", "user"),
                     ("WECOM_DB_PASSWORD", "password")):
        if os.getenv(env):
            cfg[key] = os.environ[env]
    return {k: cfg[k] for k in ("host", "port", "dbname", "user", "password") if k in cfg}


def query(sql, params=()):
    with psycopg.connect(**db_config(), options="-c default_transaction_read_only=on", connect_timeout=4) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def recipients(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return [str(x) for x in result] if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


def readable_customer_name(value):
    clean = "".join(c for c in (value or "") if unicodedata.category(c) not in ("Co", "Cc", "Cf", "Cs"))
    clean = clean.strip()
    return clean if any(c.isalnum() or unicodedata.category(c) == "So" for c in clean) else ""


def customer_name_map(userids):
    if not userids:
        return {}
    people = query("""SELECT COALESCE(member->>'userid',member->>'memberid') AS userid,
                             (ARRAY_AGG(NULLIF(member->>'name','') ORDER BY r.updated_at DESC NULLS LAST)
                               FILTER (WHERE NULLIF(member->>'name','') IS NOT NULL))[1] AS name,
                             (ARRAY_AGG(NULLIF(member->>'group_nickname','') ORDER BY r.updated_at DESC NULLS LAST)
                               FILTER (WHERE NULLIF(member->>'group_nickname','') IS NOT NULL))[1] AS group_nickname
                      FROM wecom_rooms r CROSS JOIN LATERAL
                        jsonb_array_elements(CASE WHEN r.source='external' THEN COALESCE(r.raw_json->'group_chat'->'member_list','[]'::jsonb)
                                                  ELSE COALESCE(r.raw_json->'members','[]'::jsonb) END) member
                      WHERE COALESCE(member->>'userid',member->>'memberid') = ANY(%s)
                      GROUP BY COALESCE(member->>'userid',member->>'memberid')""", (list(userids),))
    return {p["userid"]: readable_customer_name(p["name"]) or readable_customer_name(p["group_nickname"])
            for p in people}


@app.get("/")
def index():
    if "sso_token" in request.args:
        return consume_sso_ticket()
    return render_template("index.html")


@app.post("/api/login")
def login():
    check_origin()
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    if not rate_allowed("password-login-ip", request.remote_addr or "unknown", 40, 300) or not rate_allowed("password-login-user", username.lower(), 12, 300):
        audit("密码登录失败", "尝试过于频繁", actor="login")
        return jsonify({"error": "登录尝试过于频繁，请五分钟后重试"}), 429
    with user_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not row["enabled"] or not check_password_hash(row["password_hash"], str(data.get("password", ""))):
        audit("密码登录失败", "账号不存在、已停用或密码错误", actor="login")
        return jsonify({"error": "用户名或密码错误"}), 401
    session.clear()
    session.update(user_id=row["id"], username=row["username"], role=row["role"], csrf_token=secrets.token_urlsafe(24))
    audit("密码登录成功", f"用户ID {row['id']}")
    return jsonify({"username": row["username"], "role": row["role"]})


@app.post("/api/logout")
@require_login()
def logout():
    check_origin()
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@require_login()
def me():
    return jsonify({"username": session["username"], "role": session["role"], "menu_permissions": session["menu_permissions"], "csrf_token": session["csrf_token"]})


@app.get("/api/users")
@require_login(admin=True)
def users():
    with user_db() as conn:
        rows = conn.execute("SELECT id,username,display_name,role,enabled,menu_permissions,sso_key_hash,sso_key_version,sso_created_at,sso_external_user_id FROM users ORDER BY id").fetchall()
    return jsonify([{**{k: r[k] for k in ("id","username","display_name","role","enabled","sso_created_at","sso_external_user_id")},
                     "menu_permissions": json.loads(r["menu_permissions"] or "[]"), "has_sso_key": bool(r["sso_key_hash"] and r["sso_key_version"])} for r in rows])


def valid_menu_permissions(value):
    return isinstance(value, list) and len(value) <= len(MENU_KEYS) and all(isinstance(x, str) and x in MENU_KEYS for x in value) and len(value) == len(set(value))


def valid_sso_external_id(value):
    return value is None or bool(re.fullmatch(r"[A-Za-z0-9_@.\-]{1,80}", value))


@app.post("/api/users")
@require_login(admin=True)
def add_user():
    check_origin()
    data = request.get_json(silent=True) or {}
    username, password, role = str(data.get("username", "")).strip(), str(data.get("password", "")), data.get("role", "viewer")
    display_name = str(data.get("display_name", "")).strip()
    external_id = str(data.get("sso_external_user_id", "")).strip() or None
    menus = data.get("menu_permissions", [])
    if not username or len(username) > 80 or len(display_name) > 80 or not password or role not in ("admin", "viewer") or not valid_menu_permissions(menus) or not valid_sso_external_id(external_id):
        return jsonify({"error": "请填写用户名、密码和有效角色"}), 400
    try:
        with user_db() as conn:
            conn.execute("INSERT INTO users(username,display_name,password_hash,role,menu_permissions,sso_external_user_id) VALUES(?,?,?,?,?,?)", (username, display_name, generate_password_hash(password), role, json.dumps(menus), external_id))
    except sqlite3.IntegrityError:
        return jsonify({"error": "用户名或 IT小爱 userId 已存在"}), 409
    audit("新增用户", username)
    return jsonify({"ok": True}), 201


@app.patch("/api/users/<int:user_id>")
@require_login(admin=True)
def edit_user(user_id):
    check_origin()
    data = request.get_json(silent=True) or {}
    with user_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        if user_id == session.get("user_id") and data.get("enabled") is False:
            return jsonify({"error": "不能停用当前账号"}), 400
        role = data.get("role", row["role"])
        if role not in ("admin", "viewer"):
            return jsonify({"error": "角色无效"}), 400
        if user_id == session.get("user_id") and role != "admin":
            return jsonify({"error": "不能取消当前账号的管理员权限"}), 400
        password = data.get("password")
        if password is not None and not str(password):
            return jsonify({"error": "新密码不能为空"}), 400
        username = str(data.get("username", row["username"])).strip()
        display_name = str(data.get("display_name", row["display_name"])).strip()
        external_id = str(data.get("sso_external_user_id", row["sso_external_user_id"] or "")).strip() or None
        binding_changed = external_id != row["sso_external_user_id"]
        menus = data.get("menu_permissions", json.loads(row["menu_permissions"] or "[]"))
        if not username or len(username) > 80 or len(display_name) > 80 or not valid_menu_permissions(menus) or not valid_sso_external_id(external_id):
            return jsonify({"error": "用户名或显示名称无效"}), 400
        try:
            auth_version = secrets.token_hex(16) if binding_changed and row["sso_key_hash"] else row["sso_key_version"]
            conn.execute("UPDATE users SET username=?, display_name=?, role=?, enabled=?, password_hash=?, menu_permissions=?, sso_external_user_id=?, sso_key_version=? WHERE id=?",
                         (username, display_name, role, int(data.get("enabled", bool(row["enabled"]))), generate_password_hash(password) if password else row["password_hash"], json.dumps(menus), external_id, auth_version, user_id))
        except sqlite3.IntegrityError:
            return jsonify({"error": "用户名或 IT小爱 userId 已存在"}), 409
    audit("修改用户", username)
    if binding_changed:
        audit("修改免登绑定", f"用户ID {user_id}")
    return jsonify({"ok": True})


@app.post("/api/users/<int:user_id>/sso-key")
@require_login(admin=True)
def generate_user_sso_key(user_id):
    check_origin()
    with user_db() as conn:
        row = conn.execute("SELECT username,enabled,sso_key_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "用户不存在"}), 404
    if not row["enabled"]:
        return jsonify({"error": "请先启用用户"}), 400
    secret = secrets.token_urlsafe(32)
    version = secrets.token_hex(16)
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    with user_db() as conn:
        conn.execute("UPDATE users SET sso_key_hash=?,sso_key_version=?,sso_created_at=? WHERE id=?", (hashlib.sha256(secret.encode("utf-8")).hexdigest(), version, created_at, user_id))
    audit("重置免登密钥" if row["sso_key_hash"] else "生成免登密钥", row["username"])
    response = jsonify({"key": secret, "created_at": created_at})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/api/users/<int:user_id>/sso-key")
@require_login(admin=True)
def revoke_user_sso_key(user_id):
    check_origin()
    with user_db() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        conn.execute("UPDATE users SET sso_key_hash=NULL,sso_key_version=NULL WHERE id=?", (user_id,))
    audit("吊销免登授权", row["username"])
    return jsonify({"ok": True})


@app.get("/api/conversations")
@require_login()
@cached_read
def conversations():
    keyword = request.args.get("q", "").strip()[:100].lower()
    employee = request.args.get("employee", "").strip()[:100]
    category = request.args.get("category", "group")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=0, second=0, microsecond=0)
    if request.args.get("scope") == "all-groups":
        selected_date = request.args.get("date", "")
        try:
            selected_day = datetime.strptime(selected_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) if selected_date else today
        except ValueError:
            return jsonify({"error": "日期格式无效"}), 400
        selected_start = int(selected_day.timestamp() * 1000)
        selected_end = int((selected_day + timedelta(days=1)).timestamp() * 1000)
        rows = query("""SELECT r.roomid, COALESCE(NULLIF(r.room_name,''),r.roomid) AS title,
                               r.source, MAX(m.msgtime) AS msgtime,
                               COUNT(m.id) AS message_count,
                               COUNT(m.id) FILTER (WHERE m.msgtime >= %s AND m.msgtime < %s) AS today_count
                        FROM wecom_rooms r LEFT JOIN messages m ON m.roomid=r.roomid
                        GROUP BY r.roomid,r.room_name,r.source
                        ORDER BY msgtime DESC NULLS LAST,r.room_name LIMIT 5000""",
                     (selected_start, selected_end))
        return jsonify([{"id": "room:" + r["roomid"], "title": r["title"], "kind": "group",
                         "source": r["source"], "message_count": r["message_count"], "today_count": r["today_count"],
                         "preview": f"{selected_day:%m/%d} {r['today_count']} 条 · 总计 {r['message_count']} 条", "msgtime": r["msgtime"]}
                        for r in rows if not keyword or keyword in r["title"].lower()])
    if employee:
        if category in ("group", "internal"):
            selected_date = request.args.get("date", "")
            try:
                selected_day = datetime.strptime(selected_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) if selected_date else today
            except ValueError:
                return jsonify({"error": "日期格式无效"}), 400
            selected_start = int(selected_day.timestamp() * 1000)
            selected_end = int((selected_day + timedelta(days=1)).timestamp() * 1000)
            source = "internal" if category == "internal" else None
            rows = query("""SELECT r.roomid, COALESCE(NULLIF(r.room_name,''),r.roomid) AS title,
                                   MAX(m.msgtime) FILTER (WHERE m.msgtime >= %s AND m.msgtime < %s) AS msgtime,
                                   COUNT(m.id) AS message_count,
                                   COUNT(m.id) FILTER (WHERE m.msgtime >= %s AND m.msgtime < %s) AS today_count
                            FROM wecom_rooms r LEFT JOIN messages m ON m.roomid=r.roomid
                            WHERE (%s::text IS NULL OR r.source=%s) AND
                              (r.owner=%s OR EXISTS (
                                SELECT 1 FROM jsonb_array_elements(
                                  CASE WHEN r.source='external' THEN COALESCE(r.raw_json->'group_chat'->'member_list','[]'::jsonb)
                                       ELSE COALESCE(r.raw_json->'members','[]'::jsonb) END
                                ) member WHERE COALESCE(member->>'userid',member->>'memberid')=%s))
                            GROUP BY r.roomid,r.room_name ORDER BY msgtime DESC NULLS LAST,r.room_name LIMIT 5000""",
                         (selected_start, selected_end, selected_start, selected_end, source, source, employee, employee))
            result = [{"id": "room:" + r["roomid"], "title": r["title"], "kind": category,
                       "message_count": r["message_count"], "today_count": r["today_count"],
                       "preview": f"{selected_day:%m/%d} {r['today_count']} 条 · 总计 {r['message_count']} 条", "msgtime": r["msgtime"]} for r in rows]
        elif category == "customer":
            result = []
            rows = query("""SELECT from_user,tolist,msgtime,COALESCE(content,'') AS preview
                            FROM messages WHERE roomid IS NULL AND
                            (from_user=%s OR tolist::jsonb @> %s::jsonb)
                            ORDER BY msgtime DESC LIMIT 50000""", (employee, json.dumps([employee])))
            seen = set()
            for r in rows:
                peers = [x for x in set([r["from_user"], *recipients(r["tolist"])]) if x != employee]
                for peer in peers:
                    people = sorted([employee, peer])
                    key = "direct:" + json.dumps(people, ensure_ascii=False)
                    if key not in seen:
                        seen.add(key)
                        result.append({"id": key, "title": peer, "kind": "customer", "preview": r["preview"][:100], "msgtime": r["msgtime"]})
        else:
            return jsonify({"error": "无效会话类别"}), 400
        return jsonify([r for r in result if not keyword or keyword in r["title"].lower() or keyword in r["preview"].lower()])
    # Scan recent messages; cap work for a responsive local browser.
    rows = query("""SELECT from_user, tolist, roomid, room_name, msgtime, msgtype,
                          COALESCE(NULLIF(parsed_content, ''), content, '') AS preview
                   FROM messages ORDER BY msgtime DESC NULLS LAST LIMIT 10000""")
    employees = {r["userid"] for r in query("SELECT userid FROM wecom_users")}
    found = {}
    for row in rows:
        sender = row["from_user"] or "未知用户"
        peers = recipients(row["tolist"])
        if row["roomid"]:
            key = "room:" + row["roomid"]
            title = row["room_name"] or row["roomid"]
            kind = "group"
        else:
            people = sorted(set([sender, *peers]))
            if len(people) != 2:
                continue
            key = "direct:" + json.dumps(people, ensure_ascii=False)
            title = " · ".join(people)
            kind = "employee" if all(person in employees for person in people) else "customer"
        if keyword and keyword not in title.lower() and keyword not in str(row["preview"]).lower():
            continue
        if key not in found:
            found[key] = {"id": key, "title": title, "kind": kind,
                          "preview": str(row["preview"])[:100], "msgtime": row["msgtime"]}
    return jsonify(list(found.values())[:300])


@app.get("/api/messages")
@require_login()
def messages():
    conversation = request.args.get("conversation", "")
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(500, max(1, request.args.get("page_size", 20, type=int)))
    keyword = request.args.get("q", "").strip()[:100]
    if conversation.startswith("room:"):
        where, params = "m.roomid = %s", [conversation[5:]]
    elif conversation.startswith("direct:"):
        try:
            people = json.loads(conversation[7:])
        except ValueError:
            return jsonify({"error": "无效会话"}), 400
        if not isinstance(people, list) or len(people) != 2 or not all(isinstance(p, str) for p in people):
            return jsonify({"error": "无效会话"}), 400
        a, b = people
        where = "m.roomid IS NULL AND ((m.from_user = %s AND m.tolist::jsonb @> %s::jsonb) OR (m.from_user = %s AND m.tolist::jsonb @> %s::jsonb))"
        params = [a, json.dumps([b]), b, json.dumps([a])]
    else:
        return jsonify({"error": "请选择会话"}), 400
    start_ms = request.args.get("start_ms", type=int)
    end_ms = request.args.get("end_ms", type=int)
    if start_ms is not None:
        where += " AND m.msgtime >= %s"
        params.append(start_ms)
    if end_ms is not None:
        where += " AND m.msgtime < %s"
        params.append(end_ms)
    sender = request.args.get("sender", "").strip()[:80]
    if sender:
        where += " AND (m.from_user ILIKE %s OR EXISTS (SELECT 1 FROM wecom_users su WHERE su.userid=m.from_user AND su.name ILIKE %s))"
        params.extend([f"%{sender}%", f"%{sender}%"])
    category = request.args.get("type", "all")
    types = {"file": ["file", "qydiskfile", "voip_doc_share"],
             "media": ["image", "video", "emotion"],
             "link": ["link"], "weapp": ["weapp"]}
    if category != "all":
        if category not in types:
            return jsonify({"error": "无效消息类型"}), 400
        where += " AND m.msgtype = ANY(%s)"
        params.append(types[category])
    if keyword:
        where += " AND (COALESCE(m.content, '') ILIKE %s OR COALESCE(m.raw_json, '') ILIKE %s)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    total = query(f"SELECT COUNT(*) AS n FROM messages m WHERE {where}", params)[0]["n"]
    rows = query(f"""SELECT m.id, m.msgid, m.action, m.from_user, u.userid IS NOT NULL AS is_employee,
                            COALESCE(NULLIF(u.name,''), NULLIF(m.from_user_name,''), m.from_user) AS sender_name,
                            m.tolist, m.roomid, m.msgtime, m.msgtype, m.raw_json,
                            COALESCE(m.content, '') AS content, m.file_name
                     FROM messages m LEFT JOIN wecom_users u ON u.userid=m.from_user
                     WHERE {where}
                     ORDER BY m.msgtime DESC NULLS LAST, m.id DESC LIMIT %s OFFSET %s""",
                 [*params, page_size, (page - 1) * page_size])
    ids = [r["msgid"] for r in rows]
    customer_ids = list({r["from_user"] for r in rows if not r["is_employee"] and r["from_user"]})
    customer_names = customer_name_map(customer_ids)
    recalled_ids = set()
    if ids:
        recalled_ids = {r["pre_msgid"] for r in query("""SELECT raw_json::jsonb->'revoke'->>'pre_msgid' AS pre_msgid
                      FROM messages WHERE msgtype='revoke' AND raw_json::jsonb->'revoke'->>'pre_msgid'=ANY(%s)""", (ids,))}
    media = {}
    if ids:
        for item in query("""SELECT id, msgid, msgtype, filename, file_name, download_status, oss_url
                             FROM media_files WHERE msgid = ANY(%s)""", (ids,)):
            media.setdefault(item["msgid"], []).append(item)
    for row in rows:
        if not row.pop("is_employee"):
            row["sender_name"] = customer_names.get(row["from_user"]) or row["sender_name"]
        row["tolist"] = recipients(row["tolist"])
        row["media"] = media.get(row["msgid"], [])
        try:
            row["payload"] = json.loads(row.pop("raw_json") or "{}")
        except (ValueError, TypeError):
            row["payload"] = {}
        if row["msgtype"] == "text":
            original = row["payload"].get("text", {}).get("content")
            if isinstance(original, str):
                row["content"] = original
        row["recalled"] = row["msgid"] in recalled_ids or row["action"] in ("recall", "revoke")
    return jsonify({"messages": list(reversed(rows)), "page": page, "page_size": page_size,
                    "total": total, "pages": max(1, (total + page_size - 1) // page_size)})


@app.get("/api/employees")
@require_login()
def employees():
    return jsonify(query("SELECT userid, name, alias, department, status FROM wecom_users ORDER BY name LIMIT 2000"))


@app.get("/api/archive-employees")
@require_login()
@cached_read
def archive_employees():
    selected_date = request.args.get("date", "")
    try:
        day = datetime.strptime(selected_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) if selected_date else datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return jsonify({"error": "日期格式无效"}), 400
    start_ms = int(day.timestamp() * 1000)
    end_ms = int((day + timedelta(days=1)).timestamp() * 1000)
    return jsonify(query("""WITH membership AS (
                             SELECT r.roomid,r.owner AS userid FROM wecom_rooms r WHERE r.owner IS NOT NULL
                             UNION
                             SELECT r.roomid,COALESCE(member->>'userid',member->>'memberid') AS userid
                             FROM wecom_rooms r CROSS JOIN LATERAL jsonb_array_elements(
                               CASE WHEN r.source='external' THEN COALESCE(r.raw_json->'group_chat'->'member_list','[]'::jsonb)
                                    ELSE COALESCE(r.raw_json->'members','[]'::jsonb) END
                             ) member
                           ), room_counts AS (
                             SELECT userid,COUNT(DISTINCT roomid) AS group_count FROM membership GROUP BY userid
                           ), period_messages AS (
                             SELECT roomid,from_user,msgtime FROM messages
                             WHERE roomid IS NOT NULL AND msgtime >= %s AND msgtime < %s
                           ), room_messages AS (
                             SELECT roomid,COUNT(*) AS message_count FROM period_messages GROUP BY roomid
                           ), group_messages AS (
                             SELECT membership.userid,SUM(room_messages.message_count)::bigint AS message_count
                             FROM membership JOIN room_messages ON room_messages.roomid=membership.roomid
                             GROUP BY membership.userid
                           ), sent_messages AS (
                             SELECT from_user,COUNT(*) AS message_count,MAX(msgtime) AS last_msgtime
                             FROM period_messages GROUP BY from_user
                           )
                           SELECT u.userid,u.name,u.department,COALESCE(rc.group_count,0) AS group_count,
                                  COALESCE(gm.message_count,0) AS message_count,
                                  COALESCE(sm.message_count,0) AS sent_message_count,sm.last_msgtime
                           FROM wecom_users u LEFT JOIN room_counts rc ON rc.userid=u.userid
                           LEFT JOIN group_messages gm ON gm.userid=u.userid
                           LEFT JOIN sent_messages sm ON sm.from_user=u.userid
                           ORDER BY sent_message_count DESC,gm.message_count DESC,u.name LIMIT 2000""", (start_ms, end_ms)))


@app.get("/api/customers")
@require_login()
@cached_read
def customers():
    return jsonify(query("""SELECT member->>'userid' AS userid,
                                 (ARRAY_AGG(NULLIF(member->>'name','') ORDER BY r.updated_at DESC NULLS LAST)
                                   FILTER (WHERE NULLIF(member->>'name','') IS NOT NULL))[1] AS name,
                                 (ARRAY_AGG(NULLIF(member->>'group_nickname','') ORDER BY r.updated_at DESC NULLS LAST)
                                   FILTER (WHERE NULLIF(member->>'group_nickname','') IS NOT NULL))[1] AS group_nickname,
                                 COUNT(DISTINCT r.roomid) AS group_count
                          FROM wecom_rooms r CROSS JOIN LATERAL
                            jsonb_array_elements(r.raw_json->'group_chat'->'member_list') member
                          WHERE r.source='external' AND member->>'type'='2'
                            AND member->>'userid' IS NOT NULL
                          GROUP BY member->>'userid'
                          LIMIT 20000"""))


@app.get("/api/groups")
@require_login()
@cached_read
def groups():
    return jsonify(query("""SELECT r.roomid, r.room_name,
                                 COALESCE(NULLIF(r.room_name,''),'未获取群名') AS room_name_display, r.owner,
                                 COALESCE(NULLIF(u.name,''),NULLIF(r.owner_name,''),r.owner) AS owner_display,
                                 CASE WHEN NULLIF(r.room_name,'') IS NULL AND r.member_count=0
                                           AND r.error_msg IS NOT NULL THEN NULL ELSE r.member_count END AS member_count,
                                 r.source,
                                 CASE r.source WHEN 'internal' THEN '内部群'
                                               WHEN 'external' THEN '外部群' ELSE COALESCE(r.source,'未知') END AS source_label
                          FROM wecom_rooms r LEFT JOIN wecom_users u ON u.userid=r.owner
                          ORDER BY r.room_name NULLS LAST LIMIT 2000"""))


def stats_range():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = request.args.get("start_ms", int((today - timedelta(days=1)).timestamp() * 1000), type=int)
    end = request.args.get("end_ms", int(today.timestamp() * 1000), type=int)
    if start >= end or end - start > 366 * 86400000:
        return None
    return start, end


def stats_dimension():
    value = request.args.get("dimension", "day")
    return value if value in ("day", "month") else None


@app.get("/api/tools/official-groups")
@require_login()
def official_group_activity():
    period, dimension = stats_range(), stats_dimension()
    if not period or not dimension:
        return jsonify({"error": "日期范围或查询维度无效"}), 400
    start, end = period
    keyword = request.args.get("q", "").strip()[:100]
    rule, rule_params = official_group_rule()
    condition = f"m.msgtime >= %s AND m.msgtime < %s AND {rule}"
    params = [start, end, *rule_params]
    if keyword:
        condition += " AND r.room_name ILIKE %s"
        params.append(f"%{keyword}%")
    period_format = "YYYY-MM-DD" if dimension == "day" else "YYYY-MM"
    period_expr = f"to_char(to_timestamp(m.msgtime/1000.0) AT TIME ZONE 'Asia/Shanghai','{period_format}')"
    summary = query(f"""SELECT COUNT(*) AS total_messages,COUNT(DISTINCT m.roomid) AS active_groups
                        FROM messages m JOIN wecom_rooms r ON r.roomid=m.roomid
                        WHERE {condition}""", params)[0]
    daily = query(f"""SELECT {period_expr} AS day,COUNT(*) AS messages,
                             COUNT(DISTINCT m.roomid) AS active_groups
                      FROM messages m JOIN wecom_rooms r ON r.roomid=m.roomid
                      WHERE {condition} GROUP BY day ORDER BY day""", params)
    rooms = query(f"""SELECT r.roomid,r.room_name,COUNT(*) AS message_count,
                             COUNT(DISTINCT to_char(to_timestamp(m.msgtime/1000.0) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD')) AS active_days
                      FROM messages m JOIN wecom_rooms r ON r.roomid=m.roomid
                      WHERE {condition} GROUP BY r.roomid,r.room_name
                      ORDER BY message_count DESC,r.room_name LIMIT 5000""", params)
    return jsonify({"summary": summary, "daily": daily, "rooms": rooms,
                    "note": "官群按配置的当前群名关键词或群ID识别；消息量和活跃天数来自所选日期内已存档的群消息。"})


@app.get("/api/tools/service-messages")
@require_login()
def service_message_stats():
    period, dimension = stats_range(), stats_dimension()
    if not period or not dimension:
        return jsonify({"error": "日期范围或查询维度无效"}), 400
    start, end = period
    staff_ids, source = service_staff_ids()
    if not staff_ids:
        return jsonify({"summary": {"messages": 0, "staff": 0}, "rows": [], "daily": [], "distribution": [], "note": "客服统计人员为空，请在设置中选择员工。"})
    period_format = "YYYY-MM-DD" if dimension == "day" else "YYYY-MM"
    period_expr = f"to_char(to_timestamp(m.msgtime/1000.0) AT TIME ZONE 'Asia/Shanghai','{period_format}')"
    params = (start, end, staff_ids)
    rows = query(f"""SELECT {period_expr} AS day,u.userid,u.name AS staff_name,
                            COUNT(*) AS message_count,COUNT(DISTINCT m.roomid) AS service_groups
                     FROM messages m JOIN wecom_users u ON u.userid=m.from_user
                     WHERE m.msgtime >= %s AND m.msgtime < %s AND m.roomid IS NOT NULL
                       AND u.userid = ANY(%s::text[])
                     GROUP BY day,u.userid,u.name ORDER BY day DESC,message_count DESC LIMIT 5000""", params)
    daily = query("""SELECT to_char(to_timestamp(m.msgtime/1000.0) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') AS day,
                            u.userid,u.name AS staff_name,COUNT(*) AS message_count,
                            COUNT(DISTINCT m.roomid) AS service_groups
                     FROM messages m JOIN wecom_users u ON u.userid=m.from_user
                     WHERE m.msgtime >= %s AND m.msgtime < %s AND m.roomid IS NOT NULL
                       AND u.userid = ANY(%s::text[])
                     GROUP BY day,u.userid,u.name ORDER BY day,u.name""", params)
    rule, rule_params = official_group_rule()
    distribution = query(f"""SELECT u.userid,u.name AS staff_name,COUNT(*) AS message_count,
                                   COUNT(DISTINCT m.roomid) FILTER (WHERE {rule}) AS official_groups
                            FROM messages m JOIN wecom_users u ON u.userid=m.from_user
                            LEFT JOIN wecom_rooms r ON r.roomid=m.roomid
                            WHERE m.msgtime >= %s AND m.msgtime < %s AND m.roomid IS NOT NULL
                              AND u.userid = ANY(%s::text[])
                            GROUP BY u.userid,u.name ORDER BY message_count DESC,u.name""", (*rule_params, *params))
    peaks = {}
    for item in daily:
        peaks[item["userid"]] = max(peaks.get(item["userid"], 0), item["message_count"])
    for item in rows:
        item["max_daily_messages"] = peaks.get(item["userid"], 0)
        item["daily_average"] = round(item["message_count"] / max(1, len({d["day"] for d in daily if d["userid"] == item["userid"]})), 1) if dimension == "month" else item["message_count"]
    return jsonify({"summary": {"messages": sum(r["message_count"] for r in rows),
                                "staff": len({r["userid"] for r in rows})},
                    "rows": rows, "daily": daily, "distribution": distribution,
                    "note": ("客服范围来自设置中自定义的人员；" if source == "custom" else "客服范围来自默认部门编号；") + "消息量只统计员工本人发送的已存档群消息；回复官群数按所选期间发言过的官群去重。"})


@app.get("/api/stats/conversation")
@require_login()
@cached_read
def conversation_stats():
    period = stats_range()
    if not period:
        return jsonify({"error": "日期范围无效或超过一年"}), 400
    start, end = period
    staff = request.args.get("employee", "").strip()[:100]
    condition = "msgtime >= %s AND msgtime < %s"
    params = [start, end]
    if staff:
        condition += " AND from_user = %s"
        params.append(staff)
    summary = query(f"""SELECT COUNT(*) AS total_messages,
                              COUNT(*) FILTER (WHERE roomid IS NULL) AS direct_messages,
                              COUNT(*) FILTER (WHERE roomid IS NOT NULL) AS group_messages,
                              COUNT(DISTINCT roomid) AS active_groups,
                              COUNT(DISTINCT from_user) AS senders,
                              COUNT(*) FILTER (WHERE msgtype IN ('file','qydiskfile','voip_doc_share')) AS file_messages,
                              COUNT(*) FILTER (WHERE msgtype IN ('image','video','emotion')) AS media_messages,
                              COUNT(DISTINCT from_user) FILTER (WHERE from_user IN (SELECT userid FROM wecom_users)) AS active_employees
                       FROM messages WHERE {condition}""", params)[0]
    daily = query(f"""SELECT to_char(to_timestamp(msgtime/1000.0) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') AS day,
                             COUNT(*) AS value FROM messages WHERE {condition}
                      GROUP BY day ORDER BY day""", params)
    return jsonify({"summary": summary, "daily": daily, "start_ms": start, "end_ms": end})


@app.get("/api/stats/customer")
@require_login()
@cached_read
def customer_stats():
    period = stats_range()
    if not period:
        return jsonify({"error": "日期范围无效或超过一年"}), 400
    start, end = period
    staff = request.args.get("employee", "").strip()[:100]
    room_filter = "AND r.owner=%s" if staff else ""
    room_params = [staff] if staff else []
    members_sql = f"""WITH members AS (
       SELECT member->>'userid' AS userid,
              CASE WHEN (member->>'join_time') ~ '^[0-9]+$' THEN (member->>'join_time')::bigint ELSE NULL END AS joined
       FROM wecom_rooms r CROSS JOIN LATERAL jsonb_array_elements(r.raw_json->'group_chat'->'member_list') member
       WHERE r.source='external' {room_filter} AND member->>'type'='2')
       SELECT COUNT(*) AS customer_memberships,
              COUNT(DISTINCT userid) AS customers_in_groups,
              COUNT(DISTINCT userid) FILTER (WHERE joined >= %s AND joined < %s) AS joined_customers
       FROM members"""
    people = query(members_sql, [*room_params, start // 1000, end // 1000])[0]
    rooms = query(f"""SELECT COUNT(*) AS external_groups,
                           COUNT(*) FILTER (WHERE CASE WHEN (r.raw_json->'group_chat'->>'create_time') ~ '^[0-9]+$'
                             THEN (r.raw_json->'group_chat'->>'create_time')::bigint BETWEEN %s AND %s ELSE FALSE END) AS new_groups
                    FROM wecom_rooms r WHERE r.source='external' {room_filter}""",
                  [start // 1000, (end - 1) // 1000, *room_params])[0]
    active = query(f"""SELECT COUNT(DISTINCT m.roomid) AS active_groups FROM messages m
                     JOIN wecom_rooms r ON r.roomid=m.roomid
                     WHERE r.source='external' AND m.msgtime >= %s AND m.msgtime < %s {room_filter}""",
                   [start, end, *room_params])[0]
    first_join_sql = f"""WITH first_join AS (
                         SELECT member->>'userid' AS userid, MIN((member->>'join_time')::bigint) AS joined
                         FROM wecom_rooms r CROSS JOIN LATERAL jsonb_array_elements(r.raw_json->'group_chat'->'member_list') member
                         WHERE r.source='external' {room_filter} AND member->>'type'='2'
                           AND (member->>'join_time') ~ '^[0-9]+$'
                         GROUP BY member->>'userid')
                       SELECT COUNT(*) FILTER (WHERE joined < %s) AS baseline,
                              COUNT(*) FILTER (WHERE joined >= %s AND joined < %s) AS in_range
                       FROM first_join"""
    first_join = query(first_join_sql, [*room_params, start // 1000, start // 1000, end // 1000])[0]
    daily = query(f"""WITH first_join AS (
                       SELECT member->>'userid' AS userid, MIN((member->>'join_time')::bigint) AS joined
                       FROM wecom_rooms r CROSS JOIN LATERAL jsonb_array_elements(r.raw_json->'group_chat'->'member_list') member
                       WHERE r.source='external' {room_filter} AND member->>'type'='2'
                         AND (member->>'join_time') ~ '^[0-9]+$'
                       GROUP BY member->>'userid')
                      SELECT to_char(to_timestamp(joined) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') AS day,
                             COUNT(*) AS value FROM first_join
                      WHERE joined >= %s AND joined < %s GROUP BY day ORDER BY day""",
                  [*room_params, start // 1000, end // 1000])
    return jsonify({"summary": {**people, **rooms, **active}, "daily": daily,
                    "baseline": first_join["baseline"],
                    "start_ms": start, "end_ms": end,
                    "note": "客户数来自当前外部群成员；累计趋势只统计有入群时间的当前成员。流失客户和解散群需历史快照，当前库无法计算。"})


@app.get("/api/home")
@require_login()
@cached_read
def home_dashboard():
    period = stats_range()
    if not period:
        return jsonify({"error": "日期范围无效或超过一年"}), 400
    start, end = period
    summary = query("""SELECT COUNT(DISTINCT m.from_user) FILTER
                               (WHERE u.userid IS NULL AND r.source='external') AS communicating_customers,
                              COUNT(DISTINCT m.roomid) FILTER
                               (WHERE r.source='external') AS active_customer_groups,
                              COUNT(DISTINCT m.from_user) FILTER
                               (WHERE u.userid IS NOT NULL) AS active_employees,
                              COUNT(*) AS total_messages
                       FROM messages m LEFT JOIN wecom_users u ON u.userid=m.from_user
                       LEFT JOIN wecom_rooms r ON r.roomid=m.roomid
                       WHERE m.msgtime >= %s AND m.msgtime < %s""", (start, end))[0]
    daily = query("""SELECT to_char(to_timestamp(m.msgtime/1000.0) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') AS day,
                            COUNT(DISTINCT m.from_user) AS value
                     FROM messages m LEFT JOIN wecom_users u ON u.userid=m.from_user
                     JOIN wecom_rooms r ON r.roomid=m.roomid
                     WHERE m.msgtime >= %s AND m.msgtime < %s AND u.userid IS NULL AND r.source='external'
                     GROUP BY day ORDER BY day""", (start, end))
    employees = query("""SELECT u.userid,u.name,COUNT(m.id) AS sent_messages,
                                COUNT(DISTINCT m.roomid) AS active_groups
                         FROM wecom_users u JOIN messages m ON m.from_user=u.userid
                         WHERE m.msgtime >= %s AND m.msgtime < %s AND m.roomid IS NOT NULL
                         GROUP BY u.userid,u.name ORDER BY sent_messages DESC LIMIT 50""", (start, end))
    return jsonify({"summary": summary, "daily": daily, "employees": employees,
                    "start_ms": start, "end_ms": end,
                    "risk_count": None, "timeout_count": None,
                    "note": "沟通客户为期间在外部群发言的去重非员工；员工消息为本人发送的群消息。风险和超时尚无独立事件数据源。"})


@app.get("/api/file-groups")
@require_login()
@cached_read
def file_groups():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = request.args.get("start_ms", int((today - timedelta(days=6)).timestamp() * 1000), type=int)
    end_ms = request.args.get("end_ms", int((today + timedelta(days=1)).timestamp() * 1000), type=int)
    if start_ms >= end_ms or end_ms - start_ms > 366 * 86400000:
        return jsonify({"error": "文件日期范围无效"}), 400
    rooms = query("""SELECT r.roomid,COALESCE(NULLIF(r.room_name,''),r.roomid) AS room_name,r.source,
                            MAX(m.msgtime) AS latest_msgtime
                     FROM wecom_rooms r LEFT JOIN messages m ON m.roomid=r.roomid
                     GROUP BY r.roomid,r.room_name,r.source
                     ORDER BY latest_msgtime DESC NULLS LAST,r.room_name LIMIT 5000""")
    counts = query("""SELECT m.roomid,COUNT(DISTINCT m.id) AS message_count,COUNT(mf.id) AS file_count
                      FROM messages m LEFT JOIN media_files mf ON mf.msgid=m.msgid
                           AND mf.msgtype IN ('file','voip_doc_share')
                      WHERE m.roomid IS NOT NULL AND m.msgtime >= %s AND m.msgtime < %s
                      GROUP BY m.roomid""", (start_ms, end_ms))
    by_room = {r["roomid"]: r for r in counts}
    return jsonify([{**room, "message_count": by_room.get(room["roomid"], {}).get("message_count", 0),
                     "file_count": by_room.get(room["roomid"], {}).get("file_count", 0),
                     "source_label": "外部群" if room["source"] == "external" else "内部群"}
                    for room in rooms])


@app.get("/api/files")
@require_login()
def files():
    roomid = request.args.get("roomid", "").strip()
    if not roomid:
        return jsonify([])
    rows = query("""SELECT mf.id, mf.msgid, mf.msgtype, mf.filename, mf.file_name,
                                  mf.filesize, mf.download_status, mf.oss_url, m.msgtime, m.from_user,
                                  u.userid IS NOT NULL AS is_employee,
                                  COALESCE(NULLIF(u.name,''), NULLIF(m.from_user_name,''), m.from_user) AS sender_name
                           FROM media_files mf JOIN messages m ON m.msgid=mf.msgid
                           LEFT JOIN wecom_users u ON u.userid=m.from_user
                           WHERE m.roomid=%s AND mf.msgtype IN ('file','voip_doc_share')
                           ORDER BY m.msgtime DESC""", (roomid,))
    names = customer_name_map({r["from_user"] for r in rows if not r["is_employee"] and r["from_user"]})
    for row in rows:
        if not row.pop("is_employee"):
            row["sender_name"] = names.get(row["from_user"]) or row["sender_name"]
    return jsonify(rows)


def local_media_path(row):
    root_value = os.getenv("VIEWER_MEDIA_ROOT", "")
    if not root_value:
        return None
    root = Path(root_value).resolve()
    for field in ("filepath", "local_path"):
        value = row.get(field)
        if value:
            candidate = Path(value)
            path = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if path.is_file() and path.is_relative_to(root):
                return path
    return None


@app.post("/api/files/batch-download")
@require_login()
def batch_download_files():
    data = request.get_json(silent=True) or {}
    roomid, ids = data.get("roomid"), data.get("ids")
    if not isinstance(roomid, str) or not roomid or not isinstance(ids, list) or not 1 <= len(ids) <= 100 or not all(isinstance(i, int) and i > 0 for i in ids):
        return jsonify({"error": "请选择同一群内的 1 至 100 个文件"}), 400
    rows = query("""SELECT mf.id,mf.filepath,mf.local_path,mf.filename,mf.file_name
                    FROM media_files mf JOIN messages m ON m.msgid=mf.msgid
                    WHERE m.roomid=%s AND mf.id=ANY(%s) AND mf.msgtype IN ('file','voip_doc_share')""", (roomid, ids))
    if len(rows) != len(set(ids)):
        return jsonify({"error": "所选文件不属于当前群"}), 400
    archive = io.BytesIO()
    missing = []
    total_bytes = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            path = local_media_path(row)
            if not path:
                missing.append(f"{row['id']}: {row['file_name'] or row['filename'] or '未命名文件'}")
                continue
            total_bytes += path.stat().st_size
            if total_bytes > 200 * 1024 * 1024:
                return jsonify({"error": "所选文件超过 200 MB，请分批下载"}), 413
            filename = Path(row["file_name"] or row["filename"] or path.name).name
            zf.write(path, arcname=f"{row['id']}-{filename}")
        if missing:
            zf.writestr("未能打包的文件.txt", "以下文件尚未保存在服务器媒体目录，需在页面单独打开：\n" + "\n".join(missing))
    archive.seek(0)
    return send_file(archive, mimetype="application/zip", as_attachment=True, download_name="群文件.zip")


@app.get("/api/media/<int:media_id>")
@require_login()
def media(media_id):
    rows = query("SELECT filepath, local_path, filename, file_name, msgtype, oss_url FROM media_files WHERE id=%s", (media_id,))
    if not rows:
        abort(404)
    row = rows[0]
    root = Path(os.getenv("VIEWER_MEDIA_ROOT", "")).resolve() if os.getenv("VIEWER_MEDIA_ROOT") else None
    for field in ("filepath", "local_path") if root else ():
        value = row.get(field)
        if not value:
            continue
        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if path.is_file() and path.is_relative_to(root):
            name = row.get("file_name") or row.get("filename") or path.name
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            inline = mime.startswith(("image/", "audio/", "video/"))
            return send_file(path, mimetype=mime, as_attachment=not inline, download_name=name)
    url = row.get("oss_url")
    if url and urlparse(url).scheme in ("https", "http"):
        return redirect(url, code=302)
    abort(404)


@app.get("/api/media/<int:media_id>/play")
@require_login()
def play_audio(media_id):
    rows = query("SELECT filepath, local_path, filename, file_name, msgtype, oss_url FROM media_files WHERE id=%s", (media_id,))
    if not rows:
        abort(404)
    row = rows[0]
    if row["msgtype"] != "voice":
        abort(404)
    cache = DATA_DIR / "cache" / "audio"
    cache.mkdir(parents=True, exist_ok=True)
    output = cache / f"{media_id}.wav"
    if not output.is_file():
        root = Path(os.getenv("VIEWER_MEDIA_ROOT", "")).resolve() if os.getenv("VIEWER_MEDIA_ROOT") else None
        source = None
        if root:
            for field in ("filepath", "local_path"):
                value = row.get(field)
                if value:
                    candidate = Path(value)
                    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
                    if path.is_file() and path.is_relative_to(root):
                        source = path.read_bytes()
                        break
        if source is None:
            url = row.get("oss_url")
            if not url or urlparse(url).scheme != "https":
                abort(404)
            try:
                with urlopen(url, timeout=15) as remote:
                    source = remote.read(20 * 1024 * 1024 + 1)
            except OSError:
                abort(502, "语音文件读取失败")
        if len(source) > 20 * 1024 * 1024:
            abort(413, "语音文件过大")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", dir=cache, delete=False) as temp:
                temp_path = Path(temp.name)
            result = subprocess.run([get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                                     "-i", "pipe:0", "-ac", "1", "-ar", "16000", str(temp_path)],
                                    input=source, capture_output=True, timeout=45)
            if result.returncode or not temp_path.stat().st_size:
                app.logger.warning("语音转码失败 id=%s: %s", media_id, result.stderr[:300])
                abort(502, "语音转码失败")
            temp_path.replace(output)
        except subprocess.TimeoutExpired:
            abort(504, "语音转码超时")
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
    return send_file(output, mimetype="audio/wav", conditional=True)


def trusted_export_image_url(value):
    try:
        parsed = urlparse(value or "")
        port = parsed.port
    except ValueError:
        return None
    hosts = {host.strip().lower() for host in os.getenv("VIEWER_EXPORT_IMAGE_HOSTS", "apy-wecom.oss-cn-beijing.aliyuncs.com").split(",") if host.strip()}
    return value if parsed.scheme == "https" and parsed.hostname in hosts and not parsed.username and not parsed.password and port in (None, 443) else None


class NoExportRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def export_image_data(media_row):
    allowed_mimes = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
    path = local_media_path(media_row)
    if path and path.stat().st_size <= 15 * 1024 * 1024:
        mime = mimetypes.guess_type(media_row["file_name"] or media_row["filename"] or path.name)[0]
        if mime in allowed_mimes:
            return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    url = trusted_export_image_url(media_row.get("oss_url"))
    if not url:
        return None
    try:
        request_obj = Request(url, headers={"User-Agent": "ArchiveViewer/1.0"})
        with build_opener(NoExportRedirect()).open(request_obj, timeout=8) as remote:
            mime = remote.headers.get_content_type()
            if mime not in allowed_mimes:
                return None
            content = remote.read(15 * 1024 * 1024 + 1)
            if len(content) > 15 * 1024 * 1024:
                return None
            return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    except OSError:
        return None


@app.get("/api/export")
@require_login()
def export_conversation():
    conversation = request.args.get("conversation", "")
    if conversation.startswith("room:"):
        where, params = "roomid=%s", [conversation[5:]]
    elif conversation.startswith("direct:"):
        try:
            people = json.loads(conversation[7:])
        except ValueError:
            people = []
        if not isinstance(people, list) or len(people) != 2 or not all(isinstance(p, str) for p in people):
            return jsonify({"error": "无效会话"}), 400
        a, b = people
        where = "roomid IS NULL AND ((from_user=%s AND tolist::jsonb @> %s::jsonb) OR (from_user=%s AND tolist::jsonb @> %s::jsonb))"
        params = [a, json.dumps([b]), b, json.dumps([a])]
    else:
        return jsonify({"error": "请选择会话"}), 400
    start_date, end_date = request.args.get("start_date", ""), request.args.get("end_date", "")
    if start_date or end_date:
        try:
            start_day = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) if start_date else None
            end_day = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) if end_date else None
        except ValueError:
            return jsonify({"error": "导出日期格式无效"}), 400
        if start_day and end_day and start_day > end_day:
            return jsonify({"error": "结束日期不能早于开始日期"}), 400
        if start_day:
            where += " AND msgtime >= %s"
            params.append(int(start_day.timestamp() * 1000))
        if end_day:
            where += " AND msgtime < %s"
            params.append(int((end_day + timedelta(days=1)).timestamp() * 1000))
    export_format = request.args.get("format", "html")
    if export_format not in ("html", "csv", "json"):
        return jsonify({"error": "无效导出格式"}), 400
    if export_format in ("html", "json"):
        rows = query(f"""SELECT m.msgid,m.action,m.msgtime,m.msgtype,m.content,m.raw_json,m.from_user,
                                COALESCE(NULLIF(u.name,''),NULLIF(m.from_user_name,''),m.from_user) AS sender_name,
                                u.userid IS NOT NULL AS is_employee
                         FROM messages m LEFT JOIN wecom_users u ON u.userid=m.from_user
                         WHERE {where} ORDER BY m.msgtime,m.id""", params)
        names = customer_name_map({r["from_user"] for r in rows if not r["is_employee"] and r["from_user"]})
        msgids = [r["msgid"] for r in rows]
        media_by_msg = {}
        for start in range(0, len(msgids), 1000):
            for media_row in query("""SELECT id,msgid,msgtype,filepath,local_path,filename,file_name,oss_url
                                      FROM media_files WHERE msgid=ANY(%s)""", (msgids[start:start+1000],)):
                media_by_msg.setdefault(media_row["msgid"], []).append(media_row)
        recalled_ids = set()
        for start in range(0, len(msgids), 1000):
            recalled_ids.update(r["pre_msgid"] for r in query("""SELECT raw_json::jsonb->'revoke'->>'pre_msgid' AS pre_msgid
                                FROM messages WHERE msgtype='revoke' AND raw_json::jsonb->'revoke'->>'pre_msgid'=ANY(%s)""", (msgids[start:start+1000],)))
        exported = []
        embedded_image_bytes = 0
        for row in rows:
            try:
                payload = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            original = payload.get("text", {}).get("content") if isinstance(payload.get("text"), dict) else None
            content = original if row["msgtype"] == "text" and isinstance(original, str) else row["content"] or ""
            if row["msgtype"] == "revoke":
                content = "撤回了一条消息"
            elif row["msgtype"] in ("image", "emotion", "voice", "video", "qydiskfile") and (content.lstrip().startswith("{") or "sdkfileid" in content):
                content = ""
            quote = None
            match = re.match(r"^这是一条引用/回复消息[：:]\s*\r?\n(.*?)\r?\n-{4,}\r?\n(.*)$", content, re.S)
            if match:
                quoted = match.group(1).strip().strip('“”"')
                author_match = re.match(r"^([^\n：:]+)[：:]\s*\n(.*)$", quoted, re.S)
                quote = {"sender": author_match.group(1) if author_match else "", "content": author_match.group(2) if author_match else quoted}
                content = match.group(2).strip()
            attachments = []
            for media_row in media_by_msg.get(row["msgid"], []):
                filename = media_row["file_name"] or media_row["filename"] or "文件"
                attachment = {"type": media_row["msgtype"], "name": filename, "data": None, "url": None}
                if media_row["msgtype"] in ("image", "emotion"):
                    attachment["url"] = trusted_export_image_url(media_row.get("oss_url"))
                    if embedded_image_bytes < 200 * 1024 * 1024:
                        attachment["data"] = export_image_data(media_row)
                        if attachment["data"]:
                            embedded_image_bytes += len(attachment["data"]) * 3 // 4
                attachments.append(attachment)
            name = names.get(row["from_user"]) if not row["is_employee"] else None
            exported.append({"id": row["msgid"], "sender": {"id": row["from_user"], "name": name or row["sender_name"] or row["from_user"] or "未知用户"},
                             "timestamp": datetime.fromtimestamp(row["msgtime"] / 1000, ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds") if row["msgtime"] else "",
                             "type": row["msgtype"], "content": content, "quote": quote, "attachments": attachments,
                             "recalled": row["msgid"] in recalled_ids or row["action"] in ("recall", "revoke")})
        title = conversation[5:] if conversation.startswith("room:") else "双人会话"
        if conversation.startswith("room:"):
            group = query("SELECT room_name FROM wecom_rooms WHERE roomid=%s", (conversation[5:],))
            if group and group[0]["room_name"]:
                title = group[0]["room_name"]
        if export_format == "json":
            content = json.dumps({"version": 1, "title": title, "messages": exported}, ensure_ascii=False)
            return Response(content, mimetype="application/json; charset=utf-8", headers={"Content-Disposition": "attachment; filename=conversation.json", "Cache-Control": "no-store"})
        html = render_template("export_chat.html", title=title, messages=exported)
        return Response(html, mimetype="text/html; charset=utf-8", headers={"Content-Disposition": "attachment; filename=conversation.html", "Cache-Control": "no-store"})
    query("SELECT 1 FROM messages LIMIT 1")
    def stream():
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["消息ID", "时间戳", "发送人", "接收人", "群ID", "类型", "内容", "原始JSON"])
        yield '\ufeff' + out.getvalue()
        with psycopg.connect(**db_config(), options="-c default_transaction_read_only=on", connect_timeout=4) as conn:
            with conn.cursor(name="archive_export", row_factory=dict_row) as cur:
                cur.execute(f"""SELECT msgid,msgtime,from_user,tolist,roomid,msgtype,
                                      COALESCE(content,'') AS content,raw_json
                               FROM messages WHERE {where} ORDER BY msgtime,id""", params)
                for row in cur:
                    out.seek(0); out.truncate(0)
                    writer.writerow([row.get(k) for k in ("msgid","msgtime","from_user","tolist","roomid","msgtype","content","raw_json")])
                    yield out.getvalue()
    return Response(stream(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=conversation.csv"})


if __name__ == "__main__":
    if not app.secret_key:
        raise SystemExit("请先在 .env 中配置 VIEWER_SECRET_KEY")
    bootstrap_admin()
    app.run(host="127.0.0.1", port=int(os.getenv("VIEWER_PORT", "8765")), debug=False)
