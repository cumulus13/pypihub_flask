#!/usr/bin/env python3

# File: pypihub/pypihub.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2025-12-31
# Description: 
# License: MIT

"""
PyPihub - Local PyPI Server
Copyright (C) 2025 Hadi Cahyadi <cumulus13@gmail.com>

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 3 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

For commercial licensing options, contact: cumulus13@gmail.com
"""

import os
import sys
from pathlib import Path
import logging
import secrets
import hashlib
import html
import time
import uuid
from urllib.parse import quote
from packaging.utils import canonicalize_name

env_candidates = (
    Path(__file__).parent / '.env',
    Path(__file__).parent.parent / '.env',
)

env_file = None
for e in env_candidates:
    if e.is_file():
        env_file = e
        break

from envdot import load_env

load_env(env_file)

try:
    from . import logger as Logger
except:
    import logger as Logger

# logging.basicConfig(level=logging.CRITICAL)

logger = Logger.setup_logging()
# logger.setLevel(Logger.EMERGENCY_LEVEL)

# logger = logging.getLogger(__name__)

from flask import Flask, send_from_directory, request, abort, Response, jsonify, session#, redirect, url_for
from pydebugger.debug import debug
from configset import configset
import argparse
from functools import wraps
from getpass import getpass

if (Path(__file__).parent / 'settings.py').is_file():
    try:
        from . import settings as local_settings
    except:
        import settings as local_settings
else:
    local_settings = None

if (Path(__file__).parent / 'database.py').is_file():
    try:
        from . import database
    except:
        import database
else:
    database = None

try:
    from . import web as web_ui
    from . import cache as cache_mod, catalog as catalog_mod, dist, limits, pypimeta, upstream as upstream_mod
except ImportError:  # run as a script from inside the package directory
    import web as web_ui  # type: ignore
    import cache as cache_mod, catalog as catalog_mod, dist, limits, pypimeta, upstream as upstream_mod  # type: ignore

from rich.console import Console
from rich_argparse import RichHelpFormatter, _lazy_rich as rr
from typing import ClassVar
console = Console()

class CustomRichHelpFormatter(RichHelpFormatter):
    """A custom RichHelpFormatter with modified styles."""

    styles: ClassVar[dict[str, rr.StyleType]] = {
        "argparse.args": "bold #FFFF00",
        "argparse.groups": "#AA55FF",
        "argparse.help": "bold #00FFFF",
        "argparse.metavar": "bold #FF00FF",
        "argparse.syntax": "underline",
        "argparse.text": "white",
        "argparse.prog": "bold #00AAFF italic",
        "argparse.default": "bold",
    }

class settings:
    #make this possbile/available like 'settings.var' then return value from 'var = value' in settings.py
    def __getattr__(self, item):
        if local_settings is None:
            return None
        if hasattr(local_settings, item):
            return getattr(local_settings, item)
        # raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")
        return None

settings = settings()  # type: ignore

app = Flask(__name__)

# Trust X-Forwarded-* headers only when explicitly asked to (reverse proxy in front).
if str(os.getenv('PYPIHUB_TRUST_PROXY', '')).lower() in ('1', 'true', 'yes', 'on'):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)  # type: ignore

CONFIGFILE = (
    os.getenv('CONFIGFILE')
    or (settings.CONFIGFILE if getattr(settings, 'CONFIGFILE', None) and Path(settings.CONFIGFILE).is_file() else None)  # type: ignore
    or str(Path(__file__).parent / (Path(__file__).stem + '.ini'))
)
debug(CONFIGFILE=CONFIGFILE)
logger.info(f"CONFIGFILE: {CONFIGFILE}")
CONFIG = configset(CONFIGFILE)

BASE_DIR = (
    Path(os.getenv('BASE_DIR'))  # type: ignore
    if os.getenv('BASE_DIR') and Path(os.getenv('BASE_DIR')).is_dir()  # type: ignore
    else Path(settings.BASE_DIR)  # type: ignore
    if getattr(settings, 'BASE_DIR', None)
    else Path(__file__).parent
)
debug(BASE_DIR=str(BASE_DIR))
logger.info(f"BASE_DIR: {BASE_DIR}")

