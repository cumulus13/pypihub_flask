#!/usr/bin/env python3

# File: pypihub/web.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Web interface for PyPihub: package browser, package pages and a health check
# License: LGPL-3.0-or-later

"""
Web interface for PyPihub.

Reads the catalog (see ``catalog.py``), so listing a page of packages is one indexed SQL query no matter
how many files are stored. It never writes, and it cannot change how pip and twine talk to the server.

Routes
    GET /                    package browser: ?q=search &source=all|local|cached &sort=name|updated|files|size
                             &dir=asc|desc &page=N
    GET /project/<name>/     package page: details copied from pypi.org (README, links, release history) and the
                             files stored here (?page=N)
    GET /project/<name>/<version>/   the same for one release
    GET /healthz             JSON health check (used by the Docker HEALTHCHECK)

Environment
    PYPIHUB_UI_PER_PAGE      packages per page (default 30, between 10 and 100)
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from flask import Blueprint, Flask, current_app, jsonify, make_response, render_template, request, url_for
from werkzeug.exceptions import NotFound

try:
    from .catalog import SORTS, SOURCES
    from .dist import project_key, valid_filename, valid_project_name
    from .pypimeta import VERSION_RE, group_classifiers, split_requirements
except ImportError:  # run as a script from inside the package directory
    from catalog import SORTS, SOURCES  # type: ignore
    from dist import project_key, valid_filename, valid_project_name  # type: ignore
    from pypimeta import VERSION_RE, group_classifiers, split_requirements  # type: ignore

REPO_URL = "https://github.com/cumulus13/pypihub_flask"

# Paths that machines (pip, twine, curl) talk to. Their errors stay plain.
_MACHINE_PREFIXES = ("/simple/", "/packages/", "/cache/", "/upload")

# Hosts pip accepts over plain http without --trusted-host.
_PIP_SECURE_HOSTS = {"localhost", "::1"}

_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "connect-src 'self'",  # live search fetches the page itself
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


def _directories() -> Tuple[Path, Path]:
    try:
        return Path(current_app.config["LOCAL_PKG_DIR"]), Path(current_app.config["CACHE_DIR"])
    except KeyError as exc:  # pragma: no cover - programming error in the host app
        raise RuntimeError(f"pypihub.web needs app.config[{exc.args[0]!r}] to be set") from exc


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


def _utc(value) -> datetime:
    if isinstance(value, datetime):  # the catalog stores naive UTC
        return value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


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
def _ago_filter(value) -> str:
    return time_ago(_utc(value).timestamp())


@bp.app_template_filter("utc_iso")
def _utc_iso_filter(value) -> str:
    return _utc(value).isoformat(timespec="seconds")


@bp.app_template_filter("utc_text")
def _utc_text_filter(value) -> str:
    return _utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@bp.app_template_filter("iso_date")
def _iso_date_filter(value) -> str:
    parsed = _parse_iso(value)
    return f"{parsed:%b} {parsed.day}, {parsed.year}" if parsed else ""


@bp.app_template_filter("iso_text")
def _iso_text_filter(value) -> str:
    parsed = _parse_iso(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if parsed else ""


@bp.app_context_processor
def _inject_globals() -> Dict[str, object]:
    version = get_version()

    def static_url(filename: str) -> str:
        return url_for("static", filename=filename, v=version)

    context: Dict[str, object] = {
        "ph_version": version, "repo_url": REPO_URL, "static_url": static_url,
        "group_classifiers": group_classifiers, "split_requirements": split_requirements,
        "author": "Hadi Cahyadi", "email": "cumulus13@gmail.com",
    }
    context.update(_server_urls())
    return context


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

def _page(template: str, status: int = 200, remote_images: bool = False, **context: object):
    response = make_response(render_template(template, **context), status)
    # A README shows its own images, which live on other hosts. Scripts, styles and frames stay locked down.
    response.headers["Content-Security-Policy"] = _CSP.replace("img-src 'self' data:", "img-src 'self' data: https:") if remote_images else _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


PLACEHOLDER = "package-name"
FILES_PER_PAGE = 50


def _per_page() -> int:
    try:
        return min(100, max(10, int(os.getenv("PYPIHUB_UI_PER_PAGE", "30"))))
    except ValueError:
        return 30


def _catalog():
    return current_app.extensions["pypihub_catalog"]


def _default_direction(sort: str) -> str:
    return "asc" if sort == "name" else "desc"


def _list_state() -> Dict[str, object]:
    """Query string, cleaned up: unknown values fall back to the defaults instead of raising."""
    source = request.args.get("source", "all")
    sort = request.args.get("sort", "name")
    sort = sort if sort in SORTS else "name"
    direction = request.args.get("dir", "")
    return {
        "q": request.args.get("q", "").strip()[:100],
        "source": source if source in SOURCES else "all",
        "sort": sort,
        "dir": direction if direction in ("asc", "desc") else _default_direction(sort),
        "page": max(1, request.args.get("page", 1, type=int) or 1),
    }


def _list_url(state: Dict[str, object], **changes: object) -> str:
    """Link to the list with some settings changed. Defaults are left out to keep URLs short."""
    merged = dict(state, **changes)
    sort = str(merged["sort"])
    params = {
        "q": merged["q"] or None,
        "source": merged["source"] if merged["source"] != "all" else None,
        "sort": sort if sort != "name" or merged["dir"] != "asc" else None,
        "dir": merged["dir"] if merged["dir"] != _default_direction(sort) else None,
        "page": merged["page"] if int(merged["page"]) > 1 else None,  # type: ignore[call-overload]
    }
    return url_for("pypihub_ui.index", **{k: v for k, v in params.items() if v is not None})


def _page_numbers(page: int, pages: int, around: int = 2) -> List[Optional[int]]:
    """``[1, None, 4, 5, 6, None, 20]``: first, last and the neighbours of the current page; None is a gap."""
    wanted = sorted({1, pages} | {n for n in range(page - around, page + around + 1) if 1 <= n <= pages})
    numbers: List[Optional[int]] = []
    for n in wanted:
        if numbers:
            gap = n - numbers[-1]  # type: ignore[operator]
            if gap == 2:
                numbers.append(n - 1)  # an ellipsis for a single page would hide less than the number itself
            elif gap > 2:
                numbers.append(None)
        numbers.append(n)
    return numbers


def _command_package(catalog, state: Dict[str, object], listing) -> Tuple[str, bool]:
    """Package name for the pip command in the page header, and whether it is not stored here yet."""
    query = str(state["q"])
    if not query:
        return PLACEHOLDER, False
    exact = catalog.find(project_key(query))
    if exact:
        return exact, False
    if listing.total == 1:
        return listing.items[0].name, False
    if listing.total == 0 and state["source"] == "all" and valid_project_name(query):
        return query, True
    return PLACEHOLDER, False


@bp.route("/")
def index():
    catalog = _catalog()
    state = _list_state()
    listing = catalog.list_projects(
        q=str(state["q"]),
        source=str(state["source"]),
        sort=str(state["sort"]),
        descending=state["dir"] == "desc",
        page=int(state["page"]),  # type: ignore[call-overload]
        per_page=_per_page(),
    )
    state["page"] = listing.page  # clamped to the last page
    command_package, upstream_note = _command_package(catalog, state, listing)

    def sort_url(column: str) -> str:
        flip = state["sort"] == column
        direction = ("asc" if state["dir"] == "desc" else "desc") if flip else _default_direction(column)
        return _list_url(state, sort=column, dir=direction, page=1)

    return _page(
        "index.html",
        listing=listing,
        stats=catalog.stats(),
        state=state,
        command_package=command_package,
        upstream_note=upstream_note,
        numbers=_page_numbers(listing.page, listing.pages),
        page_url=lambda n: _list_url(state, page=n),
        source_url=lambda source: _list_url(state, source=source, page=1),
        sort_url=sort_url,
        aria_sort=lambda column: ("ascending" if state["dir"] == "asc" else "descending") if state["sort"] == column else "none",
        clear_url=url_for("pypihub_ui.index"),
    )


def _version_set(detail) -> set:
    """Normalised versions of the files stored here, to mark them in the release history."""
    from packaging.version import InvalidVersion, Version

    found = set()
    for item in detail.files:
        try:
            found.add(Version(item.version)) if item.version else None
        except InvalidVersion:
            continue
    return found


def _is_stored(version: str, stored: set) -> bool:
    from packaging.version import InvalidVersion, Version

    try:
        return Version(version) in stored
    except InvalidVersion:
        return False


def _project_page(name: str, version: Optional[str]):
    key = project_key(name)
    detail = _catalog().get_project(key)
    if detail is None:  # details are only ever fetched for projects stored here
        raise NotFound(f"No package named {name!r} is stored on this server.")
    if version is not None and not VERSION_RE.match(version):
        raise NotFound(f"{version!r} is not a version number.")

    meta_service = current_app.extensions.get("pypihub_meta")
    project_result = meta_service.get(key) if meta_service else None
    result, project_data = project_result, (project_result.data if project_result else None)
    if version is not None:
        known = {r["version"] for r in (project_data or {}).get("releases", [])}
        if project_data is None:
            raise NotFound("Details for that release are not available right now.")
        if version not in known:
            raise NotFound(f"{detail.name} has no release {version!r}.")
        result = meta_service.get(key, version)
        if result.data is None:
            raise NotFound(f"Details for {detail.name} {version} are not available right now.")
    meta = result.data if result else None

    stored = _version_set(detail)
    latest = (project_data or {}).get("version")
    releases = [dict(r, stored=_is_stored(r["version"], stored), latest=r["version"] == latest)
                for r in (project_data or {}).get("releases", [])]
    shown_version = (meta or {}).get("version") or version or ""
    stored_names = {f.filename for f in detail.files}
    pypi_files = [
        dict(f, stored=f["filename"] in stored_names,
             href=url_for("serve_cached", package=key, filename=f["filename"]) if valid_filename(f["filename"]) else None)
        for f in (meta or {}).get("files", [])
    ]

    pages = max(1, -(-len(detail.files) // FILES_PER_PAGE))
    page = min(max(1, request.args.get("page", 1, type=int) or 1), pages)
    files = detail.files[(page - 1) * FILES_PER_PAGE : page * FILES_PER_PAGE]
    display_name = (meta or {}).get("name") or detail.name
    return _page(
        "package.html",
        remote_images=bool(meta and meta.get("description_html")),
        pkg=detail,
        meta=meta,
        state=result.state if result else "disabled",
        fetched_at=result.fetched_at if result else None,
        display_name=display_name,
        shown_version=shown_version,
        install_target=f"{display_name}=={version}" if version else display_name,
        is_latest=bool(version is None or version == latest),
        releases=releases,
        pypi_files=pypi_files,
        files=files,
        page=page,
        pages=pages,
        numbers=_page_numbers(page, pages),
        page_url=lambda n: url_for("pypihub_ui.project_version" if version else "pypihub_ui.project",
                                   **dict({"name": detail.key}, **({"version": version} if version else {}), **({"page": n} if n > 1 else {}))),
        first=(page - 1) * FILES_PER_PAGE + 1,
        last=min(len(detail.files), page * FILES_PER_PAGE),
    )


@bp.route("/project/<name>/")
def project(name: str):
    return _project_page(name, None)


@bp.route("/project/<name>/<version>/")
def project_version(name: str, version: str):
    return _project_page(name, version)


@bp.route("/healthz")
def healthz():
    local_dir, cache_dir = _directories()
    catalog = _catalog()
    checks = {
        "packages_dir_writable": os.access(local_dir, os.W_OK),
        "cache_dir_writable": os.access(cache_dir, os.W_OK),
        "database": catalog.ping(),
    }
    healthy = all(checks.values())
    kv = current_app.extensions.get("pypihub_cache")
    if kv is not None and kv.kind == "redis":
        checks["redis"] = kv.ping()  # reported, but the site works without it, so it does not fail the check
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


def init_app(app: Flask, catalog, kv_cache=None, meta=None) -> None:
    """Register the web interface. ``LOCAL_PKG_DIR`` and ``CACHE_DIR`` must be in ``app.config``."""
    app.extensions["pypihub_catalog"] = catalog
    app.extensions["pypihub_cache"] = kv_cache
    app.extensions["pypihub_meta"] = meta
    app.register_blueprint(bp)
