"""Archive integrity and offline judging regressions, using isolated synthetic cases."""
from pathlib import Path
import io
import json
import hashlib
from http.client import IncompleteRead
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cptrainer.testdata import archive_complete, download_archive, extract_archive, select_contests, statement_rules
from cptrainer.judge import Judge, container_run, same_output
from cptrainer.config import Paths
from cptrainer.db import Database
from test_web import seed


class ArchiveTests(unittest.TestCase):
    def test_order_by_contest_date_and_match_branch_alias(self):
        refs = "a" * 40 + " refs/heads/abc377\n" + "b" * 40 + " refs/heads/jsc2024_final\n"
        contests = [{"id": "abc377", "start_epoch_second": 100, "duration_second": 10},
                    {"id": "jsc2024-final", "start_epoch_second": 200, "duration_second": 10}]
        self.assertEqual(select_contests(refs, contests, 1)[0]["id"], "jsc2024-final")
        with self.assertRaises(ValueError):
            select_contests(refs, contests, 3)

    def test_pairing_and_unsafe_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def make(entries):
                target = root / "data.tar.gz"
                with tarfile.open(target, "w:gz") as archive:
                    for name, data in entries:
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                return target
            archive = make([("repo/abc377/A/in/01", b"2\n"), ("repo/abc377/A/out/01", b"4\n")])
            manifest = extract_archive(archive, root / "valid", "abc377")
            self.assertEqual(len(manifest["tasks"]["A"]), 1)
            self.assertEqual(manifest["tasks"]["A"][0]["input"]["size"], 2)
            archive.write_bytes(archive.read_bytes()[:-8])
            with self.assertRaises(EOFError):
                extract_archive(archive, root / "truncated", "abc377")
            archive = make([("repo/abc377/A/in/01", b"2\n")])
            missing = extract_archive(archive, root / "missing", "abc377")
            self.assertEqual(missing["tasks"]["A"], [])
            self.assertEqual(missing["incomplete"]["A"]["missing_output"], ["01"])
            archive = make([("repo/abc377/A/in/../../escape", b"bad")])
            with self.assertRaisesRegex(ValueError, "路径"):
                extract_archive(archive, root / "unsafe", "abc377")

    def test_resume_and_server_ignoring_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths(Path(temporary)); paths.ensure()
            cache = paths.cache / "testdata"; cache.mkdir()
            contest = {"id": "abc377", "revision": "a" * 40}
            partial = cache / ("a" * 40 + ".tar.part")
            partial.write_bytes(b"abc")
            response = io.BytesIO(b"def")
            response.status = 206
            response.headers = {"Content-Range": "bytes 3-5/6", "Content-Length": "3"}
            with patch('cptrainer.testdata.build_opener') as opener:
                opener.return_value.open.return_value = response
                path, digest = download_archive(paths, contest)
                self.assertEqual(opener.return_value.open.call_args.args[0].get_header("Range"), "bytes=3-")
            self.assertEqual(path.read_bytes(), b"abcdef")
            self.assertEqual(digest, hashlib.sha256(b"abcdef").hexdigest())
            path.unlink(); partial.write_bytes(b"stale")
            response = io.BytesIO(b"whole")
            response.status = 200
            response.headers = {"Content-Length": "5"}
            with patch('cptrainer.testdata.build_opener') as opener:
                opener.return_value.open.return_value = response
                path, _ = download_archive(paths, contest)
            self.assertEqual(path.read_bytes(), b"whole")
            path.unlink()
            response = io.BytesIO(b"retried")
            response.status = 200
            response.headers = {"Content-Length": "7"}
            with patch('cptrainer.testdata.build_opener') as opener, patch('cptrainer.testdata.time.sleep'):
                opener.return_value.open.side_effect = [IncompleteRead(b""), response]
                path, _ = download_archive(paths, contest)
                self.assertEqual(opener.return_value.open.call_count, 2)
            self.assertEqual(path.read_bytes(), b"retried")

    def test_missing_and_corrupt_files_are_not_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths(Path(temporary)); paths.ensure()
            folder = paths.home / 'testdata' / 'abc377' / ('a' * 40)
            (folder / 'A' / 'in').mkdir(parents=True)
            target = folder / 'A' / 'in' / '01'; target.write_bytes(b"original")
            (folder / 'manifest.json').write_text(json.dumps({"files": {"A/in/01": {"size": 8, "sha256": hashlib.sha256(b"original").hexdigest()}}}))
            self.assertEqual(archive_complete(paths, 'abc377', 'a' * 40, hashes=True), 1)
            target.write_bytes(b"modified")
            self.assertEqual(archive_complete(paths, 'abc377', 'a' * 40, hashes=True), 0)
            target.unlink()
            self.assertEqual(archive_complete(paths, 'abc377', 'a' * 40), 0)

    def test_statement_checker_and_limits(self):
        prefix = '<p>Time Limit: 2 sec / Memory Limit: 1024 MiB</p><section><h3>Output</h3>'
        normal = statement_rules(prefix + '<p>Print the number of paths.</p></section>')
        self.assertEqual(normal["checker"], "tokens")
        self.assertEqual(normal["time_limit_ms"], 2000)
        special = statement_rules(prefix + '<p>If there are multiple solutions, print any of them.</p></section>')
        self.assertEqual(special["checker"], "unsupported")
        self.assertEqual(statement_rules('<p>Multiple reading orders are possible; you may print any of them.</p>' + prefix + '<p>Print the book numbers in reading order.</p></section>')["checker"], "unsupported")
        self.assertEqual(statement_rules(prefix + '<p>If there is a pair of integers that satisfies the conditions, print it.</p></section>')["checker"], "unsupported")
        self.assertEqual(statement_rules(prefix + '<p>Print your solution. Any one of them is accepted.</p></section>')["checker"], "unsupported")
        floating = statement_rules(prefix + '<p>Your answer is accepted if its absolute or relative error is at most <var>10^{-6}</var>.</p></section>')
        self.assertEqual(floating["checker"], "float")
        self.assertEqual(floating["tolerance"], 1e-6)
        self.assertEqual(statement_rules(prefix + '<p>Print the answer. Its absolute error must be at most <var>10^{−8}</var>.</p></section>')["checker"], "float_abs")
        self.assertEqual(statement_rules(prefix + '<p>Print Yes or No. You can output each letter in any case (upper or lower).</p></section>')["checker"], "tokens_ci")
        self.assertEqual(statement_rules("missing")["checker"], "unsupported")

    def test_output_comparison(self):
        self.assertTrue(same_output(b"12\tYes\n", b"12 Yes", "tokens", None))
        self.assertFalse(same_output(b"12 yes", b"12 Yes", "tokens", None))
        self.assertFalse(same_output(b"1\nextra", b"1", "tokens", None))
        self.assertTrue(same_output(b"0.33333334", b"0.3333333333", "float", 1e-6))
        self.assertFalse(same_output(b"NaN", b"0.5", "float", 1e-6))
        self.assertFalse(same_output(b"inf", b"1", "float", 1e-6))
        self.assertTrue(same_output(b"yEs NO", b"Yes No", "tokens_ci", None))
        self.assertFalse(same_output(b"1000001", b"1000000", "float_abs", 1e-6))
        self.assertTrue(same_output(b"1000001", b"1000000", "float_rel", 1e-6))


