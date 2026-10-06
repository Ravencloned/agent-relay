import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1] / "src"))
from gcb.cli import parser, run
from gcb.core import BridgeError
from gcb.targets import check_target, discover, public_target


SID = "550e8400-e29b-41d4-a716-446655440000"


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = {"pid":12345,"cwd":str(self.root),"kind":"interactive",
                        "startedAt":1,"sessionId":SID,"name":"private-project","status":"busy"}

    def mocked_cli(self, entries):
        return patch("gcb.targets.shutil.which",return_value=sys.executable), patch(
            "gcb.targets.subprocess.run",return_value=subprocess.CompletedProcess([],0,json.dumps(entries).encode(),b""))

    def test_discovery_is_read_only_and_redacts_path_by_default(self):
        which, proc = self.mocked_cli([self.fixture])
        with which, proc as call:
            buf = io.StringIO()
            with redirect_stdout(buf):
                run(parser().parse_args(["--home",str(self.root / "absent-queue"),"targets"]))
        output = json.loads(buf.getvalue())[0]
        self.assertEqual(output["session_id"],SID)
        self.assertFalse(output["delivery_supported"])
        self.assertEqual(output["route"],"unsupported_active_interactive")
        self.assertNotIn("cwd",output)
        self.assertNotIn("private-project",buf.getvalue())
        self.assertFalse((self.root / "absent-queue").exists())
        self.assertEqual(call.call_args.args[0][-2:],["agents","--json"])

    def test_exact_repo_check_and_wrong_repo(self):
        which, proc = self.mocked_cli([self.fixture])
        with which, proc:
            self.assertTrue(check_target(SID,str(self.root))["repo_match"])
            other = self.root / "other"
            other.mkdir()
            with self.assertRaises(BridgeError):
                check_target(SID,str(other))
            with self.assertRaises(BridgeError):
                check_target("00000000-0000-0000-0000-000000000001",str(self.root))

    def test_duplicate_or_malformed_sessions_fail_closed(self):
        for entries in ([self.fixture,self.fixture], [{**self.fixture,"sessionId":"wrong"}],
                        [{**self.fixture,"pid":0}], {"not":"a-list"}):
            which, proc = self.mocked_cli(entries)
            with which, proc, self.assertRaises(BridgeError):
                discover()

    def test_background_discovery_is_not_false_delivery(self):
        target = {"session_id":SID,"kind":"background","status":"idle","pid":1,
                  "cwd":str(self.root),"name":None}
        self.assertEqual(public_target(target)["route"],"background_not_enrolled")
        self.assertFalse(public_target(target)["delivery_supported"])


if __name__ == "__main__":
    unittest.main()
