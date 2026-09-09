from __future__ import annotations

from dataclasses import dataclass
import email.utils
import hashlib
import json
from pathlib import Path
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class HttpClientError(RuntimeError):
    pass


@dataclass(slots=True)
class CacheEntry:
    body_path: Path
    meta_path: Path


class CachedHttpClient:
    """Small polite JSON client with disk cache and per-host throttling."""

    def __init__(self, cache_dir: Path, user_agent: str = "cp-trainer-local/0.1"):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self._last_request: dict[str, float] = {}
        self._minimum_interval = {
            "codeforces.com": 2.05,
            "www.codeforces.com": 2.05,
            "kenkoooo.com": 1.10,
            "www.kenkoooo.com": 1.10,
            "atcoder.jp": 1.10,
        }

    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        ttl_seconds: int = 0,
        force: bool = False,
        retries: int = 3,
    ) -> Any:
        entry = self._entry(cache_key or url)
        meta = self._load_meta(entry)
        now = int(time.time())
        if (
            not force
            and ttl_seconds > 0
            and entry.body_path.exists()
            and now - int(meta.get("fetched_at", 0)) < ttl_seconds
        ):
            return json.loads(entry.body_path.read_text(encoding="utf-8"))

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if not force and entry.body_path.exists():
            if meta.get("etag"):
                headers["If-None-Match"] = str(meta["etag"])
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = str(meta["last_modified"])

        host = urlparse(url).hostname or ""
        last_error: Exception | None = None
        for attempt in range(retries):
            self._throttle(host)
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=45) as response:
                    raw = response.read()
                    text = raw.decode("utf-8")
                    data = json.loads(text)
                    entry.body_path.write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf-8"
                    )
                    self._save_meta(
                        entry,
                        {
                            "url": url,
                            "fetched_at": int(time.time()),
                            "etag": response.headers.get("ETag", ""),
                            "last_modified": response.headers.get("Last-Modified", ""),
                        },
                    )
                    return data
            except HTTPError as exc:
                if exc.code == 304 and entry.body_path.exists():
                    meta["fetched_at"] = int(time.time())
                    self._save_meta(entry, meta)
                    return json.loads(entry.body_path.read_text(encoding="utf-8"))
                if exc.code in {429, 500, 502, 503, 504}:
                    last_error = exc
                    self._backoff(attempt, exc.headers.get("Retry-After"))
                    continue
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise HttpClientError(f"HTTP {exc.code} for {url}: {body}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                self._backoff(attempt, None)
        if entry.body_path.exists() and not force:
            # A stale cache is preferable to losing an already synchronized bank.
            return json.loads(entry.body_path.read_text(encoding="utf-8"))
        raise HttpClientError(f"failed to fetch {url}: {last_error}")

    def _entry(self, key: str) -> CacheEntry:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return CacheEntry(
            body_path=self.cache_dir / f"{digest}.json",
            meta_path=self.cache_dir / f"{digest}.meta.json",
        )

    @staticmethod
    def _load_meta(entry: CacheEntry) -> dict[str, Any]:
        if not entry.meta_path.exists():
            return {}
        try:
            return json.loads(entry.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _save_meta(entry: CacheEntry, meta: dict[str, Any]) -> None:
        entry.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _throttle(self, host: str) -> None:
        interval = self._minimum_interval.get(host, 0.25)
        now = time.monotonic()
        wait = self._last_request.get(host, 0.0) + interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                delay = max(0.0, parsed.timestamp() - time.time())
        else:
            delay = (2**attempt) + random.random() * 0.5
        time.sleep(min(delay, 15.0))
