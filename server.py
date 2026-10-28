#!/usr/bin/env python3
"""Palabra Viva server: serves index.html, stores each learner's state in sqlite,
and proxies Claude so the browser never holds an API key.

    python3 server.py                 run the server (PORT, default 8080)
    python3 server.py adduser NAME    create a learner and print their login link
    python3 server.py link NAME       print another login link for NAME (old ones keep working)
    python3 server.py revoke NAME     sign NAME out everywhere (drops all their links)
    python3 server.py users           list learners with status, word counts and last use
    python3 server.py admin NAME      let NAME use the admin view (approve account requests, manage links)
    python3 server.py feedback        print every bug report and idea as JSON Lines, oldest first
    python3 server.py export NAME     print NAME's state as a backup (same shape as the app's Copy backup)
    python3 server.py import NAME     replace NAME's state from a backup on stdin

Environment: PV_DB (sqlite path), PV_PUBLIC (public URL of the app, for links),
PV_BASE (path prefix behind the proxy, for the cookie), ANTHROPIC_API_KEY,
PV_AI_DAILY (Claude calls per learner per day), ELEVENLABS_API_KEY,
PV_TTS_WORD_MODEL / PV_TTS_LONG_MODEL (ElevenLabs models for single words / sentences).
"""
import gzip
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("PV_DB", os.path.join(HERE, "data", "palabra.db"))
PUBLIC = os.environ.get("PV_PUBLIC", "http://localhost:8080").rstrip("/")
BASE = os.environ.get("PV_BASE", "").rstrip("/")
AI_DAILY = int(os.environ.get("PV_AI_DAILY", "200"))
COOKIE = "pv_token"
LOG_RETURN = 3000
KINDS = {"word", "card", "story", "meta"}

# modelTier from the app → (model, effort, extra request fields)
MODELS = {
    "quick": ("claude-haiku-5-5", "low", {}),
    "default": ("claude-sonnet-5-5", "medium", {
        "extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
        "extra_body": {"fallbacks": "default"},
    }),
}

_claude = None


def claude():
    global _claude
    if _claude is None and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        _claude = anthropic.Anthropic()
    return _claude


def now_ms():
    return int(time.time() * 1000)


# ---------- ElevenLabs text-to-speech ----------

TTS_DIR = os.path.join(os.path.dirname(DB), "tts")
TTS_WORD_MODEL = os.environ.get("PV_TTS_WORD_MODEL", "eleven_flash_v2_5")
TTS_LONG_MODEL = os.environ.get("PV_TTS_LONG_MODEL", "eleven_multilingual_v2")
TTS_WORD_MAX = 40          # up to this many characters counts as a word or short phrase
TTS_ACCENTS = ("latin american", "mexican", "colombian")
SPEEDS = {"1": 1.0, "0.85": 0.85, "0.7": 0.7}
_voices = {"at": 0, "list": []}
_voices_lock = threading.Lock()
_blocked = {}               # voice id → time until which ElevenLabs is known to refuse it


def eleven(path, body=None):
    req = urllib.request.Request("https://api.elevenlabs.io" + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "content-type": "application/json"})
    return urllib.request.urlopen(req, timeout=60)


def voices():
    """Latin American Spanish voices: the account's own Spanish voices, then the most used library voices per accent. Cached a day."""
    with _voices_lock:
        if _voices["list"] and time.time() - _voices["at"] < 86400:
            return _voices["list"]
        out, seen = [], set()
        try:
            for v in json.load(eleven("/v2/voices?page_size=100")).get("voices", []):
                lab = v.get("labels") or {}
                if lab.get("language") == "es" and v["voice_id"] not in seen:
                    seen.add(v["voice_id"])
                    out.append({"id": v["voice_id"], "name": v["name"], "accent": lab.get("accent", ""), "gender": lab.get("gender", ""), "preview": v.get("preview_url") or ""})
            for acc in TTS_ACCENTS:
                q = urllib.parse.urlencode({"page_size": 6, "language": "es", "accent": acc, "sort": "usage_character_count_1y"})
                for v in json.load(eleven("/v1/shared-voices?" + q)).get("voices", []):
                    if v["voice_id"] not in seen:
                        seen.add(v["voice_id"])
                        out.append({"id": v["voice_id"], "name": v["name"], "accent": v.get("accent", ""), "gender": v.get("gender", ""), "preview": v.get("preview_url") or ""})
        except (urllib.error.URLError, KeyError, ValueError) as e:
            sys.stderr.write(f"voices: {e}\n")
            return _voices["list"]
        _voices.update(at=time.time(), list=out)
        return out


