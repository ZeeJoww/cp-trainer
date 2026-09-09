from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import tomllib


@dataclass(slots=True)
class Config:
    cf_handle: str = ""
    atcoder_handle: str = ""
    cf_official_rating: float = 2274.0
    atcoder_official_rating: float = 1860.0
    timezone: str = "Asia/Taipei"
    weekly_problem_budget: int = 20
    cf_share: float = 0.55
    exclude_recent_days: int = 45
    min_solver_count: int = 20
    random_seed: int = 20260901

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise FileNotFoundError(
                f"configuration does not exist: {path}. Run `cp-trainer init` first."
            )
        with path.open("rb") as f:
            raw = tomllib.load(f)
        profile = raw.get("profile", {})
        training = raw.get("training", {})
        return cls(
            cf_handle=str(profile.get("cf_handle", "")),
            atcoder_handle=str(profile.get("atcoder_handle", "")),
            cf_official_rating=float(profile.get("cf_official_rating", 2274)),
            atcoder_official_rating=float(profile.get("atcoder_official_rating", 1860)),
            timezone=str(profile.get("timezone", "Asia/Taipei")),
            weekly_problem_budget=int(training.get("weekly_problem_budget", 20)),
            cf_share=float(training.get("cf_share", 0.55)),
            exclude_recent_days=int(training.get("exclude_recent_days", 45)),
            min_solver_count=int(training.get("min_solver_count", 20)),
            random_seed=int(training.get("random_seed", 20260901)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = f'''[profile]
cf_handle = {json.dumps(self.cf_handle)}
atcoder_handle = {json.dumps(self.atcoder_handle)}
cf_official_rating = {self.cf_official_rating:g}
atcoder_official_rating = {self.atcoder_official_rating:g}
timezone = {json.dumps(self.timezone)}

[training]
weekly_problem_budget = {self.weekly_problem_budget}
cf_share = {self.cf_share:g}
exclude_recent_days = {self.exclude_recent_days}
min_solver_count = {self.min_solver_count}
random_seed = {self.random_seed}
'''
        path.write_text(text, encoding="utf-8")


@dataclass(slots=True, frozen=True)
class Paths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def database(self) -> Path:
        return self.home / "trainer.sqlite3"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def exports(self) -> Path:
        return self.home / "exports"

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.exports.mkdir(parents=True, exist_ok=True)
