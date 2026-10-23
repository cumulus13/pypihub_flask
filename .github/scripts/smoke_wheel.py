"""Smoke test for an installed PyPihub wheel (run in a clean virtualenv, outside the checkout).

Checks what unit tests in the checkout cannot: that the wheel contains the templates and
static files, that the console script entry point resolves, and that the app serves pages.

    smoke_wheel.py minimal   installed with the [flask] extra only: pages work, READMEs fall back to plain text
    smoke_wheel.py full      installed with the [all] extra: README rendering is available too
"""
import importlib
import os
import sys
import tempfile
from importlib.metadata import entry_points
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "minimal"
assert mode in ("minimal", "full"), "usage: smoke_wheel.py minimal|full"
base = Path(tempfile.mkdtemp(prefix="pypihub-smoke-"))
(base / "packages").mkdir()
(base / "cache").mkdir()
os.environ.update(
    BASE_DIR=str(base),
    LOCAL_PKG_DIR=str(base / "packages"),
    CACHE_DIR=str(base / "cache"),
    CONFIGFILE=str(base / "pypihub.ini"),
    SECRET_KEY="smoke-test",
    DATABASE_URL=f"sqlite:///{base / 'smoke.db'}",
    PYPIHUB_META="0",  # no request to pypi.org from a smoke test
)

import pypihub  # noqa: E402

package_dir = Path(pypihub.__file__).resolve().parent
assert "site-packages" in str(package_dir), f"expected an installed copy, got {package_dir}"
for required in (
    "templates/index.html",
    "templates/package.html",
    "templates/_macros.html",
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

module = importlib.import_module("pypihub.pypihub")
app, catalog = module.app, module.CATALOG
from pypihub import readme  # noqa: E402

assert readme.available() is (mode == "full"), f"README rendering should be {'on' if mode == 'full' else 'off'} in {mode} mode"
if mode == "full":
    assert "<strong>" in readme.render("**x**", "text/markdown")
stored = base / "packages" / "smoke-pkg" / "smoke_pkg-1.0-py3-none-any.whl"
stored.parent.mkdir()
stored.write_bytes(b"not really a wheel")
assert catalog.reindex(str(base / "packages"), str(base / "cache")) == {"added": 1, "updated": 0, "removed": 0}
client = app.test_client()
assert client.get("/healthz").status_code == 200
page = client.get("/")
assert page.status_code == 200 and b"Python packages, served locally." in page.data
assert client.get("/static/css/pypihub.css").status_code == 200
assert b"smoke-pkg" in client.get("/").data
assert client.get("/project/smoke-pkg/").status_code == 200
index = client.get("/simple/smoke-pkg/")  # a local file is listed whether or not the upstream answers
assert index.status_code == 200 and b"smoke_pkg-1.0-py3-none-any.whl" in index.data
assert client.get("/packages/smoke-pkg/smoke_pkg-1.0-py3-none-any.whl").data == b"not really a wheel"
assert catalog.reindex(str(base / "packages"), str(base / "cache")) == {"added": 0, "updated": 0, "removed": 0}
stored.unlink()
assert catalog.reindex(str(base / "packages"), str(base / "cache")) == {"added": 0, "updated": 0, "removed": 1}
print(f"wheel smoke test passed ({mode}):", package_dir)
sys.exit(0)
