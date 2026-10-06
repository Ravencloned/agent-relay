"""Read-only discovery of Claude CLI sessions; discovery never grants control."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from .core import BridgeError, canonical

MAX_DISCOVERY_BYTES = 1_000_000
MAX_TARGETS = 256


def discover():
    executable = shutil.which("claude")
    if not executable:
        raise BridgeError("Claude Code CLI is unavailable")
    try:
        result = subprocess.run([str(Path(executable).resolve()), "agents", "--json"],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("Claude session discovery failed") from exc
    if result.returncode:
        raise BridgeError("Claude session discovery returned an error")
    if len(result.stdout) > MAX_DISCOVERY_BYTES:
        raise BridgeError("Claude session list exceeds the size limit")
    try:
        entries = json.loads(result.stdout)
    except (UnicodeError, ValueError) as exc:
        raise BridgeError("Claude session list is invalid JSON") from exc
    if not isinstance(entries, list) or len(entries) > MAX_TARGETS:
        raise BridgeError("Claude session list has invalid shape or size")
    seen = set()
    targets = []
    for item in entries:
        if not isinstance(item, dict):
            raise BridgeError("Claude session entry is invalid")
        try:
            sid = str(uuid.UUID(item["sessionId"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise BridgeError("Claude session entry has an invalid ID") from exc
        if sid in seen:
            raise BridgeError("Claude session list contains duplicate IDs")
        seen.add(sid)
        kind = item.get("kind")
        status = item.get("status")
        pid = item.get("pid")
        cwd = item.get("cwd")
        if kind not in ("interactive", "background") or not isinstance(status, str) or not isinstance(pid, int) or pid < 1 or not isinstance(cwd, str) or not cwd:
            raise BridgeError("Claude session entry has invalid metadata")
        targets.append({"session_id":sid,"kind":kind,"status":status,"pid":pid,
                        "cwd":cwd,"name":item.get("name") if isinstance(item.get("name"),str) else None})
    return targets


def public_target(target, include_path=False):
    route = ("unsupported_active_interactive" if target["kind"] == "interactive"
             else "background_not_enrolled")
    result = {"session_id":target["session_id"],"kind":target["kind"],
              "status":target["status"],"pid":target["pid"],
              "route":route,"delivery_supported":False,
              "cwd_sha256":hashlib.sha256(os.path.normcase(target["cwd"]).encode()).hexdigest()}
    if include_path:
        result["cwd"] = target["cwd"]
    return result


def check_target(session_id, repo_path):
    try:
        sid = str(uuid.UUID(session_id))
    except (ValueError, TypeError) as exc:
        raise BridgeError("Target session ID must be a UUID") from exc
    expected = canonical(repo_path)
    matches = [item for item in discover() if item["session_id"] == sid]
    if not matches:
        raise BridgeError("Target session is not in Claude's active session list")
    target = matches[0]
    try:
        cwd = canonical(target["cwd"])
    except (OSError, ValueError) as exc:
        raise BridgeError("Target working directory is missing") from exc
    if os.path.normcase(cwd) != os.path.normcase(expected):
        raise BridgeError("Target session belongs to another repository path")
    return {**public_target(target),"repo_match":True}
