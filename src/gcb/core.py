"""Durable local queue. A claimed request is never silently retried."""
import hashlib
import csv
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid


MAX_PENDING = 32
MAX_PROMPT = 16000
MAX_REPLY = 200000


class BridgeError(Exception):
    pass


SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\b(?:sk-ant-|sk-proj-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}"),
)


def redact(value):
    if not isinstance(value, str):
        return value
    value = SECRET_PATTERNS[0].sub(r"\1[REDACTED]",value)
    value = SECRET_PATTERNS[1].sub(r"\1[REDACTED]",value)
    return SECRET_PATTERNS[2].sub("[REDACTED]",value)


def _private_home(home, created):
    if os.name != "nt":
        if created:
            home.chmod(0o700)
        if home.stat().st_mode & 0o077:
            raise BridgeError("Queue directory must be owner-only (mode 0700)")
        return
    try:
        ident = subprocess.run(["whoami","/user","/fo","csv","/nh"],capture_output=True,text=True,check=True,timeout=5)
        sid = next(csv.reader(ident.stdout.splitlines()))[-1].strip()
        if not sid.startswith("S-1-5-"):
            raise ValueError("Invalid Windows SID")
        allowed = {sid.casefold(),"s-1-5-18","s-1-5-32-544",
                   (os.environ.get("USERDOMAIN","")+"\\"+getpass.getuser()).casefold(),
                   "nt authority\\system","builtin\\administrators"}
        for path in (home, *(home / n for n in ("bridge.sqlite","bridge.sqlite-wal","bridge.sqlite-shm","worker.lock"))):
            if not path.exists():
                continue
            acl = subprocess.run(["icacls",str(path)],capture_output=True,text=True,check=True,timeout=10).stdout
            entries = 0
            for line in acl.splitlines():
                if ":(" not in line:
                    continue
                entries += 1
                principal = line.split(":(",1)[0].strip()
                if principal.startswith(str(path)):
                    principal = principal[len(str(path)):].strip()
                rights = line.split(":(",1)[1]
                if principal.casefold() not in allowed and rights != "S,X)":
                    raise BridgeError("Queue state ACL includes an unapproved principal")
            if not entries:
                raise BridgeError("Cannot parse queue state ACL")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise BridgeError("Cannot verify private queue directory ACL") from exc


def canonical(path):
    return str(Path(path).expanduser().resolve(strict=True))


def git_identity(path):
    p = canonical(path)
    from .gitops import run as safe_git
    def git(*args):
        r = safe_git(p,*args,text=True)
        if r.returncode:
            raise BridgeError("Repository must be an existing Git working tree")
        return canonical(r.stdout.strip())
    top = git("rev-parse", "--show-toplevel")
    if os.path.normcase(top) != os.path.normcase(p):
        raise BridgeError("Register the Git root, not a subdirectory")
    return top, git("rev-parse", "--absolute-git-dir")


def repo_id(top, git_dir):
    return hashlib.sha256((os.path.normcase(top) + "\0" + os.path.normcase(git_dir)).encode()).hexdigest()[:20]


