#!/usr/bin/env python3

# File: pypihub/pypimeta.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Project details (README, links, classifiers, release history) copied from pypi.org into the database
# License: LGPL-3.0-or-later

"""
What a package page shows besides the files PyPihub stores: the details pypi.org shows.

They come from the upstream's JSON API, are reduced to the fields the page uses, and kept in two places:

* the database (table ``pypi_meta``), which survives restarts and is the fallback while the upstream is down;
* a cache (Redis when configured, otherwise a small in-process one) for the hot path.

A copy is used for ``PYPIHUB_META_TTL`` seconds (default 6 hours), then re-checked. The re-check sends the ETag
stored with the copy: if PyPI answers 304 (unchanged, no body) only the timestamp moves; the document is downloaded,
reduced and rendered again only when it really changed. If the check fails, the older copy is shown with a note
instead of an error. Only projects that PyPihub stores are ever looked up, so strangers
cannot make the database grow by asking for random names.

    PYPIHUB_META_TTL           seconds a copy is considered fresh (default 21600)
    PYPIHUB_META_MISSING_TTL   seconds "the upstream does not know this project" is remembered (default 3600)
    PYPIHUB_META=0             switch the feature off
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from email.utils import getaddresses
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

try:
    from . import readme
    from .cache import MemoryCache
    from .database import PypiMeta, utcnow
    from .upstream import UpstreamError
except ImportError:  # run as a script from inside the package directory
    import readme  # type: ignore
    from cache import MemoryCache  # type: ignore
    from database import PypiMeta, utcnow  # type: ignore
    from upstream import UpstreamError  # type: ignore

log = logging.getLogger(__name__)

SCHEMA = 1
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}$")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_EXTRA_RE = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")

MAX_DESCRIPTION = 1_000_000
MAX_RELEASES = 3000


# ---------------------------------------------------------------------------
# Reducing the upstream document
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def safe_url(value: Any) -> Optional[str]:
    """Only http(s) links are shown as links; ``javascript:`` and friends are dropped."""
    if not isinstance(value, str) or len(value) > 500:
        return None
    parts = urlsplit(value.strip())
    return value.strip() if parts.scheme in ("http", "https") and parts.netloc else None


def _people(names: Any, emails: Any) -> List[Dict[str, Optional[str]]]:
    """``Author`` and ``Author-email`` as pypi.org shows them: a name, linked when an address is known."""
    found: List[Dict[str, Optional[str]]] = []
    for name, address in getaddresses([emails] if isinstance(emails, str) and emails else []):
        address = address.strip()
        found.append({"name": name.strip() or None, "email": address if _EMAIL_RE.match(address) else None})
    if not any(person["name"] for person in found) and isinstance(names, str) and names.strip():
        listed = [n.strip() for n in names.split(",") if n.strip()]
        found = [{"name": n, "email": None} for n in listed[:10]] + [p for p in found if p["email"]][:3]
    return [p for p in found if p["name"] or p["email"]][:10]


def _license(info: Dict[str, Any]) -> str:
    expression = _text(info.get("license_expression"), 200)
    if expression:
        return expression
    text = _text(info.get("license"), 4000)
    if text:
        first = text.splitlines()[0].strip()
        return first[:120] + ("…" if len(first) > 120 or "\n" in text else "")
    for classifier in info.get("classifiers") or []:
        if isinstance(classifier, str) and classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip()
    return ""


def _keywords(value: Any) -> List[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    parts = value.split(",") if "," in value else value.split()
    return [p.strip() for p in parts if p.strip()][:30]


def _file(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "filename": _text(item.get("filename"), 255),
        "size": int(item.get("size") or 0),
        "uploaded": item.get("upload_time_iso_8601") or None,
        "sha256": (item.get("digests") or {}).get("sha256"),
        "packagetype": item.get("packagetype") or "",
        "python_version": item.get("python_version") or "",
        "requires_python": item.get("requires_python") or "",
        "yanked": bool(item.get("yanked")),
        "yanked_reason": _text(item.get("yanked_reason"), 300),
    }


def _version_key(version: str):
    try:
        return (1, Version(version))
    except InvalidVersion:
        return (0, Version("0"))


def _releases(releases: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows = []
    for version, files in releases.items():
        if not isinstance(files, list) or not VERSION_RE.match(version):
            continue
        times = sorted(f.get("upload_time_iso_8601") for f in files if f.get("upload_time_iso_8601"))
        try:
            pre = Version(version).is_prerelease
        except InvalidVersion:
            pre = False
        rows.append({
            "version": version,
            "uploaded": times[0] if times else None,
            "files": len(files),
            "yanked": bool(files) and all(f.get("yanked") for f in files),
            "pre": pre,
        })
    # newest first; versions that cannot be parsed go last, ordered by upload time
    rows.sort(key=lambda r: (_version_key(r["version"]), r["uploaded"] or ""), reverse=True)
    return rows[:MAX_RELEASES]


def _vulnerabilities(items: Any) -> List[Dict[str, Any]]:
    found = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("withdrawn"):
            continue
        found.append({
            "id": _text(item.get("id"), 80),
            "summary": _text(item.get("summary") or item.get("details"), 300),
            "link": safe_url(item.get("link")),
            "fixed_in": [_text(v, 40) for v in (item.get("fixed_in") or []) if isinstance(v, str)][:10],
        })
    return found[:25]


def split_requirements(requires_dist: List[str]) -> Dict[str, List[str]]:
    """``{"": [always needed], "socks": [needed for the socks extra], ...}``"""
    groups: Dict[str, List[str]] = {}
    for line in requires_dist:
        match = _EXTRA_RE.search(line)
        groups.setdefault(match.group(1) if match else "", []).append(line)
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] != "", kv[0])))


def curate(document: Dict[str, Any], with_releases: bool) -> Dict[str, Any]:
    """Reduce a JSON API document to what the page needs. Raises KeyError or TypeError on a malformed document."""
    info = document["info"]
    description = info.get("description") if isinstance(info.get("description"), str) else ""
    description = description[:MAX_DESCRIPTION]
    content_type = info.get("description_content_type") if isinstance(info.get("description_content_type"), str) else None
    project_urls = {
        _text(label, 60): url
        for label, url in (info.get("project_urls") or {}).items()
        if isinstance(label, str) and safe_url(url)
    }
    home = safe_url(info.get("home_page"))
    if home and home not in project_urls.values():
        project_urls = {"Homepage": home, **project_urls}
    data: Dict[str, Any] = {
        "schema": SCHEMA,
        "name": _text(info.get("name"), 128),
        "version": _text(info.get("version"), 128),
        "summary": _text(info.get("summary")),
        "description": description,
        "description_content_type": content_type,
        "description_html": readme.render(description, content_type),
        "authors": _people(info.get("author"), info.get("author_email")),
        "maintainers": _people(info.get("maintainer"), info.get("maintainer_email")),
        "license": _license(info),
        "project_urls": dict(list(project_urls.items())[:20]),
        "classifiers": [c for c in (info.get("classifiers") or []) if isinstance(c, str)][:200],
        "keywords": _keywords(info.get("keywords")),
        "requires_python": _text(info.get("requires_python"), 100),
        "requires_dist": [r for r in (info.get("requires_dist") or []) if isinstance(r, str)][:300],
        "yanked": bool(info.get("yanked")),
        "yanked_reason": _text(info.get("yanked_reason"), 300),
        "files": [_file(f) for f in (document.get("urls") or []) if isinstance(f, dict)][:500],
        "vulnerabilities": _vulnerabilities(document.get("vulnerabilities")),
    }
    if with_releases:
        data["releases"] = _releases(document.get("releases") or {})
    return data


def group_classifiers(classifiers: List[str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for classifier in classifiers:
        head, _, rest = classifier.partition(" :: ")
        groups.setdefault(head, []).append(rest or head)
    return groups


# ---------------------------------------------------------------------------
# Service: cache, then database, then upstream
# ---------------------------------------------------------------------------

class MetaResult:
    """``state`` is fresh, stale (the upstream failed, an older copy is shown), missing (the upstream does not
    know the project), unavailable (nothing stored and the upstream failed) or disabled."""

    __slots__ = ("data", "state", "fetched_at")

    def __init__(self, data: Optional[Dict[str, Any]], state: str, fetched_at: Optional[datetime] = None) -> None:
        self.data, self.state, self.fetched_at = data, state, fetched_at

    def dump(self) -> str:
        return json.dumps({"data": self.data, "state": self.state,
                           "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None})

    @classmethod
    def load(cls, raw: str) -> "MetaResult":
        payload = json.loads(raw)
        stamp = payload.get("fetched_at")
        return cls(payload.get("data"), payload["state"], datetime.fromisoformat(stamp) if stamp else None)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class MetaService:
    def __init__(self, session_factory, kv, upstream) -> None:
        self._session = session_factory
        # READMEs are large: never let them fill a worker's memory. Redis, if configured, holds them instead.
        self._kv = kv if getattr(kv, "kind", "") == "redis" else MemoryCache(max_items=200)
        self._upstream = upstream
        self._ttl = _float_env("PYPIHUB_META_TTL", 21600)
        self._missing_ttl = _float_env("PYPIHUB_META_MISSING_TTL", 3600)
        self._locks = [threading.Lock() for _ in range(64)]
        self._on = os.getenv("PYPIHUB_META", "1")

    @property
    def enabled(self) -> bool:
        return self._on and self._upstream.json_url("x") is not None

    def get(self, key: str, version: Optional[str] = None) -> MetaResult:
        """Details for a project's latest release (``version=None``) or for one release."""
        if not self.enabled:
            return MetaResult(None, "disabled")
        version = version or ""
        kv_key = f"meta:{key}:{version}"
        cached = self._kv.get(kv_key)
        if cached:
            return MetaResult.load(cached)
        with self._locks[hash((key, version)) % len(self._locks)]:  # one fetch per worker, not one per visitor
            cached = self._kv.get(kv_key)
            if cached:
                return MetaResult.load(cached)
            row = self._load(key, version)
            if row is not None:
                age = (utcnow() - row.fetched_at).total_seconds()
                limit = self._missing_ttl if row.status == "missing" else self._ttl
                if age < limit:
                    result = self._from_row(row, "fresh" if row.status == "ok" else "missing")
                    self._remember(kv_key, result, limit - age)
                    return result
            etag = self._stored_etag(row)
            try:
                result = self._fetch(key, version, etag)
            except UpstreamError as exc:
                log.warning("Could not refresh details for %s %s: %s", key, version or "(latest)", exc)
                if row is not None and row.status == "ok":
                    return self._from_row(row, "stale")
                if row is not None:
                    return self._from_row(row, "missing")
                return MetaResult(None, "unavailable")
            if result is None:  # 304: PyPI says our copy is still current
                self._touch(key, version)
                result = self._from_row(row, "fresh")
                result.fetched_at = utcnow()
                self._remember(kv_key, result, self._ttl)
                return result
            self._store(key, version, result)
            self._remember(kv_key, result, self._ttl if result.state == "fresh" else self._missing_ttl)
            return result

    # -- upstream -----------------------------------------------------------

    @staticmethod
    def _stored_etag(row: Optional[PypiMeta]) -> Optional[str]:
        if row is None or row.status != "ok" or not row.data:
            return None
        try:
            return json.loads(row.data).get("_etag")
        except ValueError:
            return None

    def _fetch(self, key: str, version: str, etag: Optional[str] = None) -> Optional[MetaResult]:
        """A new result, or None when ``etag`` says the stored copy is still current."""
        url = self._upstream.json_url(key, version or None)
        status, document, new_etag = self._upstream.get_json(url, etag)
        if status == 304:
            return None
        if status == 404:
            return MetaResult(None, "missing", utcnow())
        try:
            data = curate(document, with_releases=not version)
        except (KeyError, TypeError, AttributeError) as exc:
            raise UpstreamError(f"unexpected document from {url}: {exc!r}") from exc
        if new_etag:
            data["_etag"] = new_etag  # kept with the data, so no extra column and no schema change
        return MetaResult(data, "fresh", utcnow())

    # -- database -----------------------------------------------------------

    def _load(self, key: str, version: str) -> Optional[PypiMeta]:
        try:
            with self._session() as session:
                return session.execute(
                    select(PypiMeta).where(PypiMeta.project_key == key, PypiMeta.version == version)
                ).scalar_one_or_none()
        except SQLAlchemyError as exc:
            log.error("Could not read stored details: %s", exc)
            return None

    @staticmethod
    def _from_row(row: PypiMeta, state: str) -> MetaResult:
        return MetaResult(json.loads(row.data) if row.data else None, state, row.fetched_at)

    def _store(self, key: str, version: str, result: MetaResult) -> None:
        payload = json.dumps(result.data, separators=(",", ":")) if result.data is not None else None
        status = "ok" if result.state == "fresh" else "missing"
        values = {"status": status, "data": payload, "fetched_at": result.fetched_at or utcnow()}
        where = (PypiMeta.project_key == key, PypiMeta.version == version)
        try:
            with self._session() as session:
                if session.execute(update(PypiMeta).where(*where).values(**values)).rowcount:
                    session.commit()
                    return
                session.add(PypiMeta(project_key=key, version=version, **values))
                try:
                    session.commit()
                except IntegrityError:  # another worker stored it first
                    session.rollback()
                    session.execute(update(PypiMeta).where(*where).values(**values))
                    session.commit()
        except SQLAlchemyError as exc:
            log.error("Could not store details for %s: %s", key, exc)

    def _touch(self, key: str, version: str) -> None:
        """The stored copy was confirmed current: restart its freshness period without rewriting the document."""
        try:
            with self._session() as session:
                session.execute(
                    update(PypiMeta).where(PypiMeta.project_key == key, PypiMeta.version == version).values(fetched_at=utcnow())
                )
                session.commit()
        except SQLAlchemyError as exc:
            log.error("Could not update the stored details for %s: %s", key, exc)

    def _remember(self, kv_key: str, result: MetaResult, ttl: float) -> None:
        if ttl > 0:
            self._kv.set(kv_key, result.dump(), min(ttl, self._ttl))
