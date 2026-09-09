from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
import time
from typing import Any, Iterable
from urllib.parse import quote

from .config import Config
from .db import Database
from .http_client import CachedHttpClient, HttpClientError
from .types import Problem, Submission


CF_API = "https://codeforces.com/api"
ATCODER_RESOURCES = "https://kenkoooo.com/atcoder/resources"
ATCODER_API = "https://kenkoooo.com/atcoder/atcoder-api/v3"
SINCE = {"cf": "2024-07-01", "atcoder": "2023-07-03"}


def dated_problems(problems: list[Problem], contests: list[dict], platform: str) -> list[Problem]:
    dates = {str(c["id"]): int(c.get("startTimeSeconds" if platform == "cf" else "start_epoch_second", 0)) for c in contests}
    start = int(datetime.fromisoformat(SINCE[platform]).replace(tzinfo=timezone.utc).timestamp())
    now = int(time.time())
    missing = [p.problem_id for p in problems if p.contest_id not in dates]
    if missing:
        raise ValueError(f"{platform} 比赛日期缺失（{len(missing)} 题）；保留旧题库，请重试同步。")
    return [replace(p, published_at=dates[p.contest_id]) for p in problems if start <= dates[p.contest_id] <= now]


def canonical_atcoder(problems: list[Problem], contests: list[dict], pairs: list[dict]) -> list[Problem]:
    """ADT reuses IDs and can overwrite the basic dataset's contest and index."""
    dates = {str(c["id"]): int(c["start_epoch_second"]) for c in contests}
    first = {}
    for pair in pairs:
        if pair["contest_id"] not in dates:
            raise ValueError("AtCoder 比赛关联表缺少日期，请重试同步")
        pid = pair["problem_id"]
        key = (dates[pair["contest_id"]], pair["contest_id"] != pid.rsplit("_", 1)[0])
        if pid not in first or key < first[pid][0]:
            first[pid] = (key, pair)
    result = []
    for problem in problems:
        if problem.problem_id not in first:
            raise ValueError(f"AtCoder 题目缺少比赛关联：{problem.problem_id}")
        pair = first[problem.problem_id][1]
        result.append(replace(problem, contest_id=pair["contest_id"], index=str(pair["problem_index"]),
            url=f'https://atcoder.jp/contests/{pair["contest_id"]}/tasks/{problem.problem_id}'))
    return result


@dataclass(slots=True)
class SyncReport:
    platform: str
    problems: int = 0
    submissions: int = 0
    official_rating: float | None = None
    warnings: list[str] | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


def _cf_result(payload: Any, method: str) -> Any:
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        comment = payload.get("comment") if isinstance(payload, dict) else payload
        raise HttpClientError(f"Codeforces {method} failed: {comment}")
    return payload.get("result")


def parse_codeforces_problemset(payload: Any) -> list[Problem]:
    result = _cf_result(payload, "problemset.problems")
    if not isinstance(result, dict):
        raise ValueError("unexpected Codeforces problemset response")
    stats: dict[tuple[str, str], int] = {}
    for s in result.get("problemStatistics", []):
        contest_id = str(s.get("contestId", ""))
        index = str(s.get("index", ""))
        stats[(contest_id, index)] = int(s.get("solvedCount", 0) or 0)

    problems: list[Problem] = []
    for p in result.get("problems", []):
        if p.get("contestId") is None or not p.get("index"):
            continue
        contest_id = str(p["contestId"])
        index = str(p["index"])
        problem_id = f"{contest_id}{index}"
        difficulty = p.get("rating")
        problems.append(
            Problem(
                platform="cf",
                problem_id=problem_id,
                contest_id=contest_id,
                index=index,
                title=str(p.get("name", problem_id)),
                difficulty=None if difficulty is None else float(difficulty),
                points=None if p.get("points") is None else float(p["points"]),
                tags=tuple(sorted(str(x) for x in p.get("tags", []))),
                url=f"https://codeforces.com/problemset/problem/{contest_id}/{index}",
                solved_count=stats.get((contest_id, index), 0),
                is_real=True,
            )
        )
    return problems


