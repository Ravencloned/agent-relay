import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1] / "src"))
from gcb import channel_queue
from gcb.channel_ipc import dispatch
from gcb.core import BridgeError, add_repo, connect


SID = "550e8400-e29b-41d4-a716-446655440000"


class ChannelQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "fixture"
        self.repo.mkdir()
        subprocess.run(["git","init","-q",str(self.repo)],check=True)
        private = patch("gcb.core._private_home")
        private.start()
        self.addCleanup(private.stop)
        self.db = connect(self.root / "queue")
        self.addCleanup(self.db.close)
        self.rid = add_repo(self.db,self.repo)["id"]
        self.target = {"session_id":SID,"pid":12345,"kind":"interactive","status":"idle","repo_match":True}
        mocked = patch("gcb.channel_queue.check_target",return_value=self.target)
        mocked.start()
        self.addCleanup(mocked.stop)
        channel_queue.register(self.db,SID,self.rid)

    def bind(self):
        return channel_queue.bind(self.db,SID,12345)["nonce"]

    def test_no_delivery_until_bound_and_exact_reply(self):
        with self.assertRaises(BridgeError):
            channel_queue.send(self.db,SID,"user","one","hello")
        nonce = self.bind()
        queued = channel_queue.send(self.db,SID,"user","one","hello")
        self.assertEqual(queued["state"],"queued")
        self.assertEqual(channel_queue.send(self.db,SID,"user","one","hello")["id"],queued["id"])
        with self.assertRaises(BridgeError):
            channel_queue.send(self.db,SID,"user","one","changed")
        item = channel_queue.next_request(self.db,SID,nonce)
        self.assertEqual(item["id"],queued["id"])
        self.assertIsNone(channel_queue.next_request(self.db,SID,nonce))
        self.assertEqual(channel_queue.emitted(self.db,SID,nonce,item["id"])["state"],"emitted")
        self.assertEqual(channel_queue.reply(self.db,SID,nonce,item["id"],"answer")["state"],"completed")
        self.assertTrue(channel_queue.reply(self.db,SID,nonce,item["id"],"answer")["duplicate"])
        with self.assertRaises(BridgeError):
            channel_queue.reply(self.db,SID,nonce,item["id"],"different")
        self.assertEqual(self.db.execute("SELECT reply FROM channel_requests WHERE id=?",(item["id"],)).fetchone()[0],"answer")

    def test_reply_can_race_transport_ack(self):
        nonce = self.bind()
        queued = channel_queue.send(self.db,SID,"user","race","hello")
        channel_queue.next_request(self.db,SID,nonce)
        self.assertEqual(channel_queue.reply(self.db,SID,nonce,queued["id"],"fast")["state"],"completed")
        self.assertEqual(channel_queue.emitted(self.db,SID,nonce,queued["id"])["state"],"completed")

    def test_restart_marks_ambiguous_unknown_and_never_replays(self):
        nonce = self.bind()
        first = channel_queue.send(self.db,SID,"user","first","hello")
        second = channel_queue.send(self.db,SID,"user","second","next")
        channel_queue.next_request(self.db,SID,nonce)
        new_nonce = self.bind()
        self.assertNotEqual(nonce,new_nonce)
        self.assertEqual(self.db.execute("SELECT state FROM channel_requests WHERE id=?",(first["id"],)).fetchone()[0],"unknown")
        with self.assertRaises(BridgeError):
            channel_queue.next_request(self.db,SID,nonce)
        self.assertIsNone(channel_queue.next_request(self.db,SID,new_nonce))
        channel_queue.resolve(self.db,first["id"],"Inspected terminal after restart")
        self.assertEqual(channel_queue.next_request(self.db,SID,new_nonce)["id"],second["id"])

    def test_wrong_parent_pid_or_stale_process_rejected(self):
        with self.assertRaises(BridgeError):
            channel_queue.bind(self.db,SID,99999)
        nonce = self.bind()
        self.target["pid"] = 45678
        with self.assertRaises(BridgeError):
            channel_queue.send(self.db,SID,"user","stale","hello")
        with self.assertRaises(BridgeError):
            channel_queue.next_request(self.db,SID,"wrong-nonce")
        self.assertIsNotNone(nonce)

    def test_expiry_and_redaction(self):
        nonce = self.bind()
        item = channel_queue.send(self.db,SID,"user","old","hello",ttl=1)
        self.db.execute("UPDATE channel_requests SET expires_at=0 WHERE id=?",(item["id"],))
        self.assertIsNone(channel_queue.next_request(self.db,SID,nonce))
        self.assertEqual(self.db.execute("SELECT state FROM channel_requests WHERE id=?",(item["id"],)).fetchone()[0],"expired")
        item = channel_queue.send(self.db,SID,"user","secret","hello")
        channel_queue.next_request(self.db,SID,nonce)
        channel_queue.reply(self.db,SID,nonce,item["id"],"token=TOPSECRET")
        row = self.db.execute("SELECT * FROM channel_requests WHERE id=?",(item["id"],)).fetchone()
        self.assertNotIn("TOPSECRET",json.dumps(channel_queue.public_request(row)))

    def test_ipc_rejects_spoofed_reply_and_binds(self):
        bound = dispatch(self.db,{"op":"bind","session_id":SID,"parent_pid":12345})
        with self.assertRaises(BridgeError):
            dispatch(self.db,{"op":"reply","session_id":SID,"nonce":bound["nonce"],"request_id":"wrong","text":"fake"})
        with self.assertRaises(BridgeError):
            dispatch(self.db,{"op":"bind","session_id":SID,"parent_pid":True})


if __name__ == "__main__":
    unittest.main()
