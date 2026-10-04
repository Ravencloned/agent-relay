import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1] / "src"))
from gcb.core import BridgeError, add_repo, add_session, claim, connect, finish, send
from gcb.patches import apply_reviewed, context_prompt, review
from gcb import adapters


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        base = self.root / "base"
        base.mkdir()
        self.repo = self.root / "repo"
        subprocess.run(["git","init","-q","-b","main",str(base)],check=True)
        subprocess.run(["git","-C",str(base),"config","user.name","Test"],check=True)
        subprocess.run(["git","-C",str(base),"config","user.email","test@example.invalid"],check=True)
        subprocess.run(["git","-C",str(base),"config","core.autocrlf","false"],check=True)
        (base / "hello.txt").write_bytes(b"hello\n")
        (base / "crlf.txt").write_bytes(b"hello\r\n")
        (base / "gcb").mkdir()
        (base / "gcb" / "hook.py").write_text("raise RuntimeError('repo-local policy shadow')\n")
        subprocess.run(["git","-C",str(base),"add","hello.txt","crlf.txt","gcb/hook.py"],check=True)
        subprocess.run(["git","-C",str(base),"commit","-qm","fixture"],check=True)
        subprocess.run(["git","-C",str(base),"worktree","add","-qb","patch-test",str(self.repo)],check=True)
        private = patch("gcb.core._private_home")
        private.start()
        self.addCleanup(private.stop)
        self.db = connect(self.root / "queue")
        self.addCleanup(self.db.close)
        rid = add_repo(self.db,self.repo)["id"]
        self.session = add_session(self.db,rid,"patch","claude-patch")

    def complete(self, reply):
        req = send(self.db,self.session["id"],"local",str(self.db.execute("SELECT count(*) FROM requests").fetchone()[0]),"Change hello to world",files=["hello.txt"])
        claim(self.db)
        finish(self.db,req["id"],"completed",reply,launched=True)
        return req["id"]

    def test_review_and_apply_exact_digest_once(self):
        diff = "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+world\n"
        rid = self.complete(diff)
        _session, patch_text, digest, _expected = review(self.db,rid)
        self.assertEqual(patch_text,diff)
        with self.assertRaises(BridgeError):
            apply_reviewed(self.db,rid,"wrong","local")
        self.assertEqual((self.repo / "hello.txt").read_text(),"hello\n")
        self.assertEqual(apply_reviewed(self.db,rid,digest,"local")["state"],"applied")
        self.assertEqual((self.repo / "hello.txt").read_text(),"world\n")
        self.assertEqual(self.db.execute("SELECT digest FROM patch_applied WHERE request_id=?",(rid,)).fetchone()[0],digest)
        with self.assertRaises(BridgeError):
            apply_reviewed(self.db,rid,digest,"local")
        duplicate = send(self.db,self.session["id"],"local","0","Change hello to world",files=["hello.txt"])
        self.assertEqual(duplicate["id"],rid)

    def test_patch_transport_has_no_claude_tools(self):
        req = send(self.db,self.session["id"],"local","transport","Change hello to world",files=["hello.txt"])
        claim(self.db)
        session = dict(self.db.execute("SELECT s.*,r.path,r.git_dir FROM sessions s JOIN repos r ON r.id=s.repo_id WHERE s.id=?",(self.session["id"],)).fetchone())
        init = {"type":"system","subtype":"init","session_id":session["id"],"cwd":session["path"],"tools":[]}
        result = {"type":"result","session_id":session["id"],"result":"diff --git a/hello.txt b/hello.txt\n"}
        stream = "\n".join(map(json.dumps,[init,result])).encode()
        with patch("gcb.adapters.shutil.which",return_value="claude"), patch("gcb.adapters._capture",return_value=(0,stream,b"",None)) as capture:
            self.assertEqual(adapters.claude_patch_send(session,{**req,"_home":str(self.root / "queue")},30,0.05)[0],"completed")
        argv = capture.call_args.args[0]
        self.assertIn("--safe-mode",argv)
        self.assertEqual(argv[argv.index("--tools")+1],"")
        self.assertNotIn("--settings",argv)
        self.assertNotIn("gcb.hook",str(argv))

    def test_reject_extra_path_and_commands(self):
        for diff in (
            "diff --git a/../outside b/../outside\n--- a/../outside\n+++ b/../outside\n@@ -1 +1 @@\n-a\n+b\n",
            "diff --git a/other.txt b/other.txt\n--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-a\n+b\n",
            "diff --git a/hello.txt b/hello.txt\nold mode 100644\nnew mode 100755\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+world\n",
            "Run this shell command: rm -rf foo",
        ):
            rid = self.complete(diff)
            with self.assertRaises(BridgeError):
                review(self.db,rid)

    def test_git_whitespace_fix_cannot_change_reviewed_bytes(self):
        subprocess.run(["git","-C",str(self.repo),"config","apply.whitespace","fix"],check=True)
        diff = "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+world   \n"
        rid = self.complete(diff)
        _session, _patch, digest, expected = review(self.db,rid)
        self.assertEqual(expected["hello.txt"],b"world   \n")
        apply_reviewed(self.db,rid,digest,"local")
        self.assertEqual((self.repo / "hello.txt").read_bytes(),b"world   \n")

    def test_crlf_patch_is_preserved_or_rejected(self):
        diff = "diff --git a/crlf.txt b/crlf.txt\n--- a/crlf.txt\n+++ b/crlf.txt\n@@ -1 +1 @@\n-hello\r\n+world\r\n"
        req = send(self.db,self.session["id"],"local","crlf","Change hello to world",files=["crlf.txt"])
        claim(self.db)
        finish(self.db,req["id"],"completed",diff,launched=True)
        try:
            _session, _patch, digest, expected = review(self.db,req["id"])
        except BridgeError:
            self.assertEqual((self.repo / "crlf.txt").read_bytes(),b"hello\r\n")
            return
        apply_reviewed(self.db,req["id"],digest,"local")
        self.assertEqual((self.repo / "crlf.txt").read_bytes(),expected["crlf.txt"])

    def test_executable_git_config_never_runs_before_approval(self):
        marker = self.root / "helper-marker"
        if os.name == "nt":
            helper = f'cmd /c echo invoked>"{marker}"'
        else:
            helper = f'sh -c "echo invoked > {marker}"'
        subprocess.run(["git","-C",str(self.repo),"config","core.fsmonitor",helper],check=True)
        rid = self.complete("not a patch")
        with self.assertRaises(BridgeError):
            review(self.db,rid)
        self.assertFalse(marker.exists())
        subprocess.run(["git","-C",str(self.repo),"config","filter.marker.clean",helper],check=True)
        with self.assertRaises(BridgeError):
            send(self.db,self.session["id"],"local","filter","Do something",files=["hello.txt"])
        self.assertFalse(marker.exists())

    def test_crash_after_apply_never_replays_patch(self):
        diff = "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+world\n"
        rid = self.complete(diff)
        _session, _patch, digest, _expected = review(self.db,rid)
        with patch("gcb.patches.time.time",side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                apply_reviewed(self.db,rid,digest,"local")
        self.assertEqual((self.repo / "hello.txt").read_bytes(),b"world\n")
        with self.assertRaises(BridgeError):
            apply_reviewed(self.db,rid,digest,"local")

    def test_stale_branch_and_dirty_worktree(self):
        diff = "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+world\n"
        rid = self.complete(diff)
        (self.repo / "hello.txt").write_text("other\n")
        with self.assertRaises(BridgeError):
            review(self.db,rid)
        (self.repo / "hello.txt").write_text("hello\n")
        subprocess.run(["git","-C",str(self.repo),"checkout","-qb","other-branch"],check=True)
        with self.assertRaises(BridgeError):
            review(self.db,rid)

    def test_context_rejects_secret_symlink_hardlink(self):
        (self.repo / ".gitattributes").write_bytes(b"hello.txt text\n")
        with self.assertRaises(BridgeError):
            context_prompt(str(self.repo),"read it",["hello.txt"])
        (self.repo / ".gitattributes").unlink()
        (self.repo / ".env").write_text("secret")
        with self.assertRaises(BridgeError):
            context_prompt(str(self.repo),"read it",[".env"])
        (self.repo / ".env").unlink()
        external = self.root / "external"
        external.write_text("outside")
        if os.name != "nt":
            (self.repo / "link").symlink_to(external)
            with self.assertRaises(BridgeError):
                context_prompt(str(self.repo),"read it",["link"])
        os.link(self.repo / "hello.txt",self.root / "hardlinked-outside")
        with self.assertRaises(BridgeError):
            context_prompt(str(self.repo),"read it",["hello.txt"])


if __name__ == "__main__":
    unittest.main()
