from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
import random
import time
from typing import Iterable, Sequence

from .db import Database
from .rating import difficulty_for_probability, solve_probability
from .types import Problem, Recommendation


MODE_PROBABILITY_RANGES: dict[str, tuple[float, float]] = {
    "balanced": (0.86, 0.22),
    "growth": (0.68, 0.24),
    "speed": (0.94, 0.65),
    "review": (0.82, 0.45),
    "challenge": (0.45, 0.10),
}


def probability_slots(mode: str, count: int) -> list[float]:
    if mode not in MODE_PROBABILITY_RANGES:
        raise ValueError(f"unknown recommendation mode: {mode}")
    if count <= 0:
        return []
    high, low = MODE_PROBABILITY_RANGES[mode]
    if count == 1:
        return [(high + low) / 2.0]
    return [high + (low - high) * i / (count - 1) for i in range(count)]


def target_difficulties(
    skill: float,
    count: int,
    mode: str,
    *,
    platform: str,
) -> list[float]:
    targets = [difficulty_for_probability(skill, p) for p in probability_slots(mode, count)]
    grid = 100.0 if platform == "cf" else 50.0
    return [round(x / grid) * grid for x in targets]


def tag_weakness(db: Database, platform: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    counts: Counter[str] = Counter()
    for row in db.completed_training_rows(platform):
        difficulty = float(row["difficulty"])
        old_rating = float(row["old_rating"])
        expected = solve_probability(old_rating, difficulty)
        residual = expected - int(row["solved"])
        tags = json.loads(row["tags_json"] or "[]") or ["__untagged__"]
        for tag in tags:
            totals[str(tag)] += residual
            counts[str(tag)] += 1
    weakness: dict[str, float] = {}
    for tag, count in counts.items():
        # Shrink small samples strongly toward neutral.
        weakness[tag] = max(0.0, totals[tag] / (count + 4.0))
    return weakness


def _quality(problem: Problem) -> float:
    if problem.solved_count <= 0:
        return 0.25
    return min(1.0, math.log1p(problem.solved_count) / 11.0)


def _difficulty_fit(difficulty: float, target: float, sigma: float) -> float:
    z = (difficulty - target) / sigma
    return math.exp(-0.5 * z * z)


def select_problems(
    db: Database,
    *,
    platform: str,
    count: int,
    mode: str = "balanced",
    seed: int = 0,
    exclude_recent_days: int = 45,
    minimum_solver_count: int = 20,
    explicit_targets: Sequence[float] | None = None,
    extra_excluded: set[tuple[str, str]] | None = None,
) -> list[Recommendation]:
    if platform not in {"cf", "atcoder"}:
        raise ValueError("select_problems expects platform 'cf' or 'atcoder'")
    if count <= 0:
        return []
    profile = db.get_profile(platform)
    skill = float(profile["local_rating"])
    targets = list(explicit_targets) if explicit_targets is not None else target_difficulties(
        skill, count, mode, platform=platform
    )
    if len(targets) != count:
        raise ValueError("the number of explicit targets must equal count")

    recent_since = int(time.time()) - exclude_recent_days * 86400
    excluded = db.recent_assignments(recent_since)
    if extra_excluded:
        excluded |= set(extra_excluded)

    low = min(targets) - 500.0
    high = max(targets) + 500.0
    candidates = db.candidate_problems(
        platform,
        minimum_difficulty=low,
        maximum_difficulty=high,
        minimum_solver_count=minimum_solver_count,
    )
    candidates = [
        p for p in candidates if (p.platform, p.problem_id) not in excluded
    ]
    # AtCoder detailed metadata can temporarily be unavailable. Do not let a
    # missing solver_count field empty the entire bank.
    if len(candidates) < count:
        candidates = db.candidate_problems(
            platform,
            minimum_difficulty=low,
            maximum_difficulty=high,
            minimum_solver_count=0,
        )
        candidates = [
            p for p in candidates if (p.platform, p.problem_id) not in excluded
        ]
    if len(candidates) < count:
        candidates = db.candidate_problems(platform, minimum_solver_count=0)
        candidates = [
            p for p in candidates if (p.platform, p.problem_id) not in excluded
        ]
    if len(candidates) < count:
        raise ValueError(
            f"only {len(candidates)} eligible unsolved {platform} problems are available; "
            "synchronize a larger problem bank or reduce the count"
        )

    weakness = tag_weakness(db, platform)
    rng = random.Random(seed)
    jitter = {
        (p.platform, p.problem_id): rng.random() * 0.05 for p in candidates
    }
    selected: list[Recommendation] = []
    selected_keys: set[tuple[str, str]] = set()
    selected_contests: Counter[str] = Counter()
    selected_tags: Counter[str] = Counter()
    sigma = 130.0 if platform == "cf" else 150.0

    for target in targets:
        best: tuple[float, Problem, float, list[str]] | None = None
        for p in candidates:
            key = (p.platform, p.problem_id)
            if key in selected_keys or p.difficulty is None:
                continue
            fit = _difficulty_fit(p.difficulty, target, sigma)
            quality = _quality(p)
            tags = list(p.tags) or ["__untagged__"]
            weak = max((weakness.get(tag, 0.0) for tag in tags), default=0.0)
            overlap = sum(selected_tags[tag] for tag in tags) / max(1, len(tags))
            diversity_penalty = min(0.45, 0.08 * overlap)
            contest_penalty = 0.70 if selected_contests[p.contest_id] else 0.0
            weak_weight = 1.35 if mode == "review" else 0.65
            score = (
                4.0 * fit
                + 0.30 * quality
                + weak_weight * weak
                - diversity_penalty
                - contest_penalty
                + jitter[key]
            )
            weak_tags = [
                tag for tag in tags if tag != "__untagged__" and weakness.get(tag, 0.0) > 0.04
            ]
            if best is None or score > best[0]:
                best = (score, p, weak, weak_tags)
        if best is None:
            raise RuntimeError("candidate selection unexpectedly ran out of problems")
        score, problem, weak, weak_tags = best
        selected_keys.add((problem.platform, problem.problem_id))
        selected_contests[problem.contest_id] += 1
        selected_tags.update(problem.tags or ("__untagged__",))
        probability = solve_probability(skill, float(problem.difficulty))
        reason_bits = [
            f"target {target:.0f}",
            f"predicted solve chance {probability:.0%}",
        ]
        if weak_tags:
            reason_bits.append("weak-area signal: " + ", ".join(weak_tags[:2]))
        elif _quality(problem) > 0.75:
            reason_bits.append("well-tested archive problem")
        selected.append(
            Recommendation(
                problem=problem,
                target_difficulty=target,
                predicted_solve_probability=probability,
                selection_score=score,
                reason="; ".join(reason_bits),
            )
        )
    return selected