logger.info(f"os.getenv('LOCAL_PKG_DIR'): {os.getenv('LOCAL_PKG_DIR')}, is_dir: {Path(os.getenv('LOCAL_PKG_DIR')).is_dir() if os.getenv('LOCAL_PKG_DIR') else None}")  # type: ignore
logger.info(f"CONFIG.get_config('dirs', 'local_pkg'): {CONFIG.get_config('dirs', 'local_pkg')}, is_dir: {Path(CONFIG.get_config('dirs', 'local_pkg')).is_dir() if CONFIG.get_config('dirs', 'local_pkg') else None}")  # type: ignore
logger.info(f"settings.LOCAL_PKG_DIR: {getattr(settings, 'LOCAL_PKG_DIR', None)}, is_dir: {Path(getattr(settings, 'LOCAL_PKG_DIR', '')) .is_dir() if getattr(settings, 'LOCAL_PKG_DIR', None) else None}")
LOCAL_PKG_DIR = (
    Path(os.getenv('LOCAL_PKG_DIR'))  # type: ignore
    if os.getenv('LOCAL_PKG_DIR') and Path(os.getenv('LOCAL_PKG_DIR')).is_dir()  # type: ignore
    else Path(CONFIG.get_config('dirs', 'local_pkg'))  # type: ignore
    if CONFIG.get_config('dirs', 'local_pkg') and Path(CONFIG.get_config('dirs', 'local_pkg')).is_dir()  # type: ignore
    else Path(getattr(settings, 'LOCAL_PKG_DIR', ''))
    if getattr(settings, 'LOCAL_PKG_DIR', None)
    else BASE_DIR / "packages"
)
debug(LOCAL_PKG_DIR=str(LOCAL_PKG_DIR))
logger.info(f"LOCAL_PKG_DIR: {LOCAL_PKG_DIR}")

logger.info(f"os.getenv('CACHE_DIR'): {os.getenv('CACHE_DIR')}, is_dir: {Path(os.getenv('CACHE_DIR')).is_dir() if os.getenv('CACHE_DIR') else None}")  # type: ignore
logger.info(f"CONFIG.get_config('dirs', 'cache'): {CONFIG.get_config('dirs', 'cache')}, is_dir: {Path(CONFIG.get_config('dirs', 'cache')).is_dir() if CONFIG.get_config('dirs', 'cache') else None}")  # type: ignore
logger.info(f"settings.CACHE_DIR: {getattr(settings, 'CACHE_DIR', None)}, is_dir: {Path(getattr(settings, 'CACHE_DIR', '')) .is_dir() if getattr(settings, 'CACHE_DIR', None) else None}")
CACHE_DIR = (
    Path(os.getenv('CACHE_DIR'))  # type: ignore
    if os.getenv('CACHE_DIR') and Path(os.getenv('CACHE_DIR')).is_dir()  # type: ignore
    else Path(CONFIG.get_config('dirs', 'cache'))  # type: ignore
    if CONFIG.get_config('dirs', 'cache') and Path(CONFIG.get_config('dirs', 'cache')).is_dir()  # type: ignore
    else Path(getattr(settings, 'CACHE_DIR', ''))
    if getattr(settings, 'CACHE_DIR', None)
    else BASE_DIR / "cache"
)
debug(CACHE_DIR=str(CACHE_DIR))
logger.info(f"CACHE_DIR: {CACHE_DIR}")

PYPI_SIMPLE_URL = (
    os.getenv('PYPI_SIMPLE_URL')
    or getattr(settings, 'PYPI_SIMPLE_URL', None)
    or CONFIG.get_config('urls', 'pypi_simple')  # type: ignore
    or "https://pypi.org/simple"
)
debug(PYPI_SIMPLE_URL=PYPI_SIMPLE_URL)
logger.info(f"PYPI_SIMPLE_URL: {PYPI_SIMPLE_URL}")

HOST = os.getenv('HOST', getattr(settings, 'HOST', None)) or CONFIG.get_config('server', 'host', default = '0.0.0.0')  # type: ignore
debug(HOST=HOST)
logger.info(f"HOST: {HOST}")

PORT = int(
    os.getenv('PORT')
    or getattr(settings, 'PORT', None)
    or CONFIG.get_config('server', 'port')  # type: ignore
    or 5000
)
debug(PORT=PORT)
logger.info(f"PORT: {PORT}")

print("\n")

os.makedirs(LOCAL_PKG_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

SECRET_KEY = os.getenv('SECRET_KEY') or getattr(settings, 'SECRET_KEY', None)
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "No SECRET_KEY configured (environment variable SECRET_KEY or settings.SECRET_KEY). "
        "Using a random key for this process: sign-in sessions will not survive a restart "
        "and are not shared between worker processes."
    )
app.config['SECRET_KEY'] = SECRET_KEY
app.config['LOCAL_PKG_DIR'] = LOCAL_PKG_DIR
app.config['CACHE_DIR'] = CACHE_DIR

REDIS_URL = os.getenv('REDIS_URL') or getattr(settings, 'REDIS_URL', None) or None
KV_CACHE = cache_mod.make_cache(REDIS_URL)
logger.info(f"Cache backend: {KV_CACHE.kind}")

def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')

