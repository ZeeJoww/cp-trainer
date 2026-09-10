"""Synthetic, isolated checks for the archive and Web contest lifecycle."""
from pathlib import Path
from contextlib import closing
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from cptrainer.archive import Downloader, Fragments, Sanitize, archive_lock, archive_statements
from cptrainer.config import Config, Paths
from cptrainer.db import Database, SCHEMA
from cptrainer.planner import finish_session
from cptrainer.render import export_session_html
from cptrainer.sources import canonical_atcoder, dated_problems, parse_atcoder_problems
from cptrainer.types import Problem
from cptrainer.web import TrainerServer


def seed(paths):
    paths.ensure()
    Config(exclude_recent_days=0).save(paths.config)
    db = Database(paths.database)
    db.migrate()
    db.initialize_profiles(cf_handle="", atcoder_handle="", cf_rating=1800, atcoder_rating=1600)
    for platform in ["cf", "atcoder"]:
        for i in range(8):
            pid = f"{2000 + i}A" if platform == "cf" else f"abc{350 + i}_a"
            problem = Problem(platform, pid, str(2000 + i) if platform == "cf" else f"abc{350 + i}", "A", f"Synthetic check {platform} {i}", 1300 + i * 100, 100, ("dp", "graphs"), f"https://codeforces.com/problemset/problem/{2000 + i}/A" if platform == "cf" else f"https://atcoder.jp/contests/abc{350 + i}/tasks/{pid}", 100, 1720000000, True)
            db.upsert_problems([problem])
            with db.conn:
                db.conn.execute("INSERT INTO statements(platform,problem_id,html,status) VALUES(?,?,?,'ready')", (platform, pid, '<h3>Problem Statement</h3><p>Compute <var>n^2</var> and $$$x_i$$$.</p><pre>2\n4</pre>'))
    db.close()


