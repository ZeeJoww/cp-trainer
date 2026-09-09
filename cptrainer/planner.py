from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
import json
import random
from typing import Sequence
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database
from .rating import estimate_performance, score_session, update_local_rating
from .recommend import select_problems
from .types import Problem, Recommendation, SessionProblemResult


CF_POINTS = (500, 750, 1000, 1250, 1750, 2250, 3000, 3500)
ATCODER_POINTS = (100, 200, 300, 400, 500, 600, 700, 800)


@dataclass(slots=True)
class FinishResult:
    session_id: int
    score: float
    penalty_minutes: float
    performance: float | None
    performance_censored: str | None
    old_rating: float | None
    new_rating: float | None


def _point_profile(platform: str, count: int) -> list[float]:
    base = CF_POINTS if platform == "cf" else ATCODER_POINTS
    if count <= len(base):
        return [float(x) for x in base[:count]]
    result = [float(x) for x in base]
    step = 500.0 if platform == "cf" else 100.0
    while len(result) < count:
        result.append(result[-1] + step)
    return result


def create_problem_list(
    db: Database,
    *,
    platform: str,
    count: int,
    name: str,
    mode: str,
    seed: int,
    scheduled_at: int | None = None,
    exclude_recent_days: int = 45,
    minimum_solver_count: int = 20,
) -> tuple[int, list[Recommendation]]:
    recommendations = select_problems(
        db,
        platform=platform,
        count=count,
        mode=mode,
        seed=seed,
        exclude_recent_days=exclude_recent_days,
        minimum_solver_count=minimum_solver_count,
    )
    items = [
        (r.problem.platform, r.problem.problem_id, r.target_difficulty, 100.0)
        for r in recommendations
    ]
    session_id = db.create_session(
        name=name,
        kind="list",
        platform=platform,
        mode=mode,
        duration_minutes=90,
        scheduled_at=scheduled_at,
        rated=False,
        seed=seed,
        items=items,
    )
    return session_id, recommendations


def create_mixed_problem_list(
    db: Database,
    *,
    count: int,
    name: str,
    mode: str,
    seed: int,
    cf_share: float,
    scheduled_at: int | None = None,
    exclude_recent_days: int = 45,
    minimum_solver_count: int = 20,
) -> tuple[int, list[Recommendation]]:
    if count < 2:
        raise ValueError("a mixed list needs at least two problems")
    cf_count = max(1, min(count - 1, round(count * cf_share)))
    atcoder_count = count - cf_count
    cf = select_problems(
        db,
        platform="cf",
        count=cf_count,
        mode=mode,
        seed=seed * 2 + 1,
        exclude_recent_days=exclude_recent_days,
        minimum_solver_count=minimum_solver_count,
    )
    atcoder = select_problems(
        db,
        platform="atcoder",
        count=atcoder_count,
        mode=mode,
        seed=seed * 2 + 2,
        exclude_recent_days=exclude_recent_days,
        minimum_solver_count=minimum_solver_count,
    )
    recommendations: list[Recommendation] = []
    for i in range(max(len(cf), len(atcoder))):
        if i < len(cf):
            recommendations.append(cf[i])
        if i < len(atcoder):
            recommendations.append(atcoder[i])
    items = [
        (r.problem.platform, r.problem.problem_id, r.target_difficulty, 100.0)
        for r in recommendations
    ]
    session_id = db.create_session(
        name=name,
        kind="list",
        platform="mixed",
        mode=mode,
        duration_minutes=90,
        scheduled_at=scheduled_at,
        rated=False,
        seed=seed,
        items=items,
    )
    return session_id, recommendations


def contest_probability_slots(platform: str, count: int) -> list[float]:
    if count <= 0:
        return []
    if platform == "cf":
        anchors = [0.92, 0.78, 0.60, 0.42, 0.25, 0.12]
    else:
        anchors = [0.90, 0.72, 0.50, 0.30, 0.14]
    if count == len(anchors):
        return anchors
    if count == 1:
        return [0.50]
    # Interpolate from a warm-up task to a genuine stretch task.
    return [anchors[0] + (anchors[-1] - anchors[0]) * i / (count - 1) for i in range(count)]


