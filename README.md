# PyPihub - Local PyPI Server

PyPihub is a simple local PyPI server with caching and package upload capabilities. This server allows you to host your Python packages locally, cache packages from the official PyPI, and supports package uploads via `twine`.

This version uses Flask. If you install via pip, use: `pip install pypihub[flask]`

When installing with pip, PyPihub will not interrupt the download or installation process. If the requested package is not found in the `cache` or `package` directory, PyPihub will fetch it directly from pypi.org and simultaneously serve it to the client. This means the client does not have to wait for the server to finish downloading before receiving the package. All downloaded packages from pypi.org will also be saved to the cache for future use.

## Key Features

- **Multi-platform**: Supports any platform (Linux, Windows, Mac, etc.)
- **Local Package Hosting**: Host your own Python packages locally
- **PyPI Caching**: Automatic caching of packages from official PyPI for faster access
- **Twine Support**: Upload packages using `twine` with authentication
- **Web Interface**: Simple web interface for browsing packages
- **Flexible Configuration**: Configuration via INI files, environment variables, or command arguments

## Architecture

### Components

```mermaid
flowchart TB
    subgraph Clients
        direction LR
        B["Browser"]
        P["pip"]
        T["twine / curl"]
    end

    subgraph Server["PyPihub: gunicorn workers (stateless, add more behind a load balancer)"]
        direction LR
        subgraph UI["Web interface (web.py)"]
            V["Package list: search, sort, paging<br/>Package pages: README, links, releases<br/>/healthz"]
        end
        subgraph Index["Package index (pypihub.py)"]
            S["Download side<br/>/simple/NAME/<br/>/packages/NAME/FILE<br/>/cache/NAME/FILE"]
            U["Upload and sign-in<br/>POST / , POST /upload/NAME/<br/>basic auth, rate limited"]
        end
    end

    subgraph State["State"]
        direction LR
        DB[("Database<br/>SQLite, PostgreSQL or MySQL<br/>users, file catalog,<br/>copied PyPI details")]
        FS[("Files on disk<br/>packages/ uploads<br/>cache/ from PyPI")]
        R[("Redis (optional)<br/>shared cache,<br/>rate-limit counters")]
    end

    X["pypi.org<br/>simple index, JSON API, files"]

    B --> V
    P --> S
    T --> U
    V -- "pages of packages" --> DB
    V -. "cached details" .-> R
    S -- "read, save new downloads" --> FS
    S -. "file hashes" .-> DB
    U -- "write files, check users, record" --> FS
    U --> DB
    S -. "index, files, details" .-> X
    V -. "README, links, releases" .-> X
```

