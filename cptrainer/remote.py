"""Personal browser bridge: the user's browser owns login and the original submit form."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from .db import Database
from .judge import SOURCE_LIMIT, finish_run, validate_session


TIMEOUT = 15 * 60
FINAL = {"AC", "WA", "TLE", "MLE", "RE", "CE", "OLE", "PE", "FAILED", "UNKNOWN", "CANCELLED"}


def submit_url(problem) -> str:
    if problem["platform"] == "cf":
        return f"https://codeforces.com/contest/{problem['contest_id']}/submit?problemIndex={problem['problem_index']}"
    return f"https://atcoder.jp/contests/{problem['contest_id']}/submit?taskScreenName={problem['problem_id']}"


def problem_payload(db: Database, platform, pid) -> dict:
    row = db.conn.execute("SELECT * FROM problems WHERE platform=? AND problem_id=? AND is_real=1", (platform, pid)).fetchone()
    if not row or platform not in {"cf", "atcoder"}:
        raise ValueError("请选择本地题库中的 AtCoder / Codeforces 题目")
    return {"platform": platform, "problem_id": pid, "contest_id": row["contest_id"],
            "problem_index": row["problem_index"], "url": submit_url(row)}


def create_run(db: Database, data: dict) -> dict:
    problem = problem_payload(db, data.get("platform"), data.get("problem_id"))
    source, language, key = (data.get(k) for k in ["source", "language", "request_key"])
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > SOURCE_LIMIT or "\x00" in source:
        raise ValueError("代码应为非空文本，最多 128 KiB")
    if not isinstance(language, str) or not re.fullmatch(r"\d{1,8}", language):
        raise ValueError("请先连接原站并选择编程语言")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9-]{36}", key):
        raise ValueError("提交标识无效，请重新打开提交窗口")
    sid = data.get("session_id")
    old = db.conn.execute("SELECT * FROM judge_runs WHERE request_key=?", (key,)).fetchone()
    if old:
        if any(old[k] != v for k, v in {"platform": problem["platform"], "problem_id": problem["problem_id"], "language": language, "source": source, "session_id": sid, "engine": "remote"}.items()):
            raise ValueError("同一提交标识不能对应不同代码")
        run_id = old["id"]
    else:
        validate_session(db, sid, problem["platform"], problem["problem_id"])
        if db.conn.execute("SELECT 1 FROM judge_runs WHERE engine='remote' AND platform=? AND verdict IN ('QUEUED','JUDGING')", (problem["platform"],)).fetchone():
            raise ValueError("此平台已有提交正在处理，请先等待或停止等待")
        label = str(data.get("language_name", language))[:120]
        result = {"message": "等待浏览器助手提交；登录或验证请在原站完成。", "language_name": label}
        with db.conn:
            run_id = db.conn.execute("INSERT INTO judge_runs(platform,problem_id,session_id,language,source,submitted_at,total,engine,request_key,result_json) VALUES(?,?,?,?,?,?,0,'remote',?,?)",
                (problem["platform"], problem["problem_id"], sid, language, source, int(time.time()), key, json.dumps(result))).lastrowid
    return problem | {"id": run_id, "request_key": key, "source": source, "language": language,
                      "deadline": db.conn.execute("SELECT submitted_at FROM judge_runs WHERE id=?", (run_id,)).fetchone()[0] + TIMEOUT}


def checked_remote_url(run, db: Database, value) -> str:
    if not isinstance(value, str):
        raise ValueError("原站提交链接无效")
    contest = db.conn.execute("SELECT contest_id FROM problems WHERE platform=? AND problem_id=?", (run["platform"], run["problem_id"])).fetchone()[0]
    u = urlsplit(value)
    path = rf"/contests/{re.escape(contest)}/submissions/\d+" if run["platform"] == "atcoder" else rf"/contest/{re.escape(contest)}/submission/\d+"
    host = "atcoder.jp" if run["platform"] == "atcoder" else "codeforces.com"
    if u.scheme != "https" or u.netloc != host or u.query or u.fragment or not re.fullmatch(path, u.path):
        raise ValueError("原站提交链接与本题不一致")
    return value


def event(db: Database, run_id: int, key: str, data: dict) -> dict:
    run = db.conn.execute("SELECT * FROM judge_runs WHERE id=? AND engine='remote'", (run_id,)).fetchone()
    if not run or not isinstance(key, str) or not secrets.compare_digest(key, run["request_key"] or ""):
        raise PermissionError("浏览器助手提交令牌无效")
    if run["finished_at"] is not None:
        return {"ok": True, "allowed": False}
    phase = data.get("phase")
    if phase == "claim":
        with db.conn:
            claimed = db.conn.execute("UPDATE judge_runs SET verdict='JUDGING' WHERE id=? AND verdict='QUEUED' AND submitted_at>=?", (run_id, int(time.time()) - TIMEOUT)).rowcount
        return {"ok": True, "allowed": bool(claimed)}
    if phase not in {"sent", "result", "error"}:
        raise ValueError("提交事件无效")
    if phase in {"sent", "result"} and run["verdict"] not in {"JUDGING", "UNKNOWN"}:
        raise ValueError("此提交尚未发送")
    result = json.loads(run["result_json"])
    if phase in {"sent", "result"}:
        result["remote_url"] = checked_remote_url(run, db, data.get("url"))
        expected = hashlib.sha256(run["source"].replace("\r\n", "\n").strip().encode()).hexdigest()
        if not secrets.compare_digest(str(data.get("source_hash", "")), expected):
            raise ValueError("原站代码与本次提交不一致，未回填结果")
    if phase == "sent":
        result["message"] = "原站已收到代码，正在等待评测结果。"
        with db.conn:
            db.conn.execute("UPDATE judge_runs SET result_json=? WHERE id=?", (json.dumps(result), run_id))
    else:
        verdict = data.get("verdict") if phase == "result" else "FAILED" if run["verdict"] == "QUEUED" else "UNKNOWN"
        if verdict not in FINAL:
            raise ValueError("原站结果无效")
        result["message"] = str(data.get("message", "原站评测已完成。"))[:500]
        finish_run(db, run, verdict, 0, result)
    return {"ok": True}


def stop_waiting(db: Database, run_id: int) -> None:
    run = db.conn.execute("SELECT * FROM judge_runs WHERE id=? AND engine='remote'", (run_id,)).fetchone()
    if not run:
        raise KeyError("提交不存在")
    result = json.loads(run["result_json"])
    result["message"] = "已停止本地等待；已送出的原站提交无法撤回，请到原站核对结果。"
    finish_run(db, run, "CANCELLED", 0, result)


def expire_runs(db: Database) -> None:
    rows = db.conn.execute("SELECT * FROM judge_runs WHERE engine='remote' AND verdict IN ('QUEUED','JUDGING') AND submitted_at<?", (int(time.time()) - TIMEOUT,)).fetchall()
    for row in rows:
        result = json.loads(row["result_json"])
        result["message"] = "等待已超时；提交可能已送达，请先在原站核对，系统不会自动重发。"
        finish_run(db, row, "UNKNOWN", 0, result)
