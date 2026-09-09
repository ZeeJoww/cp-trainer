from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class Problem:
    platform: str
    problem_id: str
    contest_id: str
    index: str
    title: str
    difficulty: float | None
    points: float | None
    tags: tuple[str, ...]
    url: str
    solved_count: int = 0
    published_at: int | None = None
    is_real: bool = False


@dataclass(slots=True, frozen=True)
class Submission:
    platform: str
    submission_id: str
    problem_id: str
    epoch_second: int
    verdict: str
    language: str = ""


@dataclass(slots=True, frozen=True)
class Recommendation:
    problem: Problem
    target_difficulty: float
    predicted_solve_probability: float
    selection_score: float
    reason: str


@dataclass(slots=True)
class SessionProblemResult:
    ordinal: int
    problem: Problem
    target_difficulty: float
    max_points: float
    solved: bool = False
    elapsed_seconds: int | None = None
    wrong_attempts: int = 0


@dataclass(slots=True)
class ScoreResult:
    score: float
    penalty_minutes: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PerformanceEstimate:
    value: float
    censored: str | None = None
