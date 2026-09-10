"""Resumable statement archive. Only public HTTPS resources are downloaded."""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from contextlib import contextmanager
import hashlib
from html import escape, unescape
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
import os
import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config, Paths
from .db import Database
from .http_client import CachedHttpClient
from .sources import SINCE, sync_atcoder, sync_codeforces

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
TAGS = set("a abbr b blockquote br caption code col colgroup dd del details div dl dt em h1 h2 h3 h4 h5 h6 hr i img kbd li mark ol p pre s section small span strong sub summary sup table tbody td th thead tr u ul var".split())


@contextmanager
def archive_lock(paths: Paths, platform: str):
    with (paths.home / f"sync-{platform}.lock").open("a+b") as lock:
        lock.write(b"0"); lock.flush(); lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"{platform} 已有同步任务在运行") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


class Fragments(HTMLParser):
    def __init__(self, attribute: str, value: str):
        super().__init__(convert_charrefs=False)
        self.attribute, self.value = attribute, value
        self.depth = 0
        self.root_tag = ""
        self.parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if not self.depth and self.value in (attr.get(self.attribute) or "").split():
            self.depth = 1
            self.root_tag = tag
            self.parts = [self.get_starttag_text()]
        elif self.depth:
            self.parts.append(self.get_starttag_text())
            if tag == self.root_tag:
                self.depth += 1

    def handle_endtag(self, tag):
        if self.depth:
            self.parts.append(f"</{tag}>")
            if tag == self.root_tag:
                self.depth -= 1
            if not self.depth:
                self.blocks.append("".join(self.parts))

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def extract_statements(html: str, platform: str) -> dict[str, str]:
    parser = Fragments("class" if platform == "cf" else "id", "problem-statement" if platform == "cf" else "task-statement")
    parser.feed(html)
    if platform == "cf":
        labels = [re.search(r'class=["\']title["\'][^>]*>\s*([^.<]+)\.', block).group(1).strip() for block in parser.blocks]
    else:
        labels = [unescape(x).split(" - ", 1)[0].strip() for x in re.findall(r'<span class="h2">([^<]+)</span>', html)]
        limits = re.findall(r'<p>(Time Limit:.*?)</p>', html)
        if len(limits) == len(parser.blocks):
            parser.blocks = [f'<p>{limit}</p>' + block for limit, block in zip(limits, parser.blocks)]
    if not parser.blocks or len(labels) != len(parser.blocks) or len(set(labels)) != len(labels):
        raise ValueError("无法核对题面与题号，保留待下载状态")
    return dict(zip(labels, parser.blocks))


def public_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in {None, 443}:
        raise ValueError("仅下载公开 HTTPS 资源")
    for entry in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(entry[4][0]).is_global:
            raise ValueError("拒绝访问本机或私有网络资源")
    return url


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


class Downloader:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.client = CachedHttpClient(paths.cache)
        self.opener = build_opener(PublicRedirect())
        (paths.cache / "pages").mkdir(exist_ok=True)
        (paths.home / "assets").mkdir(exist_ok=True)

    def store_asset(self, raw: bytes, suffix: str) -> str:
        name = hashlib.sha256(raw).hexdigest() + suffix
        target = self.paths.home / "assets" / name
        temporary = target.with_suffix(suffix + ".part")
        temporary.write_bytes(raw)
        temporary.replace(target)
        return "/assets/" + name

    def fetch(self, url: str, *, asset: bool = False) -> tuple[bytes, str]:
        digest = hashlib.sha256(url.encode()).hexdigest()
        suffix = Path(urlsplit(url).path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,6}", suffix):
            suffix = ".bin" if asset else ".html"
        name = digest + suffix
        target = (self.paths.home / "assets" if asset else self.paths.cache / "pages") / name
        if target.exists():
            return target.read_bytes(), name
        public_url(url)
        for attempt in range(3):
            self.client._throttle(urlsplit(url).hostname)
            try:
                with self.opener.open(Request(url, headers={"User-Agent": "cp-trainer-local/0.2 (personal offline archive)", "Accept-Encoding": "identity"}), timeout=35) as response:
                    raw = response.read(64 * 1024 * 1024 + 1)
                    if len(raw) > 64 * 1024 * 1024:
                        raise ValueError("资源超过 64 MiB，未完整下载")
                    if asset and "text/html" in response.headers.get("Content-Type", ""):
                        raise ValueError("配图返回了 HTML 页面")
                temporary = target.with_suffix(target.suffix + ".part")
                temporary.write_bytes(raw)
                temporary.replace(target)
                return raw, name
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                self.client._backoff(attempt, exc.headers.get("Retry-After"))
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
                self.client._backoff(attempt, None)
        raise RuntimeError("下载失败")