def serve_fixture(directory):
    paths = Paths(Path(directory))
    if os.environ.get("CP_TRAINER_JUDGE_TEST_IMAGE"):
        from test_judge import seed_judge
        seed_judge(paths)
        (paths.home / "judge-runtime.json").write_text(json.dumps({"image": os.environ["CP_TRAINER_JUDGE_TEST_IMAGE"], "versions": "Isolated test runtime"}))
    else:
        seed(paths)
    server = TrainerServer(("127.0.0.1", 0), paths)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = Paths(Path(self.temp.name))
        seed(self.paths)
        self.server = TrainerServer(("127.0.0.1", 0), self.paths)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        h = {"Content-Type": "application/json", "X-CP-Trainer": self.server.token}
        h.update(headers or {})
        request = Request(self.url + path, data=None if data is None else json.dumps(data).encode(), headers=h)
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def create(self, **extra):
        status, result = self.request("/api/sessions", {"name": "Check", "platform": "cf", "count": 3, "duration": 5} | extra)
        self.assertEqual(status, 200, result)
        return result["id"]

    def test_lifecycle_tags_schedule_and_backup_restore(self):
        self.assertEqual(self.request("/api/state")[1]["sessions"], [])
        sid = self.create(scheduled_at=int(time.time()) + 100)
        detail = f"/api/sessions/{sid}"
        self.assertEqual(self.request(detail + "/tags")[0], 403)
        self.assertNotIn("tags", json.dumps(self.request(detail)[1]))
        self.assertEqual(self.request(detail + "/schedule", {"scheduled_at": None})[0], 200)
        self.assertEqual(self.request(detail + "/start", {})[0], 200)
        started = self.request(detail)[1]["started_at"]
        self.request(detail + "/start", {})
        self.assertEqual(self.request(detail)[1]["started_at"], started)
        self.assertEqual(self.request(detail + "/schedule", {"scheduled_at": None})[0], 400)
        self.assertEqual(self.request(detail + "/tags")[0], 403)
        with self.server.database() as db, db.conn:
            db.conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (int(time.time()) - 301, sid))
        self.assertEqual(self.request(detail + "/tags")[0], 200)
        self.assertEqual(self.request(detail + "/record", {"ordinal": 1, "solved": True, "elapsed_seconds": 900, "wrong_attempts": 0})[0], 400)
        self.assertEqual(self.request(detail + "/record", {"ordinal": 1, "solved": True, "elapsed_seconds": 120, "wrong_attempts": 1})[0], 200)
        self.assertEqual(self.request(detail + "/finish", {})[0], 200)
        self.assertEqual(self.request(detail + "/finish", {})[0], 400)
        self.assertNotIn("tags", json.dumps(self.request(detail)[1]))
        self.assertEqual(len(self.request(detail + "/tags")[1]), 3)
        with self.server.database() as db:
            self.assertEqual(len(db.rating_history("cf")), 1)
        backup = self.request("/api/sessions/clear", {})[1]["backup"]
        self.assertEqual(self.request("/api/state")[1]["sessions"], [])
        with closing(sqlite3.connect(self.paths.home / "backups" / backup)) as source, closing(sqlite3.connect(self.paths.home / "restored.sqlite3")) as restored:
            source.backup(restored)
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM session_problems").fetchone()[0], 3)

    def test_boundary_checks_and_real_offline_selection(self):
        self.assertEqual(self.request("/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/sessions/clear", {}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/sessions/clear", {}, {"X-CP-Trainer": "wrong"})[0], 403)
        for count in [0, -1, 99, True, 2.5]:
            self.assertEqual(self.request("/api/sessions", {"name": "x", "platform": "cf", "count": count, "duration": 5})[0], 400)
        with self.server.database() as db, db.conn:
            db.conn.execute("UPDATE statements SET status='partial' WHERE platform='cf'")
            db.conn.execute("UPDATE problems SET is_real=0 WHERE platform='atcoder'")
        self.assertEqual(self.request("/api/problems?platform=atcoder")[1]["total"], 0)
        self.assertEqual(self.request("/api/sessions", {"name": "x", "platform": "cf", "count": 1, "duration": 5})[0], 400)
        self.assertEqual(self.request("/api/sessions", {"name": "x", "platform": "cf", "problems": ["2000A"], "duration": 5})[0], 400)

    def test_schedule_survives_restart_and_export_hides_tags(self):
        sid = self.create()
        with self.server.database() as db:
            db.schedule_session(sid, int(time.time()) - 20)
        self.server.last_tick = 0
        self.server.service_actions()
        with self.server.database() as db:
            self.assertEqual(db.get_session(sid)["status"], "running")
            destination = self.paths.exports / "test.html"
            export_session_html(db, sid, destination, timezone="UTC", reveal=True)
            html = destination.read_text()
            self.assertNotIn("graphs", html)
            self.assertNotIn('<button id="show-tags">', html)
            finish_session(db, sid)
            export_session_html(db, sid, destination, timezone="UTC", reveal=True)
            html = destination.read_text()
            self.assertIn('<button id="show-tags">显示标签</button>', html)
            self.assertIn("class='tags' hidden", html)

    def test_atomic_finish_rolls_back_on_failure(self):
        sid = self.create()
        with self.server.database() as db:
            before = db.get_profile("cf")["local_rating"]
            db.conn.execute("CREATE TRIGGER break_history BEFORE INSERT ON rating_history BEGIN SELECT RAISE(ABORT,'test'); END")
            db.conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                finish_session(db, sid)
            self.assertEqual(db.get_profile("cf")["local_rating"], before)
            self.assertEqual(db.get_session(sid)["status"], "scheduled")


class ArchiveTests(unittest.TestCase):
    def test_embedded_images_svg_and_sync_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory)); paths.ensure()
            sanitizer = Sanitize("https://atcoder.jp/", Downloader(paths))
            sanitizer.feed('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" onload="bad()"/></svg><img src="data:image/png;base64,aGVs\nbG8=">')
            self.assertEqual(sanitizer.errors, [])
            self.assertEqual("".join(sanitizer.parts).count('/assets/'), 2)
            self.assertEqual(len(list((paths.home / "assets").iterdir())), 2)
            self.assertNotIn("onload", next((paths.home / "assets").glob("*.svg")).read_text())
            with archive_lock(paths, "cf"):
                with self.assertRaises(ValueError):
                    with archive_lock(paths, "cf"):
                        self.fail("parallel sync must be rejected")
            with archive_lock(paths, "cf"):
                pass

    def test_sanitization_and_unclosed_list_items(self):
        raw = '<div id="task-statement"><ul><li>one<li>two</ul><p>ok</p></div><div id="task-statement">next</div>'
        parser = Fragments("id", "task-statement")
        parser.feed(raw)
        self.assertEqual(len(parser.blocks), 2)
        sanitizer = Sanitize("https://atcoder.jp/")
        sanitizer.feed('<script>alert(1)</script><p onclick="bad()" style="color:red;background:url(https://x)">Safe <var>x^2</var><a href="javascript:bad()">link</a></p>')
        clean = "".join(sanitizer.parts)
        for bad in ["alert", "onclick", "javascript:", "background"]:
            self.assertNotIn(bad, clean)
        self.assertIn("<var>x^2</var>", clean)

    def test_canonical_dates_and_difficulty(self):
        contests = [{"id": "abc350", "start_epoch_second": 1710000000}, {"id": "adt", "start_epoch_second": 1750000000}]
        problem = Problem("atcoder", "abc350_a", "adt", "C", "x", 1, 1, (), "x")
        pairs = [{"problem_id": "abc350_a", "contest_id": "adt", "problem_index": "C"}, {"problem_id": "abc350_a", "contest_id": "abc350", "problem_index": "A"}]
        canonical = canonical_atcoder([problem], contests, pairs)[0]
        self.assertEqual((canonical.contest_id, canonical.index), ("abc350", "A"))
        self.assertEqual(dated_problems([canonical], contests, "atcoder")[0].published_at, 1710000000)
        old = [{"id": "abc350", "start_epoch_second": 1600000000}]
        self.assertEqual(dated_problems([canonical], old, "atcoder"), [])
        parsed = parse_atcoder_problems([{"id": "abc350_a", "contest_id": "abc350"}], {"abc350_a": {"difficulty": 0}})
        self.assertEqual(parsed[0].difficulty, 147)

    def test_archive_retries_and_preserves_completed_work(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory)); seed(paths)
            db = Database(paths.database)
            with db.conn:
                db.conn.execute("DELETE FROM statements WHERE platform='cf' AND problem_id='2000A'")
            with patch("cptrainer.archive.Downloader.fetch", side_effect=OSError("interrupted")):
                archive_statements(db, paths, "cf", lambda _: None)
            self.assertEqual(db.conn.execute("SELECT status FROM statements WHERE problem_id='2000A'").fetchone()[0], "failed")
            raw = b'<div class="problem-statement"><div class="title">A. Test</div><p>Actual content</p></div>'
            with patch("cptrainer.archive.Downloader.fetch", return_value=(raw, "unused")) as fetch:
                archive_statements(db, paths, "cf", lambda _: None)
                self.assertEqual(fetch.call_count, 1)
            self.assertEqual(db.conn.execute("SELECT status FROM statements WHERE problem_id='2000A'").fetchone()[0], "ready")
            db.close()

    def test_legacy_migration_creates_restorable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(SCHEMA)
            db = Database(path)
            db.migrate(); db.migrate()
            self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(len(list((path.parent / "backups").glob("*.sqlite3"))), 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
