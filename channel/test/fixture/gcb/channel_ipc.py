import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home",required=True)
    args = parser.parse_args()
    home = Path(args.home)
    state_file = home / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"sent":False,"reply":None}
    command = json.load(sys.stdin)
    op = command["op"]
    if op == "bind":
        result = {"nonce":"fixture-nonce"}
    elif op == "next":
        if state["sent"]:
            result = None
        else:
            state["sent"] = True
            result = {"id":"550e8400-e29b-41d4-a716-446655440001","prompt":"fixture message",
                      "source":"fixture-user","session_id":command["session_id"]}
    elif op == "emitted":
        state["emitted"] = command["request_id"]
        result = {"state":"emitted"}
    elif op == "reply":
        if command["request_id"] != "550e8400-e29b-41d4-a716-446655440001":
            print(json.dumps({"ok":False,"error":"request ID mismatch"}))
            raise SystemExit(2)
        state["reply"] = command["text"]
        state["reply_id"] = command["request_id"]
        result = {"state":"completed"}
    else:
        raise ValueError("unknown operation")
    state_file.write_text(json.dumps(state))
    print(json.dumps({"ok":True,"result":result}))


if __name__ == "__main__":
    main()
