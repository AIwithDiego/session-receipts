"""The live dashboard server (SR-10), on synthetic logs only."""

import contextlib
import http.client
import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from receipts import server

FIX = Path(__file__).parent / "fixtures"
FAKE_KEY = "sk-ant-" + "api03-" + "Q" * 40  # built at runtime so scanners don't flag the file


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logs, self.out = root / "logs", root / "out"
        shutil.copytree(FIX / "claude", self.logs / "claude")
        shutil.copytree(FIX / "codex", self.logs / "codex")
        self.store = server.Store(self.logs / "claude", self.logs / "codex", self.out)
        server.MIN_GAP = 0
        self.srv = server.make_server(self.store, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def get(self, path, method="GET", **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"localhost:{self.port}", **headers}
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def data(self):
        r, body = self.get("/api/data", **{"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status, 200)
        return json.loads(body)

    def test_page_has_no_data_inline(self):
        r, body = self.get("/", **{"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(r.status, 200)
        self.assertIn(b"window.LIVE=true", body)
        self.assertNotIn(b'src="data.js"', body)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_data_and_receipt(self):
        d = self.data()
        self.assertTrue(d["live"])
        self.assertEqual(len(d["sessions"]), 3)  # incl. the older-format Codex log, kept with a note
        sid = next(s["id"] for s in d["sessions"] if s["tool"] == "claude-code")
        r, body = self.get(f"/api/s/{sid}")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(json.loads(body)["id"], sid)
        self.assertEqual(self.get("/api/s/nope")[0].status, 404)
        self.assertEqual(self.get("/api/s/..%2F..%2Fetc")[0].status, 404)

    def test_refuses_other_sites_and_hosts(self):
        self.assertEqual(self.get("/api/data", Host="evil.example:4747")[0].status, 403)  # DNS rebinding
        self.assertEqual(self.get("/api/data", **{"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(self.get("/api/data", Origin="https://evil.example")[0].status, 403)
        self.assertEqual(self.get("/api/data", **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0].status, 403)
        # a link to the page itself from elsewhere is fine: the page holds no data
        self.assertEqual(self.get("/", **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0].status, 200)
        self.assertEqual(self.get("/api/data", method="POST")[0].status, 405)

    def test_only_changed_logs_are_read_again(self):
        self.data()
        reads = self.store.reads
        first = self.data()
        self.assertEqual(self.store.reads, reads)  # nothing changed, nothing read
        log = next((self.logs / "claude").glob("*/*.jsonl"))
        line = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        line.update(type="user", timestamp="2026-11-05T23:00:00Z", isMeta=False,
                    message={"role": "user", "content": "one more thing " + FAKE_KEY})
        with open(log, "a") as fh:
            fh.write(json.dumps(line) + "\n")
        d = self.data()
        self.assertEqual(self.store.reads, reads + 1)
        before = next(s for s in first["sessions"] if s["tool"] == "claude-code")
        after = next(s for s in d["sessions"] if s["id"] == before["id"])
        self.assertEqual(after["prompts"], before["prompts"] + 1)
        r, body = self.get(f"/api/s/{after['id']}")
        self.assertNotIn(FAKE_KEY[10:], body.decode())  # redacted

    def test_deleted_log_stays_from_archive(self):
        ids = {s["id"] for s in self.data()["sessions"]}
        shutil.rmtree(self.logs)
        d = self.data()
        self.assertEqual({s["id"] for s in d["sessions"]}, ids)
        self.assertTrue(all(s.get("archived") for s in d["sessions"]))


class InstallOffMac(unittest.TestCase):
    def test_refuses_before_writing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, plist = Path(tmp) / "out", Path(tmp) / "agents" / "x.plist"
            with mock.patch("sys.platform", "win32"), mock.patch.object(server, "PLIST", plist), \
                    mock.patch.object(server.dashboard, "OUT", out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(server.install(), 1)
                self.assertEqual(server.uninstall(), 1)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
