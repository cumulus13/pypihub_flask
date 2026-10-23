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
            "PYPIHUB_META": "0",  # no request to pypi.org from the tests; the metadata tests switch it on with a fake upstream
        }
    )
    # Run the whole suite against another database or Redis: PYPIHUB_TEST_DATABASE_URL, PYPIHUB_TEST_REDIS_URL
    for source, target in (("PYPIHUB_TEST_DATABASE_URL", "DATABASE_URL"), ("PYPIHUB_TEST_REDIS_URL", "REDIS_URL")):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
        else:
            os.environ.pop(target, None)
    if "DATABASE_URL" not in os.environ:
        os.environ["DATABASE_URL"] = f"sqlite:///{base / 'test.db'}"
    module = importlib.import_module("pypihub.pypihub")
    module.app.config["TESTING"] = True
    return SimpleNamespace(module=module, app=module.app, packages=packages, cache=cache, base=base)


@pytest.fixture
def clean(server):
    """Every test starts with empty package and cache directories."""
    import shutil

    for folder in (server.packages, server.cache):
        for child in folder.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    from sqlalchemy import delete

    from pypihub.database import DistFile, Package, PypiMeta, User

    with server.module.SessionLocal() as db:
        for model in (DistFile, PypiMeta, Package, User):
            db.execute(delete(model))
        db.commit()
    server.module.KV_CACHE.delete("catalog:stats")
    server.module.limiter.reset()
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
        server.module.CATALOG.reindex(server.packages, server.cache)  # the UI reads the catalog, as after a real upload
        return path

    return _add


# ---------------------------------------------------------------------------
# Databases for the tests that need one on its own: SQLite always, PostgreSQL and MySQL when configured
#   PYPIHUB_TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1/db
#   PYPIHUB_TEST_MYSQL_URL=mysql://user:pass@127.0.0.1/db
# ---------------------------------------------------------------------------

BACKENDS = {
    "sqlite": None,
    "postgres": os.getenv("PYPIHUB_TEST_POSTGRES_URL"),
    "mysql": os.getenv("PYPIHUB_TEST_MYSQL_URL"),
}


@pytest.fixture(params=list(BACKENDS), ids=list(BACKENDS))
def engine(request, tmp_path):
    from pypihub import database

    name = request.param
    if name == "sqlite":
        url = f"sqlite:///{tmp_path / 'test.db'}"
    elif BACKENDS[name]:
        url = BACKENDS[name]
    else:
        pytest.skip(f"set PYPIHUB_TEST_{name.upper()}_URL to test {name}")
    engine = database.make_engine(url)
    database.Base.metadata.drop_all(engine)
    database.init_db(engine, attempts=2)
    yield engine
    database.Base.metadata.drop_all(engine)
    engine.dispose()