[![Architecture: components](https://i.ibb.co/zD2S4Vk/architecture-components.png)](https://i.ibb.co/zD2S4Vk/architecture-components.png)

[![Architecture 0](https://i.ibb.co/Kp08Bxs3/pypihub-architecture4.jpg)](https://i.ibb.co/Kp08Bxs3/pypihub-architecture4.jpg)

[![Architecture 1](https://i.ibb.co/nMmXnfLS/architecture1.jpg)](https://i.ibb.co/nMmXnfLS/architecture1.jpg)

The files on disk are what pip downloads. The database is an index of them (so a page of the package list is one SQL
query, not a directory scan) plus the accounts and the details copied from PyPI. `pypihub reindex` rebuilds the index
from disk at any time. Redis only holds things that can be rebuilt, so losing it costs nothing but warm-up time.

### What happens on `pip install`

```mermaid
sequenceDiagram
    participant pip
    participant Srv as PyPihub
    participant Disk as /data (packages, cache)
    participant Up as pypi.org and files.pythonhosted.org

    pip->>Srv: GET /simple/my-pkg/
    Srv->>Disk: list local and cached files (name matched per PEP 503)
    Srv->>Up: GET simple index of my-pkg
    Up-->>Srv: file list
    Srv-->>pip: HTML with links to /packages/... and /cache/...

    pip->>Srv: GET /cache/my-pkg/my_pkg-1.0-py3-none-any.whl
    alt file is already in the cache
        Srv->>Disk: read file
        Srv-->>pip: file
    else first request for this file
        Srv->>Up: download file (streamed)
        Up-->>Srv: bytes
        Srv-->>pip: the same bytes, as they arrive
        Srv->>Disk: write the file to the cache
    end
```

[![Architecture 2](https://i.ibb.co/zTgqyw1b/pypihub-architecture2.jpg)](https://i.ibb.co/zTgqyw1b/pypihub-architecture2.jpg)

Files you uploaded are served from `/packages/...` straight from disk and never touch PyPI.

### Build and release pipeline

```mermaid
flowchart LR
    PR["push to master<br/>or pull request"] --> CI["ci.yml<br/>lint, tests, wheel smoke test,<br/>Docker build and run"]
    TG["push to master<br/>or tag v*"] --> DK["docker.yml<br/>build linux/amd64 and linux/arm64"]
    DK --> GH[("ghcr.io image")]
    RL["GitHub release published"] --> PB["publish.yml<br/>tag must equal __version__.py,<br/>build, twine check"]
    PB --> PY[("PyPI<br/>trusted publishing")]
```

[![Architecture 3](https://i.ibb.co/1G0HxgY4/pypihub-architecture3.jpg)](https://i.ibb.co/1G0HxgY4/pypihub-architecture3.jpg)

<p align="center">
  <a href="https://i.ibb.co/PZj0xJVv/pypihub-screenshot1.png" target="_blank">
    <img src="https://i.ibb.co/PZj0xJVv/pypihub-screenshot1.png" width="98%" alt="Screenshot 1">
  </a>
  <a href="https://i.ibb.co/mVVr09Cs/pypihub-screenshot2.png" target="_blank">
    <img src="https://i.ibb.co/mVVr09Cs/pypihub-screenshot2.png" width="98%" alt="Screenshot 2">
  </a>
  <a href="https://i.ibb.co/TxjmB6Df/pypihub-screenshot3.png" target="_blank">
    <img src="https://i.ibb.co/TxjmB6Df/pypihub-screenshot3.png" width="98%" alt="Screenshot 3">
  </a>
</p>


## Installation

### Requirements

Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

### Download and Setup

```bash
git clone <repository-url>
cd pypihub
```

Or you can install with pip:
```bash
$ pip install pypihub[flask]
```

## Configuration

### 1. Configuration File (pypihub.ini)

Create a `pypihub.ini` as an alternative config file, or it will be created automatically in the same directory as `pypihub.py`:

```ini
[dirs]
base = /path/to/base/directory
local_pkg = /path/to/local/packages
cache = /path/to/cache

[urls]
pypi_simple = https://pypi.org/simple

[server]
host = 0.0.0.0
port = 5000

[auths]
users = pypihub,pypihub;user2,pass2
```

### 2. Settings File (settings.py)

Alternatively, create a `settings.py` file (see example below):

```python
BASE_DIR = "/path/to/base"
LOCAL_PKG_DIR = "/path/to/packages"
CACHE_DIR = "/path/to/cache"
PYPI_SIMPLE_URL = "https://pypi.org/simple"
HOST = "0.0.0.0"
PORT = 5000
AUTHS = [("pypihub", "pypihub"), ("user2", "pass2")]
CONFIGFILE = "pypihub.ini"
```

### 3. Environment Variables

```bash
export BASE_DIR="/path/to/base"
export LOCAL_PKG_DIR="/path/to/packages"
export CACHE_DIR="/path/to/cache"
export PYPI_SIMPLE_URL="https://pypi.org/simple"
export HOST="0.0.0.0"
export PORT="5000"
export CONFIGFILE="pypihub.ini"
```

## Usage

### Running the Server

```bash
# Using default settings
python pypihub serve

# With custom configuration
# Option: '-c /path/to/config.ini'
python pypihub -c /path/to/config.ini -H 127.0.0.1 -P 8080

# Verbose mode
python pypihub -v

# Show help
python pypihub -h
```

### Command Line Arguments

```
-c, --config          Path to configuration file
-b, --base-dir        Base directory for server
-l, --local-pkg-dir   Directory for local packages
-C, --cache-dir       Directory for cached packages
-p, --pypi-simple-url PyPI simple index URL
-H, --host            Host to run server on
-P, --port            Port to run server on
-V, --version         Show PyPihub version
-v, --verbose         Enable verbose output
```

Commands (the first argument, `serve` by default):

```
pypihub                       Run the development server (production: gunicorn, see Docker)
pypihub user -u NAME          Create a user or change a password (-l lists users)
pypihub reindex               Make the package catalog match the files on disk (safe to run any time)
pypihub prune --max-size 20GB Delete the oldest cached files until the cache fits
```

After upgrading an existing installation, run `pypihub reindex` once so the package list sees the files already on
disk (the development server and the Docker image do it for you at start).

## Package Upload

### Using Twine

1. **Configure ~/.pypirc**:

```ini
[distutils]
index-servers = pypihub

[pypihub]
repository = http://localhost:5000/
username = pypihub
password = pypihub
```

2. **Upload package**:

```bash
# Build package first
python setup.py sdist bdist_wheel

# Upload to pypihub
twine upload --repository pypihub dist/*
```

### Manual Upload via HTTP

```bash
curl -X POST \
  -u pypihub:pypihub \
  -F "file=@package-1.0.0-py3-none-any.whl" \
  http://localhost:5000/upload/package-name/
```

## Installing Packages from PyPihub

### Configure pip

Add to `~/.pip/pip.conf` (Linux/Mac) or `%APPDATA%\pip\pip.ini` (Windows):

```ini
[global]
extra-index-url = http://localhost:5000/simple/
trusted-host = localhost
```

### Install Package

```bash
# Install from pypihub (with fallback to PyPI)
pip install package-name

# Install only from pypihub
pip install --index-url http://localhost:5000/simple/ package-name

# Install with extra index
pip install --extra-index-url http://localhost:5000/simple/ package-name
```

## Web Interface

Open the server address in a browser. The interface follows the system light or dark setting and has a
theme switch in the header (System, Light, Dark; the choice is remembered). Press `/` to jump to the search box.
Typing a name that is not stored yet shows the exact `pip install` command, because installing it through
PyPihub fetches it from PyPI and caches it.

It is built on Bootstrap 5.3 with self-hosted fonts and icons, all vendored under `pypihub/static`
(see `pypihub/static/vendor/README.md`), so it works on a network without internet access. README images load
from their own hosts, as on pypi.org.

## Configuration reference

Every setting can be an environment variable.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite in the data directory | `postgresql://user:pass@host/db` or `mysql://user:pass@host/db` (bare schemes use the bundled psycopg and PyMySQL drivers) |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | 10, 20 | Connections per worker. Keep `workers x (size + overflow)` below the server's connection limit |
| `REDIS_URL` | none | Shared cache and rate-limit counters. Without it each worker has its own |
| `SECRET_KEY` | random per process | Signs sign-in cookies. Set it when running more than one worker (the Docker image keeps one in the volume) |
| `PYPI_SIMPLE_URL` | `https://pypi.org/simple` | Upstream index. The JSON API for package pages is derived from it, or set `PYPI_JSON_URL` |
| `PYPIHUB_META`, `PYPIHUB_META_TTL` | on, 21600 | Package page details from PyPI: switch, and seconds before a refresh |
| `PYPIHUB_META_MISSING_TTL` | 3600 | Seconds "PyPI does not know this package" is remembered |
| `PYPIHUB_UPSTREAM_TTL` | 300 | Seconds a project's file list is reused before it is re-checked |
| `PYPIHUB_UPSTREAM_STALE_TTL` | 86400 | Seconds an older file list may still be used while PyPI is failing |
| `PYPIHUB_UPSTREAM_TIMEOUT` | `3.05,20` | Connect and read timeout towards PyPI, in seconds |
| `PYPIHUB_UPSTREAM_BACKOFF` | 15 | After a failure, seconds to answer from stored copies instead of waiting on PyPI again |
| `PYPIHUB_CACHE_MAX_SIZE` | unlimited | e.g. `100GB`. At the limit new downloads are streamed but not stored |
| `PYPIHUB_ALLOW_OVERWRITE` | off | Uploaded files are immutable unless this is on |
| `PYPIHUB_MAX_UPLOAD_MB` | 200 | Largest upload |
| `PYPIHUB_RATE_LIMIT_DEFAULT` | none | All requests per client, e.g. `1200 per minute` |
| `PYPIHUB_RATE_LIMIT_UPLOAD`, `PYPIHUB_RATE_LIMIT_AUTH_FAILURES` | `60 per minute`, `10 per minute` | Upload requests, and rejected logins (password guessing). `0` switches one off |
| `PYPIHUB_TRUST_PROXY` | off | Set to `1` behind a reverse proxy, so client addresses and links use `X-Forwarded-*` |
| `PYPIHUB_UI_PER_PAGE` | 30 | Packages per page |

## What is stored where

| Data | Stored in | For how long |
|---|---|---|
| Accounts | database | until changed |
| One row per stored file (name, version, size, SHA-256, source, date) | database | until the file is deleted, `reindex` or `prune`. Written when a file is uploaded or cached |
| Package counters on the home page | cache | 10 seconds, and dropped at once when a file is added |
| **Package details copied from PyPI** (README, links, classifiers, dependencies, release history) | **database** (durable) **and cache** (hot copy) | Fresh for `PYPIHUB_META_TTL` (6 h). Then re-checked with the ETag (see below). If PyPI cannot be reached the database copy is shown with a note, with no time limit. "Not on PyPI" is remembered for 1 h |
| PyPI's file list for a project (for `/simple/`) | cache only | Fresh for 5 min (`PYPIHUB_UPSTREAM_TTL`), then re-checked with the ETag. The last copy is kept 24 h to use while PyPI fails. "Not found" 60 s |
| "PyPI failed a moment ago" flag | cache | 15 s (`PYPIHUB_UPSTREAM_BACKOFF`) |
| Rate-limit counters | cache | the window of each limit |
| The package files | disk (`packages/`, `cache/`) | until removed |

"Cache" is **Redis when `REDIS_URL` is set**. Otherwise it is a small in-memory cache inside each worker process:
it is lost on restart and not shared between workers. Nothing important lives only there, because details are also in
the database, so a restart does not send the server back to PyPI for packages it already knows.

**How a change on PyPI is noticed.** PyPI has no push notification, so PyPihub asks, cheaply. Every document is stored
with the `ETag` PyPI sent. When a copy expires the request carries `If-None-Match`; if nothing changed PyPI answers
`304` with no body (measured on pypi.org: about 18 ms and 0 bytes for numpy, whose full document is 3.7 MB). Only when
the ETag differs is the document downloaded, reduced to the fields the page uses (numpy: 3.7 MB to 49 kB), rendered
and stored again. A new release therefore shows up within `PYPIHUB_META_TTL` on package pages and within
`PYPIHUB_UPSTREAM_TTL` for `pip`. Files you upload, or that are cached, appear in the list at once. Files changed by hand
on disk appear after `pypihub reindex`.

## Databases

**A database is always on.** With no configuration PyPihub uses SQLite in `pypihub.db` in the data directory
(`BASE_DIR`; `/data` in Docker). Redis is **off** until `REDIS_URL` is set.

To use PostgreSQL or MySQL set one variable and start the server; the tables are created at startup:

```bash
export DATABASE_URL=postgresql://pypihub:secret@127.0.0.1:5432/pypihub
export DATABASE_URL=mysql://pypihub:secret@127.0.0.1:3306/pypihub      # alternative
export REDIS_URL=redis://127.0.0.1:6379/0                                # optional
pip install "pypihub[all]"      # [postgres] or [mysql] and [redis] pull in only what you use
```

The database and the user must already exist (`CREATE DATABASE pypihub`); PyPihub creates the tables, not the
database. Special characters in a password need no escaping when you use the separate settings below. The same
values can also come from `settings.py` (`DATABASE_URL`, `DB_TYPE`, `DB_HOST`, ...), from `pypihub.ini` (`[database]`:
`type`, `host`, `port`, `name`, `username`, `password`, `path`) or from the `DB_*` variables. Where more than one is
set, `settings.py` wins over `pypihub.ini`, which wins over the environment; `DATABASE_URL` wins over all of them.
`REDIS_URL` comes from the environment or `settings.py`.

PyPihub uses SQLAlchemy and is tested on every change against **SQLite**, **PostgreSQL 16** and **MySQL 8.0**
(the whole test suite runs on each; see `.github/workflows/ci.yml`). Tables are created at startup, including when
several workers start at once, and new versions add tables without touching existing data.

A relational database is the right fit here: a few thousand small transactional writes (accounts, one catalog row
per stored file), many indexed reads and paging. ClickHouse is built for the opposite (huge append-only analytics,
no row-level updates or unique constraints), so it would be a poor home for accounts and the file catalog. It would
only make sense for download statistics at a scale PostgreSQL cannot count, and PyPihub has none.

Use PostgreSQL for a public service. SQLite is fine for a single server with modest traffic.

## Production deployment

```bash
cp .env.example .env     # set POSTGRES_PASSWORD, PYPIHUB_ADMIN_USER and PYPIHUB_ADMIN_PASSWORD
docker compose -f docker-compose.production.yml up -d
```

This starts PyPihub, PostgreSQL and Redis. Put a reverse proxy with TLS in front (and keep `PYPIHUB_TRUST_PROXY=1`).
What to look after:

- **Disk.** Anyone can make a public server cache packages, so set `PYPIHUB_CACHE_MAX_SIZE` and run
  `pypihub prune --max-size 80GB` from cron (the oldest cached files go first).
- **Backups.** The database and the `packages/` directory. The `cache/` directory and Redis can be rebuilt.
- **After restoring or editing files by hand,** run `pypihub reindex`. The container does it at every start.
- **Workers.** `WEB_CONCURRENCY` x `GUNICORN_THREADS` requests run at once. A slow or dead PyPI does not freeze the
  site: calls have timeouts and, after a failure, workers stop asking for a while.
- **More than one server** needs the `packages/` and `cache/` directories on shared storage. Everything else is
  already shared through the database and Redis.

There is no task queue (Celery, RabbitMQ) on purpose: downloads stream straight through, and the periodic jobs
(`reindex`, `prune`) are one-line commands for cron. A queue would add two services and no capability. It becomes
worth adding when there is background work such as scanning uploads for malware.

## Not covered

PyPihub is a caching index with account-based uploads, not a copy of pypi.org: there is no self-service registration,
no per-project ownership (any account may upload any project name), no yanking or deleting through the interface, and
no download statistics. Object storage (S3) for the files is not supported yet.

## Docker

```bash
cp .env.example .env        # set PYPIHUB_ADMIN_USER and PYPIHUB_ADMIN_PASSWORD
docker compose up -d
```

Or without compose:

```bash
docker run -d --name pypihub -p 5000:5000 -v pypihub-data:/data \
  -e PYPIHUB_ADMIN_USER=admin -e PYPIHUB_ADMIN_PASSWORD='change-me' \
  ghcr.io/cumulus13/pypihub_flask:latest
```

- Everything is stored in `/data` (uploaded packages, PyPI cache, user database, config). The container runs as UID 10001;
  for a bind mount run `chown -R 10001:10001 <host directory>` first.
- The account in `PYPIHUB_ADMIN_USER` is created, or has its password reset, each time the container starts.
  Use `PYPIHUB_ADMIN_PASSWORD_FILE` to read the password from a Docker secret. Add more users with
  `docker exec -it pypihub pypihub user -u NAME`.
- Without `SECRET_KEY` a key is generated once and kept in the volume.
- For PostgreSQL and Redis use `docker-compose.production.yml` (see Production deployment).
- Served by gunicorn (`WEB_CONCURRENCY` workers, `GUNICORN_THREADS` threads each). Images for `linux/amd64` and
  `linux/arm64` are published to GHCR: `:edge` from `master`, `:X.Y.Z`, `:X.Y` and `:latest` from release tags.

## Development, Tests and CI

```bash
pip install -r requirements-dev.txt
pytest
```

That runs everything on SQLite and needs no network. To run it against PostgreSQL, MySQL and Redis as well:

```bash
export PYPIHUB_TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1/pypihub_test
export PYPIHUB_TEST_MYSQL_URL=mysql://user:pass@127.0.0.1/pypihub_test
export PYPIHUB_TEST_REDIS_URL=redis://127.0.0.1:6379/0
pytest                                                    # database and Redis specific tests
PYPIHUB_TEST_DATABASE_URL=$PYPIHUB_TEST_POSTGRES_URL pytest   # the whole suite on PostgreSQL
```

The test databases are emptied by the tests: use ones you do not care about.

GitHub Actions: `ci.yml` (lint, tests on Python 3.10 to 3.13 and Windows, the whole suite on PostgreSQL and on MySQL
with Redis, wheel build and smoke test of a minimal and a full install, Docker build and run),
`docker.yml` (publish the image to GHCR) and `publish.yml` (publish a GitHub release to PyPI through trusted publishing).

## API Endpoints

### Web Interface
- `GET /` - Package browser: `?q=` search, `?source=all|local|cached`, `?sort=name|updated|files|size`, `?dir=asc|desc`, `?page=`
- `GET /project/<name>/` - Package page: details from PyPI (README, links, releases) and the files stored here
- `GET /project/<name>/<version>/` - The same for one release
- `GET /healthz` - JSON health check: directories, database and Redis (used by the Docker `HEALTHCHECK`)

### Package Management
- `POST /upload/<package>/` - Upload package (with auth)
- `POST /upload/` - Upload package via twine (with auth)
- `GET /packages/<package>/<filename>` - Serve local package files
- `GET /cache/<package>/<filename>` - Serve cached package files

### Package Index
- `GET /simple/<package>/` - Simple index (PEP 503) merging local files, cached files and PyPI's list, with hashes and `data-requires-python`

## Directory Structure

Source tree:

```
.
├── pypihub/
│   ├── pypihub.py          # Package index, upload and authentication (Flask app)
│   ├── web.py              # Web interface blueprint: /, /project/NAME/, /healthz
│   ├── database.py         # Models and engine setup for SQLite, PostgreSQL and MySQL
│   ├── catalog.py          # Database index of stored files: paged listing, statistics, reindex, prune
│   ├── pypimeta.py         # Package details copied from PyPI: reduction, storage, refresh
│   ├── readme.py           # Safe HTML for READMEs (Markdown, reStructuredText, plain text)
│   ├── upstream.py         # PyPI client: timeouts, retries, cached index, circuit breaker
│   ├── cache.py            # Key/value cache: in-process or Redis
│   ├── limits.py           # Rate limits
│   ├── dist.py             # Validation of project and file names
│   ├── logger.py           # Logging setup
│   ├── settings.py         # Settings file (optional)
│   ├── pypihub.ini         # Configuration file (optional)
│   ├── templates/          # Jinja templates for the web interface
│   └── static/             # CSS, JS, fonts, icons (Bootstrap vendored, see vendor/README.md)
├── tests/                  # pytest suite
├── docker/entrypoint.sh    # Container entrypoint
├── Dockerfile
├── docker-compose.yml        # SQLite, one container
├── docker-compose.production.yml   # PostgreSQL + Redis
└── .github/workflows/      # ci.yml, docker.yml, publish.yml
```

Data directories (the `/data` volume in Docker):

```
packages/                   # Uploaded packages
└── package-name/
    ├── package-1.0.0.tar.gz
    └── package-1.0.0-py3-none-any.whl
cache/                      # Files fetched from PyPI
└── requests/
    └── requests-2.28.1-py3-none-any.whl
pypihub.db                  # Users and package records (SQLite by default)
```

## Authentication

Default credentials:
- Username: `pypihub`
- Password: `pypihub`

`Change the default credentials before using in production.`

To change credentials, edit configuration in `settings.py`, `pypihub.ini`, or environment variables.

example 'pypihub.ini' for authentication

```ini
[auths]
users = userame password
```

## Logging

PyPihub uses custom logging with levels:
- EMERGENCY
- CRITICAL
- ERROR
- WARNING
- NOTICE
- INFO
- DEBUG

Set `verbose` mode with `-v` for debug logging.

## Troubleshooting

### Package not found
- Ensure package exists in local directory or is available on PyPI
- Check `PYPI_SIMPLE_URL` configuration

### Upload failed
- Check authentication credentials
- Ensure `LOCAL_PKG_DIR` directory is writable
- Check if file already exists (409 error)

### Cache not working
- Ensure `CACHE_DIR` directory is writable
- Check internet connection for downloading from PyPI

### Port already in use
- Change port with `-P` parameter or configuration
- Check processes using port: `lsof -i :5000`

## Development

### Dependencies
- Flask - Web framework
- BeautifulSoup4 - HTML parsing
- Requests - HTTP client
- Rich - Console output formatting
- ConfigSet - Configuration management
- PyDebugger - Debug utilities

### Contributing
1. Fork repository
2. Create feature branch
3. Commit changes
4. Push to branch
5. Create Pull Request
6. Describe your changes in the Pull Request.

## License

PyPihub is released under the [GNU Lesser General Public License v3.0 (LGPL-3.0)](https://www.gnu.org/licenses/lgpl-3.0.html).

You should have received a copy of the GNU Lesser General Public License along with this program.  
If not, see [https://www.gnu.org/licenses/](https://www.gnu.org/licenses/).

For commercial licensing options, please contact: [cumulus13@gmail.com](mailto:cumulus13@gmail.com)

## Support

For issues and questions, please create/open an issue in the repository or contact [cumulus13@gmail.com](mailto:cumulus13@gmail.com).

---

**PyPihub** - Simplifying local Python package management

`Warning: Do not expose this server to the public internet without proper authentication and security measures.`

---

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)