def parse_codeforces_submissions(payload: Any) -> list[Submission]:
    result = _cf_result(payload, "user.status")
    if not isinstance(result, list):
        raise ValueError("unexpected Codeforces submissions response")
    submissions: list[Submission] = []
    for s in result:
        p = s.get("problem", {})
        if p.get("contestId") is None or not p.get("index"):
            continue
        problem_id = f"{p['contestId']}{p['index']}"
        submissions.append(
            Submission(
                platform="cf",
                submission_id=str(s.get("id", "")),
                problem_id=problem_id,
                epoch_second=int(s.get("creationTimeSeconds", 0) or 0),
                verdict=str(s.get("verdict") or "UNKNOWN"),
                language=str(s.get("programmingLanguage") or ""),
            )
        )
    return submissions


def sync_codeforces(
    db: Database,
    config: Config,
    client: CachedHttpClient,
    *,
    force: bool = False,
) -> SyncReport:
    report = SyncReport(platform="cf")
    payload = client.get_json(
        f"{CF_API}/problemset.problems",
        cache_key="cf-problemset",
        ttl_seconds=24 * 3600,
        force=force,
    )
    contests = _cf_result(client.get_json(f"{CF_API}/contest.list?gym=false", cache_key="cf-contests", ttl_seconds=86400, force=force), "contest.list")
    problems = dated_problems(parse_codeforces_problemset(payload), contests, "cf")
    report.problems = db.upsert_problems(problems)

    handle = config.cf_handle.strip()
    if not handle:
        report.warnings.append("no Codeforces handle configured; skipped profile/submissions")
        return report

    info = client.get_json(
        f"{CF_API}/user.info?handles={quote(handle)}",
        cache_key=f"cf-user-info-{handle}",
        ttl_seconds=10 * 60,
        force=force,
    )
    users = _cf_result(info, "user.info")
    if users and users[0].get("rating") is not None:
        rating = float(users[0]["rating"])
        db.update_profile("cf", handle=handle, official_rating=rating)
        report.official_rating = rating

    known_max = db.max_submission_id("cf")
    start = 1
    page_size = 10000
    imported: list[Submission] = []
    while True:
        url = (
            f"{CF_API}/user.status?handle={quote(handle)}"
            f"&from={start}&count={page_size}"
        )
        page = client.get_json(
            url,
            cache_key=f"cf-user-status-{handle}-{start}",
            ttl_seconds=0,
            force=True,
        )
        submissions = parse_codeforces_submissions(page)
        if known_max is not None:
            new_items = [s for s in submissions if int(s.submission_id) > known_max]
            imported.extend(new_items)
            if len(new_items) < len(submissions):
                break
        else:
            imported.extend(submissions)
        if len(submissions) < page_size:
            break
        start += page_size
    report.submissions = db.upsert_submissions(imported)
    return report


