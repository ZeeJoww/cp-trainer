"""Local judging with a fresh, resource-limited, offline Docker container per case."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import secrets
import subprocess
import tempfile
import threading
import time

from .config import Paths
from .db import Database, session_ended

LANGUAGES = {"cpp17": ("main.cpp", ["/work/main"]), "python3": ("main.py", ["/usr/local/bin/python3", "-I", "-B", "/work/main.py"])}
OUTPUT_LIMIT = 16 * 1024 * 1024
SOURCE_LIMIT = 128 * 1024


def runtime_status(paths: Paths) -> dict:
    try:
        settings = json.loads((paths.home / "judge-runtime.json").read_text())
        image = settings["image"]
        if not isinstance(image, str) or not image.startswith("sha256:") or len(image) != 71:
            raise ValueError("评测镜像未固定版本")
        subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, check=True)
        return settings | {"available": True}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {"available": False, "message": "评测环境尚未就绪。请在应用目录运行 python3 scripts/setup_judge.py。"}


def same_output(actual: bytes, expected: bytes, checker: str, tolerance: float | None) -> bool:
    left, right = actual.split(), expected.split()
    if len(left) != len(right):
        return False
    if checker == "tokens":
        return left == right
    if checker == "tokens_ci":
        return [value.lower() for value in left] == [value.lower() for value in right]
    if checker not in {"float", "float_abs", "float_rel"} or tolerance is None or not 0 < tolerance < 1:
        raise ValueError("缺少可用的判题规则")
    for got, wanted in zip(left, right):
        if got == wanted:
            continue
        try:
            a, b = float(got), float(wanted)
        except ValueError:
            return False
        scale = 1 if checker == "float_abs" else abs(b) if checker == "float_rel" else max(1, abs(b))
        if not math.isfinite(a) or not math.isfinite(b) or abs(a - b) > tolerance * scale:
            return False
    return True


def container_run(image: str, work: Path, command: list[str], *, wall_seconds: float, memory_mb: int,
                  owner: str, cancel: threading.Event, input_path: Path | None = None, compile_code=False) -> dict:
    name = "cp-trainer-" + secrets.token_hex(12)
    # Only the submission directory is mounted. Expected outputs and the user's data stay outside.
    args = ["docker", "run", "--init", "--pull=never", "--name", name, "--label", "cp-trainer-owner=" + owner,
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--pids-limit", "32", "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
            "--cpus", "1", "--ulimit", "core=0", "--ulimit", "nofile=64:64", "--ulimit", "fsize=67108864:67108864",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--workdir", "/work",
            "--mount", f"type=bind,source={work},target=/work" + ("" if compile_code else ",readonly"),
            "--entrypoint", "/usr/bin/prlimit", "-i", image, f"--cpu={math.ceil(wall_seconds)}:{math.ceil(wall_seconds) + 1}", "--", *command]
    started = time.monotonic()
    output, errors = bytearray(), bytearray()
    exceeded = threading.Event()
    def drain(stream, target, limit):
        with stream:
            while block := stream.read(65536):
                remaining = limit - len(target)
                target.extend(block[:max(0, remaining)])
                if len(block) > remaining:
                    exceeded.set()
    process = None
    verdict = "OK"
    try:
        with input_path.open("rb") if input_path else open(os.devnull, "rb") as source:
            process = subprocess.Popen(args, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            readers = [threading.Thread(target=drain, args=(process.stdout, output, OUTPUT_LIMIT), daemon=True),
                       threading.Thread(target=drain, args=(process.stderr, errors, 65536), daemon=True)]
            for reader in readers:
                reader.start()
            while process.poll() is None:
                if cancel.is_set() or exceeded.is_set() or time.monotonic() - started > wall_seconds + 2:
                    verdict = "CANCELLED" if cancel.is_set() else "OLE" if exceeded.is_set() else "TLE"
                    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    break
                time.sleep(0.025)
            process.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=3)
        if verdict == "OK":
            inspect = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", name], capture_output=True, text=True, timeout=10)
            container = json.loads(inspect.stdout) if inspect.returncode == 0 else {}
            if exceeded.is_set():
                verdict = "OLE"
            elif container.get("OOMKilled"):
                verdict = "MLE"
            elif process.returncode in {152, 137}:
                verdict = "TLE" if process.returncode == 152 else "RE"
            elif process.returncode in {125, 126, 127} and not container.get("StartedAt", "").startswith("20"):
                verdict = "SE"
            elif process.returncode:
                verdict = "RE"
        return {"verdict": verdict, "elapsed_ms": round((time.monotonic() - started) * 1000),
                "output": bytes(output), "stderr": errors.decode("utf-8", errors="replace")[:8000], "exit_code": process.returncode}
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def run_payload(row, *, source=False) -> dict:
    result = dict(row)
    if not source:
        result.pop("source", None)
    result["result"] = json.loads(result.pop("result_json"))
    return result


class Judge:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.runtime = runtime_status(paths)
        self.owner = hashlib.sha256(str(paths.home.resolve()).encode()).hexdigest()[:16]
        # ponytail: one worker bounds local resource usage; add workers only for measured queue pressure.
        self.jobs = queue.Queue(maxsize=8)
        self.cancel = threading.Event()
        self.active = None
        self.worker = None
        db = Database(paths.database)
        try:
            with db.conn:
                db.conn.execute("UPDATE judge_runs SET verdict='INTERRUPTED',finished_at=? WHERE verdict IN ('QUEUED','JUDGING')", (int(time.time()),))
        finally:
            db.close()
        if self.runtime["available"]:
            stale = subprocess.run(["docker", "ps", "-aq", "--filter", "label=cp-trainer-owner=" + self.owner], capture_output=True, text=True, timeout=10)
            if stale.returncode == 0 and stale.stdout.split():
                subprocess.run(["docker", "rm", "-f", *stale.stdout.split()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)

    def submit(self, db: Database, data: dict) -> int:
        if not self.runtime["available"]:
            raise ValueError(self.runtime["message"])
        platform, pid, language, source = (data.get(k) for k in ["platform", "problem_id", "language", "source"])
        if platform != "atcoder" or not isinstance(pid, str) or language not in LANGUAGES:
            raise ValueError("请选择有测试数据的 AtCoder 题目及 C++17 / Python 3")
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > SOURCE_LIMIT or "\x00" in source:
            raise ValueError("代码应为非空文本，最多 128 KiB")
        row = db.conn.execute("SELECT * FROM judge_sets WHERE platform=? AND problem_id=?", (platform, pid)).fetchone()
        if not row or row["case_count"] < 1:
            raise ValueError("此题尚无完整测试数据，请先下载")
        if row["checker"] == "unsupported":
            raise ValueError(row["reason"])
        sid = data.get("session_id")
        if sid is not None:
            if isinstance(sid, bool) or not isinstance(sid, int):
                raise ValueError("比赛编号无效")
            session = db.get_session(sid)
            if session["status"] != "running" or session_ended(session):
                raise ValueError("本场比赛尚未开始或已结束；可不关联比赛进行补题")
            if not db.conn.execute("SELECT 1 FROM session_problems WHERE session_id=? AND platform=? AND problem_id=?", (sid, platform, pid)).fetchone():
                raise ValueError("这道题不在所选比赛中")
        if self.jobs.full():
            raise ValueError("评测队列已满，请稍后提交")
        with db.conn:
            run_id = db.conn.execute("INSERT INTO judge_runs(platform,problem_id,session_id,language,source,submitted_at,total) VALUES(?,?,?,?,?,?,?)",
                (platform, pid, sid, language, source, int(time.time()), row["case_count"])).lastrowid
        self.jobs.put_nowait(run_id)
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._work, name="cp-trainer-judge", daemon=True)
            self.worker.start()
        return run_id

    def cancel_run(self, run_id: int, db: Database):
        if self.active == run_id:
            self.cancel.set()
        with db.conn:
            db.conn.execute("UPDATE judge_runs SET verdict='CANCELLED',finished_at=? WHERE id=? AND verdict='QUEUED'", (int(time.time()), run_id))

    def _work(self):
        while True:
            run_id = self.jobs.get()
            db = Database(self.paths.database)
            try:
                row = db.conn.execute("SELECT * FROM judge_runs WHERE id=?", (run_id,)).fetchone()
                if row["verdict"] != "QUEUED":
                    continue
                self.cancel.clear()
                self.active = run_id
                with db.conn:
                    claimed = db.conn.execute("UPDATE judge_runs SET verdict='JUDGING' WHERE id=? AND verdict='QUEUED'", (run_id,)).rowcount
                if not claimed:
                    continue
                self._judge(db, row)
            except Exception as exc:
                with db.conn:
                    db.conn.execute("UPDATE judge_runs SET verdict='SE',finished_at=?,result_json=? WHERE id=?",
                        (int(time.time()), json.dumps({"message": f"评测环境 / 数据错误：{type(exc).__name__}: {str(exc)[:300]}"}), run_id))
            finally:
                self.active = None
                db.close()
                self.jobs.task_done()

    def _judge(self, db: Database, run):
        testset = db.conn.execute("SELECT j.*,a.revision FROM judge_sets j JOIN test_archives a USING(branch) WHERE j.platform=? AND j.problem_id=?", (run["platform"], run["problem_id"])).fetchone()
        cases = json.loads(testset["manifest_json"])
        with db.conn:
            db.conn.execute("UPDATE judge_runs SET total=? WHERE id=?", (len(cases), run["id"]))
        folder = (self.paths.home / testset["directory"]).resolve()
        if not folder.is_relative_to((self.paths.home / "testdata").resolve()):
            raise ValueError("测试数据路径不合法")
        runs = self.paths.home / "judge-work"
        runs.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=runs) as temporary:
            work = Path(temporary)
            work.chmod(0o777)
            filename, command = LANGUAGES[run["language"]]
            (work / filename).write_text(run["source"], encoding="utf-8")
            (work / filename).chmod(0o644)
            image = self.runtime["image"]
            result = {"cases": [], "checker": testset["checker"], "revision": testset["revision"],
                      "image": image, "message": "本机测试结果；耗时包含容器启动，时限额外预留 2 秒启动余量。"}
            if run["language"] == "cpp17":
                compiled = container_run(image, work, ["/usr/bin/g++", "-std=c++17", "-O2", "-pipe", "/work/main.cpp", "-o", "/work/main"],
                    wall_seconds=30, memory_mb=1024, owner=self.owner, cancel=self.cancel, compile_code=True)
                if compiled["verdict"] != "OK":
                    result["message"] = compiled["stderr"] or "编译未完成（时间、内存或输出超限）"
                    self._finish(db, run, "CANCELLED" if compiled["verdict"] == "CANCELLED" else "SE" if compiled["verdict"] == "SE" else "CE", 0, result)
                    return
                executable = work / "main"
                if not executable.is_file() or executable.is_symlink():
                    raise ValueError("编译器未产生可执行文件")
            else:
                # Syntax checking also happens in the isolated runtime.
                compiled = container_run(image, work, ["/usr/local/bin/python3", "-I", "-c", "import ast;ast.parse(open('/work/main.py').read())"],
                    wall_seconds=5, memory_mb=256, owner=self.owner, cancel=self.cancel)
                if compiled["verdict"] != "OK":
                    result["message"] = compiled["stderr"]
                    self._finish(db, run, "CANCELLED" if compiled["verdict"] == "CANCELLED" else "SE" if compiled["verdict"] == "SE" else "CE", 0, result)
                    return
            verdict, passed = "AC", 0
            for case in cases:
                if self.cancel.is_set():
                    verdict = "CANCELLED"
                    break
                name = case["name"]
                if Path(name).name != name or name in {".", ".."}:
                    raise ValueError("测试点名称无效")
                for side, key in [("in", "input"), ("out", "output")]:
                    target = folder / side / name
                    if target.is_symlink() or not target.is_file():
                        with db.conn:
                            db.conn.execute("UPDATE test_archives SET status='failed',error='测试数据缺失，请重新下载' WHERE branch=?", (testset["branch"],))
                        raise ValueError("测试数据缺失，请重新下载")
                    with target.open("rb") as stream:
                        if target.stat().st_size != case[key]["size"] or hashlib.file_digest(stream, "sha256").hexdigest() != case[key]["sha256"]:
                            with db.conn:
                                db.conn.execute("UPDATE test_archives SET status='failed',error='测试数据已损坏，请重新下载' WHERE branch=?", (testset["branch"],))
                            raise ValueError("测试数据已损坏，请重新下载")
                measured = container_run(image, work, command, wall_seconds=testset["time_limit_ms"] / 1000,
                    memory_mb=testset["memory_limit_mb"], owner=self.owner, cancel=self.cancel, input_path=folder / "in" / name)
                actual = measured.pop("output")
                expected = (folder / "out" / name).read_bytes()
                current = measured.pop("verdict")
                if current == "OK":
                    current = "AC" if same_output(actual, expected, testset["checker"], testset["tolerance"]) else "WA"
                case_result = {"name": name, "verdict": current, **measured}
                if current != "AC":
                    with (folder / "in" / name).open("rb") as stream:
                        case_result.update(input=stream.read(2000).decode("utf-8", errors="replace"),
                            expected=expected[:2000].decode("utf-8", errors="replace"), actual=actual[:2000].decode("utf-8", errors="replace"))
                result["cases"].append(case_result)
                if current == "AC":
                    passed += 1
                else:
                    verdict = current
                with db.conn:
                    db.conn.execute("UPDATE judge_runs SET passed=?,result_json=? WHERE id=?", (passed, json.dumps(result), run["id"]))
                # Stop at the first failing case; never call a partial pass AC.
                if current != "AC":
                    break
            self._finish(db, run, verdict, passed, result)

    def _finish(self, db: Database, run, verdict: str, passed: int, result: dict):
        with db.conn:
            db.conn.execute("UPDATE judge_runs SET verdict=?,passed=?,finished_at=?,result_json=? WHERE id=?",
                (verdict, passed, int(time.time()), json.dumps(result), run["id"]))
            if run["session_id"] is not None and verdict in {"AC", "WA", "TLE", "MLE", "RE", "OLE"}:
                session = db.get_session(run["session_id"])
                if session["status"] == "running" and session["started_at"] <= run["submitted_at"] < session["started_at"] + session["duration_minutes"] * 60:
                    if verdict == "AC":
                        db.conn.execute("UPDATE session_problems SET solved=1,elapsed_seconds=? WHERE session_id=? AND platform=? AND problem_id=? AND solved=0",
                            (run["submitted_at"] - session["started_at"], run["session_id"], run["platform"], run["problem_id"]))
                    else:
                        db.conn.execute("UPDATE session_problems SET wrong_attempts=wrong_attempts+1 WHERE session_id=? AND platform=? AND problem_id=? AND solved=0",
                            (run["session_id"], run["platform"], run["problem_id"]))