ALLOW_OVERWRITE = _truthy(os.getenv('PYPIHUB_ALLOW_OVERWRITE'))
FILE_MAX_AGE = int(os.getenv('PYPIHUB_FILE_CACHE_SECONDS', '3600'))
CACHE_MAX_BYTES = dist.parse_size(os.environ['PYPIHUB_CACHE_MAX_SIZE']) if os.getenv('PYPIHUB_CACHE_MAX_SIZE') else 0
app.config['MAX_CONTENT_LENGTH'] = int(float(os.getenv('PYPIHUB_MAX_UPLOAD_MB', '200')) * 1024 * 1024)

limiter = limits.init_limiter(app, REDIS_URL)
guard_credentials = limits.apply_limits(limiter)
UPSTREAM = upstream_mod.Upstream(PYPI_SIMPLE_URL, KV_CACHE)

if database:
    from sqlalchemy import func
    from sqlalchemy.exc import SQLAlchemyError

    User = database.User
    Package = database.Package

    def get_db_url():
        # A full URL wins over the separate settings below.
        explicit = os.getenv('DATABASE_URL') or getattr(settings, 'DATABASE_URL', None)
        if explicit:
            return database.normalize_url(explicit)
        # Take config from Settings.co, Settings.ini, App.config, or ENV
        db_type = (
            getattr(settings, 'DB_TYPE', None)
            or CONFIG.get_config('database', 'type')  # type: ignore
            or app.config.get('DB_TYPE')
            or os.getenv('DB_TYPE')
            or 'sqlite'
        ).lower()

        db_path = (
            getattr(settings, 'DB_PATH', None)
            or CONFIG.get_config('database', 'path')  # type: ignore
            or app.config.get('DB_PATH')
            or os.getenv('DB_PATH')
            or f"{BASE_DIR}/pypihub.db"
        )

        db_name = (
            getattr(settings, 'DB_NAME', None)
            or CONFIG.get_config('database', 'name')  # type: ignore
            or app.config.get('DB_NAME')
            or os.getenv('DB_NAME')
            or 'pypihub'
        )

        db_user = (
            getattr(settings, 'DB_USERNAME', None)
            or CONFIG.get_config('database', 'username')  # type: ignore
            or app.config.get('DB_USERNAME')
            or os.getenv('DB_USERNAME')
            or ''
        )

        db_pass = (
            getattr(settings, 'DB_PASSWORD', None)
            or CONFIG.get_config('database', 'password')  # type: ignore
            or app.config.get('DB_PASSWORD')
            or os.getenv('DB_PASSWORD')
            or ''
        )

        db_host = (
            getattr(settings, 'DB_HOST', None)
            or CONFIG.get_config('database', 'host')  # type: ignore
            or app.config.get('DB_HOST')
            or os.getenv('DB_HOST')
            or 'localhost'
        )

        db_port = (
            getattr(settings, 'DB_PORT', None)
            or CONFIG.get_config('database', 'port')  # type: ignore
            or app.config.get('DB_PORT')
            or os.getenv('DB_PORT')
        )

        return database.build_url(
            db_type,
            path=db_path,
            name=db_name,
            user=db_user,
            password=db_pass,
            host=db_host,
            port=db_port,
        )

    engine = database.make_engine(get_db_url())
    SessionLocal = database.session_factory(engine)
    database.init_db(engine)
    CATALOG = catalog_mod.Catalog(SessionLocal, KV_CACHE)
    META = pypimeta.MetaService(SessionLocal, KV_CACHE, UPSTREAM)
    web_ui.init_app(app, CATALOG, KV_CACHE, META)

    import bcrypt

    def hash_password(password):
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def check_password(password, hashed):
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception as e:
            logger.error(f"Password check error: {e}")
            return False

if database:
    def find_user(db, username):
        # Compared in lower case on every database: MySQL is case-insensitive by default, PostgreSQL is not.
        return db.query(User).filter(func.lower(User.username) == (username or '').strip().lower()).first()

    _DUMMY_HASH = hash_password('timing-equaliser')

def check_auth(username, password):
    if not username or not password:
        logger.warning("Username or password is empty")
        return False
    if database:
        try:
            with SessionLocal() as db:
                user = find_user(db, username)
                # Always run bcrypt, so unknown names and wrong passwords take the same time.
                password_ok = check_password(password, user.password if user else _DUMMY_HASH)
                if user and password_ok:
                    return True
                logger.warning("Invalid username or password (DB)")
                return False
        except SQLAlchemyError as e:  # type: ignore
            logger.error(f"Database error during authentication: {e}")
            if CONFIG.get_config('env', 'development', False):  # type: ignore
                logger.exception(e)
            return False
    logger.emergency("run auth without use database")  # type: ignore
    # Settings.auths must be a list of tuple/list: [("user", "pass"), ...]
    auths = getattr(settings, 'AUTHS', CONFIG.get_config_as_list('auths', 'users')) # type: ignore
    if not auths or not isinstance(auths, (list, tuple)) or not auths:
        # Default: [('pypihub', 'pypihub')]
        password = CONFIG.get_config('auths', 'password')  # type: ignore
        auths = [('pypihub', password or 'pypihub')]
    return (username, password) in auths

