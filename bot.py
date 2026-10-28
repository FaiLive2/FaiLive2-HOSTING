#!/usr/bin/env python3
"""
FaiLive2 Hosting Panel — single-file Flask application.
Runs inside Ubuntu PRoot on Android Termux (no Docker/LXC/systemd).
"""
import os, sys, re, json, time, sqlite3, secrets, shutil, signal, subprocess
import threading, traceback, platform, socket, shlex
from datetime import datetime
from functools import wraps
from pathlib import Path
from contextlib import contextmanager

try:
    import psutil
except Exception:
    psutil = None

from flask import (Flask, request, session, redirect, url_for, jsonify, abort,
                   Response, send_file, render_template_string, g)
from werkzeug.security import check_password_hash
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------
BASE_DIR    = Path(os.environ.get("FAI_BASE", Path(__file__).resolve().parent))
DATA_DIR    = BASE_DIR / "data"
LOGS_DIR    = BASE_DIR / "logs"
APPS_DIR    = BASE_DIR / "apps"
DB_FILE     = Path(os.environ.get("FAI_DB", DATA_DIR / "failive2.db"))
SECRET_FILE = Path(os.environ.get("FAI_SECRET_FILE", DATA_DIR / "secret.key"))
HOST        = os.environ.get("FAI_HOST", "127.0.0.1")
PORT        = int(os.environ.get("FAI_PORT", "8080"))
INIT_ONLY   = os.environ.get("FAI_INIT_ONLY") == "1"

for d in (DATA_DIR, LOGS_DIR, APPS_DIR):
    d.mkdir(parents=True, exist_ok=True)

def load_secret():
    if SECRET_FILE.exists():
        s = SECRET_FILE.read_text().strip()
        if s:
            return s
    s = secrets.token_hex(64)
    SECRET_FILE.write_text(s)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return s

# ---------------------------------------------------------------------------
# App + password hashing
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,
)
PH = PasswordHasher()

def hash_password(pw):
    return PH.hash(pw)

def verify_password(pw, stored):
    if not stored:
        return False
    if stored.startswith("$argon2"):
        try:
            PH.verify(stored, pw)
            return True
        except VerifyMismatchError:
            return False
        except Exception:
            return False
    try:
        return check_password_hash(stored, pw)
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