def _as_problem_map(data: Any) -> dict[str, dict[str, Any]]:
    if isinstance(data, dict):
        return {
            str(k): (v if isinstance(v, dict) else {})
            for k, v in data.items()
        }
    if isinstance(data, list):
        result: dict[str, dict[str, Any]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            pid = item.get("id") or item.get("problem_id")
            if pid is not None:
                result[str(pid)] = item
        return result
    return {}


def _atcoder_index(item: dict[str, Any], problem_id: str) -> str:
    value = item.get("problem_index") or item.get("index")
    if value:
        return str(value)
    suffix = problem_id.rsplit("_", 1)[-1]
    return suffix.upper() if len(suffix) <= 3 else suffix


def _first_number(item: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
    return None


def parse_atcoder_problems(
    problems_payload: Any,
    models_payload: Any,
    merged_payload: Any | None = None,
) -> list[Problem]:
    basic = _as_problem_map(problems_payload)
    models = _as_problem_map(models_payload)
    merged = _as_problem_map(merged_payload)
    result: list[Problem] = []
    for problem_id, item in basic.items():
        model = models.get(problem_id, {})
        detail = merged.get(problem_id, {})
        contest_id = str(
            item.get("contest_id")
            or detail.get("contest_id")
            or problem_id.rsplit("_", 1)[0]
        )
        difficulty = _first_number(model, ("difficulty", "rating"))
        if difficulty is None:
            difficulty = _first_number(detail, ("difficulty", "rating"))
        if difficulty is not None:
            difficulty = float(math.floor((difficulty if difficulty >= 400 else 400 * math.exp((difficulty - 400) / 400)) + 0.5))
        point = _first_number(detail, ("point", "points", "score"))
        solver_count = int(
            _first_number(
                detail,
                ("solver_count", "user_count", "accepted_count", "ac_count"),
            )
            or 0
        )
        title = str(
            item.get("name")
            or item.get("title")
            or detail.get("title")
            or problem_id
        )
        result.append(
            Problem(
                platform="atcoder",
                problem_id=problem_id,
                contest_id=contest_id,
                index=_atcoder_index(item, problem_id),
                title=title,
                difficulty=difficulty,
                points=point,
                tags=(),
                url=f"https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}",
                solved_count=solver_count,
                is_real=True,
            )
        )
    return result


def parse_atcoder_submissions(payload: Any) -> list[Submission]:
    if not isinstance(payload, list):
        raise ValueError("unexpected AtCoder Problems submissions response")
    result: list[Submission] = []
    for s in payload:
        if not isinstance(s, dict) or not s.get("problem_id"):
            continue
        result.append(
            Submission(
                platform="atcoder",
                submission_id=str(s.get("id", "")),
                problem_id=str(s["problem_id"]),
                epoch_second=int(s.get("epoch_second", 0) or 0),
                verdict=str(s.get("result") or "UNKNOWN"),
                language=str(s.get("language") or ""),
            )
        )
    return result


def sync_atcoder(
    db: Database,
    config: Config,
    client: CachedHttpClient,
    *,
    force: bool = False,
) -> SyncReport:
    report = SyncReport(platform="atcoder")
    problems_payload = client.get_json(
        f"{ATCODER_RESOURCES}/problems.json",
        cache_key="atcoder-problems",
        ttl_seconds=24 * 3600,
        force=force,
    )
    models_payload = client.get_json(
        f"{ATCODER_RESOURCES}/problem-models.json",
        cache_key="atcoder-models",
        ttl_seconds=24 * 3600,
        force=force,
    )
    try:
        merged_payload = client.get_json(
            f"{ATCODER_RESOURCES}/merged-problems.json",
            cache_key="atcoder-merged-problems",
            ttl_seconds=24 * 3600,
            force=force,
        )
    except HttpClientError as exc:
        merged_payload = None
        report.warnings.append(f"detailed AtCoder metadata unavailable: {exc}")
    problems = parse_atcoder_problems(
        problems_payload, models_payload, merged_payload
    )
    contests = client.get_json(f"{ATCODER_RESOURCES}/contests.json", cache_key="atcoder-contests", ttl_seconds=86400, force=force)
    pairs = client.get_json(f"{ATCODER_RESOURCES}/contest-problem.json", cache_key="atcoder-contest-problem", ttl_seconds=86400, force=force)
    problems = canonical_atcoder(problems, contests, pairs)
    problems = dated_problems(problems, contests, "atcoder")
    with db.conn:
        for p in problems:
            old = db.conn.execute("SELECT contest_id,problem_index FROM problems WHERE platform='atcoder' AND problem_id=?", (p.problem_id,)).fetchone()
            if old and tuple(old) != (p.contest_id, p.index):
                db.conn.execute("UPDATE statements SET status='pending',html='',error='原始比赛信息已校正，请重新下载' WHERE platform='atcoder' AND problem_id=?", (p.problem_id,))
    report.problems = db.upsert_problems(problems)
    with db.conn:
        # Retain old rows for saved-session history, outside the selected bank.
        db.conn.execute("UPDATE problems SET is_real=0 WHERE platform='atcoder' AND problem_id NOT IN (" + ",".join("?" for p in problems) + ")", [p.problem_id for p in problems])

    handle = config.atcoder_handle.strip()
    if not handle:
        report.warnings.append("no AtCoder handle configured; skipped submissions")
        return report

    from_second = (db.max_submission_epoch("atcoder") or -1) + 1
    imported: list[Submission] = []
    seen_ids: set[str] = set()
    while True:
        url = (
            f"{ATCODER_API}/user/submissions?user={quote(handle)}"
            f"&from_second={from_second}"
        )
        page = client.get_json(
            url,
            cache_key=f"atcoder-user-submissions-{handle}-{from_second}",
            ttl_seconds=0,
            force=True,
        )
        submissions = parse_atcoder_submissions(page)
        fresh = [s for s in submissions if s.submission_id not in seen_ids]
        imported.extend(fresh)
        seen_ids.update(s.submission_id for s in fresh)
        if len(submissions) < 500:
            break
        next_second = max(s.epoch_second for s in submissions) + 1
        if next_second <= from_second:
            report.warnings.append(
                "AtCoder submission pagination made no progress; stopped safely"
            )
            break
        from_second = next_second
    report.submissions = db.upsert_submissions(imported)
    return report