class Sanitize(HTMLParser):
    def __init__(self, base: str, downloader: Downloader | None = None):
        super().__init__(convert_charrefs=True)
        self.base, self.downloader = base, downloader
        self.parts: list[str] = []
        self.blocked = 0
        self.errors: list[str] = []

    def feed(self, data):
        def svg_image(match):
            try:
                if not self.downloader:
                    raise ValueError("内嵌图形尚未保存")
                root = ET.fromstring(match[0])
                for node in root.iter():
                    if node.tag.split("}")[-1] in {"script", "foreignObject", "image", "a"}:
                        raise ValueError("内嵌图形含外部或活动内容")
                    for key, value in list(node.attrib.items()):
                        if key.lower().startswith("on"):
                            del node.attrib[key]
                        if key.split("}")[-1] == "href" and not value.startswith("#"):
                            raise ValueError("内嵌图形依赖外部资源")
                        if re.search(r"url\(\s*['\"]?(?!#)[a-z]+:", value, re.I):
                            raise ValueError("内嵌图形依赖外部资源")
                src = self.downloader.store_asset(ET.tostring(root, encoding="utf-8"), ".svg")
                return f'<img src="{src}" alt="题面图示">'
            except Exception as exc:
                self.errors.append(f"内嵌图形未缓存：{type(exc).__name__}")
                return "<p>此处图示尚未保存，请重试同步。</p>"
        super().feed(re.sub(r"<svg\b.*?</svg>", svg_image, data, flags=re.S | re.I))

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object", "svg", "math", "form"}:
            self.blocked += 1
        if self.blocked or tag not in TAGS:
            return
        clean = []
        for key, value in attrs:
            if not value:
                continue
            if key in {"class", "title", "alt", "colspan", "rowspan", "lang"}:
                clean.append((key, value))
            elif key in {"src", "href"} and ((tag == "img" and key == "src") or (tag == "a" and key == "href")):
                if tag == "img" and value.startswith("/assets/"):
                    clean.append((key, value))
                    continue
                if tag == "img" and value.startswith("data:image/"):
                    try:
                        if not self.downloader:
                            raise ValueError("内嵌图片尚未保存")
                        match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,(.+)", value, re.S)
                        if not match:
                            raise ValueError("不支持的内嵌图片格式")
                        raw = base64.b64decode("".join(match[2].split()), validate=True)
                        if len(raw) > 32 * 1024 * 1024:
                            raise ValueError("内嵌图片超过 32 MiB")
                        clean.append((key, self.downloader.store_asset(raw, "." + match[1])))
                    except Exception as exc:
                        self.errors.append(f"内嵌图片未缓存：{type(exc).__name__}")
                    continue
                url = urljoin(self.base, value)
                if urlsplit(url).scheme not in {"https", "http"}:
                    continue
                if tag == "img" or re.search(r"\.(pdf|zip|txt|gz|in|out)(\?|$)", url, re.I):
                    if self.downloader:
                        try:
                            _, name = self.downloader.fetch(url, asset=True)
                            url = "/assets/" + name
                        except Exception as exc:
                            self.errors.append(f"附图/附件未缓存：{url} ({type(exc).__name__})")
                    else:
                        self.errors.append("附图/附件尚未下载")
                clean.append((key, url))
            elif key == "style":
                declarations = [s for s in value.split(";") if re.fullmatch(r"\s*(text-align|white-space|font-weight|font-style)\s*:\s*[a-z0-9 -]+\s*", s)]
                if declarations:
                    clean.append(("style", ";".join(declarations)))
        if tag == "a":
            clean.extend([("target", "_blank"), ("rel", "noreferrer noopener")])
        self.parts.append("<" + tag + "".join(f' {key}="{escape(value, quote=True)}"' for key, value in clean) + ">")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object", "svg", "math", "form"} and self.blocked:
            self.blocked -= 1
            return
        if not self.blocked and tag in TAGS and tag not in VOID:
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(data))


