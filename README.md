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

## Installation

### Requirements

```bash
pip install flask beautifulsoup4 requests pydebugger configset rich rich-argparse pathlib
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
python pypihub.py

# With custom configuration
# Option: '-c /path/to/config.ini'
python pypihub.py -c /path/to/config.ini -H 127.0.0.1 -P 8080

# Verbose mode
python pypihub.py -v

# Show help
python pypihub.py -h
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

## API Endpoints

### Web Interface
- `GET /` - Main page with list of local packages

### Package Management
- `POST /upload/<package>/` - Upload package (with auth)
- `POST /upload/` - Upload package via twine (with auth)
- `GET /packages/<package>/<filename>` - Serve local package files
- `GET /cache/<package>/<filename>` - Serve cached package files

### Package Index
- `GET /simple/<package>/` - Simple index for specific package

## Directory Structure

```
pypihub/
├── pypihub.py              # Main application
├── pypihub.ini             # Configuration file (optional)
├── settings.py           # Settings file (optional)
├── packages/             # Local packages directory
│   └── package-name/
│       ├── package-1.0.0.tar.gz
│       └── package-1.0.0-py3-none-any.whl
├── cache/                # Cached packages from PyPI
│   └── requests/
│       └── requests-2.28.1-py3-none-any.whl
└── logs/                 # Log files (if configured)
```

## Authentication

Default credentials:
- Username: `pypihub`
- Password: `pypihub`

`Change the default credentials before using in production.`

To change credentials, edit configuration in `settings.py`, `pypihub.ini`, or environment variables.

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

## Author
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    
[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)

[Support me on Patreon](https://www.patreon.com/cumulus13)