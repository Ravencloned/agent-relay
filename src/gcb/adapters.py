"""Agent transports. Claude output is untrusted data, never bridge control input."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .core import BridgeError, git_identity, check_patch_scope, connect, require_clean


MAX_STDOUT = 1_000_000
MAX_STDERR = 16_000
MAX_EVENTS = 10_000
MAX_LINE = 200_000


def mock_send(session, request, timeout, budget):
    return "completed", f"[MOCK] Received {len(request['prompt'])} characters for {session['name']}", "", True


def _kill_tree(proc):
    if os.name == "nt":
        # On Windows the job handle is closed by _capture before this wait.
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _capture(args, prompt, cwd, timeout):
    """Bound both pipes during the run, and stop the process tree on limits."""
    job = None
    if os.name == "nt":
        from .winjob import spawn
        proc, job = spawn(args,cwd)
    else:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd,start_new_session=True)
    out, err = bytearray(), bytearray()
    over = threading.Event()

    def drain(pipe, buf, limit):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    return
                if len(buf) + len(chunk) > limit:
                    over.set()
                    return
                buf.extend(chunk)
        finally:
            pipe.close()

    def write_prompt():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    readers = [threading.Thread(target=drain,args=(proc.stdout,out,MAX_STDOUT),daemon=True),
               threading.Thread(target=drain,args=(proc.stderr,err,MAX_STDERR),daemon=True)]
    for t in readers:
        t.start()
    writer = threading.Thread(target=write_prompt,daemon=True)
    writer.start()
    deadline = time.monotonic() + timeout
    reason = None
    while proc.poll() is None or any(t.is_alive() for t in readers):
        if over.is_set():
            reason = "output_limit"
            if job:
                job.close()
            _kill_tree(proc)
            break
        if time.monotonic() >= deadline:
            reason = "timeout"
            if job:
                job.close()
            _kill_tree(proc)
            break
        over.wait(0.001)
    if over.is_set() and reason is None:
        reason = "output_limit"
        if job:
            job.close()
        _kill_tree(proc)
    for t in readers:
        t.join(timeout=2)
    writer.join(timeout=2)
    if any(t.is_alive() for t in readers) or writer.is_alive():
        reason = reason or "pipe_stalled"
        if job:
            job.close()
        _kill_tree(proc)
    if job:
        job.close()
    return proc.returncode, bytes(out), bytes(err), reason


def _parse(data, session):
    sid = str(uuid.UUID(session["id"]))
    init = None
    result = None
    denied = False
    events = 0
    for line in data.splitlines():
        events += 1
        if events > MAX_EVENTS or len(line) > MAX_LINE:
            return None, None, False, "protocol_limit"
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, None, False, "invalid_json"
        if not isinstance(obj, dict):
            return None, None, False, "invalid_event"
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            if init is not None:
                return None, None, False, "duplicate_init"
            init = obj
        if obj.get("type") == "result":
            if init is None:
                return None, None, False, "result_before_init"
            if result is not None:
                return None, None, False, "duplicate_result"
            result = obj
        if obj.get("type") in ("assistant","user"):
            msg = obj.get("message") or {}
            if isinstance(msg, dict):
                for block in msg.get("content",[]):
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            denied = True
                        if block.get("type") == "tool_result" and block.get("is_error"):
                            denied = True
        if obj.get("type") == "system" and obj.get("subtype") in ("permission_request","permission_denied"):
            denied = True
    if not init:
        return None, None, denied, "missing_init"
    if init.get("session_id") != sid:
        return None, None, denied, "session_mismatch"
    if init.get("tools") != []:
        return init, None, denied, "tools_not_verified_disabled"
    try:
        init_cwd = str(Path(init["cwd"]).resolve(strict=True))
    except (KeyError, OSError, ValueError, TypeError):
        return init, None, denied, "cwd_missing"
    if os.path.normcase(init_cwd) != os.path.normcase(session["path"]):
        return init, None, denied, "cwd_mismatch"
    if not result:
        return init, None, denied, "missing_result"
    if result.get("session_id") != sid:
        return init, None, denied, "session_mismatch"
    return init, result, denied, None


def claude_send(session, request, timeout, budget):
    if not shutil.which("claude"):
        raise BridgeError("Claude Code CLI is unavailable")
    if budget is None or not (0 < budget <= 20):
        raise BridgeError("Live runs require a per-turn budget in (0, 20] USD")
    sid = str(uuid.UUID(session["id"]))
    try:
        top, gd = git_identity(session["path"])
    except (BridgeError, FileNotFoundError) as exc:
        raise BridgeError("Repository changed before Claude launch") from exc
    if os.path.normcase(top) != os.path.normcase(session["path"]) or os.path.normcase(gd) != os.path.normcase(session["git_dir"]):
        raise BridgeError("Repository identity changed before Claude launch")
    args = ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--safe-mode", "--strict-mcp-config", "--tools", "",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--max-budget-usd", str(budget)]
    if session["last_completed"] is None:
        args += ["--session-id", sid]
    else:
        args += ["--resume", sid]
    try:
        code, stdout, _stderr, stop_reason = _capture(args,request["prompt"],top,timeout)
    except OSError as exc:
        return "unknown", "", f"Claude process error: {type(exc).__name__}", False
    init, result, denied, parse_error = _parse(stdout,session)
    initialized = bool(init and init.get("session_id") == sid and parse_error not in ("cwd_missing","cwd_mismatch"))
    if stop_reason:
        return "unknown", "", f"Claude stopped: {stop_reason}; delivery may have occurred", initialized
    if parse_error:
        return "unknown", "", f"Claude protocol error: {parse_error}", initialized
    reply = result.get("result", "")
    if not isinstance(reply,str):
        return "unknown", "", "Claude final result is not text", initialized
    if code or result.get("is_error"):
        return "failed", reply, f"Claude returned error (exit {code})", initialized
    if denied:
        return "blocked", reply, "Claude reported a tool or permission error", initialized
    return "completed", reply, "", initialized


def claude_patch_send(session, request, timeout, budget):
    home = request.get("_home")
    if not home:
        raise BridgeError("Patch adapter requires a private queue home")
    db = connect(home)
    try:
        check_patch_scope(db, session)
        require_clean(session["path"])
    finally:
        db.close()
    return claude_send(session, request, timeout, budget)


ADAPTERS = {"mock": mock_send, "claude": claude_send, "claude-patch": claude_patch_send}
