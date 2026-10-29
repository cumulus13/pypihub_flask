#!/usr/bin/env python3

# File: pypihub/upstream.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Client for the upstream simple index (pypi.org): timeouts, retries, cached index, stale-if-error
# License: LGPL-3.0-or-later

"""
Everything PyPihub asks of PyPI goes through here.

* Every request has a timeout and a bounded retry, so a slow or dead upstream cannot hang a worker.
* A project's file list is cached (``PYPIHUB_UPSTREAM_TTL``, default 300 s). A longer-lived copy is
  kept as well and used when the upstream is down, so already-known projects keep resolving.
* Refreshing asks "has it changed?" with the ETag the upstream sent (``If-None-Match``). pypi.org answers
  304 with an empty body in about 15 ms, where the full document can be hundreds of kB (numpy: 3.7 MB).
* The JSON simple API (PEP 691) is used when the upstream offers it, with a fallback to HTML (PEP 503)
  for plain mirrors. ``requires-python`` and ``yanked`` are passed through so pip picks compatible releases.

Environment
    PYPIHUB_UPSTREAM_TTL        seconds a project's file list is reused (default 300)
    PYPIHUB_UPSTREAM_STALE_TTL  seconds a copy may be used while the upstream is failing (default 86400)
    PYPIHUB_UPSTREAM_TIMEOUT    "connect,read" seconds (default "3.05,20")
    PYPIHUB_UPSTREAM_BACKOFF    seconds to fail fast after the upstream failed (default 15, 0 disables)

A hung upstream would otherwise hold one worker thread for the whole timeout on every request and could
freeze the site. After a failure all workers (through the shared cache) skip the network for a short while
and answer from stored copies, or with 502, immediately.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from .dist import project_key
except ImportError:  # run as a script from inside the package directory
    from dist import project_key  # type: ignore

log = logging.getLogger(__name__)

ACCEPT = (
    "application/vnd.pypi.simple.v1+json, "
    "application/vnd.pypi.simple.v1+html;q=0.2, text/html;q=0.01"
)


NOT_MODIFIED = object()


class UpstreamError(Exception):
    """The upstream could not be reached or answered with a server error."""


@dataclass(frozen=True)
class UpstreamFile:
    filename: str
    url: str
    sha256: Optional[str] = None
    requires_python: Optional[str] = None
    yanked: Optional[str] = None  # None when not yanked, else the reason (possibly empty)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _timeout() -> Tuple[float, float]:
    raw = os.getenv("PYPIHUB_UPSTREAM_TIMEOUT", "3.05,20")
    try:
        connect, read = (float(part) for part in raw)
        return connect, read
    except ValueError:
        return 3.05, 20.0


def _filename_from_url(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def parse_json(payload: dict) -> List[UpstreamFile]:
    files = []
    for item in payload.get("files", []):
        filename, url = item.get("filename"), item.get("url")
        if not filename or not url:
            continue
        yanked = item.get("yanked")
        files.append(
            UpstreamFile(
                filename=filename,
                url=url,
                sha256=(item.get("hashes") or {}).get("sha256"),
                requires_python=item.get("requires-python") or None,
                yanked=(yanked if isinstance(yanked, str) else "") if yanked else None,
            )
        )
    return files


def parse_html(text: str, base_url: str) -> List[UpstreamFile]:
    files = []
    for anchor in BeautifulSoup(text, "html.parser").find_all("a"):
        href = anchor.get("href")
        if not href:
            continue
        absolute, fragment = urldefrag(urljoin(base_url, href))
        filename = _filename_from_url(absolute)
        if not filename:
            continue
        sha256 = fragment.split("=", 1)[1] if fragment.startswith("sha256=") else None
        yanked = anchor.get("data-yanked")
        files.append(
            UpstreamFile(
                filename=filename,
                url=absolute,
                sha256=sha256,
                requires_python=anchor.get("data-requires-python") or None,
                yanked=yanked if yanked is not None else None,
            )
        )
    return files


class Upstream:
    def __init__(self, simple_url: str, cache, session: Optional[requests.Session] = None) -> None:
        self._base = simple_url.rstrip("/")
        self._cache = cache
        self._ttl = _float_env("PYPIHUB_UPSTREAM_TTL", 300)
        self._stale_ttl = _float_env("PYPIHUB_UPSTREAM_STALE_TTL", 86400)
        self._timeout = _timeout()
        self._backoff = _float_env("PYPIHUB_UPSTREAM_BACKOFF", 15)
        self._session = session or self._make_session()

    @staticmethod
    def _make_session() -> requests.Session:
        session = requests.Session()
        # Retry connection errors and 502/503/504, not read timeouts: repeating a 20 s wait three times helps nobody.
        retry = Retry(total=2, connect=2, read=0, status=2, backoff_factor=0.3,
                      status_forcelist=(502, 503, 504), allowed_methods=("GET",))
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=64)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        try:
            from importlib.metadata import version

            agent = f"pypihub/{version('pypihub')}"
        except Exception:
            agent = "pypihub"
        session.headers["User-Agent"] = agent
        return session

    # -- circuit breaker ------------------------------------------------------

    def _check_breaker(self) -> None:
        if self._backoff > 0 and self._cache.get("up-down") is not None:
            raise UpstreamError("the upstream failed a moment ago; not trying again yet")

    def _trip(self, reason: object) -> None:
        if self._backoff > 0:
            log.warning("Upstream failure (%s). Skipping the network for %.0f s.", reason, self._backoff)
            self._cache.set("up-down", str(reason)[:200], self._backoff)

    # -- project index --------------------------------------------------------

    def files(self, project: str) -> Optional[List[UpstreamFile]]:
        """Files the upstream lists for a project; None when the upstream has no such project.

        Raises :class:`UpstreamError` only when the upstream is failing and no earlier copy exists.
        """
        key = project_key(project)
        fresh = self._cache.get(f"up:{key}")
        if fresh is not None:
            return self._decode(fresh)[0]
        stale = self._cache.get(f"up-stale:{key}")
        known, etag = self._decode(stale) if stale is not None else (None, None)
        try:
            self._check_breaker()
            outcome = self._fetch(key, etag if known is not None else None)
        except (requests.RequestException, UpstreamError) as exc:
            if known is not None:
                log.warning("Upstream failed for %s (%s); serving the stored file list", key, exc)
                return known
            raise UpstreamError(str(exc)) from exc
        files, etag = (known, etag) if outcome is NOT_MODIFIED else outcome
        encoded = self._encode(files, etag)
        if files is None:
            self._cache.set(f"up:{key}", encoded, min(60.0, self._ttl))  # remember "not found" briefly
        else:
            self._cache.set(f"up:{key}", encoded, self._ttl)
            self._cache.set(f"up-stale:{key}", encoded, self._stale_ttl)
        return files

    def _fetch(self, key: str, etag: Optional[str] = None):
        """``(files, etag)``, or NOT_MODIFIED when ``etag`` is still current."""
        url = f"{self._base}/{key}/"
        headers = {"Accept": ACCEPT}
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self._session.get(url, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            self._trip(exc)
            raise
        if response.status_code == 304 and etag:
            return NOT_MODIFIED
        if response.status_code == 404:
            return None, None
        if response.status_code != 200:
            if response.status_code >= 500:
                self._trip(f"{url} answered {response.status_code}")
            raise UpstreamError(f"{url} answered {response.status_code}")
        new_etag = response.headers.get("ETag")
        if "json" in response.headers.get("Content-Type", ""):
            return parse_json(response.json()), new_etag
        return parse_html(response.text, response.url), new_etag

    @staticmethod
    def _encode(files: Optional[List[UpstreamFile]], etag: Optional[str] = None) -> str:
        return json.dumps({"etag": etag, "files": None if files is None else [asdict(f) for f in files]})

    @staticmethod
    def _decode(raw: str):
        """``(files, etag)``. Also reads entries written before ETags were stored (a bare list or null)."""
        data = json.loads(raw)
        etag, items = (data.get("etag"), data.get("files")) if isinstance(data, dict) else (None, data)
        return (None if items is None else [UpstreamFile(**item) for item in items]), etag

    def find(self, project: str, filename: str) -> Optional[UpstreamFile]:
        for item in self.files(project) or []:
            if item.filename == filename:
                return item
        return None

    # -- JSON API (project details) -------------------------------------------

    def json_url(self, project: str, version: Optional[str] = None) -> Optional[str]:
        """URL of the upstream's JSON API for a project, or None when it has none (a plain mirror)."""
        base = os.getenv("PYPI_JSON_URL")
        if not base and self._base.endswith("/simple"):
            base = self._base[: -len("/simple")] + "/pypi"
        if not base:
            return None
        key = project_key(project)
        return f"{base.rstrip('/')}/{key}/{version}/json" if version else f"{base.rstrip('/')}/{key}/json"

    def get_json(self, url: str, etag: Optional[str] = None):
        """``(status, document, etag)``: 200 with a document, 304 when ``etag`` is still current, or 404.

        Raises :class:`UpstreamError` on any other outcome.
        """
        self._check_breaker()
        headers = {"Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self._session.get(url, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            self._trip(exc)
            raise UpstreamError(str(exc)) from exc
        if response.status_code == 304 and etag:
            return 304, None, etag
        if response.status_code == 404:
            return 404, None, None
        if response.status_code != 200:
            if response.status_code >= 500:
                self._trip(f"{url} answered {response.status_code}")
            raise UpstreamError(f"{url} answered {response.status_code}")
        try:
            return 200, response.json(), response.headers.get("ETag")
        except ValueError as exc:
            raise UpstreamError(f"{url} did not return JSON") from exc

    # -- file download --------------------------------------------------------

    def open(self, url: str) -> requests.Response:
        """Start downloading a file. The caller must close the response."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise UpstreamError(f"Refusing to fetch {url!r}")
        self._check_breaker()
        try:
            response = self._session.get(url, stream=True, timeout=self._timeout)
        except requests.RequestException as exc:
            self._trip(exc)
            raise UpstreamError(str(exc)) from exc
        if response.status_code != 200:
            status = response.status_code
            response.close()
            if status >= 500:
                self._trip(f"{url} answered {status}")
            raise UpstreamError(f"{url} answered {status}")
        return response
