"""Smoke test for an installed PyPihub wheel (run in a clean virtualenv, outside the checkout).

Checks what unit tests in the checkout cannot: that the wheel contains the templates and
static files, that the console script entry point resolves, and that the app serves pages.
"""
import importlib
import os
import sys
import tempfile
from importlib.metadata import entry_points
from pathlib import Path

base = Path(tempfile.mkdtemp(prefix="pypihub-smoke-"))
(base / "packages").mkdir()
(base / "cache").mkdir()
os.environ.update(
    BASE_DIR=str(base),
    LOCAL_PKG_DIR=str(base / "packages"),
    CACHE_DIR=str(base / "cache"),
    CONFIGFILE=str(base / "pypihub.ini"),
    SECRET_KEY="smoke-test",
)

import pypihub  # noqa: E402

package_dir = Path(pypihub.__file__).resolve().parent
assert "site-packages" in str(package_dir), f"expected an installed copy, got {package_dir}"
for required in (
    "templates/index.html",
    "templates/package.html",
    "static/css/pypihub.css",
    "static/js/pypihub.js",
    "static/vendor/bootstrap/bootstrap.min.css",
    "static/fonts/bricolage-grotesque-latin-wght-normal.woff2",
):
    assert (package_dir / required).is_file(), f"missing from the wheel: {required}"

scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
assert scripts.get("pypihub") == "pypihub.pypihub:usage", scripts.get("pypihub")
module_name, _, attr = scripts["pypihub"].partition(":")
assert callable(getattr(importlib.import_module(module_name), attr))

app = importlib.import_module("pypihub.pypihub").app
client = app.test_client()
assert client.get("/healthz").status_code == 200
page = client.get("/")
assert page.status_code == 200 and b"Python packages, served locally." in page.data
assert client.get("/static/css/pypihub.css").status_code == 200
print("wheel smoke test passed:", package_dir)
sys.exit(0)
