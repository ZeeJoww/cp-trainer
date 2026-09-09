from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import sys
import textwrap
import webbrowser
from zoneinfo import ZoneInfo

from .config import Config, Paths
from .db import Database
from .http_client import CachedHttpClient, HttpClientError
from .planner import (
    create_mixed_problem_list,
    create_problem_list,
    create_training_contest,
    create_weekly_plan,
    finish_session,
)
from .recommend import select_problems
from .render import export_plan_index, export_session_html, export_session_markdown
from .sources import sync_atcoder, sync_codeforces


def _default_home() -> Path:
    env = os.environ.get("CP_TRAINER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".cp-trainer"


def _table(headers: list[str], rows: list[list[object]]) -> str:
    text_rows = [[str(x) for x in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in text_rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))
    widths = [min(w, 48) for w in widths]

    def cut(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        return value[: max(1, width - 1)] + "…"

    line = "  ".join("-" * w for w in widths)
    output = [
        "  ".join(cut(h, widths[i]).ljust(widths[i]) for i, h in enumerate(headers)),
        line,
    ]
    for row in text_rows:
        output.append(
            "  ".join(cut(value, widths[i]).ljust(widths[i]) for i, value in enumerate(row))
        )
    return "\n".join(output)


def _fmt_epoch(value: int | None, timezone: str) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7)


def _slot_number(value: str) -> int:
    value = value.strip().upper()
    if value.isdigit():
        result = int(value)
    elif len(value) == 1 and "A" <= value <= "Z":
        result = ord(value) - ord("A") + 1
    else:
        raise argparse.ArgumentTypeError("slot must be a positive number or A..Z")
    if result <= 0:
        raise argparse.ArgumentTypeError("slot must be positive")
    return result


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(Path(args.home).expanduser().resolve())


def _open_state(args: argparse.Namespace) -> tuple[Paths, Config, Database]:
    paths = _paths(args)
    config = Config.load(paths.config)
    paths.ensure()
    db = Database(paths.database)
    db.migrate()
    return paths, config, db


def cmd_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if paths.config.exists() and not args.force:
        print(f"Configuration already exists: {paths.config}", file=sys.stderr)
        print("Use --force to overwrite configuration without deleting training data.", file=sys.stderr)
        return 2
    paths.ensure()
    config = Config(
        cf_handle=args.cf_handle or "",
        atcoder_handle=args.atcoder_handle or "",
        cf_official_rating=args.cf_rating,
        atcoder_official_rating=args.atcoder_rating,
        timezone=args.timezone,
        weekly_problem_budget=args.weekly_budget,
        cf_share=args.cf_share,
        exclude_recent_days=args.exclude_recent_days,
        min_solver_count=args.min_solver_count,
        random_seed=args.seed,
    )
    config.save(paths.config)
    db = Database(paths.database)
    try:
        db.migrate()
        db.initialize_profiles(
            cf_handle=config.cf_handle,
            atcoder_handle=config.atcoder_handle,
            cf_rating=config.cf_official_rating,
            atcoder_rating=config.atcoder_official_rating,
            reset_local=args.reset_local,
        )
    finally:
        db.close()
    print(f"Initialized local trainer at {paths.home}")
    print(f"CF: {config.cf_official_rating:.0f}; AtCoder: {config.atcoder_official_rating:.0f}")
    if not config.cf_handle or not config.atcoder_handle:
        print("Handles are optional; set them later with `cp-trainer profile` to import solves.")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        if args.platform is None:
            rows = []
            for p in db.profiles():
                rows.append(
                    [
                        p["platform"],
                        p["handle"] or "—",
                        f"{p['official_rating']:.0f}",
                        f"{p['local_rating']:.1f}",
                        f"±{p['rating_deviation']:.0f}",
                    ]
                )
            print(_table(["site", "handle", "official", "local", "uncertainty"], rows))
            return 0
        platform = args.platform
        if args.handle is not None:
            if platform == "cf":
                config.cf_handle = args.handle
            else:
                config.atcoder_handle = args.handle
        if args.official_rating is not None:
            if platform == "cf":
                config.cf_official_rating = args.official_rating
            else:
                config.atcoder_official_rating = args.official_rating
        config.save(paths.config)
        db.update_profile(
            platform,
            handle=args.handle,
            official_rating=args.official_rating,
            local_rating=args.local_rating,
            rating_deviation=(120.0 if args.reset_uncertainty else None),
        )
        p = db.get_profile(platform)
        print(
            f"{platform}: handle={p['handle'] or '—'}, official={p['official_rating']:.0f}, "
            f"local={p['local_rating']:.1f} ±{p['rating_deviation']:.0f}"
        )
        return 0
    finally:
        db.close()


def _run_sync(
    db: Database, config: Config, paths: Paths, platform: str, force: bool
) -> int:
    client = CachedHttpClient(paths.cache)
    reports = []
    if platform in {"all", "cf"}:
        reports.append(sync_codeforces(db, config, client, force=force))
    if platform in {"all", "atcoder"}:
        reports.append(sync_atcoder(db, config, client, force=force))
    for report in reports:
        rating = "" if report.official_rating is None else f", official rating {report.official_rating:.0f}"
        print(
            f"{report.platform}: {report.problems} problem records, "
            f"{report.submissions} new submissions{rating}"
        )
        for warning in report.warnings or []:
            print(f"  warning: {warning}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        return _run_sync(db, config, paths, args.platform, args.force)
    except (HttpClientError, ValueError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def _print_recommendations(recommendations) -> None:
    rows = []
    for i, r in enumerate(recommendations, 1):
        tags = ",".join(r.problem.tags[:3]) or "—"
        rows.append(
            [
                i,
                r.problem.platform,
                r.problem.problem_id,
                f"{r.problem.difficulty:.0f}",
                f"{r.target_difficulty:.0f}",
                f"{r.predicted_solve_probability:.0%}",
                r.problem.title,
                tags,
                r.problem.url,
            ]
        )
    print(
        _table(
            ["#", "site", "id", "diff", "target", "P(solve)", "title", "tags", "url"],
            rows,
        )
    )


def cmd_recommend(args: argparse.Namespace) -> int:
    _, config, db = _open_state(args)
    try:
        if db.problem_count() == 0:
            print("Problem bank is empty. Run `cp-trainer sync` or `cp-trainer demo` first.", file=sys.stderr)
            return 2
        if args.platform == "mixed":
            if args.count < 2:
                raise ValueError("mixed recommendations need at least two problems")
            cf_count = max(1, min(args.count - 1, round(args.count * config.cf_share)))
            atc_count = args.count - cf_count
            recommendations = []
            cf = select_problems(
                db,
                platform="cf",
                count=cf_count,
                mode=args.mode,
                seed=args.seed,
                exclude_recent_days=config.exclude_recent_days,
                minimum_solver_count=config.min_solver_count,
            )
            atc = select_problems(
                db,
                platform="atcoder",
                count=atc_count,
                mode=args.mode,
                seed=args.seed + 1,
                exclude_recent_days=config.exclude_recent_days,
                minimum_solver_count=config.min_solver_count,
            )
            for i in range(max(len(cf), len(atc))):
                if i < len(cf):
                    recommendations.append(cf[i])
                if i < len(atc):
                    recommendations.append(atc[i])
        else:
            recommendations = select_problems(
                db,
                platform=args.platform,
                count=args.count,
                mode=args.mode,
                seed=args.seed,
                exclude_recent_days=config.exclude_recent_days,
                minimum_solver_count=config.min_solver_count,
            )
        _print_recommendations(recommendations)
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_make_list(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        if args.platform == "mixed":
            sid, recs = create_mixed_problem_list(
                db,
                count=args.count,
                name=args.name,
                mode=args.mode,
                seed=args.seed,
                cf_share=config.cf_share,
                exclude_recent_days=config.exclude_recent_days,
                minimum_solver_count=config.min_solver_count,
            )
        else:
            sid, recs = create_problem_list(
                db,
                platform=args.platform,
                count=args.count,
                name=args.name,
                mode=args.mode,
                seed=args.seed,
                exclude_recent_days=config.exclude_recent_days,
                minimum_solver_count=config.min_solver_count,
            )
        destination = paths.exports / f"session-{sid}.html"
        export_session_html(db, sid, destination, timezone=config.timezone)
        print(f"Created list #{sid}: {destination}")
        _print_recommendations(recs)
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_make_contest(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        sid, _ = create_training_contest(
            db,
            platform=args.platform,
            count=args.count,
            duration_minutes=args.duration,
            name=args.name,
            seed=args.seed,
            rated=not args.unrated,
            exclude_recent_days=config.exclude_recent_days,
            minimum_solver_count=config.min_solver_count,
        )
        destination = paths.exports / f"session-{sid}.html"
        export_session_html(db, sid, destination, timezone=config.timezone)
        print(f"Created contest #{sid}: {destination}")
        print("Tags stay hidden; after the contest ends, use the Show tags button.")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_plan_week(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        ids = create_weekly_plan(
            db,
            config,
            start=args.start,
            budget=args.budget,
            seed=args.seed,
        )
        for sid in ids:
            export_session_html(
                db,
                sid,
                paths.exports / f"session-{sid}.html",
                timezone=config.timezone,
            )
        index = export_plan_index(
            db,
            ids,
            paths.exports / f"week-{args.start.isoformat()}.html",
            timezone=config.timezone,
        )
        actual_budget = args.budget or config.weekly_problem_budget
        print(f"Created {len(ids)} sessions with {actual_budget} total problems.")
        print(index)
        for sid in ids:
            s = db.get_session(sid)
            print(f"  #{sid}  {_fmt_epoch(s['scheduled_at'], config.timezone)}  {s['name']}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_sessions(args: argparse.Namespace) -> int:
    _, config, db = _open_state(args)
    try:
        rows = []
        for s in db.list_sessions(args.limit):
            rows.append(
                [
                    s["id"],
                    _fmt_epoch(s["scheduled_at"], config.timezone),
                    s["status"],
                    s["platform"],
                    s["kind"],
                    f"{s['solved_count'] or 0}/{s['problem_count']}",
                    s["name"],
                ]
            )
        print(_table(["id", "scheduled", "status", "site", "kind", "solved", "name"], rows))
        return 0
    finally:
        db.close()


def cmd_show(args: argparse.Namespace) -> int:
    _, config, db = _open_state(args)
    try:
        s = db.get_session(args.session_id)
        reveal = args.reveal or s["status"] == "finished" or s["kind"] == "list"
        rows = []
        for row in db.get_session_items(args.session_id):
            elapsed = "—" if row["elapsed_seconds"] is None else f"{row['elapsed_seconds']/60:.1f}m"
            rows.append(
                [
                    chr(ord("A") + int(row["ordinal"]) - 1) if int(row["ordinal"]) <= 26 else row["ordinal"],
                    row["platform"],
                    row["problem_id"],
                    row["title"],
                    "AC" if row["solved"] else "—",
                    elapsed,
                    row["wrong_attempts"],
                    f"{row['difficulty']:.0f}" if reveal else "hidden",
                    f"{row['target_difficulty']:.0f}" if reveal else "hidden",
                    row["url"],
                ]
            )
        print(f"#{s['id']} {s['name']} — {s['status']} — {s['duration_minutes']} min")
        print(_table(["#", "site", "id", "title", "result", "time", "wrong", "diff", "target", "url"], rows))
        if s["status"] == "finished":
            print(f"score={s['score']:.2f}, penalty={s['penalty_minutes']:.2f} min")
            if s["performance"] is not None:
                print(
                    f"performance={s['performance']:.0f}, local rating "
                    f"{s['old_rating']:.0f} -> {s['new_rating']:.1f}"
                )
        return 0
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_start(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        db.start_session(args.session_id)
        destination = export_session_html(
            db,
            args.session_id,
            paths.exports / f"session-{args.session_id}.html",
            timezone=config.timezone,
        )
        print(f"Started session #{args.session_id}")
        print(destination)
        if args.open:
            webbrowser.open(destination.as_uri())
        return 0
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_record(args: argparse.Namespace) -> int:
    _, _, db = _open_state(args)
    try:
        db.record_result(
            args.session_id,
            args.slot,
            solved=args.ac,
            elapsed_seconds=(None if args.elapsed is None else round(args.elapsed * 60)),
            wrong_attempts=args.wrong,
        )
        print(f"Recorded slot {args.slot} in session #{args.session_id}")
        return 0
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_pull_results(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        session = db.get_session(args.session_id)
        if not args.no_sync:
            platform = session["platform"] if session["platform"] != "mixed" else "all"
            _run_sync(db, config, paths, platform, False)
        changed = db.infer_session_results(args.session_id)
        export_session_html(
            db,
            args.session_id,
            paths.exports / f"session-{args.session_id}.html",
            timezone=config.timezone,
        )
        print(f"Updated {changed} problem slots from synchronized submissions.")
        return 0
    except (KeyError, ValueError, HttpClientError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_finish(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        try:
            db.infer_session_results(args.session_id)
        except ValueError:
            pass
        result = finish_session(db, args.session_id)
        destination = export_session_html(
            db,
            args.session_id,
            paths.exports / f"session-{args.session_id}.html",
            timezone=config.timezone,
            reveal=True,
        )
        print(f"Finished session #{args.session_id}")
        print(f"score={result.score:.2f}, penalty={result.penalty_minutes:.2f} min")
        if result.performance is not None:
            marker = result.performance_censored or ""
            print(
                f"local performance {marker}{result.performance:.0f}; "
                f"rating {result.old_rating:.0f} -> {result.new_rating:.1f}"
            )
        print(destination)
        return 0
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_export(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        suffix = "html" if args.format == "html" else "md"
        destination = paths.exports / f"session-{args.session_id}.{suffix}"
        if args.format == "html":
            export_session_html(
                db,
                args.session_id,
                destination,
                timezone=config.timezone,
                reveal=args.reveal,
            )
        else:
            export_session_markdown(
                db,
                args.session_id,
                destination,
                timezone=config.timezone,
                reveal=args.reveal,
            )
        print(destination)
        return 0
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_open(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        destination = export_session_html(
            db,
            args.session_id,
            paths.exports / f"session-{args.session_id}.html",
            timezone=config.timezone,
            reveal=args.reveal,
        )
        print(destination)
        webbrowser.open(destination.as_uri())
        return 0
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()


def cmd_status(args: argparse.Namespace) -> int:
    paths, config, db = _open_state(args)
    try:
        profiles = db.profiles()
        rows = []
        for p in profiles:
            rows.append(
                [
                    p["platform"],
                    p["handle"] or "—",
                    f"{p['official_rating']:.0f}",
                    f"{p['local_rating']:.1f}",
                    f"±{p['rating_deviation']:.0f}",
                    db.problem_count(p["platform"]),
                    db.solved_count(p["platform"]),
                ]
            )
        print(f"Home: {paths.home}")
        print(_table(["site", "handle", "official", "local", "uncertainty", "bank", "solved"], rows))
        recent = db.list_sessions(5)
        if recent:
            print("\nRecent sessions:")
            print(
                _table(
                    ["id", "status", "site", "solved", "name"],
                    [
                        [s["id"], s["status"], s["platform"], f"{s['solved_count'] or 0}/{s['problem_count']}", s["name"]]
                        for s in recent
                    ],
                )
            )
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cp-trainer",
        description="Local-first Codeforces + AtCoder training planner and practice rating tracker.",
    )
    parser.add_argument(
        "--home",
        default=str(_default_home()),
        help="state directory (default: ~/.cp-trainer or CP_TRAINER_HOME)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize config and SQLite database")
    p.add_argument("--cf-handle", default="")
    p.add_argument("--atcoder-handle", default="")
    p.add_argument("--cf-rating", type=float, default=2274)
    p.add_argument("--atcoder-rating", type=float, default=1860)
    p.add_argument("--timezone", default="Asia/Taipei")
    p.add_argument("--weekly-budget", type=int, default=20)
    p.add_argument("--cf-share", type=float, default=0.55)
    p.add_argument("--exclude-recent-days", type=int, default=45)
    p.add_argument("--min-solver-count", type=int, default=20)
    p.add_argument("--seed", type=int, default=20260901)
    p.add_argument("--force", action="store_true")
    p.add_argument("--reset-local", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("profile", help="show or update profile settings")
    p.add_argument("--platform", choices=["cf", "atcoder"])
    p.add_argument("--handle")
    p.add_argument("--official-rating", type=float)
    p.add_argument("--local-rating", type=float)
    p.add_argument("--reset-uncertainty", action="store_true")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("sync", help="synchronize problem banks and submissions")
    p.add_argument("--platform", choices=["all", "cf", "atcoder"], default="all")
    p.add_argument("--force", action="store_true", help="ignore metadata cache")
    p.set_defaults(func=cmd_sync)

    for name, func, help_text in (
        ("recommend", cmd_recommend, "print adaptive recommendations without saving"),
        ("make-list", cmd_make_list, "create and save an adaptive problem list"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--platform", choices=["cf", "atcoder", "mixed"], default="mixed")
        p.add_argument("--count", type=int, default=8)
        p.add_argument("--mode", choices=["balanced", "growth", "speed", "review", "challenge"], default="growth")
        p.add_argument("--seed", type=int, default=20260901)
        if name == "make-list":
            p.add_argument("--name", default="Adaptive training list")
        p.set_defaults(func=func)

    p = sub.add_parser("make-contest", help="generate a local rated training contest")
    p.add_argument("--platform", choices=["cf", "atcoder"], required=True)
    p.add_argument("--count", type=int, default=6)
    p.add_argument("--duration", type=int, default=120)
    p.add_argument("--name", default="Generated training contest")
    p.add_argument("--seed", type=int, default=20260901)
    p.add_argument("--unrated", action="store_true")
    p.set_defaults(func=cmd_make_contest)

    p = sub.add_parser("plan-week", help="generate drills plus CF and AtCoder contests")
    p.add_argument("--start", type=_parse_date, default=date.today())
    p.add_argument("--budget", type=int, help="problem budget; defaults to config")
    p.add_argument("--seed", type=int, default=20260901)
    p.set_defaults(func=cmd_plan_week)

    p = sub.add_parser("sessions", help="list saved lists and contests")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("show", help="show one session")
    p.add_argument("session_id", type=int)
    p.add_argument("--reveal", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("start", help="start the timer for a session")
    p.add_argument("session_id", type=int)
    p.add_argument("--open", action="store_true", help="open generated HTML in the default browser")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("record", help="record a problem result manually")
    p.add_argument("session_id", type=int)
    p.add_argument("slot", type=_slot_number)
    result_group = p.add_mutually_exclusive_group(required=True)
    result_group.add_argument("--ac", action="store_true")
    result_group.add_argument("--fail", action="store_true")
    p.add_argument("--elapsed", type=float, help="minutes from session start to AC")
    p.add_argument("--wrong", type=int, default=0)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("pull-results", help="sync and infer results from judge submissions")
    p.add_argument("session_id", type=int)
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_pull_results)

    p = sub.add_parser("finish", help="finish, score, and locally rate a session")
    p.add_argument("session_id", type=int)
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("export", help="export a session as HTML or Markdown")
    p.add_argument("session_id", type=int)
    p.add_argument("--format", choices=["html", "md"], default="html")
    p.add_argument("--reveal", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("open", help="open a session HTML page")
    p.add_argument("session_id", type=int)
    p.add_argument("--reveal", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("status", help="show ratings, bank size, and recent sessions")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = int(args.func(args))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        code = 130
    raise SystemExit(code)
