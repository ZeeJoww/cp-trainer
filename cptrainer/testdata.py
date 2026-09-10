"""Pinned GitHub test archives; resumable by contest, never execute archive code."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import gzip
import hashlib
from html import unescape
from http.client import IncompleteRead
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
from urllib.request import Request

from .archive import PublicRedirect, archive_lock, archive_statements
from .config import Paths
from .db import Database
from .http_client import CachedHttpClient
from .sources import ATCODER_RESOURCES, canonical_atcoder, parse_atcoder_problems
from urllib.request import build_opener

REPOSITORY = "https://github.com/conlacda/atcoder-testcases"
COMPONENT = re.compile(r"[A-Za-z0-9_. -]{1,160}")
MAX_FILE = 256 * 1024 * 1024
MAX_ARCHIVE = 4 * 1024**3
MAX_EXPANDED = 12 * 1024**3


def select_contests(refs: str, contests: list[dict], count: int) -> list[dict]:
    if not 1 <= count <= 100:
        raise ValueError("每轮下载场数应为 1–100")
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    dated = {normalize(c["id"]): c for c in contests}
    result = []
    for line in refs.splitlines():
        revision, ref = line.split()
        branch = ref.removeprefix("refs/heads/")
        if not re.fullmatch(r"[a-f0-9]{40}", revision) or not re.fullmatch(r"[a-zA-Z0-9_-]+", branch):
            raise ValueError("GitHub 分支清单格式异常")
        contest = dated.get(normalize(branch))
        if contest and contest["start_epoch_second"] + contest["duration_second"] <= time.time():
            result.append(contest | {"branch": branch, "revision": revision})
    result.sort(key=lambda c: (c["start_epoch_second"], c["id"]), reverse=True)
    if len(result) < count:
        raise ValueError(f"仅匹配到 {len(result)} 场存档，少于要求的 {count} 场")
    return result[:count]


def statement_rules(html: str) -> dict:
    # ponytail: statement-based classification is conservative; use reviewed official checkers to support more special-output tasks.
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    limits = re.search(r"Time Limit:\s*([\d.]+)\s*sec\s*/\s*Memory Limit:\s*(\d+)\s*MiB", text)
    result = {"time_limit_ms": round(float(limits[1]) * 1000) if limits else 2000,
              "memory_limit_mb": int(limits[2]) if limits else 1024,
              "checker": "tokens", "tolerance": None, "reason": "按空白分隔，逐项比对标准输出。"}
    if not limits or not 100 <= result["time_limit_ms"] <= 60000 or not 16 <= result["memory_limit_mb"] <= 4096:
        return result | {"checker": "unsupported", "reason": "题面缺少可核对的运行限制。"}
    if re.search(r"interactive problem|output.only|Interaction|インタラクティブ|対話型|出力のみ", text, re.I):
        return result | {"checker": "unsupported", "reason": "交互 / 输出型题目需要专用评测器，存档未提供。"}
    sections = re.findall(r"<h3[^>]*>\s*Output\s*</h3>(.*?)</section>", html, re.S | re.I)
    if not sections:
        sections = re.findall(r"<h3[^>]*>\s*出力\s*</h3>(.*?)</section>", html, re.S)
    output = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", " ".join(sections)))).replace("−", "-")
    if not output:
        return result | {"checker": "unsupported", "reason": "无法核对输出规则。"}
    if re.search(r"any (?:of (?:the|them|these)|one|output|valid|possible|such)|(?:print|output) (?:any|an? (?:arbitrary|possible|solution)|one of|such a|your solution)|your solution|choose one|if there is a pair.{0,100}satisf|multiple (?:possible |valid |correct )?(?:answers|solutions)|in any order|arbitrary order|どれ|任意|複数|どの順", output, re.I) or re.search(r"(?:print|output) (?:any of|one of)|multiple (?:valid |correct |possible )?(?:answers|solutions)|複数[^。]{0,120}(?:いずれ|どれ|そのうち|出力(?:すれば|しても))", text, re.I):
        return result | {"checker": "unsupported", "reason": "输出可能有多种正确答案，存档未提供特殊判题器。"}
    if re.search(r"absolute|relative|誤差", output, re.I):
        exponent = re.search(r"10\s*\^\s*\{?\s*-\s*(\d+)\s*\}?", output)
        if not exponent or not 1 <= int(exponent[1]) <= 15:
            return result | {"checker": "unsupported", "reason": "浮点误差规则需要人工核对。"}
        # AtCoder's usual absolute OR relative error rule; do not infer other checkers.
        absolute = bool(re.search(r"absolute|絶対誤差", output, re.I))
        relative = bool(re.search(r"relative|相対誤差", output, re.I))
        if absolute and relative and not re.search(r"absolute or relative|relative or absolute|誤差(?:または|もしくは)", output, re.I):
            return result | {"checker": "unsupported", "reason": "此浮点误差规则需要专用判题器。"}
        if not absolute and not relative:
            return result | {"checker": "unsupported", "reason": "此浮点误差规则需要核对。"}
        result.update(checker="float" if absolute and relative else "float_abs" if absolute else "float_rel", tolerance=10 ** -int(exponent[1]), reason=f"{'绝对或相对' if absolute and relative else '绝对' if absolute else '相对'}误差 ≤ 10^-{exponent[1]}。")
    if re.search(r"case.insensitive|either (?:upper|lower)case|uppercase (?:or|and) lowercase|in any case.*upper or lower|大文字.*小文字.*(?:区別|問わ|よい|良い)", output, re.I) and result["checker"] == "tokens":
        result.update(checker="tokens_ci", reason="按题面规则忽略字母大小写，逐项比对输出。")
    return result


def download_archive(paths: Paths, contest: dict, progress=print) -> tuple[Path, str]:
    folder = paths.cache / "testdata"
    folder.mkdir(exist_ok=True)
    destination = folder / (contest["revision"] + ".tar.gz")
    temporary = destination.with_suffix(".part")
    if not destination.exists():
        url = f"https://codeload.github.com/conlacda/atcoder-testcases/tar.gz/{contest['revision']}"
        opener = build_opener(PublicRedirect())
        for attempt in range(3):
            try:
                digest, total, last = hashlib.sha256(), 0, time.monotonic()
                offset = temporary.stat().st_size if temporary.exists() else 0
                headers = {"User-Agent": "cp-trainer-local/0.3", "Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                with opener.open(Request(url, headers=headers), timeout=60) as response:
                    resume = response.status == 206
                    if resume and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("断点响应范围不匹配")
                    if resume:
                        with temporary.open("rb") as source:
                            while block := source.read(1024 * 1024):
                                digest.update(block)
                        total = offset
                    length = response.headers.get("Content-Length")
                    expected = total + int(length) if length else None
                    with temporary.open("ab" if resume else "wb") as out:
                        while block := response.read(1024 * 1024):
                            total += len(block)
                            if total > MAX_ARCHIVE or shutil.disk_usage(folder).free < 1024**3:
                                raise ValueError("存档超过 4 GiB 或可用空间不足 1 GiB")
                            digest.update(block)
                            out.write(block)
                            if time.monotonic() - last > 4:
                                progress(f"{contest['id']}：下载 {total / 1024**2:.1f} MiB")
                                last = time.monotonic()
                    if expected is not None and total != expected:
                        raise OSError("存档传输中断")
                temporary.replace(destination)
                return destination, digest.hexdigest()
            except (OSError, TimeoutError, IncompleteRead):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    with destination.open("rb") as source:
        return destination, hashlib.file_digest(source, "sha256").hexdigest()


def extract_archive(archive: Path, destination: Path, branch: str) -> dict:
    """Copy regular input/output files only, validating every member before writing."""
    destination.mkdir(parents=True, exist_ok=False)
    files, total, count = {}, 0, 0
    with gzip.open(archive, "rb") as stream, tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith("/") or any(p in {"..", "."} or "\\" in p for p in parts):
                raise ValueError("存档包含不安全路径")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError("存档包含链接或设备文件")
            if not member.isfile():
                continue
            relative = parts[1:]
            total += member.size
            count += 1
            if member.size > MAX_FILE or total > MAX_EXPANDED or count > 50000:
                raise ValueError("存档解压大小或文件数超出限制")
            keep = len(relative) == 4 and relative[0] == branch and relative[2] in {"in", "out"}
            license_file = relative == ("LICENSE",)
            if not keep and not license_file:
                continue
            if any(not COMPONENT.fullmatch(p) for p in relative):
                raise ValueError("存档文件名格式异常")
            if shutil.disk_usage(destination).free < member.size + 1024**3:
                raise ValueError("解压空间不足，已完成的其他比赛保持不变")
            target = destination.joinpath(*(relative[1:] if keep else relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with tar.extractfile(member) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if keep:
                files["/".join(relative[1:])] = {"size": member.size, "sha256": digest.hexdigest()}
        while stream.read(1024 * 1024):
            pass  # Read the gzip trailer too: tar alone may accept a truncated download.
    tasks = {}
    for path, metadata in files.items():
        letter, kind, name = path.split("/")
        tasks.setdefault(letter, {"in": {}, "out": {}})[kind][name] = metadata
    if not tasks:
        raise ValueError("存档中没有测试数据")
    manifests, incomplete = {}, {}
    for letter, sides in tasks.items():
        if not sides["in"] or sides["in"].keys() != sides["out"].keys():
            incomplete[letter] = {"missing_input": sorted(sides["out"].keys() - sides["in"].keys()), "missing_output": sorted(sides["in"].keys() - sides["out"].keys())}
        manifests[letter] = [{"name": name, "input": sides["in"][name], "output": sides["out"][name]} for name in sorted(sides["in"].keys() & sides["out"].keys())]
    return {"tasks": manifests, "bytes": sum(item["size"] for item in files.values()), "incomplete": incomplete, "files": files}


def archive_complete(paths: Paths, branch: str, revision: str, *, hashes=False) -> int:
    folder = paths.home / "testdata" / branch / revision
    try:
        manifest = json.loads((folder / "manifest.json").read_text())
        files = manifest.get("files") or {f"{letter}/{side}/{case['name']}": case[key] for letter, cases in manifest["tasks"].items() for case in cases for side, key in [("in", "input"), ("out", "output")]}
        for name, metadata in files.items():
            target = folder / name
            if not target.resolve().is_relative_to(folder.resolve()) or target.is_symlink() or target.stat().st_size != metadata["size"]:
                return 0
            if hashes:
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["sha256"]:
                        return 0
        return len(files)
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def refresh_rules(db: Database) -> None:
    rows = db.conn.execute("SELECT j.*,s.html FROM judge_sets j LEFT JOIN statements s USING(platform,problem_id)").fetchall()
    with db.conn:
        for row in rows:
            if row["reason"].startswith("原存档") or row["case_count"] == 0:
                continue
            rules = statement_rules(row["html"] or "")
            if any(case["output"]["size"] > 16 * 1024**2 for case in json.loads(row["manifest_json"])):
                rules.update(checker="unsupported", reason="标准输出超过本地 16 MiB 输出上限。")
            db.conn.execute("UPDATE judge_sets SET checker=?,tolerance=?,reason=?,time_limit_ms=?,memory_limit_mb=? WHERE platform=? AND problem_id=?",
                (rules["checker"], rules["tolerance"], rules["reason"], rules["time_limit_ms"], rules["memory_limit_mb"], row["platform"], row["problem_id"]))


def sync_testdata(db: Database, paths: Paths, count: int = 100, progress=print) -> None:
    with archive_lock(paths, "testdata"), archive_lock(paths, "atcoder"):
        client = CachedHttpClient(paths.cache)
        resource = lambda name, key: client.get_json(f"{ATCODER_RESOURCES}/{name}.json", cache_key=key, ttl_seconds=86400)
        contests = resource("contests", "atcoder-contests")
        refs = subprocess.run(["git", "ls-remote", "--heads", REPOSITORY + ".git"], capture_output=True, text=True, timeout=90, check=True).stdout
        selected = select_contests(refs, contests, count)
        pairs = resource("contest-problem", "atcoder-contest-problem")
        problems = canonical_atcoder(parse_atcoder_problems(resource("problems", "atcoder-problems"), resource("problem-models", "atcoder-models")), contests, pairs)
        dates = {c["id"]: c["start_epoch_second"] for c in selected}
        db.upsert_problems([replace(p, published_at=dates[p.contest_id]) for p in problems if p.contest_id in dates])
        for c in selected:
            with db.conn:
                db.conn.execute("""INSERT INTO test_archives(branch,contest_id,title,started_at,revision) VALUES(?,?,?,?,?)
                    ON CONFLICT(branch) DO NOTHING""", (c["branch"], c["id"], c["title"], c["start_epoch_second"], c["revision"]))
        with db.conn:
            db.conn.execute("UPDATE statements SET status='pending' WHERE platform='atcoder' AND html NOT LIKE '%Time Limit:%' AND problem_id IN (SELECT problem_id FROM problems WHERE platform='atcoder' AND contest_id IN (" + ",".join("?" for _ in dates) + "))", list(dates))
        archive_statements(db, paths, "atcoder", progress, contest_ids=set(dates))
        refresh_rules(db)
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="testdata-download") as downloads:
            pending = {}
            complete = set()
            for c in selected:
                old = db.conn.execute("SELECT status,revision FROM test_archives WHERE branch=?", (c["branch"],)).fetchone()
                if old["status"] in {"ready", "partial"} and old["revision"] == c["revision"] and archive_complete(paths, c["branch"], c["revision"]):
                    complete.add(c["branch"])
                else:
                    pending[downloads.submit(download_archive, paths, c, progress)] = c
            for number, future in enumerate(as_completed(pending), len(complete) + 1):
                c = pending[future]
                folder = paths.home / "testdata" / c["branch"] / c["revision"]
                staging = folder.with_name(folder.name + ".part")
                try:
                    with db.conn:
                        db.conn.execute("UPDATE test_archives SET status='downloading',error='' WHERE branch=?", (c["branch"],))
                    progress(f"{number}/{count} {c['id']}：开始下载测试数据")
                    archive, digest = future.result()
                    if staging.exists():
                        shutil.rmtree(staging)
                    try:
                        manifest = extract_archive(archive, staging, c["branch"])
                    except (tarfile.TarError, EOFError, gzip.BadGzipFile):
                        archive.unlink(missing_ok=True)
                        raise
                    mapping = {str(p["problem_index"]).upper(): p["problem_id"] for p in pairs if p["contest_id"] == c["id"]}
                    if not {letter.upper() for letter in manifest["tasks"]}.issubset(mapping):
                        raise ValueError("存档题号与公开比赛题目无法完全对应")
                    manifest["missing_problems"] = sorted(set(mapping.values()) - {mapping[letter.upper()] for letter in manifest["tasks"]})
                    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    previous = folder.with_name(folder.name + ".previous")
                    if previous.exists():
                        if not folder.exists():
                            previous.rename(folder)
                        else:
                            shutil.rmtree(previous)
                    if folder.exists():
                        folder.rename(previous)
                    try:
                        staging.rename(folder)
                    except OSError:
                        if previous.exists():
                            previous.rename(folder)
                        raise
                    with db.conn:
                        db.conn.execute("DELETE FROM judge_sets WHERE branch=?", (c["branch"],))
                        for letter, cases in manifest["tasks"].items():
                            pid = mapping[letter.upper()]
                            row = db.conn.execute("SELECT html FROM statements WHERE platform='atcoder' AND problem_id=?", (pid,)).fetchone()
                            rules = statement_rules(row[0] if row else "")
                            if letter in manifest["incomplete"]:
                                rules.update(checker="unsupported", reason="原存档输入 / 输出未完整配对，已保留文件，暂不评测。")
                            if any(case["output"]["size"] > 16 * 1024**2 for case in cases):
                                rules.update(checker="unsupported", reason="标准输出超过本地 16 MiB 输出上限。")
                            db.conn.execute("""INSERT INTO judge_sets(platform,problem_id,branch,directory,case_count,manifest_json,checker,tolerance,reason,time_limit_ms,memory_limit_mb)
                                VALUES('atcoder',?,?,?,?,?,?,?,?,?,?)""", (pid, c["branch"], (folder / letter).relative_to(paths.home).as_posix(), len(cases), json.dumps(cases), rules["checker"], rules["tolerance"], rules["reason"], rules["time_limit_ms"], rules["memory_limit_mb"]))
                        error = "原存档缺少配对文件：" + ", ".join(manifest["incomplete"]) if manifest["incomplete"] else ""
                        if manifest["missing_problems"]:
                            error += ("；" if error else "") + "原存档缺少题目：" + ", ".join(manifest["missing_problems"])
                        db.conn.execute("""UPDATE test_archives SET status=?,revision=?,problem_count=?,case_count=?,bytes=?,sha256=?,downloaded_at=?,error=? WHERE branch=?""",
                            ("partial" if error else "ready", c["revision"], len(manifest["tasks"]), sum(map(len, manifest["tasks"].values())), manifest["bytes"], digest, int(time.time()), error, c["branch"]))
                    if previous.exists():
                        shutil.rmtree(previous)
                    progress(f"{number}/{count} {c['id']}：{len(manifest['tasks'])} 题已就绪")
                except Exception as exc:
                    with db.conn:
                        db.conn.execute("UPDATE test_archives SET status='failed',error=? WHERE branch=?", (f"{type(exc).__name__}: {str(exc)[:240]}", c["branch"]))
                    progress(f"{number}/{count} {c['id']}：失败，{type(exc).__name__}: {str(exc)[:180]}")


def testdata_status(db: Database) -> dict:
    archives = [dict(r) for r in db.conn.execute("SELECT * FROM test_archives ORDER BY started_at DESC,contest_id")]
    ready = [a for a in archives if a["status"] in {"ready", "partial"}]
    checkers = dict(db.conn.execute("SELECT checker,COUNT(*) FROM judge_sets GROUP BY checker"))
    return {"repository": REPOSITORY, "contests": archives, "ready": len(ready), "total": len(archives),
            "problems": sum(a["problem_count"] for a in ready), "cases": sum(a["case_count"] for a in ready),
            "bytes": sum(a["bytes"] for a in ready), "checkers": checkers}


def verify_testdata(db: Database, paths: Paths) -> dict:
    count = 0
    for row in db.conn.execute("SELECT * FROM test_archives WHERE status IN ('ready','partial')").fetchall():
        checked = archive_complete(paths, row["branch"], row["revision"], hashes=True)
        if not checked:
            with db.conn:
                db.conn.execute("UPDATE test_archives SET status='failed',error='文件缺失或校验失败，请重新下载' WHERE branch=?", (row["branch"],))
            raise ValueError("测试数据校验失败：" + row["contest_id"])
        count += checked
    return {"verified_files": count}


def main():
    from .cli import _default_home
    parser = argparse.ArgumentParser(description="下载 GitHub 存档中按比赛日期最新的 100 场 AtCoder 测试数据")
    parser.add_argument("--home", type=Path, default=_default_home())
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    paths = Paths(args.home.expanduser().resolve())
    paths.ensure()
    db = Database(paths.database)
    try:
        db.migrate()
        if args.verify:
            print(json.dumps(verify_testdata(db, paths)))
        else:
            sync_testdata(db, paths, args.count, lambda msg: print(msg, flush=True))
            status = testdata_status(db)
            print(json.dumps({k: v for k, v in status.items() if k != "contests"}))
            if status["ready"] != status["total"]:
                raise SystemExit(2)
    finally:
        db.close()


if __name__ == "__main__":
    main()