def authenticate():
    # Return json so that twine can display a clear error message
    # resp = jsonify({"error": "Invalid username or password"})
    resp = Response("Invalid username or password", status=400, content_type="text/plain")
    resp.status_code = 401
    resp.headers['WWW-Authenticate'] = 'Basic realm="Login Required"'
    logger.debug(f"resp: {resp}")
    return resp

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not auth.username or not auth.password:
            return authenticate()
        logger.debug(f"auth.username: {auth.username}")
        try:
            if not check_auth(auth.username, auth.password):
                return authenticate()
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            if CONFIG.get_config('env', 'development', False):  # type: ignore
                logger.exception(e)
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def _plain(message, status=400):
    return Response(message, status=status, content_type='text/plain')


def current_user_id():
    user_id = session.get('user_id')
    if user_id or not database or not request.authorization:
        return user_id
    try:
        with SessionLocal() as db:
            user = find_user(db, request.authorization.username)
            return user.id if user else None
    except SQLAlchemyError as e:  # type: ignore
        logger.error(f"Could not look up the uploading user: {e}")
        return None


def resolve_package_dir(root, package):
    """Name of the directory in ``root`` that holds ``package``.

    pip asks for the PEP 503 normalised name (``my-pkg``) while twine uploads create the
    directory from the file name (``my_pkg``). Fall back to the requested name when no
    directory matches.
    """
    if package in ('.', '..'):
        abort(404, description="Package not found")
    if os.path.isdir(os.path.join(root, package)):
        return package
    wanted = canonicalize_name(package)
    try:
        for name in sorted(os.listdir(root)):
            if canonicalize_name(name) == wanted and os.path.isdir(os.path.join(root, name)):
                return name
    except OSError:
        pass
    return package


def dir_for(root, package):
    """Existing directory for a project, or the normalised name for a new one."""
    name = resolve_package_dir(root, package)
    return name if os.path.isdir(os.path.join(root, name)) else dist.project_key(package)


def _uploaded_file():
    return (
        request.files.get('file')
        or request.files.get('content')
        or request.files.get('distribution')
        or next(iter(request.files.values()), None)
    )


def store_upload(file, project_name):
    """Validate and store an uploaded distribution. Returns an error Response, or None on success."""
    filename = file.filename or ''
    if not dist.is_upload_filename(filename):
        return _plain("Invalid file name. Use a .whl, .tar.gz or .zip file named with letters, digits, '.', '_', '+' and '-'.")
    if not dist.valid_project_name(project_name):
        return _plain("Invalid project name.")
    declared = dist.project_from_filename(filename)
    if declared and declared != dist.project_key(project_name):
        return _plain(f"{filename} belongs to project '{declared}', not '{dist.project_key(project_name)}'.")

    directory = dir_for(LOCAL_PKG_DIR, project_name)
    target_dir = os.path.join(LOCAL_PKG_DIR, directory)
    final = os.path.join(target_dir, filename)
    if os.path.exists(final) and not ALLOW_OVERWRITE:
        logger.warning(f"File {filename} already exists in {target_dir}")
        return _plain("File already exists")

    os.makedirs(target_dir, exist_ok=True)
    temp = os.path.join(target_dir, f".{filename}.{uuid.uuid4().hex}.part")
    digest, size = hashlib.sha256(), 0
    try:
        with open(temp, 'wb') as out:
            for chunk in iter(lambda: file.stream.read(1024 * 1024), b''):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size == 0:
            return _plain("The uploaded file is empty.")
        if ALLOW_OVERWRITE:
            os.replace(temp, final)
        else:
            try:
                os.link(temp, final)  # fails if the file appeared meanwhile, unlike a rename
            except FileExistsError:
                return _plain("File already exists")
            except OSError:  # file system without hard links
                if os.path.exists(final):
                    return _plain("File already exists")
                os.replace(temp, final)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass

    user_id = current_user_id()
    logger.info(f"Uploaded {filename} to {final}")
    try:
        CATALOG.record_file(directory, filename, 'local', size, digest.hexdigest(), uploaded_by=user_id)
        with SessionLocal() as db:
            db.add(Package(name=directory, source='upload', user_id=str(user_id) if user_id else None))
            db.commit()
    except Exception as e:  # the file is stored; `pypihub reindex` repairs the catalog
        logger.error(f"Stored {filename} but could not record it in the database: {e}")
    return None


@app.route('/upload/<package>/', methods=['POST'])
@guard_credentials
@requires_auth
def upload_package(package):
    file = _uploaded_file()
    if not file:
        return _plain("No file part")
    return store_upload(file, package) or ('Uploaded', 200)


