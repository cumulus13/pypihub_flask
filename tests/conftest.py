#!/usr/bin/env python3

# File: tests/conftest.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-21
# Description: PyPihub reads its directories and secrets from the environment when the module is
# imported, so the environment is prepared first and the app is imported once per session.
# License: MIT

"""Shared fixtures.

PyPihub reads its directories and secrets from the environment when the module is
imported, so the environment is prepared first and the app is imported once per session.
"""
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # test the checkout, not an installed copy


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    base = tmp_path_factory.mktemp("pypihub")
    packages, cache = base / "packages", base / "cache"
    packages.mkdir()
    cache.mkdir()
    os.environ.update(
        {
            "BASE_DIR": str(base),
            "LOCAL_PKG_DIR": str(packages),
            "CACHE_DIR": str(cache),
            "CONFIGFILE": str(base / "pypihub.ini"),
            "SECRET_KEY": "test-secret-key",
            "PYPIHUB_UI_CACHE_TTL": "0",
        }
    )
    module = importlib.import_module("pypihub.pypihub")
    module.app.config["TESTING"] = True
    return SimpleNamespace(module=module, app=module.app, packages=packages, cache=cache, base=base)


@pytest.fixture(autouse=True)
def clean(server):
    """Every test starts with empty package and cache directories."""
    import shutil

    for folder in (server.packages, server.cache):
        for child in folder.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    server.module.web_ui.invalidate_cache()
    yield


@pytest.fixture
def client(server):
    return server.app.test_client()


@pytest.fixture
def add_file(server):
    def _add(where, package, filename, content=b"data", mtime=None):
        folder = (server.packages if where == "local" else server.cache) / package
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        path.write_bytes(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        server.module.web_ui.invalidate_cache()
        return path

    return _add