def archive_statements(db: Database, paths: Paths, platform: str, progress=print, limit: int = 0, contest_ids: set[str] | None = None) -> None:
    downloader = Downloader(paths)
    rows = db.conn.execute("""SELECT p.* FROM problems p LEFT JOIN statements s USING(platform, problem_id)
        WHERE p.platform=? AND p.is_real=1 AND COALESCE(s.status,'pending')!='ready'
        ORDER BY p.published_at DESC, p.contest_id, p.problem_index""", (platform,)).fetchall()
    groups = defaultdict(list)
    for row in rows:
        if contest_ids is None or row["contest_id"] in contest_ids:
            groups[row["contest_id"]].append(row)
    if limit:
        groups = dict(list(groups.items())[:limit])
    for number, (contest, items) in enumerate(groups.items(), 1):
        url = f"https://codeforces.com/contest/{contest}/problems?locale=en" if platform == "cf" else f"https://atcoder.jp/contests/{contest}/tasks_print?lang=en"
        try:
            raw, _ = downloader.fetch(url)
            blocks = extract_statements(raw.decode("utf-8"), platform)
            batch_error = ""
        except Exception as exc:
            blocks = {}
            batch_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        for item in items:
            error = batch_error
            html = ""
            block = blocks.get(item["problem_index"])
            if not block:
                try:
                    raw, _ = downloader.fetch(item["url"])
                    if raw.startswith(b"%PDF-"):
                        local_url = downloader.store_asset(raw, ".pdf")
                        html = f'<p>此题由原平台以 PDF 提供，公式和配图已随文件保存在本机。</p><p><a href="{local_url}" target="_blank" rel="noreferrer">打开本地 PDF 题面 ↗</a></p>'
                        error = ""
                    else:
                        page = raw.decode("utf-8")
                        parser = Fragments("class" if platform == "cf" else "id", "problem-statement" if platform == "cf" else "task-statement")
                        parser.feed(page)
                        if len(parser.blocks) == 1:
                            block = parser.blocks[0]
                            limits = re.search(r'<p>(Time Limit:.*?)</p>', page)
                            if limits:
                                block = f'<p>{limits[1]}</p>' + block
                        elif platform == "cf":
                            # Some ICPC mirrors publish one original statement booklet.
                            pdf = re.search(r'href=["\']([^"\']+\.pdf)["\'][^>]*>\s*Statements', page)
                            if not pdf:
                                raise ValueError("原平台未提供可识别的题面或 PDF")
                            raw_pdf, name = downloader.fetch(urljoin(item["url"], unescape(pdf[1])), asset=True)
                            if not raw_pdf.startswith(b"%PDF-"):
                                raise ValueError("下载内容不是 PDF")
                            html = f'<p>原平台提供整场 PDF 题面，请在文件中查找题号 <b>{escape(item["problem_index"])}</b>。</p><p><a href="/assets/{name}" target="_blank" rel="noreferrer">打开本地 PDF 题面 ↗</a></p>'
                            error = ""
                        else:
                            raise ValueError("原平台未提供可识别的题面")
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:240]}"
            if block:
                parser = Sanitize(item["url"], downloader)
                parser.feed(block)
                html = "".join(parser.parts)
                error = "; ".join(parser.errors)
            elif not error and not html:
                error = "整场页面未找到对应题号"
            status = "ready" if html and not error else "partial" if html else "failed"
            with db.conn:
                db.conn.execute("""INSERT INTO statements(platform,problem_id,html,status,error,fetched_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(platform,problem_id) DO UPDATE SET html=CASE WHEN excluded.html='' THEN statements.html ELSE excluded.html END,
                    status=excluded.status,error=excluded.error,fetched_at=excluded.fetched_at""",
                    (platform,item["problem_id"],html,status,error,int(time.time())))
        progress(f"{platform} {number}/{len(groups)} {contest}: {len(items)} tasks" + (f" — {batch_error}" if batch_error else ""))


def archive_status(db: Database) -> dict:
    result = {}
    for platform in SINCE:
        row = db.conn.execute("""SELECT COUNT(*) total, SUM(p.difficulty IS NOT NULL) rated,
            SUM(s.status='ready') ready, SUM(s.status='partial') partial, SUM(s.status='failed') failed
            FROM problems p LEFT JOIN statements s USING(platform,problem_id) WHERE p.platform=? AND p.is_real=1""", (platform,)).fetchone()
        result[platform] = {key: value or 0 for key, value in dict(row).items()}
        result[platform]["since"] = SINCE[platform]
    return result


def main():
    from .cli import _default_home
    parser = argparse.ArgumentParser(description="同步指定日期以来的真实题库与离线题面，可安全续传")
    parser.add_argument("--home", type=Path, default=_default_home())
    parser.add_argument("--platform", choices=["all", "cf", "atcoder"], default="all")
    parser.add_argument("--statements-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="只处理指定场数；0 为全部")
    args = parser.parse_args()
    paths = Paths(args.home.expanduser().resolve())
    paths.ensure()
    db = Database(paths.database)
    db.migrate()
    config = Config.load(paths.config)
    try:
        for platform in ([args.platform] if args.platform != "all" else ["cf", "atcoder"]):
            with archive_lock(paths, platform):
                if not args.statements_only:
                    sync = sync_codeforces if platform == "cf" else sync_atcoder
                    print(sync(db, config, CachedHttpClient(paths.cache), force=True), flush=True)
                archive_statements(db, paths, platform, lambda msg: print(msg, flush=True), args.limit)
        status = archive_status(db)
        print(json.dumps(status, ensure_ascii=False), flush=True)
        selected = status.values() if args.platform == "all" else [status[args.platform]]
        if any(s["ready"] < s["total"] or not s["total"] for s in selected):
            raise SystemExit(2)
    finally:
        db.close()


if __name__ == "__main__":
    main()