@app.route('/upload/', methods=['POST'])
@app.route('/', methods=['POST'])
@guard_credentials
@requires_auth
def twine_upload():
    file = _uploaded_file()
    if not file:
        logger.error("No file part in request [400]")
        return jsonify({"error": "No file part"}), 400
    project = dist.project_from_filename(file.filename or '')
    if not project:
        return _plain("Cannot read the project name from the file name. Use a standard wheel or sdist name.")
    return store_upload(file, project) or ('OK', 200)


@app.route('/packages/<package>/<filename>')
def serve_package(package, filename):
    if not (dist.valid_project_name(package) and dist.valid_filename(filename)):
        abort(404)
    folder = os.path.join(LOCAL_PKG_DIR, resolve_package_dir(LOCAL_PKG_DIR, package))
    return send_from_directory(folder, filename, max_age=FILE_MAX_AGE)


_cache_full_logged = 0.0


def cache_is_full():
    """True when PYPIHUB_CACHE_MAX_SIZE is set and reached: new files are then streamed without being stored."""
    global _cache_full_logged
    if not CACHE_MAX_BYTES:
        return False
    try:
        full = CATALOG.stats().cached_size >= CACHE_MAX_BYTES
    except Exception as e:
        logger.error(f"Could not read the cache size: {e}")
        return False
    if full and time.monotonic() - _cache_full_logged > 60:
        _cache_full_logged = time.monotonic()
        logger.warning("Cache is full (PYPIHUB_CACHE_MAX_SIZE): serving new files without storing them. Run `pypihub prune`.")
    return full


def stream_and_cache(upstream_response, entry, directory):
    """Send an upstream file to the client while storing it.

    The file is written to a temporary name and renamed only when it is complete and its hash
    matches, so a half-downloaded or corrupt file is never served to the next client.
    """
    folder = os.path.join(CACHE_DIR, directory)
    final = os.path.join(folder, entry.filename)
    store = not cache_is_full()

    def generate():
        digest, size = hashlib.sha256(), 0
        temp = out = None
        complete = committed = False
        try:
            if store:
                try:
                    os.makedirs(folder, exist_ok=True)
                    temp = os.path.join(folder, f".{entry.filename}.{uuid.uuid4().hex}.part")
                    out = open(temp, 'wb')
                except OSError as e:
                    logger.error(f"Cannot cache {entry.filename}: {e}")
                    out = None
            for chunk in upstream_response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                if out is not None:
                    try:
                        out.write(chunk)
                    except OSError as e:  # disk full: keep serving the client, stop storing
                        logger.error(f"Cache write failed for {entry.filename}: {e}")
                        out.close()
                        out = None
                yield chunk
            complete = True
        finally:
            upstream_response.close()
            if out is not None:
                out.close()
                if complete:
                    if entry.sha256 and digest.hexdigest() != entry.sha256:
                        logger.error(f"Hash mismatch for {entry.filename}: not cached")
                    else:
                        try:
                            os.replace(temp, final)
                            committed = True
                        except OSError as e:
                            logger.error(f"Could not store {entry.filename}: {e}")
            if temp and not committed:
                try:
                    os.remove(temp)
                except FileNotFoundError:
                    pass
            if committed:
                try:
                    CATALOG.record_file(directory, entry.filename, 'cached', size, digest.hexdigest())
                    with SessionLocal() as db:
                        db.add(Package(name=directory, source='pypi', user_id=None))
                        db.commit()
                except Exception as e:  # the file is stored; `pypihub reindex` repairs the catalog
                    logger.error(f"Cached {entry.filename} but could not record it in the database: {e}")

    response = app.response_class(generate(), mimetype='application/octet-stream')
    response.headers['Content-Disposition'] = f'attachment; filename={entry.filename}'
    response.headers['X-PyPihub-Cache'] = 'MISS'
    length = upstream_response.headers.get('Content-Length')
    if length and 'Content-Encoding' not in upstream_response.headers:
        response.headers['Content-Length'] = length
    return response


@app.route('/cache/<package>/<filename>')
def serve_cached(package, filename):
    if not (dist.valid_project_name(package) and dist.valid_filename(filename)):
        abort(404)
    directory = dir_for(CACHE_DIR, package)
    folder = os.path.join(CACHE_DIR, directory)
    if os.path.isfile(os.path.join(folder, filename)):
        return send_from_directory(folder, filename, max_age=FILE_MAX_AGE)

    try:
        entry = UPSTREAM.find(package, filename)
    except upstream_mod.UpstreamError as e:
        logger.error(f"Upstream index unavailable for {package}: {e}")
        abort(502, description="The upstream index is unavailable. Try again shortly.")
    if entry is None:
        abort(404, description="File not found on the upstream index")
    try:
        upstream_response = UPSTREAM.open(entry.url)
    except upstream_mod.UpstreamError as e:
        logger.error(f"Upstream download failed for {filename}: {e}")
        abort(502, description="The upstream download failed. Try again shortly.")
    return stream_and_cache(upstream_response, entry, directory)


