"""Catalog tests. They run on SQLite always, and on PostgreSQL and MySQL when these are set:

    PYPIHUB_TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1/db
    PYPIHUB_TEST_MYSQL_URL=mysql://user:pass@127.0.0.1/db
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from pypihub import database
from pypihub.catalog import Catalog
from pypihub.database import DistFile, utcnow

from conftest import BACKENDS  # noqa: E402


@pytest.fixture
def catalog(engine):
    return Catalog(database.session_factory(engine), cache=None)


def _fill(catalog, n, source="cached", prefix="pkg"):
    for i in range(n):
        catalog.record_file(f"{prefix}{i:03d}", f"{prefix}{i:03d}-1.{i}-py3-none-any.whl", source, 1000 + i)


# --------------------------------------------------------------------------
# Schema and writes
# --------------------------------------------------------------------------

def test_schema_is_created_and_creation_is_idempotent(engine):
    database.init_db(engine, attempts=1)
    assert {"users", "packages", "dist_files"} <= set(database.Base.metadata.tables)


def test_record_file_inserts_then_updates_without_duplicates(catalog, engine):
    for size in (10, 20, 30):
        catalog.record_file("my_pkg", "my_pkg-1.0-py3-none-any.whl", "local", size, sha256="a" * 64)
    with catalog._session() as s:
        rows = s.execute(select(DistFile)).scalars().all()
    assert len(rows) == 1 and rows[0].size == 30 and rows[0].sha256 == "a" * 64
    assert rows[0].project_key == "my-pkg" and rows[0].version == "1.0" and rows[0].kind == "wheel"


def test_same_file_may_exist_in_local_and_cached(catalog):
    catalog.record_file("both", "both-1.0.tar.gz", "local", 5)
    catalog.record_file("both", "both-1.0.tar.gz", "cached", 5)
    page = catalog.list_projects()
    assert page.total == 1 and page.items[0].files == 2 and page.items[0].sources == ["local", "cached"]


def test_concurrent_record_of_the_same_file_leaves_one_row(tmp_path, request, engine):
    catalog = Catalog(database.session_factory(engine))

    def record(_):
        catalog.record_file("race", "race-1.0-py3-none-any.whl", "cached", 42)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(record, range(32)))
    with catalog._session() as s:
        assert len(s.execute(select(DistFile)).scalars().all()) == 1


def test_concurrent_schema_creation_from_many_workers(request, tmp_path):
    # fresh, empty database each time, created by 6 "workers" at once
    for backend in BACKENDS:
        url = f"sqlite:///{tmp_path / 'race.db'}" if backend == "sqlite" else BACKENDS[backend]
        if not url:
            continue
        probe = database.make_engine(url)
        database.Base.metadata.drop_all(probe)

        def start(_):
            engine = database.make_engine(url)
            try:
                database.init_db(engine, attempts=6, delay=0.2)
            finally:
                engine.dispose()

        with ThreadPoolExecutor(6) as pool:
            list(pool.map(start, range(6)))
        from sqlalchemy import inspect
        assert {"users", "packages", "dist_files"} <= set(inspect(probe).get_table_names())
        database.Base.metadata.drop_all(probe)
        probe.dispose()


# --------------------------------------------------------------------------
# Listing, pagination, search, sort
# --------------------------------------------------------------------------

def test_pagination_slices_and_clamps(catalog):
    _fill(catalog, 95)
    first = catalog.list_projects(page=1, per_page=30)
    assert (first.total, first.pages, first.first, first.last) == (95, 4, 1, 30)
    assert [i.key for i in first.items][:2] == ["pkg000", "pkg001"]
    last = catalog.list_projects(page=4, per_page=30)
    assert len(last.items) == 5 and (last.first, last.last) == (91, 95)
    beyond = catalog.list_projects(page=99, per_page=30)
    assert beyond.page == 4 and beyond.items == last.items
    assert catalog.list_projects(page=0).page == 1
    assert len(catalog.list_projects(per_page=1000).items) == 95  # capped at 100, there are 95


def test_pages_do_not_overlap_and_cover_everything(catalog):
    _fill(catalog, 61)
    seen = []
    for page in range(1, 4):
        seen += [i.key for i in catalog.list_projects(page=page, per_page=25).items]
    assert len(seen) == 61 and len(set(seen)) == 61 and seen == sorted(seen)


def test_search_matches_normalised_substring_only(catalog):
    for name in ("flask", "flask_cors", "Flask-Login", "requests", "my.pkg"):
        catalog.record_file(name, f"{name}-1.0.tar.gz", "cached", 1)
    keys = lambda q: [i.key for i in catalog.list_projects(q=q).items]  # noqa: E731
    assert keys("flask") == ["flask", "flask-cors", "flask-login"]
    assert keys("FLASK_C") == ["flask-cors"]
    assert keys("my_pkg") == ["my-pkg"]
    assert keys("zzz") == []


@pytest.mark.parametrize("q", ["%", "_", "\\", "%%", "a%b", "' OR 1=1 --", "x'; DROP TABLE dist_files;--"])
def test_search_input_is_not_a_pattern_or_sql(catalog, q):
    _fill(catalog, 5)
    assert catalog.list_projects(q=q).total == 0
    assert catalog.list_projects().total == 5  # the table is still there


def test_source_filter_and_aggregates(catalog):
    catalog.record_file("only_local", "only_local-1.0.tar.gz", "local", 10)
    catalog.record_file("only_cached", "only_cached-1.0.tar.gz", "cached", 20)
    catalog.record_file("dual", "dual-1.0.tar.gz", "local", 30)
    catalog.record_file("dual", "dual-2.0.tar.gz", "cached", 40)
    assert [i.key for i in catalog.list_projects(source="local").items] == ["dual", "only-local"]
    assert [i.key for i in catalog.list_projects(source="cached").items] == ["dual", "only-cached"]
    dual = next(i for i in catalog.list_projects().items if i.key == "dual")
    assert (dual.files, dual.size, dual.sources, dual.latest) == (2, 70, ["local", "cached"], "2.0")
    assert catalog.list_projects(source="local").total == 2


def test_sorting(catalog):
    now = utcnow()
    catalog.record_file("aaa", "aaa-1.0.tar.gz", "cached", 300, created_at=now - timedelta(days=3))
    catalog.record_file("bbb", "bbb-1.0.tar.gz", "cached", 100, created_at=now - timedelta(days=1))
    catalog.record_file("bbb", "bbb-2.0.tar.gz", "cached", 100, created_at=now - timedelta(days=1))
    catalog.record_file("ccc", "ccc-1.0.tar.gz", "cached", 200, created_at=now - timedelta(days=2))
    order = lambda **kw: [i.key for i in catalog.list_projects(**kw).items]  # noqa: E731
    assert order() == ["aaa", "bbb", "ccc"]
    assert order(sort="name", descending=True) == ["ccc", "bbb", "aaa"]
    assert order(sort="size", descending=True) == ["aaa", "bbb", "ccc"]  # 300, then 200 and 200 broken by name
    assert order(sort="files", descending=True)[0] == "bbb"
    assert order(sort="updated", descending=True) == ["bbb", "ccc", "aaa"]
    assert order(sort="nonsense") == ["aaa", "bbb", "ccc"]


def test_latest_version_prefers_stable_releases(catalog):
    for fn in ("pre-1.0-py3-none-any.whl", "pre-2.0rc1-py3-none-any.whl", "pre-0.9.tar.gz"):
        catalog.record_file("pre", fn, "cached", 1)
    catalog.record_file("onlypre", "onlypre-3.0b1-py3-none-any.whl", "cached", 1)
    items = {i.key: i for i in catalog.list_projects().items}
    assert items["pre"].latest == "1.0" and items["onlypre"].latest == "3.0b1"


def test_stats(catalog):
    catalog.record_file("a", "a-1.0.tar.gz", "local", 100)
    catalog.record_file("b", "b-1.0.tar.gz", "cached", 200)
    catalog.record_file("b", "b-2.0.tar.gz", "cached", 300)
    s = catalog.stats()
    assert (s.projects, s.local_projects, s.cached_projects, s.files, s.size, s.cached_size) == (2, 1, 1, 3, 600, 500)
    assert catalog.stats().projects == 2


def test_get_project_orders_files_newest_version_first(catalog):
    for v in ("1.0", "10.0", "2.0"):
        catalog.record_file("zz", f"zz-{v}-py3-none-any.whl", "cached", 1)
    catalog.record_file("zz", "zz-9.9.tar.gz", "cached", 1)
    detail = catalog.get_project("zz")
    assert [f.version for f in detail.files] == ["10.0", "9.9", "2.0", "1.0"]
    assert catalog.get_project("missing") is None and catalog.find("zz") == "zz" and catalog.find("nope") is None


# --------------------------------------------------------------------------
# Reindex and prune work on real directories
# --------------------------------------------------------------------------

def _write(root, project, filename, content=b"x", mtime=None):
    folder = root / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(content)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def test_reindex_adds_updates_and_removes(catalog, tmp_path):
    local, cache = tmp_path / "packages", tmp_path / "cache"
    local.mkdir(), cache.mkdir()
    _write(local, "mine", "mine-1.0-py3-none-any.whl", b"12345")
    _write(cache, "requests", "requests-2.0.tar.gz", b"1234567")
    (cache / "ghost").mkdir()  # empty directory
    _write(cache, "requests", ".hidden")
    _write(cache, "requests", "bad name.whl")
    assert catalog.reindex(local, cache) == {"added": 2, "updated": 0, "removed": 0}
    assert catalog.list_projects().total == 2
    assert catalog.reindex(local, cache) == {"added": 0, "updated": 0, "removed": 0}

    _write(cache, "requests", "requests-2.0.tar.gz", b"changed-size")  # size changed
    _write(cache, "requests", "requests-3.0.tar.gz")  # new
    (local / "mine" / "mine-1.0-py3-none-any.whl").unlink()  # gone, and so is the project
    assert catalog.reindex(local, cache) == {"added": 1, "updated": 1, "removed": 1}
    assert [i.key for i in catalog.list_projects().items] == ["requests"]
    assert catalog.get_project("requests").latest == "3.0"


def test_reindex_uses_file_mtime_as_added_time(catalog, tmp_path):
    local, cache = tmp_path / "p", tmp_path / "c"
    local.mkdir(), cache.mkdir()
    old = time.time() - 86400 * 30
    _write(cache, "old", "old-1.0.tar.gz", mtime=old)
    catalog.reindex(local, cache)
    assert abs((utcnow() - catalog.list_projects().items[0].updated).days - 30) <= 1


def test_reindex_with_one_directory_for_both_roles_counts_files_once(catalog, tmp_path):
    shared = tmp_path / "shared"
    _write(shared, "solo", "solo-1.0.tar.gz")
    catalog.record_file("solo", "solo-1.0.tar.gz", "cached", 1)  # left over from an older configuration
    catalog.reindex(shared, shared)
    item = catalog.list_projects().items[0]
    assert (item.files, item.sources) == (1, ["local"])


def test_prune_removes_oldest_cached_files_only(catalog, tmp_path):
    local, cache = tmp_path / "p", tmp_path / "c"
    local.mkdir(), cache.mkdir()
    base = time.time() - 10_000
    for i in range(10):
        _write(cache, f"old{i}", f"old{i}-1.0.tar.gz", b"x" * 1000, mtime=base + i)
    keep = _write(local, "mine", "mine-1.0.tar.gz", b"x" * 5000)
    catalog.reindex(local, cache)
    result = catalog.prune_cache(cache, max_bytes=5000, target_ratio=0.6)
    assert result["remaining"] <= 3000 and result["removed"] == 7 and result["freed"] == 7000
    remaining = sorted(p.name for p in cache.iterdir())
    assert remaining == ["old7", "old8", "old9"]  # the three newest survive, emptied directories are removed
    assert keep.exists() and catalog.get_project("mine") is not None
    assert catalog.prune_cache(cache, max_bytes=10**9)["removed"] == 0


def test_prune_never_deletes_outside_the_cache_directory(catalog, tmp_path):
    cache = tmp_path / "c"
    cache.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    with catalog._session() as s:  # a row that points outside, as a corrupted catalog could
        s.add(DistFile(project_key="..", project_name="..", filename="victim.txt", source="cached", kind="other",
                       size=10**6, created_at=utcnow()))
        s.commit()
    catalog.prune_cache(cache, max_bytes=1)
    assert victim.read_text() == "keep me"