def seed_judge(paths):
    seed(paths)
    folder = paths.home / "testdata" / "synthetic" / "A"
    manifest = []
    for number, (given, expected) in enumerate([(b"2\n", b"4\n"), (b"7\n", b"49\n")]):
        name = str(number)
        record = {"name": name}
        for side, key, value in [("in", "input", given), ("out", "output", expected)]:
            (folder / side).mkdir(parents=True, exist_ok=True)
            (folder / side / name).write_bytes(value)
            record[key] = {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        manifest.append(record)
    db = Database(paths.database)
    with db.conn:
        db.conn.execute("INSERT INTO test_archives(branch,contest_id,title,started_at,revision,status,problem_count,case_count) VALUES('synthetic','abc350','Synthetic',1,?,'ready',1,2)", ("a" * 40,))
        db.conn.execute("INSERT INTO judge_sets VALUES('atcoder','abc350_a','synthetic',?,2,?,'tokens',NULL,'测试规则',1000,128)", (folder.relative_to(paths.home).as_posix(), json.dumps(manifest)))
    db.close()


class JudgeStateTests(unittest.TestCase):
    def test_guards_scoring_restart_and_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths(Path(temporary))
            seed_judge(paths)
            with patch('cptrainer.judge.runtime_status', return_value={"available": False, "message": "Unavailable"}):
                judge = Judge(paths)
            judge.runtime = {"available": True, "image": "fake"}
            db = Database(paths.database)
            sid = db.create_session(name="Judge", kind="contest", platform="atcoder", mode="manual", duration_minutes=5, scheduled_at=None, rated=False, seed=1, items=[("atcoder", "abc350_a", 1500, 100)])
            data = {"platform": "atcoder", "problem_id": "abc350_a", "language": "python3", "source": "print(4)", "session_id": sid}
            with self.assertRaisesRegex(ValueError, "尚未开始"):
                judge.submit(db, data)
            db.start_session(sid, int(time.time()) - 50)
            with patch.object(threading.Thread, "start"):
                run_id = judge.submit(db, data)
            run = db.conn.execute("SELECT * FROM judge_runs WHERE id=?", (run_id,)).fetchone()
            judge._finish(db, run, "WA", 0, {})
            self.assertEqual(db.get_session_items(sid)[0]["wrong_attempts"], 1)
            judge._finish(db, run, "AC", 2, {})
            self.assertEqual(db.get_session_items(sid)[0]["solved"], 1)
            with patch.object(threading.Thread, "start"):
                cancelled = judge.submit(db, data)
            judge.cancel_run(cancelled, db)
            self.assertEqual(db.conn.execute("SELECT verdict FROM judge_runs WHERE id=?", (cancelled,)).fetchone()[0], "CANCELLED")
            with self.assertRaises(ValueError):
                judge.submit(db, data | {"source": "x" * 131073})
            with db.conn:
                db.conn.execute("UPDATE judge_sets SET checker='unsupported',reason='需要特殊判题器'")
            with self.assertRaisesRegex(ValueError, "特殊判题器"):
                judge.submit(db, data)
            with db.conn:
                db.conn.execute("UPDATE judge_runs SET verdict='JUDGING' WHERE id=?", (run_id,))
            with patch('cptrainer.judge.runtime_status', return_value={"available": False}):
                Judge(paths)
            self.assertEqual(db.conn.execute("SELECT verdict,source FROM judge_runs WHERE id=?", (run_id,)).fetchone()[:], ("INTERRUPTED", "print(4)"))
            db.close()


@unittest.skipUnless(os.environ.get("CP_TRAINER_JUDGE_TEST_IMAGE") and shutil.which("docker"), "设置 CP_TRAINER_JUDGE_TEST_IMAGE 运行容器评测回归")
class ContainerTests(unittest.TestCase):
    def test_execution_verdicts_limits_and_isolation(self):
        image = os.environ["CP_TRAINER_JUDGE_TEST_IMAGE"]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            folder.chmod(0o777)
            def run(source, seconds=1, memory=128):
                (folder / "main.py").write_text(source)
                (folder / "main.py").chmod(0o644)
                return container_run(image, folder, ["/usr/local/bin/python3", "-I", "/work/main.py"], wall_seconds=seconds, memory_mb=memory, owner="cp-trainer-check", cancel=threading.Event())
            self.assertEqual(run("print(4)")["output"], b"4\n")
            self.assertEqual(run("raise RuntimeError('expected')")["verdict"], "RE")
            self.assertEqual(run("while True: pass")["verdict"], "TLE")
            self.assertEqual(run("a=bytearray(512*1024*1024)", memory=64)["verdict"], "MLE")
            self.assertEqual(run("import sys;sys.stdout.write('x'*20000000)")["verdict"], "OLE")
            isolation = run("import socket,pathlib\nassert not pathlib.Path('/home').exists()\nassert not pathlib.Path('/testbed').exists()\ntry: socket.create_connection(('1.1.1.1',443),timeout=0.2)\nexcept OSError: print('isolated')\nelse: raise RuntimeError('network enabled')")
            self.assertEqual(isolation["output"], b"isolated\n", isolation)


if __name__ == "__main__":
    unittest.main()