@app.route('/simple/<package>/')
def simple_index(package):
    if not dist.valid_project_name(package):
        abort(404, description="Package not found")
    key = dist.project_key(package)
    local_name = resolve_package_dir(LOCAL_PKG_DIR, package)
    cache_name = dir_for(CACHE_DIR, package)

    try:
        known_hashes = CATALOG.hashes(key)
    except Exception as e:
        logger.warning(f"Could not read file hashes from the database: {e}")
        known_hashes = {}

    upstream_files, upstream_failed = [], False
    try:
        upstream_files = UPSTREAM.files(package) or []
    except upstream_mod.UpstreamError as e:
        upstream_failed = True
        logger.error(f"Upstream index unavailable for {package}: {e}")
    upstream_by_name = {u.filename: u for u in upstream_files}

    def anchor(prefix, directory, filename, sha256=None, info=None):
        href = f"{prefix}/{quote(directory)}/{quote(filename)}"
        if sha256:
            href += f"#sha256={sha256}"
        attrs = ''
        if info is not None and info.requires_python:
            attrs += f' data-requires-python="{html.escape(info.requires_python, quote=True)}"'
        if info is not None and info.yanked is not None:
            attrs += f' data-yanked="{html.escape(info.yanked, quote=True)}"'
        return f'<a href="{html.escape(href, quote=True)}"{attrs}>{html.escape(filename)}</a><br/>'

    links, seen = [], set()
    for source, root, directory, prefix in (
        ('local', LOCAL_PKG_DIR, local_name, '/packages'),
        ('cached', CACHE_DIR, cache_name, '/cache'),
    ):
        folder = os.path.join(root, directory)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name in seen or not dist.valid_filename(name) or not os.path.isfile(os.path.join(folder, name)):
                continue  # also hides the .part files of downloads in progress
            info = upstream_by_name.get(name) if source == 'cached' else None
            sha = known_hashes.get((source, name)) or (info.sha256 if info else None)
            links.append(anchor(prefix, directory, name, sha, info))
            seen.add(name)

    for entry in upstream_files:
        if entry.filename not in seen and dist.valid_filename(entry.filename):
            links.append(anchor('/cache', cache_name, entry.filename, entry.sha256, entry))

    if not links:
        if upstream_failed:
            abort(502, description="The upstream index is unavailable. Try again shortly.")
        abort(404, description="Package not found")

    page = (
        '<!DOCTYPE html><html><head><meta name="pypi:repository-version" content="1.0">'
        f'<title>Links for {html.escape(key)}</title></head><body><h1>Links for {html.escape(key)}</h1>'
        + ''.join(links)
        + '</body></html>'
    )
    return Response(page, mimetype='text/html')

@app.route('/signin', methods=['GET', 'POST'])
@guard_credentials
def signin():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if database:
            with SessionLocal() as db:
                user = find_user(db, username)
                if user and check_password(password, user.password):
                    session['user_id'] = user.id
                    return jsonify({"message": "Sign in successful"})
        return jsonify({"error": "Invalid username or password. Singin can be used with database."}), 401
    # Simple HTML form for sign in
    return '''
        <form method="post">
            Username: <input name="username"><br>
            Password: <input name="password" type="password"><br>
            <input type="submit" value="Sign In">
        </form>
    '''

@app.route('/signout')
def signout():
    session.pop('user_id', None)
    return jsonify({"message": "Signed out"})

def create_user_cli(username=None, password=None):
    username = (username or input("Username: ")).strip()
    password = password or getpass("Password: ")
    if not username or len(username) > 150:
        console.print("\n ❌ [bold red]The username must be 1 to 150 characters.[/]")
        return
    if database:
        with SessionLocal() as db:
            user = find_user(db, username)
            if user:
                # If user exists and password is provided, update password
                if password:
                    user.password = hash_password(password)  # type: ignore
                    db.commit()
                    console.print(f"\n ⚠️ [bold #FFFF00]Password updated for user '{username}'.[/]")
                else:
                    console.print(f"\n 🚩 [bold #00FFFF]User '{username}' already exists. No password provided to update.[/]")
                return
            hashed_pw = hash_password(password)
            new_user = User(username=username, password=hashed_pw)
            db.add(new_user)
            db.commit()
            console.print(f"\n ⚠️ [bold #FFFF00]User '{username}' created.[/]")
    else:
        console.print("\n ❌ [bold red]Database not enabled.[/]")
        
def list_user():
    if database:
        with SessionLocal() as db:
            users = db.query(User).all()
            if users:
                console.print("[bold green]Users:[/]")
                for user in users:
                    console.print(f"- {user.username}")
            else:
                console.print("[white on red]No users found.[/]")
    else:
        console.print("[bold red]Database not enabled, cannot list users.[/]")
    return

