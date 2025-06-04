#!/usr/bin/env python
# -*- coding: utf-8 -*-

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
import logging
try:
    from . import logger as Logger
except:
    import logger as Logger

# logging.basicConfig(level=logging.CRITICAL)

logger = Logger.setup_logging()
logger.setLevel(Logger.EMERGENCY_LEVEL)

# logger = logging.getLogger(__name__)

import requests
from flask import Flask, send_from_directory, request, render_template_string, abort, Response, jsonify
from bs4 import BeautifulSoup
from pydebugger.debug import debug
from configset import configset
from pathlib import Path
from functools import wraps

if (Path(__file__).parent / 'settings.py').is_file():
    try:
        from . import settings as local_settings
    except:
        import settings as local_settings
else:
    local_settings = None

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

import argparse
from pathlib import Path

class settings:
    #make this possbile/available like 'settings.var' then return value from 'var = value' in settings.py
    def __getattr__(self, item):
        if local_settings is None:
            return None
        if hasattr(local_settings, item):
            return getattr(local_settings, item)
        # raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")
        return ""

settings = settings()

app = Flask(__name__)
CONFIGFILE = os.getenv('CONFIGFILE') if os.getenv('CONFIGFILE') else settings.CONFIGFILE if Path(settings.CONFIGFILE).is_file() else str(Path(__file__).parent / Path(__file__).stem) + '.ini'
debug(CONFIGFILE = CONFIGFILE)
logger.info(f"CONFIGFILE: {CONFIGFILE}")
CONFIG = configset(CONFIGFILE)

BASE_DIR = Path(os.getenv('BASE_DIR')) if os.getenv('BASE_DIR') and Path(os.getenv('BASE_DIR', CONFIG.get_config('dirs', 'base'))).is_dir() else Path(settings.BASE_DIR) if settings.BASE_DIR else Path(__file__).parent
debug(BASE_DIR = str(BASE_DIR))
logger.info(f"BASE_DIR: {BASE_DIR}")

LOCAL_PKG_DIR = Path(os.getenv('LOCAL_PKG_DIR')) if os.getenv('LOCAL_PKG_DIR') and Path(os.getenv('LOCAL_PKG_DIR', CONFIG.get_config('dirs', 'local_pkg'))).is_dir() else Path(settings.LOCAL_PKG_DIR) if settings.LOCAL_PKG_DIR else BASE_DIR / "packages"
debug(LOCAL_PKG_DIR = str(LOCAL_PKG_DIR))
logger.info(f"LOCAL_PKG_DIR: {LOCAL_PKG_DIR}")

CACHE_DIR = Path(os.getenv('CACHE_DIR')) if os.getenv('CACHE_DIR') and Path(os.getenv('CACHE_DIR', CONFIG.get_config('dirs', 'cache'))).is_dir() else Path(settings.CACHE_DIR) if settings.CACHE_DIR else BASE_DIR / "cache"
debug(CACHE_DIR = str(CACHE_DIR))
logger.info(f"CACHE_DIR: {CACHE_DIR}")

PYPI_SIMPLE_URL = os.getenv('PYPI_SIMPLE_URL', settings.PYPI_SIMPLE_URL) or CONFIG.get_config('urls', 'pypi_simple') or "https://pypi.org/simple"
debug(PYPI_SIMPLE_URL = PYPI_SIMPLE_URL)
logger.info(f"PYPI_SIMPLE_URL: {PYPI_SIMPLE_URL}")

HOST = os.getenv('HOST', settings.HOST) or CONFIG.get_config('server', 'host') or '0.0.0.0'
debug(HOST = HOST)
logger.info(f"HOST: {HOST}")

PORT = int(os.getenv('PORT', settings.PORT) or CONFIG.get_config('server', 'port') or 5000)
debug(PORT = PORT)
logger.info(f"PORT: {PORT}")

print("\n")