def connect(home):
    h = Path(home).expanduser()
    created = not h.exists()
    h.mkdir(parents=True, exist_ok=True)
    _private_home(h,created)
    db = sqlite3.connect(h / "bridge.sqlite", timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS repos (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, git_dir TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
            repo_id TEXT NOT NULL REFERENCES repos(id), adapter TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_completed INTEGER);
        CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            source TEXT NOT NULL, key TEXT NOT NULL, prompt TEXT NOT NULL,
            state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            started_at INTEGER, finished_at INTEGER, reply TEXT, error TEXT,
            UNIQUE(source,key));
        CREATE TABLE IF NOT EXISTS audit (at INTEGER NOT NULL, request_id TEXT, event TEXT NOT NULL,
            detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS patch_scopes (session_id TEXT PRIMARY KEY REFERENCES sessions(id),
            branch TEXT NOT NULL, head TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS patch_files (request_id TEXT NOT NULL REFERENCES requests(id),
            path TEXT NOT NULL, PRIMARY KEY(request_id,path));
        CREATE TABLE IF NOT EXISTS patch_requests (request_id TEXT PRIMARY KEY REFERENCES requests(id),
            instruction TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS patch_applied (request_id TEXT PRIMARY KEY REFERENCES requests(id),
            digest TEXT NOT NULL, applied_at INTEGER NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_targets (session_id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL REFERENCES repos(id), created_at INTEGER NOT NULL,
            bound_pid INTEGER, bound_nonce TEXT, bound_at INTEGER, last_seen_at INTEGER);
        CREATE TABLE IF NOT EXISTS channel_requests (id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES channel_targets(session_id),
            source TEXT NOT NULL, key TEXT NOT NULL, prompt TEXT NOT NULL,
            state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            sent_at INTEGER, finished_at INTEGER, reply TEXT, error TEXT,
            UNIQUE(source,key));
    """)
    return db


def audit(db, request_id, event, detail=""):
    db.execute("INSERT INTO audit VALUES (?,?,?,?)", (int(time.time()), request_id, event, detail))


def add_repo(db, path):
    top, git_dir = git_identity(path)
    rid = repo_id(top,git_dir)
    db.execute("INSERT OR IGNORE INTO repos VALUES (?,?,?)", (rid, top, git_dir))
    row = db.execute("SELECT * FROM repos WHERE id=?", (rid,)).fetchone()
    if row["path"] != top or row["git_dir"] != git_dir:
        raise BridgeError("Repository identity collision")
    return dict(row)


def add_session(db, repo_id, name, adapter):
    if adapter not in ("mock", "claude", "claude-patch"):
        raise BridgeError("Unsupported adapter")
    if not name or len(name) > 80:
        raise BridgeError("Session name must be 1 to 80 characters")
    if not db.execute("SELECT 1 FROM repos WHERE id=?", (repo_id,)).fetchone():
        raise BridgeError("Repository is not allowlisted")
    sid = str(uuid.uuid4())
    if adapter == "claude-patch":
        path = db.execute("SELECT path FROM repos WHERE id=?",(repo_id,)).fetchone()[0]
        from .gitops import run as safe_git
        common = safe_git(path,"rev-parse","--path-format=absolute","--git-common-dir",text=True)
        git_dir = db.execute("SELECT git_dir FROM repos WHERE id=?",(repo_id,)).fetchone()[0]
        if common.returncode or os.path.normcase(canonical(common.stdout.strip())) == os.path.normcase(git_dir):
            raise BridgeError("Patch mode requires a separate linked Git worktree")
        branch, head = patch_checkpoint(path)
        require_clean(path)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,NULL)", (sid, name, repo_id, adapter, int(time.time())))
        if adapter == "claude-patch":
            db.execute("INSERT INTO patch_scopes VALUES (?,?,?)",(sid,branch,head))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


def patch_checkpoint(path):
    from .gitops import run as safe_git
    branch = safe_git(path,"symbolic-ref","--short","HEAD",text=True)
    head = safe_git(path,"rev-parse","HEAD",text=True)
    if branch.returncode or head.returncode or not branch.stdout.strip():
        raise BridgeError("Patch mode requires a committed, named branch")
    return branch.stdout.strip(), head.stdout.strip()


def require_clean(path):
    from .gitops import run as safe_git
    result = safe_git(path,"status","--porcelain","--ignore-submodules=all",text=True)
    if result.returncode or result.stdout.strip():
        raise BridgeError("Patch mode requires a clean Git worktree under bridge Git settings")


def check_patch_scope(db, session):
    if session["adapter"] != "claude-patch":
        raise BridgeError("Session is not in patch mode")
    scope = db.execute("SELECT * FROM patch_scopes WHERE session_id=?",(session["id"],)).fetchone()
    if not scope:
        raise BridgeError("Patch scope is missing")
    if patch_checkpoint(session["path"]) != (scope["branch"],scope["head"]):
        raise BridgeError("Patch branch or base commit changed")
    return scope


def check_session(db, sid):
    row = db.execute("SELECT s.*,r.path,r.git_dir FROM sessions s JOIN repos r ON s.repo_id=r.id WHERE s.id=?", (sid,)).fetchone()
    if not row:
        raise BridgeError("Unknown bridge-owned session")
    try:
        top, gd = git_identity(row["path"])
    except (BridgeError, FileNotFoundError) as e:
        raise BridgeError("Repository is missing or stale") from e
    if os.path.normcase(top) != os.path.normcase(row["path"]) or os.path.normcase(gd) != os.path.normcase(row["git_dir"]):
        raise BridgeError("Repository identity changed")
    if repo_id(top,gd) != row["repo_id"]:
        raise BridgeError("Repository allowlist identity changed")
    return row


def send(db, sid, source, key, prompt, ttl=86400, files=()):
    session = check_session(db, sid)
    if session["adapter"] == "claude-patch":
        original = prompt
        old = db.execute("SELECT * FROM requests WHERE source=? AND key=?",(source,key)).fetchone()
        if old:
            prior = db.execute("SELECT instruction FROM patch_requests WHERE request_id=?",(old["id"],)).fetchone()
            prior_files = [r[0] for r in db.execute("SELECT path FROM patch_files WHERE request_id=? ORDER BY rowid",(old["id"],))]
            if old["session_id"] != sid or not prior or prior["instruction"] != original or prior_files != list(files):
                raise BridgeError("Idempotency key already used for a different request")
            return dict(old)
        from .patches import context_prompt
        check_patch_scope(db, session)
        require_clean(session["path"])
        prompt, files = context_prompt(session["path"], prompt, files)
    elif files:
        raise BridgeError("File context requires a patch session")
    if not source or len(source) > 100 or not key or len(key) > 128:
        raise BridgeError("Source and idempotency key are required and bounded")
    if not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT:
        raise BridgeError("Prompt is empty or too large")
    if ttl < 1 or ttl > 604800:
        raise BridgeError("TTL must be 1 to 604800 seconds")
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        old = db.execute("SELECT * FROM requests WHERE source=? AND key=?", (source, key)).fetchone()
        if old:
            if old["session_id"] != sid or old["prompt"] != prompt:
                raise BridgeError("Idempotency key already used for a different request")
            db.execute("COMMIT")
            return dict(old)
        pending = db.execute("SELECT count(*) FROM requests WHERE state IN ('queued','running')").fetchone()[0]
        if pending >= MAX_PENDING:
            raise BridgeError("Queue is full")
        rid = str(uuid.uuid4())
        db.execute("INSERT INTO requests (id,session_id,source,key,prompt,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                   (rid,sid,source,key,prompt,"queued",now,now+ttl))
        for path in files:
            db.execute("INSERT INTO patch_files VALUES (?,?)",(rid,path))
        if session["adapter"] == "claude-patch":
            db.execute("INSERT INTO patch_requests VALUES (?,?)",(rid,original))
        audit(db,rid,"queued",f"source={redact(source)}; bytes={len(prompt.encode('utf-8'))}; sha256={hashlib.sha256(prompt.encode()).hexdigest()}")
        db.execute("COMMIT")
        return dict(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())
    except Exception:
        db.execute("ROLLBACK")
        raise


def recover(db):
    """A crash may have delivered a prompt; require inspection before any replay."""
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute("SELECT id FROM requests WHERE state='running'").fetchall()
        for row in rows:
            db.execute("UPDATE requests SET state='unknown',finished_at=?,error='Worker stopped after claim; delivery may have occurred' WHERE id=?", (int(time.time()),row["id"]))
            audit(db,row["id"],"unknown","worker recovery")
        db.execute("COMMIT")
        return len(rows)
    except Exception:
        db.execute("ROLLBACK")
        raise


def claim(db):
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        expired = db.execute("SELECT id FROM requests WHERE state='queued' AND expires_at<=?", (now,)).fetchall()
        for row in expired:
            db.execute("UPDATE requests SET state='expired',finished_at=? WHERE id=?", (now,row["id"]))
            audit(db,row["id"],"expired")
        row = db.execute("SELECT q.* FROM requests q WHERE q.state='queued' AND NOT EXISTS (SELECT 1 FROM requests x WHERE x.session_id=q.session_id AND x.state IN ('running','unknown')) ORDER BY q.created_at,q.rowid LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE requests SET state='running',started_at=? WHERE id=?", (now,row["id"]))
            audit(db,row["id"],"claimed")
        db.execute("COMMIT")
        return dict(row) if row else None
    except Exception:
        db.execute("ROLLBACK")
        raise


def finish(db, rid, state, reply="", error="", launched=False):
    if state not in ("completed","failed","blocked","unknown"):
        raise BridgeError("Invalid final state")
    if len(reply.encode("utf-8")) > MAX_REPLY:
        reply = reply.encode("utf-8")[:MAX_REPLY].decode("utf-8",errors="ignore")
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not row or row["state"] != "running":
            raise BridgeError("Request is not claimed")
        db.execute("UPDATE requests SET state=?,reply=?,error=?,finished_at=? WHERE id=?", (state,redact(reply),redact(error),int(time.time()),rid))
        if launched:
            db.execute("UPDATE sessions SET last_completed=? WHERE id=?", (int(time.time()),row["session_id"]))
        audit(db,rid,state,f"reply_bytes={len(reply.encode('utf-8'))}; error_type={error[:80]}")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def public_request(row, include_text=False):
    d = {k:row[k] for k in ("id","session_id","source","key","state","created_at","expires_at","started_at","finished_at","error") if k in row.keys()}
    for key in ("source","key","error"):
        if key in d:
            d[key] = redact(d[key])
    d["reply"] = redact(row["reply"])
    if include_text:
        d["prompt"] = redact(row["prompt"])
    return d
