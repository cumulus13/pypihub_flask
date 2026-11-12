#!/usr/bin/env python3

# File: pypihub/pkgstats.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Download statistics for the package page, copied from pypistats.org into the database and a cache
# License: LGPL-3.0-or-later

"""
Download counts shown on a package page (last day, week and month, and a 30 day chart).

pypi.org's own JSON API stopped carrying download counts (``info.downloads`` is always -1), so they come from
pypistats.org, the public service that publishes them:

    GET {PYPIHUB_STATS_URL}/packages/<name>/recent    {"data": {"last_day": n, "last_week": n, "last_month": n}, ...}
    GET {PYPIHUB_STATS_URL}/packages/<name>/overall   {"data": [{"category": "...", "date": "YYYY-MM-DD", "downloads": n}]}

The counts are PyPI's, for every version of the project. They are not downloads from this server.

pypistats.org limits requests per client address (about 5 per second and 30 per minute at the time of writing),
so a request is never made when a stored copy will do. The copy lives in two places, like the package details:

* the database (table ``pypi_stats``), fresh for ``PYPIHUB_STATS_TTL`` (default 6 hours);
* a cache (Redis when configured), fresh for ``PYPIHUB_STATS_CACHE_TTL`` (default 1 hour). When it expires the next
  view reads the database and refills the cache, and pypistats.org is not asked.

When pypistats.org fails or answers 429, all workers (through the shared cache) stop asking for
``PYPIHUB_STATS_BACKOFF`` seconds and show the stored copy, with a note. A page never fails because of statistics.

    PYPIHUB_STATS=0               switch the feature off
    PYPIHUB_STATS_URL             API base URL. Default https://pypistats.org/api when the upstream is pypi.org,
                                  none for another upstream (a mirror has no statistics)
    PYPIHUB_STATS_TTL             seconds a database copy is considered fresh (default 21600)
    PYPIHUB_STATS_CACHE_TTL       seconds a copy stays in the cache, at most PYPIHUB_STATS_TTL (default 3600, 0 = none)
    PYPIHUB_STATS_MISSING_TTL     seconds "pypistats.org does not know this project" is remembered (default 3600)
    PYPIHUB_STATS_TIMEOUT         "connect,read" seconds (default "2,5"): a first view waits at most this long
    PYPIHUB_STATS_BACKOFF         seconds to stop asking after a failure or a 429 (default 60, 0 disables)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import requests
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

try:
    from . import pypimeta
    from .cache import MemoryCache
    from .database import PypiStats, utcnow
    from .pypimeta import MetaResult
    from .upstream import Upstream
except ImportError:  # run as a script from inside the package directory
    import pypimeta  # type: ignore
    from cache import MemoryCache  # type: ignore
    from database import PypiStats, utcnow  # type: ignore
    from pypimeta import MetaResult  # type: ignore
    from upstream import Upstream  # type: ignore

log = logging.getLogger(__name__)

SCHEMA = 1
DEFAULT_URL = "https://pypistats.org/api"
CHART_DAYS = 30
MAX_BODY = 2_000_000  # bytes; a real "overall" answer is about 30 kB
MAX_BACKOFF = 600.0


class StatsError(Exception):
    """pypistats.org could not be reached, throttled the request, or sent something unusable."""


# ---------------------------------------------------------------------------
# Reducing the API answers
# ---------------------------------------------------------------------------

def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StatsError(f"not a download count: {value!r}")
    return value


def curate_recent(document: Any) -> Dict[str, Any]:
    """``{"last_day": n, "last_week": n, "last_month": n}`` from the ``recent`` answer."""
    try:
        data = document["data"]
        return {
            "schema": SCHEMA,
            "last_day": _count(data["last_day"]),
            "last_week": _count(data["last_week"]),
            "last_month": _count(data["last_month"]),
        }
    except (KeyError, TypeError) as exc:
        raise StatsError(f"unexpected 'recent' document: {exc!r}") from exc


def curate_daily(document: Any, days: int = CHART_DAYS) -> List[List[Any]]:
    """The newest ``days`` entries of the ``overall`` series as ``[["YYYY-MM-DD", downloads], ...]``.

    The series without mirror traffic is used when there is one: mirrors re-download everything and say little about
    use. Days the service lists no row for are left out, not invented as zero.
    """
    try:
        rows = document["data"]
        if not isinstance(rows, list):
            raise TypeError("data is not a list")
    except (KeyError, TypeError) as exc:
        raise StatsError(f"unexpected 'overall' document: {exc!r}") from exc
    by_category: Dict[str, Dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row["date"]))
            count = _count(row["downloads"])
        except (KeyError, ValueError, StatsError):
            continue
        by_category.setdefault(str(row.get("category") or ""), {})[day.isoformat()] = count
    series = by_category.get("without_mirrors") or by_category.get("with_mirrors")
    if not series:  # a category this code does not know: better one honest series than a guess
        series = next(iter(by_category.values()), {})
    return [[day, series[day]] for day in sorted(series)][-days:]


def sparkline(daily: List[List[Any]], width: int = 180, height: int = 40, pad: int = 2) -> str:
    """``points`` for an SVG polyline: days spaced by date, scaled from zero to the busiest day.

    Empty when there are fewer than two days. Drawn on the server, so the page needs no script.
    """
    points: List[Tuple[int, int]] = []
    for item in daily:
        try:
            points.append((date.fromisoformat(item[0]).toordinal(), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if len(points) < 2:
        return ""
    first, last = points[0][0], points[-1][0]
    span = max(1, last - first)
    peak = max(count for _, count in points)
    usable_w, usable_h = width - 2 * pad, height - 2 * pad
    coords = []
    for ordinal, count in points:
        x = pad + usable_w * (ordinal - first) / span
        y = height - pad - (usable_h * count / peak if peak else 0)
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def default_url(simple_url: Optional[str]) -> Optional[str]:
    """``PYPIHUB_STATS_URL``, else pypistats.org when the upstream is pypi.org, else None."""
    configured = str(os.getenv("PYPIHUB_STATS_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    host = (urlsplit(simple_url or "").hostname or "").lower()
    return DEFAULT_URL if host in ("pypi.org", "www.pypi.org") else None


# ---------------------------------------------------------------------------
# Service: cache, then database, then pypistats.org
# ---------------------------------------------------------------------------

def _timeout() -> Tuple[float, float]:
    raw = os.getenv("PYPIHUB_STATS_TIMEOUT", "2,5")
    parts = list(raw) if isinstance(raw, (list, tuple)) else str(raw).split(",")  # envdot may have made it a list
    try:
        connect, read = (float(part) for part in parts)
        return connect, read
    except (TypeError, ValueError):
        return 2.0, 5.0


class StatsService:
    """``get(key)`` returns a :class:`~pypihub.pypimeta.MetaResult` whose ``data`` is the reduced statistics.

    ``state`` is fresh, stale (pypistats.org failed, an older copy is shown), missing (pypistats.org does not know the
    project), unavailable (nothing stored and pypistats.org failed) or disabled.
    """

    def __init__(self, session_factory, kv, base_url: Optional[str], session: Optional[requests.Session] = None) -> None:
        self._session = session_factory
        self._kv = kv if kv is not None else MemoryCache(max_items=512)
        self._base = base_url.rstrip("/") if base_url else None
        self._http = session or Upstream._make_session()  # same user agent and retry rules as the PyPI client
        self._ttl = pypimeta._float_env("PYPIHUB_STATS_TTL", 21600)
        self._missing_ttl = pypimeta._float_env("PYPIHUB_STATS_MISSING_TTL", 3600)
        self._cache_ttl = self._cache_seconds(self._ttl)
        self._backoff = pypimeta._float_env("PYPIHUB_STATS_BACKOFF", 60)
        self._timeout = _timeout()
        self._on = pypimeta._flag_env("PYPIHUB_STATS")
        self._locks = [threading.Lock() for _ in range(64)]

    @staticmethod
    def _cache_seconds(database_ttl: float) -> float:
        wanted = pypimeta._float_env("PYPIHUB_STATS_CACHE_TTL", 3600)
        if wanted < 0:
            log.warning("PYPIHUB_STATS_CACHE_TTL must not be negative; using 3600")
            wanted = 3600
        if wanted > database_ttl:
            log.warning("PYPIHUB_STATS_CACHE_TTL (%.0f s) is longer than PYPIHUB_STATS_TTL (%.0f s); using %.0f s",
                        wanted, database_ttl, database_ttl)
        return min(wanted, max(database_ttl, 0.0))

    @property
    def enabled(self) -> bool:
        return self._on and self._base is not None

    def get(self, key: str) -> MetaResult:
        if not self.enabled:
            return MetaResult(None, "disabled")
        kv_key = f"stats:{key}"
        cached = self._kv.get(kv_key)
        if cached:
            return MetaResult.load(cached)
        with self._locks[hash(key) % len(self._locks)]:  # one request per worker, not one per visitor
            cached = self._kv.get(kv_key)
            if cached:
                return MetaResult.load(cached)
            row = self._load(key)
            if row is not None:
                age = (utcnow() - row.fetched_at).total_seconds()
                limit = self._missing_ttl if row.status == "missing" else self._ttl
                if age < limit:
                    result = self._from_row(row, "fresh" if row.status == "ok" else "missing")
                    self._remember(kv_key, result, limit - age)
                    return result
            try:
                result = self._fetch(key)
            except StatsError as exc:
                log.warning("Could not refresh download statistics for %s: %s", key, exc)
                if row is not None and row.status == "ok":
                    return self._from_row(row, "stale")
                if row is not None:
                    return self._from_row(row, "missing")
                return MetaResult(None, "unavailable")
            self._store(key, result)
            self._remember(kv_key, result, self._ttl if result.state == "fresh" else self._missing_ttl)
            return result

    # -- pypistats.org --------------------------------------------------------

    def _trip(self, reason: object, seconds: Optional[float] = None) -> None:
        wait = min(seconds if seconds else self._backoff, MAX_BACKOFF)
        if self._backoff > 0 and wait > 0:
            log.warning("pypistats.org failed (%s). Not asking again for %.0f s.", reason, wait)
            self._kv.set("stats-down", str(reason)[:200], wait)

    def _request(self, key: str, kind: str):
        """``(status, document)``: 200 with a document, or 404. Raises :class:`StatsError` on anything else."""
        if self._backoff > 0 and self._kv.get("stats-down") is not None:
            raise StatsError("pypistats.org failed a moment ago; not trying again yet")
        url = f"{self._base}/packages/{quote(key, safe='')}/{kind}"
        try:
            response = self._http.get(url, headers={"Accept": "application/json"}, timeout=self._timeout)
        except requests.RequestException as exc:
            self._trip(exc)
            raise StatsError(str(exc)) from exc
        status = response.status_code
        if status == 404:
            return 404, None
        if status == 429:
            try:
                wait: Optional[float] = float(response.headers.get("Retry-After", ""))
            except ValueError:
                wait = None
            self._trip(f"{url} answered 429", wait)
            raise StatsError(f"{url} answered 429 (rate limited)")
        if status != 200:
            if status >= 500:
                self._trip(f"{url} answered {status}")
            raise StatsError(f"{url} answered {status}")
        if len(response.content) > MAX_BODY:
            raise StatsError(f"{url} answered with {len(response.content)} bytes")
        try:
            return 200, response.json()
        except ValueError as exc:
            raise StatsError(f"{url} did not return JSON") from exc

    def _fetch(self, key: str) -> MetaResult:
        status, recent = self._request(key, "recent")
        if status == 404:
            return MetaResult(None, "missing", utcnow())
        data = curate_recent(recent)
        data["daily"] = []
        try:
            status, overall = self._request(key, "overall")
            if status == 200:
                data["daily"] = curate_daily(overall)
        except StatsError as exc:  # the totals are the point; the chart is a bonus and can wait for the next refresh
            log.info("No daily series for %s: %s", key, exc)
        return MetaResult(data, "fresh", utcnow())

    # -- database -------------------------------------------------------------

    def _load(self, key: str) -> Optional[PypiStats]:
        """The stored row, or None when there is none or it cannot be read (it is then fetched again and rewritten)."""
        try:
            with self._session() as session:
                row = session.execute(select(PypiStats).where(PypiStats.project_key == key)).scalar_one_or_none()
        except SQLAlchemyError as exc:
            log.error("Could not read stored download statistics: %s", exc)
            return None
        if row is not None and row.status == "ok":
            try:
                if not isinstance(json.loads(row.data or ""), dict):
                    raise ValueError("not an object")
            except ValueError:
                log.warning("Stored download statistics for %s are unreadable; fetching them again", key)
                return None
        return row

    @staticmethod
    def _from_row(row: PypiStats, state: str) -> MetaResult:
        return MetaResult(json.loads(row.data) if row.status == "ok" and row.data else None, state, row.fetched_at)

    def _store(self, key: str, result: MetaResult) -> None:
        payload = json.dumps(result.data, separators=(",", ":")) if result.data is not None else None
        values = {"status": "ok" if result.state == "fresh" else "missing", "data": payload,
                  "fetched_at": result.fetched_at or utcnow()}
        where = (PypiStats.project_key == key,)
        try:
            with self._session() as session:
                if session.execute(update(PypiStats).where(*where).values(**values)).rowcount:
                    session.commit()
                    return
                session.add(PypiStats(project_key=key, **values))
                try:
                    session.commit()
                except IntegrityError:  # another worker stored it first
                    session.rollback()
                    session.execute(update(PypiStats).where(*where).values(**values))
                    session.commit()
        except SQLAlchemyError as exc:
            log.error("Could not store download statistics for %s: %s", key, exc)

    def _remember(self, kv_key: str, result: MetaResult, ttl: float) -> None:
        """Hot cache: for as long as the database copy is still fresh, but never longer than the cache's own limit."""
        keep = min(ttl, self._cache_ttl)
        if keep > 0:
            self._kv.set(kv_key, result.dump(), keep)