os.makedirs(LOCAL_PKG_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

def index_usage():
    return f"""
    <h1>PyPihub - Local PyPI Server</h1>
    <p>Upload packages to your local PyPI server.</p>
    <h2>Usage</h2>
    <ul>
        <li>Upload a package: <code>POST /upload/&lt;package&gt;/</code> with file in form-data under 'file'</li>
        <li><i>twine</i> supported</li>
        <li>Access local packages: <code>/</code></li>
        <li>Access package files: <code>/packages/&lt;package&gt;/&lt;filename&gt;</code></li>
        <li>Access cached files: <code>/cache/&lt;package&gt;/&lt;filename&gt;</code></li>
        <li>Simple index for a package: <code>/simple/&lt;package&gt;/</code></li>
    </ul>
    """

@app.route('/')
def index():
    packages = os.listdir(LOCAL_PKG_DIR)
    debug(len_packages = len(packages))
    logger.notice(f"len(packages): {len(packages)}")
    return render_template_string(index_usage() + '<br/><br/>' + '<h1>Local Packages</h1><ul>{% for p in pkgs %}<li><a href="/simple/{{p}}/">{{p}}</a></li>{% endfor %}</ul>', pkgs=packages)

def check_auth(username, password):
    # settings.AUTHS harus berupa list of tuple/list: [("user", "pass"), ...]
    auths = getattr(settings, 'AUTHS', None) or CONFIG.get_config_as_list('auths', 'users')
    if not auths or not isinstance(auths, (list, tuple)) or not auths:
        # Default: [('pypihub', 'pypihub')]
        auths = [('pypihub', 'pypihub')]
    return (username, password) in auths

def authenticate():
    # Kembalikan JSON agar twine bisa menampilkan pesan error yang jelas
    # resp = jsonify({"error": "Invalid username or password"})
    resp = Response("Invalid username or password", status=400, content_type="text/plain")
    resp.status_code = 401
    resp.headers['WWW-Authenticate'] = 'Basic realm="Login Required"'
    return resp

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

@app.route('/upload/<package>/', methods=['POST'])
@requires_auth
def upload_package(package):
    file = request.files['file']
    debug(file = file)
    logger.debug(f"file: {file}")
    save_path = os.path.join(LOCAL_PKG_DIR, package)
    debug(save_path = save_path, is_dir = os.path.isdir(save_path))
    logger.debug(f"save_path: {save_path}, is_dir: {os.path.isdir(save_path)}")
    os.makedirs(save_path, exist_ok=True)
    file_path = os.path.join(save_path, file.filename)
    debug(file_path = file_path)
    logger.debug(f"file_path: {file_path}")
    file.save(file_path)
    logger.info(f"Uploaded {file.filename} to {file_path}")
    return 'Uploaded', 200

@app.route('/upload/', methods=['POST'])
@app.route('/', methods=['POST'])
@requires_auth
def twine_upload():
    # Coba ambil file dari beberapa kemungkinan field
    file = (
        request.files.get('file') or
        request.files.get('content') or
        request.files.get('distribution')
    )
    debug(file = file)
    logger.warning(f"file: {file}")
    if not file:
        # Coba ambil dari field pertama jika ada
        if request.files:
            file = next(iter(request.files.values()))
            debug(file = file)
            logger.notice(f"file: {file}")
        else:
            debug(error = "No file part in request [400]")
            logger.error("No file part in request [400]")
            # return "No file part", 400
            return jsonify({"error": "No file part"}), 400

    filename = file.filename
    debug(filename = filename)
    # Ekstrak nama package (sederhana, bisa lebih baik)
    pkg_name = filename.split('-')[0].lower()
    debug(pkg_name = pkg_name)
    logger.info(f"pkg_name: {pkg_name}")
    save_path = os.path.join(LOCAL_PKG_DIR, pkg_name)
    debug(save_path = save_path)
    logger.info(f"save_path: {save_path}")
    os.makedirs(save_path, exist_ok=True)
    file_path = os.path.join(save_path, filename)
    debug(file_path = file_path)
    logger.info(f"file_path: {file_path}")
    if os.path.isfile(file_path):
        debug(error = f"File {filename} already exists in {save_path} [409]")
        logger.error(f"File {filename} already exists in {save_path} [409]")
        # return jsonify({"error": f"File {filename} already exists"}), 409
        return Response("File already exists", status=400, content_type="text/plain")
    file.save(file_path)
    logger.info(f"Uploaded {file.filename} to {file_path} (via twine)")
    return 'OK', 200

@app.route('/packages/<package>/<filename>')
def serve_package(package, filename):
    local_path = os.path.join(LOCAL_PKG_DIR, package)
    debug(local_path = local_path)
    logger.debug(f"local_path: {local_path}")
    return send_from_directory(local_path, filename)

@app.route('/cache/<package>/<filename>')
def serve_cached(package, filename):
    cache_path = os.path.join(CACHE_DIR, package)
    debug(cache_path = cache_path)
    logger.notice(f"cache_path: {cache_path}")
    os.makedirs(cache_path, exist_ok=True)
    file_path = os.path.join(cache_path, filename)
    debug(file_path = file_path, is_file = os.path.isfile(file_path))
    logger.info(f"file_path: {file_path}, is_file: {os.path.isfile(file_path)}")

    if os.path.exists(file_path):
        # Sudah ada di cache, langsung serve
        return send_from_directory(cache_path, filename)

    # Cari URL file di PyPI simple index
    r = requests.get(f"{PYPI_SIMPLE_URL}/{package}/")
    debug(requests_status_code = r.status_code)
    logger.notice(f"requests.get status_code: {r.status_code}")
    if r.status_code != 200:
        debug(error = "Package not found on PyPI [404]")
        logger.error("Package not found on PyPI [404]")
        abort(404, description="Package not found on PyPI")
    soup = BeautifulSoup(r.text, 'html.parser')
    file_url = None
    for a in soup.find_all('a'):
        href = a.get('href')
        if href and href.split('/')[-1].split('#')[0] == filename:
            # Gunakan href langsung jika sudah absolut, jika relatif tambahkan domain
            if href.startswith('http'):
                file_url = href
            else:
                file_url = f"https://files.pythonhosted.org{href}"
            break
    debug(file_url = file_url)
    logger.debug(f"file_url: {file_url}")
    if not file_url:
        debug(error = "File not found on PyPI [404]")
        logger.error("File not found on PyPI [404]")
        abort(404, description="File not found on PyPI")

    # Stream download dari PyPI ke client dan ke disk secara paralel
    def generate():
        with requests.get(file_url, stream=True) as resp:
            if resp.status_code != 200:
                debug(error = "Failed to download from PyPI [404]")
                logger.error("Failed to download from PyPI [404]")
                abort(404, description="Failed to download from PyPI")
            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        yield chunk

    response = app.response_class(generate(), mimetype='application/octet-stream')
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response

@app.route('/simple/<package>/')
def simple_index(package):
    local_path = os.path.join(LOCAL_PKG_DIR, package)
    debug(local_path = local_path)
    logger.debug(f"local_path: {local_path}")
    cached_path = os.path.join(CACHE_DIR, package)
    debug(cached_path = cached_path)
    logger.debug(f"cached_path: {cached_path}")
    os.makedirs(cached_path, exist_ok=True)
    links = []
    found = False

    # Local files
    if os.path.exists(local_path):
        for f in os.listdir(local_path):
            links.append(f'<a href="/packages/{package}/{f}">{f}</a>')
            found = True

    # Cached files
    if os.path.exists(cached_path):
        for f in os.listdir(cached_path):
            links.append(f'<a href="/cache/{package}/{f}">{f}</a>')
            found = True

    # Fetch from PyPI (hanya generate link, TIDAK download)
    r = requests.get(f"{PYPI_SIMPLE_URL}/{package}/")
    debug(requests_status_code = r.status_code)
    logger.notice(f"requests.get status_code: {r.status_code}")
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, 'html.parser')
        for a in soup.find_all('a'):
            href = a.get('href')
            if not href:
                continue
            filename = href.split('/')[-1].split('#')[0]
            # Ambil hash jika ada
            hash_fragment = ''
            if '#' in href:
                hash_fragment = href[href.index('#'):]
            # Jika belum ada di local/cache, tambahkan link ke /cache/ dengan hash
            if not os.path.exists(os.path.join(local_path, filename)) and not os.path.exists(os.path.join(cached_path, filename)):
                links.append(f'<a href="/cache/{package}/{filename}{hash_fragment}">{filename}</a>')
                found = True
    else:
        if not found:
            debug(error = "Package not found [404]")
            logger.error("Package not found [404]")
            abort(404, description="Package not found")

    html = f"<html><body>{''.join(links)}</body></html>"
    return html

