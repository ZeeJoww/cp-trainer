from datetime import date
from pathlib import Path
import tempfile
import unittest

from cptrainer.config import Config
from cptrainer.db import Database
from cptrainer.demo import seed_demo_bank
from cptrainer.planner import create_training_contest, create_weekly_plan, finish_session
from cptrainer.recommend import select_problems


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3", allow_demo=True)
        self.db.migrate()
        self.db.initialize_profiles(
            cf_handle="",
            atcoder_handle="",
            cf_rating=2274,
            atcoder_rating=1860,
        )
        seed_demo_bank(self.db, 1)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_recommendations_are_unique_and_near_target(self):
        recs = select_problems(
            self.db,
            platform="cf",
            count=8,
            mode="growth",
            seed=2,
            exclude_recent_days=0,
            minimum_solver_count=0,
        )
        self.assertEqual(len(recs), 8)
        self.assertEqual(len({r.problem.problem_id for r in recs}), 8)
        for r in recs:
            self.assertLess(abs(r.problem.difficulty - r.target_difficulty), 350)

    def test_contest_finish_updates_rating(self):
        sid, recs = create_training_contest(
            self.db,
            platform="cf",
            count=6,
            duration_minutes=120,
            name="test",
            seed=3,
            exclude_recent_days=0,
            minimum_solver_count=0,
        )
        self.db.start_session(sid, started_at=100000)
        for slot in range(1, 5):
            self.db.record_result(
                sid,
                slot,
                solved=True,
                elapsed_seconds=slot * 900,
                wrong_attempts=0,
            )
        result = finish_session(self.db, sid)
        self.assertIsNotNone(result.performance)
        self.assertIsNotNone(result.new_rating)
        self.assertEqual(self.db.get_session(sid)["status"], "finished")

    def test_weekly_plan_uses_exact_budget(self):
        config = Config(weekly_problem_budget=20, exclude_recent_days=0, min_solver_count=0)
        ids = create_weekly_plan(self.db, config, start=date(2026, 9, 7), budget=20, seed=4)
        count = sum(len(self.db.get_session_items(sid)) for sid in ids)
        self.assertEqual(count, 20)
        self.assertEqual(len(ids), 5)


if __name__ == "__main__":
    unittest.main()
