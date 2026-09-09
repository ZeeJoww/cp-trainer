import json
from pathlib import Path
import unittest

from cptrainer.sources import (
    parse_atcoder_problems,
    parse_atcoder_submissions,
    parse_codeforces_problemset,
    parse_codeforces_submissions,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SourceParserTests(unittest.TestCase):
    def test_codeforces_problemset(self):
        problems = parse_codeforces_problemset(load("cf_problemset.json"))
        self.assertEqual(len(problems), 3)
        self.assertEqual(problems[0].problem_id, "100A")
        self.assertEqual(problems[1].difficulty, 2200)
        self.assertEqual(problems[1].solved_count, 1200)

    def test_codeforces_submissions(self):
        submissions = parse_codeforces_submissions(load("cf_submissions.json"))
        self.assertEqual([s.verdict for s in submissions], ["WRONG_ANSWER", "OK"])
        self.assertEqual(submissions[0].problem_id, "100A")

    def test_atcoder_problem_models(self):
        problems = parse_atcoder_problems(
            load("atcoder_problems.json"),
            load("atcoder_models.json"),
            load("atcoder_merged.json"),
        )
        self.assertEqual(len(problems), 2)
        self.assertEqual(problems[0].difficulty, 1701)
        self.assertEqual(problems[1].points, 400)
        self.assertEqual(problems[0].solved_count, 5000)

    def test_atcoder_submissions(self):
        submissions = parse_atcoder_submissions(load("atcoder_submissions.json"))
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[-1].verdict, "AC")


if __name__ == "__main__":
    unittest.main()