def version():
    # __version__.py must contain 'version = "x.y.z"' or 'version = 'x.y.z'' or 'version = x.y.z'
    for path in [Path(__file__).parent, Path(__file__).parent.parent]:
        version_file = path / '__version__.py'
        if version_file.is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("version", str(version_file))
            version_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(version_module)
            if hasattr(version_module, 'version'):
                return f"[black on #00FFFF]{str(version_module.version)}[/]"
            break  # break here is not necessary, as return already exits the function
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
        '-p', '--pypi-simple-url',
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
    
    parser.add_argument('serve', help='Serve the local PyPI server with default settings', nargs='?', default='serve')

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
        
    debug(f"start server on {os.environ['HOST']}:{os.environ['PORT']}")    
    logger.notice(f"start server on {os.environ['HOST']}:{os.environ['PORT']}")

    if args.serve == 'serve':
        # Start the Flask server
        app.config['BASE_DIR'] = BASE_DIR
        app.config['LOCAL_PKG_DIR'] = LOCAL_PKG_DIR
        app.config['CACHE_DIR'] = CACHE_DIR
        app.config['PYPI_SIMPLE_URL'] = PYPI_SIMPLE_URL
        app.config['HOST'] = HOST
        app.config['PORT'] = int(PORT) if PORT else 5000
        
        logger.info("Starting PyPihub server...")
        debug(app_config = app.config)
        logger.debug(f"app.config: {app.config}")
        console.print(f"[bold green]Starting PyPihub server on {HOST}:{PORT} ...[/]")
        console.print(f"[bold blue]Base Directory: {BASE_DIR}[/]")
        console.print(f"[bold blue]Local Package Directory: {LOCAL_PKG_DIR}[/]")
        console.print(f"[bold blue]Cache Directory: {CACHE_DIR}[/]")
        console.print(f"[bold blue]PyPI Simple URL: {PYPI_SIMPLE_URL}[/]")
        console.print(f"[bold blue]Configuration File: {CONFIGFILE}[/]")
        console.print(f"[bold blue]Host: {HOST}[/]")
        console.print(f"[bold blue]Port: {PORT}[/]")
        app.config['DEBUG'] = True
        app.config['ENV'] = 'development'
        app.config['TESTING'] = False
        app.config['SECRET_KEY'] = 'your_secret_key'  # Set a secret key for session management
        app.run()
        # Use the environment variables set above
        
    # app.run(
    #     host = os.environ['HOST'],
    #     port = os.environ['PORT']
    # )

if __name__ == '__main__':
    
    usage()