def create_training_contest(
    db: Database,
    *,
    platform: str,
    count: int,
    duration_minutes: int,
    name: str,
    seed: int,
    rated: bool = True,
    scheduled_at: int | None = None,
    exclude_recent_days: int = 45,
    minimum_solver_count: int = 20,
) -> tuple[int, list[Recommendation]]:
    if platform not in {"cf", "atcoder"}:
        raise ValueError("rated training contests must use one platform")
    skill = float(db.get_profile(platform)["local_rating"])
    probabilities = contest_probability_slots(platform, count)
    from .rating import difficulty_for_probability

    grid = 100.0 if platform == "cf" else 50.0
    targets = [
        round(difficulty_for_probability(skill, p) / grid) * grid
        for p in probabilities
    ]
    recommendations = select_problems(
        db,
        platform=platform,
        count=count,
        mode="balanced",
        seed=seed,
        exclude_recent_days=exclude_recent_days,
        minimum_solver_count=minimum_solver_count,
        explicit_targets=targets,
    )
    points = _point_profile(platform, count)
    items = [
        (r.problem.platform, r.problem.problem_id, r.target_difficulty, points[i])
        for i, r in enumerate(recommendations)
    ]
    session_id = db.create_session(
        name=name,
        kind="contest",
        platform=platform,
        mode="balanced",
        duration_minutes=duration_minutes,
        scheduled_at=scheduled_at,
        rated=rated,
        seed=seed,
        items=items,
    )
    return session_id, recommendations


def _scheduled_epoch(day: date, hour: int, timezone: str) -> int:
    tz = ZoneInfo(timezone)
    return int(datetime.combine(day, dt_time(hour=hour), tzinfo=tz).timestamp())


def _allocate_weekly_budget(budget: int) -> tuple[int, int, int, int, int]:
    if budget < 12:
        raise ValueError("weekly budget must be at least 12 for two useful contests")
    cf_contest = min(6, max(4, round(budget * 0.30)))
    atcoder_contest = min(5, max(4, round(budget * 0.25)))
    remaining = budget - cf_contest - atcoder_contest
    if remaining < 3:
        # Prefer preserving at least one task in each drill session.
        take = 3 - remaining
        while take and cf_contest > 4:
            cf_contest -= 1
            take -= 1
        while take and atcoder_contest > 4:
            atcoder_contest -= 1
            take -= 1
        remaining = budget - cf_contest - atcoder_contest
    a = remaining // 3
    b = remaining // 3
    c = remaining - a - b
    return a, b, c, cf_contest, atcoder_contest


def create_weekly_plan(
    db: Database,
    config: Config,
    *,
    start: date,
    budget: int | None = None,
    seed: int | None = None,
) -> list[int]:
    budget = budget or config.weekly_problem_budget
    seed = config.random_seed if seed is None else seed
    cf_drill, atc_drill, mixed_review, cf_contest, atc_contest = _allocate_weekly_budget(budget)
    plan: list[int] = []

    if cf_drill:
        sid, _ = create_problem_list(
            db,
            platform="cf",
            count=cf_drill,
            name="CF growth drill",
            mode="growth",
            seed=seed + 11,
            scheduled_at=_scheduled_epoch(start, 20, config.timezone),
            exclude_recent_days=config.exclude_recent_days,
            minimum_solver_count=config.min_solver_count,
        )
        plan.append(sid)
    if atc_drill:
        sid, _ = create_problem_list(
            db,
            platform="atcoder",
            count=atc_drill,
            name="AtCoder growth drill",
            mode="growth",
            seed=seed + 22,
            scheduled_at=_scheduled_epoch(start + timedelta(days=1), 20, config.timezone),
            exclude_recent_days=config.exclude_recent_days,
            minimum_solver_count=config.min_solver_count,
        )
        plan.append(sid)
    if mixed_review >= 2:
        sid, _ = create_mixed_problem_list(
            db,
            count=mixed_review,
            name="Cross-platform weakness review",
            mode="review",
            seed=seed + 33,
            cf_share=config.cf_share,
            scheduled_at=_scheduled_epoch(start + timedelta(days=2), 20, config.timezone),
            exclude_recent_days=config.exclude_recent_days,
            minimum_solver_count=config.min_solver_count,
        )
        plan.append(sid)
    elif mixed_review == 1:
        sid, _ = create_problem_list(
            db,
            platform="cf" if config.cf_share >= 0.5 else "atcoder",
            count=1,
            name="Weakness review",
            mode="review",
            seed=seed + 33,
            scheduled_at=_scheduled_epoch(start + timedelta(days=2), 20, config.timezone),
            exclude_recent_days=config.exclude_recent_days,
            minimum_solver_count=config.min_solver_count,
        )
        plan.append(sid)

    sid, _ = create_training_contest(
        db,
        platform="cf",
        count=cf_contest,
        duration_minutes=120,
        name="Generated CF-rated training contest",
        seed=seed + 44,
        rated=True,
        scheduled_at=_scheduled_epoch(start + timedelta(days=4), 20, config.timezone),
        exclude_recent_days=config.exclude_recent_days,
        minimum_solver_count=config.min_solver_count,
    )
    plan.append(sid)

    sid, _ = create_training_contest(
        db,
        platform="atcoder",
        count=atc_contest,
        duration_minutes=120,
        name="Generated AtCoder-rated training contest",
        seed=seed + 55,
        rated=True,
        scheduled_at=_scheduled_epoch(start + timedelta(days=5), 20, config.timezone),
        exclude_recent_days=config.exclude_recent_days,
        minimum_solver_count=config.min_solver_count,
    )
    plan.append(sid)
    return plan


