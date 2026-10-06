"""Durable, fail-closed delivery state for an opted-in Claude channel."""
import hashlib
import os
import time
import uuid

from .core import BridgeError, MAX_PENDING, MAX_PROMPT, MAX_REPLY, audit, git_identity, redact, repo_id
from .targets import check_target


def _target(db, session_id):
    row = db.execute("SELECT t.*,r.path,r.git_dir FROM channel_targets t JOIN repos r ON r.id=t.repo_id WHERE t.session_id=?",(session_id,)).fetchone()
    if not row:
        raise BridgeError("Channel target is not registered")
    top, git_dir = git_identity(row["path"])
    if os.path.normcase(top) != os.path.normcase(row["path"]) or os.path.normcase(git_dir) != os.path.normcase(row["git_dir"]) or repo_id(top,git_dir) != row["repo_id"]:
        raise BridgeError("Channel repository identity changed")
    return row


def register(db, session_id, rid):
    repo = db.execute("SELECT * FROM repos WHERE id=?",(rid,)).fetchone()
    if not repo:
        raise BridgeError("Repository is not allowlisted")
    target = check_target(session_id,repo["path"])
    db.execute("INSERT OR IGNORE INTO channel_targets (session_id,repo_id,created_at) VALUES (?,?,?)",
               (target["session_id"],rid,int(time.time())))
    row = _target(db,target["session_id"])
    if row["repo_id"] != rid:
        raise BridgeError("Channel target is already registered to another repository")
    return {"session_id":row["session_id"],"repo_id":rid,"state":"registered_unbound",
            "delivery_supported":False}


def bind(db, session_id, parent_pid):
    row = _target(db,session_id)
    target = check_target(session_id,row["path"])
    if target["pid"] != parent_pid:
        raise BridgeError("Channel process is not owned by the target Claude session")
    nonce = str(uuid.uuid4())
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        pending = db.execute("SELECT id FROM channel_requests WHERE session_id=? AND state IN ('dispatching','emitted')",(session_id,)).fetchall()
        for item in pending:
            db.execute("UPDATE channel_requests SET state='unknown',finished_at=?,error='Channel restarted after dispatch; delivery may have occurred' WHERE id=?",(now,item["id"]))
            audit(db,item["id"],"channel_unknown","binding restarted")
        db.execute("UPDATE channel_targets SET bound_pid=?,bound_nonce=?,bound_at=? WHERE session_id=?",(parent_pid,nonce,now,session_id))
        audit(db,None,"channel_bound",f"session_id={session_id}; pid={parent_pid}")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"session_id":session_id,"nonce":nonce,"bound_pid":parent_pid}


def _bound(db, session_id, nonce):
    row = _target(db,session_id)
    if not row["bound_nonce"] or row["bound_nonce"] != nonce:
        raise BridgeError("Channel binding is missing or stale")
    return row