def tts(text, voice, speed):
    """Return (mp3 bytes, characters billed). Each voice/model/speed/text is generated once and kept on disk."""
    short = len(text) <= TTS_WORD_MAX
    model = TTS_WORD_MODEL if short else TTS_LONG_MODEL
    key = hashlib.sha256(f"{voice}|{model}|{speed}|{text}".encode()).hexdigest()
    path = os.path.join(TTS_DIR, key[:2], key + ".mp3")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read(), 0
    body = {"text": text, "model_id": model, "voice_settings": {"speed": speed}}
    if model in ("eleven_flash_v2_5", "eleven_turbo_v2_5"):
        body["language_code"] = "es"
    with eleven(f"/v1/text-to-speech/{urllib.parse.quote(voice)}?output_format=mp3_44100_64", body) as r:
        audio = r.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(audio)
    os.replace(path + ".tmp", path)
    return audio, len(text)


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = db()
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created INTEGER NOT NULL, stamp INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created INTEGER NOT NULL, last_used INTEGER);
    CREATE TABLE IF NOT EXISTS items (user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, id TEXT NOT NULL, json TEXT NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY (user_id, kind, id));
    CREATE TABLE IF NOT EXISTS log (user_id INTEGER NOT NULL REFERENCES users(id), t INTEGER NOT NULL, json TEXT NOT NULL, PRIMARY KEY (user_id, t));
    CREATE TABLE IF NOT EXISTS ai_usage (user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, in_tok INTEGER NOT NULL DEFAULT 0, out_tok INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, day));
    CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, text TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}', created INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', closed INTEGER);
    """)
    if "tts_chars" not in [r[1] for r in con.execute("PRAGMA table_info(ai_usage)")]:
        con.execute("ALTER TABLE ai_usage ADD COLUMN tts_chars INTEGER NOT NULL DEFAULT 0")
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    for col, decl in (("email", "TEXT"), ("status", "TEXT NOT NULL DEFAULT 'active'"),  # pending | active | disabled
                      ("admin", "INTEGER NOT NULL DEFAULT 0"), ("note", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users (lower(email)) WHERE email IS NOT NULL")
    con.commit()
    con.close()


def token_hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def new_link(con, user_id):
    tok = secrets.token_urlsafe(24)
    con.execute("INSERT INTO tokens (hash, user_id, created) VALUES (?,?,?)", (token_hash(tok), user_id, now_ms()))
    return f"{PUBLIC}/#login={tok}"


def user_by_token(con, tok):
    if not tok:
        return None
    h = token_hash(tok)
    row = con.execute("SELECT u.id, u.name, u.admin FROM tokens t JOIN users u ON u.id = t.user_id "
                      "WHERE t.hash = ? AND u.status = 'active'", (h,)).fetchone()
    if row:
        con.execute("UPDATE tokens SET last_used = ? WHERE hash = ?", (now_ms(), h))
        con.commit()
    return row


def unique_name(con, want):
    base = "".join(c for c in want.strip() if c.isalnum() or c in " ._-")[:40].strip() or "learner"
    name, n = base, 2
    while con.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
        name, n = f"{base} {n}", n + 1
    return name


EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_requests = {}              # client address → recent account-request times


def request_allowed(addr):
    now = time.time()
    recent = [t for t in _requests.get(addr, []) if now - t < 3600]
    _requests[addr] = recent + [now]
    return len(recent) < 5


def admin_users(con):
    rows = con.execute("""
        SELECT u.id, u.name, u.email, u.status, u.admin, u.created, u.note,
               (SELECT COUNT(*) FROM items i WHERE i.user_id = u.id AND i.kind = 'word'),
               (SELECT MAX(last_used) FROM tokens t WHERE t.user_id = u.id),
               (SELECT COUNT(*) FROM tokens t WHERE t.user_id = u.id),
               (SELECT COALESCE(SUM(calls), 0) FROM ai_usage a WHERE a.user_id = u.id),
               (SELECT COALESCE(SUM(tts_chars), 0) FROM ai_usage a WHERE a.user_id = u.id)
        FROM users u ORDER BY u.status = 'pending' DESC, u.created DESC""").fetchall()
    keys = ("id", "name", "email", "status", "admin", "created", "note", "words", "last_seen", "links", "ai_calls", "voice_chars")
    return [dict(zip(keys, r)) for r in rows]


def admin_feedback(con):
    rows = con.execute("""SELECT f.id, f.kind, f.text, f.context, f.created, f.status, f.closed, u.name
                          FROM feedback f JOIN users u ON u.id = f.user_id ORDER BY f.created DESC""").fetchall()
    return [{"id": i, "kind": k, "text": t, "context": json.loads(c), "created": cr, "status": s, "closed": cl, "name": n}
            for i, k, t, c, cr, s, cl, n in rows]


def user_by_name(con, name):
    row = con.execute("SELECT id, name FROM users WHERE name = ?", (name,)).fetchone()
    if not row:
        sys.exit(f"no such user: {name}")
    return row


# ---------- state <-> rows ----------

def load_state(con, uid):
    S = {"words": {}, "cards": {}, "stories": [], "log": []}
    rows = con.execute("SELECT kind, id, json FROM items WHERE user_id = ?", (uid,)).fetchall()
    for kind, id_, js in rows:
        v = json.loads(js)
        if kind == "word":
            S["words"][id_] = v
        elif kind == "card":
            S["cards"][id_] = v
        elif kind == "story":
            S["stories"].append(v)
        elif kind == "meta":
            S[id_] = v
    S["stories"].sort(key=lambda s: (s.get("created") or 0, s.get("id") or ""))
    logs = con.execute("SELECT json FROM log WHERE user_id = ? ORDER BY t DESC LIMIT ?", (uid, LOG_RETURN)).fetchall()
    S["log"] = [json.loads(js) for (js,) in reversed(logs)]
    stamp = con.execute("SELECT stamp FROM users WHERE id = ?", (uid,)).fetchone()[0]
    return S, stamp, not rows


def state_items(S):
    """Split an app state object into (kind, id, json) rows, the same way the client does."""
    out = []
    for k, w in (S.get("words") or {}).items():
        out.append(("word", k, json.dumps(w, ensure_ascii=False)))
    for k, c in (S.get("cards") or {}).items():
        out.append(("card", k, json.dumps(c, ensure_ascii=False)))
    for st in S.get("stories") or []:
        if isinstance(st, dict) and st.get("id"):
            out.append(("story", str(st["id"]), json.dumps(st, ensure_ascii=False)))
    for k in ("settings", "newDay", "adv"):
        if k in S:
            out.append(("meta", k, json.dumps(S[k], ensure_ascii=False)))
    return out


def bump(con, uid):
    stamp = now_ms()
    con.execute("UPDATE users SET stamp = MAX(stamp + 1, ?) WHERE id = ?", (stamp, uid))
    return con.execute("SELECT stamp FROM users WHERE id = ?", (uid,)).fetchone()[0]


def apply_sync(con, uid, body):
    ts = now_ms()
    for kind, id_, js in body.get("put") or []:
        if kind not in KINDS:
            raise ValueError(f"bad kind {kind!r}")
        json.loads(js)
        con.execute("INSERT INTO items (user_id, kind, id, json, updated) VALUES (?,?,?,?,?) "
                    "ON CONFLICT (user_id, kind, id) DO UPDATE SET json = excluded.json, updated = excluded.updated",
                    (uid, kind, str(id_), js, ts))
    for kind, id_ in body.get("del") or []:
        con.execute("DELETE FROM items WHERE user_id = ? AND kind = ? AND id = ?", (uid, kind, str(id_)))
    for entry in body.get("log") or []:
        con.execute("INSERT OR IGNORE INTO log (user_id, t, json) VALUES (?,?,?)",
                    (uid, int(entry["t"]), json.dumps(entry, ensure_ascii=False)))
    return bump(con, uid)


def replace_state(con, uid, S):
    if not isinstance(S, dict) or not isinstance(S.get("words"), dict):
        raise ValueError("not a Palabra Viva state")
    ts = now_ms()
    con.execute("DELETE FROM items WHERE user_id = ?", (uid,))
    con.execute("DELETE FROM log WHERE user_id = ?", (uid,))
    con.executemany("INSERT INTO items (user_id, kind, id, json, updated) VALUES (?,?,?,?,?)",
                    [(uid, k, i, js, ts) for k, i, js in state_items(S)])
    con.executemany("INSERT OR IGNORE INTO log (user_id, t, json) VALUES (?,?,?)",
                    [(uid, int(e["t"]), json.dumps(e, ensure_ascii=False)) for e in S.get("log") or [] if isinstance(e, dict) and "t" in e])
    return bump(con, uid)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "palabra-viva"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 6)
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def set_cookie(self, tok, max_age):
        return {"Set-Cookie": f"{COOKIE}={tok}; Path={BASE or ''}/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"}

    def read_json(self):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ValueError("expected application/json")
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            # the edge proxy defaults to autoplay=(), which leaves audio silently paused
            self.send_header("Permissions-Policy", "autoplay=(self), microphone=(self)")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/health":
            return self.send_json({"ok": True})
        if not path.startswith("/api/"):
            return self.send_json({"error": "not_found"}, 404)
        con = db()
        try:
            user = user_by_token(con, self.cookie_token())
            if path == "/api/me":
                if not user:
                    return self.send_json({"error": "signed_out"}, 401)
                me = {"name": user[1], "ai": bool(os.environ.get("ANTHROPIC_API_KEY")),
                      "tts": bool(os.environ.get("ELEVENLABS_API_KEY")), "admin": bool(user[2])}
                if user[2]:
                    me["pending"] = con.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'").fetchone()[0]
                    me["reports"] = con.execute("SELECT COUNT(*) FROM feedback WHERE status = 'open'").fetchone()[0]
                return self.send_json(me)
            if not user:
                return self.send_json({"error": "signed_out"}, 401)
            if path == "/api/admin/users":
                if not user[2]:
                    return self.send_json({"error": "forbidden"}, 403)
                return self.send_json({"users": admin_users(con), "feedback": admin_feedback(con)})
            if path == "/api/voices":
                return self.send_json({"voices": voices() if os.environ.get("ELEVENLABS_API_KEY") else []})
            if path == "/api/tts":
                return self.speech(con, user)
            if path == "/api/state":
                S, stamp, empty = load_state(con, user[0])
                return self.send_json({"S": S, "stamp": stamp, "empty": empty})
            if path == "/api/stamp":
                return self.send_json({"stamp": con.execute("SELECT stamp FROM users WHERE id = ?", (user[0],)).fetchone()[0]})
            self.send_json({"error": "not_found"}, 404)
        finally:
            con.close()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self.read_json()
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        con = db()
        try:
            if path == "/api/login":
                user = user_by_token(con, str(body.get("token") or ""))
                if not user:
                    return self.send_json({"error": "bad_link"}, 401)
                return self.send_json({"name": user[1]}, headers=self.set_cookie(body["token"], 10 * 365 * 86400))
            if path == "/api/logout":
                return self.send_json({"ok": True}, headers=self.set_cookie("", 0))
            if path == "/api/request":
                return self.request_account(con, body)
            user = user_by_token(con, self.cookie_token())
            if not user:
                return self.send_json({"error": "signed_out"}, 401)
            uid = user[0]
            if path.startswith("/api/admin/"):
                if not user[2]:
                    return self.send_json({"error": "forbidden"}, 403)
                return self.admin_action(con, user, path.rsplit("/", 1)[1], body)
            if path == "/api/feedback":
                kind, text = body.get("kind"), str(body.get("text") or "").strip()
                context = json.dumps(body.get("context") if isinstance(body.get("context"), dict) else {}, ensure_ascii=False)
                if kind not in ("bug", "idea") or not text or len(text) > 4000 or len(context) > 4000:
                    return self.send_json({"error": "bad_request"}, 400)
                with con:
                    today = con.execute("SELECT COUNT(*) FROM feedback WHERE user_id = ? AND created > ?", (uid, now_ms() - 86400000)).fetchone()[0]
                    if today >= 50:
                        return self.send_json({"error": "rate_limited"}, 429)
                    con.execute("INSERT INTO feedback (user_id, kind, text, context, created) VALUES (?,?,?,?,?)", (uid, kind, text, context, now_ms()))
                self.log_message("feedback from %s: %s", user[1], kind)
                return self.send_json({"ok": True})
            if path == "/api/sync":
                try:
                    with con:
                        stamp = apply_sync(con, uid, body)
                except (ValueError, KeyError, TypeError) as e:
                    return self.send_json({"error": str(e)}, 400)
                return self.send_json({"stamp": stamp})
            if path == "/api/restore":
                try:
                    with con:
                        stamp = replace_state(con, uid, body.get("S"))
                except (ValueError, KeyError, TypeError) as e:
                    return self.send_json({"error": str(e)}, 400)
                S, stamp, _ = load_state(con, uid)
                return self.send_json({"S": S, "stamp": stamp})
            if path == "/api/ai":
                return self.ai(con, user, body)
            self.send_json({"error": "not_found"}, 404)
        finally:
            con.close()

    def request_account(self, con, body):
        """Anyone may ask for an account; it waits as 'pending' until an admin activates it.
        The reply is the same whether or not the email is already known."""
        email = str(body.get("email") or "").strip()[:200]
        if not EMAIL_OK.match(email):
            return self.send_json({"error": "bad_email"}, 400)
        if not request_allowed(self.headers.get("X-Forwarded-For") or self.client_address[0]):
            return self.send_json({"error": "rate_limited"}, 429)
        with con:
            known = con.execute("SELECT 1 FROM users WHERE lower(email) = lower(?)", (email,)).fetchone()
            pending = con.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'").fetchone()[0]
            if not known and pending < 100:
                name = unique_name(con, str(body.get("name") or "") or email.split("@")[0])
                con.execute("INSERT INTO users (name, email, status, note, created) VALUES (?,?,?,?,?)",
                            (name, email, "pending", str(body.get("note") or "")[:500], now_ms()))
                self.log_message("account request: %s <%s>", name, email)
        return self.send_json({"ok": True})

    def admin_action(self, con, admin, action, body):
        if action in ("close", "reopen"):
            with con:
                n = con.execute("UPDATE feedback SET status = ?, closed = ? WHERE id = ?",
                                ("closed" if action == "close" else "open", now_ms() if action == "close" else None, body.get("id"))).rowcount
            if not n:
                return self.send_json({"error": "no_such_report"}, 404)
            return self.send_json({"feedback": admin_feedback(con)})
        uid = body.get("id")
        row = con.execute("SELECT id, name, email, status FROM users WHERE id = ?", (uid,)).fetchone() if uid is not None else None
        with con:
            if action == "add":
                email = str(body.get("email") or "").strip() or None
                if email and not EMAIL_OK.match(email):
                    return self.send_json({"error": "bad_email"}, 400)
                if email and con.execute("SELECT 1 FROM users WHERE lower(email) = lower(?)", (email,)).fetchone():
                    return self.send_json({"error": "email_taken"}, 409)
                name = unique_name(con, str(body.get("name") or "") or (email or "").split("@")[0])
                new = con.execute("INSERT INTO users (name, email, status, created) VALUES (?,?,'active',?)", (name, email, now_ms())).lastrowid
                return self.send_json({"link": new_link(con, new), "id": new, "users": admin_users(con)})
            if not row:
                return self.send_json({"error": "no_such_user"}, 404)
            if row[0] == admin[0] and action in ("disable", "revoke", "decline"):
                return self.send_json({"error": "not_yourself"}, 400)
            out = {}
            if action in ("activate", "enable"):
                con.execute("UPDATE users SET status = 'active' WHERE id = ?", (row[0],))
                if action == "activate":
                    out["link"] = new_link(con, row[0])
            elif action == "link":
                if row[3] != "active":
                    return self.send_json({"error": "not_active"}, 400)
                out["link"] = new_link(con, row[0])
            elif action == "revoke":
                con.execute("DELETE FROM tokens WHERE user_id = ?", (row[0],))
            elif action == "disable":
                con.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (row[0],))
            elif action == "decline":
                if row[3] != "pending":
                    return self.send_json({"error": "not_pending"}, 400)
                con.execute("DELETE FROM users WHERE id = ?", (row[0],))
            elif action == "email":
                email = str(body.get("email") or "").strip() or None
                if email and not EMAIL_OK.match(email):
                    return self.send_json({"error": "bad_email"}, 400)
                try:
                    con.execute("UPDATE users SET email = ? WHERE id = ?", (email, row[0]))
                except sqlite3.IntegrityError:
                    return self.send_json({"error": "email_taken"}, 409)
            else:
                return self.send_json({"error": "not_found"}, 404)
        out["users"] = admin_users(con)
        return self.send_json(out)

    def speech(self, con, user):
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return self.send_json({"error": "tts_off"}, 503)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        text = " ".join((q.get("t") or [""])[0].split())
        voice = (q.get("v") or [""])[0]
        speed = SPEEDS.get((q.get("s") or ["1"])[0], 1.0)
        if not text or len(text) > 1500 or not voice.isalnum():
            return self.send_json({"error": "bad_request"}, 400)
        if _blocked.get(voice, 0) > time.time():
            return self.send_json({"error": "upstream_error", "status": 402}, 502)
        try:
            audio, billed = tts(text, voice, speed)
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            self.log_message("elevenlabs %s: %s", e.code, detail)
            if e.code in (401, 402):  # plan doesn't allow this voice, or quota used up: stop asking for a while
                _blocked[voice] = time.time() + 600
            return self.send_json({"error": "upstream_error", "status": e.code}, 502)
        except (urllib.error.URLError, TimeoutError) as e:
            self.log_message("elevenlabs: %s", e)
            return self.send_json({"error": "upstream_error"}, 502)
        if billed:
            with con:
                con.execute("INSERT INTO ai_usage (user_id, day) VALUES (?,?) ON CONFLICT DO NOTHING", (user[0], date.today().isoformat()))
                con.execute("UPDATE ai_usage SET tts_chars = tts_chars + ? WHERE user_id = ? AND day = ?", (billed, user[0], date.today().isoformat()))
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def ai(self, con, user, body):
        client = claude()
        if not client:
            return self.send_json({"error": "ai_off"}, 503)
        prompt = str(body.get("prompt") or "")
        if not prompt or len(prompt) > 60000:
            return self.send_json({"error": "bad_prompt"}, 400)
        uid, day = user[0], date.today().isoformat()
        with con:
            con.execute("INSERT INTO ai_usage (user_id, day) VALUES (?,?) ON CONFLICT DO NOTHING", (uid, day))
            calls = con.execute("SELECT calls FROM ai_usage WHERE user_id = ? AND day = ?", (uid, day)).fetchone()[0]
            if calls >= AI_DAILY:
                return self.send_json({"error": "rate_limited"}, 429)
            con.execute("UPDATE ai_usage SET calls = calls + 1 WHERE user_id = ? AND day = ?", (uid, day))
        tier = body.get("tier") if body.get("tier") in MODELS else "default"
        model, effort, extra = MODELS[tier]

        # One JSON object per line: {"t": text} as it streams, then {"done": stop_reason} or {"error": code}.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        import anthropic
        usage = None
        try:
            with client.messages.stream(model=model, max_tokens=32000, output_config={"effort": effort},
                                        messages=[{"role": "user", "content": prompt}], **extra) as stream:
                for text in stream.text_stream:
                    emit({"t": text})
                msg = stream.get_final_message()
                usage = msg.usage
                emit({"error": "refused"} if msg.stop_reason == "refusal" else {"done": msg.stop_reason})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except anthropic.RateLimitError:
            self.safe_emit(emit, {"error": "rate_limited"})
        except anthropic.APIStatusError as e:
            self.log_message("claude %s: %s", e.status_code, e.message)
            self.safe_emit(emit, {"error": "upstream_error"})
        except anthropic.APIConnectionError as e:
            self.log_message("claude connection: %s", e)
            self.safe_emit(emit, {"error": "upstream_error"})
        if usage:
            with con:
                con.execute("UPDATE ai_usage SET in_tok = in_tok + ?, out_tok = out_tok + ? WHERE user_id = ? AND day = ?",
                            (usage.input_tokens, usage.output_tokens, uid, day))
            self.log_message("ai %s %s in=%d out=%d", user[1], model, usage.input_tokens, usage.output_tokens)
        self.close_connection = True

    @staticmethod
    def safe_emit(emit, obj):
        try:
            emit(obj)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------- CLI ----------

def cli(args):
    con = db()
    cmd = args[0]
    if cmd == "adduser" and len(args) == 2:
        with con:
            cur = con.execute("INSERT INTO users (name, created) VALUES (?,?)", (args[1], now_ms()))
            print(new_link(con, cur.lastrowid))
    elif cmd == "link" and len(args) == 2:
        with con:
            print(new_link(con, user_by_name(con, args[1])[0]))
    elif cmd == "revoke" and len(args) == 2:
        with con:
            n = con.execute("DELETE FROM tokens WHERE user_id = ?", (user_by_name(con, args[1])[0],)).rowcount
        print(f"revoked {n} link(s)")
    elif cmd == "admin" and len(args) == 2:
        with con:
            con.execute("UPDATE users SET admin = 1, status = 'active' WHERE id = ?", (user_by_name(con, args[1])[0],))
        print(f"{args[1]} is an admin")
    elif cmd == "users":
        for u in admin_users(con):
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["last_seen"] / 1000)) if u["last_seen"] else "never"
            flag = u["status"] + (" admin" if u["admin"] else "")
            print(f"{u['name']:16} {flag:14} {u['email'] or '':28} {u['words']:5} words  {u['ai_calls']:5} ai calls  {u['voice_chars']:7} voice chars  last seen {seen}")
    elif cmd == "feedback":
        for f in reversed(admin_feedback(con)):
            print(json.dumps(f, ensure_ascii=False))
    elif cmd == "export" and len(args) == 2:
        S, _, _ = load_state(con, user_by_name(con, args[1])[0])
        S["log"] = [json.loads(js) for (js,) in con.execute("SELECT json FROM log WHERE user_id = ? ORDER BY t", (user_by_name(con, args[1])[0],))]
        print(json.dumps({"app": "palabraviva", "v": 1, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "S": S}, ensure_ascii=False))
    elif cmd == "import" and len(args) == 2:
        d = json.load(sys.stdin)
        with con:
            replace_state(con, user_by_name(con, args[1])[0], d.get("S", d))
        print(f"imported {len(d.get('S', d)['words'])} words")
    else:
        sys.exit(__doc__)


def main():
    init_db()
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listening on {port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
