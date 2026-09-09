from __future__ import annotations

import random
import time

from .db import Database
from .types import Problem, Submission


CF_TAGS = (
    "binary search",
    "data structures",
    "dp",
    "graphs",
    "greedy",
    "math",
    "number theory",
    "strings",
    "trees",
    "two pointers",
)


def seed_demo_bank(db: Database, seed: int = 20260901) -> tuple[int, int]:
    rng = random.Random(seed)
    problems: list[Problem] = []
    for i in range(420):
        difficulty = 900 + (i % 25) * 100
        contest = 9000 + i // 6
        index = chr(ord("A") + i % 6)
        tags = tuple(rng.sample(CF_TAGS, k=1 + (i % 2)))
        problems.append(
            Problem(
                platform="cf",
                problem_id=f"{contest}{index}",
                contest_id=str(contest),
                index=index,
                title=f"Demo CF {difficulty} #{i + 1}",
                difficulty=float(difficulty),
                points=float((500, 750, 1000, 1250, 1750, 2250)[i % 6]),
                tags=tags,
                url="https://codeforces.com/problemset",
                solved_count=max(20, 200000 // (1 + i % 100)),
            )
        )
    for i in range(360):
        difficulty = 600 + (i % 29) * 100
        contest = f"demo{i // 7:03d}"
        index = chr(ord("A") + i % 7)
        problem_id = f"{contest}_{index.lower()}"
        problems.append(
            Problem(
                platform="atcoder",
                problem_id=problem_id,
                contest_id=contest,
                index=index,
                title=f"Demo AtCoder {difficulty} #{i + 1}",
                difficulty=float(difficulty),
                points=float((100, 200, 300, 400, 500, 600, 700)[i % 7]),
                tags=(),
                url="https://atcoder.jp/contests/",
                solved_count=max(20, 120000 // (1 + i % 90)),
            )
        )
    count = db.upsert_problems(problems)

    now = int(time.time())
    submissions: list[Submission] = []
    sid = 1
    for p in problems:
        threshold = 1950 if p.platform == "cf" else 1450
        if p.difficulty is not None and p.difficulty <= threshold and rng.random() < 0.35:
            submissions.append(
                Submission(
                    platform=p.platform,
                    submission_id=str(10_000_000 + sid),
                    problem_id=p.problem_id,
                    epoch_second=now - rng.randint(10_000, 10_000_000),
                    verdict="OK" if p.platform == "cf" else "AC",
                    language="C++",
                )
            )
            sid += 1
    db.upsert_submissions(submissions)
    return count, len(submissions)