@contextmanager
def db():
    c = db_conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  email TEXT,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eggs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nest_id INTEGER NOT NULL REFERENCES nests(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  runtime TEXT NOT NULL,
  working_dir TEXT,
  startup TEXT NOT NULL,
  env TEXT DEFAULT '{}',
  required_files TEXT DEFAULT '[]',
  allowed_ports TEXT DEFAULT '[]',
  default_limits TEXT DEFAULT '{}',
  install_notes TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  egg_id INTEGER REFERENCES eggs(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  runtime TEXT NOT NULL,
  workdir TEXT NOT NULL,
  startup TEXT NOT NULL,
  env TEXT DEFAULT '{}',
  ports TEXT DEFAULT '[]',
  limits TEXT DEFAULT '{}',
  auto_restart INTEGER DEFAULT 0,
  max_retries INTEGER DEFAULT 3,
  pid INTEGER,
  status TEXT DEFAULT 'stopped',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  ip TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

DEFAULT_NESTS = [
    ("Minecraft", "Minecraft Java & Bedrock servers"),
    ("Discord Bots", "Discord bot runtimes"),
    ("Python Applications", "Generic Python apps"),
    ("Node.js Applications", "Node/JS apps"),
    ("Java Applications", "Generic Java apps"),
    ("PHP Applications", "PHP websites"),
    ("Web Applications", "Flask/FastAPI/Express"),
    ("Other Applications", "Anything else"),
]

DEFAULT_EGGS = [
    ("Nukkit-MOT", "Minecraft", "java", ".",
     "java -Xms512M -Xmx1024M -jar Nukkit-MOT-SNAPSHOT.jar",
     "{}", "[]", "[19132]", '{"memory_mb":1024}',
     "Place Nukkit-MOT-SNAPSHOT.jar in the workdir."),
    ("Paper", "Minecraft", "java", ".",
     "java -Xms512M -Xmx2048M -jar paper.jar nogui",
     "{}", "[]", "[25565]", '{"memory_mb":2048}', "PaperMC jar in workdir."),
    ("Vanilla Minecraft Java", "Minecraft", "java", ".",
     "java -Xms512M -Xmx2048M -jar server.jar nogui",
     "{}", "[]", "[25565]", '{"memory_mb":2048}', "Official server.jar."),
    ("Python Discord Bot", "Discord Bots", "python", ".",
     "python3 bot.py", "{}", "[]", "[]", "{}", "Python bot script."),
    ("Node.js Discord Bot", "Discord Bots", "node", ".",
     "node index.js", "{}", "[]", "[]", "{}", "Node Discord bot."),
    ("Flask Application", "Web Applications", "python", ".",
     "python3 app.py", "{}", "[]", "[5000]", "{}", "Flask app."),
    ("FastAPI Application", "Web Applications", "python", ".",
     "uvicorn main:app --host 127.0.0.1 --port 8000",
     "{}", "[]", "[8000]", "{}", "FastAPI app."),
    ("Express Application", "Web Applications", "node", ".",
     "node app.js", "{}", "[]", "[3000]", "{}", "Express app."),
    ("Generic Java Application", "Java Applications", "java", ".",
     "java -jar app.jar", "{}", "[]", "[]", "{}", "Custom Java jar."),
    ("PHP Website", "PHP Applications", "php", ".",
     "php -S 127.0.0.1:8000 -t .", "{}", "[]", "[8000]", "{}", "PHP built-in server."),
    ("Static Website", "Web Applications", "static", ".",
     "python3 -m http.server 8000", "{}", "[]", "[8000]", "{}", "Static files."),
]

def now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def audit(conn, action, detail="", user_id=None, username=None):
    try:
        conn.execute(
            "INSERT INTO audit(user_id,username,action,detail,ip,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, username or "", action, detail[:2000],
             request.remote_addr if request else "", now()))
    except Exception:
        pass

def init_db(seed=True):
    with db() as c:
        c.executescript(SCHEMA)
        if not seed:
            return
        existing = {r["name"] for r in c.execute("SELECT name FROM nests")}
        for n, d in DEFAULT_NESTS:
            if n not in existing:
                c.execute("INSERT INTO nests(name,description,created_at) VALUES(?,?,?)",
                          (n, d, now()))
        nest_ids = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM nests")}
        egg_names = {r["name"] for r in c.execute("SELECT name FROM eggs")}
        for (name, nest, rt, wd, start, env, rf, ports, lim, notes) in DEFAULT_EGGS:
            if name in egg_names:
                continue
            nid = nest_ids.get(nest)
            if not nid:
                continue
            c.execute("""INSERT INTO eggs(nest_id,name,description,runtime,working_dir,
                         startup,env,required_files,allowed_ports,default_limits,
                         install_notes,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (nid, name, "", rt, wd, start, env, rf, ports, lim, notes, now()))

def create_admin_from_env():
    user = os.environ.get("FAI_ADMIN_USER")
    pw   = os.environ.get("FAI_ADMIN_PASS")
    email = os.environ.get("FAI_ADMIN_EMAIL", "")
    if not (user and pw):
        return False
    with db() as c:
        if c.execute("SELECT id FROM users WHERE username=?", (user,)).fetchone():
            return True
        c.execute("""INSERT INTO users(username,email,password_hash,role,created_at)
                     VALUES(?,?,?,?,?)""",
                  (user, email, hash_password(pw), "admin", now()))
    return True

# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def csrf_token():
    t = session.get("_csrf")
    if not t:
        t = secrets.token_urlsafe(32)
        session["_csrf"] = t
    return t

def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    tok = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
    return tok and session.get("_csrf") and secrets.compare_digest(tok, session["_csrf"])

@app.before_request
def _csrf_guard():
    if request.path.startswith("/static") or request.path == "/api/csrf":
        return
    if not check_csrf():
        if request.path.startswith("/api/"):
            return jsonify(error="CSRF token invalid"), 400
        return "CSRF token invalid", 400

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    with db() as c:
        row = c.execute("SELECT id,username,email,role FROM users WHERE id=?",
                        (session["user_id"],)).fetchone()
    return dict(row) if row else None

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify(error="auth required"), 401
            return redirect(url_for("login_page", next=request.path))
        g.user = u
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify(error="auth required"), 401
            return redirect(url_for("login_page"))
        if u["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify(error="forbidden"), 403
            return "Forbidden", 403
        g.user = u
        return f(*a, **kw)
    return w

# ---------------------------------------------------------------------------
# Process manager
# ---------------------------------------------------------------------------
PROC = {}
PROC_LOCK = threading.Lock()
RETRY_COUNT = {}

def app_dir_for(app_row):
    wd = app_row["workdir"] if isinstance(app_row, sqlite3.Row) else app_row["workdir"]
    p = Path(wd)
    if not p.is_absolute():
        p = APPS_DIR / wd
    try:
        p = p.resolve()
        p.relative_to(APPS_DIR.resolve())
    except Exception:
        raise ValueError("Working directory must be inside apps/")
    p.mkdir(parents=True, exist_ok=True)
    return p

def start_application(app_id):
    with db() as c:
        row = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            return False, "Application not found"
        with PROC_LOCK:
            existing = PROC.get(app_id)
        if existing and existing.poll() is None:
            return False, "Already running"
        try:
            wd = app_dir_for(row)
        except Exception as e:
            return False, str(e)
        try:
            args = shlex.split(row["startup"])
        except Exception as e:
            return False, f"Invalid startup: {e}"
        if not args:
            return False, "Empty startup"
        env = dict(os.environ)
        try:
            env.update(json.loads(row["env"] or "{}"))
        except Exception:
            pass
        log_path = LOGS_DIR / f"app_{app_id}.log"
        logf = open(log_path, "ab", buffering=0)
        logf.write(f"\n=== [{now()}] START: {' '.join(args)} ===\n".encode())
        try:
            proc = subprocess.Popen(args, cwd=str(wd), env=env,
                                    stdout=logf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except FileNotFoundError:
            logf.close()
            return False, f"Executable not found: {args[0]}"
        except Exception as e:
            logf.close()
            return False, f"Spawn error: {e}"
        with PROC_LOCK:
            PROC[app_id] = proc
        c.execute("UPDATE applications SET pid=?,status='running' WHERE id=?",
                  (proc.pid, app_id))
        audit(c, "app.start", f"app_id={app_id}",
              g.user["id"] if hasattr(g, "user") else None)
        return True, f"Started PID {proc.pid}"

def stop_application(app_id, timeout=8):
    with PROC_LOCK:
        proc = PROC.get(app_id)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
    with PROC_LOCK:
        PROC.pop(app_id, None)
    with db() as c:
        c.execute("UPDATE applications SET pid=NULL,status='stopped' WHERE id=?", (app_id,))
        audit(c, "app.stop", f"app_id={app_id}",
              g.user["id"] if hasattr(g, "user") else None)
    return True, "Stopped"

def restart_application(app_id):
    stop_application(app_id)
    time.sleep(0.4)
    return start_application(app_id)

def app_status(app_id):
    with PROC_LOCK:
        p = PROC.get(app_id)
    if p is None:
        with db() as c:
            row = c.execute("SELECT pid,status FROM applications WHERE id=?", (app_id,)).fetchone()
        if row and row["pid"]:
            try:
                os.kill(row["pid"], 0)
                return "running"
            except Exception:
                return "stopped"
        return row["status"] if row else "unknown"
    return "running" if p.poll() is None else "exited"

def monitor_loop():
    while True:
        time.sleep(3)
        try:
            with db() as c:
                rows = c.execute("SELECT * FROM applications").fetchall()
            for r in rows:
                aid = r["id"]
                with PROC_LOCK:
                    p = PROC.get(aid)
                if p is None:
                    continue
                rc = p.poll()
                if rc is None:
                    continue
                with PROC_LOCK:
                    PROC.pop(aid, None)
                with db() as c:
                    c.execute("UPDATE applications SET pid=NULL,status='exited' WHERE id=?", (aid,))
                    c.execute("INSERT INTO audit(action,detail,created_at) VALUES(?,?,?)",
                              ("app.exit", f"app_id={aid} rc={rc}", now()))
                if r["auto_restart"]:
                    cnt = RETRY_COUNT.get(aid, 0)
                    if cnt < (r["max_retries"] or 3):
                        RETRY_COUNT[aid] = cnt + 1
                        time.sleep(2)
                        start_application(aid)
                    else:
                        with db() as c:
                            c.execute("INSERT INTO audit(action,detail,created_at) VALUES(?,?,?)",
                                      ("app.restart_exhausted", f"app_id={aid}", now()))
        except Exception:
            traceback.print_exc()

def shutdown_all():
    with PROC_LOCK:
        ids = list(PROC.keys())
    for aid in ids:
        try:
            stop_application(aid, timeout=4)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------
def human_bytes(n):
    if n is None:
        return "Unavailable"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024.0

def sysinfo():
    out = {}
    try:
        if psutil:
            out["cpu_percent"] = psutil.cpu_percent(interval=0.15)
            out["cpu_cores"] = psutil.cpu_count(logical=True)
            vm = psutil.virtual_memory()
            out["ram_total"] = vm.total
            out["ram_used"] = vm.used
            out["ram_percent"] = vm.percent
            du = psutil.disk_usage(str(APPS_DIR))
            out["disk_total"] = du.total
            out["disk_used"] = du.used
            out["disk_free"] = du.free
            out["disk_percent"] = du.percent
            out["uptime"] = int(time.time() - psutil.boot_time())
        else:
            for k in ("cpu_percent", "ram_total", "ram_used", "ram_percent",
                      "disk_total", "disk_used", "disk_free", "disk_percent", "uptime"):
                out[k] = None
    except Exception:
        out.setdefault("cpu_percent", None)
    out["os"] = platform.platform()
    out["arch"] = platform.machine()
    out["kernel"] = platform.release()
    out["python"] = platform.python_version()
    try:
        if os.path.exists("/proc/1/cmdline"):
            cmd = open("/proc/1/cmdline", "rb").read().decode(errors="replace")
            out["proot"] = "proot" in cmd.lower()
        else:
            out["proot"] = bool(os.environ.get("PROOT_TMP_DIR"))
    except Exception:
        out["proot"] = False
    try:
        with db() as c:
            out["users"] = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            out["apps"] = c.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            out["running"] = c.execute("SELECT COUNT(*) FROM applications WHERE status='running'").fetchone()[0]
            out["servers"] = c.execute("SELECT COUNT(*) FROM applications WHERE runtime IN ('java','node')").fetchone()[0]
    except Exception:
        pass
    return out

# ---------------------------------------------------------------------------
# Routes: auth
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET"])
def login_page():
    if current_user():
        return redirect("/")
    return render_template_string(LOGIN_HTML, csrf=csrf_token(),
                                  next=request.args.get("next", "/"))

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or request.form
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    if not u or not p:
        return jsonify(error="Username and password required"), 400
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if not row or not verify_password(p, row["password_hash"]):
            c.execute("INSERT INTO audit(action,detail,ip,created_at) VALUES(?,?,?,?)",
                      ("login.fail", f"user={u}", request.remote_addr, now()))
            return jsonify(error="Invalid credentials"), 401
        session.clear()
        session["user_id"] = row["id"]
        csrf_token()
        c.execute("INSERT INTO audit(user_id,username,action,ip,created_at) VALUES(?,?,?,?,?)",
                  (row["id"], row["username"], "login.ok", request.remote_addr, now()))
        role = row["role"]
    return jsonify(ok=True, role=role)

@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid:
        with db() as c:
            audit(c, "logout", "", uid)
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/api/me")
@login_required
def api_me():
    return jsonify(user=g.user, csrf=csrf_token())

@app.route("/api/change-password", methods=["POST"])
@login_required
def api_change_password():
    d = request.get_json(silent=True) or {}
    old, new = d.get("old", ""), d.get("new", "")
    if len(new) < 8:
        return jsonify(error="New password must be ≥ 8 chars"), 400
    with db() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
        if not verify_password(old, row["password_hash"]):
            return jsonify(error="Current password incorrect"), 400
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new), g.user["id"]))
        audit(c, "password.change", "", g.user["id"])
    return jsonify(ok=True)

# ---------------------------------------------------------------------------
# Routes: system / dashboard
# ---------------------------------------------------------------------------
@app.route("/api/system")
@login_required
def api_system():
    return jsonify(sysinfo())

@app.route("/api/dashboard")
@login_required
def api_dashboard():
    s = sysinfo()
    for k in ("ram_total", "ram_used", "disk_total", "disk_used", "disk_free"):
        s[k + "_hr"] = human_bytes(s.get(k))
    return jsonify(s)

# ---------------------------------------------------------------------------
# Routes: users (admin)
# ---------------------------------------------------------------------------
@app.route("/api/users", methods=["GET"])
@admin_required
def api_users_list():
    with db() as c:
        rows = c.execute("SELECT id,username,email,role,created_at FROM users ORDER BY id").fetchall()
    return jsonify(users=[dict(r) for r in rows])

@app.route("/api/users", methods=["POST"])
@admin_required
def api_users_create():
    d = request.get_json(silent=True) or {}
    u = (d.get("username") or "").strip()
    p = d.get("password") or ""
    r = d.get("role", "user")
    if r not in ("admin", "user"):
        return jsonify(error="bad role"), 400
    if not re.match(r"^[A-Za-z0-9_.-]{3,32}$", u):
        return jsonify(error="Username must be 3-32 chars [A-Za-z0-9_.-]"), 400
    if len(p) < 8:
        return jsonify(error="Password ≥ 8 chars"), 400
    with db() as c:
        try:
            c.execute("INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                      (u, d.get("email", ""), hash_password(p), r, now()))
        except sqlite3.IntegrityError:
            return jsonify(error="Username already exists"), 400
        audit(c, "user.create", f"user={u} role={r}", g.user["id"])
    return jsonify(ok=True)

@app.route("/api/users/<int:uid>", methods=["PUT"])
@admin_required
def api_users_update(uid):
    d = request.get_json(silent=True) or {}
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify(error="not found"), 404
        role = d.get("role", row["role"])
        if role not in ("admin", "user"):
            return jsonify(error="bad role"), 400
        if row["role"] == "admin" and role != "admin":
            n = c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if n <= 1:
                return jsonify(error="Cannot demote last admin"), 400
        c.execute("UPDATE users SET email=?,role=? WHERE id=?",
                  (d.get("email", row["email"]), role, uid))
        if d.get("password"):
            if len(d["password"]) < 8:
                return jsonify(error="Password ≥ 8 chars"), 400
            c.execute("UPDATE users SET password_hash=? WHERE id=?",
                      (hash_password(d["password"]), uid))
        audit(c, "user.update", f"user_id={uid}", g.user["id"])
    return jsonify(ok=True)

@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_users_delete(uid):
    if uid == g.user["id"]:
        return jsonify(error="Cannot delete yourself"), 400
    with db() as c:
        row = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify(error="not found"), 404
        if row["role"] == "admin":
            n = c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if n <= 1:
                return jsonify(error="Cannot delete last admin"), 400
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        audit(c, "user.delete", f"user_id={uid}", g.user["id"])
    return jsonify(ok=True)

# ---------------------------------------------------------------------------
# Routes: nests & eggs
# ---------------------------------------------------------------------------
@app.route("/api/nests", methods=["GET"])
@login_required
def api_nests():
    with db() as c:
        nests = [dict(r) for r in c.execute("SELECT * FROM nests ORDER BY name")]
        eggs = [dict(r) for r in c.execute("SELECT * FROM eggs ORDER BY name")]
    for e in eggs:
        for k in ("env", "required_files", "allowed_ports", "default_limits"):
            try:
                e[k] = json.loads(e[k] or ("{}" if k in ("env", "default_limits") else "[]"))
            except Exception:
                e[k] = {} if k in ("env", "default_limits") else []
    return jsonify(nests=nests, eggs=eggs)

@app.route("/api/nests", methods=["POST"])
@admin_required
def api_nests_create():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="name required"), 400
    with db() as c:
        try:
            c.execute("INSERT INTO nests(name,description,created_at) VALUES(?,?,?)",
                      (name, d.get("description", ""), now()))
        except sqlite3.IntegrityError:
            return jsonify(error="Nest exists"), 400
        audit(c, "nest.create", name, g.user["id"])
    return jsonify(ok=True)

@app.route("/api/nests/<int:nid>", methods=["PUT", "DELETE"])
@admin_required
def api_nests_mod(nid):
    with db() as c:
        if request.method == "DELETE":
            egg_count = c.execute("SELECT COUNT(*) FROM eggs WHERE nest_id=?", (nid,)).fetchone()[0]
            if egg_count:
                return jsonify(error="Nest has eggs; delete them first"), 400
            c.execute("DELETE FROM nests WHERE id=?", (nid,))
            audit(c, "nest.delete", f"id={nid}", g.user["id"])
            return jsonify(ok=True)
        d = request.get_json(silent=True) or {}
        c.execute("UPDATE nests SET name=?,description=? WHERE id=?",
                  ((d.get("name") or "").strip(), d.get("description", ""), nid))
        audit(c, "nest.update", f"id={nid}", g.user["id"])
    return jsonify(ok=True)

@app.route("/api/eggs", methods=["POST"])
@admin_required
def api_eggs_create():
    d = request.get_json(silent=True) or {}
    for k in ("name", "nest_id", "runtime", "startup"):
        if not d.get(k):
            return jsonify(error=f"{k} required"), 400
    with db() as c:
        c.execute("""INSERT INTO eggs(nest_id,name,description,runtime,working_dir,startup,
                     env,required_files,allowed_ports,default_limits,install_notes,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (d["nest_id"], d["name"], d.get("description", ""),
                   d["runtime"], d.get("working_dir", "."), d["startup"],
                   json.dumps(d.get("env", {})) if isinstance(d.get("env"), dict) else (d.get("env") or "{}"),
                   json.dumps(d.get("required_files", [])) if isinstance(d.get("required_files"), list) else (d.get("required_files") or "[]"),
                   json.dumps(d.get("allowed_ports", [])) if isinstance(d.get("allowed_ports"), list) else (d.get("allowed_ports") or "[]"),
                   json.dumps(d.get("default_limits", {})) if isinstance(d.get("default_limits"), dict) else (d.get("default_limits") or "{}"),
                   d.get("install_notes", ""), now()))
        eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        audit(c, "egg.create", f"egg_id={eid}", g.user["id"])
    return jsonify(ok=True, id=eid)

@app.route("/api/eggs/<int:eid>", methods=["PUT", "DELETE"])
@admin_required
def api_eggs_mod(eid):
    with db() as c:
        if request.method == "DELETE":
            used = c.execute("SELECT COUNT(*) FROM applications WHERE egg_id=?", (eid,)).fetchone()[0]
            if used:
                return jsonify(error="Egg used by applications; delete those first"), 400
            c.execute("DELETE FROM eggs WHERE id=?", (eid,))
            audit(c, "egg.delete", f"id={eid}", g.user["id"])
            return jsonify(ok=True)
        d = request.get_json(silent=True) or {}
        c.execute("""UPDATE eggs SET nest_id=?,name=?,description=?,runtime=?,working_dir=?,
                     startup=?,env=?,required_files=?,allowed_ports=?,default_limits=?,
                     install_notes=? WHERE id=?""",
                  (d.get("nest_id"), d.get("name"), d.get("description", ""),
                   d.get("runtime"), d.get("working_dir", "."), d.get("startup"),
                   json.dumps(d.get("env", {})) if isinstance(d.get("env"), dict) else (d.get("env") or "{}"),
                   json.dumps(d.get("required_files", [])) if isinstance(d.get("required_files"), list) else (d.get("required_files") or "[]"),
                   json.dumps(d.get("allowed_ports", [])) if isinstance(d.get("allowed_ports"), list) else (d.get("allowed_ports") or "[]"),
                   json.dumps(d.get("default_limits", {})) if isinstance(d.get("default_limits"), dict) else (d.get("default_limits") or "{}"),
                   d.get("install_notes", ""), eid))
        audit(c, "egg.update", f"id={eid}", g.user["id"])
    return jsonify(ok=True)

@app.route("/api/eggs/<int:eid>/clone", methods=["POST"])
@admin_required
def api_eggs_clone(eid):
    with db() as c:
        row = c.execute("SELECT * FROM eggs WHERE id=?", (eid,)).fetchone()
        if not row:
            return jsonify(error="not found"), 404
        c.execute("""INSERT INTO eggs(nest_id,name,description,runtime,working_dir,startup,
                     env,required_files,allowed_ports,default_limits,install_notes,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (row["nest_id"], row["name"] + " (copy)", row["description"],
                   row["runtime"], row["working_dir"], row["startup"], row["env"],
                   row["required_files"], row["allowed_ports"], row["default_limits"],
                   row["install_notes"], now()))
        nid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        audit(c, "egg.clone", f"from={eid} to={nid}", g.user["id"])
    return jsonify(ok=True, id=nid)

# ---------------------------------------------------------------------------
# Routes: applications
# ---------------------------------------------------------------------------
def _can_access(row):
    if g.user["role"] == "admin":
        return True
    return row["owner_id"] == g.user["id"]

def _app_by_id(aid):
    with db() as c:
        return c.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()

@app.route("/api/apps", methods=["GET"])
@login_required
def api_apps_list():
    with db() as c:
        if g.user["role"] == "admin":
            rows = c.execute("""SELECT a.*, u.username as owner_name, e.name as egg_name
                                FROM applications a
                                LEFT JOIN users u ON u.id=a.owner_id
                                LEFT JOIN eggs e ON e.id=a.egg_id
                                ORDER BY a.id""").fetchall()
        else:
            rows = c.execute("""SELECT a.*, u.username as owner_name, e.name as egg_name
                                FROM applications a
                                LEFT JOIN users u ON u.id=a.owner_id
                                LEFT JOIN eggs e ON e.id=a.egg_id
                                WHERE a.owner_id=? ORDER BY a.id""", (g.user["id"],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = app_status(r["id"])
        d["ports"] = json.loads(d.get("ports") or "[]")
        d["limits"] = json.loads(d.get("limits") or "{}")
        out.append(d)
    return jsonify(apps=out)

@app.route("/api/apps", methods=["POST"])
@login_required
def api_apps_create():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="name required"), 400
    runtime = d.get("runtime") or "python"
    workdir = d.get("workdir") or name.lower().replace(" ", "_")
    startup = d.get("startup") or ""
    if not startup:
        return jsonify(error="startup required"), 400
    owner_id = g.user["id"]
    if g.user["role"] == "admin" and d.get("owner_id"):
        owner_id = int(d["owner_id"])
    try:
        test_wd = Path(workdir)
        if test_wd.is_absolute():
            test_wd.resolve().relative_to(APPS_DIR.resolve())
        else:
            (APPS_DIR / workdir).resolve().relative_to(APPS_DIR.resolve())
    except Exception:
        return jsonify(error="workdir must be inside apps/"), 400
    with db() as c:
        c.execute("""INSERT INTO applications(owner_id,egg_id,name,runtime,workdir,startup,
                     env,ports,limits,auto_restart,max_retries,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (owner_id, d.get("egg_id"), name, runtime, workdir, startup,
                   json.dumps(d.get("env", {})), json.dumps(d.get("ports", [])),
                   json.dumps(d.get("limits", {})),
                   1 if d.get("auto_restart") else 0,
                   int(d.get("max_retries", 3)), now()))
        aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        audit(c, "app.create", f"id={aid} name={name}", g.user["id"])
    return jsonify(ok=True, id=aid)

@app.route("/api/apps/<int:aid>", methods=["PUT", "DELETE"])
@login_required
def api_apps_mod(aid):
    row = _app_by_id(aid)
    if not row:
        return jsonify(error="not found"), 404
    if not _can_access(row):
        return jsonify(error="forbidden"), 403
    if request.method == "DELETE":
        try:
            stop_application(aid)
        except Exception:
            pass
        with db() as c:
            c.execute("DELETE FROM applications WHERE id=?", (aid,))
            audit(c, "app.delete", f"id={aid}", g.user["id"])
        return jsonify(ok=True)
    d = request.get_json(silent=True) or {}
    with db() as c:
        c.execute("""UPDATE applications SET name=?,runtime=?,workdir=?,startup=?,env=?,
                     ports=?,limits=?,auto_restart=?,max_retries=? WHERE id=?""",
                  (d.get("name", row["name"]), d.get("runtime", row["runtime"]),
                   d.get("workdir", row["workdir"]), d.get("startup", row["startup"]),
                   json.dumps(d["env"]) if "env" in d else row["env"],
                   json.dumps(d["ports"]) if "ports" in d else row["ports"],
                   json.dumps(d["limits"]) if "limits" in d else row["limits"],
                   1 if d.get("auto_restart", row["auto_restart"]) else 0,
                   int(d.get("max_retries", row["max_retries"])), aid))
        audit(c, "app.update", f"id={aid}", g.user["id"])
    return jsonify(ok=True)

@app.route("/api/apps/<int:aid>/clone", methods=["POST"])
@login_required
def api_apps_clone(aid):
    row = _app_by_id(aid)
    if not row:
        return jsonify(error="not found"), 404
    if not _can_access(row):
        return jsonify(error="forbidden"), 403
    with db() as c:
        c.execute("""INSERT INTO applications(owner_id,egg_id,name,runtime,workdir,startup,
                     env,ports,limits,auto_restart,max_retries,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (row["owner_id"], row["egg_id"], row["name"] + " (copy)",
                   row["runtime"],
                   (row["workdir"] + "_copy") if not Path(row["workdir"]).is_absolute() else row["workdir"],
                   row["startup"], row["env"], row["ports"], row["limits"],
                   row["auto_restart"], row["max_retries"], now()))
        nid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        audit(c, "app.clone", f"from={aid} to={nid}", g.user["id"])
    return jsonify(ok=True, id=nid)

@app.route("/api/apps/<int:aid>/<action>", methods=["POST"])
@login_required
def api_apps_action(aid, action):
    row = _app_by_id(aid)
    if not row:
        return jsonify(error="not found"), 404
    if not _can_access(row):
        return jsonify(error="forbidden"), 403
    if action == "start":
        ok, msg = start_application(aid)
    elif action == "stop":
        ok, msg = stop_application(aid)
    elif action == "restart":
        ok, msg = restart_application(aid)
    else:
        return jsonify(error="bad action"), 400
    return jsonify(ok=ok, message=msg, status=app_status(aid))

@app.route("/api/apps/<int:aid>/logs", methods=["GET"])
@login_required
def api_apps_logs(aid):
    row = _app_by_id(aid)
    if not row:
        return jsonify(error="not found"), 404
    if not _can_access(row):
        return jsonify(error="forbidden"), 403
    logf = LOGS_DIR / f"app_{aid}.log"
    tail = int(request.args.get("tail", 500))
    if not logf.exists():
        return jsonify(logs="", status=app_status(aid))
    lines = logf.read_text(errors="replace").splitlines()[-tail:]
    return jsonify(logs="\n".join(lines), status=app_status(aid))

@app.route("/api/apps/<int:aid>/logs/download")
@login_required
def api_apps_logs_download(aid):
    row = _app_by_id(aid)
    if not row:
        return jsonify(error="not found"), 404
    if not _can_access(row):
        return jsonify(error="forbidden"), 403
    logf = LOGS_DIR / f"app_{aid}.log"
    if not logf.exists():
        return Response("", mimetype="text/plain")
    return send_file(logf, as_attachment=True, download_name=f"app_{aid}.log")

# ---------------------------------------------------------------------------
# Routes: file manager
# ---------------------------------------------------------------------------
def resolve_app_path(aid, relpath):
    row = _app_by_id(aid)
    if not row:
        abort(404)
    if not _can_access(row):
        abort(403)
    root = app_dir_for(row)
    p = (root / relpath).resolve()
    try:
        p.relative_to(root)
    except Exception:
        abort(400, "path traversal")
    cur = p
    while cur != root.parent and cur != root:
        if cur.is_symlink():
            try:
                cur.resolve().relative_to(root)
            except Exception:
                abort(400, "symlink escape")
        cur = cur.parent
    return root, p, row

@app.route("/api/apps/<int:aid>/files")
@login_required
def api_files_list(aid):
    rel = request.args.get("path", "")
    root, p, row = resolve_app_path(aid, rel)
    if not p.exists():
        return jsonify(error="not found"), 404
    if p.is_file():
        return jsonify(type="file", name=p.name, size=p.stat().st_size)
    items = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            st = e.stat()
            items.append({"name": e.name, "is_dir": e.is_dir(),
                          "size": st.st_size,
                          "mtime": datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z"})
        except Exception:
            continue
    return jsonify(type="dir", path=rel, items=items)

@app.route("/api/apps/<int:aid>/files/read")
@login_required
def api_files_read(aid):
    rel = request.args.get("path", "")
    root, p, row = resolve_app_path(aid, rel)
    if not p.is_file():
        return jsonify(error="not a file"), 400
    if p.stat().st_size > 2 * 1024 * 1024:
        return jsonify(error="too large (>2MB)"), 400
    try:
        content = p.read_text(errors="replace")
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(content=content, path=rel)

@app.route("/api/apps/<int:aid>/files/write", methods=["POST"])
@login_required
def api_files_write(aid):
    d = request.get_json(silent=True) or {}
    rel = d.get("path", "")
    root, p, row = resolve_app_path(aid, rel)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(d.get("content", ""))
    except Exception as e:
        return jsonify(error=str(e)), 400
    with db() as c:
        audit(c, "file.write", f"app={aid} path={rel}", g.user["id"])
    return jsonify(ok=True)

@app.route("/api/apps/<int:aid>/files/upload", methods=["POST"])
@login_required
def api_files_upload(aid):
    rel_dir = request.form.get("path", "")
    root, p, row = resolve_app_path(aid, rel_dir)
    if not p.is_dir():
        return jsonify(error="target not a dir"), 400
    f = request.files.get("file")
    if not f:
        return jsonify(error="no file"), 400
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f.filename or "upload.bin")[:120]
    dest = (p / name).resolve()
    try:
        dest.relative_to(root)
    except Exception:
        return jsonify(error="bad name"), 400
    f.save(dest)
    with db() as c:
        audit(c, "file.upload", f"app={aid} name={name}", g.user["id"])
    return jsonify(ok=True, name=name)

@app.route("/api/apps/<int:aid>/files/download")
@login_required
def api_files_download(aid):
    rel = request.args.get("path", "")
    root, p, row = resolve_app_path(aid, rel)
    if not p.is_file():
        return jsonify(error="not a file"), 404
    return send_file(p, as_attachment=True, download_name=p.name)

@app.route("/api/apps/<int:aid>/files/mkdir", methods=["POST"])
@login_required
def api_files_mkdir(aid):
    d = request.get_json(silent=True) or {}
    root, p, row = resolve_app_path(aid, d.get("path", ""))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)

@app.route("/api/apps/<int:aid>/files/rename", methods=["POST"])
@login_required
def api_files_rename(aid):
    d = request.get_json(silent=True) or {}
    root, src, row = resolve_app_path(aid, d.get("path", ""))
    root2, dst, _ = resolve_app_path(aid, d.get("to", ""))
    try:
        if dst.exists():
            return jsonify(error="target exists"), 400
        src.rename(dst)
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)

@app.route("/api/apps/<int:aid>/files/delete", methods=["POST"])
@login_required
def api_files_delete(aid):
    d = request.get_json(silent=True) or {}
    root, p, row = resolve_app_path(aid, d.get("path", ""))
    if p == root:
        return jsonify(error="cannot delete root"), 400
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    except Exception as e:
        return jsonify(error=str(e)), 400
    with db() as c:
        audit(c, "file.delete", f"app={aid} path={d.get('path')}", g.user["id"])
    return jsonify(ok=True)

# ---------------------------------------------------------------------------
# Routes: ports / network
# ---------------------------------------------------------------------------
@app.route("/api/ports")
@login_required
def api_ports():
    ports = {}
    with db() as c:
        if g.user["role"] == "admin":
            rows = c.execute("SELECT id,name,ports,workdir FROM applications").fetchall()
        else:
            rows = c.execute("SELECT id,name,ports,workdir FROM applications WHERE owner_id=?",
                             (g.user["id"],)).fetchall()
    for r in rows:
        try:
            plist = json.loads(r["ports"] or "[]")
        except Exception:
            plist = []
        for p in plist:
            ports.setdefault(str(p), []).append(r["name"])
    dup = {k: v for k, v in ports.items() if len(v) > 1}
    listening = []
    try:
        if psutil:
            for c in psutil.net_connections(kind="inet"):
                if c.status == "LISTEN" and c.laddr:
                    listening.append({"port": c.laddr.port, "addr": c.laddr.ip,
                                      "pid": c.pid, "type": "TCP"})
    except Exception:
        pass
    return jsonify(configured=ports, duplicates=dup, listening=listening[:500])

@app.route("/api/network/check")
@login_required
def api_network_check():
    host = request.args.get("host", "127.0.0.1")
    try:
        port = int(request.args.get("port", "0"))
    except Exception:
        return jsonify(error="bad port"), 400
    proto = request.args.get("proto", "tcp").lower()
    if not (0 < port < 65536):
        return jsonify(error="bad port"), 400
    if host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        return jsonify(open=False, note="Only localhost checks supported")
    sock = socket.socket(socket.AF_INET,
                         socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
    is_open = False
    try:
        if proto == "tcp":
            sock.settimeout(1.5)
            sock.connect((host, port))
            is_open = True
        else:
            sock.bind(("0.0.0.0", port))
            is_open = False
    except Exception:
        is_open = False
    finally:
        sock.close()
    return jsonify(open=is_open, port=port, proto=proto, host=host)

# ---------------------------------------------------------------------------
# Routes: audit
# ---------------------------------------------------------------------------
@app.route("/api/audit")
@admin_required
def api_audit():
    lim = min(int(request.args.get("limit", 200)), 1000)
    with db() as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
    return jsonify(events=[dict(r) for r in rows])

# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------
LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FaiLive2 Hosting — Login</title>
<style>
:root{--bg:#0f0b1a;--card:#1a1230;--violet:#8b5cf6;--purple:#6d28d9;--cyan:#22d3ee;
--green:#34d399;--red:#f87171;--text:#e9e4ff;--muted:#a99ec7;--border:#2c2350}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:radial-gradient(ellipse at top,#2a1a55 0%,var(--bg) 60%);
color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{width:100%;max-width:420px;background:linear-gradient(160deg,#1f1540,#140f28);
border:1px solid var(--border);border-radius:22px;padding:36px 30px;
box-shadow:0 20px 60px rgba(139,92,246,.25);animation:slideIn .7s cubic-bezier(.2,.9,.3,1)}
@keyframes slideIn{from{opacity:0;transform:translateY(30px) scale(.97)}to{opacity:1;transform:none}}
.logo{text-align:center;font-size:28px;font-weight:800;letter-spacing:1px;
background:linear-gradient(90deg,#a78bfa,#22d3ee);-webkit-background-clip:text;background-clip:text;
color:transparent;margin-bottom:6px}
.sub{text-align:center;color:var(--muted);font-size:13px;margin-bottom:26px}
label{display:block;font-size:12px;color:var(--muted);margin:14px 0 6px;text-transform:uppercase;letter-spacing:.5px}
input{width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--border);
background:#100b22;color:var(--text);font-size:15px;outline:none;transition:border .2s,box-shadow .2s}
input:focus{border-color:var(--violet);box-shadow:0 0 0 3px rgba(139,92,246,.2)}
button{width:100%;margin-top:22px;padding:14px;border:none;border-radius:12px;font-size:15px;
font-weight:700;color:#fff;background:linear-gradient(135deg,var(--purple),var(--violet));
cursor:pointer;transition:transform .15s,filter .2s}
button:hover{filter:brightness(1.1)}button:active{transform:scale(.98)}
button:disabled{opacity:.6;cursor:not-allowed}
.err{color:var(--red);font-size:13px;margin-top:14px;min-height:18px;text-align:center}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.4);
border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<div class="card">
  <div class="logo">FaiLive2</div>
  <div class="sub">Hosting Control Panel</div>
  <form id="f">
    <label>Username</label>
    <input name="username" autocomplete="username" required autofocus>
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit" id="b">Sign In</button>
    <div class="err" id="e"></div>
  </form>
</div>
<script>
const csrf="{{ csrf }}";
document.getElementById('f').addEventListener('submit', async ev=>{
  ev.preventDefault();
  const b=document.getElementById('b');const e=document.getElementById('e');
  b.disabled=true;b.innerHTML='<span class="spinner"></span>Signing in…';e.textContent='';
  const fd=new FormData(ev.target);
  try{
    const r=await fetch('/api/login',{method:'POST',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({username:fd.get('username'),password:fd.get('password')})});
    const j=await r.json();
    if(r.ok){location.href='{{ next }}';return;}
    e.textContent=j.error||'Login failed';
  }catch(err){e.textContent='Network error';}
  b.disabled=false;b.textContent='Sign In';
});
</script></body></html>"""

MAIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FaiLive2 Hosting</title>
<style>
:root{--bg:#0b0818;--panel:#140f28;--card:#1a1230;--violet:#8b5cf6;--purple:#6d28d9;
--cyan:#22d3ee;--green:#34d399;--red:#f87171;--yellow:#fbbf24;--text:#e9e4ff;
--muted:#a99ec7;--border:#2c2350;--sidebar-w:250px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.app{display:flex;min-height:100vh}
.sidebar{width:var(--sidebar-w);background:linear-gradient(180deg,#1a1038,#0f0925);
border-right:1px solid var(--border);padding:20px 14px;position:fixed;top:0;bottom:0;overflow-y:auto;
transition:transform .28s ease;z-index:50}
.brand{font-size:20px;font-weight:800;letter-spacing:.5px;margin-bottom:22px;
background:linear-gradient(90deg,#a78bfa,#22d3ee);-webkit-background-clip:text;
background-clip:text;color:transparent}
.nav a{display:flex;align-items:center;gap:10px;padding:11px 13px;border-radius:10px;
color:var(--muted);text-decoration:none;font-size:14px;margin-bottom:4px;transition:background .15s,color .15s;cursor:pointer}
.nav a:hover{background:rgba(139,92,246,.12);color:var(--text)}
.nav a.active{background:linear-gradient(90deg,rgba(139,92,246,.25),transparent);
color:#fff;border-left:3px solid var(--violet)}
.nav .ico{width:18px;text-align:center}
.main{flex:1;margin-left:var(--sidebar-w);transition:margin .28s ease;padding:20px}
.topbar{display:flex;align-items:center;gap:12px;margin-bottom:22px}
.menu-btn{background:var(--card);border:1px solid var(--border);border-radius:10px;
width:40px;height:40px;cursor:pointer;color:var(--text);font-size:18px}
.title{font-size:20px;font-weight:700}
.who{margin-left:auto;font-size:13px;color:var(--muted)}
.btn{background:linear-gradient(135deg,var(--purple),var(--violet));border:none;
color:#fff;padding:10px 16px;border-radius:10px;cursor:pointer;font-weight:600;font-size:13px;
transition:filter .15s,transform .1s}
.btn:hover{filter:brightness(1.1)}.btn:active{transform:scale(.97)}
.btn.small{padding:7px 11px;font-size:12px}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn.danger{background:linear-gradient(135deg,#b91c1c,#ef4444)}
.btn.success{background:linear-gradient(135deg,#047857,#10b981)}
.btn.warn{background:linear-gradient(135deg,#b45309,#f59e0b);color:#1a1230}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:18px;
animation:fadeIn .35s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.card h3{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:8px}
.card .big{font-size:24px;font-weight:800}
.card .sub{font-size:12px;color:var(--muted);margin-top:4px}
.bar{height:8px;background:#0f0925;border-radius:999px;overflow:hidden;margin-top:10px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--violet),var(--cyan));border-radius:999px;
transition:width .4s ease}
.table{width:100%;border-collapse:collapse;font-size:13px}
.table th,.table td{padding:10px;text-align:left;border-bottom:1px solid var(--border);vertical-align:middle}
.table th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.table tr:hover td{background:rgba(139,92,246,.06)}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700}
.badge.running{background:rgba(52,211,153,.15);color:var(--green)}
.badge.stopped,.badge.exited{background:rgba(248,113,113,.12);color:var(--red)}
.badge.admin{background:rgba(139,92,246,.2);color:#c4b5fd}
.badge.user{background:rgba(34,211,238,.15);color:var(--cyan)}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(3px);
display:flex;align-items:center;justify-content:center;z-index:200;padding:16px;animation:fadeIn .2s}
.modal{background:linear-gradient(160deg,#1f1540,#140f28);border:1px solid var(--border);
border-radius:18px;padding:26px;width:100%;max-width:560px;max-height:88vh;overflow-y:auto;
animation:slideUp .28s cubic-bezier(.2,.9,.3,1)}
@keyframes slideUp{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
.modal h2{font-size:18px;margin-bottom:16px}
.modal label{display:block;font-size:12px;color:var(--muted);margin:12px 0 5px;text-transform:uppercase;letter-spacing:.4px}
.modal input,.modal select,.modal textarea{width:100%;padding:10px 12px;border-radius:10px;
border:1px solid var(--border);background:#0f0925;color:var(--text);font-size:14px;outline:none;font-family:inherit}
.modal textarea{min-height:90px;resize:vertical;font-family:ui-monospace,monospace;font-size:12px}
.modal input:focus,.modal select:focus,.modal textarea:focus{border-color:var(--violet)}
.modal .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.modal .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:22px}
.toasts{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:8px;z-index:400}
.toast{background:#1a1230;border:1px solid var(--border);border-left:4px solid var(--violet);
padding:12px 16px;border-radius:10px;font-size:13px;min-width:220px;animation:slideInR .3s}
.toast.success{border-left-color:var(--green)}.toast.error{border-left-color:var(--red)}
@keyframes slideInR{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:none}}
.console{background:#0a0614;border:1px solid var(--border);border-radius:12px;padding:14px;
font-family:ui-monospace,monospace;font-size:12px;line-height:1.5;color:#c8bfe0;
max-height:420px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.console .err-line{color:#fca5a5}
.tab{padding:8px 14px;border-radius:10px;background:var(--card);border:1px solid var(--border);
cursor:pointer;font-size:13px;color:var(--muted)}
.empty{color:var(--muted);text-align:center;padding:30px;font-size:13px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.file-row{cursor:pointer}
.breadcrumb{font-family:ui-monospace,monospace;font-size:12px;color:var(--cyan);margin-bottom:10px}
@media(max-width:720px){
.sidebar{transform:translateX(calc(-1*var(--sidebar-w)))}
.sidebar.open{transform:none}
.main{margin-left:0}
.modal .row{grid-template-columns:1fr}
}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:40;display:none}
.overlay.show{display:block}
</style></head><body>
<div class="overlay" id="overlay" onclick="closeSidebar()"></div>
<div class="app">
  <aside class="sidebar" id="sidebar">
    <div class="brand">FaiLive2 Hosting</div>
    <nav class="nav" id="nav"></nav>
    <div style="position:absolute;bottom:16px;left:14px;right:14px">
      <a class="btn ghost" style="display:block;text-align:center" href="/logout">Sign Out</a>
    </div>
  </aside>
  <main class="main" id="main">
    <div class="topbar">
      <button class="menu-btn" onclick="toggleSidebar()">☰</button>
      <div class="title" id="pageTitle">Dashboard</div>
      <div class="who" id="who"></div>
    </div>
    <div id="view"></div>
  </main>
</div>
<div class="toasts" id="toasts"></div>
<div id="modalRoot"></div>

<script>
let ME=null, CSRF='', SYSTEM={}, ADMIN=false, FILES_APP=null, FILES_PATH='';

const $=(s,r=document)=>r.querySelector(s);
const el=(tag,attrs={},...kids)=>{const e=document.createElement(tag);
  for(const[k,v] of Object.entries(attrs)){if(k==='class')e.className=v;else if(k==='html')e.innerHTML=v;
    else if(k.startsWith('on'))e.addEventListener(k.slice(2),v);else e.setAttribute(k,v);}
  for(const k of kids)if(k!=null)e.append(k.nodeType?k:document.createTextNode(k));return e;};

function toast(msg,type=''){const t=el('div',{class:'toast '+type},msg);$('#toasts').append(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300)},3200);}

async function api(path,opts={}){opts.headers=Object.assign({'X-CSRF-Token':CSRF},opts.headers||{});
  if(opts.body&&!(opts.body instanceof FormData)){opts.headers['Content-Type']='application/json';
    if(typeof opts.body!=='string')opts.body=JSON.stringify(opts.body);}
  const r=await fetch(path,opts);
  if(r.status===401){location.href='/login';throw new Error('auth');}
  let j=null;try{j=await r.json();}catch(e){}
  if(!r.ok){toast((j&&j.error)||('Error '+r.status),'error');throw new Error((j&&j.error)||r.status);}
  return j;
}

function modal(title,body,actions){
  const root=$('#modalRoot');
  const bg=el('div',{class:'modal-bg'});
  const box=el('div',{class:'modal'});
  box.append(el('h2',{},title));
  const bodyWrap=el('div');box.append(bodyWrap);
  if(typeof body==='string')bodyWrap.innerHTML=body;else bodyWrap.append(body);
  const acts=el('div',{class:'actions'});
  for(const a of actions){const b=el('button',{class:'btn '+(a.cls||'')},a.label);
    b.onclick=()=>a.onClick?.(close);acts.append(b);}
  box.append(acts);bg.append(box);root.append(bg);
  function close(){bg.remove();}
  bg.onclick=e=>{if(e.target===bg)close();};
  return {close,box,bodyWrap};
}
function confirmDialog(msg){return new Promise(res=>{
  modal('Confirm',el('p',{},msg),[
    {label:'Cancel',cls:'ghost',onClick:c=>{c();res(false);}},
    {label:'Confirm',cls:'danger',onClick:c=>{c();res(true);}}
  ]);
});}

async function loadMe(){const j=await api('/api/me');ME=j.user;CSRF=j.csrf;ADMIN=ME.role==='admin';
  $('#who').textContent=ME.username+' · '+ME.role;buildNav();}

function buildNav(){
  const nav=$('#nav');nav.innerHTML='';
  const items=[['dashboard','Dashboard','◈'],['apps','Applications','▤'],
    ['nests','Nests & Eggs','❖'],['files','File Manager','🗀'],
    ['network','Network','⇄'],['logs','Activity','≡']];
  if(ADMIN)items.splice(1,0,['users','Users','👤']);
  for(const[id,label,ico]of items){
    const a=el('a',{onclick:()=>go(id)},el('span',{class:'ico'},ico),label);
    a.dataset.id=id;nav.append(a);}
}
function setActive(id){document.querySelectorAll('#nav a').forEach(a=>a.classList.toggle('active',a.dataset.id===id));}

const PAGES={dashboard:renderDashboard,users:renderUsers,apps:renderApps,nests:renderNests,
  files:renderFiles,network:renderNetwork,logs:renderLogs};
function go(id){setActive(id);
  const link=document.querySelector('#nav a[data-id="'+id+'"]');
  $('#pageTitle').textContent=link?link.textContent.trim():id;
  if(window.innerWidth<720)closeSidebar(); PAGES[id]($('#view'));}
function toggleSidebar(){const s=$('#sidebar');if(window.innerWidth<720){s.classList.toggle('open');$('#overlay').classList.toggle('show');}
  else{s.classList.toggle('collapsed');$('#main').classList.toggle('expanded');}}
function closeSidebar(){$('#sidebar').classList.remove('open');$('#overlay').classList.remove('show');}

function human(n){if(n==null)return 'Unavailable';const u=['B','KB','MB','GB','TB'];let i=0;
  while(n>=1024&&i<u.length-1){n/=1024;i++;}return n.toFixed(1)+' '+u[i];}
function fmtUptime(s){const d=Math.floor(s/86400);const h=Math.floor(s%86400/3600);const m=Math.floor(s%3600/60);
  return (d?d+'d ':'')+(h?h+'h ':'')+m+'m';}

async function renderDashboard(root){
  root.innerHTML='<div class="empty">Loading…</div>';
  const s=await api('/api/dashboard');SYSTEM=s;
  const card=(title,val,sub,barPct)=>el('div',{class:'card'},
    el('h3',{},title),el('div',{class:'big'},val),
    sub?el('div',{class:'sub'},sub):null,
    barPct!=null?el('div',{class:'bar'},el('i',{style:'width:'+Math.min(100,barPct)+'%'})):null);
  const wrap=el('div');
  wrap.append(el('div',{class:'grid'},
    card('CPU', s.cpu_percent!=null?s.cpu_percent.toFixed(1)+'%':'Unavailable',
         s.cpu_cores?s.cpu_cores+' cores':'', s.cpu_percent),
    card('RAM', s.ram_used!=null?human(s.ram_used)+' / '+human(s.ram_total):'Unavailable',
         s.ram_percent!=null?s.ram_percent.toFixed(1)+'%':'', s.ram_percent),
    card('Disk', s.disk_used!=null?human(s.disk_used)+' / '+human(s.disk_total):'Unavailable',
         s.disk_free!=null?human(s.disk_free)+' free':'', s.disk_percent),
    card('Uptime', s.uptime!=null?fmtUptime(s.uptime):'Unavailable','')));
  wrap.append(el('div',{class:'grid',style:'margin-top:14px'},
    card('Applications', s.apps??'—', (s.running||0)+' running'),
    card('Servers', s.servers??'—','java/node'),
    card('Users', s.users??'—',''),
    card('OS', s.arch||'—',(s.os||'').split(' ')[0]||'Unavailable')));
  if(s.proot)wrap.append(el('div',{class:'card',style:'margin-top:14px;border-color:#b45309'},
    el('h3',{},'PRoot Notice'),
    el('div',{class:'sub'},'cgroups, CPU/RAM enforcement, and public UDP are limited in Android PRoot. '+
      'Applications run as local processes, not isolated VPS containers.')));
  root.innerHTML='';root.append(wrap);
}

async function renderUsers(root){
  const j=await api('/api/users');
  const wrap=el('div');
  wrap.append(el('div',{class:'toolbar'},el('button',{class:'btn',onclick:()=>userModal()},'+ New User')));
  const t=el('table',{class:'table'});
  t.innerHTML='<thead><tr><th>ID</th><th>Username</th><th>Email</th><th>Role</th><th>Created</th><th></th></tr></thead>';
  const tb=el('tbody');
  for(const u of j.users){
    const tr=el('tr',{},el('td',{},String(u.id)),el('td',{},u.username),
      el('td',{},u.email||''),el('td',{},el('span',{class:'badge '+u.role},u.role)),
      el('td',{},u.created_at.slice(0,10)));
    const acts=el('td');
    acts.append(el('button',{class:'btn small ghost',onclick:()=>userModal(u)},'Edit'));
    acts.append(document.createTextNode(' '));
    acts.append(el('button',{class:'btn small danger',onclick:async()=>{
      if(await confirmDialog('Delete user '+u.username+'?')){await api('/api/users/'+u.id,{method:'DELETE'});
        toast('User deleted','success');renderUsers(root);}}},'Del'));
    tr.append(acts);tb.append(tr);
  }
  t.append(tb);wrap.append(t);root.innerHTML='';root.append(wrap);
}
function userModal(u){
  const isNew=!u;
  const box=el('div');
  box.innerHTML=`
    <label>Username</label><input id="mu" value="${u?u.username:''}" ${u?'disabled':''}>
    <label>Email</label><input id="me" value="${u?u.email||'':''}">
    <label>Role</label><select id="mr">
      <option value="user" ${u&&u.role==='user'?'selected':''}>user</option>
      <option value="admin" ${u&&u.role==='admin'?'selected':''}>admin</option></select>
    <label>${isNew?'Password':'New password (blank = unchanged)'}</label>
    <input id="mp" type="password" placeholder="${isNew?'≥ 8 chars':''}">`;
  modal(isNew?'New User':'Edit User',box,[
    {label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Save',onClick:async c=>{
      const body={username:box.querySelector('#mu').value,email:box.querySelector('#me').value,
        role:box.querySelector('#mr').value};
      const p=box.querySelector('#mp').value;if(p)body.password=p;
      try{
        if(isNew)await api('/api/users',{method:'POST',body});
        else await api('/api/users/'+u.id,{method:'PUT',body});
        toast('Saved','success');c();renderUsers($('#view'));
      }catch(e){}
    }}]);
}

async function renderApps(root){
  const j=await api('/api/apps');
  const wrap=el('div');
  wrap.append(el('div',{class:'toolbar'},
    el('button',{class:'btn',onclick:()=>appModal()},'+ New Application'),
    el('button',{class:'btn ghost',onclick:()=>renderApps(root)},'⟳ Refresh')));
  if(!j.apps.length){wrap.append(el('div',{class:'empty'},'No applications yet.'));root.innerHTML='';root.append(wrap);return;}
  const t=el('table',{class:'table'});
  t.innerHTML='<thead><tr><th>ID</th><th>Name</th>'+(ADMIN?'<th>Owner</th>':'')+
    '<th>Runtime</th><th>Status</th><th>PID</th><th>Ports</th><th></th></tr></thead>';
  const tb=el('tbody');
  for(const a of j.apps){
    const tr=el('tr');
    tr.append(el('td',{},String(a.id)),el('td',{},a.name));
    if(ADMIN)tr.append(el('td',{},a.owner_name||''));
    tr.append(el('td',{},a.runtime),
      el('td',{},el('span',{class:'badge '+a.status},a.status)),
      el('td',{},a.pid?String(a.pid):'—'),
      el('td',{},(a.ports||[]).join(', ')||'—'));
    const acts=el('td');
    const bt=(label,cls,fn)=>{const b=el('button',{class:'btn small '+cls},label);b.onclick=fn;return b;};
    if(a.status!=='running')acts.append(bt('Start','success',()=>appAct(a.id,'start',root)));
    else acts.append(bt('Stop','warn',()=>appAct(a.id,'stop',root)));
    acts.append(bt('Restart','ghost',()=>appAct(a.id,'restart',root)));
    acts.append(bt('Logs','ghost',()=>showLogs(a)));
    acts.append(bt('Files','ghost',()=>{FILES_APP=a.id;FILES_PATH='';go('files');}));
    acts.append(bt('Edit','ghost',()=>appModal(a)));
    acts.append(bt('Del','danger',async()=>{if(await confirmDialog('Delete '+a.name+'?')){
      await api('/api/apps/'+a.id,{method:'DELETE'});toast('Deleted','success');renderApps(root);}}));
    tr.append(acts);tb.append(tr);
  }
  t.append(tb);wrap.append(t);root.innerHTML='';root.append(wrap);
}
async function appAct(id,action,root){
  try{const r=await api('/api/apps/'+id+'/'+action,{method:'POST'});
    toast(r.message||action,'success');renderApps(root);}catch(e){}
}
function showLogs(a){
  const box=el('div');
  const cons=el('div',{class:'console',id:'consoleBox'},'Loading…');
  box.append(el('div',{class:'toolbar'},
    el('button',{class:'btn small ghost',onclick:()=>refreshLogs(a.id,cons)},'⟳ Refresh'),
    el('button',{class:'btn small ghost',onclick:()=>clearView(cons)},'Clear View'),
    el('a',{class:'btn small ghost',href:'/api/apps/'+a.id+'/logs/download'},'Download')));
  box.append(cons);
  const m=modal('Logs — '+a.name,box,[{label:'Close',cls:'ghost',onClick:c=>c()}]);
  refreshLogs(a.id,cons);
  const iv=setInterval(()=>refreshLogs(a.id,cons),4000);
  const old=m.close;m.close=()=>{clearInterval(iv);old();};
}
async function refreshLogs(id,el){
  try{const j=await api('/api/apps/'+id+'/logs?tail=400');
    el.innerHTML='';
    for(const line of (j.logs||'').split('\n')){
      const cls=/error|exception|fail|traceback/i.test(line)?'err-line':'';
      el.append(el('div',{class:cls},line));}
    el.scrollTop=el.scrollHeight;
  }catch(e){}
}
function clearView(cons){cons.innerHTML='<div style="color:#a99ec7">View cleared (log file preserved)</div>';}

async function appModal(a){
  const isNew=!a;
  const nests=await api('/api/nests');
  const box=el('div');
  box.innerHTML=`
    <label>Name</label><input id="an" value="${a?a.name:''}">
    ${ADMIN?'<label>Owner ID</label><input id="ao" type="number" value="'+(a?a.owner_id:'')+'">':''}
    <label>Runtime</label><select id="ar">
      ${['python','node','java','php','static','other'].map(r=>`<option ${a&&a.runtime===r?'selected':''}>${r}</option>`).join('')}
    </select>
    <label>Egg (optional)</label><select id="ae"><option value="">—</option>
      ${nests.eggs.map(e=>`<option value="${e.id}" ${a&&a.egg_id===e.id?'selected':''}>${e.name}</option>`).join('')}
    </select>
    <label>Working directory (relative to apps/)</label><input id="aw" value="${a?a.workdir:''}" placeholder="my_bot">
    <label>Startup command</label><input id="as" value="${a?a.startup:''}" placeholder="python3 bot.py">
    <div class="row">
      <div><label>Ports (JSON array)</label><input id="ap" value='${a?JSON.stringify(a.ports||[]):"[]"}'></div>
      <div><label>Auto-restart</label><select id="aat">
        <option value="0" ${a&&!a.auto_restart?'selected':''}>off</option>
        <option value="1" ${a&&a.auto_restart?'selected':''}>on</option></select></div>
    </div>`;
  modal(isNew?'New Application':'Edit — '+a.name,box,[
    {label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Save',onClick:async c=>{
      const body={name:box.querySelector('#an').value,runtime:box.querySelector('#ar').value,
        egg_id:parseInt(box.querySelector('#ae').value)||null,
        workdir:box.querySelector('#aw').value,startup:box.querySelector('#as').value,
        ports:JSON.parse(box.querySelector('#ap').value||'[]'),
        auto_restart:box.querySelector('#aat').value==='1'};
      if(ADMIN&&box.querySelector('#ao'))body.owner_id=parseInt(box.querySelector('#ao').value)||null;
      try{
        if(isNew)await api('/api/apps',{method:'POST',body});
        else await api('/api/apps/'+a.id,{method:'PUT',body});
        toast('Saved','success');c();renderApps($('#view'));
      }catch(e){}
    }}]);
}

async function renderNests(root){
  const j=await api('/api/nests');
  const wrap=el('div');
  wrap.append(el('div',{class:'toolbar'},
    ADMIN?el('button',{class:'btn',onclick:()=>nestModal()},'+ New Nest'):null,
    ADMIN?el('button',{class:'btn',onclick:()=>eggModal(null,j)},'+ New Egg'):null));
  wrap.append(el('h2',{style:'font-size:15px;margin:14px 0 8px'},'Nests'));
  const nt=el('table',{class:'table'});
  nt.innerHTML='<thead><tr><th>ID</th><th>Name</th><th>Description</th><th></th></tr></thead>';
  const nb=el('tbody');
  for(const n of j.nests){
    const tr=el('tr',{},el('td',{},String(n.id)),el('td',{},n.name),el('td',{},n.description||''));
    const acts=el('td');
    if(ADMIN){
      acts.append(el('button',{class:'btn small danger',onclick:async()=>{
        if(await confirmDialog('Delete nest '+n.name+'?')){await api('/api/nests/'+n.id,{method:'DELETE'});
          toast('Deleted','success');renderNests(root);}}},'Del'));
    }
    tr.append(acts);nb.append(tr);
  }
  nt.append(nb);wrap.append(nt);
  wrap.append(el('h2',{style:'font-size:15px;margin:20px 0 8px'},'Eggs'));
  const et=el('table',{class:'table'});
  et.innerHTML='<thead><tr><th>ID</th><th>Name</th><th>Nest</th><th>Runtime</th><th>Startup</th><th></th></tr></thead>';
  const eb=el('tbody');
  const nestsById={};j.nests.forEach(n=>nestsById[n.id]=n.name);
  for(const e of j.eggs){
    const tr=el('tr',{},el('td',{},String(e.id)),el('td',{},e.name),
      el('td',{},nestsById[e.nest_id]||'—'),el('td',{},e.runtime),
      el('td',{style:'font-family:monospace;font-size:11px'},e.startup));
    const acts=el('td');
    if(ADMIN){
      acts.append(el('button',{class:'btn small ghost',onclick:()=>eggModal(e,j)},'Edit'));
      acts.append(document.createTextNode(' '));
      acts.append(el('button',{class:'btn small ghost',onclick:async()=>{
        await api('/api/eggs/'+e.id+'/clone',{method:'POST'});toast('Cloned','success');renderNests(root);}},'Clone'));
      acts.append(document.createTextNode(' '));
      acts.append(el('button',{class:'btn small danger',onclick:async()=>{
        if(await confirmDialog('Delete egg '+e.name+'?')){await api('/api/eggs/'+e.id,{method:'DELETE'});
          toast('Deleted','success');renderNests(root);}}},'Del'));
    }
    tr.append(acts);eb.append(tr);
  }
  et.append(eb);wrap.append(et);
  root.innerHTML='';root.append(wrap);
}
function nestModal(){const box=el('div');
  box.innerHTML='<label>Name</label><input id="nn"><label>Description</label><input id="nd">';
  modal('New Nest',box,[{label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Save',onClick:async c=>{await api('/api/nests',{method:'POST',
      body:{name:box.querySelector('#nn').value,description:box.querySelector('#nd').value}});
      toast('Created','success');c();renderNests($('#view'));}}]);}
function eggModal(e,nests){
  const isNew=!e;
  const box=el('div');
  box.innerHTML=`
    <label>Name</label><input id="en" value="${e?e.name:''}">
    <label>Nest</label><select id="enest">${nests.nests.map(n=>`<option value="${n.id}" ${e&&e.nest_id===n.id?'selected':''}>${n.name}</option>`).join('')}</select>
    <div class="row"><div><label>Runtime</label>
      <select id="ert">${['python','node','java','php','static','other'].map(r=>`<option ${e&&e.runtime===r?'selected':''}>${r}</option>`).join('')}</select></div>
      <div><label>Working dir</label><input id="ew" value="${e?e.working_dir||'.':'.'}"></div></div>
    <label>Startup</label><input id="es" value="${e?e.startup:''}">
    <label>Description</label><input id="ed" value="${e?e.description||'':''}">
    <label>Install notes</label><textarea id="ei">${e?e.install_notes||'':''}</textarea>`;
  modal(isNew?'New Egg':'Edit — '+e.name,box,[
    {label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Save',onClick:async c=>{
      const body={name:box.querySelector('#en').value,nest_id:parseInt(box.querySelector('#enest').value),
        runtime:box.querySelector('#ert').value,working_dir:box.querySelector('#ew').value,
        startup:box.querySelector('#es').value,description:box.querySelector('#ed').value,
        install_notes:box.querySelector('#ei').value};
      try{
        if(isNew)await api('/api/eggs',{method:'POST',body});
        else await api('/api/eggs/'+e.id,{method:'PUT',body});
        toast('Saved','success');c();renderNests($('#view'));
      }catch(e){}
    }}]);
}

async function renderFiles(root){
  const apps=await api('/api/apps');
  const box=el('div');
  const sel=el('select',{class:'tab',style:'padding:9px;background:var(--card);border:1px solid var(--border);color:var(--text)'});
  sel.append(el('option',{value:''},'— select application —'));
  for(const a of apps.apps)sel.append(el('option',{value:a.id},a.name));
  if(FILES_APP)sel.value=String(FILES_APP);
  sel.onchange=()=>{FILES_APP=parseInt(sel.value)||null;FILES_PATH='';renderFiles(root);};
  box.append(el('div',{class:'toolbar'},sel));
  if(!FILES_APP){box.append(el('div',{class:'empty'},'Select an application to browse its files.'));root.innerHTML='';root.append(box);return;}
  try{
    const j=await api('/api/apps/'+FILES_APP+'/files?path='+encodeURIComponent(FILES_PATH));
    const bar=el('div',{class:'toolbar'});
    bar.append(el('div',{class:'breadcrumb'},'/'+(FILES_PATH||'')));
    bar.append(el('button',{class:'btn small ghost',onclick:()=>{FILES_PATH='';renderFiles(root);}},'🏠 Root'));
    if(FILES_PATH)bar.append(el('button',{class:'btn small ghost',onclick:()=>{
      const parts=FILES_PATH.split('/');parts.pop();FILES_PATH=parts.join('/');renderFiles(root);}},'↑ Up'));
    bar.append(el('button',{class:'btn small',onclick:()=>mkdirFile(root)},'+ Folder'));
    bar.append(el('button',{class:'btn small',onclick:()=>uploadFile(root)},'↑ Upload'));
    box.append(bar);
    const t=el('table',{class:'table'});
    t.innerHTML='<thead><tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead>';
    const tb=el('tbody');
    for(const it of j.items){
      const tr=el('tr',{class:'file-row'});
      const nameTd=el('td',{},(it.is_dir?'📁 ':'📄 ')+it.name);
      nameTd.onclick=()=>{if(it.is_dir){FILES_PATH=(FILES_PATH?FILES_PATH+'/':'')+it.name;renderFiles(root);}
        else editFile(FILES_APP,(FILES_PATH?FILES_PATH+'/':'')+it.name,root);};
      tr.append(nameTd,el('td',{},it.is_dir?'—':human(it.size)),
        el('td',{},it.mtime.slice(0,16).replace('T',' ')));
      const acts=el('td');
      if(!it.is_dir)acts.append(el('a',{class:'btn small ghost',
        href:'/api/apps/'+FILES_APP+'/files/download?path='+encodeURIComponent((FILES_PATH?FILES_PATH+'/':'')+it.name)},'↓'));
      acts.append(document.createTextNode(' '));
      acts.append(el('button',{class:'btn small danger',onclick:async()=>{
        if(await confirmDialog('Delete '+it.name+'?')){
          await api('/api/apps/'+FILES_APP+'/files/delete',{method:'POST',
            body:{path:(FILES_PATH?FILES_PATH+'/':'')+it.name}});
          toast('Deleted','success');renderFiles(root);}}},'Del'));
      tr.append(acts);tb.append(tr);
    }
    t.append(tb);box.append(t);
  }catch(e){box.append(el('div',{class:'empty'},'Error loading files.'));}
  root.innerHTML='';root.append(box);
}
function mkdirFile(root){const box=el('div');box.innerHTML='<label>Folder name</label><input id="fn">';
  modal('New Folder',box,[{label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Create',onClick:async c=>{await api('/api/apps/'+FILES_APP+'/files/mkdir',{method:'POST',
      body:{path:(FILES_PATH?FILES_PATH+'/':'')+box.querySelector('#fn').value}});
      toast('Created','success');c();renderFiles(root);}}]);}
function uploadFile(root){
  const box=el('div');
  box.innerHTML='<label>Choose file</label><input type="file" id="uf">';
  modal('Upload File',box,[{label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Upload',onClick:async c=>{const f=box.querySelector('#uf').files[0];if(!f)return;
      const fd=new FormData();fd.append('file',f);fd.append('path',FILES_PATH);
      await api('/api/apps/'+FILES_APP+'/files/upload',{method:'POST',body:fd});
      toast('Uploaded','success');c();renderFiles(root);}}]);
}
async function editFile(aid,path,root){
  const j=await api('/api/apps/'+aid+'/files/read?path='+encodeURIComponent(path));
  const box=el('div');
  box.innerHTML='<textarea id="fc" style="min-height:320px;width:100%;font-family:monospace"></textarea>';
  box.querySelector('#fc').value=j.content;
  modal('Edit — '+path,box,[
    {label:'Cancel',cls:'ghost',onClick:c=>c()},
    {label:'Save',onClick:async c=>{await api('/api/apps/'+aid+'/files/write',{method:'POST',
      body:{path,content:box.querySelector('#fc').value}});toast('Saved','success');c();}}]);
}

async function renderNetwork(root){
  const j=await api('/api/ports');
  const wrap=el('div');
  const card=(title,body)=>el('div',{class:'card',style:'margin-bottom:14px'},el('h3',{},title),body);
  const pdiv=el('div');
  if(Object.keys(j.configured).length===0)pdiv.append(el('div',{class:'empty'},'No ports configured.'));
  else{const t=el('table',{class:'table'});
    t.innerHTML='<thead><tr><th>Port</th><th>Applications</th></tr></thead>';
    const tb=el('tbody');
    for(const[p,apps]of Object.entries(j.configured)){
      const dup=j.duplicates[p];
      const tr=el('tr',{},el('td',{},p+(dup?' ⚠':'')),el('td',{},apps.join(', ')));
      if(dup)tr.style.background='rgba(248,113,113,.08)';tb.append(tr);}
    t.append(tb);pdiv.append(t);}
  wrap.append(card('Configured Ports',pdiv));
  const ldiv=el('div');
  if(!j.listening.length)ldiv.append(el('div',{class:'empty'},'No listeners detected.'));
  else{const t=el('table',{class:'table'});
    t.innerHTML='<thead><tr><th>Type</th><th>Address</th><th>Port</th><th>PID</th></tr></thead>';
    const tb=el('tbody');
    for(const l of j.listening)tb.append(el('tr',{},el('td',{},l.type),
      el('td',{},l.addr),el('td',{},String(l.port)),el('td',{},l.pid?String(l.pid):'—')));
    t.append(tb);ldiv.append(t);}
  wrap.append(card('Listening Sockets',ldiv));
  wrap.append(card('About Accessibility',el('div',{class:'sub'},
    'This panel binds to 127.0.0.1 by default. Inside Android PRoot, ports are local only — '+
    'exposing them to the internet requires a real Linux host or Termux port-forwarding. '+
    'UDP (Bedrock) may not be reachable externally.')));
  root.innerHTML='';root.append(wrap);
}

async function renderLogs(root){
  const j=await api('/api/audit?limit=300');
  const wrap=el('div');
  wrap.append(el('div',{class:'toolbar'},el('button',{class:'btn ghost',onclick:()=>renderLogs(root)},'⟳ Refresh')));
  const t=el('table',{class:'table'});
  t.innerHTML='<thead><tr><th>Time</th><th>User</th><th>Action</th><th>Detail</th><th>IP</th></tr></thead>';
  const tb=el('tbody');
  for(const e of j.events)tb.append(el('tr',{},el('td',{},e.created_at),
    el('td',{},e.username||'—'),el('td',{},e.action),el('td',{},e.detail||''),el('td',{},e.ip||'')));
  t.append(tb);wrap.append(t);root.innerHTML='';root.append(wrap);
}

(async function(){
  try{await loadMe();go('dashboard');}catch(e){console.error(e);}
})();
</script></body></html>"""

@app.route("/")
@login_required
def index():
    return render_template_string(MAIN_HTML)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    init_db(seed=True)
    if INIT_ONLY:
        create_admin_from_env()
        return
    create_admin_from_env()
    threading.Thread(target=monitor_loop, daemon=True).start()

    def _sig(signum, frame):
        print("\nShutting down gracefully...", flush=True)
        shutdown_all()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    print(f"FaiLive2 Hosting Panel running at http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False, debug=False)

if __name__ == "__main__":
    main()
