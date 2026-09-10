"""Remote bridge uses isolated records; no real account or remote POST."""
import hashlib
import json
import sqlite3
import time
import unittest
import uuid

import test_web
from cptrainer import remote
from cptrainer.db import Database
from cptrainer.judge import Judge


class RemoteTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp
    tearDown = test_web.WebTests.tearDown
    request = test_web.WebTests.request

    def submission(self, **extra):
        return {"platform": "cf", "problem_id": "2000A", "source": "print('中文 <>&')\r\n", "language": "31", "language_name": "Python 3", "request_key": str(uuid.uuid4())} | extra

    def test_bridge_claim_identity_idempotency_and_scoring(self):
        status, session = self.request("/api/sessions", {"name": "Remote", "platform": "cf", "problems": ["2000A"], "duration": 5})
        self.assertEqual(status, 200)
        sid = session["id"]
        self.request(f"/api/sessions/{sid}/start", {})
        data = self.submission(session_id=sid)
        status, run = self.request("/api/remote/runs", data)
        self.assertEqual(status, 200, run)
        self.assertEqual(self.request("/api/remote/runs", data)[1]["id"], run["id"])
        self.assertEqual(self.request("/api/remote/runs", data | {"source": "print(2)"})[0], 400)
        self.assertEqual(self.request("/api/remote/runs", self.submission())[0], 400)
        self.assertNotIn("request_key", self.request(f"/api/judge/runs/{run['id']}")[1])
        self.assertNotIn(data["request_key"], json.dumps(self.request("/api/judge/runs")[1]))
        path = f"/api/remote/runs/{run['id']}/event"
        headers = {"Origin": "https://codeforces.com", "X-CP-Trainer": "", "X-CP-Remote": data["request_key"]}
        self.assertEqual(self.request(path, {"phase": "claim"}, headers | {"X-CP-Remote": "wrong"})[0], 403)
        self.assertEqual(self.request("/api/sessions/clear", {}, headers)[0], 403)
        self.assertEqual(self.request(path, {"phase": "claim"}, headers | {"Host": "evil.example"})[0], 403)
        self.assertTrue(self.request(path, {"phase": "claim"}, headers)[1]["allowed"])
        self.assertFalse(self.request(path, {"phase": "claim"}, headers)[1]["allowed"])
        result = {"phase": "result", "verdict": "WA", "url": "https://codeforces.com/contest/2000/submission/1234", "source_hash": hashlib.sha256(data["source"].replace("\r\n", "\n").strip().encode()).hexdigest()}
        for invalid in [result | {"url": "https://evil.example/1234"}, result | {"url": "https://codeforces.com/contest/2001/submission/1234"}, result | {"source_hash": "wrong"}]:
            self.assertEqual(self.request(path, invalid, headers)[0], 400)
        self.assertEqual(self.request(path, result, headers)[0], 200)
        self.assertFalse(self.request(path, result, headers)[1]["allowed"])
        self.assertEqual(self.request(f"/api/sessions/{sid}")[1]["items"][0]["wrong_attempts"], 1)
        second = self.submission(session_id=sid)
        rid = self.request("/api/remote/runs", second)[1]["id"]
        second_path = f"/api/remote/runs/{rid}/event"
        self.assertEqual(self.request(second_path, {"phase": "claim"}, headers)[0], 403)
        headers["X-CP-Remote"] = second["request_key"]
        self.request(second_path, {"phase": "claim"}, headers)
        self.assertEqual(self.request(second_path, result | {"verdict": "AC"}, headers)[0], 200)
        self.assertEqual(self.request(f"/api/sessions/{sid}")[1]["items"][0]["solved"], 1)
        self.assertEqual(len(self.request("/api/judge/runs?platform=cf&problem_id=2000A")[1]["items"]), 2)

    def test_cancel_timeout_restart_and_validation(self):
        for extra in [{"platform": "other"}, {"problem_id": "absent"}, {"language": "cpp17"}, {"request_key": "weak"}, {"source": " "}, {"source": "a" * 131073}, {"session_id": True}]:
            self.assertIn(self.request("/api/remote/runs", self.submission(**extra))[0], (400, 404))
        data = self.submission(platform="atcoder", problem_id="abc350_a")
        _, run = self.request("/api/remote/runs", data)
        self.assertIn("taskScreenName=abc350_a", run["url"])
        Judge(self.paths)
        self.assertEqual(self.request(f"/api/judge/runs/{run['id']}")[1]["verdict"], "QUEUED")
        self.request(f"/api/judge/runs/{run['id']}/cancel", {})
        self.assertEqual(self.request(f"/api/judge/runs/{run['id']}")[1]["verdict"], "CANCELLED")
        _, run = self.request("/api/remote/runs", self.submission())
        with self.server.database() as db, db.conn:
            db.conn.execute("UPDATE judge_runs SET submitted_at=? WHERE id=?", (int(time.time()) - 901, run["id"]))
            remote.expire_runs(db)
        self.assertEqual(self.request(f"/api/judge/runs/{run['id']}")[1]["verdict"], "UNKNOWN")

    def test_v2_upgrade_has_restorable_backup(self):
        legacy = self.paths.home / "v2.sqlite3"
        db = Database(legacy); db.migrate()
        with db.conn:
            db.conn.execute("DROP INDEX idx_judge_request_key")
            db.conn.execute("ALTER TABLE judge_runs DROP COLUMN request_key")
            db.conn.execute("ALTER TABLE judge_runs DROP COLUMN engine")
            db.conn.execute("PRAGMA user_version=2")
        db.migrate()
        self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        db.close()
        backup = next(legacy.parent.glob("backups/*before-v3*.sqlite3"))
        with sqlite3.connect(backup) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(old.execute("PRAGMA integrity_check").fetchone()[0], "ok")
