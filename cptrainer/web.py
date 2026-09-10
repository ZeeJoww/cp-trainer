"""Local Web UI over the existing trainer and SQLite database."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import mimetypes
from pathlib import Path
import re
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .archive import archive_lock, archive_statements, archive_status
from .config import Config, Paths
from .db import Database, session_ended as ended
from .http_client import CachedHttpClient
from .planner import _point_profile, create_training_contest, finish_session
from .sources import sync_atcoder, sync_codeforces
from .testdata import sync_testdata, testdata_status
from .judge import Judge, run_payload, runtime_status

STATIC = Path(__file__).parent / "static"


def submit_url(problem) -> str:
    if problem["platform"] == "cf":
        return f"https://codeforces.com/contest/{problem['contest_id']}/submit?problemIndex={problem['problem_index']}"
    return f"https://atcoder.jp/contests/{problem['contest_id']}/submit?taskScreenName={problem['problem_id']}"


def integer(value, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name}应为 {low}–{high} 的整数")
    return value


def schedule(value):
    if value is None:
        return None
    return integer(value, int(time.time()) + 1, int(time.time()) + 366 * 86400, "预约时间（未来一年内）")


def session_payload(db: Database, session_id: int) -> dict:
    s = dict(db.get_session(session_id))
    s["ended"] = ended(s)
    s["items"] = []
    for row in db.get_session_items(session_id):
        item = dict(row)
        item.pop("tags_json", None)
        if not s["ended"]:
            item.pop("difficulty", None)
            item.pop("target_difficulty", None)
        item["submit_url"] = submit_url(item)
        item["offline"] = db.conn.execute("SELECT status FROM statements WHERE platform=? AND problem_id=?", (item["platform"], item["problem_id"])).fetchone()
        item["offline"] = item["offline"][0] if item["offline"] else "pending"
        s["items"].append(item)
    return s


class TrainerServer(HTTPServer):
    def __init__(self, address, paths: Paths):
        super().__init__(address, Handler)
        self.paths = paths
        self.token = secrets.token_urlsafe(24)
        self.sync_thread: threading.Thread | None = None
        self.sync_info = {"running": False, "message": "", "errors": []}
        self.last_tick = 0.0
        self.judge = Judge(paths)
        self.testdata_thread = None
        self.testdata_info = {"message": "", "error": ""}

    def start_testdata(self):
        if self.testdata_thread and self.testdata_thread.is_alive():
            raise ValueError("测试数据正在下载")
        with archive_lock(self.paths, "testdata"):
            pass
        self.testdata_info = {"message": "正在核对存档中的最近 100 场…", "error": ""}
        def run():
            try:
                with self.database() as db:
                    sync_testdata(db, self.paths, progress=lambda message: self.testdata_info.update(message=message))
            except Exception as exc:
                self.testdata_info["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        self.testdata_thread = threading.Thread(target=run, name="cp-trainer-testdata", daemon=True)
        self.testdata_thread.start()

    @contextmanager
    def database(self):
        db = Database(self.paths.database)
        try:
            yield db
        finally:
            db.close()

    def service_actions(self):
        if time.monotonic() - self.last_tick < 1:
            return
        self.last_tick = time.monotonic()
        with self.database() as db, db.conn:
            db.conn.execute("""UPDATE sessions SET status='running',started_at=scheduled_at
                WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at<=?""", (int(time.time()),))

    def start_sync(self):
        if self.sync_thread and self.sync_thread.is_alive():
            raise ValueError("题库正在同步，请稍候。")
        self.sync_info = {"running": True, "message": "正在更新题库元数据…", "errors": []}

        def run():
            try:
                with self.database() as db:
                    config = Config.load(self.paths.config)
                    client = CachedHttpClient(self.paths.cache)
                    for platform, sync in [("cf", sync_codeforces), ("atcoder", sync_atcoder)]:
                        try:
                            with archive_lock(self.paths, platform):
                                sync(db, config, client, force=True)
                                archive_statements(db, self.paths, platform, lambda msg: self.sync_info.update(message=msg))
                        except Exception as exc:
                            self.sync_info["errors"].append(f"{platform}: {type(exc).__name__}: {str(exc)[:300]}")
            finally:
                self.sync_info.update(running=False, message="本轮同步结束；缺失题面可再次同步续传。")

        self.sync_thread = threading.Thread(target=run, name="cp-trainer-sync", daemon=True)
        self.sync_thread.start()


class Handler(BaseHTTPRequestHandler):
    server: TrainerServer

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def check_origin(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise PermissionError("Host 不被允许")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + host for host in hosts}:
            raise PermissionError("仅接受此应用页面的请求")

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, mutation):
        try:
            self.check_origin()
            url = urlsplit(self.path)
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            if mutation:
                if self.headers.get("X-CP-Trainer") != self.server.token:
                    raise PermissionError("页面已过期，请刷新后重试。")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求应为 JSON")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= (800000 if url.path == "/api/judge/runs" else 32768):
                    raise ValueError("请求大小超出限制")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求应为对象")
                with self.server.database() as db:
                    result = self.mutate(db, url.path, data)
                self.reply(result)
            elif url.path.startswith("/api/"):
                with self.server.database() as db:
                    result = self.get_data(db, url.path, query)
                self.reply(result)
            else:
                self.serve_file(url.path)
        except PermissionError as exc:
            self.reply({"error": str(exc)}, 403)
        except KeyError as exc:
            self.reply({"error": str(exc)}, 404)
        except (ValueError, TypeError, OverflowError) as exc:
            self.reply({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.reply({"error": f"操作失败（{type(exc).__name__}），请重试。"}, 500)

    def serve_file(self, path):
        if path.startswith("/assets/"):
            name = path.removeprefix("/assets/")
            if not re.fullmatch(r"[a-f0-9]{64}\.[a-z0-9]{1,6}", name):
                raise KeyError("文件不存在")
            target = self.server.paths.home / "assets" / name
        else:
            target = (STATIC / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if not target.is_relative_to(STATIC.resolve()):
                raise PermissionError("文件路径无效")
        if not target.is_file():
            raise KeyError("文件不存在")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.reply(target.read_bytes(), content_type=content_type)

    def get_data(self, db, path, query):
        if path == "/api/judge/status":
            running = False
            try:
                with archive_lock(self.server.paths, "testdata"):
                    pass
            except ValueError:
                running = True
            return testdata_status(db) | {"running": running, "sync": self.server.testdata_info, "runtime": self.server.judge.runtime}
        if path == "/api/judge/runs":
            if query.get("problem_id"):
                rows = db.conn.execute("SELECT * FROM judge_runs WHERE platform='atcoder' AND problem_id=? ORDER BY id DESC LIMIT 30", (query["problem_id"],))
            else:
                rows = db.conn.execute("SELECT * FROM judge_runs ORDER BY id DESC LIMIT 50")
            return {"items": [run_payload(row) for row in rows]}
        match = re.fullmatch(r"/api/judge/runs/(\d+)", path)
        if match:
            row = db.conn.execute("SELECT * FROM judge_runs WHERE id=?", (int(match[1]),)).fetchone()
            if not row:
                raise KeyError("评测记录不存在")
            return run_payload(row, source=True)
        if path == "/api/state":
            sessions = [dict(s) | {"ended": ended(s)} for s in db.list_sessions(500)]
            return {"token": self.server.token, "now": int(time.time()), "archive": archive_status(db), "sessions": sessions,
                    "profiles": [dict(p) for p in db.profiles()], "sync": self.server.sync_info}
        match = re.fullmatch(r"/api/sessions/(\d+)(/tags)?", path)
        if match:
            sid = int(match[1])
            if match[2]:
                if not ended(db.get_session(sid)):
                    raise PermissionError("比赛结束后才可显示标签")
                return {str(row["ordinal"]): json.loads(row["tags_json"]) for row in db.get_session_items(sid)}
            return session_payload(db, sid)
        if path == "/api/problems":
            clauses, values = ["p.is_real=1"], []
            if query.get("platform") in {"cf", "atcoder"}:
                clauses.append("p.platform=?")
                values.append(query["platform"])
            if query.get("q"):
                clauses.append("(p.title LIKE ? OR p.problem_id LIKE ?)")
                values += ["%" + query["q"][:160] + "%"] * 2
            if query.get("ready") == "1":
                clauses.append("s.status='ready'")
            if query.get("judge") == "1":
                clauses.append("EXISTS (SELECT 1 FROM judge_sets j WHERE j.platform=p.platform AND j.problem_id=p.problem_id AND j.checker!='unsupported')")
            for key, op in [("min", ">="), ("max", "<=")]:
                if query.get(key):
                    val = float(query[key])
                    if not math.isfinite(val):
                        raise ValueError("难度应为有限数字")
                    clauses.append(f"p.difficulty{op}?")
                    values.append(val)
            base = " FROM problems p LEFT JOIN statements s USING(platform,problem_id) WHERE " + " AND ".join(clauses)
            total = db.conn.execute("SELECT COUNT(*)" + base, values).fetchone()[0]
            page = max(1, min(100000, int(query.get("page", 1))))
            rows = db.conn.execute("SELECT p.platform,p.problem_id,p.contest_id,p.problem_index,p.title,p.difficulty,p.url,p.published_at,COALESCE(s.status,'pending') offline,(SELECT case_count FROM judge_sets j WHERE j.platform=p.platform AND j.problem_id=p.problem_id AND j.checker!='unsupported') judge_cases" + base + " ORDER BY p.published_at DESC,p.contest_id DESC,p.problem_index LIMIT 40 OFFSET ?", values + [(page - 1) * 40]).fetchall()
            return {"total": total, "page": page, "items": [dict(r) for r in rows]}
        if path == "/api/problem":
            row = db.conn.execute("""SELECT p.platform,p.problem_id,p.contest_id,p.problem_index,p.title,p.difficulty,p.url,
                s.html,s.status offline,s.error FROM problems p LEFT JOIN statements s USING(platform,problem_id)
                WHERE p.platform=? AND p.problem_id=? AND p.is_real=1""", (query.get("platform"), query.get("id"))).fetchone()
            if not row:
                raise KeyError("题目不存在")
            tests = db.conn.execute("SELECT case_count,checker,reason,time_limit_ms,memory_limit_mb FROM judge_sets WHERE platform=? AND problem_id=?", (row["platform"], row["problem_id"])).fetchone()
            return dict(row) | {"submit_url": submit_url(row), "judge": dict(tests) if tests else None}
        raise KeyError("接口不存在")

    def mutate(self, db, path, data):
        if path == "/api/testdata/sync":
            self.server.start_testdata()
            return {"ok": True}
        if path == "/api/judge/environment":
            self.server.judge.runtime = runtime_status(self.server.paths)
            return self.server.judge.runtime
        if path == "/api/judge/runs":
            return {"id": self.server.judge.submit(db, data)}
        match = re.fullmatch(r"/api/judge/runs/(\d+)/cancel", path)
        if match:
            self.server.judge.cancel_run(int(match[1]), db)
            return {"ok": True}
        if path == "/api/sync":
            self.server.start_sync()
            return {"ok": True}
        if path == "/api/sessions/clear":
            if db.conn.execute("SELECT 1 FROM judge_runs WHERE session_id IS NOT NULL AND verdict IN ('QUEUED','JUDGING')").fetchone():
                raise ValueError("比赛还有评测未完成，请先等待或取消评测")
            backup = db.clear_sessions()
            return {"backup": backup.name}
        if path == "/api/profiles":
            platform = data.get("platform")
            if platform not in {"cf", "atcoder"}:
                raise ValueError("请选择平台")
            rating = integer(data.get("rating"), 0, 5000, "本地训练分")
            db.update_profile(platform, local_rating=rating)
            return {"ok": True}
        if path == "/api/sessions":
            platform = data.get("platform")
            if platform not in {"cf", "atcoder"}:
                raise ValueError("请选择平台")
            name = data.get("name", "")
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise ValueError("比赛名称应为 1–120 个字符")
            duration = integer(data.get("duration"), 5, 1440, "比赛时长")
            scheduled = schedule(data.get("scheduled_at"))
            if not isinstance(data.get("rated", True), bool):
                raise ValueError("计分选项无效")
            rated = data.get("rated", True)
            seed = secrets.randbelow(2**31)
            ids = data.get("problems")
            if ids is not None:
                if not isinstance(ids, list) or not 1 <= len(ids) <= 26 or any(not isinstance(pid, str) for pid in ids) or len(set(ids)) != len(ids):
                    raise ValueError("请选择 1–26 道不同的题目")
                items = []
                points = _point_profile(platform, len(ids))
                for i, pid in enumerate(ids):
                    row = db.conn.execute("""SELECT p.difficulty FROM problems p JOIN statements s USING(platform,problem_id)
                        WHERE p.platform=? AND p.problem_id=? AND p.is_real=1 AND s.status='ready'""", (platform, pid)).fetchone()
                    if not row:
                        raise ValueError("所选题目尚未完成离线下载或平台不一致")
                    if rated and row[0] is None:
                        raise ValueError("包含未估分题目，请关闭本地计分")
                    items.append((platform, pid, row[0] if row[0] is not None else 0, points[i]))
                sid = db.create_session(name=name.strip(), kind="contest", platform=platform, mode="manual", duration_minutes=duration, scheduled_at=scheduled, rated=rated, seed=seed, items=items)
            else:
                count = integer(data.get("count"), 1, 26, "题目数量")
                db.offline_only = True
                sid, _ = create_training_contest(db, platform=platform, count=count, duration_minutes=duration, name=name.strip(), seed=seed,
                    rated=rated, scheduled_at=scheduled, exclude_recent_days=Config.load(self.server.paths.config).exclude_recent_days)
            return {"id": sid}
        match = re.fullmatch(r"/api/sessions/(\d+)/(schedule|start|record|finish)", path)
        if not match:
            raise KeyError("接口不存在")
        sid, action = int(match[1]), match[2]
        session = db.get_session(sid)
        if action == "schedule":
            db.schedule_session(sid, schedule(data.get("scheduled_at")))
        elif action == "start":
            db.start_session(sid)
        elif action == "finish":
            if session["status"] != "running":
                raise ValueError("请先开始比赛")
            if db.conn.execute("SELECT 1 FROM judge_runs WHERE session_id=? AND verdict IN ('QUEUED','JUDGING')", (sid,)).fetchone():
                raise ValueError("本场还有评测未完成，请先等待或取消评测")
            finish_session(db, sid)
        elif action == "record":
            if session["status"] != "running":
                raise ValueError("只能录入已开始且未结算的比赛")
            if not isinstance(data.get("solved"), bool):
                raise ValueError("解题结果无效")
            elapsed = data.get("elapsed_seconds")
            if elapsed is not None:
                elapsed = integer(elapsed, 0, session["duration_minutes"] * 60, "用时（秒）")
            if data["solved"] and elapsed is None:
                raise ValueError("AC 结果需要填写用时")
            db.record_result(sid, integer(data.get("ordinal"), 1, 26, "题号"), solved=data["solved"], elapsed_seconds=elapsed,
                wrong_attempts=integer(data.get("wrong_attempts"), 0, 10000, "错误次数"))
        return session_payload(db, sid)


def main():
    from .cli import _default_home
    parser = argparse.ArgumentParser(description="cp-trainer 本地 Web 服务")
    parser.add_argument("--home", type=Path, default=_default_home())
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    paths = Paths(args.home.expanduser().resolve())
    paths.ensure()
    if not paths.config.exists():
        Config(timezone="Asia/Shanghai").save(paths.config)
    config = Config.load(paths.config)
    db = Database(paths.database)
    db.migrate()
    db.initialize_profiles(cf_handle=config.cf_handle, atcoder_handle=config.atcoder_handle,
        cf_rating=config.cf_official_rating, atcoder_rating=config.atcoder_official_rating)
    db.close()
    server = TrainerServer(("127.0.0.1", args.port), paths)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"CP Trainer: {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
