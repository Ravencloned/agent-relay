import argparse
import json
import os
from pathlib import Path
import sys
import time

from .adapters import ADAPTERS
from .core import BridgeError, add_repo, add_session, check_session, claim, connect, finish, public_request, recover, redact, send
from .patches import apply_reviewed, review
from .targets import check_target, discover, public_target
from . import channel_queue


def parser():
    p = argparse.ArgumentParser(prog="gcb", description="Local Claude Code task bridge; JSON output, no daemon or network listener")
    p.add_argument("--home", default=os.environ.get("GCB_HOME", str(Path.home() / ".gcb")), help="Private queue directory (default: ~/.gcb)")
    sub = p.add_subparsers(dest="command", required=True)
    x = sub.add_parser("targets", help="Inspect Claude's active sessions without opening a bridge queue")
    x.add_argument("--include-path", action="store_true", help="Show each session's full working directory")
    x = sub.add_parser("target-check", help="Verify exact Claude session and repository path; never sends a message")
    x.add_argument("--session", required=True)
    x.add_argument("--repo", required=True)
    x = sub.add_parser("channel-register", help="Register a discovered Claude target; does not enable or contact its channel")
    x.add_argument("--session",required=True)
    x.add_argument("--repo",required=True,help="Allowlisted repository ID")
    x = sub.add_parser("channel-send", help="Queue a message for an already bound channel")
    x.add_argument("--session",required=True)
    x.add_argument("--source",required=True)
    x.add_argument("--key",required=True)
    x.add_argument("--ttl",type=int,default=86400)
    x.add_argument("--text",help="Message text, or omit to read stdin")
    x = sub.add_parser("channel-read", help="Read channel delivery state and reply")
    x.add_argument("id")
    x.add_argument("--include-prompt",action="store_true")
    x = sub.add_parser("channel-watch", help="Poll a channel message until completed or timeout")
    x.add_argument("id")
    x.add_argument("--timeout",type=int,default=60)
    x = sub.add_parser("channel-resolve",help="Release an unknown channel outcome after manual inspection")
    x.add_argument("id")
    x.add_argument("--reason",required=True)
    sub.add_parser("channel-status",help="List registered channel targets and queue counts")
    x = sub.add_parser("repo-add", help="Allowlist an existing Git root")
    x.add_argument("path")
    sub.add_parser("repos", help="List allowlisted repositories")
    x = sub.add_parser("session-add", help="Create bridge-owned session metadata; does not call a model")
    x.add_argument("--repo", required=True)
    x.add_argument("--name", required=True)
    x.add_argument("--adapter", choices=ADAPTERS, default="mock")
    sub.add_parser("sessions", help="List only bridge-owned sessions")
    x = sub.add_parser("send", help="Queue a request and return its stable ID")
    x.add_argument("--session", required=True)
    x.add_argument("--source", required=True, help="Authenticated upstream actor label; local CLI trusts OS access")
    x.add_argument("--key", required=True, help="Caller-generated stable idempotency key")
    x.add_argument("--ttl", type=int, default=86400)
    x.add_argument("--text", help="Prompt text, or omit to read stdin")
    x.add_argument("--file", action="append", default=[], help="Tracked UTF-8 file to show Claude in patch mode; repeat")
    x = sub.add_parser("run-once", help="Process at most one queued request")
    x.add_argument("--live", action="store_true", help="Permit a Claude model call")
    x.add_argument("--budget-usd", type=float, help="Required per-turn Claude cost cap")
    x.add_argument("--timeout", type=int, default=1800)
    x = sub.add_parser("read", help="Read request status and final reply")
    x.add_argument("id")
    x.add_argument("--include-prompt", action="store_true")
    x = sub.add_parser("watch", help="Poll a request until terminal status or timeout")
    x.add_argument("id")
    x.add_argument("--timeout", type=int, default=60)
    x.add_argument("--include-prompt", action="store_true")
    sub.add_parser("status", help="Queue counts and unknown outcomes")
    x = sub.add_parser("audit", help="Redacted delivery event metadata")
    x.add_argument("--limit", type=int, default=50)
    x = sub.add_parser("patch-check", help="Check a completed patch against its pinned clean worktree")
    x.add_argument("id")
    x = sub.add_parser("patch-apply", help="Apply an exactly reviewed patch locally")
    x.add_argument("id")
    x.add_argument("--digest", required=True)
    x.add_argument("--source", required=True, help="Authenticated local approver label")
    x = sub.add_parser("resolve", help="Manually release an unknown result after inspection; no automatic replay")
    x.add_argument("id")
    x.add_argument("--reason", required=True)
    x.add_argument("--session-exists", choices=("yes","no"), required=True,
                   help="After inspecting Claude state, whether its session UUID was initialized")
    return p


def output(obj):
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def run(args):
    if args.command == "targets":
        output([public_target(item,args.include_path) for item in discover()])
        return
    if args.command == "target-check":
        output(check_target(args.session,args.repo))
        return
    db = connect(args.home)
    try:
        return _run_open(db,args)
    finally:
        db.close()


