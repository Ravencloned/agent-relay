"""Private stdin JSON interface used only by the channel subprocess."""
import argparse
import json
import sys

from .channel_queue import bind, emitted, next_request, reply
from .core import BridgeError, connect


def dispatch(db, command):
    if not isinstance(command,dict):
        raise BridgeError("Channel IPC input must be an object")
    op = command.get("op")
    sid = command.get("session_id")
    if not isinstance(sid,str):
        raise BridgeError("Channel IPC session is missing")
    if op == "bind":
        pid = command.get("parent_pid")
        if not isinstance(pid,int) or isinstance(pid,bool) or pid < 1:
            raise BridgeError("Invalid channel parent PID")
        return bind(db,sid,pid)
    nonce = command.get("nonce")
    if not isinstance(nonce,str):
        raise BridgeError("Channel binding nonce is missing")
    if op == "next":
        return next_request(db,sid,nonce)
    if op == "emitted":
        return emitted(db,sid,nonce,command.get("request_id"))
    if op == "reply":
        return reply(db,sid,nonce,command.get("request_id"),command.get("text"))
    raise BridgeError("Unsupported channel IPC operation")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home",required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(250_001)
        if len(raw) > 250_000:
            raise BridgeError("Channel IPC message is too large")
        command = json.loads(raw)
        db = connect(args.home)
        try:
            result = dispatch(db,command)
        finally:
            db.close()
        print(json.dumps({"ok":True,"result":result},ensure_ascii=False))
    except (BridgeError,ValueError,UnicodeError,OSError) as exc:
        print(json.dumps({"ok":False,"error":str(exc)}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
