from __future__ import annotations

from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterable, Iterator, Sequence

from .types import Problem, Submission


SCHEMA = r"""
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS profiles (
    platform TEXT PRIMARY KEY CHECK(platform IN ('cf', 'atcoder')),
    handle TEXT NOT NULL DEFAULT '',
    official_rating REAL NOT NULL,
    local_rating REAL NOT NULL,
    rating_deviation REAL NOT NULL DEFAULT 120,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    platform TEXT NOT NULL CHECK(platform IN ('cf', 'atcoder')),
    problem_id TEXT NOT NULL,
    contest_id TEXT NOT NULL DEFAULT '',
    problem_index TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    difficulty REAL,
    points REAL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL,
    solved_count INTEGER NOT NULL DEFAULT 0,
    source_updated_at INTEGER NOT NULL,
    PRIMARY KEY(platform, problem_id)
);
CREATE INDEX IF NOT EXISTS idx_problems_platform_difficulty
    ON problems(platform, difficulty);
CREATE INDEX IF NOT EXISTS idx_problems_contest
    ON problems(platform, contest_id);

CREATE TABLE IF NOT EXISTS submissions (
    platform TEXT NOT NULL CHECK(platform IN ('cf', 'atcoder')),
    submission_id TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    epoch_second INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(platform, submission_id)
);
CREATE INDEX IF NOT EXISTS idx_submissions_problem_time
    ON submissions(platform, problem_id, epoch_second);

CREATE TABLE IF NOT EXISTS problem_state (
    platform TEXT NOT NULL CHECK(platform IN ('cf', 'atcoder')),
    problem_id TEXT NOT NULL,
    solved INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_ac_epoch INTEGER,
    last_attempt_epoch INTEGER,
    PRIMARY KEY(platform, problem_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('list', 'contest')),
    platform TEXT NOT NULL CHECK(platform IN ('cf', 'atcoder', 'mixed')),
    mode TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    scheduled_at INTEGER,
    started_at INTEGER,
    finished_at INTEGER,
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'running', 'finished')),
    rated INTEGER NOT NULL DEFAULT 0,
    seed INTEGER NOT NULL,
    score REAL,
    penalty_minutes REAL,
    performance REAL,
    old_rating REAL,
    new_rating REAL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session_problems (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    platform TEXT NOT NULL CHECK(platform IN ('cf', 'atcoder')),
    problem_id TEXT NOT NULL,
    target_difficulty REAL NOT NULL,
    assigned_skill REAL NOT NULL,
    max_points REAL NOT NULL,
    solved INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds INTEGER,
    wrong_attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, ordinal),
    UNIQUE(session_id, platform, problem_id)
);
CREATE INDEX IF NOT EXISTS idx_session_problems_problem
    ON session_problems(platform, problem_id);

CREATE TABLE IF NOT EXISTS rating_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL CHECK(platform IN ('cf', 'atcoder')),
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    old_rating REAL NOT NULL,
    performance REAL NOT NULL,
    new_rating REAL NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def session_ended(session, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    return session["status"] == "finished" or (session["started_at"] is not None and session["started_at"] + session["duration_minutes"] * 60 <= now)


class Database:
    def __init__(self, path: Path, *, allow_demo: bool = False):
        self.path = path
        self.allow_demo = allow_demo
        self.offline_only = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise ValueError("数据库版本高于此程序，请使用新版 cp-trainer。")
        if version == 0 and self.path.stat().st_size:
            self.backup("before-v1")
        self.conn.executescript(SCHEMA)
        if version == 0:
            with self.transaction() as conn:
                conn.execute("ALTER TABLE problems ADD COLUMN published_at INTEGER")
                conn.execute("ALTER TABLE problems ADD COLUMN is_real INTEGER NOT NULL DEFAULT 0")
                conn.execute("""CREATE TABLE statements (
                    platform TEXT NOT NULL, problem_id TEXT NOT NULL,
                    html TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '', fetched_at INTEGER,
                    PRIMARY KEY(platform, problem_id),
                    FOREIGN KEY(platform, problem_id) REFERENCES problems(platform, problem_id)
                )""")
                conn.execute("PRAGMA user_version = 1")
        if version < 2:
            if version == 1:
                self.backup("before-v2")
            self.conn.executescript("""
                BEGIN;
                CREATE TABLE test_archives (
                    branch TEXT PRIMARY KEY, contest_id TEXT NOT NULL, title TEXT NOT NULL,
                    started_at INTEGER NOT NULL, revision TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    problem_count INTEGER NOT NULL DEFAULT 0, case_count INTEGER NOT NULL DEFAULT 0,
                    bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', downloaded_at INTEGER
                );
                CREATE TABLE judge_sets (
                    platform TEXT NOT NULL DEFAULT 'atcoder', problem_id TEXT NOT NULL,
                    branch TEXT NOT NULL REFERENCES test_archives(branch), directory TEXT NOT NULL,
                    case_count INTEGER NOT NULL, manifest_json TEXT NOT NULL,
                    checker TEXT NOT NULL, tolerance REAL, reason TEXT NOT NULL DEFAULT '',
                    time_limit_ms INTEGER NOT NULL, memory_limit_mb INTEGER NOT NULL,
                    PRIMARY KEY(platform, problem_id),
                    FOREIGN KEY(platform, problem_id) REFERENCES problems(platform, problem_id)
                );
                CREATE TABLE judge_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, problem_id TEXT NOT NULL,
                    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
                    language TEXT NOT NULL, source TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT 'QUEUED',
                    submitted_at INTEGER NOT NULL, finished_at INTEGER,
                    passed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX idx_judge_runs_problem ON judge_runs(platform, problem_id, id);
                PRAGMA user_version = 2;
                COMMIT;
            """)

    def backup(self, label: str = "manual") -> Path:
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        destination = folder / f"trainer-{label}-{time.time_ns()}.sqlite3"
        with closing(sqlite3.connect(destination)) as target:
            self.conn.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份完整性检查失败")
        return destination

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def initialize_profiles(
        self,
        *,
        cf_handle: str,
        atcoder_handle: str,
        cf_rating: float,
        atcoder_rating: float,
        reset_local: bool = False,
    ) -> None:
        now = int(time.time())
        with self.transaction() as conn:
            for platform, handle, rating in (
                ("cf", cf_handle, cf_rating),
                ("atcoder", atcoder_handle, atcoder_rating),
            ):
                existing = conn.execute(
                    "SELECT local_rating FROM profiles WHERE platform = ?", (platform,)
                ).fetchone()
                local = rating if existing is None or reset_local else existing["local_rating"]
                conn.execute(
                    """
                    INSERT INTO profiles(platform, handle, official_rating, local_rating,
                                         rating_deviation, updated_at)
                    VALUES(?, ?, ?, ?, 120, ?)
                    ON CONFLICT(platform) DO UPDATE SET
                        handle = excluded.handle,
                        official_rating = excluded.official_rating,
                        local_rating = CASE WHEN ? THEN excluded.local_rating
                                            ELSE profiles.local_rating END,
                        updated_at = excluded.updated_at
                    """,
                    (platform, handle, rating, local, now, int(reset_local)),
                )

    def update_profile(
        self,
        platform: str,
        *,
        handle: str | None = None,
        official_rating: float | None = None,
        local_rating: float | None = None,
        rating_deviation: float | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[object] = []
        if handle is not None:
            fields.append("handle = ?")
            values.append(handle)
        if official_rating is not None:
            fields.append("official_rating = ?")
            values.append(float(official_rating))
        if local_rating is not None:
            fields.append("local_rating = ?")
            values.append(float(local_rating))
        if rating_deviation is not None:
            fields.append("rating_deviation = ?")
            values.append(float(rating_deviation))
        fields.append("updated_at = ?")
        values.append(int(time.time()))
        values.append(platform)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE profiles SET {', '.join(fields)} WHERE platform = ?", values
            )
            if cur.rowcount == 0:
                raise KeyError(f"unknown profile platform: {platform}")

    def get_profile(self, platform: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM profiles WHERE platform = ?", (platform,)
        ).fetchone()
        if row is None:
            raise KeyError(f"profile is not initialized for {platform}")
        return row

    def profiles(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM profiles ORDER BY platform"))

    def upsert_problems(self, problems: Iterable[Problem]) -> int:
        now = int(time.time())
        rows = [
            (
                p.platform,
                p.problem_id,
                p.contest_id,
                p.index,
                p.title,
                p.difficulty,
                p.points,
                json.dumps(list(p.tags), ensure_ascii=False),
                p.url,
                p.solved_count,
                now,
                p.published_at,
                int(p.is_real),
            )
            for p in problems
        ]
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO problems(platform, problem_id, contest_id, problem_index,
                                     title, difficulty, points, tags_json, url,
                                     solved_count, source_updated_at, published_at, is_real)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, problem_id) DO UPDATE SET
                    contest_id = excluded.contest_id,
                    problem_index = excluded.problem_index,
                    title = excluded.title,
                    difficulty = COALESCE(excluded.difficulty, problems.difficulty),
                    points = COALESCE(excluded.points, problems.points),
                    tags_json = CASE WHEN excluded.tags_json = '[]'
                                     THEN problems.tags_json ELSE excluded.tags_json END,
                    url = excluded.url,
                    solved_count = MAX(excluded.solved_count, problems.solved_count),
                    source_updated_at = excluded.source_updated_at,
                    published_at = COALESCE(excluded.published_at, problems.published_at),
                    is_real = excluded.is_real
                """,
                rows,
            )
        return len(rows)

    def upsert_submissions(self, submissions: Iterable[Submission]) -> int:
        items = list(submissions)
        if not items:
            return 0
        rows = [
            (
                s.platform,
                s.submission_id,
                s.problem_id,
                s.epoch_second,
                s.verdict,
                s.language,
            )
            for s in items
        ]
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO submissions(platform, submission_id, problem_id,
                                        epoch_second, verdict, language)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, submission_id) DO UPDATE SET
                    problem_id = excluded.problem_id,
                    epoch_second = excluded.epoch_second,
                    verdict = excluded.verdict,
                    language = excluded.language
                """,
                rows,
            )
        self.rebuild_problem_state({s.platform for s in items})
        return len(items)

    def rebuild_problem_state(self, platforms: set[str] | None = None) -> None:
        platforms = platforms or {"cf", "atcoder"}
        for platform in platforms:
            ac = "OK" if platform == "cf" else "AC"
            rows = self.conn.execute(
                """
                SELECT problem_id,
                       COUNT(*) AS attempts,
                       MAX(CASE WHEN verdict = ? THEN 1 ELSE 0 END) AS solved,
                       MIN(CASE WHEN verdict = ? THEN epoch_second END) AS first_ac_epoch,
                       MAX(epoch_second) AS last_attempt_epoch
                FROM submissions
                WHERE platform = ?
                GROUP BY problem_id
                """,
                (ac, ac, platform),
            ).fetchall()
            with self.conn:
                self.conn.execute("DELETE FROM problem_state WHERE platform = ?", (platform,))
                self.conn.executemany(
                    """
                    INSERT INTO problem_state(platform, problem_id, solved, attempts,
                                              first_ac_epoch, last_attempt_epoch)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            platform,
                            r["problem_id"],
                            r["solved"],
                            r["attempts"],
                            r["first_ac_epoch"],
                            r["last_attempt_epoch"],
                        )
                        for r in rows
                    ],
                )

    def max_submission_id(self, platform: str) -> int | None:
        row = self.conn.execute(
            """
            SELECT MAX(CAST(submission_id AS INTEGER)) AS x
            FROM submissions WHERE platform = ?
            """,
            (platform,),
        ).fetchone()
        return None if row is None or row["x"] is None else int(row["x"])

    def max_submission_epoch(self, platform: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(epoch_second) AS x FROM submissions WHERE platform = ?",
            (platform,),
        ).fetchone()
        return None if row is None or row["x"] is None else int(row["x"])

    def problem_count(self, platform: str | None = None) -> int:
        clauses = [] if self.allow_demo else ["is_real=1"]
        if platform:
            clauses.append("platform=?")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return int(self.conn.execute("SELECT COUNT(*) FROM problems" + where, [platform] if platform else []).fetchone()[0])

    def solved_count(self, platform: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM problem_state WHERE platform = ? AND solved = 1",
                (platform,),
            ).fetchone()[0]
        )

    def get_problem(self, platform: str, problem_id: str) -> Problem:
        row = self.conn.execute(
            "SELECT * FROM problems WHERE platform = ? AND problem_id = ?",
            (platform, problem_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"problem not found: {platform}:{problem_id}")
        return self._row_to_problem(row)

    def candidate_problems(
        self,
        platform: str,
        *,
        minimum_difficulty: float | None = None,
        maximum_difficulty: float | None = None,
        minimum_solver_count: int = 0,
    ) -> list[Problem]:
        clauses = [
            "p.platform = ?",
            "p.difficulty IS NOT NULL",
            "COALESCE(s.solved, 0) = 0",
        ]
        values: list[object] = [platform]
        if not self.allow_demo:
            clauses.append("p.is_real = 1")
        if self.offline_only:
            clauses.append("EXISTS (SELECT 1 FROM statements a WHERE a.platform=p.platform AND a.problem_id=p.problem_id AND a.status='ready')")
        if minimum_difficulty is not None:
            clauses.append("p.difficulty >= ?")
            values.append(float(minimum_difficulty))
        if maximum_difficulty is not None:
            clauses.append("p.difficulty <= ?")
            values.append(float(maximum_difficulty))
        if minimum_solver_count > 0:
            clauses.append("p.solved_count >= ?")
            values.append(int(minimum_solver_count))
        rows = self.conn.execute(
            f"""
            SELECT p.*
            FROM problems p
            LEFT JOIN problem_state s
              ON s.platform = p.platform AND s.problem_id = p.problem_id
            WHERE {' AND '.join(clauses)}
            """,
            values,
        ).fetchall()
        return [self._row_to_problem(r) for r in rows]

    def recent_assignments(self, since_epoch: int) -> set[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT sp.platform, sp.problem_id
            FROM session_problems sp
            JOIN sessions s ON s.id = sp.session_id
            WHERE s.created_at >= ?
            """,
            (since_epoch,),
        )
        return {(r["platform"], r["problem_id"]) for r in rows}

    def completed_training_rows(self, platform: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT sp.solved, sp.platform, sp.problem_id, p.difficulty,
                       p.tags_json, sp.assigned_skill AS old_rating
                FROM session_problems sp
                JOIN sessions s ON s.id = sp.session_id
                JOIN problems p ON p.platform = sp.platform
                               AND p.problem_id = sp.problem_id
                WHERE s.status = 'finished'
                  AND sp.platform = ?
                  AND p.difficulty IS NOT NULL
                """,
                (platform,),
            )
        )

    def create_session(
        self,
        *,
        name: str,
        kind: str,
        platform: str,
        mode: str,
        duration_minutes: int,
        scheduled_at: int | None,
        rated: bool,
        seed: int,
        items: Sequence[tuple[str, str, float, float]],
    ) -> int:
        now = int(time.time())
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions(name, kind, platform, mode, duration_minutes,
                                     scheduled_at, status, rated, seed, created_at)
                VALUES(?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)
                """,
                (
                    name,
                    kind,
                    platform,
                    mode,
                    duration_minutes,
                    scheduled_at,
                    int(rated),
                    seed,
                    now,
                ),
            )
            session_id = int(cur.lastrowid)
            conn.executemany(
                """
                INSERT INTO session_problems(session_id, ordinal, platform,
                                             problem_id, target_difficulty,
                                             assigned_skill, max_points)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        i + 1,
                        p,
                        pid,
                        target,
                        float(conn.execute(
                            "SELECT local_rating FROM profiles WHERE platform = ?",
                            (p,),
                        ).fetchone()[0]),
                        points,
                    )
                    for i, (p, pid, target, points) in enumerate(items)
                ],
            )
        return session_id

    def list_sessions(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT s.*, COUNT(sp.ordinal) AS problem_count,
                       SUM(sp.solved) AS solved_count
                FROM sessions s
                LEFT JOIN session_problems sp ON sp.session_id = s.id
                GROUP BY s.id
                ORDER BY COALESCE(s.scheduled_at, s.created_at) DESC, s.id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def get_session(self, session_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        return row

    def get_session_items(self, session_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT sp.*, p.contest_id, p.problem_index, p.title,
                       p.difficulty, p.points, p.tags_json, p.url,
                       p.solved_count
                FROM session_problems sp
                JOIN problems p ON p.platform = sp.platform
                               AND p.problem_id = sp.problem_id
                WHERE sp.session_id = ?
                ORDER BY sp.ordinal
                """,
                (session_id,),
            )
        )

    def start_session(self, session_id: int, started_at: int | None = None) -> None:
        started_at = int(time.time()) if started_at is None else started_at
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE sessions
                SET status = 'running', started_at = COALESCE(started_at, ?)
                WHERE id = ? AND status = 'scheduled'
                """,
                (started_at, session_id),
            )
            if cur.rowcount == 0:
                row = self.get_session(session_id)
                if row["status"] != "running":
                    raise ValueError(f"session {session_id} is already {row['status']}")

    def schedule_session(self, session_id: int, scheduled_at: int | None) -> None:
        if self.get_session(session_id)["status"] != "scheduled":
            raise ValueError("比赛已开始，不能修改开始时间。")
        with self.conn:
            self.conn.execute("UPDATE sessions SET scheduled_at=? WHERE id=?", (scheduled_at, session_id))

    def clear_sessions(self) -> Path:
        backup = self.backup("before-clear")
        with self.conn:
            self.conn.execute("DELETE FROM sessions")
        return backup

    def record_result(
        self,
        session_id: int,
        ordinal: int,
        *,
        solved: bool,
        elapsed_seconds: int | None,
        wrong_attempts: int,
    ) -> None:
        if elapsed_seconds is not None and elapsed_seconds < 0:
            raise ValueError("elapsed time must be non-negative")
        if wrong_attempts < 0:
            raise ValueError("wrong attempts must be non-negative")
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE session_problems
                SET solved = ?, elapsed_seconds = ?, wrong_attempts = ?
                WHERE session_id = ? AND ordinal = ?
                """,
                (
                    int(solved),
                    elapsed_seconds,
                    wrong_attempts,
                    session_id,
                    ordinal,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"slot {ordinal} not found in session {session_id}")

    def infer_session_results(self, session_id: int) -> int:
        session = self.get_session(session_id)
        if session["started_at"] is None:
            raise ValueError("start the session before importing submissions")
        start = int(session["started_at"])
        duration = int(session["duration_minutes"]) * 60
        end = start + duration
        changed = 0
        for item in self.get_session_items(session_id):
            rows = self.conn.execute(
                """
                SELECT verdict, epoch_second
                FROM submissions
                WHERE platform = ? AND problem_id = ?
                  AND epoch_second BETWEEN ? AND ?
                ORDER BY epoch_second, CAST(submission_id AS INTEGER)
                """,
                (item["platform"], item["problem_id"], start, end),
            ).fetchall()
            ac_verdict = "OK" if item["platform"] == "cf" else "AC"
            ignored = {"COMPILATION_ERROR", "CE", "SKIPPED", "TESTING"}
            solved = False
            wrong = 0
            elapsed: int | None = None
            for row in rows:
                if row["verdict"] == ac_verdict:
                    solved = True
                    elapsed = int(row["epoch_second"]) - start
                    break
                if row["verdict"] not in ignored:
                    wrong += 1
            if rows:
                self.record_result(
                    session_id,
                    int(item["ordinal"]),
                    solved=solved,
                    elapsed_seconds=elapsed,
                    wrong_attempts=wrong,
                )
                changed += 1
        return changed

    def finish_session_row(
        self,
        session_id: int,
        *,
        score: float,
        penalty_minutes: float,
        performance: float | None,
        old_rating: float | None,
        new_rating: float | None,
        new_deviation: float | None = None,
        finished_at: int | None = None,
    ) -> None:
        finished_at = finished_at or int(time.time())
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE sessions
                SET status = 'finished', finished_at = ?, score = ?,
                    penalty_minutes = ?, performance = ?, old_rating = ?,
                    new_rating = ?
                WHERE id = ? AND status != 'finished'
                """,
                (
                    finished_at,
                    score,
                    penalty_minutes,
                    performance,
                    old_rating,
                    new_rating,
                    session_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("比赛已经结算")
            if new_rating is not None:
                platform = self.get_session(session_id)["platform"]
                cur = self.conn.execute("""UPDATE profiles SET local_rating=?,rating_deviation=?,updated_at=?
                    WHERE platform=? AND local_rating=?""", (new_rating, new_deviation, finished_at, platform, old_rating))
                if cur.rowcount != 1:
                    raise ValueError("训练分已被其他操作更新，请重试结算")
                self.conn.execute("""INSERT INTO rating_history(platform,session_id,source,old_rating,performance,new_rating,created_at)
                    VALUES(?,?,'local-contest',?,?,?,?)""", (platform, session_id, old_rating, performance, new_rating, finished_at))

    def add_rating_history(
        self,
        *,
        platform: str,
        session_id: int | None,
        source: str,
        old_rating: float,
        performance: float,
        new_rating: float,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO rating_history(platform, session_id, source,
                                           old_rating, performance, new_rating,
                                           created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    session_id,
                    source,
                    old_rating,
                    performance,
                    new_rating,
                    int(time.time()),
                ),
            )

    def rating_history(self, platform: str, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM rating_history
                WHERE platform = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (platform, limit),
            )
        )

    def _row_to_problem(self, row: sqlite3.Row) -> Problem:
        return Problem(
            platform=str(row["platform"]),
            problem_id=str(row["problem_id"]),
            contest_id=str(row["contest_id"]),
            index=str(row["problem_index"]),
            title=str(row["title"]),
            difficulty=None if row["difficulty"] is None else float(row["difficulty"]),
            points=None if row["points"] is None else float(row["points"]),
            tags=tuple(json.loads(row["tags_json"] or "[]")),
            url=str(row["url"]),
            solved_count=int(row["solved_count"] or 0),
            published_at=row["published_at"],
            is_real=bool(row["is_real"]),
        )