def send(db, session_id, source, key, prompt, ttl=86400):
    _target(db,session_id)
    if not source or len(source) > 100 or not key or len(key) > 128:
        raise BridgeError("Source and idempotency key are required and bounded")
    if not prompt or len(prompt.encode()) > MAX_PROMPT:
        raise BridgeError("Prompt is empty or too large")
    if ttl < 1 or ttl > 604800:
        raise BridgeError("TTL must be 1 to 604800 seconds")
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        row = _target(db,session_id)
        old = db.execute("SELECT * FROM channel_requests WHERE source=? AND key=?",(source,key)).fetchone()
        if old:
            if old["session_id"] != session_id or old["prompt"] != prompt:
                raise BridgeError("Idempotency key already used for a different channel request")
            db.execute("COMMIT")
            return public_request(old)
        if not row["bound_nonce"]:
            raise BridgeError("Channel target has not opted in and bound")
        target = check_target(session_id,row["path"])
        if target["pid"] != row["bound_pid"]:
            raise BridgeError("Channel target process changed; rebind at a checkpoint")
        count = db.execute("SELECT count(*) FROM channel_requests WHERE state IN ('queued','dispatching','emitted')").fetchone()[0]
        if count >= MAX_PENDING:
            raise BridgeError("Channel queue is full")
        request_id = str(uuid.uuid4())
        db.execute("INSERT INTO channel_requests (id,session_id,source,key,prompt,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                   (request_id,session_id,source,key,prompt,"queued",now,now+ttl))
        audit(db,request_id,"channel_queued",f"source={redact(source)}; bytes={len(prompt.encode())}; sha256={hashlib.sha256(prompt.encode()).hexdigest()}")
        db.execute("COMMIT")
        return public_request(db.execute("SELECT * FROM channel_requests WHERE id=?",(request_id,)).fetchone())
    except Exception:
        db.execute("ROLLBACK")
        raise


def next_request(db, session_id, nonce):
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        _bound(db,session_id,nonce)
        for item in db.execute("SELECT id FROM channel_requests WHERE session_id=? AND state='queued' AND expires_at<=?",(session_id,now)):
            db.execute("UPDATE channel_requests SET state='expired',finished_at=? WHERE id=?",(now,item["id"]))
            audit(db,item["id"],"channel_expired")
        active = db.execute("SELECT 1 FROM channel_requests WHERE session_id=? AND state IN ('dispatching','emitted','unknown') LIMIT 1",(session_id,)).fetchone()
        if active:
            db.execute("COMMIT")
            return None
        item = db.execute("SELECT * FROM channel_requests WHERE session_id=? AND state='queued' ORDER BY created_at,rowid LIMIT 1",(session_id,)).fetchone()
        if item:
            db.execute("UPDATE channel_requests SET state='dispatching',sent_at=? WHERE id=?",(now,item["id"]))
            audit(db,item["id"],"channel_dispatching")
        db.execute("COMMIT")
        return {"id":item["id"],"prompt":item["prompt"],"source":item["source"],
                "session_id":session_id} if item else None
    except Exception:
        db.execute("ROLLBACK")
        raise


def emitted(db, session_id, nonce, request_id):
    db.execute("BEGIN IMMEDIATE")
    try:
        _bound(db,session_id,nonce)
        row = db.execute("SELECT * FROM channel_requests WHERE id=? AND session_id=?",(request_id,session_id)).fetchone()
        if not row:
            raise BridgeError("Unknown channel request")
        if row["state"] == "completed":
            result = {"id":request_id,"state":"completed"}
        else:
            if row["state"] not in ("dispatching","emitted"):
                raise BridgeError("Channel request is not dispatching")
            if row["state"] == "dispatching":
                db.execute("UPDATE channel_requests SET state='emitted' WHERE id=?",(request_id,))
                audit(db,request_id,"channel_emitted","MCP transport write only; not model receipt")
            result = {"id":request_id,"state":"emitted"}
        db.execute("COMMIT")
        return result
    except Exception:
        db.execute("ROLLBACK")
        raise


def reply(db, session_id, nonce, request_id, text):
    if not isinstance(text,str) or len(text.encode()) > MAX_REPLY:
        raise BridgeError("Channel reply is invalid or too large")
    db.execute("BEGIN IMMEDIATE")
    try:
        _bound(db,session_id,nonce)
        row = db.execute("SELECT * FROM channel_requests WHERE id=? AND session_id=?",(request_id,session_id)).fetchone()
        if not row:
            raise BridgeError("Unknown channel request")
        if row["state"] == "completed":
            if row["reply"] != redact(text):
                raise BridgeError("Channel reply already recorded with different text")
            db.execute("COMMIT")
            return {"id":request_id,"state":"completed","duplicate":True}
        if row["state"] not in ("dispatching","emitted"):
            raise BridgeError("Channel request is not awaiting a reply")
        db.execute("UPDATE channel_requests SET state='completed',reply=?,finished_at=? WHERE id=?",(redact(text),int(time.time()),request_id))
        audit(db,request_id,"channel_completed",f"reply_bytes={len(text.encode())}")
        db.execute("COMMIT")
        return {"id":request_id,"state":"completed","duplicate":False}
    except Exception:
        db.execute("ROLLBACK")
        raise


def public_request(row, include_text=False):
    result = {k:row[k] for k in ("id","session_id","state","created_at","expires_at","sent_at","finished_at")}
    result["source"] = redact(row["source"])
    result["key"] = redact(row["key"])
    result["reply"] = redact(row["reply"])
    result["error"] = redact(row["error"])
    if include_text:
        result["prompt"] = redact(row["prompt"])
    return result


def resolve(db, request_id, reason):
    if len(reason) < 10 or len(reason) > 200:
        raise BridgeError("Resolution reason must be 10 to 200 characters")
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM channel_requests WHERE id=?",(request_id,)).fetchone()
        if not row or row["state"] != "unknown":
            raise BridgeError("Only unknown channel requests can be resolved")
        db.execute("UPDATE channel_requests SET state='resolved',error=? WHERE id=?",(redact(reason),request_id))
        audit(db,request_id,"channel_resolved","operator inspection recorded")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"id":request_id,"state":"resolved"}
