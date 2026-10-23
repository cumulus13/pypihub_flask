#!/usr/bin/env python3

# File: pypihub/catalog.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Database-backed catalog of stored files: paginated listing, statistics, reindex and cache pruning
# License: LGPL-3.0-or-later

"""
The catalog is what the web interface reads. Every stored file has one ``dist_files`` row, written
when a file is uploaded or cached and rebuilt from disk by :meth:`Catalog.reindex`.

The files on disk stay the source of truth for serving; the catalog only answers "what do we have",
so listing a page of packages is one indexed SQL query instead of a directory scan. Only portable SQL
is used (GROUP BY, CASE, COUNT DISTINCT, LIMIT/OFFSET), so it behaves the same on SQLite, PostgreSQL
and MySQL.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

from packaging.version import InvalidVersion, Version
from sqlalchemy import case, delete, desc, distinct, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

try:
    from .database import DistFile, utcnow
    from .dist import parse_dist_filename, project_key, valid_filename
except ImportError:  # run as a script from inside the package directory
    from database import DistFile, utcnow  # type: ignore
    from dist import parse_dist_filename, project_key, valid_filename  # type: ignore

log = logging.getLogger(__name__)

SORTS = ("name", "updated", "files", "size")
SOURCES = ("all", "local", "cached")


@dataclass
class ProjectRow:
    key: str
    name: str
    files: int
    size: int
    updated: datetime
    sources: List[str]
    latest: Optional[str] = None


@dataclass
class ProjectPage:
    items: List[ProjectRow]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def first(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.per_page + 1

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.per_page)


@dataclass
class FileRow:
    filename: str
    directory: str
    source: str
    kind: str
    version: Optional[str]
    size: int
    sha256: Optional[str]
    created_at: datetime


@dataclass
class ProjectDetail:
    key: str
    name: str
    files: List[FileRow] = field(default_factory=list)

    @property
    def sources(self) -> List[str]:
        return [s for s in ("local", "cached") if any(f.source == s for f in self.files)]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def latest(self) -> Optional[str]:
        return _latest(f.version for f in self.files)


@dataclass
class Stats:
    projects: int = 0
    local_projects: int = 0
    cached_projects: int = 0
    files: int = 0
    size: int = 0
    cached_size: int = 0


def _latest(versions) -> Optional[str]:
    parsed: List[Version] = []
    for raw in versions:
        if not raw:
            continue
        try:
            parsed.append(Version(raw))
        except InvalidVersion:
            continue
    if not parsed:
        return None
    stable = [v for v in parsed if not v.is_prerelease]
    return str(max(stable or parsed))


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _dist_values(project_dir: str, filename: str) -> dict:
    _, _, version = parse_dist_filename(filename)
    kind = parse_dist_filename(filename)[0]
    return {
        "project_key": project_key(project_dir),
        "project_name": project_dir,
        "filename": filename,
        "kind": kind,
        "version": str(version) if version is not None else None,
        "is_prerelease": bool(version is not None and version.is_prerelease),
    }


class Catalog:
    def __init__(self, session_factory, cache=None, stats_ttl: float = 10.0) -> None:
        self._session = session_factory
        self._cache = cache
        self._stats_ttl = stats_ttl

    # -- writing --------------------------------------------------------------

    def record_file(
        self,
        project_dir: str,
        filename: str,
        source: str,
        size: int,
        sha256: Optional[str] = None,
        uploaded_by: Optional[int] = None,
        created_at: Optional[datetime] = None,
    ) -> None:
        """Insert or update the row for one file. Safe when two workers record the same file at once."""
        values = _dist_values(project_dir, filename)
        where = (
            DistFile.source == source,
            DistFile.project_name == project_dir,
            DistFile.filename == filename,
        )
        changed = {k: v for k, v in values.items() if k not in ("project_name", "filename")}
        changed.update(size=size)
        if sha256:
            changed.update(sha256=sha256)
        with self._session() as session:
            if session.execute(update(DistFile).where(*where).values(**changed)).rowcount:
                session.commit()
                return
            row = dict(values, source=source, size=size, sha256=sha256, uploaded_by=uploaded_by,
                       created_at=created_at or utcnow())
            try:
                session.execute(insert(DistFile).values(**row))
                session.commit()
            except IntegrityError:  # another worker inserted it first
                session.rollback()
                session.execute(update(DistFile).where(*where).values(**changed))
                session.commit()
        self._cache_delete("stats")

    def remove_file(self, project_dir: str, filename: str, source: str) -> None:
        with self._session() as session:
            session.execute(
                delete(DistFile).where(
                    DistFile.source == source,
                    DistFile.project_name == project_dir,
                    DistFile.filename == filename,
                )
            )
            session.commit()
        self._cache_delete("stats")

    # -- reading --------------------------------------------------------------

    def ping(self) -> bool:
        try:
            with self._session() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # any driver error means "not healthy"
            log.error("Database ping failed: %s", exc)
            return False

    def is_empty(self) -> bool:
        with self._session() as session:
            return session.execute(select(DistFile.id).limit(1)).first() is None

    def stats(self) -> Stats:
        cached = self._cache_get("stats")
        if cached:
            return Stats(**cached)
        f = DistFile
        query = select(
            func.count(distinct(f.project_key)),
            func.count(distinct(case((f.source == "local", f.project_key)))),
            func.count(distinct(case((f.source == "cached", f.project_key)))),
            func.count(),
            func.coalesce(func.sum(f.size), 0),
            func.coalesce(func.sum(case((f.source == "cached", f.size), else_=0)), 0),
        )
        with self._session() as session:
            row = session.execute(query).one()
        result = Stats(*(int(v) for v in row))
        self._cache_set("stats", result.__dict__, self._stats_ttl)
        return result

    def find(self, key: str) -> Optional[str]:
        """Display name of the project with this normalised name, or None."""
        with self._session() as session:
            return session.execute(
                select(func.min(DistFile.project_name)).where(DistFile.project_key == key)
            ).scalar()

    def list_projects(
        self,
        q: str = "",
        source: str = "all",
        sort: str = "name",
        descending: bool = False,
        page: int = 1,
        per_page: int = 30,
    ) -> ProjectPage:
        f = DistFile
        sort = sort if sort in SORTS else "name"
        page = max(1, int(page))
        per_page = min(100, max(1, int(per_page)))

        has_local = func.max(case((f.source == "local", 1), else_=0))
        has_cached = func.max(case((f.source == "cached", 1), else_=0))
        files = func.count()
        size = func.sum(f.size)
        updated = func.max(f.created_at)

        grouped = select(
            f.project_key.label("key"),
            func.min(f.project_name).label("name"),
            files.label("files"),
            size.label("size"),
            updated.label("updated"),
            has_local.label("has_local"),
            has_cached.label("has_cached"),
        ).group_by(f.project_key)

        needle = project_key(q.strip()) if q.strip() else ""
        if needle:
            grouped = grouped.where(f.project_key.like(f"%{_like_escape(needle)}%", escape="\\"))
        if source == "local":
            grouped = grouped.having(has_local == 1)
        elif source == "cached":
            grouped = grouped.having(has_cached == 1)

        order = {"name": f.project_key, "updated": updated, "files": files, "size": size}[sort]
        primary = desc(order) if descending else order
        ordered = grouped.order_by(primary, f.project_key) if sort != "name" else grouped.order_by(primary)

        with self._session() as session:
            total = session.execute(select(func.count()).select_from(grouped.subquery())).scalar() or 0
            pages = max(1, -(-total // per_page))
            page = min(page, pages)
            rows = session.execute(ordered.limit(per_page).offset((page - 1) * per_page)).all()
            keys = [r.key for r in rows]
            versions: Dict[str, List[str]] = {}
            if keys:
                for key, version in session.execute(
                    select(f.project_key, f.version).where(f.project_key.in_(keys), f.version.is_not(None))
                ):
                    versions.setdefault(key, []).append(version)

        items = [
            ProjectRow(
                key=r.key,
                name=r.name,
                files=int(r.files),
                size=int(r.size or 0),
                updated=r.updated,
                sources=[s for s, on in (("local", r.has_local), ("cached", r.has_cached)) if on],
                latest=_latest(versions.get(r.key, [])),
            )
            for r in rows
        ]
        return ProjectPage(items=items, total=int(total), page=page, per_page=per_page)

    def get_project(self, key: str) -> Optional[ProjectDetail]:
        with self._session() as session:
            rows = session.execute(select(DistFile).where(DistFile.project_key == key)).scalars().all()
        if not rows:
            return None
        names = {r.project_name for r in rows}
        detail = ProjectDetail(key=key, name=names.pop() if len(names) == 1 else key)
        floor = Version("0")

        def order(row: DistFile):
            try:
                return (row.version is not None, Version(row.version) if row.version else floor, row.filename)
            except InvalidVersion:
                return (False, floor, row.filename)

        for row in sorted(rows, key=order, reverse=True):
            detail.files.append(
                FileRow(row.filename, row.project_name, row.source, row.kind, row.version, int(row.size or 0),
                        row.sha256, row.created_at)
            )
        return detail

    def hashes(self, key: str) -> Dict[Tuple[str, str], str]:
        """``{(source, filename): sha256}`` for the files of one project that have a known hash."""
        with self._session() as session:
            rows = session.execute(
                select(DistFile.source, DistFile.filename, DistFile.sha256).where(
                    DistFile.project_key == key, DistFile.sha256.is_not(None)
                )
            )
            return {(s, fn): h for s, fn, h in rows}

    # -- maintenance ----------------------------------------------------------

    def reindex(self, local_dir: str, cache_dir: str) -> Dict[str, int]:
        """Make the catalog match the two directories. Idempotent and safe to run while serving."""
        counts = {"added": 0, "updated": 0, "removed": 0}
        roots: List[Tuple[str, str]] = [("local", str(local_dir))]
        if not _same_dir(str(local_dir), str(cache_dir)):
            roots.append(("cached", str(cache_dir)))
        seen: Dict[str, set] = {"local": set(), "cached": set()}

        for source, root in roots:
            for project_dir, files in self._scan(root):
                seen[source].add(project_dir)
                self._sync_project(source, project_dir, files, counts)

        with self._session() as session:
            known = session.execute(select(DistFile.source, DistFile.project_name).distinct()).all()
            for source, name in known:
                if name not in seen.get(source, set()):
                    result = session.execute(
                        delete(DistFile).where(DistFile.source == source, DistFile.project_name == name)
                    )
                    counts["removed"] += result.rowcount or 0
            session.commit()
        self._cache_delete("stats")
        return counts

    @staticmethod
    def _scan(root: str) -> Iterator[Tuple[str, Dict[str, Tuple[int, float]]]]:
        try:
            with os.scandir(root) as top:
                directories = sorted((e for e in top if not e.name.startswith(".")), key=lambda e: e.name)
        except OSError:
            return
        for directory in directories:
            try:
                if not directory.is_dir():
                    continue
                found: Dict[str, Tuple[int, float]] = {}
                with os.scandir(directory.path) as inner:
                    for entry in inner:
                        if entry.name.startswith(".") or not valid_filename(entry.name) or not entry.is_file():
                            continue
                        stat = entry.stat()
                        found[entry.name] = (stat.st_size, stat.st_mtime)
            except OSError:
                continue
            if found:
                yield directory.name, found

    def _sync_project(self, source: str, project_dir: str, files: Dict[str, Tuple[int, float]], counts: dict) -> None:
        with self._session() as session:
            existing = {
                row.filename: row
                for row in session.execute(
                    select(DistFile).where(DistFile.source == source, DistFile.project_name == project_dir)
                ).scalars()
            }
        for filename, (size, mtime) in files.items():
            row = existing.get(filename)
            if row is None:
                self.record_file(project_dir, filename, source, size, created_at=_utc_from_timestamp(mtime))
                counts["added"] += 1
            elif int(row.size or 0) != size:
                self.record_file(project_dir, filename, source, size)
                counts["updated"] += 1
        stale = [fn for fn in existing if fn not in files]
        if stale:
            with self._session() as session:
                session.execute(
                    delete(DistFile).where(
                        DistFile.source == source,
                        DistFile.project_name == project_dir,
                        DistFile.filename.in_(stale),
                    )
                )
                session.commit()
            counts["removed"] += len(stale)

    def prune_cache(self, cache_dir: str, max_bytes: int, target_ratio: float = 0.9) -> Dict[str, int]:
        """Delete the oldest cached files until the cache is at most ``target_ratio * max_bytes``."""
        total = self.stats_uncached().cached_size
        removed = freed = 0
        if total <= max_bytes:
            return {"removed": 0, "freed": 0, "remaining": total}
        target = int(max_bytes * target_ratio)
        root = os.path.realpath(str(cache_dir))
        while total > target:
            with self._session() as session:
                batch = session.execute(
                    select(DistFile)
                    .where(DistFile.source == "cached")
                    .order_by(DistFile.created_at, DistFile.id)
                    .limit(200)
                ).scalars().all()
                if not batch:
                    break
                for row in batch:
                    path = os.path.realpath(os.path.join(root, row.project_name, row.filename))
                    if path.startswith(root + os.sep):  # never delete outside the cache directory
                        try:
                            os.remove(path)
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            log.warning("Could not delete %s: %s", path, exc)
                            continue
                        try:
                            os.rmdir(os.path.dirname(path))  # only succeeds when the project directory is empty
                        except OSError:
                            pass
                    session.execute(delete(DistFile).where(DistFile.id == row.id))
                    size = int(row.size or 0)
                    total -= size
                    freed += size
                    removed += 1
                    if total <= target:
                        break
                session.commit()
        self._cache_delete("stats")
        return {"removed": removed, "freed": freed, "remaining": total}

    def stats_uncached(self) -> Stats:
        self._cache_delete("stats")
        return self.stats()

    # -- cache helpers --------------------------------------------------------

    def _cache_get(self, key: str):
        if not self._cache:
            return None
        raw = self._cache.get(f"catalog:{key}")
        return json.loads(raw) if raw else None

    def _cache_set(self, key: str, value, ttl: float) -> None:
        if self._cache and ttl > 0:
            self._cache.set(f"catalog:{key}", json.dumps(value), ttl)

    def _cache_delete(self, key: str) -> None:
        if self._cache:
            self._cache.delete(f"catalog:{key}")
