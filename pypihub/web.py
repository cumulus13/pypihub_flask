#!/usr/bin/env python3

# File: pypihub/web.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Web interface for PyPihub: package browser, package pages and a health check
# License: LGPL-3.0-or-later

"""
Web interface for PyPihub.

Everything here is read-only and works from the same two directories the
server already uses (``LOCAL_PKG_DIR`` and ``CACHE_DIR``), so it needs no
database and cannot change how pip, twine or curl talk to the server.

Routes
    GET /                    package browser
    GET /project/<name>/     files of one package
    GET /healthz             JSON health check (used by the Docker HEALTHCHECK)

Environment
    PYPIHUB_UI_CACHE_TTL     seconds a directory scan is reused (default 5, 0 disables)
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from flask import Blueprint, Flask, current_app, jsonify, make_response, render_template, request, url_for
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version
from werkzeug.exceptions import NotFound

REPO_URL = "https://github.com/cumulus13/pypihub_flask"

# Paths that machines (pip, twine, curl) talk to. Their errors stay plain.
_MACHINE_PREFIXES = ("/simple/", "/packages/", "/cache/", "/upload")

# Hosts pip accepts over plain http without --trusted-host.
_PIP_SECURE_HOSTS = {"localhost", "::1"}

_SOURCE_ORDER = {"local": 0, "cached": 1}

_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

bp = Blueprint("pypihub_ui", __name__, template_folder="templates")


# --------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_version() -> str:
    """Version of PyPihub: ``__version__.py`` in a source checkout, else the installed metadata."""
    here = Path(__file__).resolve().parent
    for folder in (here, here.parent):
        version_file = folder / "__version__.py"
        if version_file.is_file():
            match = re.search(r"""^\s*version\s*=\s*["']?([^"'\s]+)""", version_file.read_text(encoding="utf-8"), re.M)
            if match:
                return match.group(1)
    try:
        from importlib.metadata import version as dist_version

        return dist_version("pypihub")
    except Exception:
        return "dev"


# --------------------------------------------------------------------------
# Scanning the package directories
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DistFile:
    filename: str
    directory: str  # directory name on disk, needed to build download URLs
    source: str  # "local" or "cached"
    size: int
    mtime: float
    kind: str  # "wheel", "sdist" or "other"
    version: Optional[Version]


@dataclass
class Package:
    key: str  # PEP 503 normalised name, used in URLs and for matching
    name: str  # name as stored on disk
    files: List[DistFile] = field(default_factory=list)

    @property
    def sources(self) -> List[str]:
        return sorted({f.source for f in self.files}, key=_SOURCE_ORDER.__getitem__)

    @property
    def latest(self) -> Optional[str]:
        versions = [f.version for f in self.files if f.version is not None]
        if not versions:
            return None
        stable = [v for v in versions if not v.is_prerelease]
        return str(max(stable or versions))

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def updated(self) -> float:
        return max(f.mtime for f in self.files)

    def sorted_files(self) -> List[DistFile]:
        """Newest version first; files without a parsable version last."""
        floor = Version("0")
        return sorted(
            self.files,
            key=lambda f: (f.version is not None, f.version or floor, f.filename),
            reverse=True,
        )


@dataclass
class Snapshot:
    packages: List[Package]

    @property
    def by_key(self) -> Dict[str, Package]:
        return {p.key: p for p in self.packages}

    @property
    def total_files(self) -> int:
        return sum(len(p.files) for p in self.packages)

    @property
    def total_size(self) -> int:
        return sum(p.total_size for p in self.packages)

    def count(self, source: str) -> int:
        return sum(1 for p in self.packages if source in p.sources)


def parse_dist_filename(filename: str) -> Tuple[str, Optional[Version]]:
    """Return ``(kind, version)`` for a distribution file name."""
    try:
        if filename.endswith(".whl"):
            return "wheel", parse_wheel_filename(filename)[1]
        if filename.endswith((".tar.gz", ".zip")):
            return "sdist", parse_sdist_filename(filename)[1]
    except ValueError:  # InvalidWheelFilename / InvalidSdistFilename / InvalidVersion
        pass
    return "other", None


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _scan_root(root: Path, source: str, into: Dict[str, Package]) -> None:
    try:
        with os.scandir(root) as top:
            package_dirs = sorted((e for e in top if not e.name.startswith(".")), key=lambda e: e.name)
    except OSError:
        return
    for package_dir in package_dirs:
        try:
            if not package_dir.is_dir():
                continue
            with os.scandir(package_dir.path) as inner:
                entries = list(inner)
        except OSError:
            continue
        key = canonicalize_name(package_dir.name)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            kind, version = parse_dist_filename(entry.name)
            package = into.get(key)
            if package is None:
                package = into[key] = Package(key=key, name=package_dir.name)
            elif package.name != package_dir.name:
                package.name = key  # directories differ only in spelling: show the canonical name
            package.files.append(
                DistFile(entry.name, package_dir.name, source, stat.st_size, stat.st_mtime, kind, version)
            )


def scan(local_dir: Path, cache_dir: Path) -> Snapshot:
    """Read both directories. Empty directories (pip creates them for unknown names) are ignored."""
    packages: Dict[str, Package] = {}
    _scan_root(Path(local_dir), "local", packages)
    if not _same_dir(Path(local_dir), Path(cache_dir)):
        _scan_root(Path(cache_dir), "cached", packages)
    return Snapshot(sorted(packages.values(), key=lambda p: p.key))


class _SnapshotCache:
    """Reuse a directory scan for a few seconds; a large cache can hold thousands of files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Optional[Tuple[str, str]] = None
        self._stamp = 0.0
        self._snapshot: Optional[Snapshot] = None

    def get(self, local_dir: Path, cache_dir: Path, ttl: float) -> Snapshot:
        key = (str(local_dir), str(cache_dir))
        with self._lock:
            now = time.monotonic()
            if ttl > 0 and self._snapshot is not None and self._key == key and now - self._stamp < ttl:
                return self._snapshot
            snapshot = scan(local_dir, cache_dir)
            self._key, self._stamp, self._snapshot = key, time.monotonic(), snapshot
            return snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None


_cache = _SnapshotCache()


def invalidate_cache() -> None:
    """Forget the cached scan, e.g. right after an upload."""
    _cache.invalidate()


def _ttl() -> float:
    try:
        return max(0.0, float(os.getenv("PYPIHUB_UI_CACHE_TTL", "5")))
    except ValueError:
        return 5.0


def _directories() -> Tuple[Path, Path]:
    try:
        return Path(current_app.config["LOCAL_PKG_DIR"]), Path(current_app.config["CACHE_DIR"])
    except KeyError as exc:  # pragma: no cover - programming error in the host app
        raise RuntimeError(f"pypihub.web needs app.config[{exc.args[0]!r}] to be set") from exc


def _snapshot() -> Snapshot:
    local_dir, cache_dir = _directories()
    return _cache.get(local_dir, cache_dir, _ttl())


# --------------------------------------------------------------------------
# Formatting helpers (also exposed to templates)
# --------------------------------------------------------------------------

def human_size(num: int) -> str:
    """Decimal units, like pip prints them."""
    if num < 1000:
        return f"{num} B"
    value = float(num)
    for unit in ("kB", "MB", "GB", "TB"):
        value /= 1000.0
        if value < 999.95 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"  # pragma: no cover


def time_ago(timestamp: float, now: Optional[float] = None) -> str:
    seconds = max(0, int((time.time() if now is None else now) - timestamp))
    if seconds < 60:
        return "just now"
    for limit, unit, size in (
        (3600, "minute", 60),
        (86400, "hour", 3600),
        (30 * 86400, "day", 86400),
        (365 * 86400, "month", 30 * 86400),
    ):
        if seconds < limit:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    n = seconds // (365 * 86400)
    return f"{n} year{'' if n == 1 else 's'} ago"


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _server_urls() -> Dict[str, object]:
    base = request.host_url.rstrip("/")
    parts = urlsplit(base)
    host = parts.hostname or ""
    needs_trust = parts.scheme == "http" and host not in _PIP_SECURE_HOSTS and not host.startswith("127.")
    return {
        "base_url": base,
        "pip_index_url": f"{base}/simple/",
        "trusted_host": host if needs_trust else "",
    }


@bp.app_template_filter("filesize")
def _filesize_filter(value: int) -> str:
    return human_size(int(value))


@bp.app_template_filter("ago")
def _ago_filter(value: float) -> str:
    return time_ago(float(value))


@bp.app_template_filter("utc_iso")
def _utc_iso_filter(value: float) -> str:
    return _utc(float(value)).isoformat(timespec="seconds")


@bp.app_template_filter("utc_text")
def _utc_text_filter(value: float) -> str:
    return _utc(float(value)).strftime("%Y-%m-%d %H:%M UTC")


@bp.app_context_processor
def _inject_globals() -> Dict[str, object]:
    version = get_version()

    def static_url(filename: str) -> str:
        return url_for("static", filename=filename, v=version)

    context: Dict[str, object] = {"ph_version": version, "repo_url": REPO_URL, "static_url": static_url}
    context.update(_server_urls())
    return context


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

def _page(template: str, status: int = 200, **context: object):
    response = make_response(render_template(template, **context), status)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@bp.route("/")
def index():
    return _page("index.html", snapshot=_snapshot())


@bp.route("/project/<name>/")
def project(name: str):
    package = _snapshot().by_key.get(canonicalize_name(name))
    if package is None:
        raise NotFound(f"No package named {name!r} is stored on this server.")
    return _page("package.html", pkg=package)


@bp.route("/healthz")
def healthz():
    local_dir, cache_dir = _directories()
    checks = {
        "packages_dir_writable": os.access(local_dir, os.W_OK),
        "cache_dir_writable": os.access(cache_dir, os.W_OK),
    }
    healthy = all(checks.values())
    response = jsonify(status="ok" if healthy else "degraded", version=get_version(), checks=checks)
    response.status_code = 200 if healthy else 503
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.app_errorhandler(404)
def _not_found(error):
    # pip, twine and curl keep the plain default response; browsers get a page.
    if request.path.startswith(_MACHINE_PREFIXES) or not request.accept_mimetypes.accept_html:
        return error
    return _page(
        "error.html",
        status=404,
        heading="Page not found",
        message=getattr(error, "description", None) or "That address does not exist on this server.",
    )


def init_app(app: Flask) -> None:
    """Register the web interface. ``LOCAL_PKG_DIR`` and ``CACHE_DIR`` must be in ``app.config``."""
    app.register_blueprint(bp)
