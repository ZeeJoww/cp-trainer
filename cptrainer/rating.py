from __future__ import annotations

import math
from typing import Iterable, Sequence

from .types import PerformanceEstimate, ScoreResult, SessionProblemResult


DEFAULT_SCALE = 400.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def solve_probability(
    skill: float, difficulty: float, *, scale: float = DEFAULT_SCALE
) -> float:
    """Elo/logistic probability that a user solves a task in a rated session."""
    exponent = clamp((difficulty - skill) / scale, -20.0, 20.0)
    return 1.0 / (1.0 + 10.0**exponent)


def difficulty_for_probability(
    skill: float, probability: float, *, scale: float = DEFAULT_SCALE
) -> float:
    probability = clamp(probability, 1e-6, 1.0 - 1e-6)
    return skill + scale * math.log10((1.0 - probability) / probability)


def estimate_performance(
    difficulties: Sequence[float],
    outcomes: Sequence[float],
    *,
    scale: float = DEFAULT_SCALE,
) -> PerformanceEstimate:
    """
    Find performance r such that sum(P(solve | r, d_i)) equals observed solves.

    This is an item-response estimate, not an official CF/AtCoder rating formula.
    Outcomes may be in [0, 1], although normal contests use 0/1.
    """
    if len(difficulties) != len(outcomes):
        raise ValueError("difficulties and outcomes must have the same length")
    if not difficulties:
        raise ValueError("at least one rated problem is required")
    if any(not math.isfinite(x) for x in difficulties):
        raise ValueError("all difficulties must be finite")
    if any(y < 0.0 or y > 1.0 or not math.isfinite(y) for y in outcomes):
        raise ValueError("outcomes must be finite values in [0, 1]")

    observed = sum(outcomes)
    low = min(difficulties) - 1200.0
    high = max(difficulties) + 1200.0
    if observed <= 1e-12:
        return PerformanceEstimate(low, "<=")
    if observed >= len(outcomes) - 1e-12:
        return PerformanceEstimate(high, ">=")

    for _ in range(100):
        mid = (low + high) / 2.0
        expected = sum(solve_probability(mid, d, scale=scale) for d in difficulties)
        if expected < observed:
            low = mid
        else:
            high = mid
    return PerformanceEstimate((low + high) / 2.0)


def update_local_rating(
    old_rating: float,
    performance: float,
    problem_count: int,
    old_deviation: float = 120.0,
) -> tuple[float, float]:
    """
    Conservative local update.

    A session with six rated tasks receives about 22% weight. One session is
    capped at +/-140, preventing a tiny custom contest from dominating history.
    """
    if problem_count <= 0:
        return old_rating, old_deviation
    weight = 1.0 - math.exp(-problem_count / 24.0)
    # More uncertain local ratings adapt slightly faster, but never wildly.
    uncertainty_multiplier = clamp(old_deviation / 120.0, 0.75, 1.35)
    delta = (performance - old_rating) * weight * uncertainty_multiplier
    delta = clamp(delta, -140.0, 140.0)
    new_rating = old_rating + delta
    information = min(problem_count, 12) / 12.0
    new_deviation = max(55.0, old_deviation * math.sqrt(1.0 - 0.22 * information))
    return new_rating, new_deviation


def cf_score(
    items: Sequence[SessionProblemResult], duration_minutes: int
) -> ScoreResult:
    score = 0.0
    per_problem: list[dict[str, float | int | bool]] = []
    for item in items:
        earned = 0.0
        if item.solved:
            elapsed_minutes = (
                duration_minutes
                if item.elapsed_seconds is None
                else item.elapsed_seconds / 60.0
            )
            earned = max(
                0.3 * item.max_points,
                item.max_points
                - item.max_points / 250.0 * elapsed_minutes
                - 50.0 * item.wrong_attempts,
            )
            score += max(0.0, earned)
        per_problem.append(
            {
                "ordinal": item.ordinal,
                "solved": item.solved,
                "earned": round(max(0.0, earned), 2),
                "maximum": item.max_points,
                "wrong_attempts": item.wrong_attempts,
            }
        )
    return ScoreResult(round(score, 2), 0.0, {"problems": per_problem})


def atcoder_score(
    items: Sequence[SessionProblemResult], duration_minutes: int
) -> ScoreResult:
    score = sum(item.max_points for item in items if item.solved)
    solved_elapsed = [
        item.elapsed_seconds / 60.0
        for item in items
        if item.solved and item.elapsed_seconds is not None
    ]
    last_solve = max(solved_elapsed, default=0.0)
    wrong = sum(item.wrong_attempts for item in items if item.solved)
    penalty = last_solve + 5.0 * wrong if score > 0 else 0.0
    return ScoreResult(
        round(score, 2),
        round(penalty, 2),
        {"last_solve_minutes": last_solve, "wrong_attempts": wrong},
    )


def normalized_score(items: Sequence[SessionProblemResult]) -> ScoreResult:
    maximum = sum(item.max_points for item in items)
    earned = sum(item.max_points for item in items if item.solved)
    score = 0.0 if maximum <= 0 else 1000.0 * earned / maximum
    solved_elapsed = [
        item.elapsed_seconds / 60.0
        for item in items
        if item.solved and item.elapsed_seconds is not None
    ]
    penalty = max(solved_elapsed, default=0.0) + 5.0 * sum(
        item.wrong_attempts for item in items if item.solved
    )
    return ScoreResult(round(score, 2), round(penalty, 2))


def score_session(
    platform: str,
    items: Sequence[SessionProblemResult],
    duration_minutes: int,
) -> ScoreResult:
    if platform == "cf":
        return cf_score(items, duration_minutes)
    if platform == "atcoder":
        return atcoder_score(items, duration_minutes)
    return normalized_score(items)


def confidence_interval(
    rating: float, deviation: float, multiplier: float = 1.0
) -> tuple[float, float]:
    width = max(40.0, deviation) * multiplier
    return rating - width, rating + width