def _problem_from_session_row(row) -> Problem:
    return Problem(
        platform=str(row["platform"]),
        problem_id=str(row["problem_id"]),
        contest_id=str(row["contest_id"]),
        index=str(row["problem_index"]),
        title=str(row["title"]),
        difficulty=float(row["difficulty"]) if row["difficulty"] is not None else None,
        points=float(row["points"]) if row["points"] is not None else None,
        tags=tuple(json.loads(row["tags_json"] or "[]")),
        url=str(row["url"]),
        solved_count=int(row["solved_count"] or 0),
    )


def finish_session(db: Database, session_id: int) -> FinishResult:
    session = db.get_session(session_id)
    if session["status"] == "finished":
        raise ValueError(f"session {session_id} is already finished")
    rows = db.get_session_items(session_id)
    items = [
        SessionProblemResult(
            ordinal=int(row["ordinal"]),
            problem=_problem_from_session_row(row),
            target_difficulty=float(row["target_difficulty"]),
            max_points=float(row["max_points"]),
            solved=bool(row["solved"]),
            elapsed_seconds=(
                None if row["elapsed_seconds"] is None else int(row["elapsed_seconds"])
            ),
            wrong_attempts=int(row["wrong_attempts"]),
        )
        for row in rows
    ]
    score = score_session(
        str(session["platform"]), items, int(session["duration_minutes"])
    )

    performance: float | None = None
    censored: str | None = None
    old_rating: float | None = None
    new_rating: float | None = None
    new_deviation: float | None = None
    if bool(session["rated"]) and session["platform"] in {"cf", "atcoder"}:
        rated_items = [x for x in items if x.problem.difficulty is not None]
        if not rated_items:
            raise ValueError("this session has no problems with difficulty estimates")
        estimate = estimate_performance(
            [float(x.problem.difficulty) for x in rated_items],
            [1.0 if x.solved else 0.0 for x in rated_items],
        )
        performance = estimate.value
        censored = estimate.censored
        profile = db.get_profile(str(session["platform"]))
        old_rating = float(profile["local_rating"])
        new_rating, new_deviation = update_local_rating(
            old_rating,
            performance,
            len(rated_items),
            float(profile["rating_deviation"]),
        )

    db.finish_session_row(
        session_id,
        score=score.score,
        penalty_minutes=score.penalty_minutes,
        performance=performance,
        old_rating=old_rating,
        new_rating=new_rating,
        new_deviation=new_deviation,
    )
    return FinishResult(
        session_id=session_id,
        score=score.score,
        penalty_minutes=score.penalty_minutes,
        performance=performance,
        performance_censored=censored,
        old_rating=old_rating,
        new_rating=new_rating,
    )