def version():
    # __version__.py must contain 'version = "x.y.z"' or 'version = 'x.y.z'' or 'version = x.y.z'
    for path in [Path(__file__).parent, Path(__file__).parent.parent]:
        version_file = path / '__version__.py'
        if version_file.is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("version", str(version_file))
            version_module = importlib.util.module_from_spec(spec)  # type: ignore
            spec.loader.exec_module(version_module)  # type: ignore
            if hasattr(version_module, 'version'):
                return f"[black on #00FFFF]{str(version_module.version)}[/]"
            break  # break here is not necessary, as return already exits the function
    installed = web_ui.get_version()
    if installed != 'dev':
        return f"[black on #00FFFF]{installed}[/]"
    return f"[white on blue]UNKNOWN VERSION[/]"

def usage():
    parser = argparse.ArgumentParser(
        description="PyPihub - A simple local PyPI server with caching and upload capabilities.",
        formatter_class=CustomRichHelpFormatter
    )
    parser.add_argument(
        '-c', '--config',
        type=str,
        default=CONFIGFILE,
        help='Path to the configuration file (default: %(default)s)'
    )
    parser.add_argument(
        '-b', '--base-dir',
        type=str,
        default=BASE_DIR,
        help='Base directory for the server (default: %(default)s)'
    )
    parser.add_argument(
        '-l', '--local-pkg-dir',
        type=str,
        default=LOCAL_PKG_DIR,
        help='Directory for local packages (default: %(default)s)'
    )
    parser.add_argument(
        '-C', '--cache-dir',
        type=str,
        default=CACHE_DIR,
        help='Directory for cached packages (default: %(default)s)'
    )
    parser.add_argument(
        '-i', '--pypi-simple-url',
        type=str,
        default=PYPI_SIMPLE_URL,
        help='PyPI simple index URL (default: %(default)s)'
    )
    parser.add_argument(
        '-H', '--host',
        type=str,
        default=HOST,
        help='Host to run the server on (default: %(default)s)'
    )
    parser.add_argument(
        '-P', '--port',
        type=int,
        # default=int(
        #     next(
        #         (
        #             v for v in [
        #                 os.getenv('PORT'),
        #                 CONFIG.get_config('server', 'port'),
        #                 getattr(settings, 'PORT', None) if hasattr(settings, 'PORT') else None
        #             ]
        #             if v not in (None, '')
        #         ),
        #         5000
        #     )
        # ),
        default = PORT,
        help='Port to run the server on (default: %(default)s)'
    )
    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'PyPihub {version()}',
        help='Show the version of PyPihub'
    )
    
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    
    parser.add_argument(
        'command',
        help='Command to run: serve (default), user (manage users), reindex (rebuild the package catalog from disk), prune (shrink the cache)',
        nargs='?',
        default='serve',
        choices=['serve', 'user', 'reindex', 'prune']
    )

    parser.add_argument(
        '--max-size',
        type=str,
        default=None,
        help='Size the cache may keep, e.g. 20GB or 512MiB (prune command). The oldest cached files are deleted first.'
    )
    
    parser.add_argument(
        '-u', '--username',
        type=str,
        help='Username for user management (required for user command)'
    )
    
    parser.add_argument(
        '-p', '--password',
        type=str,
        help='Password for user management (required for user command)',
        default=None
    )
    
    parser.add_argument(
        '--list',
        action='store_true',
        help='List all users (only works with user command)'
    )
    
    parser.add_argument(
        '-a', '--add',
        action='store_true',
        help='Add a new user (only works with user command, requires username and password)'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode (for development purposes only)'
    )

    if len(sys.argv) == 1:
        parser.print_help()
        return
    
    args = parser.parse_args()
    debug(args = args)
    logger.info(f"Arguments: {args}")
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        console.log("[bold green]Verbose mode enabled[/]")

    # Set environment variables or configuration based on args
    os.environ['CONFIGFILE'] = args.config if args.config else str(CONFIGFILE)
    os.environ['BASE_DIR'] = str(args.base_dir) if args.base_dir else str(BASE_DIR)
    os.environ['LOCAL_PKG_DIR'] = str(args.local_pkg_dir) if args.local_pkg_dir else str(LOCAL_PKG_DIR)
    os.environ['CACHE_DIR'] = str(args.cache_dir) if args.cache_dir else str(CACHE_DIR)
    os.environ['PYPI_SIMPLE_URL'] = args.pypi_simple_url if args.pypi_simple_url else str(PYPI_SIMPLE_URL)
    os.environ['HOST'] = args.host if args.host else str(HOST)
    os.environ['PORT'] = str(args.port) if args.port else str(PORT)

    debug(os_env_CONFIGFILE = os.environ['CONFIGFILE'])
    logger.info(f"os.environ['CONFIGFILE']: {os.environ['CONFIGFILE']}")
    debug(os_env_BASE_DIR = os.environ['BASE_DIR'])
    logger.info(f"os.environ['BASE_DIR']: {os.environ['BASE_DIR']}")
    debug(os_env_LOCAL_PKG_DIR = os.environ['LOCAL_PKG_DIR'])
    logger.info(f"os.environ['LOCAL_PKG_DIR']: {os.environ['LOCAL_PKG_DIR']}")
    debug(os_env_CACHE_DIR = os.environ['CACHE_DIR'])
    logger.info(f"os.environ['CACHE_DIR']: {os.environ['CACHE_DIR']}")
    debug(os_env_PYPI_SIMPLE_URL = os.environ['PYPI_SIMPLE_URL'])
    logger.info(f"os.environ['PYPI_SIMPLE_URL']: {os.environ['PYPI_SIMPLE_URL']}")
    debug(os_env_HOST = os.environ['HOST'])
    logger.info(f"os.environ['HOST']: {os.environ['HOST']}")
    debug(os_env_PORT = os.environ['PORT'])
    logger.info(f"os.environ['PORT']: {os.environ['PORT']}")
    
    if args.command == 'serve' and not args.list:
        # Start the Flask server
        app.config['BASE_DIR'] = BASE_DIR
        app.config['LOCAL_PKG_DIR'] = LOCAL_PKG_DIR
        app.config['CACHE_DIR'] = CACHE_DIR
        app.config['PYPI_SIMPLE_URL'] = PYPI_SIMPLE_URL
        app.config['HOST'] = args.host
        app.config['PORT'] = args.port or int(PORT) if PORT else 5000
        
        logger.info("Starting PyPihub server...")
        debug(app_config = app.config)
        logger.debug(f"app.config: {app.config}")
        console.print(f"[bold green]Starting PyPihub server on {HOST}:{PORT} ...[/]")
        console.print(f"[bold blue]Base Directory: {BASE_DIR}[/]")
        console.print(f"[bold blue]Local Package Directory: {LOCAL_PKG_DIR}[/]")
        console.print(f"[bold blue]Cache Directory: {CACHE_DIR}[/]")
        console.print(f"[bold blue]PyPI Simple URL: {PYPI_SIMPLE_URL}[/]")
        console.print(f"[bold blue]Configuration File: {CONFIGFILE}[/]")
        console.print(f"[bold blue]Host: {args.host}[/]")
        console.print(f"[bold blue]Port: {args.port}[/]")
        app.config['DEBUG'] = True
        app.config['ENV'] = 'development'
        app.config['TESTING'] = False
        debug(f"start server on {app.config['HOST']}:{app.config['PORT']}")    
        logger.notice(f"start server on {app.config['HOST']}:{app.config['PORT']}")  # type: ignore

        if CATALOG.is_empty():
            counts = CATALOG.reindex(LOCAL_PKG_DIR, CACHE_DIR)
            console.print(f"[bold blue]Indexed existing files: {counts['added']} added[/]")

        app.run(
            host=args.host,
            port=args.port,
            debug=True if args.verbose else False,
            use_reloader=False  # Disable reloader to avoid multiple instances
        )
    elif args.command == 'reindex':
        counts = CATALOG.reindex(LOCAL_PKG_DIR, CACHE_DIR)
        console.print(f"[bold green]Catalog updated:[/] {counts['added']} added, {counts['updated']} updated, {counts['removed']} removed")
        return
    elif args.command == 'prune':
        if not args.max_size:
            console.print("[bold red]--max-size is required, for example: pypihub prune --max-size 20GB[/]")
            return
        try:
            limit = dist.parse_size(args.max_size)
        except ValueError as e:
            console.print(f"[bold red]{e}[/]")
            return
        result = CATALOG.prune_cache(CACHE_DIR, limit)
        console.print(f"[bold green]Cache pruned:[/] {result['removed']} files deleted, {result['freed']:,} bytes freed, {result['remaining']:,} bytes remain")
        return
    elif args.command == 'user':
        if args.list:
            return list_user()
        elif args.add:
            if not args.username or not args.password:
                console.print("[bold red]Username and password are required to add a user.[/]")
                return
            create_user_cli(
                username=args.username,
                password=args.password
            )
            return
        elif args.username:
            if not args.password: 
                console.print("[black on #FFFF00]Password:[/] ", end = '')
                args.password = getpass()
            if args.username and not args.password:
                console.print("[bold red]Password is required to create a user.[/]")
                return
            create_user_cli(
                username=args.username,
                password=args.password
            )
            return
        else:
            console.print("\n❌ [black on #FFFF00]No command specified. Use --list to list users or --add to add a new user.[/]")
            return
    elif args.list:
        return list_user()

if __name__ == '__main__':    
    usage()