def _run_open(db,args):
    if args.command == "channel-register":
        output(channel_queue.register(db,args.session,args.repo))
    elif args.command == "channel-send":
        prompt = args.text if args.text is not None else sys.stdin.read(16001)
        output(channel_queue.send(db,args.session,args.source,args.key,prompt,args.ttl))
    elif args.command in ("channel-read","channel-watch"):
        deadline = time.monotonic() + args.timeout if args.command == "channel-watch" else 0
        while True:
            row = db.execute("SELECT * FROM channel_requests WHERE id=?",(args.id,)).fetchone()
            if not row:
                raise BridgeError("Unknown channel request")
            if args.command == "channel-read" or row["state"] in ("completed","unknown","expired","resolved") or time.monotonic() >= deadline:
                output(channel_queue.public_request(row,include_text=getattr(args,"include_prompt",False)))
                return
            time.sleep(0.5)
    elif args.command == "channel-resolve":
        output(channel_queue.resolve(db,args.id,args.reason))
    elif args.command == "channel-status":
        output({"targets":[{"session_id":r["session_id"],"repo_id":r["repo_id"],"bound":bool(r["bound_nonce"]),"bound_pid":r["bound_pid"]}
                           for r in db.execute("SELECT * FROM channel_targets ORDER BY created_at")],
                "counts":{r["state"]:r["n"] for r in db.execute("SELECT state,count(*) n FROM channel_requests GROUP BY state")}})
    elif args.command == "repo-add":
        output(add_repo(db,args.path))
    elif args.command == "repos":
        output([dict(r) for r in db.execute("SELECT * FROM repos ORDER BY path")])
    elif args.command == "session-add":
        output(add_session(db,args.repo,args.name,args.adapter))
    elif args.command == "sessions":
        output([dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY created_at,id")])
    elif args.command == "send":
        prompt = args.text if args.text is not None else sys.stdin.read(16001)
        output(public_request(send(db,args.session,args.source,args.key,prompt,args.ttl,args.file)))
    elif args.command == "run-once":
        if args.timeout < 1 or args.timeout > 86400:
            raise BridgeError("Timeout must be 1 to 86400 seconds")
        # SQLite claim provides cross-process exclusivity. Any claimed request left
        # after a crash is marked unknown only when no active worker owns the lock.
        from .lock import worker_lock
        with worker_lock(Path(args.home)):
            recover(db)
            upcoming = db.execute("SELECT s.adapter FROM requests q JOIN sessions s ON s.id=q.session_id WHERE q.state='queued' AND q.expires_at>? AND NOT EXISTS (SELECT 1 FROM requests x WHERE x.session_id=q.session_id AND x.state IN ('running','unknown')) ORDER BY q.created_at,q.rowid LIMIT 1",(int(time.time()),)).fetchone()
            if upcoming and upcoming["adapter"] in ("claude","claude-patch"):
                if not args.live or args.budget_usd is None or not (0 < args.budget_usd <= 20):
                    raise BridgeError("Claude request remains queued: pass --live and --budget-usd in (0, 20]")
            row = claim(db)
            if not row:
                output({"processed":False})
                return
            try:
                session = check_session(db,row["session_id"])
                task = {**row,"_home":str(Path(args.home).expanduser().resolve())}
                state, reply, error, initialized = ADAPTERS[session["adapter"]](session,task,args.timeout,args.budget_usd)
                finish(db,row["id"],state,reply,error,launched=initialized)
            except BridgeError as exc:
                finish(db,row["id"],"blocked",error=str(exc))
            except Exception as exc:
                finish(db,row["id"],"unknown",error=f"Unhandled adapter error: {type(exc).__name__}")
            result = db.execute("SELECT * FROM requests WHERE id=?",(row["id"],)).fetchone()
            output({"processed":True,**public_request(result)})
    elif args.command in ("read","watch"):
        deadline = time.monotonic() + args.timeout if args.command == "watch" else 0
        while True:
            row = db.execute("SELECT * FROM requests WHERE id=?",(args.id,)).fetchone()
            if not row:
                raise BridgeError("Unknown request")
            if args.command == "read" or row["state"] in ("completed","blocked","failed","unknown","expired") or time.monotonic() >= deadline:
                result = public_request(row,include_text=args.include_prompt)
                applied = db.execute("SELECT digest,applied_at,source FROM patch_applied WHERE request_id=?",(args.id,)).fetchone()
                if applied:
                    result["patch_applied"] = dict(applied)
                output(result)
                return
            time.sleep(0.5)
    elif args.command == "status":
        output({"counts":{r["state"]:r["n"] for r in db.execute("SELECT state,count(*) n FROM requests GROUP BY state")},
                "sessions":db.execute("SELECT count(*) FROM sessions").fetchone()[0]})
    elif args.command == "audit":
        output([dict(r) for r in db.execute("SELECT * FROM audit ORDER BY rowid DESC LIMIT ?",(max(1,min(args.limit,500)),))])
    elif args.command == "patch-check":
        session, patch, digest, _expected = review(db,args.id)
        output({"request_id":args.id,"repo_id":session["repo_id"],"digest":digest,"bytes":len(patch.encode()),"state":"ready"})
    elif args.command == "patch-apply":
        from .lock import worker_lock
        with worker_lock(Path(args.home)):
            output(apply_reviewed(db,args.id,args.digest,args.source))
    elif args.command == "resolve":
        if len(args.reason) < 10 or len(args.reason) > 200:
            raise BridgeError("Resolution reason must be 10 to 200 characters")
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM requests WHERE id=?",(args.id,)).fetchone()
            if not row or row["state"] != "unknown":
                raise BridgeError("Only unknown requests can be resolved")
            db.execute("UPDATE requests SET state='resolved',error=? WHERE id=?",(args.reason,args.id))
            if args.session_exists == "yes":
                db.execute("UPDATE sessions SET last_completed=? WHERE id=?",(int(time.time()),row["session_id"]))
            else:
                db.execute("UPDATE sessions SET last_completed=NULL WHERE id=?",(row["session_id"],))
            from .core import audit
            audit(db,args.id,"resolved","operator inspection recorded")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        output({"id":args.id,"state":"resolved"})


def main():
    args = parser().parse_args()
    try:
        run(args)
    except (BridgeError, ValueError, OSError) as exc:
        print(json.dumps({"error":str(exc),"type":type(exc).__name__}),file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
